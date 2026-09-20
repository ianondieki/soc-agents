"""Capacity observations, the reading built from them, and the advisory that reading may
justify (spec §7.5.1/§7.5.3; ``CAPACITY_ENABLED=false``) — Phase 5 Lane 5A.

``CAPACITY_ENABLED`` defaults to **false**. With it off nothing here runs: no observation can
be ingested, no advisory can open, the scan job reports itself off and every route in
``api/routers/capacity.py`` answers 404. The system behaves exactly as it does today.

FOUR IDEAS CARRY THIS HALF OF THE LANE
======================================

**1. An advisory is advice, and advice is structurally incapable of acting.**

§7.5.3 is one sentence long on this point — "advisory routed to Planning; never an upgrade
order" — and the whole module is arranged so that sentence cannot be violated by accident.
:func:`open_advisory` writes exactly two rows, a ``capacity_advisories`` row and its
``AuditRow``, and it imports nothing that can schedule work. There is no ``accept``, no
``raise_window_for``, no ``propose_task`` and no HITL card, for the reason
``services.maintenance.stop_clock_proposal`` has no acceptance helper beside it: a convenience
wrapper is one refactor away from being called by a job, and the day that happens a counter
export starts taking customers off air. If this lane ever needs to turn advice into a window,
the route is the one every other planned outage takes — a human, a plan, ``APPROVE_SCHEDULE``
and ``APPROVE_MAINTENANCE_WINDOW`` — and the advisory is an argument made to that human, not a
shortcut past them. ``tests/unit/test_capacity.py`` pins the empty tables after an advisory
opens.

**2. Not enough data is an answer, and it is said out loud.**

§7.4.5 warns that "thin Kenyan volumes make z-scores noisy at first" and prescribes an
"advisory only, minimum count threshold". The danger is not a wrong number; it is a *confident*
number. A cell with two samples at 95 % is not a congested cell, it is two samples, and a site
whose feed stopped on Tuesday is not a quiet site. So every reading carries an explicit
verdict, and :data:`VERDICT_INSUFFICIENT_DATA` withholds ``peak_pct`` and
``mean_busy_hour_pct`` entirely rather than returning a figure somebody will quote in a
meeting. The floor is two numbers, both configurable (``capacity.min_observations``,
``capacity.min_days``), both defaulted from the spec's own trigger shape, and both reported on
every reading next to what was actually observed, so "we do not know yet" is legible as a
measurement of the *feed* rather than of the network.

**3. A site that was deliberately off air did not have a capacity problem.**

A battery swap takes a cell down; its PRB usage for those hours is a fact about a switched-off
radio. Counting it is bad in both directions — it drags a busy cell's average down and, when a
window ends mid-hour, it invents a spike. So samples inside an approved window are excluded,
and the exclusion is asked of ``services.maintenance``, never recomputed here:
:func:`maintenance.planned_minutes` says whether the period contained any approved outage at
all (one query, the usual answer being zero) and :func:`maintenance.is_planned` decides each
surviving sample. Only SCHEDULED windows count, because that is the discipline the maintenance
lane keeps — a PROPOSED window is a plan, not a planned outage.

And when ``MAINTENANCE_ENABLED`` is off, that exclusion **cannot** be applied: ``is_planned``
returns ``None`` by design on a deployment that does not track planned work. The reading then
says :data:`PLANNED_NOT_TRACKED` in so many words instead of quietly presenting unexcluded
numbers as excluded ones. Same rule as the rain guard: an absence of evidence is never
rendered as evidence of absence.

**4. Idempotence is a property of the arithmetic, not of a constraint.**

A PRB export gets re-sent — by a retry, by a person who was not sure the first upload worked,
by a feed that restates a corrected value. ``capacity_observations`` has no UNIQUE constraint
(see the model docstring), so :func:`ingest_observations` skips samples it already holds, and,
independently, every count in :func:`read_capacity` is over DISTINCT busy-hour slots. Two
belts: the first keeps the table clean, the second means that even if a duplicate lands the
day's hour count cannot be inflated into an advisory.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from statistics import fmean
from typing import TYPE_CHECKING, Any, Iterable, Sequence

from sqlalchemy.orm import Session

from noc_agents.api.deps import _owned, _settings
from noc_agents.config import OperatorConfig
from noc_agents.db.models import AuditRow, utcnow
from noc_agents.db.models_capacity import (
    ADVISORY_ACKNOWLEDGED,
    ADVISORY_CLOSED,
    ADVISORY_OPEN,
    CAPACITY_METRICS,
    CAPACITY_SOURCES,
    DEFAULT_METRIC,
    ROUTED_TO_PLANNING,
    CapacityAdvisoryRow,
    CapacityObservationRow,
)
from noc_agents.scheduler import JobCard, JobResult
from noc_agents.services import maintenance, uploads
from noc_agents.services.clock import eat_tz, to_eat, z_utc

if TYPE_CHECKING:  # typing only
    from noc_agents.config import AppSettings

log = logging.getLogger(__name__)

UTC = timezone.utc

__all__ = [
    "CAPACITY_ENABLED_ENV",
    "CAPACITY_SCAN_JOB",
    "CapacityReading",
    "CapacityRejected",
    "DayEvidence",
    "IngestReport",
    "ObservationDraft",
    "PLANNED_APPLIED",
    "PLANNED_NOT_TRACKED",
    "VERDICT_BELOW_TRIGGER",
    "VERDICT_INSUFFICIENT_DATA",
    "VERDICT_SUSTAINED",
    "advisory_out",
    "capacity_config",
    "capacity_enabled",
    "capacity_scan",
    "ingest_observations",
    "observation_out",
    "open_advisory",
    "parse_csv",
    "read_capacity",
    "review_advisory",
    "site_readings",
]


# ---------------------------------------------------------------------------------- the flag

#: The lane's own flag, and it is deliberately not ``MAINTENANCE_ENABLED``.
#:
#: The two halves of Lane 5A share a spec section and nothing else. Planned maintenance books
#: outages: its blast radius is live customers off air, its audience is shift supervisors, and
#: what must be true before it is switched on is that the approval chain and the CA Condition
#: 9.1 reference are in place. Capacity ingest opens a **file-upload endpoint** and starts
#: writing advice about network dimensioning: its blast radius is a disk and a Planning inbox,
#: its audience is Planning, and what must be true before it is switched on is that somebody
#: trusts the counter export.
#:
#: Reusing ``MAINTENANCE_ENABLED`` would mean that arming the calendar also arms an upload
#: path, which is exactly the coupling ``COMPLAINTS_ENABLED`` refused to accept from
#: ``CONTRACTS_ENABLED`` ("turning on contract search must not also turn on a complaint
#: intake"). It would also make the useful combination impossible: a Planning team can run
#: ``CAPACITY_ENABLED=true`` with ``MAINTENANCE_ENABLED=false`` and get honest readings that
#: say, in the exclusion field, that planned work is not tracked on this deployment. The
#: dependency only runs one way — capacity reads maintenance and degrades loudly without it;
#: maintenance never reads capacity — so one flag cannot serve both without over-arming one of
#: them.
CAPACITY_ENABLED_ENV = "CAPACITY_ENABLED"

_TRUE = frozenset({"1", "true", "yes", "on"})


def capacity_enabled() -> bool:
    """``CAPACITY_ENABLED`` — default **false**, so the lane ships inert (§8.9.4).

    Read from the environment on every call, never cached at import: the test suite and the
    scheduler both flip this between runs, and a module-level constant would freeze whichever
    value happened to be set when the first import ran.
    """
    return (os.getenv(CAPACITY_ENABLED_ENV) or "").strip().lower() in _TRUE


# ------------------------------------------------------------------------------ the policy

#: §7.5.1's ``cfg.capacity`` block, verbatim, plus the three numbers that block does not name
#: but the code cannot work without. Every one is operator policy — §7.5.1 says so explicitly
#: of the trigger ("conventional trigger; operator policy, not a standard") — so none of them
#: is presented anywhere as a standard, and all of them are overridable per profile.
DEFAULT_CAPACITY: dict[str, Any] = {
    "metric": DEFAULT_METRIC,
    "prb_util_trigger_pct": 70.0,
    "sustained_days": 7,
    "consecutive_busy_hours": 3,
    # How far back a reading looks. NOT equal to sustained_days, on purpose: a 7-day lookback
    # would turn "7 sustained days" into "every single day of the last week", so one missed
    # feed day would defeat the rule permanently and silently. 14 days gives the seven days
    # room to be found while keeping the evidence recent enough to act on.
    "lookback_days": 14,
    # THE DATA FLOOR (idea 2 in the module docstring). Below either of these the reading is
    # INSUFFICIENT_DATA and no number is published. The defaults are the spec's own trigger
    # shape read as a minimum: you cannot honestly say anything about a cell until you hold at
    # least as much data as the rule needs to fire — sustained_days (7) distinct days and
    # sustained_days x consecutive_busy_hours (21) samples.
    "min_days": 7,
    "min_observations": 21,
}

CAPACITY_YAML_PATH = "capacity"

#: How far ahead of "now" a busy hour may be stamped before it is refused. A counter export
#: with a mis-set clock puts samples in next year; those samples then sit outside every
#: lookback window for months and reappear, all at once, as a sudden week of congestion. Two
#: hours of slack covers an export written just ahead of a slightly fast NTP-less server.
FUTURE_SLACK = timedelta(hours=2)

#: Verdicts. Three, and the first one is the point of the exercise.
VERDICT_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
VERDICT_SUSTAINED = "SUSTAINED_ABOVE_TRIGGER"
VERDICT_BELOW_TRIGGER = "BELOW_TRIGGER"

#: Whether planned-maintenance minutes could be taken out of the reading.
PLANNED_APPLIED = "APPLIED"
PLANNED_NOT_TRACKED = "NOT_TRACKED"

#: Who the audit trail records as the author of an advisory the scan job opened.
CAPACITY_AGENT = "agent:CapacityPlanningAgent"

ADVISORY_ENTITY_TYPE = "capacity_advisory"
OBSERVATION_ENTITY_TYPE = "capacity_observation"


def capacity_config(cfg: OperatorConfig) -> dict[str, Any]:
    """The ``capacity:`` block of the operator profile, over the §7.5.1 defaults.

    ``OperatorConfig`` does not declare a ``capacity`` field yet, and pydantic's default
    ``extra="ignore"`` means a ``capacity:`` block in the YAML is silently dropped until it
    does — so this reads defensively with ``getattr`` and falls back to the spec's own values.
    It starts honouring the profile the moment ``config.py`` grows the field, with no change
    here. (The pattern ``services/maintenance.py``, ``services/regulatory.py`` and
    ``services/memory.py`` all use.)
    """
    raw = getattr(cfg, CAPACITY_YAML_PATH, None)
    block: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
    merged = {**DEFAULT_CAPACITY}
    for key, value in block.items():
        if key in merged:
            merged[key] = value
    return merged


def _positive_int(cfg: OperatorConfig, key: str) -> int:
    try:
        return max(1, int(capacity_config(cfg)[key]))
    except (TypeError, ValueError):
        return int(DEFAULT_CAPACITY[key])


def trigger_pct(cfg: OperatorConfig) -> float:
    """The utilisation percentage at or above which a busy hour counts (§7.5.1, default 70)."""
    try:
        return float(capacity_config(cfg)["prb_util_trigger_pct"])
    except (TypeError, ValueError):
        return float(DEFAULT_CAPACITY["prb_util_trigger_pct"])


def sustained_days(cfg: OperatorConfig) -> int:
    """How many qualifying days make a trend (§7.5.1, default 7)."""
    return _positive_int(cfg, "sustained_days")


def consecutive_busy_hours(cfg: OperatorConfig) -> int:
    """How many busy hours in a day must clear the trigger for that day to qualify (default 3).

    **Interpretation, and it is a choice worth stating.** §7.5.1 names the knob
    ``consecutive_busy_hours`` but §7.5.3 states the rule as "for >= 3 busy hours/day", and the
    two do not say the same thing. This module implements the *count*, not adjacency: a
    busy-hour export lists the hours the OSS considered busy, and those need not be adjacent
    clock hours — a feed reporting 15:00, 17:00 and 19:00 describes a cell that is busy all
    afternoon, and an adjacency rule would file it as quiet. The adjacency figure is not thrown
    away: every day's evidence carries ``longest_run`` beside ``hours_at_or_above``, so a
    reviewer who disagrees with this reading of the spec can see both numbers on the advisory
    rather than having to re-derive one.
    """
    return _positive_int(cfg, "consecutive_busy_hours")


def lookback_days(cfg: OperatorConfig) -> int:
    """How many days of history a reading considers (default 14; see :data:`DEFAULT_CAPACITY`)."""
    return _positive_int(cfg, "lookback_days")


def min_days(cfg: OperatorConfig) -> int:
    """Distinct days of data below which the answer is "we do not know" (default 7)."""
    return _positive_int(cfg, "min_days")


def min_observations(cfg: OperatorConfig) -> int:
    """Samples below which the answer is "we do not know" (default 21)."""
    return _positive_int(cfg, "min_observations")


def metric_of(cfg: OperatorConfig) -> str:
    """The profile's capacity metric (§7.5.1, default ``DL_TOTAL_PRB_USAGE``)."""
    value = str(capacity_config(cfg).get("metric") or DEFAULT_METRIC).strip().upper()
    return value if value in CAPACITY_METRICS else DEFAULT_METRIC


