"""Spec §9.3 (SUPER_PROMPT_NOC_V2.md, "### 9.3 Access control / RBAC matrix"), executable.

Four rounds of RBAC review found the same class of defect four times: a route whose gate
disagreed with §9.3 because nobody had written the table down anywhere a test could read it,
and each fix was a local tuple beside one route. This file is the table, once:

1. ``SPEC_93`` is §9.3 transcribed as data, one entry per row, every cell quoted as the spec
   writes it -- and ``test_the_transcription_is_the_spec_table`` re-parses the spec file and
   fails if a single cell differs, so an edit to the spec cannot silently leave this behind.
2. ``PERM`` derives each named permission from those cells by a rule you can read next to it
   ("notes only" is not R; a bare "publish" is not edit). Where §9.3 has no row for a route,
   ``OUTSIDE`` says whose decision the gate is instead -- the spec section that names the role,
   or the recorded code decision it follows -- so "not in §9.3" is a statement, not a gap.
3. ``ROUTE_MAP`` maps EVERY registered route (method + path, from ``app.routes``, lane routers
   included) to exactly one permission; ``EXEMPT`` lists the deliberately open ones, each with
   its reason. ``test_every_registered_route_is_classified`` fails on a route in neither, so a
   new route cannot ship ungated or unclassified.
4. With ``AUTH_DISABLED=false`` and a signed cookie per role, every route's gate is asserted
   twice: statically (the ``require_role`` allow-list read off the route equals the matrix) and
   live (no cookie -> 401, every role outside -> 403, every role inside -> neither).
5. The HITL routes' gate depends on the card's TYPE (rows 2, 4, 5, 7), so each type is tested
   on its own cards: claim, approve and reject for every role, and the inbox's filtering.

Where §9.3 is ambiguous for a route, the stricter reading is taken and the comment says so;
each of those is an owner question in the round-4 report.
"""

from __future__ import annotations

import asyncio
import importlib
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Host, Mount
from starlette.websockets import WebSocketDisconnect

from noc_agents.api import auth
from noc_agents.domain.enums import HitlTaskType
from noc_agents.realtime.hub import hub

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "SUPER_PROMPT_NOC_V2.md"
FRONTEND_DIST = ROOT / "frontend" / "dist"
SECRET = "rbac-matrix-secret"
ROLES: tuple[str, ...] = auth.ROLES

NOC, SS, DM, MGMT = "noc_analyst", "shift_supervisor", "duty_manager", "management"
MSP, FE, PLAN, LEGAL, ADMIN = "msp_coordinator", "field_engineer", "planning", "legal", "admin"


# =============================================================================== 1. §9.3
# One entry per table row, in the spec's order: (the row's "Route family" cell, {role: cell}).

SPEC_93: dict[str, tuple[str, dict[str, str]]] = {
    "ops": (
        "ingest, notes, timeline, workflow, signals read",
        {NOC: "R/W", SS: "R/W", DM: "R/W", MGMT: "R", MSP: "notes only", FE: "notes only",
         PLAN: "R", LEGAL: "R", ADMIN: "R/W"},
    ),
    "hitl": (
        "HITL claim/approve/reject (broadcast, priority, assignment, power, schedule, window, regulatory)",
        {NOC: "—", SS: "✓", DM: "✓", MGMT: "—", MSP: "—", FE: "—",
         PLAN: "schedule/window only", LEGAL: "—", ADMIN: "✓"},
    ),
    "clock": (
        "SLA clock open / close / reverse",
        {NOC: "open", SS: "✓", DM: "✓", MGMT: "—", MSP: "—", FE: "—", PLAN: "—", LEGAL: "—", ADMIN: "✓"},
    ),
    "ledger": (
        "Ledger xlsx download, handover approve",
        {NOC: "—", SS: "✓", DM: "✓", MGMT: "✓", MSP: "—", FE: "—", PLAN: "—", LEGAL: "—", ADMIN: "✓"},
    ),
    "scorecards": (
        "Scorecards read / dispute / adjudicate / finalise / notice / shadow-review",
        {NOC: "read", SS: "read + dispute", DM: "all", MGMT: "read",
         MSP: "own vendor read + dispute + vendor pack", FE: "—", PLAN: "—", LEGAL: "read", ADMIN: "all"},
    ),
    "metrics": (
        "Individual metrics",
        {NOC: "own", SS: "own + direct reports", DM: "own + direct reports", MGMT: "—", MSP: "—",
         FE: "own", PLAN: "—", LEGAL: "—", ADMIN: "config only"},
    ),
    "perf": (
        "Performance actions",
        {NOC: "—", SS: "propose", DM: "propose / decide", MGMT: "read", MSP: "—", FE: "—",
         PLAN: "—", LEGAL: "read", ADMIN: "✓"},
    ),
    "pir": (
        "PIR edit / publish",
        {NOC: "edit", SS: "publish", DM: "publish", MGMT: "read", MSP: "—", FE: "—",
         PLAN: "read", LEGAL: "read", ADMIN: "✓"},
    ),
    "contracts": (
        "Contracts ingest / ask / FAQ",
        {NOC: "ask", SS: "ask", DM: "ask", MGMT: "—", MSP: "—", FE: "—", PLAN: "ask", LEGAL: "all", ADMIN: "all"},
    ),
    "complaints": (
        "Complaints file / view all / subject access",
        {NOC: "file", SS: "file + assign", DM: "view all", MGMT: "view all", MSP: "—", FE: "file",
         PLAN: "—", LEGAL: "subject access", ADMIN: "all"},
    ),
    "platform": (
        "Templates status, outbox retry, scheduler run, MCP status, agents",
        {NOC: "read", SS: "read", DM: "read", MGMT: "read", MSP: "—", FE: "—", PLAN: "—", LEGAL: "—", ADMIN: "all"},
    ),
    "memory": (
        "Memory: sites / playbooks / stats read · memos add/resolve · party priors read / erase "
        "(Phase 6, route registered only with the DPIA gate and auth on — §7.11.8)",
        {NOC: "R · — · —", SS: "R · ✓ · R", DM: "R · ✓ · R", MGMT: "R · — · —", MSP: "—", FE: "—",
         PLAN: "R · — · —", LEGAL: "R · — · erase", ADMIN: "all"},
    ),
}


