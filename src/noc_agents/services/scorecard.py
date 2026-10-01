"""Vendor scorecards: the six KPIs, the bands, the two gates and the job (spec §7.6.2,
§7.6.4, §5.3.17) -- Phase 4 Lane 4A, step 2. Behind ``SCORECARDS_ENABLED`` (default OFF).

These numbers decide vendor money. Three properties matter more than anything else here,
and each is pinned by a test that attacks it:

* **Reproducible.** A card records ``sla_terms_version`` and ``computed_by_run_id``; every
  line carries a ``formula`` with its own operands and a ``yaml_path`` that resolves to the
  term it was measured against. All arithmetic is exact (``fractions.Fraction`` over whole
  seconds) and rounded once, half-up, at the very end -- there is no float accumulation to
  make two machines disagree in the last digit. Nothing reads the wall clock to produce a
  number: an incident still open is evaluated AT THE PERIOD END, not at "now", so computing
  the same period twice over the same rows gives the same lines.
  (``test_scorecard_numbers.py``: the golden fixture, worked by hand in its docstring.)
* **WITHHELD and SHADOW hold in three layers.** This service refuses to publish past a
  failed data-quality gate or a vendor's first period without a named shadow reviewer; the
  mapper guards in ``db/models_scorecards.py`` refuse the same through ANY ORM writer
  (including the same-write flip of ``shadow_required`` or a ``dq_*`` operand, and a card
  inserted already released); the table's CHECKs are the last line for Core/raw SQL. What
  none of them can promise is a person with raw write access to the database file -- see
  that module's docstring. (``test_scorecard_gates.py`` enumerates the bypasses and names
  the ones it can only document.)
* **Nothing publishes itself.** The job computes DRAFT / SHADOW / WITHHELD and stops. A
  named human with a role and a reason moves a card on; a credit is never more than
  ``PROPOSED``; terms that are placeholders say "defaults, not contract" on the card.

WHAT THIS MODULE DELIBERATELY DOES NOT DO (this step is computation only)
    It opens no dispute, drafts no vendor notice and builds no QBR workbook -- those depend
    on HITL task ownership and arrive next. The seams they need are here: the dispute
    columns exist on the line, ``band_for`` re-bands an adjusted value, ``finalise_scorecard``
    already refuses while a line's ``dispute_status`` is OPEN, and ``line.evidence`` holds the
    incident list the vendor pack will render.

THE FIREWALL
    Blameless review content must never reach a vendor's number (§7.7, §5.3.17), and
    relationship complaints must not either (§7.8.6). This module imports neither lane and
    queries neither lane's tables. ``test_pir.py`` and ``test_complaints.py`` prove the
    static half (grep of the tree, import discovery on this file's NAME); the runtime half
    for THIS lane is ``test_scorecard_gates.py``, which captures every SQL statement of
    ``compute_period``, ``compute_on_request`` and ``close_periods`` with the lane on and a
    review row present. (``test_pir.py``'s own runtime capture runs the registered jobs with
    only ``PIR_ENABLED`` set, so it will cover this job's flag-off branch once the card is
    registered, not its computation.) Inputs are exactly: ``incidents``, ``work_notes``
    (timestamps and author roles only -- never a note body), ``incident_clock_events``,
    ``vendors``, ``maintenance_windows`` (through ``services.maintenance.planned_minutes``),
    the terms YAML and the operator profile.

HOW §7.6.2 WAS READ (every place the spec could reasonably have meant something else)
=====================================================================================
 1. **The period is a calendar month in the OPERATOR'S timezone** (EAT), converted to naive
    UTC for the queries. 23:30 EAT on 30 September is a September outage to everyone who
    will read the card. (Alternative: a UTC month -- three hours of every month would land
    on the wrong card.)
 2. **An incident belongs to the period its outage STARTED in** (``failure_time``, else
    ``outage_start_at``, else ``created_at`` -- the same instant ``services.vendors`` uses to
    pick the vendor's terms), for every KPI except availability. One incident, one card.
    (Alternative: the period it was RESTORED in -- then a long outage vanishes from the
    month it began in, and "tickets closed this month" rewards closing early.)
 3. **An incident with no restore is measured at the period end, never at "now".** If its
    adjusted elapsed time has ALREADY passed the restore limit at the period end it is an
    eligible breach; if it has not, its outcome is unknown and it is excluded, by name. A
    stop clock nobody closed is bounded by the same instant (``clock_events``' rule).
 4. **Only ``MARK_RESTORED`` / ``SUPERVISOR`` restores are measured** (the spec's words, for
    ADJ_MTTR). The same trusted set is the eligible set of SLA_COMPLIANCE_PCT: an inferred
    restore is excluded from numerator AND denominator, never counted as a breach or a
    pass. That exclusion is exactly why the data-quality gate exists. "Inferred" here means
    every provenance outside the trusted pair: ``VENDOR_NOTE_INFERRED``, ``ALARM_CLEAR``
    (reserved, no producer) and NULL (``lifecycle.close_incident`` back-fills ``restored_at``
    on close without saying how it knew).
 5. **The gate's percentage is inferred restores over RESTORED eligible incidents**, not
    over all incidents: the question is "how many of the restore times we used are
    guesses", and an open ticket has no restore time to be a guess.
 6. **MTTA measures incidents that have both timestamps.** One with no vendor note has no
    value to take a median of; it is counted and named as excluded (``NO_VENDOR_NOTE``),
    not silently dropped and not given an invented time. MTTA carries no band, so this
    cannot move money.
 7. **NOTE_COMPLIANCE_PCT is slot-based.** The vendor-owned window ``[escalated_at,
    restored_at | period end]`` is cut into consecutive slots of ``note_interval[P] x
    region_multiplier`` minutes; "expected note slots" are the COMPLETE slots; a slot is met
    when at least one vendor note falls in it (a note exactly on the boundary meets the
    slot it closes: the spec's "gap <= interval"). (Alternative: count notes whose gap from
    the previous note is within the interval -- but then twelve notes in the first twelve
    minutes score 100 % over three hours of silence. With slots, the denominator is set by
    the clock alone and no amount of posting can inflate the ratio; a vendor who is
    steadily 10 % slow scores about 90 %, which is proportionate.) The interval is the
    exact product -- 15 x 1.15 = 17.25 min -- not the monitor's truncated chase cadence.
    Stop clocks do not pause the obligation: a vendor locked out of a site can still say so.
 8. **Normalised = the same formula with each incident's minutes divided by ITS region's
    multiplier**, which for a single-region line is the spec's ``raw / multiplier``. Shown
    beside raw, never instead of it: bands and credits key on RAW. Percentages are not
    divided (95 % / 1.25 means nothing): normalised SLA compliance re-judges each incident's
    normalised minutes against the limit. NOTE_COMPLIANCE_PCT already has the multiplier
    inside its formula, so its normalised value is NULL; REPEAT_FAULT_RATE and
    AVAILABILITY_PCT are not normalised at all (the spec: "no other normalisation").
 9. **AVAILABILITY_PCT is time-based, not ticket-based**: every eligible, service-affecting
    outage of this vendor that INTERSECTS the period contributes its clipped minutes, per
    site, each site-minute once. ``scheduled_uptime`` is the spec's literal product and is
    NOT reduced by planned windows; planned minutes are removed from the UNAVAILABLE side
    only. (Alternative: also shrink the denominator -- not computable for a contracted site
    COUNT, which names no sites to look windows up for.)
10. **"Excluded once (anti-double-count)"** is read as: a minute that is both inside a stop
    clock and inside a planned window is excluded ONCE. Planned minutes are therefore only
    looked up over the part of an outage no stop clock already covers.
11. **``sites_in_scope`` is a contract term this repository does not have.** If
    ``sla_terms.vendors.<CODE>.sites_in_scope`` is ever supplied it is used and cited.
    Until then the figure is computed over the AFFECTED sites -- which is availability
    across the sites that failed, not network availability -- says so in its formula, and
    is banded ``NA``: a 99.5 % estate-wide target is not a fair test of a denominator that
    contains only the sites that went down.
12. **Bands key on the published (rounded) raw value**, so a reader can re-derive the band
    from the card alone and a dispute's ``adjusted_value`` re-bands by the same function.
    Only the three KPIs ``scorecards.bands`` configures are banded; the rest are ``NA``
    because inventing a threshold is inventing a commercial term. A line's ``yaml_path``
    names EVERY term it was judged against, ``;``-joined: the limit(s) and, on a banded line,
    the band thresholds -- ``resolve_term`` resolves the composite part by part. An
    all-priorities line cites all four per-priority limits rather than a parent mapping.
    Normalised SLA compliance judges an OPEN incident the way reading 3 judges the raw one:
    it is a normalised breach only if its normalised elapsed time exceeds the limit,
    otherwise it is left out of the normalised denominator.
13. **A credit is proposed only on a vendor-level (all-priorities) banded line that is
    RED**, only when the vendor's ``credit_shape`` is not ``none``, and is always
    ``PROPOSED``. ``per_occurrence`` takes ``credit_pct[0]``; ``escalating_consecutive``
    steps along ``credit_pct`` by the number of immediately preceding periods whose
    RELEASED card had the same line RED. The YAML names shapes and percentages but no
    trigger; "RED on the vendor-level line" is this module's reading and is THE
    interpretation with the most commercial weight -- confirm it with Supply Chain.
14. **The job leaves a card at DRAFT / SHADOW / WITHHELD.** §7.6.4 has the job publish; the
    lane's brief overrides that: no auto-publish, a human moves it on.
15. **Run and step summaries carry COUNTS only.** ``GET /api/v1/runs`` is a READERS surface
    (the vendor's coordinator included) while an unreleased card is a 404 to those roles, so
    "EGYPRO:WITHHELD" must not appear in a step's output. Per-vendor detail goes to the
    audit trail (AUDIT_READERS) and to the card itself.
16. **The dispute window ends at end of day.** ``dispute_window_working_days`` full
    Monday-Friday days after the day of publication, ending at 00:00 local of the next day,
    so a card published late on Friday and one published early on Saturday get the same
    window and the time of publication can never shorten it. Kenyan public holidays are
    NOT skipped (UNVERIFIED: no holiday calendar exists in this codebase).
17. **A ``formula`` says "contract" only for a term from a non-synthetic vendor block.**
    Everything else is named for what it is: "defaults, not contract", "SYNTHETIC sample
    contract", "operator profile, not contract" -- the formula is what the vendor pack
    renders line by line.
"""

from __future__ import annotations

import logging
import math
import os
import re
import statistics
import uuid
from contextlib import contextmanager
from calendar import monthrange
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction
from typing import Any, Callable, Iterable, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from noc_agents.agents.recurrence import problem_signature
from noc_agents.config import AppSettings, OperatorConfig
from noc_agents.db.models import AgentRunRow, AuditRow, IncidentRow, WorkNoteRow, utcnow
from noc_agents.db.models_scorecards import (
    BAND_AMBER,
    BAND_GREEN,
    BAND_NA,
    BAND_RED,
    CREDIT_NONE,
    CREDIT_PROPOSED,
    KPI_ADJ_MTTR,
    KPI_AVAILABILITY,
    KPI_MTTA,
    KPI_NOTE_COMPLIANCE,
    KPI_REPEAT_FAULT,
    COMPUTATION_SCOPE_KEY,
    KPI_SLA_COMPLIANCE,
    RECOMPUTABLE_STATUSES,
    SCORECARD_GRAPH_NAME,
    STATUS_DRAFT,
    STATUS_FINAL,
    STATUS_PUBLISHED,
    STATUS_SHADOW,
    STATUS_WITHHELD,
    VendorScorecardLineRow,
    VendorScorecardRow,
    earlier_released_card_exists,
    visible_human_name,
)
from noc_agents.db.models_vendors import ClockEventRow, VendorRow
from noc_agents.orchestrator.contract import FAILED, SUCCEEDED
from noc_agents.scheduler import JobCard, JobResult
from noc_agents.services.clock import z_utc
from noc_agents.services.clock_events import (
    LATE_OPENING_THRESHOLD_MIN,
    Interval,
    deducted_minutes,
    effective_intervals,
    late_openings,
    opening_delay_minutes,
)
from noc_agents.services.lifecycle import RESTORE_SOURCE_MARK, RESTORE_SOURCE_SUPERVISOR
from noc_agents.services.vendors import (
    FLAG,
    PRIORITIES,
    SOURCE_SLA_MINUTES,
    SlaTerms,
    backfill_incident_vendor_ids,
    incident_as_of,
    lane_enabled,
    list_vendors,
    load_sla_terms,
    normalise_code,
    seed_vendors_from_contacts,
)

log = logging.getLogger(__name__)

__all__ = [
    "AGENT",
    "GRAPH_NAME",
    "JOB_NAME",
    "LINE_SHAPE",
    "PUBLISHER_ROLES",
    "REVIEWER_ROLES",
    "SCORECARD_JOB",
    "GateResult",
    "IncidentFacts",
    "LineResult",
    "NoteSlots",
    "Period",
    "PeriodReport",
    "ScorecardComputation",
    "REFUSAL_GUARD",
    "REFUSAL_RELEASED",
    "REFUSAL_TABLE",
    "ScorecardGateError",
    "ScorecardJobError",
    "ScorecardPermissionError",
    "ScorecardStateError",
    "SiteUnavailability",
    "adjusted_elapsed_at",
    "adjusted_restore",
    "band_for",
    "build_lines",
    "close_periods",
    "compute_on_request",
    "compute_period",
    "compute_scorecard",
    "compute_vendor_period",
    "data_quality_gate",
    "dispute_window_end",
    "failure_reason",
    "finalise_scorecard",
    "gate_passes",
    "gather_facts",
    "ineligibility",
    "last_ended_period",
    "line_out",
    "lines_of",
    "mtta_minutes",
    "note_slots",
    "operator_discipline_counters",
    "period_bounds",
    "previous_period",
    "publish_scorecard",
    "record_shadow_review",
    "repeat_fault_sites",
    "resolve_term",
    "restore_is_trusted",
    "scorecard_out",
    "shadow_required_for",
    "site_unavailability",
]

