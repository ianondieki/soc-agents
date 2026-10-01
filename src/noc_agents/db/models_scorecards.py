"""Vendor scorecards and their KPI lines (spec §7.6.1) -- Phase 4 Lane 4A, step 2.

Two tables, both created by the generic additive migration (``db/migrate.py`` walks
``Base.metadata``; ``db/models_all.py`` imports this module before it runs), so there is no
hand-written DDL here.

``vendor_scorecards``
    One card per ``(operator_id, vendor_id, period)``. A card is a COMMERCIAL DOCUMENT: its
    numbers are what Supply Chain puts in front of a vendor, so it records everything needed
    to reproduce them -- ``sla_terms_version`` (which terms), ``computed_by_run_id`` (which
    run), ``period_start``/``period_end`` (which instants the month meant) -- and everything
    needed to distrust them: ``data_quality_json`` (how many restore times were guesses) and
    ``discipline_json`` (how well the OPERATOR kept its own records).

``vendor_scorecard_lines``
    One row per KPI x priority. Every line carries a human-readable ``formula`` and a
    ``yaml_path`` that resolves to the term it was measured against: a number nobody can
    trace to a contract term is not evidence. The dispute columns (``dispute_task_id`` ...
    ``adjudication_reason``) are in the §7.6.1 DDL and are declared here so the dispute lane
    adds behaviour, not schema; nothing in this step writes them.

WHY THIS MODULE BREAKS THE "NO CHECK CONSTRAINT" HABIT OF THE OTHER MODEL FILES
===============================================================================
Everywhere else in this schema a status column is a plain string and the service layer
enforces the vocabulary (see ``models_vendors.py``, ``models_pir.py``). That is the right
trade for a workflow label. It is the wrong one for the two gates below, because each is
the only thing standing between a number and a vendor's invoice, and "the service checks
it" is a promise about every FUTURE caller -- a job written next year, a route that forgets
the helper, a one-line fix that assigns ``card.status``. So the gates live in three layers,
and it matters to be exact about what each one stops:

1. **The service** (``services/scorecard.py``) is the sanctioned path and refuses with a
   readable error: WITHHELD cannot be published, a first period needs a named reviewer, a
   released card cannot be recomputed.
2. **The mapper guards below** (``_refuse_released_insert``, ``_freeze_released_card``,
   ``_freeze_published_line``) travel with the class, the way ``db.models._own_hitl_task``
   does: any Session, any caller that assigns ``card.status = "PUBLISHED"`` passes through
   them. They refuse a row INSERTED as PUBLISHED/FINAL (a released card must have been
   computed first), refuse any change to the evidence columns -- ``shadow_required``, the
   ``dq_*`` operands, the review, the terms version, the JSON -- once a card is or is
   becoming released, and refuse a released card leaving that state except PUBLISHED->FINAL.
   Before release, a card is written by exactly two hands: the COMPUTATION and a REVIEWER.
   Evidence (and the status) may change only inside ``services.scorecard.compute_scorecard``,
   which opens a session-scoped computation scope (``COMPUTATION_SCOPE_KEY`` in
   ``Session.info``, holding the ids being computed) and names a run that really exists in
   ``agent_runs`` as a scorecard run of the same operator; a "dressed-up recompute" that sets
   ``computed_by_run_id = 'anything'`` has neither and is refused. The reviewer columns take
   only a VISIBLE human name (``visible_human_name``: control and format characters removed,
   whitespace stripped, at least one letter, not an automation name). This is what closes the
   APPLICATION-BUG paths, which are the ones that realistically happen: the same-write flip
   (``card.shadow_required = 0; card.status = "PUBLISHED"``), the threshold nudged on a
   WITHHELD card, and the fake earlier card that would make the next period's shadow check
   find a "released" predecessor.
3. **The CHECK constraints** are the last line for a writer that never touches the mapper
   (Core ``update()``/``insert()``, raw SQL). ``ck_vendor_scorecards_gate`` re-does the
   §7.6.2 arithmetic from the recorded counts -- ``inferred * 100 <= threshold_pct *
   restored`` -- so a ``passed: true`` in the JSON changes nothing; the threshold is bounded
   to ``[0, 100)``. ``ck_vendor_scorecards_shadow`` requires a reviewer that is not blank
   (space, tab, LF, CR and NBSP all count as blank) and not one of the automation names the
   service also refuses. ``ck_vendor_scorecards_status`` keeps both to the exact literals.

WHAT THIS DOES NOT PROMISE. Mapper guards run for ORM unit-of-work writes only. A raw SQL
statement, a Core ``update(VendorScorecardRow)`` / ``insert(...)``, a legacy
``session.query(...).update(...)`` and ``bulk_*_mappings`` all bypass them, and a
``UPDATE vendor_scorecards SET status = 'PUBLISHED', shadow_required = 0`` written that way
produces a row the CHECKs -- which see one row and no history -- cannot tell from a
computation that found those values. A CHECK cannot compare a column with what it used to
be, and a trigger is ruled out here on purpose: ``db/migrate.py`` builds new tables from
compiled ``CreateTable`` strings, so a trigger attached as a DDL event would exist on fresh
databases and not on migrated ones. Write access of that kind is outside what a schema can
promise. What remains for it: the audit trail (a card the service released always has a
``scorecard.published`` row in ``audit_events``, written in the same transaction), and the
shadow rule LEANS ON IT -- ``earlier_released_card_exists`` counts a predecessor only when
that row exists, so a card planted PUBLISHED by raw SQL or a bulk update does not exempt
the next period from its shadow review. ``tests/unit/test_scorecard_gates.py`` exercises
every path above and names the raw-SQL cases it can only document.

COLUMNS BEYOND THE §7.6.1 DDL (additive; each exists for a stated reason)
=========================================================================
``period_start`` / ``period_end``   the naive-UTC instants the period string meant when the
                                    card was computed (the month is an EAT calendar month).
``shadow_required``                 what the SHADOW CHECK keys on; the DDL has only the
                                    reviewer columns, and a CHECK cannot run the subquery
                                    ("has any earlier card been published?") that decides it.
``dq_restored_incidents`` /         the gate's operands as real columns, because a portable
``dq_inferred_restores`` /          CHECK cannot look inside ``data_quality_json``. They are
``dq_gate_threshold_pct``           copies of the JSON's numbers and a test pins that they agree.
``terms_json``                      which terms the card stood on, and above all whether they
                                    were a CONTRACT or DEFAULTS ("defaults, not contract",
                                    §7.6.6) -- on the card, not in a log.
``lines.seq``                       a stable reading order (the position in the fixed line list).
``lines.evidence_json``             the incidents behind the line: which were measured, which
                                    were excluded and why. ``excluded_incidents`` is a count;
                                    §7.6.2 says inferred restores are "LISTED as excluded", and
                                    the vendor pack (§7.6.3) needs the list as it stood when the
                                    number was computed, not as the incidents look later.

No operator_id on ``vendor_scorecard_lines``: a line is owned through its card, the same
way an action item hangs off its review. It is therefore NOT readable through
``api.deps._owned`` on its own -- reach it through an owned card (see
``api/routers/scorecards.py``).
"""