def _spec_table() -> list[list[str]]:
    """§9.3's rows as the spec file has them today, cells stripped, header and rule dropped."""
    lines = SPEC.read_text(encoding="utf-8").split("\n")
    start = next(i for i, line in enumerate(lines) if line.startswith("### 9.3"))
    rows, i = [], start + 1
    while not lines[i].startswith("|"):
        i += 1
    while lines[i].startswith("|"):
        rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
        i += 1
    assert rows[0][1:] == list(ROLES), "the §9.3 header's role columns are not auth.ROLES, in order"
    return rows[2:]


def _roles(row: str, admits) -> frozenset[str]:
    return frozenset(role for role, cell in SPEC_93[row][1].items() if admits(cell))


def _memory_read(cell: str) -> bool:
    """The memory row packs three actions into one cell ("R · ✓ · R"); read is the first."""
    if cell in ("all", "—"):
        return cell == "all"
    return cell.split(" · ")[0] == "R"


# ======================================================================= 2. the permissions
# Each is derived from the cells above by the rule beside it. Stricter readings are marked.

PERM: dict[str, frozenset[str]] = {
    # Row 1. STRICT: "notes only" is not R -- the vendor roles read nothing in this row (NEW:rbac:1).
    "ops.read": _roles("ops", lambda c: c in ("R", "R/W")),
    "ops.write": _roles("ops", lambda c: c == "R/W"),
    "ops.notes": _roles("ops", lambda c: c in ("R/W", "notes only")),
    # Row 2; claim is in the row, so noc_analyst's "—" covers it too.
    "hitl.decide": _roles("hitl", lambda c: c == "✓"),
    "hitl.schedule_window": _roles("hitl", lambda c: c in ("✓", "schedule/window only")),
    # Row 3.
    "clock.open": _roles("clock", lambda c: c in ("open", "✓")),
    "clock.close_reverse": _roles("clock", lambda c: c == "✓"),
    # Row 4 (the xlsx download, the JSON ledger list -- STRICTER, see main.py -- and handover approve).
    "ledger": _roles("ledger", lambda c: c == "✓"),
    # Row 5.
    "scorecards.read": _roles("scorecards", lambda c: "read" in c or c == "all"),
    "scorecards.all": _roles("scorecards", lambda c: c == "all"),
    # Row 7.
    "perf.decide": _roles("perf", lambda c: "decide" in c or c == "✓"),
    # Row 8. STRICT: a bare "publish" is publish only (the table writes "file + assign" when it
    # means both), so the supervisors do not edit.
    "pir.read": _roles("pir", lambda c: c != "—"),
    "pir.edit": _roles("pir", lambda c: c in ("edit", "✓")),
    "pir.publish": _roles("pir", lambda c: c in ("publish", "✓")),
    # PATCH /pir/{id} carries the narrative AND the status. Both cells reach it: the editors
    # write content, the publishers move the review between states (sending a draft back is
    # the reviewer's other half). A publisher's content change is refused in the handler,
    # which test_a_publisher_may_send_a_review_back_but_not_rewrite_it checks.
    "pir.review": _roles("pir", lambda c: c in ("edit", "publish", "✓")),
    # Row 9. Listing and searching contracts are the asker's view, so they take "ask".
    "contracts.ask": _roles("contracts", lambda c: c in ("ask", "all")),
    "contracts.all": _roles("contracts", lambda c: c == "all"),
    # Row 10. Filers see their own filings, view-all roles see all (complaints.py).
    "complaints.file": _roles("complaints", lambda c: "file" in c or c == "all"),
    "complaints.assign": _roles("complaints", lambda c: "assign" in c or c == "all"),
    "complaints.view": _roles("complaints", lambda c: "file" in c or "view all" in c or c == "all"),
    "complaints.subject_access": _roles("complaints", lambda c: c in ("subject access", "all")),
    # Row 11.
    "platform.read": _roles("platform", lambda c: c in ("read", "all")),
    "platform.all": _roles("platform", lambda c: c == "all"),
    # Row 12, first action.
    "memory.read": _roles("memory", _memory_read),
}