# ------------------------------------------------------------------------------ the job's identity

JOB_NAME = "scorecard_close"  # spec §4.4 SCHEDULED_JOBS
INTERVAL_S = 3600  # hourly check; a no-op until a period has ended and has no card (§5.3.17)
AGENT = "SlaScorecardAgent"  # spec roster #17
GRAPH_NAME = SCORECARD_GRAPH_NAME  # agent_runs.graph_name ("scorecard"), as "outbox" / "monitor" / "pir" do; the mapper checks runs against it
ENABLED_ENV = FLAG  # SCORECARDS_ENABLED -- one flag for the whole of §7.6 (services/vendors.py)

#: Who may record a shadow review, publish or finalise (§7.6.3 ``duty_manager``+). Enforced
#: HERE and not only by ``require_role`` on the route, for the reason ``clock_events`` gives:
#: ``require_role`` is inert while ``AUTH_DISABLED=true`` (the demo default), and "a named
#: human released this card" must not be something the demo teaches people to skip.
REVIEWER_ROLES: tuple[str, ...] = ("duty_manager", "admin")
PUBLISHER_ROLES: tuple[str, ...] = REVIEWER_ROLES

# Whether a reviewer/publisher name is a VISIBLE human name is decided by ONE predicate,
# ``models_scorecards.visible_human_name`` -- used here, by the mapper guards and by the
# publish path -- so the service and the table's guard cannot drift (a U+200B "name" that one
# layer strips and another keeps is exactly how an invisible reviewer gets recorded).

#: Restore provenance a duration may be computed from (§7.6.2, §7.0.8).
TRUSTED_RESTORE_SOURCES: frozenset[str] = frozenset({RESTORE_SOURCE_MARK, RESTORE_SOURCE_SUPERVISOR})

#: What makes a work note a VENDOR note -- the same test ``worklog_monitor`` chases on and
#: ``lifecycle`` stamps ``first_vendor_note_at`` on, so the scorecard and the floor can never
#: disagree about whether the vendor spoke.
VENDOR_NOTE_ROLES: frozenset[str] = frozenset({"MSP", "FE", "RNIO"})
VENDOR_NOTE_SOURCES: frozenset[str] = frozenset({"msp", "fe", "vendor"})

#: KPIs where a bigger number is better, i.e. the only ones ``band_for`` knows how to band.
#: A band configured for a lower-is-better KPI (MTTA, MTTR) would be read upside down, so it
#: is ignored rather than guessed at.
_HIGHER_IS_BETTER: frozenset[str] = frozenset({KPI_SLA_COMPLIANCE, KPI_NOTE_COMPLIANCE, KPI_AVAILABILITY})

#: The fixed list of lines on every card, in reading order: (kpi, priority); ``None`` = all
#: priorities. Fixed, so that two months of one vendor line up row for row and a recompute
#: updates rows in place instead of inventing new ones.
LINE_SHAPE: tuple[tuple[str, str | None], ...] = (
    *((KPI_MTTA, p) for p in (*PRIORITIES, None)),
    *((KPI_ADJ_MTTR, p) for p in (*PRIORITIES, None)),
    *((KPI_SLA_COMPLIANCE, p) for p in (*PRIORITIES, None)),
    (KPI_REPEAT_FAULT, None),
    *((KPI_NOTE_COMPLIANCE, p) for p in (*PRIORITIES, None)),
    (KPI_AVAILABILITY, None),
)

_UNIT = {
    KPI_MTTA: "min",
    KPI_ADJ_MTTR: "min",
    KPI_SLA_COMPLIANCE: "pct",
    KPI_REPEAT_FAULT: "ratio",
    KPI_NOTE_COMPLIANCE: "pct",
    KPI_AVAILABILITY: "pct",
}
#: Decimal places each KPI is published to. Availability gets four because its bands sit
#: half a point apart at 99.x and one site-minute in a 31-day month is 0.0022 points.
_PLACES = {
    KPI_MTTA: 2,
    KPI_ADJ_MTTR: 2,
    KPI_SLA_COMPLIANCE: 2,
    KPI_REPEAT_FAULT: 4,
    KPI_NOTE_COMPLIANCE: 2,
    KPI_AVAILABILITY: 4,
}

# exclusion reasons (evidence_json; stable strings -- the vendor pack will show them)
X_CANCELLED = "CANCELLED"
X_PLANNED = "PLANNED_MAINTENANCE"
X_UNKNOWN_PRIORITY = "UNKNOWN_PRIORITY"
X_NOT_ESCALATED = "NOT_ESCALATED"
X_NO_VENDOR_NOTE = "NO_VENDOR_NOTE"
X_NOTE_BEFORE_ESCALATION = "NOTE_BEFORE_ESCALATION"
X_UNTRUSTED_RESTORE = "UNTRUSTED_RESTORE_SOURCE"
X_NOT_RESTORED = "NOT_RESTORED"
X_OPEN_AT_PERIOD_END = "OPEN_AT_PERIOD_END_NOT_YET_BREACHED"
X_RESTORE_BEFORE_START = "RESTORED_BEFORE_START"
X_NOT_SERVICE_AFFECTING = "NOT_SERVICE_AFFECTING"

_CARD_NS = uuid.uuid5(uuid.NAMESPACE_URL, "https://noc-agents.local/vendor-scorecards")

_MAINTENANCE_ENV = "MAINTENANCE_ENABLED"
_TRUE = {"1", "true", "yes", "on"}


class ScorecardStateError(RuntimeError):
    """The card is not in a state that allows this (route: 409)."""


class ScorecardGateError(ScorecardStateError):
    """A structural gate refuses: WITHHELD data quality, or a first period nobody reviewed (409)."""


class ScorecardPermissionError(PermissionError):
    """The acting role may not do this to a scorecard (route: 403)."""


class ScorecardJobError(RuntimeError):
    """What ``close_periods`` raises in place of the exception that stopped it. Its message is
    ``failure_reason(original)`` -- the class name and, for an IntegrityError, the constraint
    clause -- because the scheduler runner persists ``str(exc)`` of whatever a job raises into
    ``agent_runs.error_summary``, which ``GET /api/v1/runs`` serves to every reader role."""


#: The three refusals ``POST /scorecards/compute`` answers with a 409. Each detail STARTS with
#: exactly one of these, so a page can tell them apart without parsing the rest; the rest is
#: free text that may change. (The transition routes' 409s are the service's own fixed
#: messages: "WITHHELD: ...", "first period ... shadow review ...", "already ...", "only a
#: SHADOW card ...", "the dispute window is still open", "a line has an OPEN dispute ...".)
REFUSAL_RELEASED = "cannot recompute a released card"
REFUSAL_GUARD = "evidence guard refused the write"
REFUSAL_TABLE = "the card could not be written"

#: What a SQLite constraint failure looks like in ``exc.orig``: the keyword, then the
#: constraint name or the column list -- never a value. Anything else in an error text (the
#: statement, the bound parameters SQLAlchemy appends to ``str(exc)``) is exactly what must not
#: be recorded, so the clause is CUT OUT of ``exc.orig`` rather than the rest cut off.
_CONSTRAINT_CLAUSE = re.compile(r"((?:CHECK|UNIQUE|NOT NULL|FOREIGN KEY|PRIMARY KEY) constraint failed(?::\s*[\w.]+(?:,\s*[\w.]+)*)?)")


def failure_reason(exc: BaseException) -> str:
    """A fixed, non-sensitive description of why a computation stopped (reading 15, S08).

    The exception CLASS, and for an ``IntegrityError`` the constraint clause only
    (``UNIQUE constraint failed: vendor_scorecard_lines.id``). Never ``str(exc)``: for a
    database error that text carries the statement and its bound parameters -- an unreleased
    card's line values -- and it would be persisted into ``agent_runs`` / ``agent_run_steps``,
    which ``GET /api/v1/runs`` serves to roles that may not see a SHADOW or WITHHELD card.
    """
    name = type(exc).__name__
    if isinstance(exc, IntegrityError):
        origin = str(exc.orig) if exc.orig is not None else ""
        match = _CONSTRAINT_CLAUSE.search(origin)
        return f"{name}: {match.group(1)}" if match else f"{name}: constraint failed"
    return name


# ------------------------------------------------------------------------------------ periods


@dataclass(frozen=True)
class Period:
    """One scorecard period. ``start`` inclusive, ``end`` exclusive, both naive UTC."""

    label: str  # "2026-09"
    start: datetime
    end: datetime
    days: int
    timezone: str

    @property
    def minutes(self) -> int:
        """``24 x 60 x days_in_month`` (§7.6.2). EAT has no DST, so this is also ``end - start``."""
        return 24 * 60 * self.days


#: ``YYYY-MM`` in ASCII digits only. ``\\d`` would accept Unicode digits and ``int()`` accepts
#: ``"+026"``; a period label is a key in a UNIQUE constraint, so one spelling per month.
_PERIOD_RE = re.compile(r"^([0-9]{4})-([0-9]{2})$", re.ASCII)
_PERIOD_YEARS = range(2000, 2101)  # a sane range: outside it the label is a typo, not a period


def _parse_period(label: str) -> tuple[int, int]:
    match = _PERIOD_RE.match((label or "").strip())
    if match is None:
        raise ValueError(f"period must look like '2026-09', not {label!r}")
    year, month = int(match.group(1)), int(match.group(2))
    if year not in _PERIOD_YEARS or not 1 <= month <= 12:
        raise ValueError(f"period {label!r} is out of range: year 2000-2100, month 01-12")
    return year, month


def period_bounds(label: str, tz_name: str = "Africa/Nairobi") -> Period:
    """``"2026-09"`` -> the calendar month in ``tz_name`` as naive-UTC instants (reading 1)."""
    year, month = _parse_period(label)
    zone = ZoneInfo(tz_name)
    days = monthrange(year, month)[1]
    first = datetime(year, month, 1, tzinfo=zone)
    nxt = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=zone)
    to_naive_utc = lambda dt: dt.astimezone(timezone.utc).replace(tzinfo=None)  # noqa: E731
    return Period(label=f"{year:04d}-{month:02d}", start=to_naive_utc(first), end=to_naive_utc(nxt), days=days, timezone=tz_name)


def previous_period(label: str) -> str:
    year, month = _parse_period(label)
    return f"{year - (month == 1):04d}-{(month - 2) % 12 + 1:02d}"


def last_ended_period(now: datetime | None = None, tz_name: str = "Africa/Nairobi") -> str:
    """The most recent month that has fully ended in the operator's timezone."""
    local = (now or utcnow()).replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz_name))
    return previous_period(f"{local.year:04d}-{local.month:02d}")


def dispute_window_end(published_at: datetime, working_days: int, tz_name: str = "Africa/Nairobi") -> datetime:
    """When the dispute window closes (reading 16): ``working_days`` FULL Monday-Friday days
    after the day of publication, ending at 00:00 local of the day after the last one.

    Counting whole days from midnight -- not ``working_days`` x 24 h from the publish
    instant -- is what makes the window independent of the clock time of publication: a
    card published Friday 23:30 EAT and one published Saturday 01:00 EAT both run to the
    end of the same tenth working day. The vendor never gets less than N full days.

    UNVERIFIED / known gap: Kenyan public holidays are not skipped -- there is no holiday
    calendar in this codebase and inventing one is worse than saying so. The error shortens
    the vendor's window by the holidays inside it; the end date is on the card before
    publication and a supervisor can see it.
    """
    if working_days < 0:
        raise ValueError("working_days must be >= 0")
    zone = ZoneInfo(tz_name)
    day = published_at.replace(tzinfo=timezone.utc).astimezone(zone).date()
    counted = 0
    while counted < working_days:
        day += timedelta(days=1)
        if day.weekday() < 5:
            counted += 1
    end_local = datetime(day.year, day.month, day.day, tzinfo=zone) + timedelta(days=1)  # 00:00 of the next day
    return end_local.astimezone(timezone.utc).replace(tzinfo=None)


# -------------------------------------------------------------------------- exact arithmetic


def _minutes(delta: timedelta) -> Fraction:
    """A timedelta as an EXACT number of minutes (no float anywhere before the final rounding)."""
    return Fraction(delta.days * 86400 + delta.seconds, 60) + Fraction(delta.microseconds, 60_000_000)


