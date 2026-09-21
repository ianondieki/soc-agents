"""The outbox dead-letter view and retry (spec §10.4) -- conformance item C-10.

``GET /api/v1/outbox?status=FAILED|DEAD`` and ``POST /api/v1/outbox/{id}/retry``. What is
proved here, in order:

* the view lists FAILED and DEAD rows only, newest first, filtered as asked, and 422s a status
  that is not a dead letter;
* it is a SUMMARY: no payload, envelope, idempotency key or approver, and ``last_error`` with
  e-mail addresses and MSISDNs scrubbed -- checked against a row that carries all of them;
* a retry re-queues the SAME row (same id, same idempotency key, no new row), transmits nothing
  itself, and the next drain sends it exactly once; a second retry is a 409, not a second send;
* ``attempts`` is never reset, so a spent budget buys exactly one attempt per retry;
* every status that is not a dead letter is refused with a 409 and left alone, as are a channel
  row with no approval and a row whose payload housekeeping archived; the compare-and-set
  refuses even when the pre-check is bypassed;
* another operator's row is invisible to the view and a 404 to the retry;
* §9.3: the view is for the platform readers, the retry is admin's, with auth enforced; the
  audit row names the principal.

Rows are driven to FAILED/DEAD by the real ``drain_once`` with the transmitters monkeypatched,
the same way ``test_outbox.py`` does it, except where a status the machine cannot reach on its
own is needed (those are set directly, and say so).
"""

from __future__ import annotations

import importlib
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from noc_agents.adapters.email_smtp import EmailResult
from noc_agents.api import auth
from noc_agents.db.models import AuditRow, OutboxRow, get_session, utcnow
from noc_agents.orchestrator import outbox
from noc_agents.orchestrator.outbox import drain_once, enqueue
from noc_agents.realtime.hub import hub
from noc_agents.services import notify

SECRET = "outbox-admin-secret"
LIST = "/api/v1/outbox"
RETRY = "/api/v1/outbox/{id}/retry"

#: A recipient refusal the SMTP adapter quotes verbatim: permanent (5xx), and it names a mailbox.
REFUSED = "SMTP send failed: SMTPRecipientsRefused: {'wanjiku.kamau@example.com': (550, b'5.1.1 no such user')}"

