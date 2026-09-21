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
   This is what closes the APPLICATION-BUG paths, which are the ones that realistically
   happen: the same-write flip (``card.shadow_required = 0; card.status = "PUBLISHED"``) and
   the fake earlier card that would make the next period's shadow check find a "released"
   predecessor.
3. **The CHECK constraints** are the last line for a writer that never touches the mapper
   (Core ``update()``/``insert()``, raw SQL). ``ck_vendor_scorecards_gate`` re-does the
   §7.6.2 arithmetic from the recorded counts -- ``inferred * 100 <= threshold_pct *
   restored`` -- so a ``passed: true`` in the JSON changes nothing; the threshold is bounded
   to ``[0, 100)``. ``ck_vendor_scorecards_shadow`` requires a reviewer that is not blank
   (space, tab, LF, CR and NBSP all count as blank) and not one of the automation names the
   service also refuses. ``ck_vendor_scorecards_status`` keeps both to the exact literals.

WHAT THIS DOES NOT PROMISE. A person or a script with raw write access to the SQLite file
can issue ``UPDATE vendor_scorecards SET status = 'PUBLISHED', shadow_required = 0`` or
``dq_gate_threshold_pct = 99`` in one statement, and the CHECKs -- which see one row and no
history -- cannot tell that from a computation that found those values. A CHECK cannot
compare a column with what it used to be, and a trigger is ruled out here on purpose:
``db/migrate.py`` builds new tables from compiled ``CreateTable`` strings, so a trigger
attached as a DDL event would exist on fresh databases and not on migrated ones. Raw write
access to the database file is outside what a schema can promise; the audit trail
(``audit_events``, one row per transition) and the deterministic recompute are what remain
for that case. ``tests/unit/test_scorecard_gates.py`` exercises every path above and names
the raw-SQL cases it can only document.

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
from datetime import datetime
from typing import Any

from sqlalchemy import REAL, CheckConstraint, DateTime, ForeignKey, Index, Integer, Text, UniqueConstraint, event, inspect, select, text
from sqlalchemy.orm import Mapped, mapped_column

from noc_agents.db.models import Base, new_id, utcnow

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
#: migration is SQLite-only, and ``init_db`` says so for any other engine.)
_BLANKS_SQL = "' ' || char(9) || char(10) || char(13) || char(160)"


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
#: Everything frozen at and after release. Not in the list, on purpose: ``status`` (PUBLISHED
#: -> FINAL is a legal move, checked separately), ``published_at`` / ``dispute_window_ends_at``
#: (written BY the publish), ``finalised_at``, and ``narrative`` / ``narrative_ai_assisted``
#: (the QBR narrative is written after release).
CARD_EVIDENCE_COLUMNS: tuple[str, ...] = (*COMPUTED_EVIDENCE_COLUMNS, *REVIEW_COLUMNS, "computed_by_run_id")

