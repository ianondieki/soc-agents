"""On-demand assist runs: redact → (LLM or template) → one short write transaction.

Three phases, no DB work in the middle (SQLite has one writer; a model call can take
tens of seconds and must never hold the write lock):

1. ``prepare(session)``: reads only; builds a plain-Python job (redacted payload +
   deterministic template answer). The session is rolled back afterwards.
2. ``call(job, llm)``: network only, never sees the session. The template fallback
   happens INSIDE ``call``, so a model failure needs no DB cleanup.
3. One write transaction: the tracked ``llm_assist`` run, its step, and an audit row per
   LLM attempt (DPA reg 41(2): date/time, recipient, justification, data description).

Every route here is read-only for incident/brief tables; the only rows written are the
run, its step and the audit row. The LLM never changes priority, assignment or status.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy.orm import Session

from noc_agents.config import AppSettings
from noc_agents.db.models import AgentRunRow, AuditRow, IncidentRow, new_id, utcnow
from noc_agents.graph.instrumentation import RunTracker
from noc_agents.llm.client import MODEL_DRAFTING, MODEL_REASONING, get_llm, reasoning_timeout_s
from noc_agents.llm.outputs import ExecBriefDraft, Hypothesis, RootCauseAnalysis
from noc_agents.llm.redaction import redact_incident, restore_names
from noc_agents.llm.structured import LlmCallRecord, parse_structured
from noc_agents.orchestrator.contract import FAILED, SUCCEEDED, StepResult
from noc_agents.services.composition import compose_brief

GRAPH_NAME = "llm_assist"
TRIGGER = "ON_DEMAND"
ANALYSIS_NODE = "RCA"
ANALYSIS_AGENT = "TicketingAgent"
BRIEF_NODE = "BRIEF_DRAFT"
BRIEF_AGENT = "ExecutiveBriefingAgent"

MAX_BRIEF_CHARS = 2000
MAX_SUMMARY_CHARS = 2000
MAX_HYPOTHESES = 5
MAX_CHECKS = 10

# Concurrency cap for in-flight model calls. The assist routes are sync and share the anyio
# threadpool with POST /api/v1/events. Worst case per call = (1 + LLM_MAX_RETRIES) x timeout x
# (1 + one fallback attempt): brief draft 2 x 20 s x 2 = 80 s, analysis 2 x 60 s x 2 = 240 s
# with the defaults (LLM_TIMEOUT_S / LLM_REASONING_TIMEOUT_S). Past this many in flight the
# extra callers get the template answer immediately.
MAX_CONCURRENT_ASSIST = 8

# Reasoning budget: fable keeps thinking on and its thinking tokens count against max_tokens,
# so the analysis call gets medium effort and room for thinking plus the JSON. A truncated
# reply (stop_reason max_tokens) is unusable output and falls back to the template.
ANALYSIS_EFFORT = "medium"
ANALYSIS_MAX_TOKENS = 8192
BRIEF_EFFORT = "low"
BRIEF_MAX_TOKENS = 2048
_ASSIST_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_ASSIST)

RECIPIENT = "Anthropic API"
DATA_DESCRIPTION = "redacted incident fields + scrubbed notes"

GUARDRAILS = (
    "You assist a Kenyan telecom NOC. Use only the facts in the JSON you are given; do not "
    "invent alarms, causes or timings. Person names appear as tokens like <PERSON_1>: keep "
    "them verbatim, never guess who they are. You draft text for a human to review; you do "
    "not decide priority, SLA, assignment or whether anything is sent."
)
ANALYSIS_SYSTEM = GUARDRAILS + (
    " Task: from the incident record and work notes, write a short root-cause analysis: a summary, "
    "up to five ranked hypotheses each with the evidence that supports it, and the concrete checks "
    "the field engineer or MSP should run next."
)
BRIEF_SYSTEM = GUARDRAILS + (
    f" Task: draft a plain-text executive status brief of at most {MAX_BRIEF_CHARS} characters. It must quote the "
    "incident number and priority exactly as given, state impact in one line, say what is known and "
    "what happens next, and ask readers to use the brief instead of phoning the NOC."
)

GENERIC_CHECKS = [
    "Confirm the alarm is still active on the element manager",
    "Check for a parent hub or transmission event covering this site",
    "Request an ETA and on-site findings from the assignee",
]
CHECKS_BY_DOMAIN: dict[str, list[str]] = {
    "POWER": [
        "Confirm mains supply and genset status with the MSP",
        "Check battery bank voltage and remaining countdown",
        "Verify rectifier alarms and DC distribution",
    ],
    "TRANSMISSION": [
        "Check the microwave link RSL and far-end status",
        "Confirm the aggregation hub is up and carrying traffic",
        "Look for fibre cut notifications on the same ring",
    ],
    "RADIO": [
        "Check RRU/BBU alarms and cell availability per band",
        "Confirm VSWR and RET status on the affected sectors",
        "Verify the site is not in a planned outage window",
    ],
    "CORE": [
        "Check core node and interface alarms for the region",
        "Confirm signalling and user-plane KPIs on neighbouring sites",
    ],
    "ENVIRONMENT": [
        "Check temperature, door and smoke sensors at the site",
        "Confirm air-conditioning and shelter status with the FE",
    ],
}


@dataclass
class AssistJob:
    """Plain-Python input to ``call``: everything from phase 1, no ORM objects."""

    incident_id: str
    incident_number: str
    priority: str
    payload: dict[str, Any]  # redacted; the only thing the model sees
    mapping: dict[str, str]  # token -> real name, kept local
    template: dict[str, Any]  # deterministic answer fields, already in final form

    def template_response(self) -> dict[str, Any]:
        return {"incident_id": self.incident_id, "source": "template", "model": None, **self.template}


def _template_step(output_summary: str, rationale: str) -> StepResult:
    return StepResult(
        output_summary=output_summary,
        rationale=rationale,
        tools=[{"name": "template", "ok": True, "latency_ms": 0}],
        confidence=0.6,
    )


def _llm_step(rec: LlmCallRecord, output_summary: str, rationale: str) -> StepResult:
    return StepResult(
        output_summary=output_summary,
        rationale=rationale,
        tools=[
            {
                "name": "claude.parse",
                "ok": rec.ok,
                "latency_ms": rec.latency_ms,
                "error": rec.error,
                "model": rec.model_used,
                "fallback_used": rec.fallback_used,
            }
        ],
        confidence=0.8 if rec.ok else 0.6,
    )


def _validated_or_none(validate: Callable[[], Any], rec: LlmCallRecord) -> Any:
    """Run a post-parse validator. A crash reads as unusable output and keeps ``rec`` (data has
    already left the box, so the transfer record must still be written)."""
    try:
        return validate()
    except Exception as exc:  # noqa: BLE001 — never lose the audit row over a validation bug
        _mark_unusable(rec, f"validation crashed: {type(exc).__name__}")  # class only: no model text in the audit row
        return None


def _mark_unusable(rec: LlmCallRecord, reason: str) -> str:
    """Record that the model's answer was NOT used, so the step tool entry, its confidence and
    the audit row agree with the template fallback. Keeps an earlier error (e.g. a timeout)."""
    rec.ok = False
    rec.error = rec.error or reason
    return rec.error


def _audit_row(settings: AppSettings, agent_name: str, incident_id: str, rec: LlmCallRecord, justification: str) -> AuditRow:
    payload = {
        "ts": utcnow().isoformat(),
        "recipient": RECIPIENT,
        "justification": justification,
        "data_description": DATA_DESCRIPTION,
        **rec.as_dict(),
    }
    return AuditRow(
        operator_id=settings.operator.operator_id,
        actor=agent_name,
        action="llm.call",
        entity_type="incident",
        entity_id=incident_id,
        rationale=justification,
        payload_json=json.dumps(payload)[:2000],
    )


def run_assist(
    session: Session,
    settings: AppSettings,
    *,
    agent_name: str,
    node_id: str,
    incident_id: str,
    incident_number: str,
    input_summary: str,
    justification: str,
    prepare: Callable[[Session], AssistJob],
    call: Callable[[AssistJob, Any], tuple[dict[str, Any], StepResult, LlmCallRecord | None]],
    empty_answer: dict[str, Any],
) -> dict[str, Any]:
    """Run one tracked assist step. Always returns a response dict; never raises for LLM trouble.

    ``empty_answer`` is the route's answer key with a minimal safe value, built from plain
    fields only; it is used when ``prepare`` itself fails so the caller still gets the
    documented shape.
    """
    t_start = utcnow()
    job: AssistJob | None = None
    rec: LlmCallRecord | None = None
    slot = False
    try:
        job = prepare(session)  # phase 1: reads only
        session.rollback()  # release any implicit transaction before the (slow) model call
        llm = get_llm(settings)
        if llm is not None:
            slot = _ASSIST_SLOTS.acquire(blocking=False)
        response, result, rec = call(job, llm if slot else None)  # phase 2: network only
        if llm is not None and not slot:
            result.rationale = f"assist busy ({MAX_CONCURRENT_ASSIST} calls in flight): deterministic template"
    except Exception as exc:  # noqa: BLE001 — belt and braces: call() already degrades to templates
        session.rollback()
        response = job.template_response() if job else {"incident_id": incident_id, "source": "template", "model": None, **empty_answer}
        result = StepResult(status=FAILED, rationale=f"{type(exc).__name__}: {exc}"[:2000])
        rec = None
    finally:
        if slot:
            _ASSIST_SLOTS.release()
    # phase 3: one short write transaction
    try:
        run = AgentRunRow(
            id=new_id(),
            incident_id=incident_id,
            operator_id=settings.operator.operator_id,
            graph_name=GRAPH_NAME,
            trigger=TRIGGER,
            status="RUNNING",
            started_at=t_start,
        )
        session.add(run)
        session.flush()
        tracker = RunTracker(session, run)
        tracker.bind_incident(incident_id, incident_number)
        step = tracker.start_step(node_id, agent_name, input_summary)
        step.started_at = t_start  # honest duration: include the model call
        tracker.complete_step(
            step,
            status=result.status,
            output_summary=result.output_summary,
            rationale=result.rationale,
            tools=result.tools,
            confidence=result.confidence,
        )
        if rec is not None:
            session.add(_audit_row(settings, agent_name, incident_id, rec, justification))
        failed = result.status == FAILED
        tracker.finish_run(FAILED if failed else SUCCEEDED, error=result.rationale if failed else None)
        session.commit()
        response["run_id"] = run.id
    except Exception:  # noqa: BLE001 — DB trouble must not turn a template answer into a 500
        session.rollback()
        response["run_id"] = None
    return response


# --------------------------------------------------------------------------- analysis


def template_analysis(inc: IncidentRow) -> RootCauseAnalysis:
    """Deterministic analysis from the ticket's own fields (no model)."""
    cause = inc.root_cause_hypothesis or f"{inc.failure_domain} fault at {inc.site_id} (alarm {inc.alarm_code})"
    evidence = [f"alarm {inc.alarm_code}", f"failure domain {inc.failure_domain}"]
    if inc.child_sites_down:
        evidence.append(f"{inc.child_sites_down} child sites down")
    if inc.recurrence_count and inc.recurrence_count > 1:
        evidence.append(f"recurrence count {inc.recurrence_count}")
    return RootCauseAnalysis(
        summary=cause,
        hypotheses=[Hypothesis(cause=cause, likelihood="medium", evidence=evidence)],
        recommended_checks=list(CHECKS_BY_DOMAIN.get(inc.failure_domain, GENERIC_CHECKS)),
    )


