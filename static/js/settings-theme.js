/* 设置页「外观主题」的实时预览：点色板/取色器即时改主题色，切模式即时切深浅。
 *
 * 只是预览，持久化仍靠表单提交 —— 离开页面不保存时，桌面外壳会收到复位消息。
 *
 * 服务器版这段写在 settings.html 的内联 <script> 里；本项目按前端约定放
 * static/js/ 并用 static_v + defer 引入，JS 里不写 Jinja（色值全部来自
 * DOM 的 data-color / input.value）。
 */
(function () {
  const html = document.documentElement;
  const colorInput = document.getElementById('theme-color-input');
  const customInput = document.getElementById('theme-color-custom');
  const swatches = document.querySelectorAll('#color-swatches .swatch');
  if (!colorInput && !swatches.length) return;   // 不是设置页

  const THEME_DEFAULTS = {
    dark: '#63b3ff',
    light: '#2457d6',
  };
  const COLOR_RE = /^#[0-9a-f]{6}$/i;
  let colorIsCustom = html.dataset.themeColorCustom === '1';
  let previewActive = false;

  function normalizeMode(value) {
    return value === 'light' ? 'light' : 'dark';
  }

  function normalizeColor(value) {
    const color = String(value || '').trim();
    return COLOR_RE.test(color) ? color.toLowerCase() : '';
  }

  function currentColor() {
    return normalizeColor(
      html.style.getPropertyValue('--qf-user-accent')
      || html.style.getPropertyValue('--primary')
      || colorInput?.value);
  }

  function modeDefault(mode) {
    return THEME_DEFAULTS[normalizeMode(mode)];
  }

  const initialMode = normalizeMode(html.dataset.theme);
  const initialColor = currentColor();
  const initialColorIsCustom = colorIsCustom;

  function accentForeground(color) {
    const darkRatio = contrastRatio(color, '#101214');
    const lightRatio = contrastRatio(color, '#ffffff');
    if (darkRatio >= lightRatio && darkRatio >= 4.5) return '#101214';
    if (lightRatio >= 4.5) return '#ffffff';
    return contrastRatio(color, '#000000') >= lightRatio ? '#000000' : '#ffffff';
  }

  function relativeLuminance(color) {
    const channels = [1, 3, 5].map(index => parseInt(color.slice(index, index + 2), 16) / 255);
    const linear = channels.map(value => value <= 0.03928
      ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
  }

  function contrastRatio(foreground, background) {
    const first = relativeLuminance(foreground);
    const second = relativeLuminance(background);
    const lighter = Math.max(first, second);
    const darker = Math.min(first, second);
    return (lighter + 0.05) / (darker + 0.05);
  }

  function accentTextColor(color, mode) {
    color = normalizeColor(color) || modeDefault(mode);
    mode = normalizeMode(mode);
    const backgrounds = mode === 'light'
      ? ['#f5f6f8', '#ffffff', '#f0f1f3']
      : ['#202124', '#292a2d', '#323338'];
    if (backgrounds.every(background => contrastRatio(color, background) >= 4.5)) {
      return color;
    }
    const target = mode === 'light' ? [0, 0, 0] : [255, 255, 255];
    const source = [1, 3, 5].map(index => parseInt(color.slice(index, index + 2), 16));
    for (let step = 1; step <= 100; step += 1) {
      const amount = step / 100;
      const candidate = '#' + source.map((channel, index) =>
        Math.round(channel * (1 - amount) + target[index] * amount)
          .toString(16).padStart(2, '0')).join('');
      if (backgrounds.every(background => contrastRatio(candidate, background) >= 4.5)) {
        return candidate;
      }
    }
    return mode === 'light' ? '#101214' : '#f1f3f4';
  }

  function applyColor(c, {userInitiated = true} = {}) {
    c = normalizeColor(c);
    if (!c) return;
    if (userInitiated) {
      colorIsCustom = true;
      html.dataset.themeColorCustom = '1';
      if (customInput) customInput.value = '1';
    } else {
      colorIsCustom = false;
      html.dataset.themeColorCustom = '0';
      if (customInput) customInput.value = '0';
    }
    // 新旧变量同时更新：旧页面依赖 --primary，Codex 令牌层依赖 --qf-user-accent。
    html.style.setProperty('--qf-user-accent', c);
    html.style.setProperty('--primary', c);
    html.style.setProperty('--qf-user-on-accent', accentForeground(c));
    html.style.setProperty('--qf-user-accent-text', accentTextColor(c, html.dataset.theme));
    swatches.forEach(s =>
      s.classList.toggle('active', s.dataset.color.toLowerCase() === c.toLowerCase()));
    reportTheme();
  }

  function reportTheme() {
    previewActive = true;
    if (window.parent === window) return;
    window.parent.postMessage({
      source: 'quizforge',
      type: 'page-theme',
      preview: true,
      mode: normalizeMode(html.dataset.theme),
      color: currentColor(),
      textColor: accentTextColor(currentColor(), html.dataset.theme),
    }, window.location.origin);
  }

  function resetPreview() {
    if (!previewActive) return;
    previewActive = false;
    // pagehide 可能进入 bfcache，先把当前文档也恢复，避免返回设置页时
    // 把未提交的颜色和 custom 标记带回来。
    html.setAttribute('data-theme', initialMode);
    colorIsCustom = initialColorIsCustom;
    html.dataset.themeColorCustom = colorIsCustom ? '1' : '0';
    if (customInput) customInput.value = colorIsCustom ? '1' : '0';
    if (colorInput) colorInput.value = initialColor || modeDefault(initialMode);
    if (initialColorIsCustom && initialColor) {
      html.style.setProperty('--qf-user-accent', initialColor);
      html.style.setProperty('--primary', initialColor);
      html.style.setProperty('--qf-user-on-accent', accentForeground(initialColor));
      html.style.setProperty('--qf-user-accent-text', accentTextColor(initialColor, initialMode));
    } else {
      html.style.removeProperty('--qf-user-accent');
      html.style.removeProperty('--primary');
      html.style.removeProperty('--qf-user-on-accent');
      html.style.removeProperty('--qf-user-accent-text');
    }
    swatches.forEach(s =>
      s.classList.toggle('active', initialColor
        && s.dataset.color.toLowerCase() === initialColor.toLowerCase()));
    if (window.parent === window) return;
    window.parent.postMessage({
      source: 'quizforge',
      type: 'page-theme-reset',
      preview: true,
    }, window.location.origin);
  }

  swatches.forEach(s => s.addEventListener('click', () => {
    if (colorInput) colorInput.value = s.dataset.color;
    applyColor(s.dataset.color);
  }));
  if (colorInput) {
    colorInput.addEventListener('input', () => applyColor(colorInput.value));
  }

  document.querySelectorAll('input[name="theme_mode"]').forEach(r => {
    r.addEventListener('change', () => {
      if (r.checked) {
        const previousMode = normalizeMode(html.dataset.theme);
        const nextMode = normalizeMode(r.value);
        // 用户没有改过颜色时，随主题切换使用对应的默认强调色；自定义色保持不变。
        const selectedColor = currentColor();
        html.setAttribute('data-theme', nextMode);
        // 旧页面没有 data-theme-color-custom 时，用颜色值作兼容推断。
        const inferredCustom = selectedColor
          && selectedColor !== modeDefault(previousMode);
        if (!colorIsCustom && !inferredCustom
            && nextMode !== previousMode) {
          if (colorInput) colorInput.value = modeDefault(nextMode);
          applyColor(modeDefault(nextMode), {userInitiated: false});
          return;
        }
        if (selectedColor) {
          html.style.setProperty('--qf-user-accent-text', accentTextColor(selectedColor, nextMode));
        }
        reportTheme();
      }
    });
  });

  // 设置页在常驻桌面外壳中是 iframe。预览只应在页面存活期间有效；
  // 提交后页面会重新加载，新的 app-shell 消息再把已保存值写回外壳。
  window.addEventListener('pagehide', resetPreview);
  window.addEventListener('beforeunload', resetPreview);
})();
