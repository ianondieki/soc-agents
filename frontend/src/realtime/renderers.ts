import { humanEnum, humanStatus } from "../lib/agents";
import { fmtDateTime } from "../lib/time";

/**
 * WS renderer table — verified defect #26.
 *
 * Before: every frame on `/ws/ops` bumped one global `tick`, and every page
 * refetched its whole list. During a rain storm the backend emits dozens of
 * `agent.step.*` frames per second, so the wallboard issued dozens of
 * `/api/v1/incidents` + `/hitl/pending` + `/runs` + `/metrics/summary` calls per
 * second — exactly when the NOC needs the screen.
 *
 * After: each event type declares what it can actually change. High-volume,
 * single-incident events (the agent step stream) refresh **nothing** globally;
 * they only bump the revision of the incident they name, so an open workspace
 * stays live and the rest of the UI does not move. Events that change list
 * membership or counts mark the affected slices dirty and a debounced flush
 * coalesces a burst into one refetch per slice (see `useRealtime`).
 *
 * Safety: `rendererFor` never throws and an unknown type falls back to
 * `FALLBACK_RENDERER`, which is the old "refresh the lists" behaviour — still
 * debounced. A future event type therefore degrades to today's behaviour instead
 * of being dropped or blanking a panel.
 */

/** A refetchable piece of UI state. Each maps to one API call in one place. */
export type Slice =
  | "incidents"
  | "hitl"
  | "runs"
  | "metrics"
  | "audit"
  | "problems"
  | "ledger"
  | "signals"
  | "scheduler"
  | "pir";

export const ALL_SLICES: readonly Slice[] = [
  "incidents",
  "hitl",
  "runs",
  "metrics",
  "audit",
  "problems",
  "ledger",
  "signals",
  "scheduler",
  "pir",
];

export type Revisions = Record<Slice, number>;

export function zeroRevisions(): Revisions {
  return {
    incidents: 0,
    hitl: 0,
    runs: 0,
    metrics: 0,
    audit: 0,
    problems: 0,
    ledger: 0,
    signals: 0,
    scheduler: 0,
    pir: 0,
  };
}

/** A WS frame, normalised. `raw` is kept so the ticker can render unknown shapes. */
export interface NocEvent {
  type: string;
  ts: string | null;
  payload: Record<string, any>;
  incidentId: string | null;
  runId: string | null;
  seq: number | null;
  raw: any;
}

export interface RendererSpec {
  /** List slices this event can change. Empty = no global refetch at all. */
  readonly slices: readonly Slice[];
  /** Also bump the revision of `payload.incident_id` so an open workspace reloads. */
  readonly incidentScoped: boolean;
  /** Show in the Mission Control agent ticker. */
  readonly ticker: boolean;
  /**
   * Stays fully live in quiet mode. `true` = always (HITL prompts, failures);
   * `"priority"` = only when the payload says P1/P2.
   */
  readonly critical: boolean | "priority";
  /** Why this row is what it is — kept next to the row so it cannot drift. */
  readonly why: string;
}

const HIGH_PRIORITY = new Set(["P1", "P2"]);

/**
 * Event type -> what it updates.
 *
 * Types marked "(not emitted today)" are in the Phase 1 brief but no backend call
 * site publishes them yet (`grep -o 'type="[a-z.]*"' src/` lists what does). They
 * are mapped up front so the first frame that ever carries one is handled rather
 * than falling through to the blunt fallback.
 */
