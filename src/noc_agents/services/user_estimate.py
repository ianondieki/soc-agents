from __future__ import annotations

from noc_agents.config import OperatorConfig

# Nairobi metro regions share dense urban estimates
_NBI_REGIONS = {"NBI_E", "NBI_W", "NBI"}


def estimate_users(
    site_type: str,
    region_code: str,
    cfg: OperatorConfig,
    override: int | None = None,
) -> int:
    if override is not None and override >= 0:
        return override
    d = cfg.user_estimate_defaults
    st = site_type.upper()
    reg = region_code.upper()
    if st == "CORE":
        return int(d.get("CORE", 2_500_000))
    if st == "HUB":
        if reg in _NBI_REGIONS:
            return int(d.get("HUB_NBI", 450_000))
        return int(d.get("HUB_OTHER", 180_000))
    if st == "ENODEB":
        if reg in _NBI_REGIONS or reg == "CST":
            return int(d.get("ENODEB_URBAN", 22_000))
        return int(d.get("ENODEB_RURAL", 8_000))
    if st == "GNODEB":
        return int(d.get("GNODEB", 15_000))
    if st in ("BTS", "NODEB"):
        return int(d.get("BTS_RURAL", 3_500))
    if st == "TX":
        return int(d.get("TX", 50_000))
    return int(d.get("DEFAULT", 5_000))
