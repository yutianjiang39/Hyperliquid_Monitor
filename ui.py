"""Local terminal workbench for Hyperliquid history (Textual 8.2.8).

All network/accounting jobs and paged database reads execute outside the UI
thread. The console is bounded; data tables contain one page at a time.
"""
from __future__ import annotations

import json
import math
import threading
import time
import webbrowser
from datetime import datetime, timezone, timedelta
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import (
    Button, Collapsible, DataTable, Footer, Input, Label, ProgressBar, RichLog,
    Select, Static, Tab, Tabs,
)

from local_runtime import Runtime, Cancelled


INK = "#ede5d8"
MINT = "#9bc9b2"
ORANGE = "#d99a79"
MUTED = "#a49b8d"
RED = "#e39d9b"
DATASETS = {
    "operations": [("时间 · 北京", "time"), ("币种", "coin"), ("操作", "action_label"),
                   ("方向", "direction_label"), ("数量", "size"), ("价格", "price"),
                   ("已实现盈亏", "closed_pnl"), ("计价币", "pnl_token")],
    "campaigns": [("币种", "coin"), ("方向", "direction_label"), ("状态", "status"),
                  ("开仓时间", "open_time"), ("平仓时间", "close_time"),
                  ("净盈亏", "net_pnl"), ("计价币", "pnl_token"), ("完整回合", "complete")],
    "ledger": [("时间 · 北京", "time"), ("类型", "type"), ("方向", "direction"),
               ("金额", "amount"), ("代币", "token"), ("对手方", "counterparty")],
    "funding": [("时间 · 北京", "time"), ("币种", "coin"), ("资金费", "amount"),
                ("计价币", "pnl_token"), ("资金费率", "fundingRate")],
    "coverage": [("项目", "key"), ("当前记录", "value")],
}
LABELS = {"open": "持仓中", "closed": "已平仓", "interrupted": "历史中断",
          "long": "做多", "short": "做空", "spot": "现货", "unknown": "未知",
          "deposit": "充值", "withdraw": "提现", "internalTransfer": "内部转账",
          "spotTransfer": "现货转账", "accountClassTransfer": "现货/合约划转",
          "subAccountTransfer": "子账户划转", "vaultDeposit": "Vault 存入",
          "vaultWithdraw": "Vault 提出", "funding": "资金费"}


class TaskLog(Message):
    def __init__(self, text):
        super().__init__()
        self.text = text


class TaskFinished(Message):
    def __init__(self, name, result, error, cancelled):
        super().__init__()
        self.name, self.result, self.error, self.cancelled = name, result, error, cancelled


class PreviewFinished(Message):
    def __init__(self, version, dataset, result, error):
        super().__init__()
        self.version, self.dataset, self.result, self.error = version, dataset, result, error


def plain(value, maximum=100):
    """Never interpret wallet/data/log strings as terminal markup or controls."""
    if value is None or value == "":
        return "未知"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, default=str)
    value = str(value)
    value = "".join(c if ord(c) >= 32 and c != "\x7f" else " " for c in value)
    return value if len(value) <= maximum else value[:maximum - 1] + "…"


def format_time(value):
    try:
        if value in (None, "", 0):
            return "未知"
        return datetime.fromtimestamp(float(value) / 1000, timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError, OverflowError, OSError):
        return plain(value, 22)


def cell_value(row, key):
    delta = row.get("delta") if isinstance(row.get("delta"), dict) else {}
    value = row.get(key, delta.get(key))
    if key == "key" and value is None:
        value = row.get("section")
    if key == "action_label" and not value:
        value = {"open": "开仓", "add": "加仓", "reduce": "减仓", "close": "清仓",
                 "buy": "现货买入", "sell": "现货卖出"}.get(row.get("action"), row.get("action"))
    if key == "direction_label" and not value:
        value = row.get("direction")
    if key == "amount" and value is None:
        value = delta.get("usdc", delta.get("amount"))
    if key == "pnl_token" and not value:
        value = row.get("token") or delta.get("token")
    if key in ("time", "open_time", "close_time"):
        value = format_time(value)
    elif isinstance(value, str):
        value = LABELS.get(value, value)
    rendered = Text(plain(value, 68))
    if key in ("closed_pnl", "net_pnl", "amount"):
        try:
            numeric = float(value)
            if math.isfinite(numeric):
                rendered.stylize(MINT if numeric > 0 else RED if numeric < 0 else MUTED)
        except (ValueError, TypeError):
            rendered.stylize(ORANGE)
    elif value in (None, "", "未知", "历史中断"):
        rendered.stylize(ORANGE)
    return rendered


