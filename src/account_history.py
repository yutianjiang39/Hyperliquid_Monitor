"""Persist official account-value samples without synthesizing missing history.

The portfolio response is sampled account equity, not a cash ledger. Each API
window is retained separately, including older high-resolution samples captured
by previous runs. Current account mode only gates the native-perp leverage
estimate; it never changes or rescales the returned account-value observations.

Sources checked 2026-11-02:
https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint
https://hyperliquid.gitbook.io/hyperliquid-docs/trading/portfolio-graphs
"""
from __future__ import annotations

import csv
import json
import re
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

from collector import decimal_key, timestamp_ms, wallet_address


WINDOW_ORDER = ("day", "week", "month", "allTime", "perpDay", "perpWeek", "perpMonth", "perpAllTime")
HISTORY_FIELDS = ("accountValueHistory", "pnlHistory")
KNOWN_MODES = {"disabled", "default", "unifiedAccount", "portfolioMargin", "dexAbstraction"}
SUPPORTED_MODES = {"disabled", "default"}
SOURCE_NOTE = "官方 portfolio 账户净值快照；包含浮动盈亏，非钱包现金余额，也非所有链上资产。"


def _check(cancel_check):
    if cancel_check is not None:
        cancel_check()


def _now(now_ms):
    return int(time.time() * 1000) if now_ms is None else int(now_ms)


def _number(value):
    if isinstance(value, bool) or value is None or str(value).strip() == "":
        raise ValueError("数值为空或不是有限数字")
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("不是有效数字") from exc
    if not number.is_finite():
        raise ValueError("数值不是有限数字")
    return decimal_key(number)


def _sample_time(value):
    # Do not silently round fractional epoch timestamps or accept True as 1.
    if isinstance(value, bool) or value is None:
        raise ValueError("无效的采样时间")
    raw = str(value).strip()
    if re.fullmatch(r"[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", raw):
        number = Decimal(raw)
        if not number.is_finite() or number != number.to_integral_value():
            raise ValueError("采样时间须为整数毫秒")
        if number < 0 or number >= 2**63:
            raise ValueError("采样时间超出范围")
        result = int(number)
    else:
        result = timestamp_ms(value)
    if result < 0 or result >= 2**63:
        raise ValueError("采样时间超出范围")
    return result


def _sorted_windows(windows):
    return sorted(windows, key=lambda key: (WINDOW_ORDER.index(key) if key in WINDOW_ORDER else len(WINDOW_ORDER), key))


def normalize_portfolio(payload):
    """Return native window pairs, warnings, and rejected-sample count.

    A malformed top-level response is an error. Malformed individual samples
    are omitted with explicit diagnostics; no missing value is replaced by 0.
    """
    if not isinstance(payload, list):
        raise ValueError("portfolio 返回值须为 [窗口名, 数据] 数组，不能把错误响应当作空历史。")
    windows, warnings, rejected = {}, [], 0
    bad_windows = 0
    for pair in payload:
        if (not isinstance(pair, (list, tuple)) or len(pair) != 2
                or not isinstance(pair[0], str) or not pair[0].strip()
                or not isinstance(pair[1], dict)):
            bad_windows += 1
            continue
        name, body = pair[0].strip(), pair[1]
        if not isinstance(body.get("accountValueHistory"), list):
            bad_windows += 1
            continue
        target = windows.setdefault(name, {field: [] for field in HISTORY_FIELDS})
        for field in HISTORY_FIELDS:
            entries = body.get(field, [])
            if not isinstance(entries, list):
                warnings.append(f"{name}.{field} 不是数组，已保留此前缓存。")
                continue
            indexed = {row[0]: row[1] for row in target[field]}
            invalid_count = 0
            for row in entries:
                try:
                    if not isinstance(row, (list, tuple)) or len(row) != 2:
                        raise ValueError("采样格式错误")
                    stamp, value = _sample_time(row[0]), _number(row[1])
                except (ValueError, TypeError, OverflowError, InvalidOperation):
                    invalid_count += 1
                    continue
                indexed[stamp] = value
            target[field] = [[stamp, indexed[stamp]] for stamp in sorted(indexed)]
            if invalid_count:
                warnings.append(f"{name}.{field} 有 {invalid_count} 个无效采样，已跳过；未补零。")
                rejected += invalid_count
        if "vlm" in body:
            try:
                target["vlm"] = _number(body["vlm"])
            except ValueError:
                warnings.append(f"{name}.vlm 无效，未将其当作金额使用。")
    if bad_windows:
        warnings.append(f"有 {bad_windows} 个窗口结构无效，已保留此前有效窗口。")
    if payload and not windows:
        raise ValueError("portfolio 没有任何可识别的账户净值窗口。")
    return [[name, windows[name]] for name in _sorted_windows(windows)], warnings, rejected


