"""Email volume safeguards (§7.9.1, §10.3 ✱; CONFORMANCE A-07 + C-06).

Three things a free Gmail sender needs and the dispatcher did not do:

* **the v2 ``send_email`` signature** — ``to`` is where the message goes, and the demo inbox is
  reached only through the named ``DEMO_MAILBOX`` (or ``send_demo_email``). ``to=None`` — the
  shape of the Phase 4 regulator-notice mis-delivery — is refused; an empty list is the
  historical mock verbatim; ``headers`` are carried onto the message;
* **≤ 100 recipients per message**: a resolved audience is split into Bcc batches; 100 or fewer
  is one call, exactly as before;
* **``EMAIL_DAILY_CAP``** counted from ``outbox`` rows the relay accepted in the rolling 24 h:
  one WorkNote as the count crosses 80 %; past the cap a P1 still goes (with a note) and
  anything else is DEFERRED to the instant the window frees a slot — PENDING, no attempt spent,
  one note, no transfer record — never dropped.

And the property everything else rests on: with mail not configured (the demo, the whole
suite) or the cap not reached, nothing observable changes. No socket is opened anywhere here:
``smtplib.SMTP`` is a probe.
"""

from __future__ import annotations

import json
import logging
import re
import smtplib
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select, update

from noc_agents.adapters import email_smtp
from noc_agents.adapters.email_smtp import (
    DEMO_MAILBOX,
    RECIPIENTS_PER_MESSAGE,
    EmailResult,
    send_demo_email,
    send_email,
)
from noc_agents.config import get_settings
from noc_agents.db.models import IncidentRow, OutboxRow, WorkNoteRow
from noc_agents.orchestrator import outbox
from noc_agents.orchestrator.outbox import DEAD, PENDING, REJECTED_UNAPPROVED, SENT, drain_once, enqueue
from noc_agents.realtime.hub import hub
from noc_agents.services import notify
from noc_agents.services.notify import (
    DEMO_RECIPIENTS_REF,
    EMAIL_CAP_DEFER_PREFIX,
    EMAIL_CAP_WINDOW,
    EmailBudget,
    batch_recipients,
    email_daily_cap,
    email_headers,
    email_message_count,
)
from noc_agents.services.validators import EMAIL_RECIPIENTS_PER_MESSAGE

NOW = datetime(2026, 9, 21, 12, 0, 0)  # naive UTC, the storage contract
MOCK_EMPTY = "No DEMO_EMAIL_TO / GMAIL_ADDRESS — email body stored only (mock)"  # pinned by the golden test
REF = "audiences.MANAGEMENT"
SMTP_ENV = {
    "EMAIL_ENABLED": "true",
    "GMAIL_ADDRESS": "noc@example.com",
    "GMAIL_APP_PASSWORD": "app-password",
    "DEMO_EMAIL_TO": "ops@example.com",
}


# --- fixtures ---------------------------------------------------------------------------------


class ProbeSMTP:
    """``smtplib.SMTP`` stand-in: records each message; ``on_send`` may raise to simulate a reply."""

    sent: list = []
    on_send = None

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
    ProbeSMTP.sent, ProbeSMTP.on_send = [], None
    monkeypatch.setattr(smtplib, "SMTP", ProbeSMTP)
    yield ProbeSMTP
    ProbeSMTP.sent, ProbeSMTP.on_send = [], None


@pytest.fixture()
def smtp_on(monkeypatch, probe):
    """Mail genuinely configured, so the adapter would open a socket — into the probe."""
    for key, value in SMTP_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("EMAIL_DAILY_CAP", raising=False)
    return probe


@pytest.fixture()
def transfers(monkeypatch):
    """The reg 41(2) register is proved in test_transfer_register.py; here it is only observed."""
    calls: list[dict] = []
    monkeypatch.setattr(outbox, "record_transfer", lambda session, **kw: calls.append(kw))
    return calls


@pytest.fixture()
def clean_hub():
    hub._history.clear()
    yield hub
    hub._history.clear()


def _incident(session, *, priority="P3", number="INC000901") -> IncidentRow:
    inc = IncidentRow(
        operator_id="safaricom",
        incident_number=number,
        priority=priority,
        site_id="SFC-NBIE-HUB-EMB",
        region_code="NBI_E",
        correlation_fingerprint=f"fp|{number}",
    )
    session.add(inc)
    session.commit()
    return inc


def _seed_sent(session, n: int, *, at: datetime, provider="smtp", kind="EMAIL", tag="seed") -> None:
    """``n`` rows the relay already accepted at ``at`` — what the count reads."""
    for i in range(n):
        session.add(
            OutboxRow(
                operator_id="safaricom",
                kind=kind,
                idempotency_key=f"{tag}:{kind}:{provider}:{at.isoformat()}:{i}",
                payload_json="{}",
                status=SENT,
                provider=provider,
                sent_at=at,
            )
        )
    session.commit()


def _payload(inc: IncidentRow | None, *, ref=DEMO_RECIPIENTS_REF, **extra) -> dict:
    return {
        "operator_id": "safaricom",
        "incident_number": inc.incident_number if inc is not None else None,
        "audience": "RNIO",
        # As every incident producer writes it (compose_email, regulatory.notice_text): "[Px] INC… | …"
        "subject": f"[{inc.priority}] {inc.incident_number} | test" if inc is not None else "Shift handover | test",
        "body": "body",
        "recipients_ref": ref,
        "broadcast_ids": [],
        **extra,
    }


