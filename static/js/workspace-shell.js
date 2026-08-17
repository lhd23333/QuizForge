// 独立桌面版的常驻页面外壳。题库与资料库各自保活，其他业务共用一个 iframe；
// 切换栏目只隐藏/显示，不销毁已经加载的题卡、滚动位置、PDF 或资料库标签。
(function () {
  'use strict';

  const shell = document.getElementById('workspace-shell');
  const questionFrame = document.getElementById('workspace-question-frame');
  const pageFrame = document.getElementById('workspace-page-frame');
  const libraryFrame = document.getElementById('workspace-library-frame');
  if (!shell || !questionFrame || !pageFrame || !libraryFrame) return;

  const navLinks = [...document.querySelectorAll('.app-sidebar nav a[href]')];
  const frames = [questionFrame, pageFrame, libraryFrame];
  const helpByFrame = new Map();
  let questionRoute = '';
  let pageRoute = '';
  let activeRoute = normalizeRoute(shell.dataset.initialPath || '/');
  let activeFrame = null;
  let libraryReady = false;
  let pendingLibraryPath = '';
  let shellTheme = {mode: 'light', color: ''};

  function broadcastTheme() {
    frames.forEach(frame => {
      if (!frame.contentWindow) return;
      frame.contentWindow.postMessage({
        source: 'quizforge', type: 'shell-theme',
        mode: shellTheme.mode, color: shellTheme.color,
      }, window.location.origin);
    });
  }

  function sendLibraryOpen(path) {
    if (!libraryReady || !path || !libraryFrame.contentWindow) return;
    libraryFrame.contentWindow.postMessage({
      source: 'quizforge', type: 'open-library-file', path,
    }, window.location.origin);
    pendingLibraryPath = '';
  }

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

  function isQuestionBank(route) {
    return routePath(route) === '/';
  }

  function frameForRoute(route) {
    if (isLibrary(route)) return libraryFrame;
    if (isQuestionBank(route)) return questionFrame;
    return pageFrame;
  }

  function trackedRoute(frame) {
    if (frame === questionFrame) return questionRoute;
    if (frame === pageFrame) return pageRoute;
    return '/library';
  }

  function setTrackedRoute(frame, route) {
    if (frame === questionFrame) questionRoute = route;
    else if (frame === pageFrame) pageRoute = route;
  }

  function sameSection(requested, current) {
    if (!current) return false;
    const targetPath = routePath(requested);
    const currentPath = routePath(current);
    if (targetPath === '/') return currentPath === '/';
    if (targetPath === '/handouts') return currentPath.startsWith('/handouts');
    if (targetPath === '/library') return currentPath.startsWith('/library');
    if (targetPath === '/import') {
      return currentPath.startsWith('/import') || currentPath.startsWith('/block-');
    }
    if (targetPath === '/batches') return currentPath.includes('batch');
    return targetPath === currentPath;
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
    let normalized = normalizeRoute(route);
    const replace = Boolean(options && options.replace);
    const sourceFrame = options && options.sourceFrame;
    let targetFrame = frameForRoute(normalized);
    const currentTargetRoute = trackedRoute(targetFrame);
    // 从其他栏目点回题库或讲义时恢复原页面，而不是重新生成题卡或重载编辑器。
    // 用户已经停留在该栏目时再次点导航，仍按链接本义回到栏目根页面。
    if (!(options && options.force) && !sourceFrame && activeFrame && activeFrame !== targetFrame
        && sameSection(normalized, currentTargetRoute)) {
      normalized = currentTargetRoute;
      targetFrame = frameForRoute(normalized);
    }
    activeRoute = normalized;
    if (targetFrame === libraryFrame) {
      if (!libraryFrame.getAttribute('src')) libraryFrame.src = embeddedRoute('/library');
      try {
        document.title = libraryFrame.contentDocument?.title || '资料库 · QuizForge';
      } catch (error) {
        document.title = '资料库 · QuizForge';
      }
    } else {
      const current = trackedRoute(targetFrame);
      if (sourceFrame !== targetFrame && current !== normalized) {
        setTrackedRoute(targetFrame, normalized);
        targetFrame.src = embeddedRoute(normalized);
      } else if (!current) {
        setTrackedRoute(targetFrame, normalized);
      }
    }
    frames.forEach(frame => {
      const selected = frame === targetFrame;
      frame.hidden = !selected;
      frame.classList.toggle('is-active', selected);
    });
    activeFrame = targetFrame;
    try {
      const title = targetFrame.contentDocument?.title;
      if (title) document.title = title;
    } catch (error) { /* 同源页面正常可读；首次加载前没有标题时等待 load。 */ }
    updateNav(normalized);
    const help = helpByFrame.get(targetFrame);
    if (help && window.QuizForgeHelp) window.QuizForgeHelp.set(help.title, help.html);
    targetFrame.contentWindow?.postMessage({
      source: 'quizforge', type: 'shell-theme',
      mode: shellTheme.mode, color: shellTheme.color,
    }, window.location.origin);
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
    if (event.data.type === 'page-help'
        && frames.some(frame => event.source === frame.contentWindow)) {
      const help = {title: event.data.title || '当前页面帮助', html: event.data.html || ''};
      const source = frames.find(frame => event.source === frame.contentWindow);
      helpByFrame.set(source, help);
      if (source === activeFrame && window.QuizForgeHelp) {
        window.QuizForgeHelp.set(help.title, help.html);
      }
      return;
    }
    const sourceFrame = frames.find(frame => event.source === frame.contentWindow);
    if (event.data.type === 'library-ready') {
      if (sourceFrame !== libraryFrame) return;
      libraryReady = true;
      sendLibraryOpen(pendingLibraryPath);
      return;
    }
    if (event.data.type === 'open-file') {
      if (!sourceFrame || sourceFrame === libraryFrame) return;
      const path = String(event.data.path || '').trim();
      if (!path) return;
      const api = window.pywebview?.api;
      if (api?.open_local_file) api.open_local_file(path).catch?.(() => {});
      else if (event.data.fallbackUrl) window.open(String(event.data.fallbackUrl), '_blank', 'noopener');
      return;
    }
    if (event.data.type === 'open-library-file') {
      if (!sourceFrame || sourceFrame === libraryFrame) return;
      const path = String(event.data.path || '').trim();
      if (!path) return;
      pendingLibraryPath = path;
      showRoute('/library');
      sendLibraryOpen(path);
      return;
    }
    if (event.data.type === 'page-theme'
        && frames.some(frame => event.source === frame.contentWindow)) {
      const mode = event.data.mode === 'dark' ? 'dark' : 'light';
      const color = String(event.data.color || '').trim();
      shellTheme = {mode, color};
      document.documentElement.dataset.theme = mode;
      if (/^#[0-9a-f]{6}$/i.test(color)) document.documentElement.style.setProperty('--primary', color);
      broadcastTheme();
      return;
    }
    if (event.data.type === 'location') {
      const source = frames.find(frame => event.source === frame.contentWindow);
      if (!source || source === libraryFrame) return;
      const route = normalizeRoute(event.data.url);
      setTrackedRoute(source, route);
      // 隐藏页可能在异步初始化结束后才 replaceState。只记录它的新位置，不能让这条
      // 迟到消息把用户从已经切到的栏目强行拉回去。
      if (source !== activeFrame) return;
      showRoute(route, {replace: true, sourceFrame: source});
    }
  });

  questionFrame.addEventListener('load', () => {
    questionFrame.contentWindow?.postMessage({source: 'quizforge', type: 'shell-theme',
      mode: shellTheme.mode, color: shellTheme.color}, window.location.origin);
    if (activeFrame !== questionFrame) return;
    try {
      const title = questionFrame.contentDocument?.title;
      if (title) document.title = title;
    } catch (error) { /* 同源页面正常可读；加载中暂时不可读时忽略。 */ }
  });

  pageFrame.addEventListener('load', () => {
    pageFrame.contentWindow?.postMessage({source: 'quizforge', type: 'shell-theme',
      mode: shellTheme.mode, color: shellTheme.color}, window.location.origin);
    if (activeFrame !== pageFrame) return;
    try {
      const title = pageFrame.contentDocument?.title;
      if (title) document.title = title;
    } catch (error) { /* 同源页面正常可读；加载中暂时不可读时忽略。 */ }
  });

  libraryFrame.addEventListener('load', () => {
    libraryReady = false;
    libraryFrame.contentWindow?.postMessage({source: 'quizforge', type: 'shell-theme',
      mode: shellTheme.mode, color: shellTheme.color}, window.location.origin);
    if (activeFrame !== libraryFrame) return;
    try {
      document.title = libraryFrame.contentDocument?.title || '资料库 · QuizForge';
    } catch (error) {
      document.title = '资料库 · QuizForge';
    }
  });

  window.addEventListener('popstate', () => {
    const route = new URLSearchParams(window.location.search).get('path') || '/';
    showRoute(route, {replace: true, force: true});
  });

  window.addEventListener('pywebviewready', () => sendLibraryOpen(pendingLibraryPath));

  showRoute(activeRoute, {replace: true});
})();
