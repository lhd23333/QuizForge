// 设置分类切换与 MiKTeX 安装状态。分类只保存名称，不包含账号或密钥信息。
(function () {
  'use strict';

  const page = document.querySelector('.settings-page');
  if (!page) return;

  const tabs = [...page.querySelectorAll('[data-settings-tab]')];
  const panels = [...page.querySelectorAll('[data-settings-panel]')];
  const valid = new Set(panels.map(panel => panel.dataset.settingsPanel));
  const legacyHashes = {account: 'local', software_license: 'local', 'software-license': 'local', 'tex-environment': 'export'};

  function hashSection() {
    const hash = window.location.hash.slice(1);
    return valid.has(hash) ? hash : legacyHashes[hash];
  }

  function showSection(section, remember) {
    const next = valid.has(section) ? section : 'local';
    tabs.forEach(tab => {
      const active = tab.dataset.settingsTab === next;
      tab.classList.toggle('is-active', active);
      tab.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
    panels.forEach(panel => { panel.hidden = panel.dataset.settingsPanel !== next; });
    if (remember) {
      try { localStorage.setItem('quizforge.settings.section', next); } catch (_) { /* 忽略禁用存储。 */ }
      history.replaceState(history.state, '', '#' + next);
    }
  }

  let initial = hashSection();
  if (!initial) {
    try { initial = localStorage.getItem('quizforge.settings.section'); } catch (_) { /* 使用本地页。 */ }
  }
  showSection(initial || 'local', false);
  tabs.forEach(tab => tab.addEventListener('click', () => showSection(tab.dataset.settingsTab, true)));
  window.addEventListener('hashchange', () => showSection(hashSection() || 'local', false));

  const modelForm = page.querySelector('.model-add-form');
  const presetSource = modelForm?.querySelector('[data-llm-presets]');
  const presetSelect = modelForm?.querySelector('[data-llm-preset]');
  const modelInput = modelForm?.elements.namedItem('llm_model');
  const modelOptions = modelForm?.querySelector('#llm-model-options');
  const presetHint = modelForm?.querySelector('[data-llm-preset-hint]');
  let modelPresets = [];
  try { modelPresets = JSON.parse(presetSource?.textContent || '[]'); } catch (_) { modelPresets = []; }

  function selectedPreset() {
    return modelPresets.find(item => item.id === presetSelect?.value) || null;
  }

  function updateModelHint(preset) {
    if (!presetHint) return;
    const model = preset?.models?.find(item => item.id === modelInput?.value);
    if (!model) {
      presetHint.textContent = preset
        ? '可直接输入该服务商支持的其他模型；上下文窗口请以服务商控制台为准。'
        : '选择预设可自动填写地址和推荐输出；上下文窗口由模型决定。';
      return;
    }
    const output = Number(model.recommended_max_tokens || 0).toLocaleString('en-US');
    presetHint.textContent = `上下文 ${model.context_label} · 推荐最大输出 ${output} tokens`;
    const maxTokens = modelForm.elements.namedItem('llm_max_tokens');
    if (maxTokens) maxTokens.value = String(model.recommended_max_tokens);
    const redraw = modelForm.elements.namedItem('llm_for_redraw');
    if (redraw) redraw.checked = Boolean(model.supports_vision);
  }

  if (modelForm && presetSelect && modelInput && modelOptions) {
    presetSelect.addEventListener('change', () => {
      const preset = selectedPreset();
      modelOptions.replaceChildren();
      if (!preset) {
        updateModelHint(null);
        return;
      }
      const name = modelForm.elements.namedItem('llm_name');
      const baseUrl = modelForm.elements.namedItem('llm_base_url');
      if (name) name.value = preset.name;
      if (baseUrl) baseUrl.value = preset.base_url;
      preset.models.forEach(model => {
        const option = document.createElement('option');
        option.value = model.id;
        option.label = model.label;
        modelOptions.append(option);
      });
      modelInput.value = preset.models[0]?.id || '';
      updateModelHint(preset);
    });
    modelInput.addEventListener('input', () => updateModelHint(selectedPreset()));
  }

  const button = document.getElementById('tex-install-button');
  const progress = document.getElementById('tex-install-progress');
  const message = document.getElementById('tex-install-message');
  const error = document.getElementById('tex-install-error');
  if (!button || !progress || !message || !error) return;

  const active = new Set(['queued', 'downloading', 'verifying', 'installing']);
  let state = {};
  let timer = null;
  try { state = JSON.parse(page.dataset.texState || '{}'); } catch (_) { state = {}; }

  function render(next) {
    state = next || {};
    const running = active.has(state.status);
    const downloaded = Number(state.downloaded || 0);
    const total = Number(state.total || 1);
    progress.max = Math.max(total, 1);
    progress.value = Math.min(downloaded, total);
    progress.hidden = !running;
    const available = state.available !== false;
    message.textContent = state.message || (state.installed ? '本机 TeX 已可用' : (state.blocked_reason || '尚未安装'));
    error.textContent = state.error || '';
    error.hidden = !state.error;
    button.disabled = !available || running || Boolean(state.installed);
    button.textContent = state.installed ? '已安装' : (!available ? '暂不可用' : (running ? '安装中' : '安装 MiKTeX'));
    if (running && timer === null) timer = window.setTimeout(poll, 1000);
  }

  async function poll() {
    timer = null;
    try {
      const response = await fetch(page.dataset.texStatusUrl, {cache: 'no-store'});
      const data = await response.json();
      render(data.install || {});
    } catch (_) {
      error.textContent = '无法读取安装进度';
      error.hidden = false;
      if (active.has(state.status)) timer = window.setTimeout(poll, 2000);
    }
  }

  button.addEventListener('click', async () => {
    if (!window.confirm('为当前 Windows 用户安装 MiKTeX ' + state.version + '？')) return;
    button.disabled = true;
    error.hidden = true;
    try {
      const token = document.querySelector('meta[name="csrf-token"]')?.content || '';
      const response = await fetch(page.dataset.texInstallUrl, {
        method: 'POST',
        headers: {'X-CSRF-Token': token},
      });
      const data = await response.json();
      render(data.install || {});
      if (!response.ok) throw new Error(data.error || '无法开始安装');
    } catch (installError) {
      error.textContent = installError.message || '无法开始安装';
      error.hidden = false;
      button.disabled = state.available === false || Boolean(state.installed);
    }
  });

  render(state);
})();
