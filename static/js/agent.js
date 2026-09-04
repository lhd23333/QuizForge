(function () {
  'use strict';

  const panel = document.getElementById('agent-dialog');
  const openButton = document.getElementById('agent-open');
  const embeddedOpenButton = document.getElementById('agent-embedded-open');
  const isEmbedded = window.parent !== window;
  // 业务页面可能被桌面外壳或 Obsidian iframe 承载。访问父窗口 DOM
  // 只用于同源外壳检测，跨域时由 postMessage 的失败回退到本地面板。
  function parentAgentPanel() {
    if (!isEmbedded) return null;
    try { return window.parent.document.getElementById('agent-dialog'); }
    catch (_) { return null; }
  }
  if (!panel || (!openButton && !embeddedOpenButton)) return;

  const $ = id => document.getElementById(id);
  const csrf = () => document.querySelector('meta[name="csrf-token"]')?.content || '';
  const ACTIVE_SESSION_KEY = 'quizforge.agent.active-session';
  const SESSION_ORDER_KEY = 'quizforge.agent.session-order.v1';
  const state = {
    sessions: [], activeId: (() => {
      try { return localStorage.getItem(ACTIVE_SESSION_KEY); } catch (_) { return null; }
    })(), folders: [], providers: [], providerPresets: [], approvals: [],
    busy: false, opening: false, abortController: null, activeTurnId: null,
    dangerGrant: null,
    taskTimers: new Map(), taskCards: new Map(), stagedCards: new Map(), approvalTimer: null,
    draggingSessionId: null, providerEditingId: null,
  };

  function readSessionOrder() {
    try {
      const value = JSON.parse(localStorage.getItem(SESSION_ORDER_KEY) || '[]');
      return Array.isArray(value) ? value.map(item => String(item)).filter(Boolean) : [];
    } catch (_) { return []; }
  }

  function writeSessionOrder() {
    try {
      localStorage.setItem(SESSION_ORDER_KEY,
        JSON.stringify(state.sessions.map(session => String(session.id)).slice(0, 100)));
    } catch (_) { /* 隐私模式或禁用存储时仅保持本次顺序。 */ }
  }

  function applySessionOrder(rows) {
    const source = Array.isArray(rows) ? rows : [];
    const byId = new Map(source.map(session => [String(session.id), session]));
    const ordered = [];
    readSessionOrder().forEach(id => {
      const session = byId.get(id);
      if (session) { ordered.push(session); byId.delete(id); }
    });
    // 新会话沿用服务端的最新顺序，手动排序过的旧会话则保持用户顺序。
    source.forEach(session => {
      if (byId.has(String(session.id))) {
        ordered.push(session); byId.delete(String(session.id));
      }
    });
    return ordered;
  }

  function activeSession() {
    return state.sessions.find(item => item.id === state.activeId) || null;
  }

  function expireDangerIfNeeded(message) {
    if (!state.dangerGrant || !String(message || '').includes('危险模式授权已失效')) return;
    state.dangerGrant = null;
    window.queueMicrotask?.(() => renderMessages());
  }

  async function request(url, options = {}) {
    const headers = Object.assign({'X-CSRF-Token': csrf()}, options.headers || {});
    if (String(url).startsWith('/api/agent/') && state.dangerGrant?.token) {
      headers['X-Agent-Danger-Token'] = state.dangerGrant.token;
    }
    if (options.body && !(options.body instanceof FormData) && !headers['Content-Type']) {
      headers['Content-Type'] = 'application/json';
    }
    const response = await fetch(url, Object.assign({}, options, {headers}));
    const data = await response.json().catch(() => ({ok: false, error: '服务器返回了无效响应'}));
    if (!response.ok || data.ok === false) {
      expireDangerIfNeeded(data.error);
      const error = new Error(data.error || `请求失败（${response.status}）`);
      error.payload = data;
      error.status = response.status;
      throw error;
    }
    return data;
  }

  async function streamRequest(url, options, onEvent) {
    const headers = Object.assign({'X-CSRF-Token': csrf()}, options?.headers || {});
    if (state.dangerGrant?.token) headers['X-Agent-Danger-Token'] = state.dangerGrant.token;
    if (options?.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
    const response = await fetch(url, Object.assign({}, options, {headers}));
    if (!response.ok) {
      const data = await response.json().catch(() => ({ok: false, error: `请求失败（${response.status}）`}));
      expireDangerIfNeeded(data.error);
      const error = new Error(data.error || `请求失败（${response.status}）`);
      error.payload = data; error.status = response.status;
      throw error;
    }
    if (!response.body?.getReader || !window.TextDecoder) {
      const error = new Error('当前 WebView 不支持流式响应');
      error.code = 'stream_unavailable';
      throw error;
    }
    const reader = response.body.getReader();
    const decoder = new window.TextDecoder('utf-8');
    let buffer = '';

    function consumeFrame(frame) {
      let eventType = '';
      const dataLines = [];
      frame.split('\n').forEach(line => {
        if (line.startsWith('event:')) eventType = line.slice(6).trim();
        else if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart());
      });
      if (!dataLines.length) return;
      const payload = JSON.parse(dataLines.join('\n'));
      if (!payload.type && eventType) payload.type = eventType;
      onEvent(payload);
    }

    try {
      while (true) {
        const {value, done} = await reader.read();
        buffer += decoder.decode(value || new Uint8Array(), {stream: !done});
        buffer = buffer.replace(/\r\n/g, '\n');
        let boundary = buffer.indexOf('\n\n');
        while (boundary >= 0) {
          const frame = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          if (frame && !frame.startsWith(':')) consumeFrame(frame);
          boundary = buffer.indexOf('\n\n');
        }
        if (done) break;
      }
      if (buffer.trim() && !buffer.trimStart().startsWith(':')) consumeFrame(buffer);
    } finally {
      reader.releaseLock?.();
    }
  }

  function setStatus(text, stateName = 'ready') {
    const node = $('agent-status');
    if (node) { node.textContent = text; node.dataset.state = stateName; }
    if (panel) panel.dataset.state = stateName;
  }

  function setActivity(text, visible) {
    const activity = $('agent-activity');
    if (!activity) return;
    activity.hidden = !visible;
    if (text && $('agent-activity-text')) $('agent-activity-text').textContent = text;
  }

  function setOpen(open) {
    const backdrop = $('agent-backdrop');
    openButton?.setAttribute('aria-expanded', String(Boolean(open)));
    embeddedOpenButton?.setAttribute('aria-expanded', String(Boolean(open)));
    panel.setAttribute('aria-hidden', String(!open));
    if (isEmbedded && embeddedOpenButton && !parentAgentPanel()) {
      embeddedOpenButton.hidden = Boolean(open);
    }
    if (open) {
      panel.hidden = false;
      panel.setAttribute('aria-hidden', 'false');
      document.body.classList.add('agent-open');
      if (backdrop) backdrop.hidden = false;
      // 小屏幕下会话栏是覆盖层；每次重新打开面板都回到聊天正文，避免
      // 上次关闭时留下一个挡住输入框的会话抽屉。
      $('agent-workspace')?.classList.remove('show-conversations');
      $('agent-toggle-conversations')?.setAttribute('aria-expanded', 'false');
      requestAnimationFrame(() => {
        panel.dataset.open = 'true';
        // split-panes.js 在面板隐藏时无法读取宽度；打开后主动通知它刷新
        // ARIA 数值和可拖动边界，已保存的面板宽度也会立即生效。
        window.dispatchEvent(new Event('resize'));
      });
      if (!state.approvalTimer) {
        state.approvalTimer = window.setInterval(() => refreshApprovals(), 8000);
      }
      $('agent-input')?.focus();
      return;
    }
    panel.dataset.open = 'false';
    panel.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('agent-open');
    if (backdrop) backdrop.hidden = true;
    if (state.approvalTimer) {
      window.clearInterval(state.approvalTimer); state.approvalTimer = null;
    }
    window.setTimeout(() => {
      if (panel.dataset.open !== 'true') panel.hidden = true;
    }, 180);
  }

  function folderOptions(nodes, depth = 0, out = []) {
    (nodes || []).forEach(node => {
      out.push({id: node.id || '', name: `${'　'.repeat(depth)}${node.name || node.id}`});
      folderOptions(node.children, depth + 1, out);
    });
    return out;
  }

  function relativeWorkdir(path) {
    if (!path) return '';
    const text = String(path);
    const bank = document.body.dataset.bankDir || '';
    if (bank && text.toLowerCase().startsWith(bank.toLowerCase())) {
      return text.slice(bank.length).replace(/^[\\/]+/, '').replace(/\\/g, '/');
    }
    // 后端会话的新格式总是提供 workdir_id；旧会话若没有根目录信息，
    // 不猜测绝对路径中的文件夹名称，回落到题库根目录更安全。
    return '';
  }

  function renderFolderOptions() {
    const select = $('agent-workdir');
    if (!select) return;
    const current = activeSession();
    const selected = current?.workdir_id || relativeWorkdir(current?.workdir);
    select.replaceChildren(new Option('题库根目录', ''));
    folderOptions(state.folders).forEach(folder => select.add(new Option(folder.name, folder.id)));
    if (current?.scope === 'bank') select.value = selected || '';
    const output = $('agent-output-dir');
    if (output) {
      const outputSelected = current?.output_dir_id || selected || '';
      output.replaceChildren(new Option('跟随工作目录', ''));
      folderOptions(state.folders).forEach(folder => output.add(new Option(folder.name, folder.id)));
      output.value = outputSelected;
    }
    const inputDir = $('agent-input-dir');
    if (inputDir) {
      const inputSelected = current?.input_dir_id || selected || '';
      inputDir.replaceChildren(new Option('跟随题库目录', ''));
      folderOptions(state.folders).forEach(folder => inputDir.add(new Option(folder.name, folder.id)));
      inputDir.value = inputSelected;
    }
    const manual = $('agent-workdir-manual');
    if (manual && document.activeElement !== manual) manual.value = selected || '';
  }

  function workdirDisplayName(workdirId) {
    const id = String(workdirId || '').replace(/\\/g, '/').replace(/^\/+|\/+$/g, '');
    if (!id) return '题库根目录';
    const option = folderOptions(state.folders).find(item => item.id === id);
    if (option) return String(option.name || id).trim();
    const parts = id.split('/').filter(Boolean);
    return parts[parts.length - 1] || id;
  }

  function sessionTitle(session) {
    const first = (session.messages || []).find(message => message.role === 'user');
    const title = first?.content?.trim();
    return title ? title.slice(0, 36) : '新对话';
  }

  function sessionTimestamp(session) {
    const raw = session?.updated_at ?? session?.created_at;
    if (raw === undefined || raw === null || raw === '') return NaN;
    const numeric = Number(raw);
    return Number.isFinite(numeric)
      ? (numeric < 100000000000 ? numeric * 1000 : numeric)
      : Date.parse(String(raw));
  }

  function sessionTimeLabel(session) {
    const stamp = sessionTimestamp(session);
    if (!Number.isFinite(stamp)) return '';
    const delta = Math.max(0, Date.now() - stamp);
    if (delta < 60 * 1000) return '刚刚';
    if (delta < 60 * 60 * 1000) return `${Math.floor(delta / 60000)} 分钟前`;
    if (delta < 24 * 60 * 60 * 1000) return `${Math.floor(delta / 3600000)} 小时前`;
    const date = new Date(stamp);
    return Number.isNaN(date.getTime()) ? '' : `${date.getMonth() + 1}/${date.getDate()}`;
  }

  function sessionScopeLabel(session) {
    return session?.scope === 'chat' ? '仅聊天' : `题库 · ${workdirDisplayName(session?.workdir_id)}`;
  }

  function renderSessions() {
    const list = $('agent-sessions');
    const empty = $('agent-sessions-empty');
    if (!list) return;
    list.replaceChildren();
    const needle = ($('agent-session-search')?.value || '').trim().toLowerCase();
    const rows = state.sessions.filter(session => {
      return !needle || sessionTitle(session).toLowerCase().includes(needle);
    });
    if (empty) {
      empty.hidden = rows.length > 0;
      const emptyLabel = empty.querySelector('span');
      if (emptyLabel) emptyLabel.textContent = needle ? '没有匹配的对话' : '还没有对话';
      const emptyAction = empty.querySelector('button');
      if (emptyAction) emptyAction.hidden = Boolean(needle);
    }
    const count = $('agent-session-count');
    if (count) count.textContent = state.sessions.length
      ? (needle ? `${rows.length}/${state.sessions.length}` : String(state.sessions.length)) : '';
    rows.forEach(session => {
      const row = document.createElement('div');
      row.className = 'agent-session-row';
      row.dataset.sessionId = session.id;
      row.draggable = true;
      row.title = '拖动调整对话顺序';

      const button = document.createElement('button');
      button.type = 'button'; button.className = 'agent-session-item';
      button.setAttribute('role', 'option');
      button.setAttribute('aria-selected', String(session.id === state.activeId));
      button.setAttribute('aria-current', session.id === state.activeId ? 'true' : 'false');
      button.dataset.sessionId = session.id;
      button.dataset.scope = session.scope === 'chat' ? 'chat' : 'bank';
      button.title = sessionTitle(session);
      button.setAttribute('aria-label', `${sessionTitle(session)}，${sessionScopeLabel(session)}`);
      const mark = document.createElement('span');
      mark.className = 'agent-session-mark';
      mark.setAttribute('aria-hidden', 'true');
      mark.textContent = session.scope === 'chat' ? '聊' : '题';
      const title = document.createElement('strong');
      title.textContent = sessionTitle(session);
      const titleRow = document.createElement('span');
      titleRow.className = 'agent-session-title-row';
      titleRow.appendChild(title);
      const time = sessionTimeLabel(session);
      if (time) {
        const timeNode = document.createElement('time');
        timeNode.className = 'agent-session-time';
        timeNode.textContent = time;
        const stamp = sessionTimestamp(session);
        if (Number.isFinite(stamp)) {
          try { timeNode.dateTime = new Date(stamp).toISOString(); } catch (_) { /* 忽略异常时间戳 */ }
        }
        titleRow.appendChild(timeNode);
      }
      const meta = document.createElement('small');
      meta.textContent = sessionScopeLabel(session);
      button.append(mark, titleRow, meta);
      button.addEventListener('click', () => activate(session.id));
      button.addEventListener('keydown', event => {
        const items = [...list.querySelectorAll('.agent-session-item')];
        const index = items.indexOf(button);
        if (event.key === 'Delete' && !state.busy) {
          event.preventDefault(); deleteSession(session.id);
          return;
        }
        if (event.altKey && ['ArrowUp', 'ArrowDown'].includes(event.key)) {
          event.preventDefault();
          const target = event.key === 'ArrowUp' ? index - 1 : index + 1;
          const targetButton = items[target];
          if (targetButton) reorderSession(session.id, targetButton.dataset.sessionId,
            event.key === 'ArrowUp');
          return;
        }
        let next = -1;
        if (event.key === 'ArrowDown') next = Math.min(items.length - 1, index + 1);
        else if (event.key === 'ArrowUp') next = Math.max(0, index - 1);
        else if (event.key === 'Home') next = 0;
        else if (event.key === 'End') next = items.length - 1;
        if (next >= 0 && items[next] && next !== index) {
          event.preventDefault(); items[next].focus();
        }
      });

      const deleteButton = document.createElement('button');
      deleteButton.type = 'button';
      deleteButton.className = 'agent-session-delete';
      deleteButton.dataset.sessionDelete = session.id;
      deleteButton.draggable = false;
      deleteButton.title = '删除对话';
      deleteButton.setAttribute('aria-label', `删除对话：${sessionTitle(session)}`);
      const deleteGlyph = document.createElement('span');
      deleteGlyph.className = 'agent-session-delete-glyph';
      deleteGlyph.setAttribute('aria-hidden', 'true');
      deleteGlyph.innerHTML = window.QFIcon ? window.QFIcon('x') : '';
      deleteButton.appendChild(deleteGlyph);
      deleteButton.addEventListener('click', event => {
        event.preventDefault(); event.stopPropagation();
        deleteSession(session.id);
      });

      row.append(button, deleteButton);
      row.addEventListener('dragstart', event => {
        if (state.busy || event.target.closest('.agent-session-delete')) {
          event.preventDefault(); return;
        }
        state.draggingSessionId = session.id;
        row.classList.add('is-dragging');
        if (event.dataTransfer) {
          event.dataTransfer.effectAllowed = 'move';
          event.dataTransfer.setData('text/plain', session.id);
        }
      });
      row.addEventListener('dragover', event => {
        if (!state.draggingSessionId || state.draggingSessionId === session.id) return;
        event.preventDefault();
        const rect = row.getBoundingClientRect();
        const before = event.clientY < rect.top + rect.height / 2;
        row.classList.toggle('drop-before', before);
        row.classList.toggle('drop-after', !before);
        if (event.dataTransfer) event.dataTransfer.dropEffect = 'move';
      });
      row.addEventListener('dragleave', event => {
        if (!row.contains(event.relatedTarget)) {
          row.classList.remove('drop-before', 'drop-after');
        }
      });
      row.addEventListener('drop', event => {
        event.preventDefault();
        if (!state.draggingSessionId || state.draggingSessionId === session.id) return;
        const rect = row.getBoundingClientRect();
        reorderSession(state.draggingSessionId, session.id,
          event.clientY < rect.top + rect.height / 2);
        clearSessionDragState();
      });
      row.addEventListener('dragend', clearSessionDragState);
      list.appendChild(row);
    });
  }

  function clearSessionDragState() {
    state.draggingSessionId = null;
    document.querySelectorAll('.agent-session-row').forEach(row => {
      row.classList.remove('is-dragging', 'drop-before', 'drop-after');
    });
  }

  function reorderSession(sourceId, targetId, before = true) {
    const sourceIndex = state.sessions.findIndex(item => String(item.id) === String(sourceId));
    const targetIndex = state.sessions.findIndex(item => String(item.id) === String(targetId));
    if (sourceIndex < 0 || targetIndex < 0 || sourceIndex === targetIndex) return;
    const [source] = state.sessions.splice(sourceIndex, 1);
    let insertAt = state.sessions.findIndex(item => String(item.id) === String(targetId));
    if (insertAt < 0) { state.sessions.splice(sourceIndex, 0, source); return; }
    if (!before) insertAt += 1;
    state.sessions.splice(insertAt, 0, source);
    writeSessionOrder();
    renderSessions();
    setStatus('对话顺序已保存', 'ready');
  }

  function appendMessageNode(box, message) {
    const role = ['user', 'assistant', 'system'].includes(message.role) ? message.role : 'assistant';
    const row = document.createElement('div');
    row.className = `agent-message-row agent-message-row-${role}`;
    if (message.pending) row.classList.add('is-pending');
    if (message.status && message.status !== 'complete') {
      row.classList.add(`is-${message.status}`);
    }

    const body = document.createElement('div');
    body.className = 'agent-message-body';
    const bubble = document.createElement('div');
    bubble.className = `agent-message agent-message-${role}`;
    const content = String(message.content || '');
    if (role === 'assistant' && typeof window.markdownit === 'function') {
      try {
        const renderer = window.markdownit({html: false, linkify: true, breaks: true});
        renderer.validateLink = value => {
          const link = String(value || '').trim().replace(/[\u0000-\u0020]/g, '');
          if (!link || link.startsWith('//')) return false;
          const scheme = link.match(/^([a-z][a-z0-9+.-]*):/i)?.[1]?.toLowerCase();
          return !scheme || ['http', 'https', 'mailto'].includes(scheme);
        };
        bubble.innerHTML = renderer.render(content);
        bubble.querySelectorAll('a').forEach(link => {
          link.target = '_blank'; link.rel = 'noopener noreferrer';
        });
        bubble.querySelectorAll('pre').forEach(pre => {
          const copy = document.createElement('button');
          copy.type = 'button'; copy.className = 'agent-code-copy';
          copy.title = '复制代码'; copy.setAttribute('aria-label', '复制代码');
          copy.innerHTML = window.QFIcon ? window.QFIcon('copy') : '复制';
          copy.addEventListener('click', () => navigator.clipboard?.writeText(
            pre.querySelector('code')?.textContent || pre.textContent || ''));
          pre.appendChild(copy);
        });
      } catch (_) {
        bubble.textContent = content;
      }
    } else {
      bubble.textContent = content;
    }
    body.appendChild(bubble);
    if (message.status === 'stopped' || message.status === 'error') {
      const status = document.createElement('small');
      status.className = 'agent-message-status';
      status.textContent = message.status === 'stopped' ? '已停止' : '生成失败';
      body.appendChild(status);
    }
    row.appendChild(body);
    box.appendChild(row);
    if (role === 'assistant' && typeof window.renderMathInElement === 'function') {
      try {
        window.renderMathInElement(bubble, {
          delimiters: [
            {left: '$$', right: '$$', display: true},
            {left: '\\[', right: '\\]', display: true},
            {left: '\\(', right: '\\)', display: false},
            {left: '$', right: '$', display: false},
          ],
          throwOnError: false,
          ignoredTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code'],
        });
      } catch (_) { /* 保留原 Markdown 文本 */ }
    }
  }

  function approvalStatusText(status) {
    return ({pending: '等待确认', approved: '已确认', cancelled: '已取消',
      expired: '已过期'})[status] || status || '待确认';
  }

  function upsertApproval(approval) {
    if (!approval || !approval.id) return;
    const index = state.approvals.findIndex(item => item.id === approval.id);
    if (index < 0) state.approvals = [approval, ...state.approvals];
    else state.approvals[index] = Object.assign({}, state.approvals[index], approval);
    syncStageApprovalState();
  }

  function parseObject(value) {
    if (value && typeof value === 'object' && !Array.isArray(value)) return value;
    if (typeof value !== 'string') return null;
    try {
      const parsed = JSON.parse(value);
      return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : null;
    } catch (_) {
      return null;
    }
  }

  function approvalArguments(approval) {
    if (!approval) return {};
    return parseObject(approval.arguments) || parseObject(approval.args) ||
      parseObject(approval.parameters) || {};
  }

  function jobIdFrom(value) {
    const object = parseObject(value);
    if (!object) return '';
    const direct = object.job_id || object.task_id || object.job?.id || object.task?.id;
    if (direct) return String(direct);
    for (const key of ['result', 'data', 'payload', 'execution_result']) {
      const nested = jobIdFrom(object[key]);
      if (nested) return nested;
    }
    return '';
  }

  function pendingApprovalForStage(stageId, sessionId) {
    if (!stageId) return null;
    return state.approvals.find(item => {
      if (!item || item.status !== 'pending') return false;
      if (sessionId && item.session_id && String(item.session_id) !== String(sessionId)) return false;
      return String(approvalArguments(item).stage_id || '') === String(stageId);
    }) || null;
  }

  function appendSystemMessage(sessionId, content) {
    const row = state.sessions.find(item => String(item.id) === String(sessionId));
    if (!row || !content) return;
    row.messages = row.messages || [];
    if (!row.messages.some(message => String(message.content || '') === String(content))) {
      row.messages.push({role: 'system', content});
    }
  }

  function syncStageApprovalState() {
    state.stagedCards.forEach(card => {
      const stageId = card.staged?.stage_id || card.staged?.id;
      const pending = pendingApprovalForStage(stageId, card.sessionId);
      card.pendingApprovalId = pending?.id || null;
      if (pending) {
        card.start.disabled = true;
        if (!card.starting) card.status.textContent = '等待确认后启动识别';
      } else if (!card.starting && card.staged?.status === 'staged') {
        card.start.disabled = card.staged.can_start === false;
      }
    });
  }

  function renderApproval() {
    const box = $('agent-approval');
    const current = activeSession();
    if (!box || !current) return;
    const pendingRows = state.approvals.filter(item =>
      item && item.status === 'pending' &&
      (!item.session_id || String(item.session_id) === String(current.id)));
    // 会话切换到“仅聊天”或更换工作目录后，旧计划仍保留在服务端，
    // 但不能在新上下文中继续执行。优先找上下文匹配的计划；若只有旧计划，
    // 只展示失效提示，不渲染可点击的确认按钮。
    const pending = pendingRows.find(item =>
      current.scope !== 'chat' &&
      String(item.scope || 'bank') === String(current.scope || 'bank') &&
      String(item.workdir_id || '') === String(current.workdir_id || '')) ||
      pendingRows[0];
    box.replaceChildren();
    if (!pending) { box.hidden = true; return; }
    const stale = current.scope === 'chat' ||
      String(pending.scope || 'bank') !== String(current.scope || 'bank') ||
      String(pending.workdir_id || '') !== String(current.workdir_id || '');
    box.classList.toggle('is-stale', stale);

    const head = document.createElement('div');
    head.className = 'agent-approval-head';
    const title = document.createElement('strong');
    title.textContent = `${pending.action || '题库写入'} · ${approvalStatusText(pending.status)}`;
    head.appendChild(title);
    const summary = document.createElement('p');
    summary.className = 'agent-approval-summary';
    summary.textContent = stale
      ? '这项待确认操作属于其他工作范围或目录，已暂停执行。请切回原题库工作范围后重新检查，或重新发送任务。'
      : (pending.summary || 'Agent 请求执行一项需要确认的操作。');
    box.append(head, summary);
    const context = pending.context ||
      (pending.workdir_id ? `工作目录：${pending.workdir_id}` : '当前题库');
    if (context) {
      const scope = document.createElement('small');
      scope.className = 'agent-approval-context';
      const mode = pending.mode === 'danger' ? '危险模式' : '标准模式';
      scope.textContent = `${typeof context === 'string' ? context : JSON.stringify(context)} · ${mode}`;
      box.appendChild(scope);
    }

    let args = pending.preview || pending.arguments;
    if (typeof args === 'string') {
      try { args = JSON.parse(args); } catch (_) { /* 保留原始脱敏文本 */ }
    }
    const hasArgs = args && (typeof args === 'object'
      ? Object.keys(args).length : String(args).trim().length);
    if (hasArgs) {
      const details = document.createElement('details');
      const label = document.createElement('summary');
      label.textContent = '查看变更参数';
      const pre = document.createElement('pre');
      try { pre.textContent = typeof args === 'string' ? args : JSON.stringify(args, null, 2); }
      catch (_) { pre.textContent = String(args); }
      details.append(label, pre); box.appendChild(details);
    }

    if (!stale) {
      const actions = document.createElement('div');
      actions.className = 'agent-approval-actions';
      const approve = document.createElement('button');
      approve.type = 'button'; approve.className = 'btn primary agent-approval-action';
      approve.textContent = '确认执行';
      const cancel = document.createElement('button');
      cancel.type = 'button'; cancel.className = 'btn secondary agent-approval-action';
      cancel.textContent = '取消';
      approve.addEventListener('click', () => transitionApproval(pending.id, 'approve', approve, cancel));
      cancel.addEventListener('click', () => transitionApproval(pending.id, 'cancel', approve, cancel));
      actions.append(approve, cancel); box.appendChild(actions);
    }
    box.hidden = false;
  }

  async function refreshApprovals(sessionId = activeSession()?.id) {
    if (!sessionId) return;
    try {
      const data = await request(`/api/agent/approvals?session_id=${encodeURIComponent(sessionId)}`);
      if (!activeSession() || String(activeSession().id) !== String(sessionId)) return;
      state.approvals = data.approvals || [];
      syncStageApprovalState();
      renderApproval();
    } catch (_) {
      // 旧版本后端可能尚未提供确认列表；不影响普通对话和只读工具。
    }
  }

  async function refreshSession(sessionId) {
    try {
      const data = await request(`/api/agent/sessions/${encodeURIComponent(sessionId)}`);
      state.sessions = state.sessions.map(item => item.id === sessionId ? data.session : item);
      if (activeSession()?.id === sessionId) { renderSessions(); renderMessages(); }
      return data.session;
    } catch (_) {
      // 请求结束后再由下一次切换重新同步。
      return null;
    }
  }

  async function reconcileStartedStage(stageId, sessionId, result, fallbackName) {
    if (!stageId) return '';
    const card = state.stagedCards.get(stageId);
    let staged = card?.staged || null;
    let jobId = jobIdFrom(result);
    if (!jobId) {
      try {
        const data = await request(
          `/api/agent/uploads/${encodeURIComponent(stageId)}?session_id=${encodeURIComponent(sessionId)}`);
        staged = data.staged || staged;
        if (card && staged) card.staged = staged;
        jobId = jobIdFrom(staged);
      } catch (_) {
        // 暂存可能已被清理；审批结果仍由会话和审批列表保留。
      }
    }
    if (jobId) {
      appendSystemMessage(sessionId, `已启动暂存文件识别任务：${jobId}`);
      createTaskCard(sessionId, jobId, staged?.original_name || card?.staged?.original_name || fallbackName);
      removeStagedCard(stageId);
      return jobId;
    }
    if (card) {
      card.starting = false;
      card.pendingApprovalId = null;
      const started = staged && staged.status && staged.status !== 'staged';
      card.start.disabled = started || staged?.can_start === false;
      card.status.textContent = started ? '识别已启动，等待任务编号' : '未获取任务编号，可重试';
    }
    return '';
  }

  async function transitionApproval(approvalId, action, approveButton, cancelButton) {
    const current = activeSession();
    if (!current || !approvalId) return;
    const sessionId = current.id;
    const previousApproval = state.approvals.find(item => String(item.id) === String(approvalId));
    approveButton.disabled = true; cancelButton.disabled = true;
    setStatus(action === 'approve' ? '确认中…' : '取消中…', 'busy');
    try {
      const data = await request(`/api/agent/${action}`, {
        method: 'POST', body: JSON.stringify({session_id: sessionId, approval_id: approvalId}),
      });
      const returnedApproval = Object.assign({}, previousApproval || {}, data.approval || {});
      upsertApproval(data.approval || previousApproval);
      const stageId = approvalArguments(returnedApproval).stage_id;
      if (action === 'approve') {
        const result = data.result || returnedApproval.result || {};
        const jobId = jobIdFrom(result) || jobIdFrom(data);
        if (jobId) {
          const staged = stageId ? state.stagedCards.get(stageId) : null;
          appendSystemMessage(sessionId, `已启动暂存文件识别任务：${jobId}`);
          createTaskCard(sessionId, jobId, staged?.staged?.original_name || '暂存文件');
          if (stageId) removeStagedCard(stageId);
        } else if (stageId) {
          // 某些后端会先确认再异步返回 job_id；同步暂存状态可避免重复提交。
          await reconcileStartedStage(stageId, sessionId,
            returnedApproval.result || data.result || {}, '暂存文件');
        }
      }
      syncStageApprovalState();
      await refreshSession(sessionId);
      await refreshApprovals(sessionId);
      if (action === 'cancel' && stageId) {
        const card = state.stagedCards.get(stageId);
        if (card) {
          card.starting = false;
          card.pendingApprovalId = null;
          card.status.textContent = '已取消，可重新启动识别';
          card.start.disabled = card.staged?.can_start === false;
        }
      }
      setStatus(action === 'approve' ? '操作已确认' : '操作已取消', 'ready');
    } catch (error) {
      if (error.payload?.approval) upsertApproval(error.payload.approval);
      syncStageApprovalState();
      setStatus(error.message || '确认操作失败', 'error');
      approveButton.disabled = false; cancelButton.disabled = false;
    }
  }

  function dangerArmedFor(sessionId) {
    return Boolean(state.dangerGrant?.token &&
      String(state.dangerGrant.sessionId) === String(sessionId));
  }

  async function revokeDanger(sessionId, token, {silent = false} = {}) {
    const id = String(sessionId || state.dangerGrant?.sessionId || '');
    const grantToken = token || state.dangerGrant?.token;
    if (!id || !grantToken) return;
    if (dangerArmedFor(id)) state.dangerGrant = null;
    try {
      await request(`/api/agent/sessions/${encodeURIComponent(id)}/danger`, {
        method: 'DELETE', headers: {'X-Agent-Danger-Token': grantToken},
      });
    } catch (error) {
      if (!silent) setStatus(error.message || '关闭危险模式失败', 'error');
    }
  }

  async function setDangerMode(enabled) {
    const current = activeSession();
    if (!current || state.busy) { renderMessages(); return; }
    if (!enabled) {
      await revokeDanger(current.id, state.dangerGrant?.token);
      renderMessages(); setStatus('已切换到标准模式', 'ready');
      return;
    }
    const accepted = window.confirm(
      '危险模式会让 Agent 无需逐次确认即可修改题库、发起付费调用、操作模板，并执行 PowerShell、CMD、Python 等任意本机命令。命令拥有当前 Windows 用户权限。路径校验和安全检查仍然生效。确定仅为当前页面和会话开启吗？');
    if (!accepted) { renderMessages(); return; }
    try {
      const data = await request(`/api/agent/sessions/${encodeURIComponent(current.id)}/danger`, {
        method: 'POST', body: JSON.stringify({acknowledged: true}),
      });
      state.dangerGrant = {sessionId: current.id, token: data.danger_token};
      renderMessages(); setStatus('危险模式已开启', 'ready');
    } catch (error) {
      state.dangerGrant = null;
      renderMessages(); setStatus(error.message || '危险模式开启失败', 'error');
    }
  }

  function renderMessages() {
    const box = $('agent-messages');
    const current = activeSession();
    if (!box || !current) return;
    box.replaceChildren();
    (current.messages || []).forEach(message => appendMessageNode(box, message));
    const chatOnly = current.scope === 'chat';
    if (!current.messages?.length) {
      const welcome = document.createElement('div');
      welcome.className = 'agent-welcome';
      welcome.innerHTML = chatOnly
        ? '<h3>仅聊天</h3>' +
          '<p>这是一个不读取本地文件的对话。需要检索或整理题目时，可随时切换到当前题库。</p>' +
          '<div class="agent-quick-actions"><button type="button" data-agent-quick="帮我梳理一下这份学习计划">梳理学习计划</button>' +
          '<button type="button" data-agent-quick="用简洁的方式解释一个概念">解释概念</button></div>'
        : '<h3>开始一个题库工作流</h3>' +
          '<p>可以搜索、读取和整理题目，也可以导入试卷、查重或生成试卷。写入前会展示变更并等待确认。</p>' +
          '<div class="agent-quick-actions"><button type="button" data-agent-quick="列出目录">列出目录</button>' +
          '<button type="button" data-agent-quick="搜索 微积分">搜索题目</button>' +
          '<button type="button" data-agent-quick="帮我导入一份试卷">导入试卷</button></div>';
      box.appendChild(welcome);
      box.querySelectorAll('[data-agent-quick]').forEach(button => {
        button.addEventListener('click', () => {
          const input = $('agent-input');
          if (!input || state.busy) return;
          input.value = button.dataset.agentQuick || '';
          $('agent-form')?.requestSubmit();
        });
      });
    }
    state.taskCards.forEach(card => {
      if (card.sessionId === current.id) box.appendChild(card.node);
    });
    state.stagedCards.forEach(card => {
      if (card.sessionId === current.id) box.appendChild(card.node);
    });
    box.scrollTop = box.scrollHeight;
    if ($('agent-chat-title')) $('agent-chat-title').textContent = sessionTitle(current);
    if ($('agent-chat-meta')) {
      const scopeLabel = chatOnly
        ? '仅聊天 · 不访问本地文件'
        : `当前题库 · ${workdirDisplayName(current.workdir_id)} · 导出到 ${workdirDisplayName(current.output_dir_id || current.workdir_id)}`;
      $('agent-chat-meta').textContent = `${scopeLabel} · ${providerStatusLabel()}`;
    }
    const effectiveMode = dangerArmedFor(current.id) ? 'danger' : 'standard';
    if ($('agent-mode')) $('agent-mode').value = effectiveMode;
    const permissionLabel = $('agent-permission-label');
    if (permissionLabel) permissionLabel.textContent = effectiveMode === 'danger' ? '完全访问' : '标准';
    const permissionButton = $('agent-scope-toggle');
    if (permissionButton) {
      const workdirLabel = workdirDisplayName(current.workdir_id);
      permissionButton.title = `调整权限和工作范围（当前目录：${workdirLabel}）`;
      permissionButton.setAttribute('aria-label',
        `调整权限和工作范围，当前权限：${effectiveMode === 'danger' ? '完全访问' : '标准'}，工作目录：${workdirLabel}`);
    }
    panel.dataset.mode = effectiveMode;
    const dangerBanner = $('agent-danger-banner');
    if (dangerBanner) dangerBanner.hidden = effectiveMode !== 'danger';
    panel.dataset.scope = current.scope || 'bank';
    if ($('agent-provider')) $('agent-provider').value = current.provider_id || '';
    document.querySelectorAll('input[name="agent-scope"]').forEach(radio => {
      radio.checked = radio.value === (current.scope || 'bank');
    });
    const label = $('agent-scope-label');
    if (label) label.textContent = chatOnly ? '仅聊天' : workdirDisplayName(current.workdir_id);
    const field = $('agent-workdir-field');
    if (field) field.hidden = current.scope === 'chat';
    const output = $('agent-output-dir');
    if (output) output.value = current.output_dir_id || current.workdir_id || '';
    const inputDir = $('agent-input-dir');
    if (inputDir) inputDir.value = current.input_dir_id || current.workdir_id || '';
    const attach = $('agent-attach');
    if (attach) {
      attach.disabled = chatOnly || state.busy;
      attach.title = chatOnly ? '仅聊天模式不可上传文件' : '上传 PDF、DOCX 或 ZIP';
    }
    const input = $('agent-input');
    if (input) input.placeholder = chatOnly ? '输入消息，开始普通对话…' : '描述要处理的题库任务…';
    const hint = $('agent-composer-hint');
    if (hint) hint.textContent = chatOnly ? '仅聊天 · Enter 发送 · Shift+Enter 换行' : '当前题库 · Enter 发送 · Shift+Enter 换行';
    renderApproval();
    renderAttachments();
  }

  function activate(id) {
    if (!state.sessions.some(session => session.id === id)) return;
    if (state.busy && String(id) !== String(state.activeId)) {
      setStatus('当前对话正在处理，完成后再切换', 'busy');
      return;
    }
    if (state.dangerGrant && String(state.dangerGrant.sessionId) !== String(id)) {
      const previousGrant = state.dangerGrant;
      state.dangerGrant = null;
      revokeDanger(previousGrant.sessionId, previousGrant.token, {silent: true});
    }
    state.activeId = id;
    try { localStorage.setItem(ACTIVE_SESSION_KEY, id); } catch (_) { /* 隐私模式下不持久化 */ }
    restoreTaskCards(activeSession());
    restoreStagedCards(activeSession());
    renderSessions(); renderFolderOptions(); renderMessages();
    state.approvals = [];
    refreshApprovals(id);
    $('agent-workspace')?.classList.remove('show-conversations');
    $('agent-toggle-conversations')?.setAttribute('aria-expanded', 'false');
    $('agent-input')?.focus();
  }

  async function deleteSession(id) {
    const session = state.sessions.find(item => String(item.id) === String(id));
    if (!session) return;
    if (state.busy && String(id) === String(state.activeId)) {
      setStatus('当前对话正在处理，完成后再删除', 'busy');
      return;
    }
    if (!window.confirm(`删除对话“${sessionTitle(session)}”？此操作不可恢复。`)) return;
    const index = state.sessions.findIndex(item => String(item.id) === String(id));
    try {
      if (dangerArmedFor(id)) {
        const grant = state.dangerGrant;
        state.dangerGrant = null;
        await revokeDanger(id, grant?.token, {silent: true});
      }
      await request(`/api/agent/sessions/${encodeURIComponent(id)}`, {method: 'DELETE'});
      state.sessions = state.sessions.filter(item => String(item.id) !== String(id));
      writeSessionOrder();
      state.approvals = state.approvals.filter(item => String(item.session_id || '') !== String(id));
      state.taskCards.forEach((card, jobId) => {
        if (String(card.sessionId) !== String(id)) return;
        const timer = state.taskTimers.get(jobId);
        if (timer) window.clearTimeout(timer);
        state.taskTimers.delete(jobId); state.taskCards.delete(jobId);
      });
      state.stagedCards.forEach((card, stageId) => {
        if (String(card.sessionId) === String(id)) state.stagedCards.delete(stageId);
      });
      if (String(state.activeId) === String(id)) {
        state.activeId = null;
        if (state.sessions.length) {
          const next = state.sessions[Math.min(index, state.sessions.length - 1)];
          activate(next.id);
        } else {
          await createSession();
        }
      } else {
        renderSessions(); renderMessages();
      }
      setStatus('对话已删除', 'ready');
    } catch (error) {
      setStatus(error.message || '删除对话失败', 'error');
    }
  }

  async function loadFolders() {
    try {
      state.folders = (await request('/api/agent/tree')).folders || [];
      renderFolderOptions();
    } catch (error) { setStatus(error.message, 'error'); }
  }

  async function loadProviders() {
    try {
      const data = await request('/api/agent/providers');
      state.providers = data.providers || [];
      state.providerPresets = Array.isArray(data.presets) ? data.presets : [];
      renderProviders();
    } catch (error) { setStatus(error.message, 'error'); }
  }

  function availableProviders() {
    return (Array.isArray(state.providers) ? state.providers : [])
      .filter(provider => provider && provider.enabled !== false);
  }

  function providerStatusLabel() {
    const providers = availableProviders();
    if (!providers.length) return '未连接模型 · 本地快捷模式';
    const currentId = activeSession()?.provider_id || '';
    const selected = providers.find(provider => String(provider.id) === String(currentId));
    if (selected) return `已配置 · ${selected.name || selected.model || '模型'} / ${selected.model || ''}`;
    const active = providers.find(provider => provider.active);
    return active
      ? `已配置 · ${active.name || active.model || '全局模型'} / ${active.model || ''}`
      : '已配置模型 · 自动选择';
  }

  function providerEndpoint(provider) {
    const base = String(provider?.base_url || '').replace(/\/+$/, '');
    return base ? `${base}/chat/completions` : '';
  }

  function providerById(id) {
    return availableProviders().find(provider => String(provider.id) === String(id)) || null;
  }

  function currentProvider() {
    const currentId = activeSession()?.provider_id || '';
    return providerById(currentId) || availableProviders().find(provider => provider.active) || null;
  }

  function renderProviderModelOptions(preset) {
    const datalist = $('agent-provider-model-options');
    if (!datalist) return;
    datalist.replaceChildren();
    (preset?.models || []).forEach(model => {
      const option = document.createElement('option');
      option.value = String(model.id || '');
      option.label = String(model.label || model.id || '');
      datalist.appendChild(option);
    });
  }

  function renderProviderPresets() {
    const select = $('agent-provider-preset');
    if (!select) return;
    const current = select.value;
    select.replaceChildren(new Option('自定义 OpenAI 兼容服务', ''));
    state.providerPresets.forEach(preset => {
      if (!preset || !preset.id) return;
      select.add(new Option(String(preset.label || preset.id), String(preset.id)));
    });
    select.value = state.providerPresets.some(item => String(item.id) === String(current))
      ? current : '';
    const preset = state.providerPresets.find(item => String(item.id) === String(select.value));
    renderProviderModelOptions(preset);
  }

  function applyProviderPreset(id) {
    const preset = state.providerPresets.find(item => String(item.id) === String(id));
    const form = $('agent-provider-form');
    if (!preset || !form) {
      renderProviderModelOptions(null);
      return;
    }
    const model = preset.models?.[0] || {};
    if (form.elements.name) form.elements.name.value = preset.name || preset.label || '';
    if (form.elements.base_url) form.elements.base_url.value = preset.base_url || '';
    if (form.elements.model) form.elements.model.value = model.id || '';
    if (form.elements.max_tokens && model.max_tokens) {
      form.elements.max_tokens.value = String(model.max_tokens);
    }
    if (form.elements.supports_tools) form.elements.supports_tools.checked = true;
    if (form.elements.supports_vision) {
      form.elements.supports_vision.checked = Boolean(model.supports_vision);
    }
    renderProviderModelOptions(preset);
    form.elements.api_key?.focus();
  }

  function showProviderSettings() {
    const popover = $('agent-provider-popover');
    if (!popover) return;
    popover.hidden = false;
    loadProviders();
    $('agent-provider-form')?.elements.name?.focus();
  }

  function resetProviderForm() {
    const form = $('agent-provider-form');
    if (!form) return;
    state.providerEditingId = null;
    form.reset();
    if (form.elements.supports_tools) form.elements.supports_tools.checked = true;
    if (form.elements.max_tokens) form.elements.max_tokens.value = '8192';
    if (form.elements.wire_api) form.elements.wire_api.value = 'chat';
    if (form.elements.reasoning_effort) form.elements.reasoning_effort.value = '';
    if (form.elements.service_tier) form.elements.service_tier.value = '';
    if (form.elements.store_responses) form.elements.store_responses.checked = false;
    const title = form.querySelector('.agent-provider-form-title');
    if (title) title.textContent = '添加 Provider';
    const submit = form.querySelector('button[type="submit"]');
    if (submit) submit.textContent = '添加模型';
    const cancel = $('agent-provider-edit-cancel');
    if (cancel) cancel.hidden = true;
    const status = $('agent-provider-form-status');
    if (status) status.textContent = '';
  }

  function editProvider(provider) {
    const form = $('agent-provider-form');
    if (!form || !provider) return;
    state.providerEditingId = String(provider.id);
    form.elements.preset.value = '';
    form.elements.name.value = provider.name || '';
    form.elements.model.value = provider.model || '';
    form.elements.base_url.value = provider.base_url || '';
    form.elements.api_key.value = '';
    form.elements.api_key.placeholder = provider.key_configured
      ? '已配置密钥，留空则保持不变' : '请输入 API Key';
    form.elements.max_tokens.value = String(provider.max_tokens || 8192);
    form.elements.wire_api.value = provider.wire_api || 'chat';
    form.elements.reasoning_effort.value = provider.reasoning_effort || '';
    form.elements.service_tier.value = provider.service_tier || '';
    form.elements.store_responses.checked = provider.store_responses === true;
    form.elements.supports_tools.checked = provider.supports_tools !== false;
    form.elements.supports_vision.checked = provider.supports_vision === true;
    const title = form.querySelector('.agent-provider-form-title');
    if (title) title.textContent = '编辑 Provider';
    const submit = form.querySelector('button[type="submit"]');
    if (submit) submit.textContent = '保存修改';
    const cancel = $('agent-provider-edit-cancel');
    if (cancel) cancel.hidden = false;
    form.elements.name.focus();
  }

  function renderProviders() {
    const providers = availableProviders();
    const statusLabel = providerStatusLabel();
    renderProviderPresets();
    const select = $('agent-provider');
    if (select) {
      const current = activeSession()?.provider_id || '';
      select.replaceChildren(new Option(
        providers.length ? '自动（全局模型）' : '未连接模型 · 本地快捷模式', ''));
      providers.forEach(provider => {
        const label = `${provider.name} · ${provider.model}`;
        select.add(new Option(label, provider.id));
      });
      select.value = providers.some(provider => String(provider.id) === String(current))
        ? current : '';
      // 没有 Provider 时仍保持可聚焦、可点击，旁边的配置入口会给出明确下一步。
      select.disabled = false;
      select.classList.toggle('is-unconfigured', !providers.length);
      select.title = providers.length
        ? '选择本次对话使用的模型'
        : '尚未连接模型；点击“配置模型”添加 Provider';
      select.setAttribute('aria-label', providers.length
        ? 'Agent 对话模型'
        : 'Agent 对话模型，尚未连接，当前使用本地快捷模式');
    }
    const status = $('agent-provider-status');
    if (status) {
      status.textContent = statusLabel;
      // 未连接时在工具栏保留一行明确提示；已有 Provider 时由对话头部显示
      // 当前模型，避免工具栏在窄面板里被状态文本挤成多行。
      status.hidden = Boolean(providers.length);
      status.dataset.state = providers.length ? 'connected' : 'unconfigured';
      const selectedProvider = currentProvider();
      status.title = selectedProvider
        ? `请求端点：${providerEndpoint(selectedProvider)}`
        : '当前不会调用外部模型，使用本地快捷指令';
    }
    if (panel) panel.dataset.providerState = providers.length ? 'connected' : 'unconfigured';
    const settings = $('agent-provider-settings');
    if (settings) {
      settings.title = providers.length ? '管理 Agent 模型' : '配置 Agent 模型';
      settings.setAttribute('aria-label', providers.length ? '管理 Agent 模型' : '配置 Agent 模型');
      settings.classList.toggle('is-unconfigured', !providers.length);
    }
    const list = $('agent-provider-list');
    if (!list) return;
    list.replaceChildren();
    if (!providers.length) {
      const empty = document.createElement('div');
      empty.className = 'agent-provider-empty';
      const title = document.createElement('strong');
      title.textContent = '未连接模型';
      const detail = document.createElement('span');
      detail.textContent = '当前使用本地快捷模式；设置页里的识别模型不会自动连接。请在下方选择预设并填写 Agent API Key。';
      const add = document.createElement('button');
      add.type = 'button';
      add.className = 'agent-provider-empty-action';
      add.textContent = '配置模型';
      add.addEventListener('click', () => {
        $('agent-provider-form')?.elements.name?.focus();
      });
      empty.append(title, detail, add);
      list.appendChild(empty);
      return;
    }
    state.providers.forEach(provider => {
      const row = document.createElement('div');
      row.className = 'agent-provider-row';
      const info = document.createElement('div');
      info.className = 'agent-provider-row-copy';
      const name = document.createElement('strong'); name.textContent = provider.name;
      const meta = document.createElement('small');
      const endpoint = providerEndpoint(provider);
      meta.textContent = [
        provider.model,
        endpoint,
        provider.active ? '全局默认' : '',
        provider.wire_api === 'responses' ? 'Responses' : 'Chat',
        provider.key_configured ? '' : '本地无 Key',
      ].filter(Boolean).join(' · ');
      if (endpoint) meta.title = `请求端点：${endpoint}`;
      info.append(name, meta);
      const actions = document.createElement('div'); actions.className = 'agent-provider-row-actions';
      const activateButton = document.createElement('button');
      activateButton.type = 'button'; activateButton.className = 'agent-provider-action';
      activateButton.textContent = provider.active ? '已启用' : (provider.enabled === false ? '已停用' : '启用');
      activateButton.disabled = Boolean(provider.active) || provider.enabled === false;
      activateButton.addEventListener('click', () => activateProvider(provider.id));
      const testButton = document.createElement('button');
      testButton.type = 'button'; testButton.className = 'agent-provider-action';
      testButton.textContent = '测试';
      testButton.addEventListener('click', () => testProvider(provider.id, testButton));
      const editButton = document.createElement('button');
      editButton.type = 'button'; editButton.className = 'agent-provider-action';
      editButton.textContent = '编辑';
      editButton.addEventListener('click', () => editProvider(provider));
      const removeButton = document.createElement('button');
      removeButton.type = 'button'; removeButton.className = 'agent-provider-action danger';
      removeButton.textContent = '删除';
      removeButton.addEventListener('click', () => removeProvider(provider));
      actions.append(activateButton, testButton, editButton, removeButton); row.append(info, actions); list.appendChild(row);
    });
  }

  async function activateProvider(id) {
    try {
      await request(`/api/agent/providers/${encodeURIComponent(id)}/activate`, {method: 'POST'});
      await loadProviders(); setStatus('模型已切换', 'ready');
    } catch (error) { setStatus(error.message, 'error'); }
  }

  async function removeProvider(provider) {
    if (!window.confirm(`删除 Agent Provider“${provider.name}”？`)) return;
    try {
      await request(`/api/agent/providers/${encodeURIComponent(provider.id)}`, {method: 'DELETE'});
      await loadProviders(); setStatus('模型已删除', 'ready');
    } catch (error) { setStatus(error.message, 'error'); }
  }

  async function testProvider(id, button) {
    const original = button.textContent;
    button.disabled = true; button.textContent = '测试中';
    try {
      const data = await request(`/api/agent/providers/${encodeURIComponent(id)}/test`, {method: 'POST'});
      setStatus(data.reply || '连接成功', 'ready');
    } catch (error) { setStatus(error.message, 'error'); }
    finally { button.disabled = false; button.textContent = original; }
  }

  async function saveContext(changes = {}) {
    const current = activeSession();
    if (!current) return;
    const scope = changes.scope ?? document.querySelector('input[name="agent-scope"]:checked')?.value ?? current.scope;
    const workdir = scope === 'bank' ? (changes.workdir ?? $('agent-workdir')?.value ?? '') : '';
    const outputDir = scope === 'bank'
      ? (changes.output_dir ?? $('agent-output-dir')?.value ?? workdir) : '';
    const inputDir = scope === 'bank'
      ? (changes.input_dir ?? $('agent-input-dir')?.value ?? workdir) : '';
    const providerId = changes.provider_id === undefined ? (current.provider_id || null) : (changes.provider_id || null);
    try {
      setStatus('保存中…', 'busy');
      const data = await request(`/api/agent/sessions/${encodeURIComponent(current.id)}`, {
        method: 'PATCH', body: JSON.stringify({scope, workdir, output_dir: outputDir,
          input_dir: inputDir, provider_id: providerId}),
      });
      state.sessions = state.sessions.map(item => item.id === current.id ? data.session : item);
      state.approvals = [];
      renderSessions(); renderFolderOptions(); renderMessages();
      refreshApprovals(current.id); setStatus('就绪', 'ready');
    } catch (error) {
      setStatus(error.message, 'error');
      // 后端拒绝时重新渲染服务端快照，避免选择器停留在未保存状态。
      renderMessages(); renderFolderOptions();
    }
  }

  async function createSession(scope, workdir) {
    const previous = activeSession();
    const nextScope = scope || previous?.scope || 'bank';
    const nextWorkdir = workdir === undefined
      ? (nextScope === 'bank' ? (previous?.workdir_id || '') : '') : workdir;
    try {
      setStatus('创建中…', 'busy');
      const data = await request('/api/agent/sessions', {
        method: 'POST', body: JSON.stringify({scope: nextScope, workdir: nextWorkdir,
          input_dir: nextWorkdir}),
      });
      state.sessions.unshift(data.session);
      // 新建对话放在最前面；用户手动拖动过的旧对话顺序仍保留。
      state.sessions = applySessionOrder(state.sessions);
      const createdIndex = state.sessions.findIndex(item => item.id === data.session.id);
      if (createdIndex > 0) {
        const [created] = state.sessions.splice(createdIndex, 1);
        state.sessions.unshift(created);
      }
      writeSessionOrder();
      activate(data.session.id); setStatus('就绪', 'ready');
    } catch (error) { setStatus(error.message, 'error'); }
  }

  async function syncSessions() {
    const data = await request('/api/agent/sessions');
    state.sessions = applySessionOrder(data.sessions || []);
    if (state.sessions.length) writeSessionOrder();
    if (!state.sessions.length) await createSession();
    else if (state.activeId && state.sessions.some(item => item.id === state.activeId)) activate(state.activeId);
    else activate(state.sessions[0].id);
  }

  function requestParentAgentOpen() {
    if (!isEmbedded || !parentAgentPanel()) return false;
    try {
      window.parent.postMessage({source: 'quizforge', type: 'open-agent'}, window.location.origin);
      return true;
    } catch (_) { return false; }
  }

  async function openAgent() {
    if (state.opening) return;
    state.opening = true; setOpen(true); setStatus('连接中…', 'busy');
    try {
      await Promise.all([syncSessions(), loadFolders(), loadProviders()]);
      setStatus('就绪', 'ready');
    } catch (error) { setStatus(error.message, 'error'); }
    finally { state.opening = false; }
  }

  function setBusy(value) {
    state.busy = value;
    const send = $('agent-send');
    if (send) {
      send.disabled = false;
      send.dataset.mode = value ? 'stop' : 'send';
      send.title = value ? '停止生成' : '发送';
      send.setAttribute('aria-label', value ? '停止生成' : '发送');
      const icon = send.querySelector('[data-agent-send-icon]');
      if (icon && window.QFIcon) {
        icon.innerHTML = window.QFIcon(value ? 'square' : 'arrow-up', 'agent-send-mark');
      }
    }
    const input = $('agent-input');
    if (input) input.disabled = value;
    renderMessages();
  }

  function toolLabel(name) {
    return ({
      list_folders: '读取目录', browse_quizforge: '浏览 QuizForge 数据', search_questions: '搜索题目', read_question: '读取题目',
      check_duplicates: '检查重复题', inspect_conversion: '检查识别结果',
      start_conversion: '启动试卷识别', import_conversion: '导入识别结果',
      export_questions: '导出试卷', create_folder: '创建目录', update_question: '修改题目',
    })[name] || String(name || '题库工具');
  }

  function toolEventDetails(event) {
    const labels = {detail: '详情', arguments: '参数', result: '结果', error: '错误'};
    return Object.entries(labels).flatMap(([key, label]) => {
      const value = event?.[key];
      if (value === undefined || value === null || value === '') return [];
      let text;
      try {
        text = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
      } catch (_) {
        text = String(value);
      }
      return text ? [{label, text}] : [];
    });
  }

  function setStatusFromSession(session, fallbackText = '出错') {
    const assistant = [...(session?.messages || [])].reverse()
      .find(message => message.role === 'assistant');
    if (assistant?.status === 'stopped') setStatus('已停止', 'ready');
    else if (assistant?.status === 'error') setStatus('出错', 'error');
    else if (assistant && !assistant.pending) setStatus('就绪', 'ready');
    else setStatus(fallbackText, 'error');
  }

  function showEvents(events) {
    const box = $('agent-messages');
    if (!box) return;
    (events || []).forEach(event => {
      if (event.type === 'approval' || event.type === 'approval_required') {
        const approval = Object.assign({}, event.approval || event);
        if (!approval.id && event.approval_id) approval.id = event.approval_id;
        if (!approval.session_id) approval.session_id = activeSession()?.id;
        if (!approval.status) approval.status = 'pending';
        upsertApproval(approval);
        return;
      }
      if (!['tool', 'tool_state'].includes(event.type)) return;
      const detailRows = toolEventDetails(event);
      const node = document.createElement(detailRows.length ? 'details' : 'div');
      node.className = `agent-tool-event agent-tool-event-${event.status || 'done'}`;
      const summary = document.createElement(detailRows.length ? 'summary' : 'div');
      summary.className = 'agent-tool-event-summary';
      const marker = document.createElement('span');
      marker.className = 'agent-tool-event-marker';
      marker.setAttribute('aria-hidden', 'true');
      const text = document.createElement('span');
      const label = toolLabel(event.name);
      text.textContent = event.status === 'running' ? `正在执行：${label}…` :
        (event.status === 'error' ? `${label}失败` :
          (event.status === 'awaiting_confirmation' ? `${label}等待确认` : `${label}已完成`));
      summary.append(marker, text);
      node.appendChild(summary);
      if (detailRows.length) {
        const body = document.createElement('div');
        body.className = 'agent-tool-event-detail';
        detailRows.forEach(row => {
          const section = document.createElement('section');
          const heading = document.createElement('strong');
          heading.textContent = row.label;
          const content = document.createElement('pre');
          content.textContent = row.text;
          section.append(heading, content);
          body.appendChild(section);
        });
        node.appendChild(body);
      }
      box.appendChild(node);
    });
    renderApproval();
    box.scrollTop = box.scrollHeight;
  }

  async function sendMessage(event) {
    event.preventDefault();
    const current = activeSession();
    const input = $('agent-input');
    const content = input?.value.trim();
    if (!current || !content || state.busy) return;
    const sessionId = current.id;
    input.value = '';
    current.messages = current.messages || [];
    current.messages.push({role: 'user', content, pending: true});
    const pendingAssistant = {role: 'assistant', content: '', pending: true, status: 'complete'};
    current.messages.push(pendingAssistant);
    renderMessages(); renderSessions(); setBusy(true); setStatus('处理中…', 'busy');
    let responseEvents = [];
    let terminalEvent = null;
    let lastSequence = 0;
    let renderFrame = null;
    state.abortController = new AbortController();
    state.activeTurnId = null;
    setActivity('正在理解并处理请求…', true);

    function scheduleStreamRender() {
      if (renderFrame !== null) return;
      const schedule = window.requestAnimationFrame || (callback => window.setTimeout(callback, 16));
      renderFrame = schedule(() => {
        renderFrame = null;
        if (activeSession()?.id === sessionId) {
          renderMessages(); showEvents(responseEvents);
        }
      });
    }

    function handleStreamEvent(streamEvent) {
      const seq = Number(streamEvent.seq || 0);
      if (seq && seq <= lastSequence) return;
      if (seq) lastSequence = seq;
      if (streamEvent.type === 'turn_started') {
        state.activeTurnId = streamEvent.turn_id;
        setActivity('正在生成回复…', true);
      } else if (streamEvent.type === 'assistant_delta') {
        pendingAssistant.content += String(streamEvent.delta || '');
        scheduleStreamRender();
      } else if (streamEvent.type === 'tool_state' || streamEvent.type === 'approval') {
        responseEvents.push(streamEvent);
        if (streamEvent.type === 'approval') {
          const approval = Object.assign({}, streamEvent.approval || streamEvent);
          if (approval.id) upsertApproval(approval);
        }
        setActivity(streamEvent.status === 'running' ?
          `正在执行：${toolLabel(streamEvent.name)}…` : '正在整理结果…', true);
        scheduleStreamRender();
      } else if (streamEvent.type === 'error') {
        setStatus(streamEvent.error || '生成失败', 'error');
      } else if (streamEvent.type === 'turn_finished') {
        terminalEvent = streamEvent;
        if (streamEvent.session) {
          state.sessions = state.sessions.map(item => item.id === sessionId ? streamEvent.session : item);
        }
        setStatus(streamEvent.status === 'stopped' ? '已停止' :
          (streamEvent.status === 'error' ? '出错' : '就绪'),
        streamEvent.status === 'error' ? 'error' : 'ready');
      }
    }

    async function sendLegacy() {
      const data = await request('/api/agent/message', {
        method: 'POST', body: JSON.stringify({session_id: current.id, content,
          provider_id: current.provider_id || null}),
        signal: state.abortController.signal,
      });
      state.sessions = state.sessions.map(item => item.id === sessionId ? data.session : item);
      responseEvents = data.events || [];
      (data.approvals || []).forEach(upsertApproval);
      if (data.approval) upsertApproval(data.approval);
      setStatus('就绪', 'ready');
    }

    try {
      const supportsStream = typeof window.ReadableStream === 'function' &&
        typeof window.TextDecoder === 'function';
      if (supportsStream) {
        try {
          await streamRequest('/api/agent/message/stream', {
            method: 'POST', body: JSON.stringify({session_id: current.id, content,
              provider_id: current.provider_id || null}),
            signal: state.abortController.signal,
          }, handleStreamEvent);
        } catch (error) {
          if ([404, 405].includes(error.status)) await sendLegacy();
          else throw error;
        }
        if (!terminalEvent && !state.abortController.signal.aborted) {
          throw new Error('流式响应未返回终态');
        }
      } else {
        await sendLegacy();
      }
    } catch (error) {
      if (error.name === 'AbortError') {
        const reconciled = await refreshSession(sessionId);
        setStatusFromSession(reconciled, '连接已中断');
        return;
      }
      let reconciled = null;
      if (error.payload?.session) {
        reconciled = error.payload.session;
        state.sessions = state.sessions.map(item => item.id === sessionId ? reconciled : item);
        (error.payload.approvals || []).forEach(upsertApproval);
        if (error.payload.approval) upsertApproval(error.payload.approval);
      } else {
        reconciled = await refreshSession(sessionId);
      }
      if (!reconciled) {
        const row = state.sessions.find(item => item.id === sessionId);
        if (row) {
          row.messages = (row.messages || []).filter(message => !message.pending);
          row.messages.push({role: 'assistant', content: `请求失败：${error.message}`});
        }
      }
      setStatusFromSession(reconciled, '出错');
    } finally {
      if (renderFrame !== null && window.cancelAnimationFrame) {
        window.cancelAnimationFrame(renderFrame);
      }
      state.abortController = null;
      state.activeTurnId = null;
      setActivity('', false);
      setBusy(false); renderSessions();
      if (activeSession()?.id === sessionId) {
        renderMessages(); showEvents(responseEvents); refreshApprovals(sessionId);
      }
    }
  }

  function createTaskCard(sessionId, jobId, filename) {
    if (!jobId) return null;
    const existing = state.taskCards.get(jobId);
    if (existing) return existing.node;
    const node = document.createElement('div');
    node.className = 'agent-task-card'; node.dataset.jobId = jobId;
    const title = document.createElement('strong'); title.textContent = filename || '上传文件';
    const status = document.createElement('span'); status.className = 'agent-task-status'; status.textContent = '排队中';
    const link = document.createElement('a'); link.hidden = true; link.textContent = '打开转换任务';
    link.href = `/convert/file/${encodeURIComponent(jobId)}`; link.target = '_blank'; link.rel = 'noopener';
    node.append(title, status, link);
    state.taskCards.set(jobId, {sessionId, node, status, link});
    pollTask(jobId);
    return node;
  }

  function restoreTaskCards(session) {
    if (!session) return;
    // 转换任务 id 会随系统消息写入会话；页面刷新后从消息恢复卡片并继续轮询。
    const text = (session.messages || []).map(message => String(message.content || '')).join('\n');
    const ids = text.match(/(?:转换任务|任务(?:编号|ID)?)[：:]\s*([A-Za-z0-9_-]{8,80})/g) || [];
    ids.forEach(item => {
      const match = item.match(/([A-Za-z0-9_-]{8,80})$/);
      if (match) createTaskCard(session.id, match[1], '转换任务');
    });
  }

  async function restoreStagedCards(session) {
    if (!session) return;
    const text = (session.messages || []).map(message => String(message.content || '')).join('\n');
    const matches = text.match(/(?:暂存编号|stage_id)[：:]\s*([A-Za-z0-9_-]{12,80})/gi) || [];
    for (const item of matches) {
      const idMatch = item.match(/([A-Za-z0-9_-]{12,80})$/);
      const stageId = idMatch?.[1];
      if (!stageId || state.stagedCards.has(stageId)) continue;
      try {
        const data = await request(`/api/agent/uploads/${encodeURIComponent(stageId)}?session_id=${encodeURIComponent(session.id)}`);
        if (activeSession()?.id !== session.id || !data.staged) continue;
        if (data.staged.job_id) {
          appendSystemMessage(session.id, `已启动暂存文件识别任务：${data.staged.job_id}`);
          createTaskCard(session.id, data.staged.job_id, data.staged.original_name || '暂存文件');
          if (activeSession()?.id === session.id) renderMessages();
          continue;
        }
        createStagedCard(session.id, data.staged, data.staged.original_name);
        if (activeSession()?.id === session.id) renderMessages();
      } catch (_) {
        // 暂存可能已被用户移除或过期；会话正文仍保留历史提示。
      }
    }
  }

  function removeStagedCard(stageId) {
    const card = state.stagedCards.get(stageId);
    if (!card) return;
    card.node.remove();
    state.stagedCards.delete(stageId);
    renderAttachments();
  }

  async function discardStage(stageId) {
    const current = activeSession();
    if (!current || !stageId) return;
    const card = state.stagedCards.get(stageId);
    if (card?.starting || card?.pendingApprovalId) {
      setStatus('请先取消待确认的识别操作', 'error');
      return;
    }
    const button = card?.discard;
    if (button) button.disabled = true;
    try {
      await request(`/api/agent/uploads/${encodeURIComponent(stageId)}/discard`, {
        method: 'POST', body: JSON.stringify({session_id: card?.sessionId || current.id}),
      });
      removeStagedCard(stageId);
      setStatus('已移除暂存文件', 'ready');
    } catch (error) {
      if (button) button.disabled = false;
      setStatus(error.message || '移除暂存文件失败', 'error');
    }
  }

  async function startStaged(stageId) {
    const current = activeSession();
    const card = state.stagedCards.get(stageId);
    if (!current || !card || current.scope === 'chat' || card.starting || card.pendingApprovalId ||
        card.staged?.can_start === false) return;
    card.starting = true;
    const button = card.start;
    if (button) button.disabled = true;
    if (card.status) card.status.textContent = '正在启动识别…';
    setStatus('启动识别任务…', 'busy');
    try {
      const choices = card.choices || {};
      const ocrBackend = choices.ocr?.value || '';
      const engine = choices.engine?.value || '';
      const normalizationMode = choices.normalization?.value || '';
      const data = await request('/api/agent/tool', {
        method: 'POST',
        body: JSON.stringify({session_id: card.sessionId || current.id,
          name: 'start_conversion', arguments: {stage_id: stageId,
            ocr_backend: ocrBackend, engine, normalization_mode: normalizationMode}}),
      });
      const result = data.result || {};
      if (result.choice_required) {
        card.starting = false;
        if (card.status) card.status.textContent = '请先完成识别方案选择';
        setStatus(result.message || '请先选择识别方案', 'ready');
        return;
      }
      if (result.approval) upsertApproval(result.approval);
      if (result.pending_confirmation || result.approval) {
        card.starting = false;
        card.pendingApprovalId = result.approval?.id || pendingApprovalForStage(stageId, card.sessionId)?.id || null;
        if (card.status) card.status.textContent = '等待确认后启动识别';
        if (button) button.disabled = true;
        renderApproval(); refreshApprovals(current.id);
        setStatus('等待确认', 'ready');
        return;
      }
      card.starting = false;
      const jobId = jobIdFrom(result) || jobIdFrom(data);
      if (!jobId) throw new Error(result.message || '后端未返回识别任务编号');
      appendSystemMessage(card.sessionId || current.id, `已启动暂存文件识别任务：${jobId}`);
      createTaskCard(card.sessionId || current.id, jobId, card.staged?.original_name || '暂存文件');
      removeStagedCard(stageId);
      renderSessions(); renderMessages();
      setStatus('识别任务已启动', 'ready');
    } catch (error) {
      // 后端可能在 4xx 中同时返回审批对象，仍要把它展示出来，避免用户丢失确认入口。
      if (error.payload?.result?.approval) upsertApproval(error.payload.result.approval);
      if (error.payload?.approval) upsertApproval(error.payload.approval);
      renderApproval();
      card.starting = false;
      if (button) button.disabled = false;
      if (card.status) card.status.textContent = '启动失败，可重试';
      setStatus(error.message || '启动识别失败', 'error');
    }
  }

  function createStagedCard(sessionId, staged, fallbackName) {
    const stageId = staged?.stage_id || staged?.id;
    if (!stageId) return null;
    const existing = state.stagedCards.get(stageId);
    if (existing) {
      existing.staged = Object.assign({}, existing.staged, staged);
      existing.title.textContent = existing.staged.original_name || fallbackName || '上传 ZIP';
      const files = Array.isArray(existing.staged.files) ? existing.staged.files : [];
      existing.status.textContent = existing.staged.status === 'started'
        ? '识别已启动，正在等待任务状态'
        : `已安全暂存 ${files.length} 个可识别文件，等待处理`;
      existing.start.disabled = existing.starting || Boolean(existing.pendingApprovalId) ||
        existing.staged.can_start === false;
      syncStageApprovalState();
      return existing.node;
    }
    const node = document.createElement('div');
    node.className = 'agent-task-card agent-staged-card';
    node.dataset.stageId = stageId;
    const title = document.createElement('strong');
    title.textContent = staged.original_name || fallbackName || '上传 ZIP';
    const status = document.createElement('span');
    status.className = 'agent-task-status';
    const files = Array.isArray(staged.files) ? staged.files : [];
    status.textContent = staged.status === 'started'
      ? '识别已启动，正在等待任务状态'
      : `已安全暂存 ${files.length} 个可识别文件，等待处理`;
    const choicesNode = document.createElement('div');
    choicesNode.className = 'agent-staged-choices';
    const makeChoice = (label, options) => {
      const wrap = document.createElement('label');
      wrap.textContent = label;
      const select = document.createElement('select');
      select.className = 'input';
      select.add(new Option(`选择${label}`, ''));
      options.forEach(option => select.add(new Option(option.label, option.id)));
      wrap.appendChild(select); choicesNode.appendChild(wrap);
      return select;
    };
    const ocr = makeChoice('识别后端', [{id: 'mineru', label: 'MinerU'}, {id: 'doc2x', label: 'Doc2X'}]);
    const engine = makeChoice('导入方式', [{id: 'block', label: '逐题切分'}, {id: 'whole', label: '整篇规范化'}]);
    const normalization = makeChoice('规范化', [{id: 'mechanical', label: '机械，不调用 LLM'}, {id: 'llm', label: 'LLM'}, {id: 'review', label: '人工审核'}]);
    const discard = document.createElement('button');
    discard.type = 'button'; discard.className = 'agent-staged-discard';
    discard.textContent = '移除'; discard.title = '丢弃暂存文件';
    discard.addEventListener('click', () => discardStage(stageId));
    const start = document.createElement('button');
    start.type = 'button'; start.className = 'agent-staged-start';
    start.textContent = '开始识别'; start.title = '启动暂存文件识别任务';
    start.disabled = staged.status !== 'staged' || staged.can_start === false;
    start.addEventListener('click', () => startStaged(stageId));
    const details = document.createElement('details');
    const summary = document.createElement('summary');
    summary.textContent = '查看文件清单';
    const list = document.createElement('ul');
    files.slice(0, 100).forEach(file => {
      const item = document.createElement('li');
      const role = file.role ? ` · ${file.role}` : '';
      item.textContent = `${file.name || file.path || '未命名文件'}${role}`;
      list.appendChild(item);
    });
    if (files.length > 100) {
      const more = document.createElement('li');
      more.textContent = `其余 ${files.length - 100} 个文件未展开`;
      list.appendChild(more);
    }
    details.append(summary, list);
    node.append(title, status, choicesNode, start, discard, details);
    state.stagedCards.set(stageId, {
      sessionId, node, title, status, start, discard, staged,
      starting: false, pendingApprovalId: pendingApprovalForStage(stageId, sessionId)?.id || null,
      choices: {ocr, engine, normalization},
    });
    syncStageApprovalState();
    return node;
  }

  function renderAttachments() {
    const box = $('agent-attachments');
    const current = activeSession();
    if (!box || !current) return;
    box.replaceChildren();
    const cards = [...state.stagedCards.values()].filter(item => item.sessionId === current.id);
    if (!cards.length) { box.hidden = true; return; }
    cards.forEach(card => {
      const chip = document.createElement('span');
      chip.className = 'agent-attachment-chip';
      chip.title = card.staged?.original_name || '暂存文件';
      chip.appendChild(document.createTextNode(card.staged?.original_name || '暂存文件'));
      const remove = document.createElement('button');
      remove.type = 'button'; remove.className = 'agent-attachment-remove';
      remove.setAttribute('aria-label', '移除暂存文件'); remove.title = '移除';
      remove.innerHTML = window.QFIcon ? window.QFIcon('x') : '';
      remove.addEventListener('click', () => discardStage(card.staged?.stage_id || card.staged?.id));
      chip.appendChild(remove); box.appendChild(chip);
    });
    box.hidden = false;
  }

  function pollTask(jobId) {
    if (state.taskTimers.has(jobId)) return;
    let timer;
    const update = async () => {
      const card = state.taskCards.get(jobId);
      if (!card) {
        window.clearInterval(timer);
        state.taskTimers.delete(jobId);
        return;
      }
      try {
        const sessionQuery = card.sessionId
          ? `?session_id=${encodeURIComponent(card.sessionId)}` : '';
        const data = await request(
          `/api/agent/tasks/${encodeURIComponent(jobId)}${sessionQuery}`);
        const done = ['done', 'awaiting_block_review', 'completed'].includes(data.status);
        card.status.textContent = data.status === 'error' ? `失败：${data.error || '未知错误'}` :
          (done ? (data.status === 'awaiting_block_review' ? '识别完成，等待校对' : '识别完成，可前往校对')
            : `处理中 · ${data.status || '等待'}`);
        if (done) {
          card.link.hidden = false; card.link.href = `/batches`;
          window.clearInterval(timer); state.taskTimers.delete(jobId);
        } else if (data.status === 'error') {
          window.clearInterval(timer); state.taskTimers.delete(jobId);
        }
      } catch (error) {
        if (error.status === 404) {
          if (card) card.status.textContent = '任务状态已过期，请前往转换任务查看';
          window.clearInterval(timer); state.taskTimers.delete(jobId);
        } else if (error.status === 403) {
          card.status.textContent = '任务不属于当前 Agent 会话';
          window.clearInterval(timer); state.taskTimers.delete(jobId);
        }
        // 其他临时网络错误留给下一轮轮询。
      }
    };
    update();
    timer = window.setInterval(update, 1800);
    state.taskTimers.set(jobId, timer);
  }

  async function uploadFile() {
    const input = $('agent-file');
    const current = activeSession();
    const files = Array.from(input?.files || []);
    if (!files.length || !current) return;
    if (current.scope === 'chat') {
      setStatus('仅聊天模式不能上传文件', 'error'); input.value = ''; return;
    }
    const sessionId = current.id;
    setBusy(true); setStatus(`提交 ${files.length} 个文件…`, 'busy');
    const failures = [];
    try {
      for (const file of files) {
        const form = new FormData();
        form.append('session_id', sessionId); form.append('file', file);
        try {
          const data = await request('/api/agent/upload', {method: 'POST', body: form});
          state.sessions = state.sessions.map(item => item.id === sessionId ? data.session : item);
          const row = state.sessions.find(item => item.id === sessionId);
          if (data.job_id) {
            createTaskCard(sessionId, data.job_id, data.filename || file.name);
            row.messages = row.messages || [];
            if (!(row.messages || []).some(message =>
              String(message.content || '').includes(String(data.job_id)))) {
              // 把任务编号写入会话正文；刷新页面后 restoreTaskCards 才能恢复
              // 卡片并继续带会话归属轮询，而不是只留下无法追踪的提示文字。
              row.messages.push({role: 'system', content: `已提交“${data.filename || file.name}”转换任务，任务编号：${data.job_id}`});
            }
          } else if (data.staged) {
            createStagedCard(sessionId, data.staged, data.filename || file.name);
            row.messages = row.messages || [];
            const stageId = data.staged.id || data.staged.stage_id;
            if (!(row.messages || []).some(message =>
              String(message.content || '').includes(String(stageId || '')))) {
              row.messages.push({role: 'system', content: `已安全暂存“${data.filename || file.name}”，暂存编号：${stageId || '未知'}，请确认文件清单后继续。`});
            }
          } else {
            row.messages = row.messages || [];
            row.messages.push({role: 'system', content: `已接收“${data.filename || file.name}”。`});
          }
          renderSessions(); renderMessages();
        } catch (error) {
          failures.push(`${file.name}：${error.message || '上传失败'}`);
        }
      }
      if (failures.length) setStatus(`部分文件失败：${failures.join('；')}`, 'error');
      else setStatus('文件已提交', 'ready');
    } finally {
      input.value = '';
    }
    setBusy(false);
  }

  function desktopBridge() {
    try {
      if (window.pywebview?.api) return window.pywebview.api;
      if (window.QuizForgeDesktop?.api) return window.QuizForgeDesktop.api();
    } catch (_) { /* 浏览器或跨域嵌入环境没有桌面桥 */ }
    return null;
  }

  function setScopeStatus(text, isError = false) {
    const node = $('agent-scope-status');
    if (!node) return;
    node.textContent = text || '';
    node.classList.toggle('is-error', Boolean(isError));
  }

  function chooseWorkdirFromFiles(files) {
    const select = $('agent-workdir');
    const first = files?.[0];
    if (!select || !first) return;
    // 浏览器出于安全原因不会暴露绝对路径，只能利用 webkitRelativePath
    // 与后端返回的相对目录 id 匹配；匹配不到时保留当前选择并给出提示。
    const path = String(first.webkitRelativePath || '').replace(/\\/g, '/');
    const parts = path.split('/');
    if (parts.length > 1) parts.pop();
    const relative = parts.join('/');
    const option = [...select.options].find(item => item.value === relative);
    if (option) {
      select.value = relative;
      setScopeStatus(`已选择：${relative || '题库根目录'}`);
      saveContext({workdir: relative});
    } else {
      setScopeStatus('浏览器未提供绝对路径，请在下拉框中选择题库内目录', true);
    }
  }

  async function browseWorkdir() {
    const picker = $('agent-directory-picker');
    const api = desktopBridge();
    try {
      // 桌面壳若提供专用接口，优先使用它；旧版本只有题库目录选择器，
      // 也可以安全地把它作为根目录选择的降级方案。
      const browse = api?.browse_agent_directory || api?.browse_bank_directory;
      if (browse) {
        setScopeStatus('正在打开目录选择器…');
        const result = await browse.call(api);
        if (result?.cancelled) return;
        if (!result?.ok) throw new Error(result?.error || '目录选择失败');
        const relative = String(result.workdir_id || result.relative_path || '').replace(/\\/g, '/');
        const select = $('agent-workdir');
        const option = [...(select?.options || [])].find(item => item.value === relative);
        if (option) {
          select.value = relative;
          if ($('agent-output-dir')) $('agent-output-dir').value = relative;
          setScopeStatus(`已选择：${relative || '题库根目录'}`);
          await saveContext({workdir: relative, output_dir: relative});
        } else {
          // 目录树可能尚未刷新或该目录为空，仍允许用后端返回的相对 id 保存；
          // 后端会再次执行边界和存在性校验。
          if (select && relative) select.add(new Option(relative, relative));
          if (select) select.value = relative;
          if ($('agent-output-dir')) $('agent-output-dir').value = relative;
          const manual = $('agent-workdir-manual');
          if (manual) manual.value = relative;
          setScopeStatus(`已选择：${relative || '题库根目录'}`);
          await saveContext({workdir: relative, output_dir: relative});
        }
        return;
      }
      const manual = $('agent-workdir-manual');
      const value = window.prompt('请输入题库内相对目录（留空表示题库根目录）', manual?.value || '');
      if (value !== null) {
        const relative = value.trim().replace(/\\/g, '/').replace(/^\/+|\/+$/g, '');
        if (manual) manual.value = relative;
        const select = $('agent-workdir');
        if (select) select.value = relative;
        if ($('agent-output-dir')) $('agent-output-dir').value = relative;
        await saveContext({workdir: relative, output_dir: relative});
      }
    } catch (error) {
      setScopeStatus(error.message || '目录选择失败', true);
    }
  }

  $('agent-close')?.addEventListener('click', () => setOpen(false));
  $('agent-backdrop')?.addEventListener('click', () => setOpen(false));
  function openFromTrigger() {
    if (requestParentAgentOpen()) return;
    openAgent();
  }
  openButton?.addEventListener('click', openFromTrigger);
  embeddedOpenButton?.addEventListener('click', openFromTrigger);
  if (isEmbedded && !parentAgentPanel() && embeddedOpenButton) {
    document.documentElement.classList.add('embedded-agent-local');
    embeddedOpenButton.hidden = false;
  }
  // 给外壳或嵌入宿主一个稳定的程序化入口，便于导航按钮和自动化测试复用同一逻辑。
  window.QuizForgeAgent = Object.assign(window.QuizForgeAgent || {}, {
    open: openFromTrigger,
    close: () => setOpen(false),
  });
  $('agent-new')?.addEventListener('click', () => createSession());
  $('agent-new-small')?.addEventListener('click', () => createSession());
  $('agent-empty-new')?.addEventListener('click', () => createSession());
  $('agent-form')?.addEventListener('submit', sendMessage);
  $('agent-attach')?.addEventListener('click', () => { if (!state.busy) $('agent-file')?.click(); });
  $('agent-file')?.addEventListener('change', uploadFile);
  $('agent-session-search')?.addEventListener('input', renderSessions);
  $('agent-mode')?.addEventListener('change', () => {
    setDangerMode($('agent-mode').value === 'danger');
  });
  $('agent-danger-exit')?.addEventListener('click', () => setDangerMode(false));
  $('agent-provider')?.addEventListener('change', () => {
    if (!availableProviders().length) {
      showProviderSettings();
      return;
    }
    saveContext({provider_id: $('agent-provider').value});
  });
  $('agent-scope-done')?.addEventListener('click', () => {
    const menu = $('agent-scope-menu');
    if (menu) menu.hidden = true;
    $('agent-scope-toggle')?.setAttribute('aria-expanded', 'false');
    saveContext();
  });
  $('agent-scope-toggle')?.addEventListener('click', () => {
    const menu = $('agent-scope-menu');
    if (!menu) return;
    menu.hidden = !menu.hidden;
    if (!menu.hidden) $('agent-workspace')?.classList.remove('show-conversations');
    $('agent-scope-toggle')?.setAttribute('aria-expanded', String(!menu.hidden));
  });
  document.querySelectorAll('input[name="agent-scope"]').forEach(radio => radio.addEventListener('change', () => {
    const field = $('agent-workdir-field');
    if (field) field.hidden = radio.value === 'chat' && radio.checked;
  }));
  $('agent-workdir')?.addEventListener('change', () => {
    const value = $('agent-workdir').value;
    // 切换工作目录时默认把导出目录一起带过去，避免旧导出目录越过新边界。
    if ($('agent-output-dir')) $('agent-output-dir').value = value;
    saveContext({workdir: value, output_dir: value});
  });
  $('agent-workdir-manual')?.addEventListener('keydown', event => {
    if (event.key !== 'Enter') return;
    event.preventDefault();
    const value = event.currentTarget.value.trim().replace(/\\/g, '/').replace(/^\/+|\/+$/g, '');
    if ($('agent-workdir')) $('agent-workdir').value = value;
    saveContext({workdir: value});
  });
  $('agent-workdir-apply')?.addEventListener('click', () => {
    const value = String($('agent-workdir-manual')?.value || '').trim()
      .replace(/\\/g, '/').replace(/^\/+|\/+$/g, '');
    if ($('agent-workdir')) $('agent-workdir').value = value;
    saveContext({workdir: value});
  });
  $('agent-output-dir')?.addEventListener('change', () => {
    saveContext({output_dir: $('agent-output-dir').value});
  });
  $('agent-input-dir')?.addEventListener('change', () => {
    saveContext({input_dir: $('agent-input-dir').value});
  });
  $('agent-input-dir-apply')?.addEventListener('click', () => {
    const select = $('agent-input-dir');
    if (!select) return;
    const value = window.prompt('请输入题库内材料目录，相对路径留空表示跟随题库目录', select.value || '');
    if (value === null) return;
    const relative = value.trim().replace(/\\/g, '/').replace(/^\/+|\/+$/g, '');
    if (![...select.options].some(option => option.value === relative)) {
      setScopeStatus('材料目录必须先在题库目录树中存在', true);
      return;
    }
    select.value = relative;
    saveContext({input_dir: relative});
  });
  $('agent-workdir-browse')?.addEventListener('click', browseWorkdir);
  $('agent-workdir-refresh')?.addEventListener('click', loadFolders);
  $('agent-directory-picker')?.addEventListener('change', event => {
    chooseWorkdirFromFiles(event.target.files);
    event.target.value = '';
  });
  function setConversationDrawer(open) {
    const workspace = $('agent-workspace');
    if (!workspace) return;
    workspace.classList.toggle('show-conversations', Boolean(open));
    $('agent-toggle-conversations')?.setAttribute('aria-expanded', String(Boolean(open)));
  }
  $('agent-toggle-conversations')?.addEventListener('click', () => {
    const workspace = $('agent-workspace');
    if (!workspace) return;
    setConversationDrawer(!workspace.classList.contains('show-conversations'));
  });
  $('agent-conversation-backdrop')?.addEventListener('click', () => setConversationDrawer(false));
  $('agent-chat-menu')?.addEventListener('click', () => {
    const current = activeSession();
    if (current) deleteSession(current.id);
  });
  $('agent-provider-settings')?.addEventListener('click', () => {
    const popover = $('agent-provider-popover');
    if (!popover) return;
    if (popover.hidden) showProviderSettings();
    else popover.hidden = true;
  });
  $('agent-provider-close')?.addEventListener('click', () => { $('agent-provider-popover').hidden = true; });
  $('agent-provider-preset')?.addEventListener('change', event => {
    applyProviderPreset(event.currentTarget.value);
  });
  $('agent-provider-form')?.elements.model?.addEventListener('change', event => {
    const presetId = $('agent-provider-preset')?.value;
    const preset = state.providerPresets.find(item => String(item.id) === String(presetId));
    const model = (preset?.models || []).find(item => String(item.id) === String(event.currentTarget.value));
    const form = $('agent-provider-form');
    if (model && form?.elements.max_tokens && model.max_tokens) {
      form.elements.max_tokens.value = String(model.max_tokens);
    }
    if (model && form?.elements.supports_vision) {
      form.elements.supports_vision.checked = Boolean(model.supports_vision);
    }
  });
  $('agent-provider-form')?.addEventListener('submit', async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const status = $('agent-provider-form-status');
    const payload = Object.fromEntries(new FormData(form).entries());
    payload.max_tokens = Number(payload.max_tokens || 8192);
    payload.supports_tools = Boolean(form.elements.supports_tools?.checked);
    payload.supports_vision = Boolean(form.elements.supports_vision?.checked);
    payload.store_responses = Boolean(form.elements.store_responses?.checked);
    try {
      if (status) status.textContent = '保存中…';
      const editingId = state.providerEditingId;
      const url = editingId
        ? `/api/agent/providers/${encodeURIComponent(editingId)}`
        : '/api/agent/providers';
      await request(url, {method: editingId ? 'PATCH' : 'POST', body: JSON.stringify(payload)});
      await loadProviders();
      resetProviderForm();
      if (status) status.textContent = editingId ? '已保存修改' : '已添加';
    } catch (error) { if (status) status.textContent = error.message; }
  });
  $('agent-provider-edit-cancel')?.addEventListener('click', resetProviderForm);
  $('agent-cc-import')?.addEventListener('click', async () => {
    const configFile = $('agent-cc-config')?.files?.[0];
    const authFile = $('agent-cc-auth')?.files?.[0];
    const status = $('agent-provider-form-status');
    if (!configFile) { if (status) status.textContent = '请选择 CC Switch 的 config.toml'; return; }
    try {
      if (status) status.textContent = '正在导入…';
      await request('/api/agent/providers/import-cc', {
        method: 'POST',
        body: JSON.stringify({
          config_text: await configFile.text(),
          auth_text: authFile ? await authFile.text() : '{}',
        }),
      });
      await loadProviders();
      if ($('agent-cc-config')) $('agent-cc-config').value = '';
      if ($('agent-cc-auth')) $('agent-cc-auth').value = '';
      if (status) status.textContent = 'CC Switch 配置已导入';
    } catch (error) { if (status) status.textContent = error.message; }
  });
  $('agent-input')?.addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); $('agent-form')?.requestSubmit(); }
  });
  async function cancelActiveTurn() {
    if (!state.abortController) return;
    if (!state.activeTurnId) {
      setStatus('正在建立生成连接…', 'busy');
      return;
    }
    const current = activeSession();
    setStatus('正在停止…', 'busy');
    setActivity('正在停止生成…', true);
    try {
      await request(`/api/agent/turns/${encodeURIComponent(state.activeTurnId)}/cancel`, {
        method: 'POST', body: JSON.stringify({session_id: current?.id || ''}),
      });
    } catch (error) {
      setStatus(error.message || '停止失败', 'error');
    }
  }
  $('agent-cancel')?.addEventListener('click', cancelActiveTurn);
  $('agent-send')?.addEventListener('click', event => {
    if (!state.busy) return;
    event.preventDefault();
    cancelActiveTurn();
  });
  function closeAgentOverlays() {
    let closed = false;
    const scopeMenu = $('agent-scope-menu');
    if (scopeMenu && !scopeMenu.hidden) {
      scopeMenu.hidden = true;
      $('agent-scope-toggle')?.setAttribute('aria-expanded', 'false');
      closed = true;
    }
    const providerPopover = $('agent-provider-popover');
    if (providerPopover && !providerPopover.hidden) {
      providerPopover.hidden = true;
      closed = true;
    }
    const workspace = $('agent-workspace');
    if (workspace?.classList.contains('show-conversations')) {
      workspace.classList.remove('show-conversations');
      closed = true;
    }
    return closed;
  }
  document.addEventListener('pointerdown', event => {
    if (!panel || panel.hidden || !panel.contains(event.target)) return;
    if (!event.target.closest('#agent-scope-toggle, #agent-scope-menu')) {
      const menu = $('agent-scope-menu');
      if (menu && !menu.hidden) {
        menu.hidden = true;
        $('agent-scope-toggle')?.setAttribute('aria-expanded', 'false');
      }
    }
    if (!event.target.closest('#agent-provider-settings, #agent-provider-popover')) {
      const popover = $('agent-provider-popover');
      if (popover && !popover.hidden) popover.hidden = true;
    }
  });
  document.addEventListener('keydown', event => {
    if (event.key !== 'Escape' || panel.hidden) return;
    if (closeAgentOverlays()) event.preventDefault();
    else setOpen(false);
  });
})();
