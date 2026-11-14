/* 内联图标集：stroke-based，统一 24 网格、1.8 线宽。
   通过 data-icon="name" 自动替换，或 Icon.get(name) 取字符串。 */
(function () {
  const P = {
    grid: '<rect x="3" y="3" width="7" height="9" rx="1.6"/><rect x="14" y="3" width="7" height="5" rx="1.6"/><rect x="14" y="12" width="7" height="9" rx="1.6"/><rect x="3" y="16" width="7" height="5" rx="1.6"/>',
    list: '<path d="M8 6h13M8 12h13M8 18h13"/><circle cx="3.6" cy="6" r="1.1" fill="currentColor" stroke="none"/><circle cx="3.6" cy="12" r="1.1" fill="currentColor" stroke="none"/><circle cx="3.6" cy="18" r="1.1" fill="currentColor" stroke="none"/>',
    segment: '<path d="M3 17l5-6 4 4 4-8 5 6"/><path d="M3 20.5h18" opacity=".45"/>',
    compare: '<path d="M4 19V9M10 19V5M16 19v-7M22 19V3" opacity=".9"/><path d="M2 21h20" opacity=".4"/>',
    upload: '<path d="M12 16V4m0 0L7.5 8.5M12 4l4.5 4.5"/><path d="M4 16v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3"/>',
    cloud: '<path d="M7 18a4.5 4.5 0 0 1-.4-9A6 6 0 0 1 18 9.5a4 4 0 0 1-.5 8.5"/><path d="M12 13v6m0-6l-2.5 2.5M12 13l2.5 2.5"/>',
    sliders: '<path d="M4 8h9M17 8h3M4 16h3M11 16h9"/><circle cx="15" cy="8" r="2.2"/><circle cx="9" cy="16" r="2.2"/>',
    menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
    x: '<path d="M6 6l12 12M18 6L6 18"/>',
    search: '<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5"/>',
    run: '<circle cx="14.5" cy="4.6" r="1.9"/><path d="M9 21l2.6-5.2-2.4-3.6 1-4.6 3.6 1.8 3.2 2.6"/><path d="M6 12.2l3.2-2.6"/><path d="M11.6 15.8L15 18l-1 3"/><path d="M14.6 9.3l3.4 1.2"/>',
    ride: '<circle cx="5.5" cy="17" r="3.4"/><circle cx="18.5" cy="17" r="3.4"/><path d="M5.5 17l4-7h5l4 7"/><path d="M9.5 10l2.4-3.4h3.2"/><path d="M8 8.5h4.5"/>',
    swim: '<circle cx="16.5" cy="6.5" r="2"/><path d="M3 15.5l3-3.4 4 2 3.4-3.6 4.6 3"/><path d="M2.5 20c1.6-1.1 3.2-1.1 4.8 0s3.2 1.1 4.8 0 3.2-1.1 4.8 0 3.2 1.1 4.6 0" opacity=".85"/>',
    walk: '<circle cx="13" cy="4.6" r="1.9"/><path d="M10 21l2-5-1.8-3.4 1-4.4 3 2 3 2.6"/><path d="M12 12.6l-2.4 3-3.1.6"/><path d="M12 12.6l2.4 3.4 2.6.6"/>',
    other: '<circle cx="12" cy="12" r="8.2"/><path d="M8.4 12h7.2M12 8.4v7.2"/>',
    clock: '<circle cx="12" cy="12" r="8.4"/><path d="M12 7.5V12l3 1.8"/>',
    route: '<circle cx="5" cy="19" r="2.2"/><circle cx="19" cy="5" r="2.2"/><path d="M6.6 17.4c2.7-1.1 3.1-4.3 5.5-5.7 1.5-.9 3.2-2 4.1-4.1"/>',
    mountain: '<path d="M3 19l5.5-9.5L12 15l2.5-4 6.5 8z"/><path d="M3 19h18" opacity=".5"/>',
    heart: '<path d="M12 20s-7-4.4-7-9.4A4 4 0 0 1 12 8a4 4 0 0 1 7 2.6C19 15.6 12 20 12 20z"/>',
    bolt: '<path d="M13 2L4.5 13.5H11L10 22l8.5-11.5H12z"/>',
    flame: '<path d="M12 22c3.9 0 6.5-2.5 6.5-6 0-4.5-4-6.5-4-10.5 0 0-3 1.5-3 5.5 0 1.8-1 2.5-2 2.5-1.2 0-1.8-1-1.8-2.5C5.6 12.4 5.5 14 5.5 16c0 3.5 2.6 6 6.5 6z"/>',
    trend: '<path d="M3 17l5.5-5.5 3.5 3.5L21 6"/><path d="M15.5 6H21v5.5"/>',
    trophy: '<path d="M7 4h10v5a5 5 0 0 1-10 0z"/><path d="M7 5H4.5v2A3.5 3.5 0 0 0 8 10.5M17 5h2.5v2A3.5 3.5 0 0 1 16 10.5"/><path d="M12 14v3M9 21h6l-.8-3.5H9.8z"/>',
    calendar: '<rect x="3.5" y="5" width="17" height="16" rx="2.5"/><path d="M3.5 10h17M8 3v4M16 3v4"/>',
    chevron: '<path d="M9 5l7 7-7 7"/>',
    back: '<path d="M15 5l-7 7 7 7"/>',
    play: '<path d="M7 4.5l12 7.5L7 19.5z" fill="currentColor" stroke="none"/>',
    pause: '<path d="M8 4.5v15M16 4.5v15" stroke-width="3"/>',
    zoomIn: '<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5M11 8.2v5.6M8.2 11h5.6"/>',
    zoomOut: '<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5M8.2 11h5.6"/>',
    layers: '<path d="M12 3l9 5-9 5-9-5z"/><path d="M3 12.5l9 5 9-5"/><path d="M3 17l9 5 9-5" opacity=".55"/>',
    download: '<path d="M12 4v12m0 0l-4.5-4.5M12 16l4.5-4.5"/><path d="M4 18v2h16v-2"/>',
    trash: '<path d="M4 7h16M9 7V4.5h6V7M6.5 7l1 13h9l1-13"/><path d="M10 11v5M14 11v5"/>',
    pin: '<path d="M12 21s7-6.2 7-11a7 7 0 1 0-14 0c0 4.8 7 11 7 11z"/><circle cx="12" cy="10" r="2.6"/>',
    spark: '<path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1"/>',
    check: '<path d="M4.5 12.5l5 5L20 6.5"/>',
    info: '<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5.5M12 7.6v.4"/>',
    link: '<path d="M10 13.5a4 4 0 0 1 0-5.6l2-2a4 4 0 0 1 5.6 5.6l-1.2 1.2"/><path d="M14 10.5a4 4 0 0 1 0 5.6l-2 2a4 4 0 0 1-5.6-5.6l1.2-1.2"/>',
    settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-1.8-.3 1.6 1.6 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.6 1.6 0 0 0-1-1.5 1.6 1.6 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.6 1.6 0 0 0 .3-1.8 1.6 1.6 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.6 1.6 0 0 0 1.5-1 1.6 1.6 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.6 1.6 0 0 0 1.8.3H9a1.6 1.6 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.6 1.6 0 0 0 1 1.5 1.6 1.6 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0-.3 1.8V9a1.6 1.6 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.6 1.6 0 0 0-1.5 1z"/>'
  };

  function svg(name, extra) {
    const body = P[name] || P.info;
    return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" ` +
      `stroke-linecap="round" stroke-linejoin="round" ${extra || ''} aria-hidden="true">${body}</svg>`;
  }

  function hydrate(root) {
    (root || document).querySelectorAll('[data-icon]').forEach(el => {
      const name = el.getAttribute('data-icon');
      if (el.dataset.iconDone === name) return;
      el.innerHTML = svg(name);
      el.dataset.iconDone = name;
    });
  }

  const FAMILY_ICON = { run: 'run', ride: 'ride', swim: 'swim', walk: 'walk', other: 'other' };

  window.Icon = { get: svg, hydrate, family: (f) => svg(FAMILY_ICON[f] || 'other'), has: (n) => !!P[n] };
  if (document.readyState !== 'loading') hydrate();
  else document.addEventListener('DOMContentLoaded', () => hydrate());
})();
