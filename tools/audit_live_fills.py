"""Compare a report's exported fills/funding with a fresh pull from the public API.

Usage: python tools/audit_live_fills.py <report_dir> [days=7]
"""
import csv
import json
import sys
from pathlib import Path

import requests

sys.stdout.reconfigure(encoding="utf-8")
DAY = 86_400_000
ex = Path(sys.argv[1]) / "exports"
days = int(sys.argv[2]) if len(sys.argv) > 2 else 7
M = json.loads((ex / "model.json").read_text(encoding="utf-8"))
wallet = M["wallet"]


def rows(name):
    with open(ex / name, encoding="utf-8-sig", newline="") as f:
        out = list(csv.DictReader(f))
    for r in out:
        if r.get("coin", "").startswith("'"):
            r["coin"] = r["coin"][1:]
    return out


def post(body):
    return requests.post("https://api.hyperliquid.xyz/info", json=body, timeout=60).json()


ops = rows("operations.csv")
end = max(int(o["time"]) for o in ops)
start = end - days * DAY

live, cursor = [], start
while cursor <= end:
    page = post({"type": "userFillsByTime", "user": wallet, "startTime": cursor, "endTime": end, "aggregateByTime": False})
    if not page:
        break
    live += page
    if len(page) < 2000:
        break
    cursor = max(f["time"] for f in page) + 1
live = {(f["coin"], f["tid"], f["side"]): f for f in live if f["time"] <= end}

mine = {}
for o in ops:
    if start <= int(o["time"]) <= end:
        key = (o["coin"], int(o["tid"]), o["side"])
        a = mine.setdefault(key, {"sz": 0.0, "fee": 0.0, "pnl": 0.0, "px": float(o["price"])})
        # Reversal fills are exported as two legs; sum them back to one fill.
        a["sz"] += float(o["size"])
        a["fee"] += float(o["fee"] or 0)
        a["pnl"] += float(o["closed_pnl"] or 0)
missing = set(live) - set(mine)
extra = set(mine) - set(live)
bad = []
for k in set(live) & set(mine):
    f, m = live[k], mine[k]
    for name, x, y in (("sz", float(f["sz"]), m["sz"]), ("px", float(f["px"]), m["px"]),
                       ("fee", float(f["fee"]), m["fee"]), ("closedPnl", float(f["closedPnl"]), m["pnl"])):
        if abs(x - y) > 1e-6 * max(1, abs(x)):
            bad.append((k, name, x, y))
print(f"钱包 {wallet} 最近 {days} 天成交：API {len(live)} 笔，报告 {len(mine)} 笔，"
      f"报告缺 {len(missing)}，多出 {len(extra)}，字段不符 {len(bad)}")
for b in bad[:5]:
    print("  ", b)

fund = rows("funding.csv")
lf = post({"type": "userFunding", "user": wallet, "startTime": start, "endTime": end})
api_f = sum(float(e["delta"]["usdc"]) for e in lf)
rep_f = sum(float(r["usdc"]) for r in fund if start <= int(r["time"]) <= end)
print(f"  资金费：API {len(lf)} 条合计 {api_f:,.4f}，报告 {sum(1 for r in fund if start <= int(r['time']) <= end)} 条合计 {rep_f:,.4f}"
      + ("（API 单页上限 500 条，仅作参考）" if len(lf) >= 500 else ""))
