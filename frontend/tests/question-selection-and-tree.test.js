import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import {JSDOM} from 'jsdom';

const indexSource = fs.readFileSync(
  new URL('../../templates/index.html', import.meta.url), 'utf8');

function extractFunction(name) {
  const plain = indexSource.indexOf(`function ${name}(`);
  const asyncStart = indexSource.indexOf(`async function ${name}(`);
  const starts = [plain, asyncStart].filter(index => index >= 0);
  assert.ok(starts.length, `找不到函数 ${name}`);
  const start = Math.min(...starts);
  const bodyStart = indexSource.indexOf('{', start);
  let depth = 0;
  for (let index = bodyStart; index < indexSource.length; index += 1) {
    if (indexSource[index] === '{') depth += 1;
    if (indexSource[index] === '}') depth -= 1;
    if (depth === 0) return indexSource.slice(start, index + 1);
  }
  throw new Error(`函数 ${name} 未闭合`);
}

function setupSelection() {
  const dom = new JSDOM(`<!doctype html><html><body>
    <main id="q-list">
      <article class="card" data-id="q-1"><input class="sel-toggle" type="checkbox"></article>
      <article class="card" data-id="q-2"><input class="sel-toggle" type="checkbox"></article>
    </main>
  </body></html>`, {runScripts: 'outside-only'});
  dom.window.CSS ??= {};
  dom.window.CSS.escape = value => String(value).replace(/[^\w-]/g, '\\$&');
  dom.window.eval(`
    const listEl = document.getElementById('q-list');
    ${extractFunction('normalizeSelectionValue')}
    ${extractFunction('setQuestionSelected')}
    window.__selectionTest = {normalizeSelectionValue, setQuestionSelected};
  `);
  return dom;
}

test('题目勾选统一兼容 JSON 布尔值和旧 0/1 响应', () => {
  const dom = setupSelection();
  const api = dom.window.__selectionTest;
  for (const value of [true, 1, '1']) assert.equal(api.normalizeSelectionValue(value), true);
  for (const value of [false, 0, '0', null, undefined, 'true']) {
    assert.equal(api.normalizeSelectionValue(value), false);
  }

  for (const [value, expected] of [[true, true], [0, false], ['1', true], [false, false]]) {
    assert.equal(api.setQuestionSelected('q-1', value), expected);
    const card = dom.window.document.querySelector('[data-id="q-1"]');
    assert.equal(card.classList.contains('selected'), expected);
    assert.equal(card.querySelector('.sel-toggle').checked, expected);
  }
});

test('统一勾选函数会同步局部插入的同题卡节点', () => {
  const dom = setupSelection();
  const {document} = dom.window;
  const inserted = document.createElement('article');
  inserted.className = 'card';
  inserted.dataset.id = 'q-1';
  inserted.innerHTML = '<input class="sel-toggle" type="checkbox">';
  document.getElementById('q-list').append(inserted);

  dom.window.__selectionTest.setQuestionSelected('q-1', true);
  const copies = [...document.querySelectorAll('.card[data-id="q-1"]')];
  assert.equal(copies.length, 2);
  assert.ok(copies.every(card => card.classList.contains('selected')));
  assert.ok(copies.every(card => card.querySelector('.sel-toggle').checked));
});

function setupFolderTree(bankKey = 'bank-a') {
  const dom = new JSDOM(`<!doctype html><html><body>
    <ul id="folder-tree" data-bank-key="${bankKey}">
      <li class="folder-item has-children collapsed" data-folder-id="manual" data-active-path="0">
        <div class="folder-row"><span class="folder-twist"></span></div>
        <ul class="folder-children collapsed" data-parent-id="manual" data-loaded="1"></ul>
      </li>
      <li class="folder-item has-children collapsed" data-folder-id="active-root" data-active-path="1">
        <div class="folder-row"><span class="folder-twist"></span></div>
        <ul class="folder-children collapsed" data-parent-id="active-root" data-loaded="1"></ul>
      </li>
      <li class="folder-item has-children collapsed" data-folder-id="unrelated" data-active-path="0">
        <div class="folder-row"><span class="folder-twist"></span></div>
        <ul class="folder-children collapsed" data-parent-id="unrelated" data-loaded="1"></ul>
      </li>
    </ul>
  </body></html>`, {url: 'http://localhost/', runScripts: 'outside-only'});
  dom.window.eval(`
    const folderTree = document.getElementById('folder-tree');
    const FOLDER_TREE_STORE = \`quizforge:folder-tree:v1:\${folderTree?.dataset.bankKey || 'default'}\`;
    ${extractFunction('readFolderExpansion')}
    ${extractFunction('writeFolderExpansion')}
    ${extractFunction('setFolderExpanded')}
    ${extractFunction('restoreFolderExpansion')}
    window.__folderTreeTest = {setFolderExpanded, restoreFolderExpansion};
  `);
  return dom;
}

test('文件树只恢复手动项和活动祖先且按题库隔离状态', async () => {
  const dom = setupFolderTree('bank-a');
  const {document, sessionStorage} = dom.window;
  sessionStorage.setItem('quizforge:folder-tree:v1:bank-a', JSON.stringify({
    manual: true,
    'active-root': false,
  }));

  await dom.window.__folderTreeTest.restoreFolderExpansion();
  assert.equal(document.querySelector('[data-folder-id="manual"]').classList.contains('collapsed'), false);
  assert.equal(document.querySelector('[data-folder-id="active-root"]').classList.contains('collapsed'), false);
  assert.equal(document.querySelector('[data-folder-id="unrelated"]').classList.contains('collapsed'), true);

  await dom.window.__folderTreeTest.setFolderExpanded(
    document.querySelector('[data-folder-id="manual"]'), false, true);
  assert.deepEqual(
    JSON.parse(sessionStorage.getItem('quizforge:folder-tree:v1:bank-a')),
    {manual: false, 'active-root': false},
  );
  assert.equal(sessionStorage.getItem('quizforge:folder-tree:v1:bank-b'), null);
});

test('文件夹移动下拉增强后会连同可见包装器一起移动', () => {
  const dom = new JSDOM(`<!doctype html><html><body>
    <div id="origin"><span class="qf-select"><select id="folder-move-select"></select><button></button></span></div>
    <div id="target"></div>
  </body></html>`, {runScripts: 'outside-only'});
  dom.window.eval(`
    const folderMoveSelect = document.getElementById('folder-move-select');
    ${extractFunction('folderMoveControlNode')}
    ${extractFunction('placeFolderMoveControl')}
    window.__moveControl = {folderMoveControlNode, placeFolderMoveControl};
  `);

  const {document} = dom.window;
  dom.window.__moveControl.placeFolderMoveControl(document.getElementById('target'));
  const control = dom.window.__moveControl.folderMoveControlNode();
  assert.equal(control.className, 'qf-select');
  assert.equal(document.getElementById('target').nextElementSibling, control);
  assert.equal(control.querySelector('select').id, 'folder-move-select');
});
