"""Regulatory notification clocks, drafts and the approval gate (spec §5.3.20, §7.6) — Lane 4A.

``REGULATORY_ENABLED`` defaults to **false**. With it off nothing in this module runs: no
row is opened, no HITL card is raised, no ``regulatory.deadline`` event is published and the
sweep job reports itself off. The system behaves exactly as it does today.

THE ONE RULE THIS FILE EXISTS TO ENFORCE
----------------------------------------
Metric M10 is "100 % drafted; **0 auto-sent**". A notice reaching a regulator without a
named human approving it would be a serious incident in its own right, so the guarantee is
structural, not conventional, and it is layered:

1. :func:`release_notice` is the **only** function anywhere that creates an outbox row for a
   regulatory notice, and its first act is :func:`approval_of`, which refuses unless an
   ``APPROVE_REGULATORY_NOTICE`` ``HitlTaskRow`` exists, points at *this* notice, is
   ``APPROVED``, and was resolved by a named human who is not the raiser. There is no
   argument that bypasses it and no "force" flag.
2. :func:`request_approval` queues **nothing**. This is a deliberate departure from the
   shift-handover pattern (``services/handover.queue_handover``), which parks its mail in the
   outbox as ``HELD``. A HELD row is one generic ``release_held`` away from PENDING, and
   ``outbox.release_held`` releases by *incident* — so an unrelated broadcast approval on the
   same incident would promote the regulator's mail. For a regulator channel the right number
   of rows before approval is zero.
3. The row :func:`release_notice` does create carries ``requires_hitl=1`` with
   ``approved_by``/``approved_at`` copied from the task, so ``orchestrator.outbox.dispatch``
   independently refuses it (``REJECTED_UNAPPROVED``) if that approval is ever cleared.
4. ``regulatory_notifications`` has no ``APPROVED`` status for code to mistake for
   permission (see ``db/models_regulatory.py``).

``sent_at`` MEANS "THIS LEFT THE BUILDING"
------------------------------------------
Releasing a notice and transmitting it are two different events, separated by a commit and a
dispatcher pass, and the second one can fail terminally — most often because
``regulatory.recipients.CA`` is an empty list in the operator profile and
``services/notify.resolve_recipients`` refuses rather than falling back to the demo mailbox.
So :func:`release_notice` writes ``QUEUED`` and leaves ``sent_at`` NULL, and only
:func:`record_dispatch_outcome` — called from the dispatcher once the outbox row is terminal
— writes ``SENT``/``sent_at``, or ``SEND_FAILED`` with the reason. This table is the evidence
for M10 and for the Condition 9.2 24-hour obligation; "sent at 14:02" on a notice that never
left is a false assurance of a statutory duty, which is worse than a visible failure.
Lateness is judged on the *transmission*, not on the enqueue: see
``significance_json.dispatch``.

THE CLOCK STARTS AT ``failure_time``
------------------------------------
Not at detection, not at row creation. CA licence Condition 9.2 gives 24 hours from the
interruption; DPA 2019 s.43 gives 72 hours from becoming aware of a breach. A notice opened
six hours late is born with six hours already gone, and that is the point: it is what makes
``significance_json.reason_for_delay`` (required by §9.2 when ``sent_at > due_at``) a real
disclosure instead of a formality. :func:`release_notice` refuses a late send that has no
reason recorded.

EAT vs UTC (§7.0.6, defect #41)
-------------------------------
The CA reads Nairobi time; the database stores naive UTC. Both facts are true at once and
the resolution is one-directional: **all arithmetic happens in UTC, and EAT exists only on
the way out**. ``due_at = clock_started_at + timedelta(hours=…)`` — adding a duration to a
naive-UTC instant gives the correct instant in every zone. The bug this avoids is converting
``failure_time`` to EAT and storing the result as if it were UTC, which moves a 24-hour
regulatory deadline by three hours in the wrong direction. Anything a human reads goes
through ``services.clock.fmt_eat``; nothing computed from a ``fmt_eat`` value is ever stored.

UNVERIFIED (§5.3.20, §7.6.7): the 24-hour Condition 9.2 obligation is taken from the CA
Network Facilities Provider Tier 1 licence *template*. The operator's actual licence class
and Condition 9 wording must be confirmed with Legal before this lane is enabled, and
"significant" is undefined in the licence — which is why significance is a YAML rule plus a
human decision, and never a conclusion this module reaches on its own.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import update
from sqlalchemy.orm import Session

from noc_agents.api.deps import _owned
from noc_agents.config import OperatorConfig
from noc_agents.db.models import AuditRow, HitlTaskRow, IncidentRow, WorkNoteRow, new_id, utcnow
from noc_agents.db.models_regulatory import (
    DRAFT,
    NOT_REQUIRED,
    PENDING_APPROVAL,
    REGULATORY_KINDS,
    SENT,
    RegulatoryNotificationRow,
)
from noc_agents.domain.alerts import AudienceSpec
from noc_agents.domain.enums import HitlTaskType
from noc_agents.orchestrator import outbox
from noc_agents.realtime.commit_hook import buffer_event
from noc_agents.realtime.hub import RealtimeEvent
from noc_agents.scheduler import JobCard, JobResult
from noc_agents.services.clock import fmt_eat, z_utc
from noc_agents.services.evidence import get_or_build_pack
from noc_agents.services.hitl import compose_alert, envelope_payload, is_raiser

if TYPE_CHECKING:  # typing only — keeps the import graph of a flag-off process unchanged
    from noc_agents.config import AppSettings

log = logging.getLogger(__name__)

__all__ = [
    "COUNTDOWN_THRESHOLDS_H",
    "DEADLINE_EVENT",
    "DEFAULT_DEADLINE_HOURS",
    "DEFAULT_SIGNIFICANCE",
    "DISPATCH_KEY",
    "REGULATORY_ENABLED_ENV",
    "REGULATORY_JOB",
    "REGULATORY_RAISER",
    "QUEUED",
    "REGULATORY_TASK_TYPE",
    "SEND_FAILED",
    "SERVICE_STATUSES",
    "ClockStartUnknown",
    "NoticeNotApproved",
    "NoticeStateError",
    "Significance",
    "SweepReport",
    "approval_of",
    "countdown",
    "countdown_json",
    "draft_for",
    "evaluate_and_open",
    "evaluate_significance",
    "incident_notifications",
    "notice_text",
    "record_dispatch_outcome",
    "regulatory_enabled",
    "release_notice",
    "request_approval",
    "sweep_deadlines",
]

# --------------------------------------------------------------------------------- the flag

REGULATORY_ENABLED_ENV = "REGULATORY_ENABLED"
#: Spellings that read as true. Spelled out again rather than imported (as ``services/memory.py``
#: and ``pollers/weather.py`` also do) so a flag-off process does not import another lane to
#: answer a question about this one.
_TRUE = frozenset({"1", "true", "yes", "on"})


def regulatory_enabled() -> bool:
    """``REGULATORY_ENABLED`` — default **false**. Only an explicit true value arms the lane.

    Read at call time, never cached: tests flip it with ``monkeypatch.setenv`` and an operator
    flips it in ``.env`` between runs. Costs one ``os.getenv`` on the off path.
    """
    return (os.getenv(REGULATORY_ENABLED_ENV) or "").strip().lower() in _TRUE


# ----------------------------------------------------------------------------- the vocabulary

#: ``hitl_tasks.task_type`` for the gate. Read from the enum when the member exists so there
#: is exactly one spelling in the process, and falls back to the literal so this lane works
#: before ``domain/enums.py`` gains it (that file belongs to no lane and is edited once):
#:
#:     APPROVE_REGULATORY_NOTICE = "APPROVE_REGULATORY_NOTICE"
#:
#: must be added to ``HitlTaskType``. Until it is, the string is still what lands in the
#: column — the enum is not a database constraint — so the gate itself is unaffected.
try:
    REGULATORY_TASK_TYPE: str = HitlTaskType.APPROVE_REGULATORY_NOTICE.value  # type: ignore[attr-defined]
except AttributeError:  # pragma: no cover — trivially true until the enum member lands
    REGULATORY_TASK_TYPE = "APPROVE_REGULATORY_NOTICE"

#: ``hitl_tasks.entity_type`` for these cards: what the task is really about (§7.5).
NOTICE_ENTITY_TYPE = "regulatory_notification"
#: The raiser. A principal-shaped string that can never equal a human's name, so the
#: raiser ≠ approver rule (§6.5) never blocks the supervisor who has to approve the notice.
REGULATORY_RAISER = "agent:RegulatoryNotificationAgent"
#: §6.1 audience + recipient ref. A config path, never an address — the regulator's mailbox is
#: resolved at dispatch, exactly like every other audience.
NOTICE_AUDIENCE = "REGULATOR"
NOTICE_RECIPIENTS_REF = "regulatory.recipients.CA"

#: The WS event (§5.3.20). Published through ``buffer_event``, so it leaves the process only
#: if the transaction that noticed the deadline actually committed.
DEADLINE_EVENT = "regulatory.deadline"
#: Hours-remaining marks the countdown fires at (§5.3.20). Descending: the sweep publishes the
#: *smallest* crossed threshold, so a first sweep that finds 1 h left says "2 h" and not "12 h".
COUNTDOWN_THRESHOLDS_H: tuple[int, ...] = (12, 2)

#: ``significance_json`` keys this module owns. The spec puts ``rule_matched``/``yaml_path``
#: there (§7.6.1) and ``reason_for_delay`` there too (§9.2, the DPA s.43 row), so the column is
#: already a bag; the countdown bookkeeping and the release record live in it rather than
#: costing the spec's DDL two more columns.
COUNTDOWN_FIRED_KEY = "countdown_fired"
REASON_FOR_DELAY_KEY = "reason_for_delay"
RELEASE_KEY = "release"
#: The terminal outcome of the outbox row, written by :func:`record_dispatch_outcome`.
DISPATCH_KEY = "dispatch"

# ----------------------------------------------------------------- TWO STATUSES §7.6.1 OMITS
#
# **§7.6.1 enumerates four**: ``DRAFT | PENDING_APPROVAL | SENT | NOT_REQUIRED``. These two
# are not in that list. They exist anyway, for the same reason ``api/routers/pir.py`` ships a
# ``PATCH .../actions/{action_id}`` the spec's route list omits: the spec's own data model
# makes a state reachable that its vocabulary cannot name.
#
# THE BUG THEY CLOSE. Releasing a notice and transmitting it are two events separated by a
# commit and a dispatcher pass, and the second one can fail terminally — today it fails
# terminally by *default*, because ``regulatory.recipients.CA`` ships as an empty list in
# every profile and ``services/notify.resolve_recipients`` refuses rather than falling back
# to the demo mailbox. SMTP 5xx and the reg 41(2) paperwork gate fail the same way. With only
# four statuses the release had to write ``SENT`` + ``sent_at`` at *enqueue* time, so every
# one of those failures left a row reading "notified the Authority at 14:02" about a notice
# that never left the building. This table is the evidence for M10 and for the Condition 9.2
# 24-hour obligation; a false "yes, in time" there is worse than a visible failure, because
# nobody goes looking for a discharged obligation.
#
# WHY NOT REUSE THE FOUR. ``PENDING_APPROVAL`` for an approved-and-queued notice is a lie in
# the other direction (and ``is_open`` would put it back on the countdown and let the redraft
# route rewrite text already on its way to a regulator). ``DRAFT`` is worse. Leaving it
# ``SENT`` is the bug. There is no fourth option: the spec's vocabulary has no word for
# "approved, queued, nothing transmitted yet", and that state exists whether or not it does.
#
# WHY THEY ARE SAFE HERE. Neither is a permission. The module docstring's rule 4 — "no
# ``APPROVED`` status for code to mistake for permission" — is untouched: ``QUEUED`` is
# reached only *after* :func:`approval_of` has passed, and ``SEND_FAILED`` is a failure.
# Permission is still the APPROVED ``HitlTaskRow`` and nothing else.
#
# COST: none in DDL. ``regulatory_notifications.status`` is a plain TEXT column with no CHECK
# and no enum, so there is no ``SCHEMA_VERSION`` bump and no migration; and per
# ``db/models_regulatory.py``'s own docstring — "the transitions and the vocabulary are
# enforced by ``services/regulatory.py``, which is also the only module allowed to write this
# table" — the vocabulary's home is here, not in the model module. ``is_open`` treats both as
# closed, which is exactly how it treated ``SENT`` before: the sweep skips them and the
# redraft route answers 409, so no downstream behaviour moves.

#: Approved, released to the outbox, **nothing transmitted**. ``sent_at`` is still NULL.
QUEUED = "QUEUED"
#: The outbox row reached a terminal non-SENT outcome (DEAD, REJECTED_UNAPPROVED, or FAILED
#: with no attempts left). The reason is in ``significance_json.dispatch.error``; ``sent_at``
#: is still NULL, because nothing left the building.
SEND_FAILED = "SEND_FAILED"
#: The four the spec lists plus the two above, in lifecycle order. Exported for readers and
#: tests; nothing in the database constrains the column to it.
SERVICE_STATUSES: tuple[str, ...] = (DRAFT, PENDING_APPROVAL, QUEUED, SENT, SEND_FAILED, NOT_REQUIRED)

#: §7.6.1 YAML (``regulatory.deadlines_hours``). ``CBK_FACTSHEET`` is deliberately absent:
#: the spec lists it as a kind but gives it no deadline, and inventing a statutory deadline is
#: exactly the kind of thing this lane must never do. :func:`deadline_hours` returns None for
#: it and :func:`open_notification` refuses, with the reason, until Legal supplies one.
DEFAULT_DEADLINE_HOURS: dict[str, int] = {"CA_OUTAGE_24H": 24, "ODPC_BREACH_72H": 72, "CII_24H": 24}
#: §7.6.1 YAML (``regulatory.significance``, decision D15).
DEFAULT_SIGNIFICANCE: dict[str, Any] = {
    "priorities": ["P1"],
    "site_types": ["CORE", "HUB"],
    "users_affected_gte": 100000,
    "multi_region": True,
}
#: The ``yaml_path`` recorded on every verdict, so a card can show *which rule* decided.
SIGNIFICANCE_YAML_PATH = "regulatory.significance"
DEADLINES_YAML_PATH = "regulatory.deadlines_hours"

#: Where ``clock_started_at`` came from. Recorded on every row: a reader must be able to tell
#: a clock started from the real failure time from one started from a weaker column.
CLOCK_SOURCE_FAILURE_TIME = "failure_time"
CLOCK_SOURCE_OUTAGE_START = "outage_start_at"
CLOCK_SOURCE_CREATED_AT = "created_at"


class NoticeNotApproved(PermissionError):
    """A send was attempted without a valid, APPROVED, human-resolved HITL task. Never caught
    and turned into a send — the API turns it into a 403."""


class NoticeStateError(ValueError):
    """The notice is in a status this transition does not allow (already SENT, NOT_REQUIRED …)."""


class ClockStartUnknown(ValueError):
    """The incident carries no failure time, so no lawful deadline can be computed.

    Refusing is the correct answer. Falling back to ``created_at`` would invent a deadline
    later than the real one and quietly convert a missed obligation into a met one.
    """


# ------------------------------------------------------------------------------------ config


def regulatory_config(cfg: OperatorConfig) -> dict[str, Any]:
    """The ``regulatory:`` block of the operator profile, over the §7.6.1 defaults.

    ``OperatorConfig`` does not declare a ``regulatory`` field yet, and pydantic's default
    ``extra="ignore"`` means a ``regulatory:`` block in the YAML is silently dropped until it
    does — so this reads defensively with ``getattr`` and falls back to the spec's own values.
    It starts honouring the profile the moment ``config.py`` grows the field, with no change
    here. (The same pattern ``services/memory.py`` uses for its thresholds.)
    """
    raw = getattr(cfg, "regulatory", None)
    if not isinstance(raw, dict):
        return {
            "deadlines_hours": dict(DEFAULT_DEADLINE_HOURS),
            "significance": dict(DEFAULT_SIGNIFICANCE),
        }
    return {
        "deadlines_hours": {**DEFAULT_DEADLINE_HOURS, **(raw.get("deadlines_hours") or {})},
        "significance": {**DEFAULT_SIGNIFICANCE, **(raw.get("significance") or {})},
    }


def deadline_hours(kind: str, cfg: OperatorConfig) -> int | None:
    """Statutory hours for ``kind``, or None when nothing configures one (``CBK_FACTSHEET``)."""
    value = regulatory_config(cfg)["deadlines_hours"].get(kind)
    return int(value) if value is not None else None


# ------------------------------------------------------------------------------ significance


@dataclass(frozen=True)
class Significance:
    """The verdict of the D15 rule, and enough of its working to put on a card.

    Frozen because a verdict is evidence: code that wants to edit one is about to let a
    machine decide "significant", and §5.3.20 makes that an A2 decision — the rule proposes,
    a human decides and sends.

    ``checks`` records every rule that was evaluated and its outcome, including the ones that
    did **not** match, because "why is there no CA notice for this outage?" is a question
    somebody will ask under pressure. ``None`` in ``checks`` means *not evaluated* (the
    multi-region test needs a session), which is not the same as False.
    """

    significant: bool
    rule_matched: str | None
    checks: dict[str, bool | None] = field(default_factory=dict)
    yaml_path: str = SIGNIFICANCE_YAML_PATH

    def as_json(self) -> dict[str, Any]:
        """The ``significance_json`` seed (§7.6.1 shows ``{rule_matched, yaml_path}``)."""
        return {
            "significant": self.significant,
            "rule_matched": self.rule_matched,
            "yaml_path": self.yaml_path,
            "checks": dict(self.checks),
        }


def _multi_region(session: Session, inc: IncidentRow) -> bool:
    """True when this incident's cascade family spans more than one region.

    The family is the incident, its parent, its siblings under that parent, and its own
    children — the links the cascade wave already maintains (``parent_incident_id``). Read
    through ``_owned`` so the other operator's rows can never widen a significance verdict.
    """
    parent_id = inc.parent_incident_id or inc.id
    rows = session.scalars(
        _owned(IncidentRow).where(
            (IncidentRow.id == parent_id) | (IncidentRow.parent_incident_id == parent_id) | (IncidentRow.id == inc.id)
        )
    ).all()
    regions = {(r.region_code or "").upper() for r in rows if r.region_code}
    return len(regions) > 1


def evaluate_significance(
    inc: IncidentRow, cfg: OperatorConfig, *, session: Session | None = None
) -> Significance:
    """Apply the D15 rule to one incident. Pure with respect to the database unless ``session``.

    Rules are evaluated in the spec's own order (priority, site type, users affected,
    multi-region) and the FIRST match becomes ``rule_matched`` — so the reason on the card is
    the strongest, most defensible one rather than whichever happened to be checked last.

    ``session`` is optional because the multi-region test is the only rule that needs one.
    Without it that rule is recorded as ``None`` (not evaluated) rather than False: a rule
    that could not be checked must not read as a rule that failed.
    """
    rules = regulatory_config(cfg)["significance"]
    priority = (inc.priority or "").upper()
    site_type = (inc.site_type or "").upper()
    users = int(inc.users_affected or 0)
    threshold = int(rules.get("users_affected_gte") or 0)

    checks: dict[str, bool | None] = {}
    matched: str | None = None

    priorities = [str(p).upper() for p in (rules.get("priorities") or [])]
    checks["priority"] = bool(priorities) and priority in priorities
    if checks["priority"] and matched is None:
        matched = f"priority={priority}"

    site_types = [str(s).upper() for s in (rules.get("site_types") or [])]
    checks["site_type"] = bool(site_types) and site_type in site_types
    if checks["site_type"] and matched is None:
        matched = f"site_type={site_type}"

    checks["users_affected"] = bool(threshold) and users >= threshold
    if checks["users_affected"] and matched is None:
        matched = f"users_affected>={threshold}"

    if not rules.get("multi_region"):
        checks["multi_region"] = False
    elif session is None:
        checks["multi_region"] = None  # not evaluated — see the docstring
    else:
        checks["multi_region"] = _multi_region(session, inc)
        if checks["multi_region"] and matched is None:
            matched = "multi_region"

    return Significance(significant=matched is not None, rule_matched=matched, checks=checks)


# ------------------------------------------------------------------------------- the clock


def clock_start(inc: IncidentRow) -> tuple[datetime | None, str]:
    """``(instant, source)`` for the regulatory clock.

    ``failure_time`` is the column the spec names, ``outage_start_at`` is its documented
    synonym on this row (``db/models.py``: "failure_time … # = outage start"), and
    ``created_at`` is the **last resort and never a lawful clock start** — it is when this
    system made a row, which is precisely the value a late notice must not be allowed to use.
    Callers that need a real deadline check the source and refuse ``created_at``.
    """
    if inc.failure_time is not None:
        return inc.failure_time, CLOCK_SOURCE_FAILURE_TIME
    if inc.outage_start_at is not None:
        return inc.outage_start_at, CLOCK_SOURCE_OUTAGE_START
    return inc.created_at, CLOCK_SOURCE_CREATED_AT


def due_at_for(started_at: datetime, hours: int) -> datetime:
    """``started_at + hours``, in naive UTC — the storage contract.

    Deliberately a plain ``timedelta`` on the stored instant. Kenya has no DST, so EAT is
    UTC+3 always and "24 hours later" is the same instant computed either way; converting to
    EAT first and storing the result would silently shift every deadline by three hours. EAT
    appears only where a human reads the value (:func:`countdown`, :func:`notice_text`).
    """
    return started_at + timedelta(hours=hours)


def countdown(notice: RegulatoryNotificationRow, *, now: datetime | None = None) -> dict[str, Any]:
    """The workspace countdown block for one notice.

    ``minutes_remaining`` goes negative once the deadline passes rather than clamping at
    zero: "3 hours late" and "on the deadline" are different operational facts, and a UI that
    cannot tell them apart is worse than no countdown.
    """
    now = now or utcnow()
    remaining = notice.due_at - now
    minutes = int(remaining.total_seconds() // 60)
    fired = sorted(notice.significance.get(COUNTDOWN_FIRED_KEY) or [], reverse=True)
    pending = [h for h in COUNTDOWN_THRESHOLDS_H if h not in fired]
    return {
        "notification_id": notice.id,
        "kind": notice.kind,
        "status": notice.status,
        "clock_started_at": z_utc(notice.clock_started_at),
        "clock_started_at_eat": fmt_eat(notice.clock_started_at, "%Y-%m-%d %H:%M"),
        "due_at": z_utc(notice.due_at),
        "due_at_eat": fmt_eat(notice.due_at, "%Y-%m-%d %H:%M"),
        "minutes_remaining": minutes,
        "hours_remaining": round(minutes / 60, 2),
        "overdue": minutes < 0,
        "thresholds_fired": fired,
        "next_threshold_hours": min(pending) if pending else None,
        "hitl_task_id": notice.hitl_task_id,
    }


def countdown_json(notice: RegulatoryNotificationRow, *, now: datetime | None = None) -> dict[str, Any]:
    """:func:`countdown` with its two datetimes flattened to ``…Z`` strings.

    Two spellings exist because two consumers do. A FastAPI response wants real ``datetime``
    objects (``z_utc`` makes them serialize with a ``Z``), but ``hitl_tasks.proposed_payload``
    and the WebSocket frames both go through a bare ``json.dumps`` with no datetime encoder —
    a ``datetime`` in either is a ``TypeError`` at write time, which on the socket is a dropped
    frame and on the card is a failed approval request.
    """
    block = countdown(notice, now=now)
    return {**block, "clock_started_at": block["clock_started_at"].isoformat(), "due_at": block["due_at"].isoformat()}


# ------------------------------------------------------------------------------- the draft


def _service_lines(inc: IncidentRow) -> str:
    services = [s for s in inc.services_impacted if s]
    return ", ".join(services) if services else "voice/data services at the affected site"


def notice_text(
    inc: IncidentRow,
    cfg: OperatorConfig,
    *,
    kind: str,
    clock_started_at: datetime,
    due_at: datetime,
    significance: Significance,
    pack_sha: str | None = None,
) -> tuple[str, str]:
    """``(subject, body)`` for one notice — deterministic template, no model involved.

    Every time a human will read goes through ``fmt_eat``: the CA reads Nairobi time, and a
    notice that quotes UTC while calling it local is a compliance failure wearing a timestamp.
    The numbered structure mirrors what the Condition 9.2 notification has to contain; it is
    a **draft for a named officer to check and send**, and the closing lines say so on the
    page rather than only in this docstring.
    """
    region = getattr(cfg.regions.get(inc.region_code or ""), "label", None) or (inc.region_code or "")
    restored = (
        f"Restored at {fmt_eat(inc.restored_at, '%Y-%m-%d %H:%M')}"
        if inc.restored_at is not None
        else "Not yet restored at the time of this notice"
    )
    subject = f"[{inc.priority}] {inc.incident_number} | Service interruption notification to the Authority"
    body = "\n".join(
        [
            f"REGULATORY NOTIFICATION DRAFT — {kind}",
            "",
            f"1.  Licensee: {cfg.display_name}",
            f"2.  Incident reference: {inc.incident_number}",
            f"3.  Time of interruption: {fmt_eat(clock_started_at, '%Y-%m-%d %H:%M')}",
            f"4.  Notification due by: {fmt_eat(due_at, '%Y-%m-%d %H:%M')}",
            f"5.  Location: {inc.site_id} {inc.site_name}".rstrip() + f" | {inc.county or 'county not recorded'} | {region}",
            f"6.  Site type: {inc.site_type}",
            f"7.  Services affected: {_service_lines(inc)}",
            f"8.  Estimated subscribers affected: {int(inc.users_affected or 0)}",
            f"9.  Preliminary cause: {inc.root_cause_hypothesis or 'under investigation'}",
            f"10. Current status: {inc.status}. {restored}",
            f"11. Restoration action: {inc.msp_action_taken or inc.impact_summary or 'restoration in progress'}",
            f"12. Evidence pack sha256: {pack_sha or 'not yet generated'}",
            f"13. Significance basis: {significance.rule_matched or 'not significant'} ({significance.yaml_path})",
            "",
            "DRAFT — not sent. This text is released to the Authority only after a named",
            "authorised officer approves it in the NOC system (APPROVE_REGULATORY_NOTICE).",
            "Licence Condition 9 wording is UNVERIFIED for this licence class — confirm with Legal.",
        ]
    )
    return subject, body


def draft_for(
    session: Session,
    notice: RegulatoryNotificationRow,
    inc: IncidentRow,
    cfg: OperatorConfig,
    *,
    significance: Significance | None = None,
    pack_sha: str | None = None,
) -> dict[str, Any]:
    """Build (and store on the notice) the RESTRICTED / REGULATOR envelope plus its text.

    Two properties come free from ``services/alerts.build_alert`` and are the reason this
    goes through the envelope rather than around it: ``scope="RESTRICTED"`` and a ``REGULATOR``
    audience each independently force ``governance.requires_hitl=True``, so an envelope for a
    regulator can never be built in an auto-send shape.

    ``compose_alert`` is fail-soft (it returns None for a profile whose incident numbers fall
    outside §6.1, such as airtel's dated style). A missing envelope must not mean a missing
    obligation, so the draft degrades to subject/body only and records ``envelope=None``.
    """
    verdict = significance or Significance(
        significant=True,
        rule_matched=notice.significance.get("rule_matched"),
        checks=notice.significance.get("checks") or {},
    )
    subject, body = notice_text(
        inc,
        cfg,
        kind=notice.kind,
        clock_started_at=notice.clock_started_at,
        due_at=notice.due_at,
        significance=verdict,
        pack_sha=pack_sha,
    )
    alert = compose_alert(
        inc,
        cfg,
        msg_type="ALERT",
        scope="RESTRICTED",
        audiences=[
            AudienceSpec(
                audience=NOTICE_AUDIENCE,
                channels=["EMAIL"],  # the regulator notice is written; no SMS to a regulator
                language="en",
                recipients_ref=NOTICE_RECIPIENTS_REF,
            )
        ],
        hitl_task_id=notice.hitl_task_id,
    )
    draft = {
        "kind": notice.kind,
        "subject": subject,
        "body": body,
        "audience": NOTICE_AUDIENCE,
        "scope": "RESTRICTED",
        "recipients_ref": NOTICE_RECIPIENTS_REF,
        "evidence_pack_sha256": pack_sha,
        "envelope": envelope_payload(alert) if alert is not None else None,
        # Template path: no model drafted this text, so no §6.2 AI-disclosure footer belongs
        # on it. The flag is recorded rather than assumed, so an LLM-assisted wording later
        # cannot inherit a "human-written" provenance by omission.
        "ai_assisted": False,
    }
    notice.draft_alert = draft
    session.flush()
    return draft


# ------------------------------------------------------------------------------ opening a row


def incident_notifications(session: Session, incident_id: str) -> list[RegulatoryNotificationRow]:
    """This operator's regulatory rows for one incident, deadline first then kind (stable)."""
    return list(
        session.scalars(
            _owned(RegulatoryNotificationRow)
            .where(RegulatoryNotificationRow.incident_id == incident_id)
            .order_by(RegulatoryNotificationRow.due_at.asc(), RegulatoryNotificationRow.kind.asc())
        ).all()
    )


def _existing(session: Session, incident_id: str, kind: str) -> RegulatoryNotificationRow | None:
    return session.scalar(
        _owned(RegulatoryNotificationRow).where(
            RegulatoryNotificationRow.incident_id == incident_id, RegulatoryNotificationRow.kind == kind
        )
    )


def open_notification(
    session: Session,
    inc: IncidentRow,
    cfg: OperatorConfig,
    *,
    kind: str = "CA_OUTAGE_24H",
    significance: Significance | None = None,
    actor: str = REGULATORY_RAISER,
) -> RegulatoryNotificationRow:
    """Open (or return) the clock row for ``(incident, kind)``. Idempotent, sends nothing.

    A NOT_REQUIRED row is still a row: "a review was considered and is not needed" has to be
    distinguishable from "nobody looked", which is the whole reason §7.6.8 asks for a P4 to
    produce ``NOT_REQUIRED`` rather than nothing at all.

    Refuses (``ClockStartUnknown``) to open a **required** notice on an incident with no
    failure time. See :func:`clock_start`: the alternative is a deadline computed from row
    creation, which turns a missed statutory obligation into an apparently met one. A
    NOT_REQUIRED row is allowed to fall back, because it asserts no deadline.
    """
    if kind not in REGULATORY_KINDS:
        raise ValueError(f"unknown regulatory kind {kind!r}; expected one of {', '.join(REGULATORY_KINDS)}")
    existing = _existing(session, inc.id, kind)
    if existing is not None:
        return existing

    verdict = significance or evaluate_significance(inc, cfg, session=session)
    hours = deadline_hours(kind, cfg)
    if hours is None:
        raise ValueError(
            f"{kind} has no deadline in {DEADLINES_YAML_PATH}; a statutory deadline is not "
            "something this system may invent — Legal supplies it before the kind is used"
        )
    started_at, source = clock_start(inc)
    if verdict.significant and source == CLOCK_SOURCE_CREATED_AT:
        raise ClockStartUnknown(
            f"{inc.incident_number} has no failure_time or outage_start_at; the regulatory clock "
            "starts at the failure, never at row creation — record the failure time first"
        )
    if started_at is None:  # defensive: created_at is NOT NULL, so this is unreachable today
        raise ClockStartUnknown(f"{inc.incident_number} carries no usable clock start")

    data = verdict.as_json()
    data["clock_start_source"] = source
    data[COUNTDOWN_FIRED_KEY] = []
    row = RegulatoryNotificationRow(
        id=new_id(),
        operator_id=inc.operator_id,
        kind=kind,
        incident_id=inc.id,
        clock_started_at=started_at,
        due_at=due_at_for(started_at, hours),
        status=DRAFT if verdict.significant else NOT_REQUIRED,
    )
    row.significance = data
    session.add(row)
    session.flush()

    if verdict.significant:
        draft_for(session, row, inc, cfg, significance=verdict)
    _audit(
        session,
        inc,
        actor=actor,
        action="regulatory.opened",
        entity_id=row.id,
        rationale=f"{kind} {row.status}: {verdict.rule_matched or 'no significance rule matched'}",
        payload={"kind": kind, "status": row.status, "due_at": row.due_at.isoformat(), "clock_start_source": source},
    )
    return row


def evaluate_and_open(
    session: Session,
    inc: IncidentRow,
    cfg: OperatorConfig,
    *,
    kind: str = "CA_OUTAGE_24H",
    actor: str = REGULATORY_RAISER,
) -> RegulatoryNotificationRow | None:
    """The RegulatoryNotificationAgent's entry point (§5.3.20). ``None`` when the lane is off.

    Off is off: with ``REGULATORY_ENABLED`` false this writes nothing at all — not even a
    NOT_REQUIRED row — because §7.6's exit criteria require the flag-off system to behave
    exactly as it does today, and a new row on every ticket is not that.
    """
    if not regulatory_enabled():
        return None
    return open_notification(session, inc, cfg, kind=kind, actor=actor)


# --------------------------------------------------------------------------- the approval gate


def request_approval(
    session: Session,
    notice: RegulatoryNotificationRow,
    inc: IncidentRow,
    cfg: OperatorConfig,
    *,
    actor: str = REGULATORY_RAISER,
) -> HitlTaskRow:
    """Raise ``APPROVE_REGULATORY_NOTICE`` and move the notice to PENDING_APPROVAL.

    **Queues nothing.** See the module docstring, rule 2: a regulator notice has zero outbox
    rows until :func:`release_notice` runs, so no generic release path can promote it.

    The evidence pack is generated (or reused) and attached here, before the card, so the
    approver is shown the sha256 of the facts the notice is built from and can quote it later.
    """
    if notice.status == SENT:
        raise NoticeStateError(f"{notice.kind} for {inc.incident_number} is already SENT")
    if notice.status == QUEUED:
        # Approved and on the outbox already. A second card would invite a second release of
        # a notice whose first copy may be transmitted between the two clicks.
        raise NoticeStateError(
            f"{notice.kind} for {inc.incident_number} is already QUEUED on the outbox; "
            "it needs no second approval"
        )
    if notice.status == NOT_REQUIRED:
        raise NoticeStateError(
            f"{notice.kind} for {inc.incident_number} is NOT_REQUIRED; re-evaluate significance before asking for approval"
        )
    open_task = _open_task(session, notice)
    if open_task is not None:
        return open_task

    pack, _created = get_or_build_pack(session, inc, generated_by=actor)
    notice.evidence_pack_id = pack.id

    task = HitlTaskRow(
        id=new_id(),
        incident_id=inc.id,
        task_type=REGULATORY_TASK_TYPE,
        status="PENDING",
        entity_type=NOTICE_ENTITY_TYPE,
        entity_id=notice.id,
        created_by=actor,
    )
    session.add(task)
    session.flush()
    notice.hitl_task_id = task.id
    notice.status = PENDING_APPROVAL

    draft = draft_for(session, notice, inc, cfg, pack_sha=pack.sha256)
    task.proposed_payload = {
        "notification_id": notice.id,
        "kind": notice.kind,
        "incident_number": inc.incident_number,
        "subject": draft["subject"],
        "body": draft["body"],
        "audience": NOTICE_AUDIENCE,
        "scope": "RESTRICTED",
        "recipients_ref": NOTICE_RECIPIENTS_REF,
        "evidence_pack_id": pack.id,
        "evidence_pack_sha256": pack.sha256,
        "significance": notice.significance,
        "countdown": countdown_json(notice),
        "envelope": draft["envelope"],  # the key services.hitl.stored_envelope reads
        # Said out loud on the card, because the approver is the last line of defence and
        # should not have to infer it: approving releases the notice to a regulator.
        "warning": "Approving this card authorises a written notification to the regulator.",
    }
    session.flush()
    _audit(
        session,
        inc,
        actor=actor,
        action="regulatory.approval_requested",
        entity_id=notice.id,
        rationale=f"{notice.kind} draft raised for human approval; nothing is queued until a named officer approves",
        payload={"task_id": task.id, "evidence_pack_sha256": pack.sha256, "due_at": notice.due_at.isoformat()},
    )
    return task


def _open_task(session: Session, notice: RegulatoryNotificationRow) -> HitlTaskRow | None:
    """The notice's still-open card, if it has one. Prevents a second card for one notice."""
    if not notice.hitl_task_id:
        return None
    task = session.scalar(_owned(HitlTaskRow).where(HitlTaskRow.id == notice.hitl_task_id))
    if task is not None and task.status in ("PENDING", "CLAIMED"):
        return task
    return None


def approval_of(session: Session, notice: RegulatoryNotificationRow) -> HitlTaskRow:
    """The APPROVED, human-resolved task that authorises this notice — or refuse.

    THIS FUNCTION IS THE M10 GUARANTEE. Every condition below is a separate way a notice
    could otherwise escape, and each is checked against the database rather than against a
    field on the notice (``approved_by``/``approved_at`` there are a mirror for readers, not
    the authority):

    * a task id at all — a notice nobody ever raised a card for cannot be sent;
    * the task is reachable under the **active operator's** scope (``_owned``);
    * it is an ``APPROVE_REGULATORY_NOTICE`` card, not some other approval on the same
      incident that happens to be APPROVED — approving a broadcast is not approving a notice;
    * it points at *this* notice (``entity_type``/``entity_id``), so one approved card cannot
      authorise a second, different notification;
    * its status is exactly ``APPROVED``;
    * ``resolved_by`` is a **named human**: non-empty, and not an ``agent:``/``policy:``
      principal. Autonomy policy approves broadcasts; it does not write to regulators;
    * the approver is not the raiser (§6.5), which for an agent-raised card is automatic and
      for a human-raised one is the rubber-stamp guard.
    """
    where = f"{notice.kind} for notification {notice.id}"
    if not notice.hitl_task_id:
        raise NoticeNotApproved(f"{where}: no APPROVE_REGULATORY_NOTICE task has been raised")
    task = session.scalar(_owned(HitlTaskRow).where(HitlTaskRow.id == notice.hitl_task_id))
    if task is None:
        raise NoticeNotApproved(f"{where}: the approval task is not visible to this operator")
    if task.task_type != REGULATORY_TASK_TYPE:
        raise NoticeNotApproved(f"{where}: task {task.id} is a {task.task_type}, not an approval of this notice")
    if task.entity_type != NOTICE_ENTITY_TYPE or task.entity_id != notice.id:
        raise NoticeNotApproved(f"{where}: task {task.id} approves {task.entity_type} {task.entity_id}, not this notice")
    if task.status != "APPROVED":
        raise NoticeNotApproved(f"{where}: approval task is {task.status}, not APPROVED")
    approver = (task.resolved_by or "").strip()
    if not approver:
        raise NoticeNotApproved(f"{where}: the approval records no approver")
    if approver.startswith("agent:") or approver.startswith("policy:"):
        raise NoticeNotApproved(f"{where}: approved by {approver!r}; a regulator notice needs a named human")
    if is_raiser(task, approver):
        raise NoticeNotApproved(f"{where}: the person who raised the notice may not approve it (§6.5)")
    return task


def release_notice(
    session: Session,
    notice: RegulatoryNotificationRow,
    inc: IncidentRow,
    *,
    actor: str,
    reason_for_delay: str | None = None,
    external_ref: str | None = None,
    now: datetime | None = None,
) -> outbox.OutboxRow:
    """The ONLY producer of a regulatory outbox row. Refuses without a valid human approval.

    Order matters: the approval is checked *first*, before any state is touched, so a refused
    attempt leaves the notice exactly as it was. Then the §9.2 / DPA s.43 late rule — a send
    after ``due_at`` must carry ``reason_for_delay``, and the refusal is a hard one because
    the reason is the disclosure the statute actually asks for, not metadata.

    The outbox row is written with ``requires_hitl=1`` and the task's own approver and
    timestamp, so ``orchestrator.outbox.dispatch`` refuses it independently should that
    approval ever be cleared. The idempotency key is derived from the notice id and carries no
    uuid: a second release attempt returns the same row instead of queueing a second notice to
    a regulator, on top of the status guards above it. The key gains an attempt ordinal ONLY
    after a previous row has been recorded terminally failed (``SEND_FAILED``), which is the
    one case where a second row provably cannot mean two notices at the regulator — nothing
    left the building the first time. See :func:`_dispatch_attempt`.

    **This does not send, and it no longer says it did.** The notice moves to ``QUEUED`` and
    ``sent_at`` stays NULL; ``SENT``/``sent_at`` are written by
    :func:`record_dispatch_outcome` when the dispatcher reports a real transmission. The
    ``release`` record's ``late`` flag is therefore "late *at enqueue*", which is what the
    §9.2 refusal below is judged on; whether the *transmission* was late is a separate fact
    recorded under ``dispatch`` (a notice queued at due_at − 1 min and transmitted at
    due_at + 5 min is late, and the row has to say so).

    Transmits nothing — the drain after the caller's commit does that.
    """
    if notice.status == SENT:
        raise NoticeStateError(f"{notice.kind} for {inc.incident_number} has already been sent")
    if notice.status == QUEUED:
        # The guard that used to be "already SENT". A QUEUED notice has a live outbox row
        # that no one has dispatched yet; releasing again would be a second notification to a
        # regulator for one incident, which is its own kind of regulatory mess.
        raise NoticeStateError(
            f"{notice.kind} for {inc.incident_number} has already been released to the outbox "
            "and is awaiting transmission; it cannot be released twice"
        )
    if notice.status == NOT_REQUIRED:
        raise NoticeStateError(f"{notice.kind} for {inc.incident_number} is NOT_REQUIRED")

    task = approval_of(session, notice)  # <- the gate; raises NoticeNotApproved

    now = now or utcnow()
    late = now > notice.due_at
    reason = (reason_for_delay or "").strip()
    if late and not reason:
        raise NoticeStateError(
            f"{notice.kind} for {inc.incident_number} is past its {fmt_eat(notice.due_at, '%Y-%m-%d %H:%M')} deadline; "
            "a late notification must record reason_for_delay (DPA 2019 s.43; §9.2)"
        )

    attempt = _dispatch_attempt(notice)
    draft = notice.draft_alert or {}
    row = outbox.enqueue(
        session,
        kind=outbox.EMAIL,
        idempotency_key=f"EMAIL:regulatory:{notice.id}" if attempt == 1 else f"EMAIL:regulatory:{notice.id}:{attempt}",
        payload={
            "operator_id": inc.operator_id,
            "incident_number": inc.incident_number,
            "audience": NOTICE_AUDIENCE,
            "subject": draft.get("subject") or f"{inc.incident_number} | regulatory notification",
            "body": draft.get("body") or "",
            "recipients_ref": NOTICE_RECIPIENTS_REF,
            "broadcast_ids": [],
            "regulatory_notification_id": notice.id,
            "regulatory_kind": notice.kind,
            "evidence_pack_id": notice.evidence_pack_id,
            "evidence_pack_sha256": (draft.get("evidence_pack_sha256")),
        },
        incident_id=inc.id,
        hitl_task_id=task.id,
        requires_hitl=True,  # the dispatcher's own second gate (outbox.dispatch)
        approved_by=task.resolved_by,
        approved_at=task.resolved_at,
        operator_id=inc.operator_id,
    )

    # QUEUED, not SENT, and sent_at stays NULL: nothing has been transmitted at this point in
    # the story and the row must not say otherwise. record_dispatch_outcome writes the rest.
    notice.status = QUEUED
    notice.sent_at = None
    notice.approved_by = task.resolved_by
    notice.approved_at = task.resolved_at
    if external_ref:
        notice.external_ref = external_ref
    data = notice.significance
    if reason:
        data[REASON_FOR_DELAY_KEY] = reason
    data[RELEASE_KEY] = {
        "outbox_id": row.id,
        "idempotency_key": row.idempotency_key,
        "released_by": actor,
        "released_at": now.isoformat(),
        "approved_by": task.resolved_by,
        # Late AT ENQUEUE — the fact the §9.2 reason_for_delay refusal above was judged on.
        # ``dispatch.late`` is the one that answers "was the Authority notified in time?".
        "late": late,
        "attempt": attempt,
    }
    # A retry after a terminal failure supersedes the previous outcome, but must not erase it:
    # the failed attempt is moved aside so "we tried at 14:02 and it bounced" survives.
    if attempt > 1 and data.get(DISPATCH_KEY):
        data.setdefault(f"{DISPATCH_KEY}_history", []).append(data.pop(DISPATCH_KEY))
    notice.significance = data

    session.add(
        WorkNoteRow(
            incident_id=inc.id,
            author=actor,
            author_role="NOC",
            body=(
                f"Regulatory notification {notice.kind} released to the outbox after approval by "
                f"{task.resolved_by}. Nothing has been transmitted yet; the dispatcher records the "
                f"outcome on the notice." + (f" Late; reason recorded: {reason}" if late else "")
            ),
            source="regulatory",
        )
    )
    _audit(
        session,
        inc,
        actor=actor,
        # Was ``regulatory.sent``. It never described a send — it fires at enqueue — and an
        # audit trail a regulator reads back must not call a queue action a transmission.
        # ``regulatory.sent`` is now written by record_dispatch_outcome, where it is true.
        action="regulatory.released",
        entity_id=notice.id,
        rationale=f"{notice.kind} released after APPROVE_REGULATORY_NOTICE {task.id} approved by {task.resolved_by}",
        payload={
            "outbox_id": row.id,
            "approved_by": task.resolved_by,
            "late": late,
            "reason_for_delay": reason or None,
            "external_ref": notice.external_ref,
        },
    )
    session.flush()
    return row


def _dispatch_attempt(notice: RegulatoryNotificationRow) -> int:
    """Which release attempt this is: 1, or one more than the terminal failures recorded.

    The ordinal exists only to give a retry a *different* idempotency key. It is derived from
    ``significance_json`` rather than counted from the outbox, because the invariant it has to
    preserve is "a second row exists only where a first one is recorded as having transmitted
    nothing" — and that record is on the notice. A notice that has never failed is attempt 1
    and keeps the original, uuid-free key, so nothing about the existing happy path moves.
    """
    history = notice.significance.get(f"{DISPATCH_KEY}_history") or []
    latest = notice.significance.get(DISPATCH_KEY) or {}
    return len(history) + (1 if latest else 0) + 1


def record_dispatch_outcome(
    session: Session,
    row: outbox.OutboxRow,
    *,
    notification_id: str,
    final_status: str,
    now: datetime,
    error: str | None = None,
    provider: str | None = None,
) -> RegulatoryNotificationRow | None:
    """The dispatcher's terminal outcome, written back onto the notice. THE FIX FOR M10's
    EVIDENCE TABLE.

    Called from ``orchestrator.outbox._finalize`` once, when the outbox row has reached a
    status it will not leave (SENT, DEAD, REJECTED_UNAPPROVED, or FAILED with no attempts
    left). Only here does ``status = SENT`` and ``sent_at`` get written, because only here is
    it true. Everything else becomes ``SEND_FAILED`` with the reason on the row: a notice that
    was refused for want of a CA address, bounced by a 5xx, or stopped by the reg 41(2)
    paperwork gate must read as a failure, never as a discharged obligation.

    **Lateness is judged here, on the transmission.** ``release_notice`` refuses a late
    *enqueue* without a ``reason_for_delay``, but a notice queued a minute before ``due_at``
    and transmitted five minutes after it is still a late notification under Condition 9.2 /
    DPA s.43, and no reason was ever asked for. That case is recorded honestly
    (``late=True``, ``reason_for_delay_recorded=False``) rather than papered over — this
    function must not invent the disclosure the statute asks a human for.

    Returns the notice, or ``None`` when there is nothing to write to. Never raises for a
    missing or foreign row: it runs inside the drain's per-row transaction, and a notice that
    has been deleted must not stop the outbox recording the outbox's own outcome.
    """
    notice = session.get(RegulatoryNotificationRow, notification_id)
    if notice is None:
        log.warning("regulatory: outbox row %s names notification %s, which does not exist", row.id, notification_id)
        return None
    # Scoped by explicit comparison, NOT by ``_owned``: that helper reads the active
    # operator from request context, and the drainer runs on the scheduler thread where
    # there is none. Comparing the two rows' own ``operator_id`` is the same guarantee
    # without depending on ambient state.
    if notice.operator_id != row.operator_id:
        log.error(
            "regulatory: outbox row %s (operator %s) names notification %s belonging to operator %s; refusing",
            row.id, row.operator_id, notice.id, notice.operator_id,
        )
        return None
    if notice.status not in (QUEUED, SEND_FAILED):
        # A late or duplicated outcome for a notice that has moved on. Recording it would
        # overwrite a truthful state with a stale one, so it is logged and dropped.
        log.warning(
            "regulatory: outbox row %s reported %s for notification %s, which is %s — ignored",
            row.id, final_status, notice.id, notice.status,
        )
        return None

    # ``SENT`` is spelled the same in both vocabularies (outbox.SENT and the notice status)
    # and this is the one place the two meet: an outbox row that really transmitted is the
    # only thing that may put the notice into SENT.
    transmitted = final_status == outbox.SENT
    late = now > notice.due_at
    data = notice.significance
    data[DISPATCH_KEY] = {
        "attempt": (data.get(RELEASE_KEY) or {}).get("attempt", 1),
        "outbox_id": row.id,
        "outbox_status": final_status,
        "at": now.isoformat(),
        "provider": provider,
        "error": error,
        # For a transmission: it went out after the deadline. For a failure: the deadline had
        # already passed with nothing transmitted. Both are the same bad news for Condition 9.2.
        "late": late,
        # False here on a late SEND means the §9.2 disclosure was never captured, because the
        # enqueue was on time and nobody was asked for one. Saying so is the honest answer.
        "reason_for_delay_recorded": bool((data.get(REASON_FOR_DELAY_KEY) or "").strip()),
    }
    notice.significance = data
    if transmitted:
        notice.status = SENT
        notice.sent_at = now
    else:
        notice.status = SEND_FAILED
        notice.sent_at = None  # belt and braces: nothing left the building, so nothing is stamped
    session.flush()

    inc = session.get(IncidentRow, notice.incident_id)
    if inc is None:  # defensive: incident_id is a FK, so unreachable while the row exists
        return notice

    if transmitted:
        body = (
            f"Regulatory notification {notice.kind} transmitted to the {NOTICE_AUDIENCE} at "
            f"{fmt_eat(now, '%Y-%m-%d %H:%M')} EAT (outbox {row.id})."
        )
        if late:
            body += (
                f" TRANSMITTED AFTER the {fmt_eat(notice.due_at, '%Y-%m-%d %H:%M')} EAT deadline."
                + ("" if data[DISPATCH_KEY]["reason_for_delay_recorded"] else
                   " No reason_for_delay was recorded at release, because the release was on time —"
                   " DPA 2019 s.43 / §9.2 requires one for a late notification and a human must supply it.")
            )
    else:
        body = (
            f"Regulatory notification {notice.kind} was NOT transmitted: outbox {row.id} ended "
            f"{final_status} ({error or 'no error recorded'}). The obligation is NOT discharged."
        )
    session.add(
        WorkNoteRow(incident_id=inc.id, author=REGULATORY_RAISER, author_role="AGENT", body=body, source="regulatory")
    )
    _audit(
        session,
        inc,
        actor="outbox.dispatcher",
        action="regulatory.sent" if transmitted else "regulatory.send_failed",
        entity_id=notice.id,
        rationale=body,
        payload={
            "outbox_id": row.id,
            "outbox_status": final_status,
            "status": notice.status,
            "sent_at": notice.sent_at.isoformat() if notice.sent_at else None,
            "late": late,
            "error": error,
        },
    )
    return notice


# --------------------------------------------------------------------------------- the sweep


@dataclass
class SweepReport:
    """What one ``regulatory_sweep`` pass did."""

    checked: int = 0
    fired: list[dict[str, Any]] = field(default_factory=list)  # {notification_id, threshold_hours}
    overdue: int = 0
    enabled: bool = True

    def __str__(self) -> str:
        return f"regulatory sweep: checked={self.checked} fired={len(self.fired)} overdue={self.overdue}"


def _mark_countdown_fired(session: Session, notice: RegulatoryNotificationRow, thresholds: list[int]) -> bool:
    """Compare-and-set ``significance_json.countdown_fired``. True when THIS call won.

    The countdown must fire once per threshold, not once per scheduler tick, and "once" has to
    survive two tickers racing (the lease makes that unlikely, not impossible) and a crash
    between the publish and the commit. Both are handled by making the mark a conditional
    UPDATE on the column's exact previous text and only buffering the event when it matched:
    the loser writes nothing and publishes nothing, and a rollback discards the buffered event
    together with the mark, so the next sweep tries again.

    ``notice.significance`` must not have been modified in this unit of work before the call —
    autoflush would write the new value first and the WHERE clause would match the wrong text.
    This is the only writer of that column during a sweep, which is what keeps that true.
    """
    before = notice.significance_json or "{}"
    data = json.loads(before)
    data[COUNTDOWN_FIRED_KEY] = sorted({*(data.get(COUNTDOWN_FIRED_KEY) or []), *thresholds}, reverse=True)
    after = json.dumps(data, sort_keys=True, default=str)
    changed = session.execute(
        update(RegulatoryNotificationRow)
        .where(
            RegulatoryNotificationRow.id == notice.id,
            RegulatoryNotificationRow.significance_json == before,
        )
        .values(significance_json=after)
    ).rowcount
    if changed == 1:
        session.refresh(notice)
    return changed == 1


def _deadline_event(notice: RegulatoryNotificationRow, inc: IncidentRow, threshold: int, *, now: datetime) -> RealtimeEvent:
    block = countdown_json(notice, now=now)
    return RealtimeEvent(
        type=DEADLINE_EVENT,
        operator_id=notice.operator_id,
        incident_id=notice.incident_id,
        payload={
            "notification_id": notice.id,
            "kind": notice.kind,
            "status": notice.status,
            "incident_number": inc.incident_number,
            "threshold_hours": threshold,
            # Both spellings on the wire: the UTC instant a machine compares against, and the
            # EAT string a Nairobi operator reads. A single naive value would be read as local
            # time by the browser and understate the remaining hours by three (defect #41).
            # ISO strings, not datetimes: the WS frames go through ``json.dumps`` in main.py,
            # which has no datetime encoder, so a datetime here is a 500 on the socket.
            "due_at": block["due_at"],
            "due_at_eat": block["due_at_eat"],
            "minutes_remaining": block["minutes_remaining"],
            "overdue": block["overdue"],
            "hitl_task_id": notice.hitl_task_id,
        },
    )


def sweep_deadlines(session: Session, cfg: OperatorConfig, *, now: datetime | None = None) -> SweepReport:
    """Fire ``regulatory.deadline`` for every open notice that has crossed a threshold.

    One event per notice per pass at most: when a first sweep finds a notice with one hour
    left, both the 12 h and the 2 h thresholds are already crossed, so both are recorded as
    fired but only the **smallest** is published. Announcing "12 hours remaining" to a
    wallboard that has one is worse than announcing nothing.

    Events go through ``buffer_event``, never ``hub.publish_sync``: the UI is never told about
    a countdown whose mark the transaction did not keep (§7.0.4).

    Writes nothing and publishes nothing when the flag is off.
    """
    if not regulatory_enabled():
        return SweepReport(enabled=False)
    now = now or utcnow()
    report = SweepReport()
    notices = session.scalars(
        _owned(RegulatoryNotificationRow)
        # ``_owned`` already applies the active operator's clause; naming ``cfg.operator_id``
        # again is deliberate belt-and-braces on the one query that publishes to a socket.
        # If the two ever disagreed the sweep would go quiet rather than announce another
        # operator's deadlines onto this operator's wallboard.
        .where(
            RegulatoryNotificationRow.operator_id == cfg.operator_id,
            RegulatoryNotificationRow.status.in_((DRAFT, PENDING_APPROVAL)),
        )
        .order_by(RegulatoryNotificationRow.due_at.asc())
    ).all()
    for notice in notices:
        report.checked += 1
        minutes = int((notice.due_at - now).total_seconds() // 60)
        if minutes < 0:
            report.overdue += 1
        already = set(notice.significance.get(COUNTDOWN_FIRED_KEY) or [])
        crossed = [h for h in COUNTDOWN_THRESHOLDS_H if minutes <= h * 60 and h not in already]
        if not crossed:
            continue
        inc = session.get(IncidentRow, notice.incident_id)
        if inc is None:  # a notice whose incident vanished: mark it so the sweep stops retrying
            _mark_countdown_fired(session, notice, crossed)
            continue
        if not _mark_countdown_fired(session, notice, crossed):
            continue  # another pass won the mark; it publishes, we do not
        threshold = min(crossed)
        buffer_event(session, _deadline_event(notice, inc, threshold, now=now))
        report.fired.append({"notification_id": notice.id, "kind": notice.kind, "threshold_hours": threshold})
    return report


# ------------------------------------------------------------------------------- the job card


def regulatory_sweep_job(session: Session, settings: "AppSettings") -> JobResult:
    """``regulatory_sweep`` (§5.3.20: every 5 min for deadlines). Commits its own pass.

    Committing here is what publishes the buffered ``regulatory.deadline`` events — the
    after-commit listener is the only path out of the process (``realtime/commit_hook.py``).
    """
    report = sweep_deadlines(session, settings.operator)
    session.commit()
    return JobResult(
        # Off must say off: zero counts read as "swept and found nothing", which is a
        # different (and falsely reassuring) statement from "did not look" (CONFORMANCE A-10).
        summary=str(report) if report.enabled else f"regulatory_sweep skipped: {REGULATORY_ENABLED_ENV} is off",
        rationale="Regulatory deadlines counted down from failure_time; drafts only, nothing sent (M10)",
        tools=(
            {
                "name": "regulatory.sweep_deadlines",
                "ok": True,
                "enabled": report.enabled,
                "checked": report.checked,
                "fired": len(report.fired),
                "overdue": report.overdue,
            },
        ),
    )


#: Register this in ``scheduler/loop.SCHEDULED_JOBS`` (that file belongs to no lane). Import it
#: lazily there, as ``_weather_job()`` does, so the scheduler does not pull this lane in at
#: import time. ``default_enabled=False`` so ``/scheduler/status`` reports the job as OFF while
#: ``REGULATORY_ENABLED`` is unset, rather than claiming to run while the sweep declines to.
REGULATORY_JOB = JobCard(
    "regulatory_sweep",
    300,
    regulatory_sweep_job,
    REGULATORY_ENABLED_ENV,
    "RegulatoryNotificationAgent",
    "regulatory",
    default_enabled=False,
)


# ------------------------------------------------------------------------------------- audit


def _audit(
    session: Session,
    inc: IncidentRow,
    *,
    actor: str,
    action: str,
    entity_id: str,
    rationale: str,
    payload: dict[str, Any],
) -> AuditRow:
    """One ``audit_events`` row. Every regulatory transition gets one — this is the surface a
    regulator reads back, so "who did what, when, and why" is not optional here."""
    row = AuditRow(
        operator_id=inc.operator_id,
        actor=actor,
        action=action,
        entity_type=NOTICE_ENTITY_TYPE,
        entity_id=entity_id,
        rationale=rationale,
        payload_json=json.dumps({**payload, "incident_number": inc.incident_number}, sort_keys=True, default=str),
    )
    session.add(row)
    return row
