import { humanEnum, humanStatus } from "./agents";
import { fmtHM, parseInstant } from "./time";
import { isOpenTicket } from "./ticket";

/**
 * Plain-text updates for the floor's WhatsApp groups, where the NOC tells regional offices, field
 * engineers and management what is happening. Short lines, one fact each, `*bold*` only on the
 * ticket's headline (WhatsApp's own markup), times in EAT. Nothing here sends anything: the page
 * puts the text on the clipboard and a person pastes it.
 */

/** "45 min", "1 h 12 min", "3 h". */
export function lastedWords(ms: number): string {
  const min = Math.max(0, Math.round(ms / 60_000));
  if (min < 60) return `${min} min`;
  const h = Math.floor(min / 60);
  const rest = min % 60;
  return rest ? `${h} h ${rest} min` : `${h} h`;
}

const SERVICE_WORD: Record<string, string> = { VOICE: "calls", DATA: "mobile data", SMS: "SMS" };

function servicesOf(inc: any): string[] {
  const raw = Array.isArray(inc?.services_impacted) ? inc.services_impacted : [];
  const out: string[] = [];
  for (const s of raw) {
    if (typeof s !== "string") continue;
    if (/^MPESA/i.test(s)) {
      if (!inc?.mpesa_risk && !out.includes("M-PESA")) out.push("M-PESA");
      continue;
    }
    const w = SERVICE_WORD[s.toUpperCase()] ?? humanEnum(s).toLowerCase();
    if (w && !out.includes(w)) out.push(w);
  }
  return out;
}

function ownerOf(inc: any): string {
  const name = inc?.assignee_name || inc?.msp_name || inc?.responsible_msp;
  return name ? String(name) : "";
}

export interface TicketUpdateInput {
  inc: any;
  nowMs: number;
  /** "Rift Valley", from the operator profile. */
  regionLabel?: string;
  /** The newest note a person wrote on the ticket. */
  latestNote?: { at: unknown; by: string; text: string } | null;
}

/** One ticket, as a message for the group. */
export function ticketUpdate({ inc, nowMs, regionLabel, latestNote }: TicketUpdateInput): string {
  const lines: string[] = [];
  const name = inc?.site_name || inc?.site_id || "Ticket";
  lines.push(`*${inc?.priority ? `${inc.priority} ` : ""}${name}* (${inc?.incident_number ?? ""})`);

  const where = [regionLabel, inc?.county].filter((x) => x && String(x).trim()).join(", ");
  const status = humanStatus(inc?.status);
  lines.push([where, status ? status[0].toUpperCase() + status.slice(1) : ""].filter(Boolean).join(". ") + ".");

  const start = parseInstant(inc?.outage_start_at || inc?.created_at)?.getTime();
  const restored = parseInstant(inc?.restored_at)?.getTime();
  if (start != null && restored != null) {
    lines.push(`Restored at ${fmtHM(inc.restored_at)} EAT after ${lastedWords(restored - start)}.`);
  } else if (start != null) {
    lines.push(`Down since ${fmtHM(inc.outage_start_at || inc.created_at)} EAT (${lastedWords(nowMs - start)}).`);
  }

  const impact: string[] = [];
  const users = Number(inc?.users_affected);
  if (Number.isFinite(users) && users > 0) impact.push(`about ${users.toLocaleString("en-KE")} subscribers`);
  const children = Number(inc?.child_sites_down);
  if (Number.isFinite(children) && children > 0) impact.push(`${children} child ${children === 1 ? "site" : "sites"} down`);
  if (inc?.mpesa_risk) impact.push("M-PESA at risk");
  if (impact.length) lines.push(`Impact: ${impact.join("; ")}.`);

  const services = servicesOf(inc);
  if (services.length) lines.push(`Services: ${services.join(", ")}.`);

  const owner = ownerOf(inc);
  const fe = inc?.fe_name ? `field engineer ${inc.fe_name}` : "";
  if (owner || fe) lines.push(`Owner: ${[owner, fe].filter(Boolean).join(", ")}.`);

  const due = parseInstant(inc?.sla_restore_due)?.getTime();
  if (due != null && restored == null && isOpenTicket(inc?.status)) {
    lines.push(
      due >= nowMs
        ? `Restore due ${fmtHM(inc.sla_restore_due)} EAT (in ${lastedWords(due - nowMs)}).`
        : `Restore deadline ${fmtHM(inc.sla_restore_due)} EAT passed ${lastedWords(nowMs - due)} ago.`
    );
  }
  if (inc?.msp_percent_complete != null && restored == null) lines.push(`Vendor says ${inc.msp_percent_complete}% done.`);

  if (latestNote && latestNote.text.trim()) {
    const text = latestNote.text.trim().replace(/\s+/g, " ");
    lines.push(`Latest note (${fmtHM(latestNote.at)}, ${latestNote.by}): ${text.length > 280 ? `${text.slice(0, 279)}…` : text}`);
  }
  if (inc?.requires_hitl && String(inc?.hitl_state || "").toUpperCase() === "PENDING") {
    lines.push("A broadcast is waiting for a decision.");
  }
  return lines.join("\n");
}

