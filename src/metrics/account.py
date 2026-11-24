"""Current account state, rule-based profile tags and data-quality summary."""
from __future__ import annotations

DAY_MS = 86_400_000


def _f(value):
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def current_state(snapshot, composition=None):
    if not snapshot:
        return {}
    positions = []
    for dex in snapshot.get("dexes", {}).values():
        for p in dex["positions"]:
            mark, liq = _f(p.get("mark_px")), _f(p.get("liquidation_px"))
            distance = abs(mark - liq) / mark if mark and liq else None
            positions.append({**p, "dex": dex["dex"] or "native", "notional": _f(p.get("position_value")),
                              "liq_distance": distance})
    positions.sort(key=lambda p: -(p["notional"] or 0))
    gross = sum(p["notional"] or 0 for p in positions)
    long = sum(p["notional"] or 0 for p in positions if p["side"] == "long")
    hhi = sum(((p["notional"] or 0) / gross) ** 2 for p in positions) if gross else None
    # scope "trading": inside the official account value; "related": linked assets
    # reported alongside (sub-accounts, vaults) that the official value does not include.
    buckets = [{"bucket": f"永续 {d['dex'] or 'native'}", "value": _f(d.get("account_value")), "scope": "trading"}
               for d in snapshot.get("dexes", {}).values()]
    spot_value = sum(_f(r.get("value")) or 0 for r in snapshot.get("spot", []))
    if snapshot.get("mode") in ("unifiedAccount", "portfolioMargin", "dexAbstraction"):
        # Unified accounts keep perp margin as spot USDC "hold"; count it once.
        spot_value -= sum(_f(r.get("hold")) or 0 for r in snapshot.get("spot", []) if r.get("coin") == "USDC")
    buckets.append({"bucket": "现货", "value": spot_value, "scope": "trading"})
    buckets.append({"bucket": "质押 HYPE", "value": _f((snapshot.get("staking") or {}).get("value_usd")), "scope": "trading"})
    buckets.append({"bucket": "金库", "value": sum(_f(v.get("equity")) or 0 for v in snapshot.get("vaults", [])),
                    "scope": "related"})
    buckets.append({"bucket": "子账户", "value": sum(_f(s.get("account_value")) or 0 for s in snapshot.get("sub_accounts", [])),
                    "scope": "related"})
    official = _f((composition or {}).get("official_equity"))
    # An official 0 is a real 0; only a missing official value falls back to the estimate.
    equity = official if official is not None else _f((composition or {}).get("estimated_total"))
    equity_source = "official" if official is not None else ("estimated" if equity is not None else None)
    near = [p for p in positions if p["liq_distance"] is not None]
    dexes = list(snapshot.get("dexes", {}).values())
    known = lambda key: [v for v in (_f(d.get(key)) for d in dexes) if v is not None]
    margin_used, withdrawable = known("margin_used"), known("withdrawable")
    upnl = [v for v in (_f(p.get("unrealized_pnl")) for p in positions) if v is not None]
    margin_total = sum(margin_used) if margin_used else None
    errors = snapshot.get("errors") or {}
    return {
        "time": snapshot.get("time"), "mode": snapshot.get("mode"), "equity": equity,
        "equity_source": equity_source, "equity_state": None if equity is None else
        ("negative" if equity < 0 else "zero" if equity == 0 else "positive"),
        "positions": positions, "position_count": len(positions),
        "gross_exposure": gross, "long_exposure": long, "short_exposure": gross - long,
        "net_exposure": 2 * long - gross,
        # Account effective leverage: perp gross notional / official whole-account value.
        "leverage": gross / equity if equity and equity > 0 else None,
        "margin_used": margin_total,
        "margin_ratio": margin_total / equity if margin_total is not None and equity and equity > 0 else None,
        "withdrawable": sum(withdrawable) if withdrawable else None,
        "unrealized_pnl": sum(upnl) if positions else 0.0, "unrealized_pnl_scope": "perp positions only",
        "stress": stress_test(positions, equity),
        "concentration_hhi": hhi,
        "nearest_liquidation": min(near, key=lambda p: p["liq_distance"]) if near else None,
        "buckets": [b for b in buckets if b["value"]],
        "spot": snapshot.get("spot", []), "vaults": snapshot.get("vaults", []),
        "staking": snapshot.get("staking"), "sub_accounts": snapshot.get("sub_accounts", []),
        # None = the request failed (unknown), 0 = confirmed none.
        "open_orders": None if "frontendOpenOrders" in errors else snapshot.get("open_orders"),
        "open_orders_detail": snapshot.get("open_orders_detail") or [],
        "fetched_at": snapshot.get("fetched_at") or {},
        "composition": composition,
        "errors": errors,
    }


def stress_test(positions, equity, shocks=(0.05, 0.10, 0.20)):
    """Instant adverse price move on every open perp position (no funding, no liquidation path).

    Each position is moved against its own side; the loss is a linear
    approximation from notional and ignores cross-position hedges' correlation.
    """
    rows = []
    for shock in shocks:
        loss = sum((p["notional"] or 0) * shock for p in positions)
        rows.append({"shock": shock, "loss_usd": loss,
                     "pct_equity": loss / equity if equity and equity > 0 else None,
                     "liquidated": sum(1 for p in positions if p.get("liq_distance") is not None
                                       and p["liq_distance"] <= shock)})
    return {"scenarios": rows, "basis": "all positions move against their side at once"}