def _queue(session, inc: IncidentRow | None, key: str, **kw) -> OutboxRow:
    payload = _payload(inc, **{k: v for k, v in kw.items() if k in ("ref", "provider_params", "headers")})
    row = enqueue(
        session,
        kind="EMAIL",
        idempotency_key=key,
        payload=payload,
        incident_id=inc.id if inc is not None else None,
        operator_id="safaricom",
        requires_hitl=kw.get("requires_hitl", False),
    )
    session.commit()
    return row


def _get(session, row_id: str) -> OutboxRow:
    session.expire_all()
    return session.get(OutboxRow, row_id)


def _notes(session, inc: IncidentRow) -> list[str]:
    session.expire_all()
    return [n.body for n in session.scalars(select(WorkNoteRow).where(WorkNoteRow.incident_id == inc.id))]


def _settings_with(register: dict[str, list[str]]):
    settings = get_settings("safaricom").model_copy(deep=True)
    settings.operator.notification_recipients = register
    return settings


def _addresses(n: int) -> list[str]:
    return [f"eng{i:03d}@example.com" for i in range(n)]


# --- the adapter: where a message goes ----------------------------------------------------------


def test_to_none_is_refused_not_rerouted_to_the_demo_mailbox(monkeypatch):
    """The Phase 4 bug had exactly this shape: no recipients resolved, demo inbox filled in."""
    monkeypatch.setenv("DEMO_EMAIL_TO", "ops@example.com")
    result = send_email(to=None, subject="s", body="b")
    assert (result.ok, result.mode, result.to) == (False, "error", [])
    assert "DEMO_MAILBOX" in result.detail and "ops@example.com" not in result.detail


def test_an_empty_list_is_the_historical_mock_and_never_the_demo_mailbox(monkeypatch):
    monkeypatch.setenv("DEMO_EMAIL_TO", "ops@example.com")
    result = send_email(to=[], subject="s", body="b")
    assert (result.ok, result.mode, result.detail, result.to) == (True, "mock", MOCK_EMPTY, [])


def test_the_demo_mailbox_is_one_named_path_whichever_way_it_is_spelled(monkeypatch):
    monkeypatch.setenv("DEMO_EMAIL_TO", "ops@example.com")
    explicit = send_email(to=DEMO_MAILBOX, subject="s", body="b")
    named = send_demo_email(subject="s", body="b")
    compat = send_email(subject="s", body="b")  # main.py's /email/test call until integration
    assert explicit == named == compat
    assert (explicit.mode, explicit.to) == ("mock", ["ops@example.com"])
    assert repr(DEMO_MAILBOX) == "DEMO_MAILBOX"


def test_the_demo_mailbox_with_nothing_configured_is_the_pinned_mock_string():
    assert send_demo_email(subject="s", body="b") == EmailResult(ok=True, mode="mock", detail=MOCK_EMPTY, to=[])


def test_more_than_one_hundred_recipients_on_one_message_is_refused_not_truncated(probe):
    result = send_email(to=_addresses(RECIPIENTS_PER_MESSAGE + 1), subject="s", body="b")
    assert result.ok is False and "must batch" in result.detail and len(result.to) == 101
    assert probe.sent == []
    assert RECIPIENTS_PER_MESSAGE == EMAIL_RECIPIENTS_PER_MESSAGE == 100  # one number, two homes


def test_one_recipient_is_addressed_exactly_as_before(smtp_on):
    assert send_email(to=["fe@example.com"], subject="s", body="b").mode == "smtp"
    (msg,) = smtp_on.sent
    assert msg["To"] == "fe@example.com" and msg["Bcc"] is None


def test_a_batch_goes_bcc_so_recipients_do_not_see_each_other(smtp_on):
    batch = _addresses(3)
    result = send_email(to=batch, subject="s", body="b")
    assert result.ok and result.to == batch
    (msg,) = smtp_on.sent
    assert msg["To"] == "noc@example.com"  # the sender, never another recipient
    assert [a.strip() for a in msg["Bcc"].split(",")] == batch


def test_headers_reach_the_message_but_cannot_readdress_it(smtp_on):
    send_email(
        to=["fe@example.com"],
        subject="s",
        body="b",
        headers={"List-Unsubscribe": "<mailto:unsub@example.com>", "To": "attacker@example.com", "from": "x@example.com"},
    )
    (msg,) = smtp_on.sent
    assert msg["List-Unsubscribe"] == "<mailto:unsub@example.com>"
    assert msg.get_all("To") == ["fe@example.com"] and msg.get_all("From") == ["noc@example.com"]


def test_send_message_the_imip_entry_point_is_untouched(monkeypatch):
    """The Phase 5 invite path keeps its own rules: no demo fallback, its own mock string."""
    monkeypatch.setenv("DEMO_EMAIL_TO", "ops@example.com")
    msg = email_smtp.EmailMessage()
    msg["Subject"] = "invite"
    msg.set_content("b")
    result = email_smtp.send_message(msg)
    assert (result.ok, result.to) == (True, []) and "No recipients" in result.detail


# --- batching in the dispatcher ---------------------------------------------------------------


def test_batches_are_consecutive_and_at_most_one_hundred():
    people = _addresses(250)
    batches = batch_recipients(people)
    assert [len(b) for b in batches] == [100, 100, 50]
    assert [a for b in batches for a in b] == people
    assert batch_recipients(_addresses(100)) == [_addresses(100)]


def _spy_send(monkeypatch, *, fail_batch: int | None = None, reply: str = "(550, b'mailbox unavailable')") -> list[dict]:
    calls: list[dict] = []

    def fake(**kw):
        calls.append(kw)
        if fail_batch is not None and len(calls) == fail_batch:
            return EmailResult(ok=False, mode="error", detail=f"SMTP send failed: SMTPResponseException: {reply}", to=list(kw["to"]))
        return EmailResult(ok=True, mode="smtp", detail=f"Sent to {len(kw['to'])}", to=list(kw["to"]))

    monkeypatch.setattr(notify, "send_email", fake)
    return calls


