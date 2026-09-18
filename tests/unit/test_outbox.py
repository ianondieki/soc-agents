"""Transactional outbox (spec §7.0.2): every outbound side effect is a row, sent after commit.

What is proved here, in order:

* no email leaves inside the transaction — the SMTP client is monkeypatched and records, at
  the moment ``send_message`` runs, that the writing Session has no transaction open and
  that the incident is already readable from a second Session;
* duplicate ``enqueue`` on one idempotency key → one row;
* ``drain_once`` twice → one send;
* a crash between commit and drain → sent exactly once on the next drain, by a new session;
* a channel row that needs an approval it has not got → REJECTED_UNAPPROVED, never transmitted;
* a CLAIMED row older than the 120 s lease is reclaimed and sent; a fresh claim is left alone;
* transient errors back off with jitter up to max_attempts and then FAILED; programming
  errors are DEAD at once; SMTP 5xx quoted by the adapter is permanent, 4xx transient;
* a locked workbook is the dispatcher's problem: the LEDGER step succeeds, the DB row stays;
* ``release_held`` frees the approved alert's rows and suppresses the superseded ones;
* the HITL release queues approved rows and the drain runs only after that commit.
"""

from __future__ import annotations

import json
import re
import smtplib
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select, update

from noc_agents.adapters.email_smtp import EmailResult
from noc_agents.db.models import (
    AgentRunRow,
    BroadcastRow,
    HitlTaskRow,
    IncidentRow,
    OutboxRow,
    ShiftLedgerRow,
    WorkNoteRow,
    get_session,
    utcnow,
)
from noc_agents.domain.schemas import EventIngest
from noc_agents.graph.pipeline import process_event, release_broadcasts_after_hitl
from noc_agents.orchestrator import outbox
from noc_agents.orchestrator.outbox import dispatch, drain_once, enqueue, release_held
from noc_agents.realtime.hub import hub
from noc_agents.services import notify
from noc_agents.services.ledger import ledger_root

BTS_EVENT = dict(  # P4 at L2_GUARDED: auto-broadcast, so the run queues SMS + EMAIL rows
    site_id="SFC-MTK-BTS-MCH04",
    site_name="Machakos Town BTS",
    site_type="BTS",
    region_code="MTK",
    alarm_code="SITE_DOWN",
    failure_domain="POWER",
    users_affected=3200,
)
HUB_EVENT = dict(  # P2: held at the HITL gate, nothing queued for broadcast until approval
    site_id="SFC-NBIE-HUB-EMB",
    site_name="Embakasi East Aggregation HUB",
    site_type="HUB",
    region_code="NBI_E",
    alarm_code="POWER_GRID_FAIL",
    failure_domain="POWER",
    users_affected=450000,
)
SMTP_ENV = {
    "EMAIL_ENABLED": "true",
    "GMAIL_ADDRESS": "noc@example.com",
    "GMAIL_APP_PASSWORD": "app-password",
    "DEMO_EMAIL_TO": "ops@example.com",
}
MOCK_DETAIL = "No DEMO_EMAIL_TO / GMAIL_ADDRESS — email body stored only (mock)"
LEDGER_FILE_RE = re.compile(r"^ledger_\d{4}-\d{2}-\d{2}_(DAY|NIGHT)\.xlsx$")


@pytest.fixture()
def clean_hub():
    hub._history.clear()
    yield hub
    hub._history.clear()


def _elsewhere(read):
    """Run ``read(session)`` on a fresh Session: only committed rows are visible there."""
    other = get_session()
    try:
        return read(other)
    finally:
        other.close()


def _rows(session, kind: str | None = None) -> list[OutboxRow]:
    stmt = select(OutboxRow).order_by(OutboxRow.created_at, OutboxRow.id)
    if kind:
        stmt = stmt.where(OutboxRow.kind == kind)
    return session.scalars(stmt).all()


def _spy_sends(monkeypatch) -> list[dict]:
    """Count the dispatcher's SMTP-adapter calls without changing what they do."""
    calls: list[dict] = []
    real = notify.transmit_email

    def spy(payload):
        calls.append(payload)
        return real(payload)

    monkeypatch.setattr(notify, "transmit_email", spy)
    return calls


