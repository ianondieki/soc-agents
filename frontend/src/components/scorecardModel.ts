/**
 * Vendor scorecards: the pure half of the page (spec §7.6, §7.10).
 *
 * No React, no fetch, no clock and no imports. Every function takes plain data and returns plain
 * data, so the rules this screen must not get wrong can be checked by a script with no DOM:
 *
 *  - **Raw is never replaced by normalised.** `valuePair` always renders `raw_value` in the raw
 *    slot. The normalised figure goes beside it, under the server's own label ("contract-agreed
 *    regional allowance", §7.6.1). When there is no normalised value, the slot says why, in
 *    words taken from the backend's reasons.
 *  - **Each status looks like what it is.** `statusView` gives the chip, the words and the
 *    watermark. Only SHADOW has a watermark (§7.10). WITHHELD gets the gate's numbers
 *    (`gateView`) rather than a watermark, because what matters there is why.
 *  - **"Defaults, not contract" is never lost.** `termsView` reads `terms_basis` and
 *    `terms_notice` from the top level of the card, where `services/scorecard.scorecard_out`
 *    puts them.
 *
 * WHERE THE RULES COME FROM. The role sets below copy the backend, and each names its source.
 * They decide only which buttons and explanations the page shows. The server enforces the
 * rules on every call, even with `AUTH_DISABLED=true`. `/api/v1/session` reports the demo role
 * switcher, not the signed-in principal, so when the UI's guess is wrong, the server's
 * 403/404/409 is what the reader sees, in the server's words.
 */

// ------------------------------------------------------------------ vocabularies (backend copies)

/** `db/models_scorecards.SCORECARD_STATUSES`, in lifecycle order. */
export const SCORECARD_STATUSES = ["DRAFT", "SHADOW", "WITHHELD", "PUBLISHED", "FINAL"] as const;
/** `db/models_scorecards.RELEASED_STATUSES`: the only statuses a vendor may be shown. */
export const RELEASED_STATUSES: readonly string[] = ["PUBLISHED", "FINAL"];

/** `api/routers/scorecards.INTERNAL_READERS`: may see DRAFT / SHADOW / WITHHELD. */
export const INTERNAL_READERS: readonly string[] = ["duty_manager", "management", "admin"];
/**
 * `api/routers/scorecards.SCORECARD_READERS` (§9.3, the scorecards row): may read released
 * cards at all, enforced once auth is on. field_engineer and planning have no read cell and get
 * 403. msp_coordinator reads its own vendor only.
 */
export const SCORECARD_READERS: readonly string[] = [
  "noc_analyst",
  "shift_supervisor",
  "duty_manager",
  "management",
  "msp_coordinator",
  "legal",
  "admin",
];
/** `services/scorecard.REVIEWER_ROLES` / `PUBLISHER_ROLES`: shadow review, publish, finalise. */
export const PUBLISHER_ROLES: readonly string[] = ["duty_manager", "admin"];
/** `api/deps.SUPERVISORS`: may trigger `POST /scorecards/compute`. */
export const COMPUTE_ROLES: readonly string[] = ["shift_supervisor", "duty_manager", "admin"];

/** `services/scorecard.line_out` sends this on every line; kept as a fallback for older rows. */
export const NORMALISED_LABEL_DEFAULT = "contract-agreed regional allowance";
/**
 * What the normalised column means, for its header. It is NOT `raw / multiplier`: the backend
 * recomputes the line's own formula with each incident's minutes divided by THAT incident's
 * region multiplier (`services/scorecard.py`, reading 8). A percentage is never divided; each
 * incident is re-judged against its limit instead. So the normalised figure can sit above raw
 * (SLA compliance 50 % raw, 60 % normalised) and needs no single multiplier.
 */
export const NORMALISED_EXPLAINER =
  "Normalised: the line's own formula recomputed with each ticket's minutes divided by that ticket's region " +
  "multiplier. Compliance re-judges each ticket against its limit, so the figure can be above or below raw. " +
  "Bands and credits use raw. The line's formula, in its drawer, shows the exact working.";
/** `services/scorecard.PATH_SEP`: a composite `yaml_path` lists every term a line was judged against. */
export const PATH_SEP = ";";

/**
 * The dispute route (`POST /api/v1/scorecards/lines/{line_id}/dispute`) and the
 * DISPUTE_SCORECARD_LINE handling are not built (CONFORMANCE C-02). `api/routers/scorecards.py`
 * says so: "NOT here yet, on purpose". Until they exist, nobody can dispute, whatever their
 * role, and the screen says so instead of offering a form that has no endpoint behind it.
 */
export const DISPUTE_ROUTE_AVAILABLE = false;
export const DISPUTE_UNAVAILABLE_REASON =
  "Disputes are not yet available. The backend has no dispute route yet " +
  "(POST /api/v1/scorecards/lines/{line_id}/dispute, CONFORMANCE C-02), so nothing can be filed from this screen.";

// ---------------------------------------------------------------------------------- wire shapes

/** `services/scorecard.GateResult.as_json`: the card's `data_quality`. */
export type DataQuality = {
  incidents?: number;
  restored_incidents?: number;
  inferred_restores?: number;
  inferred_pct?: number | null;
  gate_threshold_pct?: number;
  passed?: boolean;
  reason?: string;
  inferred_by_source?: Record<string, number>;
  inferred_incidents?: string[];
  yaml_path?: string;
};

