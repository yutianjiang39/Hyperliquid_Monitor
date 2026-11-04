'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {analyze} = require('../src/chart_insights.js');

function points(values) {
  return values.map((value, index) => ({value, time: index * 1000, rank: index, index}));
}

test('missing data has no fabricated values, baseline or episodes', () => {
  const result = analyze(points([null, undefined, NaN, Infinity, -Infinity, '0', false]));
  assert.equal(result.count, 0);
  assert.equal(result.min, null);
  assert.equal(result.max, null);
  assert.equal(result.median, null);
  assert.equal(result.mean, null);
  assert.equal(result.first, null);
  assert.equal(result.last, null);
  assert.equal(result.segmentCount, 0);
  assert.deepEqual(result.drawdowns, []);
  assert.deepEqual(analyze(null), analyze([]));
});

test('point-weighted interpolated quantiles include zero and omit gaps', () => {
  const result = analyze(points([0, 4, null, 8, 12]));
  assert.equal(result.count, 4);
  assert.equal(result.median, 6);
  assert.equal(result.q1, 3);
  assert.equal(result.q3, 9);
  assert.equal(result.mean, 6);
  assert.equal(result.min.index, 0);
  assert.equal(result.max.index, 4);
  assert.equal(result.first.value, 0);
  assert.equal(result.last.value, 12);
  assert.equal(result.segmentCount, 2);
});

test('flat, single-point and increasing curves have no drawdown', () => {
  for (const values of [[0], [3, 3, 3], [-4, -3, 0, 10], [null, 8, null]]) {
    const result = analyze(points(values));
    assert.equal(result.drawdownCount, 0);
    assert.deepEqual(result.drawdowns, []);
  }
});

test('negative starting cumulative PnL is not silently rebased to zero', () => {
  const result = analyze(points([-100, -140, -120, -90]));
  const episode = result.drawdowns[0];
  assert.equal(result.drawdownCount, 1);
  assert.equal(episode.peak.value, -100);
  assert.equal(episode.trough.value, -140);
  assert.equal(episode.amount, 40);
  assert.equal(episode.status, 'recovered');
  assert.equal(episode.recovery.value, -90);
});

test('nested dips remain one episode until the old peak is recovered', () => {
  const result = analyze(points([100, 80, 95, 70, 85, 100, 115]));
  const episode = result.drawdowns[0];
  assert.equal(result.drawdownCount, 1);
  assert.equal(episode.peak.index, 0);
  assert.equal(episode.trough.index, 3);
  assert.equal(episode.recovery.index, 5);
  assert.equal(episode.end.index, 5);
  assert.equal(episode.amount, 30);
  assert.equal(episode.declineMs, 3000);
  assert.equal(episode.recoveryMs, 2000);
  assert.equal(episode.underwaterMs, 5000);
});

test('top three independent episodes rank by amount, including an open episode', () => {
  const result = analyze(points([100, 90, 100, 80, 110, 70, 115, 65]));
  assert.equal(result.drawdownCount, 4);
  assert.deepEqual(result.drawdowns.map(d => d.amount), [50, 40, 20]);
  assert.deepEqual(result.drawdowns.map(d => d.peak.index), [6, 4, 2]);
  const open = result.drawdowns[0];
  assert.equal(open.status, 'open');
  assert.equal(open.recovery, null);
  assert.equal(open.recoveryMs, null);
  assert.equal(open.end.index, 7);
});

test('missing records close episodes as unknown instead of recovering across a gap', () => {
  const result = analyze(points([100, 70, null, null, 110, 105, 110]));
  assert.equal(result.segmentCount, 2);
  assert.equal(result.drawdownCount, 2);
  const interrupted = result.drawdowns[0];
  assert.equal(interrupted.status, 'gap');
  assert.equal(interrupted.amount, 30);
  assert.equal(interrupted.end.index, 1);
  assert.equal(interrupted.recovery, null);
  assert.equal(interrupted.underwaterMs, 1000);
  assert.equal(result.drawdowns[1].status, 'recovered');
  assert.equal(result.drawdowns[1].peak.index, 4);
});

