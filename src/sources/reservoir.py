"""Hydromancer Reservoir (requester-pays S3, daily Parquet) as an optional backfill.

Reservoir publishes per-day Parquet files of fills and account snapshots
(positions, balances) from 2025-07-28 in ``s3://hydromancer-reservoir``
(ap-northeast-1). The public docs disagree on the exact path layout, so the
layout is discovered by listing once and cached in store meta.

Only the bytes needed are fetched: :class:`RangeFile` serves pyarrow with
HTTP range reads, the user column is read first per row group (skipped
entirely when its min/max statistics exclude the wallet) and the remaining
columns only for row groups that contain the wallet.
"""
from __future__ import annotations

import io
import re
from datetime import datetime, timezone

DAY_MS = 86_400_000
USD_PER_GB = 0.09
DATE = re.compile(r"(?:^|[/=_-])(\d{4})-?(\d{2})-?(\d{2})(?:[/._-]|$)")

USER_COLUMNS = ("user", "address", "wallet", "user_address", "account")
FILL_ALIASES = {
    "time": ("time", "timestamp", "block_time", "ts", "fill_time"),
    "coin": ("coin", "asset", "symbol", "market"),
    "px": ("px", "price"), "sz": ("sz", "size", "qty", "quantity"),
    "side": ("side",), "dir": ("dir", "direction"),
    "closedPnl": ("closedPnl", "closed_pnl", "realized_pnl"),
    "fee": ("fee",), "feeToken": ("feeToken", "fee_token"),
    "startPosition": ("startPosition", "start_position"),
    "oid": ("oid", "order_id"), "tid": ("tid", "trade_id"), "hash": ("hash", "tx_hash"),
    "crossed": ("crossed",), "builderFee": ("builderFee", "builder_fee"),
}
POSITION_ALIASES = {
    "time": ("time", "timestamp", "snapshot_time", "ts"),
    "coin": ("coin", "asset", "symbol", "market"),
    "szi": ("szi", "size", "position", "position_size", "signed_size"),
    "entry_px": ("entry_px", "entryPx", "entry_price"),
    "position_value": ("position_value", "positionValue", "notional"),
}
DATASETS = {"fills": ("fill",), "positions": ("position", "clearinghouse", "perp_state", "account_state")}


class Budget:
    """Shared byte budget across range reads; raises once spending would pass the cap."""

    def __init__(self, max_usd, usd_per_gb=USD_PER_GB):
        self.max_bytes = int(max_usd / usd_per_gb * 1e9) if max_usd is not None else None
        self.used = 0
        self.usd_per_gb = usd_per_gb

    def take(self, n):
        if self.max_bytes is not None and self.used + n > self.max_bytes:
            raise BudgetExceeded(f"已达补数预算（{self.usd:.2f} 美元）")
        self.used += n

    @property
    def usd(self):
        return self.used / 1e9 * self.usd_per_gb


class BudgetExceeded(RuntimeError):
    pass


class RangeFile(io.RawIOBase):
    """Seekable read-only file over S3 ``GetObject`` byte ranges (requester pays)."""

    def __init__(self, client, bucket, key, size, budget=None, block=1 << 18):
        self.client, self.bucket, self.key, self.size = client, bucket, key, int(size)
        self.budget, self.block = budget, block
        self.pos, self.requests, self.bytes = 0, 0, 0
        self._buf_start, self._buf = 0, b""

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, offset, whence=0):
        base = {0: 0, 1: self.pos, 2: self.size}[whence]
        self.pos = max(0, min(self.size, base + offset))
        return self.pos

    def _fetch(self, start, length):
        end = min(self.size, start + length) - 1
        if self.budget:
            self.budget.take(end - start + 1)
        body = self.client.get_object(Bucket=self.bucket, Key=self.key, Range=f"bytes={start}-{end}",
                                      RequestPayer="requester")["Body"]
        try:
            data = body.read()
        finally:
            body.close()
        self.requests += 1
        self.bytes += len(data)
        return data

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self.pos
        n = min(n, self.size - self.pos)
        if n <= 0:
            return b""
        lo, hi = self.pos - self._buf_start, self.pos - self._buf_start + n
        if not (0 <= lo and hi <= len(self._buf)):
            # Read ahead a little: pyarrow issues many small footer/page reads.
            self._buf_start, self._buf = self.pos, self._fetch(self.pos, max(n, self.block))
            lo, hi = 0, n
        data = self._buf[lo:hi]
        self.pos += len(data)
        return data

    def readinto(self, b):
        data = self.read(len(b))
        b[:len(data)] = data
        return len(data)


def _day(key):
    m = DATE.search(key)
    if not m:
        return None
    try:
        return int(datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc).timestamp() * 1000)
    except ValueError:
        return None


def discover(client, bucket, prefix="", *, max_pages=200, log=print):
    """{dataset: {day_ms: [(key, size)]}} for Parquet objects under ``prefix``."""
    found = {}
    args = {"Bucket": bucket, "Prefix": prefix, "RequestPayer": "requester", "MaxKeys": 1000}
    for _ in range(max_pages):
        page = client.list_objects_v2(**args)
        for item in page.get("Contents", []):
            key = item["Key"]
            if not key.endswith(".parquet"):
                continue
            day = _day(key)
            lowered = key.lower()
            dataset = next((name for name, words in DATASETS.items() if any(w in lowered for w in words)), None)
            if dataset and day is not None:
                found.setdefault(dataset, {}).setdefault(day, []).append((key, int(item["Size"])))
        if not page.get("IsTruncated"):
            break
        args["ContinuationToken"] = page["NextContinuationToken"]
    log("Reservoir 目录：" + ", ".join(f"{k} {len(v)} 天" for k, v in found.items()) if found else "Reservoir 目录：未发现可用数据集")
    return found


