"""On-demand assist runs: redact → (LLM or template) → one short write transaction.

Three phases, no DB work in the middle (SQLite has one writer; a model call can take
tens of seconds and must never hold the write lock):

1. ``prepare(session)``: reads only; builds a plain-Python job (redacted payload +
   deterministic template answer). The session is rolled back afterwards.
2. ``call(job, llm)``: network only, never sees the session. The template fallback
   happens INSIDE ``call``, so a model failure needs no DB cleanup.
3. One write transaction: the tracked ``llm_assist`` run, its step, an audit row per LLM
   attempt (DPA reg 41(2): date/time, recipient, justification, data description) and —
   since CONFORMANCE A-15 — the ``llm_calls`` row for that attempt.

CONFORMANCE A-15: this path used to write the audit row and nothing else, so the tokens,
the cost and the fallback outcome of the two routes most likely to be pressed repeatedly
were invisible. ``llm.client.spend_gate`` sums ``llm_calls.est_cost_usd``, so a path that
writes no row can consult the ceiling but can never move it: ``LLM_MONTHLY_BUDGET_USD``
was unenforceable here, and M11's template-fallback rate (``llm_calls.fallback_reason``)
had no denominator either. Phase 3 now writes exactly one ``llm_calls`` row per hosted
call attempt, through the same ``llm/port.record_llm_call`` the outbox ``LLM_CALL``
transmitter and ``services/contracts`` use — one writer, one shape, one price table. It is
in the SAME transaction as the audit row because the two are halves of one engineering
record: a register that says a call happened and a spend table that does not, or the
reverse, is worse than neither. The reg 41(2) transfer record is not in this transaction —
it was committed before the bytes left (see ``_record_hosted_transfer``).

No call means no row, exactly as in the other two writers: a run turned away by the spend
gate, the paperwork gate, a saturated slot or ``LLM_ENABLED=false`` sent nothing, so it
must not appear in the spend table or in M11's denominator. ``rec is not None`` is that
condition and nothing else writes a row here, so nothing is counted twice — note that one
``parse_structured`` call covering two model attempts (fable, then the opus fallback)
yields ONE record and therefore ONE row, with ``fallback_used=1``.

Between 1 and 2 sits the one exception to "no DB work in the middle": when the call is
really going to a hosted provider, the reg 41(2) transfer record (spec §2 G8, §9.2,
``services/external_calls.record_transfer``) is written and COMMITTED before the bytes
leave, exactly as ``contracts``, the complaint classifier and the outbox LLM_CALL
transmitter do. That is a short write, committed before the model call, so the write lock
is still never held across the network. A refusal from the paperwork gate (no DPIA/TIA ref
on file outside ``NOC_ENV=demo``) means the model is not called at all: the route answers
with its deterministic template, which is the same answer it gives with the LLM off. The
spend gate (``llm.client.spend_gate``: the provider's spend circuit and our own
``LLM_MONTHLY_BUDGET_USD`` ceiling) is asked before that record, and an answer from it
likewise means no record and no call.

Every route here is read-only for incident/brief tables; the only rows written are the
run, its step and the audit row. The LLM never changes priority, assignment or status.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy.orm import Session

from noc_agents.config import AppSettings
from noc_agents.db.models import AgentRunRow, AuditRow, IncidentRow, new_id, utcnow
from noc_agents.graph.instrumentation import RunTracker
from noc_agents.llm.client import MODEL_DRAFTING, MODEL_REASONING, get_llm, reasoning_timeout_s, spend_gate
from noc_agents.llm.outputs import ExecBriefDraft, Hypothesis, RootCauseAnalysis
from noc_agents.llm.port import PROVIDER_ANTHROPIC, record_llm_call
from noc_agents.llm.redaction import redact_incident, restore_names
from noc_agents.llm.structured import LlmCallRecord, parse_structured
from noc_agents.orchestrator.contract import FAILED, SUCCEEDED, StepResult
from noc_agents.services.composition import compose_brief
from noc_agents.services.external_calls import TransferPaperworkMissing, record_transfer

log = logging.getLogger("noc_agents.llm.assist")

GRAPH_NAME = "llm_assist"
TRIGGER = "ON_DEMAND"
ANALYSIS_NODE = "RCA"
ANALYSIS_AGENT = "TicketingAgent"
BRIEF_NODE = "BRIEF_DRAFT"
BRIEF_AGENT = "ExecutiveBriefingAgent"

# ``llm_calls.purpose``. M11 is measured "per assist function", so the two routes must be
# distinguishable by a GROUP BY without joining anything.
ANALYSIS_PURPOSE = "incident_analysis"
BRIEF_PURPOSE = "exec_brief_draft"

# ``llm_calls.fallback_reason`` for a call that WAS made and whose answer was not used. A
# closed vocabulary on purpose: M11 is a rate, so the column has to group. The detail (which
# validator, which exception) is already in the ``llm.call`` audit row's payload, which is the
# right place for it — ``rec.error`` may carry an SDK message and must not be copied into a
# column the spend/fallback reports read (§9.5).
FALLBACK_REASON_OUTPUT_INVALID = "output_invalid"  # the call answered; our validators rejected it (M11)
FALLBACK_REASON_MODEL_REFUSED = "model_refused"  # stop_reason=refusal on every model tried
FALLBACK_REASON_NO_OUTPUT = "no_usable_output"  # error/timeout/no parse: the call produced nothing

# Shared between the message ``_mark_unusable`` records and the classifier that maps it to a
# fallback reason, so the two can never drift apart.
VALIDATION_FAILED = "output failed validation"
VALIDATION_CRASHED = "validation crashed"

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

# The actor role on the reg 41(2) transfer row. The assist step is an agent acting on an
# operator's request, and the outbox transmitter records its hosted calls the same way.
TRANSFER_ACTOR_ROLE = "AGENT"
TRANSFER_DATA_DESCRIPTION = (
    "Pseudonymised incident record and work notes: network and operational fields only, "
    "person names replaced by <PERSON_n> tokens, e-mail addresses and MSISDNs removed "
    "before the call (llm/redaction.py)."
)

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
        _mark_unusable(rec, f"{VALIDATION_CRASHED}: {type(exc).__name__}")  # class only: no model text in the audit row
        return None


def _mark_unusable(rec: LlmCallRecord, reason: str) -> str:
    """Record that the model's answer was NOT used, so the step tool entry, its confidence and
    the audit row agree with the template fallback. Keeps an earlier error (e.g. a timeout)."""
    rec.ok = False
    rec.error = rec.error or reason
    return rec.error


def fallback_reason(rec: LlmCallRecord, *, used: bool) -> str | None:
    """``llm_calls.fallback_reason`` for one attempted assist call; ``None`` when the draft was used.

    M11 counts "LLM-assisted drafts rejected by validators / attempted", so a draft our
    validators threw away is its own code and is never mixed with a call that came back empty:
    the first says the model answered and the answer was wrong, the second says there was no
    answer to judge. ``_mark_unusable`` keeps an EARLIER error, so a timeout followed by a
    validation miss still reads as ``no_usable_output`` — which is the truth about that call.
    """
    if used:
        return None
    if rec.refused:
        return FALLBACK_REASON_MODEL_REFUSED
    error = rec.error or ""
    if error.startswith(VALIDATION_FAILED) or error.startswith(VALIDATION_CRASHED):
        return FALLBACK_REASON_OUTPUT_INVALID
    return FALLBACK_REASON_NO_OUTPUT


def _record_hosted_transfer(
    session: Session, settings: AppSettings, *, agent_name: str, incident_id: str, justification: str
) -> tuple[str | None, str | None]:
    """Write and commit the reg 41(2) transfer record BEFORE the hosted model call.

    Returns ``(transfer_record_id, None)`` when the call may go ahead, or ``(None, reason)``
    when the §7.0.10 paperwork gate refused it: the caller then must not call the model.

    The recipient identity comes from ``orchestrator/outbox.llm_recipient_identity`` rather
    than a second rule here, so the register names the same recipient (and the same
    ``transfers.yaml`` key) whichever path made the call. Imported lazily: ``llm`` sits below
    ``orchestrator``, and this is only reached when a hosted call is about to happen, so the
    LLM-off path never loads the dispatcher. ``get_llm`` answers only for
    ``LLM_PROVIDER=anthropic``, so today this always resolves to the hosted Anthropic API;
    a local openai-compatible endpoint never reaches this function (no raw client, no call).

    The gate stays ON (``enforce_gate=True``): a hosted model provider is exactly the case
    §7.0.10 makes the TIA a gating artefact for. ``NOC_ENV=demo`` records ``DEMO-UNFILED``
    and lets the call through, as everywhere else.
    """
    from noc_agents.orchestrator.outbox import llm_recipient_identity  # lazy: see docstring

    recipient, country, residency = llm_recipient_identity()
    try:
        row = record_transfer(
            session,
            recipient=recipient,
            recipient_country=country,
            justification=f"{justification} (on-demand assist, {agent_name}); lawful basis and "
            "safeguards: the DPIA/TIA on file for this recipient",
            data_description=TRANSFER_DATA_DESCRIPTION,
            actor=agent_name,
            actor_role=TRANSFER_ACTOR_ROLE,
            incident_id=incident_id,
            residency=residency,
            settings=settings,
            enforce_gate=True,
        )
        transfer_id = row.id
        # Durable BEFORE the call, as in the outbox transmitter: a crash in the gap loses a
        # draft, never a record of data that left. Committing also ends the transaction, so
        # no write lock is held while the model thinks.
        session.commit()
        return transfer_id, None
    except TransferPaperworkMissing as exc:
        session.rollback()
        log.warning("assist: %s call refused by the transfer paperwork gate: %s", agent_name, exc)
        # Recipient key and missing ref names only: no incident data in the step rationale.
        return None, (
            f"transfer paperwork gate refused the hosted call "
            f"({', '.join(exc.missing)} not filed for '{exc.recipient_key}')"
        )


def _audit_row(
    settings: AppSettings,
    agent_name: str,
    incident_id: str,
    rec: LlmCallRecord,
    justification: str,
    transfer_record_id: str | None = None,
) -> AuditRow:
    payload = {
        "ts": utcnow().isoformat(),
        "recipient": RECIPIENT,
        "justification": justification,
        "data_description": DATA_DESCRIPTION,
        **rec.as_dict(),
        # Points at the external.call row written before the call, so the engineering record
        # and the legal one can be joined (the pairing contracts makes via llm_calls.audit_id).
        "transfer_record_id": transfer_record_id,
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
    purpose: str,
    prepare: Callable[[Session], AssistJob],
    call: Callable[[AssistJob, Any], tuple[dict[str, Any], StepResult, LlmCallRecord | None]],
    empty_answer: dict[str, Any],
) -> dict[str, Any]:
    """Run one tracked assist step. Always returns a response dict; never raises for LLM trouble.

    ``empty_answer`` is the route's answer key with a minimal safe value, built from plain
    fields only; it is used when ``prepare`` itself fails so the caller still gets the
    documented shape. ``purpose`` is the ``llm_calls.purpose`` for this route (A-15/M11).
    """
    t_start = utcnow()
    job: AssistJob | None = None
    rec: LlmCallRecord | None = None
    transfer_id: str | None = None
    slot = False
    try:
        job = prepare(session)  # phase 1: reads only; redaction happens here, before any call
        session.rollback()  # release any implicit transaction before the (slow) model call
        llm = get_llm(settings)
        if llm is not None:
            slot = _ASSIST_SLOTS.acquire(blocking=False)
        refusal: str | None = None
        if slot:
            # The DB spend gate first, as the outbox LLM_CALL transmitter and contracts do:
            # ``get_llm`` checks only the in-process spend circuit, not the monthly
            # LLM_MONTHLY_BUDGET_USD ceiling summed from ``llm_calls``. An answer here means
            # no call, so it must also mean no transfer record: nothing leaves the machine.
            gate = spend_gate(session, operator_id=settings.operator.operator_id)
            if gate:  # spend_cap | budget_exhausted: a ceiling is a decision, not an error
                session.rollback()  # end the read transaction; phase 3 opens its own
                refusal = f"spend gate open ({gate})"
            else:
                # Only a call that will really leave the machine needs the record, so this
                # sits after the busy check and the spend gate: a caller that gets the
                # template for want of a slot or of budget sent nothing and must not appear
                # in the register. A DB error in here propagates to the except below, which
                # also answers with the template: no record, no call.
                transfer_id, refusal = _record_hosted_transfer(
                    session, settings, agent_name=agent_name, incident_id=incident_id, justification=justification
                )
        send = llm if slot and refusal is None else None
        response, result, rec = call(job, send)  # phase 2: network only
        if llm is not None and not slot:
            result.rationale = f"assist busy ({MAX_CONCURRENT_ASSIST} calls in flight): deterministic template"
        elif refusal is not None:
            # call(job, None) says "LLM assist off"; that is not why, so say the real reason.
            result.rationale = f"{refusal}: deterministic template"
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
            # A record means a hosted call was really attempted (``send`` is the client only
            # when a slot was free, the spend gate said nothing and the transfer record was
            # committed), so exactly these runs owe both an audit row and an ``llm_calls`` row.
            audit = _audit_row(settings, agent_name, incident_id, rec, justification, transfer_id)
            session.add(audit)
            session.flush()  # give the audit row its id: it is the fallback citation below
            used_llm = response.get("source") == "llm"  # the model's answer survived validation
            record_llm_call(
                session,
                operator_id=settings.operator.operator_id,
                agent=agent_name,
                purpose=purpose,
                # ``get_llm`` builds the raw Anthropic client and nothing else; a local
                # openai-compatible endpoint never reaches this function (no client, no call).
                provider=PROVIDER_ANTHROPIC,
                rec=rec,
                # The reg 41(2) transfer record, as in ``services/contracts`` and the outbox
                # LLM_CALL transmitter, so one join answers "what left the country and what did
                # it cost" whichever path made the call. ``transfer_id`` is never None while
                # ``rec`` is not None; the audit row's own id is a belt-and-braces citation so a
                # NOT NULL column can never be the reason a spend row is lost.
                audit_id=transfer_id or audit.id,
                run_id=run.id,
                incident_id=incident_id,
                fallback_reason=fallback_reason(rec, used=used_llm),
                validated=used_llm,
            )
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
            reason = _mark_unusable(rec, f"{VALIDATION_FAILED} (empty summary / too long / no usable hypothesis)")
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
        purpose=ANALYSIS_PURPOSE,
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
            reason = _mark_unusable(rec, f"{VALIDATION_FAILED} (length / INC number / priority)")
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
        purpose=BRIEF_PURPOSE,
        prepare=prepare,
        call=call,
        empty_answer={
            "body": f"{inc.incident_number} ({inc.priority}): executive brief unavailable; "
            "please use the stored brief or contact the shift supervisor."
        },
    )
