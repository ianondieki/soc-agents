import { applyPatch, mapStrings, type Leaf } from "./patch";
import { shiftString, type Shift } from "./clock";

/**
 * The demo's stand-in for the API: every answer comes from the recording scripts/demo/record.py
 * made of the real backend (see that file for what is in it). This module holds where the demo is
 * (before the storm, which alarm, after it), which presses the visitor has made, and the session;
 * `answer()` turns a request into the recorded response for that moment, moved to now.
 *
 * A press the recording has (approve, reject, claim, note, restore, close, reassign, handover, the
 * monitor check, a complaint and its tracking) is applied as its recorded patch, with the visitor's
 * own words in place of the placeholders. Any other change answers 409 with a sentence saying the
 * demo does not save it, which every page already shows as the server's words.
 */

type Answer = { s: number; b: unknown };
type Reads = Map<string, Answer>;

interface Action {
  status: number;
  response: unknown;
  patches: Record<string, { s: number; p: unknown }>;
  frames: any[];
  at: string;
}

export interface Recording {
  version: number;
  times: { s0: string; storm: string; final: string };
  s0: Record<string, [number, number]>;
  storm: { site_id: string; response: any; frames: any[]; reads: Record<string, { s: number; p: unknown }> }[];
  final: Record<string, { s: number; p: unknown }>;
  actions: Record<string, Action>;
  sentinels: Record<string, string>;
  complaints: { msisdn: string; texts: { text: string; match: string; place: string }[] };
  bodies: unknown[];
}

interface Pressed {
  name: string;
  /** Wall-clock ms of the press. */
  wall: number;
  /** What the visitor typed, by placeholder name, plus `who`. */
  args: Record<string, string>;
}

export interface DemoState {
  stormAt: number | null;
  steps: number;
  pressed: Pressed[];
  session: Record<string, unknown> | null;
}

export const NOT_SAVED =
  "This public demo replays a recorded run of the console, so this change is not saved. Run the console from its repository to try it.";

const STATE_KEY = "noc_demo_state_v1";
const API = "/api/v1";
/** The gap between the frames of one alarm: the live agent delay the demo stack runs with. */
const FRAME_GAP_MS = 70;

function cloneAnswer(a: Answer | undefined): Answer | undefined {
  return a && { s: a.s, b: a.b };
}

export class DemoServer {
  readonly rec: Recording;
  private s0: Reads = new Map();
  /** The reads after each storm alarm (cumulative). */
  private steps: Reads[] = [];
  /** The reads after the storm, with every detail read. */
  private final: Reads = new Map();
  private recordedAt: number;
  private loadedAt = Date.now();
  state: DemoState = { stormAt: null, steps: 0, pressed: [], session: null };
  private cache = new Map<string, Answer>();
  private sockets = new Set<(frame: string) => void>();

  constructor(rec: Recording) {
    this.rec = rec;
    this.recordedAt = Date.parse(rec.times.final);
    for (const [u, [s, i]] of Object.entries(rec.s0)) this.s0.set(u, { s, b: rec.bodies[i] });
    let cur: Reads = new Map(this.s0);
    for (const step of rec.storm) {
      cur = DemoServer.patched(cur, step.reads);
      this.steps.push(cur);
    }
    this.final = DemoServer.patched(cur, rec.final);
    this.restore();
  }

  private static patched(base: Reads, delta: Record<string, { s: number; p: unknown }>): Reads {
    const out = new Map(base);
    for (const [u, d] of Object.entries(delta)) out.set(u, { s: d.s, b: applyPatch(base.get(u)?.b ?? null, d.p) });
    return out;
  }

  // ------------------------------------------------------------------ state

  private restore(): void {
    try {
      const raw = sessionStorage.getItem(STATE_KEY);
      if (!raw) return;
      const saved = JSON.parse(raw);
      if (saved?.recorded !== this.rec.times.final) return; // another recording: start over
      this.state = saved.state;
      this.loadedAt = Number(saved.loadedAt) || this.loadedAt;
    } catch {
      /* storage blocked or malformed: a fresh demo */
    }
  }

  private save(): void {
    this.cache.clear();
    try {
      sessionStorage.setItem(STATE_KEY, JSON.stringify({ recorded: this.rec.times.final, loadedAt: this.loadedAt, state: this.state }));
    } catch {
      /* the demo still works for this page view */
    }
  }