export const RENDERERS: Readonly<Record<string, RendererSpec>> = {
  // ---- incident lifecycle: changes list membership or the KPI counts ---------
  "incident.created": {
    slices: ["incidents", "metrics", "problems", "ledger"],
    incidentScoped: true,
    ticker: true,
    critical: "priority",
    why: "a new row appears on the board; open/priority counts move; recurrence may open a problem and the ledger gains a row",
  },
  "incident.merged": {
    slices: ["incidents", "metrics"],
    incidentScoped: true,
    ticker: true,
    critical: "priority",
    why: "an alarm folds into an existing incident — the open count and the parent row change",
  },
  "incident.cascade_child": {
    slices: ["incidents", "metrics"],
    incidentScoped: true,
    ticker: true,
    critical: "priority",
    why: "a child site is suppressed under its HUB parent; children-down on the parent row changes",
  },
  "incident.closed": {
    slices: ["incidents", "metrics", "ledger", "audit"],
    incidentScoped: true,
    ticker: true,
    critical: "priority",
    why: "the row leaves the open board, counts drop, a ledger row and an audit row are written",
  },
  "incident.reassigned": {
    slices: ["incidents", "audit"],
    incidentScoped: true,
    ticker: true,
    critical: "priority",
    why: "the owner column changes on the board and an audit row is written; operator-driven, never high volume",
  },
  // ---- single-incident detail: NO global refetch ---------------------------
  "incident.updated": {
    slices: [],
    incidentScoped: true,
    ticker: true,
    critical: "priority",
    why: "(not emitted today) a field changes on one incident — the open workspace reloads, the board does not",
  },
  "incident.note": {
    slices: [],
    incidentScoped: true,
    ticker: true,
    critical: false,
    why: "a work note lands on one incident; only that workspace's timeline changes",
  },

  // ---- agent run / step stream: the storm volume ---------------------------
  "agent.run.started": {
    slices: ["runs"],
    incidentScoped: true,
    ticker: true,
    critical: false,
    why: "(not emitted today) a run appears in the Agent Observatory list",
  },
  "agent.run.finished": {
    slices: ["runs", "incidents", "metrics"],
    incidentScoped: true,
    ticker: true,
    critical: false,
    why: "the incident is fully processed — its board row is final; fires once per incident, not per step",
  },
  "agent.step.started": {
    slices: [],
    incidentScoped: true,
    ticker: true,
    critical: false,
    why: "highest-volume event in a storm and it changes one incident's workflow graph only — never a list refetch",
  },
  "agent.step.completed": {
    slices: [],
    incidentScoped: true,
    ticker: true,
    critical: false,
    why: "as above: node status + rationale on one incident's graph",
  },

  // ---- HITL: always critical, the night shift must never miss one -----------
  "hitl.created": {
    slices: ["hitl", "metrics"],
    incidentScoped: true,
    ticker: true,
    critical: true,
    why: "(not emitted today) a new approval enters the shared queue",
  },
  "hitl.claimed": {
    slices: ["hitl"],
    incidentScoped: true,
    ticker: true,
    critical: true,
    why: "the queue shows who holds it — no count change, so metrics are left alone",
  },
  "hitl.approved": {
    slices: ["hitl", "metrics", "audit"],
    incidentScoped: true,
    ticker: true,
    critical: true,
    why: "the task leaves the queue, the pending count drops, broadcasts release, an audit row is written",
  },
  "hitl.rejected": {
    slices: ["hitl", "metrics", "audit"],
    incidentScoped: true,
    ticker: true,
    critical: true,
    why: "as approved; the backend also emits agent.run.finished alongside, which covers the runs list",
  },

  // ---- delivery ------------------------------------------------------------
  "email.sent": {
    slices: [],
    incidentScoped: true,
    ticker: true,
    critical: false,
    why: "confirmation only; it appears in one incident's timeline",
  },
  "email.failed": {
    slices: [],
    incidentScoped: true,
    ticker: true,
    critical: true,
    why: "a broadcast that did not go out must stay visible even in quiet mode",
  },
  "outbox.failed": {
    slices: [],
    incidentScoped: true,
    ticker: true,
    critical: true,
    why: "a FAILED/DEAD/REJECTED_UNAPPROVED outbox row — same reason as email.failed",
  },

  // ---- external signals (§7.3.2): weather / CAP / flood / planned power -----
  "external_signal.updated": {
    slices: ["signals"],
    incidentScoped: false,
    ticker: true,
    critical: false,
    why: "the Wallboard risk strip is the only thing that reads this; six regions every 15 min, and weather is advisory so it never earns a board or metrics refetch",
  },
  "power_notice.new": {
    slices: ["signals"],
    incidentScoped: false,
    ticker: true,
    critical: false,
    why: "(not emitted today) a KPLC interruption notice was parsed; it reaches an operator as a CONFIRM_POWER_NOTICE HITL task, which hitl.created already covers",
  },
  "complaint.surge": {
    slices: ["signals"],
    incidentScoped: false,
    ticker: true,
    critical: false,
    why: "(not emitted today — Phase 6; dashboards.py reports complaint_surge=null) §7.10 lists it as a Regions refetch trigger, and Regions already refetches on the signals slice; advisory, so it never outranks a P1",
  },

  // ---- post-incident reviews (§7.7) -----------------------------------------
  "pir.opened": {
    slices: ["pir"],
    incidentScoped: true,
    ticker: true,
    critical: false,
    why: "services/pir.open_pir buffers {incident_number, opened_reason, pir_id, status} after the insert; the PIRs list and awaiting-review count change, and that incident's workspace reloads",
  },

  // ---- regulatory clock (§5.3.20): a statutory deadline is never quiet -----
  "regulatory.deadline": {
    slices: [],
    incidentScoped: true,
    ticker: true,
    critical: true,
    why: "services/regulatory.sweep_deadlines crossed a 12 h / 2 h threshold ({notification_id, kind, status, incident_number, threshold_hours, due_at, due_at_eat, minutes_remaining, overdue, hitl_task_id}); only that incident's countdown panel refetches, but quiet mode must never hide it",
  },

  // ---- platform health: the AGENTS OFFLINE and breach signals (§10.4, §9.6) --
  "scheduler.job_failed": {
    slices: ["scheduler", "runs"],
    incidentScoped: false,
    ticker: true,
    critical: true,
    why: "scheduler/loop.py publishes it only when a job's circuit opens ({job, run_id, error, consecutive_failures, circuit_open: true}); the Wallboard AgentsStatusTile re-reads /scheduler/status and the FAILED run joins the runs list",
  },
  "security.redaction_miss": {
    slices: ["audit"],
    incidentScoped: false,
    ticker: true,
    critical: true,
    why: "housekeeping.post_send_redaction_scan found contact-detail patterns in a SENT payload ({outbox_id, kind, incident_number, sent_at, email_matches, phone_matches, paths, note} — counts and paths, never the value); an audit row was written and the Wallboard shows a red chip",
  },

  // ---- housekeeping --------------------------------------------------------
  "monitor.chase": {
    slices: ["incidents", "audit"],
    incidentScoped: true,
    ticker: true,
    critical: false,
    why: "the SLA chase writes a note and can escalate, which changes the board row",
  },
  "demo.rain_storm.complete": {
    slices: ["incidents", "hitl", "runs", "metrics", "problems", "ledger"],
    incidentScoped: false,
    ticker: true,
    critical: false,
    why: "end of a bulk demo inject — one full resync is correct here and it fires once",
  },
};

