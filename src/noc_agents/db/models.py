from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import (
    REAL,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    select,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, object_session, relationship, sessionmaker


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class IncidentRow(Base):
    __tablename__ = "incidents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    operator_id: Mapped[str] = mapped_column(String(32), index=True)
    incident_number: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="NEW")
    priority: Mapped[str] = mapped_column(String(8), default="P4")
    users_affected: Mapped[int] = mapped_column(Integer, default=0)
    service_affecting: Mapped[bool] = mapped_column(Boolean, default=True)
    services_impacted_json: Mapped[str] = mapped_column(Text, default="[]")
    site_id: Mapped[str] = mapped_column(String(64), index=True)
    site_name: Mapped[str] = mapped_column(String(128), default="")
    site_type: Mapped[str] = mapped_column(String(32), default="BTS")
    region_code: Mapped[str] = mapped_column(String(8), index=True)
    county: Mapped[str | None] = mapped_column(String(64), nullable=True)
    title: Mapped[str] = mapped_column(String(256), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    narrative: Mapped[str] = mapped_column(Text, default="")
    root_cause_hypothesis: Mapped[str] = mapped_column(Text, default="")
    impact_summary: Mapped[str] = mapped_column(Text, default="")
    assignee_type: Mapped[str] = mapped_column(String(32), default="UNASSIGNED")
    assignee_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    msp_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fe_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    access_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sla_ack_due: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sla_restore_due: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    correlation_fingerprint: Mapped[str] = mapped_column(String(256), index=True)
    is_hub_major: Mapped[bool] = mapped_column(Boolean, default=False)
    recurrence_count: Mapped[int] = mapped_column(Integer, default=1)
    problem_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    mpesa_risk: Mapped[bool] = mapped_column(Boolean, default=False)
    autonomy_level_applied: Mapped[str] = mapped_column(String(32), default="L2_GUARDED")
    requires_hitl: Mapped[bool] = mapped_column(Boolean, default=False)
    hitl_state: Mapped[str] = mapped_column(String(16), default="NONE")
    failure_domain: Mapped[str] = mapped_column(String(32), default="UNKNOWN")
    alarm_code: Mapped[str] = mapped_column(String(64), default="")
    next_update_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # --- Safaricom-style TT / NOC ticket fields ---
    # These were added after the first MVP. ``server_default`` is the SQL DEFAULT that
    # the generic migration (db/migrate.py) renders when it ADDs one of them to an older
    # file -- the same literals the retired _EXTRA_INCIDENT_COLS list carried.
    outage_start_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    technology: Mapped[str] = mapped_column(String(64), default="4G", server_default="4G")
    tt_category: Mapped[str] = mapped_column(String(64), default="OTHER", server_default="OTHER")
    tt_category_label: Mapped[str] = mapped_column(String(128), default="", server_default="")
    symptom_code: Mapped[str] = mapped_column(String(64), default="", server_default="")
    site_class: Mapped[str] = mapped_column(String(32), default="STANDARD", server_default="STANDARD")
    network_element: Mapped[str] = mapped_column(String(128), default="", server_default="")
    parent_hub_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    parent_incident_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    rnio_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vendor_tt_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    battery_countdown_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    child_sites_down: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    first_vendor_note_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    restored_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolution_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolution_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    assignment_rationale: Mapped[str] = mapped_column(Text, default="", server_default="")
    severity_rationale: Mapped[str] = mapped_column(Text, default="", server_default="")

    # Floor ticket fields (explicit)
    responsible_msp: Mapped[str | None] = mapped_column(String(64), nullable=True)
    radio_oem: Mapped[str] = mapped_column(String(32), default="MIXED", server_default="MIXED")
    failure_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # = outage start
    expected_resolution_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # MSP-filled as work progresses
    msp_eta_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    msp_root_cause: Mapped[str | None] = mapped_column(Text, nullable=True)
    msp_action_taken: Mapped[str | None] = mapped_column(Text, nullable=True)
    msp_percent_complete: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- Phase 1 (spec §7.0.8): restore provenance, vendor ref, incident flags ---
    # Created by the generic migration; written by later waves (lifecycle, restore API,
    # maintenance windows, cascade). Nothing reads them yet.
    restored_source: Mapped[str | None] = mapped_column(Text, nullable=True)  # MARK_RESTORED | VENDOR_NOTE_INFERRED | SUPERVISOR | ALARM_CLEAR
    restored_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    vendor_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    planned_maintenance: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    access_risk: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    child_site_ids_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    assignment_confidence: Mapped[str] = mapped_column(Text, default="high", server_default="high")

    notes: Mapped[list[WorkNoteRow]] = relationship(back_populates="incident")
    broadcasts: Mapped[list[BroadcastRow]] = relationship(back_populates="incident")

    @property
    def services_impacted(self) -> list[str]:
        return json.loads(self.services_impacted_json or "[]")

    @services_impacted.setter
    def services_impacted(self, value: list[str]) -> None:
        self.services_impacted_json = json.dumps(value)


class WorkNoteRow(Base):
    __tablename__ = "work_notes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id"), index=True)
    author: Mapped[str] = mapped_column(String(128))
    author_role: Mapped[str] = mapped_column(String(32))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    source: Mapped[str] = mapped_column(String(32), default="system")

    incident: Mapped[IncidentRow] = relationship(back_populates="notes")