SUMMARY_KEYS = {
    "id", "kind", "status", "attempts", "max_attempts", "incident_id", "incident_number", "audience",
    "run_id", "alert_id", "hitl_task_id", "requires_hitl", "approved_at", "provider", "last_error",
    "created_at", "updated_at", "next_attempt_at", "retryable",
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A live API on its own SQLite file (the reload pattern the other route tests use)."""
    db = tmp_path / "outbox_admin.db"
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


@pytest.fixture()
def enforced(client, monkeypatch):
    """The same app with ``AUTH_DISABLED=false`` for the length of one test."""
    monkeypatch.setenv("AUTH_DISABLED", "false")
    monkeypatch.setenv("NOC_SESSION_SECRET", SECRET)
    client.cookies.clear()
    yield client
    client.cookies.clear()


def _as(client: TestClient, role: str, name: str | None = None) -> None:
    client.cookies.clear()
    claims = {"sub": f"u-{role}", "role": role, "name": name or role}
    client.cookies.set(auth.SESSION_COOKIE, auth.sign_session(claims, SECRET))


# ----------------------------------------------------------------------------- seeding


def _email_payload(n: str = "INC000001", operator_id: str = "safaricom") -> dict:
    return {
        "operator_id": operator_id,
        "incident_number": n,
        "audience": "RNIO",
        "subject": f"[P2] {n} | Embakasi East Aggregation HUB down",
        "body": "FE James Mwangi (0712 345 678, james.mwangi@example.com) is on site with the genset crew.",
        "recipients_ref": "DEMO_EMAIL_TO",
        "broadcast_ids": [],
    }


def _enqueue(key: str, *, kind: str = "EMAIL", payload: dict | None = None, operator_id: str = "safaricom", **kw) -> str:
    session = get_session()
    try:
        row = enqueue(
            session,
            kind=kind,
            idempotency_key=key,
            payload=payload if payload is not None else _email_payload(operator_id=operator_id),
            operator_id=operator_id,
            **kw,
        )
        session.commit()
        return row.id
    finally:
        session.close()


def _drain(**kw):
    session = get_session()
    try:
        return drain_once(session, **kw)
    finally:
        session.close()


def _row(row_id: str) -> OutboxRow:
    session = get_session()
    try:
        row = session.get(OutboxRow, row_id)
        session.expunge(row)
        return row
    finally:
        session.close()


def _force(row_id: str, **values) -> None:
    """Set columns directly: only for states the drain cannot reach on its own in a test."""
    session = get_session()
    try:
        session.execute(update(OutboxRow).where(OutboxRow.id == row_id).values(**values))
        session.commit()
    finally:
        session.close()


def _outbox_count() -> int:
    session = get_session()
    try:
        return session.scalar(select(func.count()).select_from(OutboxRow))
    finally:
        session.close()


def _audits(row_id: str) -> list[AuditRow]:
    session = get_session()
    try:
        rows = list(session.scalars(select(AuditRow).where(AuditRow.entity_id == row_id)))
        for row in rows:
            session.expunge(row)
        return rows
    finally:
        session.close()


def _refuse_every_email(monkeypatch) -> list[dict]:
    """``transmit_email`` answers a permanent 550: the drain marks the row DEAD at once."""
    calls: list[dict] = []

    def refused(payload):
        calls.append(payload)
        return EmailResult(ok=False, mode="error", detail=REFUSED, to=["wanjiku.kamau@example.com"])

    monkeypatch.setattr(notify, "transmit_email", refused)
    return calls


def _spy_sends(monkeypatch, real) -> list[dict]:
    """Count the dispatcher's SMTP-adapter calls; ``real`` is the mock-mode adapter (EMAIL_ENABLED=false)."""
    calls: list[dict] = []

    def spy(payload):
        calls.append(payload)
        return real(payload)

    monkeypatch.setattr(notify, "transmit_email", spy)
    return calls


def _dead_email(monkeypatch, key: str, **kw) -> str:
    """An EMAIL row the real drain has marked DEAD (SMTP 550), and the adapter put back."""
    real = notify.transmit_email
    row_id = _enqueue(key, **kw)
    _refuse_every_email(monkeypatch)
    report = _drain()
    monkeypatch.setattr(notify, "transmit_email", real)
    assert report.dead == 1, str(report)
    return row_id


def _failed_ledger_row(monkeypatch, key: str) -> str:
    """An EXCEL_ROW the real drain has retried to exhaustion: FAILED with attempts == max_attempts."""

    def locked(*_args, **_kwargs):
        raise PermissionError("[Errno 13] Permission denied: 'ledger_2026-09-16_DAY.xlsx'")

    monkeypatch.setattr(outbox, "append_excel_row", locked)
    row_id = _enqueue(
        key,
        kind="EXCEL_ROW",
        payload={"operator_id": "safaricom", "incident_number": "INC000009", "file": "ledger_2026-09-16_DAY.xlsx", "cells": []},
    )
    t0 = utcnow()
    for step in range(3):
        _drain(now=t0 + timedelta(seconds=100 * step))
    row = _row(row_id)
    assert (row.status, row.attempts, row.max_attempts) == ("FAILED", 3, 3)
    return row_id


# --------------------------------------------------------------------------------------
# The view
# --------------------------------------------------------------------------------------


def test_the_view_lists_failed_and_dead_rows_only_newest_first(client):
    ids = {status: _enqueue(f"EMAIL:view:{status}") for status in (
        "PENDING", "HELD", "CLAIMED", "SENT", "DELIVERED", "SUPPRESSED", "REJECTED_UNAPPROVED", "FAILED", "DEAD",
    )}
    now = utcnow()
    for offset, (status, row_id) in enumerate(ids.items()):
        _force(row_id, status=status, updated_at=now + timedelta(seconds=offset))  # DEAD is the newest

    listed = client.get(LIST)
    assert listed.status_code == 200, listed.text
    assert [r["id"] for r in listed.json()] == [ids["DEAD"], ids["FAILED"]]
    assert {r["status"] for r in listed.json()} == {"FAILED", "DEAD"}

    assert [r["id"] for r in client.get(LIST, params={"status": "FAILED"}).json()] == [ids["FAILED"]]
    assert [r["id"] for r in client.get(LIST, params={"status": "dead"}).json()] == [ids["DEAD"]]
    for both in ("FAILED|DEAD", "FAILED,DEAD", " DEAD , FAILED "):
        assert {r["id"] for r in client.get(LIST, params={"status": both}).json()} == {ids["FAILED"], ids["DEAD"]}, both
    assert len(client.get(LIST, params={"limit": 1}).json()) == 1


@pytest.mark.parametrize("query", [{"status": "SENT"}, {"status": "PENDING,FAILED"}, {"status": "|"}, {"limit": 0}, {"limit": 501}])
def test_the_view_refuses_what_is_not_a_dead_letter_query(client, query):
    assert client.get(LIST, params=query).status_code == 422


def test_the_view_is_a_summary_and_never_the_payload(client, monkeypatch):
    """The row carries a person's name, an MSISDN and two mailboxes (payload and last_error),
    an envelope and an approver. None of that may reach the platform readers' screen."""
    envelope = {"operator_id": "safaricom", "note": "envelope text Grace Wanjiru"}
    row_id = _dead_email(
        monkeypatch,
        "EMAIL:summary",
        envelope=envelope,
        approved_by="Grace Wanjiru",
        approved_at=utcnow(),
        requires_hitl=True,
    )

    r = client.get(LIST)
    assert r.status_code == 200, r.text
    (item,) = r.json()
    assert set(item) == SUMMARY_KEYS
    assert (item["id"], item["kind"], item["status"], item["attempts"]) == (row_id, "EMAIL", "DEAD", 1)
    assert (item["incident_number"], item["audience"]) == ("INC000001", "RNIO")
    assert item["requires_hitl"] is True and item["approved_at"] is not None and item["retryable"] is True
    # The refused mailbox is scrubbed from the error; the SMTP reply that explains it is kept.
    assert "wanjiku.kamau@example.com" not in item["last_error"]
    assert "<EMAIL>" in item["last_error"] and "550" in item["last_error"]

    text = r.text
    for secret in ("James Mwangi", "0712 345 678", "james.mwangi@example.com", "wanjiku.kamau@example.com",
                   "Grace Wanjiru", "genset crew", "EMAIL:summary", "DEMO_EMAIL_TO"):
        assert secret not in text, secret


def test_a_malformed_payload_does_not_break_the_view(client):
    row_id = _enqueue("EMAIL:malformed")
    _force(row_id, status="DEAD", payload_json="not json", last_error="TypeError: bug")
    (item,) = client.get(LIST).json()
    assert (item["id"], item["incident_number"], item["audience"]) == (row_id, None, None)


# --------------------------------------------------------------------------------------
# The retry: same row, nothing sent by the route, sent once by the next drain
# --------------------------------------------------------------------------------------


def test_a_retry_requeues_the_same_row_and_the_next_drain_sends_it_exactly_once(client, monkeypatch):
    row_id = _dead_email(monkeypatch, "EMAIL:retry-once")
    dead = _row(row_id)
    assert dead.claimed_at is not None and dead.claimed_by  # a terminal row keeps its last claim stamp
    rows_before = _outbox_count()
    sends = _spy_sends(monkeypatch, notify.transmit_email)

    r = client.post(RETRY.format(id=row_id), json={"reason": "relay credentials rotated"})

    assert r.status_code == 200, r.text
    assert (r.json()["ok"], r.json()["previous_status"]) == (True, "DEAD")
    assert (r.json()["outbox"]["id"], r.json()["outbox"]["status"]) == (row_id, "PENDING")
    queued = _row(row_id)
    assert (queued.status, queued.idempotency_key, queued.attempts) == ("PENDING", "EMAIL:retry-once", 1)
    assert (queued.claimed_at, queued.claimed_by, queued.next_attempt_at) == (None, None, None)
    assert queued.payload_json == dead.payload_json and queued.approved_at == dead.approved_at
    assert _outbox_count() == rows_before  # no second row, so the unique key has one holder
    assert sends == []  # the route transmits nothing; the drain does

    # A second click before the drain is refused, not queued twice.
    again = client.post(RETRY.format(id=row_id))
    assert again.status_code == 409 and "PENDING" in again.json()["detail"]

    first = _drain()
    assert (first.claimed, first.sent) == (1, 1)
    assert len(sends) == 1 and sends[0]["incident_number"] == "INC000001"
    sent = _row(row_id)
    assert (sent.status, sent.attempts, sent.idempotency_key) == ("SENT", 2, "EMAIL:retry-once")
    assert _drain().claimed == 0 and len(sends) == 1

    # A producer re-running with the same key still finds the one row (§6.6).
    assert _enqueue("EMAIL:retry-once") == row_id and _outbox_count() == rows_before
    after_sent = client.post(RETRY.format(id=row_id))
    assert after_sent.status_code == 409 and "second time" in after_sent.json()["detail"]
    assert _drain().claimed == 0 and len(sends) == 1


def test_attempts_are_never_reset_so_a_spent_budget_buys_one_attempt_per_retry(client, monkeypatch):
    row_id = _failed_ledger_row(monkeypatch, "EXCEL_ROW:spent")

    assert client.post(RETRY.format(id=row_id)).status_code == 200
    assert (_row(row_id).status, _row(row_id).attempts) == ("PENDING", 3)

    # Still locked: ONE attempt, straight back to FAILED -- no backoff loop of automatic resends.
    report = _drain(now=utcnow() + timedelta(hours=1))
    assert (report.claimed, report.failed, report.retried) == (1, 1, 0)
    assert (_row(row_id).status, _row(row_id).attempts) == ("FAILED", 4)

    monkeypatch.setattr(outbox, "append_excel_row", lambda *_a, **_k: "ledger_2026-09-16_DAY.xlsx")
    assert client.post(RETRY.format(id=row_id)).status_code == 200
    assert _drain(now=utcnow() + timedelta(hours=2)).sent == 1
    assert (_row(row_id).status, _row(row_id).attempts) == ("SENT", 5)


def test_the_retry_is_audited_against_the_principal(enforced, monkeypatch):
    row_id = _dead_email(monkeypatch, "EMAIL:audited")
    other = _dead_email(monkeypatch, "EMAIL:audited-2")  # before any retry re-queues the first
    _as(enforced, "admin", "Otieno Admin")

    r = enforced.post(RETRY.format(id=row_id), json={"reason": "relay back up"})

    assert r.status_code == 200, r.text
    (audit,) = _audits(row_id)
    assert (audit.action, audit.actor, audit.entity_type, audit.operator_id) == (
        "outbox.retried", "Otieno Admin", "outbox", "safaricom",
    )
    assert audit.rationale == "relay back up"
    payload = json.loads(audit.payload_json)
    assert (payload["from"], payload["to"], payload["kind"], payload["attempts"], payload["role"]) == (
        "DEAD", "PENDING", "EMAIL", 1, "admin",
    )
    assert "wanjiku.kamau@example.com" not in audit.payload_json  # the audit copy is scrubbed too

    # The body cannot name who retried: an unknown field is refused, not silently dropped.
    assert enforced.post(RETRY.format(id=other), json={"retried_by": "Mallory"}).status_code == 422
    assert _row(other).status == "DEAD" and _audits(other) == []


# --------------------------------------------------------------------------------------
# What is refused, and that refusing changes nothing
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status", ["SENT", "DELIVERED", "SUPPRESSED", "REJECTED_UNAPPROVED", "PENDING", "HELD", "CLAIMED"]
)
def test_a_row_that_is_not_a_dead_letter_is_refused_and_left_alone(client, status):
    row_id = _enqueue(f"EMAIL:refuse:{status}")
    _force(row_id, status=status, attempts=1)  # set directly: a test cannot wait for each of these
    before = _row(row_id)

    r = client.post(RETRY.format(id=row_id))

    assert r.status_code == 409, r.text
    assert status in r.json()["detail"]
    after = _row(row_id)
    assert (after.status, after.attempts, after.updated_at) == (status, 1, before.updated_at)
    assert _audits(row_id) == []


