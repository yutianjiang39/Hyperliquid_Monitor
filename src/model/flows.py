"""Ledger classification: which cash movements are capital flows vs PnL.

Classes:
  external   deposits/withdrawals and transfers to/from foreign addresses
  subaccount transfers between this master and its sub-accounts
  internal   same-address moves (spot<->perp, between DEXes): net zero
  vault      vault deposits/withdrawals/distributions
  staking    HYPE moved into/out of staking
  liquidation  informational, no cash amount
  other      rewards, genesis airdrops, gas
``amount_usd`` is signed from the wallet's perspective (+ = into the account).
"""
from __future__ import annotations

from decimal import Decimal

from sources.market_data import STABLECOINS, token_price

ZERO = Decimal(0)
CLASS_LABELS = {"external": "外部出入金", "subaccount": "子账户划转", "internal": "内部划转",
                "vault": "金库", "staking": "质押", "liquidation": "强平", "other": "其他"}


def _d(value):
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except Exception:
        return None


def _s(value):
    if value is None:
        return None
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def classify(row, wallet, sub_accounts=(), spot_prices=None):
    d = row.get("delta") or {}
    typ = d.get("type", "unknown")
    sender, dest = str(d.get("user", "")).lower(), str(d.get("destination", "")).lower()
    subs = {s.lower() for s in sub_accounts}
    token = d.get("token") or ("USDC" if d.get("usdc") is not None else None)
    amount, fee, cls, sign, approx = None, _d(d.get("fee")) or ZERO, "other", 1, False
    if typ == "deposit":
        cls, amount = "external", _d(d.get("usdc"))
    elif typ == "withdraw":
        cls, amount, sign = "external", _d(d.get("usdc")), -1
    elif typ in ("accountClassTransfer", "perpDexClassTransfer"):
        cls, amount, sign = "internal", _d(d.get("usdc")), 0
    elif typ in ("internalTransfer", "subAccountTransfer", "spotTransfer", "send"):
        amount = _d(d.get("usdcValue")) if d.get("usdcValue") is not None else _d(d.get("usdc"))
        if amount is None and token in STABLECOINS:
            amount = _d(d.get("amount"))
        if sender == wallet and dest == wallet:
            cls, sign = "internal", 0
        else:
            sign = 1 if dest == wallet else -1
            other = sender if sign > 0 else dest
            cls = "subaccount" if typ == "subAccountTransfer" or other in subs else "external"
    elif typ == "vaultDeposit":
        cls, amount, sign = "vault", _d(d.get("usdc")), -1
    elif typ == "vaultCreate":
        cls, amount, sign = "vault", _d(d.get("usdc")), -1
    elif typ == "vaultWithdraw":
        cls = "vault"
        amount = _d(d.get("netWithdrawnUsd")) if d.get("netWithdrawnUsd") is not None else _d(d.get("requestedUsd"))
        approx = d.get("netWithdrawnUsd") is None
    elif typ in ("vaultDistribution", "vaultLeaderCommission"):
        cls, amount = "vault", _d(d.get("usdc"))
    elif typ == "cStakingTransfer":
        cls = "staking"
        sign = -1 if d.get("isDeposit") else 1
        qty, px = _d(d.get("amount")), token_price(token or "HYPE", spot_prices)
        amount, approx = (None if qty is None or px is None else qty * px), True
    elif typ == "liquidation":
        cls, sign = "liquidation", 0
    elif typ in ("rewardsClaim", "spotGenesis"):
        qty = _d(d.get("amount")) if d.get("amount") is not None else _d(d.get("usdc"))
        px = token_price(token or "USDC", spot_prices)
        amount, approx = (None if qty is None or px is None else qty * px), token not in STABLECOINS
    elif typ in ("accountActivationGas", "deployGasAuction"):
        amount, sign = _d(d.get("usdc") or d.get("amount")), -1
    if amount is None and token and token not in STABLECOINS and d.get("amount") is not None and sign:
        px = token_price(token, spot_prices)
        qty = _d(d.get("amount"))
        if px is not None and qty is not None:
            amount, approx = qty * px, True
    signed = None if amount is None else amount * sign
    return {"time": row.get("time"), "hash": row.get("hash"), "type": typ, "class": cls,
            "class_label": CLASS_LABELS[cls], "token": token, "amount": _s(_d(d.get("amount")) or _d(d.get("usdc"))),
            "amount_usd": _s(signed), "fee_usd": _s(fee) if token in STABLECOINS or token is None else None,
            "approx_value": approx, "counterparty": dest if sign < 0 else (sender if sign > 0 else None),
            "vault": d.get("vault"), "liquidated_ntl": d.get("liquidatedNtlPos"),
            "liquidated_positions": d.get("liquidatedPositions")}


def classify_all(ledger, wallet, sub_accounts=(), spot_prices=None):
    return [classify(row, wallet, sub_accounts, spot_prices) for row in ledger]


CAPITAL_CLASSES = ("external", "subaccount", "vault", "staking")


def net_capital_flows(flows, classes=CAPITAL_CLASSES):
    return sum((_d(f["amount_usd"]) or ZERO for f in flows if f["class"] in classes), ZERO)
