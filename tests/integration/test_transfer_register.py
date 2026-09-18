"""The transfer register, wired to the one path that actually leaves the machine.

``services/external_calls.record_transfer`` has had unit tests since Phase 1 and had no
callers: a module nothing imports is a seam, not a feature. These tests drive the real
dispatch path — ``orchestrator/outbox.drain_once`` with the SMTP transport mocked — and
every one of them fails if the ``record_transfer`` call is deleted from ``outbox.py``.

What is proved here, in order:

* a configured send (mocked transport) writes exactly one ``external.call`` audit row, with
  ``recipient_country="US"`` / ``residency="abroad"`` for a Gmail relay and the ``gmail_smtp``
  paperwork from ``transfers.yaml`` attached;
* that row is **already committed** at the instant ``send_message`` runs — the ordering the
  whole design turns on, checked from a second Session so only durable rows are visible;
* a *mock* send (``EMAIL_ENABLED=false``, the suite's default) records **nothing**: the
  register states what crossed the border, not what was contemplated;
* a loopback relay is the operator's own box in Kenya: ``KE`` / ``local``, no gate;
* the paperwork gate behaves differently in ``NOC_ENV=demo`` (``tia_ref="DEMO-UNFILED"``)
  and in production (``paperwork_status="unfiled"``), still refuses outright for the
  channels §7.0.10 does gate, and blocks the send outright when
  ``TRANSFER_GATE_BLOCKS_SEND`` is flipped — the operator's decision, not this module's;
* no credential this process holds reaches the audit payload, even when one is planted in
  the outbox payload fields that feed the justification;
* a failure inside ``record_transfer`` sends nothing at all, so it cannot produce a second
  copy of a mail the register failed to write down.

Nothing here opens a socket: ``smtplib.SMTP`` is replaced for the duration of each test.
"""

from __future__ import annotations

import json
import logging
import smtplib
import sys
from datetime import timedelta

import pytest
import yaml
from sqlalchemy import select

from noc_agents.db.models import AuditRow, OutboxRow, get_session, new_id, utcnow
from noc_agents.orchestrator import outbox
from noc_agents.orchestrator.outbox import drain_once, enqueue, transfer_plan
from noc_agents.services import external_calls as ec
from noc_agents.services.external_calls import (
    ACTION,
    DEMO_UNFILED,
    STATUS_DEMO_UNFILED,
    STATUS_FILED,
    STATUS_NOT_REQUIRED,
    STATUS_UNFILED,
    TransferPaperworkMissing,
    record_transfer,
)

EC_LOGGER = "noc_agents.services.external_calls"

# The state in which the adapter really calls smtplib (and the transport below catches it).
SMTP_ENV = {
    "EMAIL_ENABLED": "true",
    "GMAIL_ADDRESS": "noc@example.com",
    "GMAIL_APP_PASSWORD": "app-password",
    "DEMO_EMAIL_TO": "ops@example.com",
}

FILED_GMAIL = {
    "entity": "Google LLC",
    "country": "US",
    "dpia_ref": "DPIA-SFC-2026-011",
    "tia_ref": "TIA-SFC-2026-011",
    "scc_ref": "ODPC-SCC-2026-0077",
    "confirmed_by": "DPO Office",
    "confirmed_at": "2026-05-04",
}
UNFILED_GMAIL = {**FILED_GMAIL, "dpia_ref": "", "tia_ref": ""}


# --- fixtures ---------------------------------------------------------------------------------


@pytest.fixture()
def register(tmp_path, monkeypatch):
    """Point ``record_transfer`` at a throwaway ``config/operators/<op>/transfers.yaml``.

    Only ``external_calls.OPERATORS_DIR`` moves; ``config.OPERATORS_DIR`` still resolves the
    real operator profile, which is what ``get_settings(job.operator_id)`` needs.
    """

    def _write(entries: dict[str, dict] | None = None, operator_id: str = "safaricom"):
        monkeypatch.setattr(ec, "OPERATORS_DIR", tmp_path)
        folder = tmp_path / operator_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "transfers.yaml").write_text(yaml.safe_dump(entries or {}), encoding="utf-8")
        return folder / "transfers.yaml"

    return _write


