/* 转换任务总面板：轮询各批进度、中止、删除。
 * 从服务器版移植，去掉 CSRF 令牌（软件版无鉴权、只监听 127.0.0.1）。
 */
(function () {
  'use strict';

  const listEl = document.getElementById('bo-list');
  if (!listEl) return;
  const showAll = listEl.dataset.showAll === '1';
  const statusUrl = '/batches/status' + (showAll ? '?all=1' : '');

  function rowOf(bid) { return listEl.querySelector('.bo-row[data-bid="' + bid + '"]'); }
  function setText(el, s) { if (el) el.textContent = s; }

  // ---------- 中止整批 ----------
  listEl.addEventListener('click', async ev => {
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
  listEl.addEventListener('click', async ev => {
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

  // ---------- 轮询刷新 ----------
  async function refresh() {
    let data;
    try {
      const res = await fetch(statusUrl);
      data = await res.json();
    } catch (e) { return; }   // 后端重启中之类，下一轮再试
    if (!data.ok) return;

    const seen = new Set();
    for (const b of data.batches) {
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
        const add = (text, cls) => {
          const s = document.createElement('span');
          s.className = cls;
          s.textContent = text;
          chips.appendChild(s);
        };
        if (b.converting) add('🔄 转换中 ' + b.converting, 'bp-st');
        if (b.pending) add('⏳ 等待 ' + b.pending, 'bp-st');
        if (b.ready) add('✅ 待审核 ' + b.ready, 'bp-st bp-ready');
        if (b.awaiting_block_review)
          add('✂ 待拆题审核 ' + b.awaiting_block_review, 'bp-st bp-ready');
        if (b.errors) add('❌ 错误 ' + b.errors, 'bp-st bp-err-st');
        if (b.reviewed) add('已处理 ' + b.reviewed, 'muted');
        if (b.cancelled) add('已中止', 'muted');
      }

      row.classList.toggle('bo-finished', !!b.finished);
      const cancelBtn = row.querySelector('.bo-cancel');
      if (cancelBtn) cancelBtn.hidden = !!b.finished;
      const delBtn = row.querySelector('.bo-delete');
      if (delBtn) delBtn.hidden = !!b.busy;
    }

    // 「只看进行中」模式下：处理完的批次会从接口里消失，行也跟着撤掉
    if (!showAll) {
      listEl.querySelectorAll('.bo-row').forEach(row => {
        if (!seen.has(row.dataset.bid)) row.remove();
      });
      if (!listEl.querySelectorAll('.bo-row').length) location.reload();
    }
  }

  setInterval(refresh, 5000);
})();