def evidence_strength(days, behavior, months=None):
    """Statistical evidence tier, separate from data completeness."""
    active = sum(1 for d in days or [] if d.get("ret") is not None and abs(d.get("pnl") or 0) > 0)
    trades = (behavior or {}).get("campaigns_evaluated") or 0
    months = months or 0
    if active >= 90 and trades >= 100 and months >= 6:
        tier = "solid"
    elif active >= 30 and trades >= 20 and months >= 2:
        tier = "fair"
    else:
        tier = "thin"
    need = {"solid": {"active_days": 90, "trades": 100, "months": 6},
            "fair": {"active_days": 30, "trades": 20, "months": 2}}
    nxt = "fair" if tier == "thin" else "solid" if tier == "fair" else None
    gap = {k: max(0, v - {"active_days": active, "trades": trades, "months": months}[k])
           for k, v in need[nxt].items()} if nxt else {}
    return {"tier": tier, "active_days": active, "trades": trades, "months": months,
            "next_tier": nxt, "missing": gap, "thresholds": need}


PROFILE_RULES = {
    "style": "持仓中位时长 <1小时 剥头皮；<1天 日内；<14天 波段；否则 长线持有",
    "bias": "做多回合占比 >65% 偏多；<35% 偏空；否则 多空均衡",
    "leverage": "有效杠杆均值 <2 低杠杆；<5 中杠杆；否则 高杠杆",
    "concentration": "成交额前三币种占比 >70% 集中；否则 分散",
    "execution": "maker 成交额占比 >60% 挂单型；<20% 吃单型；否则 混合",
    "activity": "活跃天数占比 >60% 高频活跃；>20% 规律活跃；否则 低频",
    "size": "当前净值 >1000万 巨鲸；>100万 大户；>10万 中户；否则 散户",
}


def profile(behavior, activity, risk, current):
    tags = {}
    hold = (behavior or {}).get("median_hold_hours")
    if hold is not None:
        tags["style"] = "剥头皮" if hold < 1 else "日内" if hold < 24 else "波段" if hold < 24 * 14 else "长线持有"
    by_dir = (behavior or {}).get("by_direction") or {}
    long_n, short_n = (by_dir.get("long") or {}).get("count", 0), (by_dir.get("short") or {}).get("count", 0)
    if long_n + short_n:
        share = long_n / (long_n + short_n)
        tags["bias"] = "偏多" if share > 0.65 else "偏空" if share < 0.35 else "多空均衡"
    lev = (risk or {}).get("leverage_mean")
    if lev is not None:
        tags["leverage"] = "低杠杆" if lev < 2 else "中杠杆" if lev < 5 else "高杠杆"
    coins = (activity or {}).get("top_coins") or []
    total = (activity or {}).get("volume")
    if coins and total:
        tags["concentration"] = "集中" if sum(c["volume"] for c in coins[:3]) / total > 0.7 else "分散"
    maker = (activity or {}).get("maker_share")
    if maker is not None:
        tags["execution"] = "挂单型" if maker > 0.6 else "吃单型" if maker < 0.2 else "混合"
    ratio = (activity or {}).get("active_day_ratio")
    if ratio is not None:
        tags["activity"] = "高频活跃" if ratio > 0.6 else "规律活跃" if ratio > 0.2 else "低频"
    equity = (current or {}).get("equity")
    if equity is not None:
        tags["size"] = "巨鲸" if equity > 1e7 else "大户" if equity > 1e6 else "中户" if equity > 1e5 else "散户"
    return {"tags": tags, "rules": PROFILE_RULES}


def quality(truncation, reconciliation, paths_summary, series, current, mtm_flags, meta_errors=None):
    breaks = (paths_summary or {}).get("breaks") or []
    return {
        "truncation": truncation, "reconciliation": {k: v for k, v in (reconciliation or {}).items() if k != "intervals"},
        "reconciliation_intervals": (reconciliation or {}).get("intervals", []),
        "position_breaks": breaks, "position_break_count": (paths_summary or {}).get("break_count", 0),
        "snapshot_corrected_gaps": (paths_summary or {}).get("snapshot_corrected_gaps", 0),
        "snapshot_mismatch": (paths_summary or {}).get("snapshot_mismatch", []),
        "seeded_coins": (paths_summary or {}).get("seeded_coins", []),
        "series_sources": {s: sum(1 for d in (series or {}).get("days", []) if d["source"] == s)
                           for s in ("official", "hybrid", "interpolated")},
        "skipped_return_days": (series or {}).get("skipped_return_days"),
        "return_base_floor": (series or {}).get("min_base"),
        # Share of daily rows that are not bracketed by official samples on both ends.
        "interpolated_share": (sum(1 for d in (series or {}).get("days", []) if d["source"] != "official")
                               / len((series or {}).get("days")) if (series or {}).get("days") else None),
        "composition": (current or {}).get("composition"),
        "snapshot_errors": (current or {}).get("errors"),
        "mtm_flags": mtm_flags or {}, "meta_errors": meta_errors or [],
    }
