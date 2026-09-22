import { humanizeType, isPlainObject } from "./hitl";

/**
 * What a HITL card is ABOUT, as one heading line. Shared by the approval card and the
 * Mission Control inbox list so the two cannot drift apart.
 *
 * Until schema v8 every card was about an incident, and the heading was its number. Since
 * v8 the maintenance lane raises cards with no incident at all: APPROVE_SCHEDULE signs off a
 * programme of work, APPROVE_MAINTENANCE_WINDOW signs off taking a site or a region off air
 * on a given night. Headlining those "unknown incident" told the supervisor about to
 * authorise a planned outage that the system did not know what they were approving — when
 * the payload says exactly (services/maintenance.py: request_window_approval puts the
 * `window` there, request_schedule_approval the maintenance `task`).
 *
 * An incident card is unchanged: its number, else its id. Never throws and never returns an
 * empty string — a card with nothing recognisable still gets its task type, humanised.
 */
export function hitlSubject(task: unknown): string {
  const t: Record<string, unknown> = isPlainObject(task) ? task : {};
  const incident = text(t.incident_number) || text(t.incident_id);
  if (incident) return incident;

  const payload: Record<string, unknown> = isPlainObject(t.proposed_payload) ? t.proposed_payload : {};
  const type = text(t.task_type);

  if (type === "APPROVE_MAINTENANCE_WINDOW" || isPlainObject(payload.window)) {
    const win: Record<string, unknown> = isPlainObject(payload.window) ? payload.window : {};
    const scope = [text(win.scope), text(win.scope_ref)].filter(Boolean).join(" ");
    const starts = text(win.starts_at_eat);
    return joinParts("Maintenance window", scope, eat(starts));
  }

  if (type === "APPROVE_SCHEDULE" || isPlainObject(payload.task)) {
    const job: Record<string, unknown> = isPlainObject(payload.task) ? payload.task : {};
    const what = text(job.task_type) ? humanizeType(job.task_type) : "";
    const site = text(job.site_id);
    const due = text(job.due_at_eat);
    return joinParts(
      "Maintenance schedule",
      [what, site && `at ${site}`].filter(Boolean).join(" "),
      due && `due ${eat(due)}`,
    );
  }

  // Any other incident-less card (a scorecard dispute, a vendor notice): its type, plus the
  // first thing the payload names it as being about.
  const about = text(payload.site_id) || text(payload.vendor_id) || text(payload.scope);
  return joinParts(humanizeType(type), about);
}

/**
 * The zone suffix, exactly once. The backend's `*_at_eat` fields are services/clock.fmt_eat
 * output, which already ends in " EAT" ("2026-11-12 00:00 EAT"); appending another printed
 * "… 00:00 EAT EAT" on every maintenance card. A bare time (an older payload, a hand-written
 * one) still gets the suffix, so the heading never shows a wall-clock time with no zone.
 */
function eat(when: string): string {
  if (!when) return "";
  return /\bEAT$/.test(when) ? when : `${when} EAT`;
}

function text(v: unknown): string {
  if (typeof v === "string") return v.trim();
  if (typeof v === "number" && Number.isFinite(v)) return String(v);
  return "";
}

/** `Maintenance window — SITE SFC-CST-HUB-MSA · 2026-09-22 01:00 EAT` */
function joinParts(head: string, ...rest: string[]): string {
  const [first, ...more] = rest.filter(Boolean);
  if (!first) return head;
  return [`${head} — ${first}`, ...more].join(" · ");
}
