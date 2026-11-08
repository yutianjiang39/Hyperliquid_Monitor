import io
import json
from datetime import datetime, timezone

import pytest

DAY = 86_400_000
HOUR = 3_600_000
T0 = int(datetime(2025, 8, 1, tzinfo=timezone.utc).timestamp() * 1000)


# ---------------------------------------------------------------- metrics

def test_drawdown_summary_durations_counts_and_shares():
    from metrics.performance import drawdown_summary
    values = [1.0, 1.1, 0.99, 1.1, 1.2, 1.08, 1.14]
    rows = [{"time": i * DAY, "index": v} for i, v in enumerate(values)]
    s = drawdown_summary(rows)
    assert s["count"] == 2
    assert s["avg_depth"] == pytest.approx(0.10)
    assert s["max_drawdown_duration_days"] == 1
    assert s["max_recovery_days"] == 1
    assert s["longest_underwater_days"] == 2
    assert s["current_underwater_days"] == 2
    assert s["underwater_ratio"] == pytest.approx(3 / 7)
    assert s["new_high_ratio"] == pytest.approx(2 / 6)


def test_omega_tail_ratio_and_monthly_stability():
    from metrics.performance import monthly_stability, omega, tail_ratio
    assert omega([0.1, -0.05, 0.02, -0.01]) == pytest.approx(2.0)
    assert omega([0.1, 0.2]) is None
    rets = [x / 100 for x in range(-5, 15)]
    assert tail_ratio(rets) == pytest.approx(0.1305 / 0.0405)
    assert tail_ratio(rets[:10]) is None
    m = monthly_stability([{"pnl": 10, "ret": 0.1}, {"pnl": -5, "ret": -0.05}, {"pnl": 3, "ret": 0.03}])
    assert m["monthly_win_rate"] == pytest.approx(2 / 3)
    assert m["positive_months"] == 2


def _campaign(i, pnl, direction="long", coin="BTC"):
    return {"id": f"c{i}", "coin": coin, "direction": direction, "eligible_for_win_rate": True,
            "close_time": i * HOUR, "open_time": i * HOUR - HOUR, "duration_ms": HOUR,
            "net_pnl": pnl, "net_pnl_with_funding": pnl}


def test_concentration_efficiency_and_by_coin():
    from metrics.behavior import by_coin, campaign_stats
    pnls = [100, 50, 10, -20, 5, 1, 1, 1, 1, 1]
    camps = [_campaign(i + 1, p, "short" if p == 50 else "long", "ETH" if p == 10 else "BTC")
             for i, p in enumerate(pnls)]
    st = campaign_stats(camps)
    assert st["concentration"]["top_count"] == 1
    assert st["concentration"]["top_share"] == pytest.approx(100 / 170)
    assert st["efficiency_ratio"] == pytest.approx(150 / 190)
    assert st["by_direction"]["short"]["pnl"] == 50
    assert st["concentration"]["pareto"][-1]["share_of_profit"] == pytest.approx(150 / 170)
    ops = [{"coin": "BTC", "closed_pnl": "10", "fee": "1", "fee_token": "USDC", "notional": "1000"},
           {"coin": "ETH", "closed_pnl": "-3", "fee": "0.5", "fee_token": "USDC", "notional": "500"}]
    funding = [{"time": 1, "delta": {"coin": "ETH", "usdc": "2"}}]
    rows = {r["coin"]: r for r in by_coin(camps, ops, funding)}
    assert rows["BTC"]["net"] == pytest.approx(9)
    assert rows["ETH"]["net"] == pytest.approx(-1.5)
    assert rows["ETH"]["campaigns"] == 1 and rows["ETH"]["win_rate"] == 1


