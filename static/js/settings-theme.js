/* 设置页「外观主题」的实时预览：点色板/取色器即时改主题色，切模式即时切深浅。
 *
 * 只是预览，持久化仍靠表单提交 —— 所以离开页面不保存的话会恢复原样，这是有意的
 * （点着玩不该留下副作用）。
 *
 * 服务器版这段写在 settings.html 的内联 <script> 里；本项目按前端约定放
 * static/js/ 并用 static_v + defer 引入，JS 里不写 Jinja（色值全部来自
 * DOM 的 data-color / input.value）。
 */
(function () {
  const html = document.documentElement;
  const colorInput = document.getElementById('theme-color-input');
  const swatches = document.querySelectorAll('#color-swatches .swatch');
  if (!colorInput && !swatches.length) return;   // 不是设置页

  function applyColor(c) {
    html.style.setProperty('--primary', c);
    swatches.forEach(s =>
      s.classList.toggle('active', s.dataset.color.toLowerCase() === c.toLowerCase()));
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
      if (r.checked) html.setAttribute('data-theme', r.value);
    });
  });
})();
