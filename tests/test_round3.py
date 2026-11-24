"""v3.3 regression tests: one per reproduced counterexample (171-180) plus the
confirmed P1 fixes and the new metrics. Each test states the old wrong value."""
from decimal import Decimal

import pytest

DAY = 86_400_000
HOUR = 3_600_000
T0 = 1_760_000_000_000 // DAY * DAY


def _day(i, ret, pnl, equity, flow=0.0, cum=None):
    return {"time": T0 + (i + 1) * DAY, "start": T0 + i * DAY, "ret": ret, "pnl": pnl, "flow": flow,
            "equity": equity, "cum_pnl": pnl if cum is None else cum, "source": "official"}


# ------------------------------------------------------------ 171-173 returns

def test_171_drawdown_includes_window_start():
    from metrics.performance import risk
    days = [_day(0, -0.10, -1000.0, 9000.0), _day(1, 0.01, 90.0, 9090.0, cum=-910.0)]
    r = risk(days)
    assert r["max_drawdown"] == pytest.approx(0.10)        # was 0.0
    assert r["max_drawdown_usd"] == pytest.approx(1000.0)  # was 0.0


def test_172_twr_is_none_without_valid_returns():
    from metrics.performance import performance, risk
    days = [_day(0, None, 5.0, 105.0), _day(1, None, -2.0, 103.0)]
    assert performance(days)["twr"] is None                 # was 0.0
    assert performance(days)["return_days"] == 0
    assert risk(days)["max_drawdown"] is None


def test_173_month_without_valid_days_is_none():
    from metrics.performance import performance
    days = [_day(0, None, 5.0, 105.0)]
    month = performance(days)["monthly"][0]
    assert month["ret"] is None and month["valid_days"] == 0   # was ret 0.0


# ------------------------------------------------------------ 174-178 fills

def test_174_funding_split_per_payment():
    from metrics.behavior import costs
    funding = [{"time": T0, "delta": {"coin": "BTC", "usdc": "-5"}},
               {"time": T0 + HOUR, "delta": {"coin": "BTC", "usdc": "8"}}]
    c = costs([], funding)
    assert (c["funding_received"], c["funding_paid"], c["funding_net"]) == (8, 5, 3)   # was 3 / 0 / 3


def test_175_fee_total_and_chart_share_one_usd_basis():
    from metrics.behavior import costs
    ops = [{"time": T0, "fee": "1", "fee_token": "USDC", "notional": "100"},
           {"time": T0 + DAY, "fee": "2", "fee_token": "HYPE", "notional": "100"}]
    priced = costs(ops, [], prices={"HYPE": "10"})
    assert priced["fees"] == pytest.approx(21) and priced["cumulative"][-1]["fees"] == pytest.approx(21)
    unpriced = costs(ops, [], prices={})
    # Old: card 1, chart 3 (raw HYPE amount added to USD).
    assert unpriced["fees"] == pytest.approx(1) and unpriced["cumulative"][-1]["fees"] == pytest.approx(1)
    assert unpriced["fee_missing_count"] == 1


def _reversal_fills():
    base = {"coin": "BTC", "feeToken": "USDC", "crossed": True}
    return [dict(base, time=T0, px="100", sz="1", side="B", startPosition="0", closedPnl="0", fee="0.1",
                 tid=1, oid=1, hash="0x1", dir="Open Long"),
            dict(base, time=T0 + 1000, px="110", sz="2", side="A", startPosition="1", closedPnl="10", fee="0.2",
                 tid=2, oid=2, hash="0x2", dir="Long > Short")]


def test_176_177_fills_and_reversals_count_unique_fill_ids():
    from engine import analyze_fills
    from metrics.behavior import activity
    ops = analyze_fills(_reversal_fills())["operations"]
    assert len(ops) == 3                                    # engine legs stay split
    a = activity(ops)
    assert a["fills"] == 2 and a["legs"] == 3                # fills was 3
    assert a["reversals"] == 1                               # was 2
    assert a["volume"] == pytest.approx(100 + 220)
    assert sum(c["fills"] for c in a["heatmap"]) == 2


