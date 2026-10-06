import { useSyncExternalStore } from "react";
import { isCriticalEvent, type NocEvent } from "../realtime/renderers";

/**
 * Alerts: what the console does, beyond updating its lists, when something happens a person must
 * notice. One vocabulary for the whole app, each cue felt (a vibration, on a phone or tablet that
 * has one), optionally heard (a short synthesised tone, off until a person turns it on) and, for
 * the alarms, seen as a signal under the top bar that says what happened in words.
 *
 *   cue          when                                         felt (ms on, off, on...)
 *   alarm-p1     a new P1 ticket                              three long pulses
 *   alarm-p2     a new P2 ticket                              two short pulses
 *   decision     a decision is waiting for a person           three light taps
 *   escalation   a case needs a person, a send failed...      two long pulses
 *   run-failed   an agent run or a scheduled job failed       one long pulse
 *   step         a hop lit on the agent rail you are watching a tick
 *   run-done     the run you are watching finished            a double tick
 *   confirm      your own action went through                 a tick
 *
 * The rules that keep it from becoming noise:
 *  - Live frames only (the replay on connect is history and never cues).
 *  - A storm does not machine-gun: a cue repeats at most every few seconds, and each repeat inside
 *    20 s doubles the gap (2.5 s, 5 s, 10 s, then every 20 s) while the signal counts them.
 *  - A weaker cue never cuts a stronger one off mid-pattern.
 *  - "Alarms only" keeps the first five; "Off" stops all of it. Quiet mode keeps only what the
 *    ticker keeps (P1 and P2 tickets, decisions, failures), and the signal does not animate.
 *  - The public pages (front page, complaint form, tracking) never cue.
 *
 * Browsers allow vibration and sound only after a person has pressed something on the page; until
 * then the signal still shows. Vibration works where the device has a motor and the browser
 * supports it (Chrome and Edge on Android); iPhones and desktops show and, if asked, sound.
 */

export type CueKind = "alarm-p1" | "alarm-p2" | "decision" | "escalation" | "run-failed" | "step" | "run-done" | "confirm";
export type AlertLevel = "all" | "alarms" | "off";

/** The localStorage keys; kept beside the other display keys' naming. */
export const ALERT_LEVEL_KEY = "noc_alerts_v1";
export const ALERT_SOUND_KEY = "noc_alert_sound_v1";

export const ALERT_LEVEL_LABEL: Record<AlertLevel, string> = {
  all: "Alarms and agent work",
  alarms: "Alarms only",
  off: "Off",
};
export const ALERT_LEVEL_MEANING: Record<AlertLevel, string> = {
  all: "The alarms, and a light tick as each agent step lands.",
  alarms: "A new P1 or P2 ticket, a decision, an escalation or a failure.",
  off: "No buzz, tone or signal. The lists still update.",
};
export const ALERT_SOUND_MEANING = "A short tone with each alarm.";

/** Felt: the vibration pattern, milliseconds on, off, on... */
export const PATTERN: Record<CueKind, number[]> = {
  "alarm-p1": [220, 90, 220, 90, 420],
  "alarm-p2": [140, 90, 140],
  decision: [70, 70, 70, 70, 70],
  escalation: [320, 140, 320],
  "run-failed": [260],
  step: [8],
  "run-done": [18, 50, 18],
  confirm: [14],
};

/** Strength: a weaker cue never interrupts a stronger one that is still playing. */
const RANK: Record<CueKind, number> = {
  "alarm-p1": 6,
  "alarm-p2": 5,
  escalation: 4,
  decision: 4,
  "run-failed": 3,
  "run-done": 2,
  confirm: 2,
  step: 1,
};

/** The least time between two cues of one kind; repeats inside STREAK_MS double it, up to 8x. */
const GAP_MS: Record<CueKind, number> = {
  "alarm-p1": 2500,
  "alarm-p2": 2500,
  decision: 2500,
  escalation: 2500,
  "run-failed": 2500,
  step: 350,
  "run-done": 1200,
  confirm: 0,
};
const STREAK_MS = 20_000;

