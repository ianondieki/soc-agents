import { Bot, ClipboardCheck, Handshake, Headset, Radar, Settings2, type LucideIcon } from "lucide-react";

/**
 * The sidebar's groups (docs/DESIGN_SYSTEM.md, "The shell"): who reaches for them, in order.
 * Each group is a disclosure; the one holding the current page opens itself, the rest remember
 * their state in localStorage. A label is its page's title, in sentence case.
 */

export type NavLinkDef = { to: string; label: string; end?: boolean };

export type NavGroupDef = {
  key: string;
  title: string;
  icon: LucideIcon;
  links: NavLinkDef[];
  /** How a count on this group reads to a screen reader: "4 waiting for a decision". */
  countLabel?: (n: number) => string;
  /** "hitl" when the count means a person holds something (Support); neutral ink otherwise. */
  countTone?: "hitl";
};

export const NAV_GROUPS: NavGroupDef[] = [
  {
    key: "operate",
    title: "Operate",
    icon: Radar,
    countLabel: (n) => `${n} waiting for a decision`,
    links: [
      { to: "/mission", label: "Mission control" },
      { to: "/incidents", label: "Incident board" },
      { to: "/hitl", label: "Approvals" },
      { to: "/shift", label: "Shift desk" },
      { to: "/wallboard", label: "Wallboard" },
    ],
  },
  {
    key: "support",
    title: "Support",
    icon: Headset,
    countLabel: (n) => `${n} with a person`,
    countTone: "hitl",
    links: [
      { to: "/support", label: "Support desk" },
      { to: "/complain", label: "Complaint form" },
    ],
  },
  {
    key: "agents",
    title: "Agents",
    icon: Bot,
    links: [
      { to: "/showcase", label: "Showcase" },
      { to: "/agents", label: "Agent observatory" },
      { to: "/workflow", label: "Workflow map" },
    ],
  },
  {
    key: "quality",
    title: "Quality",
    icon: ClipboardCheck,
    links: [
      { to: "/problems", label: "Problems" },
      { to: "/pirs", label: "Post-incident reviews" },
      { to: "/regions", label: "Regions" },
    ],
  },
  {
    key: "vendors",
    title: "Vendors",
    icon: Handshake,
    links: [
      { to: "/scorecards", label: "Vendor scorecards" },
      { to: "/contracts", label: "Contracts" },
      { to: "/maintenance", label: "Maintenance" },
    ],
  },
  {
    key: "platform",
    title: "Platform",
    icon: Settings2,
    links: [
      { to: "/audit", label: "Audit trail" },
      { to: "/settings", label: "Settings" },
    ],
  },
];

/** Which groups are open, `{ operate: true, ... }`. */
export const NAV_GROUPS_KEY = "noc.nav.groups";
/** "1" when the sidebar is collapsed to its rail, "0" when a person expanded it. */
export const NAV_RAIL_KEY = "noc.nav.rail";

/** Counts shown on a group's header, by group key; null or 0 shows nothing. */
export type NavCounts = Partial<Record<string, number | null>>;

/** The key of the group holding `pathname`, or null (the landing page, an unknown route). */
export function groupKeyOf(pathname: string): string | null {
  for (const g of NAV_GROUPS) {
    for (const l of g.links) {
      if (l.end ? pathname === l.to : pathname === l.to || pathname.startsWith(l.to + "/")) return g.key;
    }
  }
  return null;
}

export function readOpenGroups(): Record<string, boolean> {
  try {
    const raw = window.localStorage.getItem(NAV_GROUPS_KEY);
    if (raw) {
      const v = JSON.parse(raw);
      if (v && typeof v === "object" && !Array.isArray(v)) return v as Record<string, boolean>;
    }
  } catch {
    /* storage blocked or unreadable: first-visit default */
  }
  return { operate: true };
}

export function writeOpenGroups(v: Record<string, boolean>): void {
  try {
    window.localStorage.setItem(NAV_GROUPS_KEY, JSON.stringify(v));
  } catch {
    /* storage blocked: the state lasts until the tab closes */
  }
}

export function readRailPref(): boolean | null {
  try {
    const v = window.localStorage.getItem(NAV_RAIL_KEY);
    return v === "1" ? true : v === "0" ? false : null;
  } catch {
    return null;
  }
}

export function writeRailPref(rail: boolean): void {
  try {
    window.localStorage.setItem(NAV_RAIL_KEY, rail ? "1" : "0");
  } catch {
    /* storage blocked */
  }
}

/** Focusable links inside a container, in order. */
export function linksIn(el: HTMLElement | null): HTMLAnchorElement[] {
  return el ? Array.from(el.querySelectorAll<HTMLAnchorElement>("a[href]")) : [];
}

/** Move focus by `delta` through `items`, wrapping; Home and End jump. */
export function roveFocus(items: HTMLElement[], e: { key: string; preventDefault(): void }, current: Element | null): boolean {
  if (!items.length) return false;
  const i = items.findIndex((x) => x === current);
  let next = -1;
  if (e.key === "ArrowDown" || e.key === "ArrowRight") next = i < 0 ? 0 : (i + 1) % items.length;
  else if (e.key === "ArrowUp" || e.key === "ArrowLeft") next = i < 0 ? items.length - 1 : (i - 1 + items.length) % items.length;
  else if (e.key === "Home") next = 0;
  else if (e.key === "End") next = items.length - 1;
  if (next < 0) return false;
  e.preventDefault();
  items[next].focus();
  return true;
}
