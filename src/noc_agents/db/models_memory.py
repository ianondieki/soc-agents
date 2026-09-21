"""Agent-memory tables (spec §7.11.3, Phase 4 Lane 4C, step M1; ``MEMORY_ENABLED=false``).

**One table: ``memory_episodes``.** L1 of the five-layer model (§7.11.2) — one row per
RESTORED/CLOSED incident, derived by arithmetic from ``incidents`` + ``work_notes`` by
``memory/consolidate.py``. The other four tables §7.11.3 specifies (``memory_facts``,
``memory_playbooks`` + steps, ``memory_shift_memo``, ``memory_embeddings``) belong to M2/M3/M4
and are deliberately **not** declared here: M2 is gated on the ``resolution_code`` census
(§8.1) and M3's bi-temporal logic is the fiddliest code in the lane. A table declared before
its writer exists is a table the migration creates on every operator's file for nothing.

Why a derived index at all, when M0 already answers "what happened at this site" straight off
``incidents``: an episode row is the **population** every later step aggregates over — the
fault-class median and the 3×IQR outlier rule of §7.11.7 need a set of comparable durations,
not one row at a time — and it is where the derivation (EAT hour, shift, scrubbed summary,
role token) is done once instead of on every read. It is a **cache, never a source of truth**:
``scripts/backfill_memory.py`` rebuilds it from scratch and ``source_version`` exists to force
that rebuild. Nothing in the pipeline reads it (MEM1/G15).

House conventions this follows exactly (§7.11.3):

* ``String(36)`` PK with ``default=new_id``; naive-UTC ``utcnow()`` like every other table
  (MEM8 — the EAT conversion is done once, into ``hour_of_day``/``month_of_year``/
  ``shift_type``, because "is this a rain-season pattern?" is an EAT question and
  ``Africa/Nairobi`` is UTC+3 with no DST);
* ``operator_id`` on the row (G14/MEM10). ``config/default.yaml`` points both operator
  profiles at one SQLite file, so isolation is a property of each query: every read goes
  through ``api.deps._owned``, which filters this column directly — no registration in
  ``_OWNED_VIA_INCIDENT`` is needed or wanted;
* **no FK to ``incidents``** — the ``AgentRunRow``/``IncidentBriefRow`` precedent
  (``models.py:217, 291``). A FK would make this cache's rows block a purge of the incident
  rows it derives from, which is backwards: §9.4 retention is decided on ``incidents``, and a
  stale episode is pruned by ``expire_memory()``, not by a constraint;
* **no column on any existing table** (MEM7/G10). ``_migrate_sqlite``'s successor
  (``db/migrate.py``) adds a new *table* to an old file safely; a new *column* on a table
  other than ``incidents`` was brief defect #23, and memory deliberately needs none.

Privacy (§7.11.8 rule 1): no person's name reaches this table. ``assignee_token`` holds a
**role token** from the §6.1 ``assignee_role_token`` vocabulary ("MSP-EGYPRO-POWER",
"FE-NBI-E-ONCALL", "NOC-QUEUE"), ``responsible_msp`` is a company, and ``resolution_summary``
is scrubbed with ``llm/redaction.py`` before the row is written — never after. The
token↔name mapping never lives here. ``tests/unit/test_memory_privacy.py`` scans every text
column of this table (and of ``memory_note_fts``) for a name substring.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from noc_agents.db.models import Base, new_id, utcnow

__all__ = ["MemoryEpisodeRow"]


class MemoryEpisodeRow(Base):
    """L1 (§7.11.3). One row per RESTORED/CLOSED incident. Rebuildable index, never truth.

    ``incident_id`` is UNIQUE, and that constraint *is* the idempotency guarantee
    §7.11.11 test 11 asks for: a retried consolidation tick, a re-run backfill and a
    double-delivered job all converge on one row. ``consolidate_incident`` updates in place
    when the row exists rather than relying on the constraint to raise, so a retry is silent
    rather than a caught ``IntegrityError``.

    ``CANCELLED`` incidents are not episodes: a cancelled ticket is a NOC bookkeeping action,
    not something that happened at the site, and counting it would inflate every "Nth outage
    here" figure computed from this population (the same rule ``services/memory.py`` applies
    to M0's live-table recall, shared through ``episode_statuses()``).

    The three duration columns are all nullable **on purpose**. §7.11.7: a duration this
    system cannot stand behind must be absent, not approximate — ``restore_minutes`` is NULL
    unless ``restored_source ∈ {MARK_RESTORED, SUPERVISOR}`` (§7.0.8's M4 rule, the guard
    against brief defect #4's "RESTORED" substring match), NULL when the clock runs backwards,
    and NULL when the value is an outlier beyond 3×IQR for its fault class. The episode still
    exists — it counts toward "how often has this happened here" — it simply never feeds a
    median.
    """

    __tablename__ = "memory_episodes"
    __table_args__ = (
        # The one aggregate read this table serves on the hot path: "the most recent trusted
        # durations of this fault class" (``services/memory.restore_minutes_population`` and
        # the evidence ids beside it), ``ORDER BY closed_at DESC LIMIT 500``. Leading with
        # ``operator_id`` puts the operator clause (MEM10) inside the index, and the trailing
        # ``closed_at`` serves the ORDER BY, so the read is bounded by the fault class's recent
        # sample and never touches another fault class or sorts.
        #
        # There is deliberately NO (operator_id, site_id, fault_class) index any more (review
        # M10): the first version declared one "for the exact tier", but the exact tier reads
        # ``incidents`` through ``ix_incidents_site_id`` (so a just-closed ticket is recalled
        # before any job has run) and nothing read this table by site — it was write cost
        # with no reader. A file that already has it keeps a harmless unused index.
        Index("ix_memory_episodes_op_fault_closed", "operator_id", "fault_class", "closed_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    operator_id: Mapped[str] = mapped_column(String(32), index=True)
    # No FK (see the module docstring); UNIQUE is what makes consolidation idempotent.
    incident_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    incident_number: Mapped[str] = mapped_column(String(64), index=True)
    site_id: Mapped[str] = mapped_column(String(64), index=True)
    site_type: Mapped[str] = mapped_column(String(32), default="BTS")
    region_code: Mapped[str] = mapped_column(String(8), default="", index=True)
    failure_domain: Mapped[str] = mapped_column(String(32), index=True, default="UNKNOWN")
    alarm_code: Mapped[str] = mapped_column(String(64), default="")
    #: ``f"{failure_domain}|{alarm_token}|{site_type}"`` — built by ``services.memory.fault_class``
    #: from the same three columns M0's live-table recall uses, so the exact tier matches across
    #: the M0/M1 boundary. Splitting a compound alarm code into a primary token is an M2
    #: question (it needs the §8.1 ``resolution_code`` census); doing it here would be a guess.
    fault_class: Mapped[str] = mapped_column(String(96), index=True)
    priority: Mapped[str] = mapped_column(String(8), default="P4")
    users_affected: Mapped[int] = mapped_column(Integer, default=0)
    mpesa_risk: Mapped[bool] = mapped_column(Boolean, default=False)
    #: A company, not a person (§7.11.8) — scrubbed for contact details like every other
    #: company field in ``redaction.COMPANY_FIELDS``, never tokenised as a name.
    responsible_msp: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    #: §6.1 role-token vocabulary via ``services.alerts.assignee_role_token``. NEVER a name.
    assignee_token: Mapped[str | None] = mapped_column(String(32), nullable=True)

    outage_start_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    first_vendor_note_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    restored_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    #: Copied verbatim from ``incidents.restored_source`` (§7.0.8) so a later reader can see
    #: *why* ``restore_minutes`` is NULL without joining back to the ticket.
    restored_source: Mapped[str | None] = mapped_column(String(32), nullable=True)

    ack_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vendor_response_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    restore_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sla_ack_met: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    sla_restore_met: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    resolution_code: Mapped[str] = mapped_column(String(64), default="")
    #: SCRUBBED before storage (``scrub_text`` applies ``scrub_contacts`` first), capped at
    #: ``consolidate.EPISODE_SUMMARY_MAX_CHARS`` (500). Recall re-caps it to 240 (§7.11.3's
    #: evidence cap) on the way out.
    resolution_summary: Mapped[str] = mapped_column(Text, default="")
    #: Which work note the summary came from when ``incidents.resolution_summary`` was empty —
    #: the same note ``lifecycle.note_declares_restored`` called the restore, so memory and the
    #: lifecycle can never disagree about which note ended the outage.
    restoring_note_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    child_sites_down: Mapped[int] = mapped_column(Integer, default=0)
    parent_incident_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)

    #: EAT-derived (MEM8), from when the fault STARTED rather than when the paperwork closed:
    #: "does this site fail in the MAM rains / on the 19:00 load peak?" is a question about the
    #: outage, and a ticket closed three days later would answer it with the wrong hour.
    hour_of_day: Mapped[int] = mapped_column(Integer, default=0)
    month_of_year: Mapped[int] = mapped_column(Integer, default=1)
    shift_type: Mapped[str] = mapped_column(String(16), default="DAY")

    #: When the episode ended: ``COALESCE(closed_at, restored_at, created_at)``, the same
    #: expression M0 orders history by, so an episode cannot be inside a window by one clock
    #: and outside it by another. Indexed: it is the prune key for the 24-month horizon.
    closed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    built_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    #: Bump in code to invalidate every row: the backfill re-derives anything built under an
    #: older version. The one lever for "the derivation changed" that does not need a migration.
    source_version: Mapped[int] = mapped_column(Integer, default=1)
