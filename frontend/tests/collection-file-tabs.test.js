import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import {JSDOM} from 'jsdom';

const indexSource = fs.readFileSync(
  new URL('../../templates/index.html', import.meta.url), 'utf8');
const questionFilesSource = fs.readFileSync(
  new URL('../../static/js/question-files.js', import.meta.url), 'utf8');

function extractRange(startMarker, endMarker) {
  const start = indexSource.indexOf(startMarker);
  const end = indexSource.indexOf(endMarker, start);
  assert.ok(start >= 0, `找不到起始标记 ${startMarker}`);
  assert.ok(end > start, `找不到结束标记 ${endMarker}`);
  return indexSource.slice(start, end);
}

function extractFunction(source, name) {
  const start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `找不到函数 ${name}`);
  const bodyStart = source.indexOf('{', start);
  let depth = 0;
  for (let index = bodyStart; index < source.length; index += 1) {
    if (source[index] === '{') depth += 1;
    if (source[index] === '}') depth -= 1;
    if (depth === 0) return source.slice(start, index + 1);
  }
  throw new Error(`函数 ${name} 未闭合`);
}

function setupCollectionTabs(tabs, active) {
  const dom = new JSDOM('<!doctype html><html><body></body></html>', {
    url: 'http://localhost/',
    runScripts: 'outside-only',
  });
  const source = extractRange(
    'function fileKindFromEntry(entry, path) {',
    'async function refreshCurrentCollectionTab',
  );
  const serialized = JSON.stringify(tabs);
  dom.window.eval(`
    let collectionTabsState = {tabs: ${serialized}, active: ${JSON.stringify(active)}};
    let collectionTabSerial = 0;
    function nextCollectionTabKey() { collectionTabSerial += 1; return 'new-' + collectionTabSerial; }
    function activeCollectionTab() {
      return collectionTabsState.tabs.find(tab => tab.key === collectionTabsState.active) || null;
    }
    function isFileCollectionTab(tab) { return tab?.view === 'file'; }
    function collectionFilePath(tab) { return String(tab?.filePath || tab?.path || '').replace(/\\\\/g, '/').replace(/^\\/+/, ''); }
    function persistCollectionTabs() {}
    function renderCollectionTabs() {}
    function createBlankCollectionTab() {
      return {key: nextCollectionTabKey(), id: '', name: '空白页', url: '/', position: null};
    }
    ${source}
    window.__collectionTabsTest = {
      api: window.QFCollectionTabs,
      state: () => collectionTabsState,
    };
  `);
  return dom;
}

test('打开新文件时，脏预览会先固定并同步事件，再新建文件标签', () => {
  const dom = setupCollectionTabs([
    {
      key: 'old', view: 'file', id: '', name: '旧.md', url: '#file-old',
      filePath: '旧.md', fileKind: 'markdown', pinned: false, preview: true,
      fileDirty: true, fileGroupId: '',
    },
  ], 'old');
  const events = [];
  dom.window.addEventListener('qf:collection-file-pin', event => events.push({type: 'pin', tab: event.detail.tab}));
  dom.window.addEventListener('qf:collection-file-open', event => events.push({type: 'open', tab: event.detail.tab}));

  const opened = dom.window.__collectionTabsTest.api.openFile({path: '新.pdf', kind: 'pdf'});
  const state = dom.window.__collectionTabsTest.state();
  const old = state.tabs.find(tab => tab.key === 'old');

  assert.equal(old.pinned, true);
  assert.equal(old.preview, false);
  assert.equal(old.fileDirty, true, '固定标签不能清除未保存状态');
  assert.equal(state.tabs.length, 2);
  assert.equal(state.active, opened.key);
  assert.deepEqual(events.map(event => event.type), ['pin', 'open']);
  assert.equal(events[0].tab.key, 'old');
  assert.equal(events[1].tab.key, opened.key);
});