  /** Back to the start: before the storm, nothing pressed. */
  reset(): void {
    try {
      sessionStorage.removeItem(STATE_KEY);
    } catch {
      /* nothing to clear */
    }
  }

  private shift(): Shift {
    if (this.state.stormAt != null) return { by: this.state.stormAt - Date.parse(this.rec.times.storm), recordedAt: this.recordedAt };
    return { by: this.loadedAt - Date.parse(this.rec.times.s0), recordedAt: this.recordedAt };
  }

  private who(): string {
    return String(this.session().display_name || "Duty manager");
  }

  private session(): Record<string, unknown> {
    const base = (this.s0.get(`${API}/session`)?.b as Record<string, unknown>) || {};
    return { ...base, ...(this.state.session || {}) };
  }

  // ------------------------------------------------------------------ reads

  private raw(url: string): Answer | undefined {
    const n = this.state.steps;
    if (n === 0) return this.s0.get(url) ?? undefined;
    if (n < this.steps.length) return this.steps[n - 1].get(url) ?? this.final.get(url) ?? this.s0.get(url);
    return this.final.get(url) ?? this.s0.get(url);
  }

  /** A press's own leaf: its times moved to the moment of the press, the visitor's words in. */
  private leafFor(p: Pressed): Leaf {
    const act = this.rec.actions[p.name];
    const s: Shift = { by: p.wall - Date.parse(act.at), recordedAt: this.recordedAt };
    const swaps: [string, string][] = [];
    for (const [k, mark] of Object.entries(this.rec.sentinels)) if (p.args[k] != null) swaps.push([mark, p.args[k]]);
    if (p.args.cannedText != null && p.args.body != null) swaps.push([p.args.cannedText, p.args.body]);
    if (p.args.cannedMasked != null && p.args.masked != null) swaps.push([p.args.cannedMasked, p.args.masked]);
    // The place only in words written to the customer, never in a site's own name.
    const place: [string, string] | null = p.args.cannedPlace && p.args.place ? [p.args.cannedPlace, p.args.place] : null;
    return (v) => {
      if (typeof v !== "string") return v;
      let out = shiftString(v, s);
      for (const [from, to] of swaps) if (out.includes(from)) out = out.split(from).join(to);
      if (place && out.includes(place[0]) && !/eNodeB|HUB|SFC-/.test(out)) out = out.split(place[0]).join(place[1]);
      return out;
    };
  }

  /** The read as the visitor sees it now. */
  read(url: string): Answer | undefined {
    const key = `${url}|${this.state.steps}|${this.state.pressed.length}`;
    const hit = this.cache.get(key);
    if (hit) return cloneAnswer(hit);
    const base = this.raw(url);
    let out: Answer | undefined;
    if (base) {
      const s = this.shift();
      out = { s: base.s, b: mapStrings(base.b, (v) => shiftString(v as string, s)) };
    }
    for (const p of this.state.pressed) {
      const patch = this.rec.actions[p.name]?.patches[url];
      if (!patch) continue;
      out = { s: patch.s, b: applyPatch(out?.b ?? null, patch.p, this.leafFor(p)) };
    }
    if (url === `${API}/session` && out) out = { s: 200, b: this.session() };
    if (out) this.cache.set(key, out);
    return cloneAnswer(out);
  }

