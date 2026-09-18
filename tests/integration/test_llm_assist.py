"""Assist endpoints over HTTP with a fake client injected: 200 in every mode, template
fallback on every failure, nothing changed on the ticket, lifecycle graph untouched."""

from __future__ import annotations

import importlib
import json
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from noc_agents.db.models import AgentRunRow, AuditRow, IncidentBriefRow, IncidentRow, get_session
from noc_agents.llm import assist
from noc_agents.llm.client import MODEL_DRAFTING, MODEL_FALLBACK, MODEL_REASONING
from noc_agents.llm.outputs import ExecBriefDraft, Hypothesis, RootCauseAnalysis

NODE_IDS = ["INGEST", "CORRELATE", "ENRICH", "SEVERITY", "TICKET", "ASSIGN", "HITL", "BROADCAST",
            "EXEC_BRIEF", "LEDGER", "RECURRENCE", "MONITOR"]

HUB_EVENT = {
    "site_id": "SFC-NBI-HUB-001",
    "site_name": "Westlands Hub",
    "site_type": "HUB",
    "region_code": "NBI_E",
    "alarm_code": "MAINS_FAIL",
    "failure_domain": "POWER",
    "users_affected": 450000,
    "access_notes": "Genset not started. Guard: 0712345678",
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "assist.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")
    monkeypatch.setenv("OPERATOR_PROFILE", "safaricom")
    import noc_agents.config as cfg
    import noc_agents.db.models as models
    import noc_agents.main as main

    cfg.clear_settings_cache()
    models._engine = None
    models.SessionLocal = None
    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c


@pytest.fixture()
def incident(client):
    r = client.post("/api/v1/events", json=HUB_EVENT)
    assert r.status_code == 200
    inc = r.json()["incident"]
    client.post(f"/api/v1/incidents/{inc['id']}/notes", json={"author": "Peter Otieno", "author_role": "msp", "body": "Team dispatched, call 0722000111"})
    return client.get(f"/api/v1/incidents/{inc['id']}").json()


def _ok(model, parsed):
    return SimpleNamespace(parsed_output=parsed, stop_reason="end_turn", model=model,
                           usage=SimpleNamespace(input_tokens=10, output_tokens=20))


class FakeClient:
    def __init__(self, *script):
        self.script = list(script)
        self.calls: list[dict] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self._parse))

    def _parse(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0)
        if callable(item) and not hasattr(item, "parsed_output"):
            item = item(kwargs)
        if isinstance(item, BaseException):
            raise item
        return item


def _inject(monkeypatch, fake):
    monkeypatch.setattr(assist, "get_llm", lambda *_a, **_k: fake)


def _snapshot(client, inc_id):
    d = client.get(f"/api/v1/incidents/{inc_id}").json()
    return {k: d[k] for k in ("priority", "status", "assignee_name", "assignee_type", "hitl_state", "requires_hitl", "incident_number", "sla_restore_due")}


def _runs(inc_id, graph):
    s = get_session()
    try:
        rows = s.scalars(select(AgentRunRow).where(AgentRunRow.incident_id == inc_id, AgentRunRow.graph_name == graph)).all()
        for r in rows:
            r.steps
        return rows
    finally:
        s.close()


def _llm_audit(inc_id):
    s = get_session()
    try:
        return s.scalars(select(AuditRow).where(AuditRow.entity_id == inc_id, AuditRow.action == "llm.call")).all()
    finally:
        s.close()


def _tokens_for(inc_id) -> dict[str, str]:
    from noc_agents.llm.redaction import redact_incident

    s = get_session()
    try:
        inc = s.get(IncidentRow, inc_id)
        _, mapping = redact_incident(inc, inc.notes)
        return {name: token for token, name in mapping.items()}
    finally:
        s.close()


def _assert_ticket_untouched_and_graph_intact(client, inc_id, before_state, before_wf):
    assert _snapshot(client, inc_id) == before_state
    wf = client.get(f"/api/v1/incidents/{inc_id}/workflow").json()
    assert wf["run_id"] == before_wf["run_id"]
    assert [n["id"] for n in wf["nodes"]] == NODE_IDS
    assert [s["node_name"] for s in wf["steps"]] == [s["node_name"] for s in before_wf["steps"]]
    assert {n["id"]: n["status"] for n in wf["nodes"]} == {n["id"]: n["status"] for n in before_wf["nodes"]}


