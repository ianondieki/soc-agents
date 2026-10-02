/**
 * HITL approval-card model — pure functions, no React, no fetch, no throw.
 *
 * WHY THIS FILE EXISTS
 * --------------------
 * A supervisor approving a P1 broadcast at 03:00 has to see *what actually
 * leaves* before they click. The old inbox card showed `proposed_payload.sms`
 * and nothing else, so the email — the one that reaches MSP leads, the RNIO and
 * the exec list — was approved unread. That is how wrong text reaches
 * customers. Everything here exists to turn one HITL task into "the renderings,
 * side by side, with the facts that justify the decision".
 *
 * WHAT THE API RETURNS TODAY (verified against `main.py:hitl_pending` and
 * `agents/hitl.py`, not assumed):
 *
 *   { id, incident_id, incident_number, priority, site_id, task_type, status,
 *     claimed_by, created_at, proposed_payload }
 *
 * and for `APPROVE_BROADCAST` the payload is exactly
 *
 *   { priority, sms, email, audiences: [...], assignee }
 *
 * where `sms` / `email` are the plain strings from `services/composition.py`
 * (`compose_sms` / `compose_email`). `GENERIC` (the worklog-monitor escalation)
 * sends a completely different shape: `{ reason, detail, suggested_action,
 * assignee }`. So even *today* two payload shapes are in flight, and the card
 * must not assume either.
 *
 * WHAT WE ANTICIPATE (spec §6.5 / §8 Phase 2 — NOT on the wire yet):
 *   - `proposed_payload = {sms, email, whatsapp, inapp, envelope}` — the
 *     per-channel renderings plus the canonical envelope.
 *   - each channel may arrive as an object instead of a string, carrying
 *     `subject`, `segments`, `encoding`, `audience`, `language`,
 *     `language_fallback`, `template_key`, `params`.
 *   - an explicit `rendered` / `channels` container holding every channel.
 *
 * The extractor below reads the anticipated shape first and falls back to the
 * strings that exist today. Nothing here requires a backend change to work, and
 * nothing here breaks when the backend change lands.
 *
 * SAFETY CONTRACT: every exported function tolerates `null`, `undefined`, a
 * string where an object was expected, an array, a number, and a task type this
 * build has never heard of. None of them throws. The inbox must never blank.
 */

import { audienceWord, humanEnum } from "./agents";
import { detailOf, statusOf } from "./apiError";
import { parseInstant } from "./time";

/* ------------------------------------------------------------------ *
 * Small, boring coercions. Used everywhere below; each one is total.  *
 * ------------------------------------------------------------------ */

export function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** Render any JSON value as text. Never throws, never returns `undefined`. */
export function asText(v: unknown): string {
  if (v == null) return "";
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  if (Array.isArray(v)) {
    if (v.every((x) => typeof x === "string" || typeof x === "number")) return v.join(", ");
  }
  try {
    return JSON.stringify(v, null, 2) ?? "";
  } catch {
    return "(unreadable value)"; // a getter that throws, a cycle, a bad toJSON
  }
}

/** First argument that is a non-empty string, else `""`. */
function firstString(...vals: unknown[]): string {
  for (const v of vals) {
    if (typeof v === "string" && v.trim()) return v;
  }
  return "";
}

/** First argument that is a finite number, else `null`. */
function firstNumber(...vals: unknown[]): number | null {
  for (const v of vals) {
    if (typeof v === "number" && Number.isFinite(v)) return v;
  }
  return null;
}

/**
 * Own-property lookup. The `hasOwnProperty` guard is load-bearing, exactly as in
 * `realtime/renderers.ts`: a bare `obj[key]` for the key `"__proto__"` returns
 * `Object.prototype`, and `"toString"` / `"constructor"` return functions. Any
 * of those would then be read as data and blow up a `.trim()` downstream.
 */
function own(obj: unknown, key: string): unknown {
  if (!isPlainObject(obj)) return undefined;
  if (!Object.prototype.hasOwnProperty.call(obj, key)) return undefined;
  return obj[key];
}

/** Trim to a sane length so a hostile/garbage string cannot wreck the layout. */
function clamp(s: string, max: number): string {
  const clean = s.replace(/[\x00-\x08\x0b\x0c\x0e-\x1f]/g, " ");
  return clean.length > max ? `${clean.slice(0, max)}…` : clean;
}

/* ------------------------------------------------------------------ *
 * Task types                                                          *
 * ------------------------------------------------------------------ */

export interface TaskTypeSpec {
  /** Human label for the card header. */
  readonly label: string;
  /** One line: what clicking Approve actually causes. */
  readonly effect: string;
  /** What the approver is being asked to check before clicking. */
  readonly check: string;
  /** Does this type carry per-channel renderings worth showing side by side? */
  readonly channels: boolean;
  /**
   * §6.5: task types introduced by v2 (`APPROVE_PRIORITY` onward) have no legacy
   * callers, so their approve handlers require a non-empty reason
   * unconditionally. The card enforces that in the UI for those types whatever
   * `HITL_APPROVE_REASON_REQUIRED` is set to on the server.
   */
  readonly reasonRequired: boolean;
  /** False for the fallback spec — the card says so out loud. */
  readonly known: boolean;
}

