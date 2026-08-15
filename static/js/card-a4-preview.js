// 题卡显示偏好：单卡 A4 视图，以及筛选侧栏中的“全部 A4 / 解析全展开”。
(() => {
  'use strict';

  const A4_KEY = 'quizforge:cards:all-a4';
  const SOLUTION_KEY = 'quizforge:cards:solutions-open';
  const a4Toggle = document.getElementById('all-a4-toggle');
  const solutionToggle = document.getElementById('all-solutions-toggle');

  function readFlag(key) {
    try { return localStorage.getItem(key) === '1'; } catch (_error) { return false; }
  }

  function writeFlag(key, enabled) {
    try { localStorage.setItem(key, enabled ? '1' : '0'); } catch (_error) {}
  }

  function setCardA4(card, active) {
    if (!card) return;
    card.classList.toggle('a4-preview-active', active);
    const button = card.querySelector('.card-a4-preview-trigger');
    if (button) {
      button.setAttribute('aria-pressed', String(active));
      button.textContent = active ? '退出 A4' : 'A4 视图';
    }
  }

  function apply(card) {
    if (!card) return;
    if (readFlag(A4_KEY)) setCardA4(card, true);
    const solution = card.querySelector('details.q-solution');
    if (solution) solution.open = readFlag(SOLUTION_KEY);
  }

  function applyAll() {
    const allA4 = !!a4Toggle?.checked;
    const solutionsOpen = !!solutionToggle?.checked;
    document.querySelectorAll('#q-list > .card').forEach(card => {
      setCardA4(card, allA4);
      const solution = card.querySelector('details.q-solution');
      if (solution) solution.open = solutionsOpen;
    });
  }

  if (a4Toggle) a4Toggle.checked = readFlag(A4_KEY);
  if (solutionToggle) solutionToggle.checked = readFlag(SOLUTION_KEY);
  applyAll();

  a4Toggle?.addEventListener('change', () => {
    writeFlag(A4_KEY, a4Toggle.checked);
    applyAll();
  });
  solutionToggle?.addEventListener('change', () => {
    writeFlag(SOLUTION_KEY, solutionToggle.checked);
    applyAll();
  });

  document.addEventListener('click', event => {
    const button = event.target.closest('.card-a4-preview-trigger');
    if (!button) return;
    const card = button.closest('.card');
    if (!card) return;
    // 全局模式下点单卡，先退出“全部 A4”，再只切换当前卡，避免勾选状态与页面不符。
    if (a4Toggle?.checked) {
      a4Toggle.checked = false;
      writeFlag(A4_KEY, false);
      applyAll();
    }
    const active = !card.classList.contains('a4-preview-active');
    setCardA4(card, active);
    if (active) card.scrollIntoView({block: 'nearest', behavior: 'smooth'});
  });

  window.QCardDisplay = {apply, applyAll};
})();
