import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import {JSDOM} from 'jsdom';

const source = fs.readFileSync(
  new URL('../../static/js/question-files.js', import.meta.url), 'utf8');

function createWorkspace() {
  const dom = new JSDOM(`<!doctype html><html><body>
    <div id="question-file-workspace">
      <div class="question-file-panes"></div>
    </div>
  </body></html>`, {
    url: 'http://localhost/', pretendToBeVisual: true, runScripts: 'outside-only',
  });
  dom.window.fetch = async () => ({
    ok: true,
    json: async () => ({ok: true, text: '', mtime: 1}),
  });
  dom.window.eval(source);
  return dom;
}

test('文件工作区取消固定控制条且最多形成左右两栏', () => {
  const dom = createWorkspace();
  const workspace = dom.window.document.getElementById('question-file-workspace');
  const api = dom.window.QQuestionFileWorkspace;

  assert.ok(api);
  assert.equal(workspace.querySelectorAll('.qf-workspace-controls').length, 0);
  assert.equal(workspace.querySelectorAll('[data-qf-group]').length, 1);

  const first = api.open({path: 'a.pdf', kind: 'pdf'});
  assert.ok(first);
  dom.window.dispatchEvent(new dom.window.CustomEvent('qf:collection-tab-drag-start', {
    detail: {key: first.key, file: true},
  }));
  const secondaryZone = workspace.querySelector('[data-qf-drop-group="secondary"]');
  assert.ok(secondaryZone);
  secondaryZone.dispatchEvent(new dom.window.Event('dragover', {bubbles: true, cancelable: true}));
  secondaryZone.dispatchEvent(new dom.window.Event('drop', {bubbles: true, cancelable: true}));

  assert.equal(workspace.querySelectorAll('[data-qf-group]').length, 2);
  assert.equal(workspace.querySelector('.question-file-groups').classList.contains('is-vertical'), true);
  assert.equal(workspace.querySelectorAll('.qf-splitter').length, 1);
  const splitter = workspace.querySelector('.qf-splitter');
  assert.equal(splitter.getAttribute('aria-valuemin'), '25');
  assert.equal(splitter.getAttribute('aria-valuemax'), '75');
  splitter.dispatchEvent(new dom.window.KeyboardEvent('keydown', {key: 'ArrowRight', bubbles: true}));
  assert.equal(splitter.getAttribute('aria-valuenow'), '53');
  assert.equal(dom.window.sessionStorage.getItem('quizforge:question-file-split:v1'), '53');
  assert.equal(workspace.querySelectorAll('[data-qf-group="primary"] [data-file-panel]').length, 0);
  assert.equal(workspace.querySelectorAll('[data-qf-group="secondary"] [data-file-panel]').length, 1);
  assert.equal(api.addGroup(), null);
});

test('分界线拖动结束时文档级指针事件会清理拖动状态', () => {
  const dom = createWorkspace();
  const workspace = dom.window.document.getElementById('question-file-workspace');
  const api = dom.window.QQuestionFileWorkspace;
  api.open({path: 'a.pdf', kind: 'pdf'});
  api.addGroup();
  const splitter = workspace.querySelector('.qf-splitter');
  const groupsHost = workspace.querySelector('.question-file-groups');
  groupsHost.getBoundingClientRect = () => ({left: 0, width: 1000});
  const pointerEvent = (type, values = {}) => {
    const event = new dom.window.Event(type, {bubbles: true, cancelable: true});
    Object.entries(values).forEach(([key, value]) => {
      Object.defineProperty(event, key, {configurable: true, value});
    });
    return event;
  };

  splitter.dispatchEvent(pointerEvent('pointerdown', {button: 0, pointerId: 7}));
  assert.equal(splitter.classList.contains('is-dragging'), true);
  dom.window.document.dispatchEvent(pointerEvent('pointermove', {clientX: 700, pointerId: 7}));
  assert.equal(splitter.getAttribute('aria-valuenow'), '70');
  dom.window.document.dispatchEvent(pointerEvent('pointerup', {pointerId: 7}));
  assert.equal(splitter.classList.contains('is-dragging'), false);
});

