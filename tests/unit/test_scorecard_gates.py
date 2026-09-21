"""Spec §7.6.2 / §7.6.4 / §7.6.6 -- the scorecard's GATES: WITHHELD, SHADOW, the human
transitions, the job, the routes and who may see what (Phase 4 Lane 4A).

The numbers are pinned in ``test_scorecard_numbers.py``. This file is about the rules that
stand between a number and a vendor's invoice, and it tests them the hostile way: not "does
the service refuse" but "what happens when nobody calls the service" -- ORM assignment
(including the same-write flip of the column a CHECK keys on, and a planted predecessor
card), raw SQL, a falsified JSON flag, a cleared column, a lower-cased status, a whitespace
or automation reviewer name. Each ORM path must fail at the MAPPER GUARDS and each raw-SQL
path the CHECKs can see must fail at the TABLE (``db/models_scorecards.py``). The raw-SQL
writes the CHECKs cannot see -- a same-statement flip of ``shadow_required`` or the
threshold by someone with write access to the database file -- are named in
``test_raw_sql_same_statement_flips_are_outside_what_the_schema_can_promise`` rather than
left silently untested.

The fixture is the golden one (``build_fixture``), imported from the sibling module so the
two files can never drift apart on what the data is.
"""

from __future__ import annotations

import importlib
from datetime import datetime
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import event, select, text
from sqlalchemy.exc import IntegrityError

from noc_agents.api import auth
from noc_agents.db.models import AgentRunRow, AgentRunStepRow, AuditRow, BroadcastRow, IncidentRow, OutboxRow
from noc_agents.db.models_scorecards import (
    CREDIT_NONE,
    CREDIT_PROPOSED,
    KPI_AVAILABILITY,
    KPI_NOTE_COMPLIANCE,
    KPI_SLA_COMPLIANCE,
    STATUS_DRAFT,
    STATUS_FINAL,
    STATUS_PUBLISHED,
    STATUS_SHADOW,
    STATUS_WITHHELD,
    ScorecardEvidenceError,
    VendorScorecardRow,
)
from noc_agents.realtime.hub import hub
from noc_agents.scheduler import JobCard
from noc_agents.services import scorecard as sc
from noc_agents.services.vendors import DEFAULT_SLA_TERMS_PATH, FLAG, load_sla_terms
from test_scorecard_numbers import NOW, PERIOD, _card, _incident, _run, _t, _terms, build_fixture  # same folder

ALL = None
DM = dict(publisher="Grace Mwangi", publisher_role="duty_manager", reason="reviewed the lines against the incident list")
REVIEW = dict(reviewer="Grace Mwangi", reviewer_role="duty_manager", rationale="checked G01's overlapping clocks by hand")


def _compute(session, settings, *, vendor="EGYPRO", terms=None, now=NOW, period=PERIOD):
    sc.compute_period(session, settings.operator, period, run_id=_run(session), terms=terms or _terms(settings.operator), now=now, vendor_code=vendor)
    session.commit()
    return _card(session, vendor, period)


def _custom_terms(tmp_path, cfg, mutate, name="terms.yaml"):
    raw = yaml.safe_load(DEFAULT_SLA_TERMS_PATH.read_text(encoding="utf-8"))
    mutate(raw)
    path = tmp_path / name
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return load_sla_terms(path, cfg=cfg)


def _infer_restores(session, numbers):
    """Turn trusted restores into note-inferred ones (or NULL provenance): the same timestamps,
    worse paperwork -- what §7.6.2's gate exists to catch."""
    for inc in session.scalars(select(IncidentRow).where(IncidentRow.incident_number.in_(list(numbers)))):
        inc.restored_source = numbers[inc.incident_number]
    session.flush()


REFUSALS = (IntegrityError, ScorecardEvidenceError)


def _set_status(session, card, status, **cols):
    """The hostile path: change columns directly and flush. Returns the refusal, if any -- the
    mapper guard's ``ScorecardEvidenceError`` or the table's ``IntegrityError``."""
    card.status = status
    for k, v in cols.items():
        setattr(card, k, v)
    try:
        session.flush()
    except REFUSALS as exc:
        session.rollback()
        return exc
    return None


# --------------------------------------------------------------------------
# WITHHELD is structural
# --------------------------------------------------------------------------


def test_too_many_inferred_restores_withholds_the_card_with_the_reason_and_no_credit(tmp_db):
    settings, session = tmp_db
    build_fixture(session)
    _infer_restores(session, {"G01": "VENDOR_NOTE_INFERRED", "G13": None})  # now 3 of 10: 30 %
    card = _compute(session, settings)
    assert card.status == STATUS_WITHHELD and card.shadow_required == 1
    dq = card.data_quality
    assert (dq["restored_incidents"], dq["inferred_restores"], dq["inferred_pct"], dq["passed"]) == (10, 3, 30.0, False)
    assert dq["inferred_by_source"] == {"NONE": 1, "VENDOR_NOTE_INFERRED": 2} and dq["inferred_incidents"] == ["G01", "G05", "G13"]
    assert dq["reason"].startswith("WITHHELD: 3 of 10 restore times (30.0 %)") and "scorecards.max_inferred_restore_pct" in dq["reason"]
    lines = sc.lines_of(session, card.id)
    assert len(lines) == 22 and all(l.credit_status == CREDIT_NONE and l.proposed_credit_pct is None for l in lines)  # numbers shown, money not proposed
    audit = session.scalars(select(AuditRow).where(AuditRow.entity_id == card.id)).one()
    assert audit.action == "scorecard.computed" and audit.rationale.startswith("WITHHELD")


def test_the_gate_is_exactly_at_threshold_inclusive_and_over_it_exclusive(tmp_db, tmp_path):
    settings, session = tmp_db
    build_fixture(session)
    assert _compute(session, settings).status == STATUS_SHADOW  # 1 of 10 = 10.0 %: not ABOVE 10
    lower = _custom_terms(tmp_path, settings.operator, lambda raw: raw["scorecards"].update(max_inferred_restore_pct=9.99))
    assert _compute(session, settings, terms=lower).status == STATUS_WITHHELD
    zero = _custom_terms(tmp_path, settings.operator, lambda raw: raw["scorecards"].update(max_inferred_restore_pct=0))
    assert _compute(session, settings, terms=zero).status == STATUS_WITHHELD
    assert sc.gate_passes(0, 0, 0) and sc.gate_passes(1, 10, 10) and not sc.gate_passes(1, 9, 10) and not sc.gate_passes(1, 10, 9.99)


def test_the_service_refuses_to_publish_a_withheld_card(tmp_db):
    settings, session = tmp_db
    build_fixture(session)
    _infer_restores(session, {"G01": "VENDOR_NOTE_INFERRED", "G13": None})
    card = _compute(session, settings)
    with pytest.raises(sc.ScorecardGateError, match="WITHHELD"):
        sc.publish_scorecard(session, card, terms=_terms(settings.operator), cfg=settings.operator, **DM)
    with pytest.raises(sc.ScorecardStateError, match="only a SHADOW card"):
        sc.record_shadow_review(session, card, **REVIEW)
    assert card.status == STATUS_WITHHELD


