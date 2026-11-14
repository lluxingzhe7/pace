# 纯前端版（GitHub Pages，手机直接打开）

**线上地址：<https://lluxingzhe7.github.io/pace/>**（2026-11-14 已部署成功，`pages` 工作流 success）


数据存在**浏览器本地**（IndexedDB），没有服务器，电脑不用开。
引擎是原来的 Python 代码（Pyodide 在浏览器里跑），所以分析/教练/路书逻辑和后端版一致。

## 现在怎么发布

1. 仓库 → **Settings → Pages** → Build and deployment → Source 选 **GitHub Actions**
2. 等 `pages` 工作流跑完（Actions 页面能看到）
3. 手机打开 **https://lluxingzhe7.github.io/pace/**
4. 首次会显示「正在加载分析引擎」，要下载约 10 MB（Pyodide + sqlite3），**之后会被 Service Worker 缓存**
5. 浏览器菜单 → 添加到主屏幕，就是 App 样子

## 已验证 / 未验证

**已验证（2026-11-14，无头 Edge 实测）**
- 引擎启动：Pyodide → 解包 `pace.zip` → 加载 sqlite3 → 页面可用
- 导入：浏览器里造一个 900 点的 GPX，`api.importFiles` 返回 `status=ok, 10.5 km, has_hr=true`
- 仪表盘：导入后刷新页面，侧栏「1 条活动 · v1.1.0」、仪表盘 6 张卡片、PR 表都有数据
- **持久化**：刷新页面后活动还在（说明 IndexedDB → SQLite 恢复链路通了）
- 设置页：纯前端模式出现「导出数据包 / 导入数据包」，数据源入口自动隐藏
- 后端回归：auth 62 / selftest 79 / routebook 83 全绿

**未完成 / 限制**
- 首次加载慢：Pyodide + sqlite3 约 10 MB，冷启动 30–70 秒（之后 Service Worker 缓存）
- 没有逐页回归完：活动详情、教练、路书编辑器还没逐页点测
- 账号系统在纯前端模式关闭（数据只在本机浏览器）
- 依赖服务器的功能不可用：数据源同步、Strava OAuth、LLM 教练（入口已隐藏/会报错）
- 多设备不同步；清站点数据会丢，用「导出数据包」备份
- GitHub Pages 在国内访问速度和稳定性一般

## 为什么打开是 404

GitHub Pages 还没启用，`pages` 工作流跑过但失败了（`conclusion=failure`）。
去仓库 **Settings → Pages → Build and deployment → Source = GitHub Actions**，再点 Re-run 那个工作流（或随便改个文件推一次）。

## 本机怎么试

```bash
python tools/build_web_bundle.py
cd web && python -m http.server 8808
# 打开 http://127.0.0.1:8808/?static=1
```

## 注意

- 换浏览器 / 清站点数据 / 无痕模式 = 数据没了。要备份的话目前只能导出：设置页还没有「导出数据库」按钮，后续补。
- 改 `pace/` 下任何 .py 之后要重新跑 `tools/build_web_bundle.py`（Pages 工作流已自动做）。