test('unrecovered interval ends at the last valid point, not necessarily its trough', () => {
  const result = analyze(points([100, 70, 90]));
  const episode = result.drawdowns[0];
  assert.equal(episode.status, 'open');
  assert.equal(episode.trough.index, 1);
  assert.equal(episode.end.index, 2);
  assert.equal(episode.declineMs, 1000);
  assert.equal(episode.underwaterMs, 2000);
});

test('equal peaks use the latest point, equal troughs use the first point', () => {
  const result = analyze(points([100, 100, 80, 80, 100, 100, 90]));
  assert.equal(result.drawdownCount, 2);
  assert.equal(result.drawdowns[0].peak.index, 1);
  assert.equal(result.drawdowns[0].trough.index, 2);
  assert.equal(result.drawdowns[0].recovery.index, 4);
  assert.equal(result.drawdowns[1].peak.index, 5);
  assert.equal(result.min.index, 2);
  assert.equal(result.max.index, 0);
});

test('equal amounts and identical timestamps keep stable incoming event order', () => {
  const rows = points([100, 90, 100, 90, 100, 90]).map(row => ({...row, time: 123}));
  const result = analyze(rows);
  assert.deepEqual(result.drawdowns.map(d => d.peak.index), [0, 2, 4]);
  assert.deepEqual(result.drawdowns.map(d => d.declineMs), [0, 0, 0]);
  assert.equal(result.drawdowns[0].recoveryMs, 0);
  assert.equal(result.drawdowns[2].status, 'open');
});

test('even tiny genuine drawdowns survive without an epsilon recovery rule', () => {
  const result = analyze(points([1, 1 - 1e-12, 1 - 1e-13]));
  assert.equal(result.drawdownCount, 1);
  assert.ok(result.drawdowns[0].amount > 0);
  assert.equal(result.drawdowns[0].status, 'open');
});

test('statistics and episodes do not mutate input or fabricate time durations', () => {
  const input = [
    Object.freeze({value: 8, time: NaN, rank: 12, index: 50}),
    Object.freeze({value: 2, time: 100, rank: 13, index: 51}),
    Object.freeze({value: 8, time: 50, rank: 14, index: 52})
  ];
  Object.freeze(input);
  const result = analyze(input);
  const episode = result.drawdowns[0];
  assert.notEqual(result.first, input[0]);
  assert.equal(episode.peak.index, 50);
  assert.equal(episode.trough.rank, 13);
  assert.equal(episode.declineMs, null);
  assert.equal(episode.recoveryMs, null);
  assert.equal(episode.underwaterMs, null);
  result.first.value = -999;
  assert.equal(input[0].value, 8);
});

test('missing point indices use array offsets and limits do not change total counts', () => {
  const rows = [100, 50, 110, 90, 120].map((value, index) => ({value, time: index}));
  assert.equal(analyze(rows).drawdowns[0].trough.index, 1);
  const topOne = analyze(rows, {limit: 1});
  assert.equal(topOne.drawdowns.length, 1);
  assert.equal(topOne.drawdownCount, 2);
  assert.deepEqual(analyze(rows, {limit: 0}).drawdowns, []);
  assert.deepEqual(analyze(rows, {limit: -1}).drawdowns, []);
  assert.equal(analyze(rows, {limit: 1.8}).drawdowns.length, 1);
});

test('drawdown percentage uses funds at the peak, not PnL or later funds', () => {
  const rows = points([500, 300, 500]).map((row, index) => ({
    ...row, capital: [2000, 1800, 10000][index]
  }));
  const episode = analyze(rows, {capitalOf: row => row.capital}).drawdowns[0];
  assert.equal(episode.amount, 200);
  assert.equal(episode.peakCapital, 2000);
  assert.equal(episode.percentage, 10);
  assert.equal(episode.status, 'recovered');
});