const ALARMS: ReadonlySet<CueKind> = new Set(["alarm-p1", "alarm-p2", "decision", "escalation", "run-failed"]);
/** How long the signal stays after its last update. */
export const SIGNAL_MS = 9000;

export interface Signal {
  /** Increases with every new signal, so the view can replay its entrance. */
  id: number;
  kind: CueKind;
  count: number;
  /** One line: what happened. */
  text: string;
  /** A short identifier shown in mono after the words (a ticket number). */
  ref?: string;
  /** Where "Open" goes. */
  href?: string;
  at: number;
  test?: boolean;
}

export interface CueInfo {
  text?: string;
  /** The words when several of the same kind have been counted ("3 new P1 tickets"). */
  many?: (n: number) => string;
  ref?: string;
  href?: string;
  /** Where "Open" goes once several are counted. */
  manyHref?: string;
  /** The event was not critical: quiet mode drops it. Cues from a person's own action are. */
  critical?: boolean;
  test?: boolean;
}

/* ------------------------------------------------------------------ prefs --- */

function readLevel(): AlertLevel {
  try {
    const v = window.localStorage.getItem(ALERT_LEVEL_KEY);
    if (v === "all" || v === "alarms" || v === "off") return v;
  } catch {
    /* storage blocked */
  }
  return "all";
}
function readSound(): boolean {
  try {
    return window.localStorage.getItem(ALERT_SOUND_KEY) === "1";
  } catch {
    return false;
  }
}

interface State {
  level: AlertLevel;
  sound: boolean;
  signal: Signal | null;
}
let state: State = { level: "all", sound: false, signal: null };
let loaded = false;
const listeners = new Set<() => void>();

function ensureLoaded(): void {
  if (loaded) return;
  loaded = true;
  if (typeof window === "undefined") return;
  state = { ...state, level: readLevel(), sound: readSound() };
}
function emit(next: Partial<State>): void {
  state = { ...state, ...next };
  for (const l of listeners) l();
}
function subscribe(l: () => void): () => void {
  listeners.add(l);
  return () => listeners.delete(l);
}
function snapshot(): State {
  ensureLoaded();
  return state;
}

export function setAlertLevel(level: AlertLevel): void {
  ensureLoaded();
  try {
    window.localStorage.setItem(ALERT_LEVEL_KEY, level);
  } catch {
    /* storage blocked: the choice lasts until reload */
  }
  if (level === "off") {
    stopVibration();
    emit({ level, signal: null });
  } else emit({ level });
}
export function setAlertSound(on: boolean): void {
  ensureLoaded();
  try {
    window.localStorage.setItem(ALERT_SOUND_KEY, on ? "1" : "0");
  } catch {
    /* storage blocked */
  }
  emit({ sound: on });
  // The press that turned it on is the gesture that lets the page make a sound.
  if (on) unlockAudio();
}
export function dismissSignal(): void {
  held = false;
  if (signalTimer != null) window.clearTimeout(signalTimer);
  signalTimer = null;
  emit({ signal: null });
}
/** A pointer or focus on the signal holds it (its drain line pauses with it, in CSS); leaving
 *  lets the time that was left run on. */
export function holdSignal(hold: boolean): void {
  if (hold === held) return;
  held = hold;
  if (signalTimer != null) window.clearTimeout(signalTimer);
  signalTimer = null;
  const cur = state.signal;
  if (!cur) return;
  if (hold) {
    remaining = Math.max(0, deadline - Date.now());
    return;
  }
  armSignalTimer(cur.id, remaining || SIGNAL_MS);
}

function armSignalTimer(id: number, ms: number): void {
  deadline = Date.now() + ms;
  signalTimer = window.setTimeout(() => {
    signalTimer = null;
    if (state.signal && state.signal.id === id) emit({ signal: null });
  }, ms);
}

/** The alert preferences and the current signal, for React. */
export function useAlerts(): State {
  return useSyncExternalStore(subscribe, snapshot, snapshot);
}

