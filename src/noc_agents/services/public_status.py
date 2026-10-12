"""The customer's service status page (``GET /api/v1/public/status``): what a customer may know.

Open on purpose, with no role, because it answers "is it my phone or the network?" before a
complaint is filed. So it carries only what an operator would put on a public status page:

``regions``
    Every region on the operator profile, in region-code order, with its state in one word,
    the services affected, the counties named on the open tickets and since when. A state
    comes from the open, service-affecting tickets in the region (``NOT_OPEN_STATUSES``
    excluded, the same meaning of open as every console page): any P1 is ``MAJOR``, else any
    P2 is ``PARTIAL``, else any P3/P4 is ``MINOR``. With no ticket, a scheduled maintenance
    window in progress makes it ``MAINTENANCE``; otherwise it is ``NORMAL``.
``planned``
    Maintenance windows a person has approved (``SCHEDULED``) that end after now and start
    within ``PLANNED_DAYS``: when, and where at region level (a single-site window names its
    county, never the site).

Never on this payload: a site, site code, ticket number or id, subscriber count, vendor,
engineer, alarm text or note. Service codes come from a closed vocabulary (``SERVICES``), so
nothing a ticket carries in free text reaches it. Read-only, and every query goes through
``_owned``, so another operator's tickets and windows never move this operator's page.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from noc_agents.api.deps import _owned, _settings
from noc_agents.db.models import IncidentRow, utcnow
from noc_agents.db.models_maintenance import WINDOW_SCHEDULED, MaintenanceWindowRow
from noc_agents.services.clock import iso_z, to_utc
from noc_agents.services.lifecycle import NOT_OPEN_STATUSES
from noc_agents.services.sites import lookup_site

#: The states, worst first; the page shows the worst one as its headline.
STATES: tuple[str, ...] = ("MAJOR", "PARTIAL", "MINOR", "MAINTENANCE", "NORMAL")
#: The services a customer recognises. A ticket's own codes map onto these; anything else is dropped.
SERVICES: tuple[str, ...] = ("VOICE", "DATA", "SMS", "MPESA")
_SERVICE_OF: dict[str, str] = {"VOICE": "VOICE", "DATA": "DATA", "SMS": "SMS", "MPESA": "MPESA", "MPESA_CORRIDOR": "MPESA"}
#: How far ahead planned works are listed, and how long a restoration is worth saying.
PLANNED_DAYS = 7
RESTORED_HOURS = 24
#: Every region carries exactly these keys (pinned by tests/unit/test_public_status.py).
REGION_KEYS: tuple[str, ...] = (
    "region_code",
    "label",
    "state",
    "services",
    "counties",
    "since",
    "restored_at",
)
PLANNED_KEYS: tuple[str, ...] = ("starts_at", "ends_at", "scope", "region_code", "label", "county")


def _naive_utc(dt: datetime) -> datetime:
    aware = to_utc(dt)
    assert aware is not None
    return aware.replace(tzinfo=None)


def _services(inc: IncidentRow) -> set[str]:
    try:
        raw = json.loads(inc.services_impacted_json or "[]")
    except (TypeError, ValueError):
        raw = []
    out = {_SERVICE_OF[s] for s in raw if isinstance(s, str) and s in _SERVICE_OF}
    if inc.mpesa_risk:
        out.add("MPESA")
    return out


def _state_of(priorities: set[str], maintenance_now: bool) -> str:
    if "P1" in priorities:
        return "MAJOR"
    if "P2" in priorities:
        return "PARTIAL"
    if priorities:
        return "MINOR"
    return "MAINTENANCE" if maintenance_now else "NORMAL"


def _window_regions(win: MaintenanceWindowRow, codes: list[str]) -> tuple[str, list[str], str | None]:
    """Where a window is, at region level: its scope word, the regions it covers, a county."""
    scope = str(win.scope or "").upper()
    if scope == "NETWORK":
        return "NETWORK", list(codes), None
    if scope == "REGION":
        code = str(win.scope_ref or "").upper()
        return "REGION", [code] if code in codes else [], None
    site = lookup_site(win.scope_ref)
    if site is None or site.region_code.upper() not in codes:
        return "LOCAL", [], None
    return "LOCAL", [site.region_code.upper()], site.county or None


def public_status(session: Session, *, now: datetime | None = None) -> dict[str, Any]:
    """The status page payload for the active operator."""
    now = _naive_utc(now) if now is not None else _naive_utc(utcnow())
    op = _settings().operator
    codes = sorted(op.regions)

    open_rows = session.scalars(
        _owned(IncidentRow).where(
            IncidentRow.status.not_in(NOT_OPEN_STATUSES),
            IncidentRow.service_affecting.is_(True),
            IncidentRow.region_code.in_(codes),
        )
    ).all()
    restored_rows = session.scalars(
        _owned(IncidentRow).where(
            IncidentRow.restored_at.is_not(None),
            IncidentRow.restored_at >= now - timedelta(hours=RESTORED_HOURS),
            IncidentRow.restored_at <= now,
            IncidentRow.region_code.in_(codes),
        )
    ).all()
    windows = session.scalars(
        _owned(MaintenanceWindowRow)
        .where(
            MaintenanceWindowRow.status == WINDOW_SCHEDULED,
            MaintenanceWindowRow.ends_at > now,
            MaintenanceWindowRow.starts_at < now + timedelta(days=PLANNED_DAYS),
        )
        .order_by(MaintenanceWindowRow.starts_at.asc())
    ).all()

    planned: list[dict[str, Any]] = []
    under_works: set[str] = set()
    for win in windows:
        scope, covered, county = _window_regions(win, codes)
        if not covered:
            continue
        if win.starts_at <= now:
            under_works.update(covered)
        label = getattr(op.regions[covered[0]], "label", covered[0]) if scope != "NETWORK" else None
        planned.append(
            {
                "starts_at": iso_z(win.starts_at),
                "ends_at": iso_z(win.ends_at),
                "scope": scope,
                "region_code": covered[0] if scope != "NETWORK" else None,
                "label": label,
                "county": county,
            }
        )

    regions = []
    for code in codes:
        mine = [r for r in open_rows if r.region_code == code]
        services: set[str] = set()
        for row in mine:
            services |= _services(row)
        starts = [r.outage_start_at or r.created_at for r in mine if (r.outage_start_at or r.created_at)]
        restored = [r.restored_at for r in restored_rows if r.region_code == code and r.restored_at]
        cfg = op.regions[code]
        # Only counties of this region: a ticket's county is free data from the site record, and a
        # public page must never place an outage outside the region it lists it under.
        own_counties = {str(c).casefold() for c in (getattr(cfg, "counties", None) or [])}
        regions.append(
            {
                "region_code": code,
                "label": getattr(cfg, "label", code),
                "state": _state_of({r.priority for r in mine}, code in under_works),
                "services": [s for s in SERVICES if s in services],
                "counties": sorted({r.county for r in mine if r.county and (not own_counties or r.county.casefold() in own_counties)}),
                "since": iso_z(min(starts)) if starts else None,
                "restored_at": iso_z(max(restored)) if restored else None,
            }
        )

    worst = min((STATES.index(r["state"]) for r in regions), default=STATES.index("NORMAL"))
    return {
        "generated_at": iso_z(now),
        "operator_display_name": str(op.display_name).replace(" (demo profile)", ""),
        "state": STATES[worst],
        "regions": regions,
        "planned": planned,
    }
