"""Compose target-wallet cache, accounting, offline report, and audit exports."""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from collector import coverage_report, export_audit, fetch_asset_meta, ledger_view
from engine import analyze_fills
from report import build_report, archive_report
from curves import build_curves
from balance import build_balance_curve
from event_timeline import attach_event_timeline


def generate_report(store, output_dir, start, end, *, inventory=None, client=None,
                    chunk_size=1000, log=print, cancel_check=None, analysis_sink=None, coverage_extra=None):
    if cancel_check:
        cancel_check()
    fills = list(store.rows("fills", start, end))
    funding = list(store.rows("funding", start, end))
    ledger = [ledger_view(row, store.wallet) for row in store.rows("ledger", start, end)]
    meta = store.get_meta("asset_meta", {})
    if client is not None:
        fresh = fetch_asset_meta(client, fills, store=store, log=log)
        meta.update(fresh)
    for row in funding:
        coin = row.get("delta", {}).get("coin", "")
        row["pnl_token"] = meta.get(coin, {}).get("pnl_token") or ("USDC" if ":" not in coin else None)
    log(f"分析去重成交 {len(fills):,} 条，资金流水 {len(ledger):,} 条，资金费 {len(funding):,} 条 …")
    analysis = analyze_fills(fills, funding=funding, asset_meta=meta)
    # Keep all cached cash flows through the latest balance anchor, even when
    # the requested report ends earlier. Otherwise backward accounting would
    # silently omit post-window withdrawals/trades. Earlier fills also warm up
    # position quantities and average costs; displayed PnL still starts at the
    # report boundary.
    anchors = store.get_meta("balance_anchors", []) or []
    anchor_end = max((int(a.get("time") or 0) for a in anchors if isinstance(a, dict)), default=0)
    cash_end = max(end, anchor_end)
    all_fills = list(store.rows("fills", 0, cash_end))
    curve_operations = (analysis["operations"] if len(all_fills) == len(fills)
                        else analyze_fills(all_fills, asset_meta=meta)["operations"])
    cash_funding = list(store.rows("funding", 0, cash_end))
    cash_ledger = list(store.rows("ledger", 0, cash_end))
    if len(all_fills) != len(fills):
        log(f"用全部 {len(all_fills):,} 条缓存成交恢复期初仓位及窗口外现金变动 …")
    portfolio = store.get_meta("portfolio_history", [])
    portfolio_meta = dict(store.get_meta("portfolio_meta", {}))
    # An assets-only refresh can return newer equity than the cached fills.
    # Never extend old exposure into that new period as though no trade occurred.
    fill_watermark = store.get_meta("api:userFillsByTime", {})
    observed_through = max((op.get("time") or 0 for op in curve_operations), default=0)
    if (fill_watermark.get("exhausted") and not fill_watermark.get("gaps")
            and fill_watermark.get("status") == "api_visible_exhausted"):
        observed_through = max(observed_through, int(fill_watermark.get("end_ms") or 0))
    portfolio_meta["positions_observed_through_ms"] = min(end, observed_through)
    log("重建逐笔盈亏、仓位规模、账面资产与参考杠杆曲线 …")
    curves = build_curves(curve_operations, portfolio, start=start, end=end,
                          portfolio_meta=portfolio_meta)
    watermarks = [store.get_meta("api:" + key, {}) for key in
                  ("userFillsByTime", "userFunding", "userNonFundingLedgerUpdates")]
    ends = [int(w.get("end_ms") or 0) for w in watermarks
            if w.get("exhausted") and not w.get("gaps") and w.get("status") == "api_visible_exhausted"]
    gaps = [g for w in watermarks for g in (w.get("gaps") or []) if isinstance(g, dict)]
    for row in store.rows("raw_events", 0, cash_end):
        gaps.append({"start_ms": row.get("time"), "end_ms": row.get("time"),
                     "reason": "unclassified_wallet_event"})
    cash_coverage = {
        "complete": False,  # API exhaustion does not prove lifetime completeness.
        "observed_through_ms": min(ends) if len(ends) == 3 else min(
            (max((int(r.get("time") or 0) for r in rows), default=0)
             for rows in (all_fills, cash_funding, cash_ledger)), default=0),
        "available_fill_start_ms": min((int(r.get("time") or 0) for r in all_fills), default=None),
        "gaps": gaps,
        "ledger_fee_modes": store.get_meta("ledger_fee_modes", {}),
    }
    balance = build_balance_curve(curve_operations, cash_funding, cash_ledger,
        wallet=store.wallet, asset_meta=meta, anchors=anchors, start=start, end=end,
        event_times=[*curves["event_curve"], *curves["equity_curve"]], coverage=cash_coverage)
    attach_event_timeline(curves, balance, curve_operations)
    anchor_meta = store.get_meta("balance_anchor_meta", {}) or {}
    curves["balance_anchor_meta"] = anchor_meta
    curves["warnings"] = list(dict.fromkeys([*curves["warnings"], *anchor_meta.get("warnings", [])]))
    log(f"统一时间轴 {len(curves['timeline_curve']):,} 点；账面资产参考值 "
        f"{balance['summary']['observed_balance_points']:,} 点；参考杠杆 "
        f"{curves['summary']['observed_balance_entry_leverage_points']:,} 点。")
    if cancel_check:
        cancel_check()
    coverage = coverage_report(store, start, end, inventory)
    # A detected position gap is direct evidence of imperfect operation history;
    # it is displayed alongside source-level gaps, never hidden in aggregate PnL.
    coverage["position_quality"] = analysis["summary"].get("quality_counts", {})
    coverage["asset_meta_errors"] = store.get_meta("asset_meta_errors", [])
    coverage["account_history"] = portfolio_meta
    coverage["cash_balance_history"] = {"summary": balance["summary"], "anchor_meta": anchor_meta,
        "diagnostics": balance["diagnostics"], "coverage": cash_coverage}
    coverage["curves"] = curves.get("summary", {})
    if coverage_extra:
        coverage["local_runtime"] = coverage_extra
        coverage["warnings"].extend(coverage_extra.get("warnings", []))
    path = build_report(output_dir, analysis, ledger, funding, coverage, store.wallet,
                        chunk_size=chunk_size, curves=curves)
    if cancel_check:
        cancel_check()
    export_audit(store, Path(output_dir) / "audit", start, end, inventory)
    (Path(output_dir) / "audit" / "asset_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    for name, payload in (("portfolio_history", portfolio), ("portfolio_meta", portfolio_meta),
                          ("portfolio_latest_raw", store.get_meta("portfolio_latest_raw", [])),
                          ("portfolio_provenance", store.get_meta("portfolio_provenance", {})),
                          ("balance_anchors", anchors), ("balance_anchor_meta", anchor_meta),
                          ("balance_anchor_latest_raw", store.get_meta("balance_anchor_latest_raw", {})),
                          ("balance_diagnostics", balance["diagnostics"])):
        (Path(output_dir) / "audit" / (name + ".json")).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (Path(output_dir) / "audit" / "accounting_rules.txt").write_text(
        "操作按每笔成交前后有符号仓位重建，反手拆为平旧仓/开新仓。\n"
        "完整回合必须观察到从零建仓直到归零，且中间仓位连续；不能从价格倒算缺失的官方 closedPnl。\n"
        "交易费 fee 已含 builderFee，不重复扣除。净盈亏按计价币种计算，不自动汇兑。\n"
        "胜率 = 净盈亏为正的完整且盈亏已知回合 / 全部完整且盈亏已知回合（含保本回合）。\n"
        "资金费独立统计，不强行分摊到同毫秒归属不确定的仓位回合。\n"
        "盈亏曲线逐笔保留，不按日合并；毛盈亏累计 closedPnl，扣费线只扣同币种 fee（已含 builderFee），均不含资金费和浮盈。\n"
        "曲线汇总已覆盖的 USDC 计价永续（含 HIP-3），市场名保留 DEX 前缀；现货和其他计价币种不混加。\n"
        "仓位成本口径为各币种 abs(持仓数量) × 加权开仓均价的总和；多空取绝对值相加。\n"
        "最近成交价口径使用各币种最近已观察成交价，仅为估算，不是历史标记价格市值。\n"
        "官方权益直接使用 portfolio.accountValueHistory；含浮盈和资金进出，不等于现金余额或跨链钱包总资产。\n"
        "账面资产按可靠或明确标记的估算余额基准及逐事件资金流水反推；不把综合净值直接当现金，不把开永续的名义本金当支出。\n"
        "USDC现货买卖计全额现金收支；USDC永续只计已实现盈亏、费用与资金费；同地址现货/永续资金划转抵消。\n"
        "自动余额基准来自多个公开API请求，非原子快照，默认绘制已知流水重建参考线；严格线需已核验基准且不跨已知缺项。\n"
        "资金余额不含非USDC现货、Vault、借贷投资及跨链资产；借入款不等于净资产。\n"
        "默认参考杠杆=所示永续仓位参考规模/逐事件USDC账面余额；余额非正或仓位未知时留空。\n"
        "官方权益杠杆模式仍只使用不晚于该时点的净值采样，超时或非正权益不计算。所有参考倍数均非全账户实时风险杠杆。\n"
        "缺失期初成本可从连续持仓链的减仓价、数量及closedPnl反推；使用后续成交的成本带事后重建标记，不能用于无未来信息回测。\n"
        "实际成交已实现盈亏包含尚未清仓回合中的减仓盈利/亏损，未平仓浮盈不计入。\n"
        "本报告按指定日期窗口分析；若窗口切掉开仓段，该回合会被标为不完整。\n",
        encoding="utf-8")
    # Keep the enclosing folder in the ZIP, so the data directory stays adjacent.
    output = Path(output_dir).resolve()
    zip_path = archive_report(output)
    if cancel_check:
        cancel_check()
    if analysis_sink:
        analysis_sink(analysis, ledger, funding, coverage)
    result = {"html": str(path.resolve()), "zip": str(zip_path), "summary": analysis["summary"],
              "coverage": coverage, "curve_summary": curves.get("summary", {})}
    log(f"报告已生成：{zip_path.name} ({zip_path.stat().st_size / 2**20:,.2f} MiB)")
    log("完整解压后打开 index.html；请保留 data、exports、audit 文件夹。")
    return result