def test_nobody_can_move_a_failed_gate_card_off_withheld_not_even_with_raw_sql(tmp_db):
    """The CHECK re-does the arithmetic from the recorded counts: with 3 of 10 inferred the ONLY
    legal status is WITHHELD, and a 'passed: true' in the JSON changes nothing."""
    settings, session = tmp_db
    build_fixture(session)
    _infer_restores(session, {"G01": "VENDOR_NOTE_INFERRED", "G13": None})
    card = _compute(session, settings)
    card_id = card.id
    for status in (STATUS_DRAFT, STATUS_SHADOW):  # not a release: the JSON edit is evidence changing with no computation behind it
        card = session.get(VendorScorecardRow, card_id)
        exc = _set_status(session, card, status, shadow_reviewed_by="Grace Mwangi", data_quality_json='{"passed": true}')
        assert isinstance(exc, ScorecardEvidenceError) and "without a new computed_by_run_id" in str(exc), status
        card = session.get(VendorScorecardRow, card_id)
        exc = _set_status(session, card, status)  # a bare status change: the CHECK re-does the arithmetic
        assert isinstance(exc, IntegrityError) and "ck_vendor_scorecards_gate" in str(exc), status
    for status in (STATUS_PUBLISHED, STATUS_FINAL):  # a release that edits evidence in the same write: the mapper refuses first
        card = session.get(VendorScorecardRow, card_id)
        exc = _set_status(session, card, status, shadow_reviewed_by="Grace Mwangi", data_quality_json='{"passed": true}')
        assert isinstance(exc, ScorecardEvidenceError) and "frozen" in str(exc), status
        card = session.get(VendorScorecardRow, card_id)
        exc = _set_status(session, card, status)  # ...and without touching evidence: the mapper (no reviewer) or the CHECK (the gate)
        assert isinstance(exc, REFUSALS), status
    # raw SQL cannot lift it either: the gate CHECK sees the recorded counts
    for status in (STATUS_DRAFT, STATUS_SHADOW, STATUS_PUBLISHED, STATUS_FINAL):
        with pytest.raises(IntegrityError, match="ck_vendor_scorecards_gate"):
            session.execute(text("UPDATE vendor_scorecards SET status = :s, shadow_required = 0, data_quality_json = '{\"passed\": true}' WHERE id = :id"), {"s": status, "id": card_id})
        session.rollback()
    for status in (STATUS_PUBLISHED, STATUS_FINAL, "published"):
        with pytest.raises(IntegrityError):
            session.execute(text("UPDATE vendor_scorecards SET status = :s, shadow_reviewed_by = 'x' WHERE id = :id"), {"s": status, "id": card_id})
        session.rollback()
    assert session.get(VendorScorecardRow, card_id).status == STATUS_WITHHELD


def test_a_falsified_passed_flag_does_not_get_past_the_service_either(tmp_db):
    settings, session = tmp_db
    build_fixture(session)
    _infer_restores(session, {"G01": "VENDOR_NOTE_INFERRED", "G13": None})
    card = _compute(session, settings)
    card_id = card.id
    card.data_quality = {**card.data_quality, "passed": True}
    with pytest.raises(ScorecardEvidenceError, match="without a new computed_by_run_id"):
        session.flush()  # a lone edit of the JSON is refused by the mapper
    session.rollback()
    run_id = _run(session)  # dressed up as a recomputation it can be stored...
    card = session.get(VendorScorecardRow, card_id)
    card.data_quality = {**card.data_quality, "passed": True}
    card.computed_by_run_id = run_id
    session.flush()
    with pytest.raises(sc.ScorecardGateError):  # ...and the service still refuses: it re-does the arithmetic from the counts
        sc.publish_scorecard(session, card, terms=_terms(settings.operator), cfg=settings.operator, **DM)
    assert card.status == STATUS_WITHHELD


def test_fixing_the_restore_records_and_recomputing_is_the_only_way_out_of_withheld(tmp_db):
    settings, session = tmp_db
    build_fixture(session)
    _infer_restores(session, {"G01": "VENDOR_NOTE_INFERRED", "G13": None})
    assert _compute(session, settings).status == STATUS_WITHHELD
    _infer_restores(session, {"G01": "SUPERVISOR", "G13": "SUPERVISOR"})  # a named human records the real restores
    card = _compute(session, settings)
    assert card.status == STATUS_SHADOW and card.data_quality["inferred_restores"] == 1
    actions = [a.action for a in session.scalars(select(AuditRow).where(AuditRow.entity_id == card.id).order_by(AuditRow.ts, AuditRow.id))]
    assert actions == ["scorecard.computed", "scorecard.recomputed"]


# --------------------------------------------------------------------------
# SHADOW is structural
# --------------------------------------------------------------------------


def test_a_vendors_first_period_is_shadow_and_cannot_be_published_without_a_named_reviewer(tmp_db):
    settings, session = tmp_db
    build_fixture(session)
    terms = _terms(settings.operator)
    card = _compute(session, settings)
    assert card.status == STATUS_SHADOW and card.shadow_required == 1 and card.shadow_reviewed_by is None
    with pytest.raises(sc.ScorecardGateError, match="shadow review"):
        sc.publish_scorecard(session, card, terms=terms, cfg=settings.operator, **DM)
    assert card.status == STATUS_SHADOW and card.published_at is None


def test_the_shadow_gate_holds_against_the_orm_raw_sql_a_blank_reviewer_and_a_lower_cased_status(tmp_db):
    settings, session = tmp_db
    build_fixture(session)
    card = _compute(session, settings)
    card_id = card.id
    for status in (STATUS_PUBLISHED, STATUS_FINAL):
        for reviewer in (None, "", "   ", "\t\n", "\u00a0", "system", " SlaScorecardAgent ", "NOC"):
            card = session.get(VendorScorecardRow, card_id)
            exc = _set_status(session, card, status, shadow_reviewed_by=reviewer)
            assert isinstance(exc, REFUSALS) and ("shadow" in str(exc).lower()), (status, reviewer, exc)
    for statement in (
        "UPDATE vendor_scorecards SET status = 'PUBLISHED' WHERE id = :id",
        "UPDATE vendor_scorecards SET status = 'PUBLISHED', shadow_reviewed_by = ' ' WHERE id = :id",
        "UPDATE vendor_scorecards SET status = 'PUBLISHED', shadow_reviewed_by = char(9) || char(10) || char(13) WHERE id = :id",
        "UPDATE vendor_scorecards SET status = 'PUBLISHED', shadow_reviewed_by = char(160) || char(160) WHERE id = :id",  # NBSP only
        "UPDATE vendor_scorecards SET status = 'PUBLISHED', shadow_reviewed_by = 'system' WHERE id = :id",
        "UPDATE vendor_scorecards SET status = 'PUBLISHED', shadow_reviewed_by = ' SlaScorecardAgent ' WHERE id = :id",
        "UPDATE vendor_scorecards SET status = 'FINAL', shadow_reviewed_by = 'Scheduler' WHERE id = :id",
        "UPDATE vendor_scorecards SET status = 'published', shadow_reviewed_by = 'x' WHERE id = :id",  # the side door
        "UPDATE vendor_scorecards SET status = 'PUBLISHED ', shadow_reviewed_by = 'x' WHERE id = :id",
        "UPDATE vendor_scorecards SET status = 'IN_DISPUTE_WINDOW' WHERE id = :id",  # not a status
    ):
        with pytest.raises(IntegrityError):
            session.execute(text(statement), {"id": card_id})
        session.rollback()
    assert session.get(VendorScorecardRow, card_id).status == STATUS_SHADOW
    # a card row can never be INSERTED published-and-unreviewed either
    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO vendor_scorecards (id, operator_id, vendor_id, period, period_start, period_end, status, computed_at, "
                "data_quality_json, dq_restored_incidents, dq_inferred_restores, dq_gate_threshold_pct, discipline_json, shadow_required, "
                "sla_terms_version, terms_json, narrative_ai_assisted, computed_by_run_id) VALUES ('x', 'safaricom', 'v', '2026-07', "
                "'2026-06-30 21:00:00', '2026-07-31 21:00:00', 'PUBLISHED', '2026-08-01', '{}', 0, 0, 10, '{}', 1, 'v', '{}', 0, 'r')"
            )
        )
    session.rollback()


