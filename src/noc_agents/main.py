from __future__ import annotations

import io
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from openpyxl import Workbook
from sqlalchemy import func, or_, select

from noc_agents.api import auth
from noc_agents.api.auth import require_role
from noc_agents.api.deps import (
    AUDIT_READERS,
    HITL_ROLES,
    INCIDENT_READERS,
    INGEST,
    LEDGER_DOWNLOAD_ROLES,
    MEMORY_READERS,
    NOTE_AUTHOR_ROLE,
    NOTE_AUTHORS,
    OPERATIONS,
    PLATFORM_READERS,
    SUPERVISORS,
    _actor,
    _get_owned,
    _operator_scoped,
    _owned,
    _settings,
    hitl_deciders,
)
from noc_agents.api.routers import ROUTERS
from noc_agents.api.serializers import incident_out, run_out, step_out
from noc_agents.config import clear_settings_cache, get_settings
from noc_agents.db.models import (
    AgentRunRow,
    AuditRow,
    BroadcastRow,
    HitlTaskRow,
    IncidentBriefRow,
    IncidentRow,
    ProblemRow,
    ShiftLedgerRow,
    WorkNoteRow,
    get_session,
    init_db,
    utcnow,
)
from noc_agents.domain.schemas import (
    CloseIn,
    EventIngest,
    HitlDecision,
    MetricsSummary,
    NoteIn,
    ReassignIn,
    SessionIn,
    WorkflowOut,
)
from noc_agents.graph.pipeline import drain_after_commit, drain_outbox, process_event, sync_drain_enabled
from noc_agents.graph.workflow_nodes import WORKFLOW_EDGES, graph_status_map
from noc_agents.llm.assist import analyse_incident, draft_exec_brief
from noc_agents.llm.client import llm_status
from noc_agents.orchestrator.registry import agent_catalog
from noc_agents.orchestrator.runner import GRAPH_NAME as LIFECYCLE_GRAPH
from noc_agents.realtime.hub import RealtimeEvent, hub
from noc_agents.adapters.email_smtp import email_status
from noc_agents.scheduler.loop import (
    Scheduler,
    job_card,
    job_enabled,
    run_job,
    scheduler_enabled,
    status_payload,
)
from noc_agents.pollers.weather import JOB_NAME as WEATHER_JOB_NAME, weather_risk_for_region
from noc_agents.services.handover import (
    HANDOVER_TASK_TYPE,
    build_handover,
    handover_requires_hitl,
    queue_handover,
    release_handover,
)
from noc_agents.services.ledger import ledger_root
from noc_agents.services.memory import advisory_for_incident
from noc_agents.services.hitl import (
    GATING_TASK_TYPE,
    approve_reason_required,
    is_open,
    is_raiser,
    rerender_and_release,
    suppress_held_outbox,
    sync_incident_hitl_scalars,
    transition_open_task,
)
from noc_agents.services.clock import to_utc
from noc_agents.services.lifecycle import (
    TERMINAL_STATUSES,
    apply_work_note_side_effects,
    close_incident,
    reassign_incident,
    restore_incident,
    upsert_brief,
)
from noc_agents.services.notify import dispatch_handover_email, handover_email_response
from noc_agents.services.shifts import current_shift, shift_id
from noc_agents.services.scenarios import RAIN_STORM_EVENTS, scenario_payload
from noc_agents.services.worklog_monitor import chase_silent_incidents
import time

ROOT = Path(__file__).resolve().parents[2]
# NOC_FRONTEND_DIST: where the built SPA lives (default frontend/dist). A deployment that
# builds elsewhere points here; the tests point it at an empty folder to exercise the
# "UI not built" page below.
FRONTEND_DIST = Path(os.getenv("NOC_FRONTEND_DIST") or (ROOT / "frontend" / "dist"))