# ``broadcasts.status`` vocabulary (spec §6.6). The column is a plain string with no CHECK
# constraint, so Phase 2 "adds values", not DDL: this tuple is the documented vocabulary and
# the reason the column does NOT need widening -- the longest member is PENDING_HITL (12) and
# every Phase 2 addition is <= 16 characters, so String(16) still holds. (Widening it would in
# any case be invisible to db/migrate.py, which never retypes an existing column.)
#
# Written today: DRAFTED (ORM default), QUEUED (services/notify.py), PENDING_HITL
# (agents/hitl.py), SENT/FAILED (the outbox dispatcher), CANCELLED (main.py:_cancel_pending_
# broadcasts). Added by Phase 2 and not written by anything yet: HELD, SUPPRESSED, DELIVERED.
#
# §2.1 R7: CANCELLED stays the status of a draft whose approval was rejected. SUPPRESSED on a
# BroadcastRow means a renderer/validator refused it (a *different* thing from the outbox
# row status of the same name), and it never replaces CANCELLED.
BROADCAST_STATUSES: tuple[str, ...] = (
    "DRAFTED",
    "QUEUED",
    "PENDING_HITL",
    "HELD",
    "SENT",
    "DELIVERED",
    "FAILED",
    "SUPPRESSED",
    "CANCELLED",
)


class BroadcastRow(Base):
    __tablename__ = "broadcasts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id"), index=True)
    channel: Mapped[str] = mapped_column(String(16))
    audience: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="DRAFTED")  # BROADCAST_STATUSES
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    incident: Mapped[IncidentRow] = relationship(back_populates="broadcasts")