def test_a_large_audience_is_sent_in_batches_of_one_hundred(monkeypatch):
    people = _addresses(250)
    monkeypatch.setattr(notify, "get_settings", lambda *_a, **_k: _settings_with({REF: people}))
    calls = _spy_send(monkeypatch)
    result = notify.transmit_email(_payload(None, ref=REF))
    assert [len(c["to"]) for c in calls] == [100, 100, 50]
    assert result.ok and result.mode == "smtp" and result.to == people
    assert "250 recipients in 3 messages" in result.detail


def test_an_audience_of_one_hundred_or_fewer_is_one_unchanged_call(monkeypatch):
    """The no-regression case: same arguments as before batching existed, same result object."""
    monkeypatch.setattr(notify, "get_settings", lambda *_a, **_k: _settings_with({REF: _addresses(100)}))
    sent: list[EmailResult] = []

    def fake(**kw):
        sent.append(EmailResult(ok=True, mode="mock", detail="d", to=list(kw["to"])))
        assert set(kw) == {"subject", "body", "to"}  # no headers argument when there are none
        return sent[-1]

    monkeypatch.setattr(notify, "send_email", fake)
    result = notify.transmit_email(_payload(None, ref=REF))
    assert len(sent) == 1 and result is sent[0]


def test_a_failed_batch_does_not_stop_the_rest_and_fails_the_row_by_its_own_reply(monkeypatch):
    monkeypatch.setattr(notify, "get_settings", lambda *_a, **_k: _settings_with({REF: _addresses(250)}))
    calls = _spy_send(monkeypatch, fail_batch=2)
    result = notify.transmit_email(_payload(None, ref=REF))
    assert len(calls) == 3  # batch 3 still went
    assert result.ok is False and result.mode == "error" and "batch 2" in result.detail
    assert outbox._email_error_is_transient(result.detail) is False  # 5xx → DEAD


def test_a_transient_batch_failure_is_retried_as_a_whole(monkeypatch):
    monkeypatch.setattr(notify, "get_settings", lambda *_a, **_k: _settings_with({REF: _addresses(150)}))
    _spy_send(monkeypatch, fail_batch=1, reply="(451, b'try again later')")
    result = notify.transmit_email(_payload(None, ref=REF))
    assert result.ok is False and outbox._email_error_is_transient(result.detail) is True


def test_the_batches_leave_as_separate_bcc_messages_through_the_real_adapter(tmp_db, smtp_on, transfers, clean_hub, monkeypatch):
    _settings, session = tmp_db
    people = _addresses(205)
    monkeypatch.setattr(notify, "get_settings", lambda *_a, **_k: _settings_with({REF: people}))
    inc = _incident(session)
    row = _queue(session, inc, "EMAIL:batch:1", ref=REF)
    report = drain_once(session, now=NOW)
    assert report.sent == 1 and _get(session, row.id).status == SENT
    assert [len(m["Bcc"].split(",")) for m in smtp_on.sent] == [100, 100, 5]
    assert report.outcomes[row.id].delivery["to"] == people
    # Review E01: one row, three messages — and the cap counts three.
    assert json.loads(_get(session, row.id).payload_json)[notify.SMTP_ACCEPTED_KEY] == [
        {"at": NOW.isoformat(), "messages": 3}
    ]
    assert notify.email_budget(session, now=NOW).sent == 3


def test_a_result_object_without_an_accepted_count_is_read_by_shape():
    """Test doubles (and any older result type) carry only ok/mode/detail/to: an ok SMTP result
    counts one message, anything else none — never an AttributeError that kills the row."""
    from types import SimpleNamespace

    assert notify.smtp_messages_accepted(SimpleNamespace(ok=True, mode="smtp", detail="d", to=["a@example.com"])) == 1
    assert notify.smtp_messages_accepted(SimpleNamespace(ok=True, mode="mock", detail="d", to=[])) == 0
    assert notify.smtp_messages_accepted(SimpleNamespace(ok=False, mode="error", detail="d", to=[])) == 0
    assert notify.smtp_messages_accepted(EmailResult(ok=False, mode="error", detail="d", to=[], accepted=2)) == 2


def test_a_partial_batch_failure_still_counts_what_left_and_a_retry_counts_again(
    tmp_db, smtp_on, transfers, clean_hub, monkeypatch
):
    """Review E01(b), the reviewer's ``test_partial_batch_failure_uncounted``: batch 2 of 3 gets a
    421, batches 1 and 3 have left. The row retries (provider=error, no sent_at) — and the two
    messages are counted anyway. The retry re-sends all three, and all three count again,
    because they really were sent again."""
    _settings, session = tmp_db
    monkeypatch.setattr(notify, "get_settings", lambda *_a, **_k: _settings_with({REF: _addresses(250)}))
    inc = _incident(session)
    row = _queue(session, inc, "EMAIL:partial:1", ref=REF)

    def second_message_deferred(msg):
        if len(smtp_on.sent) == 2:
            raise smtplib.SMTPResponseException(421, b"try again later")

    smtp_on.on_send = second_message_deferred
    first = drain_once(session, now=NOW)
    retried = _get(session, row.id)
    assert first.retried == 1 and retried.status == PENDING and retried.provider == "error" and retried.sent_at is None
    assert len(smtp_on.sent) == 3  # batch 3 still went
    assert notify.email_budget(session, now=NOW).sent == 2  # was 0 before the fix

    smtp_on.on_send = None
    session.execute(update(OutboxRow).where(OutboxRow.id == row.id).values(next_attempt_at=None))
    session.commit()
    later = NOW + timedelta(minutes=1)
    assert drain_once(session, now=later).sent == 1
    assert notify.email_budget(session, now=later).sent == 2 + 3


