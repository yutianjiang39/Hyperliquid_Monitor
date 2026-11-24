"""Daily equity / PnL / flow series anchored to official samples.

Official ``portfolio`` points are irregular (weekly far back, hourly recently).
Each UTC day end gets:

* ``pnl``: official PnL change. Inside an official interval spanning several
  days, the reconstructed daily PnL (``model.mtm``) gives the shape and the
  official total is kept exactly by spreading the residual evenly. Without a
  reconstruction the official change is spread linearly.
* ``flow``: implied capital flow ``ΔV − ΔPnL`` (deposits +, withdrawals −) of
  each official interval, spread evenly over its duration.
* ``equity``: previous equity + pnl + flow, so it equals the official account
  value at every sample and follows the PnL shape in between.
* ``ret``: Modified Dietz daily return ``pnl / (V_prev + flow / 2)`` (flows
  assumed mid-day; an approximation, not exact sub-period TWR). Days whose
  denominator is below the base floor are skipped, counted and tagged with
  ``ret_excluded``. The floor is ``max(min(min_base, 10% of peak equity),
  1% of median equity)`` so a small but complete account still has returns.
* ``start``: real start of the row (the first row may be a partial day).
* ``now`` truncates the official samples first (interpolating one point at the
  cutoff), so nothing after the cutoff leaks into the last day.

``source`` per day is ``official`` (sample at both ends of the day),
``hybrid`` (reconstruction-shaped) or ``interpolated``.
"""
from __future__ import annotations

from bisect import bisect_left

DAY_MS = 86_400_000


def daily_series(official, reconstructed_days=None, now=None, min_base=1000.0):
    points = [p for p in official if p.get("equity") is not None]
    if now is not None and points and points[-1]["time"] > now:
        points = _cut(points, now)
    if len(points) < 2:
        return {"days": [], "skipped_return_days": 0, "min_base": min_base}
    times = [p["time"] for p in points]
    equity = [float(p["equity"]) for p in points]
    cum = [float(p["cum_pnl"]) for p in points]
    rec = {d["time"]: float(d["pnl"]) for d in (reconstructed_days or [])}
    end = min(now or times[-1], times[-1])
    first_day = (times[0] // DAY_MS + 1) * DAY_MS
    day_ends = list(range(first_day, end + 1, DAY_MS))
    if not day_ends or day_ends[-1] != end:
        day_ends.append(end)

    # Per official interval: PnL shaped by the reconstruction, implied flow
    # spread over time. Both sum exactly to the official changes.
    pnl_by_end, flow_by_end, rank_by_end = {}, {}, {}
    ranks = {"official": 0, "hybrid": 1, "interpolated": 2}
    for i in range(len(times) - 1):
        a, b = times[i], times[i + 1]
        delta = cum[i + 1] - cum[i]
        flow_total = (equity[i + 1] - equity[i]) - delta
        source = "official" if b - a <= 1.1 * DAY_MS else ("hybrid" if rec else "interpolated")
        inside = day_ends[bisect_left(day_ends, a + 1):bisect_left(day_ends, b + 1)]
        cuts = [a] + [e for e in inside if e < b] + [b]
        pieces = list(zip(cuts, cuts[1:]))
        shape = [rec.get((s // DAY_MS) * DAY_MS, 0.0) * (e - s) / DAY_MS for s, e in pieces]
        residual = delta - sum(shape)
        for (s, e), base in zip(pieces, shape):
            frac = (e - s) / (b - a)
            day_end = day_ends[min(bisect_left(day_ends, e), len(day_ends) - 1)]
            pnl_by_end[day_end] = pnl_by_end.get(day_end, 0.0) + base + residual * frac
            flow_by_end[day_end] = flow_by_end.get(day_end, 0.0) + flow_total * frac
            rank_by_end[day_end] = max(rank_by_end.get(day_end, 0), ranks[source])

    positive = sorted(v for v in equity if v > 0)
    floor_cap = min_base
    if positive:
        min_base = max(min(floor_cap, 0.10 * positive[-1]), 0.01 * positive[len(positive) // 2])
    names = {v: k for k, v in ranks.items()}
    days, skipped, v, prev_end = [], 0, equity[0], times[0]
    for e in day_ends:
        pnl, flow = pnl_by_end.get(e, 0.0), flow_by_end.get(e, 0.0)
        base = v + flow / 2
        ret = pnl / base if base >= min_base else None
        skipped += ret is None
        v += pnl + flow
        row = {"time": e, "start": prev_end, "equity": v, "pnl": pnl, "flow": flow, "ret": ret,
               "source": names[rank_by_end.get(e, 2)]}
        if ret is None:
            row["ret_excluded"] = "negative_base" if base <= 0 else "base_below_floor"
        days.append(row)
        prev_end = e
    cum_pnl = 0.0
    for d in days:
        cum_pnl += d["pnl"]
        d["cum_pnl"] = cum_pnl
    return {"days": days, "skipped_return_days": skipped, "min_base": min_base, "min_base_cap": floor_cap,
            "official_start_ms": times[0], "official_end_ms": times[-1],
            "official_pnl_total": cum[-1] - cum[0]}


def _cut(points, now):
    """Official points up to ``now``, plus one point linearly interpolated at ``now``."""
    before = [p for p in points if p["time"] <= now]
    after = next((p for p in points if p["time"] > now), None)
    if before and after and before[-1]["time"] < now:
        a, b = before[-1], after
        w = (now - a["time"]) / (b["time"] - a["time"])
        mix = lambda k: float(a[k]) + (float(b[k]) - float(a[k])) * w
        before.append({"time": now, "equity": mix("equity"), "cum_pnl": mix("cum_pnl"), "interpolated": True})
    return before


def window_start(window, now):
    """Start ms of a named window (exclusive); None for ``all``."""
    if window == "all":
        return None
    if window == "ytd":
        import datetime
        year = datetime.datetime.fromtimestamp(now / 1000, datetime.timezone.utc).year
        return int(datetime.datetime(year, 1, 1, tzinfo=datetime.timezone.utc).timestamp() * 1000)
    return now - int(window.rstrip("d")) * DAY_MS


def window_slice(days, window, now):
    """Days inside a named window: 7d / 30d / 90d / ytd / all."""
    start = window_start(window, now)
    if start is None or not days:
        return days
    return [d for d in days if d["time"] > start]