/** `services/scorecard.operator_discipline_counters`: about the OPERATOR, never the vendor. */
export type Discipline = {
  scc_events?: number;
  late_scc_openings?: number;
  late_scc_opening_threshold_min?: number;
  late_scc_opening_events?: {
    incident: string;
    event_id: string;
    scc_code: string;
    opening_delay_min: number;
    reversed: boolean;
  }[];
  missing_scc_with_confirmed_power?: number | null;
  missing_scc_with_confirmed_power_status?: string;
  missing_scc_with_confirmed_power_note?: string;
  note?: string;
};

export type ExcludedIncident = { incident: string; reason: string };

/** `services/scorecard.line_out`. */
export type ScorecardLine = {
  id: string;
  kpi: string;
  priority: string | null;
  raw_value: number | null;
  normalised_value: number | null;
  normalised_label?: string;
  region_multiplier_applied: number | null;
  unit: string;
  band: string;
  eligible_incidents: number;
  excluded_incidents: number;
  scc_minutes_deducted: number;
  formula: string;
  yaml_path: string;
  proposed_credit_pct: number | null;
  credit_status: string;
  dispute_task_id: string | null;
  dispute_status: string | null;
  adjusted_value: number | null;
  adjudicated_by: string | null;
  adjudication_reason: string | null;
  evidence?: Record<string, unknown>;
};

/** `services/scorecard.scorecard_out`. `lines` is present on `GET /scorecards/{id}` only. */
export type Scorecard = {
  id: string;
  operator_id: string;
  vendor_id: string;
  vendor_code: string | null;
  vendor_name: string | null;
  period: string;
  period_start: string | null;
  period_end: string | null;
  status: string;
  computed_at: string | null;
  published_at: string | null;
  dispute_window_ends_at: string | null;
  finalised_at: string | null;
  data_quality: DataQuality;
  discipline: Discipline;
  shadow_required: boolean;
  shadow_reviewed_by: string | null;
  shadow_reviewed_at: string | null;
  sla_terms_version: string;
  terms: Record<string, unknown>;
  terms_basis: string | null;
  terms_notice: string | null;
  narrative: string | null;
  narrative_ai_assisted: boolean;
  computed_by_run_id: string;
  lines?: ScorecardLine[];
};

/** `POST /scorecards/compute`. The `*_detail` and `scorecard_ids` keys go to INTERNAL_READERS only. */
export type ComputeResult = {
  ok: boolean;
  period: string;
  run_id: string;
  computed: number;
  skipped: number;
  computed_detail?: string[];
  skipped_detail?: string[];
  scorecard_ids?: string[];
};

/** `POST /scorecards/{id}/shadow-review | publish | finalise`. */
export type TransitionResult = { ok: boolean; scorecard: Scorecard };

/** `services/vendors.vendor_out` (the fields this page reads). */
export type Vendor = {
  id: string;
  code: string;
  display_name: string;
  type?: string;
  contract_ref?: string | null;
  active_from?: string;
  active_to?: string | null;
};

// ------------------------------------------------------------------------------- small helpers

function isNum(v: unknown): v is number {
  return typeof v === "number" && Number.isFinite(v);
}

function roleIn(role: string | null | undefined, set: readonly string[]): boolean {
  return set.includes((role || "").trim());
}

// ---------------------------------------------------------------------------------- statuses

export type StatusView = {
  /** The status word itself, never paraphrased. */
  label: string;
  /** Extra words for the chip, so a status is never carried by colour alone (§7.10). */
  tag: string;
  chip: string;
  /** Text for the watermark layer, or null. SHADOW only (§7.10). */
  watermark: string | null;
  released: boolean;
  /** One sentence on what the status means and who may see it. */
  summary: string;
};

/**
 * `clock` is passed by a view that knows the time and the card's `dispute_window_ends_at`.
 * Only then does a PUBLISHED summary say anything about the window, and it says it through
 * `windowWords`, the same words as the countdown tile, so the two cannot disagree. Without a
 * clock, the summary makes no claim about the window.
 */
export function statusView(
  status: string | null | undefined,
  clock?: { nowMs: number; windowEndsMs: number | null },
): StatusView {
  const s = (status || "").trim().toUpperCase();
  switch (s) {
    case "DRAFT":
      return {
        label: "DRAFT",
        tag: "INTERNAL",
        chip: "chip",
        watermark: null,
        released: false,
        summary:
          "Computed, and the data-quality gate passed. This is an internal working paper, visible only to duty managers, management and admins, until a duty manager publishes it.",
      };
    case "SHADOW":
      return {
        label: "SHADOW",
        tag: "INTERNAL ONLY",
        chip: "chip hitl",
        watermark: "SHADOW",
        released: false,
        summary:
          "This is the first period for this vendor under these terms (§7.6.2). Only duty managers, management and admins can see it. It cannot be published until a named human records a shadow review.",
      };
    case "WITHHELD":
      return {
        label: "WITHHELD",
        tag: "DATA-QUALITY GATE",
        chip: "chip danger",
        watermark: null,
        released: false,
        summary:
          "The data-quality gate failed: too many restore times were inferred rather than recorded. The card cannot be published. Fix the restore records, then recompute.",
      };
    case "PUBLISHED":
      return {
        label: "PUBLISHED",
        tag: "RELEASED",
        chip: "chip ok",
        watermark: null,
        released: true,
        summary: publishedSummary(clock),
      };
    case "FINAL":
      return {
        label: "FINAL",
        tag: "WINDOW CLOSED",
        chip: "chip ok",
        watermark: null,
        released: true,
        summary:
          "The dispute window has closed. A recompute is refused (409); a correction becomes a new correction period (§7.6.6).",
      };
    default:
      return {
        label: s || "UNKNOWN",
        tag: "UNRECOGNISED",
        chip: "chip warn",
        watermark: null,
        released: false,
        summary: "This build does not recognise this status. It is shown exactly as the server sent it.",
      };
  }
}