# --- header hints ---------------------------------------------------------------------------------


def test_the_list_unsubscribe_hint_becomes_a_header_only_with_a_configured_target(monkeypatch):
    hint = {"provider_params": {"list_unsubscribe": True}}
    monkeypatch.delenv(notify.LIST_UNSUBSCRIBE_ENV, raising=False)
    assert email_headers(hint) == {}  # no invented unsubscribe target
    monkeypatch.setenv(notify.LIST_UNSUBSCRIBE_ENV, "mailto:unsubscribe@example.com")
    assert email_headers(hint) == {"List-Unsubscribe": "<mailto:unsubscribe@example.com>"}
    assert email_headers({"provider_params": {"list_unsubscribe": False}}) == {}  # internal audience
    explicit = {**hint, "headers": {"List-Unsubscribe": "<https://example.com/u>"}}
    assert email_headers(explicit) == {"List-Unsubscribe": "<https://example.com/u>"}


def test_header_hints_are_carried_to_the_adapter(monkeypatch):
    monkeypatch.setenv(notify.LIST_UNSUBSCRIBE_ENV, "mailto:unsubscribe@example.com")
    monkeypatch.setattr(notify, "get_settings", lambda *_a, **_k: _settings_with({REF: ["ca@example.com"]}))
    calls = _spy_send(monkeypatch)
    notify.transmit_email(_payload(None, ref=REF, provider_params={"list_unsubscribe": True}))
    assert calls[0]["headers"] == {"List-Unsubscribe": "<mailto:unsubscribe@example.com>"}


# --- the cap: arithmetic ----------------------------------------------------------------------------


@pytest.mark.parametrize("raw,cap", [(None, 400), ("", 400), ("1600", 1600), ("0", 0), ("abc", 400), ("-5", 400)])
def test_the_cap_reads_the_environment_and_a_typo_never_switches_it_off(monkeypatch, raw, cap):
    if raw is None:
        monkeypatch.delenv("EMAIL_DAILY_CAP", raising=False)
    else:
        monkeypatch.setenv("EMAIL_DAILY_CAP", raw)
    assert email_daily_cap() == cap


def test_the_eighty_percent_line_is_a_level_from_the_320th_message():
    """Level, not edge (review E07): whether the note is still owed is decided by looking for one."""
    levels = [
        EmailBudget(cap=400, sent=sent, requested=1, oldest_sent_at=None, now=NOW).reaches_warning for sent in range(0, 400)
    ]
    assert levels.index(True) == 319 and all(levels[319:])  # from the 320th message on
    assert EmailBudget(cap=400, sent=319, requested=0, oldest_sent_at=None, now=NOW).reaches_warning is False
    assert EmailBudget(cap=400, sent=399, requested=1, oldest_sent_at=None, now=NOW).exhausted is False  # the 400th goes
    assert EmailBudget(cap=400, sent=400, requested=1, oldest_sent_at=None, now=NOW).exhausted is True
    assert EmailBudget(cap=0, sent=10_000, requested=1, oldest_sent_at=None, now=NOW).exhausted is False  # 0 = off


def test_message_count_per_row(monkeypatch):
    assert email_message_count(_payload(None)) == 1  # the demo mailbox
    monkeypatch.setattr(notify, "get_settings", lambda *_a, **_k: _settings_with({REF: _addresses(250)}))
    assert email_message_count(_payload(None, ref=REF)) == 3
    monkeypatch.setattr(notify, "get_settings", lambda *_a, **_k: _settings_with({}))
    assert email_message_count(_payload(None, ref=REF)) == 0  # refused at transmit, costs no quota


# --- the cap: what is counted ---------------------------------------------------------------------


def _decide(session, inc, key="EMAIL:decide:1", **kw):
    row = _queue(session, inc, key, **kw)
    return notify.email_cap_decision(session, _get(session, row.id), now=NOW)


def test_the_count_is_smtp_rows_in_the_rolling_day_from_the_outbox(tmp_db, monkeypatch):
    _settings, session = tmp_db
    inc = _incident(session)
    _seed_sent(session, 3, at=NOW - timedelta(hours=1))  # counted
    _seed_sent(session, 2, at=NOW - timedelta(hours=23, minutes=59))  # counted: still inside the window
    _seed_sent(session, 4, at=NOW - EMAIL_CAP_WINDOW)  # not: exactly 24 h old has left it
    _seed_sent(session, 5, at=NOW - timedelta(hours=1), provider="mock", tag="mock")  # not: never reached the relay
    _seed_sent(session, 6, at=NOW - timedelta(hours=1), kind="SMS", tag="sms")  # not: another channel
    _seed_sent(session, 7, at=NOW - timedelta(hours=1), kind="ICS_INVITE", tag="ics")  # counted: same relay account
    decision = _decide(session, inc)
    assert decision.budget.sent == 3 + 2 + 7
    assert decision.budget.oldest_sent_at == NOW - timedelta(hours=23, minutes=59)


def test_the_count_is_global_not_per_operator(tmp_db):
    """The quota belongs to the one SMTP account, whoever's incident the mail was about."""
    _settings, session = tmp_db
    inc = _incident(session)
    _seed_sent(session, 4, at=NOW - timedelta(hours=1))
    session.execute(update(OutboxRow).where(OutboxRow.kind == "EMAIL").values(operator_id="airtel"))
    session.commit()
    assert _decide(session, inc).budget.sent == 4