def test_clearing_the_shadow_required_column_alone_does_not_open_the_gate(tmp_db):
    """The service re-asks the table: is there an EARLIER released card for this vendor under
    these terms? Nothing else counts, so the column is a record, not a switch."""
    settings, session = tmp_db
    build_fixture(session)
    card = _compute(session, settings)
    card_id = card.id
    card.shadow_required = 0
    with pytest.raises(ScorecardEvidenceError, match="without a new computed_by_run_id"):
        session.flush()  # the lone edit is already refused by the mapper...
    session.rollback()
    run_id = _run(session)
    card = session.get(VendorScorecardRow, card_id)
    card.shadow_required = 0
    card.computed_by_run_id = run_id  # ...so dress it up as a recomputation; the service still re-asks the table
    session.flush()
    with pytest.raises(sc.ScorecardGateError, match="disagrees with the table"):
        sc.publish_scorecard(session, card, terms=_terms(settings.operator), cfg=settings.operator, **DM)
    assert card.status == STATUS_SHADOW and card.published_at is None


def test_shadow_review_needs_a_duty_manager_a_named_human_a_reason_and_a_shadow_card(tmp_db):
    settings, session = tmp_db
    build_fixture(session)
    card = _compute(session, settings)
    for role in ("noc_analyst", "shift_supervisor", "management", "msp_coordinator", ""):
        with pytest.raises(sc.ScorecardPermissionError):
            sc.record_shadow_review(session, card, **{**REVIEW, "reviewer_role": role})
    for who in ("", "  ", "system", "SlaScorecardAgent", "scheduler", "NOC"):
        with pytest.raises(ValueError, match="named human"):
            sc.record_shadow_review(session, card, **{**REVIEW, "reviewer": who})
    with pytest.raises(ValueError, match="rationale"):
        sc.record_shadow_review(session, card, **{**REVIEW, "rationale": "  "})
    assert card.shadow_reviewed_by is None
    sc.record_shadow_review(session, card, now=datetime(2026, 9, 2, 8), **REVIEW)
    assert (card.shadow_reviewed_by, card.shadow_reviewed_at, card.status) == ("Grace Mwangi", datetime(2026, 9, 2, 8), STATUS_SHADOW)  # reviewed, NOT published
    with pytest.raises(sc.ScorecardStateError, match="already"):
        sc.record_shadow_review(session, card, **REVIEW)
    assert [a.action for a in session.scalars(select(AuditRow).where(AuditRow.entity_id == card.id))] == ["scorecard.computed", "scorecard.shadow_reviewed"]


def test_reviewed_then_published_starts_the_dispute_window_and_the_next_period_is_a_draft(tmp_db, tmp_path):
    settings, session = tmp_db
    build_fixture(session)
    terms = _terms(settings.operator)
    card = _compute(session, settings)
    sc.record_shadow_review(session, card, **REVIEW)
    published_at = datetime(2026, 9, 18, 9, 0)  # a Friday
    for role in ("shift_supervisor", "management"):
        with pytest.raises(sc.ScorecardPermissionError):
            sc.publish_scorecard(session, card, terms=terms, cfg=settings.operator, **{**DM, "publisher_role": role})
    with pytest.raises(ValueError, match="reason"):
        sc.publish_scorecard(session, card, terms=terms, cfg=settings.operator, **{**DM, "reason": ""})
    sc.publish_scorecard(session, card, terms=terms, cfg=settings.operator, now=published_at, **DM)
    session.commit()
    assert (card.status, card.published_at) == (STATUS_PUBLISHED, published_at)
    assert card.dispute_window_ends_at == datetime(2026, 10, 2, 21, 0)  # 10 full working days (Mon-Fri), then 00:00 EAT on Sat 3 Oct
    with pytest.raises(sc.ScorecardStateError, match="already"):
        sc.publish_scorecard(session, card, terms=terms, cfg=settings.operator, **DM)

    # September: EGYPRO's card is a plain DRAFT -- a human has stood behind an earlier one
    _incident(session, "SEP1", "P2", "S-A", "NBI_E", "POWER", _t(5, 9, month=9), _t(5, 9, 2, month=9), _t(5, 9, 10, month=9), _t(5, 10, month=9), "MARK_RESTORED", {})
    september = _compute(session, settings, period="2026-09", now=datetime(2026, 10, 1, 6))
    assert (september.status, september.shadow_required) == (STATUS_DRAFT, 0)
    sc.publish_scorecard(session, september, terms=terms, cfg=settings.operator, **DM)  # no review needed
    assert september.status == STATUS_PUBLISHED

    # ...but a NEW terms version re-arms the rule (§7.6.2 "after any change to sla_terms.version")
    v2 = _custom_terms(tmp_path, settings.operator, lambda raw: raw["sla_terms"].update(version="2026-10-renegotiated"), name="v2.yaml")
    _incident(session, "OCT1", "P2", "S-A", "NBI_E", "POWER", _t(5, 9, month=10), _t(5, 9, 2, month=10), _t(5, 9, 10, month=10), _t(5, 10, month=10), "MARK_RESTORED", {})
    october = _compute(session, settings, terms=v2, period="2026-10", now=datetime(2026, 11, 1, 6))
    assert (october.status, october.shadow_required, october.sla_terms_version) == (STATUS_SHADOW, 1, "2026-10-renegotiated")


def test_an_earlier_card_that_was_computed_but_never_released_does_not_satisfy_the_rule(tmp_db):
    """July computed and forgotten; August is still the first card a human will inspect."""
    settings, session = tmp_db
    build_fixture(session)
    _incident(session, "JUL1", "P2", "S-A", "NBI_E", "POWER", _t(5, 9, month=7), _t(5, 9, 2, month=7), _t(5, 9, 10, month=7), _t(5, 10, month=7), "MARK_RESTORED", {})
    july = _compute(session, settings, period="2026-07")
    assert july.status == STATUS_SHADOW
    august = _compute(session, settings)
    assert (august.status, august.shadow_required) == (STATUS_SHADOW, 1)
    assert sc.shadow_required_for(session, operator_id="safaricom", vendor_id=august.vendor_id, period=PERIOD, sla_terms_version=august.sla_terms_version)
    # a card for a LATER period does not count either: "first" is chronological
    sc.record_shadow_review(session, july, **REVIEW)
    sc.publish_scorecard(session, july, terms=_terms(settings.operator), cfg=settings.operator, **DM)
    assert not sc.shadow_required_for(session, operator_id="safaricom", vendor_id=august.vendor_id, period=PERIOD, sla_terms_version=august.sla_terms_version)
    assert sc.shadow_required_for(session, operator_id="safaricom", vendor_id=august.vendor_id, period="2026-06", sla_terms_version=august.sla_terms_version)


def test_a_recompute_clears_the_review_and_a_released_card_cannot_be_recomputed(tmp_db):
    settings, session = tmp_db
    build_fixture(session)
    terms = _terms(settings.operator)
    card = _compute(session, settings)
    sc.record_shadow_review(session, card, **REVIEW)
    card = _compute(session, settings)  # the reviewer looked at the PREVIOUS numbers
    assert card.shadow_reviewed_by is None and card.status == STATUS_SHADOW
    sc.record_shadow_review(session, card, **REVIEW)
    sc.publish_scorecard(session, card, terms=terms, cfg=settings.operator, **DM)
    session.commit()
    with pytest.raises(sc.ScorecardStateError, match="PUBLISHED"):
        _compute(session, settings)
    session.rollback()
    # all-vendor compute skips it instead of failing the whole period
    report = sc.compute_period(session, settings.operator, PERIOD, run_id=_run(session), terms=terms, now=NOW)
    assert report.computed == ["TETRANET:SHADOW"] and any(s.startswith("EGYPRO:") and "PUBLISHED" in s for s in report.skipped)
    assert session.get(VendorScorecardRow, card.id).status == STATUS_PUBLISHED


