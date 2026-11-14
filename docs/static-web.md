# 纯前端版（GitHub Pages，手机直接打开）

数据存在**浏览器本地**（IndexedDB），没有服务器，电脑不用开。
引擎是原来的 Python 代码（Pyodide 在浏览器里跑），所以分析/教练/路书逻辑和后端版一致。

## 现在怎么发布

1. 仓库 → **Settings → Pages** → Build and deployment → Source 选 **GitHub Actions**
2. 等 `pages` 工作流跑完（Actions 页面能看到）
3. 手机打开 **https://lluxingzhe7.github.io/pace/**
4. 首次会显示「正在加载分析引擎」，要下载约 10 MB（Pyodide + sqlite3），**之后会被 Service Worker 缓存**
5. 浏览器菜单 → 添加到主屏幕，就是 App 样子

## 已验证 / 未验证

**已验证**
- `pace/webbridge.py` 在 CPython 里跑通全部接口：health / meta / dashboard / activities / import（导入 sample GPX 后 total=1）
- 浏览器里引擎能启动：Pyodide 加载 → 解包 → sqlite3 包 → 页面读数（侧栏出现「0 条活动 · v1.1.0」说明接口走通了 Python）
- 写操作会把 SQLite 存回 IndexedDB（`webbridge.dump_db` → base64）

**未验证 / 未完成**
- 首次加载在慢网络下要 30–60 秒（Pyodide 体积决定）
- 还没做整页回归：导入 → 详情 → 教练 → 路书 的逐页点测
- 账号系统在纯前端版里是关闭的（数据只在本机浏览器）
- 依赖服务器的功能不可用：数据源同步、Strava OAuth、LLM 教练（会报错，但页面还在）
- GitHub Pages 在国内访问速度和稳定性一般

## 本机怎么试

```bash
python tools/build_web_bundle.py
cd web && python -m http.server 8808
# 打开 http://127.0.0.1:8808/?static=1
```

## 注意

- 换浏览器 / 清站点数据 / 无痕模式 = 数据没了。要备份的话目前只能导出：设置页还没有「导出数据库」按钮，后续补。
- 改 `pace/` 下任何 .py 之后要重新跑 `tools/build_web_bundle.py`（Pages 工作流已自动做）。
