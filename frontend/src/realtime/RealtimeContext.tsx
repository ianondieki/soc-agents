import { createContext, useContext, type ReactNode } from "react";
import type { RealtimeState } from "./useRealtime";

/**
 * Makes the renderer-table state reachable from a route component without
 * threading a prop through every page. Only `IncidentWorkspace` needs it today
 * (it subscribes to one incident's revision), so the context is deliberately
 * thin and every consumer degrades to "no realtime" rather than throwing.
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
