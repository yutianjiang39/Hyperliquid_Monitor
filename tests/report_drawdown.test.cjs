'use strict';

// Run the report's real data adapters and HTML renderer against small event
// histories. No browser, network access or third-party packages are required.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const insights = require('../src/chart_insights.js');
const template = fs.readFileSync(path.join(__dirname, '../src/report_template.html'), 'utf8');

function section(start, end) {
  const first = template.indexOf(start);
  const last = template.indexOf(end, first);
  assert.ok(first >= 0 && last > first, 'Report source section must remain discoverable: ' + start);
  return template.slice(first, last);
}

// These are complete source sections, including the production number/time
// formatters and event identity mapping. Only DOM controls are stubbed below.
const formatters = section('const $ = id =>', 'const signClass =');
const curveFunctions = section('const CURVES=', 'function curveLocateInsight(');

function makeRows(specs) {
  const baseTime = Date.UTC(2025, 0, 2);
  return specs.map((spec, index) => {
    const row = typeof spec === 'number' ? {pnl: spec} : spec;
    return {
      time: baseTime + index * 1000,
      timeline_sequence: index + 1,
      event_sequence: index + 1,
      operation_id: 'trade-' + (index + 1),
      event_type: 'operation',
      action: 'reduce',
      market_type: 'perp',
      closed_pnl: 0,
      fee: 0,
      fee_token: 'USDC',
      cumulative_closed_pnl: row.pnl,
      observed_cumulative_closed_pnl: row.pnl,
      cumulative_net_pnl: row.netPnl ?? row.pnl,
      observed_cumulative_net_pnl: row.netPnl ?? row.pnl,
      balance_usdc: null,
      observed_balance_usdc: null,
      ...row
    };
  });
}

function report(specs, options = {}) {
  const rows = makeRows(specs);
  const controls = {
    curvePnlBasis: {value: options.pnlBasis || 'gross_known'},
    curvePositionBasis: {value: 'entry'},
    curveEquityBasis: {value: 'balance_known'},
    curveLeverageBasis: {value: 'balance_known'},
    curveInsightsPnl: {innerHTML: '', querySelectorAll: () => []}
  };
  const context = vm.createContext({
    window: {HLChartInsights: insights, __HL_CURVES__: {timeline_curve: rows}},
    document: {getElementById: id => {
      assert.ok(Object.hasOwn(controls, id), 'Unexpected DOM dependency: ' + id);
      return controls[id];
    }}
  });
  vm.runInContext(formatters + '\n' + curveFunctions + `
    curveState.from = Number(curveTimeline[0]?.time ?? 0);
    curveState.to = Number(curveTimeline.at(-1)?.time ?? 0);
    curveState.events = curveTimeline;
    globalThis.reportTest = {
      analyze: () => curveAnalyze('Pnl', curveState.events),
      clearCache: () => { curveState.insightsCache = {}; },
      render: stats => curveRenderInsights('Pnl', stats),
      details: (episode, index) => curveDrawdownDetails(episode, index),
      percent: (episode, signed) => curveDrawdownPercent(episode, signed)
    };
  `, context, {filename: 'report_template.drawdown.js'});
  return {
    ...context.reportTest,
    rows,
    controls,
    html: () => controls.curveInsightsPnl.innerHTML
  };
}

test('same-millisecond events use the peak event funds, never a later event balance', () => {
  const time = Date.UTC(2025, 0, 2);
  const view = report([
    {pnl: 500, time, balance_usdc: '2000'},
    {pnl: 300, time, balance_usdc: '1800'},
    {pnl: 300, time, balance_usdc: '11800', event_type: 'ledger'},
    {pnl: 500, time: time + 1, balance_usdc: '12000'}
  ]);
  const episode = view.analyze().drawdowns[0];
  assert.equal(episode.peak.index, 0);
  assert.equal(episode.trough.index, 1);
  assert.equal(episode.peak.time, episode.trough.time);
  assert.equal(episode.peakCapital, 2000);
  assert.equal(episode.percentage, 10);
  assert.equal(episode.peakCapitalBasis, 'strict');
  const html = view.details(episode, 0);
  assert.match(html, /−10%/);
  assert.match(html, /200 ÷ 2,000 × 100% = 10%/);
  assert.match(html, /data-insight-point="0"/);
  assert.match(html, /data-insight-point="1"/);
});