function publishedSummary(clock?: { nowMs: number; windowEndsMs: number | null }): string {
  const released = "A named human released this card, and the vendor may see it.";
  if (!clock) return released;
  const w = windowWords(clock.nowMs, clock.windowEndsMs);
  if (w === "closed")
    return released + " The dispute window has closed. The card stays PUBLISHED until a duty manager finalises it.";
  if (w === "no window recorded") return released + " The card records no dispute-window end.";
  return released + ` The dispute window is running (${w}).`;
}

/** Mirrors `api/routers/scorecards._visible_statuses`, for the explanatory line only. */
export function visibleStatuses(role: string | null | undefined): readonly string[] {
  return roleIn(role, INTERNAL_READERS) ? SCORECARD_STATUSES : RELEASED_STATUSES;
}

/** Why a list might look short for this role; `null` when the role sees every status. */
export function hiddenStatusesNote(role: string | null | undefined): string | null {
  if (roleIn(role, INTERNAL_READERS)) return null;
  const who = (role || "").trim() || "unknown";
  let note =
    "Draft, shadow and withheld cards are the operator's working papers, visible only to duty managers, " +
    `management and admins. Your role (${who.replace(/_/g, " ")}) is shown published and final cards only.`;
  if (who === "msp_coordinator") {
    note +=
      " Once sign-in is on, an msp_coordinator sees only their own vendor's cards. They see none until the identity provider links them to a vendor (§9.3).";
  }
  return note;
}

// -------------------------------------------------------------------------------- the numbers

/**
 * A value exactly as the server rounded it (`services/scorecard._PLACES`: 2 places, 4 for
 * availability and the repeat-fault ratio), plus its unit. Never re-rounded here: a figure
 * that goes in front of a vendor must be the figure the card stored. `null` is "n/a", the
 * backend's own word (`_fmt`).
 */
export function formatValue(value: number | null | undefined, unit: string | null | undefined): string {
  if (!isNum(value)) return "n/a";
  const n = String(value);
  const u = (unit || "").trim();
  if (u === "min") return n + " min";
  if (u === "pct") return n + " %";
  if (u === "ratio" || u === "") return n;
  return n + " " + u;
}

export type ValuePair = {
  raw: string;
  normalised: string;
  /** The server's label for the normalised figure. */
  normalisedLabel: string;
  /** Why there is no normalised figure; `null` when there is one. */
  normalisedWhy: string | null;
};

/**
 * Raw and normalised, side by side (§7.6.2: "shown beside raw, never instead of it"). The raw
 * slot is always `raw_value`, whatever the normalised value is. The reasons a normalised value
 * is absent come from `services/scorecard.py` (readings 8 and §7.6.2), not from this file.
 */
export function valuePair(line: Pick<ScorecardLine, "kpi" | "raw_value" | "normalised_value" | "unit" | "normalised_label">): ValuePair {
  const raw = formatValue(line.raw_value, line.unit);
  const label = (line.normalised_label || "").trim() || NORMALISED_LABEL_DEFAULT;
  if (isNum(line.normalised_value)) {
    return { raw, normalised: formatValue(line.normalised_value, line.unit), normalisedLabel: label, normalisedWhy: null };
  }
  let why: string;
  switch (line.kpi) {
    case "REPEAT_FAULT_RATE":
      why = "Not normalised: this KPI counts sites, and a count of sites has no regional time allowance.";
      break;
    case "NOTE_COMPLIANCE_PCT":
      why = "No separate normalised figure: the regional multiplier is already part of this KPI's note-slot formula.";
      break;
    case "AVAILABILITY_PCT":
      why = "Not normalised: §7.6.2 allows no other normalisation for availability.";
      break;
    default:
      why = isNum(line.raw_value) ? "No normalised value was computed for this line." : "Nothing was measurable on this line in this period.";
  }
  return { raw, normalised: "—", normalisedLabel: label, normalisedWhy: why };
}

/** The region multiplier the line used, or why there is not exactly one. */
export function multiplierText(value: number | null | undefined): { text: string; title: string } {
  if (isNum(value)) return { text: "× " + String(value), title: "Every measured ticket on this line shares this regional multiplier." };
  return {
    text: "—",
    title: "No single multiplier: the measured tickets span regions, none were measured, or the KPI takes none.",
  };
}

