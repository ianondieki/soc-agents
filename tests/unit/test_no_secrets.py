"""No credential this process holds may reach anything a human or a regulator can read.

Six sinks, all of them checked against the same set of realistic fake credentials planted in
the environment before a full incident lifecycle runs:

1. REST response bodies (and headers) — every GET the UI makes, plus the lifecycle POSTs;
2. WebSocket event payloads — the whole ``EventHub`` ring buffer, which is exactly what
   ``/ws/ops`` and the SSE stream hand to a browser;
3. log lines — the root logger at DEBUG for the duration of the run;
4. ``audit_events`` rows, including the transfer register's payload JSON;
5. ``llm_calls`` rows (and ``LlmCallRecord``, documented as never carrying text);
6. ``agent_run_steps`` rows — and, belt and braces, EVERY column of EVERY table in the
   database file, so a new table cannot quietly become a seventh sink.

Also pinned: ``GET /api/v1/profile`` no longer carries an ``email`` block. It is an
unauthenticated route and that block published the configured mailbox and the demo
recipient list; §7.0.5 removed it in Phase 1 and ``test_profile_has_no_email_block``
exists so it cannot come back by accident. ``GET /api/v1/email/status`` still serves that
information — it is the Settings page's source — and is checked here for the one thing it
must never carry: the app password itself.

Deliberately NOT exercised, and why:

* ``POST /api/v1/email/test`` and ``EMAIL_ENABLED=true``. Both reach ``smtplib.SMTP(...)``
  and this suite opens no sockets. The credentials are planted with ``EMAIL_ENABLED`` left
  false, the state in which the send path is mocked — these assertions are about what is
  *reported* and *stored*, not about a live send. The password is read only inside
  ``send_email``'s ``server.login`` call, which is the branch not taken here.
* a real model call. With ``LLM_ENABLED=false`` the lifecycle writes no ``llm_calls`` row,
  so that sink is proven two ways instead: structurally (no column of ``LlmCallRow`` can
  hold text) and behaviourally, by handing ``record_llm_call`` the worst record a failing
  adapter could produce — see ``test_a_poisoned_llm_call_record_persists_a_clean_llm_calls_row``.

Two real gaps in ``services/external_calls.scrub_secrets`` are recorded at the bottom of
this file and fixed in external_calls.py; read their notes before touching that regex.
"""

from __future__ import annotations

import importlib
import json
import logging
import sqlite3

import pytest

# Realistic shapes, obviously fake values. Every one of these strings must be unfindable.
FAKE_CREDENTIALS = {
    "ANTHROPIC_API_KEY": "sk-ant-api03-N0CPROBEFAKEKEY-4b7f2c9e1a6d8305f2b4c6e8a0d2f4b6-AA",
    "ANTHROPIC_AUTH_TOKEN": "sk-ant-oat01-N0CPROBEFAKEOAUTH-9c3e5a7b1d0f2648ace02468bdf13579",
    "GMAIL_APP_PASSWORD": "zqwe erty uiop asdf",  # Gmail shows app passwords in four blocks
    "SMTP_PASSWORD": "N0CPROBESMTPPASSWORD-6f4e2d0c8b6a4957",
    "OPENAI_COMPAT_API_KEY": "N0CPROBEOLLAMAKEY-1a2b3c4d5e6f7081",
    "AT_API_KEY": "atsk_N0CPROBEAFRICASTALKING_0f9e8d7c6b5a4938",
    "WHATSAPP_ACCESS_TOKEN": "EAAN0CPROBEWHATSAPPTOKEN00112233445566778899aabb",
    "NOC_SESSION_SECRET": "N0CPROBESESSIONSECRET-5544332211009988",
}
# The login path strips the spaces out of a Gmail app password, so the compact form is a
# second, independent needle: a leak could carry either spelling.
COMPACT_APP_PASSWORD = FAKE_CREDENTIALS["GMAIL_APP_PASSWORD"].replace(" ", "")
NEEDLES: tuple[str, ...] = (*FAKE_CREDENTIALS.values(), COMPACT_APP_PASSWORD)