class NoticeScreen(ModalScreen):
    BINDINGS = [("escape", "dismiss", "关闭")]
    CSS = """
    NoticeScreen { align: center middle; background: #11100fe0; }
    #notice { width: 88; max-width: 94%; height: auto; max-height: 90%;
              background: #24221f; border: round #d99a79; padding: 1 2; }
    #notice-title { height: auto; text-style: bold; color: #d99a79; margin-bottom: 1; }
    #notice-body { height: auto; max-height: 24; }
    #notice-close { width: 20; margin-top: 1; }
    """

    def __init__(self, title, body):
        super().__init__()
        self.notice_title, self.notice_body = title, body

    def compose(self) -> ComposeResult:
        with Vertical(id="notice"):
            yield Static(Text(self.notice_title), id="notice-title")
            with VerticalScroll(id="notice-body"):
                yield Static(Text(self.notice_body))
            yield Button("关闭 · Esc", id="notice-close")

    @on(Button.Pressed, "#notice-close")
    def close_notice(self):
        self.dismiss()


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [("escape", "cancel", "取消")]
    CSS = """
    ConfirmScreen { align: center middle; background: #11100fe0; }
    #confirm { width: 76; max-width: 94%; height: auto; background: #24221f;
               border: round #d99a79; padding: 1 2; }
    #confirm-title { height: auto; color: #d99a79; text-style: bold; margin-bottom: 1; }
    #confirm-body { height: auto; margin-bottom: 1; }
    #confirm-actions { height: 3; align: right middle; }
    #confirm-actions Button { width: auto; min-width: 18; margin-left: 1; }
    """

    def __init__(self, title, body, confirm_label="确认继续"):
        super().__init__()
        self.dialog_title, self.body, self.confirm_label = title, body, confirm_label

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm"):
            yield Static(Text(self.dialog_title), id="confirm-title")
            yield Static(Text(self.body), id="confirm-body")
            with Horizontal(id="confirm-actions"):
                yield Button("取消", id="confirm-no")
                yield Button(self.confirm_label, variant="warning", id="confirm-yes")

    @on(Button.Pressed)
    def answer(self, event):
        if event.button.id in ("confirm-yes", "confirm-no"):
            self.dismiss(event.button.id == "confirm-yes")

    def action_cancel(self):
        self.dismiss(False)