class HitlTaskRow(Base):
    __tablename__ = "hitl_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # NULLABLE since schema_version 8. Until Phase 5 every gated thing WAS an incident and this
    # was NOT NULL. An APPROVE_SCHEDULE / APPROVE_MAINTENANCE_WINDOW card is about a programme of
    # work or a night's outage and has no incident, so the maintenance lane used to file it
    # against an unrelated "anchor" incident purely to satisfy the constraint (docs/PHASE5.md).
    # NULL now means exactly "this task is not about an incident": ``entity_type``/``entity_id``
    # say what it IS about and ``operator_id`` below says who owns it. Every reader that goes
    # incident -> tasks filters ``incident_id == inc.id`` and so never meets a NULL one.
    # Relaxing NOT NULL is the one thing db/migrate.py cannot do additively -- see
    # ``_rebuild_hitl_tasks`` there.
    incident_id: Mapped[str | None] = mapped_column(ForeignKey("incidents.id"), index=True, nullable=True)
    task_type: Mapped[str] = mapped_column(String(64))
    proposed_payload_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    resolved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    claimed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Phase 2 (spec §6.5, §7.5, Appendix A): run link, generic subject, raiser, edit flag ---
    # Created by the generic migration; nothing writes or reads them yet -- the approve/reject
    # handlers that do land in a later Phase 2 wave. Every one is nullable or defaulted, because
    # ALTER TABLE ADD COLUMN cannot backfill and the rows already in the file have no value.
    run_id: Mapped[str | None] = mapped_column(Text, nullable=True)  # agent_runs.id that raised the task
    # What the task is about. Existing rows are all incident tasks, so the DDL default backfills
    # them correctly; later types point at a scorecard line, notice, window or action (§7.5).
    entity_type: Mapped[str] = mapped_column(Text, default="incident", server_default="incident")
    # The subject's id. NULL on every row written before this column existed -- a reader falls
    # back to ``incident_id`` when it is NULL, which is exactly what those rows meant.
    entity_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Who raised the task. "Raiser != approver" (§6.5) is enforced by comparing this with the
    # actor, so NULL is the safe value for the rows that predate the column: NULL never equals
    # an actor, and the rule fails open rather than locking an old task out of approval.
    created_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 0/1, the source of M15's zero-edit counter: did the approver change the draft before send?
    edited: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))

    # --- schema_version 8: the task's OWN owner (docs/PHASE5.md, "The one that should be fixed first") ---
    # The operator that owns this task; ``api.deps._owned(HitlTaskRow)`` filters on it directly.
    # Until v8 the owner was derived by joining ``incident_id -> incidents.operator_id``, which
    # stopped being possible the day a task could exist without an incident (the long comment in
    # api/deps.py keeps both halves of that argument).
    #
    # A writer does NOT have to set it: ``_own_hitl_task`` below fills it from the incident when
    # the row is inserted, and refuses a row that has neither an incident nor an operator. That
    # is why agents/hitl.py, services/worklog_monitor.py, services/regulatory.py and
    # services/handover.py are unchanged -- they pass ``incident_id`` exactly as before.
    #
    # Nullable in the DDL, on purpose, although no row this code writes ever leaves it NULL:
    #   * rollback -- a release older than v8 knows nothing about this column and INSERTs without
    #     it. NOT NULL here would turn every HITL task such a release raises into an
    #     IntegrityError: the golden path would crash on the very file a rollback has to run on;
    #   * orphans -- the migration copies a task whose incident row is missing (possible: SQLite
    #     foreign keys are not enforced in this codebase) with its owner left NULL, rather than
    #     guessing an owner or refusing to start.
    # NULL fails CLOSED: ``operator_id = :op`` is never true for NULL, so an unowned row is
    # invisible to every operator -- which is what a task with no resolvable incident already
    # was under the join.
    operator_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)

    @property
    def proposed_payload(self) -> dict:
        return json.loads(self.proposed_payload_json or "{}")

    @proposed_payload.setter
    def proposed_payload(self, value: dict) -> None:
        self.proposed_payload_json = json.dumps(value)


class HitlTaskOwnershipError(ValueError):
    """A ``HitlTaskRow`` whose owning operator cannot be established, or contradicts its incident.

    Raised from inside the flush, so the writer's transaction fails instead of committing a
    task that no operator's inbox would ever show.
    """


