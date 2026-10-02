import { agentDisplayName, humanEnum, isLifecycleNode, LIFECYCLE_NODES } from "./agents";

/**
 * The audit trail as a person reads it. Rows are written by agents (`IngestCorrelationAgent`,
 * `agent:SupervisorAgent`), by unattended jobs (`outbox.dispatcher`) and by people (the
 * principal's name). Actions are dotted tokens (`step.succeeded`, `scorecard.published`).
 *
 * Nothing here changes what is stored: every function returns a rendering, and the page keeps
 * the raw actor, action, time and rationale one click away ("Recorded as").
 * Everything in this file is a pure function, so it can be unit-tested without a browser.
 */

export type AuditEntry = {
  id: string;
  ts: string;
  actor: string;
  action: string;
  entity_type: string;
  entity_id: string;
  rationale: string;
  /** Lifted from the payload by the API; null on rows written before step payloads were JSON. */
  run_id?: string | null;
  node?: string | null;
  /** The ticket an approval card's rows name in their payload, when they name one. */
  incident_id?: string | null;
};

export type ActorKind = "agent" | "job" | "person";

const JOB_ACTORS = new Set(["outbox.dispatcher", "housekeeping", "scheduler", "system"]);

export function actorKind(actor: string | null | undefined): ActorKind {
  const a = String(actor ?? "");
  if (/Agent$/.test(a) || a.startsWith("agent:")) return "agent";
  if (JOB_ACTORS.has(a) || a.includes(".") || a.startsWith("job:")) return "job";
  return "person";
}

function capFirst(s: string): string {
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}

/** Unattended jobs whose stored name is plumbing: the job that sends released messages is the
 *  message sender on screen. The stored actor is unchanged ("Recorded as" shows it). */
const JOB_WORDS: Record<string, string> = { "outbox.dispatcher": "Message sender" };

/** "IngestCorrelationAgent" → "Ingest Correlation"; "agent:SupervisorAgent" → "Approval gate";
 *  "outbox.dispatcher" → "Message sender"; a person's name is left as written. */
export function actorLabel(actor: string | null | undefined): string {
  const a = String(actor ?? "").trim();
  if (!a) return "Unknown";
  const kind = actorKind(a);
  if (kind === "agent") return agentDisplayName(a.replace(/^agent:/, ""));
  if (kind === "job") return JOB_WORDS[a] || capFirst(a.replace(/^job:/, "").replace(/[._]/g, " "));
  return a;
}

const ACTION_WORDS: Record<string, string> = {
  "step.succeeded": "Step done",
  "step.waiting_hitl": "Waiting for a decision",
  "step.failed": "Step failed",
  "step.skipped": "Step skipped",
  "step.started": "Step started",
  "hitl.escalated": "Escalated",
  "hitl.escalation.nudged": "Nudge sent",
  "hitl.escalation.wallboard_red": "Red on the wallboard",
  "hitl.escalation.nudge_failed": "Nudge failed",
  "hitl.nudge.sent": "Nudge delivered",
  "hitl.nudge.inert": "Nudge reached nobody",
  "hitl.nudge.failed": "Nudge failed",
  "outbox.retried": "Message retried",
  "llm.call": "Model call",
  "redaction.miss": "Redaction miss",
  "retention.purge": "Retention purge",
  "upload.rejected": "Upload rejected",
};

/** "scorecard.period_computed" → "Scorecard period computed". */
export function actionLabel(action: string | null | undefined): string {
  const a = String(action ?? "").trim();
  if (!a) return "Entry";
  if (ACTION_WORDS[a]) return ACTION_WORDS[a];
  return capFirst(a.replace(/[._]/g, " "));
}

/** "HITL Gate" → "HITL gate": sentence case that keeps the acronyms. */
function sentenceCase(label: string): string {
  return label
    .split(" ")
    .map((w, i) => (i === 0 || /^[A-Z0-9‑-]{2,}$/.test(w) ? w : w.toLowerCase()))
    .join(" ");
}

