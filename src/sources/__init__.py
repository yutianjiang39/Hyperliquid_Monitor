"""Source tiers and optional plugin registry."""
from __future__ import annotations

from sources.plugins.dune import Dune
from sources.plugins.hydromancer import Hydromancer

PLUGINS = {cls.name: cls for cls in (Hydromancer, Dune)}
DEFAULT_PLUGIN_CONFIG = {"hydromancer": {"enabled": False, "url": ""},
                         "dune": {"enabled": False, "query_id": ""}}
TIERS = {
    "T0": "账户快照（当前状态真值）",
    "T0'": "官方 portfolio 历史（净值与 PnL 真值）",
    "T1": "成交 / 流水 / 资金费明细",
    "T1'": "行情与 K 线（估值）",
    "T2": "官方 S3 归档",
    "T3": "官方排行榜（交叉核对）",
    "P": "第三方插件（仅补历史）",
}


def plugin_status(config):
    settings = (config or {}).get("plugins") or {}
    out = []
    for name, cls in PLUGINS.items():
        plugin = cls(settings.get(name))
        out.append({"name": name, "enabled": bool(plugin.settings.get("enabled")),
                    "available": plugin.available(), "env_key": cls.env_key,
                    "capabilities": sorted(cls.capabilities), "cost_hint": cls.cost_hint})
    return out


def active_plugins(config, only=None):
    settings = (config or {}).get("plugins") or {}
    result = []
    for name, cls in PLUGINS.items():
        if only is not None and name not in only:
            continue
        plugin = cls(settings.get(name))
        if (plugin.settings.get("enabled") or only is not None) and plugin.available():
            result.append(plugin)
    return result