def _resolve(names, aliases):
    lowered = {n.lower(): n for n in names}
    return {field: next((lowered[a.lower()] for a in options if a.lower() in lowered), None)
            for field, options in aliases.items()}


def _ms(value, day_ms):
    if value is None:
        return day_ms + DAY_MS - 1
    if isinstance(value, datetime):
        return int((value if value.tzinfo else value.replace(tzinfo=timezone.utc)).timestamp() * 1000)
    try:
        v = int(value)
    except (TypeError, ValueError):
        return day_ms + DAY_MS - 1
    if v < 10**11:  # seconds
        return v * 1000
    if v < 10**14:  # milliseconds
        return v
    return v // 1000 if v < 10**17 else v // 10**6  # micro / nanoseconds


def _norm(value):
    if isinstance(value, bytes):
        value = "0x" + value.hex() if len(value) == 20 else value.decode(errors="replace")
    return str(value).strip().lower()


def wallet_rows(client, bucket, key, size, wallet, aliases, *, day_ms, budget=None):
    """Rows of ``wallet`` from one Parquet object, mapped to ``aliases`` fields."""
    import pyarrow.parquet as pq
    raw = RangeFile(client, bucket, key, size, budget)
    pf = pq.ParquetFile(io.BufferedReader(raw, buffer_size=1 << 16))
    names = pf.schema_arrow.names
    user_col = next((n for n in names if n.lower() in USER_COLUMNS), None)
    if user_col is None:
        return [], {"requests": raw.requests, "bytes": raw.bytes, "error": "no user column"}
    mapping = {f: c for f, c in _resolve(names, aliases).items() if c}
    target = wallet.lower()
    idx = names.index(user_col)
    out, skipped = [], 0
    for g in range(pf.num_row_groups):
        stats = pf.metadata.row_group(g).column(idx).statistics
        if stats is not None and stats.has_min_max:
            lo, hi = _norm(stats.min), _norm(stats.max)
            if not lo <= target <= hi:
                skipped += 1
                continue
        users = [_norm(u) for u in pf.read_row_group(g, columns=[user_col]).column(0).to_pylist()]
        hits = [i for i, u in enumerate(users) if u == target]
        if not hits:
            continue
        table = pf.read_row_group(g, columns=sorted(set(mapping.values())))
        cols = {f: table.column(c).to_pylist() for f, c in mapping.items()}
        for i in hits:
            row = {f: cols[f][i] for f in mapping}
            row["time"] = _ms(row.get("time"), day_ms)
            out.append({k: (str(v) if isinstance(v, float) else v) for k, v in row.items() if v is not None})
    return out, {"requests": raw.requests, "bytes": raw.bytes, "row_groups": pf.num_row_groups, "skipped_groups": skipped}


def backfill(store, client, days, *, bucket, prefix="", budget=None, log=print, cancel_check=None):
    """Load fills and position snapshots of ``store.wallet`` for the given UTC days."""
    layout = store.get_meta("reservoir_layout")
    if not layout or layout.get("bucket") != bucket or layout.get("prefix") != prefix:
        found = discover(client, bucket, prefix, log=log)
        layout = {"bucket": bucket, "prefix": prefix,
                  "datasets": {name: {str(d): v for d, v in days_.items()} for name, days_ in found.items()}}
        store.set_meta("reservoir_layout", layout)
        store.commit()
    stats = {"fills": 0, "position_snapshots": 0, "objects": 0, "bytes": 0, "requests": 0, "missing_days": 0}
    plan = (("fills", "fills", FILL_ALIASES), ("positions", "position_snapshot", POSITION_ALIASES))
    for dataset, kind, aliases in plan:
        objects = layout["datasets"].get(dataset, {})
        for day in sorted(days):
            if cancel_check:
                cancel_check()
            entries = objects.get(str(day))
            if not entries:
                stats["missing_days"] += 1
                continue
            for key, size in entries:
                rows, info = wallet_rows(client, bucket, key, size, store.wallet, aliases, day_ms=day, budget=budget)
                stats["objects"] += 1
                stats["bytes"] += info["bytes"]
                stats["requests"] += info["requests"]
                for row in rows:
                    if kind == "fills":
                        if not row.get("coin"):
                            continue
                        side = str(row.get("side") or "").lower()
                        row["side"] = "B" if side in ("b", "buy", "bid", "long") else "A" if side else None
                    store.put(kind, row, "plugin:reservoir")
                    stats["fills" if kind == "fills" else "position_snapshots"] += 1
            store.commit()
    log(f"Reservoir：读取 {stats['objects']} 个文件 {stats['bytes'] / 2**20:,.1f} MiB，"
        f"成交 {stats['fills']:,} 条，持仓快照 {stats['position_snapshots']:,} 条。")
    return stats
