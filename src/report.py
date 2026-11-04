"""Bounded-memory, offline Hyperliquid report builder (Python standard library only).

The HTML shell contains aggregate data and a chunk manifest, never every row.
Each data file calls a tiny local loader with a JSON *string*.  The worker parses
one chunk at a time, keeping the browser's main thread and DOM small.  The .js
transport is deliberate: it works after unzipping and opening file://index.html,
without a local HTTP server or disabling browser security settings.
"""
from __future__ import annotations

import csv
import json
import math
import shutil
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _script_json(value: Any) -> str:
    return _json(value).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (ValueError, InvalidOperation, TypeError):
        return None


def _time(row: dict, dataset: str = "") -> int:
    value = row.get("close_time") or row.get("open_time") if dataset == "campaigns" else row.get("time")
    try:
        number = float(value or 0)
        return int(number) if math.isfinite(number) else 0
    except (TypeError, ValueError, OverflowError):
        return 0


def _readable(value: Any) -> str:
    if value is None:
        return ""
    return _json(value) if isinstance(value, (dict, list)) else str(value)


def _csv_cell(value: Any) -> str:
    text = _readable(value)
    # Keep genuine signed numbers numeric; prevent formula execution for text.
    if text[:1] in ("=", "+", "-", "@", "\t", "\r") and _decimal(text) is None:
        return "'" + text
    return text


def _write_csv(path: Path, rows: list[dict]) -> None:
    fields: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen and not key.startswith("_"):
                fields.append(key)
                seen.add(key)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(fields or ["no_records"])
        for row in rows:
            writer.writerow([_csv_cell(row.get(key)) for key in fields])


def _normalize(row: dict, dataset: str) -> dict:
    """Create a compact display/search view without discarding raw audit fields."""
    out = dict(row)
    delta = row.get("delta") if isinstance(row.get("delta"), dict) else {}
    if dataset in ("ledger", "funding"):
        for key in ("coin", "type", "token", "direction", "counterparty"):
            if not out.get(key) and delta.get(key) is not None:
                out[key] = delta[key]
        if out.get("amount") is None:
            for key in ("usdc", "amount", "sz"):
                if delta.get(key) is not None:
                    out["amount"] = delta[key]
                    break
        if not out.get("token") and delta.get("usdc") is not None:
            out["token"] = out.get("pnl_token") or delta.get("pnl_token") or ("USDC" if ":" not in str(out.get("coin", "")) else "UNKNOWN")
        out.setdefault("source", row.get("_source", ""))
        if dataset == "funding":
            out.setdefault("type", "funding")
            out.setdefault("token", out.get("pnl_token") or ("USDC" if ":" not in str(out.get("coin", "")) else "UNKNOWN"))
            for key in ("fundingRate", "szi"):
                if out.get(key) is None and delta.get(key) is not None:
                    out[key] = delta[key]
    out["_time"] = _time(out, dataset)
    out["_kind"] = str(out.get("action") or out.get("status") or out.get("type") or "unknown")
    out["_coin"] = str(out.get("coin") or out.get("token") or "unknown")
    terms = (
        out.get(key) for key in ("coin", "token", "action", "action_label", "direction", "direction_label", "type", "status", "hash", "oid", "tid", "id", "counterparty", "source", "quality")
    )
    out["_search"] = " ".join(_readable(item) for item in terms if item is not None).lower()
    return out


def _compact_coverage(value: Any, depth: int = 0) -> Any:
    """Do not accidentally embed a full S3 object manifest in the HTML shell."""
    if depth > 4:
        if isinstance(value, (dict, list)):
            return f"{len(value):,} 项；详见 coverage.json"
        return value
    if isinstance(value, dict):
        return {str(k): _compact_coverage(v, depth + 1) for k, v in list(value.items())[:80]}
    if isinstance(value, list):
        if len(value) > 20:
            return {"total_items": len(value), "preview": [_compact_coverage(x, depth + 1) for x in value[:5]], "detail_file": "exports/coverage.json"}
        return [_compact_coverage(x, depth + 1) for x in value]
    if isinstance(value, str) and len(value) > 1200:
        return value[:1200] + "…（详见 coverage.json）"
    return value


def _chart(operations: list[dict], points: int | None = None) -> dict:
    """Compatibility summary: keep every operation; never aggregate by day."""
    result = []
    cumulative = Decimal(0)
    unknown = 0
    for row in operations:
        if row.get("market_type") == "spot" or row.get("direction") == "spot":
            continue
        token = row.get("pnl_token") or ("USDC" if ":" not in str(row.get("coin", "")) else None)
        if token != "USDC":
            continue
        pnl = _decimal(row.get("closed_pnl"))
        if pnl is None:
            unknown += 1
            continue
        stamp = _time(row)
        if stamp:
            cumulative += pnl
            result.append([stamp, str(cumulative)])
    return {"points": result, "unknown": unknown, "basis": "USDC 合约成交已实现盈亏，未扣手续费与资金费"}


