"""Dev-only: capture live public /info responses into tests/fixtures/<wallet>/.

Usage: python tools/record_fixtures.py 0xWALLET [0xWALLET ...]
Only read-only Info endpoints are called. Large fill lists are trimmed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from collector import InfoClient, wallet_address  # noqa: E402

USER_ENDPOINTS = ("clearinghouseState", "spotClearinghouseState", "userAbstraction", "subAccounts",
                  "userVaultEquities", "delegatorSummary", "frontendOpenOrders", "userFees",
                  "userRateLimit", "portfolio", "userFills", "historicalOrders")


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, default=str), encoding="utf-8")


def record(client, wallet, out):
    for endpoint in USER_ENDPOINTS:
        value = client.post({"type": endpoint, "user": wallet})
        if endpoint in ("userFills", "historicalOrders") and isinstance(value, list):
            value = value[:300]
        dump(out / f"{endpoint}.json", value)
    dexes = client.post({"type": "perpDexs"})
    for entry in dexes:
        if entry and entry.get("name"):
            dump(out / f"clearinghouseState_{entry['name']}.json",
                 client.post({"type": "clearinghouseState", "user": wallet, "dex": entry["name"]}))


def main(argv):
    client = InfoClient()
    base = ROOT / "tests" / "fixtures"
    dump(base / "perpDexs.json", client.post({"type": "perpDexs"}))
    dump(base / "spotMeta.json", client.post({"type": "spotMeta"}))
    for raw in argv:
        wallet = wallet_address(raw)
        print("recording", wallet)
        record(client, wallet, base / wallet)


if __name__ == "__main__":
    main(sys.argv[1:])
