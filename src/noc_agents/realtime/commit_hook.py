"""Realtime after commit (spec §7.0.4): the UI is never told about a row the database did not keep.

``RunTracker`` used to call ``hub.publish_sync`` from inside the open transaction, so a
fail-closed rollback left the UI believing steps had completed whose rows no longer existed.
Now the tracker calls :func:`buffer_event`, which parks the event on the session
(``session.info["events"]``), and the events leave the process only from the session's
``after_commit`` listener — in insertion order, each exactly once. A rollback of any kind
discards the buffer instead.

Listeners are registered on the ``Session`` class, so they apply to every session in the
process, including ones created before this module was imported.

Ordering facts this module relies on (SQLAlchemy 2.0, ``SessionTransaction``):

* ``after_commit`` fires for the outermost transaction **and** for a savepoint release
  (``begin_nested()``), before ``close()`` resets ``session._nested_transaction``; so
  ``session.in_nested_transaction()`` is still true during a savepoint's ``after_commit``
  and the flush waits for the outermost commit.
* ``after_rollback`` fires on a real DBAPI rollback; ``after_soft_rollback`` after every
  rollback including the ones that only pop the transaction stack. Both discard: a savepoint
  rollback over-discards rather than risk announcing a row that was rolled back.
* ``Session.close()`` fires neither rollback event, only ``after_transaction_end``; a buffer
  left behind by a closed-without-commit session must not leak into that session's next
  transaction (``session.info`` survives ``close()``), so the outermost transaction's end
  discards whatever is still there. On the commit path the flush has already emptied it.
"""

from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.orm import Session, SessionTransaction

from noc_agents.realtime.hub import RealtimeEvent, hub

EVENTS_KEY = "events"


def buffer_event(session: Session, event: RealtimeEvent) -> None:
    """Queue ``event`` for publication when ``session``'s outermost transaction commits."""
    session.info.setdefault(EVENTS_KEY, []).append(event)


def pending_events(session: Session) -> list[RealtimeEvent]:
    """The events still waiting on ``session`` (a copy; for tests and diagnostics)."""
    return list(session.info.get(EVENTS_KEY, ()))


@event.listens_for(Session, "after_commit")
def _flush(session: Session) -> None:
    """Publish the buffered events in insertion order; the hub stamps the global seq."""
    if session.in_nested_transaction():  # a savepoint release: the rows are not durable yet
        return
    for ev in session.info.pop(EVENTS_KEY, []):
        hub.publish_sync(ev)  # never raises (EventHub contract)


@event.listens_for(Session, "after_rollback")
@event.listens_for(Session, "after_soft_rollback")
def _discard(session: Session, *_args: object) -> None:
    session.info.pop(EVENTS_KEY, None)


@event.listens_for(Session, "after_transaction_end")
def _discard_if_abandoned(session: Session, transaction: SessionTransaction) -> None:
    if transaction.parent is None:  # outermost only; a committed buffer is already empty here
        session.info.pop(EVENTS_KEY, None)
