"""Return and risk statistics on the daily series (``metrics.series``).

All functions take plain floats and return JSON-safe dicts. ``None`` means the
statistic is not defined for the input (too few days, zero denominator), never
zero.

Window conventions (v3.3):

* Every window starts from its own base: TWR index 1.0 and cumulative PnL 0 at
  the window start, so a loss on the first day is a drawdown from that base.
* A return chain with no valid daily return is ``None`` (not 0 %); month /
  week buckets carry ``valid_days`` and are ``None`` when none are valid.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone

DAY_MS = 86_400_000
YEAR_DAYS = 365.0
ROLLING_MIN_VALID = 20      # 30-day rolling stats need this many valid days
CAGR_STABLE_DAYS = 180      # below this a CAGR is shown as an estimate
TAIL_MIN_DAYS = 30          # VaR / CVaR / skew / kurtosis sample floor
ACTIVE_PERIOD_USD = 0.5     # |PnL| below this makes a month/week "inactive"


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def _std(xs):
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _quantile(xs, q):
    if not xs:
        return None
    s = sorted(xs)
    pos = (len(s) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def _date(ms, fmt="%Y-%m-%d"):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime(fmt)


def _start_ms(days):
    """Window start: the first row's own start when the series knows it."""
    first = days[0]
    return first.get("start", first["time"] - DAY_MS)


def twr_index(days, base=False):
    """Cumulative time-weighted return index starting at 1.0.

    ``base`` prepends the window-start point (index 1.0) so drawdowns are
    measured from the starting capital, not from the first day's close.
    """
    level, out = 1.0, []
    if base and days:
        out.append({"time": _start_ms(days), "index": 1.0, "base": True})
    for d in days:
        if d["ret"] is not None:
            level *= 1 + d["ret"]
        out.append({"time": d["time"], "index": level, "valid": d["ret"] is not None})
    return out


def window_cum(days):
    """Cumulative PnL re-zeroed at the window start, with the start point."""
    if not days:
        return []
    acc, out = 0.0, [{"time": _start_ms(days), "cum": 0.0, "base": True}]
    for d in days:
        acc += d["pnl"]
        out.append({"time": d["time"], "cum": acc})
    return out


def xirr_detail(cashflows):
    """Money-weighted annual return with an explicit status.

    status: ok / insufficient_data / no_sign_change / bracket_limit /
    multiple_roots_possible (more than one sign change in the cash flows).
    """
    flows = sorted(((t, a) for t, a in cashflows if a), key=lambda x: x[0])
    if len(flows) < 2:
        return {"value": None, "status": "insufficient_data"}
    if all(a >= 0 for _, a in flows) or all(a <= 0 for _, a in flows):
        return {"value": None, "status": "no_sign_change"}
    t0 = flows[0][0]
    changes = sum(1 for (_, a), (_, b) in zip(flows, flows[1:]) if (a > 0) != (b > 0))

    def npv(rate):
        return sum(a / (1 + rate) ** ((t - t0) / DAY_MS / YEAR_DAYS) for t, a in flows)

    lo, hi = -0.9999, 100.0
    try:
        f_lo, f_hi = npv(lo), npv(hi)
    except OverflowError:
        return {"value": None, "status": "bracket_limit"}
    if f_lo * f_hi > 0:
        return {"value": None, "status": "bracket_limit", "bracket": [lo, hi]}
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = npv(mid)
        if abs(f_mid) < 1e-6:
            break
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    value = (lo + hi) / 2
    scale = max(abs(a) for _, a in flows)
    return {"value": value, "residual": npv(value) / scale if scale else None,
            "status": "multiple_roots_possible" if changes > 1 else "ok", "sign_changes": changes}


def xirr(cashflows):
    """Annualized money-weighted return; cashflows [(ms, amount)] investor view."""
    return xirr_detail(cashflows)["value"]


