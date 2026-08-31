// 全局帮助抽屉。业务页在桌面父壳的 iframe 中运行时，把静态帮助内容交给父壳显示。
(function () {
  'use strict';

  const THEME_COLOR_RE = /^#[0-9a-f]{6}$/i;

  function normalizeThemeMode(value) {
    // 与服务端和桌面外壳保持同一安全默认：只有明确的 light 才使用浅色。
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
    const luminance = channels.map(value => value <= 0.03928
      ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);
    const relative = 0.2126 * luminance[0] + 0.7152 * luminance[1] + 0.0722 * luminance[2];
    const backgrounds = mode === 'light' ? ['#f5f6f8', '#ffffff', '#f0f1f3'] : ['#202124', '#292a2d', '#323338'];
    const ratio = background => {
      const bgChannels = [1, 3, 5].map(index => parseInt(background.slice(index, index + 2), 16) / 255);
      const bgLinear = bgChannels.map(value => value <= 0.03928
        ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);
      const bgLuminance = 0.2126 * bgLinear[0] + 0.7152 * bgLinear[1] + 0.0722 * bgLinear[2];
      return (Math.max(relative, bgLuminance) + 0.05) / (Math.min(relative, bgLuminance) + 0.05);
    };
    if (backgrounds.every(background => ratio(background) >= 4.5)) return color;
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
      if (backgrounds.every(background => {
        const bgChannels = [1, 3, 5].map(index => parseInt(background.slice(index, index + 2), 16) / 255);
        const bgLinear = bgChannels.map(value => value <= 0.03928
          ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);
        const bgLuminance = 0.2126 * bgLinear[0] + 0.7152 * bgLinear[1] + 0.0722 * bgLinear[2];
        return (Math.max(candidateLuminance, bgLuminance) + 0.05)
          / (Math.min(candidateLuminance, bgLuminance) + 0.05) >= 4.5;
      })) return candidate;
    }
    return mode === 'light' ? '#101214' : '#f1f3f4';
  }

  function applyShellTheme(mode, color, textColor) {
    const nextMode = normalizeThemeMode(mode);
    const nextColor = normalizeThemeColor(color);
    document.documentElement.dataset.theme = nextMode;
    if (nextColor) {
      document.documentElement.style.setProperty('--qf-user-accent', nextColor);
      document.documentElement.style.setProperty('--primary', nextColor);
      document.documentElement.style.setProperty('--qf-user-on-accent', accentForeground(nextColor));
      const derivedText = accentTextColor(nextColor, nextMode);
      document.documentElement.style.setProperty('--qf-user-accent-text',
        normalizeThemeColor(textColor) === derivedText ? textColor.toLowerCase() : derivedText);
    } else {
      document.documentElement.style.removeProperty('--qf-user-accent');
      document.documentElement.style.removeProperty('--primary');
      document.documentElement.style.removeProperty('--qf-user-on-accent');
      document.documentElement.style.removeProperty('--qf-user-accent-text');
    }
  }

  // 主题同步是所有嵌入页的基础能力，不应依赖页面是否带帮助抽屉。
  window.addEventListener('message', event => {
    if (event.origin !== window.location.origin || event.source !== window.parent
        || event.data?.source !== 'quizforge' || event.data?.type !== 'shell-theme') return;
    applyShellTheme(event.data.mode, event.data.color, event.data.textColor);
  });

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
      mode: normalizeThemeMode(document.documentElement.dataset.theme),
      color: normalizeThemeColor(
        document.documentElement.style.getPropertyValue('--qf-user-accent')
        || document.documentElement.style.getPropertyValue('--primary')),
      textColor: normalizeThemeColor(
        document.documentElement.style.getPropertyValue('--qf-user-accent-text')),
    }, window.location.origin);
  }

})();
