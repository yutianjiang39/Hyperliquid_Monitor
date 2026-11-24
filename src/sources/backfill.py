"""Optional paid backfill of fills the public API cannot return.

Runs only when ``backfill.enabled`` is true *and* boto3 resolves AWS
credentials (profile or the standard environment chain); otherwise it records
"未启用付费补数" and makes no AWS request. Both sources are requester-pays,
so every run is capped by ``backfill.max_cost_usd`` (transfer estimate):

- ``reservoir``: Hydromancer Reservoir daily Parquet, fills + position
  snapshots from 2025-07-28, range-read so only the wallet's row groups move.
- ``official_s3``: Hyperliquid ``hl-mainnet-node-data`` hourly lz4 files from
  2025-05-25; whole hourly objects, so only the gap hours are listed and read.

Sources run in the configured order; targets are recomputed after each one so
a gap closed by the first source is not paid for again.
"""
from __future__ import annotations

import time

from account_history import latest_window_stats, official_series
from collector import estimate_cost, inventory_archives, one_per_hour, scan_archives
from engine import ordered_fills
from model.positions import build_paths, position_snapshots
from reconcile import detect_truncation
from sources import reservoir
from sources.backfill_targets import RESERVOIR_START_MS, day_set, gap_windows, hour_set, summarize
from sources.snapshots import latest_snapshot

FILL_PREFIXES = ("node_fills_by_block", "node_fills")


def credentials_available(profile=""):
    """(ok, reason). Never returns or logs the credentials themselves."""
    try:
        import boto3
    except ImportError:
        return False, "未安装 boto3"
    try:
        creds = boto3.Session(profile_name=profile or None).get_credentials()
    except Exception as exc:
        return False, f"AWS profile 不可用（{type(exc).__name__}）"
    return (True, None) if creds is not None else (False, "未找到 AWS 凭证（profile 或环境变量）")


def default_factory(profile=""):
    def make(region):
        import boto3
        from botocore.config import Config
        return boto3.Session(profile_name=profile or None, region_name=region).client(
            "s3", config=Config(retries={"max_attempts": 5, "mode": "standard"}, connect_timeout=15, read_timeout=90))
    return make


def targets(store, mode="gaps", now=None):
    fills = ordered_fills(list(store.rows("fills")))
    snapshot = latest_snapshot(store)
    paths = build_paths(fills, snapshot, snapshots=position_snapshots(store.rows("position_snapshot")))
    truncation = detect_truncation(fills, paths, official_series(store, "perp"), latest_window_stats(store))
    return gap_windows(paths, truncation, mode=mode, now=now)


def _save(store, status, log):
    store.set_meta("backfill_status", status)
    store.commit()
    if status.get("note"):
        log(status["note"])
    return status


def run_backfill(store, config, *, log=print, cancel_check=None, s3_factory=None, now=None):
    settings = config.get("backfill") or {}
    now = int(time.time() * 1000) if now is None else now
    if not settings.get("enabled"):
        return _save(store, {"status": "disabled", "note": "未启用付费补数（backfill.enabled = false）。", "time": now}, log)
    profile = (config.get("aws") or {}).get("profile", "")
    if s3_factory is None:
        ok, reason = credentials_available(profile)
        if not ok:
            return _save(store, {"status": "skipped", "note": "未启用付费补数：" + reason + "。", "time": now}, log)
        s3_factory = default_factory(profile)
    windows = targets(store, settings.get("mode", "gaps"), now)
    before = summarize(windows)
    if not windows:
        return _save(store, {"status": "nothing_to_do", "note": "付费补数：没有需要补的缺口。", "time": now,
                             "before": before}, log)
    budget = reservoir.Budget(settings.get("max_cost_usd", 5.0))
    results = {}
    for source in settings.get("sources") or []:
        if cancel_check:
            cancel_check()
        windows = targets(store, settings.get("mode", "gaps"), now)
        if not windows:
            break
        try:
            if source == "reservoir":
                results[source] = _reservoir(store, s3_factory, settings, windows, budget, log, cancel_check)
            elif source == "official_s3":
                results[source] = _official(store, s3_factory, config, windows, budget, log, cancel_check)
        except reservoir.BudgetExceeded as exc:
            results[source] = {"status": "over_budget", "note": str(exc)}
            log(f"付费补数 {source}：{exc}，已停止。")
        except ImportError as exc:
            results[source] = {"status": "skipped", "note": f"缺少可选依赖：{exc.name}（pip install -r requirements.txt）"}
            log(f"付费补数 {source}：" + results[source]["note"])
    after = summarize(targets(store, settings.get("mode", "gaps"), now))
    return _save(store, {"status": "done", "time": now, "before": before, "after": after, "sources": results,
                         "cost_usd_estimate": round(budget.usd, 4), "max_cost_usd": settings.get("max_cost_usd"),
                         "note": f"付费补数完成：缺口 {before['fill_gaps']} → {after['fill_gaps']}，"
                                 f"估算传输费用 ${budget.usd:,.2f}。"}, log)


def _reservoir(store, s3_factory, settings, windows, budget, log, cancel_check):
    import pyarrow.parquet  # noqa: F401  (fail early with a clear hint)
    days = {d for d in day_set(windows) if d >= RESERVOIR_START_MS - 86_400_000}
    if not days:
        return {"status": "out_of_range", "note": "缺口早于 Reservoir 覆盖起点 2025-07-28"}
    client = s3_factory(settings.get("reservoir_region") or "ap-northeast-1")
    log(f"付费补数 Reservoir：{len(days)} 个缺口日 …")
    stats = reservoir.backfill(store, client, days, bucket=settings.get("reservoir_bucket") or "hydromancer-reservoir",
                               prefix=settings.get("reservoir_prefix") or "", budget=budget, log=log,
                               cancel_check=cancel_check)
    return {"status": "done", "days": len(days), **stats}


def _official(store, s3_factory, config, windows, budget, log, cancel_check):
    hours = hour_set(windows)
    client = s3_factory((config.get("aws") or {}).get("region") or "us-east-1")
    log(f"付费补数 官方 S3：{len(hours)} 个缺口小时，先列清单估算费用 …")
    inventory = one_per_hour(inventory_archives(store, min(hours), max(hours) + 3_600_000, client=client,
                                                prefixes=FILL_PREFIXES, hours=hours, log=log, cancel_check=cancel_check))
    cost = estimate_cost(inventory)
    remaining = None if budget.max_bytes is None else (budget.max_bytes - budget.used) / 1e9 * budget.usd_per_gb
    if remaining is not None and cost["usd"] > remaining:
        log(f"官方 S3 预计传输 {cost['pending_bytes'] / 2**30:,.2f} GiB ≈ ${cost['usd']:,.2f}，"
            f"超出剩余预算 ${remaining:,.2f}；未下载。提高 backfill.max_cost_usd 后重跑。")
        return {"status": "over_budget", **cost, "remaining_usd": remaining}
    if not cost["objects"]:
        return {"status": "nothing_listed", **cost}
    budget.take(cost["pending_bytes"])
    scan = scan_archives(store, inventory, client=client, accept_requester_pays=True,
                         max_download_gib=cost["pending_bytes"] / 2**30 * 1.01 + 1e-6, log=log, cancel_check=cancel_check)
    return {"status": scan["status"], **cost, "scan": scan}
