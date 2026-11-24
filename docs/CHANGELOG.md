# 变更记录

## 文件整理（2026-11-24）

- 启动与安装脚本合并为 `start.bat`（Windows）和 `start.sh`（macOS / Linux）；加 `--install` 只安装或修复依赖。原 `start_windows.bat`、`start_macos_linux.sh`、`Start_macOS.command` 和 `scripts/` 下的安装脚本已删除。
- `requirements-dev.txt` 与 `requirements-optional.txt` 并入 `requirements.txt`，依赖只剩一个文件。
- 变更记录移到 `docs/CHANGELOG.md`，`config.example.json` 移到 `docs/`。
- `ui.py`、`local_runtime.py`、`sample_data.py` 移入 `src/`，根目录只保留 `app.py` 入口和启动脚本。
- README 精简，详细说明移到 `docs/`（使用手册、数据口径与指标、常见问题与参考）。

## 第三轮：指标定义与计算修正（2026-11-24）

- 修正：回撤计入窗口起点；空的 TWR / 月度收益保持为空；资金费按笔拆分收付；手续费统一用 USD；成交与反手按唯一记录计数；活跃日占比按日历日；截止时刻插值；单个不完整交易日按比例缩放；手续费拖累除以真实毛利；Dune 插件带参数执行并核对钱包。
- 新增：ROPC、盈亏平衡胜率、Wilson 置信区间、有效独立回合数、R 统计、最近 20 回合条带、盈亏前五笔、资金概览、BTC Beta / Alpha（HAC）、压力测试、证据强度分级、订单数与强平事件数。
- 对账：区间与官方采样对齐，新增 `insufficient_overlap` 与 `no_gap_detected` 状态。
- 报告：内置 Inter、JetBrains Mono 字体，总览、成本、持仓、数据质量页展示新字段。
- 测试：新增 `tests/test_round3.py`（25 项），共 60 项。
- 文档：`README.md` 改为完整中文说明（此前只有一行标题，GitHub 首页因此显示为空），原 `README.zh-CN.md` 并入；`config.json` 不再纳入版本管理，新增 `config.example.json`。

## 报告阈值着色（2026-11-08）

- 关键指标按经验阈值着红 / 黄 / 绿，悬停显示阈值；总览新增“风险与亮点”卡片。
- 持仓表标出距强平过近和高杠杆仓位；对账区间状态改为徽章。
- 修复 Tabulator 表格在深色模式下仍为白底。

## Windows 启动器修复

- 修复 `start_windows.bat` 一闪而过；安装脚本移到 `scripts/`。

## v3.1

- **指标总表**页：100 余项指标按“盈亏与成本 / 回合 / 收益 / 风险与尾部 / 回撤 / MAE·MFE”分组，跟随时间窗口切换；每项注明定义和来源（官方 / 日序列 / 成交 / 回合 / K 线）。
- **可选 MAE/MFE**（`--mae` 或 `"mae": true`）：每个回合的最大浮亏 / 浮盈、MFE 捕获率。
- **可选付费补数**（`backfill`，默认关闭）：只补 API 缺失的时间段，详见[付费补数](usage.md#付费补数可选默认关闭)。
- 分币种盈亏表、多空对比、盈利集中度帕累托曲线。
- 官方对账、`tools/` 下的独立核查脚本。

## v3.0

- 数据口径改为三层：官方 `portfolio` 真值、账户快照、成交与流水解释层；按日 K 线盯市重建，并与官方累计盈亏逐周对账。