# Mailbox configuration, not a credential: /api/v1/email/status may report it, and
# /api/v1/profile may not (that is the §7.0.5 removal this file pins).
FAKE_MAILBOX = "noc-probe-mailbox@example.test"

HUB_EVENT = {
    "site_id": "SFC-NBIE-HUB-EMB",
    "site_name": "Embakasi East Aggregation HUB",
    "site_type": "HUB",
    "region_code": "NBI_E",
    "alarm_code": "POWER_GRID_FAIL",
    "failure_domain": "POWER",
    "users_affected": 450000,
    "access_notes": "Genset not started",
}


def _find(needles: tuple[str, ...], text: str) -> list[str]:
    return [n for n in needles if n and n in text]


def _assert_clean(where: str, text: str, needles: tuple[str, ...] = NEEDLES) -> None:
    hits = _find(needles, text)
    assert not hits, f"CREDENTIAL LEAK in {where}: {hits!r}"


@pytest.fixture()
def planted_credentials(monkeypatch):
    """Set every credential the process knows how to hold. EMAIL_ENABLED stays false: the
    SMTP path must stay mocked, this suite opens no sockets."""
    for name, value in FAKE_CREDENTIALS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("GMAIL_ADDRESS", FAKE_MAILBOX)
    monkeypatch.setenv("SMTP_USER", FAKE_MAILBOX)
    monkeypatch.setenv("DEMO_EMAIL_TO", FAKE_MAILBOX)
    monkeypatch.setenv("EMAIL_ENABLED", "false")
    monkeypatch.setenv("LLM_ENABLED", "false")
    return FAKE_CREDENTIALS


@pytest.fixture()
def client(tmp_path, monkeypatch, planted_credentials):
    from fastapi.testclient import TestClient

    db = tmp_path / "no_secrets.db"
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
        c.db_path = str(db)  # type: ignore[attr-defined]
        yield c