def drawdowns(index_rows, key="index", relative=True, top=10):
    """Underwater series plus the ``top`` deepest drawdown episodes."""
    peak, peak_t = None, None
    underwater, episodes, current = [], [], None
    for row in index_rows:
        v, t = row[key], row["time"]
        if peak is None or v >= peak:
            if current:
                current["recovered_ms"] = t
                current["recovery_days"] = (t - current["trough_ms"]) / DAY_MS
                episodes.append(current)
                current = None
            peak, peak_t = v, t
            underwater.append({"time": t, "dd": 0.0})
            continue
        dd = (v / peak - 1) if relative and peak > 0 else (v - peak)
        underwater.append({"time": t, "dd": dd})
        if current is None:
            current = {"peak_ms": peak_t, "trough_ms": t, "depth": dd, "recovered_ms": None, "recovery_days": None}
        elif dd < current["depth"]:
            current.update(trough_ms=t, depth=dd)
    if current:
        episodes.append(current)
    for e in episodes:
        end = e["recovered_ms"] or (index_rows[-1]["time"] if index_rows else e["trough_ms"])
        e["duration_days"] = (end - e["peak_ms"]) / DAY_MS
    episodes.sort(key=lambda e: e["depth"])
    return underwater, episodes if top is None else episodes[:top]


def drawdown_summary(index_rows, key="index", relative=True, threshold=0.05):
    """Duration, count and time-share statistics over every drawdown episode.

    ``threshold`` is the depth (fraction, or USD when ``relative`` is False)
    an episode must reach to count; shallow wiggles stay in the time shares.
    """
    if not index_rows:
        return {}
    underwater, episodes = drawdowns(index_rows, key=key, relative=relative, top=None)
    significant = [e for e in episodes if threshold is None or e["depth"] <= -threshold]
    recovered = [e["recovery_days"] for e in episodes if e["recovery_days"] is not None]
    peak, highs = None, 0
    for row in index_rows:
        if peak is None or row[key] > peak:
            highs += peak is not None
            peak = row[key]
    open_episode = next((e for e in episodes if e["recovered_ms"] is None), None)
    n = len(underwater)
    return {
        "count": len(significant), "threshold": threshold, "episodes_total": len(episodes),
        "avg_depth": -_mean([e["depth"] for e in significant]) if significant else None,
        "max_drawdown_duration_days": max(((e["trough_ms"] - e["peak_ms"]) / DAY_MS for e in episodes), default=0.0),
        "max_recovery_days": max(recovered, default=None),
        "longest_underwater_days": max((e["duration_days"] for e in episodes), default=0.0),
        "current_underwater_days": open_episode["duration_days"] if open_episode else 0.0,
        "underwater_ratio": sum(1 for u in underwater if u["dd"] < 0) / n,
        "new_high_ratio": highs / (n - 1) if n > 1 else None,
    }


def omega(rets, threshold=0.0):
    """Probability-weighted gains over losses relative to ``threshold``."""
    up = sum(r - threshold for r in rets if r > threshold)
    down = sum(threshold - r for r in rets if r < threshold)
    return up / down if down else None


def tail_ratio(rets):
    """95th percentile return over the magnitude of the 5th percentile."""
    if len(rets) < 20:
        return None
    hi, lo = _quantile(rets, 0.95), _quantile(rets, 0.05)
    return hi / -lo if lo and lo < 0 else None


def monthly_stability(months):
    """Share of positive months and the dispersion of monthly returns.

    ``monthly_win_rate`` counts every calendar month in the series;
    ``active_monthly_win_rate`` only months with |PnL| >= $0.50.
    """
    rets = [m["ret"] for m in months if m.get("ret") is not None]
    pnls = [m["pnl"] for m in months]
    active = [x for x in pnls if abs(x) >= ACTIVE_PERIOD_USD]
    return {
        "months": len(months), "positive_months": sum(1 for x in pnls if x > 0),
        "monthly_win_rate": sum(1 for x in pnls if x > 0) / len(pnls) if pnls else None,
        "active_months": len(active),
        "active_monthly_win_rate": sum(1 for x in active if x > 0) / len(active) if active else None,
        "monthly_ret_std": _std(rets), "monthly_pnl_std": _std(pnls),
        "monthly_ret_mean": _mean(rets), "months_with_return": len(rets),
    }


