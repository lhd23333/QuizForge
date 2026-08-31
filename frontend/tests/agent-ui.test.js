import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import {JSDOM} from 'jsdom';

const source = fs.readFileSync(
  new URL('../../static/js/agent.js', import.meta.url), 'utf8');
const markdownSource = fs.readFileSync(
  new URL('../../static/js/agent-markdown.bundle.js', import.meta.url), 'utf8');

function response(payload, status = 200) {
  return {ok: status >= 200 && status < 300, status,
    json: async () => payload};
}

function flush() {
  return new Promise(resolve => setTimeout(resolve, 0));
}

function domMarkup() {
  return `<!doctype html><html><body>
    <button id="agent-open"></button>
    <button id="agent-backdrop" hidden></button>
    <aside id="agent-dialog" hidden>
      <span id="agent-status"></span><button id="agent-close"></button>
      <div id="agent-toolbar">
        <button id="agent-scope-toggle"></button><div id="agent-scope-menu" hidden></div>
        <select id="agent-mode"><option value="standard">标准</option><option value="danger">危险</option></select>
        <select id="agent-provider"></select><button id="agent-provider-settings"></button>
        <span id="agent-provider-status"></span>
      </div>
      <div id="agent-danger-banner" hidden><button id="agent-danger-exit"></button></div>
      <section id="agent-provider-popover" hidden><form id="agent-provider-form">
        <select id="agent-provider-preset" name="preset"></select>
        <input name="name"><input name="model"><input name="base_url">
        <input name="api_key"><input name="max_tokens" value="8192">
        <input name="supports_tools" type="checkbox" checked>
        <input name="supports_vision" type="checkbox">
      </form>
        <div id="agent-provider-list"></div><button id="agent-provider-close"></button></section>
      <input type="radio" name="agent-scope" value="bank" checked>
      <input type="radio" name="agent-scope" value="chat">
      <div id="agent-workdir-field"><select id="agent-workdir"></select></div>
      <button id="agent-scope-done"></button><button id="agent-workdir-browse"></button>
      <button id="agent-workdir-refresh"></button><input id="agent-directory-picker">
      <div id="agent-workspace">
        <button id="agent-conversation-backdrop"></button>
        <aside id="agent-conversation-pane"><div id="agent-sessions"></div>
          <div id="agent-sessions-empty" hidden><span></span><button id="agent-empty-new"></button></div>
          <input id="agent-session-search">
        </aside>
        <div class="agent-conversation-resizer"></div>
        <section id="agent-chat"><button id="agent-toggle-conversations"></button>
          <button id="agent-chat-menu"></button><div id="agent-chat-title"></div>
          <div id="agent-chat-meta"></div><div id="agent-messages"></div>
          <div id="agent-activity" hidden><span id="agent-activity-text"></span><button id="agent-cancel"></button></div>
          <div id="agent-approval" hidden></div><form id="agent-form">
            <div id="agent-attachments" hidden></div><textarea id="agent-input"></textarea>
            <button id="agent-attach"></button><input id="agent-file" type="file">
            <button id="agent-send"><span data-agent-send-icon></span></button><span id="agent-composer-hint"></span>
          </form>
        </section>
      </div>
    </aside>
  </body></html>`;
}