def _email_payload(n: str = "INC000001") -> dict:
    return {
        "operator_id": "safaricom",
        "incident_number": n,
        "audience": "TEST",
        "subject": f"[P4] {n} | test",
        "body": "body",
        "recipients_ref": "DEMO_EMAIL_TO",
        "broadcast_ids": [],
    }


def _events(kind: str) -> list[dict]:
    return [e for e in hub._history if e["type"] == kind]


# --- the point of the change ------------------------------------------------------------------


def test_no_email_leaves_inside_the_transaction(tmp_db, monkeypatch, clean_hub):
    settings, session = tmp_db
    for key, value in SMTP_ENV.items():
        monkeypatch.setenv(key, value)
    sends: list[dict] = []

    class ProbeSMTP:
        """smtplib.SMTP stand-in: records the writer's state at the instant the mail leaves."""

        def __init__(self, host, port, timeout=None):
            self.host, self.port = host, port

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def ehlo(self):
            pass

        def starttls(self, context=None):
            pass

        def login(self, user, password):
            pass

        def send_message(self, msg):
            sends.append(
                {
                    "subject": msg["Subject"],
                    "to": msg["To"],
                    "writer_in_transaction": session.in_transaction(),
                    "incidents_durable": _elsewhere(lambda s: len(s.scalars(select(IncidentRow)).all())),
                    "outbox_claimed_durably": _elsewhere(
                        lambda s: [r.status for r in s.scalars(select(OutboxRow).where(OutboxRow.kind == "EMAIL"))]
                    ),
                }
            )

    monkeypatch.setattr(smtplib, "SMTP", ProbeSMTP)

    inc = process_event(session, settings, EventIngest(**BTS_EVENT))

    assert len(sends) == 1
    (send,) = sends
    assert send["writer_in_transaction"] is False  # the whole point: the SMTP call is outside the transaction
    assert send["incidents_durable"] == 1  # the incident was already committed when the mail left
    assert send["outbox_claimed_durably"] == ["CLAIMED"]  # and the claim itself was committed first
    assert send["to"] == "ops@example.com" and inc.incident_number in send["subject"]

    (row,) = _rows(session, "EMAIL")
    assert (row.status, row.provider, row.attempts, row.last_error) == ("SENT", "smtp", 1, None)
    assert row.sent_at is not None and row.approved_by == "policy:L2_GUARDED"
    assert {b.status for b in session.scalars(select(BroadcastRow))} == {"SENT"}
    notes = [n for n in session.scalars(select(WorkNoteRow)) if n.source == "email"]
    assert len(notes) == 1 and notes[0].author == "BroadcastCommsAgent" and "mode=smtp" in notes[0].body
    (sent,) = _events("email.sent")
    assert sent["run_id"] is None and sent["payload"]["mode"] == "smtp" and sent["payload"]["to"] == ["ops@example.com"]
    types = [e["type"] for e in hub._history]
    assert types.index("email.sent") > types.index("agent.run.finished")


def test_duplicate_enqueue_yields_one_row(tmp_db):
    _settings, session = tmp_db
    first = enqueue(session, kind="EMAIL", idempotency_key="EMAIL:dup", payload={**_email_payload(), "n": 1}, operator_id="safaricom")
    second = enqueue(session, kind="EMAIL", idempotency_key="EMAIL:dup", payload={**_email_payload(), "n": 2}, operator_id="safaricom")
    session.commit()

    assert first.id == second.id
    rows = _rows(session)
    assert len(rows) == 1
    assert json.loads(rows[0].payload_json)["n"] == 1  # the first writer wins; the second is ignored, not merged
    assert (rows[0].status, rows[0].attempts, rows[0].max_attempts, rows[0].requires_hitl) == ("PENDING", 0, 3, 0)


def test_drain_once_twice_sends_once(tmp_db, monkeypatch):
    _settings, session = tmp_db
    sends = _spy_sends(monkeypatch)
    enqueue(session, kind="EMAIL", idempotency_key="EMAIL:once", payload=_email_payload(), operator_id="safaricom")
    session.commit()

    first = drain_once(session)
    assert (first.claimed, first.sent, len(sends)) == (1, 1, 1)
    second = drain_once(session)
    assert (second.claimed, second.sent, len(sends)) == (0, 0, 1)
    (row,) = _rows(session)
    assert (row.status, row.attempts, row.provider) == ("SENT", 1, "mock")


