// 官方只读提示词与本地自定义提示词管理。
(function () {
  'use strict';

  const page = document.querySelector('.prompts-page');
  if (!page) return;
  const api = page.dataset.promptsApi || '/api/prompts';
  const list = document.getElementById('prompt-list');
  const editor = document.getElementById('prompt-editor');
  const empty = document.getElementById('prompt-empty');
  const title = document.getElementById('prompt-title');
  const category = document.getElementById('prompt-category');
  const content = document.getElementById('prompt-content');
  const save = document.getElementById('prompt-save');
  const copy = document.getElementById('prompt-copy');
  const clone = document.getElementById('prompt-clone');
  const remove = document.getElementById('prompt-delete');
  const status = document.getElementById('prompt-status');
  let prompts = [];
  let activeId = '';
  let draft = false;

  function setStatus(message, error) {
    status.textContent = message || '';
    status.classList.toggle('is-error', !!error);
  }

  async function request(url, options) {
    const response = await fetch(url, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.ok) {
      throw new Error(data.error || `请求失败（${response.status}）`);
    }
    return data;
  }

  function current() {
    return prompts.find(item => item.id === activeId) || null;
  }

  function renderList() {
    const grouped = new Map();
    prompts.forEach(item => {
      const key = item.category || '自定义';
      if (!grouped.has(key)) grouped.set(key, []);
      grouped.get(key).push(item);
    });
    list.replaceChildren();
    grouped.forEach((items, group) => {
      const label = document.createElement('div');
      label.className = 'muted';
      label.textContent = group;
      list.appendChild(label);
      items.forEach(item => {
        const button = document.createElement('button');
        button.type = 'button';
        button.dataset.promptId = item.id;
        button.classList.toggle('is-active', item.id === activeId && !draft);
        const name = document.createElement('span');
        name.textContent = item.title;
        button.appendChild(name);
        button.addEventListener('click', () => select(item.id));
        list.appendChild(button);
      });
    });
  }

  function setReadonly(readonly) {
    title.readOnly = readonly;
    category.readOnly = readonly;
    content.readOnly = readonly;
    save.hidden = readonly;
    remove.hidden = readonly || draft;
    clone.hidden = !readonly;
  }

  function select(id) {
    const item = prompts.find(row => row.id === id);
    if (!item) return;
    activeId = item.id;
    draft = false;
    title.value = item.title;
    category.value = item.category;
    content.value = item.content;
    setReadonly(!!item.readonly);
    editor.hidden = false;
    empty.hidden = true;
    setStatus(item.readonly ? '官方提示词' : '保存在本机');
    renderList();
  }

  function newDraft(source) {
    activeId = '';
    draft = true;
    title.value = source ? `${source.title}（副本）` : '';
    category.value = source ? source.category : '自定义';
    content.value = source ? source.content : '';
    setReadonly(false);
    remove.hidden = true;
    clone.hidden = true;
    editor.hidden = false;
    empty.hidden = true;
    setStatus('尚未保存');
    renderList();
    title.focus();
  }

  async function copyText() {
    const value = content.value;
    try {
      await navigator.clipboard.writeText(value);
    } catch (_error) {
      content.focus();
      content.select();
      if (!document.execCommand('copy')) throw new Error('浏览器拒绝访问剪贴板');
    }
    setStatus('已复制');
  }

  async function saveCurrent() {
    const payload = {
      title: title.value.trim(),
      category: category.value.trim(),
      content: content.value,
    };
    save.disabled = true;
    setStatus('保存中…');
    try {
      if (draft) {
        const data = await request(api, {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(payload),
        });
        prompts.push(data.prompt);
        activeId = data.prompt.id;
        draft = false;
      } else {
        const data = await request(`${api}/${encodeURIComponent(activeId)}`, {
          method: 'PATCH', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(payload),
        });
        const index = prompts.findIndex(item => item.id === activeId);
        if (index >= 0) prompts[index] = data.prompt;
      }
      select(activeId);
      setStatus('已保存');
    } catch (error) {
      setStatus(error.message, true);
    } finally {
      save.disabled = false;
    }
  }

  async function deleteCurrent() {
    const item = current();
    if (!item || item.readonly || !confirm(`删除提示词“${item.title}”？`)) return;
    remove.disabled = true;
    try {
      await request(`${api}/${encodeURIComponent(item.id)}`, {method: 'DELETE'});
      prompts = prompts.filter(row => row.id !== item.id);
      activeId = '';
      if (prompts.length) select(prompts[0].id);
      else newDraft();
    } catch (error) {
      setStatus(error.message, true);
    } finally {
      remove.disabled = false;
    }
  }

  async function cloneCurrent() {
    const item = current();
    if (!item) return;
    clone.disabled = true;
    setStatus('复制中…');
    try {
      const data = await request(api, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          title: `${item.title}（副本）`, category: item.category, content: item.content,
        }),
      });
      prompts.push(data.prompt);
      select(data.prompt.id);
      setStatus('已复制为自定义提示词');
      title.focus();
      title.select();
    } catch (error) {
      setStatus(error.message, true);
    } finally {
      clone.disabled = false;
    }
  }

  document.getElementById('prompt-new').addEventListener('click', () => newDraft());
  save.addEventListener('click', saveCurrent);
  copy.addEventListener('click', () => copyText().catch(error => setStatus(error.message, true)));
  clone.addEventListener('click', cloneCurrent);
  remove.addEventListener('click', deleteCurrent);

  request(api).then(data => {
    prompts = data.prompts || [];
    if (prompts.length) select(prompts[0].id);
    else newDraft();
  }).catch(error => {
    empty.querySelector('p').textContent = error.message;
    empty.querySelector('p').classList.add('is-error');
  });
})();
