/* 应用入口：路由、导航高亮、全局状态、导入事件。 */
(function () {
  const view = document.getElementById('view');
  const pageTitle = document.getElementById('pageTitle');

  const ROUTES = [
    { pattern: /^\/?$/, key: 'dashboard', title: '仪表盘', load: (r, p) => PageDashboard.load(r, p) },
    { pattern: /^\/coach\/?$/, key: 'coach', title: '教练评估', load: (r, p) => PageCoach.load(r, p) },
    { pattern: /^\/activities\/?$/, key: 'activities', title: '运动数据', load: (r, p) => PageActivities.load(r, p) },
    { pattern: /^\/activity\/(\d+)\/?$/, key: 'activity', nav: 'activities', title: '活动详情', load: (r, p) => PageDetail.load(r, p) },
    { pattern: /^\/routebook\/?$/, key: 'routebook', title: '路书', load: (r, p) => PageRoutebook.load(r, p) },
    { pattern: /^\/routebook\/(\d+)\/?$/, key: 'routebook', nav: 'routebook', title: '路书详情', load: (r, p) => PageRoutebook.load(r, p) },
    { pattern: /^\/segments\/?$/, key: 'segments', title: '分段成绩', load: (r, p) => PageSegments.load(r, p) },
    { pattern: /^\/segment\/(\d+)\/?$/, key: 'segments', title: '分段详情', load: (r, p) => PageSegmentDetail.load(r, p) },
    { pattern: /^\/compare\/?$/, key: 'compare', title: '对比', load: (r, p) => PageCompare.load(r, p) },
    { pattern: /^\/devices\/?$/, key: 'devices', title: '数据源与设备', load: (r, p) => PageDevices.load(r, p) },
    { pattern: /^\/ble\/?$/, key: 'devices', title: '蓝牙采集', load: (r, p) => PageBle.load(r, p) },
    { pattern: /^\/settings\/?$/, key: 'settings', title: '设置', load: (r, p) => PageSettings.load(r, p) },
  ];

  const SPORT_TITLES = { all: '全部', run: '跑步', ride: '骑行', swim: '游泳' };
  // 这个开关只在「会被它过滤」的页面出现，其他页面藏起来，免得点了没反应
  const SPORT_PAGES = new Set(['dashboard', 'activities']);

  // 底图预设：高德的瓦片在国内最快，但它用 GCJ-02 坐标，必须做偏移校正
  const MAP_PROVIDERS = {
    amap: {
      label: '高德',
      url: 'https://webrd01.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}',
      gcj: true,
    },
    osm: { label: 'OSM', url: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png', gcj: false },
    offline: { label: '离线', url: '', gcj: false },
  };

  function resolveMapConfig(settings) {
    const s = settings || {};
    const providerKey = s.map_provider || 'amap';
    const custom = String(s.tile_url || '').trim();
    // 只有「自定义」才用 tile_url：老版本默认写进过 OSM 地址，
    // 会让选了高德的人实际去抓 OSM（国内经常拉不到，地图一片空白）。
    if (providerKey === 'custom' && custom) {
      return { label: '自定义', url: custom,
               gcj: /autonavi|amap|qq\.com|tencent/i.test(custom), provider: 'custom' };
    }
    const provider = MAP_PROVIDERS[providerKey] || MAP_PROVIDERS.amap;
    return { ...provider, provider: providerKey };
  }

  // 保存每个页面的筛选状态（切换 sport 时保留）
  const sportState = { value: 'all' };

  const App = {
    settings: {},
    version: '',
    meta: null,
  };

  function parseHash() {
    let raw = location.hash.replace(/^#/, '') || '/';
    const qIdx = raw.indexOf('?');
    const path = qIdx >= 0 ? raw.slice(0, qIdx) : raw;
    const search = qIdx >= 0 ? raw.slice(qIdx + 1) : '';
    const params = {};
    search.split('&').forEach(pair => {
      if (!pair) return;
      const [k, v] = pair.split('=');
      params[decodeURIComponent(k)] = decodeURIComponent(v || '');
    });
    for (const route of ROUTES) {
      const m = path.match(route.pattern);
      if (m) {
        if (m[1]) params.id = m[1];
        return { route, params, path };
      }
    }
    return { route: ROUTES[0], params, path };
  }

  function setActiveNav(key) {
    U.$$('.nav a[data-nav]').forEach(a => {
      a.classList.toggle('active', a.dataset.nav === key);
    });
  }

  function setSportNav(sport) {
    U.$$('#navSports a').forEach(a => {
      a.classList.toggle('active', !!sport && a.dataset.sportNav === sport);
    });
  }

  function cleanupPage() {
    if (window.PageDetail) PageDetail.cleanup();
    if (window.PageCompare) PageCompare.cleanup();
    if (window.PageSegmentDetail) PageSegmentDetail.cleanup();
  }

  const Router = {
    current: null,
    async render() {
      const { route, params } = parseHash();
      cleanupPage();
      setActiveNav(route.nav || route.key);
      setSportNav(route.key === 'activities' ? params.sport : null);
      const isListPage = route.key === 'activities' && !/^\/activity\//.test(location.hash.replace(/^#/, '').split('?')[0] || '');
      const sportBox = document.getElementById('sportSwitch');
      if (sportBox) {
        sportBox.style.display = (route.key === 'dashboard' || isListPage) ? '' : 'none';
        sportBox.title = route.key === 'dashboard' ? '按运动类型筛选仪表盘' : '按运动类型筛选活动列表';
      }
      if (route.key === 'activities') {
        sportState.value = params.sport || 'all';
        syncSportSwitch();
      } else if (route.key === 'dashboard') {
        if (window.PageDashboard) sportState.value = PageDashboard.state.sport || 'all';
        syncSportSwitch();
      }
      const sportTitle = route.key === 'activities' && params.sport && params.sport !== 'all'
        ? ` · ${SPORT_TITLES[params.sport] || ''}` : '';
      pageTitle.textContent = route.title + sportTitle;
      Router.current = { route, params };
      window.scrollTo({ top: 0, behavior: 'instant' in window ? 'instant' : 'auto' });
      document.getElementById('sidebar').classList.remove('open');
      try {
        await route.load(view, params);
      } catch (err) {
        console.error(err);
        view.innerHTML = `<div class="card"><div class="empty">
          <h3>页面渲染出错</h3><p>${U.esc(err.message)}</p></div></div>`;
      }
    },
    rerender() { this.render(); },
  };

  function syncSportSwitch() {
    U.$$('#sportSwitch button').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.sport === sportState.value);
    });
  }

  function wireChrome() {
    U.$$('#sportSwitch button').forEach(btn => {
      btn.addEventListener('click', () => {
        const value = btn.dataset.sport;
        sportState.value = value;
        syncSportSwitch();
        const { route } = parseHash();
        if (route.key === 'dashboard') {
          if (window.PageDashboard) PageDashboard.setSport(value);
          Router.render();
        } else {
          // 在别的页面点运动类型 = 直接去对应的运动数据页，不再「点了没反应」
          const target = '#/activities' + (value === 'all' ? '' : `?sport=${value}`);
          if (location.hash === target) Router.render();
          else location.hash = target;
        }
      });
    });

    const menuBtn = document.getElementById('btnMenu');
    if (menuBtn) {
      menuBtn.addEventListener('click', () => document.getElementById('sidebar').classList.toggle('open'));
    }
    document.addEventListener('click', (e) => {
      const sidebar = document.getElementById('sidebar');
      if (!sidebar.classList.contains('open')) return;
      if (!sidebar.contains(e.target) && !e.target.closest('#btnMenu')) sidebar.classList.remove('open');
    });

    window.addEventListener('keydown', (e) => {
      if (e.key === '/' && !/input|textarea|select/i.test(document.activeElement.tagName)) {
        e.preventDefault();
        const search = document.getElementById('actSearch');
        if (search) search.focus();
      }
    });

    document.addEventListener('pace:imported', () => {
      // 导入完成后刷新当前页面与侧栏计数
      setTimeout(() => {
        Router.render();
        refreshSidebar();
      }, 250);
    });
  }

  async function refreshSidebar() {
    try {
      const list = await api.activities({ limit: 1 });
      const count = Number(list.total || 0);
      const el = document.getElementById('sidebarStats');
      if (el) el.textContent = `${count} 条活动 · v${App.version || ''}`;
      const nav = document.getElementById('navActivityCount');
      if (nav) nav.textContent = count;
    } catch (err) {
      const el = document.getElementById('sidebarStats');
      if (el) el.textContent = '无法连接服务';
    }
  }

  async function boot() {
    // 纯前端模式：先把浏览器里的 Python 引擎拉起来
    if (window.PaceWeb && PaceWeb.enabled()) {
      try {
        await PaceWeb.boot();
      } catch (err) {
        console.error(err);
        return;
      }
    }
    UI.registerGlobalHandlers();
    UI.Importer.init();
    Icon.hydrate(document);
    syncSportSwitch();

    if (window.PACE_STATIC) document.body.classList.add('static-mode');
    // 没登录就只显示门禁，别初始化任何页面
    if (window.Auth) {
      const ready = await Auth.boot();
      if (!ready) return;
      Auth.mountChip();
    }

    // 读取设置（主题、瓦片）
    try {
      const meta = await api.meta();
      App.meta = meta;
      App.settings = meta.settings || {};
      App.version = meta.version;
      App.mapConfig = resolveMapConfig(App.settings);
      App.resolveMapConfig = resolveMapConfig;
      if (App.settings.theme) document.documentElement.dataset.theme = App.settings.theme;
      window.PaceApp = App;
    } catch (err) {
      UI.toast('无法连接到本地服务，请检查后端是否在运行', 'err', 6000);
    }

    // 高德 Key 现在只存本地服务；浏览器里旧版留下的那份清掉，免得失效 Key 一直报错
    try {
      if (localStorage.getItem('pace.amap.key')) {
        localStorage.removeItem('pace.amap.key');
      }
    } catch (err) { /* ignore */ }

    await refreshSidebar();
    window.Router = Router;
    window.addEventListener('hashchange', () => Router.render());
    Router.render();
  }

  // 主题切换时重绘图表颜色
  window.addEventListener('pace:theme', () => Router.render());

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
