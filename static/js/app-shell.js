// 全局帮助抽屉。业务页在桌面父壳的 iframe 中运行时，把静态帮助内容交给父壳显示。
(function () {
  'use strict';

  function desktopApi() {
    const candidates = [window];
    try {
      if (window.parent && window.parent !== window) candidates.push(window.parent);
      if (window.top && !candidates.includes(window.top)) candidates.push(window.top);
    } catch (_) {
      // 浏览器版或跨域嵌入时不访问父窗口，继续按普通网页运行。
    }
    for (const candidate of candidates) {
      try {
        if (candidate.pywebview?.api) return candidate.pywebview.api;
      } catch (_) {
        // WindowProxy 可能因跨域拒绝访问，忽略该候选窗口。
      }
    }
    return null;
  }

  function whenDesktopReady(callback) {
    if (desktopApi()) {
      callback(desktopApi());
      return;
    }
    const candidates = [window];
    try {
      if (window.parent && window.parent !== window) candidates.push(window.parent);
      if (window.top && !candidates.includes(window.top)) candidates.push(window.top);
    } catch (_) {
      // 同 desktopApi：网页模式无需桌面就绪事件。
    }
    candidates.forEach(candidate => {
      try {
        candidate.addEventListener('pywebviewready', () => {
          const api = desktopApi();
          if (api) callback(api);
        }, {once: true});
      } catch (_) {
        // 跨域父窗口不可监听。
      }
    });
  }

  window.QuizForgeDesktop = {api: desktopApi, whenReady: whenDesktopReady};

  const dialog = document.getElementById('page-help-dialog');
  const title = document.getElementById('page-help-title');
  const body = document.getElementById('page-help-body');
  const template = document.getElementById('page-help-content');
  const openButton = document.querySelector('[data-help-open]');
  const closeButton = document.querySelector('[data-help-close]');
  if (!dialog || !title || !body || !template) return;

  function setHelp(nextTitle, html) {
    title.textContent = nextTitle || '当前页面帮助';
    body.innerHTML = html || '<p>当前页面暂无补充说明。</p>';
  }

  function localHelp() {
    return {
      title: template.dataset.helpTitle || '当前页面帮助',
      html: template.innerHTML,
    };
  }

  function openHelp() {
    if (typeof dialog.showModal === 'function') dialog.showModal();
    else dialog.setAttribute('open', '');
  }

  setHelp(localHelp().title, localHelp().html);
  window.QuizForgeHelp = {set: setHelp, local: localHelp};

  if (openButton) openButton.addEventListener('click', openHelp);
  if (closeButton) closeButton.addEventListener('click', () => dialog.close());
  dialog.addEventListener('click', event => {
    if (event.target === dialog) dialog.close();
  });

  if (window.parent !== window) {
    const help = localHelp();
    window.parent.postMessage({
      source: 'quizforge',
      type: 'page-help',
      url: window.location.href,
      title: help.title,
      html: help.html,
    }, window.location.origin);
    window.parent.postMessage({
      source: 'quizforge',
      type: 'page-theme',
      mode: document.documentElement.dataset.theme || 'light',
      color: document.documentElement.style.getPropertyValue('--primary').trim(),
    }, window.location.origin);
  }

  window.addEventListener('message', event => {
    if (event.origin !== window.location.origin || event.source !== window.parent
        || event.data?.source !== 'quizforge' || event.data?.type !== 'shell-theme') return;
    const mode = event.data.mode === 'dark' ? 'dark' : 'light';
    const color = String(event.data.color || '').trim();
    document.documentElement.dataset.theme = mode;
    if (/^#[0-9a-f]{6}$/i.test(color)) {
      document.documentElement.style.setProperty('--primary', color);
    }
  });
})();