const STEP_NAMES: Record<string, string> = Object.fromEntries(
  LIFECYCLE_NODES.map((n) => [n.id, sentenceCase(n.label)])
);

/** The step a row records, from the payload's node: "EXEC_BRIEF" → "Exec brief",
 *  "scorecard_close" → "Scorecard close". Empty when the row has no node. */
export function stepName(node: string | null | undefined): string {
  const n = String(node ?? "").trim();
  if (!n) return "";
  if (STEP_NAMES[n]) return STEP_NAMES[n];
  const human = /[a-z]/.test(n) ? n.replace(/[._:]/g, " ") : humanEnum(n);
  return capFirst(human.trim());
}

/** What the row did, in two to four words: the step name, else the action. */
export function rowTitle(r: Pick<AuditEntry, "node" | "action">): string {
  return stepName(r.node) || actionLabel(r.action);
}

// --------------------------------------------------------------------------- outcome

export type OutcomeTone = "hitl" | "danger" | "warn" | "ok";
export type Outcome = { word: string; tone: OutcomeTone; icon: "pause" | "alert" | "dot" | "check" };

/**
 * The exception a row records, or null for a routine success. A routine step gets no word at
 * all: the outcome column only speaks when something needs a person's eye.
 */
export function outcomeOf(action: string | null | undefined): Outcome | null {
  const a = String(action ?? "").toLowerCase();
  if (/waiting_hitl|approval_requested|awaiting/.test(a)) return { word: "Waiting for a decision", tone: "hitl", icon: "pause" };
  if (/cancel/.test(a)) return { word: "Cancelled", tone: "warn", icon: "alert" };
  if (/reject/.test(a)) return { word: "Rejected", tone: "danger", icon: "alert" };
  if (/fail|error|\.miss$/.test(a)) return { word: "Failed", tone: "danger", icon: "alert" };
  if (/escalat|wallboard_red/.test(a)) return { word: "Escalated", tone: "warn", icon: "alert" };
  if (/skipped/.test(a)) return { word: "Skipped", tone: "warn", icon: "dot" };
  if (/\.approved$|^approved$|approve$/.test(a)) return { word: "Approved", tone: "ok", icon: "check" };
  return null;
}

/** Routine = an automated success with nothing for a person to look at. A person's action is
 *  a decision and never routine; an exception never is. */
export function isRoutine(r: Pick<AuditEntry, "actor" | "action">): boolean {
  return actorKind(r.actor) !== "person" && outcomeOf(r.action) === null;
}

// --------------------------------------------------------------------------- rationale

export type Fact = { key: string; label: string; value: string };
export type ParsedRationale = { sentence: string; facts: Fact[] };

const FACT = /^([\w ]{1,24})=(.+)$/;
const DROPPED_KEYS = new Set(["operator"]); // the tenant: the same on every row this operator sees
const KEY_WORDS: Record<string, string> = {
  final: "Final priority",
  users: "Subscribers",
  users_est: "Subscribers (est.)",
  users_affected: "Subscribers",
  // The assignment's keys, in the floor's words: the owner is whoever holds the ticket.
  primary: "Owner",
  pool: "Owner pool",
  "FE support": "Field engineer",
  fe_support: "Field engineer",
  RNIO: "Regional office",
  rnio: "Regional office",
};

/** "radio_oem" → "Radio OEM", "mpesa_risk" → "M‑PESA risk", "final" → "Final priority". */
export function humanKey(key: string): string {
  const k = key.trim();
  if (KEY_WORDS[k]) return KEY_WORDS[k];
  return capFirst(humanEnum(k.toUpperCase().replace(/\s+/g, "_")));
}

/** A token reads as a word (and may be humanised) when the floor says it as one: a known
 *  acronym, a level such as P2, or a real word of four letters or more. Codes such as
 *  "NBI_E" or "MW_HOP_DOWN" stay exactly as written. */
