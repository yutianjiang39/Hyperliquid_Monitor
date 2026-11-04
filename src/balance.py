"""Event-level HyperCore USDC cash-ledger reconstruction (standard library).

This module never treats portfolio accountValue as a cash balance.  It requires
an explicit, same-scope cash anchor, or a fully decomposed equity anchor.  It
then applies B(t) = B(anchor) + C(t) - C(anchor), where C contains cash flows,
not the market value of open positions.  Spot purchases spend their full quote
amount; perpetual opens do not spend their notional principal.

Scope: one HyperCore address, USDC held in spot/perpetual trading accounts.
Vault shares, supplied lending assets, non-USDC tokens and cross-chain wallets
are excluded.  Borrowed cash is cash, not net worth.  Same-address moves between
spot/perpetual DEX accounts cancel within this scope.  Current account mode must
be handled by the caller when obtaining an anchor.

The strict series refuses known missing contributions.  A separately named
observed series subtracts only known contributions and is explicitly an
estimate when gaps exist.  Neither proves that unobserved records do not exist.

Schema references (checked 2026-11-02):
https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions
https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/nodes/l1-data-schemas
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import Counter
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json


ZERO = Decimal(0)
SCOPE = "hypercore_usdc_cash"
MAX_TIME = 2**63 - 1


def _d(value):
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        value = Decimal(str(value))
        return value if value.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _s(value):
    if value is None:
        return None
    if value == ZERO:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _t(value):
    value = _d(value)
    return int(value) if value is not None and 0 <= value < MAX_TIME and value == value.to_integral_value() else None


def _token(value):
    return str(value).strip().upper().split(":", 1)[0] if value not in (None, "") else None


def _quality(row):
    q = row.get("quality", [])
    return [q] if isinstance(q, str) else list(q or [])


def _key(kind, row):
    if kind == "operation" and row.get("id"):
        return str(row["id"])
    body = {k: row.get(k) for k in ("time", "hash", "delta")}
    if body["delta"] is None:
        body["delta"] = {k: v for k, v in row.items() if not k.startswith("_") and k not in ("source", "quality")}
    return kind + ":" + hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:24]


def _event(kind, row, known, unknown, flags, index):
    delta = row.get("delta") if isinstance(row.get("delta"), dict) else {}
    return {"time": _t(row.get("time")), "id": _key(kind, row), "kind": kind,
            "coin": row.get("coin") or delta.get("coin"),
            "operation_id": row.get("id") if kind == "operation" else None,
            "source": row.get("source") or row.get("_source") or "import",
            "known": known, "unknown": int(bool(unknown)), "quality": list(dict.fromkeys(flags)),
            "order": index, "rank": {"funding": 0, "ledger": 1, "operation": 2}.get(kind, 3)}


def _cash_fee(value, token):
    """Return the USDC cash fee, independently of the trade's PnL currency."""
    fee, currency = _d(value), _token(token)
    if fee == ZERO:
        return ZERO, False
    if currency and currency != "USDC":
        return ZERO, False
    if fee is None or currency is None:
        return ZERO, True
    return fee, False