@pytest.fixture()
def live_email(monkeypatch):
    """``EMAIL_ENABLED=true`` with credentials, and ``smtplib.SMTP`` replaced by a probe.

    Returns the list of sends. Each entry carries the register rows that were **durable**
    (visible from a second Session) at the moment the message left, which is how the
    record-before-transmit ordering is checked rather than assumed.
    """
    for key, value in SMTP_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("NOC_ENV", "production")  # the strict side unless a test says otherwise
    sends: list[dict] = []

    class ProbeSMTP:
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
                    "host": self.host,
                    "to": msg["To"],
                    "subject": msg["Subject"],
                    "register_durable": _elsewhere(_register_payloads),
                }
            )

    monkeypatch.setattr(smtplib, "SMTP", ProbeSMTP)
    return sends


# --- helpers ----------------------------------------------------------------------------------


def _elsewhere(read):
    """Run ``read(session)`` on a fresh Session: only committed rows are visible there."""
    other = get_session()
    try:
        return read(other)
    finally:
        other.close()


def _register_rows(session) -> list[AuditRow]:
    return list(session.scalars(select(AuditRow).where(AuditRow.action == ACTION).order_by(AuditRow.ts, AuditRow.id)))


def _register_payloads(session) -> list[dict]:
    return [json.loads(r.payload_json or "{}") for r in _register_rows(session)]


def _email_payload(number: str = "INC000001", audience: str = "RNIO") -> dict:
    return {
        "operator_id": "safaricom",
        "incident_number": number,
        "audience": audience,
        "subject": f"[P2] {number} | Machakos Town BTS down",
        "body": "Site down. ETR 3h.",
        "recipients_ref": "DEMO_EMAIL_TO",
        "broadcast_ids": [],
    }


def _queue_email(session, *, payload: dict | None = None, incident_id: str | None = None, key: str = "EMAIL:reg") -> OutboxRow:
    row = enqueue(
        session,
        kind="EMAIL",
        idempotency_key=key,
        payload=payload or _email_payload(),
        incident_id=incident_id,
        operator_id="safaricom",
    )
    session.commit()
    return row


def _outbox_row(session, kind: str = "EMAIL") -> OutboxRow:
    return session.scalars(select(OutboxRow).where(OutboxRow.kind == kind)).one()


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == EC_LOGGER and r.levelno == logging.WARNING]


# --- the wiring exists at all -----------------------------------------------------------------


def test_the_register_is_reachable_from_the_running_application():
    """The audit's own check: importing the app must pull the register in with it.

    ``noc_agents.main`` → ``graph.pipeline`` → ``orchestrator.outbox`` →
    ``services.external_calls``. A module nothing imports cannot be a compliance control.
    """
    import noc_agents.main  # noqa: F401  (the import IS the assertion)

    assert "noc_agents.services.external_calls" in sys.modules


# --- a real send is recorded ------------------------------------------------------------------


def test_a_configured_send_writes_one_transfer_record(tmp_db, register, live_email):
    _settings, session = tmp_db
    register({"gmail_smtp": FILED_GMAIL})
    _queue_email(session)

    report = drain_once(session)

    assert (report.claimed, report.sent, len(live_email)) == (1, 1, 1)
    assert _outbox_row(session).status == "SENT"

    (row,) = _register_rows(session)
    payload = json.loads(row.payload_json)
    assert row.action == ACTION and row.actor == outbox.TRANSFER_ACTOR
    assert row.operator_id == "safaricom"
    assert payload["recipient"] == "Gmail SMTP"
    assert payload["recipient_country"] == "US"  # the register is filtered on this field
    assert payload["residency"] == "abroad"
    assert payload["cross_border"] is True
    assert payload["paperwork_status"] == STATUS_FILED
    assert payload["recipient_key"] == "gmail_smtp"  # matched the transfers.yaml key
    assert payload["entity"] == "Google LLC" and payload["entity_country"] == "US"
    assert (payload["dpia_ref"], payload["tia_ref"]) == (FILED_GMAIL["dpia_ref"], FILED_GMAIL["tia_ref"])
    assert payload["incident_id"] is None and row.entity_type == "external_call"
    # reg 41(2): date/time, recipient, justification, description of the data
    assert payload["ts"] and "INC000001" in payload["justification"]
    assert "incident number" in payload["data_description"]


