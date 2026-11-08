"""Trading behavior and cost metrics from fills (engine operations/campaigns).

Campaign statistics use only campaigns with a fully observed lifecycle
(``eligible_for_win_rate``); the count of excluded campaigns is reported.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone

DAY_MS = 86_400_000
HOLD_BUCKETS = ((0, "<5分钟"), (300_000, "5分钟-1小时"), (3_600_000, "1-4小时"), (14_400_000, "4-24小时"),
                (DAY_MS, "1-7天"), (7 * DAY_MS, "1-4周"), (28 * DAY_MS, ">4周"))


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


def _bucket(ms):
    label = HOLD_BUCKETS[0][1]
    for lower, name in HOLD_BUCKETS:
        if ms >= lower:
            label = name
    return label


def _streaks(values):
    best_win = best_loss = run = 0
    for v in values:
        if v > 0:
            run = run + 1 if run > 0 else 1
        elif v < 0:
            run = run - 1 if run < 0 else -1
        else:
            run = 0
        best_win, best_loss = max(best_win, run), min(best_loss, run)
    return best_win, -best_loss


def campaign_stats(campaigns):
    eligible = sorted((c for c in campaigns if c.get("eligible_for_win_rate")), key=lambda c: c["close_time"])
    pnls = [_f(c.get("net_pnl_with_funding")) if c.get("net_pnl_with_funding") is not None else _f(c["net_pnl"])
            for c in eligible]
    wins, losses = [x for x in pnls if x > 0], [x for x in pnls if x < 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    win_streak, loss_streak = _streaks(pnls)
    durations = [c["duration_ms"] for c in eligible if c.get("duration_ms") is not None]
    hold = Counter(_bucket(d) for d in durations)
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
    return {
        "campaigns_total": len(campaigns), "campaigns_evaluated": len(eligible),
        "campaigns_excluded": len(campaigns) - len(eligible),
        "wins": len(wins), "losses": len(losses), "gross_win": gross_win, "gross_loss": gross_loss,
        "net_pnl": sum(pnls),
        "win_rate": len(wins) / len(pnls) if pnls else None,
        "win_rate_strict": len(wins) / (len(wins) + len(losses)) if wins or losses else None,
        "avg_win": gross_win / len(wins) if wins else None,
        "avg_loss": -gross_loss / len(losses) if losses else None,
        "payoff_ratio": (gross_win / len(wins)) / (gross_loss / len(losses)) if wins and losses else None,
        "profit_factor": gross_win / gross_loss if gross_loss else None,
        "expectancy": sum(pnls) / len(pnls) if pnls else None,
        "max_win": max(pnls) if pnls else None, "max_loss": min(pnls) if pnls else None,
        "max_win_streak": win_streak, "max_loss_streak": loss_streak,
        "median_hold_hours": (_median(durations) or 0) / 3_600_000 if durations else None,
        "mean_hold_hours": sum(durations) / len(durations) / 3_600_000 if durations else None,
        # Net over everything won and lost: 1 = never lost, 0 = break-even churn.
        "efficiency_ratio": sum(pnls) / (gross_win + gross_loss) if gross_win + gross_loss else None,
        "concentration": concentration(pnls),
        "hold_distribution": [{"bucket": name, "count": hold.get(name, 0)} for _, name in HOLD_BUCKETS],
        "by_direction": by_dir, "liquidated_campaigns": sum(1 for c in campaigns if c.get("liquidated")),
        "adl_campaigns": sum(1 for c in campaigns if c.get("adl")),
        "pnl_histogram": _histogram(pnls), "scatter": scatter[-3000:],
    }


def concentration(pnls, top=0.10):
    """How much of the gross profit came from the best ``top`` share of campaigns."""
    wins = sorted((x for x in pnls if x > 0), reverse=True)
    total = sum(wins)
    if not pnls or not total:
        return {"top_share": None, "largest_share": None, "top_count": 0, "pareto": []}
    k = max(1, round(len(pnls) * top))
    # Pareto curve over all evaluated campaigns ranked by PnL, at most ~100 points.
    ranked = sorted(pnls, reverse=True)
    step = max(1, len(ranked) // 100)
    acc, pareto = 0.0, []
    for i, x in enumerate(ranked, 1):
        acc += x
        if i % step == 0 or i == len(ranked):
            pareto.append({"share_of_campaigns": i / len(ranked), "share_of_profit": acc / total})
    return {"top_share": sum(wins[:k]) / total, "largest_share": wins[0] / total, "top_count": k,
            "profit_without_top": sum(pnls) - sum(wins[:k]), "pareto": pareto}


def by_coin(campaigns, operations, funding=None):
    """Per-coin realized, fees, funding and campaign results, sorted by net."""
    rows = defaultdict(lambda: {"realized": 0.0, "fees": 0.0, "funding": 0.0, "volume": 0.0, "fills": 0,
                                "campaigns": 0, "wins": 0, "long_pnl": 0.0, "short_pnl": 0.0, "market_type": None})
    for o in operations:
        row = rows[o["coin"]]
        row["realized"] += _f(o.get("closed_pnl")) or 0.0
        if o.get("fee_token") in (None, "USDC"):
            row["fees"] += _f(o.get("fee")) or 0.0
        row["volume"] += _f(o.get("notional")) or 0.0
        row["fills"] += 1
        row["market_type"] = row["market_type"] or o.get("market_type")
    for e in funding or []:
        d = e.get("delta") or {}
        if d.get("coin"):
            rows[str(d["coin"])]["funding"] += _f(d.get("usdc")) or 0.0
    for c in campaigns:
        if not c.get("eligible_for_win_rate"):
            continue
        pnl = _f(c.get("net_pnl_with_funding")) if c.get("net_pnl_with_funding") is not None else _f(c.get("net_pnl"))
        if pnl is None:
            continue
        row = rows[c["coin"]]
        row["campaigns"] += 1
        row["wins"] += pnl > 0
        row["long_pnl" if c["direction"] == "long" else "short_pnl"] += pnl
    out = []
    for coin, r in rows.items():
        r["net"] = r["realized"] - r["fees"] + r["funding"]
        r["win_rate"] = r["wins"] / r["campaigns"] if r["campaigns"] else None
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


def activity(operations, orders=None):
    ops = [o for o in operations if o.get("time") is not None]
    if not ops:
        return {}
    days = {o["time"] // DAY_MS for o in ops}
    span_days = (max(o["time"] for o in ops) - min(o["time"] for o in ops)) / DAY_MS + 1
    heat = defaultdict(lambda: [0, 0.0])
    actions = Counter(o.get("action") for o in ops)
    notional = lambda o: _f(o.get("notional")) or 0.0
    total = sum(notional(o) for o in ops) or 1.0
    by_coin = defaultdict(lambda: {"volume": 0.0, "fills": 0, "realized": 0.0, "fees": 0.0})
    maker = taker = twap = liq = adl = hip3 = spot = 0.0
    reversals = 0
    for o in ops:
        n = notional(o)
        dt = datetime.fromtimestamp(o["time"] / 1000, timezone.utc)
        cell = heat[(dt.weekday(), dt.hour)]
        cell[0] += 1
        cell[1] += n
        row = by_coin[o["coin"]]
        row["volume"] += n
        row["fills"] += 1
        row["realized"] += _f(o.get("closed_pnl")) or 0.0
        row["fees"] += _f(o.get("fee")) or 0.0
        if o.get("maker") is True:
            maker += n
        elif o.get("maker") is False:
            taker += n
        twap += n if o.get("twap") else 0.0
        liq += n if o.get("liquidation") else 0.0
        adl += n if o.get("adl") else 0.0
        hip3 += n if ":" in o["coin"] and o.get("market_type") == "perp" else 0.0
        spot += n if o.get("market_type") == "spot" else 0.0
        reversals += ">" in str(o.get("dir_raw") or "")
    coins = sorted(({"coin": k, **v} for k, v in by_coin.items()), key=lambda r: -r["volume"])
    out = {
        "fills": len(ops), "active_days": len(days), "span_days": span_days,
        "active_day_ratio": len(days) / span_days if span_days else None,
        "fills_per_active_day": len(ops) / len(days) if days else None,
        "opens": actions.get("open", 0), "adds": actions.get("add", 0),
        "reduces": actions.get("reduce", 0), "closes": actions.get("close", 0), "reversals": reversals,
        "volume": total, "maker_share": maker / (maker + taker) if maker + taker else None,
        "twap_share": twap / total, "hip3_share": hip3 / total, "spot_share": spot / total,
        "liquidation_fills": sum(1 for o in ops if o.get("liquidation")), "liquidation_volume": liq,
        "adl_fills": sum(1 for o in ops if o.get("adl")), "adl_volume": adl,
        "heatmap": [{"weekday": w, "hour": h, "fills": v[0], "volume": v[1]} for (w, h), v in sorted(heat.items())],
        "top_coins": coins[:15], "coin_count": len(coins),
    }
    if orders:
        statuses = Counter(str(o.get("status")) for o in orders)
        types = Counter(str(o.get("orderType")) for o in orders)
        canceled = sum(v for k, v in statuses.items() if "cancel" in k.lower())
        out["orders_sample"] = {"count": len(orders), "status": dict(statuses.most_common()),
                                "types": dict(types.most_common()),
                                "cancel_rate": canceled / len(orders) if orders else None,
                                "note": "historicalOrders 仅为最近约 2000 条订单样本"}
    return out


def costs(operations, funding, gross_pnl=None, volume=None, snapshot=None):
    fees = sum(_f(o.get("fee")) or 0.0 for o in operations if o.get("fee_token") in (None, "USDC"))
    builder = sum(_f(o.get("builder_fee")) or 0.0 for o in operations)
    funding_by_coin = defaultdict(float)
    cum_fees, cum_funding = [], []
    for e in funding or []:
        d = e.get("delta") or {}
        funding_by_coin[str(d.get("coin") or "")] += _f(d.get("usdc")) or 0.0
    by_day_fee, by_day_fund = defaultdict(float), defaultdict(float)
    for o in operations:
        if o.get("time") is not None:
            by_day_fee[o["time"] // DAY_MS * DAY_MS] += _f(o.get("fee")) or 0.0
    for e in funding or []:
        by_day_fund[int(e["time"]) // DAY_MS * DAY_MS] += _f((e.get("delta") or {}).get("usdc")) or 0.0
    a = b = 0.0
    for day in sorted(set(by_day_fee) | set(by_day_fund)):
        a += by_day_fee.get(day, 0.0)
        b += by_day_fund.get(day, 0.0)
        cum_fees.append({"time": day, "fees": a, "funding": b})
    funding_total = sum(funding_by_coin.values())
    vol = volume or sum(_f(o.get("notional")) or 0.0 for o in operations)
    rates = (snapshot or {}).get("fees") or {}
    return {
        "fees": fees, "builder_fees": builder, "funding_net": funding_total,
        "funding_received": sum(v for v in funding_by_coin.values() if v > 0),
        "funding_paid": -sum(v for v in funding_by_coin.values() if v < 0),
        "fee_bps": fees / vol * 1e4 if vol else None,
        "cost_to_gross": (fees - min(funding_total, 0.0)) / gross_pnl if gross_pnl and gross_pnl > 0 else None,
        "taker_rate": _f(rates.get("cross_rate")), "maker_rate": _f(rates.get("add_rate")),
        "referral_discount": _f(rates.get("referral_discount")), "staking_discount": _f(rates.get("staking_discount")),
        "funding_by_coin": sorted(({"coin": k, "funding": v} for k, v in funding_by_coin.items()),
                                  key=lambda r: r["funding"]),
        "cumulative": cum_fees,
    }