  /** GET: the recorded read, or the closest one (a filter the recording did not ask is applied here). */
  get(path: string, query: URLSearchParams): Answer {
    const full = query.toString() ? `${path}?${query}` : path;
    const exact = this.read(full) ?? this.read(decodeURIComponent(full));
    if (exact) return exact;

    if (path === `${API}/audit`) {
      const rows = (this.read(`${API}/audit?limit=1000`)?.b as any[]) || [];
      const terms = query.getAll("q").map((t) => t.toLowerCase()).filter(Boolean);
      const hits = terms.length ? rows.filter((r) => terms.some((t) => JSON.stringify(r).toLowerCase().includes(t))) : rows;
      return { s: 200, b: hits.slice(0, Number(query.get("limit")) || 100) };
    }
    if (path === `${API}/support/complaints`) {
      const q = this.read(`${API}/support/complaints?limit=200`)?.b as any;
      const limit = Number(query.get("limit")) || 200;
      return { s: 200, b: q ? { ...q, items: (q.items || []).slice(0, limit) } : { items: [], counts: {} } };
    }
    if (path === `${API}/support/kb/search`) {
      const kb = (this.read(`${API}/support/kb`)?.b as any)?.articles || [];
      const words = (query.get("q") || "").toLowerCase().split(/\s+/).filter(Boolean);
      const hits = kb.filter((a: any) => words.some((w) => JSON.stringify(a).toLowerCase().includes(w)));
      return { s: 200, b: { results: hits.slice(0, 5).map((a: any) => ({ ...a, score: 1 })) } };
    }
    if (path === `${API}/contracts/clauses/search`) return this.contractSearch(query.get("q") || "");
    if (path === `${API}/pir` && query.get("status")) {
      const rows = (this.read(`${API}/pir`)?.b as any[]) || [];
      return { s: 200, b: rows.filter((r) => r.status === query.get("status")) };
    }
    if (path === `${API}/scorecards`) {
      const rows = (this.read(`${API}/scorecards`)?.b as any[]) || [];
      const f = (k: string, field: string) => (r: any) => !query.get(k) || String(r[field]) === query.get(k);
      return { s: 200, b: rows.filter(f("vendor", "vendor_code")).filter(f("period", "period")).filter(f("status", "status")) };
    }
    // Any other query on a recorded path: the unfiltered read.
    const plain = this.read(path);
    if (plain) return plain;
    return { s: 404, b: { detail: "Not Found" } };
  }

  private contractSearch(q: string): Answer {
    const words = q.toLowerCase().split(/\s+/).filter(Boolean);
    const seen = new Map<string, any>();
    let template: any = null;
    for (const u of this.final.keys()) {
      if (!u.startsWith(`${API}/contracts/clauses/search?`)) continue;
      const body = this.read(u)?.b as any;
      if (!body) continue;
      template = template || body;
      for (const h of body.hits || []) {
        const text = JSON.stringify(h).toLowerCase();
        if (words.length && words.some((w) => text.includes(w))) seen.set(JSON.stringify(h.clause_id ?? h.id ?? h), h);
      }
    }
    return { s: 200, b: { ...(template || {}), query: q, hits: [...seen.values()].slice(0, 8) } };
  }

  // ------------------------------------------------------------------ the realtime stream

  connect(send: (frame: string) => void): () => void {
    this.sockets.add(send);
    return () => this.sockets.delete(send);
  }

  private emit(frames: any[], s: Shift, gap = FRAME_GAP_MS): Promise<void> {
    return new Promise((done) => {
      if (!frames.length) return done();
      frames.forEach((f, n) => {
        window.setTimeout(() => {
          const moved = mapStrings(f, (v) => shiftString(v as string, s)) as any;
          // The frame happens now, whatever its recorded instant.
          moved.ts = new Date().toISOString();
          const text = JSON.stringify(moved);
          this.sockets.forEach((send) => send(text));
          if (n === frames.length - 1) done();
        }, gap * (n + 1));
      });
    });
  }

  // ------------------------------------------------------------------ presses

  private pressedNames(): Set<string> {
    return new Set(this.state.pressed.map((p) => p.name));
  }

  private press(name: string, args: Record<string, string> = {}): Answer {
    const act = this.rec.actions[name];
    const p: Pressed = { name, wall: Date.now(), args: { who: this.who(), ...args } };
    this.state.pressed.push(p);
    this.save();
    const leaf = this.leafFor(p);
    void this.emit(act.frames, { by: p.wall - Date.parse(act.at), recordedAt: this.recordedAt });
    return { s: act.status, b: mapStrings(act.response, leaf) };
  }

  private stormAlarm(body: any): Promise<Answer> | Answer {
    const steps = this.rec.storm;
    const at = steps.findIndex((st, n) => n >= this.state.steps && st.site_id === body?.site_id);
    if (at < 0) {
      return { s: 409, b: { detail: "This demo replays the heavy-rain storm once. Press Start over at the top of the page to run it again." } };
    }
    if (this.state.stormAt == null) this.state.stormAt = Date.now();
    this.state.steps = at + 1;
    this.save();
    const s = this.shift();
    const step = steps[at];
    return this.emit(step.frames, s).then(() => ({ s: 200, b: mapStrings(step.response, (v) => shiftString(v as string, s)) }));
  }