# ------------------------------------------------------------------------------- rejections


class CapacityRejected(ValueError):
    """A submission this lane refuses, carrying **every** reason at once.

    The ``services/pir.py`` and ``services/complaints.py`` house style: a form that rejects one
    field per round trip teaches people to submit less, not better. For a CSV the stakes are
    higher still — a 3 000-row export returned with "row 12 is bad", fixed, and returned again
    with "row 40 is bad" is a morning gone.
    """

    def __init__(self, errors: Sequence[str]) -> None:
        self.errors: tuple[str, ...] = tuple(errors)
        super().__init__("; ".join(self.errors) or "rejected")


# ------------------------------------------------------------------------------- ingest


@dataclass(frozen=True)
class ObservationDraft:
    """One validated sample, not yet written. ``busy_hour_at`` is already naive UTC."""

    site_id: str
    cell_id: str | None
    metric: str
    value: float
    busy_hour_at: datetime
    source: str

    @property
    def key(self) -> tuple[str, str | None, str, datetime]:
        """The natural key the duplicate check uses (see the model's note on UNIQUE)."""
        return (self.site_id, self.cell_id, self.metric, self.busy_hour_at)


#: The timezone a naive ``busy_hour_at`` in a submission is taken to be in.
#:
#: The storage contract (§7.0.6) says a naive instant is UTC, and that is the default here as
#: it is everywhere else. But a PRB export off a Kenyan OSS is written in local time far more
#: often than not, and three hours of silent error is not a rounding detail: it moves every
#: sample out of the busy hour it describes and, worse, out of the maintenance window that
#: should have excluded it. So the assumption is a *parameter* with two legal values rather
#: than a guess, the ingest report always states which one was applied, and neither is
#: inferred from the data. Refusing to guess is the same call ``maintenance.next_due`` makes
#: for a consumption-driven plan.
NAIVE_TZ_UTC = "UTC"
NAIVE_TZ_EAT = "EAT"
NAIVE_TZ_CHOICES: tuple[str, ...] = (NAIVE_TZ_UTC, NAIVE_TZ_EAT)