# --- the cap: through the drain --------------------------------------------------------------------


def test_below_eighty_percent_nothing_is_written(tmp_db, smtp_on, transfers, clean_hub, monkeypatch):
    _settings, session = tmp_db
    monkeypatch.setenv("EMAIL_DAILY_CAP", "10")
    inc = _incident(session)
    _seed_sent(session, 6, at=NOW - timedelta(hours=1))
    row = _queue(session, inc, "EMAIL:below:1")
    report = drain_once(session, now=NOW)
    assert report.sent == 1 and _get(session, row.id).status == SENT
    assert not any("volume" in n or "EMAIL_DAILY_CAP" in n for n in _notes(session, inc))


def test_crossing_eighty_percent_writes_one_note_and_still_sends(tmp_db, smtp_on, transfers, clean_hub, monkeypatch):
    _settings, session = tmp_db
    monkeypatch.setenv("EMAIL_DAILY_CAP", "10")
    inc = _incident(session)
    _seed_sent(session, 7, at=NOW - timedelta(hours=1))
    first = _queue(session, inc, "EMAIL:cross:1")
    second = _queue(session, inc, "EMAIL:cross:2")
    report = drain_once(session, now=NOW)
    assert report.sent == 2 and {_get(session, first.id).status, _get(session, second.id).status} == {SENT}
    warnings = [n for n in _notes(session, inc) if "EMAIL volume warning" in n]
    assert len(warnings) == 1  # the 8th message crossed; the 9th did not write another
    assert "8 of EMAIL_DAILY_CAP=10" in warnings[0] and "P1 still sends" in warnings[0]
    assert len(smtp_on.sent) == 2


def test_a_crossing_send_that_fails_and_retries_writes_the_note_once_after_it_really_went(
    tmp_db, smtp_on, transfers, clean_hub, monkeypatch
):
    """Review E07, the reviewer's repro: the crossing send gets a 451, retries an hour later and
    goes. Before the fix: two notes, the first for a send that never happened."""
    _settings, session = tmp_db
    monkeypatch.setenv("EMAIL_DAILY_CAP", "10")
    inc = _incident(session)
    _seed_sent(session, 7, at=NOW - timedelta(hours=1))
    row = _queue(session, inc, "EMAIL:e07:1")

    def busy(msg):
        raise smtplib.SMTPResponseException(451, b"try again later")

    smtp_on.on_send = busy
    assert drain_once(session, now=NOW).retried == 1
    assert not [n for n in _notes(session, inc) if "EMAIL volume warning" in n]  # nothing left: no note

    smtp_on.on_send = None
    session.execute(update(OutboxRow).where(OutboxRow.id == row.id).values(next_attempt_at=None))
    session.commit()
    assert drain_once(session, now=NOW + timedelta(hours=1)).sent == 1
    assert len([n for n in _notes(session, inc) if "EMAIL volume warning" in n]) == 1


def test_a_line_crossed_by_calendar_invites_is_still_noted_by_the_next_incident_mail(
    tmp_db, smtp_on, transfers, clean_hub, monkeypatch
):
    """Invites spend the same quota but are not gated; the note is owed until one is written."""
    _settings, session = tmp_db
    monkeypatch.setenv("EMAIL_DAILY_CAP", "10")
    inc = _incident(session)
    _seed_sent(session, 8, at=NOW - timedelta(hours=1), kind="ICS_INVITE", tag="ics")  # 80 % reached by invites
    _queue(session, inc, "EMAIL:ics-crossed:1")
    assert drain_once(session, now=NOW).sent == 1
    assert len([n for n in _notes(session, inc) if "EMAIL volume warning" in n]) == 1


def test_at_the_cap_a_p3_is_deferred_not_dropped(tmp_db, smtp_on, transfers, clean_hub, monkeypatch):
    _settings, session = tmp_db
    monkeypatch.setenv("EMAIL_DAILY_CAP", "10")
    inc = _incident(session, priority="P3")
    oldest = NOW - timedelta(hours=5)
    _seed_sent(session, 1, at=oldest, tag="old")
    _seed_sent(session, 9, at=NOW - timedelta(hours=1))
    row = _queue(session, inc, "EMAIL:defer:1")

    report = drain_once(session, now=NOW)

    held = _get(session, row.id)
    assert (report.claimed, report.sent, report.deferred, report.dead, report.retried) == (1, 0, 1, 0, 0)
    assert held.status == PENDING and held.attempts == 0  # nothing transmitted, no attempt spent
    assert held.next_attempt_at == oldest + EMAIL_CAP_WINDOW  # the instant a slot frees, not a guess
    assert held.last_error.startswith(EMAIL_CAP_DEFER_PREFIX) and held.sent_at is None
    assert smtp_on.sent == [] and transfers == []  # no send, so no reg 41(2) record either
    types = {e["type"] for e in hub._history}
    assert "email.sent" not in types and "outbox.failed" not in types  # no red row on the wallboard
    (note,) = [n for n in _notes(session, inc) if "EMAIL_DAILY_CAP" in n]
    assert "HELD, not dropped" in note and "P3" in note

    # Not reclaimed before its time ...
    assert drain_once(session, now=NOW + timedelta(hours=1)).claimed == 0
    # ... and once the oldest send has left the window, it goes.
    later = drain_once(session, now=oldest + EMAIL_CAP_WINDOW)
    sent = _get(session, row.id)
    assert later.sent == 1 and sent.status == SENT and sent.attempts == 1 and sent.last_error is None
    assert len(smtp_on.sent) == 1 and len(transfers) == 1


