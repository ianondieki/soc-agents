"""Spec §7.7 — post-incident reviews: the trigger matrix, blamelessness, and the firewall.

Four things are worth protecting here and each has its own section below.

**The flag.** §7.7 ships the lane OFF. A route that answers while ``PIR_ENABLED`` is unset
is a behaviour change to a running NOC, so the first section proves the whole surface is
invisible by default and that the job is inert even if someone wires its card in early.

**The trigger matrix.** The spec's own acceptance criterion: a P2 that restores produces a
DRAFT with a populated timeline and MTTA/MTTR; a P4 closed inside its SLA produces nothing.
Both directions are pinned, because a review system that opens a postmortem for every rural
BTS outage is abandoned within a week, and one that opens none for a P1 is decorative.

**Blamelessness.** §7.7 states the mechanism plainly: "Blamelessness is structural or the
PIR feeds the scorecard and engineers write defensive notes." The validator is therefore
tested as a *gate* — 422 on the way in, with the exact message the spec requires — and so is
the case that would quietly break it: an ``rnio_name`` of "RNIO" must not make the validator
reject the very wording its own error message recommends.

**The firewall.** §5.3.17 and §7.7.3 both require that the scorecard and individual-metrics
paths never read the PIR tables — the whole blamelessness argument collapses the moment a
postmortem can affect a vendor's KPI or a person's file. The last section is the spec's
"grep + join test": a static sweep of the source tree, and a runtime capture of the SQL
every scheduled job actually emits.
"""

from __future__ import annotations

import ast
import importlib
import io
import json
import pathlib
import tokenize
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select

from noc_agents.api import auth
from noc_agents.db.models import (
    ExternalSignalRow,
    AgentRunRow,
    AgentRunStepRow,
    BroadcastRow,
    HitlTaskRow,
    IncidentRow,
    OutboxRow,
    ProblemRow,
    WorkNoteRow,
    new_id,
)
from noc_agents.db.models_pir import PirActionItemRow, PostIncidentReviewRow
from noc_agents.realtime.commit_hook import pending_events
from noc_agents.realtime.hub import hub
from noc_agents.services import pir as pir_service

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "noc_agents"

T0 = datetime(2026, 9, 16, 1, 0, 0)  # naive UTC, the storage contract


# --------------------------------------------------------------------------
# Fixtures and builders
# --------------------------------------------------------------------------


@pytest.fixture()
def on(monkeypatch):
    """The lane switched on. Every test that wants a route to answer asks for this."""
    monkeypatch.setenv(pir_service.ENABLED_ENV, "true")


@pytest.fixture()
def off(monkeypatch):
    """The shipped default, stated explicitly rather than inherited from the environment."""
    monkeypatch.setenv(pir_service.ENABLED_ENV, "false")


def _incident(session, **overrides) -> IncidentRow:
    """A restored P2 at an ordinary site — the trigger-matrix positive case by default."""
    values = dict(
        operator_id="safaricom",
        incident_number="INC000001",
        status="RESTORED",
        priority="P2",
        users_affected=5000,
        site_id="SFC-NBI-BTS-001",
        site_type="BTS",
        region_code="NBI",
        correlation_fingerprint="fp-1",
        failure_domain="POWER",
        alarm_code="PWR_MAINS_FAIL",
        tt_category_label="Power - mains failure",
        failure_time=T0,
        escalated_at=T0 + timedelta(minutes=5),
        acknowledged_at=T0 + timedelta(minutes=3),
        first_vendor_note_at=T0 + timedelta(minutes=15),
        restored_at=T0 + timedelta(minutes=120),
        sla_restore_due=T0 + timedelta(minutes=180),
        created_at=T0,
        updated_at=T0 + timedelta(minutes=120),
        assignee_name="Kevin Ochieng",
        rnio_name="RNIO",
        resolution_summary="Mains restored, genset topped up",
    )
    values.update(overrides)
    inc = IncidentRow(**values)
    session.add(inc)
    session.flush()
    return inc


