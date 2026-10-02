import { noteFailure, noteResponse } from "./realtime/apiHealth";

const BASE = "";

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  let r: Response;
  try {
    r = await fetch(`${BASE}${path}`, {
      headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
      ...init,
    });
  } catch (e) {
    // Every call reports to one tracker: "API unreachable" needs two failures in a row, any calls.
    noteFailure(path);
    throw e;
  }
  noteResponse(r, path);
  if (!r.ok) {
    const t = await r.text();
    throw new Error(`${r.status}: ${t}`);
  }
  return r.json();
}


export const api = {
  profile: () => req<any>("/api/v1/profile"),
  metrics: () => req<any>("/api/v1/metrics/summary"),
  incidents: (qs = "") => req<any[]>(`/api/v1/incidents${qs}`),
  incident: (id: string) => req<any>(`/api/v1/incidents/${id}`),
  workflow: (id: string) => req<any>(`/api/v1/incidents/${id}/workflow`),
  timeline: (id: string) => req<any[]>(`/api/v1/incidents/${id}/timeline`),
  hitl: () => req<any[]>("/api/v1/hitl/pending"),
  claim: (id: string, who: string) =>
    req(`/api/v1/hitl/${id}/claim`, { method: "POST", body: JSON.stringify({ resolved_by: who }) }),
  /**
   * `reason` and `overrides` are optional and omitted from the body when empty,
   * so this stays byte-identical to the old `{resolved_by}` call for callers
   * that pass neither (`HitlDecision` defaults `overrides` to `{}` and `reason`
   * to `None`). Spec §6.5: the reason lands on the audit row, and is required by
   * the server when `HITL_APPROVE_REASON_REQUIRED=true`.
   */
  approve: (id: string, who: string, reason?: string, overrides?: Record<string, any>) =>
    req(`/api/v1/hitl/${id}/approve`, {
      method: "POST",
      body: JSON.stringify({
        resolved_by: who,
        ...(reason && reason.trim() ? { reason: reason.trim() } : {}),
        ...(overrides && Object.keys(overrides).length ? { overrides } : {}),
      }),
    }),
  reject: (id: string, who: string, reason: string) =>
    req(`/api/v1/hitl/${id}/reject`, {
      method: "POST",
      body: JSON.stringify({ resolved_by: who, reason }),
    }),
  agents: () => req<any[]>("/api/v1/agents"),
  problems: () => req<any[]>("/api/v1/problems"),
  audit: (limit = 100) => req<any[]>(`/api/v1/audit?limit=${limit}`),
  ledger: () => req<any[]>("/api/v1/shifts/ledger"),
  handover: () => req<any>("/api/v1/shifts/handover", { method: "POST" }),
  sites: () => req<any[]>("/api/v1/sites"),
  /**
   * Agent memory M0 (spec §7.11): prior resolved incidents at one site, for the
   * "Earlier at this site" workspace panel. Answers 200 with an empty list when the site is
   * unknown or `MEMORY_ENABLED` is off, so a caller never has to treat it as a failure path.
   * `site_id` is a NOC-typed string and goes in a path segment, hence the encode.
   */
  siteMemory: (siteId: string) => req<any>(`/api/v1/memory/sites/${encodeURIComponent(siteId)}`),
  brief: (id: string) => req<any>(`/api/v1/briefs/${id}`),
  session: () => req<any>("/api/v1/session"),
  setSession: (body: any) =>
    req("/api/v1/session", { method: "POST", body: JSON.stringify(body) }),
  inject: (body: any) =>
    req<any>("/api/v1/events", { method: "POST", body: JSON.stringify(body) }),
  addNote: (id: string, body: any) =>
    req<any>(`/api/v1/incidents/${id}/notes`, { method: "POST", body: JSON.stringify(body) }),
  close: (id: string, body: any) =>
    req<any>(`/api/v1/incidents/${id}/close`, { method: "POST", body: JSON.stringify(body) }),
  reassign: (id: string, body: any) =>
    req<any>(`/api/v1/incidents/${id}/reassign`, { method: "POST", body: JSON.stringify(body) }),
  monitorTick: () => req<any>("/api/v1/monitor/tick", { method: "POST", body: "{}" }),
  rainStormBulk: () => req<any>("/api/v1/demo/rain-storm", { method: "POST" }),
  rainStormEvents: () => req<any>("/api/v1/demo/rain-storm/events"),
  scenarios: () => req<any>("/api/v1/demo/scenarios"),
  emailStatus: () => req<any>("/api/v1/email/status"),
  emailTest: () => req<any>("/api/v1/email/test", { method: "POST" }),
  runs: () => req<any[]>("/api/v1/runs"),
  /**
   * The lifecycle runs of ONE incident, newest first. The workspace draws the run that
   * opened the ticket (`lib/agents.pickCreatingRun`) rather than the newest, which for a
   * HUB major is usually a two-step merge; see that helper for why.
   */
  runsFor: (incidentId: string) =>
    req<any[]>(`/api/v1/runs?incident_id=${encodeURIComponent(incidentId)}&graph_name=incident_lifecycle`),
  lifecycleRuns: () => req<any[]>("/api/v1/runs?graph_name=incident_lifecycle"),
  /**
   * Productivity rollup for the Showcase page (`services/productivity.py`): what the agents
   * did in the window and what the operator profile says it would have cost by hand.
   * `windowHours` 0 = everything on record. Shape pinned by tests/unit/test_productivity.py.
   */
  productivity: (windowHours = 24) => req<any>(`/api/v1/metrics/productivity?window_hours=${windowHours}`),
  /**
   * §7.3.2 Wallboard risk strip: `{regions: {NBI_E: weather_risk, …}, cap: {…}}`.
   *
   * The read endpoint is Phase 3 and does not exist yet — it 404s today, which
   * `req` turns into a rejected promise like any other failure. That is handled,
   * not an oversight: `components/RiskStrip.tsx` treats a rejection or an empty
   * `regions` map as "no weather data" and renders nothing at all, so with
   * `WEATHER_ENABLED=false` (the default) the wallboard is byte-for-byte what it
   * is today.
   */
  weatherRegions: () => req<any>("/api/v1/signals/weather/regions"),
  /**
   * Regions dashboard (spec §7.4.2). One row per region on the operator profile —
   * including regions with nothing open, which is the point. The response shape is
   * pinned server-side by `tests/unit/test_dashboard_regions.py`.
   */
  dashboardRegions: () => req<any>("/api/v1/dashboard/regions"),
  /** Contracts lane (spec §7.8). Every route is 404 while CONTRACTS_ENABLED=false; the page and drawer read that as "off". */
  contractsStatus: () => req<any>("/api/v1/contracts/status"),
  contracts: () => req<any[]>("/api/v1/contracts"),
  contractsSearch: (q: string, incidentId?: string | null) => req<any>(`/api/v1/contracts/clauses/search?q=${encodeURIComponent(q)}${incidentId ? `&incident_id=${encodeURIComponent(incidentId)}` : ""}`),
  contractsAsk: (body: any) => req<any>("/api/v1/contracts/ask", { method: "POST", body: JSON.stringify(body) }),
  /**
   * Planned maintenance (spec §7.5). The whole lane 404s while `MAINTENANCE_ENABLED` is off —
   * the shipped default — so `pages/Maintenance.tsx` reads a 404 as "the feature is off" and
   * renders how to turn it on, rather than an error.
   *
   * There is deliberately no `approve` here: an `APPROVE_SCHEDULE` or
   * `APPROVE_MAINTENANCE_WINDOW` card is approved on the ordinary HITL Inbox through
   * `api.approve`, so raiser≠approver and the audit trail have one home.
   * `maintenanceScheduleWindow` is what acts on that approval, and it answers 403 until the
   * window's OWN card is approved — a task's schedule approval never releases a window.
   */
  maintenanceWindows: () => req<any[]>("/api/v1/maintenance/windows"),
  maintenanceWindow: (id: string) => req<any>(`/api/v1/maintenance/windows/${id}`),
  maintenanceTasks: (qs = "") => req<any[]>(`/api/v1/maintenance/tasks${qs}`),
  maintenancePlans: () => req<any[]>("/api/v1/maintenance/plans"),
  maintenanceRequestWindowApproval: (id: string) =>
    req<any>(`/api/v1/maintenance/windows/${id}/request-approval`, { method: "POST", body: "{}" }),
  maintenanceScheduleWindow: (id: string, overrides?: { override_rain?: boolean; override_reason?: string }) =>
    req<any>(`/api/v1/maintenance/windows/${id}/schedule`, { method: "POST", body: JSON.stringify(overrides || {}) }),
  maintenanceCancelWindow: (id: string, reason: string) =>
    req<any>(`/api/v1/maintenance/windows/${id}/cancel`, { method: "POST", body: JSON.stringify({ reason }) }),
  maintenanceStopClockProposal: (incidentId: string) =>
    req<any>(`/api/v1/maintenance/incidents/${incidentId}/stop-clock-proposal`),
  /**
   * Post-incident reviews (spec §7.7.2, `api/routers/pir.py`). Every route 404s while
   * `PIR_ENABLED` is off — `pages/Pirs.tsx` reads that as "off", not as an error. PATCH and
   * publish answer 422 with the server's exact words (the blameless sentence; every publish
   * blocker joined by "; "), which `lib/apiError.detailOf` unwraps intact.
   */
  pirs: (status = "") => req<any[]>(`/api/v1/pir${status ? `?status=${encodeURIComponent(status)}` : ""}`),
  pir: (id: string) => req<any>(`/api/v1/pir/${id}`),
  pirPatch: (id: string, body: any) => req<any>(`/api/v1/pir/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  pirAddAction: (id: string, body: any) =>
    req<any>(`/api/v1/pir/${id}/actions`, { method: "POST", body: JSON.stringify(body) }),
  pirPatchAction: (id: string, actionId: string, body: any) =>
    req<any>(`/api/v1/pir/${id}/actions/${actionId}`, { method: "PATCH", body: JSON.stringify(body) }),
  pirPublish: (id: string, body: { reviewer?: string; rationale?: string }) =>
    req<any>(`/api/v1/pir/${id}/publish`, { method: "POST", body: JSON.stringify(body) }),
  pirDraftLlm: (id: string) => req<any>(`/api/v1/pir/${id}/draft/llm`, { method: "POST", body: "{}" }),
  pirAwaitingReview: () => req<any>("/api/v1/pir/awaiting-review"),
  openPirForIncident: (incidentId: string) =>
    req<any>(`/api/v1/incidents/${incidentId}/pir`, { method: "POST", body: "{}" }),
  /** Stop clocks (spec §7.6.3, `api/routers/clocks.py`). 404 while `SCORECARDS_ENABLED` is off. */
  incidentClock: (incidentId: string) => req<any>(`/api/v1/incidents/${incidentId}/clock`),
  clockOpen: (incidentId: string, body: { scc_code: string; reason: string; started_at?: string }) =>
    req<any>(`/api/v1/incidents/${incidentId}/clock`, { method: "POST", body: JSON.stringify(body) }),
  clockClose: (incidentId: string, eventId: string, body: { ended_at?: string; reason?: string }) =>
    req<any>(`/api/v1/incidents/${incidentId}/clock/${eventId}/close`, { method: "POST", body: JSON.stringify(body) }),
  clockReverse: (incidentId: string, eventId: string, body: { reason: string }) =>
    req<any>(`/api/v1/incidents/${incidentId}/clock/${eventId}/reverse`, { method: "POST", body: JSON.stringify(body) }),
  /** Regulatory clocks (spec §5.3.20). 200 with `enabled:false` while `REGULATORY_ENABLED` is off. */
  incidentRegulatory: (incidentId: string) => req<any>(`/api/v1/incidents/${incidentId}/regulatory`),
  /** Scheduler liveness (spec §7.0.3) — the poll-side truth behind the Wallboard "AGENTS OFFLINE" badge. */
  schedulerStatus: () => req<any>("/api/v1/scheduler/status"),
};