def _validated_analysis(parsed: Any, mapping: dict[str, str]) -> RootCauseAnalysis | None:
    """Deterministic checks on the model output; None means "use the template"."""
    if not isinstance(parsed, RootCauseAnalysis):
        return None
    summary = restore_names(parsed.summary.strip(), mapping)
    if not summary or len(summary) > MAX_SUMMARY_CHARS:
        return None
    usable = [h for h in parsed.hypotheses if h.cause.strip()][:MAX_HYPOTHESES]  # the first five usable ones
    hypotheses = [
        Hypothesis(
            cause=restore_names(h.cause.strip(), mapping),
            likelihood=h.likelihood,
            evidence=[restore_names(e.strip(), mapping) for e in h.evidence if e.strip()],
        )
        for h in usable
    ]
    if not hypotheses:
        return None
    checks = [restore_names(c.strip(), mapping) for c in parsed.recommended_checks if c.strip()][:MAX_CHECKS]
    return RootCauseAnalysis(summary=summary, hypotheses=hypotheses, recommended_checks=checks)


def analyse_incident(session: Session, settings: AppSettings, inc: IncidentRow) -> dict[str, Any]:
    """``POST /incidents/{id}/analysis``: root-cause hypotheses via fable → opus, else template."""

    def prepare(_: Session) -> AssistJob:
        payload, mapping = redact_incident(inc, inc.notes)
        return AssistJob(
            incident_id=inc.id,
            incident_number=inc.incident_number,
            priority=inc.priority,
            payload=payload,
            mapping=mapping,
            template={"analysis": template_analysis(inc).model_dump()},
        )

    def call(job: AssistJob, llm: Any) -> tuple[dict[str, Any], StepResult, LlmCallRecord | None]:
        if llm is None:
            return job.template_response(), _template_step("analysis (template)", "LLM assist off: deterministic template"), None
        parsed, rec = parse_structured(
            llm,
            model=MODEL_REASONING,
            system=ANALYSIS_SYSTEM,
            user=json.dumps(job.payload, default=str),
            output_model=RootCauseAnalysis,
            effort=ANALYSIS_EFFORT,
            max_tokens=ANALYSIS_MAX_TOKENS,
            timeout=reasoning_timeout_s(),
        )
        analysis = _validated_or_none(lambda: _validated_analysis(parsed, job.mapping), rec)
        if analysis is None:
            reason = _mark_unusable(rec, "output failed validation (empty summary / too long / no usable hypothesis)")
            return job.template_response(), _llm_step(rec, "analysis (template fallback)", f"LLM unusable ({reason}); template used"), rec
        response = {"incident_id": job.incident_id, "source": "llm", "model": rec.model_used, "analysis": analysis.model_dump()}
        return response, _llm_step(rec, f"analysis ({rec.model_used})", "Draft analysis for analyst review; nothing changed on the ticket"), rec

    return run_assist(
        session,
        settings,
        agent_name=ANALYSIS_AGENT,
        node_id=ANALYSIS_NODE,
        incident_id=inc.id,
        incident_number=inc.incident_number,
        input_summary=f"{inc.incident_number} {inc.failure_domain}/{inc.alarm_code}",
        justification="root-cause analysis draft to speed restoration",
        prepare=prepare,
        call=call,
        empty_answer={
            "analysis": {
                "summary": f"Analysis unavailable for {inc.incident_number}; review the ticket manually",
                "hypotheses": [],
                "recommended_checks": list(GENERIC_CHECKS),
            }
        },
    )


