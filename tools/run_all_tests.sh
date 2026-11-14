#!/usr/bin/env bash
# 全量测试一键跑：Python 套件 + Node 套件 + 在线接口检查
set -u
cd "$(dirname "$0")/.." || exit 1
DEP="$(cygpath -u "$USERPROFILE")/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies"
PY="$DEP/python/python.exe"
NODE="$DEP/node/bin/node.exe"
export PYTHONUTF8=1
URL="${1:-http://127.0.0.1:8765}"

total=0
fail=0
run() {
  local name="$1"; shift
  local out rc
  out="$("$@" 2>&1)"; rc=$?
  local num
  num=$(printf '%s\n' "$out" | grep -oE '[0-9]+ ?项' | tail -1 | grep -oE '[0-9]+' || true)
  if [ -z "${num:-}" ]; then num=0; fi
  total=$((total + num))
  if [ $rc -ne 0 ]; then fail=$((fail + 1)); fi
  printf '%-16s rc=%d  %s\n' "$name" "$rc" "$(printf '%s\n' "$out" | tail -1)"
  if [ $rc -ne 0 ]; then printf '%s\n' "$out" | tail -25; fi
}

run selftest        "$PY" tools/selftest.py
run imports         "$PY" tools/test_imports.py
run strava_offline  "$PY" tools/test_strava_offline.py
run coach           "$PY" tools/test_coach.py
run program         "$PY" tools/test_program.py
run activity        "$PY" tools/test_activity.py
run devices         "$PY" tools/test_devices.py
run strava_api      "$PY" tools/test_strava_api.py
run routebook       "$PY" tools/test_routebook.py
run llm             "$PY" tools/test_llm.py
run auth            "$PY" tools/test_auth.py
run ble_ui          "$NODE" tools/test_ble_ui.js
run routing         "$NODE" tools/test_routing_js.js
run check_api       "$PY" tools/check_api.py "$URL"

echo "--------"
echo "TOTAL=$total FAILED_SUITES=$fail"
exit $fail