export interface ShiftSummaryInput {
  /** "Day shift, Tue 13 Oct". */
  shiftName: string;
  nowMs: number;
  incidents: any[];
  /** Cards waiting for a decision. */
  waiting: number;
  /** Shift start (ms): restorations since then count as this shift's. */
  sinceMs: number;
  regionLabel: (code: string) => string;
}

/** The shift so far, for the group: counts first, then every open ticket on its own line. */
export function shiftSummary({ shiftName, nowMs, incidents, waiting, sinceMs, regionLabel }: ShiftSummaryInput): string {
  const open = incidents.filter((i) => i && isOpenTicket(i.status));
  const rank = (p: unknown) => ["P1", "P2", "P3", "P4"].indexOf(String(p)) + 1 || 9;
  open.sort((a, b) => rank(a.priority) - rank(b.priority) || String(a.created_at).localeCompare(String(b.created_at)));
  const count = (p: string) => open.filter((i) => i.priority === p).length;
  const restoredHere = incidents.filter((i) => {
    const t = parseInstant(i?.restored_at)?.getTime();
    return t != null && t >= sinceMs && t <= nowMs;
  }).length;
  const late = open.filter((i) => {
    const due = parseInstant(i.sla_restore_due)?.getTime();
    return due != null && due < nowMs;
  }).length;
  const mpesa = open.filter((i) => i.mpesa_risk).length;
  const n = (k: number, one: string, many: string) => `${k} ${k === 1 ? one : many}`;

  const lines = [`*${shiftName}* at ${fmtHM(new Date(nowMs).toISOString())} EAT`];
  if (open.length === 0) {
    lines.push("No open tickets.");
  } else {
    const byP = ["P1", "P2", "P3", "P4"].map((p) => (count(p) ? `${count(p)} ${p}` : "")).filter(Boolean).join(", ");
    lines.push(`Open: ${n(open.length, "ticket", "tickets")} (${byP}).${mpesa ? ` M-PESA at risk on ${mpesa}.` : ""}`);
  }
  lines.push(`Waiting for a decision: ${n(waiting, "card", "cards")}.`);
  if (late) lines.push(`Past the restore deadline: ${n(late, "ticket", "tickets")}.`);
  lines.push(`Restored this shift: ${restoredHere}.`);
  if (open.length) {
    lines.push("");
    for (const i of open.slice(0, 12)) {
      const since = fmtHM(i.outage_start_at || i.created_at);
      lines.push(`- ${i.priority} ${i.site_name || i.site_id} (${i.incident_number}), ${regionLabel(i.region_code)}, ${humanStatus(i.status)} since ${since}`);
    }
    if (open.length > 12) lines.push(`- and ${open.length - 12} more on the Incident board`);
  }
  return lines.join("\n");
}
