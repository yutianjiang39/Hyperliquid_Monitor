"""Operation-level accounting curves without historical mark-price requests.

Decimal strings preserve source precision.  None always means unavailable.
The cost basis is reconstructed from observed fills; the trade-price basis is
explicitly an estimate using each market's most recently observed fill price.
Neither is historical mark-to-market exposure. Account equity is the exchange's
sampled portfolio history; a separate ledger module can add reconstructed cash
balances without changing those observations.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from decimal import Decimal, InvalidOperation, localcontext

ZERO = Decimal(0)
DAY_MS = 86_400_000
PORTFOLIO_KEYS = ("allTime", "month", "week", "day")


def _d(value):
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _s(value):
    if value is None:
        return None
    if value == ZERO:
        return "0"
    return format(value, "f").rstrip("0").rstrip(".") if value.as_tuple().exponent < 0 else format(value, "f")


def _t(value):
    number = _d(value)
    if number is None or number < 0 or number != number.to_integral_value():
        return None
    return int(number)


def _included(row, position_scope="usdc_perps"):
    coin = str(row.get("coin") or "")
    token = row.get("pnl_token") or ("USDC" if ":" not in coin else None)
    return (row.get("market_type") == "perp" and str(token).upper() == "USDC"
            and (position_scope != "native" or ":" not in coin))


def _qualities(row):
    value = row.get("quality") or []
    return [value] if isinstance(value, str) else [str(x) for x in value]


def normalize_portfolio(portfolio):
    """Union actual samples; finer windows win only exact-time collisions.

    A non-null ``*_source`` with a null equity is an invalid source sample and
    must break a chart. A null source simply means this timestamp belongs only
    to the other account series. There is no interpolation or forward padding.
    """
    blocks = {}
    invalid = 0
    if isinstance(portfolio, dict):
        iterable = portfolio.items()
    elif isinstance(portfolio, (list, tuple)):
        iterable = portfolio
    else:
        iterable = []
    for item in iterable:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            invalid += 1
            continue
        key, payload = item
        if isinstance(payload, dict):
            blocks[str(key)] = payload
    merged = {}
    counts = {}
    for prefix, field in (("perp", "perp"), ("", "account")):
        samples = {}
        for suffix in PORTFOLIO_KEYS:
            key = prefix + suffix[0].upper() + suffix[1:] if prefix else suffix
            rows = blocks.get(key, {}).get("accountValueHistory", [])
            valid = {}
            if not isinstance(rows, (list, tuple)):
                invalid += 1
                continue
            for item in rows:
                if not isinstance(item, (list, tuple)) or len(item) < 2:
                    invalid += 1
                    continue
                stamp = _t(item[0])
                if stamp is None:
                    invalid += 1
                    continue
                value = _d(item[1])
                if value is None:
                    invalid += 1
                valid[stamp] = (value, key)
            counts[key] = len(valid)
            # Cached short windows can contain disconnected runs collected
            # months apart. Their min/max bounds do not describe continuous
            # coverage, so never erase coarse observations between those runs.
            # Coarse-to-fine processing gives exact-time collisions precedence
            # while retaining every other actual source timestamp.
            samples.update(valid)
        for stamp, (value, source) in samples.items():
            row = merged.setdefault(stamp, {"time": stamp, "perp_equity": None,
                "account_equity": None, "perp_source": None, "account_source": None})
            row[field + "_equity"] = _s(value)
            row[field + "_source"] = source
    return [merged[t] for t in sorted(merged)], {"window_sample_counts": counts, "invalid_samples": invalid}


def _scope(portfolio_meta, hip3_count):
    meta = portfolio_meta or {}
    abstraction = str(meta.get("user_abstraction") or "unknown")
    supported = meta.get("scope_supported") is True
    if not supported and abstraction.lower() in ("disabled", "default") and "scope_supported" not in meta:
        supported = True
    if abstraction.lower() in ("unifiedaccount", "portfoliomargin", "dexabstraction", "unified", "portfolio"):
        supported = False
    if hip3_count:
        supported = False
    return supported, abstraction


def _positions(operations):
    """Find visible starting uncertainty and intervals containing known gaps."""
    states, previous, gaps = {}, {}, []
    for index, row in operations:
        coin = str(row.get("coin") or "UNKNOWN")
        before, after = _d(row.get("start_position")), _d(row.get("end_position"))
        if coin not in states:
            # A first nonzero start proves earlier holdings existed, but does
            # not prove their quantity throughout the preceding history.
            states[coin] = {"qty": ZERO if before == ZERO else None,
                "entry": ZERO if before == ZERO else None, "price": None,
                "price_time": None, "ambiguous": False, "last_index": None,
                "entry_source": "observed_flat" if before == ZERO else None,
                "entry_anchor_time": None}
        if coin in previous:
            prev_index, prev_after = previous[coin]
            if before is None or prev_after is None or before != prev_after:
                gaps.append((prev_index, index, coin))
        previous[coin] = (index, after)
    return states, gaps


def _entry_seeds(operations):
    """Recover missing opening costs from reported realized closing PnL.

    For linear USDC perps, gross closed PnL = signed closed quantity *
    (execution price - average entry).  Reverse weighted additions only along
    a continuous, unambiguous observed chain.  These retrospective values use
    potentially rounded PnL, so their provenance must remain visible.
    """
    blocked = {"duplicate_conflict", "unresolved_same_time_order", "invalid_fill"}
    chains, seeds = {}, {}

    def finish(chain):
        if not chain:
            return
        first_before = _d(chain[0][1].get("start_position"))
        if first_before is None or first_before == ZERO:
            return
        for offset, (_, row) in enumerate(chain):
            before, after = _d(row.get("start_position")), _d(row.get("end_position"))
            price, pnl = _d(row.get("price")), _d(row.get("closed_pnl"))
            if price is None or price <= ZERO or pnl is None or before == ZERO:
                continue
            same_side = after != ZERO and (after > ZERO) == (before > ZERO)
            closed = abs(before) - abs(after) if same_side else abs(before)
            if closed <= ZERO:
                continue
            entry = price - pnl / (closed if before > ZERO else -closed)
            if entry <= ZERO:
                continue
            valid = True
            for _, preceding in reversed(chain[:offset]):
                q0, q1 = _d(preceding.get("start_position")), _d(preceding.get("end_position"))
                px = _d(preceding.get("price"))
                if q0 == ZERO or q1 == ZERO or (q0 > ZERO) != (q1 > ZERO):
                    valid = False
                    break
                if abs(q1) > abs(q0):
                    if px is None or px <= ZERO:
                        valid = False
                        break
                    entry = (abs(q1) * entry - (abs(q1) - abs(q0)) * px) / abs(q0)
                if entry <= ZERO:
                    valid = False
                    break
            if valid:
                seeds[chain[0][0]] = {"entry": entry,
                    "time": _t(row.get("time")), "operation_id": row.get("id")}
                return

    for index, row in operations:
        coin = str(row.get("coin") or "UNKNOWN")
        before, after = _d(row.get("start_position")), _d(row.get("end_position"))
        chain = chains.setdefault(coin, [])
        bad = before is None or after is None or bool(blocked.intersection(_qualities(row)))
        if chain and (bad or _d(chain[-1][1].get("end_position")) != before or before == ZERO):
            finish(chain)
            chain.clear()
        if bad:
            continue
        chain.append((index, row))
        if after == ZERO or (before != ZERO and (after > ZERO) != (before > ZERO)):
            finish(chain)
            chain.clear()
    for chain in chains.values():
        finish(chain)
    return seeds


def _advance(state, row, entry_seed=None):
    before, after = _d(row.get("start_position")), _d(row.get("end_position"))
    price = _d(row.get("price"))
    quality = []
    old_qty, old_entry = state["qty"], state["entry"]
    entry_source = state.get("entry_source")
    entry_anchor_time = state.get("entry_anchor_time")
    if before is None or after is None:
        state.update(qty=None, entry=None, ambiguous=True, entry_source=None,
            entry_anchor_time=None)
        quality.append("unknown_position")
    else:
        discontinuous = old_qty is None or before != old_qty
        if discontinuous:
            old_entry = None
            entry_source = entry_anchor_time = None
        if old_entry is None and entry_seed is not None:
            old_entry = entry_seed["entry"]
            entry_source = "closed_pnl_reconstruction"
            entry_anchor_time = entry_seed["time"]
        elif discontinuous:
            quality.append("position_resynchronized_without_entry")
        if after == ZERO:
            entry = ZERO
            entry_source, entry_anchor_time = "observed_flat", None
        elif before == ZERO:
            entry = price if price is not None and price > ZERO else None
            entry_source, entry_anchor_time = "observed_fill" if entry is not None else None, None
        elif (before > ZERO) != (after > ZERO):
            # Normally already split by the accounting engine; still handle
            # an unsplit input conservatively and make the new side explicit.
            entry = price if price is not None and price > ZERO else None
            entry_source, entry_anchor_time = "observed_fill" if entry is not None else None, None
        elif abs(after) <= abs(before):
            entry = old_entry
        elif old_entry is not None and price is not None and price > ZERO:
            added = abs(after) - abs(before)
            entry = (abs(before) * old_entry + added * price) / abs(after)
        else:
            entry = None
        ambiguous = any(q in _qualities(row) for q in ("duplicate_conflict", "unresolved_same_time_order", "invalid_fill"))
        state.update(qty=after, entry=None if ambiguous and after != ZERO else entry,
            ambiguous=ambiguous and after != ZERO,
            entry_source=None if ambiguous or entry is None else entry_source,
            entry_anchor_time=None if ambiguous or entry is None else entry_anchor_time)
        if entry is None and after != ZERO:
            quality.append("unknown_entry_basis")
        if ambiguous:
            quality.append("ambiguous_position_record")
    if price is not None and price > ZERO:
        state["price"] = price
        state["price_time"] = _t(row.get("time"))
    elif state["qty"] != ZERO:
        state["price"] = None
        state["price_time"] = None
    return quality


def _totals(states, stamp, gap_coins=()):
    longs = shorts = trade_longs = trade_shorts = ZERO
    active = unknown = inferred = 0
    cost_ok = trade_ok = True
    age = 0
    flags = []
    for coin, state in states.items():
        qty = state["qty"]
        if qty is None:
            unknown += 1
            cost_ok = trade_ok = False
            continue
        if qty == ZERO:
            continue
        active += 1
        entry, price = state["entry"], state["price"]
        if entry is not None and state.get("entry_source") == "closed_pnl_reconstruction":
            inferred += 1
            flags.append("entry_basis_reconstructed_from_closed_pnl")
            if state.get("entry_anchor_time") is not None and stamp < state["entry_anchor_time"]:
                flags.append("entry_basis_uses_later_fill")
        if entry is None:
            cost_ok = False
        elif qty > ZERO:
            longs += abs(qty) * entry
        else:
            shorts += abs(qty) * entry
        if price is None or state["ambiguous"]:
            trade_ok = False
        elif qty > ZERO:
            trade_longs += abs(qty) * price
        else:
            trade_shorts += abs(qty) * price
        if state["price_time"] is not None:
            age = max(age, stamp - state["price_time"])
    if unknown:
        flags.append("unobserved_starting_positions")
    if gap_coins:
        cost_ok = trade_ok = False
        flags.append("position_gap_interval")
    if not cost_ok:
        flags.append("entry_notional_incomplete")
    if not trade_ok:
        flags.append("trade_notional_incomplete")
    return {"gross_entry_notional": _s(longs + shorts) if cost_ok else None,
        "long_entry_notional": _s(longs) if cost_ok else None,
        "short_entry_notional": _s(shorts) if cost_ok else None,
        "gross_trade_notional": _s(trade_longs + trade_shorts) if trade_ok else None,
        "long_trade_notional": _s(trade_longs) if trade_ok else None,
        "short_trade_notional": _s(trade_shorts) if trade_ok else None,
        "observed_entry_notional": _s(longs + shorts),
        "observed_trade_notional": _s(trade_longs + trade_shorts),
        "active_markets": active, "unknown_markets": unknown, "inferred_entry_markets": inferred,
        "position_complete": cost_ok, "trade_position_complete": trade_ok,
        "price_age_ms": age if trade_ok else None, "_quality": flags}


def _make_leverage(events, equity, start, end, reference_supported, max_age_ms,
                   denominator_scope="perp", scope_supported=False, positions_through=None):
    value_key, source_key = denominator_scope + "_equity", denominator_scope + "_source"
    samples = [r for r in equity if r[source_key] is not None]
    sample_times = [r["time"] for r in samples]
    event_times = [r["time"] for r in events]
    event_stamps = set(event_times)
    queue = [(r["time"], i, r) for i, r in enumerate(events)]
    for sample in samples:
        t = sample["time"]
        if start <= t <= end and t not in event_stamps:
            index = bisect_right(event_times, t) - 1
            queue.append((t, -1, events[index] if index >= 0 else None))
    queue.sort(key=lambda item: (item[0], item[1]))
    result = []
    for stamp, index, event in queue:
        sample_index = bisect_right(sample_times, stamp) - 1
        sample = samples[sample_index] if sample_index >= 0 else None
        value = _d(sample[value_key]) if sample else None
        age = stamp - sample["time"] if sample else None
        quality = []
        if not reference_supported:
            quality.append("unsupported_equity_scope")
        elif not scope_supported:
            quality.append("covered_exposure_reference_ratio_only")
        if sample is None or value is None:
            quality.append("missing_past_equity")
        elif value <= ZERO:
            quality.append("nonpositive_equity")
        if age is not None and age > max_age_ms:
            quality.append("stale_equity")
        elif age is not None and age > 900_000:
            quality.append("equity_sample_older_than_15m")
        if event is None:
            quality.append("missing_position_history")
        elif "position_gap_interval" in event["quality"]:
            quality.append("position_gap_interval")
        position_current = positions_through is not None and stamp <= positions_through
        if not position_current:
            quality.append("position_history_not_updated")
        entry = _d(event["gross_entry_notional"]) if event else None
        trade = _d(event["gross_trade_notional"]) if event else None
        if entry is None:
            quality.append("unknown_entry_notional")
        if trade is None:
            quality.append("unknown_trade_notional")
        valid = (reference_supported and value is not None and value > ZERO and age <= max_age_ms
            and event is not None and position_current)
        price_age = (event["price_age_ms"] + stamp - event["time"]
            if event is not None and event["price_age_ms"] is not None and event["active_markets"] else 0)
        if price_age > DAY_MS:
            quality.append("last_trade_price_older_than_24h")
        result.append({"time": stamp, "event_sequence": event["sequence"] if index >= 0 else None,
            "equity_time": sample["time"] if sample else None, "equity_age_ms": age,
            "denominator_scope": denominator_scope, "equity_value": _s(value),
            "perp_equity": _s(value) if denominator_scope == "perp" else None,
            "account_equity": _s(value) if denominator_scope == "account" else None,
            "equity_source": sample[source_key] if sample else None,
            "price_age_ms": price_age, "positions_observed_through_ms": positions_through,
            "gross_entry_notional": _s(entry), "gross_trade_notional": _s(trade),
            "leverage_entry": _s(entry / value) if valid and entry is not None else None,
            "leverage_trade": _s(trade / value) if valid and trade is not None else None,
            "quality": list(dict.fromkeys(quality))})
    return result


def build_curves(operations, portfolio=None, *, start=0, end=None, portfolio_meta=None):
    """Build four chart datasets from engine operations and API portfolio data.

    Supply all cached operations up to ``end``: operations before ``start`` warm
    up position quantities/cost basis, while realized PnL restarts at zero at
    ``start``. Same-millisecond order is preserved exactly from engine output.
    No assertion about unobserved markets or complete account history is made.
    """
    with localcontext() as context:
        context.prec = 50
        return _build_curves(list(operations or []), portfolio, start=start, end=end,
            portfolio_meta=portfolio_meta or {})


def _build_curves(operations, portfolio, *, start, end, portfolio_meta):
    start = _t(start) or 0
    end = _t(end) if end is not None else 2**63 - 1
    if end is None or end < start:
        raise ValueError("曲线截止时间必须不早于开始时间。")
    raw_equity, equity_info = normalize_portfolio(portfolio)
    position_scope = "native" if portfolio_meta.get("position_scope") == "native" else "usdc_perps"
    scope_label = "原生 USDC 永续 · 已观察市场" if position_scope == "native" else "已覆盖 USDC 永续（含 HIP-3）"
    eligible, excluded = [], Counter()
    invalid_time = 0
    hip3_count = included_hip3 = 0
    for i, row in enumerate(operations):
        if not isinstance(row, dict):
            excluded["invalid"] += 1
            continue
        stamp = _t(row.get("time"))
        if stamp is None:
            invalid_time += 1
            continue
        if stamp > end:
            continue
        if row.get("market_type") == "perp" and ":" in str(row.get("coin") or ""):
            hip3_count += 1
        if not _included(row, position_scope):
            key = "spot" if row.get("market_type") == "spot" else "hip3" if position_scope == "native" and ":" in str(row.get("coin") or "") else "other_currency_or_market"
            excluded[key] += 1
            continue
        if ":" in str(row.get("coin") or ""):
            included_hip3 += 1
        eligible.append((i, row))
    # Engine output is chronological. Stable ordering also safely accepts
    # externally supplied lists without changing equal-time reversal order.
    eligible.sort(key=lambda x: (_t(x[1].get("time")), x[0]))
    eligible = list(enumerate(row for _, row in eligible))
    states, gaps = _positions(eligible)
    entry_seeds = _entry_seeds(eligible)
    gap_starts, gap_ends = {}, {}
    for first, last, coin in gaps:
        gap_starts.setdefault(first, []).append(coin)
        gap_ends.setdefault(last, []).append(coin)
    active_gaps = Counter()
    events = []
    pnl = net_observed = ZERO
    pnl_complete = net_complete = True
    unknown_pnl = unknown_fee = 0
    warmup_count = 0
    boundary_emitted = False

    def emit_boundary():
        nonlocal boundary_emitted
        if boundary_emitted or not warmup_count or not start:
            return
        total = _totals(states, start, active_gaps)
        flags = total.pop("_quality")
        events.append({"sequence": 0, "time": start, "operation_id": None, "fill_id": None,
            "coin": "", "action": "boundary", "direction": "", "closed_pnl": None,
            "fee": None, "fee_token": None, "cumulative_closed_pnl": "0", "cumulative_net_pnl": "0",
            "observed_cumulative_closed_pnl": "0", "observed_cumulative_net_pnl": "0",
            "pnl_complete": True, "net_complete": True, "position_qty": None,
            "position_entry_price": None, "position_entry_source": None,
            "position_entry_anchor_time": None,
            "quality": list(dict.fromkeys(["window_boundary", *flags])), **total})
        boundary_emitted = True

    for index, row in eligible:
        stamp = _t(row["time"])
        if stamp >= start:
            emit_boundary()
        for coin in gap_ends.get(index, []):
            active_gaps[coin] -= 1
            if active_gaps[coin] <= 0:
                del active_gaps[coin]
        for coin in gap_starts.get(index, []):
            active_gaps[coin] += 1
        coin = str(row.get("coin") or "UNKNOWN")
        quality = [*_qualities(row), *_advance(states[coin], row, entry_seeds.get(index))]
        if stamp < start:
            warmup_count += 1
            continue
        value, fee = _d(row.get("closed_pnl")), _d(row.get("fee"))
        token = str(row.get("fee_token") or "").upper()
        fee_known = fee is not None and (fee == ZERO or token == "USDC")
        if value is None:
            unknown_pnl += 1
            pnl_complete = net_complete = False
            quality.append("unknown_pnl_contribution")
        else:
            pnl += value
            net_observed += value
        if fee_known:
            net_observed -= fee
        else:
            unknown_fee += 1
            net_complete = False
            quality.append("unknown_or_non_usdc_fee")
        total = _totals(states, stamp, active_gaps)
        quality.extend(total.pop("_quality"))
        events.append({"sequence": len(events) + (0 if boundary_emitted else 1), "time": stamp,
            "operation_id": row.get("id"), "fill_id": row.get("fill_id"), "coin": coin,
            "action": row.get("action"), "direction": row.get("direction"),
            "closed_pnl": _s(value), "fee": _s(fee), "fee_token": row.get("fee_token"),
            "cumulative_closed_pnl": _s(pnl) if pnl_complete else None,
            "cumulative_net_pnl": _s(net_observed) if net_complete else None,
            "observed_cumulative_closed_pnl": _s(pnl), "observed_cumulative_net_pnl": _s(net_observed),
            "pnl_complete": pnl_complete, "net_complete": net_complete,
            "position_qty": _s(states[coin]["qty"]), "position_entry_price": _s(states[coin]["entry"]),
            "position_entry_source": states[coin].get("entry_source"),
            "position_entry_anchor_time": states[coin].get("entry_anchor_time"),
            "quality": list(dict.fromkeys(quality)), **total})
    emit_boundary()
    scope_supported, abstraction = _scope(portfolio_meta, hip3_count)
    requested_scope = portfolio_meta.get("reference_equity_scope")
    denominator_scope = requested_scope if requested_scope in ("perp", "account") else "perp"
    reference_supported = scope_supported or (
        portfolio_meta.get("reference_ratio_supported") is True and requested_scope in ("perp", "account"))
    max_age = _t(portfolio_meta.get("max_equity_age_ms"))
    if max_age is None:
        max_age = DAY_MS
    positions_through = _t(portfolio_meta.get("positions_observed_through_ms"))
    if positions_through is None:
        # A bare imported fill list proves nothing about a later trade-free
        # interval. A successful API coverage watermark may extend that bound.
        positions_through = events[-1]["time"] if events else None
    leverage = _make_leverage(events, raw_equity, start, end, reference_supported, max_age,
        denominator_scope=denominator_scope, scope_supported=scope_supported,
        positions_through=positions_through)
    # Include one preceding real sample per series for as-of provenance. It is
    # metadata only; the displayed equity curve stays inside the chosen window.
    equity = [r for r in raw_equity if start <= r["time"] <= end]
    warnings = [
        ("曲线仅含原生 USDC 永续市场；现货、HIP-3 和其他计价币种不混入总额。"
         if position_scope == "native" else
         "曲线汇总已覆盖的 USDC 计价永续市场，包含明确标为 USDC 结算的 HIP-3；保留市场前缀，现货、未知和其他计价币种不混入总额。"),
        "仓位规模按已观察到的市场重建，无法证明从未出现在成交历史中的其他持仓不存在；缺失历史会限制完整性。",
        "成本仓位规模为剩余数量 × 加权开仓成本；最近成交价规模为剩余数量 × 各市场最后一次观察到的成交价，均非历史标记价格市值。",
        "账户资产采用官方 portfolio.accountValueHistory 采样，包含未实现盈亏；不是现金余额，也不代表钱包跨链资产总额。",
        f"参考倍数 = {scope_label}仓位参考规模 / 过去最近一条 {'综合账户' if denominator_scope == 'account' else 'perp 账户'}权益采样；仅向后匹配，不使用未来权益，不插值。权益超过 {max_age / 3_600_000:g} 小时或不为正时留空。",
    ]
    if not raw_equity:
        warnings.append("没有可用的官方账户资产历史；资产与参考杠杆曲线留空，可联网更新 portfolio 后再生成。")
    if not reference_supported:
        warnings.append("无法确认已覆盖永续仓位与账户权益的参考范围，参考倍数留空；需明确选择官方综合或永续账户权益序列后再计算。")
    elif not scope_supported:
        warnings.append(f"本账户展示的是{scope_label}持仓占所选官方账户权益的参考倍数；分子不包含未覆盖市场、其他结算币种、现货和 Vault 等风险头寸，因此不等于全账户真实杠杆。历史账户模式可能发生变化，旧时点的参考比例仍以所选官方序列为准。")
    if unknown_pnl:
        warnings.append(f"区间内 {unknown_pnl} 条操作缺少已实现盈亏；完整累计曲线在缺失后留空，已知部分累计不等于完整盈亏。")
    if unknown_fee:
        warnings.append(f"区间内 {unknown_fee} 条操作的手续费未知或非 USDC；扣费后完整累计值留空，未擅自按美元换算。")
    if gaps:
        warnings.append(f"发现 {len(gaps)} 处仓位数量不连续；缺口区间的仓位与杠杆留空，成本重建不跨越缺口。")
    if entry_seeds:
        warnings.append(f"{len(entry_seeds)} 段期初成本由同一连续持仓链的减仓成交价、数量及 closedPnl 反推；标为重建值，可能受源盈亏舍入影响。使用后续成交的点仅供事后复盘，不是当时已知的信号。")
    if equity_info["invalid_samples"]:
        warnings.append(f"{equity_info['invalid_samples']} 条资产采样无效；不当作零值。")
    if invalid_time:
        warnings.append(f"{invalid_time} 条操作时间无效，未绘入时间曲线。")
    price_age = max((r["price_age_ms"] or 0 for r in events), default=0)
    if price_age > DAY_MS:
        warnings.append("部分市场最近成交价格已超过 24 小时；价格可能陈旧，参考规模不是当时实际市值。")
    if any("position_history_not_updated" in row["quality"] for row in leverage):
        warnings.append("部分账户权益采样晚于已确认的成交抓取截止时间，对应参考倍数留空；请更新成交历史，不能把旧仓位直接当作当前仓位。")
    return {"version": "2.3.0", "basis": {
        "scope": "native_usdc_perpetuals" if position_scope == "native" else "covered_usdc_perpetuals",
        "position_scope": position_scope, "scope_label": scope_label,
        "pnl": "逐操作 closedPnl 累计；不含浮盈、资金费；扣费版 fee 只扣一次，builderFee 已含在 fee 中。",
        "entry_notional": "Σ |剩余仓位| × 加权开仓成本；多空绝对值相加。",
        "entry_reconstruction": "缺失期初成本可由连续链减仓 closedPnl 反推；以质量标记区分，未知数量不回填，成本不跨仓位缺口。",
        "trade_notional": "Σ |剩余仓位| × 该市场最近观察成交价；价格可能陈旧。",
        "equity": "官方 portfolio.accountValueHistory 原始采样；保留 perp 与综合两套来源。",
        "leverage": f"参考仓位 / 过去最近{'综合账户' if denominator_scope == 'account' else '永续账户'}权益，非精确历史实际杠杆。",
        "max_equity_age_ms": max_age, "scope_supported": scope_supported,
        "reference_equity_scope": denominator_scope, "reference_ratio_supported": reference_supported,
        "positions_observed_through_ms": positions_through,
        "user_abstraction": abstraction, "start_ms": start, "end_ms": end,
        "timezone": "Asia/Shanghai", "fee_includes_builder_fee": True,
        "unrealized_pnl_reconstructed": False, "historical_prices_requested": False},
        "event_curve": events, "equity_curve": equity, "leverage_curve": leverage,
        "warnings": warnings, "summary": {
            "operation_points": sum(r["action"] != "boundary" for r in events),
            "event_points": len(events), "warmup_operations": warmup_count,
            "equity_points": len(equity), "leverage_points": len(leverage),
            "available_entry_leverage_points": sum(r["leverage_entry"] is not None for r in leverage),
            "available_trade_leverage_points": sum(r["leverage_trade"] is not None for r in leverage),
            "excluded_operations": dict(excluded), "hip3_operations": hip3_count,
            "included_hip3_operations": included_hip3,
            "unknown_pnl_operations": unknown_pnl, "unknown_fee_operations": unknown_fee,
            "position_gaps": len(gaps), "invalid_time_operations": invalid_time,
            "recovered_entry_segments": len(entry_seeds),
            "recovered_entry_points": sum(bool(r.get("inferred_entry_markets")) for r in events),
            "entry_uses_later_fill_points": sum("entry_basis_uses_later_fill" in r["quality"] for r in events),
            "cumulative_closed_pnl": _s(pnl) if pnl_complete and events else None,
            "cumulative_net_pnl": _s(net_observed) if net_complete and events else None,
            "observed_cumulative_closed_pnl": _s(pnl),
            "observed_cumulative_net_pnl": _s(net_observed),
            "first_event_ms": events[0]["time"] if events else None,
            "last_event_ms": events[-1]["time"] if events else None,
            "first_equity_ms": equity[0]["time"] if equity else None,
            "last_equity_ms": equity[-1]["time"] if equity else None,
            **equity_info}}
