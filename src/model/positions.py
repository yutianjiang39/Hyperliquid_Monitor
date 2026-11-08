"""Snapshot-anchored position paths from fills.

Every Hyperliquid fill carries ``startPosition``, the position immediately
before it. Chaining fills per coin gives an exact position path; any link
where the previous fill's end differs from the next fill's start is a
``position_break`` (fills missing in between, or a non-fill change such as a
liquidation transfer). The unexplained change is placed at the midpoint of
the gap, so the path and the imputed cash in ``model.mtm`` agree on timing.
When daily position snapshots (optional paid backfill) fall inside a gap, the
change is split into steps placed between the snapshots that bracket it.
The latest snapshot ``szi`` closes the chain.
"""
from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict
from decimal import Decimal

from engine import position_decimal

ZERO = Decimal(0)


def _d(value):
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except Exception:
        return None


def same_position(a, b):
    """Equal up to float serialization noise (~1e-12 relative)."""
    if a is None or b is None:
        return a is b
    return abs(a - b) <= max(abs(a), abs(b), Decimal(1)) * Decimal("1e-12")


def is_spot(coin):
    """Spot pairs and HIP-4 outcome tokens (``#<id>``, priced 0–1): balances, not perps."""
    return coin.startswith(("@", "#")) or "/" in coin


def signed_size(fill):
    size = _d(fill.get("sz")) or ZERO
    return size if str(fill.get("side")).upper() == "B" else -size


class PositionPath:
    def __init__(self, coin, snapshots=None):
        self.coin = coin
        self.snapshots = sorted(snapshots or [])  # [(ms, Decimal szi)] from position_snapshot records
        self.times, self.starts, self.ends = [], [], []
        self.breaks = []
        self.current = None  # snapshot szi
        self.snapshot_time = None

    def add(self, fill):
        start = position_decimal(fill.get("startPosition"))
        if start is None:
            return
        end = position_decimal(start + signed_size(fill))
        if self.ends and not same_position(self.ends[-1], start):
            prev_t, t = self.times[-1], int(fill["time"])
            known = [(prev_t, self.ends[-1])]
            known += [(st, sz) for st, sz in self.snapshots if prev_t < st < t]
            known.append((t, start))
            steps = [{"time": (t0 + t1) // 2, "from": a, "to": b}
                     for (t0, a), (t1, b) in zip(known, known[1:]) if not same_position(a, b)]
            self.breaks.append({"coin": self.coin, "time": t, "prev_time": prev_t, "mid": steps[0]["time"],
                                "expected": str(self.ends[-1]), "observed": str(start),
                                "delta": str(start - self.ends[-1]), "px": str(fill.get("px")),
                                "kind": "fill_gap", "snapshot_corrected": len(known) > 2,
                                "steps": [{"time": s["time"], "delta": str(s["to"] - s["from"])} for s in steps]})
            for step in steps:
                self.times.append(step["time"])
                self.starts.append(step["from"])
                self.ends.append(step["to"])
        self.times.append(int(fill["time"]))
        self.starts.append(start)
        self.ends.append(end)

    def close(self, current, snapshot_time):
        self.current, self.snapshot_time = current, snapshot_time
        if self.ends and current is not None and not same_position(self.ends[-1], current):
            self.breaks.append({"coin": self.coin, "time": snapshot_time, "expected": str(self.ends[-1]),
                                "observed": str(current), "delta": str(current - self.ends[-1]),
                                "px": None, "kind": "snapshot_mismatch"})

    @property
    def seeded(self):
        """Position existed before the first observed fill."""
        return bool(self.starts) and self.starts[0] != ZERO

    def at(self, t):
        """Position held at instant t (after all fills with time < t)."""
        if not self.times:
            return self.current if self.current is not None else ZERO
        index = bisect_left(self.times, t)
        if index < len(self.times):
            return self.starts[index]
        return self.current if self.current is not None and self.snapshot_time and t >= self.snapshot_time else self.ends[-1]


def position_snapshots(rows):
    """{coin: [(ms, szi)]} from stored ``position_snapshot`` records."""
    out = defaultdict(list)
    for row in rows:
        size = _d(row.get("szi"))
        if row.get("coin") and size is not None:
            out[str(row["coin"])].append((int(row["time"]), position_decimal(size)))
    return dict(out)


def build_paths(ordered_fills, snapshot=None, spot=False, snapshots=None):
    paths = {}
    snapshots = snapshots or {}
    for fill in ordered_fills:
        coin = str(fill.get("coin") or "")
        if is_spot(coin) != spot:
            continue
        if coin not in paths:
            paths[coin] = PositionPath(coin, snapshots.get(coin))
        paths[coin].add(fill)
    if snapshot is not None and not spot:
        stamp = snapshot.get("time")
        held = {p["coin"]: _d(p["szi"]) for d in snapshot.get("dexes", {}).values() for p in d["positions"]}
        for coin in set(paths) | set(held):
            paths.setdefault(coin, PositionPath(coin)).close(held.get(coin, ZERO), stamp)
    return paths


def summarize_paths(paths):
    breaks = [b for p in paths.values() for b in p.breaks]
    seeded = sorted(c for c, p in paths.items() if p.seeded)
    held_without_fills = sorted(c for c, p in paths.items() if not p.times and p.current)
    matches = sum(1 for p in paths.values() if p.current is not None and (not p.ends or same_position(p.ends[-1], p.current)))
    return {"coins": len(paths), "breaks": breaks[:500], "break_count": len(breaks),
            "fill_gap_count": sum(b["kind"] == "fill_gap" for b in breaks),
            "snapshot_corrected_gaps": sum(1 for b in breaks if b.get("snapshot_corrected")),
            "snapshot_mismatch": [b for b in breaks if b["kind"] == "snapshot_mismatch"],
            "seeded_coins": seeded, "held_without_fills": held_without_fills,
            "snapshot_matches": matches}