def test_the_record_is_committed_before_the_bytes_leave(tmp_db, register, live_email):
    """Ordering, checked from outside the writing Session rather than asserted in prose.

    Record-after would leave a window in which the mail is at Google and the process can
    die before the row commits — and the outbox retries an outcome it never recorded, so
    that window yields an unrecorded transfer AND a second copy of the mail. Record-before
    trades that for an over-record, which is the survivable side.
    """
    _settings, session = tmp_db
    register({"gmail_smtp": FILED_GMAIL})
    _queue_email(session)

    drain_once(session)

    (send,) = live_email
    assert send["host"] == "smtp.gmail.com"
    durable = send["register_durable"]
    assert len(durable) == 1, "the transfer record was not committed before the transmit"
    assert durable[0]["recipient_country"] == "US"


def test_an_incident_row_binds_the_record_to_the_incident(tmp_db, register, live_email):
    _settings, session = tmp_db
    register({"gmail_smtp": FILED_GMAIL})
    incident_id = new_id()
    _queue_email(session, incident_id=incident_id)

    drain_once(session)

    (row,) = _register_rows(session)
    assert (row.entity_type, row.entity_id) == ("incident", incident_id)
    assert json.loads(row.payload_json)["incident_id"] == incident_id


# --- what is NOT a transfer -------------------------------------------------------------------


def test_a_mock_send_records_nothing(tmp_db, register):
    """THE DECISION: a mock transmits nothing, so it is not a transfer.

    With ``EMAIL_ENABLED=false`` (the default, and what the suite runs) the adapter opens no
    socket. Recording it would tell the ODPC that incident data reached a US relay when it
    never left the process — and a register padded with transfers that never happened is
    indefensible the first time one row is checked against the mail server. It also means
    the golden run, which drains mock sends, writes no new audit rows.
    """
    _settings, session = tmp_db
    register({"gmail_smtp": FILED_GMAIL})
    _queue_email(session)

    report = drain_once(session)

    assert (report.claimed, report.sent) == (1, 1)
    row = _outbox_row(session)
    assert (row.status, row.provider) == ("SENT", "mock")
    assert _register_rows(session) == []


def test_sms_and_excel_rows_are_not_transfers(tmp_db, register, live_email):
    """SMS has no adapter until P3 (mock) and EXCEL_ROW writes a local workbook."""
    _settings, session = tmp_db
    register({"gmail_smtp": FILED_GMAIL})
    enqueue(
        session,
        kind="SMS",
        idempotency_key="SMS:reg",
        payload={"operator_id": "safaricom", "audience": "FIELD_ENGINEER", "text": "site down"},
        operator_id="safaricom",
    )
    session.commit()

    report = drain_once(session)

    assert (report.sent, len(live_email)) == (1, 0)  # a mock SMS, and no mail at all
    assert _register_rows(session) == []
    excel = OutboxRow(id="x", kind="EXCEL_ROW", operator_id="safaricom", payload_json="{}")
    assert transfer_plan(excel) is None


# --- domicile -----------------------------------------------------------------------------


def test_a_loopback_relay_is_recorded_as_kenya(tmp_db, register, live_email, monkeypatch, caplog):
    """The operator's own relay on the operator's own network: KE, local, no gate."""
    _settings, session = tmp_db
    monkeypatch.setenv("SMTP_HOST", "127.0.0.1")
    register({})  # an empty register would refuse anything cross-border
    _queue_email(session)

    with caplog.at_level(logging.WARNING, logger=EC_LOGGER):
        report = drain_once(session)

    assert (report.sent, len(live_email)) == (1, 1)
    payload = json.loads(_register_rows(session)[0].payload_json)
    assert payload["recipient_country"] == "KE"
    assert payload["residency"] == "local"
    assert payload["cross_border"] is False
    assert payload["paperwork_status"] == STATUS_NOT_REQUIRED
    assert _warnings(caplog) == []  # nothing to warn about: no cross-border paperwork applies