/* --------------------------------------------------------------- delivery --- */

const lastAt: Partial<Record<CueKind, number>> = {};
const seenAt: Partial<Record<CueKind, number>> = {};
const streak: Partial<Record<CueKind, number>> = {};
let playingUntil = 0;
let playingRank = 0;
let signalTimer: number | null = null;
let held = false;
let deadline = 0;
let remaining = 0;
let nextSignalId = 1;

function canVibrate(): boolean {
  return typeof navigator !== "undefined" && typeof navigator.vibrate === "function";
}
function stopVibration(): void {
  try {
    if (canVibrate()) navigator.vibrate(0);
  } catch {
    /* ignore */
  }
}
function quietOn(): boolean {
  try {
    return document.documentElement.getAttribute("data-quiet") === "on";
  } catch {
    return false;
  }
}
/** The public pages (front page, complaint form, tracking) are for customers: they never cue. */
export function cuesAllowedHere(pathname: string): boolean {
  const p = pathname.replace(/\/+$/, "") || "/";
  return !(p === "/" || p === "/complain" || p.startsWith("/complain/") || p === "/track" || p.startsWith("/track/"));
}
function here(): string {
  try {
    return window.location.pathname;
  } catch {
    return "/mission";
  }
}

/**
 * Fire one cue. Returns true when it was felt or heard (for tests), false when a rule held it back.
 * The signal (alarms only) is counted even while the buzz is held back by the gap.
 */
export function cue(kind: CueKind, info: CueInfo = {}, now: number = Date.now()): boolean {
  ensureLoaded();
  const { level } = state;
  if (level === "off") return false;
  if (level === "alarms" && !ALARMS.has(kind)) return false;
  if (!info.test && !cuesAllowedHere(here())) return false;
  // Quiet mode keeps what the ticker keeps (the alarms, when critical) and the tick under a
  // person's own finger; the agents' ticks and the run's end go quiet.
  const quiet = quietOn();
  if (quiet && !info.test && kind !== "confirm" && (!ALARMS.has(kind) || info.critical === false)) return false;

  if (ALARMS.has(kind)) showSignal(kind, info, now);
  if (info.test) {
    lastAt[kind] = now;
    return deliver(kind, now);
  }

  // The gap, doubled for each repeat felt inside the streak (2.5 s, 5 s, 10 s, then 20 s). The
  // streak ends after 20 s with nothing of this kind at all, felt or held back.
  const seen = seenAt[kind];
  seenAt[kind] = now;
  const inStreak = seen != null && now - seen < STREAK_MS;
  const steps = inStreak ? streak[kind] ?? 0 : 0;
  const gap = GAP_MS[kind] * (ALARMS.has(kind) ? 2 ** steps : 1);
  const prev = lastAt[kind];
  if (inStreak && prev != null && now - prev < gap) return false;
  // A weaker cue never cuts a stronger pattern off; it is not spent either.
  if (now < playingUntil && RANK[kind] < playingRank) return false;
  lastAt[kind] = now;
  streak[kind] = inStreak ? Math.min(steps + 1, 3) : 0;
  return deliver(kind, now);
}

function deliver(kind: CueKind, now: number): boolean {
  const pattern = PATTERN[kind];
  playingUntil = now + pattern.reduce((a, b) => a + b, 0);
  playingRank = RANK[kind];
  let felt = false;
  try {
    if (canVibrate()) felt = navigator.vibrate(pattern);
  } catch {
    felt = false;
  }
  if (state.sound && ALARMS.has(kind)) felt = playTone(kind) || felt;
  return felt;
}

