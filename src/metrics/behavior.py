"""Trading behavior and cost metrics from fills (engine operations/campaigns).

Campaign statistics use only campaigns with a fully observed lifecycle
(``eligible_for_win_rate``); the count of excluded campaigns is reported.

Counting rules (v3.3):

* A fill is counted once by ``fill_id``; a reversal fill that the engine split
  into a close leg and an open leg is one fill, one reversal and two legs.
* Fees are summed in USD: stablecoin fees at face value, other fee tokens at
  the supplied spot price (flagged ``approx``), unknown ones counted as
  missing instead of being added as raw token amounts.
* Funding received / paid are split per payment, before any netting.
"""
from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from datetime import datetime, timezone

from sources.market_data import STABLECOINS

DAY_MS = 86_400_000
HOLD_BUCKETS = ((0, "<5分钟"), (300_000, "5分钟-1小时"), (3_600_000, "1-4小时"), (14_400_000, "4-24小时"),
                (DAY_MS, "1-7天"), (7 * DAY_MS, "1-4周"), (28 * DAY_MS, ">4周"))
OUTCOME_EDGES = ((-0.10, "≤-10%"), (-0.03, "-10%~-3%"), (-0.01, "-3%~-1%"), (0.0, "-1%~0"),
                 (0.01, "0~1%"), (0.03, "1%~3%"), (0.10, "3%~10%"), (None, ">10%"))
DUST_USD = 0.05             # campaigns whose entry and exit notional stay below this are "dust"
R_MIN_TRADES, R_MIN_LOSSES = 20, 10
BOT_MIN_ORDERS = 50


def _f(value):
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def _median(xs):
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def _bucket(ms):
    label = HOLD_BUCKETS[0][1]
    for lower, name in HOLD_BUCKETS:
        if ms >= lower:
            label = name
    return label


def _streaks(values):
    """Longest win / loss runs and the current signed run (flat ends a run)."""
    best_win = best_loss = run = 0
    for v in values:
        if v > 0:
            run = run + 1 if run > 0 else 1
        elif v < 0:
            run = run - 1 if run < 0 else -1
        else:
            run = 0
        best_win, best_loss = max(best_win, run), min(best_loss, run)
    return best_win, -best_loss, run


def wilson(k, n, z=1.96):
    """Wilson score interval for a binomial share (assumes independent trials)."""
    if not n:
        return None
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return [max(0.0, centre - half), min(1.0, centre + half)]


def _t975(df):
    """Two-sided 95% Student t critical value (close approximation)."""
    if df <= 0:
        return None
    return 1.959964 + 2.37 / df + 2.8 / df ** 2


def fee_usd(op, prices=None):
    """(usd, approx) for one operation's fee; (None, False) when not convertible."""
    fee = _f(op.get("fee"))
    if fee is None:
        return None, False
    token = op.get("fee_token")
    if token is None or token in STABLECOINS:
        return fee, False
    px = _f((prices or {}).get(token))
    if px is None:
        return (0.0, False) if fee == 0 else (None, False)
    return fee * px, True


def _pnl(c):
    return _f(c.get("net_pnl_with_funding")) if c.get("net_pnl_with_funding") is not None else _f(c.get("net_pnl"))


def _entry(c):
    return _f(c.get("observed_entry_notional")) or _f(c.get("entry_notional"))


def _brief(c, pnl):
    return {"id": c.get("id"), "coin": c.get("coin"), "direction": c.get("direction"),
            "open_time": c.get("open_time"), "close_time": c.get("close_time"), "pnl": pnl,
            "entry_notional": _entry(c), "funding": _f(c.get("funding")),
            "fees": sum(_f(v) or 0.0 for v in (c.get("fees_by_token") or {}).values()),
            "liquidated": bool(c.get("liquidated"))}


