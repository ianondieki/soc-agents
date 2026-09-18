"""SQLite under concurrent access (spec §8, Phase 1).

One file, one writer. The connection is opened ``check_same_thread=False`` with a 30 s busy
timeout, and since Phase 1 the file runs in WAL, so readers never block the writer and a
second writer waits for the lock instead of failing at once. Everything below uses REAL
threads on that one file and asserts the outcome an operator would care about: nothing is
lost, nothing is applied twice, and nothing deadlocks.

What is proved, in order:

* the file really is in WAL and the busy timeout really is 30 s — the precondition the rest
  of this module rests on;
* **parallel ``process_event``**: N alarms ingested at once produce N incidents, N distinct
  incident numbers (the ``BEGIN IMMEDIATE`` sequence allocator is the contended row) and N
  complete 12-step runs, with no ``database is locked``;
* **the allocator on its own** and **five copies of one alarm at once**: no duplicate ticket
  numbers, and one outage is still one ticket rather than five;
* **a drain racing an incident write**: a dispatcher looping ``drain_once`` while alarms are
  being ingested transmits each row exactly once and leaves nothing behind;
* **two drains racing each other**: the compare-and-set claim means one send per row even
  when two dispatchers pick up the same PENDING row at the same instant;
* **two HITL decisions on one task**: an approve and a reject fired together — exactly one
  200 and one 409, one decision recorded, and the side effects of the LOSING decision never
  run (no double-apply: the broadcasts are either all released or all cancelled, never both);
* **duplicate enqueue under contention**: two threads enqueueing one idempotency key leave
  one row.

Nothing here retries or sleeps its way to a pass. Every wait is a ``threading.Barrier`` or a
join with a hard timeout, so a genuine deadlock fails the test instead of hanging the suite.
"""

from __future__ import annotations

import importlib
import threading
from collections import Counter

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from noc_agents.config import get_settings
from noc_agents.db.models import (
    AgentRunRow,
    AgentRunStepRow,
    BroadcastRow,
    HitlTaskRow,
    IncidentRow,
    OutboxRow,
    WorkNoteRow,
    get_session,
    init_db,
)
from noc_agents.domain.schemas import EventIngest
from noc_agents.graph.pipeline import process_event
from noc_agents.orchestrator.outbox import drain_once, enqueue
from noc_agents.realtime.hub import hub
from noc_agents.services import notify
from noc_agents.services.numbering import next_incident_number

JOIN_TIMEOUT_S = 120  # a deadlock must fail the test, not hang the suite
WRITERS = 6

HUB_EVENT = {  # P2: held at the HITL gate, which is what the decision race needs
    "site_id": "SFC-NBIE-HUB-EMB",
    "site_name": "Embakasi East Aggregation HUB",
    "site_type": "HUB",
    "region_code": "NBI_E",
    "alarm_code": "POWER_GRID_FAIL",
    "failure_domain": "POWER",
    "users_affected": 450000,
}


def _bts_event(n: int) -> dict:
    """A distinct P4 site per writer, so CORRELATE does not merge them into one ticket."""
    return {
        "site_id": f"SFC-MTK-BTS-MCH{n:02d}",
        "site_name": f"Machakos Town BTS {n}",
        "site_type": "BTS",
        "region_code": "MTK",
        "alarm_code": "SITE_DOWN",
        "failure_domain": "POWER",
        "users_affected": 3000 + n,
    }


# --- fixtures ---------------------------------------------------------------------------------


