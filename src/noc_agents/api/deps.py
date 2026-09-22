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
from noc_agents.db.models import IncidentBriefRow, IncidentRow


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

# --- The §9.3 allow-lists the A-02..A-05 round added ------------------------------
# First declared in main.py, because this file had another owner that round, and moved
# here when a cross-lane review found what that cost: a router cannot import main, so the
# lane routers kept gating row-1 reads with READERS, and ``legal`` -- which §9.3 gives R --
# got 200 from main.py's /signals/weather/regions and 403 from api/routers/signals.py's
# /signals beside it. main.py re-imports every name below, so ``main.PLATFORM_READERS`` and
# the rest stay importable (tests/unit/test_outbox_admin.py pins that one).

#: §9.3 row 1's READ column ("ingest, notes, timeline, workflow, signals read"): READERS plus
#: ``legal``, which the row gives R. Every incident-surface and signals read uses it, list
#: forms included -- a legal reader who could open an incident but not find it in the list
#: would be reading by guessed id. READERS itself is left as it is on purpose: every lane
#: router gates on it, and whether each of THOSE rows gives legal R is a question per §9.3
#: row, not a rename (contracts already answers it locally with READERS + legal).
INCIDENT_READERS: tuple[str, ...] = READERS + ("legal",)

#: §9.3 row 1, "notes only" for msp_coordinator and field_engineer: a work note is the ONE
#: thing those two roles may WRITE -- the vendor's progress update and the field engineer's
#: "generator refuelled" are the two notes this system exists to collect. ``management``,
#: ``planning`` and ``legal`` hold R on that row, not R/W, so they are absent: a note is
#: evidence, and it can also declare the service restored
#: (``services.lifecycle.note_declares_restored``), which sets the restore time MTTR and the
#: restore SLA are measured to.
NOTE_AUTHORS: tuple[str, ...] = OPERATIONS + ("msp_coordinator", "field_engineer")

#: Who a note is FROM once the caller is authenticated -- derived from the principal, never
#: read from the body (main.add_note). ``author_role`` is not a display label: "MSP"/"FE"
#: stamp ``first_vendor_note_at`` (services/lifecycle.py), the start of the vendor MTTA clock
#: (§7.6.2), and make the note count as the vendor's in the scorecard and the silence chase.
#: The keys must be exactly NOTE_AUTHORS, so no role can pass the gate without a mapping.
NOTE_AUTHOR_ROLE: dict[str, str] = {
    "noc_analyst": "NOC",
    "shift_supervisor": "NOC",
    "duty_manager": "NOC",
    "admin": "NOC",
    "msp_coordinator": "MSP",
    "field_engineer": "FE",
}
if set(NOTE_AUTHOR_ROLE) != set(NOTE_AUTHORS):  # at import, not as a KeyError mid-incident
    raise RuntimeError("NOTE_AUTHOR_ROLE must map exactly the NOTE_AUTHORS roles")

#: §9.3 row "Templates status, outbox retry, scheduler run, MCP status, agents": read for the
#: four internal roles, all of it for admin. Narrower than READERS on purpose -- these
#: surfaces describe the PLATFORM (which jobs ticked, which agents are registered, which
#: model is configured, which mailbox sends), not the incident an MSP coordinator or a field
#: engineer is working. The ACTIONS in that row stay admin-only.
PLATFORM_READERS: tuple[str, ...] = ("noc_analyst", "shift_supervisor", "duty_manager", "management", "admin")

#: §9.3's memory row, read cell ("sites / playbooks / stats read"): R for noc_analyst,
#: shift_supervisor, duty_manager, management, planning and legal, everything for admin, and
#: "—" for msp_coordinator and field_engineer. §7.11.4 disagrees with itself here: its route
#: table says "any signed-in role" for GET /memory/sites and "as today" for the incident
#: route's advisory, while its header puts every memory route "behind require_role, §9.3".
#: This resolves it toward §9.3, deliberately. A memory episode is an EARLIER ticket at the
#: site -- possibly worked by a different MSP -- with its number, restore minutes, resolution
#: code and scrubbed free text, and a name the structured columns never held survives the
#: scrubber. An MSP coordinator reading a competitor's tickets is the exact thing the row's
#: "—" is there to stop. (The same set as pir.PIR_READERS: §9.3's PIR row reads the same.)
MEMORY_READERS: tuple[str, ...] = OPERATIONS + ("management", "planning", "legal")

#: §7.9.3: ledger rows carry names and access notes, so the xlsx download is narrower than
#: OPERATIONS -- §9.3 row "Ledger xlsx download, handover approve".
LEDGER_DOWNLOAD_ROLES: tuple[str, ...] = ("shift_supervisor", "duty_manager", "management", "admin")


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
#     shift_ledger, outbox, llm_calls -- and, since schema_version 8, hitl_tasks) filter on
#     the column directly;
#   * ``incident_briefs`` (and the Phase 4 tables that register below) carry NO operator_id
#     and are owned through ``incident_id -> incidents.operator_id``.
#
# ``hitl_tasks`` changed shape, and both halves of that decision are kept here on purpose.
#
# UNTIL v8 it was owned through the join, deliberately. The argument, as this comment made it:
# every task is written with ``incident_id=inc.id`` (agents/hitl.py,
# services/worklog_monitor.py) against a NOT NULL foreign key, so the owner is already
# recorded once, on the incident, and the join is always well-defined; a denormalised
# ``hitl_tasks.operator_id`` would cost a schema change plus a backfill for a value that is
# derivable, and add a second copy that can drift from the first. "Do not 'optimise' the join
# into a column without weighing that." That was right, for as long as its premise held.
#
# The premise was "every gated thing is an incident", and Phase 5 ended it. An
# APPROVE_SCHEDULE card is about a maintenance programme and an APPROVE_MAINTENANCE_WINDOW
# card is about a night's planned outage; neither has an incident, and the scorecard-dispute
# and vendor-notice cards behind them do not either. For those tasks the owner is NOT
# derivable -- there is nothing to join to -- so the lane borrowed an unrelated "anchor"
# incident to be owned through: Tuesday's generator service filed against somebody's fibre
# cut, that incident's ``hitl_state`` flipping to PENDING because of it, and no card at all on
# a database with no incidents (docs/PHASE5.md, "The one that should be fixed first"). A
# value that cannot be derived has to be stored.
#
# What the old argument weighed, and what became of each cost:
#   * "a schema change plus a backfill" -- paid once, in db/migrate.py ``_rebuild_hitl_tasks``
#     (the one non-additive migration; ``operator_id`` is backfilled from the incident during
#     the copy and the copy is verified before the old table is dropped);
#   * "a second copy that can drift" -- answered at the only moment the copy is written:
#     ``db.models._own_hitl_task`` derives it FROM the incident on insert and refuses a row
#     whose stated operator contradicts its incident's. Nothing reassigns either
#     ``operator_id`` afterwards;
#   * and one cost the join never had: a writer can now forget the column. The same listener
#     covers it -- a task with an incident gets its owner filled in, a task with neither an
#     incident nor an operator is refused at flush -- so the fail-closed case ("no operator
#     can see this card") cannot be reached by forgetting.
#
# A row whose ``operator_id`` is NULL (written by a pre-v8 release running against a v8 file,
# or an orphan the migration could not attribute) matches no operator: invisible, never leaked.
#
# A Phase 4 table that hangs off an incident still registers itself here rather than growing
# its own operator_id column; ``register_owned_via_incident`` is that door, and the argument
# above for using it still holds for any table whose rows ALWAYS have an incident.
_OWNED_VIA_INCIDENT: dict[type, object] = {
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
