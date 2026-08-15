// 独立桌面版的常驻页面外壳。普通业务共用一个 iframe，资料库独占另一个 iframe；
// 顶栏切换只隐藏/显示，不销毁资料库，因此 PDF、分栏和各标签滚动状态持续保留。
(function () {
  'use strict';

  const shell = document.getElementById('workspace-shell');
  const pageFrame = document.getElementById('workspace-page-frame');
  const libraryFrame = document.getElementById('workspace-library-frame');
  if (!shell || !pageFrame || !libraryFrame) return;

  const navLinks = [...document.querySelectorAll('header nav a[href]')];
  let pageRoute = '';
  let activeRoute = normalizeRoute(shell.dataset.initialPath || '/');

  function normalizeRoute(raw) {
    try {
      const url = new URL(raw || '/', window.location.origin);
      if (url.origin !== window.location.origin || url.pathname === '/workspace') return '/';
      url.searchParams.delete('_embedded');
      return url.pathname + url.search + url.hash;
    } catch (error) {
      return '/';
    }
  }

  function embeddedRoute(route) {
    const url = new URL(normalizeRoute(route), window.location.origin);
    url.searchParams.set('_embedded', '1');
    return url.pathname + url.search + url.hash;
  }

  function routePath(route) {
    return new URL(normalizeRoute(route), window.location.origin).pathname;
  }

  function isLibrary(route) {
    return routePath(route).startsWith('/library');
  }

  function linkMatches(link, route) {
    const target = new URL(link.href, window.location.origin).pathname;
    const path = routePath(route);
    if (target === '/') return path === '/';
    if (target === '/library') return path.startsWith('/library');
    if (target === '/import') return path.startsWith('/import') || path.startsWith('/block-');
    if (target.includes('batches')) return path.includes('batch');
    if (target === '/about') return path === '/about' || path === '/welcome';
    return path === target || path.startsWith(target + '/');
  }

  function updateNav(route) {
    navLinks.forEach(link => {
      if (!link.classList.contains('nav-link')) return;
      link.classList.toggle('active', linkMatches(link, route));
    });
  }

  function updateOuterHistory(route, replace) {
    const url = '/workspace?path=' + encodeURIComponent(normalizeRoute(route));
    const current = window.location.pathname + window.location.search;
    if (current === url) return;
    history[replace ? 'replaceState' : 'pushState']({route: normalizeRoute(route)}, '', url);
  }

  function showRoute(route, options) {
    const normalized = normalizeRoute(route);
    const replace = Boolean(options && options.replace);
    const fromChild = Boolean(options && options.fromChild);
    activeRoute = normalized;
    if (isLibrary(normalized)) {
      if (!libraryFrame.getAttribute('src')) libraryFrame.src = embeddedRoute('/library');
      try {
        document.title = libraryFrame.contentDocument?.title || '资料库 · QuizForge';
      } catch (error) {
        document.title = '资料库 · QuizForge';
      }
      libraryFrame.hidden = false;
      pageFrame.hidden = true;
      libraryFrame.classList.add('is-active');
      pageFrame.classList.remove('is-active');
    } else {
      libraryFrame.hidden = true;
      pageFrame.hidden = false;
      libraryFrame.classList.remove('is-active');
      pageFrame.classList.add('is-active');
      if (!fromChild && pageRoute !== normalized) {
        pageRoute = normalized;
        pageFrame.src = embeddedRoute(normalized);
      }
    }
    updateNav(normalized);
    updateOuterHistory(normalized, replace);
  }

  navLinks.forEach(link => {
    const url = new URL(link.href, window.location.origin);
    if (url.origin !== window.location.origin) return;
    link.addEventListener('click', event => {
      if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      showRoute(url.pathname + url.search);
    });
  });

  window.addEventListener('message', event => {
    if (event.origin !== window.location.origin || !event.data || event.data.source !== 'quizforge') return;
    if (event.data.type === 'download') {
      const url = new URL(event.data.url || '', window.location.origin);
      if (url.origin !== window.location.origin) return;
      const anchor = document.createElement('a');
      anchor.href = url.href;
      anchor.download = event.data.filename || '';
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
      return;
    }
    if (event.data.type === 'navigate') {
      showRoute(event.data.url || '/');
      return;
    }
    if (event.data.type === 'location' && event.source === pageFrame.contentWindow) {
      const route = normalizeRoute(event.data.url);
      pageRoute = route;
      // 普通工作区内部若导航到资料库，切回已经保活的资料库实例，不另开一份。
      showRoute(route, {replace: true, fromChild: true});
    }
  });

  pageFrame.addEventListener('load', () => {
    try {
      const title = pageFrame.contentDocument?.title;
      if (title) document.title = title;
    } catch (error) { /* 同源页面正常可读；加载中暂时不可读时忽略。 */ }
  });

  libraryFrame.addEventListener('load', () => {
    if (!isLibrary(activeRoute)) return;
    try {
      document.title = libraryFrame.contentDocument?.title || '资料库 · QuizForge';
    } catch (error) {
      document.title = '资料库 · QuizForge';
    }
  });

  window.addEventListener('popstate', () => {
    const route = new URLSearchParams(window.location.search).get('path') || '/';
    showRoute(route, {replace: true});
  });

  showRoute(activeRoute, {replace: true});
})();