from __future__ import annotations

import json
import unicodedata
from datetime import datetime
from typing import Any

from sqlalchemy import REAL, CheckConstraint, DateTime, ForeignKey, Index, Integer, Text, UniqueConstraint, event, inspect, select, text
from sqlalchemy.orm import Mapped, mapped_column, object_session

from noc_agents.db.models import AgentRunRow, AuditRow, Base, new_id, utcnow

# ------------------------------------------------------------------------- vocabularies

STATUS_DRAFT = "DRAFT"  # computed, gate passed, not a first period: waiting for a human to publish
STATUS_SHADOW = "SHADOW"  # computed, gate passed, FIRST period (or first under new terms): internal eyes only
STATUS_WITHHELD = "WITHHELD"  # computed, gate FAILED: too many restore times are guesses (§7.6.2)
STATUS_PUBLISHED = "PUBLISHED"  # a named human released it; the dispute window is running
STATUS_FINAL = "FINAL"  # the window closed; a recompute is a 409 from here on (§7.6.6)

#: ``vendor_scorecards.status`` (§7.6.1), in lifecycle order.
SCORECARD_STATUSES: tuple[str, ...] = (STATUS_DRAFT, STATUS_SHADOW, STATUS_WITHHELD, STATUS_PUBLISHED, STATUS_FINAL)
#: The statuses a vendor may be shown. Everything else is the operator's working paper.
RELEASED_STATUSES: tuple[str, ...] = (STATUS_PUBLISHED, STATUS_FINAL)
#: The statuses a recompute may replace. A released card is a document someone outside the
#: building has been shown; it is corrected through a dispute, never silently rewritten.
RECOMPUTABLE_STATUSES: tuple[str, ...] = (STATUS_DRAFT, STATUS_SHADOW, STATUS_WITHHELD)

KPI_MTTA = "MTTA_MIN"
KPI_ADJ_MTTR = "ADJ_MTTR_MIN"
KPI_SLA_COMPLIANCE = "SLA_COMPLIANCE_PCT"
KPI_REPEAT_FAULT = "REPEAT_FAULT_RATE"
KPI_NOTE_COMPLIANCE = "NOTE_COMPLIANCE_PCT"
KPI_AVAILABILITY = "AVAILABILITY_PCT"

#: ``vendor_scorecard_lines.kpi`` (§7.6.1), in the spec's order -- also the order lines are
#: written and served in, so two computations of one period are comparable row for row.
KPIS: tuple[str, ...] = (KPI_MTTA, KPI_ADJ_MTTR, KPI_SLA_COMPLIANCE, KPI_REPEAT_FAULT, KPI_NOTE_COMPLIANCE, KPI_AVAILABILITY)

BAND_GREEN, BAND_AMBER, BAND_RED, BAND_NA = "GREEN", "AMBER", "RED", "NA"
BANDS: tuple[str, ...] = (BAND_GREEN, BAND_AMBER, BAND_RED, BAND_NA)

CREDIT_NONE, CREDIT_PROPOSED, CREDIT_ACCEPTED, CREDIT_WITHDRAWN = "NONE", "PROPOSED", "ACCEPTED", "WITHDRAWN"
#: ``credit_status``. The computation only ever writes NONE or PROPOSED (§7.6.2: "always
#: PROPOSED until Supply Chain/Legal accepts"); ACCEPTED/WITHDRAWN belong to a human.
CREDIT_STATUSES: tuple[str, ...] = (CREDIT_NONE, CREDIT_PROPOSED, CREDIT_ACCEPTED, CREDIT_WITHDRAWN)