#: The HITL card types, each to the permission its §9.3 row gives. A type §9.3 does not name
#: takes row 2's ✓ roles -- the stricter default (owner question).
HITL_TYPE_PERM: dict[str, str] = {
    HitlTaskType.APPROVE_BROADCAST.value: "hitl.decide",
    HitlTaskType.APPROVE_PRIORITY.value: "hitl.decide",
    HitlTaskType.APPROVE_ASSIGNMENT.value: "hitl.decide",
    HitlTaskType.CONFIRM_POWER_NOTICE.value: "hitl.decide",
    HitlTaskType.APPROVE_REGULATORY_NOTICE.value: "hitl.decide",
    HitlTaskType.APPROVE_SCHEDULE.value: "hitl.schedule_window",
    HitlTaskType.APPROVE_MAINTENANCE_WINDOW.value: "hitl.schedule_window",
    HitlTaskType.APPROVE_HANDOVER.value: "ledger",
    HitlTaskType.DISPUTE_SCORECARD_LINE.value: "scorecards.all",  # "adjudicate"
    HitlTaskType.APPROVE_VENDOR_NOTICE.value: "scorecards.all",  # "notice"
    HitlTaskType.APPROVE_PERFORMANCE_ACTION.value: "perf.decide",
    # Not named by §9.3 -> row 2's ✓ (stricter default):
    HitlTaskType.APPROVE_EXEC_BRIEF.value: "hitl.decide",
    HitlTaskType.GENERIC.value: "hitl.decide",
    HitlTaskType.APPROVE_TICKET_SYNC.value: "hitl.decide",
    HitlTaskType.APPROVE_PAGE.value: "hitl.decide",
    HitlTaskType.APPROVE_LEDGER_SYNC.value: "hitl.decide",
}
#: The route-level gate on the four HITL routes: everyone who may act on at least one type.
PERM["hitl.any"] = frozenset().union(*(PERM[p] for p in HITL_TYPE_PERM.values()))

#: The read set the lane routers still share (api/deps.READERS): §9.3 has no row for these
#: surfaces, so it is the recorded code decision, spelled out.
_READERS_SET = frozenset({NOC, SS, DM, ADMIN, MGMT, MSP, FE, PLAN})

#: Routes §9.3 has no row for. Each names whose decision its gate is.
OUTSIDE: dict[str, tuple[frozenset[str], str]] = {
    "audit": (frozenset({DM, MGMT, LEGAL, ADMIN}),
              "no §9.3 row; api/deps.AUDIT_READERS, 'the regulator-facing surface'"),
    "handover.run": (frozenset({SS, DM, ADMIN}),
                     "no §9.3 row (row 4 is the APPROVAL); preparing the handover is the supervisors'"),
    "evidence_pack": (frozenset({SS, DM, MGMT, LEGAL, ADMIN}),
                      "no §9.3 row; regulatory.EVIDENCE_READERS = supervisors + the audit readers"),
    "regulatory.prepare": (PERM["hitl.decide"],
                           "no §9.3 row for drafting; the regulatory card's row-2 deciders prepare it"),
    "scorecards.compute": (frozenset({SS, DM, ADMIN}),
                           "§7.6: 'POST /api/v1/scorecards/compute?period= (shift_supervisor+)'"),
    "maintenance.read": (_READERS_SET, "no §9.3 row; maintenance.py's recorded READERS"),
    "maintenance.plan": (frozenset({PLAN, SS, DM, ADMIN}), "no §9.3 row; maintenance.PLANNERS"),
    "maintenance.supervise": (frozenset({SS, DM, ADMIN}), "no §9.3 row; maintenance.py's SUPERVISORS"),
    "maintenance.complete": (frozenset({NOC, SS, DM, ADMIN}), "no §9.3 row; maintenance.py's OPERATIONS"),
    "capacity.read": (_READERS_SET, "no §9.3 row; capacity.py's recorded READERS"),
    "capacity.ingest": (frozenset({PLAN, ADMIN}),
                        "§7.5.3: 'POST /api/v1/capacity/observations (... planning/admin)'"),
    "capacity.review": (frozenset({SS, DM, PLAN, ADMIN}), "no §9.3 row; capacity.py's recorded reviewers"),
    "vendors.read": (_READERS_SET, "no §9.3 row; vendors.py's recorded READERS"),
    "vendors.create": (frozenset({ADMIN}), "§7.6: 'POST /api/v1/vendors (admin)'"),
    "vendors.backfill": (frozenset({SS, DM, ADMIN}), "no §9.3 row; vendors.py's SUPERVISORS"),
    "complaints.handle": (frozenset({SS, DM, MGMT, ADMIN}),
                          "§9.3 names neither acknowledge nor resolve; complaints.HANDLERS, the "
                          "managers a complaint is assigned TO (owner question)"),
    "complaints.withdraw": (PERM["complaints.view"],
                            "§9.3 does not name withdraw; whoever may see the complaint "
                            "(the service decides own vs any)"),
}
EXPECTED: dict[str, frozenset[str]] = {**PERM, **{k: v[0] for k, v in OUTSIDE.items()}}