@pytest.fixture()
def shared_db(tmp_path, monkeypatch):
    """One SQLite file, one engine, sessions handed out per thread (as the API does)."""
    db = tmp_path / "contention.db"
    url = f"sqlite:///{db.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("OPERATOR_PROFILE", "safaricom")
    monkeypatch.setenv("LEDGER_DIR", str(tmp_path / "ledgers"))
    monkeypatch.setenv("LIVE_AGENT_DELAY_MS", "0")

    import noc_agents.config as cfg
    import noc_agents.db.models as models

    cfg.clear_settings_cache()
    models._engine = None
    models.SessionLocal = None
    init_db(url)
    hub._history.clear()
    yield get_settings()
    hub._history.clear()
    cfg.clear_settings_cache()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "contention_api.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")
    monkeypatch.setenv("OPERATOR_PROFILE", "safaricom")
    monkeypatch.setenv("LEDGER_DIR", str(tmp_path / "ledgers"))
    monkeypatch.setenv("LIVE_AGENT_DELAY_MS", "0")

    import noc_agents.config as cfg
    import noc_agents.db.models as models
    import noc_agents.main as main

    cfg.clear_settings_cache()
    models._engine = None
    models.SessionLocal = None
    importlib.reload(main)
    hub._history.clear()
    with TestClient(main.app) as c:
        yield c
    hub._history.clear()


# --- helpers ----------------------------------------------------------------------------------


def _run_threads(targets: list) -> None:
    """Start every target, join with a hard timeout, and fail loudly on a thread still alive."""
    threads = [threading.Thread(target=t, daemon=True) for t in targets]
    for th in threads:
        th.start()
    for th in threads:
        th.join(JOIN_TIMEOUT_S)
    stuck = [th.name for th in threads if th.is_alive()]
    assert not stuck, f"threads still running after {JOIN_TIMEOUT_S}s — deadlock or lock starvation: {stuck}"


def _read(fn):
    session = get_session()
    try:
        return fn(session)
    finally:
        session.close()


def _spy_sends(monkeypatch) -> list[str]:
    """Record every SMTP-adapter call the dispatcher makes, without changing what it does."""
    calls: list[str] = []
    guard = threading.Lock()
    real = notify.transmit_email

    def spy(payload):
        with guard:
            calls.append(payload.get("subject", ""))
        return real(payload)

    monkeypatch.setattr(notify, "transmit_email", spy)
    return calls


# --- 0. the precondition ------------------------------------------------------------------------


def test_the_file_is_in_wal_with_a_busy_timeout(shared_db):
    """WAL plus a 30 s busy timeout plus cross-thread connections: the three settings every
    other test in this module depends on, checked at the sqlite3 connection itself."""
    session = get_session()
    try:
        assert session.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert int(session.execute(text("PRAGMA busy_timeout")).scalar()) == 30_000

        # check_same_thread=False: the very connection opened above must be usable from
        # another thread, which is how the API serves a request off the anyio worker pool.
        seen: list[object] = []

        def use_from_another_thread():
            try:
                seen.append(session.execute(text("SELECT 1")).scalar())
            except BaseException as exc:  # noqa: BLE001
                seen.append(exc)

        _run_threads([use_from_another_thread])
        assert seen == [1], f"the connection is thread-bound: {seen}"
    finally:
        session.close()


# --- 1. parallel writers --------------------------------------------------------------------


def test_parallel_process_event_loses_nothing_and_duplicates_nothing(shared_db):
    """Six alarms ingested at the same instant, each on its own Session and its own thread.

    The contended rows are ``daily_sequences`` (allocated under ``BEGIN IMMEDIATE``) and the
    outbox. Six incidents, six distinct INC numbers, six complete runs, no lock errors.
    """
    settings = shared_db
    barrier = threading.Barrier(WRITERS)
    numbers: list[str] = []
    errors: list[BaseException] = []
    guard = threading.Lock()

    def writer(n: int):
        def _run():
            session = get_session()
            try:
                barrier.wait(JOIN_TIMEOUT_S)
                inc = process_event(session, settings, EventIngest(**_bts_event(n)))
                with guard:
                    numbers.append(inc.incident_number)
            except BaseException as exc:  # noqa: BLE001 — the point is to report it, not swallow it
                with guard:
                    errors.append(exc)
            finally:
                session.close()

        return _run

    _run_threads([writer(n) for n in range(WRITERS)])

    assert errors == [], f"concurrent process_event raised: {[repr(e) for e in errors]}"
    assert len(numbers) == WRITERS
    assert len(set(numbers)) == WRITERS, f"incident numbers collided under contention: {sorted(numbers)}"

    rows = _read(lambda s: list(s.scalars(select(IncidentRow).order_by(IncidentRow.incident_number))))
    assert [r.incident_number for r in rows] == sorted(numbers)
    assert {r.site_id for r in rows} == {_bts_event(n)["site_id"] for n in range(WRITERS)}

    # Every run is complete: no half-written lifecycle left behind by a lock wait.
    runs = _read(lambda s: list(s.scalars(select(AgentRunRow))))
    assert len(runs) == WRITERS
    assert {r.status for r in runs} == {"SUCCEEDED"}
    steps = _read(lambda s: Counter(st.run_id for st in s.scalars(select(AgentRunStepRow))))
    assert set(steps.values()) == {12}, dict(steps)