def _stats(analysis: dict) -> dict:
    operations = analysis.get("operations", [])
    campaigns = analysis.get("campaigns", [])
    complete = [row for row in campaigns if row.get("status") == "closed" and row.get("complete") is True]
    pnl = Decimal(0)
    unknown = 0
    included = 0
    excluded = 0
    for row in operations:
        if row.get("market_type") == "spot" or row.get("direction") == "spot":
            continue
        token = row.get("pnl_token") or ("USDC" if ":" not in str(row.get("coin", "")) else None)
        if token != "USDC":
            excluded += 1
            continue
        included += 1
        value = _decimal(row.get("closed_pnl"))
        if value is None:
            unknown += 1
        else:
            pnl += value
    # Fee-net wins only where the engine can produce a comparable USDC result.
    comparable = [_decimal(row.get("net_pnl")) for row in complete]
    comparable = [value for value in comparable if value is not None]
    wins = sum(value > 0 for value in comparable)
    losses = sum(value < 0 for value in comparable)
    return {
        "operation_count": len(operations),
        "complete_campaigns": len(complete),
        "incomplete_campaigns": sum(row.get("history_complete") is False or row.get("status") == "interrupted" or (row.get("status") == "closed" and row.get("complete") is not True) for row in campaigns),
        "open_campaigns": sum(row.get("status") == "open" for row in campaigns),
        "closed_pnl": str(pnl) if included and included > unknown else None,
        "pnl_unknown_rows": unknown,
        "pnl_other_token_rows": excluded,
        "wins": wins,
        "losses": losses,
        "comparable_campaigns": len(comparable),
        "win_rate": str(Decimal(wins) / len(comparable) * 100) if comparable else None,
        "basis": "胜率仅含完整已平仓且净盈亏可比较的生命周期；未含资金费。",
    }


def _browser_curves(curves):
    """Pack chart rows once; exact Decimal strings remain in all exports.

    The unified timeline already contains cash, ratios, exposure and PnL.
    Shipping three duplicate object arrays would make large wallets needlessly
    expensive to open. Column names and quality sets are shared, and numeric
    chart values use the same Number precision the canvas already uses.
    """
    out = {key: curves.get(key) for key in ("version", "basis", "warnings", "summary")}
    has_timeline = bool(curves.get("timeline_curve"))
    names = ("timeline_curve", "equity_curve", "leverage_curve") if has_timeline else (
        "event_curve", "equity_curve", "leverage_curve", "balance_curve", "balance_leverage_curve")
    quality_sets, quality_ids = [], {}
    tables = {}
    aliases = {"balance": "balance_usdc", "observed_balance": "observed_balance_usdc",
               "delta": "delta_usdc", "kind": "event_type"}
    identifiers = {"id", "oid", "tid", "hash", "wallet", "operation_id", "fill_id", "event_id", "coin", "action", "direction", "fee_token",
                   "position_entry_source", "anchor_source", "source", "scope", "ledger_type",
                   "denominator_scope", "event_type", "equity_source", "perp_source", "account_source"}
    for name in names:
        rows = curves.get(name, [])
        present = {key for row in rows for key in row}
        redundant = {key for key, canonical in aliases.items() if canonical in present}
        fields = list(dict.fromkeys(key for row in rows for key in row
                                    if key not in redundant and not key.startswith("_")))
        packed = []
        for row in rows:
            values = []
            for key in fields:
                value = row.get(key)
                if key == "quality":
                    flags = tuple(value or [])
                    if flags not in quality_ids:
                        quality_ids[flags] = len(quality_sets)
                        quality_sets.append(list(flags))
                    value = quality_ids[flags]
                elif isinstance(value, str) and key not in identifiers:
                    number = _decimal(value)
                    if number is not None:
                        view = float(number)
                        value = view if math.isfinite(view) else None
                values.append(value)
            packed.append(values)
        tables[name] = {"fields": fields, "rows": packed}
    out.update(tables=tables, quality_sets=quality_sets, unified=has_timeline)
    return out


def archive_report(directory):
    """Archive a generated report, omitting temporary filesystem write copies."""
    directory = Path(directory).resolve()
    destination = directory.parent / (directory.name + ".zip")
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(directory.rglob("*")):
            relative = path.relative_to(directory)
            if path.is_file() and not any(part.startswith(".") for part in relative.parts):
                archive.write(path, Path(directory.name) / relative)
    return destination


