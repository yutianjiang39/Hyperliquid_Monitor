"""Reconcile reconstructed history against official Hyperliquid numbers.

Official ``portfolio`` PnL / volume are the truth; fills explain them. This
module answers two questions with evidence rather than "unknown":

* Is the visible fill history complete? (``detect_truncation``)
* How far is the reconstructed PnL from the official PnL, interval by
  interval at the official sample timestamps? (``reconcile_pnl``)

Comparing only at official sample points matters: ``perpAllTime`` is sampled
roughly weekly, and interpolating it to calendar boundaries invents swings of
the size of a week's PnL.
"""
from __future__ import annotations

from bisect import bisect_right
from decimal import Decimal

from model.positions import is_spot
from sources.market_data import DAY_MS

ZERO = Decimal(0)
FILL_RETENTION = 10_000
OK_PCT, WARN_PCT, OK_ABS = Decimal("0.01"), Decimal("0.05"), Decimal(100)
EVIDENCE_LABELS = {
    "fill_retention_cap": "成交条数接近 API 保留上限（约 1 万条）",
    "account_older_than_fills": "官方净值历史早于第一笔可见成交",
    "seeded_position": "某币种第一笔可见成交前已有持仓",
    "volume_shortfall": "可见成交额低于官方成交额",
    "position_breaks": "成交前仓位链断开，中间有成交缺失",
}


def _d(value):
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except Exception:
        return None


def _num(value):
    return None if value is None else float(value)


def status_for(residual, official):
    """ok / warn / fail by max(1%, $100) and 5% of the official magnitude."""
    gap, scale = abs(residual), abs(official)
    if gap <= max(scale * OK_PCT, OK_ABS):
        return "ok"
    return "warn" if gap <= scale * WARN_PCT else "fail"


def detect_truncation(fills, paths, official, window_stats, scope="perp"):
    """Return {"head_truncated", "available_from_ms", "evidence", "volume_coverage", ...}.

    ``fills`` are deduplicated fills, ``paths`` from ``model.positions``,
    ``official`` the official cumulative series for ``scope``.
    """
    evidence = []
    scoped = [f for f in fills if (scope != "perp" or not is_spot(str(f.get("coin") or "")))]
    first_fill = min((int(f["time"]) for f in scoped), default=None)
    regular = sum(1 for f in fills if f.get("twapId") is None)
    official_start = official[0]["time"] if official else None
    starts_late = official_start is None or first_fill is None or first_fill > official_start
    # Hitting the cap only matters if the visible history starts after the account.
    if regular >= FILL_RETENTION * 0.98 and starts_late:
        evidence.append({"code": "fill_retention_cap", "detail": f"{regular} 条非 TWAP 成交"})
    if official_start is not None and first_fill is not None and first_fill - official_start > 7 * DAY_MS:
        evidence.append({"code": "account_older_than_fills", "detail": {
            "official_start_ms": official_start, "first_fill_ms": first_fill}})
    seeded = sorted(coin for coin, path in paths.items() if path.seeded)
    if seeded and starts_late:
        evidence.append({"code": "seeded_position", "detail": seeded[:20]})
    window = (window_stats or {}).get("perpAllTime" if scope == "perp" else "allTime") or {}
    official_vlm = _d(window.get("vlm"))
    visible_vlm = sum(((_d(f.get("px")) or ZERO) * (_d(f.get("sz")) or ZERO) for f in scoped), ZERO)
    coverage = visible_vlm / official_vlm if official_vlm else None
    if coverage is not None and coverage < Decimal("0.98"):
        evidence.append({"code": "volume_shortfall", "detail": {
            "visible": _num(visible_vlm), "official": _num(official_vlm), "coverage": _num(coverage)}})
    gaps = [b for path in paths.values() for b in path.breaks if b["kind"] == "fill_gap"]
    if gaps:
        evidence.append({"code": "position_breaks", "detail": {
            "count": len(gaps), "coins": sorted({b["coin"] for b in gaps})[:30]}})
    # Head truncation: history before the first visible fill is missing.
    # Mid-history breaks alone are gaps, not a head cut.
    head = any(e["code"] in ("fill_retention_cap", "account_older_than_fills", "seeded_position")
               for e in evidence)
    if not head and any(e["code"] == "volume_shortfall" for e in evidence) and not gaps:
        head = True
    for item in evidence:
        item["label"] = EVIDENCE_LABELS[item["code"]]
    return {"head_truncated": head, "available_from_ms": first_fill, "official_start_ms": official_start,
            "visible_volume": _num(visible_vlm), "official_volume": _num(official_vlm),
            "volume_coverage": _num(coverage), "fill_gaps": len(gaps), "evidence": evidence,
            "status": "api_visible_truncated" if head else ("api_visible_gaps" if gaps else "complete")}