def test_a_long_storm_never_turns_a_deferral_into_failed_or_a_flood_of_notes(tmp_db, smtp_on, transfers, clean_hub, monkeypatch):
    _settings, session = tmp_db
    monkeypatch.setenv("EMAIL_DAILY_CAP", "10")
    inc = _incident(session)
    _seed_sent(session, 10, at=NOW - timedelta(hours=1))
    row = _queue(session, inc, "EMAIL:storm:1")
    for tick in range(5):  # more deferrals than max_attempts (3)
        session.execute(update(OutboxRow).where(OutboxRow.id == row.id).values(next_attempt_at=None))
        session.commit()
        assert drain_once(session, now=NOW + timedelta(minutes=tick)).deferred == 1
    held = _get(session, row.id)
    assert held.status == PENDING and held.attempts == 0
    assert len([n for n in _notes(session, inc) if "EMAIL_DAILY_CAP" in n]) == 1


def test_at_the_cap_a_p1_is_sent_anyway_and_says_so(tmp_db, smtp_on, transfers, clean_hub, monkeypatch):
    _settings, session = tmp_db
    monkeypatch.setenv("EMAIL_DAILY_CAP", "10")
    inc = _incident(session, priority="P1")
    _seed_sent(session, 10, at=NOW - timedelta(hours=1))
    row = _queue(session, inc, "EMAIL:p1:1")
    report = drain_once(session, now=NOW)
    assert report.sent == 1 and report.deferred == 0 and _get(session, row.id).status == SENT
    assert len(smtp_on.sent) == 1 and len(transfers) == 1
    (note,) = [n for n in _notes(session, inc) if "EMAIL_DAILY_CAP" in n]
    assert "P1 notice was SENT anyway" in note
    assert any(e["type"] == "email.sent" for e in hub._history)


def test_an_audience_bigger_than_the_whole_cap_is_refused_because_it_can_never_fit(
    tmp_db, smtp_on, transfers, clean_hub, monkeypatch
):
    _settings, session = tmp_db
    monkeypatch.setenv("EMAIL_DAILY_CAP", "2")
    monkeypatch.setattr(notify, "get_settings", lambda *_a, **_k: _settings_with({REF: _addresses(250)}))
    inc = _incident(session, priority="P3")  # below P1: waiting could never make 3 messages fit in 2
    row = _queue(session, inc, "EMAIL:huge:1", ref=REF)
    report = drain_once(session, now=NOW)
    dead = _get(session, row.id)
    assert report.dead == 1 and dead.status == DEAD and "can never fit" in dead.last_error
    assert smtp_on.sent == [] and transfers == []
    assert any(e["type"] == "outbox.failed" for e in hub._history)


@pytest.mark.parametrize(
    "cap,audience,seeded,messages",
    [
        (1, 101, 0, 2),  # round-3 C4-a, the verifier's repro: an EMPTY window, and a P1 that alone needs 2 > 1
        (2, 250, 0, 3),
        (2, 250, 2, 3),  # and a full window as well
    ],
)
def test_a_p1_bigger_than_the_whole_cap_is_sent_with_the_override_note(
    tmp_db, smtp_on, transfers, clean_hub, monkeypatch, cap, audience, seeded, messages
):
    """Round-3 C4: the "can never fit" refusal ran BEFORE the P1 override and killed the P1 DEAD.
    A P1 is never held by the cap — not in a full window, and not for being bigger than the cap."""
    _settings, session = tmp_db
    monkeypatch.setenv("EMAIL_DAILY_CAP", str(cap))
    monkeypatch.setattr(notify, "get_settings", lambda *_a, **_k: _settings_with({REF: _addresses(audience)}))
    if seeded:
        _seed_sent(session, seeded, at=NOW - timedelta(hours=1))
    inc = _incident(session, priority="P1")
    row = _queue(session, inc, "EMAIL:p1-huge:1", ref=REF)
    report = drain_once(session, now=NOW)
    assert (report.sent, report.dead) == (1, 0) and _get(session, row.id).status == SENT
    assert len(smtp_on.sent) == messages and len(transfers) == 1
    (note,) = [n for n in _notes(session, inc) if "EMAIL_DAILY_CAP" in n]
    assert "P1 notice was SENT anyway" in note and f"({messages} message(s))" in note
    assert not any(e["type"] == "outbox.failed" for e in hub._history)


def test_an_unapproved_row_is_still_rejected_not_deferred(tmp_db, smtp_on, transfers, clean_hub, monkeypatch):
    """The approval gate outranks the cap: a cap decision must never turn a refusal into PENDING."""
    _settings, session = tmp_db
    monkeypatch.setenv("EMAIL_DAILY_CAP", "10")
    inc = _incident(session)
    _seed_sent(session, 10, at=NOW - timedelta(hours=1))
    row = _queue(session, inc, "EMAIL:unapproved:1", requires_hitl=True)
    report = drain_once(session, now=NOW)
    assert report.rejected == 1 and _get(session, row.id).status == REJECTED_UNAPPROVED
    assert not [n for n in _notes(session, inc) if "EMAIL_DAILY_CAP" in n]


def test_a_deferred_handover_mail_has_no_incident_so_the_finding_is_logged(
    tmp_db, smtp_on, transfers, clean_hub, monkeypatch, caplog
):
    _settings, session = tmp_db
    monkeypatch.setenv("EMAIL_DAILY_CAP", "10")
    _seed_sent(session, 10, at=NOW - timedelta(hours=1))
    row = _queue(session, None, "EMAIL:handover:1")
    with caplog.at_level(logging.WARNING, logger="noc_agents.services.notify"):
        report = drain_once(session, now=NOW)
    assert report.deferred == 1 and _get(session, row.id).status == PENDING
    assert "HELD, not dropped" in caplog.text and "non-incident" in caplog.text