/**
 * Unknown / future event type: do what the UI did before this table existed —
 * refresh the lists — but through the same debounce, so an unrecognised burst
 * still costs one refetch per slice rather than one per frame.
 */
export const FALLBACK_RENDERER: RendererSpec = {
  slices: ["incidents", "hitl", "runs", "metrics"],
  incidentScoped: true,
  ticker: true,
  critical: "priority",
  why: "unknown type — degrade to the pre-table behaviour rather than render nothing",
};

/**
 * Never throws, always returns a usable spec.
 *
 * The `hasOwnProperty` guard is load-bearing, not ceremony: a plain
 * `RENDERERS[type]` returns `Object.prototype` for an event typed
 * `"__proto__"` and a function for `"toString"` or `"constructor"`. Either
 * would then be read as a spec whose `.slices` is `undefined`, which throws
 * inside the router and silently drops the frame.
 */
export function rendererFor(type: unknown): RendererSpec {
  if (typeof type !== "string" || !type) return FALLBACK_RENDERER;
  if (!Object.prototype.hasOwnProperty.call(RENDERERS, type)) return FALLBACK_RENDERER;
  const spec = RENDERERS[type];
  return spec && Array.isArray(spec.slices) ? spec : FALLBACK_RENDERER;
}

export function isKnownEventType(type: unknown): boolean {
  return typeof type === "string" && Object.prototype.hasOwnProperty.call(RENDERERS, type);
}

/**
 * Normalise a WS frame.
 *
 * `/ws/ops` sends the bare six-key envelope today. Spec §7.0.4 wraps it as
 * `{"seq": N, "event": <envelope>}` once the realtime work lands, so both shapes
 * are accepted — a frontend that only understood one of them would go blank the
 * moment the backend changed. Returns `null` for anything unusable.
 */
export function normalizeEvent(raw: any): NocEvent | null {
  if (raw == null || typeof raw !== "object") return null;

  let seq: number | null = null;
  let env: any = raw;
  if (typeof raw.seq === "number" && raw.event && typeof raw.event === "object") {
    seq = raw.seq;
    env = raw.event;
  } else if (typeof raw.seq === "number") {
    seq = raw.seq;
  }

  const type = typeof env.type === "string" ? env.type : "";
  if (!type) return null;

  const payload =
    env.payload && typeof env.payload === "object" && !Array.isArray(env.payload)
      ? (env.payload as Record<string, any>)
      : {};

  const incidentId =
    typeof env.incident_id === "string" && env.incident_id
      ? env.incident_id
      : typeof payload.incident_id === "string" && payload.incident_id
        ? payload.incident_id
        : null;

  return {
    type,
    ts: typeof env.ts === "string" ? env.ts : null,
    payload,
    incidentId,
    runId: typeof env.run_id === "string" && env.run_id ? env.run_id : null,
    seq,
    raw: env,
  };
}

/** Does this event stay live while quiet mode suppresses UI churn? */
export function isCriticalEvent(ev: NocEvent, spec?: RendererSpec): boolean {
  const s = spec ?? rendererFor(ev.type);
  if (s.critical === true) return true;
  if (s.critical === false) return false;
  const p = ev.payload?.priority;
  return typeof p === "string" && HIGH_PRIORITY.has(p.toUpperCase());
}

