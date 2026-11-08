"""One flat, labelled table of wallet metrics per time window.

Each item is ``{key, label, value, unit, source, definition}``; the values are
taken from the other metric modules, this file only selects, derives ratios
and documents. ``source`` says which tier a number comes from:

- ``official``: Hyperliquid portfolio endpoint (the truth for PnL/volume).
- ``daily``: daily series built on the official equity curve.
- ``fills``: visible fills/funding (may be truncated; see quality page).
- ``campaigns``: fully observed campaigns only.
- ``candles``: MAE/MFE from cached candles (optional).
"""
from __future__ import annotations

UNITS = ("usd", "pct", "bps", "x", "int", "days", "hours", "num")


def _item(key, label, value, unit, source, definition):
    return {"key": key, "label": label, "value": value, "unit": unit, "source": source, "definition": definition}


def _num(value):
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def _div(a, b):
    return a / b if a is not None and b not in (None, 0) else None


def scorecard(*, perf, risk, behavior, activity, costs, official=None, equity=None,
              coins=None, excursion=None):
    """Grouped metric list for one window; inputs are that window's results."""
    perf, risk, behavior = perf or {}, risk or {}, behavior or {}
    activity, costs, official = activity or {}, costs or {}, official or {}
    official_pnl, official_vlm = _num(official.get("pnl")), _num(official.get("vlm"))
    total_pnl = official_pnl if official_pnl is not None else perf.get("pnl")
    pnl_source = "official" if official_pnl is not None else "daily"
    volume = official_vlm if official_vlm is not None else activity.get("volume")
    vol_source = "official" if official_vlm is not None else "fills"
    realized = sum(r["realized"] for r in coins or [])
    fees = costs.get("fees")
    funding = costs.get("funding_net")
    mean_equity = perf.get("mean_equity")
    dd, usd_dd = risk.get("drawdown_stats") or {}, risk.get("usd_drawdown_stats") or {}
    conc = behavior.get("concentration") or {}
    by_dir = behavior.get("by_direction") or {}
    month = perf.get("monthly_stability") or {}
    lev = risk.get("leverage_mean")

    groups = [
        ("盈亏与成本", [
            _item("total_pnl", "总盈亏（含手续费、资金费）", total_pnl, "usd", pnl_source,
                  "官方 portfolio 窗口盈亏，已扣手续费、含资金费和未实现盈亏"),
            _item("trading_net", "交易净盈亏", realized - (fees or 0) if coins else None, "usd", "fills",
                  "Σ closedPnl − 手续费（可见成交，不含资金费和未实现）"),
            _item("fees", "手续费", fees, "usd", "fills", "可见成交的 USDC 手续费合计"),
            _item("fee_bps", "手续费率", costs.get("fee_bps"), "bps", "fills", "手续费 ÷ 可见成交额 × 1万"),
            _item("funding", "资金费净额", funding, "usd", "fills", "资金费收入 − 支出，正数为净收入"),
            _item("funding_share", "资金费占盈亏比", _div(funding, abs(total_pnl) if total_pnl else None), "pct", "fills",
                  "资金费净额 ÷ |总盈亏|；高说明靠资金费套利"),
            _item("maker_share", "Maker 占比", activity.get("maker_share"), "pct", "fills", "挂单成交额 ÷ 全部成交额"),
            _item("volume", "累计成交量", volume, "usd", vol_source, "窗口内成交额（官方 vlm 优先）"),
            _item("turnover_equity", "成交量 ÷ 当前净值", _div(volume, equity), "x", vol_source, "换手倍数，按当前官方净值"),
            _item("turnover_mean", "成交量 ÷ 平均净值", _div(volume, mean_equity), "x", vol_source, "换手倍数，按窗口平均净值"),
            _item("pnl_per_volume", "单位成交额收益", _div(total_pnl * 1e4 if total_pnl is not None else None, volume),
                  "bps", pnl_source, "总盈亏 ÷ 成交量 × 1万"),
            _item("fills", "总成交笔数", activity.get("fills"), "int", "fills", "可见成交条数"),
            _item("avg_fill", "平均单笔成交额", _div(activity.get("volume"), activity.get("fills")), "usd", "fills",
                  "可见成交额 ÷ 成交笔数"),
        ]),
        ("回合", [
            _item("campaigns", "总回合数", behavior.get("campaigns_evaluated"), "int", "campaigns",
                  "开仓到平仓完整可见的回合；另有 %s 个不完整回合未计入" % behavior.get("campaigns_excluded", 0)),
            _item("wins", "盈利回合数", behavior.get("wins"), "int", "campaigns", "净盈亏（含资金费）> 0 的回合"),
            _item("win_rate", "胜率", behavior.get("win_rate"), "pct", "campaigns", "盈利回合 ÷ 全部回合"),
            _item("mean_hold", "平均持仓时长", behavior.get("mean_hold_hours"), "hours", "campaigns", "开仓到平仓时长的均值"),
            _item("median_hold", "持仓时长中位数", behavior.get("median_hold_hours"), "hours", "campaigns", "开仓到平仓时长的中位数"),
            _item("avg_win", "平均盈利", behavior.get("avg_win"), "usd", "campaigns", "盈利回合的平均净盈亏"),
            _item("avg_loss", "平均亏损", behavior.get("avg_loss"), "usd", "campaigns", "亏损回合的平均净盈亏"),
            _item("payoff", "盈亏比", behavior.get("payoff_ratio"), "x", "campaigns", "平均盈利 ÷ |平均亏损|"),
            _item("profit_factor", "盈利因子", behavior.get("profit_factor"), "x", "campaigns", "总盈利 ÷ |总亏损|"),
            _item("expectancy", "每回合期望值", behavior.get("expectancy"), "usd", "campaigns", "全部回合净盈亏的平均"),
            _item("efficiency", "盈亏效率比", behavior.get("efficiency_ratio"), "pct", "campaigns",
                  "净盈亏 ÷ (总盈利 + |总亏损|)；1 为从不亏损，0 为来回打平"),
            _item("max_win_streak", "最大连胜", behavior.get("max_win_streak"), "int", "campaigns", "按平仓时间排序的连续盈利回合数"),
            _item("max_loss_streak", "最大连败", behavior.get("max_loss_streak"), "int", "campaigns", "按平仓时间排序的连续亏损回合数"),
            _item("top10_share", "盈利集中度（前 10%）", conc.get("top_share"), "pct", "campaigns",
                  "盈亏最高的前 10%% 回合（%s 个）占总盈利的比例；高说明靠少数大单" % conc.get("top_count", 0)),
            _item("largest_share", "最大单笔占总盈利", conc.get("largest_share"), "pct", "campaigns", "单个最赚回合 ÷ 总盈利"),
            _item("long_pnl", "多头盈亏", (by_dir.get("long") or {}).get("pnl"), "usd", "campaigns", "多头回合净盈亏合计"),
            _item("long_win_rate", "多头胜率", (by_dir.get("long") or {}).get("win_rate"), "pct", "campaigns", "多头回合胜率"),
            _item("short_pnl", "空头盈亏", (by_dir.get("short") or {}).get("pnl"), "usd", "campaigns", "空头回合净盈亏合计"),
            _item("short_win_rate", "空头胜率", (by_dir.get("short") or {}).get("win_rate"), "pct", "campaigns", "空头回合胜率"),
        ]),
        ("收益", [
            _item("twr", "时间加权收益 TWR", perf.get("twr"), "pct", "daily", "按日链接收益，剔除出入金影响"),
            _item("cagr", "年化收益", perf.get("cagr"), "pct", "daily", "TWR 年化（窗口不足 30 天不计）"),
            _item("xirr", "资金加权年化 XIRR", perf.get("xirr"), "pct", "daily", "考虑出入金时点的内部收益率"),
            # Day-end exposure of an intraday trader is ~0; dividing by it would explode the ratio.
            _item("lev_adj_roi", "杠杆调整 ROI", _div(perf.get("twr"), lev) if lev and lev >= 0.1 else None, "pct", "daily",
                  "TWR ÷ 平均日末有效杠杆（%s）" % ("%.2fx" % lev if lev and lev >= 0.1
                                                  else "日末敞口过低，日内交易不适用" if lev is not None else "无数据")),
            _item("monthly_win_rate", "月度胜率", month.get("monthly_win_rate"), "pct", "daily",
                  "正收益月份占比（%s / %s 月）" % (month.get("positive_months", 0), month.get("months", 0))),
            _item("monthly_std", "月收益标准差", month.get("monthly_ret_std"), "pct", "daily", "月度 TWR 的标准差，越小越稳"),
            _item("best_month", "最佳月", (perf.get("best_month") or {}).get("pnl"), "usd", "daily",
                  (perf.get("best_month") or {}).get("period") or ""),
            _item("worst_month", "最差月", (perf.get("worst_month") or {}).get("pnl"), "usd", "daily",
                  (perf.get("worst_month") or {}).get("period") or ""),
        ]),
        ("风险与尾部", [
            _item("volatility", "年化波动率", risk.get("volatility"), "pct", "daily", "日收益标准差 × √365"),
            _item("sharpe", "Sharpe", risk.get("sharpe"), "num", "daily", "日均收益 ÷ 日波动 × √365，无风险利率取 0"),
            _item("sortino", "Sortino", risk.get("sortino"), "num", "daily", "只用下行波动的 Sharpe"),
            _item("calmar", "Calmar", risk.get("calmar"), "num", "daily", "年化收益 ÷ 最大回撤"),
            _item("omega", "Omega", risk.get("omega"), "num", "daily", "日收益中 Σ盈利 ÷ Σ|亏损|（阈值 0）"),
            _item("tail_ratio", "尾部比率", risk.get("tail_ratio"), "num", "daily",
                  "日收益 95 分位 ÷ |5 分位|；>1 说明右尾更厚（至少 20 天）"),
            _item("var95", "VaR 95%（日）", risk.get("var95"), "pct", "daily", "日收益 5 分位"),
            _item("cvar95", "CVaR 95%（日）", risk.get("cvar95"), "pct", "daily", "低于 VaR 的日收益均值"),
            _item("leverage_mean", "平均有效杠杆", lev, "x", "fills", "日末总敞口 ÷ 净值的均值"),
        ]),
        ("回撤", [
            _item("max_dd", "最大回撤", risk.get("max_drawdown"), "pct", "daily", "TWR 指数自峰值的最大跌幅"),
            _item("max_dd_usd", "最大回撤金额", risk.get("max_drawdown_usd"), "usd", "daily", "累计盈亏自峰值的最大回落"),
            _item("current_dd", "当前回撤", risk.get("current_drawdown"), "pct", "daily", "当前 TWR 指数距峰值"),
            _item("pnl_peak", "盈利峰值", risk.get("pnl_peak"), "usd", "daily", "窗口内累计盈亏的最高点"),
            _item("initial_loss", "初始亏损", risk.get("initial_loss"), "usd", "daily",
                  "首次转为盈利前累计盈亏的最低点；从未浮亏则为空"),
            _item("recovery_factor", "恢复因子", risk.get("recovery_factor"), "x", "daily", "净盈亏 ÷ 最大回撤金额"),
            _item("underwater_ratio", "回撤时间占比", dd.get("underwater_ratio"), "pct", "daily", "TWR 低于前高的天数占比"),
            _item("new_high_ratio", "创新高时间占比", dd.get("new_high_ratio"), "pct", "daily", "创出新高的天数占比"),
            _item("max_dd_duration", "最大回撤时长", dd.get("max_drawdown_duration_days"), "days", "daily", "单次回撤从峰值到谷底的最长天数"),
            _item("max_recovery", "最大恢复时长", dd.get("max_recovery_days"), "days", "daily", "已恢复回撤中，谷底回到前高的最长天数"),
            _item("longest_underwater", "最长水下时长", dd.get("longest_underwater_days"), "days", "daily", "峰值到收复（或至今）的最长天数"),
            _item("current_underwater", "当前水下时长", dd.get("current_underwater_days"), "days", "daily", "当前回撤已持续天数"),
            _item("dd_count", "回撤次数（≥5%）", dd.get("count"), "int", "daily", "深度至少 5% 的回撤次数"),
            _item("avg_dd_depth", "平均回撤深度", dd.get("avg_depth"), "pct", "daily", "≥5% 回撤的平均深度"),
            _item("dd_count_usd", "金额回撤次数", usd_dd.get("count"), "int", "daily",
                  "累计盈亏回落超过最大净值 5% 的次数"),
        ]),
    ]
    if excursion and excursion.get("evaluated"):
        groups.append(("MAE / MFE", [
            _item("mae_mean", "平均最大浮亏 MAE", excursion.get("mae_pct_mean"), "pct", "candles", "回合内最不利价格相对开仓均价"),
            _item("mfe_mean", "平均最大浮盈 MFE", excursion.get("mfe_pct_mean"), "pct", "candles", "回合内最有利价格相对开仓均价"),
            _item("mfe_capture", "MFE 捕获率", excursion.get("mfe_capture_median"), "pct", "candles",
                  "已实现盈亏 ÷ MFE 金额的中位数；低说明利润回吐"),
            _item("win_mae", "盈利回合平均 MAE", excursion.get("win_mae_pct_mean"), "pct", "candles", "赚钱的回合曾经扛过多深的浮亏"),
            _item("loss_mfe", "亏损回合平均 MFE", excursion.get("loss_mfe_pct_mean"), "pct", "candles", "亏钱的回合曾经有过多少浮盈"),
            _item("loss_mae", "平均止损深度", excursion.get("loss_mae_pct_mean"), "pct", "candles", "亏损回合的平均 MAE"),
            _item("mae_coverage", "MAE 覆盖回合", excursion.get("evaluated"), "int", "candles",
                  "可计算 MAE/MFE 的完整回合数（精度不足 %s 个；持仓短于所用 K 线的回合不计入上面的均值）" % excursion.get("coarse", 0)),
        ]))
    return [{"group": name, "items": items} for name, items in groups]
