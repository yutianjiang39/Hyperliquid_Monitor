# 命令行模式

[← 返回 README](../README.md)

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

## MAE / MFE（可选）

免费的 `candleSnapshot` 每个周期只给最近 5000 根 K 线。程序对每个有回合的币种把 4 个周期各取一次，缓存后只增量更新：

| 周期 | 5000 根覆盖 |
| --- | --- |
| 15m | 约 52 天 |
| 1h | 约 208 天 |
| 4h | 约 833 天 |
| 1d | 全部 |

每个回合按成交切成若干持仓段，每段用“覆盖得到的最细周期”扫描高低价：近期回合用 15m，远期依次退到 1h / 4h / 1d。浮动盈亏 = 段前已实现 + 当时仓位 × (极值价 − 持仓均价)，不含手续费和资金费。持仓短于所用 K 线的回合标为“精度不足”，不计入汇总均值。报告给出 MAE × 盈亏散点、MFE 捕获率分布和各周期使用次数。首次运行要下载 K 线（每币种 4 次请求，已节流），之后读缓存。

## 付费补数（可选，默认关闭）

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

## 第三方数据插件（可选，默认关闭）

| 插件 | 能力 | 环境变量 | 配置 |
| --- | --- | --- | --- |
| Hydromancer | 成交、流水、资金费（请求体与官方 /info 相同，历史更长） | `HYDROMANCER_API_KEY` | `plugins.hydromancer.enabled`、可选 `url` |
| Dune | 成交（读取你自己维护的、带 `wallet` 参数的已保存查询） | `DUNE_API_KEY` | `plugins.dune.enabled`、`query_id` |

- 密钥只从环境变量读取，配置文件拒绝出现密钥字段。
- 插件数据的来源标记为 `plugin:<名称>`，合并优先级最低：同一条记录有官方副本时，以官方为准。
- 插件均按对方计费，详见各自的套餐说明。

## 导入官方净值历史

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
