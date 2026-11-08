"""Offline wallet report: Tabler layout, ECharts charts, Tabulator tables.

The folder opens from file:// with no network. Data is shipped as local
<script> files (``fetch`` is blocked for file:// in most browsers):

    index.html            template with the summary model inlined
    assets/vendor/*       pinned third-party files (sha256 in manifest.json)
    assets/app.js|css     report application
    data/<table>_N.js     columnar table chunks, loaded on demand
    exports/*.csv|json    full data for spreadsheets and audits
"""
from __future__ import annotations

import csv
import hashlib
import json
import shutil
import zipfile
from decimal import Decimal, InvalidOperation
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENDOR = HERE.parent / "vendor"
TABLE_COLUMNS = {
    "operations": ("time", "coin", "action_label", "direction_label", "side", "price", "size", "notional",
                   "start_position", "end_position", "closed_pnl", "fee", "fee_token", "maker", "twap",
                   "liquidation", "dir_raw", "oid", "tid", "hash", "source", "quality"),
    "campaigns": ("open_time", "close_time", "coin", "direction_label", "status", "complete", "max_size",
                  "observed_entry_notional", "observed_exit_notional", "closed_pnl", "net_pnl", "funding",
                  "net_pnl_with_funding", "duration_ms", "fill_count", "order_count", "ended_by",
                  "maker_notional", "taker_notional", "quality"),
    "flows": ("time", "class_label", "type", "token", "amount", "amount_usd", "fee_usd", "approx_value",
              "counterparty", "vault", "hash"),
    "funding": ("time", "coin", "usdc", "szi", "fundingRate", "pnl_token"),
}


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str, allow_nan=False)


def _script_json(value):
    return (_json(value).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def _decimal(value):
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _csv_cell(value):
    if value is None:
        return ""
    text = _json(value) if isinstance(value, (dict, list)) else str(value)
    # Keep genuine signed numbers numeric; prevent formula execution for text.
    if text[:1] in ("=", "+", "-", "@", "\t", "\r") and _decimal(text) is None:
        return "'" + text
    return text


def write_csv(path, rows):
    fields, seen = [], set()
    for row in rows:
        for key in row:
            if key not in seen and not key.startswith("_"):
                fields.append(key)
                seen.add(key)
    with Path(path).open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(fields or ["no_records"])
        for row in rows:
            writer.writerow([_csv_cell(row.get(key)) for key in fields])


def archive_report(directory):
    """Zip a report folder, keeping the folder name as the top-level entry."""
    directory = Path(directory).resolve()
    destination = directory.parent / (directory.name + ".zip")
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(directory.rglob("*")):
            relative = path.relative_to(directory)
            if path.is_file() and not any(part.startswith(".") for part in relative.parts):
                archive.write(path, Path(directory.name) / relative)
    return destination


def _cell(value):
    if isinstance(value, float) and value != value:
        return None
    if isinstance(value, (list, dict)):
        return _json(value) if value else None
    return value


def _funding_rows(funding):
    out = []
    for row in funding:
        d = row.get("delta") or {}
        out.append({"time": row.get("time"), "coin": d.get("coin"), "usdc": d.get("usdc"), "szi": d.get("szi"),
                    "fundingRate": d.get("fundingRate"), "pnl_token": row.get("pnl_token")})
    return out


def copy_vendor(target):
    manifest = json.loads((VENDOR / "manifest.json").read_text(encoding="utf-8"))
    target.mkdir(parents=True, exist_ok=True)
    for name, item in manifest["files"].items():
        data = (VENDOR / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise ValueError(f"前端库 {name} 校验失败；请运行 python tools/vendor_fetch.py --force")
        (target / name).write_bytes(data)
    return manifest


def build_report(output_dir, model, engine, funding, *, chunk_size=5000, title=None):
    """Write the report folder and return the ``index.html`` path."""
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    for name in ("data", "exports", "assets"):
        if (output / name).exists():
            shutil.rmtree(output / name)
        (output / name).mkdir()
    copy_vendor(output / "assets" / "vendor")
    for name in ("app.js", "app.css"):
        shutil.copyfile(HERE / "assets" / name, output / "assets" / name)

    tables = {
        "operations": sorted(engine["operations"], key=lambda r: -(r.get("time") or 0)),
        "campaigns": sorted(engine["campaigns"], key=lambda r: -(r.get("close_time") or r.get("open_time") or 0)),
        "flows": sorted(model["flows"]["rows"], key=lambda r: -(r.get("time") or 0)),
        "funding": sorted(_funding_rows(funding), key=lambda r: -(r.get("time") or 0)),
    }
    manifest = {}
    for name, rows in tables.items():
        fields = TABLE_COLUMNS[name]
        chunks = []
        for start in range(0, len(rows), chunk_size):
            part = [[_cell(row.get(f)) for f in fields] for row in rows[start:start + chunk_size]]
            path = f"data/{name}_{len(chunks):04d}.js"
            (output / path).write_text(
                f"HL.receive({_script_json(name)},{len(chunks)},JSON.parse({_script_json(_json(part))}));\n",
                encoding="utf-8")
            chunks.append(path)
        write_csv(output / "exports" / f"{name}.csv", rows)
        manifest[name] = {"fields": list(fields), "count": len(rows), "chunks": chunks, "csv": f"exports/{name}.csv"}

    page_model = {k: v for k, v in model.items() if k != "flows"}
    page_model["flows"] = {k: v for k, v in model["flows"].items() if k != "rows"}
    page_model["tables"] = manifest
    page_model["title"] = title
    for name in ("series", "mtm"):
        write_csv(output / "exports" / f"{name}_days.csv", model[name]["days"])
    write_csv(output / "exports" / "reconciliation_intervals.csv", model["quality"]["reconciliation_intervals"])
    write_csv(output / "exports" / "monthly_returns.csv", model["performance"].get("monthly", []))
    (output / "exports" / "model.json").write_text(json.dumps(page_model, ensure_ascii=False, indent=1, default=str),
                                                    encoding="utf-8")
    template = (HERE / "templates" / "index.html").read_text(encoding="utf-8")
    html = template.replace("__HL_MODEL__", _script_json(page_model))
    html = html.replace("__HL_TITLE__", (title or "Hyperliquid 钱包分析").replace("<", "&lt;"))
    (output / "index.html").write_text(html, encoding="utf-8")
    (output / "打开报告.txt").write_text(
        "完整解压后，用 Chrome / Edge / Firefox / Safari 打开 index.html。\n"
        "请保留同目录下 assets、data、exports 文件夹；报告完全离线，不访问网络。\n"
        "exports/ 中的 CSV 含全部记录；model.json 是页面使用的全部指标与对账结果。\n", encoding="utf-8")
    return output / "index.html"