@event.listens_for(HitlTaskRow, "before_insert")
def _own_hitl_task(mapper, connection, target: HitlTaskRow) -> None:
    """Give every inserted task a correct ``operator_id``, or refuse the row.

    The rule, case by case:

    * ``incident_id`` given, ``operator_id`` not -- every writer that existed before v8. The
      owner is the incident's operator, read here. An ``incident_id`` that resolves to no
      incident is refused: nothing could own that row.
    * both given -- they must agree. This is the answer to the old objection that a second copy
      of the owner "can drift from the first": the copy is checked against the original at the
      only moment it is written, and nothing in this codebase reassigns
      ``incidents.operator_id`` or ``hitl_tasks.operator_id`` afterwards.
    * ``operator_id`` only -- a task that is not about an incident (a maintenance window; the
      scorecard-dispute and vendor-notice cards after it). Owned directly.
    * neither -- refused. Such a row would be invisible to ``_owned`` for every operator: a
      pending approval nobody can see, let alone decide.

    Why a mapper event rather than an edit to each writer: there are six writers today, one of
    them (agents/hitl.py) on the golden path, and the next card type adds a seventh. A rule
    every writer has to remember is a rule one of them forgets, and this failure is silent --
    the card simply never appears in an inbox, fail-closed, so no test of the *writer* notices.
    Hung on the mapped class, the rule travels with the class: any Session, any sessionmaker,
    any caller that ``session.add()``s a HitlTaskRow passes through it.

    Why ``before_insert`` and not a Session-wide ``before_flush``: it is scoped to this one
    class instead of running on every flush in the process, and it is handed the flush's own
    ``connection``, so the lookup sees exactly what the INSERT will see -- including an incident
    written earlier in the same, still uncommitted, transaction.

    What it does NOT cover, because no mapper event can: Core ``insert(HitlTaskRow)``
    statements, ``bulk_insert_mappings`` and raw SQL. None exists in this codebase. A row
    written that way without ``operator_id`` is stored (the column is nullable, see above) and
    stays invisible to every operator until somebody sets it -- closed, not leaked.
    """
    stated = (target.operator_id or "").strip() or None
    derived = _incident_operator(connection, target) if target.incident_id else None
    if stated is None and derived is None:
        if not target.incident_id:
            raise HitlTaskOwnershipError(
                f"hitl task {target.id} ({target.task_type}) has neither incident_id nor operator_id, so no "
                "operator could ever see it; pass operator_id= for a task that is not about an incident"
            )
        raise HitlTaskOwnershipError(
            f"hitl task {target.id} ({target.task_type}) names incident {target.incident_id}, which does "
            "not exist, and carries no operator_id: its owner cannot be derived"
        )
    if stated is not None and derived is not None and stated != derived:
        raise HitlTaskOwnershipError(
            f"hitl task {target.id} ({target.task_type}) says operator {stated!r}, but its incident "
            f"{target.incident_id} belongs to {derived!r}"
        )
    # The incident's operator when there is one; otherwise the owner the writer stated outright
    # (a dangling incident_id beside a stated owner is no worse than it was before v8).
    target.operator_id = derived or stated


def _incident_operator(connection, task: HitlTaskRow) -> str | None:
    """``incidents.operator_id`` for ``task.incident_id``; None when there is no such incident.

    The Session's pending objects are searched first. The unit of work orders INSERTs by
    ``relationship()``, not by ForeignKey, and there is no relationship between these two
    classes -- so an incident added in the same flush as its task (tests/unit/
    test_phase2_schema.py does exactly that) has usually NOT been inserted yet when this runs.
    It is still in ``session.new`` until the flush ends.
    """
    session = object_session(task)
    if session is not None:
        for pending in session.new:
            if isinstance(pending, IncidentRow) and pending.id == task.incident_id:
                return pending.operator_id
    incidents = IncidentRow.__table__
    return connection.execute(
        select(incidents.c.operator_id).where(incidents.c.id == task.incident_id)
    ).scalar()