test('a transfer on a repeated PnL peak updates the start funds for the next decline', () => {
  const view = report([
    {pnl: 100, balance_usdc: '1000'},
    {pnl: 100, balance_usdc: '2000', event_type: 'ledger'},
    {pnl: 0, balance_usdc: '1900'}
  ]);
  const episode = view.analyze().drawdowns[0];
  assert.equal(episode.peak.index, 1);
  assert.equal(episode.peakCapital, 2000);
  assert.equal(episode.percentage, 5);
});

test('verified funds take priority over an available reference estimate', () => {
  const view = report([
    {pnl: 500, balance_usdc: '2000', observed_balance_usdc: '10000'},
    {pnl: 300, balance_usdc: '1800', observed_balance_usdc: '9800'}
  ]);
  const episode = view.analyze().drawdowns[0];
  assert.equal(episode.peakCapital, 2000);
  assert.equal(episode.percentage, 10);
  assert.equal(episode.peakCapitalBasis, 'strict');
  const html = view.details(episode, 0);
  assert.match(html, /连续可核对账面资金/);
  assert.doesNotMatch(html, /class="drawdown-capital-tag"/);
  assert.match(html, /200 ÷ 2,000 × 100% = 10%/);
});

test('missing verified funds use the same event reference and label it visibly', () => {
  const view = report([
    {pnl: 500, balance_usdc: null, observed_balance_usdc: '4000'},
    {pnl: 300, balance_usdc: null, observed_balance_usdc: '3800'}
  ]);
  const episode = view.analyze().drawdowns[0];
  assert.equal(episode.peakCapital, 4000);
  assert.equal(episode.percentage, 5);
  assert.equal(episode.peakCapitalBasis, 'known');
  const html = view.details(episode, 0);
  assert.match(html, /−5%/);
  assert.match(html, /class="drawdown-capital-tag">参考/);
  assert.match(html, /已知流水重建 · 参考/);
  assert.match(html, /200 ÷ 4,000 × 100% = 5%/);
});

test('zero or negative verified funds never fall back to a positive estimate', () => {
  for (const strictBalance of ['0', '-500']) {
    const view = report([
      {pnl: 100, balance_usdc: strictBalance, observed_balance_usdc: '10000'},
      {pnl: 0, balance_usdc: '-600', observed_balance_usdc: '9900'}
    ]);
    const episode = view.analyze().drawdowns[0];
    assert.equal(episode.amount, 100);
    assert.equal(episode.peakCapital, Number(strictBalance));
    assert.equal(episode.peakCapitalBasis, 'strict');
    assert.equal(episode.percentage, null);
    const html = view.details(episode, 0);
    assert.match(html, /−100 USDC/);
    assert.match(html, /drawdown-percent unavailable/);
    assert.match(html, /峰值时资金不大于零/);
    assert.match(html, /仅展示回撤金额/);
    assert.doesNotMatch(html, /class="drawdown-capital-tag"/);
    assert.doesNotMatch(html, /× 100% =/);
  }
});

test('missing start funds are not replaced by a future known balance', () => {
  const view = report([
    {pnl: 100},
    {pnl: 0, balance_usdc: '1000', observed_balance_usdc: '1000'},
    {pnl: 100, balance_usdc: '1100', observed_balance_usdc: '1100'}
  ]);
  const episode = view.analyze().drawdowns[0];
  assert.equal(episode.amount, 100);
  assert.equal(episode.peakCapital, null);
  assert.equal(episode.peakCapitalBasis, 'unknown');
  assert.equal(episode.percentage, null);
  const html = view.details(episode, 0);
  assert.match(html, /−100 USDC/);
  assert.match(html, /峰值时资金未知/);
  assert.match(html, /资金数据缺失/);
  assert.match(html, /drawdown-percent unavailable/);
  assert.doesNotMatch(html, /× 100% =/);
});