#: Line columns that are EVIDENCE: the number as published. What humans later decide about
#: it -- ``band`` re-judged by a dispute, the credit's status, every ``dispute_*`` /
#: ``adjusted_*`` / ``adjudicat*`` column -- stays writable, because that is the dispute
#: lane's job; the published figure it argues about does not move.
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
    ``sla_terms.version``, is PUBLISHED or FINAL. ``executor`` is a Session or the flush's own
    Connection -- both ``execute()`` a select -- so the guard sees a card released earlier in
    the same, still uncommitted, transaction.
    """
    row = executor.execute(
        select(VendorScorecardRow.id)
        .where(
            VendorScorecardRow.operator_id == operator_id,
            VendorScorecardRow.vendor_id == vendor_id,
            VendorScorecardRow.sla_terms_version == sla_terms_version,
            VendorScorecardRow.period < period,  # "YYYY-MM" sorts chronologically
            VendorScorecardRow.status.in_(RELEASED_STATUSES),
        )
        .limit(1)
    ).scalar()
    return row is not None


def _derived_shadow_required(connection, target: VendorScorecardRow) -> int:
    return 0 if earlier_released_card_exists(
        connection, operator_id=target.operator_id, vendor_id=target.vendor_id, period=target.period, sla_terms_version=target.sla_terms_version
    ) else 1


def _reviewer_named(target: VendorScorecardRow) -> bool:
    who = (target.shadow_reviewed_by or "").strip(" \t\n\r\xa0").lower()
    return bool(who) and who not in NOT_A_HUMAN_NAMES


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


@event.listens_for(VendorScorecardRow, "before_update")
def _freeze_released_card(mapper, connection, target: VendorScorecardRow) -> None:
    """Once a card is (or in this very write becomes) PUBLISHED/FINAL, its evidence is frozen.

    The realistic bypass of a CHECK that sees one row is the SAME-WRITE flip: set the status
    and the operand it keys on together (``shadow_required = 0`` with ``status = 'PUBLISHED'``,
    or ``dq_gate_threshold_pct = 99`` on a WITHHELD card on its way to DRAFT). This guard has
    what the CHECK has not -- the row's history -- so that write is refused, and so is every
    later edit to the numbers a vendor was shown. Before release, computed evidence may change
    only in a write that also names a new ``computed_by_run_id`` (a recomputation); at and
    after release nothing but ``status`` (PUBLISHED -> FINAL), the publish/finalise timestamps
    and the narrative may change. Mapper-level, so it covers every ORM writer; not Core/raw
    SQL (see the module docstring for what that leaves).
    """
    old_status, new_status = _previous(target, "status"), target.status
    releasing = new_status in RELEASED_STATUSES
    if old_status in RELEASED_STATUSES and new_status != old_status and (old_status, new_status) != (STATUS_PUBLISHED, STATUS_FINAL):
        raise ScorecardEvidenceError(f"scorecard {target.id} is {old_status}: the only move from there is PUBLISHED -> FINAL, not {new_status!r}")
    if not (releasing or old_status in RELEASED_STATUSES):
        # Unreleased: the computation may rewrite its evidence, but ONLY as a computation --
        # i.e. in the same write that names the new run. A lone edit to a dq_* operand or to
        # shadow_required, with or without a status change, has no computation behind it.
        computed = [c for c in COMPUTED_EVIDENCE_COLUMNS if _changed(target, c)]
        if computed and not _changed(target, "computed_by_run_id"):
            raise ScorecardEvidenceError(
                f"scorecard {target.id}: {', '.join(computed)} changed without a new computed_by_run_id -- "
                "evidence is written by a computation, never edited; recompute the card"
            )
        return
    frozen = [c for c in CARD_EVIDENCE_COLUMNS if _changed(target, c)]
    if frozen:
        raise ScorecardEvidenceError(
            f"scorecard {target.id}: {', '.join(frozen)} cannot change on a card that is {new_status} -- "
            "a released card's evidence is frozen; recompute a DRAFT/SHADOW/WITHHELD card or open a dispute"
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


@event.listens_for(VendorScorecardLineRow, "before_update")
def _freeze_published_line(mapper, connection, target: VendorScorecardLineRow) -> None:
    """The published figure on a line does not move once its card is released (see
    ``LINE_EVIDENCE_COLUMNS`` for what stays writable). The card's status is read on the
    flush's own connection, so a card released earlier in the same transaction counts."""
    changed = [c for c in LINE_EVIDENCE_COLUMNS if _changed(target, c)]
    if not changed:
        return
    card_id = _previous(target, "scorecard_id") or target.scorecard_id
    status = connection.execute(select(VendorScorecardRow.status).where(VendorScorecardRow.id == card_id)).scalar()
    if status in RELEASED_STATUSES:
        raise ScorecardEvidenceError(
            f"scorecard line {target.id}: {', '.join(changed)} cannot change on a {status} card -- the published figure is frozen; "
            "the dispute columns (dispute_status, adjusted_value, adjudicated_by, adjudication_reason) are where a correction lives"
        )
