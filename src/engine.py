"""Exact, conservative Hyperliquid fill-to-position accounting (stdlib only).

Public interface: analyze_fills(fills, funding=None, asset_meta=None).
Amounts are decimal strings or None. None means unavailable, never zero.
asset_meta may map a coin to {market_type: 'perp'|'spot', pnl_token: 'USDC'};
spot_coins and pnl_tokens mappings are also accepted. Native perps settle in
USDC. Builder DEX markets (a colon in coin) need an explicit pnl_token mapping.

A campaign is a contiguous signed position from flat to flat. Reversals are
split into close/open legs, including proportional fee allocation. ``complete``
is about lifecycle evidence; ``eligible_for_win_rate`` additionally requires
known realized PNL and comparable fees. Funding stays separate. Builder fees
are reported separately and NEVER deducted a second time from ``fee``.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
from itertools import groupby
import json

ZERO = Decimal(0)
ACTION_LABELS = {
    "open": "开仓", "add": "加仓", "reduce": "减仓", "close": "清仓",
    "buy": "现货买入", "sell": "现货卖出", "unknown": "待核对",
}
DIRECTION_LABELS = {"long": "做多", "short": "做空", "spot": "现货", "unknown": "未知"}
QUALITY_LABELS = {
    "missing_start": "未观察到开仓，属于历史片段",
    "position_discontinuity": "前后仓位不连续，可能缺少成交或发生非成交仓位变动",
    "missing_start_position": "缺少成交前仓位",
    "missing_pnl": "缺少官方已实现盈亏",
    "missing_fee": "缺少手续费",
    "missing_fee_token": "缺少手续费币种",
    "unknown_pnl_token": "未知盈亏计价币种",
    "mixed_fee_tokens": "手续费币种不同，未做汇率换算",
    "invalid_fill": "缺少有效价格、数量、方向或时间",
    "duplicate_conflict": "相同成交标识的数据不一致，已保留字段较完整的一份",
    "unresolved_same_time_order": "同毫秒成交顺序存在歧义",
    "legacy_trade_missing_pnl_fee": "旧版逐笔成交归档不提供已实现盈亏和手续费",
    "import_missing_start_position": "导入记录缺少成交前仓位",
    "liquidation": "强平成交",
    "adl": "自动减仓 (ADL)",
}


def _decimal(value):
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def position_decimal(value):
    """startPosition with float serialization noise removed.

    The API emits values such as 38272466.1000000015 (a double printed with
    excess digits). Rounding to 15 significant digits restores the exact
    position so fill chains and same-millisecond ordering link correctly.
    """
    number = _decimal(value)
    if number is None or number == ZERO:
        return number
    rounded = number.quantize(Decimal(1).scaleb(number.adjusted() - 14))
    return rounded.normalize() if rounded == rounded.to_integral_value() else rounded


def _number(value):
    if value is None:
        return None
    if value == ZERO:
        return "0"
    result = format(value, "f")
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    return result


def _timestamp(value):
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
        return result if result >= 0 and str(value).strip() not in ("",) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _token(value):
    if value is None or value == "":
        return None
    return str(value).strip().upper() or None


def _json_hash(value):
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:24]


def _metadata(coin, asset_meta):
    asset_meta = asset_meta or {}
    data = asset_meta.get(coin, {}) if isinstance(asset_meta, dict) else {}
    if isinstance(data, str):
        data = {"market_type": data}
    if not isinstance(data, dict):
        data = {}
    return data


def _market_type(fill, asset_meta):
    coin = str(fill.get("coin") or "UNKNOWN")
    data = _metadata(coin, asset_meta)
    explicit = fill.get("_market_type") or fill.get("market_type") or data.get("market_type")
    if explicit in ("spot", "perp"):
        return explicit
    if isinstance(asset_meta, dict) and coin in asset_meta.get("spot_coins", []):
        return "spot"
    direction = str(fill.get("dir") or "").lower()
    if direction in ("buy", "sell") or coin.startswith(("@", "#")) or "/" in coin:
        return "spot"
    return "perp"


def _pnl_token(coin, asset_meta, fill=None):
    data = _metadata(coin, asset_meta)
    value = (fill or {}).get("pnl_token") or data.get("pnl_token") or data.get("collateral_token")
    if not value and isinstance(asset_meta, dict):
        value = (asset_meta.get("pnl_tokens") or {}).get(coin)
    if value:
        return _token(value)
    return "USDC" if ":" not in coin else None


def _fill_key(fill):
    coin = str(fill.get("coin") or "UNKNOWN")
    side = str(fill.get("side") or "").upper()
    tid = fill.get("tid")
    if tid is not None and tid != "":
        # One match can legitimately create both buyer and seller fills for a
        # wallet. Keep the sides separate, and never deduplicate across markets.
        return "tid:" + str(coin) + ":" + str(tid) + ":" + side
    fields = ("coin", "time", "hash", "oid", "side", "px", "sz", "startPosition", "dir")
    return "row:" + _json_hash({key: fill.get(key) for key in fields})


def _sort_tid(value):
    try:
        return (0, int(value))
    except (ValueError, TypeError):
        return (1, str(value or ""))


def _fill_sort_key(fill):
    time = _timestamp(fill.get("time"))
    return (time if time is not None else 2**63 - 1, _sort_tid(fill.get("tid")),
            str(fill.get("coin") or ""), str(fill.get("side") or ""), _fill_key(fill))


def _source_order(fill):
    block = _timestamp(fill.get("_block_number"))
    index = _timestamp(fill.get("_event_index"))
    return (block, index) if block is not None and index is not None else None


def _same_value(key, first, second):
    if key in ("px", "sz", "startPosition", "closedPnl", "fee", "time"):
        left, right = _decimal(first), _decimal(second)
        return left is not None and right is not None and left == right
    if key == "feeToken":
        return _token(first) == _token(second)
    return str(first) == str(second)


def _order_same_millisecond(fills):
    """Prefer position continuity over opaque trade IDs inside one timestamp.

    A tid is a deterministic tie breaker, not proof of execution order. A
    Authoritative archive block/event coordinates always retain their order.
    A unique startPosition chain restores missing source coordinates where
    the records allow it.
    Ambiguous branching remains visible, rather than inventing a reliable
    completed campaign from an arbitrary order.
    """
    def group_key(row):
        timestamp = _timestamp(row.get("time"))
        return (timestamp if timestamp is not None else 2**63 - 1, str(row.get("coin") or ""))

    ordered = sorted(fills, key=lambda row: (group_key(row), _fill_sort_key(row)))
    result = []
    expected_by_coin = {}
    for (_, coin), iterator in groupby(ordered, key=group_key):
        remaining = list(iterator)
        expected = expected_by_coin.get(coin)
        while remaining:
            source_positions = [_source_order(row) for row in remaining if _source_order(row) is not None]
            first_source = min(source_positions) if source_positions else None
            # Later archive events cannot jump ahead merely because their
            # startPosition happens to match after a missing intermediate fill.
            available = [row for row in remaining if _source_order(row) is None or _source_order(row) == first_source]
            choices = [row for row in available if expected is not None and position_decimal(row.get("startPosition")) == expected]
            if not choices:
                if expected is None:
                    choices = [row for row in available if position_decimal(row.get("startPosition")) == ZERO]
                if not choices:
                    ends = set()
                    for row in remaining:
                        start, size = position_decimal(row.get("startPosition")), _decimal(row.get("sz"))
                        side = str(row.get("side") or "").upper()
                        if start is not None and size is not None and side in ("B", "A"):
                            ends.add(start + (size if side == "B" else -size))
                    choices = [row for row in available if position_decimal(row.get("startPosition")) not in ends]
            choices = choices or available
            row = min(choices, key=_fill_sort_key)
            same_start = [other for other in choices if position_decimal(other.get("startPosition")) == position_decimal(row.get("startPosition"))]
            if len(same_start) > 1:
                # Closed independent cycles can share zero; an intermediate
                # nonzero branch cannot be unambiguously attached to a cycle.
                if position_decimal(row.get("startPosition")) not in (None, ZERO):
                    for other in same_start:
                        other["_same_time_ambiguous"] = True
            result.append(row)
            remaining.remove(row)
            start, size = position_decimal(row.get("startPosition")), _decimal(row.get("sz"))
            side = str(row.get("side") or "").upper()
            expected = start + (size if side == "B" else -size) if start is not None and size is not None and side in ("A", "B") else None
        expected_by_coin[coin] = expected
    final_order = []
    for _, iterator in groupby(result, key=lambda row: _timestamp(row.get("time"))):
        same_time = list(iterator)
        if all(_source_order(row) is not None for row in same_time):
            # Restore cross-market block order, too. Market reconstruction is
            # independent, so this leaves each market's accounting unchanged.
            # Python's stable sort retains the continuity order when two sides
            # of the same legacy event have identical source coordinates.
            same_time.sort(key=_source_order)
        final_order.extend(same_time)
    return final_order


def ordered_fills(fills):
    """Deduplicated fills in reconstruction order (same-ms continuity aware)."""
    return _deduplicate(fills)[0]


def _deduplicate(fills):
    unique = {}
    input_count = 0
    conflicts = 0
    important = ("px", "sz", "startPosition", "closedPnl", "fee", "feeToken", "time", "side", "oid")
    for original in fills:
        input_count += 1
        if not isinstance(original, dict):
            original = {"_invalid_raw": str(original)}
        fill = dict(original)
        key = _fill_key(fill)
        if key not in unique:
            unique[key] = fill
            continue
        old = unique[key]
        conflicting = [k for k in important if old.get(k) not in (None, "")
                       and fill.get(k) not in (None, "") and not _same_value(k, old[k], fill[k])]
        # A copy from the API can enrich old archive records with missing fields.
        old_score = sum(old.get(k) not in (None, "") for k in important)
        new_score = sum(fill.get(k) not in (None, "") for k in important)
        primary, supplement = (fill, old) if new_score > old_score else (old, fill)
        merged = dict(primary)
        for k, value in supplement.items():
            if merged.get(k) in (None, "") and value not in (None, ""):
                merged[k] = value
        sources = set()
        for row in (old, fill):
            for source in (row.get("_source"), row.get("source")):
                if isinstance(source, str) and source:
                    sources.add(source)
            extra_sources = row.get("_sources") or []
            if isinstance(extra_sources, str):
                extra_sources = [extra_sources]
            sources.update(str(x) for x in extra_sources if x)
        if sources:
            merged["_sources"] = sorted(sources)
        if conflicting or old.get("_duplicate_conflict") or fill.get("_duplicate_conflict"):
            merged["_duplicate_conflict"] = True
            conflicts += bool(conflicting)
        unique[key] = merged
    return _order_same_millisecond(unique.values()), input_count, conflicts


def _base_operation(fill, asset_meta, wallet=None):
    coin = str(fill.get("coin") or "UNKNOWN")
    market_type = _market_type(fill, asset_meta)
    px, size = _decimal(fill.get("px")), _decimal(fill.get("sz"))
    pnl, fee = _decimal(fill.get("closedPnl")), _decimal(fill.get("fee"))
    fee_token = _token(fill.get("feeToken"))
    source_quality = fill.get("_quality") or []
    quality = [source_quality] if isinstance(source_quality, str) else [str(item) for item in source_quality]
    if fill.get("_duplicate_conflict"):
        quality.append("duplicate_conflict")
    if fill.get("_same_time_ambiguous"):
        quality.append("unresolved_same_time_order")
    if market_type == "perp" and pnl is None:
        quality.append("missing_pnl")
    if fee is None:
        quality.append("missing_fee")
    elif fee != 0 and not fee_token:
        quality.append("missing_fee_token")
    pnl_token = _pnl_token(coin, asset_meta, fill) if market_type == "perp" else None
    if market_type == "perp" and pnl_token is None:
        quality.append("unknown_pnl_token")
    liquidation = fill.get("liquidation") if isinstance(fill.get("liquidation"), dict) else None
    counterparty = False
    if liquidation and wallet and str(liquidation.get("liquidatedUser") or wallet).lower() != wallet:
        liquidation, counterparty = None, True  # counterparty to another account's liquidation
    adl = "auto-deleveraging" in str(fill.get("dir") or "").lower()
    if liquidation:
        quality.append("liquidation")
    if adl:
        quality.append("adl")
    source = fill.get("_sources") or fill.get("_source") or fill.get("source") or "unknown"
    if isinstance(source, list):
        source = ", ".join(str(x) for x in source)
    return {
        "id": _fill_key(fill), "fill_id": _fill_key(fill),
        "time": _timestamp(fill.get("time")), "coin": coin,
        "display_name": _metadata(coin, asset_meta).get("display_name") or coin,
        "block_number": _timestamp(fill.get("_block_number")),
        "event_index": _timestamp(fill.get("_event_index")),
        "market_type": market_type, "action": "unknown", "action_label": ACTION_LABELS["unknown"],
        "direction": "spot" if market_type == "spot" else "unknown",
        "direction_label": DIRECTION_LABELS["spot" if market_type == "spot" else "unknown"],
        "price": _number(px), "size": _number(size),
        "notional": _number(px * size) if px is not None and size is not None else None,
        "start_position": _number(position_decimal(fill.get("startPosition"))), "end_position": None,
        "closed_pnl": _number(pnl), "pnl_token": pnl_token,
        "fee": _number(fee), "fee_token": fee_token,
        "builder_fee": _number(_decimal(fill.get("builderFee"))),
        "oid": str(fill["oid"]) if fill.get("oid") is not None else None,
        "tid": str(fill["tid"]) if fill.get("tid") is not None else None,
        "hash": fill.get("hash"), "source": str(source), "quality": quality,
        "dir_raw": fill.get("dir"), "side": fill.get("side"),
        "liquidation": bool(liquidation), "liquidation_mark_px": (liquidation or {}).get("markPx"),
        "liquidation_method": (liquidation or {}).get("method"), "adl": adl,
        "liquidation_counterparty": counterparty,
        "maker": None if fill.get("crossed") is None else not fill.get("crossed"),
        "twap": fill.get("twapId") is not None,
    }


def _set_action(op, action, direction):
    op["action"] = action
    op["action_label"] = ACTION_LABELS[action]
    op["direction"] = direction
    op["direction_label"] = DIRECTION_LABELS[direction]


def _direction(position):
    return "long" if position > ZERO else "short"


def _new_campaign(coin, direction, op, starting_position, observed_open, sequence):
    quality = set()
    if not observed_open:
        quality.add("missing_start")
    return {
        "id": "position:" + coin + ":" + str(sequence), "coin": coin,
        "display_name": op.get("display_name", coin),
        "direction": direction, "direction_label": DIRECTION_LABELS[direction],
        "status": "open", "open_time": op["time"] if observed_open else None,
        "close_time": None, "first_observed_time": op["time"], "last_observed_time": op["time"],
        "pnl_token": op.get("pnl_token"), "_position": starting_position,
        "_max_size": abs(starting_position), "_entry_notional": ZERO,
        "_exit_notional": ZERO, "_pnl": ZERO, "_fees": defaultdict(lambda: ZERO),
        "_builder_fees": defaultdict(lambda: ZERO), "_pnl_missing": False,
        "_fee_missing": False, "_entry_missing": not observed_open,
        "_exit_missing": False, "_fill_ids": set(), "_order_ids": set(),
        "_quality": quality, "_observed_open": observed_open, "_continuous": True,
        "_liquidated": False, "_adl": False, "_maker_notional": ZERO, "_taker_notional": ZERO,
    }


def _consume_campaign(campaign, operation):
    action = operation["action"]
    notional = _decimal(operation["notional"])
    if action in ("open", "add"):
        if notional is None:
            campaign["_entry_missing"] = True
        else:
            campaign["_entry_notional"] += notional
    elif action in ("reduce", "close"):
        if notional is None:
            campaign["_exit_missing"] = True
        else:
            campaign["_exit_notional"] += notional
    pnl = _decimal(operation["closed_pnl"])
    if pnl is None:
        campaign["_pnl_missing"] = True
    else:
        campaign["_pnl"] += pnl
    fee, fee_token = _decimal(operation["fee"]), operation.get("fee_token")
    if fee is None:
        campaign["_fee_missing"] = True
    elif fee_token:
        campaign["_fees"][fee_token] += fee
    elif fee != ZERO:
        campaign["_fees"]["UNKNOWN"] += fee
        campaign["_fee_missing"] = True
    builder_fee = _decimal(operation["builder_fee"])
    if builder_fee is not None:
        campaign["_builder_fees"][fee_token or "UNKNOWN"] += builder_fee
    campaign["_liquidated"] |= bool(operation.get("liquidation"))
    campaign["_adl"] |= bool(operation.get("adl"))
    if notional is not None and operation.get("maker") is not None:
        campaign["_maker_notional" if operation["maker"] else "_taker_notional"] += notional
    campaign["_fill_ids"].add(operation["fill_id"])
    if operation["oid"] is not None:
        campaign["_order_ids"].add(operation["oid"])
    campaign["_quality"].update(operation["quality"])
    if any(code in operation["quality"] for code in ("duplicate_conflict", "unresolved_same_time_order")):
        campaign["_continuous"] = False
    campaign["_position"] = _decimal(operation["end_position"])
    campaign["_max_size"] = max(campaign["_max_size"],
                                abs(_decimal(operation["start_position"]) or ZERO),
                                abs(campaign["_position"] or ZERO))
    campaign["last_observed_time"] = operation["time"]
    operation["campaign_id"] = campaign["id"]
    if action == "close":
        campaign["status"] = "closed"
        campaign["close_time"] = operation["time"]


def _finalize_campaign(campaign):
    pnl_known = not campaign["_pnl_missing"]
    fees_known = not campaign["_fee_missing"]
    token = campaign["pnl_token"]
    same_currency = token is not None and all(
        fee_token == token or value == ZERO for fee_token, value in campaign["_fees"].items())
    if not same_currency and campaign["_fees"]:
        campaign["_quality"].add("mixed_fee_tokens")
    complete = (campaign["status"] == "closed" and campaign["_observed_open"]
                and campaign["_continuous"])
    net = campaign["_pnl"] - campaign["_fees"].get(token, ZERO) if pnl_known and fees_known and same_currency else None
    result = {key: value for key, value in campaign.items() if not key.startswith("_")}
    result.update({
        "max_size": _number(campaign["_max_size"]),
        "entry_notional": None if campaign["_entry_missing"] else _number(campaign["_entry_notional"]),
        "exit_notional": None if campaign["_exit_missing"] else _number(campaign["_exit_notional"]),
        "observed_entry_notional": _number(campaign["_entry_notional"]),
        "observed_exit_notional": _number(campaign["_exit_notional"]),
        "closed_pnl": _number(campaign["_pnl"]) if pnl_known else None,
        "observed_closed_pnl": _number(campaign["_pnl"]),
        "fees_by_token": {k: _number(v) for k, v in sorted(campaign["_fees"].items())},
        "builder_fees_by_token": {k: _number(v) for k, v in sorted(campaign["_builder_fees"].items())},
        "net_pnl": _number(net), "net_pnl_excludes_funding": True,
        "fill_count": len(campaign["_fill_ids"]), "order_count": len(campaign["_order_ids"]),
        "complete": complete, "history_complete": campaign["_observed_open"] and campaign["_continuous"],
        "pnl_complete": pnl_known, "fees_complete": fees_known,
        "eligible_for_win_rate": complete and net is not None,
        "quality": sorted(campaign["_quality"]), "end_position": _number(campaign["_position"]),
        "liquidated": campaign["_liquidated"], "adl": campaign["_adl"],
        "ended_by": ("liquidation" if campaign["_liquidated"] else "adl" if campaign["_adl"] else "trade")
                    if campaign["status"] == "closed" else None,
        "maker_notional": _number(campaign["_maker_notional"]),
        "taker_notional": _number(campaign["_taker_notional"]),
    })
    if result["open_time"] is not None and result["close_time"] is not None:
        result["duration_ms"] = result["close_time"] - result["open_time"]
    else:
        result["duration_ms"] = None
    return result


def _funding_summary(funding, asset_meta):
    totals = defaultdict(lambda: ZERO)
    seen = set()
    count = missing = 0
    for event in funding or []:
        if not isinstance(event, dict):
            missing += 1
            continue
        key = _json_hash({k: v for k, v in event.items() if k not in ("source", "_source", "_sources")})
        if key in seen:
            continue
        seen.add(key)
        count += 1
        delta = event.get("delta", event)
        if not isinstance(delta, dict):
            missing += 1
            continue
        amount = _decimal(delta.get("usdc", delta.get("amount")))
        coin = str(delta.get("coin") or "")
        token = _token(delta.get("token") or delta.get("pnl_token")) or _pnl_token(coin, asset_meta)
        if amount is None:
            missing += 1
        else:
            totals[token or "UNKNOWN"] += amount
    return {"funding_record_count": count, "funding_missing_amount_count": missing,
            "funding_by_token": {k: _number(v) for k, v in sorted(totals.items())},
            "funding_included_in_campaign_pnl": False}


def _attach_funding(campaigns, funding):
    """Credit each funding payment to the campaign holding that coin at that time.

    Open time inclusive, close time exclusive. Payments outside every observed
    campaign are returned per token instead of being spread around.
    """
    by_coin = defaultdict(list)
    for campaign in campaigns:
        campaign["_funding"] = ZERO
        start = campaign.get("open_time") or campaign.get("first_observed_time")
        by_coin[campaign["coin"]].append((start, campaign.get("close_time"), campaign))
    starts = {}
    for coin, spans in by_coin.items():
        spans.sort(key=lambda x: x[0] or 0)
        starts[coin] = [x[0] or 0 for x in spans]
    unattributed = defaultdict(lambda: ZERO)
    for event in funding or []:
        delta = event.get("delta", event) if isinstance(event, dict) else {}
        amount = _decimal(delta.get("usdc"))
        if amount is None:
            continue
        coin, t = str(delta.get("coin") or ""), _timestamp(event.get("time"))
        target = None
        if t is not None and coin in by_coin:
            index = bisect_right(starts[coin], t) - 1
            if index >= 0:
                start, end, campaign = by_coin[coin][index]
                if start is not None and (end is None or t < end):
                    target = campaign
        if target is None:
            unattributed[str(event.get("pnl_token") or "USDC")] += amount
        else:
            target["_funding"] += amount
    for campaign in campaigns:
        value = campaign.pop("_funding")
        campaign["funding"] = _number(value)
        net = _decimal(campaign.get("net_pnl"))
        campaign["net_pnl_with_funding"] = _number(net + value) if net is not None else None
    return unattributed


def analyze_fills(fills, funding=None, asset_meta=None, wallet=None):
    """Return operations, campaigns, summary and warnings as JSON-safe objects.

    ``win_rate`` is a percentage, not a fraction. Its denominator includes
    complete closed campaigns with known net PNL, including break-even ones.
    API ``closedPnl`` excludes trading fees; this engine subtracts ``fee`` once
    only when its currency equals the campaign PNL currency. Funding is separate.
    With ``wallet`` given, a fill is a liquidation only when that wallet was the
    liquidated user (not the counterparty absorbing someone else's liquidation).
    """
    with localcontext() as ctx:
        ctx.prec = 80
        return _analyze(fills, funding, asset_meta, (wallet or "").lower() or None)


def _analyze(fills, funding, asset_meta, wallet=None):
    ordered, input_count, duplicate_conflicts = _deduplicate(fills)
    operations = []
    finalized = []
    active = {}
    sequences = Counter()
    quality_counts = Counter()
    summary_fees = defaultdict(lambda: ZERO)
    summary_builder_fees = defaultdict(lambda: ZERO)
    summary_pnl = defaultdict(lambda: ZERO)
    summary_turnover = defaultdict(lambda: ZERO)
    summary_pnl_missing = Counter()
    invalid_count = perp_count = spot_count = missing_fee_count = missing_pnl_count = quote_unknown_count = 0

    def finish(coin, interrupted=False):
        campaign = active.pop(coin, None)
        if campaign is None:
            return
        if interrupted:
            campaign["status"] = "interrupted"
            campaign["_continuous"] = False
            campaign["_quality"].add("position_discontinuity")
        finalized.append(_finalize_campaign(campaign))

    for fill in ordered:
        operation = _base_operation(fill, asset_meta, wallet)
        coin = operation["coin"]
        side = str(fill.get("side") or "").upper()
        if side in ("BUY", "BID"):
            side = "B"
        elif side in ("SELL", "ASK", "S"):
            side = "A"
        size, px = _decimal(fill.get("sz")), _decimal(fill.get("px"))
        start = position_decimal(fill.get("startPosition"))
        pnl, fee = _decimal(fill.get("closedPnl")), _decimal(fill.get("fee"))
        fee_token = operation["fee_token"]
        pnl_token = operation["pnl_token"]
        is_perp = operation["market_type"] == "perp"
        perp_count += is_perp
        spot_count += not is_perp
        if fee is None:
            missing_fee_count += 1
        else:
            summary_fees[fee_token or "UNKNOWN"] += fee
        builder_fee = _decimal(fill.get("builderFee"))
        if builder_fee is not None:
            summary_builder_fees[fee_token or "UNKNOWN"] += builder_fee
        if is_perp:
            if pnl is None:
                missing_pnl_count += 1
                summary_pnl_missing[pnl_token or "UNKNOWN"] += 1
            else:
                summary_pnl[pnl_token or "UNKNOWN"] += pnl
            if pnl_token is None:
                quote_unknown_count += 1
            if size is not None and px is not None:
                summary_turnover[pnl_token or "UNKNOWN"] += size * px
        valid = size is not None and size > ZERO and px is not None and px > ZERO and side in ("A", "B") and operation["time"] is not None
        if not valid:
            operation["quality"].append("invalid_fill")
            invalid_count += 1
            if is_perp:
                finish(coin, interrupted=True)
            operations.append(operation)
            quality_counts.update(set(operation["quality"]))
            continue
        signed_size = size if side == "B" else -size
        end = start + signed_size if start is not None else None
        operation["end_position"] = _number(end)
        if not is_perp:
            _set_action(operation, "buy" if side == "B" else "sell", "spot")
            operations.append(operation)
            quality_counts.update(set(operation["quality"]))
            continue
        if start is None:
            operation["quality"].append("missing_start_position")
            finish(coin, interrupted=True)
            operations.append(operation)
            quality_counts.update(set(operation["quality"]))
            continue
        if coin in active and active[coin]["_position"] != start:
            finish(coin, interrupted=True)
            operation["quality"].append("position_discontinuity")
        elif coin not in active and start != ZERO:
            operation["quality"].append("missing_start")
        # Exact Decimal arithmetic makes zero and sign transitions explicit.
        reversal = start != ZERO and end != ZERO and (start > ZERO) != (end > ZERO)
        if reversal:
            close_size, open_size = abs(start), abs(end)
            close_op, open_op = dict(operation), dict(operation)
            close_op["quality"], open_op["quality"] = list(operation["quality"]), list(operation["quality"])
            close_op.update(id=operation["id"] + ":close", size=_number(close_size),
                            notional=_number(px * close_size), end_position="0", reversal=True)
            open_op.update(id=operation["id"] + ":open", size=_number(open_size),
                           notional=_number(px * open_size), start_position="0",
                           closed_pnl="0", reversal=True)
            # closedPnl belongs entirely to the closing leg. A newly opened leg
            # realizes no PNL, even if the original archive lacks closing PNL.
            open_op["quality"] = [q for q in open_op["quality"] if q not in
                                  ("missing_pnl", "missing_start", "position_discontinuity")]
            if fee is not None:
                close_fee = fee * close_size / size
                close_op["fee"] = _number(close_fee)
                open_op["fee"] = _number(fee - close_fee)
            if builder_fee is not None:
                close_builder_fee = builder_fee * close_size / size
                close_op["builder_fee"] = _number(close_builder_fee)
                open_op["builder_fee"] = _number(builder_fee - close_builder_fee)
            _set_action(close_op, "close", _direction(start))
            _set_action(open_op, "open", _direction(end))
            legs = [close_op, open_op]
        else:
            if start == ZERO:
                action, direction = "open", _direction(end)
            elif end == ZERO:
                action, direction = "close", _direction(start)
            elif abs(end) > abs(start):
                action, direction = "add", _direction(start)
            else:
                action, direction = "reduce", _direction(start)
            _set_action(operation, action, direction)
            operation["reversal"] = False
            legs = [operation]
        for op in legs:
            if coin not in active:
                sequences[coin] += 1
                observed_open = op["action"] == "open" and _decimal(op["start_position"]) == ZERO
                active[coin] = _new_campaign(coin, op["direction"], op,
                    _decimal(op["start_position"]), observed_open, sequences[coin])
            _consume_campaign(active[coin], op)
            operations.append(op)
            quality_counts.update(set(op["quality"]))
            if op["action"] == "close":
                finish(coin)
    for coin in list(active):
        finish(coin)
    unattributed_funding = _attach_funding(finalized, funding)
    # Most recent campaigns first, while operations remain chronological so a
    # report/export can choose either order without changing reconstruction.
    finalized.sort(key=lambda x: (x["last_observed_time"] or 0, x["coin"], x["id"]), reverse=True)
    eligible = [x for x in finalized if x["eligible_for_win_rate"]]
    wins = sum(_decimal(x["net_pnl"]) > ZERO for x in eligible)
    losses = sum(_decimal(x["net_pnl"]) < ZERO for x in eligible)
    flats = len(eligible) - wins - losses
    by_status = Counter(x["status"] for x in finalized)
    known_tokens = set(summary_pnl) | set(summary_pnl_missing)
    complete_pnl_by_token = {
        token: None if summary_pnl_missing[token] else _number(summary_pnl[token])
        for token in sorted(known_tokens)
    }
    times = [x["time"] for x in operations if x["time"] is not None]
    summary = {
        "input_fill_count": input_count, "unique_fill_count": len(ordered),
        "duplicate_fill_count": input_count - len(ordered), "duplicate_conflict_count": duplicate_conflicts,
        "operation_count": len(operations), "perp_fill_count": perp_count, "spot_fill_count": spot_count,
        "invalid_fill_count": invalid_count, "campaign_count": len(finalized),
        "closed_campaign_count": by_status["closed"], "open_campaign_count": by_status["open"],
        "interrupted_campaign_count": by_status["interrupted"],
        "complete_closed_campaign_count": sum(x["complete"] for x in finalized),
        "incomplete_campaign_count": sum(not x["complete"] for x in finalized),
        "evaluated_campaign_count": len(eligible), "winning_campaign_count": wins,
        "losing_campaign_count": losses, "breakeven_campaign_count": flats,
        "win_rate": _number(Decimal(wins) * 100 / Decimal(len(eligible))) if eligible else None,
        "win_rate_strict": _number(Decimal(wins) * 100 / Decimal(wins + losses)) if wins + losses else None,
        "unattributed_funding_by_token": {k: _number(v) for k, v in sorted(unattributed_funding.items())},
        "win_rate_basis": "complete_closed_campaigns_with_known_net_pnl_including_breakeven",
        "realized_pnl_by_token": complete_pnl_by_token,
        "observed_realized_pnl_by_token": {k: _number(v) for k, v in sorted(summary_pnl.items())},
        "realized_pnl_usdc": complete_pnl_by_token.get("USDC"),
        "observed_realized_pnl_usdc": _number(summary_pnl["USDC"]) if "USDC" in known_tokens else None,
        "closed_pnl_total": complete_pnl_by_token.get("USDC") if known_tokens <= {"USDC"} else None,
        "fees_by_token": {k: _number(v) for k, v in sorted(summary_fees.items())},
        "builder_fees_by_token": {k: _number(v) for k, v in sorted(summary_builder_fees.items())},
        "builder_fee_deducted_separately": False,
        "turnover_by_token": {k: _number(v) for k, v in sorted(summary_turnover.items())},
        "missing_pnl_fill_count": missing_pnl_count, "missing_fee_fill_count": missing_fee_count,
        "unknown_pnl_token_fill_count": quote_unknown_count,
        "first_fill_time": min(times) if times else None, "last_fill_time": max(times) if times else None,
        "quality_counts": dict(sorted(quality_counts.items())),
    }
    summary.update(_funding_summary(funding, asset_meta))
    warnings = []
    if input_count != len(ordered):
        warnings.append(f"已去重 {input_count - len(ordered)} 条重叠成交；反向成交和跨市场成交分别保留。")
    if duplicate_conflicts:
        warnings.append(f"发现 {duplicate_conflicts} 次成交字段冲突；同一标识优先保留信息较完整的数据，需核对来源。")
    if summary["incomplete_campaign_count"]:
        warnings.append(f"{summary['incomplete_campaign_count']} 个仓位周期未完整闭合或历史有缺口，未纳入完整战绩胜率。")
    if missing_pnl_count:
        warnings.append(f"{missing_pnl_count} 条合约成交缺少官方 closedPnl；相关盈亏显示未知，未用买卖金额差冒充。")
    if missing_fee_count:
        warnings.append(f"{missing_fee_count} 条成交缺少手续费；相关净盈亏和胜率保留为空。")
    if quote_unknown_count:
        warnings.append(f"{quote_unknown_count} 条成交的盈亏计价币种未知，未与 USDC 合计。")
    if invalid_count:
        warnings.append(f"{invalid_count} 条成交缺少必要字段，保留原始活动但不推断仓位变化。")
    warnings.append("战绩净盈亏仅扣同币种交易手续费；builderFee 单列，资金费率单列。胜率分母包含可评估的保本交易。")
    return {"operations": operations, "campaigns": finalized, "summary": summary, "warnings": warnings}