def _operation_flow(row, meta, index):
    coin = str(row.get("coin") or "")
    market = row.get("market_type") or meta.get("market_type")
    if not market:
        market = "spot" if coin.startswith("@") or "/" in coin else "perp"
    known, unknown, flags = ZERO, False, []
    if market == "spot":
        pair = str(meta.get("display_name") or row.get("display_name") or coin).split("/")
        base = _token(meta.get("base_token") or (pair[0] if len(pair) == 2 else None))
        quote = _token(meta.get("quote_token") or (pair[1] if len(pair) == 2 else None))
        side = str(row.get("side") or "").upper()
        direction = 1 if side in ("B", "BUY", "BID") or row.get("action") == "buy" else -1 if side in ("A", "SELL", "ASK", "S") or row.get("action") == "sell" else None
        size, price = _d(row.get("size")), _d(row.get("price"))
        if quote == "USDC":
            if direction is None or size is None or price is None or size < ZERO or price <= ZERO:
                unknown = True
                flags.append("invalid_spot_cash_flow")
            else:
                known -= direction * size * price
        elif base == "USDC":
            if direction is None or size is None or size < ZERO:
                unknown = True
                flags.append("invalid_spot_cash_flow")
            else:
                known += direction * size
        elif quote is None or base is None:
            unknown = True
            flags.append("unknown_spot_cash_currency")
        else:
            flags.append("non_usdc_spot_cash_excluded")
        # Spot closedPnl is a cost-basis statistic, not another cash receipt.
    elif market == "perp":
        currency = _token(row.get("pnl_token") or meta.get("pnl_token") or meta.get("collateral_token") or ("USDC" if ":" not in coin else None))
        pnl = _d(row.get("closed_pnl"))
        if currency == "USDC":
            if pnl is None:
                unknown = True
                flags.append("unknown_realized_pnl")
            else:
                known += pnl
        elif currency is None:
            if pnl != ZERO:
                unknown = True
                flags.append("unknown_perp_settlement_currency")
        else:
            flags.append("non_usdc_perp_pnl_excluded")
    else:
        unknown = True
        flags.append("unknown_market_cash_effect")
    fee, missing_fee = _cash_fee(row.get("fee"), row.get("fee_token"))
    known -= fee  # builder_fee is already included in fee: never subtract it.
    if missing_fee:
        unknown = True
        flags.append("unknown_usdc_fee")
    uncertain = {"duplicate_conflict", "invalid_fill", "position_discontinuity"}
    matched = uncertain.intersection(_quality(row))
    if matched:
        unknown = True
        flags.extend(sorted(matched))
    if "unresolved_same_time_order" in _quality(row):
        flags.append("same_millisecond_operation_order_uncertain")
    return _event("operation", row, known, unknown, flags, index)


def _funding_flow(row, meta, index):
    delta = row.get("delta") or row
    if not isinstance(delta, dict):
        return _event("funding", row, ZERO, True, ["invalid_funding_payload"], index)
    coin = str(delta.get("coin") or "")
    currency = _token(delta.get("token") or delta.get("pnl_token") or row.get("pnl_token") or meta.get("pnl_token") or ("USDC" if ":" not in coin else None))
    value = _d(delta.get("usdc", delta.get("amount")))
    flags, unknown = [], False
    if currency == "USDC":
        if value is None:
            value, unknown = ZERO, True
            flags.append("unknown_funding_amount")
    elif currency is None:
        value, unknown = ZERO, True
        flags.append("unknown_funding_currency")
    else:
        value = ZERO
        flags.append("non_usdc_funding_excluded")
    return _event("funding", row, value, unknown, flags, index)