test('Agent 会话支持拖放排序、逐条删除并恢复本机顺序', async () => {
  const dom = new JSDOM(domMarkup(), {
    url: 'http://localhost/', pretendToBeVisual: true, runScripts: 'outside-only',
  });
  const sessions = [
    {id: 'a', scope: 'bank', workdir: '/bank', workdir_id: '', mode: 'standard',
      provider_id: null, messages: [{role: 'user', content: '第一个'}], created_at: 1, updated_at: 2},
    {id: 'b', scope: 'bank', workdir: '/bank', workdir_id: '', mode: 'standard',
      provider_id: null, messages: [{role: 'user', content: '第二个'}], created_at: 1, updated_at: 1},
  ];
  const calls = [];
  dom.window.confirm = () => true;
  dom.window.fetch = async (url, options = {}) => {
    calls.push([String(url), options.method || 'GET']);
    if (url === '/api/agent/sessions') return response({ok: true, sessions});
    if (String(url).startsWith('/api/agent/approvals')) return response({ok: true, approvals: []});
    if (url === '/api/agent/tree') return response({ok: true, folders: []});
    if (url === '/api/agent/providers') return response({ok: true, providers: [], presets: []});
    if (String(url).includes('/api/agent/sessions/') && options.method === 'DELETE') {
      const id = String(url).split('/').pop();
      const index = sessions.findIndex(item => item.id === id);
      if (index >= 0) sessions.splice(index, 1);
      return response({ok: true});
    }
    return response({ok: true, session: sessions[0]});
  };
  dom.window.eval(source);
  dom.window.QuizForgeAgent.open();
  await flush(); await flush(); await flush();

  const list = dom.window.document.getElementById('agent-sessions');
  assert.deepEqual([...list.querySelectorAll('.agent-session-item')]
    .map(node => node.dataset.sessionId), ['a', 'b']);
  const rows = [...list.querySelectorAll('.agent-session-row')];
  assert.equal(rows[0].draggable, true);
  const transfer = {effectAllowed: '', dropEffect: '', setData() {}};
  const start = new dom.window.Event('dragstart', {bubbles: true, cancelable: true});
  Object.defineProperty(start, 'dataTransfer', {value: transfer});
  rows[0].dispatchEvent(start);
  const drop = new dom.window.Event('drop', {bubbles: true, cancelable: true});
  Object.defineProperty(drop, 'dataTransfer', {value: transfer});
  Object.defineProperty(drop, 'clientY', {value: 1});
  rows[1].dispatchEvent(drop);
  assert.deepEqual([...list.querySelectorAll('.agent-session-item')]
    .map(node => node.dataset.sessionId), ['b', 'a']);
  assert.match(dom.window.localStorage.getItem('quizforge.agent.session-order.v1'), /b.*a/);

  const deleteButton = list.querySelector('[data-session-delete="b"]');
  deleteButton.click();
  await flush(); await flush();
  assert.deepEqual([...list.querySelectorAll('.agent-session-item')]
    .map(node => node.dataset.sessionId), ['a']);
  assert.ok(calls.some(([url, method]) => method === 'DELETE' && url.endsWith('/b')));
  dom.window.QuizForgeAgent.close();
  await flush();
});

test('Agent Provider 预设可填充兼容地址、模型和能力参数', async () => {
  const dom = new JSDOM(domMarkup(), {
    url: 'http://localhost/', pretendToBeVisual: true, runScripts: 'outside-only',
  });
  const session = {id: 'preset-session', scope: 'bank', workdir: '/bank', workdir_id: '',
    mode: 'standard', provider_id: null, messages: [], created_at: 1, updated_at: 1};
  const presets = [{id: 'deepseek', label: 'DeepSeek', name: 'DeepSeek',
    base_url: 'https://api.deepseek.com', models: [
      {id: 'deepseek-chat', label: 'DeepSeek Chat', max_tokens: 32768},
    ]}];
  dom.window.fetch = async url => {
    if (url === '/api/agent/sessions') return response({ok: true, sessions: [session]});
    if (String(url).startsWith('/api/agent/approvals')) return response({ok: true, approvals: []});
    if (url === '/api/agent/tree') return response({ok: true, folders: []});
    if (url === '/api/agent/providers') return response({ok: true, providers: [], presets});
    return response({ok: true, session});
  };
  dom.window.eval(source);
  dom.window.QuizForgeAgent.open();
  await flush(); await flush(); await flush();
  dom.window.document.getElementById('agent-provider-settings').click();
  await flush(); await flush();

  const select = dom.window.document.getElementById('agent-provider-preset');
  assert.equal(select.options.length, 2);
  select.value = 'deepseek';
  select.dispatchEvent(new dom.window.Event('change', {bubbles: true}));
  const form = dom.window.document.getElementById('agent-provider-form');
  assert.equal(form.elements.name.value, 'DeepSeek');
  assert.equal(form.elements.base_url.value, 'https://api.deepseek.com');
  assert.equal(form.elements.model.value, 'deepseek-chat');
  assert.equal(form.elements.max_tokens.value, '32768');
  dom.window.QuizForgeAgent.close();
  await flush();
});

