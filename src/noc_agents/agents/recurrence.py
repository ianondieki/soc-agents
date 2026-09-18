"""RECURRENCE: count repeat failures at the site and open/update a problem record."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from noc_agents.db.models import IncidentRow, ProblemRow, utcnow
from noc_agents.orchestrator.contract import IncidentState, RunContext, StepResult
from noc_agents.services.numbering import next_problem_number


def problem_signature(site_id: str, failure_domain: str) -> str:
    """The identity of a problem record: ``site|domain`` (spec D7, defect #34).

    It must be computed from exactly the columns the occurrence count is counted
    over — see :func:`run`. Before this fix the signature also carried
    ``alarm_code`` while the count did not, so a chronic site reporting the same
    failure under two alarm codes opened two problem records that each claimed
    the *combined* count. A known-error record then had no single PRB to attach
    to, which is the whole reason §7.7 needs this.
    """
    return f"{site_id}|{failure_domain}"


def input_summary(state: IncidentState, ctx: RunContext) -> str:
    return state.fingerprint


def run(state: IncidentState, ctx: RunContext) -> StepResult:
    cfg, session, inc = ctx.cfg, ctx.session, state.incident
    lookback = int((cfg.recurrence or {}).get("lookback_days", 30))
    threshold = int((cfg.recurrence or {}).get("threshold_count", 3))
    since = utcnow() - timedelta(days=lookback)
    # The count is over site + failure_domain within the lookback window. Keep
    # problem_signature() over the same two columns — that pairing IS the fix.
    prior = session.scalars(
        select(IncidentRow).where(
            IncidentRow.operator_id == cfg.operator_id,
            IncidentRow.site_id == inc.site_id,
            IncidentRow.failure_domain == inc.failure_domain,
            IncidentRow.created_at >= since,
        )
    ).all()
    count = len(prior)
    inc.recurrence_count = count
    if not (count >= threshold and (cfg.recurrence or {}).get("auto_open_problem", True)):
        return StepResult(
            output_summary=f"count={count} threshold={threshold}",
            rationale="Below recurrence threshold — no problem record",
            tools=[{"name": "count_recurrence", "ok": True, "latency_ms": 2}],
        )

    sig = problem_signature(inc.site_id, inc.failure_domain)
    problem = session.scalar(
        select(ProblemRow).where(
            ProblemRow.operator_id == cfg.operator_id,
            ProblemRow.signature == sig,
            ProblemRow.status.in_(["OPEN", "MONITORING"]),
        )
    )
    if problem is None:
        pnum = next_problem_number(
            session,
            cfg.problem_prefix,
            cfg.timezone,
            numbering_style=getattr(cfg, "numbering_style", "inc9"),
        )
        problem = ProblemRow(
            operator_id=cfg.operator_id,
            problem_number=pnum,
            signature=sig,
            site_id=inc.site_id,
            region_code=inc.region_code,
            occurrence_count=count,
            summary=f"Recurring {inc.failure_domain} at {inc.site_id} ({count}x in {lookback}d)",
            dominant_failure_domain=inc.failure_domain,
        )
        problem.linked_incident_ids = [inc.id]
        session.add(problem)
        session.flush()
    else:
        problem.occurrence_count = count
        problem.last_seen = utcnow()
        ids = problem.linked_incident_ids
        if inc.id not in ids:
            ids.append(inc.id)
            problem.linked_incident_ids = ids
    inc.problem_id = problem.id
    return StepResult(
        output_summary=f"problem {problem.problem_number} count={count}",
        rationale=f"Threshold {threshold} met — chronic site under problem management",
        tools=[{"name": "create_or_update_problem", "ok": True, "latency_ms": 3}],
    )