class ImportScreen(ModalScreen[dict | None]):
    BINDINGS = [("escape", "cancel", "取消")]
    CSS = """
    ImportScreen { align: center middle; background: #11100fe0; }
    #import-dialog { width: 90; max-width: 96%; height: auto; background: #24221f;
                     border: round #d99a79; padding: 1 2; }
    #import-title { height: 2; color: #d99a79; text-style: bold; }
    #import-note { height: auto; margin-bottom: 1; color: #a49b8d; }
    #import-dialog Input, #import-dialog Select { margin-bottom: 1; }
    #import-buttons { height: 3; align: right middle; }
    #import-buttons Button { width: 20; margin-left: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="import-dialog"):
            yield Static("导入已有历史", id="import-title")
            yield Static("填写本地 JSON / JSONL / CSV 文件路径。成交应属于当前钱包；重复记录会合并。", id="import-note")
            yield Input(placeholder="例如 C:\\data\\fills.jsonl 或 /Users/me/fills.csv", id="import-path")
            yield Select([("成交记录", "fills"), ("资金流水", "ledger"), ("资金费", "funding"), ("官方权益历史", "portfolio"), ("USDC 账面余额基准", "balance")], value="fills", allow_blank=False, id="import-kind")
            yield Select([("数字时间戳：毫秒", "ms"), ("数字时间戳：秒", "s")], value="ms", allow_blank=False, id="import-unit")
            with Horizontal(id="import-buttons"):
                yield Button("取消", id="import-cancel")
                yield Button("开始导入", variant="primary", id="import-submit")

    @on(Select.Changed, "#import-kind")
    def import_kind_changed(self, event):
        kind = event.value
        unit = self.query_one("#import-unit", Select)
        fixed_unit = kind in ("portfolio", "balance")
        if fixed_unit:
            unit.value = "ms"
        unit.disabled = fixed_unit
        note = self.query_one("#import-note", Static)
        path = self.query_one("#import-path", Input)
        if kind == "balance":
            note.update("导入同一账户的 USDC 账面余额基准（JSON / CSV）。数字时间戳必须为毫秒，或使用 ISO 时间；请勿填综合净值。")
            path.placeholder = "例如 verified_balance.json 或 cash_anchors.csv"
        elif kind == "portfolio":
            note.update("导入官方净值历史（JSON / CSV）。数字时间戳必须为毫秒，或使用 ISO 时间；CSV 需提供 window。")
            path.placeholder = "例如 portfolio.json 或 portfolio_samples.csv"
        else:
            note.update("填写本地 JSON / JSONL / CSV 文件路径。记录应属于当前钱包；重复记录会合并。")
            path.placeholder = "例如 C:\\data\\fills.jsonl 或 /Users/me/fills.csv"

    @on(Button.Pressed, "#import-cancel")
    def action_cancel(self):
        self.dismiss(None)

    @on(Button.Pressed, "#import-submit")
    def submit_import(self):
        path = self.query_one("#import-path", Input).value.strip().strip('"')
        if not path:
            self.notify("请先填写文件路径。", severity="warning")
            return
        kind = self.query_one("#import-kind", Select).value
        self.dismiss({"path": path, "kind": kind,
                      "timestamp_unit": "ms" if kind in ("portfolio", "balance")
                      else self.query_one("#import-unit", Select).value})


class HistoryApp(App):
    TITLE = "Hyperliquid · 本地历史工作台"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("ctrl+r", "api", "读取 API", priority=True),
        Binding("d", "demo", "演示"),
        Binding("ctrl+l", "focus_logs", "日志", priority=True),
        Binding("escape", "cancel_task", "停止", priority=True),
        Binding("question_mark", "help", "帮助"),
        Binding("q", "request_quit", "退出"),
        Binding("ctrl+c", "request_quit", "退出", priority=True, show=False),
    ]
    CSS = """
    Screen { background: #1b1a18; color: #ede5d8; layout: vertical; }
    #top { height: 3; border-bottom: solid #45403a; padding: 0 2; align-vertical: middle; }
    #brand { width: 1fr; content-align: left middle; text-style: bold; color: #d99a79; }
    #top-status { width: auto; max-width: 58; content-align: right middle; color: #b3aa9b; }
    #workspace { height: 1fr; }
    #sidebar { width: 34; min-width: 30; background: #211f1c; padding: 1 1; border-right: solid #45403a; }
    .section-label { height: 1; color: #d99a79; text-style: bold; margin-bottom: 1; }
    .field-label { height: 1; color: #a49b8d; }
    #sidebar Input { height: 3; background: #1b1a18; border: tall #524b43; margin-bottom: 0; }
    #sidebar Input:focus { border: tall #d99a79; }
    #sidebar Button { width: 100%; height: 3; min-width: 8; margin-top: 0; border: tall #4d463e; background: #2c2924; color: #ede5d8; }
    #sidebar Button:hover { background: #3a342d; }
    #sidebar #api { background: #a96c50; border: tall #a96c50; color: #fff4e9; text-style: bold; }
    #sidebar #stop { color: #e39d9b; }
    #small-actions { height: 3; }
    #small-actions Button { width: 1fr; }
    #archive-options { background: #211f1c; padding: 0; margin: 0; }
    #archive-options CollapsibleTitle { color: #b3aa9b; }
    #sidebar-note { height: auto; color: #8f877b; margin-top: 1; }
    #main { width: 1fr; padding: 1 2 0 2; }
    #metrics { height: 5; margin-bottom: 1; }
    .metric { width: 1fr; border: round #45403a; padding: 0 1; margin-right: 1; background: #211f1c; }
    .metric:last-child { margin-right: 0; }
    #task-header { height: 1; margin-bottom: 0; }
    #task-label { width: 1fr; color: #ede5d8; }
    #elapsed { width: 15; content-align: right middle; color: #a49b8d; }
    #activity { height: 1; margin: 0; color: #d99a79; }
    #activity Bar { width: 1fr; }
    #logs { height: 10; min-height: 5; background: #171614; border: round #45403a; padding: 0 1; margin-bottom: 1; scrollbar-color: #65584a; }
    #logs:focus { border: round #d99a79; }
    #results { height: 1fr; min-height: 10; }
    #dataset-tabs { height: 3; }
    Tabs { background: #1b1a18; }
    Tab { color: #a49b8d; }
    Tab.-active { color: #d99a79; text-style: bold; }
    Underline > .underline--bar { color: #d99a79; background: #d99a79; }
    #filterbar { height: 3; }
    #filter-coin { width: 20; }
    #filter-text { width: 1fr; }
    #filter-apply { width: 10; min-width: 8; }
    #filterbar Input { background: #211f1c; border: tall #45403a; }
    #records { height: 1fr; min-height: 3; background: #211f1c; border: round #45403a; }
    DataTable > .datatable--header { background: #38322b; color: #d6c3ad; text-style: bold; }
    DataTable > .datatable--cursor { background: #514236; color: #fff1dc; }
    DataTable > .datatable--odd-row { background: #26231f; }
    DataTable > .datatable--even-row { background: #211f1c; }
    #pager { height: 3; align-vertical: middle; }
    #pager Label { width: 1fr; color: #a49b8d; content-align: left middle; }
    #pager Button { min-width: 10; width: 12; height: 3; background: #2c2924; border: tall #45403a; margin-left: 1; }
    #command { height: 3; background: #24211d; border: tall #695441; }
    #hint { height: 1; color: #9a8c7b; }
    Footer { background: #28241f; color: #b6a895; }
    Footer > .footer--key { background: #3b3128; color: #e5ae8d; }
    .hidden { display: none; }
    """

    def __init__(self, config_path=None, *, runtime=None):
        super().__init__()
        self.runtime = runtime if runtime is not None else Runtime(config_path)
        self.busy = False
        self.cancel_event = threading.Event()
        self.quit_after_task = False
        self.task_started = 0.0
        self.task_name = "就绪"
        self.dataset = "operations"
        self.page = 1
        self.pages = 1
        self.page_size = 50
        self.visible_rows = []
        self.preview_version = 0
        self.task_result = None
        self.last_task_error = None
        self.log_count = 0
        self._mounted_ui = False
        self._workspace_screen = None

    def compose(self) -> ComposeResult:
        cfg = self.runtime.config
        aws = cfg.get("aws", {})
        with Horizontal(id="top"):
            yield Static("▌ HL HISTORY  /  本地工作台", id="brand")
            yield Static("只读 · 本地缓存", id="top-status")
        with Horizontal(id="workspace"):
            with VerticalScroll(id="sidebar"):
                yield Label("工作区", classes="section-label")
                yield Label("目标钱包", classes="field-label")
                yield Input(str(cfg.get("wallet") or ""), placeholder="0x…实际账户地址", id="wallet")
                yield Label("开始日期 · UTC / 空白=最早", classes="field-label")
                yield Input(str(cfg.get("start_date_utc") or ""), placeholder="2025-01-01", id="start")
                yield Label("结束时间 · UTC / 空白=现在", classes="field-label")
                yield Input(str(cfg.get("end_date_utc") or ""), placeholder="2026-01-01T23:59:59Z", id="end")
                with Collapsible(title="归档选项", collapsed=True, id="archive-options"):
                    yield Label("本轮下载预算 · GiB", classes="field-label")
                    yield Input(str(aws.get("max_download_gib", 10)), id="budget")
                    yield Label("AWS profile · 空白用默认凭证", classes="field-label")
                    yield Input(str(aws.get("profile") or ""), placeholder="default / 自定义 profile", id="profile")
                    yield Label("AWS 区域", classes="field-label")
                    yield Input(str(aws.get("region") or "us-east-1"), id="region")
                yield Button("保存设置", id="save")
                yield Button("读取 API + 生成报告", id="api")
                yield Button("更新资产历史 + 四曲线", id="assets")
                yield Button("规划历史归档", id="plan")
                yield Button("按预算下载归档", id="scan")
                yield Button("用缓存生成报告", id="report")
                yield Button("导入历史文件", id="import")
                with Horizontal(id="small-actions"):
                    yield Button("模拟演示", id="demo")
                    yield Button("环境检查", id="doctor")
                yield Button("打开 HTML 报告", id="open-report")
                yield Button("停止当前任务 · Esc", id="stop", disabled=True)
                yield Static("API 仅含可见历史。归档与导入可继续回补；实际覆盖请看“数据覆盖”。\n\n本地终端无需 Claude 账户。", id="sidebar-note")
            with Vertical(id="main"):
                with Horizontal(id="metrics"):
                    yield Static("操作记录\n—", id="metric-ops", classes="metric")
                    yield Static("完整平仓\n—", id="metric-cycles", classes="metric")
                    yield Static("已知盈亏 · USDC\n—", id="metric-pnl", classes="metric")
                    yield Static("覆盖状态\n待读取", id="metric-coverage", classes="metric")
                with Horizontal(id="task-header"):
                    yield Label("● 就绪 · 输入钱包后读取，或按 d 查看模拟演示", id="task-label")
                    yield Label("", id="elapsed")
                yield ProgressBar(total=1, show_percentage=False, show_eta=False, id="activity")
                yield RichLog(id="logs", max_lines=1500, wrap=True, markup=False, highlight=False)
                with Vertical(id="results"):
                    yield Tabs(Tab("操作", id="operations"), Tab("战绩", id="campaigns"), Tab("流水", id="ledger"),
                               Tab("资金费", id="funding"), Tab("数据覆盖", id="coverage"), id="dataset-tabs")
                    with Horizontal(id="filterbar"):
                        yield Input(placeholder="币种 · 精确", id="filter-coin")
                        yield Input(placeholder="搜索记录 / Hash / 关键词，回车应用", id="filter-text")
                        yield Button("筛选", id="filter-apply")
                    yield DataTable(id="records", zebra_stripes=True, cursor_type="row")
                    with Horizontal(id="pager"):
                        yield Label("尚未载入明细 · 每页 50 条", id="page-info")
                        yield Button("上一页", id="prev", disabled=True)
                        yield Button("下一页", id="next", disabled=True)
                yield Input(placeholder="› /api  /assets  /report  /import  /demo  /stop  /help", id="command")
                yield Static("Tab 切换焦点 · Enter 查看行详情 · 表格支持横向滚动", id="hint")
        yield Footer()

    def on_mount(self):
        self._workspace_screen = self.screen
        self._mounted_ui = True
        self.main_query("#activity", ProgressBar).display = False
        self.append_log("工作台已就绪。所有数据保存在本机；任务开始后这里会实时显示进度。", "welcome")
        self.append_log("提示：普通 API 不能取回超过保留范围的成交；可通过归档或导入补齐可用历史。", "note")
        self.set_interval(1, self.update_elapsed)
        self.adjust_layout(self.size.width, self.size.height)
        self.update_wallet_header()
        if self.runtime.last_result:
            self.apply_summary(self.runtime.last_result)
        self.request_preview()
        self.main_query("#command", Input).focus()

    def main_query(self, selector, expect_type=None):
        """Keep background updates on the workspace when a modal is open."""
        return self._workspace_screen.query_one(selector, expect_type)

    def on_resize(self, event):
        if not self._mounted_ui or not self.is_running:
            return
        self.adjust_layout(event.size.width, event.size.height)

    def adjust_layout(self, width, height):
        small = height < 36
        self.main_query("#metrics").display = not small
        self.main_query("#logs", RichLog).styles.height = 5 if height < 29 else 6 if small else 10
        self.main_query("#filterbar").display = height >= 29
        self.main_query("#hint").display = height >= 27
        self.main_query("#sidebar").styles.width = 30 if width < 110 else 34
        self.main_query("#hint", Static).update("建议窗口至少 110 列 × 36 行；左栏可滚动。" if width < 110 or small else "Tab 切换焦点 · Enter 查看行详情 · 表格支持横向滚动")

    def update_elapsed(self):
        if self.busy and self.is_running:
            seconds = int(time.monotonic() - self.task_started)
            self.main_query("#elapsed", Label).update(f"{seconds // 60:02d}:{seconds % 60:02d}")

    def update_wallet_header(self):
        wallet = str(self.runtime.config.get("wallet") or "未设置钱包")
        short_wallet = wallet[:8] + "…" + wallet[-6:] if len(wallet) > 22 else wallet
        self.main_query("#top-status", Static).update(Text("只读 · " + short_wallet, style=MUTED))

    def append_log(self, message, level="info"):
        clean = plain(message, 4000)
        tone = ORANGE if level in ("note", "warning") else RED if level == "error" else MINT if level in ("done", "welcome") else INK
        row = Text(datetime.now().strftime("%H:%M:%S") + "  ", style="#847b6d")
        row.append(clean, style=tone)
        self.main_query("#logs", RichLog).write(row)
        self.log_count += 1

    def save_form(self, *, announce=False):
        if self.busy:
            self.notify("任务进行中，完成或停止后再修改设置。", severity="warning")
            return False
        try:
            budget = float(self.main_query("#budget", Input).value)
            if not math.isfinite(budget) or budget <= 0:
                raise ValueError("归档预算须为大于 0 的有限数值。")
            updates = {"wallet": self.main_query("#wallet", Input).value.strip(),
                       "start_date_utc": self.main_query("#start", Input).value.strip(),
                       "end_date_utc": self.main_query("#end", Input).value.strip(),
                       "aws": {**self.runtime.config.get("aws", {}), "max_download_gib": budget,
                               "profile": self.main_query("#profile", Input).value.strip() or None,
                               "region": self.main_query("#region", Input).value.strip() or "us-east-1"}}
            context_fields = ("wallet", "data_dir", "output_dir")
            previous_context = tuple(self.runtime.config.get(key) for key in context_fields)
            self.runtime.configure(updates)
            self.update_wallet_header()
            if previous_context != tuple(self.runtime.config.get(key) for key in context_fields):
                self.task_result = None
                self.last_task_error = None
                if self.runtime.last_result:
                    self.apply_summary(self.runtime.last_result)
                else:
                    self.reset_summary()
                self.page, self.pages = 1, 1
                self.visible_rows = []
                self.main_query("#records", DataTable).clear()
                self.main_query("#prev", Button).disabled = True
                self.main_query("#next", Button).disabled = True
                self.main_query("#task-label", Label).update(Text("● 已切换钱包 · 读取当前缓存", style=MUTED))
                self.request_preview()
            elif self.runtime.last_result and self.runtime.last_result.get("demo"):
                self.main_query("#top-status", Static).update(Text("模拟数据演示 · 非真实钱包", style=ORANGE))
            if announce:
                self.append_log("设置已保存。", "done")
            return True
        except Exception as exc:
            self.append_log("设置未保存：" + str(exc), "error")
            self.notify(str(exc), severity="error")
            return False

    def set_busy(self, value):
        self.busy = value
        for selector in ("#wallet", "#start", "#end", "#budget", "#profile", "#region", "#save", "#api", "#plan", "#scan", "#report", "#import", "#demo", "#doctor"):
            self.main_query(selector).disabled = value
        self.main_query("#stop", Button).disabled = not value
        progress = self.main_query("#activity", ProgressBar)
        progress.display = value
        if value:
            progress.update(total=None, progress=0)

    def start_task(self, name, method, kwargs=None, *, save=True):
        if self.busy:
            self.notify("已有任务在运行，请等待或按 Esc 停止。", severity="warning")
            return
        if save and not self.save_form():
            return
        self.cancel_event = threading.Event()
        self.task_name, self.task_started = name, time.monotonic()
        self.task_result = None
        self.last_task_error = None
        self.set_busy(True)
        self.main_query("#task-label", Label).update(Text("● " + name, style=ORANGE))
        self.append_log("开始 · " + name, "note")
        self.execute_task(name, method, dict(kwargs or {}), self.cancel_event)

    @work(thread=True, group="history-task", exit_on_error=False)
    def execute_task(self, name, method, kwargs, cancellation):
        def log(message):
            self.post_message(TaskLog(message))
        try:
            result = getattr(self.runtime, method)(log=log, cancel=cancellation, **kwargs)
            self.post_message(TaskFinished(name, result, None, False))
        except Cancelled as exc:
            self.post_message(TaskFinished(name, None, str(exc) or "已停止，已保存可恢复进度。", True))
        except Exception as exc:
            self.post_message(TaskFinished(name, None, str(exc), False))

    def on_task_log(self, message):
        if self._mounted_ui and self.is_running:
            self.append_log(message.text)

    def on_task_finished(self, message):
        if self._mounted_ui and self.is_running:
            self.finish_task(message.name, message.result, message.error, message.cancelled)

    def finish_task(self, name, result, error, cancelled):
        self.set_busy(False)
        self.task_result, self.last_task_error = result, error
        if cancelled:
            self.append_log(error or "任务已停止。", "note")
            self.main_query("#task-label", Label).update(Text("○ 已停止 · 可从缓存继续", style=ORANGE))
        elif error:
            self.append_log(name + "未完成：" + error, "error")
            self.main_query("#task-label", Label).update(Text("! 任务未完成 · 查看日志", style=RED))
            self.notify(error[:200], severity="error", timeout=8)
        else:
            result = result or {}
            partial = result.get("status") in ("partial", "partial_failed_objects", "budget_pause", "paused")
            paused = result.get("status") in ("budget_pause", "paused")
            self.append_log(name + ("已结束；仍有缺口或待续传部分。" if partial else "已完成。"), "warning" if partial else "done")
            status_label = "本轮已暂停 · 待续传" if paused else "本轮完成 · 仍有待补范围" if partial else "任务完成 · 覆盖以报告为准"
            self.main_query("#task-label", Label).update(Text("● " + status_label, style=ORANGE if partial else MINT))
            if "pending_bytes" in result:
                self.append_log(f"归档清单：{result.get('object_count', 0):,} 个对象，待扫描 {result['pending_bytes'] / 2**30:,.2f} GiB。", "note")
            if result.get("html"):
                self.append_log("HTML 报告：" + str(result["html"]), "done")
            if result.get("summary") or result.get("coverage"):
                self.apply_summary(result)
            if name == "检查本地环境":
                self.push_screen(NoticeScreen("环境检查", json.dumps(result, ensure_ascii=False, indent=2, default=str)))
        self.page = 1
        self.request_preview()
        if self.quit_after_task:
            self.exit()

    def reset_summary(self):
        for selector, title in (("#metric-ops", "操作记录"), ("#metric-cycles", "完整平仓"),
                                ("#metric-pnl", "已知盈亏 · USDC"), ("#metric-coverage", "覆盖状态")):
            self.main_query(selector, Static).update(Text(title + "\n待读取", style=MUTED))

    def apply_summary(self, result):
        summary = result.get("summary", {})
        coverage = result.get("coverage", {})
        self.main_query("#metric-ops", Static).update(Text("操作记录\n" + f"{summary.get('operation_count', 0):,}", style=INK))
        self.main_query("#metric-cycles", Static).update(Text("完整平仓\n" + f"{summary.get('complete_closed_campaign_count', 0):,}", style=INK))
        pnl = summary.get("observed_realized_pnl_usdc")
        pnl_tone = ORANGE
        try:
            numeric_pnl = float(pnl)
            if math.isfinite(numeric_pnl):
                pnl_tone = RED if numeric_pnl < 0 else MINT if numeric_pnl > 0 else MUTED
        except (ValueError, TypeError):
            pass
        self.main_query("#metric-pnl", Static).update(Text("已知盈亏 · USDC\n" + plain(pnl, 22), style=pnl_tone))
        status = coverage.get("status") or result.get("status") or "覆盖待核验"
        self.main_query("#metric-coverage", Static).update(Text("覆盖状态\n" + plain(status, 28), style=ORANGE))
        if result.get("demo"):
            self.main_query("#top-status", Static).update(Text("模拟数据演示 · 非真实钱包", style=ORANGE))
        else:
            self.update_wallet_header()

    def request_preview(self):
        if not self._mounted_ui:
            return
        self.preview_version += 1
        version = self.preview_version
        coin = self.main_query("#filter-coin", Input).value.strip()
        query = self.main_query("#filter-text", Input).value.strip()
        self.main_query("#page-info", Label).update("正在读取当前页…")
        self.load_preview(version, self.dataset, self.page, coin, query)

    @work(thread=True, group="preview", exit_on_error=False)
    def load_preview(self, version, dataset, page, coin, query):
        try:
            result = self.runtime.preview(dataset, page=page, page_size=self.page_size, coin=coin, query=query)
            self.post_message(PreviewFinished(version, dataset, result, None))
        except Exception as exc:
            self.post_message(PreviewFinished(version, dataset, None, str(exc)))

    def on_preview_finished(self, message):
        if self._mounted_ui and self.is_running:
            self.apply_preview(message.version, message.dataset, message.result, message.error)

    def apply_preview(self, version, dataset, result, error):
        if version != self.preview_version:
            return
        table = self.main_query("#records", DataTable)
        table.clear(columns=True)
        fields = DATASETS[dataset]
        table.add_columns(*(title for title, _ in fields))
        if error:
            self.visible_rows = []
            self.pages = 1
            self.main_query("#page-info", Label).update("暂无可显示记录 · " + plain(error, 75))
        else:
            result = result or {}
            rows = result.get("rows", [])[:self.page_size]
            self.visible_rows = rows
            self.page = result.get("page", self.page)
            self.pages = max(1, result.get("pages", 1))
            for i, row in enumerate(rows):
                if not isinstance(row, dict):
                    row = {"key": str(i + 1), "value": row}
                table.add_row(*(cell_value(row, key) for _, key in fields), key=str(i))
            total = result.get("total", len(rows))
            self.main_query("#page-info", Label).update(f"{total:,} 条 · 第 {self.page:,} / {self.pages:,} 页 · 每页 {self.page_size} 条")
        self.main_query("#prev", Button).disabled = self.page <= 1
        self.main_query("#next", Button).disabled = self.page >= self.pages

    @on(Tabs.TabActivated, "#dataset-tabs")
    def switch_dataset(self, event):
        if event.tab.id in DATASETS:
            self.dataset, self.page = event.tab.id, 1
            if self._mounted_ui:
                self.request_preview()

    @on(DataTable.RowSelected, "#records")
    def row_detail(self, event):
        try:
            row = self.visible_rows[int(event.row_key.value)]
        except (ValueError, IndexError, TypeError):
            return
        text = json.dumps(row, ensure_ascii=False, indent=2, default=str)
        self.push_screen(NoticeScreen("记录详情 · 精确原始值", plain(text, 20000).replace("  ", " ") if "\x1b" in text else text))

    @on(Button.Pressed)
    def buttons(self, event):
        identity = event.button.id
        actions = {"api": self.action_api, "demo": self.action_demo, "plan": self.action_plan,
                   "scan": self.action_scan, "report": self.action_report, "assets": self.action_assets, "import": self.action_import,
                   "doctor": self.action_doctor, "stop": self.action_cancel_task,
                   "open-report": self.action_open_report}
        if identity in actions:
            actions[identity]()
        elif identity == "save":
            if self.save_form(announce=True):
                self.page = 1
                self.request_preview()
        elif identity == "filter-apply":
            self.page = 1
            self.request_preview()
        elif identity == "prev" and self.page > 1:
            self.page -= 1
            self.request_preview()
        elif identity == "next" and self.page < self.pages:
            self.page += 1
            self.request_preview()

    @on(Input.Submitted)
    def input_submitted(self, event):
        if event.input.id in ("filter-coin", "filter-text"):
            self.page = 1
            self.request_preview()
        elif event.input.id == "command":
            command = event.value.strip()
            event.input.value = ""
            if command:
                self.dispatch_command(command)
        elif event.input.id in ("wallet", "start", "end"):
            self.save_form(announce=True)

    def dispatch_command(self, command):
        name, _, argument = command.strip().partition(" ")
        name = name.lower().lstrip("/")
        actions = {"api": self.action_api, "plan": self.action_plan, "scan": self.action_scan,
                   "report": self.action_report, "assets": self.action_assets, "demo": self.action_demo, "stop": self.action_cancel_task,
                   "help": self.action_help, "quit": self.action_request_quit, "open": self.action_open_report,
                   "doctor": self.action_doctor, "import": self.action_import}
        if name == "wallet" and argument.strip():
            if self.busy:
                self.notify("请先停止当前任务再切换钱包。", severity="warning")
                return
            self.main_query("#wallet", Input).value = argument.strip()
            self.save_form(announce=True)
            self.page = 1
            self.request_preview()
        elif name in actions:
            actions[name]()
        else:
            self.append_log("未知命令。输入 /help 查看可用操作。", "note")

    def action_api(self):
        self.start_task("读取 API 并生成报告", "run_api")

    def action_report(self):
        self.start_task("从本地缓存生成报告", "generate_report")

    def action_assets(self):
        self.start_task("更新官方资产历史与四曲线", "refresh_assets")

    def action_demo(self):
        self.start_task("模拟数据演示", "run_demo", save=False)

    def action_plan(self):
        if self.busy or not self.save_form():
            return
        self.push_screen(ConfirmScreen("列出官方归档", "将使用你已配置的 AWS 身份列出官方归档文件。\n\nAWS 的目录请求也可能计费。本步只规划文件数量和压缩体积，不下载归档内容。", "列出清单"),
                         lambda ok: self.start_task("规划历史归档", "plan_archives", save=False) if ok else None)

    def action_scan(self):
        if self.busy or not self.save_form():
            return
        plan = self.runtime.last_plan
        if not plan:
            self.notify("请先运行“规划历史归档”，查看待下载体积。", severity="warning")
            return
        pending = float(plan.get("pending_bytes", 0)) / 2**30
        count = plan.get("object_count", len(plan.get("objects", [])))
        budget = self.runtime.config.get("aws", {}).get("max_download_gib", 10)
        body = (f"清单：{count:,} 个文件，待扫描约 {pending:,.2f} GiB。\n本轮压缩下载预算：{budget} GiB。\n\n"
                "按钱包筛选仍需下载全市场归档。AWS 请求与传输由该 AWS 账户支付；GiB 预算不是费用金额上限。\n\n达到预算后保存进度，可再次运行续传。")
        self.push_screen(ConfirmScreen("确认本轮归档下载", body, "确认并开始"),
                         lambda ok: self.start_task("归档回补与报告", "scan_archives", {"accept_requester_pays": True}, save=False) if ok else None)

    def action_import(self):
        if self.busy or not self.save_form():
            return
        self.push_screen(ImportScreen(), lambda info: self.start_task("导入历史文件", "import_file", info, save=False) if info else None)

    def action_doctor(self):
        # doctor can perform local credential-provider checks, so use the worker too.
        self.start_task("检查本地环境", "doctor", save=False)

    def action_cancel_task(self):
        # Escape belongs to any open modal first; do not cancel a paid-task prompt.
        if len(self.screen_stack) > 1:
            self.screen.dismiss(None)
            return
        if not self.busy:
            return
        self.cancel_event.set()
        self.main_query("#task-label", Label).update(Text("◌ 正在停止 · 等待当前请求结束并保存进度", style=ORANGE))
        self.append_log("已请求停止。进行中的网络请求可能仍需片刻返回；缓存会在安全检查点保留。", "note")

    def action_open_report(self):
        result = self.runtime.last_result or {}
        path = result.get("html")
        if path and Path(path).is_file():
            webbrowser.open(Path(path).resolve().as_uri())
            self.append_log("已请求浏览器打开本地 HTML 报告。", "done")
        else:
            self.notify("还没有可打开的报告，先读取数据或运行模拟演示。", severity="warning")

    def action_focus_logs(self):
        self.main_query("#logs", RichLog).focus()

    def action_help(self):
        self.push_screen(NoticeScreen("本地工作台 · 使用帮助", "1. 输入实际钱包地址，点击“读取 API + 生成报告”。\n"
            "2. 更多历史可用“规划归档”→“按预算下载”，或导入已有文件。\n"
            "3. 操作、战绩、资金流水和资金费在下方分页查看；按 Enter 查看精确行详情。\n"
            "4. “打开 HTML 报告”用于浏览器查看和 CSV 下载，报告可以离线打开。\n\n"
            "命令：/api /assets /plan /scan /report /import /demo /stop /open /doctor\n"
            "切换钱包：/wallet 0x…\n\n"
            "Ctrl+R 读取 API；d 演示（输入框外）；Esc 停止；q 退出。\n"
            "任务停止在安全检查点生效，网络请求超时前可能仍需等待。\n\n"
            "范围：单个 HyperCore 实际地址的成交与资金历史。普通 API 只开放最近成交，归档和导入也须核对实际覆盖。\n"
            "已知 USDC 盈亏未扣交易费与资金费，不是账户权益。窗口不需要 Claude 账户，也不需要钱包私钥。"))

    def action_request_quit(self):
        if self.busy:
            self.push_screen(ConfirmScreen("任务仍在进行", "可以停止任务并等待缓存保存后退出。\n\n正在进行的网络请求会在返回或超时后响应停止。", "停止并退出"), self.confirm_quit)
        else:
            self.exit()

    def confirm_quit(self, answer):
        if answer:
            self.quit_after_task = True
            self.action_cancel_task()

    def on_unmount(self):
        self._mounted_ui = False
        self.preview_version += 1
        self.cancel_event.set()


def run_ui(config_path=None):
    """Called by app.py; keep Textual imports out of headless CLI-only execution."""
    return HistoryApp(config_path).run()

