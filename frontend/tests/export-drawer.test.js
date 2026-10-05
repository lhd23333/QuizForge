import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {JSDOM} from 'jsdom';


const SOURCE = fs.readFileSync(
  new URL('../../static/js/export-drawer.js', import.meta.url), 'utf8');


// 与 templates/index.html 的导出抽屉同构的简化 DOM：保留 export-drawer.js
// 按 id / name 查询的全部元素与结构（预设↔四维联动、纸色组、prefill 链路）。
function makeDom(url = 'http://localhost/') {
  return new JSDOM(`<!doctype html><html><body>
    <aside id="export-panel" class="export-panel export-drawer hidden">
      <form method="post" action="/export" id="export-form">
        <input type="hidden" name="tags" value="">
        <input type="hidden" name="match" value="">
        <input type="hidden" name="type" value="">
        <input type="hidden" name="std_exam" id="export-std-exam" value="">
        <select name="mode" id="export-mode" hidden>
          <option value="exam">试卷</option>
          <option value="exam_std">标准试卷</option>
          <option value="note">笔记</option>
          <option value="lecture">讲解</option>
          <option value="slides">横版课件</option>
          <option value="practice">双栏刷题</option>
          <option value="list">清单</option>
          <option value="handout">讲义</option>
        </select>
        <input type="text" name="title" value="试卷">
        <input type="radio" name="solution_mode" value="none" checked>
        <input type="radio" name="solution_mode" value="inline">
        <input type="radio" name="solution_mode" value="separate">
        <input type="radio" name="preset" value="custom" checked>
        <input type="radio" name="preset" value="exam">
        <input type="radio" name="preset" value="exam_std">
        <input type="radio" name="preset" value="note">
        <input type="radio" name="preset" value="lecture">
        <input type="radio" name="preset" value="slides">
        <input type="radio" name="preset" value="practice">
        <input type="radio" name="preset" value="list">
        <input type="radio" name="preset" value="handout">
        <input type="radio" name="layout" value="flow" checked>
        <input type="radio" name="layout" value="compact">
        <input type="radio" name="layout" value="adaptive">
        <input type="radio" name="layout" value="one">
        <input type="radio" name="layout" value="two">
        <input type="radio" name="grouped" value="1" checked>
        <input type="radio" name="grouped" value="0">
        <input type="radio" name="columns" value="1" checked>
        <input type="radio" name="columns" value="2">
        <input type="radio" name="ratio" value="a4" checked>
        <input type="radio" name="ratio" value="wide">
        <select name="fmt" id="export-format">
          <option value="pdf">PDF</option>
          <option value="docx">Word</option>
          <option value="zip">ZIP</option>
        </select>
        <small id="word-export-hint" hidden></small>
        <details id="std-fields" style="display:none"></details>
        <div id="keypoints-field" class="keypoints-field hidden">
          <textarea name="keypoints"></textarea>
        </div>
        <select name="template_id" id="export-template">
          <option value="">内置模板</option>
          <option value="t-list" data-supported-modes="list">清单模板</option>
        </select>
        <select name="cjk_font" id="export-cjk-font">
          <option value="FandolSong">FandolSong</option>
        </select>
        <select name="latin_font" id="export-latin-font">
          <option value="LatinModernRoman">LatinModernRoman</option>
        </select>
        <small id="cjk-font-preview"></small>
        <small id="latin-font-preview"></small>
        <div id="paper-tone-field" class="export-field-block">
          <input type="radio" name="paper_tone_choice" value="white" checked>
          <input type="radio" name="paper_tone_choice" value="cream">
          <input type="radio" name="paper_tone_choice" value="custom">
          <input type="hidden" name="paper_tone" id="paper-tone-value" value="white">
          <div class="tone-swatches hidden" id="tone-swatches">
            <button type="button" class="tone-swatch" data-tone="#ADD8E6"></button>
            <button type="button" class="tone-swatch" data-tone="#DFF5E1"></button>
            <input type="color" id="tone-color" value="#ADD8E6">
          </div>
        </div>
        <label id="wimath-logo-field"><input type="checkbox" name="wimath_logo"></label>
        <button type="button" id="preview-btn">预览分页</button>
        <button type="submit">导出</button>
      </form>
    </aside>
    <button type="button" id="export-drawer-trigger" aria-expanded="false"></button>
    <button type="button" id="export-drawer-close"></button>
    <div class="fullpage-box hidden"></div>
    <div class="fullpage-box hidden"></div>
    <div id="preview-overlay" class="preview-overlay hidden">
      <b id="preview-title"></b><span id="preview-info"></span>
      <a id="preview-open" style="display:none"></a>
      <button type="button" id="preview-close"></button>
      <iframe id="preview-frame"></iframe>
    </div>
  </body></html>`, {
    url,
    runScripts: 'outside-only',
    pretendToBeVisual: true,
  });
}


