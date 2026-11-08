import hashlib
import json
import re
from decimal import Decimal

import pytest

from conftest import ROOT, WALLETS, FixtureClient


def test_snapshot_from_fixtures_composes_close_to_official(tmp_path):
    from collector import HistoryStore
    from sources.snapshots import collect_snapshot, compose
    wallet = WALLETS[0]
    store = HistoryStore(tmp_path / "w.sqlite", wallet)
    try:
        snap = collect_snapshot(store, FixtureClient(wallet), spot_prices={"USDC": "1", "USDT0": "1", "USDH": "1"},
                                log=lambda *a: None)
        assert snap["mode"] == "unifiedAccount"
        assert "" in snap["dexes"]
        portfolio = json.loads((ROOT / "tests/fixtures" / wallet / "portfolio.json").read_text(encoding="utf-8"))
        equity = dict(portfolio)["allTime"]["accountValueHistory"][-1][1]
        comp = compose(snap, equity)
        assert abs(Decimal(comp["residual_pct"])) < 1
    finally:
        store.close()


def test_plugins_disabled_by_default(monkeypatch):
    from local_runtime import DEFAULT_CONFIG
    from sources import active_plugins
    monkeypatch.setenv("HYDROMANCER_API_KEY", "x")
    assert active_plugins(DEFAULT_CONFIG) == []
    assert [p.name for p in active_plugins(DEFAULT_CONFIG, {"hydromancer"})] == ["hydromancer"]


def test_config_rejects_plugin_secrets():
    from local_runtime import validate_config
    with pytest.raises(ValueError):
        validate_config({"plugins": {"dune": {"api_key": "secret"}}})


def test_vendor_manifest_hashes():
    manifest = json.loads((ROOT / "src/vendor/manifest.json").read_text(encoding="utf-8"))
    for name, item in manifest["files"].items():
        assert hashlib.sha256((ROOT / "src/vendor" / name).read_bytes()).hexdigest() == item["sha256"], name


@pytest.fixture(scope="module")
def demo_report(tmp_path_factory):
    from local_runtime import Runtime
    base = tmp_path_factory.mktemp("demo")
    (base / "config.json").write_text(json.dumps({"data_dir": "data", "output_dir": "reports"}), encoding="utf-8")
    return Runtime(base / "config.json").run_demo(log=lambda *a: None)


def test_demo_report_is_offline_and_reconciles(demo_report):
    from pathlib import Path
    html = Path(demo_report["html"])
    folder = html.parent
    remote = re.compile(r"""(?:src|href)\s*=\s*["']\s*(?:https?:)?//""", re.I)
    for path in [html, folder / "assets/app.js"]:
        assert not remote.search(path.read_text(encoding="utf-8")), path
    assert (folder / "assets/vendor/echarts.min.js").is_file()
    assert list((folder / "data").glob("operations_*.js"))
    assert Path(demo_report["zip"]).is_file()
    assert demo_report["model_summary"]["reconciliation"] == "ok"
    assert "模拟数据演示" in html.read_text(encoding="utf-8")


def test_spot_prices_join_contexts_by_coin():
    from sources.market_data import fetch_market_meta

    class Client:
        def post(self, body):
            if body["type"] == "spotMetaAndAssetCtxs":
                meta = {"tokens": [{"index": 0, "name": "USDC"}, {"index": 150, "name": "HYPE"}, {"index": 9, "name": "JUNK"}],
                        "universe": [{"name": "@9", "index": 9, "tokens": [9, 0]}, {"name": "@107", "index": 107, "tokens": [150, 0]}]}
                # Longer than the universe and out of order, as the live API returns.
                ctxs = [{"coin": "@107", "markPx": "40.5", "dayNtlVlm": "2e7"}, {"coin": "@999", "markPx": "1"},
                        {"coin": "@9", "markPx": "0.001", "dayNtlVlm": "5"}]
                return [meta, ctxs]
            if body["type"] == "perpDexs":
                return [None]
            return [{"universe": [], "collateralToken": 0}, []]

    out = fetch_market_meta(Client(), log=lambda *a: None)
    assert out["@107"]["mark_px"] == "40.5"
    assert out["@9"]["mark_px"] == "0.001"