def test_concurrent_number_allocation_never_collides(shared_db):
    """The ticket-number allocator on its own, from eight fresh sessions at once.

    ``services/numbering._next_sequence_value`` opens ``BEGIN IMMEDIATE`` and then does a
    read-modify-write of one ``daily_sequences`` row. Two allocators that both read
    ``last_value`` before either writes would hand out the same INC number, which on a NOC
    floor means two tickets that cannot be told apart. Eight threads, eight numbers, no gaps.
    """
    n = 8
    barrier = threading.Barrier(n)
    numbers: list[str] = []
    errors: list[BaseException] = []
    guard = threading.Lock()

    def allocate():
        session = get_session()
        try:
            barrier.wait(JOIN_TIMEOUT_S)
            number = next_incident_number(session, "INC", numbering_style="inc9")
            session.commit()
            with guard:
                numbers.append(number)
        except BaseException as exc:  # noqa: BLE001
            session.rollback()
            with guard:
                errors.append(exc)
        finally:
            session.close()

    _run_threads([allocate] * n)
    assert errors == [], f"concurrent allocation raised: {[repr(e) for e in errors]}"
    assert sorted(numbers) == [f"INC{i:06d}" for i in range(1, n + 1)], sorted(numbers)


def test_the_same_alarm_ingested_five_times_at_once_makes_one_ticket(shared_db):
    """Five copies of one alarm land together (an element manager retrying, or five links
    flapping on the same site). CORRELATE must merge them: one incident, five callers all
    handed the same ticket — not five tickets for one outage."""
    settings = shared_db
    event = {
        "site_id": "SFC-MTK-BTS-DUP01",
        "site_name": "Machakos Duplicate BTS",
        "site_type": "BTS",
        "region_code": "MTK",
        "alarm_code": "SITE_DOWN",
        "failure_domain": "POWER",
        "users_affected": 3200,
    }
    n = 5
    barrier = threading.Barrier(n)
    returned: list[str] = []
    errors: list[BaseException] = []
    guard = threading.Lock()

    def ingest():
        session = get_session()
        try:
            barrier.wait(JOIN_TIMEOUT_S)
            inc = process_event(session, settings, EventIngest(**event))
            with guard:
                returned.append(inc.incident_number)
        except BaseException as exc:  # noqa: BLE001
            with guard:
                errors.append(exc)
        finally:
            session.close()

    _run_threads([ingest] * n)
    assert errors == [], f"concurrent duplicate ingest raised: {[repr(e) for e in errors]}"

    rows = _read(lambda s: list(s.scalars(select(IncidentRow).where(IncidentRow.site_id == event["site_id"]))))
    assert len(rows) == 1, f"one outage produced {len(rows)} tickets: {[r.incident_number for r in rows]}"
    assert set(returned) == {rows[0].incident_number}, returned
    merged = _read(
        lambda s: [
            n.body
            for n in s.scalars(select(WorkNoteRow).where(WorkNoteRow.incident_id == rows[0].id))
            if n.body.startswith("Correlated duplicate alarm")
        ]
    )
    assert len(merged) == n - 1, f"expected {n - 1} merge notes, got {merged}"


