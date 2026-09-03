import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {JSDOM} from 'jsdom';

const source = fs.readFileSync(
  new URL('../../static/js/image-redraw.js', import.meta.url), 'utf8');

function flush() {
  return new Promise(resolve => setTimeout(resolve, 0));
}

function setup(fetchImpl) {
  const dom = new JSDOM(`<!doctype html><html><body>
    <article class="card" data-id="q1">
      <div class="img-layout-bar" data-id="q1" data-originals="[]">
        <button type="button" class="img-version-btn">版本</button>
        <button type="button" class="img-redraw-btn">AI 重绘</button>
        <button type="button" class="img-restore-btn" hidden>还原原图</button>
      </div>
      <div class="body"><img src="/assets/current.png"></div>
    </article>
  </body></html>`, {
    url: 'http://localhost/',
    pretendToBeVisual: true,
    runScripts: 'outside-only',
  });
  const dialogProto = dom.window.HTMLDialogElement.prototype;
  dialogProto.showModal = function showModal() { this.open = true; };
  dialogProto.close = function close() {
    this.open = false;
    this.dispatchEvent(new dom.window.Event('close'));
  };
  dom.window.alert = () => {};
  dom.window.confirm = () => true;
  dom.window.QFIcon = name => `<svg data-icon="${name}"></svg>`;
  dom.window.QImgLayout = {
    replaceBody: (bar, html) => {
      bar.closest('.card').querySelector('.body').innerHTML = html;
      return true;
    },
  };
  dom.window.fetch = fetchImpl;
  dom.window.eval(source);
  dom.window.document.dispatchEvent(new dom.window.Event('DOMContentLoaded'));
  return dom;
}

function response(data) {
  return {text: async () => JSON.stringify(data)};
}

function rows(current = 'current.png') {
  return [
    {name: 'source.jpg', src: '/assets/source.jpg', kind: 'original',
      created: '2026-09-01T10:00:00', model: '', prompt: '',
      current: current === 'source.jpg', exists: true, can_delete: false},
    {name: 'redraw_1111111111111111.png',
      src: '/assets/redraw_1111111111111111.png', kind: 'generated',
      created: '2026-09-01T11:00:00', model: 'qwen-vl', prompt: '标出辅助线',
      current: current === 'redraw_1111111111111111.png', exists: true, can_delete: true},
    {name: 'current.png', src: '/assets/current.png', kind: 'generated',
      created: '2026-09-01T12:00:00', model: 'gpt-image-2', prompt: '',
      current: current === 'current.png', exists: true, can_delete: false},
  ];
}

test('版本历史弹窗显示缩略图、模型和当前状态', async () => {
  const requests = [];
  const dom = setup(async (url, options = {}) => {
    requests.push({url, options});
    return response({ok: true, index: 0, versions: rows()});
  });

  dom.window.document.querySelector('.img-version-btn').click();
  await flush();
  await flush();

  assert.equal(requests.length, 1);
  assert.match(requests[0].url, /redraw\/versions\?index=0$/);
  assert.equal(dom.window.document.querySelectorAll('.image-version-item').length, 3);
  assert.equal(dom.window.document.querySelectorAll('.image-version-preview img').length, 3);
  assert.equal(dom.window.document.querySelector('.image-version-state').textContent,
               '当前使用');
  assert.match(dom.window.document.querySelector('.image-versions-list').textContent,
               /gpt-image-2/);
  assert.match(dom.window.document.querySelector('.image-versions-list').textContent,
               /标出辅助线/);
});

test('版本历史可以切换版本并无刷新更新题干', async () => {
  const requests = [];
  const dom = setup(async (url, options = {}) => {
    requests.push({url, options});
    if (url.includes('/version/switch')) {
      return response({ok: true, body_html: '<img src="/assets/source.jpg">',
        versions: rows('source.jpg')});
    }
    return response({ok: true, index: 0, versions: rows()});
  });

  dom.window.document.querySelector('.img-version-btn').click();
  await flush();
  const generated = dom.window.document.querySelectorAll('.image-version-item')[1];
  [...generated.querySelectorAll('button')]
    .find(button => button.textContent.includes('切换')).click();
  await flush();
  await flush();

  assert.match(requests.at(-1).url, /redraw\/version\/switch$/);
  assert.deepEqual(JSON.parse(requests.at(-1).options.body), {
    index: 0, name: 'redraw_1111111111111111.png',
  });
  assert.equal(dom.window.document.querySelector('.body img').getAttribute('src'),
               '/assets/source.jpg');
  assert.equal(dom.window.document.querySelector('.image-versions-dialog'), null);
});

test('删除历史版本前确认并刷新列表', async () => {
  const requests = [];
  const dom = setup(async (url, options = {}) => {
    requests.push({url, options});
    if (url.includes('/version/delete')) {
      return response({ok: true, index: 0, versions: rows().filter(
        row => row.name !== 'redraw_1111111111111111.png')});
    }
    return response({ok: true, index: 0, versions: rows()});
  });

  dom.window.document.querySelector('.img-version-btn').click();
  await flush();
  const generated = dom.window.document.querySelectorAll('.image-version-item')[1];
  [...generated.querySelectorAll('button')]
    .find(button => button.textContent.includes('删除')).click();
  await flush();
  await flush();

  assert.match(requests.at(-1).url, /redraw\/version\/delete$/);
  assert.deepEqual(JSON.parse(requests.at(-1).options.body), {
    index: 0, name: 'redraw_1111111111111111.png',
  });
  assert.equal(dom.window.document.querySelectorAll('.image-version-item').length, 2);
});
