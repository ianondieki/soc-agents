"""The orchestrator: runs one alarm through every lifecycle agent, in order.

Read top-to-bottom, ``run_incident_lifecycle`` does this:

1. open a RUNNING run row and a tracker (the only thing that writes step/audit rows
   and publishes ``agent.*`` realtime events);
2. for each ``NodeCard`` in the registry: ask the agent for its input summary, start
   the step, call ``agent.run(state, ctx)``, record the returned ``StepResult``;
   a ``SHORT_CIRCUIT`` result (merge / cascade) ends the walk early;
3. finish the run, commit, publish the terminal ``incident.*`` event, return the row.

Error handling depends on the agent's criticality in its ``AgentProfile``:

* fail-closed (INGEST .. BROADCAST): the exception rolls the transaction back, a
  FAILED run row plus the one FAILED step are persisted in a fresh transaction,
  ``agent.run.finished`` with ``status: FAILED`` is published and the exception is
  re-raised to the caller. No incident row survives.
* fail-soft (EXEC_BRIEF .. MONITOR): the step is recorded FAILED with the error as its
  rationale and the run continues. A DB error, or anything that leaves the session
  needing a rollback, cannot be absorbed and is escalated to the fail-closed path.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from noc_agents.config import AppSettings
from noc_agents.db.models import AgentRunRow, AgentRunStepRow, IncidentRow, new_id, utcnow
from noc_agents.domain.schemas import EventIngest
from noc_agents.graph.instrumentation import RunTracker, timed_ms
from noc_agents.orchestrator.contract import (
    FAIL_SOFT,
    FAILED,
    SHORT_CIRCUIT,
    SUCCEEDED,
    WAITING_HITL,
    IncidentState,
    RunContext,
    StepResult,
)
from noc_agents.orchestrator.registry import NODE_CARDS, NodeCard, profile_for
from noc_agents.realtime.hub import RealtimeEvent, hub

GRAPH_NAME = "incident_lifecycle"


def run_incident_lifecycle(session: Session, settings: AppSettings, event: EventIngest) -> IncidentRow:
    """Full multi-agent lifecycle for one alarm/event. Commits; returns the incident row."""
    cfg = settings.operator
    run = AgentRunRow(
        id=new_id(),
        operator_id=cfg.operator_id,
        graph_name=GRAPH_NAME,
        trigger="EVENT",
        status="RUNNING",
        started_at=utcnow(),
    )
    session.add(run)
    session.flush()
    tracker = RunTracker(session, run)
    ctx = RunContext(session=session, settings=settings, tracker=tracker, run=run)
    state = IncidentState(event=event)
    facts = _RunFacts(run.id, cfg.operator_id, run.started_at)  # plain values that survive a rollback

    short: StepResult | None = None
    card: NodeCard | None = None
    step: AgentRunStepRow | None = None
    try:  # the guarded region ends with the commit; a failure after it propagates untouched
        for card in NODE_CARDS:
            t0 = time.perf_counter()
            inp, inp_error = _input_summary(card, state, ctx)
            step = tracker.start_step(card.node_id, card.agent, inp)
            if inp_error is not None:
                result = _soft_failure(card, inp_error, t0)
            else:
                result = _run_step(card, state, ctx, t0)
            if result.status == SHORT_CIRCUIT:
                # Recorded SUCCEEDED before run.incident_id is set, so the audit row has entity_id "".
                _record(tracker, step, result, status=SUCCEEDED)
                short = result
                break
            _record(tracker, step, result, status=result.status)
            step = None
        card, step = None, None  # anything raised below is "after the loop"
        if short is not None:
            run.incident_id = short.incident.id
            tracker.finish_run(SUCCEEDED)
        else:
            tracker.finish_run(WAITING_HITL if state.waiting_hitl else SUCCEEDED)
        session.commit()
    except Exception as exc:
        _fail_closed(session, tracker, facts, card, step, exc)
        raise

    # Post-commit epilogue: the rows are durable, so an error here is reported as-is.
    if short is not None:
        hub.publish_sync(
            RealtimeEvent(
                type=short.event_type,
                operator_id=cfg.operator_id,
                incident_id=short.incident.id,
                run_id=run.id,
                payload=short.event_payload,
            )
        )
        return short.incident
    inc = state.incident
    session.refresh(inc)
    hub.publish_sync(
        RealtimeEvent(
            type="incident.created",
            operator_id=cfg.operator_id,
            incident_id=inc.id,
            run_id=run.id,
            payload={
                "incident_number": inc.incident_number,
                "priority": inc.priority,
                "site_id": inc.site_id,
                "region_code": inc.region_code,
                "requires_hitl": inc.requires_hitl,
            },
        )
    )
    return inc


# --- one step ---------------------------------------------------------------------------


def _is_fail_soft(card: NodeCard) -> bool:
    return profile_for(card).criticality == FAIL_SOFT


def _input_summary(card: NodeCard, state: IncidentState, ctx: RunContext) -> tuple[str, Exception | None]:
    """The step's input summary; a broken summary on a fail-soft card must not fail the run."""
    try:
        return card.input_summary(state, ctx), None
    except Exception as exc:
        if not _is_fail_soft(card):
            raise
        return "", exc