# ===================================================================== 3. every route
G, P, PA, PU = "GET", "POST", "PATCH", "PUT"
ROUTE_MAP: dict[tuple[str, str], str] = {
    # --- main.py: row 1 ---------------------------------------------------------------
    (P, "/api/v1/events"): "ops.write",
    (P, "/api/v1/events/batch"): "ops.write",
    (P, "/api/v1/demo/rain-storm"): "ops.write",
    (G, "/api/v1/demo/scenarios"): "ops.read",
    (G, "/api/v1/demo/rain-storm/events"): "ops.read",
    (G, "/api/v1/incidents"): "ops.read",
    (G, "/api/v1/incidents/{incident_id}"): "ops.read",
    (G, "/api/v1/incidents/{incident_id}/timeline"): "ops.read",
    (G, "/api/v1/incidents/{incident_id}/workflow"): "ops.read",
    (G, "/api/v1/runs"): "ops.read",
    (G, "/api/v1/runs/{run_id}"): "ops.read",
    (G, "/api/v1/briefs/{incident_id}"): "ops.read",
    (G, "/api/v1/problems"): "ops.read",
    (G, "/api/v1/sites"): "ops.read",
    (G, "/api/v1/signals/weather/regions"): "ops.read",
    (G, "/api/v1/stream/events"): "ops.read",
    # The socket twin of the SSE stream: the same envelopes, so the same row. Its gate is in
    # the handler (auth.authorise_socket), because no HTTP dependency can bind on a handshake.
    ("WS", "/ws/ops"): "ops.read",
    (P, "/api/v1/incidents/{incident_id}/notes"): "ops.notes",
    (P, "/api/v1/incidents/{incident_id}/close"): "ops.write",
    (P, "/api/v1/incidents/{incident_id}/restore"): "ops.write",
    (P, "/api/v1/incidents/{incident_id}/reassign"): "ops.write",
    (P, "/api/v1/incidents/{incident_id}/analysis"): "ops.write",
    (P, "/api/v1/incidents/{incident_id}/brief/draft"): "ops.write",
    (P, "/api/v1/monitor/tick"): "ops.write",
    # --- main.py: HITL (rows 2, 4, 5, 7 by card type) ------------------------------------
    (G, "/api/v1/hitl/pending"): "hitl.any",
    (P, "/api/v1/hitl/{task_id}/claim"): "hitl.any",
    (P, "/api/v1/hitl/{task_id}/approve"): "hitl.any",
    (P, "/api/v1/hitl/{task_id}/reject"): "hitl.any",
    # --- main.py: row 4, row 11, outside --------------------------------------------------
    (G, "/api/v1/shifts/ledger"): "ledger",
    (G, "/api/v1/shifts/ledger/{shift_id:path}.xlsx"): "ledger",
    (P, "/api/v1/shifts/handover"): "handover.run",
    (G, "/api/v1/agents"): "platform.read",
    (G, "/api/v1/metrics/productivity"): "platform.read",
    (G, "/api/v1/agents/{name}"): "platform.read",
    (G, "/api/v1/llm/status"): "platform.read",
    (G, "/api/v1/email/status"): "platform.read",
    (G, "/api/v1/scheduler/status"): "platform.read",
    (P, "/api/v1/scheduler/run/{job}"): "platform.all",
    (P, "/api/v1/email/test"): "platform.all",
    (G, "/api/v1/audit"): "audit",
    # --- signals.py, dashboards.py, memory.py -------------------------------------------
    (G, "/api/v1/signals"): "ops.read",
    (G, "/api/v1/signals/county-map"): "ops.read",
    (G, "/api/v1/signals/precision"): "ops.read",
    (G, "/api/v1/dashboard/regions"): "ops.read",
    (G, "/api/v1/memory/sites/{site_id}"): "memory.read",
    # --- clocks.py (row 3; the read is row 1) --------------------------------------------
    (G, "/api/v1/incidents/{incident_id}/clock"): "ops.read",
    (P, "/api/v1/incidents/{incident_id}/clock"): "clock.open",
    (P, "/api/v1/incidents/{incident_id}/clock/{event_id}/close"): "clock.close_reverse",
    (P, "/api/v1/incidents/{incident_id}/clock/{event_id}/reverse"): "clock.close_reverse",
    # --- regulatory.py --------------------------------------------------------------------
    (G, "/api/v1/incidents/{incident_id}/regulatory"): "ops.read",
    (P, "/api/v1/incidents/{incident_id}/regulatory"): "regulatory.prepare",
    (P, "/api/v1/regulatory/{notification_id}/draft"): "regulatory.prepare",
    (P, "/api/v1/regulatory/{notification_id}/request-approval"): "regulatory.prepare",
    (P, "/api/v1/regulatory/{notification_id}/send"): "regulatory.prepare",
    (G, "/api/v1/incidents/{incident_id}/evidence-pack"): "evidence_pack",
    # --- pir.py (row 8; the known-error panel is row 1) -------------------------------------
    (G, "/api/v1/incidents/{incident_id}/known-error"): "ops.read",
    (G, "/api/v1/pir"): "pir.read",
    (G, "/api/v1/pir/awaiting-review"): "pir.read",
    (G, "/api/v1/pir/{pir_id}"): "pir.read",
    (G, "/api/v1/pir/{pir_id}/actions"): "pir.read",
    (PA, "/api/v1/pir/{pir_id}"): "pir.review",
    (P, "/api/v1/pir/{pir_id}/actions"): "pir.edit",
    (PA, "/api/v1/pir/{pir_id}/actions/{action_id}"): "pir.edit",
    (P, "/api/v1/pir/{pir_id}/draft/llm"): "pir.edit",
    (P, "/api/v1/incidents/{incident_id}/pir"): "pir.edit",
    (PA, "/api/v1/problems/{problem_id}"): "pir.edit",  # known-error fields: problem management
    (P, "/api/v1/pir/{pir_id}/publish"): "pir.publish",
    # --- contracts.py (row 9) ------------------------------------------------------------
    (G, "/api/v1/contracts/status"): "contracts.ask",
    (G, "/api/v1/contracts"): "contracts.ask",
    (G, "/api/v1/contracts/clauses/search"): "contracts.ask",
    (P, "/api/v1/contracts/ask"): "contracts.ask",
    (P, "/api/v1/contracts"): "contracts.all",
    (P, "/api/v1/contracts/ingest-samples"): "contracts.all",
    (G, "/api/v1/contracts/faq"): "contracts.all",
    (P, "/api/v1/contracts/faq"): "contracts.all",
    (G, "/api/v1/contracts/queries"): "contracts.all",
    # --- complaints.py (row 10) ----------------------------------------------------------
    (P, "/api/v1/complaints"): "complaints.file",
    (P, "/api/v1/complaints/classify"): "complaints.file",
    (G, "/api/v1/complaints"): "complaints.view",
    (G, "/api/v1/complaints/stats"): "complaints.view",
    (G, "/api/v1/complaints/{complaint_id}"): "complaints.view",
    (G, "/api/v1/complaints/subject-access/{ref}"): "complaints.subject_access",
    (P, "/api/v1/complaints/{complaint_id}/assign"): "complaints.assign",
    (P, "/api/v1/complaints/{complaint_id}/acknowledge"): "complaints.handle",
    (P, "/api/v1/complaints/{complaint_id}/resolve"): "complaints.handle",
    (P, "/api/v1/complaints/{complaint_id}/withdraw"): "complaints.withdraw",
    # --- scorecards.py (row 5; not edited here -- asserted) -------------------------------------
    (G, "/api/v1/scorecards"): "scorecards.read",
    (G, "/api/v1/scorecards/{card_id}"): "scorecards.read",
    (P, "/api/v1/scorecards/compute"): "scorecards.compute",
    (P, "/api/v1/scorecards/{card_id}/finalise"): "scorecards.all",
    (P, "/api/v1/scorecards/{card_id}/publish"): "scorecards.all",
    (P, "/api/v1/scorecards/{card_id}/shadow-review"): "scorecards.all",
    # --- outbox_admin.py, templates.py (row 11; asserted) ----------------------------------------
    (G, "/api/v1/outbox"): "platform.read",
    (P, "/api/v1/outbox/{outbox_id}/retry"): "platform.all",
    (PU, "/api/v1/templates/{template_id}/status"): "platform.all",
    # --- maintenance.py (row 2 decides its cards via /hitl; the rest has no §9.3 row) ---------
    (G, "/api/v1/maintenance/incidents/{incident_id}/stop-clock-proposal"): "ops.read",
    (G, "/api/v1/maintenance/plans"): "maintenance.read",
    (G, "/api/v1/maintenance/tasks"): "maintenance.read",
    (G, "/api/v1/maintenance/windows"): "maintenance.read",
    (G, "/api/v1/maintenance/windows/{window_id}"): "maintenance.read",
    (P, "/api/v1/maintenance/plans"): "maintenance.plan",
    (P, "/api/v1/maintenance/tasks/{task_id}/request-approval"): "maintenance.plan",
    (P, "/api/v1/maintenance/tasks/{task_id}/schedule"): "maintenance.plan",
    (P, "/api/v1/maintenance/windows"): "maintenance.plan",
    (P, "/api/v1/maintenance/windows/{window_id}/request-approval"): "maintenance.plan",
    (P, "/api/v1/maintenance/windows/{window_id}/tasks/{task_id}"): "maintenance.plan",
    (P, "/api/v1/maintenance/windows/{window_id}/schedule"): "maintenance.supervise",
    (P, "/api/v1/maintenance/windows/{window_id}/cancel"): "maintenance.supervise",
    (P, "/api/v1/maintenance/windows/{window_id}/notice-sent"): "maintenance.supervise",
    (P, "/api/v1/maintenance/tasks/{task_id}/complete"): "maintenance.complete",
    # --- capacity.py, vendors.py (no §9.3 row; asserted) --------------------------------------
    (G, "/api/v1/capacity/observations"): "capacity.read",
    (G, "/api/v1/capacity/sites/{site_id}"): "capacity.read",
    (G, "/api/v1/capacity/advisories"): "capacity.read",
    (G, "/api/v1/capacity/advisories/{advisory_id}"): "capacity.read",
    (P, "/api/v1/capacity/observations"): "capacity.ingest",
    (P, "/api/v1/capacity/observations/csv"): "capacity.ingest",
    (P, "/api/v1/capacity/advisories/{advisory_id}/review"): "capacity.review",
    (G, "/api/v1/vendors"): "vendors.read",
    (P, "/api/v1/vendors"): "vendors.create",
    (P, "/api/v1/vendors/backfill"): "vendors.backfill",
}

