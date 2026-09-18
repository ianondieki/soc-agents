/**
 * Weather risk strip — normalisation and staleness (spec §7.3).
 *
 * THE OPERATIONAL POINT. Weather is *advisory*: §7.3 says plainly that no model
 * covering Kenya is finer than ~10 km, so a flag here never changes an incident
 * priority. What it can do is tell a night shift "the engineer you are about to
 * send to RFT is driving into a storm" — and that is only worth anything if the
 * reading is current. `pollers/weather.py` is fail-soft by design: when a fetch
 * fails it *keeps the last good row* and annotates it (§7.3.5, "provider down →
 * previous row kept, stale=true after valid_until"). So the row on the glass is
 * not evidence that the data is fresh. An operator reading three-hour-old rain
 * as if it were now is worse off than one reading nothing at all.
 *
 * Hence: every tile carries its own age, and the age is computed **client-side
 * from `fetched_at`**, not merely copied from the server's `stale` boolean. Three
 * independent things can rot, and each is caught here:
 *
 *   1. the poller stopped   → `fetched_at` recedes, age climbs, badge flips;
 *   2. the API stopped      → the response itself freezes, but `fetched_at` is an
 *                             absolute instant, so the age *still* climbs;
 *   3. the browser tab was left up overnight → same, for the same reason.
 *
 * A server that forgets to set `stale` cannot make this component lie. That is
 * the whole design: the badge is the point, not decoration.
 *
 * CONFIRMED vs ANTICIPATED (see the report / the comments on each field).
 * `derive_weather_risk` in `src/noc_agents/adapters/weather.py` and `staleness`
 * in `src/noc_agents/pollers/weather.py` are the producers of this block and
 * were read directly, so the field names below are taken from the backend, not
 * guessed. The *endpoint* that serves them (`GET /api/v1/signals/weather/regions`)
 * did not exist when this was written — it 404s on the demo server — so the
 * envelope around the block is the spec's §7.3.2 shape, defensively parsed.
 *
 * Nothing in this module throws. Every reader is total: an unknown field, a
 * string where a number was expected, a missing `regions` key or a completely
 * different response shape all degrade to "no data", never to an exception.
 */

import { isPlainObject } from "./hitl";
import { parseInstant } from "./time";

/* ------------------------------------------------------------------ *
 * Thresholds                                                          *
 * ------------------------------------------------------------------ */

/**
 * The poller runs every 15 minutes (`cfg.weather.poll_minutes: 15`) and stamps
 * each row `valid_until = fetched_at + ROW_VALID_FOR`, which is **one hour**
 * (`pollers/weather.py::ROW_VALID_FOR`). Both numbers were read from the
 * backend, so neither is invented here:
 *
 *  - past 20 minutes at least one poll cycle has been missed → AGEING (a quiet
 *    hint that the feed is limping, not yet a warning);
 *  - past 60 minutes the backend itself considers the row expired → STALE.
 *
 * `valid_until` from the response wins over the local hour whenever it is
 * present, so retuning `ROW_VALID_FOR` on the backend retunes the badge without
 * a frontend change.
 */
export const AGEING_AFTER_S = 20 * 60;
export const STALE_AFTER_S = 60 * 60;

/** A strip whose own fetch has not succeeded for this long says so in the header. */
export const FEED_STALLED_AFTER_MS = 3 * 60_000;

/* ------------------------------------------------------------------ *
 * Total readers                                                       *
 * ------------------------------------------------------------------ */

function num(v: unknown): number | null {
  if (typeof v === "number") return Number.isFinite(v) ? v : null;
  if (typeof v === "string" && v.trim()) {
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }
  return null;
}

function bool(v: unknown): boolean {
  if (typeof v === "boolean") return v;
  if (typeof v === "number") return v !== 0;
  if (typeof v === "string") return ["1", "true", "yes", "on"].includes(v.trim().toLowerCase());
  return false;
}

function str(v: unknown): string {
  return typeof v === "string" ? v.trim() : "";
}

