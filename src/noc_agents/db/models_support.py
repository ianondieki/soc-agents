"""The support desk's tables (docs/SUPPORT_DESK.md): complaints, the agents' steps, tool calls,
messages, and the eval runs.

Declared against the shared ``Base`` so the additive migration in ``db/migrate.py`` creates
them with no hand-written DDL (``db/models_all.py`` imports this module). They arrived in
schema_version 10, which exists so the first start that creates them takes a backup first.
schema_version 11 (docs/CLOSE_THE_LOOP.md) added the notices and surges tables and a few
nullable columns on complaints and messages (where the customer was told, still-down reports).

**Operator scoping (§8).** ``support_complaints`` and ``support_eval_runs`` carry their own
``operator_id`` and every read filters on it in the WHERE clause. Steps, tool calls and
messages hang off a complaint (``complaint_id``) and are only ever read *after* their
complaint has passed that filter, so they need no second copy of the owner.

**Personal data, kept small.** A customer complaint holds a name, a phone number and free
text. The MSISDN is stored normalised (it is what dedupe, repeat detection and the account
lookup key on) next to its masked form, and only the masked form leaves the API. These tables
are NOT yet classified in ``config/retention.yaml``: that file is the document Legal signs,
and choosing a retention period for customer complaints is their decision, so until then the
housekeeping job leaves them alone (an unclassified table is never purged).

**JSON columns** (``*_json``) hold the structured parts that are only ever read whole -- the
triage detail, citations, a step's detail, a tool's arguments and result, an eval report --
exactly as the rest of the schema does (``incidents.services_impacted_json`` and friends).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from noc_agents.db.models import Base, new_id, utcnow


class SupportComplaintRow(Base):
    """One customer complaint and where the desk took it."""

    __tablename__ = "support_complaints"
    __table_args__ = (
        UniqueConstraint("operator_id", "ref", name="uq_support_complaints_operator_ref"),
        Index("ix_support_complaints_operator_created", "operator_id", "created_at"),
        Index("ix_support_complaints_operator_msisdn", "operator_id", "msisdn", "created_at"),
        Index("ix_support_complaints_operator_status", "operator_id", "status"),
        # schema_version 11: who to tell when an incident is restored, and what a surge counts.
        Index("ix_support_complaints_operator_incident", "operator_id", "linked_incident_id"),
        Index("ix_support_complaints_operator_place", "operator_id", "place", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    operator_id: Mapped[str] = mapped_column(String(32))
    ref: Mapped[str] = mapped_column(String(16))  # CMP-000123
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    channel: Mapped[str] = mapped_column(String(16), default="web")
    #: The name the customer typed. Kept apart from ``account_holder`` (the name on the account
    #: the MSISDN belongs to) because only the first may be echoed to an anonymous caller: the
    #: public form must not tell whoever types a number whose number it is.
    customer_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    account_holder: Mapped[str | None] = mapped_column(String(128), nullable=True)
    msisdn: Mapped[str] = mapped_column(String(16))  # E.164, never serialised
    msisdn_masked: Mapped[str] = mapped_column(String(32))
    account_ref: Mapped[str | None] = mapped_column(String(32), nullable=True)
    language: Mapped[str] = mapped_column(String(8), default="en")
    subject: Mapped[str] = mapped_column(String(90), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    body_hash: Mapped[str] = mapped_column(String(64))
    category: Mapped[str] = mapped_column(String(32), default="other")
    urgency: Mapped[str] = mapped_column(String(16), default="normal")
    sentiment: Mapped[str] = mapped_column(String(16), default="calm")
    route: Mapped[str] = mapped_column(String(16), default="resolver")
    status: Mapped[str] = mapped_column(String(24), default="escalated")
    outcome: Mapped[str | None] = mapped_column(String(24), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    triage_json: Mapped[str] = mapped_column(Text, default="{}")
    escalation_reason_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    escalation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    claimed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reply: Mapped[str | None] = mapped_column(Text, nullable=True)
    citations_json: Mapped[str] = mapped_column(Text, default="[]")
    linked_incident_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    sla_due_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: Wall time of the automatic pipeline (intake to reply), for the median handle time.
    handle_ms: Mapped[int] = mapped_column(Integer, default=0)

    # --- schema_version 11: close the loop (docs/CLOSE_THE_LOOP.md) ----------------------------
    # All nullable: ALTER TABLE ADD COLUMN cannot backfill, and a v10 row simply has none of them.
    #: The place the customer named (normalised gazetteer name, "kayole"), the first one in the
    #: text. What the restore SMS names and what a surge is keyed on; NULL when none was named.
    place: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Why a ``closed`` complaint closed: ``service_restored`` when the restore SMS closed it.
    closure_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: When the customer was told service is back, and about WHICH incident: a still-down report
    #: can relink the complaint to a new ticket, which must tell them again when it is restored.
    told_restored_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    told_incident_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    #: The latest "still down" report from the Track page (each report is also a step).
    still_down_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # --- schema_version 12: link strength (docs/CLOSE_THE_LOOP.md) --------------------------------
    #: How ``linked_incident_id`` was set: ``site`` | ``county`` | ``wide_area`` (the desk's own strong
    #: links), ``person`` (a staff member or a surge's confirmer). NULL when not linked, and on rows
    #: linked before v12. A weak region-only match is never linked, so never recorded here.
    link_strength: Mapped[str | None] = mapped_column(String(16), nullable=True)


class SupportStepRow(Base):
    """One agent step in a complaint's trace: who, what, why (summary), detail, how long."""

    __tablename__ = "support_steps"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    complaint_id: Mapped[str] = mapped_column(String(36), ForeignKey("support_complaints.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    agent: Mapped[str] = mapped_column(String(16))
    action: Mapped[str] = mapped_column(String(32))
    summary: Mapped[str] = mapped_column(Text)
    detail_json: Mapped[str] = mapped_column(Text, default="{}")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SupportToolCallRow(Base):
    """One tool call by the action agent, with the policy that allowed, limited or refused it."""

    __tablename__ = "support_tool_calls"
    __table_args__ = (Index("ix_support_tool_calls_tool_subject", "tool", "subject_ref"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    complaint_id: Mapped[str] = mapped_column(String(36), ForeignKey("support_complaints.id"), index=True)
    tool: Mapped[str] = mapped_column(String(32))
    args_json: Mapped[str] = mapped_column(Text, default="{}")
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16))
    policy: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: What the call acted on -- an M-PESA code, a charge, a bundle, an incident -- so that
    #: "already reversed" and "re-credited in the last 30 days" are one indexed lookup.
    subject_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    decided_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SupportMessageRow(Base):
    """The conversation: the customer's complaint, the desk's replies, staff notes to the customer."""

    __tablename__ = "support_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    complaint_id: Mapped[str] = mapped_column(String(36), ForeignKey("support_complaints.id"), index=True)
    author: Mapped[str] = mapped_column(String(16))  # customer | agent | staff
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    body: Mapped[str] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    #: schema_version 11: how the message travelled when it was not the desk's own reply --
    #: ``sms`` for the restore and confirmed-outage messages, ``web`` for a Track-page report.
    #: NULL on every earlier row (the form and the desk's reply).
    channel: Mapped[str | None] = mapped_column(String(16), nullable=True)


class SupportEvalRunRow(Base):
    """One run of the eval suite; ``report_json`` is the contract's ``EvalReport``, whole."""

    __tablename__ = "support_eval_runs"
    __table_args__ = (Index("ix_support_eval_runs_operator_ran", "operator_id", "ran_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    operator_id: Mapped[str] = mapped_column(String(32))
    ran_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    mode: Mapped[str] = mapped_column(String(16))
    dataset_name: Mapped[str] = mapped_column(String(64))
    dataset_version: Mapped[str] = mapped_column(String(32))
    size: Mapped[int] = mapped_column(Integer)
    passed: Mapped[int] = mapped_column(Integer)  # 0/1, as the rest of the schema stores flags
    report_json: Mapped[str] = mapped_column(Text)


# --- schema_version 11: close the loop (docs/CLOSE_THE_LOOP.md) ------------------------------


class SupportNoticeRow(Base):
    """One "service is back" notice for an incident's customers: sent at once, or waiting on an
    ``APPROVE_CUSTOMER_UPDATE`` card, or rejected. The SMS themselves are outbox rows (one per
    number, keyed ``support-restore:{incident_id}:{msisdn_hash}``); this row is what the Outages
    tab and the incident panel read as the notice's state, and where a rejection's reason lives."""

    __tablename__ = "support_notices"
    __table_args__ = (Index("ix_support_notices_operator_incident", "operator_id", "incident_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    operator_id: Mapped[str] = mapped_column(String(32))
    incident_id: Mapped[str] = mapped_column(String(36))
    state: Mapped[str] = mapped_column(String(24))  # sent | awaiting_approval | held_back
    card_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    recipients: Mapped[int] = mapped_column(Integer, default=0)
    languages_json: Mapped[str] = mapped_column(Text, default="{}")
    text_en: Mapped[str | None] = mapped_column(Text, nullable=True)
    text_sw: Mapped[str | None] = mapped_column(Text, nullable=True)
    restore_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: schema_version 12: the note the restorer wrote (or the close's summary), shown on the card as
    #: the evidence that service is back (docs/CLOSE_THE_LOOP.md 7.2).
    restore_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class SupportSurgeRow(Base):
    """A burst of complaints about one place with no open incident: "Possible outage in Rongai".

    ``open_place`` is the place while the surge still collects complaints (``open``, or
    ``confirmed`` with no ticket yet because the ingest failed) and NULL once it is settled; the
    unique key on ``(operator_id, open_place)`` is what makes "one open surge per place" true
    even when two complaints race (SQLite treats NULLs as distinct, so settled surges never clash).
    """

    __tablename__ = "support_surges"
    __table_args__ = (
        UniqueConstraint("operator_id", "open_place", name="uq_support_surges_operator_open_place"),
        Index("ix_support_surges_operator_status", "operator_id", "status"),
        Index("ix_support_surges_operator_created", "operator_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    operator_id: Mapped[str] = mapped_column(String(32))
    place: Mapped[str] = mapped_column(String(64))  # normalised, as the gazetteer names it
    region_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: open | ingesting (a confirm holds it) | confirmed | dismissed | stale (its card was decided
    #: without the decision reaching the surge, e.g. while the desk was off)
    status: Mapped[str] = mapped_column(String(16), default="open")
    origin: Mapped[str] = mapped_column(String(16), default="complaints")  # complaints | still_down
    parent_incident_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    open_place: Mapped[str | None] = mapped_column(String(64), nullable=True)
    card_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    incident_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    complaints: Mapped[int] = mapped_column(Integer, default=0)
    numbers: Mapped[int] = mapped_column(Integer, default=0)
    first_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    decided_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: schema_version 12: what the confirm did -- ``ticket_opened`` (a synthetic alarm through the
    #: ingest) or ``linked_existing`` (a real open incident already covered the place).
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)


class SupportSurgeMemberRow(Base):
    """A complaint (``kind="complaint"``) or a still-down report on one (``kind="still_down"``,
    ``at`` = the report's time) counted in a surge. A complaint is counted once per surge per kind."""

    __tablename__ = "support_surge_members"
    __table_args__ = (UniqueConstraint("surge_id", "complaint_id", "kind", name="uq_support_surge_members"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    surge_id: Mapped[str] = mapped_column(String(36), ForeignKey("support_surges.id"), index=True)
    complaint_id: Mapped[str] = mapped_column(String(36), ForeignKey("support_complaints.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