def test_a_retry_is_not_an_approval(client):
    """A channel row that needs a HITL approval it never got. The dispatcher would refuse it
    again; the route refuses it first and says why. Set directly: the machine files this case
    as REJECTED_UNAPPROVED, so a FAILED one only arises from a crash inside the lease."""
    row_id = _enqueue("EMAIL:unapproved", requires_hitl=True)
    _force(row_id, status="FAILED", attempts=3, last_error="lease expired with no attempts left")

    (item,) = client.get(LIST).json()
    assert item["retryable"] is False
    r = client.post(RETRY.format(id=row_id))
    assert r.status_code == 409 and "not an approval" in r.json()["detail"]
    assert _row(row_id).status == "FAILED"


def test_an_archived_payload_is_not_retried(client):
    """Housekeeping (§9.4) replaces a DEAD row's payload with a summary after 90 days."""
    row_id = _enqueue("EMAIL:archived")
    _force(row_id, status="DEAD", payload_json=json.dumps({"_archived": True, "kind": "EMAIL", "incident_number": "INC000001"}))

    r = client.post(RETRY.format(id=row_id))

    assert r.status_code == 409 and "archived" in r.json()["detail"]
    assert _row(row_id).status == "DEAD"


def test_the_compare_and_set_refuses_even_when_the_precheck_is_bypassed(client, monkeypatch):
    """The write is guarded by its own WHERE clause, not only by the read before it: with the
    pre-check forced open, a SENT row is still not flipped back to PENDING."""
    from noc_agents.api.routers import outbox_admin

    row_id = _enqueue("EMAIL:cas")
    _force(row_id, status="SENT")
    monkeypatch.setattr(outbox_admin, "_retry_refusal", lambda _row, _payload: None)

    r = client.post(RETRY.format(id=row_id))

    assert r.status_code == 409 and "changed while" in r.json()["detail"]
    assert _row(row_id).status == "SENT"
    assert _audits(row_id) == []


