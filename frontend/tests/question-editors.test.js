import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import {JSDOM} from 'jsdom';

const inlineSource = fs.readFileSync(
  new URL('../../static/js/inline-editor.js', import.meta.url), 'utf8');
const standaloneSource = fs.readFileSync(
  new URL('../../static/js/question-editor.js', import.meta.url), 'utf8');

function flush() {
  return new Promise(resolve => setTimeout(resolve, 0));
}

function inlineMarkup() {
  return `<main id="q-list" data-custom-sort="0">
    <article class="card" data-id="q-1" draggable="true">
      <section class="inline-editor" data-id="q-1" data-new="0" data-collection="">
        <div class="inline-editor-workbench" data-mode="source">
          <textarea class="inline-body-source">题目草稿</textarea>
          <details class="inline-optional-field" data-inline-optional="solution" open>
            <summary><label class="inline-preview-toggle">
              <input type="checkbox" class="inline-preview-enabled"
                     data-preview-field="solution" checked>
            </label></summary>
            <textarea class="inline-solution-source">解析草稿</textarea>
          </details>
          <details class="inline-optional-field" data-inline-optional="note" open>
            <summary><label class="inline-preview-toggle">
              <input type="checkbox" class="inline-preview-enabled"
                     data-preview-field="note" checked>
            </label></summary>
            <textarea class="inline-note-source">备注草稿</textarea>
          </details>
          <div class="inline-preview-pane">
            <div class="inline-preview-status"></div>
            <div class="inline-preview-body"></div>
            <div class="inline-preview-solution" hidden>
              <div class="inline-preview-solution-body"></div>
            </div>
            <div class="inline-preview-note" hidden>
              <div class="inline-preview-note-body"></div>
            </div>
          </div>
        </div>
        <select class="inline-type"><option value="填空题" selected>填空题</option></select>
        <select class="inline-difficulty"><option value="3" selected>3</option></select>
        <input class="inline-source" value="本地题源">
        <input class="inline-tags" value="代数, 校内">
        <button type="button" class="inline-save">保存</button>
        <button type="button" class="inline-cancel">取消</button>
        <span class="inline-save-status"></span>
      </section>
    </article>
  </main>`;
}

function setupInline() {
  const dom = new JSDOM(`<!doctype html><html><body>${inlineMarkup()}</body></html>`, {
    url: 'http://localhost/', pretendToBeVisual: true, runScripts: 'outside-only',
  });
  dom.window.eval(inlineSource);
  return dom;
}

test('题卡解析和备注 textarea 在 84-280px 内增高并在超限后滚动', () => {
  const dom = setupInline();
  const {document, Event} = dom.window;
  const solution = document.querySelector('.inline-solution-source');
  const note = document.querySelector('.inline-note-source');
  let solutionHeight = 20;
  let noteHeight = 400;
  Object.defineProperty(solution, 'scrollHeight', {configurable: true, get: () => solutionHeight});
  Object.defineProperty(note, 'scrollHeight', {configurable: true, get: () => noteHeight});

  solution.dispatchEvent(new Event('input', {bubbles: true}));
  note.dispatchEvent(new Event('input', {bubbles: true}));
  assert.equal(solution.style.height, '84px');
  assert.equal(solution.style.overflowY, 'hidden');
  assert.equal(note.style.height, '280px');
  assert.equal(note.style.overflowY, 'auto');

  solutionHeight = 176;
  solution.dispatchEvent(new Event('input', {bubbles: true}));
  assert.equal(solution.style.height, '176px');
});

test('题卡预览勾选仅属于当前会话且不会进入保存 payload', async () => {
  const dom = setupInline();
  const {document, Event} = dom.window;
  const requests = [];
  dom.window.fetch = async (url, options) => {
    requests.push({url, options});
    return {ok: true, json: async () => ({ok: true, card_html: '<article></article>'})};
  };
  dom.window.QReplaceQuestionCard = () => {};
  const solutionToggle = document.querySelector('[data-preview-field="solution"]');
  const noteToggle = document.querySelector('[data-preview-field="note"]');
  solutionToggle.checked = false;
  noteToggle.checked = true;

  document.querySelector('.inline-save').dispatchEvent(new Event('click', {bubbles: true}));
  await flush();
  const payload = JSON.parse(requests.at(-1).options.body);
  assert.equal(payload.body, '题目草稿');
  assert.equal(payload.solution, '解析草稿');
  assert.equal(payload.note, '备注草稿');
  assert.equal(Object.hasOwn(payload, 'solutionPreview'), false);
  assert.equal(Object.hasOwn(payload, 'notePreview'), false);
  assert.equal(solutionToggle.hasAttribute('name'), false);
  assert.equal(noteToggle.hasAttribute('name'), false);
});

