/** Per-browser-session demo state and the live storm runner. In a plain module, not in a
 *  component file, so App and the pages that read them never import each other. */

/** Set once a storm has finished in this browser session; the guide reads "Storm done". Nothing
 *  starts a storm by itself: the presenter's press of Launch is the opening beat. */
export const STORM_DONE_KEY = "noc_storm_done_v1";

/** The guided demo's current step, so the bar follows the presenter across pages. */
export const GUIDE_STEP_KEY = "noc_guide_v1";

/** How long one alarm may take (the inject and the look-up after it) before the storm stops. */
export const STORM_ALARM_TIMEOUT_MS = 20_000;
/** The gap between two alarms, so each run lands on the rail on its own. */
export const STORM_GAP_MS = 1500;

export type StormPhase = "idle" | "starting" | "running" | "settling" | "done" | "failed";

/** Everything a page shows about the storm. App owns one; Mission control, Settings and the
 *  guide read it. */
export interface StormState {
  phase: StormPhase;
  /** Alarms in the scenario (0 until the templates arrive). */
  total: number;
  /** Alarms injected so far. */
  done: number;
  opened: number;
  folded: number;
  /** The sentence for the latest alarm, or what is happening now. */
  text: string;
  /** Why it stopped, in the server's words (phase "failed"). */
  error: string;
  firstIncidentId: string | null;
  lastOpenedId: string | null;
}

export const STORM_IDLE: StormState = {
  phase: "idle",
  total: 0,
  done: 0,
  opened: 0,
  folded: 0,
  text: "",
  error: "",
  firstIncidentId: null,
  lastOpenedId: null,
};

export function isStorming(s: StormState): boolean {
  return s.phase === "starting" || s.phase === "running" || s.phase === "settling";
}

/** What one alarm did: opened a ticket, or folded into an open one at Correlate. */
export interface StormAlarmResult {
  site: string;
  outcome: "opened" | "folded";
  incident: any;
  /** The ticket it folded into, when it folded. */
  into: string | null;
}

/**
 * fetch with a deadline. A failure reads like api.ts's (`"<status>: <body>"`), so
 * lib/apiError.detailOf unwraps the server's sentence; a deadline reads as a sentence of its own.
 */
async function call<T>(path: string, init: RequestInit | undefined, timeoutMs: number): Promise<T> {
  const ctl = new AbortController();
  const timer = window.setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const r = await fetch(path, {
      ...init,
      headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
      signal: ctl.signal,
    });
    if (!r.ok) {
      const t = await r.text();
      throw new Error(`${r.status}: ${t}`);
    }
    return (await r.json()) as T;
  } catch (e: any) {
    if (ctl.signal.aborted) throw new Error(`The API did not answer within ${Math.round(timeoutMs / 1000)} s.`);
    throw e;
  } finally {
    window.clearTimeout(timer);
  }
}

const sleep = (ms: number) => new Promise<void>((r) => window.setTimeout(r, ms));

/** The scenario's alarms, in the order the storm sends them. */
export async function fetchStormTemplates(timeoutMs = STORM_ALARM_TIMEOUT_MS): Promise<any[]> {
  const tpl = await call<any>("/api/v1/demo/rain-storm/events", undefined, timeoutMs);
  return Array.isArray(tpl?.events) ? tpl.events : [];
}

/**
 * Did this alarm open a ticket or fold into one? The inject answers with the incident either way
 * (the new ticket, or the open ticket it joined), so the alarm's own run says which: the newest
 * lifecycle run on that incident opened it when it carries a Ticket step. If that look-up fails,
 * a different site on the answer means a child folded under its HUB.
 */
async function classify(ev: any, inc: any, timeoutMs: number): Promise<{ outcome: "opened" | "folded"; into: string | null }> {
  const fallback = (): { outcome: "opened" | "folded"; into: string | null } =>
    inc?.site_id && ev?.site_id && inc.site_id !== ev.site_id
      ? { outcome: "folded", into: inc?.incident_number ?? null }
      : { outcome: "opened", into: null };
  if (!inc?.id) return fallback();
  try {
    const runs = await call<any[]>(
      `/api/v1/runs?incident_id=${encodeURIComponent(inc.id)}&graph_name=incident_lifecycle`,
      undefined,
      timeoutMs
    );
    const newest = Array.isArray(runs) ? runs[0] : null;
    if (!newest || !Array.isArray(newest.steps)) return fallback();
    const opened = newest.steps.some((s: any) => s?.node_name === "TICKET");
    return opened ? { outcome: "opened", into: null } : { outcome: "folded", into: inc?.incident_number ?? null };
  } catch {
    return fallback();
  }
}

/**
 * Send the scenario's alarms one at a time from index `from`, `gapMs` apart, each with its own
 * `timeoutMs` deadline. Resolves when the last alarm has been processed; rejects on the first
 * failure, after `onAlarm` has reported every alarm that did go through (so a caller can offer
 * "Resume storm" from the next one).
 */
export async function runStorm(opts: {
  templates: any[];
  from: number;
  gapMs?: number;
  timeoutMs?: number;
  onAlarm: (index: number, total: number, result: StormAlarmResult) => void;
}): Promise<void> {
  const { templates, from, onAlarm } = opts;
  const gap = opts.gapMs ?? STORM_GAP_MS;
  const timeout = opts.timeoutMs ?? STORM_ALARM_TIMEOUT_MS;
  const total = templates.length;
  for (let i = from; i < total; i++) {
    const ev = templates[i];
    const res = await call<any>("/api/v1/events", { method: "POST", body: JSON.stringify(ev) }, timeout);
    const inc = res?.incident ?? null;
    const { outcome, into } = await classify(ev, inc, timeout);
    onAlarm(i + 1, total, { site: String(ev?.site_id ?? inc?.site_id ?? "an alarm"), outcome, incident: inc, into });
    if (i < total - 1) await sleep(gap);
  }
}

/** A storm failure as one clause: the server's words, or what an unreachable API looks like. */
export function stormFailureText(detail: string): string {
  if (/failed to fetch|networkerror|load failed/i.test(detail)) return "The API is unreachable.";
  return detail;
}

/** "5 tickets opened, 6 alarms folded into open tickets" ("" before the first alarm). */
export function stormCounts(s: StormState): string {
  if (s.done === 0 && s.phase !== "done") return "";
  const n = (k: number, one: string, many: string) => `${k} ${k === 1 ? one : many}`;
  return `${n(s.opened, "ticket", "tickets")} opened, ${n(s.folded, "alarm", "alarms")} folded into open tickets`;
}

/** The storm in one line, for a page that is not Mission control ("" while idle or stopped). */
export function stormLine(s: StormState): string {
  if (s.phase === "done") return `Storm complete: ${s.total} alarms, ${stormCounts(s)}.`;
  if (isStorming(s)) return s.text || "Starting the heavy-rain storm";
  return "";
}

/** Why the storm stopped and where, in one line ("" unless it stopped). */
export function stormStopLine(s: StormState): string {
  return s.phase === "failed" ? `Stopped after alarm ${s.done} of ${s.total || "?"}: ${s.error}` : "";
}
