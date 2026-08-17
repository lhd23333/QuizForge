(function () {
  'use strict';
  document.getElementById('q-sel-all')?.addEventListener('click', () => {
    document.querySelectorAll('#restore-form input[name="qid"]').forEach(box => { box.checked = true; });
  });
  document.getElementById('q-sel-none')?.addEventListener('click', () => {
    document.querySelectorAll('#restore-form input[name="qid"]').forEach(box => { box.checked = false; });
  });
})();