const KPI_WORDS: Record<string, { label: string; hint: string }> = {
  MTTA_MIN: { label: "MTTA", hint: "median minutes from escalation to the first vendor note" },
  ADJ_MTTR_MIN: { label: "Adjusted MTTR", hint: "median restore minutes, stop-clock minutes deducted" },
  SLA_COMPLIANCE_PCT: { label: "SLA compliance", hint: "restores within the priority's limit" },
  REPEAT_FAULT_RATE: { label: "Repeat-fault rate", hint: "sites with 2+ same-signature tickets / affected sites" },
  NOTE_COMPLIANCE_PCT: { label: "Note compliance", hint: "note slots with a vendor note" },
  AVAILABILITY_PCT: { label: "Availability", hint: "scheduled uptime less unavailable minutes" },
};

export function kpiView(kpi: string): { label: string; hint: string; code: string } {
  const known = KPI_WORDS[kpi];
  return { label: known ? known.label : kpi, hint: known ? known.hint : "", code: kpi };
}

export function priorityLabel(priority: string | null | undefined): string {
  return priority ? priority : "All priorities";
}

export function bandView(band: string | null | undefined): { label: string; chip: string; title: string } {
  const b = (band || "").trim().toUpperCase();
  if (b === "GREEN") return { label: "GREEN", chip: "chip ok", title: "At or above the green threshold." };
  if (b === "AMBER") return { label: "AMBER", chip: "chip warn", title: "Between the amber and green thresholds." };
  if (b === "RED") return { label: "RED", chip: "chip danger", title: "Below the amber threshold." };
  if (b === "NA")
    return {
      label: "No band",
      chip: "chip",
      title: "No band: none is configured for this KPI, or nothing was measurable. None is invented (reading 12).",
    };
  return { label: b || "—", chip: "chip warn", title: "A band value this build does not recognise, shown as sent." };
}

/** `null` when there is no credit on the line (`credit_status` NONE). */
export function creditView(line: Pick<ScorecardLine, "proposed_credit_pct" | "credit_status">): { label: string; chip: string; title: string } | null {
  const status = (line.credit_status || "NONE").trim().toUpperCase();
  if (status === "NONE") return null;
  const pct = isNum(line.proposed_credit_pct) ? String(line.proposed_credit_pct) + " % " : "";
  if (status === "PROPOSED")
    return {
      label: pct + "PROPOSED",
      chip: "chip warn",
      title: "A proposal only. Supply Chain / Legal accept a credit; nothing is billed or sent from this system (§7.6.2).",
    };
  if (status === "ACCEPTED") return { label: pct + "ACCEPTED", chip: "chip ok", title: "Accepted by Supply Chain / Legal." };
  if (status === "WITHDRAWN") return { label: pct + "WITHDRAWN", chip: "chip", title: "Withdrawn." };
  return { label: pct + status, chip: "chip", title: "A credit status this build does not recognise, shown as sent." };
}

/** A line's own dispute state, from the backend's columns. Always `null` until C-02 writes them. */
export function lineDisputeView(line: Pick<ScorecardLine, "dispute_status" | "adjusted_value" | "unit">): { label: string; detail: string | null } | null {
  const s = (line.dispute_status || "").trim().toUpperCase();
  if (!s) return null;
  const detail = s === "ADJUSTED" && isNum(line.adjusted_value) ? "adjusted to " + formatValue(line.adjusted_value, line.unit) : null;
  return { label: "DISPUTE " + s, detail };
}

/**
 * Whether the lines table may offer a dispute. This depends on the backend's surface alone:
 * there is no dispute route, so the answer is no for every role and every card. Encoding a
 * role or window rule for a route that does not exist would be a guess at a future backend.
 */
export function disputeAffordance(): { canDispute: boolean; reason: string } {
  return { canDispute: DISPUTE_ROUTE_AVAILABLE, reason: DISPUTE_UNAVAILABLE_REASON };
}

/** A `yaml_path` split into the terms it cites. */
export function yamlPaths(path: string | null | undefined): string[] {
  return (path || "")
    .split(PATH_SEP)
    .map((p) => p.trim())
    .filter(Boolean);
}

// ------------------------------------------------------------------------------- the evidence

/** `evidence.excluded`, keeping only well-formed rows. */
export function excludedOf(line: Pick<ScorecardLine, "evidence">): ExcludedIncident[] {
  const raw = line.evidence ? (line.evidence as Record<string, unknown>).excluded : undefined;
  if (!Array.isArray(raw)) return [];
  const out: ExcludedIncident[] = [];
  for (const row of raw) {
    if (row && typeof row === "object") {
      const r = row as Record<string, unknown>;
      if (typeof r.incident === "string" && typeof r.reason === "string") out.push({ incident: r.incident, reason: r.reason });
    }
  }
  return out;
}

/** `services/scorecard.X_*`, in words. The code is always shown beside the words. */
const EXCLUSION_WORDS: Record<string, string> = {
  CANCELLED: "cancelled ticket",
  PLANNED_MAINTENANCE: "planned maintenance",
  UNKNOWN_PRIORITY: "no P1-P4 priority",
  NOT_ESCALATED: "never escalated to the vendor",
  NO_VENDOR_NOTE: "no vendor note recorded",
  NOTE_BEFORE_ESCALATION: "vendor note earlier than the escalation (a data error)",
  UNTRUSTED_RESTORE_SOURCE: "restore time inferred, not recorded by a named human",
  NOT_RESTORED: "not restored",
  OPEN_AT_PERIOD_END_NOT_YET_BREACHED: "still open at the period end and not yet over its limit",
  RESTORED_BEFORE_START: "restore time earlier than the start (a data error)",
  NOT_SERVICE_AFFECTING: "not service-affecting",
  OUTSIDE_PERIOD: "outside the period after clipping",
};

