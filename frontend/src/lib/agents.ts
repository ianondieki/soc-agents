import { parseInstant } from "./time";

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

/** A lifecycle hop: its node id, the one label every page shows for it, the agent that runs
 *  it and what it does, in one sentence. */
export interface LifecycleNode {
  id: string;
  label: string;
  agent: string;
  does: string;
}

/** The twelve lifecycle nodes in execution order. Mirrors `orchestrator/registry.py`
 *  NODE_CARDS; the live list from `/workflow` or `/agents` wins for order and status, but the
 *  label always comes from here through `nodeLabel()` so a hop has one name on every page.
 *  `does` is the sentence the Workflow map, the Showcase steps and the Agents page print. */
export const LIFECYCLE_NODES: readonly LifecycleNode[] = [
  { id: "INGEST", label: "Ingest", agent: "IngestCorrelationAgent", does: "Normalise the alarm and fingerprint it (site, alarm code, domain)." },
  { id: "CORRELATE", label: "Correlate", agent: "IngestCorrelationAgent", does: "Fold a repeat or a child site into the open ticket or its parent HUB." },
  { id: "ENRICH", label: "Enrich", agent: "EnrichmentAgent", does: "Site catalogue: region, RNIO, FE on call, subscribers affected, TT classification." },
  { id: "SEVERITY", label: "Severity", agent: "SeverityImpactAgent", does: "P4 under 50k users; P3, P2, P1 above; HUB floor P2; CORE floor P1; M‑PESA corridor tag." },
  { id: "TICKET", label: "Ticket", agent: "TicketingAgent", does: "Allocate the INC number, fill the TT fields, write the narrative and set the SLA clocks." },
  { id: "ASSIGN", label: "Assign", agent: "DispatchAssignmentAgent", does: "Region × domain matrix: power to Egypro or Tetranet, fibre to Egypro Fibre, radio to the FE." },
  { id: "HITL", label: "Approval", agent: "SupervisorAgent", does: "Hold P1 and P2 wording for the shift. Nothing external leaves without a named person." },
  { id: "BROADCAST", label: "Broadcast", agent: "BroadcastCommsAgent", does: "Draft and queue the RNIO, FE and MSP SMS and email through the outbox." },
  { id: "EXEC_BRIEF", label: "Exec brief", agent: "ExecutiveBriefingAgent", does: "Write the exec brief management reads instead of phoning the NOC." },
  { id: "LEDGER", label: "Shift ledger", agent: "ShiftLedgerAgent", does: "Append the Excel shift ledger row (EAT)." },
  { id: "RECURRENCE", label: "Recurrence", agent: "RecurrenceProblemAgent", does: "Count faults at this site in the window; open or update a problem record." },
  { id: "MONITOR", label: "Monitor", agent: "WorklogMonitorAgent", does: "Set the note-chase and SLA clocks; chase a silent vendor." },
];

export const LIFECYCLE_NODE_IDS: ReadonlySet<string> = new Set(LIFECYCLE_NODES.map((n) => n.id));

/** Only the twelve lifecycle nodes belong on the rail: a scheduler job or an assist run
 *  records steps through the same tracker under its own node names. */
export function isLifecycleNode(id: unknown): boolean {
  return typeof id === "string" && LIFECYCLE_NODE_IDS.has(id);
}

/**
 * The one name of a hop, everywhere (rail, Showcase, Workflow map, Agents, Audit trail):
 * "EXEC_BRIEF" → "Exec brief", "HITL" → "Approval". Unknown ids (a scheduler job's node) are
 * humanised, so a new node still reads as words.
 */
export function nodeLabel(id: unknown): string {
  const hit = LIFECYCLE_NODES.find((n) => n.id === id);
  if (hit) return hit.label;
  const s = humanEnum(id);
  return s ? s[0].toUpperCase() + s.slice(1) : "";
}

/** What a hop does, in one sentence ("" for a node that is not one of the twelve). */
export function nodeDoes(id: unknown): string {
  return LIFECYCLE_NODES.find((n) => n.id === id)?.does ?? "";
}

/** Run statuses that mean the run has decided what it was going to decide. */
export const FINISHED_RUN_STATUSES: ReadonlySet<string> = new Set(["SUCCEEDED", "WAITING_HITL"]);

