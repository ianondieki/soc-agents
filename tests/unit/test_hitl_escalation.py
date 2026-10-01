"""The §6.5 HITL escalation ladder (CONFORMANCE B-01): what happens when nobody clicks.

Every test here starts from the real thing: a P1/P2 HUB alarm through ``process_event`` at
``L2_GUARDED``, which leaves an ``APPROVE_BROADCAST`` card PENDING and unclaimed with its
drafts ``PENDING_HITL``. Time is injected (``now=``) so the rungs are proved without waiting.

What is proved:

* the three rungs, in order and only once each: T+5 supervisor nudge (SMS + in-app rows),
  T+15 duty-manager nudge + ``hitl.escalated``, T+30 red mark (no rows, no event);
* idempotency across re-runs, restarts (fresh sessions) and two concurrent passes forced to
  read before either writes -- one row per task per rung per channel, one event, one audit
  row per rung, for ever;
* the ladder never releases: after every rung and a full outbox drain, the drafts are still
  ``PENDING_HITL``, the card is still PENDING and unclaimed, the run is still WAITING_HITL,
  no channel-kind outbox row exists and no ``email.*``/``broadcast.*`` event was published;
* the flag off does nothing (job summary names the flag; the database is byte-identical);
* no recipient configured: the SMS half is recorded SUPPRESSED with the reason, the in-app
  half still goes, nothing crashes and no address is invented;
* an incident-less card is off the ladder unless its producer stamped a priority, in which
  case ``hitl.escalated`` carries ``incident_number: null``;
* dispatch: the in-app nudge is a ``hitl.nudge`` event after the outcome commit, the SMS goes
  through the mock path, and a nudge dispatched after the card was decided is inert;
* the regulatory sweep notes a red card on the open CA 24-h notice, once, and sends nothing.
"""

from __future__ import annotations

import json
import threading
from datetime import timedelta

import pytest
from sqlalchemy import func, inspect, select, text, update

from noc_agents.config import HitlEscalationConfig, OperatorConfig, get_settings
from noc_agents.db.models import (
    AgentRunRow,
    AuditRow,
    BroadcastRow,
    HitlTaskRow,
    IncidentRow,
    OutboxRow,
    get_session,
    utcnow,
)
from noc_agents.domain.schemas import EventIngest
from noc_agents.graph.pipeline import process_event
from noc_agents.orchestrator import outbox
from noc_agents.orchestrator.contract import SUCCEEDED
from noc_agents.orchestrator.outbox import drain_once
from noc_agents.realtime.hub import hub
from noc_agents.scheduler.loop import SCHEDULED_JOBS, job_card, run_job, status_payload
from noc_agents.services import hitl_escalation as he
from noc_agents.services import regulatory
from noc_agents.services.gsm7 import is_gsm7
from noc_agents.services.templates import HITL_NUDGE_PARAMS, TemplateRegistry, load_seed_templates

HUB_EVENT = {
    "site_id": "SFC-NBIE-HUB-EMB",
    "site_name": "Embakasi East Aggregation HUB",
    "site_type": "HUB",
    "region_code": "NBI_E",
    "alarm_code": "POWER_GRID_FAIL",
    "failure_domain": "POWER",
    "users_affected": 450000,
}
SMALL_EVENT = {
    "site_id": "SFC-NBI-BTS-001",
    "site_name": "Kayole BTS 1",
    "site_type": "BTS",
    "region_code": "NBI_E",
    "alarm_code": "RACK_DOOR_OPEN",
    "failure_domain": "ENV",
    "users_affected": 1200,
}

MIN = timedelta(minutes=1)


# --------------------------------------------------------------------------------- fixtures


@pytest.fixture()
def on(monkeypatch):
    monkeypatch.setenv("HITL_ESCALATION_ENABLED", "true")


@pytest.fixture()
def clean_hub():
    hub._history.clear()
    yield
    hub._history.clear()


@pytest.fixture()
def gated(tmp_db):
    """A P1/P2 HUB incident held at the gate: ``(settings, incident id, task id, created_at)``."""
    settings, session = tmp_db
    inc = process_event(session, settings, EventIngest(**HUB_EVENT))
    task = session.scalar(select(HitlTaskRow).where(HitlTaskRow.incident_id == inc.id, HitlTaskRow.task_type == "APPROVE_BROADCAST"))
    assert task is not None and task.status == "PENDING" and task.claimed_at is None
    assert inc.priority in ("P1", "P2")
    inc_id, task_id, created = inc.id, task.id, task.created_at
    session.commit()
    session.close()
    return settings, inc_id, task_id, created


