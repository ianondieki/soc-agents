"""Vendor routes (spec §7.6.3): ``GET /api/v1/vendors``, ``POST /api/v1/vendors`` (admin),
plus ``POST /api/v1/vendors/backfill`` to stamp ``incidents.vendor_id`` from ``msp_name``.

Behind ``SCORECARDS_ENABLED`` (default OFF): every route answers **404** while the flag is
off -- not 403 or 503 -- so the surface is byte-for-byte what it was before this lane
existed (an unknown path). The flag is read per request, like every other flag here.

Reads go through ``_owned``/``_get_owned`` (operator scoping, §8); writes are gated with
``require_role``. ``main.py`` includes this router; nothing here imports ``main``.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from noc_agents.api import auth
from noc_agents.api.auth import require_role
from noc_agents.api.deps import READERS, SUPERVISORS, _actor, _settings
from noc_agents.db.models import AuditRow, get_session
from noc_agents.services.vendors import (
    FLAG,
    backfill_incident_vendor_ids,
    create_vendor,
    lane_enabled,
    list_vendors,
    seed_vendors_from_contacts,
    vendor_out,
)

router = APIRouter(prefix="/api/v1", tags=["vendors"])


def require_lane() -> None:
    """Dependency: 404 while ``SCORECARDS_ENABLED`` is off (see the module docstring)."""
    if not lane_enabled():
        raise HTTPException(404, f"Not found ({FLAG} is off)")


class VendorIn(BaseModel):
    """Body of ``POST /api/v1/vendors``. ``active_from`` is the contract's commencement
    date -- the natural key's third part -- so a re-contracted vendor is a new row."""

    code: str
    display_name: str | None = None
    type: str = "MSP"
    active_from: date
    active_to: date | None = None
    contract_ref: str | None = None
    contacts: dict = {}


@router.get("/vendors", dependencies=[Depends(require_lane), Depends(require_role(*READERS))])
def list_vendors_route(active_only: bool = False) -> list[dict]:
    """This operator's vendors, seeded from ``cfg.msp_contacts`` on first sight.

    The seeding on a GET is deliberate and safe: the rows are a pure function of the
    operator profile, the insert is keyed on the natural key and never updates, so the
    call is idempotent and cannot destroy or alter anything a human wrote. Without it a
    fresh deployment shows an empty vendor list until someone remembers an admin step.
    """
    s = _settings()
    session = get_session()
    try:
        if seed_vendors_from_contacts(session, s.operator):
            session.commit()
        rows = list_vendors(session, s.operator.operator_id, as_of=date.today() if active_only else None)
        return [vendor_out(r) for r in rows]
    finally:
        session.close()


@router.post("/vendors", dependencies=[Depends(require_lane)])
def create_vendor_route(body: VendorIn, principal: auth.Principal = Depends(require_role("admin"))) -> dict:
    """Admin creates a vendor (§7.6.3). 400 on a bad type/code/window, 409 on a duplicate
    natural key. Written under the ACTIVE operator only -- a client cannot name another."""
    s = _settings()
    session = get_session()
    try:
        try:
            row = create_vendor(
                session,
                operator_id=s.operator.operator_id,
                code=body.code,
                display_name=body.display_name,
                type=body.type,
                active_from=body.active_from,
                active_to=body.active_to,
                contract_ref=body.contract_ref,
                contacts=body.contacts,
            )
        except LookupError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        session.add(
            AuditRow(
                operator_id=row.operator_id,
                actor=_actor(principal, None),
                action="vendor.created",
                entity_type="vendor",
                entity_id=row.id,
                rationale="",
                payload_json=str({"code": row.code, "type": row.type, "active_from": row.active_from.isoformat()}),
            )
        )
        session.commit()
        return {"ok": True, "vendor": vendor_out(row)}
    finally:
        session.close()


@router.post("/vendors/backfill", dependencies=[Depends(require_lane), Depends(require_role(*SUPERVISORS))])
def backfill_route() -> dict:
    """Stamp ``vendor_id`` on this operator's incidents that have a resolvable ``msp_name``
    and none yet. Idempotent; seeds the vendor rows first so a fresh database resolves.
    Returns how many incidents were stamped."""
    s = _settings()
    session = get_session()
    try:
        seed_vendors_from_contacts(session, s.operator)
        stamped = backfill_incident_vendor_ids(session, s.operator.operator_id)
        session.commit()
        return {"ok": True, "stamped": stamped}
    finally:
        session.close()