# --------------------------------------------------------------------------------------
# Operator scoping
# --------------------------------------------------------------------------------------


def test_another_operators_dead_letters_are_invisible_and_a_404(client, monkeypatch):
    mine = _dead_email(monkeypatch, "EMAIL:mine")
    theirs = _dead_email(monkeypatch, "EMAIL:theirs", operator_id="airtel")

    assert [r["id"] for r in client.get(LIST).json()] == [mine]
    for target in (theirs, "no-such-row"):
        r = client.post(RETRY.format(id=target))
        assert r.status_code == 404, r.text
        assert r.json()["detail"] == "outbox row not found"
    assert _row(theirs).status == "DEAD"
    assert _audits(theirs) == []


# --------------------------------------------------------------------------------------
# §9.3, with auth enforced
# --------------------------------------------------------------------------------------


def test_the_view_is_for_the_platform_readers(enforced, monkeypatch):
    _dead_email(monkeypatch, "EMAIL:rbac-view")
    readers = {"noc_analyst", "shift_supervisor", "duty_manager", "management", "admin"}

    enforced.cookies.clear()
    assert enforced.get(LIST).status_code == 401
    got = {}
    for role in sorted(auth.ROLES):
        _as(enforced, role)
        got[role] = enforced.get(LIST).status_code
    assert got == {role: (200 if role in readers else 403) for role in auth.ROLES}


def test_only_admin_may_retry(enforced, monkeypatch):
    row_id = _dead_email(monkeypatch, "EMAIL:rbac-retry")

    enforced.cookies.clear()
    assert enforced.post(RETRY.format(id=row_id)).status_code == 401
    refused = {}
    for role in sorted(set(auth.ROLES) - {"admin"}):
        _as(enforced, role)
        refused[role] = enforced.post(RETRY.format(id=row_id)).status_code
    assert refused == {role: 403 for role in refused}
    assert _row(row_id).status == "DEAD"

    _as(enforced, "admin")
    assert enforced.post(RETRY.format(id=row_id)).status_code == 200


def test_the_readers_tuple_matches_mains_platform_readers(client):
    """Spelled out in the router because a router may not import ``main``; pinned here instead."""
    import noc_agents.main as main
    from noc_agents.api.routers import outbox_admin

    assert outbox_admin.PLATFORM_READERS == main.PLATFORM_READERS