const BROADCAST: TaskTypeSpec = {
  label: "Broadcast approval",
  effect: "Approving releases the SMS and email for sending, exactly as shown below.",
  check: "Once approved, it reaches everyone under Goes to and cannot be recalled.",
  channels: true,
  // The four legacy approve calls send `{resolved_by}` alone and must keep
  // working (§2.1 R6), so the server does not require a reason for this type by
  // default. The UI requirement is a separate, one-line switch in ApprovalCard.
  reasonRequired: false,
  known: true,
};

/**
 * Type → card behaviour.
 *
 * `APPROVE_BROADCAST` and `GENERIC` are produced today (`agents/hitl.py` and
 * `services/worklog_monitor.py:ESCALATION_TASK_TYPE = "GENERIC"`). Everything
 * else is declared up front so the first task that ever carries one renders a
 * real card instead of falling through to the raw-payload fallback.
 */
export const TASK_TYPES: Readonly<Record<string, TaskTypeSpec>> = {
  APPROVE_BROADCAST: BROADCAST,
  APPROVE_PRIORITY: {
    label: "Priority change",
    effect: "Approving re-prices the ticket; SLA due times are recomputed from the new priority.",
    check: "Does the impact below justify the proposed priority? A P1 wakes people up.",
    channels: false,
    reasonRequired: true,
    known: true,
  },
  APPROVE_ASSIGNMENT: {
    label: "Assignment change",
    effect: "Approving moves the ticket to a different owner or vendor.",
    check: "Is the proposed owner the vendor that actually holds this site?",
    channels: false,
    reasonRequired: true,
    known: true,
  },
  APPROVE_HANDOVER: {
    label: "Shift handover",
    effect: "Approving lets the handover pack leave the NOC and closes the outgoing shift.",
    check: "Every open P1/P2 accounted for, and nothing in the pack that should not be shared.",
    channels: true,
    reasonRequired: true,
    known: true,
  },
  APPROVE_EXEC_BRIEF: {
    label: "Exec brief",
    effect: "Approving sends the exec brief to the management distribution list.",
    check: "Numbers and next-update time correct; no unconfirmed root cause stated as fact.",
    channels: true,
    reasonRequired: true,
    known: true,
  },
  GENERIC: {
    label: "Escalation",
    effect: "Records the decision only — nothing is transmitted externally.",
    check: "Is the escalation real, and who is picking it up?",
    channels: false,
    reasonRequired: false,
    known: true,
  },
  // §7.1.2 gates for write-capable MCP cards. Unreachable until such a card is
  // connected, but a mapped row costs nothing and beats the fallback.
  APPROVE_TICKET_SYNC: {
    label: "Ticket sync (external system)",
    effect: "Approving lets an agent write this ticket into the external ticketing system.",
    check: "The fields below are what the external system will receive.",
    channels: false,
    reasonRequired: true,
    known: true,
  },
  APPROVE_PAGE: {
    label: "Page a human",
    effect: "Approving pages the named on-call person.",
    check: "Right person, right hour — a page at 03:00 is a real cost.",
    channels: false,
    reasonRequired: true,
    known: true,
  },
  APPROVE_LEDGER_SYNC: {
    label: "Ledger sync",
    effect: "Approving writes the shift ledger to the external store.",
    check: "The ledger rows below are final once written.",
    channels: false,
    reasonRequired: true,
    known: true,
  },
};

/**
 * A task type this build has never seen. Deliberately *permissive*: it still
 * tries the channel extraction (a future type may well carry renderings), lists every
 * payload field underneath and opens the raw payload, so an unknown type degrades to
 * "everything the backend sent, unstyled but readable" rather than to a blank.
 * A reason is required because nobody can tell you what you just approved.
 */
export const FALLBACK_TASK_SPEC: TaskTypeSpec = {
  label: "Approval",
  effect: "This build does not recognise this kind of card; approve only if you know what it does.",
  check: "Every field the backend sent is shown below, unchanged.",
  channels: true,
  reasonRequired: true,
  known: false,
};

/** Never throws; always returns a usable spec. */
export function specFor(taskType: unknown): TaskTypeSpec {
  if (typeof taskType !== "string" || !taskType) return FALLBACK_TASK_SPEC;
  if (!Object.prototype.hasOwnProperty.call(TASK_TYPES, taskType)) return FALLBACK_TASK_SPEC;
  const spec = TASK_TYPES[taskType];
  return spec && typeof spec.label === "string" ? spec : FALLBACK_TASK_SPEC;
}

export function isKnownTaskType(taskType: unknown): boolean {
  return typeof taskType === "string" && Object.prototype.hasOwnProperty.call(TASK_TYPES, taskType);
}

