from decimal import Decimal

import pytest

from metrics.performance import twr_index, xirr
from metrics.series import daily_series
from model.positions import PositionPath
from reconcile import _thin, detect_truncation, reconcile_pnl, status_for

DAY = 86_400_000
T0 = 1_760_000_000_000 // DAY * DAY


def test_status_thresholds():
    assert status_for(Decimal(99), Decimal(1000)) == "ok"          # under $100 floor
    assert status_for(Decimal(900), Decimal(100000)) == "ok"       # under 1%
    assert status_for(Decimal(4000), Decimal(100000)) == "warn"    # under 5%
    assert status_for(Decimal(6000), Decimal(100000)) == "fail"


def test_thin_keeps_endpoints_and_min_gap():
    points = [{"time": T0 + i * DAY // 4} for i in range(60)]
    kept = _thin(points, 7 * DAY)
    assert kept[0] is points[0] and kept[-1] is points[-1]
    gaps = [b["time"] - a["time"] for a, b in zip(kept, kept[1:])]
    assert all(g >= 7 * DAY for g in gaps)


def official(cums, equities=None):
    return [{"time": T0 + i * DAY, "cum_pnl": Decimal(str(c)),
             "equity": None if equities is None else Decimal(str(equities[i]))} for i, c in enumerate(cums)]


def test_reconcile_exact_match_is_ok():
    days = [{"time": T0 + i * DAY, "pnl": Decimal(10), "price_pnl": Decimal(10), "fees": Decimal(0),
             "funding": Decimal(0), "realized": Decimal(0), "volume": Decimal(0), "imputed_notional": Decimal(0)}
            for i in range(20)]
    off = official([10 * i for i in range(21)])
    result = reconcile_pnl(off, {"days": days, "start": T0, "end": T0 + 20 * DAY})
    assert result["status"] == "ok" and abs(result["residual"]) < 1e-6
    assert result["interval_status"]["fail"] == 0


def test_truncation_flags_late_first_fill():
    path = PositionPath("BTC")
    path.add({"time": T0 + 30 * DAY, "startPosition": "5", "sz": "1", "side": "B", "px": "1"})
    fills = [{"coin": "BTC", "time": T0 + 30 * DAY, "px": "1", "sz": "1"}]
    off = official([0, 1])
    result = detect_truncation(fills, {"BTC": path}, off, {})
    assert result["head_truncated"] and result["status"] == "api_visible_truncated"
    assert {e["code"] for e in result["evidence"]} >= {"account_older_than_fills", "seeded_position"}


def test_truncation_ignores_seed_when_fills_predate_account():
    path = PositionPath("BTC")
    path.add({"time": T0 - DAY, "startPosition": "5", "sz": "1", "side": "B", "px": "1"})
    fills = [{"coin": "BTC", "time": T0 - DAY, "px": "1", "sz": "1"}]
    result = detect_truncation(fills, {"BTC": path}, official([0, 1]), {})
    assert not result["head_truncated"]


def test_daily_series_totals_match_official():
    # Samples every 3 days; a deposit of 500 between day 3 and day 6.
    cums = [0, 30, 45, 90]
    equity = [1000, 1030, 1545, 1590]
    off = [{"time": T0 + 3 * i * DAY, "cum_pnl": Decimal(c), "equity": Decimal(e)} for i, (c, e) in enumerate(zip(cums, equity))]
    rec = [{"time": T0 + i * DAY, "pnl": Decimal(5 + i)} for i in range(10)]
    out = daily_series(off, rec, min_base=1)
    days = out["days"]
    assert sum(d["pnl"] for d in days) == pytest.approx(90)
    assert sum(d["flow"] for d in days) == pytest.approx(500)
    assert days[-1]["equity"] == pytest.approx(1590)


def test_twr_compounds_returns():
    idx = twr_index([{"time": 1, "ret": 0.1}, {"time": 2, "ret": None}, {"time": 3, "ret": -0.5}])
    assert idx[-1]["index"] == pytest.approx(0.55)


def test_xirr_simple_year():
    # Invest 100, receive 110 a year later -> 10%.
    assert xirr([(0, -100), (int(365 * DAY), 110)]) == pytest.approx(0.10, abs=1e-4)
    assert xirr([(0, 100), (DAY, 5)]) is None