def test_scorecard_groups_and_sources():
    from metrics.scorecard import scorecard
    cards = scorecard(perf={"twr": 0.5, "pnl": 10}, risk={"leverage_mean": 0.01}, behavior={}, activity={},
                      costs={"funding_net": 5.0}, official={"pnl": "100", "vlm": "1000"}, equity=50.0, coins=[])
    items = {i["key"]: i for g in cards for i in g["items"]}
    assert items["total_pnl"]["value"] == 100 and items["total_pnl"]["source"] == "official"
    assert items["turnover_equity"]["value"] == pytest.approx(20)
    assert items["funding_share"]["value"] == pytest.approx(0.05)
    # Day-end leverage ~0 (intraday trader): ratio would explode, so it is withheld.
    assert items["lev_adj_roi"]["value"] is None
    assert all(set(i) == {"key", "label", "value", "unit", "source", "definition"} for i in items.values())


# ------------------------------------------------------------- MAE / MFE

def test_excursion_uses_finest_interval_per_segment():
    from metrics.excursion import excursions
    camp = {"id": "c1", "coin": "BTC", "direction": "long", "eligible_for_win_rate": True,
            "open_time": T0, "close_time": T0 + 3 * HOUR, "duration_ms": 3 * HOUR, "net_pnl": 10}
    ops = [{"campaign_id": "c1", "time": T0, "price": "100", "end_position": "1"},
           {"campaign_id": "c1", "time": T0 + 3 * HOUR, "price": "110", "end_position": "0"}]
    ladder = {
        "15m": {T0 + 2 * HOUR + k * 900_000: (112.0, 99.0) for k in range(4)},
        # The 1h bar at +2h must not be used: 15m covers that stretch.
        "1h": {T0: (105.0, 95.0), T0 + HOUR: (120.0, 98.0), T0 + 2 * HOUR: (200.0, 50.0)},
        "4h": {}, "1d": {},
    }
    asked = []
    out = excursions([camp], ops, lambda coin, since: asked.append((coin, since)) or ladder)
    assert asked == [("BTC", T0)]
    row = out["rows"][0]
    assert row["mae"] == pytest.approx(-5)
    assert row["mfe"] == pytest.approx(20)
    assert row["mfe_pct"] == pytest.approx(0.2)
    assert row["resolution"] == "1h" and not row["coarse"]
    assert row["capture"] == pytest.approx(0.5)
    assert out["evaluated"] == 1


def test_ladder_requests_latest_5000_bars_per_interval():
    from sources.market_data import INTERVAL_MS, ladder
    calls = []

    class Client:
        def post(self, body):
            calls.append(body["req"])
            return []

    now = T0 + 400 * DAY
    ladder(Client(), None, "BTC", since=T0, now=now, throttle=0)
    by = {c["interval"]: c for c in calls}
    assert set(by) == {"15m", "1h", "4h", "1d"} and len(calls) == 4  # one request per interval
    assert by["15m"]["startTime"] == now - INTERVAL_MS["15m"] * 4999
    assert by["1d"]["startTime"] == T0  # clipped to the first trade, not 5000 days back


# ------------------------------------------------------------- backfill

def _fill(t, start, sz, side, tid):
    return {"coin": "BTC", "time": t, "px": "100", "sz": str(sz), "side": side, "startPosition": str(start),
            "closedPnl": "0", "fee": "0", "feeToken": "USDC", "tid": tid, "oid": tid, "hash": "0x" + str(tid),
            "dir": "Open Long"}


def _gapped_store(store):
    # 10:00 opens 1, 15:00 sees start 2 -> one fill (12:30, +1) is missing in between.
    store.put("fills", _fill(T0 + 10 * HOUR, 0, 1, "B", 1), "api:userFillsByTime")
    store.put("fills", _fill(T0 + 15 * HOUR, 2, 2, "A", 3), "api:userFillsByTime")
    store.commit()
    return store


def test_gap_windows_and_hours(store):
    from sources.backfill import targets
    from sources.backfill_targets import hour_set
    windows = targets(_gapped_store(store), now=T0 + DAY)
    assert [(w["start"], w["end"], w["reason"]) for w in windows] == [(T0 + 10 * HOUR, T0 + 15 * HOUR, "fill_gap")]
    assert hour_set(windows) == {T0 + h * HOUR for h in range(10, 16)}


