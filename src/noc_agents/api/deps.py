"""Shared request-scoped helpers: RBAC allow-lists, the actor rule, operator scoping.

Lifted out of ``main.py`` verbatim when Phase 4 split the API across router modules.
``main.py`` imports these names and its existing routes are unchanged; a router module
imports from here instead of from ``main``, which would be a cycle (``main`` includes
the routers).

Nothing in this module may import ``noc_agents.main``.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select

from noc_agents.api import auth
from noc_agents.config import get_settings
from noc_agents.db.models import HitlTaskRow, IncidentBriefRow, IncidentRow


def _settings():
    """Always resolve current operator profile (not frozen at import)."""
    return get_settings()


# --- RBAC allow-lists (§7.0.5) -------------------------------------------------
# Inert while AUTH_DISABLED=true (the demo default): require_role() then returns
# the role-switcher principal and never rejects. "admin" is listed explicitly —
# the dependency grants it no implicit bypass.
SUPERVISORS: tuple[str, ...] = ("shift_supervisor", "duty_manager", "admin")
OPERATIONS: tuple[str, ...] = ("noc_analyst",) + SUPERVISORS
# Read surfaces: broad, because a field engineer or MSP coordinator needs to see the
# incident they are working. Still not "anyone with the port open" — see INGEST below.
READERS: tuple[str, ...] = OPERATIONS + ("management", "msp_coordinator", "field_engineer", "planning")
# The audit trail is the regulator-facing surface. Narrow it deliberately.
AUDIT_READERS: tuple[str, ...] = ("duty_manager", "management", "legal", "admin")
# Alarm ingest starts the whole 12-node lifecycle and writes incidents, runs, steps,
# audit rows and outbox rows. It was UNGATED: with auth fully enforced an anonymous
# POST still ran a complete lifecycle (found by the Phase 2 conformance audit, which
# forced AUTH_DISABLED=false + NOC_ENV=production and got 200). §9.3 restricts
# msp_coordinator and field_engineer to "notes only", so they are excluded here.
INGEST: tuple[str, ...] = OPERATIONS
# Vendor scorecards and QBR packs are commercial documents: management and the MSP
# coordinator read them, the wider operations floor does not need them (§7.6).
COMMERCIAL: tuple[str, ...] = ("management", "msp_coordinator", "duty_manager", "admin")


def _actor(principal: auth.Principal, claimed: str | None) -> str:
    """Who to RECORD as having taken an action.

    When the request is genuinely authenticated, the actor is the principal and the
    client's own claim is ignored — otherwise a supervisor with a valid cookie could
    approve a customer broadcast under any name they typed, and the approval trail
    (the entire point of the HITL gate) would be forgeable.

    With AUTH_DISABLED=true there is no identity to forge: the demo role switcher is a
    UI affordance, not an identity store, so the caller-supplied name is kept and the
    existing demo behaviour and tests are unchanged.
    """
    if principal.authenticated:
        return principal.display_name
    return (claimed or "").strip() or principal.display_name


# --- Operator scoping (spec §8) --------------------------------------------------
# One process serves one profile (OPERATOR_PROFILE), but every profile shares one
# DATABASE_URL: both operators' rows live in one file, so isolation is a property of
# each QUERY, not of the deployment. The helpers below are the only place the API
# builds the operator clause. Every read of an operator-owned table goes through them,
# so the WHERE clause -- never a check after the fetch -- is what keeps the other
# operator's rows out. A new route reuses them; a reviewer checks them here.
#
# Two table shapes:
#   * tables that carry ``operator_id`` (incidents, problems, agent_runs, audit_events,
#     shift_ledger, outbox, llm_calls) filter on the column directly;
#   * ``hitl_tasks`` and ``incident_briefs`` carry NO operator_id and are owned through
#     ``incident_id -> incidents.operator_id``. For hitl_tasks this is deliberate: every
#     task is written with ``incident_id=inc.id`` (agents/hitl.py, services/worklog_monitor.py)
#     against a NOT NULL foreign key, so the owner is already recorded once, on the
#     incident, and the join is always well-defined. A denormalised
#     ``hitl_tasks.operator_id`` would cost a schema change plus a backfill for a value
#     that is derivable, and add a second copy that can drift from the first. Do not
#     "optimise" the join into a column without weighing that.
#
# A Phase 4 table that hangs off an incident registers itself here rather than growing
# its own operator_id column; ``register_owned_via_incident`` is that door.
_OWNED_VIA_INCIDENT: dict[type, object] = {
    HitlTaskRow: HitlTaskRow.incident_id,
    IncidentBriefRow: IncidentBriefRow.incident_id,
}


def register_owned_via_incident(model: type, incident_id_column) -> None:
    """Declare that ``model`` is owned through ``incident_id_column -> incidents.operator_id``.

    Call at import time from the module that defines the model, so that ``_owned(model)``
    is scoped from the first query and a route cannot accidentally read an unscoped table.
    """
    _OWNED_VIA_INCIDENT[model] = incident_id_column


def _operator_scoped(stmt, model):
    """Restrict ``stmt``, whose FROM is ``model``, to the rows the active operator owns."""
    op_id = _settings().operator.operator_id
    via = _OWNED_VIA_INCIDENT.get(model)
    if via is not None:
        return stmt.join(IncidentRow, IncidentRow.id == via).where(IncidentRow.operator_id == op_id)
    return stmt.where(model.operator_id == op_id)


def _owned(model):
    """``SELECT model`` restricted to the active operator; add filters and ordering to it."""
    return _operator_scoped(select(model), model)


def _get_owned(session, model, row_id: str, *, what: str):
    """The ``model`` row with ``row_id`` if the active operator owns it, else 404.

    404, not 403: a 403 would confirm that the id exists in another operator's data, which
    is the very fact this scoping protects. 404 is indistinguishable from "no such id".
    """
    row = session.scalar(_owned(model).where(model.id == row_id))
    if row is None:
        raise HTTPException(404, f"{what} not found")
    return row
