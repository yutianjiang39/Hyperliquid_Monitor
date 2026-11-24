"""Shared, resumable local runtime for CLI and Textual terminal interface.

No desktop/browser server, private keys, trading endpoints or AI API is needed.
Each job owns its SQLite connection on the worker thread. Derived display rows
are indexed once; the terminal queries at most one small page at a time.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import copy
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
from collector import (HistoryStore, InfoClient, S3_BUCKET, S3_PREFIXES, HOUR_MS,
                       wallet_address, date_ms, object_hour, collect_api,
                       inventory_archives, scan_archives, import_history_file)
from pipeline import generate_report as build_history_report
from account_history import collect_account_history, import_portfolio_history
from sources.market_data import fetch_market_meta
from sources.snapshots import collect_snapshot
from sources.account_extras import collect_extras
from sources.leaderboard import fetch_leaderboard_row
from sources import DEFAULT_PLUGIN_CONFIG, active_plugins, plugin_status
from analysis import analyze_wallet
from sources.backfill import run_backfill

VERSION = "3.1.0"
DEFAULT_CONFIG = {
    "version": VERSION, "wallet": "", "start_date_utc": "", "end_date_utc": "",
    "data_dir": "data", "output_dir": "reports", "chunk_size": 5000,
    "incremental_api": True, "leaderboard": False, "include_subaccounts": False, "plugins": DEFAULT_PLUGIN_CONFIG,
    "aws": {"profile": "", "region": "us-east-1", "max_download_gib": 10.0, "max_objects": 0},
    "mae": False,
    # Paid requester-pays archives; used only when enabled AND AWS credentials resolve.
    "backfill": {"enabled": False, "sources": ["reservoir", "official_s3"], "max_cost_usd": 5.0, "mode": "gaps",
                 "reservoir_bucket": "hydromancer-reservoir", "reservoir_region": "ap-northeast-1",
                 "reservoir_prefix": ""},
}
BACKFILL_SOURCES = ("official_s3", "reservoir")
DATASETS = ("operations", "campaigns", "ledger", "funding", "coverage")


class Cancelled(BaseException):
    """Cooperative cancellation; not mistaken for a recoverable API/schema error."""


def check_cancel(event=None):
    if event is not None and event.is_set():
        raise Cancelled("任务已停止，已缓存内容保留。")


def safe_message(value):
    text = str(value)
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        secret = os.environ.get(key)
        if secret:
            text = text.replace(secret, "[已隐藏凭证]")
    text = re.sub(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b", "[已隐藏凭证]", text)
    text = re.sub(r"(?i)(X-Amz-(?:Signature|Security-Token)|SecretAccessKey|SessionToken)([=:]\s*)[^\s&\"']+", r"\1\2[已隐藏]", text)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    return "".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 32)


def atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex[:8] + ".tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False, default=str), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def validate_config(value):
    if not isinstance(value, dict):
        raise ValueError("配置须是 JSON 对象。")
    unknown = set(value) - set(DEFAULT_CONFIG)
    if unknown:
        raise ValueError("未识别的配置字段：" + ", ".join(sorted(unknown)) + "。AWS 凭证请放标准环境变量或 profile。")
    out = copy.deepcopy(DEFAULT_CONFIG)
    out.update({k: v for k, v in value.items() if k not in ("aws", "plugins", "backfill")})
    backfill = value.get("backfill", {})
    if not isinstance(backfill, dict) or set(backfill) - set(DEFAULT_CONFIG["backfill"]):
        raise ValueError("backfill 只接受 " + "、".join(DEFAULT_CONFIG["backfill"]) + "；AWS 密钥请放 profile 或环境变量。")
    out["backfill"].update(backfill)
    bf = out["backfill"]
    if not isinstance(bf["enabled"], bool):
        raise ValueError("backfill.enabled 须为 true/false。")
    if not isinstance(bf["sources"], list) or not set(bf["sources"]) <= set(BACKFILL_SOURCES):
        raise ValueError("backfill.sources 只能包含 " + "、".join(BACKFILL_SOURCES) + "。")
    cost = bf["max_cost_usd"]
    if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost < 0:
        raise ValueError("backfill.max_cost_usd 须为非负数（美元）。")
    if bf["mode"] not in ("gaps", "full"):
        raise ValueError("backfill.mode 只能是 gaps 或 full。")
    for k in ("reservoir_bucket", "reservoir_region", "reservoir_prefix"):
        if not isinstance(bf[k], str):
            raise ValueError(f"backfill.{k} 必须为字符串。")
    plugins = value.get("plugins", {})
    if not isinstance(plugins, dict) or set(plugins) - set(DEFAULT_CONFIG["plugins"]) or any(
            not isinstance(v, dict) or set(v) - set(DEFAULT_CONFIG["plugins"][k]) for k, v in plugins.items()):
        raise ValueError("plugins 只接受 hydromancer{enabled,url} 与 dune{enabled,query_id}；API key 请放环境变量。")
    for name, settings in plugins.items():
        out["plugins"][name].update(settings)
    aws = value.get("aws", {})
    if not isinstance(aws, dict) or set(aws) - set(DEFAULT_CONFIG["aws"]):
        raise ValueError("aws 只接受 profile、region、max_download_gib、max_objects；不要在配置中写入密钥。")
    out["aws"].update(aws)
    for field in ("wallet", "start_date_utc", "end_date_utc", "data_dir", "output_dir"):
        if not isinstance(out[field], str):
            raise ValueError(f"{field} 必须为字符串。")
        out[field] = out[field].strip()
    if out["wallet"]:
        out["wallet"] = wallet_address(out["wallet"])
    for field in ("start_date_utc", "end_date_utc"):
        if out[field] and date_ms(out[field]) < 0:
            raise ValueError("日期不可早于 1970 年。")
    if out["start_date_utc"] and out["end_date_utc"] and date_ms(out["start_date_utc"]) > date_ms(out["end_date_utc"]):
        raise ValueError("开始日期不可晚于结束日期。")
    if not out["data_dir"] or not out["output_dir"]:
        raise ValueError("缓存与报告目录不可为空。")
    n = out["chunk_size"]
    if isinstance(n, bool) or not isinstance(n, int) or not 100 <= n <= 20000:
        raise ValueError("chunk_size 须为 100–20000 的整数。")
    for flag in ("incremental_api", "leaderboard", "include_subaccounts", "mae"):
        if not isinstance(out[flag], bool):
            raise ValueError(flag + " 须为 true/false。")
    for k in ("profile", "region"):
        if out["aws"][k] is None and k == "profile":
            out["aws"][k] = ""
        if not isinstance(out["aws"][k], str):
            raise ValueError("AWS profile/region 必须为字符串。")
        out["aws"][k] = out["aws"][k].strip()
    if not out["aws"]["region"]:
        raise ValueError("AWS region 不可为空。")
    budget = out["aws"]["max_download_gib"]
    if isinstance(budget, bool) or not isinstance(budget, (int, float)) or not math.isfinite(budget) or budget <= 0:
        raise ValueError("下载预算须为有限的正数 GiB。")
    cap = out["aws"]["max_objects"]
    if isinstance(cap, bool) or not isinstance(cap, int) or cap < 0:
        raise ValueError("max_objects 须为非负整数，0 表示不限文件数。")
    out["version"] = VERSION
    return out


def load_config(path):
    path = Path(path)
    if not path.exists():
        return copy.deepcopy(DEFAULT_CONFIG)
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError) as exc:
        raise ValueError(f"无法读取配置 {path.name}：{safe_message(exc)}") from exc
    return validate_config(value)


class WalletLock:
    """OS advisory lock; an abandoned lock file never blocks future runs."""

    def __init__(self, path):
        self.path, self.handle = Path(path), None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0, os.SEEK_END)
                if self.handle.tell() == 0:
                    self.handle.write(b"0")
                    self.handle.flush()
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise RuntimeError("另一个任务正在使用该钱包缓存。请等它结束后再运行。") from exc
        return self

    def __exit__(self, *args):
        if self.handle is not None:
            try:
                if os.name == "nt":
                    import msvcrt
                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            finally:
                self.handle.close()


class LocalInfoClient(InfoClient):
    """Short network timeout; cancel also interrupts throttling/retry waits."""

    def __init__(self, cancel=None):
        super().__init__(timeout=(5, 20), retries=3)
        self.cancel_event = cancel

    def _wait(self, seconds):
        if self.cancel_event is not None:
            if self.cancel_event.wait(max(0, seconds)):
                check_cancel(self.cancel_event)
        else:
            time.sleep(max(0, seconds))

    def post(self, body):
        import requests
        check_cancel(self.cancel_event)
        self._wait(max(0, self.next_request - time.monotonic()))
        for attempt in range(self.retries):
            check_cancel(self.cancel_event)
            try:
                r = self.session.post(self.url, json=body, timeout=self.timeout)
                check_cancel(self.cancel_event)
                if r.status_code == 429 or r.status_code >= 500:
                    if attempt == self.retries - 1:
                        r.raise_for_status()
                    self._wait(min(15, 2 ** (attempt + 1)))
                    continue
                r.raise_for_status()
                out = r.json(parse_float=Decimal)
                if ((isinstance(out, str) and body.get("type") != "userAbstraction")
                        or (isinstance(out, dict) and ("error" in out or out.get("status") == "err"))):
                    raise RuntimeError("Info 查询返回错误：" + safe_message(out)[:240])
                count = len(out) if isinstance(out, list) else 0
                self.next_request = time.monotonic() + (20 + math.ceil(count / 20)) * 60 / 1000
                return out
            except (requests.Timeout, requests.ConnectionError):
                check_cancel(self.cancel_event)
                if attempt == self.retries - 1:
                    raise
                self._wait(min(15, 2 ** (attempt + 1)))
        raise RuntimeError("Info 请求重试耗尽。")


class CheckedBody:
    def __init__(self, body, event):
        self.body, self.event = body, event

    def read(self, *args, **kwargs):
        check_cancel(self.event)
        try:
            result = self.body.read(*args, **kwargs)
        except Exception as exc:
            # The collector retains transport errors in audit exports. Scrub
            # credential-bearing URLs before those errors cross that boundary.
            raise RuntimeError(safe_message(exc)) from None
        check_cancel(self.event)
        return result

    def readinto(self, buffer):
        data = self.read(len(buffer))
        buffer[:len(data)] = data
        return len(data)

    def readable(self):
        return True

    def seekable(self):
        return False

    def close(self):
        self.body.close()

    def __getattr__(self, name):
        return getattr(self.body, name)


class CheckedS3:
    def __init__(self, client, cancel=None):
        self.client, self.cancel = client, cancel

    def list_objects_v2(self, **kwargs):
        check_cancel(self.cancel)
        try:
            result = self.client.list_objects_v2(**kwargs)
        except Exception as exc:
            raise RuntimeError(safe_message(exc)) from None
        check_cancel(self.cancel)
        return result

    def get_object(self, **kwargs):
        check_cancel(self.cancel)
        try:
            result = self.client.get_object(**kwargs)
        except Exception as exc:
            raise RuntimeError(safe_message(exc)) from None
        try:
            check_cancel(self.cancel)
        except Cancelled:
            result["Body"].close()
            raise
        result["Body"] = CheckedBody(result["Body"], self.cancel)
        return result


@dataclass
class Job:
    store: HistoryStore
    wallet: str
    directory: Path
    start: int
    end: int
    log: object
    check: object
    status: str = "completed"


class Runtime:
    def __init__(self, config_path=None, *, info_factory=None, s3_factory=None, clock=None):
        self.config_path = Path(config_path or ROOT / "config.json").expanduser().resolve()
        self.base = self.config_path.parent
        self.config = load_config(self.config_path)
        self.info_factory = info_factory
        self.s3_factory = s3_factory
        self.clock = clock or (lambda: int(time.time() * 1000))
        self._busy = threading.Lock()
        # Windows cannot replace a SQLite file while a preview connection has
        # it open. Serialize only reader lifetimes and the final atomic swap;
        # constructing a new index remains concurrent with reading the old one.
        self._view_lock = threading.Lock()
        self.last_result = None
        self.last_plan = None
        # Per-invocation CLI switches; they never persist into config.json.
        self.plugin_override = None
        self.leaderboard_override = False
        self.subaccounts_override = False
        self.mae_override = False
        self._view_path = None
        self._reload_state()

    def _path(self, value):
        value = Path(value).expanduser()
        return (value if value.is_absolute() else self.base / value).resolve()

    def _wallet_dir(self, wallet=None):
        return self._path(self.config["data_dir"]) / (wallet or wallet_address(self.config["wallet"]))

    def _reload_state(self):
        self.last_result, self.last_plan, self._view_path = None, None, None
        if not self.config["wallet"]:
            return
        directory = self._wallet_dir()
        self._view_path = directory / "views.sqlite"
        for name, attr in (("last_report.json", "last_result"), ("archive_plan.json", "last_plan")):
            path = directory / name
            if path.is_file():
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(value, dict) and value.get("wallet") == self.config["wallet"]:
                        if name == "last_report.json":
                            for key in ("html", "zip", "views_db"):
                                relative = value.get(key + "_relative")
                                if relative:
                                    candidate = (self.base / relative).resolve()
                                    if candidate.is_relative_to(self.base) and candidate.is_file():
                                        value[key] = str(candidate)
                        setattr(self, attr, value)
                except (ValueError, OSError):
                    pass

    def configure(self, updates):
        if not self._busy.acquire(blocking=False):
            raise RuntimeError("任务运行期间不能更改钱包与目录；请先停止或等待完成。")
        try:
            updated = copy.deepcopy(self.config)
            for key, value in updates.items():
                if key == "aws" and isinstance(value, dict):
                    updated["aws"].update(value)
                else:
                    updated[key] = value
            updated = validate_config(updated)
            changed_context = any(updated[k] != self.config[k] for k in ("wallet", "data_dir", "output_dir"))
            atomic_json(self.config_path, updated)
            self.config = updated
            if changed_context:
                self._reload_state()
            return copy.deepcopy(updated)
        finally:
            self._busy.release()

    def _range(self):
        start, end = date_ms(self.config["start_date_utc"], 0), date_ms(self.config["end_date_utc"], self.clock())
        if end < start:
            raise ValueError("开始日期不能晚于结束日期。")
        return start, end

    @contextmanager
    def _job(self, name, log, cancel, *, demo=False):
        if not self._busy.acquire(blocking=False):
            raise RuntimeError("已有任务运行中，请等待或先停止。")
        store = None
        logger = lambda message: log(safe_message(message))
        try:
            check_cancel(cancel)
            wallet = "0x" + "11" * 20 if demo else wallet_address(self.config["wallet"])
            directory = self._path(self.config["data_dir"]) / "demo" / wallet if demo else self._wallet_dir(wallet)
            directory.mkdir(parents=True, exist_ok=True)
            start, end = (0, self.clock()) if demo else self._range()
            with WalletLock(directory / ".run.lock"):
                store = HistoryStore(directory / "wallet_history.sqlite", wallet)
                check = lambda: check_cancel(cancel)
                def checked_log(message):
                    check()
                    logger(message)
                job = Job(store, wallet, directory, start, end, checked_log, check)
                state = {"task": name, "status": "running", "start_ms": start, "end_ms": end, "started_at_ms": self.clock()}
                store.set_meta("local_job", state)
                store.commit()
                try:
                    yield job
                    state.update(status=job.status, finished_at_ms=self.clock())
                except (Cancelled, KeyboardInterrupt):
                    state.update(status="interrupted", finished_at_ms=self.clock())
                    store.set_meta("local_interruption", state)
                    if name == "api":
                        store.set_meta("last_api_run", {"start_ms": start, "end_ms": end,
                            "results": [{"endpoint": "local_task", "status": "interrupted", "exhausted": False,
                                         "warning": "任务中断；已提交的分页数据保留，下一次可继续查询。"}]})
                    if name == "scan":
                        store.set_meta("last_archive_scan", {"status": "interrupted", "note": "已完成对象保留；未完成对象下次重扫并去重。"})
                    logger("已停止。已完成的缓存和归档检查点已保留。")
                    raise Cancelled("任务已停止。")
                except Exception as exc:
                    state.update(status="failed", error=safe_message(exc)[:500], finished_at_ms=self.clock())
                    store.set_meta("local_last_failure", state)
                    raise
                finally:
                    # A scan resumes the reviewed plan's frozen range, which
                    # can differ from the current wall clock used at job entry.
                    state.update(start_ms=job.start, end_ms=job.end)
                    store.set_meta("local_job", state)
                    store.commit()
        finally:
            try:
                if store is not None:
                    store.close()
            finally:
                self._busy.release()

    def _info(self, cancel):
        return self.info_factory(cancel) if self.info_factory else LocalInfoClient(cancel)

    def _s3(self, cancel):
        if self.s3_factory:
            return CheckedS3(self.s3_factory(cancel), cancel)
        import boto3
        from botocore.config import Config
        os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
        profile = self.config["aws"]["profile"] or None
        session = boto3.Session(profile_name=profile, region_name=self.config["aws"]["region"])
        if session.get_credentials() is None:
            raise RuntimeError("未找到 AWS 凭证。请配置标准 AWS profile 或环境变量；公开 API 与演示无需这些凭证。")
        return CheckedS3(session.client("s3", config=Config(retries={"max_attempts": 2, "mode": "standard"}, connect_timeout=5, read_timeout=20)), cancel)

    def _plan(self, wallet, start, end, *, strict=False):
        path = self._wallet_dir(wallet) / "archive_plan.json"
        if not path.is_file():
            return None
        try:
            plan = json.loads(path.read_text(encoding="utf-8"))
            validate_plan(plan, wallet)
            if plan["start_ms"] != start:
                if strict:
                    raise ValueError("开始日期已变更，请重新列出归档清单。")
                return None
            if strict and self.config["end_date_utc"] and plan["end_ms"] != end:
                raise ValueError("结束日期已变更，请重新列出归档清单。")
            return plan
        except (ValueError, TypeError, KeyError) as exc:
            if strict:
                raise ValueError("归档清单无效或与当前配置不一致：" + safe_message(exc)) from exc
            return None

    def _write_views(self, directory, analysis, ledger, funding, coverage, check):
        path = directory / "views.sqlite"
        tmp = directory / ("views_" + uuid.uuid4().hex + ".tmp.sqlite")
        db = sqlite3.connect(tmp)
        try:
            db.executescript("""
                CREATE TABLE display_rows(dataset TEXT, seq INTEGER, time INTEGER, coin TEXT, search TEXT, payload TEXT);
                CREATE INDEX by_dataset_time ON display_rows(dataset,time DESC,seq DESC);
                CREATE INDEX by_dataset_coin ON display_rows(dataset,coin,time DESC);
                CREATE TABLE metadata(key TEXT PRIMARY KEY,payload TEXT);
            """)
            datasets = {"operations": analysis["operations"], "campaigns": analysis["campaigns"], "ledger": ledger, "funding": funding,
                        "coverage": [{"section": k, "value": json.dumps(v, ensure_ascii=False, default=str)} for k, v in coverage.items()]}
            for dataset, rows in datasets.items():
                batch = []
                for index, row in enumerate(rows):
                    if index % 250 == 0:
                        check()
                    delta = row.get("delta", {})
                    stamp = row.get("time") or row.get("close_time") or row.get("open_time") or 0
                    coin = str(row.get("coin") or row.get("token") or delta.get("coin") or delta.get("token") or "")
                    search = " ".join(str(row.get(k, "")) for k in ("coin", "type", "action", "action_label", "status", "direction", "hash", "oid", "tid", "counterparty", "section", "value")).lower()
                    search += " " + coin.lower()
                    batch.append((dataset, index, int(stamp), coin, search, json.dumps(row, ensure_ascii=False, default=str)))
                    if len(batch) == 1000:
                        db.executemany("INSERT INTO display_rows VALUES(?,?,?,?,?,?)", batch)
                        batch.clear()
                if batch:
                    db.executemany("INSERT INTO display_rows VALUES(?,?,?,?,?,?)", batch)
            db.execute("INSERT INTO metadata VALUES(?,?)", ("generated_at_ms", str(self.clock())))
            db.commit()
            check()
            db.close()
            with self._view_lock:
                check()
                os.replace(tmp, path)
                self._view_path = path
        finally:
            db.close()
            tmp.unlink(missing_ok=True)

    def _build(self, job, *, inventory=None, demo=False, client=None):
        job.check()
        folder = ("demo_" if demo else job.wallet[2:10] + "_") + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:5]
        output = self._path(self.config["output_dir"]) / folder
        warnings = []
        if demo:
            warnings.append("模拟数据示例；本报告不是实际钱包数据。")
        if inventory and inventory["end_ms"] < job.end:
            warnings.append("归档清单的截止时间早于报告截止时间；新时段需重新列清单并补抓。")
        interrupted = job.store.get_meta("local_interruption")
        if interrupted:
            warnings.append("本地曾有中断任务；已完成内容保留，请结合 API/归档状态判断是否已补齐。")
        result = build_history_report(job.store, output, job.start, job.end, inventory=inventory, client=client,
            title="模拟数据演示 · Hyperliquid 钱包分析" if demo else None,
            chunk_size=self.config["chunk_size"], log=job.log, cancel_check=job.check, mae=self._mae(),
            analysis_sink=lambda a, l, f, c: self._write_views(job.directory, a, l, f, c, job.check),
            coverage_extra={"runtime_version": VERSION, "last_interruption": interrupted,
                            "last_failure": job.store.get_meta("local_last_failure"), "warnings": warnings, "demo": demo})
        result.update(wallet=job.wallet, generated_at_ms=self.clock(), status=job.status, demo=demo,
                      views_db=str(job.directory / "views.sqlite"))
        # Preserve convenient absolute paths during this run and portable paths
        # when the entire local package is moved to another computer/folder.
        for key in ("html", "zip", "views_db"):
            path = Path(result[key]).resolve()
            if path.is_relative_to(self.base):
                result[key + "_relative"] = path.relative_to(self.base).as_posix()
        atomic_json(job.directory / "last_report.json", result)
        self.last_result = result
        return result

    def run_api(self, log=print, cancel=None):
        with self._job("api", log, cancel) as job:
            client = self._info(cancel)
            try:
                results = collect_api(job.store, job.start, job.end, client=client,
                    incremental=self.config["incremental_api"], log=job.log, cancel_check=job.check)
                partial = any(r.get("status") != "api_visible_exhausted" or r.get("gaps") for r in results)
                job.status = "partial" if partial else "completed"
                if partial:
                    job.log("部分 API 数据未完整返回，报告将保留错误与覆盖提示。")
                self._refresh_live(job, client)
                return self._build(job, inventory=self._plan(job.wallet, job.start, job.end), client=client)
            finally:
                if getattr(client, "session", None) is not None:
                    client.session.close()

    def plan_archives(self, log=print, cancel=None):
        with self._job("plan", log, cancel) as job:
            job.log("官方 AWS 归档清单：列目录也可能产生请求费用；此步骤不会下载数据对象。")
            client = self._s3(cancel)
            plan = inventory_archives(job.store, job.start, job.end, client=client, log=job.log, cancel_check=job.check)
            plan.update(wallet=job.wallet, planned_at_ms=self.clock(), plan_version=1)
            validate_plan(plan, job.wallet)
            job.status = "partial" if plan["errors"] else "completed"
            plan["status"] = job.status
            atomic_json(job.directory / "archive_plan.json", plan)
            self.last_plan = plan
            return plan

    def scan_archives(self, log=print, cancel=None, accept_requester_pays=False):
        if not accept_requester_pays:
            raise ValueError("请先查看清单体积，并明确确认 requester-pays 下载。")
        with self._job("scan", log, cancel) as job:
            plan = self._plan(job.wallet, job.start, job.end, strict=True)
            if plan is None:
                raise ValueError("尚无归档清单，请先运行“归档清单”。")
            # The plan deliberately freezes its end time. A later run resumes
            # exactly that plan, not an unreviewed newly expanded set of files.
            job.start, job.end = plan["start_ms"], plan["end_ms"]
            for obj in plan["objects"]:
                job.store.register_object(obj)
            job.store.commit()
            result = scan_archives(job.store, plan, client=self._s3(cancel), accept_requester_pays=True,
                max_download_gib=self.config["aws"]["max_download_gib"], max_objects=self.config["aws"]["max_objects"],
                log=job.log, cancel_check=job.check)
            for obj in plan["objects"]:
                obj["status"] = job.store.object_status(obj)
            plan["pending_bytes"] = sum(o["size"] for o in plan["objects"] if o["status"] != "done")
            atomic_json(job.directory / "archive_plan.json", plan)
            self.last_plan = plan
            job.status = "paused" if result["status"] == "budget_pause" else "partial" if (result.get("failed_objects") or plan["errors"] or plan.get("unknown_keys")) else "completed"
            report = self._build(job, inventory=plan)
            report["scan"] = result
            return report

    def generate_report(self, log=print, cancel=None):
        with self._job("report", log, cancel) as job:
            # Wallet data stays cache-only; MAE/MFE may still download public candles.
            client = self._info(cancel) if self._mae() else None
            try:
                return self._build(job, inventory=self._plan(job.wallet, job.start, job.end), client=client)
            finally:
                if getattr(client, "session", None) is not None:
                    client.session.close()

    def _mae(self):
        return bool(self.config["mae"] or self.mae_override)

    def _run_plugins(self, job, only=None):
        """Backfill streams older than the official API window from keyed plugins."""
        for plugin in active_plugins(self.config, only):
            for kind in sorted(plugin.capabilities):
                first = next(iter(job.store.rows(kind)), None)
                stop = first["time"] - 1 if first else job.end
                if stop <= job.start:
                    continue
                job.log(f"插件 {plugin.name}：补抓 {kind}（官方最早记录之前）… {plugin.cost_hint}")
                try:
                    plugin.fetch(job.store, kind, job.start, stop, log=job.log, cancel_check=job.check)
                except Exception as exc:
                    job.status = "partial"
                    job.log(f"插件 {plugin.name} {kind} 失败：" + safe_message(exc))

    def _collect_subaccounts(self, job, client):
        """Collect and analyze each sub-account in its own cache; summarize on the master.

        Sub-accounts are separate HyperCore accounts with their own official
        portfolio, so each is reconciled on its own and only summed for display.
        """
        snapshot = job.store.get_meta("latest_snapshot") or {}
        rows = []
        for sub in snapshot.get("sub_accounts", []):
            address = sub.get("address")
            if not address:
                continue
            job.log(f"子账户 {sub.get('name') or ''} {address}：抓取与分析 …")
            directory = self._wallet_dir(address)
            directory.mkdir(parents=True, exist_ok=True)
            store = HistoryStore(directory / "wallet_history.sqlite", address)
            try:
                sub_job = Job(store, address, directory, job.start, job.end, job.log, job.check)
                results = collect_api(store, job.start, job.end, client=client,
                    incremental=self.config["incremental_api"], log=job.log, cancel_check=job.check)
                if any(r.get("status") != "api_visible_exhausted" or r.get("gaps") for r in results):
                    job.status = "partial"
                self._refresh_live(sub_job, client, secondary=False)
                model, _ = analyze_wallet(store, client, log=job.log, cancel_check=job.check)
                rec, trunc = model["quality"]["reconciliation"], model["quality"]["truncation"]
                rows.append({"address": address, "name": sub.get("name"),
                             "official_pnl": (model["official"]["windows"].get("allTime") or {}).get("pnl"),
                             "equity": model["current"].get("equity"), "twr": model["performance"].get("twr"),
                             "max_drawdown": model["risk"].get("max_drawdown"),
                             "volume": (model["official"]["windows"].get("allTime") or {}).get("vlm"),
                             "fills": model["engine_summary"].get("unique_fill_count"),
                             "reconciliation": rec.get("status"), "residual_pct": rec.get("residual_pct"),
                             "history": trunc.get("status")})
                if sub_job.status != "completed":
                    job.status = "partial"
            except Cancelled:
                raise
            except Exception as exc:
                job.status = "partial"
                job.log(f"子账户 {address} 失败：" + safe_message(exc))
                rows.append({"address": address, "name": sub.get("name"), "error": safe_message(exc)[:200]})
            finally:
                store.close()
        job.store.set_meta("sub_account_summary", rows)
        job.store.commit()
        return rows

    def _refresh_live(self, job, client, secondary=True):
        """Fetch the official truth layers: market meta, portfolio, snapshot, extras.

        Fills/funding/ledger are bridged up to the snapshot time so the position
        chain ends where the snapshot starts, even for a historical report end.
        No AWS requests are made here.
        """
        fetch_market_meta(client, job.store, log=job.log)
        account = collect_account_history(job.store, client, log=job.log,
            cancel_check=job.check, now_ms=self.clock())
        if account.get("status") != "ok":
            job.status = "partial"
        snapshot = collect_snapshot(job.store, client, spot_prices=job.store.get_meta("spot_prices", {}),
            log=job.log, cancel_check=job.check, now_ms=self.clock())
        if snapshot.get("errors"):
            job.status = "partial"
        if snapshot["time"] > job.end:
            job.log("增量补齐成交、资金费和转账至快照时刻，使仓位链与快照衔接 …")
            results = collect_api(job.store, job.start, snapshot["time"], client=client,
                incremental=True, log=job.log, cancel_check=job.check)
            if any(r.get("status") != "api_visible_exhausted" or r.get("gaps") for r in results):
                job.status = "partial"
        collect_extras(job.store, client, log=job.log, cancel_check=job.check)
        if not secondary:
            job.store.commit()
            return snapshot
        self._run_plugins(job, self.plugin_override)
        try:
            run_backfill(job.store, self.config, log=job.log, cancel_check=job.check)
        except Cancelled:
            raise
        except Exception as exc:
            job.status = "partial"
            job.log("付费补数失败（已缓存内容保留）：" + safe_message(exc))
        if self.config["include_subaccounts"] or self.subaccounts_override:
            self._collect_subaccounts(job, client)
        if self.config["leaderboard"] or self.leaderboard_override:
            try:
                fetch_leaderboard_row(job.store, log=job.log)
            except Exception as exc:
                job.log("排行榜核对失败（可选数据，不影响报告）：" + safe_message(exc))
        job.store.commit()
        return snapshot

    def refresh_assets(self, log=print, cancel=None):
        """Refresh the official portfolio and snapshot, and incrementally bridge flows."""
        with self._job("assets", log, cancel) as job:
            client = self._info(cancel)
            try:
                self._refresh_live(job, client)
                return self._build(job, inventory=self._plan(job.wallet, job.start, job.end), client=client)
            finally:
                if getattr(client, "session", None) is not None:
                    client.session.close()

    def import_file(self, path, kind="fills", field_map=None, timestamp_unit="ms", log=print, cancel=None):
        if kind not in ("fills", "ledger", "funding", "portfolio"):
            raise ValueError("导入类型须为 fills、ledger、funding 或 portfolio。")
        if timestamp_unit not in ("ms", "s"):
            raise ValueError("时间戳单位须为 ms 或 s。")
        if kind == "portfolio" and timestamp_unit != "ms":
            raise ValueError("官方净值导入只接受毫秒时间戳或 ISO 日期；请转换秒时间戳后使用 --timestamp-unit ms。")
        if field_map is not None and (not isinstance(field_map, dict) or
                any(not isinstance(k, str) or not isinstance(v, str) for k, v in field_map.items())):
            raise ValueError("字段映射须为列名字符串组成的 JSON 对象。")
        path = self._path(path)
        if not path.is_file():
            raise ValueError("找不到要导入的文件：" + str(path))
        with self._job("import", log, cancel) as job:
            if kind == "portfolio":
                result = import_portfolio_history(job.store, path, log=job.log,
                    cancel_check=job.check, now_ms=self.clock())
            else:
                result = import_history_file(job.store, path, kind=kind, field_map=field_map, timestamp_unit=timestamp_unit,
                    start=job.start, end=job.end, log=job.log, cancel_check=job.check)
            job.status = "partial" if (result.get("rejected_count") or result.get("rejected_samples")
                or (kind == "portfolio" and result.get("status") not in ("ok", "imported"))) else "completed"
            report = self._build(job, inventory=self._plan(job.wallet, job.start, job.end))
            return {**result, **report, "import": result}

    def run_demo(self, log=print, cancel=None):
        from sample_data import demo_records, demo_portfolio, demo_snapshot
        with self._job("demo", log, cancel, demo=True) as job:
            job.log("运行模拟数据演示：不会查询网络，不使用真实钱包或 AWS 凭证。")
            fills, ledger, funding = demo_records()
            for kind, records in (("fills", fills), ("ledger", ledger), ("funding", funding)):
                for row in records:
                    job.check()
                    job.store.put(kind, row, "import:SYNTHETIC_DEMO")
            job.store.set_meta("asset_meta", {"BTC": {"market_type": "perp", "pnl_token": "USDC"},
                                              "ETH": {"market_type": "perp", "pnl_token": "USDC"}})
            portfolio = demo_portfolio(fills, ledger, funding)
            job.store.set_meta("portfolio_history", portfolio)
            job.store.set_meta("portfolio_meta", {"status": "ok", "user_abstraction": "disabled",
                "scope_supported": True, "reference_ratio_supported": True,
                "reference_equity_scope": "perp", "source": "SYNTHETIC_DEMO",
                "warnings": ["资产快照为模拟数据，仅用于离线功能演示。"]})
            job.store.set_meta("latest_snapshot", demo_snapshot(portfolio))
            job.store.commit()
            return self._build(job, demo=True)

    def preview(self, dataset, page=1, page_size=50, coin="", query=""):
        if dataset not in DATASETS:
            raise ValueError("未知的数据表。")
        page_size = max(1, min(50, int(page_size)))
        page = max(1, int(page))
        empty = {"rows": [], "total": 0, "page": 1, "page_size": page_size, "pages": 1}
        with self._view_lock:
            path = self._view_path
            if path is None or not Path(path).is_file():
                return empty
            # Read-only and short lived. The lock lasts until close, preventing
            # the writer from replacing an open database on Windows.
            db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
            try:
                where, params = ["dataset=?"], [dataset]
                if coin:
                    where.append("coin=?")
                    params.append(coin)
                for word in str(query).lower().split():
                    word = word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    where.append("search LIKE ? ESCAPE '\\'")
                    params.append("%" + word + "%")
                condition = " AND ".join(where)
                total = db.execute("SELECT count(*) FROM display_rows WHERE " + condition, params).fetchone()[0]
                pages = max(1, math.ceil(total / page_size))
                page = min(page, pages)
                rows = db.execute("SELECT payload FROM display_rows WHERE " + condition + " ORDER BY time DESC,seq DESC LIMIT ? OFFSET ?", [*params, page_size, (page-1)*page_size])
                return {"rows": [json.loads(r[0]) for r in rows], "total": total, "page": page, "page_size": page_size, "pages": pages}
            finally:
                db.close()

    def analyze(self, log=print, cancel=None, *, live=True):
        """Analyze the cache without writing a report (reconcile / metrics)."""
        with self._job("analyze", log, cancel) as job:
            client = self._info(cancel) if live else None
            try:
                model, _ = analyze_wallet(job.store, client, log=job.log, cancel_check=job.check, mae=self._mae())
            finally:
                if getattr(client, "session", None) is not None:
                    client.session.close()
            return model

    def plugins(self, action="list", log=print, cancel=None):
        status = plugin_status(self.config)
        if action == "test":
            wallet = wallet_address(self.config["wallet"])
            settings = self.config["plugins"]
            from sources import PLUGINS
            for row in status:
                check_cancel(cancel)
                plugin = PLUGINS[row["name"]](settings.get(row["name"]))
                if not plugin.available():
                    row["test"] = {"ok": False, "detail": "未设置环境变量 " + row["env_key"] + " 或缺少必要配置"}
                    continue
                try:
                    row["test"] = plugin.test(wallet)
                except Exception as exc:
                    row["test"] = {"ok": False, "detail": safe_message(exc)[:200]}
                log(f"{row['name']}: " + ("可用" if row["test"]["ok"] else "不可用") + " · " + row["test"]["detail"])
        return {"status": "completed", "plugins": status,
                "note": "插件默认关闭；仅在 config.json plugins.<name>.enabled=true 或 --plugins 指定时运行，密钥只读环境变量。"}

    def doctor(self, log=print, cancel=None):
        check_cancel(cancel)
        versions = {}
        for name in ("requests", "boto3", "lz4", "textual", "rich"):
            try:
                versions[name] = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                versions[name] = "未安装"
        result = {"runtime_version": VERSION, "python": sys.version.split()[0], "platform": sys.platform,
                  "config": str(self.config_path), "data_dir": str(self._path(self.config["data_dir"])),
                  "output_dir": str(self._path(self.config["output_dir"])), "dependencies": versions,
                  "aws_profile": self.config["aws"]["profile"] or os.environ.get("AWS_PROFILE", "default"),
                  "aws_environment_credentials_set": bool(os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY")),
                  "aws_note": "仅检查环境配置，不联网验证身份；规划归档时使用标准 AWS 凭证链。",
                  "status": "completed" if all(v != "未安装" for v in versions.values()) else "partial"}
        log("环境检查完成；凭证值不会显示。")
        return result


def validate_plan(plan, wallet):
    if not isinstance(plan, dict) or plan.get("wallet") != wallet:
        raise ValueError("归档清单不属于当前钱包。")
    start, end = plan.get("start_ms"), plan.get("end_ms")
    if any(isinstance(x, bool) or not isinstance(x, int) or x < 0 for x in (start, end)) or end < start:
        raise ValueError("归档清单时间范围无效。")
    if not isinstance(plan.get("objects"), list) or not isinstance(plan.get("errors"), list):
        raise ValueError("归档清单缺少对象/错误列表。")
    seen = set()
    for obj in plan["objects"]:
        if not isinstance(obj, dict):
            raise ValueError("归档对象格式无效。")
        prefix, key = obj.get("prefix"), obj.get("key")
        if obj.get("bucket") != S3_BUCKET or prefix not in S3_PREFIXES or not isinstance(key, str) or not key.startswith(prefix + "/"):
            raise ValueError("归档清单只允许官方 bucket 与已支持的历史前缀。")
        if key in seen:
            raise ValueError("归档清单包含重复对象。")
        seen.add(key)
        size, hour, etag = obj.get("size"), obj.get("hour_ms"), obj.get("etag")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError("归档对象大小无效。")
        if isinstance(hour, bool) or not isinstance(hour, int) or object_hour(key) != hour or hour > end or hour + HOUR_MS <= start:
            raise ValueError("归档对象小时与清单范围不一致。")
        if not isinstance(etag, str) or not etag:
            raise ValueError("归档对象缺少 ETag。")
    if plan.get("object_count") != len(plan["objects"]):
        raise ValueError("归档对象数不匹配。")
    if plan.get("compressed_bytes") != sum(o["size"] for o in plan["objects"]):
        raise ValueError("归档清单体积与对象不匹配。")
    return plan

