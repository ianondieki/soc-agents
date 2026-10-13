/**
 * When this person last had the console in front of them, so Mission control can say what
 * changed while they were away (components/WhileAway.tsx). A moment is written every minute while
 * the page is visible and when it is hidden; coming back after `AWAY_MS` or more (a new tab the
 * next morning, or this one after a long break) opens a catch-up from that moment. Per browser,
 * in localStorage; where storage is blocked there is simply no catch-up.
 */

const KEY = "noc_last_seen_v1";
/** Shorter than this is a glance away, not an absence. */
export const AWAY_MS = 15 * 60_000;
/** A catch-up never reaches further back than this. */
export const AWAY_MAX_MS = 24 * 3_600_000;

function read(): number | null {
  try {
    const v = Number(window.localStorage.getItem(KEY));
    return Number.isFinite(v) && v > 0 ? v : null;
  } catch {
    return null;
  }
}

function write(ms: number): void {
  try {
    window.localStorage.setItem(KEY, String(ms));
  } catch {
    /* no storage, no catch-up */
  }
}

type Listener = (since: number | null) => void;
let pending: number | null = null;
const listeners = new Set<Listener>();
let started = false;

function returned(last: number | null, now: number): void {
  if (last == null || now - last < AWAY_MS) return;
  pending = Math.max(last, now - AWAY_MAX_MS);
  listeners.forEach((fn) => fn(pending));
}

/** Start keeping the moment (App calls it once). Idempotent. */
export function trackPresence(): void {
  if (started) return;
  started = true;
  const now = Date.now();
  returned(read(), now);
  write(now);
  window.setInterval(() => {
    if (document.visibilityState === "visible") write(Date.now());
  }, 60_000);
  document.addEventListener("visibilitychange", () => {
    const t = Date.now();
    if (document.visibilityState === "hidden") write(t);
    else {
      returned(read(), t);
      write(t);
    }
  });
}

/** The moment a catch-up runs from, or null; `onChange` hears a new one. */
export function awaySince(onChange?: Listener): { since: number | null; stop: () => void } {
  if (onChange) listeners.add(onChange);
  return { since: pending, stop: () => onChange && listeners.delete(onChange) };
}

/** The person has read the catch-up. */
export function dismissAway(): void {
  pending = null;
  write(Date.now());
  listeners.forEach((fn) => fn(null));
}

export interface AwayFacts {
  opened: any[];
  restored: number;
  closed: number;
  approved: number;
  rejected: number;
  /** Who made those decisions, in order of first decision. */
  deciders: string[];
  /** Restore deadlines that ran out in the window on tickets not restored by then. */
  breached: any[];
}

const within = (v: unknown, from: number, to: number): boolean => {
  if (typeof v !== "string" || !v) return false;
  const t = Date.parse(/[zZ]|[+-]\d{2}:?\d{2}$/.test(v) ? v : `${v}Z`);
  return Number.isFinite(t) && t > from && t <= to;
};

/** What changed between `since` and `now`, from the ticket list and the audit trail. */
export function awayFacts(incidents: any[], audit: any[], since: number, now: number): AwayFacts {
  const opened = incidents.filter((i) => i && !i.parent_incident_id && within(i.created_at, since, now));
  const restored = incidents.filter((i) => within(i?.restored_at, since, now)).length;
  const closed = incidents.filter((i) => within(i?.closed_at, since, now)).length;
  const breached = incidents.filter((i) => {
    if (!within(i?.sla_restore_due, since, now)) return false;
    const due = Date.parse(i.sla_restore_due);
    const back = i.restored_at ? Date.parse(i.restored_at) : NaN;
    return !(Number.isFinite(back) && back <= due);
  });
  const decisions = audit
    .filter((r) => (r?.action === "hitl.approved" || r?.action === "hitl.rejected") && within(r.ts, since, now))
    .sort((a, b) => String(a.ts).localeCompare(String(b.ts)));
  const deciders: string[] = [];
  for (const r of decisions) if (r.actor && !deciders.includes(r.actor)) deciders.push(String(r.actor));
  return {
    opened,
    restored,
    closed,
    approved: decisions.filter((r) => r.action === "hitl.approved").length,
    rejected: decisions.filter((r) => r.action === "hitl.rejected").length,
    deciders,
    breached,
  };
}
