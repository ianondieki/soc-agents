"""What the row says after the dispatcher has had its turn — spec §7.6.1, §9.2, metric M10.

THE BUG THIS FILE WAS WRITTEN FOR. ``release_notice`` used to write ``status=SENT`` and a
``sent_at`` timestamp at the moment the notice was *enqueued*. The outbox dispatches later,
and that dispatch can fail terminally — most importantly it fails terminally **by default**,
because ``regulatory.recipients.CA`` ships as an empty list in every operator profile and
``services/notify.resolve_recipients`` refuses the whole send rather than falling back to the
demo mailbox. So the shipped, unconfigured system produced rows reading "notified the
Communications Authority at 14:02" about notices that never left the building.

That is not an ordinary status bug. ``regulatory_notifications`` is the evidence for M10 and
for the Condition 9.2 24-hour obligation: "did we notify the Authority in time?" is answered
from this table. A false *yes* is worse than a visible failure, because nobody re-checks a
discharged obligation.

The rule the whole file pins, in one line: **``sent_at`` means "this left the building".**

``test_a_terminally_failed_dispatch_does_not_leave_the_row_claiming_it_was_sent`` is the test
that matters; it drives the real failure with the real shipped configuration and no network.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest

from noc_agents.config import get_settings
from noc_agents.db.models import HitlTaskRow, IncidentRow, OutboxRow, utcnow
from noc_agents.db.models_regulatory import PENDING_APPROVAL, SENT, RegulatoryNotificationRow
from noc_agents.orchestrator import outbox
from noc_agents.services import notify
from noc_agents.services.notify import UnresolvedRecipients, resolve_recipients
from noc_agents.services.regulatory import (
    DISPATCH_KEY,
    QUEUED,
    SEND_FAILED,
    NoticeStateError,
    open_notification,
    record_dispatch_outcome,
    release_notice,
    request_approval,
)

# The same reference instant test_regulatory_clock.py uses: 12:00 EAT stored as naive UTC.
FAILURE = datetime(2026, 9, 16, 9, 0, 0)
APPROVER = "Grace Wanjiru"  # a named human, never an agent: or policy: principal
ON_TIME = FAILURE + timedelta(hours=23)  # comfortably inside the 24-hour CA window


@pytest.fixture()
def on(monkeypatch):
    """``REGULATORY_ENABLED=true``. The lane writes nothing at all without it."""
    monkeypatch.setenv("REGULATORY_ENABLED", "true")


@dataclass
class _FakeEmailResult:
    """What ``adapters.email_smtp.send_email`` returns, without a socket (hard rule: no
    network in tests). Patched over ``notify.transmit_email``, which is the only SMTP call."""

    ok: bool
    mode: str
    detail: str
    to: list[str]


def _transmits(monkeypatch, *, ok: bool = True, detail: str = "delivered") -> None:
    """Make the one SMTP call succeed (or fail) deterministically, with nothing on a wire."""
    monkeypatch.setattr(
        notify,
        "transmit_email",
        lambda payload: _FakeEmailResult(ok=ok, mode="smtp", detail=detail, to=["ca@example.ke"]),
    )


def _incident(session, *, number: str = "INC000901", operator_id: str = "safaricom", **overrides) -> IncidentRow:
    values = dict(
        operator_id=operator_id,
        incident_number=number,
        status="IN_PROGRESS",
        priority="P1",
        users_affected=450000,
        site_id="SFC-MTK-HUB-THK",
        site_name="Thika Hub",
        site_type="HUB",
        region_code="MTK",
        county="Kiambu",
        correlation_fingerprint="fp-reg-dispatch",
        failure_time=FAILURE,
    )
    values.update(overrides)
    inc = IncidentRow(**values)
    session.add(inc)
    session.flush()
    return inc


def _released(session, *, now: datetime = ON_TIME, **release_kwargs):
    """An incident with an approved notice already released to the outbox, committed.

    Committed on purpose: ``drain_once`` is documented as something you call AFTER the
    producer's commit, and the whole bug lives in the gap between those two moments.
    """
    cfg = get_settings().operator
    inc = _incident(session)
    notice = open_notification(session, inc, cfg)
    task = request_approval(session, notice, inc, cfg)
    task.status = "APPROVED"
    task.resolved_by = APPROVER
    task.resolved_at = utcnow()
    session.flush()
    row = release_notice(session, notice, inc, actor="Duty Manager", now=now, **release_kwargs)
    session.commit()
    return inc, notice, row


# =======================================================================================
# The default state: the shipped profile has no CA address, so the dispatch really does die
# =======================================================================================


def test_the_shipped_profile_declares_no_ca_address_so_this_is_the_default_path(tmp_db, on):
    """Not an edge case. ``config/operators/safaricom.yaml`` declares
    ``regulatory.recipients.CA: []`` deliberately — the operator fills in the address from
    their own licence correspondence — and ``resolve_recipients`` refuses rather than falling
    back to the demo mailbox. Every assertion below rests on this being today's behaviour."""
    with pytest.raises(UnresolvedRecipients, match="empty"):
        resolve_recipients("regulatory.recipients.CA", operator_id="safaricom")


