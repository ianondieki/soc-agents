import { noteFailure, noteResponse } from "../realtime/apiHealth";
import { fmtMs } from "./agents";
import { fmtDateTime, fmtHM, parseInstant } from "./time";

/**
 * The Support desk (docs/SUPPORT_DESK.md): the contract's shapes, the API calls, and the words
 * the two screens use for them. No React in here, so the public form and the console page share
 * one vocabulary and the realtime layer stays free of it.
 */

// ------------------------------------------------------------------------------- shapes

export type Category =
  | "network"
  | "data_bundles"
  | "mpesa"
  | "billing"
  | "sim_and_fraud"
  | "device_settings"
  | "roaming"
  | "account"
  | "other";
export type Urgency = "low" | "normal" | "high" | "critical";
export type Sentiment = "calm" | "frustrated" | "angry";
export type Route = "resolver" | "action" | "human";
export type Status = "answered" | "action_taken" | "awaiting_approval" | "escalated" | "in_progress" | "resolved" | "closed";
export type Outcome = "auto_resolved" | "action_completed" | "escalated" | "human_resolved";
export type Channel = "web" | "sms" | "app" | "call_centre" | "social";
export type Language = "en" | "sw" | "mixed";
export type ToolStatus = "ok" | "refused" | "needs_approval" | "approved" | "rejected" | "failed";
export type Agent = "intake" | "triage" | "resolver" | "action" | "escalation" | "human";

export interface Citation {
  article_id: string;
  title: string;
  score: number;
}

export interface Escalation {
  reason_code: string;
  reason: string;
  at: string;
  claimed_by: string | null;
}

export interface LinkedIncident {
  id: string;
  incident_number: string;
  title: string;
  status: string;
}

export interface Complaint {
  id: string;
  ref: string;
  created_at: string;
  updated_at: string;
  channel: Channel;
  customer: { name: string | null; msisdn_masked: string; account_ref: string | null };
  language: Language;
  subject: string;
  body: string;
  category: Category;
  urgency: Urgency;
  sentiment: Sentiment;
  route: Route;
  status: Status;
  outcome: Outcome | null;
  confidence: number;
  escalation: Escalation | null;
  reply: string | null;
  citations: Citation[];
  linked_incident: LinkedIncident | null;
  sla_due_at: string;
}

export interface Step {
  seq: number;
  agent: Agent;
  action: string;
  summary: string;
  detail: Record<string, unknown>;
  duration_ms: number;
  at: string;
}

export interface ToolCall {
  id: string;
  tool: string;
  args: Record<string, unknown>;
  result: Record<string, unknown> | null;
  status: ToolStatus;
  policy: string | null;
  at: string;
  decided_by: string | null;
}

export interface Message {
  id: string;
  author: "customer" | "agent" | "staff";
  name: string | null;
  body: string;
  at: string;
}

export interface CaseDetail {
  complaint: Complaint;
  steps: Step[];
  tool_calls: ToolCall[];
  messages: Message[];
}

export interface Queue {
  items: Complaint[];
  counts: {
    by_status: Partial<Record<Status, number>>;
    by_route: Partial<Record<Route, number>>;
    by_category: Partial<Record<Category, number>>;
  };
}

export interface Metrics {
  total: number;
  auto_resolved: number;
  action_completed: number;
  escalated: number;
  human_resolved: number;
  awaiting_approval: number;
  resolution_rate: number;
  escalation_rate: number;
  median_handle_ms: number;
  by_category: Record<string, number>;
}

export interface KbArticle {
  id: string;
  title: string;
  category: Category;
  summary: string;
  body: string;
  updated_at: string;
}

export interface KbHit {
  article_id: string;
  title: string;
  score: number;
  snippet: string;
}

export interface EvalMetrics {
  resolution_rate: number;
  wrong_escalation_rate: number;
  missed_escalation_rate: number;
  safety_missed_escalation_rate: number;
  escalation_reason_accuracy: number;
  containment_rate: number;
  triage_accuracy: number;
  routing_accuracy: number;
  grounded_answer_rate: number;
  tool_accuracy: number;
  p50_ms: number;
}

export interface EvalGate {
  metric: string;
  op: ">=" | "<=" | "==";
  threshold: number;
  value: number;
  passed: boolean;
}

