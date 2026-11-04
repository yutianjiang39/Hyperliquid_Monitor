"""Align operation, cash-ledger and observed-equity events without interpolation.

All chart rows share one event sequence, including same-millisecond fills.
Positions are carried only inside the observed fill-history interval.  The
cash-based ratios deliberately have no portfolio-sample age cutoff: their
denominator comes from explicitly labelled ledger reconstruction, not from
pretending a stale equity sample is current.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, localcontext


PNL_FIELDS = ("cumulative_closed_pnl", "cumulative_net_pnl",
              "observed_cumulative_closed_pnl", "observed_cumulative_net_pnl",
              "pnl_complete", "net_complete")
POSITION_FIELDS = ("gross_entry_notional", "long_entry_notional", "short_entry_notional",
                   "gross_trade_notional", "long_trade_notional", "short_trade_notional",
                   "observed_entry_notional", "observed_trade_notional", "active_markets",
                   "unknown_markets", "position_complete", "trade_position_complete",
                   "price_age_ms", "inferred_entry_markets")
POSITION_FLAGS = {"unobserved_starting_positions", "position_gap_interval",
                  "entry_notional_incomplete", "trade_notional_incomplete",
                  "entry_basis_reconstructed_from_closed_pnl", "entry_basis_uses_later_fill",
                  "unknown_entry_basis", "ambiguous_position_record"}


def _d(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        value = Decimal(str(value))
        return value if value.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _s(value):
    if value is None:
        return None
    if value == 0:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def attach_event_timeline(curves, balance, operations=None):
    """Enrich ``curves`` in place, preserving all legacy chart arrays.

    A balance row is an after-event state, except for the explicitly labelled
    start boundary.  A funding/transfer row never inherits a previous fill's
    identity, direction or realized PnL.  Calculated values stay Decimal strings.
    """
    with localcontext() as context:
        context.prec = 60
        return _attach(curves, balance, operations or [])


def _attach(curves, balance, operations):
    events = curves.get("event_curve", [])
    event_by_operation = {r["operation_id"]: r for r in events if r.get("operation_id")}
    boundaries = {r["time"]: r for r in events if r.get("action") == "boundary"}
    operation_by_id = {r["id"]: r for r in operations if r.get("id")}
    through = curves.get("basis", {}).get("positions_observed_through_ms")
    state = None
    timeline, leverage = [], []
    default_pnl = {key: "0" for key in PNL_FIELDS[:4]}
    default_pnl.update(pnl_complete=True, net_complete=True)
    for index, cash in enumerate(balance.get("balance_curve", []), 1):
        stamp = int(cash["time"])
        operation_id = cash.get("operation_id")
        event = event_by_operation.get(operation_id)
        if event is not None:
            state = event
        elif cash.get("event_type") in ("boundary", "window_boundary"):
            state = boundaries.get(stamp, state)
        row = dict(cash)
        row.update(sequence=index, timeline_sequence=index)
        row.update({key: state.get(key) if state else default_pnl.get(key)
                    for key in PNL_FIELDS})
        row.update({key: state.get(key) if state else None for key in POSITION_FIELDS})
        flags = list(row.get("quality") or [])
        if state:
            flags.extend(flag for flag in state.get("quality", []) if flag in POSITION_FLAGS)
        position_current = state is not None and through is not None and stamp <= through
        row["position_state_time_ms"] = state["time"] if state else None
        row["positions_observed_through_ms"] = through
        if not position_current:
            flags.append("missing_position_history" if state is None else "position_history_not_updated")
            for key in POSITION_FIELDS:
                row[key] = False if key.endswith("complete") else None
        elif row.get("price_age_ms") is not None:
            row["price_age_ms"] += max(0, stamp - state["time"])

        operation = operation_by_id.get(operation_id)
        if operation is not None:
            for key in ("action", "action_label", "direction", "direction_label", "coin",
                        "fill_id", "closed_pnl", "fee", "fee_token", "price", "size",
                        "start_position", "end_position", "market_type"):
                row[key] = operation.get(key)
            row["position_qty"] = operation.get("end_position")
        else:
            row.update(action=row.get("event_type") or "sample", direction="", fill_id=None,
                       closed_pnl=None, fee=None, fee_token=None, position_qty=None)
        for key in ("position_entry_price", "position_entry_source", "position_entry_anchor_time"):
            row[key] = event.get(key) if event else None

        strict, observed = _d(row.get("balance_usdc")), _d(row.get("observed_balance_usdc"))
        entry, trade = _d(row.get("gross_entry_notional")), _d(row.get("gross_trade_notional"))
        if observed is not None and observed <= 0:
            flags.append("nonpositive_cash_balance")
        if observed is None:
            flags.append("missing_cash_balance")
        for prefix, denominator in (("", strict), ("observed_", observed)):
            available = position_current and denominator is not None and denominator > 0
            row[prefix + "leverage_entry"] = _s(entry / denominator) if available and entry is not None else None
            row[prefix + "leverage_trade"] = _s(trade / denominator) if available and trade is not None else None
        row["denominator_scope"] = "hypercore_usdc_cash"
        row["quality"] = list(dict.fromkeys(flags))
        cash["timeline_sequence"] = index
        timeline.append(row)
        leverage.append({key: row.get(key) for key in (
            "time", "sequence", "timeline_sequence", "event_sequence", "operation_id", "event_type",
            "balance", "observed_balance", "balance_usdc", "observed_balance_usdc",
            "balance_complete", "history_coverage_verified", "anchor_time", "anchor_source",
            "leverage_entry", "leverage_trade", "observed_leverage_entry", "observed_leverage_trade",
            "gross_entry_notional", "gross_trade_notional", "position_state_time_ms",
            "positions_observed_through_ms", "denominator_scope", "quality")})

    # A true snapshot belongs after all known events with the same timestamp.
    last_by_time = {row["time"]: row["timeline_sequence"] for row in timeline}
    operation_sequence = {row["operation_id"]: row["timeline_sequence"]
                          for row in timeline if row.get("operation_id")}
    boundary_sequence = {row["time"]: row["timeline_sequence"] for row in timeline
                         if row.get("event_type") in ("boundary", "window_boundary")}
    for row in events:
        row["timeline_sequence"] = (boundary_sequence.get(row["time"]) if row.get("action") == "boundary"
                                    else operation_sequence.get(row.get("operation_id")))
    for row in curves.get("equity_curve", []):
        row["timeline_sequence"] = last_by_time.get(row["time"])

    curves.update(timeline_curve=timeline, balance_curve=balance.get("balance_curve", []),
                  balance_leverage_curve=leverage, balance_anchors=balance.get("anchors", []),
                  balance_diagnostics=balance.get("diagnostics", []),
                  cash_flow_events=balance.get("flow_events", []))
    curves.setdefault("basis", {}).update(
        balance_scope="hypercore_usdc_cash",
        balance="同地址 USDC 账面资金（现货+USDC永续），剔除永续浮盈亏；不含非USDC资产、Vault及借贷投资价值。",
        balance_leverage="已覆盖USDC永续仓位参考金额 / 同时刻USDC账面余额；已知流水和非原子余额快照可能有误差。",
        event_timeline="成交、资金费、转账、余额基准及官方净值点按同一时间/事件序号排列；没有行情插值。",
        default_equity_basis="balance_known", default_leverage_basis="balance_known")
    curves.setdefault("summary", {}).update(balance.get("summary", {}))
    curves["summary"].update(
        timeline_points=len(timeline),
        available_balance_entry_leverage_points=sum(r["leverage_entry"] is not None for r in leverage),
        available_balance_trade_leverage_points=sum(r["leverage_trade"] is not None for r in leverage),
        observed_balance_entry_leverage_points=sum(r["observed_leverage_entry"] is not None for r in leverage),
        observed_balance_trade_leverage_points=sum(r["observed_leverage_trade"] is not None for r in leverage),
        dense_position_points=sum(r.get("gross_entry_notional") is not None for r in timeline))
    curves["warnings"] = list(dict.fromkeys([*curves.get("warnings", []), *balance.get("warnings", [])]))
    return curves
