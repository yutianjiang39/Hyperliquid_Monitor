"""Recompute MAE/MFE for a few campaigns from freshly downloaded candles.

Usage: python tools/audit_mae.py <report_dir> [n=5]
"""
import csv
import json
import random
import sys
from pathlib import Path

import requests

sys.stdout.reconfigure(encoding="utf-8")
ex = Path(sys.argv[1]) / "exports"
n = int(sys.argv[2]) if len(sys.argv) > 2 else 5
M = json.loads((ex / "model.json").read_text(encoding="utf-8"))
STEP = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}

with open(ex / "operations.csv", encoding="utf-8-sig", newline="") as f:
    ops = list(csv.DictReader(f))
for o in ops:
    if o["coin"].startswith("'"):
        o["coin"] = o["coin"][1:]
by = {}
for o in ops:
    by.setdefault(o["campaign_id"], []).append(o)

rows = [r for r in M["excursion"]["rows"] if not r["coarse"]]
random.seed(1)
for r in random.sample(rows, min(n, len(rows))):
    step = STEP[r["resolution"]]
    bars = requests.post("https://api.hyperliquid.xyz/info", json={"type": "candleSnapshot", "req": {
        "coin": r["coin"], "interval": r["resolution"],
        "startTime": r["open_time"] - step, "endTime": r["close_time"]}}, timeout=60).json()
    bars = {b["t"]: (float(b["h"]), float(b["l"])) for b in bars}
    sign = 1 if r["direction"] == "long" else -1
    legs = sorted(by[r["id"]], key=lambda o: int(o["time"]))
    qty = cost = realized = 0.0
    worst = best = 0.0
    prev = None
    for o in legs:
        t, px = int(o["time"]), float(o["price"])
        if prev is not None and qty:
            # Every bar touching [prev, t) at this resolution, held at qty/cost.
            b = prev // step * step
            while b < t:
                if b in bars:
                    hi, lo = bars[b]
                    adv, fav = (lo, hi) if sign > 0 else (hi, lo)
                    worst = min(worst, realized + (adv - cost) * abs(qty) * sign)
                    best = max(best, realized + (fav - cost) * abs(qty) * sign)
                b += step
        end = float(o["end_position"])
        new = end if end * sign > 0 else 0.0
        if abs(new) > abs(qty):
            cost = (cost * abs(qty) + px * (abs(new) - abs(qty))) / abs(new)
        elif abs(new) < abs(qty):
            realized += (px - cost) * (abs(qty) - abs(new)) * sign
        qty, prev = new, t
    ok = abs(worst - r["mae"]) <= 1e-6 * max(1, abs(worst)) and abs(best - r["mfe"]) <= 1e-6 * max(1, abs(best))
    print(f"{'PASS' if ok else 'FAIL'} {r['coin']:10s} {r['direction']:5s} {r['resolution']:3s} "
          f"MAE 报告 {r['mae']:>12,.2f} 复算 {worst:>12,.2f}   MFE 报告 {r['mfe']:>12,.2f} 复算 {best:>12,.2f}")