#: Open on purpose, each with its reason. The ops socket is no longer here: since A-14 it is
#: gated in ROUTE_MAP like the rest of row 1.
EXEMPT: dict[tuple[str, str], str] = {
    (G, "/health"): "the load balancer's liveness probe; answers before anyone logs in",
    (G, "/api/v1/profile"): "the login screen names the operator before anyone has a role",
    (G, "/api/v1/session"): "the demo role switcher; with auth on it grants nothing",
    (P, "/api/v1/session"): "the demo role switcher; with auth on it grants nothing",
    (G, "/api/v1/shifts/current"): "the same shift values /api/v1/profile already serves",
    (G, "/api/v1/metrics/summary"): "aggregate counts only; pinned open by test_auth_skeleton",
    (G, "/openapi.json"): "FastAPI's schema: route shapes, no operator data -- serving it in "
                          "production is an open decision (RBAC review round 4, item 6)",
    (G, "/docs"): "FastAPI's Swagger UI over /openapi.json (same open decision)",
    (G, "/docs/oauth2-redirect"): "part of FastAPI's Swagger UI (same open decision)",
    (G, "/redoc"): "FastAPI's ReDoc over /openapi.json (same open decision)",
}
#: Mounted sub-applications, which are not routes and carry no ``require_role``: whatever the
#: mounted app serves is served on its own terms. Each one needs a reason here, or the
#: classification test fails -- an ungated Starlette app mounted on a lane router would
#: otherwise pass every other test in this file (RBAC review round 5).
MOUNT_EXEMPT: dict[str, str] = {
    "/assets": "the built SPA's static bundle (js/css): the same public shell as GET /, "
               "mounted only when frontend/dist/assets exists",
}

#: The built SPA shell: registered only when frontend/dist exists; the login screen lives here.
SPA_EXEMPT: dict[tuple[str, str], str] = {
    (G, "/"): "the SPA shell: the login screen is served from here",
    (G, "/{full_path:path}"): "the SPA's deep links: same shell, same reason",
}