/** `"UNTRUSTED_RESTORE_SOURCE:INFERRED"` → words plus the source that made it untrusted. */
export function exclusionWords(reason: string): string {
  const [head, ...rest] = (reason || "").split(":");
  const words = EXCLUSION_WORDS[head.trim()];
  if (!words) return reason || "—";
  const suffix = rest.join(":").trim();
  return suffix ? `${words} (source: ${suffix})` : words;
}

/** Stop-clock minutes per incident (or per site, for availability) where any were deducted. */
export function sccBreakdown(line: Pick<ScorecardLine, "evidence">): { who: string; minutes: number }[] {
  const ev = (line.evidence || {}) as Record<string, unknown>;
  const out: { who: string; minutes: number }[] = [];
  const take = (rows: unknown, key: string) => {
    if (!Array.isArray(rows)) return;
    for (const row of rows) {
      if (!row || typeof row !== "object") continue;
      const r = row as Record<string, unknown>;
      const minutes = r.scc_minutes;
      const who = r[key];
      if (isNum(minutes) && minutes > 0 && typeof who === "string") out.push({ who, minutes });
    }
  };
  take(ev.measured, "incident");
  take(ev.sites, "site_id");
  return out;
}

function cell(v: unknown): string {
  if (v == null) return "—";
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "number") return Number.isFinite(v) ? String(v) : "—";
  if (typeof v === "string") return v;
  if (Array.isArray(v)) return v.map(cell).join(", ");
  try {
    return JSON.stringify(v);
  } catch {
    return String(v);
  }
}

/** A list of flat evidence rows as a table: columns in first-seen key order, every cell a string. */
export function tabulate(rows: unknown): { columns: string[]; rows: string[][] } {
  if (!Array.isArray(rows)) return { columns: [], rows: [] };
  const objs = rows.filter((r): r is Record<string, unknown> => !!r && typeof r === "object" && !Array.isArray(r));
  const columns: string[] = [];
  for (const o of objs) for (const k of Object.keys(o)) if (!columns.includes(k)) columns.push(k);
  return { columns, rows: objs.map((o) => columns.map((c) => cell(o[c]))) };
}

/** The scalar facts beside the rows (e.g. availability's `sites_in_scope_source`). */
export function evidenceFacts(evidence: Record<string, unknown> | undefined): [string, string][] {
  if (!evidence) return [];
  const out: [string, string][] = [];
  for (const [k, v] of Object.entries(evidence)) {
    if (k === "excluded" || k === "measured" || k === "sites") continue;
    if (v != null && typeof v === "object" && !Array.isArray(v)) continue;
    out.push([k, cell(v)]);
  }
  return out;
}

/** Column header words: `adjusted_minutes` → `adjusted minutes`. */
export function columnWords(key: string): string {
  return key.replace(/_/g, " ");
}

/** Consecutive lines of one KPI together, in the server's order (`seq`). Never re-sorted. */
export function groupByKpi(lines: readonly ScorecardLine[]): { kpi: string; lines: ScorecardLine[] }[] {
  const groups: { kpi: string; lines: ScorecardLine[] }[] = [];
  for (const line of lines) {
    const last = groups[groups.length - 1];
    if (last && last.kpi === line.kpi) last.lines.push(line);
    else groups.push({ kpi: line.kpi, lines: [line] });
  }
  return groups;
}

// ------------------------------------------------------------------------------ the two gates

export type GateView = {
  /** `true` passed, `false` failed, `null` when the card does not say. */
  passed: boolean | null;
  incidents: string;
  restored: string;
  inferred: string;
  pct: string;
  threshold: string;
  /** The gate's arithmetic as one sentence, from the recorded numbers. */
  sentence: string;
  /** The server's own reason, verbatim ("" when the gate passed). */
  reason: string;
  bySource: [string, number][];
  inferredIncidents: string[];
  yamlPath: string;
};

function count(v: unknown): string {
  return isNum(v) ? String(v) : "not recorded";
}

/** §7.6.2's data-quality gate, as the card recorded it. Nothing is recomputed here. */
export function gateView(dq: DataQuality | null | undefined): GateView {
  const d = dq || {};
  const passed = typeof d.passed === "boolean" ? d.passed : null;
  const pct = isNum(d.inferred_pct) ? String(d.inferred_pct) + " %" : "n/a";
  const threshold = isNum(d.gate_threshold_pct) ? String(d.gate_threshold_pct) + " %" : "not recorded";
  let sentence: string;
  if (isNum(d.restored_incidents) && d.restored_incidents === 0) {
    sentence = "No restore times were recorded in this period, so no restore was inferred. The gate passes with nothing to check.";
  } else {
    sentence =
      `${count(d.inferred_restores)} of ${count(d.restored_incidents)} restore times (${pct}) were inferred rather than ` +
      `recorded by a named human. The limit is ${threshold}.`;
  }
  const bySource = Object.entries(d.inferred_by_source || {}).filter((e): e is [string, number] => isNum(e[1]));
  return {
    passed,
    incidents: count(d.incidents),
    restored: count(d.restored_incidents),
    inferred: count(d.inferred_restores),
    pct,
    threshold,
    sentence,
    reason: (d.reason || "").trim(),
    bySource,
    inferredIncidents: Array.isArray(d.inferred_incidents) ? d.inferred_incidents.filter((x) => typeof x === "string") : [],
    yamlPath: d.yaml_path || "scorecards.max_inferred_restore_pct",
  };
}