/** `APPROVE_POWER_NOTICE` → `Approve power notice`. Used for unknown types. */
export function humanizeType(taskType: unknown): string {
  if (typeof taskType !== "string" || !taskType.trim()) return "Unknown card";
  const words = clamp(taskType.trim(), 48).replace(/[_\-.]+/g, " ").toLowerCase();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** Header label: the mapped label, or the humanised raw type for unknowns. */
export function labelFor(taskType: unknown): string {
  const spec = specFor(taskType);
  return spec.known ? spec.label : humanizeType(taskType);
}

/* ------------------------------------------------------------------ *
 * SMS segmentation (GSM 03.38)                                        *
 * ------------------------------------------------------------------ */

// GSM-7 basic table. Each of these costs one septet.
const GSM7_BASIC = new Set(
  "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
);
// Basic-table extension: ESC + char, so two septets each.
const GSM7_EXT = new Set("^{}\\[~]|€\f");

export interface SmsMetrics {
  encoding: "GSM-7" | "UCS-2";
  /** Septets for GSM-7, UTF-16 code units for UCS-2. */
  units: number;
  segments: number;
  /**
   * True when this was computed in the browser rather than read off the
   * payload. The card labels it, because `services/gsm7.py` is the authority:
   * it transliterates first, and its `_pack` also models the rule that an
   * ESC pair may not straddle a segment boundary — which this does not. The
   * two can therefore differ by one segment on a text full of `{}[]€`.
   */
  estimated: boolean;
  /**
   * The first character that forced UCS-2, when one did. Naming it is the whole
   * point: "142 chars, 3 segments" makes a supervisor shrug, but "UCS-2 forced
   * by '—' U+2014" tells them a template needs fixing.
   */
  offender?: { char: string; codepoint: string; count: number };
}

function codepointOf(ch: string): string {
  const cp = ch.codePointAt(0);
  if (cp == null) return "";
  return `U+${cp.toString(16).toUpperCase().padStart(4, "0")}`;
}

/**
 * Segment count for a draft SMS.
 *
 * GSM-7: 160 septets in a single message, 153 per part once concatenated (the
 * UDH eats 7). UCS-2: 70 units single, 67 per part. A non-BMP character (an
 * emoji) is a surrogate pair and therefore costs two units — counting
 * `string.length` is exactly right here, and `[...string].length` would be
 * wrong.
 *
 * Kiswahili in Latin script stays GSM-7 (§6.5), so a Kiswahili draft that
 * suddenly reports UCS-2 means a stray curly quote or dash has crept in — worth
 * seeing before you press Approve, because it doubles the bill and can truncate.
 */
export function smsMetrics(text: unknown): SmsMetrics {
  const s = typeof text === "string" ? text : "";
  if (!s) return { encoding: "GSM-7", units: 0, segments: 0, estimated: true };

  let septets = 0;
  let bad = "";
  for (const ch of s) {
    if (GSM7_BASIC.has(ch)) septets += 1;
    else if (GSM7_EXT.has(ch)) septets += 2;
    else {
      bad = ch;
      break;
    }
  }
  if (!bad) {
    const segments = septets <= 160 ? 1 : Math.ceil(septets / 153);
    return { encoding: "GSM-7", units: septets, segments, estimated: true };
  }
  const units = s.length;
  const segments = units <= 70 ? 1 : Math.ceil(units / 67);
  let count = 0;
  for (const ch of s) if (ch === bad) count += 1;
  return {
    encoding: "UCS-2",
    units,
    segments,
    estimated: true,
    offender: { char: bad, codepoint: codepointOf(bad), count },
  };
}

/* ------------------------------------------------------------------ *
 * Channel extraction                                                  *
 * ------------------------------------------------------------------ */

export interface RenderedChannel {
  key: string;
  label: string;
  /** What the recipient reads. `""` when the backend sent nothing for it. */
  text: string;
  /** Email subject / WhatsApp template key / in-app title. */
  heading: string;
  /** Segment count, encoding, audience, language: short facts beside the label, one span each. */
  meta: string[];
  /** False when the channel key was present but carried no text. */
  present: boolean;
}

const CHANNEL_LABELS: Readonly<Record<string, string>> = {
  sms: "SMS",
  email: "Email",
  whatsapp: "WhatsApp",
  inapp: "In-app",
  in_app: "In-app",
  ledger: "Ledger entry",
  voice: "Voice",
  ussd: "USSD",
};

/** Order the NOC reads them in: shortest and most irreversible first. */
const CHANNEL_ORDER = ["sms", "email", "whatsapp", "inapp", "in_app", "ledger", "voice", "ussd"];

/** Flat payload keys that are channels today. Anything else flat is a fact. */
const FLAT_CHANNEL_KEYS = ["sms", "email", "whatsapp", "inapp", "in_app"];

/**
 * Top-level payload keys the card shows elsewhere, so the field list does not
 * repeat them.
 *
 * `ledger` is deliberately NOT here even though it is in `CHANNEL_ORDER`: a flat
 * top-level `ledger` key is not a rendering today, and skipping it would hide a
 * field the backend sent. Inside an explicit `rendered` / `channels` container
 * it still gets its own column. The rule this encodes: when in doubt, show it.
 */
export const HANDLED_PAYLOAD_KEYS = new Set([
  ...FLAT_CHANNEL_KEYS,
  "rendered",
  "renderings",
  "channels",
  "envelope",
]);

function channelLabel(key: string): string {
  if (Object.prototype.hasOwnProperty.call(CHANNEL_LABELS, key)) return CHANNEL_LABELS[key];
  return humanizeType(key);
}

/**
 * `compose_email` returns `"Subject: …\n\n<body>"` — one string, subject inline.
 * Splitting it is what lets the email column show a real subject line above the
 * body instead of burying it in the first row of a scroll box. If the shape is
 * anything else the whole string stays as the body.
 */
export function splitEmail(raw: string): { subject: string; body: string } {
  const m = /^\s*Subject:[ \t]*(.*)(?:\r?\n)([\s\S]*)$/.exec(raw);
  if (!m) return { subject: "", body: raw };
  return { subject: m[1].trim(), body: m[2].replace(/^\s*\r?\n/, "") };
}

interface ChannelValue {
  text: string;
  heading: string;
  meta: Record<string, unknown>;
}

function readChannelValue(value: unknown): ChannelValue {
  if (typeof value === "string") return { text: value, heading: "", meta: {} };
  if (isPlainObject(value)) {
    return {
      text: firstString(value.text, value.body, value.message, value.content, value.rendered),
      heading: firstString(value.subject, value.title, value.template_key, value.template),
      meta: value,
    };
  }
  if (value == null) return { text: "", heading: "", meta: {} };
  return { text: asText(value), heading: "", meta: {} };
}

/** One plain fact per entry; the card prints them as separate muted spans, never joined. */
function channelMeta(key: string, v: ChannelValue): string[] {
  const chips: string[] = [];
  const m = v.meta;

  if (key === "sms" && v.text) {
    // Prefer the backend's own numbers the moment Phase 2 starts sending them:
    // `services/gsm7.py` transliterates first and models ESC-pair packing, so
    // its answer is authoritative. Key names below are that module's
    // `SmsCost` / `Offender` fields verbatim.
    const segs = firstNumber(own(m, "segments"), own(m, "segment_count"));
    const enc = firstString(own(m, "encoding"));
    if (segs != null || enc) {
      if (segs != null) chips.push(`${segs} segment${segs === 1 ? "" : "s"}`);
      if (enc) chips.push(enc);
      const units = firstNumber(own(m, "units"));
      if (units != null) chips.push(`${units} units`);
      const remaining = firstNumber(own(m, "remaining"));
      if (remaining != null) chips.push(`${remaining} spare in last segment`);
      const offenders = own(m, "offenders");
      if (Array.isArray(offenders) && offenders.length > 0) {
        const first = offenders[0];
        const ch = isPlainObject(first) ? firstString(first.char) : "";
        const cp = isPlainObject(first) ? firstString(first.codepoint) : "";
        const more = offenders.length > 1 ? ` +${offenders.length - 1} more` : "";
        chips.push(`forced by ${ch ? `"${ch}" ` : ""}${cp}${more}`.trim());
      }
    } else {
      const est = smsMetrics(v.text);
      chips.push(`${est.segments} segment${est.segments === 1 ? "" : "s"} (est.)`);
      chips.push(est.encoding);
      chips.push(`${est.units} chars`);
      if (est.offender) {
        const { char, codepoint, count } = est.offender;
        chips.push(`forced by "${char}" ${codepoint}${count > 1 ? ` ×${count}` : ""}`);
      }
    }
  }

  const audience = own(m, "audience") ?? own(m, "audiences");
  if (audience != null) {
    const words = Array.isArray(audience)
      ? audience.map((x) => (typeof x === "string" ? audienceWord(x) : asText(x))).join(", ")
      : typeof audience === "string"
        ? audienceWord(audience)
        : asText(audience);
    const a = clamp(words, 60);
    if (a) chips.push(a);
  }

  const to = own(m, "to") ?? own(m, "recipients");
  if (Array.isArray(to)) chips.push(`${to.length} recipient${to.length === 1 ? "" : "s"}`);
  else if (typeof to === "string" && to.trim()) chips.push(clamp(to, 48));

  const lang = firstString(own(m, "language"));
  if (lang) chips.push(lang);
  const fallback = firstString(own(m, "language_fallback"));
  // §6.5: an unapproved `sw` template silently renders `en`. The approver has to
  // be told, otherwise a Kiswahili audience is quietly served English.
  if (fallback) chips.push(`language falls back to ${fallback}`);

  const params = own(m, "params");
  if (Array.isArray(params)) chips.push(`${params.length} param${params.length === 1 ? "" : "s"}`);
  else if (isPlainObject(params)) {
    const n = Object.keys(params).length;
    chips.push(`${n} param${n === 1 ? "" : "s"}`);
  }

  return chips;
}

/**
 * Where the channel text came from.
 *
 * `envelope` is the tell: §6.5 has the Phase 2 payload as
 * `{sms, email, whatsapp, inapp, envelope}`, so a payload carrying an envelope
 * came out of the renderer pipeline, and one without it is the v1 draft composed
 * by `services/composition.py`. The card prints this verbatim so nobody reads a
 * v1 draft as a final rendering.
 */
export function renderingSource(payload: unknown): "envelope" | "draft" | "none" {
  if (!isPlainObject(payload)) return "none";
  if (isPlainObject(own(payload, "envelope"))) return "envelope";
  return "draft";
}

/**
 * Every channel rendering in a payload, in reading order.
 *
 * Lookup order — anticipated container first, today's flat strings last:
 *   1. `payload.rendered`   (anticipated)
 *   2. `payload.renderings` (anticipated)
 *   3. `payload.channels`   (anticipated)
 *   4. `payload.envelope.rendered` / `.channels` (anticipated)
 *   5. `payload.{sms,email,whatsapp,inapp}` (EXISTS TODAY — plain strings)
 *
 * Inside an explicit container every key is treated as a channel, so a channel
 * added later (USSD, voice) appears without a frontend change. In the flat case
 * only the known channel keys are read, because the flat payload also holds
 * `priority`, `audiences` and `assignee`, which are facts and not messages.
 */
export function channelsFor(payload: unknown): RenderedChannel[] {
  if (!isPlainObject(payload)) return [];

  let source: Record<string, unknown> | null = null;
  let explicit = false;
  for (const k of ["rendered", "renderings", "channels"]) {
    const c = own(payload, k);
    if (isPlainObject(c)) {
      source = c;
      explicit = true;
      break;
    }
  }
  if (!source) {
    const env = own(payload, "envelope");
    for (const k of ["rendered", "channels"]) {
      const c = own(env, k);
      if (isPlainObject(c)) {
        source = c;
        explicit = true;
        break;
      }
    }
  }
  if (!source) source = payload;

  const keys = explicit
    ? Object.keys(source).sort((a, b) => {
        const ia = CHANNEL_ORDER.indexOf(a);
        const ib = CHANNEL_ORDER.indexOf(b);
        return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib) || a.localeCompare(b);
      })
    : CHANNEL_ORDER.filter((k) => FLAT_CHANNEL_KEYS.includes(k));

  const out: RenderedChannel[] = [];
  const seenLabels = new Set<string>();
  for (const key of keys) {
    const raw = own(source, key);
    if (raw === undefined) continue;
    const v = readChannelValue(raw);
    let text = v.text;
    let heading = v.heading;
    if (key === "email" && !heading) {
      const split = splitEmail(text);
      heading = split.subject;
      text = split.body;
    }
    if (!text && !heading) continue; // an empty channel is noise, not information
    const label = channelLabel(key);
    if (seenLabels.has(label)) continue; // `inapp` and `in_app` both present
    seenLabels.add(label);
    out.push({
      key,
      label,
      text,
      heading,
      meta: channelMeta(key, { ...v, text }),
      present: Boolean(text),
    });
  }
  return out;
}

