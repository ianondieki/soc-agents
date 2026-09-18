import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useOpsSocket } from "../hooks/useOpsSocket";
import { ANY_INCIDENT, EventRouter } from "./EventRouter";
import { zeroRevisions, type NocEvent, type Revisions, type Slice } from "./renderers";

const TICKER_MAX = 100; // the ticker only ever shows 100 lines
const INCIDENT_REV_CAP = 500; // prune the per-incident map on a long shift
const QUIET_KEY = "noc_quiet_mode_v1";

function loadQuietMode(): boolean {
  try {
    return window.localStorage.getItem(QUIET_KEY) === "1";
  } catch {
    return false; // private window / blocked storage
  }
}

export interface RealtimeState {
  connected: boolean;
  /** Newest first, capped at 100 — what the Mission Control ticker renders. */
  events: NocEvent[];
  /** Per-slice counters; a page depends on the one slice it reads. */
  revisions: Revisions;
  /** Revision for one incident: events naming it, plus the global fallback. */
  incidentRevision: (id?: string | null) => number;
  quietMode: boolean;
  setQuietMode: (on: boolean) => void;
  /** Ticker lines held back by quiet mode. */
  suppressed: number;
  /** Total frames accepted — diagnostics only. */
  received: number;
}

/**
 * React binding for `EventRouter` (which holds the renderer table, the debounce
 * and the quiet-mode gate). All this layer does is turn the router's callbacks
 * into state a component can depend on.
 */
export function useRealtime(): RealtimeState {
  const [events, setEvents] = useState<NocEvent[]>([]);
  const [revisions, setRevisions] = useState<Revisions>(zeroRevisions);
  const [incidentRevs, setIncidentRevs] = useState<Record<string, number>>({});
  const [quietMode, setQuietModeState] = useState<boolean>(loadQuietMode);
  const [suppressed, setSuppressed] = useState(0);
  const [received, setReceived] = useState(0);

  const routerRef = useRef<EventRouter | null>(null);
  if (routerRef.current == null) {
    routerRef.current = new EventRouter(
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
        onReveal(batch: NocEvent[]) {
          setEvents((prev) => [...batch, ...prev].slice(0, TICKER_MAX));
        },
        onSuppressed(total: number) {
          setSuppressed(total);
        },
        onReceived(total: number) {
          setReceived(total);
        },
      },
      { quiet: loadQuietMode() }
    );
  }

  const handleMessage = useCallback((raw: any) => {
    routerRef.current?.handle(raw);
  }, []);

  const { connected } = useOpsSocket(handleMessage);

  useEffect(() => {
    const router = routerRef.current;
    return () => router?.dispose();
  }, []);

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

  return useMemo(
    () => ({
      connected,
      events,
      revisions,
      incidentRevision,
      quietMode,
      setQuietMode,
      suppressed,
      received,
    }),
    [connected, events, revisions, incidentRevision, quietMode, setQuietMode, suppressed, received]
  );
}
