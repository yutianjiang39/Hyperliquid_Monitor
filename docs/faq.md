# 常见问题与参考

[← 返回 README](../README.md)

## 常见问题

**找不到 Python / 版本太旧**：安装 Python 3.11/3.12，再重开终端；Windows 检查 PATH。启动器不会自动下载安装 Python 本体。

**第一次安装失败**：先查看终端保留的错误，确认能访问 Python 包源。不要跳过依赖错误强行启动；修复后运行 `start.bat --install`（Windows）或 `bash start.sh --install`（macOS / Linux）即可。

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

## 参考文档

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