#: ``dispute_status`` (§7.6.1). Declared for the dispute lane; nothing here writes it.
DISPUTE_STATUSES: tuple[str, ...] = ("OPEN", "UPHELD", "ADJUSTED", "WITHDRAWN")


#: Names that are not a named human. ``shadow_reviewed_by`` exists to close the "first
#: scorecard nobody inspected" gap (§7.6.2); a job signing its own work re-opens it. Shared
#: with ``services.scorecard`` so the CHECK below and the service refuse the same names.
NOT_A_HUMAN_NAMES: tuple[str, ...] = ("slascorecardagent", "system", "scheduler", "agent", "automation", "noc", "none", "null")

#: Every character SQLite's one-argument ``trim()`` would NOT strip but a human reader would
#: see as blank: tab, LF, CR and NBSP. (``char()`` is SQLite's code-point function; the
#: migration is SQLite-only, and ``init_db`` says so for any other engine.) The CHECK cannot
#: know Unicode categories, so U+200B and its kin are stopped by ``visible_human_name`` in
#: the service and the mapper, not by the table.
_BLANKS_SQL = "' ' || char(9) || char(10) || char(13) || char(160)"

#: ``agent_runs.graph_name`` of a scorecard computation. Owned here (not in the service) so
#: the mapper guard can check a card's ``computed_by_run_id`` against a real run without
#: importing the service layer; ``services.scorecard.GRAPH_NAME`` is this value.
SCORECARD_GRAPH_NAME = "scorecard"

#: ``Session.info`` key under which ``services.scorecard.compute_scorecard`` records the card
#: ids it is writing. The mapper guards accept a change to a card's evidence only while its id
#: is in this set: evidence is written by the computation and by nothing else.
COMPUTATION_SCOPE_KEY = "scorecard_computation_scope"

#: Unicode general categories that never belong in a person's name and that most renderers
#: draw as nothing: control (Cc), format (Cf -- U+200B ZERO WIDTH SPACE, U+200D, U+2060,
#: U+FEFF ...), surrogates (Cs), private use (Co), unassigned (Cn). Removed outright, so a
#: name that is only made of them becomes empty and fails the letter test.
_INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})


def visible_human_name(value: str | None) -> str | None:
    """The cleaned name if ``value`` is a VISIBLE human name, else ``None``.

    One predicate for the service (``_named_human``), the mapper guards (``_reviewer_named``,
    the write-time check on ``shadow_reviewed_by``) and ``publish_scorecard``, so they cannot
    disagree about what counts. The rules: control/format/surrogate/private/unassigned
    characters are removed anywhere in the string; every Unicode whitespace (categories Zs,
    Zl, Zp and the ASCII controls ``str.strip`` knows) is stripped from both ends; what is
    left must contain at least one letter and must not be one of ``NOT_A_HUMAN_NAMES``.
    ``"\u200b"`` (zero-width space), ``"\u3000"`` (ideographic space), ``"\x0b"``,
    ``"12345"`` and ``"system"`` are all ``None``; ``"Grace Mwangi"``, ``"N'gang'a"`` and
    ``"\u674e\u96f7"`` come back as themselves.
    """
    if value is None:
        return None
    without_invisibles = "".join(ch for ch in str(value) if unicodedata.category(ch) not in _INVISIBLE_CATEGORIES)
    cleaned = without_invisibles.strip()
    if not any(ch.isalpha() for ch in cleaned):
        return None
    if cleaned.casefold() in NOT_A_HUMAN_NAMES:
        return None
    return cleaned


def _sql_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


#: The §7.6.2 gate, as SQL over the row's own columns. ``inferred_pct > max`` WITHHOLDS, so
#: exactly-at-threshold passes; cross-multiplied so there is no division and no 0/0 (a period
#: with no restores has nothing inferred and passes vacuously). The service evaluates the
#: SAME expression in Python (``services.scorecard.gate_passes``) so the two cannot disagree.
_GATE_PASSES_SQL = "dq_inferred_restores * 100.0 <= dq_gate_threshold_pct * dq_restored_incidents"


