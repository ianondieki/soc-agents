import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { CornerDownLeft, Search } from "lucide-react";
import { NAV_GROUPS } from "./nav";

/**
 * "Go to a page": every console page in one search box. Opened by the sidebar's search row (the
 * rail's search icon) or Ctrl+K / Cmd+K anywhere in the console. A dialog holding one combobox:
 * typing filters the pages (their label, group and keywords from nav.ts), the arrow keys move,
 * Enter opens, Escape or a press outside closes and hands focus back to where it was.
 */

type Item = { to: string; label: string; group: string; hay: string };

const ITEMS: Item[] = [
  ...NAV_GROUPS.flatMap((g) =>
    g.links.map((l) => ({ to: l.to, label: l.label, group: g.title, hay: `${l.label} ${g.title} ${l.keywords || ""}`.toLowerCase() }))
  ),
  { to: "/", label: "Front page", group: "Kenya NOC", hay: "front page landing home kenya noc" },
];

/** Every word typed must appear; a label that starts with the query ranks first, then one with a
 *  word starting with it, then the rest in sidebar order. */
export function matchPages(query: string): Item[] {
  const q = query.trim().toLowerCase();
  if (!q) return ITEMS;
  const words = q.split(/\s+/);
  const ranked: Array<{ item: Item; rank: number; i: number }> = [];
  ITEMS.forEach((item, i) => {
    if (!words.every((w) => item.hay.includes(w))) return;
    const label = item.label.toLowerCase();
    const rank = label.startsWith(q) ? 0 : label.split(/\s+/).some((w) => w.startsWith(words[0])) ? 1 : 2;
    ranked.push({ item, rank, i });
  });
  return ranked.sort((a, b) => a.rank - b.rank || a.i - b.i).map((r) => r.item);
}

/** The label with the first typed word marked, when the label holds it. */
function marked(label: string, query: string): ReactNode {
  const w = query.trim().toLowerCase().split(/\s+/)[0];
  if (!w) return label;
  const at = label.toLowerCase().indexOf(w);
  if (at < 0) return label;
  return (
    <>
      {label.slice(0, at)}
      <mark>{label.slice(at, at + w.length)}</mark>
      {label.slice(at + w.length)}
    </>
  );
}

/** Ctrl+K / Cmd+K toggles it. */
export function useGoToShortcut(toggle: () => void) {
  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === "k") {
        e.preventDefault();
        toggle();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [toggle]);
}

export default function GoTo({ open, onClose }: { open: boolean; onClose: () => void }) {
  const nav = useNavigate();
  const { pathname } = useLocation();
  const [q, setQ] = useState("");
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const returnTo = useRef<HTMLElement | null>(null);
  const id = useId();
  const listId = `${id}-list`;
  const results = useMemo(() => matchPages(q), [q]);

  // Opening: remember where focus was, start empty, put the caret in the box, hold the page still.
  useLayoutEffect(() => {
    if (!open) return;
    returnTo.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setQ("");
    setActive(0);
    inputRef.current?.focus();
    const root = document.documentElement;
    const before = root.style.overflow;
    root.style.overflow = "hidden";
    return () => {
      root.style.overflow = before;
      const back = returnTo.current;
      if (back && back.isConnected) back.focus({ preventScroll: true });
    };
  }, [open]);

  useEffect(() => setActive(0), [q]);

  // Keep the highlighted page in view.
  useEffect(() => {
    listRef.current?.querySelector<HTMLElement>(`[data-i="${active}"]`)?.scrollIntoView({ block: "nearest" });
  }, [active, results]);

  if (!open) return null;

  const go = (item: Item | undefined) => {
    if (!item) return;
    // Focus goes to the new page, not back to the opener.
    returnTo.current = null;
    onClose();
    if (item.to !== pathname) nav(item.to);
  };

  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    const n = results.length;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (n) setActive((a) => (a + 1) % n);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      if (n) setActive((a) => (a - 1 + n) % n);
    } else if (e.key === "Enter") {
      e.preventDefault();
      go(results[active]);
    } else if (e.key === "Escape") {
      e.preventDefault();
      onClose();
    } else if (e.key === "Tab") {
      // One control in the dialog: Tab stays in the box.
      e.preventDefault();
    }
  };

  const optionId = (i: number) => `${id}-opt-${i}`;

  return (
    <div className="goto-layer">
      <div className="goto-scrim" onClick={onClose} aria-hidden="true" />
      <div className="goto" role="dialog" aria-modal="true" aria-label="Go to a page">
        <div className="goto-field">
          <Search size={18} strokeWidth={1.75} aria-hidden="true" />
          <input
            ref={inputRef}
            className="goto-input"
            type="text"
            role="combobox"
            aria-expanded="true"
            aria-controls={listId}
            aria-autocomplete="list"
            aria-activedescendant={results.length ? optionId(active) : undefined}
            aria-label="Go to a page"
            placeholder="Go to a page"
            autoComplete="off"
            spellCheck={false}
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={onKey}
          />
          <kbd aria-hidden="true">Esc</kbd>
        </div>
        <ul ref={listRef} id={listId} className="goto-list" role="listbox" aria-label="Pages">
          {results.map((item, i) => (
            <li
              key={item.to}
              id={optionId(i)}
              data-i={i}
              role="option"
              aria-selected={i === active}
              className={"goto-opt" + (i === active ? " is-active" : "") + (item.to === pathname ? " is-current" : "")}
              onPointerMove={() => i !== active && setActive(i)}
              onPointerDown={(e) => e.preventDefault()}
              onClick={() => go(item)}
            >
              <span className="goto-label">{marked(item.label, q)}</span>
              <span className="goto-group">{item.to === pathname ? "You are here" : item.group}</span>
              <CornerDownLeft className="goto-enter" size={14} strokeWidth={1.75} aria-hidden="true" />
            </li>
          ))}
        </ul>
        {!results.length && (
          <p className="goto-empty" role="status">
            No page matches “{q.trim()}”.
          </p>
        )}
      </div>
    </div>
  );
}
