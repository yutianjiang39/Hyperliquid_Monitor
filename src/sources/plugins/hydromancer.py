"""Hydromancer: same request bodies as the official /info API, longer history.

POST https://api.hydromancer.xyz/info with ``Authorization: Bearer <key>``.
Per its public docs, single-wallet fills reach back to 2025-07-28.
"""
from __future__ import annotations

import os

from collector import InfoClient, api_rows
from sources.plugins.base import SourcePlugin

ENDPOINTS = {"fills": ("userFillsByTime", 2000), "ledger": ("userNonFundingLedgerUpdates", 500),
             "funding": ("userFunding", 500)}


class HydromancerClient(InfoClient):
    def __init__(self, key, url="https://api.hydromancer.xyz/info"):
        super().__init__(url=url)
        self.session.headers.update({"Authorization": "Bearer " + key, "User-Agent": "hl-monitor/3"})


class Hydromancer(SourcePlugin):
    name = "hydromancer"
    env_key = "HYDROMANCER_API_KEY"
    capabilities = frozenset(ENDPOINTS)
    cost_hint = "按 Hydromancer 套餐计费；历史成交请求权重较高。"

    def client(self):
        return HydromancerClient(os.environ[self.env_key], self.settings.get("url") or "https://api.hydromancer.xyz/info")

    def test(self, wallet):
        import time
        rows = self.client().post({"type": "userFillsByTime", "user": wallet,
                                   "startTime": int(time.time() * 1000) - 86_400_000})
        return {"ok": isinstance(rows, list), "detail": f"最近 24 小时成交 {len(rows or [])} 条"}

    def fetch(self, store, kind, start_ms, end_ms, *, log=print, cancel_check=None, client=None):
        endpoint, cap = ENDPOINTS[kind]
        client = client or self.client()
        added = [0]

        def save(rows):
            for row in rows:
                if cancel_check:
                    cancel_check()
                if kind in ("ledger", "funding") and not isinstance(row.get("delta"), dict):
                    continue
                added[0] += store.put(kind, row, "plugin:hydromancer")
            store.commit()

        stats = api_rows(client, endpoint, store.wallet, start_ms, end_ms, cap, save)
        stats["new_rows"] = added[0]
        log(f"Hydromancer {endpoint}: 收到 {stats['received_rows']:,} 条，新增 {added[0]:,} 条")
        return stats
