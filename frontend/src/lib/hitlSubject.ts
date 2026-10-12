import { humanEnum } from "./agents";
import { humanizeType, isPlainObject } from "./hitl";
import { eatWords } from "./time";

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
  // A handover is about the shift, not the ticket it is anchored to for scoping.
  if (text(t.task_type) === "APPROVE_HANDOVER") return handoverSubject(t);
  const incident = text(t.incident_number) || text(t.incident_id);
  if (incident) return incident;

  const payload: Record<string, unknown> = isPlainObject(t.proposed_payload) ? t.proposed_payload : {};
  const type = text(t.task_type);

  if (type === "APPROVE_MAINTENANCE_WINDOW" || isPlainObject(payload.window)) {
    const win: Record<string, unknown> = isPlainObject(payload.window) ? payload.window : {};
    // The scope is an enum ("SITE", "REGION"); the ref after it is an identifier and stays as written.
    const ref = text(win.scope_ref);
    const scope = text(win.scope);
    const starts = text(win.starts_at_eat);
    const where = ref ? (scope && scope !== "SITE" ? `${humanEnum(scope)} ${ref}` : ref) : humanEnum(scope);
    // The row and the card already name the type ("Maintenance window"): lead with where and when.
    return [where || "Maintenance window", starts && `from ${eat(starts)}`].filter(Boolean).join(", ");
  }

  if (type === "APPROVE_SCHEDULE" || isPlainObject(payload.task)) {
    const job: Record<string, unknown> = isPlainObject(payload.task) ? payload.task : {};
    const what = text(job.task_type) ? humanizeType(job.task_type) : "";
    const site = text(job.site_id);
    const due = text(job.due_at_eat);
    // Lead with the site and the work, so twelve schedule cards do not all start the same.
    const lead = [site, what && what.toLowerCase()].filter(Boolean).join(", ") || "Maintenance schedule";
    return [lead, due && `due ${eat(due)}`].filter(Boolean).join(", ");
  }

  // A burst of complaints about a place with no ticket (docs/CLOSE_THE_LOOP.md §3).
  if (type === "CONFIRM_POSSIBLE_OUTAGE") {
    const place = text(payload.place);
    return place ? `Possible outage in ${place}` : "Possible outage";
  }

  // Any other incident-less card (a scorecard dispute, a vendor notice): its type, plus the
  // first thing the payload names it as being about.
  const about = text(payload.site_id) || text(payload.vendor_id) || text(payload.scope);
  return joinParts(humanizeType(type), about);
}

/**
 * True when the heading is an incident number or id, an identifier the card sets in the
 * mono face; false for a maintenance or other incident-less card, whose heading is prose.
 */
export function hitlSubjectIsIncident(task: unknown): boolean {
  const t: Record<string, unknown> = isPlainObject(task) ? task : {};
  if (text(t.task_type) === "APPROVE_HANDOVER") return false;
  return Boolean(text(t.incident_number) || text(t.incident_id));
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

/** "Day shift handover, Mon 12 Oct" from the card's shift id ("safaricom:2026-10-12:DAY"). */
function handoverSubject(t: Record<string, unknown>): string {
  const payload: Record<string, unknown> = isPlainObject(t.proposed_payload) ? t.proposed_payload : {};
  const sid = text(payload.shift_id);
  const m = /:(\d{4})-(\d{2})-(\d{2}):(DAY|NIGHT)$/i.exec(sid);
  const kind = (m ? m[4] : text(payload.shift)).toUpperCase() === "NIGHT" ? "Night" : "Day";
  if (!m) return `${kind} shift handover`;
  const [y, mo, d] = [Number(m[1]), Number(m[2]), Number(m[3])];
  const wd = WEEKDAYS[new Date(Date.UTC(y, mo - 1, d)).getUTCDay()];
  return `${kind} shift handover, ${wd} ${d} ${MONTHS[mo - 1] ?? ""}`.trim();
}

function eat(when: string): string {
  return eatWords(when, " ");
}

function text(v: unknown): string {
  if (typeof v === "string") return v.trim();
  if (typeof v === "number" && Number.isFinite(v)) return String(v);
  return "";
}

/** `Maintenance window — site SFC-CST-HUB-MSA, 2026-09-22 01:00 EAT` (no middle-dot strings). */
function joinParts(head: string, ...rest: string[]): string {
  const [first, ...more] = rest.filter(Boolean);
  if (!first) return head;
  return [`${head} — ${first}`, ...more].join(", ");
}