def _flat(routes):
    """Every route, walking into included routers (FastAPI keeps them as ``_IncludedRouter``).

    A Mount is yielded as itself and never descended into: what a mounted app serves is the
    mounted app's business, which is exactly why every mount needs an entry in MOUNT_EXEMPT.
    """
    for route in routes:
        if hasattr(route, "original_router"):
            yield from _flat(route.original_router.routes)
        else:
            yield route


def _mounts(routes) -> dict[str, str]:
    """Every mounted sub-application, by path -- including ones a lane router mounts."""
    found: dict[str, str] = {}
    for route in _flat(routes):
        if isinstance(route, (Mount, Host)):
            key = getattr(route, "path", None) or f"host:{getattr(route, 'host', '?')}"
            found[key] = type(getattr(route, "app", route)).__name__
    return found


def _registered(app) -> dict[tuple[str, str], object]:
    out = {}
    for route in _flat(app.routes):
        if getattr(route, "endpoint", None) is None:
            continue  # the /assets static mount: files, not a handler
        methods = getattr(route, "methods", None)
        for method in sorted(set(methods) - {"HEAD"}) if methods else ["WS"]:
            out[(method, route.path)] = route
    return out


def _gate(route) -> frozenset[str] | None:
    """The roles the route's ``require_role`` dependencies admit (their intersection), or None."""
    sets: list[frozenset[str]] = []

    def walk(dependant):
        for dep in dependant.dependencies:
            if getattr(dep.call, "__name__", "").startswith("require_role"):
                sets.extend(c.cell_contents for c in (dep.call.__closure__ or ())
                            if isinstance(c.cell_contents, frozenset))
            walk(dep)

    dependant = getattr(route, "dependant", None)  # FastAPI's /docs routes are plain Starlette routes
    if dependant is not None:
        walk(dependant)
    return frozenset.intersection(*sets) if sets else None


# ================================================================================ fixtures

LANES = ("PIR_ENABLED", "CONTRACTS_ENABLED", "COMPLAINTS_ENABLED", "MAINTENANCE_ENABLED",
         "CAPACITY_ENABLED", "SCORECARDS_ENABLED", "REGULATORY_ENABLED")


@pytest.fixture(scope="module")
def app_client(tmp_path_factory):
    """The whole app on its own SQLite file, every lane switched on so no lane's 404 fires
    before its gate, seeded with the demo default and then enforced per test."""
    mp = pytest.MonkeyPatch()
    db = tmp_path_factory.mktemp("rbac_matrix") / "rbac.db"
    mp.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")
    mp.setenv("OPERATOR_PROFILE", "safaricom")
    mp.setenv("AUTH_DISABLED", "true")
    mp.setenv("NOC_ENV", "demo")  # the complaints/contracts/ledger routes must be registered
    mp.delenv("NOC_SESSION_SECRET", raising=False)
    for lane in LANES:
        mp.setenv(lane, "true")

    import noc_agents.config as cfg
    import noc_agents.db.models as models
    import noc_agents.main as main

    cfg.clear_settings_cache()
    models._engine = None
    models.SessionLocal = None
    importlib.reload(main)
    auth.reset_sessions()
    hub._history.clear()
    client = TestClient(main.app, raise_server_exceptions=False)  # a 500 after the gate is not 401/403
    client.__enter__()
    try:
        yield main, client
    finally:
        client.__exit__(None, None, None)
        hub._history.clear()
        auth.reset_sessions()
        models._engine = None
        models.SessionLocal = None
        importlib.reload(main)  # back to the default shape while DATABASE_URL still points here
        mp.undo()
        cfg.clear_settings_cache()


@pytest.fixture()
def enforced(app_client, monkeypatch):
    main, client = app_client
    monkeypatch.setenv("AUTH_DISABLED", "false")
    monkeypatch.setenv("NOC_SESSION_SECRET", SECRET)
    client.cookies.clear()
    yield main, client
    client.cookies.clear()


def _as(client: TestClient, role: str, name: str | None = None) -> None:
    client.cookies.clear()
    client.cookies.set(
        auth.SESSION_COOKIE, auth.sign_session({"sub": f"u-{role}", "role": role, "name": name or role}, SECRET)
    )


# ============================================================================ the tests


def test_the_transcription_is_the_spec_table():
    """SPEC_93 is §9.3 cell for cell. A spec edit that is not carried here fails this test."""
    spec_rows = _spec_table()
    ours = [[family, *[cells[role] for role in ROLES]] for family, cells in SPEC_93.values()]
    assert ours == spec_rows


def test_the_derived_permissions_say_what_the_report_says():
    """The rules above, pinned as sets, so a change to a rule is a visible diff here."""
    assert PERM["ops.read"] == {NOC, SS, DM, MGMT, PLAN, LEGAL, ADMIN}  # no vendor role (strict)
    assert PERM["ops.notes"] == {NOC, SS, DM, ADMIN, MSP, FE}
    assert PERM["hitl.any"] == {SS, DM, MGMT, PLAN, ADMIN}  # noc_analyst "—"
    assert PERM["pir.edit"] == {NOC, ADMIN} and PERM["pir.publish"] == {SS, DM, ADMIN}
    assert PERM["pir.review"] == {NOC, SS, DM, ADMIN}
    assert PERM["contracts.ask"] == {NOC, SS, DM, PLAN, LEGAL, ADMIN} and PERM["contracts.all"] == {LEGAL, ADMIN}
    assert PERM["complaints.assign"] == {SS, ADMIN}
    assert PERM["memory.read"] == {NOC, SS, DM, MGMT, PLAN, LEGAL, ADMIN}