function standaloneMarkup() {
  return `<form class="standalone-question-editor" data-preview-url="/question/q-1/preview">
    <textarea name="body">独立页题目</textarea>
    <details class="inline-optional-field" open><summary><label class="inline-preview-toggle">
      <input type="checkbox" class="standalone-preview-enabled"
             data-preview-field="solution" checked>
    </label></summary>
      <textarea name="solution" class="standalone-auto-textarea">独立页解析</textarea>
    </details>
    <details class="inline-optional-field" open><summary><label class="inline-preview-toggle">
      <input type="checkbox" class="standalone-preview-enabled"
             data-preview-field="note" checked>
    </label></summary>
      <textarea name="note" class="standalone-auto-textarea">独立页备注</textarea>
    </details>
    <select name="type"><option value="解答题" selected>解答题</option></select>
    <select name="difficulty"><option value="4" selected>4</option></select>
    <input name="source" value="独立页题源">
    <input name="tags" value="几何, 校内">
    <aside class="standalone-preview-pane">
      <div class="inline-preview-status"></div>
      <div class="inline-preview-body"></div>
      <div class="inline-preview-solution" hidden><div class="inline-preview-solution-body"></div></div>
      <div class="inline-preview-note" hidden><div class="inline-preview-note-body"></div></div>
    </aside>
  </form>`;
}

test('独立编辑页复用正式预览且预览开关不进入表单', async () => {
  const dom = new JSDOM(`<!doctype html><html><body>${standaloneMarkup()}</body></html>`, {
    url: 'http://localhost/', pretendToBeVisual: true, runScripts: 'outside-only',
  });
  const {document, Event, FormData} = dom.window;
  const solution = document.querySelector('[name="solution"]');
  const note = document.querySelector('[name="note"]');
  let solutionHeight = 30;
  Object.defineProperty(solution, 'scrollHeight', {configurable: true, get: () => solutionHeight});
  Object.defineProperty(note, 'scrollHeight', {configurable: true, get: () => 420});
  const requests = [];
  dom.window.fetch = async (url, options) => {
    requests.push({url, options});
    return {ok: true, json: async () => ({
      ok: true,
      body_html: '<p>题目预览</p>',
      solution_html: '<p>解析预览</p>',
      note_html: '<p>备注预览</p>',
    })};
  };
  dom.window.eval(standaloneSource);
  await flush();
  await flush();

  assert.equal(solution.style.height, '84px');
  assert.equal(note.style.height, '280px');
  assert.equal(note.style.overflowY, 'auto');
  assert.equal(document.querySelector('.inline-preview-solution').hidden, false);
  assert.equal(document.querySelector('.inline-preview-note').hidden, false);
  const payload = JSON.parse(requests.at(-1).options.body);
  assert.deepEqual(payload, {
    body: '独立页题目', solution: '独立页解析', note: '独立页备注',
    type: '解答题', difficulty: '4', source: '独立页题源', tags: '几何, 校内',
  });

  const solutionToggle = document.querySelector('[data-preview-field="solution"]');
  solutionToggle.checked = false;
  solutionToggle.dispatchEvent(new Event('change', {bubbles: true}));
  await flush();
  await flush();
  assert.equal(document.querySelector('.inline-preview-solution').hidden, true);
  assert.equal(solution.value, '独立页解析');
  assert.equal(note.value, '独立页备注');

  solutionHeight = 190;
  solution.dispatchEvent(new Event('input', {bubbles: true}));
  assert.equal(solution.style.height, '190px');
  const formData = new FormData(document.querySelector('form'));
  assert.equal(formData.get('solution'), '独立页解析');
  assert.equal(formData.get('note'), '独立页备注');
  assert.equal([...formData.keys()].some(key => key.includes('preview')), false);
});
