// 本地资料库工作区：每个标签是一个页面槽位。文件点击只替换当前槽位，只有
// 加号会增加空白页；已打开槽位继续保留 DOM，切换时不销毁 PDF 阅读位置。
(function () {
  'use strict';

  const SESSION_KEY = 'quizforge:library-workspace:v3';
  const MAX_TABS = 20;
  const tree = document.getElementById('library-tree');
  const panesHost = document.getElementById('library-panes');
  const paneTemplate = document.getElementById('library-pane-template');
  if (!tree || !panesHost || !paneTemplate) return;

  const tabs = new Map();
  const panes = new Map();
  let layout = 'single';
  let focusedPaneId = 'primary';
  let splitRatio = 50;
  let tabSerial = 0;
  let splitter = null;
  let initialTreePromise = Promise.resolve();

  function kindFromPath(path) {
    if (String(path || '').replace(/\\/g, '/').startsWith('_handouts/')) return 'handout';
    const ext = (path.match(/\.[^.\/]+$/) || [''])[0].toLowerCase();
    if (['.md', '.markdown'].includes(ext)) return 'markdown';
    if (ext === '.pdf') return 'pdf';
    if (['.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.svg'].includes(ext)) return 'image';
    return '';
  }

  function validPath(raw) {
    const path = String(raw || '').replace(/\\/g, '/').replace(/^\/+/, '');
    const parts = path.split('/').filter(Boolean);
    if (!parts.length || parts.some((part, index) => part === '..'
        || (part.startsWith('.')
          && !(index === 0 && part === '.quizforge-history')))) return '';
    return parts.join('/');
  }

  function displayPath(path) {
    return path === '.quizforge-history'
      ? '历史记录'
      : path.replace(/^\.quizforge-history\//, '历史记录/');
  }

  function parentPath(path) {
    const parts = path.split('/');
    parts.pop();
    return parts.join('/');
  }

  function pathName(path) {
    return path.split('/').pop() || path;
  }

  async function fetchJson(url, options) {
    const response = await fetch(url, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.ok) {
      const error = new Error(data.error || `请求失败（${response.status}）`);
      error.status = response.status;
      error.data = data;
      throw error;
    }
    return data;
  }

  function createPane(id) {
    const fragment = paneTemplate.content.cloneNode(true);
    const root = fragment.querySelector('[data-library-pane]');
    root.dataset.paneId = id;
    const pane = {
      id,
      root,
      tabsBar: root.querySelector('.library-tabs'),
      content: root.querySelector('.library-pane-content'),
      empty: root.querySelector('.library-empty'),
      order: [],
      active: '',
    };
    panes.set(id, pane);
    panesHost.append(fragment);
    return pane;
  }

  function focusedPane() {
    return panes.get(focusedPaneId) || panes.get('primary');
  }

  function setFocusedPane(id) {
    if (!panes.has(id)) return;
    focusedPaneId = id;
    panes.forEach(pane => pane.root.classList.toggle('is-focused', pane.id === id));
    markActiveTree();
  }

  function ensureSplitter() {
    if (!splitter) {
      splitter = document.createElement('div');
      splitter.className = 'library-splitter';
      splitter.setAttribute('role', 'separator');
      splitter.setAttribute('aria-label', '调整分栏大小');
      splitter.addEventListener('pointerdown', event => {
        if (layout === 'single' || event.button !== 0) return;
        splitter.setPointerCapture?.(event.pointerId);
        event.preventDefault();
        function move(moveEvent) {
          const rect = panesHost.getBoundingClientRect();
          const raw = layout === 'vertical'
            ? ((moveEvent.clientX - rect.left) / rect.width) * 100
            : ((moveEvent.clientY - rect.top) / rect.height) * 100;
          splitRatio = Math.max(20, Math.min(80, raw));
          panesHost.style.setProperty('--library-split', splitRatio + '%');
        }
        function stop() {
          splitter.removeEventListener('pointermove', move);
          splitter.removeEventListener('pointerup', stop);
          splitter.removeEventListener('pointercancel', stop);
          saveSession();
        }
        splitter.addEventListener('pointermove', move);
        splitter.addEventListener('pointerup', stop);
        splitter.addEventListener('pointercancel', stop);
      });
    }
    const secondary = panes.get('secondary');
    if (secondary && splitter.parentElement !== panesHost) panesHost.append(splitter);
    if (secondary) panesHost.insertBefore(splitter, secondary.root);
  }

  function moveTab(tabKey, destinationId, activateMoved) {
    const tab = tabs.get(tabKey);
    const source = tab ? panes.get(tab.paneId) : null;
    const destination = panes.get(destinationId);
    if (!tab || !source || !destination || source === destination) return;
    source.order = source.order.filter(item => item !== tabKey);
    if (source.active === tabKey) source.active = source.order[source.order.length - 1] || '';
    destination.order.push(tabKey);
    tab.paneId = destination.id;
    if (tab.panel) destination.content.append(tab.panel);
    if (activateMoved) destination.active = tabKey;
  }

  function setLayout(next, options) {
    const mode = ['single', 'vertical', 'horizontal'].includes(next) ? next : 'single';
    const restore = Boolean(options && options.restore);
    if (mode === 'single') {
      const secondary = panes.get('secondary');
      const primary = panes.get('primary');
      if (secondary && primary) {
        [...secondary.order].forEach(path => moveTab(path, 'primary', false));
        if (!primary.active) primary.active = primary.order[0] || '';
        secondary.root.remove();
        panes.delete('secondary');
      }
      splitter?.remove();
      if (focusedPaneId === 'secondary') focusedPaneId = 'primary';
    } else {
      if (!panes.has('secondary')) createPane('secondary');
      ensureSplitter();
      if (!restore && layout === 'single') focusedPaneId = 'secondary';
    }
    layout = mode;
    panesHost.className = `library-panes is-${layout}`;
    panesHost.style.setProperty('--library-split', splitRatio + '%');
    document.querySelectorAll('[data-library-layout]').forEach(button => {
      button.classList.toggle('is-active', button.dataset.libraryLayout === layout);
    });
    renderAll();
    setFocusedPane(focusedPaneId);
    if (!restore) saveSession();
  }

  function saveSession() {
    try {
      sessionStorage.setItem(SESSION_KEY, JSON.stringify({
        version: 3,
        layout,
        focused: focusedPaneId,
        split: splitRatio,
      }));
    } catch (error) { /* 存储不可用时，本次页面生命周期仍正常保活。 */ }
  }

  function markActiveTree() {
    const activePath = tabs.get(focusedPane()?.active || '')?.path || '';
    tree.querySelectorAll('.library-tree-entry.is-active').forEach(node =>
      node.classList.remove('is-active'));
    tree.querySelectorAll('.library-tree-entry[data-path]').forEach(node => {
      if (node.dataset.path === activePath) node.classList.add('is-active');
    });
  }

  function pathToolbar(tab, markdown) {
    const toolbar = document.createElement('div');
    toolbar.className = 'library-view-toolbar';
    const path = document.createElement('span');
    path.className = markdown ? 'library-current-path' : 'library-current-path library-current-path-only';
    path.textContent = displayPath(tab.path);
    path.title = displayPath(tab.path);
    toolbar.append(path);
    if (markdown) {
      const modes = document.createElement('div');
      modes.className = 'library-md-modes';
      [['read', '阅读模式'], ['source', '源码模式']].forEach(([mode, label]) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = `library-md-mode${tab.mode === mode ? ' is-active' : ''}`;
        button.dataset.libraryMode = mode;
        button.dataset.tabKey = tab.key;
        button.textContent = label;
        modes.append(button);
      });
      toolbar.append(modes);
      const saveArea = document.createElement('div');
      saveArea.className = 'library-md-save-area';
      const status = document.createElement('span');
      status.className = 'library-md-save-status';
      const save = document.createElement('button');
      save.type = 'button';
      save.className = 'library-md-save';
      save.dataset.librarySave = tab.key;
      save.textContent = '保存';
      saveArea.append(status, save);
      toolbar.append(saveArea);
      tab.saveButton = save;
      tab.saveStatus = status;
    }
    return toolbar;
  }

  function updateMarkdownState(tab, message, isError) {
    if (!tab.saveButton || !tab.saveStatus) return;
    tab.saveButton.hidden = tab.mode !== 'source';
    tab.saveButton.disabled = !tab.dirty || tab.saving || tab.text === undefined;
    tab.saveStatus.textContent = message !== undefined
      ? message
      : tab.saving ? '正在保存…' : tab.dirty ? '未保存' : tab.mtime ? '已保存' : '';
    tab.saveStatus.classList.toggle('is-error', Boolean(isError));
    tab.saveStatus.classList.toggle('is-dirty', !isError && Boolean(tab.dirty));
  }

  function renderMarkdownContent(tab) {
    if (!tab.content || !tab.editor) return;
    tab.panel.querySelectorAll('.library-md-mode').forEach(button => {
      button.classList.toggle('is-active', button.dataset.libraryMode === tab.mode);
    });
    if (tab.loadError) {
      tab.content.className = 'library-document-content library-viewer-error';
      tab.content.textContent = tab.loadError;
      tab.content.hidden = false;
      tab.editor.hidden = true;
      updateMarkdownState(tab, tab.loadError, true);
      return;
    }
    tab.content.className = 'library-document-content library-markdown';
    tab.content.hidden = tab.mode === 'source';
    tab.editor.hidden = tab.mode !== 'source';
    if (tab.text === undefined) {
      tab.content.textContent = '正在读取 Markdown…';
      tab.editor.disabled = true;
    } else {
      tab.editor.disabled = false;
      if (document.activeElement !== tab.editor) tab.editor.value = tab.text;
      tab.content.innerHTML = window.QTextPreview?.renderRich(tab.text, {
        basePath: parentPath(tab.path),
      }) || '';
      window.QMath?.typeset(tab.content);
    }
    updateMarkdownState(tab);
  }

  async function loadMarkdown(tab) {
    try {
      const data = await fetchJson(`/api/library/read?path=${encodeURIComponent(tab.path)}`);
      tab.text = data.text;
      tab.savedText = data.text;
      tab.mtime = data.mtime;
      tab.dirty = false;
      tab.loadError = '';
    } catch (error) {
      tab.text = '';
      tab.loadError = error.message;
    }
    renderMarkdownContent(tab);
  }

  async function saveMarkdown(tab) {
    if (!tab || tab.kind !== 'markdown' || !tab.dirty || tab.saving) return;
    // st_mtime_ns 超过 JavaScript 安全整数上限，版本标记只能作为字符串原样往返；
    // 转成 Number 会丢失末位并让后端把正常保存误判成外部修改冲突。
    if (typeof tab.mtime !== 'string' || !/^\d{1,32}$/.test(tab.mtime)) {
      updateMarkdownState(tab, '文件尚未读取完成', true);
      return;
    }
    tab.saving = true;
    updateMarkdownState(tab);
    let message = '已保存';
    let failed = false;
    try {
      const data = await fetchJson('/api/library/write', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({path: tab.path, text: tab.text, mtime: tab.mtime}),
      });
      tab.mtime = data.mtime;
      tab.savedText = tab.text;
      tab.dirty = false;
      tab.discardArmedUntil = 0;
    } catch (error) {
      message = error.message;
      failed = true;
    } finally {
      tab.saving = false;
      updateMarkdownState(tab, message, failed);
    }
  }

  function ensurePanel(tab) {
    if (tab.panel) return tab.panel;
    const panel = document.createElement('div');
    panel.className = 'library-document-panel';
    panel.dataset.path = tab.path;
    panel.hidden = true;
    // Markdown 首次渲染会立即查询本面板里的模式按钮，所以引用必须先挂回 tab；
    // 放到分支末尾会让第一帧拿到 null，整次打开停在空白面板。
    tab.panel = panel;
    panes.get(tab.paneId).content.append(panel);
    const rawUrl = `/library/raw?path=${encodeURIComponent(tab.path)}`;
    if (tab.kind === 'markdown') {
      panel.append(pathToolbar(tab, true));
      const content = document.createElement('article');
      tab.content = content;
      const editor = document.createElement('textarea');
      editor.className = 'library-markdown-editor';
      editor.spellcheck = false;
      editor.setAttribute('aria-label', `编辑 ${tab.name}`);
      editor.hidden = true;
      editor.addEventListener('input', () => {
        tab.text = editor.value;
        tab.dirty = tab.text !== tab.savedText;
        tab.discardArmedUntil = 0;
        updateMarkdownState(tab);
      });
      tab.editor = editor;
      panel.append(content, editor);
      renderMarkdownContent(tab);
      loadMarkdown(tab);
    } else if (tab.kind === 'pdf') {
      panel.append(pathToolbar(tab, false));
      const frame = document.createElement('iframe');
      frame.className = 'library-pdf-frame';
      frame.src = rawUrl;
      frame.title = tab.name;
      panel.append(frame);
    } else if (tab.kind === 'image') {
      panel.append(pathToolbar(tab, false));
      const stage = document.createElement('div');
      stage.className = 'library-image-stage';
      const image = document.createElement('img');
      image.src = rawUrl;
      image.alt = tab.name;
      stage.append(image);
      panel.append(stage);
    } else {
      const error = document.createElement('div');
      error.className = 'library-viewer-error';
      error.textContent = '暂不支持这种文件类型';
      panel.append(error);
    }
    return panel;
  }

  function addBlankDocumentTab(pane = focusedPane(), activateTab = true) {
    if (!pane || tabs.size >= MAX_TABS) return null;
    const serial = ++tabSerial;
    const key = `page-${Date.now().toString(36)}-${serial.toString(36)}`;
    const tab = {
      key, path: '', kind: 'blank', name: '空白页', mode: 'read',
      paneId: pane.id, panel: null, dirty: false, saving: false,
      discardArmedUntil: 0, serial,
    };
    tabs.set(key, tab);
    pane.order.push(key);
    if (activateTab) pane.active = key;
    return tab;
  }

  function replaceTabDocument(tab, entry) {
    const path = validPath(entry.path);
    const kind = entry.kind || kindFromPath(path);
    if (!tab || !path || !kind) return false;
    if (tab.dirty) {
      updateMarkdownState(tab, '请先保存当前修改，再打开其他文件', true);
      return false;
    }
    tab.panel?.remove();
    ['content', 'editor', 'saveButton', 'saveStatus', 'text', 'savedText',
      'mtime', 'loadError'].forEach(name => delete tab[name]);
    Object.assign(tab, {
      path,
      kind,
      name: entry.name || pathName(path),
      mode: entry.mode === 'source' ? 'source' : 'read',
      panel: null,
      dirty: false,
      saving: false,
      discardArmedUntil: 0,
    });
    return true;
  }

  function drawTabs(pane) {
    pane.tabsBar.replaceChildren();
    pane.order.forEach(tabKey => {
      const tab = tabs.get(tabKey);
      if (!tab) return;
      const item = document.createElement('div');
      item.className = `library-tab${tabKey === pane.active ? ' is-active' : ''}`;
      const open = document.createElement('button');
      open.type = 'button';
      open.className = 'library-tab-open';
      open.dataset.tabKey = tabKey;
      open.setAttribute('role', 'tab');
      open.setAttribute('aria-selected', String(tabKey === pane.active));
      open.title = tab.path || tab.name;
      if (tab.kind !== 'blank') {
        const icon = document.createElement('span');
        icon.className = `library-file-icon is-${tab.kind}`;
        icon.textContent = tab.kind === 'markdown' ? 'M' : tab.kind === 'pdf' ? 'P' : 'I';
        open.append(icon);
      }
      const label = document.createElement('span');
      label.className = 'library-tab-label';
      label.textContent = tab.name;
      open.append(label);
      item.append(open);
      if (layout !== 'single') {
        const move = document.createElement('button');
        move.type = 'button';
        move.className = 'library-tab-move';
        move.dataset.moveTab = tabKey;
        move.title = pane.id === 'primary' ? '移到另一栏' : '移到第一栏';
        move.setAttribute('aria-label', move.title);
        move.textContent = layout === 'vertical' ? '⇄' : '⇅';
        item.append(move);
      }
      const close = document.createElement('button');
      close.type = 'button';
      close.className = 'library-tab-close';
      close.dataset.closeTab = tabKey;
      close.title = `关闭 ${tab.name}`;
      close.setAttribute('aria-label', close.title);
      close.textContent = '×';
      close.hidden = pane.order.length <= 1;
      item.append(close);
      pane.tabsBar.append(item);
    });
    const add = document.createElement('button');
    add.type = 'button';
    add.className = 'library-tab-add';
    add.dataset.libraryTabAdd = pane.id;
    add.title = '新建空白页';
    add.setAttribute('aria-label', '新建空白页');
    add.textContent = '＋';
    pane.tabsBar.append(add);
    pane.tabsBar.hidden = false;
  }

  function renderPane(pane) {
    drawTabs(pane);
    const activeTab = tabs.get(pane.active);
    if (activeTab && activeTab.kind !== 'blank') ensurePanel(activeTab);
    pane.order.forEach(tabKey => {
      const panel = tabs.get(tabKey)?.panel;
      if (panel) panel.hidden = tabKey !== pane.active;
    });
    pane.empty.hidden = Boolean(activeTab && activeTab.kind !== 'blank');
  }

  function renderAll() {
    panes.forEach(renderPane);
    markActiveTree();
  }

  function activate(tabKey) {
    const tab = tabs.get(tabKey);
    if (!tab) return;
    const pane = panes.get(tab.paneId);
    pane.active = tabKey;
    setFocusedPane(pane.id);
    renderPane(pane);
    saveSession();
  }

  function openDocument(entry) {
    const path = validPath(entry.path);
    const kind = entry.kind || kindFromPath(path);
    if (!path || !kind) return;
    if (kind === 'handout') {
      const target = `/handouts?path=${encodeURIComponent(path)}`;
      if (window.parent !== window) {
        window.parent.postMessage({source: 'quizforge', type: 'navigate', url: target}, '*');
      } else {
        window.location.href = target;
      }
      return;
    }
    const pane = focusedPane();
    let tab = tabs.get(pane.active);
    if (!tab) tab = addBlankDocumentTab(pane, true);
    if (!replaceTabDocument(tab, {...entry, path, kind})) return;
    pane.active = tab.key;
    renderPane(pane);
    markActiveTree();
    saveSession();
  }

  function closeDocument(tabKey) {
    const tab = tabs.get(tabKey);
    if (!tab) return;
    if (tab.dirty && Date.now() > (tab.discardArmedUntil || 0)) {
      tab.discardArmedUntil = Date.now() + 4000;
      updateMarkdownState(tab, '有未保存修改；4 秒内再次关闭将放弃', true);
      return;
    }
    const pane = panes.get(tab.paneId);
    const index = pane.order.indexOf(tabKey);
    pane.order.splice(index, 1);
    tab.panel?.remove();
    tabs.delete(tabKey);
    if (!pane.order.length) addBlankDocumentTab(pane, true);
    else if (pane.active === tabKey) {
      pane.active = pane.order[index] || pane.order[index - 1] || pane.order[0];
    }
    renderPane(pane);
    markActiveTree();
    saveSession();
  }

  function moveDocument(tabKey) {
    const tab = tabs.get(tabKey);
    if (!tab || layout === 'single') return;
    const destinationId = tab.paneId === 'primary' ? 'secondary' : 'primary';
    const source = panes.get(tab.paneId);
    moveTab(tabKey, destinationId, true);
    if (!source.order.length) addBlankDocumentTab(source, true);
    setFocusedPane(destinationId);
    renderPane(source);
    renderPane(panes.get(destinationId));
    saveSession();
  }

  function entryNode(entry) {
    const node = document.createElement('div');
    node.className = 'library-tree-node';
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `library-tree-entry is-${entry.kind}`;
    button.dataset.path = entry.path;
    button.dataset.kind = entry.kind;
    button.setAttribute('role', 'treeitem');
    button.title = entry.path;
    const twist = document.createElement('span');
    twist.className = 'library-tree-twist';
    twist.textContent = entry.kind === 'folder' ? '›' : '';
    const label = document.createElement('span');
    label.className = 'library-tree-label';
    label.textContent = entry.name;
    button.append(twist);
    if (entry.kind !== 'folder') {
      const icon = document.createElement('span');
      icon.className = `library-file-icon is-${entry.kind}`;
      icon.textContent = entry.kind === 'handout' ? 'H' : entry.kind === 'markdown' ? 'M'
        : entry.kind === 'pdf' ? 'P' : 'I';
      button.append(icon);
    }
    button.append(label);
    node.append(button);
    if (entry.kind === 'folder') {
      const children = document.createElement('div');
      children.className = 'library-tree-children';
      children.hidden = true;
      node.append(children);
    }
    return node;
  }

  async function loadChildren(host, path, offset) {
    if (!offset) {
      host.replaceChildren();
      const loading = document.createElement('div');
      loading.className = 'library-loading';
      loading.textContent = '正在读取…';
      host.append(loading);
    }
    try {
      const data = await fetchJson(`/api/library/children?path=${encodeURIComponent(path)}&offset=${offset || 0}`);
      if (!offset) host.replaceChildren();
      data.entries.forEach(entry => host.append(entryNode(entry)));
      if (!data.done) {
        const more = document.createElement('button');
        more.type = 'button';
        more.className = 'library-tree-more';
        more.dataset.path = path;
        more.dataset.offset = data.next_offset;
        more.textContent = `继续加载（${data.next_offset}/${data.total}）`;
        host.append(more);
      }
      host.dataset.loaded = '1';
      markActiveTree();
      return data;
    } catch (error) {
      host.replaceChildren();
      const message = document.createElement('div');
      message.className = 'library-tree-error';
      message.textContent = error.message;
      host.append(message);
      return null;
    }
  }

  async function revealFile(rawPath) {
    const path = validPath(rawPath);
    if (!path) return false;
    await initialTreePromise;
    const parts = path.split('/');
    let parent = '';
    let host = tree;
    let entry = null;
    for (let index = 0; index < parts.length; index += 1) {
      const current = parent ? `${parent}/${parts[index]}` : parts[index];
      entry = [...host.querySelectorAll(':scope > .library-tree-node > .library-tree-entry')]
        .find(node => node.dataset.path === current) || null;
      while (!entry) {
        const more = host.querySelector(':scope > .library-tree-more');
        if (!more) break;
        const offset = Number(more.dataset.offset || 0);
        more.remove();
        await loadChildren(host, parent, offset);
        entry = [...host.querySelectorAll(':scope > .library-tree-node > .library-tree-entry')]
          .find(node => node.dataset.path === current) || null;
      }
      if (!entry) return false;
      if (index === parts.length - 1) break;
      if (entry.dataset.kind !== 'folder') return false;
      const node = entry.closest('.library-tree-node');
      const children = node.querySelector(':scope > .library-tree-children');
      children.hidden = false;
      entry.classList.add('is-open');
      if (children.dataset.loaded !== '1') await loadChildren(children, current, 0);
      host = children;
      parent = current;
    }
    if (!entry) return false;
    openDocument({path, kind: entry.dataset.kind,
                  name: entry.querySelector('.library-tree-label')?.textContent});
    entry.classList.add('is-revealed');
    entry.scrollIntoView?.({block: 'center'});
    window.setTimeout(() => entry.classList.remove('is-revealed'), 1200);
    return true;
  }

  tree.addEventListener('click', event => {
    const more = event.target.closest('.library-tree-more');
    if (more) {
      const host = more.parentElement || tree;
      more.remove();
      loadChildren(host, more.dataset.path, Number(more.dataset.offset));
      return;
    }
    const entry = event.target.closest('.library-tree-entry');
    if (!entry) return;
    if (entry.dataset.kind === 'folder') {
      const node = entry.closest('.library-tree-node');
      const children = node.querySelector(':scope > .library-tree-children');
      const opening = children.hidden;
      children.hidden = !opening;
      entry.classList.toggle('is-open', opening);
      if (opening && children.dataset.loaded !== '1') loadChildren(children, entry.dataset.path, 0);
    } else {
      openDocument({path: entry.dataset.path, kind: entry.dataset.kind,
                    name: entry.querySelector('.library-tree-label')?.textContent});
    }
  });

  window.addEventListener('message', event => {
    if (event.origin !== window.location.origin || event.source !== window.parent
        || event.data?.source !== 'quizforge'
        || event.data?.type !== 'open-library-file') return;
    revealFile(event.data.path).catch(() => {});
  });

  panesHost.addEventListener('click', event => {
    const paneRoot = event.target.closest('[data-library-pane]');
    if (paneRoot) setFocusedPane(paneRoot.dataset.paneId);
    const add = event.target.closest('.library-tab-add');
    if (add) {
      const pane = panes.get(add.dataset.libraryTabAdd) || focusedPane();
      addBlankDocumentTab(pane, true);
      setFocusedPane(pane.id);
      renderPane(pane);
      saveSession();
      return;
    }
    const close = event.target.closest('.library-tab-close');
    if (close) {
      closeDocument(close.dataset.closeTab);
      return;
    }
    const move = event.target.closest('.library-tab-move');
    if (move) {
      moveDocument(move.dataset.moveTab);
      return;
    }
    const open = event.target.closest('.library-tab-open');
    if (open) {
      activate(open.dataset.tabKey);
      return;
    }
    const mode = event.target.closest('.library-md-mode');
    if (mode) {
      const tab = tabs.get(mode.dataset.tabKey);
      if (!tab || tab.kind !== 'markdown') return;
      tab.mode = mode.dataset.libraryMode;
      renderMarkdownContent(tab);
      saveSession();
      return;
    }
    const save = event.target.closest('.library-md-save');
    if (save) {
      saveMarkdown(tabs.get(save.dataset.librarySave));
      return;
    }
    const link = event.target.closest('.library-doc-link');
    if (link) openDocument({path: link.dataset.libraryPath});
  });

  document.querySelectorAll('[data-library-layout]').forEach(button => {
    button.addEventListener('click', () => setLayout(button.dataset.libraryLayout));
  });

  document.addEventListener('keydown', event => {
    const active = focusedPane()?.active;
    const tab = active ? tabs.get(active) : null;
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's'
        && tab?.kind === 'markdown') {
      event.preventDefault();
      saveMarkdown(tab);
      return;
    }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'w' && active) {
      event.preventDefault();
      closeDocument(active);
    }
  });

  createPane('primary');
  try {
    const saved = JSON.parse(sessionStorage.getItem(SESSION_KEY) || '{}');
    if (saved.version === 3) {
      splitRatio = Number.isFinite(Number(saved.split))
        ? Math.max(20, Math.min(80, Number(saved.split))) : 50;
      layout = ['single', 'vertical', 'horizontal'].includes(saved.layout)
        ? saved.layout : 'single';
      focusedPaneId = saved.focused === 'secondary' ? 'secondary' : 'primary';
    }
  } catch (error) {
    // 损坏会话只丢布局，不影响继续打开本地文件。
  }

  setLayout(layout, {restore: true});
  panes.forEach(pane => {
    if (!pane.order.length) addBlankDocumentTab(pane, true);
  });
  renderAll();
  setFocusedPane(focusedPaneId);
  initialTreePromise = loadChildren(tree, '', 0);
  if (window.parent !== window) window.parent.postMessage(
    {source: 'quizforge', type: 'library-ready'}, window.location.origin);
  const initialOpen = new URLSearchParams(window.location.search).get('open');
  if (initialOpen) revealFile(initialOpen).catch(() => {});
  window.addEventListener('beforeunload', event => {
    if (![...tabs.values()].some(tab => tab.dirty)) return;
    event.preventDefault();
    event.returnValue = '';
  });
})();
