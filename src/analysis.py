"""Wallet analysis: snapshot-anchored model, reconciliation and metrics.

``analyze_wallet`` turns one wallet cache (plus optional live client) into a
single JSON-safe ``model`` dict that the report, CLI and TUI all read.
Official numbers are the truth; reconstructed pieces explain them and every
gap is published as a residual with its evidence.
"""
from __future__ import annotations

import time
from decimal import Decimal

from account_history import latest_window_stats, official_series
from engine import analyze_fills, ordered_fills
from bisect import bisect_right

from metrics.account import current_state, evidence_strength, profile, quality
from metrics.excursion import excursions, summarize as excursion_stats
from metrics.behavior import activity, by_coin, campaign_stats, costs
from metrics.performance import market_model, performance, risk
from metrics.scorecard import scorecard
from metrics.series import daily_series, window_slice, window_start
from model.flows import classify_all, net_capital_flows
from model.mtm import daily_perp_mtm
from model.positions import build_paths, position_snapshots, summarize_paths
from reconcile import detect_truncation, reconcile_pnl
from sources.market_data import DAY_MS, candles, ladder
from sources.snapshots import compose, latest_snapshot

WINDOWS = ("7d", "30d", "90d", "ytd", "all")
WINDOW_LABELS = {"7d": "近7天", "30d": "近30天", "90d": "近90天", "ytd": "今年", "all": "全部"}


