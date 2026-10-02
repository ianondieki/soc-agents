import { useEffect, useState, type CSSProperties } from "react";

/**
 * Small layout helpers shared by list pages. In a plain module, not in a page file, so a page
 * that needs them does not pull another page's chunk (Mission control's first paint stays
 * inside its JS budget).
 */

/** Phones: below this width a ticket list reads as two stacked lines per row, not a sideways table. */
export const NARROW_QUERY = "(max-width: 600px)";

/** A `.row` whose one child takes the whole width (the stacked phone rows). */
export const ONE_COL: CSSProperties = { gridTemplateColumns: "minmax(0, 1fr)" };

/**
 * True while the viewport matches `query` (default: phone width). Shared by the Incident board
 * and the Shift desk, which swap their table for stacked rows on a phone.
 */
export function useNarrow(query: string = NARROW_QUERY): boolean {
  const read = () => typeof window !== "undefined" && typeof window.matchMedia === "function" && window.matchMedia(query).matches;
  const [narrow, setNarrow] = useState<boolean>(read);
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const mq = window.matchMedia(query);
    const sync = () => setNarrow(mq.matches);
    sync();
    if (typeof mq.addEventListener === "function") {
      mq.addEventListener("change", sync);
      return () => mq.removeEventListener("change", sync);
    }
    mq.addListener(sync); // Safari < 14
    return () => mq.removeListener(sync);
  }, [query]);
  return narrow;
}
