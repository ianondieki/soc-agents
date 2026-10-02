import type { NocEvent } from "./renderers";

/** The ticker keeps the latest 100 lines. */
export const TICKER_MAX = 100;
/** Frames kept for the agent rail: a storm is about 170, so this holds two of them. */
export const FRAMES_MAX = 400;
/** A burst of frames (a whole run lands at once) is announced to the rail once, not 26 times. */
const FRAME_NOTIFY_MS = 30;

/** Frames the rail draws a run from. */
const RUN_FRAME_TYPES: ReadonlySet<string> = new Set([
  "agent.step.started",
  "agent.step.completed",
  "agent.run.finished",
  "incident.created",
  "incident.merged",
  "incident.cascade_child",
]);

type Listener = () => void;

/**
 * The live feed as an external store, so the few components that read it (the ticker, the rail,
 * the redaction chip) re-render when it moves and nothing else does: App holds one stable
 * EventFeed and never re-renders for a frame.
 *
 * Two channels:
 *  - the ticker: lines the router reveals at its paced rate, newest first, capped at 100, plus
 *    the count quiet mode held back;
 *  - run frames: every agent step / run / incident frame the moment it arrives, oldest first,
 *    for the rail, which paces its own hop-by-hop replay.
 * `tap` hands every frame (any type) to a callback without subscribing a component, for App's
 * few counters (the guide's "approved since open", the newest ticket).
 */
export class EventFeed {
  private events: NocEvent[] = [];
  private suppressed = 0;
  private frames: NocEvent[] = [];
  private readonly tickerListeners = new Set<Listener>();
  private readonly frameListeners = new Set<Listener>();
  private readonly taps = new Set<(ev: NocEvent) => void>();
  private frameTimer: number | null = null;

  // ------------------------------------------------------------------ ticker --
  subscribeTicker = (fn: Listener): (() => void) => {
    this.tickerListeners.add(fn);
    return () => {
      this.tickerListeners.delete(fn);
    };
  };
  getEvents = (): NocEvent[] => this.events;
  getSuppressed = (): number => this.suppressed;

  reveal(batch: NocEvent[]): void {
    if (batch.length === 0) return;
    this.events = [...batch, ...this.events].slice(0, TICKER_MAX);
    this.emit(this.tickerListeners);
  }

  setSuppressed(total: number): void {
    if (total === this.suppressed) return;
    this.suppressed = total;
    this.emit(this.tickerListeners);
  }

  // ------------------------------------------------------------------ frames --
  subscribeFrames = (fn: Listener): (() => void) => {
    this.frameListeners.add(fn);
    return () => {
      this.frameListeners.delete(fn);
    };
  };
  getFrames = (): NocEvent[] => this.frames;

  tap(fn: (ev: NocEvent) => void): () => void {
    this.taps.add(fn);
    return () => {
      this.taps.delete(fn);
    };
  }

  push(ev: NocEvent): void {
    for (const fn of Array.from(this.taps)) {
      try {
        fn(ev);
      } catch {
        /* a counter must never break the feed */
      }
    }
    if (!RUN_FRAME_TYPES.has(ev.type)) return;
    const next = this.frames.length >= FRAMES_MAX ? this.frames.slice(this.frames.length - FRAMES_MAX + 1) : this.frames.slice();
    next.push(ev);
    this.frames = next;
    if (this.frameTimer == null) {
      this.frameTimer = window.setTimeout(() => {
        this.frameTimer = null;
        this.emit(this.frameListeners);
      }, FRAME_NOTIFY_MS);
    }
  }

  private emit(set: Set<Listener>): void {
    for (const fn of Array.from(set)) {
      try {
        fn();
      } catch {
        /* one listener must not starve the others */
      }
    }
  }
}