export type FailureKind =
  | "wrong_escalation"
  | "missed_escalation"
  | "wrong_route"
  | "wrong_category"
  | "wrong_article"
  | "wrong_tool"
  | "unresolved";

export interface EvalFailure {
  case_id: string;
  text: string;
  kind: FailureKind;
  expected: Record<string, unknown>;
  actual: Record<string, unknown>;
}

export interface EvalReport {
  run_id: string;
  ran_at: string;
  mode: "deterministic" | "llm";
  /** `split` is the headline split ("holdout" on a full run); `excluded` counts contested cases left out. */
  dataset: { name: string; version: string; size: number; split: string; excluded?: number };
  metrics: EvalMetrics;
  gates: EvalGate[];
  passed: boolean;
  confusion: { labels: Route[]; matrix: number[][] };
  by_category: {
    category: string;
    n: number;
    resolution_rate: number | null;
    wrong_escalation_rate: number | null;
    triage_accuracy: number | null;
  }[];
  failures: EvalFailure[];
  /** Per split (dev, validation, holdout; older reports: dev, test). The top-level metrics are the
   *  headline split's, named in `dataset.split`. */
  by_split?: Record<string, EvalMetrics>;
}

// ---------------------------------------------------------------------------------- API

/** What a failed call carries: the status, the body (parsed when JSON) and Retry-After, in seconds. */
export class SupportApiError extends Error {
  status: number;
  body: unknown;
  retryAfter: number | null;
  constructor(status: number, body: unknown, retryAfter: number | null) {
    super(`${status}: ${typeof body === "string" ? body : JSON.stringify(body)}`);
    this.status = status;
    this.body = body;
    this.retryAfter = retryAfter;
  }
}

export const isSupportApiError = (e: unknown): e is SupportApiError => e instanceof SupportApiError;
export const statusOfError = (e: unknown): number | null => (isSupportApiError(e) ? e.status : null);
/** `404` on every route is the lane switched off (`SUPPORT_DESK_ENABLED=false`). */
export const isLaneOff = (e: unknown): boolean => statusOfError(e) === 404;
/** A fetch that never reached the server. */
export const isNetworkError = (e: unknown): boolean => !isSupportApiError(e) && e instanceof TypeError;

/** FastAPI's `{"detail": …}`, as one sentence; the pydantic list joined, or the fallback. */
export function errorDetail(e: unknown, fallback = "The request failed."): string {
  if (!isSupportApiError(e)) return e instanceof Error && e.message ? e.message : fallback;
  const b: any = e.body;
  const d = b && typeof b === "object" ? b.detail : null;
  if (typeof d === "string" && d.trim()) return capFirst(d.trim());
  if (Array.isArray(d)) {
    const lines = d
      .map((x: any) => (typeof x?.msg === "string" ? x.msg.replace(/^Value error,\s*/i, "") : ""))
      .filter(Boolean);
    if (lines.length) return capFirst(lines.join("; "));
  }
  return fallback;
}

/** The pydantic errors of a 422 by field: `{ msisdn: "…", body: "…" }`. */
export function fieldErrors(e: unknown): Record<string, string> {
  const out: Record<string, string> = {};
  if (!isSupportApiError(e) || e.status !== 422) return out;
  const d = (e.body as any)?.detail;
  if (!Array.isArray(d)) return out;
  for (const x of d) {
    const loc = Array.isArray(x?.loc) ? x.loc.filter((p: unknown) => p !== "body") : [];
    const field = String(loc[0] ?? "");
    const msg = typeof x?.msg === "string" ? x.msg.replace(/^Value error,\s*/i, "") : "";
    if (field && msg && !out[field]) out[field] = capFirst(msg);
  }
  return out;
}

function capFirst(s: string): string {
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}

const BASE = "/api/v1/support";

/** Every answer, with its status, so a 200-duplicate reads differently from a 201. */
export interface Answer<T> {
  status: number;
  data: T;
}