#: Accepted spellings of a busy-hour timestamp, tried in order. ISO 8601 first (what a machine
#: writes), then the shapes a spreadsheet produces when somebody opens the export.
_TIMESTAMP_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%d/%m/%Y %H:%M",
)


def parse_busy_hour(raw: Any, *, naive_tz: str = NAIVE_TZ_UTC) -> datetime:
    """A busy-hour timestamp as naive UTC, or ``ValueError``.

    An aware value is converted and its own offset believed. A naive value is interpreted in
    ``naive_tz`` — see :data:`NAIVE_TZ_UTC`.
    """
    zone = (naive_tz or NAIVE_TZ_UTC).strip().upper()
    if zone not in NAIVE_TZ_CHOICES:
        raise ValueError(f"naive_tz must be one of {', '.join(NAIVE_TZ_CHOICES)}")

    if isinstance(raw, datetime):
        parsed: datetime | None = raw
    else:
        text = str(raw or "").strip()
        if not text:
            raise ValueError("busy_hour_at is required")
        parsed = None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            for fmt in _TIMESTAMP_FORMATS:
                try:
                    parsed = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
        if parsed is None:
            raise ValueError(f"busy_hour_at {text!r} is not a timestamp this lane recognises")

    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC).replace(tzinfo=None)
    if zone == NAIVE_TZ_EAT:
        return parsed.replace(tzinfo=eat_tz()).astimezone(UTC).replace(tzinfo=None)
    return parsed


def validate_observation(
    raw: dict[str, Any],
    *,
    cfg: OperatorConfig,
    naive_tz: str = NAIVE_TZ_UTC,
    default_source: str = "MANUAL",
    now: datetime | None = None,
) -> tuple[ObservationDraft | None, list[str]]:
    """One submitted sample as a draft, or ``(None, [every reason it was refused])``.

    The value range check is the one that matters. ``DL_TOTAL_PRB_USAGE`` is a percentage
    (3GPP TS 28.552), so 0-100 is not a formatting preference: a 700 is a units error — raw PRB
    counts, or per-mille — and a units error that reaches the table is an advisory somebody
    spends capital on.
    """
    errors: list[str] = []

    site_id = str(raw.get("site_id") or "").strip()
    if not site_id:
        errors.append("site_id is required")

    cell_raw = raw.get("cell_id")
    cell_id = str(cell_raw).strip() if cell_raw not in (None, "") else None

    metric = str(raw.get("metric") or metric_of(cfg)).strip().upper()
    if metric not in CAPACITY_METRICS:
        errors.append(f"metric {metric!r} is not one of {', '.join(CAPACITY_METRICS)}")

    value: float | None = None
    try:
        value = float(raw.get("value"))
    except (TypeError, ValueError):
        errors.append(f"value {raw.get('value')!r} is not a number")
    else:
        if not 0.0 <= value <= 100.0:
            errors.append(f"value {value} is outside 0-100 % ({metric} is a percentage; check the units)")

    busy_hour_at: datetime | None = None
    try:
        busy_hour_at = parse_busy_hour(raw.get("busy_hour_at"), naive_tz=naive_tz)
    except ValueError as exc:
        errors.append(str(exc))
    else:
        if busy_hour_at > (now or utcnow()) + FUTURE_SLACK:
            errors.append(f"busy_hour_at {busy_hour_at.isoformat()} is in the future; check the exporting clock")

    source = str(raw.get("source") or default_source).strip().upper()
    if source not in CAPACITY_SOURCES:
        errors.append(f"source {source!r} is not one of {', '.join(CAPACITY_SOURCES)}")

    if errors or value is None or busy_hour_at is None:
        return None, errors
    return ObservationDraft(site_id, cell_id, metric, value, busy_hour_at, source), []


