"""Market metadata, current marks and cached historical candles (public /info).

Every perp DEX listed by ``perpDexs`` is queried, not only those seen in fills,
so a HIP-3 position without recent fills still has metadata. A failing DEX
keeps its markets visible with ``meta_error`` instead of disappearing.
"""
from __future__ import annotations

import json
import time
from decimal import Decimal

STABLECOINS = frozenset({"USDC", "USDT0", "USDT", "USDH", "USDE", "USDHL", "FEUSD"})
DAY_MS = 86_400_000
INTERVAL_MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": DAY_MS}


def _dec(value):
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except Exception:
        return None


def dex_of(coin):
    coin = str(coin or "")
    return coin.split(":", 1)[0] if ":" in coin else ""


def fetch_market_meta(client, store=None, log=print):
    """Return {coin: meta} plus ``spot_prices`` {token: USD} and errors.

    meta keys: market_type, dex, pnl_token, sz_decimals, mark_px, oracle_px,
    funding, open_interest, max_leverage, display_name, base_token, quote_token.
    """
    out, errors, spot_prices = {}, [], {token: "1" for token in STABLECOINS}
    tokens = {0: "USDC"}
    try:
        spot_meta, spot_ctxs = client.post({"type": "spotMetaAndAssetCtxs"})
        tokens = {t["index"]: t["name"] for t in spot_meta.get("tokens", [])}
        best = {}
        # Contexts are keyed by ``coin``; the list is longer than the universe
        # and not in universe order, so positional zip misprices tokens.
        by_coin = {c.get("coin"): c for c in spot_ctxs if isinstance(c, dict)}
        for pair in spot_meta.get("universe", []):
            ctx = by_coin.get(pair["name"])
            ids = pair.get("tokens", [])
            base = tokens.get(ids[0]) if ids else None
            quote = tokens.get(ids[1]) if len(ids) > 1 else None
            mark = _dec(ctx.get("markPx")) if isinstance(ctx, dict) else None
            meta = {"market_type": "spot", "dex": "", "display_name": f"{base}/{quote}",
                    "base_token": base, "quote_token": quote, "pnl_token": quote,
                    "mark_px": None if mark is None else str(mark),
                    "day_volume": ctx.get("dayNtlVlm") if isinstance(ctx, dict) else None}
            out[pair["name"]] = meta
            out["@" + str(pair["index"])] = meta
            # Token USD price: most liquid pair quoted in a stablecoin.
            if base and quote in STABLECOINS and mark is not None and base not in STABLECOINS:
                volume = _dec(meta["day_volume"]) or Decimal(0)
                if base not in best or volume > best[base][0]:
                    best[base] = (volume, mark)
        spot_prices.update({token: str(px) for token, (_, px) in best.items()})
    except Exception as exc:
        errors.append("spotMetaAndAssetCtxs: " + str(exc)[:200])
    dexes = [""]
    try:
        listed = client.post({"type": "perpDexs"})
        dexes += [d["name"] for d in listed[1:] if isinstance(d, dict) and d.get("name")]
    except Exception as exc:
        errors.append("perpDexs: " + str(exc)[:200])
    for dex in dexes:
        try:
            body = {"type": "metaAndAssetCtxs"}
            if dex:
                body["dex"] = dex
            meta, ctxs = client.post(body)
            collateral = tokens.get(meta.get("collateralToken", 0))
            for asset, ctx in zip(meta.get("universe", []), ctxs):
                name = asset["name"]
                if dex and ":" not in name:
                    name = dex + ":" + name
                out[name] = {
                    "market_type": "perp", "dex": dex, "pnl_token": collateral,
                    "collateral_token": collateral, "sz_decimals": asset.get("szDecimals"),
                    "max_leverage": asset.get("maxLeverage"), "delisted": bool(asset.get("isDelisted")),
                    "mark_px": ctx.get("markPx"), "oracle_px": ctx.get("oraclePx"),
                    "funding": ctx.get("funding"), "open_interest": ctx.get("openInterest"),
                    "day_volume": ctx.get("dayNtlVlm"), "display_name": name,
                }
        except Exception as exc:
            errors.append(f"metaAndAssetCtxs {dex or 'native'}: " + str(exc)[:200])
            out.setdefault("__dex_errors__", {})[dex] = str(exc)[:200]
    if store is not None:
        store.set_meta("asset_meta", out)
        store.set_meta("spot_prices", spot_prices)
        store.set_meta("asset_meta_errors", errors)
        store.commit()
    if errors:
        log("部分市场元数据不可用：" + "; ".join(errors[:3]))
    return out


def market_for(coin, meta):
    """Meta for a coin, keeping unknown HIP-3 markets visible with a flag."""
    data = (meta or {}).get(coin)
    if data:
        return data
    dex = dex_of(coin)
    errors = (meta or {}).get("__dex_errors__", {})
    kind = "outcome" if coin.startswith("#") else "spot" if coin.startswith("@") or "/" in coin else "perp"
    return {"market_type": kind, "dex": dex,
            "pnl_token": "USDC" if not dex else None, "display_name": coin,
            "meta_error": errors.get(dex, "market not listed")}


def token_price(token, spot_prices):
    if token in STABLECOINS:
        return Decimal(1)
    return _dec((spot_prices or {}).get(token))


