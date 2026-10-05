import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {JSDOM} from 'jsdom';


const SOURCE = fs.readFileSync(
  new URL('../../static/js/export-tasks.js', import.meta.url), 'utf8');


function task(overrides = {}) {
  return {
    id: 't1', title: '月考卷', fmt: 'pdf', bank: '数学题库',
    bank_path: 'D:/bank', status: 'running', stage: 'compiling',
    stage_text: '编译中（第 1/2 遍） · 已排出 3 页', percent: 50,
    question_count: 12, created_at: 1000, started_at: 1001,
    finished_at: null, error: '', artifact: null,
    payload: {mode: 'exam', scope: 'selected', solution_mode: 'inline'},
    ...overrides,
  };
}

const DONE_ARTIFACT = {
  path: 'D:/out/quiz_1/月考卷.pdf', name: '月考卷.pdf', token: 'a',
  url: '/outfile/a?dl=1', view_url: '/outfile/a',
};


function makeDom(initialTasks) {
  const dom = new JSDOM(`<!doctype html><html><body>
    <section id="export-tasks" data-poll-ms="2000">
      <section><div class="bo-list" id="export-list-active"></div>
        <p id="export-empty-active">空</p></section>
      <section><div class="bo-list" id="export-list-done"></div>
        <p id="export-empty-done">空</p></section>
      <section><div class="bo-list" id="export-list-stopped"></div>
        <p id="export-empty-stopped">空</p></section>
      <div id="export-empty-state"></div>
    </section>
    <dialog id="export-detail">
      <b id="export-detail-title"></b>
      <button type="button" data-export-detail-close>x</button>
      <div id="export-detail-body"></div>
      <footer id="export-detail-actions"></footer>
    </dialog>
    <dialog id="export-viewer">
      <b id="export-viewer-title"></b>
      <a id="export-viewer-download" href="#">dl</a>
      <button type="button" data-export-viewer-close>x</button>
      <iframe id="export-viewer-frame" src="about:blank"></iframe>
    </dialog>
    <script type="application/json" id="export-tasks-data">${
      JSON.stringify(initialTasks)}</script>
  </body></html>`, {
    url: 'http://localhost/export-tasks', runScripts: 'outside-only',
    pretendToBeVisual: true,
  });
  return dom;
}


function boot(dom, pool, {confirmAnswer = true} = {}) {
  // jsdom 至今未实现 dialog.showModal/close，用属性打桩替代——
  // 断言看 hasAttribute('open')，不依赖未实现的反射行为。
  for (const id of ['export-detail', 'export-viewer']) {
    const dialog = dom.window.document.getElementById(id);
    if (typeof dialog.showModal !== 'function') {
      dialog.showModal = function () { this.setAttribute('open', ''); };
      dialog.close = function () { this.removeAttribute('open'); };
    }
  }
  const calls = [];
  dom.window.fetch = async (url, opts = {}) => {
    calls.push({url, opts});
    if (url === '/export-tasks/status') {
      return {ok: true, json: async () => ({ok: true, tasks: pool.tasks})};
    }
    return {ok: true, json: async () => ({ok: true})};
  };
  dom.window.confirm = () => confirmAnswer;
  dom.window.alert = () => {};
  let tick = null;
  dom.window.setInterval = fn => { tick = fn; return 1; };
  dom.window.eval(SOURCE);
  return {calls, tick};
}

const settle = () => new Promise(resolve => setTimeout(resolve, 10));


test('首屏内联数据渲染：按状态归入三个分区并显隐空提示', () => {
  const dom = makeDom([
    task({id: 'act', status: 'running', percent: 50}),
    task({id: 'fin', status: 'done', stage_text: '已完成', percent: 100,
          finished_at: 1002, artifact: DONE_ARTIFACT}),
    task({id: 'stop', status: 'cancelled', stage_text: '已终止', percent: 40,
          finished_at: 1003}),
  ]);
  boot(dom, {tasks: []});
  const doc = dom.window.document;

  assert.equal(doc.querySelectorAll('#export-list-active .export-row').length, 1);
  assert.equal(doc.querySelectorAll('#export-list-done .export-row').length, 1);
  assert.equal(doc.querySelectorAll('#export-list-stopped .export-row').length, 1);
  assert.equal(doc.getElementById('export-empty-active').hidden, true);
  assert.equal(doc.getElementById('export-empty-state').hidden, true);

  const activeFill = doc.querySelector('#export-list-active .bo-bar-fill');
  assert.equal(activeFill.style.width, '50%');
  // 进行中只出「终止」，不出打开/删除
  assert.ok(doc.querySelector('#export-list-active .export-cancel'));
  assert.equal(doc.querySelector('#export-list-active .export-view'), null);

  const doneRow = doc.querySelector('#export-list-done .export-row');
  assert.ok(doneRow.querySelector('.export-view'));       // 打开 PDF
  assert.ok(doneRow.querySelector('.export-reveal'));     // 打开文件夹
  assert.ok(doneRow.querySelector('.export-retry'));
  assert.ok(doneRow.querySelector('.export-modify'));
  assert.ok(doneRow.querySelector('.export-delete'));

  const stopRow = doc.querySelector('#export-list-stopped .export-row');
  assert.ok(stopRow.querySelector('.export-retry'));
  assert.equal(stopRow.querySelector('.export-view'), null);   // 无产物
});


