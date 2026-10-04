import { useCallback, useEffect, useLayoutEffect, useRef, useState, type FocusEvent, type KeyboardEvent, type PointerEvent } from "react";
import { Link, NavLink, useLocation } from "react-router-dom";
import { ChevronDown, PanelLeftClose, PanelLeftOpen } from "lucide-react";
import { BrandMark } from "./BrandMark";
import { NAV_GROUPS, groupKeyOf, linksIn, readOpenGroups, roveFocus, writeOpenGroups, type NavCounts, type NavGroupDef } from "./nav";

/**
 * The console's sidebar (docs/DESIGN_SYSTEM.md, "The shell"). Two shapes:
 *
 *  - expanded (248 px): the brand row, six disclosure groups, "Collapse sidebar" at the foot.
 *    A group header is a button with the group's icon, name, a count when it has one and a
 *    chevron; its links open and close on a `grid-template-rows` animation. The group holding
 *    the current page opens itself; the rest keep their state in localStorage.
 *  - rail (72 px): one icon button per group. Pressing it, or resting a mouse on it for 150 ms,
 *    opens a flyout beside the rail with that group's pages. The flyout closes on Escape,
 *    outside click or navigation and hands focus back to its icon.
 *
 * The phone sheet (PhoneMenu.tsx) reuses the group list below.
 */

const ICON = { size: 18, strokeWidth: 1.75 } as const;
/* Group headers are the quiet layer (12 px, muted): a 16 px icon sits level with them. */
const HEAD_ICON = { size: 16, strokeWidth: 1.75 } as const;

/** Which groups are open: the stored state, plus the current page's group. */
export function useOpenGroups(pathname: string) {
  const [open, setOpen] = useState<Record<string, boolean>>(readOpenGroups);
  useEffect(() => {
    const k = groupKeyOf(pathname);
    if (!k) return;
    setOpen((prev) => (prev[k] ? prev : { ...prev, [k]: true }));
  }, [pathname]);
  const toggle = useCallback((key: string) => {
    setOpen((prev) => {
      const next = { ...prev, [key]: !prev[key] };
      writeOpenGroups(next);
      return next;
    });
  }, []);
  return { open, toggle };
}

function CountBadge({ n, label, className }: { n: number | null | undefined; label?: (n: number) => string; className: string }) {
  if (typeof n !== "number" || n <= 0) return null;
  return (
    <span className={className}>
      <span aria-hidden="true">{n > 99 ? "99+" : n}</span>
      <span className="sr-only">{label ? `, ${label(n)}` : `, ${n}`}</span>
    </span>
  );
}

