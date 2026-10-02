/**
 * Shared vocabulary for drawing agent runs: node order, status normalisation and the
 * choice of WHICH run represents an incident.
 *
 * The backend's `/incidents/{id}/workflow` deliberately shows the NEWEST lifecycle run
 * (pinned by `tests/system/test_contracts.py`). For a HUB major that has absorbed six
 * cascade children, the newest run is a two-step merge, so the ticket's own twelve-step
 * run — the one a supervisor wants to audit — was hidden behind "pending" nodes. The UI
 * therefore reads `/runs?incident_id=` and picks the run that opened the ticket
 * (`pickCreatingRun`), while still counting the merges it absorbed.
 */

/** The twelve lifecycle nodes in execution order. Mirrors `orchestrator/registry.py`
 *  NODE_CARDS; the live list from `/workflow` or `/agents` wins when present. */
export const LIFECYCLE_NODES: readonly { id: string; label: string; agent: string }[] = [
  { id: "INGEST", label: "Ingest", agent: "IngestCorrelationAgent" },
  { id: "CORRELATE", label: "Correlate", agent: "IngestCorrelationAgent" },
  { id: "ENRICH", label: "Enrich", agent: "EnrichmentAgent" },
  { id: "SEVERITY", label: "Severity", agent: "SeverityImpactAgent" },
  { id: "TICKET", label: "Ticket", agent: "TicketingAgent" },
  { id: "ASSIGN", label: "Assign", agent: "DispatchAssignmentAgent" },
  { id: "HITL", label: "HITL Gate", agent: "SupervisorAgent" },
  { id: "BROADCAST", label: "Broadcast", agent: "BroadcastCommsAgent" },
  { id: "EXEC_BRIEF", label: "Exec Brief", agent: "ExecutiveBriefingAgent" },
  { id: "LEDGER", label: "Shift Ledger", agent: "ShiftLedgerAgent" },
  { id: "RECURRENCE", label: "Recurrence", agent: "RecurrenceProblemAgent" },
  { id: "MONITOR", label: "Monitor", agent: "WorklogMonitorAgent" },
];

export const LIFECYCLE_NODE_IDS: ReadonlySet<string> = new Set(LIFECYCLE_NODES.map((n) => n.id));

/** Only the twelve lifecycle nodes belong on the rail: a scheduler job or an assist run
 *  records steps through the same tracker under its own node names. */
export function isLifecycleNode(id: unknown): boolean {
  return typeof id === "string" && LIFECYCLE_NODE_IDS.has(id);
}

/** Run statuses that mean the run has decided what it was going to decide. */
export const FINISHED_RUN_STATUSES: ReadonlySet<string> = new Set(["SUCCEEDED", "WAITING_HITL"]);

/** Chip class for a run status. CANCELLED (a rejected gate) and PENDING are neutral, never green. */
export function runChipClass(status: unknown): string {
  const s = String(status ?? "").toUpperCase();
  if (s === "RUNNING") return "chip accent";
  if (s === "WAITING_HITL") return "chip hitl";
  if (s === "SUCCEEDED") return "chip ok";
  if (s === "FAILED") return "chip danger";
  return "chip";
}

/** "incident_lifecycle" → "Incident lifecycle". */
export function humanGraph(name: unknown): string {
  const s = String(name ?? "").replace(/_/g, " ");
  return s ? s[0].toUpperCase() + s.slice(1) : "";
}

/** "L2_GUARDED" → "L2 guarded": the level token stays as written, the rest reads as words. */
export function humanAutonomy(level: unknown): string {
  const [lvl, ...rest] = String(level || "L2_GUARDED").split("_");
  return [lvl, ...rest.map((w) => w.toLowerCase())].join(" ");
}

/** "AWAITING_VENDOR" → "awaiting vendor": an enum as a person reads it. */
export function humanStatus(status: unknown): string {
  return String(status ?? "").toLowerCase().replace(/_/g, " ");
}

const ACRONYMS: Record<string, string> = {
  MPESA: "M-PESA", HUB: "HUB", CORE: "CORE", MSP: "MSP", RNIO: "RNIO", FE: "FE", TX: "TX", MW: "MW", SMS: "SMS",
  NOC: "NOC", SLA: "SLA", EAT: "EAT", CA: "CA", PIR: "PIR", TT: "TT", INC: "INC", OEM: "OEM", RF: "RF", IP: "IP",
  DWDM: "DWDM", GSM: "GSM", LTE: "LTE", VOICE: "voice", DATA: "data", HITL: "HITL",
};

