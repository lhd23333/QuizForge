/* 导航红心（「转换任务」后的未处理计数）实时刷新。
 *
 * 为什么需要：徽标是服务端整页渲染时算的（app.py 的 _inject_nav_badge），
 * 而监控任务完成不会触发整页刷新——桌面壳导航只换 iframe、面板轮询只改
 * 行、外层壳 document 永不重载。不轮询的话，红心数字永远停在打开页面时
 * 那一刻的旧值（"任务完成后红心数量却不减少"的根因）。
 *
 * 只对顶层文档启动：iframe 内的侧栏被 embedded-view CSS 隐藏（桌面壳业务页、
 * Obsidian 内嵌都命中），徽标不可见，轮询纯属浪费；而且这些文档还可能被
 * 导航整体销毁，扛不住长期轮询。
 */
(function () {
  'use strict';

  if (window.parent !== window) return;
  const badge = document.getElementById('nav-batch-count');
  if (!badge) return;

  const POLL_MS = 10000;

  function render(count) {
    badge.textContent = String(count);
    badge.hidden = !count;
  }

  async function refresh() {
    try {
      const res = await fetch('/nav/count');
      if (!res.ok) return;
      const data = await res.json();
      const count = Number(data && data.nav_batch_count);
      if (Number.isFinite(count) && count >= 0) render(count);
    } catch (error) {
      /* 后端重启中之类：下一轮再试，不打扰用户。 */
    }
  }

  setInterval(refresh, POLL_MS);
})();