def _ledger_flow(row, wallet, index, fee_modes):
    d = row.get("delta") or row
    if not isinstance(d, dict):
        return _event("ledger", row, ZERO, True, ["invalid_ledger_payload"], index)
    kind = str(d.get("type") or row.get("type") or "unknown")
    token = _token(d.get("token") or ("USDC" if d.get("usdc") is not None else None))
    amount = _d(d.get("usdc", d.get("amount")))
    known, unknown, flags = ZERO, False, []
    sender = str(d.get("user") or d.get("sender") or "").lower()
    destination = str(d.get("destination") or "").lower()
    payer = False
    fee_kind = None

    if wallet and str(d.get("vault") or "").lower() == wallet:
        result = _event("ledger", row, ZERO, True, ["vault_account_cash_effect_unsupported"], index)
        result["ledger_type"] = kind
        return result

    if kind in ("accountClassTransfer", "perpDexClassTransfer"):
        flags.append("same_address_cash_transfer_cancelled")
    elif kind == "vaultWithdraw":
        amount = _d(d.get("netWithdrawnUsd"))
        if amount is None:
            unknown = True
            flags.append("vault_withdraw_net_amount_missing")
        else:
            known = amount  # commission/closingCost are not deducted again.
    elif kind in ("deposit", "vaultDistribution", "vaultLeaderCommission"):
        if amount is None:
            unknown = True
        else:
            known = amount
    elif kind in ("withdraw", "vaultDeposit", "vaultCreate"):
        if amount is None:
            unknown = True
        else:
            known = -amount
        payer = kind in ("withdraw", "vaultCreate")
        fee_kind = kind
    elif kind in ("internalTransfer", "subAccountTransfer", "spotTransfer", "send"):
        if kind in ("internalTransfer", "subAccountTransfer"):
            token = token or "USDC"
        if sender == wallet and destination == wallet:
            flags.append("same_address_cash_transfer_cancelled")
            payer = True
        elif sender == wallet:
            payer = True
            if token == "USDC" and amount is not None:
                known = -amount
            elif token is None or (token == "USDC" and amount is None):
                unknown = True
        elif destination == wallet:
            if token == "USDC" and amount is not None:
                known = amount
            elif token is None or (token == "USDC" and amount is None):
                unknown = True
        else:
            unknown = True
            flags.append("transfer_direction_unknown")
        fee_kind = "transfer"
    elif kind in ("spotGenesis", "rewardsClaim"):
        token = token or ("USDC" if kind == "rewardsClaim" else None)
        if token == "USDC" and amount is not None:
            known = amount
        elif token is None or (token == "USDC" and amount is None):
            unknown = True
    elif kind == "borrowLend":
        op = str(d.get("operation") or "").lower()
        flags.append("lending_cash_not_net_assets")
        if token == "USDC":
            if amount is None:
                unknown = True
            elif op in ("supply", "repay"):
                known = -amount
            elif op in ("withdraw", "borrow"):
                known = amount  # amount includes interestAmount; do not add it twice.
            else:
                unknown = True
        elif token is None:
            unknown = True
    elif kind in ("accountActivationGas", "deployGasAuction"):
        if token == "USDC" and amount is not None:
            known = -amount
        elif token is None:
            unknown = True
    elif kind in ("cStakingTransfer", "stakingTransfer", "delegate", "undelegate") and token not in (None, "USDC"):
        flags.append("non_usdc_staking_excluded")
    elif kind == "liquidation":
        # accountValue is a state/value, not a signed wallet cash delta.
        unknown = True
        flags.append("liquidation_cash_effect_unknown")
    elif token and token != "USDC":
        flags.append("non_usdc_ledger_excluded")
    else:
        unknown = True
        flags.append("unsupported_usdc_ledger_type:" + kind)

    if payer:
        fee = _d(d.get("fee"))
        fee_token = _token(d.get("feeToken") or ("USDC" if kind in ("withdraw", "vaultCreate", "internalTransfer") else token))
        if fee is not None and fee != ZERO and fee_token in (None, "USDC"):
            mode = d.get("fee_included")
            mode = "included" if mode is True else "additional" if mode is False else fee_modes.get(fee_kind, "unknown")
            if mode == "additional":
                known -= fee
            elif mode != "included":
                unknown = True
                flags.append(str(fee_kind) + "_fee_convention_unverified")
        elif fee is None and "fee" in d and fee_token in (None, "USDC"):
            unknown = True
            flags.append("unknown_ledger_fee")
    if unknown and not flags:
        flags.append("unknown_ledger_cash_amount")
    result = _event("ledger", row, known, unknown, flags, index)
    result["ledger_type"] = kind
    return result


def normalize_balance_anchors(anchors):
    """Accept explicit USDC cash or explicitly decomposed same-scope equity.

    Cash: {time, balance_usdc|balance, scope: SCOPE, source, validated: true}.
    Decomposed equity additionally requires equity_scope_verified:true and all
    of equity_value, unrealized_pnl, non_cash_net_value, where the last field
    includes net non-cash assets/liabilities outside the target cash scope.
    A naked accountValue or equity snapshot is deliberately rejected.
    All snapshots are interpreted as the balance AFTER events at that time.
    """
    accepted, diagnostics = {}, []
    for index, original in enumerate(anchors or []):
        if not isinstance(original, dict):
            diagnostics.append({"type": "invalid_anchor", "index": index})
            continue
        row = dict(original)
        stamp = _t(row.get("time"))
        scope = row.get("scope")
        value = _d(row.get("balance_usdc", row.get("balance")))
        method = "explicit_cash_balance"
        if value is None and row.get("equity_scope_verified") is True:
            parts = [_d(row.get(key)) for key in ("equity_value", "unrealized_pnl", "non_cash_net_value")]
            if all(p is not None for p in parts):
                value = parts[0] - parts[1] - parts[2]
                method = "explicit_equity_decomposition"
        if stamp is None or value is None or scope != SCOPE or _token(row.get("token") or "USDC") != "USDC":
            diagnostics.append({"type": "rejected_anchor", "index": index,
                                "time": stamp, "reason": "cash_amount_scope_or_decomposition_missing"})
            continue
        normalized = {"time": stamp, "balance_usdc": _s(value), "balance": _s(value),
                      "scope": SCOPE, "source": str(row.get("source") or "manual_cash_anchor"),
                      "validated": row.get("validated") is True, "method": method,
                      "quality": _quality(row)}
        if stamp in accepted and accepted[stamp]["balance_usdc"] != normalized["balance_usdc"]:
            diagnostics.append({"type": "conflicting_anchor", "time": stamp,
                                "old": accepted[stamp]["balance_usdc"], "new": normalized["balance_usdc"]})
            normalized["validated"] = False
            normalized["quality"].append("conflicting_cash_anchors")
        accepted[stamp] = normalized
    return [accepted[t] for t in sorted(accepted)], diagnostics


