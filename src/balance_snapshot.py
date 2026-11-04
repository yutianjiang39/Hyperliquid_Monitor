"""Derived USDC cash anchors from public, non-atomic account snapshots.

The scope is this HyperCore address's USDC cash across spot and USDC-settled
perp DEXs, including reserved/isolated collateral.  It excludes spot coin values,
vault shares, staking and other chains.  Never use withdrawable or deduct holds.

Official references checked 2026-11-02:
https://hyperliquid.gitbook.io/hyperliquid-docs/trading/account-abstraction-modes
https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/spot
https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals

Unified spot balances are the aggregate trading-balance source, and the official
unified-account risk formula uses spot total less isolated margins as collateral.
Interpreting that total as USDC equity and removing all matching unrealized PnL
produces a *derived* cash anchor.  Separate HTTP snapshots are not atomic, so
automatic anchors remain validated=False even if two acquisitions agree.
"""
from __future__ import annotations

import csv
import json
import time
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path

from collector import timestamp_ms, wallet_address


SCOPE = "hypercore_usdc_cash"
ZERO = Decimal(0)
SUPPORTED_STANDARD = {"disabled", "default", "standard"}
SOURCE_URLS = [
    "https://hyperliquid.gitbook.io/hyperliquid-docs/trading/account-abstraction-modes",
    "https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/spot",
    "https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals",
]


def _clock(now_ms):
    value = now_ms() if callable(now_ms) else now_ms
    return int(time.time() * 1000) if value is None else int(value)


def _number(value, label):
    if value is None or isinstance(value, bool) or str(value).strip() == "":
        raise ValueError(f"{label} 缺少有效数值")
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} 不是有效数值") from exc
    if not result.is_finite():
        raise ValueError(f"{label} 不是有限数值")
    return result


def _string(value):
    if value == ZERO:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _dex_names(payload):
    if not isinstance(payload, list) or not payload or payload[0] is not None:
        raise ValueError("perpDexs 缺少原生 DEX 占位项，无法确认全部市场范围")
    result = [""]
    for entry in payload[1:]:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or not entry["name"]:
            raise ValueError("perpDexs 含无法识别的市场，未发布不完整的余额锚点")
        if entry["name"] in result:
            raise ValueError("perpDexs 市场名称重复")
        result.append(entry["name"])
    return result


def _metadata_by_dex(payload, names):
    """Accept both documented [meta, contexts] and actual bare-meta lists.

    Names are grounded in universe prefixes, not guessed from response order.
    Empty universes are resolved by the caller with an explicit meta request.
    """
    if not isinstance(payload, list) or not payload:
        raise ValueError("allPerpMetas 没有可用的市场元数据")
    mapped = {}
    for item in payload:
        meta = item[0] if isinstance(item, list) and item and isinstance(item[0], dict) else item
        if not isinstance(meta, dict) or not isinstance(meta.get("universe"), list):
            raise ValueError("allPerpMetas 含无法识别的元数据")
        prefixes = set()
        for market in meta["universe"]:
            if not isinstance(market, dict) or not isinstance(market.get("name"), str) or not market["name"]:
                raise ValueError("市场元数据缺少有效名称")
            prefixes.add(market["name"].split(":", 1)[0] if ":" in market["name"] else "")
        if not prefixes:
            continue
        if len(prefixes) != 1:
            raise ValueError("同一 DEX 元数据含不一致的市场前缀")
        name = next(iter(prefixes))
        if name not in names or name in mapped:
            raise ValueError("DEX 清单与元数据不一致，请重新采集")
        mapped[name] = meta
    return mapped


def _collateral_token(meta, dex):
    token = _number(meta.get("collateralToken"), f"{dex or '原生 DEX'} collateralToken")
    if token < ZERO or token != token.to_integral_value():
        raise ValueError("collateralToken 必须为非负整数")
    return int(token)


