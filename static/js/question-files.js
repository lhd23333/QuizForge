// 题库内文件工作区：文件树中的 PDF/Markdown 在题库页内打开，不再跳转资料库。
(function () {
  'use strict';

  const workspace = document.getElementById('question-file-workspace');
  if (!workspace) return;
  const initialPanesHost = workspace.querySelector('.question-file-panes');
  if (!initialPanesHost) return;
  if (workspace.dataset.questionFilesReady === '1') return;
  workspace.dataset.questionFilesReady = '1';

  const state = {
    groups: new Map(),
    tabSerial: 0,
    focusedGroupId: 'primary',
    layout: 'single',
    splitRatio: 50,
    maxGroups: 2,
    dragWorkspaceWasHidden: null,
  };
  const SPLIT_STORE = 'quizforge:question-file-split:v1';

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
    workspace.dataset.qfHasPanel = show ? '1' : '0';
    const activeTop = window.QFCollectionTabs?.active?.();
    // 题集页签激活时由顶栏控制器隐藏文件工作区；文件页签激活后才显示。
    workspace.hidden = Boolean(window.QFCollectionTabs) && activeTop
      ? !window.QFCollectionTabs.isFile(activeTop)
      : !show;
  }

  function injectWorkspaceStyles() {
    if (document.querySelector('style[data-question-file-workspace]')) return;
    const style = document.createElement('style');
    style.dataset.questionFileWorkspace = '1';
    style.textContent = `
      .question-file-workspace.qf-group-workspace { grid-template-rows: minmax(260px, 1fr); }
      .qf-group-head button { min-height:27px; padding:3px 8px;
        border:1px solid var(--border); border-radius:4px; background:transparent; color:var(--text-2); cursor:pointer; }
      .qf-group-head button:hover { border-color:var(--primary); background:var(--primary-soft); color:var(--primary); }
      .question-file-groups { position:relative; display:grid; min-height:0; min-width:0; gap:1px; background:var(--border); }
      .question-file-groups.is-single { grid-template-columns:minmax(0,1fr); grid-template-rows:minmax(0,1fr); }
      .question-file-groups.is-vertical { grid-template-columns:minmax(160px, var(--qf-split, 50%)) 6px minmax(160px, 1fr); grid-template-rows:minmax(0,1fr); }
      .qf-editor-group { display:grid; grid-template-rows:auto minmax(0,1fr); min-width:0; min-height:0; background:var(--surface); }
      .qf-editor-group.is-focused { box-shadow:inset 0 0 0 1px var(--primary); }
      .qf-group-head { display:flex; align-items:center; gap:6px; min-height:29px; padding:3px 7px;
        border-bottom:1px solid var(--border); background:var(--surface-2); }
      .qf-group-head strong { min-width:0; overflow:hidden; color:var(--muted); font-size:11px; font-weight:600; text-overflow:ellipsis; white-space:nowrap; }
      .qf-group-head .qf-group-spacer { flex:1; }
      .qf-group-head .qf-group-close { width:26px; padding-inline:0; }
      .qf-group-head .qf-group-close[hidden] { display:none; }
      .qf-group-tabs { min-width:0; }
      .qf-group-tabs { display:none !important; }
      .qf-group-panes { min-height:0; min-width:0; }
      .qf-group-empty { display:grid; place-items:center; height:100%; min-height:100px; color:var(--muted); font-size:12px; }
      .qf-editor-group.is-drop-target, .qf-group-empty.is-drop-target { outline:2px dashed var(--primary); outline-offset:-3px; background:var(--primary-soft); }
      .qf-splitter { grid-column:2; cursor:col-resize; background:var(--border); touch-action:none; }
      .qf-splitter:hover, .qf-splitter.is-dragging { background:var(--primary); }
      .qf-splitter[hidden] { display:none; }
      .qf-tab-drop-overlay { position:absolute; z-index:8; inset:0; display:grid; grid-template-columns:1fr 1fr; gap:8px; padding:10px; background:color-mix(in srgb, var(--surface) 42%, transparent); }
      .qf-tab-drop-zone { border:2px dashed var(--border-strong); background:color-mix(in srgb, var(--surface) 78%, transparent); }
      .qf-tab-drop-zone.is-drop-target { border-color:var(--primary); background:var(--primary-soft); }
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
        .question-file-groups.is-vertical { grid-template-columns:minmax(160px, var(--qf-split, 50%)) 6px minmax(160px, 1fr); }
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
    const close = makeButton('×', 'qf-group-close', '关闭此编辑器分组');
    close.dataset.qfGroupClose = id;
    close.hidden = id === 'primary';
    head.append(title, spacer, close);

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
    // 文件标签统一渲染在题库顶栏；分组这里只保留标题和内容面板。
    root.append(head, groupPanesHost);
    state.groups.set(id, group);
    root.addEventListener('pointerdown', () => focusGroup(id));
    return group;
  }

  function setupShell() {
    injectWorkspaceStyles();
    const previousChildren = [...workspace.children].filter(child => child !== initialPanesHost);
    /* 顶部标签承担页面切换和拖入分栏，工作区本身不再显示固定布局按钮。 */
    const groupsHost = document.createElement('div');
    groupsHost.className = 'question-file-groups is-single';
    groupsHost.dataset.qfGroups = '1';
    workspace.classList.add('qf-group-workspace');
    workspace.replaceChildren(groupsHost);
    const primary = createGroup('primary', null, initialPanesHost);
    groupsHost.append(primary.root);
    previousChildren.forEach(child => primary.root.append(child));
    state.groupsHost = groupsHost;
    try {
      const stored = sessionStorage.getItem(SPLIT_STORE);
      const saved = stored === null ? NaN : Number(stored);
      if (Number.isFinite(saved)) state.splitRatio = Math.max(25, Math.min(75, saved));
    } catch (_error) { /* sessionStorage 不可用时使用默认比例 */ }
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
    state.layout = state.groups.size > 1 ? 'vertical' : 'single';
    groupsHost.className = `question-file-groups is-${state.layout}`;
    groupsHost.style.setProperty('--qf-split', `${state.splitRatio}%`);
    const secondary = state.groups.get('secondary');
    if (secondary && secondary.root.parentElement !== groupsHost) groupsHost.append(secondary.root);
    if (secondary && state.splitter?.parentElement !== groupsHost) {
      groupsHost.insertBefore(state.splitter, secondary.root);
    }
    if (state.splitter) {
      state.splitter.hidden = !secondary;
      state.splitter.setAttribute('aria-valuenow', String(Math.round(state.splitRatio)));
    }
    state.groups.forEach(group => {
      const close = group.root.querySelector('[data-qf-group-close]');
      if (close) close.hidden = group.id === 'primary' || state.groups.size < 2;
    });
  }

  function renderGroup(group) {
    if (!group) return;
    // 标签已经在题库顶栏渲染，组内不再复制一套标签按钮。
    group.tabsHost?.replaceChildren();
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
    const activeTab = group.tabs.get(group.activeKey);
    group.title.textContent = activeTab?.name || (group.id === 'primary' ? '编辑器 1' : '编辑器 2');
    group.root.classList.toggle('is-focused', group.id === state.focusedGroupId);
  }

  function renderAll() {
    state.groups.forEach(renderGroup);
    renderLayout();
    showWorkspace(allTabs().length > 0 || state.groups.size > 1);
  }

  function persistSplitRatio() {
    try { sessionStorage.setItem(SPLIT_STORE, String(state.splitRatio)); }
    catch (_error) { /* sessionStorage 不可用时仅保留当前会话状态 */ }
  }

  function ensureSplitter() {
    if (state.splitter) return state.splitter;
    const splitter = document.createElement('div');
    splitter.className = 'qf-splitter';
    splitter.setAttribute('role', 'separator');
    splitter.setAttribute('aria-label', '调整文件分栏宽度');
    splitter.setAttribute('aria-orientation', 'vertical');
    splitter.setAttribute('aria-valuemin', '25');
    splitter.setAttribute('aria-valuemax', '75');
    splitter.setAttribute('aria-valuenow', String(Math.round(state.splitRatio)));
    splitter.tabIndex = 0;
    let dragging = false;
    const update = event => {
      if (!dragging) return;
      const rect = state.groupsHost.getBoundingClientRect();
      const raw = ((event.clientX - rect.left) / rect.width) * 100;
      state.splitRatio = Math.max(25, Math.min(75, raw));
      state.groupsHost.style.setProperty('--qf-split', `${state.splitRatio}%`);
      splitter.setAttribute('aria-valuenow', String(Math.round(state.splitRatio)));
    };
    const stop = () => {
      if (!dragging) return;
      dragging = false;
      splitter.classList.remove('is-dragging');
      document.documentElement.classList.remove('is-resizing-pane');
      persistSplitRatio();
    };
    splitter.addEventListener('pointerdown', event => {
      if (event.button !== 0 || state.groups.size < 2) return;
      dragging = true;
      splitter.setPointerCapture?.(event.pointerId);
      splitter.classList.add('is-dragging');
      document.documentElement.classList.add('is-resizing-pane');
      event.preventDefault();
    });
    splitter.addEventListener('pointermove', update);
    splitter.addEventListener('pointerup', stop);
    splitter.addEventListener('pointercancel', stop);
    // 指针捕获在嵌入页面或旧版浏览器中可能失效，文档级监听确保拖出分界线后仍能更新并收尾。
    document.addEventListener('pointermove', update);
    document.addEventListener('pointerup', stop);
    document.addEventListener('pointercancel', stop);
    window.addEventListener('blur', stop);
    splitter.addEventListener('keydown', event => {
      if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
      event.preventDefault();
      state.splitRatio = Math.max(25, Math.min(75,
        state.splitRatio + (event.key === 'ArrowRight' ? 3 : -3)));
      renderLayout();
      persistSplitRatio();
    });
    state.splitter = splitter;
    return splitter;
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
    [...group.order].forEach(key => {
      const tab = group.tabs.get(key);
      migrateTab(tab, group, primary, false);
      // 主栏在持久化数据中用空字符串表示，关闭次栏后同步清理旧分组标记。
      window.QFCollectionTabs?.setFileGroup?.(tab?.key, 'primary');
    });
    group.root.remove();
    state.groups.delete(group.id);
    if (state.focusedGroupId === group.id) state.focusedGroupId = 'primary';
    if (state.groups.size === 1) state.layout = 'single';
    renderAll();
    focusGroup(state.focusedGroupId);
  }

  function collapseEmptySecondary() {
    const secondary = state.groups.get('secondary');
    if (secondary && secondary.order.length === 0) removeGroup(secondary);
  }

  function addGroup(sourceGroup = focusedGroup()) {
    if (state.groups.size >= state.maxGroups) {
      showToast(`最多支持 ${state.maxGroups} 个编辑器分组`, true);
      return null;
    }
    const group = createGroup('secondary');
    state.groupsHost.append(group.root);
    ensureSplitter();
    state.layout = 'vertical';
    focusGroup(group.id);
    renderAll();
    return group;
  }

  function ensureGroup(groupId) {
    const normalized = String(groupId || '').trim();
    if (normalized !== 'secondary') return state.groups.get('primary');
    return state.groups.get('secondary') || addGroup(focusedGroup());
  }

  function setLayout(next) {
    const layout = next === 'single' ? 'single' : 'vertical';
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
    window.QFCollectionTabs?.setFileDirty?.(tab.key, Boolean(tab.dirty));
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
    // 文件名只在题库顶栏标签显示；面板工具栏保留操作按钮，避免重复占位。
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
    if (!options.skipTop && window.QFCollectionTabs?.closeFile) {
      const top = window.QFCollectionTabs.active?.();
      // 顶栏负责真正移除页签；收到 close 事件后会再次调用本函数并跳过回调。
      if (window.QFCollectionTabs.closeFile(tab.key, {force: true})) return true;
      if (top?.key === tab.key) return false;
    }
    const group = groupForTab(tab);
    if (!group) return false;
    const index = group.order.indexOf(tab.key);
    group.order = group.order.filter(key => key !== tab.key);
    group.tabs.delete(tab.key);
    tab.panel?.remove();
    if (group.previewKey === tab.key) group.previewKey = '';
    if (group.activeKey === tab.key) group.activeKey = group.order[index] || group.order[index - 1] || group.order[0] || '';
    if (!options.keepGroup) collapseEmptySecondary();
    renderAll();
    return true;
  }

  function syncTopDescriptor(descriptor, options = {}) {
    if (!descriptor?.key || !descriptor.filePath) return null;
    const activate = options.activate !== false;
    const path = normalizePath(descriptor.filePath);
    const kind = kindFrom({path, kind: descriptor.fileKind});
    let tab = allTabs().find(item => item.key === descriptor.key);
    let group = ensureGroup(descriptor.fileGroupId) || focusedGroup()
      || state.groups.get('primary');
    if (!group) return null;
    if (tab && tab.group !== group) {
      migrateTab(tab, groupForTab(tab), group, false);
      collapseEmptySecondary();
    }
    if (!tab) {
      tab = {
        key: descriptor.key, path, name: String(descriptor.name || pathName(path)),
        kind, pinned: Boolean(descriptor.pinned), mode: 'read', generation: 0,
        dirty: Boolean(descriptor.fileDirty), text: undefined, group, groupId: group.id,
      };
      group.tabs.set(tab.key, tab);
      group.order.push(tab.key);
      createPanel(tab);
    } else {
      const changed = tab.path !== path || tab.kind !== kind;
      if (changed) {
        tab.panel?.remove();
        Object.assign(tab, {path, kind, name: String(descriptor.name || pathName(path)),
          mode: 'read', generation: (tab.generation || 0) + 1, dirty: false,
          text: undefined, savedText: undefined, mtime: undefined, panel: null,
          preview: null, editor: null, pdfFrame: null});
        createPanel(tab);
      } else {
        tab.name = String(descriptor.name || tab.name || pathName(path));
      }
      tab.pinned = Boolean(descriptor.pinned);
      tab.group = group;
      tab.groupId = group.id;
    }
    if (activate) {
      group.activeKey = tab.key;
      focusGroup(group.id);
    }
    if (!tab.pinned) group.previewKey = tab.key;
    else if (group.previewKey === tab.key) group.previewKey = '';
    renderAll();
    return tab;
  }

  function open(meta, options = {}) {
    const path = normalizePath(meta?.path);
    if (!path) return null;
    const topApi = window.QFCollectionTabs;
    if (topApi?.openFile) {
      const descriptor = topApi.openFile({
        ...meta, path, kind: kindFrom({...meta, path}),
      }, {pin: Boolean(options.pin), groupId: options.groupId || focusedGroup()?.id});
      return syncTopDescriptor(descriptor);
    }
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
      else removeTab(preview, {force: true, keepGroup: true});
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

  function moveTabToGroup(tabOrKey, groupId) {
    const tab = typeof tabOrKey === 'string'
      ? allTabs().find(item => item.key === tabOrKey) : tabOrKey;
    const source = groupForTab(tab);
    const destination = ensureGroup(groupId);
    if (!tab || !source || !destination || source === destination) return false;
    migrateTab(tab, source, destination, true);
    window.QFCollectionTabs?.setFileGroup?.(tab.key, destination.id);
    collapseEmptySecondary();
    focusGroup(destination.id);
    renderAll();
    return true;
  }

  function renamePath(oldPath, newPath) {
    const oldValue = normalizePath(oldPath);
    const nextValue = normalizePath(newPath);
    if (!oldValue || !nextValue) return;
    window.QFCollectionTabs?.renameFilePath?.(oldValue, nextValue);
    allTabs().filter(tab => tab.path === oldValue || tab.path.startsWith(`${oldValue}/`)).forEach(tab => {
      tab.path = tab.path === oldValue
        ? nextValue : `${nextValue}${tab.path.slice(oldValue.length)}`;
      // 目录改名时保留每个文件自己的名称，不能把嵌套文件都改成目录名。
      tab.name = pathName(tab.path);
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

  // 顶栏标签是文件状态的唯一来源；题库文件脚本只负责把活动标签映射成内容面板。
  window.addEventListener('qf:collection-file-open', event => {
    syncTopDescriptor(event.detail?.tab);
  });
  window.addEventListener('qf:collection-file-activate', event => {
    const descriptor = event.detail?.tab;
    if (!descriptor) return;
    syncTopDescriptor(descriptor);
  });
  window.addEventListener('qf:collection-file-pin', event => {
    const descriptor = event.detail?.tab;
    if (!descriptor) return;
    const tab = allTabs().find(item => item.key === descriptor.key);
    if (tab) {
      tab.pinned = Boolean(descriptor.pinned);
      const group = groupForTab(tab);
      if (group) {
        if (tab.pinned && group.previewKey === tab.key) group.previewKey = '';
        if (!tab.pinned) group.previewKey = tab.key;
      }
      renderAll();
    }
  });
  window.addEventListener('qf:collection-file-group', event => {
    const descriptor = event.detail?.tab;
    const tab = allTabs().find(item => item.key === descriptor?.key);
    const target = ensureGroup(descriptor?.fileGroupId);
    const source = groupForTab(tab);
    if (tab && source && target && source !== target) {
      migrateTab(tab, source, target, true);
      collapseEmptySecondary();
      renderAll();
    }
  });
  window.addEventListener('qf:collection-file-close', event => {
    const key = event.detail?.tab?.key;
    const tab = allTabs().find(item => item.key === key);
    if (tab) removeTab(tab, {skipTop: true, force: true});
  });

  // 控制条在 groupsHost 外层，统一委托到整个工作区才能保证布局按钮也生效。
  workspace.addEventListener('click', event => {
    const groupRoot = event.target.closest('[data-qf-group]');
    if (groupRoot) focusGroup(groupRoot.dataset.qfGroup);
    const closeGroupButton = event.target.closest('[data-qf-group-close]');
    if (closeGroupButton) { removeGroup(state.groups.get(closeGroupButton.dataset.qfGroup)); return; }
  });

  let draggedTopTab = '';
  function showTabDropOverlay(show, options = {}) {
    state.dropOverlay?.remove();
    state.dropOverlay = null;
    if (!show) {
      if (options.restore !== false && state.dragWorkspaceWasHidden !== null) {
        workspace.hidden = state.dragWorkspaceWasHidden;
      }
      state.dragWorkspaceWasHidden = null;
      return;
    }
    if (state.dragWorkspaceWasHidden === null) {
      state.dragWorkspaceWasHidden = workspace.hidden;
    }
    // 非活动文件标签也可以拖入工作区；拖拽结束后再恢复原页面。
    workspace.hidden = false;
    const overlay = document.createElement('div');
    overlay.className = 'qf-tab-drop-overlay';
    ['primary', 'secondary'].forEach(id => {
      const zone = document.createElement('div');
      zone.className = 'qf-tab-drop-zone';
      zone.dataset.qfDropGroup = id;
      overlay.append(zone);
    });
    state.groupsHost.append(overlay);
    state.dropOverlay = overlay;
  }
  workspace.addEventListener('dragover', event => {
    if (!draggedTopTab) return;
    const zone = event.target.closest('[data-qf-drop-group]');
    if (!zone) return;
    event.preventDefault();
    workspace.querySelectorAll('.is-drop-target').forEach(node => node.classList.remove('is-drop-target'));
    zone.classList.add('is-drop-target');
    if (event.dataTransfer) event.dataTransfer.dropEffect = 'move';
  });
  workspace.addEventListener('dragleave', event => {
    if (!event.relatedTarget || !workspace.contains(event.relatedTarget)) {
      workspace.querySelectorAll('.is-drop-target').forEach(node => node.classList.remove('is-drop-target'));
    }
  });
  workspace.addEventListener('drop', event => {
    if (!draggedTopTab) return;
    const zone = event.target.closest('[data-qf-drop-group]');
    const targetId = zone?.dataset.qfDropGroup;
    if (!targetId) return;
    event.preventDefault();
    const descriptor = window.QFCollectionTabs?.active?.();
    const tab = allTabs().find(item => item.key === draggedTopTab);
    const target = ensureGroup(targetId);
    const source = groupForTab(tab);
    const effectiveTarget = target;
    if (tab && source && effectiveTarget && source !== effectiveTarget) {
      migrateTab(tab, source, effectiveTarget, true);
      window.QFCollectionTabs?.setFileGroup?.(tab.key, effectiveTarget.id);
      window.QFCollectionTabs?.activateFile?.(tab.key);
      collapseEmptySecondary();
      renderAll();
    } else if (descriptor?.key === draggedTopTab && effectiveTarget) {
      window.QFCollectionTabs?.setFileGroup?.(draggedTopTab, effectiveTarget.id);
      window.QFCollectionTabs?.activateFile?.(draggedTopTab);
    }
    workspace.querySelectorAll('.is-drop-target').forEach(node => node.classList.remove('is-drop-target'));
    showTabDropOverlay(false, {restore: false});
    draggedTopTab = '';
  });
  window.addEventListener('qf:collection-tab-drag-start', event => {
    draggedTopTab = event.detail?.file ? String(event.detail.key || '') : '';
    if (draggedTopTab && event.detail?.tab) {
      // 刷新后非活动标签尚未建立内容面板；拖动时惰性同步，但不抢走当前活动面板。
      syncTopDescriptor(event.detail.tab, {activate: false});
    }
    showTabDropOverlay(Boolean(draggedTopTab));
  });
  window.addEventListener('qf:collection-tab-drag-end', () => {
    draggedTopTab = '';
    showTabDropOverlay(false);
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
    const collectionFile = event.target.closest('[data-collection-tab-type="file"]');
    if (collectionFile) {
      event.preventDefault();
      window.QFCollectionTabs?.pinFile?.(collectionFile.dataset.collectionTabKey, true);
      return;
    }
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
    moveTabToGroup,
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
  const restoredFile = window.QFCollectionTabs?.active?.();
  if (restoredFile && window.QFCollectionTabs.isFile(restoredFile)) {
    syncTopDescriptor(restoredFile);
  }
  if (window.parent !== window) {
    try {
      window.parent.postMessage({source: 'quizforge', type: 'question-files-ready'}, window.location.origin);
    } catch (_error) { /* 非标准嵌入环境没有可用的消息通道时继续使用本页。 */ }
  }
  focusGroup('primary');
  renderAll();
})();
