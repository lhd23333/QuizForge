// 拆题结果人工审核页（block_review.html）：左对照原文件、右改块（合并/拆分/
// 删除/调序），每块源码下方是实时渲染预览。提交时把当前 DOM 顺序整份序列化为
// blocks_json 交给后端（见 app.py 的 batch_group_blocks_confirm）。
//
// 与校对页的题卡不同：那边操作的是「题目」（已规范化的最终结果），这里操作的是
// 「块」（切块阶段的原始碎片，还没过 LLM）。多了调序和删除——块的顺序本身就是
// 信息（后续判断「这块解析属于哪道题」靠顺序），审核阶段必须能调整它。
(function () {
  'use strict';

  const list = document.getElementById('br-list');
  const form = document.getElementById('block-review-form');
  if (!list || !form) return;

  const ZONE_LABEL = {stem: '题干区', solution: '解析区'};
  // 审核阶段图还在 MinerU 中间目录里，正文里是 `![](images/xxx)` 相对引用，
  // 得换成 batch_group_block_image 那条临时路径才取得到（见该路由说明）。
  const IMG_BASE = list.dataset.imgbase || '';

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  // 块正文仍是 MinerU 的 images/ 相对路径；公共预览器除安全转义与还原图片外，
  // 也会把 HTML/管道表格渲染出来，人工审核时才能看清表格有没有被切断。
  function previewHtml(text) {
    return window.QTextPreview.render(text, {
      imageMode: 'mineru', imageBase: IMG_BASE,
    });
  }

  // 公式渲染走 QMath（见 static/js/math.js）——定界符和 macros 只该有一份配置，
  // 别在这里直接调 renderMathInElement。
  function renderPreview(card) {
    const ta = card.querySelector('.br-text');
    const box = card.querySelector('.br-preview');
    if (!ta || !box) return;
    box.innerHTML = previewHtml(ta.value);
    if (window.QMath) window.QMath.typeset(box);
  }

  // 输入时不要每个字符都重排一遍公式：KaTeX 虽快，长块连打时仍会顿。
  const timers = new WeakMap();
  function schedulePreview(card) {
    clearTimeout(timers.get(card));
    timers.set(card, setTimeout(() => renderPreview(card), 200));
  }

  function makeCard(block) {
    const card = document.createElement('article');
    card.className = 'card br-card';
    card.dataset.number = block.number === null || block.number === undefined
      ? '' : String(block.number);
    card.dataset.section = block.section || '';
    card.dataset.group = block.group || '';
    card.dataset.lineNo = block.line_no || 0;
    card.dataset.kind = block.kind || 'unknown';

    const numLabel = block.number === null || block.number === undefined
      ? '（无题号）' : `第 ${block.number} 题`;
    const zone = block.zone === 'solution' ? 'solution' : 'stem';

    card.innerHTML = `
      <div class="card-head">
        <span class="br-num">${numLabel}</span>
        <select class="input br-zone" style="width:auto;font-size:12px;padding:2px 6px">
          <option value="stem" ${zone === 'stem' ? 'selected' : ''}>${ZONE_LABEL.stem}</option>
          <option value="solution" ${zone === 'solution' ? 'selected' : ''}>${ZONE_LABEL.solution}</option>
        </select>
        ${block.section ? `<span class="muted" style="font-size:12px">分区：${escapeHtml(block.section)}</span>` : ''}
        ${block.group ? `<span class="muted" style="font-size:12px">分组：${escapeHtml(block.group)}</span>` : ''}
        <span class="spacer"></span>
        <button type="button" class="btn btn-sm br-up" title="上移">⬆</button>
        <button type="button" class="btn btn-sm br-down" title="下移">⬇</button>
        <button type="button" class="btn btn-sm br-merge-up" title="把本块内容并入上一块（用于误拆）">⬆ 合并到上一块</button>
        <button type="button" class="btn btn-sm br-split" title="在本块下方插入一张空块，把多余内容剪贴过去">✂ 拆分</button>
        <button type="button" class="btn btn-sm br-delete" title="删除误切出来的块">🗑 删除</button>
      </div>
      <textarea class="input br-text" rows="5"
                style="font-size:12px;width:100%">${escapeHtml(block.text || '')}</textarea>
      <div class="br-preview qcard-render"></div>
    `;
    renderPreview(card);
    return card;
  }

  function updateCount() {
    const n = list.querySelectorAll('.br-card').length;
    const countEl = document.getElementById('br-count');
    if (countEl) countEl.textContent = `共 ${n} 块`;
  }

  // 初始渲染：数据由模板写在 data-blocks 上（JSON），不是内联脚本变量。
  const initial = JSON.parse(list.dataset.blocks || '[]');
  initial.forEach(b => list.appendChild(makeCard(b)));
  updateCount();

  list.addEventListener('input', e => {
    const card = e.target.closest('.br-card');
    if (card && e.target.classList.contains('br-text')) schedulePreview(card);
  });

  list.addEventListener('click', e => {
    const card = e.target.closest('.br-card');
    if (!card) return;

    if (e.target.closest('.br-up')) {
      const prev = card.previousElementSibling;
      if (prev) card.parentElement.insertBefore(card, prev);
      return;
    }
    if (e.target.closest('.br-down')) {
      const next = card.nextElementSibling;
      if (next) card.parentElement.insertBefore(next, card);
      return;
    }
    if (e.target.closest('.br-merge-up')) {
      const prev = card.previousElementSibling;
      if (!prev) { alert('已经是第一块，无法向上合并'); return; }
      const pText = prev.querySelector('.br-text');
      const cText = card.querySelector('.br-text');
      if (pText && cText && cText.value.trim()) {
        pText.value = pText.value.replace(/\s+$/, '') + '\n' + cText.value.trim();
        renderPreview(prev);
      }
      card.remove();
      updateCount();
      return;
    }
    if (e.target.closest('.br-split')) {
      const zone = card.querySelector('.br-zone').value;
      const nb = makeCard({
        number: null, text: '', section: card.dataset.section,
        group: card.dataset.group, zone, line_no: card.dataset.lineNo,
        kind: 'unknown',
      });
      card.after(nb);
      nb.querySelector('.br-text').focus();
      updateCount();
      return;
    }
    if (e.target.closest('.br-delete')) {
      if (list.querySelectorAll('.br-card').length <= 1) {
        alert('至少要保留一块');
        return;
      }
      card.remove();
      updateCount();
      return;
    }
  });

  // 「只看原文件」：块多的时候源码框占掉大半屏，切掉能一眼扫完整份切块结果。
  const toggle = document.getElementById('br-toggle-src');
  if (toggle) {
    toggle.addEventListener('click', () => {
      list.classList.toggle('br-hide-src');
      toggle.textContent = list.classList.contains('br-hide-src')
        ? '显示块源码' : '只看原文件';
    });
  }

  function serialize() {
    return Array.from(list.querySelectorAll('.br-card')).map((card, i) => {
      const numRaw = card.dataset.number;
      return {
        index: i,
        number: numRaw === '' ? null : parseInt(numRaw, 10),
        text: card.querySelector('.br-text').value,
        section: card.dataset.section || null,
        group: card.dataset.group || null,
        zone: card.querySelector('.br-zone').value,
        line_no: parseInt(card.dataset.lineNo, 10) || 0,
        kind: card.dataset.kind || 'unknown',
      };
    });
  }

  form.querySelectorAll('[data-br-submit]').forEach(btn => {
    btn.addEventListener('click', () => {
      if (!list.querySelectorAll('.br-card').length) {
        alert('没有可用的块，请检查是否把所有块都删掉了');
        return;
      }
      document.getElementById('block-review-action').value = btn.dataset.brSubmit;
      document.getElementById('block-review-json').value =
        JSON.stringify(serialize());
      form.submit();
    });
  });
})();