test('轮询后状态跨分区迁移：行移动且按钮集合随之更新', async () => {
  const pool = {tasks: [task({id: 'm1', status: 'running', percent: 50})]};
  const dom = makeDom(pool.tasks);
  const {tick} = boot(dom, pool);
  const doc = dom.window.document;

  pool.tasks = [task({
    id: 'm1', status: 'done', stage_text: '已完成', percent: 100,
    finished_at: 2000, artifact: DONE_ARTIFACT,
  })];
  await tick();

  assert.equal(doc.querySelectorAll('#export-list-active .export-row').length, 0);
  const doneRow = doc.querySelector('#export-list-done .export-row');
  assert.ok(doneRow);
  assert.equal(
    doneRow.querySelector('.bo-bar-fill').style.width, '100%');
  assert.ok(doneRow.querySelector('.export-view'));
  assert.equal(doneRow.querySelector('.export-cancel'), null);
  assert.equal(doc.getElementById('export-empty-done').hidden, true);
  assert.equal(doc.getElementById('export-empty-active').hidden, false);
});


test('终止任务：确认后 POST cancel 并触发刷新', async () => {
  const pool = {tasks: [task({id: 'c1', status: 'running'})]};
  const dom = makeDom(pool.tasks);
  const {calls, tick} = boot(dom, pool, {confirmAnswer: true});

  dom.window.document.querySelector('.export-cancel').click();
  await settle();

  const cancelCall = calls.find(c => c.url === '/export-tasks/c1/cancel');
  assert.ok(cancelCall, '应发送终止请求');
  assert.equal(cancelCall.opts.method, 'POST');
  await tick();   // 仍可正常轮询
});


test('终止任务被取消确认时不发请求', async () => {
  const pool = {tasks: [task({id: 'c2', status: 'running'})]};
  const dom = makeDom(pool.tasks);
  const {calls} = boot(dom, pool, {confirmAnswer: false});

  dom.window.document.querySelector('.export-cancel').click();
  await settle();
  assert.equal(calls.length, 0);
});


test('打开 PDF：填入查看器 iframe 并 showModal', () => {
  const dom = makeDom([
    task({id: 'v1', status: 'done', artifact: DONE_ARTIFACT}),
  ]);
  boot(dom, {tasks: []});
  const doc = dom.window.document;

  doc.querySelector('#export-list-done .export-view').click();
  const viewer = doc.getElementById('export-viewer');
  assert.equal(viewer.hasAttribute('open'), true);
  assert.ok(doc.getElementById('export-viewer-frame').src.endsWith('/outfile/a'));
  assert.equal(doc.getElementById('export-viewer-title').textContent,
               '月考卷.pdf');
});


test('顶层文档下载走带 download 属性的锚点', async () => {
  const dom = makeDom([
    task({id: 'd1', status: 'done', artifact: DONE_ARTIFACT}),
  ]);
  boot(dom, {tasks: []});
  const clicked = [];
  dom.window.HTMLAnchorElement.prototype.click = function () {
    clicked.push(`${this.getAttribute('href')}|${this.getAttribute('download')}`);
  };

  dom.window.document.querySelector('#export-list-done .export-download').click();
  await settle();
  assert.deepEqual(clicked, ['/outfile/a?dl=1|月考卷.pdf']);
});


test('删除历史：确认后 POST delete；取消确认不动', async () => {
  const pool = {tasks: [
    task({id: 'x1', status: 'cancelled', finished_at: 5}),
  ]};
  const dom = makeDom(pool.tasks);
  const {calls} = boot(dom, pool, {confirmAnswer: false});
  const doc = dom.window.document;

  doc.querySelector('#export-list-stopped .export-delete').click();
  await settle();
  assert.equal(calls.filter(c => c.url.endsWith('/delete')).length, 0);

  dom.window.confirm = () => true;
  doc.querySelector('#export-list-stopped .export-delete').click();
  await settle();
  const del = calls.find(c => c.url === '/export-tasks/x1/delete');
  assert.ok(del);
  assert.equal(del.opts.method, 'POST');
});


test('点任务名称打开详情：展示来源题库与配置', () => {
  const dom = makeDom([
    task({id: 'i1', status: 'failed', stage_text: '失败', percent: 50,
          error: 'xelatex 返回码 1', finished_at: 9,
          payload: {mode: 'exam_std', scope: 'selected',
                    solution_mode: 'inline', subject: '数学',
                    pinned_ids: ['q1', 'q2', 'q3']}}),
  ]);
  boot(dom, {tasks: []});
  const doc = dom.window.document;

  doc.querySelector('#export-list-stopped .export-name').click();
  assert.equal(doc.getElementById('export-detail').hasAttribute('open'), true);
  assert.equal(doc.getElementById('export-detail-title').textContent, '月考卷');
  const body = doc.getElementById('export-detail-body').textContent;
  assert.ok(body.includes('数学题库'));
  assert.ok(body.includes('标准试卷'));
  assert.ok(body.includes('固定 3 道'));
  assert.ok(body.includes('xelatex 返回码 1'));
  // 失败任务在详情里同样提供重新导出入口
  assert.ok(doc.querySelector('#export-detail-actions .export-retry'));
});


test('空任务列表显示空状态提示', () => {
  const dom = makeDom([]);
  boot(dom, {tasks: []});
  const doc = dom.window.document;
  assert.equal(doc.getElementById('export-empty-state').hidden, false);
  assert.equal(doc.getElementById('export-empty-active').hidden, false);
  assert.equal(doc.querySelectorAll('.export-row').length, 0);
});
