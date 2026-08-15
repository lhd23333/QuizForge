(function () {
  'use strict';

  var button = document.querySelector('[data-copy-license-device]');
  var input = document.getElementById('license-device-id');
  if (!button || !input) return;

  function fallbackCopy() {
    input.focus();
    input.select();
    input.setSelectionRange(0, input.value.length);
    return document.execCommand('copy');
  }

  button.addEventListener('click', async function () {
    var original = button.textContent;
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(input.value);
      } else if (!fallbackCopy()) {
        throw new Error('copy failed');
      }
      button.textContent = '已复制';
    } catch (_error) {
      input.focus();
      input.select();
      button.textContent = '请手动复制';
    }
    window.setTimeout(function () { button.textContent = original; }, 1600);
  });
})();
