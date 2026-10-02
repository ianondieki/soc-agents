import { createContext, useContext, useSyncExternalStore, type ReactNode } from "react";
import type { NocEvent } from "./renderers";
import type { RealtimeState } from "./useRealtime";

/**
 * Makes the realtime state reachable from a route component without threading a prop through
 * every page. Every consumer degrades to "no realtime" rather than throwing.
 *
 * The context value changes on a debounced flush (a revision moved, the link changed), never on
 * a frame. The ticker lines, the quiet-mode count and the run frames live in `state.feed`, an
 * external store; only the components that call the hooks below re-render when it moves.
 */
const RealtimeCtx = createContext<RealtimeState | null>(null);

export function RealtimeProvider({
  value,
  children,
}: {
  value: RealtimeState;
  children: ReactNode;
}) {
  return <RealtimeCtx.Provider value={value}>{children}</RealtimeCtx.Provider>;
}

/** `null` outside a provider — callers must cope, never assume. */
export function useRealtimeState(): RealtimeState | null {
  return useContext(RealtimeCtx);
}

/**
 * Revision for one incident. Returns 0 with no provider, so a page mounted
 * outside the tree still loads once and simply does not live-update.
 */
export function useIncidentRevision(id?: string | null): number {
  const rt = useContext(RealtimeCtx);
  if (!rt) return 0;
  try {
    return rt.incidentRevision(id);
  } catch {
    return 0;
  }
}

/** Quiet mode for pages that want to drop their own animation/polling churn. */
export function useQuietMode(): boolean {
  const rt = useContext(RealtimeCtx);
  return rt?.quietMode ?? false;
}

const NO_EVENTS: NocEvent[] = [];
const noSubscribe = () => () => undefined;
const noEvents = () => NO_EVENTS;
const zero = () => 0;

/** The ticker: newest first, the latest 100 lines (step-started frames are not ticker lines). */
export function useTickerEvents(): NocEvent[] {
  const feed = useContext(RealtimeCtx)?.feed;
  return useSyncExternalStore(feed ? feed.subscribeTicker : noSubscribe, feed ? feed.getEvents : noEvents);
}

/** Ticker lines quiet mode held back. */
export function useSuppressedCount(): number {
  const feed = useContext(RealtimeCtx)?.feed;
  return useSyncExternalStore(feed ? feed.subscribeTicker : noSubscribe, feed ? feed.getSuppressed : zero);
}

/** Agent step / run / incident frames, oldest first, the moment they arrive (unpaced). */
export function useRunFrames(): NocEvent[] {
  const feed = useContext(RealtimeCtx)?.feed;
  return useSyncExternalStore(feed ? feed.subscribeFrames : noSubscribe, feed ? feed.getFrames : noEvents);
}