#: §7.5.4's sample file (``data/seed/v2/capacity_sample.csv``) header. Column ORDER is free —
#: rows are mapped by header name — but a missing required column is refused rather than
#: defaulted, because a CSV with no ``busy_hour_at`` is not a capacity export with an omission,
#: it is a different file.
CSV_REQUIRED_COLUMNS: tuple[str, ...] = ("site_id", "metric", "value", "busy_hour_at")
CSV_OPTIONAL_COLUMNS: tuple[str, ...] = ("cell_id", "source")


def parse_csv(
    text: str,
    *,
    cfg: OperatorConfig,
    naive_tz: str = NAIVE_TZ_UTC,
    now: datetime | None = None,
) -> list[ObservationDraft]:
    """Every row of a capacity CSV as drafts, or :class:`CapacityRejected` with every error.

    All-or-nothing on purpose. A half-ingested week is the worst possible state for this lane:
    the missing days do not announce themselves, they simply make a busy cell look like a cell
    with a thin feed, and the reading that comes back says INSUFFICIENT_DATA for a reason that
    has nothing to do with the network. Refusing the file keeps the failure where somebody can
    see it.

    Parsing is ``uploads.iter_csv_rows`` — the ``csv`` module with §7.9.5's 100 000-row cap —
    and not a second CSV reader written here.
    """
    rows = uploads.iter_csv_rows(text)
    try:
        header = next(rows)
    except StopIteration:
        raise CapacityRejected(["the CSV is empty"]) from None

    columns = [c.strip().lower().lstrip("﻿") for c in header]
    missing = [c for c in CSV_REQUIRED_COLUMNS if c not in columns]
    if missing:
        raise CapacityRejected([f"missing column(s): {', '.join(missing)}"])

    index = {name: columns.index(name) for name in CSV_REQUIRED_COLUMNS + CSV_OPTIONAL_COLUMNS if name in columns}
    drafts: list[ObservationDraft] = []
    errors: list[str] = []
    for line_no, row in enumerate(rows, start=2):
        if not any((cell or "").strip() for cell in row):
            continue  # a trailing blank line is not an error
        raw = {name: (row[pos] if pos < len(row) else None) for name, pos in index.items()}
        draft, row_errors = validate_observation(raw, cfg=cfg, naive_tz=naive_tz, default_source="CSV", now=now)
        if row_errors:
            errors.extend(f"row {line_no}: {message}" for message in row_errors)
        elif draft is not None:
            drafts.append(draft)

    if errors:
        raise CapacityRejected(errors)
    if not drafts:
        raise CapacityRejected(["the CSV has a header but no data rows"])
    return drafts


@dataclass(frozen=True)
class IngestReport:
    """What one submission did. ``duplicates`` is a success, not a failure — see idea 4."""

    accepted: int
    duplicates: int
    sites: tuple[str, ...]
    first_busy_hour: datetime | None
    last_busy_hour: datetime | None
    naive_tz: str
    source: str

    def as_json(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "duplicates": self.duplicates,
            "sites": list(self.sites),
            "first_busy_hour": z_utc(self.first_busy_hour),
            "last_busy_hour": z_utc(self.last_busy_hour),
            # Always stated, never inferred: the reader of an ingest needs to know which
            # assumption was applied to naive timestamps (see NAIVE_TZ_UTC).
            "naive_tz_applied": self.naive_tz,
            "source": self.source,
        }


def operator_id() -> str:
    """The active operator. One process serves one profile (``api/deps.py``)."""
    return _settings().operator.operator_id


def owned_observations():
    """``SELECT`` over this operator's observations. The operator clause is in the WHERE."""
    return _owned(CapacityObservationRow)


def owned_advisories():
    """``SELECT`` over this operator's advisories. The operator clause is in the WHERE."""
    return _owned(CapacityAdvisoryRow)


def _existing_keys(
    session: Session,
    drafts: Sequence[ObservationDraft],
) -> set[tuple[str, str | None, str, datetime]]:
    """The natural keys this operator already holds, among the ones being submitted.

    One bounded query over the submitted span rather than one per row: a 3 000-row export
    would otherwise be 3 000 round trips. The span is the submission's own min/max busy hour,
    so the read never walks the whole table.
    """
    if not drafts:
        return set()
    low = min(d.busy_hour_at for d in drafts)
    high = max(d.busy_hour_at for d in drafts)
    sites = {d.site_id for d in drafts}
    rows = session.execute(
        owned_observations()
        .with_only_columns(
            CapacityObservationRow.site_id,
            CapacityObservationRow.cell_id,
            CapacityObservationRow.metric,
            CapacityObservationRow.busy_hour_at,
        )
        .where(
            CapacityObservationRow.site_id.in_(sites),
            CapacityObservationRow.busy_hour_at >= low,
            CapacityObservationRow.busy_hour_at <= high,
        )
    ).all()
    return {(r[0], r[1], r[2], r[3]) for r in rows}


