"""The support desk's tables (docs/SUPPORT_DESK.md): complaints, the agents' steps, tool calls,
messages, and the eval runs.

Declared against the shared ``Base`` so the additive migration in ``db/migrate.py`` creates
them with no hand-written DDL (``db/models_all.py`` imports this module). They arrived in
schema_version 10, which exists so the first start that creates them takes a backup first.

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