def _read(fn):
    s = get_session()
    try:
        return fn(s)
    finally:
        s.close()


def _task(task_id: str) -> dict:
    def read(s):
        t = s.get(HitlTaskRow, task_id)
        return {"status": t.status, "claimed_by": t.claimed_by, "claimed_at": t.claimed_at, "payload": t.proposed_payload}

    return _read(read)


def _nudge_rows(task_id: str | None = None) -> list[OutboxRow]:
    def read(s):
        stmt = select(OutboxRow).where(OutboxRow.kind == outbox.HITL_NUDGE)
        if task_id:
            stmt = stmt.where(OutboxRow.hitl_task_id == task_id)
        rows = s.scalars(stmt.order_by(OutboxRow.idempotency_key)).all()
        s.expunge_all()
        return rows

    return _read(read)


def _events(kind: str) -> list[dict]:
    return [e for e in hub._history if e["type"] == kind]


def _audits(action: str, entity_id: str | None = None) -> list[AuditRow]:
    def read(s):
        stmt = select(AuditRow).where(AuditRow.action == action)
        if entity_id:
            stmt = stmt.where(AuditRow.entity_id == entity_id)
        rows = s.scalars(stmt).all()
        s.expunge_all()
        return rows

    return _read(read)


def _run(settings, now):
    """One pass on a fresh session -- what a restarted process does."""
    s = get_session()
    try:
        return he.escalate_once(s, settings.operator, now=now)
    finally:
        s.close()


RUN_BOOKKEEPING = frozenset({"agent_runs", "agent_run_steps", "scheduled_job_state"})


def _snapshot() -> dict:
    """Every row of every table (less the run's own bookkeeping), for "nothing moved" proofs."""
    s = get_session()
    try:
        out = {}
        for table in sorted(inspect(s.get_bind()).get_table_names()):
            if table in RUN_BOOKKEEPING:
                continue
            sql = f'SELECT * FROM "{table}"'
            if table == "audit_events":
                sql += " WHERE action NOT LIKE 'step.%'"
            out[table] = sorted(tuple(repr(v) for v in row) for row in s.execute(text(sql)).all())
        return out
    finally:
        s.close()


def _external_state(inc_id: str) -> dict:
    """Everything the ladder must never move: drafts, the card, the run, channel rows."""

    def read(s):
        drafts = sorted(b.status for b in s.scalars(select(BroadcastRow).where(BroadcastRow.incident_id == inc_id)))
        tasks = sorted((t.task_type, t.status, t.claimed_by) for t in s.scalars(select(HitlTaskRow).where(HitlTaskRow.incident_id == inc_id)))
        run = s.scalar(select(AgentRunRow).where(AgentRunRow.incident_id == inc_id, AgentRunRow.graph_name == "incident_lifecycle"))
        channel_rows = s.scalar(select(func.count()).select_from(OutboxRow).where(OutboxRow.kind.in_(sorted(outbox.CHANNEL_KINDS)))) or 0
        inc = s.get(IncidentRow, inc_id)
        return {
            "drafts": drafts,
            "tasks": tasks,
            "run": (run.status, [st.status for st in run.steps]),
            "channel_rows": channel_rows,
            "hitl": (inc.requires_hitl, inc.hitl_state, inc.status, inc.priority),
        }

    return _read(read)


# ---------------------------------------------------------------------- config and defaults


def test_config_defaults_equal_the_spec_and_the_yaml_block_is_read():
    esc = HitlEscalationConfig()
    assert (esc.supervisor_minutes, esc.duty_manager_minutes, esc.wallboard_minutes) == (5, 15, 30)
    assert esc.priorities == ["P1", "P2"] and esc.channels == ["SMS", "INAPP"]
    # A profile that says nothing gets the spec.
    minimal = OperatorConfig.model_validate(
        {
            "operator_id": "x", "display_name": "X", "incident_prefix": "INC", "problem_prefix": "PRB",
            "priority_thresholds": {}, "site_type_priority_floor": {}, "sla_minutes": {}, "regions": {}, "shifts": {},
        }
    )
    assert [(r.minutes, r.target) for r in he.ladder(minimal)] == [(5, "supervisor"), (15, "duty_manager"), (30, "wallboard")]
    # Both shipped profiles carry the block (B-13: it is typed, so it is no longer dropped) and
    # both recipient refs, empty on purpose.
    for profile in ("safaricom", "airtel"):
        cfg = get_settings(profile).operator
        assert cfg.hitl.escalation.supervisor_minutes == 5
        assert cfg.notification_recipients["hitl.recipients.supervisor"] == []
        assert cfg.notification_recipients["hitl.recipients.duty_manager"] == []
    # A ladder that does not ascend, or an external channel, is refused at load time.
    with pytest.raises(ValueError, match="ascend"):
        HitlEscalationConfig(supervisor_minutes=15, duty_manager_minutes=5)
    with pytest.raises(ValueError, match="SMS and INAPP only"):
        HitlEscalationConfig(channels=["EMAIL"])