def pnl_path(days):
    """Peak and initial-loss figures of the cumulative PnL path, re-zeroed per window."""
    cum = [row["cum"] for row in window_cum(days)]
    if not cum:
        return {}
    # Initial loss: the low reached before the window was ever in profit.
    first_up = next((i for i, v in enumerate(cum) if v > 0), len(cum))
    low = min(cum[:first_up] or [0.0])
    return {"pnl_peak": max(cum), "pnl_trough": min(cum), "initial_loss": low if low < 0 else None}


def _years(days):
    """Covered time from the window's real start to its last row end."""
    return (days[-1]["time"] - _start_ms(days)) / DAY_MS / YEAR_DAYS


def performance(days, *, official_pnl=None, capital_flows=None):
    """Window returns. ``capital_flows`` [(ms, usd)] (+ into the account) are
    real ledger flows; when given they drive XIRR at their true timestamps."""
    if not days:
        return {}
    rets = [d["ret"] for d in days if d["ret"] is not None]
    index = twr_index(days)
    twr = index[-1]["index"] - 1 if rets else None
    years = _years(days)
    span_days = years * YEAR_DAYS
    cagr = (1 + twr) ** (1 / years) - 1 if twr is not None and span_days >= 30 and twr > -1 else None
    start_equity = days[0]["equity"] - days[0]["pnl"] - days[0]["flow"]
    end_equity = days[-1]["equity"]
    start_ms = _start_ms(days)
    if capital_flows is not None:
        cashflows = [(start_ms, -start_equity)] + [(t, -a) for t, a in capital_flows if start_ms < t <= days[-1]["time"]]
        xirr_basis = "ledger"
    else:
        cashflows = [(start_ms, -start_equity)] + [(d["time"], -d["flow"]) for d in days]
        xirr_basis = "implied_daily"
    cashflows.append((days[-1]["time"], end_equity))
    x = xirr_detail(cashflows)
    pnl = sum(d["pnl"] for d in days)
    net_flow = sum(d["flow"] for d in days)
    invested = start_equity + sum(max(d["flow"], 0) for d in days)
    peak_equity = max([start_equity] + [d["equity"] for d in days])
    daily = [d["pnl"] for d in days]
    gains, losses = sum(v for v in daily if v > 0), -sum(v for v in daily if v < 0)
    new = lambda: {"pnl": 0.0, "growth": 1.0, "valid_days": 0, "days": 0}
    monthly, weekly = defaultdict(new), defaultdict(new)
    for d in days:
        for bucket, key in ((monthly, _date(d["time"] - 1, "%Y-%m")), (weekly, _date(d["time"] - 1, "%G-W%V"))):
            b = bucket[key]
            b["pnl"] += d["pnl"]
            b["days"] += 1
            if d["ret"] is not None:
                b["growth"] *= 1 + d["ret"]
                b["valid_days"] += 1
    table = lambda b: [{"period": k, "pnl": v["pnl"], "ret": v["growth"] - 1 if v["valid_days"] else None,
                        "valid_days": v["valid_days"], "days": v["days"]} for k, v in sorted(b.items())]
    months, weeks = table(monthly), table(weekly)
    active_weeks = [w for w in weeks if abs(w["pnl"]) >= ACTIVE_PERIOD_USD]

    def extreme(rows, key, fn):
        return fn(rows, key=lambda r: r[key]) if rows else None
    day_rows = [{"period": _date(d["time"] - 1), "pnl": d["pnl"], "ret": d["ret"]} for d in days]
    return {
        "pnl": pnl, "official_pnl": official_pnl, "net_flow": net_flow,
        "start_equity": start_equity, "end_equity": end_equity,
        "mean_equity": _mean([d["equity"] for d in days]),
        "peak_equity": peak_equity,
        # Window PnL over the highest account value seen (start included); not a margin return.
        "return_on_peak_capital": pnl / peak_equity if peak_equity > 0 else None,
        "twr": twr, "cagr": cagr, "cagr_estimated": cagr is not None and span_days < CAGR_STABLE_DAYS,
        "xirr": x["value"], "xirr_status": x["status"], "xirr_basis": xirr_basis,
        "roi_on_invested": pnl / invested if invested > 0 else None,
        "profit_factor_daily": gains / losses if losses else None,
        "positive_day_ratio": sum(1 for v in daily if v > 0) / len(daily),
        "best_day": extreme(day_rows, "pnl", max), "worst_day": extreme(day_rows, "pnl", min),
        "best_week": extreme(weeks, "pnl", max), "worst_week": extreme(weeks, "pnl", min),
        "best_month": extreme(months, "pnl", max), "worst_month": extreme(months, "pnl", min),
        "weeks": len(weeks), "positive_weeks": sum(1 for w in weeks if w["pnl"] > 0),
        "weekly_win_rate": sum(1 for w in weeks if w["pnl"] > 0) / len(weeks) if weeks else None,
        "active_weeks": len(active_weeks),
        "active_weekly_win_rate": (sum(1 for w in active_weeks if w["pnl"] > 0) / len(active_weeks)
                                   if active_weeks else None),
        "return_days": len(rets), "days": len(days), "years": years, "span_days": span_days,
        "return_coverage": len(rets) / len(days),
        "start_ms": start_ms, "end_ms": days[-1]["time"],
        "monthly": months, "weekly": weeks, "twr_index": index,
        "monthly_stability": monthly_stability(months),
    }


