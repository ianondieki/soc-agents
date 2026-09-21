"""Message-template approval route (spec §6.3, §6.4, §9.3) -- conformance item C-09.

    PUT /api/v1/templates/{id}/status    {status, signoff_ref?, reason?}    admin

Thin transport over ``services/templates.py:TemplateRegistry.set_status``, which is the model
and already enforces what must be true of the row whoever the caller is: the status is one of
§6.3's five, ``APPROVED`` names an actor, a Kiswahili row needs a legal/management reviewer and
a recorded sign-off, and leaving ``APPROVED`` keeps the approval history. This module adds what
only a route can know: who is asking, and whether this route is the right door for the row.

**Who.** §9.3's platform row ("Templates status, outbox retry, scheduler run, MCP status,
agents") gives the write to ``admin`` alone. The approver's NAME is the principal's
(``_actor(principal, None)``). The body has no field for it and refuses a field it does not
know (``extra="forbid"``), so a client that believes it is naming the approver gets a 422
instead of being silently ignored. With ``AUTH_DISABLED=true`` the principal is the demo role
switcher, exactly as for every other write.

**Kiswahili (§6.4 hard rule).** The reviewer ROLE comes from the principal as well, never from
the body: a body field would let anyone type ``legal``. So a ``sw`` row reaches ``APPROVED``
only when the caller IS legal or management and states a ``signoff_ref`` (docs/SIGNOFF.md).
Read next to §9.3 that is a real conflict, and it is left standing on purpose: with auth
enforced this route admits only admin, admin is not a reviewer role, so no ``sw`` template can
be approved through it. That fails closed and costs nothing today (no ``sw`` row is seeded,
and D4 has not named the reviewer). Which way to resolve it is the owner's decision.

**WhatsApp.** §6.3: "WhatsApp templates mirror Meta's status." A hand-set status on a WHATSAPP
row would make the mirror lie (a template Meta has PAUSED would read APPROVED here), so the
route governs the operator-approved channels only and answers 409 for any other. It is an
allow-list, so a channel added later is refused until someone decides who approves it.

**Idempotent, as a PUT should be.** Asking for the status a row already has changes nothing and
answers 200 with ``changed: false``. That matters most for ``APPROVED``: running ``set_status``
again would overwrite ``approved_by``/``approved_at`` with the second caller and erase who
actually approved the words. A real re-approval (PAUSED -> APPROVED) does name the new approver,
and the audit row keeps the previous one.

**Audit.** Every change writes ``AuditRow(action="template.<verb>")`` with the transition, the
caller's role and the sign-off reference. The table has no column for the last two, so the
audit row is where "who signed this off, in what capacity, recorded where" is kept.

**Scoping.** ``message_templates`` carries ``operator_id``: the row is resolved through
``_get_owned`` (another operator's id is a 404, never a 403) before the registry sees it, and
the registry re-checks it against its own operator.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from noc_agents.api import auth
from noc_agents.api.auth import require_role
from noc_agents.api.deps import _actor, _get_owned, _settings
from noc_agents.db.models import AuditRow, MessageTemplateRow, get_session
from noc_agents.services.templates import (
    APPROVAL_STATUSES,
    REVIEWED_LANGUAGES,
    REVIEWER_ROLES,
    TemplateError,
    TemplateNotFound,
    TemplateRegistry,
)

router = APIRouter(prefix="/api/v1", tags=["templates"])

#: §9.3 platform row, write side: "all" for admin, "read" for the four internal roles, "—" for
#: everyone else. Admin is listed explicitly, as everywhere: ``require_role`` has no bypass.
TEMPLATE_APPROVERS: tuple[str, ...] = ("admin",)

#: §6.3: "Email/SMS/in-app templates are APPROVED by the operator (a named human via
#: PUT /api/v1/templates/{id}/status); WhatsApp templates mirror Meta's status."
OPERATOR_APPROVED_CHANNELS: tuple[str, ...] = ("EMAIL", "SMS", "INAPP")

#: ``AuditRow.action`` per target status, in the past tense the other governance routes use
#: (``pir.published``, ``scorecard.finalised``).
_AUDIT_ACTIONS: dict[str, str] = {
    "DRAFT": "template.returned_to_draft",
    "SUBMITTED": "template.submitted",
    "APPROVED": "template.approved",
    "REJECTED": "template.rejected",
    "PAUSED": "template.paused",
}
if set(_AUDIT_ACTIONS) != set(APPROVAL_STATUSES):  # at import, not as a KeyError mid-approval
    raise RuntimeError("_AUDIT_ACTIONS must map exactly the registry's APPROVAL_STATUSES")


class TemplateStatusIn(BaseModel):
    """The requested transition. There is deliberately no approver field (module docstring)."""

    model_config = ConfigDict(extra="forbid")

    status: str
    signoff_ref: str | None = None  # §6.4: where a sw sign-off is recorded (docs/SIGNOFF.md#...)
    reason: str | None = None  # free text, kept on the audit row


def template_out(row: MessageTemplateRow) -> dict[str, Any]:
    """The row's identity and approval state. The body is not echoed: the caller has the id."""
    return {
        "id": row.id,
        "channel": row.channel,
        "template_key": row.template_key,
        "language": row.language,
        "version": row.version,
        "approval_status": row.approval_status,
        "approved_by": row.approved_by,
        "approved_at": row.approved_at,
        "updated_at": row.updated_at,
    }


