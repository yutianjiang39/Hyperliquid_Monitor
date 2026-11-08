"""Return and risk statistics on the daily series (``metrics.series``).

All functions take plain floats and return JSON-safe dicts. ``None`` means the
statistic is not defined for the input (too few days, zero denominator), never
zero.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone

DAY_MS = 86_400_000
YEAR_DAYS = 365.0


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


def twr_index(days):
    """Cumulative time-weighted return index starting at 1.0."""
    level, out = 1.0, []
    for d in days:
        if d["ret"] is not None:
            level *= 1 + d["ret"]
        out.append({"time": d["time"], "index": level})
    return out


def xirr(cashflows):
    """Annualized money-weighted return; cashflows [(ms, amount)] investor view."""
    flows = [(t, a) for t, a in cashflows if a]
    if len(flows) < 2 or all(a >= 0 for _, a in flows) or all(a <= 0 for _, a in flows):
        return None
    t0 = flows[0][0]

    def npv(rate):
        return sum(a / (1 + rate) ** ((t - t0) / DAY_MS / YEAR_DAYS) for t, a in flows)

    lo, hi = -0.9999, 100.0
    f_lo, f_hi = npv(lo), npv(hi)
    if f_lo * f_hi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = npv(mid)
        if abs(f_mid) < 1e-6:
            break
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2


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
        "count": len(significant), "threshold": threshold,
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
    """Share of positive months and the dispersion of monthly returns."""
    rets = [m["ret"] for m in months if m["ret"] is not None]
    pnls = [m["pnl"] for m in months]
    return {
        "months": len(months), "positive_months": sum(1 for x in pnls if x > 0),
        "monthly_win_rate": sum(1 for x in pnls if x > 0) / len(pnls) if pnls else None,
        "monthly_ret_std": _std(rets), "monthly_pnl_std": _std(pnls),
        "monthly_ret_mean": _mean(rets),
    }


def pnl_path(days):
    """Peak and initial-loss figures of the cumulative PnL path."""
    cum = [d["cum_pnl"] for d in days]
    if not cum:
        return {}
    # Initial loss: the low reached before the account was ever in profit.
    first_up = next((i for i, v in enumerate(cum) if v > 0), len(cum))
    low = min(cum[:first_up] or [0.0])
    return {"pnl_peak": max(cum), "pnl_trough": min(cum), "initial_loss": low if low < 0 else None}


def performance(days, *, official_pnl=None):
    if not days:
        return {}
    rets = [d["ret"] for d in days if d["ret"] is not None]
    index = twr_index(days)
    twr = index[-1]["index"] - 1 if index else None
    years = (days[-1]["time"] - days[0]["time"] + DAY_MS) / DAY_MS / YEAR_DAYS
    cagr = (1 + twr) ** (1 / years) - 1 if twr is not None and years >= 30 / YEAR_DAYS and twr > -1 else None
    start_equity = days[0]["equity"] - days[0]["pnl"] - days[0]["flow"]
    cashflows = [(days[0]["time"] - DAY_MS, -start_equity)]
    cashflows += [(d["time"], -d["flow"]) for d in days]
    cashflows.append((days[-1]["time"], days[-1]["equity"]))
    pnl = sum(d["pnl"] for d in days)
    net_flow = sum(d["flow"] for d in days)
    invested = start_equity + sum(max(d["flow"], 0) for d in days)
    daily = [d["pnl"] for d in days]
    gains, losses = sum(x for x in daily if x > 0), -sum(x for x in daily if x < 0)
    monthly, weekly = defaultdict(lambda: [0.0, 1.0]), defaultdict(lambda: [0.0, 1.0])
    for d in days:
        for bucket, key in ((monthly, _date(d["time"] - 1, "%Y-%m")), (weekly, _date(d["time"] - 1, "%G-W%V"))):
            bucket[key][0] += d["pnl"]
            if d["ret"] is not None:
                bucket[key][1] *= 1 + d["ret"]
    table = lambda b: [{"period": k, "pnl": v[0], "ret": v[1] - 1} for k, v in sorted(b.items())]
    months, weeks = table(monthly), table(weekly)

    def extreme(rows, key, fn):
        return fn(rows, key=lambda r: r[key]) if rows else None
    day_rows = [{"period": _date(d["time"] - 1), "pnl": d["pnl"], "ret": d["ret"]} for d in days]
    return {
        "pnl": pnl, "official_pnl": official_pnl, "net_flow": net_flow,
        "start_equity": start_equity, "end_equity": days[-1]["equity"],
        "mean_equity": _mean([d["equity"] for d in days]),
        "twr": twr, "cagr": cagr, "xirr": xirr(cashflows),
        "roi_on_invested": pnl / invested if invested > 0 else None,
        "profit_factor_daily": gains / losses if losses else None,
        "positive_day_ratio": sum(1 for x in daily if x > 0) / len(daily),
        "best_day": extreme(day_rows, "pnl", max), "worst_day": extreme(day_rows, "pnl", min),
        "best_week": extreme(weeks, "pnl", max), "worst_week": extreme(weeks, "pnl", min),
        "best_month": extreme(months, "pnl", max), "worst_month": extreme(months, "pnl", min),
        "return_days": len(rets), "days": len(days), "years": years,
        "monthly": months, "weekly": weeks, "twr_index": index,
        "monthly_stability": monthly_stability(months),
    }


def risk(days, *, exposure_days=None):
    if not days:
        return {}
    rets = [d["ret"] for d in days if d["ret"] is not None]
    index = twr_index(days)
    underwater, episodes = drawdowns(index)
    cum = [{"time": d["time"], "cum": d["cum_pnl"]} for d in days]
    usd_underwater, usd_episodes = drawdowns(cum, key="cum", relative=False, top=5)
    mdd = min((u["dd"] for u in underwater), default=0.0)
    vol_d = _std(rets)
    mean_d = _mean(rets)
    downside = [min(r, 0.0) for r in rets]
    down_dev = math.sqrt(sum(x * x for x in downside) / len(downside)) if downside else None
    years = (days[-1]["time"] - days[0]["time"] + DAY_MS) / DAY_MS / YEAR_DAYS
    twr = index[-1]["index"] - 1
    cagr = (1 + twr) ** (1 / years) - 1 if years >= 30 / YEAR_DAYS and twr > -1 else None
    ulcer = math.sqrt(sum((u["dd"] * 100) ** 2 for u in underwater) / len(underwater)) if underwater else None
    var95 = _quantile(rets, 0.05)
    tail = [r for r in rets if var95 is not None and r <= var95]
    skew = kurt = None
    if vol_d and len(rets) > 3:
        n = len(rets)
        skew = sum((r - mean_d) ** 3 for r in rets) / n / vol_d ** 3
        kurt = sum((r - mean_d) ** 4 for r in rets) / n / vol_d ** 4 - 3
    usd_threshold = 0.05 * max((abs(d["equity"]) for d in days), default=0.0)
    path = pnl_path(days)
    usd_mdd = -min((u["dd"] for u in usd_underwater), default=0.0)
    pnl_total = sum(d["pnl"] for d in days)
    rolling = []
    for i in range(29, len(days)):
        window = [d["ret"] for d in days[i - 29:i + 1] if d["ret"] is not None]
        sd = _std(window)
        rolling.append({"time": days[i]["time"], "vol": sd * math.sqrt(YEAR_DAYS) if sd else None,
                        "sharpe": (_mean(window) / sd * math.sqrt(YEAR_DAYS)) if sd else None})
    out = {
        "max_drawdown": -mdd, "max_drawdown_usd": usd_mdd,
        "current_drawdown": -underwater[-1]["dd"] if underwater else None,
        "longest_drawdown_days": max((e["duration_days"] for e in episodes), default=0.0),
        "volatility": vol_d * math.sqrt(YEAR_DAYS) if vol_d else None,
        "sharpe": mean_d / vol_d * math.sqrt(YEAR_DAYS) if vol_d else None,
        "sortino": mean_d / down_dev * math.sqrt(YEAR_DAYS) if down_dev else None,
        "calmar": cagr / -mdd if cagr is not None and mdd < 0 else None,
        "ulcer_index": ulcer, "var95": var95, "cvar95": _mean(tail),
        "skew": skew, "excess_kurtosis": kurt,
        "omega": omega(rets), "tail_ratio": tail_ratio(rets),
        "recovery_factor": pnl_total / usd_mdd if usd_mdd > 0 else None,
        "drawdown_stats": drawdown_summary(index),
        # USD episodes count at 5% of the largest equity seen, the dollar analogue of the % threshold.
        "usd_drawdown_stats": drawdown_summary(cum, key="cum", relative=False, threshold=usd_threshold),
        **path,
        "underwater": underwater, "drawdown_episodes": episodes, "usd_drawdown_episodes": usd_episodes,
        "rolling_30d": rolling,
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
                   leverage_max=max(values) if values else None)
    return out
