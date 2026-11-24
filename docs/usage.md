# 使用手册

[← 返回 README](../README.md)

## 终端窗口

- 输入目标钱包、UTC 起止日期、AWS profile、每轮下载预算。
- **API 抓取**（`run`）：依次更新以下数据，然后生成报告与本地分页视图：
  - 可见成交、资金流水、资金费；
  - 市场元数据与现货价格；
  - 官方 portfolio；
  - 账户快照、订单样本与质押记录。
- **更新快照 + 官方净值**（`snapshot`，旧名 `assets`）：更新官方净值与账户快照，增量补齐成交、资金费和转账到快照时刻，再生成报告。
- **归档清单**：列出官方 S3 实际可用对象、文件数和待下载压缩体积。
- **归档扫描**：确认清单及 requester-pays 费用后开始下载；达到预算暂停，重跑可续传。
- **导入文件**：合并目标钱包的 JSON / JSONL / CSV，类别为 fills、ledger、funding 或 portfolio。
- **生成报告**：只用本地缓存重算。已缓存的日 K 线不会重复下载；缺失的 K 线在联网时补抓。
- **演示**：不填钱包、不联网也能体验。数据明确标为模拟数据，并与实际钱包缓存隔离。
- **停止**：在安全检查点停止后台任务，已经提交的记录与文件检查点会保存。
- 窗口中可切换查看操作、战绩、转账、资金费和覆盖范围，每页最多 **50 行**；顶部显示官方累计盈亏与对账状态。

底部命令输入框也可直接操作，输入后按 Enter：

| 窗口命令 | 动作 |
| --- | --- |
| `/demo` | 运行模拟演示 |
| `/api` | 读取 API 并生成报告 |
| `/assets` | 更新官方净值与账户快照，增量补流水并生成报告 |
| `/plan` | 查看归档清单确认提示 |
| `/scan` | 按已有清单、预算扫描归档 |
| `/import` | 打开本地文件导入窗口 |
| `/report` | 用当前缓存重建报告 |
| `/open` | 在浏览器打开最近报告 |
| `/stop` | 请求停止当前任务 |
| `/help` | 查看帮助 |
| `/wallet 0x…` | 填入并保存目标钱包 |
| `/quit` | 退出窗口；任务运行时会先提示停止 |

可用 Tab 切换焦点，表格中按 Enter 查看该条记录的完整字段；Esc 请求停止，Ctrl+C 退出。

### 运行说明

`run` 执行完整更新；`snapshot` 只刷新官方净值与快照并增量补流水；`report` 只用缓存。仓位链需要衔接到当前快照，因此即使报告截止日较早，也会缓存截止日之后、到快照时刻为止的记录；报告可在页面上自由切换时间窗口。

报告通过浏览器 `file://` 打开本地 `index.html`，不需要启动网页服务器。可以查看逐笔变化并使用报告提供的筛选、缩放和明细；关闭浏览器不会删除缓存。

开始日期留空表示来源能提供的最早记录。结束日期留空表示任务开始时刻。
配置中的日期和报告中的时间都使用 **UTC**。截止只填 `2026-01-01` 代表该日零点，要包含整天请写 `2026-01-01T23:59:59.999Z`。

## 命令行模式

窗口之外，同一程序也支持命令行。下面以激活项目虚拟环境后的 `python` 为例；未激活时可使用 `.venv/bin/python`，Windows 使用 `.venv\Scripts\python.exe`。

```bash
python app.py --help
python app.py doctor
python app.py demo --open
python app.py init --wallet 0x你的钱包地址
python app.py run --open
python app.py snapshot --open
python app.py report --open
python app.py reconcile            # 对账；失败退出码 4，--json 输出全部区间
python app.py metrics              # 打印核心指标；--json 输出完整模型
python app.py plugins list         # 查看第三方插件状态
python app.py plugins test         # 用环境变量里的密钥测试连通性
python app.py plan --profile hyperliquid-history
python app.py scan --accept-requester-pays --budget-gib 10
python app.py import history.jsonl --kind fills
python app.py import portfolio.json --kind portfolio --open
python app.py self-test            # 运行 pytest（需 pip install -r requirements-extra.txt）
```

