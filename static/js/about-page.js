(function () {
  'use strict';
  const actions = document.querySelector('.desktop-only-actions');
  window.QuizForgeDesktop?.whenReady(() => {
    if (actions) actions.hidden = false;
  });
  document.querySelectorAll('[data-desktop-open]').forEach(button => {
    button.addEventListener('click', async () => {
      const kind = button.dataset.desktopOpen;
      const method = kind === 'bank' ? 'open_bank_folder'
        : kind === 'logs' ? 'open_log_folder' : 'open_data_folder';
      try {
        const api = window.QuizForgeDesktop?.api();
        if (!api?.[method]) throw new Error('桌面接口尚未就绪');
        const result = await api[method]();
        if (!result.ok) window.alert(result.error || '打开目录失败');
      } catch (error) {
        window.alert('打开目录失败：' + error.message);
      }
    });
  });
})();