def merge_portfolio(previous, incoming, provenance, *, source, observed_at_ms):
    """Merge by (window, field, timestamp), preserving all disjoint old points."""
    old, old_warnings, _ = normalize_portfolio(previous or [])
    windows = {name: body for name, body in old}
    provenance = json.loads(json.dumps(provenance or {}, ensure_ascii=False))
    for name, body in incoming:
        target = windows.setdefault(name, {field: [] for field in HISTORY_FIELDS})
        window_provenance = provenance.setdefault(name, {})
        for field in HISTORY_FIELDS:
            indexed = {row[0]: row[1] for row in target.get(field, [])}
            field_provenance = window_provenance.setdefault(field, {})
            for stamp, value in body.get(field, []):
                key = str(stamp)
                record = dict(field_provenance.get(key, {}))
                if stamp in indexed and indexed[stamp] != value:
                    record["revision_count"] = int(record.get("revision_count", 0)) + 1
                    record["previous_value"] = indexed[stamp]
                record.setdefault("first_observed_at_ms", observed_at_ms)
                record.update(source=source, last_observed_at_ms=observed_at_ms)
                field_provenance[key] = record
                indexed[stamp] = value
            target[field] = [[stamp, indexed[stamp]] for stamp in sorted(indexed)]
        if "vlm" in body:
            # Volume is the latest API window statistic, not a sum across fetches.
            target["vlm"] = body["vlm"]
    return [[name, windows[name]] for name in _sorted_windows(windows)], provenance, old_warnings


def _counts(history):
    result = {}
    for pair in history if isinstance(history, list) else []:
        if (isinstance(pair, (list, tuple)) and len(pair) == 2
                and isinstance(pair[0], str) and isinstance(pair[1], dict)
                and isinstance(pair[1].get("accountValueHistory"), list)):
            result[pair[0]] = len(pair[1]["accountValueHistory"])
    return result


def _mode_warnings(mode, status):
    if status != "ok":
        return ["账户模式本次未核实；历史净值仍可展示，账户范围未确认的杠杆参考比率暂停计算。"]
    if mode not in SUPPORTED_MODES:
        return [f"账户模式为 {mode}；绘制已覆盖 USDC 永续仓位 / 官方账户净值参考比率，不能视为全部币种与资产的真实杠杆。"]
    return []


def _ratio_scope(mode, status):
    known = status == "ok" and mode in KNOWN_MODES
    return {
        "reference_ratio_supported": known,
        "reference_equity_scope": ("perp" if mode in SUPPORTED_MODES else "account") if known else None,
        "reference_ratio_label": ("已覆盖 USDC 永续仓位 / 官方永续净值（参考比率）" if mode in SUPPORTED_MODES
                                  else "已覆盖 USDC 永续仓位 / 官方账户净值（参考比率）") if known else "账户模式未知",
    }


