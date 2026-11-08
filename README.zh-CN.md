# Hyperliquid Local · 钱包分析 v3.1（官方真值 + 盯市对账 + 指标总表 + Tabler 报告）

查看 Hyperliquid 单钱包的真实盈亏、收益率、回撤、风险、交易行为、成本与持仓，支持本地缓存、AWS 历史回填和完全离线的 HTML 报告。

程序在你的电脑上运行，公开 Hyperliquid 查询不需要私钥。更新真实数据需要联网；已生成报告、缓存重算和模拟演示可以离线运行。

### v3.0 的数据口径

旧版用成交和流水去“反推”资产和盈亏，API 只保留近期成交，所以历史一长就既不全也不准。v3.0 改为三层：

| 层级 | 来源 | 用途 |
| --- | --- | --- |
| **真值** | 官方 `portfolio`（各窗口净值、累计盈亏、成交额） | 收益、回撤、风险指标全部锚定在这里，数值与官方页面一致 |
| **当前状态真值** | 账户快照：所有永续 DEX（含 HIP-3）的 `clearinghouseState`、现货、子账户、金库、质押、挂单、费率档 | 持仓、资产构成、杠杆、费率 |
| **解释层** | 成交、资金费、资金流水（API + 可选 S3 归档 / 导入 / 第三方插件） | 解释盈亏从哪里来：价格、手续费、资金费、交易行为 |

**盯市对账**：按日 K 线收盘价重建永续盈亏 `期末市值 − 期初市值 − 买卖现金 − 手续费 + 资金费`，与官方累计盈亏逐周对账：

- 残差 ≤ max(1%, $100) 为“通过”，≤ 5% 为“有偏差”，否则为“失败”；
- 残差和每个区间的结果都公开在“数据质量”页。

**缺失成交**：成交链的 `startPosition` 前后对不上，说明中间有成交没返回。程序按缺口中点和当日收盘价补录一笔，并统计补录的名义金额。报告把这类历史标为“历史有缺口”，与“开头被截断”区分开。

**日序列**：官方采样之间以重建的日盈亏为形状，残差平均分摊，所以区间合计严格等于官方值。隐含资金流 = Δ净值 − Δ盈亏。

### v3.1 新增