/* ------------------------------------------------------------ ticker text --
 * The generic ticker line reads `incident_number`, `node`, `agent`, `status`
 * and one of `rationale`/`output`/`detail`. None of the payloads below carries
 * the fields that make them worth reading (the failed job's name, the
 * deadline, the pattern counts), or its `detail` is written for a developer
 * (the mock email names environment variables), so each gets a describer built
 * from what the backend actually sends — grep the `type="…"` string in
 * src/noc_agents/ for the publisher. Mission control prints the description in
 * place of the raw detail; the stored event is never changed. A describer that
 * throws or gets an odd payload yields "" and the generic line stands.
 * Sentence case throughout: the ticker shouts nothing ("overdue", not "OVERDUE").
 *
 * `security.redaction_miss` is described from counts and JSON paths only: the
 * payload never carries the matched value (§9.5) and this text must not grow a
 * way to show one.
 */
function num(v: unknown): string {
  return typeof v === "number" && Number.isFinite(v) ? String(v) : "?";
}

function span(minutes: unknown): string {
  if (typeof minutes !== "number" || !Number.isFinite(minutes)) return "?";
  const m = Math.abs(Math.trunc(minutes));
  const h = Math.floor(m / 60);
  return h ? `${h} h ${m % 60} min` : `${m} min`;
}

/** True when a mock adapter stored the message instead of sending it. */
function isMock(p: Record<string, any>): boolean {
  return String(p.mode ?? "").toLowerCase() === "mock";
}

const DESCRIBERS: Readonly<Record<string, (p: Record<string, any>) => string>> = {
  // services/notify.record_email_outcome: {incident_number, mode, to, detail, status}. A mock
  // send's detail names env vars ("No DEMO_EMAIL_TO / GMAIL_ADDRESS — …") or lists the
  // addresses it would have used; the floor reads what happened, not how to configure it.
  "email.sent": (p) => {
    if (isMock(p)) return "Email stored, not sent (mock)";
    const n = Array.isArray(p.to) ? p.to.length : 0;
    return n ? `Sent to ${n} recipient${n === 1 ? "" : "s"}` : "";
  },
  "email.failed": (p) => (isMock(p) ? "Email stored, not sent (mock)" : ""),
  // services/pir.open_pir: {incident_number, opened_reason, pir_id, status}
  "pir.opened": (p) => `Review opened, trigger ${humanEnum(p.opened_reason) || "?"}`,
  // services/regulatory._deadline_event: {notification_id, kind, status, incident_number,
  // threshold_hours, due_at, due_at_eat, minutes_remaining, overdue, hitl_task_id}
  "regulatory.deadline": (p) =>
    `${humanEnum(p.kind) || "notice"}, ${p.overdue ? `overdue by ${span(p.minutes_remaining)}` : `${span(p.minutes_remaining)} left`}` +
    `, due ${fmtDateTime(p.due_at)} EAT, ${num(p.threshold_hours)} h threshold`,
  // scheduler/loop.py: {job, run_id, error, consecutive_failures, circuit_open}
  "scheduler.job_failed": (p) =>
    `Job ${humanStatus(p.job) || "?"}, ${p.circuit_open ? "circuit open" : "failed"} after ${num(p.consecutive_failures)} failures` +
    (p.error ? `, ${String(p.error).slice(0, 90)}` : ""),
  // services/housekeeping.RedactionHit.as_payload: {outbox_id, kind, incident_number, sent_at,
  // email_matches, phone_matches, paths, note}
  "security.redaction_miss": (p) =>
    `Redaction miss in a sent ${humanEnum(p.kind) || "message"}: ${num(p.email_matches)} email and ${num(p.phone_matches)} MSISDN patterns` +
    (Array.isArray(p.paths) && p.paths.length ? ` at ${p.paths.slice(0, 3).join(", ")}` : ""),
  // NO PUBLISHER YET (Phase 6): the one describer built from the spec, not from code — §7.4.2's
  // {region_code, product_hint, zscore, count, bucket_start}. Every field is optional here, so a
  // producer that ships a different shape degrades to "complaint surge", never to a throw.
  "complaint.surge": (p) =>
    [
      "Complaint surge",
      p.region_code,
      p.product_hint,
      p.count != null ? `${num(p.count)} complaints` : null,
      typeof p.zscore === "number" ? `z ${p.zscore.toFixed(1)}` : null,
      p.bucket_start ? `from ${fmtDateTime(p.bucket_start)} EAT` : null,
    ]
      .filter(Boolean)
      .join(", "),
};

/** Extra ticker text for one event, or "" — never throws. */
export function describeEvent(ev: NocEvent): string {
  try {
    if (!Object.prototype.hasOwnProperty.call(DESCRIBERS, ev.type)) return "";
    const text = DESCRIBERS[ev.type](ev.payload || {});
    return text ? `— ${text}` : "";
  } catch {
    return "";
  }
}