test('Agent SSE 可跨任意 UTF-8 字节边界增量渲染并接收终态', async () => {
  const dom = new JSDOM(domMarkup(), {
    url: 'http://localhost/', pretendToBeVisual: true, runScripts: 'outside-only',
  });
  dom.window.TextDecoder = TextDecoder;
  dom.window.ReadableStream = ReadableStream;
  const session = {id: 'stream-session', scope: 'chat', workdir: null, workdir_id: '',
    mode: 'standard', provider_id: null, messages: [], created_at: 1, updated_at: 1};
  const finalSession = {...session, messages: [
    {role: 'user', content: '你好', status: 'complete', turn_id: 'turn-1'},
    {role: 'assistant', content: '你好', status: 'complete', turn_id: 'turn-1'},
  ]};
  const encoder = new TextEncoder();
  const wire = encoder.encode([
    'event: turn_started\ndata: {"type":"turn_started","turn_id":"turn-1","seq":1}\n\n',
    'event: assistant_delta\ndata: {"type":"assistant_delta","turn_id":"turn-1","seq":2,"delta":"你"}\n\n',
    'event: assistant_delta\ndata: {"type":"assistant_delta","turn_id":"turn-1","seq":3,"delta":"好"}\n\n',
    `event: turn_finished\ndata: ${JSON.stringify({type: 'turn_finished', turn_id: 'turn-1',
      seq: 4, status: 'complete', session: finalSession})}\n\n`,
  ].join(''));
  dom.window.fetch = async (url) => {
    if (url === '/api/agent/sessions') return response({ok: true, sessions: [session]});
    if (String(url).startsWith('/api/agent/approvals')) return response({ok: true, approvals: []});
    if (url === '/api/agent/tree') return response({ok: true, folders: []});
    if (url === '/api/agent/providers') return response({ok: true, providers: [], presets: []});
    if (url === '/api/agent/message/stream') {
      return new Response(new ReadableStream({
        start(controller) {
          // 刻意切在中文多字节序列内部，验证 TextDecoder 的 stream 模式。
          [7, 83, 121, 164, wire.length].reduce((start, end) => {
            controller.enqueue(wire.slice(start, end));
            return end;
          }, 0);
          controller.close();
        },
      }), {status: 200, headers: {'Content-Type': 'text/event-stream'}});
    }
    return response({ok: true, session});
  };
  dom.window.eval(source);
  dom.window.QuizForgeAgent.open();
  await flush(); await flush(); await flush();
  const input = dom.window.document.getElementById('agent-input');
  input.value = '你好';
  dom.window.document.getElementById('agent-form').dispatchEvent(
    new dom.window.Event('submit', {bubbles: true, cancelable: true}));
  await new Promise(resolve => setTimeout(resolve, 40));

  const messages = [...dom.window.document.querySelectorAll('.agent-message')]
    .map(node => node.textContent.trim());
  assert.deepEqual(messages, ['你好', '你好']);
  assert.equal(dom.window.document.querySelector('.agent-message-avatar'), null);
  assert.equal(dom.window.document.getElementById('agent-status').textContent, '就绪');
  dom.window.QuizForgeAgent.close();
});

