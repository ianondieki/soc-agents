"""Planned maintenance: plans, tasks, windows, the two approval gates, the rain guard and
the planned-window stop-clock **proposal** (spec §7.5; ``MAINTENANCE_ENABLED=false``).

``MAINTENANCE_ENABLED`` defaults to **false**. With it off nothing in this module runs: no
task is proposed, no HITL card is raised, no window can be scheduled, the two jobs report
themselves off and every route in ``api/routers/maintenance.py`` answers 404. The system
behaves exactly as it does today.

FOUR IDEAS CARRY THIS LANE
==========================

**1. Planned maintenance takes live customers off air on purpose, so there are two gates
and they ask different questions.**

``APPROVE_SCHEDULE`` signs off *the programme*: this work, at this site, on this date, with
this assignee. ``APPROVE_MAINTENANCE_WINDOW`` signs off *going ahead on the night*: the CA
Condition 9.1 reference is in hand, the customer notice has gone out, the weather is not
about to make a five-hour job a fifteen-hour one, and no other crew is already booked on the
same site. A programme approved in a Tuesday planning meeting is not permission to switch a
hub off on Friday, and :func:`schedule_window` enforces that structurally: it calls
:func:`window_approval_of`, which requires an ``APPROVE_MAINTENANCE_WINDOW`` card pointing at
*this* window. An APPROVED ``APPROVE_SCHEDULE`` card — even one on the same anchor incident,
even for a task inside this very window — fails the ``task_type`` check and the window stays
PROPOSED. ``tests/unit/test_maintenance.py`` enumerates the bypasses.

**2. The rain guard fails SAFE and fails HONEST.**

The normal case is ``WEATHER_ENABLED=false``: there is no forecast at all. "No forecast" is
not "no rain", and a guard that returns CLEAR when it has no data is worse than no guard,
because it launders an absence of evidence into a positive assurance that somebody will act
on at 01:00. So :func:`rain_guard` is three-valued and says which of the three it is on the
card, always. See its docstring for the full reasoning, including why a *fresh forecast that
says storm* is a hard refusal with a named override, while *no forecast in the rain season*
raises the flag but does not refuse.

**3. The planned-window stop-clock is a PROPOSAL and nothing else.**

Stop-clock minutes are deducted from a vendor's SLA figure, and that is a commercial act
(§7.6). :func:`stop_clock_proposal` therefore returns a dict and writes **nothing** — it does
not call ``services.clock_events.open_clock_event``, it does not add a row, it does not
buffer an event. The only way a ``PLANNED_MAINTENANCE`` SCC is ever opened is a human on
``POST /api/v1/incidents/{id}/clock``, which is the existing, role-gated, audited route in
``api/routers/clocks.py``. Pinned by
``test_a_planned_window_proposal_never_opens_a_stop_clock_event``.

**4. Overlapping windows are decided, not emergent.**

Two windows may *overlap while PROPOSED* — planning is iterative and two teams drafting work
for the same night is normal. A second window may **not reach SCHEDULED** while another
SCHEDULED window covers an intersecting scope and an intersecting period
(:func:`overlapping_windows`, raising :class:`WindowOverlapError`). Two crews independently
taking the same site off air is exactly how a planned outage becomes an unplanned one, and
availability exclusion would count the same dark hour twice. Scope containment is explicit:
NETWORK conflicts with everything, REGION with the sites in it, SITE with itself. Intervals
are half-open — a window ending at 05:00 does not conflict with one starting at 05:00.

EAT vs UTC (§7.0.6)
-------------------
Every instant in these tables is naive **UTC**, like the rest of the schema. The floor, the
customer notice and the rain-season calendar all read Nairobi time, and that conversion
happens once, on the way out, through ``services.clock``. The bug this avoids is storing a
00:00–05:00 *EAT* window as if it were UTC, which puts a five-hour hub outage three hours
into the morning peak.

UNVERIFIED (§7.5.1, §7.5.6)
---------------------------
Every shipped interval is **secondary-sourced** — NFPA 110, IEEE 1187/1188 and TIA-222 are
paywalled — so :data:`DEFAULT_INTERVALS` names the practice in a note and
``maintenance_plans.standard_ref`` is NOT NULL, and neither is a claim that the standard has
been read. The CA licence Condition 9.1 written-approval requirement is taken from the
Network Facilities Provider licence template; the operator's actual licence class must be
confirmed with Legal before this lane is enabled.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from noc_agents.api.deps import _owned
from noc_agents.config import OperatorConfig
from noc_agents.db.models import AuditRow, HitlTaskRow, IncidentRow, new_id, utcnow
from noc_agents.db.models_maintenance import (
    CONSUMPTION_DRIVEN_TASK_TYPES,
    MAINTENANCE_TASK_TYPES,
    TASK_CANCELLED,
    TASK_DONE,
    TASK_INVITED,
    TASK_IN_PROGRESS,
    TASK_MISSED,
    TASK_PROPOSED,
    TASK_SCHEDULED,
    TASK_STATUSES,
    WINDOW_CANCELLED,
    WINDOW_COMPLETED,
    WINDOW_PROPOSED,
    WINDOW_SCHEDULED,
    WINDOW_SCOPES,
    MaintenancePlanRow,
    MaintenanceTaskRow,
    MaintenanceWindowRow,
)
from noc_agents.domain.enums import HitlTaskType
from noc_agents.realtime.commit_hook import buffer_event
from noc_agents.realtime.hub import RealtimeEvent
from noc_agents.scheduler import JobCard, JobResult
from noc_agents.services import sites as site_catalogue
from noc_agents.services.clock import eat_tz, fmt_eat, to_eat, z_utc
from noc_agents.services.clock_events import Interval, effective_intervals
from noc_agents.services.hitl import is_raiser

if TYPE_CHECKING:  # typing only — keeps the import graph of a flag-off process unchanged
    from noc_agents.config import AppSettings

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_ATTENDEES_REF",
    "DEFAULT_INTERVALS",
    "DEFAULT_WINDOW",
    "MAINTENANCE_ENABLED_ENV",
    "MAINTENANCE_PLAN_DUE_JOB",
    "MAINTENANCE_RAISER",
    "MAINTENANCE_WINDOW_SWEEP_JOB",
    "PLANNED_MAINTENANCE_SCC",
    "RAIN_SEASON_MONTHS",
    "SCHEDULE_ENTITY_TYPE",
    "SCHEDULE_TASK_TYPE",
    "WINDOW_ENTITY_TYPE",
    "WINDOW_TASK_TYPE",
    "CaApprovalRequired",
    "MaintenanceStateError",
    "NoAnchorIncident",
    "RainGuardBlocked",
    "RainVerdict",
    "ScheduleNotApproved",
    "WindowNotApproved",
    "WindowOverlapError",
    "anchor_incident",
    "bump_sequence",
    "cancel_window",
    "complete_task",
    "complete_window",
    "create_plan",
    "create_window",
    "default_window_bounds",
    "intervals_overlap",
    "is_planned",
    "maintenance_config",
    "maintenance_enabled",
    "make_uid",
    "mark_invited",
    "mark_scheduled",
    "next_due",
    "overlapping_windows",
    "owned_tasks",
    "plan_due_job",
    "plan_interval",
    "plan_out",
    "plan_sites",
    "planned_maintenance_tag",
    "planned_minutes",
    "propose_assignee",
    "propose_task",
    "rain_guard",
    "request_schedule_approval",
    "request_window_approval",
    "reschedule_window",
    "schedule_approval_of",
    "schedule_window",
    "scheduled_windows_for_site",
    "scopes_conflict",
    "stop_clock_proposal",
    "task_out",
    "tasks_for_window",
    "validate_plan",
    "window_ics_fields",
    "window_approval_of",
    "window_covers",
    "window_out",
    "window_sweep_job",
]


# --------------------------------------------------------------------------------- the flag

MAINTENANCE_ENABLED_ENV = "MAINTENANCE_ENABLED"
#: Spellings that read as true. Spelled out again rather than imported (as ``services/pir.py``,
#: ``services/regulatory.py`` and ``pollers/weather.py`` also do) so a flag-off process does not
#: import another lane to answer a question about this one.
_TRUE = frozenset({"1", "true", "yes", "on"})


def maintenance_enabled() -> bool:
    """``MAINTENANCE_ENABLED`` — default **false**. Only an explicit true value arms the lane.

    Read at call time, never frozen at import: tests flip it with ``monkeypatch.setenv`` and an
    operator flips it in ``.env`` between runs. Costs one ``os.getenv`` on the off path.
    """
    return (os.getenv(MAINTENANCE_ENABLED_ENV) or "").strip().lower() in _TRUE


# --------------------------------------------------------------------------- the vocabulary

#: ``hitl_tasks.task_type`` for the two gates. Read from the enum, which already carries both
#: members, so there is exactly one spelling of each in the process.
SCHEDULE_TASK_TYPE: str = HitlTaskType.APPROVE_SCHEDULE.value
WINDOW_TASK_TYPE: str = HitlTaskType.APPROVE_MAINTENANCE_WINDOW.value

#: ``hitl_tasks.entity_type`` for the two card kinds: what each task is really *about*. The
#: pair (task_type, entity_type+entity_id) is what makes one approval unable to stand in for
#: another — see :func:`_approval_of`.
SCHEDULE_ENTITY_TYPE = "maintenance_task"
WINDOW_ENTITY_TYPE = "maintenance_window"

#: The raiser. A principal-shaped string that can never equal a human's name, so the
#: raiser ≠ approver rule (§6.5) never blocks the supervisor who has to approve the card.
MAINTENANCE_RAISER = "agent:MaintenancePlanningAgent"

#: Default ``recipients_ref`` for a window's iMIP invite. A key in the operator profile's
#: ``notification_recipients`` register (§7.0.2), never an address list: an unfilled key makes
#: ``services.notify.resolve_recipients`` refuse the dispatch, which is the correct failure —
#: an unsent invite is one visible, fixable problem; an invite sent to the wrong mailbox is a
#: disclosure of a planned outage that cannot be taken back.
DEFAULT_ATTENDEES_REF = "maintenance.recipients.FE_ONCALL"

#: The SCC code a planned window proposes (§7.6.1 vocabulary; the member already exists in
#: ``db.models_vendors.SCC_CODES``). Named here rather than imported so this module does not
#: take a dependency on the vendor lane's model module for one string.
PLANNED_MAINTENANCE_SCC = "PLANNED_MAINTENANCE"

#: The WS event this lane publishes, always through ``buffer_event`` so it leaves the process
#: only if the transaction that produced it actually committed (§7.0.4).
WINDOW_EVENT = "maintenance.window"


class MaintenanceStateError(ValueError):
    """The plan/task/window is not in a state that allows this transition (route: 409)."""


class ScheduleNotApproved(PermissionError):
    """No valid, APPROVED, human-resolved ``APPROVE_SCHEDULE`` card authorises this task."""


class WindowNotApproved(PermissionError):
    """No valid, APPROVED, human-resolved ``APPROVE_MAINTENANCE_WINDOW`` card authorises this
    window. Raised even when every task inside it has an approved schedule — that is the
    whole point of there being two gates (module docstring, idea 1)."""


class CaApprovalRequired(PermissionError):
    """A REGION/NETWORK window has no ``ca_approval_ref`` (licence Condition 9.1, §7.5.6)."""


class RainGuardBlocked(PermissionError):
    """A fresh forecast says storm over this window's scope. Overridable only by a named
    human supplying a reason, which is audited — never by code."""


class WindowOverlapError(ValueError):
    """Another SCHEDULED window already covers an intersecting scope and period."""


class NoAnchorIncident(RuntimeError):
    """``hitl_tasks.incident_id`` is NOT NULL and there is no incident to anchor on.

    Fail closed: no card can be raised, so no approval is possible, so nothing is scheduled —
    rather than a window that becomes executable without a gate. See :func:`anchor_incident`.
    """


# ------------------------------------------------------------------------------------ config

#: §7.5.1 ``maintenance.intervals``. Every note names the practice the interval comes from and
#: every one is **secondary-sourced** — the standards themselves are paywalled and have not
#: been read. They are defaults for a human to review, not findings.
DEFAULT_INTERVALS: dict[str, dict[str, Any]] = {
    "GENERATOR_EXERCISE": {
        "interval_days": 30,
        "note": "monthly >=30 min at >=30% nameplate (NFPA 110 practice; secondary source)",
    },
    "GENERATOR_LOAD_BANK": {"interval_days": 365, "note": "annual load bank (NFPA 110 practice; secondary source)"},
    "BATTERY_CHECK": {"interval_days": 7, "note": "weekly voltage (IEEE 1187/1188 practice; secondary source)"},
    "BATTERY_CAPACITY": {
        "interval_days": 90,
        "note": "quarterly capacity; replace on measured capacity/impedance",
    },
    "TOWER_VISUAL": {"interval_days": 365, "note": "annual visual (TIA-222 practice; secondary source)"},
    "TOWER_STRUCTURAL": {
        "interval_days": 1095,
        "note": "3y self-supporting / 5y guyed (TIA-222 practice; secondary source)",
    },
    "FUEL_RUN": {
        "consumption_driven": True,
        "note": "from battery_countdown_min / genset telemetry trend + last fill",
    },
}

#: §7.5.1 ``maintenance.window``. 00:00–05:00 EAT is the Kenyan overnight low-traffic window.
DEFAULT_WINDOW: dict[str, Any] = {
    "default_start_eat": "00:00",
    "default_end_eat": "05:00",
    "notice_days": 7,
    "rain_guard_months": [3, 4, 5, 10, 11, 12],
}

#: The Kenyan bimodal rainy seasons: MAM (March–May, the "long rains") and OND
#: (October–December, the "short rains"). Taken from ``cfg.maintenance.window.rain_guard_months``
#: when the profile supplies it. Evaluated against the window's **EAT** start date, because the
#: season is a Kenyan calendar fact: 21:30 UTC on 28 February is already 1 March in Nairobi.
RAIN_SEASON_MONTHS: tuple[int, ...] = tuple(DEFAULT_WINDOW["rain_guard_months"])

#: Where a reviewer finds the policy these numbers came from.
INTERVALS_YAML_PATH = "maintenance.intervals"
WINDOW_YAML_PATH = "maintenance.window"


def maintenance_config(cfg: OperatorConfig) -> dict[str, Any]:
    """The ``maintenance:`` block of the operator profile, over the §7.5.1 defaults.

    ``OperatorConfig`` does not declare a ``maintenance`` field yet, and pydantic's default
    ``extra="ignore"`` means a ``maintenance:`` block in the YAML is silently dropped until it
    does — so this reads defensively with ``getattr`` and falls back to the spec's own values.
    It starts honouring the profile the moment ``config.py`` grows the field, with no change
    here. (The same pattern ``services/regulatory.py`` and ``services/memory.py`` use.)
    """
    raw = getattr(cfg, "maintenance", None)
    block: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
    intervals = dict(DEFAULT_INTERVALS)
    for key, value in (block.get("intervals") or {}).items():
        if isinstance(value, dict):
            intervals[str(key).upper()] = {**intervals.get(str(key).upper(), {}), **value}
    window = {**DEFAULT_WINDOW, **(block.get("window") or {})}
    return {"intervals": intervals, "window": window}


def notice_days(cfg: OperatorConfig) -> int:
    """How many days of customer notice the operator's policy asks for (§7.5.1, default 7)."""
    try:
        return max(0, int(maintenance_config(cfg)["window"]["notice_days"]))
    except (TypeError, ValueError):
        return int(DEFAULT_WINDOW["notice_days"])


