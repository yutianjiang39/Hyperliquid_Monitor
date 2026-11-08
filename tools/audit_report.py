"""Independent correctness audit of a v3.1 report.

Recomputes metrics from the report's exported CSVs (and the live public API)
WITHOUT importing project code, then compares with model.json values.
Usage: python tools/audit_report.py <report_dir> [--live]
"""
import csv
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
DAY = 86_400_000
d = Path(sys.argv[1])
ex = d / "exports"
M = json.loads((ex / "model.json").read_text(encoding="utf-8"))
results = []


def rows(name):
    with open(ex / name, encoding="utf-8-sig", newline="") as f:
        out = list(csv.DictReader(f))
    # The export prefixes formula-like cells ("@182") with ' against CSV injection.
    for r in out:
        if r.get("coin", "").startswith("'"):
            r["coin"] = r["coin"][1:]
    return out


def f(v):
    try:
        return None if v in (None, "", "None") else float(v)
    except ValueError:
        return None


def check(group, name, model, mine, tol=1e-6, rel=True):
    if model is None and mine is None:
        ok, diff = True, 0.0
    elif model is None or mine is None:
        ok, diff = False, None
    else:
        diff = model - mine
        scale = max(abs(mine), 1.0) if rel else 1.0
        ok = abs(diff) <= tol * scale
    results.append((group, name, model, mine, diff, ok))


def card(key, window="all"):
    for g in M["scorecard"][window]:
        for i in g["items"]:
            if i["key"] == key:
                return i["value"]
    raise KeyError(key)


