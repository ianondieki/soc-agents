"""Confidential complaint intake routes (spec §7.8.2, §5.3.21; ``COMPLAINTS_ENABLED=false``).

Thin transport over ``services/complaints.py``: this module parses bodies, applies RBAC,
turns the service's verdicts into status codes and serialises rows. Every rule worth
arguing about — who may see what, the minimisation validator, the transitions, the
retention reduction — lives in the service, where it can be tested without a client and
reused by the scheduled job.

**The RBAC here is the product.** §9.3's complaints row reads
``file / view all / subject access``: ``noc_analyst`` and ``field_engineer`` file,
``shift_supervisor`` files and assigns, ``duty_manager`` and ``management`` view all,
``legal`` gets subject access, ``admin`` gets everything, and ``msp_coordinator`` and
``planning`` appear nowhere in the row — an MSP coordinator seeing complaints about MSPs is
the exact conflict of interest this table exists to manage. Two readings had to be settled
and both are written down rather than left in the allow-list:

* ``shift_supervisor`` is given read access to all complaints, because §9.3 gives them
  *assign* and nobody can hand out a complaint they cannot see. The alternative — a
  supervisor who may assign only ids somebody else reads out to them — is not a narrower
  permission, it is a broken one;
* ``duty_manager`` and ``management`` are **not** given *file*. §9.3's cell for them says
  "view all" and the matrix is read literally: filing is an act, not a consequence of
  reading. A manager who needs a complaint filed asks the supervisor, which is also the
  path that leaves a trail. Flag it to the operator if that is wrong for their floor; it is
  one tuple to change, and it is deliberately a tuple rather than an inference.

**Above every role, one rule.** The person a complaint is about can never read it. That is
not a check in this file: ``services.complaints.visible_complaints`` puts it in the WHERE
clause, every read here goes through it, and the answer for a complaint about yourself is
404 — the same answer as for an id that does not exist, because 403 would confirm to a
subject that a complaint about them is on file. It applies to ``admin`` too.

**The flag is a 404, not a 403** — the ``api/routers/pir.py`` precedent. With
``COMPLAINTS_ENABLED`` unset the lane is invisible: §7.8 ships it off, and a 403 would
announce that the feature exists and the caller is merely not allowed it.

**The production guard is registration-time.** §7.8.6: "the routes cannot exist while
``AUTH_DISABLED=true`` in production (§7.0.5 guard)". Not "answer 403" — *cannot exist*. So
the routes below are declared inside an ``if``, the way ``main.py`` declares the ledger
download, and with ``AUTH_DISABLED=true`` and ``NOC_ENV=production`` this router registers
no routes at all: an unauthenticated production deployment has no complaint surface to
reach, rather than one whose gates are all inert.

**Operator scoping.** ``relationship_complaints`` and ``subject_persons`` both carry
``operator_id``. The service builds the clause itself rather than taking a bare
``_owned(...)`` select, because the tenancy predicate and the "not about you" predicate
have to be in the same statement; ``_settings()`` here supplies the operator id, exactly as
``api/deps`` does.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from noc_agents.api import auth
from noc_agents.api.auth import require_role
from noc_agents.api.deps import _actor, _settings
from noc_agents.db.models import get_session
from noc_agents.db.models_complaints import RelationshipComplaintRow
from noc_agents.llm.client import get_llm_port, llm_enabled
from noc_agents.services import complaints as svc
from noc_agents.services.external_calls import TransferPaperworkMissing, record_transfer

router = APIRouter(prefix="/api/v1", tags=["complaints"])


# ----------------------------------------------------------------------------- the roles
#
# Spelled out here, not reused from ``api/deps``: the deps tuples (OPERATIONS, READERS,
# SUPERVISORS) are about the incident surface, and every one of them is wider than any cell
# in §9.3's complaints row. Borrowing one would silently give an MSP coordinator or a
# planner a complaint queue the moment somebody widened it for a dashboard.

#: §9.3 "file": the people who work with vendors and field engineers and therefore have
#: something to complain about. ``admin`` is listed explicitly — no implicit bypass anywhere.
FILERS: tuple[str, ...] = ("noc_analyst", "shift_supervisor", "field_engineer", "admin")
#: §9.3 "view all" (+ shift_supervisor, see the module docstring).
VIEW_ALL: tuple[str, ...] = ("shift_supervisor", "duty_manager", "management", "admin")
#: Anyone who may reach the surface at all. A filer sees their own filings; the rest see all.
COMPLAINT_ROLES: tuple[str, ...] = tuple(dict.fromkeys(FILERS + VIEW_ALL))
#: §9.3 "assign".
ASSIGNERS: tuple[str, ...] = ("shift_supervisor", "duty_manager", "management", "admin")
#: Acknowledging and resolving are the manager's acts; a supervisor who assigned it is
#: included because on a small NOC floor they are often the person who handles it.
HANDLERS: tuple[str, ...] = ASSIGNERS
#: §9.3 "subject access" — and nothing else. Legal exercises DPA s.26 on behalf of a person;
#: ``legal`` is deliberately NOT in COMPLAINT_ROLES, so the queue itself stays closed to them.
SUBJECT_ACCESS: tuple[str, ...] = ("legal", "admin")


def require_complaints_enabled() -> None:
    """404 the whole lane while ``COMPLAINTS_ENABLED`` is off (the default)."""
    if not svc.complaints_enabled():
        raise HTTPException(
            404, "the complaint intake is not enabled on this deployment (COMPLAINTS_ENABLED=false)"
        )


_ENABLED = Depends(require_complaints_enabled)

#: §7.0.5 / §7.8.6. Evaluated once, at import, exactly like ``main.py``'s own guard: the
#: routes are not registered at all in an unauthenticated production deployment.
PRODUCTION_GUARDED_ROUTES: tuple[str, ...] = (
    "POST /api/v1/complaints",
    "POST /api/v1/complaints/classify",
    "GET /api/v1/complaints",
    "GET /api/v1/complaints/stats",
    "GET /api/v1/complaints/subject-access/{ref}",
    "GET /api/v1/complaints/{complaint_id}",
    "POST /api/v1/complaints/{complaint_id}/assign",
    "POST /api/v1/complaints/{complaint_id}/acknowledge",
    "POST /api/v1/complaints/{complaint_id}/resolve",
    "POST /api/v1/complaints/{complaint_id}/withdraw",
)
_PRODUCTION_GUARD = auth.production_guard_active()


# ------------------------------------------------------------------------------- bodies


class ComplaintIn(BaseModel):
    """One filing. ``filed_by`` is a claim; with auth on the principal wins (``_actor``)."""

    subject_type: str
    category: str
    severity: str
    description: str
    filed_by: str | None = None
    vendor_id: str | None = None
    subject_role_token: str | None = None
    subject_person_ref: str | None = None
    incident_id: str | None = None
    evidence_note_ids: list[str] = []
    #: The filer's own statement that they accepted a model's suggested category/severity.
    #: A disclosure flag, not a decision record (§7.8.1 ``classification_ai_assisted``): the
    #: row is filed by the human either way.
    classification_ai_assisted: bool = False


class ClassifyIn(BaseModel):
    text: str


class AssignIn(BaseModel):
    manager: str
    rationale: str | None = None


class AcknowledgeIn(BaseModel):
    note: str | None = None


class ResolveIn(BaseModel):
    resolution: str


class WithdrawIn(BaseModel):
    reason: str | None = None


# ------------------------------------------------------------------------------ helpers


def _operator_id() -> str:
    return _settings().operator.operator_id


def _sees_all(principal: auth.Principal) -> bool:
    """§7.8.2: "own for engineers; all for duty_manager/management"."""
    return principal.role in VIEW_ALL


def _fetch(session, complaint_id: str, principal: auth.Principal) -> RelationshipComplaintRow:
    """One complaint this caller may see, or 404.

    404 and never 403, for two different reasons that happen to agree: another operator's
    row must not be confirmed to exist (``api/deps._get_owned``), and a complaint *about the
    caller* must not be confirmed to exist to the caller. The second is the one that matters
    here, and it is why this helper never distinguishes "no such id" from "not yours".
    """
    actor = _actor(principal, None)
    row = svc.get_visible(
        session,
        complaint_id,
        operator_id=_operator_id(),
        actor=actor,
        all_complaints=_sees_all(principal),
    )
    if row is None:
        raise HTTPException(404, "complaint not found")
    return row


def _guard_422(problems: list[str]) -> None:
    """Every failure at once, in the order the validator found them."""
    if problems:
        raise HTTPException(422, "; ".join(problems))


if _PRODUCTION_GUARD:
    auth.log_production_guard(PRODUCTION_GUARDED_ROUTES)
else:

    # ------------------------------------------------------------------------- filing

    @router.post("/complaints", dependencies=[_ENABLED])
    def file_complaint(
        body: ComplaintIn, principal: auth.Principal = Depends(require_role(*FILERS))
    ) -> dict:
        """File one complaint. 422 lists every problem at once; nothing is echoed back.

        The assistant never reaches this route (§5.3.21, autonomy A2): a complaint is filed
        by a named human confirming a form, and ``classification_ai_assisted`` records only
        that a model suggested the category they accepted.
        """
        actor = _actor(principal, body.filed_by)
        session = get_session()
        try:
            operator_id = _operator_id()
            _guard_422(
                svc.validate_complaint(
                    session,
                    operator_id=operator_id,
                    filed_by=actor,
                    subject_type=body.subject_type,
                    vendor_id=body.vendor_id,
                    subject_role_token=body.subject_role_token,
                    subject_person_ref=body.subject_person_ref,
                    category=body.category,
                    severity=body.severity,
                    description=body.description,
                    incident_id=body.incident_id,
                )
            )
            row = svc.file_complaint(
                session,
                _settings(),
                filed_by=actor,
                subject_type=body.subject_type,
                category=body.category,
                severity=body.severity,
                description=body.description,
                vendor_id=body.vendor_id,
                subject_role_token=body.subject_role_token,
                subject_person_ref=body.subject_person_ref,
                incident_id=body.incident_id,
                evidence_note_ids=body.evidence_note_ids,
                classification_ai_assisted=body.classification_ai_assisted,
            )
            session.flush()
            svc.audit(
                session,
                operator_id=operator_id,
                actor=actor,
                action=svc.AUDIT_FILED,
                entity_id=row.id,
                rationale="complaint filed",
                # Ids, category and status only — never the description (§9.5's rule).
                payload={
                    "category": row.category,
                    "severity": row.severity,
                    "subject_type": row.subject_type,
                    "ai_assisted": int(row.classification_ai_assisted or 0),
                },
            )
            session.commit()
            return svc.complaint_out(row)
        finally:
            session.close()

    @router.post("/complaints/classify", dependencies=[_ENABLED])
    def classify(
        body: ClassifyIn, principal: auth.Principal = Depends(require_role(*FILERS))
    ) -> dict:
        """Suggest a category and severity for typed text. **Files nothing.**

        DPA 2019 s.35 and General Regulations reg 22: no decision about a person may rest on
        automated processing alone. What comes back is a draft carrying
        ``advisory_only: true`` and the disclosure line; the caller then submits the form
        themselves, or does not.

        The hosted model is used only when ``LLM_ENABLED`` is on *and* the reg 41(2)
        transfer record can be written (§7.0.10 gate). A missing TIA/DPIA is not an error
        here: the deterministic keyword classifier answers instead and the response says so
        in ``source``, because a complaint form that fails because Legal has not filed
        paperwork is a complaint that does not get filed.
        """
        actor = _actor(principal, None)
        text = (body.text or "").strip()
        if not text:
            raise HTTPException(422, "text is required")
        if len(text) > svc.MAX_DESCRIPTION_CHARS:
            raise HTTPException(
                422,
                f"text must be at most {svc.MAX_DESCRIPTION_CHARS} characters "
                f"(DPA 2019 s.25 minimisation); it is {len(text)}",
            )
        session = get_session()
        try:
            operator_id = _operator_id()
            port = None
            transfer_id = None
            if llm_enabled():
                port = get_llm_port()
                if port is not None:
                    try:
                        transfer = record_transfer(
                            session,
                            recipient="Anthropic API (claude-opus-5)",
                            recipient_country="US",
                            justification="complaint category suggestion (advisory; DPA s.35 human decides)",
                            data_description="scrubbed complaint text typed by the filer; no MSISDN/e-mail",
                            actor=actor,
                            actor_role=principal.role,
                            incident_id=None,
                            residency="abroad",
                        )
                        transfer_id = transfer.id
                    except TransferPaperworkMissing:
                        port = None  # no paperwork, no transfer: fall back, do not fail
            draft = svc.classify(text, port=port)
            svc.audit(
                session,
                operator_id=operator_id,
                actor=actor,
                action=svc.AUDIT_CLASSIFIED,
                entity_id="draft",
                rationale="classification suggested; nothing filed",
                payload={
                    "source": draft.source,
                    "category": draft.category,
                    "severity": draft.severity,
                    "transfer_record_id": transfer_id,
                },
            )
            session.commit()
            return draft.as_dict()
        finally:
            session.close()

    # -------------------------------------------------------------------------- reads
    #
    # ``/complaints/stats`` and ``/complaints/subject-access/{ref}`` are declared BEFORE
    # ``/complaints/{complaint_id}``: FastAPI resolves in registration order, so the
    # parametric route would otherwise swallow them and 404 for a complaint called "stats".

    @router.get("/complaints", dependencies=[_ENABLED])
    def list_complaints(
        status: str | None = None,
        category: str | None = None,
        vendor_id: str | None = None,
        month: str | None = None,
        limit: int = 100,
        principal: auth.Principal = Depends(require_role(*COMPLAINT_ROLES)),
    ) -> list[dict]:
        """The caller's complaint queue. Descriptions are NOT in the list payload.

        §7.8.2's filters (vendor / category / month) plus status. ``month`` is ``YYYY-MM``
        and filters on ``filed_at``, which is what the quarterly statistics are built from.
        """
        actor = _actor(principal, None)
        session = get_session()
        try:
            operator_id = _operator_id()
            stmt = svc.visible_complaints(
                session, operator_id=operator_id, actor=actor, all_complaints=_sees_all(principal)
            )
            if status:
                stmt = stmt.where(RelationshipComplaintRow.status == status.upper())
            if category:
                stmt = stmt.where(RelationshipComplaintRow.category == category.upper())
            if vendor_id:
                stmt = stmt.where(RelationshipComplaintRow.vendor_id == vendor_id)
            if month:
                try:
                    year, mon = (int(part) for part in month.split("-", 1))
                except ValueError:
                    raise HTTPException(422, "month must be YYYY-MM") from None
                start = _month_start(year, mon)
                stmt = stmt.where(
                    RelationshipComplaintRow.filed_at >= start,
                    RelationshipComplaintRow.filed_at < _month_start(*_next_month(year, mon)),
                )
            rows = session.scalars(
                stmt.order_by(RelationshipComplaintRow.filed_at.desc()).limit(max(1, min(limit, 500)))
            ).all()
            svc.audit(
                session,
                operator_id=operator_id,
                actor=actor,
                action=svc.AUDIT_LISTED,
                entity_id="*",
                rationale="complaint queue listed",
                payload={"count": len(rows), "scope": "all" if _sees_all(principal) else "own"},
            )
            session.commit()
            # include_description=False: a queue is read over shoulders in an open-plan NOC.
            return [svc.complaint_out(row, include_description=False) for row in rows]
        finally:
            session.close()

    @router.get("/complaints/stats", dependencies=[_ENABLED])
    def complaint_stats(
        principal: auth.Principal = Depends(require_role(*COMPLAINT_ROLES)),
    ) -> dict:
        """Counts only (§7.8.2), over the caller's own visible set.

        These counts are the raw material for the operator's quarterly complaint statistics
        (Consumer Protection Regulations 2010 reg 7(13)); the export route itself is not in
        this lane. They are also the only complaint surface that never reveals an id.
        """
        actor = _actor(principal, None)
        session = get_session()
        try:
            return svc.stats(
                session,
                operator_id=_operator_id(),
                actor=actor,
                all_complaints=_sees_all(principal),
            )
        finally:
            session.close()

    @router.get("/complaints/subject-access/{ref}", dependencies=[_ENABLED])
    def subject_access(
        ref: str, principal: auth.Principal = Depends(require_role(*SUBJECT_ACCESS))
    ) -> dict:
        """DPA 2019 s.26: everything held about one subject person. ``legal``/``admin`` only.

        Read scoped to the active operator and audited: an access request answered from this
        system leaves a record that it was answered, which is what the operator needs when
        the ODPC asks how it handles s.26 requests.

        The complainant's identity is withheld and the export says so; what the route cannot
        find (a complaint that carries no subject ref) is also stated in the payload rather
        than left for the reader to assume. See ``services.complaints.subject_access``.
        """
        actor = _actor(principal, None)
        session = get_session()
        try:
            operator_id = _operator_id()
            export = svc.subject_access(session, operator_id=operator_id, ref=ref)
            if export is None:
                raise HTTPException(404, "subject not found")
            svc.audit(
                session,
                operator_id=operator_id,
                actor=actor,
                action=svc.AUDIT_SUBJECT_ACCESS,
                entity_id=ref,
                rationale="DPA 2019 s.26 access request answered",
                payload={"complaints": export["count"]},
            )
            session.commit()
            return export
        finally:
            session.close()

    @router.get("/complaints/{complaint_id}", dependencies=[_ENABLED])
    def get_complaint(
        complaint_id: str, principal: auth.Principal = Depends(require_role(*COMPLAINT_ROLES))
    ) -> dict:
        """One complaint in full. Opening it is a deliberate act and it writes an audit row."""
        actor = _actor(principal, None)
        session = get_session()
        try:
            row = _fetch(session, complaint_id, principal)
            svc.audit(
                session,
                operator_id=_operator_id(),
                actor=actor,
                action=svc.AUDIT_VIEWED,
                entity_id=row.id,
                rationale="complaint opened",
                payload={"status": row.status, "role": principal.role},
            )
            session.commit()
            return svc.complaint_out(row)
        finally:
            session.close()

    # ------------------------------------------------------------------------- writes

    @router.post("/complaints/{complaint_id}/assign", dependencies=[_ENABLED])
    def assign_complaint(
        complaint_id: str,
        body: AssignIn,
        principal: auth.Principal = Depends(require_role(*ASSIGNERS)),
    ) -> dict:
        """Hand the complaint to a named manager (§9.3 "assign").

        Refused when that manager is the subject of this complaint — the write-side half of
        the rule ``visible_complaints`` enforces on reads.
        """
        actor = _actor(principal, None)
        session = get_session()
        try:
            row = _fetch(session, complaint_id, principal)
            _guard_422(svc.assign(session, row, manager=body.manager))
            svc.audit(
                session,
                operator_id=_operator_id(),
                actor=actor,
                action=svc.AUDIT_ASSIGNED,
                entity_id=row.id,
                rationale=(body.rationale or "")[:500],
                payload={"assigned_manager": row.assigned_manager, "status": row.status},
            )
            session.commit()
            return svc.complaint_out(row)
        finally:
            session.close()

    @router.post("/complaints/{complaint_id}/acknowledge", dependencies=[_ENABLED])
    def acknowledge_complaint(
        complaint_id: str,
        body: AcknowledgeIn,
        principal: auth.Principal = Depends(require_role(*HANDLERS)),
    ) -> dict:
        """OPEN → ACKNOWLEDGED, with the follow-up clock restarted from now.

        Consumer Protection Regulations 2010 reg 7 expects complaints to be acknowledged
        against a reference; the reference is the complaint id and the acknowledgement is a
        state with a timestamp, so "was it acknowledged?" is answerable without reading a
        note.
        """
        actor = _actor(principal, None)
        session = get_session()
        try:
            row = _fetch(session, complaint_id, principal)
            _guard_422(svc.acknowledge(session, row, actor=actor))
            svc.audit(
                session,
                operator_id=_operator_id(),
                actor=actor,
                action=svc.AUDIT_ACKNOWLEDGED,
                entity_id=row.id,
                rationale=(body.note or "")[:500],
                payload={"status": row.status, "follow_up_due_at": row.follow_up_due_at},
            )
            session.commit()
            return svc.complaint_out(row)
        finally:
            session.close()

    @router.post("/complaints/{complaint_id}/resolve", dependencies=[_ENABLED])
    def resolve_complaint(
        complaint_id: str,
        body: ResolveIn,
        principal: auth.Principal = Depends(require_role(*HANDLERS)),
    ) -> dict:
        """Close the complaint with a written outcome, which is mandatory.

        The outcome is what survives the 24-month reduction (§9.4), so it is also the
        operator's own record that it acted — an empty one costs them that evidence.
        """
        actor = _actor(principal, None)
        session = get_session()
        try:
            row = _fetch(session, complaint_id, principal)
            _guard_422(svc.resolve(session, row, actor=actor, resolution=body.resolution))
            svc.audit(
                session,
                operator_id=_operator_id(),
                actor=actor,
                action=svc.AUDIT_RESOLVED,
                entity_id=row.id,
                # The rationale is a fixed string, not the resolution text: audit_events is
                # readable by more roles than the complaint is (§9.3), so copying the outcome
                # into it would widen the audience for the words themselves.
                rationale="complaint resolved",
                payload={"status": row.status, "resolved_at": row.resolved_at},
            )
            session.commit()
            return svc.complaint_out(row)
        finally:
            session.close()

    @router.post("/complaints/{complaint_id}/withdraw", dependencies=[_ENABLED])
    def withdraw_complaint(
        complaint_id: str,
        body: WithdrawIn,
        principal: auth.Principal = Depends(require_role(*COMPLAINT_ROLES)),
    ) -> dict:
        """The filer takes their own complaint back (``WITHDRAWN``).

        **Not in §7.8.2's route list.** ``WITHDRAWN`` is in §7.8.1's shipped status
        vocabulary with no route able to reach it, which would mean a state machine that
        cannot be completed — the same omission ``api/routers/pir.py`` records for action
        items. Only the filer may use it: a manager who could withdraw a complaint on
        someone else's behalf is a manager who can make one disappear, and the reason people
        do not report things is the belief that reporting them changes nothing.
        """
        actor = _actor(principal, None)
        session = get_session()
        try:
            row = _fetch(session, complaint_id, principal)
            if row.filed_by != actor:
                raise HTTPException(403, "only the person who filed a complaint may withdraw it")
            _guard_422(svc.withdraw(row, reason=body.reason or ""))
            svc.audit(
                session,
                operator_id=_operator_id(),
                actor=actor,
                action=svc.AUDIT_WITHDRAWN,
                entity_id=row.id,
                rationale="withdrawn by the complainant",
                payload={"status": row.status},
            )
            session.commit()
            return svc.complaint_out(row)
        finally:
            session.close()


# ------------------------------------------------------------------------- month helpers


def _month_start(year: int, month: int) -> datetime:
    if not 1 <= month <= 12:
        raise HTTPException(422, "month must be YYYY-MM with a month between 01 and 12")
    return datetime(year, month, 1)


def _next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)
