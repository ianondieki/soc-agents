"""``recipients_ref`` resolution, and the refusal that replaces the demo-mailbox fallback.

Phase 4 shipped the regulatory lane with ``recipients_ref="regulatory.recipients.CA"`` on the
approved notice, and a ``transmit_email`` that ignored it: every message — the CA notification
included — was resolved by the SMTP adapter to ``DEMO_EMAIL_TO``. What is proved here:

* the demo path is untouched — no ref, an empty ref and ``DEMO_EMAIL_TO`` all still reach
  ``send_email`` with no ``to`` argument, so the adapter resolves the demo mailbox exactly as
  it always has (this is the regression that matters: every incident, handover and HITL
  release email goes this way);
* a named ref resolves out of the operator profile and those addresses reach the adapter;
* a ref that is unknown, declared-but-empty, or configured with something that is not an
  e-mail address REFUSES — the dispatch dies (DEAD, no retry), the reason names the ref, and
  the SMTP adapter is never called;
* the ref the regulatory lane writes is the ref these profiles declare, so the two halves
  cannot drift apart silently;
* as shipped, both profiles declare that ref EMPTY, so an approved CA notice today refuses
  rather than going to the demo inbox.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from noc_agents.config import get_settings
from noc_agents.db.models import OutboxRow
from noc_agents.orchestrator.outbox import DEAD, SENT, drain_once, enqueue
from noc_agents.realtime.hub import hub
from noc_agents.services import notify
from noc_agents.services.notify import DEMO_RECIPIENTS_REF, UnresolvedRecipients, resolve_recipients
from noc_agents.services.regulatory import NOTICE_RECIPIENTS_REF

CA_REF = "regulatory.recipients.CA"
PROFILES = ("safaricom", "airtel")


@pytest.fixture()
def clean_hub():
    hub._history.clear()
    yield hub
    hub._history.clear()


def _settings_with(register: dict[str, list[str]], profile: str = "safaricom"):
    """A deep copy of a real profile with the recipient register replaced (never the cache)."""
    settings = get_settings(profile).model_copy(deep=True)
    settings.operator.notification_recipients = register
    return settings


def _spy_send(monkeypatch) -> list[dict]:
    """Record what ``transmit_email`` hands the SMTP adapter, and answer like the mock path."""
    from noc_agents.adapters.email_smtp import EmailResult

    calls: list[dict] = []

    def fake(*, subject, body, to=None, html=False):
        calls.append({"subject": subject, "body": body, "to": to})
        return EmailResult(ok=True, mode="mock", detail="test", to=list(to or []))

    monkeypatch.setattr(notify, "send_email", fake)
    return calls


def _payload(ref: str | None, *, operator_id: str = "safaricom") -> dict:
    payload = {
        "operator_id": operator_id,
        "incident_number": "INC000001",
        "audience": "CA",
        "subject": "[P1] INC000001 | regulatory notification",
        "body": "body",
        "broadcast_ids": [],
    }
    if ref is not None:
        payload["recipients_ref"] = ref
    return payload


# --- the demo path must not move --------------------------------------------------------------


@pytest.mark.parametrize("ref", [None, "", "   ", DEMO_RECIPIENTS_REF])
def test_the_demo_path_still_calls_the_adapter_with_no_recipients(monkeypatch, ref):
    """No ref / the demo ref => ``send_email(subject=…, body=…)``, byte-for-byte as before.

    The adapter's own ``demo_recipients()`` then decides, which is what every incident,
    handover and HITL-release email has always relied on.
    """
    calls = _spy_send(monkeypatch)
    result = notify.transmit_email(_payload(ref))
    assert result.ok is True
    assert calls == [{"subject": "[P1] INC000001 | regulatory notification", "body": "body", "to": None}]


def test_the_demo_ref_is_never_looked_up_in_the_profile(monkeypatch):
    """``DEMO_EMAIL_TO`` means "ask the environment", so an operator profile cannot redefine it."""
    monkeypatch.setattr(notify, "get_settings", lambda *_a, **_k: pytest.fail("profile read for the demo ref"))
    calls = _spy_send(monkeypatch)
    notify.transmit_email(_payload(DEMO_RECIPIENTS_REF))
    assert calls[0]["to"] is None


# --- resolution ---------------------------------------------------------------------------------


def test_a_configured_ref_resolves_to_its_addresses():
    settings = _settings_with({CA_REF: ["ca.notifications@example.com", " second@example.com "]})
    assert resolve_recipients(CA_REF, operator_id=None, settings=settings) == [
        "ca.notifications@example.com",
        "second@example.com",
    ]


def test_a_resolved_ref_reaches_the_adapter_as_the_recipient_list(monkeypatch):
    monkeypatch.setattr(notify, "get_settings", lambda *_a, **_k: _settings_with({CA_REF: ["ca@example.com"]}))
    calls = _spy_send(monkeypatch)
    result = notify.transmit_email(_payload(CA_REF))
    assert calls[0]["to"] == ["ca@example.com"]
    assert result.to == ["ca@example.com"]


# --- and every way of not resolving fails closed -------------------------------------------------


def test_an_unknown_ref_refuses_instead_of_falling_back_to_the_demo_mailbox(monkeypatch):
    monkeypatch.setattr(notify, "get_settings", lambda *_a, **_k: _settings_with({}))
    calls = _spy_send(monkeypatch)
    with pytest.raises(UnresolvedRecipients) as exc:
        notify.transmit_email(_payload("audiences.REGULATOR"))
    assert "audiences.REGULATOR" in str(exc.value)
    assert DEMO_RECIPIENTS_REF in str(exc.value)  # the refusal says what it refused to do
    assert calls == []  # nothing was handed to SMTP


def test_a_declared_but_empty_ref_refuses():
    settings = _settings_with({CA_REF: []})
    with pytest.raises(UnresolvedRecipients, match="declared but empty"):
        resolve_recipients(CA_REF, operator_id=None, settings=settings)


@pytest.mark.parametrize("value", ["RNIO-NBI-W", "+254711000001", "ops at example.com", "ops@localhost"])
def test_a_ref_that_resolves_to_something_that_is_not_an_address_refuses(value):
    """The ref vocabulary also contains role tokens and phone refs; none of them are mailboxes."""
    settings = _settings_with({CA_REF: [value]})
    with pytest.raises(UnresolvedRecipients, match="not"):
        resolve_recipients(CA_REF, operator_id=None, settings=settings)


def test_a_refusal_never_quotes_the_offending_value():
    """``last_error`` is stored and exportable: a finding that repeats what it found is a
    second copy of it (§9.5). The ref and a count are enough to fix the YAML."""
    settings = _settings_with({CA_REF: ["RNIO-NBI-W"]})
    with pytest.raises(UnresolvedRecipients) as exc:
        resolve_recipients(CA_REF, operator_id=None, settings=settings)
    assert "RNIO-NBI-W" not in str(exc.value)
    assert "1 of 1" in str(exc.value)


def test_a_ref_without_an_operator_id_refuses():
    with pytest.raises(UnresolvedRecipients, match="operator_id"):
        resolve_recipients(CA_REF, operator_id=None)


def test_an_unloadable_operator_profile_refuses():
    with pytest.raises(UnresolvedRecipients, match="did not load"):
        resolve_recipients(CA_REF, operator_id="no-such-operator")


def test_an_empty_ref_refuses_when_it_reaches_the_resolver():
    with pytest.raises(UnresolvedRecipients, match="empty recipients_ref"):
        resolve_recipients("   ", operator_id="safaricom")


# --- what the two lanes agree on, and what ships -------------------------------------------------


@pytest.mark.parametrize("profile", PROFILES)
def test_every_shipped_profile_declares_the_regulatory_ref(profile):
    """The lane that queues the notice and the profile that resolves it must use ONE string."""
    register = get_settings(profile).operator.notification_recipients
    assert NOTICE_RECIPIENTS_REF == CA_REF
    assert CA_REF in register, f"{profile}.yaml does not declare {CA_REF}"


@pytest.mark.parametrize("profile", PROFILES)
def test_the_shipped_regulatory_recipient_is_empty_so_a_notice_refuses(profile):
    """Empty ON PURPOSE: the CA notification mailbox comes from the operator's own licence
    correspondence. An invented address would read as configured, which is the worse half of
    the failure — an unsent notice is visible and fixable, a misdirected one is neither."""
    settings = get_settings(profile)
    assert settings.operator.notification_recipients[CA_REF] == []
    with pytest.raises(UnresolvedRecipients):
        resolve_recipients(CA_REF, operator_id=None, settings=settings)


# --- through the dispatcher ----------------------------------------------------------------------


def test_an_unresolvable_ref_makes_the_row_dead_and_sends_nothing(tmp_db, monkeypatch, clean_hub):
    """The chosen failure mode, end to end: DEAD (terminal, not retried), the ref in
    ``last_error``, an ``outbox.failed`` event for the wallboard — and no SMTP call."""
    _settings, session = tmp_db
    calls = _spy_send(monkeypatch)
    enqueue(
        session,
        kind="EMAIL",
        idempotency_key="EMAIL:regulatory:test-1",
        payload=_payload(CA_REF),
        operator_id="safaricom",
    )
    session.commit()

    report = drain_once(session)
    row = session.scalars(select(OutboxRow)).one()
    assert report.dead == 1 and report.sent == 0
    assert row.status == DEAD
    assert CA_REF in (row.last_error or "")
    assert row.attempts == 1  # terminal: a missing config entry does not appear during a backoff
    assert calls == []
    failures = [e for e in hub._history if e["type"] == "outbox.failed"]
    assert failures and failures[0]["payload"]["status"] == DEAD


def test_a_resolved_ref_dispatches_to_the_configured_recipient(tmp_db, monkeypatch, clean_hub):
    _settings, session = tmp_db
    monkeypatch.setattr(notify, "get_settings", lambda *_a, **_k: _settings_with({CA_REF: ["ca@example.com"]}))
    enqueue(
        session,
        kind="EMAIL",
        idempotency_key="EMAIL:regulatory:test-2",
        payload=_payload(CA_REF),
        operator_id="safaricom",
    )
    session.commit()

    report = drain_once(session)
    row = session.scalars(select(OutboxRow)).one()
    assert report.sent == 1 and row.status == SENT
    assert report.outcomes[row.id].delivery["to"] == ["ca@example.com"]


def test_the_demo_rows_still_drain_exactly_as_before(tmp_db, monkeypatch, clean_hub):
    """The no-regression case at drain level: a DEMO_EMAIL_TO row is SENT in mock mode with
    no recipients, which is what the whole existing suite asserts about the demo path."""
    _settings, session = tmp_db
    payload = json.loads(json.dumps(_payload(DEMO_RECIPIENTS_REF)))
    enqueue(session, kind="EMAIL", idempotency_key="EMAIL:demo:test-3", payload=payload, operator_id="safaricom")
    session.commit()

    report = drain_once(session)
    row = session.scalars(select(OutboxRow)).one()
    assert row.status == SENT and report.sent == 1
    assert report.outcomes[row.id].delivery == {
        "mode": "mock",
        "to": [],
        "detail": "No DEMO_EMAIL_TO / GMAIL_ADDRESS — email body stored only (mock)",
    }
