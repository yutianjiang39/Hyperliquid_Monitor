"""Point-in-time account snapshot (tier T0): the current-state ground truth.

One run collects every perp DEX clearinghouse, spot balances, account mode,
sub-accounts, vault equity, staking, open orders and fee tier. Snapshots are
appended to the cache so repeated runs build up a position/equity history.

The responses are separate HTTP calls, not an atomic snapshot. The parsed
total is compared with the official ``portfolio`` account value and the gap is
published as ``composition.residual`` rather than hidden.
"""
from __future__ import annotations

import time
from decimal import Decimal

from sources.market_data import STABLECOINS, token_price

ZERO = Decimal(0)


def _d(value):
    try:
        return Decimal(str(value)) if value not in (None, "") else ZERO
    except Exception:
        return ZERO


def _s(value):
    if value is None:
        return None
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def parse_clearinghouse(payload, dex):
    summary = payload.get("marginSummary") or {}
    positions = []
    for entry in payload.get("assetPositions") or []:
        p = entry.get("position") or {}
        szi = _d(p.get("szi"))
        if szi == ZERO:
            continue
        value = _d(p.get("positionValue"))
        lev = p.get("leverage") or {}
        cum = p.get("cumFunding") or {}
        positions.append({
            "coin": p.get("coin"), "dex": dex, "side": "long" if szi > 0 else "short",
            "szi": _s(szi), "entry_px": p.get("entryPx"),
            "mark_px": _s(value / abs(szi)) if szi else None,
            "position_value": _s(value), "unrealized_pnl": p.get("unrealizedPnl"),
            "roe": p.get("returnOnEquity"), "leverage_type": lev.get("type"),
            "leverage": lev.get("value"), "liquidation_px": p.get("liquidationPx"),
            "margin_used": p.get("marginUsed"), "max_leverage": p.get("maxLeverage"),
            "cum_funding_all_time": cum.get("allTime"), "cum_funding_since_open": cum.get("sinceOpen"),
        })
    return {
        "dex": dex, "account_value": summary.get("accountValue"),
        "total_ntl_pos": summary.get("totalNtlPos"), "total_raw_usd": summary.get("totalRawUsd"),
        "margin_used": summary.get("totalMarginUsed"), "withdrawable": payload.get("withdrawable"),
        "maintenance_margin": payload.get("crossMaintenanceMarginUsed"), "positions": positions,
        "time": payload.get("time"),
    }


def parse_spot(payload, spot_prices):
    rows = []
    for b in (payload or {}).get("balances") or []:
        total = _d(b.get("total"))
        if total == ZERO:
            continue
        px = token_price(b.get("coin"), spot_prices)
        value = None if px is None else total * px
        entry = _d(b.get("entryNtl"))
        stable = b.get("coin") in STABLECOINS
        rows.append({
            "coin": b.get("coin"), "token": b.get("token"), "total": _s(total), "hold": b.get("hold"),
            "entry_ntl": _s(entry), "price": None if px is None else _s(px),
            "value": None if value is None else _s(value),
            "unrealized_pnl": None if (value is None or stable or entry == ZERO) else _s(value - entry),
            "stable": stable,
        })
    rows.sort(key=lambda r: -_d(r["value"]))
    return rows


def compose(snapshot, official_equity=None):
    """Estimate total equity from components using the account-mode rule.

    Standard ("disabled"/"default"): spot at mark + sum of every DEX accountValue.
    Unified/portfolio margin: perp margin lives in spot USDC ``hold``; the best
    observed rule is spot at mark - USDC hold + sum of DEX accountValue. The
    official value remains the truth; the residual is reported.
    """
    spot_value = sum((_d(r["value"]) for r in snapshot["spot"] if r["value"] is not None), ZERO)
    perp_value = sum((_d(d["account_value"]) for d in snapshot["dexes"].values()), ZERO)
    usdc_hold = sum((_d(r["hold"]) for r in snapshot["spot"] if r["coin"] == "USDC"), ZERO)
    # Official account value includes staked HYPE (delegated + pending) at mark.
    staking_value = _d((snapshot.get("staking") or {}).get("value_usd"))
    mode = snapshot.get("mode")
    if mode in ("unifiedAccount", "portfolioMargin", "dexAbstraction"):
        rule, total = "spot_mark - usdc_hold + sum(dex accountValue) + staking", spot_value - usdc_hold + perp_value
    else:
        rule, total = "spot_mark + sum(dex accountValue) + staking", spot_value + perp_value
    total += staking_value
    out = {"rule": rule, "spot_value": _s(spot_value), "perp_value": _s(perp_value),
           "usdc_hold": _s(usdc_hold), "staking_value": _s(staking_value), "estimated_total": _s(total),
           "official_equity": None if official_equity is None else _s(_d(official_equity))}
    if official_equity is not None:
        residual = _d(official_equity) - total
        out["residual"] = _s(residual)
        base = abs(_d(official_equity))
        out["residual_pct"] = _s(residual / base * 100) if base else None
    return out