function wordLike(part: string): boolean {
  return /^[A-Z]\d+$/.test(part) || /^[A-Z]{4,}$/.test(part) || humanEnum(part) !== part.toLowerCase();
}

/** "['EGYPRO_FIBRE', 'FIELD_ENGINEER']" → "egypro fibre, field engineer"; "MIXED" → "mixed";
 *  anything with lower case, digits mid-code or punctuation is returned unchanged. */
export function humanValue(value: string): string {
  const v = value.trim();
  const list = /^\[(.*)\]$/.exec(v);
  if (list) {
    return list[1]
      .split(/,\s*/)
      .map((item) => item.trim().replace(/^['"]|['"]$/g, ""))
      .filter(Boolean)
      .map(humanValue)
      .join(", ");
  }
  if (/^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*$/.test(v) && v.split("_").every(wordLike)) return humanEnum(v);
  return v;
}

/**
 * Split a stored rationale into the sentence a person reads and the key=value facts the agent
 * recorded beside it. Segments are separated by "; "; a segment that is `key=value` with a short
 * word key is a fact, everything else stays in the sentence, verbatim and in order.
 */
export function parseRationale(text: string | null | undefined): ParsedRationale {
  const sentence: string[] = [];
  const facts: Fact[] = [];
  for (const raw of String(text ?? "").split("; ")) {
    const seg = raw.trim();
    if (!seg) continue;
    const m = FACT.exec(seg);
    if (m && m[1].trim()) {
      const key = m[1].trim();
      if (DROPPED_KEYS.has(key)) continue;
      facts.push({ key, label: humanKey(key), value: humanValue(m[2]) });
    } else {
      sentence.push(seg);
    }
  }
  return { sentence: sentence.join("; "), facts };
}

// --------------------------------------------------------------------------- records

const ENTITY_WORDS: Record<string, string> = {
  incident: "Ticket",
  hitl_task: "Approval",
  problem: "Problem",
  outbox: "Message",
  housekeeping: "Housekeeping run",
  handover: "Handover",
  post_incident_review: "Post-incident review",
  vendor_scorecard: "Scorecard",
  vendor_scorecard_period: "Scorecard period",
  vendor_scorecard_line: "Scorecard line",
  maintenance_window: "Maintenance window",
  maintenance_task: "Maintenance task",
  maintenance_plan: "Maintenance plan",
  message_template: "Message template",
  clock_event: "Clock event",
  capacity_observation: "Capacity observation",
  memory: "Memory",
  vendor: "Vendor",
};

export function entityLabel(entityType: string | null | undefined): string {
  const t = String(entityType ?? "");
  if (ENTITY_WORDS[t]) return ENTITY_WORDS[t];
  return capFirst(t.replace(/_/g, " ")) || "Record";
}

/** A generated id says nothing to a reader; a code (a vendor, a date, a site) does. */
export function isOpaqueId(id: string | null | undefined): boolean {
  const s = String(id ?? "");
  return /^[0-9a-f]{8}-[0-9a-f]{4}-/i.test(s) || /^[0-9a-f]{24,}$/i.test(s);
}

// --------------------------------------------------------------------------- blocks

export type BlockKind = "ticket" | "folded" | "alarm" | "approval" | "job" | "record";

export type AuditBlock = {
  key: string;
  kind: BlockKind;
  /** Newest first, as the API returns them. */
  rows: AuditEntry[];
  /** The incident the block is about, when one is known (ticket, or a linked approval). */
  ticketId: string | null;
  entityType: string;
  entityId: string;
  /** Folded runs: the parent HUB site the rationale names, e.g. "SFC-NBIE-HUB-EMB". */
  parentSite: string | null;
  /** Job runs: the step name, e.g. "Scorecard close". */
  jobName: string | null;
  /** How many runs the block holds: consecutive alarms folded under the same parent share one. */
  runs: number;
};

const PARENT_SITE = /parent HUB ([A-Z0-9][A-Z0-9-]{2,40})/;

/**
 * One block key per row. A lifecycle run is one block (its intake steps were written before
 * the ticket existed and carry no entity id; the run id keeps them with their ticket). Rows
 * written before the API returned a run id are walked the same way: a run ends at its INGEST
 * step, so the older rows below an INGEST start the next run. Consecutive runs of the same
 * scheduled job share a block. Everything else is keyed by the record it is about.
 */
export function blockKeys(rows: AuditEntry[]): string[] {
  const keys: string[] = [];
  let legacy = 0;
  let prev: AuditEntry | null = null;
  let prevLegacy = false;
  for (const r of rows) {
    const lifecycle = isLifecycleNode(r.node);
    const isLegacy = !r.run_id && lifecycle && r.entity_type === "incident";
    let key: string;
    if (r.run_id && (lifecycle || r.entity_id)) key = `run:${r.run_id}`;
    else if (r.run_id && r.node) key = `job:${r.node}`;
    else if (isLegacy) {
      const continues =
        prevLegacy &&
        prev !== null &&
        prev.node !== "INGEST" &&
        !(prev.entity_id === "" && r.entity_id !== "") &&
        !(prev.entity_id && r.entity_id && prev.entity_id !== r.entity_id);
      if (!continues) legacy += 1;
      key = `legacy:${legacy}`;
    } else key = `rec:${r.entity_type}:${r.entity_id}`;
    keys.push(key);
    prev = r;
    prevLegacy = isLegacy;
  }
  return keys;
}

/**
 * Consecutive rows with the same block key, newest block first. Consecutive alarms folded
 * under the same parent HUB then share one block ("Folded into an open ticket", 2 alarms).
 * A block's React key ends with its OLDEST row id, which stays put while a live run adds
 * steps on top.
 */
export function groupBlocks(rows: AuditEntry[]): AuditBlock[] {
  const keys = blockKeys(rows);
  const grouped: { base: string; block: AuditBlock }[] = [];
  let lastKey = "";
  rows.forEach((r, i) => {
    const key = keys[i];
    const last = grouped[grouped.length - 1];
    if (last && key === lastKey) last.block.rows.push(r);
    else
      grouped.push({
        base: key.split(":")[0],
        block: { key, kind: "record", rows: [r], ticketId: null, entityType: r.entity_type, entityId: r.entity_id, parentSite: null, jobName: null, runs: 1 },
      });
    lastKey = key;
  });
  const out: AuditBlock[] = [];
  for (const { base, block } of grouped) {
    classify(block, base);
    const last = out[out.length - 1];
    if (last && block.kind === "folded" && last.kind === "folded" && block.parentSite && block.parentSite === last.parentSite) {
      last.rows.push(...block.rows);
      last.runs += 1;
    } else out.push(block);
  }
  for (const b of out) b.key = `${b.key}#${b.rows[b.rows.length - 1].id}`;
  return out;
}

function classify(b: AuditBlock, base: string): void {
  const ticketRow = b.rows.find((r) => r.entity_type === "incident" && r.entity_id);
  const linked = b.rows.find((r) => r.incident_id)?.incident_id ?? null;
  if (base === "run" || base === "legacy") {
    if (ticketRow) {
      b.kind = "ticket";
      b.ticketId = ticketRow.entity_id;
      b.entityType = "incident";
      b.entityId = ticketRow.entity_id;
    } else if (b.rows.some((r) => isLifecycleNode(r.node))) {
      // "Folded" only when a step names the parent it merged under; an alarm run that ended
      // with no ticket for another reason is an alarm run, nothing more.
      b.kind = "alarm";
      for (const r of b.rows) {
        const m = PARENT_SITE.exec(r.rationale || "");
        if (m) {
          b.kind = "folded";
          b.parentSite = m[1];
          break;
        }
      }
    } else {
      b.kind = "job";
      b.jobName = stepName(b.rows[b.rows.length - 1].node) || null;
    }
    return;
  }
  if (base === "job") {
    b.kind = "job";
    b.jobName = stepName(b.rows[0].node) || null;
    return;
  }
  if (ticketRow) {
    b.kind = "ticket";
    b.ticketId = ticketRow.entity_id;
    return;
  }
  b.kind = b.entityType === "hitl_task" ? "approval" : "record";
  b.ticketId = linked;
}

// --------------------------------------------------------------------------- display rows

/** One display row: identical consecutive entries (same actor, action, step and reason) merged. */
export type DisplayRow = { id: string; entry: AuditEntry; count: number; oldestTs: string };

export function mergeIdentical(rows: AuditEntry[]): DisplayRow[] {
  const out: DisplayRow[] = [];
  for (const r of rows) {
    const last = out[out.length - 1];
    if (
      last &&
      last.entry.actor === r.actor &&
      last.entry.action === r.action &&
      (last.entry.node ?? "") === (r.node ?? "") &&
      (last.entry.rationale ?? "") === (r.rationale ?? "")
    ) {
      last.count += 1;
      last.oldestTs = r.ts;
    } else out.push({ id: r.id, entry: r, count: 1, oldestTs: r.ts });
  }
  return out;
}

/** "Ingest, Correlate, Enrich…": the routine steps of a block, oldest first, unique, three named. */
export function routineNames(rows: AuditEntry[], max = 3): string {
  const names: string[] = [];
  for (const r of [...rows].reverse()) {
    const n = rowTitle(r);
    if (!names.includes(n)) names.push(n);
  }
  return names.slice(0, max).join(", ") + (names.length > max ? "…" : "");
}

/** The words a search matches for one row: raw and rendered, so either spelling finds it. */
export function rowHaystack(r: AuditEntry): string {
  return [r.actor, actorLabel(r.actor), r.action, actionLabel(r.action), r.rationale, r.node, stepName(r.node), r.entity_type, r.entity_id, r.incident_id]
    .join(" ")
    .toLowerCase();
}

// --------------------------------------------------------------------------- search all entries

/** Tickets a search names, resolved to their ids, at most this many (the API takes 25 terms). */
export const SEARCH_ID_CAP = 20;

type TicketLite = { id: string; incident_number?: string | null; site_id?: string | null; site_name?: string | null };

/**
 * The terms "Search all entries" sends as `q`: what was typed, plus the id of every ticket whose
 * number, site code or site name contains it. A step row stores the ticket's id, never its
 * number, so "INC000027" alone would find only the rows whose reason happens to quote it. When
 * the text names more tickets than the cap, only the text is sent: a partial list of ids would
 * be a partial answer that looks complete.
 */
export function searchTerms(typed: string, tickets: Iterable<TicketLite>): string[] {
  const text = typed.trim();
  if (!text) return [];
  const needle = text.toLowerCase();
  const ids: string[] = [];
  for (const t of tickets) {
    if (!t || typeof t.id !== "string" || !t.id) continue;
    const hit = [t.incident_number, t.site_id, t.site_name].some((f) => typeof f === "string" && f.toLowerCase().includes(needle));
    if (hit && !ids.includes(t.id)) ids.push(t.id);
  }
  return ids.length <= SEARCH_ID_CAP ? [text, ...ids] : [text];
}

/** `/api/v1/audit?limit=1000&q=INC000027&q=0b22…`: each term its own `q`, encoded. */
export function auditUrl(limit: number, terms: readonly string[] = []): string {
  const qs = new URLSearchParams({ limit: String(limit) });
  for (const t of terms) if (t.trim()) qs.append("q", t.trim());
  return `/api/v1/audit?${qs}`;
}