def _independence(eligible):
    """Effective independent trades: n / mean number of concurrently open campaigns."""
    spans = [(c["open_time"], c["close_time"]) for c in eligible
             if c.get("open_time") is not None and c.get("close_time") is not None]
    if not spans:
        return None, None
    opens = sorted(a for a, _ in spans)
    closes = sorted(b for _, b in spans)
    n = len(spans)
    # Overlapping = all - (closed before this opened) - (opened after this closed).
    concurrent = [n - bisect_right(closes, a) - (n - bisect_left(opens, b)) for a, b in spans]
    mean = _mean([max(1, k) for k in concurrent])
    return n / mean, mean


def _r_stats(eligible, pnls):
    """Historical-loss-normalised R: 1R = |median return-on-notional of losing trades|."""
    rets = [(p / _entry(c)) for c, p in zip(eligible, pnls) if _entry(c)]
    losses = [r for r in rets if r < 0]
    out = {"n": len(rets), "losses": len(losses), "min_trades": R_MIN_TRADES, "min_losses": R_MIN_LOSSES}
    if len(rets) < R_MIN_TRADES or len(losses) < R_MIN_LOSSES:
        out["status"] = "insufficient_data"
        return out, rets
    one_r = abs(_median(losses))
    if not one_r:
        out["status"] = "degenerate"
        return out, rets
    rs = [r / one_r for r in rets]
    mean = _mean(rs)
    sd = math.sqrt(sum((x - mean) ** 2 for x in rs) / (len(rs) - 1))
    t = _t975(len(rs) - 1)
    half = t * sd / math.sqrt(len(rs))
    out.update(status="ok", one_r=one_r, expectancy_r=mean, avg_r_ci95=[mean - half, mean + half],
               worst_r=min(rs), beyond_3r=sum(1 for x in rs if x <= -3),
               beyond_3r_share=sum(1 for x in rs if x <= -3) / len(rs))
    return out, rets


def _outcome_buckets(rets):
    counts = Counter()
    for r in rets:
        for edge, label in OUTCOME_EDGES:
            if edge is None or r <= edge:
                counts[label] += 1
                break
    return [{"bucket": label, "count": counts.get(label, 0)} for _, label in OUTCOME_EDGES]


def _size_after(eligible, pnls, equity_at):
    """Median position size (% of prior equity) after a losing vs a winning trade."""
    if not equity_at or not eligible:
        return None
    by_close = sorted(((c["close_time"], p) for c, p in zip(eligible, pnls) if c.get("close_time")),
                      key=lambda x: x[0])
    close_t = [t for t, _ in by_close]
    sized, after = [], {"loss": [], "win": []}
    for c in sorted(eligible, key=lambda c: c.get("open_time") or 0):
        eq, entry = equity_at(c.get("open_time")), _entry(c)
        if not eq or eq <= 0 or not entry:
            continue
        pct = entry / eq
        sized.append(pct)
        i = bisect_left(close_t, c["open_time"]) - 1
        if i >= 0 and by_close[i][1] != 0:
            after["loss" if by_close[i][1] < 0 else "win"].append(pct)
    ml, mw = _median(after["loss"]), _median(after["win"])
    return {"size_pct_median": _median(sized), "size_pct_mean": _mean(sized), "coverage": len(sized) / len(eligible),
            "after_loss_median": ml, "after_win_median": mw, "after_loss_n": len(after["loss"]),
            "after_win_n": len(after["win"]), "ratio": ml / mw if ml is not None and mw else None,
            "basis": "entry notional ÷ last day-end equity before open"}


