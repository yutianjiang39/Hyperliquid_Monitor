"""Deterministic SIMULATED data; no actual wallet records."""
from decimal import Decimal as D


def demo_records():
    fills, ledger, funding = [], [], []
    base = 1767225600000
    for cycle in range(80):
        coin = "BTC" if cycle % 2 else "ETH"
        p = D("90000") if coin == "BTC" else D("3000")
        qty = D("0.05") if coin == "BTC" else D("1")
        side = 1 if cycle % 3 else -1
        diff = D(cycle % 11 - 4) * (D("100") if coin == "BTC" else D("5"))
        stamp = base + cycle * 7_200_000
        legs = [(D(0), qty, side, p, D(0)), (qty*side, qty/2, side, p, D(0)),
                (qty*D("1.5")*side, qty/2, -side, p+diff, diff*qty/2*side),
                (qty*side, qty, -side, p+diff, diff*qty*side)]
        for i, (before, size, sign, px, pnl) in enumerate(legs):
            n = cycle * 4 + i + 1
            fills.append({"coin": coin, "time": stamp+i*100000, "side": "B" if sign>0 else "A", "px":str(px),
                          "sz":str(size), "startPosition":str(before), "closedPnl":str(pnl), "fee":str(px*size*D("0.0003")),
                          "feeToken":"USDC", "tid":n, "oid":10000+n, "hash":"0x"+format(n,"064x")})
        funding.append({"time":stamp, "hash":"0x"+format(100000+cycle,"064x"),
                        "delta":{"type":"funding","coin":coin,"usdc":str(D(cycle%7-3)/10),"fundingRate":"0.00001","szi":str(qty*side)}})
    for i in range(12):
        ledger.append({"time":base+i*12_000_000,"hash":"0x"+format(200000+i,"064x"),
                       "delta":{"type":"deposit" if i%2==0 else "withdraw","usdc":str(1000+i*50)}})
    # Include one visible incomplete legacy episode so unknown fields can be seen.
    fills.append({"coin":"BTC","time":base+81*7_200_000,"side":"A","px":"91000","sz":"0.1", "startPosition":"0.1",
                  "closedPnl":None,"fee":None,"feeToken":None,"tid":99999,"oid":99999,"hash":"0x"+"ef"*32,
                  "_quality":"legacy_trade_missing_pnl_fee"})
    return fills, ledger, funding


def demo_portfolio(fills, ledger, funding):
    """Deterministic synthetic 15-minute equity samples for offline UI checks.

    This is a marked demo account with zero unrealized PnL, not a fetched wallet.
    Deposits/withdrawals and known fees/funding affect the sample balance.
    """
    changes = []
    for row in fills:
        if row.get("closedPnl") is not None and row.get("fee") is not None:
            changes.append((row["time"], D(row["closedPnl"]) - D(row["fee"])))
    for row in funding:
        changes.append((row["time"], D(row["delta"]["usdc"])))
    for row in ledger:
        delta = row["delta"]
        changes.append((row["time"], D(delta["usdc"]) * (1 if delta["type"] == "deposit" else -1)))
    changes.sort(key=lambda x: x[0])
    step = 15 * 60 * 1000
    start = min(row["time"] for row in fills) - step
    end = max(row["time"] for row in fills) + 2 * step
    index, balance, history = 0, D("10000"), []
    for stamp in range(start, end + 1, step):
        while index < len(changes) and changes[index][0] <= stamp:
            balance += changes[index][1]
            index += 1
        history.append([stamp, str(balance)])
    return [["allTime", {"accountValueHistory": history}],
            ["perpAllTime", {"accountValueHistory": history}]]