test('each independent drawdown fixes its own peak capital and keeps amount ranking', () => {
  const rows = points([100, 50, 100, 80, 120, 80]).map((row, index) => ({
    ...row, capital: [1000, 950, 100, 80, 400, 360][index]
  }));
  const result = analyze(rows, {capitalOf: row => row.capital});
  assert.deepEqual(result.drawdowns.map(d => d.amount), [50, 40, 20]);
  assert.deepEqual(result.drawdowns.map(d => d.peakCapital), [1000, 400, 100]);
  assert.deepEqual(result.drawdowns.map(d => d.percentage), [5, 10, 20]);
  assert.equal(result.drawdowns[1].status, 'open');
});

test('equal PnL peaks use capital at the latest peak even when funds changed', () => {
  const rows = points([200, 200, 100]).map((row, index) => ({
    ...row, capital: [1000, 2000, 1900][index]
  }));
  const episode = analyze(rows, {capitalOf: row => row.capital}).drawdowns[0];
  assert.equal(episode.peak.index, 1);
  assert.equal(episode.peakCapital, 2000);
  assert.equal(episode.percentage, 5);
});

test('negative PnL and losses exceeding capital do not distort percentages', () => {
  const rows = points([-100, -400]).map(row => ({...row, capital: 200}));
  const episode = analyze(rows, {capitalOf: row => row.capital}).drawdowns[0];
  assert.equal(episode.amount, 300);
  assert.equal(episode.peakCapital, 200);
  assert.equal(episode.percentage, 150);
});

test('missing, non-positive or non-numeric capital never fabricates a percentage', () => {
  const rows = points([100, 50]);
  const noCapital = analyze(rows).drawdowns[0];
  assert.equal(noCapital.peakCapital, null);
  assert.equal(noCapital.percentage, null);
  for (const capital of [null, undefined, NaN, Infinity, -Infinity, '1000', false, 0, -100]) {
    const episode = analyze(rows, {capitalOf: () => capital}).drawdowns[0];
    assert.equal(episode.amount, 50);
    assert.equal(episode.percentage, null);
    assert.equal(episode.peakCapital, capital === 0 || capital === -100 ? capital : null);
  }
  const overflow = analyze(rows, {capitalOf: () => Number.MIN_VALUE}).drawdowns[0];
  assert.equal(overflow.percentage, null);
});

test('capital cannot carry across a data gap or a missing peak balance', () => {
  const rows = points([100, 50, null, 200, 150, 200]).map((row, index) => ({
    ...row, capital: [1000, 950, null, null, 1950, 2000][index]
  }));
  const episodes = analyze(rows, {capitalOf: row => row.capital}).drawdowns;
  assert.equal(episodes[0].status, 'gap');
  assert.equal(episodes[0].percentage, 5);
  assert.equal(episodes[1].status, 'recovered');
  assert.equal(episodes[1].peakCapital, null);
  assert.equal(episodes[1].percentage, null);
});

test('capital lookup receives the original peak record and its input offset', () => {
  const rows = [
    Object.freeze({value: 100, time: 0, index: 50}),
    Object.freeze({value: 80, time: 1, index: 51}),
    Object.freeze({value: 110, time: 2, index: 52}),
    Object.freeze({value: 100, time: 3, index: 53})
  ];
  Object.freeze(rows);
  const calls = [];
  const result = analyze(rows, {capitalOf: (row, inputOffset) => {
    assert.equal(row, rows[inputOffset]);
    calls.push(inputOffset);
    return [1000, 980, 500, 490][inputOffset];
  }});
  assert.deepEqual(calls, [0, 2]);
  assert.deepEqual(result.drawdowns.map(d => d.percentage), [2, 2]);
  assert.equal(result.drawdowns[0].peak.index, 50);
  assert.equal(rows[0].peakCapital, undefined);
});

test('browser script works with no CommonJS loader or network dependencies', () => {
  const context = vm.createContext({});
  const source = fs.readFileSync(path.join(__dirname, '../src/chart_insights.js'), 'utf8');
  vm.runInContext(source, context);
  const result = vm.runInContext(
    'HLChartInsights.analyze([{value:10,time:0},{value:6,time:1},{value:10,time:2}])',
    context
  );
  assert.equal(result.median, 10);
  assert.equal(result.drawdowns[0].amount, 4);
  assert.equal(result.drawdowns[0].status, 'recovered');
});