def test_the_nudge_template_is_seeded_approved_by_policy_with_the_card_vocabulary(tmp_db):
    settings, session = tmp_db
    seeds, _ = load_seed_templates(settings.operator.operator_id)
    nudges = {(s.channel, s.language): s for s in seeds if s.template_key == "hitl_nudge"}
    assert set(nudges) == {("SMS", "en"), ("INAPP", "en")}
    for seed in nudges.values():
        assert seed.approval_status == "APPROVED" and seed.approved_by == "policy:hitl_escalation"
        assert {name for name, _ in seed.params} == set(HITL_NUDGE_PARAMS)
    reg = TemplateRegistry.for_config(session, settings.operator)
    reg.sync()
    session.commit()
    assert reg.for_send("SMS", "hitl_nudge").ok and reg.for_send("INAPP", "hitl_nudge").ok


def test_the_job_card_is_registered_off_by_default_and_rechecks_its_flag(tmp_db, monkeypatch):
    settings, session = tmp_db
    card = job_card("hitl_escalation")
    assert card is not None and card in SCHEDULED_JOBS
    assert card.default_enabled is False and card.enabled_env == "HITL_ESCALATION_ENABLED"
    assert card.agent == "SupervisorAgent" and card.graph_name == "hitl_escalation"
    monkeypatch.delenv("HITL_ESCALATION_ENABLED", raising=False)
    assert next(j for j in status_payload(session)["jobs"] if j["name"] == "hitl_escalation")["enabled"] is False
    # The manual route's path: run_job calls the function directly; it must say off, by name.
    monkeypatch.setenv("HITL_ESCALATION_ENABLED", "false")
    out = run_job(card, settings, reset_circuit=True)
    assert (out.status, out.summary) == (SUCCEEDED, "hitl_escalation skipped: HITL_ESCALATION_ENABLED is off")


# ------------------------------------------------------------------------------- the rungs


def test_flag_off_the_ladder_writes_nothing_even_with_a_card_forty_minutes_old(gated, monkeypatch, clean_hub):
    settings, inc_id, task_id, created = gated
    monkeypatch.setenv("HITL_ESCALATION_ENABLED", "false")
    before = _snapshot()
    report = _run(settings, created + 40 * MIN)
    assert report.enabled is False
    out = run_job(job_card("hitl_escalation"), settings, reset_circuit=True)
    assert out.status == SUCCEEDED and "HITL_ESCALATION_ENABLED" in out.summary
    assert _snapshot() == before
    assert _events("hitl.escalated") == [] and _nudge_rows() == []


def test_rung_5_nudges_the_supervisor_and_nothing_else_moves(gated, on, clean_hub):
    settings, inc_id, task_id, created = gated
    external = _external_state(inc_id)
    assert external["drafts"] and set(external["drafts"]) == {"PENDING_HITL"}

    assert _run(settings, created + 4 * MIN).escalated == []  # not yet
    report = _run(settings, created + 6 * MIN)

    assert [e["rungs"] for e in report.escalated] == [[5]]
    assert (report.nudges_queued, report.nudges_suppressed, report.events, report.lost) == (1, 1, 0, 0)
    rows = _nudge_rows(task_id)
    assert [r.idempotency_key for r in rows] == [f"HITL_NUDGE:{task_id}:5:INAPP", f"HITL_NUDGE:{task_id}:5:SMS"]
    by_ch = {json.loads(r.payload_json)["channel"]: r for r in rows}
    # The in-app half is queued; the SMS half is written and closed SUPPRESSED: no recipient.
    assert by_ch["INAPP"].status == "PENDING"
    assert by_ch["SMS"].status == "SUPPRESSED" and "hitl.recipients.supervisor" in by_ch["SMS"].last_error
    for r in rows:
        assert r.approved_by == "policy:hitl_escalation" and int(r.requires_hitl) == 0
        p = json.loads(r.payload_json)
        assert p["audience"] == "NOC_SHIFT" and p["escalation_target"] == "supervisor" and p["rung_minutes"] == 5
        assert p["template_key"] == "hitl_nudge" and p["template_version"] == "1"
        assert "@" not in json.dumps(p) and "+254" not in json.dumps(p)  # refs, never addresses
    mark = he.escalation_state(_task(task_id)["payload"])
    assert set(mark["rungs"]) == {"5"} and mark["level_minutes"] == 5 and mark["wallboard_red"] is False
    assert mark["rungs"]["5"]["target"] == "supervisor" and mark["rungs"]["5"]["recipients_configured"] == 0
    assert _events("hitl.escalated") == []
    assert len(_audits("hitl.escalation.nudged", task_id)) == 1
    assert _external_state(inc_id) == external