def test_crash_between_commit_and_drain_sends_exactly_once(tmp_db, monkeypatch, clean_hub):
    settings, session = tmp_db
    monkeypatch.setenv("OUTBOX_SYNC_DRAIN", "false")  # the process dies before it can drain
    sends = _spy_sends(monkeypatch)

    inc = process_event(session, settings, EventIngest(**BTS_EVENT))
    session.close()  # ... and its session with it

    # Committed with the incident: the rows and the QUEUED drafts. Nothing has left.
    assert sends == [] and _events("email.sent") == []
    pending = _elsewhere(lambda s: sorted((r.kind, r.status) for r in s.scalars(select(OutboxRow))))
    assert pending == [("EMAIL", "PENDING"), ("EXCEL_ROW", "PENDING"), ("SMS", "PENDING"), ("SMS", "PENDING")]
    assert _elsewhere(lambda s: {b.status for b in s.scalars(select(BroadcastRow))}) == {"QUEUED"}
    assert _elsewhere(lambda s: [n.source for n in s.scalars(select(WorkNoteRow))]) == ["agent"]

    fresh = get_session()  # the next process
    try:
        report = drain_once(fresh)
        assert (report.claimed, report.sent, len(sends)) == (4, 4, 1)
        again = drain_once(fresh)
        assert (again.claimed, len(sends)) == (0, 1)
        assert {r.status for r in _rows(fresh)} == {"SENT"}
        assert {b.status for b in fresh.scalars(select(BroadcastRow))} == {"SENT"}
        assert [n.source for n in fresh.scalars(select(WorkNoteRow))] == ["agent", "email"]
        excel = next(r for r in _rows(fresh) if r.kind == "EXCEL_ROW")
        assert excel.provider_message_id and excel.provider_message_id.endswith(".xlsx")
    finally:
        fresh.close()
    (sent,) = _events("email.sent")
    assert sent["incident_id"] == inc.id and sent["run_id"] is None
    assert sent["payload"] == {
        "incident_number": "INC000001",
        "mode": "mock",
        "to": [],
        "detail": MOCK_DETAIL,
        "status": "SENT",
    }


def test_unapproved_channel_row_is_refused_and_never_transmitted(tmp_db, monkeypatch, clean_hub):
    _settings, session = tmp_db
    sends = _spy_sends(monkeypatch)
    row = enqueue(  # PENDING although it needs an approval nobody gave (e.g. flipped by hand)
        session,
        kind="EMAIL",
        idempotency_key="EMAIL:unapproved",
        payload=_email_payload(),
        incident_id="inc-1",
        requires_hitl=True,
        operator_id="safaricom",
    )
    held = enqueue(
        session,
        kind="SMS",
        idempotency_key="SMS:held",
        payload=_email_payload(),
        incident_id="inc-1",
        requires_hitl=True,
        held=True,
        operator_id="safaricom",
    )
    session.commit()

    assert dispatch(row).status == "REJECTED_UNAPPROVED"  # the pure routing decision, no session involved

    report = drain_once(session)
    assert (report.claimed, report.rejected, report.sent, sends) == (1, 1, 0, [])
    session.expire_all()
    assert row.status == "REJECTED_UNAPPROVED" and "approval" in row.last_error
    assert held.status == "HELD" and held.attempts == 0  # HELD rows are invisible to the drain
    (failed,) = _events("outbox.failed")
    assert failed["payload"]["status"] == "REJECTED_UNAPPROVED" and failed["payload"]["outbox_id"] == row.id
    (mail_failed,) = _events("email.failed")
    assert mail_failed["payload"]["status"] == "FAILED" and mail_failed["payload"]["mode"] == "error"