def build_report(output_dir, analysis, ledger, funding, coverage, wallet, *, chunk_size=1000, curves=None) -> Path:
    """Write an offline report folder and return its ``index.html`` path.

    ``analysis`` comes from engine.analyze_fills. ``ledger`` and ``funding`` are
    sequences of API-shaped dictionaries; normalized display fields are optional.
    The report performs no network requests. Existing unrelated output files are
    preserved; only its own data and export subdirectories are replaced.
    """
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    chunk_size = max(100, min(2000, int(chunk_size)))
    data_dir = output / "data"
    export_dir = output / "exports"
    for directory in (data_dir, export_dir):
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir()
    if curves is None:
        from curves import build_curves
        curves = build_curves(analysis.get("operations", []))
    # The full point data lives beside the HTML. Loading local scripts also
    # works under file://, unlike fetch() in many browsers.
    packed = _browser_curves(curves)
    (data_dir / "curves.js").write_text(
        "window.__HL_CURVES__=(()=>{const p=JSON.parse(" + _script_json(_json(packed)) + ");"
        "for(const [name,t] of Object.entries(p.tables)){p[name]=t.rows.map(v=>{const r={};"
        "t.fields.forEach((k,i)=>{r[k]=k==='quality'?p.quality_sets[v[i]]:v[i]});return r})}"
        "if(p.unified){p.event_curve=p.timeline_curve.filter(r=>r.event_sequence!=null||r.action==='boundary');"
        "p.balance_curve=p.timeline_curve;p.balance_leverage_curve=p.timeline_curve;}"
        "delete p.tables;delete p.quality_sets;delete p.unified;return p})();\n", encoding="utf-8")
    # Keep analytics local too: file:// reports need no CDN or extra dependency.
    shutil.copyfile(Path(__file__).with_name("chart_insights.js"), data_dir / "chart_insights.js")
    for name in ("event_curve", "equity_curve", "leverage_curve", "timeline_curve",
                 "balance_curve", "balance_leverage_curve", "cash_flow_events"):
        _write_csv(export_dir / (name + ".csv"), curves.get(name, []))
    (export_dir / "curves.json").write_text(_json(curves), encoding="utf-8")
    datasets = {
        "operations": list(analysis.get("operations", [])),
        "campaigns": list(analysis.get("campaigns", [])),
        "ledger": list(ledger or []),
        "funding": list(funding or []),
    }
    manifests = {}
    for name, source_rows in datasets.items():
        # Sort indices rather than keep a second fully normalized data copy.
        order = sorted(range(len(source_rows)), key=lambda i: (_time(source_rows[i], name), i), reverse=True)
        chunks = []
        all_coins: set[str] = set()
        all_kinds: set[str] = set()
        for start in range(0, len(order), chunk_size):
            chunk_id = f"{name}:{len(chunks)}"
            rows = [_normalize(source_rows[index], name) for index in order[start : start + chunk_size]]
            coins = sorted({row["_coin"] for row in rows})
            kinds = sorted({row["_kind"] for row in rows})
            all_coins.update(coins)
            all_kinds.update(kinds)
            path = f"data/{name}_{len(chunks):06d}.js"
            # Double JSON encoding avoids parsing large object literals on UI thread.
            (output / path).write_text(f"window.__HL_RECEIVE__({_script_json(chunk_id)},{_script_json(_json(rows))});\n", encoding="utf-8")
            timestamps = [row["_time"] for row in rows]
            chunks.append({"id": chunk_id, "path": path, "count": len(rows), "minTime": min(timestamps), "maxTime": max(timestamps), "coins": coins if len(coins) <= 64 else None, "kinds": kinds})
        csv_path = f"exports/{name}.csv"
        _write_csv(output / csv_path, source_rows)
        manifests[name] = {"count": len(source_rows), "chunks": chunks, "coins": sorted(all_coins), "kinds": sorted(all_kinds), "csv": csv_path}
    (export_dir / "coverage.json").write_text(json.dumps(coverage, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (export_dir / "summary.json").write_text(json.dumps(analysis.get("summary", {}), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    warning_values = list(analysis.get("warnings", []))
    warning_values.extend(curves.get("warnings", []))
    if isinstance(coverage, dict) and isinstance(coverage.get("warnings"), list):
        warning_values.extend(coverage["warnings"])
    # Keep no more than 30 preview warnings; full inputs live in the audit files.
    warnings = list(dict.fromkeys(_readable(w) for w in warning_values))
    (export_dir / "warnings.json").write_text(json.dumps(warnings, ensure_ascii=False, indent=2), encoding="utf-8")
    model = {
        "version": "2.3.2",
        "wallet": str(wallet),
        "generated": datetime.now(timezone.utc).isoformat(),
        "datasets": manifests,
        "stats": _stats(analysis),
        "chart": {"path": "data/curves.js", "summary": curves.get("summary", {})},
        "summary": _compact_coverage(analysis.get("summary", {})),
        "coverage": _compact_coverage(coverage),
        "warnings": warnings[:30],
        "warningCount": len(warnings),
    }
    template = Path(__file__).with_name("report_template.html").read_text(encoding="utf-8")
    (output / "index.html").write_text(template.replace("__REPORT_MODEL_JSON__", _script_json(model)), encoding="utf-8")
    (output / "打开报告.txt").write_text(
        "请完整解压报告压缩包，再用 Chrome、Edge、Firefox 或 Safari 打开 index.html。\n"
        "请保留同目录下 data 和 exports 文件夹；只复制 index.html 会缺少明细。\n"
        "报告完全离线，不需要安装服务或联网。浏览器一次只显示 50 条，最多 100 条。\n"
        "首次跨全部记录搜索需逐片扫描，进度条会显示状态；后续翻页只载入相关分片。\n"
        "CSV 包含全部已抓取记录，未受页面筛选影响。原始精度与审计字段保留在 CSV。\n"
        "覆盖范围、来源缺口与未知盈亏请查看页面“数据覆盖”和 exports/coverage.json。\n",
        encoding="utf-8",
    )
    return output / "index.html"