def test_rung_15_nudges_the_duty_manager_and_publishes_hitl_escalated_once(gated, on, clean_hub):
    settings, inc_id, task_id, created = gated
    _run(settings, created + 6 * MIN)
    report = _run(settings, created + 16 * MIN)

    assert [e["rungs"] for e in report.escalated] == [[15]] and report.events == 1
    keys = sorted(r.idempotency_key for r in _nudge_rows(task_id))
    assert keys == sorted(f"HITL_NUDGE:{task_id}:{m}:{c}" for m in (5, 15) for c in ("SMS", "INAPP"))
    (ev,) = _events("hitl.escalated")
    assert set(ev) == {"type", "operator_id", "payload", "incident_id", "run_id", "ts"}  # the standard envelope
    inc_number = _read(lambda s: s.get(IncidentRow, inc_id).incident_number)
    assert ev["incident_id"] == inc_id
    assert {"task_id", "task_type", "incident_number"} <= set(ev["payload"])  # Appendix C's three keys
    assert (ev["payload"]["task_id"], ev["payload"]["task_type"], ev["payload"]["incident_number"]) == (task_id, "APPROVE_BROADCAST", inc_number)
    assert ev["payload"]["rung_minutes"] == 15 and ev["payload"]["nudged"] == ["supervisor", "duty_manager"]
    assert ev["payload"]["created_at"].endswith("Z") and ev["payload"]["created_at_eat"].endswith(" EAT")
    mark = he.escalation_state(_task(task_id)["payload"])
    assert set(mark["rungs"]) == {"5", "15"} and mark["rungs"]["15"]["event"] == "hitl.escalated" and mark["wallboard_red"] is False
    assert len(_audits("hitl.escalated", task_id)) == 1


def test_rung_30_marks_the_card_red_and_queues_nothing_more(gated, on, clean_hub):
    settings, inc_id, task_id, created = gated
    _run(settings, created + 6 * MIN)
    _run(settings, created + 16 * MIN)
    report = _run(settings, created + 31 * MIN)

    assert [e["rungs"] for e in report.escalated] == [[30]]
    assert (report.nudges_queued, report.nudges_suppressed, report.events) == (0, 0, 0)
    assert len(_nudge_rows(task_id)) == 4  # unchanged: the last rung sends nothing
    assert len(_events("hitl.escalated")) == 1
    mark = he.escalation_state(_task(task_id)["payload"])
    assert mark["wallboard_red"] is True and mark["level_minutes"] == 30
    assert mark["red_since"].endswith("Z") and mark["red_since_eat"].endswith(" EAT")
    assert mark["rungs"]["30"] == {**mark["rungs"]["30"], "kind": "wallboard_red", "target": "wallboard", "unclaimed_minutes": 31}
    assert len(_audits("hitl.escalation.wallboard_red", task_id)) == 1
    # What the Wallboard reads (the pending route returns proposed_payload as is):
    red = _read(lambda s: [t.id for t in he.wallboard_red_tasks(s, operator_id=settings.operator.operator_id, incident_id=inc_id)])
    assert red == [task_id]


def test_a_long_freeze_fires_every_crossed_rung_once_in_one_pass(gated, on, clean_hub):
    settings, inc_id, task_id, created = gated
    report = _run(settings, created + 45 * MIN)
    assert [e["rungs"] for e in report.escalated] == [[5, 15, 30]]
    assert len(_nudge_rows(task_id)) == 4 and len(_events("hitl.escalated")) == 1
    mark = he.escalation_state(_task(task_id)["payload"])
    assert set(mark["rungs"]) == {"5", "15", "30"} and mark["wallboard_red"] is True
    assert mark["rungs"]["15"]["unclaimed_minutes"] == 45  # the truth at the time it fired