def test_a_drain_racing_an_incident_write_transmits_each_row_once(shared_db, monkeypatch):
    """A dispatcher looping ``drain_once`` while alarms land: one send per row, nothing stranded.

    This is the shape of the real system once the scheduler owns the drain — the writer's
    commit and the dispatcher's claim are on the same file at the same time.
    """
    settings = shared_db
    monkeypatch.setenv("OUTBOX_SYNC_DRAIN", "false")  # the drainer thread owns the outbox here
    sends = _spy_sends(monkeypatch)
    stop = threading.Event()
    start = threading.Barrier(2)
    errors: list[BaseException] = []
    guard = threading.Lock()

    def drainer():
        session = get_session()
        try:
            start.wait(JOIN_TIMEOUT_S)
            while not stop.is_set():
                drain_once(session)
            drain_once(session)  # final sweep after the writer has finished committing
        except BaseException as exc:  # noqa: BLE001
            with guard:
                errors.append(exc)
        finally:
            session.close()

    def ingester():
        session = get_session()
        try:
            start.wait(JOIN_TIMEOUT_S)
            for n in range(4):
                process_event(session, settings, EventIngest(**_bts_event(n)))
        except BaseException as exc:  # noqa: BLE001
            with guard:
                errors.append(exc)
        finally:
            session.close()
            stop.set()

    _run_threads([drainer, ingester])
    assert errors == [], f"drain racing a write raised: {[repr(e) for e in errors]}"

    rows = _read(lambda s: list(s.scalars(select(OutboxRow))))
    assert len(rows) == 4 * 4, f"expected 2 SMS + 1 EMAIL + 1 EXCEL_ROW per incident, got {Counter(r.kind for r in rows)}"
    unfinished = [(r.kind, r.status, r.last_error) for r in rows if r.status != "SENT"]
    assert unfinished == [], f"the race left rows unsent: {unfinished}"
    assert all(r.attempts == 1 for r in rows), [(r.kind, r.attempts) for r in rows]

    # One SMTP call per EMAIL row: a claim that leaked would show up as a second send.
    assert len(sends) == 4
    assert len(set(sends)) == 4, f"the same email was transmitted twice: {Counter(sends)}"

    # And exactly one email work note per incident (the drain writes it after the outcome commits).
    notes = _read(
        lambda s: Counter(
            n.incident_id for n in s.scalars(select(WorkNoteRow).where(WorkNoteRow.source == "email"))
        )
    )
    assert sorted(notes.values()) == [1, 1, 1, 1], dict(notes)


def test_two_drains_on_one_row_send_it_once(shared_db, monkeypatch):
    """Four dispatchers start together on one PENDING EMAIL row. The compare-and-set claim
    means one of them wins; the row is transmitted once and ends SENT."""
    settings = shared_db
    monkeypatch.setenv("OUTBOX_SYNC_DRAIN", "false")
    sends = _spy_sends(monkeypatch)

    writer = get_session()
    try:
        process_event(writer, settings, EventIngest(**_bts_event(1)))
    finally:
        writer.close()

    pending = _read(lambda s: [r.status for r in s.scalars(select(OutboxRow))])
    assert set(pending) == {"PENDING"}, pending

    drainers = 4
    barrier = threading.Barrier(drainers)
    errors: list[BaseException] = []
    guard = threading.Lock()

    def drainer():
        session = get_session()
        try:
            barrier.wait(JOIN_TIMEOUT_S)
            drain_once(session)
        except BaseException as exc:  # noqa: BLE001
            with guard:
                errors.append(exc)
        finally:
            session.close()

    _run_threads([drainer] * drainers)
    assert errors == [], f"concurrent drains raised: {[repr(e) for e in errors]}"

    rows = _read(lambda s: list(s.scalars(select(OutboxRow))))
    assert {r.status for r in rows} == {"SENT"}, [(r.kind, r.status, r.last_error) for r in rows]
    assert [r.attempts for r in rows] == [1] * len(rows), "a row was claimed by more than one drainer"
    assert len(sends) == 1, f"the email left {len(sends)} times under concurrent drains"
    assert (
        _read(lambda s: len(list(s.scalars(select(WorkNoteRow).where(WorkNoteRow.source == "email"))))) == 1
    ), "the email outcome was recorded twice"