function strList(v: unknown): string[] {
  if (!Array.isArray(v)) return [];
  const out: string[] = [];
  for (const item of v) {
    const s = typeof item === "string" ? item.trim() : "";
    if (s) out.push(s);
  }
  return out;
}

function own(o: Record<string, unknown>, key: string): unknown {
  return Object.prototype.hasOwnProperty.call(o, key) ? o[key] : undefined;
}

/** First key that is actually present, so a renamed field does not blank a tile. */
function pick(o: Record<string, unknown>, ...keys: string[]): unknown {
  for (const k of keys) {
    const v = own(o, k);
    if (v !== undefined && v !== null) return v;
  }
  return undefined;
}

/* ------------------------------------------------------------------ *
 * Shapes                                                              *
 * ------------------------------------------------------------------ */

/**
 * The measured precision of `storm_flag` for a region (§7.3.3): "the Wallboard
 * strip shows the measured precision beside the flag so nobody trusts a number
 * that was never checked."
 *
 * ANTICIPATED. `scripts/backtest_signals.py` does not exist yet, so no shape is
 * confirmed. Absent precision is rendered as the words "precision unmeasured",
 * which is the honest reading and the one the spec sentence is actually about.
 */
export interface Precision {
  /** 0..1. */
  value: number | null;
  /** Incidents in the backtest sample, when the producer reports one. */
  sample: number | null;
}

export interface RegionRisk {
  /** `NBI_E`, `RFT`, … — the map key, or the block's own `region_code`. */
  regionCode: string;
  /** Human label when the endpoint supplies one; otherwise the code. */
  label: string;
  stormFlag: boolean;
  floodFlag: boolean;
  /** What the API claimed. Never the only input to the badge — see `stalenessOf`. */
  serverStale: boolean;
  fetchedAt: Date | null;
  validUntil: Date | null;
  /** Server-side age at the moment the response was built, in seconds. */
  serverAgeS: number | null;
  rainMm: number | null;
  gustKmh: number | null;
  precipProbPct: number | null;
  /** `OPEN_METEO` | `MET_NORWAY` — which provider the stored row came from. */
  source: string;
  /** `storm_reasons`, e.g. "rain 24 mm/6h >= 20". Shown as a tooltip, not on the glass. */
  reasons: string[];
  /** `external_signals.last_error` — why the last fetch failed, if it did. */
  lastError: string;
  precision: Precision | null;
  capAlertIds: string[];
}

/** KMD CAP feed health (§7.3.2 `cap: {newest_sent, stale, alerts:[…]}`). */
export interface CapState {
  newestSent: Date | null;
  stale: boolean;
  alertCount: number;
}

export interface WeatherStrip {
  /**
   * False when the endpoint says the feature is off, or when it 404s / returns
   * nothing. `WEATHER_ENABLED=false` (the default) therefore means the strip
   * never renders at all, rather than rendering an empty box.
   */
  enabled: boolean;
  regions: RegionRisk[];
  cap: CapState | null;
}

export const EMPTY_STRIP: WeatherStrip = { enabled: false, regions: [], cap: null };

/* ------------------------------------------------------------------ *
 * Parsing                                                             *
 * ------------------------------------------------------------------ */

/**
 * A side map keyed by region code (the anticipated top-level `precision` block).
 * Matches on the resolved code, the original `regions` key, and case-insensitively
 * on either — a producer that writes `nbi_e` must not silently lose its number.
 */
function lookupByCode(map: unknown, regionCode: string, rawKey: string): unknown {
  if (!isPlainObject(map)) return undefined;
  const direct = own(map, regionCode) ?? (rawKey ? own(map, rawKey) : undefined);
  if (direct !== undefined) return direct;
  for (const [k, v] of Object.entries(map)) {
    if (k.toUpperCase() === regionCode) return v;
  }
  return undefined;
}

