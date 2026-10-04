import { useCallback, useEffect, useState } from "react";

/**
 * The console follows the shift (docs/DESIGN_SYSTEM.md). `<html data-theme="day" | "night">`.
 *
 * A person picks Auto, Day or Night; Auto is Day from 06:00 to 18:59 in Nairobi and Night
 * otherwise, whatever the browser's own time zone. Projector mode and the Wallboard are always
 * Night: one hangs on a TV across the room, the other washes out a light page. index.html's
 * bootstrap applies the same rule before the first paint, so nothing flashes the other theme.
 */

export type ThemePref = "auto" | "day" | "night";
export type Theme = "day" | "night";

/** The localStorage key index.html's bootstrap reads too: keep the two in step. */
export const THEME_KEY = "noc.theme";

export const THEME_LABEL: Record<ThemePref, string> = {
  auto: "Auto (follows the shift)",
  day: "Day",
  night: "Night",
};

const THEME_COLOR: Record<Theme, string> = { day: "#F7F8FA", night: "#13161C" };

export function readThemePref(): ThemePref {
  try {
    const v = window.localStorage.getItem(THEME_KEY);
    return v === "day" || v === "night" ? v : "auto";
  } catch {
    return "auto";
  }
}

/** The hour in Nairobi (0-23), from the device clock. */
export function eatHour(now: Date = new Date()): number {
  try {
    const h = new Intl.DateTimeFormat("en-GB", { hour: "numeric", hourCycle: "h23", timeZone: "Africa/Nairobi" }).format(now);
    const n = Number(h);
    if (Number.isFinite(n)) return n;
  } catch {
    /* no Intl time zones: EAT is UTC+3 all year */
  }
  return (now.getUTCHours() + 3) % 24;
}

export function shiftTheme(now: Date = new Date()): Theme {
  const h = eatHour(now);
  return h >= 6 && h < 19 ? "day" : "night";
}

function forcedNight(pathname: string): boolean {
  try {
    if (document.documentElement.getAttribute("data-display") === "projector") return true;
  } catch {
    /* ignore */
  }
  return pathname.startsWith("/wallboard");
}

export function resolveTheme(pref: ThemePref, pathname: string, now: Date = new Date()): Theme {
  if (forcedNight(pathname)) return "night";
  return pref === "auto" ? shiftTheme(now) : pref;
}

export function applyTheme(theme: Theme): void {
  try {
    const root = document.documentElement;
    if (root.getAttribute("data-theme") !== theme) root.setAttribute("data-theme", theme);
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", THEME_COLOR[theme]);
  } catch {
    /* ignore */
  }
}

/**
 * The theme preference and the theme in force. Re-resolves on a route change (the Wallboard),
 * when projector mode flips (pass it in), and once a minute while on Auto so the console turns at
 * 06:00 and 19:00 without a reload.
 */
export function useTheme(pathname: string, projector: boolean) {
  const [pref, setPrefState] = useState<ThemePref>(readThemePref);
  const [theme, setTheme] = useState<Theme>(() => resolveTheme(readThemePref(), pathname));

  useEffect(() => {
    const update = () => {
      const t = resolveTheme(pref, pathname);
      setTheme(t);
      applyTheme(t);
    };
    update();
    if (pref !== "auto") return;
    const id = window.setInterval(update, 60_000);
    return () => window.clearInterval(id);
  }, [pref, pathname, projector]);

  const setPref = useCallback((next: ThemePref) => {
    try {
      if (next === "auto") window.localStorage.removeItem(THEME_KEY);
      else window.localStorage.setItem(THEME_KEY, next);
    } catch {
      /* storage blocked: the choice lasts until the tab closes */
    }
    setPrefState(next);
  }, []);

  return { pref, theme, setPref };
}