/* ------------------------------------------------------------------ *
 * The raw payload table (the unknown-type degrade path)               *
 * ------------------------------------------------------------------ */

export interface PayloadEntry {
  key: string;
  label: string;
  value: string;
  /** Multi-line or long — render in a <pre> rather than inline. */
  long: boolean;
}

/**
 * Every payload field not already shown as a channel, humanised.
 *
 * This is what makes an unrecognised task type safe: whatever the backend sent,
 * the approver can read it. `GENERIC` (the only non-broadcast type produced
 * today) renders here as `Reason / Detail / Suggested action / Assignee`
 * instead of the raw `JSON.stringify` dump the old card showed.
 */
export function payloadEntries(payload: unknown, skip = HANDLED_PAYLOAD_KEYS): PayloadEntry[] {
  if (payload == null) return [];
  if (!isPlainObject(payload)) {
    const value = asText(payload);
    return value ? [{ key: "payload", label: "Payload", value, long: value.length > 80 }] : [];
  }
  const out: PayloadEntry[] = [];
  for (const key of Object.keys(payload)) {
    if (skip.has(key)) continue;
    const value = asText(own(payload, key));
    if (!value) continue;
    out.push({
      key,
      label: humanizeType(key),
      value: clamp(value, 4000),
      long: value.length > 80 || value.includes("\n"),
    });
  }
  return out;
}