def _ols(y, x):
    """y = a + b x. Returns a, b, r2 and Newey-West (HAC) standard errors."""
    n = len(y)
    if n < 3:
        return None
    mx, my = sum(x) / n, sum(y) / n
    sxx = sum((v - mx) ** 2 for v in x)
    if not sxx:
        return None
    b = sum((u - mx) * (v - my) for u, v in zip(x, y)) / sxx
    a = my - b * mx
    resid = [v - a - b * u for u, v in zip(x, y)]
    sst = sum((v - my) ** 2 for v in y)
    r2 = 1 - sum(e * e for e in resid) / sst if sst else None
    # Newey-West with Bartlett weights; lag rule floor(4 (n/100)^(2/9)).
    lag = int(4 * (n / 100) ** (2 / 9))
    z = [(1.0, u - mx) for u in x]  # centred regressors keep the 2x2 inverse simple
    s = [[0.0, 0.0], [0.0, 0.0]]
    for k in range(lag + 1):
        w = 1.0 if k == 0 else 1 - k / (lag + 1)
        for t in range(k, n):
            for i in range(2):
                for j in range(2):
                    v = z[t][i] * resid[t] * z[t - k][j] * resid[t - k]
                    s[i][j] += w * (v if k == 0 else v + z[t - k][i] * resid[t - k] * z[t][j] * resid[t])
    # With a centred regressor X'X = diag(n, sxx), so the sandwich is elementwise.
    var_c, var_b, cov_cb = s[0][0] / n ** 2, s[1][1] / sxx ** 2, s[0][1] / (n * sxx)
    # Centred intercept c = a + b mx  ->  a = c - b mx.
    var_a = var_c + mx ** 2 * var_b - 2 * mx * cov_cb
    se_a, se_b = math.sqrt(max(var_a, 0)), math.sqrt(max(var_b, 0))
    return {"alpha": a, "beta": b, "r2": r2, "se_alpha": se_a, "se_beta": se_b, "lag": lag, "n": n,
            "t_alpha": a / se_a if se_a else None}