function showSignal(kind: CueKind, info: CueInfo, now: number): void {
  const cur = state.signal;
  const live = cur && now - cur.at < SIGNAL_MS && !cur.test;
  let next: Signal;
  if (live && cur.kind === kind) {
    const count = cur.count + 1;
    next = {
      ...cur,
      count,
      text: info.many ? info.many(count) : info.text || cur.text,
      ref: info.many ? undefined : info.ref,
      href: info.manyHref || info.href || cur.href,
      at: now,
    };
  } else if (live && RANK[cur.kind] > RANK[kind]) {
    // A stronger signal is up: it stays; this one is in the lists and the ticker.
    return;
  } else {
    next = { id: nextSignalId++, kind, count: 1, text: info.text || "", ref: info.ref, href: info.href, at: now, test: info.test };
  }
  emit({ signal: next });
  if (signalTimer != null) window.clearTimeout(signalTimer);
  signalTimer = null;
  // Every update restarts the time (the view restarts its drain line too).
  remaining = SIGNAL_MS;
  if (held) return;
  armSignalTimer(next.id, SIGNAL_MS);
}

/** A person's own action went through: a tick under the finger (never a signal). */
export function confirmCue(): void {
  cue("confirm", { critical: true });
}

/** What a test press does: the P1 alarm, felt, heard if sound is on, and its signal. */
export function testAlarm(): void {
  cue("alarm-p1", { text: "This is how a new P1 ticket arrives", test: true });
}

/* ------------------------------------------------------------------ sound --- */

let audioCtx: AudioContext | null = null;

function audio(): AudioContext | null {
  try {
    if (!audioCtx) {
      const AC: typeof AudioContext | undefined = window.AudioContext || (window as any).webkitAudioContext;
      if (!AC) return null;
      audioCtx = new AC();
    }
    if (audioCtx.state === "suspended") void audioCtx.resume().catch(() => undefined);
    return audioCtx;
  } catch {
    return null;
  }
}
function unlockAudio(): void {
  audio();
}

/** Each alarm's tone: [frequency Hz, start s, length s] notes, a wave and a level. */
const TONES: Partial<Record<CueKind, { notes: [number, number, number][]; wave: OscillatorType; gain: number }>> = {
  "alarm-p1": { notes: [[880, 0, 0.14], [660, 0.17, 0.16], [880, 0.5, 0.14], [660, 0.67, 0.2]], wave: "triangle", gain: 0.16 },
  "alarm-p2": { notes: [[740, 0, 0.12], [740, 0.18, 0.14]], wave: "triangle", gain: 0.12 },
  decision: { notes: [[523, 0, 0.12], [784, 0.13, 0.2]], wave: "sine", gain: 0.12 },
  escalation: { notes: [[440, 0, 0.22], [440, 0.32, 0.22]], wave: "triangle", gain: 0.13 },
  "run-failed": { notes: [[330, 0, 0.32]], wave: "sine", gain: 0.12 },
};

function playTone(kind: CueKind): boolean {
  const t = TONES[kind];
  const ac = audio();
  if (!t || !ac || ac.state !== "running") return false;
  try {
    const t0 = ac.currentTime + 0.01;
    for (const [freq, start, len] of t.notes) {
      const osc = ac.createOscillator();
      const g = ac.createGain();
      osc.type = t.wave;
      osc.frequency.value = freq;
      // A soft attack and release: no click at either end.
      g.gain.setValueAtTime(0, t0 + start);
      g.gain.linearRampToValueAtTime(t.gain, t0 + start + 0.012);
      g.gain.exponentialRampToValueAtTime(0.0001, t0 + start + len);
      osc.connect(g).connect(ac.destination);
      osc.start(t0 + start);
      osc.stop(t0 + start + len + 0.02);
    }
    return true;
  } catch {
    return false;
  }
}

/**
 * Once, from App: the first press or key on the page lets a later alarm sound, if sound is on
 * (browsers keep audio silent until then).
 */
export function installAlertUnlock(): () => void {
  const onGesture = () => {
    ensureLoaded();
    if (state.sound) unlockAudio();
  };
  window.addEventListener("pointerdown", onGesture, { capture: true, passive: true });
  window.addEventListener("keydown", onGesture, { capture: true });
  return () => {
    window.removeEventListener("pointerdown", onGesture, { capture: true });
    window.removeEventListener("keydown", onGesture, { capture: true });
  };
}

/* ---------------------------------------------------------- live events --- */

