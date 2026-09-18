from __future__ import annotations

from dataclasses import dataclass

from noc_agents.config import OperatorConfig
from noc_agents.domain.enums import Priority, max_priority


@dataclass(frozen=True)
class SeverityResult:
    priority: Priority
    rationale: str
    mpesa_risk: bool
    base: Priority
    floor: Priority
    forced_p1: bool


def users_to_priority(users: int, cfg: OperatorConfig) -> Priority:
    t = cfg.priority_thresholds
    if users <= t.P4_max_users:
        return Priority.P4
    if users <= t.P3_max_users:
        return Priority.P3
    if users <= t.P2_max_users:
        return Priority.P2
    return Priority.P1


def site_floor(site_type: str, cfg: OperatorConfig) -> Priority:
    raw = cfg.site_type_priority_floor.get(site_type.upper(), "P4")
    return Priority(raw)


def evaluate_severity(
    *,
    users_affected: int,
    site_type: str,
    region_code: str,
    multi_region: bool = False,
    child_sites_down: int = 0,
    cfg: OperatorConfig,
) -> SeverityResult:
    base = users_to_priority(users_affected, cfg)
    floor = site_floor(site_type, cfg)
    final = max_priority(base, floor)
    forced = False
    rules = cfg.p1_force_rules or {}
    reasons: list[str] = [
        f"operator={cfg.operator_id}",
        f"users={users_affected}→{base.value}",
        f"site_type={site_type} floor={floor.value}",
    ]

    if multi_region and rules.get("multi_region"):
        final = Priority.P1
        forced = True
        reasons.append("forced P1: multi_region cascade")

    site_types = [s.upper() for s in (rules.get("site_types") or [])]
    if site_type.upper() in site_types:
        final = Priority.P1
        forced = True
        reasons.append(f"forced P1: site_type in {site_types}")

    child_gte = int(rules.get("child_sites_down_gte") or 10**9)
    if child_sites_down >= child_gte:
        final = Priority.P1
        forced = True
        reasons.append(f"forced P1: child_sites_down={child_sites_down}>={child_gte}")

    users_gte = int(rules.get("users_affected_gte") or 10**12)
    if users_affected >= users_gte:
        final = Priority.P1
        forced = True
        reasons.append(f"forced P1: users_affected>={users_gte}")

    mpesa = False
    mr = cfg.mpesa_risk
    if mr.enable and site_type.upper() in [s.upper() for s in mr.site_types]:
        if not mr.regions or region_code.upper() in [r.upper() for r in mr.regions]:
            mpesa = True
            reasons.append("mpesa_risk=true (CORE/HUB corridor rule)")

    reasons.append(f"final={final.value}")
    return SeverityResult(
        priority=final,
        rationale="; ".join(reasons),
        mpesa_risk=mpesa,
        base=base,
        floor=floor,
        forced_p1=forced,
    )