test('Agent 停止按钮调用后端取消并保留 stopped 部分回复', async () => {
  const dom = new JSDOM(domMarkup(), {
    url: 'http://localhost/', pretendToBeVisual: true, runScripts: 'outside-only',
  });
  dom.window.TextDecoder = TextDecoder;
  dom.window.ReadableStream = ReadableStream;
  const session = {id: 'cancel-session', scope: 'chat', workdir: null, workdir_id: '',
    mode: 'standard', provider_id: null, messages: [], created_at: 1, updated_at: 1};
  const calls = [];
  let streamController;
  const encoder = new TextEncoder();
  dom.window.fetch = async (url, options = {}) => {
    calls.push(String(url));
    if (url === '/api/agent/sessions') return response({ok: true, sessions: [session]});
    if (String(url).startsWith('/api/agent/approvals')) return response({ok: true, approvals: []});
    if (url === '/api/agent/tree') return response({ok: true, folders: []});
    if (url === '/api/agent/providers') return response({ok: true, providers: [], presets: []});
    if (url === '/api/agent/message/stream') {
      return new Response(new ReadableStream({
        start(controller) {
          streamController = controller;
          controller.enqueue(encoder.encode(
            'event: turn_started\ndata: {"type":"turn_started","turn_id":"turn-stop","seq":1}\n\n' +
            'event: assistant_delta\ndata: {"type":"assistant_delta","turn_id":"turn-stop","seq":2,"delta":"部分"}\n\n'));
        },
      }), {status: 200});
    }
    if (String(url).includes('/api/agent/turns/turn-stop/cancel')) {
      const finalSession = {...session, messages: [
        {role: 'user', content: '开始', status: 'complete', turn_id: 'turn-stop'},
        {role: 'assistant', content: '部分', status: 'stopped', turn_id: 'turn-stop'},
      ]};
      streamController.enqueue(encoder.encode(
        `event: turn_finished\ndata: ${JSON.stringify({type: 'turn_finished', turn_id: 'turn-stop',
          seq: 3, status: 'stopped', session: finalSession})}\n\n`));
      streamController.close();
      return response({ok: true, turn: {turn_id: 'turn-stop', status: 'cancelling'}});
    }
    return response({ok: true, session});
  };
  dom.window.eval(source);
  dom.window.QuizForgeAgent.open();
  await flush(); await flush(); await flush();
  dom.window.document.getElementById('agent-input').value = '开始';
  dom.window.document.getElementById('agent-form').dispatchEvent(
    new dom.window.Event('submit', {bubbles: true, cancelable: true}));
  await new Promise(resolve => setTimeout(resolve, 20));
  dom.window.document.getElementById('agent-cancel').click();
  await new Promise(resolve => setTimeout(resolve, 40));

  assert.ok(calls.some(url => url.includes('/api/agent/turns/turn-stop/cancel')));
  assert.equal(dom.window.document.querySelector('.agent-message-assistant').textContent.trim(), '部分');
  assert.equal(dom.window.document.querySelector('.agent-message-status').textContent, '已停止');
  assert.equal(dom.window.document.getElementById('agent-status').textContent, '已停止');
  dom.window.QuizForgeAgent.close();
});

test('Agent 工具状态使用可展开详情且模型内容只作为安全文本', async () => {
  const dom = new JSDOM(domMarkup(), {
    url: 'http://localhost/', pretendToBeVisual: true, runScripts: 'outside-only',
  });
  dom.window.TextDecoder = TextDecoder;
  dom.window.ReadableStream = ReadableStream;
  const session = {id: 'tool-session', scope: 'chat', workdir: null, workdir_id: '',
    mode: 'standard', provider_id: null, messages: [], created_at: 1, updated_at: 1};
  const finalSession = {...session, messages: [
    {role: 'user', content: '搜索', status: 'complete', turn_id: 'turn-tool'},
    {role: 'assistant', content: '完成', status: 'complete', turn_id: 'turn-tool'},
  ]};
  const unsafe = '<img src=x onerror=alert(1)>';
  const encoder = new TextEncoder();
  const wire = [
    'event: turn_started\ndata: {"type":"turn_started","turn_id":"turn-tool","seq":1}\n\n',
    `event: tool_state\ndata: ${JSON.stringify({type: 'tool_state', turn_id: 'turn-tool',
      seq: 2, name: 'search_questions', status: 'running', arguments: {query: unsafe}})}\n\n`,
    `event: tool_state\ndata: ${JSON.stringify({type: 'tool_state', turn_id: 'turn-tool',
      seq: 3, name: 'search_questions', status: 'done', result: {total: 1}})}\n\n`,
    `event: turn_finished\ndata: ${JSON.stringify({type: 'turn_finished', turn_id: 'turn-tool',
      seq: 4, status: 'complete', session: finalSession})}\n\n`,
  ].join('');
  dom.window.fetch = async url => {
    if (url === '/api/agent/sessions') return response({ok: true, sessions: [session]});
    if (String(url).startsWith('/api/agent/approvals')) return response({ok: true, approvals: []});
    if (url === '/api/agent/tree') return response({ok: true, folders: []});
    if (url === '/api/agent/providers') return response({ok: true, providers: [], presets: []});
    if (url === '/api/agent/message/stream') {
      return new Response(new ReadableStream({start(controller) {
        controller.enqueue(encoder.encode(wire)); controller.close();
      }}), {status: 200});
    }
    return response({ok: true, session});
  };
  dom.window.eval(source);
  dom.window.QuizForgeAgent.open();
  await flush(); await flush(); await flush();
  dom.window.document.getElementById('agent-input').value = '搜索';
  dom.window.document.getElementById('agent-form').dispatchEvent(
    new dom.window.Event('submit', {bubbles: true, cancelable: true}));
  await new Promise(resolve => setTimeout(resolve, 40));

  const rows = [...dom.window.document.querySelectorAll('details.agent-tool-event')];
  assert.equal(rows.length, 2);
  assert.match(rows[0].querySelector('summary').textContent, /正在执行：搜索题目/);
  assert.match(rows[0].querySelector('pre').textContent, /<img src=x/);
  assert.equal(rows[0].querySelector('img'), null);
  assert.equal(rows[0].open, false);
  dom.window.QuizForgeAgent.close();
});