def _populate_history(session, inc: IncidentRow) -> None:
    """Work notes, an instrumented run, a sent broadcast and a HITL task: timeline fodder."""
    session.add_all(
        [
            WorkNoteRow(
                incident_id=inc.id,
                author="Kevin Ochieng",
                author_role="noc_analyst",
                body="Site down, dispatching",
                created_at=T0 + timedelta(minutes=2),
            ),
            WorkNoteRow(
                incident_id=inc.id,
                author="EGYPRO FE",
                author_role="field_engineer",
                body="On site, generator out of fuel",
                created_at=T0 + timedelta(minutes=60),
            ),
        ]
    )
    run = AgentRunRow(incident_id=inc.id, operator_id=inc.operator_id, status="SUCCEEDED", started_at=T0)
    session.add(run)
    session.flush()
    session.add_all(
        [
            AgentRunStepRow(
                run_id=run.id,
                seq=1,
                node_name="INGEST",
                agent_name="IngestCorrelationAgent",
                status="SUCCEEDED",
                started_at=T0,
                finished_at=T0 + timedelta(seconds=2),
                output_summary="incident opened",
            ),
            AgentRunStepRow(
                run_id=run.id,
                seq=2,
                node_name="ASSIGN",
                agent_name="DispatchAssignmentAgent",
                status="SUCCEEDED",
                started_at=T0 + timedelta(seconds=3),
                finished_at=T0 + timedelta(seconds=4),
                output_summary="assigned to MSP_POWER",
            ),
        ]
    )
    session.add(
        BroadcastRow(
            incident_id=inc.id,
            channel="SMS",
            audience="INTERNAL",
            message="P2 power outage at SFC-NBI-BTS-001",
            status="SENT",
            sent_at=T0 + timedelta(minutes=6),
        )
    )
    session.add(
        HitlTaskRow(
            incident_id=inc.id,
            task_type="APPROVE_CUSTOMER_BROADCAST",
            status="APPROVED",
            resolved_by="Grace Wanjiru",
            created_at=T0 + timedelta(minutes=4),
        )
    )
    session.flush()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A live API on its own SQLite file (the reload pattern the other route tests use)."""
    db = tmp_path / "pir.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")
    monkeypatch.setenv("OPERATOR_PROFILE", "safaricom")

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
        importlib.reload(main)


def _api_session():
    from noc_agents.db.models import get_session

    return get_session()


def _seed_via_api(**overrides) -> tuple[str, str]:
    """Build an incident (with history) straight into the API's own database.

    Returns ``(incident_id, pir_id_or_empty)``; the review is opened through the service so
    the route tests start from the state the 5-minute job would have produced.
    """
    session = _api_session()
    try:
        inc = _incident(session, **overrides)
        _populate_history(session, inc)
        pir, _created = pir_service.open_pir(session, inc, reason=pir_service.REASON_P1_P2)
        session.commit()
        return inc.id, pir.id
    finally:
        session.close()


# --------------------------------------------------------------------------
# The flag: with PIR_ENABLED unset the lane does not exist
# --------------------------------------------------------------------------


def test_the_flag_defaults_to_false_so_an_unset_environment_keeps_todays_behaviour(monkeypatch):
    monkeypatch.delenv(pir_service.ENABLED_ENV, raising=False)
    assert pir_service.pir_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_only_an_explicit_true_value_turns_the_lane_on(monkeypatch, value):
    monkeypatch.setenv(pir_service.ENABLED_ENV, value)
    assert pir_service.pir_enabled() is True


def test_every_pir_route_is_a_404_while_the_flag_is_off(client, off):
    """404 and not 403: the feature does not exist yet, and saying "not allowed" would be a
    different — and false — statement about a deployment that has never enabled it."""
    for method, path in (
        ("get", "/api/v1/pir"),
        ("get", "/api/v1/pir/awaiting-review"),
        ("get", "/api/v1/pir/anything"),
        ("get", "/api/v1/pir/anything/actions"),
        ("patch", "/api/v1/pir/anything"),
        ("post", "/api/v1/pir/anything/actions"),
        ("patch", "/api/v1/pir/anything/actions/whatever"),
        ("post", "/api/v1/pir/anything/publish"),
        ("post", "/api/v1/pir/anything/draft/llm"),
        ("post", "/api/v1/incidents/anything/pir"),
        ("patch", "/api/v1/problems/anything"),
        ("get", "/api/v1/incidents/anything/known-error"),
    ):
        response = client.request(method.upper(), path, json={})
        assert response.status_code == 404, f"{method.upper()} {path} answered {response.status_code}"
        assert "PIR_ENABLED" in response.json()["detail"]


def test_the_auto_open_job_is_inert_while_the_flag_is_off(tmp_db, off):
    """Re-checked inside the job, not only on its card: a card wired into the loop early
    must do nothing, rather than merely never be scheduled."""
    settings, session = tmp_db
    inc = _incident(session)
    session.commit()

    result = pir_service.auto_open(session, settings, now=T0 + timedelta(minutes=130))

    assert "PIR_ENABLED=false" in result.summary
    assert session.scalars(select(PostIncidentReviewRow)).all() == []
    assert inc.id  # the incident itself is untouched


def test_the_job_card_reports_itself_off_when_the_flag_is_unset():
    """``default_enabled=False`` keeps /scheduler/status honest: an operator who reads
    'enabled' and finds no reviews has a worse problem than one who reads 'off'."""
    card = pir_service.PIR_JOB
    assert card.name == "pir_autoopen"
    assert card.interval_s == 300  # every 5 min (§7.7.3)
    assert card.enabled_env == "PIR_ENABLED"
    assert card.default_enabled is False
    assert card.agent == "PostIncidentReviewAgent"


# --------------------------------------------------------------------------
# The trigger matrix (§5.3.18 / §7.7.7)
# --------------------------------------------------------------------------


def test_a_restored_p2_opens_a_draft_with_a_populated_timeline_and_metrics(tmp_db, on):
    settings, session = tmp_db
    inc = _incident(session)
    _populate_history(session, inc)
    session.commit()

    result = pir_service.auto_open(session, settings, now=T0 + timedelta(minutes=130))
    session.commit()

    pir = session.scalar(select(PostIncidentReviewRow).where(PostIncidentReviewRow.incident_id == inc.id))
    assert pir is not None, result.summary
    assert pir.status == "DRAFT"
    assert pir.opened_reason == pir_service.REASON_P1_P2

    timeline = json.loads(pir.timeline_json)
    kinds = {entry["kind"] for entry in timeline}
    # Every source §7.7.3 names that has rows in this fixture is represented.
    assert {"work_note", "agent_step", "broadcast", "hitl"} <= kinds
    assert len(timeline) >= 6
    assert timeline == sorted(timeline, key=lambda e: e["ts"]), "the timeline must read in time order"

    # MTTA is §7.6.2's pair (first_vendor_note_at − escalated_at) = 15 − 5 = 10 minutes.
    assert pir.mtta_minutes == 10.0
    # MTTR is restored_at − failure_time = 120 minutes; with no stop-clock table the adjusted
    # figure equals it, because no deduction can be evidenced.
    assert pir.mttr_minutes == 120.0
    assert pir.adjusted_mttr_minutes == 120.0

    impact = json.loads(pir.impact_json)
    assert impact["users_affected"] == 5000
    assert impact["duration_minutes"] == 120.0
    assert impact["revenue_note"] is None, "a revenue estimate is a commercial judgement, never computed here"


def test_a_p4_closed_inside_its_sla_opens_no_review_at_all(tmp_db, on):
    """The negative half of the acceptance criterion. A postmortem for every rural BTS
    outage is how a review process stops being read."""
    settings, session = tmp_db
    inc = _incident(
        session,
        incident_number="INC000004",
        priority="P4",
        status="CLOSED",
        users_affected=200,
        restored_at=T0 + timedelta(minutes=60),
        closed_at=T0 + timedelta(minutes=70),
        sla_restore_due=T0 + timedelta(minutes=480),
    )
    session.commit()

    assert pir_service.trigger_reason(session, inc) is None
    result = pir_service.auto_open(session, settings, now=T0 + timedelta(minutes=80))
    session.commit()

    assert session.scalars(select(PostIncidentReviewRow)).all() == []
    assert "opened=0" in result.summary


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"priority": "P1"}, pir_service.REASON_P1_P2),
        ({"priority": "P4", "site_type": "HUB"}, pir_service.REASON_HUB_CORE),
        ({"priority": "P4", "site_type": "CORE"}, pir_service.REASON_HUB_CORE),
        (
            {"priority": "P4", "restored_at": T0 + timedelta(minutes=500), "sla_restore_due": T0 + timedelta(minutes=480)},
            pir_service.REASON_SLA_BREACH,
        ),
        ({"priority": "P4", "problem_id": "prb-1"}, pir_service.REASON_PROBLEM_LINKED),
    ],
)
def test_each_rule_of_the_trigger_matrix_names_itself_in_opened_reason(tmp_db, on, overrides, expected):
    """``opened_reason`` is the answer to "why is there a PIR for this?", so each rule has to
    be distinguishable after the fact, not merely to have fired."""
    _settings, session = tmp_db
    inc = _incident(session, **overrides)
    session.commit()
    assert pir_service.trigger_reason(session, inc) == expected


def test_a_failed_lifecycle_run_opens_a_review_even_for_a_quiet_p4(tmp_db, on):
    """The agents failing on a ticket is itself the thing to review — nobody else will notice."""
    _settings, session = tmp_db
    inc = _incident(session, priority="P4", site_type="BTS", users_affected=10)
    session.add(AgentRunRow(incident_id=inc.id, operator_id=inc.operator_id, status="FAILED", started_at=T0))
    session.commit()
    assert pir_service.trigger_reason(session, inc) == pir_service.REASON_RUN_FAILED


def test_a_cancelled_incident_gets_not_required_rather_than_a_draft(tmp_db, on):
    """A false alarm has nothing to review — but the row still exists, so "no review needed"
    and "nobody looked" are different states on the wallboard."""
    _settings, session = tmp_db
    inc = _incident(session, status="CANCELLED", priority="P1")
    session.commit()

    pir, created = pir_service.open_pir(session, inc, reason=pir_service.REASON_MANUAL)
    session.commit()

    assert created is True
    assert pir.status == "NOT_REQUIRED"
    assert pir.status != "DRAFT"


def test_opening_a_review_twice_returns_the_first_one(tmp_db, on):
    """UNIQUE(incident_id) plus the check: two ticks of a 5-minute job that overlap, or a
    manual open landing beside one, must not produce two postmortems of one outage."""
    _settings, session = tmp_db
    inc = _incident(session)
    session.commit()

    first, created_first = pir_service.open_pir(session, inc, reason=pir_service.REASON_P1_P2)
    session.commit()
    second, created_second = pir_service.open_pir(session, inc, reason=pir_service.REASON_MANUAL)
    session.commit()

    assert created_first is True and created_second is False
    assert first.id == second.id
    assert second.opened_reason == pir_service.REASON_P1_P2, "the original reason is not overwritten"
    assert len(session.scalars(select(PostIncidentReviewRow)).all()) == 1


def test_the_job_is_idempotent_across_ticks(tmp_db, on):
    settings, session = tmp_db
    inc = _incident(session)
    session.commit()

    first = pir_service.auto_open(session, settings, now=T0 + timedelta(minutes=130))
    session.commit()
    second = pir_service.auto_open(session, settings, now=T0 + timedelta(minutes=135))
    session.commit()

    assert "opened=1" in first.summary
    assert "opened=0" in second.summary
    assert len(session.scalars(select(PostIncidentReviewRow)).all()) == 1
    assert inc.id


# --------------------------------------------------------------------------
# The pir.opened event: buffered, never published from inside the transaction
# --------------------------------------------------------------------------


def test_pir_opened_waits_for_the_commit_and_is_discarded_by_a_rollback(tmp_db, on):
    """``buffer_event``, never ``hub.publish_sync``: an event announced inside a transaction
    that then rolls back tells the wallboard about a review that does not exist. That exact
    bug is why realtime/commit_hook.py exists."""
    _settings, session = tmp_db
    inc = _incident(session)
    session.commit()
    before = hub.last_seq

    pir_service.open_pir(session, inc, reason=pir_service.REASON_P1_P2)
    buffered = pending_events(session)
    assert [e.type for e in buffered] == ["pir.opened"]
    assert hub.last_seq == before, "nothing may reach subscribers while the transaction is open"

    session.rollback()
    assert pending_events(session) == []
    assert hub.last_seq == before


def test_pir_opened_carries_the_incident_number_and_the_reason(tmp_db, on):
    _settings, session = tmp_db
    inc = _incident(session)
    session.commit()
    before = hub.last_seq

    pir, _ = pir_service.open_pir(session, inc, reason=pir_service.REASON_HUB_CORE)
    session.commit()

    assert hub.last_seq == before + 1
    published = hub.recent(1)[0]
    assert published["type"] == "pir.opened"
    assert published["payload"]["incident_number"] == inc.incident_number
    assert published["payload"]["opened_reason"] == pir_service.REASON_HUB_CORE
    assert published["payload"]["pir_id"] == pir.id


# --------------------------------------------------------------------------
# The blameless validator (§7.7.3)
# --------------------------------------------------------------------------


def test_a_person_named_in_root_causes_is_a_422_with_the_spec_message(client, on):
    inc_id, pir_id = _seed_via_api()
    response = client.patch(
        f"/api/v1/pir/{pir_id}",
        json={"root_causes": "Kevin Ochieng did not escalate the low-fuel alarm"},
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == (
        "describe what the system allowed, not who did it — use RNIO / FE / MSP_POWER"
    )
    assert inc_id


def test_the_rejection_never_echoes_the_name_back(client, on):
    """Answering "your root cause names Kevin Ochieng" would be the disclosure this lane is
    built to avoid — and it would put the name in every proxy log on the way home."""
    _inc_id, pir_id = _seed_via_api()
    response = client.patch(f"/api/v1/pir/{pir_id}", json={"root_causes": "Kevin Ochieng missed it"})
    assert "Kevin" not in response.text and "Ochieng" not in response.text


def test_a_first_name_alone_is_caught_because_the_scrubber_matches_name_parts(client, on):
    """Reuse of ``NameMap`` is what buys this: "Kevin" is registered as an alias of the
    assignee "Kevin Ochieng", so the defensive half-name does not slip through."""
    _inc_id, pir_id = _seed_via_api()
    response = client.patch(f"/api/v1/pir/{pir_id}", json={"contributing_factors": "Kevin was on a double shift"})
    assert response.status_code == 422


def test_the_wording_the_error_message_recommends_is_accepted(client, on):
    """The regression that would quietly break the whole rule: this incident's ``rnio_name``
    is literally "RNIO", and NameMap registers any four-letter name part as an alias — so
    without the role-token exclusion the validator would reject its own suggested remedy."""
    _inc_id, pir_id = _seed_via_api()
    response = client.patch(
        f"/api/v1/pir/{pir_id}",
        json={
            "root_causes": "The low-fuel alarm was suppressed, so RNIO and MSP_POWER had no signal to act on",
            "contributing_factors": "FE dispatch had no fuel-level data for the site",
        },
    )
    assert response.status_code == 200, response.text
    assert "RNIO" in response.json()["root_causes"]


def test_the_msp_root_cause_is_not_prefilled_when_it_names_someone(tmp_db, on):
    """Vendor free text routinely names the technician who attended. Copying it into
    ``root_causes`` would put a name into the one field this lane keeps clean, through a
    path the PATCH validator never sees — so the prefill declines and a human writes it."""
    _settings, session = tmp_db
    inc = _incident(session, msp_root_cause="Kevin Ochieng left the fuel cap off")
    session.commit()

    pir, _ = pir_service.open_pir(session, inc, reason=pir_service.REASON_P1_P2)
    session.commit()
    assert pir.root_causes is None


def test_a_clean_msp_root_cause_is_prefilled(tmp_db, on):
    _settings, session = tmp_db
    inc = _incident(session, msp_root_cause="Generator fuel line blocked by sediment")
    session.commit()

    pir, _ = pir_service.open_pir(session, inc, reason=pir_service.REASON_P1_P2)
    session.commit()
    assert pir.root_causes == "Generator fuel line blocked by sediment"


def _cap_row(session, *, external_id: str, identifier: str, fetched_at, valid_from, valid_until, region="NBI"):
    """One ``external_signals`` row shaped as ``pollers/kmd_cap.py`` writes a CAP alert."""
    session.add(
        ExternalSignalRow(
            id=new_id(), operator_id="safaricom", source="KMD_CAP", source_url="https://meteo.go.ke/x.xml",
            region_code=region, fetched_at=fetched_at, valid_from=valid_from, valid_until=valid_until,
            stale=0, confidence=1.0, storm_flag=1, flood_flag=0, planned_power=0, access_risk=0,
            payload_json="{}",
            derived_json=json.dumps({"kind": "cap_alert", "identifier": identifier}, separators=(",", ":")),
            external_id=external_id, created_at=fetched_at,
        )
    )


def test_the_timeline_lists_one_cap_alert_once_and_never_before_its_span_began(tmp_db, on):
    """A KMD alert re-attributed to a region opens a NEW row from the moment of attribution
    (pollers/kmd_cap.reattribute_live), so one alert can hold several rows. The timeline selected
    on valid_from alone, so a row stored two hours AFTER the failure was listed as "active at
    failure time" — the same alert twice, and the region shown as warned during exactly the window
    in which its county was mapped elsewhere (review finding PIR-SPAN).

    The rows here carry the ``valid_from`` the writer used to copy from the alert (its effective
    time), because that is what a database written before the writer was fixed still holds: the
    reader has to be right about those too."""
    _settings, session = tmp_db
    inc = _incident(session)
    at = inc.failure_time
    effective = at - timedelta(minutes=25)
    # In force at the failure, and known then.
    _cap_row(session, external_id="a1#NBI", identifier="a1", fetched_at=at - timedelta(minutes=20),
             valid_from=effective, valid_until=at + timedelta(hours=2))
    # A second span of the SAME alert, also known before the failure: one alert, one entry.
    _cap_row(session, external_id="a1#NBI@20260916T005500Z", identifier="a1", fetched_at=at - timedelta(minutes=5),
             valid_from=effective, valid_until=at + timedelta(hours=6))
    # A span attributed two hours AFTER the failure, carrying the alert's effective time: not
    # evidence at failure time, however early its valid_from claims to start.
    _cap_row(session, external_id="a1#NBI@20260916T030000Z", identifier="a1", fetched_at=at + timedelta(hours=2),
             valid_from=effective, valid_until=at + timedelta(hours=6))
    # A different alert, in force and known at the failure: its own entry.
    _cap_row(session, external_id="b1#NBI", identifier="b1", fetched_at=at - timedelta(minutes=10),
             valid_from=at - timedelta(minutes=10), valid_until=at + timedelta(hours=1))
    session.commit()

    timeline = pir_service.assemble_timeline(session, inc)
    cap_entries = [e for e in timeline if e["kind"] == "signal" and "KMD_CAP" in e["title"]]
    assert [e["ts"] for e in cap_entries] == [
        (at - timedelta(minutes=20)).replace(microsecond=0).isoformat() + "Z",
        (at - timedelta(minutes=10)).replace(microsecond=0).isoformat() + "Z",
    ]
    assert {json.loads(e["detail"])["identifier"] for e in cap_entries} == {"a1", "b1"}


def test_the_timeline_says_nothing_of_an_alert_attributed_only_after_the_failure(tmp_db, on):
    """The gap case: while the county belonged to another region, this region was not warned —
    even though the span the poller later opened carries the alert's earlier effective time."""
    _settings, session = tmp_db
    inc = _incident(session)
    at = inc.failure_time
    _cap_row(session, external_id="a1#NBI@20260916T030000Z", identifier="a1", fetched_at=at + timedelta(hours=2),
             valid_from=at - timedelta(minutes=25), valid_until=at + timedelta(hours=6))
    session.commit()
    timeline = pir_service.assemble_timeline(session, inc)
    assert [e for e in timeline if e["kind"] == "signal"] == []