def collect_account_history(store, client, *, log=print, cancel_check=None, now_ms=None):
    """Fetch public historical equity and account mode; retain cache on failure.

    Returns the persisted ``portfolio_meta`` dictionary. Recoverable endpoint
    errors never stop fill collection/reporting. Cancellation derives from
    BaseException in the local runtime and deliberately propagates unchanged.
    """
    stamp = _now(now_ms)
    previous_meta = store.get_meta("portfolio_meta", {}) or {}
    meta = dict(previous_meta)
    warnings, response, incoming = [], None, None
    response_error = None
    rejected = 0
    _check(cancel_check)
    try:
        response = client.post({"type": "portfolio", "user": store.wallet})
        _check(cancel_check)
        incoming, diagnostics, rejected = normalize_portfolio(response)
        warnings.extend(diagnostics)
    except Exception as exc:
        response_error = str(exc)
        warnings.append("账户净值历史获取失败，保留此前缓存：" + str(exc)[:240])
    _check(cancel_check)
    mode = previous_meta.get("user_abstraction")
    mode_status = "error"
    mode_error = None
    try:
        result = client.post({"type": "userAbstraction", "user": store.wallet})
        _check(cancel_check)
        if not isinstance(result, str) or result not in KNOWN_MODES:
            raise ValueError("userAbstraction 未返回已识别的账户模式。")
        mode, mode_status = result, "ok"
        meta["user_abstraction_checked_at_ms"] = stamp
        meta["user_abstraction_source"] = "api:userAbstraction"
    except Exception as exc:
        mode_error = str(exc)
        warnings.append("账户模式查询失败：" + str(exc)[:240])
    _check(cancel_check)
    scope_notes = _mode_warnings(mode, mode_status)
    warnings.extend(scope_notes)
    old_history = store.get_meta("portfolio_history", []) or []
    history = old_history
    if incoming is not None:
        try:
            history, provenance, merge_warnings = merge_portfolio(
                old_history, incoming, store.get_meta("portfolio_provenance", {}),
                source="api:portfolio", observed_at_ms=stamp)
            warnings.extend(merge_warnings)
        except (ValueError, TypeError):
            # An obsolete/malformed cache must not discard a fresh valid result.
            history, provenance, _ = merge_portfolio([], incoming, {}, source="api:portfolio", observed_at_ms=stamp)
            warnings.append("此前账户历史缓存格式无效，已用本次有效窗口重新建立缓存。")
        store.set_meta("portfolio_history", history)
        store.set_meta("portfolio_provenance", provenance)
        store.set_meta("portfolio_latest_raw", response)
        # Each fetch is kept intact: day/week/month pnlHistory restarts at zero
        # at the window start, so it may only be differenced within one fetch.
        store.put("portfolio_fetch", {"time": stamp, "windows": incoming}, "api:portfolio")
        meta["last_success_at_ms"] = stamp
        meta["source"] = "api:portfolio"
        if not any(_counts(incoming).values()):
            warnings.append("本次未返回账户净值采样；已有历史已保留，未生成虚构的零余额。")
    counts = _counts(history) if isinstance(history, list) else {}
    if incoming is None:
        status = "partial" if any(counts.values()) else "error"
    else:
        # A recognized unified-account mode changes the ratio's scope, but is
        # not itself a failed or incomplete network request.
        data_warnings = [w for w in warnings if w not in scope_notes]
        status = "partial" if data_warnings or mode_status != "ok" else "ok"
    meta.update(fetched_at_ms=stamp, status=status, warnings=list(dict.fromkeys(warnings)),
                user_abstraction=mode, user_abstraction_status=mode_status,
                scope_supported=mode_status == "ok" and mode in SUPPORTED_MODES,
                source_note=SOURCE_NOTE, sample_counts=counts, rejected_samples=rejected,
                has_cached_history=any(counts.values()),
                latest_fetch_succeeded=incoming is not None)
    meta.update(_ratio_scope(mode, mode_status))
    store.set_meta("portfolio_meta", meta)
    store.set_meta("portfolio_last_attempt", {
        "attempted_at_ms": stamp, "source": "api:portfolio", "error": response_error,
        "raw_response": response, "user_abstraction_error": mode_error,
    })
    store.commit()
    log(f"账户净值历史：{sum(counts.values())} 个窗口内采样，状态 {status}。")
    for warning in meta["warnings"]:
        log(warning)
    return meta