test('Agent 普通断流会读取服务端终态而不构造本地失败消息', async () => {
  const dom = new JSDOM(domMarkup(), {
    url: 'http://localhost/', pretendToBeVisual: true, runScripts: 'outside-only',
  });
  dom.window.TextDecoder = TextDecoder;
  dom.window.ReadableStream = ReadableStream;
  const session = {id: 'disconnect-session', scope: 'chat', workdir: null, workdir_id: '',
    mode: 'standard', provider_id: null, messages: [], created_at: 1, updated_at: 1};
  const finalSession = {...session, messages: [
    {role: 'user', content: '继续', status: 'complete', turn_id: 'turn-disconnect'},
    {role: 'assistant', content: '部分', status: 'stopped', turn_id: 'turn-disconnect'},
  ]};
  let reconciled = 0;
  const encoder = new TextEncoder();
  dom.window.fetch = async url => {
    if (url === '/api/agent/sessions') return response({ok: true, sessions: [session]});
    if (url === '/api/agent/sessions/disconnect-session') {
      reconciled += 1;
      return response({ok: true, session: finalSession});
    }
    if (String(url).startsWith('/api/agent/approvals')) return response({ok: true, approvals: []});
    if (url === '/api/agent/tree') return response({ok: true, folders: []});
    if (url === '/api/agent/providers') return response({ok: true, providers: [], presets: []});
    if (url === '/api/agent/message/stream') {
      return new Response(new ReadableStream({start(controller) {
        controller.enqueue(encoder.encode(
          'event: turn_started\ndata: {"type":"turn_started","turn_id":"turn-disconnect","seq":1}\n\n' +
          'event: assistant_delta\ndata: {"type":"assistant_delta","turn_id":"turn-disconnect","seq":2,"delta":"部分"}\n\n'));
        controller.error(new Error('socket closed'));
      }}), {status: 200});
    }
    return response({ok: true, session});
  };
  dom.window.eval(source);
  dom.window.QuizForgeAgent.open();
  await flush(); await flush(); await flush();
  dom.window.document.getElementById('agent-input').value = '继续';
  dom.window.document.getElementById('agent-form').dispatchEvent(
    new dom.window.Event('submit', {bubbles: true, cancelable: true}));
  await new Promise(resolve => setTimeout(resolve, 50));

  const messages = [...dom.window.document.querySelectorAll('.agent-message')]
    .map(node => node.textContent.trim());
  assert.equal(reconciled, 1);
  assert.ok(messages.some(text => text.includes('部分')));
  assert.ok(messages.every(text => !text.includes('请求失败')));
  assert.equal(dom.window.document.getElementById('agent-status').textContent, '已停止');
  dom.window.QuizForgeAgent.close();
});

