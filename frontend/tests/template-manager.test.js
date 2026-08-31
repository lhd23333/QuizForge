import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import {JSDOM} from 'jsdom';

const source = fs.readFileSync(
  new URL('../../static/js/template-manager.js', import.meta.url), 'utf8');

function flush() {
  return new Promise(resolve => setTimeout(resolve, 0));
}

function markup() {
  return `<!doctype html><html><head>
    <meta name="csrf-token" content="csrf-test">
  </head><body>
    <section id="template-manager">
      <form id="template-upload-form">
        <input id="template-upload-file" name="file" type="file">
        <input id="template-upload-name" name="name">
        <input id="template-upload-version" name="version" value="1.0.0">
        <button type="submit">上传</button>
      </form>
      <p id="template-manager-status"></p>
      <div id="template-manager-list"></div>
    </section>
  </body></html>`;
}

function response(payload, status = 200) {
  return {ok: status >= 200 && status < 300, status, json: async () => payload};
}

function buttonLabels(item) {
  return [...item.querySelectorAll('button')].map(button => button.textContent.trim());
}

test('模板管理器按验证、启用和默认状态提供对应操作', async () => {
  const dom = new JSDOM(markup(), {
    url: 'http://localhost/settings', pretendToBeVisual: true, runScripts: 'outside-only',
  });
  const templates = [
    {id: 'pending', name: '待验证模板', format: 'tex', version: '1.0.0',
      enabled: false, selected: false, validation: {status: 'pending'}, supported_modes: ['list']},
    {id: 'valid', name: '验证通过模板', format: 'tex', version: '1.0.0',
      enabled: false, selected: false, validation: {status: 'valid'}, supported_modes: ['list', 'practice']},
    {id: 'enabled', name: '已启用模板', format: 'tex', version: '1.0.0',
      enabled: true, selected: false, validation: {status: 'valid'}, supported_modes: ['list']},
    {id: 'selected', name: '默认模板', format: 'tex', version: '1.0.0',
      enabled: true, selected: true, validation: {status: 'valid'}, supported_modes: ['list']},
    {id: 'reference', name: 'PDF 参考', format: 'pdf', version: '1.0.0',
      reference_only: true, enabled: false, selected: false, preview_url: '/preview.pdf'},
  ];
  dom.window.fetch = async () => response({ok: true, templates});
  dom.window.eval(source);
  await flush();
  await flush();

  const item = id => dom.window.document.querySelector(`[data-template-id="${id}"]`);
  assert.deepEqual(buttonLabels(item('pending')), ['验证', '删除']);
  assert.deepEqual(buttonLabels(item('valid')), ['验证', '启用', '删除']);
  assert.deepEqual(buttonLabels(item('enabled')), ['设为默认', '停用', '删除']);
  assert.deepEqual(buttonLabels(item('selected')), ['停用', '删除']);
  assert.deepEqual(buttonLabels(item('reference')), ['删除']);
  assert.equal(item('pending').querySelector('.template-status').textContent, '待验证');
  assert.equal(item('valid').querySelector('.template-status').textContent, '验证通过');
  assert.equal(item('selected').querySelector('.template-status').textContent, '当前默认');
  assert.equal(item('reference').querySelector('.template-status').textContent, 'PDF 参考');
  assert.deepEqual(
    [...item('valid').querySelectorAll('.template-mode-list span')].map(node => node.textContent),
    ['list', 'practice'],
  );
  const preview = item('reference').querySelector('a');
  assert.equal(preview.getAttribute('href'), '/preview.pdf');
  assert.equal(preview.getAttribute('rel'), 'noopener');
});

test('模板上传、验证和启用走受保护接口并在操作后刷新状态', async () => {
  const dom = new JSDOM(markup(), {
    url: 'http://localhost/settings', pretendToBeVisual: true, runScripts: 'outside-only',
  });
  const templates = [{
    id: 'custom', name: '自定义模板', format: 'tex', version: '1.0.0',
    enabled: false, selected: false, validation: {status: 'pending'},
    supported_modes: ['list'],
  }];
  const calls = [];
  dom.window.confirm = () => true;
  dom.window.fetch = async (url, options = {}) => {
    calls.push({url: String(url), options});
    if ((options.method || 'GET') === 'GET') return response({ok: true, templates});
    if (String(url).endsWith('/validate')) {
      templates[0].validation = {status: 'valid'};
      return response({ok: true, template: templates[0]});
    }
    if (String(url).endsWith('/enable')) {
      templates[0].enabled = true;
      return response({ok: true, template: templates[0]});
    }
    if (String(url) === '/api/templates' && options.method === 'POST') {
      return response({ok: true, template: templates[0]});
    }
    return response({ok: true});
  };
  dom.window.eval(source);
  await flush();
  await flush();

  dom.window.document.querySelector('[data-template-id="custom"] button').click();
  await flush();
  await flush();
  await flush();
  assert.ok(calls.some(call => call.url === '/api/templates/custom/validate'
    && call.options.method === 'POST'));
  assert.ok(buttonLabels(dom.window.document.querySelector('[data-template-id="custom"]'))
    .includes('启用'));

  const enable = [...dom.window.document.querySelectorAll('[data-template-id="custom"] button')]
    .find(button => button.textContent.trim() === '启用');
  enable.click();
  await flush();
  await flush();
  await flush();
  const enableCall = calls.find(call => call.url === '/api/templates/custom/enable');
  assert.ok(enableCall);
  assert.deepEqual(JSON.parse(enableCall.options.body), {confirm: true});
  assert.ok(buttonLabels(dom.window.document.querySelector('[data-template-id="custom"]'))
    .includes('设为默认'));

  const form = dom.window.document.getElementById('template-upload-form');
  dom.window.document.getElementById('template-upload-name').value = '新模板';
  dom.window.document.getElementById('template-upload-version').value = '2.0.0';
  form.dispatchEvent(new dom.window.Event('submit', {bubbles: true, cancelable: true}));
  await flush();
  await flush();
  await flush();
  const upload = calls.find(call => call.url === '/api/templates'
    && call.options.method === 'POST');
  assert.ok(upload.options.body instanceof dom.window.FormData);
  assert.equal(upload.options.headers.get('X-CSRF-Token'), 'csrf-test');
  assert.equal(dom.window.document.getElementById('template-upload-name').value, '');
  assert.equal(dom.window.document.getElementById('template-upload-version').value, '1.0.0');
  assert.match(dom.window.document.getElementById('template-manager-status').textContent,
    /验证通过前不会用于导出/);
});