def test_a_period_that_has_not_ended_cannot_be_computed(tmp_db):
    settings, session = tmp_db
    build_fixture(session)
    with pytest.raises(ValueError, match="has not ended"):
        _compute(session, settings, now=datetime(2026, 8, 31, 20, 59))  # 23:59 EAT on the 31st: one minute early
    assert _compute(session, settings, now=datetime(2026, 8, 31, 21, 0)).period == PERIOD


# --------------------------------------------------------------------------
# FINAL, and the dispute seam
# --------------------------------------------------------------------------


def test_finalise_waits_for_the_window_and_for_open_disputes(tmp_db):
    settings, session = tmp_db
    build_fixture(session)
    terms = _terms(settings.operator)
    card = _compute(session, settings)
    with pytest.raises(sc.ScorecardStateError, match="PUBLISHED"):
        sc.finalise_scorecard(session, card, actor="Grace Mwangi", actor_role="duty_manager")
    sc.record_shadow_review(session, card, **REVIEW)
    sc.publish_scorecard(session, card, terms=terms, cfg=settings.operator, now=datetime(2026, 9, 18, 9), **DM)
    with pytest.raises(sc.ScorecardStateError, match="still open"):
        sc.finalise_scorecard(session, card, actor="Grace Mwangi", actor_role="duty_manager", now=datetime(2026, 10, 2, 20, 59))
    line = sc.lines_of(session, card.id)[0]
    line.dispute_status = "OPEN"  # what the dispute lane will write -- a dispute column stays writable after release
    session.flush()
    with pytest.raises(sc.ScorecardStateError, match="OPEN dispute"):
        sc.finalise_scorecard(session, card, actor="Grace Mwangi", actor_role="duty_manager", now=datetime(2026, 10, 2, 21))
    line.dispute_status = "UPHELD"
    with pytest.raises(sc.ScorecardPermissionError):
        sc.finalise_scorecard(session, card, actor="Grace Mwangi", actor_role="shift_supervisor", now=datetime(2026, 10, 2, 21))
    sc.finalise_scorecard(session, card, actor="Grace Mwangi", actor_role="duty_manager", now=datetime(2026, 10, 2, 21))
    assert (card.status, card.finalised_at) == (STATUS_FINAL, datetime(2026, 10, 2, 21))
    with pytest.raises(sc.ScorecardStateError, match="FINAL"):
        _compute(session, settings)  # §7.6.6: 409, create a correction period instead


def test_band_for_rebands_an_adjusted_value_the_dispute_lane_will_hand_it(tmp_db):
    terms = load_sla_terms(DEFAULT_SLA_TERMS_PATH)
    bands = terms.scorecards["bands"]
    assert sc.band_for(KPI_SLA_COMPLIANCE, 50.0, bands) == "RED" and sc.band_for(KPI_SLA_COMPLIANCE, 92.5, bands) == "AMBER"
    assert sc.band_for(KPI_AVAILABILITY, 99.5, bands) == "GREEN" and sc.band_for(KPI_AVAILABILITY, 99.4999, bands) == "AMBER"


# --------------------------------------------------------------------------
# Credits: proposals only
# --------------------------------------------------------------------------


def test_credit_shapes_per_occurrence_and_escalating_and_none(tmp_db, tmp_path):
    settings, session = tmp_db
    build_fixture(session)
    terms = _terms(settings.operator)
    # TETRANET (per_occurrence [25]) has one August incident: N-TETRA, P1, 480 min with no vendor
    # note at all -> SLA and NOTE both RED on the vendor-level line -> 25 % proposed on each
    tetranet = _compute(session, settings, vendor="TETRANET", terms=terms)
    credited = {(l.kpi, l.priority): (l.proposed_credit_pct, l.credit_status) for l in sc.lines_of(session, tetranet.id) if l.credit_status != CREDIT_NONE}
    assert credited == {(KPI_SLA_COMPLIANCE, ALL): (25.0, CREDIT_PROPOSED), (KPI_NOTE_COMPLIANCE, ALL): (25.0, CREDIT_PROPOSED)}
    assert all(l.credit_status == CREDIT_NONE for l in sc.lines_of(session, tetranet.id) if l.priority is not None)  # never on a per-priority line
    # a vendor whose shape is `none` proposes nothing however RED it is
    flat = _custom_terms(tmp_path, settings.operator, lambda raw: raw["sla_terms"]["vendors"]["TETRANET"].update(credit_shape="none"))
    tetranet = _compute(session, settings, vendor="TETRANET", terms=flat)
    assert all(l.credit_status == CREDIT_NONE for l in sc.lines_of(session, tetranet.id))
    # an unknown shape is not guessed at
    odd = _custom_terms(tmp_path, settings.operator, lambda raw: raw["sla_terms"]["vendors"]["TETRANET"].update(credit_shape="quadratic_surcharge"))
    tetranet = _compute(session, settings, vendor="TETRANET", terms=odd)
    assert all(l.credit_status == CREDIT_NONE for l in sc.lines_of(session, tetranet.id))


def test_escalation_counts_only_immediately_preceding_released_red_periods(tmp_db):
    settings, session = tmp_db
    build_fixture(session)
    terms = _terms(settings.operator)
    # July: an EGYPRO breach, RED, but the card is never released -> August's step stays at 15 %
    _incident(session, "JUL1", "P1", "S-A", "NBI_E", "POWER", _t(5, 9, month=7), _t(5, 9, 2, month=7), _t(5, 9, 10, month=7), _t(5, 12, month=7), "MARK_RESTORED", {})
    july = _compute(session, settings, period="2026-07")
    assert {l.band for l in sc.lines_of(session, july.id) if l.kpi == KPI_SLA_COMPLIANCE and l.priority is None} == {"RED"}
    august = _compute(session, settings)
    sla = next(l for l in sc.lines_of(session, august.id) if l.kpi == KPI_SLA_COMPLIANCE and l.priority is None)
    assert (sla.proposed_credit_pct, sla.evidence["credit"]["consecutive_prior_red_periods"]) == (15.0, [])
    # release July, recompute August: now the second consecutive RED -> 30 %
    sc.record_shadow_review(session, july, **REVIEW)
    sc.publish_scorecard(session, july, terms=terms, cfg=settings.operator, **DM)
    august = _compute(session, settings)
    sla = next(l for l in sc.lines_of(session, august.id) if l.kpi == KPI_SLA_COMPLIANCE and l.priority is None)
    assert (sla.proposed_credit_pct, sla.credit_status, sla.evidence["credit"]["consecutive_prior_red_periods"]) == (30.0, CREDIT_PROPOSED, ["2026-07"])
    assert august.status == STATUS_DRAFT  # July's release also means August needs no shadow


# --------------------------------------------------------------------------
# The job
# --------------------------------------------------------------------------


def test_the_job_card_ships_off_and_the_job_rechecks_its_own_flag(tmp_db, monkeypatch):
    settings, session = tmp_db
    card = sc.SCORECARD_JOB
    assert isinstance(card, JobCard)
    assert (card.name, card.interval_s, card.enabled_env, card.agent, card.graph_name, card.default_enabled) == ("scorecard_close", 3600, "SCORECARDS_ENABLED", "SlaScorecardAgent", "scorecard", False)
    assert card.fn is sc.close_periods
    build_fixture(session)
    session.commit()
    monkeypatch.setenv(FLAG, "false")
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        statements.append(statement)

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        result = card.fn(session, settings)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert "off" in result.summary and "inert" in result.rationale
    assert statements == [] and session.scalars(select(VendorScorecardRow)).all() == []


