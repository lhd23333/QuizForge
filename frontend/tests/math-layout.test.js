import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {JSDOM} from 'jsdom';


test('KaTeX 后会测量普通题卡的固有宽度并逐级降列', () => {
  const dom = new JSDOM(`<!doctype html><html><body>
    <div class="q-opts" data-cols="4">
      <span class="q-opt"></span><span class="q-opt"></span>
    </div>
  </body></html>`, {runScripts: 'outside-only'});
  dom.window.renderMathInElement = () => {};
  const grid = dom.window.document.querySelector('.q-opts');
  Object.defineProperty(grid, 'clientWidth', {get: () => 400});
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

test('容器缩窄后降列，恢复宽度后回到服务端首选列数', () => {
  const dom = new JSDOM(`<!doctype html><html><body>
    <div class="q-opts" data-cols="4"><span class="q-opt"></span></div>
  </body></html>`, {runScripts: 'outside-only'});
  dom.window.renderMathInElement = () => {};
  let observerCallback;
  dom.window.ResizeObserver = class {
    constructor(callback) { observerCallback = callback; }
    observe() {}
  };
  let wide = true;
  const grid = dom.window.document.querySelector('.q-opts');
  const option = grid.querySelector('.q-opt');
  Object.defineProperty(grid, 'clientWidth', {get: () => wide ? 800 : 320});
  Object.defineProperty(option, 'scrollWidth', {get: () => 150});
  Object.defineProperty(option, 'clientWidth', {
    get: () => {
      const cols = Number(grid.dataset.cols);
      if (cols === 4) return wide ? 180 : 70;
      return wide ? 360 : 150;
    },
  });
  const source = fs.readFileSync(
    new URL('../../static/js/math.js', import.meta.url), 'utf8');
  dom.window.eval(source);
  assert.equal(grid.dataset.cols, '4');

  wide = false;
  observerCallback([{target: grid}]);
  assert.equal(grid.dataset.cols, '2');

  wide = true;
  observerCallback([{target: grid}]);
  assert.equal(grid.dataset.cols, '4');
});

test('题卡选项节点替换后解除旧监听并监听新节点', async () => {
  const dom = new JSDOM(`<!doctype html><html><body>
    <div id="card"><div class="q-opts" data-cols="2"><span class="q-opt"></span></div></div>
  </body></html>`, {runScripts: 'outside-only'});
  dom.window.renderMathInElement = () => {};
  const observed = [];
  const unobserved = [];
  dom.window.ResizeObserver = class {
    observe(target) { observed.push(target); }
    unobserve(target) { unobserved.push(target); }
  };
  const source = fs.readFileSync(
    new URL('../../static/js/math.js', import.meta.url), 'utf8');
  dom.window.eval(source);

  const oldGrid = dom.window.document.querySelector('.q-opts');
  const replacement = oldGrid.cloneNode(true);
  oldGrid.replaceWith(replacement);
  await new Promise(resolve => dom.window.setTimeout(resolve, 0));

  assert.ok(unobserved.includes(oldGrid));
  assert.ok(observed.includes(replacement));
});