def test_snapshot_corrected_gap_steps():
    from decimal import Decimal
    from model.positions import build_paths, position_snapshots
    from engine import ordered_fills
    fills = ordered_fills([_fill(T0 + 10 * HOUR, 0, 1, "B", 1), _fill(T0 + 15 * HOUR, 2, 2, "A", 3)])
    snaps = position_snapshots([{"time": T0 + 11 * HOUR, "coin": "BTC", "szi": "2"}])
    path = build_paths(fills, snapshots=snaps)["BTC"]
    gap = path.breaks[0]
    assert gap["snapshot_corrected"] and gap["steps"] == [{"time": T0 + 10 * HOUR + HOUR // 2, "delta": "1"}]
    assert path.at(T0 + 12 * HOUR) == Decimal(2)


class FakeS3:
    def __init__(self, buckets):
        self.buckets, self.gets, self.lists = buckets, [], []

    def list_objects_v2(self, Bucket, Prefix="", StartAfter=None, ContinuationToken=None, **kw):
        self.lists.append(Prefix)
        keys = sorted(k for k in self.buckets[Bucket] if k.startswith(Prefix) and (StartAfter is None or k > StartAfter))
        return {"Contents": [{"Key": k, "Size": len(self.buckets[Bucket][k]), "ETag": '"e"'} for k in keys],
                "IsTruncated": False}

    def get_object(self, Bucket, Key, Range=None, **kw):
        data = self.buckets[Bucket][Key]
        if Range:
            a, b = map(int, Range[len("bytes="):].split("-"))
            data = data[a:b + 1]
        self.gets.append((Key, Range, len(data)))
        return {"Body": io.BytesIO(data)}


def _node_hour(events):
    import lz4.frame
    lines = [json.dumps({"block_number": 1, "block_time": "2025-08-01T12:30:00", "events": events})]
    return lz4.frame.compress(("\n".join(lines) + "\n").encode())


def _official_bucket(wallet):
    hidden = _fill(T0 + 12 * HOUR + 30 * 60_000, 1, 1, "B", 2)
    objects = {}
    for h in range(24):
        events = [[wallet, hidden]] if h == 12 else [["0x" + "1" * 40, _fill(T0 + h * HOUR, 0, 1, "B", 100 + h)]]
        objects[f"node_fills_by_block/hourly/20250801/{h}.lz4"] = _node_hour(events)
        objects[f"node_fills/hourly/20250801/{h}.lz4"] = _node_hour(events)
    return {"hl-mainnet-node-data": objects}


def _config(**backfill):
    from local_runtime import validate_config
    return validate_config({"backfill": {"enabled": True, **backfill}})


def test_official_backfill_reads_only_gap_hours_and_closes_gap(store):
    pytest.importorskip("lz4")
    from sources.backfill import run_backfill
    s3 = FakeS3(_official_bucket(store.wallet))
    status = run_backfill(_gapped_store(store), _config(sources=["official_s3"]), log=lambda *a: None,
                          s3_factory=lambda region: s3, now=T0 + DAY)
    assert sorted(k for k, _, _ in s3.gets) == sorted(f"node_fills_by_block/hourly/20250801/{h}.lz4" for h in range(10, 16))
    assert status["before"]["fill_gaps"] == 1 and status["after"]["fill_gaps"] == 0
    assert any(r.get("tid") == 2 for r in store.rows("fills"))


def test_official_backfill_stops_over_budget(store):
    pytest.importorskip("lz4")
    from sources.backfill import run_backfill
    s3 = FakeS3(_official_bucket(store.wallet))
    status = run_backfill(_gapped_store(store), _config(sources=["official_s3"], max_cost_usd=1e-12),
                          log=lambda *a: None, s3_factory=lambda region: s3, now=T0 + DAY)
    assert status["sources"]["official_s3"]["status"] == "over_budget"
    assert s3.gets == []


def test_backfill_disabled_or_without_credentials_makes_no_requests(store, monkeypatch, tmp_path):
    from sources.backfill import run_backfill
    from local_runtime import DEFAULT_CONFIG

    def boom(region):
        raise AssertionError("no S3 client may be created")
    assert run_backfill(store, DEFAULT_CONFIG, log=lambda *a: None, s3_factory=boom)["status"] == "disabled"
    pytest.importorskip("boto3")
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "none"))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "none"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    logs = []
    status = run_backfill(store, _config(), log=logs.append)
    assert status["status"] == "skipped" and "未启用付费补数" in logs[-1]