def test_the_vendors_own_words_stay_on_the_timeline_even_when_they_name_someone(tmp_db, on):
    """The blameless rule governs the review's *analysis*, not the evidence it cites.
    Redacting a work note inside a postmortem falsifies the record it exists to preserve."""
    _settings, session = tmp_db
    inc = _incident(session)
    _populate_history(session, inc)
    session.commit()

    timeline = pir_service.assemble_timeline(session, inc)
    notes = [e for e in timeline if e["kind"] == "work_note"]
    assert any("dispatching" in e["detail"] for e in notes)


def test_an_owner_token_that_is_a_persons_name_is_refused(client, on):
    """§7.7.6: role tokens, not names. An action outlives whoever is on shift today."""
    _inc_id, pir_id = _seed_via_api()
    response = client.post(
        f"/api/v1/pir/{pir_id}/actions",
        json={
            "type": "prevent",
            "priority": "P1",
            "description": "Add a fuel-level telemetry check",
            "owner_token": "Kevin Ochieng",
            "due_date": "2026-10-30",
        },
    )
    assert response.status_code == 422
    assert "role token" in response.json()["detail"]


def test_an_unknown_action_type_or_priority_is_refused(client, on):
    _inc_id, pir_id = _seed_via_api()
    base = {
        "type": "prevent",
        "priority": "P1",
        "description": "x",
        "owner_token": "MSP_POWER",
        "due_date": "2026-10-30",
    }
    assert client.post(f"/api/v1/pir/{pir_id}/actions", json={**base, "type": "fix"}).status_code == 422
    assert client.post(f"/api/v1/pir/{pir_id}/actions", json={**base, "priority": "P9"}).status_code == 422


