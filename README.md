# Hyperliquid Monitor · 钱包分析 v3.1

官方真值 + 盯市对账 + 指标总表 + 离线 Tabler 报告。

查看 Hyperliquid 单钱包的真实盈亏、收益率、回撤、风险、交易行为、成本与持仓，支持本地缓存、AWS 历史回填和完全离线的 HTML 报告。

程序在你的电脑上运行，公开 Hyperliquid 查询不需要私钥。更新真实数据需要联网；已生成报告、缓存重算和模拟演示可以离线运行。

## 功能

- **官方真值**：收益、回撤、风险指标锚定官方 `portfolio`，数值与官方页面一致。
- **盯市对账**：按日 K 线重建盈亏，与官方累计盈亏逐周对账，残差公开在报告里。
- **指标总表**：100 余项收益、风险、回合、成本指标，每项注明定义和来源。
- **离线报告**：单个 HTML 目录，打开时不访问网络，支持深色模式。
- **本地缓存**：SQLite 去重缓存，支持断点续传和文件导入。
- **可选补数**：官方 S3 归档、Hydromancer、Dune，默认全部关闭。

## 快速启动

需要 **Python 3.10+，推荐 Python 3.11 或 3.12**。请先完整解压到可写文件夹，不要直接在压缩包中运行。

### Windows

1. 安装 Python，并在安装时启用 **Add Python to PATH**。
2. 双击 `start.bat`。首次运行自动创建项目专用 `.venv` 并安装依赖，随后进入终端窗口。
3. 建议把窗口放大到至少 **100 列 × 30 行**，120 × 40 更舒适。Windows Terminal、PowerShell 或正常命令提示符均可作为终端宿主。

### macOS / Linux

在解压目录的终端中运行：

```bash
bash start.sh
```

首次运行同样会自动创建 `.venv`、安装依赖。如果系统默认 `python3` 版本较旧，可指定已安装的解释器：

```bash
HL_PYTHON_BIN=/path/to/python3.12 bash start.sh --install
bash start.sh
```

部分 Linux 发行版需要先安装其 Python venv 支持包，具体以 `python3 -m venv` 的提示为准。

### 只安装或修复依赖

`start.bat --install`（Windows）或 `bash start.sh --install`（macOS / Linux）只创建 `.venv` 并安装依赖，不启动程序。安装只使用已有 Python，不安装 Python 本体，不请求管理员权限。

| 依赖文件 | 内容 |
| --- | --- |
| `requirements.txt` | 运行所需，启动脚本自动安装 |
| `requirements-extra.txt` | 可选：pytest（`self-test`）和 pyarrow（Reservoir 付费补数），需要时手动 `pip install -r requirements-extra.txt` |

**请使用终端窗口，避免 IDE 的纯文本输出面板或 Jupyter 输出区。** 它们通常无法提供交互终端所需的键盘、尺寸和鼠标事件。

## 使用顺序

1. 第一次先运行“演示”，确认终端显示和本地文件权限正常。
2. 填入真实目标钱包，运行“API 抓取”。不要填写 API agent 地址、私钥或助记词。
3. 若要回补更早历史，导入已有历史导出，或设置标准 AWS 凭证后列归档清单。
4. 查看清单体积，再扫描归档。每轮默认 10 GiB，达到预算后续跑同一清单；它是压缩字节预算，不是账单金额上限。
5. 在表格中查看数据，或打开报告。HTML 报告目录中的 `data`、`exports`、`audit` 文件夹需要一起保留。

窗口各功能见[使用手册](docs/usage.md#终端窗口)。

## 常用命令

```bash
python app.py demo --open      # 模拟演示
python app.py init --wallet 0x你的钱包地址
python app.py run --open       # 完整更新并生成报告
python app.py report --open    # 只用缓存重算
python app.py doctor           # 环境自检
```

完整命令和可选开关见[使用手册](docs/usage.md#命令行模式)。

## 文档

| 文档 | 内容 |
| --- | --- |
| [使用手册](docs/usage.md) | 终端窗口、全部命令与可选开关、MAE/MFE、付费补数、第三方插件、导入、AWS 凭证、本地文件结构 |
| [数据口径与指标](docs/methodology.md) | 三层数据来源、盯市对账、量化指标、报告页面、计算口径与代码结构 |
| [常见问题与参考](docs/faq.md) | 安装、终端、AWS、报告相关问题，官方接口文档链接 |
| [变更记录](CHANGELOG.md) | 各版本变更 |

## 许可证

本项目代码以 [MIT 许可证](LICENSE) 发布。`src/vendor/` 下内置的 Tabler、Apache ECharts、Tabulator 以及 Inter、JetBrains Mono 字体仍按各自的许可证分发。
