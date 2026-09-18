"""Degraded mode (spec §8, Phase 1): the NOC still works with NOTHING optional available.

This is the property that lets the system run on a bad day in Nairobi — no Console credit,
no SMTP relay, no MCP server, no upstream link — and it is deliberately the strictest
environment the suite constructs:

* **No LLM.** ``LLM_ENABLED=false``, no credential, and the optional ``anthropic`` package is
  not importable. The suite does not merely *check* that it is absent (it is, in this
  environment): a ``sys.meta_path`` blocker makes ``import anthropic`` raise for the duration
  of every test here, so the lifecycle is proved to need no part of it even on a machine
  where somebody has run ``pip install .[llm]``.
* **No MCP runtime.** ``mcp`` is blocked the same way. The MCP *requirement cards* in the
  registry are declarative and must stay renderable with no client installed.
* **No email credentials.** ``EMAIL_ENABLED=false`` and every credential variable is empty,
  so the SMTP adapter has nothing to authenticate with.
* **No network.** Every outbound TCP connect and every DNS lookup to a non-loopback host
  raises and is recorded. Loopback is left alone because the ASGI test transport and the
  Windows ``socketpair`` the event loop builds itself are not "the network".

What is proved, in order:

1. the optional extras are genuinely absent from this environment (recorded, not assumed);
2. the whole 12-node lifecycle runs green for an auto-broadcast (P4) incident and for a
   gated (P2) one, including the HITL approval and the close — with no network at all;
3. the **templates carry the load**: the SMS, email, exec-brief and Excel-ledger content is
   the deterministic composition text, quoting the incident number, priority, site and
   region label — not an empty string standing in for a model that never answered;
4. **nothing silently no-ops**: every outbox row reaches SENT, the .xlsx is really on disk
   with the incident in it, and the degrade itself is written into the ticket's work notes
   (``mode=mock``) so an auditor can see what did and did not leave the building;
5. the on-demand LLM routes answer ``source="template"`` with real content rather than
   500-ing or returning a stub;
6. no outbound connection was attempted anywhere in any of the above.
"""

from __future__ import annotations

import importlib
import os
import socket
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import select

from noc_agents.db.models import (
    BroadcastRow,
    IncidentBriefRow,
    IncidentRow,
    OutboxRow,
    ShiftLedgerRow,
    WorkNoteRow,
    get_session,
)
from noc_agents.realtime.hub import hub

# Every optional extra declared in pyproject. None of them may be needed to run a shift.
OPTIONAL_EXTRAS: tuple[str, ...] = (
    "anthropic",
    "mcp",
    "africastalking",
    "pdfplumber",
    "feedparser",
    "icalendar",
    "sqlite_vec",
    "model2vec",
)

# Hostnames the sandbox may legitimately talk to: the in-process ASGI transport never opens a
# socket at all, but asyncio on Windows builds its self-pipe with socket.socketpair(), which
# connects to 127.0.0.1. Blocking that would test the event loop, not the NOC.
LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost", "0.0.0.0", "", None})

BTS_EVENT = {  # P4 at L2_GUARDED: no HITL gate, the broadcast goes out on the auto path
    "site_id": "SFC-MTK-BTS-MCH04",
    "site_name": "Machakos Town BTS",
    "site_type": "BTS",
    "region_code": "MTK",
    "alarm_code": "SITE_DOWN",
    "failure_domain": "POWER",
    "users_affected": 3200,
    "access_notes": "Genset tank empty",
}
HUB_EVENT = {  # P2: held at the HITL gate, so the approval path is exercised too
    "site_id": "SFC-NBIE-HUB-EMB",
    "site_name": "Embakasi East Aggregation HUB",
    "site_type": "HUB",
    "region_code": "NBI_E",
    "alarm_code": "POWER_GRID_FAIL",
    "failure_domain": "POWER",
    "users_affected": 450000,
}
MOCK_DETAIL = "No DEMO_EMAIL_TO / GMAIL_ADDRESS — email body stored only (mock)"
TERMINAL_OK = "SENT"