def market_model(days, benchmark_closes, min_days=30, trim=0.05):
    """Daily return regression on a benchmark (BTC by default, no leverage removal).

    ``benchmark_closes`` maps UTC day-start ms -> close. A wallet day ending at
    ``time`` is matched to the benchmark move over the same calendar day.
    alpha is an arithmetic daily intercept, not a compounded excess return.
    ``trimmed`` re-estimates after dropping the best ``trim`` share of days.
    """
    closes = benchmark_closes or {}
    pairs = []
    for d in days:
        if d["ret"] is None:
            continue
        day = (d["time"] - 1) // DAY_MS * DAY_MS
        a, b = closes.get(day - DAY_MS), closes.get(day)
        if a and b:
            pairs.append((d["ret"], b / a - 1, d["time"]))
    out = {"n": len(pairs), "min_days": min_days, "benchmark": "BTC"}
    if len(pairs) < min_days:
        out["status"] = "insufficient_data"
        return out
    fit = _ols([p[0] for p in pairs], [p[1] for p in pairs])
    if not fit:
        out["status"] = "degenerate"
        return out
    out.update(fit, status="ok", alpha_annual=fit["alpha"] * YEAR_DAYS,
               beta_ci95=[fit["beta"] - 1.96 * fit["se_beta"], fit["beta"] + 1.96 * fit["se_beta"]],
               alpha_ci95=[fit["alpha"] - 1.96 * fit["se_alpha"], fit["alpha"] + 1.96 * fit["se_alpha"]])
    k = max(1, round(len(pairs) * trim))
    kept = sorted(pairs, key=lambda p: p[0])[:-k]
    t = _ols([p[0] for p in kept], [p[1] for p in kept]) if len(kept) >= min_days else None
    out["trimmed"] = {"dropped_days": k, "alpha": t["alpha"], "t_alpha": t["t_alpha"],
                      "beta": t["beta"]} if t else None
    return out


def heat_check(days, recent=30, min_recent=20, min_base=60):
    """Recent mean daily return vs the earlier baseline (Welch t)."""
    rets = [(d["time"], d["ret"]) for d in days if d["ret"] is not None]
    if not rets:
        return {"status": "insufficient_data"}
    end = days[-1]["time"]
    for span in (recent, 60, 90):
        cut = end - span * DAY_MS
        a = [r for t, r in rets if t > cut]
        b = [r for t, r in rets if t <= cut]
        if len(a) >= min_recent:
            break
    if len(a) < min_recent or len(b) < min_base:
        return {"status": "insufficient_data", "recent_n": len(a), "base_n": len(b)}
    sa, sb = _std(a), _std(b)
    se = math.sqrt(sa ** 2 / len(a) + sb ** 2 / len(b)) if sa and sb else None
    diff = _mean(a) - _mean(b)
    return {"status": "ok", "recent_days": span, "recent_mean": _mean(a), "base_mean": _mean(b),
            "recent_n": len(a), "base_n": len(b), "diff": diff, "t": diff / se if se else None}


