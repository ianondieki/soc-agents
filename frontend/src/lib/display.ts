import { useEffect, useState } from "react";
import { flushSync } from "react-dom";
import { fmtDate, fmtHM } from "./time";

/**
 * How the screen is being shown: on a desk (standard), on a washed-out projector or a TV across
 * the room (projector), or on paper (print).
 *
 * Projector mode is `<html data-display="projector">`. index.html's bootstrap sets it before the
 * first paint from `?display=projector` in the URL (stored, so the TV keeps it) or from what was
 * stored here, so neither the shell nor the Wallboard flashes the desk look first. The tokens it
 * changes live in styles.css under `:root[data-display="projector"]`.
 */

export type DisplayMode = "standard" | "projector";

/** The localStorage key index.html's bootstrap reads too: keep the two in step. */
export const DISPLAY_KEY = "noc_display_v1";

/** What each display toggle does, said once in words (Settings) as well as in the toggle's title,
 *  so a finger on a tablet, which never sees a title, can still read it. */
export const QUIET_MEANING =
  "Night shift: stop animations and non-critical ticker churn. P1 and P2 tickets and decisions stay live.";
export const PROJECTOR_MEANING =
  "Larger type, brighter text and heavier lines for a projector or a TV across the room; it turns quiet mode on too. A link ending in ?display=projector opens a screen that way.";

export function readDisplay(): DisplayMode {
  try {
    return document.documentElement.getAttribute("data-display") === "projector" ? "projector" : "standard";
  } catch {
    return "standard";
  }
}

/** Quiet mode as it was before projector mode turned it on ("1"/"0"); index.html writes it too. */
export const QUIET_BEFORE_PROJECTOR_KEY = "noc_quiet_before_projector_v1";

export function rememberQuietBeforeProjector(quiet: boolean): void {
  try {
    window.localStorage.setItem(QUIET_BEFORE_PROJECTOR_KEY, quiet ? "1" : "0");
  } catch {
    /* storage blocked: leaving projector mode then keeps quiet mode on */
  }
}

/** What quiet mode was before projector mode (null when unknown), forgetting it. */
export function recallQuietBeforeProjector(): boolean | null {
  try {
    const v = window.localStorage.getItem(QUIET_BEFORE_PROJECTOR_KEY);
    window.localStorage.removeItem(QUIET_BEFORE_PROJECTOR_KEY);
    return v === "1" ? true : v === "0" ? false : null;
  } catch {
    return null;
  }
}

/** Switch the display mode now and remember it for this browser. */
export function applyDisplay(mode: DisplayMode): void {
  try {
    const root = document.documentElement;
    if (mode === "projector") root.setAttribute("data-display", "projector");
    else root.removeAttribute("data-display");
  } catch {
    /* ignore */
  }
  try {
    window.localStorage.setItem(DISPLAY_KEY, mode);
  } catch {
    /* storage blocked: the mode lasts until the page is reloaded */
  }
}

/**
 * True while the page is being printed: between `beforeprint` and `afterprint`, and while the print
 * media type applies (a browser's print preview, or a test emulating print). A page that folds
 * rows away on screen (the Audit trail's routine steps) unfolds them while this is true, because
 * paper has no "Show" button.
 */
export function usePrinting(): boolean {
  const [printing, setPrinting] = useState(() => {
    try {
      return window.matchMedia("print").matches;
    } catch {
      return false;
    }
  });
  useEffect(() => {
    // Rendered synchronously: the browser lays the page out for paper right after `beforeprint`
    // returns, before a batched update would have run.
    const set = (v: boolean) => {
      try {
        flushSync(() => setPrinting(v));
      } catch {
        setPrinting(v);
      }
    };
    const on = () => set(true);
    const off = () => set(false);
    let mq: MediaQueryList | null = null;
    const change = () => set(!!mq?.matches);
    try {
      mq = window.matchMedia("print");
      mq.addEventListener?.("change", change);
    } catch {
      mq = null;
    }
    window.addEventListener("beforeprint", on);
    window.addEventListener("afterprint", off);
    return () => {
      mq?.removeEventListener?.("change", change);
      window.removeEventListener("beforeprint", on);
      window.removeEventListener("afterprint", off);
    };
  }, []);
  return printing;
}

/** "Printed 02 Oct 2026, 15:24 EAT": the footer on every printed page. */
export function printedLine(at: Date = new Date()): string {
  return `Printed ${fmtDate(at)}, ${fmtHM(at)} EAT`;
}
