/* 导出任务面板：进度轮询、终止、删除历史、重新导出、打开产物。
 *
 * 数据源是 /export-tasks/status（服务进程内的任务镜像，不解析快照文件）。
 * 刷新策略与转换任务面板不同：导出任务生命周期短、行数少、状态迁移频繁
 * （排队→编译→完成），所以这里做**按行增量更新**而不是整表重建：
 *   - 行还在原分区：只改进度条宽度与阶段文本（CSS transition 让进度平滑）；
 *   - 状态跨分区（进行中→已完成/已终止）：把那一行重建到目标分区；
 *   - 数据里消失的 id：移除行。
 * 这样每 2 秒一轮轮询不会打断点击、悬停和文本选择。
 */
(function () {
  'use strict';

  const root = document.getElementById('export-tasks');
  if (!root) return;

  const LISTS = {
    active: document.getElementById('export-list-active'),
    done: document.getElementById('export-list-done'),
    stopped: document.getElementById('export-list-stopped'),
  };
  const EMPTIES = {
    active: document.getElementById('export-empty-active'),
    done: document.getElementById('export-empty-done'),
    stopped: document.getElementById('export-empty-stopped'),
  };
  const POLL_MS = Math.max(1000, Number(root.dataset.pollMs) || 2000);
  const embedded = window.parent !== window;   // Obsidian 插件 / 桌面壳 iframe
  const tasks = new Map();                     // id -> 最近一次任务数据
  let detailTaskId = '';

  const FMT_LABEL = {pdf: 'PDF', docx: 'Word', tex: 'LaTeX 源码', zip: 'LaTeX 源码包'};
  const MODE_LABEL = {
    exam: '试卷', exam_std: '标准试卷', note: '笔记', lecture: '讲解',
    slides: '横版课件', practice: '双栏刷题', list: '清单', handout: '讲义',
  };
  const SOLUTION_LABEL = {none: '不带解析', inline: '题后附解析', separate: '解析另起页'};
  const SCOPE_LABEL = {selected: '已勾选题目', filtered: '当前筛选结果', all: '全部题目'};
  const NON_ACTIVE = new Set(['done', 'cancelled', 'failed', 'interrupted']);

  function esc(text) { return String(text == null ? '' : text); }
  function fmtTime(ts) {
    const value = Number(ts);
    if (!Number.isFinite(value) || value <= 0) return '';
    return new Date(value * 1000).toLocaleString('zh-CN', {hour12: false});
  }
  function truncate(text, limit) {
    const s = esc(text);
    return s.length > limit ? s.slice(0, limit) + '…' : s;
  }
  function groupOf(status) {
    if (status === 'queued' || status === 'running') return 'active';
    if (status === 'done') return 'done';
    return 'stopped';
  }
  function statusLabel(status) {
    if (status === 'queued') return '排队中';
    if (status === 'running') return '进行中';
    if (status === 'done') return '已完成';
    if (status === 'cancelled') return '已终止';
    if (status === 'interrupted') return '已中断';
    return '失败';
  }
  function statusClass(status) {
    if (status === 'done') return 'bp-st bp-ready';
    if (status === 'queued' || status === 'running') return 'bp-st';
    return 'bp-st bp-err-st';
  }
  function fmtLabel(fmt) { return FMT_LABEL[fmt] || fmt || ''; }
  // 服务端的进度上限是 88，完成才是 100；失败/终止时进度条停在最后阶段，
  // 用户一眼能看出卡在哪一步（比如 50% 编译中）。
  function percentOf(task) {
    const value = Number(task.percent);
    return Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : 0;
  }

  // ------------------------------------------------------------------
  // 行渲染
  // ------------------------------------------------------------------

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function buildActions(task, includeDetail) {
    const act = el('span', 'bo-act');
    const id = task.id;
    const artifact = task.artifact || {};
    if (task.status === 'queued' || task.status === 'running') {
      act.appendChild(actionButton('终止', 'export-cancel', 'btn-danger-text'));
    } else {
      if (artifact.view_url && /\.pdf$/i.test(artifact.name || artifact.path || '')) {
        act.appendChild(actionButton('打开 PDF', 'export-view'));
      }
      if (artifact.url) {
        act.appendChild(actionButton(
          /\.pdf$/i.test(artifact.name || '') ? '下载' : '下载产物', 'export-download'));
      }
      if (artifact.path) {
        act.appendChild(actionButton('打开文件夹', 'export-reveal'));
      }
      if (NON_ACTIVE.has(task.status)) {
        act.appendChild(actionButton('重新导出', 'export-retry'));
        act.appendChild(actionButton('修改配置', 'export-modify'));
      }
      act.appendChild(actionButton('删除', 'export-delete', 'btn-danger-text'));
    }
    if (includeDetail) act.appendChild(actionButton('详情', 'export-detail-open'));
    return act;
  }

  function actionButton(text, cls, extra) {
    const btn = el('button', 'btn btn-sm ' + cls + (extra ? ' ' + extra : ''), text);
    btn.type = 'button';
    return btn;
  }

  function buildChips(task) {
    const chips = el('span', 'bo-chips');
    const qa = Number(task.question_count);
    if (Number.isFinite(qa) && qa > 0) chips.appendChild(el('span', 'bp-st', qa + ' 题'));
    const fmt = fmtLabel(task.fmt);
    if (fmt) chips.appendChild(el('span', 'bp-st', fmt));
    if (task.error) {
      const error = el('span', 'muted bo-library-error', truncate(task.error, 90));
      error.title = esc(task.error);
      chips.appendChild(error);
    }
    if (task.status === 'done' && task.artifact?.name) {
      chips.appendChild(el('span', 'muted bo-library-output',
                           truncate(task.artifact.name, 60)));
    }
    return chips;
  }

  function buildRow(task) {
    const row = el('div', 'bo-row export-row');
    row.dataset.taskId = task.id;

    const name = el('button', 'export-name');
    name.type = 'button';
    name.appendChild(el('span', 'export-title', esc(task.title) || '试卷'));
    const source = [task.bank ? '来源：' + esc(task.bank) : '',
                    MODE_LABEL[task.payload?.mode] || ''].filter(Boolean).join(' · ');
    name.appendChild(el('span', 'muted export-source', source));
    row.appendChild(name);

    const prog = el('span', 'bo-prog');
    const bar = el('span', 'bo-bar');
    const fill = el('span', 'bo-bar-fill');
    fill.style.width = percentOf(task) + '%';
    bar.appendChild(fill);
    prog.appendChild(bar);
    prog.appendChild(el('span', 'bo-num', esc(task.stage_text) || statusLabel(task.status)));
    row.appendChild(prog);

    row.appendChild(buildChips(task));

    const act = buildActions(task, true);
    act.dataset.sig = actionsSignature(task);
    row.appendChild(act);
    row.dataset.sig = rowSignature(task);
    return row;
  }

  // 只在「会改变按钮构成」的字段变化时才重建操作区，避免每 2 秒重建按钮
  // 打断悬停/点击（进度与文本变化不触发重建）。
  function actionsSignature(task) {
    const artifact = task.artifact || {};
    return [task.status, artifact.url || '', artifact.view_url || '',
            artifact.path || ''].join('|');
  }
  function rowSignature(task) {
    return [task.status, task.stage_text || '', percentOf(task),
            Number(task.question_count) || 0, task.error || '',
            task.artifact?.name || '', task.title || '', task.fmt || '',
            task.bank || '', task.payload?.mode || ''].join('|');
  }

  function updateRow(row, task) {
    const signature = rowSignature(task);
    if (row.dataset.sig === signature) return;   // 完全没变，整行跳过
    row.dataset.sig = signature;
    const fill = row.querySelector('.bo-bar-fill');
    if (fill) fill.style.width = percentOf(task) + '%';
    const num = row.querySelector('.bo-num');
    if (num) num.textContent = esc(task.stage_text) || statusLabel(task.status);
    const title = row.querySelector('.export-title');
    if (title) title.textContent = esc(task.title) || '试卷';
    const source = row.querySelector('.export-source');
    if (source) {
      source.textContent = [task.bank ? '来源：' + esc(task.bank) : '',
                            MODE_LABEL[task.payload?.mode] || '']
        .filter(Boolean).join(' · ');
    }
    const oldChips = row.querySelector('.bo-chips');
    if (oldChips) oldChips.replaceWith(buildChips(task));
    const oldAct = row.querySelector('.bo-act');
    const actSig = actionsSignature(task);
    if (oldAct && oldAct.dataset.sig !== actSig) {
      const fresh = buildActions(task, true);
      fresh.dataset.sig = actSig;
      oldAct.replaceWith(fresh);
    }
  }

  // 依据数据顺序重排节点；只在顺序确实不同时移动 DOM（避免无谓的重排
  // 打断 hover 与点击）。从末尾往前建立锚链，与 append 方向相反。
  function orderList(listEl, rows) {
    let anchor = null;
    for (let i = rows.length - 1; i >= 0; i--) {
      const row = rows[i];
      if (row.parentNode !== listEl || row.nextElementSibling !== anchor) {
        listEl.insertBefore(row, anchor);
      }
      anchor = row;
    }
  }

  function applyTasks(list) {
    const seen = new Set();
    const groups = {active: [], done: [], stopped: []};
    // 先建 id -> 行 的索引：避免每任务一次全文档查询，也不依赖
    // CSS.escape（jsdom 等环境里没有这个函数，会让整轮渲染静默失败）。
    const rowsById = new Map();
    document.querySelectorAll('.export-row').forEach(row => {
      rowsById.set(row.dataset.taskId, row);
    });
    for (const task of list || []) {
      if (!task || !task.id) continue;
      seen.add(task.id);
      tasks.set(task.id, task);
      const group = groupOf(task.status);
      let row = rowsById.get(task.id) || null;
      if (row && groupOf(row.dataset.group || '') !== group) {
        // 状态跨分区：直接重建行（进度着色/按钮集合都不同）。
        row.remove();
        row = null;
      }
      if (!row) {
        row = buildRow(task);
        row.dataset.group = group;
      } else {
        updateRow(row, task);
      }
      groups[group].push(row);
    }
    // 数据里已经没有的任务（被删除或过期）→ 撤行
    document.querySelectorAll('.export-row').forEach(row => {
      if (!seen.has(row.dataset.taskId)) {
        row.remove();
        tasks.delete(row.dataset.taskId);
      }
    });
    for (const [name, rows] of Object.entries(groups)) {
      orderList(LISTS[name], rows);
      const empty = EMPTIES[name];
      if (empty) empty.hidden = rows.length > 0;
    }
    const emptyState = document.getElementById('export-empty-state');
    if (emptyState) emptyState.hidden = seen.size > 0;
  }

  // ------------------------------------------------------------------
  // 操作
  // ------------------------------------------------------------------

  async function post(url) {
    const res = await fetch(url, {method: 'POST'});
    let data = null;
    try { data = await res.json(); } catch (error) { data = null; }
    if (!res.ok || !data || !data.ok) {
      throw new Error((data && data.error) || ('请求失败（HTTP ' + res.status + '）'));
    }
    return data;
  }

  function downloadArtifact(task) {
    const artifact = task.artifact || {};
    if (!artifact.url) return;
    if (embedded) {
      // Obsidian 插件 / 桌面壳由外层文档落盘（同 base.html 的下载桥协议）。
      window.parent.postMessage({
        source: 'quizforge', type: 'download',
        url: new URL(artifact.url, location.href).href,
        filename: artifact.name || '',
      }, '*');
      return;
    }
    const anchor = document.createElement('a');
    anchor.href = artifact.url;
    anchor.download = artifact.name || '';
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  }

  const viewer = document.getElementById('export-viewer');
  const viewerFrame = document.getElementById('export-viewer-frame');
  const viewerTitle = document.getElementById('export-viewer-title');
  const viewerDownload = document.getElementById('export-viewer-download');

  function openViewer(task) {
    const artifact = task.artifact || {};
    if (!artifact.view_url) return;
    viewerTitle.textContent = artifact.name || '产物预览';
    viewerDownload.href = artifact.url || '#';
    viewerDownload.setAttribute('download', artifact.name || '');
    viewerFrame.src = artifact.view_url;
    viewer.showModal();
  }
  function closeViewer() {
    viewer.close();
    viewerFrame.src = 'about:blank';
  }
  viewer?.addEventListener('click', event => {
    if (event.target === viewer) closeViewer();   // 点遮罩关闭
  });
  viewer?.querySelectorAll('[data-export-viewer-close]').forEach(btn =>
    btn.addEventListener('click', closeViewer));

  // 详情面板
  const detail = document.getElementById('export-detail');
  const detailBody = document.getElementById('export-detail-body');
  const detailActions = document.getElementById('export-detail-actions');
  const detailTitle = document.getElementById('export-detail-title');

  function detailRow(dl, key, value) {
    if (!value) return;
    dl.appendChild(el('dt', null, key));
    dl.appendChild(el('dd', null, value));
  }

  function openDetail(task) {
    detailTaskId = task.id;
    detailTitle.textContent = esc(task.title) || '导出任务';
    detailBody.textContent = '';
    const dl = el('dl', 'export-detail-grid');
    detailRow(dl, '状态', statusLabel(task.status)
      + (task.stage_text && task.stage_text !== statusLabel(task.status)
         ? ' · ' + esc(task.stage_text) : ''));
    detailRow(dl, '来源题库', esc(task.bank)
      + (task.bank_path ? '（' + esc(task.bank_path) + '）' : ''));
    const payload = task.payload || {};
    detailRow(dl, '导出格式', fmtLabel(task.fmt));
    detailRow(dl, '版式模式', MODE_LABEL[payload.mode] || esc(payload.mode));
    const scopeText = SCOPE_LABEL[payload.scope] || esc(payload.scope) || '';
    detailRow(dl, '范围', scopeText
      + (payload.scope === 'selected' && payload.pinned_ids?.length
         ? '（固定 ' + payload.pinned_ids.length + ' 道）' : ''));
    detailRow(dl, '解析', SOLUTION_LABEL[payload.solution_mode] || '');
    detailRow(dl, '题量', Number(task.question_count) > 0
      ? task.question_count + ' 题' : '');
    if (payload.keypoints) detailRow(dl, '知识要点', truncate(payload.keypoints, 200));
    const so = payload.std_opts || {};
    if (payload.mode === 'exam_std') {
      detailRow(dl, '科目', esc(so.subject));
      if (so.secret_notice) detailRow(dl, '保密说明', esc(so.secret_notice));
      if (so.exam_notes) detailRow(dl, '卷首说明', truncate(so.exam_notes, 200));
      const points = so.section_points || {};
      const pointText = ['single', 'multi', 'blank', 'solve']
        .filter(key => points[key])
        .map(key => ({single: '单选', multi: '多选', blank: '填空', solve: '解答'}[key]
                     + ' ' + points[key] + ' 分')).join('，');
      if (pointText) detailRow(dl, '分值', pointText);
    }
    if (payload.cjk_font || payload.latin_font) {
      detailRow(dl, '字体', [payload.cjk_font, payload.latin_font]
        .filter(Boolean).join(' / '));
    }
    if (payload.paper_tone && payload.paper_tone !== 'white') {
      detailRow(dl, '纸张底色', '米黄护眼');
    }
    if (payload.wimath_logo) detailRow(dl, 'WIMath 标志', '已启用');
    if (payload.show_source) detailRow(dl, '显示题源', '已启用');
    if (payload.template_id) detailRow(dl, '导出模板', esc(payload.template_id));
    const hf = payload.header_footer || {};
    const hfText = ['header_left', 'header_center', 'header_right',
                    'footer_left', 'footer_center', 'footer_right']
      .filter(key => hf[key]).map(key => hf[key]).join(' | ');
    if (hfText) detailRow(dl, '页眉页脚', truncate(hfText, 200));
    detailRow(dl, '创建时间', fmtTime(task.created_at));
    detailRow(dl, '开始时间', fmtTime(task.started_at));
    detailRow(dl, '结束时间', fmtTime(task.finished_at));
    if (task.artifact?.path) detailRow(dl, '产物文件', esc(task.artifact.path));
    if (task.error) detailRow(dl, '错误', esc(task.error));
    detailBody.appendChild(dl);

    detailActions.textContent = '';
    const actions = buildActions(task, false);
    while (actions.firstChild) detailActions.appendChild(actions.firstChild);
    if (!detailActions.childElementCount) {
      detailActions.appendChild(el('span', 'muted', '这个任务暂时没有可用的操作。'));
    }
    detail.showModal();
  }

  detail?.addEventListener('click', event => {
    if (event.target === detail) detail.close();   // 点遮罩关闭
  });
  detail?.querySelectorAll('[data-export-detail-close]').forEach(btn =>
    btn.addEventListener('click', () => detail.close()));

  // ------------------------------------------------------------------
  // 事件委托：行内按钮 + 点名称开详情
  // ------------------------------------------------------------------

  function handleAction(event) {
    const target = event.target.closest('button');
    if (!target) return;
    const row = target.closest('.export-row');
    const actionRow = row || detailActions;
    const taskId = row ? row.dataset.taskId : detailTaskId;
    const task = tasks.get(taskId);
    if (!task && !target.classList.contains('export-delete')) return;

    if (target.classList.contains('export-detail-open')) {
      openDetail(task);
      event.preventDefault();
      return;
    }
    const scopeAct = (fn) => { event.preventDefault(); void fn(target, task); };

    if (target.classList.contains('export-view')) {
      scopeAct(async () => openViewer(task));
    } else if (target.classList.contains('export-download')) {
      scopeAct(async () => downloadArtifact(task));
    } else if (target.classList.contains('export-reveal')) {
      scopeAct(async (btn) => {
        btn.disabled = true;
        try {
          await post('/export-tasks/' + encodeURIComponent(task.id) + '/reveal');
        } catch (error) {
          alert('打开文件夹失败：' + error.message);
        }
        btn.disabled = false;
      });
    } else if (target.classList.contains('export-cancel')) {
      if (!confirm('终止这条导出？正在编译的进程会被结束，半成品会被清理。')) return;
      scopeAct(async (btn) => {
        btn.disabled = true;
        try {
          await post('/export-tasks/' + encodeURIComponent(task.id) + '/cancel');
        } catch (error) {
          alert('终止失败：' + error.message);
        }
        btn.disabled = false;
        refresh();
      });
    } else if (target.classList.contains('export-delete')) {
      if (!confirm('删除这条任务历史？已经生成的产物文件不受影响，'
                   + '仍可在导出目录里找到。')) return;
      scopeAct(async (btn) => {
        btn.disabled = true;
        try {
          await post('/export-tasks/' + encodeURIComponent(taskId) + '/delete');
          row?.remove();
          detail.close();
          refresh();
        } catch (error) {
          alert('删除失败：' + error.message);
          btn.disabled = false;
        }
      });
    } else if (target.classList.contains('export-retry')) {
      scopeAct(async (btn) => {
        btn.disabled = true;
        try {
          await post('/export-tasks/' + encodeURIComponent(task.id) + '/retry');
        } catch (error) {
          alert('重新导出失败：' + error.message);
          btn.disabled = false;
        }
        refresh();
      });
    } else if (target.classList.contains('export-modify')) {
      // 回题库页并预填这条任务的配置（index.html 读取 ?export_task=<id>）。
      event.preventDefault();
      location.href = '/?export_task=' + encodeURIComponent(task.id);
    }
  }

  for (const listEl of Object.values(LISTS)) {
    listEl?.addEventListener('click', event => {
      const name = event.target.closest('.export-name');
      if (name) {
        const row = name.closest('.export-row');
        const task = row && tasks.get(row.dataset.taskId);
        if (task) openDetail(task);
        return;
      }
      handleAction(event);
    });
  }
  detailActions?.addEventListener('click', event => {
    const task = tasks.get(detailTaskId);
    if (!task) return;
    if (event.target.closest('.export-detail-open')) return;
    handleAction(event);
    if (!detail.open) return;
    // 详情里的操作改变了状态：把内容刷新成最新一份。
    const fresh = tasks.get(detailTaskId);
    if (fresh) openDetail(fresh);
  });

  // ------------------------------------------------------------------
  // 轮询与首屏
  // ------------------------------------------------------------------

  async function refresh() {
    let data;
    try {
      const res = await fetch('/export-tasks/status');
      if (!res.ok) return;
      data = await res.json();
    } catch (error) {
      return;   // 后端重启中之类，下一轮再试
    }
    if (!data || !data.ok) return;
    applyTasks(data.tasks || []);
  }

  function bootstrap() {
    const holder = document.getElementById('export-tasks-data');
    if (holder) {
      try {
        applyTasks(JSON.parse(holder.textContent || '[]'));
      } catch (error) { /* 数据破损时交给首轮轮询 */ }
    }
  }

  bootstrap();
  setInterval(refresh, POLL_MS);
})();