async function call<T>(path: string, init?: RequestInit): Promise<Answer<T>> {
  let r: Response;
  try {
    r = await fetch(`${BASE}${path}`, {
      headers: { Accept: "application/json", ...(init?.body ? { "Content-Type": "application/json" } : {}), ...(init?.headers || {}) },
      ...init,
    });
  } catch (e) {
    noteFailure(path);
    throw e;
  }
  noteResponse(r, path);
  if (!r.ok) {
    const text = await r.text();
    let body: unknown = text;
    try {
      body = JSON.parse(text);
    } catch {
      /* plain text */
    }
    const ra = r.headers.get("retry-after");
    const retryAfter = ra != null && Number.isFinite(Number(ra)) ? Number(ra) : null;
    throw new SupportApiError(r.status, body, retryAfter);
  }
  const data = (r.status === 204 ? null : await r.json()) as T;
  return { status: r.status, data };
}

const json = <T>(path: string, init?: RequestInit) => call<T>(path, init).then((a) => a.data);

export interface ComplaintIn {
  body: string;
  msisdn: string;
  name?: string;
  subject?: string;
  channel?: Channel;
  account_ref?: string;
}

export const supportApi = {
  queue: (params: { status?: string; route?: string; category?: string; q?: string; limit?: number } = {}) => {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== "" && v !== null) qs.set(k, String(v));
    const s = qs.toString();
    return json<Queue>(`/complaints${s ? `?${s}` : ""}`);
  },
  complaint: (id: string) => json<CaseDetail>(`/complaints/${encodeURIComponent(id)}`),
  /** 201 with the detail; 200 when the same number sent the same text in the last two minutes. */
  register: (body: ComplaintIn) => call<CaseDetail>("/complaints", { method: "POST", body: JSON.stringify(body) }),
  claim: (id: string) => json<CaseDetail>(`/complaints/${encodeURIComponent(id)}/claim`, { method: "POST", body: "{}" }),
  resolve: (id: string, reply: string, note?: string) =>
    json<CaseDetail>(`/complaints/${encodeURIComponent(id)}/resolve`, {
      method: "POST",
      body: JSON.stringify(note && note.trim() ? { reply, note: note.trim() } : { reply }),
    }),
  approve: (id: string, callId: string) =>
    json<CaseDetail>(`/complaints/${encodeURIComponent(id)}/actions/${encodeURIComponent(callId)}/approve`, { method: "POST", body: "{}" }),
  reject: (id: string, callId: string, reason: string) =>
    json<CaseDetail>(`/complaints/${encodeURIComponent(id)}/actions/${encodeURIComponent(callId)}/reject`, {
      method: "POST",
      body: JSON.stringify({ reason }),
    }),
  kb: () => json<{ articles: KbArticle[] }>("/kb"),
  kbSearch: (q: string) => json<{ results: KbHit[] }>(`/kb/search?q=${encodeURIComponent(q)}`),
  metrics: (hours = 0) => json<Metrics>(`/metrics?hours=${hours}`),
  evalsLatest: () => json<EvalReport>("/evals/latest"),
  evalsRun: () => json<EvalReport>("/evals/run", { method: "POST", body: "{}" }),
  seed: () => json<{ created: number }>("/demo/seed", { method: "POST", body: "{}" }),
};

// ---------------------------------------------------------------------------------- words

export const CATEGORY_WORD: Record<Category, string> = {
  network: "Network",
  data_bundles: "Data bundles",
  mpesa: "M-PESA",
  billing: "Billing",
  sim_and_fraud: "SIM and fraud",
  device_settings: "Device settings",
  roaming: "Roaming",
  account: "Account",
  other: "Other",
};
export const CATEGORIES = Object.keys(CATEGORY_WORD) as Category[];

export const STATUS_WORD: Record<Status, string> = {
  answered: "Answered",
  action_taken: "Action taken",
  awaiting_approval: "Awaiting approval",
  escalated: "With a person",
  in_progress: "Claimed",
  resolved: "Resolved",
  closed: "Closed",
};
export const STATUSES = Object.keys(STATUS_WORD) as Status[];

export const ROUTE_WORD: Record<Route, string> = { resolver: "Resolver", action: "Action agent", human: "Person" };
export const ROUTES = Object.keys(ROUTE_WORD) as Route[];

export const CHANNEL_WORD: Record<Channel, string> = {
  web: "Web form",
  sms: "SMS",
  app: "App",
  call_centre: "Call centre",
  social: "Social",
};

export const LANGUAGE_WORD: Record<Language, string> = { en: "English", sw: "Kiswahili", mixed: "English and Kiswahili" };

export const AGENT_WORD: Record<Agent, string> = {
  intake: "Intake",
  triage: "Triage",
  resolver: "Resolver",
  action: "Action agent",
  escalation: "Escalation",
  human: "Person",
};