def test_a_terminally_failed_dispatch_does_not_leave_the_row_claiming_it_was_sent(tmp_db, on):
    """THE test. Approve a notice, enqueue it, let the dispatch fail the way it fails today.

    Before the fix this failed on the third assertion with "nothing was transmitted but the
    row says SENT": the outbox row was correctly DEAD and the notice still read SENT with a
    ``sent_at``. That pair is the false assurance — an auditor reading the evidence table
    would have been told the Authority was notified.
    """
    _settings, session = tmp_db
    _inc, notice, row = _released(session)

    report = outbox.drain_once(session)

    assert (report.claimed, report.dead, report.sent) == (1, 1, 0)
    assert session.get(OutboxRow, row.id).status == outbox.DEAD
    session.refresh(notice)
    assert notice.status == SEND_FAILED, "a dispatch that transmitted nothing must not read as a send"
    assert notice.sent_at is None, "sent_at means 'this left the building'"


def test_the_reason_the_dispatch_died_is_on_the_row(tmp_db, on):
    """"It failed" is not enough for a statutory obligation: the row has to say WHY, or the
    operator cannot tell a missing config line from a bounced relay."""
    _settings, session = tmp_db
    _inc, notice, row = _released(session)

    outbox.drain_once(session)
    session.refresh(notice)

    record = notice.significance[DISPATCH_KEY]
    assert record["outbox_status"] == outbox.DEAD
    assert record["outbox_id"] == row.id
    assert "regulatory.recipients.CA" in record["error"]
    assert "empty" in record["error"]


def test_the_failure_is_written_where_a_human_will_trip_over_it(tmp_db, on):
    """A status nobody reads is not visibility. The incident gets a work note and the audit
    trail a regulator reads back gets ``regulatory.send_failed`` — never ``regulatory.sent``."""
    from noc_agents.db.models import AuditRow, WorkNoteRow

    _settings, session = tmp_db
    inc, _notice, _row = _released(session)

    outbox.drain_once(session)

    notes = [n.body for n in session.query(WorkNoteRow).filter(WorkNoteRow.incident_id == inc.id)]
    assert any("was NOT transmitted" in n and "obligation is NOT discharged" in n for n in notes)
    actions = {a.action for a in session.query(AuditRow).all()}
    assert "regulatory.send_failed" in actions
    assert "regulatory.sent" not in actions, "nothing was sent; the audit trail must not say it was"
    assert "regulatory.released" in actions, "the enqueue is still recorded, under its own name"


# =======================================================================================
# The other two terminal failures §7.6 has to survive
# =======================================================================================


def test_a_dispatch_refused_for_want_of_approval_reads_as_a_failure(tmp_db, on):
    """The outbox's independent M10 gate: clear the approval off the row and the dispatcher
    refuses it (REJECTED_UNAPPROVED). The notice must follow the refusal, not the enqueue."""
    _settings, session = tmp_db
    _inc, notice, row = _released(session)
    session.get(OutboxRow, row.id).approved_at = None
    session.commit()

    outbox.drain_once(session)

    assert session.get(OutboxRow, row.id).status == outbox.REJECTED_UNAPPROVED
    session.refresh(notice)
    assert (notice.status, notice.sent_at) == (SEND_FAILED, None)
    assert notice.significance[DISPATCH_KEY]["outbox_status"] == outbox.REJECTED_UNAPPROVED


