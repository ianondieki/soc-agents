import { Component, type ErrorInfo, type ReactNode } from "react";

/**
 * One card's blast radius.
 *
 * React unmounts the whole tree on an uncaught render error, so without this a
 * single malformed HITL payload would replace the entire inbox with a blank
 * page — the exact failure the inbox may never have. `lib/hitl.ts` is written
 * so that cannot happen, but "written so it cannot happen" is not a guarantee;
 * this is.
 *
 * Deliberately not a shared app-wide boundary: the point is that card N+1 still
 * renders, and still has working Approve/Reject buttons, when card N cannot.
 *
 * Once tripped it stays tripped for that card. Remounting on new data would
 * loop if the data is what is broken, and a stuck fallback that shows the raw
 * payload is more useful at 03:00 than a card that flickers.
 */
export class CardBoundary extends Component<
  { fallback: ReactNode; children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };

  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    try {
      // Console only. A failed card must not trigger a fetch, a toast or a
      // state write anywhere else in the tree.
      console.error("HITL card failed to render", error, info?.componentStack);
    } catch {
      /* console itself is unavailable — nothing to do, and nothing to throw */
    }
  }

  render(): ReactNode {
    return this.state.failed ? this.props.fallback : this.props.children;
  }
}

export default CardBoundary;
