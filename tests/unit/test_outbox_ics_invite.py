"""The ``ICS_INVITE`` transmitter (§7.5.6): a maintenance-window invite that a client accepts.

``services/ics`` builds the RFC 5545 calendar object and the RFC 6047 iMIP message; this
file is about the half that gets it onto the wire — the outbox transmitter and the SMTP
adapter's ``send_message`` entry point. What is proved:

* the invite goes to the ATTENDEES in the calendar object, never to ``DEMO_EMAIL_TO``, even
  when a demo mailbox is configured — a rerouted invite is an engineer who never learns
  about the window, and an outsider holding an event for a site they have nothing to do with;
* ``From:`` is the ORGANIZER and the adapter leaves it alone (RFC 6047 §3: clients drop an
  invite whose From and ORGANIZER disagree), while the ``text/calendar; method=REQUEST``
  part survives the trip through the adapter;
* a payload version this dispatcher does not understand is refused, not guessed at;
* the approval gate applies (``ICS_INVITE`` is a channel kind), and attendee addresses get
  the same reg 41(2) transfer record as an outage e-mail, written BEFORE the send;
* SMTP failures are classified the way the EMAIL path classifies them.

No socket is opened: ``smtplib.SMTP`` is replaced, as in ``tests/unit/test_outbox.py``.
"""

from __future__ import annotations

import json
import smtplib
from datetime import datetime

import pytest
from sqlalchemy import select

from noc_agents.adapters import email_smtp
from noc_agents.adapters.email_smtp import EmailResult
from noc_agents.db.models import AuditRow, OutboxRow, get_session
from noc_agents.orchestrator import outbox
from noc_agents.orchestrator.outbox import DEAD, FAILED, ICS_INVITE, REJECTED_UNAPPROVED, SENT, drain_once, enqueue
from noc_agents.realtime.hub import hub
from noc_agents.services import ics

SMTP_ENV = {
    "EMAIL_ENABLED": "true",
    "GMAIL_ADDRESS": "noc@example.com",
    "GMAIL_APP_PASSWORD": "app-password",
    "DEMO_EMAIL_TO": "ops@example.com",  # present on purpose: the invite must ignore it
}
ORGANIZER = "noc.maintenance@example.com"
ATTENDEES = ("fe.embakasi@example.com", "egypro.fibre.ke@example.com")


@pytest.fixture()
def clean_hub():
    hub._history.clear()
    yield hub
    hub._history.clear()


def _invite(**over) -> ics.WindowInvite:
    fields = dict(
        window_id="mw-0001",
        uid=ics.stable_uid("mw-0001", domain="noc.example.com"),
        starts_at=datetime(2026, 9, 21, 22, 0),
        ends_at=datetime(2026, 9, 22, 2, 0),
        summary="Planned fibre splice - Embakasi ring",
        organizer=ORGANIZER,
        attendees=ATTENDEES,
        sequence=0,
        operator_id="safaricom",
        location="SFC-NBIE-HUB-EMB",
    )
    fields.update(over)
    return ics.WindowInvite(**fields)


def _payload(**over) -> dict:
    payload = ics.invite_outbox_payload(_invite())
    payload.update(over)
    return payload


def _queue(session, payload: dict, *, key: str = "ics:safaricom:mw-0001@noc.example.com:REQUEST:0", **kw) -> OutboxRow:
    row = enqueue(session, kind=ICS_INVITE, idempotency_key=key, payload=payload, operator_id="safaricom", **kw)
    session.commit()
    return row


def _row(session) -> OutboxRow:
    return session.scalars(select(OutboxRow).where(OutboxRow.kind == ICS_INVITE)).one()


def _spy_messages(monkeypatch, result: EmailResult | None = None) -> list:
    """Capture the finished message the transmitter hands the adapter."""
    seen: list = []

    def fake(msg, *, to=None):
        seen.append(msg)
        return result or EmailResult(ok=True, mode="mock", detail="captured", to=list(ATTENDEES))

    monkeypatch.setattr(outbox.email_smtp, "send_message", fake)
    return seen


