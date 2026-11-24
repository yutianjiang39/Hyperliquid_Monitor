# AWS：本地凭证配置

[← 返回 README](../README.md)

使用 Boto3 的标准 AWS 凭证链：已有 profile、环境变量或标准凭证文件。界面中的 profile 填已有配置名称，留空使用默认凭证链。程序配置文件只保存 profile 名、区域和预算，**不保存 access key、secret key 或 session token**。

如果你已安装 AWS CLI，可用其标准配置方式建立专用 profile：

```bash
aws configure --profile hyperliquid-history
```

然后在窗口 profile 中填 `hyperliquid-history`。没有 AWS CLI 时也可按 Boto3 官方凭证文档设置标准环境变量/凭证文件；AWS CLI 不是运行本程序的必要依赖。

官方 S3 是 requester-pays 数据来源。**列清单可能有请求费用，下载产生传输/请求费用。** 按钱包筛选仍要下载、解压全市场文件；不是只收取目标钱包的几 KB 数据量。预算按计划压缩字节估算，重试等费用另计。

每次归档清单固定起止时间。稍后续传使用同一范围；要补到更新时刻，请重新列清单。配置起止日期变更后，过期清单不能直接用于付费扫描。