class _ExtrasBlocked:
    """Meta-path finder that makes the optional extras unimportable for this test module."""

    def find_spec(self, fullname, path=None, target=None):  # noqa: D401, ANN001
        root = fullname.split(".")[0]
        if root in OPTIONAL_EXTRAS:
            raise ImportError(
                f"degraded mode: {fullname!r} is an optional extra and must not be required "
                "for the incident lifecycle"
            )
        return None


@pytest.fixture()
def extras_absent():
    """``import anthropic`` / ``import mcp`` raise ImportError for the whole test."""
    blocker = _ExtrasBlocked()
    sys.meta_path.insert(0, blocker)
    cached = {name: sys.modules.pop(name) for name in list(sys.modules) if name.split(".")[0] in OPTIONAL_EXTRAS}
    try:
        yield blocker
    finally:
        sys.meta_path.remove(blocker)
        sys.modules.update(cached)


@pytest.fixture()
def no_network(monkeypatch):
    """Record and refuse every non-loopback connect / DNS lookup. Yields the attempt list."""
    attempts: list[object] = []
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def _host_of(address) -> object:
        return address[0] if isinstance(address, tuple) and address else address

    def guarded_connect(self, address, *args, **kwargs):
        host = _host_of(address)
        if host not in LOOPBACK:
            attempts.append(("connect", address))
            raise AssertionError(f"degraded mode: outbound TCP connect attempted to {address!r}")
        return real_connect(self, address, *args, **kwargs)

    def guarded_connect_ex(self, address, *args, **kwargs):
        host = _host_of(address)
        if host not in LOOPBACK:
            attempts.append(("connect_ex", address))
            raise AssertionError(f"degraded mode: outbound TCP connect attempted to {address!r}")
        return real_connect_ex(self, address, *args, **kwargs)

    def guarded_create_connection(address, *args, **kwargs):
        attempts.append(("create_connection", address))
        raise AssertionError(f"degraded mode: outbound TCP connect attempted to {address!r}")

    def guarded_getaddrinfo(host, port, *args, **kwargs):
        if host not in LOOPBACK:
            attempts.append(("getaddrinfo", host))
            raise AssertionError(f"degraded mode: DNS lookup attempted for {host!r}")
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect, raising=True)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex, raising=True)
    monkeypatch.setattr(socket, "create_connection", guarded_create_connection, raising=True)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo, raising=True)
    return attempts


@pytest.fixture()
def client(tmp_path, monkeypatch, extras_absent, no_network):
    """A NOC with nothing optional: no LLM, no mail credentials, no MCP, no network."""
    db = tmp_path / "degraded.db"
    ledgers = tmp_path / "ledgers"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")
    monkeypatch.setenv("OPERATOR_PROFILE", "safaricom")
    monkeypatch.setenv("LIVE_AGENT_DELAY_MS", "0")
    monkeypatch.setenv("LEDGER_DIR", str(ledgers))
    monkeypatch.setenv("LLM_ENABLED", "false")
    monkeypatch.setenv("EMAIL_ENABLED", "false")
    for key in (
        "LLM_PROVIDER",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "GMAIL_ADDRESS",
        "GMAIL_APP_PASSWORD",
        "SMTP_USER",
        "SMTP_PASSWORD",
        "DEMO_EMAIL_TO",
    ):
        monkeypatch.setenv(key, "")

    import noc_agents.config as cfg
    import noc_agents.db.models as models
    import noc_agents.main as main

    cfg.clear_settings_cache()
    models._engine = None
    models.SessionLocal = None
    importlib.reload(main)
    hub._history.clear()
    with TestClient(main.app) as c:
        c.ledger_dir = ledgers  # type: ignore[attr-defined]
        yield c
    hub._history.clear()