export type TermsView = { kind: "CONTRACT" | "DEFAULTS" | "UNKNOWN"; label: string; chip: string; text: string };

/**
 * What the card's terms were: a contract, or "defaults, not contract" (§7.6.6). If either
 * field says defaults, the card is treated as defaults. The cautious reading wins.
 */
export function termsView(card: Pick<Scorecard, "terms_basis" | "terms_notice">): TermsView {
  const basis = (card.terms_basis || "").trim().toUpperCase();
  const notice = (card.terms_notice || "").trim();
  const saysDefaults = basis === "DEFAULTS_NOT_CONTRACT" || notice.toLowerCase().startsWith("defaults, not contract");
  if (saysDefaults)
    return {
      kind: "DEFAULTS",
      label: "DEFAULTS, NOT CONTRACT",
      chip: "chip warn",
      text: notice || "defaults, not contract: no contract has been read for this vendor.",
    };
  if (basis === "CONTRACT") return { kind: "CONTRACT", label: "CONTRACT TERMS", chip: "chip", text: notice || "contract terms" };
  return {
    kind: "UNKNOWN",
    label: "TERMS BASIS NOT RECORDED",
    chip: "chip warn",
    text: notice || "The card does not record whether its terms were a contract or defaults. Treat its figures as defaults.",
  };
}

// ----------------------------------------------------------------------------------- actions

export type ActionState = { show: boolean; enabled: boolean; why: string | null };
export type CardActions = {
  roleMayAct: boolean;
  review: ActionState;
  publish: ActionState;
  finalise: ActionState;
  /** A sentence for a card with no action at all (WITHHELD, FINAL), else `null`. */
  none: string | null;
};

const ROLE_WHY = "Shadow review, publish and finalise are duty_manager / admin acts (§7.6.3).";

/**
 * Which of the three human transitions to offer on this card. These follow the service's own
 * preconditions (`record_shadow_review`, `publish_scorecard`, `finalise_scorecard`), so a
 * button is not offered when its call is certain to be refused. The server still decides, and
 * its 409 is shown verbatim when this reading is stale.
 *
 * `nowMs` and `windowEndsMs` are passed in, so this function reads no clock.
 */
export function cardActions(
  card: Pick<Scorecard, "status" | "shadow_required" | "shadow_reviewed_by">,
  role: string | null | undefined,
  nowMs: number,
  windowEndsMs: number | null,
): CardActions {
  const may = roleIn(role, PUBLISHER_ROLES);
  const status = (card.status || "").trim().toUpperCase();
  const reviewed = !!(card.shadow_reviewed_by || "").trim();
  const off: ActionState = { show: false, enabled: false, why: null };
  const gated = (show: boolean, blockedWhy: string | null): ActionState =>
    !show ? off : { show: true, enabled: may && !blockedWhy, why: !may ? ROLE_WHY : blockedWhy };

  const review = gated(status === "SHADOW" && !reviewed, null);
  const needsReview = (status === "SHADOW" || !!card.shadow_required) && !reviewed;
  const publish = gated(
    status === "DRAFT" || status === "SHADOW",
    needsReview ? "This is a first period: a named human must record a shadow review before it can be published." : null,
  );
  let finaliseWhy: string | null = null;
  if (status === "PUBLISHED") {
    if (windowEndsMs == null) finaliseWhy = "The card records no dispute-window end.";
    else if (nowMs < windowEndsMs) finaliseWhy = "The dispute window is still open.";
  }
  const finalise = gated(status === "PUBLISHED", finaliseWhy);

  let none: string | null = null;
  if (status === "WITHHELD")
    none = "A WITHHELD card cannot be published or reviewed. A shift supervisor records the real restore times, then the period is recomputed.";
  else if (status === "FINAL") none = "A FINAL card takes no further action here. A correction is a new correction period (§7.6.6).";

  return { roleMayAct: may, review, publish, finalise, none };
}

/**
 * The relative half of the dispute-window countdown. The page always shows the absolute EAT
 * instant beside it (§7.10: "every countdown shows the absolute EAT deadline next to the
 * remaining time"). Whole hours, rounded down. The clock is passed in.
 */
export function windowWords(nowMs: number, endMs: number | null): string {
  if (endMs == null || !Number.isFinite(endMs)) return "no window recorded";
  const left = endMs - nowMs;
  if (left <= 0) return "closed";
  const hours = Math.floor(left / 3_600_000);
  const days = Math.floor(hours / 24);
  const rest = hours % 24;
  if (days > 0) return `open, ${days} d ${rest} h left`;
  if (hours > 0) return `open, ${hours} h left`;
  return "open, under 1 h left";
}

export function mayCompute(role: string | null | undefined): boolean {
  return roleIn(role, COMPUTE_ROLES);
}