def _bars(client, store, coin, interval, start, end, log, need, checked_key, throttle=0.0):
    """Cached bars {bucket_ms: payload} for [start, end]; only missing ranges are fetched.

    Bars are stored with full OHLCV. ``need`` lists payload fields a cached row
    must carry to count (old close-only rows are refetched when highs/lows are
    needed); ``checked_key`` remembers ranges the API already answered empty.
    """
    step = INTERVAL_MS[interval]
    start, end = (start // step) * step, (end // step) * step
    cached = {}
    if store is not None:
        for row in store.db.execute(
                "SELECT payload FROM records WHERE kind='candle' AND coin=? AND time BETWEEN ? AND ?",
                (f"{coin}|{interval}", start, end)):
            payload = json.loads(row[0])
            if all(k in payload for k in need):
                cached[payload["time"]] = payload
    # The forming bar is never cached and callers use the live mark for it;
    # ranges already asked for (e.g. before listing / after delisting) are not re-asked.
    now_bucket = (int(time.time() * 1000) // step) * step
    meta_key = f"{checked_key}:{coin}|{interval}"
    checked = (store.get_meta(meta_key) or [None, None]) if store is not None else [None, None]
    needed = [t for t in range(start, min(end, now_bucket - step) + 1, step) if t not in cached
              and not (checked[0] is not None and checked[0] <= t <= checked[1])]
    if needed and client is not None:
        lo, hi = needed[0], needed[-1] + step - 1
        # candleSnapshot returns at most 5000 bars per request.
        cursor = lo
        while cursor <= hi:
            chunk_end = min(hi, cursor + step * 5000 - 1)
            try:
                bars = client.post({"type": "candleSnapshot", "req": {
                    "coin": coin, "interval": interval, "startTime": cursor, "endTime": chunk_end}})
            except Exception as exc:
                log(f"K 线 {coin} {interval} 获取失败：{str(exc)[:120]}")
                break
            for bar in bars or []:
                t = int(bar["t"])
                payload = {"time": t, "o": str(bar.get("o")), "h": str(bar.get("h")), "l": str(bar.get("l")),
                           "c": str(bar["c"]), "v": str(bar.get("v"))}
                cached[t] = payload
                # The still-forming bar is not cached; it would freeze a partial close.
                if store is not None and t < now_bucket:
                    store.db.execute(
                        "INSERT OR REPLACE INTO records VALUES(?,?,?,?,?,?,?,?)",
                        ("candle", f"{coin}|{interval}|{t}", t, f"{coin}|{interval}",
                         json.dumps(payload, separators=(",", ":")), "api:candleSnapshot", 50, None))
            cursor = chunk_end + 1
            if throttle:
                time.sleep(throttle)
        else:
            if store is not None:
                lo_c = lo if checked[0] is None else min(lo, checked[0])
                hi_c = hi if checked[1] is None else max(hi, checked[1])
                # Only extend over a contiguous range, otherwise remember the new one.
                if checked[0] is not None and (lo > checked[1] + step or hi < checked[0] - step):
                    lo_c, hi_c = lo, hi
                store.set_meta(meta_key, [lo_c, hi_c])
        if store is not None:
            store.commit()
    return dict(sorted(cached.items()))


def candles(client, store, coin, start, end, interval="1d", log=print):
    """Cached closes {bucket_start_ms: close}. Only missing ranges are fetched."""
    bars = _bars(client, store, coin, interval, start, end, log, ("c",), "candle_checked")
    return {t: b["c"] for t, b in bars.items()}


def candles_ohlc(client, store, coin, interval, start, end, log=print, throttle=0.0):
    """Cached {bucket_start_ms: (high, low)} floats for excursion scans."""
    bars = _bars(client, store, coin, interval, start, end, log, ("h", "l"), "ohlc_checked", throttle)
    out = {}
    for t, b in bars.items():
        try:
            out[t] = (float(b["h"]), float(b["l"]))
        except (TypeError, ValueError):
            continue
    return out


# Finest first. Each interval keeps the latest 5000 bars of the free API:
# 15m ~52 days, 1h ~208 days, 4h ~833 days, 1d everything.
LADDER = ("15m", "1h", "4h", "1d")
LADDER_BARS = 5000


def ladder(client, store, coin, *, since=None, now=None, log=print, throttle=0.25):
    """{interval: {bucket: (high, low)}} for every ladder interval of one coin.

    Each interval is asked only for its own reachable range (latest 5000 bars),
    clipped to ``since`` so a coin traded recently does not pull years of 1d bars.
    """
    now = int(time.time() * 1000) if now is None else int(now)
    out = {}
    for interval in LADDER:
        step = INTERVAL_MS[interval]
        lo = now - step * (LADDER_BARS - 1)
        if since is not None:
            lo = max(lo, (since // step) * step)
        out[interval] = candles_ohlc(client, store, coin, interval, lo, now, log=log, throttle=throttle)
    return out


def mark_at(series, t, step):
    """Close of the bar containing t, else the latest earlier close."""
    if not series:
        return None
    bucket = (t // step) * step
    if bucket in series:
        return _dec(series[bucket])
    earlier = [k for k in series if k <= bucket]
    return _dec(series[max(earlier)]) if earlier else None
