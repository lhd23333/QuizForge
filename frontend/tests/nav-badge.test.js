import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {JSDOM} from 'jsdom';


const SOURCE = fs.readFileSync(
  new URL('../../static/js/nav-badge.js', import.meta.url), 'utf8');


function makeDom(count) {
  const dom = new JSDOM(`<!doctype html><html><body>
    <a class="nav-link" href="/batches"><span>转换任务</span>
      <span class="nav-badge" id="nav-batch-count"${count ? '' : ' hidden'}>${count}</span></a>
  </body></html>`, {
    url: 'http://localhost/', runScripts: 'outside-only', pretendToBeVisual: true,
  });
  return dom;
}


test('顶层文档轮询 /nav/count 并同步徽标（含归零隐藏）', async () => {
  const dom = makeDom(5);
  const calls = [];
  dom.window.fetch = async url => {
    calls.push(url);
    return {ok: true, json: async () => ({ok: true, nav_batch_count: 0})};
  };
  let tick = null;
  dom.window.setInterval = fn => { tick = fn; return 1; };

  dom.window.eval(SOURCE);
  const badge = dom.window.document.getElementById('nav-batch-count');
  assert.equal(badge.hidden, false);   // 服务端渲染的初始计数 5
  assert.equal(typeof tick, 'function');

  await tick();
  assert.deepEqual(calls, ['/nav/count']);
  assert.equal(badge.textContent, '0');
  assert.equal(badge.hidden, true);    // 计数归零必须重新隐藏（CSS 靠 [hidden]）
});


test('轮询失败与后端异常响应都静默，不打断页面', async () => {
  const dom = makeDom(2);
  let mode = 'reject';
  dom.window.fetch = async () => {
    if (mode === 'reject') throw new Error('boom');
    return {ok: false, json: async () => ({})};
  };
  let tick = null;
  dom.window.setInterval = fn => { tick = fn; return 1; };

  dom.window.eval(SOURCE);
  const badge = dom.window.document.getElementById('nav-batch-count');

  await tick();                       // 网络错误：保持旧值
  assert.equal(badge.textContent, '2');
  mode = 'not-ok';
  await tick();                       // 非 200：同样保持
  assert.equal(badge.textContent, '2');
  assert.equal(badge.hidden, false);
});


test('base.html 常渲染徽标并挂载 nav-badge.js（与 CSS hidden 规则三位一体）', () => {
  // id / 脚本 / CSS 三处是松耦合的字符串契约：任何一处改名都会让红心
  // 静默停在旧值（脚本找不到元素就直接 return）或显示红色 0（[hidden]
  // 被 .nav-badge 的作者 display 压过）。
  const template = fs.readFileSync(
    new URL('../../templates/base.html', import.meta.url), 'utf8');
  const css = fs.readFileSync(
    new URL('../../static/style.css', import.meta.url), 'utf8');
  assert.match(template, /id="nav-batch-count"/);
  assert.match(template, /js\/nav-badge\.js/);
  assert.match(css, /\.nav-badge\[hidden\]\s*\{\s*display:\s*none/);
});


test('嵌入文档（iframe 内）不轮询：徽标被 embedded-view 隐藏', () => {
  const dom = new JSDOM('<!doctype html><html><body><iframe id="f"></iframe></body></html>', {
    url: 'http://localhost/', runScripts: 'outside-only', pretendToBeVisual: true,
  });
  const frame = dom.window.document.getElementById('f');
  const embedded = frame.contentWindow;
  embedded.document.body.innerHTML =
    '<span class="nav-badge" id="nav-batch-count" hidden>0</span>';
  let scheduled = false;
  embedded.setInterval = () => { scheduled = true; return 1; };
  let fetched = false;
  embedded.fetch = () => { fetched = true; };

  embedded.eval(SOURCE);
  assert.equal(scheduled, false, 'iframe 文档不得注册轮询');
  assert.equal(fetched, false, 'iframe 文档不得发起请求');
});
