#!/usr/bin/env python3
"""Launch the terminal window, or use the same local runtime from a CLI."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import subprocess
import threading
import webbrowser

ROOT = Path(__file__).resolve().parent


def parser():
    p = argparse.ArgumentParser(description="Hyperliquid 本地 Python 窗口版 · 历史抓取、战绩和离线报告", formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="直接运行 app.py 或 app.py tui 打开终端窗口。先试 app.py demo 可离线检查。\n"
               "普通 API 仅有最近成交；完整历史需合并归档/导出，实际覆盖见报告。")
    p.add_argument("--version", action="version", version="Hyperliquid Local 3.1.0")
    p.add_argument("--config", default=str(ROOT / "config.json"), help="配置文件；相对数据路径以其所在文件夹为基准")
    subs = p.add_subparsers(dest="command")
    descriptions = {"tui":"打开全屏终端窗口", "menu":"tui 的兼容别名", "init":"创建/更新本地配置", "run":"更新 API 并生成报告",
                    "collect":"run 的兼容别名", "plan":"列出官方 S3 归档清单（可能有 AWS 请求费用）", "scan":"按已审核清单扫描归档并生成报告",
                    "report":"仅用缓存生成报告（--mae 时联网下载 K 线）", "snapshot":"更新官方净值与账户快照并增量补流水，生成报告",
                    "assets":"snapshot 的兼容别名", "import":"导入 JSON/JSONL/CSV 并生成报告", "demo":"运行完全离线的模拟演示",
                    "reconcile":"用缓存与官方 portfolio 对账；失败时退出码 4", "metrics":"输出钱包量化指标（不生成报告）",
                    "plugins":"查看/测试可选第三方数据插件",
                    "doctor":"检查 Python/依赖和目录", "self-test":"运行 pytest 单元测试", "open":"打开最近报告"}
    for name, help_text in descriptions.items():
        s = subs.add_parser(name, help=help_text, description=help_text)
        s.add_argument("--config", default=argparse.SUPPRESS, help="配置文件路径")
        if name not in ("tui", "menu", "self-test", "plugins"):
            s.add_argument("--wallet", help="实际 HyperCore 钱包地址")
            s.add_argument("--start", help="UTC 开始日期，空字符串表示最早可用")
            s.add_argument("--end", help="UTC 截止时刻，空字符串表示当前时刻")
            s.add_argument("--data-dir", help="缓存目录")
            s.add_argument("--output-dir", help="报告目录")
            s.add_argument("--profile", help="已有 AWS profile 名称，不填使用标准凭证链")
            s.add_argument("--region", help="AWS 区域，默认 us-east-1")
            s.add_argument("--budget-gib", type=float, help="每轮压缩下载预算，不是金额上限")
            s.add_argument("--max-objects", type=int, help="每轮最多扫描文件数，0 不另限数量")
            s.add_argument("--json", action="store_true", help="stdout 只输出 JSON，进度写到 stderr")
        if name in ("run", "collect", "scan", "report", "snapshot", "assets", "import", "demo"):
            s.add_argument("--open", action="store_true", help="完成后在默认浏览器打开 HTML")
        if name in ("run", "collect", "snapshot", "assets"):
            s.add_argument("--leaderboard", action="store_true", help="本次额外下载官方排行榜（数十 MB）交叉核对")
            s.add_argument("--include-subaccounts", action="store_true", help="本次同时抓取并分析各子账户，在主报告中汇总")
            s.add_argument("--plugins", help="本次启用的第三方插件，逗号分隔，如 hydromancer,dune（需环境变量密钥）")
        if name in ("run", "report", "snapshot", "assets", "metrics", "demo"):
            s.add_argument("--mae", action="store_true", help="本次计算 MAE/MFE（首次下载 15m/1h/4h/1d K 线，各最近 5000 根）")
        if name in ("reconcile", "metrics"):
            s.add_argument("--offline", action="store_true", help="不联网补 K 线，只用缓存")
        if name == "plugins":
            s.add_argument("action", nargs="?", choices=["list", "test"], default="list")
            s.add_argument("--json", action="store_true", help="stdout 只输出 JSON")
        if name in ("run", "collect", "scan"):
            s.add_argument("--accept-requester-pays", action="store_true", help="明确确认使用已配置 AWS 身份支付归档请求/传输费用")
        if name in ("run", "collect"):
            s.add_argument("--archive", action="store_true", help="API 后列清单并扫描，必须同时给 --accept-requester-pays")
        if name == "import":
            s.add_argument("path", help="要导入的本地文件，属于当前钱包")
            s.add_argument("--kind", choices=["fills","ledger","funding","portfolio"], default="fills")
            s.add_argument("--timestamp-unit", choices=["ms","s"], default="ms")
            s.add_argument("--field-map", default="{}", help='成交字段映射 JSON，如 {"px":"price"}')
    return p


def print_result(result, json_output=False):
    from local_runtime import safe_message
    if json_output:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return
    if result.get("html"):
        summary = result.get("summary", {})
        print("\n" + ("模拟数据演示完成" if result.get("demo") else "报告已生成"))
        print("任务状态：", result.get("status", "completed"))
        print("成交 / 操作：", summary.get("unique_fill_count", 0), "/", summary.get("operation_count", 0))
        print("完整已平仓回合：", summary.get("complete_closed_campaign_count", 0))
        print("实际覆盖：", result.get("coverage", {}).get("status", "待核验"))
        print("HTML：", result["html"])
        print("ZIP ：", result.get("zip", ""))
    else:
        # Do not dump the large S3 object list into a terminal.
        display = {k:v for k,v in result.items() if k != "objects"}
        if "pending_bytes" in display:
            display["pending_gib"] = round(display["pending_bytes"] / 2**30, 3)
        print(safe_message(json.dumps(display, ensure_ascii=False, indent=2, default=str)))


def metrics_view(model, full=False):
    """Scalar metrics for the terminal; ``full`` keeps every series for --json."""
    if full:
        return {"model": model}
    scalars = lambda d: {k: v for k, v in (d or {}).items() if not isinstance(v, (list, dict))}
    return {"wallet": model["wallet"], "performance": scalars(model["performance"]), "risk": scalars(model["risk"]),
            "behavior": scalars(model["behavior"]), "costs": scalars(model["costs"]),
            "activity": scalars(model["activity"]), "profile": model["profile"].get("tags"),
            "windows": {w: {"pnl": v["performance"].get("pnl"), "twr": v["performance"].get("twr"),
                            "max_drawdown": v["risk"].get("max_drawdown"), "sharpe": v["risk"].get("sharpe")}
                        for w, v in model["windows"].items()},
            "reconciliation": model["quality"]["reconciliation"].get("status"),
            "history": model["quality"]["truncation"].get("status")}


def main(argv=None):
    if sys.version_info < (3, 10):
        print("需要 Python 3.10 或更高版本；推荐 Python 3.11/3.12。", file=sys.stderr)
        return 2
    p = parser()
    args = p.parse_args(argv)
    command = args.command or "tui"
    if command in ("tui", "menu"):
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            print("终端窗口需要在真实终端中启动。Windows 请双击 start.bat，macOS / Linux 请在终端执行 bash start.sh。\n"
                  "非交互环境可用 app.py demo、app.py run、app.py --help。", file=sys.stderr)
            return 2
        try:
            from ui import run_ui
            run_ui(args.config)
            return 0
        except ModuleNotFoundError as exc:
            print("缺少终端界面依赖：" + str(exc) + "。请先运行相应 install 脚本。", file=sys.stderr)
            return 2
        except KeyboardInterrupt:
            return 130
    if command == "self-test":
        try:
            import pytest  # noqa: F401
        except ModuleNotFoundError:
            print("缺少 pytest：请运行 pip install -r requirements-extra.txt", file=sys.stderr)
            return 2
        return subprocess.call([sys.executable, "-m", "pytest", "-q", str(ROOT / "tests")], cwd=str(ROOT))
    from local_runtime import Runtime, Cancelled, safe_message
    cancel = threading.Event()
    try:
        runtime = Runtime(args.config)
        changes = {}
        for flag, key in (("wallet","wallet"),("start","start_date_utc"),("end","end_date_utc"),("data_dir","data_dir"),("output_dir","output_dir")):
            if getattr(args,flag,None) is not None:
                changes[key] = getattr(args,flag)
        aws = {}
        for flag,key in (("profile","profile"),("region","region"),("budget_gib","max_download_gib"),("max_objects","max_objects")):
            if getattr(args,flag,None) is not None:
                aws[key] = getattr(args,flag)
        if aws:
            changes["aws"] = aws
        if changes or command == "init":
            runtime.configure(changes)
        if getattr(args, "plugins", None):
            runtime.plugin_override = {name.strip() for name in args.plugins.split(",") if name.strip()}
        runtime.leaderboard_override = bool(getattr(args, "leaderboard", False))
        runtime.subaccounts_override = bool(getattr(args, "include_subaccounts", False))
        runtime.mae_override = bool(getattr(args, "mae", False))
        log = (lambda text: print(text, file=sys.stderr, flush=True)) if getattr(args,"json",False) else (lambda text: print(text, flush=True))
        if command == "init":
            result = {"status":"completed","config":str(runtime.config_path),"settings":runtime.config}
        elif command in ("run", "collect"):
            if args.archive and not args.accept_requester_pays:
                raise ValueError("--archive 需要同时指定 --accept-requester-pays。建议先单独运行 plan 查看体积。")
            result = runtime.run_api(log=log,cancel=cancel)
            if args.archive:
                runtime.plan_archives(log=log,cancel=cancel)
                result = runtime.scan_archives(log=log,cancel=cancel,accept_requester_pays=True)
        elif command == "plan":
            result = runtime.plan_archives(log=log,cancel=cancel)
        elif command == "scan":
            result = runtime.scan_archives(log=log,cancel=cancel,accept_requester_pays=args.accept_requester_pays)
        elif command == "report":
            result = runtime.generate_report(log=log,cancel=cancel)
        elif command in ("snapshot", "assets"):
            result = runtime.refresh_assets(log=log,cancel=cancel)
        elif command == "reconcile":
            model = runtime.analyze(log=log, cancel=cancel, live=not args.offline)
            quality = model["quality"]
            result = {"status": "completed", "reconciliation": quality["reconciliation"],
                      "truncation": quality["truncation"]}
            if not args.json:
                result["reconciliation"] = {k: v for k, v in quality["reconciliation"].items() if k != "intervals"}
                result["truncation"] = {k: v for k, v in quality["truncation"].items() if k != "evidence"}
            print_result(result, args.json)
            return 4 if quality["reconciliation"].get("status") == "fail" else 0
        elif command == "metrics":
            model = runtime.analyze(log=log, cancel=cancel, live=not args.offline)
            result = {"status": "completed", **metrics_view(model, full=args.json)}
        elif command == "plugins":
            result = runtime.plugins(args.action, log=log, cancel=cancel)
        elif command == "import":
            mapping = json.loads(args.field_map)
            if not isinstance(mapping,dict) or any(not isinstance(v,str) for v in mapping.values()):
                raise ValueError("--field-map 必须是列名映射 JSON 对象。")
            result = runtime.import_file(args.path,kind=args.kind,field_map=mapping,timestamp_unit=args.timestamp_unit,log=log,cancel=cancel)
        elif command == "demo":
            result = runtime.run_demo(log=log,cancel=cancel)
        elif command == "doctor":
            result = runtime.doctor(log=log,cancel=cancel)
        elif command == "open":
            result = runtime.last_result
            if not result or not Path(result.get("html", "")).is_file():
                raise ValueError("当前钱包尚无已生成的报告。")
            webbrowser.open(Path(result["html"]).resolve().as_uri())
        else:
            raise ValueError("未知命令。")
        print_result(result, getattr(args,"json",False))
        if getattr(args,"open",False) and result.get("html"):
            webbrowser.open(Path(result["html"]).resolve().as_uri())
        return 3 if result.get("status") in ("partial","paused") else 0
    except (Cancelled, KeyboardInterrupt):
        cancel.set()
        print("\n任务已停止。重新运行会使用保留的缓存。", file=sys.stderr)
        return 130
    except Exception as exc:
        print("错误：" + safe_message(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

