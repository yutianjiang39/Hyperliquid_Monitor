"""MAE / MFE per campaign from a multi-resolution candle ladder.

A campaign's PnL path is realized-so-far plus the open position marked at the
bar extremes (low/high), using the position and average cost between fills.
Each holding segment is scanned with the finest interval whose cached bars
reach back that far, so recent trades use 15m bars and old ones fall back to
1h / 4h / 1d. Fees and funding are left out: this measures price risk taken.
"""
from __future__ import annotations

from sources.market_data import INTERVAL_MS, LADDER


def _f(value):
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def _segments(campaign, ops):
    """[(start_ms, end_ms, signed_qty, avg_cost, realized_before)] between fills."""
    sign = 1.0 if campaign["direction"] == "long" else -1.0
    qty = cost = realized = 0.0
    out, prev_t = [], None
    entry_peak = 0.0
    for op in ops:
        t, px = op["time"], _f(op.get("price"))
        if prev_t is not None and qty and t > prev_t:
            out.append((prev_t, t, qty, cost, realized))
        end = _f(op.get("end_position")) or 0.0
        # A reversal fill ends this campaign; its other leg belongs to the next one.
        new_qty = end if end * sign > 0 else 0.0
        if px is not None:
            if abs(new_qty) > abs(qty):
                cost = (cost * abs(qty) + px * (abs(new_qty) - abs(qty))) / abs(new_qty)
            elif abs(new_qty) < abs(qty):
                realized += (px - cost) * (abs(qty) - abs(new_qty)) * sign
        qty = new_qty
        entry_peak = max(entry_peak, abs(qty) * cost)
        prev_t = t
    return out, entry_peak, realized


def _coverage(ladder):
    """Earliest bucket of each interval; intervals without bars are skipped."""
    return {iv: min(bars) for iv, bars in ladder.items() if bars}


def _scan(seg, ladder, cover, sign):
    """Worst/best path value over one segment and the coarsest interval used."""
    start, end, qty, cost, realized = seg
    worst = best = None
    best_t, coarsest, cursor_end, holes = None, None, end, 0
    for interval in LADDER:
        if interval not in cover or cursor_end <= start:
            continue
        step = INTERVAL_MS[interval]
        lo = max(start, cover[interval])
        if lo >= cursor_end:
            continue
        bars = ladder[interval]
        t = (lo // step) * step
        used = False
        while t < cursor_end:
            bar = bars.get(t)
            if bar:
                hi_px, lo_px = bar
                adverse, favourable = (lo_px, hi_px) if sign > 0 else (hi_px, lo_px)
                w = realized + (adverse - cost) * abs(qty) * sign
                b = realized + (favourable - cost) * abs(qty) * sign
                worst = w if worst is None else min(worst, w)
                if best is None or b > best:
                    best, best_t = b, t
                used = True
            else:
                # A missing bar inside the covered range: the extreme may be in the hole.
                holes += 1
            t += step
        if used:
            coarsest = interval
        cursor_end = lo
    return worst, best, best_t, coarsest, cursor_end > start or holes > 0


def excursions(campaigns, operations, ladder_for, *, limit=5000, log=None):
    """Per-campaign MAE/MFE rows and aggregates.

    ``ladder_for(coin, since_ms)`` returns {interval: {bucket: (high, low)}}.
    Only complete campaigns of perp/spot markets with candles are evaluated.
    """
    ops_by = {}
    for op in operations:
        if op.get("campaign_id"):
            ops_by.setdefault(op["campaign_id"], []).append(op)
    chosen = [c for c in campaigns if c.get("eligible_for_win_rate") and c.get("open_time")
              and not str(c["coin"]).startswith("#")]
    chosen = sorted(chosen, key=lambda c: c["close_time"])[-limit:]
    first_open = {}
    for c in chosen:
        first_open[c["coin"]] = min(first_open.get(c["coin"], c["open_time"]), c["open_time"])
    ladders, rows, no_candles = {}, [], 0
    for c in chosen:
        if c["coin"] not in ladders:
            ladders[c["coin"]] = ladder_for(c["coin"], first_open[c["coin"]])
            if log and len(ladders) % 10 == 0:
                log(f"MAE/MFE：K 线 {len(ladders)} / {len(first_open)} 个币种 …")
        lad = ladders[c["coin"]]
        cover = _coverage(lad)
        sign = 1.0 if c["direction"] == "long" else -1.0
        segs, entry_peak, realized = _segments(c, sorted(ops_by.get(c["id"], []), key=lambda o: o["time"]))
        if not segs or not entry_peak:
            continue
        mae = mfe = 0.0
        mfe_t, resolution, missing = None, None, False
        for seg in segs:
            w, b, bt, used, gap = _scan(seg, lad, cover, sign)
            missing |= gap
            if w is not None:
                mae = min(mae, w)
            if b is not None and b > mfe:
                mfe, mfe_t = b, bt
            if used and (resolution is None or LADDER.index(used) > LADDER.index(resolution)):
                resolution = used
        if resolution is None:
            no_candles += 1
            continue
        pnl = _f(c.get("net_pnl_with_funding")) if c.get("net_pnl_with_funding") is not None else _f(c.get("net_pnl"))
        # Shorter than one bar of the interval used: extremes include price moves outside the holding.
        coarse = (c.get("duration_ms") or 0) < INTERVAL_MS[resolution] or missing
        rows.append({
            "id": c["id"], "coin": c["coin"], "direction": c["direction"],
            "open_time": c["open_time"], "close_time": c["close_time"],
            "entry_notional": entry_peak, "pnl": pnl, "price_pnl": realized,
            "mae": mae, "mfe": mfe, "mae_pct": mae / entry_peak, "mfe_pct": mfe / entry_peak,
            "mfe_time": mfe_t, "resolution": resolution, "coarse": coarse, "gapped": missing,
            "capture": realized / mfe if mfe > 0 else None,
        })
    eligible = sum(1 for c in campaigns if c.get("eligible_for_win_rate") and c.get("open_time"))
    return {"rows": rows, "candidates": len(chosen), "eligible_total": eligible, "limit": limit,
            "earliest_open": min((r["open_time"] for r in rows), default=None),
            "no_candles": no_candles, **summarize(rows)}


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def _median(xs):
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def summarize(rows, since=None):
    rows = [r for r in rows if since is None or r["close_time"] >= since]
    fine = [r for r in rows if not r["coarse"]]
    wins = [r for r in fine if (r["pnl"] or 0) > 0]
    losses = [r for r in fine if (r["pnl"] or 0) < 0]
    captures = [r["capture"] for r in fine if r["capture"] is not None]
    return {
        "evaluated": len(rows), "coarse": len(rows) - len(fine),
        "gapped": sum(1 for r in rows if r.get("gapped")),
        "resolution_counts": {iv: sum(1 for r in rows if r["resolution"] == iv) for iv in LADDER},
        "mae_pct_mean": _mean([r["mae_pct"] for r in fine]), "mae_pct_median": _median([r["mae_pct"] for r in fine]),
        "mfe_pct_mean": _mean([r["mfe_pct"] for r in fine]), "mfe_pct_median": _median([r["mfe_pct"] for r in fine]),
        "mfe_capture_median": _median(captures),
        "win_mae_pct_mean": _mean([r["mae_pct"] for r in wins]),
        "loss_mfe_pct_mean": _mean([r["mfe_pct"] for r in losses]),
        "loss_mae_pct_mean": _mean([r["mae_pct"] for r in losses]),
    }