/** What each agent does, in one line, for the trace. */
export const AGENT_DOES: Record<Agent, string> = {
  intake: "Normalised the text, masked the number, detected the language and checked for a repeat.",
  triage: "Chose the category, urgency and sentiment, raised any risk flags and picked the route.",
  resolver: "Searched the knowledge base and answered only from an article that is grounded.",
  action: "Looked up the account and called tools under their policy limits.",
  escalation: "Sent the case to a person, with the reason.",
  human: "A member of staff decided.",
};

export const TOOL_STATUS_WORD: Record<ToolStatus, string> = {
  ok: "Done",
  refused: "Refused",
  needs_approval: "Needs approval",
  approved: "Approved",
  rejected: "Rejected",
  failed: "Failed",
};

/** A tool's name as the floor says it. */
export const TOOL_WORD: Record<string, string> = {
  lookup_account: "Look up the account",
  issue_refund: "Issue a refund",
  reverse_mpesa: "Reverse the M-PESA transfer",
  recredit_bundle: "Re-credit the bundle",
  link_incident: "Link a network incident",
  update_ticket: "Update the ticket",
  reset_network_settings: "Send network settings",
};
export const toolWord = (tool: unknown): string => TOOL_WORD[String(tool)] ?? humanWords(tool);

/** The same tools as a noun phrase inside a sentence: "approve the M-PESA reversal". */
export const TOOL_PHRASE: Record<string, string> = {
  lookup_account: "the account lookup",
  issue_refund: "the refund",
  reverse_mpesa: "the M-PESA reversal",
  recredit_bundle: "the bundle re-credit",
  link_incident: "the incident link",
  update_ticket: "the ticket update",
  reset_network_settings: "the network settings SMS",
};
export const toolPhrase = (tool: unknown): string => TOOL_PHRASE[String(tool)] ?? humanWords(tool);

/** The escalation reason codes (config/support/policy.yaml), as a short phrase after "With a person:". */
export const REASON_WORD: Record<string, string> = {
  fraud_or_sim_swap: "fraud or SIM swap reported",
  legal_or_regulator: "a legal or regulatory matter",
  threat_or_safety: "a threat or safety concern",
  needs_verification: "the transfer needs verifying with the customer",
  over_refund_limit: "over the automatic refund limit",
  repeat_unresolved: "third complaint in a week",
  angry_high_value: "angry high-value customer",
  low_confidence: "triage was not confident",
  not_grounded: "no grounded article to answer from",
  tool_failed: "a tool call failed",
};
export const reasonWord = (code: unknown): string => REASON_WORD[String(code)] ?? humanWords(code);

/** Safety reasons: the gate on missed escalations is zero for these. */
export const SAFETY_REASONS: ReadonlySet<string> = new Set(["fraud_or_sim_swap", "legal_or_regulator", "threat_or_safety"]);

export const FAILURE_KIND_WORD: Record<FailureKind, string> = {
  wrong_escalation: "Wrong escalation",
  missed_escalation: "Missed escalation",
  wrong_route: "Wrong route",
  wrong_category: "Wrong category",
  wrong_article: "Wrong article",
  wrong_tool: "Wrong tool",
  unresolved: "Unresolved",
};

/** `some_enum_value` -> "some enum value"; anything with lower case already is kept. */
export function humanWords(v: unknown): string {
  const s = String(v ?? "").trim();
  if (!s) return "";
  return s.replace(/[_\s]+/g, " ").toLowerCase();
}