/**
 * Any upper-case enum as a person reads it, keeping the acronyms the floor says as acronyms:
 * "MPESA_CORRIDOR" → "M-PESA corridor", "FIELD_ENGINEER" → "field engineer", "L2_GUARDED" → "L2 guarded",
 * "TX_MW" → "TX MW". Text that already contains lower case is returned unchanged.
 */
export function humanEnum(value: unknown): string {
  const raw = String(value ?? "").trim();
  if (!raw || /[a-z]/.test(raw)) return raw;
  return raw
    .split(/[_\s]+/)
    .map((w) => (ACRONYMS[w] ? ACRONYMS[w] : /^[A-Z]\d+$/.test(w) ? w : w.toLowerCase()))
    .join(" ");
}

/** The run status as a person reads it. */
export function runStatusWord(status: unknown): string {
  const s = String(status ?? "").toUpperCase();
  if (s === "WAITING_HITL") return "waiting for a human";
  return s ? s.toLowerCase() : "";
}

export type NodeStatus = "pending" | "running" | "succeeded" | "waiting_hitl" | "failed" | "skipped";

export interface RailStep {
  node_name: string;
  agent_name?: string;
  status?: string;
  duration_ms?: number | null;
  rationale?: string | null;
  output_summary?: string | null;
  input_summary?: string | null;
  tools_called?: { name?: string; ok?: boolean; latency_ms?: number; error?: string | null }[];
  confidence?: number | null;
  seq?: number;
}

/** Backend step/node status words → the rail's vocabulary. Unknown words read as pending. */
export function normaliseStatus(raw: unknown): NodeStatus {
  const s = String(raw ?? "").toLowerCase();
  if (s === "succeeded") return "succeeded";
  if (s === "waiting_hitl") return "waiting_hitl";
  if (s === "failed") return "failed";
  if (s === "started" || s === "running") return "running";
  if (s === "skipped") return "skipped";
  return "pending";
}

export const STATUS_WORD: Record<NodeStatus, string> = {
  pending: "pending",
  running: "running",
  succeeded: "done",
  waiting_hitl: "waiting for a human",
  failed: "failed",
  skipped: "skipped",
};

/** "IngestCorrelationAgent" → "Ingest Correlation": the agent name as a person reads it. */
export function agentDisplayName(name: string | undefined | null): string {
  if (!name) return "";
  return name.replace(/Agent$/, "").replace(/([a-z])([A-Z])/g, "$1 $2");
}

/** The step a node ran, from a list of steps (last one wins if a node ran twice). */
export function stepsByNode(steps: RailStep[] | undefined | null): Record<string, RailStep> {
  const out: Record<string, RailStep> = {};
  for (const s of steps || []) if (s && s.node_name) out[s.node_name] = s;
  return out;
}

/**
 * Among an incident's lifecycle runs (newest first, as `/runs` returns them), the one that
 * opened the ticket: it carries a TICKET step. Falls back to the newest run with the most
 * steps, then the newest run, then null.
 */
export function pickCreatingRun<T extends { steps?: RailStep[]; graph_name?: string }>(runs: T[] | undefined | null): T | null {
  const lifecycle = (runs || []).filter((r) => !r.graph_name || r.graph_name === "incident_lifecycle");
  if (lifecycle.length === 0) return null;
  const creating = lifecycle.find((r) => (r.steps || []).some((s) => s.node_name === "TICKET"));
  if (creating) return creating;
  return lifecycle.reduce((best, r) => ((r.steps?.length ?? 0) > (best.steps?.length ?? 0) ? r : best), lifecycle[0]);
}

/** Runs that FINISHED early because the alarm folded into an open ticket (merge or cascade).
 *  A run still executing has decided nothing yet and is not counted. */
export function countAbsorbed<T extends { steps?: RailStep[]; status?: string }>(runs: T[] | undefined | null): number {
  return (runs || []).filter(
    (r) => FINISHED_RUN_STATUSES.has(String(r.status || "")) && !(r.steps || []).some((s) => s.node_name === "TICKET")
  ).length;
}

/** Whole-run elapsed time from its step durations, when the run row has no finished_at yet. */
export function sumDurations(steps: RailStep[] | undefined | null): number {
  let total = 0;
  for (const s of steps || []) if (typeof s.duration_ms === "number") total += s.duration_ms;
  return total;
}

export function fmtMs(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms)) return "—";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(ms < 10000 ? 2 : 1)} s`;
}

export function fmtMinutes(min: number | null | undefined): string {
  if (min == null || !Number.isFinite(min)) return "—";
  if (min < 60) return `${Math.round(min)} min`;
  const h = min / 60;
  return `${h >= 10 ? Math.round(h) : h.toFixed(1)} h`;
}

export function fmtInt(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  return n.toLocaleString("en-KE");
}