function readPrecision(raw: unknown): Precision | null {
  const v = num(raw);
  if (v !== null) {
    // A producer may report 0..1 or a percentage; both are readable, 0..1 is stored.
    return { value: v > 1 ? v / 100 : v, sample: null };
  }
  if (!isPlainObject(raw)) return null;
  const value = num(pick(raw, "precision", "value", "storm_flag", "p"));
  const sample = num(pick(raw, "sample", "n", "support", "count", "sample_size"));
  if (value === null && sample === null) return null;
  return { value: value !== null && value > 1 ? value / 100 : value, sample };
}

/**
 * One `weather_risk` block → a `RegionRisk`.
 *
 * CONFIRMED field names (read from `adapters/weather.py::derive_weather_risk`
 * and `pollers/weather.py::staleness` / `weather_risk_for_region`):
 * `rain_mm_next_6h`, `precip_prob_max_pct`, `gust_kmh_max`, `storm_flag`,
 * `storm_reasons`, `flood_flag`, `cap_alert_ids`, `source`, `fetched_at`,
 * `stale`, `age_s`, `valid_until`, `last_error`, `region_code`.
 * ANTICIPATED: `label`, `precision`.
 */
function readRegion(code: string, raw: unknown, topPrecision: unknown): RegionRisk | null {
  if (!isPlainObject(raw)) return null;
  const regionCode = (str(pick(raw, "region_code", "region", "code")) || code).toUpperCase();
  if (!regionCode) return null;

  const precision =
    readPrecision(pick(raw, "precision", "measured_precision", "storm_precision")) ??
    readPrecision(lookupByCode(topPrecision, regionCode, code));

  return {
    regionCode,
    label: str(pick(raw, "label", "region_label", "name")) || regionCode,
    stormFlag: bool(pick(raw, "storm_flag", "storm")),
    floodFlag: bool(pick(raw, "flood_flag", "flood")),
    serverStale: bool(pick(raw, "stale")),
    fetchedAt: parseInstant(pick(raw, "fetched_at", "fetchedAt")),
    validUntil: parseInstant(pick(raw, "valid_until", "validUntil")),
    serverAgeS: num(pick(raw, "age_s", "age_seconds")),
    rainMm: num(pick(raw, "rain_mm_next_6h", "rain_mm")),
    gustKmh: num(pick(raw, "gust_kmh_max", "gust_kmh")),
    precipProbPct: num(pick(raw, "precip_prob_max_pct", "precip_prob_pct")),
    source: str(pick(raw, "source")),
    reasons: strList(pick(raw, "storm_reasons", "reasons")),
    lastError: str(pick(raw, "last_error")),
    precision,
    capAlertIds: strList(pick(raw, "cap_alert_ids")),
  };
}

function readCap(raw: unknown): CapState | null {
  if (!isPlainObject(raw)) return null;
  const alerts = pick(raw, "alerts");
  const count = Array.isArray(alerts) ? alerts.length : (num(pick(raw, "alert_count", "count")) ?? 0);
  const newestSent = parseInstant(pick(raw, "newest_sent", "newestSent", "sent"));
  if (!newestSent && count === 0 && !("stale" in raw)) return null;
  return { newestSent, stale: bool(pick(raw, "stale")), alertCount: count };
}

/**
 * The §7.3.2 envelope → a `WeatherStrip`, sorted by region code.
 *
 * Accepts the documented `{regions: {CODE: block}}`, a `{regions: [block…]}`
 * list, and a bare top-level list, because the endpoint does not exist yet and a
 * frontend that only understood one of those would blank the moment the backend
 * chose another. Anything else parses to "no data".
 *
 * ORDER IS ALPHABETICAL AND FIXED, never "worst first". A six-tile strip on a
 * wall-mounted screen is read by position — an operator learns that RFT is the
 * fourth tile. Reordering it when a flag trips would trade that for a scan.
 */