def _spot_usdc(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("balances"), list):
        raise ValueError("spotClearinghouseState 没有余额列表")
    matches = []
    for row in payload["balances"]:
        if not isinstance(row, dict):
            raise ValueError("现货余额列表含无效记录")
        token = row.get("token")
        if token in (0, "0") or (token is None and row.get("coin") == "USDC"):
            if row.get("coin") != "USDC":
                raise ValueError("USDC token 0 与返回资产名称不一致")
            matches.append(row)
    if len(matches) > 1:
        raise ValueError("USDC 余额重复，不能重复相加")
    # A successful balance list omits tokens whose balance is zero.
    return _number(matches[0].get("total"), "USDC total") if matches else ZERO


def _perp_components(payload, dex, *, require_equity):
    if not isinstance(payload, dict) or not isinstance(payload.get("assetPositions"), list):
        raise ValueError(f"{dex or '原生 DEX'} 缺少持仓列表")
    pnl = ZERO
    positions, seen = [], set()
    for entry in payload["assetPositions"]:
        position = entry.get("position") if isinstance(entry, dict) else None
        if not isinstance(position, dict):
            raise ValueError("持仓结构无效")
        coin = position.get("coin")
        if not isinstance(coin, str) or not coin or coin in seen:
            raise ValueError("持仓市场名称缺失或重复")
        prefix = coin.split(":", 1)[0] if ":" in coin else ""
        if prefix != dex:
            raise ValueError("持仓返回的 DEX 与请求不一致")
        seen.add(coin)
        qty = _number(position.get("szi"), f"{coin} szi")
        value = _number(position.get("unrealizedPnl"), f"{coin} unrealizedPnl")
        if qty == ZERO and value != ZERO:
            raise ValueError("零仓位出现非零未实现盈亏")
        pnl += value
        positions.append([coin, _string(qty), position.get("entryPx")])
    equity = None
    if require_equity:
        summary = payload.get("marginSummary")
        if not isinstance(summary, dict):
            raise ValueError("标准账户缺少 marginSummary")
        equity = _number(summary.get("accountValue"), f"{dex or '原生 DEX'} accountValue")
    return pnl, equity, sorted(positions)


def collect_balance_anchor(store, client, *, log=print, cancel_check=None, now_ms=None):
    """Collect one derived anchor plus a second-snapshot consistency check.

    Failure retains previously cached and explicitly imported/manual anchors.
    All requests are public /info reads.  Unsupported account modes do not
    receive an automatically guessed balance.  Cancellation propagates.
    """
    with localcontext() as context:
        context.prec = 60
        return _collect(store, client, log=log, cancel_check=cancel_check, now_ms=now_ms)