def test_with_the_flag_on_the_job_computes_the_last_ended_period_once_and_never_publishes(tmp_db, monkeypatch):
    settings, session = tmp_db
    build_fixture(session)
    session.commit()
    monkeypatch.setenv(FLAG, "true")
    freeze = datetime(2026, 9, 21, 6, 0)
    monkeypatch.setattr(sc, "utcnow", lambda: freeze)
    result = sc.close_periods(session, settings)
    cards = session.scalars(select(VendorScorecardRow)).all()
    assert {c.period for c in cards} == {"2026-08"} and {c.status for c in cards} == {STATUS_SHADOW}  # EGYPRO and TETRANET; nothing PUBLISHED
    assert "computed=2" in result.summary and result.tools[0]["name"] == "scorecard.compute_period"
    for c in cards:
        run = session.get(AgentRunRow, c.computed_by_run_id)
        assert run is not None and run.graph_name == "scorecard" and run.trigger == "SCHEDULE"
    # a second tick recomputes nothing -- a card that exists is left exactly as it is
    before = {c.id: (c.computed_at, c.computed_by_run_id) for c in cards}
    again = sc.close_periods(session, settings)
    assert "computed=0" in again.summary and again.tools[0]["computed"] == 0 and "skipped" not in again.summary.replace("skipped=", "")
    detail = session.scalars(select(AuditRow).where(AuditRow.action == "scorecard.period_computed").order_by(AuditRow.ts.desc(), AuditRow.id.desc())).first()
    assert detail is not None and "EGYPRO: card exists" in detail.payload_json and "TETRANET: card exists" in detail.payload_json
    assert {c.id: (c.computed_at, c.computed_by_run_id) for c in session.scalars(select(VendorScorecardRow))} == before
    assert session.scalars(select(OutboxRow)).all() == [] and session.scalars(select(BroadcastRow)).all() == []


def test_through_the_scheduler_runner_the_card_cites_the_runners_own_run(tmp_db, monkeypatch):
    from noc_agents.db.models import get_session
    from noc_agents.scheduler.loop import run_job

    settings, session = tmp_db
    build_fixture(session)
    session.commit()
    monkeypatch.setenv(FLAG, "true")
    monkeypatch.setattr(sc, "utcnow", lambda: datetime(2026, 9, 21, 6, 0))
    outcome = run_job(sc.SCORECARD_JOB, settings, session_factory=get_session)
    assert outcome.status == "SUCCEEDED", outcome.error
    session.expire_all()
    cards = session.scalars(select(VendorScorecardRow)).all()
    assert cards and all(c.computed_by_run_id == outcome.run_id for c in cards)
    assert session.get(AgentRunRow, outcome.run_id).status == "SUCCEEDED"


def test_a_missing_terms_file_stops_the_job_loudly(tmp_db, monkeypatch, tmp_path):
    settings, session = tmp_db
    build_fixture(session)
    session.commit()
    monkeypatch.setenv(FLAG, "true")
    monkeypatch.setenv("SLA_TERMS_PATH", str(tmp_path / "missing.yaml"))
    monkeypatch.setattr(sc, "utcnow", lambda: datetime(2026, 9, 21, 6, 0))
    with pytest.raises(FileNotFoundError):
        sc.close_periods(session, settings)
    session.rollback()
    assert session.scalars(select(VendorScorecardRow)).all() == []


# --------------------------------------------------------------------------
# Routes: flag, visibility, scoping, transitions
# --------------------------------------------------------------------------

HUB_EVENT = {"site_id": "SFC-NBI-HUB-001", "site_name": "Hub", "site_type": "HUB", "region_code": "NBI_E", "alarm_code": "POWER_GRID_FAIL", "failure_domain": "POWER", "users_affected": 450000}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A live API on its own SQLite file with the lane ON (the clock-route tests' pattern)."""
    db = tmp_path / "scorecards.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")
    monkeypatch.setenv("OPERATOR_PROFILE", "safaricom")
    monkeypatch.setenv(FLAG, "true")

    import noc_agents.config as cfg
    import noc_agents.db.models as models
    import noc_agents.main as main

    cfg.clear_settings_cache()
    models._engine = None
    models.SessionLocal = None
    importlib.reload(main)
    auth.reset_sessions()
    hub._history.clear()
    c = TestClient(main.app)
    c.__enter__()
    try:
        yield c
    finally:
        c.__exit__(None, None, None)
        hub._history.clear()
        auth.reset_sessions()
        models._engine = None
        models.SessionLocal = None
        cfg.clear_settings_cache()
        monkeypatch.delenv(FLAG, raising=False)
        importlib.reload(main)


def _as(client: TestClient, role: str, name: str | None = None) -> None:
    assert client.post("/api/v1/session", json={"display_name": name or role, "role": role}).status_code == 200


def _seed(monkeypatch):
    from noc_agents.db.models import get_session

    session = get_session()
    try:
        build_fixture(session)
        session.commit()
    finally:
        session.close()
    monkeypatch.setattr(sc, "utcnow", lambda: datetime(2026, 9, 21, 6, 0))


def test_routes_are_a_404_while_the_flag_is_off(client, monkeypatch):
    monkeypatch.setenv(FLAG, "false")
    _as(client, "duty_manager")
    assert client.get("/api/v1/scorecards").status_code == 404
    assert client.get("/api/v1/scorecards/x").status_code == 404
    assert client.post("/api/v1/scorecards/compute?period=2026-08").status_code == 404
    assert client.post("/api/v1/scorecards/x/shadow-review", json={"rationale": "r"}).status_code == 404
    assert client.post("/api/v1/scorecards/x/publish", json={"reason": "r"}).status_code == 404
    assert client.post("/api/v1/scorecards/x/finalise", json={}).status_code == 404
    monkeypatch.setenv(FLAG, "true")
    assert client.get("/api/v1/scorecards").status_code == 200


