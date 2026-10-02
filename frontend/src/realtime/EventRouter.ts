import {
  FALLBACK_RENDERER,
  isCriticalEvent,
  normalizeEvent,
  rendererFor,
  type NocEvent,
  type RendererSpec,
  type Slice,
} from "./renderers";

/* ---------------------------------------------------------------- tuning --
 * A storm is dozens of frames per second. Two independent valves:
 *   1. refetch — slices are marked dirty the instant a frame arrives, then
 *      flushed on a debounce, so a burst costs one refetch per slice.
 *   2. ticker  — frames are revealed on a paced drain, batched under backlog.
 * Marking dirty is synchronous, so a ticker line dropped under load never
 * costs a refetch.
 */
export const DEBOUNCE_MS = 350;
export const QUIET_DEBOUNCE_MS = 900; // night shift, nothing critical pending
export const MAX_WAIT_MS = 1200; // a continuous stream still flushes this often

export const PENDING_MAX = 200; // hard cap on un-revealed frames; oldest dropped
export const REVEAL_MS = 90; // the original "feels live" stagger, small bursts
export const FIRST_REVEAL_MS = 40;
export const BURST_AFTER = 25; // above this backlog, reveal in batches
export const BURST_BATCH = 25;
export const BURST_REVEAL_MS = 120;
export const CATCHUP_DIVISOR = 8; // below BURST_AFTER, still clear the tail in a bounded time
export const QUIET_REVEAL_MS = 400;
export const COUNTER_MS = 1000; // counters publish at most once a second

export interface RouterHooks {
  /** One debounced flush. `slices` and/or `incidentIds` is non-empty. */
  onFlush(slices: Slice[], incidentIds: string[]): void;
  /** Ticker lines to prepend, newest first. */
  onReveal(batch: NocEvent[]): void;
  /** Running total of ticker lines held back by quiet mode. */
  onSuppressed(total: number): void;
  /** Running total of accepted frames — diagnostics only. */
  onReceived(total: number): void;
  /** Every accepted frame, the moment it arrives: unpaced, not gated by quiet mode. The agent
   *  rail reads runs from here (it paces its own replay); the ticker reads `onReveal`. */
  onFrame?(ev: NocEvent): void;
}

export interface RouterOptions {
  quiet?: boolean;
  now?: () => number;
  setTimer?: (fn: () => void, ms: number) => number;
  clearTimer?: (id: number) => void;
}

/** Sentinel id: an unknown event with no incident_id must reload any open workspace. */
export const ANY_INCIDENT = "*";

/**
 * The whole of defect #26's fix, with no React in it — which is also why it can
 * be driven by a virtual clock in a test.
 *
 * Every public method is total: a malformed frame, an unknown event type or a
 * throwing hook is swallowed, because a WS feed must never be able to take the
 * wallboard down.
 */
export class EventRouter {
  private readonly hooks: RouterHooks;
  private readonly now: () => number;
  private readonly setTimer: (fn: () => void, ms: number) => number;
  private readonly clearTimer: (id: number) => void;

  private quiet: boolean;
  private disposed = false;

  private dirtySlices = new Set<Slice>();
  private dirtyIncidents = new Set<string>();
  private dirtyCritical = false;
  private firstDirtyAt: number | null = null;
  private flushTimer: number | null = null;

  private pending: NocEvent[] = [];
  private revealTimer: number | null = null;

  private suppressedTotal = 0;
  private suppressedTimer: number | null = null;
  private receivedTotal = 0;
  private receivedTimer: number | null = null;

  constructor(hooks: RouterHooks, opts: RouterOptions = {}) {
    this.hooks = hooks;
    this.quiet = opts.quiet ?? false;
    this.now = opts.now ?? (() => Date.now());
    this.setTimer = opts.setTimer ?? ((fn, ms) => window.setTimeout(fn, ms) as unknown as number);
    this.clearTimer = opts.clearTimer ?? ((id) => window.clearTimeout(id));
  }

  setQuiet(on: boolean): void {
    this.quiet = on;
    if (!on) {
      this.suppressedTotal = 0;
      this.safely(() => this.hooks.onSuppressed(0));
    }
  }

  isQuiet(): boolean {
    return this.quiet;
  }

  /** Un-revealed ticker frames — diagnostics / tests. */
  pendingCount(): number {
    return this.pending.length;
  }

  /** One raw WS frame in. Never throws. */
  handle(raw: any): void {
    if (this.disposed) return;

    let ev: NocEvent | null = null;
    try {
      ev = normalizeEvent(raw);
    } catch {
      ev = null;
    }
    if (!ev) return;

    let spec: RendererSpec;
    try {
      spec = rendererFor(ev.type);
    } catch {
      spec = FALLBACK_RENDERER;
    }

    let critical = false;
    try {
      critical = isCriticalEvent(ev, spec);
    } catch {
      critical = false;
    }

    this.noteReceived();
    if (this.hooks.onFrame) this.safely(() => this.hooks.onFrame?.(ev as NocEvent));
    this.safely(() => this.markDirty(ev as NocEvent, spec, critical));

    if (!spec.ticker) return;
    if (this.quiet && !critical) {
      this.noteSuppressed();
      return;
    }
    this.enqueueTicker(ev);
  }

