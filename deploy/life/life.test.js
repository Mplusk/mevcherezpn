'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { nextGeneration } = require('./life.js');

function board(points, w = 8, h = 8) {
  const cells = new Uint8Array(w * h);
  for (const [x, y] of points) cells[y * w + x] = 1;
  return cells;
}
test('a block remains stable', () => {
  const cells = board([[2, 2], [2, 3], [3, 2], [3, 3]]);
  assert.deepEqual(nextGeneration(cells, 8, 8), cells);
});
test('a blinker alternates with a period of two', () => {
  const initial = board([[2, 3], [3, 3], [4, 3]]);
  const vertical = board([[3, 2], [3, 3], [3, 4]]);
  assert.deepEqual(nextGeneration(initial, 8, 8), vertical);
  assert.deepEqual(nextGeneration(vertical, 8, 8), initial);
});
test('a glider travels one cell diagonally in four generations', () => {
  let cells = board([[1, 0], [2, 1], [0, 2], [1, 2], [2, 2]]);
  for (let i = 0; i < 4; i++) cells = nextGeneration(cells, 8, 8);
  assert.deepEqual(cells, board([[2, 1], [3, 2], [1, 3], [2, 3], [3, 3]]));
});
test('neighbors wrap around opposite edges', () => {
  const cells = board([[7, 7], [0, 7], [7, 0]]);
  assert.equal(nextGeneration(cells, 8, 8)[0], 1);
});
test('invalid board sizes fail rather than silently evolving', () => {
  assert.throws(() => nextGeneration(new Uint8Array(5), 2, 2), RangeError);
});
