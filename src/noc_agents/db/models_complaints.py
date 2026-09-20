"""The confidential complaint intake and its restricted subject register (§7.8.1, §5.3.21).

Two tables, declared against the shared ``Base`` from ``db/models.py`` so the generic
additive migration in ``db/migrate.py`` creates them with no hand-written DDL
(``db/models_all.py`` already imports this module, which is what puts them into
``Base.metadata`` before ``migrate_additive`` walks it). ``SCHEMA_VERSION`` is untouched:
these are new tables, not a change to an existing one.

**This is the most sensitive data in the system, and the sensitivity runs sideways.**
An incident record is confidential from the public; a complaint is confidential from
*colleagues* — above all from the person it is about, and from anyone who could tell them.
Everything below is shaped by that one fact:

* the subject of a complaint is never a name in this table. ``subject_role_token`` is a
  role code (``FE-NBI-E-01``, ``MSP-EGYPRO-POWER``) and ``subject_person_ref`` is an opaque
  key into :class:`SubjectPersonRow`, which only ``legal``/``admin`` may read (§9.3). The
  token↔name mapping therefore lives in exactly one table, with its own RBAC, and a leak of
  ``relationship_complaints`` on its own names nobody;
* ``description`` is free text and free text is where minimisation dies, so it is short by
  construction (``services/complaints.MAX_DESCRIPTION_CHARS``), refused when it carries an
  MSISDN or an e-mail, and refused when it spells out the subject's own name — DPA 2019
  s.25 (minimisation), enforced on the way IN by ``services/complaints.validate_complaint``
  rather than cleaned up later;
* ``subject_person_ref`` is indexed *with* ``operator_id`` because DPA 2019 s.26 gives the
  subject the right to know what is held about them, and a right that costs a full scan of
  every complaint's free text is a right the operator will not honour on request. See
  ``services/complaints.subject_access`` for what that index can and cannot answer.

**Why the complaint is not a column on ``incidents``.** An incident row is written by the
lifecycle graph on every event and read by the wallboard many times a second; a complaint is
written once by a person, read by two or three people ever, and must be invisible to most of
the roles that read incidents. They also die at different times: §9.4 keeps network facts for
the licence's three years and reduces a complaint's free text at 24 months. Separate
lifetimes, separate RBAC, separate tables.

Operator scoping (§8): both tables carry their own ``operator_id``, so ``api.deps._owned``
filters them directly with no join. ``incident_id`` is deliberately NOT the ownership path —
a complaint may have no incident at all.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from noc_agents.db.models import Base, new_id, utcnow

__all__ = ["RelationshipComplaintRow", "SubjectPersonRow"]


class RelationshipComplaintRow(Base):
    """One filed complaint about a vendor or an individual (§7.8.1 ``relationship_complaints``).

    ``status`` vocabulary: ``OPEN | ACKNOWLEDGED | IN_REVIEW | RESOLVED | WITHDRAWN``, a plain
    string with no CHECK constraint — the same treatment ``incidents.status`` and
    ``post_incident_reviews.status`` get in this schema. The vocabulary and the legal
    transitions live in ``services/complaints.py``, which is also where the RBAC on each
    transition is decided.

    ``category`` is an enumerated list, not free text, and that is a compliance mechanism
    rather than a convenience: §7.8.6 reads DPA s.25 minimisation as "enumerated categories,
    minimal free text", so the category is what the operator reports on and the description
    is what gets reduced at 24 months (§9.4).

    ``classification_ai_assisted`` records that a model suggested the category or severity
    that was ultimately filed. It is a disclosure flag, not a decision record: under DPA 2019
    s.35 no automated system may decide anything about a person here, so the classifier only
    ever pre-fills a form a human submits (§5.3.21, autonomy A2) — see
    ``services/complaints.classify``.
    """

    __tablename__ = "relationship_complaints"
    __table_args__ = (
        # The manager queue and the stats route (counts only) both read operator+status.
        Index("ix_complaints_operator_status", "operator_id", "status"),
        # "Show me everything held about this person" — DPA 2019 s.26. Indexed rather than
        # scanned: an access request that costs a table scan of free text is one the
        # operator answers late, badly, or not at all. See the module docstring.
        Index("ix_complaints_subject_ref", "operator_id", "subject_person_ref"),
        # An engineer's own filings (§7.8.2: "own for engineers") and the reminder sweep's
        # "overdue and not acknowledged" both need these.
        Index("ix_complaints_filed_by", "operator_id", "filed_by"),
        Index("ix_complaints_follow_up", "operator_id", "follow_up_due_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    operator_id: Mapped[str] = mapped_column(String(32), index=True)

    # The complainant. A name, deliberately: a confidential complaint is not an anonymous
    # one — Employment Act 2007 s.41 requires the person complained about to be able to
    # answer the allegation, and s.43 puts the burden of proving the reason for a dismissal
    # on the employer, so an untraceable allegation is worthless as evidence and unfair as
    # a process. It is also why this column never appears in the subject-access export
    # (``services/complaints.subject_access``): who may see it is an RBAC decision, not a
    # storage one.
    filed_by: Mapped[str] = mapped_column(Text)
    filed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    # VENDOR | INDIVIDUAL. A complaint about a company and a complaint about a person are
    # different legal objects: the first is contract management, the second is employee
    # personal data with s.35, s.31 and Employment Act consequences. Keeping them in one
    # table with an explicit discriminator means the stricter rules cannot be forgotten —
    # every validator, every RBAC check and the retention rule all branch on this column.
    subject_type: Mapped[str] = mapped_column(String(16))
    vendor_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # A role code, never a person's name (§5.3.21 "subject persons referenced by role token
    # + pseudonymous key"). ``services/complaints.is_role_token`` is the shape check.
    subject_role_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Pseudonymous key into ``subject_persons`` — the restricted table. NULL for a VENDOR
    # complaint, and NULL is also legitimate for an INDIVIDUAL one where the filer names
    # only a role: not knowing exactly who was on the ladder is a normal state of the world,
    # and forcing a ref would make people guess at one.
    subject_person_ref: Mapped[str | None] = mapped_column(String(36), nullable=True)

    incident_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # NO_SHOW | LATE_ARRIVAL | UNSAFE_PRACTICE | POOR_COMMUNICATION | ACCESS_ISSUE | CONDUCT | OTHER
    category: Mapped[str] = mapped_column(String(32))
    # Validated on the way in: no MSISDN, no e-mail, length-capped, and never the subject's
    # own name (§7.8.6 → DPA s.25). Reduced to category + resolution at 24 months (§9.4).
    description: Mapped[str] = mapped_column(Text)
    # Work-note ids only. The evidence stays where it was written; copying it here would
    # make a second copy of the same words with different retention and different RBAC.
    evidence_note_ids_json: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    severity: Mapped[str] = mapped_column(String(8))  # LOW | MEDIUM | HIGH
    status: Mapped[str] = mapped_column(String(16), default="OPEN", server_default="OPEN")

    # The named manager handling it. Named, not a role token: someone has to be answerable
    # for a complaint, and "MANAGEMENT" is nobody. ``services/complaints`` refuses to assign
    # a manager who is the subject of the same complaint.
    assigned_manager: Mapped[str | None] = mapped_column(Text, nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Default five working days from filing (§7.8.3). The reminder that fires here carries
    # counts and references only — never the complaint text (§9.5's rule, applied here).
    follow_up_due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # When the free text stops being kept (§9.4: 24 months). Per-row rather than derived from
    # a global "days" setting, because the clock that matters is the one recorded at filing
    # time: changing a config value must not silently re-date data already collected under
    # the notice the filer was given. The ROW survives — §9.4's action for this class is
    # pseudonymise, not delete — so the category counts behind the quarterly statistics
    # (Consumer Protection Regs 2010 reg 7(13)) remain answerable.
    retention_until: Mapped[datetime] = mapped_column(DateTime, index=True)
    classification_ai_assisted: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))

    # --- additive beyond §7.8.1's DDL, both for the same reason: retention has to be
    # provable and idempotent. ``pseudonymised_at`` is what stops a second pass re-reducing
    # an already-reduced description (and what lets the subject-access export say honestly
    # "the text was reduced on this date" instead of implying it never existed).
    pseudonymised_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class SubjectPersonRow(Base):
    """The pseudonym → person mapping for complaint subjects (§7.8.1 ``subject_persons``).

    **The restricted table.** §9.3 gives it to ``legal`` and ``admin`` and to nobody else;
    §7.11.8 makes the same ``ref`` the only person-shaped value Phase 6's memory tier may
    ever hold. Everything that needs to talk about a person — a complaint, a memory fact —
    carries the ``ref``; only this one row says who that is. That is what makes a complaint
    export, a memory bundle or a leaked query result unable to name anybody.

    There is no route in this lane that lists this table. ``GET /complaints/subject-access/{ref}``
    reads exactly one row, by its ref, for ``legal``/``admin``, and records an audit row for
    doing so (DPA 2019 s.26 is a right exercised about one person at a time).

    Retention: §9.4 puts ``subject_persons`` in the personal staff/vendor class at 400 days.
    That is shorter than the 24-month life of a complaint's category counts, and deliberately
    so — once the mapping is gone the surviving counts are genuinely anonymous data.
    """

    __tablename__ = "subject_persons"
    __table_args__ = (Index("ix_subject_persons_operator", "operator_id", "display_name"),)

    # The pseudonym itself is the primary key (§7.8.1). A uuid4, never anything derived from
    # the name: a hash of a name is a name to anyone holding a staff list.
    ref: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    operator_id: Mapped[str] = mapped_column(String(32), index=True)
    display_name: Mapped[str] = mapped_column(Text)
    employer_vendor_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
