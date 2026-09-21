"""The memory write path: build ``memory_episodes``, feed the FTS index, expire both.

Spec §7.11.4 (write APIs), §7.11.5 (jobs), §7.11.7 (the duration refusals) and §7.11.8
(retention). This is the only module in the project that writes a ``memory_*`` row.

THE ONE RULE THAT SHAPES EVERYTHING HERE (MEM5)
-----------------------------------------------
**Nothing here runs inside ``run_incident_lifecycle``.** ``runner._fail_closed`` rolls back
the run, its steps, its audit rows, the incident, the sequence bump and the notes; a memory
row written inside an agent would die with it, and — worse — a *partially* consolidated
episode could survive a later partial commit and be indistinguishable from a real one. So
consolidation runs **post-commit only**, from three callers that each own a short transaction
of their own:

* the ``memory_consolidate`` scheduled job (a fresh session per tick, §4.4);
* ``scripts/backfill_memory.py``, a one-off resumable pass;
* ``HousekeepingAgent``, for :func:`expire_memory` alone (§9.4).

``tests/unit/test_memory_consolidation.py`` pins it from the other end: a fail-closed run
leaves **zero** memory rows, and a later ``consolidate_incident`` on the surviving incident
still succeeds.

IDEMPOTENCE
-----------
``memory_episodes.incident_id`` is UNIQUE, and :func:`consolidate_incident` updates in place
when the row exists rather than letting the constraint raise — so a retried tick, a
re-delivered job and a second backfill all converge on **one** row with identical values
(§7.11.11 test 11). The one value that could have drifted between runs is the 3×IQR outlier
verdict, because it depends on the fault class's population: the incident is excluded from
its own comparison set precisely so the second run judges it against the same sample as the
first.

WHAT IS REFUSED, AND WHY IT IS REFUSED HERE RATHER THAN LATER
-------------------------------------------------------------
``restore_minutes`` is NULL unless ``incidents.restored_source ∈ {MARK_RESTORED, SUPERVISOR}``
(§7.0.8's M4 rule), NULL when the clock runs backwards, and NULL when the value is an outlier
beyond 3×IQR for its fault class. ``VENDOR_NOTE_INFERRED`` is a regex hit on the word
"RESTORED" inside a vendor's free text (brief defect #4) and a NULL source is
``close_incident``'s ``restored_at = closed_at`` back-fill — both produce a number that looks
exactly like an MTTR and is not one. The episode row still exists, so the outage still counts
toward "how often has this happened here"; it simply never feeds a median. Refusing at the
source is the whole point: a wrong duration admitted here is a wrong median in M2, a wrong
prior in M3 and an engineer told to expect a four-hour restore on a fault that takes twenty
minutes.

PRIVACY (§7.11.8 rule 1)
------------------------
Every free-text field is scrubbed **before** the row is written, with the project's only
scrubber (``llm/redaction.py``) and with **one** ``NameMap`` per incident, seeded with every
name the incident carries *and has carried*: the four person columns (``assignee_name``,
``fe_name``, ``rnio_name``, ``restored_by``), everyone its ASSIGN step recorded, both sides of
every reassign note, and every note author (``services/memory._name_map``; review M01/M11).
``assignee_token`` holds a §6.1 role token, never a name. Once housekeeping has pseudonymised
an incident, its text is **never re-derived** — the NameMap could no longer see the names
still sitting in the notes (review M02; :func:`_write_episode`).

The honest limits, each pinned as a strict ``xfail`` in ``tests/unit/test_memory_privacy.py``
rather than pretended away: there is no NER, so a person named only in prose and in none of
those records is not recognised; and ``NameMap`` registers name *parts* of four letters or
more, so a three-letter first name on its own ("Ann", "Ian") survives (review M08 — a
deliberate false-positive trade-off: several are ordinary English words). §7.11.7's
compensating controls are the pre-send ``validate_no_contacts`` check and the §9.6 daily
redaction scan.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Sequence

from sqlalchemy import or_, select
from sqlalchemy import text as sql
from sqlalchemy.orm import Session

from noc_agents.api.deps import _owned
from noc_agents.config import AppSettings
from noc_agents.db.models import AuditRow, IncidentRow, ScheduledJobStateRow, WorkNoteRow, new_id, utcnow
from noc_agents.db.models_memory import MemoryEpisodeRow
from noc_agents.llm.redaction import scrub_contacts, scrub_text
from noc_agents.memory.schema import (
    FTS_TABLE,
    REF_NOTE,
    REF_RESOLUTION,
    ensure_memory_schema,
    fts_available,
    optimize_index,
)
from noc_agents.scheduler import JobCard, JobResult
from noc_agents.services.alerts import assignee_role_token
from noc_agents.services.clock import to_eat
from noc_agents.services.memory import (
    EPISODE_SUMMARY_MAX_CHARS,
    MEMORY_ENABLED_ENV,
    episode_closed_at,
    episode_closed_at_expr,
    episode_statuses,
    fault_class,
    is_pseudonymised,
    memory_enabled,
    memory_settings,
    name_map_for,
    outlier_bounds,
    person_name_history,
    restore_minutes_population,
    scrubbed_resolution,
    trusted_restore_minutes,
)
from noc_agents.services.shifts import current_shift

log = logging.getLogger("noc_agents.memory.consolidate")

__all__ = [
    "AGENT",
    "AUDIT_ACTION",
    "ENABLED_ENV",
    "EXPIRE_AUDIT_ACTION",
    "GRAPH_NAME",
    "INTERVAL_S",
    "JOB_NAME",
    "MEMORY_CONSOLIDATE_JOB",
    "NOTE_BODY_MAX_CHARS",
    "SKIPPED",
    "SOURCE_VERSION",
    "TEXT_FROZEN",
    "WRITTEN",
    "backfill_plan",
    "consolidate_all",
    "consolidate_incident",
    "consolidate_outcome",
    "consolidate_recent",
    "episode_counts",
    "expire_memory",
    "pending_incidents",
]

# --------------------------------------------------------------------------- job identity

JOB_NAME = "memory_consolidate"
INTERVAL_S = 300  # §7.11.5: every 300 s. `memory_rebuild` (daily, full recompute) is M2.
#: §5.1 roster: memory's writes are attributed to RecurrenceProblemAgent and reported under
#: ``graph_name="memory"``. No ``AgentProfile`` is added, so the exact-equality catalog tests
#: of §2.1 are untouched — that is why an existing agent name is reused rather than invented.
AGENT = "RecurrenceProblemAgent"
GRAPH_NAME = "memory"
#: The lane's own flag, shared with recall. §7.11.3 gates "reads and consolidation" on it.
#: :func:`expire_memory` deliberately does **not** consult it (see its docstring).
ENABLED_ENV = MEMORY_ENABLED_ENV

AUDIT_ACTION = "memory.consolidate"  # §7.11.8 auditability
EXPIRE_AUDIT_ACTION = "memory.expire"
ACTOR = f"agent:{AGENT}"

#: Bump to invalidate every derived row: the backfill re-derives anything built under an
#: older version (``scripts/backfill_memory.py --rebuild`` forces it outright). The one lever
#: for "the derivation changed" that costs no migration.
SOURCE_VERSION = 1

#: §4.5 contention budget: short transactions, ≤ 50 rows per commit. A tick that finds more
#: work leaves the rest for the next tick five minutes later; consolidation is never urgent.
BATCH_LIMIT = 50

#: How far behind the last finish a tick looks. Generous enough to cover failed ticks, an open
#: circuit or a weekend restart; bounded so a tick on a large file never tries to backfill
#: years (that is ``scripts/backfill_memory.py``'s job). The overlap costs one indexed query,
#: not a re-consolidation: rows already current are filtered out in SQL.
COLD_START_LOOKBACK = timedelta(days=2)

#: Cap on one indexed note body. The FTS index is a lookup aid, not a second copy of the
#: ticket (§7.11.7, "unbounded growth"): a vendor who pastes a 40 kB mail thread into a note
#: must not grow the index by 40 kB. Longer than the 500-char episode summary because this
#: text is searched rather than displayed, and a phrase at character 600 is exactly what
#: somebody is searching for.
NOTE_BODY_MAX_CHARS = 1000

#: Provenance values whose ``restored_at`` may decide an SLA verdict — the same pair §7.0.8's
#: M4 rule trusts for a duration. Spelled out here rather than re-exported through
#: ``services.memory`` so the rule is readable at the line that applies it.
_TRUSTED_FOR_SLA = ("MARK_RESTORED", "SUPERVISOR")

#: Notes indexed per incident, newest first. A ticket with 200 vendor notes is a chase
#: problem, not a recall problem.
NOTE_INDEX_LIMIT = 40


# --------------------------------------------------------------------------- derivation


def _notes_newest_first(session: Session, incident_id: str) -> list[WorkNoteRow]:
    """This incident's notes, newest first — the order ``NameMap`` seeding assumes.

    ``work_notes`` carries no ``operator_id``; it is owned through
    ``incident_id -> incidents.operator_id``. Safe here **only** because ``incident_id`` came
    from an ``_owned(IncidentRow)`` statement one call up.
    """
    return list(
        session.scalars(
            select(WorkNoteRow)
            .where(WorkNoteRow.incident_id == incident_id)
            .order_by(WorkNoteRow.created_at.desc())
        ).all()
    )


def _outage_start(inc: IncidentRow) -> datetime:
    """When the fault began, by the best column available. ``created_at`` is NOT NULL."""
    return inc.outage_start_at or inc.failure_time or inc.created_at


def _minutes(start: datetime | None, end: datetime | None) -> int | None:
    """Whole minutes between two timestamps, or ``None`` when the pair cannot carry a duration.

    A negative interval returns ``None`` rather than a negative number: it is a data error,
    and rendering it as a negative MTTA hides that.
    """
    if start is None or end is None or end < start:
        return None
    return int((end - start).total_seconds() // 60)


def _restore_minutes(session: Session, inc: IncidentRow, fault_class_key: str) -> int | None:
    """The §7.0.8 M4 rule, then the §7.11.7 3×IQR outlier rule. NULL on any doubt.

    The provenance refusals live in ``services.memory.trusted_restore_minutes`` — one
    implementation, shared with recall, so a duration recall refuses to show can never reach
    a median. The outlier rule is applied **here** and not there because it needs a
    *population*, which only the derived index has.

    The incident is excluded from its own comparison set: that is what makes the verdict
    idempotent (see the module docstring), and it is also the correct statistics — a value
    cannot be judged against a sample it is a member of.
    """
    minutes = trusted_restore_minutes(inc)
    if minutes is None:
        return None
    sample = restore_minutes_population(
        session, fault_class_key=fault_class_key, exclude_incident_id=inc.id
    )
    bounds = outlier_bounds(sample)
    if bounds is None:  # fewer than 4 comparable episodes: no quartiles, so no verdict
        return minutes
    low, high = bounds
    if minutes < low or minutes > high:
        log.info(
            "memory: restore_minutes=%s refused as a 3xIQR outlier for %s (n=%d, bounds=%.1f..%.1f)",
            minutes, fault_class_key, len(sample), low, high,
        )
        return None
    return minutes


def _sla_met(actual: datetime | None, due: datetime | None) -> bool | None:
    """``actual <= due``, or ``None`` when either side is missing. Never a default of False.

    "We do not know whether the ack SLA was met" and "the ack SLA was missed" are different
    facts, and a False that means the first would become a breach in every later count.
    """
    if actual is None or due is None:
        return None
    return actual <= due


def _eat_pattern(inc: IncidentRow, settings: AppSettings) -> tuple[int, int, str]:
    """``(hour_of_day, month_of_year, shift_type)`` in **EAT** (MEM8).

    Derived from when the fault *started*, not from when the ticket closed: "does this site
    fail in the MAM rains, or on the 19:00 load peak?" is a question about the outage, and a
    ticket closed three days later would answer it with the wrong hour and possibly the wrong
    month.

    ``services.shifts.current_shift`` treats a **naive** datetime as already being in the
    operator's timezone — which ours are not, they are naive UTC (``db/models.py:20``). It is
    handed the aware EAT value on purpose; passing the raw column would silently shift every
    night incident by three hours and put a 22:00 UTC outage in the wrong shift and the wrong
    day. That trap is the reason MEM8 exists as a rule.
    """
    started = _outage_start(inc)
    eat = to_eat(started)
    shift = current_shift(settings.operator, eat).upper()
    return (eat.hour, eat.month, shift)


# --------------------------------------------------------------------------- FTS rows


def _fts_delete(session: Session, incident_id: str) -> None:
    session.execute(sql(f"DELETE FROM {FTS_TABLE} WHERE incident_id = :iid"), {"iid": incident_id})


def _fts_insert(session: Session, row: MemoryEpisodeRow, *, body: str, ref_kind: str) -> None:
    session.execute(
        sql(
            f"INSERT INTO {FTS_TABLE} (body, site_id, failure_domain, fault_class, incident_id, ref_kind) "
            "VALUES (:body, :site_id, :failure_domain, :fault_class, :incident_id, :ref_kind)"
        ),
        {
            "body": body,
            "site_id": row.site_id,
            "failure_domain": row.failure_domain,
            "fault_class": row.fault_class,
            "incident_id": row.incident_id,
            "ref_kind": ref_kind,
        },
    )


def _index_text(
    session: Session,
    row: MemoryEpisodeRow,
    *,
    notes: Sequence[WorkNoteRow],
    names: Any,
) -> int:
    """Rebuild this incident's FTS rows from **scrubbed** text. Returns rows written.

    Delete-then-insert, never merge: the index must equal the derivation. A note edited or a
    resolution rewritten would otherwise leave its old row behind, searchable forever — and
    if the edit was somebody removing a name, the stale row would be the leak.
    """
    _fts_delete(session, row.incident_id)
    written = 0
    if row.resolution_summary:
        _fts_insert(session, row, body=row.resolution_summary, ref_kind=REF_RESOLUTION)
        written += 1
    for note in notes[:NOTE_INDEX_LIMIT]:
        body = (scrub_text(note.body, names) or "").strip()[:NOTE_BODY_MAX_CHARS]
        if body:
            _fts_insert(session, row, body=body, ref_kind=REF_NOTE)
            written += 1
    return written


# --------------------------------------------------------------------------- the write API


#: Outcomes of one consolidation, counted separately in the job summary.
WRITTEN = "written"  # episode built or refreshed from source, text included
TEXT_FROZEN = "text_frozen"  # pseudonymised incident: numbers refreshed, text left as it was
SKIPPED = "skipped"  # not an episode, not this operator's, unknown, or beyond the horizon


def _horizon(settings: AppSettings, now: datetime | None = None) -> datetime:
    """The oldest episode end memory may hold — the same line ``expire_memory`` prunes at
    (review M12), so nothing this module builds is something the prune would remove."""
    days = int(memory_settings(settings.operator)["episode_max_age_days"])
    return (now or utcnow()) - timedelta(days=days)


def consolidate_incident(session: Session, *, settings: AppSettings, incident_id: str) -> int:
    """Build or refresh **one** ``memory_episodes`` row plus its FTS rows (§7.11.4).

    Returns the number of episode rows written (1, or 0 when the incident is not an episode,
    is not this operator's, does not exist, or ended beyond the 24-month horizon). Idempotent
    on ``incident_id``. :func:`consolidate_outcome` is the same call with the reason kept.

    Does **not** commit. The caller owns the transaction — the scheduler's runner commits a
    tick, the backfill commits a batch of 50 (§4.5). It does flush, so the row is visible to
    the FTS insert that follows and to a caller that wants to read it back.

    Operator scoping is ``api.deps._owned``, the single door (G14/MEM10): an id belonging to
    the other operator simply does not come back and 0 is returned. This function never takes
    an operator id as an argument, so there is no second way to write into the wrong tenant.
    """
    return 0 if consolidate_outcome(session, settings=settings, incident_id=incident_id) == SKIPPED else 1


def consolidate_outcome(session: Session, *, settings: AppSettings, incident_id: str) -> str:
    """:func:`consolidate_incident`, returning :data:`WRITTEN`, :data:`TEXT_FROZEN` or
    :data:`SKIPPED` so the job and the backfill can say what they did.

    **All of an incident's writes happen inside one SAVEPOINT** (review M06). The episode row
    is added, filled, flushed and its FTS rows deleted and re-inserted within
    ``session.begin_nested()``; if anything raises, the savepoint rolls back that incident's
    row *and* its FTS rows together and the exception propagates to the caller's per-incident
    guard. Without it, a failure after ``session.add`` left a half-filled row (default
    ``closed_at``, empty text, ``built_at=now``) that the tick then committed as *current* —
    so the queue never offered it again — and a failure after the FTS delete committed an
    episode with no index rows. Whatever the caller does with the exception, the transaction
    it continues holds either this incident's complete derivation or none of it.

    One side effect worth knowing: ``realtime/commit_hook`` discards the session's buffered
    realtime events on any savepoint rollback (deliberately over-discarding), so in a tick
    where one incident fails, that job run's "step started" events are not published. The
    run and step ROWS are unaffected, and the completed step is still announced.
    """
    inc = session.scalar(_owned(IncidentRow).where(IncidentRow.id == incident_id))
    if inc is None:
        return SKIPPED
    if inc.status not in episode_statuses():
        # Not an episode (still open, or CANCELLED). Any stale row for it is left alone
        # rather than deleted: a reopened incident is a real case and deciding what its
        # history means is M3's business, not a side effect of a five-minute tick.
        return SKIPPED
    if episode_closed_at(inc) < _horizon(settings):
        # Review M12: older than expire_memory's horizon. Building it would only hand the
        # nightly prune something to delete again — and with housekeeping in its default
        # dry-run posture, nothing would.
        return SKIPPED
    frozen = is_pseudonymised(inc)
    _ensure_outer_transaction(session)
    with session.begin_nested():
        _write_episode(session, settings=settings, inc=inc, frozen=frozen)
    return TEXT_FROZEN if frozen else WRITTEN


def _ensure_outer_transaction(session: Session) -> None:
    """Make sure SQLite's own transaction is open before a SAVEPOINT is taken.

    The ``sqlite3`` driver in its default (legacy) mode issues ``BEGIN`` lazily, just before
    the first INSERT/UPDATE/DELETE — so a session that has only *read* so far is, at the SQLite
    level, not in a transaction at all. A ``SAVEPOINT`` issued then does not nest: SQLite
    starts a transaction with it, and ``RELEASE`` **commits** that transaction. Measured, not
    assumed: without this, a caller that consolidated and then called ``rollback()`` found the
    episode already committed — the opposite of "the caller owns the transaction". Opening the
    transaction explicitly when the driver reports none is enough to make the savepoint a true
    nested one; the caller's ``commit()``/``rollback()`` then ends it exactly as before.
    Every other backend already nests correctly and is left alone.
    """
    connection = session.connection()
    if connection.dialect.name != "sqlite":
        return
    raw = getattr(connection.connection, "driver_connection", None)
    if raw is not None and not getattr(raw, "in_transaction", True):
        connection.exec_driver_sql("BEGIN")


def _write_episode(session: Session, *, settings: AppSettings, inc: IncidentRow, frozen: bool) -> None:
    """Derive and write one episode. Called only inside :func:`consolidate_outcome`'s savepoint.

    ``frozen`` is review M02. Housekeeping pseudonymises an old incident's person columns and
    leaves its notes and ``resolution_summary`` untouched, so after that a NameMap built from
    the row no longer knows the names still sitting in the text, and re-deriving the text
    would write them back into memory. So for a pseudonymised incident **no source text is
    read at all**: the network facts (durations, SLA verdicts, EAT pattern, fault class) are
    re-derived — they are network data and pseudonymisation does not change them — while
    ``resolution_summary``, ``restoring_note_id`` and the FTS rows stay exactly as they were
    scrubbed when the names were still known. An incident pseudonymised before it was ever
    consolidated gets an episode with no text at all: its numbers still count toward the
    fault-class population, and the job's queue drains instead of re-offering it every tick.
    """
    notes: list[WorkNoteRow] = []
    names = None
    resolution = None
    if not frozen:
        notes = _notes_newest_first(session, inc.id)
        # ONE map for every field of this incident (§7.11.8), seeded with everyone it has
        # ever carried — the ASSIGN record and the reassign notes, not just today's columns
        # (review M01).
        names = name_map_for(
            inc, notes, history=person_name_history(session, [inc.id]).get(inc.id, ())
        )
        resolution = scrubbed_resolution(inc, notes, limit=EPISODE_SUMMARY_MAX_CHARS, names=names)
    key = fault_class(inc.failure_domain, inc.alarm_code, inc.site_type)
    hour, month, shift = _eat_pattern(inc, settings)

    row = session.scalar(
        _owned(MemoryEpisodeRow).where(MemoryEpisodeRow.incident_id == inc.id)
    )
    created = row is None
    if row is None:
        row = MemoryEpisodeRow(
            id=new_id(),
            operator_id=inc.operator_id,
            incident_id=inc.id,
            resolution_summary="",
            restoring_note_id=None,
        )
        session.add(row)

    row.incident_number = inc.incident_number
    row.site_id = inc.site_id
    row.site_type = inc.site_type or "BTS"
    row.region_code = inc.region_code or ""
    row.failure_domain = inc.failure_domain or "UNKNOWN"
    row.alarm_code = inc.alarm_code or ""
    row.fault_class = key
    row.priority = inc.priority or "P4"
    row.users_affected = int(inc.users_affected or 0)
    row.mpesa_risk = bool(inc.mpesa_risk)
    # A company, scrubbed for contact details only — exactly how ``redaction.COMPANY_FIELDS``
    # treats it. Tokenising it as a name would hide network data (the assignee often *is* the
    # MSP company) and would put a token where M4a expects ``vendors.code``.
    row.responsible_msp = scrub_contacts(inc.responsible_msp or inc.msp_name)
    row.assignee_token = assignee_role_token(inc, settings.operator)  # a ROLE, never a name

    row.outage_start_at = inc.outage_start_at
    row.escalated_at = inc.escalated_at
    row.acknowledged_at = inc.acknowledged_at
    row.first_vendor_note_at = inc.first_vendor_note_at
    row.restored_at = inc.restored_at
    row.restored_source = inc.restored_source

    # The NOC's own clock. §7.6.2's contractual MTTA is the vendor pair below; these are two
    # different measurements and the column names say which is which.
    row.ack_minutes = _minutes(_outage_start(inc), inc.acknowledged_at)
    row.vendor_response_minutes = _minutes(
        inc.escalated_at or inc.created_at, inc.first_vendor_note_at
    )
    row.restore_minutes = _restore_minutes(session, inc, key)
    row.sla_ack_met = _sla_met(inc.acknowledged_at, inc.sla_ack_due)
    # Gated on the same provenance as the duration: a "restore" the system inferred from a
    # substring cannot be the evidence that a restore SLA was met.
    row.sla_restore_met = (
        _sla_met(inc.restored_at, inc.sla_restore_due)
        if row.restore_minutes is not None or inc.restored_source in _TRUSTED_FOR_SLA
        else None
    )

    row.resolution_code = inc.resolution_code or ""
    if resolution is not None:  # frozen: keep the text scrubbed while the names were known
        row.resolution_summary = resolution.text
        row.restoring_note_id = resolution.restoring_note_id
    row.child_sites_down = int(inc.child_sites_down or 0)
    row.parent_incident_id = inc.parent_incident_id
    row.hour_of_day = hour
    row.month_of_year = month
    row.shift_type = shift
    row.closed_at = episode_closed_at(inc)
    row.built_at = utcnow()
    row.source_version = SOURCE_VERSION
    session.flush()

    if not frozen and ensure_memory_schema(session):
        _index_text(session, row, notes=notes, names=names)
    log.debug(
        "memory: %s episode for %s%s",
        "created" if created else "refreshed",
        inc.incident_number,
        " (pseudonymised: text frozen)" if frozen else "",
    )


# --------------------------------------------------------------------------- the job


def _not_current():
    """SQL predicate: this incident has no episode, or its episode is stale.

    Stale means built before the incident row last changed, or built by an older derivation
    (:data:`SOURCE_VERSION`). It is the same test :func:`_is_current` applies in Python, stated
    once in SQL so the job's work queue and the backfill agree on what "done" means.
    """
    return or_(
        MemoryEpisodeRow.id.is_(None),
        MemoryEpisodeRow.built_at < IncidentRow.updated_at,
        MemoryEpisodeRow.source_version != SOURCE_VERSION,
    )


def pending_incidents(
    session: Session,
    *,
    since: datetime | None,
    limit: int = BATCH_LIMIT,
    horizon: datetime | None = None,
) -> list[str]:
    """Finished incidents touched since ``since`` whose episode is missing or stale.

    The work queue is **derived from state**, not from a cursor — the ``pir_autoopen`` pattern
    (a bounded lookback plus "not already done", ``services/pir.auto_open``). That is what makes
    a tick exactly-once in effect without being exactly-once in mechanism:

    * an incident that closed *while the previous tick was running* is still "not current" and
      is picked up next time — a pure ``>= last_finished_at`` cursor would skip it, because the
      scheduler stamps ``last_finished_at`` after the job returns;
    * a tick that failed and rolled back leaves its incidents "not current", so the next tick
      redoes them — a cursor would have moved past them;
    * an incident consolidated once and not touched since is never re-read.

    Three timestamps bound the window because an incident becomes an episode by three routes:
    a supervisor marking it restored, a close, or any later edit (``updated_at`` is
    ``onupdate=utcnow``, so a corrected resolution text is re-derived). Oldest first, so a
    backlog drains in the order it happened.

    The honest limit: an incident that fails to consolidate *every* time stays at the head of
    this queue. It costs one slot of :data:`BATCH_LIMIT` per tick, is logged with a traceback
    each time and shows as ``failed=N`` / ``ok: false`` on the job's step, so it is visible —
    but fifty such rows would stall the queue until someone looks.
    """
    stmt = (
        _owned(IncidentRow)
        .outerjoin(MemoryEpisodeRow, MemoryEpisodeRow.incident_id == IncidentRow.id)
        .where(IncidentRow.status.in_(episode_statuses()), _not_current())
    )
    if horizon is not None:
        # Review M12: never offer an incident older than the prune horizon — the job would
        # rebuild what expire_memory removed, and consolidate_outcome would skip it anyway.
        stmt = stmt.where(episode_closed_at_expr() >= horizon)
    if since is not None:
        stmt = stmt.where(
            or_(
                IncidentRow.restored_at >= since,
                IncidentRow.closed_at >= since,
                IncidentRow.updated_at >= since,
            )
        )
    rows = session.scalars(stmt.order_by(IncidentRow.updated_at).limit(max(1, int(limit)))).all()
    return [r.id for r in rows]


def backfill_plan(
    session: Session, *, since: datetime | None = None, horizon: datetime | None = None
) -> dict[str, int]:
    """What a backfill would do, counted with the same predicate it uses. Writes nothing.

    ``since`` bounds on ``created_at`` here, as ``scripts/backfill_memory.py --since`` does —
    the backfill walks history by when it happened, the job by when it last changed.
    ``horizon`` is the 24-month prune line (review M12).
    """
    base = _owned(IncidentRow).where(IncidentRow.status.in_(episode_statuses()))
    if horizon is not None:
        base = base.where(episode_closed_at_expr() >= horizon)
    if since is not None:
        base = base.where(IncidentRow.created_at >= since)
    finished = len(list(session.scalars(base).all()))
    stale = len(
        list(
            session.scalars(
                base.outerjoin(MemoryEpisodeRow, MemoryEpisodeRow.incident_id == IncidentRow.id).where(
                    _not_current()
                )
            ).all()
        )
    )
    return {"finished": finished, "to_build": stale, "current": finished - stale}


def _last_finished(session: Session, job_name: str = JOB_NAME) -> datetime | None:
    state = session.get(ScheduledJobStateRow, job_name)
    return state.last_finished_at if state is not None else None


def _audit(session: Session, settings: AppSettings, action: str, payload: dict[str, Any]) -> None:
    """One ``AuditRow`` per batch (§7.11.8 auditability), ``payload_json`` as **real JSON**.

    ``graph/instrumentation.py`` writes ``str(dict)`` (brief defect #33); §4.5 requires
    ``json.dumps`` for new code, so this is the correct form and not the prevailing one.
    """
    session.add(
        AuditRow(
            id=new_id(),
            ts=utcnow(),
            operator_id=settings.operator.operator_id,
            actor=ACTOR,
            action=action,
            entity_type="memory",
            entity_id=GRAPH_NAME,
            rationale="deterministic consolidation; no LLM, no network (MEM6)",
            payload_json=json.dumps(payload, default=str),
        )
    )


def consolidate_recent(session: Session, settings: AppSettings) -> JobResult:
    """``memory_consolidate`` (§7.11.5): episodes for everything that finished since last tick.

    Re-checks ``MEMORY_ENABLED`` itself rather than trusting the card's ``default_enabled``,
    the same way ``pollers.weather.poll`` and ``services.pir.auto_open`` do: a card wired into
    ``SCHEDULED_JOBS`` on a machine that never opted in must be **inert**, not merely
    unscheduled. Off means no read, no write, one skipped step.

    Does not commit — the scheduler's runner commits the tick, which is also what makes the
    §4.5 "one short write transaction" shape true and what lets a failed tick roll back
    cleanly and be retried (consolidation is idempotent, so a retry costs nothing).
    """
    if not memory_enabled():
        return JobResult(
            summary=f"{ENABLED_ENV}=false — no episodes consolidated",
            rationale="feature flag off; memory writes nothing and reads nothing",
        )
    # A bounded look-back anchored on the last finish, not a cursor: the "not current" filter
    # in pending_incidents is what makes it exactly-once in effect, so the window only has to
    # be wide enough to survive a failed tick or a restart. Beyond it, the backfill script.
    anchor = _last_finished(session) or utcnow()
    since = min(anchor, utcnow()) - COLD_START_LOOKBACK
    ids = pending_incidents(session, since=since, limit=BATCH_LIMIT, horizon=_horizon(settings))
    written = 0
    frozen = 0
    failed = 0
    for incident_id in ids:
        try:
            # Each incident is its own SAVEPOINT inside consolidate_outcome (review M06): a
            # failure here has already rolled back that incident's row and FTS rows.
            outcome = consolidate_outcome(session, settings=settings, incident_id=incident_id)
        except Exception:  # noqa: BLE001 — fail-soft per incident: one bad ticket is not a tick
            failed += 1
            log.exception("memory: consolidating %s failed", incident_id)
            continue
        if outcome != SKIPPED:
            written += 1
        if outcome == TEXT_FROZEN:
            frozen += 1
    if written or failed:
        _audit(
            session,
            settings,
            AUDIT_ACTION,
            {
                "considered": len(ids),
                "written": written,
                "text_frozen": frozen,
                "failed": failed,
                "since": since.isoformat(),
            },
        )
    return JobResult(
        summary=f"episodes={written} considered={len(ids)} failed={failed} text_frozen={frozen}",
        rationale=(
            f"incidents RESTORED/CLOSED since {since.isoformat()}Z, capped at {BATCH_LIMIT} per tick "
            "(§4.5 contention budget); idempotent on incident_id; "
            f"{frozen} pseudonymised incident(s) refreshed WITHOUT re-deriving their text (review M02)"
        ),
        tools=(
            {
                "name": "memory.consolidate_incident",
                "ok": failed == 0,
                "rows": written,
                "text_frozen": frozen,
            },
        ),
    )


#: The scheduler card (§4.4 roster). **Not** registered in ``scheduler/loop.SCHEDULED_JOBS``
#: by this lane — that file belongs to integration, and adding
#: ``memory.consolidate.MEMORY_CONSOLIDATE_JOB`` to the tuple is the whole change.
#: ``default_enabled=False`` so ``/scheduler/status`` reports the job as off while
#: ``MEMORY_ENABLED`` is unset, rather than claiming it is enabled and producing nothing.
MEMORY_CONSOLIDATE_JOB = JobCard(
    JOB_NAME,
    INTERVAL_S,
    consolidate_recent,
    ENABLED_ENV,
    AGENT,
    GRAPH_NAME,
    max_seconds=60,
    default_enabled=False,
)


# --------------------------------------------------------------------------- retention


def expire_memory(
    session: Session,
    *,
    settings: AppSettings,
    now: datetime | None = None,
) -> dict[str, int]:
    """Prune what memory is no longer allowed to keep. Called by ``HousekeepingAgent`` (§9.4).

    §7.11.4 gives this function four duties. **M1 creates only ``memory_episodes``**, so only
    the fourth exists yet and the others land with the tables they operate on:

    * (a) close validity windows past ``expires_at`` — ``memory_facts``, M3;
    * (b) HARD DELETE ``memory_facts`` rows with ``contains_personal_data=1`` older than
      ``party_lookback_days`` — M3/Phase 6, the one place memory deletes rather than
      invalidates;
    * (c) expire shift memos past ``memo_max_age_days`` — ``memory_shift_memo``, M3;
    * (d) **prune ``memory_episodes`` older than 24 months** — here, now, with its FTS rows.

    Two properties ``services/housekeeping.py`` states and this function must keep:

    * it is called **regardless of ``MEMORY_ENABLED``** — retention must never depend on a
      *read* flag (§9.4). A lane switched off for reading still holds derived rows, and "we
      stopped looking at it" is not a defence for keeping it. So this function does not
      consult :func:`memory_enabled` and must not start;
    * it is **not called in dry run**, because it hard-deletes; the posture check is
      housekeeping's, not this function's.

    Does not commit: housekeeping runs one duty per short transaction and commits after each
    (``services/housekeeping.run``). Never raises — a missing table on a file that has never
    consolidated is "nothing to prune", and a failed prune must not fail the whole nightly
    run; it returns counts either way.
    """
    at = now or utcnow()
    horizon = at - timedelta(days=int(memory_settings(settings.operator)["episode_max_age_days"]))
    counts = {"episodes_pruned": 0, "fts_rows_pruned": 0}
    try:
        stale = list(
            session.scalars(
                _owned(MemoryEpisodeRow).where(MemoryEpisodeRow.closed_at < horizon)
            ).all()
        )
    except Exception:  # noqa: BLE001 — no table yet: nothing has ever been consolidated
        log.warning("memory: episode prune skipped", exc_info=True)
        return counts
    if not stale:
        return counts
    for row in stale:
        counts["fts_rows_pruned"] += _fts_prune(session, row.incident_id)
        session.delete(row)
    counts["episodes_pruned"] = len(stale)
    session.flush()
    if counts["fts_rows_pruned"]:
        # Review M09: a prune that leaves the pruned text's terms in the index's segment
        # b-trees has not erased it. secure-delete (memory/schema.py) removes them at the
        # DELETE; this merge is the backstop for an index built before that option was set,
        # or on a SQLite too old to have it. Once per prune, never per incident.
        optimize_index(session)
    _audit(
        session,
        settings,
        EXPIRE_AUDIT_ACTION,
        {
            "category": "episodes",
            "horizon": horizon.isoformat(),
            "episodes_pruned": counts["episodes_pruned"],
            "fts_rows_pruned": counts["fts_rows_pruned"],
            # Said out loud in the audit trail because it is the property a regulator would
            # ask about: this ran whether or not anyone could read memory that day.
            "gated_on_memory_enabled": False,
        },
    )
    return counts


def _fts_prune(session: Session, incident_id: str) -> int:
    """Drop one incident's FTS rows. Returns how many went; 0 when there is no index."""
    if not fts_available(session):
        return 0
    try:
        before = session.execute(
            sql(f"SELECT count(*) FROM {FTS_TABLE} WHERE incident_id = :iid"), {"iid": incident_id}
        ).scalar_one()
        _fts_delete(session, incident_id)
        return int(before or 0)
    except Exception:  # noqa: BLE001 — a cache that is not there needs no pruning
        return 0


# --------------------------------------------------------------------------- backfill


def consolidate_all(
    session: Session,
    *,
    settings: AppSettings,
    since: datetime | None = None,
    batch: int = BATCH_LIMIT,
    limit: int | None = None,
    rebuild: bool = False,
    on_batch: Callable[[int, int], None] | None = None,
) -> dict[str, int]:
    """Every finished incident, in batches — the engine behind ``scripts/backfill_memory.py``.

    Walks the incidents oldest-first by a stable ``(created_at, id)`` page cursor and
    consolidates each one. **Resumable and safe to re-run** (§7.11.5's backfill row): each
    write is idempotent, so an interrupted pass is continued by running the command again —
    it re-walks the list (one indexed lookup per already-built incident) but redoes none of
    the work, because ``rebuild=False`` skips every episode already built at the current
    :data:`SOURCE_VERSION` and no older than the incident it derives from.

    ``rebuild=True`` re-derives everything regardless, which is what a :data:`SOURCE_VERSION`
    bump needs and what no schema migration can do.

    Commits once per batch (§4.5's short-transaction rule), so an interruption loses at most
    the batch in flight. ``on_batch(considered, written)`` is called after each commit, so a
    CLI can show progress without this module importing anything that prints.
    """
    counts = {"considered": 0, "written": 0, "skipped": 0, "failed": 0, "text_frozen": 0}
    size = max(1, int(batch))
    offset = 0
    horizon = _horizon(settings)
    while True:
        ids = _backfill_batch(session, since=since, batch=size, offset=offset, horizon=horizon)
        if not ids:
            break
        offset += len(ids)
        for incident_id in ids:
            counts["considered"] += 1
            try:
                if not rebuild and _is_current(session, incident_id):
                    counts["skipped"] += 1
                    continue
                # One SAVEPOINT per incident, inside consolidate_outcome (review M06).
                outcome = consolidate_outcome(session, settings=settings, incident_id=incident_id)
                if outcome != SKIPPED:
                    counts["written"] += 1
                if outcome == TEXT_FROZEN:
                    counts["text_frozen"] += 1
            except Exception:  # noqa: BLE001 — one bad ticket must not end a backfill
                counts["failed"] += 1
                log.exception("memory: backfilling %s failed", incident_id)
            if limit is not None and counts["considered"] >= int(limit):
                break
        session.commit()
        if on_batch is not None:
            on_batch(counts["considered"], counts["written"])
        if limit is not None and counts["considered"] >= int(limit):
            break
    return counts


def _backfill_batch(
    session: Session,
    *,
    since: datetime | None,
    batch: int,
    offset: int,
    horizon: datetime | None = None,
) -> list[str]:
    """One page of finished incidents, oldest first, by a stable cursor.

    ``ORDER BY created_at, id`` is a total order, so successive pages never overlap and never
    skip — an ordering on ``created_at`` alone would do both whenever two tickets share a
    timestamp, which at a NOC ingesting a cascade is every hub outage. ``horizon`` keeps the
    backfill inside the 24-month line ``expire_memory`` prunes at (review M12).
    """
    stmt = _owned(IncidentRow).where(IncidentRow.status.in_(episode_statuses()))
    if horizon is not None:
        stmt = stmt.where(episode_closed_at_expr() >= horizon)
    if since is not None:
        stmt = stmt.where(IncidentRow.created_at >= since)
    rows = session.scalars(
        stmt.order_by(IncidentRow.created_at, IncidentRow.id).offset(max(0, int(offset))).limit(batch)
    ).all()
    return [r.id for r in rows]


def _is_current(session: Session, incident_id: str) -> bool:
    """True when this incident's episode is already built at the current source version and
    is not older than the incident row it derives from."""
    row = session.scalar(_owned(MemoryEpisodeRow).where(MemoryEpisodeRow.incident_id == incident_id))
    if row is None or int(row.source_version or 0) != SOURCE_VERSION:
        return False
    inc = session.scalar(_owned(IncidentRow).where(IncidentRow.id == incident_id))
    if inc is None or inc.updated_at is None or row.built_at is None:
        return False
    return row.built_at >= inc.updated_at


def episode_counts(session: Session) -> dict[str, int]:
    """Row counts for the lane, operator-scoped — the unbounded-growth tripwire of §7.11.4's
    ``GET /api/v1/memory/stats``. The route itself is M2; the numbers it needs live here so
    the backfill script and the job can print them without a second query to review."""
    episodes = len(list(session.scalars(_owned(MemoryEpisodeRow)).all()))
    fts = 0
    if fts_available(session):
        # Not operator-scoped: the FTS table has no operator column (memory/schema.py says
        # why). A size tripwire wants the physical row count anyway.
        fts = int(session.execute(sql(f"SELECT count(*) FROM {FTS_TABLE}")).scalar_one() or 0)
    return {"memory_episodes": episodes, FTS_TABLE: fts}