def _dump_database(db_path: str) -> str:
    """Every column of every row of every table, as text. A new table joins the scan for free."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        con.text_factory = lambda b: b.decode("utf-8", "replace")
        tables = [
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        assert tables, "no tables: the lifecycle did not write to the database under test"
        chunks = []
        for table in tables:
            for row in con.execute(f'SELECT * FROM "{table}"'):
                chunks.append(f"{table}: " + " | ".join("" if v is None else str(v) for v in row))
        return "\n".join(chunks)
    finally:
        con.close()


@pytest.fixture()
def lifecycle(client, caplog):
    """One full incident lifecycle with the credentials planted, returning every sink."""
    from noc_agents.realtime.hub import hub

    hub._history.clear()
    caplog.set_level(logging.DEBUG)
    bodies: dict[str, str] = {}

    def record(name, response):
        bodies[name] = f"{dict(response.headers)}\n{response.text}"
        return response

    inc = record("POST /api/v1/events", client.post("/api/v1/events", json=HUB_EVENT)).json()["incident"]
    inc_id = inc["id"]

    pending = record("GET /api/v1/hitl/pending", client.get("/api/v1/hitl/pending")).json()
    assert pending, "the lifecycle raised no HITL task: the approve path was not exercised"
    task_id = pending[0]["id"]
    record("claim", client.post(f"/api/v1/hitl/{task_id}/claim", json={"resolved_by": "Supervisor A"}))
    record("approve", client.post(f"/api/v1/hitl/{task_id}/approve", json={"resolved_by": "Supervisor A"}))

    record(
        "POST notes",
        client.post(
            f"/api/v1/incidents/{inc_id}/notes",
            json={
                "author": "Egypro tech",
                "author_role": "MSP",
                "body": "On site, genset started",
                "msp_root_cause": "Grid failure; genset fuel low",
                "msp_action_taken": "Refuelled and started DG",
                "msp_percent_complete": 60,
            },
        ),
    )
    record("POST monitor tick", client.post("/api/v1/monitor/tick"))
    record(
        "POST close",
        client.post(
            f"/api/v1/incidents/{inc_id}/close",
            json={"resolved_by": "NOC Lead", "resolution_note": "Genset restored, service up"},
        ),
    )
    record("POST handover", client.post("/api/v1/shifts/handover"))

    for path in (
        "/health",
        "/api/v1/profile",
        "/api/v1/email/status",
        "/api/v1/llm/status",
        "/api/v1/session",
        "/api/v1/incidents",
        f"/api/v1/incidents/{inc_id}",
        f"/api/v1/incidents/{inc_id}/timeline",
        f"/api/v1/incidents/{inc_id}/workflow",
        f"/api/v1/briefs/{inc_id}",
        "/api/v1/runs",
        "/api/v1/agents",
        "/api/v1/problems",
        "/api/v1/audit?limit=500",
        "/api/v1/metrics/summary",
        "/api/v1/shifts/current",
        "/api/v1/scheduler/status",
        "/api/v1/sites",
        "/api/v1/demo/scenarios",
    ):
        record(f"GET {path}", client.get(path))

    yield {
        "incident_id": inc_id,
        "bodies": bodies,
        "events": "\n".join(json.dumps(dict(e), default=str) for e in hub.recent(10_000)),
        "logs": "\n".join(f"{r.name} {r.getMessage()}" for r in caplog.records),
        "db": _dump_database(client.db_path),
    }
    hub._history.clear()  # leave the shared singleton as we found it


# ------------------------------------------------------------------ the six sinks


def test_no_credential_reaches_a_rest_response(lifecycle):
    assert lifecycle["bodies"], "no responses captured"
    for name, text in lifecycle["bodies"].items():
        _assert_clean(f"response {name}", text)


def test_no_credential_reaches_a_websocket_event_payload(lifecycle):
    # A floor, so this can never pass vacuously: a full run publishes ~26 events on its own.
    assert lifecycle["events"].count("\n") >= 20, "too few events published to be a real check"
    _assert_clean("EventHub ring buffer (what /ws/ops and the SSE stream send)", lifecycle["events"])


def test_no_credential_reaches_a_log_line(lifecycle):
    assert lifecycle["logs"].count("\n") >= 5, "too few log records captured at DEBUG to be a real check"
    _assert_clean("log output", lifecycle["logs"])


def test_no_credential_reaches_any_persisted_row(lifecycle):
    """audit_events, llm_calls, agent_run_steps — and every other table in the file."""
    dump = lifecycle["db"]
    for table in ("incidents", "audit_events", "agent_run_steps", "agent_runs", "work_notes"):
        assert f"{table}: " in dump, f"{table} wrote no rows: the lifecycle did not exercise it"
    _assert_clean("the database file", dump)


# ------------------------------------------------------------------ the §7.0.5 profile removal


def test_profile_has_no_email_block(client):
    """§7.0.5: /api/v1/profile is unauthenticated and used to publish the mailbox config.

    The block was removed in Phase 1. This pins the removal — the mailbox, the recipient
    list and the key itself must all be absent, whatever the environment holds."""
    body = client.get("/api/v1/profile")
    assert body.status_code == 200
    payload = body.json()
    text = body.text

    assert "email" not in payload
    assert not [k for k in payload if "mail" in k.lower() or "smtp" in k.lower()]
    assert FAKE_MAILBOX not in text, "the configured mailbox is back on the unauthenticated profile"
    assert "recipients" not in text
    _assert_clean("/api/v1/profile", text)

    # The information still has a home, on the route built for it.
    status = client.get("/api/v1/email/status")
    assert status.status_code == 200 and "configured" in status.json()


def test_email_status_reports_configuration_but_never_the_password(client):
    status = client.get("/api/v1/email/status")
    payload = status.json()

    assert payload["configured"] is False  # EMAIL_ENABLED=false gates it even with creds present
    assert payload["host"] and payload["port"]
    _assert_clean("/api/v1/email/status", status.text)
    for key, value in payload.items():
        assert not _find(NEEDLES, str(value)), f"/api/v1/email/status.{key} carries a credential"


def test_llm_status_reports_presence_not_the_credential(client):
    from noc_agents.llm.client import llm_port_status, llm_status

    body = client.get("/api/v1/llm/status")
    assert body.status_code == 200
    _assert_clean("/api/v1/llm/status", body.text)

    # The status dicts answer "is a credential set?" and "which kind?", never "which value?".
    for name, snapshot in (("llm_status", llm_status()), ("llm_port_status", llm_port_status())):
        _assert_clean(name, json.dumps(snapshot, default=str))
    assert llm_port_status()["credential_kind"] in ("api_key", "auth_token", None)
    assert isinstance(llm_status()["credential_present"], bool)


# ------------------------------------------------------------------ the seams that handle credentials


def test_llm_call_row_and_record_have_nowhere_to_put_a_secret(client):
    """The ``llm_calls`` row is documented as never carrying text. Pinned structurally, so a
    new ``prompt``/``response`` column has to argue with this test first."""
    from noc_agents.db.models import LlmCallRow
    from noc_agents.llm.structured import LlmCallRecord

    columns = {c.name for c in LlmCallRow.__table__.columns}
    # ``input_tokens`` / ``output_tokens`` / ``cache_read_tokens`` are COUNTS, not material.
    counters = {"input_tokens", "output_tokens", "cache_read_tokens"}
    forbidden = ("prompt", "response", "completion", "content", "text", "body", "key", "token", "secret")
    suspects = [c for c in columns - counters if any(f in c for f in forbidden)]
    assert not suspects, f"text-shaped column in llm_calls: {suspects}"

    fields = set(LlmCallRecord.__dataclass_fields__)
    assert not [f for f in fields if any(w in f for w in ("prompt", "response", "content", "text"))]
    assert {"model_requested", "ok", "input_tokens", "output_tokens"} <= fields


def test_a_poisoned_llm_call_record_persists_a_clean_llm_calls_row(client):
    """The lifecycle writes no ``llm_calls`` row (LLM_ENABLED=false), so the sink is proven
    by writing one directly from the worst record a failing adapter could hand over:
    ``rec.error`` holding the raw credential. ``record_llm_call`` maps no field of the record
    to a text column, so the row comes back clean."""
    from noc_agents.db.models import LlmCallRow, get_session
    from noc_agents.llm.port import record_llm_call
    from noc_agents.llm.structured import LlmCallRecord

    rec = LlmCallRecord(model_requested="claude-fable-5-1")
    rec.ok = False
    rec.error = f"AuthenticationError: invalid x-api-key {FAKE_CREDENTIALS['ANTHROPIC_API_KEY']}"
    rec.input_tokens, rec.output_tokens, rec.latency_ms = 120, 0, 42

    session = get_session()
    try:
        row = record_llm_call(
            session,
            operator_id="safaricom",
            agent="EnrichmentAgent",
            purpose="root_cause",
            provider="anthropic",
            rec=rec,
            audit_id="audit-probe",
        )
        session.commit()
        stored = session.get(LlmCallRow, row.id)
        assert stored is not None and stored.ok == 0
        _assert_clean(
            "llm_calls row",
            json.dumps({c.name: getattr(stored, c.name) for c in LlmCallRow.__table__.columns}, default=str),
        )
    finally:
        session.close()

    _assert_clean("the database after a poisoned llm_calls row", _dump_database(client.db_path))


def test_describe_error_keeps_a_failed_auth_call_content_free(client):
    """The one error a credential is plausibly involved in is the 401. It takes the
    status-code branch, so only the class name and the status reach the audit hint."""
    from noc_agents.llm.structured import describe_error

    class AuthenticationError(Exception):
        status_code = 401

    hint = describe_error(AuthenticationError(f"invalid x-api-key {FAKE_CREDENTIALS['ANTHROPIC_API_KEY']}"))
    assert hint == "AuthenticationError (status 401)"
    _assert_clean("describe_error(status error)", hint)
    assert "invalid x-api-key" not in hint  # the provider's message is dropped whole


def test_scrub_secrets_blanks_every_credential_this_process_holds(client):
    """``_secret_values()`` reads the environment, so the exact value each variable holds is
    replaced wherever it appears — no key=value spelling needed."""
    from noc_agents.services.external_calls import REDACTED, scrub_secrets

    for name, value in FAKE_CREDENTIALS.items():
        scrubbed = scrub_secrets(f"operator pasted this while debugging: {value} — end")
        assert value not in scrubbed, f"scrub_secrets let the value of {name} through"
        assert REDACTED in scrubbed


def test_transfer_register_writes_a_clean_row(client):
    """``record_transfer`` is the row a regulator reads. A credential pasted into its
    free-text fields is blanked before it is written, and nothing lands elsewhere."""
    from noc_agents.db.models import AuditRow, get_session
    from noc_agents.services.external_calls import record_transfer

    session = get_session()
    try:
        row = record_transfer(
            session,
            recipient="ollama-local",
            recipient_country="KE",
            justification=f"retry after auth failure, api_key={FAKE_CREDENTIALS['ANTHROPIC_API_KEY']}",
            data_description=f"prompt context; relay login {FAKE_CREDENTIALS['SMTP_PASSWORD']}",
            actor="tester",
            actor_role="noc_engineer",
            incident_id=None,
            residency="local",
        )
        session.commit()
        stored = session.get(AuditRow, row.id)
        assert stored is not None
        _assert_clean(
            "audit_events transfer row",
            json.dumps({c.name: getattr(stored, c.name) for c in AuditRow.__table__.columns}, default=str),
        )
    finally:
        session.close()

    _assert_clean("the database after a poisoned transfer record", _dump_database(client.db_path))


# ------------------------------------------------------- scrubber defects found here, now FIXED
# Two REAL defects in ``scrub_secrets`` were found by this file and fixed in
# ``src/noc_agents/services/external_calls.py``:
#   1. the compact (space-stripped) Gmail app password — the form actually transmitted by
#      adapters/email_smtp.py — was not in the known-values set and matched no pattern;
#   2. the key=value pattern used a leading , which can never match after "_", so every
#      PREFIX_WORD=value credential (GMAIL_APP_PASSWORD, SMTP_PASSWORD, AT_API_KEY,
#      X_BEARER_TOKEN, NOC_SESSION_SECRET, ...) slipped through — the exact case the
#      pattern exists for, since it is the fallback for keys this process does not hold.
# These now assert the fixed behaviour. If either regresses, this file turns red.


def test_scrub_secrets_blanks_the_compact_gmail_app_password(client):
    from noc_agents.services.external_calls import scrub_secrets

    assert COMPACT_APP_PASSWORD not in scrub_secrets(f"pasted the app password {COMPACT_APP_PASSWORD}")


def test_scrub_secrets_blanks_a_prefixed_credential_assignment(client):
    from noc_agents.services.external_calls import REDACTED, scrub_secrets

    for line in (
        "GMAIL_APP_PASSWORD=zqweertyuiopasdf",
        "SMTP_PASSWORD=hunter2hunter2hunter2",
        "AT_API_KEY=atsk_abcdefghijklmnop",
        "X_BEARER_TOKEN=AAAAAAAAbbbbbbbb",
        "NOC_SESSION_SECRET=abcdefghijklmnop",
    ):
        assert REDACTED in scrub_secrets(line), f"not redacted: {line}"
