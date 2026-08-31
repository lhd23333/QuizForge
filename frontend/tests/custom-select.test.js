import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import {JSDOM} from 'jsdom';

const source = fs.readFileSync(
  new URL('../../static/js/custom-select.js', import.meta.url), 'utf8');

function setup(markup) {
  const dom = new JSDOM(`<!doctype html><html><body>${markup}</body></html>`, {
    url: 'http://localhost/', pretendToBeVisual: true, runScripts: 'outside-only',
  });
  dom.window.eval(source);
  dom.window.document.dispatchEvent(new dom.window.Event('DOMContentLoaded'));
  return dom;
}

function flush() {
  return new Promise(resolve => setTimeout(resolve, 0));
}

test('自定义下拉保留原生表单值并展示说明与勾选', () => {
  const dom = setup(`<form><select name="mode">
    <option value="a">普通</option>
    <option value="b" data-description="显示更多排版选项">高级</option>
  </select></form>`);
  const {document} = dom.window;
  const select = document.querySelector('select');
  const trigger = document.querySelector('.qf-select-trigger');
  assert.equal(select.name, 'mode');
  assert.equal(trigger.textContent.trim(), '普通');
  trigger.click();
  const rows = [...document.querySelectorAll('.qf-select-option')];
  assert.equal(rows[1].querySelector('small').textContent, '显示更多排版选项');
  rows[1].click();
  assert.equal(select.value, 'b');
  assert.equal(trigger.textContent.trim(), '高级');
  assert.equal(new dom.window.FormData(document.querySelector('form')).get('mode'), 'b');
});

test('自定义下拉支持键盘、表单重置和动态选项', async () => {
  const dom = setup(`<form><select name="kind">
    <option value="one">一</option><option value="two">二</option>
  </select><button type="reset">重置</button></form>`);
  const {document, Event, KeyboardEvent} = dom.window;
  const select = document.querySelector('select');
  const trigger = document.querySelector('.qf-select-trigger');
  trigger.dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowDown', bubbles: true}));
  assert.equal(trigger.getAttribute('aria-expanded'), 'true');
  document.querySelector('.qf-select-menu').dispatchEvent(
    new KeyboardEvent('keydown', {key: 'End', bubbles: true}));
  assert.equal(document.activeElement.textContent.trim(), '二');
  document.activeElement.click();
  assert.equal(select.value, 'two');
  document.querySelector('form').reset();
  await flush();
  assert.equal(trigger.textContent.trim(), '一');

  const option = document.createElement('option');
  option.value = 'three'; option.textContent = '三';
  select.appendChild(option);
  await flush();
  select.value = 'three';
  select.dispatchEvent(new Event('change', {bubbles: true}));
  assert.equal(trigger.textContent.trim(), '三');
});

test('隐藏和禁用选项不会进入键盘导航且动态属性会同步', async () => {
  const dom = setup(`<select name="range">
    <option value="first">第一项</option>
    <option value="hidden" hidden>暂时隐藏</option>
    <option value="last">最后可选项</option>
    <option value="disabled" disabled>不可用项</option>
  </select>`);
  const {document, KeyboardEvent} = dom.window;
  const select = document.querySelector('select');
  const trigger = document.querySelector('.qf-select-trigger');
  const menu = document.querySelector('.qf-select-menu');

  trigger.dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowDown', bubbles: true}));
  menu.dispatchEvent(new KeyboardEvent('keydown', {key: 'End', bubbles: true}));
  assert.equal(document.activeElement.textContent.trim(), '最后可选项');
  menu.dispatchEvent(new KeyboardEvent('keydown', {key: 'Home', bubbles: true}));
  assert.equal(document.activeElement.textContent.trim(), '第一项');

  menu.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
  assert.equal(trigger.getAttribute('aria-expanded'), 'false');
  assert.equal(document.activeElement, trigger);

  select.options[1].hidden = false;
  select.options[2].hidden = true;
  await flush();
  trigger.dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowDown', bubbles: true}));
  menu.dispatchEvent(new KeyboardEvent('keydown', {key: 'End', bubbles: true}));
  assert.equal(document.activeElement.textContent.trim(), '暂时隐藏');
  assert.equal(
    [...menu.querySelectorAll('[role="option"]')]
      .find(row => row.textContent.includes('最后可选项')).hidden,
    true,
  );
});

test('原生下拉的 hidden 属性和 hidden 类会同步到增强包装器', async () => {
  const dom = setup('<select class="hidden"><option>移动到</option></select>');
  const {document} = dom.window;
  const select = document.querySelector('select');
  const shell = document.querySelector('.qf-select');

  assert.equal(shell.hidden, true);
  select.classList.remove('hidden');
  await flush();
  assert.equal(shell.hidden, false);
  select.hidden = true;
  await flush();
  assert.equal(shell.hidden, true);
});

test('动态移除下拉时同步清理挂在 body 的菜单', async () => {
  const dom = setup('<div id="host"><select><option>临时选项</option></select></div>');
  const {document} = dom.window;
  assert.equal(document.querySelectorAll('.qf-select-menu').length, 1);

  document.querySelector('#host').remove();
  await flush();
  assert.equal(document.querySelectorAll('.qf-select-menu').length, 0);
});

test('增强层保留紧凑下拉的显式宽度', () => {
  const dom = setup(`
    <select class="input" style="width:auto"><option>紧凑</option></select>
    <select class="input"><option>整行</option></select>`);
  const shells = dom.window.document.querySelectorAll('.qf-select');
  assert.equal(shells[0].style.width, 'auto');
  assert.equal(shells[0].classList.contains('is-fluid'), false);
  assert.equal(shells[1].classList.contains('is-fluid'), true);
});

test('同一批大量动态选项只重建一次 listbox', async () => {
  const dom = setup('<select><option value="">题库根目录</option></select>');
  const {document, Option} = dom.window;
  const select = document.querySelector('select');
  const menu = document.querySelector('.qf-select-menu');
  const nativeReplaceChildren = menu.replaceChildren.bind(menu);
  let rebuilds = 0;
  menu.replaceChildren = (...nodes) => {
    rebuilds += 1;
    nativeReplaceChildren(...nodes);
  };

  for (let index = 0; index < 500; index += 1) {
    select.add(new Option(`目录 ${index}`, `folder-${index}`));
  }
  await flush();

  assert.equal(rebuilds, 1);
  assert.equal(menu.querySelectorAll('[role="option"]').length, 501);
});