def _collect(store, client, *, log, cancel_check, now_ms):
    def clock():
        return _clock(now_ms)

    def check():
        if cancel_check is not None:
            cancel_check()

    previous = dict(store.get_meta("balance_anchor_meta", {}) or {})
    existing = list(store.get_meta("balance_anchors", []) or [])
    stamp = clock()
    raw = {"wallet": store.wallet, "attempted_at_ms": stamp, "requests": [], "rounds": []}
    warnings = []
    meta = {**previous, "fetched_at_ms": stamp, "latest_fetch_succeeded": False,
        "scope": SCOPE, "source_urls": SOURCE_URLS, "warnings": warnings, "error": None}

    def request(body):
        check()
        record = {"request": dict(body), "request_start_ms": clock()}
        raw["requests"].append(record)
        try:
            response = client.post(body)
            record["response"] = response
            return response, record
        except Exception as exc:
            record["error"] = str(exc)[:500]
            raise
        finally:
            record["request_end_ms"] = clock()
            check()

    try:
        mode, _ = request({"type": "userAbstraction", "user": store.wallet})
        meta["user_abstraction"] = mode
        if mode != "unifiedAccount" and mode not in SUPPORTED_STANDARD:
            meta["status"] = "unsupported"
            warnings.append(f"账户模式 {mode!s} 暂不自动推导 USDC 账面余额；可导入已核实的同范围余额基准点。")
        else:
            names = _dex_names(request({"type": "perpDexs"})[0])
            all_meta = request({"type": "allPerpMetas"})[0]
            mapped = _metadata_by_dex(all_meta, names)
            for dex in names:
                if dex not in mapped:
                    payload = request({"type": "meta", "dex": dex})[0]
                    if not isinstance(payload, dict) or not isinstance(payload.get("universe"), list):
                        raise ValueError(f"{dex or '原生 DEX'} 无法取得完整元数据")
                    mapped[dex] = payload
            usdc_dexs = [dex for dex in names if _collateral_token(mapped[dex], dex) == 0]
            if "" not in usdc_dexs:
                raise ValueError("元数据未确认原生 DEX 的 USDC 结算范围")
            raw["usdc_dexs"] = usdc_dexs
            meta["usdc_dexs"] = usdc_dexs
            require_equity = mode in SUPPORTED_STANDARD
            for iteration in range(2):
                round_start = clock()
                spot, spot_request = request({"type": "spotClearinghouseState", "user": store.wallet})
                spot_total = _spot_usdc(spot)
                unrealized = perp_equity = ZERO
                positions, state_times = [], []
                for dex in usdc_dexs:
                    state, _ = request({"type": "clearinghouseState", "user": store.wallet, "dex": dex})
                    u, equity, signature = _perp_components(state, dex, require_equity=require_equity)
                    unrealized += u
                    if equity is not None:
                        perp_equity += equity
                    positions.extend(signature)
                    server_time = state.get("time")
                    if isinstance(server_time, int) and not isinstance(server_time, bool) and server_time >= 0:
                        state_times.append(server_time)
                end = clock()
                balance = spot_total - unrealized + (perp_equity if require_equity else ZERO)
                raw["rounds"].append({"round": iteration + 1, "time": end,
                    "request_start_ms": round_start, "request_end_ms": end,
                    "snapshot_span_ms": max(0, end - round_start),
                    "perp_state_times": state_times,
                    "spot_request_start_ms": spot_request["request_start_ms"],
                    "spot_request_end_ms": spot_request["request_end_ms"],
                    "spot_usdc_total": _string(spot_total),
                    "usdc_perp_unrealized_pnl": _string(unrealized),
                    "standard_perp_equity": _string(perp_equity) if require_equity else None,
                    "balance_usdc": _string(balance), "position_signature": sorted(positions)})
            last_mode, _ = request({"type": "userAbstraction", "user": store.wallet})
            if last_mode != mode:
                raise ValueError("采集期间账户模式发生变化，未发布余额锚点")
            first, last = raw["rounds"]
            difference = _number(last["balance_usdc"], "balance") - _number(first["balance_usdc"], "balance")
            tolerance = max(Decimal("0.02"), abs(_number(last["balance_usdc"], "balance")) * Decimal("0.000001"))
            quality = ["derived_snapshot", "non_atomic_snapshot", "anchor_estimate"]
            stable = abs(difference) <= tolerance
            if not stable:
                quality.append("paired_snapshot_change")
                warnings.append("两轮余额推导值有差异，可能来自采样时差或期间资金活动；仅作估算基准。")
            if first["position_signature"] != last["position_signature"]:
                quality.append("positions_changed_during_snapshot")
                warnings.append("两轮采集间持仓发生变化，快照无法视为同一原子状态。")
            if max(first["snapshot_span_ms"], last["snapshot_span_ms"]) > 30_000:
                quality.append("wide_snapshot_time_span")
                warnings.append("余额快照采集跨度超过 30 秒，估算误差可能扩大。")
            formula = "spot_usdc_total + sum(usdc_perp_account_value - unrealized_pnl)" if require_equity else "spot_usdc_total - sum(usdc_perp_unrealized_pnl)"
            anchor = {"time": last["time"], "balance_usdc": last["balance_usdc"],
                "scope": SCOPE, "source": "derived_snapshot:standard" if require_equity else "derived_snapshot:unified",
                "validated": False, "quality": quality, "user_abstraction": mode,
                "formula": formula, "usdc_dexs": usdc_dexs,
                "snapshot_start_ms": last["request_start_ms"], "snapshot_end_ms": last["request_end_ms"],
                "snapshot_span_ms": last["snapshot_span_ms"], "spot_usdc_total": last["spot_usdc_total"],
                "usdc_perp_unrealized_pnl": last["usdc_perp_unrealized_pnl"],
                "paired_balance_difference_usdc": _string(difference),
                "paired_check_tolerance_usdc": _string(tolerance), "paired_check_stable": stable,
                "source_urls": SOURCE_URLS}
            key = (anchor["time"], anchor["scope"], anchor["source"])
            retained = [row for row in existing if not isinstance(row, dict) or
                (row.get("time"), row.get("scope"), row.get("source")) != key]
            retained.append(anchor)
            store.set_meta("balance_anchors", retained)
            existing = retained
            meta.update(status="estimated", latest_fetch_succeeded=True,
                latest_anchor_time=anchor["time"], last_success_at_ms=clock(),
                latest_balance_usdc=anchor["balance_usdc"], validated=False,
                paired_check_stable=stable, paired_balance_difference_usdc=_string(difference),
                snapshot_span_ms=last["snapshot_span_ms"], formula=formula)
            warnings.append("USDC 余额基准由多次非原子快照扣除未实现盈亏推导，仅用于已知流水估算线；两次结果接近也不代表精确历史会计已验证。")
            log(f"已取得 USDC 账面余额估算基准；覆盖 {len(usdc_dexs)} 个 USDC DEX，两轮差额 {_string(difference)} USDC。")
    except Exception as exc:
        meta["status"] = "partial" if existing else "error"
        meta["error"] = str(exc)[:500]
        warnings.append("余额基准采集失败，保留已有基准：" + str(exc)[:300])
        log(warnings[-1])
    check()
    meta["has_cached_anchors"] = bool(existing)
    meta["anchor_count"] = len(existing)
    store.set_meta("balance_anchor_latest_raw", raw)
    store.set_meta("balance_anchor_meta", meta)
    return meta


