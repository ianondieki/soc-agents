import { Bot, ClipboardCheck, Handshake, Headset, Radar, Settings2, type LucideIcon } from "lucide-react";

/**
 * The sidebar's groups (docs/DESIGN_SYSTEM.md, "The shell"): who reaches for them, in order.
 * Each group is a disclosure; the one holding the current page opens itself, the rest remember
 * their state in localStorage. A label is its page's title, in sentence case.
 */

/** `keywords`: the other words a person types for this page in "Go to a page" (GoTo.tsx). */
export type NavLinkDef = { to: string; label: string; end?: boolean; keywords?: string };

export type NavGroupDef = {
  key: string;
  title: string;
  icon: LucideIcon;
  links: NavLinkDef[];
  /** How a count on this group reads to a screen reader: "4 waiting for a decision". */
  countLabel?: (n: number) => string;
  /** "hitl" when the count means a person holds something (Support); neutral ink otherwise. */
  countTone?: "hitl";
  /** The page the count belongs to: on the group while it is closed, on this page once it opens. */
  countTo?: string;
  /** True on the first group after the daily desks (Operate, Support): a hairline goes above it. */
  sectionStart?: boolean;
};

export const NAV_GROUPS: NavGroupDef[] = [
  {
    key: "operate",
    title: "Operate",
    icon: Radar,
    countLabel: (n) => `${n} waiting for a decision`,
    countTo: "/hitl",
    links: [
      { to: "/mission", label: "Mission control", keywords: "home dashboard storm alarms live" },
      { to: "/incidents", label: "Incident board", keywords: "tickets incidents outages sites" },
      { to: "/hitl", label: "Approvals", keywords: "decisions hitl waiting approve reject cards" },
      { to: "/shift", label: "Shift desk", keywords: "handover ledger shift" },
      { to: "/wallboard", label: "Wallboard", keywords: "tv screen projector big" },
    ],
  },
  {
    key: "support",
    title: "Support",
    icon: Headset,
    countLabel: (n) => `${n} with a person`,
    countTone: "hitl",
    countTo: "/support",
    links: [
      { to: "/support", label: "Support desk", keywords: "complaints cases refunds evals outages customers" },
      { to: "/complain", label: "Complaint form", keywords: "customer send complaint public" },
      { to: "/track", label: "Track a complaint", keywords: "reference status customer public" },
    ],
  },
  {
    key: "agents",
    title: "Agents",
    icon: Bot,
    sectionStart: true,
    links: [
      { to: "/showcase", label: "Showcase", keywords: "demo agents tour" },
      { to: "/agents", label: "Agent observatory", keywords: "runs tools agents metrics" },
      { to: "/workflow", label: "Workflow map", keywords: "graph steps pipeline flow" },
    ],
  },
  {
    key: "quality",
    title: "Quality",
    icon: ClipboardCheck,
    links: [
      { to: "/problems", label: "Problems", keywords: "root cause recurring" },
      { to: "/pirs", label: "Post-incident reviews", keywords: "pir review lessons" },
      { to: "/regions", label: "Regions", keywords: "counties map nairobi rift coast" },
    ],
  },
  {
    key: "vendors",
    title: "Vendors",
    icon: Handshake,
    links: [
      { to: "/scorecards", label: "Vendor scorecards", keywords: "vendors sla suppliers" },
      { to: "/contracts", label: "Contracts", keywords: "vendors sla penalties" },
      { to: "/maintenance", label: "Maintenance", keywords: "planned works windows" },
    ],
  },
  {
    key: "platform",
    title: "Platform",
    icon: Settings2,
    links: [
      { to: "/audit", label: "Audit trail", keywords: "log history who did what" },
      { to: "/settings", label: "Settings", keywords: "autonomy profile operator preferences" },
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