/** How a run was started, as a word worth printing: "" for the ordinary alarm event ("Incident
 *  lifecycle" needs no "event"), "scheduled", "on request", or the trigger humanised. */
export function triggerWord(trigger: unknown): string {
  const t = String(trigger ?? "").toUpperCase();
  if (!t || t === "EVENT") return "";
  if (t === "SCHEDULE") return "scheduled";
  if (t === "REQUEST") return "on request";
  return humanEnum(t);
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
  MPESA: "M‑PESA", HUB: "HUB", CORE: "CORE", MSP: "MSP", RNIO: "RNIO", FE: "FE", TX: "TX", MW: "MW", SMS: "SMS",
  NOC: "NOC", SLA: "SLA", EAT: "EAT", CA: "CA", PIR: "PIR", TT: "TT", INC: "INC", OEM: "OEM", RF: "RF", IP: "IP",
  DWDM: "DWDM", ENODEB: "eNodeB", NODEB: "NodeB", BTS: "BTS", BSC: "BSC", RNC: "RNC", MSC: "MSC", MGW: "MGW", POP: "PoP", GSM: "GSM", LTE: "LTE", VOICE: "voice", DATA: "data", HITL: "HITL",
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

/** The one phrase for a run or hop that is parked on a person, everywhere in the UI. */
export const WAITING_WORD = "waiting for a decision";

/** The run status as a person reads it. */
export function runStatusWord(status: unknown): string {
  const s = String(status ?? "").toUpperCase();
  if (s === "WAITING_HITL") return WAITING_WORD;
  return s ? s.toLowerCase() : "";
}

/**
 * How a run's status is shown when it is worth a look: the word and its tone (a `.state`
 * modifier in styles.css). A routine success returns null, because good news that is the
 * normal state is not coloured and gets no chip.
 */
export function runOutcome(status: unknown): { word: string; tone: "hitl" | "danger" | "accent" | "" } | null {
  const s = String(status ?? "").toUpperCase();
  if (!s || s === "SUCCEEDED") return null;
  if (s === "WAITING_HITL") return { word: WAITING_WORD, tone: "hitl" };
  if (s === "FAILED") return { word: "failed", tone: "danger" };
  if (s === "RUNNING") return { word: "running", tone: "accent" };
  return { word: runStatusWord(s), tone: "" };
}

/** A hop's state on the rail. `decided` is a hop that waited for a person and has been decided
 *  (the backend leaves those step rows at WAITING_HITL; `displaySteps` maps them). */
export type NodeStatus = "pending" | "running" | "succeeded" | "waiting_hitl" | "decided" | "failed" | "skipped";

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
  started_at?: string | null;
  finished_at?: string | null;
  /** Set by `displaySteps` on a decided hop: what the person decided. */
  decision?: "approved" | "rejected";
  /** Set by `displaySteps` on the Approval hop: how long the run waited (or has waited) for a person. */
  waited_ms?: number | null;
}

/** Backend step/node status words → the rail's vocabulary. Unknown words read as pending. */
export function normaliseStatus(raw: unknown): NodeStatus {
  const s = String(raw ?? "").toLowerCase();
  if (s === "succeeded") return "succeeded";
  if (s === "waiting_hitl") return "waiting_hitl";
  if (s === "decided") return "decided";
  if (s === "failed") return "failed";
  if (s === "started" || s === "running") return "running";
  if (s === "skipped") return "skipped";
  return "pending";
}

export const STATUS_WORD: Record<NodeStatus, string> = {
  pending: "pending",
  running: "running",
  succeeded: "done",
  waiting_hitl: WAITING_WORD,
  decided: "approved",
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

/** "Agents took": the sum of the step durations. This is the one figure for how long a run's
 *  agents worked, everywhere; a run's finished_at moves when a person approves hours later, so
 *  finished_at minus started_at is time waited, never time worked. */
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

/** "3 h 16 m", "4 m", "35 s": a wait, as a person says it. */
export function fmtWait(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return "—";
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} m`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h} h ${m % 60} m`;
  return `${Math.floor(h / 24)} d ${h % 24} h`;
}