class ProblemRow(Base):
    __tablename__ = "problems"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    operator_id: Mapped[str] = mapped_column(String(32), index=True)
    problem_number: Mapped[str] = mapped_column(String(64), unique=True)
    signature: Mapped[str] = mapped_column(String(256), index=True)
    site_id: Mapped[str] = mapped_column(String(64), index=True)
    region_code: Mapped[str] = mapped_column(String(8))
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    status: Mapped[str] = mapped_column(String(32), default="OPEN")
    summary: Mapped[str] = mapped_column(Text, default="")
    linked_incident_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    dominant_failure_domain: Mapped[str] = mapped_column(String(32), default="UNKNOWN")
    # Known-error fields (§7.7.1). ITIL problem management: a PRB whose cause is understood
    # and which has a workaround is a "known error", and that workaround is what ENRICH
    # surfaces on the NEXT incident with the same signature -- which is the whole point of
    # recording it. Added by the generic additive migration; nullable because every existing
    # problem row predates them.
    root_cause: Mapped[str | None] = mapped_column(Text, nullable=True)
    workaround: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_known_error: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    known_error_since: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    permanent_fix_plan: Mapped[str | None] = mapped_column(Text, nullable=True)
    # A role token (RNIO, FE_CENTRAL, MSP_POWER), never a person's name -- §7.7.6.
    owner_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    closure_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    @property
    def linked_incident_ids(self) -> list[str]:
        return json.loads(self.linked_incident_ids_json or "[]")

    @linked_incident_ids.setter
    def linked_incident_ids(self, value: list[str]) -> None:
        self.linked_incident_ids_json = json.dumps(value)


class AuditRow(Base):
    __tablename__ = "audit_events"
    # The subject lookup. ``db.models_scorecards.earlier_released_card_exists`` (§7.6.2's shadow
    # rule) asks, for every card it checks, "is there a ``scorecard.published`` row for THIS
    # entity?" -- a correlated EXISTS on (entity_type, entity_id, action). With only the ``ts``
    # index that is a full scan of a keep-forever table on every check (measured ~1000x slower at
    # 2M rows). Column order is the predicate's: type, then id, then action, so the EXISTS is a
    # single index probe and a lookup by (entity_type, entity_id) alone uses the same prefix.
    # An index carries no data, so db/migrate.py creates a missing one on EVERY start, without a
    # version bump or a backup ("ON EVERY START" in its docstring); a fresh file gets it here.
    __table_args__ = (Index("ix_audit_events_entity_action", "entity_type", "entity_id", "action"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    operator_id: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(64))
    entity_type: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[str] = mapped_column(String(64))
    rationale: Mapped[str] = mapped_column(Text, default="")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")


class AgentRunRow(Base):
    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    incident_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    operator_id: Mapped[str] = mapped_column(String(32))
    graph_name: Mapped[str] = mapped_column(String(64), default="incident_lifecycle")
    trigger: Mapped[str] = mapped_column(String(32), default="EVENT")
    status: Mapped[str] = mapped_column(String(32), default="PENDING")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    current_node: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    steps: Mapped[list[AgentRunStepRow]] = relationship(back_populates="run", order_by="AgentRunStepRow.seq")


class AgentRunStepRow(Base):
    __tablename__ = "agent_run_steps"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    node_name: Mapped[str] = mapped_column(String(64))
    agent_name: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="STARTED")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    tools_called_json: Mapped[str] = mapped_column(Text, default="[]")
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    parent_step_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    run: Mapped[AgentRunRow] = relationship(back_populates="steps")

    @property
    def tools_called(self) -> list:
        return json.loads(self.tools_called_json or "[]")

    @tools_called.setter
    def tools_called(self, value: list) -> None:
        self.tools_called_json = json.dumps(value)


class SequenceRow(Base):
    __tablename__ = "daily_sequences"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # prefix+date
    last_value: Mapped[int] = mapped_column(Integer, default=0)


