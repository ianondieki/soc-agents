"""Post-incident review records and their action items (spec §7.7.1, ``PIR_ENABLED=false``).

Two tables, declared against the shared ``Base`` from ``db/models.py`` so the generic
additive migration in ``db/migrate.py`` creates them with no hand-written DDL
(``db/models_all.py`` already imports this module, which is what puts them into
``Base.metadata`` before ``migrate_additive`` walks it).

The known-error columns of §7.7.1 are **not** here: they belong to ``problems``, an
existing table, so they live on ``ProblemRow`` in ``db/models.py`` next to the rest of
that row. This module holds only the two tables the spec creates new.

Why the narrative lives in its own table rather than as columns on ``incidents``:
an incident row is written by the lifecycle graph on every event and read by the
wallboard many times a second; a PIR is written once by a human, days later, and read
by nobody in a hurry. They also die at different times — §7.7.6 keeps the review with
the incident record for the licence Condition 12.2 retention (≥ 3 years), while the
operational incident row is subject to the §9.4 column-class rules. Separate lifetimes,
separate tables.

Operator scoping (§8): ``post_incident_reviews`` carries its own ``operator_id``, so
``api.deps._owned`` filters it directly with no join. ``pir_action_items`` deliberately
does **not** carry one — see the comment on :class:`PirActionItemRow`.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from noc_agents.db.models import Base, new_id, utcnow

__all__ = ["PirActionItemRow", "PostIncidentReviewRow"]


class PostIncidentReviewRow(Base):
    """One blameless post-incident review (Google SRE postmortem template, §7.7).

    ``status`` vocabulary: ``DRAFT | IN_REVIEW | PUBLISHED | NOT_REQUIRED``. A plain
    string with no CHECK constraint, matching how ``broadcasts.status`` and
    ``incidents.status`` are handled in this schema — the vocabulary is enforced by the
    service layer (``services/pir.py``), which is also where the transitions live.

    ``NOT_REQUIRED`` is a real row, not the absence of one: a CANCELLED incident (a false
    alarm, a duplicate) must record *that a review was considered and is not needed*,
    otherwise "no PIR" is indistinguishable from "nobody looked" — which is precisely the
    gap §7.7 exists to close ("no postmortem left unreviewed").

    ``trigger`` (what set the incident off) is kept apart from ``root_causes`` (what let
    it become an outage) because the SRE template insists on the separation: conflating
    them is how a review ends at "the generator failed" instead of "the generator failed
    AND the low-fuel alarm had been suppressed for six weeks". ``TRIGGER`` is a SQLite
    keyword; SQLAlchemy quotes it in both ``CREATE TABLE`` and ``ALTER TABLE ADD COLUMN``
    (``migrate._add_column_sql`` goes through the dialect's identifier preparer), so the
    spec's column name is kept as written.
    """

    __tablename__ = "post_incident_reviews"
    __table_args__ = (Index("ix_pir_operator_status", "operator_id", "status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    operator_id: Mapped[str] = mapped_column(String(32), index=True)
    # UNIQUE is the idempotency guarantee the 5-minute auto-open job leans on: two ticks
    # that both decide the same incident needs a review cannot produce two reviews, even
    # if they overlap. The job checks first and inserts under a savepoint; this constraint
    # is what makes that check safe rather than merely likely.
    incident_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="DRAFT", server_default="DRAFT")
    # P1_P2 | HUB_CORE | SLA_BREACH | PROBLEM_LINKED | RUN_FAILED | MANUAL — which rule of the
    # §5.3.18 trigger matrix opened this review. Kept because "why is there a PIR for this?"
    # is the first question asked of every auto-opened one.
    opened_reason: Mapped[str] = mapped_column(String(32))

    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # {users_affected, duration_minutes, adjusted_duration_minutes, services, revenue_note}
    impact_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")

    detection_method: Mapped[str | None] = mapped_column(Text, nullable=True)
    detected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    trigger: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The two blameless-validated fields (§7.7.3). Everything the validator rejects is
    # rejected on the way IN, so a published review cannot contain a person's name.
    root_causes: Mapped[str | None] = mapped_column(Text, nullable=True)
    contributing_factors: Mapped[str | None] = mapped_column(Text, nullable=True)

    # REAL, not INTEGER: a 4-minute-30-second MTTA rounded to 4 or 5 minutes is a different
    # SLA answer, and these numbers get quoted in vendor conversations.
    mtta_minutes: Mapped[float | None] = mapped_column(Float, nullable=True)
    mttr_minutes: Mapped[float | None] = mapped_column(Float, nullable=True)
    adjusted_mttr_minutes: Mapped[float | None] = mapped_column(Float, nullable=True)
    # [{ts, kind, title, detail, actor_role}] assembled from the operational tables (§7.7.3).
    # Stored, not recomputed on read: the timeline is evidence of what the record looked like
    # when a human reviewed it, and the source rows (work notes, HITL tasks) keep changing.
    timeline_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")

    went_well: Mapped[str | None] = mapped_column(Text, nullable=True)
    went_poorly: Mapped[str | None] = mapped_column(Text, nullable=True)
    got_lucky: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 1 whenever a model touched any of the text (§7.7.6). Set when the draft is QUEUED, not
    # when it returns — see services/pir.queue_llm_draft for why the conservative side is the
    # correct one for a disclosure flag.
    ai_assisted: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    # The named human who published. Never a role token and never an agent: §5.3.18 makes
    # publishing an A2 act, and an unattributable postmortem is not a postmortem.
    reviewer: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class PirActionItemRow(Base):
    """One typed action item with exactly one owner and a due date (§7.7.1).

    **Operator scoping.** This table has no ``operator_id`` column, on purpose, and it is
    not registered with ``api.deps.register_owned_via_incident`` either — that door is for
    tables owned through ``incident_id -> incidents.operator_id``, and an action item has
    no incident id. It is owned through its parent review:
    ``pir_action_items.pir_id -> post_incident_reviews.operator_id``. Every route reaches
    the children by fetching the parent first with
    ``_get_owned(session, PostIncidentReviewRow, pir_id, what="PIR")`` — which applies the
    operator clause and 404s (never 403s) on another operator's review — and only then
    filtering on ``pir_id == pir.id``. The rows a caller can see are therefore bounded by
    the parent fetch, in the WHERE clause, not by a check after the read. A denormalised
    ``operator_id`` here would be a second copy of a derivable value that can drift from
    the first; see the same argument for ``hitl_tasks`` in ``api/deps.py``.

    ``owner_token`` is a role token (RNIO, FE_CENTRAL, MSP_POWER), never a person's name
    (§7.7.6): the action outlives the person on shift, and a name here is exactly the
    blame the rest of this lane is built to keep out.
    """

    __tablename__ = "pir_action_items"
    __table_args__ = (Index("ix_pir_actions_pir_status", "pir_id", "status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    pir_id: Mapped[str] = mapped_column(String(36), index=True)
    # prevent | mitigate | detect | repair | investigate — the SRE template's typed actions.
    # The type matters because a review whose every action is "repair" has learned nothing:
    # the split is what makes "we only ever fix, we never prevent" visible on the card.
    type: Mapped[str] = mapped_column(String(16))
    priority: Mapped[str] = mapped_column(String(4))  # P0 | P1 | P2 | P3
    description: Mapped[str] = mapped_column(Text)
    owner_token: Mapped[str] = mapped_column(Text)
    due_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), default="OPEN", server_default="OPEN")
    # Set when the action is "raise/attach a PRB": links the review to problem management so
    # the permanent fix has one home rather than two.
    problem_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # Free text for whatever the operator actually tracks work in (a JIRA key, a change
    # number). Deliberately not a foreign key to anything: the tracker is not this system.
    tracking_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
