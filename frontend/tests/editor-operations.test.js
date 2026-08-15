import test from 'node:test';
import assert from 'node:assert/strict';
import {JSDOM} from 'jsdom';
import {Editor, Node} from '@tiptap/core';
import StarterKit from '@tiptap/starter-kit';
import {Markdown} from '@tiptap/markdown';
import {Mathematics} from '@tiptap/extension-mathematics';

import {insertBlockAt, moveQuestionBefore} from '../editor-operations.js';

const dom = new JSDOM('<!doctype html><html><body></body></html>', {pretendToBeVisual: true});
for (const key of ['window', 'document', 'navigator', 'Node', 'HTMLElement', 'MutationObserver',
  'DOMParser', 'getComputedStyle']) {
  Object.defineProperty(globalThis, key, {
    value: dom.window[key], configurable: true, writable: true,
  });
}
globalThis.requestAnimationFrame = callback => setTimeout(callback, 0);
globalThis.cancelAnimationFrame = clearTimeout;
globalThis.innerHeight = 900;
globalThis.innerWidth = 1400;

const HandoutQuestion = Node.create({
  name: 'handoutQuestion', group: 'block', atom: true, draggable: true, selectable: true,
  addAttributes() { return {blockId: {default: ''}}; },
  parseHTML() { return [{tag: 'section[data-test-question]'}]; },
  renderHTML() { return ['section', {'data-test-question': ''}]; },
});

function makeEditor(content, extra = []) {
  const element = document.createElement('div');
  document.body.append(element);
  return new Editor({element, extensions: [StarterKit, ...extra], content});
}

test('在正文中间插入块节点会拆分段落', () => {
  const editor = makeEditor('<p>abcdef</p>', [HandoutQuestion]);
  assert.equal(insertBlockAt(editor, 4, {
    type: 'handoutQuestion', attrs: {blockId: 'q_middle'},
  }), true);
  assert.deepEqual(editor.getJSON().content.map(node => node.type),
    ['paragraph', 'handoutQuestion', 'paragraph']);
  assert.equal(editor.getJSON().content[0].content[0].text, 'abc');
  assert.equal(editor.getJSON().content[2].content[0].text, 'def');
  editor.destroy();
});

test('拖动重排按 blockId 移动且不复制题目', () => {
  const editor = makeEditor({type: 'doc', content: ['q_one', 'q_two', 'q_three'].map(blockId => ({
    type: 'handoutQuestion', attrs: {blockId},
  }))}, [HandoutQuestion]);
  assert.equal(moveQuestionBefore(editor, 'q_three', 'q_one'), true);
  assert.deepEqual(editor.getJSON().content
    .filter(node => node.type === 'handoutQuestion').map(node => node.attrs.blockId),
    ['q_three', 'q_one', 'q_two']);
  editor.destroy();
});

test('中文正文支持撤销与重做', () => {
  const editor = makeEditor('<p></p>');
  editor.commands.insertContent('中文输入');
  assert.equal(editor.getText(), '中文输入');
  editor.commands.undo();
  assert.equal(editor.getText(), '');
  editor.commands.redo();
  assert.equal(editor.getText(), '中文输入');
  editor.destroy();
});

test('Markdown 多级标题和公式可解析、编辑并序列化', () => {
  const element = document.createElement('div');
  document.body.append(element);
  const editor = new Editor({
    element,
    extensions: [StarterKit, Markdown, Mathematics.configure({katexOptions: {throwOnError: false}})],
    content: '## 二级标题\n\n正文 $x^2$\n\n$$\nsum_{i=1}^n i\n$$',
    contentType: 'markdown',
  });
  assert.equal(editor.getJSON().content[0].type, 'heading');
  assert.equal(editor.getJSON().content[0].attrs.level, 2);
  let inlinePos = -1;
  editor.state.doc.descendants((node, pos) => {
    if (node.type.name === 'inlineMath') inlinePos = pos;
  });
  assert.ok(inlinePos > 0);
  editor.commands.updateInlineMath({latex: 'x^3+1', pos: inlinePos});
  const markdown = editor.getMarkdown();
  assert.match(markdown, /^## 二级标题/m);
  assert.match(markdown, /\$x\^3\+1\$/);
  assert.match(markdown, /\$\$/);
  editor.destroy();
});