/** One disclosure group. `idPrefix` keeps ids unique when the phone sheet and the sidebar both render. */
export function NavGroup({
  group,
  open,
  active,
  count,
  onToggle,
  onNavigate,
  idPrefix = "nav",
}: {
  group: NavGroupDef;
  open: boolean;
  active: boolean;
  count?: number | null;
  onToggle: (key: string) => void;
  onNavigate?: () => void;
  idPrefix?: string;
}) {
  const Icon = group.icon;
  const panelId = `${idPrefix}-${group.key}`;
  return (
    <div className={"nav-group" + (open ? " open" : "") + (active ? " active" : "")}>
      <button
        type="button"
        className="nav-group-btn"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => onToggle(group.key)}
      >
        <Icon {...HEAD_ICON} aria-hidden="true" />
        <span className="nav-group-name">{group.title}</span>
        <CountBadge n={count} label={group.countLabel} className={"nav-count" + (group.countTone ? ` ${group.countTone}` : "")} />
        <ChevronDown className="nav-chevron" size={16} strokeWidth={1.75} aria-hidden="true" />
      </button>
      <div className="nav-group-panel" id={panelId}>
        <div className="nav-group-inner">
          <ul className="nav-links">
            {group.links.map((l) => (
              <li key={l.to}>
                <NavLink to={l.to} end={l.end} onClick={onNavigate}>
                  {l.label}
                </NavLink>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  );
}

/** The list of groups, as the expanded sidebar and the phone sheet show it. */
export function NavGroups({
  pathname,
  counts,
  onNavigate,
  idPrefix,
}: {
  pathname: string;
  counts: NavCounts;
  onNavigate?: () => void;
  idPrefix?: string;
}) {
  const { open, toggle } = useOpenGroups(pathname);
  const activeKey = groupKeyOf(pathname);
  return (
    <>
      {NAV_GROUPS.map((g) => (
        <NavGroup
          key={g.key}
          group={g}
          open={!!open[g.key]}
          active={activeKey === g.key}
          count={counts[g.key]}
          onToggle={toggle}
          onNavigate={onNavigate}
          idPrefix={idPrefix}
        />
      ))}
    </>
  );
}

type Flyout = { key: string; byHover: boolean; top: number };

const HOVER_OPEN_MS = 150;
const HOVER_CLOSE_MS = 220;

export function Sidebar({ rail, onToggleRail, counts }: { rail: boolean; onToggleRail: () => void; counts: NavCounts }) {
  const { pathname } = useLocation();
  const activeKey = groupKeyOf(pathname);
  const scrollRef = useRef<HTMLDivElement>(null);

  // Expanded: keep the current page's link in view when the list is taller than the screen.
  // After the open animation (200 ms), so the measured position is the settled one.
  useEffect(() => {
    if (rail) return;
    const t = window.setTimeout(() => {
      const box = scrollRef.current;
      const link = box?.querySelector<HTMLElement>("a.active");
      if (!box || !link || box.scrollHeight <= box.clientHeight + 1) return;
      const top = link.offsetTop - box.offsetTop;
      const bottom = top + link.offsetHeight;
      if (top < box.scrollTop || bottom > box.scrollTop + box.clientHeight) {
        box.scrollTop = Math.max(0, top - box.clientHeight / 2);
      }
    }, 240);
    return () => window.clearTimeout(t);
  }, [pathname, rail]);

  // ---- the rail's flyout --------------------------------------------------------------------
  const [flyout, setFlyout] = useState<Flyout | null>(null);
  const flyoutRef = useRef<HTMLDivElement>(null);
  const btnRefs = useRef<Record<string, HTMLButtonElement | null>>({});
  const hoverTimer = useRef(0);
  const closeTimer = useRef(0);
  const clearTimers = () => {
    window.clearTimeout(hoverTimer.current);
    window.clearTimeout(closeTimer.current);
  };

  const openFlyout = useCallback((key: string, byHover: boolean) => {
    clearTimers();
    const rect = btnRefs.current[key]?.getBoundingClientRect();
    setFlyout({ key, byHover, top: rect ? rect.top : 64 });
  }, []);

  const closeFlyout = useCallback((refocus: boolean) => {
    clearTimers();
    setFlyout((f) => {
      if (f && refocus) btnRefs.current[f.key]?.focus({ preventScroll: true });
      return null;
    });
  }, []);

  // Opened by a press or a key: focus lands on the first page.
  useLayoutEffect(() => {
    const el = flyoutRef.current;
    if (!flyout || !el) return;
    // Keep the whole flyout on screen.
    const h = el.offsetHeight;
    const max = window.innerHeight - 8 - h;
    if (flyout.top > max) el.style.top = `${Math.max(8, max)}px`;
    if (!flyout.byHover) linksIn(el)[0]?.focus({ preventScroll: true });
  }, [flyout]);

  // Navigation and leaving the rail both close it.
  useEffect(() => {
    setFlyout((f) => {
      if (f) btnRefs.current[f.key]?.focus({ preventScroll: true });
      return null;
    });
  }, [pathname]);
  useEffect(() => {
    if (!rail) setFlyout(null);
  }, [rail]);

  // Outside press closes it.
  useEffect(() => {
    if (!flyout) return;
    const onDown = (e: Event) => {
      const t = e.target as Node | null;
      if (!t) return;
      if (flyoutRef.current?.contains(t)) return;
      if (btnRefs.current[flyout.key]?.contains(t)) return;
      closeFlyout(false);
    };
    document.addEventListener("pointerdown", onDown, true);
    return () => document.removeEventListener("pointerdown", onDown, true);
  }, [flyout, closeFlyout]);

  useEffect(() => () => clearTimers(), []);

  const onIconEnter = (key: string) => (e: PointerEvent) => {
    if (e.pointerType !== "mouse") return;
    window.clearTimeout(closeTimer.current);
    if (flyout?.key === key) return;
    window.clearTimeout(hoverTimer.current);
    hoverTimer.current = window.setTimeout(() => openFlyout(key, true), HOVER_OPEN_MS);
  };
  const onIconLeave = (e: PointerEvent) => {
    if (e.pointerType !== "mouse") return;
    window.clearTimeout(hoverTimer.current);
    if (flyout?.byHover) closeTimer.current = window.setTimeout(() => closeFlyout(false), HOVER_CLOSE_MS);
  };
  const onIconClick = (key: string) => () => {
    if (flyout?.key === key) {
      if (flyout.byHover) setFlyout({ ...flyout, byHover: false });
      else closeFlyout(false);
      return;
    }
    openFlyout(key, false);
  };
  const onIconKey = (key: string) => (e: KeyboardEvent) => {
    if (e.key === "ArrowDown" || e.key === "ArrowRight") {
      e.preventDefault();
      openFlyout(key, false);
    } else if (e.key === "Escape" && flyout) {
      e.preventDefault();
      closeFlyout(true);
    }
  };
  const onFlyoutKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Escape") {
      e.preventDefault();
      closeFlyout(true);
      return;
    }
    roveFocus(linksIn(flyoutRef.current), e, document.activeElement);
  };
  const onFlyoutBlur = (e: FocusEvent<HTMLDivElement>) => {
    const to = e.relatedTarget as Node | null;
    if (!to) return;
    if (flyoutRef.current?.contains(to)) return;
    if (flyout && btnRefs.current[flyout.key]?.contains(to)) return;
    closeFlyout(false);
  };

  if (rail) {
    const fg = flyout ? NAV_GROUPS.find((g) => g.key === flyout.key) : null;
    return (
      <nav className="nav rail" aria-label="Main">
        <div className="nav-brand">
          <Link to="/" className="brand" aria-label="Kenya NOC, front page" title="Kenya NOC">
            <BrandMark />
          </Link>
        </div>
        <ul className="nav-rail-list">
          {NAV_GROUPS.map((g) => {
            const Icon = g.icon;
            const n = counts[g.key];
            const expanded = flyout?.key === g.key;
            return (
              <li key={g.key}>
                <button
                  ref={(el) => {
                    btnRefs.current[g.key] = el;
                  }}
                  type="button"
                  className={"nav-rail-btn" + (activeKey === g.key ? " active" : "")}
                  aria-label={g.title + (typeof n === "number" && n > 0 && g.countLabel ? `, ${g.countLabel(n)}` : "")}
                  aria-expanded={expanded}
                  aria-controls={expanded ? "nav-flyout" : undefined}
                  title={g.title}
                  onPointerEnter={onIconEnter(g.key)}
                  onPointerLeave={onIconLeave}
                  onClick={onIconClick(g.key)}
                  onKeyDown={onIconKey(g.key)}
                >
                  <Icon {...ICON} aria-hidden="true" />
                  {typeof n === "number" && n > 0 && <span className={"nav-rail-count" + (g.countTone ? ` ${g.countTone}` : "")} aria-hidden="true" />}
                </button>
                {/* The flyout lives after its own button (still position: fixed), so Tab moves on
                    from its last page to the next group's icon. */}
                {expanded && fg && (
                  <div
                    ref={flyoutRef}
                    id="nav-flyout"
                    className="nav-flyout"
                    role="group"
                    aria-label={fg.title}
                    style={{ top: flyout.top }}
                    onKeyDown={onFlyoutKey}
                    onBlur={onFlyoutBlur}
                    onPointerEnter={() => window.clearTimeout(closeTimer.current)}
                    onPointerLeave={(e) => {
                      if (e.pointerType === "mouse" && flyout.byHover) closeTimer.current = window.setTimeout(() => closeFlyout(false), HOVER_CLOSE_MS);
                    }}
                  >
                    <div className="nav-flyout-title">{fg.title}</div>
                    <ul className="nav-links">
                      {fg.links.map((l) => (
                        <li key={l.to}>
                          <NavLink to={l.to} end={l.end}>
                            {l.label}
                          </NavLink>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </li>
            );
          })}
        </ul>
        <div className="nav-foot">
          <button type="button" className="nav-rail-btn" aria-label="Expand sidebar" title="Expand sidebar" onClick={onToggleRail}>
            <PanelLeftOpen {...ICON} aria-hidden="true" />
          </button>
        </div>
      </nav>
    );
  }

  return (
    <nav className="nav" aria-label="Main">
      <div className="nav-brand">
        <Link to="/" className="brand" title="Front page">
          <BrandMark />
          <span>Kenya NOC</span>
        </Link>
      </div>
      <div className="nav-scroll" ref={scrollRef}>
        <NavGroups pathname={pathname} counts={counts} />
      </div>
      <div className="nav-foot">
        <button type="button" className="nav-collapse" onClick={onToggleRail} title="Collapse sidebar">
          <PanelLeftClose {...ICON} aria-hidden="true" />
          <span>Collapse sidebar</span>
        </button>
      </div>
    </nav>
  );
}