# --------------------------------------------------------------------------- exec brief


def _validated_brief(parsed: Any, mapping: dict[str, str], incident_number: str, priority: str) -> str | None:
    if not isinstance(parsed, ExecBriefDraft):
        return None
    body = restore_names(parsed.body.strip(), mapping)
    if not body or len(body) > MAX_BRIEF_CHARS:
        return None
    if incident_number not in body or priority not in body:
        return None
    return body


def draft_exec_brief(session: Session, settings: AppSettings, inc: IncidentRow) -> dict[str, Any]:
    """``POST /incidents/{id}/brief/draft``: brief text via opus, else the pipeline template.

    Returns text only; never inserts an ``IncidentBriefRow``.
    """

    def prepare(_: Session) -> AssistJob:
        payload, mapping = redact_incident(inc, inc.notes)
        return AssistJob(
            incident_id=inc.id,
            incident_number=inc.incident_number,
            priority=inc.priority,
            payload=payload,
            mapping=mapping,
            template={"body": compose_brief(settings.operator, inc)},
        )

    def call(job: AssistJob, llm: Any) -> tuple[dict[str, Any], StepResult, LlmCallRecord | None]:
        if llm is None:
            return job.template_response(), _template_step("brief draft (template)", "LLM assist off: deterministic template"), None
        parsed, rec = parse_structured(
            llm,
            model=MODEL_DRAFTING,
            system=BRIEF_SYSTEM,
            user=json.dumps(job.payload, default=str),
            output_model=ExecBriefDraft,
            effort=BRIEF_EFFORT,
            max_tokens=BRIEF_MAX_TOKENS,
        )
        body = _validated_or_none(lambda: _validated_brief(parsed, job.mapping, job.incident_number, job.priority), rec)
        if body is None:
            reason = _mark_unusable(rec, "output failed validation (length / INC number / priority)")
            return job.template_response(), _llm_step(rec, "brief draft (template fallback)", f"LLM unusable ({reason}); template used"), rec
        response = {"incident_id": job.incident_id, "source": "llm", "model": rec.model_used, "body": body}
        return response, _llm_step(rec, f"brief draft ({rec.model_used})", "Draft brief for review; not published"), rec

    return run_assist(
        session,
        settings,
        agent_name=BRIEF_AGENT,
        node_id=BRIEF_NODE,
        incident_id=inc.id,
        incident_number=inc.incident_number,
        input_summary=f"{inc.incident_number} {inc.priority}",
        justification="executive brief draft to reduce calls into the NOC",
        prepare=prepare,
        call=call,
        empty_answer={
            "body": f"{inc.incident_number} ({inc.priority}): executive brief unavailable; "
            "please use the stored brief or contact the shift supervisor."
        },
    )