# ---------------------------------------------------------------------------- idempotency


def test_reruns_and_restarts_never_double_nudge(gated, on, clean_hub):
    """Every rung is run three times on fresh sessions (a restart is a fresh process, which is
    a fresh session): the second and third passes find nothing to do."""
    settings, inc_id, task_id, created = gated
    for minutes, expected_rows, expected_events in ((6, 2, 0), (7, 2, 0), (16, 4, 1), (17, 4, 1), (31, 4, 1), (32, 4, 1), (90, 4, 1)):
        for _ in range(3):
            _run(settings, created + minutes * MIN)
        assert len(_nudge_rows(task_id)) == expected_rows, minutes
        assert len(_events("hitl.escalated")) == expected_events, minutes
    assert {r.attempts for r in _nudge_rows(task_id)} == {0}  # never re-queued either
    assert len(_audits("hitl.escalation.nudged", task_id)) == 1
    assert len(_audits("hitl.escalated", task_id)) == 1
    assert len(_audits("hitl.escalation.wallboard_red", task_id)) == 1
    # The outbox key alone is a second, independent guard: enqueue is INSERT OR IGNORE.
    assert _read(lambda s: s.scalar(select(func.count()).select_from(OutboxRow).where(OutboxRow.idempotency_key == f"HITL_NUDGE:{task_id}:5:INAPP"))) == 1


def test_two_concurrent_passes_that_both_read_before_either_writes_nudge_once(gated, on, clean_hub, monkeypatch):
    """Two schedulers (the lease makes it unlikely, not impossible): both passes select the same
    unclaimed card, then race. The compare-and-set on the card's exact previous text lets one
    commit; the other rolls back its rows, its audit and its buffered event together."""
    settings, inc_id, task_id, created = gated
    barrier = threading.Barrier(2, timeout=30)
    original = he._candidate_ids

    def rendezvous(session, cfg, now):
        ids = original(session, cfg, now)
        barrier.wait()  # both have read; neither has written
        return ids

    monkeypatch.setattr(he, "_candidate_ids", rendezvous)
    reports: list = []
    errors: list = []

    def run():
        try:
            reports.append(_run(settings, created + 16 * MIN))
        except Exception as exc:  # noqa: BLE001 -- surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors, errors
    assert len(reports) == 2
    assert sum(len(r.escalated) for r in reports) == 1  # exactly one winner
    assert sum(r.lost for r in reports) <= 1
    assert len(_nudge_rows(task_id)) == 4 and len(_events("hitl.escalated")) == 1
    assert len(_audits("hitl.escalated", task_id)) == 1 and len(_audits("hitl.escalation.nudged", task_id)) == 1
    mark = he.escalation_state(_task(task_id)["payload"])
    assert set(mark["rungs"]) == {"5", "15"}


def test_a_claim_between_the_read_and_the_write_leaves_no_mark(gated, on, clean_hub, monkeypatch):
    """The gate is PENDING AND unclaimed AND the same text: a claim that lands mid-pass wins."""
    settings, inc_id, task_id, created = gated
    original = he._candidate_ids

    def claim_under_us(session, cfg, now):
        ids = original(session, cfg, now)
        s = get_session()
        try:
            s.execute(update(HitlTaskRow).where(HitlTaskRow.id == task_id).values(claimed_by="Grace Wanjiru", claimed_at=utcnow()))
            s.commit()
        finally:
            s.close()
        return ids

    monkeypatch.setattr(he, "_candidate_ids", claim_under_us)
    report = _run(settings, created + 16 * MIN)
    assert report.escalated == [] and _nudge_rows(task_id) == [] and _events("hitl.escalated") == []
    assert he.escalation_state(_task(task_id)["payload"]) == {}
    assert _audits("hitl.escalation.nudged", task_id) == []


# ---------------------------------------------------------------------- no external release


