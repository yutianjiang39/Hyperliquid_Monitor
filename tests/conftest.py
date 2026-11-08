import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

FIXTURES = ROOT / "tests" / "fixtures"
WALLETS = ("0x8bae3527e5a33fa0cf184f37bc112d071463ab6d", "0xf97ad6704baec104d00b88e0c157e2b7b3a1ddd1",
           "0xcb02837caaee310c178501855b63bf7f4f4b1f8b")


class FixtureClient:
    """Serves recorded /info responses; unknown requests raise like a failed call."""

    def __init__(self, wallet):
        self.folder = FIXTURES / wallet

    def post(self, body):
        kind = body["type"]
        if kind == "perpDexs":
            return json.loads((FIXTURES / "perpDexs.json").read_text(encoding="utf-8"))
        name = kind + ("_" + body["dex"] if body.get("dex") else "")
        path = self.folder / (name + ".json")
        if not path.is_file():
            raise RuntimeError("no fixture for " + name)
        return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def store(tmp_path):
    from collector import HistoryStore
    s = HistoryStore(tmp_path / "wallet_history.sqlite", WALLETS[0])
    yield s
    s.close()
