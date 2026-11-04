"""Read-only HyperCore history collection, resumable S3 scan, and explicit coverage.

Sources (checked 2026-11-02):
https://hyperliquid.gitbook.io/hyperliquid-docs/historical-data
https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint
https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/nodes/l1-data-schemas

An exhausted API query or fully scanned S3 inventory is NOT proof of wallet-lifetime
completeness. Unknown target events and failed objects are retained and reported.
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import math
import re
import sqlite3
import time
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

VERSION = "2.3.0-local"
INFO_URL = "https://api.hyperliquid.xyz/info"
S3_BUCKET = "hl-mainnet-node-data"
S3_PREFIXES = ("node_fills_by_block", "node_fills", "node_trades", "misc_events_by_block")
HOUR_MS = 3_600_000
ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
SOURCES = [
    {"title": "官方历史数据", "url": "https://hyperliquid.gitbook.io/hyperliquid-docs/historical-data"},
    {"title": "官方 Info API", "url": "https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint"},
    {"title": "资金流水 API", "url": "https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals"},
    {"title": "节点数据格式", "url": "https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/nodes/l1-data-schemas"},
    {"title": "节点输出格式", "url": "https://github.com/hyperliquid-dex/node"},
]


def js(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str, allow_nan=False)


def digest(value):
    return hashlib.sha256(js(value).encode()).hexdigest()


def wallet_address(value):
    value = str(value).strip()
    if not ADDR.fullmatch(value):
        raise ValueError("请输入实际账户的 0x + 40 位十六进制地址，不是私钥或 API agent 地址。")
    return value.lower()


def timestamp_ms(value):
    """API integers are milliseconds; explicit ISO times are UTC if no zone is given."""
    if value is None or value == "":
        raise ValueError("缺少时间字段")
    if isinstance(value, (int, float, Decimal)) or re.fullmatch(r"[+-]?\d+(?:\.\d+)?", str(value).strip()):
        return int(Decimal(str(value).strip()))
    parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def date_ms(value, default=0):
    return timestamp_ms(value) if str(value).strip() else default


def decimal_key(value):
    if value is None or value == "":
        return None
    try:
        d = Decimal(str(value).strip())
        if not d.is_finite():
            return None
        if d.is_zero():
            return "0"
        # Decimal.normalize() obeys the current precision and can round long IDs
        # or amounts. Formatting then trimming decimal zeroes is exact.
        text = format(d, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    except (InvalidOperation, ValueError):
        return str(value)


def canonical(value):
    """Normalize equivalent API/archive decimal strings without rounding numbers."""
    if isinstance(value, dict):
        return {k: canonical(v) for k, v in sorted(value.items()) if not k.startswith("_") and v is not None}
    if isinstance(value, list):
        return [canonical(v) for v in value]
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return decimal_key(value)
    if isinstance(value, str):
        s = value.strip()
        if re.fullmatch(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", s):
            return decimal_key(s)
        return s.lower() if re.fullmatch(r"0x[0-9a-fA-F]+", s) else s
    return value


def fill_semantic_key(f):
    # Includes startPosition: identical partial fills on the same order can be distinct.
    return digest([f.get("coin"), f.get("time"), f.get("side"), str(f.get("oid", "")),
                   decimal_key(f.get("px")), decimal_key(f.get("sz")),
                   decimal_key(f.get("startPosition")), str(f.get("hash") or "").strip().lower()])


def record_key(kind, row):
    if kind == "fills":
        if row.get("tid") is not None:
            return digest([row.get("coin"), str(row["tid"]), row.get("side"), str(row.get("oid", ""))])
        return "fallback:" + fill_semantic_key(row)
    if kind in ("ledger", "funding"):
        delta = row.get("delta", {})
        if kind == "funding":
            # Owner metadata occurs in some archive wrappers but not in the API.
            delta = {k: v for k, v in delta.items() if k not in ("user", "users")}
        return digest([row.get("time"), str(row.get("hash") or "").strip().lower(), canonical(delta)])
    # Public display provenance is not part of the underlying event identity.
    return digest(canonical({k: v for k, v in row.items() if k != "source"}))


def source_rank(source):
    if source.startswith("api:"):
        return 50
    if source.startswith("s3:node_fills"):
        return 40
    if source.startswith("s3:misc_events"):
        return 35
    if source.startswith("import:"):
        return 30
    return 10


class HistoryStore:
    """One wallet per database; only target-wallet records, never all-market fills."""

    def __init__(self, path, wallet):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.wallet = wallet_address(wallet)
        self.db = sqlite3.connect(self.path)
        # DELETE journal works more predictably than WAL on a mounted Colab Drive.
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS records(
            kind TEXT NOT NULL, id TEXT NOT NULL, time INTEGER, coin TEXT,
            payload TEXT NOT NULL, source TEXT NOT NULL, priority INTEGER,
            semantic_key TEXT, PRIMARY KEY(kind,id));
          CREATE INDEX IF NOT EXISTS rec_time ON records(kind,time);
          CREATE INDEX IF NOT EXISTS rec_semantic ON records(kind,semantic_key);
          CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS objects(
            bucket TEXT,key TEXT,etag TEXT,size INTEGER,hour_ms INTEGER,prefix TEXT,
            status TEXT,stats TEXT,PRIMARY KEY(bucket,key));
        """)
        old = self.get_meta("wallet")
        if old and old != self.wallet:
            self.db.close()
            raise ValueError("该缓存属于另一钱包。请选择不同 CACHE_DIR，避免混入数据。")
        self.set_meta("wallet", self.wallet)
        self.set_meta("version", VERSION)
        self.commit()

    def set_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, js(value)))

    def get_meta(self, key, default=None):
        row = self.db.execute("SELECT payload FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, kind, row, source):
        row = dict(row)
        row["time"] = timestamp_ms(row.get("time"))
        row["_source"] = source
        row["source"] = source
        if row.get("feeToken"):
            row["feeToken"] = str(row["feeToken"]).strip()
        key = record_key(kind, row)
        semantic = fill_semantic_key(row) if kind == "fills" else key
        old = self.db.execute("SELECT id,payload,priority FROM records WHERE kind=? AND id=?", (kind, key)).fetchone()
        if old is None and kind == "fills":
            # Bridge a legacy trade without tid to the richer equivalent fill.
            candidates = self.db.execute("SELECT id,payload,priority FROM records WHERE kind=? AND semantic_key=?", (kind, semantic)).fetchall()
            compatible = [r for r in candidates if row.get("tid") is None or json.loads(r[1]).get("tid") in (None, row.get("tid"))]
            if len(compatible) == 1:
                old = compatible[0]
        priority = source_rank(source)
        if old:
            prev = json.loads(old[1])
            prefer_new = priority >= old[2]
            merged = dict(prev)
            for k, v in row.items():
                if v is not None and (prefer_new or merged.get(k) is None):
                    merged[k] = v
            merged["_sources"] = sorted(set(prev.get("_sources", [prev.get("_source", "")])) | {source})
            if not prefer_new:
                source, priority = prev.get("_source", source), old[2]
            # Retain an existing strong key when adding an inferior legacy copy.
            if row.get("tid") is None and prev.get("tid") is not None:
                key = old[0]
            if old[0] != key:
                self.db.execute("DELETE FROM records WHERE kind=? AND id=?", (kind, old[0]))
            row = merged
        row["_source"] = source
        row["source"] = source
        row["_record_id"] = key
        if row.get("_quality") == "legacy_trade_missing_pnl_fee" and all(row.get(k) is not None for k in ("closedPnl", "fee", "feeToken")):
            row.pop("_quality", None)
        if row.get("_quality") == "import_missing_start_position" and row.get("startPosition") is not None:
            row.pop("_quality", None)
        coin = row.get("coin") or row.get("delta", {}).get("coin")
        self.db.execute("INSERT OR REPLACE INTO records VALUES(?,?,?,?,?,?,?,?)",
                        (kind, key, row["time"], coin, js(row), source, priority, semantic))
        return old is None

    def rows(self, kind, start=0, end=2**63 - 1):
        for row in self.db.execute("SELECT payload FROM records WHERE kind=? AND time BETWEEN ? AND ? ORDER BY time,id", (kind, start, end)):
            yield json.loads(row[0])

    def counts(self, start=0, end=2**63 - 1):
        return dict(self.db.execute("SELECT kind,count(*) FROM records WHERE time BETWEEN ? AND ? GROUP BY kind", (start, end)))

    def register_object(self, obj):
        old = self.db.execute("SELECT etag,size,status FROM objects WHERE bucket=? AND key=?", (obj["bucket"], obj["key"])).fetchone()
        if old and old[0] == obj["etag"] and old[1] == obj["size"]:
            return old[2]
        self.db.execute("INSERT OR REPLACE INTO objects VALUES(?,?,?,?,?,?,?,?)",
                        (obj["bucket"], obj["key"], obj["etag"], obj["size"], obj["hour_ms"], obj["prefix"], "pending", "{}"))
        return "pending"

    def finish_object(self, obj, status, stats):
        self.db.execute("UPDATE objects SET status=?,stats=? WHERE bucket=? AND key=?", (status, js(stats), obj["bucket"], obj["key"]))
        self.commit()

    def object_status(self, obj):
        row = self.db.execute("SELECT status FROM objects WHERE bucket=? AND key=? AND etag=? AND size=?",
                              (obj["bucket"], obj["key"], obj["etag"], obj["size"])).fetchone()
        return row[0] if row else "pending"

    def commit(self):
        self.db.commit()

    def close(self):
        self.commit()
        self.db.close()


class InfoClient:
    """Public /info only: no signer, no trading endpoints, bounded exponential retries."""

    def __init__(self, url=INFO_URL, timeout=45, retries=5, throttle=True):
        import requests
        self.session = requests.Session()
        self.url, self.timeout, self.retries = url, timeout, retries
        self.next_request = 0.0
        self.throttle = throttle

    def post(self, body):
        import requests
        if self.throttle:
            time.sleep(max(0, self.next_request - time.monotonic()))
        for attempt in range(self.retries):
            try:
                r = self.session.post(self.url, json=body, timeout=self.timeout)
                if r.status_code == 429 or r.status_code >= 500:
                    if attempt == self.retries - 1:
                        r.raise_for_status()
                    delay = min(30.0, 2 ** (attempt + 1))
                    try:
                        delay = min(30.0, max(delay, float(r.headers.get("Retry-After", 0))))
                    except ValueError:
                        pass
                    time.sleep(delay)
                    continue
                r.raise_for_status()
                out = r.json(parse_float=Decimal)
                if isinstance(out, dict) and ("error" in out or out.get("status") == "err"):
                    raise RuntimeError("Info API 返回错误: " + str(out)[:300])
                if isinstance(out, str) and body.get("type") != "userAbstraction":
                    raise RuntimeError("Info API 返回错误字符串: " + out[:300])
                # Aim below the documented 1200 weighted requests per minute.
                n = len(out) if isinstance(out, list) else 0
                self.next_request = time.monotonic() + (20 + math.ceil(n / 20)) * 60 / 1000
                return out
            except (requests.Timeout, requests.ConnectionError):
                if attempt == self.retries - 1:
                    raise
                time.sleep(min(30, 2 ** (attempt + 1)))
        raise RuntimeError("API 重试耗尽")


def api_rows(client, endpoint, wallet, start, end, cap, on_page=None, max_pages=200_000):
    """Inclusive boundary re-query. A saturated millisecond is flagged, never hidden."""
    cursor = start
    stats = {"endpoint": endpoint, "start_ms": start, "end_ms": end, "pages": 0,
             "received_rows": 0, "exhausted": False, "gaps": [], "first_ms": None, "last_ms": None}
    for _ in range(max_pages):
        if cursor > end:
            stats["exhausted"] = True
            break
        body = {"type": endpoint, "user": wallet, "startTime": cursor, "endTime": end}
        if endpoint == "userFillsByTime":
            body["aggregateByTime"] = False
        response = client.post(body)
        if not isinstance(response, list):
            raise ValueError(f"{endpoint}: 预期数组，收到 {type(response).__name__}")
        stats["pages"] += 1
        if not response:
            stats["exhausted"] = True
            break
        rows = []
        for item in response:
            if not isinstance(item, dict):
                raise ValueError(f"{endpoint}: 未识别的记录结构")
            row = dict(item.get("fill", item))
            if "fill" in item:
                row["twapId"] = item.get("twapId")
            row["time"] = timestamp_ms(row.get("time"))
            if not cursor <= row["time"] <= end:
                raise ValueError(f"{endpoint}: 返回时间超出请求区间，停止避免跳过记录")
            rows.append(row)
        stats["received_rows"] += len(rows)
        lo, hi = min(r["time"] for r in rows), max(r["time"] for r in rows)
        stats["first_ms"] = lo if stats["first_ms"] is None else min(stats["first_ms"], lo)
        stats["last_ms"] = hi
        if on_page:
            on_page(rows)
        if hi == cursor:
            if len(rows) >= cap:
                stats["gaps"].append({"time": cursor, "reason": "同一毫秒响应饱和；API 无 offset，需归档核对", "rows": len(rows)})
            cursor += 1  # Only after an inclusive request has isolated this boundary.
        else:
            cursor = hi
    else:
        stats["gaps"].append({"reason": "达到分页保护上限，未完成"})
    return stats


def collect_api(store, start=0, end=None, *, client=None, incremental=True, log=print, cancel_check=None):
    end = int(time.time() * 1000) if end is None else int(end)
    client = client or InfoClient()
    results = []
    endpoints = [("userFillsByTime", "fills", 2000), ("userTwapSliceFillsByTime", "fills", 2000),
                 ("userNonFundingLedgerUpdates", "ledger", 500), ("userFunding", "funding", 500)]
    for endpoint, kind, cap in endpoints:
        if cancel_check:
            cancel_check()
        old = store.get_meta("api:" + endpoint, {})
        query_start = start
        if (incremental and old.get("exhausted") and not old.get("gaps")
                and old.get("requested_start_ms", start + 1) <= start <= old.get("end_ms", -1)
                and old.get("end_ms", end + 1) <= end):
            query_start = max(start, int(old["end_ms"]) - 86_400_000)
        log(f"读取 {endpoint} …")
        added = [0]

        def save(rows):
            for index, row in enumerate(rows):
                if cancel_check and index % 250 == 0:
                    cancel_check()
                if kind in ("ledger", "funding") and not isinstance(row.get("delta"), dict):
                    raise ValueError("资金流水缺少 delta，不能安全归类")
                added[0] += store.put(kind, row, "api:" + endpoint)
            store.commit()

        try:
            stats = api_rows(client, endpoint, store.wallet, query_start, end, cap, save)
            stats["new_rows"] = added[0]
            # This watermark describes one continuously queried interval. Do not
            # join disjoint historical requests and silently skip the gap later.
            stats["requested_start_ms"] = start
            stats["incremental"] = query_start > start
            stats["status"] = "api_visible_exhausted" if stats["exhausted"] else "partial"
            store.set_meta("api:" + endpoint, stats)
        except Exception as exc:
            stats = {"endpoint": endpoint, "status": "failed", "error": str(exc)[:500], "new_rows": added[0], "exhausted": False}
            if endpoint == "userTwapSliceFillsByTime":
                # The time endpoint is named in rate-limit docs, but its retention
                # contract is not documented. Fall back explicitly to recent 2000.
                try:
                    response = client.post({"type": "userTwapSliceFills", "user": store.wallet})
                    if not isinstance(response, list):
                        raise ValueError("TWAP 响应不是数组")
                    count = 0
                    for item in response:
                        f = dict(item["fill"])
                        f["twapId"] = item.get("twapId")
                        if start <= timestamp_ms(f["time"]) <= end:
                            count += store.put("fills", f, "api:userTwapSliceFills")
                    stats.update(status="recent_twap_fallback", fallback_new_rows=count,
                                 warning="按时间 TWAP 请求失败；回退到最近最多 2000 条，历史仍需归档。")
                except Exception as fallback_exc:
                    stats["fallback_error"] = str(fallback_exc)[:500]
            # Do not advance a cached watermark after failed/partial collection.
        results.append(stats)
        store.commit()
        log(f"  {stats['status']}，新增 {stats.get('new_rows', 0):,} 条")
    store.set_meta("last_api_run", {"start_ms": start, "end_ms": end, "results": results})
    store.commit()
    return results


def fetch_asset_meta(client, fills, store=None, log=print):
    """Resolve actual collateral tokens; do not treat every HIP-3 PnL as USDC."""
    out, errors = {}, []
    coins = {f.get("coin", "") for f in fills}
    try:
        spot = client.post({"type": "spotMeta"})
        tokens = {t["index"]: t["name"] for t in spot.get("tokens", [])}
        for pair in spot.get("universe", []):
            ids = pair.get("tokens", [])
            meta = {"market_type": "spot", "display_name": "/".join(tokens.get(i, str(i)) for i in ids),
                    "quote_token": tokens.get(ids[1]) if len(ids) > 1 else None}
            out[pair["name"]] = meta
            out["@" + str(pair["index"])] = meta
    except Exception as exc:
        tokens = {0: "USDC"}
        errors.append("spotMeta: " + str(exc)[:200])
    dexes = sorted({c.split(":", 1)[0] if ":" in c else "" for c in coins if not c.startswith("@") and "/" not in c})
    for dex in dexes:
        try:
            payload = {"type": "metaAndAssetCtxs"}
            if dex:
                payload["dex"] = dex
            response = client.post(payload)
            meta = response[0]
            collateral = tokens.get(meta.get("collateralToken", 0 if not dex else None))
            for asset in meta.get("universe", []):
                name = asset["name"]
                if dex and ":" not in name:
                    name = dex + ":" + name
                out[name] = {"market_type": "perp", "pnl_token": collateral, "sz_decimals": asset.get("szDecimals")}
        except Exception as exc:
            errors.append("meta " + (dex or "native") + ": " + str(exc)[:200])
    if store:
        store.set_meta("asset_meta", out)
        store.set_meta("asset_meta_errors", errors)
        store.commit()
    if errors:
        log("部分市场元数据不可用：相关币种的净收益不会强行折算为 USDC。")
    return out


def has_wallet(value, wallet):
    if isinstance(value, str):
        return value.lower() == wallet
    if isinstance(value, dict):
        return any(has_wallet(k, wallet) or has_wallet(v, wallet) for k, v in value.items())
    if isinstance(value, list):
        return any(has_wallet(v, wallet) for v in value)
    return False


def untag(value):
    if not isinstance(value, dict):
        return None, value
    if isinstance(value.get("type"), str):
        return value["type"], {k: v for k, v in value.items() if k != "type"}
    if len(value) == 1:
        tag, body = next(iter(value.items()))
        if isinstance(body, (dict, list)):
            return tag, body
    return None, value


def delta_normalize(delta):
    tag, body = untag(delta)
    if not isinstance(body, dict):
        raise ValueError("资金 delta 不是对象")
    out = dict(body)
    if tag:
        compact = tag.replace("_", "").lower()
        tags = {s.lower(): s for s in ["withdraw", "deposit", "internalTransfer", "subAccountTransfer", "spotTransfer",
                "accountClassTransfer", "perpDexClassTransfer", "vaultDeposit", "vaultWithdraw", "vaultCreate",
                "vaultDistribution", "vaultLeaderCommission", "liquidation", "rewardsClaim", "spotGenesis",
                "accountActivationGas", "deployGasAuction", "funding"]}
        out["type"] = tags.get(compact, tag[0].lower() + tag[1:])
    else:
        out["type"] = "unknown"
    return out


def parse_archive_record(record, wallet, context=None):
    """Yield (kind,row); unknown wallet-bearing records stay in raw_events."""
    context = dict(context or {})
    if isinstance(record, dict) and isinstance(record.get("events"), list):
        for i, event in enumerate(record["events"]):
            ctx = {**context, "_block_number": record.get("block_number"), "_event_index": i,
                   "_block_time": record.get("block_time")}
            yield from parse_archive_record(event, wallet, ctx)
        return
    if isinstance(record, list):
        paired_owner = (len(record) == 2 and isinstance(record[0], str)
                        and ADDR.fullmatch(record[0]))
        if paired_owner and record[0].lower() != wallet:
            # A builder/validator/counterparty mention inside another owner's
            # fill must not be turned into the target account's own fill.
            return
        if paired_owner and isinstance(record[1], dict):
            body = dict(record[1])
            if all(k in body for k in ("coin", "px", "sz", "side")):
                yield "fills", {**body, **context}
            elif isinstance(body.get("delta"), dict):
                d = delta_normalize(body["delta"])
                yield ("funding" if d.get("type") == "funding" else "ledger"), {**body, "delta": d, **context}
            elif all(k in body for k in ("coin", "usdc", "szi")):
                yield "funding", {"time": body.get("time", context.get("_block_time")), "hash": body.get("hash"),
                                  "delta": {"type": "funding", **body}, **context}
            else:
                yield "raw_events", {"time": body.get("time", context.get("_block_time", 0)), "raw": record,
                                     "reason": "未识别的钱包配对事件", **context}
        elif has_wallet(record, wallet):
            yield "raw_events", {"time": context.get("_block_time") or 0, "raw": record,
                                 "reason": "未识别的钱包数组事件", **context}
        return
    if not isinstance(record, dict) or not has_wallet(record, wallet):
        return
    if isinstance(record.get("side_info"), list):
        for i, side in enumerate(record["side_info"]):
            if str(side.get("user", "")).lower() != wallet:
                continue
            if i not in (0, 1):
                continue
            f = {k: record.get(k) for k in ("coin", "time", "px", "sz", "hash")}
            f.update(side="B" if i == 0 else "A", startPosition=side.get("start_pos"),
                     oid=side.get("oid"), twapId=side.get("twap_id"), closedPnl=None, fee=None,
                     feeToken=None, tid=record.get("tid"), _quality="legacy_trade_missing_pnl_fee", **context)
            yield "fills", f
        return
    if isinstance(record.get("fill"), dict) and str(record.get("user", "")).lower() == wallet:
        yield "fills", {**record["fill"], **context}
        return
    inner = record.get("inner", record)
    tag, body = untag(inner)
    t = record.get("time", context.get("_block_time", 0))
    h = record.get("hash")
    if isinstance(body, dict):
        users = body.get("users")
        delta = body.get("delta")
        # When the event supplies affected account owners, that list is
        # authoritative. A target appearing only as a vault/validator/reference
        # inside an event for somebody else is not the target's ledger update.
        explicit_owner = isinstance(users, list) or ADDR.fullmatch(str(body.get("user", "")))
        if isinstance(users, list):
            owns_delta = has_wallet(users, wallet)
        elif ADDR.fullmatch(str(body.get("user", ""))):
            owns_delta = str(body["user"]).lower() == wallet
        else:
            d0 = delta_normalize(delta) if isinstance(delta, dict) else {}
            owns_delta = any(str(d0.get(k, "")).lower() == wallet for k in ("user", "destination"))
        if isinstance(delta, dict) and owns_delta:
            d = delta_normalize(delta)
            yield ("funding" if d["type"] == "funding" else "ledger"), {"time": t, "hash": h, "delta": d, **context}
            return
        if isinstance(delta, dict) and explicit_owner:
            return
        # Only accept per-user funding when ownership and amount are explicit.
        if (tag or "").lower() == "funding":
            if str(body.get("user", "")).lower() == wallet and all(k in body for k in ("coin", "usdc", "szi")):
                yield "funding", {"time": t, "hash": h, "delta": {"type": "funding", **body}, **context}
                return
            pairs = body.get("deltas", body.get("users", []))
            emitted = False
            if isinstance(pairs, list):
                for pair in pairs:
                    if isinstance(pair, list) and len(pair) == 2 and str(pair[0]).lower() == wallet and isinstance(pair[1], dict):
                        d = pair[1]
                        if all(k in d for k in ("coin", "usdc", "szi")):
                            yield "funding", {"time": t, "hash": h, "delta": {"type": "funding", **d}, **context}
                            emitted = True
            if emitted:
                return
    yield "raw_events", {"time": t, "hash": h, "raw": record, "event_type": tag,
                         "reason": "保留原始事件；其地址归属、金额单位或事件格式未充分核实", **context}


def object_hour(key):
    m = re.search(r"/(\d{8})/(\d{1,2})(?:\.[^/]*)?$", key)
    if not m or not 0 <= int(m.group(2)) < 24:
        return None
    try:
        dt = datetime.strptime(m.group(1), "%Y%m%d").replace(tzinfo=timezone.utc, hour=int(m.group(2)))
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None


def s3_client(region="us-east-1"):
    import boto3
    from botocore.config import Config
    return boto3.Session(region_name=region).client("s3", config=Config(retries={"max_attempts": 5, "mode": "standard"}, connect_timeout=15, read_timeout=90))


def inventory_archives(store, start, end, *, client=None, prefixes=S3_PREFIXES, log=print, cancel_check=None):
    """List actual objects first; AWS LIST itself can incur requester-pays charges."""
    client = client or s3_client()
    objects, errors, unknown = [], [], []
    # All-history mode uses zero and lists from each prefix's actual beginning.
    start_date = datetime.fromtimestamp(start / 1000, timezone.utc).strftime("%Y%m%d") if start else "00000000"
    for prefix in prefixes:
        if cancel_check:
            cancel_check()
        log(f"列出归档 {prefix} …")
        args = {"Bucket": S3_BUCKET, "Prefix": prefix + "/", "RequestPayer": "requester", "MaxKeys": 1000}
        if start:
            args["StartAfter"] = f"{prefix}/hourly/{start_date}/"
        try:
            while True:
                if cancel_check:
                    cancel_check()
                page = client.list_objects_v2(**args)
                for item in page.get("Contents", []):
                    key = item["Key"]
                    if key.endswith("/"):
                        continue
                    hour = object_hour(key)
                    if hour is None:
                        unknown.append({"prefix": prefix, "key": key, "reason": "未识别对象时间路径"})
                        continue
                    if hour + HOUR_MS <= start or hour > end:
                        continue
                    obj = {"bucket": S3_BUCKET, "key": key, "etag": item.get("ETag", ""), "size": int(item["Size"]),
                           "hour_ms": hour, "prefix": prefix}
                    obj["status"] = store.register_object(obj)
                    objects.append(obj)
                if not page.get("IsTruncated"):
                    break
                args.pop("StartAfter", None)
                token = page.get("NextContinuationToken")
                if not token:
                    raise ValueError("S3 截断清单却没有下一页令牌")
                args["ContinuationToken"] = token
        except Exception as exc:
            errors.append({"prefix": prefix, "error": str(exc)[:500]})
            log(f"  清单未完成：{prefix} / {type(exc).__name__}")
        store.commit()
    objects.sort(key=lambda o: (o["hour_ms"], S3_PREFIXES.index(o["prefix"]) if o["prefix"] in S3_PREFIXES else 9, o["key"]))
    result = {"start_ms": start, "end_ms": end, "objects": objects, "errors": errors, "unknown_keys": unknown,
              "object_count": len(objects), "compressed_bytes": sum(o["size"] for o in objects),
              "pending_bytes": sum(o["size"] for o in objects if o["status"] != "done")}
    store.set_meta("last_inventory_summary", {k: v for k, v in result.items() if k != "objects"})
    store.commit()
    log(f"清单：{len(objects):,} 个对象；待扫描压缩体积 {result['pending_bytes'] / 2**30:,.2f} GiB。")
    return result


def ingest_archive_stream(store, binary_stream, source, *, start=0, end=2**63 - 1, cancel_check=None):
    stats = Counter(lines=0, matched_lines=0, parsed_rows=0, new_rows=0, unknown_events=0, parse_errors=0)
    samples = []
    wallet_bytes = store.wallet.encode()
    for line in binary_stream:
        stats["lines"] += 1
        if cancel_check and stats["lines"] % 250 == 1:
            cancel_check()
        if isinstance(line, str):
            line = line.encode()
        # Still decompress the whole object; this filter only saves JSON parsing work.
        if wallet_bytes not in line.lower():
            continue
        stats["matched_lines"] += 1
        try:
            record = json.loads(line, parse_float=Decimal)
            for kind, row in parse_archive_record(record, store.wallet):
                row["time"] = timestamp_ms(row.get("time"))
                if start <= row["time"] <= end:
                    stats["parsed_rows"] += 1
                    stats["unknown_events"] += int(kind == "raw_events")
                    stats["new_rows"] += int(store.put(kind, row, source))
        except Exception as exc:
            stats["parse_errors"] += 1
            if len(samples) < 3:
                samples.append({"line": stats["lines"], "error": str(exc)[:250], "raw_preview": line[:1500].decode(errors="replace")})
        if stats["matched_lines"] % 1000 == 0:
            store.commit()
    return {**dict(stats), "error_samples": samples}


def scan_archives(store, inventory, *, client=None, accept_requester_pays=False,
                  max_download_gib=10, max_objects=0, log=print, cancel_check=None):
    """Stream each object, keeping only this wallet. Restart is per completed object."""
    if not accept_requester_pays:
        raise ValueError("请先查看清单体积，再在 Colab 勾选 ACCEPT_AWS_REQUESTER_PAYS。未开始下载。")
    if max_download_gib <= 0:
        raise ValueError("MAX_DOWNLOAD_GIB 必须为正数；它是本次压缩下载预算。")
    import lz4.frame
    client = client or s3_client()
    budget = int(max_download_gib * 2**30)
    used, count, skipped, failed = 0, 0, 0, 0
    stop_reason = "finished_available_inventory"
    for obj in inventory["objects"]:
        if cancel_check:
            cancel_check()
        if store.object_status(obj) == "done":
            skipped += 1
            continue
        if used + obj["size"] > budget or (max_objects and count >= max_objects):
            stop_reason = "budget_pause"
            log("达到本次预算，已保存进度；增大预算或再次运行可继续。")
            break
        # Count attempted bytes too: a failed transfer/retry may still have AWS charges.
        used += obj["size"]
        count += 1
        log(f"[{count}] 扫描 {obj['key']}  ({obj['size'] / 2**20:,.1f} MiB)")
        body = None
        try:
            response = client.get_object(Bucket=obj["bucket"], Key=obj["key"], RequestPayer="requester", IfMatch=obj["etag"])
            body = response["Body"]
            if obj["key"].endswith(".lz4"):
                stream = lz4.frame.open(body, "rb")
            elif obj["key"].endswith(".gz"):
                stream = gzip.GzipFile(fileobj=body)
            else:
                stream = io.BufferedReader(body)
            with stream:
                # Checkpoint the complete object, so later widening a date
                # range within this same hour cannot skip cached-out records.
                # Only the target wallet is kept; report queries apply the
                # requested time range when reading back from SQLite.
                stats = ingest_archive_stream(store, stream, "s3:" + obj["key"], cancel_check=cancel_check)
            status = "done" if stats["parse_errors"] == 0 else "parse_error"
            failed += int(status != "done")
            store.finish_object(obj, status, stats)
            log(f"  匹配 {stats['parsed_rows']:,} 条，新增 {stats['new_rows']:,} 条，未知事件 {stats['unknown_events']:,} 条")
        except Exception as exc:
            failed += 1
            store.finish_object(obj, "failed", {"error": str(exc)[:500]})
            log(f"  失败并保留重试标记：{type(exc).__name__}: {str(exc)[:180]}")
        finally:
            if body is not None:
                body.close()
    if stop_reason == "finished_available_inventory" and failed:
        stop_reason = "partial_failed_objects"
    result = {"status": stop_reason, "attempted_objects": count, "skipped_completed_objects": skipped,
              "failed_objects": failed, "attempted_compressed_bytes": used, "budget_gib": max_download_gib,
              "note": "预算按清单压缩字节估算；AWS 重试、请求和费用税费不等于此字节数。"}
    store.set_meta("last_archive_scan", result)
    store.commit()
    return result


FIELD_ALIASES = {
    "time": ["time", "timestamp", "Timestamp", "Time", "date", "datetime"],
    "coin": ["coin", "Coin", "symbol", "asset"], "px": ["px", "price", "Price"],
    "sz": ["sz", "size", "Size", "quantity"], "side": ["side", "Side"],
    "startPosition": ["startPosition", "start_position", "start_pos", "Start Position"],
    "closedPnl": ["closedPnl", "closed_pnl", "Closed Pnl", "Closed PnL", "closed pnl"],
    "fee": ["fee", "Fee"], "feeToken": ["feeToken", "fee_token", "Fee Token"],
    "tid": ["tid", "trade_id", "Trade ID"], "oid": ["oid", "order_id", "Order ID"],
    "hash": ["hash", "Hash", "tx_hash"], "dir": ["dir", "direction", "Direction"],
}


def normalize_import_fill(item, field_map=None, timestamp_unit="ms"):
    out = dict(item)
    for key, aliases in FIELD_ALIASES.items():
        names = [field_map[key]] if field_map and key in field_map else aliases
        for name in names:
            if name in item and item[name] not in (None, ""):
                out[key] = item[name]
                break
    for key in ("closedPnl", "fee", "feeToken", "startPosition", "tid", "oid"):
        if out.get(key) == "":
            out[key] = None
    side = str(out.get("side", "")).upper()
    out["side"] = {"BUY": "B", "BID": "B", "SELL": "A", "ASK": "A", "S": "A"}.get(side, side)
    if out["side"] not in ("A", "B"):
        # Direction can identify buy vs sell, but never assume a zero starting balance.
        out["side"] = {"Open Long": "B", "Close Short": "B", "Buy": "B", "Short > Long": "B",
                       "Open Short": "A", "Close Long": "A", "Sell": "A", "Long > Short": "A"}.get(out.get("dir"))
    for k in ("coin", "px", "sz", "time", "side"):
        if out.get(k) in (None, ""):
            raise ValueError(f"导入成交缺少 {k}。请用 FIELD_MAP 映射列名。")
    t = out["time"]
    if timestamp_unit == "s" and (isinstance(t, (int, float, Decimal)) or re.fullmatch(r"[+-]?\d+(?:\.\d+)?", str(t).strip())):
        t = int(Decimal(str(t)) * 1000)
    out["time"] = timestamp_ms(t)
    if out["time"] < 1_000_000_000_000:
        raise ValueError("成交时间早于 2001，可能是秒/毫秒设置错误。请检查 IMPORT_TIMESTAMP_UNIT。")
    if out.get("startPosition") is None:
        out["_quality"] = "import_missing_start_position"
    return out


def import_history_file(store, path, *, kind="fills", field_map=None, timestamp_unit="ms", start=0, end=2**63 - 1, log=print, cancel_check=None):
    """JSON array, JSONL, CSV; import files must belong to the selected wallet."""
    if kind not in ("fills", "ledger", "funding"):
        raise ValueError("导入类别须为 fills、ledger 或 funding。")
    if timestamp_unit not in ("ms", "s"):
        raise ValueError("IMPORT_TIMESTAMP_UNIT 须为 ms 或 s。")
    path = Path(path)
    added, total, rejected, rejected_count, outside = 0, 0, [], 0, 0
    opener = gzip.open if path.suffix == ".gz" else open
    actual_suffix = Path(path.stem).suffix if path.suffix == ".gz" else path.suffix
    with opener(path, "rt", encoding="utf-8-sig") as fh:
        if actual_suffix.lower() == ".csv":
            items = csv.DictReader(fh)
        elif actual_suffix.lower() in (".jsonl", ".ndjson"):
            def jsonl_items():
                for line in fh:
                    if not line.strip():
                        continue
                    try:
                        yield json.loads(line, parse_float=Decimal)
                    except (ValueError, TypeError) as exc:
                        # Keep scanning later lines and account for every
                        # rejected row, rather than losing the import summary.
                        yield exc
            items = jsonl_items()
        else:
            data = json.load(fh, parse_float=Decimal)
            if isinstance(data, dict):
                owner = data.get("user", data.get("wallet"))
                if owner and str(owner).strip().lower() != store.wallet:
                    raise ValueError("导入文件标记的钱包与目标不一致")
                data = data.get(kind, data.get("data", data.get("records")))
            if not isinstance(data, list):
                raise ValueError("JSON 需为记录数组，或含 fills/data/records 数组。")
            items = iter(data)
        for item in items:
            total += 1
            if cancel_check and total % 250 == 1:
                cancel_check()
            try:
                if isinstance(item, Exception):
                    raise item
                if not isinstance(item, dict):
                    raise ValueError("预期每条记录为对象")
                owner = item.get("user", item.get("wallet"))
                if owner and wallet_address(owner) != store.wallet:
                    raise ValueError("该记录属于另一钱包")
                row = normalize_import_fill(item, field_map, timestamp_unit) if kind == "fills" else dict(item)
                if kind in ("ledger", "funding"):
                    if isinstance(row.get("delta"), str):
                        row["delta"] = json.loads(row["delta"])
                    if not isinstance(row.get("delta"), dict):
                        raise ValueError("资金记录须包含官方 delta 对象")
                    row["delta"] = delta_normalize(row["delta"])
                    t = row.get("time")
                    if timestamp_unit == "s" and (isinstance(t, (int, float, Decimal)) or re.fullmatch(r"[+-]?\d+(?:\.\d+)?", str(t).strip())):
                        row["time"] = int(Decimal(str(t).strip()) * 1000)
                row["time"] = timestamp_ms(row.get("time"))
                if not start <= row["time"] <= end:
                    outside += 1
                    continue
                added += store.put(kind, row, "import:" + path.name)
            except Exception as exc:
                rejected_count += 1
                if len(rejected) < 30:
                    rejected.append({"row": total, "error": str(exc)[:250]})
            if total % 1000 == 0:
                store.commit()
    result = {"file": path.name, "kind": kind, "rows": total, "added": added, "outside_range": outside,
              "rejected_count": rejected_count, "rejected_examples": rejected,
              "note": "导入文件内容由使用者提供；导入成功不证明时间覆盖完整。"}
    previous = store.get_meta("imports", [])
    store.set_meta("imports", previous + [result])
    store.commit()
    log(f"导入 {path.name}: {total:,} 行，新增 {added:,} 行，拒绝 {rejected_count:,} 行")
    return result


def ledger_view(row, wallet):
    d = row.get("delta", {})
    typ = d.get("type", "unknown")
    sender = str(d.get("user", "")).lower()
    dest = str(d.get("destination", "")).lower()
    direction, counterparty = "未知", None
    if typ in ("deposit", "vaultDistribution", "vaultLeaderCommission", "rewardsClaim", "spotGenesis"):
        direction = "流入"
    elif typ in ("withdraw", "vaultDeposit", "vaultCreate", "accountActivationGas", "deployGasAuction"):
        direction = "流出"
    elif sender == wallet and dest == wallet:
        direction = "内部"
    elif sender == wallet:
        direction, counterparty = "流出", d.get("destination")
    elif dest == wallet:
        direction, counterparty = "流入", d.get("user")
    elif typ in ("accountClassTransfer", "perpDexClassTransfer"):
        direction = "现货→合约" if d.get("toPerp") is True else ("合约→现货" if d.get("toPerp") is False else "内部")
    amount = d.get("usdc") if d.get("usdc") is not None else d.get("amount")
    token = d.get("token") or ("USDC" if d.get("usdc") is not None else None)
    # requestedUsd is a request/basis field, not necessarily the actual cash paid out.
    quality = []
    if typ == "vaultWithdraw" and amount is None:
        quality.append("仅有提现请求/结算字段，实际到账金额未提供")
    if token is None and amount is not None:
        quality.append("金额币种未提供")
    return {**row, "type": typ, "amount": None if amount is None else str(amount), "token": token,
            "direction": direction, "counterparty": counterparty or d.get("vault"), "quality": quality}


def gap_intervals(hours, start, end):
    """Compact missing UTC-hour intervals; no million-element hourly array."""
    hours = sorted(set(hours))
    cursor, stop = (start // HOUR_MS) * HOUR_MS, (end // HOUR_MS) * HOUR_MS
    gaps = []
    for h in hours:
        if h < cursor or h > stop:
            continue
        if h > cursor:
            gaps.append({"start_ms": cursor, "end_ms": h - 1, "hours": (h - cursor) // HOUR_MS})
        cursor = h + HOUR_MS
    if cursor <= stop:
        gaps.append({"start_ms": cursor, "end_ms": stop + HOUR_MS - 1, "hours": (stop - cursor) // HOUR_MS + 1})
    return gaps


def coverage_report(store, start, end, inventory=None):
    api = store.get_meta("last_api_run", {})
    warning = ["全生命周期完整性未被证明：接口耗尽、归档扫描完成与钱包创世以来无遗漏不是同一件事。",
               "仅分析目标 HyperCore 地址；子账户、Vault 地址须分别运行，HyperEVM/其他链通用转账不在此范围。",
               "旧 node_trades 可能缺少 closedPnl 和 fee，未知值保持为空；资金费独立统计。"]
    counts = store.counts(start, end)
    result = {"version": VERSION, "wallet": store.wallet, "status": "覆盖待核验", "lifetime_complete": False,
              "requested_start_ms": start, "requested_end_ms": end, "generated_at_ms": int(time.time() * 1000),
              "record_counts": counts, "api": api, "imports": store.get_meta("imports", []),
              "archive_scan": store.get_meta("last_archive_scan"), "sources": SOURCES,
              "warnings": warning, "scope": "HyperCore 单地址：成交 / 仓位回合 / 非资金费流水 / 资金费"}
    if counts.get("raw_events"):
        warning.append(f"发现 {counts['raw_events']:,} 条目标钱包的未解释事件，已保存 raw_events.jsonl；资金历史存在解释缺口。")
    if any(r.get("status") == "failed" or r.get("gaps") for r in api.get("results", [])):
        warning.append("部分 API 请求失败或同毫秒响应饱和；详见 API 明细。")
    if inventory:
        per_prefix, scanned = {}, []
        for p in S3_PREFIXES:
            objects = [o for o in inventory["objects"] if o["prefix"] == p]
            done = [o for o in objects if store.object_status(o) == "done"]
            scanned.extend(done)
            per_prefix[p] = {"objects": len(objects), "scanned_objects": len(done), "compressed_bytes": sum(o["size"] for o in objects),
                             "first_hour_ms": min((o["hour_ms"] for o in objects), default=None),
                             "last_hour_ms": max((o["hour_ms"] for o in objects), default=None)}
        trade_done = [o["hour_ms"] for o in scanned if o["prefix"] != "misc_events_by_block"]
        misc_done = [o["hour_ms"] for o in scanned if o["prefix"] == "misc_events_by_block"]
        # With no user lower bound, distinguish pre-archive uncertainty from missing
        # hours inside observed coverage. Never pretend 1970 is the chain launch.
        observed = [o["hour_ms"] for o in inventory["objects"]]
        effective_start = start or min(observed, default=end)
        tg, mg = gap_intervals(trade_done, effective_start, end), gap_intervals(misc_done, effective_start, end)
        archive = {"prefixes": per_prefix, "listed_objects": len(inventory["objects"]), "scanned_objects": len(scanned),
                   "list_errors": inventory["errors"], "unknown_key_count": len(inventory["unknown_keys"]),
                   "trade_missing_hour_intervals": tg[:40], "trade_missing_hours": sum(g["hours"] for g in tg),
                   "misc_missing_hour_intervals": mg[:40], "misc_missing_hours": sum(g["hours"] for g in mg),
                   "before_first_archive": "首个可用归档之前未有完整性证明" if not start else None,
                   "all_listed_objects_scanned": bool(inventory["objects"]) and len(scanned) == len(inventory["objects"])
                   and not inventory["errors"] and not inventory["unknown_keys"]}
        result["archive"] = archive
        if archive["all_listed_objects_scanned"]:
            result["status"] = "已扫描所列可用归档；全生命周期待核验"
        else:
            result["status"] = "部分覆盖 / 归档待续传"
        if tg or mg:
            warning.append("归档存在未扫描/未列出的小时（也可能是历史前缀尚未上线或最新小时未发布）；API 或导入可能补齐，但不能自动证明。")
        if inventory["unknown_keys"]:
            warning.append(f"归档清单有 {len(inventory['unknown_keys']):,} 个无法识别时间路径的对象尚未扫描。")
    else:
        result["status"] = "API / 导入可见范围；尚未扫描官方归档"
        warning.append("普通成交 API 仅提供最近 10,000 条。仅运行 API 不能回补高频钱包的更早历史。")
    return result


def export_audit(store, directory, start, end, inventory=None):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for kind in ("fills", "ledger", "funding", "raw_events"):
        with (directory / f"{kind}.jsonl").open("w", encoding="utf-8") as f:
            for row in store.rows(kind, start, end):
                f.write(js(row) + "\n")
    if inventory:
        with (directory / "archive_manifest.jsonl").open("w", encoding="utf-8") as f:
            for obj in inventory["objects"]:
                r = store.db.execute("SELECT status,stats FROM objects WHERE bucket=? AND key=?", (obj["bucket"], obj["key"])).fetchone()
                f.write(js({**obj, "status": r[0] if r else "pending", "stats": json.loads(r[1]) if r else {}}) + "\n")
    (directory / "coverage.json").write_text(json.dumps(coverage_report(store, start, end, inventory), ensure_ascii=False, indent=2), encoding="utf-8")

