# 纯前端版（GitHub Pages，手机直接打开）

**线上地址：<https://lluxingzhe7.github.io/pace/>**（2026-11-14 已部署成功，`pages` 工作流 success）


数据存在**浏览器本地**（IndexedDB），没有服务器，电脑不用开。
引擎是原来的 Python 代码（Pyodide 在浏览器里跑），所以分析/教练/路书逻辑和后端版一致。

## 账号（纯前端版）

和网页版一样，第一次打开是**创建账号**，之后每次进去先**登录**；不同账号的数据分开存（IndexedDB 里 `db:<用户名>`）。

- 密码用浏览器 WebCrypto 的 **PBKDF2-SHA256（12 万次）** 加盐哈希，不保存明文
- 「记住我」60 天；退出后回到登录页
- 顶栏账号按钮可改密码、退出
- **这是本机浏览器账号，不是服务器账号**：能挡住别人随手打开你的 PACE，
  但懂开发者工具的人仍能读到浏览器里的数据（Web 的固有边界）

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

**逐页回归（2026-11-14 晚，本地静态服务）**

| 页面 | 结果 |
|---|---|
| 仪表盘 | 6 张卡片，数据正确 |
| 运动数据列表 | 「共 1 条」，累计距离/时间统计正常 |
| 活动详情 | 4 个图表 canvas，标题「跑步详情」，指标 10.5 km |
| 教练 | 4 张卡片正常 |
| 分段成绩 | 2 张卡片（空态） |
| 路书 | 2 张卡片（空态） |
| 设置 | 导出/导入数据包按钮存在 |
| 刷新持久化 | 数据仍在（IndexedDB） |
| 控制台 | 零报错 |

**未完成 / 限制**
- 首次加载慢：Pyodide + sqlite3 约 10 MB，冷启动 30–70 秒（之后 Service Worker 缓存）
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

## 性能与 AI

- **实测**：线上冷启动 27.1 秒（首次要下运行时），二次打开 **4.1 秒**（Service Worker 命中缓存）。
- 启动时显示分阶段进度条（① 运行时 ② 启动 ③ sqlite3 ④ PACE 引擎 ⑤ 恢复数据），失败有「重试」按钮。
- **AI 教练在手机版也能用**：DeepSeek 接口允许跨域，浏览器直接调用；
  Key 只存在本机浏览器（设置 → AI 教练，页头会标「Key 只存本机浏览器」）。
  注意：浏览器用的是同步 XHR，AI 回答时界面会短暂卡住（手机上通常 2–10 秒）。

## 注意

- 换浏览器 / 清站点数据 / 无痕模式 = 数据没了。要备份的话目前只能导出：设置页还没有「导出数据库」按钮，后续补。
- 改 `pace/` 下任何 .py 之后要重新跑 `tools/build_web_bundle.py`（Pages 工作流已自动做）。