  private async wholeStorm(): Promise<Answer> {
    const from = this.state.steps;
    if (this.state.stormAt == null) this.state.stormAt = Date.now();
    this.state.steps = this.rec.storm.length;
    this.save();
    const s = this.shift();
    const frames = this.rec.storm.slice(from).flatMap((st) => st.frames);
    await this.emit(frames, s, 20);
    await this.emit([{ type: "demo.rain_storm.complete", operator_id: "safaricom", payload: { count: this.rec.storm.length, regions: ["RFT", "MTK", "NBI_E"] }, incident_id: null, run_id: null, ts: "" }], s);
    const incidents = this.rec.storm.map((st) => st.response?.incident).filter(Boolean);
    return {
      s: 200,
      b: mapStrings({ ok: true, scenario: "rain_storm_mw", count: incidents.length, incidents, events_template: [] }, (v) => shiftString(v as string, s)),
    };
  }

  private decide(kind: "claim" | "approve" | "reject", id: string, body: any): Answer {
    const done = this.pressedNames();
    if (done.has(`approve:${id}`)) return { s: 409, b: { detail: "task already APPROVED" } };
    if (done.has(`reject:${id}`)) return { s: 409, b: { detail: "task already REJECTED" } };
    if (kind === "claim" && done.has(`claim:${id}`)) return { s: 409, b: { detail: "task already CLAIMED" } };
    if (kind === "reject" && !String(body?.reason || "").trim()) return { s: 400, b: { detail: "reason required on reject" } };
    const name = `${kind}:${id}`;
    if (this.rec.actions[name]) return this.press(name, { reason: String(body?.reason || "") });
    const card = ((this.read(`${API}/hitl/pending`)?.b as any[]) || []).find((c) => c.id === id);
    if (!card) return { s: 404, b: { detail: "task not found" } };
    return { s: 409, b: { detail: NOT_SAVED } };
  }

  private ticket(id: string, verb: string, body: any): Answer {
    const done = this.pressedNames();
    const closed = done.has(`close:${id}`) || done.has(`close-after-restore:${id}`);
    if (verb === "notes") {
      const text = String(body?.body || "").trim();
      if (!text) return { s: 422, b: { detail: "body: a note needs some text" } };
      if (body?.mark_restored && !done.has(`restore:${id}`) && !closed) return this.press(`restore:${id}`, { note: text });
      if (this.rec.actions[`note:${id}`] && !done.has(`note:${id}`)) return this.press(`note:${id}`, { note: text });
      return { s: 409, b: { detail: NOT_SAVED } };
    }
    if (verb === "close") {
      if (closed) return { s: 200, b: { ok: true, already: true, status: "CLOSED", withdrawn_cards: [] } };
      const name = done.has(`restore:${id}`) ? `close-after-restore:${id}` : `close:${id}`;
      return this.rec.actions[name] ? this.press(name) : { s: 409, b: { detail: NOT_SAVED } };
    }
    if (verb === "reassign") {
      if (closed) return { s: 409, b: { detail: "the ticket is closed, so it cannot be reassigned" } };
      const name = `reassign:${id}`;
      if (!this.rec.actions[name] || done.has(name)) return { s: 409, b: { detail: NOT_SAVED } };
      return this.press(name, { msp: String(body?.assignee_name || "").trim(), reason: String(body?.reason || "").trim() });
    }
    return { s: 409, b: { detail: NOT_SAVED } };
  }

  private complaint(body: any): Answer {
    const text = String(body?.body || "").trim();
    const msisdn = String(body?.msisdn || "").replace(/[\s-]/g, "");
    const texts = this.rec.complaints.texts;
    let n = texts.findIndex((t) => t.match && new RegExp(t.match, "i").test(text));
    if (n < 0) n = texts.length - 1;
    const name = `complain:${n}`;
    const act = this.rec.actions[name];
    if (!act) return { s: 409, b: { detail: NOT_SAVED } };
    const canned = (act.response as any)?.complaint?.customer?.msisdn_masked;
    const args = {
      body: text,
      cannedText: texts[n].text,
      masked: maskMsisdn(msisdn),
      cannedMasked: canned ?? "",
      msisdn,
      cannedPlace: texts[n].place,
      place: placeIn(text, texts[n].match) || texts[n].place,
    };
    if (this.pressedNames().has(name)) {
      // The same kind of complaint again: the desk already has it on file.
      return { s: 200, b: mapStrings(act.response, this.leafFor({ name, wall: Date.now(), args: { who: this.who(), ...args } })) };
    }
    return this.press(name, args);
  }

