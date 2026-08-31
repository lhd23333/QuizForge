import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {JSDOM} from 'jsdom';


test('首次绑定保留服务端判定的一图一选项模式', () => {
  const dom = new JSDOM(`<!doctype html><html><body>
    <div id="prev-cards" data-subject="math">
      <article class="qcard">
        <select class="type-sel"><option selected>单选题</option></select>
        <textarea name="body_0">特殊格式选项正文</textarea>
        <div class="qcard-image-import" data-existing="4">
          <select class="qcard-img-mode" data-value="pair"></select>
          <select class="qcard-img-flow"><option value="column">上下排列</option>
            <option value="row">左右排列</option></select>
          <input class="qcard-img-mode-touched" value="">
          <input class="qcard-img-flow-touched" value="">
          <button class="qcard-image-pick"></button>
          <input class="qcard-image-input" type="file" multiple>
          <span class="qcard-image-count"></span>
          <div class="qcard-image-preview"></div>
        </div>
      </article>
    </div>
  </body></html>`, {runScripts: 'outside-only'});
  const source = fs.readFileSync(
    new URL('../../static/js/import-preview-images.js', import.meta.url), 'utf8');
  dom.window.eval(source);

  assert.equal(dom.window.document.querySelector('.qcard-img-mode').value, 'pair');
});


test('题卡内聚焦时可直接粘贴剪贴板图片', () => {
  const dom = new JSDOM(`<!doctype html><html><body>
    <div id="prev-cards" data-subject="math" data-max-image-bytes="1024">
      <article class="qcard">
        <select class="type-sel"><option selected>解答题</option></select>
        <textarea name="body_0"></textarea>
        <div class="qcard-image-import" data-existing="0">
          <select class="qcard-img-mode"></select>
          <select class="qcard-img-flow"><option value="column">上下排列</option></select>
          <input class="qcard-img-mode-touched" value="">
          <input class="qcard-img-flow-touched" value="">
          <button class="qcard-image-pick"></button>
          <input class="qcard-image-input" type="file" multiple>
          <span class="qcard-image-count"></span>
          <div class="qcard-image-preview"></div>
        </div>
      </article>
    </div>
  </body></html>`, {runScripts: 'outside-only', pretendToBeVisual: true});
  dom.window.URL.createObjectURL = () => 'blob:test';
  dom.window.URL.revokeObjectURL = () => {};
  dom.window.DataTransfer = class DataTransfer {
    constructor() {
      this._files = [];
      this.items = {add: file => this._files.push(file)};
    }
    get files() { return this._files; }
  };
  const input = dom.window.document.querySelector('.qcard-image-input');
  Object.defineProperty(input, 'files', {value: [], writable: true});
  const source = fs.readFileSync(
    new URL('../../static/js/import-preview-images.js', import.meta.url), 'utf8');
  dom.window.eval(source);

  dom.window.document.querySelector('textarea').focus();
  const event = new dom.window.Event('paste', {bubbles: true, cancelable: true});
  Object.defineProperty(event, 'clipboardData', {
    value: {files: [new dom.window.File([new Uint8Array([1, 2, 3])], '', {
      type: 'image/png',
    })]},
  });
  dom.window.document.dispatchEvent(event);

  assert.equal(event.defaultPrevented, true);
  assert.equal(input.files.length, 1);
  assert.match(input.files[0].name, /^clipboard-\d+-1\.png$/);
  assert.match(dom.window.document.querySelector('.qcard-image-count').textContent, /共 1 张/);
});


test('粘贴上限包含题卡已有图片', () => {
  const dom = new JSDOM(`<!doctype html><html><body>
    <div id="prev-cards" data-subject="math" data-max-image-bytes="1024">
      <article class="qcard">
        <select class="type-sel"><option selected>解答题</option></select>
        <textarea name="body_0"></textarea>
        <div class="qcard-image-import" data-existing="20">
          <select class="qcard-img-mode"></select>
          <select class="qcard-img-flow"><option value="column">上下排列</option></select>
          <input class="qcard-img-mode-touched" value="">
          <input class="qcard-img-flow-touched" value="">
          <button class="qcard-image-pick"></button>
          <input class="qcard-image-input" type="file" multiple>
          <span class="qcard-image-count"></span>
          <div class="qcard-image-preview"></div>
        </div>
      </article>
    </div>
  </body></html>`, {runScripts: 'outside-only', pretendToBeVisual: true});
  dom.window.URL.createObjectURL = () => 'blob:test';
  dom.window.URL.revokeObjectURL = () => {};
  dom.window.DataTransfer = class DataTransfer {
    constructor() {
      this._files = [];
      this.items = {add: file => this._files.push(file)};
    }
    get files() { return this._files; }
  };
  let alertMessage = '';
  dom.window.alert = message => { alertMessage = message; };
  const input = dom.window.document.querySelector('.qcard-image-input');
  Object.defineProperty(input, 'files', {value: [], writable: true});
  const source = fs.readFileSync(
    new URL('../../static/js/import-preview-images.js', import.meta.url), 'utf8');
  dom.window.eval(source);

  dom.window.document.querySelector('textarea').focus();
  const event = new dom.window.Event('paste', {bubbles: true, cancelable: true});
  Object.defineProperty(event, 'clipboardData', {
    value: {files: [new dom.window.File([new Uint8Array([1])], 'extra.png', {
      type: 'image/png',
    })]},
  });
  dom.window.document.dispatchEvent(event);

  assert.equal(input.files.length, 0);
  assert.match(alertMessage, /最多附加 20 张/);
});