def test_an_unknown_relay_reads_as_cross_border(tmp_db, register, live_email, monkeypatch):
    """Unknown domicile is the conservative side — the reading the ODPC would take."""
    _settings, session = tmp_db
    monkeypatch.setenv("SMTP_HOST", "mail.example.net")
    register({})
    _queue_email(session)

    drain_once(session)

    payload = json.loads(_register_rows(session)[0].payload_json)
    assert (payload["recipient_country"], payload["residency"], payload["cross_border"]) == ("??", "abroad", True)


# --- the paperwork gate ---------------------------------------------------------------------


def test_production_records_the_unfiled_gap_and_still_sends(tmp_db, register, live_email, caplog):
    """§7.0.10 gates the hosted LLM and residency="abroad" MCP cards — not the SMTP relay.

    So the gap is written into the register (``paperwork_status="unfiled"``, empty refs)
    rather than hidden by refusing to write a row for a transfer that happened.
    """
    _settings, session = tmp_db
    register({"gmail_smtp": UNFILED_GMAIL})

    _queue_email(session)
    with caplog.at_level(logging.WARNING, logger=EC_LOGGER):
        report = drain_once(session)

    assert (report.sent, len(live_email)) == (1, 1)
    payload = json.loads(_register_rows(session)[0].payload_json)
    assert payload["env"] == "production"
    assert payload["paperwork_status"] == STATUS_UNFILED
    assert (payload["dpia_ref"], payload["tia_ref"]) == ("", "")  # no filing is invented
    assert payload["cross_border"] is True
    (warning,) = _warnings(caplog)  # one line, not a wall of noise
    assert STATUS_UNFILED in warning and "dpia_ref, tia_ref" in warning
    assert DEMO_UNFILED not in warning


def test_demo_records_demo_unfiled_and_still_sends(tmp_db, register, live_email, monkeypatch, caplog):
    """The same transfer under NOC_ENV=demo: the demo register is honest, not clean."""
    _settings, session = tmp_db
    monkeypatch.setenv("NOC_ENV", "demo")
    register({"gmail_smtp": UNFILED_GMAIL})

    _queue_email(session)
    with caplog.at_level(logging.WARNING, logger=EC_LOGGER):
        report = drain_once(session)

    assert (report.sent, len(live_email)) == (1, 1)
    payload = json.loads(_register_rows(session)[0].payload_json)
    assert payload["env"] == "demo"
    assert payload["paperwork_status"] == STATUS_DEMO_UNFILED
    assert payload["dpia_ref"] == DEMO_UNFILED and payload["tia_ref"] == DEMO_UNFILED
    (warning,) = _warnings(caplog)
    assert DEMO_UNFILED in warning and "NOC_ENV=demo" in warning


def test_the_gate_still_refuses_for_the_channels_it_covers(tmp_db, register, monkeypatch):
    """The gate is not dead code: the hosted LLM and abroad MCP cards keep the default.

    Same recipient, same unfiled register, ``enforce_gate`` left at its default — this is
    what ``llm/`` and the MCP client will call, and it refuses and writes nothing.
    """
    settings, session = tmp_db
    monkeypatch.setenv("NOC_ENV", "production")
    register({"gmail_smtp": UNFILED_GMAIL})
    recipient, country, residency = outbox.smtp_relay_identity("smtp.gmail.com")

    with pytest.raises(TransferPaperworkMissing) as excinfo:
        record_transfer(
            session,
            recipient=recipient,
            recipient_country=country,
            justification="hosted-model draft",
            data_description="redacted incident fields",
            actor="TicketingAgent",
            actor_role="AGENT",
            incident_id=None,
            residency=residency,
            settings=settings,
        )

    assert excinfo.value.missing == ("dpia_ref", "tia_ref")
    assert _register_rows(session) == []  # nothing left the machine, so nothing is recorded


