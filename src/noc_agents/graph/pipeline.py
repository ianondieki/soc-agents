"""Facade over the orchestrator: the public entry points the API and scripts import.

The lifecycle itself lives in ``noc_agents.orchestrator.runner`` (one module per agent
under ``noc_agents.agents``). The helpers below are re-exported under their historical
underscore names so existing imports keep working.

Outbox (spec §7.0.2): the lifecycle only *queues* its side effects. ``process_event``
drains the outbox right after the runner's commit when ``OUTBOX_SYNC_DRAIN`` is on (the
default until the scheduler thread of §7.0.3 owns the drain), so the API, the demo and the
tests see the email leave — after the commit, never inside it.
"""

from __future__ import annotations

import logging
import os

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from noc_agents.config import AppSettings
from noc_agents.db.models import BroadcastRow, HitlTaskRow, IncidentRow, get_session, utcnow
from noc_agents.domain.schemas import EventIngest
from noc_agents.orchestrator.outbox import DrainReport, drain_once
from noc_agents.services.composition import (  # noqa: F401  (re-exported)
    compose_email as _compose_email,
    compose_narrative as _compose_narrative,
    compose_sms as _compose_sms,
    needs_hitl as _needs_hitl,
    region_label as _region_label,
    sla_due as _sla_due,
)
from noc_agents.services.hitl import GATING_TASK_TYPE
from noc_agents.services.ledger import (  # noqa: F401  (re-exported)
    ROOT,
    eat_now as _eat_now,
    ledger_root,
    write_excel_row as _write_excel_row,
)
from noc_agents.services.notify import QUEUED, dispatch_incident_email, dispatch_incident_sms

# NOTE: no module-level import of noc_agents.orchestrator.runner here. graph/__init__ imports this
# module on ANY `noc_agents.graph.*` import, so a top-level runner import would be circular.
# (orchestrator.outbox is safe: it imports nothing under noc_agents.graph.)

log = logging.getLogger(__name__)


def process_event(session: Session, settings: AppSettings, event: EventIngest) -> IncidentRow:
    """Full multi-agent lifecycle for one alarm/event (Safaricom-first product path)."""
    from noc_agents.orchestrator.runner import run_incident_lifecycle  # lazy on purpose, see NOTE above

    inc = run_incident_lifecycle(session, settings, event)  # commits, or raises with nothing to drain
    if sync_drain_enabled():
        drain_once(session)  # after the commit: the rows are durable and no transaction is open
    return inc


def sync_drain_enabled() -> bool:
    """OUTBOX_SYNC_DRAIN: drain the outbox synchronously after each producer's commit.

    Default on: it preserves today's behaviour (the email leaves during the request) for
    the API, the demo and the tests. Set it off once a scheduler thread drains instead.
    """
    raw = (os.getenv("OUTBOX_SYNC_DRAIN") or "true").strip().lower()
    return raw not in ("0", "false", "no", "off")


def drain_outbox(session: Session) -> DrainReport:
    """One drain pass on ``session`` (which must not be mid-transaction: commit first)."""
    return drain_once(session)


def drain_after_commit(session: Session) -> None:
    """Drain once the caller's current transaction commits, in a session of its own.

    For producers whose commit belongs to a caller this module does not control (the HITL
    approve route). The listener fires once, after the COMMIT has been issued; it uses a
    fresh session because the committing one is still winding its transaction down. A
    drain failure is logged, not raised: the approval is already durable and the rows stay
    PENDING for the next drain.
    """
    if session.info.get("outbox_drain_armed"):
        return
    session.info["outbox_drain_armed"] = True

    @event.listens_for(session, "after_commit", once=True)
    def _drain(committed: Session) -> None:
        committed.info.pop("outbox_drain_armed", None)
        other = get_session()
        try:
            drain_once(other)
        except Exception:  # noqa: BLE001
            log.exception("outbox: post-commit drain failed; rows stay PENDING for the next drain")
        finally:
            other.close()


def release_broadcasts_after_hitl(session: Session, incident_id: str, *, approved_by: str | None = None) -> None:
    """After HITL approve: queue the held drafts in the outbox as approved rows.

    Nothing is transmitted here. The drafts go PENDING_HITL → QUEUED and one outbox row per
    SMS draft plus one EMAIL row (``requires_hitl=1`` with ``approved_by``/``approved_at``
    set, so the dispatcher's refusal rule lets them through) is queued in the caller's
    transaction. When OUTBOX_SYNC_DRAIN is on, a drain runs right after that commit, so the
    approved email leaves as promptly as it did before — but no longer inside the transaction.
    """
    rows = session.scalars(
        select(BroadcastRow).where(
            BroadcastRow.incident_id == incident_id,
            BroadcastRow.status == "PENDING_HITL",
        )
    ).all()
    inc = session.get(IncidentRow, incident_id)
    if inc is None or not rows:
        return
    approved_by = approved_by or _latest_approver(session, incident_id) or "HITL"
    approved_at = utcnow()
    sms_drafts = [(b.audience, b.message, b.id) for b in rows if b.channel == "SMS"]
    email_rows = [b for b in rows if b.channel == "EMAIL"]
    if sms_drafts:
        dispatch_incident_sms(
            session, inc, drafts=sms_drafts, requires_hitl=True, approved_by=approved_by, approved_at=approved_at
        )
    if email_rows:  # one real email per incident, from the approved wording
        dispatch_incident_email(
            session,
            inc,
            email_rows[0].message,
            audience="HITL_APPROVED",
            requires_hitl=True,
            approved_by=approved_by,
            approved_at=approved_at,
            broadcast_ids=[b.id for b in email_rows],
        )
    for b in rows:
        b.status = QUEUED
        b.sent_at = None
    session.flush()
    if sync_drain_enabled():
        drain_after_commit(session)


def _latest_approver(session: Session, incident_id: str) -> str | None:
    session.flush()  # the approval may still be a pending ORM change (autoflush is off), as in services/hitl.py
    task = session.scalars(
        select(HitlTaskRow)
        .where(
            HitlTaskRow.incident_id == incident_id,
            HitlTaskRow.task_type == GATING_TASK_TYPE,
            HitlTaskRow.status == "APPROVED",
        )
        .order_by(HitlTaskRow.resolved_at.desc())
    ).first()
    return task.resolved_by if task is not None else None