  dispose(): void {
    this.disposed = true;
    for (const t of [this.flushTimer, this.revealTimer, this.suppressedTimer, this.receivedTimer]) {
      if (t != null) this.safely(() => this.clearTimer(t));
    }
    this.flushTimer = null;
    this.revealTimer = null;
    this.suppressedTimer = null;
    this.receivedTimer = null;
    this.pending = [];
    this.dirtySlices.clear();
    this.dirtyIncidents.clear();
  }

  // ------------------------------------------------------------- internals --

  private safely(fn: () => void): void {
    try {
      fn();
    } catch {
      /* a hook or a host timer must never break the feed */
    }
  }

  private markDirty(ev: NocEvent, spec: RendererSpec, critical: boolean): void {
    for (const s of spec.slices) this.dirtySlices.add(s);
    if (spec.incidentScoped) {
      if (ev.incidentId) this.dirtyIncidents.add(ev.incidentId);
      else if (spec === FALLBACK_RENDERER) this.dirtyIncidents.add(ANY_INCIDENT);
    }
    if (critical) this.dirtyCritical = true;
    this.scheduleFlush();
  }

  private scheduleFlush(): void {
    if (this.dirtySlices.size === 0 && this.dirtyIncidents.size === 0) return;
    const t = this.now();
    if (this.firstDirtyAt == null) this.firstDirtyAt = t;
    const base = this.dirtyCritical || !this.quiet ? DEBOUNCE_MS : QUIET_DEBOUNCE_MS;
    const elapsed = t - this.firstDirtyAt;
    const wait = Math.max(0, Math.min(base, MAX_WAIT_MS - elapsed));
    if (this.flushTimer != null) this.safely(() => this.clearTimer(this.flushTimer as number));
    this.flushTimer = this.setTimer(() => this.flush(), wait);
  }

  private flush(): void {
    this.flushTimer = null;
    this.firstDirtyAt = null;
    this.dirtyCritical = false;
    const slices = Array.from(this.dirtySlices);
    const incidents = Array.from(this.dirtyIncidents);
    this.dirtySlices.clear();
    this.dirtyIncidents.clear();
    if (slices.length === 0 && incidents.length === 0) return;
    this.safely(() => this.hooks.onFlush(slices, incidents));
  }

  private enqueueTicker(ev: NocEvent): void {
    this.pending.push(ev);
    if (this.pending.length > PENDING_MAX) {
      this.pending.splice(0, this.pending.length - PENDING_MAX); // drop oldest
    }
    if (this.revealTimer == null) {
      this.revealTimer = this.setTimer(() => this.drain(), FIRST_REVEAL_MS);
    }
  }

  private drain(): void {
    if (this.disposed) return;
    if (this.pending.length === 0) {
      this.revealTimer = null;
      return;
    }
    // One line every 90 ms is the "feels live" stagger and it stays exactly that
    // while the feed is quiet. Under backlog the batch grows with the queue, so
    // the tail of a storm clears in a bounded time instead of trickling out at
    // 11 lines/sec long after the storm ended.
    const take =
      this.pending.length > BURST_AFTER
        ? Math.min(this.pending.length, BURST_BATCH)
        : Math.max(1, Math.ceil(this.pending.length / CATCHUP_DIVISOR));
    const burst = take > 1;
    const batch = this.pending.splice(0, take);
    const newestFirst = batch.slice().reverse();
    this.safely(() => this.hooks.onReveal(newestFirst));
    const delay = this.quiet ? QUIET_REVEAL_MS : burst ? BURST_REVEAL_MS : REVEAL_MS;
    this.revealTimer = this.setTimer(() => this.drain(), delay);
  }

  private noteSuppressed(): void {
    this.suppressedTotal += 1;
    if (this.suppressedTimer == null) {
      // The counter itself must not churn — that would defeat quiet mode.
      this.suppressedTimer = this.setTimer(() => {
        this.suppressedTimer = null;
        this.safely(() => this.hooks.onSuppressed(this.suppressedTotal));
      }, COUNTER_MS);
    }
  }

  private noteReceived(): void {
    this.receivedTotal += 1;
    if (this.receivedTimer == null) {
      this.receivedTimer = this.setTimer(() => {
        this.receivedTimer = null;
        this.safely(() => this.hooks.onReceived(this.receivedTotal));
      }, COUNTER_MS);
    }
  }
}