def campaign_stats(campaigns, *, equity_at=None):
    eligible = sorted((c for c in campaigns if c.get("eligible_for_win_rate")), key=lambda c: c["close_time"])
    pnls = [_pnl(c) for c in eligible]
    wins, losses = [x for x in pnls if x > 0], [x for x in pnls if x < 0]
    flats = len(pnls) - len(wins) - len(losses)
    gross_win, gross_loss = sum(wins), -sum(losses)
    win_streak, loss_streak, current = _streaks(pnls)
    durations = [c["duration_ms"] for c in eligible if c.get("duration_ms") is not None]
    hold = Counter(_bucket(d) for d in durations)
    win_hold = [c["duration_ms"] for c, p in zip(eligible, pnls) if p > 0 and c.get("duration_ms") is not None]
    loss_hold = [c["duration_ms"] for c, p in zip(eligible, pnls) if p < 0 and c.get("duration_ms") is not None]
    by_dir = {}
    for direction in ("long", "short"):
        sub = [p for c, p in zip(eligible, pnls) if c["direction"] == direction]
        up, down = sum(x for x in sub if x > 0), -sum(x for x in sub if x < 0)
        by_dir[direction] = {"count": len(sub), "pnl": sum(sub), "gross_win": up, "gross_loss": down,
                             "wins": sum(1 for x in sub if x > 0),
                             "win_rate": sum(1 for x in sub if x > 0) / len(sub) if sub else None,
                             "profit_factor": up / down if down else None,
                             "expectancy": sum(sub) / len(sub) if sub else None}
    scatter = [{"coin": c["coin"], "hold_h": c["duration_ms"] / 3_600_000, "pnl": p, "direction": c["direction"],
                "close_time": c["close_time"], "notional": _f(c.get("observed_entry_notional"))}
               for c, p in zip(eligible, pnls) if c.get("duration_ms") is not None]
    # Win rate under three cost bases: before fees (closedPnl), after fees, after fees + funding.
    before_fees = [_f(c.get("closed_pnl")) for c in eligible if _f(c.get("closed_pnl")) is not None]
    trade_only = [_f(c.get("net_pnl")) for c in eligible if _f(c.get("net_pnl")) is not None]
    avg_w = gross_win / len(wins) if wins else None
    avg_l = gross_loss / len(losses) if losses else None
    strict_n = len(wins) + len(losses)
    r_stats, rets = _r_stats(eligible, pnls)
    independent, overlap = _independence(eligible)
    dust = [(c, p) for c, p in zip(eligible, pnls)
            if max(_f(c.get("observed_entry_notional")) or 0.0, _f(c.get("observed_exit_notional")) or 0.0) < DUST_USD]
    ranked = sorted(zip(eligible, pnls), key=lambda cp: cp[1])
    return {
        "campaigns_total": len(campaigns), "campaigns_evaluated": len(eligible),
        "campaigns_excluded": len(campaigns) - len(eligible),
        "wins": len(wins), "losses": len(losses), "flats": flats,
        "gross_win": gross_win, "gross_loss": gross_loss,
        "gross_trade_profit": sum(x for x in before_fees if x > 0),
        "net_pnl": sum(pnls),
        "pnl_basis": "net_pnl_with_funding",
        "win_rate": len(wins) / len(pnls) if pnls else None,
        "win_rate_strict": len(wins) / strict_n if strict_n else None,
        "win_rate_strict_ci95": wilson(len(wins), strict_n),
        "win_rate_trade_only": sum(1 for x in trade_only if x > 0) / len(trade_only) if trade_only else None,
        "win_rate_before_fees": sum(1 for x in before_fees if x > 0) / len(before_fees) if before_fees else None,
        "avg_win": avg_w,
        "avg_loss": -avg_l if avg_l is not None else None,
        # Break-even win rate with |avg loss|; compare with the strict (flats excluded) win rate.
        "break_even_win_rate": avg_l / (avg_w + avg_l) if avg_w is not None and avg_l is not None else None,
        "payoff_ratio": avg_w / avg_l if avg_w is not None and avg_l else None,
        "profit_factor": gross_win / gross_loss if gross_loss else None,
        "profit_factor_status": "ok" if gross_loss else ("no_losses" if wins else "no_sample"),
        "expectancy": sum(pnls) / len(pnls) if pnls else None,
        "max_win": max(wins) if wins else None, "max_loss": min(losses) if losses else None,
        "max_win_streak": win_streak, "max_loss_streak": loss_streak, "current_streak": current,
        "median_hold_hours": (_median(durations) or 0) / 3_600_000 if durations else None,
        "mean_hold_hours": sum(durations) / len(durations) / 3_600_000 if durations else None,
        "win_median_hold_hours": _median(win_hold) / 3_600_000 if win_hold else None,
        "loss_median_hold_hours": _median(loss_hold) / 3_600_000 if loss_hold else None,
        "win_hold_n": len(win_hold), "loss_hold_n": len(loss_hold),
        # Net over everything won and lost: 1 = never lost, 0 = break-even churn.
        "efficiency_ratio": sum(pnls) / (gross_win + gross_loss) if gross_win + gross_loss else None,
        "concentration": concentration(pnls),
        "independent_trades": independent, "mean_concurrent": overlap,
        "r": r_stats, "outcome_buckets": _outcome_buckets(rets), "outcome_n": len(rets),
        "size": _size_after(eligible, pnls, equity_at),
        "dust_campaigns": len(dust), "dust_pnl": sum(p for _, p in dust), "dust_threshold": DUST_USD,
        "last_20": [{"id": c.get("id"), "coin": c.get("coin"), "pnl": p, "close_time": c.get("close_time")}
                    for c, p in zip(eligible[-20:], pnls[-20:])],
        "top_wins": [_brief(c, p) for c, p in reversed(ranked[-5:]) if p > 0],
        "top_losses": [_brief(c, p) for c, p in ranked[:5] if p < 0],
        "hold_distribution": [{"bucket": name, "count": hold.get(name, 0)} for _, name in HOLD_BUCKETS],
        "by_direction": by_dir, "liquidated_campaigns": sum(1 for c in campaigns if c.get("liquidated")),
        "adl_campaigns": sum(1 for c in campaigns if c.get("adl")),
        "pnl_histogram": _histogram(pnls), "scatter": scatter[-3000:],
    }


