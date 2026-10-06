import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useOpsSocket } from "../hooks/useOpsSocket";
import { ANY_INCIDENT, EventRouter } from "./EventRouter";
import { EventFeed } from "./feed";
import { zeroRevisions, type Revisions, type Slice } from "./renderers";
import { firstAnswerServerMs, onFirstAnswer, serverNow } from "./apiHealth";
import { parseInstant } from "../lib/time";
import { cueForEvent } from "../lib/feedback";

const INCIDENT_REV_CAP = 500; // prune the per-incident map on a long shift
const QUIET_KEY = "noc_quiet_mode_v1";
/** How long a first connection may take before the top bar calls the stream down. */
const CONNECT_GRACE_MS = 3000;

function loadQuietMode(): boolean {
  try {
    const stored = window.localStorage.getItem(QUIET_KEY);
    if (stored === "1" || stored === "0") return stored === "1";
  } catch {
    /* private window / blocked storage: fall through */
  }
  // Nothing stored (or storage blocked): what index.html's bootstrap set, which is on for a
  // `?display=projector` link (projector turns quiet mode on).
  try {
    return document.documentElement.getAttribute("data-quiet") === "on";
  } catch {
    return false;
  }
}

/** How long a new connection's frames wait for the first REST answer before going through. */
const HOLD_MAX_MS = 10_000;
/** Frames held at most while waiting (the replay is 15; a storm adds a few dozen a second). */
const HOLD_CAP = 400;

/** A frame's server time (ms), from either envelope shape; null when it has none. */
function frameTime(raw: any): number | null {
  const env = raw && typeof raw === "object" && raw.event && typeof raw.event === "object" ? raw.event : raw;
  return parseInstant(env?.ts)?.getTime() ?? null;
}

/**
 * Where a connection's replay ends. The server sends its recent frames again on every connect;
 * those predate the connection, and the lists each page loads on mount already include what they
 * did. A frame stamped before the cut is history: it bumps no revision and draws no run.
 *
 *  - The cut is the moment the connection opened, on the server's clock: realtime/apiHealth.ts
 *    reads that clock from the REST answers' `Date` headers, so a wrong clock on this machine
 *    changes nothing. The estimate errs early (the header has one-second resolution and the
 *    answer's travel time is not subtracted), so a frame from the last second or two before the
 *    connection can still count as live and cost one refetch; a live frame is never history.
 *  - On the first connection the cut is never later than the first REST answer
 *    (`min(socket open, first answer)`, both on the server's clock): the pages' mount loads were
 *    asked around that answer, so a frame stamped after it may be news to them, and it counts as
 *    live even when the socket opened later. A reconnect cuts at its own open, as before (App
 *    refetches every list when the stream comes back).
 *  - A connection that opens before any REST answer has arrived (the first seconds on a slow link)
 *    holds its frames until the first answer gives the server's clock, or for 10 s at most.
 */
interface ReplayGate {
  cut: number | null;
  /** A connection has opened before: the next one is a reconnect. */
  opened: boolean;
  holding: boolean;
  held: any[];
  openedAt: number;
  cancelWait: (() => void) | null;
  timer: number | null;
}

/** The event stream as the top bar names it: still connecting (the first ~3 s), live, or down
 *  (it was live and dropped, or never came up). */
export type LinkState = "connecting" | "live" | "down";

export interface RealtimeState {
  connected: boolean;
  link: LinkState;
  /** Per-slice counters; a page depends on the one slice it reads. */
  revisions: Revisions;
  /** Revision for one incident: events naming it, plus the global fallback. */
  incidentRevision: (id?: string | null) => number;
  quietMode: boolean;
  setQuietMode: (on: boolean) => void;
  /**
   * The ticker lines, the quiet-mode count and the run frames, as an external store: read them
   * with `useTickerEvents` / `useSuppressedCount` / `useRunFrames` (RealtimeContext), which
   * re-render only the component that reads them. Stable for the life of the app.
   */
  feed: EventFeed;
  /** Total frames accepted — diagnostics only. A ref behind a getter: reading it never renders. */
  received: () => number;
}

/**
 * React binding for `EventRouter` (which holds the renderer table, the debounce and the
 * quiet-mode gate). This layer turns the router's flushes into the revision counters pages
 * depend on, and hands frames and ticker lines to the `EventFeed` store, so App re-renders for
 * a debounced flush, never for a frame.
 *
 * The router is created inside the effect, not during render: React StrictMode mounts, cleans
 * up and mounts again in development, and a router made once in render would be disposed by the
 * first cleanup and dead for the second mount.
 */
