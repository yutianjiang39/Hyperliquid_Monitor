# 计算口径与代码结构

[← 返回 README](../README.md)

账务引擎使用 Decimal 精度，处理开、加、减、清仓分类，反手拆分，同毫秒顺序和重复记录合并。`startPosition` 中的浮点噪声（如 `38272466.1000000015`）会先按 15 位有效数字还原，再判断仓位链是否连续。

- 回合统计只用完整观察到“开仓到清仓”的回合；含资金费的净盈亏单列。
- `fee` 已包含 builderFee，不重复扣除。
- 现货按快照价值估值，稳定币按 1 美元；非 USDC 结算的 HIP-3 市场按结算币现货价折算，并在质量页列出。
- 统一账户：现货 USDC 中被永续占用的 hold 不重复计入。

| 模块 | 作用 |
| --- | --- |
| `src/sources/` | 账户快照、市场元数据与多周期 K 线缓存、订单样本与质押、排行榜、第三方插件、付费补数（`backfill.py`、`backfill_targets.py`、`reservoir.py`） |
| `src/account_history.py` | 官方 portfolio 抓取与多次抓取合并 |
| `src/engine.py` | 成交分类、回合、资金费归属 |
| `src/model/` | 仓位链（`positions.py`）、日盯市重建（`mtm.py`）、资金流分类（`flows.py`） |
| `src/reconcile.py` | 历史完整性判定与逐周对账 |
| `src/metrics/` | 日序列、收益、风险、行为、成本、账户画像、MAE/MFE（`excursion.py`）、指标总表（`scorecard.py`） |
| `src/analysis.py` | 把以上组合成报告模型 |
| `src/report/` | Tabler + ECharts + Tabulator 离线报告；`src/vendor/manifest.json` 锁定前端库版本与 sha256 |

- 更新前端库：`python tools/vendor_fetch.py --force`，校验用 `--verify`。
- 录制测试夹具：`python tools/record_fixtures.py 0x…`。
- 单元测试在 `tests/`，用 `python app.py self-test` 或 `python -m pytest` 运行。

## 独立核查工具

`tools/` 下的三个脚本**不导入项目代码**，只读取报告目录里的导出文件（以及公开 API），用独立实现重算指标，再和报告对比：

| 脚本 | 核查内容 |
| --- | --- |
| `python tools/audit_report.py <报告目录> [--live]` | 指标总表约 60 项：成交、手续费、资金费、回合胜率 / 盈利因子 / 连胜、日序列恒等式、TWR、回撤、Sharpe / Omega / 尾部比率、月度表；加 `--live` 时还会和实时官方 portfolio 对比 |
| `python tools/audit_live_fills.py <报告目录> [天数]` | 最近 N 天成交逐笔对比 API（数量、价格、手续费、已实现盈亏），以及资金费合计 |
| `python tools/audit_mae.py <报告目录> [条数]` | 随机抽取 MAE/MFE 回合，重新下载 K 线后重算 |

报告中的成交比 `userFillsByTime` 多出的部分是 TWAP 分片成交（来自 `userTwapSliceFillsByTime`）；官方成交量包含这部分。
