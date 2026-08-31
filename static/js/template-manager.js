(function () {
  'use strict';

  const root = document.getElementById('template-manager');
  if (!root) return;
  const form = document.getElementById('template-upload-form');
  const list = document.getElementById('template-manager-list');
  const status = document.getElementById('template-manager-status');
  const csrf = () => document.querySelector('meta[name="csrf-token"]')?.content || '';

  function setStatus(message, kind = '') {
    status.textContent = message || '';
    status.dataset.kind = kind;
  }

  async function api(url, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set('X-CSRF-Token', csrf());
    if (options.body && !(options.body instanceof FormData)) {
      headers.set('Content-Type', 'application/json');
    }
    const response = await fetch(url, {...options, headers});
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.ok === false) {
      const error = new Error(data.error || `请求失败（${response.status}）`);
      error.code = data.code || '';
      throw error;
    }
    return data;
  }

  function statusLabel(template) {
    if (template.reference_only) return 'PDF 参考';
    if (template.selected) return '当前默认';
    if (template.enabled) return '已启用';
    const value = template.validation?.status || template.status;
    return ({valid: '验证通过', failed: '验证失败', stale: '源码已变化',
      pending: '待验证', disabled: '已停用'})[value] || '待验证';
  }

  function action(label, className, handler) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = className || 'btn btn-sm';
    button.textContent = label;
    button.addEventListener('click', async () => {
      button.disabled = true;
      try { await handler(); } finally { if (button.isConnected) button.disabled = false; }
    });
    return button;
  }

  async function mutate(template, operation) {
    setStatus('正在处理…');
    try {
      await operation();
      await load();
      setStatus('已更新', 'success');
    } catch (error) {
      setStatus(error.message || '操作失败', 'error');
    }
  }

  function renderTemplate(template) {
    const item = document.createElement('article');
    item.className = 'template-manager-item';
    item.dataset.templateId = template.id;

    const head = document.createElement('div');
    head.className = 'template-manager-item-head';
    const title = document.createElement('div');
    const name = document.createElement('strong');
    name.textContent = template.name || template.source_file || '未命名模板';
    const meta = document.createElement('small');
    meta.textContent = `${String(template.format || '').toUpperCase()} · ${template.version || '1.0.0'}`;
    title.append(name, meta);
    const badge = document.createElement('span');
    badge.className = 'template-status';
    badge.dataset.status = template.reference_only ? 'reference' :
      (template.selected ? 'selected' : (template.enabled ? 'enabled' : (template.validation?.status || template.status || 'pending')));
    badge.textContent = statusLabel(template);
    head.append(title, badge);
    item.appendChild(head);

    const detail = document.createElement('p');
    detail.className = 'template-manager-detail';
    detail.textContent = template.description || template.validation?.message ||
      (template.reference_only ? '只读版式参考' : '等待验证');
    item.appendChild(detail);

    if (template.supported_modes?.length) {
      const modes = document.createElement('div');
      modes.className = 'template-mode-list';
      template.supported_modes.forEach(mode => {
        const token = document.createElement('span');
        token.textContent = mode;
        modes.appendChild(token);
      });
      item.appendChild(modes);
    }

    const actions = document.createElement('div');
    actions.className = 'template-manager-actions';
    if (!template.reference_only && !template.enabled) {
      actions.appendChild(action('验证', 'btn btn-sm', () => mutate(template, () =>
        api(`/api/templates/${encodeURIComponent(template.id)}/validate`, {
          method: 'POST', body: JSON.stringify({}),
        }))));
    }
    if (template.preview_url) {
      const preview = document.createElement('a');
      preview.className = 'btn btn-sm';
      preview.href = template.preview_url;
      preview.target = '_blank';
      preview.rel = 'noopener';
      preview.textContent = '预览';
      actions.appendChild(preview);
    }
    const valid = template.validation?.status === 'valid';
    if (!template.reference_only && valid && !template.enabled) {
      actions.appendChild(action('启用', 'btn btn-sm btn-primary', async () => {
        if (!window.confirm(`确认启用模板“${template.name}”？`)) return;
        await mutate(template, () => api(`/api/templates/${encodeURIComponent(template.id)}/enable`, {
          method: 'POST', body: JSON.stringify({confirm: true}),
        }));
      }));
    }
    if (template.enabled && !template.selected) {
      actions.appendChild(action('设为默认', 'btn btn-sm btn-primary', () => mutate(template, () =>
        api(`/api/templates/${encodeURIComponent(template.id)}/select`, {
          method: 'POST', body: JSON.stringify({}),
        }))));
    }
    if (template.enabled) {
      actions.appendChild(action('停用', 'btn btn-sm', () => mutate(template, () =>
        api(`/api/templates/${encodeURIComponent(template.id)}/disable`, {
          method: 'POST', body: JSON.stringify({}),
        }))));
    }
    actions.appendChild(action('删除', 'btn btn-sm btn-danger-text', async () => {
      if (!window.confirm(`删除模板“${template.name}”？此操作不可恢复。`)) return;
      await mutate(template, () => api(`/api/templates/${encodeURIComponent(template.id)}`, {method: 'DELETE'}));
    }));
    item.appendChild(actions);
    return item;
  }

  async function load() {
    const data = await api('/api/templates');
    list.replaceChildren();
    if (!data.templates?.length) {
      const empty = document.createElement('p');
      empty.className = 'settings-note';
      empty.textContent = '还没有自定义模板。';
      list.appendChild(empty);
      return;
    }
    data.templates.forEach(template => list.appendChild(renderTemplate(template)));
  }

  form.addEventListener('submit', async event => {
    event.preventDefault();
    const submit = form.querySelector('[type="submit"]');
    submit.disabled = true;
    setStatus('正在上传…');
    try {
      await api('/api/templates', {method: 'POST', body: new FormData(form)});
      form.reset();
      document.getElementById('template-upload-version').value = '1.0.0';
      await load();
      setStatus('模板已上传，验证通过前不会用于导出。', 'success');
    } catch (error) {
      setStatus(error.message || '上传失败', 'error');
    } finally {
      submit.disabled = false;
    }
  });

  load().catch(error => setStatus(error.message || '无法读取模板', 'error'));
}());