def test_cap_zero_switches_it_off(tmp_db, smtp_on, transfers, clean_hub, monkeypatch):
    _settings, session = tmp_db
    monkeypatch.setenv("EMAIL_DAILY_CAP", "0")
    inc = _incident(session)
    _seed_sent(session, 50, at=NOW - timedelta(hours=1))
    row = _queue(session, inc, "EMAIL:off:1")
    assert drain_once(session, now=NOW).sent == 1 and _get(session, row.id).status == SENT


def _broken_count(*_a, **_k):
    raise RuntimeError("database is locked")


def test_a_failing_count_does_not_send_a_p3_and_ends_visibly(tmp_db, smtp_on, transfers, clean_hub, monkeypatch, caplog):
    """Review E05: fail-open only for a P1. Anything else is FAILED with the reason — retried with
    backoff, then terminal with ``outbox.failed`` — so a persistent fault cannot switch the cap
    off for every row."""
    _settings, session = tmp_db
    monkeypatch.setattr(notify, "email_cap_decision", _broken_count)
    inc = _incident(session, priority="P3")
    row = _queue(session, inc, "EMAIL:broken:p3")
    with caplog.at_level(logging.ERROR, logger="noc_agents.orchestrator.outbox"):
        first = drain_once(session, now=NOW)
    held = _get(session, row.id)
    assert first.retried == 1 and first.sent == 0 and held.status == PENDING
    assert "could not be checked" in held.last_error and "nothing was transmitted" in held.last_error
    assert "not transmitted, will retry" in caplog.text
    for tick in range(1, 3):  # the remaining attempts
        session.execute(update(OutboxRow).where(OutboxRow.id == row.id).values(next_attempt_at=None))
        session.commit()
        drain_once(session, now=NOW + timedelta(minutes=tick))
    assert _get(session, row.id).status == "FAILED"
    assert any(e["type"] == "outbox.failed" for e in hub._history)
    assert smtp_on.sent == [] and transfers == []


def test_a_failing_count_lets_a_p1_through_and_leaves_a_trace(tmp_db, smtp_on, transfers, clean_hub, monkeypatch, caplog):
    _settings, session = tmp_db
    monkeypatch.setattr(notify, "email_cap_decision", _broken_count)
    inc = _incident(session, priority="P1")
    row = _queue(session, inc, "EMAIL:broken:p1")
    with caplog.at_level(logging.ERROR, logger="noc_agents.orchestrator.outbox"):
        assert drain_once(session, now=NOW).sent == 1
    assert _get(session, row.id).status == SENT and len(smtp_on.sent) == 1
    (note,) = [n for n in _notes(session, inc) if "could not be checked" in n]
    assert "SENT without counting it against the cap" in note
    assert "sending uncounted" in caplog.text


@pytest.mark.parametrize("priority,sent", [("P1", True), ("P3", False)])
def test_a_database_that_refuses_every_read_still_lets_a_p1_through(
    tmp_db, smtp_on, transfers, clean_hub, monkeypatch, priority, sent
):
    """Round-3 C5, verifier 2's repro at the ENGINE (not patched functions): every SELECT during the
    cap gate fails, so the count AND the live priority lookup fail the same way. Before the fix the
    P1 fail-open collapsed to fail-closed (FAILED, nothing sent). The P1 decision now falls back to
    the priority the row was queued with, so it no longer depends on a second read that fails too;
    a P3 is still not sent."""
    import sqlite3

    from sqlalchemy import event

    _settings, session = tmp_db
    inc = _incident(session, priority=priority)
    row = _queue(session, inc, f"EMAIL:c5:{priority}")
    engine, state, refused = session.get_bind(), {"on": False}, []

    def refuse_reads(conn, cursor, statement, parameters, context, executemany):
        if state["on"] and statement.lstrip().upper().startswith("SELECT"):
            table = re.search(r"\bFROM\s+\"?(\w+)", statement)
            refused.append(table.group(1) if table else "?")
            raise sqlite3.OperationalError("disk I/O error")

    real_decision, real_register = notify.email_cap_decision, outbox._register_then_dispatch

    def unreadable_from_here(*a, **k):  # the database stops answering reads for the whole cap gate …
        state["on"] = True
        return real_decision(*a, **k)

    def readable_again(*a, **k):  # … and answers again for the send and its outcome
        state["on"] = False
        return real_register(*a, **k)

    monkeypatch.setattr(notify, "email_cap_decision", unreadable_from_here)
    monkeypatch.setattr(outbox, "_register_then_dispatch", readable_again)
    event.listen(engine, "before_cursor_execute", refuse_reads)
    try:
        report = drain_once(session, now=NOW)
    finally:
        state["on"] = False
        event.remove(engine, "before_cursor_execute", refuse_reads)
    assert {"outbox", "incidents"} <= set(refused)  # both reads really failed, at the engine
    status = _get(session, row.id).status
    if sent:
        assert (report.sent, status, len(smtp_on.sent)) == (1, SENT, 1)
        (note,) = [n for n in _notes(session, inc) if "could not be checked" in n]
        assert "P1 notice was SENT without counting it against the cap" in note
    else:
        assert (report.sent, report.retried, status, smtp_on.sent) == (0, 1, PENDING, [])


def test_the_queued_priority_is_read_from_the_subject_the_producers_write():
    assert notify.queued_priority({"subject": "[P1] INC000123 | Embakasi East HUB | Nairobi East"}) == "P1"
    assert notify.queued_priority({"subject": "  [p2] INC000123 | x"}) == "P2"
    assert notify.queued_priority({"subject": "Shift handover 2026-09-21_DAY"}) is None
    assert notify.queued_priority({"subject": "[P5] INC000123 | x"}) is None
    assert notify.queued_priority({}) is None


