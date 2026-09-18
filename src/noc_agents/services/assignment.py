from __future__ import annotations

import random
import re
from dataclasses import dataclass

from noc_agents.config import OperatorConfig, RegionConfig
from noc_agents.domain.enums import AssigneeType

_SEP = re.compile(r"[^A-Z0-9]+")


def alarm_tokens(alarm_code: str) -> set[str]:
    """Upper-case tokens of an alarm code split on any non-alphanumeric run: TX_FIBER_CUT -> {TX, FIBER, CUT}.

    Only the two fragments that misfire as substrings (BER inside FIBER, FO inside FORCED /
    FORWARD) are matched against tokens; every other rule stays a substring rule on purpose.
    """
    return {t for t in _SEP.split(alarm_code.upper()) if t}


@dataclass(frozen=True)
class AssignmentResult:
    assignee_type: AssigneeType
    assignee_name: str
    msp_name: str | None
    fe_name: str | None
    rnio_name: str | None
    domain_key: str
    radio_oem: str
    responsible_party: str  # MSP or FE name for ticket "responsible" field
    rationale: str


def domain_lane(failure_domain: str, site_type: str, alarm_code: str = "") -> str:
    """Map failure to power | tx_fiber | tx_mw | radio | core | unknown."""
    d = failure_domain.upper()
    st = site_type.upper()
    a = alarm_code.upper()
    tok = alarm_tokens(alarm_code)
    if st == "CORE" or d == "CORE":
        return "core"
    if d == "POWER" or st == "POWER":
        return "power"
    if d == "ENVIRONMENT":
        return "power"  # often passive/power plant partners
    if "MW" in a or "MICROWAVE" in a or "BER" in tok:
        return "tx_mw"
    if d == "TRANSMISSION" or "FIBRE" in a or "FIBER" in a or "FO" in tok:
        return "tx_fiber"
    if st == "TX":
        return "tx_mw" if "MW" in a else "tx_fiber"
    if d in ("RADIO", "ACCESS"):
        return "radio"
    return "unknown"


def _pick_fe(cfg: OperatorConfig, region_code: str, region: RegionConfig | None) -> str:
    pool = (cfg.field_engineers_demo or {}).get(region_code.upper()) or []
    if pool:
        return pool[0]  # deterministic primary demo FE
    return region.fe_oncall if region else f"FE-{region_code}-01"


def assign(
    *,
    failure_domain: str,
    site_type: str,
    region_code: str,
    cfg: OperatorConfig,
    alarm_code: str = "",
) -> AssignmentResult:
    """Region-aware Safaricom assignment.

    Power → responsible power MSP (Egypro / Tetranet / ATC …)
    TX    → responsible fibre/TX MSP (Egypro Fibre / Soliton / Camusat …)
    Radio → Huawei Radio in NBI_W/CST; else regional FE
    """
    reg = region_code.upper()
    region: RegionConfig | None = cfg.regions.get(reg)
    rnio = region.rnio if region else f"RNIO-{reg}"
    fe = _pick_fe(cfg, reg, region)
    lane = domain_lane(failure_domain, site_type, alarm_code)
    by_reg = (cfg.assignment_by_region or {}).get(reg) or {}
    radio_oem = str(by_reg.get("radio_oem") or (region.radio_oem if region else "MIXED") or "MIXED")

    pool_key = {
        "power": "power",
        "tx_fiber": "tx_fiber",
        "tx_mw": "tx_mw",
        "radio": "radio",
        "core": "core",
        "unknown": "power",
    }.get(lane, "power")

    pool: list[str]
    if pool_key == "core":
        pool = ["NOC"]
    else:
        raw = by_reg.get(pool_key)
        if isinstance(raw, list) and raw:
            pool = [str(x) for x in raw]
        else:
            # legacy matrix fallback
            legacy_map = {
                "power": "power_passive",
                "tx_fiber": "transmission_fiber",
                "tx_mw": "transmission_mw",
                "radio": "radio_active",
            }
            legacy = legacy_map.get(pool_key, "unknown")
            pool = list(cfg.assignment_matrix.get(legacy) or ["NOC"])

    choice = pool[0]
    rationale_bits = [
        f"region={reg} ({region.label if region else reg})",
        f"lane={lane}",
        f"pool={pool}",
        f"primary={choice}",
        f"radio_oem={radio_oem}",
    ]

    if choice in ("FIELD_ENGINEER", "NOC"):
        if choice == "NOC":
            return AssignmentResult(
                assignee_type=AssigneeType.NOC,
                assignee_name="NOC-QUEUE",
                msp_name=None,
                fe_name=fe,
                rnio_name=rnio,
                domain_key=lane,
                radio_oem=radio_oem,
                responsible_party="NOC-QUEUE",
                rationale="; ".join(rationale_bits) + "; core/NOC queue",
            )
        return AssignmentResult(
            assignee_type=AssigneeType.FIELD_ENGINEER,
            assignee_name=fe,
            msp_name=None,
            fe_name=fe,
            rnio_name=rnio,
            domain_key=lane,
            radio_oem=radio_oem,
            responsible_party=fe,
            rationale="; ".join(rationale_bits) + f"; FE={fe}",
        )

    # MSP / OEM vendor (Egypro, Tetranet, Huawei Radio, Camusat, …)
    return AssignmentResult(
        assignee_type=AssigneeType.MSP,
        assignee_name=choice,
        msp_name=choice,
        fe_name=fe,
        rnio_name=rnio,
        domain_key=lane,
        radio_oem=radio_oem,
        responsible_party=choice,
        rationale="; ".join(rationale_bits) + f"; FE support={fe}; RNIO={rnio}",
    )


# Back-compat helpers used by older tests
def failure_domain_to_matrix_key(domain: str, site_type: str, alarm_code: str = "") -> str:
    lane = domain_lane(domain, site_type, alarm_code)
    return {
        "power": "power_passive",
        "tx_fiber": "transmission_fiber",
        "tx_mw": "transmission_mw",
        "radio": "radio_active",
        "core": "core",
        "unknown": "unknown",
    }.get(lane, "unknown")