/** Run statuses after which nothing more happens on the run (WAITING_HITL is still waiting). */
export const DONE_RUN_STATUSES: ReadonlySet<string> = new Set(["SUCCEEDED", "FAILED", "CANCELLED"]);

export function isRunDone(status: unknown): boolean {
  return DONE_RUN_STATUSES.has(String(status ?? "").toUpperCase());
}

/** The run opened a ticket: it carries a TICKET step. */
export function opensTicket(run: { steps?: RailStep[] | null } | null | undefined): boolean {
  return (run?.steps || []).some((s) => s && s.node_name === "TICKET");
}

/** The site an alarm came from, read from the Ingest step's input ("site=SFC-RFT-HUB-NKR"). */
export function alarmSite(steps: RailStep[] | null | undefined): string | null {
  const ingest = (steps || []).find((s) => s && s.node_name === "INGEST");
  const m = /site=([A-Za-z0-9][A-Za-z0-9_-]*)/.exec(String(ingest?.input_summary ?? ""));
  return m ? m[1] : null;
}

/**
 * A run that folded into an open ticket at Correlate (a repeat alarm, or a child site under its
 * HUB), read from its Correlate step ("merged into INC000001", "cascade child under INC000001").
 * Null for a run that opened a ticket or has not reached a decision at Correlate.
 */
export function foldOf(
  run: { steps?: RailStep[] | null } | null | undefined
): { into: string | null; site: string | null; kind: "repeat" | "child" } | null {
  const steps = run?.steps || [];
  if (opensTicket(run)) return null;
  const corr = steps.find((s) => s && s.node_name === "CORRELATE");
  if (!corr) return null;
  const m = /(merged into|cascade child under)\s+([A-Z]{2,}\d+)/i.exec(String(corr.output_summary ?? ""));
  if (!m) return null;
  return { into: m[2], site: alarmSite(steps), kind: /merged/i.test(m[1]) ? "repeat" : "child" };
}

function msOf(value: unknown): number | null {
  const d = parseInstant(value);
  return d ? d.getTime() : null;
}

/**
 * The steps of a run as the rail should draw them.
 *
 * - The backend leaves the Approval and Broadcast rows at WAITING_HITL after a person decides,
 *   while the run reads SUCCEEDED (approved) or CANCELLED (rejected). On a decided run those hops
 *   read "decided": approved (both), or rejected (Approval) with the Broadcast skipped, since it
 *   never left.
 * - The Approval hop carries `waited_ms`: from the hop parking the run to the decision (the run's
 *   finished_at), or to `now` while it still waits.
 * - A finished run that folded into an open ticket at Correlate draws the hops it never reached
 *   as skipped, not as "pending" grey dashes that look stalled.
 */
export function displaySteps(
  run: { status?: string | null; steps?: RailStep[] | null; finished_at?: string | null } | null | undefined,
  now: number = Date.now()
): RailStep[] {
  const steps = (run?.steps || []).filter((s): s is RailStep => !!s && !!s.node_name).map((s) => ({ ...s }));
  const status = String(run?.status ?? "").toUpperCase();
  const decided = status === "SUCCEEDED" || status === "CANCELLED";
  const rejected = status === "CANCELLED";
  const decidedAt = msOf(run?.finished_at);
  for (const s of steps) {
    if (normaliseStatus(s.status) !== "waiting_hitl") continue;
    if (s.node_name === "HITL") {
      const parkedAt = msOf(s.finished_at);
      const until = decided ? decidedAt : status === "WAITING_HITL" ? now : null;
      s.waited_ms = parkedAt != null && until != null ? Math.max(0, until - parkedAt) : null;
    }
    if (!decided) continue;
    if (rejected && s.node_name === "BROADCAST") {
      s.status = "SKIPPED";
      continue;
    }
    s.status = "DECIDED";
    s.decision = rejected ? "rejected" : "approved";
  }
  if (isRunDone(status) && foldOf({ steps })) {
    const ran = new Set(steps.map((s) => s.node_name));
    for (const n of LIFECYCLE_NODES) {
      if (!ran.has(n.id)) steps.push({ node_name: n.id, agent_name: n.agent, status: "SKIPPED" });
    }
  }
  return steps;
}