class ShiftLedgerRow(Base):
    __tablename__ = "shift_ledger"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    operator_id: Mapped[str] = mapped_column(String(32), index=True)
    shift_id: Mapped[str] = mapped_column(String(64))
    shift_type: Mapped[str] = mapped_column(String(16))
    incident_number: Mapped[str] = mapped_column(String(64))
    priority: Mapped[str] = mapped_column(String(8))
    site: Mapped[str] = mapped_column(String(128))
    site_type: Mapped[str] = mapped_column(String(32))
    region_code: Mapped[str] = mapped_column(String(8))
    owner: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[str] = mapped_column(String(32))
    last_note_summary: Mapped[str] = mapped_column(Text, default="")
    sla_risk: Mapped[bool] = mapped_column(Boolean, default=False)
    mpesa_risk: Mapped[bool] = mapped_column(Boolean, default=False)
    row_written_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class IncidentBriefRow(Base):
    __tablename__ = "incident_briefs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    incident_id: Mapped[str] = mapped_column(String(36), index=True)
    body: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# --------------------------------------------------------------------------------------
# Phase 1 platform tables (spec §7.0). Created by the generic migration in db/migrate.py;
# the code that writes and reads them lands in later waves (outbox drain, scheduler
# loop, LLM port). Column names, nullability and defaults follow the spec DDL 1:1.
# --------------------------------------------------------------------------------------


class OutboxRow(Base):
    """Transactional outbox (§7.0.2): side effects become rows, dispatched after commit."""

    __tablename__ = "outbox"
    __table_args__ = (Index("ix_outbox_status_next", "status", "next_attempt_at"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=new_id)
    operator_id: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    kind: Mapped[str] = mapped_column(Text)  # EMAIL | SMS | WHATSAPP | ICS_INVITE | EXCEL_ROW | LLM_CALL | REG_EVALUATE | PIR_OPEN | HITL_NUDGE
    idempotency_key: Mapped[str] = mapped_column(Text, unique=True)
    incident_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    hitl_task_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    alert_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_json: Mapped[str] = mapped_column(Text)  # ChannelPayload or job args; recipients as refs; never secrets
    envelope_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # NocAlert for channel kinds
    requires_hitl: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    approved_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # PENDING | HELD | CLAIMED | SENT | DELIVERED | FAILED | SUPPRESSED | REJECTED_UNAPPROVED | DEAD
    status: Mapped[str] = mapped_column(Text, default="PENDING", server_default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, server_default=text("3"))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    claimed_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    provider: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class SchedulerLeaseRow(Base):
    """One row per lease name (§7.0.3): whichever process holds it runs the ticker."""

    __tablename__ = "scheduler_lease"

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    owner: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    renewed_at: Mapped[datetime] = mapped_column(DateTime)


class ScheduledJobStateRow(Base):
    """Last outcome and circuit-breaker state of each scheduled job (§7.0.3)."""

    __tablename__ = "scheduled_job_state"

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    last_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    circuit_open: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))


class LlmCallRow(Base):
    """One row per model call (§7.0.9): tokens, cost, fallback and the audit row it cites."""

    __tablename__ = "llm_calls"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=new_id)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    operator_id: Mapped[str] = mapped_column(Text)
    agent: Mapped[str] = mapped_column(Text)
    purpose: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)
    model_requested: Mapped[str] = mapped_column(Text)
    model_used: Mapped[str | None] = mapped_column(Text, nullable=True)
    ok: Mapped[int] = mapped_column(Integer)
    refused: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    fallback_used: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    fallback_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cache_read_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    est_cost_usd: Mapped[float | None] = mapped_column(REAL, nullable=True)
    validated: Mapped[int | None] = mapped_column(Integer, nullable=True)
    run_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    incident_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    audit_id: Mapped[str] = mapped_column(Text)  # audit_events row carrying the reg 41(2) fields


# --------------------------------------------------------------------------------------
# Phase 2 message tables (spec §6.3, §6.6). Created by the generic migration in db/migrate.py;
# NOTHING reads or writes them yet -- ``services/templates.py:TemplateRegistry.sync`` and the
# delivery webhooks land in later Phase 2 waves. Column names, nullability and defaults follow
# the spec DDL 1:1, so the seam is what those waves expect to find.
# --------------------------------------------------------------------------------------