def _parquet(rows, group):
    import pyarrow as pa
    import pyarrow.parquet as pq
    buf = io.BytesIO()
    pq.write_table(pa.Table.from_pylist(rows), buf, row_group_size=group, compression="NONE", use_dictionary=False)
    return buf.getvalue()


def _reservoir_bucket(wallet, filler=6000):
    import random
    rnd = random.Random(7)
    hidden = {"user": wallet, "time": T0 + 12 * HOUR + 30 * 60_000, "coin": "BTC", "price": "100", "size": "1",
              "side": "buy", "closed_pnl": "0", "fee": "0", "fee_token": "USDC", "start_position": "1", "tid": 2}
    others = sorted(("0x" + "".join(rnd.choice("0123456789abcdef") for _ in range(40)) for _ in range(2 * filler)))
    rows = [dict(hidden, user=u, tid=10 + i) for i, u in enumerate(others[:filler])]
    rows += [hidden]
    rows += [dict(hidden, user=u, tid=10 + filler + i) for i, u in enumerate(others[filler:])]
    rows.sort(key=lambda r: r["user"])
    snaps = [{"user": wallet, "time": T0 + 20 * HOUR, "coin": "BTC", "szi": "0"}]
    return {"hydromancer-reservoir": {
        "by_dex/hyperliquid/fills/daily/2025-08-01.parquet": _parquet(rows, filler // 2),
        "by_dex/hyperliquid/positions/daily/2025-08-01.parquet": _parquet(snaps, 100),
    }}


def test_reservoir_range_reads_skip_row_groups(store):
    pytest.importorskip("pyarrow")
    from sources.reservoir import FILL_ALIASES, wallet_rows
    s3 = FakeS3(_reservoir_bucket(store.wallet))
    key = "by_dex/hyperliquid/fills/daily/2025-08-01.parquet"
    size = len(s3.buckets["hydromancer-reservoir"][key])
    rows, info = wallet_rows(s3, "hydromancer-reservoir", key, size, store.wallet, FILL_ALIASES, day_ms=T0)
    assert [r["tid"] for r in rows] == [2]
    assert info["skipped_groups"] >= 2
    assert info["bytes"] < size * 0.6


def test_reservoir_backfill_closes_gap_and_stores_snapshots(store):
    pytest.importorskip("pyarrow")
    from sources.backfill import run_backfill
    s3 = FakeS3(_reservoir_bucket(store.wallet, filler=200))
    status = run_backfill(_gapped_store(store), _config(sources=["reservoir"]), log=lambda *a: None,
                          s3_factory=lambda region: s3, now=T0 + DAY)
    assert status["after"]["fill_gaps"] == 0
    added = [r for r in store.rows("fills") if r.get("tid") == 2]
    assert added and added[0]["source"] == "plugin:reservoir" and added[0]["side"] == "B"
    assert [r["szi"] for r in store.rows("position_snapshot")] == ["0"]


def test_config_backfill_validation():
    from local_runtime import validate_config
    with pytest.raises(ValueError):
        validate_config({"backfill": {"aws_secret_access_key": "x"}})
    with pytest.raises(ValueError):
        validate_config({"backfill": {"sources": ["somewhere"]}})
    assert validate_config({})["backfill"]["enabled"] is False