def ingest_observations(
    session: Session,
    drafts: Sequence[ObservationDraft],
    *,
    actor: str,
    naive_tz: str = NAIVE_TZ_UTC,
    source: str = "CSV",
) -> IngestReport:
    """Write the drafts this operator does not already hold, and say what happened.

    Adds rows; does not commit — the caller owns the transaction, as everywhere else in this
    codebase. Nothing here opens an advisory: ingesting a measurement and forming a judgement
    about measurements are separate acts, and fusing them would mean a file upload could raise
    advice as a side effect of arriving.
    """
    seen = _existing_keys(session, drafts)
    op_id = operator_id()
    accepted = 0
    duplicates = 0
    for draft in drafts:
        if draft.key in seen:
            duplicates += 1
            continue
        seen.add(draft.key)  # a file that repeats a row inside itself is its own duplicate
        session.add(
            CapacityObservationRow(
                operator_id=op_id,
                site_id=draft.site_id,
                cell_id=draft.cell_id,
                metric=draft.metric,
                value=draft.value,
                busy_hour_at=draft.busy_hour_at,
                source=draft.source,
            )
        )
        accepted += 1

    # This project's sessionmaker has autoflush OFF, so without this the rows just added are
    # invisible to a read issued on the same session before the commit -- and reading straight
    # after an ingest ("did that file land?") is the obvious next thing a caller does.
    session.flush()

    sites = tuple(sorted({d.site_id for d in drafts}))
    report = IngestReport(
        accepted=accepted,
        duplicates=duplicates,
        sites=sites,
        first_busy_hour=min((d.busy_hour_at for d in drafts), default=None),
        last_busy_hour=max((d.busy_hour_at for d in drafts), default=None),
        naive_tz=(naive_tz or NAIVE_TZ_UTC).strip().upper(),
        source=source,
    )
    _audit(
        session,
        actor=actor,
        action="capacity.observations.ingested",
        entity_type=OBSERVATION_ENTITY_TYPE,
        entity_id=",".join(sites[:5]) or "-",
        rationale=f"{accepted} accepted, {duplicates} already held",
        payload=report.as_json(),
    )
    return report


# ------------------------------------------------------------------------------- the reading


@dataclass(frozen=True)
class DayEvidence:
    """One EAT calendar day's working, as it appears on a reading and on an advisory.

    The day is the **EAT** day, not the UTC one. "3 busy hours a day on 7 days" is a statement
    about the operator's day; a 22:00 EAT sample is 19:00 UTC the same day, but a 01:00 EAT
    sample is 22:00 UTC the day *before*, and grouping on UTC would split one Kenyan night
    across two entries and defeat the hour count on both.
    """

    day: date
    samples: int
    hours_at_or_above: int
    longest_run: int
    peak_pct: float
    excluded_planned: int
    qualifies: bool

    def as_json(self) -> dict[str, Any]:
        return {
            "day_eat": self.day.isoformat(),
            "samples": self.samples,
            "hours_at_or_above_trigger": self.hours_at_or_above,
            # Reported beside the count on purpose, so a reviewer can see both readings of
            # "consecutive_busy_hours" — see consecutive_busy_hours() for why.
            "longest_adjacent_run": self.longest_run,
            "peak_pct": round(self.peak_pct, 2),
            "excluded_planned": self.excluded_planned,
            "qualifies": self.qualifies,
        }


@dataclass(frozen=True)
class CapacityReading:
    """What can honestly be said about one cell over one period — including "not much".

    ``peak_pct`` and ``mean_busy_hour_pct`` are ``None`` whenever ``verdict`` is
    :data:`VERDICT_INSUFFICIENT_DATA`, and that is the whole design: a caller cannot render a
    number this lane does not stand behind, because there is no number to render. ``reason``
    is the sentence a human reads, and it names the shortfall rather than hiding it.
    """

    site_id: str
    cell_id: str | None
    metric: str
    period_start: datetime
    period_end: datetime
    verdict: str
    observations: int
    distinct_days: int
    qualifying_days: int
    excluded_planned: int
    planned_exclusion: str
    planned_minutes: int | None
    trigger_pct: float
    sustained_days_required: int
    busy_hours_required: int
    min_observations: int
    min_days: int
    peak_pct: float | None
    mean_busy_hour_pct: float | None
    reason: str
    days: tuple[DayEvidence, ...] = field(default_factory=tuple)

    @property
    def advisable(self) -> bool:
        """True only for a sustained trend on sufficient data. The one gate on writing advice."""
        return self.verdict == VERDICT_SUSTAINED

    def as_json(self) -> dict[str, Any]:
        return {
            "site_id": self.site_id,
            "cell_id": self.cell_id,
            "metric": self.metric,
            "period_start": z_utc(self.period_start),
            "period_end": z_utc(self.period_end),
            "verdict": self.verdict,
            "reason": self.reason,
            # Explicit rather than implied by two nulls: a UI that shows a dash needs to know
            # whether the dash means "zero" or "we are not telling you".
            "numbers_withheld": self.verdict == VERDICT_INSUFFICIENT_DATA,
            "peak_pct": None if self.peak_pct is None else round(self.peak_pct, 2),
            "mean_busy_hour_pct": None if self.mean_busy_hour_pct is None else round(self.mean_busy_hour_pct, 2),
            "observations": self.observations,
            "distinct_days": self.distinct_days,
            "qualifying_days": self.qualifying_days,
            "excluded_planned": self.excluded_planned,
            "planned_exclusion": self.planned_exclusion,
            "planned_minutes": self.planned_minutes,
            "policy": {
                "trigger_pct": self.trigger_pct,
                "sustained_days": self.sustained_days_required,
                "consecutive_busy_hours": self.busy_hours_required,
                "min_observations": self.min_observations,
                "min_days": self.min_days,
                "yaml_path": CAPACITY_YAML_PATH,
                # Said on every reading, not buried in a docs page: §7.5.1 calls the trigger a
                # "conventional trigger; operator policy, not a standard".
                "note": "operator policy, not a standard (§7.5.1)",
            },
            "days": [d.as_json() for d in self.days],
        }


def _hour_slot(at: datetime) -> datetime:
    """The EAT clock hour a sample belongs to, to the hour. Aware EAT, for grouping only."""
    eat = to_eat(at)
    return eat.replace(minute=0, second=0, microsecond=0)


def _longest_adjacent_run(slots: Iterable[datetime]) -> int:
    """The longest run of adjacent clock hours in ``slots`` (which need not be sorted)."""
    ordered = sorted(set(slots))
    if not ordered:
        return 0
    best = run = 1
    for previous, current in zip(ordered, ordered[1:]):
        run = run + 1 if current - previous == timedelta(hours=1) else 1
        best = max(best, run)
    return best


