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
  audit row names the principal;
* the producer decides as well as the status (review routes-correctness#1, #3): a regulatory
  notice is never retried here, because its lane re-releases it as a new row and a retry sent
  the Communications Authority a second notice; a handover is re-run, not retried; a
  maintenance invite only while it is the newest for its uid; a complaint reminder only on its
  own day; an unclassified producer not at all. Each rule is driven through the REAL producer,
  so the key format the classifier reads is the one the producer actually writes;
* a housekeeping sweep that archives the payload between the check and the write turns the
  retry into a 409 (review routes-correctness#6).

Rows are driven to FAILED/DEAD by the real ``drain_once`` with the transmitters monkeypatched,
the same way ``test_outbox.py`` does it, except where a status the machine cannot reach on its
own is needed (those are set directly, and say so).
"""

from __future__ import annotations

import importlib
import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from noc_agents.adapters.email_smtp import EmailResult
from noc_agents.api import auth
from noc_agents.config import get_settings
from noc_agents.db.models import AuditRow, IncidentRow, OutboxRow, get_session, utcnow
from noc_agents.db.models_complaints import RelationshipComplaintRow
from noc_agents.db.models_regulatory import RegulatoryNotificationRow
from noc_agents.orchestrator import outbox
from noc_agents.orchestrator.outbox import drain_once, enqueue
from noc_agents.realtime.hub import hub
from noc_agents.services import complaints, housekeeping, ics, notify
from noc_agents.services import pir as pir_service
from noc_agents.services.regulatory import open_notification, release_notice, request_approval

SECRET = "outbox-admin-secret"
LIST = "/api/v1/outbox"
RETRY = "/api/v1/outbox/{id}/retry"

#: A recipient refusal the SMTP adapter quotes verbatim: permanent (5xx), and it names a mailbox.
REFUSED = "SMTP send failed: SMTPRecipientsRefused: {'wanjiku.kamau@example.com': (550, b'5.1.1 no such user')}"

SUMMARY_KEYS = {
    "id", "kind", "producer", "status", "attempts", "max_attempts", "incident_id", "incident_number",
    "audience", "run_id", "alert_id", "hitl_task_id", "requires_hitl", "approved_at", "provider",
    "last_error", "created_at", "updated_at", "next_attempt_at", "retryable", "retry_refusal",
}

#: The regulatory lane's own reference instants (tests/unit/test_regulatory_dispatch_outcome.py).
FAILURE = datetime(2026, 9, 16, 9, 0, 0)
ON_TIME = FAILURE + timedelta(hours=23)  # inside the 24-hour CA window

BTS_EVENT = {  # P4 at L2_GUARDED: auto-broadcast, so the run queues SMS, EMAIL and ledger rows
    "site_id": "SFC-MTK-BTS-MCH04",
    "site_name": "Machakos Town BTS",
    "site_type": "BTS",
    "region_code": "MTK",
    "alarm_code": "SITE_DOWN",
    "failure_domain": "POWER",
    "users_affected": 3200,
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
    monkeypatch.setattr(outbox_admin, "_retry_refusal", lambda *_args: None)

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



# --------------------------------------------------------------------------------------
# The producer decides too (review routes-correctness#1, #3)
# --------------------------------------------------------------------------------------


def _released_notice() -> tuple[str, str, str]:
    """An approved CA notice released to the outbox by the REAL ``release_notice``, committed.

    Mirrors ``tests/unit/test_regulatory_dispatch_outcome.py::_released``. The shipped profile
    declares no CA address, so the first drain kills row #1 -- the default path, not a contrivance.
    """
    session = get_session()
    try:
        cfg = get_settings().operator
        inc = IncidentRow(
            operator_id="safaricom",
            incident_number="INC000901",
            status="IN_PROGRESS",
            priority="P1",
            users_affected=450000,
            site_id="SFC-MTK-HUB-THK",
            site_name="Thika Hub",
            site_type="HUB",
            region_code="MTK",
            county="Kiambu",
            correlation_fingerprint="fp-outbox-admin",
            failure_time=FAILURE,
        )
        session.add(inc)
        session.flush()
        notice = open_notification(session, inc, cfg)
        task = request_approval(session, notice, inc, cfg)
        task.status = "APPROVED"
        task.resolved_by = "Grace Wanjiru"
        task.resolved_at = utcnow()
        session.flush()
        row = release_notice(session, notice, inc, actor="Duty Manager", now=ON_TIME)
        session.commit()
        return inc.id, notice.id, row.id
    finally:
        session.close()


def _notice(notice_id: str) -> tuple[str, dict]:
    session = get_session()
    try:
        notice = session.get(RegulatoryNotificationRow, notice_id)
        return notice.status, dict(notice.significance)
    finally:
        session.close()


def _ca_sends(monkeypatch) -> list[str]:
    """The CA address is now configured: every transmission succeeds and is counted, no socket."""
    sends: list[str] = []

    def delivered(payload):
        sends.append(payload.get("regulatory_notification_id"))
        return EmailResult(ok=True, mode="smtp", detail="250 accepted", to=["ca@example.ke"])

    monkeypatch.setattr(notify, "transmit_email", delivered)
    return sends


def _view(row_id: str, client: TestClient) -> dict:
    (item,) = [r for r in client.get(LIST).json() if r["id"] == row_id]
    return item


def test_a_failed_regulatory_notice_is_recovered_by_its_lane_and_never_by_a_retry(client, monkeypatch):
    """routes-correctness#1. Row #1 dies for want of a CA address; the lane re-releases the
    notice as row #2 once the address exists. Row #1 is still in the dead-letter view, and a
    retry of it -- before the re-release or hours after -- must never reach the Authority."""
    monkeypatch.setenv("REGULATORY_ENABLED", "true")
    _inc_id, notice_id, row1 = _released_notice()
    assert _drain(now=ON_TIME).dead == 1
    assert _notice(notice_id)[0] == "SEND_FAILED"
    sends = _ca_sends(monkeypatch)
    lane_path = f"POST /api/v1/regulatory/{notice_id}/send"

    item = _view(row1, client)
    assert (item["producer"], item["status"], item["retryable"]) == ("regulatory_notice", "DEAD", False)
    assert lane_path in item["retry_refusal"]
    refused = client.post(RETRY.format(id=row1), json={"reason": "CA address now configured"})
    assert refused.status_code == 409 and lane_path in refused.json()["detail"]
    assert _row(row1).status == "DEAD" and _audits(row1) == []

    # The recovery the lane owns: a new, attempt-numbered row.
    released = client.post(
        f"/api/v1/regulatory/{notice_id}/send", json={"reason_for_delay": "CA address was missing from the profile"}
    )
    assert released.status_code == 200, released.text
    row2 = released.json()["outbox_id"]
    assert row2 != row1 and _row(row2).idempotency_key == f"EMAIL:regulatory:{notice_id}:2"
    assert _drain(now=ON_TIME + timedelta(minutes=1)).sent == 1
    assert _notice(notice_id)[0] == "SENT" and sends == [notice_id]

    # Hours later row #1 is still listed, still refused, and nothing more leaves.
    assert _view(row1, client)["retryable"] is False
    assert client.post(RETRY.format(id=row1)).status_code == 409
    assert _drain(now=ON_TIME + timedelta(hours=3)).claimed == 0
    assert sends == [notice_id]  # ONE notice at the Authority
    assert _row(row1).status == "DEAD"


def test_refusing_the_regulatory_retry_keeps_the_failed_attempt_in_the_evidence(client, monkeypatch):
    """routes-correctness#3. The retry used to overwrite ``significance.dispatch`` and number the
    resend attempt 1, erasing "we tried and it bounced" from the M10 evidence. Refused, the only
    way on is the lane's re-release, which moves the failure into ``dispatch_history``."""
    monkeypatch.setenv("REGULATORY_ENABLED", "true")
    _inc_id, notice_id, row1 = _released_notice()
    _drain(now=ON_TIME)
    failed = _notice(notice_id)[1]["dispatch"]
    assert (failed["outbox_id"], failed["outbox_status"], failed["attempt"]) == (row1, "DEAD", 1)
    sends = _ca_sends(monkeypatch)

    assert client.post(RETRY.format(id=row1)).status_code == 409  # the overlapping order: retry first
    released = client.post(f"/api/v1/regulatory/{notice_id}/send", json={"reason_for_delay": "CA address added"})
    assert released.status_code == 200, released.text
    row2 = released.json()["outbox_id"]
    report = _drain(now=ON_TIME + timedelta(minutes=1))

    assert (report.claimed, report.sent) == (1, 1) and len(sends) == 1
    status, significance = _notice(notice_id)
    assert status == "SENT"
    assert (significance["dispatch"]["outbox_id"], significance["dispatch"]["attempt"]) == (row2, 2)
    assert [(h["outbox_id"], h["outbox_status"], h["attempt"]) for h in significance["dispatch_history"]] == [
        (row1, "DEAD", 1)
    ]


def test_every_producer_that_feeds_its_outcome_back_is_one_the_retry_refuses():
    """``orchestrator/outbox._PRODUCER_OUTCOMES`` lists the producers told what became of their
    message, which is what lets them own a recovery path. A new one must be classified in
    ``outbox_admin`` before its rows are silently retryable; this is where that gets asked."""
    from noc_agents.api.routers import outbox_admin

    assert set(outbox._PRODUCER_OUTCOMES) == {outbox_admin.REGULATORY_PAYLOAD_KEY}


def test_a_shift_handover_is_re_run_not_retried(client):
    """Each POST /shifts/handover composes the handover afresh under a new key. Retrying an old
    row re-sends an old snapshot, and a second handover if it was already re-run. (With
    HANDOVER_REQUIRES_HITL on, the default, the mail is queued HELD behind an approval card
    anchored on an open incident, hence the ingest first.)"""
    assert client.post("/api/v1/events", json=BTS_EVENT).status_code == 200
    assert client.post("/api/v1/shifts/handover").status_code == 200
    session = get_session()
    try:
        row_id = session.scalar(select(OutboxRow.id).where(OutboxRow.idempotency_key.like("EMAIL:handover:%")))
    finally:
        session.close()
    assert row_id is not None
    _force(row_id, status="DEAD", last_error="SMTP send failed: (535, b'bad credentials')")

    item = _view(row_id, client)
    assert (item["producer"], item["retryable"]) == ("shift_handover", False)
    r = client.post(RETRY.format(id=row_id))
    assert r.status_code == 409 and "POST /api/v1/shifts/handover" in r.json()["detail"]
    assert _row(row_id).status == "DEAD"


def test_broadcast_and_ledger_rows_from_the_real_lifecycle_stay_retryable(client):
    """Keyed once per incident and audience (or shift): queueing again returns the same row, so
    the retry is the only recovery these rows have."""
    assert client.post("/api/v1/events", json=BTS_EVENT).status_code == 200
    session = get_session()
    try:
        rows = {r.kind: r.id for r in session.scalars(select(OutboxRow))}
    finally:
        session.close()
    assert {"EMAIL", "SMS", "EXCEL_ROW"} <= set(rows)
    for row_id in rows.values():
        _force(row_id, status="DEAD", last_error="relay down")

    by_kind = {item["kind"]: item for item in client.get(LIST).json()}
    assert {kind: (item["producer"], item["retryable"]) for kind, item in by_kind.items()} == {
        "EMAIL": ("incident_broadcast", True),
        "SMS": ("incident_broadcast", True),
        "EXCEL_ROW": ("shift_ledger", True),
    }
    assert all(item["retry_refusal"] is None for item in by_kind.values())
    assert client.post(RETRY.format(id=rows["EMAIL"])).status_code == 200


def test_a_pir_model_draft_stays_retryable(client, monkeypatch):
    """One row per review (a second queue returns the same row), and the transmitter writes no
    text anywhere, so a retry cannot produce a second draft."""
    monkeypatch.setenv("PIR_ENABLED", "true")
    incident_id = client.post("/api/v1/events", json=BTS_EVENT).json()["incident"]["id"]
    session = get_session()
    try:
        inc = session.get(IncidentRow, incident_id)
        review, _created = pir_service.open_pir(session, inc, reason=pir_service.REASON_MANUAL)
        row, _queued = pir_service.queue_llm_draft(session, review, inc)
        session.commit()
        row_id = row.id
    finally:
        session.close()
    _force(row_id, status="DEAD", last_error="model refused: policy")

    item = _view(row_id, client)
    assert (item["kind"], item["producer"], item["retryable"]) == ("LLM_CALL", "pir_llm_draft", True)
    assert client.post(RETRY.format(id=row_id)).status_code == 200


def _invite(**over) -> ics.WindowInvite:
    fields = dict(
        window_id="mw-0001",
        uid=ics.stable_uid("mw-0001", domain="noc.example.com"),
        starts_at=datetime(2026, 9, 21, 22, 0),
        ends_at=datetime(2026, 9, 22, 2, 0),
        summary="Planned fibre splice - Embakasi ring",
        organizer="noc.maintenance@example.com",
        attendees=("fe.embakasi@example.com",),
        sequence=0,
        operator_id="safaricom",
        location="SFC-NBIE-HUB-EMB",
    )
    fields.update(over)
    return ics.WindowInvite(**fields)


def _queue_invite(invite: ics.WindowInvite, method: str = "REQUEST", **payload_over) -> str:
    """An invite row under the key the REAL ``ics.invite_idempotency_key`` writes."""
    payload = {"operator_id": "safaricom", **payload_over}
    return _enqueue(ics.invite_idempotency_key(invite, method), kind="ICS_INVITE", payload=payload)


def test_a_maintenance_invite_is_retried_only_while_it_is_the_newest_for_its_uid(client):
    first = _enqueue(
        ics.invite_idempotency_key(_invite()), kind="ICS_INVITE", payload=ics.invite_outbox_payload(_invite())
    )
    _force(first, status="DEAD", last_error="SMTP send failed: (535, b'bad credentials')")
    # A uid that merely starts with this one, with a higher sequence, supersedes nothing here.
    _queue_invite(_invite(window_id="mw-0001x", uid=_invite().uid + ":x", sequence=5))
    item = _view(first, client)
    assert (item["producer"], item["retryable"]) == ("maintenance_invite", True)

    # The window is rescheduled: sequence 1 is the invite the attendees must get.
    second = _queue_invite(_invite(sequence=1))
    _force(second, status="DEAD", last_error="SMTP send failed: (535, b'bad credentials')")
    item = _view(first, client)
    assert item["retryable"] is False and "REQUEST sequence 1" in item["retry_refusal"]
    r = client.post(RETRY.format(id=first))
    assert r.status_code == 409 and "superseded" in r.json()["detail"]
    assert _view(second, client)["retryable"] is True

    # Then cancelled: the old REQUEST must never follow the CANCEL into a calendar.
    _queue_invite(_invite(sequence=2), method="CANCEL")
    r = client.post(RETRY.format(id=second))
    assert r.status_code == 409 and "CANCEL sequence 2" in r.json()["detail"]
    assert (_row(first).status, _row(second).status) == ("DEAD", "DEAD")


def test_a_complaint_reminder_is_retried_only_on_its_own_day(client, monkeypatch):
    """The follow-up job re-issues a reminder each day with what is still overdue; yesterday's
    may list complaints since closed. Today's has no other way out today."""
    monkeypatch.setenv("COMPLAINTS_ENABLED", "true")
    now = utcnow()
    session = get_session()
    try:
        session.add(
            RelationshipComplaintRow(
                operator_id="safaricom",
                filed_by="Grace Wanjiru",
                filed_at=now - timedelta(days=10),
                subject_type="VENDOR",
                vendor_id="vendor-egypro",
                category="NO_SHOW",
                severity="MEDIUM",
                description="Crew did not attend the 09:00 SLA visit.",
                status=complaints.OPEN,
                assigned_manager="Duty Manager East",
                follow_up_due_at=now - timedelta(days=3),
                retention_until=now + timedelta(days=complaints.RETENTION_DAYS),
                updated_at=now,
            )
        )
        session.commit()
        settings = get_settings()
        complaints.send_due_reminders(session, settings, now=now - timedelta(days=1))
        complaints.send_due_reminders(session, settings, now=now)
        session.commit()
        keys = {r.id: r.idempotency_key for r in session.scalars(select(OutboxRow))}
    finally:
        session.close()
    yesterday = next(i for i, k in keys.items() if k.endswith((now - timedelta(days=1)).date().isoformat()))
    today = next(i for i, k in keys.items() if k.endswith(now.date().isoformat()))
    for row_id in (yesterday, today):
        _force(row_id, status="DEAD", last_error="UnresolvedRecipients: complaints.recipients.MANAGEMENT")

    assert (_view(today, client)["producer"], _view(today, client)["retryable"]) == ("complaint_reminder", True)
    old = _view(yesterday, client)
    assert old["retryable"] is False and "fresh one each day" in old["retry_refusal"]
    assert "Duty Manager East" not in json.dumps(old)  # the key names the manager; the view never does
    assert client.post(RETRY.format(id=yesterday)).status_code == 409
    assert client.post(RETRY.format(id=today)).status_code == 200


def test_a_row_from_an_unclassified_producer_is_refused(client):
    """Fail closed: a producer nobody has classified may own a recovery path of its own."""
    row_id = _enqueue("HITL_NUDGE:task-1:T+5", kind="HITL_NUDGE", payload={"operator_id": "safaricom"})
    _force(row_id, status="DEAD", last_error="no transmitter for outbox kind 'HITL_NUDGE'")

    item = _view(row_id, client)
    assert (item["producer"], item["retryable"]) == ("unknown", False)
    r = client.post(RETRY.format(id=row_id))
    assert r.status_code == 409 and "no retry rule" in r.json()["detail"]


# --------------------------------------------------------------------------------------
# The archive race (review routes-correctness#6)
# --------------------------------------------------------------------------------------


def _sweep() -> int:
    """Housekeeping's §9.4 outbox sweep, applied, in its own session: what the daily job does."""
    session = get_session()
    try:
        report = housekeeping.sweep_outbox(session, get_settings(), now=utcnow(), apply=True)
        session.commit()
        return report.archived
    finally:
        session.close()


def test_a_sweep_that_archives_the_payload_after_the_check_turns_the_retry_into_a_409(client, monkeypatch):
    """The pre-check reads a live payload; housekeeping archives it and commits; the write must
    still refuse, or the row is queued with nothing left to send."""
    from noc_agents.api.routers import outbox_admin

    row_id = _dead_email(monkeypatch, "EMAIL:archive-race")
    _force(row_id, updated_at=utcnow() - timedelta(days=100))  # past the 90-day retention
    real = outbox_admin._retry_refusal
    archived: list[int] = []

    def check_then_sweep(*args):
        verdict = real(*args)
        if not archived:  # only between the route's own check and its compare-and-set
            archived.append(_sweep())
        return verdict

    monkeypatch.setattr(outbox_admin, "_retry_refusal", check_then_sweep)
    r = client.post(RETRY.format(id=row_id))

    assert archived == [1]  # housekeeping really did archive it in between
    assert r.status_code == 409 and "changed while" in r.json()["detail"]
    after = _row(row_id)
    assert after.status == "DEAD" and json.loads(after.payload_json)[housekeeping.ARCHIVED_KEY] is True
    assert _audits(row_id) == []