/** A detail key as a label: "account_found" -> "Account found", "msisdn_masked" -> "Number". */
const KEY_LABEL: Record<string, string> = {
  msisdn_masked: "Number",
  msisdn: "Number",
  chars: "Length",
  account_found: "Account on record",
  tier: "Tier",
  risk_flags: "Risk flags",
  intent: "Intent",
  tool: "Tool",
  places: "Places named",
  scores: "Category scores",
  source: "Decided by",
  grounded: "Grounded",
  article_id: "Article",
  score: "Score",
  candidates: "Candidates",
  reason_code: "Reason code",
  evidence: "Evidence",
  also_matched: "Also matched",
  held_tool: "Held tool call",
  why: "Why this target",
  args: "Arguments",
  result: "Result",
  policy: "Policy",
  claimed_by: "Claimed by",
  resolved_by: "Resolved by",
  decided_by: "Decided by",
  note: "Note",
  transaction_code: "Transaction code",
  amount_kes: "Amount",
  new_mpesa_balance_kes: "New M-PESA balance",
  mpesa_balance_kes: "M-PESA balance",
  airtime_balance_kes: "Airtime balance",
  account_ref: "Account",
  recent_transactions: "Recent transactions",
  bundles: "Bundles",
  charges: "Charges",
  incident_number: "Ticket",
  incident_id: "Ticket id",
  site_name: "Site",
  region_code: "Region",
  reversal_id: "Reversal",
  counterparty: "Counterparty",
  credited_to: "Credited to",
  bundle_id: "Bundle",
  regions: "Regions",
  place: "Place",
  match: "Matched on",
  found: "Found",
  status: "Status",
  plan: "Plan",
  device: "Device",
  name: "Name",
  language: "Language",
  channel: "Channel",
  category: "Category",
  confidence: "Confidence",
  urgency: "Urgency",
  sentiment: "Sentiment",
  route: "Route",
  reasons: "Reasons that fired",
  escalate: "Escalate",
};
export function keyLabel(k: string): string {
  if (KEY_LABEL[k]) return KEY_LABEL[k];
  const s = humanWords(k);
  return s ? s[0].toUpperCase() + s.slice(1) : k;
}

/** A scalar detail value as text: money in KES, booleans as words, enums as words. */
export function valueText(k: string, v: unknown): string {
  if (v == null || v === "") return "—";
  if (typeof v === "boolean") return v ? "Yes" : "No";
  if (typeof v === "number") {
    if (/_kes$/.test(k)) return `KES ${v.toLocaleString("en-KE")}`;
    if (/_mb$/.test(k)) return v >= 1024 ? `${(v / 1024).toFixed(v % 1024 ? 1 : 0)} GB` : `${v} MB`;
    if (k === "hours_ago") return `${v} h ago`;
    return Number.isInteger(v) ? String(v) : String(Math.round(v * 1000) / 1000);
  }
  const s = String(v);
  if (k === "category" && (CATEGORY_WORD as Record<string, string>)[s]) return (CATEGORY_WORD as Record<string, string>)[s];
  if (k === "route" && (ROUTE_WORD as Record<string, string>)[s]) return (ROUTE_WORD as Record<string, string>)[s];
  if (k === "channel" && (CHANNEL_WORD as Record<string, string>)[s]) return (CHANNEL_WORD as Record<string, string>)[s];
  if (k === "language" && (LANGUAGE_WORD as Record<string, string>)[s]) return (LANGUAGE_WORD as Record<string, string>)[s];
  if (k === "status" && (TOOL_STATUS_WORD as Record<string, string>)[s]) return (TOOL_STATUS_WORD as Record<string, string>)[s];
  if (k === "reason_code" || k === "held_tool" || k === "tool" || k === "intent") return s; // codes stay codes
  if (/^[a-z_]+$/.test(s) && s.includes("_")) return humanWords(s);
  return s;
}

/** Keys whose values are identifiers or codes: shown in the mono. */
export const MONO_KEYS: ReadonlySet<string> = new Set([
  "msisdn_masked",
  "msisdn",
  "article_id",
  "reason_code",
  "held_tool",
  "tool",
  "intent",
  "transaction_code",
  "reversal_id",
  "account_ref",
  "incident_number",
  "incident_id",
  "region_code",
  "bundle_id",
  "code",
  "id",
]);

// ------------------------------------------------------------------------------- figures

/** `0.5385` -> "54%". */
export function pct(v: unknown, digits = 0): string {
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  return `${(n * 100).toFixed(digits)}%`;
}

export function kes(v: unknown): string {
  const n = Number(v);
  return Number.isFinite(n) ? `KES ${n.toLocaleString("en-KE")}` : "";
}

/** Minutes since `iso`, never negative; null when unparseable. */
export function minutesSince(iso: unknown, now = Date.now()): number | null {
  const d = parseInstant(iso);
  if (!d) return null;
  return Math.max(0, Math.floor((now - d.getTime()) / 60000));
}

const NBSP = " ";