def test_an_smtp_5xx_reads_as_a_failure_not_a_send(tmp_db, on, monkeypatch):
    """A permanent SMTP reply is DEAD at once (no retry will fix a 550), and the notice has
    to say so. The adapter swallows its own exception into ``EmailResult(ok=False)``."""
    _settings, session = tmp_db
    _inc, notice, row = _released(session)
    _transmits(monkeypatch, ok=False, detail="SMTP error (550, b'5.1.1 No such recipient')")

    outbox.drain_once(session)

    assert session.get(OutboxRow, row.id).status == outbox.DEAD
    session.refresh(notice)
    assert (notice.status, notice.sent_at) == (SEND_FAILED, None)
    assert "550" in notice.significance[DISPATCH_KEY]["error"]


def test_a_transient_failure_with_attempts_left_leaves_the_notice_queued(tmp_db, on, monkeypatch):
    """Only a TERMINAL outcome touches the notice. A row going back to PENDING for another
    attempt is still in flight, and calling it SEND_FAILED would be the mirror image of the
    original bug — a visible failure that is not one yet."""
    _settings, session = tmp_db
    _inc, notice, row = _released(session)

    def _boom(payload):
        raise OSError("connection reset by peer")  # transient by outbox.is_transient

    monkeypatch.setattr(notify, "transmit_email", _boom)
    report = outbox.drain_once(session)

    assert (report.retried, report.dead, report.sent) == (1, 0, 0)
    assert session.get(OutboxRow, row.id).status == outbox.PENDING
    session.refresh(notice)
    assert notice.status == QUEUED and notice.sent_at is None
    assert DISPATCH_KEY not in notice.significance, "no terminal outcome yet, so nothing to record"


# =======================================================================================
# The happy path: SENT is written once, by the thing that actually transmitted
# =======================================================================================


def test_a_real_transmission_is_what_writes_sent_and_sent_at(tmp_db, on, monkeypatch):
    _settings, session = tmp_db
    _inc, notice, row = _released(session)
    _transmits(monkeypatch)
    drained_at = ON_TIME + timedelta(minutes=4)

    report = outbox.drain_once(session, now=drained_at)

    assert (report.sent, report.dead) == (1, 0)
    assert session.get(OutboxRow, row.id).status == outbox.SENT
    session.refresh(notice)
    assert notice.status == SENT
    # The transmit instant, not the enqueue instant: they are four minutes apart here on
    # purpose, and it is the later one that answers "when was the Authority notified?".
    assert notice.sent_at == drained_at
    assert notice.significance[DISPATCH_KEY]["outbox_status"] == outbox.SENT


# =======================================================================================
# §9.2 / DPA 2019 s.43 — lateness is a fact about the TRANSMISSION
# =======================================================================================


def test_a_notice_queued_in_time_but_transmitted_late_says_so(tmp_db, on, monkeypatch):
    """The case the old code could not express at all. ``release_notice`` refuses a late
    *enqueue* without a ``reason_for_delay``, so an on-time enqueue is never asked for one —
    and then the drain runs after ``due_at``. The notification IS late under Condition 9.2 and
    DPA s.43, and the row has to say so rather than inherit the enqueue's clean bill."""
    _settings, session = tmp_db
    _inc, notice, _row = _released(session, now=ON_TIME)
    assert notice.significance["release"]["late"] is False  # queued inside the window
    _transmits(monkeypatch)
    after_deadline = notice.due_at + timedelta(minutes=20)

    outbox.drain_once(session, now=after_deadline)
    session.refresh(notice)

    assert notice.status == SENT and notice.sent_at > notice.due_at
    assert notice.significance[DISPATCH_KEY]["late"] is True
    # And it is honest about the gap it cannot fill: no reason_for_delay was ever collected,
    # because at release there was nothing to explain. Inventing one here would fabricate the
    # disclosure the statute asks a named human for.
    assert notice.significance[DISPATCH_KEY]["reason_for_delay_recorded"] is False
    assert "reason_for_delay" not in notice.significance