- **指标总表**页：约 70 项指标按“盈亏与成本 / 回合 / 收益 / 风险与尾部 / 回撤 / MAE·MFE”分组，跟随时间窗口切换；每项注明定义和来源（官方 / 日序列 / 成交 / 回合 / K 线）。
- **可选 MAE/MFE**（`--mae` 或 `"mae": true`）：每个回合的最大浮亏 / 浮盈、MFE 捕获率。
- **可选付费补数**（`backfill`，默认关闭）：只补 API 缺失的时间段，详见[付费补数](#付费补数可选默认关闭)。
- 分币种盈亏表、多空对比、盈利集中度帕累托曲线。

### 量化指标

- **收益**：
  - 累计盈亏、TWR（Modified Dietz 串联）、年化、XIRR（资金加权）、投入资本回报；
  - 最佳/最差日、周、月，月度收益日历；
  - 7 天 / 30 天 / 90 天 / 年初至今 / 全部窗口切换。
- **风险**：
  - 最大回撤（百分比与金额）、回撤段、水下曲线；
  - Sharpe、Sortino、Calmar、**Omega**、**尾部比率**（日收益 95 分位 ÷ |5 分位|）、VaR/CVaR 95%；
  - 回撤时长（峰→谷）、恢复时长（谷→新高）、最长水下、回撤次数（≥5%）与平均深度、水下 / 创新高时间占比、恢复因子、盈利峰值、初始亏损；
  - 月度胜率与月收益标准差；杠杆调整 ROI（TWR ÷ 平均日末杠杆，日内交易者不适用时留空）；
  - Ulcer、偏度、峰度、30 日滚动指标放在风险页“更多统计”折叠区；
  - 按盯市敞口计算的杠杆序列。
- **交易行为**：
  - 回合胜率、盈亏比、Profit factor、期望值、连胜连败、平均 / 中位持仓时长；
  - 盈亏效率比（净盈亏 ÷ (总盈利 + |总亏损|)）、**盈利集中度**（前 10% 回合占总盈利、最大单笔占比）；
  - 分币种盈亏（已实现、手续费、资金费、净额、胜率、多空），分多空的胜率与盈亏；
  - 资金费占盈亏比、Maker 占比、换手倍数（成交量 ÷ 当前 / 平均净值）、单位成交额收益（bps）；
  - 持仓时长分布，多空拆分，盈亏 × 持仓时长散点；
  - 小时 × 星期热力图，Maker / TWAP / HIP-3 / 现货占比，强平次数（只计本钱包被强平的成交）。
- **成本**：手续费（bps）、资金费收付（按币种）、Builder 费、成本占毛利比例、费率档与折扣。
- **持仓与资产**：当前持仓、各账户桶价值、现货余额、子账户、金库、质押、挂单。
- **画像**：频率、风格、方向、集中度、杠杆等标签，附判定规则。

### 报告

报告使用成熟的开源组件，全部随包内置并校验 sha256，打开时不访问网络：

- [Tabler](https://tabler.io/)：页面框架；
- [Apache ECharts](https://echarts.apache.org/)：图表；
- [Tabulator](https://tabulator.info/)：可筛选、排序的大表格。

页面分为：总览、指标总表、业绩、风险、交易行为、持仓与资产、成本、资金流、数据质量、原始数据。

- 支持深色模式。
- 每个数字标明来源：官方 / 重建 / 混合。
- 明细按需分片加载，几十万条成交也能流畅打开。

## 1. 快速启动

需要 **Python 3.10+，推荐 Python 3.11 或 3.12**。请先完整解压到可写文件夹，不要直接在压缩包中运行。

### Windows

1. 安装 Python，并在安装时启用 **Add Python to PATH**。
2. 双击 `start_windows.bat`。
3. 首次运行自动创建项目专用 `.venv` 并安装依赖；随后进入终端窗口。
4. 建议把窗口放大到至少 **100 列 × 30 行**，120 × 40 更舒适。Windows Terminal、PowerShell 或正常命令提示符均可作为终端宿主。

如果需要分开安装/启动，可先双击 `install_windows.bat`，再启动。安装器只使用已有 Python，不安装 Python 本体，不请求管理员权限。

### macOS

安装 Python 后，可双击 `Start_macOS.command`。也可以在终端进入解压目录运行：

```bash
bash start_macos_linux.sh
```

首次运行同样会自动创建 `.venv`、安装依赖。如果系统默认 `python3` 版本较旧，可指定已安装的解释器：

```bash
HL_PYTHON_BIN=/path/to/python3.12 bash install_macos_linux.sh
bash start_macos_linux.sh
```

### Linux

在解压目录的终端中运行：

```bash
bash start_macos_linux.sh
```

部分发行版需要先安装其 Python venv 支持包，具体以 `python3 -m venv` 的提示为准。

**请使用终端窗口，避免 IDE 的纯文本输出面板或 Jupyter 输出区。** 它们通常无法提供交互终端所需的键盘、尺寸和鼠标事件。

## 2. 窗口中可以做什么

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

## 3. 推荐使用顺序

1. 第一次先运行“演示”，确认终端显示和本地文件权限正常。
2. 填入真实目标钱包，运行“API 抓取”。不要填写 API agent 地址、私钥或助记词。
3. 若要回补更早历史，导入已有历史导出，或设置标准 AWS 凭证后列归档清单。
4. 查看清单体积，再扫描归档。每轮默认 10 GiB，达到预算后续跑同一清单；它是压缩字节预算，不是账单金额上限。
5. 在表格中查看数据，或打开报告。HTML 报告目录中的 `data`、`exports`、`audit` 文件夹需要一起保留。

`run` 执行完整更新；`snapshot` 只刷新官方净值与快照并增量补流水；`report` 只用缓存。仓位链需要衔接到当前快照，因此即使报告截止日较早，也会缓存截止日之后、到快照时刻为止的记录；报告可在页面上自由切换时间窗口。

报告通过浏览器 `file://` 打开本地 `index.html`，不需要启动网页服务器。可以查看逐笔变化并使用报告提供的筛选、缩放和明细；关闭浏览器不会删除缓存。

开始日期留空表示来源能提供的最早记录。结束日期留空表示任务开始时刻。
配置中的日期和报告中的时间都使用 **UTC**。截止只填 `2026-01-01` 代表该日零点，要包含整天请写 `2026-01-01T23:59:59.999Z`。

## 4. “全部历史”的实际边界

官方文档标注 `userFillsByTime` 每次最多 2,000 条、最近 10,000 条的可用范围；实际响应或已有缓存可能更多，程序按真实返回保留全部已获得记录，不承诺接口返回钱包的全部历史。更早数据通过官方归档或已有导出补充。TWAP、非资金费流水和资金费分开读取，成交会合并去重。限制和分页字段见[官方 Info API](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint#retrieve-a-users-fills-by-time)。

官方归档前缀：

- `node_fills_by_block`、`node_fills`：新旧完整 fill 格式。
- `node_trades`：更早旧格式，缺少官方 `closedPnl` / fee 时保留未知。
- `misc_events_by_block`：可识别的转账、资金费等非成交事件；未知目标事件保存在审计输出，标明解释缺口。

程序显示 API 可见范围、归档清单范围、已扫描对象、缺失小时、未知字段和失败对象。**“清单扫描完毕”不自动等于钱包终身历史被证明完整。**

范围是目标 **HyperCore 单地址**的成交/仓位变化/资金流水/资金费。子账户可用 `--include-subaccounts` 一并分析，Vault 自有地址需单独运行；未成交撤单、每次杠杆变更、HyperEVM 或其他链通用钱包动作不自动包含。

## 5. AWS：本地凭证配置

使用 Boto3 的标准 AWS 凭证链：已有 profile、环境变量或标准凭证文件。界面中的 profile 填已有配置名称，留空使用默认凭证链。程序配置文件只保存 profile 名、区域和预算，**不保存 access key、secret key 或 session token**。

如果你已安装 AWS CLI，可用其标准配置方式建立专用 profile：

```bash
aws configure --profile hyperliquid-history
```

然后在窗口 profile 中填 `hyperliquid-history`。没有 AWS CLI 时也可按 Boto3 官方凭证文档设置标准环境变量/凭证文件；AWS CLI 不是运行本程序的必要依赖。

官方 S3 是 requester-pays 数据来源。**列清单可能有请求费用，下载产生传输/请求费用。** 按钱包筛选仍要下载、解压全市场文件；不是只收取目标钱包的几 KB 数据量。预算按计划压缩字节估算，重试等费用另计。

每次归档清单固定起止时间。稍后续传使用同一范围；要补到更新时刻，请重新列清单。配置起止日期变更后，过期清单不能直接用于付费扫描。

## 6. 本地文件与续传

默认目录相对 `config.json` 所在位置解析，而不是当前终端目录：

| 路径 | 内容 |
| --- | --- |
| `config.json` | 首次保存后生成，包含钱包、日期、目录、AWS profile 和预算 |
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

## 7. 命令行模式

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
python app.py self-test            # 运行 pytest（需 pip install -r requirements-dev.txt）
```

可选开关（只对本次运行生效，不写入配置）：

- `--leaderboard`：额外下载官方排行榜（数十 MB），核对该地址的排名与成交额分位。也可以在 `config.json` 中设 `"leaderboard": true`。
- `--plugins hydromancer,dune`：本次启用第三方历史插件，只补官方最早记录之前的历史。
- `--include-subaccounts`：同时抓取并分析每个子账户（各自独立缓存与对账），在主报告“持仓与资产”页汇总其官方盈亏、回撤和对账状态。也可设 `"include_subaccounts": true`。
- `--mae`：计算 MAE/MFE（见下）。也可设 `"mae": true`。

#### MAE / MFE（可选）

免费的 `candleSnapshot` 每个周期只给最近 5000 根 K 线。程序对每个有回合的币种把 4 个周期各取一次，缓存后只增量更新：

| 周期 | 5000 根覆盖 |
| --- | --- |
| 15m | 约 52 天 |
| 1h | 约 208 天 |
| 4h | 约 833 天 |
| 1d | 全部 |

每个回合按成交切成若干持仓段，每段用“覆盖得到的最细周期”扫描高低价：近期回合用 15m，远期依次退到 1h / 4h / 1d。浮动盈亏 = 段前已实现 + 当时仓位 × (极值价 − 持仓均价)，不含手续费和资金费。持仓短于所用 K 线的回合标为“精度不足”，不计入汇总均值。报告给出 MAE × 盈亏散点、MFE 捕获率分布和各周期使用次数。首次运行要下载 K 线（每币种 4 次请求，已节流），之后读缓存。

#### 付费补数（可选，默认关闭）

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
- Reservoir 需要 `pip install -r requirements-optional.txt`（pyarrow）。Reservoir 公开文档里的目录结构版本不一，程序首次运行会列目录自动识别，结果缓存在钱包库中；也可用 `backfill.reservoir_prefix` 限定前缀。

#### 第三方数据插件（可选，默认关闭）

| 插件 | 能力 | 环境变量 | 配置 |
| --- | --- | --- | --- |
| Hydromancer | 成交、流水、资金费（请求体与官方 /info 相同，历史更长） | `HYDROMANCER_API_KEY` | `plugins.hydromancer.enabled`、可选 `url` |
| Dune | 成交（读取你自己维护的、带 `wallet` 参数的已保存查询） | `DUNE_API_KEY` | `plugins.dune.enabled`、`query_id` |

- 密钥只从环境变量读取，配置文件拒绝出现密钥字段。
- 插件数据的来源标记为 `plugin:<名称>`，合并优先级最低：同一条记录有官方副本时，以官方为准。
- 插件均按对方计费，详见各自的套餐说明。

#### 导入官方净值历史

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

## 8. 计算口径与代码结构

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

### 独立核查工具

`tools/` 下的三个脚本**不导入项目代码**，只读取报告目录里的导出文件（以及公开 API），用独立实现重算指标，再和报告对比：

| 脚本 | 核查内容 |
| --- | --- |
| `python tools/audit_report.py <报告目录> [--live]` | 指标总表约 60 项：成交、手续费、资金费、回合胜率 / 盈利因子 / 连胜、日序列恒等式、TWR、回撤、Sharpe / Omega / 尾部比率、月度表；加 `--live` 时还会和实时官方 portfolio 对比 |
| `python tools/audit_live_fills.py <报告目录> [天数]` | 最近 N 天成交逐笔对比 API（数量、价格、手续费、已实现盈亏），以及资金费合计 |
| `python tools/audit_mae.py <报告目录> [条数]` | 随机抽取 MAE/MFE 回合，重新下载 K 线后重算 |

报告中的成交比 `userFillsByTime` 多出的部分是 TWAP 分片成交（来自 `userTwapSliceFillsByTime`）；官方成交量包含这部分。

## 9. 常见问题

**找不到 Python / 版本太旧**：安装 Python 3.11/3.12，再重开终端；Windows 检查 PATH。启动器不会自动下载安装 Python 本体。

**第一次安装失败**：先查看终端保留的错误，确认能访问 Python 包源。不要跳过依赖错误强行启动；修复后重新运行安装脚本即可。

**窗口较窄、按钮显示不全**：放大终端或缩小字体；设置区可滚动，推荐 120 × 40。

**终端显示方块字或对齐异常**：使用支持中文和等宽字符的终端字体，并保持 UTF-8。Windows 启动器会设置 UTF-8。

**按停止没有立刻消失**：程序正等待当前网络请求或一个分析/写报告阶段结束；停止按钮会设置取消请求，在安全检查点提交并关闭缓存。

**AWS 没权限、403 或无凭证**：这是归档身份/权限问题，不会当成“没有历史”；API/导入/演示仍可使用。

**只看到最近数据**：普通成交 API 只保留近期成交。收益、回撤等指标仍来自官方净值，不受影响；交易行为统计只覆盖可见成交。需要更早的明细时，请合并归档、导入导出文件或启用插件。

**对账显示“有偏差”或“失败”**：常见原因有三种。
1. API 没返回部分成交：质量页会列出断点和补录的币种。
2. 缺少某个币种的日 K 线。
3. 非 USDC 结算市场的折算误差。

残差会公开显示，指标仍以官方净值为准。可以导入更早的成交，或开启插件补历史后重跑。

**历史“开头被截断”和“有缺口”的区别**：
- 开头被截断：首笔可见成交晚于官方净值起点，且有期初持仓或成交额明显不足。
- 有缺口：开头完整，只是中间缺了一些成交。

**收益率为什么和“盈亏 ÷ 本金”不同**：TWR 剔除了入金、出金的时点影响，衡量的是交易能力。XIRR 计入资金进出的时点，衡量的是资金实际收益。两者都会在业绩页给出。

**只复制 HTML 后没有明细**：请完整解压报告 ZIP，保留相邻 `data/exports/audit` 目录，再打开 `index.html`。

**钱包为空/无记录**：确认是实际账户地址；API agent 地址和子账户地址不是同一个账户。

## 10. 参考文档

- [Hyperliquid 历史归档](https://hyperliquid.gitbook.io/hyperliquid-docs/historical-data)
- [Hyperliquid Info API](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint)
- [资金流水接口](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals)
- [官方资产历史采样说明](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/portfolio-graphs)
- [开仓成本与盈亏](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/entry-price-and-pnl)
- [Hydromancer API](https://docs.hydromancer.xyz/)
- [Dune API](https://docs.dune.com/api-reference/overview/introduction)
- [统一账户余额口径](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/spot)
- [节点原始格式](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/nodes/l1-data-schemas)
- [Textual 后台任务](https://textual.textualize.io/guide/workers/)
- [Python venv](https://docs.python.org/3/library/venv.html)
- [Boto3 凭证配置](https://boto3.amazonaws.com/v1/documentation/api/latest/guide/credentials.html)

