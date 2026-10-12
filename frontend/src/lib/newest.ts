import { useCallback, useRef } from "react";

/**
 * Overlapping fetches of one list (a storm's flushes, the heartbeat and a reconnect at once) may
 * answer out of order; only the newest request may write. Call the returned function as a request
 * starts: it hands back `fresh()`, true only while no later request of this kind has begun.
 *
 *   const turn = useNewest();
 *   const fresh = turn();
 *   api.incidents().then((rows) => { if (fresh()) setRows(rows); });
 */
export function useNewest(): () => () => boolean {
  const seq = useRef(0);
  return useCallback(() => {
    const mine = ++seq.current;
    return () => mine === seq.current;
  }, []);
}