可选开关（只对本次运行生效，不写入配置）：

- `--leaderboard`：额外下载官方排行榜（数十 MB），核对该地址的排名与成交额分位。也可以在 `config.json` 中设 `"leaderboard": true`。
- `--plugins hydromancer,dune`：本次启用第三方历史插件，只补官方最早记录之前的历史。
- `--include-subaccounts`：同时抓取并分析每个子账户（各自独立缓存与对账），在主报告“持仓与资产”页汇总其官方盈亏、回撤和对账状态。也可设 `"include_subaccounts": true`。
- `--mae`：计算 MAE/MFE（见下）。也可设 `"mae": true`。

### MAE / MFE（可选）

免费的 `candleSnapshot` 每个周期只给最近 5000 根 K 线。程序对每个有回合的币种把 4 个周期各取一次，缓存后只增量更新：

| 周期 | 5000 根覆盖 |
| --- | --- |
| 15m | 约 52 天 |
| 1h | 约 208 天 |
| 4h | 约 833 天 |
| 1d | 全部 |

每个回合按成交切成若干持仓段，每段用“覆盖得到的最细周期”扫描高低价：近期回合用 15m，远期依次退到 1h / 4h / 1d。浮动盈亏 = 段前已实现 + 当时仓位 × (极值价 − 持仓均价)，不含手续费和资金费。持仓短于所用 K 线的回合标为“精度不足”，不计入汇总均值。报告给出 MAE × 盈亏散点、MFE 捕获率分布和各周期使用次数。首次运行要下载 K 线（每币种 4 次请求，已节流），之后读缓存。

### 付费补数（可选，默认关闭）

公开 API 能拿的已经全部在用。更早或中途缺失的成交只有两个来源，**都需要 AWS 账号，并按下载量向你的账号计费（requester-pays）**：

| 来源 | 内容 | 覆盖 | 读取方式 |
| --- | --- | --- | --- |
| `reservoir`：Hydromancer Reservoir `s3://hydromancer-reservoir`（东京区） | 每日 Parquet：成交、每日持仓快照 | 2025-07-28 起 | 按字节范围读取，先看 row group 统计跳过不含本钱包的块，只下载需要的列 |
| `official_s3`：官方 `hl-mainnet-node-data` | 每小时 lz4：全市场成交 | 2025-05-25 起 | 只列出、只下载缺口所在小时的文件（每小时一个，优先 `node_fills_by_block`） |

2025-05-25 之前的成交，任何官方或免费来源都拿不到。

```json
"backfill": {"enabled": true, "sources": ["reservoir", "official_s3"], "max_cost_usd": 5.0, "mode": "gaps"}
```

- 只有 `enabled: true` **且** boto3 能找到 AWS 凭证（`aws.profile` 或标准环境变量）时，`run` / `snapshot` 才会补数；否则日志和质量页显示“未启用付费补数”，不发出任何 AWS 请求。
- `mode: "gaps"` 只补仓位链断点（上一笔成交到下一笔成交之间）和开头截断（归档起点到首笔可见成交）；`"full"` 补归档起点之后的全部。
- `max_cost_usd`：按传输量 × $0.09/GB 估算的本次上限。官方 S3 先列清单估算，超出就不下载；Reservoir 逐次累计，到上限即停。请求费另计。
- 按配置顺序执行，每个来源之后重算缺口，前一个已补上的不再付费读取。
- Reservoir 的每日持仓快照会校正缺口：缺口内有快照时，补录成交被拆到快照两侧，质量页显示“由持仓快照校正的缺口”数。
- 补来的记录来源标记为 `plugin:reservoir` 或 `s3:…`，同一条成交以官方 API 副本为准。
- Reservoir 需要 `pip install -r requirements-extra.txt`（pyarrow）。Reservoir 公开文档里的目录结构版本不一，程序首次运行会列目录自动识别，结果缓存在钱包库中；也可用 `backfill.reservoir_prefix` 限定前缀。

