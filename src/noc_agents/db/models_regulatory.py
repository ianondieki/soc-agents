"""Regulatory notification clocks and immutable evidence packs (spec §7.6.1) — Phase 4 Lane 4A.

Two tables, both created by the generic additive migration (``db/migrate.py`` walks
``Base.metadata``; ``db/models_all.py`` imports this module before it runs), so there is no
hand-written DDL here and no ``SCHEMA_VERSION`` to bump.

``regulatory_notifications``
    One row per (incident, regulatory obligation). The obligation is a **clock**, and the
    clock is the reason the table exists at all: CA licence Condition 9.2 gives 24 hours
    from a significant unforeseen interruption to notify the Authority in writing, and DPA
    2019 s.43 gives 72 hours from becoming aware of a personal-data breach. Both are
    measured from the *event*, not from the moment this system noticed it, so
    ``clock_started_at`` is copied from ``incidents.failure_time`` and ``due_at`` is derived
    from it once, at row creation, and then never recomputed. A row opened six hours late
    is born with six hours already gone — which is precisely what makes
    ``significance_json.reason_for_delay`` (§9.2, the s.43 row) a meaningful field rather
    than an apology template.

    ``status`` vocabulary: ``DRAFT | PENDING_APPROVAL | SENT | NOT_REQUIRED``. A plain
    string with no CHECK, matching ``incidents.status`` and ``broadcasts.status``; the
    transitions and the vocabulary are enforced by ``services/regulatory.py``, which is also
    the only module allowed to write this table.

    **There is deliberately no ``APPROVED`` status.** Approval is not a state of the
    notification, it is a *fact about a named human* recorded on ``hitl_tasks`` and mirrored
    here as ``approved_by`` / ``approved_at``. Metric M10 is "100 % drafted; **0 auto-sent**",
    and a status called APPROVED would invite code that treats the string as permission to
    transmit. Permission is the APPROVED ``HitlTaskRow`` at ``hitl_task_id`` and nothing
    else — see ``services/regulatory.release_notice``.

``evidence_packs``
    Append-only. A pack is the bytes the CA (or a vendor, or a tribunal) was shown, and
    ``sha256`` is what makes "the same bytes" provable three years later under licence
    Condition 12.2. Rows are therefore **never updated**: a fresh generation whose content
    differs is a new row, and the old row keeps its own hash. There is no UNIQUE on
    ``incident_id`` for that reason.

    The hash covers ``pack_json`` only. ``generated_at`` and ``generated_by`` are columns,
    not pack fields, because a generation timestamp inside the hashed content would make
    the hash change on every read and the §7.6.8 exit criterion ("evidence pack hash
    stable") unachievable by construction. See ``services/evidence.py`` for the canonical
    serialisation that the hash is taken over.

Operator scoping (§8): both tables carry their own ``operator_id`` (the spec's DDL does),
so ``api.deps._owned`` filters them directly with no join and there is nothing to register
with ``register_owned_via_incident``.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from noc_agents.db.models import Base, new_id, utcnow

__all__ = [
    "NOTIFICATION_STATUSES",
    "NOT_REQUIRED",
    "REGULATORY_KINDS",
    "DRAFT",
    "PENDING_APPROVAL",
    "SENT",
    "EvidencePackRow",
    "RegulatoryNotificationRow",
]

#: ``regulatory_notifications.kind`` vocabulary (§7.6.1), in the spec's order. A closed set:
#: each kind carries its own statutory deadline and its own recipient, so a typo must fail at
#: the service boundary rather than become a fifth obligation nobody has a deadline for.
REGULATORY_KINDS: tuple[str, ...] = (
    "CA_OUTAGE_24H",  # CA licence Condition 9.2 — 24 h written notice of a significant interruption
    "ODPC_BREACH_72H",  # DPA 2019 s.43 — 72 h from becoming aware, with reasons for any delay
    "CII_24H",  # Computer Misuse & Cybercrimes critical-infrastructure notification
    "CBK_FACTSHEET",  # Central Bank factsheet for an M-PESA-affecting outage
)

#: ``regulatory_notifications.status`` vocabulary (§7.6.1). See the module docstring for why
#: ``APPROVED`` is not one of them.
DRAFT = "DRAFT"
PENDING_APPROVAL = "PENDING_APPROVAL"
SENT = "SENT"
NOT_REQUIRED = "NOT_REQUIRED"
#: The four §7.6.1 names ONLY. This is no longer the whole vocabulary: the service adds
#: ``QUEUED`` (approved and on the outbox, nothing transmitted) and ``SEND_FAILED`` (the
#: outbox row reached a terminal non-SENT outcome), because the four above cannot tell
#: "we queued it" apart from "it arrived" -- and the row is the evidence for the CA's
#: 24-hour obligation, so that gap read as a discharged obligation that was not discharged.
#:
#: ``services.regulatory.SERVICE_STATUSES`` is the authoritative list and the service is
#: where the vocabulary is enforced (this module declares no CHECK constraint; the column is
#: plain TEXT, which is why the addition cost no migration). Kept here as the record of what
#: the spec enumerates, and deliberately NOT widened: a reader comparing the two should see
#: the deviation, not have it hidden from them.
NOTIFICATION_STATUSES: tuple[str, ...] = (DRAFT, PENDING_APPROVAL, SENT, NOT_REQUIRED)


class RegulatoryNotificationRow(Base):
    """One regulatory obligation on one incident, with its clock (§7.6.1)."""

    __tablename__ = "regulatory_notifications"
    __table_args__ = (
        # The idempotency guarantee the 5-minute sweep and the post-commit REG_EVALUATE
        # outbox row both lean on: two evaluations of the same incident cannot open two
        # CA_OUTAGE_24H clocks, whichever one commits first. Without it a second row would
        # be opened with a *later* clock_started_at only if failure_time had moved — but it
        # would also be opened with a second HITL card, and two approvals for one notice is
        # exactly the confusion that gets the wrong version sent.
        UniqueConstraint("incident_id", "kind", name="uq_regulatory_notifications_incident_kind"),
        # The sweep's query: this operator's live clocks, soonest deadline first.
        Index("ix_regulatory_operator_status_due", "operator_id", "status", "due_at"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=new_id)
    operator_id: Mapped[str] = mapped_column(Text, index=True)
    kind: Mapped[str] = mapped_column(Text)  # REGULATORY_KINDS
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id"), index=True)

    # THE CLOCK. Copied from incidents.failure_time at creation — never utcnow(), never the
    # detection time, never created_at. due_at is derived once from clock_started_at and is
    # not recomputed on read: a deadline that moves when you look at it is not a deadline.
    clock_started_at: Mapped[datetime] = mapped_column(DateTime)  # naive UTC, the storage contract
    due_at: Mapped[datetime] = mapped_column(DateTime)  # = clock_started_at + the kind's statutory hours

    status: Mapped[str] = mapped_column(Text, default=DRAFT, server_default=DRAFT)

    # {rule_matched, yaml_path, checks{}, countdown_fired[], reason_for_delay, release{}}.
    # §7.6.1 shows {rule_matched, yaml_path}; §9.2 (the DPA s.43 row) additionally requires
    # reason_for_delay to live here when sent_at > due_at. It is therefore already a bag
    # rather than a fixed shape, and the countdown's fired-threshold record and the release
    # record live in it too instead of costing the spec's DDL two extra columns.
    significance_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    # The NocAlert envelope (scope=RESTRICTED, audience=REGULATOR) the approver reviewed,
    # as JSON. Nullable: a profile whose incident numbers fall outside §6.1 gets no envelope
    # and a plain-text draft instead (services/hitl.compose_alert is fail-soft), which is a
    # degraded draft, not a missing obligation.
    draft_alert_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The named human. Mirrored from the APPROVED HitlTaskRow at approval time so that a
    # reader of this row alone can answer "who let this go to the regulator?"; the task is
    # still the authority, and release_notice re-reads it rather than trusting these two.
    approved_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # The regulator's own reference for the notice, recorded by a human after the fact.
    external_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The APPROVE_REGULATORY_NOTICE task. NULL until approval is requested; it is the ONLY
    # thing that can authorise a send (services/regulatory.release_notice).
    hitl_task_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The pack attached to the notice, so what the regulator was told and what it was told
    # from are one provable pair.
    evidence_pack_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    @property
    def significance(self) -> dict[str, Any]:
        return json.loads(self.significance_json or "{}")

    @significance.setter
    def significance(self, value: dict[str, Any]) -> None:
        # sort_keys so two writes of the same facts produce the same bytes; the countdown's
        # compare-and-set (services/regulatory) matches on this column's exact text.
        self.significance_json = json.dumps(value, sort_keys=True, default=str)

    @property
    def draft_alert(self) -> dict[str, Any] | None:
        return json.loads(self.draft_alert_json) if self.draft_alert_json else None

    @draft_alert.setter
    def draft_alert(self, value: dict[str, Any] | None) -> None:
        self.draft_alert_json = json.dumps(value, sort_keys=True, default=str) if value is not None else None

    @property
    def is_open(self) -> bool:
        """The clock is still running: the notice is neither sent nor ruled out."""
        return self.status in (DRAFT, PENDING_APPROVAL)


class EvidencePackRow(Base):
    """One immutable evidence pack for one incident (§7.6.1 ``evidence_packs``).

    Append-only by contract. Nothing in ``services/evidence.py`` issues an UPDATE against
    this table, and nothing should: the value of a pack is that the bytes behind the hash
    cannot have changed since a human quoted them.
    """

    __tablename__ = "evidence_packs"
    __table_args__ = (
        # "the latest pack for this incident" is the read the API does on every workspace
        # load; "every pack for this incident, oldest first" is the audit read.
        Index("ix_evidence_packs_incident_generated", "incident_id", "generated_at"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=new_id)
    operator_id: Mapped[str] = mapped_column(Text, index=True)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id"), index=True)
    # OUTSIDE the hash, on purpose — see the module docstring.
    generated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    generated_by: Mapped[str] = mapped_column(Text)
    #: Hex sha256 of the canonical serialisation of ``pack_json`` (services/evidence.py).
    sha256: Mapped[str] = mapped_column(Text, index=True)
    pack_json: Mapped[str] = mapped_column(Text)

    @property
    def pack(self) -> dict[str, Any]:
        return json.loads(self.pack_json or "{}")
