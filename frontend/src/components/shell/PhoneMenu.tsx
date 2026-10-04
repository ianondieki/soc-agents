import { useCallback, useEffect, useLayoutEffect, useRef, useState, type FocusEvent, type ReactNode, type RefObject } from "react";
import { useLocation } from "react-router-dom";
import { Menu, X } from "lucide-react";
import { DisplayControls, Switch, type DisplayState } from "./DisplayMenu";
import { NavGroups } from "./Sidebar";
import type { NavCounts } from "./nav";

/**
 * The phone's menu (<= 760 px): a "Menu" button in the top bar that drops a sheet over the page
 * with the same accordion groups as the sidebar, then the Display controls and the guided demo.
 * It closes on navigation, Escape, the Close button or a press on the scrim, and hands focus
 * back to the Menu button. Nothing else is trapped.
 */
export function PhoneMenu({
  anchorRef,
  counts,
  display,
  guideOpen,
  onGuide,
  identity,
}: {
  /** The top bar: the sheet hangs from its bottom edge. */
  anchorRef: RefObject<HTMLElement>;
  counts: NavCounts;
  display: DisplayState;
  guideOpen: boolean;
  onGuide: () => void;
  /** The operator, its autonomy and shift, and the live state: the line the phone bar has no room for. */
  identity?: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const [top, setTop] = useState(56);
  const btnRef = useRef<HTMLButtonElement>(null);
  const sheetRef = useRef<HTMLDivElement>(null);
  const { pathname } = useLocation();

  const close = useCallback((refocus: boolean) => {
    setOpen(false);
    if (refocus) btnRef.current?.focus({ preventScroll: true });
  }, []);

  // Navigation closes it.
  const firstPath = useRef(pathname);
  useEffect(() => {
    if (firstPath.current !== pathname) close(true);
    firstPath.current = pathname;
  }, [pathname, close]);

  const measure = useCallback(() => {
    const r = anchorRef.current?.getBoundingClientRect();
    if (r) setTop(Math.round(r.bottom));
  }, [anchorRef]);

  useLayoutEffect(() => {
    if (!open) return;
    measure();
    sheetRef.current?.querySelector<HTMLElement>("button, a[href]")?.focus({ preventScroll: true });
  }, [open, measure]);

  useEffect(() => {
    if (!open) return;
    const root = document.documentElement;
    const before = root.style.overflow;
    root.style.overflow = "hidden";
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        close(true);
      }
    };
    document.addEventListener("keydown", onKey);
    window.addEventListener("resize", measure);
    return () => {
      root.style.overflow = before;
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("resize", measure);
    };
  }, [open, close, measure]);

  // Tab out of the sheet (past its last control, or Shift+Tab before its first) closes it and
  // puts focus back on the Menu button, so focus never lands behind the sheet.
  const onSheetBlur = (e: FocusEvent<HTMLDivElement>) => {
    const to = e.relatedTarget as Node | null;
    if (!to || sheetRef.current?.contains(to) || btnRef.current?.contains(to)) return;
    close(true);
  };

  return (
    <>
      <button
        ref={btnRef}
        type="button"
        className="btn sm phone-menu-btn"
        aria-expanded={open}
        aria-controls="phone-menu"
        onClick={() => (open ? close(false) : setOpen(true))}
      >
        {open ? <X size={16} strokeWidth={1.75} aria-hidden="true" /> : <Menu size={16} strokeWidth={1.75} aria-hidden="true" />}
        <span>{open ? "Close" : "Menu"}</span>
      </button>
      {open && (
        <>
          <div className="sheet-scrim" style={{ top }} onClick={() => close(false)} aria-hidden="true" />
          <div
            ref={sheetRef}
            id="phone-menu"
            className="sheet"
            role="region"
            aria-label="Menu"
            style={{ top, maxHeight: `calc(100dvh - ${top}px)` }}
            onBlur={onSheetBlur}
          >
            {identity && <div className="sheet-identity">{identity}</div>}
            <div className="sheet-nav">
              <NavGroups pathname={pathname} counts={counts} idPrefix="sheet" />
            </div>
            <div className="menu-sep" role="separator" />
            <div className="sheet-section">
              <div className="menu-label">Presenting</div>
              <Switch
                id="sheet-guide"
                on={guideOpen}
                onToggle={() => {
                  onGuide();
                  close(true);
                }}
                title="Guided demo"
                meaning="A five-step walkthrough for presenting the prototype."
              />
            </div>
            <div className="menu-sep" role="separator" />
            <div className="sheet-section">
              <DisplayControls d={display} idPrefix="sheet-display" />
            </div>
          </div>
        </>
      )}
    </>
  );
}