class MessageTemplateRow(Base):
    """Versioned, approval-aware template registry (§6.3), seeded from YAML.

    A template is *data*: ``config/operators/<op>/templates/*.yaml`` is synced into this table
    by an idempotent upsert on (channel, template_key, language, version). Any content change
    bumps ``version``; the version actually used is stamped on the payload and the broadcast so
    a sent message can always be traced back to the exact text that was approved.
    """

    __tablename__ = "message_templates"
    __table_args__ = (
        UniqueConstraint(
            "operator_id", "channel", "template_key", "language", "version",
            name="uq_message_templates_key",
        ),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=new_id)
    operator_id: Mapped[str] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(Text)  # EMAIL | SMS | WHATSAPP | INAPP
    # site_down_alert | incident_update | incident_restored | assignment_notice | chase_reminder
    # | vendor_notice | handover | regulatory_ca_24h | maintenance_invite | complaint_followup
    template_key: Mapped[str] = mapped_column(Text)
    language: Mapped[str] = mapped_column(Text)  # en | sw
    version: Mapped[int] = mapped_column(Integer)
    body: Mapped[str] = mapped_column(Text)  # Jinja2; rendered sandboxed with StrictUndefined
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)  # email only
    params_schema_json: Mapped[str] = mapped_column(Text)  # JSON Schema of the allowed variables
    provider_template_name: Mapped[str | None] = mapped_column(Text, nullable=True)  # WhatsApp: Meta name
    provider_language_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    # DRAFT | SUBMITTED | APPROVED | REJECTED | PAUSED (mirrors Meta's own states).
    # §6.4 hard rule: a `sw` row may not be APPROVED with `approved_by` NULL -- enforced by the
    # seeder and its test, not by the schema, because SQLite cannot express it as a constraint
    # the additive migration is allowed to add later.
    approval_status: Mapped[str] = mapped_column(Text, default="DRAFT", server_default="DRAFT")
    approved_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class DeliveryReceiptRow(Base):
    """What a provider said happened to one outbox row (§6.6).

    One outbox row can collect several receipts (Africa's Talking returns a per-recipient status
    on send and then posts a delivery report; WhatsApp posts sent/delivered/read separately), so
    this is a log, not a status column: ``outbox.status``/``delivered_at`` stay the summary.
    """

    __tablename__ = "delivery_receipts"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=new_id)
    outbox_id: Mapped[str] = mapped_column(Text, index=True)  # outbox.id; the operator is read through it
    provider: Mapped[str] = mapped_column(Text)  # africastalking | whatsapp | smtp
    # Africa's Talking messageId / Meta messages[0].id. Nullable: SMTP has no delivery receipt
    # and records ACCEPTED_BY_RELAY with no id of its own.
    provider_message_id: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    provider_status: Mapped[str] = mapped_column(Text)  # the provider's own word, stored verbatim
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    raw_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")  # the body as received


# --------------------------------------------------------------------------------------
# Phase 3 early-warning cache (spec §7.3.1). Created by the generic migration in
# db/migrate.py. Written by ``pollers/weather.py`` (and, in later waves, the KMD CAP,
# GloFAS flood, KPLC and complaint pollers); read by ENRICH and the Wallboard strip as a
# *cache*: nothing on the hot path ever waits on the network. Column names, nullability
# and defaults follow the spec DDL 1:1.
# --------------------------------------------------------------------------------------

#: ``external_signals.source`` vocabulary (§7.3.1).
SIGNAL_SOURCES: tuple[str, ...] = ("OPEN_METEO", "MET_NORWAY", "KMD_CAP", "GLOFAS", "KPLC", "COMPLAINTS")