/** Pretty-print the whole payload for the collapsed "raw" disclosure. */
export function rawPayload(payload: unknown): string {
  if (payload == null) return "(no payload)";
  const text = asText(payload);
  return text || "(empty payload)";
}

/* ------------------------------------------------------------------ *
 * Incident facts                                                      *
 * ------------------------------------------------------------------ */

export interface Fact {
  label: string;
  value: string;
  /**
   * Set only on the three facts that change how fast a supervisor must act: the M-PESA
   * corridor at risk (danger), a HUB major (warn, on the site type) and 50 000 or more users
   * (warn). The card gives those the one "needs attention" treatment (a dot in the state
   * colour); every other fact is plain text.
   */
  attention?: "danger" | "warn";
  /** An identifier (the site code): drawn in the mono face. */
  mono?: boolean;
  /** A list that reads on one line (the audiences): takes two grid columns where there are two. */
  wide?: boolean;
}

function pushFact(out: Fact[], label: string, value: unknown, extra?: Pick<Fact, "attention" | "mono" | "wide">): void {
  if (value == null || value === "") return;
  const text = clamp(asText(value), 120);
  if (!text) return;
  out.push({ label, value: text, ...extra });
}

/** An upper-case enum as a person reads it; free text and non-strings pass through `asText`. */
function human(v: unknown): string {
  return typeof v === "string" ? humanEnum(v) : asText(v);
}