def collect_snapshot(store, client, *, spot_prices=None, dexes=None, log=print, cancel_check=None, now_ms=None):
    """Fetch and persist one snapshot; failures of optional parts are recorded."""
    wallet = store.wallet
    stamp = int(time.time() * 1000) if now_ms is None else int(now_ms)
    errors = {}

    def call(name, body, default=None):
        if cancel_check:
            cancel_check()
        try:
            return client.post(body)
        except Exception as exc:
            errors[name] = str(exc)[:240]
            return default

    if dexes is None:
        listed = call("perpDexs", {"type": "perpDexs"}, [None])
        dexes = [""] + [d["name"] for d in (listed or [])[1:] if isinstance(d, dict) and d.get("name")]
    spot_prices = spot_prices if spot_prices is not None else (store.get_meta("spot_prices", {}) or {})
    snapshot = {"time": stamp, "wallet": wallet, "dexes": {}, "errors": errors}
    snapshot["mode"] = call("userAbstraction", {"type": "userAbstraction", "user": wallet})
    for dex in dexes:
        body = {"type": "clearinghouseState", "user": wallet}
        if dex:
            body["dex"] = dex
        payload = call(f"clearinghouseState:{dex or 'native'}", body)
        if isinstance(payload, dict):
            parsed = parse_clearinghouse(payload, dex)
            # Empty HIP-3 accounts are omitted to keep the snapshot compact.
            if dex == "" or parsed["positions"] or _d(parsed["account_value"]) != ZERO:
                snapshot["dexes"][dex] = parsed
    snapshot["spot"] = parse_spot(call("spotClearinghouseState", {"type": "spotClearinghouseState", "user": wallet}, {}), spot_prices)
    subs = call("subAccounts", {"type": "subAccounts", "user": wallet}) or []
    snapshot["sub_accounts"] = [{
        "address": s.get("subAccountUser"), "name": s.get("name"),
        "account_value": (s.get("clearinghouseState") or {}).get("marginSummary", {}).get("accountValue"),
        "positions": len((s.get("clearinghouseState") or {}).get("assetPositions") or []),
        "spot": parse_spot(s.get("spotState") or {}, spot_prices),
    } for s in subs if isinstance(s, dict)]
    snapshot["vaults"] = [{"vault": v.get("vaultAddress"), "equity": v.get("equity"),
                           "locked_until_ms": v.get("lockedUntilTimestamp")}
                          for v in call("userVaultEquities", {"type": "userVaultEquities", "user": wallet}) or []]
    staking = call("delegatorSummary", {"type": "delegatorSummary", "user": wallet}) or {}
    hype = token_price("HYPE", spot_prices)
    delegated = _d(staking.get("delegated")) + _d(staking.get("undelegated")) + _d(staking.get("totalPendingWithdrawal"))
    snapshot["staking"] = {**staking, "hype_price": _s(hype) if hype is not None else None,
                           "value_usd": _s(delegated * hype) if hype is not None else None}
    orders = call("frontendOpenOrders", {"type": "frontendOpenOrders", "user": wallet}) or []
    snapshot["open_orders"] = len(orders)
    fees = call("userFees", {"type": "userFees", "user": wallet}) or {}
    snapshot["fees"] = {"cross_rate": fees.get("userCrossRate"), "add_rate": fees.get("userAddRate"),
                        "referral_discount": fees.get("activeReferralDiscount"),
                        "staking_discount": (fees.get("activeStakingDiscount") or {}).get("discount"),
                        "daily": [{"date": d.get("date"), "taker": d.get("userCross"), "maker": d.get("userAdd")}
                                  for d in fees.get("dailyUserVlm") or []]}
    limit = call("userRateLimit", {"type": "userRateLimit", "user": wallet}) or {}
    snapshot["cum_volume"] = limit.get("cumVlm")
    store.put("snapshot", snapshot, "api:snapshot")
    store.set_meta("latest_snapshot", snapshot)
    store.commit()
    positions = sum(len(d["positions"]) for d in snapshot["dexes"].values())
    log(f"账户快照：模式 {snapshot['mode']}，{len(snapshot['dexes'])} 个 DEX 账户，{positions} 个持仓，"
        f"{len(snapshot['spot'])} 种现货，{len(snapshot['sub_accounts'])} 个子账户。")
    if errors:
        log("快照部分接口失败：" + "; ".join(f"{k}: {v[:80]}" for k, v in list(errors.items())[:3]))
    return snapshot


def latest_snapshot(store):
    return store.get_meta("latest_snapshot")


def snapshot_positions(snapshot):
    return [p for d in (snapshot or {}).get("dexes", {}).values() for p in d["positions"]]