/** "under a minute", "4 min", "3 h 12 min", "2 d 4 h". */
export function fmtAgeMin(mins: number | null): string {
  if (mins == null) return "";
  if (mins < 1) return "under a minute";
  if (mins < 60) return `${mins}${NBSP}min`;
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  if (h < 48) return m ? `${h}${NBSP}h ${m}${NBSP}min` : `${h}${NBSP}h`;
  const d = Math.floor(h / 24);
  return `${d}${NBSP}d ${h % 24}${NBSP}h`;
}

export const fmtAge = (iso: unknown): string => fmtAgeMin(minutesSince(iso));

/** "15:32 EAT" today, else "05 Oct, 15:32 EAT". */
export function fmtDue(iso: unknown): string {
  const d = parseInstant(iso);
  if (!d) return "—";
  const today = new Date();
  const sameDay = fmtDateTime(d).slice(0, 6) === fmtDateTime(today).slice(0, 6);
  return sameDay ? `${fmtHM(d)} EAT` : `${fmtDateTime(d).replace(/:\d{2}$/, "")} EAT`;
}

/** Minutes until `iso`; negative when past. */
export function minutesUntil(iso: unknown, now = Date.now()): number | null {
  const d = parseInstant(iso);
  return d ? Math.round((d.getTime() - now) / 60000) : null;
}

export { fmtMs };

// -------------------------------------------------------------------------------- verdict

export type Tone = "" | "hitl" | "ok" | "warn" | "danger";

export interface Verdict {
  text: string;
  /** A second, quieter line: the article title, the policy sentence, the reason. */
  sub?: string;
  tone: Tone;
  route: Route;
}

/** The successful or held tool call the case turns on: not the lookup, not the ticket note. */
export function keyToolCall(calls: ToolCall[]): ToolCall | null {
  const meaningful = calls.filter((c) => c.tool !== "lookup_account" && c.tool !== "update_ticket");
  return (
    meaningful.find((c) => c.status === "needs_approval") ??
    meaningful.find((c) => c.status === "ok" || c.status === "approved") ??
    meaningful.find((c) => c.status === "refused" || c.status === "failed" || c.status === "rejected") ??
    null
  );
}

/** "Reversed KES 1,500 by the action agent", from a successful call. */
export function actionSentence(call: ToolCall): string {
  const r = (call.result ?? {}) as Record<string, unknown>;
  const a = call.args ?? {};
  const amount = kes(r.amount_kes ?? a.amount_kes ?? r.refund_kes);
  switch (call.tool) {
    case "reverse_mpesa":
      return amount ? `Reversed ${amount} by the action agent` : "Reversed the M-PESA transfer by the action agent";
    case "issue_refund":
      return amount ? `Refunded ${amount} by the action agent` : "Refunded by the action agent";
    case "recredit_bundle":
      return r.name || r.bundle ? `Re-credited the ${String(r.name ?? r.bundle)} bundle by the action agent` : "Re-credited the bundle by the action agent";
    case "link_incident":
      return r.incident_number ? `Linked to ${String(r.incident_number)} by the action agent` : "Linked to a network incident by the action agent";
    case "reset_network_settings":
      return "Sent the network settings by the action agent";
    default:
      return `${toolWord(call.tool)}: done by the action agent`;
  }
}

/** The one line that says how the case ended, or where it is now. */
export function verdictOf(d: CaseDetail): Verdict {
  const c = d.complaint;
  const key = keyToolCall(d.tool_calls);
  const cite = c.citations[0];
  const who = c.escalation?.claimed_by;
  switch (c.status) {
    case "answered":
      return cite
        ? { text: `Answered by the resolver from ${cite.article_id}`, sub: cite.title, tone: "", route: "resolver" }
        : { text: "Answered by the resolver", tone: "", route: "resolver" };
    case "action_taken":
      if (key && (key.status === "ok" || key.status === "approved")) {
        const by = key.decided_by ? ` after ${key.decided_by} approved it` : "";
        return { text: actionSentence(key) + by, sub: key.policy ?? undefined, tone: "", route: c.route };
      }
      return { text: "Fixed by the action agent", tone: "", route: "action" };
    case "awaiting_approval":
      return {
        text: key ? `A person must approve ${toolPhrase(key.tool)}` : "A person must approve a tool call",
        sub: key?.policy ?? c.escalation?.reason,
        tone: "hitl",
        route: "human",
      };
    case "escalated":
      return { text: `With a person: ${reasonWord(c.escalation?.reason_code)}`, sub: c.escalation?.reason, tone: "hitl", route: "human" };
    case "in_progress":
      return { text: `Claimed by ${who ?? "a person"}: ${reasonWord(c.escalation?.reason_code)}`, sub: c.escalation?.reason, tone: "hitl", route: "human" };
    case "resolved": {
      const step = [...d.steps].reverse().find((s) => s.agent === "human" && s.action === "resolved");
      const by = String(step?.detail?.resolved_by ?? who ?? "a person");
      return { text: `Resolved by ${by}`, sub: c.escalation ? reasonWord(c.escalation.reason_code) : undefined, tone: "", route: "human" };
    }
    case "closed":
      return { text: "Closed", tone: "", route: c.route };
    default:
      return { text: STATUS_WORD[c.status] ?? humanWords(c.status), tone: "", route: c.route };
  }
}

