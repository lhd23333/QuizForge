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
  let pendingQuestionFile = null;
  let questionReady = false;
  const THEME_COLOR_RE = /^#[0-9a-f]{6}$/i;

  function normalizeThemeMode(value) {
    // 非法值统一回落深色，和服务端 ui_prefs 的安全默认保持一致。
    return value === 'light' ? 'light' : 'dark';
  }

  function normalizeThemeColor(value) {
    const color = String(value || '').trim();
    return THEME_COLOR_RE.test(color) ? color.toLowerCase() : '';
  }

  function accentForeground(color) {
    const channels = [1, 3, 5].map(index => parseInt(color.slice(index, index + 2), 16) / 255);
    const linear = channels.map(value => value <= 0.03928
      ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);
    const luminance = 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
    const darkChannels = [16, 18, 20].map(value => value / 255).map(value => value <= 0.03928
      ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);
    const darkLuminance = 0.2126 * darkChannels[0] + 0.7152 * darkChannels[1] + 0.0722 * darkChannels[2];
    const darkRatio = (Math.max(luminance, darkLuminance) + 0.05)
      / (Math.min(luminance, darkLuminance) + 0.05);
    const lightRatio = (Math.max(luminance, 1) + 0.05) / (Math.min(luminance, 1) + 0.05);
    if (darkRatio >= lightRatio && darkRatio >= 4.5) return '#101214';
    if (lightRatio >= 4.5) return '#ffffff';
    const blackRatio = (luminance + 0.05) / 0.05;
    return blackRatio >= lightRatio ? '#000000' : '#ffffff';
  }

  function accentTextColor(color, mode) {
    const channels = [1, 3, 5].map(index => parseInt(color.slice(index, index + 2), 16) / 255);
    const linear = channels.map(value => value <= 0.03928
      ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);
    const sourceLuminance = 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
    const backgrounds = mode === 'light' ? ['#f5f6f8', '#ffffff', '#f0f1f3'] : ['#202124', '#292a2d', '#323338'];
    const luminanceOf = background => {
      const values = [1, 3, 5].map(index => parseInt(background.slice(index, index + 2), 16) / 255);
      const converted = values.map(value => value <= 0.03928
        ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);
      return 0.2126 * converted[0] + 0.7152 * converted[1] + 0.0722 * converted[2];
    };
    const readable = luminance => backgrounds.every(background => {
      const backgroundLuminance = luminanceOf(background);
      return (Math.max(luminance, backgroundLuminance) + 0.05)
        / (Math.min(luminance, backgroundLuminance) + 0.05) >= 4.5;
    });
    if (readable(sourceLuminance)) return color;
    const target = mode === 'light' ? [0, 0, 0] : [255, 255, 255];
    const source = [1, 3, 5].map(index => parseInt(color.slice(index, index + 2), 16));
    for (let step = 1; step <= 100; step += 1) {
      const amount = step / 100;
      const candidate = '#' + source.map((channel, index) =>
        Math.round(channel * (1 - amount) + target[index] * amount)
          .toString(16).padStart(2, '0')).join('');
      const candidateChannels = [1, 3, 5].map(index => parseInt(candidate.slice(index, index + 2), 16) / 255);
      const candidateLinear = candidateChannels.map(value => value <= 0.03928
        ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);
      const candidateLuminance = 0.2126 * candidateLinear[0] + 0.7152 * candidateLinear[1] + 0.0722 * candidateLinear[2];
      if (readable(candidateLuminance)) return candidate;
    }
    return mode === 'light' ? '#101214' : '#f1f3f4';
  }

  function readDocumentTheme() {
    const style = document.documentElement.style;
    const mode = normalizeThemeMode(document.documentElement.dataset.theme);
    const color = normalizeThemeColor(
      style.getPropertyValue('--qf-user-accent')
      || style.getPropertyValue('--primary'));
    return {
      mode,
      color,
      textColor: color
        ? (normalizeThemeColor(style.getPropertyValue('--qf-user-accent-text'))
          || accentTextColor(color, mode))
        : '',
    };
  }

  const persistedTheme = readDocumentTheme();
  let shellTheme = {...persistedTheme};
  let previewFrame = null;

  function applyDocumentTheme(theme) {
    const mode = normalizeThemeMode(theme?.mode);
    const color = normalizeThemeColor(theme?.color);
    const derivedText = color ? accentTextColor(color, mode) : '';
    const suppliedText = normalizeThemeColor(theme?.textColor);
    const textColor = suppliedText === derivedText ? suppliedText : derivedText;
    document.documentElement.dataset.theme = mode;
    if (color) {
      document.documentElement.style.setProperty('--qf-user-accent', color);
      document.documentElement.style.setProperty('--primary', color);
      document.documentElement.style.setProperty('--qf-user-on-accent', accentForeground(color));
      document.documentElement.style.setProperty('--qf-user-accent-text', textColor);
    } else {
      // 空色值表示使用 CSS 的主题默认色，不能残留上一份自定义色。
      document.documentElement.style.removeProperty('--qf-user-accent');
      document.documentElement.style.removeProperty('--primary');
      document.documentElement.style.removeProperty('--qf-user-on-accent');
      document.documentElement.style.removeProperty('--qf-user-accent-text');
    }
  }

  function setShellTheme(theme, broadcast = true) {
    const mode = normalizeThemeMode(theme?.mode);
    const color = normalizeThemeColor(theme?.color);
    const derivedText = color ? accentTextColor(color, mode) : '';
    const suppliedText = normalizeThemeColor(theme?.textColor);
    shellTheme = {
      mode,
      color,
      textColor: suppliedText === derivedText ? suppliedText : derivedText,
    };
    applyDocumentTheme(shellTheme);
    if (broadcast) broadcastTheme();
  }

  function restorePersistedTheme() {
    previewFrame = null;
    setShellTheme(persistedTheme);
  }

  function broadcastTheme() {
    frames.forEach(frame => {
      if (!frame.contentWindow) return;
      frame.contentWindow.postMessage({
        source: 'quizforge', type: 'shell-theme',
        mode: shellTheme.mode, color: shellTheme.color, textColor: shellTheme.textColor,
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

  function sendQuestionFile(meta) {
    const path = String(meta?.path || '').trim();
    if (!path) return;
    const payload = {source: 'quizforge', type: 'open-question-file', path,
      kind: String(meta?.kind || '')};
    if (!questionReady || !questionFrame.contentWindow) {
      pendingQuestionFile = payload;
      return;
    }
    try {
      questionFrame.contentWindow.postMessage(payload, window.location.origin);
      pendingQuestionFile = null;
    } catch (_error) {
      pendingQuestionFile = payload;
    }
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

    // 预览只属于设置页当前文档。导航触发 iframe 换页前先复位，避免在
    // pagehide 消息尚未到达时把临时主题带到其它工作区。
    if (previewFrame) {
      const currentPreviewRoute = trackedRoute(previewFrame);
      if (previewFrame !== targetFrame
          || (currentPreviewRoute
              && routePath(currentPreviewRoute) !== routePath(normalized))) {
        restorePersistedTheme();
      }
    }
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
        if (targetFrame === questionFrame) questionReady = false;
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
      mode: shellTheme.mode, color: shellTheme.color, textColor: shellTheme.textColor,
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
    if (event.data.type === 'open-agent') {
      // 业务 iframe 不保留重复的全局导航；它可以请求顶层外壳打开同一份
      // Agent 会话面板。只接受本外壳实际创建的 iframe，避免其它同源窗口
      // 借消息触发桌面 UI。
      const sourceFrame = frames.find(frame => event.source === frame.contentWindow);
      if (!sourceFrame) return;
      document.getElementById('agent-open')?.click();
      return;
    }
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
    if (event.data.type === 'open-question-file'
        || event.data.type === 'open-library-file') {
      if (!sourceFrame) return;
      const path = String(event.data.path || '').trim();
      if (!path) return;
      // 旧消息名只保留协议兼容，语义统一为题库内文件标签，不再切换资料库 iframe。
      if (sourceFrame === questionFrame) sendQuestionFile({path, kind: event.data.kind});
      else {
        pendingQuestionFile = {source: 'quizforge', type: 'open-question-file', path,
          kind: String(event.data.kind || '')};
        showRoute('/', {force: true});
        sendQuestionFile(pendingQuestionFile);
      }
      return;
    }
    if (event.data.type === 'page-theme' && sourceFrame) {
      const nextTheme = {
        mode: normalizeThemeMode(event.data.mode),
        color: normalizeThemeColor(event.data.color),
        textColor: normalizeThemeColor(event.data.textColor),
      };
      if (event.data.preview === true) {
        previewFrame = sourceFrame;
        setShellTheme(nextTheme);
        return;
      }
      // 设置页预览期间，隐藏 iframe 发来的旧服务端主题不能打断预览。
      if (previewFrame && previewFrame !== sourceFrame) return;
      persistedTheme.mode = nextTheme.mode;
      persistedTheme.color = nextTheme.color;
      persistedTheme.textColor = nextTheme.textColor
        || (nextTheme.color ? accentTextColor(nextTheme.color, nextTheme.mode) : '');
      previewFrame = null;
      setShellTheme(nextTheme);
      return;
    }
    if (event.data.type === 'page-theme-reset' && sourceFrame) {
      if (previewFrame === sourceFrame) restorePersistedTheme();
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
    questionReady = true;
    questionFrame.contentWindow?.postMessage({source: 'quizforge', type: 'shell-theme',
      mode: shellTheme.mode, color: shellTheme.color, textColor: shellTheme.textColor}, window.location.origin);
    if (pendingQuestionFile) sendQuestionFile(pendingQuestionFile);
    if (activeFrame !== questionFrame) return;
    try {
      const title = questionFrame.contentDocument?.title;
      if (title) document.title = title;
    } catch (error) { /* 同源页面正常可读；加载中暂时不可读时忽略。 */ }
  });

  pageFrame.addEventListener('load', () => {
    pageFrame.contentWindow?.postMessage({source: 'quizforge', type: 'shell-theme',
      mode: shellTheme.mode, color: shellTheme.color, textColor: shellTheme.textColor}, window.location.origin);
    if (activeFrame !== pageFrame) return;
    try {
      const title = pageFrame.contentDocument?.title;
      if (title) document.title = title;
    } catch (error) { /* 同源页面正常可读；加载中暂时不可读时忽略。 */ }
  });

  libraryFrame.addEventListener('load', () => {
    libraryReady = false;
    libraryFrame.contentWindow?.postMessage({source: 'quizforge', type: 'shell-theme',
      mode: shellTheme.mode, color: shellTheme.color, textColor: shellTheme.textColor}, window.location.origin);
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

  window.addEventListener('pywebviewready', () => {
    sendLibraryOpen(pendingLibraryPath);
    if (pendingQuestionFile) sendQuestionFile(pendingQuestionFile);
  });

  showRoute(activeRoute, {replace: true});
})();