def read_capacity(
    session: Session,
    cfg: OperatorConfig,
    *,
    site_id: str,
    cell_id: str | None = None,
    metric: str | None = None,
    now: datetime | None = None,
) -> CapacityReading:
    """What this operator's data supports saying about one cell, right now.

    Pure read: it writes nothing, proposes nothing and decides nothing beyond the verdict.
    :func:`open_advisory` is the only function that turns a reading into a row, and it takes a
    reading rather than recomputing one, so what an advisory claims and what the API shows are
    the same arithmetic run once.
    """
    at = now or utcnow()
    metric = (metric or metric_of(cfg)).strip().upper()
    period_end = at
    period_start = at - timedelta(days=lookback_days(cfg))
    threshold = trigger_pct(cfg)
    need_days = sustained_days(cfg)
    need_hours = consecutive_busy_hours(cfg)
    floor_obs = min_observations(cfg)
    floor_days = min_days(cfg)

    stmt = owned_observations().where(
        CapacityObservationRow.site_id == site_id,
        CapacityObservationRow.metric == metric,
        CapacityObservationRow.busy_hour_at >= period_start,
        CapacityObservationRow.busy_hour_at <= period_end,
    )
    # IS NULL and "= value" are different predicates, and a site-level sample is not a sample
    # of sector 1: §7.5.1 allows cell_id to be NULL, so the two are read separately and never
    # averaged together.
    stmt = stmt.where(
        CapacityObservationRow.cell_id.is_(None) if cell_id is None else CapacityObservationRow.cell_id == cell_id
    )
    rows = session.scalars(stmt.order_by(CapacityObservationRow.busy_hour_at)).all()

    # --- planned maintenance, asked of the lane that owns it (idea 3) ---------------------
    # planned_minutes() is one query and the usual answer is zero, which is why it gates the
    # per-sample is_planned() calls rather than the other way round. Neither figure is
    # recomputed here: this module has no idea what a SCHEDULED window is and must not learn.
    if maintenance.maintenance_enabled():
        planned = maintenance.planned_minutes(
            session, operator_id(), site_id, period_start=period_start, period_end=period_end
        )
        exclusion = PLANNED_APPLIED
    else:
        planned = None
        exclusion = PLANNED_NOT_TRACKED

    kept: list[CapacityObservationRow] = []
    excluded_planned = 0
    excluded_by_day: dict[date, int] = {}
    for row in rows:
        if planned and maintenance.is_planned(session, operator_id(), site_id, row.busy_hour_at) is not None:
            excluded_planned += 1
            day = _hour_slot(row.busy_hour_at).date()
            excluded_by_day[day] = excluded_by_day.get(day, 0) + 1
            continue
        kept.append(row)

    by_day: dict[date, list[CapacityObservationRow]] = {}
    for row in kept:
        by_day.setdefault(_hour_slot(row.busy_hour_at).date(), []).append(row)

    days: list[DayEvidence] = []
    for day in sorted(by_day):
        samples = by_day[day]
        # DISTINCT hour slots, so a re-sent file cannot make three samples of 17:00 look like
        # three busy hours (idea 4).
        hot_slots = {_hour_slot(s.busy_hour_at) for s in samples if s.value >= threshold}
        days.append(
            DayEvidence(
                day=day,
                samples=len(samples),
                hours_at_or_above=len(hot_slots),
                longest_run=_longest_adjacent_run(hot_slots),
                peak_pct=max(s.value for s in samples),
                excluded_planned=excluded_by_day.get(day, 0),
                qualifies=len(hot_slots) >= need_hours,
            )
        )
    for day, count in excluded_by_day.items():
        # A day whose every sample was inside a window still belongs on the evidence: "nothing
        # here, because the site was off air on purpose" is a different statement from silence.
        if day not in by_day:
            days.append(DayEvidence(day, 0, 0, 0, 0.0, count, False))
    days.sort(key=lambda d: d.day)

    observations = len(kept)
    distinct_days = len(by_day)
    qualifying_days = sum(1 for d in days if d.qualifies)

    planned_note = (
        ""
        if exclusion == PLANNED_APPLIED
        else (
            " Planned-maintenance minutes were NOT excluded: "
            f"{maintenance.MAINTENANCE_ENABLED_ENV} is off on this deployment."
        )
    )

    # --- the verdict, data floor first (idea 2) -------------------------------------------
    # Order matters. The floor is checked BEFORE the trigger, so a cell with four red-hot
    # samples is "not enough data" rather than "sustained": four samples cannot be sustained,
    # whatever they say. Note that excluding planned samples can push a cell under the floor,
    # and when it does, INSUFFICIENT_DATA is the honest answer -- not "fine".
    if observations < floor_obs or distinct_days < floor_days:
        return CapacityReading(
            site_id=site_id,
            cell_id=cell_id,
            metric=metric,
            period_start=period_start,
            period_end=period_end,
            verdict=VERDICT_INSUFFICIENT_DATA,
            observations=observations,
            distinct_days=distinct_days,
            qualifying_days=qualifying_days,
            excluded_planned=excluded_planned,
            planned_exclusion=exclusion,
            planned_minutes=planned,
            trigger_pct=threshold,
            sustained_days_required=need_days,
            busy_hours_required=need_hours,
            min_observations=floor_obs,
            min_days=floor_days,
            # Withheld, deliberately. There IS a maximum in the rows; publishing it would make
            # a spike out of a sample and this lane would have learned nothing from §7.4.5.
            peak_pct=None,
            mean_busy_hour_pct=None,
            reason=(
                f"Not enough data to advise: {observations} observation(s) across {distinct_days} day(s) "
                f"in the last {lookback_days(cfg)} days, against a floor of {floor_obs} observation(s) "
                f"across {floor_days} day(s). No utilisation figure is published for this cell." + planned_note
            ),
            days=tuple(days),
        )

    peak = max(r.value for r in kept)
    mean = fmean(r.value for r in kept)
    sustained = qualifying_days >= need_days
    if sustained:
        reason = (
            f"{qualifying_days} day(s) had at least {need_hours} busy hour(s) at or above {threshold:g} % "
            f"{metric}, against a policy of {need_days} day(s). Peak {peak:g} %, mean {mean:.1f} %. "
            "Routed to Planning as advice; this is not an upgrade order." + planned_note
        )
    else:
        reason = (
            f"Only {qualifying_days} of the last {distinct_days} day(s) had at least {need_hours} busy hour(s) "
            f"at or above {threshold:g} % {metric}, against a policy of {need_days} day(s). "
            f"Peak {peak:g} %, mean {mean:.1f} %." + planned_note
        )

    return CapacityReading(
        site_id=site_id,
        cell_id=cell_id,
        metric=metric,
        period_start=period_start,
        period_end=period_end,
        verdict=VERDICT_SUSTAINED if sustained else VERDICT_BELOW_TRIGGER,
        observations=observations,
        distinct_days=distinct_days,
        qualifying_days=qualifying_days,
        excluded_planned=excluded_planned,
        planned_exclusion=exclusion,
        planned_minutes=planned,
        trigger_pct=threshold,
        sustained_days_required=need_days,
        busy_hours_required=need_hours,
        min_observations=floor_obs,
        min_days=floor_days,
        peak_pct=peak,
        mean_busy_hour_pct=mean,
        reason=reason,
        days=tuple(days),
    )