def test_compute_list_hide_review_publish_through_the_api(client, monkeypatch):
    _seed(monkeypatch)
    _as(client, "shift_supervisor", "S. Kamau")
    assert client.post("/api/v1/scorecards/compute?period=2026-13").status_code == 400
    assert client.post("/api/v1/scorecards/compute?period=0001-01").status_code == 400  # never a 500
    assert client.post("/api/v1/scorecards/compute?period=%2B026-09").status_code == 400
    assert client.post("/api/v1/scorecards/compute?period=2026-08&vendor=NOBODY").status_code == 404
    r = client.post("/api/v1/scorecards/compute?period=2026-08")
    assert r.status_code == 200, r.text
    body = r.json()
    # a supervisor may trigger the computation but is told counts only: which vendor came out
    # SHADOW or WITHHELD is exactly what GET /scorecards/{id} would 404 them on
    assert body["period"] == "2026-08" and body["computed"] == 2 and body["run_id"]
    assert not {"computed_detail", "skipped_detail", "scorecard_ids"} & set(body) and "SHADOW" not in r.text
    _as(client, "duty_manager", "Grace Mwangi")
    body = client.post("/api/v1/scorecards/compute?period=2026-08").json()  # a recompute of SHADOW cards: allowed
    assert sorted(body["computed_detail"]) == ["EGYPRO:SHADOW", "TETRANET:SHADOW"] and body["computed"] == 2
    egypro_id = next(i for i, c in zip(body["scorecard_ids"], body["computed_detail"]) if c.startswith("EGYPRO"))
    _as(client, "shift_supervisor", "S. Kamau")

    # a supervisor is not a commercial reader of the working papers: the SHADOW card is hidden
    assert client.get("/api/v1/scorecards").json() == [] and client.get(f"/api/v1/scorecards/{egypro_id}").status_code == 404
    _as(client, "msp_coordinator", "vendor rep")
    assert client.get("/api/v1/scorecards").json() == [] and client.get(f"/api/v1/scorecards/{egypro_id}").status_code == 404
    _as(client, "management", "CFO")
    assert {c["status"] for c in client.get("/api/v1/scorecards").json()} == {"SHADOW"}
    detail = client.get(f"/api/v1/scorecards/{egypro_id}").json()
    assert detail["vendor_code"] == "EGYPRO" and detail["terms_notice"].startswith("defaults, not contract") and len(detail["lines"]) == 22
    line = next(l for l in detail["lines"] if l["kpi"] == KPI_SLA_COMPLIANCE and l["priority"] is None)
    assert (line["raw_value"], line["normalised_value"], line["band"], line["credit_status"]) == (50.0, 60.0, "RED", "PROPOSED")
    assert line["yaml_path"].endswith(";scorecards.bands.SLA_COMPLIANCE_PCT") and line["yaml_path"].startswith("sla_terms.default.P1.restore;")
    assert line["normalised_label"] == "contract-agreed regional allowance" and line["evidence"]["excluded"]
    assert client.get("/api/v1/scorecards?vendor=tetranet&period=2026-08").json()[0]["vendor_code"] == "TETRANET"
    assert client.get("/api/v1/scorecards?status=DRAFT").json() == [] and client.get("/api/v1/scorecards?status=BOGUS").status_code == 400
    # management may look but may not review or publish (§7.6.3 duty_manager+): 403, even with auth off
    assert client.post(f"/api/v1/scorecards/{egypro_id}/shadow-review", json={"rationale": "looked"}).status_code == 403
    assert client.post(f"/api/v1/scorecards/{egypro_id}/publish", json={"reason": "go"}).status_code == 403

    _as(client, "duty_manager", "Grace Mwangi")
    assert client.post(f"/api/v1/scorecards/{egypro_id}/publish", json={"reason": "go"}).status_code == 409  # unreviewed first period
    assert client.post(f"/api/v1/scorecards/{egypro_id}/shadow-review", json={"rationale": "   "}).status_code == 400
    r = client.post(f"/api/v1/scorecards/{egypro_id}/shadow-review", json={"rationale": "checked the overlapping clocks"})
    assert r.status_code == 200 and r.json()["scorecard"]["shadow_reviewed_by"] == "Grace Mwangi" and r.json()["scorecard"]["status"] == "SHADOW"
    assert client.post(f"/api/v1/scorecards/{egypro_id}/finalise", json={}).status_code == 409
    r = client.post(f"/api/v1/scorecards/{egypro_id}/publish", json={"reason": "reviewed"})
    assert r.status_code == 200 and r.json()["scorecard"]["status"] == "PUBLISHED" and r.json()["scorecard"]["dispute_window_ends_at"]
    assert client.post("/api/v1/scorecards/compute?period=2026-08&vendor=EGYPRO").status_code == 409  # released: not recomputable
    # now the vendor side can see it -- and only it
    _as(client, "msp_coordinator", "vendor rep")
    seen = client.get("/api/v1/scorecards").json()
    assert [c["status"] for c in seen] == ["PUBLISHED"] and client.get(f"/api/v1/scorecards/{egypro_id}").status_code == 200
    # nothing went anywhere
    from noc_agents.db.models import get_session

    session = get_session()
    try:
        assert session.scalars(select(OutboxRow)).all() == [] and session.scalars(select(BroadcastRow)).all() == []
    finally:
        session.close()


def test_another_operators_card_is_a_404_never_a_403(client, monkeypatch):
    from noc_agents.config import get_settings
    from noc_agents.db.models import get_session

    _seed(monkeypatch)
    session = get_session()
    try:
        airtel = get_settings("airtel").operator
        _incident(session, "ATL-1", "P1", "ATL-S", "NBI", "POWER", _t(9, 1), _t(9, 1, 1), _t(9, 1, 30), _t(9, 9), "MARK_RESTORED", {}, operator_id="airtel", msp="Camusat")
        sc.compute_period(session, airtel, PERIOD, run_id=_run(session, "airtel"), terms=_terms(airtel), now=NOW, vendor_code="CAMUSAT")
        session.commit()
        atl_card = session.scalars(select(VendorScorecardRow).where(VendorScorecardRow.operator_id == "airtel")).one()
        atl_id = atl_card.id
    finally:
        session.close()
    _as(client, "duty_manager", "Grace Mwangi")
    assert client.get("/api/v1/scorecards").json() == []
    assert client.get(f"/api/v1/scorecards/{atl_id}").status_code == 404
    assert client.post(f"/api/v1/scorecards/{atl_id}/shadow-review", json={"rationale": "x"}).status_code == 404
    assert client.post(f"/api/v1/scorecards/{atl_id}/publish", json={"reason": "x"}).status_code == 404
    assert client.post(f"/api/v1/scorecards/{atl_id}/finalise", json={}).status_code == 404


def test_an_authenticated_vendor_principal_without_a_vendor_binding_sees_nothing():
    """§9.3 'own vendor' cannot be enforced until the principal carries a vendor; until then an
    AUTHENTICATED msp_coordinator fails closed, and the demo's role switcher (no identity) does not."""
    from noc_agents.api.routers.scorecards import _vendor_binding

    real = auth.Principal(role="msp_coordinator", display_name="v", authenticated=True, source="cookie")
    assert _vendor_binding(real) == (True, None)
    demo = auth.Principal(role="msp_coordinator", display_name="v", authenticated=False, source="role_switcher")
    assert _vendor_binding(demo) == (False, None)
    # the seam: a principal that carries ``vendor_code`` (what the identity provider will set)
    bound = SimpleNamespace(role="msp_coordinator", display_name="v", authenticated=True, source="cookie", vendor_code="egypro")
    assert _vendor_binding(bound) == (True, "EGYPRO")
    assert _vendor_binding(auth.Principal(role="duty_manager", display_name="d", authenticated=True, source="cookie")) == (False, None)


# --------------------------------------------------------------------------
# The mapper guards: the bypasses the adversarial review reproduced
# --------------------------------------------------------------------------


def _refused(session, action):
    """Run ``action`` and flush; return the refusal (mapper or table) or ``None``."""
    try:
        action()
        session.flush()
    except REFUSALS as exc:
        session.rollback()
        return exc
    return None


def test_the_same_write_flip_of_shadow_required_is_refused_by_the_mapper(tmp_db):
    """The reviewers' reproduction: ``card.shadow_required = 0; card.status = "PUBLISHED"`` used
    to commit with no reviewer, because the CHECK saw a consistent row. The mapper guard sees
    the row's history and refuses the write; the card stays SHADOW with its column intact."""
    settings, session = tmp_db
    build_fixture(session)
    card = _compute(session, settings)
    card_id = card.id

    def flip():
        card.shadow_required = 0
        card.status = STATUS_PUBLISHED

    exc = _refused(session, flip)
    assert isinstance(exc, ScorecardEvidenceError) and "shadow_required" in str(exc) and "frozen" in str(exc)
    fresh = session.get(VendorScorecardRow, card_id)
    assert (fresh.status, fresh.shadow_required, fresh.shadow_reviewed_by) == (STATUS_SHADOW, 1, None)
    # the two-step variant: a lone edit to the column is refused (evidence changes only with a
    # computation); dressed up with a new run id it passes that test but is refused at the
    # release, because the column is re-derived from the table at that moment
    exc = _refused(session, lambda: setattr(fresh, "shadow_required", 0))
    assert isinstance(exc, ScorecardEvidenceError) and "without a new computed_by_run_id" in str(exc)
    run_id = _run(session)
    fresh = session.get(VendorScorecardRow, card_id)
    fresh.shadow_required = 0
    fresh.computed_by_run_id = run_id
    session.flush()
    exc = _refused(session, lambda: setattr(fresh, "status", STATUS_PUBLISHED))
    assert isinstance(exc, ScorecardEvidenceError) and "contradicts the table" in str(exc)
    # and with the column honest but no reviewer, the release itself is refused
    fresh = session.get(VendorScorecardRow, card_id)
    exc = _refused(session, lambda: setattr(fresh, "status", STATUS_PUBLISHED))
    assert isinstance(exc, ScorecardEvidenceError) and "named shadow reviewer" in str(exc)
    assert session.get(VendorScorecardRow, card_id).status == STATUS_SHADOW