/** Where a case waits for a person: escalated, claimed, or a tool call held for approval. */
export const withPerson = (s: Status): boolean => s === "escalated" || s === "in_progress" || s === "awaiting_approval";

// ------------------------------------------------------------------------- customer words

export interface CustomerStep {
  key: string;
  /** Short head: "Read and sorted by our triage agent". */
  head: string;
  /** One quieter line under it, or "". */
  line: string;
  tone: Tone;
}

const CATEGORY_AS: Record<Category, string> = {
  network: "a network problem",
  data_bundles: "a data bundle problem",
  mpesa: "an M-PESA problem",
  billing: "a billing problem",
  sim_and_fraud: "a SIM or fraud matter",
  device_settings: "a device settings problem",
  roaming: "a roaming problem",
  account: "an account matter",
  other: "a general request",
};

function toolDone(call: ToolCall): string {
  switch (call.tool) {
    case "reverse_mpesa":
      return "Our action agent reversed the transfer";
    case "issue_refund":
      return "Our action agent issued the refund";
    case "recredit_bundle":
      return "Our action agent re-credited your bundle";
    case "link_incident":
      return "Linked to a known network fault our engineers are working on";
    case "reset_network_settings":
      return "Our action agent sent the settings to your phone";
    default:
      return "Our action agent fixed it";
  }
}

/**
 * What happened to a complaint, in the customer's words, one line per stage. Built from the
 * step agents, the tool calls' names and statuses, the citations and the escalation, so the
 * public view (blank details and arguments) reads the same as the staff view.
 */
export function customerSteps(d: CaseDetail): CustomerStep[] {
  const c = d.complaint;
  const out: CustomerStep[] = [];
  const agents = new Set(d.steps.map((s) => s.agent));
  out.push({ key: "intake", head: "We received your complaint", line: `Your number is kept masked on our side as ${c.customer.msisdn_masked}.`, tone: "" });
  if (agents.has("triage")) {
    out.push({ key: "triage", head: "Read and sorted by our triage agent", line: `Filed as ${CATEGORY_AS[c.category] ?? "a request"}.`, tone: "" });
  }
  const cite = c.citations[0];
  if (agents.has("resolver") && c.route === "resolver") {
    out.push({ key: "resolver", head: "Answered from our help articles", line: cite ? `From “${cite.title}”.` : "", tone: "" });
  }
  const key = keyToolCall(d.tool_calls);
  if (key) {
    if (key.status === "ok" || key.status === "approved") out.push({ key: "action", head: toolDone(key), line: key.decided_by ? `Approved by a member of our team.` : "", tone: "" });
    else if (key.status === "needs_approval") out.push({ key: "action", head: "This needs a person's approval first", line: key.policy ?? "", tone: "hitl" });
    else if (key.status === "refused" || key.status === "failed") out.push({ key: "action", head: "It could not be fixed automatically", line: key.policy ?? "", tone: "warn" });
  } else if (agents.has("action") && c.route !== "action") {
    out.push({ key: "action", head: "Our action agent looked at your account", line: "", tone: "" });
  }
  if (c.escalation) {
    const reason = c.escalation.reason.replace(/^it\s+/i, "it ");
    out.push({ key: "escalation", head: "Passed to a person in our team", line: `Because ${reason}. We will reply by ${fmtDue(c.sla_due_at)}.`, tone: "hitl" });
  }
  if (c.status === "in_progress") out.push({ key: "human", head: "A member of our team is on it now", line: "", tone: "hitl" });
  if (c.status === "resolved") out.push({ key: "human", head: "Resolved by a member of our team", line: "", tone: "" });
  return out;
}