def test_the_ladder_never_releases_approves_rejects_or_claims(gated, on, clean_hub, monkeypatch):
    """Every rung, then a full drain of the nudges: the gate is exactly as the pipeline left it."""
    settings, inc_id, task_id, created = gated
    external = _external_state(inc_id)
    assert set(external["drafts"]) == {"PENDING_HITL"} and external["run"][0] == "WAITING_HITL" and external["channel_rows"] == 0
    # The lifecycle's own rows (the LEDGER node's EXCEL_ROW) are what the outbox holds before
    # the ladder runs; afterwards it must hold exactly those plus HITL_NUDGE rows.
    kinds_before = _read(lambda s: {r.kind for r in s.scalars(select(OutboxRow))})
    assert kinds_before <= {"EXCEL_ROW"}

    for minutes in (6, 16, 31, 60):
        _run(settings, created + minutes * MIN)
    s = get_session()
    try:
        report = drain_once(s)
    finally:
        s.close()
    assert report.claimed == 2 and report.sent == 2 and report.rejected == 0  # the two queued in-app nudges

    assert _external_state(inc_id) == external
    assert _task(task_id)["status"] == "PENDING" and _task(task_id)["claimed_by"] is None
    assert {e["type"] for e in hub._history} <= {"hitl.escalated", "hitl.nudge"}
    assert not [e for e in hub._history if e["type"].startswith(("email.", "broadcast."))]
    assert _read(lambda s: {r.kind for r in s.scalars(select(OutboxRow))}) == kinds_before | {"HITL_NUDGE"}


def test_dispatch_delivers_the_inapp_nudge_as_an_event_and_the_sms_through_the_mock_path(gated, on, clean_hub):
    settings, inc_id, task_id, created = gated
    cfg = settings.operator.model_copy(deep=True)
    cfg.notification_recipients["hitl.recipients.supervisor"] = ["+254700000001"]  # roster filled in
    s = get_session()
    try:
        he.escalate_once(s, cfg, now=created + 6 * MIN)
        report = drain_once(s)
    finally:
        s.close()
    assert report.claimed == 2 and report.sent == 2
    rows = {json.loads(r.payload_json)["channel"]: r for r in _nudge_rows(task_id)}
    assert rows["SMS"].status == "SENT" and rows["SMS"].provider == "mock"  # the existing mock SMS path
    assert rows["INAPP"].status == "SENT" and rows["INAPP"].provider == "inapp"
    assert "+254700000001" not in (rows["SMS"].payload_json + (rows["SMS"].last_error or ""))  # never on the row
    (ev,) = _events("hitl.nudge")
    assert ev["incident_id"] == inc_id and ev["payload"]["audience"] == "NOC_SHIFT" and ev["payload"]["channel"] == "INAPP"
    assert ev["payload"]["task_id"] == task_id and ev["payload"]["escalation_target"] == "supervisor"
    assert "unclaimed for 6 min" in ev["payload"]["text"]
    assert len(_audits("hitl.nudge.sent", task_id)) == 2
    mark = he.escalation_state(_task(task_id)["payload"])
    assert mark["rungs"]["5"]["recipients_configured"] == 1


def test_a_nudge_dispatched_after_the_card_was_decided_nudges_nobody(gated, on, clean_hub):
    settings, inc_id, task_id, created = gated
    _run(settings, created + 6 * MIN)
    s = get_session()
    try:  # decided before the outbox got to it (a freeze, a dead-letter retry)
        s.execute(update(HitlTaskRow).where(HitlTaskRow.id == task_id).values(status="APPROVED", resolved_by="Grace Wanjiru", resolved_at=utcnow()))
        s.commit()
        report = drain_once(s)
    finally:
        s.close()
    assert report.sent == 1  # the in-app row (the SMS row is SUPPRESSED and never claimed)
    row = next(r for r in _nudge_rows(task_id) if json.loads(r.payload_json)["channel"] == "INAPP")
    assert row.status == "SENT" and row.provider == "none" and row.last_error == "no nudge: task is APPROVED"
    assert _events("hitl.nudge") == []
    assert len(_audits("hitl.nudge.inert", task_id)) == 1


# ------------------------------------------------------------------------ recipients and text


def test_no_recipient_configured_is_recorded_not_crashed_and_no_number_is_invented(gated, on, clean_hub):
    settings, inc_id, task_id, created = gated
    assert settings.operator.notification_recipients["hitl.recipients.supervisor"] == []
    report = _run(settings, created + 6 * MIN)
    assert report.nudges_suppressed == 1 and report.nudges_queued == 1
    sms = next(r for r in _nudge_rows(task_id) if json.loads(r.payload_json)["channel"] == "SMS")
    assert sms.status == "SUPPRESSED"
    assert sms.last_error.startswith("no_recipient:") and "config/operators/safaricom.yaml" in sms.last_error
    assert json.loads(sms.payload_json)["recipients_ref"] == "hitl.recipients.supervisor"
    (audit,) = _audits("hitl.escalation.nudged", task_id)
    assert json.loads(audit.payload_json)["channels"]["SMS"]["status"] == "SUPPRESSED"
    assert json.loads(audit.payload_json)["recipients_configured"] == 0
    # The drain never touches it, and the in-app nudge still reaches the shift.
    s = get_session()
    try:
        drain = drain_once(s)
    finally:
        s.close()
    assert (drain.claimed, drain.sent) == (1, 1) and len(_events("hitl.nudge")) == 1


