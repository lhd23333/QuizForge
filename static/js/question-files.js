// 题库内文件工作区：文件树中的 PDF/Markdown 在题库页内打开，不再跳转资料库。
(function () {
  'use strict';

  const workspace = document.getElementById('question-file-workspace');
  if (!workspace) return;
  const initialTabsHost = workspace.querySelector('.question-file-tabs');
  const initialPanesHost = workspace.querySelector('.question-file-panes');
  if (!initialTabsHost || !initialPanesHost) return;
  if (workspace.dataset.questionFilesReady === '1') return;
  workspace.dataset.questionFilesReady = '1';

  const state = {
    groups: new Map(),
    groupSerial: 1,
    tabSerial: 0,
    focusedGroupId: 'primary',
    layout: 'single',
    maxGroups: 4,
  };

  function csrfToken() {
    return document.querySelector('meta[name="csrf-token"]')?.content || '';
  }

  function normalizePath(path) {
    return String(path || '').replace(/\\/g, '/').replace(/^\/+/, '')
      .split('/')
      .filter(part => part && part !== '.' && part !== '..')
      .join('/');
  }

  function pathName(path) {
    return String(path || '').split('/').pop() || String(path || '');
  }

  function stemName(path) {
    return pathName(path).replace(/\.[^.]+$/, '');
  }

  function kindFrom(meta) {
    const kind = String(meta?.kind || '').toLowerCase();
    if (kind === 'pdf' || /\.pdf$/i.test(meta?.path || '')) return 'pdf';
    return 'markdown';
  }

  function makeButton(label, className, title) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = className;
    button.textContent = label;
    button.title = title || label;
    button.setAttribute('aria-label', button.title);
    return button;
  }

  function showWorkspace(show) {
    workspace.hidden = !show;
  }

  function injectWorkspaceStyles() {
    if (document.querySelector('style[data-question-file-workspace]')) return;
    const style = document.createElement('style');
    style.dataset.questionFileWorkspace = '1';
    style.textContent = `
      .question-file-workspace.qf-group-workspace { grid-template-rows: auto minmax(260px, 1fr); }
      .qf-workspace-controls { display:flex; align-items:center; gap:6px; flex-wrap:wrap;
        padding:5px 7px; border-bottom:1px solid var(--border); background:var(--surface-2); }
      .qf-workspace-controls .qf-layout-label { margin-right:auto; color:var(--muted); font-size:11px; }
      .qf-workspace-controls button, .qf-group-head button { min-height:27px; padding:3px 8px;
        border:1px solid var(--border); border-radius:4px; background:transparent; color:var(--text-2); cursor:pointer; }
      .qf-workspace-controls button:hover, .qf-workspace-controls button.is-active,
      .qf-group-head button:hover { border-color:var(--primary); background:var(--primary-soft); color:var(--primary); }
      .question-file-groups { display:grid; min-height:0; min-width:0; gap:1px; background:var(--border); }
      .question-file-groups.is-single { grid-template-columns:minmax(0,1fr); grid-template-rows:minmax(0,1fr); }
      .question-file-groups.is-vertical { grid-template-columns:repeat(var(--qf-group-count), minmax(0,1fr)); grid-template-rows:minmax(0,1fr); }
      .question-file-groups.is-horizontal { grid-template-columns:minmax(0,1fr); grid-template-rows:repeat(var(--qf-group-count), minmax(0,1fr)); }
      .qf-editor-group { display:grid; grid-template-rows:auto auto minmax(0,1fr); min-width:0; min-height:0; background:var(--surface); }
      .qf-editor-group.is-focused { box-shadow:inset 0 0 0 1px var(--primary); }
      .qf-group-head { display:flex; align-items:center; gap:6px; min-height:29px; padding:3px 7px;
        border-bottom:1px solid var(--border); background:var(--surface-2); }
      .qf-group-head strong { min-width:0; overflow:hidden; color:var(--muted); font-size:11px; font-weight:600; text-overflow:ellipsis; white-space:nowrap; }
      .qf-group-head .qf-group-spacer { flex:1; }
      .qf-group-head .qf-group-close { width:26px; padding-inline:0; }
      .qf-group-tabs { min-width:0; }
      .qf-group-panes { min-height:0; min-width:0; }
      .qf-group-empty { display:grid; place-items:center; height:100%; min-height:100px; color:var(--muted); font-size:12px; }
      .question-file-tab { max-width:100%; }
      .question-file-tab-open { min-width:0; }
      .question-file-tab-move { width:24px; min-height:28px; padding:0; border:0; background:transparent; color:var(--muted); cursor:pointer; }
      .question-file-tab-move:hover { color:var(--primary); background:var(--primary-soft); }
      .qf-dialog-grid { display:grid; gap:9px; }
      .question-file-dialog { overflow:auto; }
      .qf-dialog-field { display:grid; gap:4px; color:var(--text-2); font-size:12px; }
      .qf-dialog-field > span { font-weight:600; }
      .qf-dialog-field .input, .qf-dialog-field select, .qf-dialog-field textarea { width:100%; box-sizing:border-box; }
      .qf-dialog-check { display:flex; align-items:center; justify-content:space-between; gap:12px; color:var(--text-2); font-size:12px; }
      .qf-dialog-check input { flex:none; }
      .qf-dialog-footer { display:flex; align-items:center; justify-content:flex-end; gap:8px; margin-top:3px; }
      .qf-dialog-status { min-height:20px; margin-right:auto; color:var(--muted); font-size:12px; }
      .qf-dialog-status.is-error { color:var(--danger); }
      .qf-dialog-head { display:flex; align-items:center; gap:8px; margin-bottom:4px; }
      .qf-dialog-head h2 { margin:0; font-size:16px; }
      .qf-dialog-head .qf-dialog-close { margin-left:auto; }
      @media (max-width:640px) {
        .question-file-groups.is-vertical { grid-template-columns:minmax(0,1fr); grid-template-rows:repeat(var(--qf-group-count), minmax(180px,1fr)); }
        .question-file-groups.is-horizontal { grid-template-rows:repeat(var(--qf-group-count), minmax(180px,1fr)); }
      }
    `;
    document.head.append(style);
  }

  function fetchJson(url, options) {
    return fetch(url, options).then(async response => {
      const data = await response.json().catch(() => ({}));
      if (!response.ok || !data.ok) {
        const error = new Error(data.error || `请求失败（${response.status}）`);
        error.status = response.status;
        error.data = data;
        throw error;
      }
      return data;
    });
  }

  function showToast(message, isError = false) {
    if (!message) return;
    document.querySelector('.toast.toast-live')?.remove();
    const toast = document.createElement('div');
    toast.className = `toast toast-live${isError ? ' toast-error' : ''}`;
    toast.textContent = message;
    document.body.append(toast);
    requestAnimationFrame(() => toast.classList.add('show'));
    window.setTimeout(() => {
      toast.classList.remove('show');
      window.setTimeout(() => toast.remove(), 180);
    }, 2200);
  }

  function closeDialog(dialog) {
    if (!dialog) return;
    if (dialog.open) dialog.close();
    dialog.remove();
  }

  function openDialog(dialog) {
    document.body.append(dialog);
    if (typeof dialog.showModal === 'function') dialog.showModal();
    else dialog.setAttribute('open', '');
  }

  function dialogField(labelText, type = 'text', className = 'input') {
    const label = document.createElement('label');
    label.className = 'qf-dialog-field';
    const caption = document.createElement('span');
    caption.textContent = labelText;
    const input = document.createElement(type === 'textarea' ? 'textarea' : type === 'select' ? 'select' : 'input');
    if (type !== 'textarea' && type !== 'select') input.type = type;
    input.className = className;
    label.append(caption, input);
    return {label, input};
  }

  function appendOptions(select, options) {
    options.forEach(([value, label]) => {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = label;
      select.append(option);
    });
  }

  async function loadCollections(select, status) {
    try {
      const data = await fetchJson('/collections/options');
      if (!select.isConnected) return;
      (data.collections || []).forEach(row => {
        if (!row?.id || row.id === '临时卡片') return;
        const option = document.createElement('option');
        option.value = row.id;
        option.textContent = `${'　'.repeat(Math.max(0, Number(row.depth) || 0))}${row.name}`;
        select.append(option);
      });
    } catch (error) {
      if (select.isConnected && !status.textContent) {
        status.textContent = error.message || '题集列表加载失败';
        status.classList.add('is-error');
      }
    }
  }

  function parsePageNumbers(raw) {
    const values = String(raw || '').split(/[,，\s]+/).filter(Boolean).map(Number);
    return values.length && values.every(value => Number.isInteger(value) && value > 0)
      ? values : null;
  }

  // 资料库工具约定 ranges 为 [[起始页,结束页], ...]，不能改成 pages。
  function parseRanges(raw) {
    const ranges = String(raw || '').split(/[,，]+/).map(value => value.trim())
      .filter(Boolean).map(value => value.split(/[-~—–]/).map(item => Number(item.trim())));
    if (!ranges.length || ranges.some(item => item.length !== 2
        || item.some(value => !Number.isInteger(value) || value < 1 || value > 100000)
        || item[0] > item[1])) return null;
    return ranges;
  }

  function focusedGroup() {
    return state.groups.get(state.focusedGroupId) || state.groups.get('primary');
  }

  function allTabs() {
    const result = [];
    state.groups.forEach(group => group.order.forEach(key => {
      const tab = group.tabs.get(key);
      if (tab) result.push(tab);
    }));
    return result;
  }

  function tabForPath(path) {
    const value = normalizePath(path);
    return allTabs().find(tab => tab.path === value) || null;
  }

  function groupForTab(tab) {
    return tab ? state.groups.get(tab.groupId) || null : null;
  }

  function createGroup(id, tabsHost = null, panesHost = null) {
    const root = document.createElement('section');
    root.className = 'qf-editor-group';
    root.dataset.qfGroup = id;

    const head = document.createElement('div');
    head.className = 'qf-group-head';
    const title = document.createElement('strong');
    title.textContent = id === 'primary' ? '编辑器 1' : `编辑器 ${state.groups.size + 1}`;
    title.dataset.qfGroupTitle = id;
    const spacer = document.createElement('span');
    spacer.className = 'qf-group-spacer';
    const split = makeButton('分栏', 'qf-group-split', '在当前编辑器旁边新建分栏');
    split.dataset.qfGroupSplit = id;
    const close = makeButton('×', 'qf-group-close', '关闭此编辑器分组');
    close.dataset.qfGroupClose = id;
    close.hidden = id === 'primary';
    head.append(title, spacer, split, close);

    const groupTabsHost = tabsHost || document.createElement('div');
    groupTabsHost.classList.add('question-file-tabs', 'qf-group-tabs');
    groupTabsHost.dataset.qfGroupTabs = id;
    const groupPanesHost = panesHost || document.createElement('div');
    groupPanesHost.classList.add('question-file-panes', 'qf-group-panes');
    groupPanesHost.dataset.qfGroupPanes = id;

    const group = {
      id,
      root,
      title,
      tabsHost: groupTabsHost,
      panesHost: groupPanesHost,
      tabs: new Map(),
      order: [],
      activeKey: '',
      previewKey: '',
    };
    root.append(head, groupTabsHost, groupPanesHost);
    state.groups.set(id, group);
    root.addEventListener('pointerdown', () => focusGroup(id));
    return group;
  }

  function setupShell() {
    injectWorkspaceStyles();
    const previousChildren = [...workspace.children].filter(child =>
      child !== initialTabsHost && child !== initialPanesHost);
    const controls = document.createElement('div');
    controls.className = 'qf-workspace-controls';
    const label = document.createElement('span');
    label.className = 'qf-layout-label';
    label.textContent = '文件编辑器';
    controls.append(label);
    [['single', '单栏'], ['vertical', '左右分栏'], ['horizontal', '上下分栏']]
      .forEach(([value, text]) => {
        const button = makeButton(text, 'qf-layout-button', `${text}布局`);
        button.dataset.qfLayout = value;
        controls.append(button);
      });
    const add = makeButton('＋ 分栏', 'qf-add-group', '新建编辑器分组');
    add.dataset.qfAddGroup = '1';
    controls.append(add);

    const groupsHost = document.createElement('div');
    groupsHost.className = 'question-file-groups is-single';
    groupsHost.dataset.qfGroups = '1';
    workspace.classList.add('qf-group-workspace');
    workspace.replaceChildren(controls, groupsHost);
    const primary = createGroup('primary', initialTabsHost, initialPanesHost);
    groupsHost.append(primary.root);
    previousChildren.forEach(child => primary.root.append(child));
    state.groupsHost = groupsHost;
    state.controls = controls;
    return primary;
  }

  function focusGroup(id) {
    if (!state.groups.has(id)) return;
    state.focusedGroupId = id;
    state.groups.forEach(group => group.root.classList.toggle('is-focused', group.id === id));
  }

  function renderLayout() {
    const groupsHost = state.groupsHost;
    if (!groupsHost) return;
    groupsHost.className = `question-file-groups is-${state.layout}`;
    groupsHost.style.setProperty('--qf-group-count', String(Math.max(1, state.groups.size)));
    state.controls?.querySelectorAll('[data-qf-layout]').forEach(button => {
      const active = button.dataset.qfLayout === state.layout;
      button.classList.toggle('is-active', active);
      button.setAttribute('aria-pressed', String(active));
    });
    state.groups.forEach(group => {
      const close = group.root.querySelector('[data-qf-group-close]');
      if (close) close.hidden = group.id === 'primary' || state.groups.size < 2;
    });
  }

  function renderGroup(group) {
    if (!group) return;
    group.tabsHost.replaceChildren();
    group.order.forEach(key => {
      const tab = group.tabs.get(key);
      if (!tab) return;
      const item = document.createElement('div');
      item.className = `question-file-tab${key === group.activeKey ? ' is-active' : ''}`;
      item.dataset.fileTab = key;
      item.dataset.qfGroup = group.id;
      item.setAttribute('role', 'tab');
      item.setAttribute('aria-selected', String(key === group.activeKey));
      const label = `${tab.dirty ? '* ' : ''}${tab.name || pathName(tab.path)}`;
      const open = makeButton(label, 'question-file-tab-open', tab.path);
      open.dataset.fileTabOpen = key;
      const pin = makeButton(tab.pinned ? '●' : '○', 'question-file-tab-pin',
        tab.pinned ? '取消固定标签' : '固定临时标签');
      pin.dataset.fileTabPin = key;
      const move = makeButton('⇄', 'question-file-tab-move', '移动到另一编辑器分组');
      move.dataset.fileTabMove = key;
      move.hidden = state.groups.size < 2;
      const close = makeButton('×', 'question-file-tab-close', '关闭标签');
      close.dataset.fileTabClose = key;
      item.append(open, pin, move, close);
      group.tabsHost.append(item);
    });
    group.panesHost.querySelectorAll('[data-file-panel]').forEach(panel => {
      panel.classList.toggle('is-active', panel.dataset.filePanel === group.activeKey);
      panel.hidden = panel.dataset.filePanel !== group.activeKey;
    });
    let empty = group.panesHost.querySelector(':scope > .qf-group-empty');
    if (!group.order.length) {
      if (!empty) {
        empty = document.createElement('div');
        empty.className = 'qf-group-empty';
        empty.textContent = '从文件树选择 PDF 或 Markdown';
        group.panesHost.append(empty);
      }
    } else {
      empty?.remove();
    }
    group.root.classList.toggle('is-focused', group.id === state.focusedGroupId);
  }

  function renderAll() {
    state.groups.forEach(renderGroup);
    renderLayout();
    showWorkspace(allTabs().length > 0 || state.groups.size > 1);
  }

  function findOtherGroup(group) {
    return [...state.groups.values()].find(item => item !== group) || null;
  }

  function migrateTab(tab, source, destination, activate = false) {
    if (!tab || !source || !destination || source === destination) return;
    source.tabs.delete(tab.key);
    source.order = source.order.filter(key => key !== tab.key);
    if (source.activeKey === tab.key) source.activeKey = source.order[source.order.length - 1] || '';
    if (source.previewKey === tab.key) source.previewKey = '';
    tab.groupId = destination.id;
    tab.group = destination;
    destination.tabs.set(tab.key, tab);
    destination.order.push(tab.key);
    destination.panesHost.append(tab.panel);
    if (!tab.pinned && destination.previewKey && destination.previewKey !== tab.key) {
      // 合并分组时目标栏只能保留一个临时预览；其余预览自动固定，避免丢失未保存内容。
      tab.pinned = true;
    }
    if (!tab.pinned) destination.previewKey = tab.key;
    if (activate || !destination.activeKey) destination.activeKey = tab.key;
    renderGroup(source);
    renderGroup(destination);
  }

  function removeGroup(group) {
    if (!group || group.id === 'primary' || !state.groups.has(group.id)) return;
    const primary = state.groups.get('primary');
    [...group.order].forEach(key => migrateTab(group.tabs.get(key), group, primary, false));
    group.root.remove();
    state.groups.delete(group.id);
    if (state.focusedGroupId === group.id) state.focusedGroupId = 'primary';
    if (state.groups.size === 1) state.layout = 'single';
    renderAll();
    focusGroup(state.focusedGroupId);
  }

  function addGroup(sourceGroup = focusedGroup()) {
    if (state.groups.size >= state.maxGroups) {
      showToast(`最多支持 ${state.maxGroups} 个编辑器分组`, true);
      return null;
    }
    let id;
    do { id = `group-${++state.groupSerial}`; } while (state.groups.has(id));
    const group = createGroup(id);
    state.groupsHost.append(group.root);
    if (state.layout === 'single') state.layout = 'vertical';
    focusGroup(group.id);
    renderAll();
    return group;
  }

  function setLayout(next) {
    const layout = ['single', 'vertical', 'horizontal'].includes(next) ? next : 'single';
    if (layout === 'single' && state.groups.size > 1) {
      const primary = state.groups.get('primary');
      [...state.groups.values()].filter(group => group !== primary)
        .forEach(group => removeGroup(group));
    }
    state.layout = layout === 'single' ? 'single' : layout;
    if (state.layout !== 'single' && state.groups.size < 2) addGroup(focusedGroup());
    renderAll();
  }

  function setStatus(tab, message, error = false) {
    if (!tab?.status) return;
    tab.status.textContent = message || '';
    tab.status.classList.toggle('is-error', error);
  }

  function renderMarkdown(tab) {
    if (!tab?.preview) return;
    const text = String(tab.text ?? '');
    const renderer = window.QTextPreview?.renderRich;
    if (typeof renderer === 'function') {
      tab.preview.innerHTML = renderer(text, {
        basePath: tab.path.includes('/') ? tab.path.slice(0, tab.path.lastIndexOf('/')) : '',
      }) || '';
      window.QMath?.typeset?.(tab.preview);
    } else {
      tab.preview.textContent = text;
    }
  }

  function updateMarkdown(tab) {
    if (!tab?.editor || !tab.preview) return;
    const source = tab.mode === 'source';
    tab.editor.hidden = !source;
    tab.preview.hidden = source;
    tab.modeButtons?.forEach(button => {
      const active = button.dataset.fileMode === tab.mode;
      button.classList.toggle('is-active', active);
      button.setAttribute('aria-pressed', String(active));
    });
    if (!source && tab.text !== undefined) renderMarkdown(tab);
    if (tab.save) {
      tab.save.hidden = !source;
      tab.save.disabled = !tab.dirty || tab.saving || tab.mtime === undefined;
    }
    if (tab.dirty) setStatus(tab, '未保存');
    const group = groupForTab(tab);
    if (group) renderGroup(group);
  }

  async function readMarkdown(tab) {
    const generation = ++tab.generation;
    if (tab.editor) tab.editor.disabled = true;
    setStatus(tab, '正在读取…');
    try {
      const data = await fetchJson(`/api/library/read?path=${encodeURIComponent(tab.path)}`);
      if (generation !== tab.generation || !tab.editor) return;
      tab.text = String(data.text || '');
      tab.savedText = tab.text;
      tab.mtime = data.mtime;
      tab.dirty = false;
      tab.editor.value = tab.text;
      tab.editor.disabled = false;
      setStatus(tab, '');
      updateMarkdown(tab);
    } catch (error) {
      if (generation !== tab.generation || !tab.preview) return;
      setStatus(tab, error.message || '读取 Markdown 失败', true);
      tab.preview.textContent = tab.status?.textContent || '读取 Markdown 失败';
    }
  }

  async function saveMarkdown(tab) {
    if (!tab || tab.kind !== 'markdown' || !tab.dirty || tab.saving) return;
    tab.saving = true;
    updateMarkdown(tab);
    try {
      const data = await fetchJson('/api/library/write', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken()},
        body: JSON.stringify({path: tab.path, text: tab.text, mtime: tab.mtime}),
      });
      tab.mtime = data.mtime;
      tab.savedText = tab.text;
      tab.dirty = false;
      setStatus(tab, '已保存');
      window.QRefreshCurrentCollection?.(false);
      showToast('Markdown 已保存');
    } catch (error) {
      setStatus(tab, error.message || '保存 Markdown 失败', true);
    } finally {
      tab.saving = false;
      updateMarkdown(tab);
    }
  }

  function addToolbar(tab, panel) {
    const toolbar = document.createElement('div');
    toolbar.className = 'question-file-toolbar';
    const path = document.createElement('span');
    path.className = 'question-file-path';
    path.dataset.qfPath = tab.key;
    path.textContent = tab.path;
    path.title = tab.path;
    toolbar.append(path);
    if (tab.kind === 'markdown') {
      const modes = document.createElement('span');
      modes.className = 'question-file-modes';
      tab.modeButtons = ['read', 'source'].map((mode, index) => {
        const button = makeButton(index ? '编辑' : '预览', 'question-file-mode',
          index ? '编辑 Markdown' : '预览 Markdown');
        button.dataset.fileMode = mode;
        button.addEventListener('click', () => {
          tab.mode = mode;
          updateMarkdown(tab);
        });
        modes.append(button);
        return button;
      });
      toolbar.append(modes);
      tab.status = document.createElement('span');
      tab.status.className = 'question-file-status';
      toolbar.append(tab.status);
      tab.save = makeButton('保存', 'btn btn-sm question-file-save', '保存 Markdown');
      tab.save.addEventListener('click', () => saveMarkdown(tab));
      toolbar.append(tab.save);
      const card = makeButton('制卡', 'btn btn-sm question-file-card', '从 Markdown 制卡');
      card.addEventListener('click', () => openCardDialog(tab));
      toolbar.append(card);
    } else {
      const tools = makeButton('工具', 'btn btn-sm question-file-tools', 'PDF 工具');
      tools.addEventListener('click', () => openToolDialog(tab));
      const card = makeButton('制卡', 'btn btn-sm question-file-card', '从 PDF 制卡');
      card.addEventListener('click', () => openCardDialog(tab));
      toolbar.append(tools, card);
    }
    panel.append(toolbar);
  }

  function createPanel(tab) {
    const panel = document.createElement('article');
    panel.className = 'question-file-panel';
    panel.dataset.filePanel = tab.key;
    tab.panel = panel;
    addToolbar(tab, panel);
    if (tab.kind === 'pdf') {
      const frame = document.createElement('iframe');
      frame.className = 'question-file-pdf';
      frame.src = `/library/raw?path=${encodeURIComponent(tab.path)}`;
      frame.title = tab.name;
      frame.setAttribute('loading', 'lazy');
      tab.pdfFrame = frame;
      panel.append(frame);
    } else {
      tab.preview = document.createElement('article');
      tab.preview.className = 'question-file-markdown';
      tab.editor = document.createElement('textarea');
      tab.editor.className = 'question-file-editor';
      tab.editor.spellcheck = false;
      tab.editor.disabled = true;
      tab.editor.setAttribute('aria-label', `编辑 ${tab.name}`);
      tab.editor.hidden = true;
      tab.editor.addEventListener('input', () => {
        tab.text = tab.editor.value;
        tab.dirty = tab.text !== tab.savedText;
        updateMarkdown(tab);
      });
      panel.append(tab.preview, tab.editor);
      updateMarkdown(tab);
      void readMarkdown(tab);
    }
    tab.group.panesHost.append(panel);
  }

  function confirmDiscard(tab) {
    if (!tab?.dirty) return true;
    if (typeof window.confirm !== 'function') return false;
    try {
      return window.confirm(`“${tab.name}”有未保存修改，确定关闭并放弃吗？`);
    } catch (_error) {
      return false;
    }
  }

  function removeTab(tabOrKey, options = {}) {
    const tab = typeof tabOrKey === 'string'
      ? allTabs().find(item => item.key === tabOrKey) : tabOrKey;
    if (!tab) return false;
    if (!options.force && !confirmDiscard(tab)) return false;
    const group = groupForTab(tab);
    if (!group) return false;
    const index = group.order.indexOf(tab.key);
    group.order = group.order.filter(key => key !== tab.key);
    group.tabs.delete(tab.key);
    tab.panel?.remove();
    if (group.previewKey === tab.key) group.previewKey = '';
    if (group.activeKey === tab.key) group.activeKey = group.order[index] || group.order[index - 1] || group.order[0] || '';
    renderAll();
    return true;
  }

  function open(meta, options = {}) {
    const path = normalizePath(meta?.path);
    if (!path) return null;
    const existing = tabForPath(path);
    if (existing) {
      const group = groupForTab(existing);
      if (options.pin) {
        existing.pinned = true;
        if (group?.previewKey === existing.key) group.previewKey = '';
      }
      if (group) {
        group.activeKey = existing.key;
        focusGroup(group.id);
        renderAll();
      }
      return existing;
    }
    const group = state.groups.get(options.groupId) || focusedGroup() || state.groups.get('primary');
    if (!group) return null;
    const preview = group.previewKey ? group.tabs.get(group.previewKey) : null;
    if (preview && !preview.pinned) {
      // 有未保存内容的临时标签不能静默丢弃，自动固定后再打开下一个文件。
      if (preview.dirty) preview.pinned = true;
      else removeTab(preview, {force: true});
    }
    const tab = {
      key: `file-${++state.tabSerial}`,
      path,
      name: String(meta?.name || pathName(path)),
      kind: kindFrom(meta),
      pinned: Boolean(options.pin),
      mode: 'read',
      generation: 0,
      dirty: false,
      text: undefined,
      group,
      groupId: group.id,
    };
    group.tabs.set(tab.key, tab);
    group.order.push(tab.key);
    group.activeKey = tab.key;
    if (!tab.pinned) group.previewKey = tab.key;
    createPanel(tab);
    focusGroup(group.id);
    renderAll();
    return tab;
  }

  function moveTabToOtherGroup(tab) {
    const source = groupForTab(tab);
    const destination = findOtherGroup(source);
    if (!source || !destination) return;
    migrateTab(tab, source, destination, true);
    focusGroup(destination.id);
    renderAll();
  }

  function renamePath(oldPath, newPath) {
    const oldValue = normalizePath(oldPath);
    const nextValue = normalizePath(newPath);
    if (!oldValue || !nextValue) return;
    allTabs().filter(tab => tab.path === oldValue || tab.path.startsWith(`${oldValue}/`)).forEach(tab => {
      tab.path = tab.path === oldValue
        ? nextValue : `${nextValue}${tab.path.slice(oldValue.length)}`;
      tab.name = pathName(nextValue);
      const pathNode = tab.panel?.querySelector('[data-qf-path]');
      if (pathNode) { pathNode.textContent = nextValue; pathNode.title = nextValue; }
      if (tab.pdfFrame) tab.pdfFrame.src = `/library/raw?path=${encodeURIComponent(nextValue)}`;
    });
    renderAll();
  }

  function closePath(path) {
    const value = normalizePath(path);
    allTabs().filter(tab => tab.path === value || tab.path.startsWith(`${value}/`))
      .forEach(tab => removeTab(tab, {force: true}));
  }

  function selectedMarkdownText(tab) {
    if (!tab || tab.kind !== 'markdown') return '';
    if (tab.editor && !tab.editor.hidden && document.activeElement === tab.editor) {
      const start = Number(tab.editor.selectionStart);
      const end = Number(tab.editor.selectionEnd);
      if (Number.isInteger(start) && Number.isInteger(end) && end > start) {
        return tab.editor.value.slice(start, end);
      }
    }
    const selection = window.getSelection?.();
    return selection && !selection.isCollapsed && tab.panel?.contains(selection.anchorNode)
      && tab.panel?.contains(selection.focusNode) ? selection.toString() : '';
  }

  function dialogScaffold(titleText, className = 'question-file-dialog') {
    document.querySelectorAll(`.${className}`).forEach(dialog => closeDialog(dialog));
    const dialog = document.createElement('dialog');
    dialog.className = className;
    const form = document.createElement('form');
    form.method = 'dialog';
    const head = document.createElement('div');
    head.className = 'qf-dialog-head';
    const title = document.createElement('h2');
    title.textContent = titleText;
    const close = makeButton('×', 'qf-dialog-close', '关闭');
    head.append(title, close);
    const body = document.createElement('div');
    body.className = 'qf-dialog-grid';
    const footer = document.createElement('div');
    footer.className = 'qf-dialog-footer';
    const status = document.createElement('span');
    status.className = 'qf-dialog-status';
    const submit = makeButton('加入任务', 'btn btn-primary', '加入任务');
    submit.type = 'submit';
    footer.append(status, submit);
    form.append(head, body, footer);
    dialog.append(form);
    close.addEventListener('click', () => closeDialog(dialog));
    dialog.addEventListener('cancel', event => { event.preventDefault(); closeDialog(dialog); });
    return {dialog, form, body, footer, status, submit};
  }

  async function openCardDialog(tab) {
    if (!tab || !['markdown', 'pdf'].includes(tab.kind)) return;
    const ui = dialogScaffold(tab.kind === 'markdown' ? 'Markdown 制卡' : 'PDF 制卡');
    const modeField = dialogField('制卡模式', 'select');
    appendOptions(modeField.input, [['single', '单题制卡'], ['multi', '多题制卡']]);
    ui.body.append(modeField.label);
    const boundaryField = dialogField('题目边界', 'select');
    appendOptions(boundaryField.input, [['auto', '智能审查题号连续性'], ['whitelist', '白名单分题（允许跳号）']]);
    ui.body.append(boundaryField.label);
    const engineField = dialogField('拆题方式', 'select');
    appendOptions(engineField.input, [['block', '逐题识别'], ['whole', '整篇识别']]);
    ui.body.append(engineField.label);
    const ocrField = dialogField('PDF 解析服务', 'select');
    appendOptions(ocrField.input, [['mineru', 'MinerU'], ['doc2x', 'Doc2X']]);
    ocrField.label.hidden = tab.kind !== 'pdf';
    ui.body.append(ocrField.label);
    const nameField = dialogField('题卡名称', 'text');
    nameField.input.maxLength = 180;
    ui.body.append(nameField.label);
    const targetField = dialogField('保存到题集', 'select');
    const temporary = document.createElement('option');
    temporary.value = '';
    temporary.textContent = '临时卡片（默认）';
    targetField.input.append(temporary);
    ui.body.append(targetField.label);
    const textField = tab.kind === 'markdown' ? dialogField('制卡文本', 'textarea') : null;
    if (textField) {
      textField.input.rows = 9;
      textField.input.value = selectedMarkdownText(tab) || String(tab.text || '');
      textField.input.placeholder = '可编辑或粘贴 Markdown 内容';
      ui.body.append(textField.label);
    }
    const pagesField = tab.kind === 'pdf' ? dialogField('PDF 页码（可选）', 'text') : null;
    if (pagesField) {
      pagesField.input.placeholder = '留空识别全文，例如 1,3,5';
      ui.body.append(pagesField.label);
    }
    const solutionField = document.createElement('label');
    solutionField.className = 'qf-dialog-check';
    const solutionText = document.createElement('span');
    solutionText.textContent = '识别解析';
    const solution = document.createElement('input');
    solution.type = 'checkbox';
    solution.checked = true;
    solutionField.append(solutionText, solution);
    ui.body.append(solutionField);
    const llmField = document.createElement('label');
    llmField.className = 'qf-dialog-check';
    const llmText = document.createElement('span');
    llmText.textContent = '大模型规范化';
    const llm = document.createElement('input');
    llm.type = 'checkbox';
    llm.checked = false;
    llmField.append(llmText, llm);
    llmField.hidden = tab.kind !== 'pdf';
    ui.body.append(llmField);
    boundaryField.input.addEventListener('change', () => {
      if (boundaryField.input.value === 'whitelist') engineField.input.value = 'block';
    });
    modeField.input.addEventListener('change', () => {
      nameField.label.firstElementChild.textContent = modeField.input.value === 'multi' ? '命名前缀' : '题卡名称';
    });
    void loadCollections(targetField.input, ui.status);
    ui.form.addEventListener('submit', async event => {
      event.preventDefault();
      const common = {
        split_mode: modeField.input.value,
        boundary_mode: boundaryField.input.value,
        target_collection: targetField.input.value,
        card_name: nameField.input.value.trim(),
        include_solution: solution.checked,
        use_llm: llm.checked,
        ocr_backend: ocrField.input.value,
        // 白名单边界依赖逐题切分；即使控件尚未触发 change，也不能把 whole 送到后端。
        engine: boundaryField.input.value === 'whitelist' ? 'block' : engineField.input.value,
      };
      let payload;
      if (tab.kind === 'markdown') {
        const text = textField.input.value;
        if (!text.trim()) {
          ui.status.textContent = '请先提供 Markdown 内容';
          ui.status.classList.add('is-error');
          textField.input.focus();
          return;
        }
        payload = {mode: 'markdown', ...common, text, source: stemName(tab.path)};
      } else {
        payload = {mode: 'file', ...common, path: tab.path};
        const rawPages = pagesField.input.value.trim();
        if (rawPages) {
          payload.pages = parsePageNumbers(rawPages);
          if (!payload.pages) {
            ui.status.textContent = 'PDF 页码格式无效';
            ui.status.classList.add('is-error');
            pagesField.input.focus();
            return;
          }
        }
      }
      ui.submit.disabled = true;
      ui.status.classList.remove('is-error');
      ui.status.textContent = '正在登记…';
      try {
        await fetchJson('/api/library/card-task', {
          method: 'POST',
          headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken()},
          body: JSON.stringify(payload),
        });
        closeDialog(ui.dialog);
        showToast('已加入制卡任务');
      } catch (error) {
        ui.status.textContent = error.message || '制卡任务登记失败';
        ui.status.classList.add('is-error');
        ui.submit.disabled = false;
      }
    });
    openDialog(ui.dialog);
    (textField?.input || pagesField?.input || modeField.input).focus();
  }

  function openToolDialog(tab) {
    if (!tab || tab.kind !== 'pdf') return;
    const ui = dialogScaffold('PDF 工具');
    const operationField = dialogField('操作', 'select');
    appendOptions(operationField.input, [
      ['pdf_extract', '提取页面'], ['pdf_reorder', '页面排序'],
      ['pdf_split', '拆分 PDF'], ['pdf_merge', '合并 PDF'], ['pdf_rotate', '旋转页面'],
    ]);
    ui.body.append(operationField.label);
    const sourceField = dialogField('PDF 文件（合并时每行一份）', 'textarea');
    sourceField.input.rows = 3;
    sourceField.input.value = tab.path;
    ui.body.append(sourceField.label);
    const pagesField = dialogField('页码', 'text');
    pagesField.input.placeholder = '例如 1,3,5';
    ui.body.append(pagesField.label);
    const rangesField = dialogField('拆分区间', 'text');
    rangesField.input.placeholder = '例如 1-3,4-8';
    ui.body.append(rangesField.label);
    const rotationField = dialogField('旋转角度', 'select');
    appendOptions(rotationField.input, [['90', '90°'], ['180', '180°'], ['270', '270°']]);
    ui.body.append(rotationField.label);
    const outputField = dialogField('输出文件（相对资料库）', 'text');
    outputField.input.placeholder = '留空使用默认名称';
    ui.body.append(outputField.label);
    const outputDirField = dialogField('输出文件夹（相对资料库）', 'text');
    outputDirField.input.placeholder = '留空使用源文件夹';
    ui.body.append(outputDirField.label);

    function refreshToolFields() {
      const operation = operationField.input.value;
      const pageOperation = ['pdf_extract', 'pdf_reorder', 'pdf_rotate'].includes(operation);
      sourceField.input.readOnly = operation !== 'pdf_merge';
      pagesField.label.hidden = !pageOperation;
      rangesField.label.hidden = operation !== 'pdf_split';
      rotationField.label.hidden = operation !== 'pdf_rotate';
      outputField.label.hidden = operation === 'pdf_split';
      outputDirField.label.hidden = operation !== 'pdf_split';
      if (operation === 'pdf_merge' && sourceField.input.value === tab.path) sourceField.input.value = `${tab.path}\n`;
    }
    operationField.input.addEventListener('change', refreshToolFields);
    refreshToolFields();
    ui.form.addEventListener('submit', async event => {
      event.preventDefault();
      const operation = operationField.input.value;
      const payload = {operation};
      if (operation === 'pdf_merge') {
        payload.sources = sourceField.input.value.split(/[\r\n]+/).map(value => normalizePath(value.trim())).filter(Boolean);
        if (payload.sources.length < 2) {
          ui.status.textContent = '合并至少需要两份 PDF';
          ui.status.classList.add('is-error');
          return;
        }
      } else {
        payload.source = tab.path;
      }
      if (operation === 'pdf_extract' || operation === 'pdf_rotate') {
        payload.pages = parsePageNumbers(pagesField.input.value);
        if (!payload.pages) {
          ui.status.textContent = '页码格式无效';
          ui.status.classList.add('is-error');
          pagesField.input.focus();
          return;
        }
      }
      if (operation === 'pdf_reorder') {
        payload.order = parsePageNumbers(pagesField.input.value);
        if (!payload.order) {
          ui.status.textContent = '排序页码格式无效';
          ui.status.classList.add('is-error');
          pagesField.input.focus();
          return;
        }
      }
      if (operation === 'pdf_split') {
        payload.ranges = parseRanges(rangesField.input.value);
        if (!payload.ranges) {
          ui.status.textContent = '拆分区间格式无效';
          ui.status.classList.add('is-error');
          rangesField.input.focus();
          return;
        }
        payload.output_dir = outputDirField.input.value.trim();
      } else {
        payload.output_path = outputField.input.value.trim();
      }
      if (operation === 'pdf_rotate') payload.rotation = Number(rotationField.input.value);
      ui.submit.disabled = true;
      ui.status.classList.remove('is-error');
      ui.status.textContent = '正在登记…';
      try {
        await fetchJson('/api/library/task', {
          method: 'POST',
          headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken()},
          body: JSON.stringify(payload),
        });
        closeDialog(ui.dialog);
        showToast('已加入转换任务');
      } catch (error) {
        ui.status.textContent = error.message || '转换任务登记失败';
        ui.status.classList.add('is-error');
        ui.submit.disabled = false;
      }
    });
    openDialog(ui.dialog);
    operationField.input.focus();
  }

  function openFromEvent(event) {
    const payload = event?.detail || event?.data || {};
    if (typeof payload === 'string') {
      open({path: payload});
      return;
    }
    if (payload?.detail && typeof payload.detail === 'object') return open(payload.detail);
    if (payload?.path) open(payload);
  }

  function handleMessage(event) {
    if (event.origin && event.origin !== window.location.origin) return;
    if (event.source && event.source !== window && event.source !== window.parent) return;
    const data = event.data || {};
    if (data.type !== 'open-question-file' && data.type !== 'open-library-file') return;
    open(data);
  }

  setupShell();

  // 控制条在 groupsHost 外层，统一委托到整个工作区才能保证布局按钮也生效。
  workspace.addEventListener('click', event => {
    const groupRoot = event.target.closest('[data-qf-group]');
    if (groupRoot) focusGroup(groupRoot.dataset.qfGroup);
    const add = event.target.closest('[data-qf-add-group]');
    if (add) { addGroup(focusedGroup()); return; }
    const layout = event.target.closest('[data-qf-layout]');
    if (layout) { setLayout(layout.dataset.qfLayout); return; }
    const split = event.target.closest('[data-qf-group-split]');
    if (split) { addGroup(state.groups.get(split.dataset.qfGroup)); return; }
    const closeGroupButton = event.target.closest('[data-qf-group-close]');
    if (closeGroupButton) { removeGroup(state.groups.get(closeGroupButton.dataset.qfGroup)); return; }
    const close = event.target.closest('[data-file-tab-close]');
    if (close) { removeTab(close.dataset.fileTabClose); return; }
    const pin = event.target.closest('[data-file-tab-pin]');
    if (pin) {
      const tab = allTabs().find(item => item.key === pin.dataset.fileTabPin);
      if (tab) {
        tab.pinned = !tab.pinned;
        const group = groupForTab(tab);
        if (tab.pinned && group?.previewKey === tab.key) group.previewKey = '';
        if (!tab.pinned && group) {
          const previous = group.previewKey ? group.tabs.get(group.previewKey) : null;
          if (previous && previous !== tab) {
            if (previous.dirty) previous.pinned = true;
            else removeTab(previous, {force: true});
          }
          group.previewKey = tab.key;
        }
        renderAll();
      }
      return;
    }
    const move = event.target.closest('[data-file-tab-move]');
    if (move) { moveTabToOtherGroup(allTabs().find(item => item.key === move.dataset.fileTabMove)); return; }
    const openButton = event.target.closest('[data-file-tab-open]');
    if (openButton) {
      const tab = allTabs().find(item => item.key === openButton.dataset.fileTabOpen);
      const group = groupForTab(tab);
      if (group) { group.activeKey = tab.key; focusGroup(group.id); renderAll(); }
    }
  });

  document.addEventListener('click', event => {
    const paper = event.target.closest('button.paper-open-question[data-question-file-path], button.paper-open-library[data-library-path]');
    if (paper) {
      event.preventDefault();
      open({path: paper.dataset.questionFilePath || paper.dataset.libraryPath, kind: 'pdf'});
      return;
    }
    const link = event.target.closest('.folder-file-link');
    if (!link) return;
    event.preventDefault();
    open({
      path: link.dataset.libraryPath || link.closest('.folder-file-item')?.dataset.filePath,
      kind: link.dataset.libraryKind || link.closest('.folder-file-item')?.dataset.fileKind,
      name: link.querySelector('.folder-file-name')?.textContent || link.textContent.trim(),
    });
  });

  document.addEventListener('dblclick', event => {
    const link = event.target.closest('.folder-file-link');
    if (link) {
      event.preventDefault();
      open({
        path: link.dataset.libraryPath || link.closest('.folder-file-item')?.dataset.filePath,
        kind: link.dataset.libraryKind || link.closest('.folder-file-item')?.dataset.fileKind,
        name: link.querySelector('.folder-file-name')?.textContent || link.textContent.trim(),
      }, {pin: true});
      return;
    }
    const tabButton = event.target.closest('[data-file-tab-open]');
    if (tabButton) {
      const tab = allTabs().find(item => item.key === tabButton.dataset.fileTabOpen);
      if (tab) { tab.pinned = true; const group = groupForTab(tab); if (group) group.previewKey = ''; renderAll(); }
    }
  });

  window.addEventListener('open-question-file', openFromEvent);
  window.addEventListener('qf:open-question-file', openFromEvent);
  window.addEventListener('open-library-file', openFromEvent);
  window.addEventListener('message', handleMessage);
  window.addEventListener('hashchange', () => {
    const hash = window.location.hash;
    if (!hash.startsWith('#file-')) return;
    try { open({path: decodeURIComponent(hash.slice(6))}); } catch (_error) { /* 忽略损坏的 hash。 */ }
  });
  window.addEventListener('qf:toast', event => {
    const detail = event.detail;
    showToast(typeof detail === 'object' ? detail?.message : detail,
      typeof detail === 'object' && Boolean(detail?.error));
  });

  const api = {
    open,
    renamePath,
    closePath,
    addGroup,
    setLayout,
    focusGroup,
  };
  window.QQuestionFileWorkspace = api;

  const initialFile = new URL(window.location.href).searchParams.get('file')
    || new URL(window.location.href).searchParams.get('open');
  const initialHash = window.location.hash.startsWith('#file-')
    ? window.location.hash.slice(6) : '';
  if (initialFile || initialHash) {
    window.requestAnimationFrame(() => {
      const path = initialFile || (() => {
        try { return decodeURIComponent(initialHash); } catch (_error) { return ''; }
      })();
      if (path) open({path});
    });
  }
  if (window.parent !== window) {
    try {
      window.parent.postMessage({source: 'quizforge', type: 'question-files-ready'}, window.location.origin);
    } catch (_error) { /* 非标准嵌入环境没有可用的消息通道时继续使用本页。 */ }
  }
  focusGroup('primary');
  renderAll();
})();