function boot(dom, {configPayload = null} = {}) {
  const calls = [];
  dom.window.fetch = async (url, opts = {}) => {
    calls.push({url, opts});
    if (configPayload && url.endsWith('/config')) {
      return {ok: true, json: async () => ({ok: true, payload: configPayload})};
    }
    return {ok: true, json: async () => ({ok: true, url: '/outfile/tok'})};
  };
  dom.window.flashToast = () => {};
  dom.window.alert = () => {};
  dom.window.eval(SOURCE);
  return {calls, doc: dom.window.document, win: dom.window};
}


const settle = () => new Promise(resolve => setTimeout(resolve, 10));
const checkedValue = (doc, name) =>
  doc.querySelector(`input[name="${name}"]:checked`)?.value;
const clickRadio = (doc, win, name, value) => {
  const el = doc.querySelector(`input[name="${name}"][value="${value}"]`);
  el.checked = true;   // radio 组互斥由规范保证（含程序赋值）
  el.dispatchEvent(new win.Event('change', {bubbles: true}));
};


test('初始：mode 折算为 exam，专属面板与独占整页均隐藏', () => {
  const dom = makeDom();
  const {doc} = boot(dom);
  assert.equal(doc.getElementById('export-mode').value, 'exam');
  assert.equal(doc.getElementById('std-fields').style.display, 'none');
  assert.ok(doc.getElementById('keypoints-field').classList.contains('hidden'));
  for (const el of doc.querySelectorAll('.fullpage-box')) {
    assert.ok(el.classList.contains('hidden'));
  }
});


test('预设=标准试卷：四维回默认、卷头开关置位、说明面板显示', () => {
  const dom = makeDom();
  const {doc, win} = boot(dom);
  clickRadio(doc, win, 'preset', 'exam_std');
  assert.equal(checkedValue(doc, 'layout'), 'flow');
  assert.equal(checkedValue(doc, 'columns'), '1');
  assert.equal(doc.getElementById('export-std-exam').value, '1');
  assert.equal(doc.getElementById('export-mode').value, 'exam_std');
  assert.equal(doc.getElementById('std-fields').style.display, 'block');
});


test('预设=讲义/笔记：知识要点与独占整页跟随版式显隐', () => {
  const dom = makeDom();
  const {doc, win} = boot(dom);
  const keypoints = doc.getElementById('keypoints-field');
  clickRadio(doc, win, 'preset', 'handout');
  assert.equal(doc.getElementById('export-mode').value, 'handout');
  assert.ok(!keypoints.classList.contains('hidden'));
  for (const el of doc.querySelectorAll('.fullpage-box')) {
    assert.ok(!el.classList.contains('hidden'));
  }
  // 笔记与讲义同为「一页两题」：字段跟随版式（layout=two）而不是预设，
  // 后端对任意 two 组合都消费知识要点与 fullpage_ids。
  clickRadio(doc, win, 'preset', 'note');
  assert.equal(doc.getElementById('export-mode').value, 'note');
  assert.ok(!keypoints.classList.contains('hidden'));
  // 换成流式（试卷）后两项不再有意义，隐藏。
  clickRadio(doc, win, 'preset', 'exam');
  assert.ok(keypoints.classList.contains('hidden'));
  for (const el of doc.querySelectorAll('.fullpage-box')) {
    assert.ok(el.classList.contains('hidden'));
  }
});


test('改动任一维度后预设回退「自定义」且不回弹', () => {
  const dom = makeDom();
  const {doc, win} = boot(dom);
  clickRadio(doc, win, 'preset', 'exam');
  assert.equal(checkedValue(doc, 'preset'), 'exam');
  clickRadio(doc, win, 'layout', 'two');
  assert.equal(checkedValue(doc, 'preset'), 'custom');
  assert.equal(doc.getElementById('export-mode').value, 'note');
  // 改回与原预设一致的四维：按需求保持「自定义」，不自动回弹高亮。
  clickRadio(doc, win, 'layout', 'flow');
  assert.equal(checkedValue(doc, 'preset'), 'custom');
});