def test_a_planted_predecessor_card_is_refused_and_cannot_propagate(tmp_db):
    """INSERTing a fake earlier card as PUBLISHED with shadow_required = 0 would make the next
    period's ``shadow_required_for`` find a "released" card and compute it as DRAFT. Refused
    at insert (a card is never born released), refused as a DRAFT with a planted column (the
    column is derived from the table), and refused when a planted DRAFT is later flipped."""
    settings, session = tmp_db
    build_fixture(session)
    august = _compute(session, settings)
    vendor_id = august.vendor_id

    def fake(status, shadow_required, period="2026-07", **cols):
        row = VendorScorecardRow(
            id=f"fake-{period}-{status}-{shadow_required}", operator_id="safaricom", vendor_id=vendor_id, period=period,
            period_start=datetime(2026, 6, 30, 21), period_end=datetime(2026, 7, 31, 21), status=status, computed_at=NOW,
            data_quality_json="{}", dq_restored_incidents=0, dq_inferred_restores=0, dq_gate_threshold_pct=10.0, discipline_json="{}",
            shadow_required=shadow_required, sla_terms_version=august.sla_terms_version, computed_by_run_id="r", **cols,
        )
        session.add(row)
        return row

    exc = _refused(session, lambda: fake(STATUS_PUBLISHED, 0))
    assert isinstance(exc, ScorecardEvidenceError) and "cannot be inserted as PUBLISHED" in str(exc)
    exc = _refused(session, lambda: fake(STATUS_FINAL, 0, shadow_reviewed_by="Somebody"))
    assert isinstance(exc, ScorecardEvidenceError) and "cannot be inserted as FINAL" in str(exc)
    exc = _refused(session, lambda: fake(STATUS_DRAFT, 0))  # no earlier released card exists: the table says 1
    assert isinstance(exc, ScorecardEvidenceError) and "contradicts the table" in str(exc)
    # an honest DRAFT for July (shadow_required = 1) may be inserted -- and then cannot be flipped
    honest = fake(STATUS_DRAFT, 1)
    session.flush()
    exc = _refused(session, lambda: setattr(honest, "status", STATUS_PUBLISHED))
    assert isinstance(exc, ScorecardEvidenceError) and "named shadow reviewer" in str(exc)
    # nothing propagated: August is still a first period
    assert sc.shadow_required_for(session, operator_id="safaricom", vendor_id=vendor_id, period=PERIOD, sla_terms_version=august.sla_terms_version)
    session.expire_all()
    assert _compute(session, settings).status == STATUS_SHADOW
    # raw SQL cannot plant a PUBLISHED predecessor with shadow_required = 1 either (the CHECK sees the blank reviewer)
    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO vendor_scorecards (id, operator_id, vendor_id, period, period_start, period_end, status, computed_at, "
                "data_quality_json, dq_restored_incidents, dq_inferred_restores, dq_gate_threshold_pct, discipline_json, shadow_required, "
                "sla_terms_version, terms_json, narrative_ai_assisted, computed_by_run_id) VALUES ('raw', 'safaricom', :v, '2026-07', "
                "'2026-06-30 21:00:00', '2026-07-31 21:00:00', 'PUBLISHED', '2026-08-01', '{}', 0, 0, 10, '{}', 1, :ver, '{}', 0, 'r')"
            ),
            {"v": vendor_id, "ver": august.sla_terms_version},
        )
    session.rollback()


def test_the_threshold_cannot_be_moved_to_let_a_withheld_card_out(tmp_db, tmp_path):
    """The contested sibling: ``dq_gate_threshold_pct = 100`` (or 99) in the same write as the
    status used to reach PUBLISHED or FINAL with data_quality_json still saying passed=false.
    Now: 100 is outside the CHECK's bound, and any threshold change while releasing is a frozen
    evidence change the mapper refuses."""
    settings, session = tmp_db
    build_fixture(session)
    _infer_restores(session, {"G01": "VENDOR_NOTE_INFERRED", "G13": None})
    card = _compute(session, settings)
    card_id = card.id
    for status in (STATUS_PUBLISHED, STATUS_FINAL, STATUS_DRAFT, STATUS_SHADOW, STATUS_WITHHELD):
        for threshold in (100.0, 99.0, 1000.0, 30.0):
            card = session.get(VendorScorecardRow, card_id)
            exc = _set_status(session, card, status, dq_gate_threshold_pct=threshold, shadow_reviewed_by="Grace Mwangi")
            assert isinstance(exc, REFUSALS), (status, threshold)
            fresh = session.get(VendorScorecardRow, card_id)
            assert (fresh.status, fresh.dq_gate_threshold_pct) == (STATUS_WITHHELD, 10.0)
    # the same nudge dressed up as a recomputation still cannot LEAVE WITHHELD: the CHECK re-does the arithmetic
    card = session.get(VendorScorecardRow, card_id)
    exc = _set_status(session, card, STATUS_DRAFT, dq_gate_threshold_pct=20.0, computed_by_run_id=_run(session))
    assert isinstance(exc, IntegrityError) and "ck_vendor_scorecards_gate" in str(exc)
    # even on its own, an out-of-range threshold is refused by the table
    for threshold in (100, 100.0, -1, 250):
        with pytest.raises(IntegrityError):
            session.execute(text("UPDATE vendor_scorecards SET dq_gate_threshold_pct = :t WHERE id = :id"), {"t": threshold, "id": card_id})
        session.rollback()
    # and the service refuses a terms file that asks for one
    hundred = _custom_terms(tmp_path, settings.operator, lambda raw: raw["scorecards"].update(max_inferred_restore_pct=100))
    with pytest.raises(ValueError, match=r"\[0, 100\)"):
        sc.compute_period(session, settings.operator, PERIOD, run_id=_run(session), terms=hundred, now=NOW, vendor_code="EGYPRO")


def test_a_released_cards_evidence_and_published_figures_are_frozen(tmp_db):
    settings, session = tmp_db
    build_fixture(session)
    terms = _terms(settings.operator)
    card = _compute(session, settings)
    sc.record_shadow_review(session, card, **REVIEW)
    sc.publish_scorecard(session, card, terms=terms, cfg=settings.operator, **DM)
    session.commit()
    card_id = card.id
    for column, value in (
        ("dq_inferred_restores", 0), ("dq_restored_incidents", 99), ("dq_gate_threshold_pct", 50.0), ("shadow_required", 0),
        ("shadow_reviewed_by", "Somebody Else"), ("sla_terms_version", "2030-01"), ("computed_by_run_id", "other"),
        ("data_quality_json", "{}"), ("discipline_json", "{}"), ("terms_json", "{}"), ("period", "2026-07"), ("vendor_id", "v2"),
    ):
        card = session.get(VendorScorecardRow, card_id)
        exc = _refused(session, lambda: setattr(card, column, value))
        assert isinstance(exc, ScorecardEvidenceError) and column in str(exc), column
    card = session.get(VendorScorecardRow, card_id)
    for status in (STATUS_DRAFT, STATUS_SHADOW, STATUS_WITHHELD):  # no way back from a release except forward to FINAL
        exc = _refused(session, lambda: setattr(card, "status", status))
        assert isinstance(exc, ScorecardEvidenceError) and "PUBLISHED -> FINAL" in str(exc), status
        card = session.get(VendorScorecardRow, card_id)
    card.narrative = "QBR narrative, written after release"  # NOT evidence: allowed
    session.flush()
    line = sc.lines_of(session, card_id)[0]
    for column, value in (("raw_value", 1.0), ("formula", "x"), ("evidence_json", "{}"), ("scc_minutes_deducted", 7), ("proposed_credit_pct", 99.0), ("eligible_incidents", 42)):
        exc = _refused(session, lambda: setattr(line, column, value))
        assert isinstance(exc, ScorecardEvidenceError) and column in str(exc), column
        line = sc.lines_of(session, card_id)[0]
    line.dispute_status, line.adjusted_value, line.adjudicated_by, line.adjudication_reason = "ADJUSTED", 55.0, "Grace Mwangi", "clock evidence"
    line.credit_status = "WITHDRAWN"  # what humans decide about a figure stays writable
    session.flush()
    assert session.get(VendorScorecardRow, card_id).status == STATUS_PUBLISHED


