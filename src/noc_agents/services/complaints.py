"""The confidential complaint intake (spec §7.8, §5.3.21; ``COMPLAINTS_ENABLED=false``).

Everything here is either a pure function over already-loaded rows or a short read/write
against the session it is handed. No network and no model call happens inside this module:
the optional classifier takes an :class:`~noc_agents.llm.port.LlmPort` the caller supplies,
and the manager reminder leaves through the transactional outbox like every other message
in this system.

Five ideas carry the lane.

**A confidential complaint is confidential from colleagues, not from outsiders.** The
threat here is not an attacker on the internet; it is the person the complaint is about
reading it, or hearing about it, and retaliating. So the RBAC is not a wrapper around the
feature — it *is* the feature. :func:`visible_complaints` is the only way this lane reads
the table, and it puts the "not about you" predicate in the WHERE clause next to the
operator clause, exactly as ``api/deps._owned`` does for tenancy. A duty manager who is
himself the subject of a complaint does not see a filtered version of it; he gets 404, the
same answer he would get for an id that does not exist (``api/deps._get_owned`` explains
why 404 and never 403). The complement is enforced on the way in too: a complaint cannot
be filed against its own filer, and cannot be assigned to a manager who is its subject.

**No automated decision about a person.** DPA 2019 s.35 forbids a decision based solely on
automated processing that significantly affects someone, and General Regulations reg 22
spells out the safeguards. :func:`classify` therefore routes text into a category and a
suggested severity and stops: it returns a draft, never a row. Filing is an act by a named
human (§5.3.21 autonomy A2), ``classification_ai_assisted`` records that a model touched
the suggestion, and :data:`AI_DISCLOSURE` travels with every draft. The classifier also has
no access to identity: it never suggests a ``subject_person_ref``, never proposes a manager
and never sets a status. Employment Act 2007 s.41 (explanation and hearing), s.43 (burden
of proof) and s.45(5)(f) (prior warnings as evidence) all assume a human process, and
§7.8.6 makes the complaint's exclusion from scorecards and individual metrics absolute —
nothing in this module is importable from those paths and nothing here reads them.

**Minimisation is enforced on the way in.** §7.8.6 reads DPA s.25 as "enumerated
categories, minimal free text, no MSISDN/email (validator)". :func:`validate_complaint`
returns *every* failure at once (the ``services/pir.py`` house style: a form that rejects
one field per round trip teaches people to write less, not better), and it refuses a
description that spells out the subject's own name — the whole point of
``subject_person_ref`` is that the name lives in one restricted table, and a name copied
into free text quietly undoes that. The name check reuses ``llm/redaction.NameMap`` +
``scrub_text``; there is exactly one scrubber in this codebase and this module does not
add a second. Its documented limit applies here as it does everywhere: there is no NER, so
a person named in free text who is not in a known field is not recognised.

**A reminder must not become a second copy of the complaint.** §9.5 forbids quoting the
breach in the breach record; the same rule is applied here. :func:`reminder_body` emits a
count and opaque references — never the category, the severity, the subject or a word of
the description — because a reminder that quotes the complaint puts it in an inbox, on a
phone, and eventually in front of the wrong person.

**Retention is per row and reduces rather than deletes.** §9.4 classes
``relationship_complaints`` at 24 months with the action *pseudonymise*, not delete: the
category counts survive (the operator owes quarterly complaint statistics under Consumer
Protection Regulations 2010 reg 7(13)) while the free text goes. The CA licence Condition
12.2 three-year floor does **not** reach this table — §9.4 confines that floor to the
network/QoS class and says purge "never touches the 3-year network facts" — so 24 months is
a ceiling here, not a number in tension with a floor. See :func:`pseudonymise_expired`,
which honours the same posture gate (``config/retention.yaml``'s ``dry_run`` plus
``HOUSEKEEPING_APPLY``) as every other retention action in the system, because the operator
signing one document should not find a second, separate deletion switch elsewhere.

Flag: ``COMPLAINTS_ENABLED``, default **false**. Appendix B lists ``CONTRACTS_ENABLED`` for
§7.8 as a whole; that flag's own line in ``.env.example`` describes contract clause search
and the contract FAQ, and binding the most sensitive surface in the system to the switch an
operator flips to search SLAs would mean enabling a contract search enables a complaint
intake. One extra flag is cheaper than that coupling, and it stays OFF when unset.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Sequence

from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from noc_agents.config import AppSettings
from noc_agents.db.models import AuditRow, OutboxRow, utcnow
from noc_agents.db.models_complaints import RelationshipComplaintRow, SubjectPersonRow
from noc_agents.llm.redaction import EMAIL_RE, PHONE_RE, NameMap, scrub_contacts, scrub_text
from noc_agents.orchestrator import outbox
from noc_agents.scheduler import JobCard, JobResult
from noc_agents.services.clock import z_utc

log = logging.getLogger("noc_agents.services.complaints")

__all__ = [
    "AI_DISCLOSURE",
    "CATEGORIES",
    "COMPLAINTS_JOB",
    "ENABLED_ENV",
    "FOLLOW_UP_WORKING_DAYS",
    "MAX_DESCRIPTION_CHARS",
    "RETENTION_DAYS",
    "SEVERITIES",
    "STATUSES",
    "SUBJECT_TYPES",
    "ComplaintDraft",
    "ReminderReport",
    "RetentionReport",
    "acknowledge",
    "add_working_days",
    "assign",
    "classify",
    "complaint_out",
    "complaints_enabled",
    "file_complaint",
    "is_role_token",
    "keyword_draft",
    "pseudonymise_expired",
    "reminder_body",
    "resolve",
    "send_due_reminders",
    "stats",
    "subject_access",
    "subject_refs_for",
    "visible_complaints",
    "withdraw",
]


# --------------------------------------------------------------------------- the flag

ENABLED_ENV = "COMPLAINTS_ENABLED"
_TRUE = {"1", "true", "yes", "on"}


def complaints_enabled() -> bool:
    """``COMPLAINTS_ENABLED`` — default **false**. Read at call time, never frozen at import.

    With it off nothing in this lane runs: every route in ``api/routers/complaints.py``
    404s and the job returns a no-op, so the system behaves exactly as it did before
    Phase 5.
    """
    return (os.getenv(ENABLED_ENV) or "").strip().lower() in _TRUE


# --------------------------------------------------------------------- the vocabularies

SUBJECT_VENDOR, SUBJECT_INDIVIDUAL = "VENDOR", "INDIVIDUAL"
SUBJECT_TYPES: tuple[str, ...] = (SUBJECT_VENDOR, SUBJECT_INDIVIDUAL)

# §7.8.1's enumerated list, in its order. Enumerated, not free text, is the minimisation
# mechanism (DPA s.25): the category is what gets reported and counted, so the description
# can stay short and can be reduced at 24 months without losing the operator's statistics.
NO_SHOW = "NO_SHOW"
LATE_ARRIVAL = "LATE_ARRIVAL"
UNSAFE_PRACTICE = "UNSAFE_PRACTICE"
POOR_COMMUNICATION = "POOR_COMMUNICATION"
ACCESS_ISSUE = "ACCESS_ISSUE"
CONDUCT = "CONDUCT"
OTHER = "OTHER"
CATEGORIES: tuple[str, ...] = (
    NO_SHOW, LATE_ARRIVAL, UNSAFE_PRACTICE, POOR_COMMUNICATION, ACCESS_ISSUE, CONDUCT, OTHER,
)

LOW, MEDIUM, HIGH = "LOW", "MEDIUM", "HIGH"
SEVERITIES: tuple[str, ...] = (LOW, MEDIUM, HIGH)

OPEN, ACKNOWLEDGED, IN_REVIEW, RESOLVED, WITHDRAWN = (
    "OPEN", "ACKNOWLEDGED", "IN_REVIEW", "RESOLVED", "WITHDRAWN",
)
STATUSES: tuple[str, ...] = (OPEN, ACKNOWLEDGED, IN_REVIEW, RESOLVED, WITHDRAWN)
#: Nothing may be changed once a complaint has ended. A resolved or withdrawn complaint is
#: the record of what a named manager decided; editing it in place would rewrite that under
#: their name — the same argument ``services/pir.py`` makes about a PUBLISHED review.
TERMINAL_STATUSES: frozenset[str] = frozenset({RESOLVED, WITHDRAWN})

#: §7.8.3: "Managers get a reminder at ``follow_up_due_at`` (default 5 working days)".
#: Working days, not calendar days — a complaint filed on a Friday afternoon whose clock
#: ran through the weekend would be chased on a Wednesday for no reason. Kenyan public
#: holidays are NOT modelled (there is no holiday calendar in this system); the effect is
#: that a reminder can fire one working day early in a holiday week, which is the safe
#: direction for a complaint.
FOLLOW_UP_WORKING_DAYS = 5
#: §9.4: relationship complaints, 24 months, action *pseudonymise*. 730 = 2 x 365, matching
#: ``config/retention.yaml``'s ``relationship_complaints`` class, which is the document
#: Legal signs. Kept as a constant here as well as in the YAML for the same reason
#: ``housekeeping.LICENCE_FLOOR_DAYS`` is in code: a retention promise that exists only in a
#: file someone can edit is not a guarantee.
RETENTION_DAYS = 730
#: DPA s.25 minimisation, expressed as a number. Long enough for "arrived 14:40 for an 09:00
#: SLA, third time this month, tower left unlocked", short enough that nobody pastes a chat
#: log into it. Enforced, not advisory.
MAX_DESCRIPTION_CHARS = 1000
MAX_RESOLUTION_CHARS = 1000

AI_DISCLOSURE = (
    "AI-assisted suggestion only. It classifies text; it decides nothing about any person "
    "and files nothing. A named human confirms and submits the form (DPA 2019 s.35, "
    "General Regulations reg 22; Employment Act 2007 s.41)."
)

# Messages. Fixed strings so the API, the tests and the UI all quote the same words.
MSG_NO_CONTACTS = (
    "description must not contain a phone number or an e-mail address "
    "(DPA 2019 s.25 minimisation; §7.8.6)"
)
MSG_NAMES_SUBJECT = (
    "describe what happened, not who: refer to the person by their role token or subject "
    "reference, never by name (§7.8.1 — the name lives only in subject_persons)"
)
MSG_SELF = "a complaint cannot be filed against the person filing it"
MSG_MANAGER_IS_SUBJECT = (
    "this manager is the subject of this complaint and may not handle it"
)

# --------------------------------------------------------------- audit actions (§7.8.3)
# "every view/edit writes an AuditRow". Reads are audited as well as writes, which is
# unusual in this codebase and deliberate: on this table, who LOOKED is the fact an
# investigation turns on.
AUDIT_FILED = "complaint.filed"
AUDIT_VIEWED = "complaint.viewed"
AUDIT_LISTED = "complaint.listed"
AUDIT_ASSIGNED = "complaint.assigned"
AUDIT_ACKNOWLEDGED = "complaint.acknowledged"
AUDIT_RESOLVED = "complaint.resolved"
AUDIT_WITHDRAWN = "complaint.withdrawn"
AUDIT_CLASSIFIED = "complaint.classified"
AUDIT_SUBJECT_ACCESS = "complaint.subject_access"
AUDIT_RETENTION = "complaint.retention"
ENTITY = "relationship_complaint"


# ------------------------------------------------------------------------------ helpers


def _clean(value: str | None) -> str:
    return (value or "").strip()


def _norm(value: str | None) -> str:
    """Identity comparison key for a person's display name.

    Case- and whitespace-insensitive, and nothing cleverer. This is the join between an
    authenticated principal and a ``subject_persons`` row, and it is the weakest link in
    the lane: with a real identity provider the join key would be ``Principal.subject``
    (a stable user id), and ``subject_persons`` would carry it. It does not today, so this
    is written down rather than hidden — see :func:`subject_refs_for`.
    """
    return " ".join(_clean(value).lower().split())


def is_role_token(value: str | None) -> bool:
    """``RNIO-NBI-E`` / ``FE-MTK-01`` / ``MSP_EGYPRO_POWER`` shapes, never a person's name.

    The same shape rule ``services/pir.py`` applies to an action item's owner: upper case,
    no lower-case letters, and at least one separator or digit. "Kevin Ochieng" fails;
    so does "Kevin".
    """
    token = _clean(value)
    if not token or any(ch.islower() for ch in token):
        return False
    return any(ch.isdigit() for ch in token) or any(sep in token for sep in "-_")


def add_working_days(start: datetime, days: int) -> datetime:
    """``start`` plus ``days`` working days (Mon–Fri), preserving the time of day."""
    out = start
    remaining = max(0, int(days))
    while remaining:
        out = out + timedelta(days=1)
        if out.weekday() < 5:  # 5 = Saturday, 6 = Sunday
            remaining -= 1
    return out


def _subject_name_map(session: Session, *, operator_id: str, refs: Iterable[str | None]) -> NameMap:
    """A :class:`NameMap` seeded with the display names behind ``refs``.

    Used to keep those names OUT of free text, which is the opposite of the usual direction
    (redaction normally keeps names out of a model prompt). One scrubber, two uses.
    """
    names = NameMap()
    wanted = [r for r in {_clean(r) for r in refs} if r]
    if not wanted:
        return names
    rows = session.scalars(
        select(SubjectPersonRow).where(
            SubjectPersonRow.operator_id == operator_id, SubjectPersonRow.ref.in_(wanted)
        )
    ).all()
    for row in rows:
        names.token_for(row.display_name)
    return names


def _names_leak(text: str, names: NameMap) -> bool:
    """True when ``text`` still mentions one of ``names`` (the scrubber decides, not a regex)."""
    if not text or not names.token_to_name:
        return False
    return scrub_text(text, names) != text


# --------------------------------------------------------------------------- the subject
# guard: the one rule the whole lane exists to keep


def subject_refs_for(session: Session, *, operator_id: str, actor: str) -> frozenset[str]:
    """Every ``subject_persons.ref`` that IS this actor.

    The set is normally empty (most people are the subject of nothing) and is never larger
    than the number of register entries sharing a display name. Two people with the same
    name both lose sight of both complaints, which is the safe direction: the cost is a
    manager who cannot see one complaint, and the alternative cost is a subject who can.

    The comparison is done in Python over the operator's register rather than in SQL,
    because the normalisation (case *and* collapsed whitespace) has to be the same one the
    validator applies, and SQL ``lower()`` is only half of it. The register holds one row
    per person ever named in a complaint — tens, not millions — so the cost is a bounded
    scan on a read path that is already writing an audit row.
    """
    key = _norm(actor)
    if not key:
        return frozenset()
    rows = session.scalars(
        select(SubjectPersonRow).where(SubjectPersonRow.operator_id == operator_id)
    ).all()
    return frozenset(row.ref for row in rows if _norm(row.display_name) == key)


def visible_complaints(
    session: Session,
    *,
    operator_id: str,
    actor: str,
    all_complaints: bool,
):
    """``SELECT`` over the complaints this actor may see. **The only read path in this lane.**

    Three predicates, all in the WHERE clause and never a check after the fetch:

    1. ``operator_id`` — tenancy (§8), the same clause ``api/deps._owned`` builds. It is
       spelled out here rather than reusing ``_owned`` because this function must add the
       other two in the same statement, and a caller that could get a bare ``_owned``
       select of this table would be one refactor away from reading somebody else's
       complaint;
    2. **not about you** — every complaint whose ``subject_person_ref`` is one of this
       actor's refs is excluded, *whatever the caller's role*. A duty manager, an admin and
       the legal team are all excluded from a complaint about themselves. This is what makes
       "the person complained about can read the complaint" structurally impossible rather
       than merely unlikely, and it is why the exclusion lives here and not in the router;
    3. ``all_complaints`` — §7.8.2's "own for engineers; all for duty_manager/management".
       A filer always sees what they filed.

    The residual risk is stated rather than hidden: an INDIVIDUAL complaint filed against a
    ``subject_role_token`` with no ``subject_person_ref`` cannot be excluded by this
    predicate, because nothing in the database says which human holds that token today.
    Such a complaint is visible only to the read-all roles and to its filer, which limits
    the exposure but does not eliminate it. Give a complaint about a named person a ref.
    """
    stmt = select(RelationshipComplaintRow).where(RelationshipComplaintRow.operator_id == operator_id)
    mine = subject_refs_for(session, operator_id=operator_id, actor=actor)
    if mine:
        stmt = stmt.where(
            or_(
                RelationshipComplaintRow.subject_person_ref.is_(None),
                RelationshipComplaintRow.subject_person_ref.notin_(sorted(mine)),
            )
        )
    if not all_complaints:
        stmt = stmt.where(RelationshipComplaintRow.filed_by == actor)
    return stmt


def get_visible(
    session: Session, complaint_id: str, *, operator_id: str, actor: str, all_complaints: bool
) -> RelationshipComplaintRow | None:
    """One complaint, or ``None`` — which the router turns into 404, never 403.

    403 would confirm that a complaint about this person exists, to that person. The whole
    point of :func:`visible_complaints` would be lost in the status code.
    """
    return session.scalar(
        visible_complaints(
            session, operator_id=operator_id, actor=actor, all_complaints=all_complaints
        ).where(RelationshipComplaintRow.id == complaint_id)
    )


# ------------------------------------------------------------------------- the validator


def validate_complaint(
    session: Session,
    *,
    operator_id: str,
    filed_by: str,
    subject_type: str,
    vendor_id: str | None,
    subject_role_token: str | None,
    subject_person_ref: str | None,
    category: str,
    severity: str,
    description: str,
    incident_id: str | None = None,
) -> list[str]:
    """Every problem with this filing, in one list (§ the ``services/pir.py`` house style).

    All failures at once: a complaint form that rejects one field per round trip is a form
    people abandon, and an abandoned complaint is the failure mode this lane exists to
    prevent. Nothing is written and nothing is echoed back — a rejection never repeats the
    offending text, because the answer travels back over the same wire the text came in on.
    """
    problems: list[str] = []
    subject_type = _clean(subject_type).upper()
    category = _clean(category).upper()
    severity = _clean(severity).upper()
    description = _clean(description)

    if subject_type not in SUBJECT_TYPES:
        problems.append(f"subject_type must be one of {list(SUBJECT_TYPES)}")
    if category not in CATEGORIES:
        problems.append(f"category must be one of {list(CATEGORIES)}")
    if severity not in SEVERITIES:
        problems.append(f"severity must be one of {list(SEVERITIES)}")
    if not _clean(filed_by):
        problems.append("filed_by is required: an untraceable allegation cannot be answered "
                        "(Employment Act 2007 s.41)")

    # Who the complaint is about must be expressible without a name.
    if subject_type == SUBJECT_VENDOR:
        if not _clean(vendor_id):
            problems.append("a VENDOR complaint needs vendor_id")
        if _clean(subject_person_ref):
            problems.append(
                "a VENDOR complaint must not carry subject_person_ref: naming an individual "
                "inside a complaint about their employer is how a contract dispute becomes a "
                "disciplinary one (Employment Act 2007 s.41/s.43)"
            )
    elif subject_type == SUBJECT_INDIVIDUAL:
        if not _clean(subject_role_token) and not _clean(subject_person_ref):
            problems.append(
                "an INDIVIDUAL complaint needs subject_role_token or subject_person_ref"
            )
    if _clean(subject_role_token) and not is_role_token(subject_role_token):
        problems.append(
            "subject_role_token must be a role token (RNIO-NBI-E / FE-MTK-01 / MSP-EGYPRO-POWER), "
            "not a person's name (§7.8.1)"
        )

    ref = _clean(subject_person_ref)
    if ref:
        known = session.scalar(
            select(SubjectPersonRow).where(
                SubjectPersonRow.operator_id == operator_id, SubjectPersonRow.ref == ref
            )
        )
        if known is None:
            # Not "unknown ref" — the register is restricted, so the error must not confirm
            # or deny what is in it to a caller who may not read it.
            problems.append("subject_person_ref is not a usable subject reference")
        elif _norm(known.display_name) == _norm(filed_by):
            # The self-complaint bar. Without it, the filer IS the subject, and the filer can
            # always read what they filed — which would be a complaint its subject can read,
            # the one thing this lane must make impossible.
            problems.append(MSG_SELF)

    # Minimisation (DPA s.25) on the one free-text field.
    if not description:
        problems.append("description is required")
    if len(description) > MAX_DESCRIPTION_CHARS:
        problems.append(
            f"description must be at most {MAX_DESCRIPTION_CHARS} characters "
            f"(DPA 2019 s.25 minimisation); it is {len(description)}"
        )
    if description and (PHONE_RE.search(description) or EMAIL_RE.search(description)):
        problems.append(MSG_NO_CONTACTS)
    if description and ref:
        names = _subject_name_map(session, operator_id=operator_id, refs=[ref])
        if _names_leak(description, names):
            problems.append(MSG_NAMES_SUBJECT)
    return problems


# ------------------------------------------------------------------------------- writing


def file_complaint(
    session: Session,
    settings: AppSettings,
    *,
    filed_by: str,
    subject_type: str,
    category: str,
    severity: str,
    description: str,
    vendor_id: str | None = None,
    subject_role_token: str | None = None,
    subject_person_ref: str | None = None,
    incident_id: str | None = None,
    evidence_note_ids: Sequence[str] = (),
    classification_ai_assisted: bool = False,
    now: datetime | None = None,
) -> RelationshipComplaintRow:
    """Write one validated complaint. The caller validates first and owns the commit.

    ``follow_up_due_at`` and ``retention_until`` are both stamped here, from the same
    ``now``: the follow-up clock is an operational promise to the complainant and the
    retention clock is a promise to the subject, and both should be answerable from the row
    alone without recomputing them from a config value that may since have changed.
    """
    now = now or utcnow()
    row = RelationshipComplaintRow(
        operator_id=settings.operator.operator_id,
        filed_by=_clean(filed_by),
        filed_at=now,
        subject_type=_clean(subject_type).upper(),
        vendor_id=_clean(vendor_id) or None,
        subject_role_token=_clean(subject_role_token) or None,
        subject_person_ref=_clean(subject_person_ref) or None,
        incident_id=_clean(incident_id) or None,
        category=_clean(category).upper(),
        description=_clean(description),
        evidence_note_ids_json=json.dumps([str(i) for i in evidence_note_ids]),
        severity=_clean(severity).upper(),
        status=OPEN,
        follow_up_due_at=add_working_days(now, FOLLOW_UP_WORKING_DAYS),
        retention_until=now + timedelta(days=RETENTION_DAYS),
        classification_ai_assisted=1 if classification_ai_assisted else 0,
        updated_at=now,
    )
    session.add(row)
    return row


def assign(
    session: Session, complaint: RelationshipComplaintRow, *, manager: str, now: datetime | None = None
) -> list[str]:
    """Hand the complaint to a named manager. Returns the problems, or ``[]`` on success.

    The manager may not be the subject. That is checked here rather than in the router
    because it is the write-side half of :func:`visible_complaints`: the read side makes a
    subject unable to *see* their complaint, and this makes them unable to be handed it.
    """
    manager = _clean(manager)
    problems: list[str] = []
    if not manager:
        problems.append("assigned_manager is required")
    if complaint.status in TERMINAL_STATUSES:
        problems.append(f"this complaint is {complaint.status} and cannot be reassigned")
    if manager and complaint.subject_person_ref:
        mine = subject_refs_for(
            session, operator_id=complaint.operator_id, actor=manager
        )
        if complaint.subject_person_ref in mine:
            problems.append(MSG_MANAGER_IS_SUBJECT)
    if problems:
        return problems
    complaint.assigned_manager = manager
    complaint.updated_at = now or utcnow()
    return []


def acknowledge(
    session: Session, complaint: RelationshipComplaintRow, *, actor: str, now: datetime | None = None
) -> list[str]:
    """OPEN → ACKNOWLEDGED, and the follow-up clock restarts from the acknowledgement.

    Consumer Protection Regulations 2010 reg 7 requires complaints to be acknowledged with
    a reference; the acknowledgement is therefore a state, with a timestamp, not a note.
    Restarting the clock is what makes the reminder mean "nobody has moved this on" rather
    than "this was late once, for ever".
    """
    now = now or utcnow()
    if complaint.status in TERMINAL_STATUSES:
        return [f"this complaint is {complaint.status} and cannot be acknowledged"]
    if complaint.status == OPEN:
        complaint.status = ACKNOWLEDGED
    complaint.acknowledged_at = complaint.acknowledged_at or now
    complaint.assigned_manager = complaint.assigned_manager or _clean(actor)
    complaint.follow_up_due_at = add_working_days(now, FOLLOW_UP_WORKING_DAYS)
    complaint.updated_at = now
    return []


def resolve(
    session: Session,
    complaint: RelationshipComplaintRow,
    *,
    actor: str,
    resolution: str,
    now: datetime | None = None,
) -> list[str]:
    """Close the complaint with a written outcome. A resolution is mandatory.

    A complaint closed with no recorded outcome is indistinguishable from one nobody acted
    on, and it is the outcome — not the allegation — that survives the 24-month reduction
    (§9.4), so an empty one costs the operator its own record of what it did.
    """
    now = now or utcnow()
    text = _clean(resolution)
    problems: list[str] = []
    if complaint.status in TERMINAL_STATUSES:
        problems.append(f"this complaint is already {complaint.status}")
    if not text:
        problems.append("a resolution is required to close a complaint")
    if len(text) > MAX_RESOLUTION_CHARS:
        problems.append(f"resolution must be at most {MAX_RESOLUTION_CHARS} characters")
    if text and (PHONE_RE.search(text) or EMAIL_RE.search(text)):
        problems.append(MSG_NO_CONTACTS.replace("description", "resolution"))
    if text and complaint.subject_person_ref:
        names = _subject_name_map(
            session, operator_id=complaint.operator_id, refs=[complaint.subject_person_ref]
        )
        if _names_leak(text, names):
            problems.append(MSG_NAMES_SUBJECT.replace("describe what happened", "describe what was done"))
    if problems:
        return problems
    complaint.status = RESOLVED
    complaint.resolution = text
    complaint.resolved_at = now
    complaint.assigned_manager = complaint.assigned_manager or _clean(actor)
    complaint.follow_up_due_at = None  # nothing left to chase
    complaint.updated_at = now
    return []


def withdraw(
    complaint: RelationshipComplaintRow, *, reason: str = "", now: datetime | None = None
) -> list[str]:
    """The filer takes the complaint back (``WITHDRAWN``).

    **Not in §7.8.2's route list, and here anyway.** §7.8.1 ships ``WITHDRAWN`` in the status
    vocabulary, and a shipped state no route can reach is a state machine that cannot be
    completed — the same reasoning ``api/routers/pir.py`` records for the action-item PATCH.
    It also matters more here than there: a person who filed a complaint and no longer wants
    it pursued has no other way out, and "you may file but never retract" is how people stop
    filing. Only the filer may use it (enforced in the router), and the row survives with its
    counts, because a withdrawal is itself a fact the operator may need (reg 7(13)).
    """
    now = now or utcnow()
    if complaint.status in TERMINAL_STATUSES:
        return [f"this complaint is already {complaint.status}"]
    complaint.status = WITHDRAWN
    complaint.resolution = _clean(reason) or "withdrawn by the complainant"
    complaint.resolved_at = now
    complaint.follow_up_due_at = None
    complaint.updated_at = now
    return []


# ---------------------------------------------------------------------------- the audit


def audit(
    session: Session,
    *,
    operator_id: str,
    actor: str,
    action: str,
    entity_id: str,
    rationale: str = "",
    payload: dict[str, Any] | None = None,
) -> AuditRow:
    """One ``audit_events`` row per view or edit (§7.8.3).

    ``payload`` carries ids, counts and statuses — never the description, never the
    resolution, never the subject's name. An audit trail that quotes the complaint is a
    second copy of it in a table more people can read than can read the complaint itself
    (§9.3 gives ``audit_events`` to duty_manager/management/legal/admin), which would undo
    the entire RBAC design one convenient log line at a time.
    """
    row = AuditRow(
        operator_id=operator_id,
        actor=_clean(actor) or "unknown",
        action=action,
        entity_type=ENTITY,
        entity_id=entity_id,
        rationale=rationale[:500],
        payload_json=json.dumps(payload or {}, default=str),
    )
    session.add(row)
    return row


# ------------------------------------------------------------------------ the classifier


class ComplaintDraftOut(BaseModel):
    """What a model is allowed to return. Two enumerated fields and a short reason.

    Deliberately minimal: the model may not name a person, may not propose a manager, may
    not set a status and may not reference an incident it was not given. Anything it says
    beyond these fields is not read, so there is nothing for a prompt injection inside a
    pasted complaint to reach.
    """

    category: str = Field(default=OTHER)
    severity: str = Field(default=LOW)
    reason: str = Field(default="")


@dataclass(frozen=True)
class ComplaintDraft:
    """A suggestion for a human to accept, edit or ignore. Never a row, never a decision."""

    category: str
    severity: str
    rationale: str
    source: str  # "keyword" | "llm"
    ai_assisted: bool
    disclosure: str = AI_DISCLOSURE
    #: Always True, and serialised, so the UI and any future caller cannot read this object
    #: as an outcome. DPA 2019 s.35 / reg 22: advisory only, human decides.
    advisory_only: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "severity": self.severity,
            "rationale": self.rationale,
            "source": self.source,
            "ai_assisted": self.ai_assisted,
            "advisory_only": self.advisory_only,
            "disclosure": self.disclosure,
        }


#: Keyword rules, most specific first. Deterministic, offline, and the answer whenever the
#: model is off, unavailable, refused by the transfer gate or returns something outside the
#: vocabulary. The lane must work with ``LLM_ENABLED=false`` — that is the §9.2 s.49(2)
#: suspension case, not a degraded mode.
_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (UNSAFE_PRACTICE, ("unsafe", "no ppe", "without ppe", "harness", "helmet", "live wire",
                       "no earthing", "safety", "hazard")),
    # Whole words only (the haystack is word-split before matching), so stems are spelled
    # out rather than left as prefixes: "harass" would not match "harassed", and matching on
    # substrings instead would make "escalated" a LATE_ARRIVAL.
    (CONDUCT, ("rude", "abusive", "shouted", "insult", "insulted", "insulting", "harassed",
               "harassment", "threat", "threatened", "drunk", "intimidated")),
    (NO_SHOW, ("no show", "no-show", "did not arrive", "never arrived", "did not turn up",
               "failed to attend", "nobody came")),
    (LATE_ARRIVAL, ("late", "arrived after", "hours after", "delayed arrival", "overdue arrival")),
    (ACCESS_ISSUE, ("access", "gate", "padlock", "locked", "keys", "permit", "denied entry",
                    "landlord")),
    (POOR_COMMUNICATION, ("no update", "no feedback", "unreachable", "did not answer",
                          "not picking", "no response", "ignored my")),
)
#: The categories that should reach a human quickly whatever else the text says. A suggestion
#: only — the filer can lower it, and nothing downstream acts on severity by itself.
_HIGH_SEVERITY = frozenset({UNSAFE_PRACTICE, CONDUCT})
_MEDIUM_SEVERITY = frozenset({NO_SHOW, ACCESS_ISSUE})

_WORD = re.compile(r"[^\w]+")


def keyword_draft(text: str) -> ComplaintDraft:
    """The deterministic classifier. Never fails, never calls anything, always answers."""
    haystack = " " + _WORD.sub(" ", _clean(text).lower()) + " "
    for category, needles in _KEYWORDS:
        for needle in needles:
            if f" {_WORD.sub(' ', needle)} ".replace("  ", " ") in haystack:
                return ComplaintDraft(
                    category=category,
                    severity=_suggested_severity(category),
                    rationale=f"keyword rule matched for {category}",
                    source="keyword",
                    ai_assisted=False,
                )
    return ComplaintDraft(
        category=OTHER,
        severity=LOW,
        rationale="no keyword rule matched; a person chooses the category",
        source="keyword",
        ai_assisted=False,
    )


def _suggested_severity(category: str) -> str:
    if category in _HIGH_SEVERITY:
        return HIGH
    if category in _MEDIUM_SEVERITY:
        return MEDIUM
    return LOW


SYSTEM_CLASSIFY = (
    "You sort a telecom NOC relationship complaint into one enumerated category and suggest "
    "a severity. You are not deciding anything about any person and your output is shown to "
    "a human who edits and submits it. Never output a name, a phone number or an e-mail "
    "address. Never recommend an action, a sanction or a manager. If the text does not "
    "clearly match a category, answer OTHER.\n"
    f"category is one of: {', '.join(CATEGORIES)}\n"
    f"severity is one of: {', '.join(SEVERITIES)}"
)


def classify(text: str, *, port: Any | None = None, model: str = "claude-opus-5") -> ComplaintDraft:
    """Suggest a category and severity for ``text``. **Never writes and never files.**

    ``port`` is an :class:`~noc_agents.llm.port.LlmPort` the caller has already decided it
    may use — this function does not read the feature flags, build a client or record a
    transfer, so it is trivially testable with a fake and impossible to turn into a
    surprise network call from inside a request. With ``port=None`` the deterministic
    classifier answers.

    The text is scrubbed with ``scrub_contacts`` before it can reach a model (§5.3.21), so
    an MSISDN or e-mail the filer pasted never leaves the box even though the validator
    would have refused the filing anyway. Names are a different matter and are stated
    plainly in ``llm/redaction.py``: there is no NER, so a name typed into free text is not
    recognised and would travel. That is why the hosted path is gated by the caller
    (``LLM_ENABLED`` plus the reg 41(2) transfer record) and why the system prompt forbids
    echoing one back.

    Any model answer outside the vocabulary falls back to the keyword draft: a category the
    system does not have is not a suggestion, it is a bug that would otherwise reach a form.
    """
    fallback = keyword_draft(text)
    if port is None:
        return fallback
    safe = scrub_contacts(_clean(text)) or ""
    try:
        parsed, _rec = port.draft(
            model=model,
            system=SYSTEM_CLASSIFY,
            user=safe[:MAX_DESCRIPTION_CHARS],
            output_model=ComplaintDraftOut,
            effort="low",
            max_tokens=512,
        )
    except Exception:  # noqa: BLE001 — a classifier must never be the reason a filing fails
        log.warning("complaint classifier: model call failed; using the keyword draft")
        return fallback
    if parsed is None:
        return fallback
    category = _clean(getattr(parsed, "category", "")).upper()
    severity = _clean(getattr(parsed, "severity", "")).upper()
    if category not in CATEGORIES or severity not in SEVERITIES:
        return fallback
    reason = _clean(getattr(parsed, "reason", ""))[:200]
    # The model's own words are scrubbed on the way back too: it was told not to echo a
    # name or a number, and "was told not to" is not a control.
    reason = scrub_contacts(reason) or ""
    return ComplaintDraft(
        category=category,
        severity=severity,
        rationale=reason or "model suggestion",
        source="llm",
        ai_assisted=True,
    )


# --------------------------------------------------------------------------- serialising


def complaint_out(row: RelationshipComplaintRow, *, include_description: bool = True) -> dict[str, Any]:
    """One complaint as the API returns it. Timestamps carry an explicit ``Z`` (§7.0.6).

    ``include_description=False`` is what the list view uses: a queue of twenty complaints
    is read over someone's shoulder in an open-plan NOC, and the categories are enough to
    work the queue. Opening one is a deliberate act, and it writes an audit row.
    """
    body = {
        "id": row.id,
        "filed_by": row.filed_by,
        "filed_at": z_utc(row.filed_at),
        "subject_type": row.subject_type,
        "vendor_id": row.vendor_id,
        "subject_role_token": row.subject_role_token,
        "subject_person_ref": row.subject_person_ref,
        "incident_id": row.incident_id,
        "category": row.category,
        "severity": row.severity,
        "status": row.status,
        "evidence_note_ids": json.loads(row.evidence_note_ids_json or "[]"),
        "assigned_manager": row.assigned_manager,
        "acknowledged_at": z_utc(row.acknowledged_at),
        "follow_up_due_at": z_utc(row.follow_up_due_at),
        "resolution": row.resolution,
        "resolved_at": z_utc(row.resolved_at),
        "retention_until": z_utc(row.retention_until),
        "pseudonymised_at": z_utc(row.pseudonymised_at),
        "classification_ai_assisted": int(row.classification_ai_assisted or 0),
    }
    if include_description:
        body["description"] = row.description
    return body


def stats(session: Session, *, operator_id: str, actor: str, all_complaints: bool) -> dict[str, Any]:
    """Counts only (§7.8.2). No ids, no text, no subject — a count is not a disclosure.

    Computed over the caller's own visible set, so a manager who is the subject of a
    complaint does not learn of its existence from a category total either.
    """
    base = visible_complaints(
        session, operator_id=operator_id, actor=actor, all_complaints=all_complaints
    ).subquery()
    rows = session.execute(
        select(base.c.category, base.c.severity, base.c.status, func.count()).group_by(
            base.c.category, base.c.severity, base.c.status
        )
    ).all()
    by_category: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    by_status: dict[str, int] = {}
    total = 0
    for category, severity, status, count in rows:
        by_category[category] = by_category.get(category, 0) + count
        by_severity[severity] = by_severity.get(severity, 0) + count
        by_status[status] = by_status.get(status, 0) + count
        total += count
    return {
        "total": total,
        "by_category": by_category,
        "by_severity": by_severity,
        "by_status": by_status,
        "open_overdue": sum(
            1
            for _ in session.execute(
                select(base.c.id).where(
                    base.c.status.notin_(sorted(TERMINAL_STATUSES)),
                    base.c.follow_up_due_at.isnot(None),
                    base.c.follow_up_due_at < utcnow(),
                )
            )
        ),
    }


# ------------------------------------------------------------------- subject access (s.26)


def subject_access(session: Session, *, operator_id: str, ref: str) -> dict[str, Any] | None:
    """Everything this system holds about one subject person (DPA 2019 s.26).

    Returns ``None`` when the ref is unknown to this operator — the caller answers 404.

    **What is included and why.** s.26 gives the data subject the right to be informed of
    the use of their personal data and to access it; Employment Act 2007 s.41 separately
    requires that a person facing a consequence can answer the allegation, which is
    impossible without knowing what it says. So the export carries the register entry, every
    complaint about them, each one's category, severity, status, dates, outcome and the
    allegation itself.

    **What is withheld and why.** ``filed_by`` is not in the export, and the description and
    resolution are passed through the shared scrubber with the complainant's name registered,
    so a name that was typed into the text does not reach the subject either. Disclosing who
    complained would make the intake unusable: the realistic consequence of naming a
    complainant to the person complained about is retaliation, and a complaint nobody dares
    file protects nobody. Where the operator concludes that the complainant's identity must
    be disclosed to run a fair disciplinary process, that is a human decision made by Legal
    with the file in front of them, not a default of this route.

    **What this route cannot do, stated rather than implied.** The answer is complete only
    for complaints that carry this person's ``subject_person_ref``. A complaint that names a
    role token, or one whose free text mentions the person without a ref, is not found —
    there is no NER in this system (``llm/redaction.py`` says so in its own docstring) and
    an index cannot find a name nobody recorded. The mitigation is upstream: the validator
    pushes filers towards refs and refuses a description that spells the subject's name.
    ``unstructured_text_not_searched`` is in the payload so the person answering the request
    knows to say so.
    """
    person = session.scalar(
        select(SubjectPersonRow).where(
            SubjectPersonRow.operator_id == operator_id, SubjectPersonRow.ref == ref
        )
    )
    if person is None:
        return None
    rows = session.scalars(
        select(RelationshipComplaintRow)
        .where(
            RelationshipComplaintRow.operator_id == operator_id,
            RelationshipComplaintRow.subject_person_ref == person.ref,
        )
        .order_by(RelationshipComplaintRow.filed_at)
    ).all()
    items = []
    for row in rows:
        names = NameMap()
        names.token_for(row.filed_by)  # the complainant's identity, kept out of the export
        items.append(
            {
                "id": row.id,
                "filed_at": z_utc(row.filed_at),
                "category": row.category,
                "severity": row.severity,
                "status": row.status,
                "allegation": scrub_text(row.description, names),
                "outcome": scrub_text(row.resolution, names),
                "acknowledged_at": z_utc(row.acknowledged_at),
                "resolved_at": z_utc(row.resolved_at),
                "retention_until": z_utc(row.retention_until),
                "text_reduced_at": z_utc(row.pseudonymised_at),
                "ai_assisted_classification": bool(row.classification_ai_assisted),
            }
        )
    return {
        "subject_ref": person.ref,
        "display_name": person.display_name,
        "employer_vendor_id": person.employer_vendor_id,
        "registered_at": z_utc(person.created_at),
        "complaints": items,
        "count": len(items),
        "legal_basis": "Data Protection Act 2019 s.26 (right of access); s.25(g) (retention)",
        "complainant_identity_withheld": True,
        "unstructured_text_not_searched": (
            "Complaints that do not carry this subject reference are not included: there is "
            "no free-text name search in this system. See services/complaints.subject_access."
        ),
        "retention_note": (
            f"Complaint free text is reduced to its category and outcome {RETENTION_DAYS} days "
            "after filing (DPA 2019 s.25(g); §9.4). The category counts are kept for the "
            "operator's quarterly complaint statistics (Consumer Protection Regulations 2010 "
            "reg 7(13))."
        ),
    }


# ------------------------------------------------------------------------------ retention


@dataclass
class RetentionReport:
    """What one retention pass did (or would do, in the default dry-run posture)."""

    applied: bool = False
    matched: int = 0
    changed: int = 0
    refused: str = ""

    def summary(self) -> str:
        if self.refused:
            return f"complaint retention refused: {self.refused}"
        verb = "reduced" if self.applied else "would reduce"
        return f"complaint retention: {verb} {self.matched} description(s)"

    def as_tool(self) -> dict[str, Any]:
        return {
            "name": "complaints.pseudonymise_expired",
            "ok": not self.refused,
            "applied": self.applied,
            "matched": self.matched,
            "changed": self.changed,
            "refused": self.refused or None,
        }


def reduced_description(row: RelationshipComplaintRow, *, when: datetime, names: NameMap) -> str:
    """The text that replaces the allegation at 24 months: category plus outcome (§7.8.3).

    The outcome survives the reduction because it is the operator's record of what it did —
    the allegation is what it no longer needs. Both are run through the shared scrubber with
    the subject's name registered, so a name the reduction would otherwise carry forward is
    replaced rather than copied into the surviving text.
    """
    outcome = scrub_text(_clean(row.resolution), names) or "no outcome recorded"
    return (
        f"[free text reduced {when.date().isoformat()} — DPA 2019 s.25(g), §9.4] "
        f"category={row.category}; severity={row.severity}; status={row.status}; "
        f"outcome={outcome}"
    )


def pseudonymise_expired(
    session: Session,
    settings: AppSettings,
    *,
    now: datetime | None = None,
    apply: bool | None = None,
) -> RetentionReport:
    """Reduce the free text of every complaint past ``retention_until`` (§9.4, DPA s.25(g)).

    The row survives — §9.4's action for this class is *pseudonymise*, not delete — so the
    category counts behind the operator's quarterly statistics (Consumer Protection
    Regulations 2010 reg 7(13)) and the subject-access record of *that* a complaint existed
    both remain. The CA licence Condition 12.2 three-year floor does not apply: §9.4 confines
    that floor to the network/QoS class and this is not an operational record.

    **Posture.** ``apply`` defaults to the same two-key gate every other retention action in
    this system obeys — ``posture.dry_run: false`` in ``config/retention.yaml`` *and*
    ``HOUSEKEEPING_APPLY=true`` — read through ``services/housekeeping`` rather than
    reimplemented, so the document Legal signs governs this table too. A policy that fails
    validation refuses the whole pass rather than reducing anything on a half-read file.

    Idempotent: ``pseudonymised_at`` is set when the text is reduced and the query skips
    rows that carry it, so the second run of the day finds nothing and the text is never
    reduced twice.
    """
    from noc_agents.services import housekeeping  # local: keeps this module importable alone

    now = now or utcnow()
    report = RetentionReport()
    try:
        policy = housekeeping.load_policy()
        problems = housekeeping.validate_policy(policy)
        if problems:
            report.refused = "; ".join(problems)
            return report
        report.applied = housekeeping.applying(policy) if apply is None else bool(apply)
    except Exception as exc:  # noqa: BLE001 — an unreadable policy must never mean "delete"
        report.refused = f"retention policy unreadable ({type(exc).__name__})"
        return report

    rows = session.scalars(
        select(RelationshipComplaintRow).where(
            RelationshipComplaintRow.operator_id == settings.operator.operator_id,
            RelationshipComplaintRow.retention_until <= now,
            RelationshipComplaintRow.pseudonymised_at.is_(None),
        )
    ).all()
    for row in rows:
        report.matched += 1
        if not report.applied:
            continue
        names = _subject_name_map(
            session, operator_id=row.operator_id, refs=[row.subject_person_ref]
        )
        names.token_for(row.filed_by)
        row.description = reduced_description(row, when=now, names=names)
        if row.resolution:
            row.resolution = scrub_text(row.resolution, names)
        row.pseudonymised_at = now
        row.updated_at = now
        report.changed += 1
    if report.changed:
        audit(
            session,
            operator_id=settings.operator.operator_id,
            actor="housekeeping",
            action=AUDIT_RETENTION,
            entity_id="*",
            rationale="DPA 2019 s.25(g); §9.4 relationship_complaints, 24 months, pseudonymise",
            payload={"matched": report.matched, "changed": report.changed},
        )
    return report


# ---------------------------------------------------------------------- manager reminders


@dataclass
class ReminderReport:
    """What one reminder sweep queued. ``groups`` is managers (plus the unassigned bucket)."""

    groups: int = 0
    complaints: int = 0
    queued: int = 0
    already_queued: int = 0

    def summary(self) -> str:
        return (
            f"complaint reminders: {self.complaints} overdue across {self.groups} manager(s); "
            f"{self.queued} queued, {self.already_queued} already queued"
        )

    def as_tool(self) -> dict[str, Any]:
        return {
            "name": "complaints.send_due_reminders",
            "ok": True,
            "groups": self.groups,
            "complaints": self.complaints,
            "queued": self.queued,
            "already_queued": self.already_queued,
        }


UNASSIGNED = "UNASSIGNED"
REMINDER_AUDIENCE = "MANAGEMENT"
#: The ``recipients_ref`` the reminder carries. ``services/notify.resolve_recipients`` refuses
#: to send a ref the operator profile does not declare rather than falling back to the demo
#: mailbox, which is exactly the behaviour this lane wants: a reminder that a complaint is
#: overdue, delivered to whichever inbox happens to be configured for demos, is a disclosure
#: that something confidential exists to people who have no business knowing it. The operator
#: adds ``complaints.recipients.MANAGEMENT`` to ``notification_recipients`` in
#: ``config/operators/<op>.yaml`` before switching the lane on; until they do, the reminder is
#: queued, refused at dispatch with that message, and visible in the outbox.
REMINDER_RECIPIENTS_REF = "complaints.recipients.MANAGEMENT"


def overdue_complaints(
    session: Session, *, operator_id: str, now: datetime | None = None
) -> list[RelationshipComplaintRow]:
    """Live complaints whose follow-up date has passed, oldest first.

    Read directly rather than through :func:`visible_complaints`: this is the scheduler, not
    a person, and it never renders a complaint — it counts them. The one thing it must not
    do is put the contents anywhere, which :func:`reminder_body` is responsible for.
    """
    now = now or utcnow()
    return list(
        session.scalars(
            select(RelationshipComplaintRow)
            .where(
                RelationshipComplaintRow.operator_id == operator_id,
                RelationshipComplaintRow.status.notin_(sorted(TERMINAL_STATUSES)),
                RelationshipComplaintRow.follow_up_due_at.isnot(None),
                RelationshipComplaintRow.follow_up_due_at <= now,
            )
            .order_by(RelationshipComplaintRow.follow_up_due_at)
        ).all()
    )


def reminder_body(rows: Sequence[RelationshipComplaintRow], *, manager: str, now: datetime) -> str:
    """The reminder text: a count and opaque references. **Nothing about any complaint.**

    §9.5 forbids the breach record from quoting the breach; the same rule is applied to this
    message, because a reminder is a copy of whatever it quotes, sitting in an inbox that
    is backed up, forwarded and read on a phone in a matatu. So: no category (a category is
    an allegation — "UNSAFE_PRACTICE" says what someone is accused of), no severity, no
    vendor, no subject, no incident, and obviously not a word of the description. A
    reference and a due date are enough to make someone open the queue, which is the only
    place the complaint is shown, behind the RBAC that belongs to it.
    """
    who = "unassigned complaints" if manager == UNASSIGNED else f"complaints assigned to {manager}"
    lines = [
        f"{len(rows)} confidential complaint(s) past their follow-up date ({who}).",
        "",
        "This reminder deliberately contains no detail: open the complaint queue to read them.",
        "",
        "Reference                             Follow-up was due",
    ]
    for row in rows:
        lines.append(f"{row.id}  {z_utc(row.follow_up_due_at)}")
    lines += [
        "",
        f"Generated {z_utc(now)} by the complaint intake (§7.8.3).",
        "Confidential: these records are visible only to the roles §9.3 lists, and never to "
        "the person a complaint is about.",
    ]
    return "\n".join(lines)


def send_due_reminders(
    session: Session, settings: AppSettings, *, now: datetime | None = None
) -> ReminderReport:
    """Queue one reminder per manager per day for complaints past ``follow_up_due_at``.

    Grouped by manager and keyed by the day, so a complaint that stays overdue for a week
    produces one reminder a day to one person, not one per complaint per tick: the outbox's
    ``INSERT OR IGNORE`` on ``idempotency_key`` is the deduplication, so no new column and
    no "last reminded at" state can drift out of step with what was actually sent.

    Nothing is transmitted here. The row goes to the transactional outbox — the one sender —
    and the dispatcher takes it from there, which also means a reminder cannot escape while
    ``OUTBOX_DISPATCH_ENABLED`` is off. Recipients come from the operator's configured
    notification addresses: per-manager addressing arrives with §7.9.1's ``send_email(to=…)``
    signature change, which belongs to the channels lane, so the audience is recorded in the
    payload and the message stays content-free either way.
    """
    now = now or utcnow()
    operator_id = settings.operator.operator_id
    report = ReminderReport()
    rows = overdue_complaints(session, operator_id=operator_id, now=now)
    if not rows:
        return report
    groups: dict[str, list[RelationshipComplaintRow]] = {}
    for row in rows:
        groups.setdefault(_clean(row.assigned_manager) or UNASSIGNED, []).append(row)
    report.groups = len(groups)
    report.complaints = len(rows)
    day = now.date().isoformat()
    for manager, items in sorted(groups.items()):
        key = f"complaint.reminder|{operator_id}|{manager}|{day}"
        before = session.scalar(
            select(func.count()).select_from(OutboxRow).where(OutboxRow.idempotency_key == key)
        )
        outbox.enqueue(
            session,
            kind=outbox.EMAIL,
            idempotency_key=key,
            operator_id=operator_id,
            payload={
                "operator_id": operator_id,
                "incident_number": None,
                "audience": REMINDER_AUDIENCE,
                "recipients_ref": REMINDER_RECIPIENTS_REF,
                "manager": manager,
                "subject": f"[NOC] {len(items)} complaint(s) awaiting follow-up",
                "body": reminder_body(items, manager=manager, now=now),
                # Ids only — the same rule §9.5 applies to a transfer record's payload.
                "complaint_ids": [row.id for row in items],
                "count": len(items),
            },
        )
        if before:
            report.already_queued += 1
        else:
            report.queued += 1
    return report


# ------------------------------------------------------------------------------- the job


JOB_NAME = "complaints_followup"
AGENT = "ComplaintIntakeAgent"
GRAPH_NAME = "complaints"
INTERVAL_S = 3600  # hourly: the unit of the follow-up clock is a working day


def followup_job(session: Session, settings: AppSettings) -> JobResult:
    """``complaints_followup``: queue overdue-manager reminders, then run the retention pass.

    Does not commit — the scheduler's runner does, so a failure anywhere in the tick leaves
    neither a reminder nor a half-reduced description behind.
    """
    if not complaints_enabled():
        # Re-checked here and not only on the card, so a card wired into the loop with the
        # flag unset is inert rather than merely un-scheduled (the ``services/pir.py`` rule).
        return JobResult(
            summary="COMPLAINTS_ENABLED=false — no reminders, no retention pass",
            rationale="feature flag off",
        )
    reminders = send_due_reminders(session, settings)
    retention = pseudonymise_expired(session, settings)
    return JobResult(
        summary=f"{reminders.summary()}; {retention.summary()}",
        rationale=(
            "Follow-up reminders carry counts and references only (§9.5 rule applied to "
            "complaints); retention reduces free text at 24 months (DPA 2019 s.25(g), §9.4)"
        ),
        tools=(reminders.as_tool(), retention.as_tool()),
    )


#: NOT registered in ``scheduler/loop.py`` — that file belongs to another lane. Adding
#: ``services.complaints.COMPLAINTS_JOB`` to ``SCHEDULED_JOBS`` (lazily imported, like the
#: PIR card) is the whole change. ``default_enabled=False`` so ``/scheduler/status`` reports
#: the job as off while ``COMPLAINTS_ENABLED`` is unset rather than claiming it is enabled
#: and producing nothing.
COMPLAINTS_JOB = JobCard(
    JOB_NAME,
    INTERVAL_S,
    followup_job,
    ENABLED_ENV,
    AGENT,
    GRAPH_NAME,
    max_seconds=60,
    default_enabled=False,
)