def _round(value: Fraction | None, places: int) -> float | None:
    """Round half-up, exactly, once. Every value here is >= 0.

    ``round()`` on a float is banker's rounding over a binary approximation: 57.125 and
    57.135 do not round the way a person with a contract expects, and the answer can differ
    by platform. This is integer arithmetic on the exact ratio, so it cannot.
    """
    if value is None:
        return None
    scaled = Fraction(value) * (10**places)
    return ((2 * scaled.numerator + scaled.denominator) // (2 * scaled.denominator)) / (10**places)


def _median(values: Sequence[Fraction]) -> Fraction | None:
    """The median; for an even count, the mean of the two middle values (``statistics.median``)."""
    return Fraction(statistics.median(values)) if values else None


def _multiplier(cfg: OperatorConfig, region_code: str | None) -> Fraction:
    """``region_sla_note_multiplier[region]`` as an exact fraction (1.15 -> 23/20); 1 when the
    region has none. Through ``str`` so the YAML's decimal is read as written, not as the
    nearest binary float."""
    raw = (cfg.region_sla_note_multiplier or {}).get(region_code or "", 1.0)
    value = Fraction(str(raw))
    if value <= 0:
        raise ValueError(f"region_sla_note_multiplier[{region_code!r}] must be > 0, got {raw!r}")
    return value


# ------------------------------------------------------------------------------------- facts


@dataclass(frozen=True)
class IncidentFacts:
    """Everything the KPI functions may know about one incident -- and nothing else.

    The pure functions below take these, never an ORM row or a session, so the golden test
    can hand-build them and so it is visible at a glance what a vendor's number can depend
    on. There is deliberately no free text in here: no note body, no narrative, no names.
    """

    incident_id: str
    incident_number: str
    priority: str
    status: str
    site_id: str
    region_code: str
    failure_domain: str
    started_at: datetime  # failure_time, else outage_start_at, else created_at
    region_multiplier: Fraction = Fraction(1)
    planned_maintenance: bool = False
    service_affecting: bool = True
    escalated_at: datetime | None = None
    first_vendor_note_at: datetime | None = None
    restored_at: datetime | None = None
    restored_source: str | None = None
    vendor_note_times: tuple[datetime, ...] = ()
    clock_events: tuple[Any, ...] = ()  # ClockEventRow-shaped: started_at / ended_at / reversed_at / opened_at

    @property
    def signature(self) -> str:
        """``site|domain`` -- the recurrence identity (defect #34), from the one function that defines it."""
        return problem_signature(self.site_id, self.failure_domain)


def ineligibility(f: IncidentFacts) -> str | None:
    """Why ``f`` is outside §7.6.2's eligible set ("vendor assigned, not CANCELLED, not
    ``planned_maintenance``"), or ``None`` when it is inside. "Vendor assigned" is how the
    facts were selected, so it is not re-tested here."""
    if (f.status or "").upper() == "CANCELLED":
        return X_CANCELLED
    if f.planned_maintenance:
        return X_PLANNED
    return None


def restore_is_trusted(f: IncidentFacts) -> bool:
    return (f.restored_source or "") in TRUSTED_RESTORE_SOURCES


# ------------------------------------------------------------------- KPI primitives (pure)


def mtta_minutes(f: IncidentFacts) -> tuple[Fraction | None, str | None]:
    """``first_vendor_note_at - escalated_at`` in exact minutes, or ``(None, reason)``."""
    if f.escalated_at is None:
        return None, X_NOT_ESCALATED
    if f.first_vendor_note_at is None:
        return None, X_NO_VENDOR_NOTE
    if f.first_vendor_note_at < f.escalated_at:
        return None, X_NOTE_BEFORE_ESCALATION  # a data error, not a negative acknowledgement
    return _minutes(f.first_vendor_note_at - f.escalated_at), None


def adjusted_restore(f: IncidentFacts) -> tuple[Fraction | None, int, str | None]:
    """``restored_at - failure_time - stop-clock minutes`` for a TRUSTED restore.

    Returns ``(adjusted_minutes, scc_minutes, None)`` or ``(None, 0, reason)``. The deduction
    is ``clock_events.deducted_minutes`` over ``[failure_time, restored_at]``: the UNION of
    un-reversed stop clocks, in whole minutes, truncated -- reused, not re-implemented.
    """
    if f.restored_at is None:
        return None, 0, X_NOT_RESTORED
    if not restore_is_trusted(f):
        return None, 0, f"{X_UNTRUSTED_RESTORE}:{f.restored_source or 'NONE'}"
    if f.restored_at < f.started_at:
        return None, 0, X_RESTORE_BEFORE_START
    scc = deducted_minutes(f.clock_events, window_start=f.started_at, window_end=f.restored_at)
    return _minutes(f.restored_at - f.started_at) - scc, scc, None


def adjusted_elapsed_at(f: IncidentFacts, at: datetime) -> tuple[Fraction, int]:
    """Adjusted outage time of a NOT-yet-restored incident as of ``at`` (the period end).

    A stop clock with no ``ended_at`` is bounded by ``at``: a clock someone forgot to close
    does not go on deducting into next month (reading 3).
    """
    scc = deducted_minutes(f.clock_events, window_start=f.started_at, window_end=at)
    return _minutes(at - f.started_at) - scc, scc


@dataclass(frozen=True)
class NoteSlots:
    expected: int
    met: int
    interval_minutes: Fraction


def note_slots(f: IncidentFacts, *, note_interval: int, period_end: datetime) -> NoteSlots | None:
    """Expected and met note slots for one incident (reading 7), or ``None`` if never escalated."""
    if f.escalated_at is None:
        return None
    interval = Fraction(note_interval) * f.region_multiplier
    if interval <= 0:
        raise ValueError("note interval must be > 0")
    end = f.restored_at if f.restored_at is not None else period_end
    if end <= f.escalated_at:
        return NoteSlots(0, 0, interval)
    expected = math.floor(_minutes(end - f.escalated_at) / interval)
    met: set[int] = set()
    for at in f.vendor_note_times:
        if at < f.escalated_at or at > end:
            continue
        # Slot k is (k*I, (k+1)*I]; a note at exactly 0 belongs to slot 0.
        k = max(0, math.ceil(_minutes(at - f.escalated_at) / interval) - 1)
        if k < expected:
            met.add(k)
    return NoteSlots(expected, len(met), interval)


def repeat_fault_sites(facts: Iterable[IncidentFacts]) -> tuple[list[str], list[str]]:
    """``(affected_sites, repeat_sites)``: sites, not visits (§7.6.2). A repeat site has at
    least two eligible incidents with the SAME ``site|domain`` signature in the period."""
    by_signature: dict[str, int] = {}
    sites: set[str] = set()
    for f in facts:
        sites.add(f.site_id)
        by_signature[f.signature] = by_signature.get(f.signature, 0) + 1
    repeats = {f.site_id for f in facts if by_signature[f.signature] >= 2}
    return sorted(sites), sorted(repeats)


@dataclass(frozen=True)
class _Span:
    """An interval shaped like a clock event, so ``clock_events.effective_intervals`` -- the
    union arithmetic that was verified against adversarial cases -- does every union in this
    module and none is re-implemented (``services.maintenance`` does the same)."""

    started_at: datetime
    ended_at: datetime
    reversed_at: datetime | None = None


def _gaps(start: datetime, end: datetime, covered: Sequence[Interval]) -> list[Interval]:
    """``[start, end]`` minus ``covered`` (sorted, disjoint, inside the window)."""
    out: list[Interval] = []
    cursor = start
    for iv in covered:
        if iv.start > cursor:
            out.append(Interval(cursor, iv.start))
        cursor = max(cursor, iv.end)
    if end > cursor:
        out.append(Interval(cursor, end))
    return out


@dataclass(frozen=True)
class SiteUnavailability:
    site_id: str
    gross_minutes: Fraction  # the site was down (union of this vendor's outages, clipped to the period)
    scc_minutes: int  # of which a stop clock was running (whole minutes, truncated)
    planned_minutes: int  # of the remainder, inside a SCHEDULED window (whole minutes, truncated)
    incidents: tuple[str, ...]

    @property
    def unavailable_minutes(self) -> Fraction:
        return self.gross_minutes - self.scc_minutes - self.planned_minutes


PlannedMinutesFn = Callable[[str, datetime, datetime], int]


def site_unavailability(
    site_id: str,
    facts: Sequence[IncidentFacts],
    period: Period,
    planned_minutes_fn: PlannedMinutesFn | None,
) -> SiteUnavailability:
    """One site's unavailable minutes in the period (readings 9 and 10).

    * each outage is clipped to the period; an unrestored one ends at the period end;
    * a stop clock excuses only ITS OWN incident: a minute counts if ANY incident covering
      it is not stopped at that minute, so two overlapping tickets on one site are neither
      double-counted nor allowed to borrow each other's stop clocks;
    * planned minutes are looked up only over what is left, so a minute inside both a stop
      clock and a planned window is excluded once.
    """
    outages: list[_Span] = []
    owed: list[_Span] = []
    numbers: list[str] = []
    for f in facts:
        lo = max(f.started_at, period.start)
        hi = min(f.restored_at if f.restored_at is not None else period.end, period.end)
        if hi <= lo:
            continue
        numbers.append(f.incident_number)
        outages.append(_Span(lo, hi))
        stopped = effective_intervals(f.clock_events, window_start=lo, window_end=hi)
        owed.extend(_Span(g.start, g.end) for g in _gaps(lo, hi, stopped))
    window = dict(window_start=period.start, window_end=period.end)
    down = effective_intervals(outages, **window)
    attributable = effective_intervals(owed, **window)
    gross = sum((iv.duration for iv in down), timedelta())
    unstopped = sum((iv.duration for iv in attributable), timedelta())
    # Truncated like ``deducted_minutes``: a partial stop-clock minute is not credited. For a
    # single incident this IS ``deducted_minutes(events, window=outage)`` -- a test pins it.
    scc = int((gross - unstopped).total_seconds() // 60)
    planned = 0
    if planned_minutes_fn is not None:
        planned = sum(int(planned_minutes_fn(site_id, iv.start, iv.end)) for iv in attributable)
    return SiteUnavailability(site_id, _minutes(gross), scc, planned, tuple(numbers))


def band_for(kpi: str, value: float | None, bands: dict[str, Any] | None) -> str:
    """GREEN / AMBER / RED / NA for a published value (reading 12). Also what the dispute
    lane calls to re-band an ``adjusted_value``."""
    if value is None or kpi not in _HIGHER_IS_BETTER:
        return BAND_NA
    cfg = (bands or {}).get(kpi)
    if not isinstance(cfg, dict) or cfg.get("green") is None or cfg.get("amber") is None:
        return BAND_NA  # no threshold configured: never invent one
    if value >= float(cfg["green"]):
        return BAND_GREEN
    if value >= float(cfg["amber"]):
        return BAND_AMBER
    return BAND_RED


# ------------------------------------------------------------------------------- the two gates


def gate_passes(inferred_restores: int, restored_incidents: int, threshold_pct: float) -> bool:
    """§7.6.2: ``inferred_restore_pct > max_inferred_restore_pct`` -> WITHHELD. The SAME
    cross-multiplied expression as ``ck_vendor_scorecards_gate`` in the table, on purpose."""
    return inferred_restores * 100.0 <= threshold_pct * restored_incidents


@dataclass(frozen=True)
class GateResult:
    incidents: int  # eligible incidents attributed to the period
    restored_incidents: int
    inferred_restores: int
    threshold_pct: float
    passed: bool
    by_source: dict[str, int]
    inferred_incidents: tuple[str, ...]

    @property
    def inferred_pct(self) -> float | None:
        if not self.restored_incidents:
            return None
        return _round(Fraction(self.inferred_restores * 100, self.restored_incidents), 2)

    @property
    def reason(self) -> str:
        if self.passed:
            return ""
        return (
            f"WITHHELD: {self.inferred_restores} of {self.restored_incidents} restore times "
            f"({self.inferred_pct} %) were inferred rather than recorded by a named human, above the "
            f"{self.threshold_pct:g} % limit (scorecards.max_inferred_restore_pct). Restore-based KPIs "
            f"exclude those incidents, so at this rate the card describes the data, not the vendor. "
            f"Fix: a supervisor records the real restore on each listed incident, then recompute."
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "incidents": self.incidents,
            "restored_incidents": self.restored_incidents,
            "inferred_restores": self.inferred_restores,
            "inferred_pct": self.inferred_pct,
            "gate_threshold_pct": self.threshold_pct,
            "passed": self.passed,
            "reason": self.reason,
            "inferred_by_source": dict(sorted(self.by_source.items())),
            "inferred_incidents": list(self.inferred_incidents),
            "yaml_path": "scorecards.max_inferred_restore_pct",
        }


def data_quality_gate(facts: Iterable[IncidentFacts], threshold_pct: float) -> GateResult:
    """Count the restores the card leans on and how many are guesses (readings 4 and 5)."""
    eligible = [f for f in facts if ineligibility(f) is None]
    restored = [f for f in eligible if f.restored_at is not None]
    inferred = [f for f in restored if not restore_is_trusted(f)]
    by_source: dict[str, int] = {}
    for f in inferred:
        key = f.restored_source or "NONE"
        by_source[key] = by_source.get(key, 0) + 1
    return GateResult(
        incidents=len(eligible),
        restored_incidents=len(restored),
        inferred_restores=len(inferred),
        threshold_pct=float(threshold_pct),
        passed=gate_passes(len(inferred), len(restored), float(threshold_pct)),
        by_source=by_source,
        inferred_incidents=tuple(sorted(f.incident_number for f in inferred)),
    )


def operator_discipline_counters(facts: Iterable[IncidentFacts], *, late_threshold_min: int) -> dict[str, Any]:
    """§7.6.2's operator-side counters: OUR data hygiene, shown to the vendor.

    ``opened_at - started_at`` measures how late OUR NOC recorded a stop clock. It is
    REPORTED here and is an input to nothing: no KPI function takes it, and a test computes
    one fixture with prompt and with late openings and requires identical lines. A counter
    that could move a vendor's number in either direction would be an incentive to mis-keep
    the record.

    ``missing_scc_with_confirmed_power`` is ``None``, not 0: the planned-power link it needs
    (§7.3, the KPLC lane) does not exist in this schema yet. "We did not look" and "we looked
    and found none" are different statements and only the first one is true today.
    """
    late: list[dict[str, Any]] = []
    total = 0
    for f in facts:
        total += len(f.clock_events)
        for ev in late_openings(f.clock_events, threshold_min=late_threshold_min):
            late.append(
                {
                    "incident": f.incident_number,
                    "event_id": ev.id,
                    "scc_code": ev.scc_code,
                    "opening_delay_min": opening_delay_minutes(ev),
                    "reversed": ev.reversed_at is not None,
                }
            )
    late.sort(key=lambda row: (row["incident"], str(row["event_id"])))
    return {
        "scc_events": total,
        "late_scc_openings": len(late),
        "late_scc_opening_threshold_min": late_threshold_min,
        "late_scc_opening_events": late,
        "missing_scc_with_confirmed_power": None,
        "missing_scc_with_confirmed_power_status": "NOT_COMPUTABLE",
        "missing_scc_with_confirmed_power_note": (
            "not computed: this deployment has no confirmed planned-power link to test against "
            "(the KPLC notice lane is not built); null means 'not looked', never 'none found'"
        ),
        "note": "operator-side counters; they describe the operator's record-keeping and move no vendor KPI",
    }


# ------------------------------------------------------------------------------------- lines


@dataclass(frozen=True)
class LineResult:
    kpi: str
    priority: str | None
    raw_value: float | None
    normalised_value: float | None
    region_multiplier_applied: float | None
    unit: str
    band: str
    eligible_incidents: int
    excluded_incidents: int
    scc_minutes_deducted: int
    formula: str
    yaml_path: str
    evidence: dict[str, Any] = field(default_factory=dict)
    proposed_credit_pct: float | None = None
    credit_status: str = CREDIT_NONE

    @property
    def key(self) -> tuple[str, str | None]:
        return self.kpi, self.priority


#: Separator of a composite ``yaml_path``: every term a line was judged against, in order.
PATH_SEP = ";"


def _resolve_one(terms: SlaTerms, cfg: OperatorConfig, yaml_path: str) -> Any:
    if yaml_path == "sla_minutes":
        return {p: {"ack": b.ack, "restore": b.restore, "note_interval": b.note_interval} for p, b in cfg.sla_minutes.items()}
    if yaml_path.startswith("sla_minutes."):
        parts = yaml_path.split(".")
        band = cfg.sla_minutes.get(parts[1]) if len(parts) > 1 else None
        if band is None:
            raise KeyError(yaml_path)
        if len(parts) == 2:
            return {"ack": band.ack, "restore": band.restore, "note_interval": band.note_interval}
        if len(parts) == 3 and parts[2] in ("ack", "restore", "note_interval"):
            return getattr(band, parts[2])
        raise KeyError(yaml_path)
    return terms.resolve_path(yaml_path)


def resolve_term(terms: SlaTerms, cfg: OperatorConfig, yaml_path: str) -> Any:
    """The value(s) a line's ``yaml_path`` points at; ``KeyError`` when any part points at nothing.

    A path is one dotted key, or several joined with ``;`` (an all-priorities line cites all
    four limits; a banded line also cites its band thresholds). A single path returns its
    value; a composite returns ``{path: value}``. Paths into the terms file go through
    ``SlaTerms.resolve_path``; a band that fell back to the operator profile cites
    ``sla_minutes.P3.restore`` (``services.vendors`` is honest that the value did NOT come
    from the terms file) and is resolved against the profile, as is the bare ``sla_minutes``.
    """
    parts = [part.strip() for part in yaml_path.split(PATH_SEP) if part.strip()]
    if not parts:
        raise KeyError(yaml_path)
    if len(parts) == 1:
        return _resolve_one(terms, cfg, parts[0])
    return {part: _resolve_one(terms, cfg, part) for part in parts}


def _resolves(terms: SlaTerms, path: str) -> bool:
    try:
        terms.resolve_path(path)
        return True
    except KeyError:
        return False


def _first_resolving(terms: SlaTerms, *paths: str) -> str:
    for path in paths:
        if _resolves(terms, path):
            return path
    return "sla_terms.version"  # mandatory, so it always resolves


def _term_path(terms: SlaTerms, vendor_code: str, priority: str | None, key: str) -> str:
    """The ``yaml_path`` of the limit(s) a time-based line was judged against. One priority:
    that exact value (``sla_terms.default.P1.restore``). All priorities: all four, ``;``-joined
    -- each incident was judged against its OWN priority's value, and every part resolves
    (through the profile for a ``sla_minutes`` fallback)."""
    if priority is not None:
        return terms.bands_for(vendor_code, priority).path(key)
    return PATH_SEP.join(terms.bands_for(vendor_code, p).path(key) for p in PRIORITIES)


def _band_path(terms: SlaTerms, kpi: str) -> str | None:
    """The thresholds a banded line was banded against, if the file has them."""
    path = f"scorecards.bands.{kpi}"
    return path if _resolves(terms, path) else None


def _banded_path(terms: SlaTerms, kpi: str, limit_path: str) -> str:
    """Limit(s) plus band thresholds: a banded line cites what it was BANDED against too --
    above all the vendor-level SLA line, which is the one that carries a proposed credit."""
    band = _band_path(terms, kpi)
    return f"{limit_path}{PATH_SEP}{band}" if band else limit_path


def _band_note(kpi: str, bands: dict[str, Any]) -> str:
    cfg = (bands or {}).get(kpi)
    if isinstance(cfg, dict) and cfg.get("green") is not None and cfg.get("amber") is not None:
        return f"; band: GREEN >= {cfg['green']}, AMBER >= {cfg['amber']}, else RED (scorecards.bands.{kpi})"
    return "; band NA: no thresholds configured for this KPI and none invented"


def _term_label(terms: SlaTerms, vendor_code: str, priority: str, key: str, noun: str) -> str:
    """How a per-priority formula names the term it used (reading 17). The word "contract"
    appears only when the value came from a NON-synthetic vendor block; every other origin
    is named for what it is, because this text is what the vendor pack renders."""
    band = terms.bands_for(vendor_code, priority)
    path, value = band.path(key), band.minutes(key)
    vendor = terms.vendor(vendor_code)
    if band.source == SOURCE_SLA_MINUTES:
        return f"; {noun} {value} min ({path} -- operator profile, not contract)"
    if path.startswith("sla_terms.vendors."):
        if vendor is not None and vendor.has_contract:
            return f"; contract {noun} {value} min ({path}, contract {vendor.contract_ref})"
        return f"; {noun} {value} min ({path} -- SYNTHETIC sample contract, not an agreement anyone signed)"
    return f"; {noun} {value} min ({path} -- defaults, not contract)"


def _vendor_raw_block(terms: SlaTerms, vendor_code: str) -> tuple[str | None, dict[str, Any]]:
    """The vendor's block as WRITTEN in the YAML, with the key as written (a ``yaml_path``
    must use the file's own spelling to resolve)."""
    for key, block in ((terms.raw.get("sla_terms") or {}).get("vendors") or {}).items():
        if normalise_code(key) == normalise_code(vendor_code):
            return str(key), dict(block or {})
    return None, {}


def _shared_multiplier(facts: Sequence[IncidentFacts]) -> float | None:
    values = {f.region_multiplier for f in facts}
    return float(values.pop()) if len(values) == 1 else None


def _fmt(value: float | None, unit: str) -> str:
    if value is None:
        return "n/a"
    return f"{value:g} {unit}" if unit != "ratio" else f"{value:g}"


def _excluded(pool: Sequence[IncidentFacts], reasons: dict[str, str]) -> list[dict[str, str]]:
    return [{"incident": f.incident_number, "reason": reasons[f.incident_id]} for f in pool if f.incident_id in reasons]


def _reason_summary(excluded: Sequence[dict[str, str]]) -> str:
    counts: dict[str, int] = {}
    for row in excluded:
        counts[row["reason"]] = counts.get(row["reason"], 0) + 1
    return ", ".join(f"{n} {reason}" for reason, n in sorted(counts.items())) or "none"


def _line(kpi: str, priority: str | None, **kw: Any) -> LineResult:
    return LineResult(kpi=kpi, priority=priority, unit=_UNIT[kpi], **kw)


def _mtta_line(pool: Sequence[IncidentFacts], priority: str | None, terms: SlaTerms, vendor_code: str) -> LineResult:
    reasons: dict[str, str] = {}
    measured: list[tuple[IncidentFacts, Fraction]] = []
    for f in pool:
        why = ineligibility(f)
        value = None
        if why is None:
            value, why = mtta_minutes(f)
        if value is None:
            reasons[f.incident_id] = why or X_NOT_ESCALATED
        else:
            measured.append((f, value))
    raw = _round(_median([v for _, v in measured]), _PLACES[KPI_MTTA])
    norm = _round(_median([v / f.region_multiplier for f, v in measured]), _PLACES[KPI_MTTA])
    excluded = _excluded(pool, reasons)
    target = _term_label(terms, vendor_code, priority, "ack", "ack target") if priority else ""
    return _line(
        KPI_MTTA,
        priority,
        raw_value=raw,
        normalised_value=norm,
        region_multiplier_applied=_shared_multiplier([f for f, _ in measured]),
        band=BAND_NA,  # no band is configured for MTTA and none is invented (reading 12)
        eligible_incidents=len(measured),
        excluded_incidents=len(excluded),
        scc_minutes_deducted=0,
        formula=(
            f"median(first_vendor_note_at - escalated_at) over {len(measured)} incident(s) = {_fmt(raw, 'min')}"
            f"{target}; normalised = median of each value / its region multiplier = {_fmt(norm, 'min')}; "
            f"excluded: {_reason_summary(excluded)}"
        ),
        yaml_path=_term_path(terms, vendor_code, priority, "ack"),
        evidence={
            "measured": [{"incident": f.incident_number, "minutes": _round(v, 2), "region_multiplier": float(f.region_multiplier)} for f, v in measured],
            "excluded": excluded,
        },
    )


@dataclass(frozen=True)
class _Restore:
    """One incident's standing against its restore limit."""

    facts: IncidentFacts
    kind: str  # MEASURED | OPEN_BREACHED
    minutes: Fraction
    scc_minutes: int
    limit: int

    @property
    def compliant(self) -> bool:
        return self.kind == "MEASURED" and self.minutes <= self.limit

    @property
    def normalised_minutes(self) -> Fraction:
        return self.minutes / self.facts.region_multiplier

    @property
    def compliant_normalised(self) -> bool:
        return self.kind == "MEASURED" and self.normalised_minutes <= self.limit

    @property
    def in_normalised_denominator(self) -> bool:
        """Reading 3 applied to the normalised figure: an OPEN incident is a normalised breach
        only if its normalised elapsed time already exceeds the limit; if not, its normalised
        outcome is unknown and it is left out of that denominator (its raw breach stands)."""
        return self.kind == "MEASURED" or self.normalised_minutes > self.limit


def _classify_restore(f: IncidentFacts, limit: int, period: Period) -> tuple[_Restore | None, str | None]:
    """MEASURED (trusted restore), OPEN_BREACHED (no restore and already over the limit at the
    period end), or excluded with a reason (readings 3 and 4)."""
    why = ineligibility(f)
    if why is not None:
        return None, why
    if f.restored_at is None:
        elapsed, scc = adjusted_elapsed_at(f, period.end)
        if elapsed > limit:
            return _Restore(f, "OPEN_BREACHED", elapsed, scc, limit), None
        return None, X_OPEN_AT_PERIOD_END
    minutes, scc, why = adjusted_restore(f)
    if minutes is None:
        return None, why
    return _Restore(f, "MEASURED", minutes, scc, limit), None


def _restore_lines(
    pool: Sequence[IncidentFacts], priority: str | None, terms: SlaTerms, vendor_code: str, period: Period, bands: dict[str, Any]
) -> tuple[LineResult, LineResult]:
    """ADJ_MTTR_MIN and SLA_COMPLIANCE_PCT for one pool: they share one classification, so
    the two lines can never disagree about which incidents were measured."""
    reasons: dict[str, str] = {}
    standing: list[_Restore] = []
    for f in pool:
        if f.priority not in PRIORITIES:
            reasons[f.incident_id] = ineligibility(f) or X_UNKNOWN_PRIORITY
            continue
        result, why = _classify_restore(f, terms.bands_for(vendor_code, f.priority).restore, period)
        if result is None:
            reasons[f.incident_id] = why or X_NOT_RESTORED
        else:
            standing.append(result)

    # --- ADJ_MTTR_MIN: measured (trusted, restored) incidents only
    measured = [r for r in standing if r.kind == "MEASURED"]
    mttr_reasons = dict(reasons)
    for r in standing:
        if r.kind != "MEASURED":
            mttr_reasons[r.facts.incident_id] = X_NOT_RESTORED
    mttr_excluded = _excluded(pool, mttr_reasons)
    raw = _round(_median([r.minutes for r in measured]), _PLACES[KPI_ADJ_MTTR])
    norm = _round(_median([r.minutes / r.facts.region_multiplier for r in measured]), _PLACES[KPI_ADJ_MTTR])
    limit_note = _term_label(terms, vendor_code, priority, "restore", "restore limit") if priority else ""
    mttr = _line(
        KPI_ADJ_MTTR,
        priority,
        raw_value=raw,
        normalised_value=norm,
        region_multiplier_applied=_shared_multiplier([r.facts for r in measured]),
        band=BAND_NA,
        eligible_incidents=len(measured),
        excluded_incidents=len(mttr_excluded),
        scc_minutes_deducted=sum(r.scc_minutes for r in measured),
        formula=(
            f"median(restored_at - failure_time - stop-clock minutes) over {len(measured)} incident(s) with "
            f"restored_source in MARK_RESTORED/SUPERVISOR = {_fmt(raw, 'min')}{limit_note}; stop-clock minutes = "
            f"union of un-reversed SCC intervals inside the outage, whole minutes; normalised = "
            f"{_fmt(norm, 'min')}; excluded: {_reason_summary(mttr_excluded)}"
        ),
        yaml_path=_term_path(terms, vendor_code, priority, "restore"),
        evidence={
            "measured": [
                {
                    "incident": r.facts.incident_number,
                    "adjusted_minutes": _round(r.minutes, 2),
                    "scc_minutes": r.scc_minutes,
                    "restored_source": r.facts.restored_source,
                    "region_multiplier": float(r.facts.region_multiplier),
                }
                for r in measured
            ],
            "excluded": mttr_excluded,
        },
    )

    # --- SLA_COMPLIANCE_PCT: measured + already-breached-at-period-end
    compliant = sum(1 for r in standing if r.compliant)
    norm_standing = [r for r in standing if r.in_normalised_denominator]
    compliant_norm = sum(1 for r in norm_standing if r.compliant_normalised)
    sla_excluded = _excluded(pool, reasons)
    sla_raw = _round(Fraction(100 * compliant, len(standing)), _PLACES[KPI_SLA_COMPLIANCE]) if standing else None
    sla_norm = _round(Fraction(100 * compliant_norm, len(norm_standing)), _PLACES[KPI_SLA_COMPLIANCE]) if norm_standing else None
    sla = _line(
        KPI_SLA_COMPLIANCE,
        priority,
        raw_value=sla_raw,
        normalised_value=sla_norm,
        region_multiplier_applied=_shared_multiplier([r.facts for r in standing]),
        band=band_for(KPI_SLA_COMPLIANCE, sla_raw, bands),
        eligible_incidents=len(standing),
        excluded_incidents=len(sla_excluded),
        scc_minutes_deducted=sum(r.scc_minutes for r in standing),
        formula=(
            f"100 x {compliant} compliant / {len(standing)} eligible = {_fmt(sla_raw, '%')}; compliant = adjusted "
            f"restore <= restore minutes of the incident's own priority{limit_note}; an incident with no restore "
            f"counts as a breach only if already over its limit at the period end; normalised (each incident's "
            f"minutes / its region multiplier, same limit; an open incident is a normalised breach only if its "
            f"normalised elapsed exceeds the limit, else left out) = 100 x {compliant_norm} / {len(norm_standing)} = "
            f"{_fmt(sla_norm, '%')}{_band_note(KPI_SLA_COMPLIANCE, bands)}; excluded: {_reason_summary(sla_excluded)}"
        ),
        yaml_path=_banded_path(terms, KPI_SLA_COMPLIANCE, _term_path(terms, vendor_code, priority, "restore")),
        evidence={
            "measured": [
                {
                    "incident": r.facts.incident_number,
                    "kind": r.kind,
                    "adjusted_minutes": _round(r.minutes, 2),
                    "limit_minutes": r.limit,
                    "compliant": r.compliant,
                    "compliant_normalised": r.compliant_normalised,
                    "in_normalised_denominator": r.in_normalised_denominator,
                    "scc_minutes": r.scc_minutes,
                }
                for r in standing
            ],
            "excluded": sla_excluded,
        },
    )
    return mttr, sla


def _repeat_line(pool: Sequence[IncidentFacts], terms: SlaTerms) -> LineResult:
    reasons = {f.incident_id: why for f in pool if (why := ineligibility(f)) is not None}
    eligible = [f for f in pool if f.incident_id not in reasons]
    sites, repeats = repeat_fault_sites(eligible)
    raw = _round(Fraction(len(repeats), len(sites)), _PLACES[KPI_REPEAT_FAULT]) if sites else None
    excluded = _excluded(pool, reasons)
    return _line(
        KPI_REPEAT_FAULT,
        None,
        raw_value=raw,
        normalised_value=None,  # a count of sites has no regional time allowance (reading 8)
        region_multiplier_applied=None,
        band=BAND_NA,
        eligible_incidents=len(eligible),
        excluded_incidents=len(excluded),
        scc_minutes_deducted=0,
        formula=(
            f"{len(repeats)} site(s) with >= 2 incidents of the same site|domain signature in the period / "
            f"{len(sites)} affected site(s) = {_fmt(raw, 'ratio')} (sites, not visits); band NA: no thresholds are "
            f"configured for this KPI and none invented; the only term it depends on is the period definition "
            f"(scorecards.period); excluded: {_reason_summary(excluded)}"
        ),
        yaml_path=_first_resolving(terms, "scorecards.period"),
        evidence={
            "affected_sites": sites,
            "repeat_sites": repeats,
            "measured": [{"incident": f.incident_number, "site_id": f.site_id, "signature": f.signature} for f in eligible],
            "excluded": excluded,
        },
    )


def _note_line(pool: Sequence[IncidentFacts], priority: str | None, terms: SlaTerms, vendor_code: str, period: Period, bands: dict[str, Any]) -> LineResult:
    reasons: dict[str, str] = {}
    measured: list[tuple[IncidentFacts, NoteSlots]] = []
    for f in pool:
        why = ineligibility(f)
        if why is None and f.priority not in PRIORITIES:
            why = X_UNKNOWN_PRIORITY
        slots = None
        if why is None:
            slots = note_slots(f, note_interval=terms.bands_for(vendor_code, f.priority).note_interval, period_end=period.end)
            if slots is None:
                why = X_NOT_ESCALATED
        if slots is None:
            reasons[f.incident_id] = why or X_NOT_ESCALATED
        else:
            measured.append((f, slots))
    expected = sum(s.expected for _, s in measured)
    met = sum(s.met for _, s in measured)
    raw = _round(Fraction(100 * met, expected), _PLACES[KPI_NOTE_COMPLIANCE]) if expected else None
    excluded = _excluded(pool, reasons)
    interval_note = _term_label(terms, vendor_code, priority, "note_interval", "note interval") if priority else ""
    return _line(
        KPI_NOTE_COMPLIANCE,
        priority,
        raw_value=raw,
        normalised_value=None,  # the multiplier is already inside this KPI's formula (reading 8)
        region_multiplier_applied=_shared_multiplier([f for f, _ in measured]),
        band=band_for(KPI_NOTE_COMPLIANCE, raw, bands),
        eligible_incidents=len(measured),
        excluded_incidents=len(excluded),
        scc_minutes_deducted=0,
        formula=(
            f"100 x {met} met / {expected} expected note slots = {_fmt(raw, '%')}; a slot is note_interval of the "
            f"incident's priority x its region multiplier, counted from escalated_at to restored_at (period end "
            f"if not restored); expected = complete slots; met = a slot with >= 1 vendor note{interval_note}"
            f"{_band_note(KPI_NOTE_COMPLIANCE, bands)}; excluded: {_reason_summary(excluded)}"
        ),
        yaml_path=_banded_path(terms, KPI_NOTE_COMPLIANCE, _term_path(terms, vendor_code, priority, "note_interval")),
        evidence={
            "measured": [
                {
                    "incident": f.incident_number,
                    "slot_minutes": _round(s.interval_minutes, 4),
                    "expected": s.expected,
                    "met": s.met,
                    "region_multiplier": float(f.region_multiplier),
                }
                for f, s in measured
            ],
            "excluded": excluded,
        },
    )


def _availability_line(
    pool: Sequence[IncidentFacts],
    terms: SlaTerms,
    vendor_code: str,
    period: Period,
    bands: dict[str, Any],
    planned_minutes_fn: PlannedMinutesFn | None,
) -> LineResult:
    reasons: dict[str, str] = {}
    for f in pool:
        why = ineligibility(f)
        if why is None and not f.service_affecting:
            why = X_NOT_SERVICE_AFFECTING  # the site stayed on air: nothing was unavailable
        if why is not None:
            reasons[f.incident_id] = why
    by_site: dict[str, list[IncidentFacts]] = {}
    for f in pool:
        if f.incident_id not in reasons:
            by_site.setdefault(f.site_id, []).append(f)
    sites = [site_unavailability(site_id, by_site[site_id], period, planned_minutes_fn) for site_id in sorted(by_site)]
    sites = [s for s in sites if s.incidents]
    contributing = {n for s in sites for n in s.incidents}
    for f in pool:  # an outage that turned out not to intersect the period after clipping
        if f.incident_id not in reasons and f.incident_number not in contributing:
            reasons[f.incident_id] = "OUTSIDE_PERIOD"
    unavailable = sum((s.unavailable_minutes for s in sites), Fraction(0))

    # reading 11: the denominator's site count is a contract term this repository lacks
    key, block = _vendor_raw_block(terms, vendor_code)
    contracted = block.get("sites_in_scope")
    scope_problem = ""
    scope_is_contracted = isinstance(contracted, int) and not isinstance(contracted, bool) and contracted > 0
    if scope_is_contracted:
        in_scope, scope_source = int(contracted), f"contracted (sla_terms.vendors.{key}.sites_in_scope)"
        if in_scope < len(sites):
            scope_problem = f"contracted sites_in_scope ({in_scope}) is smaller than the {len(sites)} affected sites"
    else:
        in_scope, scope_source = len(sites), "AFFECTED SITES ONLY -- no contracted site list in sla_terms"
    uptime = period.minutes * in_scope
    raw = None
    if uptime > 0 and not scope_problem:
        raw = _round(Fraction(100) * (uptime - unavailable) / uptime, _PLACES[KPI_AVAILABILITY])
    banded = scope_is_contracted and not scope_problem and raw is not None
    band = band_for(KPI_AVAILABILITY, raw, bands) if banded else BAND_NA
    # The line is BANDED on scorecards.bands; the contract-shaped availability_target_pct is
    # cited beside it, and a disagreement between the two is said out loud, not resolved silently.
    target_pct = terms.availability_target_pct
    green = ((bands or {}).get(KPI_AVAILABILITY) or {}).get("green") if isinstance((bands or {}).get(KPI_AVAILABILITY), dict) else None
    target_note = ""
    if target_pct is not None and green is not None and float(target_pct) != float(green):
        target_note = (
            f"; NOTE: sla_terms.default.availability_target_pct ({target_pct:g}) and scorecards.bands.{KPI_AVAILABILITY}.green "
            f"({float(green):g}) disagree -- banded on scorecards.bands, the target is shown for the record"
        )
    if planned_minutes_fn is None:
        planned_note = f"planned maintenance windows were NOT excluded ({_MAINTENANCE_ENV} is off: the windows were not looked at, which is not the same as there being none)"
    else:
        planned_note = f"{sum(s.planned_minutes for s in sites)} min inside SCHEDULED maintenance windows excluded (only where no stop clock already excluded them)"
    band_note = "" if banded else (
        f"; band NA: {scope_problem}" if scope_problem else "; band NA: this is availability across the sites that failed, not across the estate, so the estate-wide target is not applied"
    )
    excluded = _excluded(pool, reasons)
    return _line(
        KPI_AVAILABILITY,
        None,
        raw_value=raw,
        normalised_value=None,  # §7.6.2: "no other normalisation"
        region_multiplier_applied=None,
        band=band,
        eligible_incidents=len(contributing),
        excluded_incidents=len(excluded),
        scc_minutes_deducted=sum(s.scc_minutes for s in sites),
        formula=(
            f"100 x (scheduled_uptime - unavailable_minutes) / scheduled_uptime = {_fmt(raw, '%')}; scheduled_uptime = "
            f"24 x 60 x {period.days} days x {in_scope} site(s) = {uptime} min; sites_in_scope: {scope_source}; "
            f"unavailable_minutes = {_fmt(_round(unavailable, 2), 'min')} = per-site union of this vendor's service-affecting "
            f"outages clipped to the period, minus stop-clock minutes, minus planned-window minutes; {planned_note}"
            f"{band_note}{_band_note(KPI_AVAILABILITY, bands) if banded else ''}{target_note}; excluded: {_reason_summary(excluded)}"
        ),
        yaml_path=PATH_SEP.join(
            [path for path in (_band_path(terms, KPI_AVAILABILITY), "sla_terms.default.availability_target_pct") if path and _resolves(terms, path)]
            or ["sla_terms.version"]
        ),
        evidence={
            "sites": [
                {
                    "site_id": s.site_id,
                    "incidents": list(s.incidents),
                    "gross_minutes": _round(s.gross_minutes, 2),
                    "scc_minutes": s.scc_minutes,
                    "planned_minutes": s.planned_minutes,
                    "unavailable_minutes": _round(s.unavailable_minutes, 2),
                }
                for s in sites
            ],
            "sites_in_scope": in_scope,
            "sites_in_scope_source": scope_source,
            "scheduled_uptime_minutes": uptime,
            "unavailable_minutes": _round(unavailable, 2),
            "planned_windows_excluded": planned_minutes_fn is not None,
            "excluded": excluded,
        },
    )


def build_lines(
    attributed: Sequence[IncidentFacts],
    intersecting: Sequence[IncidentFacts],
    *,
    terms: SlaTerms,
    vendor_code: str,
    period: Period,
    planned_minutes_fn: PlannedMinutesFn | None = None,
) -> list[LineResult]:
    """All 22 lines of ``LINE_SHAPE`` from facts alone. Pure: no session, no clock, no I/O.

    ``attributed`` are the incidents whose outage STARTED in the period (reading 2);
    ``intersecting`` are those whose outage OVERLAPS it (availability only, reading 9).
    """
    bands = dict(terms.scorecards.get("bands") or {})
    by_key: dict[tuple[str, str | None], LineResult] = {}
    for priority in (*PRIORITIES, None):
        pool = [f for f in attributed if priority is None or f.priority == priority]
        by_key[(KPI_MTTA, priority)] = _mtta_line(pool, priority, terms, vendor_code)
        mttr, sla = _restore_lines(pool, priority, terms, vendor_code, period, bands)
        by_key[(KPI_ADJ_MTTR, priority)] = mttr
        by_key[(KPI_SLA_COMPLIANCE, priority)] = sla
        by_key[(KPI_NOTE_COMPLIANCE, priority)] = _note_line(pool, priority, terms, vendor_code, period, bands)
    by_key[(KPI_REPEAT_FAULT, None)] = _repeat_line(attributed, terms)
    by_key[(KPI_AVAILABILITY, None)] = _availability_line(intersecting, terms, vendor_code, period, bands, planned_minutes_fn)
    return [by_key[key] for key in LINE_SHAPE]


# ----------------------------------------------------------------------------- reading the rows


def _chunks(values: Sequence[str], size: int = 500) -> Iterable[Sequence[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def gather_facts(session: Session, cfg: OperatorConfig, vendor: VendorRow, period: Period) -> tuple[list[IncidentFacts], list[IncidentFacts]]:
    """``(attributed, intersecting)`` for one vendor and period. Reads only; never writes.

    Operator-scoped in the WHERE clause, and by ``vendor_id`` -- call
    ``backfill_incident_vendor_ids`` first, because ``vendor_id`` is not stamped at ASSIGN
    time. Ordered by ``(start, incident_number)`` so everything downstream is deterministic.
    """
    start_expr = func.coalesce(IncidentRow.failure_time, IncidentRow.outage_start_at, IncidentRow.created_at)
    rows = session.scalars(
        select(IncidentRow)
        .where(
            IncidentRow.operator_id == vendor.operator_id,
            IncidentRow.vendor_id == vendor.id,
            start_expr < period.end,
            or_(start_expr >= period.start, IncidentRow.restored_at.is_(None), IncidentRow.restored_at > period.start),
        )
        .order_by(start_expr, IncidentRow.incident_number)
    ).all()
    ids = [r.id for r in rows]
    notes: dict[str, list[datetime]] = {}
    events: dict[str, list[ClockEventRow]] = {}
    for chunk in _chunks(ids):
        # timestamps and author roles ONLY: a note's body never reaches a vendor's number
        for incident_id, created_at, role, source in session.execute(
            select(WorkNoteRow.incident_id, WorkNoteRow.created_at, WorkNoteRow.author_role, WorkNoteRow.source)
            .where(WorkNoteRow.incident_id.in_(chunk))
            .order_by(WorkNoteRow.created_at, WorkNoteRow.id)
        ):
            if (role or "").upper() in VENDOR_NOTE_ROLES or (source or "").lower() in VENDOR_NOTE_SOURCES:
                notes.setdefault(incident_id, []).append(created_at)
        for ev in session.scalars(
            select(ClockEventRow).where(ClockEventRow.incident_id.in_(chunk)).order_by(ClockEventRow.started_at, ClockEventRow.created_at, ClockEventRow.id)
        ):
            events.setdefault(ev.incident_id, []).append(ev)

    attributed: list[IncidentFacts] = []
    intersecting: list[IncidentFacts] = []
    for r in rows:
        f = IncidentFacts(
            incident_id=r.id,
            incident_number=r.incident_number,
            priority=r.priority,
            status=r.status,
            site_id=r.site_id,
            region_code=r.region_code,
            failure_domain=r.failure_domain,
            started_at=incident_as_of(r),
            region_multiplier=_multiplier(cfg, r.region_code),
            planned_maintenance=bool(r.planned_maintenance),
            service_affecting=bool(r.service_affecting),
            escalated_at=r.escalated_at,
            first_vendor_note_at=r.first_vendor_note_at,
            restored_at=r.restored_at,
            restored_source=r.restored_source,
            vendor_note_times=tuple(notes.get(r.id, ())),
            clock_events=tuple(events.get(r.id, ())),
        )
        if period.start <= f.started_at < period.end:
            attributed.append(f)
        if f.started_at < period.end and (f.restored_at is None or f.restored_at > period.start):
            intersecting.append(f)
    return attributed, intersecting


def _maintenance_on() -> bool:
    """``MAINTENANCE_ENABLED``, spelled out again rather than imported, so that a process with
    the maintenance lane off does not import that lane to find out it is off."""
    return (os.getenv(_MAINTENANCE_ENV) or "").strip().lower() in _TRUE


def _planned_minutes_fn(session: Session, operator_id: str) -> PlannedMinutesFn | None:
    """``services.maintenance.planned_minutes`` bound to this operator, or ``None`` while that
    lane is off. ``None`` is reported on the availability line as "NOT excluded" -- a lane
    that is off has not told us there were no windows."""
    if not _maintenance_on():
        return None
    from noc_agents.services.maintenance import planned_minutes  # lazy: see _maintenance_on

    return lambda site_id, start, end: planned_minutes(session, operator_id, site_id, period_start=start, period_end=end)


# ------------------------------------------------------------------------------ one computation


@dataclass(frozen=True)
class ScorecardComputation:
    """What ``compute_vendor_period`` found. Nothing here has been written anywhere."""

    vendor_id: str
    vendor_code: str
    period: Period
    lines: tuple[LineResult, ...]
    gate: GateResult
    discipline: dict[str, Any]
    terms_info: dict[str, Any]
    sla_terms_version: str
    attributed_incidents: int
    intersecting_incidents: int


def _scorecard_knob(terms: SlaTerms, key: str) -> Any:
    value = terms.scorecards.get(key)
    if value is None:
        raise ValueError(f"{terms.path}: scorecards.{key} is required -- a policy value is never guessed")
    return value


def _terms_info(terms: SlaTerms, vendor_code: str, period: Period, planned_excluded: bool) -> dict[str, Any]:
    """What the card stood on -- above all whether it was a contract (§7.6.6)."""
    vendor_terms = terms.vendor(vendor_code)
    has_contract = bool(vendor_terms and vendor_terms.has_contract)
    band_sources = {p: terms.bands_for(vendor_code, p).yaml_path for p in PRIORITIES}
    if has_contract:
        notice = f"contract {vendor_terms.contract_ref}"
    elif vendor_terms is not None and vendor_terms.contract_is_synthetic:
        notice = (
            f"defaults, not contract: {vendor_terms.contract_ref} is a SYNTHETIC sample, not an agreement anyone signed. "
            "Every band, target and credit figure on this card is a placeholder until Supply Chain supplies the real terms."
        )
    else:
        notice = "defaults, not contract: no contract has been read for this vendor; bands are the operator's default SLA minutes."
    return {
        "version": terms.version,
        "source": terms.source,
        "file": terms.path.name,
        "vendor_code": vendor_code,
        "basis": "CONTRACT" if has_contract else "DEFAULTS_NOT_CONTRACT",
        "notice": notice,
        "contract_ref": vendor_terms.contract_ref if vendor_terms else None,
        "contract_is_synthetic": bool(vendor_terms.contract_is_synthetic) if vendor_terms else False,
        "credit_shape": terms.credit_shape_for(vendor_code),
        "credit_pct": list(vendor_terms.credit_pct) if vendor_terms else [],
        "band_paths": band_sources,
        "bands_from_operator_profile": sorted(p for p in PRIORITIES if terms.bands_for(vendor_code, p).source == SOURCE_SLA_MINUTES),
        "planned_windows_excluded": planned_excluded,
        "period": {"label": period.label, "timezone": period.timezone, "start": period.start.isoformat(), "end": period.end.isoformat(), "days": period.days},
    }


def compute_vendor_period(session: Session, cfg: OperatorConfig, vendor: VendorRow, period: Period | str, *, terms: SlaTerms) -> ScorecardComputation:
    """The numbers for one vendor and one period (spec §5.3.17 ``compute_vendor_period``).

    Reads and returns; writes nothing. ``compute_scorecard`` is the function that persists.
    """
    if isinstance(period, str):
        period = period_bounds(period, cfg.timezone)
    kind = str(_scorecard_knob(terms, "period")).upper()
    if kind != "MONTH":
        raise ValueError(f"scorecards.period={kind!r} is not supported; only MONTH is defined by §7.6")
    threshold = float(_scorecard_knob(terms, "max_inferred_restore_pct"))
    if not 0 <= threshold < 100:  # the table's CHECK bounds it the same way; fail here with a readable error
        raise ValueError(f"{terms.path}: scorecards.max_inferred_restore_pct must be in [0, 100), not {threshold:g}")
    late_threshold = int(terms.scorecards.get("late_scc_opening_minutes") or LATE_OPENING_THRESHOLD_MIN)

    attributed, intersecting = gather_facts(session, cfg, vendor, period)
    planned_fn = _planned_minutes_fn(session, vendor.operator_id)
    lines = build_lines(attributed, intersecting, terms=terms, vendor_code=vendor.code, period=period, planned_minutes_fn=planned_fn)
    eligible = [f for f in attributed if ineligibility(f) is None]
    return ScorecardComputation(
        vendor_id=vendor.id,
        vendor_code=vendor.code,
        period=period,
        lines=tuple(lines),
        gate=data_quality_gate(attributed, threshold),
        discipline=operator_discipline_counters(eligible, late_threshold_min=late_threshold),
        terms_info=_terms_info(terms, vendor.code, period, planned_fn is not None),
        sla_terms_version=terms.version,
        attributed_incidents=len(attributed),
        intersecting_incidents=len(intersecting),
    )


# ------------------------------------------------------------------------ shadow rule, credits


def shadow_required_for(session: Session, *, operator_id: str, vendor_id: str, period: str, sla_terms_version: str) -> bool:
    """§7.6.2's shadow rule, decided from the table -- never from a flag somebody passed in.

    Shadow review is required UNLESS an EARLIER period's card for this same vendor row, under
    this same ``sla_terms.version``, has already been released (PUBLISHED or FINAL). That one
    test covers all three cases the spec names:

    * a vendor's first period -- nothing earlier exists;
    * the first period after a terms change -- nothing earlier exists under the NEW version;
    * a re-contracted vendor -- that is a new ``vendors`` row, so nothing earlier exists for it.

    "Released" and not merely "computed": if July's card was computed and never looked at,
    August is still the first card any human will inspect. "Released" also means released BY
    THE SERVICE: the query requires the ``scorecard.published`` audit row ``publish_scorecard``
    writes, so a predecessor planted PUBLISHED by raw SQL or a bulk ``update()`` exempts nobody.
    The same query runs inside the mapper guards (``models_scorecards.earlier_released_card_exists``):
    a card's ``shadow_required`` is checked against it on INSERT and again at the moment of
    release, so the chain of "somebody named looked at this vendor's numbers under these
    terms" cannot be skipped by planting a column value.
    """
    return not earlier_released_card_exists(
        session, operator_id=operator_id, vendor_id=vendor_id, period=period, sla_terms_version=sla_terms_version
    )


def _red_streak(session: Session, *, operator_id: str, vendor_id: str, period: str, kpi: str) -> tuple[int, list[str]]:
    """How many IMMEDIATELY preceding periods had this vendor-level line RED on a RELEASED
    card (reading 13). An unreleased or withheld month breaks the chain: a RED nobody has
    stood behind is not an occurrence, and under-claiming is the safe error for a proposal."""
    streak, periods, cursor = 0, [], period
    for _ in range(36):  # three years is far past any credit table; also bounds the walk
        cursor = previous_period(cursor)
        band = session.scalar(
            select(VendorScorecardLineRow.band)
            .join(VendorScorecardRow, VendorScorecardRow.id == VendorScorecardLineRow.scorecard_id)
            .where(
                VendorScorecardRow.operator_id == operator_id,
                VendorScorecardRow.vendor_id == vendor_id,
                VendorScorecardRow.period == cursor,
                VendorScorecardRow.status.in_((STATUS_PUBLISHED, STATUS_FINAL)),
                VendorScorecardLineRow.kpi == kpi,
                VendorScorecardLineRow.priority.is_(None),
            )
        )
        if band != BAND_RED:
            break
        streak += 1
        periods.append(cursor)
    return streak, periods


def _with_credits(session: Session, comp: ScorecardComputation, *, operator_id: str, terms: SlaTerms) -> list[LineResult]:
    """Attach PROPOSED credits (reading 13). Never on a WITHHELD card; never anything but PROPOSED."""
    lines = list(comp.lines)
    shape = (terms.credit_shape_for(comp.vendor_code) or "none").strip().lower()
    vendor_terms = terms.vendor(comp.vendor_code)
    pcts = tuple(vendor_terms.credit_pct) if vendor_terms else ()
    if not comp.gate.passed or shape == "none" or not pcts or shape not in ("per_occurrence", "escalating_consecutive"):
        return lines  # an unknown shape proposes nothing: a credit is never guessed
    key, _ = _vendor_raw_block(terms, comp.vendor_code)
    out: list[LineResult] = []
    for line in lines:
        if line.priority is not None or line.band != BAND_RED:
            out.append(line)
            continue
        streak, periods = (0, [])
        if shape == "escalating_consecutive":
            streak, periods = _red_streak(session, operator_id=operator_id, vendor_id=comp.vendor_id, period=comp.period.label, kpi=line.kpi)
        pct = float(pcts[min(streak, len(pcts) - 1)])
        synthetic = " -- SYNTHETIC contract: defaults, not contract" if comp.terms_info.get("basis") != "CONTRACT" else ""
        out.append(
            replace(
                line,
                proposed_credit_pct=pct,
                credit_status=CREDIT_PROPOSED,
                formula=line.formula
                + f"; credit PROPOSED {pct:g} % (band RED on the vendor-level line; shape {shape}, step {min(streak, len(pcts) - 1) + 1} of "
                f"{len(pcts)}, sla_terms.vendors.{key}.credit_pct){synthetic}; a proposal only, until Supply Chain/Legal accepts",
                evidence={**line.evidence, "credit": {"shape": shape, "consecutive_prior_red_periods": periods, "pct": pct, "status": CREDIT_PROPOSED}},
            )
        )
    return out


# ---------------------------------------------------------------------------------- persisting


def _card_id(operator_id: str, vendor_id: str, period: str) -> str:
    """Deterministic, like ``vendor_id_for``: a rebuilt database re-issues the same card ids."""
    return str(uuid.uuid5(_CARD_NS, f"{operator_id}|{vendor_id}|{period}"))


def _line_id(card_id: str, kpi: str, priority: str | None) -> str:
    return str(uuid.uuid5(_CARD_NS, f"{card_id}|{kpi}|{priority or 'ALL'}"))


def _existing_card(session: Session, vendor: VendorRow, period: str) -> VendorScorecardRow | None:
    """The card for this vendor row and period, by its deterministic id or, failing that, by
    the natural key (a row written by another id scheme must still be found, never duplicated)."""
    return session.get(VendorScorecardRow, _card_id(vendor.operator_id, vendor.id, period)) or session.scalar(
        select(VendorScorecardRow).where(
            VendorScorecardRow.operator_id == vendor.operator_id,
            VendorScorecardRow.vendor_id == vendor.id,
            VendorScorecardRow.period == period,
        )
    )


def _audit(session: Session, card: VendorScorecardRow, *, actor: str, action: str, rationale: str, payload: dict[str, Any] | None = None) -> None:
    session.add(
        AuditRow(
            operator_id=card.operator_id,
            actor=actor,
            action=action,
            entity_type="vendor_scorecard",
            entity_id=card.id,
            rationale=rationale[:2000],
            payload_json=str({"vendor_id": card.vendor_id, "period": card.period, "status": card.status, **(payload or {})})[:2000],
        )
    )
    session.flush()


def lines_of(session: Session, card_id: str) -> list[VendorScorecardLineRow]:
    """A card's lines in reading order. Callers reach the CARD through ``_get_owned`` first."""
    return list(session.scalars(select(VendorScorecardLineRow).where(VendorScorecardLineRow.scorecard_id == card_id).order_by(VendorScorecardLineRow.seq)))


@contextmanager
def _computation_scope(session: Session, card_id: str):
    """Mark ``card_id`` as being written BY THE COMPUTATION for the duration of the block.

    The mapper guards in ``db/models_scorecards.py`` accept a change to a card's evidence or
    status (before release) only while the card's id is in ``session.info[COMPUTATION_SCOPE_KEY]``
    -- and only if ``computed_by_run_id`` names a real scorecard run. This is what makes the
    "recompute" exemption unforgeable from an ORM session that merely assigns attributes: the
    adversarial review's dressed-up edit (threshold 30, ``passed: True``,
    ``computed_by_run_id='not-a-real-run'``, status SHADOW, one write) has no scope and no run.
    Scoped to ONE card id, so a computation of card A cannot carry an edit of card B through the
    same flush.
    """
    scope: set[str] = session.info.setdefault(COMPUTATION_SCOPE_KEY, set())
    scope.add(card_id)
    try:
        yield
    finally:
        scope.discard(card_id)
        if not scope:
            session.info.pop(COMPUTATION_SCOPE_KEY, None)


def compute_scorecard(
    session: Session,
    cfg: OperatorConfig,
    vendor: VendorRow,
    period: str,
    *,
    terms: SlaTerms,
    run_id: str,
    now: datetime | None = None,
    actor: str = AGENT,
) -> VendorScorecardRow:
    """Compute and STORE one vendor's card. Flushes; the caller commits.

    The status written is decided here and only here, and is never PUBLISHED:

    * gate failed                      -> ``WITHHELD`` (reason on the card, no credit proposed);
    * else first period / new terms    -> ``SHADOW``;
    * else                             -> ``DRAFT``.

    Raises ``ValueError`` for a period that has not ended (a card for half a month would
    occupy the UNIQUE key with numbers that are wrong by construction) and
    ``ScorecardStateError`` for a card that is already PUBLISHED or FINAL: §7.6.6 makes a
    recompute after FINAL a 409, and this extends it to PUBLISHED, because a published card
    is a document a vendor has been shown and may be disputing line by line -- it is
    corrected through the dispute route, not rewritten underneath them.

    A recompute CLEARS any shadow review: the named human reviewed the previous numbers.
    """
    if not (run_id or "").strip():
        raise ValueError("run_id is required: a card must name the run that computed it")
    if vendor.operator_id != cfg.operator_id:
        raise ValueError("vendor belongs to another operator")
    at = now or utcnow()
    bounds = period_bounds(period, cfg.timezone)
    if at < bounds.end:
        raise ValueError(f"period {bounds.label} has not ended (it ends {bounds.end.isoformat()}Z)")

    card_id = _card_id(vendor.operator_id, vendor.id, bounds.label)
    card = _existing_card(session, vendor, bounds.label)
    if card is not None and card.status not in RECOMPUTABLE_STATUSES:
        raise ScorecardStateError(
            f"{REFUSAL_RELEASED}: scorecard {bounds.label} for {vendor.code} is {card.status} -- "
            "correct a published line through a dispute, or create a correction period (§7.6.6)"
        )

    comp = compute_vendor_period(session, cfg, vendor, bounds, terms=terms)
    needs_shadow = shadow_required_for(
        session, operator_id=vendor.operator_id, vendor_id=vendor.id, period=bounds.label, sla_terms_version=terms.version
    )
    status = STATUS_WITHHELD if not comp.gate.passed else (STATUS_SHADOW if needs_shadow else STATUS_DRAFT)
    lines = _with_credits(session, comp, operator_id=vendor.operator_id, terms=terms)

    recomputed = card is not None
    # Scope the row that will actually be written: an existing card keeps ITS id, which may be
    # outside the uuid5 scheme (``_existing_card`` finds it by natural key on purpose); only a
    # brand-new card gets the deterministic one.
    written_id = card.id if card is not None else card_id
    with _computation_scope(session, written_id):
        card = _write_card(session, card, written_id, vendor, bounds, comp, lines, needs_shadow, status, terms, run_id, at, recomputed)
    _audit(
        session,
        card,
        actor=actor,
        action="scorecard.recomputed" if recomputed else "scorecard.computed",
        rationale=comp.gate.reason or f"computed against sla_terms {terms.version}",
        payload={"run_id": run_id, "vendor": vendor.code, "gate_passed": comp.gate.passed, "shadow_required": needs_shadow},
    )
    return card


def _write_card(session, card, card_id, vendor, bounds, comp, lines, needs_shadow, status, terms, run_id, at, recomputed) -> VendorScorecardRow:
    """The one place a card's evidence is written. Called inside ``_computation_scope`` only."""
    if card is None:
        card = VendorScorecardRow(id=card_id, operator_id=vendor.operator_id, vendor_id=vendor.id, period=bounds.label)
        session.add(card)
    card.period_start, card.period_end = bounds.start, bounds.end
    card.status = status
    card.computed_at = at
    card.dispute_window_ends_at = card.published_at = card.finalised_at = None
    card.data_quality = comp.gate.as_json()
    card.dq_restored_incidents = comp.gate.restored_incidents
    card.dq_inferred_restores = comp.gate.inferred_restores
    card.dq_gate_threshold_pct = comp.gate.threshold_pct
    card.discipline = comp.discipline
    card.shadow_required = 1 if needs_shadow else 0
    card.shadow_reviewed_by = card.shadow_reviewed_at = None
    card.sla_terms_version = terms.version
    card.terms = comp.terms_info
    card.computed_by_run_id = run_id

    existing = {(row.kpi, row.priority): row for row in lines_of(session, card.id)} if recomputed else {}
    for seq, line in enumerate(lines):
        row = existing.pop(line.key, None)
        if row is None:
            row = VendorScorecardLineRow(id=_line_id(card.id, line.kpi, line.priority), scorecard_id=card.id, kpi=line.kpi, priority=line.priority)
            session.add(row)
        row.seq = seq
        row.raw_value, row.normalised_value = line.raw_value, line.normalised_value
        row.region_multiplier_applied = line.region_multiplier_applied
        row.unit, row.band = line.unit, line.band
        row.eligible_incidents, row.excluded_incidents = line.eligible_incidents, line.excluded_incidents
        row.scc_minutes_deducted = line.scc_minutes_deducted
        row.formula, row.yaml_path = line.formula, line.yaml_path
        row.evidence = line.evidence
        row.proposed_credit_pct, row.credit_status = line.proposed_credit_pct, line.credit_status
    for stale in existing.values():  # LINE_SHAPE shrank between releases
        session.delete(stale)
    session.flush()  # inside the scope: this is the write the mapper guards admit
    return card


@dataclass
class PeriodReport:
    """What one ``compute_period`` did. ``computed`` / ``skipped`` name vendors and statuses
    and go to the audit trail and to internal readers; ``summary()`` is COUNTS ONLY, because
    it is written into ``agent_run_steps``, which ``GET /api/v1/runs`` serves to every reader
    role while an unreleased card is a 404 to most of them (reading 15)."""

    period: str
    computed: list[str] = field(default_factory=list)  # "EGYPRO:SHADOW"
    skipped: list[str] = field(default_factory=list)  # "ATC: no incidents"
    card_ids: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return f"period={self.period} computed={len(self.computed)} skipped={len(self.skipped)}"

    def counts(self) -> dict[str, int]:
        return {"computed": len(self.computed), "skipped": len(self.skipped)}


def compute_period(
    session: Session,
    cfg: OperatorConfig,
    period: str,
    *,
    run_id: str,
    vendor_code: str | None = None,
    terms: SlaTerms | None = None,
    now: datetime | None = None,
    only_missing: bool = False,
    actor: str = AGENT,
) -> PeriodReport:
    """Every vendor's card for one period (or one vendor's). Flushes; the caller commits.

    Seeds the vendor rows and stamps ``incidents.vendor_id`` FIRST -- ``vendor_id`` is not
    written at ASSIGN time, so without the backfill a card would silently be computed over
    whichever incidents happened to have had a stop clock opened on them.

    A vendor with no incident in or across the period gets no card when every vendor is being
    computed (a card with nothing behind it is not evidence of anything, and would use up the
    vendor's one shadow review on a blank page). Naming a vendor computes it regardless.
    ``only_missing`` is the JOB's mode: it never recomputes a card that exists.
    """
    bounds = period_bounds(period, cfg.timezone)
    terms = terms or load_sla_terms(cfg=cfg)
    seed_vendors_from_contacts(session, cfg)
    backfill_incident_vendor_ids(session, cfg.operator_id)

    wanted = normalise_code(vendor_code) if vendor_code else None
    report = PeriodReport(period=bounds.label)
    period_first, period_last = _local_dates(bounds)
    for vendor in list_vendors(session, cfg.operator_id):
        if wanted and vendor.code != wanted:
            continue
        if vendor.active_from > period_last or (vendor.active_to is not None and vendor.active_to < period_first):
            continue  # these terms were not in force at any point in the period
        if only_missing and _existing_card(session, vendor, bounds.label) is not None:
            report.skipped.append(f"{vendor.code}: card exists")
            continue
        if not wanted:
            attributed, intersecting = gather_facts(session, cfg, vendor, bounds)
            if not attributed and not intersecting:
                report.skipped.append(f"{vendor.code}: no incidents")
                continue
        try:
            card = compute_scorecard(session, cfg, vendor, bounds.label, terms=terms, run_id=run_id, now=now, actor=actor)
        except ScorecardStateError as exc:
            if wanted:
                raise
            report.skipped.append(f"{vendor.code}: {exc}")
            continue
        report.computed.append(f"{vendor.code}:{card.status}")
        report.card_ids.append(card.id)
    if wanted and not report.computed and not report.skipped:
        raise LookupError(f"no vendor {wanted} with terms in force during {bounds.label}")
    # The per-vendor detail belongs to the audit trail (AUDIT_READERS), never to a run summary.
    session.add(
        AuditRow(
            operator_id=cfg.operator_id,
            actor=actor,
            action="scorecard.period_computed",
            entity_type="vendor_scorecard_period",
            entity_id=bounds.label,
            rationale=f"run {run_id}",
            payload_json=str({"period": bounds.label, "computed": list(report.computed), "skipped": list(report.skipped), "run_id": run_id})[:2000],
        )
    )
    session.flush()
    return report


def _local_dates(period: Period) -> tuple[date, date]:
    year, month = _parse_period(period.label)
    return date(year, month, 1), date(year, month, period.days)


# ---------------------------------------------------------------------------- human transitions


def _require_role(role: str, allowed: tuple[str, ...], what: str) -> None:
    if (role or "").strip() not in allowed:
        raise ScorecardPermissionError(f"role {role!r} may not {what}; only {list(allowed)}")


def _named_human(name: str | None, what: str) -> str:
    """The cleaned, visible human name, or ``ValueError``. Control/format characters (zero-width
    spaces included) are removed, whitespace stripped, and at least one letter is required --
    the same predicate the mapper guard applies at write time (``visible_human_name``)."""
    who = visible_human_name(name)
    if who is None:
        raise ValueError(f"{what} must be a visible human name (letters, not an automation name), not {name!r}")
    return who


def record_shadow_review(
    session: Session,
    card: VendorScorecardRow,
    *,
    reviewer: str,
    reviewer_role: str,
    rationale: str,
    now: datetime | None = None,
) -> VendorScorecardRow:
    """A named human says they have inspected this SHADOW card (§7.6.2). Flushes.

    It does NOT publish: reviewing and releasing are two acts with two audit rows, the same
    separation the regulatory lane keeps between approving a notice and sending it.
    ``rationale`` is mandatory (§7.6.6: a non-empty reason on every scorecard decision).
    """
    _require_role(reviewer_role, REVIEWER_ROLES, "record a shadow review")
    who = _named_human(reviewer, "the shadow reviewer")
    text = (rationale or "").strip()
    if not text:
        raise ValueError("rationale is required")
    if card.status != STATUS_SHADOW:
        raise ScorecardStateError(f"only a SHADOW card takes a shadow review; this one is {card.status}")
    if (card.shadow_reviewed_by or "").strip():
        raise ScorecardStateError(f"already shadow-reviewed by {card.shadow_reviewed_by}")
    card.shadow_reviewed_by = who
    card.shadow_reviewed_at = now or utcnow()
    session.flush()
    _audit(session, card, actor=who, action="scorecard.shadow_reviewed", rationale=text)
    return card


def publish_scorecard(
    session: Session,
    card: VendorScorecardRow,
    *,
    publisher: str,
    publisher_role: str,
    reason: str,
    terms: SlaTerms,
    cfg: OperatorConfig,
    now: datetime | None = None,
) -> VendorScorecardRow:
    """Release a card to the vendor and start the dispute window. Flushes; sends NOTHING.

    The two structural gates, checked here for a readable error and again by the table's
    CHECK constraints for every caller that is not this function:

    * data quality -- a WITHHELD card, or one whose recorded counts fail the gate, is refused;
    * shadow -- required if the card says so OR if the table says so now (``shadow_required_for``
      is re-asked, so clearing the column alone does not open the gate; a column that
      contradicts the table is refused outright). Publishing writes NO evidence column:
      ``shadow_required`` and the ``dq_*`` operands are the computation's, and the mapper
      guard freezes them at this moment.
    """
    _require_role(publisher_role, PUBLISHER_ROLES, "publish a scorecard")
    who = _named_human(publisher, "the publisher")
    text = (reason or "").strip()
    if not text:
        raise ValueError("reason is required")
    passed = gate_passes(card.dq_inferred_restores, card.dq_restored_incidents, card.dq_gate_threshold_pct)
    if card.status == STATUS_WITHHELD or not passed or card.data_quality.get("passed") is not True:
        raise ScorecardGateError(card.data_quality.get("reason") or "WITHHELD: the data-quality gate did not pass; fix the restore records and recompute")
    if card.status not in (STATUS_DRAFT, STATUS_SHADOW):
        raise ScorecardStateError(f"scorecard is already {card.status}")
    table_says = shadow_required_for(
        session, operator_id=card.operator_id, vendor_id=card.vendor_id, period=card.period, sla_terms_version=card.sla_terms_version
    )
    if not card.shadow_required and table_says:
        # The column is evidence written by the computation and frozen at release; a 0 the
        # table does not agree with is a planted or stale value, and publishing does not
        # "correct" it -- a recompute does.
        raise ScorecardGateError(
            "shadow_required is 0 but no earlier released card exists for this vendor under these terms; "
            "the card's record disagrees with the table -- recompute it before publishing"
        )
    if (card.shadow_required or table_says) and not (card.shadow_reviewed_by or "").strip():
        raise ScorecardGateError(
            "first period for this vendor under these terms: a named human must record a shadow review "
            "(POST /api/v1/scorecards/{id}/shadow-review) before it can be PUBLISHED"
        )
    at = now or utcnow()
    card.status = STATUS_PUBLISHED
    card.published_at = at
    card.dispute_window_ends_at = dispute_window_end(at, int(_scorecard_knob(terms, "dispute_window_working_days")), cfg.timezone)
    session.flush()
    _audit(session, card, actor=who, action="scorecard.published", rationale=text, payload={"dispute_window_ends_at": card.dispute_window_ends_at.isoformat()})
    return card


def finalise_scorecard(
    session: Session,
    card: VendorScorecardRow,
    *,
    actor: str,
    actor_role: str,
    reason: str = "",
    now: datetime | None = None,
) -> VendorScorecardRow:
    """PUBLISHED -> FINAL once the dispute window has closed and no line's dispute is OPEN."""
    _require_role(actor_role, PUBLISHER_ROLES, "finalise a scorecard")
    who = _named_human(actor, "the finaliser")
    if card.status != STATUS_PUBLISHED:
        raise ScorecardStateError(f"only a PUBLISHED card can be finalised; this one is {card.status}")
    at = now or utcnow()
    if card.dispute_window_ends_at is None or at < card.dispute_window_ends_at:
        raise ScorecardStateError("the dispute window is still open")
    if any(line.dispute_status == "OPEN" for line in lines_of(session, card.id)):
        raise ScorecardStateError("a line has an OPEN dispute; adjudicate it first")
    card.status = STATUS_FINAL
    card.finalised_at = at
    session.flush()
    _audit(session, card, actor=who, action="scorecard.finalised", rationale=(reason or "").strip() or "dispute window closed")
    return card


# ------------------------------------------------------------------------------ runs and the job


def compute_on_request(
    session: Session,
    settings: AppSettings,
    period: str,
    *,
    vendor_code: str | None = None,
    actor: str,
    now: datetime | None = None,
) -> tuple[PeriodReport, str]:
    """``POST /scorecards/compute``: one tracked run (``trigger="REQUEST"``) so that
    ``computed_by_run_id`` always names a real ``agent_runs`` row. Flushes; the caller commits."""
    # Lazy: ``graph.instrumentation`` pulls in the whole lifecycle graph, which a process that
    # only reads scorecards (or has the lane off) has no reason to pay for at import.
    from noc_agents.graph.instrumentation import RunTracker

    cfg = settings.operator
    period_bounds(period, cfg.timezone)  # validate before a run row is opened for a typo
    run = AgentRunRow(id=str(uuid.uuid4()), incident_id=None, operator_id=cfg.operator_id, graph_name=GRAPH_NAME, trigger="REQUEST", status="RUNNING", started_at=utcnow())
    session.add(run)
    session.flush()
    tracker = RunTracker(session, run)
    input_summary = f"period={period} vendor={vendor_code or '*'} requested_by={actor}"
    step = tracker.start_step(JOB_NAME, AGENT, input_summary)
    try:
        report = compute_period(session, cfg, period, run_id=run.id, vendor_code=vendor_code, now=now, actor=actor)
    except Exception as exc:
        # The failure may have come out of a flush (a mapper guard, an IntegrityError), which
        # leaves the session needing a rollback: writing the step on it would raise
        # PendingRollbackError over the real error, the route would answer 500 and the run row
        # would be lost with the rollback. So: roll back first, then record the failure as its
        # own committed run -- the only commit this function makes, and it persists nothing but
        # that record -- and re-raise the ORIGINAL exception for the caller to map (400/404/409).
        session.rollback()
        log.warning("scorecard: compute %s failed (%s)", input_summary, failure_reason(exc), exc_info=exc)  # the log, not the run row
        _record_failed_run(session, cfg.operator_id, run.id, input_summary, exc)
        raise
    tracker.complete_step(step, status=SUCCEEDED, output_summary=report.summary(), rationale="arithmetic only; nothing published, nothing sent; per-vendor detail in audit_events", confidence=None)
    tracker.finish_run(SUCCEEDED)
    return report, run.id


def _record_failed_run(session: Session, operator_id: str, run_id: str, input_summary: str, exc: BaseException) -> None:
    """After a rollback: a FAILED ``agent_runs`` row (same id, so the caller's ``run_id`` stays
    true) with one FAILED step naming the error by ``failure_reason`` -- the class and, for an
    IntegrityError, the constraint clause; never ``str(exc)``, which for a database error
    carries the bound parameters of an unreleased card's lines into a READERS surface --
    committed on its own. Best effort: a second failure here must not mask the first, so it is
    logged and swallowed."""
    from noc_agents.graph.instrumentation import RunTracker

    try:
        started = utcnow()
        run = AgentRunRow(id=run_id, incident_id=None, operator_id=operator_id, graph_name=GRAPH_NAME, trigger="REQUEST", status="RUNNING", started_at=started)
        session.add(run)
        session.flush()
        tracker = RunTracker(session, run)
        step = tracker.start_step(JOB_NAME, AGENT, input_summary)
        reason = failure_reason(exc)
        tracker.complete_step(step, status=FAILED, output_summary="", rationale=reason, confidence=None)
        tracker.finish_run(FAILED, error=reason)
        session.commit()
    except Exception:  # noqa: BLE001 -- the original error is the one the caller must see
        log.exception("scorecard: could not record the failed run %s", run_id)
        session.rollback()


def _scheduler_run_id(session: Session, operator_id: str) -> str | None:
    """The ``agent_runs`` row ``scheduler.loop.run_job`` opened for THIS execution: it is
    added and flushed on the same session just before the job function is called."""
    return session.scalar(
        select(AgentRunRow.id)
        .where(AgentRunRow.operator_id == operator_id, AgentRunRow.graph_name == GRAPH_NAME, AgentRunRow.trigger == "SCHEDULE", AgentRunRow.status == "RUNNING")
        .order_by(AgentRunRow.started_at.desc())
        .limit(1)
    )


def close_periods(session: Session, settings: AppSettings) -> JobResult:
    """The hourly job (§7.6.4 ``jobs/scorecards.close_periods``). Commits.

    Inert unless ``SCORECARDS_ENABLED`` is on -- re-checked HERE, not only on the card,
    because ``POST /scheduler/run/{job}`` runs a job whatever its card says. When it does
    run it computes the last ENDED period for each vendor that has no card for it yet, and
    stops: it never recomputes, never publishes, never finalises and never sends. A missing
    or unversioned terms file, or a write the table refuses, raises ``ScorecardJobError`` --
    carrying ``failure_reason`` of the cause, never its text -- so the runner records a FAILED
    run: a scorecard job "must stop, not compute on air" (``services.vendors.load_sla_terms``).
    """
    if not lane_enabled():
        return JobResult(summary=f"{FLAG} is off: nothing computed", rationale="lane disabled; the job is inert")
    cfg = settings.operator
    period = last_ended_period(tz_name=cfg.timezone)
    run_id = _scheduler_run_id(session, cfg.operator_id)
    own_run = None
    if run_id is None:
        # Called outside ``scheduler.loop.run_job`` (a script, a test driving the card's ``fn``
        # directly): there is no run row to cite, and ``computed_by_run_id`` must never name a
        # run that does not exist -- so open one, and close it below.
        own_run = AgentRunRow(id=str(uuid.uuid4()), incident_id=None, operator_id=cfg.operator_id, graph_name=GRAPH_NAME, trigger="SCHEDULE", status="RUNNING", started_at=utcnow())
        session.add(own_run)
        session.flush()
        run_id = own_run.id
    try:
        report = compute_period(session, cfg, period, run_id=run_id, only_missing=True)
    except Exception as exc:
        # ``scheduler.loop.run_job`` records ``f"{type(exc).__name__}: {exc}"`` of whatever a
        # job raises -- into agent_runs.error_summary, the step and the agent.run.finished
        # event -- and for a database error that text carries an unreleased card's line values.
        # So the job never lets the original out: it rolls back and raises a ScorecardJobError
        # whose message is the safe reason. The full exception goes to the log only.
        session.rollback()
        log.warning("scorecard: %s failed for period %s (%s)", JOB_NAME, period, failure_reason(exc), exc_info=exc)
        raise ScorecardJobError(f"{failure_reason(exc)} (period {period}; the full error is in the application log)") from None
    if own_run is not None:
        own_run.status, own_run.finished_at = SUCCEEDED, utcnow()
    session.commit()
    return JobResult(
        summary=report.summary(),
        rationale="computed unreleased cards only; publishing is a named human's act; per-vendor detail in audit_events",
        tools=({"name": "scorecard.compute_period", "ok": True, "period": report.period, **report.counts()},),
    )


#: The job's card (spec §4.4: ``JobCard("scorecard_close", 3600, ..., "SCORECARDS_ENABLED",
#: "SlaScorecardAgent")``). Registered in ``scheduler/loop.py`` (``_scorecard_job()`` in
#: ``SCHEDULED_JOBS``, imported lazily like the other lanes' cards). ``default_enabled=False``
#: so ``/scheduler/status`` reports the job as off while the flag is unset, rather than
#: claiming it is enabled and producing nothing; ``close_periods`` re-checks the flag itself.
SCORECARD_JOB = JobCard(
    JOB_NAME,
    INTERVAL_S,
    close_periods,
    ENABLED_ENV,
    AGENT,
    GRAPH_NAME,
    max_seconds=120,
    default_enabled=False,
)


# ----------------------------------------------------------------------------------- serializing


def line_out(row: VendorScorecardLineRow, *, with_evidence: bool = True) -> dict[str, Any]:
    out = {
        "id": row.id,
        "kpi": row.kpi,
        "priority": row.priority,
        "raw_value": row.raw_value,
        "normalised_value": row.normalised_value,
        "normalised_label": "contract-agreed regional allowance",  # §7.6.1: how the UI must label it
        "region_multiplier_applied": row.region_multiplier_applied,
        "unit": row.unit,
        "band": row.band,
        "eligible_incidents": row.eligible_incidents,
        "excluded_incidents": row.excluded_incidents,
        "scc_minutes_deducted": row.scc_minutes_deducted,
        "formula": row.formula,
        "yaml_path": row.yaml_path,
        "proposed_credit_pct": row.proposed_credit_pct,
        "credit_status": row.credit_status,
        "dispute_task_id": row.dispute_task_id,
        "dispute_status": row.dispute_status,
        "adjusted_value": row.adjusted_value,
        "adjudicated_by": row.adjudicated_by,
        "adjudication_reason": row.adjudication_reason,
    }
    if with_evidence:
        out["evidence"] = row.evidence
    return out


def scorecard_out(card: VendorScorecardRow, vendor: VendorRow | None = None, lines: Sequence[VendorScorecardLineRow] | None = None) -> dict[str, Any]:
    """Wire shape of one card; every timestamp Z-stamped (§7.0.6). ``terms_notice`` is on the
    top level on purpose: "defaults, not contract" is not something to find three keys deep."""
    terms = card.terms
    out: dict[str, Any] = {
        "id": card.id,
        "operator_id": card.operator_id,
        "vendor_id": card.vendor_id,
        "vendor_code": vendor.code if vendor else terms.get("vendor_code"),
        "vendor_name": vendor.display_name if vendor else None,
        "period": card.period,
        "period_start": z_utc(card.period_start),
        "period_end": z_utc(card.period_end),
        "status": card.status,
        "computed_at": z_utc(card.computed_at),
        "published_at": z_utc(card.published_at),
        "dispute_window_ends_at": z_utc(card.dispute_window_ends_at),
        "finalised_at": z_utc(card.finalised_at),
        "data_quality": card.data_quality,
        "discipline": card.discipline,
        "shadow_required": bool(card.shadow_required),
        "shadow_reviewed_by": card.shadow_reviewed_by,
        "shadow_reviewed_at": z_utc(card.shadow_reviewed_at),
        "sla_terms_version": card.sla_terms_version,
        "terms": terms,
        "terms_basis": terms.get("basis"),
        "terms_notice": terms.get("notice"),
        "narrative": card.narrative,
        "narrative_ai_assisted": bool(card.narrative_ai_assisted),
        "computed_by_run_id": card.computed_by_run_id,
    }
    if lines is not None:
        out["lines"] = [line_out(row) for row in lines]
    return out
