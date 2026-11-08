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
    buckets = [{"bucket": f"永续 {d['dex'] or 'native'}", "value": _f(d.get("account_value"))}
               for d in snapshot.get("dexes", {}).values()]
    spot_value = sum(_f(r.get("value")) or 0 for r in snapshot.get("spot", []))
    if snapshot.get("mode") in ("unifiedAccount", "portfolioMargin", "dexAbstraction"):
        # Unified accounts keep perp margin as spot USDC "hold"; count it once.
        spot_value -= sum(_f(r.get("hold")) or 0 for r in snapshot.get("spot", []) if r.get("coin") == "USDC")
    buckets.append({"bucket": "现货", "value": spot_value})
    buckets.append({"bucket": "金库", "value": sum(_f(v.get("equity")) or 0 for v in snapshot.get("vaults", []))})
    buckets.append({"bucket": "质押 HYPE", "value": _f((snapshot.get("staking") or {}).get("value_usd"))})
    buckets.append({"bucket": "子账户", "value": sum(_f(s.get("account_value")) or 0 for s in snapshot.get("sub_accounts", []))})
    equity = _f((composition or {}).get("official_equity")) or _f((composition or {}).get("estimated_total"))
    near = [p for p in positions if p["liq_distance"] is not None]
    return {
        "time": snapshot.get("time"), "mode": snapshot.get("mode"), "equity": equity,
        "positions": positions, "position_count": len(positions),
        "gross_exposure": gross, "long_exposure": long, "short_exposure": gross - long,
        "net_exposure": 2 * long - gross, "leverage": gross / equity if equity else None,
        "concentration_hhi": hhi,
        "nearest_liquidation": min(near, key=lambda p: p["liq_distance"]) if near else None,
        "buckets": [b for b in buckets if b["value"]],
        "spot": snapshot.get("spot", []), "vaults": snapshot.get("vaults", []),
        "staking": snapshot.get("staking"), "sub_accounts": snapshot.get("sub_accounts", []),
        "open_orders": snapshot.get("open_orders"), "composition": composition,
        "errors": snapshot.get("errors") or {},
    }


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
        "composition": (current or {}).get("composition"),
        "snapshot_errors": (current or {}).get("errors"),
        "mtm_flags": mtm_flags or {}, "meta_errors": meta_errors or [],
    }