def cells_with_data(
    session: Session,
    cfg: OperatorConfig,
    *,
    site_id: str | None = None,
    metric: str | None = None,
    now: datetime | None = None,
) -> list[tuple[str, str | None]]:
    """``(site_id, cell_id)`` pairs this operator has data for inside the lookback period."""
    at = now or utcnow()
    metric = (metric or metric_of(cfg)).strip().upper()
    stmt = (
        owned_observations()
        .with_only_columns(CapacityObservationRow.site_id, CapacityObservationRow.cell_id)
        .where(
            CapacityObservationRow.metric == metric,
            CapacityObservationRow.busy_hour_at >= at - timedelta(days=lookback_days(cfg)),
            CapacityObservationRow.busy_hour_at <= at,
        )
        .distinct()
    )
    if site_id:
        stmt = stmt.where(CapacityObservationRow.site_id == site_id)
    return sorted(((r[0], r[1]) for r in session.execute(stmt).all()), key=lambda p: (p[0], p[1] or ""))


def site_readings(
    session: Session,
    cfg: OperatorConfig,
    site_id: str,
    *,
    metric: str | None = None,
    now: datetime | None = None,
) -> list[CapacityReading]:
    """One reading per cell at ``site_id``. Never one averaged reading for the site.

    Three sectors with 90 %, 40 % and 40 % average to 57 % and look comfortable; the sector
    that is actually full is the one carrying the complaints. The site-level roll-up this lane
    is prepared to make is a count of cells per verdict, which the router does.
    """
    return [
        read_capacity(session, cfg, site_id=site, cell_id=cell, metric=metric, now=now)
        for site, cell in cells_with_data(session, cfg, site_id=site_id, metric=metric, now=now)
    ]


# ------------------------------------------------------------------------------- advisories
#
# EVERYTHING BELOW WRITES ADVICE AND NOTHING ELSE.
#
# The two functions here add a ``capacity_advisories`` row and an ``AuditRow``. They import no
# scheduler, raise no HITL card, touch no incident and open no clock event, and
# ``test_a_capacity_advisory_never_schedules_maintenance_or_touches_an_incident`` asserts those
# tables are still empty afterwards. If a future change needs an advisory to cause something,
# the change goes through a human and the maintenance lane's two approval gates -- not through
# a helper added here.


def _audit(
    session: Session,
    *,
    actor: str,
    action: str,
    entity_type: str,
    entity_id: str,
    rationale: str,
    payload: dict[str, Any],
) -> None:
    session.add(
        AuditRow(
            operator_id=operator_id(),
            actor=actor,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            rationale=rationale,
            payload_json=json.dumps(payload, default=str),
        )
    )


def open_advisory(
    session: Session,
    reading: CapacityReading,
    *,
    actor: str = CAPACITY_AGENT,
) -> CapacityAdvisoryRow | None:
    """Open one advisory for ``reading``, or ``None`` when the reading does not justify one.

    Two refusals, both structural:

    * a reading that is not :data:`VERDICT_SUSTAINED` writes nothing — in particular
      :data:`VERDICT_INSUFFICIENT_DATA` never opens an advisory, because advice drawn from data
      this lane has already said it does not trust is worse than silence;
    * an OPEN advisory already covering this ``(site, cell, metric)`` is returned unchanged
      rather than duplicated, so an hourly job cannot turn one congested cell into a hundred
      cards. Re-advising is what the reviewer's CLOSE is for.
    """
    if not reading.advisable:
        return None

    existing = session.scalar(
        owned_advisories().where(
            CapacityAdvisoryRow.site_id == reading.site_id,
            CapacityAdvisoryRow.cell_id.is_(None)
            if reading.cell_id is None
            else CapacityAdvisoryRow.cell_id == reading.cell_id,
            CapacityAdvisoryRow.metric == reading.metric,
            CapacityAdvisoryRow.status == ADVISORY_OPEN,
        )
    )
    if existing is not None:
        return existing

    row = CapacityAdvisoryRow(
        operator_id=operator_id(),
        site_id=reading.site_id,
        cell_id=reading.cell_id,
        opened_at=utcnow(),
        # The policy AS IT WAS, copied onto the row; see the model docstring.
        trigger_pct=reading.trigger_pct,
        sustained_days=reading.qualifying_days,
        status=ADVISORY_OPEN,
        routed_to=ROUTED_TO_PLANNING,
        metric=reading.metric,
        evidence_json=json.dumps(reading.as_json(), default=str),
    )
    session.add(row)
    session.flush()  # so the audit row can name the advisory it is about
    _audit(
        session,
        actor=actor,
        action="capacity.advisory.opened",
        entity_type=ADVISORY_ENTITY_TYPE,
        entity_id=row.id,
        rationale=reading.reason,
        payload={"site_id": reading.site_id, "cell_id": reading.cell_id, "verdict": reading.verdict},
    )
    return row


def review_advisory(
    session: Session,
    advisory: CapacityAdvisoryRow,
    *,
    status: str,
    actor: str,
    note: str | None = None,
) -> CapacityAdvisoryRow:
    """Record that a named human read the advice, and what they said (ACKNOWLEDGED | CLOSED).

    ``actor`` must be a person. An advisory is opened by a job and closed by a human, and the
    asymmetry is the point: the machine may say "look at this", only a human may say "seen" or
    "no action". A CLOSED advisory is terminal — re-opening one would rewrite the record of
    what Planning decided, and the correct answer to a cell that is busy again is a new
    advisory with its own evidence.
    """
    target = (status or "").strip().upper()
    if target not in (ADVISORY_ACKNOWLEDGED, ADVISORY_CLOSED):
        raise CapacityRejected([f"status must be one of {ADVISORY_ACKNOWLEDGED}, {ADVISORY_CLOSED}"])
    if advisory.status == ADVISORY_CLOSED:
        raise CapacityRejected(["this advisory is CLOSED; open a new one rather than re-opening it"])
    who = (actor or "").strip()
    if not who or who.startswith(("agent:", "policy:")):
        raise CapacityRejected(["an advisory is reviewed by a named human, not by an agent"])

    previous = advisory.status
    advisory.status = target
    advisory.reviewed_by = who
    advisory.reviewed_at = utcnow()
    advisory.review_note = (note or "").strip() or None
    _audit(
        session,
        actor=who,
        action="capacity.advisory.reviewed",
        entity_type=ADVISORY_ENTITY_TYPE,
        entity_id=advisory.id,
        rationale=advisory.review_note or f"{previous} -> {target}",
        payload={"from": previous, "to": target, "site_id": advisory.site_id, "cell_id": advisory.cell_id},
    )
    return advisory