def build_balance_curve(operations, funding=None, ledger=None, *, wallet="", asset_meta=None,
                        anchors=None, start=0, end=None, event_times=None, coverage=None):
    """Build an event timeline and anchor diagnostics without querying prices.

    Pass ALL records through the latest anchor even when report end is earlier.
    ``event_times`` accepts timestamps or curve rows; operation_id + sequence map
    each original position event without coalescing same-millisecond reversals.
    ``coverage`` may contain complete, observed_through_ms, available_fill_start_ms,
    gaps (start_ms/end_ms)
    and ledger_fee_modes ({withdraw|transfer|vaultCreate: included|additional}).
    Unproven historical completeness stays separate from calculable arithmetic.
    """
    with localcontext() as context:
        context.prec = 60
        return _build(list(operations or []), list(funding or []), list(ledger or []),
                      wallet=str(wallet).lower(), asset_meta=asset_meta or {}, anchors=anchors or [],
                      start=start, end=end, event_times=event_times or [], coverage=coverage or {})


def _build(operations, funding, ledger, *, wallet, asset_meta, anchors, start, end, event_times, coverage):
    start = _t(start) or 0
    end = _t(end) if end is not None else MAX_TIME
    if end is None or end < start:
        raise ValueError("余额曲线结束时间不得早于开始时间。")
    normalized_anchors, diagnostics = normalize_balance_anchors(anchors)
    fee_modes = coverage.get("ledger_fee_modes") or {}
    flows, seen = [], {}
    for kind, rows in (("operation", operations), ("funding", funding), ("ledger", ledger)):
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or _t(row.get("time")) is None:
                diagnostics.append({"type": "invalid_event", "kind": kind, "index": index})
                continue
            if kind == "operation":
                info = asset_meta.get(str(row.get("coin") or ""), {})
                flow = _operation_flow(row, info if isinstance(info, dict) else {}, index)
            elif kind == "funding":
                delta = row.get("delta") or row
                coin = str(delta.get("coin") or "") if isinstance(delta, dict) else ""
                info = asset_meta.get(coin, {})
                flow = _funding_flow(row, info if isinstance(info, dict) else {}, index)
            else:
                flow = _ledger_flow(row, wallet, index, fee_modes)
            key = (kind, flow["id"])
            if key in seen:
                previous = seen[key]
                if previous["known"] != flow["known"] or previous["unknown"] != flow["unknown"]:
                    previous["unknown"] = 1
                    previous["quality"].append("duplicate_cash_flow_conflict")
                    diagnostics.append({"type": "duplicate_cash_flow_conflict", "id": flow["id"]})
                continue
            seen[key] = flow
            flows.append(flow)

    # Explicit known coverage gaps are uncertainty barriers, never zero flows.
    for index, gap in enumerate(coverage.get("gaps") or []):
        if not isinstance(gap, dict):
            continue
        stop = _t(gap.get("end_ms"))
        if stop is not None:
            flows.append({"time": stop, "id": "coverage-gap:" + str(index), "kind": "coverage_gap",
                          "known": ZERO, "unknown": 1, "quality": ["known_history_gap"],
                          "order": index, "rank": -1, "operation_id": None, "source": "coverage"})
    through = _t(coverage.get("observed_through_ms"))
    if through is not None:
        for anchor in normalized_anchors:
            if anchor["time"] > through:
                flows.append({"time": anchor["time"], "id": "anchor-after-history:" + str(anchor["time"]),
                              "kind": "coverage_gap", "known": ZERO, "unknown": 1,
                              "quality": ["cash_anchor_newer_than_history"], "order": 0, "rank": -1,
                              "operation_id": None, "source": "coverage"})
    flows.sort(key=lambda f: (f["time"], f["rank"], f["order"], f["id"]))
    times = [f["time"] for f in flows]
    sums, unknown_counts = [ZERO], [0]
    for flow in flows:
        sums.append(sums[-1] + flow["known"])
        unknown_counts.append(unknown_counts[-1] + flow["unknown"])
    anchor_times = [a["time"] for a in normalized_anchors]
    for anchor in normalized_anchors:
        anchor["_index"] = bisect_right(times, anchor["time"])
    conflicts = []
    for left, right in zip(normalized_anchors, normalized_anchors[1:]):
        lo, hi = left["_index"], right["_index"]
        residual = _d(right["balance_usdc"]) - _d(left["balance_usdc"]) - (sums[hi] - sums[lo])
        missing = unknown_counts[hi] - unknown_counts[lo]
        tolerance = _d(coverage.get("anchor_tolerance_usdc")) or Decimal("0.000001")
        diagnostics.append({"type": "anchor_reconciliation", "start_ms": left["time"], "end_ms": right["time"],
                            "residual_usdc": _s(residual), "unknown_contributions": missing,
                            "matched": abs(residual) <= tolerance and missing == 0})
        if abs(residual) > tolerance and missing == 0:
            conflicts.append((left["time"], right["time"]))

    sequence_map = {}
    extra = []
    for i, event in enumerate(event_times):
        if isinstance(event, dict):
            stamp = _t(event.get("time"))
            if event.get("operation_id"):
                sequence_map[str(event["operation_id"])] = event.get("sequence")
                continue
            kind = event.get("event_type") or event.get("kind") or ("boundary" if event.get("action") == "boundary" else "equity_sample")
        else:
            stamp, kind = _t(event), "sample"
        if stamp is not None:
            is_boundary = kind in ("boundary", "window_boundary")
            extra.append({"time": stamp, "id": "sample:" + str(i), "kind": kind, "rank": -2 if is_boundary else 9,
                          "operation_id": None, "order": i, "known": ZERO, "unknown": 0,
                          "quality": ["window_boundary"] if is_boundary else [], "source": "timeline",
                          "_prefix": bisect_left(times, stamp) if is_boundary else bisect_right(times, stamp)})
    for i, anchor in enumerate(normalized_anchors):
        extra.append({"time": anchor["time"], "id": "anchor:" + str(i), "kind": "balance_snapshot",
                      "rank": 10, "order": i, "known": ZERO, "unknown": 0, "quality": [],
                      "operation_id": None, "source": anchor["source"], "_prefix": anchor["_index"]})
    timeline = [dict(flow, _prefix=i + 1) for i, flow in enumerate(flows)] + extra
    timeline.sort(key=lambda f: (f["time"], f["rank"], f["order"], f["id"]))
    kind_sets = {}
    for row in flows:
        kind_sets.setdefault(row["time"], set()).add(row["kind"])
    rows = []
    unknown_types = Counter()
    for row in flows:
        if row["unknown"]:
            unknown_types.update(row["quality"] or ["unknown_contribution"])
    unplaced_events = any(d["type"] == "invalid_event" for d in diagnostics)
    available_start = _t(coverage.get("available_fill_start_ms"))

    for item in timeline:
        stamp, prefix = item["time"], item["_prefix"]
        if not start <= stamp <= end:
            continue
        quality = list(item["quality"])
        candidate = None
        if normalized_anchors:
            ai = bisect_left(anchor_times, stamp)
            right = normalized_anchors[ai] if ai < len(normalized_anchors) else None
            left = normalized_anchors[ai - 1] if ai > 0 else None
            candidates = [a for a in (right, left) if a is not None]
            # Prefer the next snapshot (backward accounting), but a past known
            # anchor can rescue a segment separated from the next by a gap.
            candidate = next((a for a in candidates if a["validated"] and unknown_counts[a["_index"]] == unknown_counts[prefix]), candidates[0])
        observed, strict, count_between = None, None, 0
        anchor_time, source = None, None
        complete = False
        if candidate is not None:
            anchor_time, source = candidate["time"], candidate["source"]
            observed = _d(candidate["balance_usdc"]) + sums[prefix] - sums[candidate["_index"]]
            count_between = abs(unknown_counts[prefix] - unknown_counts[candidate["_index"]])
            mismatch = any(lo < stamp < hi for lo, hi in conflicts)
            exact_snapshot = item["kind"] == "balance_snapshot" and stamp == candidate["time"]
            complete = candidate["validated"] and count_between == 0 and (not mismatch or exact_snapshot)
            if complete:
                strict = observed
            if not candidate["validated"]:
                quality.append("cash_anchor_unvalidated")
            if count_between:
                quality.append("unknown_components_between_anchor")
            if mismatch:
                quality.append("cash_anchors_do_not_reconcile")
            quality.extend(candidate["quality"])
            if not exact_snapshot:
                quality.append("cash_ledger_reconstruction")
        else:
            quality.append("cash_anchor_missing")
        if coverage.get("complete") is not True:
            quality.append("history_coverage_unverified")
        if len(kind_sets.get(stamp, ())) > 1:
            quality.append("same_millisecond_cross_source_order_unknown")
        after_all_at_time = prefix == bisect_right(times, stamp)
        order_uncertain = (len(kind_sets.get(stamp, ())) > 1 or
                           "same_millisecond_operation_order_uncertain" in quality)
        if order_uncertain and not after_all_at_time:
            complete = False
            strict = None
        if unplaced_events and item["kind"] != "balance_snapshot":
            quality.append("unplaced_cash_event")
            complete = False
            strict = None
        if available_start is not None and stamp < available_start and item["kind"] != "balance_snapshot":
            quality.append("before_available_fill_history")
            complete = False
            strict = None
        delta = _s(item["known"]) if not item["unknown"] else None
        row = {"time": stamp, "sequence": len(rows) + 1,
               "event_sequence": sequence_map.get(str(item.get("operation_id"))),
               "operation_id": item.get("operation_id"), "event_id": item["id"],
               "event_type": item["kind"], "kind": item["kind"], "coin": item.get("coin"),
               "ledger_type": item.get("ledger_type"), "delta_usdc": delta, "delta": delta,
               "known_delta_usdc": _s(item["known"]),
               "balance_usdc": _s(strict), "balance": _s(strict),
               "observed_balance_usdc": _s(observed), "observed_balance": _s(observed),
               "cumulative_known_delta_usdc": _s(sums[prefix]),
               "anchor_time": anchor_time, "anchor_source": source,
               "balance_complete": complete, "history_coverage_verified": coverage.get("complete") is True,
               "unknown_contributions_to_anchor": count_between,
               "quality": list(dict.fromkeys(quality)), "scope": SCOPE}
        rows.append(row)

    warnings = [
        "账面余额范围为同一 HyperCore 地址的 USDC 现金（现货和 USDC 永续），不含非 USDC 现货市值、Vault 份额及借贷投资；不等于综合账户净值或可提现余额。",
        "余额逐事件按基准点和资金流水反推；不请求历史价格，不重建持仓浮盈亏。已知流水估计值不保证历史无遗漏。",
        "现货买卖计入实际 USDC 收支；永续只计入已实现盈亏及费用，不扣开仓名义本金；builderFee 不重复扣费。",
    ]
    if not normalized_anchors:
        warnings.append("尚无同口径的 USDC 账面余额基准点；绝对余额与相应杠杆留空，请联网更新余额或导入明确的余额快照。")
    if unknown_types:
        warnings.append("部分现金贡献或历史区间不明；严格余额在跨越这些缺项时留空，已知流水余额仅作估计。")
    if conflicts:
        warnings.append("多个现金余额基准点与已知流水未能核对一致；对应区间严格余额留空，差额未伪装成入金或收益。")
    if any("lending_cash_not_net_assets" in f["quality"] for f in flows):
        warnings.append("借贷转入转出只改变本图现金；借入款不等于新增净资产，借贷资产与负债不在本图中轧差。")
    cleaned_anchors = [{k: v for k, v in a.items() if not k.startswith("_")} for a in normalized_anchors]
    exported_flows = [{"time": f["time"], "id": f["id"], "kind": f["kind"],
                       "operation_id": f.get("operation_id"), "ledger_type": f.get("ledger_type"),
                       "known_delta_usdc": _s(f["known"]), "delta_usdc": _s(f["known"]) if not f["unknown"] else None,
                       "quality": f["quality"], "source": f["source"]} for f in flows]
    return {"version": "2.3.0", "scope": SCOPE, "balance_curve": rows, "flow_events": exported_flows,
            "anchors": cleaned_anchors, "diagnostics": diagnostics, "warnings": warnings,
            "summary": {"balance_points": len(rows), "strict_balance_points": sum(r["balance"] is not None for r in rows),
                        "observed_balance_points": sum(r["observed_balance"] is not None for r in rows),
                        "cash_anchor_count": len(cleaned_anchors), "unknown_cash_events": sum(f["unknown"] for f in flows),
                        "unknown_cash_reasons": dict(unknown_types), "anchor_reconciliation_conflicts": len(conflicts),
                        "history_coverage_verified": coverage.get("complete") is True,
                        "ledger_fee_modes": dict(fee_modes), "historical_prices_requested": False,
                        "unrealized_pnl_reconstructed": False}}