def test_the_nudge_text_is_deterministic_gsm7_and_carries_no_narrative(gated, on, clean_hub):
    settings, inc_id, task_id, created = gated
    _run(settings, created + 6 * MIN)
    inc_number, priority = _read(lambda s: (s.get(IncidentRow, inc_id).incident_number, s.get(IncidentRow, inc_id).priority))
    texts = {json.loads(r.payload_json)["channel"]: json.loads(r.payload_json)["message"] for r in _nudge_rows(task_id)}
    assert texts["SMS"] == f"NOC HITL: {priority} APPROVE_BROADCAST for {inc_number} unclaimed 6 min. A decision is waiting in the HITL inbox (supervisor)."
    assert is_gsm7(texts["SMS"]) and len(texts["SMS"]) <= 160
    for body in texts.values():
        assert inc_number in body and "6 min" in body
        for narrative in ("Embakasi", "HUB", "450", "POWER", "Genset", "Owner"):
            assert narrative not in body


def test_a_paused_template_stops_the_nudge_with_the_reason_rather_than_inventing_text(gated, on, clean_hub):
    settings, inc_id, task_id, created = gated
    s = get_session()
    try:
        reg = TemplateRegistry.for_config(s, settings.operator)
        reg.sync()
        reg.set_status(reg.latest("INAPP", "hitl_nudge", "en"), "PAUSED", actor="Grace Wanjiru")
        s.commit()
    finally:
        s.close()
    report = _run(settings, created + 6 * MIN)
    assert report.nudges_suppressed == 2 and report.nudges_queued == 0
    inapp = next(r for r in _nudge_rows(task_id) if json.loads(r.payload_json)["channel"] == "INAPP")
    assert inapp.status == "SUPPRESSED" and inapp.last_error.startswith("no_approved_template:") and "PAUSED" in inapp.last_error
    assert json.loads(inapp.payload_json)["message"] is None


# ---------------------------------------------------------------------- what is on the ladder


def test_claimed_cards_and_p3_p4_cards_are_not_on_the_ladder(tmp_db, on, clean_hub):
    settings, session = tmp_db
    hub_inc = process_event(session, settings, EventIngest(**HUB_EVENT))
    task = session.scalar(select(HitlTaskRow).where(HitlTaskRow.incident_id == hub_inc.id))
    task.claimed_by, task.claimed_at = "Grace Wanjiru", utcnow()  # someone who can decide is looking
    small = process_event(session, settings, EventIngest(**SMALL_EVENT))
    assert small.priority in ("P3", "P4")
    low = HitlTaskRow(incident_id=small.id, task_type="GENERIC", status="PENDING", created_by="agent:WorklogMonitorAgent")
    session.add(low)
    session.commit()
    ids = (task.id, low.id)
    created = task.created_at
    session.close()

    report = _run(settings, created + 40 * MIN)
    assert report.escalated == [] and report.not_on_ladder == 1  # the P3/P4 card was looked at and left; the claimed one was never a candidate
    assert _nudge_rows() == [] and _events("hitl.escalated") == []
    for tid in ids:
        assert he.escalation_state(_task(tid)["payload"]) == {}


def test_an_incident_less_card_without_a_priority_is_not_on_the_ladder(tmp_db, on, clean_hub):
    settings, session = tmp_db
    card = HitlTaskRow(
        incident_id=None,
        operator_id=settings.operator.operator_id,
        task_type="APPROVE_MAINTENANCE_WINDOW",
        entity_type="maintenance_window",
        entity_id="win-1",
        created_by="planning-desk",
        status="PENDING",
        created_at=utcnow() - 40 * MIN,
    )
    card.proposed_payload = {"window": {"scope": "SITE", "scope_ref": "SFC-CST-HUB-MSA"}}
    session.add(card)
    session.commit()
    cid = card.id
    session.close()
    report = _run(settings, utcnow())
    assert report.checked == 1 and report.not_on_ladder == 1 and report.escalated == []
    assert _nudge_rows() == [] and he.escalation_state(_task(cid)["payload"]) == {}