# ------------------------------------------------------------------------------ serializers


def observation_out(row: CapacityObservationRow) -> dict[str, Any]:
    """One observation as the API returns it. Timestamps carry an explicit ``Z`` (§7.0.6)."""
    return {
        "id": row.id,
        "site_id": row.site_id,
        "cell_id": row.cell_id,
        "metric": row.metric,
        "value": row.value,
        "busy_hour_at": z_utc(row.busy_hour_at),
        "source": row.source,
        "created_at": z_utc(row.created_at),
    }


def advisory_out(row: CapacityAdvisoryRow) -> dict[str, Any]:
    """One advisory as the API returns it, evidence included.

    ``advice_only`` is not decoration. Every consumer of this payload — a page, a later agent,
    somebody reading a JSON dump in a year — is told in the payload itself that this row
    authorises nothing (§7.5.3).
    """
    try:
        evidence = json.loads(row.evidence_json or "{}")
    except (TypeError, ValueError):  # pragma: no cover - a row written outside this module
        evidence = {}
    return {
        "id": row.id,
        "site_id": row.site_id,
        "cell_id": row.cell_id,
        "metric": row.metric,
        "opened_at": z_utc(row.opened_at),
        "trigger_pct": row.trigger_pct,
        "sustained_days": row.sustained_days,
        "status": row.status,
        "routed_to": row.routed_to,
        "reviewed_by": row.reviewed_by,
        "reviewed_at": z_utc(row.reviewed_at),
        "review_note": row.review_note,
        "evidence": evidence,
        "advice_only": True,
        "advice_note": "Advice for Planning. This does not schedule maintenance and is not an upgrade order (§7.5.3).",
    }


# ------------------------------------------------------------------------------- the job

SCAN_JOB_NAME = "capacity_scan"
#: Hourly. A trend measured in days does not change between minutes, and the input arrives by
#: file upload, so anything faster is load with no new information behind it.
SCAN_INTERVAL_S = 3600
AGENT = "CapacityPlanningAgent"
GRAPH_NAME = "capacity"

#: How many cells one scan will look at. A bound, not a tuning knob: 6 000 sites times three
#: sectors is 18 000 readings, and a job that cannot finish inside its ``max_seconds`` budget
#: is recorded FAILED and tells the floor nothing useful. The unscanned remainder is picked up
#: by the next tick, because the ordering is stable.
SCAN_CELL_LIMIT = 500


def capacity_scan(session: Session, settings: "AppSettings", *, now: datetime | None = None) -> JobResult:
    """Read every cell with recent data and open advisories for the sustained ones.

    This job's entire vocabulary of action is "open an advisory". It does not propose a
    maintenance task, does not book a window and does not touch an incident; it cannot, because
    nothing it imports can do those things. See the banner above :func:`open_advisory`.
    """
    if not capacity_enabled():
        return JobResult(summary=f"capacity scan off ({CAPACITY_ENABLED_ENV}=false)")

    cfg = settings.operator
    at = now or utcnow()
    pairs = cells_with_data(session, cfg, now=at)[:SCAN_CELL_LIMIT]
    opened: list[str] = []
    counts = {VERDICT_SUSTAINED: 0, VERDICT_BELOW_TRIGGER: 0, VERDICT_INSUFFICIENT_DATA: 0}
    for site_id, cell_id in pairs:
        reading = read_capacity(session, cfg, site_id=site_id, cell_id=cell_id, now=at)
        counts[reading.verdict] = counts.get(reading.verdict, 0) + 1
        if reading.advisable:
            row = open_advisory(session, reading)
            if row is not None and row.id not in opened:
                opened.append(row.id)
    session.commit()

    return JobResult(
        summary=(
            f"scanned {len(pairs)} cell(s): {counts[VERDICT_SUSTAINED]} sustained, "
            f"{counts[VERDICT_BELOW_TRIGGER]} below trigger, "
            f"{counts[VERDICT_INSUFFICIENT_DATA]} with too little data; {len(opened)} advisory row(s) open"
        ),
        # The "too little data" count is in the rationale as well as the summary on purpose:
        # it is the number that tells an operator their FEED is broken, not their network.
        rationale=(
            f"{counts[VERDICT_INSUFFICIENT_DATA]} cell(s) had fewer than "
            f"{min_observations(cfg)} observation(s) across {min_days(cfg)} day(s) and were not judged"
        ),
        tools=({"tool": "capacity.scan", "cells": len(pairs), "advisories_open": len(opened)},),
    )


def capacity_scan_job(session: Session, settings: "AppSettings") -> JobResult:
    """JobCard entry point for :func:`capacity_scan` (the card's ``fn`` takes no kwargs)."""
    return capacity_scan(session, settings)


#: The card. ``default_enabled=False`` so an unset ``CAPACITY_ENABLED`` reads as OFF in
#: ``/scheduler/status`` too — an operator who sees "enabled" beside a job producing nothing is
#: worse off than one who sees a job plainly saying it is off.
#:
#: NOT registered in ``scheduler/loop.py`` by this lane: the exact line to add is in the
#: hand-off note, so the file that lists every scheduled job in the system is edited once, by
#: the person integrating, rather than by each lane in turn.
CAPACITY_SCAN_JOB = JobCard(
    SCAN_JOB_NAME,
    SCAN_INTERVAL_S,
    capacity_scan_job,
    CAPACITY_ENABLED_ENV,
    AGENT,
    GRAPH_NAME,
    default_enabled=False,
)
