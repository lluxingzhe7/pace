/* 共享 UI 片段：提示、空状态、骨架屏、导入对话框、确认框。 */
(function () {
  const toastHost = () => document.getElementById('toasts');

  function toast(message, kind, ms) {
    const host = toastHost();
    if (!host) return;
    const node = document.createElement('div');
    node.className = 'toast ' + (kind || '');
    node.innerHTML = message;
    host.appendChild(node);
    const life = ms || (kind === 'err' ? 5200 : 3200);
    setTimeout(() => {
      node.style.transition = 'opacity .25s, transform .25s';
      node.style.opacity = '0';
      node.style.transform = 'translateY(6px)';
      setTimeout(() => node.remove(), 260);
    }, life);
  }

  function empty(icon, title, text, actionHtml) {
    return `<div class="empty">
      ${Icon.get(icon || 'spark')}
      <h3>${U.esc(title || '还没有数据')}</h3>
      <p>${text || ''}</p>
      ${actionHtml || ''}
    </div>`;
  }

  function skeletonCard(height) {
    return `<div class="card card-pad"><div class="skeleton" style="height:${height || 120}px"></div></div>`;
  }

  function spinner(text) {
    return `<div class="empty"><div class="skeleton" style="width:42px;height:42px;border-radius:50%"></div>
      <p class="tiny dim">${U.esc(text || '载入中…')}</p></div>`;
  }

  /** 运动类型 chip */
  function sportChip(sport) {
    const fam = U.family(sport);
    return `<span class="chip ${fam}">${Icon.family(fam)}${U.esc(U.sportLabel(sport))}</span>`;
  }

  function confirmDialog(title, text, okLabel, danger) {
    return new Promise((resolve) => {
      const dlg = document.createElement('dialog');
      dlg.className = 'dialog';
      // 整块内容都要在 <form method="dialog"> 里：按钮落在 form 外面时点它不会提交，
      // 对话框不关闭、Promise 永远不 resolve，表现出来就是「点删除没反应」。
      dlg.innerHTML = `
        <form method="dialog">
          <div class="dialog-head">
            <h2>${U.esc(title)}</h2>
            <button class="icon-btn" value="cancel" aria-label="关闭">${Icon.get('x')}</button>
          </div>
          <div class="dialog-body"><p class="muted">${text}</p></div>
          <div class="dialog-foot">
            <button class="btn" value="cancel" autofocus>取消</button>
            <button class="btn ${danger ? 'btn-danger' : 'btn-primary'}" value="ok">${U.esc(okLabel || '确定')}</button>
          </div>
        </form>`;
      document.body.appendChild(dlg);
      dlg.addEventListener('close', () => {
        const ok = dlg.returnValue === 'ok';
        dlg.remove();
        resolve(ok);
      });
      dlg.showModal();
    });
  }

  /** 导入流程：选择/拖拽 -> 上传 -> 展示结果 -> 通知刷新 */
  const Importer = {
    dialog: null,
    busy: false,
    init() {
      this.dialog = document.getElementById('importDialog');
      const dz = document.getElementById('dropzone');
      const input = document.getElementById('fileInput');
      if (!dz || !input) return;

      dz.addEventListener('click', () => input.click());
      dz.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); }
      });
      input.addEventListener('change', () => {
        if (input.files && input.files.length) this.upload(Array.from(input.files));
        input.value = '';
      });
      ['dragenter', 'dragover'].forEach(ev => dz.addEventListener(ev, (e) => {
        e.preventDefault(); dz.classList.add('over');
      }));
      ['dragleave', 'drop'].forEach(ev => dz.addEventListener(ev, (e) => {
        e.preventDefault(); dz.classList.remove('over');
      }));
      dz.addEventListener('drop', (e) => {
        const files = Array.from(e.dataTransfer.files || []);
        if (files.length) this.upload(files);
      });
      // 页面级拖拽
      window.addEventListener('dragover', (e) => {
        if (!this.dialog.open && e.dataTransfer && e.dataTransfer.types.includes('Files')) {
          e.preventDefault();
        }
      });
      window.addEventListener('drop', (e) => {
        if (this.dialog.open) return;
        const files = Array.from(e.dataTransfer.files || []);
        if (!files.length) return;
        e.preventDefault();
        this.open();
        this.upload(files);
      });
    },

    open() {
      if (!this.dialog) this.init();
      const results = document.getElementById('importResults');
      if (results) results.innerHTML = '';
      const prog = document.getElementById('importProgress');
      if (prog) prog.hidden = true;
      this.dialog.showModal();
    },

    async upload(files) {
      if (this.busy) { toast('正在导入中，请稍候', 'warn'); return; }
      if (!this.dialog || !this.dialog.open) this.open();
      this.busy = true;
      const prog = document.getElementById('importProgress');
      const bar = document.getElementById('importBar');
      const status = document.getElementById('importStatus');
      const resultsEl = document.getElementById('importResults');
      prog.hidden = false;
      resultsEl.innerHTML = '';

      const usable = files.filter(f => f.size < 80 * 1024 * 1024);
      if (usable.length !== files.length) {
        toast('已跳过超过 80MB 的文件', 'warn');
      }

      try {
        const results = await api.importFiles(usable, (i, total, name) => {
          bar.style.width = `${Math.round((i / Math.max(1, total)) * 100)}%`;
          status.textContent = name ? `正在导入 ${i + 1}/${total}：${name}` : `完成 ${total} 个文件`;
        });
        bar.style.width = '100%';
        this.renderResults(results, resultsEl);
        const created = results.reduce((n, r) => n + ((r.imported || []).filter(i => !i.merged).length), 0);
        const merged = results.reduce((n, r) => n + ((r.imported || []).filter(i => i.merged).length), 0);
        const dup = results.reduce((n, r) => n + ((r.duplicates || []).length), 0);
        if (created > 0 || merged > 0) {
          const bits = [];
          if (created) bits.push(`新增 ${created} 条`);
          if (merged) bits.push(`合并 ${merged} 条`);
          if (dup) bits.push(`跳过 ${dup} 条重复`);
          toast(bits.join('，'), 'ok');
          document.dispatchEvent(new CustomEvent('pace:imported'));
        } else if (dup > 0) {
          toast(`这些活动已经在库里了（${dup} 条重复）`, 'warn');
        } else {
          toast('没有导入任何活动，请检查文件格式', 'err');
        }
      } catch (err) {
        toast(U.esc(err.message), 'err');
        status.textContent = '导入失败：' + err.message;
      } finally {
        this.busy = false;
        setTimeout(() => { if (prog) prog.hidden = true; }, 1200);
      }
    },

    renderResults(results, host) {
      const rows = [];
      let importedCount = 0;
      let importedWithHr = 0;
      let importedWithPower = 0;
      results.forEach(r => {
        const base = `<div class="result-row">`;
        if (r.status === 'error') {
          rows.push(`${base}<span class="err">${Icon.get('x')}</span>
            <span class="mono">${U.esc(r.file)}</span>
            <span class="muted">${U.esc(r.reason || '解析失败')}</span></div>`);
          return;
        }
        (r.imported || []).forEach(item => {
          importedCount++;
          if (item.has_hr) importedWithHr++;
          if (item.has_power) importedWithPower++;
          const flags = [
            item.has_hr ? '<span class="chip chip-accent">心率</span>' : '<span class="chip dim-chip">无心率</span>',
            item.has_power ? '<span class="chip chip-accent">功率</span>' : '<span class="chip dim-chip">无功率</span>',
          ].join('');
          rows.push(`${base}<span class="ok">${Icon.get('check')}</span>
            <span>${U.esc(item.title || r.file)}</span>
            <span class="dim mono">${U.km(item.distance_m)} km · ${U.dur(item.moving_seconds)}</span>
            ${item.merged ? `<span class="chip chip-accent" title="同一趟运动的另一份文件，已并进同一条记录">已合并${item.replaced ? '·补齐数据' : ''}</span>` : ''}
            ${flags}
            ${item.segments_matched ? `<span class="chip chip-accent">${item.segments_matched} 个分段</span>` : ''}
            </div>`);
        });
        (r.duplicates || []).forEach(item => {
          rows.push(`${base}<span class="dim">${Icon.get('info')}</span>
            <span>${U.esc(item.title || r.file)}</span>
            <span class="dim">已存在，已跳过</span></div>`);
        });
        (r.errors || []).forEach(err => {
          rows.push(`${base}<span class="err">${Icon.get('x')}</span>
            <span class="mono">${U.esc(r.file)}</span><span class="muted">${U.esc(err)}</span></div>`);
        });
      });
      // 全是 GPX 时最容易踩的坑：文件里没有心率/功率通道，界面却不解释
      if (importedCount && !importedWithHr && !importedWithPower) {
        rows.push(`<p class="tiny" style="color:var(--warn);margin-top:8px;line-height:1.6">
          这次导入的文件里没有心率和功率。GPX 导出只带轨迹与时间，
          要心率/功率请在 iGPSPORT App 里导出 <b>FIT</b>（或用数据线从 Activities 文件夹拷 .fit），再拖进来。
        </p>`);
      }
      host.innerHTML = rows.join('') || '<p class="tiny dim">没有可显示的结果</p>';
    },
  };

  function icon(name) { return Icon.get(name); }

  function registerGlobalHandlers() {
    const side = document.getElementById('btnImportSide');
    const top = document.getElementById('btnImport');
    [side, top].forEach(btn => btn && btn.addEventListener('click', () => Importer.open()));
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') Importer.dialog && Importer.dialog.open && Importer.dialog.close();
    });
  }

  window.UI = { toast, empty, skeletonCard, spinner, sportChip, confirmDialog, Importer, icon, registerGlobalHandlers };
})();
