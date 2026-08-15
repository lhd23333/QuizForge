import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {JSDOM} from 'jsdom';


function loadLayoutScript(html) {
  const dom = new JSDOM(`<!doctype html><html><body>${html}</body></html>`, {
    pretendToBeVisual: true,
    runScripts: 'outside-only',
  });
  dom.window.alert = () => {};
  dom.window.qfRequests = [];
  dom.window.fetch = async (_url, options) => {
    const payload = JSON.parse(options?.body || '{}');
    dom.window.qfRequests.push({url: _url, payload});
    return {json: async () => ({ok: true, align: payload.align || ''})};
  };
  const source = fs.readFileSync(new URL('../../static/js/image-layout.js', import.meta.url), 'utf8');
  dom.window.eval(source);
  return dom;
}


test('解析混排图片宽度只计算一次且可即时切换到左侧', async () => {
  const dom = loadLayoutScript(`
    <article class="card">
      <details class="q-solution" open>
        <div class="img-layout-bar" data-id="q1" data-field="solution"
             data-layouts='[{"i":0,"w":35,"align":"right"}]' data-groups="[]">
          <span class="img-align-chip" data-align="left">左</span>
          <span class="img-align-chip active" data-align="right">右</span>
          <span class="img-width-wrap"><span class="img-width-value"></span>
            <span class="img-width-reset"></span></span>
        </div>
        <div class="solution-body">
          <div class="q-solution-flow q-solution-flow-right">
            <div class="q-solution-flow-img" data-split-lead="0" data-unit-count="1"
                 style="width:35%"><img src="/assets/a.png" style="width:100%"></div>
            <div class="q-stem">解析正文</div>
          </div>
        </div>
      </details>
    </article>`);
  const box = dom.window.document.querySelector('.qimg-resizable');
  assert.ok(box);
  assert.equal(box.style.width, '100%');
  assert.equal(dom.window.document.querySelector('.q-solution-flow-img').style.width, '35%');

  dom.window.document.querySelector('[data-align="left"]').click();
  await new Promise(resolve => dom.window.setTimeout(resolve, 0));
  const flow = dom.window.document.querySelector('.q-solution-flow');
  assert.ok(flow.classList.contains('q-solution-flow-left'));
  assert.ok(!flow.classList.contains('q-solution-flow-right'));
});


test('解析多图混排在任意组图上改方向都持久化到组首图', async () => {
  const dom = loadLayoutScript(`
    <article class="card">
      <details class="q-solution" open>
        <div class="img-layout-bar" data-id="q2" data-field="solution"
             data-layouts='[{"i":0,"w":35,"align":"right"},{"i":1,"w":35,"align":""}]'
             data-groups='[{"ids":[0,1],"row":false}]'>
          <span class="img-align-chip" data-align="left">左</span>
          <span class="img-align-chip active" data-align="right">右</span>
          <span class="img-width-wrap"><span class="img-width-value"></span>
            <span class="img-width-reset"></span></span>
          <span class="img-pick-label"></span>
        </div>
        <div class="solution-body">
          <div class="q-solution-flow q-solution-flow-right">
            <div class="q-solution-flow-img" data-split-lead="0" data-unit-count="2"
                 style="width:35%">
              <div class="q-fig-stack q-fig-stack-split">
                <img src="/assets/a.png" style="width:100%">
                <img src="/assets/b.png" style="width:100%">
              </div>
            </div>
            <div class="q-stem">解析正文</div>
          </div>
        </div>
      </details>
    </article>`);

  const images = dom.window.document.querySelectorAll('.qimg-resizable');
  images[1].click();
  assert.ok(dom.window.document.querySelector('[data-align="right"]').classList.contains('active'));

  dom.window.document.querySelector('[data-align="left"]').click();
  await new Promise(resolve => dom.window.setTimeout(resolve, 0));

  assert.equal(dom.window.qfRequests.length, 1);
  assert.equal(dom.window.qfRequests[0].payload.index, 0);
  assert.equal(dom.window.qfRequests[0].payload.align, 'left');
  const flow = dom.window.document.querySelector('.q-solution-flow');
  assert.ok(flow.classList.contains('q-solution-flow-left'));
  assert.ok(!flow.classList.contains('q-solution-flow-right'));
});