export function normalizeStrip(raw: unknown): WeatherStrip {
  try {
    if (raw == null) return EMPTY_STRIP;

    let container: unknown = raw;
    let topPrecision: unknown = undefined;
    let cap: CapState | null = null;

    if (isPlainObject(raw)) {
      // An explicit `enabled: false` is honoured even if rows came with it.
      const enabled = own(raw, "enabled");
      if (enabled !== undefined && !bool(enabled)) return EMPTY_STRIP;
      topPrecision = pick(raw, "precision", "measured_precision");
      cap = readCap(pick(raw, "cap"));
      const regions = own(raw, "regions");
      if (regions !== undefined) container = regions;
    }

    const out: RegionRisk[] = [];
    if (Array.isArray(container)) {
      for (const item of container) {
        const r = readRegion("", item, topPrecision);
        if (r) out.push(r);
      }
    } else if (isPlainObject(container)) {
      for (const [code, block] of Object.entries(container)) {
        const r = readRegion(code, block, topPrecision);
        if (r) out.push(r);
      }
    }

    // One tile per region. A duplicated code would otherwise give React two
    // children with the same key; the newest reading wins.
    const byCode = new Map<string, RegionRisk>();
    for (const r of out) {
      const prev = byCode.get(r.regionCode);
      if (!prev) {
        byCode.set(r.regionCode, r);
        continue;
      }
      const a = r.fetchedAt ? r.fetchedAt.getTime() : -Infinity;
      const b = prev.fetchedAt ? prev.fetchedAt.getTime() : -Infinity;
      if (a > b) byCode.set(r.regionCode, r);
    }

    const rows = Array.from(byCode.values());
    rows.sort((a, b) => a.regionCode.localeCompare(b.regionCode));
    return { enabled: rows.length > 0, regions: rows, cap };
  } catch {
    return EMPTY_STRIP; // a shape nobody predicted must not take the wallboard down
  }
}

/* ------------------------------------------------------------------ *
 * Staleness                                                           *
 * ------------------------------------------------------------------ */

export type StaleTone = "fresh" | "ageing" | "stale";

export interface Staleness {
  tone: StaleTone;
  /** Seconds since `fetched_at`, or `null` when the row carries no usable instant. */
  ageS: number | null;
  /** Long-form explanation — a `title`, never the thing read from across the room. */
  why: string;
}

/**
 * Age in seconds, preferring the absolute `fetched_at` over the server's `age_s`.
 *
 * `age_s` was true when the response was built; `fetched_at` stays true while the
 * page sits on the wall. Using the absolute instant is what makes the badge
 * survive the API dying with the tab still open. `age_s` is the fallback for a
 * row that somehow arrives without a parseable timestamp, and it is advanced by
 * however long the response has been on screen so it does not freeze either.
 */
export function ageSecondsOf(r: RegionRisk, receivedAtMs: number, nowMs: number): number | null {
  if (r.fetchedAt) {
    const s = Math.floor((nowMs - r.fetchedAt.getTime()) / 1000);
    return s < 0 ? 0 : s; // clock skew must never print a negative age
  }
  if (r.serverAgeS !== null) {
    const held = Math.max(0, Math.floor((nowMs - receivedAtMs) / 1000));
    return Math.max(0, Math.floor(r.serverAgeS) + held);
  }
  return null;
}

/**
 * The badge decision. STALE when **any** of:
 *
 *  - the API said so (`stale: true` — the poller sets it once `valid_until` passes);
 *  - `valid_until` is in the past by this browser's clock;
 *  - the age is at or past `STALE_AFTER_S`;
 *  - the row carries no timestamp at all — an unknown age is not a fresh one,
 *    and treating it as fresh is exactly the failure this strip exists to stop.
 */