  private track(path: string, body: any): Answer {
    const ref = String(body?.ref || "").trim().toUpperCase();
    const msisdn = String(body?.msisdn || "").replace(/[\s-]/g, "");
    const filed = this.state.pressed.find((p) => {
      if (!p.name.startsWith("complain:")) return false;
      const r = (this.rec.actions[p.name].response as any)?.complaint?.ref;
      return String(r).toUpperCase() === ref && sameNumber(p.args.msisdn, msisdn);
    });
    if (!filed) return { s: 404, b: { detail: "We could not find a complaint with that reference and number." } };
    const n = filed.name.split(":")[1];
    if (path.endsWith("/still-down")) {
      const name = `still-down:${n}`;
      const act = this.rec.actions[name];
      if (!act) return { s: 409, b: { detail: NOT_SAVED } };
      if (act.status >= 400 || this.pressedNames().has(name)) return { s: act.status, b: mapStrings(act.response, this.leafFor({ ...filed, name })) };
      return this.press(name, filed.args);
    }
    const act = this.rec.actions[`track:${n}`];
    return act ? { s: act.status, b: mapStrings(act.response, this.leafFor({ ...filed, name: `track:${n}` })) } : { s: 404, b: { detail: "Not Found" } };
  }

  /** One request, answered. A promise when the answer waits on the realtime frames it causes. */
  answer(method: string, path: string, query: URLSearchParams, body: any): Answer | Promise<Answer> {
    if (method === "GET" || method === "HEAD") return this.get(path, query);
    let m: RegExpExecArray | null;
    if (path === `${API}/session`) {
      const name = String(body?.display_name ?? "").trim();
      if (!name || !/\p{L}|\p{N}/u.test(name)) return { s: 422, b: { detail: [{ loc: ["body", "display_name"], msg: "Enter a name" }] } };
      this.state.session = { display_name: name, role: body?.role ?? this.session().role, region_filter: body?.region_filter ?? null, msp_filter: body?.msp_filter ?? null };
      this.save();
      return { s: 200, b: this.session() };
    }
    if (path === `${API}/events`) return this.stormAlarm(body);
    if (path === `${API}/demo/rain-storm`) return this.wholeStorm();
    if ((m = /^\/api\/v1\/hitl\/([^/]+)\/(claim|approve|reject)$/.exec(path))) return this.decide(m[2] as any, m[1], body);
    if ((m = /^\/api\/v1\/incidents\/([^/]+)\/(notes|close|reassign)$/.exec(path))) return this.ticket(m[1], m[2], body);
    if (path === `${API}/monitor/tick`) {
      return this.pressedNames().has("monitor-tick") ? { s: 200, b: { ok: true, checked: 0, escalated: [] } } : this.press("monitor-tick");
    }
    if (path === `${API}/shifts/handover`) {
      return this.pressedNames().has("handover") ? { s: 409, b: { detail: NOT_SAVED } } : this.press("handover");
    }
    if (path === `${API}/support/complaints`) return this.complaint(body);
    if (path === `${API}/support/track` || path === `${API}/support/track/still-down`) return this.track(path, body);
    if (path === `${API}/support/evals/run`) return this.get(`${API}/support/evals/latest`, new URLSearchParams());
    return { s: 409, b: { detail: NOT_SAVED } };
  }
}

/** "+254 7•• •• 5678", as the support desk writes a customer number. */
export function maskMsisdn(raw: string): string {
  const digits = raw.replace(/\D/g, "");
  const local = digits.startsWith("254") ? digits.slice(3) : digits.startsWith("0") ? digits.slice(1) : digits;
  if (local.length < 9) return raw;
  return `+254 ${local[0]}•• •• ${local.slice(-4)}`;
}

/** The place the visitor named, from the complaint's word list ("Umoja"), or "" when none is one. */
function placeIn(text: string, match: string): string {
  for (const alt of match.split("|")) {
    if (!/^[a-z ]+$/.test(alt) || alt === "hakuna" || alt === "rift") continue;
    if (new RegExp(`\\b${alt}\\b`, "i").test(text)) return alt.replace(/\b[a-z]/g, (c) => c.toUpperCase());
  }
  return "";
}

function sameNumber(a: string | undefined, b: string): boolean {
  const n = (x: string) => x.replace(/\D/g, "").replace(/^(254|0)/, "");
  return !!a && n(a) === n(b);
}