test('dragging a file tab from a collection page reveals the workspace and focuses the dropped pane', () => {
  const dom = createWorkspace();
  const workspace = dom.window.document.getElementById('question-file-workspace');
  const api = dom.window.QQuestionFileWorkspace;
  const activeCollection = {key: 'collection', view: 'collection'};
  let activeTop = activeCollection;
  dom.window.QFCollectionTabs = {
    active: () => activeTop,
    isFile: tab => tab?.view === 'file',
    setFileGroup() {},
    activateFile(key) { activeTop = {key, view: 'file'}; },
  };
  const first = api.open({path: 'first.md', kind: 'markdown'});
  assert.equal(workspace.hidden, true);

  dom.window.dispatchEvent(new dom.window.CustomEvent('qf:collection-tab-drag-start', {
    detail: {
      key: first.key, file: true,
      tab: {key: first.key, filePath: 'first.md', fileKind: 'markdown',
        name: 'first.md', pinned: true, fileDirty: false, fileGroupId: ''},
    },
  }));
  assert.equal(workspace.hidden, false);
  assert.ok(workspace.querySelector('.qf-tab-drop-overlay'));

  const secondaryZone = workspace.querySelector('[data-qf-drop-group="secondary"]');
  secondaryZone.dispatchEvent(new dom.window.Event('dragover', {bubbles: true, cancelable: true}));
  secondaryZone.dispatchEvent(new dom.window.Event('drop', {bubbles: true, cancelable: true}));
  assert.equal(activeTop.key, first.key);
  assert.equal(workspace.hidden, false);
  assert.equal(workspace.querySelector('.question-file-groups').classList.contains('is-vertical'), true);
  assert.equal(workspace.querySelector('[data-qf-group="secondary"] [data-file-panel]')?.dataset.filePanel, first.key);

  dom.window.dispatchEvent(new dom.window.CustomEvent('qf:collection-tab-drag-end'));
  assert.equal(workspace.querySelector('.qf-tab-drop-overlay'), null);
});

test('cancelling a file tab drag restores a collection page workspace hidden state', () => {
  const dom = createWorkspace();
  const workspace = dom.window.document.getElementById('question-file-workspace');
  const api = dom.window.QQuestionFileWorkspace;
  const activeCollection = {key: 'collection', view: 'collection'};
  dom.window.QFCollectionTabs = {
    active: () => activeCollection,
    isFile: tab => tab?.view === 'file',
  };
  const first = api.open({path: 'first.md', kind: 'markdown'});
  assert.equal(workspace.hidden, true);

  dom.window.dispatchEvent(new dom.window.CustomEvent('qf:collection-tab-drag-start', {
    detail: {key: first.key, file: true, tab: {key: first.key, filePath: 'first.md', fileKind: 'markdown'}},
  }));
  assert.equal(workspace.hidden, false);
  dom.window.dispatchEvent(new dom.window.CustomEvent('qf:collection-tab-drag-end'));
  assert.equal(workspace.hidden, true);
});

test('次栏最后一个标签关闭后自动合并回单栏', () => {
  const dom = createWorkspace();
  const workspace = dom.window.document.getElementById('question-file-workspace');
  const api = dom.window.QQuestionFileWorkspace;
  const tab = api.open({path: 'a.pdf', kind: 'pdf'});
  api.addGroup();
  assert.equal(api.moveTabToGroup(tab.key, 'secondary'), true);
  api.closePath('a.pdf');

  assert.equal(workspace.querySelectorAll('[data-qf-group]').length, 1);
  assert.equal(workspace.querySelector('.question-file-groups').classList.contains('is-single'), true);
  assert.equal(workspace.querySelectorAll('.qf-splitter').length, 1);
  assert.equal(workspace.querySelector('.qf-splitter').hidden, true);
});

test('刷新后拖动未激活文件标签时惰性建立面板且不切换当前标签', () => {
  const dom = createWorkspace();
  const workspace = dom.window.document.getElementById('question-file-workspace');
  const api = dom.window.QQuestionFileWorkspace;
  const active = api.open({path: 'active.pdf', kind: 'pdf'});

  dom.window.dispatchEvent(new dom.window.CustomEvent('qf:collection-tab-drag-start', {
    detail: {
      key: 'persisted', file: true,
      tab: {
        key: 'persisted', filePath: 'persisted.pdf', fileKind: 'pdf',
        name: 'persisted.pdf', pinned: true, fileDirty: false, fileGroupId: '',
      },
    },
  }));
  assert.equal(workspace.querySelectorAll('[data-file-panel]').length, 2);
  assert.equal(workspace.querySelector('[data-qf-group="primary"]').querySelector('.is-active')?.dataset.filePanel, active.key);

  const zone = workspace.querySelector('[data-qf-drop-group="secondary"]');
  zone.dispatchEvent(new dom.window.Event('drop', {bubbles: true, cancelable: true}));
  assert.equal(workspace.querySelectorAll('[data-qf-group]').length, 2);
  assert.equal(workspace.querySelector('[data-qf-group="secondary"] [data-file-panel]')?.dataset.filePanel, 'persisted');
});