def test_raw_sql_same_statement_flips_are_outside_what_the_schema_can_promise(tmp_db):
    """DOCUMENTED, NOT PREVENTED. A single raw statement that sets ``status = 'PUBLISHED'``
    together with ``shadow_required = 0`` (or a WITHHELD card's threshold to 99) produces a row
    the CHECKs see as consistent, and no mapper runs for raw SQL. A trigger is ruled out
    (``db/migrate.py`` builds new tables from compiled DDL strings, so a DDL-event trigger
    would exist on fresh files and not on migrated ones). This is raw write access to the
    database file, which the schema cannot promise anything about; what remains is the audit
    trail -- no ``scorecard.published`` row will exist for such a card -- and the recompute."""
    settings, session = tmp_db
    build_fixture(session)
    card = _compute(session, settings)
    card_id = card.id
    session.execute(text("UPDATE vendor_scorecards SET status = 'PUBLISHED', shadow_required = 0 WHERE id = :id"), {"id": card_id})
    session.commit()
    session.expire_all()
    assert session.get(VendorScorecardRow, card_id).status == STATUS_PUBLISHED  # the documented gap
    assert not session.scalars(select(AuditRow).where(AuditRow.entity_id == card_id, AuditRow.action == "scorecard.published")).all()


# --------------------------------------------------------------------------
# The dispute window, run summaries, the PIR firewall at runtime
# --------------------------------------------------------------------------


def test_the_dispute_window_ends_at_end_of_day_so_publishing_late_never_shortens_it():
    """10 working days: Fri 18 Sep 23:30 EAT, Sat 19 Sep 01:00 EAT and Sun 20 Sep all run to
    00:00 EAT on Sat 3 Oct (= 2 Oct 21:00Z): Mon 21 ... Fri 2 Oct are the ten full days."""
    saturday_3_oct_midnight_eat = datetime(2026, 10, 2, 21, 0)
    assert sc.dispute_window_end(datetime(2026, 9, 18, 6, 0), 10) == saturday_3_oct_midnight_eat  # Fri 09:00 EAT
    assert sc.dispute_window_end(datetime(2026, 9, 18, 20, 30), 10) == saturday_3_oct_midnight_eat  # Fri 23:30 EAT
    assert sc.dispute_window_end(datetime(2026, 9, 18, 22, 0), 10) == saturday_3_oct_midnight_eat  # Sat 01:00 EAT
    assert sc.dispute_window_end(datetime(2026, 9, 20, 9, 0), 10) == saturday_3_oct_midnight_eat  # Sun 12:00 EAT
    assert sc.dispute_window_end(datetime(2026, 9, 21, 6, 0), 10) == datetime(2026, 10, 5, 21, 0)  # Mon 09:00 EAT -> Tue 6 Oct 00:00 EAT
    assert sc.dispute_window_end(datetime(2026, 9, 18, 20, 30), 0) == datetime(2026, 9, 18, 21, 0)  # zero days: end of the publish day
    with pytest.raises(ValueError):
        sc.dispute_window_end(datetime(2026, 9, 18, 6, 0), -1)


def test_run_and_step_summaries_carry_counts_only_never_a_vendors_unreleased_status(tmp_db, monkeypatch):
    """``GET /api/v1/runs`` is served to every reader role (msp_coordinator included) while an
    unreleased card is a 404 to them, so no run row, step output, rationale or tools payload may
    say which vendor came out SHADOW or WITHHELD. The detail lives in audit_events (AUDIT_READERS)."""
    settings, session = tmp_db
    build_fixture(session)
    _infer_restores(session, {"N-TETRA": "VENDOR_NOTE_INFERRED"})  # TETRANET: 1 of 1 inferred -> WITHHELD
    session.commit()
    monkeypatch.setenv(FLAG, "true")
    monkeypatch.setattr(sc, "utcnow", lambda: datetime(2026, 9, 21, 6, 0))
    report, run_id = sc.compute_on_request(session, settings, PERIOD, actor="Grace Mwangi")
    session.commit()
    assert sorted(report.computed) == ["EGYPRO:SHADOW", "TETRANET:WITHHELD"]  # the object has the detail...
    session.execute(text("DELETE FROM vendor_scorecard_lines"))
    session.execute(text("DELETE FROM vendor_scorecards"))
    session.commit()
    job = sc.close_periods(session, settings)
    forbidden = ("SHADOW", "WITHHELD", "DRAFT", "EGYPRO:", "TETRANET:")
    texts = [job.summary, job.rationale, str(job.tools)]
    for run in session.scalars(select(AgentRunRow).where(AgentRunRow.graph_name == sc.GRAPH_NAME)):
        texts += [str(run.error_summary), str(run.current_node)]
    for step in session.scalars(select(AgentRunStepRow)):
        texts += [str(step.output_summary), str(step.rationale), str(step.input_summary), step.tools_called_json]
    for chunk in texts:
        assert not any(word in chunk for word in forbidden), chunk
    assert "computed=2" in job.summary and job.tools[0]["computed"] == 2
    # ...and the audit trail has it, per vendor
    audits = session.scalars(select(AuditRow).where(AuditRow.action == "scorecard.period_computed")).all()
    assert audits and all("EGYPRO:SHADOW" in a.payload_json and "TETRANET:WITHHELD" in a.payload_json for a in audits)
    assert audits[0].entity_type == "vendor_scorecard_period" and audits[0].entity_id == PERIOD


def test_the_scorecard_paths_emit_no_sql_against_the_pir_tables(tmp_db, monkeypatch):
    """The runtime half of the firewall, for THIS lane: with the lane on and a review row
    present, every statement of compute_period, compute_on_request and close_periods is
    captured, and none names a PIR table. (``test_pir.py``'s capture runs the registered jobs
    with only PIR_ENABLED on, so it cannot reach this computation.)"""
    from noc_agents.services import pir as pir_service

    settings, session = tmp_db
    rows = build_fixture(session)
    pir_service.open_pir(session, rows["G01"], reason=pir_service.REASON_P1_P2)
    session.commit()
    monkeypatch.setenv(FLAG, "true")
    monkeypatch.setattr(sc, "utcnow", lambda: datetime(2026, 9, 21, 6, 0))
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        statements.append(statement)

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        sc.compute_period(session, settings.operator, PERIOD, run_id=_run(session), terms=_terms(settings.operator), now=NOW)
        session.commit()
        sc.compute_on_request(session, settings, PERIOD, actor="Grace Mwangi")
        session.commit()
        session.execute(text("DELETE FROM vendor_scorecard_lines"))
        session.execute(text("DELETE FROM vendor_scorecards"))
        session.commit()
        sc.close_periods(session, settings)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert len(statements) > 50, "the harness must actually have captured the computation's SQL"
    touched = [s for s in statements if "post_incident_reviews" in s or "pir_action_items" in s]
    assert touched == [], touched[:3]
    assert session.scalars(select(VendorScorecardRow)).all()  # the job did compute