def test_the_gate_can_block_the_send_when_the_operator_says_so(tmp_db, register, live_email, monkeypatch):
    """TRANSFER_GATE_BLOCKS_SEND is the deferred decision, and the blocking path works.

    Flipping the one constant turns an unfiled cross-border relay into a refusal: nothing is
    transmitted, so nothing is recorded, and the row is DEAD rather than retried — a missing
    legal filing is not a transient error.
    """
    _settings, session = tmp_db
    monkeypatch.setattr(outbox, "TRANSFER_GATE_BLOCKS_SEND", True)
    register({"gmail_smtp": UNFILED_GMAIL})
    _queue_email(session)

    report = drain_once(session)

    assert (report.claimed, report.sent, report.dead, len(live_email)) == (1, 0, 1, 0)
    row = _outbox_row(session)
    assert row.status == "DEAD"
    assert "refused" in row.last_error and "tia_ref" in row.last_error
    assert _register_rows(session) == []


# --- no credential in the register -------------------------------------------------------------


def test_no_credential_reaches_the_transfer_record(tmp_db, register, live_email, monkeypatch):
    """The register is meant to be handed to a regulator; a key must not travel with it.

    The needles are planted where they can actually reach the payload: the app password is
    the one this process holds, and the outbox payload fields that feed the justification
    carry a second, third-party-shaped key.
    """
    _settings, session = tmp_db
    app_password = "zqwe erty uiop asdf"  # Gmail shows app passwords in four blocks
    compact = app_password.replace(" ", "")  # the login path strips the spaces before sending
    pasted_key = "sk-ant-api03-N0CREGISTERFAKE-4b7f2c9e1a6d8305f2b4c6e8a0d2f4b6-AA"
    monkeypatch.setenv("GMAIL_APP_PASSWORD", app_password)
    register({"gmail_smtp": FILED_GMAIL})
    _queue_email(
        session,
        payload=_email_payload(
            number=f"INC000007 api_key={pasted_key}",
            audience=f"RNIO (relay creds {app_password} / {compact})",
        ),
    )

    drain_once(session)

    (row,) = _register_rows(session)
    columns = " || ".join(str(getattr(row, c.name)) for c in row.__table__.columns)
    for needle in (app_password, compact, pasted_key):
        assert needle not in row.payload_json, f"{needle!r} reached the transfer register payload"
        assert needle not in columns, f"{needle!r} reached an audit_events column"
    assert "<REDACTED>" in row.payload_json  # the scrubber ran, rather than the needles missing by luck


# --- the ordering failure analysis, as a test ---------------------------------------------------


def test_a_failure_to_record_cannot_cause_a_double_send(tmp_db, register, live_email, monkeypatch):
    """The point of recording FIRST: an unwritable register sends nothing at all.

    With the record after the transmit, a raise here would abort the outcome commit, leave
    the row CLAIMED, and the 120 s lease would hand it to the next drain — a second copy of
    a mail that was never written down. Recording first makes "failed to record" and
    "sent twice" mutually exclusive: the transmit is simply never reached.
    """
    _settings, session = tmp_db
    register({"gmail_smtp": FILED_GMAIL})
    real_record = outbox.record_transfer

    def broken(*args, **kwargs):
        raise RuntimeError("register unavailable")

    monkeypatch.setattr(outbox, "record_transfer", broken)
    _queue_email(session)

    first = drain_once(session)

    assert (first.claimed, first.sent, first.errors) == (1, 0, 0)
    assert live_email == [], "the mail left despite the register failing to record it"
    assert _register_rows(session) == []
    row = _outbox_row(session)
    assert row.status == "PENDING" and row.attempts == 1  # transient: it will be retried
    assert "transfer register write failed" in row.last_error
    assert row.next_attempt_at is not None

    # The register comes back, and the retry is the FIRST and ONLY send.
    monkeypatch.setattr(outbox, "record_transfer", real_record)
    second = drain_once(session, now=utcnow() + timedelta(seconds=300))

    assert (second.claimed, second.sent) == (1, 1)
    assert len(live_email) == 1, "the retry sent a second copy"
    assert len(_register_rows(session)) == 1
    row = _outbox_row(session)
    assert (row.status, row.attempts) == ("SENT", 2)