def _csv_portfolio(path, cancel_check):
    windows = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = set(reader.fieldnames or [])
        time_column = next((key for key in ("time", "timestamp_ms", "time_ms") if key in headers), None)
        value_column = next((key for key in ("account_value", "accountValue", "value") if key in headers), None)
        if "window" not in headers or time_column is None or value_column is None:
            raise ValueError("账户历史 CSV 需包含 window,time,account_value；time 可用毫秒时间戳或 ISO 时间。")
        for line, row in enumerate(reader, 2):
            _check(cancel_check)
            window = (row.get("window") or "").strip()
            if not window:
                raise ValueError(f"账户历史 CSV 第 {line} 行缺少 window，不能猜测净值所属范围。")
            body = windows.setdefault(window, {field: [] for field in HISTORY_FIELDS})
            # Validation is performed together with native JSON samples below.
            body["accountValueHistory"].append([row.get(time_column), row.get(value_column)])
            if row.get("pnl") not in (None, ""):
                body["pnlHistory"].append([row.get(time_column), row["pnl"]])
    return [[window, body] for window, body in windows.items()]


def import_portfolio_history(store, path, *, log=print, cancel_check=None, now_ms=None,
                             user_abstraction=None):
    """Import native portfolio JSON or CSV ``window,time,account_value[,pnl]``.

    JSON may optionally be an envelope containing ``portfolio``, ``wallet``
    (or ``user``), and ``user_abstraction``. An absent mode stays unknown unless
    a previously verified mode is available; imported samples never assume a
    zero balance or invent account scope.
    """
    path, stamp = Path(path), _now(now_ms)
    _check(cancel_check)
    envelope = None
    if path.suffix.lower() == ".csv":
        response = _csv_portfolio(path, cancel_check)
    else:
        response = json.loads(path.read_text(encoding="utf-8-sig"), parse_float=Decimal)
        if isinstance(response, dict) and "portfolio" in response:
            envelope, response = response, response["portfolio"]
            supplied_wallet = envelope.get("wallet") or envelope.get("user")
            if supplied_wallet and wallet_address(supplied_wallet) != store.wallet:
                raise ValueError("账户净值文件中的钱包地址与当前缓存不同，未导入。")
            if user_abstraction is None:
                user_abstraction = envelope.get("user_abstraction")
    _check(cancel_check)
    incoming, warnings, rejected = normalize_portfolio(response)
    if not any(_counts(incoming).values()):
        raise ValueError("该文件没有有效的账户净值采样，未修改已有历史。")
    prior = store.get_meta("portfolio_meta", {}) or {}
    mode = user_abstraction if user_abstraction is not None else prior.get("user_abstraction")
    if user_abstraction is not None and user_abstraction not in KNOWN_MODES:
        raise ValueError("user_abstraction 不属于已识别的账户模式，未导入。")
    # A supplied/imported mode is a stated source scope; otherwise retain only
    # a successfully verified prior mode, never revive a failed/stale check.
    mode_status = "ok" if (user_abstraction is not None or prior.get("user_abstraction_status") == "ok") else "unknown"
    source = "import:" + path.name
    history, provenance, diagnostics = merge_portfolio(
        store.get_meta("portfolio_history", []), incoming,
        store.get_meta("portfolio_provenance", {}), source=source, observed_at_ms=stamp)
    warnings.extend(diagnostics)
    scope_notes = _mode_warnings(mode, mode_status)
    warnings.extend(scope_notes)
    meta = dict(prior)
    meta.update(fetched_at_ms=stamp, last_success_at_ms=stamp,
                status="partial" if any(w not in scope_notes for w in warnings) or mode_status != "ok" else "ok",
                warnings=list(dict.fromkeys(warnings)),
                source=source, source_note=SOURCE_NOTE, sample_counts=_counts(history),
                rejected_samples=rejected, has_cached_history=True,
                user_abstraction=mode, user_abstraction_status=mode_status,
                scope_supported=mode_status == "ok" and mode in SUPPORTED_MODES,
                latest_fetch_succeeded=True, import_file=path.name)
    meta.update(_ratio_scope(mode, mode_status))
    if user_abstraction is not None:
        meta["user_abstraction_source"] = source
        meta["user_abstraction_checked_at_ms"] = stamp
    _check(cancel_check)
    store.set_meta("portfolio_history", history)
    store.set_meta("portfolio_provenance", provenance)
    store.set_meta("portfolio_latest_raw", response)
    store.set_meta("portfolio_meta", meta)
    store.set_meta("portfolio_last_attempt", {"attempted_at_ms": stamp, "source": source,
                                              "error": None, "raw_response": response})
    store.commit()
    log(f"账户净值导入完成：{sum(_counts(incoming).values())} 个采样；保留全部不同时间的历史缓存。")
    for warning in meta["warnings"]:
        log(warning)
    return meta