class ExternalSignalRow(Base):
    """One fetched outside-world signal, kept so it can be read without the network (§7.3.1).

    A row is keyed by *kind + area + validity window*: ``(operator_id, source, external_id)``
    is unique, and for weather rows ``external_id`` is ``<region_code>:<poll bucket>`` so a
    re-run inside the same bucket upserts instead of duplicating, while successive polls keep
    a history for the §10.6 backtest and the M6 lead-time metric (``failure_time − fetched_at``).

    Staleness is computable from the row alone: ``fetched_at`` says when the provider was last
    reached and ``valid_until`` when the row stops being trustworthy. ``stale`` is the *stored*
    flag the poller sets when it fails past ``valid_until``; readers still recompute against
    ``utcnow()`` (``pollers.weather.is_stale``) so a dead poller cannot leave a row looking fresh.
    ``payload_json`` is the provider response as received; ``derived_json`` is the
    ``weather_risk`` block. ``last_error`` carries the most recent failure so an operator sees
    *why* the strip is stale, not just that it is.
    """

    __tablename__ = "external_signals"
    __table_args__ = (
        UniqueConstraint("operator_id", "source", "external_id", name="uq_external_signals_external_id"),
        Index("ix_signals_region_valid", "region_code", "valid_until"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=new_id)
    operator_id: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)  # OPEN_METEO | MET_NORWAY | KMD_CAP | GLOFAS | KPLC | COMPLAINTS
    source_url: Mapped[str] = mapped_column(Text)
    region_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    county: Mapped[str | None] = mapped_column(Text, nullable=True)
    site_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    valid_until: Mapped[datetime] = mapped_column(DateTime)
    stale: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    confidence: Mapped[float] = mapped_column(REAL, default=1.0, server_default=text("1.0"))
    storm_flag: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    flood_flag: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    planned_power: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    access_risk: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))  # engineers may not reach the site (rain/flood)
    payload_json: Mapped[str] = mapped_column(Text)
    derived_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # weather_risk block
    external_id: Mapped[str | None] = mapped_column(Text, nullable=True)  # CAP identifier / KPLC ULID / feed guid / weather bucket (dedupe)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SchemaVersionRow(Base):
    """One row per schema version ever applied to this file; the highest is current.

    A file without this table is version 1 (everything created before Phase 1).
    Written only by db/migrate.py, inside the migration's own transaction.
    """

    __tablename__ = "schema_version"

    version: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    applied_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


_engine = None
SessionLocal = None


def init_db(database_url: str, *, backup_dir: Path | None = None):
    """Open the database, bring its schema up to date and bind the session factory.

    Runs on every process start and in every test, so the usual case (schema already
    current) is one version read and one PRAGMA: no backup, no transaction.

    ``backup_dir`` is where the pre-migration copy goes when a migration is due; the
    default is ``<database file's folder>/backups`` (``data/backups`` for the shipped DB).
    """
    global _engine, SessionLocal
    # timeout: a second writer waits up to 30 s for the SQLite lock instead of failing at once.
    connect_args = {"check_same_thread": False, "timeout": 30} if database_url.startswith("sqlite") else {}
    _engine = create_engine(database_url, future=True, connect_args=connect_args)
    SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False, future=True)
    from noc_agents.db import models_all  # noqa: F401  registers every model module's tables
    from noc_agents.db.migrate import default_backup_dir, migrate_additive  # needs Base from here

    # The migration runs BEFORE create_all so the backup it takes is the file exactly as
    # the previous release left it -- create_all would already have added the new tables.
    # On a brand-new file it creates every table itself (and takes no backup).
    migrate_additive(_engine, backup_dir=backup_dir or default_backup_dir(_engine))
    # Belt and braces: the additive path is SQLite-only, so any other engine is built
    # here; on SQLite everything already exists and this is a catalogue check per table.
    Base.metadata.create_all(_engine)
    return _engine


def get_session():
    if SessionLocal is None:
        raise RuntimeError("DB not initialized")
    return SessionLocal()
