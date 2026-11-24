"""One flat, labelled table of wallet metrics per time window.

Each item is ``{key, label, value, unit, source, definition}`` plus ``n`` (the
sample the value rests on) when it is meaningful; the values are taken from the
other metric modules, this file only selects, derives ratios and documents.
``source`` says which tier a number comes from:

- ``official``: Hyperliquid portfolio endpoint (the truth for PnL/volume).
- ``daily``: daily series built on the official equity curve.
- ``fills``: visible fills/funding (may be truncated; see quality page).
- ``campaigns``: fully observed campaigns only.
- ``candles``: MAE/MFE from cached candles (optional).
- ``benchmark``: daily regression on BTC closes (optional).

All ``pct`` values are fractions (0.25 = 25 %); the report multiplies by 100.
"""
from __future__ import annotations

UNITS = ("usd", "pct", "bps", "x", "int", "days", "hours", "num")


def _item(key, label, value, unit, source, definition, n=None):
    item = {"key": key, "label": label, "value": value, "unit": unit, "source": source, "definition": definition}
    if n is not None:
        item["n"] = n
    return item


def _num(value):
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def _div(a, b):
    return a / b if a is not None and b not in (None, 0) else None


def _pct(x):
    return "—" if x is None else "%.0f%%" % (x * 100)


