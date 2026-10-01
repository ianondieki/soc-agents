"""Post-incident reviews and problem management (spec §7.7, §5.3.18; ``PIR_ENABLED=false``).

Everything in this module is either a pure function over already-loaded rows or a short
read/write against the session it is handed. No network, no model call: the optional LLM
draft leaves through the outbox (:func:`queue_llm_draft`), never from inside a request or
a transaction.

Four ideas carry the lane.

**Blamelessness is structural, not cultural.** §7.7 is blunt about why: "Blamelessness is
structural or the PIR feeds the scorecard and engineers write defensive notes." So the
validator is a gate on the way *in* (:func:`blameless_violation`), not a review checklist,
and it reuses the DPA-2019 scrubber that already knows this operator's people — the same
``NameMap`` + ``scrub_text`` path ``services/validators.py`` uses — rather than growing a
second list of names to drift out of date. Part-matching, word boundaries and the
role-code rule come along for free, which is what lets "RNIO restored the link" pass while
"Kevin restored the link" does not.

**The review is separated from the scorecard by construction.** §5.3.17 ("PIR content and
relationship complaints are never inputs") and §7.7.3 both demand it, and
``tests/unit/test_pir.py`` proves it by grepping the tree: nothing outside this lane may so
much as name the PIR tables. This module therefore computes its own MTTA/MTTR rather than
importing ``services/scorecard.py``. That is deliberate and it is the cheap side of the
trade: the arrow must point one way only, and a shared helper is exactly how the vendor
KPI path would end up with a PIR import in it six months from now. The consequence is
stated in :func:`compute_metrics` — the PIR number is a narrative aid, the scorecard number
is the contractual one, and where they can differ is written down.

**A missing review must be visible.** A CANCELLED incident gets a ``NOT_REQUIRED`` row, not
nothing at all, so "no review needed" and "nobody looked" are different states on the
wallboard ("no postmortem left unreviewed", https://sre.google/workbook/postmortem-culture/).

**The trigger is recorded, not inferred later.** ``opened_reason`` says which rule of the
§5.3.18 matrix fired, because the first thing anyone asks about an auto-opened review is
why it exists.

Flag: ``PIR_ENABLED``, default **false**. The job card carries ``default_enabled=False`` and
:func:`auto_open` re-checks the flag itself, so wiring the card into the scheduler is safe
before the operator decides to turn the lane on.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime, timedelta
from typing import Any, Iterable

from sqlalchemy import Table, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from noc_agents.agents.recurrence import problem_signature
# The role vocabulary, from the module that owns it. ``api/auth.py`` imports nothing from
# ``services``, so this is a leaf import and not a cycle; duplicating the nine role names
# here is how "duty_manager" would eventually pass as a person's name.
from noc_agents.api.auth import ROLES
from noc_agents.config import AppSettings
from noc_agents.db.models import (
    AgentRunRow,
    AgentRunStepRow,
    Base,
    BroadcastRow,
    ExternalSignalRow,
    HitlTaskRow,
    IncidentRow,
    ProblemRow,
    WorkNoteRow,
    utcnow,
)
from noc_agents.db.models_pir import PirActionItemRow, PostIncidentReviewRow
from noc_agents.llm.redaction import redact_incident
from noc_agents.orchestrator import outbox
from noc_agents.realtime.commit_hook import buffer_event
from noc_agents.realtime.hub import RealtimeEvent
from noc_agents.scheduler import JobCard, JobResult
from noc_agents.services.clock import z_utc
from noc_agents.services.validators import personal_data_violations

log = logging.getLogger("noc_agents.services.pir")

__all__ = [
    "ACTION_PRIORITIES",
    "ACTION_STATUSES",
    "ACTION_TYPES",
    "BLAMELESS_MESSAGE",
    "ENABLED_ENV",
    "OPENED_REASONS",
    "PIR_JOB",
    "PIR_STATUSES",
    "ROLE_TOKENS",
    "TERMINAL_ACTION_STATUSES",
    "assemble_timeline",
    "auto_open",
    "awaiting_review_count",
    "blameless_violation",
    "compute_metrics",
    "impact_block",
    "is_named_reviewer",
    "is_role_token",
    "known_error_for_incident",
    "known_error_note",
    "open_pir",
    "person_names_for_incident",
    "pir_enabled",
    "publish_blockers",
    "queue_llm_draft",
    "transition_action",
    "trigger_reason",
]


# --------------------------------------------------------------------------- the flag

ENABLED_ENV = "PIR_ENABLED"
_TRUE = {"1", "true", "yes", "on"}


def pir_enabled() -> bool:
    """``PIR_ENABLED`` — default **false**. Read at call time, never frozen at import.

    With it off, nothing in this lane runs: the job returns a no-op and every route in
    ``api/routers/pir.py`` 404s, so the system behaves exactly as it did before Phase 4.
    """
    return (os.getenv(ENABLED_ENV) or "").strip().lower() in _TRUE


# --------------------------------------------------------------------- the vocabularies

DRAFT, IN_REVIEW, PUBLISHED, NOT_REQUIRED = "DRAFT", "IN_REVIEW", "PUBLISHED", "NOT_REQUIRED"
PIR_STATUSES: tuple[str, ...] = (DRAFT, IN_REVIEW, PUBLISHED, NOT_REQUIRED)

REASON_P1_P2 = "P1_P2"
REASON_HUB_CORE = "HUB_CORE"
REASON_SLA_BREACH = "SLA_BREACH"
REASON_PROBLEM_LINKED = "PROBLEM_LINKED"
REASON_RUN_FAILED = "RUN_FAILED"
REASON_MANUAL = "MANUAL"
OPENED_REASONS: tuple[str, ...] = (
    REASON_P1_P2,
    REASON_HUB_CORE,
    REASON_SLA_BREACH,
    REASON_PROBLEM_LINKED,
    REASON_RUN_FAILED,
    REASON_MANUAL,
)

#: The SRE template's typed actions. The type is not decoration: a review whose every
#: action is "repair" has learned nothing, and the split is what makes that visible.
ACTION_TYPES: tuple[str, ...] = ("prevent", "mitigate", "detect", "repair", "investigate")
ACTION_PRIORITIES: tuple[str, ...] = ("P0", "P1", "P2", "P3")
ACTION_STATUSES: tuple[str, ...] = ("OPEN", "IN_PROGRESS", "DONE", "WONT_DO")
#: The two statuses that mean no more work will be done — whether it was finished or
#: abandoned. ``WONT_DO`` is as terminal as ``DONE`` and deliberately so: an action the
#: operator has decided against is a *recorded* decision, not a backlog item that quietly
#: rots, and the review's follow-up rate has to count it as closed to be honest.
TERMINAL_ACTION_STATUSES: frozenset[str] = frozenset({"DONE", "WONT_DO"})
#: An action item at one of these priorities is what §7.7.2 requires before a
#: user-affecting outage's review may be published.
BLOCKING_ACTION_PRIORITIES: frozenset[str] = frozenset({"P0", "P1"})


# ------------------------------------------------------------------ blameless validator

#: The exact text §5.3.18 / §7.7.3 require on the 422. It names the alternative rather than
#: only refusing, because "no names" without "then what?" is how a reviewer ends up writing
#: "the engineer" — which is the same blame with a thinner disguise.
BLAMELESS_MESSAGE = "describe what the system allowed, not who did it — use RNIO / FE / MSP_POWER"

#: Role tokens the operator's own vocabulary uses, which must never be treated as person
#: names however they arrive. Without this a site whose ``rnio_name`` is literally "RNIO"
#: would make the validator reject the very wording its own error message recommends —
#: ``NameMap`` registers any name part of four letters or more as an alias, and "RNIO" is
#: four letters with no digit and no hyphen, so its role-code rule does not catch it.
ROLE_TOKENS: frozenset[str] = frozenset(
    {
        "RNIO",
        "FE",
        "NOC",
        "MSP",
        "MSP_POWER",
        "MSP_TX",
        "MSP_RADIO",
        "VENDOR",
        "AGENT",
        "SYSTEM",
        "SUPERVISOR",
        "DUTY_MANAGER",
        "FIELD_ENGINEER",
        "UNASSIGNED",
    }
)


#: Labels that identify a *chair*, not a person. The demo role switcher's default display
#: name is "NOC Analyst", and every authenticated principal falls back to its role name when
#: the claim carries none — so "non-empty" is not the same question as "named". Normalised
#: to upper case with spaces folded to underscores before the comparison.
_GENERIC_REVIEWER_LABELS: frozenset[str] = frozenset(r.upper() for r in ROLES) | ROLE_TOKENS


def is_named_reviewer(name: str | None) -> bool:
    """True when ``name`` identifies a person rather than a chair.

    §5.3.18 makes publishing an A2 act: "a *named human* still publishes". A review signed
    "NOC Analyst" or "shift_supervisor" is signed by whoever happened to be sitting there,
    which is the same as unsigned — and it is the value the demo role switcher supplies by
    default, so without this check the rule would be unreachable in exactly the environment
    where it is easiest to skip.
    """
    cleaned = " ".join((name or "").split())
    if not cleaned:
        return False
    return cleaned.upper().replace(" ", "_") not in _GENERIC_REVIEWER_LABELS


#: The shape of a role token: upper case, no spaces, letters/digits/underscore/hyphen/dot.
#: ``FE_CENTRAL``, ``MSP_POWER``, ``RNIO-NBI-E`` pass; "Kevin Ochieng" and "kevin" do not.
_ROLE_TOKEN_RE = re.compile(r"^[A-Z0-9_\-.]+$")


def is_role_token(value: str | None) -> bool:
    """True when ``value`` has the shape of a role token rather than a person's name.

    A *structural* check, used where there is no incident to source real names from — the
    ``problems`` known-error owner, which outlives every incident attached to it. It cannot
    tell a role token from a name typed in block capitals ("KEVIN"), and it is not trying
    to: the point is that the ordinary way of writing a name ("Kevin Ochieng") does not get
    through, so the field keeps meaning "which team owns the permanent fix" (§7.7.6).
    """
    cleaned = (value or "").strip()
    return bool(cleaned) and bool(_ROLE_TOKEN_RE.match(cleaned))


def person_names_for_incident(session: Session, inc: IncidentRow) -> list[str]:
    """Every person this incident actually names, for the blameless validator to reject.

    The point of sourcing them from the incident rather than from a static list is that
    the validator then knows *this* outage's cast: the assignee, the field engineer, the
    RNIO, whoever wrote a work note, whoever asserted the restore, whoever claimed or
    resolved a HITL task. Those are exactly the people a defensive reviewer would name.

    ``msp_name`` / ``responsible_msp`` / ``radio_oem`` are excluded on purpose: they are
    companies, and "EGYPRO could not reach the site" is a fact about a vendor, not blame
    aimed at a person — ``llm/redaction.py`` draws the same line with ``COMPANY_FIELDS``.
    Role tokens (:data:`ROLE_TOKENS`) are excluded for the reason given there.
    """
    candidates: list[str] = [getattr(inc, field, None) for field in ("assignee_name", "fe_name", "rnio_name")]
    candidates.append(getattr(inc, "restored_by", None))
    candidates.extend(session.scalars(select(WorkNoteRow.author).where(WorkNoteRow.incident_id == inc.id)).all())
    for column in (HitlTaskRow.claimed_by, HitlTaskRow.resolved_by):
        candidates.extend(session.scalars(select(column).where(HitlTaskRow.incident_id == inc.id)).all())

    seen: dict[str, None] = {}  # dict, not set: keeps the order stable for tests and logs
    for raw in candidates:
        name = (raw or "").strip()
        if not name or name.upper() in ROLE_TOKENS:
            continue
        seen.setdefault(name, None)
    return list(seen)


def blameless_violation(text: str | None, names: Iterable[str]) -> bool:
    """True when ``text`` names one of ``names`` — reuse, not a second implementation.

    Delegates to ``services/validators.personal_data_violations``, which is itself built on
    ``llm/redaction.NameMap`` + ``scrub_text``. That buys three behaviours this validator
    would otherwise have to re-derive and get subtly wrong: a registered full name is also
    matched by its parts of four letters or more ("Kevin" for "Kevin Ochieng"), matching is
    whole-word and case-insensitive (so "Ann" cannot eat "Announcement"), and role-code
    shaped values (``FE-MTK-01``) are matched whole only.

    Only the ``personal_data_name`` finding is consulted. An MSISDN or an e-mail address in
    a root cause is a different wrong thing, caught by the channel validators on any path
    that would transmit the text; the 422 here says one thing and says it precisely.

    The names found are deliberately never returned, logged or put in the response: the
    error message is fixed, and echoing "your root cause names Kevin Ochieng" back through
    an API response is the disclosure this lane is meant to avoid.
    """
    body = (text or "").strip()
    if not body:
        return False
    findings = personal_data_violations(body, forbidden_names=[n for n in names if n])
    return any(f.code == "personal_data_name" for f in findings)


# ---------------------------------------------------------------------- trigger matrix

_TERMINAL_FOR_REVIEW: tuple[str, ...] = ("RESTORED", "CLOSED")
_MAJOR_SITE_TYPES: frozenset[str] = frozenset({"HUB", "CORE"})


def trigger_reason(session: Session, inc: IncidentRow) -> str | None:
    """Which §5.3.18 rule says this incident needs a review, or ``None`` for "it does not".

    First match wins and the order is deliberate: the broadest, most-quoted rule (P1/P2)
    is reported in preference to the narrower ones, because ``opened_reason`` is read as
    "why a human has to spend an hour on this" and "it was a P1" is the answer that needs
    no further explanation. The database query (a failed lifecycle run) is last so the
    common cases never pay for it.

    The negative case is the one the acceptance criteria pin: a P4 at an ordinary site,
    restored inside its SLA, with no problem record and no failed run, returns ``None`` —
    the floor is not asked to write a postmortem for a two-hour rural BTS outage.
    """
    if (inc.priority or "").upper() in ("P1", "P2"):
        return REASON_P1_P2
    if (inc.site_type or "").upper() in _MAJOR_SITE_TYPES:
        return REASON_HUB_CORE
    if inc.restored_at and inc.sla_restore_due and inc.restored_at > inc.sla_restore_due:
        return REASON_SLA_BREACH
    if inc.problem_id:
        return REASON_PROBLEM_LINKED
    failed_run = session.scalar(
        select(AgentRunRow.id).where(AgentRunRow.incident_id == inc.id, AgentRunRow.status == "FAILED").limit(1)
    )
    if failed_run:
        return REASON_RUN_FAILED
    return None


# -------------------------------------------------------------------------- the timeline

#: One timeline entry quotes its source; it does not reproduce a 4 kB vendor essay. The
#: full text stays where it was written and the review links to the incident.
MAX_DETAIL_CHARS = 400
#: Upper bound on entries so one chatty incident cannot produce a 2 MB ``timeline_json``.
MAX_TIMELINE_ENTRIES = 300


def _clip(value: str | None) -> str:
    text = " ".join((value or "").split())
    return text if len(text) <= MAX_DETAIL_CHARS else text[: MAX_DETAIL_CHARS - 1] + "…"


def _entry(ts: datetime | None, kind: str, title: str, detail: str | None, actor_role: str | None) -> dict | None:
    """One ``{ts, kind, title, detail, actor_role}`` entry, or ``None`` when it has no time.

    An entry with no timestamp cannot be placed on a timeline, and inventing one (falling
    back to the incident's own ``created_at``, say) would put a never-sent broadcast at the
    start of the outage and let a reviewer read a causal order that never happened. Dropping
    it is the honest failure: the row is still in the database, and the review is not
    evidence of anything it cannot place.
    """
    if ts is None:
        return None
    return {
        "ts": z_utc(ts).isoformat(),
        "kind": kind,
        "title": _clip(title),
        "detail": _clip(detail),
        "actor_role": actor_role or "SYSTEM",
    }


def _optional_table(name: str, *required_columns: str) -> Table | None:
    """A table declared by another Phase 4 lane, if that lane has landed; else ``None``.

    §7.7.3 wants ``incident_clock_events`` and ``regulatory_notifications`` on the timeline,
    and both belong to the vendor/regulatory lanes. Looking them up in ``Base.metadata`` at
    call time — rather than importing those lanes' model modules — means this lane neither
    blocks on them nor breaks when they land, and no import arrow is created between two
    features that have nothing to do with each other. ``db/models_all.py`` imports every
    model module at start-up, so a landed table is always present here.

    ``required_columns`` is the price of that independence: another lane owns those column
    names, so a table that does not carry the ones this module reads is treated as absent
    rather than crashing the review. A missing timeline section is a gap; a 500 on every PIR
    because a neighbouring lane renamed a column is an outage.
    """
    table = Base.metadata.tables.get(name)
    if table is None:
        return None
    if any(column not in table.c for column in required_columns):
        log.warning("pir: table %s exists but lacks %s; leaving it off the timeline", name, list(required_columns))
        return None
    return table


def _clock_event_entries(session: Session, inc: IncidentRow) -> list[dict]:
    """Stop-clock (SCC) events, when the vendor lane's table exists."""
    table = _optional_table("incident_clock_events", "incident_id", "scc_code", "started_at")
    if table is None:
        return []
    cols = table.c
    rows = session.execute(select(table).where(cols.incident_id == inc.id)).mappings().all()
    entries: list[dict] = []
    for row in rows:
        reversed_at = row.get("reversed_at")
        detail = row.get("reason") or ""
        if reversed_at is not None:
            # A reversed SCC still belongs on the timeline: "we stopped the clock and were
            # later told we should not have" is exactly the kind of fact a review exists to
            # surface, and deleting it from the narrative is how it stops being discussed.
            detail = f"{detail} (REVERSED: {row.get('reversal_reason') or 'no reason recorded'})"
        entries.append(
            _entry(
                row.get("started_at"),
                "scc",
                f"stop-clock {row.get('scc_code')}",
                detail,
                row.get("opened_role"),
            )
        )
        if row.get("ended_at") is not None:
            entries.append(
                _entry(row.get("ended_at"), "scc", f"stop-clock {row.get('scc_code')} ended", None, row.get("opened_role"))
            )
    return [e for e in entries if e]


def _regulatory_entries(session: Session, inc: IncidentRow) -> list[dict]:
    """Regulatory notification clocks, when the regulatory lane's table exists."""
    table = _optional_table("regulatory_notifications", "incident_id", "kind", "clock_started_at")
    if table is None:
        return []
    cols = table.c
    rows = session.execute(select(table).where(cols.incident_id == inc.id)).mappings().all()
    entries = [
        _entry(
            row.get("clock_started_at"),
            "regulatory",
            f"{row.get('kind')} clock started",
            f"due {row.get('due_at')}, status {row.get('status')}",
            "COMPLIANCE",
        )
        for row in rows
    ]
    return [e for e in entries if e]


def _cap_identifier(row: ExternalSignalRow) -> str | None:
    """The CAP ``identifier`` a signal row carries, or ``None`` for anything that is not a CAP
    alert. One alert can hold several rows — one per region, and one per attribution span — and
    the PIR timeline counts them as the single alert KMD issued (review finding PIR-SPAN)."""
    if row.source != "KMD_CAP" or not row.derived_json:
        return None
    try:
        block = json.loads(row.derived_json)
    except ValueError:
        return None
    if not isinstance(block, dict) or block.get("kind") != "cap_alert":
        return None
    identifier = block.get("identifier")
    return str(identifier) if identifier else None


def assemble_timeline(session: Session, inc: IncidentRow) -> list[dict]:
    """``[{ts, kind, title, detail, actor_role}]`` for one incident, oldest first (§7.7.3).

    Sources: ``work_notes``, ``agent_run_steps``, ``incident_clock_events``, ``broadcasts``,
    ``hitl_tasks``, ``regulatory_notifications`` and the ``external_signals`` that were
    *active at* ``failure_time`` — the weather or planned-power row that was true when the
    site went down, not whatever the poller holds today, because the review's question is
    what the floor could have known at the time.

    The MSP's own free text rides in here as evidence and is not blameless-validated: the
    blameless rule governs the review's analysis (``root_causes`` /
    ``contributing_factors``), not the record of what a vendor actually wrote. Redacting a
    work note inside a postmortem would falsify the evidence it exists to preserve.
    """
    entries: list[dict | None] = []

    notes = session.scalars(select(WorkNoteRow).where(WorkNoteRow.incident_id == inc.id)).all()
    entries.extend(
        _entry(n.created_at, "work_note", f"{n.author_role or 'note'} note", n.body, n.author_role) for n in notes
    )

    steps = session.execute(
        select(AgentRunStepRow, AgentRunRow.graph_name)
        .join(AgentRunRow, AgentRunRow.id == AgentRunStepRow.run_id)
        .where(AgentRunRow.incident_id == inc.id)
    ).all()
    entries.extend(
        _entry(
            step.finished_at or step.started_at,
            "agent_step",
            f"{graph}/{step.node_name} {step.status}",
            step.output_summary or step.rationale,
            "AGENT",
        )
        for step, graph in steps
    )

    broadcasts = session.scalars(select(BroadcastRow).where(BroadcastRow.incident_id == inc.id)).all()
    # ``broadcasts`` carries no created_at, only ``sent_at``: a draft that was never sent has
    # no time and is dropped by _entry. That is a schema limitation, not a decision — a
    # created_at on that table would put "we drafted a customer SMS and held it" on the
    # timeline, which is worth having. Noted for whoever next opens db/models.py.
    entries.extend(
        _entry(b.sent_at, "broadcast", f"{b.channel} to {b.audience} ({b.status})", b.message, "NOC") for b in broadcasts
    )

    tasks = session.scalars(select(HitlTaskRow).where(HitlTaskRow.incident_id == inc.id)).all()
    entries.extend(
        _entry(
            t.created_at,
            "hitl",
            f"{t.task_type} raised",
            f"status {t.status}" + (f", resolved by {t.resolved_by}" if t.resolved_by else ""),
            "SUPERVISOR",
        )
        for t in tasks
    )

    at = inc.failure_time or inc.outage_start_at or inc.created_at
    if at is not None:
        # "Active at failure time" means the row's OWN span covered that instant. A row is never
        # evidence before it was fetched, and a KMD alert re-attributed to a region opens a new
        # span rather than reviving the old one (pollers/kmd_cap.reattribute_live), so a span
        # stored at 14:30 must not appear on a 12:15 timeline — it did, listing one alert twice
        # and saying the region was warned through the very gap in which it was not mapped to the
        # county (review finding PIR-SPAN). ``fetched_at <= at`` is the general form of that:
        # every source writes ``fetched_at`` when it learned the thing.
        signals = session.scalars(
            select(ExternalSignalRow).where(
                ExternalSignalRow.operator_id == inc.operator_id,
                ExternalSignalRow.fetched_at <= at,
                ExternalSignalRow.valid_until >= at,
                or_(ExternalSignalRow.valid_from.is_(None), ExternalSignalRow.valid_from <= at),
                or_(
                    ExternalSignalRow.region_code == inc.region_code,
                    ExternalSignalRow.site_id == inc.site_id,
                ),
            )
            .order_by(ExternalSignalRow.fetched_at)
        ).all()
        # One alert is one entry however many spans it has: the identifier is what KMD issued,
        # and a reader should see "KMD warned about this", not one line per attribution.
        seen_alerts: set[str] = set()
        for row in signals:
            identifier = _cap_identifier(row)
            if identifier is not None:
                if identifier in seen_alerts:
                    continue
                seen_alerts.add(identifier)
            entries.append(
                _entry(
                    row.fetched_at,
                    "signal",
                    f"{row.source} signal active at failure time",
                    row.derived_json or row.last_error,
                    "EXTERNAL",
                )
            )

    entries.extend(_clock_event_entries(session, inc))
    entries.extend(_regulatory_entries(session, inc))

    timeline = sorted((e for e in entries if e), key=lambda e: e["ts"])
    return timeline[:MAX_TIMELINE_ENTRIES]


# ----------------------------------------------------------------------------- metrics


def _minutes(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return round((end - start).total_seconds() / 60.0, 2)


def _scc_overlap_minutes(session: Session, inc: IncidentRow, window_start: datetime, window_end: datetime) -> float:
    """Σ overlap of un-reversed stop-clock intervals with ``[window_start, window_end]``.

    0.0 when the vendor lane's table does not exist yet, which makes ``adjusted_mttr`` equal
    ``mttr`` — the conservative answer: no deduction is claimed that cannot be evidenced.
    A reversed SCC (``reversed_at IS NOT NULL``) deducts nothing, matching §7.6.2.
    """
    table = _optional_table("incident_clock_events", "incident_id", "started_at", "reversed_at")
    if table is None:
        return 0.0
    cols = table.c
    rows = session.execute(
        select(table).where(cols.incident_id == inc.id, cols.reversed_at.is_(None))
    ).mappings().all()
    total = 0.0
    for row in rows:
        started = row.get("started_at")
        if started is None:
            continue
        ended = row.get("ended_at") or window_end  # still open: it runs to the end of the window
        lo, hi = max(started, window_start), min(ended, window_end)
        if hi > lo:
            total += (hi - lo).total_seconds() / 60.0
    return round(total, 2)


def compute_metrics(session: Session, inc: IncidentRow) -> dict[str, float | None]:
    """``{mtta_minutes, mttr_minutes, adjusted_mttr_minutes}`` for one incident.

    **MTTA** follows §7.6.2's definition — ``first_vendor_note_at − escalated_at`` — applied
    to this one incident. When the incident was never escalated to a vendor (the NOC fixed
    it in house, or the ticket never left the floor) that pair does not exist, and the
    fallback is the NOC's own clock: ``acknowledged_at − failure_time``. Those are two
    different measurements and the fallback is *not* interchangeable with the contractual
    number, which is why it is written down here rather than hidden: a PIR MTTA must never
    be quoted in a vendor conversation without checking which clock produced it.

    **MTTR** is ``restored_at − failure_time``; **adjusted MTTR** subtracts the un-reversed
    stop-clock minutes inside that window.

    This function deliberately does not import ``services/scorecard.py``. The scorecard must
    never depend on the PIR tables (§5.3.17, proved by the grep test), and a shared helper
    is the most likely way that arrow would eventually get reversed. The cost is a second
    implementation of two subtractions; the benefit is that the dependency cannot rot.
    """
    failure = inc.failure_time or inc.outage_start_at or inc.created_at
    mtta = _minutes(inc.escalated_at, inc.first_vendor_note_at)
    if mtta is None:
        mtta = _minutes(failure, inc.acknowledged_at)
    mttr = _minutes(failure, inc.restored_at)
    adjusted = mttr
    if mttr is not None and failure is not None and inc.restored_at is not None:
        adjusted = round(max(0.0, mttr - _scc_overlap_minutes(session, inc, failure, inc.restored_at)), 2)
    return {"mtta_minutes": mtta, "mttr_minutes": mttr, "adjusted_mttr_minutes": adjusted}


def impact_block(inc: IncidentRow, metrics: dict[str, float | None]) -> dict[str, Any]:
    """The ``impact_json`` payload: ``{users_affected, duration_minutes, adjusted_duration_minutes, services, revenue_note}``.

    ``revenue_note`` is left empty for a human. Revenue impact is a commercial estimate that
    depends on ARPU, tariff mix and time of day; the NOC has none of those, and a number
    invented here would be quoted in a board pack within the week.
    """
    return {
        "users_affected": int(inc.users_affected or 0),
        "duration_minutes": metrics.get("mttr_minutes"),
        "adjusted_duration_minutes": metrics.get("adjusted_mttr_minutes"),
        "services": list(inc.services_impacted or []),
        "revenue_note": None,
    }


# ------------------------------------------------------------------------- opening a PIR


def _detection(inc: IncidentRow) -> tuple[str, datetime | None]:
    """How the outage was found, prefilled from what the incident row can actually prove.

    An incident with an alarm code arrived from the alarm feed; one without was typed in by
    a human (a customer complaint, a field call). Both are a *prefill* a reviewer corrects —
    the point is that the field starts with the truth the database holds rather than blank.
    """
    if (inc.alarm_code or "").strip():
        return "ALARM", inc.created_at
    return "MANUAL_REPORT", inc.created_at


def _prefill_root_causes(inc: IncidentRow, names: Iterable[str]) -> str | None:
    """The MSP's stated root cause, but only when it passes the blameless validator.

    Vendor free text regularly names the technician who attended. Copying it into
    ``root_causes`` would put a person's name into the one field this lane exists to keep
    clean, and it would arrive through a path the PATCH validator never sees. When the text
    does not pass, the field is left empty for a human to write — and the vendor's own words
    are still on the timeline as evidence, where naming a person is a fact rather than a
    finding.
    """
    stated = (inc.msp_root_cause or "").strip()
    if not stated or blameless_violation(stated, names):
        return None
    return stated


def open_pir(
    session: Session,
    inc: IncidentRow,
    *,
    reason: str,
    now: datetime | None = None,
) -> tuple[PostIncidentReviewRow, bool]:
    """Create the review for ``inc`` (or return the existing one). ``(row, created)``.

    Does not commit: the caller owns the transaction. The ``pir.opened`` event is buffered
    on the session and leaves only when that transaction commits — never ``hub.publish_sync``
    from in here, because an event announced inside a transaction that then rolls back tells
    the wallboard about a review that does not exist (the bug ``realtime/commit_hook.py``
    was written for).

    A CANCELLED incident gets ``NOT_REQUIRED`` rather than ``DRAFT``: a false alarm or a
    duplicate has nothing to review, but the row still records that the question was asked.
    """
    now = now or utcnow()
    existing = session.scalar(select(PostIncidentReviewRow).where(PostIncidentReviewRow.incident_id == inc.id))
    if existing is not None:
        return existing, False

    metrics = compute_metrics(session, inc)
    names = person_names_for_incident(session, inc)
    detection_method, detected_at = _detection(inc)
    status = NOT_REQUIRED if (inc.status or "").upper() == "CANCELLED" else DRAFT
    row = PostIncidentReviewRow(
        operator_id=inc.operator_id,
        incident_id=inc.id,
        status=status,
        opened_reason=reason,
        summary=(inc.resolution_summary or "").strip() or None,
        impact_json=json.dumps(impact_block(inc, metrics)),
        detection_method=detection_method,
        detected_at=detected_at,
        # The trigger is what set it off; the alarm and the ticket category are what the
        # system observed. Root causes stay separate and stay human-written.
        trigger=" / ".join(p for p in (inc.alarm_code, inc.tt_category_label) if (p or "").strip()) or None,
        root_causes=_prefill_root_causes(inc, names),
        contributing_factors=None,
        mtta_minutes=metrics["mtta_minutes"],
        mttr_minutes=metrics["mttr_minutes"],
        adjusted_mttr_minutes=metrics["adjusted_mttr_minutes"],
        timeline_json=json.dumps(assemble_timeline(session, inc)),
        ai_assisted=0,
        created_at=now,
        updated_at=now,
    )
    try:
        # A savepoint, so that losing the race against another opener (the job tick and a
        # manual open landing together) costs this one row and not the whole batch. The
        # UNIQUE on incident_id is what makes the earlier SELECT safe rather than merely
        # lucky; this is the branch that runs when it was not.
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        log.info("pir: %s already has a review (raced); keeping the existing one", inc.incident_number)
        winner = session.scalar(select(PostIncidentReviewRow).where(PostIncidentReviewRow.incident_id == inc.id))
        if winner is not None:
            return winner, False
        raise
    # Buffered after the savepoint succeeded: a savepoint rollback discards the whole event
    # buffer (commit_hook over-discards deliberately), so an event queued before the insert
    # would take unrelated events down with it.
    buffer_event(
        session,
        RealtimeEvent(
            type="pir.opened",
            operator_id=inc.operator_id,
            incident_id=inc.id,
            payload={
                "incident_number": inc.incident_number,
                "opened_reason": reason,
                "pir_id": row.id,
                "status": row.status,
            },
        ),
    )
    return row, True


# ------------------------------------------------------------------------- publish rules


def action_items(session: Session, pir: PostIncidentReviewRow) -> list[PirActionItemRow]:
    """This review's action items, oldest first.

    Scoped by construction: the caller has already resolved ``pir`` through
    ``_get_owned``, which applied the operator clause, so filtering on ``pir_id`` cannot
    reach another operator's rows (see the docstring on ``PirActionItemRow``).
    """
    return list(
        session.scalars(
            select(PirActionItemRow)
            .where(PirActionItemRow.pir_id == pir.id)
            .order_by(PirActionItemRow.created_at, PirActionItemRow.id)
        ).all()
    )


def awaiting_review_count(session: Session, *, operator_id: str) -> int:
    """Reviews written but not yet signed — the Wallboard's "PIRs awaiting review" counter.

    DRAFT **and** IN_REVIEW, because the number exists to answer "how much unfinished
    learning are we carrying?" and a review parked in review is exactly as unfinished as one
    still being written. ``NOT_REQUIRED`` is excluded: it is a decision, not a backlog item.

    Takes ``operator_id`` as an argument rather than reaching for ``api.deps``, so that
    ``main.py``'s metrics route can call it with the operator it already resolved and this
    module stays free of the API layer.
    """
    return int(
        session.scalar(
            select(func.count())
            .select_from(PostIncidentReviewRow)
            .where(
                PostIncidentReviewRow.operator_id == operator_id,
                PostIncidentReviewRow.status.in_((DRAFT, IN_REVIEW)),
            )
        )
        or 0
    )


def transition_action(action: PirActionItemRow, new_status: str, *, now: datetime | None = None) -> None:
    """Move an action item to ``new_status`` and keep ``closed_at`` truthful.

    ``closed_at`` is stamped when the item *becomes* terminal and cleared when it leaves
    the terminal set, so the column always answers "when did work on this stop?" and never
    "when was this row last touched":

    * OPEN/IN_PROGRESS → DONE/WONT_DO — stamp ``now``.
    * DONE/WONT_DO → OPEN/IN_PROGRESS — clear it. Something that is being worked again has
      no closing date, and leaving a stale one there is how a follow-up-rate report ends up
      counting an item as closed while an engineer is still on it.
    * DONE → WONT_DO (or the reverse) — leave the original stamp alone. Work stopped when
      it stopped; reclassifying *why* it stopped does not move that moment.
    * No change of status at all — leave it alone, so an edit to the description or the due
      date cannot silently re-date a closure.

    Mutates in place and does not commit: the caller owns the transaction. Pure enough to
    be tested without a session, which is the point of it living here rather than inline in
    the route.
    """
    old_status = (action.status or "").upper()
    new_status = (new_status or "").upper()
    if new_status == old_status:
        return
    was_terminal = old_status in TERMINAL_ACTION_STATUSES
    is_terminal = new_status in TERMINAL_ACTION_STATUSES
    action.status = new_status
    if is_terminal and not was_terminal:
        action.closed_at = now or utcnow()
    elif was_terminal and not is_terminal:
        action.closed_at = None


def publish_blockers(session: Session, pir: PostIncidentReviewRow, *, reviewer: str | None) -> tuple[str, ...]:
    """Everything standing between this review and PUBLISHED. Empty means publishable.

    §7.7.2's two hard rules, plus two the same reasoning demands:

    * **A named reviewer.** §5.3.18 makes publishing an A2 act. A postmortem nobody signed
      is a document nobody owns.
    * **≥ 1 P0/P1 action item when ``impact.users_affected > 0``.** Straight from the SRE
      workbook: a user-affecting outage that produced no urgent action produced no learning.
    * **Blameless text.** The PATCH route validates on the way in, but text can also arrive
      from the LLM draft path, so the gate is re-applied at the moment it becomes permanent.
    * **Not ``NOT_REQUIRED``.** A review that was ruled unnecessary cannot be published; if
      it turns out to be needed, it is moved to DRAFT first and written.

    The caller turns these into a 422 with the reasons listed. Returning them all at once
    rather than raising on the first is deliberate: a reviewer who has to discover three
    problems through three round-trips stops publishing reviews.
    """
    blockers: list[str] = []
    if pir.status == NOT_REQUIRED:
        blockers.append("a NOT_REQUIRED review cannot be published; move it to DRAFT first")
    if not is_named_reviewer(reviewer):
        blockers.append("a named reviewer is required (a role label such as 'NOC Analyst' is not a name)")
    impact = json.loads(pir.impact_json or "{}")
    try:
        users = int(impact.get("users_affected") or 0)
    except (TypeError, ValueError):
        users = 0
    if users > 0:
        priorities = {(a.priority or "").upper() for a in action_items(session, pir)}
        if not (priorities & BLOCKING_ACTION_PRIORITIES):
            blockers.append(
                "a user-affecting outage needs at least one P0 or P1 action item before the review can be published"
            )
    inc = session.get(IncidentRow, pir.incident_id)
    if inc is not None:
        names = person_names_for_incident(session, inc)
        if blameless_violation(pir.root_causes, names) or blameless_violation(pir.contributing_factors, names):
            blockers.append(BLAMELESS_MESSAGE)
    return tuple(blockers)


# -------------------------------------------------------------- known errors (ITIL PRB)


def known_error_for_incident(session: Session, inc: IncidentRow) -> dict[str, Any] | None:
    """The open known error matching this incident's signature, or ``None``.

    This is the read §7.7.3 asks ENRICH/RECURRENCE to make: once a published review has
    recorded a root cause and a workaround on the problem record, the *next* incident with
    the same ``site|domain`` signature surfaces that text instead of making the analyst on
    shift rediscover it at 03:00. That is the entire commercial value of problem management
    and until now the columns existed with nothing reading them.

    The signature comes from ``agents.recurrence.problem_signature`` — the same function
    RECURRENCE uses to open the problem — so a known error can only fail to match here if it
    would also have failed to match there (defect #34 aligned the two).
    """
    signature = problem_signature(inc.site_id, inc.failure_domain)
    problem = session.scalar(
        select(ProblemRow)
        .where(
            ProblemRow.operator_id == inc.operator_id,
            ProblemRow.signature == signature,
            ProblemRow.is_known_error == 1,
            ProblemRow.closed_at.is_(None),
        )
        .order_by(ProblemRow.known_error_since.desc(), ProblemRow.last_seen.desc())
    )
    if problem is None:
        return None
    return {
        "problem_id": problem.id,
        "problem_number": problem.problem_number,
        "signature": problem.signature,
        "root_cause": problem.root_cause,
        "workaround": problem.workaround,
        "permanent_fix_plan": problem.permanent_fix_plan,
        "owner_token": problem.owner_token,
        "known_error_since": problem.known_error_since,
        "occurrence_count": problem.occurrence_count,
        "target_date": problem.target_date,
    }


def known_error_note(known_error: dict[str, Any]) -> str:
    """The text ENRICH puts in front of the analyst. Role tokens only — never a name."""
    lines = [f"KNOWN ERROR {known_error['problem_number']} — this site has seen this before."]
    if known_error.get("root_cause"):
        lines.append(f"Root cause: {known_error['root_cause']}")
    if known_error.get("workaround"):
        lines.append(f"Workaround: {known_error['workaround']}")
    if known_error.get("permanent_fix_plan"):
        owner = known_error.get("owner_token") or "unassigned"
        lines.append(f"Permanent fix ({owner}): {known_error['permanent_fix_plan']}")
    return "\n".join(lines)


# ------------------------------------------------------------------ the optional LLM draft


LLM_DRAFT_MODEL = "claude-opus-5"  # §5.3.18: opus or local, never Fable — PIR text quotes free-text notes
LLM_DRAFT_PURPOSE = "pir_draft"


def queue_llm_draft(session: Session, pir: PostIncidentReviewRow, inc: IncidentRow) -> tuple[Any, bool]:
    """Queue the optional model draft as an outbox ``LLM_CALL``. ``(row, queued_now)``.

    Three rules, all of them load-bearing:

    * **Out of band.** The call is a row, not a request: §7.7.3 says the draft goes through
      the outbox, and ``orchestrator/outbox.py`` explains why nothing may hold a SQLite
      write lock across a model call that can take a minute.
    * **Redacted first.** The payload is built by ``llm/redaction.redact_incident``, so what
      is stored — and what would later be transmitted — carries ``<PERSON_n>`` tokens, not
      staff names. The token→name map it returns is **deliberately dropped here**: it is the
      re-identification key, and writing it into the outbox row beside the pseudonymised
      payload would undo the pseudonymisation in the same table.
    * **``ai_assisted=1`` now, not when the text arrives.** The flag is a disclosure (§7.7.6),
      and a disclosure flag belongs on the conservative side: a review marked AI-assisted
      whose draft never came back over-declares, which costs nothing; one marked only on
      success under-declares the moment a crash lands between the two, which is the failure
      that matters. A named human still publishes either way.

    Idempotent per review: the key is the review id, so a double-click queues one draft.
    """
    notes = session.scalars(select(WorkNoteRow).where(WorkNoteRow.incident_id == inc.id)).all()
    payload, _token_to_name = redact_incident(inc, notes)
    key = f"pir-llm-draft:{pir.id}"
    before = session.scalar(select(outbox.OutboxRow.id).where(outbox.OutboxRow.idempotency_key == key))
    row = outbox.enqueue(
        session,
        kind="LLM_CALL",
        idempotency_key=key,
        payload={
            "operator_id": pir.operator_id,
            "purpose": LLM_DRAFT_PURPOSE,
            "model": LLM_DRAFT_MODEL,
            "pir_id": pir.id,
            "incident_number": inc.incident_number,
            # Text only, and DRAFT text at that: the model may propose summary, root causes
            # and lessons. It never writes status, reviewer, action items or metrics.
            "fields": ["summary", "root_causes", "went_well", "went_poorly", "got_lucky"],
            "redacted_incident": payload,
        },
        incident_id=inc.id,
        operator_id=pir.operator_id,
    )
    pir.ai_assisted = 1
    pir.updated_at = utcnow()
    return row, before is None


# ------------------------------------------------------------------------- the 5-min job

JOB_NAME = "pir_autoopen"
INTERVAL_S = 300  # every 5 min (§7.7.3)
AGENT = "PostIncidentReviewAgent"
GRAPH_NAME = "pir"

#: How far back a tick looks for newly restored/closed incidents. Far wider than the 5-minute
#: interval on purpose: the alternative — a watermark of "since the last run" — silently
#: skips every incident that closed while the scheduler was down, and a missing postmortem is
#: invisible by nature. A wide window is safe because UNIQUE(incident_id) makes re-opening
#: impossible, so the worst case is a slightly longer SELECT.
AUTO_OPEN_LOOKBACK = timedelta(hours=24)
#: Ceiling per tick, so a bulk close (or the first tick after a long outage of the scheduler
#: itself) cannot turn one job run into a thousand-row transaction. The rest are picked up by
#: the next tick, which is five minutes away.
AUTO_OPEN_LIMIT = 50


def auto_open(session: Session, settings: AppSettings, *, now: datetime | None = None) -> JobResult:
    """``jobs/pir.auto_open`` — open DRAFT reviews for incidents that just ended (§7.7.3).

    Does not commit: the scheduler's runner commits, which is also when the buffered
    ``pir.opened`` events reach the wallboard.
    """
    if not pir_enabled():
        # Re-checked here and not only on the job card, so that a card wired into the loop
        # with the flag unset is inert rather than merely un-scheduled.
        return JobResult(summary="PIR_ENABLED=false — no reviews opened", rationale="feature flag off")

    now = now or utcnow()
    cutoff = now - AUTO_OPEN_LOOKBACK
    operator_id = settings.operator.operator_id
    candidates = session.scalars(
        select(IncidentRow)
        .where(
            IncidentRow.operator_id == operator_id,
            IncidentRow.status.in_(_TERMINAL_FOR_REVIEW),
            or_(
                IncidentRow.restored_at >= cutoff,
                IncidentRow.closed_at >= cutoff,
                IncidentRow.updated_at >= cutoff,
            ),
            IncidentRow.id.notin_(select(PostIncidentReviewRow.incident_id)),
        )
        .order_by(IncidentRow.updated_at)
        .limit(AUTO_OPEN_LIMIT)
    ).all()

    opened: list[str] = []
    skipped = 0
    for inc in candidates:
        reason = trigger_reason(session, inc)
        if reason is None:
            skipped += 1
            continue
        _row, created = open_pir(session, inc, reason=reason, now=now)
        if created:
            opened.append(f"{inc.incident_number}:{reason}")
    return JobResult(
        summary=f"opened={len(opened)} considered={len(candidates)} below_threshold={skipped}",
        rationale="; ".join(opened) if opened else "no incident met a §5.3.18 trigger rule",
        tools=({"name": "pir_auto_open", "ok": True, "latency_ms": 0},),
    )


#: The scheduler card (§4.4 roster). NOT registered in ``scheduler/loop.SCHEDULED_JOBS`` by
#: this lane — that file belongs to integration; adding ``services.pir.PIR_JOB`` to the tuple
#: is the whole change. ``default_enabled=False`` so ``/scheduler/status`` reports the job as
#: off while ``PIR_ENABLED`` is unset, rather than claiming it is enabled and producing nothing.
PIR_JOB = JobCard(
    JOB_NAME,
    INTERVAL_S,
    auto_open,
    ENABLED_ENV,
    AGENT,
    GRAPH_NAME,
    max_seconds=60,
    default_enabled=False,
)


# --------------------------------------------------------------------------- serializing


def pir_out(pir: PostIncidentReviewRow) -> dict[str, Any]:
    """One review as the API returns it. Timestamps carry an explicit ``Z`` (§7.0.6)."""
    return {
        "id": pir.id,
        "incident_id": pir.incident_id,
        "status": pir.status,
        "opened_reason": pir.opened_reason,
        "summary": pir.summary,
        "impact": json.loads(pir.impact_json or "{}"),
        "detection_method": pir.detection_method,
        "detected_at": z_utc(pir.detected_at),
        "trigger": pir.trigger,
        "root_causes": pir.root_causes,
        "contributing_factors": pir.contributing_factors,
        "mtta_minutes": pir.mtta_minutes,
        "mttr_minutes": pir.mttr_minutes,
        "adjusted_mttr_minutes": pir.adjusted_mttr_minutes,
        "timeline": json.loads(pir.timeline_json or "[]"),
        "went_well": pir.went_well,
        "went_poorly": pir.went_poorly,
        "got_lucky": pir.got_lucky,
        "ai_assisted": int(pir.ai_assisted or 0),
        "reviewer": pir.reviewer,
        "reviewed_at": z_utc(pir.reviewed_at),
        "published_at": z_utc(pir.published_at),
        "created_at": z_utc(pir.created_at),
        "updated_at": z_utc(pir.updated_at),
    }


def action_out(action: PirActionItemRow) -> dict[str, Any]:
    """One action item as the API returns it. ``due_date`` is a date, so it needs no Z."""
    return {
        "id": action.id,
        "pir_id": action.pir_id,
        "type": action.type,
        "priority": action.priority,
        "description": action.description,
        "owner_token": action.owner_token,
        "due_date": action.due_date.isoformat() if isinstance(action.due_date, date) else action.due_date,
        "status": action.status,
        "problem_id": action.problem_id,
        "tracking_ref": action.tracking_ref,
        "created_at": z_utc(action.created_at),
        "closed_at": z_utc(action.closed_at),
    }