def quantile(xs, q):
    s = sorted(xs)
    pos = (len(s) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


# ------------------------------------------------------------- A. live official
if "--live" in sys.argv:
    import requests
    wallet = M["wallet"]
    post = lambda body: requests.post("https://api.hyperliquid.xyz/info", json=body, timeout=60).json()
    port = dict(post({"type": "portfolio", "user": wallet}))
    live_all = port["allTime"]
    live_pnl = float(live_all["pnlHistory"][-1][1])
    live_vlm = float(live_all["vlm"])
    model_pnl = f(M["official"]["windows"]["allTime"]["pnl"])
    # Live drifts after the report was built; 1% band.
    check("A 官方真值", "allTime 盈亏 vs 实时官方 API", model_pnl, live_pnl, tol=0.01)
    check("A 官方真值", "allTime 成交量 vs 实时官方 API", f(M["official"]["windows"]["allTime"]["vlm"]), live_vlm, tol=0.01)
    check("A 官方真值", "当前净值 vs 实时官方 API", f(M["current"]["equity"]), float(live_all["accountValueHistory"][-1][1]), tol=0.02)
check("A 官方真值", "指标总表 总盈亏 = 官方 allTime 盈亏", card("total_pnl"), f(M["official"]["windows"]["allTime"]["pnl"]))
check("A 官方真值", "指标总表 成交量 = 官方 allTime vlm", card("volume"), f(M["official"]["windows"]["allTime"]["vlm"]))

# ------------------------------------------------------------- B. fills
ops = rows("operations.csv")
ids = [o["id"] for o in ops]
check("B 成交", "成交去重（反手成交拆成开/平两腿，按 id 判重）", len(ids), len(set(ids)), tol=0, rel=False)
bad_notional = sum(1 for o in ops if f(o["price"]) is not None and f(o["size"]) is not None and f(o["notional"]) is not None
                   and abs(f(o["price"]) * f(o["size"]) - f(o["notional"])) > 1e-6 * max(1, f(o["notional"])))
check("B 成交", "名义金额 = 价格 × 数量（不符条数）", bad_notional, 0, tol=0, rel=False)
bad_chain = sum(1 for o in ops if f(o["start_position"]) is not None and f(o["end_position"]) is not None
                and abs(f(o["start_position"]) + (1 if o["side"] == "B" else -1) * f(o["size"]) - f(o["end_position"])) > 1e-6 * max(1, abs(f(o["end_position"]))))
check("B 成交", "成交后仓位 = 成交前 ± 数量（不符条数）", bad_chain, 0, tol=0, rel=False)
vol = sum(f(o["notional"]) or 0 for o in ops)
fees = sum(f(o["fee"]) or 0 for o in ops if o["fee_token"] in ("", "USDC"))
mk = sum(f(o["notional"]) or 0 for o in ops if o["maker"] == "True")
tk = sum(f(o["notional"]) or 0 for o in ops if o["maker"] == "False")
check("B 成交", "成交笔数", card("fills"), len(ops), tol=0, rel=False)
check("B 成交", "手续费合计", card("fees"), fees)
check("B 成交", "手续费率 bps", card("fee_bps"), fees / vol * 1e4)
check("B 成交", "Maker 占比", card("maker_share"), mk / (mk + tk))
check("B 成交", "平均单笔成交额", card("avg_fill"), vol / len(ops))
fund = rows("funding.csv")
fund_total = sum(f(r["usdc"]) or 0 for r in fund)
check("B 成交", "资金费净额", card("funding"), fund_total)
realized = sum(f(o["closed_pnl"]) or 0 for o in ops)
check("B 成交", "交易净盈亏 = Σ已实现 − 手续费", card("trading_net"), realized - fees)

# by-coin table sums back to the totals
bc = M["windows"]["all"]["by_coin"]
coin_real, coin_fee = defaultdict(float), defaultdict(float)
for o in ops:
    coin_real[o["coin"]] += f(o["closed_pnl"]) or 0
    if o["fee_token"] in ("", "USDC"):
        coin_fee[o["coin"]] += f(o["fee"]) or 0
coin_fund = defaultdict(float)
for r in fund:
    coin_fund[r["coin"]] += f(r["usdc"]) or 0
worst = max((abs(r["net"] - (coin_real[r["coin"]] - coin_fee[r["coin"]] + coin_fund[r["coin"]])) for r in bc), default=0)
check("B 成交", "分币种净额 = 已实现 − 手续费 + 资金费（最大偏差 $）", worst, 0.0, tol=0.01, rel=False)
if len(bc) < 200:
    check("B 成交", "分币种净额合计", sum(r["net"] for r in bc), realized - fees + fund_total)

# ------------------------------------------------------------- C. campaigns
camps = rows("campaigns.csv")
ops_by = defaultdict(list)
for o in ops:
    ops_by[o["campaign_id"]].append(o)
bad_net = 0
for c in camps:
    if c["eligible_for_win_rate"] != "True":
        continue
    fee_tok = json.loads(c["fees_by_token"] or "{}")
    mine = sum(f(o["closed_pnl"]) or 0 for o in ops_by[c["id"]]) - sum(f(v) for v in fee_tok.values())
    if abs(mine - f(c["net_pnl"])) > 1e-6 * max(1, abs(mine)):
        bad_net += 1
check("C 回合", "回合净盈亏 = Σ该回合成交(已实现−手续费)（不符个数）", bad_net, 0, tol=0, rel=False)
el = sorted((c for c in camps if c["eligible_for_win_rate"] == "True"), key=lambda c: int(c["close_time"]))
pn = [f(c["net_pnl_with_funding"]) if f(c["net_pnl_with_funding"]) is not None else f(c["net_pnl"]) for c in el]
wins, losses = [x for x in pn if x > 0], [x for x in pn if x < 0]
gw, gl = sum(wins), -sum(losses)
check("C 回合", "完整回合数", card("campaigns"), len(el), tol=0, rel=False)
check("C 回合", "胜率", card("win_rate"), len(wins) / len(pn))
check("C 回合", "平均盈利", card("avg_win"), gw / len(wins))
check("C 回合", "平均亏损", card("avg_loss"), -gl / len(losses))
check("C 回合", "盈亏比", card("payoff"), (gw / len(wins)) / (gl / len(losses)))
check("C 回合", "盈利因子", card("profit_factor"), gw / gl)
check("C 回合", "期望值", card("expectancy"), sum(pn) / len(pn))
check("C 回合", "盈亏效率比", card("efficiency"), sum(pn) / (gw + gl))
best = worst_ = run = 0
for x in pn:
    run = (run + 1 if run > 0 else 1) if x > 0 else (run - 1 if run < 0 else -1) if x < 0 else 0
    best, worst_ = max(best, run), min(worst_, run)
check("C 回合", "最大连胜", card("max_win_streak"), best, tol=0, rel=False)
check("C 回合", "最大连败", card("max_loss_streak"), -worst_, tol=0, rel=False)
dur = sorted(int(c["close_time"]) - int(c["open_time"]) for c in el)
check("C 回合", "平均持仓（小时）", card("mean_hold"), sum(dur) / len(dur) / 3.6e6)
med = dur[len(dur) // 2] if len(dur) % 2 else (dur[len(dur) // 2 - 1] + dur[len(dur) // 2]) / 2
check("C 回合", "持仓中位数（小时）", card("median_hold"), med / 3.6e6)
k = max(1, round(len(pn) * 0.1))
top = sorted(wins, reverse=True)
check("C 回合", "盈利集中度 前10%", card("top10_share"), sum(top[:k]) / gw)
check("C 回合", "最大单笔占总盈利", card("largest_share"), top[0] / gw)
for side, key in (("long", "long_pnl"), ("short", "short_pnl")):
    check("C 回合", f"{side} 盈亏", card(key), sum(x for c, x in zip(el, pn) if c["direction"] == side))

# ------------------------------------------------------------- D. daily series
days = rows("series_days.csv")
eq = [f(r["equity"]) for r in days]
pnl = [f(r["pnl"]) for r in days]
flow = [f(r["flow"]) for r in days]
bad_id = sum(1 for i in range(1, len(days)) if abs(eq[i] - eq[i - 1] - pnl[i] - flow[i]) > 1e-6 * max(1, abs(eq[i])))
check("D 日序列", "净值变化 = 盈亏 + 资金流（不符天数）", bad_id, 0, tol=0, rel=False)
cum, bad_cum = 0.0, 0
for r, p in zip(days, pnl):
    cum += p
    bad_cum += abs(cum - f(r["cum_pnl"])) > 1e-6 * max(1, abs(cum))
check("D 日序列", "累计盈亏 = 日盈亏累加（不符天数）", bad_cum, 0, tol=0, rel=False)
check("D 日序列", "Σ日盈亏 = 官方 allTime 盈亏", sum(pnl), f(M["official"]["windows"]["allTime"]["pnl"]), tol=0.005)
rets = [f(r["ret"]) for r in days if f(r["ret"]) is not None]
lvl, idx = 1.0, []
for r in days:
    if f(r["ret"]) is not None:
        lvl *= 1 + f(r["ret"])
    idx.append((int(r["time"]), lvl))
check("D 日序列", "TWR", card("twr"), lvl - 1)
peak, mdd, under, highs, pk = None, 0.0, 0, 0, None
episodes, cur = [], None
for t, v in idx:
    if peak is None or v >= peak:
        if peak is not None and v > peak:
            highs += 1
        if cur:
            cur["rec"] = t
            episodes.append(cur)
            cur = None
        peak, pk = v, t
        continue
    dd = v / peak - 1
    under += 1
    mdd = min(mdd, dd)
    if cur is None or dd < cur["depth"]:
        cur = {"peak": pk, "trough": t, "depth": dd, "rec": None} if cur is None else dict(cur, trough=t, depth=dd)
if cur:
    episodes.append(cur)
check("D 日序列", "最大回撤", card("max_dd"), -mdd)
check("D 日序列", "当前回撤", card("current_dd"), -(idx[-1][1] / max(v for _, v in idx) - 1))
check("D 日序列", "回撤次数 ≥5%", card("dd_count"), sum(1 for e in episodes if e["depth"] <= -0.05), tol=0, rel=False)
sig = [e for e in episodes if e["depth"] <= -0.05]
check("D 日序列", "平均回撤深度", card("avg_dd_depth"), -sum(e["depth"] for e in sig) / len(sig) if sig else None)
check("D 日序列", "最大回撤时长（峰→谷，天）", card("max_dd_duration"), max(((e["trough"] - e["peak"]) / DAY for e in episodes), default=0))
rec = [(e["rec"] - e["trough"]) / DAY for e in episodes if e["rec"]]
check("D 日序列", "最大恢复时长（天）", card("max_recovery"), max(rec) if rec else None)
check("D 日序列", "水下时间占比", card("underwater_ratio"), under / len(idx))
check("D 日序列", "创新高时间占比", card("new_high_ratio"), highs / (len(idx) - 1))
n = len(rets)
mean = sum(rets) / n
sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (n - 1))
dd_dev = math.sqrt(sum(min(r, 0) ** 2 for r in rets) / n)
check("D 日序列", "年化波动率", card("volatility"), sd * math.sqrt(365))
check("D 日序列", "Sharpe", card("sharpe"), mean / sd * math.sqrt(365))
check("D 日序列", "Sortino", card("sortino"), mean / dd_dev * math.sqrt(365))
check("D 日序列", "Omega", card("omega"), sum(r for r in rets if r > 0) / -sum(r for r in rets if r < 0))
check("D 日序列", "尾部比率", card("tail_ratio"), quantile(rets, 0.95) / -quantile(rets, 0.05))
v95 = quantile(rets, 0.05)
check("D 日序列", "VaR 95", card("var95"), v95)
tail = [r for r in rets if r <= v95]
check("D 日序列", "CVaR 95", card("cvar95"), sum(tail) / len(tail))
years = (int(days[-1]["time"]) - int(days[0]["time"]) + DAY) / DAY / 365
cagr = (lvl) ** (1 / years) - 1
check("D 日序列", "年化收益 CAGR", card("cagr"), cagr)
check("D 日序列", "Calmar", card("calmar"), cagr / -mdd)
cp = [f(r["cum_pnl"]) for r in days]
check("D 日序列", "盈利峰值", card("pnl_peak"), max(cp))
usd_peak, usd_mdd = None, 0.0
for v in cp:
    usd_peak = v if usd_peak is None or v > usd_peak else usd_peak
    usd_mdd = min(usd_mdd, v - usd_peak)
check("D 日序列", "最大回撤金额", card("max_dd_usd"), -usd_mdd)
check("D 日序列", "恢复因子", card("recovery_factor"), sum(pnl) / -usd_mdd)
mon = defaultdict(lambda: [0.0, 1.0])
for r in days:
    key = datetime.fromtimestamp((int(r["time"]) - 1) / 1000, timezone.utc).strftime("%Y-%m")
    mon[key][0] += f(r["pnl"])
    if f(r["ret"]) is not None:
        mon[key][1] *= 1 + f(r["ret"])
mrows = {r["period"]: r for r in rows("monthly_returns.csv")}
check("D 日序列", "月度盈亏表与日序列一致（最大偏差 $）",
      max(abs(v[0] - f(mrows[k]["pnl"])) for k, v in mon.items()), 0.0, tol=0.01, rel=False)
check("D 日序列", "月度胜率", card("monthly_win_rate"), sum(1 for v in mon.values() if v[0] > 0) / len(mon))
mr = [v[1] - 1 for v in mon.values()]
mm = sum(mr) / len(mr)
check("D 日序列", "月收益标准差", card("monthly_std"), math.sqrt(sum((x - mm) ** 2 for x in mr) / (len(mr) - 1)))
check("D 日序列", "换手 = 成交量 ÷ 当前净值", card("turnover_equity"), f(M["official"]["windows"]["allTime"]["vlm"]) / f(M["current"]["equity"]))

# ------------------------------------------------------------- E. windows
for w, key in (("30d", "month"), ("7d", "week")):
    check("E 窗口", f"{w} 总盈亏 = 官方 {key} 盈亏", card("total_pnl", w), f(M["official"]["windows"][key]["pnl"]))

# ------------------------------------------------------------- report
fails = [r for r in results if not r[5]]
fmt = lambda v: "—" if v is None else (f"{v:,.6g}" if isinstance(v, float) else str(v))
print(f"钱包 {M['wallet']}  共 {len(results)} 项，通过 {len(results) - len(fails)}，不符 {len(fails)}")
for g, name, a, b, diff, ok in results:
    print(f"{'PASS' if ok else 'FAIL'}  {g:8s} {name:40s} 报告 {fmt(a):>16s}  复算 {fmt(b):>16s}")