class VendorScorecardRow(Base):
    """One vendor's card for one period (§7.6.1 ``vendor_scorecards``)."""

    __tablename__ = "vendor_scorecards"
    __table_args__ = (
        UniqueConstraint("operator_id", "vendor_id", "period", name="uq_vendor_scorecards_operator_vendor_period"),
        CheckConstraint(f"status IN ({_sql_list(SCORECARD_STATUSES)})", name="ck_vendor_scorecards_status"),
        CheckConstraint(f"status = '{STATUS_WITHHELD}' OR ({_GATE_PASSES_SQL})", name="ck_vendor_scorecards_gate"),
        CheckConstraint(
            f"status NOT IN ({_sql_list(RELEASED_STATUSES)}) OR shadow_required = 0 "
            f"OR (shadow_reviewed_by IS NOT NULL AND length(trim(shadow_reviewed_by, {_BLANKS_SQL})) > 0 "
            f"AND lower(trim(shadow_reviewed_by, {_BLANKS_SQL})) NOT IN ({_sql_list(NOT_A_HUMAN_NAMES)}))",
            name="ck_vendor_scorecards_shadow",
        ),
        CheckConstraint("shadow_required IN (0, 1)", name="ck_vendor_scorecards_shadow_flag"),
        CheckConstraint(
            "dq_restored_incidents >= 0 AND dq_inferred_restores >= 0 AND dq_inferred_restores <= dq_restored_incidents "
            "AND dq_gate_threshold_pct >= 0 AND dq_gate_threshold_pct < 100",
            name="ck_vendor_scorecards_dq_counts",
        ),
        Index("ix_vendor_scorecards_operator_period", "operator_id", "period"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=new_id)
    operator_id: Mapped[str] = mapped_column(Text, index=True)
    vendor_id: Mapped[str] = mapped_column(Text, index=True)  # vendors.id -- one ROW of terms, not one code
    period: Mapped[str] = mapped_column(Text)  # "2026-09": a calendar month in the operator's timezone
    period_start: Mapped[datetime] = mapped_column(DateTime)  # naive UTC, inclusive
    period_end: Mapped[datetime] = mapped_column(DateTime)  # naive UTC, exclusive
    status: Mapped[str] = mapped_column(Text, default=STATUS_DRAFT, server_default=STATUS_DRAFT)
    computed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    dispute_window_ends_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finalised_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # {incidents, restored_incidents, inferred_restores, inferred_pct, gate_threshold_pct, passed, reason, ...}
    data_quality_json: Mapped[str] = mapped_column(Text)
    dq_restored_incidents: Mapped[int] = mapped_column(Integer)
    dq_inferred_restores: Mapped[int] = mapped_column(Integer)
    dq_gate_threshold_pct: Mapped[float] = mapped_column(REAL)
    # {late_scc_openings, missing_scc_with_confirmed_power, ...} -- about the OPERATOR, never the vendor
    discipline_json: Mapped[str] = mapped_column(Text)
    shadow_required: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))  # fail closed
    shadow_reviewed_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    shadow_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sla_terms_version: Mapped[str] = mapped_column(Text)
    terms_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    narrative: Mapped[str | None] = mapped_column(Text, nullable=True)  # QBR narrative; not written by the computation
    narrative_ai_assisted: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    computed_by_run_id: Mapped[str] = mapped_column(Text)  # agent_runs.id

    @property
    def data_quality(self) -> dict[str, Any]:
        return json.loads(self.data_quality_json or "{}")

    @data_quality.setter
    def data_quality(self, value: dict[str, Any]) -> None:
        self.data_quality_json = json.dumps(value, sort_keys=True)

    @property
    def discipline(self) -> dict[str, Any]:
        return json.loads(self.discipline_json or "{}")

    @discipline.setter
    def discipline(self, value: dict[str, Any]) -> None:
        self.discipline_json = json.dumps(value, sort_keys=True)

    @property
    def terms(self) -> dict[str, Any]:
        return json.loads(self.terms_json or "{}")

    @terms.setter
    def terms(self, value: dict[str, Any]) -> None:
        self.terms_json = json.dumps(value, sort_keys=True)

    @property
    def is_released(self) -> bool:
        """PUBLISHED or FINAL: a vendor may have been shown this card."""
        return self.status in RELEASED_STATUSES


