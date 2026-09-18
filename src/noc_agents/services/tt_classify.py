from __future__ import annotations

from dataclasses import dataclass

from noc_agents.config import OperatorConfig
from noc_agents.services.assignment import alarm_tokens


@dataclass(frozen=True)
class TTClassification:
    tt_category: str
    tt_category_label: str
    symptom_code: str
    technology_csv: str
    site_class: str
    rationale: str


def _match_category(alarm_code: str, failure_domain: str) -> str:
    a = alarm_code.upper()
    d = failure_domain.upper()
    tok = alarm_tokens(alarm_code)
    if "FUEL" in a or "THEFT" in a:
        return "POWER_FUEL"
    if "GENSET" in a or "DG_" in a:
        return "POWER_GENSET"
    if "RECT" in a or "BATTERY" in a:
        return "POWER_RECTIFIER"
    if "GRID" in a or "POWER" in a:
        if d == "POWER" or "POWER" in a or "GRID" in a:
            return "POWER_GRID"
    if d == "POWER":
        return "POWER_GRID"
    if "FIBRE" in a or "FIBER" in a or "FO" in tok:
        return "TX_FIBRE"
    if "MW" in a or "MICROWAVE" in a or "BER" in tok:
        return "TX_MW"
    if d == "TRANSMISSION":
        return "TX_FIBRE"
    if "VSWR" in a:
        return "RADIO_VSWR"
    if d in ("RADIO", "ACCESS"):
        return "RADIO_CELL"
    if d == "CORE" or "CORE" in a:
        return "CORE_NODE"
    if "FLOOD" in a or "ACCESS" in a:
        return "ENV_ACCESS"
    if "TEMP" in a or "AC" in a or d == "ENVIRONMENT":
        return "ENV_TEMP"
    return "OTHER"


def classify_tt(
    *,
    alarm_code: str,
    failure_domain: str,
    site_type: str,
    technology: list[str] | None,
    cfg: OperatorConfig,
) -> TTClassification:
    code = _match_category(alarm_code, failure_domain)
    label = (cfg.tt_categories or {}).get(code) or code.replace("_", " ").title()
    site_class = (cfg.site_class_by_type or {}).get(site_type.upper(), "STANDARD")
    tech = ",".join(technology or ["4G"])
    return TTClassification(
        tt_category=code,
        tt_category_label=str(label),
        symptom_code=alarm_code.upper(),
        technology_csv=tech,
        site_class=str(site_class),
        rationale=(
            f"alarm={alarm_code} domain={failure_domain} → "
            f"TT category {code} ({label}); site_class={site_class}"
        ),
    )
