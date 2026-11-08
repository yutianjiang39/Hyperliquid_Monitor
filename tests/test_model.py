from decimal import Decimal

from engine import ordered_fills, position_decimal
from model.mtm import daily_perp_mtm
from model.positions import build_paths, same_position

DAY = 86_400_000
T0 = 1_760_000_000_000 // DAY * DAY


def fill(coin, t, side, px, sz, start, tid):
    return {"coin": coin, "time": t, "side": side, "px": str(px), "sz": str(sz), "startPosition": str(start),
            "closedPnl": "0", "fee": "0", "feeToken": "USDC", "tid": tid, "oid": tid, "hash": "0x%064x" % tid}


def test_position_decimal_removes_float_noise():
    assert position_decimal("38272466.1000000015") == Decimal("38272466.1")
    assert position_decimal("-0.30000000000000004") == Decimal("-0.3")
    assert position_decimal("0") == 0


def test_same_position_tolerance():
    assert same_position(Decimal("38272466.1"), Decimal("38272466.1000000015"))
    assert not same_position(Decimal("1"), Decimal("1.001"))


def test_noisy_chain_has_no_breaks():
    fills = [fill("BTC", T0, "B", 100, "0.1", "0", 1),
             fill("BTC", T0 + 1, "B", 100, "0.2", "0.1000000000000000055", 2),
             fill("BTC", T0 + 2, "A", 100, "0.3", "0.30000000000000004", 3)]
    paths = build_paths(ordered_fills(fills))
    assert paths["BTC"].breaks == []


def test_gap_inserts_break_at_midpoint():
    fills = [fill("ETH", T0, "B", 10, 1, 0, 1), fill("ETH", T0 + 1000, "A", 10, 1, 3, 2)]
    path = build_paths(ordered_fills(fills))["ETH"]
    assert len(path.breaks) == 1
    gap = path.breaks[0]
    assert gap["kind"] == "fill_gap" and gap["mid"] == T0 + 500 and Decimal(gap["delta"]) == 2
    assert path.at(T0 + 600) == 3


def test_snapshot_mismatch_recorded():
    fills = [fill("SOL", T0, "B", 10, 1, 0, 1)]
    snapshot = {"time": T0 + DAY, "dexes": {"": {"positions": [{"coin": "SOL", "szi": "2", "mark_px": "12"}]}}}
    path = build_paths(ordered_fills(fills), snapshot)["SOL"]
    assert [b["kind"] for b in path.breaks] == ["snapshot_mismatch"]


def test_mtm_identity_round_trip():
    # Buy 2 @100 on day 0, sell 2 @130 on day 2: price PnL = 60, minus fees, plus funding.
    fills = [fill("BTC", T0 + 3600_000, "B", 100, 2, 0, 1), fill("BTC", T0 + 2 * DAY + 3600_000, "A", 130, 2, 2, 2)]
    fills[0]["fee"], fills[1]["fee"] = "1", "1.5"
    funding = [{"time": T0 + DAY, "delta": {"coin": "BTC", "usdc": "-0.5"}}]
    snapshot = {"time": T0 + 3 * DAY + 1000, "dexes": {"": {"positions": []}}}
    ordered = ordered_fills(fills)
    paths = build_paths(ordered, snapshot)
    closes = {T0 - DAY: "100", T0: "110", T0 + DAY: "120", T0 + 2 * DAY: "130"}
    out = daily_perp_mtm(ordered, funding, paths, snapshot, {"BTC": {"market_type": "perp", "pnl_token": "USDC"}},
                         {}, lambda coin, a, b: closes, T0, snapshot["time"])
    total = sum(d["pnl"] for d in out["days"])
    assert total == Decimal("60") - Decimal("2.5") - Decimal("0.5")
    assert sum(d["price_pnl"] for d in out["days"]) == 60
    # Day 0 holds 2 from 100 to the 110 close.
    assert out["days"][0]["price_pnl"] == 20
