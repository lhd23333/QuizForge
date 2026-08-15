import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {JSDOM} from 'jsdom';


test('KaTeX 后测量到选项溢出时会逐级降为单列', () => {
  const dom = new JSDOM(`<!doctype html><html><body>
    <div class="q-split-text">
      <div class="q-opts" data-cols="4">
        <span class="q-opt"></span><span class="q-opt"></span>
      </div>
    </div>
  </body></html>`, {runScripts: 'outside-only'});
  dom.window.renderMathInElement = () => {};
  dom.window.document.querySelectorAll('.q-opt').forEach(option => {
    Object.defineProperty(option, 'scrollWidth', {get: () => 240});
    Object.defineProperty(option, 'clientWidth', {
      get: () => Number(option.closest('.q-opts').dataset.cols) === 1 ? 260 : 100,
    });
  });
  const source = fs.readFileSync(
    new URL('../../static/js/math.js', import.meta.url), 'utf8');
  dom.window.eval(source);

  assert.equal(dom.window.document.querySelector('.q-opts').dataset.cols, '1');
});