// ----------------------------------------------------------------------------- query strings

/** `YYYY-MM` with a real month. The server re-validates (400 for anything else). */
export function isPeriod(value: string | null | undefined): boolean {
  const m = /^(\d{4})-(\d{2})$/.exec((value || "").trim());
  if (!m) return false;
  const month = Number(m[2]);
  return month >= 1 && month <= 12;
}

function query(params: Record<string, string | null | undefined>): string {
  const parts: string[] = [];
  for (const [k, v] of Object.entries(params)) {
    const t = (v || "").trim();
    if (t) parts.push(encodeURIComponent(k) + "=" + encodeURIComponent(t));
  }
  return parts.length ? "?" + parts.join("&") : "";
}

/** `GET /scorecards?vendor=&period=&status=`. Empty filters and "ALL" are left out. */
export function listQuery(f: { vendor?: string | null; period?: string | null; status?: string | null }): string {
  return query({
    vendor: f.vendor,
    period: isPeriod(f.period) ? f.period : null,
    status: f.status && f.status.toUpperCase() !== "ALL" ? f.status : null,
  });
}

/** `POST /scorecards/compute?period=&vendor=`. No period means the last ended month (server default). */
export function computeQuery(f: { period?: string | null; vendor?: string | null }): string {
  return query({ period: isPeriod(f.period) ? f.period : null, vendor: f.vendor });
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** `"2026-08"` → `"Aug 2026"`. An EAT calendar month; anything else is returned as-is. */
export function periodWords(period: string | null | undefined): string {
  const p = (period || "").trim();
  if (!isPeriod(p)) return p || "—";
  return MONTHS[Number(p.slice(5, 7)) - 1] + " " + p.slice(0, 4);
}

// --------------------------------------------------------------------------------- failures

export type FailureView = {
  kind: "off" | "role" | "signin" | "notfound" | "state" | "input" | "unavailable" | "error";
  title: string;
  body: string;
};

export type FailureWhere = "list" | "detail" | "action" | "compute";

/**
 * A 409 from the scorecard routes has several causes, and the page must not name the wrong one.
 * `api/routers/scorecards.py` maps three different failures to 409: a service refusal
 * (`ScorecardStateError`, including the gate), a mapper-guard refusal (`ScorecardEvidenceError`)
 * and a write the table itself rejected (`IntegrityError` -> "the card could not be written: ...").
 *
 * Each rule matches a SHORT, stable fragment of the server's sentence -- the part that states the
 * rule, not the names, ids or counts around it -- and says only what that sentence says. An
 * unmatched 409 gets a headline that asserts nothing, and the page prints the server's sentence
 * underneath either way. A new or reworded backend message therefore degrades to "the server
 * refused; here is what it said", never to a confident wrong cause.
 */
const CONFLICT_RULES: { match: RegExp; title: string; body: string }[] = [
  {
    // services/scorecard.compute_scorecard, on a PUBLISHED/FINAL card
    match: /cannot be recomputed/i,
    title: "Already released",
    body: "A card for that period is PUBLISHED or FINAL, so it is not recomputed. A correction goes through a dispute or a correction period (§7.6.6).",
  },
  {
    // the router's IntegrityError branch: a CHECK or UNIQUE refused the row
    match: /could not be written/i,
    title: "The table refused the write",
    body: "A database constraint rejected the card, so nothing was saved and the transaction was rolled back. The constraint is named below; a recompute fails the same way until the cause is fixed.",
  },
  {
    // db/models_scorecards: evidence written outside compute_scorecard's scope
    match: /outside a computation/i,
    title: "Refused by a write guard",
    body: "A card's figures are written by a computation and by nothing else, so the write was refused and nothing changed. Recompute the card.",
  },
  {
    match: /evidence is frozen|published figure is frozen|cannot change on a card that is/i,
    title: "Released evidence is frozen",
    body: "The figures on a released card cannot change. Nothing was changed; a correction goes through a dispute (§7.6.6).",
  },
  {
    match: /contradicts the table|disagrees with the table|no earlier released card exists/i,
    title: "The card disagrees with the table",
    body: "The card's shadow-review flag does not match what the table says about earlier periods, so the server refused. Recompute the card.",
  },
  {
    match: /is not a scorecard run|cannot be inserted as|never deleted|only move from there/i,
    title: "Refused by a write guard",
    body: "A guard refused this change to the card's lifecycle, and nothing was changed. The server's sentence below says which rule.",
  },
  {
    // services/scorecard.publish_scorecard, ScorecardGateError
    match: /^WITHHELD|data-quality gate/i,
    title: "Withheld by the data-quality gate",
    body: "The card cannot be released while the gate fails. A shift supervisor records the real restore times, then the period is recomputed.",
  },
  {
    match: /must record a shadow review|first period for this vendor/i,
    title: "Shadow review required",
    body: "A first period under these terms needs a named human to record a shadow review before it can be published.",
  },
  {
    match: /only a SHADOW card takes a shadow review|already shadow-reviewed/i,
    title: "Not open for review",
    body: "This card is not a SHADOW card awaiting its first review.",
  },
  { match: /scorecard is already/i, title: "Already released", body: "This card has already been released." },
  {
    match: /only a PUBLISHED card can be finalised/i,
    title: "Not PUBLISHED",
    body: "Only a PUBLISHED card can be finalised.",
  },
  {
    match: /dispute window is still open/i,
    title: "The dispute window is still open",
    body: "A card is finalised only after its dispute window has closed.",
  },
  {
    match: /OPEN dispute/i,
    title: "A dispute is still open",
    body: "A line on this card has an OPEN dispute. It is adjudicated before the card can be finalised.",
  },
];

/** The rule whose fragment the server's sentence contains, or `null` when none does. */
export function conflictRule(detail: string): { title: string; body: string } | null {
  const text = (detail || "").trim();
  if (!text) return null;
  for (const rule of CONFLICT_RULES) if (rule.match.test(text)) return { title: rule.title, body: rule.body };
  return null;
}

/**
 * An HTTP status, read for this surface. The server's own words are always shown beside it;
 * this supplies the headline. A 403 or 404 is never shown as a crash or a blank page.
 *
 * `where`:
 * - `list`: the page's first read. A 404 there means the lane flag is off.
 * - `detail`: one card. A 404 means the card is not found, or is not visible to this role.
 * - `action`: shadow review, publish or finalise on one card.
 * - `compute`: `POST /scorecards/compute`. No card is involved, so a 404 means no vendor with
 *   terms in force for that month (or the lane is off), never "no such scorecard".
 *
 * `detail` is the server's sentence (`lib/apiError.detailOf`), and it decides two headlines:
 * - a 503 is "Terms unavailable" only when compute's own terms check produced it
 *   (`sla_terms unavailable: ...`); any other 503, such as sign-in on with no
 *   `NOC_SESSION_SECRET`, gets a neutral headline;
 * - a 409 is classified by `conflictRule`, because the routes map a service refusal, a
 *   write-guard refusal and a rejected write all to 409.
 *
 * When the sentence matches no rule, the headline asserts no cause. The caller prints the
 * server's sentence beside whatever this returns.
 */
export function failureView(status: number | null, where: FailureWhere, detail = ""): FailureView {
  const compute = where === "compute";
  if (status === 401) return { kind: "signin", title: "Sign in required", body: "Sign in to use vendor scorecards." };
  if (status === 403) {
    let body: string;
    if (compute) body = "Computing a period is for shift_supervisor, duty_manager and admin.";
    else if (where === "action") body = "Your role may not take this action on a scorecard.";
    else
      body =
        "Vendor scorecards can be read by noc_analyst, shift_supervisor, duty_manager, management, msp_coordinator " +
        "(own vendor only), legal and admin (§9.3). field_engineer and planning cannot read them.";
    return { kind: "role", title: "Not available to your role", body };
  }
  if (status === 404) {
    if (where === "list")
      return {
        kind: "off",
        title: "Vendor scorecards are off",
        body: "Vendor scorecards are not enabled on this deployment. With SCORECARDS_ENABLED off, every /scorecards route answers 404 by design.",
      };
    if (compute)
      return {
        kind: "notfound",
        title: "Nothing to compute",
        body:
          "No vendor with terms in force for that month matched (an unknown vendor, or a month outside its contract dates), " +
          "or the scorecards lane is off. Nothing was computed.",
      };
    return {
      kind: "notfound",
      title: "Not found",
      body:
        "No such scorecard, or none your role may see. The server answers 404, never 403, for another operator's card and for an unreleased card this role may not see, so it never confirms that a card exists.",
    };
  }
  if (status === 409) {
    const known = conflictRule(detail);
    if (known) return { kind: "state", ...known };
    return {
      kind: "state",
      title: "Refused",
      body: detail
        ? "The server refused this. Its own sentence below says why."
        : "The server refused this and gave no reason.",
    };
  }
  if (status === 400 || status === 422)
    return {
      kind: "input",
      title: "Rejected",
      body: compute ? "The period is malformed or has not ended yet." : "The server rejected the request as sent.",
    };
  if (status === 503) {
    if (compute && /^\s*sla_terms unavailable/i.test(detail))
      return { kind: "unavailable", title: "Terms unavailable", body: "The SLA terms file is missing or unversioned, so nothing was computed." };
    return { kind: "unavailable", title: "Service unavailable", body: "The server could not serve this request. Its own sentence below names the cause." };
  }
  return { kind: "error", title: "Could not load", body: "The request failed. The rest of the page is unaffected." };
}

// -------------------------------------------------------------------------------- watermark

function xmlEscape(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&apos;");
}

/**
 * A CSS `background-image` value that tiles `text` diagonally across whatever it covers, so a
 * SHADOW card is marked from its first line to its twenty-second, however tall the page gets.
 * Decorative only: the page also says SHADOW in words.
 */
export function watermarkImage(text: string): string {
  const t = xmlEscape(text);
  const svg =
    "<svg xmlns='http://www.w3.org/2000/svg' width='460' height='260'>" +
    "<text x='230' y='130' text-anchor='middle' dominant-baseline='middle' transform='rotate(-20 230 130)' " +
    "font-family='IBM Plex Sans, system-ui, sans-serif' font-size='64' font-weight='800' letter-spacing='10' " +
    "fill='rgba(196,161,255,0.11)'>" +
    t +
    "</text></svg>";
  return 'url("data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg) + '")';
}