test('打开另一个文件时，只复用干净的临时预览标签', () => {
  const dom = setupCollectionTabs([
    {
      key: 'preview', view: 'file', id: '', name: '旧.md', url: '#file-old',
      filePath: '旧.md', fileKind: 'markdown', pinned: false, preview: true,
      fileDirty: false, fileGroupId: '',
    },
  ], 'preview');
  const opened = dom.window.__collectionTabsTest.api.openFile({path: '新.md'});
  const state = dom.window.__collectionTabsTest.state();

  assert.equal(opened.key, 'preview');
  assert.equal(opened.filePath, '新.md');
  assert.equal(opened.preview, true);
  assert.equal(state.tabs.length, 1);
});

test('关闭活动文件标签后激活相邻文件，不调用题集页加载逻辑', () => {
  const dom = setupCollectionTabs([
    {
      key: 'left', view: 'file', id: '', name: '左.pdf', url: '#file-left',
      filePath: '左.pdf', fileKind: 'pdf', pinned: true, preview: false,
      fileDirty: false, fileGroupId: '',
    },
    {
      key: 'right', view: 'file', id: '', name: '右.md', url: '#file-right',
      filePath: '右.md', fileKind: 'markdown', pinned: true, preview: false,
      fileDirty: false, fileGroupId: '',
    },
  ], 'left');
  const events = [];
  dom.window.addEventListener('qf:collection-file-close', event => events.push(`close:${event.detail.tab.key}`));
  dom.window.addEventListener('qf:collection-file-activate', event => events.push(`activate:${event.detail.tab.key}`));

  assert.equal(dom.window.__collectionTabsTest.api.closeFile('left', {force: true}), true);
  const state = dom.window.__collectionTabsTest.state();
  assert.deepEqual(Array.from(state.tabs, tab => tab.key), ['right']);
  assert.equal(state.active, 'right');
  assert.deepEqual(events, ['close:left', 'activate:right']);
});

test('同一文件在不同组中允许独立打开副本标签', () => {
  const dom = setupCollectionTabs([
    {
      key: 'left', view: 'file', id: '', name: 'a.md', url: '#file-a',
      filePath: 'docs/a.md', fileKind: 'markdown', pinned: true, preview: false,
      fileDirty: false, fileGroupId: 'primary',
    },
    {
      key: 'right-preview', view: 'file', id: '', name: 'b.md', url: '#file-b',
      filePath: 'docs/b.md', fileKind: 'markdown', pinned: true, preview: false,
      fileDirty: false, fileGroupId: 'group-2',
    },
  ], 'right-preview');

  const opened = dom.window.__collectionTabsTest.api.openFile(
    {path: 'docs/a.md', kind: 'markdown'}, {groupId: 'group-2'});
  const state = dom.window.__collectionTabsTest.state();

  assert.notEqual(opened.key, 'left');
  assert.equal(opened.filePath, 'docs/a.md');
  assert.equal(opened.fileGroupId, 'group-2');
  assert.equal(state.tabs.length, 3);
  assert.equal(state.tabs.find(tab => tab.key === 'left').fileGroupId, 'primary');
  assert.equal(state.tabs.find(tab => tab.key === 'right-preview').filePath, 'docs/b.md');
});

test('目录重命名时，嵌套文件标签保留自身文件名', () => {
  const dom = new JSDOM('<!doctype html><html><body></body></html>', {
    runScripts: 'outside-only',
  });
  const renamePath = extractFunction(questionFilesSource, 'renamePath');
  dom.window.eval(`
    const tabs = [
      {path: '旧目录/a.md', name: 'a.md'},
      {path: '旧目录/sub/b.pdf', name: 'b.pdf'},
      {path: '其他/c.md', name: 'c.md'},
    ];
    function normalizePath(path) {
      return String(path || '').replace(/\\\\/g, '/').replace(/^\\/+/, '')
        .split('/').filter(part => part && part !== '.' && part !== '..').join('/');
    }
    function pathName(path) { return String(path || '').split('/').pop() || String(path || ''); }
    function allTabs() { return tabs; }
    function renderAll() {}
    window.QFCollectionTabs = {renameFilePath() {}};
    ${renamePath}
    renamePath('旧目录', '新目录');
    window.__renamedTabs = tabs;
  `);
  assert.deepEqual(
    Array.from(dom.window.__renamedTabs, tab => [tab.path, tab.name]),
    [['新目录/a.md', 'a.md'], ['新目录/sub/b.pdf', 'b.pdf'], ['其他/c.md', 'c.md']],
  );
});
