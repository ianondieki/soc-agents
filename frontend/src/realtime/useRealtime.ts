import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useOpsSocket } from "../hooks/useOpsSocket";
import { ANY_INCIDENT, EventRouter } from "./EventRouter";
import { EventFeed } from "./feed";
import { zeroRevisions, type Revisions, type Slice } from "./renderers";

const INCIDENT_REV_CAP = 500; // prune the per-incident map on a long shift
const QUIET_KEY = "noc_quiet_mode_v1";
/** How long a first connection may take before the top bar calls the stream down. */
const CONNECT_GRACE_MS = 3000;

function loadQuietMode(): boolean {
  try {
    return window.localStorage.getItem(QUIET_KEY) === "1";
  } catch {
    return false; // private window / blocked storage
  }
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

  const handleMessage = useCallback((raw: any) => {
    routerRef.current?.handle(raw);
  }, []);

  const { connected } = useOpsSocket(handleMessage);

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