def _anchor_time(value):
    if value is None or isinstance(value, bool) or str(value).strip() == "":
        raise ValueError("余额基准缺少 time")
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        stamp = timestamp_ms(value)
    else:
        if not number.is_finite() or number != number.to_integral_value():
            raise ValueError("余额基准 time 须为整数毫秒或 ISO 日期时间")
        stamp = int(number)
    if stamp < 0 or stamp >= 2**63:
        raise ValueError("余额基准时间超出范围")
    return stamp


def import_balance_anchors(store, path, *, log=print, cancel_check=None, now_ms=None):
    """Import explicit cash anchors, never reinterpret portfolio equity.

    JSON accepts an array or {wallet, anchors: [...]}; CSV uses the same columns.
    Every row must explicitly declare scope, time and balance_usdc (or balance).
    The file is validated in full before the cache is changed.  `validated` is
    a user assertion, not a claim that the application verified the balance.
    """
    def check():
        if cancel_check is not None:
            cancel_check()

    def check_wallet(value):
        if value is not None and str(value).strip() and wallet_address(value) != wallet_address(store.wallet):
            raise ValueError("余额基准文件的钱包地址与当前账户不同，未导入")

    check()
    path = Path(path)
    source = "import:" + path.name
    is_csv = path.suffix.lower() == ".csv"
    if is_csv:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    else:
        payload = json.loads(path.read_text(encoding="utf-8-sig"), parse_float=Decimal)
        if isinstance(payload, dict):
            check_wallet(payload.get("wallet"))
            if "anchors" not in payload:
                raise ValueError("JSON 必须包含 anchors 数组；不能把 portfolio/accountValue 当作账面余额")
            rows = payload["anchors"]
        else:
            rows = payload
    if not isinstance(rows, list) or not rows:
        raise ValueError("文件没有余额基准记录，未修改缓存")
    stamp = _clock(now_ms)
    incoming = {}
    errors = []
    for index, row in enumerate(rows, 1):
        check()
        try:
            if not isinstance(row, dict):
                raise ValueError("每条余额基准必须为对象")
            check_wallet(row.get("wallet"))
            if row.get("scope") != SCOPE:
                raise ValueError(f"必须明确声明 scope={SCOPE}；综合净值不能作为现金余额导入")
            when = _anchor_time(row.get("time"))
            value = row.get("balance_usdc") if "balance_usdc" in row else row.get("balance")
            balance = _string(_number(value, "balance_usdc"))
            if "balance_usdc" in row and "balance" in row and row["balance"] not in (None, ""):
                if _number(row["balance"], "balance") != _number(value, "balance_usdc"):
                    raise ValueError("balance 与 balance_usdc 不一致")
            validated = row.get("validated", False)
            if is_csv and isinstance(validated, str):
                text = validated.strip().lower()
                if text in ("true", "1"):
                    validated = True
                elif text in ("false", "0", ""):
                    validated = False
                else:
                    raise ValueError("validated 必须为 true 或 false")
            if not isinstance(validated, bool):
                raise ValueError("validated 必须为布尔值；省略时按未核验处理")
            quality = row.get("quality") or []
            if is_csv and isinstance(quality, str):
                quality = json.loads(quality)
            if not isinstance(quality, list) or any(not isinstance(item, str) for item in quality):
                raise ValueError("quality 必须为字符串数组")
            quality = list(dict.fromkeys([*quality, "imported_cash_anchor",
                "user_validated_anchor" if validated else "anchor_estimate"]))
            anchor = {"time": when, "balance_usdc": balance, "scope": SCOPE,
                "source": source, "validated": validated, "quality": quality,
                "wallet": wallet_address(store.wallet), "imported_at_ms": stamp}
            if row.get("source") is not None:
                anchor["declared_source"] = str(row["source"])
            key = (when, SCOPE, source)
            if key in incoming and (incoming[key]["balance_usdc"], incoming[key]["validated"]) != (balance, validated):
                raise ValueError("文件内同一时刻存在互相冲突的余额基准")
            incoming[key] = anchor
        except (ValueError, TypeError, OverflowError, InvalidOperation) as exc:
            errors.append(f"第 {index} 条：{exc}")
    if errors:
        raise ValueError(f"余额基准有 {len(errors)} 条无效记录，整份文件未导入，缓存未变。" + "；".join(errors[:5]))
    check()
    existing = list(store.get_meta("balance_anchors", []) or [])
    retained = []
    for old in existing:
        if not isinstance(old, dict):
            retained.append(old)
            continue
        key = (old.get("time"), old.get("scope"), old.get("source"))
        if key not in incoming:
            retained.append(old)
        elif old.get("balance_usdc") != incoming[key]["balance_usdc"]:
            incoming[key]["previous_balance_usdc"] = old.get("balance_usdc")
            incoming[key]["revision_count"] = int(old.get("revision_count", 0)) + 1
    retained.extend(incoming.values())
    usable = [row for row in retained if isinstance(row, dict)
        and row.get("scope") == SCOPE and isinstance(row.get("time"), int)]
    latest = max(usable, key=lambda row: (row["time"], row.get("validated") is True))
    warnings = ["导入值是用户声明的 HyperCore USDC 账面余额，不是官方账户净值。validated=true 表示用户已核验该余额及统计范围。"]
    unvalidated = sum(row["validated"] is not True for row in incoming.values())
    if unvalidated:
        warnings.append(f"{unvalidated} 个导入基准未声明已核验，仅参与已知流水估算线。")
    previous = dict(store.get_meta("balance_anchor_meta", {}) or {})
    meta = {**previous, "status": "imported", "source": source, "scope": SCOPE,
        "import_file": path.name, "imported_at_ms": stamp, "latest_import_succeeded": True,
        "last_success_at_ms": stamp, "has_cached_anchors": True,
        "anchor_count": len(retained), "imported_count": len(incoming), "rejected_count": 0,
        "imported_validated_count": len(incoming) - unvalidated,
        "latest_anchor_time": latest["time"], "latest_balance_usdc": latest.get("balance_usdc"),
        "validated": latest.get("validated") is True, "warnings": warnings, "error": None}
    check()
    store.set_meta("balance_anchors", retained)
    store.set_meta("balance_anchor_meta", meta)
    store.set_meta("balance_anchor_latest_import", {"source": source, "imported_at_ms": stamp,
        "anchors": list(incoming.values())})
    if callable(getattr(store, "commit", None)):
        store.commit()
    log(f"USDC 账面余额基准导入完成：{len(incoming)} 个；保留其他来源的历史基准。")
    return meta