/**
 * The facts that justify the decision, merged from the task row, the incident
 * (when the board fetch succeeded) and the payload — in that order of trust.
 *
 * `incident` is optional on purpose: `/api/v1/hitl/pending` returns only
 * `incident_number`, `priority` and `site_id`, so the card asks the incident
 * list for the rest. If that call fails the card still renders every fact the
 * task row carries. A missing fact is omitted, never printed as "undefined".
 */
export function factsFor(task: unknown, incident: unknown): Fact[] {
  const t = isPlainObject(task) ? task : {};
  const inc = isPlainObject(incident) ? incident : {};
  const payload = isPlainObject(own(t, "proposed_payload")) ? own(t, "proposed_payload") : {};
  const facts: Fact[] = [];

  // Name and code are separate facts: the code is an identifier (mono), the name is prose.
  const siteId = firstString(own(inc, "site_id"), own(t, "site_id"));
  const siteName = firstString(own(inc, "site_name"));
  pushFact(facts, "Site", siteName);
  pushFact(facts, "Site code", siteId, { mono: true });
  // A HUB major is said by the site type (with the attention dot) and "N child sites down";
  // a separate "HUB major: yes" fact repeated it.
  const hubMajor = own(inc, "is_hub_major") === true;
  pushFact(facts, "Site type", human(firstString(own(inc, "site_type"))), hubMajor ? { attention: "warn" } : undefined);

  const region = firstString(own(inc, "region_code"));
  const county = firstString(own(inc, "county"));
  if (region || county) pushFact(facts, "Region", [region, county].filter(Boolean).join(", "));

  const users = firstNumber(own(inc, "users_affected"));
  if (users != null) {
    pushFact(facts, "Subscribers (est.)", users.toLocaleString("en-KE"), users >= 50000 ? { attention: "warn" } : undefined);
  }

  pushFact(facts, "Domain", human(own(inc, "failure_domain")));

  // The M-PESA corridor is its own fact when it is at risk, so Services does not list it again.
  const mpesaRisk = own(inc, "mpesa_risk") === true;
  const services = own(inc, "services_impacted");
  if (Array.isArray(services) && services.length) {
    const listed = mpesaRisk ? services.filter((s) => !(typeof s === "string" && /^MPESA/i.test(s))) : services;
    if (listed.length) pushFact(facts, "Services", listed.map(human).join(", "));
  }

  if (mpesaRisk) pushFact(facts, "M‑PESA", "at risk", { attention: "danger" });
  // Good news in its normal state is not coloured (brief rule 7).
  if (own(inc, "service_affecting") === false) pushFact(facts, "Service affecting", "no");

  const children = firstNumber(own(inc, "child_sites_down"));
  if (children != null && children > 0) pushFact(facts, "Child sites down", String(children));

  const recurrence = firstNumber(own(inc, "recurrence_count"));
  if (recurrence != null && recurrence > 1) pushFact(facts, "Recurrence", `${recurrence} times`);

  // Owner and MSP are names or codes as the floor wrote them: never humanised. The MSP is
  // printed only when it is not the owner (an MSP-owned ticket said the same name twice).
  // The incident status is left out: the card is about the decision, not the ticket's state.
  const owner = firstString(own(inc, "assignee_name"), own(payload, "assignee"));
  const msp = firstString(own(inc, "responsible_msp"), own(inc, "msp_name"));
  pushFact(facts, "Owner", owner);
  if (msp && msp.trim().toLowerCase() !== owner.trim().toLowerCase()) pushFact(facts, "Vendor", msp);

  // "Goes to": who the approved message reaches, in the floor's words ("regional office (RNIO),
  // field engineer, vendor (MSP), management"). The check line names this fact.
  const audiences = own(payload, "audiences");
  if (Array.isArray(audiences) && audiences.length) {
    pushFact(facts, "Goes to", audiences.map((a) => (typeof a === "string" ? audienceWord(a) : asText(a))).join(", "), { wide: true });
  }

  return facts;
}

/**
 * The payload fields the card does not already show, for the short field list under the
 * facts. A broadcast's `priority` (the pill), `audiences` and `assignee` (facts) and its
 * internal `alert_id` are left out, so for a broadcast the list is empty; a GENERIC
 * escalation still shows its reason, detail and suggested action, and an unrecognised type
 * every field it carries. The raw payload disclosure always holds everything, unchanged.
 */
export function extraEntries(task: unknown, facts: Fact[]): PayloadEntry[] {
  const t = isPlainObject(task) ? task : {};
  const payload = own(t, "proposed_payload");
  if (!isPlainObject(payload)) return payloadEntries(payload);
  const skip = new Set(HANDLED_PAYLOAD_KEYS);
  const shown = (label: string) => facts.find((f) => f.label === label)?.value ?? "";
  const priority = own(payload, "priority");
  if (typeof priority === "string" && priority === own(t, "priority")) skip.add("priority");
  if (shown("Goes to") && Array.isArray(own(payload, "audiences"))) skip.add("audiences");
  const assignee = asText(own(payload, "assignee")).trim();
  if (assignee && assignee === shown("Owner")) skip.add("assignee");
  // A recognised type shows the alert elsewhere; an unknown one keeps every field it carries.
  if (specFor(own(t, "task_type")).known && typeof own(payload, "alert_id") === "string") skip.add("alert_id");
  return payloadEntries(payload, skip);
}