test('选横版 16:9：收敛一页一题 + 单栏，不兼容项被禁用', () => {
  const dom = makeDom();
  const {doc, win} = boot(dom);
  clickRadio(doc, win, 'ratio', 'wide');
  assert.equal(checkedValue(doc, 'layout'), 'one');
  assert.equal(checkedValue(doc, 'columns'), '1');
  assert.equal(doc.getElementById('export-mode').value, 'slides');
  const layout = value =>
    doc.querySelector(`input[name="layout"][value="${value}"]`);
  assert.ok(layout('flow').disabled);
  assert.ok(layout('compact').disabled);
  assert.ok(layout('adaptive').disabled);
  assert.ok(layout('two').disabled);
  assert.equal(layout('one').disabled, false);
  assert.ok(doc.querySelector('input[name="columns"][value="2"]').disabled);
});


test('选双栏：一页一题/两题被禁用，回单栏后解除', () => {
  const dom = makeDom();
  const {doc, win} = boot(dom);
  clickRadio(doc, win, 'columns', '2');
  assert.ok(doc.querySelector('input[name="layout"][value="one"]').disabled);
  assert.ok(doc.querySelector('input[name="layout"][value="two"]').disabled);
  assert.equal(
    doc.querySelector('input[name="layout"][value="flow"]').disabled, false);
  clickRadio(doc, win, 'columns', '1');
  assert.equal(
    doc.querySelector('input[name="layout"][value="one"]').disabled, false);
});


test('纸张颜色：radio 驱动 hidden 真值与色卡显隐', () => {
  const dom = makeDom();
  const {doc, win} = boot(dom);
  const hidden = () => doc.getElementById('paper-tone-value').value;
  clickRadio(doc, win, 'paper_tone_choice', 'cream');
  assert.equal(hidden(), 'cream');
  assert.ok(doc.getElementById('tone-swatches').classList.contains('hidden'));
  // 切「自定义」采用色卡当前颜色，不提交 "custom" 字面值。
  clickRadio(doc, win, 'paper_tone_choice', 'custom');
  assert.equal(hidden(), '#ADD8E6');
  assert.ok(!doc.getElementById('tone-swatches').classList.contains('hidden'));
  doc.querySelector('.tone-swatch[data-tone="#DFF5E1"]').click();
  assert.equal(hidden(), '#DFF5E1');
  assert.equal(checkedValue(doc, 'paper_tone_choice'), 'custom');
});


test('格式=Word：纸色与 WIMath 禁用，切回 PDF 恢复', () => {
  const dom = makeDom();
  const {doc, win} = boot(dom);
  const fmt = doc.getElementById('export-format');
  fmt.value = 'docx';
  fmt.dispatchEvent(new win.Event('change', {bubbles: true}));
  assert.ok(doc.querySelector('input[name="paper_tone_choice"]').disabled);
  assert.ok(doc.querySelector('#wimath-logo-field input').disabled);
  assert.equal(doc.getElementById('word-export-hint').hidden, false);
  fmt.value = 'pdf';
  fmt.dispatchEvent(new win.Event('change', {bubbles: true}));
  assert.equal(doc.querySelector('input[name="paper_tone_choice"]').disabled, false);
  assert.equal(doc.getElementById('word-export-hint').hidden, true);
});


test('模板下拉按折算 mode 过滤', () => {
  const dom = makeDom();
  const {doc, win} = boot(dom);
  const opt = doc.querySelector('#export-template option[value="t-list"]');
  assert.equal(opt.hidden, true);          // 初始 mode=exam，清单模板不可见
  clickRadio(doc, win, 'preset', 'list');
  assert.equal(opt.hidden, false);
  assert.equal(opt.disabled, false);
});


test('抽屉开合：触发、关闭与选题清空自动收起', () => {
  const dom = makeDom();
  const {doc} = boot(dom);
  const drawer = doc.getElementById('export-panel');
  const trigger = doc.getElementById('export-drawer-trigger');
  trigger.click();
  assert.equal(drawer.classList.contains('hidden'), false);
  assert.equal(trigger.getAttribute('aria-expanded'), 'true');
  dom.window.QFExportDrawer.closeIfOpen();
  assert.ok(drawer.classList.contains('hidden'));
  trigger.click();
  doc.getElementById('export-drawer-close').click();
  assert.ok(drawer.classList.contains('hidden'));
});


