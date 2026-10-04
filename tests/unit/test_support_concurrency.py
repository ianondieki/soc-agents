"""Concurrency (review finding 2): two people, or two complaints, acting on one case or one
transfer at the same moment, on a real file-backed SQLite database with two threads.

Each test puts a barrier exactly where the race window was -- inside the tool run, inside the
step numbering -- so that WITHOUT the write lock both threads are guaranteed to be inside the
window together, and the bug shows every time. WITH the lock the second thread waits at
``BEGIN IMMEDIATE``, the first times out at the barrier alone and commits, and the second then
reads what the first wrote. The barrier timeout is what keeps the locked case from deadlocking.
"""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from noc_agents.db.models import Base
from noc_agents.db.models_support import SupportMessageRow, SupportStepRow, SupportToolCallRow
from noc_agents.support import desk, views
from noc_agents.support.context import default_context

OP = "safaricom"
CTX = default_context()
BARRIER_TIMEOUT_S = 1.5
PARKED = "Nimetuma 12000 kwa namba mbaya, code ni SHR2M9PL4Q. Tafadhali rudisha."


@pytest.fixture()
def factory(tmp_path):
    from noc_agents.db import models_all  # noqa: F401  (every table on Base)

    engine = create_engine(f"sqlite:///{(tmp_path / 'race.db').as_posix()}",
                           connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, autoflush=False, future=True)
    engine.dispose()


def _rendezvous(parties: int = 2):
    barrier = threading.Barrier(parties)

    def wait() -> None:
        try:
            barrier.wait(timeout=BARRIER_TIMEOUT_S)
        except threading.BrokenBarrierError:
            pass  # alone in the window: the other thread is (correctly) waiting for the lock

    return wait


def _race(factory, work) -> list[object]:
    """Run ``work(session)`` on two threads at once; each result is a return value or the exception."""
    results: list[object] = [None, None]

    def run(i: int) -> None:
        session = factory()
        try:
            results[i] = work(session)
        except Exception as exc:  # noqa: BLE001 -- the loser's refusal is the result we assert on
            results[i] = exc
        finally:
            session.close()

    threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return results


def _parked_case(factory) -> tuple[str, str]:
    with factory() as session:
        row = desk.process_complaint(session, operator_id=OP, body=PARKED, msisdn="0700000118", ctx=CTX,
                                     emit_events=False).complaint
        call = session.scalar(select(SupportToolCallRow).where(SupportToolCallRow.complaint_id == row.id,
                                                               SupportToolCallRow.status == "needs_approval"))
        return row.id, call.id


def test_two_simultaneous_approvals_reverse_the_transfer_once(factory, monkeypatch):
    complaint_id, call_id = _parked_case(factory)
    wait = _rendezvous()
    real_run_tool = desk.run_tool

    def run_tool_in_the_window(name, env, args, **kwargs):
        if name == "reverse_mpesa":
            wait()  # both approvers inside the tool run at once, unless the lock serialises them
        return real_run_tool(name, env, args, **kwargs)

    monkeypatch.setattr(desk, "run_tool", run_tool_in_the_window)

    def approve(session):
        row = views.get_complaint(session, OP, complaint_id)
        desk.approve(session, row, call_id, actor="approver", ctx=CTX, emit_events=False)
        return "approved"

    results = _race(factory, approve)
    assert sorted(type(r).__name__ for r in results) == ["DeskConflict", "str"], results
    with factory() as session:
        assert session.get(SupportToolCallRow, call_id).status == "approved"
        tickets = session.scalar(select(func.count()).select_from(SupportToolCallRow).where(
            SupportToolCallRow.complaint_id == complaint_id, SupportToolCallRow.tool == "update_ticket"))
        reversed_messages = session.scalar(select(func.count()).select_from(SupportMessageRow).where(
            SupportMessageRow.complaint_id == complaint_id, SupportMessageRow.body.like("%we have reversed%")))
        assert (tickets, reversed_messages) == (1, 1)


def test_two_simultaneous_claims_make_one_owner_and_no_duplicate_step_numbers(factory, monkeypatch):
    with factory() as session:
        row = desk.process_complaint(session, operator_id=OP, body="Someone did a SIM swap on my line", msisdn="0700001245",
                                     ctx=CTX, emit_events=False).complaint
        complaint_id = row.id
    wait = _rendezvous()
    real_next_seq = desk._next_seq

    def next_seq_in_the_window(session, row):
        seq = real_next_seq(session, row)
        wait()  # both claimers hold the same "next" number at once, unless the lock serialises them
        return seq

    monkeypatch.setattr(desk, "_next_seq", next_seq_in_the_window)

    def claim(session):
        desk.claim(session, views.get_complaint(session, OP, complaint_id), actor=f"agent-{threading.get_ident()}",
                   emit_events=False)
        return "claimed"

    results = _race(factory, claim)
    assert sorted(type(r).__name__ for r in results) == ["DeskConflict", "str"], results
    with factory() as session:
        seqs = session.scalars(select(SupportStepRow.seq).where(SupportStepRow.complaint_id == complaint_id)).all()
        assert len(seqs) == len(set(seqs))
        assert [s for s in session.scalars(select(SupportStepRow.action).where(
            SupportStepRow.complaint_id == complaint_id)) if s == "claimed"] == ["claimed"]


def test_two_simultaneous_complaints_about_one_transfer_park_one_reversal(factory, monkeypatch):
    wait = _rendezvous()
    real_run_tool = desk.run_tool

    def run_tool_in_the_window(name, env, args, **kwargs):
        if name == "reverse_mpesa":
            wait()
        return real_run_tool(name, env, args, **kwargs)

    monkeypatch.setattr(desk, "run_tool", run_tool_in_the_window)
    texts = iter([PARKED, "Please reverse SHR2M9PL4Q, I sent KES 12,000 to the wrong number by mistake"])
    lock = threading.Lock()

    def file(session):
        with lock:
            text = next(texts)
        return desk.process_complaint(session, operator_id=OP, body=text, msisdn="0700000118", ctx=CTX,
                                      emit_events=False).complaint.escalation_reason_code

    results = _race(factory, file)
    assert sorted(results) == ["over_refund_limit", "tool_failed"], results  # the second points at the first
    with factory() as session:
        parked = session.scalar(select(func.count()).select_from(SupportToolCallRow).where(
            SupportToolCallRow.tool == "reverse_mpesa", SupportToolCallRow.status == "needs_approval"))
        assert parked == 1
