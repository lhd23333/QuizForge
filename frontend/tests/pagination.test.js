import test from 'node:test';
import assert from 'node:assert/strict';

import {layoutPaginatedBlocks} from '../pagination.js';

test('A4 单栏完整块在页底自动移到下一页', () => {
  const result = layoutPaginatedBlocks([
    {height: 800}, {height: 300, marginTop: 10},
  ], 'a4-1');
  assert.equal(result.pageCount, 2);
  assert.equal(result.placements[1].page, 1);
  assert.equal(result.placements[1].column, 0);
});

test('A4 双栏按左栏、右栏、下一页顺序放置', () => {
  const result = layoutPaginatedBlocks([
    {height: 900}, {height: 900}, {height: 900},
  ], 'a4-2');
  assert.deepEqual(result.placements.map(item => [item.page, item.column]),
    [[0, 0], [0, 1], [1, 0]]);
});

test('显式分页无论当前栏位都进入下一页左栏', () => {
  const result = layoutPaginatedBlocks([
    {height: 120}, {kind: 'pageBreak'}, {height: 120},
  ], 'a4-2');
  assert.equal(result.placements[2].page, 1);
  assert.equal(result.placements[2].column, 0);
});

test('双栏大题首题按余量落位，后续大题各自另起一栏', () => {
  const fits = layoutPaginatedBlocks([
    {height: 300},
    {height: 420, practiceSolve: true},
    {height: 180, practiceSolve: true},
    {height: 160, practiceSolve: true},
  ], 'a4-2');
  assert.deepEqual(fits.placements.map(item => [item.page, item.column]),
    [[0, 0], [0, 0], [0, 1], [1, 0]]);

  const doesNotFit = layoutPaginatedBlocks([
    {height: 750}, {height: 420, practiceSolve: true},
  ], 'a4-2');
  assert.deepEqual(doesNotFit.placements.map(item => [item.page, item.column]),
    [[0, 0], [0, 1]]);
});

test('显式分页已为后续大题建立新栏时不重复跳栏', () => {
  const result = layoutPaginatedBlocks([
    {height: 300, practiceSolve: true},
    {kind: 'pageBreak'},
    {height: 300, practiceSolve: true},
  ], 'a4-2');
  assert.deepEqual(result.placements.map(item => [item.page, item.column]),
    [[0, 0], [0, 0], [1, 0]]);
});
