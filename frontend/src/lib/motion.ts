import { type RefObject, useEffect, useRef, useState } from "react";
import { useQuietMode } from "../realtime/RealtimeContext";

/**
 * Motion helpers for pages that move on their own (docs/DESIGN_SYSTEM.md, Motion). Anything that
 * moves without being asked stops for the operating system's reduced-motion setting and for the
 * floor's quiet mode; `useCalm()` is the one switch a page reads.
 */

const REDUCED = "(prefers-reduced-motion: reduce)";

/** True while the operating system asks for reduced motion; follows the setting live. */
export function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(() => {
    try {
      return window.matchMedia(REDUCED).matches;
    } catch {
      return false;
    }
  });
  useEffect(() => {
    let mq: MediaQueryList;
    try {
      mq = window.matchMedia(REDUCED);
    } catch {
      return;
    }
    const on = () => setReduced(mq.matches);
    mq.addEventListener?.("change", on);
    return () => mq.removeEventListener?.("change", on);
  }, []);
  return reduced;
}

/** Quiet mode or reduced motion: draw the end state, move nothing. */
export function useCalm(): boolean {
  const quiet = useQuietMode();
  const reduced = usePrefersReducedMotion();
  return quiet || reduced;
}

/**
 * A number that counts up to `target` once, over `ms`, easing out. Calm, or no target yet: the
 * target itself. A later change of target counts on from where the number stands.
 */
export function useCountUp(target: number | null | undefined, calm: boolean, ms = 900): number | null {
  const [shown, setShown] = useState<number | null>(calm || target == null ? (target ?? null) : 0);
  const from = useRef(0);
  useEffect(() => {
    if (target == null) {
      setShown(null);
      return;
    }
    if (calm) {
      from.current = target;
      setShown(target);
      return;
    }
    const start = performance.now();
    const a = from.current;
    let raf = 0;
    const tick = (t: number) => {
      const k = Math.min(1, (t - start) / ms);
      const eased = 1 - Math.pow(1 - k, 3);
      const v = a + (target - a) * eased;
      setShown(v);
      if (k < 1) raf = requestAnimationFrame(tick);
      else from.current = target;
    };
    raf = requestAnimationFrame(tick);
    return () => {
      cancelAnimationFrame(raf);
      from.current = target;
    };
  }, [target, calm, ms]);
  return shown;
}

/** True once the element has come at least `threshold` into view (and stays true). */
export function useSeenOnce<T extends Element>(threshold = 0.2): [RefObject<T>, boolean] {
  const ref = useRef<T>(null);
  const [seen, setSeen] = useState(false);
  useEffect(() => {
    const el = ref.current;
    if (!el || seen) return;
    if (typeof IntersectionObserver === "undefined") {
      setSeen(true);
      return;
    }
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          setSeen(true);
          io.disconnect();
        }
      },
      { threshold },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [seen, threshold]);
  return [ref, seen];
}