/* ------------------------------------------------------------------ *
 * Age / escalation ladder                                             *
 * ------------------------------------------------------------------ */

/** Whole minutes since `created_at`, or `null` when unparseable. */
export function ageMinutes(createdAt: unknown, now: number = Date.now()): number | null {
  const d = parseInstant(createdAt);
  if (!d) return null;
  const mins = Math.floor((now - d.getTime()) / 60000);
  return mins < 0 ? 0 : mins; // clock skew must not print "-2m"
}

const NBSP = "\u00a0";

/**
 * A wait as a person says it: "16 h 16 min", "4 min", "under a minute"; `""` when unknown.
 * A no-break space keeps each number with its unit, so "16 h" never splits across lines.
 */
export function fmtWait(mins: number | null): string {
  if (mins == null) return "";
  if (mins < 1) return "under a minute";
  if (mins < 60) return `${mins}${NBSP}min`;
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  return m ? `${h}${NBSP}h ${m}${NBSP}min` : `${h}${NBSP}h`;
}

/** The card's age: "waiting 16 h 16 min". */
export function fmtAge(mins: number | null): string {
  if (mins == null) return "age unknown";
  return `waiting ${fmtWait(mins)}`;
}

/**
 * §6.5 escalation ladder: T+5 unclaimed nudges the supervisor, T+15 the duty
 * manager, T+30 turns the Wallboard red. The backend job does not exist yet;
 * colouring the age chip on the same thresholds costs nothing and means the
 * inbox already reads the way the ladder will behave.
 */
export function ageTone(mins: number | null): "" | "warn" | "danger" {
  if (mins == null) return "";
  if (mins >= 30) return "danger";
  if (mins >= 5) return "warn";
  return "";
}

/**
 * True once the ladder has started on this task: a P1 or P2 still unclaimed at T+5. The card
 * prints its age in the watch colour then; before that, or once somebody holds the task, the
 * age is plain text.
 */
export function ladderBreached(mins: number | null, priority: unknown, claimedBy: unknown): boolean {
  if (ageTone(mins) === "") return false;
  if (typeof claimedBy === "string" && claimedBy) return false;
  return priority === "P1" || priority === "P2";
}

/* ------------------------------------------------------------------ *
 * The queue: order, summary                                           *
 * ------------------------------------------------------------------ */

const PRIORITY_RANK: Readonly<Record<string, number>> = { P1: 0, P2: 1, P3: 2, P4: 3 };

/** P1 first, then P2…P4, then cards with no priority (a handover, a maintenance window). */
export function priorityRank(priority: unknown): number {
  return typeof priority === "string" && Object.prototype.hasOwnProperty.call(PRIORITY_RANK, priority)
    ? PRIORITY_RANK[priority]
    : 9;
}

/**
 * The order a supervisor works the queue in: the most urgent card first, and within a priority
 * the card that has waited longest. A card whose time cannot be read goes after the dated ones
 * of its priority; the id breaks ties, so two answers of the same queue always draw the same
 * order. Total: tolerates anything in the array.
 */
export function compareCards(a: unknown, b: unknown): number {
  const ta = isPlainObject(a) ? a : {};
  const tb = isPlainObject(b) ? b : {};
  const rank = priorityRank(own(ta, "priority")) - priorityRank(own(tb, "priority"));
  if (rank) return rank;
  const da = parseInstant(own(ta, "created_at"))?.getTime() ?? Infinity;
  const db = parseInstant(own(tb, "created_at"))?.getTime() ?? Infinity;
  if (da !== db) return da < db ? -1 : 1;
  const ia = asText(own(ta, "id"));
  const ib = asText(own(tb, "id"));
  return ia < ib ? -1 : ia > ib ? 1 : 0;
}

export function sortQueue<T>(tasks: readonly T[]): T[] {
  return [...tasks].sort(compareCards);
}

export interface QueueSummary {
  /** "4 P1, 36 P2": the count per priority present, most urgent first; "" when none carries one. */
  byPriority: string;
  waiting: number;
  /** Minutes the oldest card has waited, or null. */
  oldest: number | null;
}

/** The line under the page head: "4 P1, 36 P2", "40 waiting", "oldest 3 h 16 min". */
export function queueSummary(tasks: readonly unknown[], now: number = Date.now()): QueueSummary {
  const counts: Record<string, number> = {};
  let oldest: number | null = null;
  for (const t of tasks) {
    const p = isPlainObject(t) ? own(t, "priority") : undefined;
    if (typeof p === "string" && Object.prototype.hasOwnProperty.call(PRIORITY_RANK, p)) counts[p] = (counts[p] ?? 0) + 1;
    const m = ageMinutes(isPlainObject(t) ? own(t, "created_at") : undefined, now);
    if (m != null && (oldest == null || m > oldest)) oldest = m;
  }
  const byPriority = Object.keys(PRIORITY_RANK)
    .filter((p) => counts[p])
    .map((p) => `${counts[p]} ${p}`)
    .join(", ");
  return { byPriority, waiting: tasks.length, oldest };
}

