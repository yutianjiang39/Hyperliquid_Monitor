"""Secondary detail streams: order history sample and staking history/rewards."""
from __future__ import annotations


def collect_extras(store, client, *, log=print, cancel_check=None):
    wallet, result = store.wallet, {}
    for endpoint, kind in (("historicalOrders", "orders"), ("delegatorHistory", "staking"),
                           ("delegatorRewards", "staking_rewards")):
        if cancel_check:
            cancel_check()
        try:
            rows = client.post({"type": endpoint, "user": wallet}) or []
        except Exception as exc:
            result[endpoint] = {"status": "failed", "error": str(exc)[:240]}
            continue
        added = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            if kind == "orders":
                order = dict(row.get("order") or {})
                row = {**order, "status": row.get("status"),
                       "time": row.get("statusTimestamp") or order.get("timestamp")}
            if row.get("time") is None:
                continue
            added += store.put(kind, row, "api:" + endpoint)
        result[endpoint] = {"status": "ok", "rows": len(rows), "new_rows": added,
                            "note": "historicalOrders 仅返回最近 2000 条，属于近期样本" if kind == "orders" else None}
    store.set_meta("extras_last_run", result)
    store.commit()
    log("订单样本/质押记录：" + ", ".join(f"{k} {v.get('rows', v.get('status'))}" for k, v in result.items()))
    return result