def concentration(pnls, top=0.10):
    """How much of the profit came from the best trades, under two denominators.

    ``*_share`` divide by the sum of winning campaigns' net PnL (fees deducted,
    attributed funding included; not a before-fee gross). ``*_of_net`` divide
    by the net of all evaluated campaigns and are None when that net is <= 0.
    """
    wins = sorted((x for x in pnls if x > 0), reverse=True)
    total = sum(wins)
    if not pnls or not total:
        return {"top_share": None, "largest_share": None, "top_count": 0, "pareto": []}
    net = sum(pnls)
    k = max(1, round(len(pnls) * top))
    # Pareto curve over all evaluated campaigns ranked by PnL, at most ~100 points.
    ranked = sorted(pnls, reverse=True)
    step = max(1, len(ranked) // 100)
    acc, pareto = 0.0, []
    for i, x in enumerate(ranked, 1):
        acc += x
        if i % step == 0 or i == len(ranked):
            pareto.append({"share_of_campaigns": i / len(ranked), "share_of_profit": acc / total})
    of_net = (lambda x: x / net) if net > 0 else (lambda x: None)
    return {"top_share": sum(wins[:k]) / total, "largest_share": wins[0] / total, "top_count": k,
            "top5_share": sum(wins[:5]) / total,
            "largest_of_net": of_net(wins[0]), "top5_of_net": of_net(sum(wins[:5])), "top_of_net": of_net(sum(wins[:k])),
            "profit_without_top": net - sum(wins[:k]),
            "profit_without_best": net - wins[0], "profit_without_top5": net - sum(wins[:5]),
            "net": net, "pareto": pareto}


def by_coin(campaigns, operations, funding=None, prices=None):
    """Per-coin realized, fees (USD), funding and campaign results, sorted by net."""
    rows = defaultdict(lambda: {"realized": 0.0, "fees": 0.0, "funding": 0.0, "volume": 0.0, "fills": 0,
                                "campaigns": 0, "wins": 0, "long_pnl": 0.0, "short_pnl": 0.0, "market_type": None,
                                "fee_missing": 0, "_fills": set()})
    for o in operations:
        row = rows[o["coin"]]
        row["realized"] += _f(o.get("closed_pnl")) or 0.0
        usd, _ = fee_usd(o, prices)
        if usd is None:
            row["fee_missing"] += 1
        else:
            row["fees"] += usd
        row["volume"] += _f(o.get("notional")) or 0.0
        row["_fills"].add(o.get("fill_id") or id(o))
        row["market_type"] = row["market_type"] or o.get("market_type")
    for e in funding or []:
        d = e.get("delta") or {}
        if d.get("coin"):
            rows[str(d["coin"])]["funding"] += _f(d.get("usdc")) or 0.0
    for c in campaigns:
        if not c.get("eligible_for_win_rate"):
            continue
        pnl = _pnl(c)
        if pnl is None:
            continue
        row = rows[c["coin"]]
        row["campaigns"] += 1
        row["wins"] += pnl > 0
        row["long_pnl" if c["direction"] == "long" else "short_pnl"] += pnl
    out = []
    for coin, r in rows.items():
        r["fills"] = len(r.pop("_fills"))
        r["net"] = r["realized"] - r["fees"] + r["funding"]
        r["net_complete"] = r["fee_missing"] == 0
        r["win_rate"] = r["wins"] / r["campaigns"] if r["campaigns"] else None
        r["win_rate_ci95"] = wilson(r["wins"], r["campaigns"])
        r["ret_on_volume_bps"] = r["net"] / r["volume"] * 1e4 if r["volume"] else None
        out.append({"coin": coin, **r})
    return sorted(out, key=lambda r: -r["net"])


def _histogram(values, bins=30):
    if not values:
        return []
    lo, hi = min(values), max(values)
    if lo == hi:
        return [{"from": lo, "to": hi, "count": len(values)}]
    # Symmetric log-ish clipping: ignore the outer 1% so one trade does not flatten the chart.
    s = sorted(values)
    lo, hi = s[int(len(s) * 0.01)], s[min(len(s) - 1, int(len(s) * 0.99))]
    width = (hi - lo) / bins or 1
    counts = [0] * bins
    for v in values:
        counts[min(bins - 1, max(0, int((v - lo) / width)))] += 1
    return [{"from": lo + i * width, "to": lo + (i + 1) * width, "count": c} for i, c in enumerate(counts)]


def _automation_evidence(order_times):
    """Observable rhythm facts only; public fills cannot prove who placed an order."""
    n = len(order_times)
    if n < BOT_MIN_ORDERS:
        return {"status": "insufficient_data", "orders": n, "min_orders": BOT_MIN_ORDERS}
    ts = sorted(order_times)
    gaps = [(b - a) / 1000 for a, b in zip(ts, ts[1:]) if b > a]
    hours = Counter(datetime.fromtimestamp(t / 1000, timezone.utc).hour for t in ts)
    mean_gap = _mean(gaps)
    sd_gap = math.sqrt(sum((g - mean_gap) ** 2 for g in gaps) / len(gaps)) if gaps else None
    quiet = 24 - len(hours)
    return {"status": "ok", "orders": n, "hours_active": len(hours), "hours_quiet": quiet,
            "median_gap_s": _median(gaps), "gap_cv": sd_gap / mean_gap if mean_gap else None,
            "sub_second_share": sum(1 for g in gaps if g < 1) / len(gaps) if gaps else None,
            "note": "仅为节奏特征，公开成交无法确定操作者是程序还是人"}


def activity(operations, orders=None):
    ops = [o for o in operations if o.get("time") is not None]
    if not ops:
        return {}
    days = {o["time"] // DAY_MS for o in ops}
    first, last = min(o["time"] for o in ops), max(o["time"] for o in ops)
    span_days = last // DAY_MS - first // DAY_MS + 1        # calendar days, so the ratio is <= 100%
    heat = defaultdict(lambda: [0, 0.0, 0])
    actions = Counter(o.get("action") for o in ops)
    notional = lambda o: _f(o.get("notional")) or 0.0
    total = sum(notional(o) for o in ops)
    by_coin_rows = defaultdict(lambda: {"volume": 0.0, "fills": 0, "realized": 0.0, "fees": 0.0})
    maker = taker = unknown_maker = twap = liq = adl = hip3 = spot = 0.0
    maker_n = taker_n = 0
    seen, reversal_ids, oids, order_first = set(), set(), set(), {}
    liq_events, counterparty = set(), set()
    for o in ops:
        n = notional(o)
        fid = o.get("fill_id") or id(o)
        new_fill = fid not in seen
        seen.add(fid)
        dt = datetime.fromtimestamp(o["time"] / 1000, timezone.utc)
        cell = heat[(dt.weekday(), dt.hour)]
        cell[0] += new_fill
        cell[1] += n
        row = by_coin_rows[o["coin"]]
        row["volume"] += n
        row["fills"] += new_fill
        row["realized"] += _f(o.get("closed_pnl")) or 0.0
        row["fees"] += _f(o.get("fee")) or 0.0
        if o.get("maker") is True:
            maker += n
            maker_n += new_fill
        elif o.get("maker") is False:
            taker += n
            taker_n += new_fill
        else:
            unknown_maker += n
        twap += n if o.get("twap") else 0.0
        liq += n if o.get("liquidation") else 0.0
        adl += n if o.get("adl") else 0.0
        hip3 += n if ":" in o["coin"] and o.get("market_type") == "perp" else 0.0
        spot += n if o.get("market_type") == "spot" else 0.0
        if o.get("reversal") or (o.get("reversal") is None and ">" in str(o.get("dir_raw") or "")):
            reversal_ids.add(fid)
        if o.get("oid") and not o.get("twap"):
            oids.add(o["oid"])
            if o["oid"] not in order_first:
                order_first[o["oid"]] = o["time"]
                cell[2] += 1
        if o.get("liquidation") and new_fill:
            liq_events.add((o["coin"], o["time"] // 1000))
        if o.get("liquidation_counterparty") and new_fill:
            counterparty.add(fid)
    share = (lambda x: x / total) if total else (lambda x: None)
    fills = len(seen)
    coins = sorted(({"coin": k, **v} for k, v in by_coin_rows.items()), key=lambda r: -r["volume"])
    out = {
        "fills": fills, "legs": len(ops), "active_days": len(days), "span_days": span_days,
        "first_fill_ms": first, "last_fill_ms": last,
        "active_day_ratio": len(days) / span_days,
        "fills_per_active_day": fills / len(days) if days else None,
        "orders": len(oids), "orders_per_active_day": len(oids) / len(days) if days else None,
        "orders_basis": "unique oid, TWAP slices excluded",
        "opens": actions.get("open", 0), "adds": actions.get("add", 0),
        "reduces": actions.get("reduce", 0), "closes": actions.get("close", 0), "reversals": len(reversal_ids),
        "volume": total,
        "maker_share": maker / (maker + taker) if maker + taker else None,
        "maker_share_count": maker_n / (maker_n + taker_n) if maker_n + taker_n else None,
        "maker_unknown_share": share(unknown_maker),
        "twap_share": share(twap), "hip3_share": share(hip3), "spot_share": share(spot),
        "liquidation_fills": len({o.get("fill_id") or id(o) for o in ops if o.get("liquidation")}),
        "liquidation_events": len(liq_events), "liquidation_volume": liq,
        "liquidation_counterparty_fills": len(counterparty),
        "adl_fills": len({o.get("fill_id") or id(o) for o in ops if o.get("adl")}), "adl_volume": adl,
        "heatmap": [{"weekday": w, "hour": h, "fills": v[0], "volume": v[1], "orders": v[2]}
                    for (w, h), v in sorted(heat.items())],
        "top_coins": coins[:15], "coin_count": len(coins),
        "automation": _automation_evidence(list(order_first.values())),
    }
    if orders:
        # historicalOrders rows are status snapshots; keep the latest status per oid.
        latest = {}
        for o in orders:
            inner = o.get("order") or {}
            oid = inner.get("oid", o.get("oid"))
            key = oid if oid is not None else id(o)
            stamp = o.get("statusTimestamp") or 0
            if key not in latest or stamp >= (latest[key].get("statusTimestamp") or 0):
                latest[key] = o
        uniq = list(latest.values())
        statuses = Counter(str(o.get("status")) for o in uniq)
        types = Counter(str(o.get("orderType") or (o.get("order") or {}).get("orderType")) for o in uniq)
        canceled = sum(v for k, v in statuses.items() if "cancel" in k.lower())
        out["orders_sample"] = {"count": len(uniq), "rows": len(orders), "status": dict(statuses.most_common()),
                                "types": dict(types.most_common()),
                                "cancel_rate": canceled / len(uniq) if uniq else None,
                                "note": "historicalOrders 仅为最近约 2000 条订单样本；按 oid 取最新状态"}
    return out


def costs(operations, funding, gross_trade_profit=None, volume=None, snapshot=None, prices=None):
    """Fees (USD), funding split per payment, and cost ratios.

    ``gross_trade_profit`` is the sum of positive closedPnl of evaluated
    campaigns, i.e. winning trades before fees; ``fee_drag`` = fees / it.
    """
    fees = 0.0
    fee_missing = fee_approx = 0
    by_day_fee, by_day_fund = defaultdict(float), defaultdict(float)
    for o in operations:
        usd, approx = fee_usd(o, prices)
        if usd is None:
            fee_missing += 1
            continue
        fees += usd
        fee_approx += approx
        if o.get("time") is not None:
            by_day_fee[o["time"] // DAY_MS * DAY_MS] += usd
    builder = sum(_f(o.get("builder_fee")) or 0.0 for o in operations)
    funding_by_coin = defaultdict(float)
    received = paid = 0.0
    events = 0
    for e in funding or []:
        d = e.get("delta") or {}
        amount = _f(d.get("usdc"))
        if amount is None:
            continue
        events += 1
        funding_by_coin[str(d.get("coin") or "")] += amount
        if amount > 0:
            received += amount
        else:
            paid -= amount
        by_day_fund[int(e["time"]) // DAY_MS * DAY_MS] += amount
    cum, a, b = [], 0.0, 0.0
    for day in sorted(set(by_day_fee) | set(by_day_fund)):
        a += by_day_fee.get(day, 0.0)
        b += by_day_fund.get(day, 0.0)
        cum.append({"time": day, "fees": a, "funding": b})
    funding_total = received - paid
    vol = volume if volume is not None else sum(_f(o.get("notional")) or 0.0 for o in operations)
    rates = (snapshot or {}).get("fees") or {}
    gross = gross_trade_profit if gross_trade_profit and gross_trade_profit > 0 else None
    return {
        "fees": fees, "fee_basis": "usd", "fee_missing_count": fee_missing, "fee_approx_count": fee_approx,
        "builder_fees": builder, "funding_net": funding_total,
        "funding_received": received, "funding_paid": paid, "funding_events": events,
        "fee_bps": fees / vol * 1e4 if vol else None,
        "gross_trade_profit": gross_trade_profit,
        "fee_drag": fees / gross if gross else None,
        "cost_to_gross": (fees + paid - received if paid > received else fees) / gross if gross else None,
        "taker_rate": _f(rates.get("cross_rate")), "maker_rate": _f(rates.get("add_rate")),
        "referral_discount": _f(rates.get("referral_discount")), "staking_discount": _f(rates.get("staking_discount")),
        "funding_by_coin": sorted(({"coin": k, "funding": v} for k, v in funding_by_coin.items()),
                                  key=lambda r: r["funding"]),
        "cumulative": cum,
    }