# --------------------------------------------------------------------------
# Action-item transitions (PATCH /pir/{id}/actions/{action_id})
#
# This route is NOT in §7.7.2's list. It exists because the §7.7.1 DDL ships
# status OPEN | IN_PROGRESS | DONE | WONT_DO together with a closed_at column,
# and a vocabulary with three terminal states that nothing can reach is an
# omission rather than a restriction: "an action item can be created but never
# closed" makes the lane inert, and the "≥ 1 P0/P1 action" publish gate becomes
# a promise the product cannot keep. These tests pin the deviation so it stays a
# reasoned one — in particular that closed_at tracks the *transition* and not the
# last write.
# --------------------------------------------------------------------------


def _create_action(client: TestClient, pir_id: str, **overrides) -> dict:
    body = {
        "type": "prevent",
        "priority": "P1",
        "description": "Alarm the generator fuel level and route it to MSP_POWER",
        "owner_token": "MSP_POWER",
        "due_date": "2026-10-30",
    }
    body.update(overrides)
    response = client.post(f"/api/v1/pir/{pir_id}/actions", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_a_new_action_item_starts_open_with_no_closing_date(client, on):
    _inc_id, pir_id = _seed_via_api()
    action = _create_action(client, pir_id)
    assert action["status"] == "OPEN"
    assert action["closed_at"] is None


def test_completing_an_action_item_stamps_closed_at(client, on):
    _inc_id, pir_id = _seed_via_api()
    action = _create_action(client, pir_id)
    response = client.patch(f"/api/v1/pir/{pir_id}/actions/{action['id']}", json={"status": "DONE"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "DONE"
    assert response.json()["closed_at"] is not None


def test_wont_do_closes_an_action_item_just_as_done_does(client, on):
    """A decision not to act is a recorded decision, not a backlog item that rots — so the
    follow-up rate has to count it as closed to be honest."""
    _inc_id, pir_id = _seed_via_api()
    action = _create_action(client, pir_id)
    body = client.patch(f"/api/v1/pir/{pir_id}/actions/{action['id']}", json={"status": "WONT_DO"}).json()
    assert body["status"] == "WONT_DO"
    assert body["closed_at"] is not None


def test_in_progress_is_not_terminal_and_stamps_nothing(client, on):
    _inc_id, pir_id = _seed_via_api()
    action = _create_action(client, pir_id)
    body = client.patch(f"/api/v1/pir/{pir_id}/actions/{action['id']}", json={"status": "IN_PROGRESS"}).json()
    assert body["status"] == "IN_PROGRESS"
    assert body["closed_at"] is None


def test_reopening_an_action_item_clears_its_closing_date(client, on):
    """A stale closed_at on something being worked again is how a follow-up report counts an
    item as closed while an engineer is still on it."""
    _inc_id, pir_id = _seed_via_api()
    action = _create_action(client, pir_id)
    client.patch(f"/api/v1/pir/{pir_id}/actions/{action['id']}", json={"status": "DONE"})
    body = client.patch(f"/api/v1/pir/{pir_id}/actions/{action['id']}", json={"status": "IN_PROGRESS"}).json()
    assert body["status"] == "IN_PROGRESS"
    assert body["closed_at"] is None


def test_moving_between_two_terminal_states_keeps_the_original_closing_date(client, on):
    """Work stopped when it stopped. Reclassifying *why* it stopped does not move that moment."""
    _inc_id, pir_id = _seed_via_api()
    action = _create_action(client, pir_id)
    first = client.patch(f"/api/v1/pir/{pir_id}/actions/{action['id']}", json={"status": "DONE"}).json()
    second = client.patch(f"/api/v1/pir/{pir_id}/actions/{action['id']}", json={"status": "WONT_DO"}).json()
    assert second["status"] == "WONT_DO"
    assert second["closed_at"] == first["closed_at"]


def test_editing_a_closed_action_item_does_not_re_date_its_closure(client, on):
    """The rule is "when and only when the status becomes terminal" — a due-date slip or a
    typo fix must leave the closing date exactly where it was."""
    _inc_id, pir_id = _seed_via_api()
    action = _create_action(client, pir_id)
    closed = client.patch(f"/api/v1/pir/{pir_id}/actions/{action['id']}", json={"status": "DONE"}).json()
    edited = client.patch(
        f"/api/v1/pir/{pir_id}/actions/{action['id']}",
        json={"description": "Alarm the generator fuel level (telemetry, not a manual check)"},
    ).json()
    assert edited["closed_at"] == closed["closed_at"]
    assert edited["status"] == "DONE"
    assert "telemetry" in edited["description"]


def test_the_due_date_and_owner_can_be_corrected(client, on):
    _inc_id, pir_id = _seed_via_api()
    action = _create_action(client, pir_id)
    body = client.patch(
        f"/api/v1/pir/{pir_id}/actions/{action['id']}",
        json={"due_date": "2026-12-15", "owner_token": "FE_CENTRAL", "priority": "P0", "tracking_ref": "NOC-4412"},
    ).json()
    assert body["due_date"] == "2026-12-15"
    assert body["owner_token"] == "FE_CENTRAL"
    assert body["priority"] == "P0"
    assert body["tracking_ref"] == "NOC-4412"


def test_the_patch_route_enforces_the_same_vocabularies_as_create(client, on):
    """A vocabulary enforced on POST and not on PATCH is enforced nowhere: the second
    request is the easy way round the first."""
    _inc_id, pir_id = _seed_via_api()
    action = _create_action(client, pir_id)
    path = f"/api/v1/pir/{pir_id}/actions/{action['id']}"
    assert client.patch(path, json={"status": "CLOSED"}).status_code == 422
    assert client.patch(path, json={"type": "fix"}).status_code == 422
    assert client.patch(path, json={"priority": "P9"}).status_code == 422


def test_a_required_field_cannot_be_cleared_to_null(client, on):
    """An explicit null on a NOT NULL column is a bad request, not a server error: without
    this it reaches the commit and comes back as a 500 that says nothing useful."""
    _inc_id, pir_id = _seed_via_api()
    action = _create_action(client, pir_id)
    path = f"/api/v1/pir/{pir_id}/actions/{action['id']}"
    for field in ("type", "priority", "description", "owner_token", "due_date", "status"):
        response = client.patch(path, json={field: None})
        assert response.status_code == 422, f"{field} cleared to null answered {response.status_code}"
        assert "cannot be cleared" in response.json()["detail"]


def test_the_patch_route_refuses_an_owner_token_that_is_a_persons_name(client, on):
    _inc_id, pir_id = _seed_via_api()
    action = _create_action(client, pir_id)
    response = client.patch(
        f"/api/v1/pir/{pir_id}/actions/{action['id']}", json={"owner_token": "Kevin Ochieng"}
    )
    assert response.status_code == 422
    assert "role token" in response.json()["detail"]


def test_an_action_item_belonging_to_a_different_review_is_a_404(client, on):
    """The ``pir_id`` predicate, not the id alone: an action item is reachable only through
    the review it hangs off, even within one operator."""
    _inc_id_a, pir_a = _seed_via_api()
    action = _create_action(client, pir_a)
    session = _api_session()
    try:
        other = _incident(session, incident_number="INC000099", correlation_fingerprint="fp-99")
        other_pir, _ = pir_service.open_pir(session, other, reason=pir_service.REASON_MANUAL)
        session.commit()
        pir_b = other_pir.id
    finally:
        session.close()

    assert client.patch(f"/api/v1/pir/{pir_b}/actions/{action['id']}", json={"status": "DONE"}).status_code == 404
    assert client.patch(f"/api/v1/pir/{pir_a}/actions/does-not-exist", json={"status": "DONE"}).status_code == 404


def test_another_operators_action_item_is_a_404(client, on):
    """Through the parent, as everywhere else in this lane: the foreign review 404s first, so
    the child is never reached and its existence is never confirmed."""
    session = _api_session()
    try:
        inc = _incident(session, operator_id="othertel", incident_number="OTH000002", correlation_fingerprint="fp-oth")
        foreign = PostIncidentReviewRow(
            operator_id="othertel", incident_id=inc.id, status="DRAFT", opened_reason="MANUAL"
        )
        session.add(foreign)
        session.flush()
        action = PirActionItemRow(
            pir_id=foreign.id,
            type="prevent",
            priority="P0",
            description="their action",
            owner_token="MSP_POWER",
            due_date=date(2026, 10, 30),
        )
        session.add(action)
        session.commit()
        foreign_pir_id, foreign_action_id = foreign.id, action.id
    finally:
        session.close()

    response = client.patch(
        f"/api/v1/pir/{foreign_pir_id}/actions/{foreign_action_id}", json={"status": "WONT_DO"}
    )
    assert response.status_code == 404

    session = _api_session()
    try:
        untouched = session.get(PirActionItemRow, foreign_action_id)
        assert untouched.status == "OPEN" and untouched.closed_at is None
    finally:
        session.close()


def test_the_transition_rule_is_a_pure_function_over_the_row():
    """``transition_action`` is in the service, not the route, so the rule can be read and
    tested without a client — and so a second caller cannot reimplement it differently."""
    action = PirActionItemRow(
        pir_id="p", type="prevent", priority="P1", description="d", owner_token="MSP_POWER",
        due_date=date(2026, 10, 30), status="OPEN",
    )
    pir_service.transition_action(action, "DONE", now=T0)
    assert (action.status, action.closed_at) == ("DONE", T0)

    pir_service.transition_action(action, "WONT_DO", now=T0 + timedelta(days=1))
    assert action.closed_at == T0, "terminal to terminal must not move the closing date"

    pir_service.transition_action(action, "OPEN", now=T0 + timedelta(days=2))
    assert (action.status, action.closed_at) == ("OPEN", None)

    pir_service.transition_action(action, "OPEN", now=T0 + timedelta(days=3))
    assert action.closed_at is None, "a no-op transition changes nothing"


def test_closing_every_action_does_not_unpublish_or_reopen_the_review(client, on):
    """Action items keep moving after the review is signed — that is the point of them. The
    review itself stays PUBLISHED and immutable."""
    _inc_id, pir_id = _seed_via_api()
    action = _create_action(client, pir_id, priority="P0")
    assert client.post(f"/api/v1/pir/{pir_id}/publish", json={"reviewer": "Grace Wanjiru"}).status_code == 200

    assert client.patch(f"/api/v1/pir/{pir_id}/actions/{action['id']}", json={"status": "DONE"}).status_code == 200
    body = client.get(f"/api/v1/pir/{pir_id}").json()
    assert body["status"] == "PUBLISHED"
    assert body["actions"][0]["status"] == "DONE"
    assert client.patch(f"/api/v1/pir/{pir_id}", json={"summary": "x"}).status_code == 409


# --------------------------------------------------------------------------
# Publish rules (§7.7.2)
# --------------------------------------------------------------------------


def _add_action(client: TestClient, pir_id: str, priority: str = "P1") -> None:
    response = client.post(
        f"/api/v1/pir/{pir_id}/actions",
        json={
            "type": "prevent",
            "priority": priority,
            "description": "Alarm the generator fuel level and route it to MSP_POWER",
            "owner_token": "MSP_POWER",
            "due_date": "2026-10-30",
        },
    )
    assert response.status_code == 200, response.text


def test_publishing_without_a_named_reviewer_is_a_422(client, on):
    """The demo role switcher's default display name is "NOC Analyst" — a chair, not a
    person — so "non-empty" would make this rule unreachable exactly where it matters."""
    _inc_id, pir_id = _seed_via_api()
    _add_action(client, pir_id)
    response = client.post(f"/api/v1/pir/{pir_id}/publish", json={})
    assert response.status_code == 422, response.text
    assert "named reviewer" in response.json()["detail"]


def test_a_user_affecting_outage_cannot_be_published_without_a_p0_or_p1_action(client, on):
    """Straight from the SRE workbook: an outage that affected users and produced no urgent
    action produced no learning."""
    _inc_id, pir_id = _seed_via_api()
    _add_action(client, pir_id, priority="P3")
    response = client.post(f"/api/v1/pir/{pir_id}/publish", json={"reviewer": "Grace Wanjiru"})
    assert response.status_code == 422
    assert "P0 or P1" in response.json()["detail"]


def test_publish_succeeds_with_a_named_reviewer_and_a_p1_action(client, on):
    _inc_id, pir_id = _seed_via_api()
    _add_action(client, pir_id, priority="P1")
    response = client.post(
        f"/api/v1/pir/{pir_id}/publish", json={"reviewer": "Grace Wanjiru", "rationale": "reviewed at handover"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "PUBLISHED"
    assert body["reviewer"] == "Grace Wanjiru"
    assert body["published_at"] and body["reviewed_at"]


def test_an_outage_that_affected_nobody_may_publish_without_an_urgent_action(client, on):
    """The rule is conditioned on ``impact.users_affected > 0``. A maintenance-window blip
    that reached no subscriber does not need a P0 to be worth writing down."""
    _inc_id, pir_id = _seed_via_api(users_affected=0)
    response = client.post(f"/api/v1/pir/{pir_id}/publish", json={"reviewer": "Grace Wanjiru"})
    assert response.status_code == 200, response.text


def test_republishing_is_a_409_and_a_published_review_is_immutable(client, on):
    _inc_id, pir_id = _seed_via_api(users_affected=0)
    assert client.post(f"/api/v1/pir/{pir_id}/publish", json={"reviewer": "Grace Wanjiru"}).status_code == 200
    assert client.post(f"/api/v1/pir/{pir_id}/publish", json={"reviewer": "Grace Wanjiru"}).status_code == 409
    # Editing it in place would rewrite, under that reviewer's name, what they signed.
    assert client.patch(f"/api/v1/pir/{pir_id}", json={"summary": "actually..."}).status_code == 409


def test_publish_cannot_be_reached_through_the_patch_route(client, on):
    _inc_id, pir_id = _seed_via_api()
    response = client.patch(f"/api/v1/pir/{pir_id}", json={"status": "PUBLISHED"})
    assert response.status_code == 422
    assert "publish through" in response.json()["detail"]


def test_a_not_required_review_cannot_be_published(tmp_db, on):
    _settings, session = tmp_db
    inc = _incident(session, status="CANCELLED", users_affected=0)
    session.commit()
    pir, _ = pir_service.open_pir(session, inc, reason=pir_service.REASON_MANUAL)
    session.commit()

    blockers = pir_service.publish_blockers(session, pir, reviewer="Grace Wanjiru")
    assert any("NOT_REQUIRED" in b for b in blockers)


def test_every_unmet_precondition_is_reported_at_once(tmp_db, on):
    """Three round-trips to discover three problems is how a reviewer stops publishing."""
    _settings, session = tmp_db
    inc = _incident(session)
    session.commit()
    pir, _ = pir_service.open_pir(session, inc, reason=pir_service.REASON_P1_P2)
    session.commit()

    blockers = pir_service.publish_blockers(session, pir, reviewer="shift_supervisor")
    assert len(blockers) == 2  # role label is not a name + no P0/P1 action for 5000 users


def test_the_awaiting_review_counter_counts_unfinished_learning_only(client, on):
    """DRAFT and IN_REVIEW count; a published review and a NOT_REQUIRED one do not. The
    number answers "how much unfinished learning are we carrying?", and a review that was
    ruled unnecessary is a decision, not a backlog item."""
    _inc_id, pir_id = _seed_via_api(users_affected=0)
    assert client.get("/api/v1/pir/awaiting-review").json()["awaiting_review"] == 1

    assert client.patch(f"/api/v1/pir/{pir_id}", json={"status": "IN_REVIEW"}).status_code == 200
    assert client.get("/api/v1/pir/awaiting-review").json()["awaiting_review"] == 1

    assert client.post(f"/api/v1/pir/{pir_id}/publish", json={"reviewer": "Grace Wanjiru"}).status_code == 200
    assert client.get("/api/v1/pir/awaiting-review").json()["awaiting_review"] == 0


def test_the_counter_route_is_not_swallowed_by_the_pir_id_route(client, on):
    """Registration order, pinned: ``/pir/awaiting-review`` must not be read as a review id."""
    response = client.get("/api/v1/pir/awaiting-review")
    assert response.status_code == 200
    assert "awaiting_review" in response.json()


def test_a_role_label_is_not_a_named_reviewer():
    assert pir_service.is_named_reviewer("Grace Wanjiru") is True
    assert pir_service.is_named_reviewer("") is False
    assert pir_service.is_named_reviewer("   ") is False
    assert pir_service.is_named_reviewer("NOC Analyst") is False
    assert pir_service.is_named_reviewer("shift_supervisor") is False
    assert pir_service.is_named_reviewer("RNIO") is False


# --------------------------------------------------------------------------
# The optional LLM draft (§7.7.2, §7.7.3)
# --------------------------------------------------------------------------


def test_the_llm_draft_becomes_a_redacted_outbox_row_and_never_a_call(client, on):
    """Out of band by construction: the route writes a row and returns. Nothing may hold a
    SQLite write lock across a model call, and nothing may call a model inside a request."""
    _inc_id, pir_id = _seed_via_api()
    response = client.post(f"/api/v1/pir/{pir_id}/draft/llm")
    assert response.status_code == 200, response.text
    assert response.json()["queued"] is True
    assert response.json()["ai_assisted"] == 1

    session = _api_session()
    try:
        row = session.scalar(select(OutboxRow).where(OutboxRow.kind == "LLM_CALL"))
        assert row is not None
        payload = json.loads(row.payload_json)
        assert payload["model"] == "claude-opus-5"  # §5.3.18: opus or local, never Fable
        assert payload["fields"] == ["summary", "root_causes", "went_well", "went_poorly", "got_lucky"]
        # Redacted before it is stored, so what would later be transmitted carries tokens.
        serialised = json.dumps(payload)
        assert "Kevin" not in serialised and "Ochieng" not in serialised
        assert "<PERSON_" in serialised
        # The token -> name map is the re-identification key and must not be stored beside
        # the pseudonymised payload, or the pseudonymisation is undone in one table.
        assert "token_to_name" not in serialised
    finally:
        session.close()


def test_queueing_the_draft_twice_queues_one_draft(client, on):
    _inc_id, pir_id = _seed_via_api()
    first = client.post(f"/api/v1/pir/{pir_id}/draft/llm").json()
    second = client.post(f"/api/v1/pir/{pir_id}/draft/llm").json()
    assert first["queued"] is True
    assert second["queued"] is False and second["already_queued"] is True
    assert first["outbox_id"] == second["outbox_id"]


def test_ai_assisted_is_stamped_when_the_draft_is_queued_not_when_it_returns(client, on):
    """A disclosure flag belongs on the conservative side: over-declaring costs nothing,
    while declaring only on success under-declares the moment a crash lands in the gap."""
    _inc_id, pir_id = _seed_via_api()
    assert client.get(f"/api/v1/pir/{pir_id}").json()["ai_assisted"] == 0
    client.post(f"/api/v1/pir/{pir_id}/draft/llm")
    assert client.get(f"/api/v1/pir/{pir_id}").json()["ai_assisted"] == 1

    # And a named human still publishes it — assistance never becomes authorship.
    _add_action(client, pir_id, priority="P0")
    published = client.post(f"/api/v1/pir/{pir_id}/publish", json={"reviewer": "Grace Wanjiru"}).json()
    assert published["ai_assisted"] == 1
    assert published["reviewer"] == "Grace Wanjiru"


# --------------------------------------------------------------------------
# Operator scoping (§8)
# --------------------------------------------------------------------------


def test_another_operators_review_is_a_404_and_its_actions_are_unreachable(client, on):
    """404, not 403: a 403 would confirm the id exists in the other operator's data, which is
    the fact the scoping protects. The action items are reached only through the parent, so
    the parent's 404 is what keeps the children out."""
    session = _api_session()
    try:
        inc = _incident(session, operator_id="othertel", incident_number="OTH000001")
        foreign = PostIncidentReviewRow(
            operator_id="othertel", incident_id=inc.id, status="DRAFT", opened_reason="MANUAL"
        )
        session.add(foreign)
        session.flush()
        session.add(
            PirActionItemRow(
                pir_id=foreign.id,
                type="prevent",
                priority="P0",
                description="their action",
                owner_token="MSP_POWER",
                due_date=date(2026, 10, 30),
            )
        )
        session.commit()
        foreign_id = foreign.id
    finally:
        session.close()

    assert client.get(f"/api/v1/pir/{foreign_id}").status_code == 404
    assert client.get(f"/api/v1/pir/{foreign_id}/actions").status_code == 404
    assert client.patch(f"/api/v1/pir/{foreign_id}", json={"summary": "x"}).status_code == 404
    assert client.post(f"/api/v1/pir/{foreign_id}/publish", json={"reviewer": "Grace Wanjiru"}).status_code == 404
    # And the other operator's review never appears in the list either.
    assert all(row["id"] != foreign_id for row in client.get("/api/v1/pir").json())


def test_an_action_item_cannot_borrow_another_operators_problem(client, on):
    _inc_id, pir_id = _seed_via_api()
    session = _api_session()
    try:
        problem = ProblemRow(
            operator_id="othertel",
            problem_number="PRB000099",
            signature="OTH-SITE|POWER",
            site_id="OTH-SITE",
            region_code="NBI",
        )
        session.add(problem)
        session.commit()
        foreign_problem_id = problem.id
    finally:
        session.close()

    response = client.post(
        f"/api/v1/pir/{pir_id}/actions",
        json={
            "type": "prevent",
            "priority": "P1",
            "description": "x",
            "owner_token": "MSP_POWER",
            "due_date": "2026-10-30",
            "problem_id": foreign_problem_id,
        },
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------
# The firewall: PIR tables are never read by the scorecard / individual-metrics paths
# --------------------------------------------------------------------------

#: Every spelling of the PIR tables. A module that reads them has to name one of these.
PIR_IDENTIFIERS = (
    "post_incident_reviews",
    "pir_action_items",
    "PostIncidentReviewRow",
    "PirActionItemRow",
)
#: The lane itself, which is of course allowed to name them.
PIR_LANE_FILES = {
    SRC / "db" / "models_pir.py",
    SRC / "services" / "pir.py",
    SRC / "api" / "routers" / "pir.py",
}


def _executable_source(path: pathlib.Path) -> str:
    """``path``'s source with comments and docstrings removed.

    The rule being enforced is "no module outside the lane *reads* these tables", and prose
    is not a read: ``services/evidence.py`` cites ``pir_action_items`` in a docstring to
    explain that it scopes ``broadcasts`` the same way, which is a reference worth keeping,
    not a join. Other string literals are deliberately kept, so a module that reaches the
    tables through raw SQL (``text("SELECT ... FROM post_incident_reviews")``) is still
    caught — that is exactly the case a naive "strip every string" version would miss.
    """
    raw = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(raw)
    except SyntaxError:  # unparseable: fail loud on the raw text rather than silently pass
        return raw
    lines = raw.splitlines()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) or not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            for index in range(first.lineno - 1, (first.end_lineno or first.lineno)):
                lines[index] = ""
    blanked = "\n".join(lines)
    kept: list[str] = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(blanked).readline):
            if token.type != tokenize.COMMENT:
                kept.append(token.string)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return blanked
    return "\n".join(kept)


def test_no_module_outside_the_pir_lane_names_the_pir_tables():
    """The spec's grep test (§7.7.3, §5.3.17), widened from "the scorecard job" to the whole
    tree — which is both stricter and, while the scorecard lane is still being built, the
    only form that can fail today rather than in six months.

    The point is not tidiness. If a postmortem can reach a vendor's KPI or a person's file,
    engineers write defensive notes and the reviews stop being worth reading; §7.7 says so
    outright. Making the *reference* impossible is cheaper to enforce than auditing each
    join, and it survives a refactor that a hand-checked query does not.

    The lane stays usable through functions — ``services.pir.known_error_for_incident`` is
    what ENRICH calls, ``services.pir.PIR_JOB`` is what the scheduler registers — and neither
    of those names a table, so wiring this lane in does not make this test fail.
    """
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if path in PIR_LANE_FILES:
            continue
        code = _executable_source(path)
        hits = [identifier for identifier in PIR_IDENTIFIERS if identifier in code]
        if hits:
            offenders.append(f"{path.relative_to(SRC)}: {hits}")
    assert offenders == [], (
        "PIR tables referenced outside the PIR lane — §7.7.3 forbids the scorecard and "
        f"individual-metrics paths from reading them: {offenders}"
    )


def test_the_scorecard_and_individual_metrics_modules_do_not_import_the_pir_lane():
    """The same rule stated the way it will be read once those lanes land: whatever module
    ends up computing vendor KPIs or individual metrics must not import this one either.

    It is a superset of the table grep — an import is how a reference starts — and it is
    written to discover the modules by name rather than by a hard-coded path, so it starts
    protecting ``services/scorecard.py`` the moment that file exists.
    """
    suspects = [
        path
        for path in sorted(SRC.rglob("*.py"))
        if any(word in path.name.lower() for word in ("scorecard", "individual", "metric"))
    ]
    for path in suspects:
        text = _executable_source(path)
        assert "services.pir" not in text and "services import pir" not in text, (
            f"{path.relative_to(SRC)} imports the PIR lane; §5.3.17 keeps PIR content out of KPI inputs"
        )
        assert "models_pir" not in text, f"{path.relative_to(SRC)} imports the PIR models"


def test_no_scheduled_job_emits_sql_against_the_pir_tables(tmp_db, on):
    """The join half of the spec's "grep + join test", done against real SQL.

    Every card in ``scheduler.loop.SCHEDULED_JOBS`` except the PIR one is run with a cursor
    listener attached, and the statements they emit are checked for the PIR table names. It
    covers the outbox drain and the monitor today; when the scorecard and individual-metrics
    jobs are added to that tuple they are covered automatically, with no edit here — which is
    the property a hand-listed test would not have.
    """
    settings, session = tmp_db
    inc = _incident(session)
    _populate_history(session, inc)
    pir_service.open_pir(session, inc, reason=pir_service.REASON_P1_P2)
    session.commit()

    from noc_agents.scheduler.loop import SCHEDULED_JOBS

    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001, D401
        statements.append(statement)

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        for card in SCHEDULED_JOBS:
            if card.name == pir_service.JOB_NAME:
                continue
            card.fn(session, settings)
            session.commit()
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert statements, "the harness must actually have captured SQL, or it proves nothing"
    touched = [s for s in statements if "post_incident_reviews" in s or "pir_action_items" in s]
    assert touched == [], f"a scheduled job read the PIR tables: {touched[:3]}"


def test_the_pir_tables_have_no_foreign_key_into_the_vendor_or_metrics_schema():
    """Structural backstop for the same rule: nothing hangs off a PIR, so no future join
    reaches one by following a relationship someone added without thinking about §5.3.17."""
    for model in (PostIncidentReviewRow, PirActionItemRow):
        assert model.__table__.foreign_keys == set(), f"{model.__tablename__} grew a foreign key"