### 第三方数据插件（可选，默认关闭）

| 插件 | 能力 | 环境变量 | 配置 |
| --- | --- | --- | --- |
| Hydromancer | 成交、流水、资金费（请求体与官方 /info 相同，历史更长） | `HYDROMANCER_API_KEY` | `plugins.hydromancer.enabled`、可选 `url` |
| Dune | 成交（读取你自己维护的、带 `wallet` 参数的已保存查询） | `DUNE_API_KEY` | `plugins.dune.enabled`、`query_id` |

- 密钥只从环境变量读取，配置文件拒绝出现密钥字段。
- 插件数据的来源标记为 `plugin:<名称>`，合并优先级最低：同一条记录有官方副本时，以官方为准。
- 插件均按对方计费，详见各自的套餐说明。

### 导入官方净值历史

可以直接导入官方 `portfolio` 响应的原生 JSON 数组。若文件有包装，支持以下结构；`wallet` 可用 `user` 代替，填写后会核对是否与当前钱包一致：

```json
{
  "wallet": "0x你的钱包地址",
  "user_abstraction": "unifiedAccount",
  "portfolio": [
    ["allTime", {"accountValueHistory": [[1730000000000, "10000.25"]], "pnlHistory": [], "vlm": "0"}]
  ]
}
```

上例中的钱包占位符必须替换为实际 42 字符地址；账户模式应填真实来源的模式。联网运行一次 `snapshot` 可获取账户模式与快照。

CSV 支持以下格式，`pnl` 列可省略；`time` 为整数毫秒或带时区的 ISO 时间。必须提供 `window`，避免混淆永续与综合净值：

```csv
window,time,account_value,pnl
perpAllTime,1730000000000,10000.25,120.00
allTime,1730000000000,15000.25,130.00
```

```bash
python app.py import portfolio_samples.csv --kind portfolio --open
```

仅 `fills/ledger/funding` 导入使用 `--timestamp-unit`；portfolio CSV 时间须按上述格式填写。不合法数值不会补零；有效历史按窗口和时间合并，原缓存中不同时间的点会保留。

自定义配置文件可放在命令前或后：

```bash
python app.py --config work/config.json run
python app.py run --config work/config.json
```

一次执行 API、清单与按预算归档扫描：

```bash
python app.py run --archive --accept-requester-pays --budget-gib 10 --open
```

建议首次单独运行 `plan`，看清实际体积后再下载。`--json` 让结果写 stdout、进度写 stderr，便于脚本调用。
退出码：0 完成所选任务；1 错误；2 用法/终端环境问题；3 部分失败或预算暂停；4 对账失败（`reconcile`）；130 用户停止。
任务完成仍应检查报告的数据覆盖状态，不能把退出码 0 当作终身历史完整证明。

## AWS：本地凭证配置

使用 Boto3 的标准 AWS 凭证链：已有 profile、环境变量或标准凭证文件。界面中的 profile 填已有配置名称，留空使用默认凭证链。程序配置文件只保存 profile 名、区域和预算，**不保存 access key、secret key 或 session token**。

如果你已安装 AWS CLI，可用其标准配置方式建立专用 profile：

```bash
aws configure --profile hyperliquid-history
```

然后在窗口 profile 中填 `hyperliquid-history`。没有 AWS CLI 时也可按 Boto3 官方凭证文档设置标准环境变量/凭证文件；AWS CLI 不是运行本程序的必要依赖。

官方 S3 是 requester-pays 数据来源。**列清单可能有请求费用，下载产生传输/请求费用。** 按钱包筛选仍要下载、解压全市场文件；不是只收取目标钱包的几 KB 数据量。预算按计划压缩字节估算，重试等费用另计。

每次归档清单固定起止时间。稍后续传使用同一范围；要补到更新时刻，请重新列清单。配置起止日期变更后，过期清单不能直接用于付费扫描。

## 本地文件与续传

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