# --- helpers ---------------------------------------------------------------------------------


def _read(fn):
    session = get_session()
    try:
        return fn(session)
    finally:
        session.close()


def _outbox(incident_id: str) -> list[OutboxRow]:
    return _read(
        lambda s: list(
            s.scalars(
                select(OutboxRow).where(OutboxRow.incident_id == incident_id).order_by(OutboxRow.created_at, OutboxRow.id)
            )
        )
    )


def _notes(incident_id: str) -> list[str]:
    return _read(
        lambda s: [
            n.body
            for n in s.scalars(
                select(WorkNoteRow).where(WorkNoteRow.incident_id == incident_id).order_by(WorkNoteRow.created_at)
            )
        ]
    )


def _broadcasts(incident_id: str) -> list[BroadcastRow]:
    return _read(lambda s: list(s.scalars(select(BroadcastRow).where(BroadcastRow.incident_id == incident_id))))


def _ingest(client, event: dict) -> dict:
    r = client.post("/api/v1/events", json=event)
    assert r.status_code == 200, r.text
    return r.json()["incident"]


# --- 1. the environment really is bare --------------------------------------------------------


def test_optional_extras_are_absent_and_unimportable(extras_absent):
    """Recorded, not assumed: neither the LLM SDK nor the MCP client is installed here, and
    the blocker this module installs makes them unimportable regardless."""
    for name in OPTIONAL_EXTRAS:
        with pytest.raises(ImportError):
            importlib.import_module(name)

    from noc_agents.llm import client as llm_client

    assert llm_client.sdk_importable() is False
    # Nothing under noc_agents may import either package at module scope: importing the whole
    # package tree with the blocker active is the proof.
    for module in ("noc_agents.main", "noc_agents.llm.client", "noc_agents.llm.assist", "noc_agents.orchestrator.registry"):
        importlib.import_module(module)


# --- 2. the lifecycle completes ---------------------------------------------------------------