def rain_guard_months(cfg: OperatorConfig) -> tuple[int, ...]:
    """The months the rain guard treats as rain season (§7.5.1, default MAM + OND)."""
    raw = maintenance_config(cfg)["window"].get("rain_guard_months") or []
    months = tuple(int(m) for m in raw if str(m).strip().isdigit() and 1 <= int(m) <= 12)
    return months or RAIN_SEASON_MONTHS


# ------------------------------------------------------------------------- due-date arithmetic


def plan_interval(plan: MaintenancePlanRow) -> timedelta | None:
    """The plan's recurrence as a duration, or ``None`` when the calendar is not the driver.

    ``None`` means one of two things and both are deliberate refusals rather than guesses:

    * ``consumption_driven`` (``FUEL_RUN``) — the interval follows the genset's burn rate and
      the last fill, which live in telemetry this module does not read. A fuel run proposed on
      a fixed 30-day cycle is either a wasted truck roll or a site that runs dry at 03:00.
    * no interval recorded at all — a plan that cannot say how often is not a plan.

    ``interval_days`` and ``interval_hours`` add, so "every 90 days" and "every 500 running
    hours plus a quarterly check" can both be expressed.
    """
    if int(plan.consumption_driven or 0) or (plan.task_type or "").upper() in CONSUMPTION_DRIVEN_TASK_TYPES:
        return None
    days = int(plan.interval_days or 0)
    hours = int(plan.interval_hours or 0)
    if days <= 0 and hours <= 0:
        return None
    return timedelta(days=days, hours=hours)


#: Why the due date is where it is. Recorded on the card, because "when was this last done?"
#: is the first question anyone asks of a proposed maintenance task.
BASIS_LAST_COMPLETION = "LAST_COMPLETION"
BASIS_PLAN_CREATED = "PLAN_CREATED"
BASIS_CONSUMPTION = "CONSUMPTION_DRIVEN"
BASIS_NO_INTERVAL = "NO_INTERVAL"


def next_due(
    plan: MaintenancePlanRow,
    *,
    last_completed_at: datetime | None,
    now: datetime | None = None,
) -> tuple[datetime | None, str]:
    """``(due_at, basis)`` for the next occurrence of ``plan``, or ``(None, why-not)``.

    The arithmetic is trivial; the honest part is ``basis``.

    With a recorded completion the answer is a fact: ``last_completed_at + interval``. With
    **no** completion recorded, this system does not know when the generator was last
    exercised, and there are only two available answers. "Due immediately" turns the first
    tick after a plan is written into 6 000 overdue tasks. "Due one interval after the plan was
    created" quietly assumes the work was done on the day somebody typed the policy in. The
    second is the usable one, so it is what this returns — and it returns
    ``BASIS_PLAN_CREATED`` alongside it so the card says *assumed*, not *measured*, and the
    first cycle after a plan is written is the cycle in which real completion dates get
    backfilled. Nothing downstream may treat the two bases as the same statement.

    Naive UTC in, naive UTC out (§7.0.6). Pure: no session, no clock unless ``now`` is omitted.
    """
    interval = plan_interval(plan)
    if interval is None:
        if int(plan.consumption_driven or 0) or (plan.task_type or "").upper() in CONSUMPTION_DRIVEN_TASK_TYPES:
            return None, BASIS_CONSUMPTION
        return None, BASIS_NO_INTERVAL
    if last_completed_at is not None:
        return last_completed_at + interval, BASIS_LAST_COMPLETION
    base = plan.created_at or (now or utcnow())
    return base + interval, BASIS_PLAN_CREATED


def plan_sites(plan: MaintenancePlanRow, cfg: OperatorConfig) -> list[str]:
    """Every site id this plan covers, in catalogue order.

    ``site_id`` names one site. ``site_class`` names a set, and the set is resolved against
    **two** vocabularies on purpose, because this codebase genuinely has two and neither is
    wrong:

    * ``SiteRecord.site_class`` — the §7.0.7 catalogue vocabulary (MACRO / HUB / CORE /
      SMALL_CELL / FTTH_POP), i.e. what kind of site it physically is;
    * ``cfg.site_class_by_type`` — the operator profile's criticality banding (CRITICAL /
      MAJOR / STANDARD) derived from ``site_type``, i.e. how much it matters.

    A Planning engineer writing "every CRITICAL site gets a quarterly battery capacity test"
    and one writing "every HUB gets an annual tower inspection" are both saying something
    sensible, and refusing one of them because it used the other list would be a vocabulary
    quibble standing between a maintenance regime and the sites it protects. So both match,
    and the match is case-insensitive.
    """
    if (plan.site_id or "").strip():
        return [plan.site_id.strip()]
    want = (plan.site_class or "").strip().upper()
    if not want:
        return []
    by_type = {str(k).upper(): str(v).upper() for k, v in (cfg.site_class_by_type or {}).items()}
    out: list[str] = []
    for site in site_catalogue.all_sites():
        catalogue_class = (site.site_class or "").upper()
        criticality = by_type.get((site.site_type or "").upper(), "")
        if want in (catalogue_class, criticality):
            out.append(site.site_id)
    return out


def propose_assignee(plan: MaintenancePlanRow, site_id: str, cfg: OperatorConfig) -> str | None:
    """The role token the job proposes for this task (§7.5.3: ``fe_oncall`` / owner vendor).

    **A role token, never a person's name** (§7.11.8). ``cfg.regions[...].fe_oncall`` is already
    one (``FE-NBI-E-01``), and ``cfg.msp_contacts`` is keyed by vendor code (``EGYPRO``), which
    is also a token. The owner vendor wins when the plan names one, because a plan with a
    contracted owner is that vendor's work and routing it to the region's field engineer would
    quietly transfer a contractual obligation to the operator's own staff.

    ``owner_vendor_id`` is a ``vendors.id`` in the spec's DDL, but Planning very often has the
    code and not the uuid, so a value that matches an ``msp_contacts`` key is accepted as the
    token directly. A uuid that matches nothing here is left to the vendor lane to resolve —
    this returns the region's field engineer rather than inventing a token from a uuid.
    """
    owner = (plan.owner_vendor_id or "").strip()
    if owner:
        contacts = {str(k).upper(): k for k in (cfg.msp_contacts or {})}
        if owner.upper() in contacts:
            return owner.upper()
    site = site_catalogue.lookup_site(site_id)
    region = (site.region_code if site else "") or ""
    region_cfg = (cfg.regions or {}).get(region)
    token = (getattr(region_cfg, "fe_oncall", "") or "").strip() if region_cfg else ""
    return token or None


# ----------------------------------------------------------------- windows: scope and overlap