def test_every_registered_route_is_classified(app_client):
    """Every route -- main.py and every lane router -- is in ROUTE_MAP or EXEMPT, exactly once;
    a route in neither is a route nobody decided, and this is where it stops."""
    main, _ = app_client
    registered = set(_registered(main.app))
    exempt = {**EXEMPT, **(SPA_EXEMPT if FRONTEND_DIST.exists() else {})}
    assert not set(ROUTE_MAP) & set(exempt), "a route is both mapped and exempt"
    assert sorted(registered - set(ROUTE_MAP) - set(exempt)) == [], "unclassified routes"
    assert sorted((set(ROUTE_MAP) | set(exempt)) - registered) == [], "the matrix names routes that do not exist"
    assert all(key in EXPECTED for key in ROUTE_MAP.values())
    assert all(reason.strip() for reason in exempt.values())
    # Mounted sub-apps are not routes and have no gate of ours: each needs a written reason.
    mounts = _mounts(main.app.routes)
    assert sorted(set(mounts) - set(MOUNT_EXEMPT)) == [], f"unclassified mounts: {mounts}"
    assert all(reason.strip() for reason in MOUNT_EXEMPT.values())


def test_every_exempt_route_really_is_open(app_client):
    """An exemption is a claim that the route is open; check it (the socket and the SPA are
    exercised elsewhere)."""
    main, _ = app_client
    registered = _registered(main.app)
    for key in EXEMPT:
        if key[0] != "WS":
            assert _gate(registered[key]) is None, key


def test_every_hitl_card_type_has_a_row():
    """A new HitlTaskType must be given its §9.3 row here -- and api/deps agrees with it."""
    from noc_agents.api import deps

    assert set(HITL_TYPE_PERM) == {t.value for t in HitlTaskType}
    for task_type, perm in HITL_TYPE_PERM.items():
        assert frozenset(deps.hitl_deciders(task_type)) == PERM[perm], task_type
    assert frozenset(deps.HITL_ROLES) == PERM["hitl.any"]


@pytest.mark.parametrize("key", sorted(k for k in ROUTE_MAP if k[0] != "WS"),
                         ids=lambda k: f"{k[0]} {k[1]}")
def test_the_gate_on_the_route_is_the_matrix(app_client, key):
    """Static: the allow-list read off the route equals the matrix, role for role.

    The socket is excluded here and only here: no ``require_role`` can sit on a handshake, so
    its gate is inside the handler and is checked live, connection by connection, below.
    """
    main, _ = app_client
    route = _registered(main.app)[key]
    expected = EXPECTED[ROUTE_MAP[key]]
    assert _gate(route) == expected, (
        f"{key}: gate {sorted(_gate(route) or ())} != §9.3 {sorted(expected)} ({ROUTE_MAP[key]})"
    )


_PARAM = re.compile(r"\{([^}:]+)(?::[^}]*)?\}")
_VALUES = {"shift_id": "2026-09-17_DAY", "site_id": "SFC-NBIE-HUB-EMB", "name": "SupervisorAgent",
           "job": "no-such-job"}
#: Requests shaped to fail AFTER the gate for routes that would otherwise run for real.
_QUERY = {(P, "/api/v1/demo/rain-storm"): "?stagger_ms=not-a-number"}


def _request(client: TestClient, key: tuple[str, str]):
    method, path = key
    url = _PARAM.sub(lambda m: _VALUES.get(m.group(1), "does-not-exist"), path) + _QUERY.get(key, "")
    if method == G:
        return client.get(url)
    return client.request(method, url, json={})


def _ended_stream(*_args, **_kwargs):
    q: asyncio.Queue = asyncio.Queue()
    q.put_nowait(None)  # the hub's overflow sentinel: the SSE generator replays, then returns
    return q


@pytest.mark.parametrize("key", sorted(k for k in ROUTE_MAP if k[0] != "WS"),
                         ids=lambda k: f"{k[0]} {k[1]}")
def test_the_gate_answers_the_matrix_for_every_caller(enforced, monkeypatch, key):
    """Live, with auth enforced: no cookie -> 401; every role outside -> 403; every role inside
    -> neither (the handler may still say 404/422 for the made-up ids and empty bodies)."""
    _, client = enforced
    monkeypatch.setattr(hub, "subscribe", _ended_stream)
    client.cookies.clear()
    assert _request(client, key).status_code == 401, "no cookie"
    expected = EXPECTED[ROUTE_MAP[key]]
    got = {}
    for role in ROLES:
        _as(client, role)
        got[role] = _request(client, key).status_code
    wrong = {r: c for r, c in got.items() if (c == 403) == (r in expected) or c in (401, 503)}
    assert not wrong, f"{key} ({ROUTE_MAP[key]}): expected {sorted(expected)}, got {got}"


def test_the_ops_socket_is_gated_like_the_rest_of_row_one(enforced):
    """A-14: the handshake needs a signed session and a row-1 read role, and a refusal is a
    close (1008) BEFORE the accept -- so a refused caller is never sent the replay.
    """
    _, client = enforced
    client.cookies.clear()
    with pytest.raises(WebSocketDisconnect) as anonymous:
        with client.websocket_connect("/ws/ops"):
            pass
    assert anonymous.value.code == 1008
    for role in ROLES:
        _as(client, role)
        if role in PERM["ops.read"]:
            with client.websocket_connect(f"/ws/ops?since={hub.last_seq}"):
                pass  # accepted: the handshake completed
        else:
            with pytest.raises(WebSocketDisconnect) as refused:
                with client.websocket_connect("/ws/ops"):
                    pass
            assert refused.value.code == 1008, role