/* ------------------------------------------------------------------ *
 * A decision taken somewhere else                                     *
 * ------------------------------------------------------------------ */

export type Verdict = "approved" | "rejected" | "closed";

/** What another session did to a card, as far as this tab can tell. Every field may be unknown. */
export interface ElsewhereDecision {
  verdict: Verdict | null;
  by: string | null;
  at: string | null;
}

/** The API's 409 says what the card became: `{"detail": "task already APPROVED"}`. */
export function verdictOfConflict(err: unknown): Verdict | null {
  const m = /already\s+([A-Z_]+)/i.exec(detailOf(err, ""));
  if (!m) return null;
  const s = m[1].toUpperCase();
  if (s === "APPROVED") return "approved";
  if (s === "REJECTED") return "rejected";
  return "closed"; // EXPIRED, CANCELLED, …: decided, though not by a yes or a no
}

/**
 * The decision as the incident timeline records it: the work note an approve or a reject
 * writes ("HITL approved broadcast/assignment." / "HITL rejected: wording", authored
 * "NOC Analyst (NOC)"), the newest one at or after the card was raised. Null when the timeline
 * has none (the card had no incident, or the note has not landed yet).
 */
export function decisionFromTimeline(items: unknown, raisedAt: unknown): ElsewhereDecision | null {
  if (!Array.isArray(items)) return null;
  const since = parseInstant(raisedAt)?.getTime() ?? -Infinity;
  let best: ElsewhereDecision | null = null;
  let bestT = -Infinity;
  for (const it of items) {
    if (!isPlainObject(it) || own(it, "kind") !== "note") continue;
    const m = /^HITL (approved|rejected)\b/i.exec(asText(own(it, "detail")));
    if (!m) continue;
    const t = parseInstant(own(it, "ts"))?.getTime() ?? -Infinity;
    if (t < since || t < bestT) continue;
    const by = asText(own(it, "title")).replace(/\s*\([^)]*\)\s*$/, "").trim();
    best = { verdict: m[1].toLowerCase() as Verdict, by: by || null, at: asText(own(it, "ts")) || null };
    bestT = t;
  }
  return best;
}

/**
 * "Approved by NOC Analyst in another session, 15:24"; the parts that are unknown are left out.
 * A card that left the queue without a known yes or no (expired, cancelled, reseeded) is not
 * called decided: "No longer waiting: it was closed in another session".
 */
export function elsewhereHeadline(d: ElsewhereDecision, hm: (ts: unknown) => string): string {
  const at = d.at ? hm(d.at) : "";
  const when = at ? `, ${at}` : "";
  if (d.verdict !== "approved" && d.verdict !== "rejected") return `No longer waiting: it was closed in another session${when}`;
  const verb = d.verdict === "approved" ? "Approved" : "Rejected";
  const by = d.by ? ` by ${clamp(d.by, 60)}` : "";
  return `${verb}${by} in another session${when}`;
}

/* ------------------------------------------------------------------ *
 * Errors                                                              *
 * ------------------------------------------------------------------ */

/** `fetch` rejects with a TypeError whose wording differs per browser when the API is down. */
const NETWORK_FAILURE = /failed to fetch|networkerror|load failed|network request failed/i;

/**
 * A decision request's failure, as the problem and what to do. The server's own sentence
 * (`detailOf`) is used where it is the useful part (a 400 or 403 says exactly which rule
 * refused it); a dead network reads as one, not as "TypeError: Failed to fetch". The raw
 * text is kept by the caller as a `title`, so nothing is hidden.
 */
export function friendlyError(err: unknown): string {
  const status = statusOf(err);
  const raw = err instanceof Error ? err.message : asText(err);
  if (status === 409) return "Somebody else got there first; your decision was not recorded.";
  if (status === 404) return "The card is gone; your decision was not recorded.";
  if (status === 401) return "Your session has ended. Sign in again, then retry.";
  if (status === 403) return clamp(`Not permitted: ${detailOf(err, "your role cannot decide this card")}.`, 200);
  if (status === 400 || status === 422) return clamp(capFirst(detailOf(err, "The API refused the request.")), 200);
  if (status != null && status >= 500) return "The API failed on that request. Nothing was decided; try again.";
  if (NETWORK_FAILURE.test(raw)) return "The API is unreachable. Nothing was decided; try again.";
  if (!raw) return "The request failed.";
  return clamp(detailOf(err), 200);
}

/**
 * The queue's own load failure, as the problem in one sentence. Separate from
 * `friendlyError`, whose wording ("Nothing was decided") is about a decision.
 */
export function friendlyLoadError(err: unknown): string {
  const status = statusOf(err);
  const raw = err instanceof Error ? err.message : asText(err);
  if (status === 401) return "Your session has ended. Sign in again, then retry.";
  if (status === 403) return "Your role cannot see the approvals queue.";
  if (status != null && status >= 500) return "The API failed while loading the queue.";
  if (NETWORK_FAILURE.test(raw)) return "The API is unreachable.";
  if (!raw) return "The request failed.";
  return clamp(detailOf(err), 160);
}

function capFirst(s: string): string {
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}
