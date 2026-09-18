const BASE = "";

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
    ...init,
  });
  if (!r.ok) {
    const t = await r.text();
    throw new Error(`${r.status}: ${t}`);
  }
  return r.json();
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

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
  audit: () => req<any[]>("/api/v1/audit"),
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
};

/**
 * Live rain storm: inject events one-by-one with delay so WebSocket
 * agent activity streams onto Mission Control in real time.
 */
export async function runLiveRainStorm(
  onProgress?: (i: number, total: number, incident: any) => void,
  delayMs = 1600
): Promise<{ count: number; incidents: any[] }> {
  const tpl = await api.rainStormEvents();
  const events: any[] = tpl.events || [];
  const incidents: any[] = [];
  for (let i = 0; i < events.length; i++) {
    const res = await api.inject(events[i]);
    const inc = res.incident;
    incidents.push(inc);
    onProgress?.(i + 1, events.length, inc);
    if (i < events.length - 1) await sleep(delayMs);
  }
  return { count: incidents.length, incidents };
}
