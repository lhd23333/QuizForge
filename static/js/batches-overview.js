/* 转换任务总面板：轮询各批进度、中止、删除。
 * 从服务器版移植，去掉 CSRF 令牌（软件版无鉴权、只监听 127.0.0.1）。
 */
(function () {
  'use strict';

  const rootEl = document.getElementById('batches-overview');
  if (!rootEl) return;
  const listEl = document.getElementById('bo-list');
  const libraryListEl = document.getElementById('bo-library-list');
  const sourceListEl = document.getElementById('bo-source-list');
  const showAll = rootEl.dataset.showAll === '1';
  const statusUrl = '/batches/status' + (showAll ? '?all=1' : '');

  function rowOf(bid) { return listEl?.querySelector('.bo-row[data-bid="' + bid + '"]'); }
  function libraryRowOf(taskId) {
    return libraryListEl?.querySelector('.bo-library-row[data-task-id="'
      + taskId + '"]');
  }
  function sourceRowOf(taskId) {
    return sourceListEl?.querySelector('.bo-source-row[data-task-id="'
      + taskId + '"]');
  }
  function setText(el, s) { if (el) el.textContent = s; }

  // 来源任务状态与文本/进度映射（服务端 _SOURCE_TASK_STATUS 的镜像）。
  // 单文件任务的"进度"就是阶段；不能照抄批次/资料库"未知 id 就整页 reload"
  // 的刷新模式——监控会持续新增任务，那样每导入一个文件整页就闪一次。
  const SOURCE_STATUS = {
    queued: {text: '排队中', pct: 10},
    converting: {text: '识别中', pct: 40},
    validating: {text: '正在提交', pct: 80},
    committed: {text: '已完成', pct: 100},
    failed: {text: '失败', pct: 0},
    interrupted: {text: '已中断', pct: 0},
    dismissed: {text: '已忽略', pct: 0},
  };

  function buildSourceRow(task) {
    const row = document.createElement('div');
    row.className = 'bo-row bo-source-row';
    row.dataset.taskId = task.task_id;
    const name = document.createElement('span');
    name.className = 'bo-name';
    const prog = document.createElement('span');
    prog.className = 'bo-prog';
    const bar = document.createElement('span');
    bar.className = 'bo-bar';
    const fill = document.createElement('span');
    fill.className = 'bo-bar-fill';
    bar.appendChild(fill);
    const num = document.createElement('span');
    num.className = 'bo-num';
    prog.append(bar, num);
    const chips = document.createElement('span');
    chips.className = 'bo-chips';
    const actions = document.createElement('span');
    actions.className = 'bo-act';
    row.append(name, prog, chips, actions);
    return row;
  }

  function updateSourceRow(row, task) {
    row.classList.toggle('bo-finished', task.status === 'committed');
    setText(row.querySelector('.bo-name'), task.label
      + (task.source_name ? ' · ' + task.source_name : ''));
    const state = SOURCE_STATUS[task.status] || {text: task.status, pct: 0};
    setText(row.querySelector('.bo-num'), task.status_text || state.text);
    const fill = row.querySelector('.bo-bar-fill');
    if (fill) fill.style.width = (task.percent ?? state.pct) + '%';
    const chips = row.querySelector('.bo-chips');
    if (chips) {
      chips.textContent = '';
      if (task.warnings?.length) {
        const warn = document.createElement('span');
        warn.className = 'bp-st';
        warn.title = task.warnings.join('；');
        warn.textContent = '需人工校对';
        chips.appendChild(warn);
      }
      if (task.error) {
        const detail = document.createElement('span');
        detail.className = 'muted bo-library-error';
        detail.textContent = task.error;
        chips.appendChild(detail);
      }
      if (task.output) {
        const output = document.createElement('span');
        output.className = 'muted bo-library-output';
        output.textContent = task.output;
        chips.appendChild(output);
      }
    }
    const actions = row.querySelector('.bo-act');
    if (actions) {
      actions.textContent = '';
      if (task.retryable) {
        const retry = document.createElement('button');
        retry.type = 'button';
        retry.className = 'btn btn-sm bo-source-retry';
        retry.textContent = '重试';
        actions.appendChild(retry);
      }
      if (task.dismissible) {
        const dismiss = document.createElement('button');
        dismiss.type = 'button';
        dismiss.className = 'btn btn-sm bo-source-dismiss';
        dismiss.textContent = '忽略';
        actions.appendChild(dismiss);
      }
      if (task.removable) {
        const remove = document.createElement('button');
        remove.type = 'button';
        remove.className = 'btn btn-sm bo-source-remove';
        remove.textContent = '移除记录';
        actions.appendChild(remove);
      }
    }
  }

  function refreshSourceRows(tasks) {
    if (!sourceListEl) return;
    const seen = new Set();
    const newRows = [];
    for (const task of tasks || []) {
      seen.add(task.task_id);
      let row = sourceRowOf(task.task_id);
      if (!row) {
        row = buildSourceRow(task);
        newRows.push(row);
      }
      updateSourceRow(row, task);
    }
    // 服务端按时间倒序（新的在前）；从最旧的开始 prepend，最新的落在最上面。
    for (const row of newRows.reverse()) sourceListEl.prepend(row);
    sourceListEl.querySelectorAll('.bo-source-row').forEach(row => {
      if (!seen.has(row.dataset.taskId)) row.remove();
    });
    const empty = document.getElementById('bo-source-empty');
    if (empty) empty.hidden = !!sourceListEl.querySelector('.bo-source-row');
    // 页面级空态块（"没有待处理的任务"）是服务端渲染的；监控在停留期间产生
    // 新任务时它不会自己消失，这里补一刀，避免空态与任务行同屏。
    if (newRows.length) document.getElementById('bo-empty-state')?.remove();
  }

  // ---------- 中止整批 ----------
  listEl?.addEventListener('click', async ev => {
    const btn = ev.target.closest('.bo-cancel');
    if (!btn) return;
    const row = btn.closest('.bo-row');
    if (!confirm('中止这一批？已经在识别的几组停不下来，但结果不会入库。')) return;
    btn.disabled = true;
    try {
      await fetch('/batch-convert/' + row.dataset.bid + '/cancel', {method: 'POST'});
    } catch (e) { /* 网络抖动：下一轮 refresh 会纠正显示 */ }
    btn.disabled = false;
    refresh();
  });

  // ---------- 删除一批 ----------
  listEl?.addEventListener('click', async ev => {
    const btn = ev.target.closest('.bo-delete');
    if (!btn) return;
    const row = btn.closest('.bo-row');
    if (!confirm('从列表里删掉这一批？已入库的题目不受影响，未审核的结果会丢弃。')) return;
    btn.disabled = true;
    try {
      const res = await fetch('/batch/' + row.dataset.bid + '/delete', {method: 'POST'});
      const data = await res.json();
      if (!data.ok) { alert(data.error || '删除失败'); btn.disabled = false; return; }
      row.remove();
      // 删空了就整页刷新，让空状态那段文案出来
      if (!listEl.querySelectorAll('.bo-row').length) location.reload();
    } catch (e) {
      alert('请求出错：' + e.message);
      btn.disabled = false;
    }
  });

  // 资料库任务没有标准批次的中止/审核阶段，只提供同一重试入口。
  libraryListEl?.addEventListener('click', async ev => {
    const btn = ev.target.closest('.bo-library-retry');
    if (!btn) return;
    const row = btn.closest('.bo-library-row');
    if (!row) return;
    btn.disabled = true;
    try {
      const response = await fetch('/api/library/task/' + encodeURIComponent(row.dataset.taskId)
        + '/retry', {method: 'POST'});
      const data = await response.json().catch(() => ({}));
      if (!response.ok || !data.ok) throw new Error(data.error || '重试失败');
      refresh();
    } catch (e) {
      alert('重试失败：' + e.message);
      btn.disabled = false;
    }
  });

  // 实时监控任务：重试（失败/中断/已忽略）、忽略（失败/中断）与移除记录（已完成）。
  sourceListEl?.addEventListener('click', async ev => {
    const retryBtn = ev.target.closest('.bo-source-retry');
    const dismissBtn = ev.target.closest('.bo-source-dismiss');
    const removeBtn = ev.target.closest('.bo-source-remove');
    if (!retryBtn && !dismissBtn && !removeBtn) return;
    const row = (retryBtn || dismissBtn || removeBtn).closest('.bo-source-row');
    if (!row) return;
    const taskId = row.dataset.taskId;
    if (retryBtn) {
      retryBtn.disabled = true;
      try {
        const res = await fetch('/api/source-ingest/retry/'
          + encodeURIComponent(taskId), {method: 'POST'});
        const data = await res.json().catch(() => ({}));
        if (!res.ok || !data.ok) throw new Error(data.error || '重试失败');
      } catch (e) {
        alert('重试失败：' + e.message);
      }
      retryBtn.disabled = false;
      refresh();
      return;
    }
    if (dismissBtn) {
      if (!confirm('忽略这条记录？它会从列表与红心计数里移出，之后不会自动重转'
        + '这份文件（保留去重指纹，避免重复消耗识别额度）；要恢复处理，可在'
        + '「连已完成一起看」视图里点重试。')) {
        return;
      }
      dismissBtn.disabled = true;
      try {
        const res = await fetch('/api/source-ingest/task/'
          + encodeURIComponent(taskId) + '/dismiss', {method: 'POST'});
        const data = await res.json().catch(() => ({}));
        if (!res.ok || !data.ok) throw new Error(data.error || '忽略失败');
        // 交给下一轮 refresh 撤行：默认视图服务端已过滤 dismissed，重启一行
        // 反而会对不上；show_all 视图里它会原地变成"已忽略"（可重试恢复）。
        refresh();
      } catch (e) {
        alert('忽略失败：' + e.message);
        dismissBtn.disabled = false;
      }
      return;
    }
    if (!confirm('仅从列表移除这条已完成记录，已生成的产物和源文件都不受影响。')) {
      return;
    }
    removeBtn.disabled = true;
    try {
      const res = await fetch('/api/source-ingest/task/'
        + encodeURIComponent(taskId) + '/delete', {method: 'POST'});
      const data = await res.json().catch(() => ({}));
      if (!res.ok || !data.ok) throw new Error(data.error || '移除失败');
      row.remove();
      const empty = document.getElementById('bo-source-empty');
      if (empty) empty.hidden = !!sourceListEl.querySelector('.bo-source-row');
    } catch (e) {
      alert('移除失败：' + e.message);
      removeBtn.disabled = false;
    }
  });

  function refreshLibraryRows(tasks) {
    if (!libraryListEl) {
      if (tasks?.length) location.reload();
      return;
    }
    const seen = new Set();
    for (const task of tasks || []) {
      seen.add(task.task_id);
      const row = libraryRowOf(task.task_id);
      if (!row) { location.reload(); return; }
      row.classList.toggle('bo-finished', !!task.finished);
      const fill = row.querySelector('.bo-bar-fill');
      if (fill) fill.style.width = task.finished ? '100%' : '0%';
      setText(row.querySelector('.bo-num'), task.finished ? '1/1'
        : task.busy ? '处理中' : '已停止');
      const chips = row.querySelector('.bo-chips');
      if (chips) {
        chips.innerHTML = '';
        const state = document.createElement('span');
        state.className = task.status === 'done' ? 'bp-st bp-ready'
          : ['error', 'interrupted'].includes(task.status) ? 'bp-st bp-err-st' : 'bp-st';
        state.textContent = task.status === 'queued' ? '排队中'
          : task.status === 'done' ? '已完成'
            : task.status === 'interrupted' ? '已中断'
              : task.busy ? '处理中' : '失败';
        chips.appendChild(state);
        if (task.error) {
          const detail = document.createElement('span');
          detail.className = 'muted bo-library-error'; detail.textContent = task.error;
          chips.appendChild(detail);
        }
        if (task.outputs?.length) {
          const output = document.createElement('span');
          output.className = 'muted bo-library-output';
          output.textContent = task.outputs.join('、'); chips.appendChild(output);
        }
      }
      const actions = row.querySelector('.bo-act');
      if (actions) {
        const retry = actions.querySelector('.bo-library-retry');
        const shouldRetry = ['error', 'interrupted'].includes(task.status);
        if (shouldRetry && !retry) {
          const button = document.createElement('button');
          button.type = 'button'; button.className = 'btn btn-sm bo-library-retry';
          button.textContent = '重试'; actions.appendChild(button);
        } else if (!shouldRetry && retry) retry.remove();
      }
    }
    // 默认视图中完成项会消失；完整视图中也可能由其他窗口清除任务。
    // 两种模式都按接口结果撤掉旧行，避免 show_all 页面长期显示幽灵任务。
    libraryListEl.querySelectorAll('.bo-library-row').forEach(row => {
      if (!seen.has(row.dataset.taskId)) row.remove();
    });
    if (!libraryListEl.querySelector('.bo-library-row')) location.reload();
  }

  // ---------- 轮询刷新 ----------
  async function refresh() {
    let data;
    try {
      const res = await fetch(statusUrl);
      data = await res.json();
    } catch (e) { return; }   // 后端重启中之类，下一轮再试
    if (!data.ok) return;

    const batches = data.batches || [];
    const libraryTasks = data.library_tasks || [];
    const sourceTasks = data.source_tasks || [];
    if (!listEl && batches.length) { location.reload(); return; }

    const seen = new Set();
    for (const b of batches) {
      seen.add(b.batch_id);
      const row = rowOf(b.batch_id);
      if (!row) { location.reload(); return; }   // 新来了一批，重排序号

      setText(row.querySelector('.bo-num'), b.done + '/' + b.total);
      const fill = row.querySelector('.bo-bar-fill');
      if (fill) fill.style.width = (b.total ? 100 * b.done / b.total : 0) + '%';

      // 状态小标签整块重建：种类会随进度增减，逐个 toggle 更啰嗦
      const chips = row.querySelector('.bo-chips');
      if (chips) {
        chips.innerHTML = '';
        const add = (text, cls, iconName) => {
          const s = document.createElement('span');
          s.className = cls;
          if (iconName && window.QFIcon) {
            const markup = window.QFIcon(iconName);
            if (markup) s.insertAdjacentHTML('beforeend', markup);
          }
          s.appendChild(document.createTextNode(text));
          chips.appendChild(s);
        };
        if (b.converting) add('转换中 ' + b.converting, 'bp-st', 'refresh-cw');
        if (b.pending) add('等待 ' + b.pending, 'bp-st', 'clock');
        if (b.ready) add('待审核 ' + b.ready, 'bp-st bp-ready', 'check-circle');
        if (b.awaiting_block_review)
          add('待拆题审核 ' + b.awaiting_block_review, 'bp-st bp-ready', 'scissors');
        if (b.errors) add('错误 ' + b.errors, 'bp-st bp-err-st', 'alert-triangle');
        if (b.reviewed) add('已处理 ' + b.reviewed, 'muted');
        if (b.cancelled) add('已中止', 'muted');
      }

      row.classList.toggle('bo-finished', !!b.finished);
      const cancelBtn = row.querySelector('.bo-cancel');
      if (cancelBtn) cancelBtn.hidden = !!b.finished;
      const delBtn = row.querySelector('.bo-delete');
      if (delBtn) delBtn.hidden = !!b.busy;
    }

    // 默认视图中已处理项会消失；完整视图也可能由其他窗口删除批次。
    if (listEl) {
      listEl.querySelectorAll('.bo-row').forEach(row => {
        if (!seen.has(row.dataset.bid)) row.remove();
      });
      if (!listEl.querySelectorAll('.bo-row').length) {
        location.reload();
        return;
      }
    }
    refreshLibraryRows(libraryTasks);
    refreshSourceRows(sourceTasks);
  }

  setInterval(refresh, 5000);
})();
