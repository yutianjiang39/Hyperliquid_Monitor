# 本地文件与续传

[← 返回 README](../README.md)

默认目录相对 `config.json` 所在位置解析，而不是当前终端目录：

| 路径 | 内容 |
| --- | --- |
| `config.json` | 首次保存后生成，包含钱包、日期、目录、AWS profile 和预算；不纳入版本管理，字段可参考 `config.example.json` |
| `data/钱包地址/wallet_history.sqlite` | 去重后的原始缓存、API 游标和归档检查点 |
| `data/钱包地址/archive_plan.json` | 归档清单，绑定钱包与冻结时间范围 |
| `data/钱包地址/views.sqlite` | 窗口分页索引，生成报告时更新 |
| `data/钱包地址/last_report.json` | 生成报告时写入的最近报告路径和汇总 |
| `data/demo/` | 独立模拟演示缓存 |
| `reports/` | 运行后生成的独立 HTML 报告目录和 ZIP |

官方净值各次抓取、账户快照、市场元数据、日 K 线、订单样本和质押记录都保存在该钱包的 SQLite 缓存中。刷新失败时保留此前数据并显示原因，不会把错误当作零。服务器的旧净值采样可能变粗，程序保留此前缓存的细采样。

每个报告目录包含：

| 路径 | 内容 |
| --- | --- |
| `index.html` | 报告入口，双击用浏览器打开 |
| `assets/` | 内置的 Tabler / ECharts / Tabulator 与页面脚本 |
| `data/*.js` | 成交、回合、资金流、资金费的分片明细 |
| `exports/operations.csv` 等 | 全部明细的 CSV |
| `exports/series_days.csv` | 日净值、日盈亏、资金流、日收益及来源 |
| `exports/mtm_days.csv` | 盯市重建的日分项：价格盈亏、手续费、资金费、敞口、补录名义金额 |
| `exports/reconciliation_intervals.csv` | 逐周对账区间 |
| `exports/monthly_returns.csv` | 月度收益 |
| `exports/model.json` | 页面用到的全部指标 |
| `audit/` | 原始成交/流水/资金费 JSONL、最新快照、市场元数据、对账与历史完整性判定、`accounting_rules.txt` 计算口径 |

关闭电脑或窗口后，已经提交的缓存仍保留。再次选择同一钱包和缓存目录即可继续。
同一钱包缓存有进程级运行锁，避免两个终端同时更新相同数据；正常退出或进程结束后锁自动释放。
备份时先停止任务/关闭程序，再复制该钱包的缓存目录。不要把整个 `.venv` 复制到其他系统，重新安装依赖即可。