def test_concurrent_enqueue_of_one_key_leaves_one_row(shared_db):
    """The outbox's unique idempotency key under real contention: INSERT OR IGNORE, one row."""
    payload = {
        "operator_id": "safaricom",
        "incident_number": "INC000001",
        "audience": "TEST",
        "subject": "[P4] INC000001 | contention",
        "body": "body",
        "recipients_ref": "DEMO_EMAIL_TO",
        "broadcast_ids": [],
    }
    n = 5
    barrier = threading.Barrier(n)
    ids: list[str] = []
    errors: list[BaseException] = []
    guard = threading.Lock()

    def enqueuer():
        session = get_session()
        try:
            barrier.wait(JOIN_TIMEOUT_S)
            row = enqueue(
                session,
                kind="EMAIL",
                idempotency_key="EMAIL:contention:TEST",
                payload=payload,
                operator_id="safaricom",
            )
            session.commit()
            with guard:
                ids.append(row.id)
        except BaseException as exc:  # noqa: BLE001
            session.rollback()
            with guard:
                errors.append(exc)
        finally:
            session.close()

    _run_threads([enqueuer] * n)
    assert errors == [], f"concurrent enqueue raised: {[repr(e) for e in errors]}"
    rows = _read(lambda s: list(s.scalars(select(OutboxRow).where(OutboxRow.idempotency_key == "EMAIL:contention:TEST"))))
    assert len(rows) == 1, f"the idempotency key produced {len(rows)} rows"
    assert set(ids) == {rows[0].id}, f"enqueue handed back ids that are not the surviving row: {set(ids)}"


# --- 2. two HITL decisions on one task ---------------------------------------------------------


@pytest.fixture()
def gated(client):
    """A P2 incident parked on its APPROVE_BROADCAST gate."""
    inc = client.post("/api/v1/events", json=HUB_EVENT).json()["incident"]
    assert inc["requires_hitl"] is True
    task = next(
        t
        for t in client.get("/api/v1/hitl/pending").json()
        if t["incident_id"] == inc["id"] and t["task_type"] == "APPROVE_BROADCAST"
    )
    return inc, task["id"]