def test_a_late_release_carries_its_reason_all_the_way_to_the_transmission(tmp_db, on, monkeypatch):
    _settings, session = tmp_db
    late = FAILURE + timedelta(hours=27)
    _inc, notice, _row = _released(
        session, now=late, reason_for_delay="site inaccessible during curfew; failure time confirmed on restoration"
    )
    _transmits(monkeypatch)

    outbox.drain_once(session, now=late + timedelta(minutes=2))
    session.refresh(notice)

    assert notice.status == SENT
    assert notice.significance[DISPATCH_KEY]["late"] is True
    assert notice.significance[DISPATCH_KEY]["reason_for_delay_recorded"] is True
    assert "curfew" in notice.significance["reason_for_delay"]


def test_a_failure_after_the_deadline_records_the_deadline_as_blown(tmp_db, on):
    """Late is late whether or not anything was transmitted: a dead dispatch past ``due_at``
    means the obligation is now overdue AND undischarged, and both halves are on the row."""
    _settings, session = tmp_db
    _inc, notice, _row = _released(session)

    outbox.drain_once(session, now=notice.due_at + timedelta(hours=1))
    session.refresh(notice)

    assert notice.status == SEND_FAILED and notice.sent_at is None
    assert notice.significance[DISPATCH_KEY]["late"] is True


# =======================================================================================
# The guards that existed before, still guarding
# =======================================================================================


def test_a_queued_notice_cannot_be_released_again(tmp_db, on):
    """Two written notifications to a regulator for one incident is its own kind of mess.
    The guard used to key on SENT; it keys on QUEUED now, and refuses just as hard."""
    _settings, session = tmp_db
    inc, notice, _row = _released(session)

    with pytest.raises(NoticeStateError, match="already been released to the outbox"):
        release_notice(session, notice, inc, actor="Duty Manager", now=ON_TIME)
    assert session.query(OutboxRow).count() == 1


def test_a_transmitted_notice_cannot_be_released_again(tmp_db, on, monkeypatch):
    _settings, session = tmp_db
    inc, notice, _row = _released(session)
    _transmits(monkeypatch)
    outbox.drain_once(session)
    session.refresh(notice)
    assert notice.status == SENT

    with pytest.raises(NoticeStateError, match="already been sent"):
        release_notice(session, notice, inc, actor="Duty Manager", now=ON_TIME)
    assert session.query(OutboxRow).count() == 1


def test_a_queued_notice_cannot_have_a_second_approval_card_raised(tmp_db, on):
    """A second card invites a second release of a notice whose first copy may be transmitted
    between the two clicks."""
    _settings, session = tmp_db
    inc, notice, _row = _released(session)

    with pytest.raises(NoticeStateError, match="already QUEUED"):
        request_approval(session, notice, inc, get_settings().operator)


# =======================================================================================
# Recovery: the address gets filled in, and the obligation is still dischargeable
# =======================================================================================


def test_a_failed_notice_can_be_released_again_once_the_address_is_fixed(tmp_db, on, monkeypatch):
    """The default failure is a missing config line. If SEND_FAILED were terminal for the
    notice too, filling that line in would leave a live statutory obligation permanently
    undischargeable through the system that raised it.

    The second release is safe precisely because the first is recorded as having transmitted
    NOTHING: a second outbox row can only exist after a terminal non-SENT outcome, so it can
    never mean two notices at the regulator. It gets its own idempotency key for that reason,
    and the failed attempt is kept rather than overwritten.
    """
    _settings, session = tmp_db
    inc, notice, first = _released(session)
    outbox.drain_once(session)
    session.refresh(notice)
    assert notice.status == SEND_FAILED

    _transmits(monkeypatch)  # stands in for the operator filling in regulatory.recipients.CA
    second = release_notice(session, notice, inc, actor="Duty Manager", now=ON_TIME)
    session.commit()
    assert second.id != first.id and second.idempotency_key != first.idempotency_key
    assert notice.status == QUEUED and notice.sent_at is None

    outbox.drain_once(session, now=ON_TIME)
    session.refresh(notice)

    assert notice.status == SENT and notice.sent_at == ON_TIME
    # The bounce is still on the record: "we tried, it failed, then we tried again".
    history = notice.significance["dispatch_history"]
    assert [h["outbox_status"] for h in history] == [outbox.DEAD]
    assert notice.significance[DISPATCH_KEY]["attempt"] == 2