export function useRealtime(): RealtimeState {
  const [revisions, setRevisions] = useState<Revisions>(zeroRevisions);
  const [incidentRevs, setIncidentRevs] = useState<Record<string, number>>({});
  const [quietMode, setQuietModeState] = useState<boolean>(loadQuietMode);
  const quietRef = useRef(quietMode);
  quietRef.current = quietMode;
  const receivedRef = useRef(0);

  const feedRef = useRef<EventFeed | null>(null);
  if (feedRef.current == null) feedRef.current = new EventFeed();
  const feed = feedRef.current;

  const routerRef = useRef<EventRouter | null>(null);
  useEffect(() => {
    const router = new EventRouter(
      {
        onFlush(slices: Slice[], incidentIds: string[]) {
          if (slices.length > 0) {
            setRevisions((prev) => {
              const next = { ...prev };
              for (const s of slices) next[s] = (next[s] ?? 0) + 1;
              return next;
            });
          }
          if (incidentIds.length > 0) {
            setIncidentRevs((prev) => {
              let next: Record<string, number> = { ...prev };
              if (Object.keys(next).length > INCIDENT_REV_CAP) {
                // Long shift, thousands of incidents: keep the global counter and
                // the ids in flight. A pruned id restarts at 0, which still reads
                // as "the dependency changed" and costs at most one extra fetch.
                const kept: Record<string, number> = {};
                const anyRev = next[ANY_INCIDENT];
                if (anyRev !== undefined) kept[ANY_INCIDENT] = anyRev;
                next = kept;
              }
              for (const id of incidentIds) next[id] = (next[id] ?? 0) + 1;
              return next;
            });
          }
        },
        onReveal(batch) {
          feed.reveal(batch);
        },
        onSuppressed(total: number) {
          feed.setSuppressed(total);
        },
        onReceived(total: number) {
          receivedRef.current = total;
        },
        onFrame(ev) {
          feed.push(ev);
          // A live frame worth a person's attention buzzes, sounds (if asked) and signals; the
          // replay on connect never reaches here (lib/feedback.ts).
          cueForEvent(ev);
        },
      },
      { quiet: quietRef.current }
    );
    routerRef.current = router;
    return () => {
      router.dispose();
      if (routerRef.current === router) routerRef.current = null;
    };
  }, [feed]);

  const gateRef = useRef<ReplayGate>({
    cut: null,
    opened: false,
    holding: false,
    held: [],
    openedAt: 0,
    cancelWait: null,
    timer: null,
  });

  const deliver = useCallback((raw: any) => {
    const cut = gateRef.current.cut;
    const at = frameTime(raw);
    routerRef.current?.handle(raw, { history: cut != null && at != null && at < cut });
  }, []);

  const release = useCallback(() => {
    const g = gateRef.current;
    g.cancelWait?.();
    g.cancelWait = null;
    if (g.timer != null) window.clearTimeout(g.timer);
    g.timer = null;
    g.holding = false;
    const held = g.held;
    g.held = [];
    for (const raw of held) deliver(raw);
  }, [deliver]);

  const handleOpen = useCallback(() => {
    const g = gateRef.current;
    // Frames still held from a connection that dropped predate this one as well.
    if (g.holding) {
      if (g.cut == null) g.cut = Number.POSITIVE_INFINITY;
      release();
    }
    const first = !g.opened;
    g.opened = true;
    g.openedAt = Date.now();
    // The first connection's cut is never after the first REST answer; a reconnect's is its open.
    const cutAt = (openServerMs: number) => {
      const answered = first ? firstAnswerServerMs() : null;
      return answered != null ? Math.min(openServerMs, answered) : openServerMs;
    };
    const now = serverNow();
    if (now != null) {
      g.cut = cutAt(now);
      return;
    }
    g.holding = true;
    g.cut = null;
    g.cancelWait = onFirstAnswer(() => {
      const est = serverNow();
      g.cut = cutAt(est != null ? g.openedAt + (est - Date.now()) : g.openedAt);
      release();
    });
    g.timer = window.setTimeout(() => {
      // No answer in 10 s: this machine's clock is all there is.
      g.cut = cutAt(g.openedAt);
      release();
    }, HOLD_MAX_MS);
  }, [release]);

  const handleMessage = useCallback(
    (raw: any) => {
      const g = gateRef.current;
      if (g.holding) {
        g.held.push(raw);
        if (g.held.length > HOLD_CAP) g.held.shift();
        return;
      }
      deliver(raw);
    },
    [deliver]
  );

  useEffect(
    () => () => {
      const g = gateRef.current;
      g.cancelWait?.();
      if (g.timer != null) window.clearTimeout(g.timer);
    },
    []
  );

  const { connected } = useOpsSocket(handleMessage, handleOpen);

  // "Connecting" for the first few seconds, so the top bar never flashes red before the first
  // connection has had a chance; "down" once it was live and dropped, or never came up.
  const [wasLive, setWasLive] = useState(false);
  const [graceOver, setGraceOver] = useState(false);
  useEffect(() => {
    if (connected) setWasLive(true);
  }, [connected]);
  useEffect(() => {
    const t = window.setTimeout(() => setGraceOver(true), CONNECT_GRACE_MS);
    return () => window.clearTimeout(t);
  }, []);
  const link: LinkState = connected ? "live" : wasLive || graceOver ? "down" : "connecting";

  const setQuietMode = useCallback((on: boolean) => {
    setQuietModeState(on);
    routerRef.current?.setQuiet(on);
    try {
      window.localStorage.setItem(QUIET_KEY, on ? "1" : "0");
    } catch {
      /* storage blocked — quiet mode still works for this session */
    }
  }, []);

  const incidentRevision = useCallback(
    (id?: string | null) => {
      const any = incidentRevs[ANY_INCIDENT] ?? 0;
      if (!id) return any;
      return any + (incidentRevs[id] ?? 0);
    },
    [incidentRevs]
  );

  const received = useCallback(() => receivedRef.current, []);

  return useMemo(
    () => ({
      connected,
      link,
      revisions,
      incidentRevision,
      quietMode,
      setQuietMode,
      feed,
      received,
    }),
    [connected, link, revisions, incidentRevision, quietMode, setQuietMode, feed, received]
  );
}
