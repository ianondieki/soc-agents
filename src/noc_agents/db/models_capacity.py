"""Capacity observations and the advisories derived from them (spec §7.5.1) — Phase 5 Lane 5A.

Two tables, both created by the generic additive migration (``db/migrate.py`` walks
``Base.metadata``; ``db/models_all.py`` already imports this module before it runs), so there
is no hand-written DDL here.

Why two tables and not one
--------------------------
``capacity_observations``
    *What a counter said.* One busy-hour sample of one metric at one cell. It is a
    measurement: it is never edited, it carries no opinion, and it is the only thing in this
    lane a machine may write without a human in the loop. High volume, short sentences.

``capacity_advisories``
    *What somebody was told about those measurements.* An advisory is **advice**: it says
    "this cell has been busy for a week, Planning should look". It cannot schedule
    maintenance, cannot touch an incident and cannot open a stop clock. It is a separate row
    from the observations that justified it because advice outlives the samples (§9.4 retains
    raw counters for far less time than the decision they prompted) and because a human
    reviewing it writes on the advice, never on the measurement.

The pair is deliberately NOT one table with a ``kind`` column. A measurement and a judgement
about measurements have different authors, different retention and different blast radius,
and the one thing this lane must never do is let a judgement be mistaken for a reading.

Operator scoping (§8)
---------------------
Both tables carry their own ``operator_id``, exactly as the spec's DDL does, so
``api.deps._owned`` filters them directly with no join and another operator's rows are out by
the WHERE clause rather than by a check after the fetch. Two operators legitimately run cells
at the same ``site_id`` string in this schema, which is why every read in
``services/capacity.py`` goes through ``_owned`` and why ``tests/unit/test_capacity.py`` seeds
both operators at one site before it asserts a single total.

Status vocabularies are plain strings with no CHECK constraint, matching
``maintenance_windows.status`` and ``incidents.status``: the vocabulary and every transition
between its members are enforced by ``services/capacity.py``, which is the only module allowed
to write these tables.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from noc_agents.db.models import Base, new_id, utcnow

__all__ = [
    "ADVISORY_ACKNOWLEDGED",
    "ADVISORY_CLOSED",
    "ADVISORY_OPEN",
    "ADVISORY_STATUSES",
    "CAPACITY_METRICS",
    "CAPACITY_SOURCES",
    "DEFAULT_METRIC",
    "ROUTED_TO_PLANNING",
    "CapacityAdvisoryRow",
    "CapacityObservationRow",
]

#: ``capacity_observations.metric``. §7.5.1 ships exactly one: 3GPP TS 28.552's "DL Total PRB
#: Usage", a percentage in [0, 100]. A closed set, for the same reason the maintenance task
#: types are closed — a typo must fail at the service boundary rather than quietly become a
#: second metric with its own silent trigger that nobody has a threshold for. The percentage
#: range is validated in ``services.capacity.validate_observation``: a value of 700 is a units
#: error (per-mille, or raw PRB counts) and a units error that is stored becomes a false
#: advisory that somebody spends money on.
DEFAULT_METRIC = "DL_TOTAL_PRB_USAGE"
CAPACITY_METRICS: tuple[str, ...] = (DEFAULT_METRIC,)

#: ``capacity_observations.source`` (§7.5.1). Provenance travels with every sample because the
#: three do not deserve equal trust: ``PM_FEED`` came from the OSS, ``CSV`` from a file someone
#: exported and possibly edited, ``MANUAL`` from a person typing. §7.5.5 is explicit that a
#: real PRB feed is absent here, so ``CSV``/``MANUAL`` is the shipped reality — and an advisory
#: that cannot say where its numbers came from is not evidence of anything.
CAPACITY_SOURCES: tuple[str, ...] = ("CSV", "MANUAL", "PM_FEED")

#: ``capacity_advisories.status``. There is deliberately no ``ACTIONED`` or ``APPROVED``
#: member: acting on an advisory means ordering hardware or booking a window, and both happen
#: elsewhere (for a window, in ``services/maintenance.py`` behind two named human approvals).
#: The most this lane records is that a human read the advice and what they said about it —
#: ``reviewed_by``/``review_note`` — which is why ACKNOWLEDGED and CLOSED are the only two
#: destinations an OPEN advisory has.
ADVISORY_OPEN = "OPEN"
ADVISORY_ACKNOWLEDGED = "ACKNOWLEDGED"
ADVISORY_CLOSED = "CLOSED"
ADVISORY_STATUSES: tuple[str, ...] = (ADVISORY_OPEN, ADVISORY_ACKNOWLEDGED, ADVISORY_CLOSED)

#: ``capacity_advisories.routed_to`` — §7.5.1's default and, for now, its only value. §7.5.3:
#: "advisory routed to Planning; never an upgrade order."
ROUTED_TO_PLANNING = "PLANNING"


class CapacityObservationRow(Base):
    """One busy-hour sample of one metric, at one cell, at one instant (§7.5.1).

    ``busy_hour_at`` is naive **UTC**, like every other instant in this schema (§7.0.6). A PRB
    export from an OSS is almost always written in local time, so the conversion happens once,
    on the way in (``services.capacity.parse_busy_hour``), and never in a query. The bug this
    avoids is a Nairobi 17:00 busy hour stored as 17:00 UTC: it then lands at 20:00 EAT, three
    hours after the peak it is supposed to describe, and — the part that actually costs
    money — it stops lining up with the maintenance window that should have excluded it.

    ``cell_id`` is nullable because §7.5.1 allows a site-level sample, but a real PRB figure is
    per cell: a site with three sectors has three numbers and their average is nobody's
    congestion. ``services.capacity`` therefore groups by ``(site_id, cell_id)`` and never
    averages across cells.

    There is no UNIQUE constraint on ``(operator_id, site_id, cell_id, metric, busy_hour_at)``
    and that is a decision, not an omission. The spec's DDL has none; a PM feed legitimately
    restates a value it has corrected; and a UNIQUE here would turn a re-sent file into a 500
    rather than a no-op. Idempotence is enforced one level up instead, where it can be
    *reported*: ``services.capacity.ingest_observations`` skips a sample it already holds and
    counts it as a duplicate, and every aggregate counts DISTINCT ``busy_hour_at`` values, so
    a duplicate that somehow lands still cannot inflate a day's hour count into an advisory.
    """

    __tablename__ = "capacity_observations"
    __table_args__ = (
        # The lane's one hot query: "every sample for this cell in this period", which is what
        # the reading, the duplicate check and the API list all issue.
        Index("ix_capacity_obs_lookup", "operator_id", "site_id", "cell_id", "metric", "busy_hour_at"),
        Index("ix_capacity_obs_operator_busy_hour", "operator_id", "busy_hour_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    operator_id: Mapped[str] = mapped_column(String(32), index=True)
    site_id: Mapped[str] = mapped_column(String(64), index=True)
    cell_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metric: Mapped[str] = mapped_column(String(48))  # CAPACITY_METRICS
    value: Mapped[float] = mapped_column(Float)  # percent for DL_TOTAL_PRB_USAGE, 0-100
    busy_hour_at: Mapped[datetime] = mapped_column(DateTime)  # naive UTC, the storage contract
    source: Mapped[str] = mapped_column(String(16))  # CAPACITY_SOURCES
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CapacityAdvisoryRow(Base):
    """One piece of advice for Planning: this cell has been sustained above the trigger (§7.5.1).

    **This row is not an instruction and nothing downstream may treat it as one.** §7.5.3:
    "advisory routed to Planning; never an upgrade order". It has no ``window_id``, no
    ``incident_id``, no ``hitl_task_id`` and no ``task_id``, and those absences are the
    enforcement: there is nothing here for a later refactor to hang an action off. It is the
    same discipline ``services.maintenance.stop_clock_proposal`` keeps by having no ``accept``
    helper beside it — a convenience wrapper is one refactor from being called by a job.

    ``trigger_pct`` and ``sustained_days`` are the *policy in force when the advisory opened*,
    copied onto the row rather than looked up when it is read. §7.5.1 calls the 70 % trigger
    "conventional trigger; operator policy, not a standard", so it is a number somebody chose
    and will one day change — and an advisory re-read after that change must still say what it
    was actually judged against, or the evidence quietly rewrites itself.

    ``evidence_json`` is the day-by-day working: how many busy hours each day cleared the
    trigger, how many samples were dropped because the site was inside an approved maintenance
    window, and whether that exclusion could be applied at all. It exists because a reader who
    cannot see the working has to take the number on trust, and the failure mode this lane is
    built against is exactly that — a confident figure with nothing behind it.
    """

    __tablename__ = "capacity_advisories"
    __table_args__ = (
        Index("ix_capacity_adv_operator_status", "operator_id", "status"),
        Index("ix_capacity_adv_scope", "operator_id", "site_id", "cell_id", "metric", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    operator_id: Mapped[str] = mapped_column(String(32), index=True)
    site_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    cell_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    trigger_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    sustained_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=ADVISORY_OPEN, server_default=ADVISORY_OPEN)
    routed_to: Mapped[str] = mapped_column(String(32), default=ROUTED_TO_PLANNING, server_default=ROUTED_TO_PLANNING)

    # --- beyond §7.5.1's DDL, and why each one earns its column -------------------------
    # The spec's DDL for this table is four columns of scope and two of policy; these five are
    # additive, and each answers a question a reviewer of an advisory will certainly ask.
    #
    # ``metric``: §7.5.1 ships one metric today and names a data path (PM_FEED) that will bring
    # more. An advisory that cannot say WHICH counter it is about is unreadable the moment
    # there are two, and back-filling it later means guessing.
    metric: Mapped[str] = mapped_column(String(48), default=DEFAULT_METRIC, server_default=DEFAULT_METRIC)
    # ``evidence_json``: the working (see the class docstring). JSON text, like every other
    # "show me why" column in this schema (``vendor_scorecards.data_quality_json``,
    # ``regulatory_notifications.significance_json``).
    evidence_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    # ``reviewed_by`` / ``reviewed_at`` / ``review_note``: who in Planning read it and what they
    # said. A named human, because acknowledging advice is a human act; the note is free text
    # because "no action, a new site goes up here in Q1" is the useful answer and no closed
    # vocabulary contains it.
    reviewed_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
