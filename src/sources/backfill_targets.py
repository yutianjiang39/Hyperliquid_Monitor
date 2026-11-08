"""Which hours/days a paid archive backfill should read.

Paid archives are billed per byte, so only the windows the public API could
not explain are targeted: each ``fill_gap`` (previous fill → next fill, for
that coin) and the truncated head (first archive day → first visible fill).
"""
from __future__ import annotations

from datetime import datetime, timezone

HOUR_MS = 3_600_000
DAY_MS = 86_400_000
# Earliest data in the official node archive (node_fills) and in Reservoir.
ARCHIVE_START_MS = int(datetime(2025, 5, 25, tzinfo=timezone.utc).timestamp() * 1000)
RESERVOIR_START_MS = int(datetime(2025, 7, 28, tzinfo=timezone.utc).timestamp() * 1000)


def gap_windows(paths, truncation, *, mode="gaps", now=None, floor_ms=ARCHIVE_START_MS):
    """[{start, end, coin, reason}] windows to backfill, clipped to archive coverage."""
    now = now if now is not None else int(datetime.now(timezone.utc).timestamp() * 1000)
    truncation = truncation or {}
    if mode == "full":
        first = truncation.get("available_from_ms") or now
        return [{"start": floor_ms, "end": first, "coin": None, "reason": "full"}] if first > floor_ms else []
    out = []
    for path in (paths or {}).values():
        for b in path.breaks:
            if b["kind"] != "fill_gap" or b.get("prev_time") is None:
                continue
            start, end = max(b["prev_time"], floor_ms), b["time"]
            if end > start:
                out.append({"start": start, "end": end, "coin": b["coin"], "reason": "fill_gap"})
    first = truncation.get("available_from_ms")
    if truncation.get("head_truncated") and first:
        start = max(truncation.get("official_start_ms") or floor_ms, floor_ms)
        if first > start:
            out.append({"start": start, "end": first, "coin": None, "reason": "head"})
    return sorted(out, key=lambda w: w["start"])


def hour_set(windows):
    """Hour buckets (ms) touched by any window."""
    hours = set()
    for w in windows:
        h = w["start"] // HOUR_MS * HOUR_MS
        while h <= w["end"]:
            hours.add(h)
            h += HOUR_MS
    return hours


def day_set(windows):
    """UTC day buckets (ms) touched by any window."""
    return {h // DAY_MS * DAY_MS for h in hour_set(windows)}


def summarize(windows):
    hours = hour_set(windows)
    return {"windows": len(windows), "hours": len(hours), "days": len(day_set(windows)),
            "fill_gaps": sum(1 for w in windows if w["reason"] == "fill_gap"),
            "head": next(({"start": w["start"], "end": w["end"]} for w in windows if w["reason"] in ("head", "full")), None)}