def test_stale_claimed_row_is_reclaimed_after_the_lease(tmp_db, monkeypatch):
    _settings, session = tmp_db
    sends = _spy_sends(monkeypatch)
    now = utcnow()
    stale = enqueue(session, kind="EMAIL", idempotency_key="EMAIL:stale", payload=_email_payload(), operator_id="safaricom")
    fresh = enqueue(session, kind="EMAIL", idempotency_key="EMAIL:fresh", payload=_email_payload(), operator_id="safaricom")
    session.execute(  # a drainer claimed it 121 s ago and died
        update(OutboxRow)
        .where(OutboxRow.id == stale.id)
        .values(status="CLAIMED", claimed_at=now - timedelta(seconds=121), claimed_by="worker-that-died", attempts=1)
    )
    session.execute(  # another drainer is legitimately mid-flight (10 s into its lease)
        update(OutboxRow)
        .where(OutboxRow.id == fresh.id)
        .values(status="CLAIMED", claimed_at=now - timedelta(seconds=10), claimed_by="worker-alive", attempts=1)
    )
    session.commit()

    report = drain_once(session, now=now, worker="worker-two")
    assert (report.reclaimed, report.claimed, report.sent, len(sends)) == (1, 1, 1, 1)
    session.expire_all()
    assert (stale.status, stale.attempts, stale.claimed_by) == ("SENT", 2, "worker-two")  # the crashed attempt counted
    assert (fresh.status, fresh.attempts, fresh.claimed_by) == ("CLAIMED", 1, "worker-alive")  # lease respected


def test_stale_claim_with_no_attempts_left_is_closed_as_failed(tmp_db, monkeypatch):
    _settings, session = tmp_db
    sends = _spy_sends(monkeypatch)
    now = utcnow()
    row = enqueue(session, kind="EMAIL", idempotency_key="EMAIL:exhausted", payload=_email_payload(), operator_id="safaricom")
    session.execute(
        update(OutboxRow)
        .where(OutboxRow.id == row.id)
        .values(status="CLAIMED", claimed_at=now - timedelta(seconds=500), claimed_by="worker-that-died", attempts=3)
    )
    session.commit()

    report = drain_once(session, now=now)
    assert (report.reclaimed, report.claimed, sends) == (0, 0, [])
    session.expire_all()
    assert row.status == "FAILED" and "no attempts left" in row.last_error


# --- retries ------------------------------------------------------------------------------------


def test_transient_error_retries_with_backoff_then_fails(tmp_db, monkeypatch, clean_hub):
    _settings, session = tmp_db

    def locked(*_args, **_kwargs):
        raise PermissionError("[Errno 13] Permission denied: 'ledger_2026-09-16_DAY.xlsx'")

    monkeypatch.setattr(outbox, "append_excel_row", locked)
    row = enqueue(
        session,
        kind="EXCEL_ROW",
        idempotency_key="EXCEL_ROW:retry",
        payload={"operator_id": "safaricom", "incident_number": "INC000009", "file": "ledger_2026-09-16_DAY.xlsx", "cells": []},
        operator_id="safaricom",
    )
    session.commit()
    t0 = utcnow()

    first = drain_once(session, now=t0)
    assert (first.claimed, first.retried, first.failed) == (1, 1, 0)
    session.expire_all()
    assert (row.status, row.attempts, row.claimed_at, row.claimed_by) == ("PENDING", 1, None, None)
    assert row.last_error.startswith("PermissionError: [Errno 13]")
    assert t0 + timedelta(seconds=2) <= row.next_attempt_at <= t0 + timedelta(seconds=3)  # 2**1 + jitter <= half

    assert drain_once(session, now=t0 + timedelta(seconds=1)).claimed == 0  # not due yet
    second = drain_once(session, now=t0 + timedelta(seconds=100))
    assert (second.claimed, second.retried) == (1, 1)
    third = drain_once(session, now=t0 + timedelta(seconds=200))
    assert (third.claimed, third.retried, third.failed) == (1, 0, 1)
    session.expire_all()
    assert (row.status, row.attempts) == ("FAILED", 3)
    assert drain_once(session, now=t0 + timedelta(seconds=300)).claimed == 0
    (failed,) = _events("outbox.failed")
    assert failed["payload"] == {
        "outbox_id": row.id,
        "kind": "EXCEL_ROW",
        "incident_number": "INC000009",
        "status": "FAILED",
        "attempts": 3,
        "error": "PermissionError: [Errno 13] Permission denied: 'ledger_2026-09-16_DAY.xlsx'",
    }


