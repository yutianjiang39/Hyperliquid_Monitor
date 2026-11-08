"""Optional cross-check against the official public leaderboard (tier T3).

The file is large (tens of MB); it is streamed and only this wallet's row kept.
"""
from __future__ import annotations

import json
import time

URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"


def fetch_leaderboard_row(store, *, session=None, log=print, max_age_ms=86_400_000):
    cached = store.get_meta("leaderboard_row")
    now = int(time.time() * 1000)
    if cached and now - int(cached.get("fetched_at_ms") or 0) < max_age_ms:
        return cached
    import requests
    session = session or requests.Session()
    response = session.get(URL, timeout=(10, 300))
    response.raise_for_status()
    rows = json.loads(response.content.decode("utf-8")).get("leaderboardRows", [])
    row = next((r for r in rows if str(r.get("ethAddress", "")).lower() == store.wallet), None)
    result = {"fetched_at_ms": now, "found": row is not None, "rank_population": len(rows)}
    if row:
        result.update(display_name=row.get("displayName"), account_value=row.get("accountValue"),
                      windows={name: perf for name, perf in row.get("windowPerformances", [])})
        volumes = sorted(float((dict(r.get("windowPerformances", [])).get("allTime") or {}).get("vlm") or 0) for r in rows)
        mine = float(result["windows"].get("allTime", {}).get("vlm") or 0)
        below = sum(v < mine for v in volumes)
        result["all_time_volume_percentile"] = round(100 * below / len(volumes), 2) if volumes else None
    store.set_meta("leaderboard_row", result)
    store.commit()
    log("排行榜核对：" + ("已找到该地址" if row else "该地址不在排行榜中"))
    return result