function str(v: unknown): string {
  return typeof v === "string" ? v.trim() : "";
}

/**
 * The cue for one live frame, if it has one. Steps and finished runs cue from the rail a person is
 * watching (components/AgentRail.tsx), not from here, so a storm's hundreds of steps on tickets
 * nobody has open stay silent.
 */
export function cueForEvent(ev: NocEvent): void {
  let critical = false;
  try {
    critical = isCriticalEvent(ev);
  } catch {
    critical = false;
  }
  const p = ev.payload || {};
  const number = str(p.incident_number);
  const incidentHref = ev.incidentId ? `/incidents/${encodeURIComponent(ev.incidentId)}` : "/incidents";
  switch (ev.type) {
    case "incident.created": {
      const prio = str(p.priority).toUpperCase();
      if (prio !== "P1" && prio !== "P2") return;
      cue(prio === "P1" ? "alarm-p1" : "alarm-p2", {
        text: `New ${prio} ticket`,
        many: (n) => `${n} new ${prio} tickets`,
        ref: number || undefined,
        href: incidentHref,
        manyHref: "/incidents",
        critical,
      });
      return;
    }
    case "agent.run.finished": {
      const status = str(p.status).toUpperCase();
      if (status === "WAITING_HITL") {
        cue("decision", {
          text: "A decision is waiting",
          many: (n) => `${n} new decisions waiting`,
          ref: number || undefined,
          href: "/hitl",
          critical: true,
        });
      } else if (status === "FAILED") {
        cue("run-failed", {
          text: "An agent run failed",
          many: (n) => `${n} agent runs failed`,
          ref: number || undefined,
          href: ev.incidentId ? incidentHref : "/agents",
          manyHref: "/agents",
          critical: true,
        });
      }
      return;
    }
    case "hitl.created":
      cue("decision", { text: "A decision is waiting", many: (n) => `${n} new decisions waiting`, ref: number || undefined, href: "/hitl", critical });
      return;
    case "support.escalated":
      cue("escalation", {
        text: "A support case needs a person",
        many: (n) => `${n} support cases need a person`,
        ref: str(p.ref) || undefined,
        href: "/support",
        critical,
      });
      return;
    case "support.still_down":
      cue("escalation", {
        text: "A customer says the service is still down",
        many: (n) => `${n} customers say the service is still down`,
        ref: number || undefined,
        href: "/support",
        critical,
      });
      return;
    case "support.surge":
    case "complaint.surge":
      cue("escalation", { text: "Complaints are surging", many: () => "Complaints are surging", href: "/support", critical });
      return;
    case "regulatory.deadline":
      cue("escalation", { text: "An Authority deadline is close", many: (n) => `${n} Authority deadlines are close`, href: "/mission", critical });
      return;
    case "email.failed":
    case "outbox.failed":
      cue("escalation", {
        text: "A message failed to send",
        many: (n) => `${n} messages failed to send`,
        ref: number || undefined,
        href: ev.incidentId ? incidentHref : "/audit",
        manyHref: "/audit",
        critical,
      });
      return;
    case "security.redaction_miss":
      cue("escalation", { text: "A redaction check found a miss", many: (n) => `${n} redaction misses`, href: "/audit", critical });
      return;
    case "scheduler.job_failed":
      cue("run-failed", {
        text: "A scheduled job failed",
        many: (n) => `${n} scheduled jobs failed`,
        href: "/audit",
        critical,
      });
      return;
    default:
      return;
  }
}

/** Test hook: forget the gaps and streaks and the signal (unit tests and the dev console). */
export function resetCues(): void {
  for (const k of Object.keys(lastAt) as CueKind[]) delete lastAt[k];
  for (const k of Object.keys(streak) as CueKind[]) delete streak[k];
  for (const k of Object.keys(seenAt) as CueKind[]) delete seenAt[k];
  playingUntil = 0;
  playingRank = 0;
  if (signalTimer != null) window.clearTimeout(signalTimer);
  signalTimer = null;
  emit({ signal: null });
}