def test_programming_error_is_dead_at_once(tmp_db, monkeypatch):
    _settings, session = tmp_db

    def bug(*_args, **_kwargs):
        raise TypeError("cells must be a list")

    monkeypatch.setattr(outbox, "append_excel_row", bug)
    row = enqueue(
        session,
        kind="EXCEL_ROW",
        idempotency_key="EXCEL_ROW:bug",
        payload={"operator_id": "safaricom", "file": "ledger_2026-09-16_DAY.xlsx", "cells": None},
        operator_id="safaricom",
    )
    unknown = enqueue(session, kind="PIR_OPEN", idempotency_key="PIR:1", payload={"operator_id": "safaricom"}, operator_id="safaricom")
    session.commit()

    report = drain_once(session)
    assert (report.claimed, report.dead, report.retried) == (2, 2, 0)
    session.expire_all()
    assert (row.status, row.attempts, row.last_error) == ("DEAD", 1, "TypeError: cells must be a list")
    assert unknown.status == "DEAD" and "no transmitter" in unknown.last_error
    assert drain_once(session).claimed == 0


def test_smtp_5xx_from_the_adapter_is_dead_and_4xx_transient(tmp_db, monkeypatch, clean_hub):
    _settings, session = tmp_db
    inc_id = "inc-smtp"
    draft = BroadcastRow(incident_id=inc_id, channel="EMAIL", audience="RNIO", message="m", status="QUEUED")
    session.add(draft)
    session.flush()
    payload = {**_email_payload(), "broadcast_ids": [draft.id]}
    auth = enqueue(session, kind="EMAIL", idempotency_key="EMAIL:535", payload=payload, incident_id=inc_id, operator_id="safaricom")
    grey = enqueue(session, kind="EMAIL", idempotency_key="EMAIL:421", payload=_email_payload(), incident_id=inc_id, operator_id="safaricom")
    session.commit()
    replies = {
        auth.id: "SMTP send failed: SMTPAuthenticationError: (535, b'5.7.8 Username and Password not accepted')",
        grey.id: "SMTP send failed: SMTPResponseException: (421, b'4.7.0 Try again later')",
    }
    # both payloads are identical; tell the rows apart by the order the drain claims them (created_at, id)
    order = [r.id for r in _rows(session, "EMAIL")]
    calls: list[str] = []

    def adapter(payload):
        rid = order[len(calls)]
        calls.append(rid)
        return EmailResult(ok=False, mode="error", detail=replies[rid], to=["ops@example.com"])

    monkeypatch.setattr(notify, "transmit_email", adapter)
    report = drain_once(session)
    assert calls == order
    assert (report.dead, report.retried) == (1, 1)
    session.expire_all()
    assert (auth.status, auth.attempts) == ("DEAD", 1)
    assert (grey.status, grey.attempts) == ("PENDING", 1) and grey.next_attempt_at is not None
    assert draft.status == "FAILED" and draft.sent_at is None  # the dead row's draft is closed out
    (mail_failed,) = _events("email.failed")
    assert mail_failed["payload"]["detail"] == replies[auth.id] and mail_failed["payload"]["status"] == "FAILED"
    note = next(n for n in session.scalars(select(WorkNoteRow)) if n.source == "email")
    assert note.incident_id == inc_id and "mode=error" in note.body and "535" in note.body


@pytest.mark.parametrize(
    "exc, transient",
    [
        (OSError("io"), True),
        (PermissionError("locked"), True),
        (TimeoutError("timed out"), True),
        (ConnectionRefusedError(), True),
        (smtplib.SMTPServerDisconnected("Connection unexpectedly closed"), True),
        (smtplib.SMTPResponseException(421, b"try later"), True),
        (smtplib.SMTPAuthenticationError(535, b"bad creds"), False),
        (smtplib.SMTPRecipientsRefused({"a@b": (550, b"no such user")}), False),
        (smtplib.SMTPRecipientsRefused({"a@b": (450, b"greylisted")}), True),
        (httpx.TransportError("net"), True),
        (httpx.HTTPStatusError("rate", request=httpx.Request("GET", "http://x"), response=httpx.Response(429)), True),
        (httpx.HTTPStatusError("down", request=httpx.Request("GET", "http://x"), response=httpx.Response(503)), True),
        (httpx.HTTPStatusError("bad", request=httpx.Request("GET", "http://x"), response=httpx.Response(400)), False),
        (TypeError("bug"), False),
        (KeyError("k"), False),
        (ValueError("v"), False),
    ],
)
def test_transient_classification(exc, transient):
    assert outbox.is_transient(exc) is transient