@pytest.mark.parametrize("first", ["approve", "reject"])
def test_approve_and_reject_race_one_wins_and_nothing_is_double_applied(client, gated, first):
    """Two supervisors decide the same task in the same instant — one approves, one rejects.

    Exactly one gets 200; the loser gets 409 from the compare-and-set in
    ``services/hitl.transition_open_task`` and runs NONE of its side effects. The test does
    not care WHICH wins (that is a genuine race); it cares that the outcome is internally
    consistent — one status, one work note, one terminal run status, and broadcasts that are
    either all released or all CANCELLED but never a mixture of the two decisions.

    ``first`` only decides which thread is STARTED first (both then wait on a barrier); it is
    there so both the release branch and the cancel branch of the assertions get exercised.
    """
    inc, task_id = gated
    barrier = threading.Barrier(2)
    results: list[tuple[str, int]] = []
    guard = threading.Lock()

    def decide(kind: str, body: dict):
        def _run():
            barrier.wait(JOIN_TIMEOUT_S)
            r = client.post(f"/api/v1/hitl/{task_id}/{kind}", json=body)
            with guard:
                results.append((kind, r.status_code))

        return _run

    calls = {
        "approve": decide("approve", {"resolved_by": "Supervisor A"}),
        "reject": decide("reject", {"resolved_by": "Supervisor B", "reason": "wording wrong"}),
    }
    second = "reject" if first == "approve" else "approve"
    _run_threads([calls[first], calls[second]])

    codes = sorted(code for _kind, code in results)
    assert codes == [200, 409], f"both decisions were accepted or both refused: {results}"
    winner = next(kind for kind, code in results if code == 200)

    tasks = _read(
        lambda s: [(t.status, t.resolved_by, t.reason) for t in s.scalars(select(HitlTaskRow).where(HitlTaskRow.id == task_id))]
    )
    assert len(tasks) == 1
    status, resolved_by, reason = tasks[0]
    assert status == ("APPROVED" if winner == "approve" else "REJECTED")
    assert resolved_by == ("Supervisor A" if winner == "approve" else "Supervisor B")
    assert (reason is None) is (winner == "approve"), "the loser's reason leaked onto the winner's row"

    # Exactly one decision note: the loser's side effects never ran.
    hitl_notes = _read(
        lambda s: [
            n.body
            for n in s.scalars(
                select(WorkNoteRow).where(WorkNoteRow.incident_id == inc["id"], WorkNoteRow.source == "hitl")
            )
        ]
    )
    assert len(hitl_notes) == 1, hitl_notes
    assert hitl_notes[0].startswith("HITL approved" if winner == "approve" else "HITL rejected")

    # The broadcasts followed one decision only — never released AND cancelled.
    statuses = _read(
        lambda s: {b.status for b in s.scalars(select(BroadcastRow).where(BroadcastRow.incident_id == inc["id"]))}
    )
    assert "PENDING_HITL" not in statuses, statuses
    if winner == "approve":
        assert "CANCELLED" not in statuses, f"a rejected draft survived an approval: {statuses}"
    else:
        assert statuses == {"CANCELLED"}, f"a rejection left drafts in a sent state: {statuses}"
        channel_rows = _read(
            lambda s: [
                (r.kind, r.status)
                for r in s.scalars(
                    select(OutboxRow).where(
                        OutboxRow.incident_id == inc["id"], OutboxRow.kind.in_(["EMAIL", "SMS", "WHATSAPP", "ICS_INVITE"])
                    )
                )
            ]
        )
        assert channel_rows == [], f"the rejected broadcast was queued for transmission anyway: {channel_rows}"

    # The waiting run was finished once, by the winner.
    runs = _read(
        lambda s: [(r.status, r.error_summary) for r in s.scalars(select(AgentRunRow).where(AgentRunRow.incident_id == inc["id"]))]
    )
    assert len(runs) == 1
    assert runs[0][0] == ("SUCCEEDED" if winner == "approve" else "CANCELLED")

    # And the incident's derived scalars agree with the one decision that happened.
    body = client.get(f"/api/v1/incidents/{inc['id']}").json()
    assert body["requires_hitl"] is False
    assert body["hitl_state"] == status

    # One realtime event for the decision that stood, none for the one that did not.
    kinds = Counter(e["type"] for e in hub._history if e["type"] in ("hitl.approved", "hitl.rejected"))
    assert kinds == Counter({("hitl.approved" if winner == "approve" else "hitl.rejected"): 1}), kinds


def test_claim_and_decision_race_never_loses_the_decision(client, gated):
    """A claim and an approve land together. CLAIMED is still an OPEN status, so the approve
    is allowed either way; what must never happen is the task ending up claimed-but-open with
    the approval silently dropped."""
    inc, task_id = gated
    barrier = threading.Barrier(2)
    results: dict[str, int] = {}
    guard = threading.Lock()

    def call(kind: str, body: dict):
        def _run():
            barrier.wait(JOIN_TIMEOUT_S)
            r = client.post(f"/api/v1/hitl/{task_id}/{kind}", json=body)
            with guard:
                results[kind] = r.status_code

        return _run

    _run_threads([call("claim", {"resolved_by": "Analyst"}), call("approve", {"resolved_by": "Supervisor A"})])

    assert results["approve"] in (200, 409), results
    assert results["claim"] in (200, 409), results
    final = _read(lambda s: s.get(HitlTaskRow, task_id).status)
    if results["approve"] == 200:
        assert final == "APPROVED", f"an accepted approval did not stick: {final} ({results})"
    else:
        assert final in ("APPROVED", "REJECTED"), f"the approval was refused but the task is still {final}"
