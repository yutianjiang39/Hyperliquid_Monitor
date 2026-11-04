#!/usr/bin/env python3
"""Launch the terminal window, or use the same local runtime from a CLI."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import threading
import unittest
import webbrowser

ROOT = Path(__file__).resolve().parent


def parser():
    p = argparse.ArgumentParser(description="Hyperliquid 本地 Python 窗口版 · 历史抓取、战绩和离线报告", formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="直接运行 app.py 或 app.py tui 打开终端窗口。先试 app.py demo 可离线检查。\n"
               "普通 API 仅有最近成交；完整历史需合并归档/导出，实际覆盖见报告。")
    p.add_argument("--version", action="version", version="Hyperliquid Local 2.3.2")
    p.add_argument("--config", default=str(ROOT / "config.json"), help="配置文件；相对数据路径以其所在文件夹为基准")
    subs = p.add_subparsers(dest="command")
    descriptions = {"tui":"打开全屏终端窗口", "menu":"tui 的兼容别名", "init":"创建/更新本地配置", "run":"更新 API 并生成报告",
                    "collect":"run 的兼容别名", "plan":"列出官方 S3 归档清单（可能有 AWS 请求费用）", "scan":"按已审核清单扫描归档并生成报告",
                     "report":"仅用缓存生成报告", "assets":"更新资产/余额基准并增量补流水，生成四曲线报告", "import":"导入 JSON/JSONL/CSV 并生成报告", "demo":"运行完全离线的模拟演示",
                    "doctor":"检查 Python/依赖和目录", "self-test":"运行内置账务与本地工作流检查", "open":"打开最近报告"}
    for name, help_text in descriptions.items():
        s = subs.add_parser(name, help=help_text, description=help_text)
        s.add_argument("--config", default=argparse.SUPPRESS, help="配置文件路径")
        if name not in ("tui", "menu", "self-test"):
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
        if name in ("run", "collect", "scan", "report", "assets", "import", "demo"):
            s.add_argument("--open", action="store_true", help="完成后在默认浏览器打开 HTML")
        if name in ("run", "collect", "scan"):
            s.add_argument("--accept-requester-pays", action="store_true", help="明确确认使用已配置 AWS 身份支付归档请求/传输费用")
        if name in ("run", "collect"):
            s.add_argument("--archive", action="store_true", help="API 后列清单并扫描，必须同时给 --accept-requester-pays")
        if name == "import":
            s.add_argument("path", help="要导入的本地文件，属于当前钱包")
            s.add_argument("--kind", choices=["fills","ledger","funding","portfolio","balance"], default="fills")
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


def main(argv=None):
    if sys.version_info < (3, 10):
        print("需要 Python 3.10 或更高版本；推荐 Python 3.11/3.12。", file=sys.stderr)
        return 2
    p = parser()
    args = p.parse_args(argv)
    command = args.command or "tui"
    if command in ("tui", "menu"):
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            print("终端窗口需要在真实终端中启动。请双击 start_windows.bat / Start_macOS.command，或在终端执行 bash start_macos_linux.sh。\n"
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
        sys.path.insert(0, str(ROOT / "src"))
        suite = unittest.TestSuite()
        for module in ("engine_tests", "collector_tests", "curves_tests", "account_history_tests",
                       "balance_tests", "balance_snapshot_tests", "event_timeline_tests", "report_tests", "local_tests", "ui_tests", "launcher_tests"):
            suite.addTests(unittest.defaultTestLoader.loadTestsFromName(module))
        return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1
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
        elif command == "assets":
            result = runtime.refresh_assets(log=log,cancel=cancel)
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