# ---------------------------------------------------------------------------
# Official series: stitched equity and absolute cumulative PnL (ground truth)
# ---------------------------------------------------------------------------
FINE_TO_COARSE = {"total": ("day", "week", "month", "allTime"),
                  "perp": ("perpDay", "perpWeek", "perpMonth", "perpAllTime")}


def _fetch_points(windows, names):
    """Absolute (t, equity, cum_pnl, window) points for one fetch.

    All windows of one fetch end at the same latest sample, so relative window
    PnL is anchored to the all-time cumulative value at that shared end point.
    Finer windows replace coarser points inside their own time span.
    """
    by_name = {name: body for name, body in windows if isinstance(body, dict)}
    coarse = by_name.get(names[-1])
    if not coarse or not coarse.get("pnlHistory"):
        return []
    anchor_pnl = Decimal(str(coarse["pnlHistory"][-1][1]))
    points, cutoff = {}, None
    for name in names:  # finest first
        body = by_name.get(name)
        if not body:
            continue
        equity = {int(t): Decimal(str(v)) for t, v in body.get("accountValueHistory", [])}
        pnl = [(int(t), Decimal(str(v))) for t, v in body.get("pnlHistory", [])]
        if not pnl:
            continue
        offset = anchor_pnl - pnl[-1][1]
        first = pnl[0][0]
        for t, v in pnl:
            if cutoff is not None and t >= cutoff:
                continue
            if t in points:
                continue
            points[t] = {"time": t, "equity": equity.get(t), "cum_pnl": v + offset, "window": name}
        cutoff = first if cutoff is None else min(cutoff, first)
    return [points[t] for t in sorted(points)]


def official_series(store, scope="total"):
    """Merge all cached portfolio fetches into one absolute series.

    Later fetches win on overlapping timestamps; earlier fetches keep the finer
    historical samples that the API has since coarsened.
    """
    names = FINE_TO_COARSE[scope]
    merged = {}
    for row in store.rows("portfolio_fetch"):
        for point in _fetch_points(row.get("windows") or [], names):
            point["fetched_at_ms"] = row["time"]
            merged[point["time"]] = point
    if not merged:
        # Legacy cache: only the merged allTime window is trustworthy for PnL.
        legacy = store.get_meta("portfolio_history", []) or []
        legacy = [[n, b] for n, b in legacy if n == names[-1]]
        for point in _fetch_points(legacy, names[-1:]):
            point["fetched_at_ms"] = None
            merged[point["time"]] = point
    return [merged[t] for t in sorted(merged)]


def latest_window_stats(store):
    """Official PnL and volume per window from the newest fetch."""
    rows = list(store.rows("portfolio_fetch"))
    if not rows:
        history = store.get_meta("portfolio_history", []) or []
        windows = history
    else:
        windows = rows[-1].get("windows") or []
    out = {}
    for name, body in windows:
        pnl = body.get("pnlHistory") or []
        equity = body.get("accountValueHistory") or []
        out[name] = {
            "pnl": str(Decimal(str(pnl[-1][1])) - Decimal(str(pnl[0][1]))) if len(pnl) >= 1 else None,
            "cum_pnl_end": str(pnl[-1][1]) if pnl else None,
            "equity_end": str(equity[-1][1]) if equity else None,
            "equity_start": str(equity[0][1]) if equity else None,
            "start_ms": int(equity[0][0]) if equity else None,
            "end_ms": int(equity[-1][0]) if equity else None,
            "vlm": body.get("vlm"),
            "samples": len(equity),
        }
    return out