def _plain(value):
    """Decimals to float, recursively; keeps the model JSON-serializable."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def excursion_summary(excursion, since):
    return excursion_stats(excursion["rows"], since) if excursion else None


def analyze_wallet(store, client=None, *, log=print, cancel_check=None, now_ms=None, mae=False):
    def check():
        if cancel_check:
            cancel_check()

    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    meta = store.get_meta("asset_meta", {}) or {}
    spot_prices = store.get_meta("spot_prices", {}) or {}
    snapshot = latest_snapshot(store)
    window_stats = latest_window_stats(store)
    official_total = official_series(store, "total")
    official_perp = official_series(store, "perp")
    official_equity = (official_total[-1].get("equity") if official_total else None)
    composition = compose(snapshot, official_equity) if snapshot else None

    check()
    raw_fills = list(store.rows("fills"))
    fills = ordered_fills(raw_fills)
    funding = list(store.rows("funding"))
    ledger = list(store.rows("ledger"))
    log(f"分析成交 {len(fills):,} 条、资金费 {len(funding):,} 条、资金流水 {len(ledger):,} 条 …")
    for row in funding:
        coin = (row.get("delta") or {}).get("coin", "")
        row["pnl_token"] = (meta.get(coin) or {}).get("pnl_token") or ("USDC" if ":" not in coin else None)
    engine = analyze_fills(fills, funding=funding, asset_meta=meta, wallet=store.wallet)
    paths = build_paths(fills, snapshot, snapshots=position_snapshots(store.rows("position_snapshot")))
    paths_summary = summarize_paths(paths)
    truncation = detect_truncation(fills, paths, official_perp, window_stats)
    log(f"历史完整性：{truncation['status']}；仓位断点 {paths_summary['break_count']} 处。")

    check()
    mtm = None
    if official_perp and snapshot:
        start = max(official_perp[0]["time"], truncation["available_from_ms"] or 0)
        log("按日盯市重建永续盈亏（首次运行需下载日 K 线）…")
        mtm = daily_perp_mtm(fills, funding, paths, snapshot, meta, spot_prices,
                             lambda coin, a, b: candles(client, store, coin, a, b, "1d", log=log),
                             start, min(now, snapshot["time"]))
    reconciliation = (reconcile_pnl(official_perp, mtm, truncation, equity=official_equity) if mtm
                      else {"status": "no_data", "intervals": []})
    if mtm and reconciliation.get("official") is not None:
        log(f"对账：官方 {reconciliation['official']:,.0f}，重建 {reconciliation['reconstructed']:,.0f}，"
            f"残差 {reconciliation['residual']:,.0f}（{reconciliation['status']}）。")
    elif mtm:
        log(f"对账：可比官方采样点不足（{reconciliation['status']}）。")

    check()
    subs = [s["address"] for s in (snapshot or {}).get("sub_accounts", []) if s.get("address")]
    flows = classify_all(ledger, store.wallet, subs, spot_prices)
    series = daily_series(official_total, (mtm or {}).get("days"), now=now)
    days = series["days"]
    current = current_state(snapshot, composition)
    excursion = None
    if mae:
        check()
        log("MAE/MFE：按币种读取 15m/1h/4h/1d K 线（各最近 5000 根，首次需下载）…")
        excursion = excursions(engine["campaigns"], engine["operations"],
                               lambda coin, since: ladder(client, store, coin, since=since, now=now, log=log), log=log)
        log(f"MAE/MFE：评估 {excursion['evaluated']:,} / {excursion['candidates']:,} 个完整回合，"
            f"精度不足 {excursion['coarse']:,} 个，缺 K 线 {excursion['no_candles']:,} 个。")
    exposure = (mtm or {}).get("days")
    equity_at = _equity_lookup(days)
    ledger_flows = _ledger_capital(flows)
    benchmark = _benchmark(client, store, days, log)
    by_window, cards = {}, {}
    for window in WINDOWS:
        part = window_slice(days, window, now)
        stats = window_stats.get({"7d": "week", "30d": "month", "all": "allTime"}.get(window, ""), {})
        # Fill-based pieces use the same window. An empty window stays empty:
        # no daily rows must never fall back to the whole history.
        start = window_start(window, now)
        since = None if start is None else (part[0]["time"] - DAY_MS if part else start)
        ops = [o for o in engine["operations"] if since is None or (o.get("time") or 0) >= since]
        camps = [c for c in engine["campaigns"] if since is None or (c.get("close_time") or 0) >= since]
        fund = [e for e in funding if since is None or int(e["time"]) >= since]
        w_perf = performance(part, official_pnl=stats.get("pnl"), capital_flows=_xirr_flows(ledger_flows, part))
        w_risk = risk(part, exposure_days=exposure)
        w_beh, w_act = campaign_stats(camps, equity_at=equity_at), activity(ops)
        w_cost = costs(ops, fund, gross_trade_profit=w_beh.get("gross_trade_profit"), volume=w_act.get("volume"),
                       snapshot=snapshot, prices=spot_prices)
        w_coins = by_coin(camps, ops, fund, prices=spot_prices)
        w_market = market_model(part, benchmark)
        cards[window] = scorecard(perf=w_perf, risk=w_risk, behavior=w_beh, activity=w_act, costs=w_cost,
                                  official=stats if window in ("7d", "30d", "all") else None,
                                  equity=current.get("equity"), coins=w_coins,
                                  excursion=excursion_summary(excursion, since), market=w_market)
        by_window[window] = {
            "label": WINDOW_LABELS[window],
            "performance": {k: v for k, v in w_perf.items() if k not in ("monthly", "weekly", "twr_index")},
            "risk": {k: v for k, v in w_risk.items()
                     if k not in ("underwater", "rolling_30d", "drawdown_episodes", "usd_drawdown_episodes",
                                  "leverage_series")},
            "official_volume": stats.get("vlm"),
            "by_coin": w_coins[:200],
            "by_direction": w_beh.get("by_direction"),
            "concentration": w_beh.get("concentration"),
            "market": w_market,
            "trades": {k: w_beh.get(k) for k in ("wins", "losses", "flats", "win_rate_strict", "win_rate_strict_ci95",
                                                 "break_even_win_rate", "current_streak", "last_20", "top_wins",
                                                 "top_losses", "r", "independent_trades")},
            "empty": not part,
        }
    perf_all = performance(days, official_pnl=(window_stats.get("allTime") or {}).get("pnl"),
                           capital_flows=_xirr_flows(ledger_flows, days))
    risk_all = risk(days, exposure_days=exposure)
    behavior = campaign_stats(engine["campaigns"], equity_at=equity_at)
    act = activity(engine["operations"], list(store.rows("orders")))
    cost = costs(engine["operations"], funding, gross_trade_profit=behavior.get("gross_trade_profit"),
                 volume=act.get("volume"), snapshot=snapshot, prices=spot_prices)
    prof = profile(behavior, act, risk_all, current)
    qual = quality(truncation, reconciliation, paths_summary, series, current,
                   (mtm or {}).get("flags"), store.get_meta("asset_meta_errors", []))
    qual["backfill"] = store.get_meta("backfill_status") or {"status": "disabled", "note": "未启用付费补数"}
    qual["volume_coverage_with_imputed"] = (
        (truncation["visible_volume"] + float(reconciliation.get("components", {}).get("imputed_notional") or 0))
        / truncation["official_volume"] if truncation.get("official_volume") else None)

    model = {
        "wallet": store.wallet, "generated_at_ms": now,
        "official": {"windows": window_stats, "series_total": official_total, "series_perp": official_perp,
                     "equity": official_equity},
        "leaderboard": store.get_meta("leaderboard_row"),
        "sub_account_summary": store.get_meta("sub_account_summary") or [],
        "series": series, "windows": by_window, "scorecard": cards,
        "excursion": dict(excursion, rows=excursion["rows"][-3000:]) if excursion else None,
        "performance": perf_all, "risk": risk_all,
        "market": market_model(days, benchmark),
        "evidence": evidence_strength(days, behavior, perf_all.get("monthly_stability", {}).get("active_months")),
        "capital": capital_summary(flows, now),
        "pnl_breakdown": _breakdown(reconciliation, cost, flows),
        "behavior": behavior, "activity": act, "costs": cost,
        "current": current, "profile": prof, "quality": qual,
        "flows": {"rows": flows, "net_capital": net_capital_flows(flows),
                  "by_class": _sum_by(flows, "class_label")},
        "mtm": {"days": (mtm or {}).get("days", []), "by_coin": (mtm or {}).get("by_coin", {}),
                "start": (mtm or {}).get("start"), "flags": (mtm or {}).get("flags", {})},
        "engine_summary": engine["summary"], "engine_warnings": engine["warnings"],
    }
    return _plain(model), engine


LEDGER_CLASSES = ("external", "subaccount", "vault")


def _equity_lookup(days):
    """equity_at(ms): last day-end equity strictly before ``ms`` (no look-ahead)."""
    times = [d["time"] for d in days]

    def at(ms):
        if ms is None:
            return None
        i = bisect_right(times, ms - 1) - 1
        return days[i]["equity"] if i >= 0 else None
    return at


def _ledger_capital(flows):
    return sorted((int(f["time"]), float(f["amount_usd"])) for f in flows
                  if f["class"] in LEDGER_CLASSES and f.get("amount_usd") is not None and f.get("time") is not None)


def _xirr_flows(ledger, days):
    """Ledger flows for XIRR when they explain the implied net flow of ``days``.

    The implied flow (Δequity − ΔPnL) is the official truth; ledger rows give
    the real timestamps. If they disagree by more than max(2%, $100) the
    ledger is incomplete for this range and the implied daily flows are used.
    """
    if not days or not ledger:
        return None
    start = days[0].get("start", days[0]["time"] - DAY_MS)
    inside = [(t, a) for t, a in ledger if start < t <= days[-1]["time"]]
    implied = sum(d["flow"] for d in days)
    booked = sum(a for _, a in inside)
    if abs(booked - implied) > max(0.02 * abs(implied), 100.0):
        return None
    return inside


def _benchmark(client, store, days, log):
    """BTC daily closes over the series; {} when unavailable (no network, no cache)."""
    if not days:
        return {}
    try:
        bars = candles(client, store, "BTC", days[0]["time"] - 2 * DAY_MS, days[-1]["time"], "1d", log=log) or {}
        return {int(t): float(v) for t, v in bars.items() if v not in (None, "")}
    except Exception as exc:  # benchmark is optional; the report says why it is missing
        log(f"BTC 基准 K 线不可用：{str(exc)[:80]}")
        return {}


def capital_summary(flows, now, recent_days=60):
    """Gross deposits / withdrawals, net, peak net invested and recent reliance."""
    rows = sorted((f for f in flows if f["class"] in ("external", "subaccount") and f.get("amount_usd") is not None),
                  key=lambda f: int(f["time"] or 0))
    dep = wd = net = peak = 0.0
    recent_dep = recent_net = 0.0
    approx = 0
    for f in rows:
        a = float(f["amount_usd"])
        approx += bool(f.get("approx_value"))
        if a > 0:
            dep += a
        else:
            wd -= a
        net += a
        peak = max(peak, net)
        if int(f["time"]) >= now - recent_days * DAY_MS:
            recent_net += a
            recent_dep += max(a, 0.0)
    return {"deposited": dep, "withdrawn": wd, "net": net, "peak_net_invested": peak,
            "withdrawn_to_deposited": wd / dep if dep else None,
            "recent_days": recent_days, "recent_net": recent_net, "recent_deposited": recent_dep,
            "recent_share_of_deposits": recent_dep / dep if dep else None,
            "rows": len(rows), "approx_rows": approx, "classes": ["external", "subaccount"],
            "note": "按真实流水逐笔统计；同日入金和出金分别计入，不用日净流量代替"}


def _sum_by(rows, key):
    out = {}
    for row in rows:
        value = row.get("amount_usd")
        if value is not None:
            out[row[key]] = out.get(row[key], 0.0) + float(value)
    return out


def _breakdown(reconciliation, cost, flows):
    """Official PnL = components + residual, for the reconstructed range (perp)."""
    comp = reconciliation.get("components") or {}
    if not comp:
        return None
    price = float(comp.get("price_pnl") or 0)
    funding = float(comp.get("funding") or 0)
    fees = float(comp.get("fees") or 0)
    realized = float(comp.get("realized") or 0)
    official = float(reconciliation.get("official") or 0)
    rows = [
        {"name": "已实现盈亏 (closedPnl)", "value": realized},
        {"name": "未实现/补录价差", "value": price - realized},
        {"name": "资金费", "value": funding},
        {"name": "手续费", "value": -fees},
        # Closes the bar chart exactly at the official total.
        {"name": "残差（官方 − 重建）", "value": official - (price + funding - fees)},
    ]
    return {"official": official, "rows": rows,
            "start_ms": reconciliation.get("start_ms"), "end_ms": reconciliation.get("end_ms"),
            "scope": "perp"}
