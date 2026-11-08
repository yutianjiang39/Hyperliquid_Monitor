/* Offline wallet report. Reads window.HL_MODEL (inlined) and lazily loads
 * table chunks from data/*.js via <script> tags (file:// safe). */
(function () {
  'use strict';
  const M = window.HL_MODEL;
  const DAY = 86400000;
  const state = { page: null, window: 'all', charts: [], tables: {}, loaded: {}, pending: {} };

  // ---------- formatting ----------
  const isNum = (v) => typeof v === 'number' && Number.isFinite(v);
  const num = (v) => (v === null || v === undefined || v === '' ? null : Number(v));
  function usd(v, digits) {
    v = num(v);
    if (!isNum(v)) return '—';
    const a = Math.abs(v), s = v < 0 ? '−' : '';
    if (digits === undefined) {
      if (a >= 1e9) return s + '$' + (a / 1e9).toFixed(2) + 'B';
      if (a >= 1e6) return s + '$' + (a / 1e6).toFixed(2) + 'M';
      if (a >= 1e4) return s + '$' + (a / 1e3).toFixed(1) + 'K';
      return s + '$' + a.toFixed(a >= 100 ? 0 : 2);
    }
    return s + '$' + a.toLocaleString('en-US', { maximumFractionDigits: digits, minimumFractionDigits: digits });
  }
  function pct(v, d) {
    v = num(v);
    if (!isNum(v)) return '—';
    return (v * 100).toFixed(d === undefined ? (Math.abs(v) >= 10 ? 0 : 1) : d) + '%';
  }
  function fix(v, d) { v = num(v); return isNum(v) ? v.toFixed(d === undefined ? 2 : d) : '—'; }
  function int(v) { v = num(v); return isNum(v) ? Math.round(v).toLocaleString('en-US') : '—'; }
  function date(ms, withTime) {
    if (!ms) return '—';
    const s = new Date(Number(ms)).toISOString();
    return withTime ? s.slice(0, 16).replace('T', ' ') : s.slice(0, 10);
  }
  function esc(s) {
    return String(s === null || s === undefined ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }
  function hours(h) {
    if (!isNum(h)) return '—';
    if (h < 1) return Math.round(h * 60) + ' 分钟';
    if (h < 48) return h.toFixed(1) + ' 小时';
    return (h / 24).toFixed(1) + ' 天';
  }
  const SOURCE = { official: '官方', rebuilt: '重建', hybrid: '混合', daily: '日序列', fills: '成交', campaigns: '回合', candles: 'K线' };
  const STATUS = { ok: ['ok', '✓', '对账通过'], warn: ['warn', '!', '对账有偏差'], fail: ['fail', '✕', '对账失败'], no_data: ['', '?', '无对账数据'] };

  // ---------- theme / colors ----------
  function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
  function C() {
    return {
      ink1: css('--ink-1'), ink2: css('--ink-2'), muted: css('--ink-muted'), grid: css('--grid'), axis: css('--axis'),
      surface: css('--surface-1'), s1: css('--series-1'), s2: css('--series-2'), s3: css('--series-3'),
      s4: css('--series-4'), s5: css('--series-5'), s6: css('--series-6'),
      pos: css('--pos'), neg: css('--neg'), mid: css('--mid'), seq0: css('--seq-0'), seq1: css('--seq-1'),
    };
  }
  function baseOption(c, extra) {
    return Object.assign({
      animation: false,
      textStyle: { fontFamily: 'system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif', color: c.ink2 },
      grid: { left: 64, right: 24, top: 36, bottom: 40, containLabel: false },
      tooltip: { trigger: 'axis', backgroundColor: c.surface, borderColor: c.grid, textStyle: { color: c.ink1, fontSize: 12 },
        axisPointer: { type: 'line', lineStyle: { color: c.axis } } },
    }, extra || {});
  }
  function timeAxis(c) {
    return { type: 'time', axisLine: { lineStyle: { color: c.axis } }, axisLabel: { color: c.muted, hideOverlap: true },
      splitLine: { show: false }, axisTick: { show: false } };
  }
  function valueAxis(c, fmt) {
    return { type: 'value', scale: false, axisLine: { show: false }, axisTick: { show: false },
      axisLabel: { color: c.muted, formatter: fmt || ((v) => usd(v)) }, splitLine: { lineStyle: { color: c.grid, width: 1 } } };
  }
  function legend(c) { return { top: 0, right: 0, textStyle: { color: c.ink2 }, icon: 'roundRect', itemWidth: 12, itemHeight: 4 }; }

  function chart(el, build) {
    if (!el) return;
    const inst = echarts.init(el, null, { renderer: 'canvas' });
    const entry = { el, inst, build };
    state.charts.push(entry);
    inst.setOption(build(C()), true);
    return inst;
  }
  function rerender() {
    const c = C();
    state.charts.forEach((e) => { if (e.el.offsetParent !== null) { e.inst.setOption(e.build(c), true); e.inst.resize(); } });
  }
  window.addEventListener('resize', () => state.charts.forEach((e) => e.inst.resize()));

  // ---------- data helpers ----------
  function windowStart() {
    const now = M.generated_at_ms;
    if (state.window === 'all') return -Infinity;
    if (state.window === 'ytd') return Date.UTC(new Date(now).getUTCFullYear(), 0, 1);
    return now - parseInt(state.window, 10) * DAY;
  }
  const inWin = (t) => t > windowStart();
  const days = () => (M.series.days || []).filter((d) => inWin(d.time));
  const W = () => (M.windows || {})[state.window] || {};

  function receive(name, index, rows) {
    const tbl = M.tables[name];
    (state.pending[name] = state.pending[name] || [])[index] = rows;
    const got = state.pending[name].filter(Boolean).length;
    if (got === tbl.chunks.length) {
      const out = [];
      state.pending[name].forEach((part) => part.forEach((r) => {
        const o = {};
        tbl.fields.forEach((f, i) => { o[f] = r[i]; });
        out.push(o);
      }));
      state.loaded[name].resolve(out);
    }
  }
  window.HL = { receive };
  function load(name) {
    if (state.loaded[name]) return state.loaded[name].promise;
    let resolve;
    const promise = new Promise((r) => { resolve = r; });
    state.loaded[name] = { promise, resolve };
    const tbl = M.tables[name];
    if (!tbl || !tbl.chunks.length) { resolve([]); return promise; }
    tbl.chunks.forEach((src) => {
      const s = document.createElement('script');
      s.src = src;
      s.onerror = () => console.error('无法载入 ' + src + '：请保留报告目录中的 data 文件夹');
      document.body.appendChild(s);
    });
    return promise;
  }

  // ---------- building blocks ----------
  function card(title, body, opts) {
    opts = opts || {};
    return `<div class="${opts.col || 'col-12'}"><div class="card h-100"><div class="card-header"><h3 class="card-title">${title}${opts.sub ? `<small>${opts.sub}</small>` : ''}</h3>${opts.actions || ''}</div><div class="card-body${opts.flush ? ' p-0' : ''}">${body}</div></div></div>`;
  }
  function kpi(label, value, sub, source, col) {
    return `<div class="${col || 'col-6 col-md-4 col-xl-2'}"><div class="card kpi"><div class="card-body py-3">
      <div class="kpi-label">${label}${source ? `<span class="src">${SOURCE[source] || source}</span>` : ''}</div>
      <div class="kpi-value${String(value).replace(/<[^>]+>/g, '').length > 12 ? ' long' : ''}">${value}</div><div class="kpi-sub">${sub || '&nbsp;'}</div></div></div></div>`;
  }
  function statTable(rows) {
    return `<table class="table table-sm stat-table mb-0"><tbody>${rows.map((r) => `<tr><td>${r[0]}</td><td class="num">${r[1]}</td>${r[2] !== undefined ? `<td class="muted small">${r[2]}</td>` : ''}</tr>`).join('')}</tbody></table>`;
  }
  function statusBadge(status, text) {
    const s = STATUS[status] || STATUS.no_data;
    return `<span class="status ${s[0]}"><span class="icon">${s[1]}</span>${esc(text || s[2])}</span>`;
  }
  const chartDiv = (id, cls) => `<div class="chart ${cls || ''}" id="${id}"></div>`;
  function table(id, name, columns, opts) {
    opts = opts || {};
    const el = document.getElementById(id);
    if (!el || state.tables[id]) return;
    el.innerHTML = '<div class="empty">载入中…</div>';
    const make = (rows) => {
      el.innerHTML = '';
      state.tables[id] = new Tabulator(el, Object.assign({
        data: rows, columns, layout: 'fitDataStretch', height: opts.height || 480, pagination: false,
        placeholder: '无记录', renderVertical: 'virtual', columnDefaults: { headerTooltip: true },
      }, opts.tabulator || {}));
    };
    if (Array.isArray(name)) make(name); else load(name).then(make);
  }
  const fmtCell = (fn) => (cell) => fn(cell.getValue());
  const yesFmt = (cell) => (cell.getValue() ? '是' : '');
  const col = (title, field, extra) => Object.assign({ title, field, headerFilter: 'input' }, extra || {});
  const numCol = (title, field, fn, extra) => Object.assign({ title, field, hozAlign: 'right', sorter: 'number', formatter: fmtCell(fn || ((v) => (v === null ? '' : Number(v).toLocaleString('en-US', { maximumFractionDigits: 6 })))) }, extra || {});
  const timeCol = (title, field) => ({ title, field, sorter: 'number', formatter: fmtCell((v) => date(v, true)), width: 140 });

  // ---------- pages ----------
  const PAGES = [
    ['overview', '总览', true], ['scorecard', '指标总表', true], ['performance', '业绩', true], ['risk', '风险', true], ['trading', '交易行为', false],
    ['positions', '持仓与资产', false], ['costs', '成本', false], ['flows', '资金流', false], ['quality', '数据质量', false], ['raw', '原始数据', false],
  ];
  const RENDER = {};

  RENDER.overview = function (root) {
    const p = W().performance || {}, r = W().risk || {}, b = M.behavior || {}, cur = M.current || {};
    const rec = (M.quality || {}).reconciliation || {};
    const allPnl = num((M.official.windows.allTime || {}).pnl);
    root.innerHTML = `<div class="row row-cards">
      ${kpi('官方累计盈亏', usd(allPnl), '全部历史 · 含现货', 'official')}
      ${kpi('当前账户净值', usd(cur.equity ?? M.official.equity), cur.mode ? '账户模式 ' + esc(cur.mode) : '', 'official')}
      ${kpi('窗口盈亏', usd(p.pnl), W().label || '', 'hybrid')}
      ${kpi('时间加权收益 TWR', pct(p.twr), '年化 ' + pct(p.cagr), 'hybrid')}
      ${kpi('最大回撤', pct(r.max_drawdown), usd(r.max_drawdown_usd) + ' 累计盈亏回撤', 'hybrid')}
      ${kpi('Sharpe', fix(r.sharpe), 'Sortino ' + fix(r.sortino), 'hybrid')}
      ${kpi('回合胜率', pct(b.win_rate), int(b.campaigns_evaluated) + ' 个完整回合', 'rebuilt')}
      ${kpi('盈亏比', fix(b.payoff_ratio), 'Profit factor ' + fix(b.profit_factor), 'rebuilt')}
      ${kpi('官方成交额', usd(num((M.official.windows.allTime || {}).vlm)), '全部历史', 'official')}
      ${kpi('当前杠杆', isNum(cur.leverage) ? cur.leverage.toFixed(cur.leverage < 0.1 ? 3 : 2) + '×' : '—', '仓位 ' + int(cur.position_count) + ' 个', 'official')}
      ${kpi('对账残差', pct(rec.residual_pct), usd(rec.residual) + ' / 官方 ' + usd(rec.official), 'hybrid')}
      ${kpi('资金净流入', usd(p.net_flow), '窗口内隐含（净值−盈亏）', 'official')}
      ${card('账户净值', chartDiv('c-equity'), { col: 'col-lg-8', sub: '官方 portfolio 采样，日间按重建盈亏形状补点' })}
      ${card('资产构成', chartDiv('c-buckets'), { col: 'col-lg-4', sub: '当前快照' })}
      ${card('累计永续盈亏：官方 vs 重建', chartDiv('c-recon', 'tall'), { col: 'col-lg-8', sub: '差值即残差，见“数据质量”' })}
      ${card('钱包画像', profileHtml(), { col: 'col-lg-4', sub: '规则标签，阈值见下' })}
      ${card('主要持仓', '<div id="t-top" class="table-host"></div>', { flush: true })}
    </div>`;
    chart(document.getElementById('c-equity'), (c) => {
      const d = days();
      return baseOption(c, { xAxis: timeAxis(c), yAxis: valueAxis(c),
        series: [{ type: 'line', name: '账户净值', showSymbol: false, lineStyle: { width: 2, color: c.s1 }, itemStyle: { color: c.s1 },
          areaStyle: { color: c.s1, opacity: 0.12 }, data: d.map((x) => [x.time, x.equity]) }],
        tooltip: Object.assign(baseOption(c).tooltip, { valueFormatter: (v) => usd(v, 0) }) });
    });
    chart(document.getElementById('c-buckets'), (c) => donut(c, (cur.buckets || []).map((x) => ({ name: x.bucket, value: x.value }))));
    chart(document.getElementById('c-recon'), (c) => {
      const off = (M.official.series_perp || []).filter((x) => inWin(x.time));
      const base0 = off.length ? off[0].cum_pnl : 0;
      let acc = 0;
      const mdays = M.mtm.days || [];
      const rec = [];
      const startT = off.length ? off[0].time : -Infinity;
      // Both lines start at 0 at the first official sample of the window; a
      // reconstruction that begins later (truncated history) joins the
      // official line where it starts.
      const firstT = mdays.length ? Math.max(startT, mdays[0].time) : startT;
      const at = (t) => {
        const i = off.findIndex((x) => x.time >= t);
        if (i <= 0) return i === 0 ? off[0].cum_pnl : (off.length ? off[off.length - 1].cum_pnl : 0);
        const a = off[i - 1], b = off[i];
        return a.cum_pnl + (b.cum_pnl - a.cum_pnl) * (t - a.time) / (b.time - a.time);
      };
      acc = off.length && firstT > startT ? at(firstT) - base0 : 0;
      if (mdays.length) rec.push([firstT, acc]);
      mdays.forEach((d) => {
        const end = d.time + DAY;
        if (end <= startT) return;
        const share = d.time < startT ? (end - startT) / DAY : 1;
        acc += d.pnl * share;
        rec.push([end, acc]);
      });
      return baseOption(c, { legend: legend(c), xAxis: timeAxis(c), yAxis: valueAxis(c),
        tooltip: Object.assign(baseOption(c).tooltip, { valueFormatter: (v) => usd(v, 0) }),
        series: [
          { type: 'line', name: '官方', showSymbol: false, lineStyle: { width: 2, color: c.s1 }, itemStyle: { color: c.s1 },
            data: off.map((x) => [x.time, x.cum_pnl - base0]) },
          { type: 'line', name: '重建', showSymbol: false, lineStyle: { width: 2, color: c.s2 }, itemStyle: { color: c.s2 },
            data: rec },
        ] });
    });
    const pos = (cur.positions || []).slice(0, 20);
    table('t-top', pos, [
      col('币种', 'coin'), col('DEX', 'dex'), col('方向', 'side'),
      numCol('数量', 'szi'), numCol('名义价值', 'notional', (v) => usd(v)), numCol('开仓价', 'entry_px'), numCol('标记价', 'mark_px'),
      numCol('未实现盈亏', 'unrealized_pnl', (v) => usd(v)), numCol('杠杆', 'leverage'), numCol('强平价', 'liquidation_px'),
      numCol('距强平', 'liq_distance', (v) => pct(v)),
    ], { height: Math.min(480, 60 + pos.length * 34) });
  };

  // Scorecard values by unit; the definition rides along as a hover title.
  const UNIT_FMT = {
    usd: (v) => usd(v), pct: (v) => pct(v, Math.abs(num(v)) >= 10 ? 0 : 2), bps: (v) => (isNum(num(v)) ? fix(v, 2) + ' bps' : '—'),
    x: (v) => (isNum(num(v)) ? fix(v, 2) + '×' : '—'), int: int, days: (v) => (isNum(num(v)) ? fix(v, 1) + ' 天' : '—'),
    hours: (v) => hours(num(v)), num: (v) => fix(v, 2),
  };
  RENDER.scorecard = function (root) {
    const groups = (M.scorecard || {})[state.window] || [];
    const body = (g) => `<table class="table table-sm stat-table score-table mb-0"><tbody>${g.items.map((i) => {
      return `<tr title="${esc(i.definition)}"><td>${esc(i.label)}<div class="def">${esc(i.definition)}</div></td>
        <td class="num">${(UNIT_FMT[i.unit] || fix)(i.value)}</td><td class="srccell"><span class="src">${SOURCE[i.source] || esc(i.source)}</span></td></tr>`;
    }).join('')}</tbody></table>`;
    root.innerHTML = `<div class="row row-cards">
      <div class="col-12"><div class="small muted">窗口：${esc(W().label || '')}。来源：<b>官方</b> = portfolio 接口；<b>日序列</b> = 官方净值日序列；<b>成交</b> / <b>回合</b> = 可见成交（历史可能截断，见“数据质量”）；<b>K线</b> = MAE/MFE。“—”表示样本不足或不适用。</div></div>
      ${groups.map((g) => card(esc(g.group), body(g), { col: 'col-xl-6', flush: true })).join('')}
    </div>`;
  };

  function profileHtml() {
    const prof = M.profile || {}, tags = prof.tags || {}, rules = prof.rules || {};
    const names = { style: '风格', bias: '方向', leverage: '杠杆', concentration: '集中度', execution: '执行', activity: '活跃度', size: '体量' };
    const t = Object.keys(tags).map((k) => `<span class="tag"><b>${names[k] || k}</b>${esc(tags[k])}</span>`).join('');
    const r = Object.keys(rules).map((k) => `<li>${names[k] || k}：${esc(rules[k])}</li>`).join('');
    return `<div class="mb-2">${t || '<span class="muted">数据不足</span>'}</div><details><summary class="small muted">判定规则</summary><ul class="small muted mt-2 mb-0">${r}</ul></details>`;
  }

  function donut(c, items) {
    items = items.filter((x) => isNum(x.value) && x.value > 0).sort((a, b) => b.value - a.value);
    if (items.length > 6) {
      const rest = items.slice(5).reduce((s, x) => s + x.value, 0);
      items = items.slice(0, 5).concat([{ name: '其他', value: rest }]);
    }
    const palette = [c.s1, c.s2, c.s3, c.s4, c.s5, c.s6];
    return { animation: false, tooltip: { trigger: 'item', valueFormatter: (v) => usd(v, 0), backgroundColor: c.surface, borderColor: c.grid, textStyle: { color: c.ink1 } },
      legend: { bottom: 0, textStyle: { color: c.ink2 }, icon: 'roundRect', itemWidth: 10, itemHeight: 10 },
      series: [{ type: 'pie', radius: ['48%', '70%'], center: ['50%', '44%'], avoidLabelOverlap: true,
        itemStyle: { borderColor: c.surface, borderWidth: 2, borderRadius: 4 },
        label: { color: c.ink2, formatter: (p) => p.percent >= 8 ? p.percent.toFixed(0) + '%' : '' },
        data: items.map((x, i) => Object.assign({}, x, { itemStyle: { color: palette[i % palette.length] } })) }] };
  }

  RENDER.performance = function (root) {
    const p = W().performance || {}, all = M.performance || {};
    root.innerHTML = `<div class="row row-cards">
      ${card('盈亏分解（永续，重建区间）', chartDiv('c-wf'), { col: 'col-lg-6', sub: '各项 + 残差 = 官方' })}
      ${card('窗口收益指标', statTable([
        ['盈亏', usd(p.pnl), '官方 ' + usd(p.official_pnl)], ['TWR', pct(p.twr)], ['年化 CAGR', pct(p.cagr), '≥30 天才计算'],
        ['XIRR（资金加权）', pct(p.xirr)], ['投入资本回报', pct(p.roi_on_invested), '盈亏 / (期初 + 入金)'],
        ['盈利日占比', pct(p.positive_day_ratio)], ['日度 Profit factor', fix(p.profit_factor_daily)],
        ['最好的一天', p.best_day ? usd(p.best_day.pnl) + ' · ' + p.best_day.period : '—'],
        ['最差的一天', p.worst_day ? usd(p.worst_day.pnl) + ' · ' + p.worst_day.period : '—'],
        ['最好的月', p.best_month ? usd(p.best_month.pnl) + ' · ' + p.best_month.period : '—'],
        ['最差的月', p.worst_month ? usd(p.worst_month.pnl) + ' · ' + p.worst_month.period : '—'],
        ['收益率样本天数', int(p.return_days) + ' / ' + int(p.days)],
      ]), { col: 'col-lg-6', flush: true })}
      ${card('月度收益率', chartDiv('c-month', 'short'), { sub: '蓝 = 盈利，红 = 亏损；悬停看金额' })}
      ${card('每日盈亏', chartDiv('c-daily'), { col: 'col-lg-8' })}
      ${card('日收益率分布', chartDiv('c-rhist'), { col: 'col-lg-4' })}
      ${card('时间加权收益指数', chartDiv('c-twr'), { col: 'col-lg-6', sub: '起点 = 1' })}
      ${card('分币种永续盈亏（重建）', chartDiv('c-coin', 'tall'), { col: 'col-lg-6', sub: '盈利前 10 与亏损前 10' })}
      ${card('各窗口对比', windowTable(), { flush: true })}
      ${card('月度明细', '<div id="t-month" class="table-host"></div>', { flush: true })}
    </div>`;
    chart(document.getElementById('c-wf'), (c) => waterfall(c, M.pnl_breakdown));
    chart(document.getElementById('c-month'), (c) => monthHeat(c, all.monthly || []));
    chart(document.getElementById('c-daily'), (c) => baseOption(c, { xAxis: timeAxis(c), yAxis: valueAxis(c),
      tooltip: Object.assign(baseOption(c).tooltip, { valueFormatter: (v) => usd(v, 0) }),
      series: [{ type: 'bar', name: '日盈亏', barMaxWidth: 8, data: days().map((d) => ({ value: [d.time - DAY / 2, d.pnl],
        itemStyle: { color: d.pnl >= 0 ? c.pos : c.neg, borderRadius: d.pnl >= 0 ? [2, 2, 0, 0] : [0, 0, 2, 2] } })) }] }));
    chart(document.getElementById('c-rhist'), (c) => histogram(c, days().map((d) => d.ret).filter(isNum), (v) => pct(v, 1)));
    chart(document.getElementById('c-twr'), (c) => {
      let lvl = 1;
      const data = days().map((d) => { if (isNum(d.ret)) lvl *= 1 + d.ret; return [d.time, lvl]; });
      return baseOption(c, { xAxis: timeAxis(c), yAxis: Object.assign(valueAxis(c, (v) => v.toFixed(1) + '×'), { type: lvl > 20 ? 'log' : 'value' }),
        tooltip: Object.assign(baseOption(c).tooltip, { valueFormatter: (v) => fix(v, 3) + '×' }),
        series: [{ type: 'line', name: 'TWR 指数', showSymbol: false, lineStyle: { width: 2, color: c.s1 }, itemStyle: { color: c.s1 }, data }] });
    });
    chart(document.getElementById('c-coin'), (c) => {
      const entries = Object.entries(M.mtm.by_coin || {}).map(([k, v]) => [k, Number(v)]).sort((a, b) => a[1] - b[1]);
      const pick = entries.length > 20 ? entries.slice(0, 10).concat(entries.slice(-10)) : entries;
      return baseOption(c, { grid: { left: 110, right: 40, top: 10, bottom: 30 }, tooltip: { trigger: 'item', valueFormatter: (v) => usd(v, 0) },
        xAxis: valueAxis(c), yAxis: { type: 'category', data: pick.map((x) => x[0]), axisLabel: { color: c.ink2 }, axisLine: { lineStyle: { color: c.axis } }, axisTick: { show: false } },
        series: [{ type: 'bar', barMaxWidth: 12, data: pick.map((x) => ({ value: x[1], itemStyle: { color: x[1] >= 0 ? c.pos : c.neg, borderRadius: x[1] >= 0 ? [0, 2, 2, 0] : [2, 0, 0, 2] } })) }] });
    });
    table('t-month', (all.monthly || []).slice().reverse(), [col('月份', 'period'), numCol('盈亏', 'pnl', (v) => usd(v, 0)), numCol('收益率', 'ret', (v) => pct(v, 2))], { height: 320 });
  };

  function windowTable() {
    const ws = M.windows || {};
    const keys = Object.keys(ws);
    const rows = [['盈亏', (w) => usd(w.performance.pnl)], ['官方盈亏', (w) => usd(w.performance.official_pnl)], ['TWR', (w) => pct(w.performance.twr)],
      ['最大回撤', (w) => pct(w.risk.max_drawdown)], ['波动率（年化）', (w) => pct(w.risk.volatility)], ['Sharpe', (w) => fix(w.risk.sharpe)],
      ['Sortino', (w) => fix(w.risk.sortino)], ['VaR 95（日）', (w) => pct(w.risk.var95, 2)], ['官方成交额', (w) => usd(w.official_volume)]];
    return `<div class="table-responsive"><table class="table table-sm stat-table mb-0"><thead><tr><th></th>${keys.map((k) => `<th class="num">${esc(ws[k].label)}</th>`).join('')}</tr></thead><tbody>
      ${rows.map((r) => `<tr><td>${r[0]}</td>${keys.map((k) => `<td class="num">${r[1](ws[k])}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
  }

  function waterfall(c, bd) {
    if (!bd) return { title: { text: '无重建数据', left: 'center', top: 'middle', textStyle: { color: c.muted, fontSize: 13 } } };
    const names = bd.rows.map((r) => r.name).concat(['官方合计']);
    let run = 0;
    const base = [], val = [];
    bd.rows.forEach((r) => { const v = r.value; base.push(v >= 0 ? run : run + v); val.push({ value: Math.abs(v), raw: v, itemStyle: { color: v >= 0 ? c.pos : c.neg, borderRadius: 2 } }); run += v; });
    base.push(Math.min(0, run));
    val.push({ value: Math.abs(run), raw: run, itemStyle: { color: c.ink2, borderRadius: 2 } });
    return baseOption(c, { grid: { left: 64, right: 16, top: 20, bottom: 70 },
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, formatter: (ps) => { const p = ps.find((x) => x.seriesIndex === 1); return esc(p.name) + '<br>' + usd(p.data.raw, 0); } },
      xAxis: { type: 'category', data: names, axisLabel: { color: c.ink2, interval: 0, rotate: 20 }, axisLine: { lineStyle: { color: c.axis } }, axisTick: { show: false } },
      yAxis: valueAxis(c),
      series: [{ type: 'bar', stack: 'w', itemStyle: { color: 'transparent' }, emphasis: { disabled: true }, data: base },
        { type: 'bar', stack: 'w', barMaxWidth: 36, data: val, label: { show: true, position: 'top', color: c.ink2, formatter: (p) => usd(p.data.raw) } }] });
  }

  function monthHeat(c, months) {
    if (!months.length) return {};
    const years = [...new Set(months.map((m) => m.period.slice(0, 4)))].sort();
    const data = months.map((m) => [parseInt(m.period.slice(5, 7), 10) - 1, years.indexOf(m.period.slice(0, 4)), m.ret, m.pnl]);
    const lim = Math.max(0.05, ...months.map((m) => Math.abs(m.ret)).filter(isNum).sort((a, b) => a - b).slice(0, Math.ceil(months.length * 0.9)));
    return { animation: false, tooltip: { formatter: (p) => `${years[p.data[1]]}-${String(p.data[0] + 1).padStart(2, '0')}<br>收益率 ${pct(p.data[2], 1)}<br>盈亏 ${usd(p.data[3], 0)}`,
      backgroundColor: c.surface, borderColor: c.grid, textStyle: { color: c.ink1 } },
      grid: { left: 56, right: 90, top: 10, bottom: 30 },
      xAxis: { type: 'category', data: ['1月', '2月', '3月', '4月', '5月', '6月', '7月', '8月', '9月', '10月', '11月', '12月'], axisLabel: { color: c.muted }, axisLine: { show: false }, axisTick: { show: false }, splitArea: { show: false } },
      yAxis: { type: 'category', data: years, axisLabel: { color: c.muted }, axisLine: { show: false }, axisTick: { show: false } },
      visualMap: { min: -lim, max: lim, dimension: 2, calculable: false, orient: 'vertical', right: 0, top: 'middle', itemHeight: 120,
        inRange: { color: [c.neg, c.mid, c.pos] }, textStyle: { color: c.muted }, formatter: (v) => pct(v, 0) },
      series: [{ type: 'heatmap', data, itemStyle: { borderColor: c.surface, borderWidth: 2, borderRadius: 4 },
        label: { show: true, color: c.ink1, fontSize: 11, formatter: (p) => pct(p.data[2], 0) } }] };
  }

  function histogram(c, values, fmt) {
    if (!values.length) return {};
    const s = values.slice().sort((a, b) => a - b);
    const lo = s[Math.floor(s.length * 0.01)], hi = s[Math.min(s.length - 1, Math.floor(s.length * 0.99))];
    const n = 30, w = (hi - lo) / n || 1, counts = new Array(n).fill(0);
    values.forEach((v) => { counts[Math.min(n - 1, Math.max(0, Math.floor((v - lo) / w)))]++; });
    return bars(c, counts.map((k, i) => [fmt(lo + i * w) + ' ~ ' + fmt(lo + (i + 1) * w), k, lo + (i + 0.5) * w]));
  }
  function bars(c, rows, valueFmt) {
    return baseOption(c, { grid: { left: 48, right: 16, top: 16, bottom: 40 },
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, valueFormatter: valueFmt || ((v) => int(v)) },
      xAxis: { type: 'category', data: rows.map((r) => r[0]), axisLabel: { color: c.muted, hideOverlap: true, formatter: (v) => v.split(' ~ ')[0] }, axisLine: { lineStyle: { color: c.axis } }, axisTick: { show: false } },
      yAxis: valueAxis(c, valueFmt || ((v) => int(v))),
      series: [{ type: 'bar', barCategoryGap: '8%', data: rows.map((r) => ({ value: r[1], itemStyle: { color: r[2] === undefined ? c.s1 : (r[2] >= 0 ? c.pos : c.neg), borderRadius: [2, 2, 0, 0] } })) }] });
  }
  function line(c, series, yFmt, extra) {
    const pal = [c.s1, c.s2, c.s3];
    return baseOption(c, Object.assign({ legend: series.length > 1 ? legend(c) : undefined, xAxis: timeAxis(c), yAxis: valueAxis(c, yFmt),
      tooltip: Object.assign(baseOption(c).tooltip, { valueFormatter: yFmt }),
      series: series.map((s, i) => ({ type: 'line', name: s.name, showSymbol: false, connectNulls: false, lineStyle: { width: 2, color: pal[i] }, itemStyle: { color: pal[i] },
        areaStyle: s.area ? { color: pal[i], opacity: 0.15 } : undefined, data: s.data })),
    }, extra || {}));
  }

  RENDER.risk = function (root) {
    const r = W().risk || {}, all = M.risk || {}, cur = M.current || {};
    root.innerHTML = `<div class="row row-cards">
      ${kpi('最大回撤', pct(r.max_drawdown), '当前回撤 ' + pct(r.current_drawdown), 'hybrid')}
      ${kpi('年化波动率', pct(r.volatility), 'VaR95 ' + pct(r.var95, 2), 'hybrid')}
      ${kpi('Sharpe / Sortino', fix(r.sharpe) + ' / ' + fix(r.sortino), 'Calmar ' + fix(r.calmar), 'hybrid')}
      ${kpi('Omega / 尾部比率', fix(r.omega) + ' / ' + fix(r.tail_ratio), '最长水下 ' + fix((r.drawdown_stats || {}).longest_underwater_days, 0) + ' 天', 'hybrid')}
      ${kpi('有效杠杆均值', fix(all.leverage_mean) + '×', 'p95 ' + fix(all.leverage_p95) + '× · 最高 ' + fix(all.leverage_max) + '×', 'rebuilt')}
      ${kpi('持仓集中度 HHI', fix(cur.concentration_hhi), '1 = 单一持仓', 'official')}
      ${card('水下回撤（TWR）', chartDiv('c-uw'), { col: 'col-lg-8' })}
      ${card('风险统计', statTable([
        ['CVaR 95（日）', pct(r.cvar95, 2)], ['恢复因子', fix(r.recovery_factor), '净盈亏 ÷ 最大回撤金额'],
        ['回撤次数（≥5%）', int((r.drawdown_stats || {}).count), '平均深度 ' + pct((r.drawdown_stats || {}).avg_depth, 1)],
        ['水下 / 新高时间占比', pct((r.drawdown_stats || {}).underwater_ratio, 0) + ' / ' + pct((r.drawdown_stats || {}).new_high_ratio, 0)],
        ['$ 最大回撤（累计盈亏）', usd(r.max_drawdown_usd)], ['被强平回合', int((M.behavior || {}).liquidated_campaigns)],
        ['强平成交额', usd((M.activity || {}).liquidation_volume)], ['ADL 成交', int((M.activity || {}).adl_fills)],
        ['多空净敞口', usd(cur.net_exposure)], ['最近强平距离', cur.nearest_liquidation ? esc(cur.nearest_liquidation.coin) + ' ' + pct(cur.nearest_liquidation.liq_distance) : '—'],
      ]), { col: 'col-lg-4', flush: true })}
      ${card('有效杠杆（永续名义 / 净值）', chartDiv('c-lev', 'short'), { col: 'col-lg-6', sub: '重建' })}
      ${card('多空敞口', chartDiv('c-ls', 'short'), { col: 'col-lg-6', sub: '重建，按日收盘' })}
      ${card('最深回撤区间', '<div id="t-dd" class="table-host"></div>', { flush: true })}
      <div class="col-12"><details class="more" id="more-risk"><summary>更多统计：Ulcer、偏度、峰度、滚动 30 日波动率与 Sharpe</summary>
        <div class="row row-cards mt-1">
          ${card('分布形态', statTable([['Ulcer 指数', fix(r.ulcer_index), '回撤深度的均方根'], ['偏度', fix(r.skew)], ['超额峰度', fix(r.excess_kurtosis)],
            ['最大回撤时长（峰→谷）', fix((r.drawdown_stats || {}).max_drawdown_duration_days, 0) + ' 天'], ['最大恢复时长（谷→新高）', fix((r.drawdown_stats || {}).max_recovery_days, 0) + ' 天']]), { col: 'col-lg-4', flush: true })}
          ${card('滚动 30 日波动率（年化）', chartDiv('c-vol', 'short'), { col: 'col-lg-4' })}
          ${card('滚动 30 日 Sharpe', chartDiv('c-sh', 'short'), { col: 'col-lg-4' })}
        </div></details></div>
    </div>`;
    const inw = (rows, key) => (rows || []).filter((x) => inWin(x.time)).map((x) => [x.time, x[key]]);
    chart(document.getElementById('c-uw'), (c) => line(c, [{ name: '回撤', data: inw(all.underwater, 'dd'), area: true }], (v) => pct(v, 1)));
    // Folded charts have no size until opened; build them on first open.
    document.getElementById('more-risk').addEventListener('toggle', function once(e) {
      if (!e.target.open) return;
      e.target.removeEventListener('toggle', once);
      chart(document.getElementById('c-vol'), (c) => line(c, [{ name: '波动率', data: inw(all.rolling_30d, 'vol') }], (v) => pct(v, 0)));
      chart(document.getElementById('c-sh'), (c) => line(c, [{ name: 'Sharpe', data: inw(all.rolling_30d, 'sharpe') }], (v) => fix(v, 1)));
    });
    chart(document.getElementById('c-lev'), (c) => line(c, [{ name: '杠杆', data: inw(all.leverage_series, 'leverage') }], (v) => fix(v, 1) + '×'));
    chart(document.getElementById('c-ls'), (c) => line(c, [{ name: '多头', data: inw(all.leverage_series, 'long') }, { name: '空头', data: inw(all.leverage_series, 'short') }], (v) => usd(v)));
    table('t-dd', (all.drawdown_episodes || []), [timeCol('峰值', 'peak_ms'), timeCol('谷底', 'trough_ms'), timeCol('恢复', 'recovered_ms'),
      numCol('深度', 'depth', (v) => pct(v, 1)), numCol('持续天数', 'duration_days', (v) => fix(v, 0)), numCol('恢复天数', 'recovery_days', (v) => fix(v, 0))], { height: 360 });
  };

  RENDER.trading = function (root) {
    const b = M.behavior || {}, a = M.activity || {};
    root.innerHTML = `<div class="row row-cards">
      ${kpi('完整回合', int(b.campaigns_evaluated), '排除不完整 ' + int(b.campaigns_excluded), 'rebuilt')}
      ${kpi('胜率（含保本 / 严格）', pct(b.win_rate) + ' / ' + pct(b.win_rate_strict), '连胜 ' + int(b.max_win_streak) + ' · 连亏 ' + int(b.max_loss_streak), 'rebuilt')}
      ${kpi('平均盈利 / 亏损', usd(b.avg_win) + ' / ' + usd(b.avg_loss), '每回合期望 ' + usd(b.expectancy), 'rebuilt')}
      ${kpi('持仓中位时长', hours(b.median_hold_hours), '', 'rebuilt')}
      ${kpi('Maker 占比', pct(a.maker_share), 'TWAP ' + pct(a.twap_share), 'rebuilt')}
      ${kpi('活跃天数', int(a.active_days), '日均 ' + fix(a.fills_per_active_day, 0) + ' 笔成交', 'rebuilt')}
      ${card('持仓时长 × 回合盈亏', chartDiv('c-scatter', 'tall'), { col: 'col-lg-8', sub: '横轴对数刻度；含资金费' })}
      ${card('交易行为', statTable([
        ['成交笔数', int(a.fills)], ['开仓 / 加仓', int(a.opens) + ' / ' + int(a.adds)], ['减仓 / 清仓', int(a.reduces) + ' / ' + int(a.closes)],
        ['反手', int(a.reversals)], ['最大单回合盈利', usd(b.max_win)], ['最大单回合亏损', usd(b.max_loss)],
        ['做多回合', int(((b.by_direction || {}).long || {}).count) + ' · ' + usd(((b.by_direction || {}).long || {}).pnl)],
        ['做空回合', int(((b.by_direction || {}).short || {}).count) + ' · ' + usd(((b.by_direction || {}).short || {}).pnl)],
        ['HIP-3 成交额占比', pct(a.hip3_share)], ['现货成交额占比', pct(a.spot_share)], ['交易币种数', int(a.coin_count)],
        ['订单样本撤单率', a.orders_sample ? pct(a.orders_sample.cancel_rate) : '—', a.orders_sample ? '近 ' + int(a.orders_sample.count) + ' 单' : ''],
      ]), { col: 'col-lg-4', flush: true })}
      ${card('交易时段热力图（UTC）', chartDiv('c-heat'), { col: 'col-lg-8', sub: '颜色 = 成交额' })}
      ${card('持仓时长分布', chartDiv('c-hold'), { col: 'col-lg-4' })}
      ${card('回合盈亏分布', chartDiv('c-phist'), { col: 'col-lg-6' })}
      ${card('成交额前 15 币种', '<div id="t-coins" class="table-host"></div>', { col: 'col-lg-6', flush: true })}
      ${card('多空对比', chartDiv('c-dir', 'short'), { col: 'col-lg-6', sub: '完整回合，含资金费' })}
      ${card('盈利集中度', chartDiv('c-pareto', 'short'), { col: 'col-lg-6',
        sub: '回合按盈亏从高到低累计；前 10% 回合贡献 ' + pct((b.concentration || {}).top_share, 0) + ' 的总盈利' })}
      ${card('分币种盈亏', '<div id="t-bycoin" class="table-host"></div>', { flush: true, sub: '可见成交：已实现 − 手续费 + 资金费；回合列只计完整回合' })}
      ${M.excursion && M.excursion.evaluated ? card('MAE × 回合盈亏', chartDiv('c-mae', 'tall'), { col: 'col-lg-8',
        sub: excursionNote(M.excursion) }) + card('MFE 捕获率分布', chartDiv('c-cap'), { col: 'col-lg-4', sub: '已实现价格盈亏 ÷ 最大浮盈' }) : ''}
      ${card('仓位回合', '<div id="t-camp" class="table-host"></div>', { flush: true, sub: M.tables.campaigns.count + ' 条' })}
    </div>`;
    chart(document.getElementById('c-scatter'), (c) => {
      const pts = (b.scatter || []).filter((s) => s.hold_h > 0 && inWin(s.close_time));
      const mk = (dir, color, name) => ({ type: 'scatter', name, symbolSize: 8, itemStyle: { color, opacity: 0.75, borderColor: c.surface, borderWidth: 1 },
        data: pts.filter((s) => s.direction === dir).map((s) => [s.hold_h, s.pnl, s.coin, s.close_time]) });
      return baseOption(c, { legend: legend(c), tooltip: { trigger: 'item', formatter: (p) => `${esc(p.data[2])} · ${esc(p.seriesName)}<br>持仓 ${hours(p.data[0])}<br>盈亏 ${usd(p.data[1], 0)}<br>${date(p.data[3])}` },
        xAxis: { type: 'log', name: '持仓小时', nameLocation: 'middle', nameGap: 26, nameTextStyle: { color: c.muted }, axisLabel: { color: c.muted }, splitLine: { lineStyle: { color: c.grid } }, axisLine: { lineStyle: { color: c.axis } } },
        yAxis: valueAxis(c), series: [mk('long', c.s1, '做多'), mk('short', c.s2, '做空')] });
    });
    chart(document.getElementById('c-heat'), (c) => {
      const cells = a.heatmap || [];
      const max = Math.max(1, ...cells.map((x) => x.volume));
      return { animation: false, tooltip: { formatter: (p) => `${['周一', '周二', '周三', '周四', '周五', '周六', '周日'][p.data[1]]} ${p.data[0]}:00<br>成交额 ${usd(p.data[2], 0)}<br>${int(p.data[3])} 笔`,
        backgroundColor: c.surface, borderColor: c.grid, textStyle: { color: c.ink1 } },
        grid: { left: 48, right: 16, top: 10, bottom: 60 },
        xAxis: { type: 'category', data: [...Array(24).keys()].map(String), axisLabel: { color: c.muted }, axisLine: { show: false }, axisTick: { show: false } },
        yAxis: { type: 'category', data: ['周一', '周二', '周三', '周四', '周五', '周六', '周日'], inverse: true, axisLabel: { color: c.muted }, axisLine: { show: false }, axisTick: { show: false } },
        visualMap: { min: 0, max, dimension: 2, orient: 'horizontal', left: 'center', bottom: 0, itemWidth: 10, itemHeight: 160, inRange: { color: [c.seq0, c.seq1] }, textStyle: { color: c.muted }, formatter: (v) => usd(v) },
        series: [{ type: 'heatmap', data: cells.map((x) => [x.hour, x.weekday, x.volume, x.fills]), itemStyle: { borderColor: c.surface, borderWidth: 2, borderRadius: 3 } }] };
    });
    chart(document.getElementById('c-hold'), (c) => bars(c, (b.hold_distribution || []).map((h) => [h.bucket, h.count])));
    chart(document.getElementById('c-dir'), (c) => {
      const d = b.by_direction || {};
      const rows = [['做多', d.long || {}], ['做空', d.short || {}]];
      return baseOption(c, { grid: { left: 64, right: 24, top: 36, bottom: 30 }, legend: legend(c),
        tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, formatter: (ps) => {
          const x = rows[ps[0].dataIndex][1];
          return `${rows[ps[0].dataIndex][0]}<br>总盈利 ${usd(x.gross_win, 0)} · 总亏损 ${usd(-(x.gross_loss || 0), 0)}<br>净 ${usd(x.pnl, 0)} · 胜率 ${pct(x.win_rate, 0)} · ${int(x.count)} 回合`;
        } },
        xAxis: { type: 'category', data: rows.map((r) => r[0]), axisLabel: { color: c.ink2 }, axisLine: { lineStyle: { color: c.axis } }, axisTick: { show: false } },
        yAxis: valueAxis(c),
        series: [
          { type: 'bar', name: '总盈利', stack: 'd', barMaxWidth: 36, itemStyle: { color: c.pos }, data: rows.map((r) => ({ value: r[1].gross_win || 0, itemStyle: { borderRadius: [4, 4, 0, 0] } })) },
          { type: 'bar', name: '总亏损', stack: 'd', barMaxWidth: 36, itemStyle: { color: c.neg }, data: rows.map((r) => ({ value: -(r[1].gross_loss || 0), itemStyle: { borderRadius: [0, 0, 4, 4] } })) },
          { type: 'scatter', name: '净盈亏', symbol: 'rect', symbolSize: [40, 3], itemStyle: { color: c.ink1 }, data: rows.map((r) => r[1].pnl || 0), z: 5 },
        ] });
    });
    chart(document.getElementById('c-pareto'), (c) => {
      const pts = ((b.concentration || {}).pareto || []).map((p) => [p.share_of_campaigns, p.share_of_profit]);
      return baseOption(c, { grid: { left: 56, right: 24, top: 20, bottom: 40 },
        tooltip: { trigger: 'axis', formatter: (ps) => `前 ${pct(ps[0].data[0], 0)} 回合<br>累计占总盈利 ${pct(ps[0].data[1], 0)}` },
        xAxis: { type: 'value', min: 0, max: 1, axisLabel: { color: c.muted, formatter: (v) => pct(v, 0) }, splitLine: { show: false }, axisLine: { lineStyle: { color: c.axis } } },
        yAxis: valueAxis(c, (v) => pct(v, 0)),
        series: [{ type: 'line', name: '累计盈利占比', showSymbol: false, lineStyle: { width: 2, color: c.s1 }, itemStyle: { color: c.s1 }, data: pts,
          markLine: { silent: true, symbol: 'none', lineStyle: { color: c.muted, type: 'dashed', width: 1 }, label: { color: c.muted, formatter: '前 10%' }, data: [{ xAxis: 0.1 }] } }] });
    });
    table('t-bycoin', ((M.windows || {}).all || {}).by_coin || [], [col('币种', 'coin'), col('类型', 'market_type', { width: 70 }),
      numCol('净额', 'net', (v) => usd(v)), numCol('已实现', 'realized', (v) => usd(v)), numCol('手续费', 'fees', (v) => usd(v)),
      numCol('资金费', 'funding', (v) => usd(v)), numCol('成交额', 'volume', (v) => usd(v)), numCol('成交笔数', 'fills', int),
      numCol('完整回合', 'campaigns', int), numCol('胜率', 'win_rate', (v) => pct(v, 0)), numCol('做多盈亏', 'long_pnl', (v) => usd(v)), numCol('做空盈亏', 'short_pnl', (v) => usd(v))],
    { height: 420, tabulator: { initialSort: [{ column: 'net', dir: 'desc' }] } });
    if (M.excursion && M.excursion.evaluated) {
      const ex = M.excursion.rows;
      chart(document.getElementById('c-mae'), (c) => {
        const mk = (dir, color, name) => ({ type: 'scatter', name, symbolSize: (v) => (v[4] ? 5 : 8),
          itemStyle: { color, opacity: 0.75, borderColor: c.surface, borderWidth: 1 },
          data: ex.filter((r) => r.direction === dir).map((r) => [r.mae_pct, r.pnl, r.coin, r.resolution, r.coarse, r.mfe_pct, r.close_time]) });
        return baseOption(c, { legend: legend(c),
          tooltip: { trigger: 'item', formatter: (p) => `${esc(p.data[2])} · ${esc(p.seriesName)} · ${esc(p.data[3])}${p.data[4] ? '（精度不足）' : ''}<br>MAE ${pct(p.data[0], 2)} · MFE ${pct(p.data[5], 2)}<br>盈亏 ${usd(p.data[1], 0)}<br>${date(p.data[6])}` },
          xAxis: { type: 'value', name: '最大浮亏（占开仓名义）', nameLocation: 'middle', nameGap: 26, nameTextStyle: { color: c.muted },
            axisLabel: { color: c.muted, formatter: (v) => pct(v, 0) }, splitLine: { lineStyle: { color: c.grid } }, axisLine: { lineStyle: { color: c.axis } } },
          yAxis: valueAxis(c), series: [mk('long', c.s1, '做多'), mk('short', c.s2, '做空')] });
      });
      chart(document.getElementById('c-cap'), (c) => histogram(c, ex.filter((r) => !r.coarse && isNum(r.capture)).map((r) => Math.max(-2, Math.min(1.5, r.capture))), (v) => pct(v, 0)));
    }
    chart(document.getElementById('c-phist'), (c) => bars(c, (b.pnl_histogram || []).map((h) => [usd(h.from) + ' ~ ' + usd(h.to), h.count, (h.from + h.to) / 2])));
    table('t-coins', (a.top_coins || []), [col('币种', 'coin'), numCol('成交额', 'volume', (v) => usd(v)), numCol('成交笔数', 'fills', int),
      numCol('已实现盈亏', 'realized', (v) => usd(v)), numCol('手续费', 'fees', (v) => usd(v))], { height: 420 });
    table('t-camp', 'campaigns', [timeCol('开仓', 'open_time'), timeCol('平仓', 'close_time'), col('币种', 'coin'), col('方向', 'direction_label'), col('状态', 'status'),
      { title: '完整', field: 'complete', formatter: yesFmt, hozAlign: 'center', headerFilter: 'tickCross', headerFilterParams: { tristate: true } },
      numCol('最大仓位', 'max_size'), numCol('开仓名义', 'observed_entry_notional', (v) => usd(v)), numCol('净盈亏', 'net_pnl', (v) => usd(v)),
      numCol('资金费', 'funding', (v) => usd(v)), numCol('含资金费净盈亏', 'net_pnl_with_funding', (v) => usd(v)),
      numCol('持仓时长', 'duration_ms', (v) => (v === null ? '' : hours(v / 3600000))), numCol('成交数', 'fill_count', int), col('结束方式', 'ended_by'), col('质量标记', 'quality')]);
  };

  function excursionNote(ex) {
    const rc = ex.resolution_counts || {};
    return '精度：' + ['15m', '1h', '4h', '1d'].map((k) => k + ' ' + int(rc[k])).join(' · ') + '；小点 = 精度不足（持仓短于所用 K 线）';
  }

  RENDER.positions = function (root) {
    const cur = M.current || {}, st = cur.staking || {};
    root.innerHTML = `<div class="row row-cards">
      ${kpi('总敞口', usd(cur.gross_exposure), '多 ' + usd(cur.long_exposure) + ' · 空 ' + usd(cur.short_exposure), 'official')}
      ${kpi('质押 HYPE', fix(num(st.delegated), 2), usd(st.value_usd), 'official')}
      ${kpi('金库', int((cur.vaults || []).length) + ' 个', usd((cur.vaults || []).reduce((s, v) => s + (num(v.equity) || 0), 0)), 'official')}
      ${kpi('子账户', int((cur.sub_accounts || []).length) + ' 个', usd((cur.sub_accounts || []).reduce((s, v) => s + (num(v.account_value) || 0), 0)), 'official')}
      ${kpi('挂单', int(cur.open_orders), '', 'official')}
      ${kpi('快照时间', date(cur.time, true), 'UTC', 'official')}
      ${card('当前持仓', '<div id="t-pos" class="table-host"></div>', { flush: true })}
      ${card('各账户桶价值', chartDiv('c-bk', 'short'), { col: 'col-lg-6' })}
      ${card('现货余额', '<div id="t-spot" class="table-host"></div>', { col: 'col-lg-6', flush: true })}
      ${card('子账户', '<div id="t-sub" class="table-host"></div>', { col: 'col-lg-6', flush: true })}
      ${card('金库', '<div id="t-vault" class="table-host"></div>', { col: 'col-lg-6', flush: true })}
    </div>`;
    table('t-pos', cur.positions || [], [col('币种', 'coin'), col('DEX', 'dex'), col('方向', 'side'), numCol('数量', 'szi'), numCol('名义价值', 'notional', (v) => usd(v)),
      numCol('开仓价', 'entry_px'), numCol('标记价', 'mark_px'), numCol('未实现盈亏', 'unrealized_pnl', (v) => usd(v)), numCol('ROE', 'roe', (v) => pct(v)),
      col('杠杆类型', 'leverage_type'), numCol('杠杆', 'leverage'), numCol('强平价', 'liquidation_px'), numCol('距强平', 'liq_distance', (v) => pct(v)),
      numCol('占用保证金', 'margin_used', (v) => usd(v)), numCol('开仓以来资金费', 'cum_funding_since_open', (v) => usd(v))], { height: 360 });
    chart(document.getElementById('c-bk'), (c) => {
      const rows = (cur.buckets || []).slice().sort((x, y) => y.value - x.value);
      return baseOption(c, { grid: { left: 110, right: 40, top: 10, bottom: 30 }, tooltip: { trigger: 'item', valueFormatter: (v) => usd(v, 0) }, xAxis: valueAxis(c),
        yAxis: { type: 'category', inverse: true, data: rows.map((r) => r.bucket), axisLabel: { color: c.ink2 }, axisLine: { lineStyle: { color: c.axis } }, axisTick: { show: false } },
        series: [{ type: 'bar', barMaxWidth: 14, data: rows.map((r) => ({ value: r.value, itemStyle: { color: c.s1, borderRadius: [0, 2, 2, 0] } })) }] });
    });
    table('t-spot', cur.spot || [], [col('币种', 'coin'), numCol('数量', 'total'), numCol('冻结', 'hold'), numCol('价格', 'price'), numCol('价值', 'value', (v) => usd(v)),
      numCol('成本', 'entry_ntl', (v) => usd(v)), numCol('浮盈', 'unrealized_pnl', (v) => usd(v))], { height: 300 });
    // Sub-accounts analyzed with --include-subaccounts carry their own official figures.
    const subInfo = {};
    (M.sub_account_summary || []).forEach((s) => { subInfo[s.address] = s; });
    const subRows = (cur.sub_accounts || []).map((s) => Object.assign({}, s, subInfo[s.address] || {}));
    const subCols = [col('名称', 'name'), col('地址', 'address'), numCol('永续净值', 'account_value', (v) => usd(v)), numCol('持仓数', 'positions', int)];
    if ((M.sub_account_summary || []).length) {
      subCols.push(numCol('官方累计盈亏', 'official_pnl', (v) => usd(v)), numCol('最大回撤', 'max_drawdown', (v) => pct(v, 1)),
        col('对账', 'reconciliation'), col('历史', 'history'));
    }
    table('t-sub', subRows, subCols, { height: 240 });
    table('t-vault', cur.vaults || [], [col('金库', 'vault'), numCol('权益', 'equity', (v) => usd(v)), timeCol('锁定至', 'locked_until_ms')], { height: 240 });
  };

  RENDER.costs = function (root) {
    const k = M.costs || {};
    root.innerHTML = `<div class="row row-cards">
      ${kpi('手续费合计', usd(k.fees), pct(k.fee_bps / 1e4, 3) + ' 成交额（' + fix(k.fee_bps, 2) + ' bps）', 'rebuilt')}
      ${kpi('资金费净额', usd(k.funding_net), '收 ' + usd(k.funding_received) + ' · 付 ' + usd(k.funding_paid), 'rebuilt')}
      ${kpi('Builder 费', usd(k.builder_fees), '已含于手续费，单列', 'rebuilt')}
      ${kpi('成本 / 毛利', pct(k.cost_to_gross), '手续费 + 净付资金费', 'rebuilt')}
      ${kpi('费率档', k.taker_rate !== null ? pct(k.taker_rate, 3) + ' / ' + pct(k.maker_rate, 3) : '—', 'taker / maker', 'official')}
      ${kpi('折扣', '推荐 ' + pct(k.referral_discount, 0) + ' · 质押 ' + pct(k.staking_discount, 0), '', 'official')}
      ${card('累计手续费与资金费', chartDiv('c-cost'), { col: 'col-lg-7' })}
      ${card('分币种资金费', chartDiv('c-fund', 'tall'), { col: 'col-lg-5', sub: '付出前 10 与收入前 10' })}
      ${card('资金费明细', '<div id="t-funding" class="table-host"></div>', { flush: true, sub: M.tables.funding.count + ' 条' })}
    </div>`;
    chart(document.getElementById('c-cost'), (c) => {
      const rows = (k.cumulative || []).filter((x) => inWin(x.time));
      return line(c, [{ name: '累计手续费', data: rows.map((x) => [x.time, -x.fees]) }, { name: '累计资金费', data: rows.map((x) => [x.time, x.funding]) }], (v) => usd(v));
    });
    chart(document.getElementById('c-fund'), (c) => {
      const e = (k.funding_by_coin || []);
      const pick = e.length > 20 ? e.slice(0, 10).concat(e.slice(-10)) : e;
      return baseOption(c, { grid: { left: 100, right: 30, top: 10, bottom: 30 }, tooltip: { trigger: 'item', valueFormatter: (v) => usd(v, 0) }, xAxis: valueAxis(c),
        yAxis: { type: 'category', data: pick.map((x) => x.coin), axisLabel: { color: c.ink2 }, axisLine: { lineStyle: { color: c.axis } }, axisTick: { show: false } },
        series: [{ type: 'bar', barMaxWidth: 12, data: pick.map((x) => ({ value: x.funding, itemStyle: { color: x.funding >= 0 ? c.pos : c.neg, borderRadius: 2 } })) }] });
    });
    table('t-funding', 'funding', [timeCol('时间', 'time'), col('币种', 'coin'), numCol('金额', 'usdc'), numCol('持仓', 'szi'), numCol('费率', 'fundingRate'), col('计价', 'pnl_token')]);
  };

  RENDER.flows = function (root) {
    const f = M.flows || {};
    const byClass = Object.entries(f.by_class || {});
    root.innerHTML = `<div class="row row-cards">
      ${kpi('流水净资本流入', usd(f.net_capital), '外部 + 子账户 + 金库 + 质押', 'official')}
      ${byClass.slice(0, 5).map(([k, v]) => kpi(esc(k), usd(v), '', 'official')).join('')}
      ${card('累计资金流：隐含 vs 流水', chartDiv('c-flow'), { sub: '隐含 = 官方净值变化 − 官方盈亏；差异来自流水缺失、现货价值变动或子账户' })}
      ${card('资金流水（已分类）', '<div id="t-flows" class="table-host"></div>', { flush: true, sub: M.tables.flows.count + ' 条' })}
    </div>`;
    load('flows').then((rows) => {
      chart(document.getElementById('c-flow'), (c) => {
        let a = 0, b = 0;
        const implied = days().map((d) => { a += d.flow; return [d.time, a]; });
        const cap = { '外部出入金': 1, '子账户划转': 1, '金库': 1, '质押': 1 };
        const st = windowStart();
        const ledger = rows.filter((r) => cap[r.class_label] && r.amount_usd !== null && r.time > st).sort((x, y) => x.time - y.time).map((r) => { b += Number(r.amount_usd); return [r.time, b]; });
        return line(c, [{ name: '隐含', data: implied }, { name: '流水', data: ledger }], (v) => usd(v));
      });
    });
    table('t-flows', 'flows', [timeCol('时间', 'time'), col('分类', 'class_label'), col('类型', 'type'), col('币种', 'token'), numCol('数量', 'amount'),
      numCol('美元金额', 'amount_usd', (v) => usd(v, 2)), numCol('费用', 'fee_usd'), { title: '估值', field: 'approx_value', formatter: yesFmt, hozAlign: 'center' }, col('对手方', 'counterparty'), col('金库', 'vault'), col('哈希', 'hash')]);
  };

  RENDER.quality = function (root) {
    const q = M.quality || {}, t = q.truncation || {}, rec = q.reconciliation || {}, comp = q.composition || {};
    const detail = (d) => {
      if (typeof d === 'string') return d;
      if (Array.isArray(d)) return d.join(', ');
      const names = { visible: '可见', official: '官方', coverage: '覆盖率', count: '处数', coins: '币种', official_start_ms: '官方起点', first_fill_ms: '首笔成交' };
      return Object.entries(d || {}).map(([k, v]) => (names[k] || k) + ' ' + (k === 'coverage' ? pct(v, 1) : k.endsWith('_ms') ? date(v) : Array.isArray(v) ? v.join(', ') : isNum(v) && v > 1000 ? usd(v) : v)).join(' · ');
    };
    const ev = (t.evidence || []).map((e) => `<li><b>${esc(e.label)}</b><br><span class="muted small">${esc(detail(e.detail))}</span></li>`).join('');
    const truncText = { complete: '可见历史完整', api_visible_gaps: '历史中段有缺口', api_visible_truncated: '早期历史被截断' }[t.status] || t.status;
    root.innerHTML = `<div class="row row-cards">
      ${kpi('对账状态', statusBadge(rec.status), (rec.reasons || []).join(', ') || '&nbsp;', 'hybrid')}
      ${kpi('对账残差', usd(rec.residual), pct(rec.residual_pct, 2) + ' 官方', 'hybrid')}
      ${kpi('周度区间通过', int((rec.interval_status || {}).ok) + ' / ' + int(((rec.interval_status || {}).ok || 0) + ((rec.interval_status || {}).warn || 0) + ((rec.interval_status || {}).fail || 0)), '偏差 ' + int((rec.interval_status || {}).warn) + ' · 失败 ' + int((rec.interval_status || {}).fail), 'hybrid')}
      ${kpi('成交额覆盖率', pct(t.volume_coverage, 1), '含补录缺口 ' + pct(q.volume_coverage_with_imputed, 1), 'hybrid')}
      ${kpi('仓位断点', int(q.position_break_count), '快照不一致 ' + int((q.snapshot_mismatch || []).length), 'rebuilt')}
      ${kpi('资产组合残差', pct(num(comp.residual_pct) / 100, 2), '快照合计 vs 官方净值，差 ' + usd(comp.residual), 'official')}
      ${card('历史完整性：' + esc(truncText), `<ul class="evidence mb-2">${ev || '<li>未发现截断证据</li>'}</ul>
        <div class="small muted">可见成交起点 ${date(t.available_from_ms)} · 官方净值起点 ${date(t.official_start_ms)} · 可见成交额 ${usd(t.visible_volume)} / 官方 ${usd(t.official_volume)}</div>`, { col: 'col-lg-6' })}
      ${card('数据来源与标记', statTable([
        ['日序列：官方 / 混合 / 插值', Object.values(q.series_sources || {}).map(int).join(' / ')], ['跳过的收益日（分母过小）', int(q.skipped_return_days)],
        ['盯市：缺 K 线回退', int(((q.mtm_flags || {}).mark_fallback || []).length)], ['盯市：无标记价', esc(((q.mtm_flags || {}).no_mark || []).join(', ') || '无')],
        ['盯市：补录缺口币种', esc(((q.mtm_flags || {}).gap_imputed || []).join(', ') || '无')], ['元数据错误', int((q.meta_errors || []).length)],
        ['快照接口错误', esc(Object.keys(q.snapshot_errors || {}).join(', ') || '无')], ['有期初持仓的币种', esc((q.seeded_coins || []).join(', ') || '无')],
        ['付费补数', esc((q.backfill || {}).note || '未启用付费补数')], ['由持仓快照校正的缺口', int(q.snapshot_corrected_gaps)],
      ]), { col: 'col-lg-6', flush: true })}
      ${card('周度对账残差（官方 − 重建）', chartDiv('c-res'), { sub: '蓝 = 官方更高，红 = 重建更高' })}
      ${card('对账区间', '<div id="t-rec" class="table-host"></div>', { col: 'col-lg-6', flush: true })}
      ${card('仓位断点', '<div id="t-brk" class="table-host"></div>', { col: 'col-lg-6', flush: true, sub: '成交前仓位与上一笔成交后仓位不一致处' })}
    </div>`;
    chart(document.getElementById('c-res'), (c) => baseOption(c, { xAxis: timeAxis(c), yAxis: valueAxis(c),
      tooltip: Object.assign(baseOption(c).tooltip, { valueFormatter: (v) => usd(v, 0) }),
      series: [{ type: 'bar', name: '残差', barMaxWidth: 10, data: (q.reconciliation_intervals || []).map((i) => ({ value: [i.end_ms, i.residual], itemStyle: { color: i.residual >= 0 ? c.pos : c.neg, borderRadius: 2 } })) }] }));
    table('t-rec', (q.reconciliation_intervals || []).slice().reverse(), [timeCol('起', 'start_ms'), timeCol('止', 'end_ms'), numCol('官方', 'official', (v) => usd(v, 0)),
      numCol('重建', 'reconstructed', (v) => usd(v, 0)), numCol('残差', 'residual', (v) => usd(v, 0)), col('状态', 'status')], { height: 380 });
    table('t-brk', q.position_breaks || [], [timeCol('时间', 'time'), col('币种', 'coin'), numCol('应为', 'expected'), numCol('实际', 'observed'), numCol('差额', 'delta'), numCol('下一笔成交价', 'px'), col('类型', 'kind')], { height: 380 });
  };

  RENDER.raw = function (root) {
    root.innerHTML = `<div class="row row-cards">
      ${card('全部成交', '<div id="t-ops" class="table-host"></div>', { flush: true, sub: M.tables.operations.count + ' 条 · 表头可筛选，CSV 在 exports/',
        actions: `<div class="card-actions"><a class="btn btn-sm" href="${M.tables.operations.csv}" download>下载 CSV</a></div>` })}
    </div>`;
    table('t-ops', 'operations', [timeCol('时间', 'time'), col('币种', 'coin'), col('动作', 'action_label'), col('方向', 'direction_label'), col('买卖', 'side', { width: 60 }),
      numCol('价格', 'price'), numCol('数量', 'size'), numCol('名义', 'notional', (v) => usd(v)), numCol('成交前仓位', 'start_position'), numCol('成交后仓位', 'end_position'),
      numCol('已实现盈亏', 'closed_pnl', (v) => usd(v, 2)), numCol('手续费', 'fee'), col('费币', 'fee_token'),
      { title: 'Maker', field: 'maker', formatter: yesFmt, hozAlign: 'center', headerFilter: 'tickCross', headerFilterParams: { tristate: true } },
      { title: 'TWAP', field: 'twap', formatter: yesFmt, hozAlign: 'center' }, { title: '强平', field: 'liquidation', formatter: yesFmt, hozAlign: 'center' },
      col('原始方向', 'dir_raw'), col('订单号', 'oid'), col('来源', 'source'), col('质量', 'quality')], { height: 640 });
  };

  // ---------- shell ----------
  function show(page) {
    if (!RENDER[page]) page = 'overview';
    state.page = page;
    document.querySelectorAll('#nav .nav-link').forEach((a) => a.classList.toggle('active', a.dataset.page === page));
    document.querySelectorAll('.page-section').forEach((s) => s.classList.toggle('active', s.id === 'p-' + page));
    const info = PAGES.find((p) => p[0] === page);
    document.getElementById('filters').classList.toggle('hidden', !info[2]);
    const sec = document.getElementById('p-' + page);
    if (!sec.dataset.window || (info[2] && sec.dataset.window !== state.window)) renderPage(page);
    else rerender();
  }
  function renderPage(page) {
    const sec = document.getElementById('p-' + page);
    state.charts = state.charts.filter((e) => { if (sec.contains(e.el)) { e.inst.dispose(); return false; } return true; });
    Object.keys(state.tables).forEach((id) => { if (sec.querySelector('#' + id)) { state.tables[id].destroy(); delete state.tables[id]; } });
    sec.dataset.window = state.window;
    RENDER[page](sec);
  }
  function init() {
    document.getElementById('wallet').textContent = M.wallet;
    document.getElementById('generated').textContent = '生成于 ' + date(M.generated_at_ms, true) + ' UTC';
    const rec = (M.quality || {}).reconciliation || {};
    document.getElementById('badge-recon').innerHTML = statusBadge(rec.status, (STATUS[rec.status] || STATUS.no_data)[2] + (isNum(rec.residual_pct) ? ' ' + pct(rec.residual_pct, 1) : ''));
    const t = (M.quality || {}).truncation || {};
    document.getElementById('badge-trunc').innerHTML = statusBadge(t.status === 'complete' ? 'ok' : t.status === 'api_visible_gaps' ? 'warn' : 'fail',
      { complete: '历史完整', api_visible_gaps: '历史有缺口', api_visible_truncated: '早期历史截断' }[t.status] || '历史未知');
    document.getElementById('nav').innerHTML = PAGES.map((p) => `<li class="nav-item"><a class="nav-link" data-page="${p[0]}" href="#${p[0]}">${p[1]}</a></li>`).join('');
    document.getElementById('pages').innerHTML = PAGES.map((p) => `<section class="page-section" id="p-${p[0]}"></section>`).join('');
    const wins = Object.keys(M.windows || {});
    document.getElementById('windows').innerHTML = wins.map((w) => `<button type="button" class="btn${w === state.window ? ' active' : ''}" data-w="${w}">${esc(M.windows[w].label)}</button>`).join('');
    document.getElementById('windows').addEventListener('click', (e) => {
      const w = e.target.dataset && e.target.dataset.w;
      if (!w) return;
      state.window = w;
      document.querySelectorAll('#windows .btn').forEach((b) => b.classList.toggle('active', b.dataset.w === w));
      renderPage(state.page);
    });
    document.getElementById('window-note').textContent = '作用于总览、业绩、风险页的指标与图表';
    const themeBtn = document.getElementById('theme');
    const setTheme = (t) => { document.documentElement.setAttribute('data-bs-theme', t); themeBtn.textContent = t === 'dark' ? '浅色' : '深色'; try { localStorage.setItem('hl-theme', t); } catch (e) { /* file:// may block storage */ } };
    let saved = null;
    try { saved = localStorage.getItem('hl-theme'); } catch (e) { /* ignore */ }
    setTheme(saved || (window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'));
    themeBtn.addEventListener('click', () => { setTheme(document.documentElement.getAttribute('data-bs-theme') === 'dark' ? 'light' : 'dark'); rerender(); });
    document.getElementById('footer').innerHTML = '来源标记：<b>官方</b> = Hyperliquid portfolio / 快照接口；<b>重建</b> = 由成交、资金费重建；<b>混合</b> = 官方总额 + 重建日内形状。'
      + '所有金额以 USD 计，时间为 UTC。完整数据见 exports/。';
    window.addEventListener('hashchange', () => show(location.hash.slice(1)));
    show(location.hash.slice(1) || 'overview');
  }
  document.addEventListener('DOMContentLoaded', init);
}());
