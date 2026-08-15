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
