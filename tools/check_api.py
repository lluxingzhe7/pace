"""API 冒烟测试：对运行中的服务逐个打接口，报告状态与耗时。

用法: python tools/check_api.py [base_url]
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
AUTH: dict[str, str] = {}
PASSED = 0
FAILED: list[str] = []


def check(name, cond, detail=""):
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ok   {name:42} {detail}")
    else:
        FAILED.append(f"{name} {detail}")
        print(f"  FAIL {name:42} {detail}")


def get(path, timeout=20):
    t0 = time.time()
    req = urllib.request.Request(BASE + path,
                                 headers={"Accept": "application/json", **AUTH})
    # 本机服务不该走系统代理：环境里挂了 Privoxy 时 urlopen 会返回 500
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        body = resp.read()
    return resp.status, json.loads(body.decode("utf-8")), (time.time() - t0) * 1000


def post(path, payload, timeout=60):
    data = json.dumps(payload).encode("utf-8") if payload is not None else b""
    req = urllib.request.Request(BASE + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8", **AUTH})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        _remember_cookie(resp)
        return resp.status, json.loads(resp.read().decode("utf-8"))


def put(path, payload, timeout=60):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, method="PUT",
                                 headers={"Content-Type": "application/json; charset=utf-8", **AUTH})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def delete(path, timeout=20):
    req = urllib.request.Request(BASE + path, method="DELETE", headers=dict(AUTH))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _remember_cookie(resp):
    for header in resp.headers.get_all("Set-Cookie") or []:
        if header.startswith("pace_session="):
            AUTH["Cookie"] = header.split(";", 1)[0]


def login_for_tests():
    """用测试账号登录；没有就先注册（注册关了就设 PACE_TEST_USER/PASS）。"""
    user = os.environ.get("PACE_TEST_USER") or "apitest"
    password = os.environ.get("PACE_TEST_PASS") or "apitest12345"
    try:
        status, me = get("/api/auth/me")
        if status == 200:
            return me.get("user")
    except Exception:
        pass
    for path in ("/api/auth/login", "/api/auth/register"):
        try:
            payload = {"username": user, "password": password, "remember": True}
            status, body = post(path, payload)
            if status == 200 and body.get("user"):
                return body["user"]
        except urllib.error.HTTPError:
            continue
    return None


def status_of(path):
    try:
        get(path)
        return 200
    except urllib.error.HTTPError as exc:
        return exc.code


def main() -> int:
    print(f"目标服务: {BASE}")
    try:
        status, health, ms = get("/api/health")
    except Exception as exc:
        print(f"无法连接: {exc}")
        return 2
    check("health", status == 200 and health.get("ok"), f"{health.get('activities')} 活动 {ms:.0f}ms")

    # 账号：未登录要挡住；然后登录（没有就注册）
    try:
        code = status_of("/api/activities")
    except Exception:
        code = 0
    check("未登录访问接口被挡住", code in (401, 200),
          f"{code}（200 = 该实例用了 --no-auth）")
    user = login_for_tests()
    check("测试账号能登录", bool(user), (user or {}).get("username", "登录失败"))
    if user:
        status, me, _ = get("/api/auth/me")
        check("me 返回当前账号", status == 200 and me.get("user", {}).get("id") == user.get("id"))
        try:
            status, _, _ = get("/api/auth/users")
        except urllib.error.HTTPError as exc:
            status = exc.code
        check("成员列表只对站长开放", status in (200, 403), str(status))

    status, meta, ms = get("/api/meta")
    check("meta", status == 200 and "settings" in meta)

    status, dash, ms = get("/api/dashboard?days=28&tz=8")
    check("dashboard", status == 200 and "weekly_buckets" in dash, f"{ms:.0f}ms")
    check("dashboard 周桶=12", len(dash.get("weekly_buckets", [])) == 12)
    check("dashboard 热力图=182", len(dash.get("heatmap", [])) == 182)
    check("dashboard 体能曲线=120", len(dash.get("load_series", [])) == 120)

    status, acts, ms = get("/api/activities?limit=5")
    check("activities", status == 200 and len(acts.get("items", [])) <= 5, f"total={acts.get('total')}")

    if acts.get("items"):
        aid = acts["items"][0]["id"]
        status, detail, ms = get(f"/api/activities/{aid}")
        check("activity 详情", status == 200 and "streams" in detail, f"{ms:.0f}ms")
        check("activity 有分段字段", "segments" in detail)
        ids = ",".join(str(x["id"]) for x in acts["items"][:3])
        status, cmp_, ms = get(f"/api/activities/compare?ids={ids}")
        check("compare", status == 200 and len(cmp_.get("activities", [])) >= 1)

    for path, expected in (
        ("/api/activities/999999", 404),
        ("/api/segments/999999", 404),
        ("/api/nope", 404),
        ("/api/activities/abc", 400),
    ):
        code = status_of(path)
        check(f"错误路径 {path}", code == expected, f"{code} (期望 {expected})")

    status, prs, ms = get("/api/prs")
    check("prs", status == 200 and "records" in prs, f"{len(prs.get('records', []))} 条")

    status, segs, ms = get("/api/segments")
    check("segments", status == 200 and "segments" in segs)

    status, sug, ms = get("/api/segments/suggest")
    check("segments/suggest", status == 200 and "suggestions" in sug, f"{ms:.0f}ms")

    status, settings, ms = get("/api/settings")
    check("settings", status == 200 and settings.get("settings", {}).get("hr_max"))

    # ---------------------------------------------------------------- 路书
    status, rbs, ms = get("/api/routebooks")
    check("routebooks 列表", status == 200 and "routebooks" in rbs,
          f"{len(rbs.get('routebooks', []))} 条 {ms:.0f}ms")

    made_ids: list[int] = []
    try:
        status, made = post("/api/routebooks", {
            "name": "API 冒烟临时路书",
            "points": [[23.10, 113.30], [23.11, 113.31], [23.12, 113.33], [23.12, 113.36]],
            "waypoints": [
                {"lat": 23.10, "lon": 113.30, "name": "起点", "tag": "打卡点", "note": "",
                 "show": True},
                {"lat": 23.12, "lon": 113.33, "name": "补给", "tag": "食物补给",
                 "note": "有水和面", "show": True},
            ],
            "climb_m": 45.5,
        })
        rid = made.get("id")
        made_ids.append(rid)
        check("routebook 新建", status == 201 and bool(rid), str(status))
        info = made.get("routebook") or {}
        check("新建就带分析结果", "cues" in info and "climbs" in info, str(sorted(info)))
        check("手绘路书没有海拔剖面", info.get("profile") == [], str(len(info.get("profile") or [])))
        check("手绘路书说明提到海拔",
              any("海拔" in n for n in info.get("notes", [])), str(info.get("notes")))
        status, one, ms = get(f"/api/routebooks/{rid}")
        check("routebook 详情", status == 200 and one.get("point_count") == 4,
              str(one.get("point_count")))
        wps = one.get("waypoints") or []
        check("途经点存下来了", len(wps) == 2, str(len(wps)))
        check("途经点带里程",
              all(isinstance(w.get("d"), (int, float)) for w in wps), str(wps))
        check("途经点字段返回", wps[1].get("tag") == "食物补给" and wps[1].get("note") == "有水和面",
              str(wps[1]) if wps else "empty")
        check("爬升能存下来", abs(float(one.get("climb_m") or 0) - 45.5) < 0.01,
              str(one.get("climb_m")))
        req = urllib.request.Request(f"{BASE}/api/routebooks/{rid}/gpx", headers=dict(AUTH))
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=20) as resp:
            gpx = resp.read().decode("utf-8")
        check("GPX 带途经点航点", gpx.count("<wpt ") == 2, str(gpx.count("<wpt ")))
        cues_before = len((one.get("routebook") or {}).get("cues", []))
        status, renamed = put(f"/api/routebooks/{rid}", {"name": "改名后的路书"})
        check("routebook 改名", status == 200 and renamed.get("name") == "改名后的路书",
              str(renamed.get("name")))
        cues_after = len((renamed.get("routebook") or {}).get("cues", []))
        check("改名不丢转向提示", cues_after == cues_before, f"{cues_after}/{cues_before}")

        # 从活动生成的路书：改名不能把爬坡/公里节点弄丢（streams 要重新挂上）
        status, allacts, ms = get("/api/activities?limit=40")
        gps = [a for a in allacts.get("items", []) if a.get("has_gps")]
        if gps:
            status, fromact = post("/api/routebooks/from-activity",
                                   {"activity_id": gps[0]["id"]})
            bid = fromact.get("id")
            made_ids.append(bid)
            check("routebook 从活动生成", status == 201 and bool(bid), str(status))
            status, full, ms = get(f"/api/routebooks/{bid}")
            info2 = full.get("routebook") or {}
            kms_before = len(info2.get("kms", []))
            climbs_before = len(info2.get("climbs", []))
            prof_before = len(info2.get("profile", []))
            check("活动路书有公里节点", kms_before > 0, str(kms_before))
            check("活动路书有海拔剖面", prof_before > 10, str(prof_before))
            check("剖面有最高/最低海拔",
                  info2.get("alt_max") is not None and info2.get("alt_min") is not None,
                  f"{info2.get('alt_min')}/{info2.get('alt_max')}")
            status, renamed2 = put(f"/api/routebooks/{bid}", {"name": "活动路书改名"})
            after = renamed2.get("routebook") or {}
            check("活动路书改名保留公里节点",
                  len(after.get("kms", [])) == kms_before,
                  f"{len(after.get('kms', []))}/{kms_before}")
            check("活动路书改名保留爬坡",
                  len(after.get("climbs", [])) == climbs_before,
                  f"{len(after.get('climbs', []))}/{climbs_before}")
            check("活动路书改名保留剖面",
                  len(after.get("profile", [])) == prof_before,
                  f"{len(after.get('profile', []))}/{prof_before}")
    finally:
        for rid in made_ids:
            if rid:
                try:
                    delete(f"/api/routebooks/{rid}")
                except Exception as exc:      # noqa: BLE001 - 清理失败不该盖住主结果
                    print(f"  warn 清理临时路书失败 #{rid}: {exc}")

    status, rbs2, ms = get("/api/routebooks")
    check("临时路书已清理", len(rbs2.get("routebooks", [])) == len(rbs.get("routebooks", [])),
          f"{len(rbs2.get('routebooks', []))}/{len(rbs.get('routebooks', []))}")

    # 静态资源
    for path, kind in (("/", "text/html"), ("/css/app.css", "text/css"),
                       ("/js/app.js", "javascript"), ("/favicon.svg", "svg")):
        req = urllib.request.Request(BASE + path, headers=dict(AUTH))
        with urllib.request.urlopen(req, timeout=15) as resp:
            check(f"static {path}", resp.status == 200 and kind in resp.headers.get("Content-Type", ""),
                  resp.headers.get("Content-Type", ""))

    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项 / 通过 {PASSED} 项")
        for f in FAILED:
            print("  -", f)
        return 1
    print(f"全部通过（{PASSED} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
