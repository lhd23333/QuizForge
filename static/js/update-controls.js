// 设置页和关于页共用的更新控件；只有用户点击后才访问更新清单。
(function () {
  'use strict';

  document.querySelectorAll('[data-update-control]').forEach(control => {
    const button = control.querySelector('[data-update-action]');
    const status = control.querySelector('[data-update-status]');
    const progress = control.querySelector('[data-update-progress]');
    const download = control.querySelector('[data-update-download]');
    if (!button || !status) return;

    let manifest = null;
    let polling = null;
    const desktopApi = () => window.QuizForgeDesktop?.api?.() || null;
    const nativeUpdater = () => Boolean(desktopApi()?.start_update);

    function setBusy(busy) {
      button.disabled = busy;
    }

    function showDownload(url, visible) {
      if (!download) return;
      download.hidden = !visible;
      if (visible) download.href = url;
      else download.removeAttribute('href');
    }

    function renderUpdate(next) {
      const state = next || {};
      const active = ['queued', 'checking', 'downloading', 'verified', 'exiting'].includes(state.status);
      status.textContent = state.message || '正在更新';
      setBusy(active);
      if (progress) {
        const total = Number(state.total || 0);
        progress.max = Math.max(total, 1);
        progress.value = Math.min(Number(state.downloaded || 0), progress.max);
        progress.hidden = state.status !== 'downloading';
      }
      if (state.status === 'failed') {
        button.disabled = false;
        button.textContent = '重试更新';
      }
      if (active && state.status !== 'exiting') {
        polling = window.setTimeout(poll, 700);
      }
    }

    async function poll() {
      polling = null;
      try {
        const api = desktopApi();
        if (!api?.update_status) throw new Error('桌面更新接口尚未就绪');
        const result = await api.update_status();
        renderUpdate(result.update || {});
      } catch (error) {
        status.textContent = error.message || '无法读取更新进度';
        button.disabled = false;
      }
    }

    async function check() {
      setBusy(true);
      button.textContent = '检查中';
      status.textContent = '正在检查更新';
      showDownload('', false);
      try {
        const response = await fetch('/api/update/check', {
          headers: {Accept: 'application/json'}, credentials: 'same-origin', cache: 'no-store',
        });
        const result = await response.json();
        if (!response.ok || !result.ok) throw new Error(result.error || '更新清单不可用');
        manifest = result;
        if (!result.enabled) {
          status.textContent = result.message || '未配置更新地址';
          button.textContent = '重新检查';
        } else if (!result.available) {
          status.textContent = result.message || '当前已是最新版本';
          button.textContent = '重新检查';
        } else if (!result.installable) {
          status.textContent = `发现 ${result.latest_version}，更新清单尚未完成签名配置`;
          button.textContent = '重新检查';
        } else {
          status.textContent = `发现 ${result.latest_version}`;
          button.textContent = nativeUpdater() ? '一键更新' : '检查完成';
          showDownload(result.download_url, !nativeUpdater());
        }
      } catch (error) {
        manifest = null;
        status.textContent = error.message || '检查更新失败';
        button.textContent = '重试检查';
      } finally {
        setBusy(false);
      }
    }

    async function install() {
      if (!nativeUpdater()) {
        showDownload(manifest?.download_url || '', Boolean(manifest?.download_url));
        return;
      }
      if (!window.confirm(`更新到 QuizForge ${manifest.latest_version}？软件会自动关闭并重新启动。`)) return;
      setBusy(true);
      showDownload('', false);
      try {
        const api = desktopApi();
        if (!api?.start_update) throw new Error('桌面更新接口尚未就绪');
        const result = await api.start_update();
        if (!result.ok) throw new Error(result.error || '无法开始更新');
        renderUpdate(result.update || {});
      } catch (error) {
        status.textContent = error.message || '无法开始更新';
        button.textContent = '重试更新';
        setBusy(false);
      }
    }

    button.addEventListener('click', () => {
      if (manifest?.available && manifest?.installable) install();
      else check();
    });
    window.QuizForgeDesktop?.whenReady(() => {
      if (manifest?.available && manifest?.installable) {
        button.textContent = '一键更新';
        showDownload('', false);
      }
    });
    window.addEventListener('beforeunload', () => {
      if (polling !== null) window.clearTimeout(polling);
    });
  });
})();
