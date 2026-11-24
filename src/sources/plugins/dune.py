"""Dune: run a user-owned saved query for one wallet and read that execution.

The query must accept a ``wallet`` parameter and return one row per fill with
Hyperliquid column names (time, coin, side, px, sz, startPosition, closedPnl,
fee, feeToken, tid, oid, hash, dir). Column aliases from imports are accepted.

Wallet safety: reading "the latest results" of a saved query returns whatever
wallet it last ran for. Instead this plugin executes the query with
``query_parameters={"wallet": ...}`` (POST /query/{id}/execute), waits on that
``execution_id`` and pages /execution/{id}/results, so every row comes from an
execution parameterised with this wallet. When a row also carries a wallet
column (user / wallet / address / trader) it must match, else it is rejected.
"""
from __future__ import annotations

import os
import time

from collector import normalize_import_fill
from sources.plugins.base import SourcePlugin

BASE = "https://api.dune.com/api/v1"
WALLET_COLUMNS = ("user", "wallet", "address", "trader", "account")
DONE = "QUERY_STATE_COMPLETED"
FAILED = ("QUERY_STATE_FAILED", "QUERY_STATE_CANCELLED", "QUERY_STATE_EXPIRED")


def row_wallet(row):
    """Wallet named by a result row, if any column carries one."""
    for key in WALLET_COLUMNS:
        value = row.get(key)
        if isinstance(value, str) and value.lower().startswith("0x") and len(value) == 42:
            return value.lower()
    return None


class Dune(SourcePlugin):
    name = "dune"
    env_key = "DUNE_API_KEY"
    capabilities = frozenset({"fills"})
    cost_hint = "按 Dune credits 计费（每次补数会执行一次参数化查询）；需要自行维护返回 Hyperliquid 字段的查询。"

    def available(self):
        return super().available() and bool(self.settings.get("query_id"))

    def _headers(self):
        return {"X-Dune-API-Key": os.environ[self.env_key]}

    def execute(self, session, wallet, *, cancel_check=None, poll=2.0, timeout=600, sleep=time.sleep):
        """Start an execution for ``wallet`` and wait for it; returns the execution id."""
        response = session.post(f"{BASE}/query/{self.settings['query_id']}/execute", timeout=60,
                                json={"query_parameters": {"wallet": wallet}}, headers=self._headers())
        response.raise_for_status()
        execution = response.json()["execution_id"]
        waited = 0.0
        while True:
            if cancel_check:
                cancel_check()
            status = session.get(f"{BASE}/execution/{execution}/status", timeout=60, headers=self._headers())
            status.raise_for_status()
            state = status.json().get("state")
            if state == DONE:
                return execution
            if state in FAILED:
                raise RuntimeError(f"Dune 执行 {execution} 结束于 {state}")
            if waited >= timeout:
                raise TimeoutError(f"Dune 执行 {execution} 超过 {timeout}s 未完成")
            sleep(poll)
            waited += poll

    def test(self, wallet):
        import requests
        session = requests.Session()
        execution = self.execute(session, wallet)
        response = session.get(f"{BASE}/execution/{execution}/results", params={"limit": 1}, timeout=60,
                               headers=self._headers())
        response.raise_for_status()
        rows = (response.json().get("result") or {}).get("rows") or []
        owner = row_wallet(rows[0]) if rows else None
        note = "" if owner is None else ("，钱包列一致" if owner == wallet.lower() else "，钱包列不一致！")
        return {"ok": owner in (None, wallet.lower()), "detail": f"参数化执行 {execution} 可读，返回 {len(rows)} 行样本{note}"}

    def fetch(self, store, kind, start_ms, end_ms, *, log=print, cancel_check=None, session=None, sleep=time.sleep):
        if kind != "fills":
            raise ValueError("Dune 插件仅支持 fills")
        if session is None:
            import requests
            session = requests.Session()
        wallet = store.wallet.lower()
        execution = self.execute(session, wallet, cancel_check=cancel_check, sleep=sleep)
        url = f"{BASE}/execution/{execution}/results"
        params = {"limit": 10000, "offset": 0}
        received = added = rejected = foreign = 0
        with_column = 0
        while True:
            if cancel_check:
                cancel_check()
            response = session.get(url, params=params, timeout=60, headers=self._headers())
            response.raise_for_status()
            body = response.json()
            rows = (body.get("result") or {}).get("rows") or []
            for row in rows:
                received += 1
                owner = row_wallet(row)
                if owner is not None:
                    with_column += 1
                    if owner != wallet:
                        foreign += 1
                        continue
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
        log(f"Dune 查询 {self.settings['query_id']}（执行 {execution}）：收到 {received:,} 条，新增 {added:,} 条，"
            f"拒绝 {rejected:,} 条，其他钱包 {foreign:,} 条")
        return {"received_rows": received, "new_rows": added, "rejected": rejected, "foreign_wallet_rows": foreign,
                "rows_with_wallet_column": with_column, "execution_id": execution}
