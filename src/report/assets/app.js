/* Offline wallet report. Reads window.HL_MODEL (inlined) and lazily loads
 * table chunks from data/*.js via <script> tags (file:// safe). */
(function () {
  'use strict';
  const M = window.HL_MODEL;
  const DAY = 86400000;
  const state = { page: null, window: 'all', charts: [], tables: {}, loaded: {}, pending: {}, progress: {}, shown: {}, curve: { scope: 'wallet', layers: { equity: true, invested: true, dd: true, flows: true } } };

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
  const STATUS = { ok: ['ok', '✓', '对账通过'], warn: ['warn', '!', '对账有偏差'], fail: ['fail', '✕', '对账失败'], no_data: ['', '?', '无对账数据'], insufficient_overlap: ['', '?', '可比点不足'] };

  // ---------- ratings ----------
  // Rule-of-thumb thresholds that tint a metric red (bad), amber (warn) or
  // green (good); checked in that order. Keys follow metrics/scorecard.py,
  // the last block is report-only. Drawdowns are positive magnitudes; VaR and
  // CVaR are negative daily returns.
  const lt = (x) => (v) => v < x, le = (x) => (v) => v <= x, gt = (x) => (v) => v > x, ge = (x) => (v) => v >= x;
  const RULES = {
    total_pnl: { bad: lt(0), hint: '< 0 危险' },
    twr: { bad: lt(0), hint: '< 0 危险' },
    cagr: { bad: lt(0), good: ge(0.5), hint: '< 0 危险 · ≥ 50% 优秀' },
    expectancy: { bad: lt(0), hint: '< 0 危险' },
    pnl_per_volume: { bad: lt(0), good: ge(5), hint: '< 0 危险 · ≥ 5 bps 优秀' },
    maker_share: { good: ge(0.6), hint: '≥ 60% 优秀' },
    // Win rate and payoff trade off against each other (scalpers vs trend
    // followers), so neither alone is flagged as dangerous; profit factor is.
    win_rate: { warn: lt(0.3), good: ge(0.6), hint: '< 30% 留意 · ≥ 60% 优秀（需结合盈亏比）' },
    payoff: { good: ge(2), hint: '≥ 2 优秀（需结合胜率）' },
    profit_factor: { bad: lt(1), warn: lt(1.2), good: ge(1.5), hint: 'Profit factor < 1 危险 · < 1.2 留意 · ≥ 1.5 优秀' },
    efficiency: { bad: lt(0), good: ge(0.3), hint: '< 0 危险 · ≥ 30% 优秀' },
    max_loss_streak: { bad: ge(10), warn: ge(6), hint: '≥ 10 危险 · ≥ 6 留意' },
    top10_share: { warn: gt(0.9), hint: '> 90% 留意（依赖少数大单）' },
    largest_share: { warn: gt(0.5), hint: '> 50% 留意（依赖单笔）' },
    monthly_win_rate: { bad: lt(0.4), warn: lt(0.5), good: ge(0.7), hint: '< 40% 危险 · < 50% 留意 · ≥ 70% 优秀' },
    volatility: { bad: gt(1.5), warn: gt(0.8), hint: '> 150% 危险 · > 80% 留意' },
    sharpe: { bad: lt(0), warn: lt(0.5), good: ge(2), hint: '< 0 危险 · < 0.5 留意 · ≥ 2 优秀' },
    sortino: { bad: lt(0), warn: lt(0.7), good: ge(3), hint: '< 0 危险 · < 0.7 留意 · ≥ 3 优秀' },
    calmar: { bad: lt(0.5), warn: lt(1), good: ge(3), hint: '< 0.5 危险 · < 1 留意 · ≥ 3 优秀' },
    omega: { bad: lt(1), warn: lt(1.2), good: ge(1.5), hint: '< 1 危险 · < 1.2 留意 · ≥ 1.5 优秀' },
    tail_ratio: { bad: lt(0.8), warn: lt(1), good: ge(1.2), hint: '< 0.8 危险 · < 1 留意 · ≥ 1.2 优秀' },
    var95: { bad: le(-0.1), warn: le(-0.05), hint: '≤ −10% 危险 · ≤ −5% 留意' },
    cvar95: { bad: le(-0.15), warn: le(-0.08), hint: '≤ −15% 危险 · ≤ −8% 留意' },
    leverage_mean: { bad: gt(10), warn: gt(5), hint: '> 10× 危险 · > 5× 留意' },
    max_dd: { bad: ge(0.3), warn: ge(0.2), good: le(0.1), hint: '≥ 30% 危险 · ≥ 20% 留意 · ≤ 10% 优秀' },
    current_dd: { bad: ge(0.2), warn: ge(0.1), hint: '≥ 20% 危险 · ≥ 10% 留意' },
    avg_dd_depth: { bad: ge(0.2), warn: ge(0.12), hint: '≥ 20% 危险 · ≥ 12% 留意' },
    recovery_factor: { bad: lt(1), warn: lt(2), good: ge(5), hint: '< 1 危险 · < 2 留意 · ≥ 5 优秀' },
    underwater_ratio: { bad: gt(0.7), warn: gt(0.5), good: le(0.2), hint: '> 70% 危险 · > 50% 留意 · ≤ 20% 优秀' },
    longest_underwater: { bad: gt(180), warn: gt(90), hint: '> 180 天危险 · > 90 天留意' },
    current_underwater: { bad: gt(180), warn: gt(90), hint: '> 180 天危险 · > 90 天留意' },
    mfe_capture: { bad: lt(0), warn: lt(0.2), good: ge(0.5), hint: '< 0 危险 · < 20% 留意 · ≥ 50% 优秀' },
    // report-only
    fee_drag: { bad: gt(0.5), warn: gt(0.25), good: le(0.1), hint: '手续费 ÷ 成交毛利：> 50% 危险 · > 25% 留意 · ≤ 10% 优秀' },
    cost_to_gross: { bad: gt(0.5), warn: gt(0.25), good: le(0.1), hint: '> 50% 危险 · > 25% 留意 · ≤ 10% 优秀' },
    liq_distance: { bad: lt(0.1), warn: lt(0.25), hint: '距强平 < 10% 危险 · < 25% 留意' },
    position_leverage: { bad: gt(20), warn: gt(10), hint: '> 20× 危险 · > 10× 留意' },
    volume_coverage: { bad: lt(0.5), warn: lt(0.8), good: ge(0.95), hint: '< 50% 危险 · < 80% 留意 · ≥ 95% 优秀' },
    position_breaks: { warn: gt(0), good: (v) => v === 0, hint: '> 0 留意 · 0 优秀' },
    liquidations: { bad: gt(0), hint: '> 0 危险（曾被强平）' },
    recon_status: { map: { fail: 'bad', warn: 'warn', ok: 'good' }, hint: '官方 vs 重建累计盈亏残差：≤ max(1%, $100) 通过，≤ 5% 有偏差' },
  };
  const TONE = { bad: ['tone-bad', '✕', '危险'], warn: ['tone-warn', '!', '留意'], good: ['tone-good', '✓', '优秀'] };
  const LEVELS = ['bad', 'warn', 'good'];
  function rate(key, value) {
    const r = RULES[key];
    if (!r) return null;
    if (r.map) return r.map[value] || null;
    value = num(value);
    if (!isNum(value)) return null;
    return LEVELS.find((lvl) => r[lvl] && r[lvl](value)) || null;
  }
  // {level, cls, icon, title} for tinting; title carries the thresholds.
  function tone(key, value) {
    const level = rate(key, value), r = RULES[key];
    const t = level ? TONE[level] : ['', '', ''];
    return { level, cls: t[0], icon: t[1], title: r ? (level ? t[2] + ' · ' : '') + '阈值：' + r.hint : '' };
  }
  const toneIcon = (t) => (t && t.icon ? `<span class="tone-icon">${t.icon}</span>` : '');
  const toneAttr = (t) => (t && t.title ? ` title="${esc(t.title)}"` : '');

  // ---------- theme / colors ----------
  function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
  const CHART_FONT = '"Inter Var", "Inter Fallback", "PingFang SC", "Microsoft YaHei", system-ui, sans-serif';
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
      textStyle: { fontFamily: CHART_FONT, color: c.ink2 },
      grid: { left: 64, right: 24, top: 36, bottom: 40, containLabel: false },
      tooltip: { trigger: 'axis', backgroundColor: c.surface, borderColor: c.grid, borderRadius: 10, padding: [8, 12],
        extraCssText: 'box-shadow:0 4px 14px rgba(15,27,38,.10);', textStyle: { color: c.ink1, fontSize: 12, fontFamily: CHART_FONT },
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

  // Motion is opt-out: reduced-motion users and background tabs get final values.
  const motionOK = () => !(window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches) && document.visibilityState !== 'hidden';
  // Charts initialise when they scroll near the viewport (or a folded section
  // opens); bar charts grow once on first paint, never on theme re-renders.
  function chart(el, build) {
    if (!el) return null;
    const entry = { el, inst: null, build };
    state.charts.push(entry);
    const init = () => {
      if (entry.inst || entry.dead) return;
      entry.inst = echarts.init(el, null, { renderer: 'canvas' });
      const opt = build(C());
      const bars = [].concat(opt.series || []).some((s) => s && s.type === 'bar');
      if (bars && motionOK()) Object.assign(opt, { animation: true, animationDuration: 500, animationEasing: 'cubicOut', animationDurationUpdate: 0 });
      entry.inst.setOption(opt, true);
      if (entry.onInit) entry.onInit(entry.inst);
    };
    if (window.IntersectionObserver) {
      entry.io = new IntersectionObserver((es) => { if (es.some((x) => x.isIntersecting)) { entry.io.disconnect(); init(); } }, { rootMargin: '240px 0px' });
      entry.io.observe(el);
    } else init();
    return entry;
  }
  function disposeChart(e) { e.dead = true; if (e.io) e.io.disconnect(); if (e.inst) e.inst.dispose(); }
  function rerender() {
    const c = C();
    state.charts.forEach((e) => { if (e.inst && e.el.offsetParent !== null) { e.inst.setOption(e.build(c), true); e.inst.resize(); } });
  }
  window.addEventListener('resize', () => state.charts.forEach((e) => e.inst && e.inst.resize()));
  const emptyChart = (c, text) => ({ title: { text, left: 'center', top: 'middle', textStyle: { color: c.muted, fontSize: 13, fontWeight: 400, fontFamily: CHART_FONT } } });

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
    (state.progress[name] || []).forEach((fn) => fn(got, tbl.chunks.length));
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
  // A metric cell inside a .gl-g strip (cells are divided by hairlines, not boxed).
  // `col` is kept for call compatibility; the strip lays cells out itself.
  function metricLabel(label, source, t) {
    return `${toneIcon(t)}<span class="t" title="${esc(String(label).replace(/<[^>]+>/g, ''))}">${label}</span>${source ? `<span class="src">${SOURCE[source] || source}</span>` : ''}`;
  }
  // `more` (optional): { n: raw number, id: tween identity, f: tween format, k: scorecard key for the
  // info bubble, to: jump target "page" or "page#anchor", why: reason shown
  // in place of the sub line when the value is unavailable }.
  function metricCell(cls, label, value, sub, source, t, more) {
    more = more || {};
    const missing = 'n' in more && !isNum(num(more.n));
    const subHtml = missing && more.why ? `<span class="why">${more.why}</span>` : (sub || '&nbsp;');
    const link = more.to ? ` data-to="${esc(more.to)}" role="link" tabindex="0" aria-label="${esc(String(label).replace(/<[^>]+>/g, ''))}：查看明细"` : '';
    const tween = 'n' in more && !missing && more.id ? ` data-k="${esc(more.id)}" data-n="${num(more.n)}" data-f="${more.f || 'usd'}"` : '';
    return `<div class="${cls} ${t ? t.cls : ''}${more.to ? ' jump' : ''}"${link}${more.k ? '' : toneAttr(t)}><div class="${cls}-l">${metricLabel(label, source, t)}${more.k ? infoBtn(more.k, t, value) : ''}${more.to ? '<span class="go" aria-hidden="true">›</span>' : ''}</div>
      <div class="${cls}-v" data-fit${tween}>${value}</div><div class="${cls}-s">${subHtml}</div></div>`;
  }
  function kpi(label, value, sub, source, col, t, more) { return metricCell('gl', label, value, sub, source, t, more); }
  function heroNum(label, value, sub, source, t, more) { return metricCell('hn', label, value, sub, source, t, more); }

  // Count-up between the previously shown value and the new one (650 ms, ease
  // out cubic). First paint shows final values; reduced motion skips it.
  const TWEEN = {
    usd: (v) => usd(v),
    pct: (v, fin) => { const m = /\.(\d+)%/.exec(fin); return (v * 100).toFixed(m ? m[1].length : 0) + '%'; },
    fix: (v, fin) => { const m = /\.(\d+)/.exec(fin); return v.toFixed(m ? m[1].length : 0); },
  };
  function animateNumbers(scope) {
    scope.querySelectorAll('[data-k][data-n]').forEach((el) => {
      const k = el.dataset.k, to = Number(el.dataset.n), from = state.shown[k];
      state.shown[k] = to;
      if (!motionOK() || from === undefined || from === to || !isNum(from) || !isNum(to)) return;
      const final = el.innerHTML, finText = el.textContent, f = TWEEN[el.dataset.f] || TWEEN.usd, t0 = performance.now();
      const step = (now) => {
        if (!el.isConnected) return;
        const p = Math.min(1, (now - t0) / 650), e = 1 - Math.pow(1 - p, 3);
        if (p < 1) { el.textContent = f(from + (to - from) * e, finText); requestAnimationFrame(step); } else el.innerHTML = final;
      };
      requestAnimationFrame(step);
    });
  }

  // Structured info bubble: name, definition, thresholds, current value, source.
  const DEF = {};
  function infoBtn(key, t, value) {
    const d = DEF[key] || {};
    const r = RULES[key];
    const data = { n: d.label || '', d: d.definition || '', h: r ? r.hint : '', v: String(value === undefined ? '' : value).replace(/<[^>]+>/g, ''), l: t && t.level ? TONE[t.level][2] : '', s: d.source ? (SOURCE[d.source] || d.source) : '' };
    return `<button type="button" class="info" aria-label="指标说明" data-tip="${esc(JSON.stringify(data))}">i</button>`;
  }
  let tipEl = null, tipFor = null;
  function showTip(btn) {
    let d;
    try { d = JSON.parse(btn.dataset.tip); } catch (e) { return; }
    if (!tipEl) { tipEl = document.createElement('div'); tipEl.className = 'tipbox'; tipEl.setAttribute('role', 'tooltip'); document.body.appendChild(tipEl); }
    tipEl.innerHTML = `${d.n ? `<b>${esc(d.n)}</b>` : ''}${d.d ? `<p>${esc(d.d)}</p>` : ''}${d.h ? `<div class="tb-row"><span>阈值</span>${esc(d.h)}</div>` : ''}`
      + `${d.v ? `<div class="tb-row"><span>当前</span>${esc(d.v)}${d.l ? ` · ${esc(d.l)}` : ''}</div>` : ''}${d.s ? `<div class="tb-row"><span>来源</span>${esc(d.s)}</div>` : ''}`;
    const r = btn.getBoundingClientRect();
    tipEl.style.display = 'block';
    const w = tipEl.offsetWidth, h = tipEl.offsetHeight;
    const left = Math.max(8, Math.min(window.innerWidth - w - 8, r.left + r.width / 2 - w / 2));
    const top = r.top - h - 8 > 8 ? r.top - h - 8 : r.bottom + 8;
    tipEl.style.left = left + 'px'; tipEl.style.top = top + 'px';
    tipFor = btn;
  }
  function hideTip() { if (tipEl) tipEl.style.display = 'none'; tipFor = null; }

  // Chapter = one shell with a header strip; its blocks are borderless and
  // separated by hairlines instead of nested cards. Optional: number, scope
  // chip (time range of the data inside), a one-line data summary, and a
  // smooth fold (default shut for detail chapters).
  function chapter(title, desc, blocks, opts) {
    opts = opts || {};
    const head = `${opts.no ? `<span class="chap-no">${opts.no}</span>` : ''}<h2>${title}</h2>${desc ? `<p class="chap-sum">${desc}</p>` : ''}<span class="chap-tail">${opts.chip || ''}${opts.tools || ''}${opts.fold ? `<button type="button" class="fold-btn" aria-expanded="${!opts.shut}" aria-controls="${opts.id}-body">${opts.shut ? '展开' : '收起'}<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg></button>` : ''}</span>`;
    const body = `<div class="chap-r">${blocks.join('')}</div>`;
    return `<section class="chap-box${opts.fold ? ' foldable' : ''}${opts.shut ? ' shut' : ''}"${opts.id ? ` id="${opts.id}"` : ''}><div class="chap-l">${head}</div>`
      + (opts.fold ? `<div class="clp" id="${opts.id}-body"><div class="clp-in">${body}</div></div>` : body) + '</section>';
  }
  // Scope chip: what time range the chapter's numbers cover.
  function scopeChip(kind, text, tip) {
    return `<span class="wchip ${kind}" title="${esc(tip || '')}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true">${kind === 'snap' ? '<circle cx="12" cy="12" r="3"/><path d="M4 8V6a2 2 0 0 1 2-2h2M16 4h2a2 2 0 0 1 2 2v2M20 16v2a2 2 0 0 1-2 2h-2M8 20H6a2 2 0 0 1-2-2v-2"/>' : '<rect x="4" y="5" width="16" height="15" rx="2"/><path d="M4 10h16M9 3v4M15 3v4"/>'}</svg>${esc(text)}</span>`;
  }
  const FOLD_OPEN = {};
  function setFold(box, open) {
    box.classList.toggle('shut', !open);
    const b = box.querySelector('.fold-btn');
    if (b) { b.setAttribute('aria-expanded', open); b.firstChild.textContent = open ? '收起' : '展开'; }
    if (open && box.id && FOLD_OPEN[box.id]) { const fn = FOLD_OPEN[box.id]; delete FOLD_OPEN[box.id]; setTimeout(fn, 30); }
    if (open) setTimeout(() => { queueFit(); state.charts.forEach((e) => e.inst && box.contains(e.el) && e.inst.resize()); }, 480);
  }
  function blk(body, opts) {
    opts = opts || {};
    const head = opts.title ? `<div class="blk-h"><h3>${opts.title}</h3>${opts.sub ? `<small>${opts.sub}</small>` : ''}</div>` : '';
    return `<div class="blk ${opts.w || ''}${opts.flush ? ' flush' : ''}">${head}${body}</div>`;
  }
  // Shrink a number to its container's measured width (not a character count).
  function fitAll(scope) {
    (scope || document).querySelectorAll('[data-fit]').forEach((el) => {
      if (!el.offsetParent) return;
      el.style.fontSize = '';
      const avail = el.clientWidth;
      if (!avail || el.scrollWidth <= avail) return;
      const full = parseFloat(getComputedStyle(el).fontSize);
      el.style.fontSize = Math.max(full * 0.58, Math.floor(full * avail / el.scrollWidth * 10) / 10) + 'px';
    });
  }
  let fitQueued = false;
  function queueFit() {
    if (fitQueued) return;
    fitQueued = true;
    requestAnimationFrame(() => { fitQueued = false; fitAll(); });
  }
  // Rows are [label, value, note?, tone?].
  function statTable(rows) {
    const notes = rows.some((r) => r[2] !== undefined);
    return `<table class="table table-sm stat-table mb-0"><tbody>${rows.map((r) => `<tr class="${r[3] ? r[3].cls : ''}"${toneAttr(r[3])}><td>${toneIcon(r[3])}${r[0]}</td><td class="num">${r[1]}</td>${notes ? `<td class="muted small">${r[2] !== undefined ? r[2] : ''}</td>` : ''}</tr>`).join('')}</tbody></table>`;
  }
  function statusBadge(status, text) {
    const s = STATUS[status] || STATUS.no_data;
    return `<span class="status ${s[0]}"><span class="icon">${s[1]}</span>${esc(text || s[2])}</span>`;
  }
  const chartDiv = (id, cls) => `<div class="chart ${cls || ''}" id="${id}"></div>`;
  const TABLE_NAMES = { operations: '成交', campaigns: '回合', flows: '资金流水', funding: '资金费' };
  const NARROW = '(max-width: 760px)';
  const isNarrow = () => !!(window.matchMedia && matchMedia(NARROW).matches);
  // Skeleton keeps the final height while chunks arrive; the caption says
  // which table is loading and how many chunks are in.
  function skeleton(height, caption) {
    const rows = Math.max(3, Math.min(12, Math.floor((height - 44) / 34)));
    return `<div class="skel" style="height:${height}px"><div class="skel-cap"><span class="skel-t">${caption}</span><span class="skel-bar"><i></i></span></div>${'<div class="skel-row"><i></i><i></i><i></i><i></i></div>'.repeat(rows)}</div>`;
  }
  function table(id, name, columns, opts) {
    opts = opts || {};
    const el = document.getElementById(id);
    if (!el || state.tables[id]) return;
    const height = opts.height || 480;
    const empty = opts.empty || '无记录';
    const make = (rows) => {
      if (opts.onRows) opts.onRows(rows);
      // No rows: a sentence explaining why beats an empty grid of headers.
      if (!rows.length) { el.innerHTML = `<div class="empty-why">${empty}</div>`; state.tables[id] = { destroy() {}, setFilter() {}, clearFilter() {} }; return; }
      // Small tables turn into label/value cards on phones.
      if (opts.cards !== false && isNarrow() && rows.length <= 60) { el.innerHTML = cardList(rows, columns, empty); state.tables[id] = { destroy() {}, setFilter() {}, clearFilter() {} }; return; }
      el.innerHTML = '';
      state.tables[id] = new Tabulator(el, Object.assign({
        data: rows, columns, layout: 'fitDataStretch', height: rows.length ? height : Math.min(height, 150), pagination: false,
        placeholder: `<div class="empty-why">${empty}</div>`, renderVertical: 'virtual', columnDefaults: { headerTooltip: true },
      }, opts.tabulator || {}));
    };
    if (Array.isArray(name)) { make(name); return; }
    const tbl = M.tables[name] || { chunks: [] };
    el.innerHTML = skeleton(height, `正在载入${TABLE_NAMES[name] || name}：0 / ${tbl.chunks.length} 块 · ${int(tbl.count)} 条`);
    (state.progress[name] = state.progress[name] || []).push((got, total) => {
      const cap = el.querySelector('.skel-t'), bar = el.querySelector('.skel-bar i');
      if (cap) cap.textContent = `正在载入${TABLE_NAMES[name] || name}：${got} / ${total} 块 · ${int(tbl.count)} 条`;
      if (bar) bar.style.width = Math.round(got / total * 100) + '%';
    });
    load(name).then(make);
  }
  // Render Tabulator column definitions as stacked cards (narrow screens).
  function cardList(rows, columns, empty) {
    if (!rows.length) return `<div class="empty-why">${empty}</div>`;
    const cell = (c, row) => {
      const span = document.createElement('span');
      const v = row[c.field];
      const html = c.formatter && typeof c.formatter === 'function'
        ? c.formatter({ getValue: () => v, getElement: () => span, getRow: () => ({ getData: () => row }) })
        : (v === null || v === undefined ? '' : esc(v));
      return `<span class="${span.className}">${html === undefined || html === null || html === '' ? '—' : html}</span>`;
    };
    const [head, ...rest] = columns.filter((c) => c.field);
    return `<div class="mcards">${rows.map((row) => `<div class="mcard"><div class="mc-h">${cell(head, row)}</div><dl>${rest.map((c) => `<div><dt>${esc(c.title)}</dt><dd>${cell(c, row)}</dd></div>`).join('')}</dl></div>`).join('')}</div>`;
  }
  const fmtCell = (fn) => (cell) => fn(cell.getValue());
  const yesFmt = (cell) => (cell.getValue() ? '是' : '');
  const col = (title, field, extra) => Object.assign({ title, field, headerFilter: 'input' }, extra || {});
  const numCol = (title, field, fn, extra) => Object.assign({ title, field, hozAlign: 'right', sorter: 'number', formatter: fmtCell(fn || ((v) => (v === null ? '' : Number(v).toLocaleString('en-US', { maximumFractionDigits: 6 })))) }, extra || {});
  const timeCol = (title, field) => ({ title, field, sorter: 'number', formatter: fmtCell((v) => date(v, true)), width: 140 });
  // Cell elements are reused by the virtual renderer, so classes are set both ways.
  const setClasses = (el, map) => Object.keys(map).forEach((k) => el.classList.toggle(k, !!map[k]));
  const signedFmt = (fn) => (cell) => {
    const v = num(cell.getValue());
    setClasses(cell.getElement(), { 'signed-pos': v > 0, 'signed-neg': v < 0 });
    return fn(cell.getValue());
  };
  const toneFmt = (key, fn) => (cell) => {
    const t = tone(key, cell.getValue()), el = cell.getElement();
    setClasses(el, { 'cell-tone-bad': t.level === 'bad', 'cell-tone-warn': t.level === 'warn', 'cell-tone-good': t.level === 'good' });
    el.title = t.level ? t.title : '';
    return fn(cell.getValue());
  };
  const signedCol = (title, field, fn, extra) => numCol(title, field, fn, Object.assign({ formatter: signedFmt(fn) }, extra || {}));
  const toneCol = (title, field, key, fn, extra) => numCol(title, field, fn, Object.assign({ formatter: toneFmt(key, fn || ((v) => (v === null ? '' : fix(v)))) }, extra || {}));

  // ---------- pages ----------
  const PAGES = [
    ['overview', '总览', true], ['scorecard', '指标总表', true], ['performance', '业绩', true], ['risk', '风险', true], ['trading', '交易行为', false],
    ['positions', '持仓与资产', false], ['costs', '成本', false], ['flows', '资金流', false], ['quality', '数据质量', false], ['raw', '原始数据', false],
  ];
  const RENDER = {};

  // ---------- overview: a numbered report that reads top to bottom ----------
  // Reasons shown in place of a metric when it cannot be computed.
  const WHY = {
    twr: '收益日不足 2 天或日初净值过小，无法串联',
    cagr: '窗口不足 30 天，不做年化',
    max_dd: '窗口内日序列不足，无法计算回撤',
    sharpe: '有效收益日少于 2 天',
    win_rate: '没有完整回合：可见成交中没有开平都可见的仓位',
    payoff: '盈利或亏损回合数为 0，比值无意义',
    pnl: '窗口内没有官方净值采样',
    equity: '没有官方净值或账户快照',
    leverage: '没有账户快照',
  };
  // Official perp cumulative PnL vs reconstruction, both rebased to 0 at the
  // first official sample of the window (reconstruction joins where it starts).
  function perpCurves() {
    const off = (M.official.series_perp || []).filter((x) => inWin(x.time));
    const base0 = off.length ? off[0].cum_pnl : 0;
    const mdays = M.mtm.days || [];
    const rec = [];
    const startT = off.length ? off[0].time : -Infinity;
    const firstT = mdays.length ? Math.max(startT, mdays[0].time) : startT;
    const at = (t) => {
      const i = off.findIndex((x) => x.time >= t);
      if (i <= 0) return i === 0 ? off[0].cum_pnl : (off.length ? off[off.length - 1].cum_pnl : 0);
      const a = off[i - 1], b = off[i];
      return a.cum_pnl + (b.cum_pnl - a.cum_pnl) * (t - a.time) / (b.time - a.time);
    };
    let acc = off.length && firstT > startT ? at(firstT) - base0 : 0;
    if (mdays.length) rec.push([firstT, acc]);
    mdays.forEach((d) => {
      const end = d.time + DAY;
      if (end <= startT) return;
      acc += d.pnl * (d.time < startT ? (end - startT) / DAY : 1);
      rec.push([end, acc]);
    });
    return { official: off.map((x) => [x.time, x.cum_pnl - base0]), rebuilt: rec };
  }
  function reconChart(c) {
    const pc = perpCurves();
    if (!pc.official.length && !pc.rebuilt.length) return emptyChart(c, '没有官方永续盈亏采样，无法对账');
    return baseOption(c, { legend: legend(c), xAxis: timeAxis(c), yAxis: valueAxis(c),
      tooltip: Object.assign(baseOption(c).tooltip, { valueFormatter: (v) => usd(v, 0) }),
      series: [
        { type: 'line', name: '官方', showSymbol: false, lineStyle: { width: 2, color: c.s1 }, itemStyle: { color: c.s1 }, data: pc.official },
        { type: 'line', name: '重建', showSymbol: false, lineStyle: { width: 2, color: c.s2 }, itemStyle: { color: c.s2 }, data: pc.rebuilt },
      ] });
  }

  // Evidence strength (how far the numbers can be trusted / generalised) is
  // kept apart from risk. Every point is a plain, checkable rule.
  function evidence() {
    const q = M.quality || {}, rec = q.reconciliation || {}, t = q.truncation || {}, b = M.behavior || {};
    const daysN = num(((M.windows.all || {}).performance || {}).return_days) || 0;
    const camps = num(b.campaigns_evaluated) || 0;
    const rules = [
      { ok: rec.status === 'ok', label: '对账通过', next: '对账未通过：重建与官方盈亏不一致，先看“数据质量”页' },
      { ok: (t.status === 'no_gap_detected' || t.status === 'complete'), label: '未发现历史缺口', next: '成交历史有缺口或被截断，可补数或导入归档' },
      { ok: camps >= 30, label: '完整回合 ≥ 30', next: `还差 ${int(30 - camps)} 个完整回合（现 ${int(camps)} / 30）` },
      { ok: daysN >= 90, label: '日序列 ≥ 90 天', next: `还需积累 ${int(90 - daysN)} 天日序列（现 ${int(daysN)} / 90 天）` },
    ];
    const level = rules.filter((r) => r.ok).length;
    return { level, word: ['很弱', '弱', '一般', '较强', '强'][level], rules, next: rules.find((r) => !r.ok) };
  }
  function riskLevel(n) {
    const level = n.bad >= 3 ? 4 : n.bad >= 1 ? 3 : n.warn >= 3 ? 2 : n.warn >= 1 ? 1 : 0;
    return { level, word: ['很低', '低', '中', '偏高', '高'][level] };
  }
  const meter = (level, of, cls, title) => `<span class="mtr ${cls}" role="img" aria-label="${esc(title)}" title="${esc(title)}">${Array.from({ length: of }, (_, i) => `<i class="${i < level ? 'on' : ''}"></i>`).join('')}</span>`;

  // "Skill or luck": cumulative log return vs the 80% band a zero-mean random
  // walk with the same daily volatility would stay in. Plain t-test logic.
  const Z80 = 1.2816;
  function luck() {
    const rets = days().filter((d) => isNum(d.ret) && d.ret > -1);
    const lr = rets.map((d) => Math.log(1 + d.ret));
    const n = lr.length;
    if (n < 2) return { n, enough: false };
    const mean = lr.reduce((s, x) => s + x, 0) / n;
    const sd = Math.sqrt(lr.reduce((s, x) => s + (x - mean) * (x - mean), 0) / (n - 1));
    let acc = 0;
    const path = rets.map((d, i) => { acc += lr[i]; return [d.time, acc]; });
    const trimK = Math.max(1, Math.round(n * 0.05));
    const best = lr.map((x, i) => [x, i]).sort((a, b) => b[0] - a[0]).slice(0, trimK).map((x) => x[1]);
    const drop = new Set(best);
    let acc2 = 0;
    const trimmed = rets.map((d, i) => { if (!drop.has(i)) acc2 += lr[i]; return [d.time, acc2]; });
    const z = sd > 0 ? acc / (sd * Math.sqrt(n)) : 0;
    return { n, enough: n >= 20, mean, sd, path, trimmed, trimK, z, total: acc, trimTotal: acc2 };
  }
  function luckSentence(L) {
    if (!L.enough) return `只有 ${int(L.n)} 个收益日，样本太短，无法区分能力与运气`;
    const pos = L.z > Z80 ? '高于随机区间上沿' : L.z < -Z80 ? '低于随机区间下沿' : '仍在随机区间内，尚不能排除运气';
    const dep = L.total > 0 && L.trimTotal <= 0 ? `；去掉最好的 ${L.trimK} 天后转为亏损，依赖少数好日子` : '';
    return '累计收益' + pos + dep;
  }

  function sparkline(series) {
    if (series.length < 2) return '<div class="spk empty-why">日序列不足，无法画趋势</div>';
    const W0 = 300, H0 = 52, pad = 4;
    const xs = series.map((p) => p[0]), ys = series.map((p) => p[1]);
    const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
    const X = (x) => pad + (x - x0) / ((x1 - x0) || 1) * (W0 - pad * 2), Y = (y) => H0 - pad - (y - y0) / ((y1 - y0) || 1) * (H0 - pad * 2);
    const d = series.map((p, i) => (i ? 'L' : 'M') + X(p[0]).toFixed(1) + ' ' + Y(p[1]).toFixed(1)).join('');
    const pk = series.reduce((m, p) => (p[1] > m[1] ? p : m), series[0]), last = series[series.length - 1], first = series[0];
    const dot = (p, cls) => `<circle class="${cls}" cx="${X(p[0]).toFixed(1)}" cy="${Y(p[1]).toFixed(1)}" r="3"/>`;
    return `<div class="spk"><svg viewBox="0 0 ${W0} ${H0}" preserveAspectRatio="none" aria-hidden="true"><path class="spk-a" d="${d}L${X(last[0]).toFixed(1)} ${H0}L${X(first[0]).toFixed(1)} ${H0}Z"/><path class="spk-l" d="${d}" vector-effect="non-scaling-stroke"/>${dot(first, 'p0')}${dot(pk, 'pk')}${dot(last, 'pn')}</svg>
      <div class="spk-k"><span>起 ${usd(first[1])}</span><span>峰 ${usd(pk[1])} · ${date(pk[0])}</span><span>今 ${usd(last[1])}</span></div></div>`;
  }

  // Account total -> buckets, each row with a share bar.
  function bucketBars(cur) {
    const rows = (cur.buckets || []).filter((x) => isNum(num(x.value))).sort((a, b) => b.value - a.value);
    const total = rows.reduce((s, x) => s + Math.max(0, x.value), 0);
    if (!rows.length) return `<div class="empty-why">${cur.time ? '快照中没有资产' : '未取到账户快照，无法显示资产分布'}</div>`;
    return `<div class="nw"><div class="nw-t"><span>账户总额</span><b>${usd(total)}</b></div>${rows.map((x) => {
      const s = total > 0 ? Math.max(0, x.value) / total : 0;
      return `<div class="nw-r"><span class="nw-n">${esc(x.bucket)}${x.scope === 'related' ? ' <small class="muted" title="子账户 / 金库：关联资产，不计入本钱包交易收益">关联</small>' : ''}</span><span class="nw-v">${usd(x.value)}</span><span class="nw-p">${pct(s, 1)}</span><span class="bar"><i style="width:${(s * 100).toFixed(1)}%"></i></span></div>`;
    }).join('')}</div>`;
  }

  function hourBars(heat) {
    const h = new Array(24).fill(0);
    (heat || []).forEach((x) => { if (isNum(x.hour)) h[x.hour] += num(x.volume) || 0; });
    const mx = Math.max(...h);
    if (!(mx > 0)) return '';
    return `<div class="hrs" title="按 UTC 小时的成交额分布（完整热力图见“交易行为”页）">${h.map((v, i) => `<i style="height:${Math.max(6, v / mx * 100).toFixed(0)}%" title="${i}:00 UTC · ${usd(v)}"></i>`).join('')}</div><div class="hrs-k"><span>0</span><span>6</span><span>12</span><span>18</span><span>23 UTC</span></div>`;
  }

  // Point estimate + 80% interval + zero line, as a small SVG error bar.
  function errorBar(label, mean, sd, n, fmt, unit) {
    if (!(n >= 2) || !isNum(mean) || !isNum(sd)) return `<div class="eb"><div class="eb-h"><span>${label}</span><b>—</b></div><div class="why">样本不足（n = ${int(n)}）</div></div>`;
    const half = Z80 * sd / Math.sqrt(n), lo = mean - half, hi = mean + half;
    const ext = Math.max(Math.abs(lo), Math.abs(hi)) * 1.15 || 1;
    const X = (v) => (50 + v / ext * 46).toFixed(2);
    const verdict = lo > 0 ? '区间在 0 之上' : hi < 0 ? '区间在 0 之下' : '区间跨过 0';
    return `<div class="eb"><div class="eb-h"><span>${label}</span><b>${fmt(mean)}</b></div>
      <svg viewBox="0 0 100 18" preserveAspectRatio="none" aria-hidden="true"><line class="eb-z" x1="50" x2="50" y1="0" y2="18"/><line class="eb-r" x1="${X(lo)}" x2="${X(hi)}" y1="9" y2="9"/><line class="eb-c" x1="${X(lo)}" x2="${X(lo)}" y1="5" y2="13"/><line class="eb-c" x1="${X(hi)}" x2="${X(hi)}" y1="5" y2="13"/><circle class="eb-p" cx="${X(mean)}" cy="9" r="2.6"/></svg>
      <div class="eb-k">80% 区间 ${fmt(lo)} ~ ${fmt(hi)} · ${verdict} · n = ${int(n)} ${unit}</div></div>`;
  }

  function profileTiles(b, a, r, cur) {
    const tags = (M.profile || {}).tags || {};
    const evald = num(b.campaigns_evaluated) || 0, wins = num(b.wins);
    const tile = (name, icon, t, main, sub, extra) => `<div class="tile"><div class="tile-h">${svg(icon)}<span>${name}</span>${t && t.level ? `<span class="lv ${t.level}">${TONE[t.level][2]}</span>` : '<span class="lv">—</span>'}</div><div class="tmain">${main}</div><div class="tsub">${sub}</div>${extra || ''}</div>`;
    const pf = tone('profit_factor', b.profit_factor), dd = tone('max_dd', r.max_drawdown);
    return `<div class="tprof">
      ${tile('交易', 'style', null, `${int(a.fills)} 笔成交${isNum(num(a.orders)) ? ' · ' + int(a.orders) + ' 个订单' : ''} · ${int(evald)} 个完整回合`, `活跃 ${int(a.active_days)} / ${int(a.span_days)} 天 · 日均 ${fix(a.fills_per_active_day, 0)} 笔`, hourBars(a.heatmap))}
      ${tile('结果', 'target', pf.level ? pf : tone('win_rate', b.win_rate), `胜率 ${pct(b.win_rate)} · Profit factor ${fix(b.profit_factor)}`, isNum(wins) ? `赢 ${int(wins)} · 输或持平 ${int(evald - wins)} · 每回合期望 ${usd(b.expectancy)}` : '没有完整回合')}
      ${tile('风险', 'leverage', dd, `最大回撤 ${pct(r.max_drawdown)} · 平均杠杆 ${fix((M.risk || {}).leverage_mean)}×`, `最长连亏 ${int(b.max_loss_streak)} 回合 · 被强平 ${int(b.liquidated_campaigns)} 次`)}
      ${tile('行为', 'activity', tone('maker_share', a.maker_share), `${esc(tags.style || '—')} · ${esc(tags.bias || '—')}`, `持仓中位 ${hours(b.median_hold_hours)} · Maker ${pct(a.maker_share)} · ${int(a.coin_count)} 个币种`)}
      ${tile('资金', 'wallet', null, `净值 ${usd(cur.equity ?? M.official.equity)} · ${esc(tags.size || '体量未知')}`, `净资本流入 ${usd((M.flows || {}).net_capital)} · 子账户 ${int((cur.sub_accounts || []).length)} · 金库 ${int((cur.vaults || []).length)}`)}
    </div>`;
  }

  // Round 3: compact blocks for the new backend fields.
  function stressHtml(cur) {
    const sc = ((cur.stress || {}).scenarios || []);
    if (!cur.time || !(cur.positions || []).length || !sc.length) return '';
    return `<div class="mini-h">压力测试（全部仓位同时逆向）</div><table class="table table-sm stat-table mb-0"><tbody>${sc.map((s) => {
      const t = isNum(s.pct_equity) ? (s.pct_equity > 0.5 ? TONE.bad : s.pct_equity > 0.2 ? TONE.warn : null) : null;
      return `<tr class="${t ? t[0] : ''}"><td>价格逆向 ${pct(s.shock, 0)}</td><td class="num">${usd(-s.loss_usd)}</td><td class="muted small">${isNum(s.pct_equity) ? '占净值 ' + pct(s.pct_equity, 0) : ''}${s.liquidated ? ' · ' + int(s.liquidated) + ' 仓触及强平' : ''}</td></tr>`;
    }).join('')}</tbody></table><div class="method">线性近似：不含资金费、对冲相关性与强平路径。</div>`;
  }
  function recentStrip(b) {
    const xs = b.last_20 || [];
    if (!xs.length) return '<div class="empty-why">没有完整回合</div>';
    const mx = Math.max(...xs.map((x) => Math.abs(num(x.pnl) || 0))) || 1;
    const cs = num(b.current_streak) || 0;
    const streak = cs > 0 ? `当前连胜 ${int(cs)}` : cs < 0 ? `当前连亏 ${int(-cs)}` : '';
    return `<div class="l20">${xs.map((x) => {
      const p = num(x.pnl) || 0, h = Math.max(8, Math.abs(p) / mx * 100);
      return `<i class="${p > 0 ? 'w' : p < 0 ? 'l' : 'f'}" style="height:${h.toFixed(0)}%" title="${esc(x.coin)} · ${usd(p)} · ${date(x.close_time)}"></i>`;
    }).join('')}</div><div class="spk-k"><span>赢 ${int(xs.filter((x) => x.pnl > 0).length)} · 输 ${int(xs.filter((x) => x.pnl < 0).length)}</span><span>${streak}</span><span>合计 ${usd(xs.reduce((s, x) => s + (num(x.pnl) || 0), 0))}</span></div>`;
  }
  function winRateBox(b) {
    const ci = b.win_rate_strict_ci95, be = num(b.break_even_win_rate), ws = num(b.win_rate_strict);
    const ciTxt = Array.isArray(ci) ? `95% 区间 ${pct(ci[0], 0)} ~ ${pct(ci[1], 0)}` : '样本不足';
    const verdictTxt = !isNum(ws) || !isNum(be) ? '样本不足，无法判断' : Array.isArray(ci) && ci[0] > be ? '区间下沿高于盈亏平衡线：胜率足以覆盖盈亏比' : Array.isArray(ci) && ci[1] < be ? '区间上沿低于盈亏平衡线：按当前盈亏比会亏损' : '区间跨过盈亏平衡线，尚不能下结论';
    return statTable([
      ['严格胜率（不含持平）', pct(ws), ciTxt],
      ['盈亏平衡胜率', pct(be), '= 平均亏损 ÷ (平均盈利 + 平均亏损)'],
      ['含持平胜率', pct(b.win_rate), '持平 ' + int(b.flats) + ' 个'],
      ['仅价格（不含资金费）', pct(b.win_rate_trade_only), ''],
      ['费前胜率', pct(b.win_rate_before_fees), '手续费吃掉的胜率'],
      ['独立回合数', int(b.independent_trades), '同时持仓的回合合并计 · 平均并发 ' + fix(b.mean_concurrent, 1)],
    ]) + `<div class="method">${verdictTxt}</div>`;
  }
  function topTrades(b) {
    const row = (x) => `<tr><td>${esc(x.coin)} <small class="muted">${x.direction === 'long' ? '多' : x.direction === 'short' ? '空' : ''}</small></td><td class="num ${x.pnl < 0 ? 'neg' : ''}">${usd(x.pnl)}</td><td class="muted small">${date(x.close_time)}</td></tr>`;
    const cc = b.concentration || {};
    const body = (b.top_wins || []).map(row).join('') + (b.top_losses || []).map(row).join('');
    if (!body) return '<div class="empty-why">没有完整回合</div>';
    return `<table class="table table-sm stat-table mb-0"><tbody>${body}</tbody></table>
      <div class="method">净盈亏 ${usd(cc.net)} · 去掉最好 1 笔 ${usd(cc.profit_without_best)} · 去掉最好 5 笔 ${usd(cc.profit_without_top5)}${isNum(num(cc.top5_of_net)) ? ' · 前 5 笔相当于净盈亏的 ' + pct(cc.top5_of_net, 0) : ''}</div>`;
  }
  function capitalHtml(k) {
    if (!k || !k.rows) return '<div class="mini-h">资金</div><div class="empty-why">没有外部出入金流水</div>';
    return `<div class="mini-h">资金（逐笔流水）</div>` + statTable([
      ['累计入金 / 出金', usd(k.deposited) + ' / ' + usd(k.withdrawn), isNum(num(k.withdrawn_to_deposited)) ? '出入比 ' + pct(k.withdrawn_to_deposited, 0) : ''],
      ['净投入 · 峰值', usd(k.net) + ' · ' + usd(k.peak_net_invested), int(k.rows) + ' 笔' + (k.approx_rows ? '，估值 ' + int(k.approx_rows) + ' 笔' : '')],
      [`近 ${int(k.recent_days)} 天入金占比`, pct(k.recent_share_of_deposits, 0), '高 = 近期才加大投入'],
    ]);
  }
  function marketHtml(mk) {
    if (!mk) return '';
    if (mk.status !== 'ok') return `<div class="mini-h">与 BTC 的相关性</div><div class="empty-why">${mk.status === 'insufficient_data' ? `可配对日 ${int(mk.n)} 天，少于 ${int(mk.min_days)} 天` : '无法估计'}</div>`;
    const ci = (x) => (Array.isArray(x) ? `95% ${fix(x[0], 2)} ~ ${fix(x[1], 2)}` : '');
    return `<div class="mini-h">与 BTC 的相关性（日收益回归，n = ${int(mk.n)}）</div>` + statTable([
      ['Beta', fix(mk.beta, 2), ci(mk.beta_ci95)],
      ['Alpha（日，年化）', pct(mk.alpha, 3) + ' · ' + pct(mk.alpha_annual, 0), 't = ' + fix(mk.t_alpha, 2) + (mk.trimmed ? ' · 去掉最好 ' + int(mk.trimmed.dropped_days) + ' 天后 t = ' + fix(mk.trimmed.t_alpha, 2) : '')],
      ['R²', fix(mk.r2, 2), '低 = 收益与 BTC 关系弱'],
    ]);
  }
  function evidenceTierHtml(e) {
    if (!e) return '';
    const names = { thin: '单薄', fair: '尚可', solid: '扎实' };
    const lv = { thin: 1, fair: 2, solid: 3 }[e.tier] || 0;
    const miss = Object.entries(e.missing || {}).filter(([, v]) => v > 0).map(([k, v]) => ({ active_days: '有盈亏的收益日', trades: '完整回合', months: '活跃月' }[k] + ' 还差 ' + int(v))).join(' · ');
    return `<div class="mini-h">统计证据强度 ${meter(lv, 3, 'ev', names[e.tier] || '')} <b>${names[e.tier] || '—'}</b></div>
      <div class="method">有盈亏的收益日 ${int(e.active_days)} · 完整回合 ${int(e.trades)} · 活跃月 ${int(e.months)}${e.next_tier ? '；升到“' + names[e.next_tier] + '”：' + (miss || '已满足') : '；已是最高档'}。与数据完整性分开评估。</div>`;
  }

  // "Adds up" equations. Account level uses the ledger (not the implied flow,
  // which would make the equation an identity); perp level uses the breakdown.
  function reconEquations() {
    const pa = (M.windows.all || {}).performance || {};
    const bd = M.pnl_breakdown, rec = (M.quality || {}).reconciliation || {};
    const term = (label, v, first) => `<span class="eq-t"><small>${label}</small><b class="${v < 0 ? 'neg' : ''}">${first ? '' : (v < 0 ? '− ' : '+ ')}${first ? usd(v) : usd(Math.abs(v))}</b></span>`;
    const perp = bd && bd.rows ? `<div class="eq"><div class="eq-h"><span>永续层（重建 vs 官方）</span>${statusBadge(rec.status, ({ ok: '对得上', warn: '有偏差', fail: '对不上' })[rec.status] || '无对账数据')}</div>
      <div class="eq-row">${term('官方永续盈亏', num(bd.official), true)}<span class="eq-o">=</span>${bd.rows.map((x, i) => term(esc(x.name), num(x.value), false) + (i < bd.rows.length - 1 ? '' : '')).join('')}</div></div>` : '';
    const acct = `<div class="eq" id="eq-acct"><div class="eq-h"><span>账户层（全部历史）</span><span class="muted small">计算中…</span></div></div>`;
    return { html: acct + perp, fill: () => {
      const host = document.getElementById('eq-acct');
      if (!host) return;
      const s = num(pa.start_equity), e = num(pa.end_equity), pnl = num(pa.pnl);
      const t0 = ((M.series.days || [])[0] || {}).time;
      if (![s, e, pnl].every(isNum)) { host.innerHTML = '<div class="eq-h"><span>账户层（全部历史）</span></div><div class="why">缺少期初 / 期末净值或官方盈亏，无法列等式</div>'; return; }
      load('flows').then((rows) => {
        const cap = { external: 1, subaccount: 1, vault: 1, staking: 1 };
        const capL = { '外部出入金': 1, '子账户划转': 1, '金库': 1, '质押': 1 };
        const ledger = rows.filter((x) => (cap[x.class] || capL[x.class_label]) && x.amount_usd !== null && (!t0 || x.time > t0)).reduce((acc, x) => acc + Number(x.amount_usd), 0);
        const other = e - s - ledger - pnl, tol = Math.max(100, Math.abs(e) * 0.01);
        const ok = Math.abs(other) <= tol;
        host.innerHTML = `<div class="eq-h"><span>账户层（全部历史）</span>${statusBadge(ok ? 'ok' : 'warn', ok ? '对得上' : '有未解释差额')}</div>
          <div class="eq-row">${term('期末净值', e, true)}<span class="eq-o">=</span>${term('期初净值', s, true)}${term('净资本流入（流水）', ledger)}${term('交易盈亏（官方）', pnl)}${term('其他 / 未解释', other)}</div>
          <div class="eq-k">“其他”来自流水缺失、现货价格变动或子账户；容差 max($100, 1% 净值)。</div>`;
      });
    } };
  }

  function campaignCols() {
    return [timeCol('开仓', 'open_time'), timeCol('平仓', 'close_time'), col('币种', 'coin', { formatter: coinFmt }), col('方向', 'direction_label'), col('状态', 'status'),
      { title: '完整', field: 'complete', formatter: yesFmt, hozAlign: 'center', headerFilter: 'tickCross', headerFilterParams: { tristate: true } },
      numCol('最大仓位', 'max_size'), numCol('开仓名义', 'observed_entry_notional', (v) => usd(v)), signedCol('净盈亏', 'net_pnl', (v) => usd(v)),
      signedCol('资金费', 'funding', (v) => usd(v)), signedCol('含资金费净盈亏', 'net_pnl_with_funding', (v) => usd(v)),
      numCol('持仓时长', 'duration_ms', (v) => (v === null ? '' : hours(v / 3600000))), numCol('成交数', 'fill_count', int), col('结束方式', 'ended_by'), col('质量标记', 'quality')];
  }
  // Business quick filters above the column filters (which stay available).
  function campaignTable(id, barId, height) {
    const f = { kind: 'all', tiny: false };
    const pnlOf = (x) => num(x.net_pnl_with_funding ?? x.net_pnl);
    const KINDS = [['all', '全部', () => true], ['open', '持仓中', (x) => x.status === 'open'],
      ['won', '盈利', (x) => x.status !== 'open' && pnlOf(x) > 0], ['lost', '亏损或持平', (x) => x.status !== 'open' && isNum(pnlOf(x)) && pnlOf(x) <= 0]];
    const apply = () => {
      const t = state.tables[id];
      if (!t) return;
      const k = KINDS.find((x) => x[0] === f.kind)[2];
      t.setFilter((x) => k(x) && (!f.tiny || !(num(x.observed_entry_notional) < 100)));
    };
    table(id, 'campaigns', campaignCols(), { height: height || 480, empty: '没有回合：可见成交为空或历史被截断', onRows: (rows) => {
      const bar = document.getElementById(barId);
      if (!bar) return;
      bar.innerHTML = KINDS.map((k) => `<button type="button" class="qf${k[0] === f.kind ? ' on' : ''}" data-qf="${k[0]}" aria-pressed="${k[0] === f.kind}">${k[1]} <span>${int(rows.filter(k[2]).length)}</span></button>`).join('')
        + `<label class="qf-x"><input type="checkbox" data-qf-tiny> 隐藏极小回合（开仓名义 &lt; $100，${int(rows.filter((x) => num(x.observed_entry_notional) < 100).length)} 条）</label>`;
      bar.addEventListener('click', (e) => {
        const b = e.target.closest('[data-qf]');
        if (!b) return;
        f.kind = b.dataset.qf;
        bar.querySelectorAll('[data-qf]').forEach((x) => { x.classList.toggle('on', x === b); x.setAttribute('aria-pressed', x === b); });
        apply();
      });
      bar.querySelector('[data-qf-tiny]').addEventListener('change', (e) => { f.tiny = e.target.checked; apply(); });
    } });
  }

  const hexA = (hex, a) => {
    const h = String(hex).replace('#', '');
    if (h.length !== 6) return hex;
    return `rgba(${parseInt(h.slice(0, 2), 16)},${parseInt(h.slice(2, 4), 16)},${parseInt(h.slice(4, 6), 16)},${a})`;
  };
  const fade = (color, top) => new echarts.graphic.LinearGradient(0, 0, 0, 1, [{ offset: 0, color: hexA(color, top) }, { offset: 1, color: hexA(color, 0) }]);

  // Equity (or perp PnL) on top, drawdown below, one shared time axis.
  // Layers: equity, net invested, drawdown, deposit/withdraw events.
  function curveOption(c) {
    const L = state.curve.layers, wallet = state.curve.scope === 'wallet';
    const d = days();
    const grids = [{ left: 64, right: 96, top: 14, bottom: '36%' }, { left: 64, right: 96, top: '70%', bottom: 26 }];
    const xa = (i) => Object.assign(timeAxis(c), { gridIndex: i, axisLabel: { show: i === 1, color: c.muted, hideOverlap: true } });
    const end = (name, fmt, color) => ({ show: true, formatter: (p) => `${name}\n${fmt(p.value[1])}`, color, fontSize: 11, lineHeight: 14, fontFamily: CHART_FONT, distance: 6 });
    const series = [];
    let top = [], dd = [];
    if (wallet) {
      top = d.map((x) => [x.time, x.equity]);
      dd = ((M.risk || {}).underwater || []).filter((x) => inWin(x.time)).map((x) => [x.time, x.dd]);
      if (L.equity) series.push({ type: 'line', name: '账户净值', xAxisIndex: 0, yAxisIndex: 0, showSymbol: false, lineStyle: { width: 2, color: c.s1 }, itemStyle: { color: c.s1 }, areaStyle: { color: fade(c.s1, 0.22) }, endLabel: end('净值', (v) => usd(v), c.ink1), data: top, z: 3 });
      if (L.invested && d.length) {
        let inv = num((W().performance || {}).start_equity);
        if (!isNum(inv)) inv = d[0].equity - d[0].pnl - d[0].flow;
        series.push({ type: 'line', name: '净投入', xAxisIndex: 0, yAxisIndex: 0, step: 'end', showSymbol: false, lineStyle: { width: 1.4, type: 'dashed', color: c.ink2 }, itemStyle: { color: c.ink2 }, endLabel: end('净投入', (v) => usd(v), c.ink2), data: d.map((x) => { inv += x.flow || 0; return [x.time, inv]; }), z: 2 });
      }
      if (L.flows && state.curve.events) {
        const eq = (t) => { const k = d.findIndex((x) => x.time >= t); return k < 0 ? (d.length ? d[d.length - 1].equity : 0) : d[k].equity; };
        const ev = state.curve.events.filter((x) => inWin(x.time));
        series.push({ type: 'scatter', name: '出入金', xAxisIndex: 0, yAxisIndex: 0, symbolSize: 10, z: 5,
          tooltip: { trigger: 'item', formatter: (p) => `${p.data.amt > 0 ? '入金' : '出金'} ${usd(Math.abs(p.data.amt), 0)}<br>${date(p.data.value[0], true)} UTC` },
          data: ev.map((x) => ({ value: [x.time, eq(x.time)], amt: x.amount, symbol: 'triangle', symbolRotate: x.amount > 0 ? 0 : 180,
            itemStyle: { color: x.amount > 0 ? c.pos : c.neg, borderColor: c.surface, borderWidth: 1.5 } })) });
      }
    } else {
      const pc = perpCurves();
      top = pc.official;
      let pk = -Infinity;
      dd = pc.official.map((x) => { pk = Math.max(pk, x[1]); return [x[0], x[1] - pk]; });
      if (L.equity) {
        series.push({ type: 'line', name: '官方永续累计盈亏', xAxisIndex: 0, yAxisIndex: 0, showSymbol: false, lineStyle: { width: 2, color: c.s1 }, itemStyle: { color: c.s1 }, areaStyle: { color: fade(c.s1, 0.18), origin: 0 }, endLabel: end('官方', (v) => usd(v), c.ink1), data: pc.official, z: 3 });
        series.push({ type: 'line', name: '重建', xAxisIndex: 0, yAxisIndex: 0, showSymbol: false, lineStyle: { width: 1.6, color: c.s2 }, itemStyle: { color: c.s2 }, data: pc.rebuilt, z: 2 });
      }
    }
    if (L.dd) series.push({ type: 'line', name: wallet ? '回撤（TWR）' : '回撤（$）', xAxisIndex: 1, yAxisIndex: 1, showSymbol: false, lineStyle: { width: 1.5, color: c.neg }, itemStyle: { color: c.neg },
      areaStyle: { color: hexA(c.neg, 0.14) }, endLabel: end('回撤', wallet ? (v) => pct(v, 1) : (v) => usd(v), c.neg), data: dd });
    if (!top.length) return emptyChart(c, wallet ? '窗口内没有官方净值采样' : '没有官方永续盈亏采样');
    // End labels of nearby lines (equity vs net invested) push apart instead of overlapping.
    series.forEach((s) => { if (s.endLabel) s.labelLayout = { moveOverlap: 'shiftY' }; });
    const ddFmt = wallet ? (v) => pct(v, 0) : (v) => usd(v);
    return baseOption(c, { grid: grids, xAxis: [xa(0), xa(1)],
      yAxis: [Object.assign(valueAxis(c), { gridIndex: 0, scale: true }), Object.assign(valueAxis(c, ddFmt), { gridIndex: 1, max: 0, splitNumber: 2 })],
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      tooltip: Object.assign(baseOption(c).tooltip, { valueFormatter: undefined, formatter: (ps) => {
        const list = [].concat(ps).filter((p) => p.seriesType === 'line');
        if (!list.length) return '';
        return date(list[0].value[0]) + list.map((p) => `<br>${p.marker}${esc(p.seriesName)} <b>${/回撤（TWR）/.test(p.seriesName) ? pct(p.value[1], 1) : usd(p.value[1], 0)}</b>`).join('');
      } }),
      series });
  }

  function luckOption(c, L) {
    if (!L.enough && L.n < 2) return emptyChart(c, '收益日不足，无法计算');
    const sd = L.sd;
    const t0 = L.path[0][0];
    const band = L.path.map((p, i) => [p[0], Z80 * sd * Math.sqrt(i + 1)]);
    const lower = band.map((p) => [p[0], -p[1]]), width = band.map((p) => [p[0], 2 * p[1]]);
    const pctLog = (v) => pct(Math.exp(v) - 1, 0);
    return baseOption(c, { grid: { left: 52, right: 16, top: 30, bottom: 30 }, legend: Object.assign(legend(c), { data: ['账户（对数累计）', `去掉最好的 ${L.trimK} 天`, '随机 80% 区间'] }),
      xAxis: Object.assign(timeAxis(c), { min: t0 }), yAxis: valueAxis(c, pctLog),
      tooltip: Object.assign(baseOption(c).tooltip, { formatter: (ps) => date(ps[0].value[0]) + ps.filter((p) => p.seriesName !== '_lo').map((p) => `<br>${p.marker}${esc(p.seriesName)} <b>${p.seriesName === '随机 80% 区间' ? '±' + pct(Math.exp(p.value[1] / 2) - 1, 1) : pctLog(p.value[1])}</b>`).join('') }),
      series: [
        { type: 'line', name: '_lo', stack: 'band', data: lower, showSymbol: false, lineStyle: { opacity: 0 }, silent: true, tooltip: { show: false } },
        { type: 'line', name: '随机 80% 区间', stack: 'band', data: width, showSymbol: false, lineStyle: { opacity: 0 }, itemStyle: { color: c.muted }, areaStyle: { color: hexA(c.muted, 0.17) }, silent: true },
        { type: 'line', name: `去掉最好的 ${L.trimK} 天`, data: L.trimmed, showSymbol: false, lineStyle: { width: 1.4, type: 'dashed', color: c.ink2 }, itemStyle: { color: c.ink2 } },
        { type: 'line', name: '账户（对数累计）', data: L.path, showSymbol: false, lineStyle: { width: 1.8, color: c.s1 }, itemStyle: { color: c.s1 }, z: 4 },
      ] });
  }

  function windowSeg() {
    return `<div class="seg seg-sm" role="group" aria-label="时间窗口">${Object.keys(M.windows || {}).map((w) => `<button type="button" class="${w === state.window ? 'on' : ''}" data-w="${w}" aria-pressed="${w === state.window}">${esc(M.windows[w].label)}</button>`).join('')}</div>`;
  }
  function windowRange() {
    const d = days();
    return d.length ? date(d[0].time) + ' ~ ' + date(d[d.length - 1].time) : '无数据';
  }

  RENDER.overview = function (root) {
    const p = W().performance || {}, r = W().risk || {}, b = M.behavior || {}, a = M.activity || {}, cur = M.current || {};
    const rec = (M.quality || {}).reconciliation || {};
    const allPnl = num((M.official.windows.allTime || {}).pnl);
    const fl = collectFlags(), v = verdict(fl, allPnl), ev = evidence(), rk = riskLevel(v.n), L = luck();
    const winLabel = W().label || '';
    const wins = num(b.wins), evald = num(b.campaigns_evaluated) || 0;
    const posns = cur.positions || [];
    const sumOf = (k) => posns.reduce((s, x) => s + (num(x[k]) || 0), 0);
    const nl = cur.nearest_liquidation;
    const snapChip = cur.time ? scopeChip('snap', '快照 ' + date(cur.time, true) + ' UTC', '账户快照时刻的状态，不随时间窗口变化；离线报告不会实时更新') : scopeChip('snap', '无快照', '缓存中没有账户快照');
    const winChip = scopeChip('win', winLabel + ' · ' + windowRange(), '本章数字随顶部时间窗口切换');
    const allChip = scopeChip('win', '全部历史', '不随时间窗口变化');
    const eqSeries = days().map((x) => [x.time, x.equity]).filter((x) => isNum(x[1]));
    const pkPt = eqSeries.reduce((m, x) => (!m || x[1] > m[1] ? x : m), null);
    const lastPt = eqSeries[eqSeries.length - 1];
    const pf = tone('profit_factor', b.profit_factor);
    const eqs = reconEquations();
    const sc = (M.scorecard || {})[state.window] || [];
    root.innerHTML = `<div class="hero">
        ${verdictCard(fl, allPnl, { v, ev, rk, L })}
        <div class="spanel">
          <div class="sp-h"><h3>表现</h3>${windowSeg()}</div>
          <div class="hnums">
            ${heroNum('窗口盈亏', usd(p.pnl), (winLabel ? esc(winLabel) + ' · ' : '') + '官方 ' + usd(p.official_pnl) + ' · 全部历史 ' + usd(allPnl), 'hybrid', tone('total_pnl', p.pnl), { n: p.pnl, id: 'pnl', f: 'usd', to: 'overview#ch-02', why: WHY.pnl })}
            ${heroNum('时间加权收益 TWR', pct(p.twr), '年化 ' + pct(p.cagr) + ' · 串联 ' + int(p.return_days) + ' / ' + int(p.days) + ' 天', 'hybrid', isNum(num(p.cagr)) ? tone('cagr', p.cagr) : tone('twr', p.twr), { n: p.twr, id: 'twr', f: 'pct', to: 'performance', why: WHY.twr, k: 'twr' })}
            ${heroNum('回合胜率', pct(b.win_rate), isNum(wins) ? `赢 ${int(wins)} · 输或持平 ${int(evald - wins)}（共 ${int(evald)}）· 盈亏平衡 ${pct(b.break_even_win_rate, 0)}` : '', 'rebuilt', tone('win_rate', b.win_rate), { n: b.win_rate, id: 'wr', f: 'pct', to: 'overview#ch-04', why: WHY.win_rate, k: 'win_rate' })}
            ${heroNum('最大回撤', pct(r.max_drawdown), usd(r.max_drawdown_usd) + ' · 当前回撤 ' + pct(r.current_drawdown), 'hybrid', tone('max_dd', r.max_drawdown), { n: r.max_drawdown, id: 'mdd', f: 'pct', to: 'risk', why: WHY.max_dd, k: 'max_dd' })}
          </div>
          <div class="sp-chart"><div class="sp-cap">账户净值 · ${esc(winLabel)}</div>${sparkline(eqSeries)}</div>
        </div>
      </div>

      ${chapter('当前', cur.time ? (posns.length ? `净值 ${usd(cur.equity)} · ${int(posns.length)} 个仓位 · 杠杆 ${fix(cur.leverage)}×${nl ? ' · 最近强平 ' + esc(nl.coin) + ' ' + pct(nl.liq_distance) : ''}` : `净值 ${usd(cur.equity)} · 当前空仓`) : '没有账户快照', [
        blk(`<div class="gl-g">
          ${kpi('账户净值', usd(cur.equity ?? M.official.equity), cur.mode ? '账户模式 ' + esc(cur.mode) : '', 'official', null, null, { n: cur.equity ?? M.official.equity, why: WHY.equity, to: 'positions' })}
          ${kpi('未实现盈亏', cur.time ? usd(isNum(num(cur.unrealized_pnl)) ? num(cur.unrealized_pnl) : sumOf('unrealized_pnl')) : '—', posns.length ? int(posns.length) + ' 个仓位合计' : (cur.time ? '空仓，未实现盈亏为 0' : ''), 'official', null, null, cur.time ? null : { n: null, why: '没有账户快照' })}
          ${kpi('总敞口', usd(cur.gross_exposure), '多 ' + usd(cur.long_exposure) + ' · 空 ' + usd(cur.short_exposure), 'official', null, null, cur.time ? null : { n: null, why: '没有账户快照' })}
          ${kpi('占用保证金', cur.time ? usd(isNum(num(cur.margin_used)) ? num(cur.margin_used) : sumOf('margin_used')) : '—', '占净值 ' + pct(cur.margin_ratio) + ' · 可提 ' + usd(cur.withdrawable) + ' · 杠杆 ' + (isNum(cur.leverage) ? fix(cur.leverage) + '×' : '—'), 'official', null, tone('leverage_mean', cur.leverage), cur.time ? null : { n: null, why: '没有账户快照' })}
          ${kpi('最近强平距离', nl ? pct(nl.liq_distance) : '—', nl ? esc(nl.coin) : (posns.length ? '' : ''), 'official', null, nl ? tone('liq_distance', nl.liq_distance) : null, nl ? null : { n: null, why: cur.time ? (posns.length ? '持仓均为全仓且无强平价' : '空仓，没有强平价') : '没有账户快照' })}
        </div>`, { flush: true }),
        blk(bucketBars(cur) + stressHtml(cur), { title: '资产分布', sub: '账户总额 → 各资产桶；交易账户 / 关联账户分开标注', w: 'w4' }),
        blk('<div id="t-top" class="table-host"></div>', { title: '持仓', sub: '按名义价值前 20；红 = 距强平过近或杠杆过高', w: 'w8' }),
      ], { no: '01', id: 'ch-01', chip: snapChip })}

      ${chapter('业绩曲线', pkPt ? `峰值 ${usd(pkPt[1])}（${date(pkPt[0])}）· 当前 ${usd(lastPt[1])} · 最大回撤 ${pct(r.max_drawdown)}` : '窗口内没有净值数据', [
        blk(`<div class="curve-ctl"><div class="seg seg-sm" role="group" aria-label="分析口径" id="curve-scope">
            <button type="button" data-scope="wallet" class="${state.curve.scope === 'wallet' ? 'on' : ''}" aria-pressed="${state.curve.scope === 'wallet'}" title="官方 portfolio 净值，含现货与出入金影响">整个钱包</button>
            <button type="button" data-scope="perp" class="${state.curve.scope === 'perp' ? 'on' : ''}" aria-pressed="${state.curve.scope === 'perp'}" title="只看永续交易的累计盈亏：官方 vs 由成交重建">仅永续交易</button></div>
          <div class="layers" id="curve-layers" role="group" aria-label="图层">${[['equity', '净值 / 盈亏'], ['invested', '净投入'], ['dd', '回撤'], ['flows', '出入金']].map(([k, t]) => `<button type="button" class="lc${state.curve.layers[k] ? ' on' : ''}" data-layer="${k}" aria-pressed="${!!state.curve.layers[k]}"><i class="lc-${k}"></i>${t}</button>`).join('')}</div></div>
          ${chartDiv('c-curve', 'curve')}
          <div class="method">口径：上图为官方 portfolio 净值（日间按重建盈亏形状补点），下图为时间加权收益的水下回撤；“仅永续交易”切换为官方永续累计盈亏与重建值。净投入 = 期初净值 + 累计隐含资金流。</div>`),
      ], { no: '02', id: 'ch-02', chip: winChip, tools: windowSeg() })}

      ${chapter('能力还是运气', luckSentence(L), [
        blk(chartDiv('c-luck') + '<div class="method">方法：日收益取对数后累计；灰带是“期望为 0、波动率与本账户相同”的随机游走 80% 区间（±1.28·σ·√t）。虚线去掉最好的 5% 交易日，用来看收益是否依赖少数几天。</div>', { title: '累计收益 vs 随机区间', w: 'w8' }),
        blk(errorBar('日均收益（日序列）', L.mean, L.sd, L.n, (x) => pct(Math.exp(x) - 1, 2), '天')
          + errorBar('每回合期望（完整回合）', ...campStats(b), (x) => usd(x), '回合')
          + '<div class="method">点 = 估计值，横线 = 80% 置信区间，竖线 = 0。区间跨过 0 时，不能说明有稳定优势。</div>', { title: '优势的不确定性', w: 'w4' }),
      ], { no: '03', id: 'ch-03', chip: winChip })}

      ${chapter('交易画像', `${esc(((M.profile || {}).tags || {}).style || '—')} · 胜率 ${pct(b.win_rate)} · Profit factor ${fix(b.profit_factor)}${pf.level ? '（' + TONE[pf.level][2] + '）' : ''}`, [
        blk(profileTiles(b, a, r, cur) + `<div class="method">画像为规则标签：${Object.entries(((M.profile || {}).rules) || {}).map(([k, x]) => `${PROFILE_NAMES[k] || esc(k)}：${esc(x)}`).join('；')}</div>`),
        blk(recentStrip(b), { title: '最近 20 个完整回合', sub: '从左到右为时间顺序；蓝 = 盈利，红 = 亏损，灰 = 持平；柱高 = 盈亏大小', w: 'w6' }),
        blk(winRateBox(b), { title: '胜率要不要紧', sub: '严格胜率（不含持平）与盈亏平衡胜率对比', w: 'w6' }),
        blk(topTrades(b), { title: '最大 5 笔盈利 / 亏损', sub: '含资金费；去掉它们后的净盈亏见下方', w: 'w6' }),
        blk(capitalHtml(M.capital) + marketHtml(M.market) + evidenceTierHtml(M.evidence), { title: '资金、市场相关性与证据强度', w: 'w6' }),
      ], { no: '04', id: 'ch-04', chip: scopeChip('win', '可见成交 · 全部', '回合、成交类统计基于全部可见成交，不随时间窗口变化') })}

      ${chapter('风险与亮点', `${v.n.bad} 项危险 · ${v.n.warn} 项留意 · ${v.n.good} 项亮点`, [blk(flagsHtml(fl) + '<div class="method">按通用经验阈值自动判定，点击或悬停 ⓘ 查看阈值，不构成投资判断。</div>')], { no: '05', id: 'ch-05', chip: winChip })}

      ${chapter('对账', rec.status === 'ok' ? '重建盈亏与官方值对得上，过程指标可信' : rec.status ? '重建与官方存在偏差，过程类指标需谨慎' : '没有可对账的官方数据', [
        blk(eqs.html, { w: 'w6' }),
        blk(chartDiv('c-recon'), { title: '累计永续盈亏：官方 vs 重建', sub: '差值即残差，明细见“数据质量”', w: 'w6' }),
      ], { no: '06', id: 'ch-06', chip: allChip })}

      ${chapter('全部指标', `${sc.reduce((s, g) => s + g.items.length, 0)} 项，按主题分组`, sc.map((g) => blk(scoreTable(g), { title: esc(g.group), w: 'w6', flush: true })), { no: '07', id: 'ch-07', chip: winChip, fold: true, shut: true })}
      ${chapter('资金流水', `${int(M.tables.flows.count)} 条 · 净资本流入 ${usd((M.flows || {}).net_capital)}`, [blk('<div id="t-ov-flows" class="table-host"></div>', { flush: true })], { no: '08', id: 'ch-08', chip: allChip, fold: true, shut: true })}
      ${chapter('回合明细', `${int(M.tables.campaigns.count)} 条 · 完整 ${int(evald)} 个`, [blk('<div class="qfbar" id="qf-ov"></div><div id="t-ov-camp" class="table-host"></div>', { flush: true })], { no: '09', id: 'ch-09', chip: scopeChip('win', '可见成交 · 全部', ''), fold: true, shut: true })}`;

    const curve = chart(document.getElementById('c-curve'), curveOption);
    if (curve && state.curve.layers.flows && !state.curve.events) {
      load('flows').then((rows) => {
        // Only capital in/out of the wallet; the 60 largest keep the chart readable.
        state.curve.events = rows.filter((x) => (x.class === 'external' || x.class_label === '外部出入金') && isNum(num(x.amount_usd)) && num(x.amount_usd) !== 0)
          .sort((x, y) => Math.abs(y.amount_usd) - Math.abs(x.amount_usd)).slice(0, 60).map((x) => ({ time: x.time, amount: num(x.amount_usd) }));
        if (curve.inst) curve.inst.setOption(curveOption(C()), true);
      });
    }
    const redraw = () => { if (curve && curve.inst) curve.inst.setOption(curveOption(C()), true); };
    root.querySelector('#curve-scope').addEventListener('click', (e) => {
      const btn = e.target.closest('[data-scope]');
      if (!btn) return;
      state.curve.scope = btn.dataset.scope;
      root.querySelectorAll('#curve-scope button').forEach((x) => { x.classList.toggle('on', x === btn); x.setAttribute('aria-pressed', x === btn); });
      root.querySelectorAll('#curve-layers [data-layer="invested"], #curve-layers [data-layer="flows"]').forEach((x) => { x.disabled = state.curve.scope !== 'wallet'; });
      redraw();
    });
    root.querySelectorAll('#curve-layers [data-layer="invested"], #curve-layers [data-layer="flows"]').forEach((x) => { x.disabled = state.curve.scope !== 'wallet'; });
    root.querySelector('#curve-layers').addEventListener('click', (e) => {
      const btn = e.target.closest('[data-layer]');
      if (!btn || btn.disabled) return;
      const k = btn.dataset.layer;
      state.curve.layers[k] = !state.curve.layers[k];
      btn.classList.toggle('on', state.curve.layers[k]);
      btn.setAttribute('aria-pressed', state.curve.layers[k]);
      redraw();
    });
    chart(document.getElementById('c-luck'), (c) => luckOption(c, L));
    chart(document.getElementById('c-recon'), reconChart);
    eqs.fill();
    positionsTable('t-top', posns.slice(0, 20), 360);
    FOLD_OPEN['ch-08'] = () => flowsTable('t-ov-flows', 420);
    FOLD_OPEN['ch-09'] = () => campaignTable('t-ov-camp', 'qf-ov', 480);
    updateVbar(v, ev, rk);
  };

  // Sample mean / sd / n of closed-campaign PnL (from the scatter sample).
  function campStats(b) {
    const xs = (b.scatter || []).map((s) => num(s.pnl)).filter(isNum);
    const n = xs.length;
    if (n < 2) return [null, null, n];
    const m = xs.reduce((s, x) => s + x, 0) / n;
    return [m, Math.sqrt(xs.reduce((s, x) => s + (x - m) * (x - m), 0) / (n - 1)), n];
  }
  function positionsTable(id, rows, height) {
    const cur = M.current || {};
    table(id, rows, [
      col('币种', 'coin', { formatter: coinFmt }), col('DEX', 'dex'), col('方向', 'side'),
      numCol('数量', 'szi'), numCol('名义价值', 'notional', (v) => usd(v)), numCol('开仓价', 'entry_px'), numCol('标记价', 'mark_px'),
      signedCol('未实现盈亏', 'unrealized_pnl', (v) => usd(v)), toneCol('杠杆', 'leverage', 'position_leverage', (v) => fix(v, 0) + '×'), numCol('强平价', 'liquidation_px'),
      toneCol('距强平', 'liq_distance', 'liq_distance', (v) => pct(v)),
    ], { height: Math.min(height, 60 + rows.length * 34), empty: cur.time ? '当前空仓：没有未平仓位，未实现盈亏为 0，也没有强平价' : '未取到账户快照，无法显示当前持仓（报告仅基于缓存成交生成）' });
  }
  function flowsTable(id, height) {
    table(id, 'flows', [timeCol('时间', 'time'), col('分类', 'class_label'), col('类型', 'type'), col('币种', 'token'), numCol('数量', 'amount'),
      signedCol('美元金额', 'amount_usd', (v) => usd(v, 2)), numCol('费用', 'fee_usd'), { title: '估值', field: 'approx_value', formatter: yesFmt, hozAlign: 'center' }, col('对手方', 'counterparty'), col('金库', 'vault'), col('哈希', 'hash')],
    { height, empty: '没有资金流水：可能从未出入金，或流水接口未返回', cards: false });
  }
  function scoreTable(g) {
    return `<table class="table table-sm stat-table score-table mb-0"><tbody>${g.items.map((i) => {
      const t = tone(i.key, i.value), val = (UNIT_FMT[i.unit] || fix)(i.value);
      return `<tr class="${t.cls}"><td>${toneIcon(t)}${esc(i.label)}${infoBtn(i.key, t, val)}<div class="def">${esc(i.definition)}</div></td>
        <td class="num">${val}</td><td class="srccell"><span class="src">${SOURCE[i.source] || esc(i.source)}</span></td></tr>`;
    }).join('')}</tbody></table>`;
  }

  // Scorecard values by unit; the definition rides along as a hover title.
  const UNIT_FMT = {
    usd: (v) => usd(v), pct: (v) => pct(v, Math.abs(num(v)) >= 10 ? 0 : 2), bps: (v) => (isNum(num(v)) ? fix(v, 2) + ' bps' : '—'),
    x: (v) => (isNum(num(v)) ? fix(v, 2) + '×' : '—'), int: int, days: (v) => (isNum(num(v)) ? fix(v, 1) + ' 天' : '—'),
    hours: (v) => hours(num(v)), num: (v) => fix(v, 2),
  };
  RENDER.scorecard = function (root) {
    const groups = (M.scorecard || {})[state.window] || [];
    root.innerHTML = `<div class="row row-cards">
      <div class="col-12"><div class="small muted">窗口：${esc(W().label || '')}。来源：<b>官方</b> = portfolio 接口；<b>日序列</b> = 官方净值日序列；<b>成交</b> / <b>回合</b> = 可见成交（历史可能截断，见“数据质量”）；<b>K线</b> = MAE/MFE。“—”表示样本不足或不适用。</div>
        <div class="small muted mt-1">${toneLegend()}</div></div>
      ${groups.map((g) => card(esc(g.group), scoreTable(g), { col: 'col-xl-6', flush: true })).join('')}
    </div>`;
  };

  function toneLegend() {
    return LEVELS.map((l) => `<span class="flag ${TONE[l][0]}">${toneIcon({ icon: TONE[l][1] })}${TONE[l][2]}</span>`).join('')
      + ' 按通用经验阈值着色，不构成投资判断；点 ⓘ 查看定义、阈值与来源。';
  }

  // Flags for the overview: every rated scorecard item of the window plus a
  // few live-account checks, worst first.
  function collectFlags() {
    const flags = [];
    ((M.scorecard || {})[state.window] || []).forEach((g) => g.items.forEach((i) => {
      const t = tone(i.key, i.value);
      if (t.level) flags.push({ t, label: i.label, value: (UNIT_FMT[i.unit] || fix)(i.value) });
    }));
    const cur = M.current || {}, q = M.quality || {}, rec = q.reconciliation || {};
    const add = (key, raw, label, value) => { const t = tone(key, raw); if (t.level) flags.push({ t, label, value }); };
    if (cur.nearest_liquidation) add('liq_distance', cur.nearest_liquidation.liq_distance, '最近强平距离 ' + esc(cur.nearest_liquidation.coin), pct(cur.nearest_liquidation.liq_distance));
    const lev = Math.max(...(cur.positions || []).map((x) => num(x.leverage)).filter(isNum), -Infinity);
    if (isNum(lev)) add('position_leverage', lev, '最高持仓杠杆', fix(lev, 0) + '×');
    add('liquidations', (M.behavior || {}).liquidated_campaigns, '被强平回合', int((M.behavior || {}).liquidated_campaigns));
    add('recon_status', rec.status, '对账', ({ ok: '通过', warn: '有偏差', fail: '失败' })[rec.status] + (isNum(num(rec.residual_pct)) ? ' ' + pct(rec.residual_pct, 1) : ''));
    const trunc = (q.truncation || {}).status;
    if (trunc && trunc !== 'complete' && trunc !== 'no_gap_detected') {
      flags.push({ t: { level: 'warn', cls: TONE.warn[0], icon: TONE.warn[1], title: '回合类指标只覆盖可见成交，见“数据质量”' },
        label: '历史', value: trunc === 'api_visible_gaps' ? '中段有缺口' : '早期被截断' });
    }
    flags.sort((a, b) => LEVELS.indexOf(a.t.level) - LEVELS.indexOf(b.t.level));
    return flags;
  }
  function flagsHtml(flags) {
    flags = flags || collectFlags();
    const chip = (f) => `<span class="flag ${f.t.cls}"${toneAttr(f.t)}>${toneIcon(f.t)}${f.label} <b>${f.value}</b></span>`;
    const risks = flags.filter((f) => f.t.level !== 'good'), goods = flags.filter((f) => f.t.level === 'good');
    return `<div class="flag-row"><span class="flag-head">风险信号</span>${risks.map(chip).join('') || '<span class="muted small">未发现明显风险信号</span>'}</div>
      <div class="flag-row"><span class="flag-head">亮点</span>${goods.map(chip).join('') || '<span class="muted small">无</span>'}</div>`;
  }

  // One-line answer to "how is this wallet doing overall", derived only from
  // the same rule flags listed under it (no hidden scoring).
  function verdict(flags, allPnl) {
    const n = { bad: 0, warn: 0, good: 0 };
    flags.forEach((f) => { n[f.t.level]++; });
    const head = !isNum(allPnl) ? '账户' : allPnl >= 0 ? '盈利账户' : '亏损账户';
    const [word, cls] = n.bad >= 3 ? ['风险信号较多', 'v-bad'] : n.bad >= 1 ? ['存在风险信号', 'v-bad']
      : n.warn >= 3 ? ['需要留意', 'v-warn'] : ['整体稳健', 'v-good'];
    return { text: head + ' · ' + word, cls, n };
  }
  const PROFILE_NAMES = { style: '风格', bias: '方向', leverage: '杠杆', concentration: '集中度', execution: '执行', activity: '活跃度', size: '体量' };
  const ICON = {
    style: '<path d="M3 12h4l3-7 4 14 3-7h4"/>',
    bias: '<path d="M7 20V4M3 8l4-4 4 4M17 4v16M13 16l4 4 4-4"/>',
    leverage: '<path d="M4 16a8 8 0 1 1 16 0"/><path d="M12 16l4-5"/>',
    concentration: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3"/>',
    execution: '<path d="M13 3L5 14h6l-1 7 8-11h-6z"/>',
    activity: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
    size: '<path d="M5 20V12M10 20V8M15 20V4M20 20v-6"/>',
    copy: '<rect x="8" y="8" width="12" height="12" rx="2.5"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/>',
    check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    target: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="4"/><circle cx="12" cy="12" r=".8"/>',
    wallet: '<rect x="3" y="6" width="18" height="13" rx="2.5"/><path d="M16 12.5h2M3 9.5h18M6 6l9-3 1.5 3"/>',
    ext: '<path d="M14 4h6v6M20 4l-9 9M18 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4"/>',
    download: '<path d="M12 4v11M7 10.5l5 5 5-5M5 20h14"/>',
    print: '<path d="M7 9V4h10v5M7 17H5a1 1 0 0 1-1-1v-6a1 1 0 0 1 1-1h14a1 1 0 0 1 1 1v6a1 1 0 0 1-1 1h-2"/><rect x="7" y="14" width="10" height="6" rx="1"/>',
  };
  const svg = (name) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICON[name] || '<circle cx="12" cy="12" r="3"/>'}</svg>`;
  const shortAddr = (a) => (a && a.length > 14 ? a.slice(0, 6) + '…' + a.slice(-4) : a || '');
  let toastTimer = null;
  function toast(text, bad) {
    let el = document.getElementById('toast');
    if (!el) { el = document.createElement('div'); el.id = 'toast'; el.setAttribute('role', 'status'); el.setAttribute('aria-live', 'polite'); document.body.appendChild(el); }
    el.className = 'toast-msg show' + (bad ? ' bad' : '');
    el.textContent = text;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.className = 'toast-msg' + (bad ? ' bad' : ''); }, 1800);
  }
  // The report never links out (offline guarantee); the explorer URL is copied instead.
  const EXPLORER = 'https:' + '//app.hyperliquid.xyz/explorer/address/';
  function copyText(text, btn) {
    const done = () => {
      toast((btn && btn.dataset.copyMsg) || '已复制完整地址');
      if (!btn) return;
      const old = btn.innerHTML;
      btn.classList.add('ok'); btn.innerHTML = svg('check');
      setTimeout(() => { btn.classList.remove('ok'); btn.innerHTML = old; }, 1200);
    };
    const legacy = () => {
      const ta = document.createElement('textarea');
      ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta); ta.select();
      let ok = false;
      try { ok = document.execCommand('copy'); } catch (e) { /* ignore */ }
      ta.remove();
      if (ok) done(); else toast('复制失败，请手动选择地址复制', true);
    };
    if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done, legacy);
    else legacy();
  }
  const copyBtn = (text) => `<button type="button" class="icon-btn" data-copy="${esc(text)}" title="复制完整地址" aria-label="复制完整地址">${svg('copy')}</button>`;

  // Identity + actions, persona with factual tags, and a two-axis verdict:
  // evidence strength (can the numbers be trusted) and risk (rule flags).
  function verdictCard(flags, allPnl, x) {
    const prof = M.profile || {}, tags = prof.tags || {}, rules = prof.rules || {};
    const v = x.v, ev = x.ev, rk = x.rk, b = M.behavior || {}, a = M.activity || {}, cur = M.current || {};
    const persona = ['style', 'bias'].map((k) => tags[k]).filter(Boolean).map(esc).join(' · ');
    const rest = Object.keys(tags).filter((k) => k !== 'style' && k !== 'bias');
    const fact = (icon, k, val, tip) => `<li title="${esc(tip)}">${svg(icon)}<span class="k">${k}</span>${val}</li>`;
    const tagHtml = rest.map((k) => fact(k, PROFILE_NAMES[k] || esc(k), esc(tags[k]), (PROFILE_NAMES[k] || k) + '：' + (rules[k] || ''))).join('')
      + fact('leverage', '平均杠杆', isNum(num((M.risk || {}).leverage_mean)) ? fix(M.risk.leverage_mean, 1) + '×' : '—', '重建：永续名义 ÷ 净值的日均值')
      + fact('wallet', '账户价值', usd(cur.equity ?? M.official.equity), '官方净值')
      + fact('activity', '持仓中位', hours(b.median_hold_hours), '完整回合的持仓时长中位数')
      + fact('size', '活跃天数', int(a.active_days), '有成交的天数');
    const r = Object.keys(rules).map((k) => `<li>${PROFILE_NAMES[k] || esc(k)}：${esc(rules[k])}</li>`).join('');
    const exp = ['operations', 'campaigns', 'flows', 'funding'].filter((k) => M.tables[k]).map((k) => `<a href="${esc(M.tables[k].csv)}" download>${TABLE_NAMES[k]} CSV <small>${int(M.tables[k].count)} 条</small></a>`).join('')
      + '<a href="exports/model.json" download>全部指标 model.json</a>';
    const evTip = ev.rules.map((x) => (x.ok ? '✓ ' : '✕ ') + x.label).join('\n');
    return `<div class="vcard">
      <div>
        <div class="id-row"><span class="id-addr" title="${esc(M.wallet)}">${esc(shortAddr(M.wallet))}</span>
          <span class="id-act">${copyBtn(M.wallet)}
          <button type="button" class="icon-btn" data-copy="${esc(EXPLORER + M.wallet)}" data-copy-msg="已复制区块浏览器链接（需联网打开）" title="复制 Hyperliquid 浏览器链接（报告本身不联网）" aria-label="复制区块浏览器链接">${svg('ext')}</button>
          <details class="menu"><summary class="icon-btn" title="导出数据" aria-label="导出数据">${svg('download')}</summary><div class="menu-pop">${exp}</div></details>
          <button type="button" class="icon-btn" data-print title="打印 / 存为 PDF" aria-label="打印">${svg('print')}</button></span></div>
        <div class="persona">${persona || '钱包画像数据不足'}</div>
      </div>
      <ul class="ttags">${tagHtml}</ul>
      <div class="vc-lv">
        <div class="lb">综合判定${W().label ? ' · ' + esc(W().label) : ''}</div>
        <b class="${v.cls}">${v.text}</b>
        <div class="vc-axes">
          <div class="vc-ax" title="${esc(evTip)}"><span>证据强度</span>${meter(ev.level, 4, 'ev', '证据强度 ' + ev.word + '：' + ev.level + ' / 4')}<b>${ev.word}</b></div>
          <div class="vc-ax"><span>风险</span>${meter(rk.level, 4, 'rk', '风险 ' + rk.word)}<b>${rk.word}</b></div>
        </div>
        <div class="vc-chips">
          <span class="vc-chip" data-to="overview#ch-05" role="link" tabindex="0"><span class="dot bad"></span>${v.n.bad} 项危险</span>
          <span class="vc-chip" data-to="overview#ch-05" role="link" tabindex="0"><span class="dot warn"></span>${v.n.warn} 项留意</span>
          <span class="vc-chip" data-to="overview#ch-05" role="link" tabindex="0"><span class="dot good"></span>${v.n.good} 项亮点</span>
        </div>
        ${ev.next ? `<div class="vc-next">${svg('target')}<span><b>下一步：</b>${esc(ev.next.next)}</span></div>` : ''}
      </div>
      <div class="vc-note">风险来自“05 风险与亮点”的规则计数；证据强度 = 对账通过、历史完整、完整回合 ≥ 30、日序列 ≥ 90 天，满足几条记几格。不构成投资判断。${r ? `<details class="rules mt-1"><summary>画像判定规则</summary><ul class="small muted mt-2 mb-0">${r}</ul></details>` : ''}</div>
    </div>`;
  }

  // Sticky summary once the hero has scrolled away.
  function updateVbar(v, ev, rk) {
    const bar = document.getElementById('vbar');
    if (!bar) return;
    bar.innerHTML = `<span class="vb-a">${esc(shortAddr(M.wallet))}</span><b class="${v.cls}">${esc(v.text)}</b>
      <span class="vb-m">证据 ${meter(ev.level, 4, 'ev', '证据强度 ' + ev.word)} 风险 ${meter(rk.level, 4, 'rk', '风险 ' + rk.word)}</span>
      <span class="vb-c"><span class="dot bad"></span>${v.n.bad}<span class="dot warn"></span>${v.n.warn}<span class="dot good"></span>${v.n.good}</span>
      <span class="vb-w">${esc(W().label || '')}</span><button type="button" class="btn btn-sm btn-quiet" data-top>回顶部</button>`;
    const hero = document.querySelector('#p-overview .hero');
    if (state.vbarIO) state.vbarIO.disconnect();
    if (!hero || !window.IntersectionObserver) return;
    state.vbarIO = new IntersectionObserver((es) => { bar.classList.toggle('show', state.page === 'overview' && !es[0].isIntersecting); }, { rootMargin: '-56px 0px 0px 0px' });
    state.vbarIO.observe(hero);
  }

  // Coin monogram + name; colour is a stable hash of the symbol (identity only).
  function coinBadge(coin) {
    if (!coin) return '';
    const s = String(coin), base = s.replace(/^.*:/, '').replace(/^@/, '');
    let h = 0;
    for (let i = 0; i < base.length; i++) h = (h * 31 + base.charCodeAt(i)) >>> 0;
    return `<span class="coin"><i class="cb c${h % 6}">${esc(base.slice(0, 1).toUpperCase())}</i>${esc(s)}</span>`;
  }
  const coinFmt = (cell) => coinBadge(cell.getValue());

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
        ['盈亏', usd(p.pnl), '官方 ' + usd(p.official_pnl), tone('total_pnl', p.pnl)], ['TWR', pct(p.twr), undefined, tone('twr', p.twr)],
        ['年化 CAGR', pct(p.cagr), '≥30 天才计算', tone('cagr', p.cagr)],
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
    table('t-month', (all.monthly || []).slice().reverse(), [col('月份', 'period'), signedCol('盈亏', 'pnl', (v) => usd(v, 0)), signedCol('收益率', 'ret', (v) => pct(v, 2))], { height: 320 });
  };

  function windowTable() {
    const ws = M.windows || {};
    const keys = Object.keys(ws);
    // [label, format, rating key, raw value]
    const rows = [['盈亏', (w) => usd(w.performance.pnl), 'total_pnl', (w) => w.performance.pnl], ['官方盈亏', (w) => usd(w.performance.official_pnl)],
      ['TWR', (w) => pct(w.performance.twr), 'twr', (w) => w.performance.twr],
      ['最大回撤', (w) => pct(w.risk.max_drawdown), 'max_dd', (w) => w.risk.max_drawdown], ['波动率（年化）', (w) => pct(w.risk.volatility), 'volatility', (w) => w.risk.volatility],
      ['Sharpe', (w) => fix(w.risk.sharpe), 'sharpe', (w) => w.risk.sharpe], ['Sortino', (w) => fix(w.risk.sortino), 'sortino', (w) => w.risk.sortino],
      ['VaR 95（日）', (w) => pct(w.risk.var95, 2), 'var95', (w) => w.risk.var95], ['官方成交额', (w) => usd(w.official_volume)]];
    const cell = (r, w) => { const t = r[2] ? tone(r[2], r[3](w)) : null; return `<td class="num ${t && t.level ? 'cell-' + t.cls : ''}"${t && t.level ? toneAttr(t) : ''}>${r[1](w)}</td>`; };
    return `<div class="table-responsive"><table class="table table-sm stat-table mb-0"><thead><tr><th></th>${keys.map((k) => `<th class="num">${esc(ws[k].label)}</th>`).join('')}</tr></thead><tbody>
      ${rows.map((r) => `<tr><td>${r[0]}</td>${keys.map((k) => cell(r, ws[k])).join('')}</tr>`).join('')}</tbody></table></div>`;
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
      <div class="col-12"><div class="gl-g">
      ${kpi('最大回撤', pct(r.max_drawdown), '当前回撤 ' + pct(r.current_drawdown), 'hybrid', null, tone('max_dd', r.max_drawdown))}
      ${kpi('年化波动率', pct(r.volatility), 'VaR95 ' + pct(r.var95, 2), 'hybrid', null, tone('volatility', r.volatility))}
      ${kpi('Sharpe / Sortino', fix(r.sharpe) + ' / ' + fix(r.sortino), 'Calmar ' + fix(r.calmar), 'hybrid', null, tone('sharpe', r.sharpe))}
      ${kpi('Omega / 尾部比率', fix(r.omega) + ' / ' + fix(r.tail_ratio), '最长水下 ' + fix((r.drawdown_stats || {}).longest_underwater_days, 0) + ' 天', 'hybrid', null, tone('omega', r.omega))}
      ${kpi('有效杠杆均值', fix(all.leverage_mean) + '×', 'p95 ' + fix(all.leverage_p95) + '× · 最高 ' + fix(all.leverage_max) + '×', 'rebuilt', null, tone('leverage_mean', all.leverage_mean))}
      ${kpi('持仓集中度 HHI', fix(cur.concentration_hhi), '1 = 单一持仓', 'official')}
      </div></div>
      ${card('水下回撤（TWR）', chartDiv('c-uw'), { col: 'col-lg-8' })}
      ${card('风险统计', statTable([
        ['CVaR 95（日）', pct(r.cvar95, 2), undefined, tone('cvar95', r.cvar95)], ['恢复因子', fix(r.recovery_factor), '净盈亏 ÷ 最大回撤金额', tone('recovery_factor', r.recovery_factor)],
        ['回撤次数（≥5%）', int((r.drawdown_stats || {}).count), '平均深度 ' + pct((r.drawdown_stats || {}).avg_depth, 1), tone('avg_dd_depth', (r.drawdown_stats || {}).avg_depth)],
        ['水下 / 新高时间占比', pct((r.drawdown_stats || {}).underwater_ratio, 0) + ' / ' + pct((r.drawdown_stats || {}).new_high_ratio, 0), undefined, tone('underwater_ratio', (r.drawdown_stats || {}).underwater_ratio)],
        ['$ 最大回撤（累计盈亏）', usd(r.max_drawdown_usd)], ['被强平回合', int((M.behavior || {}).liquidated_campaigns), undefined, tone('liquidations', (M.behavior || {}).liquidated_campaigns)],
        ['强平成交额', usd((M.activity || {}).liquidation_volume)], ['ADL 成交', int((M.activity || {}).adl_fills)],
        ['多空净敞口', usd(cur.net_exposure)], ['最近强平距离', cur.nearest_liquidation ? esc(cur.nearest_liquidation.coin) + ' ' + pct(cur.nearest_liquidation.liq_distance) : '—', undefined,
          cur.nearest_liquidation ? tone('liq_distance', cur.nearest_liquidation.liq_distance) : null],
      ]), { col: 'col-lg-4', flush: true })}
      ${card('有效杠杆（永续名义 / 净值）', chartDiv('c-lev', 'short'), { col: 'col-lg-6', sub: '重建' })}
      ${card('多空敞口', chartDiv('c-ls', 'short'), { col: 'col-lg-6', sub: '重建，按日收盘' })}
      ${card('最深回撤区间', '<div id="t-dd" class="table-host"></div>', { flush: true })}
      <div class="col-12">${chapter('更多统计', `Ulcer ${fix(r.ulcer_index)} · 偏度 ${fix(r.skew)} · 超额峰度 ${fix(r.excess_kurtosis)} · 滚动 30 日波动率与 Sharpe`, [
          blk(statTable([['Ulcer 指数', fix(r.ulcer_index), '回撤深度的均方根'], ['偏度', fix(r.skew)], ['超额峰度', fix(r.excess_kurtosis)],
            ['最大回撤时长（峰→谷）', fix((r.drawdown_stats || {}).max_drawdown_duration_days, 0) + ' 天'], ['最大恢复时长（谷→新高）', fix((r.drawdown_stats || {}).max_recovery_days, 0) + ' 天']]), { title: '分布形态', w: 'w4', flush: true }),
          blk(chartDiv('c-vol', 'short'), { title: '滚动 30 日波动率（年化）', w: 'w4' }),
          blk(chartDiv('c-sh', 'short'), { title: '滚动 30 日 Sharpe', w: 'w4' }),
        ], { id: 'more-risk', fold: true, shut: true })}</div>
    </div>`;
    const inw = (rows, key) => (rows || []).filter((x) => inWin(x.time)).map((x) => [x.time, x[key]]);
    chart(document.getElementById('c-uw'), (c) => line(c, [{ name: '回撤', data: inw(all.underwater, 'dd'), area: true }], (v) => pct(v, 1)));
    // Folded charts have no size until opened; build them on first open.
    FOLD_OPEN['more-risk'] = () => {
      chart(document.getElementById('c-vol'), (c) => line(c, [{ name: '波动率', data: inw(all.rolling_30d, 'vol') }], (v) => pct(v, 0)));
      chart(document.getElementById('c-sh'), (c) => line(c, [{ name: 'Sharpe', data: inw(all.rolling_30d, 'sharpe') }], (v) => fix(v, 1)));
    };
    chart(document.getElementById('c-lev'), (c) => line(c, [{ name: '杠杆', data: inw(all.leverage_series, 'leverage') }], (v) => fix(v, 1) + '×'));
    chart(document.getElementById('c-ls'), (c) => line(c, [{ name: '多头', data: inw(all.leverage_series, 'long') }, { name: '空头', data: inw(all.leverage_series, 'short') }], (v) => usd(v)));
    table('t-dd', (all.drawdown_episodes || []), [timeCol('峰值', 'peak_ms'), timeCol('谷底', 'trough_ms'), timeCol('恢复', 'recovered_ms'),
      numCol('深度', 'depth', (v) => pct(v, 1)), numCol('持续天数', 'duration_days', (v) => fix(v, 0)), numCol('恢复天数', 'recovery_days', (v) => fix(v, 0))], { height: 360 });
  };

  RENDER.trading = function (root) {
    const b = M.behavior || {}, a = M.activity || {};
    root.innerHTML = `<div class="row row-cards">
      <div class="col-12"><div class="gl-g">
      ${kpi('完整回合', int(b.campaigns_evaluated), '排除不完整 ' + int(b.campaigns_excluded), 'rebuilt')}
      ${kpi('胜率（含保本 / 严格）', pct(b.win_rate) + ' / ' + pct(b.win_rate_strict), '连胜 ' + int(b.max_win_streak) + ' · 连亏 ' + int(b.max_loss_streak), 'rebuilt', null, tone('win_rate', b.win_rate))}
      ${kpi('平均盈利 / 亏损', usd(b.avg_win) + ' / ' + usd(b.avg_loss), '每回合期望 ' + usd(b.expectancy), 'rebuilt', null, tone('expectancy', b.expectancy))}
      ${kpi('持仓中位时长', hours(b.median_hold_hours), '', 'rebuilt')}
      ${kpi('Maker 占比', pct(a.maker_share), 'TWAP ' + pct(a.twap_share), 'rebuilt', null, tone('maker_share', a.maker_share))}
      ${kpi('活跃天数', int(a.active_days), '日均 ' + fix(a.fills_per_active_day, 0) + ' 笔成交', 'rebuilt')}
      </div></div>
      ${card('持仓时长 × 回合盈亏', chartDiv('c-scatter', 'tall'), { col: 'col-lg-8', sub: '横轴对数刻度；含资金费；按平仓日归入窗口' })}
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
      ${card('仓位回合', '<div class="qfbar" id="qf-camp"></div><div id="t-camp" class="table-host"></div>', { flush: true, sub: M.tables.campaigns.count + ' 条 · 快捷筛选与表头筛选可叠加' })}
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
    table('t-bycoin', ((M.windows || {}).all || {}).by_coin || [], [col('币种', 'coin', { formatter: coinFmt }), col('类型', 'market_type', { width: 70 }),
      signedCol('净额', 'net', (v) => usd(v)), signedCol('已实现', 'realized', (v) => usd(v)), numCol('手续费', 'fees', (v) => usd(v)),
      signedCol('资金费', 'funding', (v) => usd(v)), numCol('成交额', 'volume', (v) => usd(v)), numCol('成交笔数', 'fills', int),
      numCol('完整回合', 'campaigns', int), numCol('胜率', 'win_rate', (v) => pct(v, 0)), signedCol('做多盈亏', 'long_pnl', (v) => usd(v)), signedCol('做空盈亏', 'short_pnl', (v) => usd(v))],
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
    table('t-coins', (a.top_coins || []), [col('币种', 'coin', { formatter: coinFmt }), numCol('成交额', 'volume', (v) => usd(v)), numCol('成交笔数', 'fills', int),
      signedCol('已实现盈亏', 'realized', (v) => usd(v)), numCol('手续费', 'fees', (v) => usd(v))], { height: 420 });
    campaignTable('t-camp', 'qf-camp', 480);
  };

  function excursionNote(ex) {
    const rc = ex.resolution_counts || {};
    return '精度：' + ['15m', '1h', '4h', '1d'].map((k) => k + ' ' + int(rc[k])).join(' · ') + '；小点 = 精度不足（持仓短于所用 K 线）';
  }

  RENDER.positions = function (root) {
    const cur = M.current || {}, st = cur.staking || {};
    root.innerHTML = `<div class="row row-cards">
      <div class="col-12"><div class="gl-g">
      ${kpi('总敞口', usd(cur.gross_exposure), '多 ' + usd(cur.long_exposure) + ' · 空 ' + usd(cur.short_exposure), 'official')}
      ${kpi('质押 HYPE', fix(num(st.delegated), 2), usd(st.value_usd), 'official')}
      ${kpi('金库', int((cur.vaults || []).length) + ' 个', usd((cur.vaults || []).reduce((s, v) => s + (num(v.equity) || 0), 0)), 'official')}
      ${kpi('子账户', int((cur.sub_accounts || []).length) + ' 个', usd((cur.sub_accounts || []).reduce((s, v) => s + (num(v.account_value) || 0), 0)), 'official')}
      ${kpi('挂单', cur.open_orders === null || cur.open_orders === undefined ? '未知' : int(cur.open_orders), cur.open_orders === null ? '挂单接口失败' : '', 'official')}
      ${kpi('快照时间', date(cur.time, true), 'UTC', 'official')}
      </div></div>
      ${card('当前持仓', '<div id="t-pos" class="table-host"></div>', { flush: true })}
      ${card('各账户桶价值', chartDiv('c-bk', 'short'), { col: 'col-lg-6' })}
      ${card('现货余额', '<div id="t-spot" class="table-host"></div>', { col: 'col-lg-6', flush: true })}
      ${card('子账户', '<div id="t-sub" class="table-host"></div>', { col: 'col-lg-6', flush: true })}
      ${card('金库', '<div id="t-vault" class="table-host"></div>', { col: 'col-lg-6', flush: true })}
    </div>`;
    table('t-pos', (cur.positions || []).map((p) => Object.assign({}, p, { funding_income_since_open: isNum(num(p.cum_funding_since_open)) ? -num(p.cum_funding_since_open) : null })), [col('币种', 'coin', { formatter: coinFmt }), col('DEX', 'dex'), col('方向', 'side'), numCol('数量', 'szi'), numCol('名义价值', 'notional', (v) => usd(v)),
      numCol('开仓价', 'entry_px'), numCol('标记价', 'mark_px'), signedCol('未实现盈亏', 'unrealized_pnl', (v) => usd(v)), signedCol('ROE', 'roe', (v) => pct(v)),
      col('杠杆类型', 'leverage_type'), toneCol('杠杆', 'leverage', 'position_leverage', (v) => fix(v, 0) + '×'), numCol('强平价', 'liquidation_px'),
      toneCol('距强平', 'liq_distance', 'liq_distance', (v) => pct(v)),
      numCol('占用保证金', 'margin_used', (v) => usd(v)), signedCol('开仓以来资金费（正=收入）', 'funding_income_since_open', (v) => usd(v))], { height: 360 });
    chart(document.getElementById('c-bk'), (c) => {
      const rows = (cur.buckets || []).slice().sort((x, y) => y.value - x.value);
      return baseOption(c, { grid: { left: 110, right: 40, top: 10, bottom: 30 }, tooltip: { trigger: 'item', valueFormatter: (v) => usd(v, 0) }, xAxis: valueAxis(c),
        yAxis: { type: 'category', inverse: true, data: rows.map((r) => r.bucket), axisLabel: { color: c.ink2 }, axisLine: { lineStyle: { color: c.axis } }, axisTick: { show: false } },
        series: [{ type: 'bar', barMaxWidth: 14, data: rows.map((r) => ({ value: r.value, itemStyle: { color: c.s1, borderRadius: [0, 2, 2, 0] } })) }] });
    });
    const spotTotal = (cur.spot || []).reduce((s, x) => s + Math.max(0, num(x.value) || 0), 0);
    const spotRows = (cur.spot || []).map((x) => Object.assign({}, x, { share: spotTotal > 0 ? Math.max(0, num(x.value) || 0) / spotTotal : null }));
    table('t-spot', spotRows, [col('币种', 'coin', { formatter: coinFmt }), numCol('价值', 'value', (v) => usd(v)),
      numCol('占比', 'share', null, { formatter: (cell) => { const v = num(cell.getValue()); return isNum(v) ? `<span class="share"><span class="bar"><i style="width:${(v * 100).toFixed(1)}%"></i></span>${pct(v, 1)}</span>` : ''; }, width: 150 }),
      numCol('数量', 'total'), numCol('冻结', 'hold'), numCol('价格', 'price'),
      numCol('成本', 'entry_ntl', (v) => usd(v)), signedCol('浮盈', 'unrealized_pnl', (v) => usd(v))],
    { height: 300, empty: cur.time ? '快照中没有现货余额' : '未取到账户快照', tabulator: { initialSort: [{ column: 'value', dir: 'desc' }] } });
    // Sub-accounts analyzed with --include-subaccounts carry their own official figures.
    const subInfo = {};
    (M.sub_account_summary || []).forEach((s) => { subInfo[s.address] = s; });
    const subRows = (cur.sub_accounts || []).map((s) => Object.assign({}, s, subInfo[s.address] || {}));
    const subCols = [col('名称', 'name'), col('地址', 'address'), numCol('永续净值', 'account_value', (v) => usd(v)), numCol('持仓数', 'positions', int)];
    if ((M.sub_account_summary || []).length) {
      subCols.push(signedCol('官方累计盈亏', 'official_pnl', (v) => usd(v)), toneCol('最大回撤', 'max_drawdown', 'max_dd', (v) => pct(v, 1)),
        col('对账', 'reconciliation'), col('历史', 'history'));
    }
    table('t-sub', subRows, subCols, { height: 240 });
    table('t-vault', cur.vaults || [], [col('金库', 'vault'), numCol('权益', 'equity', (v) => usd(v)), timeCol('锁定至', 'locked_until_ms')], { height: 240 });
  };

  RENDER.costs = function (root) {
    const k = M.costs || {};
    root.innerHTML = `<div class="row row-cards">
      <div class="col-12"><div class="gl-g">
      ${kpi('手续费合计', usd(k.fees), pct(k.fee_bps / 1e4, 3) + ' 成交额（' + fix(k.fee_bps, 2) + ' bps）', 'rebuilt')}
      ${kpi('资金费净额', usd(k.funding_net), '收 ' + usd(k.funding_received) + ' · 付 ' + usd(k.funding_paid), 'rebuilt')}
      ${kpi('Builder 费', usd(k.builder_fees), '已含于手续费，单列', 'rebuilt')}
      ${kpi('手续费 / 成交毛利', pct(k.fee_drag), '成本 / 毛利 ' + pct(k.cost_to_gross) + '（含净付资金费）', 'rebuilt', null, tone('fee_drag', k.fee_drag))}
      ${kpi('费率档', k.taker_rate !== null ? pct(k.taker_rate, 3) + ' / ' + pct(k.maker_rate, 3) : '—', 'taker / maker', 'official')}
      ${kpi('折扣', '推荐 ' + pct(k.referral_discount, 0) + ' · 质押 ' + pct(k.staking_discount, 0), '', 'official')}
      </div></div>
      ${card('累计手续费与资金费', chartDiv('c-cost'), { col: 'col-lg-7', sub: '按费用发生日归入窗口（回合类指标按平仓日归入）' })}
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
    table('t-funding', 'funding', [timeCol('时间', 'time'), col('币种', 'coin'), signedCol('金额', 'usdc', (v) => (v === null ? '' : Number(v).toLocaleString('en-US', { maximumFractionDigits: 6 }))), numCol('持仓', 'szi'), numCol('费率', 'fundingRate'), col('计价', 'pnl_token')]);
  };

  RENDER.flows = function (root) {
    const f = M.flows || {};
    const byClass = Object.entries(f.by_class || {});
    root.innerHTML = `<div class="row row-cards">
      <div class="col-12"><div class="gl-g">
      ${kpi('流水净资本流入', usd(f.net_capital), '外部 + 子账户 + 金库 + 质押', 'official')}
      ${byClass.slice(0, 5).map(([k, v]) => kpi(esc(k), usd(v), '', 'official')).join('')}
      </div></div>
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
    const truncText = { complete: '未发现缺口', no_gap_detected: '未发现缺口（不等于证明完整）', api_visible_gaps: '历史中段有缺口', api_visible_truncated: '早期历史被截断' }[t.status] || t.status;
    root.innerHTML = `<div class="row row-cards">
      <div class="col-12"><div class="gl-g">
      ${kpi('对账状态', statusBadge(rec.status), (rec.reasons || []).join(', ') || '&nbsp;', 'hybrid', null, tone('recon_status', rec.status))}
      ${kpi('对账残差', usd(rec.residual), pct(rec.residual_pct, 2) + ' 官方', 'hybrid', null, tone('recon_status', rec.status))}
      ${kpi('周度区间通过', int((rec.interval_status || {}).ok) + ' / ' + int(((rec.interval_status || {}).ok || 0) + ((rec.interval_status || {}).warn || 0) + ((rec.interval_status || {}).fail || 0)), '偏差 ' + int((rec.interval_status || {}).warn) + ' · 失败 ' + int((rec.interval_status || {}).fail), 'hybrid')}
      ${kpi('成交额覆盖率', pct(t.volume_coverage, 1), '含补录缺口 ' + pct(q.volume_coverage_with_imputed, 1), 'hybrid', null, tone('volume_coverage', t.volume_coverage))}
      ${kpi('仓位断点', int(q.position_break_count), '快照不一致 ' + int((q.snapshot_mismatch || []).length), 'rebuilt', null, tone('position_breaks', q.position_break_count))}
      ${kpi('资产组合残差', pct(num(comp.residual_pct) / 100, 2), '快照合计 vs 官方净值，差 ' + usd(comp.residual), 'official')}
      </div></div>
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
      numCol('重建', 'reconstructed', (v) => usd(v, 0)), signedCol('残差', 'residual', (v) => usd(v, 0)), col('状态', 'status', { formatter: (cell) => statusBadge(cell.getValue(), (STATUS[cell.getValue()] || STATUS.no_data)[2]) })], { height: 380 });
    table('t-brk', q.position_breaks || [], [timeCol('时间', 'time'), col('币种', 'coin'), numCol('应为', 'expected'), numCol('实际', 'observed'), numCol('差额', 'delta'), numCol('下一笔成交价', 'px'), col('类型', 'kind')], { height: 380 });
  };

  RENDER.raw = function (root) {
    root.innerHTML = `<div class="row row-cards">
      ${card('全部成交', '<div id="t-ops" class="table-host"></div>', { flush: true, sub: M.tables.operations.count + ' 条 · 表头可筛选，CSV 在 exports/',
        actions: `<div class="card-actions"><a class="btn btn-sm btn-pri" href="${M.tables.operations.csv}" download>下载 CSV</a></div>` })}
    </div>`;
    table('t-ops', 'operations', [timeCol('时间', 'time'), col('币种', 'coin'), col('动作', 'action_label'), col('方向', 'direction_label'), col('买卖', 'side', { width: 60 }),
      numCol('价格', 'price'), numCol('数量', 'size'), numCol('名义', 'notional', (v) => usd(v)), numCol('成交前仓位', 'start_position'), numCol('成交后仓位', 'end_position'),
      signedCol('已实现盈亏', 'closed_pnl', (v) => usd(v, 2)), numCol('手续费', 'fee'), col('费币', 'fee_token'),
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
    // The overview carries its own window control next to the numbers it changes.
    document.getElementById('filters').classList.toggle('hidden', !info[2] || page === 'overview');
    const sec = document.getElementById('p-' + page);
    hideTip();
    if (page !== 'overview') { const vb = document.getElementById('vbar'); if (vb) vb.classList.remove('show'); }
    if (!sec.dataset.window || (info[2] && sec.dataset.window !== state.window)) renderPage(page);
    else rerender();
    queueFit();
  }
  // Page-level heading (overview has its own hero instead).
  const HEADS = {
    scorecard: '约 70 项指标按主题分组，跟随时间窗口切换；每项注明定义与来源',
    performance: '收益、月度表现与分币种盈亏',
    risk: '回撤、波动、尾部风险与杠杆',
    trading: '回合质量、持仓时长、交易时段与盈利集中度',
    positions: '当前持仓、现货、子账户与金库（快照）',
    costs: '手续费、资金费与费率档',
    flows: '外部出入金、子账户与金库划转',
    quality: '官方 vs 重建对账、历史完整性与数据标记',
    raw: '全部成交明细，表头可筛选',
  };
  function renderPage(page) {
    const sec = document.getElementById('p-' + page);
    state.charts = state.charts.filter((e) => { if (sec.contains(e.el)) { disposeChart(e); return false; } return true; });
    Object.keys(state.tables).forEach((id) => { if (sec.querySelector('#' + id)) { state.tables[id].destroy(); delete state.tables[id]; } });
    // Lazy fold callbacks belong to the old DOM.
    Object.keys(FOLD_OPEN).forEach((k) => { if (sec.querySelector('#' + k)) delete FOLD_OPEN[k]; });
    const first = !sec.dataset.window;
    sec.dataset.window = state.window;
    hideTip();
    RENDER[page](sec);
    if (HEADS[page]) {
      const info = PAGES.find((p) => p[0] === page);
      sec.insertAdjacentHTML('afterbegin', `<div class="page-head"><h2>${info[1]}</h2><p>${HEADS[page]}${info[2] && W().label ? ' · 当前窗口 ' + esc(W().label) : ''}</p></div>`);
    }
    // First visit: short fade-up and staggered tags; window switches only tween numbers.
    if (first && motionOK()) {
      sec.classList.remove('enter'); void sec.offsetWidth; sec.classList.add('enter');
      sec.querySelectorAll('.ttags li').forEach((li, i) => { li.style.animationDelay = Math.min(200, i * 28) + 'ms'; li.classList.add('pop'); });
    }
    syncWindowButtons();
    queueFit();
    animateNumbers(sec);
  }
  function syncWindowButtons() {
    document.querySelectorAll('[data-w]').forEach((b) => { b.classList.toggle('on', b.dataset.w === state.window); b.setAttribute('aria-pressed', b.dataset.w === state.window); });
  }
  function setWindow(w) {
    if (!w || w === state.window || !(M.windows || {})[w]) return;
    state.window = w;
    syncWindowButtons();
    renderPage(state.page);
  }
  // Navigate to "page" or "page#anchor", then flash the target briefly.
  function go(to) {
    const [page, anchor] = String(to).split('#');
    if (page && page !== state.page) {
      if (location.hash.slice(1) !== page) history.replaceState(null, '', '#' + page);
      show(page);
    }
    const target = anchor ? document.getElementById(anchor) : document.getElementById('p-' + page);
    if (!target) return;
    if (target.classList.contains('shut')) setFold(target, true);
    const smooth = motionOK() ? 'smooth' : 'auto';
    requestAnimationFrame(() => {
      const y = target.getBoundingClientRect().top + window.scrollY - 72;
      window.scrollTo({ top: Math.max(0, y), behavior: anchor ? smooth : 'auto' });
      if (anchor) { target.classList.remove('gl-flash'); void target.offsetWidth; target.classList.add('gl-flash'); setTimeout(() => target.classList.remove('gl-flash'), 1600); }
    });
  }
  function init() {
    document.getElementById('wallet').innerHTML = `<span title="${esc(M.wallet)}">${esc(shortAddr(M.wallet))}</span>${copyBtn(M.wallet)}`;
    // Metric definitions for info bubbles, from every window's scorecard.
    Object.values(M.scorecard || {}).forEach((gs) => gs.forEach((g) => g.items.forEach((i) => { if (!DEF[i.key]) DEF[i.key] = { label: i.label, definition: i.definition, source: i.source }; })));
    document.body.insertAdjacentHTML('afterbegin', '<div id="vbar" class="vbar" aria-label="摘要"></div>');
    document.addEventListener('click', (e) => {
      const t = e.target.closest ? e.target : e.target.parentElement;
      const cp = t.closest('[data-copy]');
      if (cp) { copyText(cp.dataset.copy, cp); return; }
      const info = t.closest('.info');
      if (info) { e.preventDefault(); e.stopPropagation(); if (tipFor === info) hideTip(); else showTip(info); return; }
      if (!t.closest('.tipbox')) hideTip();
      document.querySelectorAll('details.menu[open]').forEach((d) => { if (!d.contains(t)) d.removeAttribute('open'); });
      if (t.closest('[data-print]')) { window.print(); return; }
      if (t.closest('[data-top]')) { window.scrollTo({ top: 0, behavior: motionOK() ? 'smooth' : 'auto' }); return; }
      const fb = t.closest('.fold-btn');
      if (fb) { const box = fb.closest('.chap-box'); setFold(box, box.classList.contains('shut')); return; }
      const wb = t.closest('[data-w]');
      if (wb) { setWindow(wb.dataset.w); return; }
      const link = t.closest('[data-to]');
      if (link && !t.closest('a,button:not([data-to]),details')) go(link.dataset.to);
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { hideTip(); document.querySelectorAll('details.menu[open]').forEach((d) => d.removeAttribute('open')); return; }
      if ((e.key === 'Enter' || e.key === ' ') && e.target.matches && e.target.matches('[data-to]')) { e.preventDefault(); go(e.target.dataset.to); }
    });
    document.addEventListener('mouseover', (e) => { const i = e.target.closest && e.target.closest('.info'); if (i && matchMedia('(hover: hover)').matches && tipFor !== i) showTip(i); });
    document.addEventListener('mouseout', (e) => { const i = e.target.closest && e.target.closest('.info'); if (i && matchMedia('(hover: hover)').matches && !i.contains(e.relatedTarget)) hideTip(); });
    window.addEventListener('scroll', () => { if (tipFor) hideTip(); }, { passive: true });
    // Layout-dependent pieces (table vs. cards) re-render when crossing the phone breakpoint.
    if (window.matchMedia) {
      const mq = matchMedia(NARROW);
      const onMq = () => { document.querySelectorAll('.page-section').forEach((s) => { if (s.id !== 'p-' + state.page) delete s.dataset.window; }); if (state.page) renderPage(state.page); };
      if (mq.addEventListener) mq.addEventListener('change', onMq); else if (mq.addListener) mq.addListener(onMq);
    }
    if (window.ResizeObserver) new ResizeObserver(queueFit).observe(document.getElementById('pages'));
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(queueFit);
    document.getElementById('generated').textContent = '生成于 ' + date(M.generated_at_ms, true) + ' UTC';
    const rec = (M.quality || {}).reconciliation || {};
    document.getElementById('badge-recon').innerHTML = statusBadge(rec.status, (STATUS[rec.status] || STATUS.no_data)[2] + (isNum(rec.residual_pct) ? ' ' + pct(rec.residual_pct, 1) : ''));
    const t = (M.quality || {}).truncation || {};
    document.getElementById('badge-trunc').innerHTML = statusBadge((t.status === 'complete' || t.status === 'no_gap_detected') ? 'ok' : t.status === 'api_visible_gaps' ? 'warn' : 'fail',
      { complete: '未见缺口', no_gap_detected: '未见缺口', api_visible_gaps: '历史有缺口', api_visible_truncated: '早期历史截断' }[t.status] || '历史未知');
    document.getElementById('nav').innerHTML = PAGES.map((p) => `<li class="nav-item"><a class="nav-link" data-page="${p[0]}" href="#${p[0]}">${p[1]}</a></li>`).join('');
    document.getElementById('pages').innerHTML = PAGES.map((p) => `<section class="page-section" id="p-${p[0]}"></section>`).join('');
    const wins = Object.keys(M.windows || {});
    document.getElementById('windows').innerHTML = wins.map((w) => `<button type="button" class="${w === state.window ? 'on' : ''}" data-w="${w}" aria-pressed="${w === state.window}">${esc(M.windows[w].label)}</button>`).join('');
    document.getElementById('window-note').textContent = '作用于总览、业绩、风险页的指标与图表';
    const themeBtn = document.getElementById('theme');
    const setTheme = (t) => { document.documentElement.setAttribute('data-bs-theme', t); themeBtn.textContent = t === 'dark' ? '浅色' : '深色'; try { localStorage.setItem('hl-theme', t); } catch (e) { /* file:// may block storage */ } };
    let saved = null;
    try { saved = localStorage.getItem('hl-theme'); } catch (e) { /* ignore */ }
    setTheme(saved || (window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'));
    themeBtn.addEventListener('click', () => { setTheme(document.documentElement.getAttribute('data-bs-theme') === 'dark' ? 'light' : 'dark'); rerender(); });
    document.getElementById('footer').innerHTML = '来源标记：<b>官方</b> = Hyperliquid portfolio / 快照接口；<b>重建</b> = 由成交、资金费重建；<b>混合</b> = 官方总额 + 重建日内形状。'
      + '所有金额以 USD 计，时间为 UTC。完整数据见 exports/。';
    window.addEventListener('hashchange', () => { const h = location.hash.slice(1); if (h.split('#')[0] !== state.page) show(h.split('#')[0]); });
    const h0 = location.hash.slice(1) || 'overview';
    show(h0.split('#')[0]);
    if (h0.includes('#')) go(h0);
  }
  document.addEventListener('DOMContentLoaded', init);
}());