def intervals_overlap(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    """True when ``[a_start, a_end)`` and ``[b_start, b_end)`` intersect.

    **Half-open, deliberately.** A window that ends at 05:00 and one that starts at 05:00 are
    back-to-back, not overlapping: that is how a night of work is actually split between two
    crews, and treating it as a clash would refuse the most common legitimate arrangement in
    the name of a boundary condition. The same convention ``services.clock_events`` uses for
    stop-clock intervals.
    """
    return a_start < b_end and b_start < a_end


def scopes_conflict(scope_a: str, ref_a: str, scope_b: str, ref_b: str) -> bool:
    """True when two window scopes can touch the same site.

    Containment is stated here rather than left to emerge from a query:

    * ``NETWORK`` conflicts with everything, including another NETWORK window — a
      network-wide window is by definition every site;
    * ``REGION`` conflicts with the same region, and with a ``SITE`` window for a site the
      catalogue places in that region;
    * ``SITE`` conflicts with the same site id.

    A SITE window whose site is not in the catalogue cannot be placed in a region, so it
    conflicts only with an identical site id and with NETWORK. That is the conservative
    direction for an *unknown* site: it does not manufacture a regional clash out of a
    lookup failure, and the site-level clash — the one that actually puts two crews on one
    tower — is still caught.
    """
    a, b = (scope_a or "").upper(), (scope_b or "").upper()
    ra, rb = (ref_a or "").strip(), (ref_b or "").strip()
    if "NETWORK" in (a, b):
        return True
    if a == "REGION" and b == "REGION":
        return ra.upper() == rb.upper()
    if a == "SITE" and b == "SITE":
        return ra == rb
    site_ref, region_ref = (ra, rb) if a == "SITE" else (rb, ra)
    site = site_catalogue.lookup_site(site_ref)
    return bool(site) and (site.region_code or "").upper() == region_ref.upper()


def overlapping_windows(
    session: Session,
    operator_id: str,
    *,
    scope: str,
    scope_ref: str,
    starts_at: datetime,
    ends_at: datetime,
    statuses: tuple[str, ...] = (WINDOW_SCHEDULED,),
    exclude_id: str | None = None,
) -> list[MaintenanceWindowRow]:
    """Windows of ``operator_id`` in ``statuses`` that clash with the given scope and period.

    Defaults to SCHEDULED only, which is the rule stated in the module docstring: overlap
    while PROPOSED is allowed (planning is iterative), overlap once SCHEDULED is not. Callers
    that want to *warn* about a proposed clash pass ``statuses=(WINDOW_PROPOSED, WINDOW_SCHEDULED)``.

    The time predicate is pushed into SQL; the scope predicate is applied in Python because
    it needs the site catalogue (REGION ⊃ SITE), which SQLite cannot join to. The SQL half is
    what keeps the candidate set small.
    """
    stmt = select(MaintenanceWindowRow).where(
        MaintenanceWindowRow.operator_id == operator_id,
        MaintenanceWindowRow.status.in_(statuses),
        MaintenanceWindowRow.starts_at < ends_at,
        MaintenanceWindowRow.ends_at > starts_at,
    )
    if exclude_id:
        stmt = stmt.where(MaintenanceWindowRow.id != exclude_id)
    rows = session.scalars(stmt.order_by(MaintenanceWindowRow.starts_at)).all()
    return [w for w in rows if scopes_conflict(scope, scope_ref, w.scope, w.scope_ref)]


def window_covers(window: MaintenanceWindowRow, *, site_id: str, at: datetime) -> bool:
    """True when ``window`` covers ``site_id`` at instant ``at`` — scope and time together."""
    if not intervals_overlap(window.starts_at, window.ends_at, at, at + timedelta(microseconds=1)):
        return False
    return scopes_conflict(window.scope, window.scope_ref, "SITE", site_id)


# ---------------------------------------------------------------------------- the rain guard


@dataclass(frozen=True)
class RainVerdict:
    """What the rain guard concluded, and — always — on what evidence.

    ``verdict`` is one of :data:`RAIN_CLEAR`, :data:`RAIN_STORM`, :data:`RAIN_NO_FORECAST_SEASON`
    or :data:`RAIN_NO_FORECAST`. ``flag`` is what lands in ``maintenance_windows.rain_season_flag``.
    ``blocking`` is what :func:`schedule_window` refuses on. ``evidence`` carries the per-region
    forecast blocks (or their absence) so the card can show the approver what was actually known.
    """

    verdict: str
    flag: int
    blocking: bool
    reason: str
    evidence: dict[str, Any]

    def as_json(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "rain_season_flag": self.flag,
            "blocking": self.blocking,
            "reason": self.reason,
            "evidence": self.evidence,
            "yaml_path": WINDOW_YAML_PATH,
        }


#: A usable, fresh forecast covering the window's scope shows no storm.
RAIN_CLEAR = "CLEAR"
#: A usable, fresh forecast shows ``storm_flag`` over at least one region the window touches.
RAIN_STORM = "STORM_FORECAST"
#: No usable forecast, and the window falls in the MAM/OND rain season.
RAIN_NO_FORECAST_SEASON = "NO_FORECAST_RAIN_SEASON"
#: No usable forecast, outside the rain season.
RAIN_NO_FORECAST = "NO_FORECAST"


def _regions_for_window(window: MaintenanceWindowRow, cfg: OperatorConfig) -> list[str]:
    """The region codes a window's scope touches, for the weather lookup."""
    scope = (window.scope or "").upper()
    if scope == "REGION":
        return [(window.scope_ref or "").upper()]
    if scope == "NETWORK":
        return sorted((cfg.regions or {}).keys())
    site = site_catalogue.lookup_site(window.scope_ref)
    return [(site.region_code or "").upper()] if site and site.region_code else []


def rain_guard(
    session: Session,
    window: MaintenanceWindowRow,
    cfg: OperatorConfig,
    *,
    now: datetime | None = None,
) -> RainVerdict:
    """§7.5.3's rain guard, three-valued, composing the Phase 3 weather cache.

    WHY THREE VALUES AND NOT A BOOLEAN
    ----------------------------------
    The guard has to answer "will weather make this five-hour job dangerous or much longer?"
    and there are three honest answers, not two. ``WEATHER_ENABLED`` defaults to **false**,
    which is the normal state of this deployment, so the *most common* case is that there is
    no forecast at all — and "no forecast" is not "no rain". A two-valued guard has to map
    that case onto one of its two answers, and whichever it picks it is lying: CLEAR launders
    an absence of evidence into an assurance somebody will act on at 01:00 up a tower, and
    STORM makes the guard cry wolf every single night until somebody disables it, which is the
    same as CLEAR with extra steps. So the absence of data is its own verdict, it is never
    rendered as CLEAR, and it says so on the card.

    WHAT BLOCKS AND WHAT ONLY WARNS
    -------------------------------
    * :data:`RAIN_STORM` — a **fresh** forecast actively says storm over a region this window
      touches. ``blocking=True``: :func:`schedule_window` refuses, and the only way past is a
      named human passing ``override_rain`` with a reason, which is audited. Evidence exists
      and it says no; the default answer must be no.
    * :data:`RAIN_NO_FORECAST_SEASON` — no usable forecast, and the window's EAT start month is
      in the MAM/OND rain season. ``flag=1`` (the approver sees the warning §7.5.3 asks for)
      but ``blocking=False``. It does **not** refuse, and that is a deliberate line: refusing
      would mean this module decides, on no evidence whatsoever, that half the year is closed
      to planned maintenance — including the fuel runs and battery checks that are what keep
      sites up *through* the rains. A guard with no data may raise its hand; it may not make
      policy.
    * :data:`RAIN_NO_FORECAST` — no usable forecast, outside the season. ``flag=0``, but the
      verdict string is still ``NO_FORECAST`` and never ``CLEAR``, so nothing downstream can
      read "the guard passed" out of "the guard could not run".
    * :data:`RAIN_CLEAR` — a fresh forecast exists for every region touched and none shows a
      storm. This is the only verdict that means the guard actually checked and was satisfied.

    Composes ``pollers.weather.weather_risk_for_region`` rather than recomputing anything:
    that function is a pure cache read (it works with the network down), it already recomputes
    staleness against *now* rather than trusting the stored flag, and it is the same block
    ENRICH and the Wallboard read — so the approver's warning and the wallboard's risk strip
    can never disagree. A **stale** block is treated as no forecast: a four-hour-old "no storm"
    is not evidence about tonight.

    ``NETWORK`` scope evaluates every region on the profile; a storm anywhere flags the window,
    because a network-wide window is work at every site including the ones under the storm.
    """
    # Imported here, not at module import: a flag-off process should not pull the weather
    # poller (and its HTTP adapter) in to answer a question about maintenance. The same reason
    # ``scheduler/loop.py`` imports the weather job lazily.
    from noc_agents.pollers import weather

    now = now or utcnow()
    months = rain_guard_months(cfg)
    start_eat = to_eat(window.starts_at)
    in_season = bool(start_eat and start_eat.month in months)

    regions = [r for r in _regions_for_window(window, cfg) if r]
    evidence: dict[str, Any] = {
        "in_rain_season": in_season,
        "rain_guard_months": list(months),
        "window_start_eat": fmt_eat(window.starts_at, "%Y-%m-%d %H:%M"),
        "regions": {},
    }
    storms: list[str] = []
    unknown: list[str] = []
    for region in regions:
        block = weather.weather_risk_for_region(session, window.operator_id, region, now)
        if block is None:
            evidence["regions"][region] = {"forecast": "NONE"}
            unknown.append(region)
            continue
        if block.get("stale"):
            # A stale block is data about a moment that has passed. Recording the age rather
            # than the (possibly reassuring) contents is the honest summary.
            evidence["regions"][region] = {"forecast": "STALE", "age_s": block.get("age_s"), "last_error": block.get("last_error")}
            unknown.append(region)
            continue
        evidence["regions"][region] = {
            "forecast": "FRESH",
            "storm_flag": bool(block.get("storm_flag")),
            "level": block.get("level"),
            "source": block.get("source"),
            "age_s": block.get("age_s"),
        }
        if block.get("storm_flag"):
            storms.append(region)
    if not regions:
        # A window whose scope cannot be mapped to any region (an unknown site id) has no
        # forecast by construction; it is still governed by the season rule below.
        evidence["regions"] = {}
        unknown = ["<unmapped scope>"]

    if storms:
        return RainVerdict(
            RAIN_STORM,
            1,
            True,
            f"storm forecast over {', '.join(sorted(storms))} across this window; "
            "rescheduling is the default answer — overriding needs a named human and a reason",
            evidence,
        )
    if unknown:
        if in_season:
            return RainVerdict(
                RAIN_NO_FORECAST_SEASON,
                1,
                False,
                f"no usable forecast for {', '.join(unknown)} and the window falls in the "
                f"{start_eat.strftime('%B') if start_eat else '?'} rain season — treat as adverse until "
                "someone checks the sky; the guard could not clear this window",
                evidence,
            )
        return RainVerdict(
            RAIN_NO_FORECAST,
            0,
            False,
            f"no usable forecast for {', '.join(unknown)} (WEATHER_ENABLED is normally off); "
            "the guard did not run — this is not a statement that the weather is fine",
            evidence,
        )
    return RainVerdict(RAIN_CLEAR, 0, False, f"fresh forecast for {', '.join(regions)}, no storm flag", evidence)


# ------------------------------------------------------------------------- the anchor incident


def anchor_incident(session: Session, cfg: OperatorConfig, *, incident_id: str | None = None) -> IncidentRow | None:
    """The incident a maintenance HITL card hangs off, or ``None``.

    ``hitl_tasks.incident_id`` is NOT NULL, and operator ownership of a task is derived by
    joining it to ``incidents`` (``api.deps._OWNED_VIA_INCIDENT``), so a card that belongs to
    no incident can neither be stored nor fetched — it would be invisible in
    ``GET /api/v1/hitl/pending`` and ``POST /api/v1/hitl/{id}/approve`` would answer 404. A
    maintenance window is not an incident, so this lane has the same problem
    ``services/handover.py`` documented and solves it the same way: a **scoping** anchor, with
    what the card is really about carried in ``entity_type``/``entity_id``.

    Order of preference:

    1. ``maintenance_windows.incident_id`` when the window exists *because* of an incident
       (emergency work arising from a PRB) — then the anchor is not a workaround at all, it is
       the truth;
    2. otherwise the operator's most recent incident, **of any status**. Handover anchors to an
       *open* incident because the handover is about open incidents; maintenance is not, so
       narrowing to open ones would refuse to schedule work on a quiet network, which is the
       night you actually want to do it.
    3. ``None`` — a database with no incident at all. The caller fails closed
       (:class:`NoAnchorIncident`): no card, therefore no approval, therefore nothing scheduled.

    This is a schema workaround and it should not survive. Making ``hitl_tasks.incident_id``
    nullable and adding ``hitl_tasks.operator_id`` (so ownership stops being derived through
    the join) removes it; both are additive changes to files this lane does not own.
    """
    if incident_id:
        row = session.scalar(_owned(IncidentRow).where(IncidentRow.id == incident_id))
        if row is not None:
            return row
    return session.scalar(_owned(IncidentRow).order_by(IncidentRow.created_at.desc()).limit(1))


# ------------------------------------------------------------------------------ audit helper


def _json_safe(value: Any) -> Any:
    """Make a payload safe for ``hitl_tasks.proposed_payload_json``.

    ``HitlTaskRow.proposed_payload`` setter is a plain ``json.dumps`` with no ``default=``, and
    the serializers in this module return ``services.clock.z_utc`` instants (a ``datetime``
    subclass that renders with an explicit ``Z``). Those are exactly right on an API response,
    where FastAPI encodes them, and a ``TypeError`` on the way into the card. So the card gets
    the same values as ISO-8601 strings — identical text, no second serializer, and the ``Z``
    survives. ``services/regulatory.py`` solves the same problem the same way with
    ``countdown_json``.
    """
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _audit(session: Session, *, actor: str, action: str, entity_type: str, entity_id: str, rationale: str, payload: dict) -> None:
    """One audit row per transition, flushed at once: the session factory is autoflush=False,
    so without the flush a same-transaction reader would not see the row until something else
    happened to flush."""
    session.add(
        AuditRow(
            operator_id=_operator_of(session, entity_type, entity_id),
            actor=actor,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            rationale=rationale[:2000],
            payload_json=json.dumps(payload, default=str)[:4000],
        )
    )
    session.flush()


def _operator_of(session: Session, entity_type: str, entity_id: str) -> str:
    """The operator that owns the audited row, read back rather than passed in.

    Cheap, and it means an audit row can never be filed under the wrong operator because a
    caller passed the active profile while editing a row it had resolved differently.
    """
    if entity_type == WINDOW_ENTITY_TYPE:
        row = session.get(MaintenanceWindowRow, entity_id)
        if row is not None:
            return row.operator_id
    if entity_type == SCHEDULE_ENTITY_TYPE:
        task = session.get(MaintenanceTaskRow, entity_id)
        if task is not None:
            plan = session.get(MaintenancePlanRow, task.plan_id)
            if plan is not None:
                return plan.operator_id
    if entity_type == "maintenance_plan":
        plan = session.get(MaintenancePlanRow, entity_id)
        if plan is not None:
            return plan.operator_id
    from noc_agents.config import get_settings  # local: config is already imported everywhere

    return get_settings().operator.operator_id


# ----------------------------------------------------------------------------- plans and tasks


def validate_plan(values: dict[str, Any]) -> None:
    """Refuse a plan that cannot be acted on. ``ValueError`` with the reason (route: 422).

    Four rules, each of which is a real failure mode rather than schema decoration:

    * exactly one of ``site_id`` / ``site_class`` — §7.5.1's "one of the two". Both set is
      ambiguous (does the class widen the plan or restrict it?); neither is a plan attached to
      nothing.
    * a known ``task_type`` — the ten in §7.5.1. A typo would become an eleventh kind of work
      that no interval, no standard and no engineer knows about.
    * an interval **or** ``consumption_driven`` — a plan that says neither can never produce a
      due date, so it would sit in the table looking like a maintenance regime and generating
      nothing. That is worse than no plan at all.
    * a non-empty ``standard_ref`` — see the module docstring. Every interval here is
      secondary-sourced; the citation travels with the policy.
    """
    site_id = (values.get("site_id") or "").strip()
    site_class = (values.get("site_class") or "").strip()
    if bool(site_id) == bool(site_class):
        raise ValueError("a plan needs exactly one of site_id or site_class (§7.5.1: 'one of the two')")
    task_type = (values.get("task_type") or "").strip().upper()
    if task_type not in MAINTENANCE_TASK_TYPES:
        raise ValueError(f"unknown task_type {values.get('task_type')!r}; expected one of {list(MAINTENANCE_TASK_TYPES)}")
    consumption = bool(values.get("consumption_driven")) or task_type in CONSUMPTION_DRIVEN_TASK_TYPES
    has_interval = int(values.get("interval_days") or 0) > 0 or int(values.get("interval_hours") or 0) > 0
    if not consumption and not has_interval:
        raise ValueError("a plan needs interval_days, interval_hours, or consumption_driven=true")
    if not (values.get("standard_ref") or "").strip():
        raise ValueError(
            "standard_ref is required: every interval shipped with this lane is secondary-sourced "
            f"(see {INTERVALS_YAML_PATH}), so the plan must name the practice it follows"
        )


def create_plan(session: Session, cfg: OperatorConfig, values: dict[str, Any], *, actor: str) -> MaintenancePlanRow:
    """Validate and insert one plan. Flushes; the caller commits."""
    validate_plan(values)
    task_type = (values["task_type"] or "").strip().upper()
    plan = MaintenancePlanRow(
        id=new_id(),
        operator_id=cfg.operator_id,
        site_id=(values.get("site_id") or "").strip() or None,
        site_class=((values.get("site_class") or "").strip() or None),
        task_type=task_type,
        interval_days=values.get("interval_days"),
        interval_hours=values.get("interval_hours"),
        consumption_driven=1 if (values.get("consumption_driven") or task_type in CONSUMPTION_DRIVEN_TASK_TYPES) else 0,
        owner_vendor_id=(values.get("owner_vendor_id") or "").strip() or None,
        standard_ref=(values["standard_ref"] or "").strip(),
        active=0 if values.get("active") is False else 1,
        created_at=utcnow(),
    )
    session.add(plan)
    session.flush()
    _audit(
        session,
        actor=actor,
        action="maintenance.plan_created",
        entity_type="maintenance_plan",
        entity_id=plan.id,
        rationale=plan.standard_ref,
        payload={"task_type": plan.task_type, "site_id": plan.site_id, "site_class": plan.site_class},
    )
    return plan


def owned_tasks(session: Session, operator_id: str):
    """``SELECT maintenance_tasks`` restricted to ``operator_id`` through the plan join.

    ``maintenance_tasks`` carries no ``operator_id`` (see ``db/models_maintenance.py``), so the
    operator clause lives in this join and every read of the table goes through here. There is
    deliberately no ``_owned(MaintenanceTaskRow)`` to reach for: it would silently return every
    operator's rows, because ``api.deps._owned`` falls back to ``model.operator_id`` and the
    column does not exist.
    """
    return (
        select(MaintenanceTaskRow)
        .join(MaintenancePlanRow, MaintenancePlanRow.id == MaintenanceTaskRow.plan_id)
        .where(MaintenancePlanRow.operator_id == operator_id)
    )


def last_completion(session: Session, plan_id: str, site_id: str) -> datetime | None:
    """When this plan was last completed at this site, or ``None`` if it never has been."""
    return session.scalar(
        select(func.max(MaintenanceTaskRow.completed_at)).where(
            MaintenanceTaskRow.plan_id == plan_id,
            MaintenanceTaskRow.site_id == site_id,
            MaintenanceTaskRow.status == TASK_DONE,
        )
    )


#: A task in one of these statuses is "live": the work is still expected to happen. One live
#: occurrence per (plan, site) at a time is the job's idempotency rule — see :func:`propose_task`.
LIVE_TASK_STATUSES: tuple[str, ...] = (TASK_PROPOSED, TASK_SCHEDULED, TASK_INVITED, TASK_IN_PROGRESS)


def propose_task(
    session: Session,
    plan: MaintenancePlanRow,
    site_id: str,
    cfg: OperatorConfig,
    *,
    due_at: datetime,
    basis: str = BASIS_LAST_COMPLETION,
    now: datetime | None = None,
) -> tuple[MaintenanceTaskRow, bool]:
    """Create the PROPOSED occurrence of ``plan`` at ``site_id``. ``(row, created)``.

    Idempotent by construction: one **live** task per ``(plan, site)`` at a time. The job runs
    hourly and a monthly generator exercise must not accumulate one task per tick, and the
    check is "is there already a live one?" rather than "is there one with this exact due
    date?" — the due date moves when a completion is backfilled, and an identity that moves is
    not an identity.

    Does not commit: the caller owns the transaction.
    """
    existing = session.scalar(
        select(MaintenanceTaskRow).where(
            MaintenanceTaskRow.plan_id == plan.id,
            MaintenanceTaskRow.site_id == site_id,
            MaintenanceTaskRow.status.in_(LIVE_TASK_STATUSES),
        )
    )
    if existing is not None:
        return existing, False
    row = MaintenanceTaskRow(
        id=new_id(),
        plan_id=plan.id,
        site_id=site_id,
        due_at=due_at,
        proposed_assignee_token=propose_assignee(plan, site_id, cfg),
        status=TASK_PROPOSED,
        created_at=now or utcnow(),
    )
    session.add(row)
    session.flush()
    _audit(
        session,
        actor=MAINTENANCE_RAISER,
        action="maintenance.task_proposed",
        entity_type=SCHEDULE_ENTITY_TYPE,
        entity_id=row.id,
        rationale=f"{plan.task_type} at {site_id} due {due_at.isoformat()} ({basis})",
        payload={"plan_id": plan.id, "basis": basis, "proposed_assignee_token": row.proposed_assignee_token},
    )
    return row, True


def tasks_for_window(session: Session, window: MaintenanceWindowRow) -> list[MaintenanceTaskRow]:
    """Every task booked into ``window``, oldest due first.

    Scoped by construction: the caller resolved ``window`` through ``_get_owned`` (operator
    clause applied, 404 and never 403), and ``window_id`` is what bounds this read — the same
    parent-scoping argument ``db/models_pir.py`` makes for ``pir_action_items``.
    """
    return list(
        session.scalars(
            select(MaintenanceTaskRow)
            .where(MaintenanceTaskRow.window_id == window.id)
            .order_by(MaintenanceTaskRow.due_at, MaintenanceTaskRow.id)
        ).all()
    )


def complete_task(
    session: Session,
    task: MaintenanceTaskRow,
    *,
    completed_by: str,
    outcome: str,
    evidence_note: str | None = None,
    completed_at: datetime | None = None,
    now: datetime | None = None,
) -> MaintenanceTaskRow:
    """Record that the work was done. ``completed_by`` and ``outcome`` are mandatory.

    A completion with no outcome is a tick in a box, and a tick in a box is what makes a
    maintenance regime look healthy while the generator has not started in a year. The
    completion also *is* the due-date arithmetic for the next occurrence
    (:func:`next_due` reads it), so it is the one field the whole lane depends on being true.

    A task that was already DONE or CANCELLED is a 409, not a silent overwrite: re-dating a
    completion moves every future due date at that site.
    """
    if task.status in (TASK_DONE, TASK_CANCELLED):
        raise MaintenanceStateError(f"task is already {task.status}")
    who = (completed_by or "").strip()
    if not who:
        raise ValueError("completed_by is required")
    text = (outcome or "").strip()
    if not text:
        raise ValueError("outcome is required: what was found and what was done")
    at = now or utcnow()
    stamped = completed_at or at
    if stamped > at + timedelta(minutes=1):
        raise ValueError("completed_at cannot be in the future")
    task.status = TASK_DONE
    task.completed_at = stamped
    task.completed_by = who
    task.outcome = text
    task.evidence_note = (evidence_note or "").strip() or None
    session.flush()
    _audit(
        session,
        actor=who,
        action="maintenance.task_completed",
        entity_type=SCHEDULE_ENTITY_TYPE,
        entity_id=task.id,
        rationale=text,
        payload={"site_id": task.site_id, "completed_at": stamped.isoformat(), "window_id": task.window_id},
    )
    return task


# ----------------------------------------------------------------------------------- windows


def make_uid(window_id: str, operator_id: str) -> str:
    """The RFC 5545 ``UID`` for a window. Allocated once, never changed.

    Domain-qualified so it is globally unique, which is what the standard asks for and what
    lets a later ``METHOD:REQUEST`` or ``METHOD:CANCEL`` land on the invite already in an
    engineer's calendar rather than creating a second entry beside it.
    """
    return f"maint-{window_id}@{operator_id}.noc.invalid"


def default_window_bounds(day_eat, cfg: OperatorConfig) -> tuple[datetime, datetime]:
    """The operator's default overnight window on ``day_eat``, as naive **UTC** bounds.

    §7.5.1 ships 00:00–05:00 EAT. The conversion happens here, once, and the two instants that
    come back are in the storage contract — which is what stops a 00:00 EAT window being
    written as 00:00 UTC and starting at 03:00 in the morning peak (§7.0.6, defect #41).
    An end time at or before the start is read as crossing midnight and rolls to the next day.
    """
    window_cfg = maintenance_config(cfg)["window"]
    start_h, start_m = _hhmm(window_cfg.get("default_start_eat"), DEFAULT_WINDOW["default_start_eat"])
    end_h, end_m = _hhmm(window_cfg.get("default_end_eat"), DEFAULT_WINDOW["default_end_eat"])
    tz = eat_tz()
    start_eat = datetime.combine(day_eat, time(start_h, start_m), tzinfo=tz)
    end_eat = datetime.combine(day_eat, time(end_h, end_m), tzinfo=tz)
    if end_eat <= start_eat:
        end_eat = end_eat + timedelta(days=1)
    return (
        start_eat.astimezone(timezone.utc).replace(tzinfo=None),
        end_eat.astimezone(timezone.utc).replace(tzinfo=None),
    )


def _hhmm(raw: Any, fallback: str) -> tuple[int, int]:
    text = str(raw or fallback).strip()
    try:
        hh, _, mm = text.partition(":")
        return int(hh), int(mm or 0)
    except ValueError:
        hh, _, mm = fallback.partition(":")
        return int(hh), int(mm or 0)


def create_window(
    session: Session,
    cfg: OperatorConfig,
    values: dict[str, Any],
    *,
    actor: str,
    now: datetime | None = None,
) -> MaintenanceWindowRow:
    """Create a PROPOSED window. Flushes; the caller commits. Schedules nothing.

    A new window is always PROPOSED, whatever the caller asks for. There is no route from
    "created" to "customers off air" that does not pass :func:`schedule_window`, and therefore
    :func:`window_approval_of`.

    Overlap with other PROPOSED windows is **allowed** and reported back to the caller as a
    warning rather than refused — see the module docstring, idea 4.
    """
    scope = (values.get("scope") or "").strip().upper()
    if scope not in WINDOW_SCOPES:
        raise ValueError(f"scope must be one of {list(WINDOW_SCOPES)}")
    scope_ref = (values.get("scope_ref") or "").strip()
    if not scope_ref:
        raise ValueError("scope_ref is required (a site id, a region code, or the operator id for NETWORK)")
    starts_at, ends_at = values.get("starts_at"), values.get("ends_at")
    if not isinstance(starts_at, datetime) or not isinstance(ends_at, datetime):
        raise ValueError("starts_at and ends_at are required")
    starts_at, ends_at = _naive_utc(starts_at), _naive_utc(ends_at)
    if ends_at <= starts_at:
        raise ValueError("ends_at must be after starts_at")

    window_id = new_id()
    row = MaintenanceWindowRow(
        id=window_id,
        operator_id=cfg.operator_id,
        scope=scope,
        scope_ref=scope_ref,
        starts_at=starts_at,
        ends_at=ends_at,
        uid=make_uid(window_id, cfg.operator_id),
        sequence=0,
        rrule=(values.get("rrule") or "").strip() or None,
        organizer=(values.get("organizer") or "").strip() or f"noc@{cfg.operator_id}",
        # A config path, never addresses: attendee e-mail is personal data (§7.5.6) and is
        # resolved at dispatch by ``services.notify.resolve_recipients``, which reads the
        # operator profile's ``notification_recipients`` register. The default follows that
        # register's ``<lane>.recipients.<AUDIENCE>`` convention, so an unfilled key REFUSES
        # the invite rather than falling back to the demo mailbox — the same fail-closed
        # behaviour the regulatory lane relies on.
        attendees_ref=(values.get("attendees_ref") or "").strip() or DEFAULT_ATTENDEES_REF,
        status=WINDOW_PROPOSED,
        ca_approval_ref=(values.get("ca_approval_ref") or "").strip() or None,
        incident_id=(values.get("incident_id") or "").strip() or None,
        rain_season_flag=0,
        created_at=now or utcnow(),
    )
    session.add(row)
    session.flush()
    verdict = rain_guard(session, row, cfg, now=now)
    row.rain_season_flag = verdict.flag
    session.flush()
    _audit(
        session,
        actor=actor,
        action="maintenance.window_created",
        entity_type=WINDOW_ENTITY_TYPE,
        entity_id=row.id,
        rationale=f"{scope} {scope_ref} {fmt_eat(starts_at, '%Y-%m-%d %H:%M')}–{fmt_eat(ends_at, '%H:%M')} EAT",
        payload={"uid": row.uid, "rain_guard": verdict.as_json()},
    )
    return row


def _naive_utc(value: datetime) -> datetime:
    """Store naive UTC (the DB contract); an offset-aware input is converted, never silently
    written as a local wall clock (§7.0.6, defect #41). Same rule as ``services/clock_events``."""
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def bump_sequence(session: Session, window: MaintenanceWindowRow, *, reason: str, actor: str) -> int:
    """Advance the RFC 5545 ``SEQUENCE`` and return the new value.

    Every reschedule and every cancellation goes through here. A client that receives a
    ``METHOD:REQUEST`` whose SEQUENCE has not advanced past the one it already holds is
    entitled by the standard to ignore it — so a moved window whose sequence did not move is a
    calendar entry that silently stays on the old night.
    """
    window.sequence = int(window.sequence or 0) + 1
    session.flush()
    _audit(
        session,
        actor=actor,
        action="maintenance.window_sequence_bumped",
        entity_type=WINDOW_ENTITY_TYPE,
        entity_id=window.id,
        rationale=reason,
        payload={"uid": window.uid, "sequence": window.sequence},
    )
    return window.sequence


def _announce(session: Session, window: MaintenanceWindowRow, change: str, extra: dict | None = None) -> None:
    """Tell the UI after commit — never before (§7.0.4)."""
    buffer_event(
        session,
        RealtimeEvent(
            type=WINDOW_EVENT,
            operator_id=window.operator_id,
            incident_id=window.incident_id,
            payload={
                "window_id": window.id,
                "uid": window.uid,
                "change": change,
                "status": window.status,
                "scope": window.scope,
                "scope_ref": window.scope_ref,
                "starts_at": z_utc(window.starts_at),
                "ends_at": z_utc(window.ends_at),
                **(extra or {}),
            },
        ),
    )


# --------------------------------------------------------------------------- the two gates


def _open_card(session: Session, task_id: str | None) -> HitlTaskRow | None:
    """A still-open card by id, if there is one. Prevents a second card for one subject."""
    if not task_id:
        return None
    card = session.scalar(_owned(HitlTaskRow).where(HitlTaskRow.id == task_id))
    if card is not None and card.status in ("PENDING", "CLAIMED"):
        return card
    return None


def _approval_of(
    session: Session,
    *,
    card_id: str | None,
    task_type: str,
    entity_type: str,
    entity_id: str,
    what: str,
    error: type[PermissionError],
) -> HitlTaskRow:
    """The APPROVED, human-resolved card that authorises ``entity_id`` — or refuse.

    Both gates share this function on purpose: two hand-written checkers would drift, and the
    one that drifted would be the one nobody was reading. Every condition below is a separate
    way an unapproved window or task could otherwise escape, and each is checked **against the
    database**, not against the mirror columns on the maintenance row (``approved_by`` there is
    for readers, not authority):

    * a card id at all — work nobody ever raised a card for cannot go ahead;
    * the card is reachable under the **active operator's** scope (``_owned``);
    * ``task_type`` matches exactly. This is the line that keeps the two gates apart: an
      APPROVED ``APPROVE_SCHEDULE`` — even one raised for a task inside this very window, even
      on the same anchor incident — is not an approval of the window, and vice versa;
    * ``entity_type``/``entity_id`` point at *this* subject, so one approved card cannot
      authorise a second, different window;
    * status is exactly ``APPROVED``;
    * ``resolved_by`` is a **named human**: non-empty, and not an ``agent:``/``policy:``
      principal. Autonomy policy approves broadcasts; it does not take customers off air;
    * the approver is not the raiser (§6.5) — automatic for an agent-raised card, and the
      rubber-stamp guard for a human-raised one.
    """
    if not card_id:
        raise error(f"{what}: no {task_type} card has been raised")
    card = session.scalar(_owned(HitlTaskRow).where(HitlTaskRow.id == card_id))
    if card is None:
        raise error(f"{what}: the approval card is not visible to this operator")
    if card.task_type != task_type:
        raise error(f"{what}: card {card.id} is a {card.task_type}, not a {task_type}")
    if card.entity_type != entity_type or card.entity_id != entity_id:
        raise error(f"{what}: card {card.id} approves {card.entity_type} {card.entity_id}, not this one")
    if card.status != "APPROVED":
        raise error(f"{what}: approval card is {card.status}, not APPROVED")
    approver = (card.resolved_by or "").strip()
    if not approver:
        raise error(f"{what}: the approval records no approver")
    if approver.startswith("agent:") or approver.startswith("policy:"):
        raise error(f"{what}: approved by {approver!r}; taking customers off air needs a named human")
    if is_raiser(card, approver):
        raise error(f"{what}: the person who raised this may not approve it (§6.5)")
    return card


def schedule_approval_of(session: Session, task: MaintenanceTaskRow) -> HitlTaskRow:
    """The APPROVED ``APPROVE_SCHEDULE`` card that authorises this task, or refuse.

    Signs off *the programme*: this work, at this site, on this date, with this assignee. It
    is **not** permission to take the site off air — that is :func:`window_approval_of`.
    """
    return _approval_of(
        session,
        card_id=task.hitl_task_id,
        task_type=SCHEDULE_TASK_TYPE,
        entity_type=SCHEDULE_ENTITY_TYPE,
        entity_id=task.id,
        what=f"maintenance task {task.id}",
        error=ScheduleNotApproved,
    )


def window_approval_of(session: Session, window: MaintenanceWindowRow) -> HitlTaskRow:
    """The APPROVED ``APPROVE_MAINTENANCE_WINDOW`` card that authorises this window, or refuse.

    THIS FUNCTION IS THE "NOT ON THE PLAN'S APPROVAL ALONE" GUARANTEE. Signing off a programme
    and signing off going ahead on the night are different acts by different people at
    different times with different information — the second one knows whether the CA reference
    came back, whether the customer notice went out, and what the sky looks like. A window
    whose every task carries an APPROVED ``APPROVE_SCHEDULE`` still fails here, because
    ``_approval_of`` compares ``task_type`` exactly.
    """
    return _approval_of(
        session,
        card_id=window.hitl_task_id,
        task_type=WINDOW_TASK_TYPE,
        entity_type=WINDOW_ENTITY_TYPE,
        entity_id=window.id,
        what=f"maintenance window {window.id}",
        error=WindowNotApproved,
    )


def request_schedule_approval(
    session: Session,
    task: MaintenanceTaskRow,
    plan: MaintenancePlanRow,
    cfg: OperatorConfig,
    *,
    actor: str = MAINTENANCE_RAISER,
    now: datetime | None = None,
) -> HitlTaskRow:
    """Raise ``APPROVE_SCHEDULE`` for one task (§7.5.2 payload: task, assignee_token, ics_preview).

    **Queues nothing**, for the reason ``services/regulatory.request_approval`` spells out: a
    HELD outbox row is one generic ``release_held`` away from PENDING, and ``release_held``
    releases by *incident* — so an unrelated broadcast approval on the anchor incident would
    promote a calendar invite to an engineer for work nobody approved. The right number of
    rows before approval is zero, and ``mark_invited`` is the only thing that creates one.
    """
    if task.status != TASK_PROPOSED:
        raise MaintenanceStateError(f"task is {task.status}; only a PROPOSED task needs a schedule approval")
    existing = _open_card(session, task.hitl_task_id)
    if existing is not None:
        return existing

    anchor = anchor_incident(session, cfg, incident_id=_window_incident(session, task))
    if anchor is None:
        raise NoAnchorIncident(
            "no incident to anchor an APPROVE_SCHEDULE card on (hitl_tasks.incident_id is NOT NULL); "
            "no card raised and nothing scheduled"
        )

    card = HitlTaskRow(
        id=new_id(),
        incident_id=anchor.id,
        task_type=SCHEDULE_TASK_TYPE,
        status="PENDING",
        entity_type=SCHEDULE_ENTITY_TYPE,
        entity_id=task.id,
        created_by=actor,
    )
    session.add(card)
    session.flush()
    task.hitl_task_id = card.id
    window = session.get(MaintenanceWindowRow, task.window_id) if task.window_id else None
    card.proposed_payload = _json_safe({
        "task": task_out(task, plan=plan),
        "assignee_token": task.proposed_assignee_token,
        "ics_preview": window_ics_fields(session, window, cfg) if window is not None else None,
        "standard_ref": plan.standard_ref,
        "anchor_incident_number": anchor.incident_number,
        # Said out loud, because the approver should not have to infer the scope of what they
        # are signing: this card is the programme, not the night.
        "warning": (
            "Approving this schedules the work and authorises a calendar invite. It is NOT "
            "approval to take the site off air — the window needs its own "
            "APPROVE_MAINTENANCE_WINDOW card."
        ),
    })
    session.flush()
    _audit(
        session,
        actor=actor,
        action="maintenance.schedule_approval_requested",
        entity_type=SCHEDULE_ENTITY_TYPE,
        entity_id=task.id,
        rationale=f"{plan.task_type} at {task.site_id} due {task.due_at.isoformat()}",
        payload={"card_id": card.id, "assignee_token": task.proposed_assignee_token, "anchor_incident_id": anchor.id},
    )
    return card


def _window_incident(session: Session, task: MaintenanceTaskRow) -> str | None:
    if not task.window_id:
        return None
    window = session.get(MaintenanceWindowRow, task.window_id)
    return window.incident_id if window is not None else None


def request_window_approval(
    session: Session,
    window: MaintenanceWindowRow,
    cfg: OperatorConfig,
    *,
    actor: str = MAINTENANCE_RAISER,
    now: datetime | None = None,
) -> HitlTaskRow:
    """Raise ``APPROVE_MAINTENANCE_WINDOW`` (§7.5.2 payload: window, rain_season_flag, ca_approval_ref).

    The rain guard runs **here**, so its verdict is on the card the approver reads, and it runs
    **again** in :func:`schedule_window` — a card raised a week ago was judged on a week-old
    forecast, and the question "is it about to rain on this job" has to be asked at the moment
    the answer matters.

    Queues nothing, for the same reason as :func:`request_schedule_approval`.
    """
    if window.status != WINDOW_PROPOSED:
        raise MaintenanceStateError(f"window is {window.status}; only a PROPOSED window needs approval")
    existing = _open_card(session, window.hitl_task_id)
    if existing is not None:
        return existing

    anchor = anchor_incident(session, cfg, incident_id=window.incident_id)
    if anchor is None:
        raise NoAnchorIncident(
            "no incident to anchor an APPROVE_MAINTENANCE_WINDOW card on (hitl_tasks.incident_id is "
            "NOT NULL); no card raised, so the window cannot be scheduled"
        )

    verdict = rain_guard(session, window, cfg, now=now)
    window.rain_season_flag = verdict.flag
    clashes = overlapping_windows(
        session,
        window.operator_id,
        scope=window.scope,
        scope_ref=window.scope_ref,
        starts_at=window.starts_at,
        ends_at=window.ends_at,
        statuses=(WINDOW_PROPOSED, WINDOW_SCHEDULED),
        exclude_id=window.id,
    )
    card = HitlTaskRow(
        id=new_id(),
        incident_id=anchor.id,
        task_type=WINDOW_TASK_TYPE,
        status="PENDING",
        entity_type=WINDOW_ENTITY_TYPE,
        entity_id=window.id,
        created_by=actor,
    )
    session.add(card)
    session.flush()
    window.hitl_task_id = card.id
    needs_ca = window.scope in ("REGION", "NETWORK")
    card.proposed_payload = _json_safe({
        "window": window_out(window),
        "rain_season_flag": verdict.flag,
        "rain_guard": verdict.as_json(),
        "ca_approval_ref": window.ca_approval_ref,
        "ca_approval_required": needs_ca,
        "customer_notice_sent_at": z_utc(window.customer_notice_sent_at),
        "notice_days_policy": notice_days(cfg),
        "tasks": [task_out(t) for t in tasks_for_window(session, window)],
        "overlapping_windows": [
            {"id": w.id, "status": w.status, "scope": w.scope, "scope_ref": w.scope_ref, "starts_at": z_utc(w.starts_at)}
            for w in clashes
        ],
        "anchor_incident_number": anchor.incident_number,
        "warning": (
            "Approving this authorises taking live customers off air between "
            f"{fmt_eat(window.starts_at, '%Y-%m-%d %H:%M')} and {fmt_eat(window.ends_at, '%H:%M')} EAT."
        ),
    })
    session.flush()
    _audit(
        session,
        actor=actor,
        action="maintenance.window_approval_requested",
        entity_type=WINDOW_ENTITY_TYPE,
        entity_id=window.id,
        rationale=verdict.reason,
        payload={
            "card_id": card.id,
            "rain_verdict": verdict.verdict,
            "ca_approval_required": needs_ca,
            "overlaps": [w.id for w in clashes],
        },
    )
    return card


# ----------------------------------------------------------------------- state transitions


def mark_scheduled(
    session: Session,
    task: MaintenanceTaskRow,
    *,
    actor: str,
    assignee_token: str | None = None,
) -> MaintenanceTaskRow:
    """PROPOSED → SCHEDULED, only behind an APPROVED ``APPROVE_SCHEDULE`` card (§7.5.3).

    ``assignee_token`` records who a human actually settled on, which may differ from
    ``proposed_assignee_token``; the proposal is kept beside it rather than overwritten, so
    "the system suggested X, a human chose Y" stays visible.
    """
    if task.status != TASK_PROPOSED:
        raise MaintenanceStateError(f"task is {task.status}; only a PROPOSED task can be scheduled")
    card = schedule_approval_of(session, task)  # <- the gate
    token = (assignee_token or task.proposed_assignee_token or "").strip() or None
    task.assignee_token = token
    task.status = TASK_SCHEDULED
    session.flush()
    _audit(
        session,
        actor=actor,
        action="maintenance.task_scheduled",
        entity_type=SCHEDULE_ENTITY_TYPE,
        entity_id=task.id,
        rationale=f"APPROVE_SCHEDULE {card.id} approved by {card.resolved_by}",
        payload={"assignee_token": token, "window_id": task.window_id},
    )
    return task


def mark_invited(session: Session, task: MaintenanceTaskRow, *, outbox_id: str, actor: str) -> MaintenanceTaskRow:
    """SCHEDULED → INVITED once the iMIP invite row exists (§7.5.3). Called by the ICS lane.

    Two preconditions, both re-checked here rather than trusted from the caller:

    * the task's own ``APPROVE_SCHEDULE`` is still APPROVED — a card revoked between scheduling
      and invitation must not produce an invite;
    * if the task sits in a window, that window is **SCHEDULED**. An invite is a message telling
      named engineers to be at a site at 01:00; sending it for a window nobody has approved is
      how the approval gate gets routed around socially rather than technically.
    """
    if task.status not in (TASK_SCHEDULED, TASK_INVITED):
        raise MaintenanceStateError(f"task is {task.status}; only a SCHEDULED task can be invited")
    schedule_approval_of(session, task)  # <- gate 1
    if task.window_id:
        window = session.get(MaintenanceWindowRow, task.window_id)
        if window is None or window.status != WINDOW_SCHEDULED:
            raise WindowNotApproved(
                f"task {task.id} sits in window {task.window_id}, which is "
                f"{window.status if window else 'missing'}; an invite may not go out for a window that "
                "has not been approved for the night"
            )
    task.status = TASK_INVITED
    session.flush()
    _audit(
        session,
        actor=actor,
        action="maintenance.task_invited",
        entity_type=SCHEDULE_ENTITY_TYPE,
        entity_id=task.id,
        rationale=f"iMIP invite queued as outbox {outbox_id}",
        payload={"outbox_id": outbox_id, "window_id": task.window_id},
    )
    return task


def schedule_window(
    session: Session,
    window: MaintenanceWindowRow,
    cfg: OperatorConfig,
    *,
    actor: str,
    override_rain: bool = False,
    override_reason: str = "",
    now: datetime | None = None,
) -> MaintenanceWindowRow:
    """PROPOSED → SCHEDULED: the moment this window becomes able to take customers off air.

    Five checks, in this order, because the order is what makes a refusal leave the row exactly
    as it was:

    1. the window is PROPOSED (409 otherwise);
    2. :func:`window_approval_of` — an APPROVED ``APPROVE_MAINTENANCE_WINDOW`` card pointing at
       *this* window, resolved by a named human who is not the raiser. **An approved
       APPROVE_SCHEDULE on every task inside the window does not satisfy this**, which is the
       property ``tests/unit/test_maintenance.py`` pins;
    3. CA licence Condition 9.1: a REGION or NETWORK window needs ``ca_approval_ref``
       (§7.5.6; decision D8 exempts SITE). Prior *written* Authority approval is a licence
       condition, not an internal preference, so it is checked in code rather than left on a
       checklist;
    4. the rain guard, **recomputed now** rather than read off the card. A fresh forecast that
       says storm refuses (:class:`RainGuardBlocked`) unless a named human passes
       ``override_rain`` with a reason, which is audited. See :func:`rain_guard` for why "no
       forecast" warns instead;
    5. no other **SCHEDULED** window covers an intersecting scope and period
       (:class:`WindowOverlapError`). Two crews on one site is how a planned outage becomes an
       unplanned one. The fix is to move or cancel one of them, so there is no override here.
    """
    if window.status != WINDOW_PROPOSED:
        raise MaintenanceStateError(f"window is {window.status}; only a PROPOSED window can be scheduled")

    card = window_approval_of(session, window)  # <- the gate that the two-gate rule lives in

    if window.scope in ("REGION", "NETWORK") and not (window.ca_approval_ref or "").strip():
        raise CaApprovalRequired(
            f"a {window.scope} window needs ca_approval_ref before it may be SCHEDULED: CA licence "
            "Condition 9.1 requires prior WRITTEN Authority approval for an intentional interruption "
            "(§7.5.6; UNVERIFIED for this operator's licence — confirm with Legal)"
        )

    verdict = rain_guard(session, window, cfg, now=now)
    # The flag is written only once every check has passed, so a refusal really does leave the
    # row as it was: a caller that catches the exception and commits anyway (a route that turns
    # it into a 409 does not, but a script might) must not persist half of this transition.
    if verdict.blocking and not override_rain:
        raise RainGuardBlocked(verdict.reason)
    if verdict.blocking and not (override_reason or "").strip():
        raise ValueError("overriding the rain guard requires a reason; it is recorded on the audit row")

    clashes = overlapping_windows(
        session,
        window.operator_id,
        scope=window.scope,
        scope_ref=window.scope_ref,
        starts_at=window.starts_at,
        ends_at=window.ends_at,
        statuses=(WINDOW_SCHEDULED,),
        exclude_id=window.id,
    )
    if clashes:
        raise WindowOverlapError(
            f"window {clashes[0].id} ({clashes[0].scope} {clashes[0].scope_ref}) is already SCHEDULED over an "
            "intersecting scope and period; move or cancel one of them"
        )

    window.status = WINDOW_SCHEDULED
    window.approved_by = card.resolved_by
    window.rain_season_flag = verdict.flag
    session.flush()
    _audit(
        session,
        actor=actor,
        action="maintenance.window_scheduled",
        entity_type=WINDOW_ENTITY_TYPE,
        entity_id=window.id,
        rationale=f"APPROVE_MAINTENANCE_WINDOW {card.id} approved by {card.resolved_by}; rain {verdict.verdict}",
        payload={
            "approved_by": card.resolved_by,
            "ca_approval_ref": window.ca_approval_ref,
            "rain_guard": verdict.as_json(),
            "rain_override": bool(verdict.blocking and override_rain),
            "rain_override_reason": (override_reason or "").strip() or None,
        },
    )
    _announce(session, window, "scheduled", {"approved_by": card.resolved_by, "rain_season_flag": verdict.flag})
    return window


def reschedule_window(
    session: Session,
    window: MaintenanceWindowRow,
    *,
    starts_at: datetime,
    ends_at: datetime,
    actor: str,
    reason: str,
) -> MaintenanceWindowRow:
    """Move a window — and **revoke its approval** in the same act.

    "Approve going ahead on Tuesday night" is not approval to go ahead on Thursday: the
    approver weighed a specific night's notice period, weather and crew availability. So a move
    sends the window back to PROPOSED, clears ``hitl_task_id`` and ``approved_by``, and bumps
    ``SEQUENCE`` (the calendar clients need the advance, and the ICS lane sends a fresh
    ``METHOD:REQUEST``). A new ``APPROVE_MAINTENANCE_WINDOW`` card is then required.

    Any task already INVITED for the old night goes back to SCHEDULED: the invitation it is
    recording is now for a time that no longer exists.
    """
    if window.status in (WINDOW_CANCELLED, WINDOW_COMPLETED):
        raise MaintenanceStateError(f"window is {window.status} and cannot be moved")
    starts_at, ends_at = _naive_utc(starts_at), _naive_utc(ends_at)
    if ends_at <= starts_at:
        raise ValueError("ends_at must be after starts_at")
    text = (reason or "").strip()
    if not text:
        raise ValueError("a reason is required to move a window; it goes on the audit row and the invite")
    was = (window.starts_at, window.ends_at, window.status)
    window.starts_at, window.ends_at = starts_at, ends_at
    window.status = WINDOW_PROPOSED
    window.hitl_task_id = None
    window.approved_by = None
    for task in tasks_for_window(session, window):
        if task.status == TASK_INVITED:
            task.status = TASK_SCHEDULED
    bump_sequence(session, window, reason=f"rescheduled: {text}", actor=actor)
    _audit(
        session,
        actor=actor,
        action="maintenance.window_rescheduled",
        entity_type=WINDOW_ENTITY_TYPE,
        entity_id=window.id,
        rationale=text,
        payload={
            "from": {"starts_at": was[0].isoformat(), "ends_at": was[1].isoformat(), "status": was[2]},
            "to": {"starts_at": starts_at.isoformat(), "ends_at": ends_at.isoformat(), "status": window.status},
            "approval_revoked": was[2] == WINDOW_SCHEDULED,
        },
    )
    _announce(session, window, "rescheduled", {"sequence": window.sequence})
    return window


def cancel_window(session: Session, window: MaintenanceWindowRow, *, actor: str, reason: str) -> MaintenanceWindowRow:
    """Cancel a window and everything booked into it (§7.5.2, §7.5.3 ``METHOD:CANCEL``).

    ``SEQUENCE`` advances here too: a CANCEL whose sequence has not moved past the REQUEST the
    client already holds can be ignored, which would leave engineers with a live calendar entry
    for work that is not happening.
    """
    if window.status in (WINDOW_CANCELLED, WINDOW_COMPLETED):
        raise MaintenanceStateError(f"window is already {window.status}")
    text = (reason or "").strip()
    if not text:
        raise ValueError("a reason is required to cancel a window")
    window.status = WINDOW_CANCELLED
    cancelled = 0
    for task in tasks_for_window(session, window):
        if task.status in LIVE_TASK_STATUSES:
            task.status = TASK_CANCELLED
            cancelled += 1
    bump_sequence(session, window, reason=f"cancelled: {text}", actor=actor)
    _audit(
        session,
        actor=actor,
        action="maintenance.window_cancelled",
        entity_type=WINDOW_ENTITY_TYPE,
        entity_id=window.id,
        rationale=text,
        payload={"tasks_cancelled": cancelled, "sequence": window.sequence},
    )
    _announce(session, window, "cancelled", {"tasks_cancelled": cancelled})
    return window


def complete_window(
    session: Session,
    window: MaintenanceWindowRow,
    *,
    actor: str,
    now: datetime | None = None,
) -> MaintenanceWindowRow:
    """SCHEDULED → COMPLETED once the night is over. Tasks still live in it become MISSED.

    MISSED is a real status and not an absence, for the reason §7.7 gives about ``NOT_REQUIRED``
    reviews: "the window passed and the battery check did not happen" has to be distinguishable
    from "nobody looked". It is also the number that makes a maintenance regime honest.
    """
    if window.status != WINDOW_SCHEDULED:
        raise MaintenanceStateError(f"window is {window.status}; only a SCHEDULED window can be completed")
    missed = 0
    for task in tasks_for_window(session, window):
        if task.status in LIVE_TASK_STATUSES:
            task.status = TASK_MISSED
            missed += 1
    window.status = WINDOW_COMPLETED
    session.flush()
    _audit(
        session,
        actor=actor,
        action="maintenance.window_completed",
        entity_type=WINDOW_ENTITY_TYPE,
        entity_id=window.id,
        rationale=f"window ended {fmt_eat(window.ends_at, '%Y-%m-%d %H:%M')} EAT",
        payload={"tasks_missed": missed},
    )
    _announce(session, window, "completed", {"tasks_missed": missed})
    return window


# ------------------------------------------------- what the other lanes read (planned cover)


def scheduled_windows_for_site(
    session: Session,
    operator_id: str,
    site_id: str,
    *,
    start: datetime,
    end: datetime,
) -> list[MaintenanceWindowRow]:
    """Every SCHEDULED window covering ``site_id`` that intersects ``[start, end)``.

    SCHEDULED only — a PROPOSED window is a plan, not a planned outage, and excluding its
    minutes from an availability figure would credit a vendor for a night nobody approved.
    """
    rows = session.scalars(
        select(MaintenanceWindowRow)
        .where(
            MaintenanceWindowRow.operator_id == operator_id,
            MaintenanceWindowRow.status == WINDOW_SCHEDULED,
            MaintenanceWindowRow.starts_at < end,
            MaintenanceWindowRow.ends_at > start,
        )
        .order_by(MaintenanceWindowRow.starts_at)
    ).all()
    return [w for w in rows if scopes_conflict(w.scope, w.scope_ref, "SITE", site_id)]


def is_planned(
    session: Session,
    operator_id: str,
    site_id: str,
    at: datetime,
) -> MaintenanceWindowRow | None:
    """The SCHEDULED window that has ``site_id`` off air at ``at``, or ``None``.

    The read §7.5.3 asks ENRICH to make: an alarm raised inside an approved window is tagged
    ``planned_maintenance=1`` rather than treated as a fault. Returns the *window*, not a
    boolean, because everything downstream (the tag, the availability exclusion, the stop-clock
    proposal) needs to say **which** window, and "planned, but we cannot tell you why" is not
    an answer anybody can act on.

    Returns ``None`` when the lane is off, so a flag-off system behaves exactly as today.
    """
    if not maintenance_enabled():
        return None
    windows = scheduled_windows_for_site(session, operator_id, site_id, start=at, end=at + timedelta(microseconds=1))
    return windows[0] if windows else None


def planned_maintenance_tag(session: Session, inc: IncidentRow, *, now: datetime | None = None) -> dict[str, Any]:
    """``{planned_maintenance, window_id, window_uid, …}`` for one incident (§7.5.3, ENRICH).

    Evaluated at the incident's **failure time**, not at now: an alarm that arrived during an
    approved window is planned work even if somebody looks at the ticket the next afternoon.
    Always returns a dict, with ``planned_maintenance: 0`` when the lane is off or no window
    covers the site — so a caller never has to branch on ``None``.
    """
    at = inc.failure_time or inc.outage_start_at or inc.created_at or (now or utcnow())
    window = is_planned(session, inc.operator_id, inc.site_id or "", at)
    if window is None:
        return {"planned_maintenance": 0, "window_id": None, "window_uid": None}
    return {
        "planned_maintenance": 1,
        "window_id": window.id,
        "window_uid": window.uid,
        "window_scope": f"{window.scope} {window.scope_ref}",
        "window_starts_at": z_utc(window.starts_at),
        "window_ends_at": z_utc(window.ends_at),
        "evaluated_at": z_utc(at),
    }


@dataclass(frozen=True)
class _WindowInterval:
    """Adapter so a window's period can go through ``clock_events.effective_intervals``.

    Deliberate reuse rather than a second union-and-clip implementation. That function already
    guarantees the property this needs — the total is the length of the **union**, so two
    windows covering the same hour exclude that hour once — and it is pinned by
    ``tests/unit/test_clock_events.py``. Writing the merge again here would mean two
    implementations of the one piece of arithmetic that decides what a vendor is paid, and the
    second one would be the one nobody tested. ``reversed_at`` is always ``None``: a window has
    no equivalent of a withdrawn stop clock (a withdrawn window is CANCELLED, and a CANCELLED
    window is never in this list in the first place).
    """

    started_at: datetime
    ended_at: datetime | None
    reversed_at: datetime | None = None


def planned_minutes(
    session: Session,
    operator_id: str,
    site_id: str,
    *,
    period_start: datetime,
    period_end: datetime,
) -> int:
    """Whole minutes ``site_id`` was inside an approved window during the period.

    What §7.6.2's availability KPI subtracts ("planned windows excluded"). Overlapping windows
    contribute their union, never their sum — see :class:`_WindowInterval`. Truncated, not
    rounded, matching ``clock_events.deducted_minutes``: a partial minute of planned outage is
    not credited.
    """
    windows = scheduled_windows_for_site(session, operator_id, site_id, start=period_start, end=period_end)
    intervals: list[Interval] = effective_intervals(
        [_WindowInterval(w.starts_at, w.ends_at) for w in windows],
        window_start=period_start,
        window_end=period_end,
    )
    return int(sum((iv.duration for iv in intervals), timedelta()).total_seconds() // 60)


# ------------------------------------------------------- the planned-window stop-clock PROPOSAL


def stop_clock_proposal(
    session: Session,
    inc: IncidentRow,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """A ``PLANNED_MAINTENANCE`` stop-clock **proposal** for ``inc``, or ``None``.

    READ THIS BEFORE CHANGING ANYTHING HERE.

    Stop-clock minutes are deducted from a vendor's adjusted MTTR and therefore from what the
    operator can claim under the contract (§7.6.2). That is a commercial act with a
    counterparty, and §7.6 is built on the premise that such an act is made by a named human
    who can defend it — which is why ``services.clock_events.open_clock_event`` demands an
    ``opened_by``, an ``opened_role`` from the operations floor, a non-empty reason, and
    records ``opened_at - started_at`` as a *discipline counter against the operator*.

    So this function:

    * writes **nothing** — no ``incident_clock_events`` row, no audit row, no buffered event;
    * calls nothing that writes;
    * returns a dict that names the window, the interval and the code, and says
      ``requires_human: True`` and how to accept it.

    Accepting it is the existing, unchanged, role-gated route
    ``POST /api/v1/incidents/{id}/clock`` in ``api/routers/clocks.py``. There is deliberately no
    "accept" helper in this module: a convenience wrapper that opened the event would be one
    refactor away from being called by a job, and the whole point is that nothing but a person
    can start this clock. ``test_a_planned_window_proposal_never_opens_a_stop_clock_event``
    asserts the clock-events table is still empty afterwards.

    Returns ``None`` when the lane is off, when the incident has no site, or when no SCHEDULED
    window covered that site at the failure time. The proposed interval is the **intersection**
    of the window with the outage, clipped at the restore time when the incident is restored:
    proposing the whole window would deduct minutes the site was already back up.
    """
    if not maintenance_enabled():
        return None
    site_id = (inc.site_id or "").strip()
    if not site_id:
        return None
    now = now or utcnow()
    failure = inc.failure_time or inc.outage_start_at or inc.created_at or now
    outage_end = inc.restored_at or now
    windows = scheduled_windows_for_site(session, inc.operator_id, site_id, start=failure, end=max(outage_end, failure + timedelta(microseconds=1)))
    if not windows:
        return None
    window = windows[0]
    start = max(window.starts_at, failure)
    end = min(window.ends_at, outage_end)
    if end <= start:
        return None
    return {
        "scc_code": PLANNED_MAINTENANCE_SCC,
        "status": "PROPOSED",
        "requires_human": True,
        "opened": False,
        "window_id": window.id,
        "window_uid": window.uid,
        "window_scope": f"{window.scope} {window.scope_ref}",
        "proposed_started_at": z_utc(start),
        "proposed_ended_at": z_utc(end),
        "proposed_minutes": int((end - start).total_seconds() // 60),
        "reason": (
            f"{inc.site_id} was inside approved maintenance window {window.uid} "
            f"({fmt_eat(window.starts_at, '%Y-%m-%d %H:%M')}–{fmt_eat(window.ends_at, '%H:%M')} EAT, "
            f"approved by {window.approved_by or 'unrecorded'})"
        ),
        "accept_with": {
            "method": "POST",
            "path": f"/api/v1/incidents/{inc.id}/clock",
            "body": {"scc_code": PLANNED_MAINTENANCE_SCC, "started_at": z_utc(start), "reason": "<why, in your words>"},
            "note": (
                "A stop clock deducts minutes from a vendor's SLA figure. This system proposes; "
                "a named person on an operations role opens it, and owns it."
            ),
        },
    }


# ------------------------------------------------------------------- the seam for the ICS lane


def window_ics_fields(session: Session, window: MaintenanceWindowRow | None, cfg: OperatorConfig) -> dict[str, Any] | None:
    """Everything the iMIP/iCalendar lane needs to build one VEVENT — and nothing it does not.

    **Which seam to use.** ``services/ics.py`` reads a ``MaintenanceWindowRow`` directly with
    ``invite_from_window(row, attendees=[...])`` — duck-typed on ``id``/``uid``/``starts_at``/
    ``ends_at``/``scope_ref``/``organizer``/``sequence``/``status``/``rrule``, all of which this
    lane's row carries. **That is the seam for building the actual VEVENT**, and it is the right
    one: it keeps the bytes' owner in one module. This function is for the *human-readable
    preview* §7.5.2 puts on the ``APPROVE_SCHEDULE`` card (``ics_preview``), where the approver
    needs EAT alongside UTC, the task list and the CA reference — and where pulling in
    ``icalendar`` to render a card would make an optional dependency mandatory. Two seams, two
    jobs; neither is a reimplementation of the other.

    A **data** seam, on purpose: this module owns the window's identity and lifecycle, that
    module owns RFC 5545 and RFC 6047. Neither needs to import the other's internals, and the
    ICS lane can be tested against this dict without a maintenance database.

    * ``uid``/``sequence`` — the calendar identity. ``sequence`` is already correct for the
      current state; the ICS lane must not bump it, it calls :func:`bump_sequence`.
    * ``dtstart``/``dtend`` are aware UTC instants. ``tzid`` is ``Africa/Nairobi`` because the
      engineer's calendar should render EAT (§7.5.3); rendering them as ``TZID=Africa/Nairobi``
      local times is the ICS lane's job and it has the zone name here to do it.
    * ``method`` is derived from status: a CANCELLED window is a ``METHOD:CANCEL``, anything
      else a ``METHOD:REQUEST``. Derived rather than passed so the two cannot disagree.
    * ``attendees_ref`` is a config path. Attendee addresses are personal data (§7.5.6) and are
      resolved at dispatch, never stored on this row.
    """
    if window is None:
        return None
    tasks = tasks_for_window(session, window)
    kinds = sorted({session.get(MaintenancePlanRow, t.plan_id).task_type for t in tasks if session.get(MaintenancePlanRow, t.plan_id)})
    summary = f"Planned maintenance — {window.scope} {window.scope_ref}"
    if kinds:
        summary = f"{summary} ({', '.join(kinds)})"
    return {
        "uid": window.uid,
        "sequence": int(window.sequence or 0),
        "method": "CANCEL" if window.status == WINDOW_CANCELLED else "REQUEST",
        "status": window.status,
        "dtstart": z_utc(window.starts_at),
        "dtend": z_utc(window.ends_at),
        "tzid": cfg.timezone or "Africa/Nairobi",
        "dtstart_eat": fmt_eat(window.starts_at, "%Y-%m-%d %H:%M"),
        "dtend_eat": fmt_eat(window.ends_at, "%Y-%m-%d %H:%M"),
        "rrule": window.rrule,
        "organizer": window.organizer,
        "attendees_ref": window.attendees_ref,
        "summary": summary,
        "location": window.scope_ref,
        "description": (
            f"Planned maintenance window {window.uid}. "
            f"Tasks: {', '.join(f'{t.site_id}/{t.id[:8]}' for t in tasks) or 'none booked'}. "
            f"CA approval ref: {window.ca_approval_ref or 'not required (SITE scope)'}. "
            f"Rain-season flag: {int(window.rain_season_flag or 0)}."
        ),
        "task_ids": [t.id for t in tasks],
    }


# ----------------------------------------------------------------------------------- the jobs

PLAN_DUE_JOB_NAME = "maintenance_plan_due"
PLAN_DUE_INTERVAL_S = 3600  # hourly: a due date that moves by an hour changes nothing
WINDOW_SWEEP_JOB_NAME = "maintenance_window_sweep"
WINDOW_SWEEP_INTERVAL_S = 300  # every 5 min, so a window closes promptly after its end
AGENT = "MaintenancePlanningAgent"
GRAPH_NAME = "maintenance"

#: Ceiling per tick, so the first run against 6 000 sites cannot turn one job into a
#: thousand-row transaction. The rest are picked up an hour later.
PLAN_DUE_TASK_LIMIT = 100
#: Ceiling on cards raised per tick. A hundred approval cards landing in the HITL inbox at once
#: is the same as none: nobody reads an inbox that just became a wall.
PLAN_DUE_CARD_LIMIT = 25


def plan_due(session: Session, settings: "AppSettings", *, now: datetime | None = None) -> JobResult:
    """``jobs/maintenance.plan_due`` (§7.5.3) — propose the tasks that are coming due.

    Computes each active plan's next occurrence per site from the last recorded completion,
    proposes an assignee from the region roster, and raises ``APPROVE_SCHEDULE``. Only work
    coming due inside the notice horizon is proposed: a battery check due in eight months is
    not a card anybody should be looking at today.

    Does not commit — the scheduler's runner commits, which is also when the buffered events
    reach the UI.
    """
    if not maintenance_enabled():
        # Re-checked here and not only on the job card, so a card wired into the loop with the
        # flag unset is inert rather than merely un-scheduled.
        return JobResult(summary="MAINTENANCE_ENABLED=false — no tasks proposed", rationale="feature flag off")

    now = now or utcnow()
    cfg = settings.operator
    horizon = now + timedelta(days=notice_days(cfg))
    plans = session.scalars(
        select(MaintenancePlanRow).where(MaintenancePlanRow.operator_id == cfg.operator_id, MaintenancePlanRow.active == 1)
    ).all()

    proposed: list[str] = []
    cards = 0
    skipped_consumption = 0
    not_yet = 0
    no_anchor = 0
    for plan in plans:
        for site_id in plan_sites(plan, cfg):
            if len(proposed) >= PLAN_DUE_TASK_LIMIT:
                break
            due_at, basis = next_due(plan, last_completed_at=last_completion(session, plan.id, site_id), now=now)
            if due_at is None:
                skipped_consumption += 1
                continue
            if due_at > horizon:
                not_yet += 1
                continue
            task, created = propose_task(session, plan, site_id, cfg, due_at=due_at, basis=basis, now=now)
            if not created:
                continue
            proposed.append(f"{plan.task_type}@{site_id}")
            if cards < PLAN_DUE_CARD_LIMIT:
                try:
                    request_schedule_approval(session, task, plan, cfg, now=now)
                    cards += 1
                except NoAnchorIncident:
                    # Fail closed and stay visible: the task exists and is PROPOSED, which is
                    # inert, and the count below says why no card was raised.
                    no_anchor += 1
    return JobResult(
        summary=f"proposed={len(proposed)} cards={cards} not_due_yet={not_yet} consumption_driven={skipped_consumption}",
        rationale=(
            "; ".join(proposed[:20]) if proposed else "no plan came due inside the notice horizon"
        )
        + (f" (no anchor incident for {no_anchor} card(s))" if no_anchor else ""),
        tools=({"name": "maintenance_plan_due", "ok": True, "latency_ms": 0},),
    )


def window_sweep(session: Session, settings: "AppSettings", *, now: datetime | None = None) -> JobResult:
    """``jobs/maintenance.window_sweep`` — close finished windows, and make failures visible.

    Three things, and two of them are deliberately *not* tidying up:

    * a SCHEDULED window whose ``ends_at`` has passed becomes COMPLETED, and any task still
      live inside it becomes MISSED (see :func:`complete_window`);
    * a PROPOSED window whose start time has passed is **left alone** and counted as
      ``lapsed``. A window nobody approved in time is a planning failure, and auto-cancelling
      it would erase the evidence of it on the next tick;
    * a window starting inside the notice period with no ``customer_notice_sent_at`` is counted
      as ``notice_due``. Counted, never sent: a customer notice is a broadcast and broadcasts go
      through §6's own approval path, not through a sweep job.
    """
    if not maintenance_enabled():
        return JobResult(summary="MAINTENANCE_ENABLED=false — no windows swept", rationale="feature flag off")

    now = now or utcnow()
    cfg = settings.operator
    horizon = now + timedelta(days=notice_days(cfg))
    rows = session.scalars(
        select(MaintenanceWindowRow).where(
            MaintenanceWindowRow.operator_id == cfg.operator_id,
            MaintenanceWindowRow.status.in_((WINDOW_PROPOSED, WINDOW_SCHEDULED)),
        )
    ).all()
    completed = lapsed = notice_due = 0
    for window in rows:
        if window.status == WINDOW_SCHEDULED and window.ends_at <= now:
            complete_window(session, window, actor=MAINTENANCE_RAISER, now=now)
            completed += 1
            continue
        if window.status == WINDOW_PROPOSED and window.starts_at <= now:
            lapsed += 1
            continue
        if window.starts_at <= horizon and window.customer_notice_sent_at is None:
            notice_due += 1
    return JobResult(
        summary=f"completed={completed} lapsed_unapproved={lapsed} customer_notice_due={notice_due}",
        rationale=(
            f"{lapsed} window(s) reached their start time still PROPOSED — left as they are so the "
            "planning failure stays visible" if lapsed else "no unapproved window has lapsed"
        ),
        tools=({"name": "maintenance_window_sweep", "ok": True, "latency_ms": 0},),
    )


def plan_due_job(session: Session, settings: "AppSettings") -> JobResult:
    """JobCard entry point for :func:`plan_due` (the card's ``fn`` signature takes no kwargs)."""
    return plan_due(session, settings)


def window_sweep_job(session: Session, settings: "AppSettings") -> JobResult:
    """JobCard entry point for :func:`window_sweep`."""
    return window_sweep(session, settings)


#: The scheduler cards (§4.4 roster). NOT registered in ``scheduler/loop.SCHEDULED_JOBS`` by
#: this lane — that file belongs to integration; adding these two names to the tuple is the
#: whole change. ``default_enabled=False`` so ``/scheduler/status`` reports them as off while
#: ``MAINTENANCE_ENABLED`` is unset, rather than claiming they are enabled and producing nothing.
MAINTENANCE_PLAN_DUE_JOB = JobCard(
    PLAN_DUE_JOB_NAME,
    PLAN_DUE_INTERVAL_S,
    plan_due_job,
    MAINTENANCE_ENABLED_ENV,
    AGENT,
    GRAPH_NAME,
    max_seconds=120,
    default_enabled=False,
)

MAINTENANCE_WINDOW_SWEEP_JOB = JobCard(
    WINDOW_SWEEP_JOB_NAME,
    WINDOW_SWEEP_INTERVAL_S,
    window_sweep_job,
    MAINTENANCE_ENABLED_ENV,
    AGENT,
    GRAPH_NAME,
    max_seconds=60,
    default_enabled=False,
)


# ---------------------------------------------------------------------------------- serializing


def plan_out(plan: MaintenancePlanRow) -> dict[str, Any]:
    """One plan as the API returns it. Timestamps carry an explicit ``Z`` (§7.0.6)."""
    return {
        "id": plan.id,
        "site_id": plan.site_id,
        "site_class": plan.site_class,
        "task_type": plan.task_type,
        "interval_days": plan.interval_days,
        "interval_hours": plan.interval_hours,
        "consumption_driven": int(plan.consumption_driven or 0),
        "owner_vendor_id": plan.owner_vendor_id,
        "standard_ref": plan.standard_ref,
        # Said on every plan, not buried in a docs page: the interval is a secondary-sourced
        # default for a human to review, never a verified reading of the standard.
        "standard_note": DEFAULT_INTERVALS.get(plan.task_type, {}).get("note"),
        "active": int(plan.active or 0),
        "created_at": z_utc(plan.created_at),
    }


def task_out(task: MaintenanceTaskRow, *, plan: MaintenancePlanRow | None = None) -> dict[str, Any]:
    """One task as the API returns it."""
    body = {
        "id": task.id,
        "plan_id": task.plan_id,
        "site_id": task.site_id,
        "due_at": z_utc(task.due_at),
        "due_at_eat": fmt_eat(task.due_at, "%Y-%m-%d %H:%M"),
        "window_id": task.window_id,
        "proposed_assignee_token": task.proposed_assignee_token,
        "assignee_token": task.assignee_token,
        "hitl_task_id": task.hitl_task_id,
        "status": task.status,
        "completed_at": z_utc(task.completed_at),
        "completed_by": task.completed_by,
        "evidence_note": task.evidence_note,
        "outcome": task.outcome,
        "created_at": z_utc(task.created_at),
    }
    if plan is not None:
        body["task_type"] = plan.task_type
        body["standard_ref"] = plan.standard_ref
    return body


def window_out(window: MaintenanceWindowRow) -> dict[str, Any]:
    """One window as the API returns it. Both UTC and EAT, because the storage contract is UTC
    and every human on this floor reads EAT — showing one without the other is how a window
    gets discussed at the wrong time of night."""
    return {
        "id": window.id,
        "scope": window.scope,
        "scope_ref": window.scope_ref,
        "starts_at": z_utc(window.starts_at),
        "ends_at": z_utc(window.ends_at),
        "starts_at_eat": fmt_eat(window.starts_at, "%Y-%m-%d %H:%M"),
        "ends_at_eat": fmt_eat(window.ends_at, "%Y-%m-%d %H:%M"),
        "uid": window.uid,
        "sequence": int(window.sequence or 0),
        "rrule": window.rrule,
        "organizer": window.organizer,
        "attendees_ref": window.attendees_ref,
        "status": window.status,
        "ca_approval_ref": window.ca_approval_ref,
        "ca_approval_required": window.scope in ("REGION", "NETWORK"),
        "customer_notice_sent_at": z_utc(window.customer_notice_sent_at),
        "approved_by": window.approved_by,
        "hitl_task_id": window.hitl_task_id,
        "incident_id": window.incident_id,
        "rain_season_flag": int(window.rain_season_flag or 0),
        "created_at": z_utc(window.created_at),
    }