def test_an_incident_less_card_with_a_declared_priority_rides_the_ladder_with_a_null_incident_number(tmp_db, on, clean_hub):
    settings, session = tmp_db
    card = HitlTaskRow(
        incident_id=None,
        operator_id=settings.operator.operator_id,
        task_type="APPROVE_VENDOR_NOTICE",
        entity_type="vendor_notice",
        entity_id="vn-7",
        created_by="agent:SlaScorecardAgent",
        status="PENDING",
        created_at=utcnow() - 16 * MIN,
    )
    card.proposed_payload = {"priority": "P1", "subject": "notice"}
    session.add(card)
    session.commit()
    cid = card.id
    session.close()

    report = _run(settings, utcnow())
    assert [e["rungs"] for e in report.escalated] == [[5, 15]]
    (ev,) = _events("hitl.escalated")
    assert ev["incident_id"] is None
    assert ev["payload"]["incident_number"] is None and ev["payload"]["task_id"] == cid and ev["payload"]["task_type"] == "APPROVE_VENDOR_NOTICE"
    rows = _nudge_rows(cid)
    assert len(rows) == 4 and all(r.incident_id is None for r in rows)
    text_ = json.loads(next(r for r in rows if r.idempotency_key.endswith(":15:SMS")).payload_json)["message"]
    assert "for vendor_notice vn-7 unclaimed 16 min" in text_ and "(duty_manager)" in text_
    assert he.escalation_state(_task(cid)["payload"])["level_minutes"] == 15


# -------------------------------------------------------------------- the CA 24-h card (T+30)


def test_the_regulatory_sweep_notes_a_red_card_on_the_ca_24h_notice_once_and_sends_nothing(gated, on, clean_hub, monkeypatch):
    settings, inc_id, task_id, created = gated
    monkeypatch.setenv("REGULATORY_ENABLED", "true")
    s = get_session()
    try:
        inc = s.get(IncidentRow, inc_id)
        inc.failure_time = created - 60 * MIN  # the clock starts at the failure, never at row creation
        s.commit()
        notice = regulatory.open_notification(s, inc, settings.operator)  # P1: significant, DRAFT
        s.commit()
        nid, status = notice.id, notice.status
    finally:
        s.close()
    assert status == "DRAFT"

    def sweep(now):
        s = get_session()
        try:
            rep = regulatory.sweep_deadlines(s, settings.operator, now=now)
            s.commit()
            return rep
        finally:
            s.close()

    non_nudge_rows = lambda: _read(lambda s: s.scalar(select(func.count()).select_from(OutboxRow).where(OutboxRow.kind != "HITL_NUDGE")))  # noqa: E731
    rows_before = non_nudge_rows()  # the lifecycle's own ledger row; the sweep must add nothing

    # Before T+30 the card is not red: the sweep notes nothing.
    _run(settings, created + 16 * MIN)
    assert sweep(created + 17 * MIN).hitl_noted == []
    _run(settings, created + 31 * MIN)
    first = sweep(created + 32 * MIN)
    assert first.hitl_noted == [{"notification_id": nid, "task_id": task_id}]
    assert sweep(created + 33 * MIN).hitl_noted == []  # once per (notice, task)

    def read(s):
        n = s.get(regulatory.RegulatoryNotificationRow, nid)
        return n.status, n.significance.get(regulatory.HITL_ESCALATION_KEY), regulatory.countdown(n, now=created + 33 * MIN), n.sent_at

    status, note, block, sent_at = _read(read)
    assert status == "DRAFT" and sent_at is None
    assert note["task_id"] == task_id and note["task_type"] == "APPROVE_BROADCAST" and note["level_minutes"] == 30
    assert note["unclaimed_minutes"] == 32 and note["red_since_eat"].endswith(" EAT") and "unclaimed for 32 min" in note["note"]
    assert block["hitl_escalation"] == note  # what the workspace panel and the approval card read
    assert len(_audits("regulatory.hitl_escalation_noted", nid)) == 1
    # Draft-only: no regulatory outbox row, the notice never moved, nothing left the building.
    assert non_nudge_rows() == rows_before
    assert _read(lambda s: s.scalar(select(func.count()).select_from(OutboxRow).where(OutboxRow.kind == "EMAIL"))) == 0
    assert _task(task_id)["status"] == "PENDING"
