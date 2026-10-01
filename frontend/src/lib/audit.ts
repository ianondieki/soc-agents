import { agentDisplayName } from "./agents";

/**
 * The audit trail as a person reads it. Rows are written by agents (`IngestCorrelationAgent`,
 * `agent:SupervisorAgent`), by unattended jobs (`outbox.dispatcher`) and by people (the
 * principal's name). Actions are dotted tokens (`step.succeeded`, `scorecard.published`).
 * Nothing here changes what is stored; the raw strings stay in the row's title attribute.
 */

export type AuditEntry = {
  id: string;
  ts: string;
  actor: string;
  action: string;
  entity_type: string;
  entity_id: string;
  rationale: string;
};

export type ActorKind = "agent" | "job" | "person";

const JOB_ACTORS = new Set(["outbox.dispatcher", "housekeeping", "scheduler", "system"]);

export function actorKind(actor: string | null | undefined): ActorKind {
  const a = String(actor ?? "");
  if (/Agent$/.test(a) || a.startsWith("agent:")) return "agent";
  if (JOB_ACTORS.has(a) || a.includes(".") || a.startsWith("job:")) return "job";
  return "person";
}

/** "IngestCorrelationAgent" → "Ingest Correlation"; "agent:SupervisorAgent" → "Supervisor";
 *  "outbox.dispatcher" → "Outbox dispatcher"; a person's name is left as written. */
export function actorLabel(actor: string | null | undefined): string {
  const a = String(actor ?? "").trim();
  if (!a) return "—";
  const kind = actorKind(a);
  if (kind === "agent") return agentDisplayName(a.replace(/^agent:/, ""));
  if (kind === "job") {
    const words = a.replace(/^job:/, "").replace(/[._]/g, " ");
    return words[0].toUpperCase() + words.slice(1);
  }
  return a;
}

const ACTION_WORDS: Record<string, string> = {
  "step.succeeded": "step done",
  "step.waiting_hitl": "waiting for a person",
  "step.failed": "step failed",
  "step.skipped": "step skipped",
  "step.started": "step started",
  "hitl.escalated": "escalated",
  "hitl.escalation.nudged": "nudge sent",
  "hitl.escalation.wallboard_red": "red on the wallboard",
  "hitl.escalation.nudge_failed": "nudge failed",
  "outbox.retried": "message retried",
  "llm.call": "model call",
  "redaction.miss": "redaction miss",
  "retention.purge": "retention purge",
  "upload.rejected": "upload rejected",
};

/** "scorecard.period_computed" → "scorecard period computed". */
export function actionLabel(action: string | null | undefined): string {
  const a = String(action ?? "").trim();
  if (!a) return "—";
  if (ACTION_WORDS[a]) return ACTION_WORDS[a];
  return a.replace(/[._]/g, " ");
}

/** Chip tone: green for a completed or released thing, lavender for a person's turn,
 *  red for a failure or a rejection, neutral for everything else. */
export function actionTone(action: string | null | undefined): string {
  const a = String(action ?? "").toLowerCase();
  if (/waiting_hitl|approval_requested|awaiting|requested$/.test(a)) return "hitl";
  if (/fail|reject|miss|cancel|purge|error/.test(a)) return "danger";
  if (/succeeded|published|released|sent|approved|finalised|completed|scheduled|created|opened/.test(a)) return "ok";
  return "";
}

const ENTITY_WORDS: Record<string, string> = {
  incident: "Ticket",
  hitl_task: "Approval",
  problem: "Problem",
  outbox: "Outbox message",
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
  const words = t.replace(/_/g, " ");
  return words ? words[0].toUpperCase() + words.slice(1) : "Record";
}

export type AuditGroup = { key: string; entity_type: string; entity_id: string; rows: AuditEntry[] };

/** Consecutive rows about the same record, newest first — one alarm's twelve steps read as
 *  one block instead of twelve unrelated lines. */
export function groupConsecutive(rows: AuditEntry[]): AuditGroup[] {
  const out: AuditGroup[] = [];
  for (const r of rows) {
    const last = out[out.length - 1];
    if (last && last.entity_type === r.entity_type && last.entity_id === r.entity_id) last.rows.push(r);
    else out.push({ key: `${r.entity_type}:${r.entity_id}:${r.id}`, entity_type: r.entity_type, entity_id: r.entity_id, rows: [r] });
  }
  return out;
}
