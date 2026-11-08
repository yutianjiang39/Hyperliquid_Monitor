"""Compose wallet cache -> analysis model -> offline report, audit exports and ZIP."""
from __future__ import annotations

import json
from pathlib import Path

from analysis import analyze_wallet
from collector import coverage_report, export_audit, ledger_view
from report import archive_report, build_report


def generate_report(store, output_dir, start, end, *, inventory=None, client=None,
                    chunk_size=5000, log=print, cancel_check=None, analysis_sink=None, coverage_extra=None,
                    title=None, mae=False):
    """Analyze the whole cache and write the report.

    ``start``/``end`` scope the source-coverage audit only: metrics always use
    the full cache (official history is anchored to it), and the report lets
    the reader choose a time window.
    """
    if cancel_check:
        cancel_check()
    model, engine = analyze_wallet(store, client, log=log, cancel_check=cancel_check, mae=mae)
    coverage = coverage_report(store, start, end, inventory)
    coverage["quality"] = {k: v for k, v in model["quality"].items()
                           if k not in ("reconciliation_intervals", "position_breaks")}
    coverage["status"] = model["quality"]["truncation"]["status"]
    if coverage_extra:
        coverage["local_runtime"] = coverage_extra
        coverage.setdefault("warnings", []).extend(coverage_extra.get("warnings", []))
    model["coverage"] = {"status": coverage["status"], "warnings": coverage.get("warnings", [])}
    funding = list(store.rows("funding"))
    path = build_report(output_dir, model, engine, funding, chunk_size=chunk_size, title=title)
    if cancel_check:
        cancel_check()
    audit = Path(output_dir) / "audit"
    export_audit(store, audit, start, end, inventory)
    for name, payload in (("asset_meta", store.get_meta("asset_meta", {})),
                          ("latest_snapshot", store.get_meta("latest_snapshot", {})),
                          ("coverage", coverage), ("engine_summary", engine["summary"]),
                          ("reconciliation", model["quality"]["reconciliation"]),
                          ("truncation", model["quality"]["truncation"])):
        (audit / (name + ".json")).write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                                              encoding="utf-8")
    (audit / "accounting_rules.txt").write_text(
        "官方 portfolio（净值、累计盈亏、成交额）与账户快照是真值；成交、资金费、流水用于解释真值。\n"
        "永续盈亏按日盯市重建：期末市值 − 期初市值 − 买卖现金 − 手续费 + 资金费，与官方逐周对账，残差公开。\n"
        "成交链 startPosition 不连续处视为缺失成交，按缺口中点、当日收盘价补录一笔，并统计补录名义金额。\n"
        "日序列：官方采样之间以重建日盈亏为形状，残差平均分摊，保证区间合计等于官方值；隐含资金流 = Δ净值 − Δ盈亏。\n"
        "收益率：Modified Dietz 日收益串联为 TWR；分母低于中位净值 1% 的日子跳过并计数。XIRR 以隐含资金流计算。\n"
        "回合统计只用完整观察到开仓到清仓的回合；含资金费的净盈亏单列。强平仅指本钱包被强平的成交。\n"
        "手续费 fee 已含 builderFee，不重复扣除。现货按快照价值估值，稳定币按 1 美元。\n", encoding="utf-8")
    zip_path = archive_report(Path(output_dir).resolve())
    if analysis_sink:
        ledger = [ledger_view(row, store.wallet) for row in store.rows("ledger")]
        analysis_sink(engine, ledger, funding, coverage)
    log(f"报告已生成：{zip_path.name} ({zip_path.stat().st_size / 2**20:,.2f} MiB)")
    return {"html": str(path.resolve()), "zip": str(zip_path), "summary": engine["summary"],
            "coverage": coverage, "model_summary": _headline(model)}


def _headline(model):
    perf, risk, rec = model["performance"], model["risk"], model["quality"]["reconciliation"]
    return {"official_all_time_pnl": (model["official"]["windows"].get("allTime") or {}).get("pnl"),
            "equity": model["current"].get("equity"), "twr": perf.get("twr"), "max_drawdown": risk.get("max_drawdown"),
            "sharpe": risk.get("sharpe"), "win_rate": model["behavior"].get("win_rate"),
            "reconciliation": rec.get("status"), "residual_pct": rec.get("residual_pct"),
            "history": model["quality"]["truncation"]["status"]}