# =======================================================================================
# Blast radius: the hook must not touch anything that is not a regulatory notice
# =======================================================================================


def test_an_ordinary_email_row_is_untouched_by_the_hook(tmp_db, on):
    """``outbox.py`` is on the golden path. A row whose payload names no producer row goes
    through ``_producer_outcome`` unchanged and never imports the regulatory lane at all."""
    _settings, session = tmp_db
    inc = _incident(session, number="INC000902")
    row = outbox.enqueue(
        session,
        kind=outbox.EMAIL,
        idempotency_key=f"EMAIL:{inc.id}:NOC",
        payload={
            "operator_id": inc.operator_id,
            "incident_number": inc.incident_number,
            "audience": "NOC",
            "subject": "s",
            "body": "b",
            "broadcast_ids": [],
        },
        incident_id=inc.id,
        operator_id=inc.operator_id,
    )
    session.commit()

    report = outbox.drain_once(session)

    assert (report.sent, report.dead, report.errors) == (1, 0, 0)
    assert session.get(OutboxRow, row.id).status == outbox.SENT  # mock mode; no socket opened


def test_another_operators_outbox_row_cannot_write_this_notice(tmp_db, on):
    """§8 operator scoping, in the one place ``api.deps._owned`` cannot help: the drainer runs
    on the scheduler thread, where there is no active-operator context to scope by. The two
    rows' own ``operator_id`` values are compared instead, and a mismatch writes nothing."""
    _settings, session = tmp_db
    _inc, notice, row = _released(session)
    theirs = OutboxRow(
        id="not-ours",
        kind=outbox.EMAIL,
        idempotency_key="EMAIL:regulatory:foreign",
        payload_json="{}",
        operator_id="airtel",
        status=outbox.CLAIMED,
    )

    result = record_dispatch_outcome(
        session, theirs, notification_id=notice.id, final_status=outbox.SENT, now=ON_TIME
    )

    assert result is None
    session.refresh(notice)
    assert notice.status == QUEUED and notice.sent_at is None
    assert session.get(OutboxRow, row.id).status == outbox.PENDING


def test_an_outcome_for_a_notice_that_has_moved_on_is_dropped(tmp_db, on):
    """A stale or duplicated outcome must not overwrite a truthful state with an older one."""
    _settings, session = tmp_db
    cfg = get_settings().operator
    inc = _incident(session)
    notice = open_notification(session, inc, cfg)
    request_approval(session, notice, inc, cfg)
    assert notice.status == PENDING_APPROVAL
    row = OutboxRow(
        id="stale", kind=outbox.EMAIL, idempotency_key="EMAIL:regulatory:stale",
        payload_json="{}", operator_id=inc.operator_id, status=outbox.CLAIMED,
    )

    assert record_dispatch_outcome(session, row, notification_id=notice.id, final_status=outbox.SENT, now=ON_TIME) is None
    assert notice.status == PENDING_APPROVAL and notice.sent_at is None


def test_a_missing_notice_does_not_break_the_drain(tmp_db, on):
    """The drain records the outbox's own outcome even if the producer row has gone; a
    raising hook would roll the outcome back and have the row dispatched all over again."""
    _settings, session = tmp_db
    inc = _incident(session)
    row = OutboxRow(
        id="orphan", kind=outbox.EMAIL, idempotency_key="EMAIL:regulatory:orphan",
        payload_json="{}", operator_id=inc.operator_id, status=outbox.CLAIMED,
    )

    assert record_dispatch_outcome(session, row, notification_id="no-such-notice", final_status=outbox.SENT, now=ON_TIME) is None


def test_the_notice_row_is_only_ever_written_by_a_terminal_outcome(tmp_db, on):
    """Guards the shape of the contract rather than one path: whatever the dispatcher says,
    the row never carries ``sent_at`` without ``status == SENT``, and never the reverse."""
    _settings, session = tmp_db
    _inc, notice, _row = _released(session)

    outbox.drain_once(session)
    session.refresh(notice)

    assert (notice.status == SENT) == (notice.sent_at is not None)