class VendorScorecardLineRow(Base):
    """One KPI for one priority (or all of them) on one card (§7.6.1 ``vendor_scorecard_lines``)."""

    __tablename__ = "vendor_scorecard_lines"
    __table_args__ = (
        # One line per (card, kpi, priority). SQLite and PostgreSQL both treat NULLs as
        # distinct in a UNIQUE, so this does not by itself stop two all-priority lines; the
        # service writes lines from one fixed list (``services.scorecard.LINE_SHAPE``) and
        # replaces them wholesale on a recompute, which is what actually guarantees it.
        UniqueConstraint("scorecard_id", "kpi", "priority", name="uq_vendor_scorecard_lines_card_kpi_priority"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=new_id)
    scorecard_id: Mapped[str] = mapped_column(ForeignKey("vendor_scorecards.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))  # position in LINE_SHAPE
    kpi: Mapped[str] = mapped_column(Text)  # KPIS
    priority: Mapped[str | None] = mapped_column(Text, nullable=True)  # P1..P4, or NULL = all priorities
    raw_value: Mapped[float | None] = mapped_column(REAL, nullable=True)  # NULL = nothing measurable (band NA)
    normalised_value: Mapped[float | None] = mapped_column(REAL, nullable=True)  # BESIDE raw, never instead of it
    region_multiplier_applied: Mapped[float | None] = mapped_column(REAL, nullable=True)  # NULL when the line mixes regions
    unit: Mapped[str] = mapped_column(Text)  # "min" | "pct" | "ratio"
    band: Mapped[str] = mapped_column(Text)  # BANDS
    eligible_incidents: Mapped[int] = mapped_column(Integer)
    excluded_incidents: Mapped[int] = mapped_column(Integer)
    scc_minutes_deducted: Mapped[int] = mapped_column(Integer)
    formula: Mapped[str] = mapped_column(Text)  # human-readable, with this line's own operands
    yaml_path: Mapped[str] = mapped_column(Text)  # resolves: services.scorecard.resolve_term
    evidence_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    proposed_credit_pct: Mapped[float | None] = mapped_column(REAL, nullable=True)
    credit_status: Mapped[str] = mapped_column(Text, default=CREDIT_NONE, server_default=CREDIT_NONE)
    # --- the dispute lane's columns (§7.6.1). Declared now; written by nothing in this step. ---
    dispute_task_id: Mapped[str | None] = mapped_column(Text, nullable=True)  # hitl_tasks.id (DISPUTE_SCORECARD_LINE)
    dispute_status: Mapped[str | None] = mapped_column(Text, nullable=True)  # DISPUTE_STATUSES
    adjusted_value: Mapped[float | None] = mapped_column(REAL, nullable=True)
    adjudicated_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    adjudication_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    @property
    def evidence(self) -> dict[str, Any]:
        return json.loads(self.evidence_json or "{}")

    @evidence.setter
    def evidence(self, value: dict[str, Any]) -> None:
        self.evidence_json = json.dumps(value, sort_keys=True)


# ------------------------------------------------------------------ the mapper-level guards


class ScorecardEvidenceError(ValueError):
    """A write that would alter a released card's evidence, or release a card that was never
    computed. Raised from inside the flush, so the writer's transaction fails instead of
    committing a number a vendor was shown and can no longer reproduce."""


#: Card columns the COMPUTATION writes. They may change only together with
#: ``computed_by_run_id`` -- that is, only when a new computation writes them as a set -- and
#: never once the card is, or is becoming, PUBLISHED/FINAL. A WITHHELD card whose threshold is
#: nudged to 99 on its way to DRAFT is exactly the write this stops: the CHECK sees a
#: consistent row, the mapper sees evidence changing with no computation behind it.
COMPUTED_EVIDENCE_COLUMNS: tuple[str, ...] = (
    "operator_id",
    "vendor_id",
    "period",
    "period_start",
    "period_end",
    "computed_at",
    "data_quality_json",
    "dq_restored_incidents",
    "dq_inferred_restores",
    "dq_gate_threshold_pct",
    "discipline_json",
    "shadow_required",
    "sla_terms_version",
    "terms_json",
)
#: What a named human recorded before release. Writable while the card is unreleased (that is
#: what a shadow review is), frozen from the moment of release.
REVIEW_COLUMNS: tuple[str, ...] = ("shadow_reviewed_by", "shadow_reviewed_at")
#: Everything frozen at and after release: the computed evidence, the review, the run.
CARD_EVIDENCE_COLUMNS: tuple[str, ...] = (*COMPUTED_EVIDENCE_COLUMNS, *REVIEW_COLUMNS, "computed_by_run_id")
#: Written by the RELEASE transitions and by nothing else: ``publish_scorecard`` sets
#: ``status`` / ``published_at`` / ``dispute_window_ends_at`` in one write, ``finalise_scorecard``
#: sets ``status`` / ``finalised_at`` in one write. Outside those two writes they are frozen.
CARD_RELEASE_COLUMNS: tuple[str, ...] = ("status", "published_at", "dispute_window_ends_at", "finalised_at")
#: The QBR narrative, written by a human after the numbers and allowed at any status.
CARD_NARRATIVE_COLUMNS: tuple[str, ...] = ("narrative", "narrative_ai_assisted")
#: What a PUBLISH may change, what a FINALISE may change, what a released card may change at rest.
_PUBLISH_WRITE: frozenset[str] = frozenset({"status", "published_at", "dispute_window_ends_at", *CARD_NARRATIVE_COLUMNS})
_FINALISE_WRITE: frozenset[str] = frozenset({"status", "finalised_at", *CARD_NARRATIVE_COLUMNS})
_RELEASED_AT_REST: frozenset[str] = frozenset(CARD_NARRATIVE_COLUMNS)

#: The DISPUTE SEAM: the only line columns a writer other than the computation may touch, on
#: an unreleased card and on a released one alike. They are what a human decides ABOUT a
#: published figure -- the C-02 dispute path (``DISPUTE_SCORECARD_LINE``) writes them, and a
#: later feature that needs another column adds it HERE, explicitly, rather than finding a
#: hole. ``band`` is here because an ADJUSTED dispute re-bands the line; ``credit_status``
#: because Supply Chain/Legal accept or withdraw a proposal (§7.6.2).
LINE_DISPUTE_COLUMNS: tuple[str, ...] = (
    "band",
    "credit_status",
    "dispute_task_id",
    "dispute_status",
    "adjusted_value",
    "adjudicated_by",
    "adjudication_reason",
)

#: Line columns that are EVIDENCE: the number as computed and, once released, as published.
#: Written by ``compute_scorecard`` only -- inserted, updated and deleted inside the parent
#: card's computation scope -- and frozen for good once the card is released.
LINE_EVIDENCE_COLUMNS: tuple[str, ...] = (
    "scorecard_id",
    "seq",
    "kpi",
    "priority",
    "raw_value",
    "normalised_value",
    "region_multiplier_applied",
    "unit",
    "eligible_incidents",
    "excluded_incidents",
    "scc_minutes_deducted",
    "formula",
    "yaml_path",
    "evidence_json",
    "proposed_credit_pct",
)


def earlier_released_card_exists(executor, *, operator_id: str, vendor_id: str, period: str, sla_terms_version: str) -> bool:
    """§7.6.2's shadow rule as ONE query, shared by the service and the mapper guards below.

    True when an EARLIER period's card for the same vendor row, under the same
    ``sla_terms.version``, is PUBLISHED or FINAL **and was released by the service**: the
    ``scorecard.published`` row ``publish_scorecard`` writes to ``audit_events`` in the same
    transaction must exist for it. A card planted PUBLISHED by raw SQL or a bulk ``update()``
    has no such row and therefore exempts nobody from a shadow review. (If an audit row were
    ever pruned, the error runs the safe way: a review is required again, never skipped.)
    ``executor`` is a Session or the flush's own Connection -- both ``execute()`` a select --
    so the guard sees a card released earlier in the same, still uncommitted, transaction.
    """
    released_by_service = (
        select(AuditRow.id)
        .where(
            AuditRow.entity_type == "vendor_scorecard",
            AuditRow.entity_id == VendorScorecardRow.id,
            AuditRow.action == "scorecard.published",
        )
        .exists()
    )
    row = executor.execute(
        select(VendorScorecardRow.id)
        .where(
            VendorScorecardRow.operator_id == operator_id,
            VendorScorecardRow.vendor_id == vendor_id,
            VendorScorecardRow.sla_terms_version == sla_terms_version,
            VendorScorecardRow.period < period,  # "YYYY-MM" sorts chronologically
            VendorScorecardRow.status.in_(RELEASED_STATUSES),
            released_by_service,
        )
        .limit(1)
    ).scalar()
    return row is not None


def _derived_shadow_required(connection, target: VendorScorecardRow) -> int:
    return 0 if earlier_released_card_exists(
        connection, operator_id=target.operator_id, vendor_id=target.vendor_id, period=target.period, sla_terms_version=target.sla_terms_version
    ) else 1


def _reviewer_named(target: VendorScorecardRow) -> bool:
    return visible_human_name(target.shadow_reviewed_by) is not None


def _in_computation_scope(target: VendorScorecardRow) -> bool:
    """True while ``services.scorecard.compute_scorecard`` is writing THIS card."""
    session = object_session(target)
    return session is not None and target.id in (session.info.get(COMPUTATION_SCOPE_KEY) or ())


def _run_is_a_scorecard_computation(connection, target: VendorScorecardRow) -> bool:
    """``computed_by_run_id`` names a real ``agent_runs`` row: a scorecard run of this
    operator. A run id that resolves to nothing is the signature of an edit posing as a
    computation."""
    if not target.computed_by_run_id:
        return False
    row = connection.execute(
        select(AgentRunRow.graph_name, AgentRunRow.operator_id).where(AgentRunRow.id == target.computed_by_run_id)
    ).first()
    return row is not None and row[0] == SCORECARD_GRAPH_NAME and row[1] == target.operator_id


def _require_computation(connection, target: VendorScorecardRow, what: str) -> None:
    if not _in_computation_scope(target):
        raise ScorecardEvidenceError(
            f"scorecard {target.id}: {what} outside a computation -- evidence is written by "
            "services.scorecard.compute_scorecard and by nothing else; recompute the card"
        )
    if not _run_is_a_scorecard_computation(connection, target):
        raise ScorecardEvidenceError(
            f"scorecard {target.id}: computed_by_run_id={target.computed_by_run_id!r} is not a scorecard run of "
            f"operator {target.operator_id} in agent_runs -- a computation names the run that did it"
        )


#: What may change on an UNRELEASED card outside a computation: the human review (a named
#: reviewer's act) and the QBR narrative. Everything else -- the evidence, the run and the
#: status -- is the computation's.
_FREE_BEFORE_RELEASE: frozenset[str] = frozenset({*REVIEW_COLUMNS, "narrative", "narrative_ai_assisted"})


def _changed(target: object, column: str) -> bool:
    return bool(inspect(target).attrs[column].history.has_changes())


def _previous(target: object, column: str):
    history = inspect(target).attrs[column].history
    return history.deleted[0] if history.deleted else getattr(target, column)


@event.listens_for(VendorScorecardRow, "before_insert")
def _refuse_released_insert(mapper, connection, target: VendorScorecardRow) -> None:
    """A card is never BORN released. ``compute_scorecard`` writes DRAFT / SHADOW / WITHHELD;
    PUBLISHED and FINAL are reached by ``publish_scorecard`` / ``finalise_scorecard`` on a row
    that already exists. An INSERT with a released status is therefore either a bug or a fake
    predecessor planted so the next period's ``shadow_required_for`` finds "an earlier
    released card" -- and is refused either way."""
    if target.status in RELEASED_STATUSES:
        raise ScorecardEvidenceError(
            f"scorecard {target.id} cannot be inserted as {target.status}: a card is computed first and released by a named human afterwards"
        )
    # ``shadow_required`` is DERIVED from the table, never taken on trust -- the same move as
    # ``_own_hitl_task`` deriving ``operator_id``. A DRAFT planted with shadow_required = 0 and
    # later flipped to PUBLISHED would otherwise become the "earlier released card" that lets
    # every following period skip its shadow review.
    derived = _derived_shadow_required(connection, target)
    if int(target.shadow_required if target.shadow_required is not None else 1) != derived:
        raise ScorecardEvidenceError(
            f"scorecard {target.id}: shadow_required={target.shadow_required} contradicts the table, which says {derived} "
            "(is there an earlier released card for this vendor under these terms?); the column is derived, not chosen"
        )
    # And a card is born of a computation: inside compute_scorecard's scope, citing a real run.
    _require_computation(connection, target, "inserted")
    if target.shadow_reviewed_by is not None and not _reviewer_named(target):
        raise ScorecardEvidenceError(f"scorecard {target.id}: shadow_reviewed_by must be a visible human name")


@event.listens_for(VendorScorecardRow, "before_update")
def _freeze_released_card(mapper, connection, target: VendorScorecardRow) -> None:
    """Once a card is (or in this very write becomes) PUBLISHED/FINAL, its evidence is frozen.

    The realistic bypass of a CHECK that sees one row is the SAME-WRITE flip: set the status
    and the operand it keys on together (``shadow_required = 0`` with ``status = 'PUBLISHED'``,
    or ``dq_gate_threshold_pct = 99`` on a WITHHELD card on its way to DRAFT). This guard has
    what the CHECK has not -- the row's history -- so that write is refused, and so is every
    later edit to the numbers a vendor was shown. The primary key never changes. Before
    release, evidence and status may change only inside ``compute_scorecard``'s computation
    scope, citing a run that exists (a recomputation); the review columns take a visible human
    name. At and after release the rule is DEFAULT DENY: a publish may write ``status`` +
    ``published_at`` + ``dispute_window_ends_at``, a finalise ``status`` + ``finalised_at``,
    and the narrative may be written at any time; nothing else, by name. Mapper-level, so it
    covers every ORM unit-of-work writer; not Core ``update()``, ``query.update()`` or raw SQL
    (see the module docstring for what that leaves).
    """
    if _changed(target, "id"):
        # The primary key is the card's identity for its lines (FK), its audit rows (the S02
        # predecessor evidence) and every dispute task that will point at it. Renaming it orphans
        # all three at once. Immutable through the ORM at every status, scope or no scope.
        raise ScorecardEvidenceError(f"scorecard {_previous(target, 'id')}: the primary key is immutable (attempted rename to {target.id!r})")
    old_status, new_status = _previous(target, "status"), target.status
    releasing = new_status in RELEASED_STATUSES
    if old_status in RELEASED_STATUSES and new_status != old_status and (old_status, new_status) != (STATUS_PUBLISHED, STATUS_FINAL):
        raise ScorecardEvidenceError(f"scorecard {target.id} is {old_status}: the only move from there is PUBLISHED -> FINAL, not {new_status!r}")
    if not (releasing or old_status in RELEASED_STATUSES):
        # Unreleased: two hands may write. A REVIEWER may set the review columns (to a visible
        # human name); the COMPUTATION may rewrite everything else -- but only from inside
        # compute_scorecard's scope, naming a run that exists. A "dressed-up recompute" that
        # sets computed_by_run_id = 'anything' from the ORM has neither and is refused; so is
        # a lone edit to a dq_* operand, to shadow_required or to the status.
        if _changed(target, "shadow_reviewed_by") and target.shadow_reviewed_by is not None and not _reviewer_named(target):
            raise ScorecardEvidenceError(f"scorecard {target.id}: shadow_reviewed_by must be a visible human name, not {target.shadow_reviewed_by!r}")
        computed = [c.key for c in mapper.column_attrs if c.key not in _FREE_BEFORE_RELEASE and _changed(target, c.key)]
        if computed:
            _require_computation(connection, target, f"{', '.join(computed)} changed")
            if not _changed(target, "computed_by_run_id"):
                raise ScorecardEvidenceError(
                    f"scorecard {target.id}: {', '.join(computed)} changed without a new computed_by_run_id -- "
                    "a computation names the run that did it; recompute the card"
                )
        return
    # Released, or becoming released: DEFAULT DENY. The only writes are the publish (status +
    # the two window stamps), the finalise (status + its stamp) and the narrative at rest;
    # every other changed column -- evidence, review, run, a stamp outside its transition -- is
    # refused by name.
    if releasing and old_status not in RELEASED_STATUSES:
        allowed = _PUBLISH_WRITE
    elif (old_status, new_status) == (STATUS_PUBLISHED, STATUS_FINAL):
        allowed = _FINALISE_WRITE
    else:
        allowed = _RELEASED_AT_REST
    refused = [c.key for c in mapper.column_attrs if c.key not in allowed and _changed(target, c.key)]
    if refused:
        raise ScorecardEvidenceError(
            f"scorecard {target.id}: {', '.join(refused)} cannot change on a card that is {new_status} -- "
            "a released card is frozen except for its release stamps and the narrative; recompute a DRAFT/SHADOW/WITHHELD card or open a dispute"
        )
    if releasing and old_status not in RELEASED_STATUSES:
        # The moment of release: the shadow rule is re-derived from the table, so a column that
        # was planted at 0 does not open the gate, and the reviewer must be a named human.
        if int(target.shadow_required or 0) == 0 and _derived_shadow_required(connection, target) == 1:
            raise ScorecardEvidenceError(
                f"scorecard {target.id}: shadow_required is 0 but no earlier released card exists for this vendor under "
                f"sla_terms {target.sla_terms_version}; the column contradicts the table -- recompute the card"
            )
        if int(target.shadow_required or 0) == 1 and not _reviewer_named(target):
            raise ScorecardEvidenceError(
                f"scorecard {target.id}: a first period cannot become {new_status} without a named shadow reviewer"
            )


# DEFAULT-DENY BY CONSTRUCTION. The guards below decide column by column from these lists, so a
# column that is in none of them would be writable by omission. That is refused at import: a new
# column has to be placed -- evidence, review, release, narrative, key; evidence or dispute --
# before the process starts. (``test_every_scorecard_column_is_classified`` states it again.)
_CARD_CLASSIFIED: frozenset[str] = frozenset({"id", *CARD_EVIDENCE_COLUMNS, *CARD_RELEASE_COLUMNS, *CARD_NARRATIVE_COLUMNS})
_LINE_CLASSIFIED: frozenset[str] = frozenset({"id", *LINE_EVIDENCE_COLUMNS, *LINE_DISPUTE_COLUMNS})
_unplaced_card = {c.name for c in VendorScorecardRow.__table__.columns} - _CARD_CLASSIFIED
_unplaced_line = {c.name for c in VendorScorecardLineRow.__table__.columns} - _LINE_CLASSIFIED
if _unplaced_card or _unplaced_line:  # pragma: no cover -- an import-time refusal, not a runtime branch
    raise RuntimeError(
        f"models_scorecards: columns not placed in any guard list -- vendor_scorecards {sorted(_unplaced_card)}, "
        f"vendor_scorecard_lines {sorted(_unplaced_line)}; classify them before this module can be imported"
    )
assert not (set(LINE_EVIDENCE_COLUMNS) & set(LINE_DISPUTE_COLUMNS)), "a line column cannot be both evidence and the dispute seam"


def _card_in_scope(session, card_id: str) -> bool:
    return session is not None and card_id in (session.info.get(COMPUTATION_SCOPE_KEY) or ())


def _parent_status(connection, card_id: str) -> str | None:
    """The parent card's status as the flush's own connection sees it; ``None`` when the card row
    is not there yet (a brand-new card whose lines are inserted in the same flush)."""
    return connection.execute(select(VendorScorecardRow.status).where(VendorScorecardRow.id == card_id)).scalar()


def _line_evidence_write(mapper, connection, target: VendorScorecardLineRow, what: str, changed: list[str]) -> None:
    """The one rule for a line's evidence: frozen on a released card; otherwise the
    computation's, i.e. only inside the PARENT CARD's computation scope."""
    card_id = _previous(target, "scorecard_id") or target.scorecard_id
    status = _parent_status(connection, card_id)
    if status in RELEASED_STATUSES:
        raise ScorecardEvidenceError(
            f"scorecard line {target.id}: {what} on a {status} card ({', '.join(changed)}) -- the published figure is frozen; "
            f"the dispute columns ({', '.join(LINE_DISPUTE_COLUMNS)}) are where a correction lives"
        )
    if not _card_in_scope(object_session(target), card_id):
        raise ScorecardEvidenceError(
            f"scorecard line {target.id}: {what} outside a computation ({', '.join(changed)}) -- a line's evidence is "
            "written by services.scorecard.compute_scorecard and by nothing else; recompute the card"
        )


@event.listens_for(VendorScorecardLineRow, "before_insert")
def _line_born_of_computation(mapper, connection, target: VendorScorecardLineRow) -> None:
    """A line is inserted by the computation of its card and by nothing else. An "extra line
    with a 25 % credit" added to a SHADOW card from the ORM is refused here."""
    _line_evidence_write(mapper, connection, target, "inserted", list(LINE_EVIDENCE_COLUMNS))


@event.listens_for(VendorScorecardLineRow, "before_update")
def _line_written_by_computation(mapper, connection, target: VendorScorecardLineRow) -> None:
    """Evidence columns change only inside the parent card's computation scope, and never once
    the card is released. The dispute seam (``LINE_DISPUTE_COLUMNS``) is free either way. The
    line's identity -- its ``id`` and the card it belongs to -- never changes at all: a renamed
    line is unreachable from its card and from the dispute task that cites it."""
    for key in ("id", "scorecard_id"):
        if _changed(target, key):
            raise ScorecardEvidenceError(f"scorecard line {_previous(target, 'id')}: {key} is immutable (attempted change to {getattr(target, key)!r})")
    changed = [c for c in LINE_EVIDENCE_COLUMNS if _changed(target, c)]
    if changed:
        _line_evidence_write(mapper, connection, target, "evidence changed", changed)


@event.listens_for(VendorScorecardLineRow, "before_delete")
def _line_deleted_by_computation(mapper, connection, target: VendorScorecardLineRow) -> None:
    """A line disappears only when the computation drops it (LINE_SHAPE shrank between
    releases) -- never from a released card, never from outside the scope."""
    _line_evidence_write(mapper, connection, target, "deleted", ["row"])


@event.listens_for(VendorScorecardRow, "before_delete")
def _refuse_card_delete(mapper, connection, target: VendorScorecardRow) -> None:
    """Nothing in this codebase deletes a card: a recompute updates in place, retention keeps
    scorecards (§9.4, >= 3 years). A released card is evidence a vendor was shown; an
    unreleased one may go only inside its own computation scope."""
    if target.status in RELEASED_STATUSES:
        raise ScorecardEvidenceError(f"scorecard {target.id} is {target.status}: a released card is never deleted")
    if not _card_in_scope(object_session(target), target.id):
        raise ScorecardEvidenceError(f"scorecard {target.id}: deleted outside a computation -- a card is not removed by hand")