# --- the LEDGER node no longer touches the file ----------------------------------------------------


def test_locked_workbook_is_the_dispatchers_problem_not_the_runs(tmp_db, monkeypatch, clean_hub, tmp_path):
    settings, session = tmp_db
    monkeypatch.setenv("LEDGER_DIR", str(tmp_path / "ledgers"))  # this test's own folder: no earlier workbook
    real = outbox.append_excel_row
    blocked = [True]

    def workbook(*args, **kwargs):
        if blocked[0]:
            raise PermissionError("[Errno 13] Permission denied: 'ledger_2026-09-16_DAY.xlsx'")
        return real(*args, **kwargs)

    monkeypatch.setattr(outbox, "append_excel_row", workbook)

    inc = process_event(session, settings, EventIngest(**HUB_EVENT))  # the sync drain runs and the append fails

    run = session.scalar(select(AgentRunRow).where(AgentRunRow.incident_id == inc.id))
    assert (run.status, run.error_summary) == ("WAITING_HITL", None)
    assert [s.status for s in run.steps].count("FAILED") == 0 and len(run.steps) == 12
    ledger = next(s for s in run.steps if s.node_name == "LEDGER")
    assert ledger.status == "SUCCEEDED" and LEDGER_FILE_RE.match(ledger.output_summary)
    assert ledger.tools_called == [{"name": "outbox.enqueue", "ok": True, "latency_ms": 2, "error": None}]
    assert len(session.scalars(select(ShiftLedgerRow)).all()) == 1  # the DB row is written inside the node
    (excel,) = _rows(session, "EXCEL_ROW")
    assert (excel.status, excel.attempts) == ("PENDING", 1)
    assert excel.last_error.startswith("PermissionError: [Errno 13]")
    assert not (ledger_root() / "safaricom" / ledger.output_summary).exists()

    blocked[0] = False  # Excel is closed; the next drain appends the row
    report = drain_once(session, now=utcnow() + timedelta(seconds=100))
    assert (report.claimed, report.sent) == (1, 1)
    assert (ledger_root() / "safaricom" / ledger.output_summary).exists()


# --- HITL ------------------------------------------------------------------------------------------


def test_release_held_releases_the_new_alert_and_suppresses_the_old(tmp_db, monkeypatch):
    _settings, session = tmp_db
    sends = _spy_sends(monkeypatch)
    old = enqueue(session, kind="EMAIL", idempotency_key="EMAIL:v1", payload=_email_payload(), incident_id="inc-h", alert_id="alert-1", requires_hitl=True, held=True, operator_id="safaricom")
    new = enqueue(session, kind="EMAIL", idempotency_key="EMAIL:v2", payload=_email_payload(), incident_id="inc-h", alert_id="alert-2", requires_hitl=True, held=True, operator_id="safaricom")
    session.commit()
    assert drain_once(session).claimed == 0  # HELD rows are never claimed

    approved_at = utcnow()
    released = release_held(session, incident_id="inc-h", alert_id="alert-2", approved_by="Supervisor A", approved_at=approved_at)
    session.commit()
    assert released == 1
    session.expire_all()
    assert (new.status, new.approved_by, new.approved_at) == ("PENDING", "Supervisor A", approved_at)
    assert old.status == "SUPPRESSED" and "alert-2" in old.last_error

    report = drain_once(session)
    assert (report.claimed, report.sent, len(sends)) == (1, 1, 1)
    session.expire_all()
    assert new.status == "SENT" and old.status == "SUPPRESSED"