class ProbeSMTP:
    """``smtplib.SMTP`` stand-in. ``on_send`` runs at the instant the message leaves."""

    on_send = None
    sent: list = []

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
        ProbeSMTP.sent.append(msg)
        if ProbeSMTP.on_send is not None:
            ProbeSMTP.on_send(msg)


@pytest.fixture()
def probe(monkeypatch):
    ProbeSMTP.sent = []
    ProbeSMTP.on_send = None
    monkeypatch.setattr(smtplib, "SMTP", ProbeSMTP)
    yield ProbeSMTP
    ProbeSMTP.sent = []
    ProbeSMTP.on_send = None


# --- the transmitter exists, and addresses the attendees ---------------------------------------


def test_the_kind_has_a_transmitter():
    assert ICS_INVITE in outbox._TRANSMITTERS


def test_the_invite_goes_to_the_attendees_and_never_to_the_demo_mailbox(tmp_db, monkeypatch, clean_hub):
    settings, session = tmp_db
    monkeypatch.setenv("DEMO_EMAIL_TO", "ops@example.com")  # configured, and irrelevant here
    _queue(session, _payload())
    drain_once(session)
    row = _row(session)
    assert row.status == SENT and row.provider == "mock"
    assert "ops@example.com" not in (row.last_error or "")
    # The adapter's mock detail names who it WOULD have sent to: the attendees, not the demo box.
    assert row.last_error is None
    sent_to = json.loads(row.payload_json)["to"]
    assert sent_to == list(ATTENDEES)


def test_the_message_keeps_the_organizer_as_from_and_carries_the_calendar_part(tmp_db, monkeypatch):
    settings, session = tmp_db
    seen = _spy_messages(monkeypatch)
    _queue(session, _payload())
    drain_once(session)

    (msg,) = seen
    assert msg["From"] == ORGANIZER  # RFC 6047 §3 — a mismatch is silently dropped by clients
    assert msg["To"] == ", ".join(ATTENDEES)
    calendar = [p for p in msg.walk() if p.get_content_type() == "text/calendar"]
    assert calendar, "no text/calendar part survived"
    assert calendar[0].get_param("method") == "REQUEST"  # what draws Accept/Decline
    assert "BEGIN:VEVENT" in calendar[0].get_content()


# --- refusals ----------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [{"payload_version": 2}, {"payload_version": 0}])
def test_a_payload_version_this_dispatcher_does_not_know_is_refused(tmp_db, monkeypatch, bad, clean_hub):
    settings, session = tmp_db
    seen = _spy_messages(monkeypatch)
    _queue(session, _payload(**bad))
    drain_once(session)
    row = _row(session)
    assert row.status == DEAD and "refusing to guess" in (row.last_error or "")
    assert seen == []  # nothing was built and nothing was sent


def test_a_payload_with_no_attendees_is_refused(tmp_db, monkeypatch, clean_hub):
    settings, session = tmp_db
    seen = _spy_messages(monkeypatch)
    _queue(session, _payload(to=[]))
    drain_once(session)
    assert _row(session).status == DEAD
    assert seen == []


def test_an_unapproved_invite_is_refused_by_the_approval_gate(tmp_db, monkeypatch, clean_hub):
    """``ICS_INVITE`` is a channel kind, so the dispatcher's own second gate applies to it."""
    settings, session = tmp_db
    seen = _spy_messages(monkeypatch)
    _queue(session, _payload(), requires_hitl=True)
    drain_once(session)
    row = _row(session)
    assert row.status == REJECTED_UNAPPROVED and seen == []


# --- the configured relay: the transfer record, then the send ----------------------------------


