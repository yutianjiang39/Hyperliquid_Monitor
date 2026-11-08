"""Mark-to-market PnL reconstruction for perpetuals.

For one coin over [a, b):
    pnl = pos_b * mark_b - pos_a * mark_a - sum(signed_size * px) - fees + funding
This identity needs no entry price, so positions opened before the visible
history are valued correctly at the boundary mark. Position breaks (fills the
API no longer returns) are booked as imputed trades of the missing size at the
gap midpoint, priced at that day's close (else the next fill price), so a
vanished position is not read as a total loss; their notional is reported as
``imputed_notional``. Summing per UTC day gives a
daily reconstructed perp PnL series that is reconciled against official PnL.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from model.positions import is_spot, signed_size
from sources.market_data import DAY_MS, market_for, token_price

ZERO = Decimal(0)


def _d(value):
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except Exception:
        return None


def day_floor(t):
    return (int(t) // DAY_MS) * DAY_MS


def daily_perp_mtm(ordered_fills, funding, paths, snapshot, meta, spot_prices, candle_fn, start, now):
    """Return {days:[...], by_coin:{...}, flags:{...}}.

    candle_fn(coin, start_ms, end_ms) -> {bucket_ms: close} with 1d buckets.
    Day d covers [d, d+1day); its closing mark is the close of bucket d.
    The last (current) day closes at ``now`` with the snapshot / meta mark.
    """
    first_day = day_floor(start)
    days = list(range(first_day, day_floor(now) + DAY_MS, DAY_MS))
    by_day = {d: defaultdict(lambda: ZERO) for d in days}
    by_coin = defaultdict(lambda: ZERO)
    flags = {"mark_fallback": [], "fx_converted": [], "no_mark": [], "meta_error": [], "gap_imputed": []}
    snapshot_marks = {p["coin"]: _d(p["mark_px"]) for d in (snapshot or {}).get("dexes", {}).values()
                      for p in d["positions"]}
    fills_by_coin = defaultdict(list)
    for fill in ordered_fills:
        coin = str(fill.get("coin") or "")
        if not is_spot(coin) and int(fill["time"]) >= first_day:
            fills_by_coin[coin].append(fill)
    funding_by_coin = defaultdict(list)
    for event in funding or []:
        delta = event.get("delta") or {}
        if int(event["time"]) >= first_day:
            funding_by_coin[str(delta.get("coin") or "")].append((int(event["time"]), _d(delta.get("usdc")) or ZERO))
    coins = set(fills_by_coin) | {c for c, p in paths.items() if p.at(first_day) or p.current}
    for coin in sorted(coins):
        path = paths.get(coin)
        market = market_for(coin, meta)
        if market.get("meta_error"):
            flags["meta_error"].append(coin)
        token = market.get("pnl_token") or "USDC"
        fx = token_price(token, spot_prices) or Decimal(1)
        if token != "USDC" and fx != 1:
            flags["fx_converted"].append(coin)
        closes = {k: _d(v) for k, v in (candle_fn(coin, first_day - DAY_MS, now) or {}).items()}
        coin_fills = _with_imputed_gaps(fills_by_coin.get(coin, []), path, first_day, closes,
                                        snapshot_marks.get(coin) or _d(market.get("mark_px")))
        if any(f.get("_imputed") for f in coin_fills):
            flags["gap_imputed"].append(coin)
        last_px = None

        def mark_end_of(day):
            nonlocal last_px
            if day == days[-1]:
                live = snapshot_marks.get(coin) or _d(market.get("mark_px"))
                if live is not None:
                    return live
            value = closes.get(day)
            if value is None:
                earlier = [k for k in closes if k <= day]
                value = closes[max(earlier)] if earlier else None
            if value is None and last_px is not None:
                flags["mark_fallback"].append(coin)
                value = last_px
            return value

        index = 0
        prev_mark = mark_end_of(first_day - DAY_MS)
        if prev_mark is None:
            # Before any candle: fall back to the first fill price if needed.
            prev_mark = _d(coin_fills[0]["px"]) if coin_fills else None
        prev_pos = path.at(first_day) if path else ZERO
        fund_events = funding_by_coin.get(coin, [])
        fund_index = 0
        for day in days:
            end = day + DAY_MS
            cash = fees = realized = volume = imputed = ZERO
            while index < len(coin_fills) and int(coin_fills[index]["time"]) < end:
                fill = coin_fills[index]
                px = _d(fill["px"]) or ZERO
                size = signed_size(fill)
                cash += size * px
                if fill.get("_imputed"):
                    imputed += abs(size) * px
                    index += 1
                    continue
                volume += abs(size) * px
                fee = _d(fill.get("fee")) or ZERO
                fee_token = fill.get("feeToken") or token
                fees += fee if fee_token in (token, "USDC") else fee * (token_price(fee_token, spot_prices) or ZERO)
                realized += _d(fill.get("closedPnl")) or ZERO
                last_px = px
                index += 1
            fund = ZERO
            while fund_index < len(fund_events) and fund_events[fund_index][0] < end:
                fund += fund_events[fund_index][1]
                fund_index += 1
            pos = path.at(min(end, now + 1)) if path else ZERO
            mark = mark_end_of(day)
            if mark is None:
                mark = last_px if last_px is not None else (prev_mark if prev_mark is not None else ZERO)
                if pos:
                    flags["no_mark"].append(coin)
            if prev_mark is None:
                prev_mark = mark
                if prev_pos:
                    flags["no_mark"].append(coin)
            price_pnl = pos * mark - prev_pos * prev_mark - cash
            row = by_day[day]
            row["price_pnl"] += price_pnl * fx
            row["fees"] += fees * fx
            row["funding"] += fund * fx
            row["realized"] += realized * fx
            row["gross_exposure"] += abs(pos * mark) * fx
            row["long_exposure"] += max(pos * mark, ZERO) * fx
            row["short_exposure"] += max(-pos * mark, ZERO) * fx
            row["volume"] += volume * fx
            row["imputed_notional"] += imputed * fx
            by_coin[coin] += (price_pnl - fees + fund) * fx
            prev_pos, prev_mark = pos, mark
    out_days = []
    for day in days:
        row = by_day[day]
        pnl = row["price_pnl"] - row["fees"] + row["funding"]
        out_days.append({"time": day, "pnl": pnl, **{k: row[k] for k in (
            "price_pnl", "fees", "funding", "realized", "gross_exposure", "long_exposure",
            "short_exposure", "volume", "imputed_notional")}})
    for key in flags:
        flags[key] = sorted(set(flags[key]))
    return {"start": first_day, "end": now, "days": out_days,
            "by_coin": {k: v for k, v in sorted(by_coin.items(), key=lambda kv: kv[1])}, "flags": flags}


def _with_imputed_gaps(coin_fills, path, first_day, closes, live_mark):
    """Merge one synthetic trade per position break into the fill stream."""
    if path is None or not path.breaks:
        return coin_fills
    synthetic = []
    for item in path.breaks:
        if not _d(item.get("delta")):
            continue
        if item["kind"] == "fill_gap":
            # One synthetic trade per step; snapshot-corrected gaps have several.
            for step in item.get("steps") or [{"time": item["mid"], "delta": item["delta"]}]:
                if step["time"] >= first_day and _d(step["delta"]):
                    px = closes.get(day_floor(step["time"])) or _d(item["px"])
                    synthetic.append(_synthetic(dict(item, delta=step["delta"]), step["time"], px))
        elif item["kind"] == "snapshot_mismatch" and live_mark is not None:
            synthetic.append(_synthetic(item, int(item["time"]), live_mark))
    return sorted(coin_fills + synthetic, key=lambda fill: int(fill["time"])) if synthetic else coin_fills


def _synthetic(item, time, px):
    delta = _d(item["delta"])
    return {"coin": item["coin"], "time": time, "px": str(px), "sz": str(abs(delta)),
            "side": "B" if delta > 0 else "A", "fee": "0", "closedPnl": "0", "_imputed": True}


def lifetime_realized_check(ordered_fills, funding, snapshot):
    """sum(closedPnl) - fees + funding + current uPnL, for complete histories."""
    perp = [f for f in ordered_fills if not is_spot(str(f.get("coin") or ""))]
    closed = sum((_d(f.get("closedPnl")) or ZERO for f in perp), ZERO)
    fees = sum((_d(f.get("fee")) or ZERO for f in perp), ZERO)
    fund = sum((_d((e.get("delta") or {}).get("usdc")) or ZERO for e in funding or []), ZERO)
    upnl = sum((_d(p["unrealized_pnl"]) or ZERO for d in (snapshot or {}).get("dexes", {}).values()
                for p in d["positions"]), ZERO)
    return {"closed_pnl": closed, "fees": fees, "funding": fund, "unrealized_now": upnl,
            "total": closed - fees + fund + upnl}