def test_hitl_release_queues_approved_rows_and_drains_only_after_the_commit(tmp_db, monkeypatch, clean_hub):
    settings, session = tmp_db
    sends = _spy_sends(monkeypatch)
    inc = process_event(session, settings, EventIngest(**HUB_EVENT))  # P2: gated
    assert [r.kind for r in _rows(session)] == ["EXCEL_ROW"]  # nothing queued for the broadcast while gated
    assert {b.status for b in session.scalars(select(BroadcastRow))} == {"PENDING_HITL"}

    # What main.hitl_approve does around the release: resolve the task, release, commit.
    task = session.scalar(select(HitlTaskRow).where(HitlTaskRow.incident_id == inc.id))
    task.status, task.resolved_by, task.resolved_at = "APPROVED", "Supervisor A", utcnow()
    release_broadcasts_after_hitl(session, inc.id)

    assert session.in_transaction() and sends == []  # queued, not sent
    queued = [r for r in _rows(session) if r.kind in ("EMAIL", "SMS")]
    assert sorted(r.kind for r in queued) == ["EMAIL", "SMS", "SMS", "SMS", "SMS"]
    assert {(r.status, r.requires_hitl, r.approved_by) for r in queued} == {("PENDING", 1, "Supervisor A")}
    assert all(r.approved_at is not None for r in queued)
    assert {b.status for b in session.scalars(select(BroadcastRow))} == {"QUEUED"}
    assert _elsewhere(lambda s: [r.kind for r in s.scalars(select(OutboxRow))]) == ["EXCEL_ROW"]  # not committed yet

    session.commit()  # the once-only after_commit listener drains in a session of its own

    assert len(sends) == 1
    assert _elsewhere(lambda s: {b.status for b in s.scalars(select(BroadcastRow))}) == {"SENT"}
    assert _elsewhere(lambda s: {r.status for r in s.scalars(select(OutboxRow))}) == {"SENT"}
    note = next(n for n in _elsewhere(lambda s: s.scalars(select(WorkNoteRow)).all()) if n.source == "email")
    assert note.author == "BroadcastCommsAgent" and "HITL_APPROVED" in note.body and "mode=mock" in note.body
    (sent,) = _events("email.sent")
    assert sent["incident_id"] == inc.id and sent["payload"]["detail"] == MOCK_DETAIL
    session.execute(select(OutboxRow))  # a later commit on the same session must not drain again
    session.commit()
    assert len(sends) == 1


def test_hitl_release_with_sync_drain_off_leaves_rows_for_the_next_drain(tmp_db, monkeypatch):
    settings, session = tmp_db
    monkeypatch.setenv("OUTBOX_SYNC_DRAIN", "false")
    sends = _spy_sends(monkeypatch)
    inc = process_event(session, settings, EventIngest(**HUB_EVENT))
    release_broadcasts_after_hitl(session, inc.id, approved_by="Duty Manager")
    session.commit()

    assert sends == []
    assert {r.status for r in _rows(session, "EMAIL")} == {"PENDING"}
    assert _rows(session, "EMAIL")[0].approved_by == "Duty Manager"
    report = drain_once(session)
    assert report.sent == 6 and len(sends) == 1  # 1 EMAIL + 4 SMS + the run's EXCEL_ROW


# --- handover -------------------------------------------------------------------------------------


def test_handover_email_is_queued_then_sent_by_the_drain(tmp_db, monkeypatch):
    _settings, session = tmp_db
    sends = _spy_sends(monkeypatch)
    queued = notify.dispatch_handover_email(session, "[SAFARICOM] handover", "body", operator_id="safaricom", shift_id="safaricom-2026-09-17-DAY")
    assert queued["status"] == "PENDING" and queued["mode"] == "outbox" and sends == []
    session.commit()

    report = drain_once(session)
    assert notify.handover_email_response(session, queued["outbox_id"], report) == {
        "ok": True,
        "mode": "mock",
        "detail": MOCK_DETAIL,
        "to": [],
        "status": "SENT",
        "outbox_id": queued["outbox_id"],
    }
    assert len(sends) == 1 and sends[0]["subject"] == "[SAFARICOM] handover"
    assert [n.source for n in session.scalars(select(WorkNoteRow))] == []  # no incident, no note

    again = notify.dispatch_handover_email(session, "[SAFARICOM] handover", "body", operator_id="safaricom", shift_id="safaricom-2026-09-17-DAY")
    session.commit()
    assert again["outbox_id"] != queued["outbox_id"]  # each POST is a deliberate send, as before
    assert notify.handover_email_response(session, again["outbox_id"], None)["status"] == "PENDING"