test('任务回填（四维 payload）：四维/纸色/pinned_ids/自动展开', async () => {
  const dom = makeDom('http://localhost/?export_task=t1');
  const {doc, calls} = boot(dom, {configPayload: {
    mode: 'list', layout: 'compact', grouped: '0', columns: '1', ratio: 'a4',
    std_exam: false, title: '回填卷', solution_mode: 'separate',
    paper_tone: '#DFF5E1', fmt: 'pdf', keypoints: '',
    pinned_ids: ['q1', 'q2'], tags: ['a'],
    header_footer: {}, std_opts: {},
  }});
  await settle();
  assert.ok(calls.some(c => c.url === '/export-tasks/t1/config'));
  assert.equal(checkedValue(doc, 'layout'), 'compact');
  assert.equal(checkedValue(doc, 'grouped'), '0');
  // compact+不分题型不在预设表里 → 自定义高亮；mode 折算为 list。
  assert.equal(checkedValue(doc, 'preset'), 'custom');
  assert.equal(doc.getElementById('export-mode').value, 'list');
  assert.equal(checkedValue(doc, 'solution_mode'), 'separate');
  assert.equal(doc.getElementById('export-format').value, 'pdf');
  assert.equal(doc.getElementById('paper-tone-value').value, '#DFF5E1');
  assert.deepEqual(
    [...doc.querySelectorAll('input[name="pinned_ids"]')].map(el => el.value),
    ['q1', 'q2']);
  assert.equal(doc.getElementById('export-panel').classList.contains('hidden'),
               false);
  // 一次性回填：地址栏的 export_task 参数已清理。
  assert.ok(!dom.window.location.search.includes('export_task'));
});


test('任务回填（旧 payload 无四维）：按 mode 反推四维且 mode 保留', async () => {
  const dom = makeDom('http://localhost/?export_task=t9');
  const {doc} = boot(dom, {configPayload: {
    mode: 'handout', keypoints: '本讲要点', title: '旧卷',
    solution_mode: 'none', paper_tone: 'cream', fmt: 'pdf',
    pinned_ids: [], tags: [], header_footer: {}, std_opts: {},
  }});
  await settle();
  assert.equal(checkedValue(doc, 'layout'), 'two');
  assert.equal(checkedValue(doc, 'grouped'), '0');
  assert.equal(checkedValue(doc, 'preset'), 'handout');
  assert.equal(doc.getElementById('export-mode').value, 'handout');
  assert.ok(!doc.getElementById('keypoints-field').classList.contains('hidden'));
  assert.equal(doc.getElementById('paper-tone-value').value, 'cream');
});


test('预览：POST /preview 固定 fmt=pdf 并打开弹层贴取件地址', async () => {
  const dom = makeDom();
  const {doc, calls} = boot(dom);
  doc.getElementById('preview-btn').click();
  await settle();
  const call = calls.find(c => c.url === '/preview');
  assert.ok(call);
  assert.equal(call.opts.body.get('fmt'), 'pdf');
  assert.equal(call.opts.body.get('preview_kind'), 'pdf');
  assert.equal(doc.getElementById('preview-overlay').classList.contains('hidden'),
               false);
  assert.ok(doc.getElementById('preview-frame')
    .getAttribute('src').endsWith('/outfile/tok'));
});


test('提交：POST /export 携带四维与纸色字段', async () => {
  const dom = makeDom();
  const {doc, calls} = boot(dom);
  doc.getElementById('export-form')
    .dispatchEvent(new dom.window.Event('submit', {bubbles: true, cancelable: true}));
  await settle();
  const call = calls.find(c => c.url === '/export');
  assert.ok(call);
  assert.equal(call.opts.body.get('layout'), 'flow');
  assert.equal(call.opts.body.get('grouped'), '1');
  assert.equal(call.opts.body.get('columns'), '1');
  assert.equal(call.opts.body.get('ratio'), 'a4');
  assert.equal(call.opts.body.get('mode'), 'exam');
  assert.equal(call.opts.body.get('paper_tone'), 'white');
});
