/* 转换任务总面板：轮询各批进度、中止、删除。
 * 从服务器版移植，去掉 CSRF 令牌（软件版无鉴权、只监听 127.0.0.1）。
 */
(function () {
  'use strict';

  const rootEl = document.getElementById('batches-overview');
  if (!rootEl) return;
  const listEl = document.getElementById('bo-list');
  const libraryListEl = document.getElementById('bo-library-list');
  const showAll = rootEl.dataset.showAll === '1';
  const statusUrl = '/batches/status' + (showAll ? '?all=1' : '');

  function rowOf(bid) { return listEl?.querySelector('.bo-row[data-bid="' + bid + '"]'); }
  function libraryRowOf(taskId) {
    return libraryListEl?.querySelector('.bo-library-row[data-task-id="'
      + taskId + '"]');
  }
  function setText(el, s) { if (el) el.textContent = s; }

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
  }

  setInterval(refresh, 5000);
})();