test('zero and negative cumulative PnL still use positive cash as the denominator', () => {
  for (const peakPnl of [0, -100]) {
    const view = report([
      {pnl: peakPnl, balance_usdc: '2000'},
      {pnl: peakPnl - 200, balance_usdc: '1800'}
    ]);
    const episode = view.analyze().drawdowns[0];
    assert.equal(episode.amount, 200);
    assert.equal(episode.peakCapital, 2000);
    assert.equal(episode.percentage, 10);
    assert.equal(view.percent(episode), '−10%');
    assert.equal(view.percent(episode, false), '10%');
  }
});

test('switching asset views and recalculating leaves peak-funds percentages unchanged', () => {
  const view = report([
    {pnl: 500, balance_usdc: '2000', observed_balance_usdc: '4000', account_equity: '80000', perp_equity: '60000'},
    {pnl: 300, balance_usdc: '1800', observed_balance_usdc: '3800', account_equity: '70000', perp_equity: '50000'}
  ]);
  for (const assetBasis of ['balance_known', 'balance_strict', 'account', 'perp']) {
    view.controls.curveEquityBasis.value = assetBasis;
    view.controls.curveLeverageBasis.value = assetBasis.startsWith('balance') ? assetBasis : 'official';
    // Also force a fresh calculation: a cached result must not conceal coupling.
    view.clearCache();
    const episode = view.analyze().drawdowns[0];
    assert.equal(episode.peakCapital, 2000);
    assert.equal(episode.peakCapitalBasis, 'strict');
    assert.equal(episode.percentage, 10);
  }
});

test('changing the PnL fee basis recalculates the numerator while retaining event cash', () => {
  const view = report([
    {pnl: 500, netPnl: 480, balance_usdc: '2000'},
    {pnl: 300, netPnl: 260, balance_usdc: '1780'}
  ]);
  const gross = view.analyze().drawdowns[0];
  assert.equal(gross.amount, 200);
  assert.equal(gross.percentage, 10);
  view.controls.curvePnlBasis.value = 'net_known';
  const net = view.analyze().drawdowns[0];
  assert.equal(net.amount, 220);
  assert.equal(net.peakCapital, 2000);
  assert.equal(net.percentage, 11);
});

test('all displayed episodes include their own funds, percentage, source and formula', () => {
  const view = report([
    {pnl: 100, balance_usdc: '1000'},
    {pnl: 50, balance_usdc: '950'},
    {pnl: 100, observed_balance_usdc: '100'},
    {pnl: 80, observed_balance_usdc: '80'},
    {pnl: 120, balance_usdc: '400'},
    {pnl: 80, balance_usdc: '360'}
  ]);
  const stats = view.analyze();
  assert.deepEqual(stats.drawdowns.map(d => d.amount), [50, 40, 20]);
  assert.deepEqual(stats.drawdowns.map(d => d.percentage), [5, 10, 20]);
  view.render(stats);
  const html = view.html();
  const items = html.match(/<details class="drawdown-item"[\s\S]*?<\/details>/g);
  assert.equal(items.length, 3);
  for (const [index, percentage, formula, source] of [
    [0, '−5%', '50 ÷ 1,000 × 100% = 5%', '连续可核对账面资金'],
    [1, '−10%', '40 ÷ 400 × 100% = 10%', '连续可核对账面资金'],
    [2, '−20%', '20 ÷ 100 × 100% = 20%', '已知流水重建 · 参考']
  ]) {
    assert.ok(items[index].includes(percentage));
    assert.ok(items[index].includes(formula));
    assert.ok(items[index].includes(source));
    assert.ok(items[index].includes('峰值时资金'));
  }
  assert.match(html, /按峰谷差额排序/);
  assert.match(html, /分母固定在该段起点/);
});

test('legacy balance field aliases are read at the same peak event', () => {
  const view = report([
    {pnl: 500, balance: '2500', observed_balance: '5000'},
    {pnl: 250, balance: '2250', observed_balance: '4750'}
  ]);
  const episode = view.analyze().drawdowns[0];
  assert.equal(episode.peakCapital, 2500);
  assert.equal(episode.peakCapitalBasis, 'strict');
  assert.equal(episode.percentage, 10);
});