#: What GET / answers when there is no build to serve. A presenter who opens the port and
#: sees a bare 404 JSON starts debugging the wrong thing; this says what is missing and how
#: to get it. Static text, no operator data, so it is as open as the shell it stands in for.
NO_BUILD_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kenya NOC: UI not built</title>
<style>
  body{margin:0;background:#0a1220;color:#f0f6ff;font:16px/1.5 system-ui,sans-serif}
  main{max-width:60ch;margin:12vh auto;padding:0 1.25rem}
  h1{font-size:1.5rem;margin:0 0 .5rem}p{color:#a8bdd6}
  pre{background:#050a14;border:1px solid #2a3f5f;border-radius:10px;padding:.9rem 1rem;overflow:auto;color:#3ecbff}
  a{color:#3ecbff}
</style></head><body><main>
<h1>The NOC API is running, but the web UI has not been built.</h1>
<p>This port answers the API (<a href="/health">/health</a>, <a href="/docs">/docs</a>). Mission Control is the
React app under <code>frontend/</code>; build it once, then start the server again:</p>
<pre>cd frontend
npm install
npm run build</pre>
<p>Or run the development server beside the API while you work on it, and open
<a href="http://127.0.0.1:5173">http://127.0.0.1:5173</a>:</p>
<pre>cd frontend
npm run dev</pre>
<p>One command for both on Linux and macOS: <code>bash scripts/run_all.sh</code>; on Windows:
<code>scripts\\run_all.ps1</code>.</p>
</main></body></html>
"""


_boot = _settings()
init_db(_boot.database_url)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Scheduler (§7.0.3): one ticker task per process, only when SCHEDULER_ENABLED=true.

    Off by default, so a bare import or the test suite starts nothing. The DB lease decides
    which of several processes actually runs jobs; shutdown stops the task and releases it.
    """
    scheduler: Scheduler | None = None
    if scheduler_enabled():
        scheduler = Scheduler()
        scheduler.start()
    application.state.scheduler = scheduler
    try:
        yield
    finally:
        if scheduler is not None:
            await scheduler.stop()
        application.state.scheduler = None


app = FastAPI(
    title="Kenya NOC Mission Control",
    description="Multi-agent incident system — Safaricom-first demo profile",
    version="0.1.0",
    lifespan=lifespan,
)
app.state.scheduler = None  # set by the lifespan; None when it has not run or the flag is off
app.add_middleware(
    CORSMiddleware,
    # CORS_ORIGINS (default http://localhost:5173). Never "*": we answer with
    # credentials, and a wildcard there is both a browser error and a CSRF door.
    allow_origins=auth.cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Compress every HTTP answer of 1 KiB or more for a client that accepts gzip: the JS chunks, the
# stylesheet and the big JSON lists (/api/v1/runs, /api/v1/audit) shrink to a quarter or less,
# which is most of a first load on a weak link. HTTP only -- the middleware passes WebSocket
# scopes (/ws/ops) straight through -- and Starlette's default exclusions keep it off
# text/event-stream (the SSE feed must flush frame by frame) and already-compressed bodies
# (woff2 fonts, images, zip).
app.add_middleware(GZipMiddleware, minimum_size=1024)

# The RBAC allow-lists, the _actor rule and the operator-scoping helpers moved to
# api/deps.py when Phase 4 split the API into router modules -- a router cannot import
# main (main includes the routers). They are imported above; the routes below are
# unchanged, and api/deps.py is still the one place the operator clause is built.
#
# So, later, did the §9.3 allow-lists this file used to declare itself (INCIDENT_READERS,
# NOTE_AUTHORS, NOTE_AUTHOR_ROLE, PLATFORM_READERS, LEDGER_DOWNLOAD_ROLES): a review found lane
# routers gating the same §9.3 rows with READERS, because a router cannot import main.
# MEMORY_READERS joined them there. Each is imported above, so ``main.PLATFORM_READERS`` and
# the others still resolve.


# Deliberately open: the load balancer / container liveness probe. It has no session and
# must answer before anyone has logged in; it returns the operator profile name and
# nothing else. Pinned open by test_auth_skeleton.
@app.get("/health")
def health() -> dict[str, str]:
    s = _settings()
    return {"status": "ok", "operator": s.operator_profile}


# Deliberately open: the login screen has to name the operator, its regions and the shift
# BEFORE anyone has a role -- gating it would lock the door from the inside. It is safe to
# leave open only because the "email" block was removed from the body (see below); nothing
# incident-specific is served here. Pinned open by test_auth_skeleton.
@app.get("/api/v1/profile")
def profile() -> dict[str, Any]:
    op = _settings().operator
    return {
        "operator_id": op.operator_id,
        "display_name": op.display_name,
        "incident_prefix": op.incident_prefix,
        "autonomy_level": op.autonomy_level,
        "timezone": op.timezone,
        "regions": {
            k: {
                "label": v.label,
                "rnio": v.rnio,
                "fe_oncall": v.fe_oncall,
                "description": v.description,
                "coverage_areas": v.coverage_areas,
                "counties": v.counties,
            }
            for k, v in op.regions.items()
        },
        "locale_notes": op.locale_notes,
        "shift": current_shift(op),
        "shift_id": shift_id(op),
        # No "email" block here (§7.0.5): it carried the configured mailbox and
        # the demo recipient list, and /api/v1/profile is unauthenticated.
        # GET /api/v1/email/status still serves it for the Settings page.
    }


# §9.3 platform-status row. Gated, and NOT with READERS: the body carries the configured
# mailbox and the demo recipient list -- the very block Phase 1 removed from the
# unauthenticated /api/v1/profile. Serving it from a second route to anyone who asks would
# undo that fix.
@app.get("/api/v1/email/status", dependencies=[Depends(require_role(*PLATFORM_READERS))])
def get_email_status() -> dict[str, Any]:
    return email_status()


# admin only: this one actually SENDS mail to the configured recipients (§9.3 puts the
# actions in the platform row -- outbox retry, scheduler run -- with admin, not read).
@app.post("/api/v1/email/test", dependencies=[Depends(require_role("admin"))])
def send_test_email() -> dict[str, Any]:
    """Send a one-off test message to DEMO_EMAIL_TO / GMAIL_ADDRESS."""
    from noc_agents.adapters.email_smtp import send_demo_email

    result = send_demo_email(
        subject="[NOC DEMO] Kenya NOC Mission Control — test mail",
        body=(
            "This is a test from Kenya NOC Mission Control.\n\n"
            "If you received this, Gmail SMTP is configured correctly.\n"
            "Outage injects (and HITL approve for P1/P2) will email this inbox.\n"
        ),
    )
    return {
        "ok": result.ok,
        "mode": result.mode,
        "detail": result.detail,
        "to": result.to,
    }


# The scenario catalogue is canned text, but it is the operations floor's screen, so it is
# gated like the rest of the read surface rather than left open on the "it is only demo
# data" argument -- the route is what an operator reaches, and INCIDENT_READERS (§9.3 row
# 1's read column) is who may reach it.
@app.get("/api/v1/demo/scenarios", dependencies=[Depends(require_role(*INCIDENT_READERS))])
def list_scenarios() -> dict[str, Any]:
    return {
        "scenarios": [
            {
                "id": "rain_storm_mw",
                "name": "Heavy rains — Rift / Mt Kenya / Nairobi East MW cascade",
                "description": (
                    "Storm cells degrade microwave hops; parent HUBs fail then child sites "
                    "cascade. Agents fire live on Mission Control."
                ),
                "event_count": len(RAIN_STORM_EVENTS),
                "regions": ["RFT", "MTK", "NBI_E"],
                "events": scenario_payload(),
            }
        ]
    }


@app.get("/api/v1/demo/rain-storm/events", dependencies=[Depends(require_role(*INCIDENT_READERS))])
def rain_storm_events() -> dict[str, Any]:
    """Event templates only — frontend injects one-by-one for a live agent feed."""
    return {"scenario": "rain_storm_mw", "events": scenario_payload()}


@app.post("/api/v1/demo/rain-storm", dependencies=[Depends(require_role(*INGEST))])
def run_rain_storm(stagger_ms: int = 0) -> dict[str, Any]:
    """Inject heavy-rain MW cascade. stagger_ms>0 spaces events so UI feels live.

    Prefer frontend staggered inject for best live effect; this endpoint supports
    both bulk (0) and mild server-side stagger.

    Gated with INGEST, exactly like POST /events (§9.3): "demo" is the name of the
    payload, not of the code path — every event here goes through ``process_event`` and
    writes real incidents, agent runs, step rows, audit rows and outbox rows. An
    unauthenticated caller who could reach this could manufacture a storm in a
    production NOC one POST at a time.
    """
    clear_settings_cache()
    s = _settings()
    session = get_session()
    created: list[dict] = []
    try:
        for i, event in enumerate(RAIN_STORM_EVENTS):
            if stagger_ms > 0 and i > 0:
                time.sleep(min(stagger_ms, 5000) / 1000.0)
            inc = process_event(session, s, event)
            created.append(
                {
                    "incident_number": inc.incident_number,
                    "id": inc.id,
                    "priority": inc.priority,
                    "site_id": inc.site_id,
                    "region_code": inc.region_code,
                    "msp": inc.responsible_msp or inc.msp_name,
                    "status": inc.status,
                }
            )
        hub.publish_sync(
            RealtimeEvent(
                type="demo.rain_storm.complete",
                operator_id=s.operator.operator_id,
                payload={"count": len(created), "regions": ["RFT", "MTK", "NBI_E"]},
            )
        )
        return {
            "ok": True,
            "scenario": "rain_storm_mw",
            "count": len(created),
            "incidents": created,
            "events_template": scenario_payload(),
        }
    finally:
        session.close()


# Deliberately open, both of them: this IS the role switcher, so it is the one surface a
# caller must reach before they have a role. It grants nothing — with AUTH_DISABLED=false
# require_role() reads the signed cookie and never looks at this store, so setting a role
# here cannot widen what anyone may do (test_auth_skeleton pins that the switcher is a UI
# affordance, not an identity store).
@app.post("/api/v1/session")
def set_session(body: SessionIn, request: Request, response: Response) -> dict:
    """Set THIS client's demo role switcher (mints a noc_client cookie if needed)."""
    return auth.set_client_session(request, body.model_dump(), response)


# Deliberately open: the other half of the role switcher, read on every page load.
@app.get("/api/v1/session")
def get_session_api(request: Request) -> dict:
    return auth.get_client_session(request)


@app.post("/api/v1/events", dependencies=[Depends(require_role(*INGEST))])
def ingest_event(body: EventIngest) -> dict:
    session = get_session()
    try:
        # reload settings in case env changed in tests
        clear_settings_cache()
        s = get_settings()
        inc = process_event(session, s, body)
        return {"incident": incident_out(inc).model_dump()}
    finally:
        session.close()


# Same gate as POST /events, for the same reason (§9.3): this runs the identical 12-node
# lifecycle, once per element of the list.
@app.post("/api/v1/events/batch", dependencies=[Depends(require_role(*INGEST))])
def ingest_batch(events: list[EventIngest]) -> dict:
    session = get_session()
    try:
        clear_settings_cache()
        s = get_settings()
        outs = []
        for e in events:
            inc = process_event(session, s, e)
            outs.append(incident_out(inc).model_dump())
        return {"incidents": outs}
    finally:
        session.close()


# §9.3 row 1 read. INCIDENT_READERS (READERS + legal) since the A-04 review: legal holds R on
# this row, and the list must agree with its detail forms below.
@app.get("/api/v1/incidents", dependencies=[Depends(require_role(*INCIDENT_READERS))])
def list_incidents(
    priority: str | None = None,
    region: str | None = None,
    status: str | None = None,
    q: str | None = None,
) -> list[dict]:
    session = get_session()
    try:
        stmt = _owned(IncidentRow)
        if priority:
            stmt = stmt.where(IncidentRow.priority == priority.upper())
        if region:
            stmt = stmt.where(IncidentRow.region_code == region.upper())
        if status:
            stmt = stmt.where(IncidentRow.status == status.upper())
        rows = list(session.scalars(stmt.order_by(IncidentRow.created_at.desc())).all())
        if q:
            ql = q.lower()
            rows = [
                r
                for r in rows
                if ql in r.incident_number.lower()
                or ql in r.site_id.lower()
                or ql in (r.site_name or "").lower()
            ]
        # True ops order: P1 first, then P2, …
        prio = {"P1": 0, "P2": 1, "P3": 2, "P4": 3}
        rows.sort(key=lambda r: (prio.get(r.priority, 9), r.created_at))
        return [incident_out(r).model_dump() for r in rows]
    finally:
        session.close()


# The detail form of GET /api/v1/incidents, and gated with the same tuple: gating the list
# while the record it lists answers anonymously is not a control.
@app.get("/api/v1/incidents/{incident_id}")
def get_incident(
    incident_id: str,
    principal: auth.Principal = Depends(require_role(*INCIDENT_READERS)),
) -> dict:
    session = get_session()
    try:
        row = _get_owned(session, IncidentRow, incident_id, what="incident")
        payload = incident_out(row).model_dump()
        # §7.11.5: advisory memory, computed at request time, additive, and on this
        # single-incident route ONLY -- the list route is polled by the wallboard, so an
        # advisory there would be one recall per ticket per refresh (§7.11.4). None when
        # MEMORY_ENABLED is off, which makes the key null rather than absent (§7.11.11
        # test 25). The helper never raises; a broken memory store cannot 500 this route.
        #
        # WHO gets it follows §9.3's MEMORY row, not the incident row that gates this route.
        # Since round 4 the two sets are equal (row 1 is read strictly: the vendor roles are
        # "notes only" and never reach this route), so this is defence in depth -- the day a
        # vendor binding (D18) lets a coordinator read their own tickets, the memory row still
        # gives them "—": the advisory is EARLIER tickets at this site,
        # including ones a different MSP worked, with their numbers, restore minutes,
        # resolution codes and scrubbed free text. §7.11.4 says this route's advisory is served
        # "as today"; api/deps.MEMORY_READERS records why §9.3 wins. They get None -- the key
        # stays present, the same shape as the flag being off, so a renderer written against
        # it keeps working. With AUTH_DISABLED=true there is no identity (the switcher is a UI
        # affordance and every gate is inert), so the demo computes it for everyone as before.
        # The approval card's frozen copy (agents/hitl.py) is served only by /hitl/pending,
        # which is OPERATIONS -- inside the memory row.
        memory_reader = not principal.authenticated or principal.role in MEMORY_READERS
        payload["advisory"] = advisory_for_incident(session, row) if memory_reader else None
        return payload
    finally:
        session.close()


# §9.3 row 1: NOTE_AUTHORS, which is the only allow-list in this file that is WIDER than
# OPERATIONS. A note is the one write an msp_coordinator or a field_engineer is given, and
# it is not a comment box: apply_work_note_side_effects can stamp the vendor's first
# response (``first_vendor_note_at``, which the silent-vendor count keys on) and — via
# note_declares_restored — record the service as restored. So it is gated in both
# directions: the two vendor roles are let in because this is their route, and
# management/planning/legal are kept out because §9.3 gives them R, not R/W, on this row.
@app.post("/api/v1/incidents/{incident_id}/notes")
def add_note(
    incident_id: str,
    body: NoteIn,
    principal: auth.Principal = Depends(require_role(*NOTE_AUTHORS)),
) -> dict:
    # WHO wrote a note -- the name, the capacity, the channel -- comes from the principal once
    # there is one; the body supplies only WHAT it says. Opening this route to the two vendor
    # roles made three body fields forgeable, and each one moves a number someone is measured
    # by:
    #   * author_role: "MSP"/"FE" stamps first_vendor_note_at, the start of the vendor MTTA
    #     clock (§7.6.2), and makes the note count as the vendor's in the scorecard and the
    #     silence chase. An analyst could make a vendor look faster; a vendor could post as
    #     "NOC" and never be counted as having answered.
    #   * author: becomes restored_by on a restoring note (§7.0.8) -- the name on the moment
    #     MTTR and the restore SLA are measured to.
    #   * source: "msp"/"fe"/"vendor" ALSO count as the vendor's (services/scorecard.py,
    #     services/worklog_monitor.py), and "monitor" is the silence chase's own dedupe
    #     marker, so a posted "monitor" note would suppress the next chase note. An authenticated
    #     human's note arrived through this route, so its channel is "ui" -- what the SPA
    #     already sends.
    # The role is DERIVED, not checked-and-refused with a 422: the SPA computes author_role
    # from the demo switcher ("msp" in the role name, else "NOC"), which is wrong for a field
    # engineer, so refusing would break the real UI the day auth is turned on -- and there is
    # nothing a client can add to what the signed cookie already says. The name follows
    # _actor, as on every other route. With AUTH_DISABLED=true there is no identity to forge
    # (the switcher is a UI affordance), so the demo keeps the body's role and channel.
    author = _actor(principal, body.author)
    if principal.authenticated:
        author_role, source = NOTE_AUTHOR_ROLE[principal.role], "ui"
    else:
        author_role, source = body.author_role, body.source
    session = get_session()
    try:
        row = _get_owned(session, IncidentRow, incident_id, what="incident")
        note = WorkNoteRow(
            incident_id=incident_id,
            author=author,
            author_role=author_role,
            body=body.body,
            source=source,
        )
        session.add(note)
        apply_work_note_side_effects(
            session,
            row,
            author_role=author_role,
            body=body.body,
            author=author,  # becomes restored_by when this note restores (§7.0.8)
            mark_restored=body.mark_restored,
            vendor_tt_ref=body.vendor_tt_ref,
            msp_eta_at=body.msp_eta_at,
            msp_root_cause=body.msp_root_cause,
            msp_action_taken=body.msp_action_taken,
            msp_percent_complete=body.msp_percent_complete,
        )
        session.commit()
        hub.publish_sync(
            RealtimeEvent(
                type="incident.note",
                operator_id=row.operator_id,
                incident_id=row.id,
                payload={
                    "incident_number": row.incident_number,
                    "author": author,
                    "status": row.status,
                },
            )
        )
        return {"ok": True, "note_id": note.id, "status": row.status}
    finally:
        session.close()


@app.post("/api/v1/incidents/{incident_id}/close")
def close_inc(
    incident_id: str,
    body: CloseIn,
    principal: auth.Principal = Depends(require_role(*OPERATIONS)),
) -> dict:
    actor = _actor(principal, body.closed_by)
    session = get_session()
    try:
        row = _get_owned(session, IncidentRow, incident_id, what="incident")
        close_incident(
            session,
            row,
            closed_by=actor,
            resolution_code=body.resolution_code,
            resolution_summary=body.resolution_summary,
        )
        session.commit()
        return {"ok": True, "incident": incident_out(row).model_dump()}
    finally:
        session.close()


class RestoreIn(BaseModel):
    """Body of ``POST /api/v1/incidents/{id}/restore`` (spec §7.0.8).

    ``restored_at`` is optional and exists because the supervisor is usually
    told at 02:41 that the site came back at 02:12 — recording the keystroke
    instead of the observation inflates MTTR on every such ticket. Omit it and
    "now" is used. Defined here, next to its only route, rather than in
    ``domain.schemas``: it is an input shape for one endpoint, and nothing else
    imports it.
    """

    note: str
    restored_at: datetime | None = None


@app.post("/api/v1/incidents/{incident_id}/restore")
def restore_inc(
    incident_id: str,
    body: RestoreIn,
    principal: auth.Principal = Depends(require_role(*OPERATIONS)),
) -> dict:
    """A named human asserting the service is back — ``restored_source=SUPERVISOR``.

    This is the only restore path that is neither a flag on a vendor's note nor
    a regex over its text, so it is also the only one whose timestamp can be
    defended in an SLA dispute. The gate is inert while ``AUTH_DISABLED=true``
    (the demo default); ``restored_by`` is the acting principal, never a name
    the client chose for itself.
    """
    note = body.note.strip()
    if not note:
        raise HTTPException(400, "note required")
    at: datetime | None = None
    if body.restored_at is not None:
        # Store naive UTC (the DB contract); accept an offset-aware time from a
        # client and convert rather than silently writing a local wall clock.
        at = to_utc(body.restored_at).replace(tzinfo=None)
        if at > utcnow() + timedelta(minutes=1):  # a minute of clock skew, not a window
            raise HTTPException(400, "restored_at cannot be in the future")
    session = get_session()
    try:
        row = _get_owned(session, IncidentRow, incident_id, what="incident")
        if row.status in TERMINAL_STATUSES:
            raise HTTPException(409, f"incident is {row.status}; reopen it before recording a restore")
        restore_incident(
            session,
            row,
            restored_by=principal.display_name,
            note=note,
            restored_at=at,
        )
        session.commit()
        hub.publish_sync(
            RealtimeEvent(
                type="incident.note",
                operator_id=row.operator_id,
                incident_id=row.id,
                payload={
                    "incident_number": row.incident_number,
                    "author": principal.display_name,
                    "status": row.status,
                },
            )
        )
        return {"ok": True, "incident": incident_out(row).model_dump()}
    finally:
        session.close()


@app.post("/api/v1/incidents/{incident_id}/reassign", dependencies=[Depends(require_role(*OPERATIONS))])
def reassign_inc(incident_id: str, body: ReassignIn) -> dict:
    if not body.reason.strip():
        raise HTTPException(400, "reason required")
    session = get_session()
    try:
        row = _get_owned(session, IncidentRow, incident_id, what="incident")
        reassign_incident(
            session,
            row,
            assignee_type=body.assignee_type,
            assignee_name=body.assignee_name,
            msp_name=body.msp_name or (body.assignee_name if body.assignee_type == "MSP" else None),
            fe_name=body.fe_name,
            by=body.by,
            reason=body.reason,
        )
        session.commit()
        return {"ok": True, "incident": incident_out(row).model_dump()}
    finally:
        session.close()


@app.post("/api/v1/monitor/tick", dependencies=[Depends(require_role(*OPERATIONS))])
def monitor_tick() -> dict:
    """Run WorklogMonitorAgent silence/SLA chase across open tickets.

    A write, not a read: the chase writes work notes, re-arms ``next_update_at`` and can
    raise HITL tasks across every open ticket, so it is gated like the other floor actions
    (§9.3 row 1, R/W). It is deliberately NOT admin-only the way
    ``POST /scheduler/run/{job}`` is — the analyst on shift presses this one; admin owns the
    scheduler itself.
    """
    session = get_session()
    try:
        clear_settings_cache()
        s = _settings()
        results = chase_silent_incidents(session, s.operator)
        return {
            "chased": len(results),
            "results": [
                {
                    "incident_id": r.incident_id,
                    "incident_number": r.incident_number,
                    "action": r.action,
                    "detail": r.detail,
                    "note_written": r.note_written,
                    "task_created": r.task_created,
                }
                for r in results
            ],
        }
    finally:
        session.close()


# --- Scheduler (§7.0.3) ---------------------------------------------------------


def _weather_card():
    """The weather poller's JobCard, or None if it is not registered."""
    return job_card(WEATHER_JOB_NAME)


# §9.3 row 1 names "signals read" explicitly, with R for legal: INCIDENT_READERS.
@app.get("/api/v1/signals/weather/regions", dependencies=[Depends(require_role(*INCIDENT_READERS))])
def weather_regions() -> dict:
    """The Wallboard risk strip's feed (spec §7.3.2).

    Shape: ``{enabled, regions: {CODE: weather_risk, ...}, cap: {...}}``.

    Three deliberate properties, because this drives a screen a night shift trusts:

    * **Every configured region appears**, even one that has never been fetched — it comes
      back as ``null`` so the strip can show six tiles with a gap, rather than silently
      omitting a region an operator expects to see. A missing tile reads as "no risk"; an
      empty tile reads as "no data", and those are very different claims.
    * **``stale`` is recomputed against now**, not read from the stored column
      (``pollers.weather.weather_risk_for_region`` does this). The poller is fail-soft and
      keeps the last good row when a provider dies, so the presence of a reading is no
      evidence of its freshness.
    * **``enabled`` is reported honestly.** With ``WEATHER_ENABLED`` off nothing polls, so
      the rows are frozen; the strip hides itself rather than presenting stale data as a
      live forecast.

    ``cap`` (KMD CAP alerts) is declared here with an empty alert list because the strip's
    contract includes it, but the CAP poller is a later Phase 3 lane. It is explicitly
    ``available: false`` so nobody reads "no alerts" as "no warnings in force".
    """
    session = get_session()
    try:
        cfg = _settings().operator
        now = utcnow()
        regions = {
            code: weather_risk_for_region(session, cfg.operator_id, code, now)
            for code in cfg.regions
        }
        return {
            "enabled": job_enabled(_weather_card()) if _weather_card() else False,
            "regions": regions,
            "cap": {"available": False, "newest_sent": None, "stale": True, "alerts": []},
        }
    finally:
        session.close()


# §9.3 platform row, read side (the run side below is admin).
@app.get("/api/v1/scheduler/status", dependencies=[Depends(require_role(*PLATFORM_READERS))])
def scheduler_status() -> dict:
    """Lease holder, age of the last tick and every job's last outcome — read from the DB, so
    any process answers for the one that ticks (the "AGENTS OFFLINE" runbook check)."""
    session = get_session()
    try:
        return status_payload(session)
    finally:
        session.close()


@app.post("/api/v1/scheduler/run/{job}", dependencies=[Depends(require_role("admin"))])
def scheduler_run(job: str) -> dict:
    """Run one job now, as a SCHEDULE-triggered run, resetting its circuit breaker first.
    Works whether or not the loop is enabled: it is the operator's manual override."""
    card = job_card(job)
    if card is None:
        raise HTTPException(404, "job not found")
    clear_settings_cache()
    outcome = run_job(card, _settings(), reset_circuit=True)
    return outcome.to_dict()


def _lifecycle_run(session, incident_id: str) -> AgentRunRow | None:
    """The run the UI workflow graph and timeline show: the latest lifecycle-graph run, so a later
    run of another graph (a monitor or assist run) cannot blank the 12-node view; falls back to
    the latest run of any graph when the incident has no lifecycle run."""
    latest = (
        _owned(AgentRunRow)
        .where(AgentRunRow.incident_id == incident_id)
        .order_by(AgentRunRow.started_at.desc())
    )
    return session.scalar(latest.where(AgentRunRow.graph_name == LIFECYCLE_GRAPH)) or session.scalar(latest)


# §9.3 row 1 names "timeline" and "workflow" as reads of the incident surface, so both take
# the same tuple as the incident they belong to.
@app.get("/api/v1/incidents/{incident_id}/timeline", dependencies=[Depends(require_role(*INCIDENT_READERS))])
def timeline(incident_id: str) -> list[dict]:
    session = get_session()
    try:
        _get_owned(session, IncidentRow, incident_id, what="incident")  # the child rows below are keyed by it
        items: list[dict] = []
        run = _lifecycle_run(session, incident_id)
        if run:
            for s in run.steps:
                items.append(
                    {
                        "kind": "agent_step",
                        "ts": s.started_at,
                        "title": f"{s.agent_name} · {s.node_name}",
                        "status": s.status,
                        "detail": s.rationale or s.output_summary,
                    }
                )
        for n in session.scalars(
            select(WorkNoteRow).where(WorkNoteRow.incident_id == incident_id).order_by(WorkNoteRow.created_at)
        ):
            items.append(
                {
                    "kind": "note",
                    "ts": n.created_at,
                    "title": f"{n.author} ({n.author_role})",
                    "status": "NOTE",
                    "detail": n.body,
                }
            )
        for b in session.scalars(
            select(BroadcastRow).where(BroadcastRow.incident_id == incident_id)
        ):
            items.append(
                {
                    "kind": "broadcast",
                    "ts": b.sent_at or utcnow(),
                    "title": f"{b.channel} → {b.audience}",
                    "status": b.status,
                    "detail": b.message[:240],
                }
            )
        items.sort(key=lambda x: x["ts"] or utcnow())
        return items
    finally:
        session.close()


@app.get("/api/v1/incidents/{incident_id}/workflow", dependencies=[Depends(require_role(*INCIDENT_READERS))])
def workflow(incident_id: str) -> WorkflowOut:
    session = get_session()
    try:
        _get_owned(session, IncidentRow, incident_id, what="incident")
        run = _lifecycle_run(session, incident_id)
        status_map: dict[str, str] = {}
        steps = []
        run_id = None
        if run:
            run_id = run.id
            for s in run.steps:
                st = s.status.lower()
                if st == "succeeded":
                    status_map[s.node_name] = "succeeded"
                elif st == "waiting_hitl":
                    status_map[s.node_name] = "waiting_hitl"
                elif st == "failed":
                    status_map[s.node_name] = "failed"
                elif st == "started":
                    status_map[s.node_name] = "running"
                else:
                    status_map[s.node_name] = st
                steps.append(step_out(s))
        nodes = graph_status_map(status_map)
        return WorkflowOut(incident_id=incident_id, run_id=run_id, nodes=nodes, edges=WORKFLOW_EDGES, steps=steps)
    finally:
        session.close()


# §9.3 row 1 read; the same tuple as the incidents the runs belong to.
@app.get("/api/v1/runs", dependencies=[Depends(require_role(*INCIDENT_READERS))])
def list_runs(incident_id: str | None = None, graph_name: str | None = None) -> list[dict]:
    """Recent runs, newest first. ``graph_name`` (e.g. ``incident_lifecycle``) keeps on-demand
    assist runs out of a panel that only wants lifecycle runs."""
    session = get_session()
    try:
        stmt = _owned(AgentRunRow).order_by(AgentRunRow.started_at.desc()).limit(50)
        if incident_id:
            stmt = stmt.where(AgentRunRow.incident_id == incident_id)
        if graph_name:
            stmt = stmt.where(AgentRunRow.graph_name == graph_name)
        rows = session.scalars(stmt).all()
        return [run_out(r).model_dump() for r in rows]
    finally:
        session.close()


# The detail form of GET /api/v1/runs; same tuple, same reason as the incident detail.
@app.get("/api/v1/runs/{run_id}", dependencies=[Depends(require_role(*INCIDENT_READERS))])
def get_run(run_id: str) -> dict:
    session = get_session()
    try:
        run = _get_owned(session, AgentRunRow, run_id, what="run")
        return run_out(run).model_dump()
    finally:
        session.close()


# §9.3 platform row names "agents" explicitly: the registry describes the system's own
# shape (every agent, its mission, its tools and its autonomy), which is an internal
# document, not something a vendor's coordinator needs.
@app.get("/api/v1/agents", dependencies=[Depends(require_role(*PLATFORM_READERS))])
def list_agents() -> list[dict]:
    return agent_catalog()


@app.get("/api/v1/agents/{name}", dependencies=[Depends(require_role(*PLATFORM_READERS))])
def get_agent(name: str) -> dict:
    """One ``agent_catalog()`` entry by ``AgentProfile.name`` (§7.1.3), or 404."""
    for entry in agent_catalog():
        if entry["name"] == name:
            return entry
    raise HTTPException(404, "agent not found")


# §9.3 rows 2, 4, 5 and 7: the inbox shows a role the cards it may act on and no others --
# planning its schedule and window cards, management its handover cards, a supervisor the
# rest (api/deps.HITL_DECIDERS). noc_analyst, whose row-2 cell is "—", is refused the inbox:
# the Wallboard's pending COUNT stays on the open /metrics/summary. With AUTH_DISABLED=true
# there is no identity, so the demo lists every card as before.
@app.get("/api/v1/hitl/pending")
def hitl_pending(principal: auth.Principal = Depends(require_role(*HITL_ROLES))) -> list[dict]:
    session = get_session()
    try:
        tasks = session.scalars(
            _owned(HitlTaskRow)
            .where(HitlTaskRow.status.in_(["PENDING", "CLAIMED"]))
            .order_by(HitlTaskRow.created_at.asc())
        ).all()
        if principal.authenticated:
            tasks = [t for t in tasks if principal.role in hitl_deciders(t.task_type)]
        out = []
        for t in tasks:
            # Since schema v8 a maintenance card (APPROVE_SCHEDULE / APPROVE_MAINTENANCE_WINDOW)
            # has incident_id NULL. session.get(IncidentRow, None) returns None today but emits
            # an SAWarning that a future SQLAlchemy may raise, and pyproject pins only a floor --
            # so every lookup of a task's incident below is guarded the same way.
            inc = session.get(IncidentRow, t.incident_id) if t.incident_id else None
            out.append(
                {
                    "id": t.id,
                    "incident_id": t.incident_id,
                    "incident_number": inc.incident_number if inc else None,
                    "priority": inc.priority if inc else None,
                    "site_id": inc.site_id if inc else None,
                    "task_type": t.task_type,
                    "status": t.status,
                    "claimed_by": t.claimed_by,
                    "created_at": t.created_at,
                    "proposed_payload": t.proposed_payload,
                }
            )
        return out
    finally:
        session.close()


VALID_PRIORITIES = ("P1", "P2", "P3", "P4")


def _open_task_or_409(session, task_id: str) -> HitlTaskRow:
    """The task if it is still PENDING/CLAIMED; 404 when unknown or another operator's, 409
    once a decision was made.

    The ownership check is in the lookup's WHERE clause (``_get_owned`` joins through the
    task's incident), so a supervisor never holds another operator's task object at all.
    This is the cheap early answer; ``_transition_or_409`` is the real guard against two
    concurrent decisions on the same task.
    """
    t = _get_owned(session, HitlTaskRow, task_id, what="task")
    if not is_open(t):
        raise HTTPException(409, f"task already {t.status}")
    return t


def _transition_or_409(session, t: HitlTaskRow, new_status: str, **values) -> None:
    """Compare-and-set the task to ``new_status``; 409 when another decision got there first."""
    if not transition_open_task(session, t, new_status, **values):
        raise HTTPException(409, f"task already {t.status}")


def _not_the_raiser_or_403(t: HitlTaskRow, actor: str) -> None:
    """Raiser ≠ approver (§6.5): the person who raised a task may not decide it.

    ``created_by`` is ``agent:SupervisorAgent`` for pipeline-raised tasks and NULL for rows
    that predate the column, so the rule only ever bites a human deciding their own escalation.
    """
    if is_raiser(t, actor):
        raise HTTPException(403, "the person who raised a task may not approve or reject it")


def _may_act_on_or_403(principal: auth.Principal, t: HitlTaskRow) -> None:
    """§9.3: who may claim, approve or reject a card depends on its TYPE, which a route-level
    allow-list cannot see -- planning decides schedule and window cards only (row 2),
    management approves handovers (row 4), scorecard adjudication and notices are duty
    manager's (row 5). ``api/deps.HITL_DECIDERS`` is the table; a type it does not name takes
    row 2's supervisors. Inert with AUTH_DISABLED=true, like every gate (no identity to check).

    403, not 404: the task is this operator's (``_open_task_or_409`` already applied the
    operator clause), so its existence is not the secret here -- the refusal is about role.
    """
    if principal.authenticated and principal.role not in hitl_deciders(t.task_type):
        raise HTTPException(403, f"role '{principal.role}' may not act on {t.task_type} cards")


def _apply_overrides(inc: IncidentRow, overrides: dict) -> None:
    if overrides.get("priority"):
        inc.priority = overrides["priority"]
    if overrides.get("assignee"):
        inc.assignee_name = overrides["assignee"]
        if overrides.get("msp_name"):
            inc.msp_name = overrides["msp_name"]


def _finish_waiting_run(session, inc: IncidentRow, status: str, error: str | None = None) -> AgentRunRow | None:
    """Close the incident's latest WAITING_HITL run; step rows are left as they are."""
    run = session.scalar(
        _owned(AgentRunRow)
        .where(AgentRunRow.incident_id == inc.id, AgentRunRow.status == "WAITING_HITL")
        .order_by(AgentRunRow.started_at.desc())
    )
    if run:
        run.status = status
        run.finished_at = utcnow()
        if error is not None:
            run.error_summary = error
    return run


def _cancel_pending_broadcasts(session, incident_id: str) -> int:
    rows = session.scalars(
        select(BroadcastRow).where(BroadcastRow.incident_id == incident_id, BroadcastRow.status == "PENDING_HITL")
    ).all()
    for b in rows:
        b.status = "CANCELLED"  # 9 chars: fits BroadcastRow.status String(16)
        b.sent_at = None
    return len(rows)


# Claim is "this card is mine to decide". It follows §9.3 row 2, where noc_analyst is "—",
# claim included: the same roles as the decision, per card type (_may_act_on_or_403).
# Until round 4 any operations role could claim, recorded as a deliberate deviation so an
# analyst could put their name on a card they were working. Dropped, because a claim is not
# only a label: it takes the card out of "unclaimed", and the §6.5 escalation ladder (T+5
# nudge, T+15 duty manager, T+30 red on the Wallboard) keys on unclaimed -- an analyst's claim
# on a card they cannot decide would silence the ladder that fetches someone who can.
@app.post("/api/v1/hitl/{task_id}/claim")
def hitl_claim(
    task_id: str,
    body: HitlDecision,
    principal: auth.Principal = Depends(require_role(*HITL_ROLES)),
) -> dict:
    actor = _actor(principal, body.resolved_by)
    session = get_session()
    try:
        t = _open_task_or_409(session, task_id)
        _may_act_on_or_403(principal, t)
        _transition_or_409(session, t, "CLAIMED", claimed_by=actor, claimed_at=utcnow())
        inc = session.get(IncidentRow, t.incident_id) if t.incident_id else None  # v8: see hitl_pending
        if inc:
            sync_incident_hitl_scalars(session, inc)  # every task writer derives the scalars (C3)
        session.commit()
        hub.publish_sync(
            RealtimeEvent(
                type="hitl.claimed",
                operator_id=_settings().operator.operator_id,
                incident_id=t.incident_id,
                payload={"task_id": t.id, "claimed_by": t.claimed_by},
            )
        )
        return {"ok": True, "claimed_by": t.claimed_by}
    finally:
        session.close()


@app.post("/api/v1/hitl/{task_id}/approve")
def hitl_approve(
    task_id: str,
    body: HitlDecision,
    principal: auth.Principal = Depends(require_role(*HITL_ROLES)),
) -> dict:
    actor = _actor(principal, body.resolved_by)
    session = get_session()
    try:
        t = _open_task_or_409(session, task_id)
        _may_act_on_or_403(principal, t)  # §9.3 by card type: planning, management, ...
        _not_the_raiser_or_403(t, actor)
        gating = t.task_type == GATING_TASK_TYPE
        priority = body.overrides.get("priority")
        if gating and priority and priority not in VALID_PRIORITIES:
            raise HTTPException(400, f"priority override must be one of {', '.join(VALID_PRIORITIES)}")
        # §6.5 / §2.1 R6: a reason on approve is required only behind HITL_APPROVE_REASON_REQUIRED
        # (default false), so the legacy {"resolved_by": ...} bodies keep working. No length floor.
        if approve_reason_required() and not (body.reason or "").strip():
            raise HTTPException(400, "reason required on approve")
        decided_at = utcnow()
        _transition_or_409(session, t, "APPROVED", resolved_by=actor, resolved_at=decided_at, reason=body.reason)
        inc = session.get(IncidentRow, t.incident_id) if t.incident_id else None  # v8: see hitl_pending
        incident_number = inc.incident_number if inc else None
        if inc:
            if gating:  # only the broadcast gate releases the held drafts and finishes the run
                _apply_overrides(inc, body.overrides)
                # Defect #11: the draft the supervisor reviewed is NOT what leaves. Rebuild the
                # envelope from the incident as overridden, re-render every channel, and release
                # the new rows through outbox.release_held; the old wording is never enqueued.
                released = rerender_and_release(
                    session, inc, _settings().operator, task=t, approved_by=actor, approved_at=decided_at
                )
                t.edited = 1 if released.edited else 0
                if released.released and sync_drain_enabled():
                    drain_after_commit(session)  # transmit after THIS commit, never inside it
                _finish_waiting_run(session, inc, "SUCCEEDED")
                note = "HITL approved broadcast/assignment."
            elif t.task_type == HANDOVER_TASK_TYPE:
                # Defect #30: the handover mail has been sitting HELD since the route queued
                # it. Approval is the ONLY thing that moves it to PENDING; the dispatcher
                # would refuse it otherwise (REJECTED_UNAPPROVED). No run is waiting on it.
                moved = release_handover(session, t, approved_by=actor, approved_at=decided_at)
                if moved and sync_drain_enabled():
                    drain_after_commit(session)  # transmit after THIS commit, never inside it
                which = f" {t.entity_id}" if t.entity_id else ""  # the shift id, when the row carries one
                note = f"HITL approved shift handover{which}: email released to the outbox."
            else:  # GENERIC (monitor escalation) and any other type: record the decision only
                note = f"HITL approved ({t.task_type})."
            # Defect #24: whatever this approval changed (priority, assignee, a released
            # broadcast), the brief must not still describe the incident as it was.
            upsert_brief(session, inc, _settings().operator)
            session.add(
                WorkNoteRow(
                    incident_id=inc.id,
                    author=actor,
                    author_role="NOC",
                    body=note,
                    source="hitl",
                )
            )
            sync_incident_hitl_scalars(session, inc)
        session.commit()
        hub.publish_sync(
            RealtimeEvent(
                type="hitl.approved",
                operator_id=_settings().operator.operator_id,
                incident_id=t.incident_id,
                payload={
                    "task_id": t.id,
                    "resolved_by": actor,
                    "task_type": t.task_type,
                    "incident_number": incident_number,
                },
            )
        )
        return {"ok": True}
    finally:
        session.close()


@app.post("/api/v1/hitl/{task_id}/reject")
def hitl_reject(
    task_id: str,
    body: HitlDecision,
    principal: auth.Principal = Depends(require_role(*HITL_ROLES)),
) -> dict:
    actor = _actor(principal, body.resolved_by)
    if not body.reason:
        raise HTTPException(400, "reason required on reject")
    session = get_session()
    try:
        t = _open_task_or_409(session, task_id)
        _may_act_on_or_403(principal, t)
        _not_the_raiser_or_403(t, actor)
        _transition_or_409(
            session, t, "REJECTED", resolved_by=actor, resolved_at=utcnow(), reason=body.reason
        )
        inc = session.get(IncidentRow, t.incident_id) if t.incident_id else None  # v8: see hitl_pending
        incident_number = inc.incident_number if inc else None
        finished: dict | None = None  # agent.run.finished payload, published after the commit
        if inc:
            if t.task_type == GATING_TASK_TYPE:  # the broadcast will never go out: finalise drafts and run
                _cancel_pending_broadcasts(session, inc.id)  # CANCELLED: the tested draft status (§2.1 R7)
                suppress_held_outbox(session, inc.id, reason=f"hitl_rejected: {body.reason}")  # SUPPRESSED is the outbox's
                error = f"HITL rejected: {body.reason}"
                run = _finish_waiting_run(session, inc, "CANCELLED", error=error)
                if run:
                    finished = {
                        "seq": max((s.seq for s in run.steps), default=0),
                        "incident_number": incident_number,
                        "run_id": run.id,
                        "status": "CANCELLED",
                        "error": error,
                    }
            session.add(
                WorkNoteRow(
                    incident_id=inc.id,
                    author=actor,
                    author_role="NOC",
                    body=f"HITL rejected: {body.reason}",
                    source="hitl",
                )
            )
            sync_incident_hitl_scalars(session, inc)
        session.commit()
        operator_id = _settings().operator.operator_id
        if finished:
            hub.publish_sync(
                RealtimeEvent(
                    type="agent.run.finished",
                    operator_id=operator_id,
                    incident_id=t.incident_id,
                    run_id=finished["run_id"],
                    payload=finished,
                )
            )
        hub.publish_sync(
            RealtimeEvent(
                type="hitl.rejected",
                operator_id=operator_id,
                incident_id=t.incident_id,
                payload={
                    "task_id": t.id,
                    "reason": body.reason,
                    "task_type": t.task_type,
                    "incident_number": incident_number,
                },
            )
        )
        return {"ok": True}
    finally:
        session.close()


# An incident read (the published brief for one incident), so the incident tuple.
@app.get("/api/v1/briefs/{incident_id}", dependencies=[Depends(require_role(*INCIDENT_READERS))])
def get_brief(incident_id: str) -> dict:
    session = get_session()
    try:
        b = session.scalar(
            _owned(IncidentBriefRow)
            .where(IncidentBriefRow.incident_id == incident_id)
            .order_by(IncidentBriefRow.updated_at.desc())
        )
        if not b:
            raise HTTPException(404, "brief not found")
        return {"incident_id": incident_id, "body": b.body, "updated_at": b.updated_at}
    finally:
        session.close()


# --- optional Claude assist layer (Stage D): on-demand only, never on POST /events ---


# §9.3 platform row ("MCP status" and its neighbours): which provider and model this
# deployment is wired to is configuration, not incident data. No secret material is in the
# body — the gate is about the configuration, not about a leak.
@app.get("/api/v1/llm/status", dependencies=[Depends(require_role(*PLATFORM_READERS))])
def get_llm_status() -> dict:
    """Feature-flag view for the UI. Contains no secret material."""
    return llm_status()


# Both assist routes are gated with OPERATIONS although neither writes to the ticket: they
# SPEND — each one can call the hosted model when LLM_ENABLED is on, and an anonymous
# caller with a loop could run down the budget (§7.0.9 spend cap) without ever touching a
# row. OPERATIONS is the floor that reads the output.
@app.post("/api/v1/incidents/{incident_id}/analysis", dependencies=[Depends(require_role(*OPERATIONS))])
def incident_analysis(incident_id: str) -> dict:
    """Root-cause hypotheses for analysts (fable → opus, else template). Changes nothing on the ticket."""
    session = get_session()
    try:
        inc = _get_owned(session, IncidentRow, incident_id, what="incident")
        return analyse_incident(session, _settings(), inc)
    finally:
        session.close()


@app.post("/api/v1/incidents/{incident_id}/brief/draft", dependencies=[Depends(require_role(*OPERATIONS))])
def incident_brief_draft(incident_id: str) -> dict:
    """Executive brief draft (opus, else template). Text only: no IncidentBriefRow is written."""
    session = get_session()
    try:
        inc = _get_owned(session, IncidentRow, incident_id, what="incident")
        return draft_exec_brief(session, _settings(), inc)
    finally:
        session.close()


# Problem records are recurring-fault history over the same incidents INCIDENT_READERS read.
@app.get("/api/v1/problems", dependencies=[Depends(require_role(*INCIDENT_READERS))])
def list_problems() -> list[dict]:
    session = get_session()
    try:
        rows = session.scalars(_owned(ProblemRow).order_by(ProblemRow.last_seen.desc())).all()
        return [
            {
                "id": p.id,
                "problem_number": p.problem_number,
                "site_id": p.site_id,
                "region_code": p.region_code,
                "occurrence_count": p.occurrence_count,
                "status": p.status,
                "summary": p.summary,
                "dominant_failure_domain": p.dominant_failure_domain,
                "linked_incident_ids": p.linked_incident_ids,
            }
            for p in rows
        ]
    finally:
        session.close()


# A search is a handful of words, not a query language: the Audit trail sends what was typed
# plus the ids of the tickets it names (a step row carries the ticket's id, never its number).
AUDIT_SEARCH_MAX_TERMS = 25
AUDIT_SEARCH_MAX_LEN = 200


@app.get("/api/v1/audit", dependencies=[Depends(require_role(*AUDIT_READERS))])
def list_audit(
    limit: int = Query(100, ge=1, le=1000),
    q: list[str] | None = Query(None),
) -> list[dict]:
    """The operator's audit rows, newest first. Each row also carries ``run_id`` and ``node``
    lifted from its payload (None when absent), so the Audit trail can keep a run's intake
    steps, written before the ticket existed, with the ticket they opened; and
    ``incident_id`` when the payload names one (an approval card's escalation rows do).

    ``q`` (repeatable) narrows the rows before ``limit`` is applied, so a search reaches past
    the newest page: a row is kept when any term occurs, ignoring case, in its actor, action,
    rationale or entity_id. ``%`` and ``_`` in a term are literal. Blank terms are ignored;
    more than 25 terms, or a term over 200 characters, is a 422.

    Step rows are written as JSON (graph/instrumentation.py). Older step rows hold a Python repr
    (``{'node': 'INGEST', 'output': ...}``): their node is still read, by pattern and never by
    evaluation; their run_id was never recorded. Nothing stored is changed here.
    """
    terms = list(dict.fromkeys(t.strip() for t in (q or []) if t and t.strip()))
    if len(terms) > AUDIT_SEARCH_MAX_TERMS:
        raise HTTPException(422, f"at most {AUDIT_SEARCH_MAX_TERMS} search terms")
    if any(len(t) > AUDIT_SEARCH_MAX_LEN for t in terms):
        raise HTTPException(422, f"a search term is at most {AUDIT_SEARCH_MAX_LEN} characters")
    legacy_node = re.compile(r"\{'node': '([A-Za-z0-9_.:-]{1,64})'")
    json_prefix = re.compile(r'\{"node": "([A-Za-z0-9_.:-]{1,64})", "run_id": "([A-Za-z0-9-]{1,64})"')

    def refs(raw: str | None) -> tuple[str | None, str | None, str | None]:
        text = raw or ""
        try:
            data = json.loads(text)
        except (ValueError, RecursionError):  # a pre-JSON repr, a payload cut at the cap, or absurd nesting
            cut = json_prefix.match(text)
            if cut:
                return cut.group(2), cut.group(1), None
            old = legacy_node.match(text)
            return None, (old.group(1) if old else None), None
        if not isinstance(data, dict):
            return None, None, None

        def text_or_none(key: str) -> str | None:
            value = data.get(key)
            return value if isinstance(value, str) and value else None

        return text_or_none("run_id"), text_or_none("node"), text_or_none("incident_id")

    session = get_session()
    try:
        stmt = _owned(AuditRow)
        if terms:
            fields = (AuditRow.actor, AuditRow.action, AuditRow.rationale, AuditRow.entity_id)
            stmt = stmt.where(or_(*(f.icontains(t, autoescape=True) for t in terms for f in fields)))
        rows = session.scalars(stmt.order_by(AuditRow.ts.desc()).limit(limit)).all()
        out: list[dict] = []
        for a in rows:
            run_id, node, incident_id = refs(a.payload_json)
            out.append(
                {
                    "id": a.id,
                    "ts": a.ts,
                    "actor": a.actor,
                    "action": a.action,
                    "entity_type": a.entity_type,
                    "entity_id": a.entity_id,
                    "rationale": a.rationale,
                    "run_id": run_id,
                    "node": node,
                    "incident_id": incident_id,
                }
            )
        return out
    finally:
        session.close()


# Deliberately open: it returns the shift window, the shift id and the timezone — the exact
# three values the deliberately-open /api/v1/profile already serves. Gating the smaller
# route while the larger one answers anonymously would be theatre, not a control. If
# /profile is ever closed, close this with it.
@app.get("/api/v1/shifts/current")
def shift_current() -> dict:
    op = _settings().operator
    return {"shift": current_shift(op), "shift_id": shift_id(op), "timezone": op.timezone}


# --- Shift ledger download (§5.3.9 P2, §7.9.3) ---------------------------------
# The workbook is built in memory from ``shift_ledger`` rows and streamed. The route
# opens NO file: defect #6 was that a download read a path off disk, and ``shift_id``
# arrives from the URL, which is attacker input.
#
# Two independent gates stand in front of that, in this order:
#   1. the §7.9.3 pattern — ``2026-09-17_DAY``. Anything else is 422 before a Path
#      object exists at all, so ``..``, ``/etc/passwd``, ``C:\\...``, ``\\\\host\\share``
#      and every percent-encoded separator are rejected as *strings*;
#   2. ``Path.is_relative_to`` on the resolved candidate, per OWASP ("use indexes rather
#      than actual portions of file names"). The pattern already makes this unreachable —
#      it is here because a pattern is one edit away from being loosened, and because the
#      resolved path, not the string, is what a traversal actually escapes with.
# The candidate path is then thrown away: the response body comes from the DB.
XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
# LEDGER_DOWNLOAD_ROLES (narrower than OPERATIONS: ledger rows carry names and access notes)
# lives in api/deps.py with the other §9.3 allow-lists.
_SHIFT_ID_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}_(DAY|NIGHT)$")
#: The DB key is ``<operator>:<date>:<SHIFT>`` (agents/ledger.py), which carries the
#: operator; the URL form does not, and must not — the route is operator-scoped already.
LEDGER_SHEET_HEADERS = [
    "Row written (UTC)",
    "Incident No",
    "Priority",
    "Site",
    "Type",
    "Region",
    "Owner",
    "Status",
    "Shift",
    "M-PESA Risk",
    "SLA Risk",
    "Last note",
]


def _validated_shift_id(raw: str) -> str:
    """``2026-09-17_DAY`` or 422. The value is a key, never a path (§7.9.3)."""
    if not _SHIFT_ID_RE.match(raw or ""):
        raise HTTPException(422, "shift_id must look like 2026-09-17_DAY or 2026-09-17_NIGHT")
    root = ledger_root().resolve()
    candidate = (root / f"ledger_{raw}.xlsx").resolve()
    if not candidate.is_relative_to(root):  # defence in depth; the pattern got here first
        raise HTTPException(422, "shift_id must look like 2026-09-17_DAY or 2026-09-17_NIGHT")
    return raw


def _stored_shift_key(operator_id: str, url_shift_id: str) -> str:
    """URL ``2026-09-17_DAY`` → the ``shift_ledger.shift_id`` the LEDGER node wrote."""
    date_part, _, shift_part = url_shift_id.partition("_")
    return f"{operator_id}:{date_part}:{shift_part}"


def _ledger_workbook(rows: list[ShiftLedgerRow], handover: dict, url_shift_id: str) -> bytes:
    """The .xlsx bytes: one sheet of ledger rows, one of the current handover package."""
    wb = Workbook()
    ws = wb.active
    ws.title = "ShiftLedger"
    ws.append(LEDGER_SHEET_HEADERS)
    for r in rows:
        ws.append(
            [
                str(r.row_written_at or ""),
                r.incident_number,
                r.priority,
                r.site,
                r.site_type,
                r.region_code,
                r.owner,
                r.status,
                r.shift_type,
                "YES" if r.mpesa_risk else "NO",
                "YES" if r.sla_risk else "NO",
                r.last_note_summary or "",
            ]
        )
    # §7.9.3 asks for a Handover sheet from build_handover. That package is always "now",
    # so it is labelled with the shift it was generated for — a download of last week's
    # ledger must not read as if this handover belonged to that shift.
    hs = wb.create_sheet("Handover")
    hs.append([f"Ledger shift: {url_shift_id}"])
    hs.append([f"Handover package generated for the {str(handover.get('shift', '')).upper()} shift (current)"])
    hs.append([f"Watchlist: {handover.get('watch_count', 0)} of {handover.get('open_total', 0)} open"])
    hs.append([])
    for line in str(handover.get("body", "")).split("\n"):
        hs.append([line])
    buf = io.BytesIO()
    wb.save(buf)  # openpyxl writes the zip into the buffer; nothing touches the file system
    wb.close()
    return buf.getvalue()


# --- Production guard (§7.0.5) -------------------------------------------------
# AUTH_DISABLED=true with NOC_ENV=production means an unauthenticated API in
# front of real data, so the routes that DOWNLOAD the shift ledger are not
# registered at all — a 404 beats serving every shift's incident/owner rows to
# anyone who finds the URL. The other §7.0.5 families guard themselves in their own
# router, with the same helper and the same one log line: complaints
# (api/routers/complaints.py) and contracts (api/routers/contracts.py); individual
# metrics joins when it lands. Everything else stays registered and is gated by
# require_role() instead.
PRODUCTION_GUARDED_ROUTES: tuple[str, ...] = (
    "GET /api/v1/shifts/ledger",
    "GET /api/v1/shifts/ledger/{shift_id}.xlsx",
)
_PRODUCTION_GUARD = auth.production_guard_active()

if _PRODUCTION_GUARD:
    auth.log_production_guard(PRODUCTION_GUARDED_ROUTES)
else:

    # §9.3 names only "Ledger xlsx download" (row 4); this JSON list serves the same rows,
    # owner names included. Row 1 (incident reads) could claim it too, so the cell is
    # AMBIGUOUS; resolved to the stricter row 4 -- noc_analyst out, management in -- and
    # listed for the owner.
    @app.get("/api/v1/shifts/ledger", dependencies=[Depends(require_role(*LEDGER_DOWNLOAD_ROLES))])
    def shift_ledger() -> list[dict]:
        session = get_session()
        try:
            rows = session.scalars(
                _owned(ShiftLedgerRow).order_by(ShiftLedgerRow.row_written_at.desc()).limit(200)
            ).all()
            return [
                {
                    "incident_number": r.incident_number,
                    "priority": r.priority,
                    "site": r.site,
                    "region_code": r.region_code,
                    "owner": r.owner,
                    "status": r.status,
                    "shift_type": r.shift_type,
                    "mpesa_risk": r.mpesa_risk,
                    "row_written_at": r.row_written_at,
                }
                for r in rows
            ]
        finally:
            session.close()

    # ``{shift_id:path}`` deliberately captures slashes too. With the default converter a
    # traversal-shaped id simply fails to match the route and answers 404; capturing it
    # means _validated_shift_id sees it and answers 422 — the route rejects the input
    # itself rather than relying on the router happening to miss.
    @app.get(
        "/api/v1/shifts/ledger/{shift_id:path}.xlsx",
        dependencies=[Depends(require_role(*LEDGER_DOWNLOAD_ROLES))],
    )
    def shift_ledger_xlsx(shift_id: str) -> Response:
        validated = _validated_shift_id(shift_id)
        session = get_session()
        try:
            op = _settings().operator
            rows = session.scalars(
                _owned(ShiftLedgerRow)
                .where(ShiftLedgerRow.shift_id == _stored_shift_key(op.operator_id, validated))
                .order_by(ShiftLedgerRow.row_written_at.asc())
            ).all()
            # An empty shift is a real answer, not a 404: "nothing happened on nights" is
            # exactly what a supervisor downloads the workbook to prove.
            content = _ledger_workbook(list(rows), build_handover(session, op), validated)
        finally:
            session.close()
        return Response(
            content=content,
            media_type=XLSX_MEDIA_TYPE,
            headers={
                "Content-Disposition": f'attachment; filename="ledger_{validated}.xlsx"',
                "Cache-Control": "no-store",
            },
        )


@app.post("/api/v1/shifts/handover", dependencies=[Depends(require_role(*SUPERVISORS))])
def shift_handover() -> dict:
    session = get_session()
    try:
        clear_settings_cache()
        s = get_settings()
        ho = build_handover(session, s.operator)
        sid = shift_id(s.operator)
        if handover_requires_hitl():
            # Defect #30 (§5.3.12): the handover no longer leaves because someone pressed a
            # button. The envelope + APPROVE_HANDOVER task are written here and the mail is
            # queued HELD; POST /hitl/{id}/approve is the only thing that releases it. The
            # drain below is deliberately still called: it proves the held row is not
            # transmitted by the very pass that transmits everything else.
            gate = queue_handover(session, s.operator, ho, shift_id=sid)
            session.commit()
            if sync_drain_enabled():
                drain_outbox(session)
            ho["hitl"] = gate
            ho["email"] = (
                handover_email_response(session, gate["outbox_id"], None)
                if gate["outbox_id"]
                else {
                    "ok": False,
                    "mode": "outbox",
                    "detail": gate["blocked_reason"],
                    "to": [],
                    "status": gate["status"],
                    "outbox_id": None,
                }
            )
            if gate["task_id"]:
                hub.publish_sync(
                    RealtimeEvent(
                        type="hitl.created",
                        operator_id=s.operator.operator_id,
                        payload={
                            "task_id": gate["task_id"],
                            "task_type": HANDOVER_TASK_TYPE,
                            "shift_id": sid,
                            "watch_count": ho["watch_count"],
                        },
                    )
                )
            return ho
        # HANDOVER_REQUIRES_HITL=false — the pre-defect-#30 path, unchanged.
        # Outbox (§7.0.2): the mail is a row first. It commits here and is transmitted by the
        # drain that follows — never inside the transaction.
        queued = dispatch_handover_email(
            session, ho["subject"], ho["body"], operator_id=s.operator.operator_id, shift_id=sid
        )
        session.commit()
        report = drain_outbox(session) if sync_drain_enabled() else None
        ho["hitl"] = {"required": False, "task_id": None, "alert_id": None, "outbox_id": queued["outbox_id"],
                      "status": queued["status"], "blocked_reason": None}
        ho["email"] = handover_email_response(session, queued["outbox_id"], report)
        return ho
    finally:
        session.close()


# Deliberately open, and this one is a recorded decision rather than an oversight:
# test_auth_skeleton::test_deliberately_open_routes_stay_open_when_auth_is_enforced pins it
# open on the grounds that it is aggregate counts with no incident detail — no id, no site,
# no name, no text. The conformance audit (A-04) listed it among the ungated reads; the
# existing decision is kept, because closing it would move a test that was written
# deliberately. Everything it counts is gated at the routes that serve the rows.
@app.get("/api/v1/metrics/summary")
def metrics() -> MetricsSummary:
    session = get_session()
    try:
        op = _settings().operator
        open_rows = session.scalars(
            _owned(IncidentRow).where(IncidentRow.status.not_in(["CLOSED", "CANCELLED"]))
        ).all()
        by_p = {"P1": 0, "P2": 0, "P3": 0, "P4": 0}
        by_r: dict[str, int] = {}
        sla_risk = 0
        silent = 0
        now = utcnow()
        for r in open_rows:
            by_p[r.priority] = by_p.get(r.priority, 0) + 1
            by_r[r.region_code] = by_r.get(r.region_code, 0) + 1
            if r.sla_restore_due and r.sla_restore_due < now:
                sla_risk += 1
            if r.sla_ack_due and r.sla_ack_due < now and not r.first_vendor_note_at:
                silent += 1
        hitl = session.scalar(
            _operator_scoped(select(func.count()).select_from(HitlTaskRow), HitlTaskRow).where(
                HitlTaskRow.status.in_(["PENDING", "CLAIMED"])
            )
        ) or 0
        running = session.scalar(
            _operator_scoped(select(func.count()).select_from(AgentRunRow), AgentRunRow).where(
                AgentRunRow.status.in_(["RUNNING", "WAITING_HITL"])
            )
        ) or 0
        problems = session.scalar(
            _operator_scoped(select(func.count()).select_from(ProblemRow), ProblemRow).where(
                ProblemRow.status.in_(["OPEN", "MONITORING"])
            )
        ) or 0
        return MetricsSummary(
            operator_id=op.operator_id,
            operator_display_name=op.display_name,
            autonomy_level=op.autonomy_level,
            open_total=len(open_rows),
            by_priority=by_p,
            hitl_pending=int(hitl),
            sla_risk=sla_risk,
            agents_running=int(running),
            by_region=by_r,
            problems_open=int(problems),
            silent_at_risk=silent,
        )
    finally:
        session.close()


# The site catalogue is network infrastructure inventory (ids, names, coordinates, parent
# hubs) — §9.4 class "network facts", not public data. INCIDENT_READERS: the field engineer
# and the MSP coordinator working a site need to look it up, and legal holds R on row 1.
@app.get("/api/v1/sites", dependencies=[Depends(require_role(*INCIDENT_READERS))])
def list_sites() -> list[dict]:
    path = ROOT / "data" / "seed" / "safaricom_sites.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


# The SSE stream carries the same envelopes as the incident routes (incident number, site,
# priority), so it takes the same tuple. It is an ordinary GET, so the ordinary dependency
# works; EventSource sends cookies on a same-origin connection, which is how the signed
# session reaches it.
@app.get("/api/v1/stream/events", dependencies=[Depends(require_role(*INCIDENT_READERS))])
async def sse_events():
    q = hub.subscribe()

    async def gen():
        try:
            for item in hub.recent(10):
                yield f"data: {json.dumps(item)}\n\n"
            while True:
                item = await q.get()
                if item is None:  # overflow sentinel: the hub dropped this subscriber; end the stream
                    break
                yield f"data: {json.dumps(item)}\n\n"
        finally:
            hub.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream")


# Gated, through the socket's own seam (A-14). require_role() cannot do it: it is an HTTP
# dependency whose ``request: Request`` parameter FastAPI never binds on a WebSocket, so
# declaring it in ``dependencies=`` here kills the handshake with a TypeError, auth on or
# off. ``auth.authorise_socket`` is the socket's answer -- the same signed session, closed
# with 1008 before the accept instead of raising 401/403 -- and INCIDENT_READERS is the
# tuple because this feed carries incident numbers, sites, run and HITL events: §9.3 row 1
# read, the same as the SSE twin above. Inert with AUTH_DISABLED=true, so the demo and
# test_contracts.py's frozen frame contract are untouched.
@app.websocket("/ws/ops")
async def ws_ops(ws: WebSocket, since: int | None = None):
    """Ops feed. ``?since=N`` replays only the records newer than seq N (spec §7.0.4), each as
    ``{"seq": N, "event": <envelope>}``; without it the last 15 envelopes are replayed as
    before. Live frames stay the bare six-key envelope: the frozen contract
    (``tests/system/test_contracts.py::test_ws_ops_replays_recent_and_delivers_live_events``)
    pins that shape, so the §7.0.4 live-path wrapper waits for that contract to be re-cut
    (``hub.subscribe(with_seq=True)`` is the one-line switch)."""
    # Before accept() and before any replay: a refused caller must never be sent a frame.
    if await auth.authorise_socket(ws, *INCIDENT_READERS) is None:
        return  # closed with 1008; auth.authorise_socket said why in the close reason
    await ws.accept()
    q = hub.subscribe()
    try:
        if since is not None:
            for frame in hub.since(since):
                await ws.send_json(frame)
        else:
            for item in hub.recent(15):
                await ws.send_json(item)
        while True:
            item = await q.get()
            if item is None:  # overflow sentinel: close so the client reconnects and replays recent()
                await ws.close(code=1013)
                break
            await ws.send_json(item)
    except WebSocketDisconnect:
        pass
    except Exception:
        try:
            await ws.close()
        except Exception:
            pass
    finally:
        hub.unsubscribe(q)


# Serve built frontend if present (registered last so API routes always win)
# Feature-lane routers (api/routers/). Registered HERE, above the SPA mount, and not at
# the end of the file: the "/{full_path:path}" fallback below matches every path, and
# FastAPI resolves in registration order, so a router included after it would be dead --
# every one of its routes would return the index page instead. Included after the @app
# routes above so those keep precedence on any path both could match.
for _router in ROUTERS:
    app.include_router(_router)


#: Vite names every file under /assets after a hash of its bytes (index-3sW5Qz-1.css), so the
#: bytes behind such a URL never change: a browser may keep them for a year without asking.
#: index.html is the opposite -- it names the current hashes -- so it is always revalidated.
IMMUTABLE_ASSET = "public, max-age=31536000, immutable"
#: Other files at the root of the build (public/: the self-hosted fonts, their licences) keep
#: their names across builds, so they are cached for a week rather than forever.
STABLE_FILE = "public, max-age=604800"
SPA_SHELL = "no-cache"


class HashedAssets(StaticFiles):
    """The /assets mount, answering with the long-lived cache header (304s included)."""

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = IMMUTABLE_ASSET
        return response


if FRONTEND_DIST.exists():
    assets = FRONTEND_DIST / "assets"
    if assets.exists():
        app.mount("/assets", HashedAssets(directory=str(assets)), name="assets")

    # Deliberately open, both: this is the built SPA shell. The login screen is served from
    # here, so a gate would make it impossible to ever acquire the cookie that passes the
    # gate. The bundle is static assets and carries no operator data — every byte of that
    # arrives through the API routes above, which are gated.
    @app.get("/")
    def spa_index():
        index = FRONTEND_DIST / "index.html"
        return FileResponse(index, headers={"Cache-Control": SPA_SHELL})

    @app.get("/{full_path:path}")
    def spa_fallback(full_path: str):
        # Never steal API / health / docs / openapi
        blocked = ("api/", "api", "ws/", "ws", "health", "docs", "openapi.json", "redoc")
        if full_path.startswith(blocked) or full_path in blocked:
            raise HTTPException(404, detail="Not found")
        candidate = (FRONTEND_DIST / full_path).resolve()
        # Path traversal guard (§7.9.3). ``startswith`` compared STRINGS, so a sibling
        # directory whose name merely begins with the dist path (``dist-backup``) passed
        # it; is_relative_to compares path components, which is the actual question.
        if not candidate.is_relative_to(FRONTEND_DIST.resolve()):
            raise HTTPException(404)
        if candidate.exists() and candidate.is_file():
            shell = candidate.name == "index.html"
            return FileResponse(candidate, headers={"Cache-Control": SPA_SHELL if shell else STABLE_FILE})
        return FileResponse(FRONTEND_DIST / "index.html", headers={"Cache-Control": SPA_SHELL})

else:
    # No build to serve: say so at the door. 503, because the thing this port is asked for is
    # not ready; the API routes above are unaffected and /health still answers 200.
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def ui_not_built() -> HTMLResponse:
        return HTMLResponse(NO_BUILD_PAGE, status_code=503, headers={"Cache-Control": "no-cache"})


def run() -> None:
    import uvicorn

    uvicorn.run("noc_agents.main:app", host="0.0.0.0", port=8000, reload=False)


if __name__ == "__main__":
    run()