def test_auto_broadcast_lifecycle_completes_with_nothing_optional(client, no_network):
    """P4 alarm → ticket → assignment → broadcast → ledger → brief → close, all offline."""
    inc = _ingest(client, BTS_EVENT)
    assert inc["incident_number"].startswith("INC")
    assert inc["priority"] == "P4"
    assert inc["assignee_name"], "assignment must still happen with no optional service available"
    assert inc["sla_restore_due"], "SLA clock must still be set"

    # Every node of the 12-node graph ran and succeeded — nothing skipped, nothing failed.
    wf = client.get(f"/api/v1/incidents/{inc['id']}/workflow").json()
    statuses = {n["id"]: n["status"] for n in wf["nodes"]}
    assert set(statuses.values()) == {"succeeded"}, statuses
    assert len(wf["steps"]) == 12
    assert [s["status"] for s in wf["steps"]] == ["SUCCEEDED"] * 12

    # The incident closes through the normal route.
    r = client.post(
        f"/api/v1/incidents/{inc['id']}/notes",
        json={"author": "NOC", "author_role": "NOC", "body": "Genset refuelled, site up.", "mark_restored": True},
    )
    assert r.status_code == 200, r.text
    r = client.post(
        f"/api/v1/incidents/{inc['id']}/close",
        json={"closed_by": "NOC", "resolution_code": "POWER_RESTORED", "resolution_summary": "Genset refuelled"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["incident"]["status"] == "CLOSED"

    assert no_network == [], f"the offline lifecycle reached for the network: {no_network}"


def test_hitl_gated_lifecycle_completes_with_nothing_optional(client, no_network):
    """A P2 hub outage: held at the gate, approved by a supervisor, broadcast released — offline."""
    inc = _ingest(client, HUB_EVENT)
    assert inc["priority"] in ("P1", "P2")
    assert inc["requires_hitl"] is True
    assert {b.status for b in _broadcasts(inc["id"])} == {"PENDING_HITL"}

    pending = client.get("/api/v1/hitl/pending").json()
    task = next(t for t in pending if t["incident_id"] == inc["id"] and t["task_type"] == "APPROVE_BROADCAST")
    r = client.post(f"/api/v1/hitl/{task['id']}/approve", json={"resolved_by": "Supervisor A"})
    assert r.status_code == 200, r.text

    # The approval released the held drafts and the dispatcher transmitted them — in mock mode.
    assert "PENDING_HITL" not in {b.status for b in _broadcasts(inc["id"])}
    kinds = {row.kind: row.status for row in _outbox(inc["id"])}
    assert kinds.get("EMAIL") == TERMINAL_OK, kinds
    assert client.get(f"/api/v1/incidents/{inc['id']}").json()["hitl_state"] == "APPROVED"
    assert no_network == [], f"the offline HITL path reached for the network: {no_network}"


# --- 3. the templates carry the load ----------------------------------------------------------


def test_templates_produce_the_operator_facing_content(client):
    """With no model in the building the SMS, email and exec brief are still real, specific text."""
    inc = _ingest(client, BTS_EVENT)
    number, site, priority = inc["incident_number"], BTS_EVENT["site_id"], inc["priority"]

    by_channel: dict[str, str] = {b.channel: b.message for b in _broadcasts(inc["id"])}
    assert set(by_channel) == {"SMS", "EMAIL"}

    sms = by_channel["SMS"]
    assert number in sms and site in sms and f"[{priority}]" in sms
    assert "est.users 3200" in sms
    assert inc["assignee_name"] in sms

    email = by_channel["EMAIL"]
    assert email.startswith("Subject:")
    assert number in email and "Machakos Town BTS" in email
    assert "| Mt Kenya |" in email, "the region LABEL must be rendered, not the raw code"
    assert "Est. users: 3200" in email
    assert "Narrative:" in email and "Hypothesis:" in email
    assert "Genset tank empty" in email, "the enrichment text must survive into the template body"
    assert len(email.splitlines()) > 8, "the template must be the full email, not a stub line"

    brief = client.get(f"/api/v1/briefs/{inc['id']}")
    assert brief.status_code == 200, brief.text
    body = brief.json()["body"]
    assert number in body and priority in body and "Est. users: 3,200" in body
    assert "What we know:" in body and "Impact:" in body


def test_llm_routes_fall_back_to_templates_not_to_errors(client, no_network):
    """The on-demand assist routes answer with deterministic template content, never a 5xx."""
    inc = _ingest(client, BTS_EVENT)

    status = client.get("/api/v1/llm/status").json()
    assert status["enabled"] is False
    assert status["sdk_installed"] is False
    assert status["credential_present"] is False

    r = client.post(f"/api/v1/incidents/{inc['id']}/analysis")
    assert r.status_code == 200, r.text
    analysis = r.json()
    assert analysis["source"] == "template"
    assert analysis["model"] is None
    assert analysis["analysis"]["summary"].strip(), "the template analysis must say something"
    assert analysis["analysis"]["hypotheses"], "the template must still rank hypotheses"

    r = client.post(f"/api/v1/incidents/{inc['id']}/brief/draft")
    assert r.status_code == 200, r.text
    draft = r.json()
    assert draft["source"] == "template"
    assert draft["model"] is None
    assert inc["incident_number"] in draft["body"]

    assert no_network == [], f"an assist route reached for the network with the LLM off: {no_network}"


# --- 4. nothing silently no-ops ---------------------------------------------------------------


def test_every_side_effect_reaches_a_terminal_sent_state(client):
    """No outbox row is left PENDING, FAILED, DEAD or REJECTED because an optional service is missing."""
    inc = _ingest(client, BTS_EVENT)
    rows = _outbox(inc["id"])
    assert {r.kind for r in rows} == {"SMS", "EMAIL", "EXCEL_ROW"}
    bad = [(r.kind, r.status, r.last_error) for r in rows if r.status != TERMINAL_OK]
    assert bad == [], f"side effects did not complete in degraded mode: {bad}"
    assert all(r.sent_at is not None for r in rows)
    assert all(r.attempts == 1 for r in rows), "a degraded send must not burn retries"


def test_the_degrade_is_written_down_not_hidden(client):
    """A mock send is only honest if the ticket says so: the email work note quotes mode=mock."""
    inc = _ingest(client, BTS_EVENT)
    email_notes = [n for n in _notes(inc["id"]) if n.startswith("[BroadcastCommsAgent] EMAIL")]
    assert len(email_notes) == 1, _notes(inc["id"])
    note = email_notes[0]
    assert "mode=mock" in note, note
    assert MOCK_DETAIL in note, note
    assert "to=['(none)']" in note, note

    # And the SMS rows say the adapter is not there rather than pretending it delivered.
    sms = [r for r in _outbox(inc["id"]) if r.kind == "SMS"]
    assert sms and all(r.provider == "mock" for r in sms)

    # The email/SMS status surfaces the same fact to the UI.
    es = client.get("/api/v1/email/status").json()
    assert es["configured"] is False
    assert es["provider"] == "mock"
    assert es["recipients"] == []


def test_the_excel_ledger_is_really_written_offline(client):
    """The .xlsx append is local file I/O and must still happen: row on disk, row in the DB."""
    inc = _ingest(client, BTS_EVENT)
    folder = Path(client.ledger_dir) / "safaricom"  # type: ignore[attr-defined]
    files = sorted(folder.glob("ledger_*.xlsx"))
    assert len(files) == 1, f"no shift ledger workbook written: {list(folder.glob('*')) if folder.exists() else folder}"

    ws = load_workbook(files[0]).active
    rows = list(ws.iter_rows(values_only=True))
    assert rows[0][:3] == ("Time (EAT)", "Incident No", "Priority")
    assert any(r[1] == inc["incident_number"] for r in rows[1:]), rows

    db_rows = _read(
        lambda s: list(s.scalars(select(ShiftLedgerRow).where(ShiftLedgerRow.incident_number == inc["incident_number"])))
    )
    assert len(db_rows) == 1
    assert db_rows[0].operator_id == "safaricom"

    assert _read(lambda s: len(list(s.scalars(select(IncidentBriefRow).where(IncidentBriefRow.incident_id == inc["id"]))))) == 1


def test_read_surfaces_stay_up_with_nothing_optional(client, no_network):
    """The screens a supervisor actually opens during an outage all answer 200 while degraded."""
    inc = _ingest(client, BTS_EVENT)
    for path in (
        "/health",
        "/api/v1/profile",
        "/api/v1/agents",
        "/api/v1/incidents",
        "/api/v1/runs",
        "/api/v1/metrics/summary",
        "/api/v1/problems",
        "/api/v1/audit",
        "/api/v1/shifts/current",
        "/api/v1/shifts/ledger",
        "/api/v1/hitl/pending",
        f"/api/v1/incidents/{inc['id']}/timeline",
    ):
        r = client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code} {r.text}"

    # The MCP requirement cards are declarative: they must render with no client installed.
    agents = client.get("/api/v1/agents").json()
    assert agents, "the agent catalogue must render without the mcp package"
    assert any(a.get("mcp") for a in agents), "MCP requirement cards disappeared when mcp was unimportable"

    assert _read(lambda s: s.scalar(select(IncidentRow.id).where(IncidentRow.id == inc["id"]))) == inc["id"]
    assert no_network == [], f"a read surface reached for the network: {no_network}"


def test_the_whole_module_never_touched_the_network(no_network):
    """Sanity check on the guard itself: it really does refuse an outbound connect."""
    with pytest.raises(AssertionError):
        socket.create_connection(("smtp.gmail.com", 587), timeout=1)
    assert no_network and no_network[-1][0] == "create_connection"