def risk(days, *, exposure_days=None):
    if not days:
        return {}
    rets = [d["ret"] for d in days if d["ret"] is not None]
    index = twr_index(days, base=True)
    underwater, episodes = drawdowns(index)
    _, all_episodes = drawdowns(index, top=None)
    cum = window_cum(days)
    usd_underwater, usd_episodes = drawdowns(cum, key="cum", relative=False, top=5)
    valid_chain = bool(rets)
    mdd = min((u["dd"] for u in underwater), default=0.0) if valid_chain else None
    vol_d = _std(rets)
    mean_d = _mean(rets)
    downside = [min(r, 0.0) for r in rets]
    down_dev = math.sqrt(sum(v * v for v in downside) / len(downside)) if downside else None
    years = _years(days)
    twr = index[-1]["index"] - 1 if valid_chain else None
    cagr = (1 + twr) ** (1 / years) - 1 if twr is not None and years * YEAR_DAYS >= 30 and twr > -1 else None
    ulcer = (math.sqrt(sum((u["dd"] * 100) ** 2 for u in underwater) / len(underwater))
             if underwater and valid_chain else None)
    tail_ok = len(rets) >= TAIL_MIN_DAYS
    q05 = _quantile(rets, 0.05) if tail_ok else None
    tail = [r for r in rets if q05 is not None and r <= q05]
    skew = kurt = None
    if tail_ok:
        # Population (moment) estimators throughout: m3 / m2^1.5 and m4 / m2^2 - 3.
        n = len(rets)
        m2 = sum((r - mean_d) ** 2 for r in rets) / n
        if m2 > 0:
            skew = sum((r - mean_d) ** 3 for r in rets) / n / m2 ** 1.5
            kurt = sum((r - mean_d) ** 4 for r in rets) / n / m2 ** 2 - 3
    usd_threshold = 0.05 * max((abs(d["equity"]) for d in days), default=0.0)
    path = pnl_path(days)
    usd_mdd = -min((u["dd"] for u in usd_underwater), default=0.0)
    pnl_total = sum(d["pnl"] for d in days)
    rolling = []
    for i in range(29, len(days)):
        window = [d["ret"] for d in days[i - 29:i + 1] if d["ret"] is not None]
        sd = _std(window) if len(window) >= ROLLING_MIN_VALID else None
        rolling.append({"time": days[i]["time"], "valid_days": len(window),
                        "vol": sd * math.sqrt(YEAR_DAYS) if sd else None,
                        "sharpe": (_mean(window) / sd * math.sqrt(YEAR_DAYS)) if sd else None})
    dd_stats = drawdown_summary(index) if valid_chain else {}
    out = {
        "max_drawdown": -mdd if mdd is not None else None, "max_drawdown_usd": usd_mdd,
        "current_drawdown": -underwater[-1]["dd"] if underwater and valid_chain else None,
        # Every episode, not just the deepest ten: a shallow but long one counts.
        "longest_drawdown_days": max((e["duration_days"] for e in all_episodes), default=0.0) if valid_chain else None,
        "volatility": vol_d * math.sqrt(YEAR_DAYS) if vol_d else None,
        "volatility_daily": vol_d,
        "sharpe": mean_d / vol_d * math.sqrt(YEAR_DAYS) if vol_d else None,
        "sortino": mean_d / down_dev * math.sqrt(YEAR_DAYS) if down_dev else None,
        "calmar": cagr / -mdd if cagr is not None and mdd is not None and mdd < 0 else None,
        "ulcer_index": ulcer,
        # 5th percentile of daily returns (usually negative) and the loss-positive 95% VaR.
        "var95": q05, "cvar95": _mean(tail),
        "var95_loss": -q05 if q05 is not None and q05 < 0 else (0.0 if q05 is not None else None),
        "tail_sample_days": len(rets), "tail_min_days": TAIL_MIN_DAYS,
        "skew": skew, "excess_kurtosis": kurt,
        "omega": omega(rets), "tail_ratio": tail_ratio(rets),
        "recovery_factor": pnl_total / usd_mdd if usd_mdd > 0 else None,
        "return_days": len(rets),
        "drawdown_stats": dd_stats,
        # USD episodes count at 5% of the largest equity seen, the dollar analogue of the % threshold.
        "usd_drawdown_stats": drawdown_summary(cum, key="cum", relative=False, threshold=usd_threshold),
        **path,
        "underwater": underwater, "drawdown_episodes": episodes, "usd_drawdown_episodes": usd_episodes,
        "rolling_30d": rolling, "rolling_min_valid": ROLLING_MIN_VALID,
        "heat": heat_check(days),
    }
    if exposure_days:
        equity = {d["time"] // DAY_MS: d["equity"] for d in days}
        lev = []
        for row in exposure_days:
            v = equity.get(row["time"] // DAY_MS + 1)
            if v and v > 0:
                lev.append({"time": row["time"], "leverage": float(row["gross_exposure"]) / v,
                            "net": (float(row["long_exposure"]) - float(row["short_exposure"])) / v,
                            "long": float(row["long_exposure"]), "short": float(row["short_exposure"])})
        values = [x["leverage"] for x in lev]
        out.update(leverage_series=lev, leverage_mean=_mean(values), leverage_p95=_quantile(values, 0.95),
                   leverage_max=max(values) if values else None, leverage_samples=len(values),
                   leverage_sampling="day_end")
    return out