def _cumulative(days):
    """Reconstructed cumulative PnL as a function of time (linear within a day)."""
    times, values, total = [], [], ZERO
    for day in days:
        times.append(day["time"])
        values.append(total)
        total += day["pnl"]
    end = days[-1]["time"] + DAY_MS if days else 0
    pnl = [d["pnl"] for d in days]

    def at(t):
        if not times or t <= times[0]:
            return ZERO
        if t >= end:
            return total
        index = bisect_right(times, t) - 1
        return values[index] + pnl[index] * Decimal(t - times[index]) / DAY_MS
    return at


def _thin(points, min_gap):
    """Drop samples so consecutive kept points are at least ``min_gap`` apart."""
    kept = points[:1]
    for point in points[1:-1]:
        if point["time"] - kept[-1]["time"] >= min_gap:
            kept.append(point)
    if len(points) > 1:
        if kept[-1] is not points[-1] and points[-1]["time"] - kept[-1]["time"] < min_gap and len(kept) > 1:
            kept.pop()
        kept.append(points[-1])
    return kept


def reconcile_pnl(official, mtm, truncation=None, now=None, interval_ms=7 * DAY_MS):
    """Compare official cumulative PnL with the MTM reconstruction.

    Intervals run between consecutive official samples inside the
    reconstructed range, merged to at least ``interval_ms`` because the
    reconstruction is daily while recent official samples are intraday. The
    range before ``mtm["start"]`` is official-only.
    """
    days = mtm.get("days") or []
    if not official or not days:
        return {"status": "no_data", "intervals": []}
    start, end = mtm["start"], min(now or mtm["end"], mtm["end"])
    recon = _cumulative(days)
    points = _thin([p for p in official if start <= p["time"] <= end], interval_ms)
    official_total = points[-1]["cum_pnl"] - points[0]["cum_pnl"] if len(points) > 1 else ZERO
    # An interval is judged against its own size, but never tighter than 0.5%
    # of the whole period, so a flat week is not failed for a few dollars.
    floor = abs(official_total) * Decimal("0.005")
    intervals = []
    for a, b in zip(points, points[1:]):
        off = b["cum_pnl"] - a["cum_pnl"]
        rec = recon(b["time"]) - recon(a["time"])
        intervals.append({"start_ms": a["time"], "end_ms": b["time"], "official": _num(off),
                          "reconstructed": _num(rec), "residual": _num(off - rec),
                          "status": "ok" if abs(off - rec) <= floor else status_for(off - rec, off)})
    recon_total = recon(points[-1]["time"]) - recon(points[0]["time"]) if len(points) > 1 else ZERO
    residual = official_total - recon_total
    components = {key: _num(sum((d[key] for d in days), ZERO))
                  for key in ("price_pnl", "fees", "funding", "realized", "volume", "imputed_notional")}
    before = [p for p in official if p["time"] < start]
    official_only = None
    if before:
        first_rec = points[0] if points else official[-1]
        official_only = {"start_ms": official[0]["time"], "end_ms": first_rec["time"],
                         "official": _num(first_rec["cum_pnl"] - official[0]["cum_pnl"])}
    status = status_for(residual, official_total)
    reasons = []
    if components["imputed_notional"]:
        reasons.append("imputed_fill_gaps")
    if truncation and truncation.get("head_truncated"):
        reasons.append("head_truncated")
    return {
        "status": status, "reasons": reasons,
        "start_ms": points[0]["time"] if points else start, "end_ms": points[-1]["time"] if points else end,
        "official": _num(official_total), "reconstructed": _num(recon_total), "residual": _num(residual),
        "residual_pct": _num(residual / official_total) if official_total else None,
        "components": components, "official_only": official_only, "intervals": intervals,
        "interval_status": {s: sum(1 for i in intervals if i["status"] == s) for s in ("ok", "warn", "fail")},
    }