def test_178_active_day_ratio_uses_calendar_days():
    from metrics.behavior import activity
    midnight = T0 + DAY
    ops = [{"time": midnight - 1, "coin": "BTC", "notional": "1", "fill_id": "a"},
           {"time": midnight + 1, "coin": "BTC", "notional": "1", "fill_id": "b"}]
    a = activity(ops)
    assert a["span_days"] == 2 and a["active_day_ratio"] == 1.0   # was ~2.0


# ------------------------------------------------------------ 179-180 series / reconcile

def test_179_cutoff_truncates_official_inputs():
    from metrics.series import daily_series
    off = [{"time": T0 + i * DAY, "cum_pnl": Decimal(100 * i), "equity": Decimal(1000 + 100 * i)} for i in range(3)]
    out = daily_series(off, now=T0 + DAY, min_base=1)
    assert [round(d["pnl"]) for d in out["days"]] == [100]       # was [200]
    mid = daily_series(off, now=T0 + DAY + DAY // 2, min_base=1)
    assert sum(d["pnl"] for d in mid["days"]) == pytest.approx(150)


def test_180_partial_last_day_is_not_rescaled_twice():
    from reconcile import reconcile_pnl
    now = T0 + DAY // 2
    row = {"time": T0, "pnl": Decimal(50), "price_pnl": Decimal(50), "fees": Decimal(0), "funding": Decimal(0),
           "realized": Decimal(0), "volume": Decimal(0), "imputed_notional": Decimal(0)}
    off = [{"time": T0, "cum_pnl": Decimal(0)}, {"time": now, "cum_pnl": Decimal(50)}]
    r = reconcile_pnl(off, {"days": [row], "start": T0, "end": now})
    assert r["reconstructed"] == pytest.approx(50) and r["residual"] == pytest.approx(0)   # was 25 / 25


# ------------------------------------------------------------ 181-182

def test_181_fee_drag_uses_gross_winning_trade_profit():
    from metrics.behavior import costs
    ops = [{"time": T0, "fee": "10", "fee_token": "USDC", "notional": "1000"}]
    c = costs(ops, [], gross_trade_profit=100.0)
    assert c["fee_drag"] == pytest.approx(0.10)
    assert costs(ops, [], gross_trade_profit=0)["fee_drag"] is None


class _Resp:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


class _DuneSession:
    def __init__(self, rows):
        self.rows, self.posted = rows, []

    def post(self, url, json=None, **kw):
        self.posted.append((url, json))
        return _Resp({"execution_id": "E1", "state": "QUERY_STATE_PENDING"})

    def get(self, url, params=None, **kw):
        if url.endswith("/status"):
            return _Resp({"state": "QUERY_STATE_COMPLETED"})
        assert "/execution/E1/results" in url
        return _Resp({"result": {"rows": self.rows}})


class _Store:
    wallet = "0x" + "a" * 40

    def __init__(self):
        self.saved = []

    def put(self, kind, row, source):
        self.saved.append(row)
        return 1

    def commit(self):
        pass


def test_182_dune_runs_parameterised_execution_and_rejects_other_wallets(monkeypatch):
    from sources.plugins.dune import Dune
    monkeypatch.setenv("DUNE_API_KEY", "x")
    fill = {"time": T0, "coin": "BTC", "side": "B", "px": "1", "sz": "1", "startPosition": "0",
            "closedPnl": "0", "fee": "0", "feeToken": "USDC", "tid": 1, "oid": 1, "hash": "0x1", "dir": "Open Long"}
    rows = [dict(fill, user=_Store.wallet), dict(fill, tid=2, user="0x" + "b" * 40)]
    session, store = _DuneSession(rows), _Store()
    out = Dune({"query_id": 7}).fetch(store, "fills", 0, T0 + DAY, log=lambda *_: None, session=session,
                                      sleep=lambda *_: None)
    assert session.posted[0][1] == {"query_parameters": {"wallet": _Store.wallet}}
    assert out["foreign_wallet_rows"] == 1 and len(store.saved) == 1 and out["execution_id"] == "E1"


# ------------------------------------------------------------ confirmed P1

def test_198_longest_underwater_scans_every_episode():
    from metrics.performance import risk
    days, eq, i = [], 1000.0, 0
    for _ in range(11):                      # eleven deep one-day dips, recovered next day
        for ret in (-0.20, 0.25):
            eq *= 1 + ret
            days.append(_day(i, ret, 0.0, eq))
            i += 1
    for ret in [-0.01] + [0.0] * 30 + [0.0102]:  # one shallow dip lasting a month
        eq *= 1 + ret
        days.append(_day(i, ret, 0.0, eq))
        i += 1
    assert risk(days)["longest_drawdown_days"] >= 31         # was 1 (only the deepest 10)


def test_199_window_cum_pnl_restarts_at_zero():
    from metrics.performance import pnl_path
    days = [_day(0, None, -50.0, 0, cum=950.0), _day(1, None, 100.0, 0, cum=1050.0)]
    assert pnl_path(days)["initial_loss"] == pytest.approx(-50)   # was None


def test_200_window_start_defines_empty_windows():
    from metrics.series import window_slice, window_start
    now = T0 + 100 * DAY
    assert window_start("7d", now) == now - 7 * DAY and window_start("all", now) is None
    assert window_slice([_day(0, 0.1, 1, 1)], "7d", now) == []


def test_204_extremes_come_from_their_own_subsets():
    from metrics.behavior import campaign_stats
    camps = [{"id": i, "coin": "BTC", "direction": "long", "eligible_for_win_rate": True, "close_time": i,
              "open_time": i - 1, "duration_ms": 1, "net_pnl": p, "net_pnl_with_funding": p} for i, p in ((1, 5), (2, 9))]
    st = campaign_stats(camps)
    assert st["max_win"] == 9 and st["max_loss"] is None      # max_loss was 5
    assert st["profit_factor"] is None and st["profit_factor_status"] == "no_losses"


def test_207_unknown_notional_is_zero_volume_not_one_dollar():
    from metrics.behavior import activity
    a = activity([{"time": T0, "coin": "BTC", "notional": None, "fill_id": "x"}])
    assert a["volume"] == 0 and a["twap_share"] is None       # volume was 1.0


def test_210_official_zero_equity_is_kept():
    from metrics.account import current_state
    snap = {"dexes": {}, "spot": [], "time": T0, "errors": {"frontendOpenOrders": "timeout"}, "open_orders": 0}
    cur = current_state(snap, {"official_equity": "0", "estimated_total": "123"})
    assert cur["equity"] == 0 and cur["equity_source"] == "official"   # was 123
    assert cur["leverage"] is None
    assert cur["open_orders"] is None                                   # 243: failed request is unknown, not 0


def test_212_excursion_flags_missing_bars():
    from metrics.excursion import _scan
    ladder = {"1h": {T0: (101.0, 99.0), T0 + 2 * HOUR: (105.0, 95.0)}}
    seg = (T0, T0 + 3 * HOUR, 1.0, 100.0, 0.0)
    *_, gap = _scan(seg, ladder, {"1h": T0}, 1.0)
    assert gap                                                  # was False (hole at T0+1h ignored)


def test_222_223_components_align_and_overlap_is_required():
    from reconcile import reconcile_pnl
    rows = [{"time": T0 + i * DAY, "pnl": Decimal(9), "price_pnl": Decimal(10), "fees": Decimal(2),
             "funding": Decimal(1), "realized": Decimal(0), "volume": Decimal(0), "imputed_notional": Decimal(0)}
            for i in range(30)]
    mtm = {"days": rows, "start": T0, "end": T0 + 30 * DAY}
    off = [{"time": T0 + 10 * DAY, "cum_pnl": Decimal(0)}, {"time": T0 + 20 * DAY, "cum_pnl": Decimal(90)}]
    r = reconcile_pnl(off, mtm)
    c = r["components"]
    assert c["price_pnl"] - c["fees"] + c["funding"] == pytest.approx(r["reconstructed"])   # was 30-day sums
    one = reconcile_pnl(off[:1], mtm)
    assert one["status"] == "insufficient_overlap" and one["residual"] is None              # was "ok"


def test_227_no_break_is_not_called_complete():
    from model.positions import PositionPath
    from reconcile import detect_truncation
    path = PositionPath("BTC")
    path.add({"time": T0, "startPosition": "0", "sz": "1", "side": "B", "px": "1"})
    fills = [{"coin": "BTC", "time": T0, "px": "1", "sz": "1"}]
    out = detect_truncation(fills, {"BTC": path}, [{"time": T0, "cum_pnl": Decimal(0)}], {})
    assert out["status"] == "no_gap_detected"                   # was "complete"


# ------------------------------------------------------------ new metrics

def test_trade_metrics_break_even_wilson_streak_and_r():
    from metrics.behavior import campaign_stats, wilson
    pnls = [10, -5] * 12 + [3, 4, -1]
    camps = [{"id": i, "coin": "BTC", "direction": "long", "eligible_for_win_rate": True, "close_time": i * HOUR,
              "open_time": i * HOUR - HOUR // 2, "duration_ms": HOUR // 2, "net_pnl": p, "net_pnl_with_funding": p,
              "closed_pnl": p + 1, "observed_entry_notional": 100.0} for i, p in enumerate(pnls, 1)]
    st = campaign_stats(camps)
    w, l = st["avg_win"], -st["avg_loss"]
    assert st["break_even_win_rate"] == pytest.approx(l / (w + l))
    lo, hi = st["win_rate_strict_ci95"]
    assert lo < st["win_rate_strict"] < hi
    assert wilson(0, 0) is None
    assert st["current_streak"] == -1 and st["max_win_streak"] == 2
    assert st["r"]["status"] == "ok" and st["r"]["one_r"] == pytest.approx(0.05)
    assert st["independent_trades"] == pytest.approx(len(pnls))       # no overlap
    assert len(st["last_20"]) == 20 and st["top_wins"][0]["pnl"] == 10


def test_return_on_peak_capital_and_weekly_share():
    from metrics.performance import performance
    days = [_day(0, 0.1, 100.0, 1100.0), _day(1, -0.05, -55.0, 1045.0)]
    p = performance(days)
    assert p["peak_equity"] == 1100 and p["return_on_peak_capital"] == pytest.approx(45 / 1100)


def test_market_model_recovers_beta():
    from metrics.performance import market_model
    import random
    rnd = random.Random(1)
    closes, px, days = {}, 100.0, []
    closes[T0 - DAY] = px
    for i in range(120):
        m = rnd.gauss(0, 0.02)
        px *= 1 + m
        closes[T0 + i * DAY] = px
        days.append({"time": T0 + (i + 1) * DAY, "ret": 0.0005 + 1.5 * m + rnd.gauss(0, 0.002)})
    out = market_model(days, closes)
    assert out["status"] == "ok" and out["beta"] == pytest.approx(1.5, abs=0.05)
    assert out["beta_ci95"][0] < 1.5 < out["beta_ci95"][1]
    assert market_model(days[:10], closes)["status"] == "insufficient_data"


def test_xirr_reports_status():
    from metrics.performance import xirr_detail
    assert xirr_detail([(0, -100), (365 * DAY, 110)])["status"] == "ok"
    assert xirr_detail([(0, 100), (DAY, 5)])["status"] == "no_sign_change"


def test_capital_summary_counts_both_directions():
    from analysis import capital_summary
    flows = [{"time": T0, "class": "external", "amount_usd": "1000"},
             {"time": T0 + 1, "class": "external", "amount_usd": "-1000"},
             {"time": T0 + 2, "class": "external", "amount_usd": "500"}]
    c = capital_summary(flows, T0 + DAY)
    assert (c["deposited"], c["withdrawn"], c["net"], c["peak_net_invested"]) == (1500, 1000, 500, 1000)