def test_the_transfer_record_is_written_before_the_invite_leaves(tmp_db, monkeypatch, probe, clean_hub):
    """Attendee addresses are personal data (§7.5.6): same reg 41(2) row as an outage e-mail,
    committed BEFORE the bytes move — the ordering the module docstring argues for."""
    settings, session = tmp_db
    for key, value in SMTP_ENV.items():
        monkeypatch.setenv(key, value)
    durable: dict = {}

    def at_send(msg):
        other = get_session()
        try:
            rows = list(other.scalars(select(AuditRow).where(AuditRow.action == "external.call")))
            durable["registered"] = [json.loads(r.payload_json)["recipient"] for r in rows]
        finally:
            other.close()

    ProbeSMTP.on_send = at_send
    _queue(session, _payload())
    drain_once(session)

    row = _row(session)
    assert row.status == SENT and row.provider == "smtp"
    assert durable["registered"] == ["Gmail SMTP"]  # already committed when the invite left
    (msg,) = ProbeSMTP.sent
    assert msg["From"] == ORGANIZER and msg["To"] == ", ".join(ATTENDEES)
    register = json.loads(
        session.scalars(select(AuditRow).where(AuditRow.action == "external.call")).one().payload_json
    )
    assert register["recipient_country"] == "US" and register["cross_border"] is True
    assert "attendee mailbox addresses" in register["data_description"]


def test_no_relay_no_attendees_means_no_register_row(tmp_db, monkeypatch):
    """A mock send writes nothing: the register states what crossed the border, not intentions."""
    settings, session = tmp_db
    job = OutboxRow(id="x", operator_id="safaricom", kind=ICS_INVITE, payload_json=json.dumps(_payload()), attempts=1)
    assert outbox.transfer_plan(job) is None  # EMAIL_ENABLED is off in the suite
    for key, value in SMTP_ENV.items():
        monkeypatch.setenv(key, value)
    assert outbox.transfer_plan(job) is not None
    empty = OutboxRow(id="y", operator_id="safaricom", kind=ICS_INVITE, payload_json=json.dumps(_payload(to=[])), attempts=1)
    assert outbox.transfer_plan(empty) is None


# --- SMTP failures are classified like the EMAIL path ------------------------------------------


@pytest.mark.parametrize(
    "exc,expected",
    [
        (smtplib.SMTPResponseException(451, b"try again later"), FAILED),
        (smtplib.SMTPResponseException(550, b"mailbox unavailable"), DEAD),
    ],
)
def test_smtp_reply_codes_decide_retry_or_death(tmp_db, monkeypatch, probe, exc, expected, clean_hub):
    settings, session = tmp_db
    for key, value in SMTP_ENV.items():
        monkeypatch.setenv(key, value)

    def boom(msg):
        raise exc

    ProbeSMTP.on_send = boom
    _queue(session, _payload())
    drain_once(session)
    row = _row(session)
    # A transient failure with attempts left goes back to PENDING; a permanent one is DEAD.
    assert row.status == ("PENDING" if expected == FAILED else DEAD)
    assert "SMTP send failed" in (row.last_error or "")


# --- the adapter entry point itself ------------------------------------------------------------


def test_send_message_does_not_borrow_the_demo_mailbox(monkeypatch):
    monkeypatch.setenv("DEMO_EMAIL_TO", "ops@example.com")
    msg = ics.build_imip_message(_payload())
    del msg["To"]
    result = email_smtp.send_message(msg)
    assert result.ok is True and result.to == [] and "No recipients" in result.detail


def test_send_message_keeps_a_caller_set_from_and_fills_an_absent_one(monkeypatch, probe):
    for key, value in SMTP_ENV.items():
        monkeypatch.setenv(key, value)
    msg = ics.build_imip_message(_payload())
    assert email_smtp.send_message(msg).mode == "smtp"
    assert ProbeSMTP.sent[-1]["From"] == ORGANIZER

    plain = email_smtp.EmailMessage()
    plain["Subject"] = "no organiser here"
    plain["To"] = "fe@example.com"
    plain.set_content("body")
    assert email_smtp.send_message(plain).mode == "smtp"
    assert ProbeSMTP.sent[-1].get_all("From") == ["noc@example.com"]  # exactly one From header


def test_send_email_is_untouched_by_the_new_entry_point(monkeypatch, probe):
    """The demo path must not regress: same recipients, same mode, same detail string."""
    monkeypatch.setenv("EMAIL_ENABLED", "false")
    monkeypatch.setenv("DEMO_EMAIL_TO", "ops@example.com")
    result = email_smtp.send_email(subject="s", body="b")
    assert (result.ok, result.mode, result.to) == (True, "mock", ["ops@example.com"])
    assert result.detail == "SMTP not configured — would have sent to ['ops@example.com'] (mock)"
    assert ProbeSMTP.sent == []