// ----------------------------------------------------------------------------- the form

/** Kenyan mobile numbers: 07XXXXXXXX, 01XXXXXXXX, +2547…, +2541…, 2547…, 2541…; spaces and dashes allowed. */
export function msisdnProblem(raw: string): string | null {
  const s = raw.replace(/[\s-]/g, "");
  if (!s) return "Enter the phone number the problem is on.";
  if (!/^\+?\d+$/.test(s)) return "Use digits only, with an optional + at the start.";
  if (/^0[17]\d{8}$/.test(s)) return null;
  if (/^\+?254[17]\d{8}$/.test(s)) return null;
  if (/^0[17]/.test(s) || /^\+?254[17]/.test(s)) return "That number is the wrong length. A Kenyan number is 10 digits, like 0712 345 678.";
  return "Enter a Kenyan mobile number: 07…, 01…, or +2547… / +2541….";
}

export const BODY_MIN = 5;
export const BODY_MAX = 4000;

// -------------------------------------------------------------------------------- evals

export interface MetricDef {
  key: keyof EvalMetrics;
  label: string;
  /** One plain sentence, from the contract's definitions. */
  means: string;
  /** Lower is better. */
  lowerBetter?: boolean;
  /** Milliseconds, not a rate. */
  ms?: boolean;
}

export const METRIC_DEFS: Record<keyof EvalMetrics, MetricDef> = {
  resolution_rate: {
    key: "resolution_rate",
    label: "Resolution rate",
    means: "Of the cases the gold set marks resolvable, the share the desk resolved correctly without a person: the right route, and a cited article or a tool call that succeeded.",
  },
  wrong_escalation_rate: {
    key: "wrong_escalation_rate",
    label: "Wrong-escalation rate",
    means: "Of the cases the desk sent to a person, the share that did not need one.",
    lowerBetter: true,
  },
  missed_escalation_rate: {
    key: "missed_escalation_rate",
    label: "Missed-escalation rate",
    means: "Of the gold cases that need a person, the share the desk kept. The dangerous error.",
    lowerBetter: true,
  },
  safety_missed_escalation_rate: {
    key: "safety_missed_escalation_rate",
    label: "Missed escalations on safety cases",
    means: "Missed escalations among fraud or SIM swap, legal or regulator, and threat or safety cases. The gate is zero.",
    lowerBetter: true,
  },
  escalation_reason_accuracy: {
    key: "escalation_reason_accuracy",
    label: "Escalation reason accuracy",
    means: "Of the cases rightly sent to a person, the share sent for the right reason.",
  },
  containment_rate: {
    key: "containment_rate",
    label: "Containment",
    means: "Cases closed without a person, over all cases in the set.",
  },
  triage_accuracy: {
    key: "triage_accuracy",
    label: "Triage accuracy",
    means: "Cases where triage chose the category the gold set names.",
  },
  routing_accuracy: {
    key: "routing_accuracy",
    label: "Routing accuracy",
    means: "Cases that ended on the route the gold set names.",
  },
  grounded_answer_rate: {
    key: "grounded_answer_rate",
    label: "Grounded answers",
    means: "Resolver answers that cited an article in the gold set's accepted list.",
  },
  tool_accuracy: {
    key: "tool_accuracy",
    label: "Tool accuracy",
    means: "Action cases where the agent chose the gold tool, whatever the call's status.",
  },
  p50_ms: {
    key: "p50_ms",
    label: "Median time per case",
    means: "The median time the desk took from intake to reply, per case.",
    ms: true,
  },
};

export const GATE_WORD: Record<string, string> = {
  ">=": "at least",
  "<=": "at most",
  "==": "exactly",
};

/** The gate on a metric, from the report, or null. */
export const gateFor = (r: EvalReport, key: string): EvalGate | null => r.gates.find((g) => g.metric === key) ?? null;

/** The route labels of the confusion matrix, as the UI names them. */
export const confusionLabel = (r: string): string => (r === "human" ? "Person" : r === "action" ? "Action" : r === "resolver" ? "Resolver" : r);