@router.put("/templates/{template_id}/status")
def set_template_status(
    template_id: str,
    body: TemplateStatusIn,
    principal: auth.Principal = Depends(require_role(*TEMPLATE_APPROVERS)),
) -> dict:
    """Move one template to ``status``. 404 foreign/unknown, 409 WhatsApp, 403 sw by a non-reviewer."""
    status = (body.status or "").strip().upper()
    if status not in APPROVAL_STATUSES:
        raise HTTPException(422, f"status must be one of {list(APPROVAL_STATUSES)}")
    actor = _actor(principal, None)
    session = get_session()
    try:
        row = _get_owned(session, MessageTemplateRow, template_id, what="template")
        if row.channel not in OPERATOR_APPROVED_CHANNELS:
            raise HTTPException(
                409,
                f"a {row.channel} template's status mirrors the provider's own review (§6.3) and is not "
                f"set by hand; this route governs {list(OPERATOR_APPROVED_CHANNELS)}",
            )
        previous_status = row.approval_status
        if status == previous_status:
            # Nothing to do, and nothing to write: see "Idempotent" in the module docstring.
            return {"ok": True, "changed": False, "previous_status": previous_status, "template": template_out(row)}
        if status == "APPROVED" and row.language in REVIEWED_LANGUAGES and principal.role not in REVIEWER_ROLES:
            # A 403 rather than the registry's 422: the refusal is about who is asking, and no
            # body the caller could send would change it.
            raise HTTPException(
                403,
                f"a {row.language!r} template may only be approved by a reviewer whose role is one of "
                f"{sorted(REVIEWER_ROLES)} (§6.4 hard rule); the caller's role is {principal.role!r}",
            )
        previous_by, previous_at = row.approved_by, row.approved_at
        registry = TemplateRegistry(session, _settings().operator.operator_id)
        try:
            registry.set_status(row, status, actor=actor, reviewer_role=principal.role, signoff_ref=body.signoff_ref)
        except TemplateNotFound as exc:  # unreachable after _get_owned; mapped so it can never be a 500
            session.rollback()
            raise HTTPException(404, "template not found") from exc
        except TemplateError as exc:  # no sign-off, an anonymous approval: the registry's own rules
            session.rollback()
            raise HTTPException(422, str(exc)) from exc
        session.add(
            AuditRow(
                operator_id=_settings().operator.operator_id,
                actor=actor,
                action=_AUDIT_ACTIONS[status],
                entity_type="message_template",
                entity_id=row.id,
                rationale=(body.reason or "").strip(),
                payload_json=json.dumps(
                    {
                        "channel": row.channel,
                        "template_key": row.template_key,
                        "language": row.language,
                        "version": row.version,
                        "from": previous_status,
                        "to": status,
                        "role": principal.role,
                        "signoff_ref": (body.signoff_ref or "").strip() or None,
                        "previous_approved_by": previous_by,
                        "previous_approved_at": previous_at,
                    },
                    default=str,
                ),
            )
        )
        session.commit()
        return {"ok": True, "changed": True, "previous_status": previous_status, "template": template_out(row)}
    finally:
        session.close()