test('危险模式令牌仅在当前页面会话生效并在切换会话时撤销', async () => {
  const dom = new JSDOM(domMarkup(), {
    url: 'http://localhost/', pretendToBeVisual: true, runScripts: 'outside-only',
  });
  const sessions = [
    {id: 'danger-a', scope: 'bank', workdir: '/bank', workdir_id: '', mode: 'standard',
      provider_id: null, messages: [], created_at: 1, updated_at: 2},
    {id: 'danger-b', scope: 'bank', workdir: '/bank', workdir_id: '', mode: 'standard',
      provider_id: null, messages: [], created_at: 1, updated_at: 1},
  ];
  const calls = [];
  dom.window.confirm = () => true;
  dom.window.fetch = async (url, options = {}) => {
    calls.push({url: String(url), method: options.method || 'GET', headers: options.headers || {}});
    if (url === '/api/agent/sessions') return response({ok: true, sessions});
    if (String(url).startsWith('/api/agent/approvals')) return response({ok: true, approvals: []});
    if (url === '/api/agent/tree') return response({ok: true, folders: []});
    if (url === '/api/agent/providers') return response({ok: true, providers: [], presets: []});
    if (String(url).endsWith('/danger') && options.method === 'POST') {
      return response({ok: true, armed: true, danger_token: 'page-token'});
    }
    if (String(url).endsWith('/danger') && options.method === 'DELETE') {
      return response({ok: true, armed: false});
    }
    return response({ok: true, session: sessions[0]});
  };
  dom.window.eval(source);
  dom.window.QuizForgeAgent.open();
  try {
    await flush(); await flush(); await flush();

    const mode = dom.window.document.getElementById('agent-mode');
    mode.value = 'danger';
    mode.dispatchEvent(new dom.window.Event('change', {bubbles: true}));
    await flush(); await flush();
    assert.equal(dom.window.document.getElementById('agent-danger-banner').hidden, false);
    assert.equal(dom.window.document.getElementById('agent-dialog').dataset.mode, 'danger');

    dom.window.document.querySelector('.agent-session-item[data-session-id="danger-b"]').click();
    await flush(); await flush();
    assert.equal(dom.window.document.getElementById('agent-danger-banner').hidden, true);
    const revoke = calls.find(call => call.method === 'DELETE' && call.url.endsWith('/danger'));
    assert.ok(revoke);
    assert.equal(revoke.headers['X-Agent-Danger-Token'], 'page-token');
  } finally {
    dom.window.QuizForgeAgent.close();
    dom.window.close();
  }
});

test('Agent Markdown 禁用原始 HTML 与危险链接协议', async () => {
  const dom = new JSDOM(domMarkup(), {
    url: 'http://localhost/', pretendToBeVisual: true, runScripts: 'outside-only',
  });
  const session = {id: 'markdown-session', scope: 'chat', workdir: null, workdir_id: '',
    mode: 'standard', provider_id: null, created_at: 1, updated_at: 1, messages: [{
      role: 'assistant', status: 'complete', content:
        '# 标题\n<img src=x onerror=alert(1)>\n[危险](javascript:alert(1))\n' +
        '![内联](data:image/png;base64,AAAA)\n[安全](https://example.com)\n```js\nalert(1)\n```',
    }]};
  dom.window.fetch = async url => {
    if (url === '/api/agent/sessions') return response({ok: true, sessions: [session]});
    if (String(url).startsWith('/api/agent/approvals')) return response({ok: true, approvals: []});
    if (url === '/api/agent/tree') return response({ok: true, folders: []});
    if (url === '/api/agent/providers') return response({ok: true, providers: [], presets: []});
    return response({ok: true, session});
  };
  dom.window.eval(markdownSource);
  dom.window.eval(source);
  dom.window.QuizForgeAgent.open();
  try {
    await flush(); await flush(); await flush();
    const bubble = dom.window.document.querySelector('.agent-message-assistant');
    assert.ok(bubble.querySelector('h1'));
    assert.equal(bubble.querySelector('img'), null);
    assert.equal(bubble.querySelector('a[href^="javascript:"]'), null);
    assert.equal(bubble.querySelector('a[href^="data:"]'), null);
    const safe = bubble.querySelector('a[href="https://example.com"]');
    assert.ok(safe);
    assert.equal(safe.rel, 'noopener noreferrer');
    assert.ok(bubble.querySelector('pre code'));
  } finally {
    dom.window.QuizForgeAgent.close();
    dom.window.close();
  }
});
