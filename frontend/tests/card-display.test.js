import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {JSDOM} from 'jsdom';


function loadDisplayScript() {
  const dom = new JSDOM(`<!doctype html><html><body>
    <input id="all-a4-toggle" type="checkbox">
    <input id="all-solutions-toggle" type="checkbox">
    <main id="q-list">
      <article class="card"><button class="card-a4-preview-trigger">A4 视图</button>
        <details class="q-solution"></details></article>
      <article class="card"><button class="card-a4-preview-trigger">A4 视图</button>
        <details class="q-solution"></details></article>
    </main>
  </body></html>`, {
    url: 'http://localhost/',
    pretendToBeVisual: true,
    runScripts: 'outside-only',
  });
  dom.window.HTMLElement.prototype.scrollIntoView = () => {};
  const source = fs.readFileSync(
    new URL('../../static/js/card-a4-preview.js', import.meta.url), 'utf8');
  dom.window.eval(source);
  return dom;
}


test('全部 A4 与解析展开会应用到当前和后加载题卡', () => {
  const dom = loadDisplayScript();
  const document = dom.window.document;
  const a4 = document.getElementById('all-a4-toggle');
  const solutions = document.getElementById('all-solutions-toggle');

  a4.checked = true;
  a4.dispatchEvent(new dom.window.Event('change'));
  solutions.checked = true;
  solutions.dispatchEvent(new dom.window.Event('change'));
  assert.equal(document.querySelectorAll('.a4-preview-active').length, 2);
  assert.ok([...document.querySelectorAll('details')].every(item => item.open));

  const next = document.createElement('article');
  next.className = 'card';
  next.innerHTML = '<button class="card-a4-preview-trigger"></button>'
    + '<details class="q-solution"></details>';
  document.getElementById('q-list').append(next);
  dom.window.QCardDisplay.apply(next);
  assert.ok(next.classList.contains('a4-preview-active'));
  assert.ok(next.querySelector('details').open);
});


test('取消勾选会恢复默认题卡与解析折叠状态', () => {
  const dom = loadDisplayScript();
  const document = dom.window.document;
  const a4 = document.getElementById('all-a4-toggle');
  const solutions = document.getElementById('all-solutions-toggle');
  a4.checked = true;
  a4.dispatchEvent(new dom.window.Event('change'));
  solutions.checked = true;
  solutions.dispatchEvent(new dom.window.Event('change'));

  a4.checked = false;
  a4.dispatchEvent(new dom.window.Event('change'));
  solutions.checked = false;
  solutions.dispatchEvent(new dom.window.Event('change'));
  assert.equal(document.querySelectorAll('.a4-preview-active').length, 0);
  assert.ok([...document.querySelectorAll('details')].every(item => !item.open));
});