def scorecard(*, perf, risk, behavior, activity, costs, official=None, equity=None,
              coins=None, excursion=None, market=None):
    """Grouped metric list for one window; inputs are that window's results."""
    perf, risk, behavior = perf or {}, risk or {}, behavior or {}
    activity, costs, official, market = activity or {}, costs or {}, official or {}, market or {}
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
    camps = behavior.get("campaigns_evaluated")
    strict_n = (behavior.get("wins") or 0) + (behavior.get("losses") or 0) if behavior else None
    ci = behavior.get("win_rate_strict_ci95")
    rdays = perf.get("return_days")
    r = behavior.get("r") or {}
    r_ok = r.get("status") == "ok"
    pf_status = {"no_losses": "无亏损回合，未定义", "no_sample": "无回合样本"}.get(behavior.get("profit_factor_status"), "")
    vis_vol = activity.get("volume")

    groups = [
        ("盈亏与成本", [
            _item("total_pnl", "总盈亏（含手续费、资金费）", total_pnl, "usd", pnl_source,
                  "官方 portfolio 窗口盈亏，已扣手续费、含资金费和未实现盈亏" if pnl_source == "official"
                  else "官方日序列重建的窗口盈亏（无官方窗口值时）"),
            _item("trading_net", "可见成交已实现净盈亏（不含资金费）", realized - (fees or 0) if coins else None, "usd", "fills",
                  "Σ closedPnl − 手续费；只含可见成交，不含资金费和未实现盈亏，与总盈亏口径不同"),
            _item("fees", "手续费（USD）", fees, "usd", "fills",
                  "可见成交手续费逐笔折成美元：稳定币按面值，其他币种按现价估算 %s 笔，无法折算 %s 笔未计入"
                  % (costs.get("fee_approx_count", 0), costs.get("fee_missing_count", 0))),
            _item("fee_bps", "手续费率", costs.get("fee_bps"), "bps", "fills", "手续费 ÷ 可见成交额 × 1万"),
            _item("fee_drag", "手续费 ÷ 盈利回合毛利", costs.get("fee_drag"), "pct", "campaigns",
                  "手续费 ÷ 盈利回合扣费前 closedPnl 之和（%s）" % ("%.0f USD" % costs["gross_trade_profit"]
                                                         if costs.get("gross_trade_profit") else "无盈利回合")),
            _item("funding", "资金费净额", funding, "usd", "fills", "资金费收入 − 支出，正数为净收入"),
            _item("funding_received", "资金费收入", costs.get("funding_received"), "usd", "fills",
                  "逐笔为正的资金费之和，先分正负再汇总，不按币种净额抵消", costs.get("funding_events")),
            _item("funding_paid", "资金费支出", costs.get("funding_paid"), "usd", "fills",
                  "逐笔为负的资金费绝对值之和", costs.get("funding_events")),
            _item("funding_share", "资金费占盈亏比", _div(funding, abs(total_pnl) if total_pnl else None), "pct", "fills",
                  "资金费净额 ÷ |总盈亏|；高说明靠资金费套利"),
            _item("maker_share", "Maker 占比（金额）", activity.get("maker_share"), "pct", "fills",
                  "Maker 成交额 ÷ 已知 Maker/Taker 的成交额；未知占全部成交额 %s" % _pct(activity.get("maker_unknown_share"))),
            _item("maker_share_count", "Maker 占比（笔数）", activity.get("maker_share_count"), "pct", "fills",
                  "Maker 成交笔数 ÷ 已知 Maker/Taker 的成交笔数（频率视角）", activity.get("fills")),
            _item("volume", "累计成交量", volume, "usd", vol_source, "窗口内成交额（官方 vlm 优先）"),
            _item("volume_visible", "可见成交额", vis_vol, "usd", "fills",
                  "可见 fills 的成交额；占官方 %s" % _pct(_div(vis_vol, official_vlm))),
            _item("turnover_equity", "成交量 ÷ 当前净值", _div(volume, equity), "x", vol_source, "换手倍数，按当前官方净值"),
            _item("turnover_mean", "成交量 ÷ 平均净值", _div(volume, mean_equity), "x", vol_source, "换手倍数，按窗口平均净值"),
            _item("pnl_per_volume", "单位成交额收益", _div(total_pnl * 1e4 if total_pnl is not None else None, volume),
                  "bps", pnl_source, "总盈亏 ÷ 成交量 × 1万"),
            _item("fills", "总成交笔数", activity.get("fills"), "int", "fills",
                  "按唯一成交 ID 计；反手拆出的操作腿另有 %s 条" % activity.get("legs", 0)),
            _item("orders", "订单数", activity.get("orders"), "int", "fills", "按唯一订单 ID 计，排除 TWAP 切片"),
            _item("orders_per_day", "每活跃日订单数", activity.get("orders_per_active_day"), "num", "fills",
                  "订单数 ÷ 有成交的 UTC 日数（%s 天）" % activity.get("active_days", 0)),
            _item("avg_fill", "平均单笔成交额", _div(activity.get("volume"), activity.get("fills")), "usd", "fills",
                  "可见成交额 ÷ 唯一成交笔数"),
            _item("liq_events", "强平事件数", activity.get("liquidation_events"), "int", "fills",
                  "本账户被强平的成交按（币种, 秒）去重；另有强平成交 %s 笔、被强平回合 %s 个、作为他人强平对手方 %s 笔"
                  % (activity.get("liquidation_fills", 0), behavior.get("liquidated_campaigns", 0),
                     activity.get("liquidation_counterparty_fills", 0))),
        ]),
        ("回合", [
            _item("campaigns", "总回合数", camps, "int", "campaigns",
                  "开仓到平仓完整可见的回合；另有 %s 个不完整回合未计入；盈 %s / 亏 %s / 平 %s"
                  % (behavior.get("campaigns_excluded", 0), behavior.get("wins", 0), behavior.get("losses", 0),
                     behavior.get("flats", 0))),
            _item("independent_trades", "有效独立回合数", behavior.get("independent_trades"), "num", "campaigns",
                  "回合数 ÷ 平均同时持有回合数（%s）；近似值，相关币种仍可能不独立"
                  % ("%.2f" % behavior["mean_concurrent"] if behavior.get("mean_concurrent") else "—"), camps),
            _item("wins", "盈利回合数", behavior.get("wins"), "int", "campaigns", "净盈亏（含资金费）> 0 的回合"),
            _item("win_rate", "胜率（含资金费，含持平）", behavior.get("win_rate"), "pct", "campaigns",
                  "盈利回合 ÷ 全部回合；持平计入分母", camps),
            _item("win_rate_strict", "严格胜率（剔除持平）", behavior.get("win_rate_strict"), "pct", "campaigns",
                  "盈利 ÷ (盈利 + 亏损)；95%% Wilson 区间 %s–%s，假设回合相互独立"
                  % ((_pct(ci[0]), _pct(ci[1])) if ci else ("—", "—")), strict_n),
            _item("break_even_win_rate", "盈亏平衡胜率", behavior.get("break_even_win_rate"), "pct", "campaigns",
                  "|平均亏损| ÷ (平均盈利 + |平均亏损|)；严格胜率高于它才有正期望", strict_n),
            _item("win_rate_trade_only", "交易胜率（扣手续费，不含资金费）", behavior.get("win_rate_trade_only"), "pct",
                  "campaigns", "net_pnl > 0 的回合占比", camps),
            _item("win_rate_before_fees", "手续费前胜率", behavior.get("win_rate_before_fees"), "pct", "campaigns",
                  "closedPnl > 0 的回合占比；与上一项的差距是手续费吃掉的胜率", camps),
            _item("mean_hold", "平均持仓时长", behavior.get("mean_hold_hours"), "hours", "campaigns", "开仓到平仓时长的均值"),
            _item("median_hold", "持仓时长中位数", behavior.get("median_hold_hours"), "hours", "campaigns", "开仓到平仓时长的中位数"),
            _item("win_hold", "盈利回合持仓中位数", behavior.get("win_median_hold_hours"), "hours", "campaigns",
                  "盈利回合的持仓时长中位数", behavior.get("win_hold_n")),
            _item("loss_hold", "亏损回合持仓中位数", behavior.get("loss_median_hold_hours"), "hours", "campaigns",
                  "亏损回合的持仓时长中位数；明显长于盈利回合说明亏损单拿得更久", behavior.get("loss_hold_n")),
            _item("avg_win", "平均盈利", behavior.get("avg_win"), "usd", "campaigns", "盈利回合的平均净盈亏"),
            _item("avg_loss", "平均亏损", behavior.get("avg_loss"), "usd", "campaigns", "亏损回合的平均净盈亏"),
            _item("max_win", "最大单回合盈利", behavior.get("max_win"), "usd", "campaigns", "只在盈利回合中取最大；无盈利回合为空"),
            _item("max_loss", "最大单回合亏损", behavior.get("max_loss"), "usd", "campaigns", "只在亏损回合中取最小；无亏损回合为空"),
            _item("payoff", "盈亏比", behavior.get("payoff_ratio"), "x", "campaigns", "平均盈利 ÷ |平均亏损|"),
            _item("profit_factor", "盈利因子（回合）", behavior.get("profit_factor"), "x", "campaigns",
                  "盈利回合合计 ÷ |亏损回合合计|" + ("；" + pf_status if pf_status else ""), camps),
            _item("expectancy", "每回合期望值", behavior.get("expectancy"), "usd", "campaigns", "全部回合净盈亏的平均"),
            _item("expectancy_r", "历史亏损归一化期望（R）", r.get("expectancy_r") if r_ok else None, "num", "campaigns",
                  ("1R = 亏损回合收益率（净盈亏 ÷ 入场名义额）中位数的绝对值 %.2f%%；95%% 区间 %.2f~%.2f R；"
                   "不是事先设定的止损 R" % (r["one_r"] * 100, r["avg_r_ci95"][0], r["avg_r_ci95"][1])) if r_ok
                  else "需至少 %s 个回合且 %s 个亏损回合" % (r.get("min_trades", 20), r.get("min_losses", 10)), r.get("n")),
            _item("worst_r", "最差回合（R）", r.get("worst_r") if r_ok else None, "num", "campaigns",
                  "最差一次亏损是典型亏损的多少倍", r.get("n")),
            _item("beyond_3r", "超过 −3R 的回合", r.get("beyond_3r") if r_ok else None, "int", "campaigns",
                  "亏损超过 3 倍典型亏损的次数（占 %s）；为 0 不代表没有尾部风险" % _pct(r.get("beyond_3r_share")), r.get("n")),
            _item("efficiency", "盈亏效率比", behavior.get("efficiency_ratio"), "pct", "campaigns",
                  "净盈亏 ÷ (总盈利 + |总亏损|)；1 为从不亏损，0 为来回打平"),
            _item("max_win_streak", "最大连胜", behavior.get("max_win_streak"), "int", "campaigns", "按平仓时间排序的连续盈利回合数"),
            _item("max_loss_streak", "最大连败", behavior.get("max_loss_streak"), "int", "campaigns", "按平仓时间排序的连续亏损回合数"),
            _item("current_streak", "当前连胜/连败", behavior.get("current_streak"), "int", "campaigns",
                  "正数为当前连胜、负数为连败；持平回合终止连续段"),
            _item("top10_share", "盈利集中度（前 10%）", conc.get("top_share"), "pct", "campaigns",
                  "前 10%% 回合（%s 个）占盈利回合净收益合计；分母已扣手续费、含资金费，不是手续费前毛利"
                  % conc.get("top_count", 0)),
            _item("largest_share", "最大单笔占盈利回合合计", conc.get("largest_share"), "pct", "campaigns",
                  "单个最赚回合 ÷ 盈利回合净收益合计"),
            _item("largest_of_net", "最大单笔占净利润", conc.get("largest_of_net"), "pct", "campaigns",
                  "单个最赚回合 ÷ 全部回合净盈亏；超过 100% 说明其余回合合计为负；净利润 ≤ 0 时不显示"),
            _item("top5_share", "前五笔占盈利回合合计", conc.get("top5_share"), "pct", "campaigns",
                  "最赚 5 个回合 ÷ 盈利回合净收益合计"),
            _item("without_best", "去掉最佳一笔后净盈亏", conc.get("profit_without_best"), "usd", "campaigns",
                  "敏感性分析：全部回合净盈亏 − 最赚一笔"),
            _item("without_top5", "去掉最佳五笔后净盈亏", conc.get("profit_without_top5"), "usd", "campaigns",
                  "敏感性分析：全部回合净盈亏 − 最赚五笔"),
            _item("dust", "尘埃回合", behavior.get("dust_campaigns"), "int", "campaigns",
                  "入场与出场名义额都低于 $%s 的回合，合计 %s USD；仍计入金额，提示其对胜率与样本数的影响"
                  % (behavior.get("dust_threshold", 0.05), "%.2f" % (behavior.get("dust_pnl") or 0))),
            _item("long_pnl", "多头盈亏", (by_dir.get("long") or {}).get("pnl"), "usd", "campaigns", "多头回合净盈亏合计"),
            _item("long_win_rate", "多头胜率", (by_dir.get("long") or {}).get("win_rate"), "pct", "campaigns", "多头回合胜率",
                  (by_dir.get("long") or {}).get("count")),
            _item("short_pnl", "空头盈亏", (by_dir.get("short") or {}).get("pnl"), "usd", "campaigns", "空头回合净盈亏合计"),
            _item("short_win_rate", "空头胜率", (by_dir.get("short") or {}).get("win_rate"), "pct", "campaigns", "空头回合胜率",
                  (by_dir.get("short") or {}).get("count")),
        ]),
        ("收益", [
            _item("twr", "时间加权收益 TWR", perf.get("twr"), "pct", "daily",
                  "按日 Modified Dietz（出入金视为日中发生）串联的近似 TWR；有效收益日 %s / %s 天，无有效日时为空"
                  % (rdays or 0, perf.get("days") or 0), rdays),
            _item("cagr", "年化收益", perf.get("cagr"), "pct", "daily",
                  "TWR 年化；不足 30 天不计，30–179 天为估算" + ("（当前为估算）" if perf.get("cagr_estimated") else "")),
            _item("xirr", "资金加权年化 XIRR", perf.get("xirr"), "pct", "daily",
                  "现金流内部收益率；现金流时点：%s；求解状态：%s"
                  % ({"ledger": "真实资金流水", "implied_daily": "按日隐含净流量（流水不完整）"}.get(perf.get("xirr_basis"), "—"),
                     perf.get("xirr_status") or "—")),
            _item("ropc", "峰值资本回报", perf.get("return_on_peak_capital"), "pct", "daily",
                  "窗口净盈亏 ÷ 窗口内最高账户权益（%s）；不替代 TWR，也不是保证金回报"
                  % ("%.0f USD" % perf["peak_equity"] if perf.get("peak_equity") else "—")),
            # Day-end exposure of an intraday trader is ~0; dividing by it would explode the ratio.
            _item("lev_adj_roi", "按日末杠杆粗调收益", _div(perf.get("twr"), lev) if lev and lev >= 0.1 else None, "pct", "daily",
                  "TWR ÷ 平均日末有效杠杆（%s）；未计日内敞口，不是严格去杠杆收益" % ("%.2fx" % lev if lev and lev >= 0.1
                                                  else "日末敞口过低，日内交易不适用" if lev is not None else "无数据")),
            _item("monthly_win_rate", "月度胜率（日历月）", month.get("monthly_win_rate"), "pct", "daily",
                  "盈利月份 ÷ 序列内全部月份（%s / %s 月）" % (month.get("positive_months", 0), month.get("months", 0)),
                  month.get("months")),
            _item("active_monthly_win_rate", "月度胜率（活跃月）", month.get("active_monthly_win_rate"), "pct", "daily",
                  "只计 |月盈亏| ≥ $0.5 的月份（%s 月）" % month.get("active_months", 0), month.get("active_months")),
            _item("weekly_win_rate", "盈利周占比", perf.get("active_weekly_win_rate"), "pct", "daily",
                  "盈利周 ÷ |周盈亏| ≥ $0.5 的活跃周（%s 周；日历周口径 %s）"
                  % (perf.get("active_weeks", 0), _pct(perf.get("weekly_win_rate"))), perf.get("active_weeks")),
            _item("monthly_std", "月收益标准差", month.get("monthly_ret_std"), "pct", "daily",
                  "月度 TWR 的标准差，越小越稳；无有效收益日的月份不计"),
            _item("profit_factor_daily", "盈利因子（日度）", perf.get("profit_factor_daily"), "x", "daily",
                  "盈利日合计 ÷ |亏损日合计|；与回合盈利因子回答不同问题", perf.get("days")),
            _item("best_month", "最佳月", (perf.get("best_month") or {}).get("pnl"), "usd", "daily",
                  (perf.get("best_month") or {}).get("period") or ""),
            _item("worst_month", "最差月", (perf.get("worst_month") or {}).get("pnl"), "usd", "daily",
                  (perf.get("worst_month") or {}).get("period") or ""),
        ]),
        ("风险与尾部", [
            _item("volatility", "年化波动率", risk.get("volatility"), "pct", "daily",
                  "日收益标准差 × √365；日波动率 %s" % ("%.2f%%" % (risk["volatility_daily"] * 100)
                                                if risk.get("volatility_daily") else "—"), rdays),
            _item("sharpe", "Sharpe", risk.get("sharpe"), "num", "daily", "日均收益 ÷ 日波动 × √365，无风险利率取 0", rdays),
            _item("sortino", "Sortino", risk.get("sortino"), "num", "daily",
                  "日均收益 ÷ 下行偏差 × √365；下行偏差对全部有效日取 √mean(min(r,0)²)", rdays),
            _item("calmar", "Calmar", risk.get("calmar"), "num", "daily", "年化收益 ÷ 最大回撤"),
            _item("omega", "Omega", risk.get("omega"), "num", "daily", "日收益中 Σ盈利 ÷ Σ|亏损|（阈值 0）", rdays),
            _item("tail_ratio", "尾部比率", risk.get("tail_ratio"), "num", "daily",
                  "日收益 95 分位 ÷ |5 分位|；>1 说明右尾更厚（至少 20 天）", rdays),
            _item("var95", "日收益 5% 分位", risk.get("var95"), "pct", "daily",
                  "有效日收益的第 5 百分位（通常为负）；少于 %s 天不计" % risk.get("tail_min_days", 30), rdays),
            _item("var95_loss", "95% VaR（日，损失为正）", risk.get("var95_loss"), "pct", "daily",
                  "−(日收益 5% 分位)；分位为正时记 0", rdays),
            _item("cvar95", "CVaR 95%（日）", risk.get("cvar95"), "pct", "daily", "不高于 5% 分位的日收益均值", rdays),
            _item("leverage_mean", "平均有效杠杆", lev, "x", "fills", "日末总敞口 ÷ 净值的均值（只采样日末，看不到日内）",
                  risk.get("leverage_samples")),
            _item("beta_btc", "BTC Beta", market.get("beta"), "num", "benchmark",
                  ("日收益对 BTC 日涨跌的回归斜率；95%% 区间 %.2f~%.2f；R² %.2f；Newey-West 标准误"
                   % (market["beta_ci95"][0], market["beta_ci95"][1], market.get("r2") or 0))
                  if market.get("status") == "ok" else "需至少 %s 个可配对的有效收益日（当前 %s）"
                  % (market.get("min_days", 30), market.get("n", 0)), market.get("n")),
            _item("alpha_btc", "扣除 BTC 影响后的日超额（年化）", market.get("alpha_annual"), "pct", "benchmark",
                  ("回归截距 × 365（算术，非复利，含杠杆）；t = %.2f；去掉最好 %s 天后 t = %s"
                   % (market.get("t_alpha") or 0, (market.get("trimmed") or {}).get("dropped_days", "—"),
                      "%.2f" % market["trimmed"]["t_alpha"] if (market.get("trimmed") or {}).get("t_alpha") is not None else "—"))
                  if market.get("status") == "ok" else "样本不足或无 BTC 日线", market.get("n")),
        ]),
        ("回撤", [
            _item("max_dd", "最大回撤", risk.get("max_drawdown"), "pct", "daily",
                  "TWR 指数自峰值的最大跌幅；以窗口起点 1.0 为初始峰值，日末采样，出入金已剔除", rdays),
            _item("max_dd_usd", "最大回撤金额", risk.get("max_drawdown_usd"), "usd", "daily",
                  "窗口内累计盈亏（起点归零）自峰值的最大回落"),
            _item("current_dd", "当前回撤", risk.get("current_drawdown"), "pct", "daily", "当前 TWR 指数距峰值"),
            _item("pnl_peak", "盈利峰值", risk.get("pnl_peak"), "usd", "daily", "窗口内累计盈亏（起点归零）的最高点"),
            _item("initial_loss", "初始亏损", risk.get("initial_loss"), "usd", "daily",
                  "窗口内首次转为盈利前累计盈亏的最低点；从未浮亏则为空"),
            _item("recovery_factor", "恢复因子", risk.get("recovery_factor"), "x", "daily", "净盈亏 ÷ 最大回撤金额"),
            _item("underwater_ratio", "回撤时间占比", dd.get("underwater_ratio"), "pct", "daily", "TWR 低于前高的天数占比"),
            _item("new_high_ratio", "创新高时间占比", dd.get("new_high_ratio"), "pct", "daily", "创出新高的天数占比"),
            _item("max_dd_duration", "最大回撤时长", dd.get("max_drawdown_duration_days"), "days", "daily", "单次回撤从峰值到谷底的最长天数"),
            _item("max_recovery", "最大恢复时长", dd.get("max_recovery_days"), "days", "daily", "已恢复回撤中，谷底回到前高的最长天数"),
            _item("longest_underwater", "最长水下时长", dd.get("longest_underwater_days"), "days", "daily",
                  "全部回撤（不限最深 10 次）中峰值到收复（或至今）的最长天数"),
            _item("current_underwater", "当前水下时长", dd.get("current_underwater_days"), "days", "daily", "当前回撤已持续天数"),
            _item("dd_count", "回撤次数（≥5%）", dd.get("count"), "int", "daily", "深度至少 5% 的回撤次数"),
            _item("avg_dd_depth", "平均回撤深度", dd.get("avg_depth"), "pct", "daily", "≥5% 回撤的平均深度"),
            _item("dd_count_usd", "金额回撤次数", usd_dd.get("count"), "int", "daily",
                  "累计盈亏回落超过最大净值 5% 的次数"),
        ]),
    ]
    if excursion and excursion.get("evaluated"):
        groups.append(("MAE / MFE", [
            _item("mae_mean", "平均最大浮亏 MAE", excursion.get("mae_pct_mean"), "pct", "candles",
                  "回合路径（含此前部分平仓已实现价差）最大浮亏 ÷ 峰值入场名义额；不扣费、不含资金费"),
            _item("mfe_mean", "平均最大浮盈 MFE", excursion.get("mfe_pct_mean"), "pct", "candles",
                  "回合路径最大浮盈 ÷ 峰值入场名义额；不扣费、不含资金费"),
            _item("mfe_capture", "MFE 捕获率", excursion.get("mfe_capture_median"), "pct", "candles",
                  "价格已实现盈亏 ÷ MFE 金额的中位数（都不含费用）；低说明利润回吐"),
            _item("win_mae", "盈利回合平均 MAE", excursion.get("win_mae_pct_mean"), "pct", "candles",
                  "按含资金费净盈亏分组的盈利回合，曾扛过多深的价格浮亏"),
            _item("loss_mfe", "亏损回合平均 MFE", excursion.get("loss_mfe_pct_mean"), "pct", "candles",
                  "按含资金费净盈亏分组的亏损回合，曾有过多少价格浮盈"),
            _item("loss_mae", "平均止损深度", excursion.get("loss_mae_pct_mean"), "pct", "candles", "亏损回合的平均 MAE"),
            _item("mae_coverage", "MAE 覆盖回合", excursion.get("evaluated"), "int", "candles",
                  "可计算 MAE/MFE 的完整回合数（精度不足 %s 个，其中 K 线有缺口 %s 个；持仓短于所用 K 线的回合不计入上面的均值）"
                  % (excursion.get("coarse", 0), excursion.get("gapped", 0))),
        ]))
    return [{"group": name, "items": items} for name, items in groups]
