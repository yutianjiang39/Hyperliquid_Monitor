"""Dune: read the latest results of a user-owned saved query.

The query must accept a ``wallet`` parameter and return one row per fill with
Hyperliquid column names (time, coin, side, px, sz, startPosition, closedPnl,
fee, feeToken, tid, oid, hash, dir). Column aliases from imports are accepted.
GET https://api.dune.com/api/v1/query/{id}/results with header X-Dune-API-Key.
"""
from __future__ import annotations

import os

from collector import normalize_import_fill
from sources.plugins.base import SourcePlugin

API = "https://api.dune.com/api/v1/query/{query_id}/results"


class Dune(SourcePlugin):
    name = "dune"
    env_key = "DUNE_API_KEY"
    capabilities = frozenset({"fills"})
    cost_hint = "按 Dune credits 计费；需要自行维护返回 Hyperliquid 字段的查询。"

    def available(self):
        return super().available() and bool(self.settings.get("query_id"))

    def test(self, wallet):
        import requests
        response = requests.get(API.format(query_id=self.settings["query_id"]), timeout=60,
                                params={"limit": 1, "params.wallet": wallet},
                                headers={"X-Dune-API-Key": os.environ[self.env_key]})
        response.raise_for_status()
        rows = (response.json().get("result") or {}).get("rows") or []
        return {"ok": True, "detail": f"查询可读，返回 {len(rows)} 行样本"}

    def fetch(self, store, kind, start_ms, end_ms, *, log=print, cancel_check=None, session=None):
        if kind != "fills":
            raise ValueError("Dune 插件仅支持 fills")
        import requests
        session = session or requests.Session()
        url = API.format(query_id=self.settings["query_id"])
        params = {"limit": 10000, "offset": 0, "params.wallet": store.wallet}
        received = added = rejected = 0
        while True:
            if cancel_check:
                cancel_check()
            response = session.get(url, params=params, timeout=60,
                                   headers={"X-Dune-API-Key": os.environ[self.env_key]})
            response.raise_for_status()
            body = response.json()
            rows = (body.get("result") or {}).get("rows") or []
            for row in rows:
                received += 1
                try:
                    fill = normalize_import_fill(row)
                except ValueError:
                    rejected += 1
                    continue
                if start_ms <= fill["time"] <= end_ms:
                    added += store.put("fills", fill, "plugin:dune")
            store.commit()
            if not body.get("next_offset"):
                break
            params["offset"] = body["next_offset"]
        log(f"Dune 查询 {self.settings['query_id']}: 收到 {received:,} 条，新增 {added:,} 条，拒绝 {rejected:,} 条")
        return {"received_rows": received, "new_rows": added, "rejected": rejected}
