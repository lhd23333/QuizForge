import test from 'node:test';
import assert from 'node:assert/strict';

import {
  PAGE_BREAK_MARKER,
  createAutosave,
  hasUnsavedWork,
  numberLabels,
  parseHandoutBody,
  questionMarker,
  reconcileSaveSuccess,
  sourceIsLocallyEdited,
} from '../handout-model.js';

test('题目边界、显式分页与普通 Markdown 往返', () => {
  const marker = questionMarker({blockId: 'q_abcdef', body: '题干 $x$', solution: '解析'});
  const source = `# 标题\n\n${marker}\n\n${PAGE_BREAK_MARKER}\n\n正文`;
  const result = parseHandoutBody(source, {
    q_abcdef: {number_override: '例1', solution_placement: 'inline'},
  });
  assert.deepEqual(result.blocks.map(block => block.kind), ['markdown', 'question', 'markdown', 'pageBreak', 'markdown']);
  assert.equal(result.blocks[1].numberOverride, '例1');
  assert.equal(result.blocks[1].solutionPlacement, 'inline');
  assert.equal(result.blocks[1].body, '题干 $x$');
  assert.equal(result.blocks[1].solution, '解析');
  assert.deepEqual(result.warnings, []);
});

test('损坏题目标记保留原始 Markdown 并报告告警', () => {
  const source = '<!-- quizforge:question q_abcdef -->\n\n不能丢掉';
  const result = parseHandoutBody(source, {});
  assert.equal(result.blocks.length, 1);
  assert.equal(result.blocks[0].kind, 'markdown');
  assert.match(result.blocks[0].text, /不能丢掉/);
  assert.match(result.warnings[0], /缺少结束标记/);
});

test('自定义题号仍占用逻辑序号位置', () => {
  assert.deepEqual(numberLabels([
    {numberOverride: null}, {numberOverride: '练习A'}, {numberOverride: ''},
  ]), ['1', '练习A', '3']);
});

test('来源快照能识别讲义内本地改动', () => {
  const snapshot = {body: '原题', solution: '原解析'};
  assert.equal(sourceIsLocallyEdited({body: '原题', solution: '原解析'}, snapshot), false);
  assert.equal(sourceIsLocallyEdited({body: '改题', solution: '原解析'}, snapshot), true);
});

test('中文输入法组合期间不触发自动保存，结束后重新防抖', async () => {
  let callback = null;
  let saves = 0;
  const timers = {
    setTimeout(fn) { callback = fn; return 1; },
    clearTimeout() { callback = null; },
  };
  const autosave = createAutosave(async () => { saves += 1; }, 1000, timers);
  autosave.beginComposition();
  autosave.schedule();
  assert.equal(callback, null);
  assert.equal(saves, 0);
  autosave.endComposition();
  assert.equal(typeof callback, 'function');
  await callback();
  assert.equal(saves, 1);
});

test('题卡检查器草稿也属于未保存工作', () => {
  assert.equal(hasUnsavedWork(false, true), true);
  assert.equal(hasUnsavedWork(false, false), false);
});

test('保存响应只清除发起请求时的同一版草稿', () => {
  const savedMetadata = {title: '磁盘版本'};
  const currentMetadata = {title: '请求期间的新版本'};
  assert.deepEqual(reconcileSaveSuccess({
    saveRevision: 7, currentRevision: 7, currentMetadata, savedMetadata,
  }), {current: true, dirty: false, metadata: savedMetadata, reschedule: false});
  assert.deepEqual(reconcileSaveSuccess({
    saveRevision: 7, currentRevision: 8, currentMetadata, savedMetadata,
  }), {current: false, dirty: true, metadata: currentMetadata, reschedule: true});
});