export function stalenessOf(r: RegionRisk, ageS: number | null, nowMs: number): Staleness {
  const expired = r.validUntil ? nowMs >= r.validUntil.getTime() : false;

  if (ageS === null) {
    return {
      tone: "stale",
      ageS: null,
      why: "This row carries no readable fetch time, so its age cannot be established. Treated as stale.",
    };
  }
  if (r.serverStale || expired || ageS >= STALE_AFTER_S) {
    const parts: string[] = [];
    if (r.serverStale) parts.push("the API marked this row stale");
    if (expired) parts.push("its validity window has passed");
    if (ageS >= STALE_AFTER_S) parts.push(`it was fetched ${fmtAgeLong(ageS)} ago`);
    if (r.lastError) parts.push(`last fetch error: ${r.lastError}`);
    return {
      tone: "stale",
      ageS,
      why: `Last good reading kept on the glass because ${parts.join("; ")}. Do not read it as current.`,
    };
  }
  if (ageS >= AGEING_AFTER_S) {
    return {
      tone: "ageing",
      ageS,
      why:
        `Fetched ${fmtAgeLong(ageS)} ago — the poller runs every 15 minutes, so at least one ` +
        `cycle has been missed.` + (r.lastError ? ` Last fetch error: ${r.lastError}` : ""),
    };
  }
  return { tone: "fresh", ageS, why: `Fetched ${fmtAgeLong(ageS)} ago.` };
}

/** `4m`, `1h 12m`, `3h` — short enough for a tile read from across a room. */
export function fmtAgeShort(ageS: number | null): string {
  if (ageS === null) return "AGE ?";
  if (ageS < 60) return "<1m";
  const mins = Math.floor(ageS / 60);
  if (mins < 60) return `${mins}m`;
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  return m ? `${h}h ${m}m` : `${h}h`;
}

/** The same age in prose, for tooltips. */
export function fmtAgeLong(ageS: number | null): string {
  if (ageS === null) return "an unknown time";
  if (ageS < 60) return "less than a minute";
  const mins = Math.floor(ageS / 60);
  if (mins < 60) return `${mins} minute${mins === 1 ? "" : "s"}`;
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  return m ? `${h}h ${m}m` : `${h} hour${h === 1 ? "" : "s"}`;
}

/* ------------------------------------------------------------------ *
 * Risk wording                                                        *
 * ------------------------------------------------------------------ */

export type RiskLevel = "storm" | "flood" | "clear";

/**
 * Deliberately only the backend's own two flags plus "clear". There is no
 * invented middle tier: §7.3.1 marks the thresholds UNVERIFIED and says they are
 * to be tuned from the backtest in YAML, "never in code" — a frontend-only
 * "WATCH" band would be exactly that, a threshold in code, and it could disagree
 * with the flag printed next to it. The raw millimetres and gusts are on the
 * tile, so an operator can see a 18 mm CLEAR for themselves.
 */
export function riskLevelOf(r: RegionRisk): RiskLevel {
  if (r.stormFlag) return "storm";
  if (r.floodFlag) return "flood";
  return "clear";
}

/** One word, read in well under two seconds. */
export function riskWord(level: RiskLevel): string {
  return level === "storm" ? "STORM" : level === "flood" ? "FLOOD" : "CLEAR";
}

/**
 * The measured-precision line beside a flag (§7.3.3). Returns `""` for an
 * unflagged region — a CLEAR tile has no claim that needs qualifying, and the
 * words would only add noise to a screen read at distance.
 */
export function precisionLabel(r: RegionRisk): string {
  if (!r.stormFlag && !r.floodFlag) return "";
  const p = r.precision;
  if (!p || p.value === null) return "precision unmeasured";
  const pct = Math.round(Math.max(0, Math.min(1, p.value)) * 100);
  return p.sample !== null ? `precision ${pct}% (n=${p.sample})` : `precision ${pct}%`;
}

/** Provider + reasons + error, for the tile's `title`. Nothing is hidden. */
export function tileTitle(r: RegionRisk, s: Staleness): string {
  const lines = [`${r.label} (${r.regionCode})`, s.why];
  if (r.source) lines.push(`Source: ${r.source}`);
  if (r.reasons.length) lines.push(`Flagged because: ${r.reasons.join("; ")}`);
  if (r.capAlertIds.length) lines.push(`KMD CAP alerts: ${r.capAlertIds.join(", ")}`);
  const pl = precisionLabel(r);
  if (pl) lines.push(`Backtested ${pl}.`);
  lines.push("Advisory only — weather never changes an incident priority (spec §7.3).");
  return lines.join("\n");
}