def _run_step(card: NodeCard, state: IncidentState, ctx: RunContext, t0: float) -> StepResult:
    """Call the agent. A fail-soft agent's exception becomes a FAILED result instead of propagating."""
    try:
        return card.run(state, ctx)
    except Exception as exc:
        if not _is_fail_soft(card) or _session_broken(ctx.session, exc):
            raise
        return _soft_failure(card, exc, t0)


def _session_broken(session: Session, exc: Exception) -> bool:
    """A DB error, or a session left pending-rollback by a failed flush, cannot be absorbed fail-soft."""
    return isinstance(exc, SQLAlchemyError) or not session.is_active


def _soft_failure(card: NodeCard, exc: Exception, t0: float) -> StepResult:
    err = _error_text(exc)
    tools = profile_for(card).tools
    tool = tools[0] if tools else card.node_id.lower()
    return StepResult(
        status=FAILED,
        output_summary=f"{card.node_id} failed (fail-soft); run continued",
        rationale=err,
        tools=[{"name": tool, "ok": False, "latency_ms": timed_ms(t0), "error": err}],
        confidence=None,
    )


def _record(tracker: RunTracker, step: AgentRunStepRow, result: StepResult, *, status: str) -> None:
    tracker.complete_step(
        step,
        status=status,
        output_summary=result.output_summary,
        rationale=result.rationale,
        tools=result.tools,
        confidence=result.confidence,
    )


# --- fail-closed path -------------------------------------------------------------------


@dataclass(frozen=True)
class _RunFacts:
    """Plain values captured before any failure; ORM objects are not trusted after a rollback."""

    run_id: str
    operator_id: str
    started_at: datetime


def _error_text(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:2000]


def _ms_since(started: datetime | None) -> int | None:
    return None if started is None else int((utcnow() - started).total_seconds() * 1000)


def _fail_closed(
    session: Session,
    tracker: RunTracker,
    facts: _RunFacts,
    card: NodeCard | None,
    step: AgentRunStepRow | None,
    exc: Exception,
) -> None:
    """Roll everything back, persist a FAILED run (+ the failed step) and announce it."""
    err = _error_text(exc)
    node = card.node_id if card else None  # None: failure in finish_run/commit after the loop
    agent = card.agent if card else None
    if step is not None:  # capture plain values BEFORE the rollback expunges the row
        seq, started, inp = step.seq, step.started_at, step.input_summary
    else:  # start_step itself raised: tracker.seq is already incremented, the row never existed
        seq, started, inp = tracker.seq, None, ""
    session.rollback()  # discards run, steps, audit rows, incident, sequence bump, notes
    try:  # fresh transaction with exactly the rows that describe the failure
        session.add(
            AgentRunRow(
                id=facts.run_id,
                incident_id=None,
                operator_id=facts.operator_id,
                graph_name=GRAPH_NAME,
                trigger="EVENT",
                status=FAILED,
                started_at=facts.started_at,
                finished_at=utcnow(),
                current_node=node,
                error_summary=err,
            )
        )
        if card is not None:
            session.add(
                AgentRunStepRow(
                    id=new_id(),
                    run_id=facts.run_id,
                    seq=seq,
                    node_name=node,
                    agent_name=agent,
                    status=FAILED,
                    started_at=started or utcnow(),
                    finished_at=utcnow(),
                    duration_ms=_ms_since(started),
                    input_summary=inp,
                    output_summary="",
                    rationale=err,
                    tools_called=[],
                    confidence=None,
                )
            )
        session.commit()
    except Exception:  # the DB itself is broken: do not mask the original error
        session.rollback()
    hub.publish_sync(
        RealtimeEvent(
            type="agent.run.finished",
            operator_id=facts.operator_id,
            incident_id=None,
            run_id=facts.run_id,
            payload={
                "seq": seq,
                "incident_number": None,
                "run_id": facts.run_id,
                "status": FAILED,
                "error": err,
                "node": node,
            },
        )
    )