def test_a_failing_note_can_never_turn_a_deferral_into_a_send(tmp_db, smtp_on, transfers, clean_hub, monkeypatch):
    """Review E05, the reviewer's repro: at the cap, a P3 whose held-note write fails was SENT past
    the cap. The note is now written with the outcome, so a failure there leaves the row claimed
    for the lease to reclaim — and nothing is transmitted."""
    _settings, session = tmp_db
    monkeypatch.setenv("EMAIL_DAILY_CAP", "10")
    inc = _incident(session, priority="P3")
    _seed_sent(session, 10, at=NOW - timedelta(hours=1))
    row = _queue(session, inc, "EMAIL:e05:1")

    def locked(*_a, **_k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(notify, "record_email_cap_note", locked)
    report = drain_once(session, now=NOW)
    # The decision stood (deferred is counted when it is taken); only its recording failed.
    assert (report.sent, report.deferred, report.errors) == (0, 1, 1)
    assert smtp_on.sent == [] and transfers == []
    assert _get(session, row.id).status != SENT


@pytest.mark.parametrize("drainers", [2, 4])
def test_concurrent_drainers_overshoot_by_at_most_one_message_each(
    tmp_db, smtp_on, transfers, clean_hub, monkeypatch, drainers
):
    """Review E06 — documented, not reserved (see ``notify.email_budget``). N drainers holding N
    different rows at cap−1 all read the same count and all send: the window ends at
    cap + (drainers − 1) × requested. 4 is the round-3 replay (3 over): every concurrent
    synchronous ingest request is a drainer, so the count of drainers is not a fixed 3."""
    from noc_agents.db.models import get_session

    _settings, session = tmp_db
    monkeypatch.setenv("EMAIL_DAILY_CAP", "10")
    inc = _incident(session)
    _seed_sent(session, 9, at=NOW - timedelta(hours=1))
    rows = [_get(session, _queue(session, inc, f"EMAIL:race:{i}").id) for i in range(drainers)]
    others = [get_session() for _ in range(drainers - 1)]
    try:
        decisions = [notify.email_cap_decision(s, r, now=NOW) for s, r in zip([session, *others], rows)]
    finally:
        for other in others:
            other.close()
    assert [d.action for d in decisions] == [notify.CAP_SEND] * drainers  # none sees the others
    window_after_all = decisions[0].budget.sent + sum(d.budget.requested for d in decisions)
    assert window_after_all == 10 + (drainers - 1)  # the documented formula
    # One drainer is exact: it re-reads the count between rows, so every row after the first is held.
    report = drain_once(session, now=NOW)
    assert (report.sent, report.deferred) == (1, drainers - 1) and notify.email_budget(session, now=NOW).sent == 10


# --- nothing observable changes when the cap is not in play -------------------------------------


def test_the_cap_is_never_consulted_when_mail_is_not_configured(tmp_db, transfers, clean_hub, monkeypatch):
    """EMAIL_ENABLED=false — the demo default and every other test in the suite: the gate returns
    before the database, so no existing row, event or note can move because of it."""
    _settings, session = tmp_db
    monkeypatch.setattr(notify, "email_cap_decision", lambda *_a, **_k: pytest.fail("cap consulted in mock mode"))
    monkeypatch.setenv("EMAIL_DAILY_CAP", "1")
    inc = _incident(session)
    _seed_sent(session, 1000, at=NOW - timedelta(hours=1))
    row = _queue(session, inc, "EMAIL:mock:1")
    report = drain_once(session, now=NOW)
    assert report.sent == 1 and report.deferred == 0
    assert report.outcomes[row.id].delivery == {"mode": "mock", "to": [], "detail": MOCK_EMPTY}


def _observe(session, inc: IncidentRow, row_id: str) -> dict:
    """Everything a drain makes observable about one row, with the per-incident ids normalised."""
    row = _get(session, row_id)
    events = [
        (e["type"], {k: v for k, v in (e.get("payload") or {}).items() if k != "incident_number"})
        for e in hub._history
        if e.get("incident_id") == inc.id
    ]
    return {
        "row": (row.status, row.provider, row.last_error, row.attempts, row.next_attempt_at, row.sent_at),
        "notes": [n.replace(inc.incident_number, "INC") for n in _notes(session, inc)],
        "events": events,
    }


def test_below_the_cap_a_configured_send_is_identical_to_one_with_no_gate_at_all(
    tmp_db, smtp_on, transfers, clean_hub, monkeypatch
):
    """Same row, same note, same event, same SMTP message, with the gate live vs. removed."""
    _settings, session = tmp_db
    _seed_sent(session, 5, at=NOW - timedelta(hours=1))

    gated = _incident(session, number="INC000911")
    gated_row = _queue(session, gated, "EMAIL:parity:gated")
    gated_report = drain_once(session, now=NOW)

    monkeypatch.setattr(outbox, "_email_cap_gate", lambda *_a, **_k: (None, None))
    bare = _incident(session, number="INC000912")
    bare_row = _queue(session, bare, "EMAIL:parity:bare")
    bare_report = drain_once(session, now=NOW)

    assert _observe(session, gated, gated_row.id) == _observe(session, bare, bare_row.id)
    assert str(gated_report) == str(bare_report) and gated_report.deferred == bare_report.deferred == 0
    first, second = smtp_on.sent
    assert (first["To"], first["Bcc"], first.get_content()) == (second["To"], second["Bcc"], second.get_content())
    assert len(transfers) == 2