def test_a_publisher_may_send_a_review_back_but_not_rewrite_it(enforced):
    """§9.3 row 8 split, as the gate now draws it: the supervisors move a review between
    states (the send-back a reviewer needs), the analyst writes its content.
    """
    _, client = enforced
    pir_id = "does-not-exist"  # the gate and the content check both run before the lookup
    for role in sorted(PERM["pir.publish"] - PERM["pir.edit"]):  # shift_supervisor, duty_manager
        _as(client, role)
        sent_back = client.patch(f"/api/v1/pir/{pir_id}", json={"status": "DRAFT"})
        assert sent_back.status_code == 404, (role, sent_back.text)  # through the gate
        rewrite = client.patch(f"/api/v1/pir/{pir_id}", json={"summary": "my words"})
        assert rewrite.status_code == 403, (role, rewrite.text)
        assert "content" in rewrite.json()["detail"], role
    for role in sorted(PERM["pir.edit"]):  # noc_analyst, admin: content is theirs
        _as(client, role)
        assert client.patch(f"/api/v1/pir/{pir_id}", json={"summary": "my words"}).status_code == 404, role


# ------------------------------------------------------------------ HITL, by card type


def _card(task_type: str, *, raiser: str = "rbac-matrix-raiser") -> str:
    """One PENDING, incident-less card of ``task_type``: no incident means approve and reject
    record the decision and touch nothing else, so every (type, role) can get a fresh card."""
    from noc_agents.config import get_settings
    from noc_agents.db.models import HitlTaskRow, get_session

    session = get_session()
    try:
        card = HitlTaskRow(
            incident_id=None,
            operator_id=get_settings().operator.operator_id,
            task_type=task_type,
            entity_type="rbac_matrix",
            entity_id=task_type,
            created_by=raiser,
            status="PENDING",
        )
        card.proposed_payload = {}
        session.add(card)
        session.commit()
        return card.id
    finally:
        session.close()


@pytest.mark.parametrize("task_type", sorted(HITL_TYPE_PERM))
def test_who_may_act_on_a_card_follows_its_type(enforced, task_type):
    """Claim, approve and reject, for every role, each on its own fresh card of this type."""
    _, client = enforced
    allowed = PERM[HITL_TYPE_PERM[task_type]]
    got = {}
    for role in ROLES:
        _as(client, role)
        got[(role, "claim")] = client.post(f"/api/v1/hitl/{_card(task_type)}/claim", json={}).status_code
        got[(role, "approve")] = client.post(
            f"/api/v1/hitl/{_card(task_type)}/approve", json={"reason": "checked"}).status_code
        got[(role, "reject")] = client.post(
            f"/api/v1/hitl/{_card(task_type)}/reject", json={"reason": "not now"}).status_code
    want = {(role, act): (200 if role in allowed else 403) for role in ROLES for act in ("claim", "approve", "reject")}
    assert got == want, task_type


def test_the_inbox_shows_each_role_the_cards_it_may_act_on(enforced):
    _, client = enforced
    batch = {_card(task_type): task_type for task_type in HITL_TYPE_PERM}
    for role in ROLES:
        _as(client, role)
        r = client.get("/api/v1/hitl/pending")
        if role not in PERM["hitl.any"]:
            assert r.status_code == 403, role
            continue
        seen = {batch[t["id"]] for t in r.json() if t["id"] in batch}
        assert seen == {t for t, perm in HITL_TYPE_PERM.items() if role in PERM[perm]}, role


def test_raiser_is_not_approver_for_the_new_deciders_too(enforced):
    """Planning may decide window cards -- but not one it raised; nor may management approve a
    handover it raised. The type check lets them in; the §6.5 rule still stops them."""
    _, client = enforced
    for role, task_type in ((PLAN, HitlTaskType.APPROVE_MAINTENANCE_WINDOW.value),
                            (MGMT, HitlTaskType.APPROVE_HANDOVER.value)):
        mine = _card(task_type, raiser=f"{role}-raiser")
        _as(client, role, name=f"{role}-raiser")
        r = client.post(f"/api/v1/hitl/{mine}/approve", json={"reason": "own card"})
        assert r.status_code == 403 and "raised" in r.json()["detail"], (role, r.text)


# ------------------------------------------------------------- the leak NEW:rbac:1 closed


def test_a_vendor_role_cannot_read_earlier_tickets_through_row_one(enforced):
    """The round-4 finding, replayed: refused the memory advisory, an msp_coordinator could read
    the same earlier tickets unscrubbed through the incident list, detail and timeline. Row 1
    read strictly ("notes only") closes every one of those doors -- and leaves the note open."""
    _, client = enforced
    _as(client, NOC)
    created = client.post("/api/v1/events", json={
        "site_id": "SFC-RFT-HUB-ELD", "site_type": "HUB", "region_code": "RFT",
        "alarm_code": "SITE_DOWN", "failure_domain": "POWER"})
    assert created.status_code == 200, created.text
    incident_id = created.json()["incident"]["id"]
    for role in (MSP, FE):
        _as(client, role)
        for path in ("/api/v1/incidents?q=SFC-RFT-HUB-ELD&status=CLOSED", f"/api/v1/incidents/{incident_id}",
                     f"/api/v1/incidents/{incident_id}/timeline", f"/api/v1/incidents/{incident_id}/workflow",
                     "/api/v1/memory/sites/SFC-RFT-HUB-ELD", "/api/v1/problems"):
            assert client.get(path).status_code == 403, (role, path)
        note = client.post(f"/api/v1/incidents/{incident_id}/notes", json={"body": "On site, ETA 20 min"})
        assert note.status_code == 200, (role, note.text)