# ------------------------------------------------------------------ status


def test_status_endpoint_with_sdk_absent(client, monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", None)
    r = client.get("/api/v1/llm/status")
    assert r.status_code == 200
    assert r.json() == {"enabled": False, "sdk_installed": False, "credential_present": False,
                        "complex_model": MODEL_REASONING, "standard_model": MODEL_DRAFTING}


def test_status_never_contains_secret(client, monkeypatch):
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-very-secret")
    body = client.get("/api/v1/llm/status").text
    assert "sk-ant-very-secret" not in body and json.loads(body)["credential_present"] is True


# ------------------------------------------------------------------ disabled → template


def test_analysis_template_mode_when_llm_disabled(client, incident):
    inc_id = incident["id"]
    before, wf_before = _snapshot(client, inc_id), client.get(f"/api/v1/incidents/{inc_id}/workflow").json()
    r = client.post(f"/api/v1/incidents/{inc_id}/analysis")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "template" and body["model"] is None and body["incident_id"] == inc_id
    analysis = body["analysis"]
    assert analysis["summary"] == incident["root_cause_hypothesis"]
    assert analysis["hypotheses"][0]["likelihood"] == "medium"
    assert analysis["recommended_checks"] == assist.CHECKS_BY_DOMAIN["POWER"]
    runs = _runs(inc_id, "llm_assist")
    assert len(runs) == 1 and runs[0].id == body["run_id"]
    assert runs[0].status == "SUCCEEDED" and runs[0].trigger == "ON_DEMAND"
    assert [(s.node_name, s.agent_name, s.status) for s in runs[0].steps] == [("RCA", "TicketingAgent", "SUCCEEDED")]
    assert runs[0].steps[0].tools_called == [{"name": "template", "ok": True, "latency_ms": 0}]
    assert _llm_audit(inc_id) == []  # no external call → no transfer record
    _assert_ticket_untouched_and_graph_intact(client, inc_id, before, wf_before)
    assert client.get(f"/api/v1/runs/{body['run_id']}").json()["graph_name"] == "llm_assist"


def test_brief_template_mode_matches_pipeline_brief_and_writes_no_brief_row(client, incident):
    inc_id = incident["id"]
    stored = client.get(f"/api/v1/briefs/{inc_id}").json()["body"]
    r = client.post(f"/api/v1/incidents/{inc_id}/brief/draft")
    assert r.status_code == 200
    assert r.json()["source"] == "template" and r.json()["body"] == stored
    s = get_session()
    try:
        assert s.scalar(select(IncidentBriefRow).where(IncidentBriefRow.incident_id == inc_id).order_by(IncidentBriefRow.updated_at)) is not None
        assert len(s.scalars(select(IncidentBriefRow).where(IncidentBriefRow.incident_id == inc_id)).all()) == 1
    finally:
        s.close()


def test_unknown_incident_is_404(client):
    assert client.post("/api/v1/incidents/nope/analysis").status_code == 404
    assert client.post("/api/v1/incidents/nope/brief/draft").status_code == 404


# ------------------------------------------------------------------ fake success → llm mode


def test_analysis_llm_success_with_redaction_and_name_restore(client, incident, monkeypatch):
    inc_id = incident["id"]
    before, wf_before = _snapshot(client, inc_id), client.get(f"/api/v1/incidents/{inc_id}/workflow").json()
    tokens = _tokens_for(inc_id)  # real name -> token the model will see
    parsed = RootCauseAnalysis(
        summary=f"Mains failure; genset did not auto-start. Owner {tokens[incident['assignee_name']]}.",
        hypotheses=[Hypothesis(cause="Genset controller fault", likelihood="high",
                               evidence=["alarm MAINS_FAIL", f"note by {tokens['Peter Otieno']}"])],
        recommended_checks=["Check genset controller", "Confirm fuel level"],
    )
    fake = FakeClient(_ok(MODEL_REASONING, parsed))
    _inject(monkeypatch, fake)

    r = client.post(f"/api/v1/incidents/{inc_id}/analysis")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "llm" and body["model"] == MODEL_REASONING
    assert body["analysis"]["hypotheses"][0]["cause"] == "Genset controller fault"
    assert body["analysis"]["summary"].endswith(f"Owner {incident['assignee_name']}.")
    assert body["analysis"]["hypotheses"][0]["evidence"][1] == "note by Peter Otieno"

    # what left the box: redacted JSON, no names / phones, documented call shape
    kw = fake.calls[0]
    sent = kw["messages"][0]["content"]
    sent_json = json.loads(sent)
    for person in (incident["fe_name"], incident["rnio_name"], "Peter Otieno"):
        assert person and person not in sent, person
    assert sent_json["assignee_name"].startswith("<PERSON_")  # tokenised even when it names the MSP
    assert sent_json["msp_name"] == incident["msp_name"]  # company name is allowlisted network data
    assert "0712345678" not in sent and "0722000111" not in sent
    assert sent_json["incident_number"] == incident["incident_number"]
    assert kw["output_format"] is RootCauseAnalysis and kw["output_config"] == {"effort": "medium"}
    assert kw["max_tokens"] == assist.ANALYSIS_MAX_TOKENS == 8192
    assert kw["betas"] == ["server-side-fallback-2026-07-01"] and kw["fallbacks"] == "default"
    assert "thinking" not in kw and "tool_choice" not in kw and kw["timeout"] == 60.0  # reasoning budget, not the 20 s drafting one

    run = _runs(inc_id, "llm_assist")[0]
    assert run.status == "SUCCEEDED"
    step = run.steps[0]
    assert step.started_at <= step.finished_at and step.tools_called[0]["name"] == "claude.parse"
    assert step.tools_called[0]["ok"] is True and step.tools_called[0]["model"] == MODEL_REASONING
    audit = _llm_audit(inc_id)
    assert len(audit) == 1 and audit[0].actor == "TicketingAgent"
    payload = json.loads(audit[0].payload_json)
    assert payload["recipient"] == "Anthropic API" and payload["ok"] is True and payload["refused"] is False
    assert payload["fallback_used"] is False and payload["input_tokens"] == 10 and payload["output_tokens"] == 20
    assert set(payload) >= {"ts", "justification", "data_description", "model_requested", "model_used", "latency_ms"}
    assert payload["effort"] == "medium" and payload["max_tokens"] == 8192 and payload["timeout_s"] == 60.0
    assert "Genset controller" not in audit[0].payload_json  # no content in the audit trail
    _assert_ticket_untouched_and_graph_intact(client, inc_id, before, wf_before)


def test_brief_llm_success_uses_opus_and_writes_no_brief_row(client, incident, monkeypatch):
    inc_id = incident["id"]
    owner_token = _tokens_for(inc_id)[incident["assignee_name"]]
    text = f"{incident['incident_number']} ({incident['priority']}) Westlands Hub power outage. Owner {owner_token}. Next update 15 min."
    fake = FakeClient(_ok(MODEL_DRAFTING, ExecBriefDraft(body=text)))
    _inject(monkeypatch, fake)
    r = client.post(f"/api/v1/incidents/{inc_id}/brief/draft")
    assert r.status_code == 200
    assert r.json()["source"] == "llm" and r.json()["model"] == MODEL_DRAFTING
    assert r.json()["body"] == text.replace(owner_token, incident["assignee_name"])
    assert fake.calls[0]["model"] == MODEL_DRAFTING and fake.calls[0]["output_config"] == {"effort": "low"}
    assert fake.calls[0]["timeout"] == 20.0 and fake.calls[0]["max_tokens"] == 2048
    s = get_session()
    try:
        assert len(s.scalars(select(IncidentBriefRow).where(IncidentBriefRow.incident_id == inc_id)).all()) == 1
    finally:
        s.close()
    run = _runs(inc_id, "llm_assist")[0]
    assert [(st.node_name, st.agent_name) for st in run.steps] == [("BRIEF_DRAFT", "ExecutiveBriefingAgent")]


# ------------------------------------------------------------------ failures → template, still 200


def test_refusal_on_both_models_falls_back_to_template(client, incident, monkeypatch):
    inc_id = incident["id"]
    refusal = SimpleNamespace(parsed_output=None, stop_reason="refusal", model=MODEL_REASONING, usage=None)
    fake = FakeClient(refusal, refusal)
    _inject(monkeypatch, fake)
    r = client.post(f"/api/v1/incidents/{inc_id}/analysis")
    assert r.status_code == 200 and r.json()["source"] == "template"
    assert [c["model"] for c in fake.calls] == [MODEL_REASONING, MODEL_FALLBACK]
    run = _runs(inc_id, "llm_assist")[0]
    assert run.status == "SUCCEEDED" and run.steps[0].status == "SUCCEEDED"
    assert "refusal" in run.steps[0].rationale
    payload = json.loads(_llm_audit(inc_id)[0].payload_json)
    assert payload["refused"] is True and payload["ok"] is False and payload["fallback_used"] is True


def test_timeout_on_fable_recovers_on_opus(client, incident, monkeypatch):
    inc_id = incident["id"]
    APITimeoutError = type("APITimeoutError", (Exception,), {})
    parsed = RootCauseAnalysis(summary="ok", hypotheses=[Hypothesis(cause="c", likelihood="low", evidence=[])], recommended_checks=[])
    fake = FakeClient(APITimeoutError("timed out"), _ok(MODEL_FALLBACK, parsed))
    _inject(monkeypatch, fake)
    r = client.post(f"/api/v1/incidents/{inc_id}/analysis")
    assert r.status_code == 200
    assert r.json()["source"] == "llm" and r.json()["model"] == MODEL_FALLBACK
    assert [c["model"] for c in fake.calls] == [MODEL_REASONING, MODEL_FALLBACK]
    assert json.loads(_llm_audit(inc_id)[0].payload_json)["fallback_used"] is True


def test_timeout_on_both_models_falls_back_to_template(client, incident, monkeypatch):
    inc_id = incident["id"]
    APITimeoutError = type("APITimeoutError", (Exception,), {})
    fake = FakeClient(APITimeoutError("t1"), APITimeoutError("t2"))
    _inject(monkeypatch, fake)
    r = client.post(f"/api/v1/incidents/{inc_id}/analysis")
    assert r.status_code == 200 and r.json()["source"] == "template"
    assert r.json()["analysis"]["summary"] == incident["root_cause_hypothesis"]
    assert _runs(inc_id, "llm_assist")[0].status == "SUCCEEDED"


def test_malformed_output_falls_back_to_template_without_retry(client, incident, monkeypatch):
    inc_id = incident["id"]
    ValidationError = type("ValidationError", (ValueError,), {})
    fake = FakeClient(ValidationError("1 validation error for ExecBriefDraft"))
    _inject(monkeypatch, fake)
    r = client.post(f"/api/v1/incidents/{inc_id}/brief/draft")
    assert r.status_code == 200 and r.json()["source"] == "template"
    assert len(fake.calls) == 1
    payload = json.loads(_llm_audit(inc_id)[0].payload_json)
    assert payload["ok"] is False and payload["error"].startswith("ValidationError")


def test_brief_missing_incident_number_fails_validation_and_uses_template(client, incident, monkeypatch):
    inc_id = incident["id"]
    fake = FakeClient(_ok(MODEL_DRAFTING, ExecBriefDraft(body="Everything is fine, no numbers here.")))
    _inject(monkeypatch, fake)
    r = client.post(f"/api/v1/incidents/{inc_id}/brief/draft")
    assert r.status_code == 200 and r.json()["source"] == "template"
    assert incident["incident_number"] in r.json()["body"]
    # the step's tool entry, its confidence and the audit row all agree that the model's answer was NOT used
    run = _runs(inc_id, "llm_assist")[0]
    tool = run.steps[0].tools_called[0]
    assert tool["name"] == "claude.parse" and tool["ok"] is False and "failed validation" in tool["error"]
    assert run.steps[0].confidence == 0.6 and "template fallback" in run.steps[0].output_summary
    payload = json.loads(_llm_audit(inc_id)[0].payload_json)
    assert payload["ok"] is False and "failed validation" in payload["error"]
    assert "no numbers here" not in json.dumps(payload)  # still no model text in the audit row
    fake2 = FakeClient(_ok(MODEL_DRAFTING, ExecBriefDraft(body=f"{incident['incident_number']} {incident['priority']} " + "x" * 2500)))
    _inject(monkeypatch, fake2)
    assert client.post(f"/api/v1/incidents/{inc_id}/brief/draft").json()["source"] == "template"
    assert all(json.loads(a.payload_json)["ok"] is False for a in _llm_audit(inc_id))


def test_analysis_without_usable_hypotheses_marks_the_record_unusable(client, incident, monkeypatch):
    inc_id = incident["id"]
    parsed = RootCauseAnalysis(summary="s", hypotheses=[Hypothesis(cause="   ", likelihood="low", evidence=[])], recommended_checks=[])
    _inject(monkeypatch, FakeClient(_ok(MODEL_REASONING, parsed)))
    r = client.post(f"/api/v1/incidents/{inc_id}/analysis")
    assert r.status_code == 200 and r.json()["source"] == "template"
    payload = json.loads(_llm_audit(inc_id)[0].payload_json)
    assert payload["ok"] is False and "no usable hypothesis" in payload["error"]
    assert _runs(inc_id, "llm_assist")[0].steps[0].tools_called[0]["ok"] is False


def test_usable_hypotheses_are_kept_even_after_five_blank_ones():
    blanks = [Hypothesis(cause=" ", likelihood="low", evidence=[]) for _ in range(5)]
    real = [Hypothesis(cause=f"cause {i}", likelihood="medium", evidence=[]) for i in range(7)]
    out = assist._validated_analysis(RootCauseAnalysis(summary="s", hypotheses=blanks + real, recommended_checks=[]), {})
    assert out is not None and [h.cause for h in out.hypotheses] == [f"cause {i}" for i in range(5)]


def test_client_that_explodes_unexpectedly_still_returns_template(client, incident, monkeypatch):
    inc_id = incident["id"]

    class Broken:
        beta = None  # attribute access inside parse_structured raises AttributeError

    _inject(monkeypatch, Broken())
    r = client.post(f"/api/v1/incidents/{inc_id}/analysis")
    assert r.status_code == 200 and r.json()["source"] == "template"
    assert _runs(inc_id, "llm_assist")[0].status == "SUCCEEDED"


def test_sdk_absent_with_flag_and_key_is_template_mode(client, incident, monkeypatch):
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setitem(sys.modules, "anthropic", None)
    inc_id = incident["id"]
    r = client.post(f"/api/v1/incidents/{inc_id}/analysis")
    assert r.status_code == 200 and r.json()["source"] == "template"
    assert client.get("/api/v1/llm/status").json()["sdk_installed"] is False


def test_no_db_lock_is_held_during_the_model_call(client, incident, monkeypatch):
    """A concurrent writer must succeed while parse() is in flight (phase 2 holds no transaction)."""
    inc_id = incident["id"]
    parsed = RootCauseAnalysis(summary="s", hypotheses=[Hypothesis(cause="c", likelihood="low", evidence=[])], recommended_checks=[])

    def slow_parse(kwargs):
        other = get_session()
        try:
            other.add(AgentRunRow(id="concurrent-run", incident_id=inc_id, operator_id="safaricom", graph_name="probe",
                                  trigger="TEST", status="SUCCEEDED"))
            other.commit()  # would raise "database is locked" if the request held the write lock
        finally:
            other.close()
        return _ok(MODEL_REASONING, parsed)

    _inject(monkeypatch, FakeClient(slow_parse))
    r = client.post(f"/api/v1/incidents/{inc_id}/analysis")
    assert r.status_code == 200 and r.json()["source"] == "llm"
    assert _runs(inc_id, "probe")[0].id == "concurrent-run"


def test_assist_runs_are_listed_but_do_not_replace_the_lifecycle_view(client, incident):
    inc_id = incident["id"]
    wf_before = client.get(f"/api/v1/incidents/{inc_id}/workflow").json()
    client.post(f"/api/v1/incidents/{inc_id}/analysis")
    client.post(f"/api/v1/incidents/{inc_id}/brief/draft")
    runs = client.get(f"/api/v1/runs?incident_id={inc_id}").json()
    assert sorted(r["graph_name"] for r in runs) == ["incident_lifecycle", "llm_assist", "llm_assist"]
    assert all(isinstance(r["steps"], list) for r in runs)
    wf = client.get(f"/api/v1/incidents/{inc_id}/workflow").json()
    assert wf["run_id"] == wf_before["run_id"] and [s["node_name"] for s in wf["steps"]] == NODE_IDS
    tl = client.get(f"/api/v1/incidents/{inc_id}/timeline").json()
    assert [i["title"].split(" · ")[1] for i in tl if i["kind"] == "agent_step"] == NODE_IDS


def test_prepare_failure_returns_200_with_a_failed_tracked_run(client, incident, monkeypatch):
    inc_id = incident["id"]
    before, wf_before = _snapshot(client, inc_id), client.get(f"/api/v1/incidents/{inc_id}/workflow").json()

    def boom(*_a, **_k):
        raise ValueError("bad template")

    monkeypatch.setattr(assist, "compose_brief", boom)
    r = client.post(f"/api/v1/incidents/{inc_id}/brief/draft")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "template" and body["incident_id"] == inc_id
    assert incident["incident_number"] in body["body"]  # documented answer key is always present
    run = _runs(inc_id, "llm_assist")[0]
    assert run.id == body["run_id"] and run.status == "FAILED"
    assert run.steps[0].status == "FAILED" and "ValueError: bad template" in run.steps[0].rationale
    _assert_ticket_untouched_and_graph_intact(client, inc_id, before, wf_before)


def test_saturated_assist_slots_skip_the_model_and_use_the_template(client, incident, monkeypatch):
    inc_id = incident["id"]
    fake = FakeClient(_ok(MODEL_DRAFTING, ExecBriefDraft(body="never used")))
    _inject(monkeypatch, fake)
    full = assist._ASSIST_SLOTS.__class__(1)
    full.acquire()
    monkeypatch.setattr(assist, "_ASSIST_SLOTS", full)
    r = client.post(f"/api/v1/incidents/{inc_id}/brief/draft")
    assert r.status_code == 200 and r.json()["source"] == "template"
    assert fake.calls == []  # no model call while the slots are saturated
    run = _runs(inc_id, "llm_assist")[0]
    assert run.status == "SUCCEEDED" and "assist busy" in run.steps[0].rationale
    assert _llm_audit(inc_id) == []
    full.release()
    assert client.post(f"/api/v1/incidents/{inc_id}/brief/draft").json()["source"] == "template"  # body lacks INC number → template
    assert len(fake.calls) == 1  # slot free again → the model was called


def test_validation_crash_after_the_model_call_still_writes_the_audit_row(client, incident, monkeypatch):
    inc_id = incident["id"]
    parsed = RootCauseAnalysis(summary="s", hypotheses=[Hypothesis(cause="c", likelihood="low", evidence=[])], recommended_checks=[])
    _inject(monkeypatch, FakeClient(_ok(MODEL_REASONING, parsed)))

    def boom(*_a, **_k):
        raise KeyError("restore blew up")

    monkeypatch.setattr(assist, "restore_names", boom)
    r = client.post(f"/api/v1/incidents/{inc_id}/analysis")
    assert r.status_code == 200 and r.json()["source"] == "template" and "analysis" in r.json()
    run = _runs(inc_id, "llm_assist")[0]
    assert run.status == "SUCCEEDED" and "validation crashed" in run.steps[0].rationale
    audit = _llm_audit(inc_id)
    assert len(audit) == 1  # data left the box → transfer record written even though validation crashed
    payload = json.loads(audit[0].payload_json)
    assert payload["ok"] is False and payload["models_tried"] == [MODEL_REASONING]


def test_runs_for_one_incident_keep_the_50_row_cap(client, incident):
    inc_id = incident["id"]
    s = get_session()
    try:
        for i in range(55):
            s.add(AgentRunRow(id=f"cap-{i}", incident_id=inc_id, operator_id="safaricom", graph_name="llm_assist",
                              trigger="ON_DEMAND", status="SUCCEEDED"))
        s.commit()
    finally:
        s.close()
    assert len(client.get(f"/api/v1/runs?incident_id={inc_id}").json()) == 50
    assert len(client.get("/api/v1/runs").json()) == 50
    assert len(client.get(f"/api/v1/runs?incident_id={inc_id}&graph_name=incident_lifecycle").json()) == 1


def test_runs_can_be_filtered_by_graph_name(client, incident):
    inc_id = incident["id"]
    client.post(f"/api/v1/incidents/{inc_id}/analysis")
    lifecycle = client.get("/api/v1/runs?graph_name=incident_lifecycle").json()
    assert lifecycle and all(r["graph_name"] == "incident_lifecycle" for r in lifecycle)
    assist_runs = client.get(f"/api/v1/runs?incident_id={inc_id}&graph_name=llm_assist").json()
    assert [r["graph_name"] for r in assist_runs] == ["llm_assist"]
