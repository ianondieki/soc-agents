import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type FocusEvent } from "react";
import { useNavigate } from "react-router-dom";
import { Bell, Bot, CheckCheck, Siren, TimerOff, UserRoundCheck, type LucideIcon } from "lucide-react";
import { api } from "../../api";
import { nodeLabel, regionName } from "../../lib/agents";
import { labelFor } from "../../lib/hitl";
import { useCalm } from "../../lib/motion";

/**
 * The bell in the top bar and its inbox (GET /api/v1/notifications): open P1s, restore clocks
 * run out, cards waiting for a decision and failed agent runs, newest first, in three tabs
 * (alarms, people, agents). The inbox is rebuilt from durable state, so a reload brings back
 * what was there; what this person has read is kept in their own browser (a convenience, never
 * shared): "Mark all as read" remembers the moment, opening an item remembers its id.
 *
 * The bell's badge counts the unread items, and the bell rings once (a short swing) when a new
 * one arrives; quiet mode and reduced motion keep it still. The panel is a dialog: Escape and a
 * click outside close it, focus returns to the bell.
 */

/** Per operator and per person, so one person's "Mark all as read" on a shared console never
 *  hides what is new from the next person who signs in there. */
const READ_PREFIX = "noc_inbox_read_v1";
/** The roles the inbox route admits (api/deps.INCIDENT_READERS); the shell shows no bell to others. */
export const INBOX_ROLES: ReadonlySet<string> = new Set(["noc_analyst", "shift_supervisor", "duty_manager", "admin", "management", "planning", "legal"]);
/** The roles that may open the Agent observatory (api/deps.PLATFORM_READERS). */
const AGENT_PAGE_ROLES: ReadonlySet<string> = new Set(["noc_analyst", "shift_supervisor", "duty_manager", "management", "admin"]);
type Group = "alarm" | "person" | "agent";
type Filter = "all" | Group;

interface ReadState {
  /** Everything at or before this instant (ISO) is read. */
  until: string | null;
  /** Items opened one by one since then. */
  ids: string[];
}

function loadRead(key: string): ReadState {
  try {
    const raw = JSON.parse(localStorage.getItem(key) || "null");
    if (raw && typeof raw === "object") {
      return { until: typeof raw.until === "string" ? raw.until : null, ids: Array.isArray(raw.ids) ? raw.ids.filter((x: unknown) => typeof x === "string").slice(-300) : [] };
    }
  } catch {
    /* private window or blocked storage: nothing read yet */
  }
  return { until: null, ids: [] };
}

function saveRead(key: string, r: ReadState): void {
  try {
    localStorage.setItem(key, JSON.stringify({ until: r.until, ids: r.ids.slice(-300) }));
  } catch {
    /* ignore */
  }
}

const GROUP_ICON: Record<string, LucideIcon> = { p1_open: Siren, restore_breached: TimerOff, approval_waiting: UserRoundCheck, run_failed: Bot };
const FILTERS: { key: Filter; label: string }[] = [
  { key: "all", label: "All" },
  { key: "alarm", label: "Alarms" },
  { key: "person", label: "Decisions" },
  { key: "agent", label: "Agents" },
];

/** "just now", "12 min ago", "3 h ago", "2 days ago". */
function ago(iso: string | null, now: number): string {
  const t = iso ? Date.parse(iso) : NaN;
  if (!Number.isFinite(t)) return "";
  const min = Math.max(0, Math.round((now - t) / 60000));
  if (min < 1) return "just now";
  if (min < 60) return `${min} min ago`;
  const h = Math.round(min / 60);
  if (h < 24) return `${h} h ago`;
  const d = Math.round(h / 24);
  return d === 1 ? "yesterday" : `${d} days ago`;
}

/** What an item says: its title, one line under it, and where it leads (null: nowhere this role
 *  may open, so the item only marks itself read). */
function wordsOf(it: any, profile: any, role: string): { title: string; meta: string; href: string | null } {
  const site = it.site_name || "an unnamed site";
  const region = it.region_code ? regionName(it.region_code, profile) : "";
  const ticket = it.incident_number || "";
  const toTicket = it.incident_id ? `/incidents/${it.incident_id}` : "/incidents";
  switch (it.kind) {
    case "p1_open":
      return { title: `P1 opened at ${site}`, meta: [ticket, region].filter(Boolean).join(", "), href: toTicket };
    case "restore_breached":
      return { title: `Restore time passed at ${site}`, meta: [ticket, it.priority].filter(Boolean).join(", "), href: toTicket };
    case "approval_waiting":
      return {
        title: `${labelFor(it.task_type)} waits for a decision`,
        meta: ticket ? `${ticket} at ${site}` : "Not about one ticket",
        href: "/hitl",
      };
    case "run_failed":
      return {
        title: it.node ? `An agent run failed at ${nodeLabel(it.node)}` : "An agent run failed",
        meta: [ticket, it.error].filter(Boolean).join(": "),
        href: it.incident_id ? toTicket : AGENT_PAGE_ROLES.has(role) ? "/agents" : null,
      };
    default:
      return { title: "Something needs a look", meta: ticket, href: toTicket };
  }
}

export default function NotificationCenter({
  rev,
  profile,
  who,
  role,
  compact = false,
}: {
  rev: number;
  profile: any;
  /** Who reads: the read state is kept per operator and per person. */
  who: string;
  role: string;
  compact?: boolean;
}) {
  const nav = useNavigate();
  const calm = useCalm();
  const readKey = `${READ_PREFIX}:${String(profile?.operator_id || "operator")}:${who}`;
  const [data, setData] = useState<any | null>(null);
  const [failed, setFailed] = useState(false);
  const [read, setRead] = useState<ReadState>(() => loadRead(readKey));
  useEffect(() => setRead(loadRead(readKey)), [readKey]);
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState<Filter>("all");
  const [ring, setRing] = useState(0);
  const [now, setNow] = useState(() => Date.now());
  const btnRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ top: number; right: number }>({ top: 64, right: 16 });

  // Fetch on mount, at most every 1.5 s while the stream says incidents, cards or runs are
  // changing (a throttle, not a debounce: a storm that bumps the revisions faster than that still
  // refreshes the inbox), and every two minutes so a clock that runs out with no frame arrives.
  const alive = useRef(true);
  const last = useRef(0);
  const pending = useRef<number | null>(null);
  const load = useCallback(() => {
    pending.current = null;
    last.current = Date.now();
    api
      .notifications()
      .then((d) => {
        if (!alive.current) return;
        setData(d);
        setFailed(false);
      })
      .catch(() => alive.current && setFailed(true));
  }, []);
  useEffect(() => {
    alive.current = true;
    const every = window.setInterval(load, 120_000);
    return () => {
      alive.current = false;
      window.clearInterval(every);
      if (pending.current != null) window.clearTimeout(pending.current);
    };
  }, [load]);
  useEffect(() => {
    if (pending.current != null) return;
    const wait = Math.max(0, 1500 - (Date.now() - last.current));
    pending.current = window.setTimeout(load, last.current === 0 ? 300 : wait);
  }, [rev, load]);

  const items: any[] = Array.isArray(data?.items) ? data.items : [];
  const isUnread = useCallback(
    (it: any) => !read.ids.includes(it.id) && (!read.until || String(it.at || "") > read.until),
    [read]
  );
  const unread = items.filter(isUnread);

  // Ring once when the unread count goes up (not on the first load).
  const seen = useRef<number | null>(null);
  useEffect(() => {
    if (data == null) return;
    if (seen.current != null && unread.length > seen.current && !calm) setRing((n) => n + 1);
    seen.current = unread.length;
  }, [data, unread.length, calm]);

  const place = useCallback(() => {
    const r = btnRef.current?.getBoundingClientRect();
    if (!r) return;
    setPos({ top: Math.round(r.bottom + 6), right: Math.max(8, Math.round(window.innerWidth - r.right)) });
  }, []);
  const close = useCallback((refocus: boolean) => {
    setOpen(false);
    if (refocus) btnRef.current?.focus({ preventScroll: true });
  }, []);

  useLayoutEffect(() => {
    if (!open) return;
    place();
    setNow(Date.now());
    panelRef.current?.querySelector<HTMLElement>("[data-autofocus]")?.focus({ preventScroll: true });
  }, [open, place]);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: Event) => {
      const t = e.target as Node | null;
      if (!t || panelRef.current?.contains(t) || btnRef.current?.contains(t)) return;
      close(false);
    };
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        close(true);
      }
    };
    document.addEventListener("pointerdown", onDown, true);
    document.addEventListener("keydown", onKey);
    window.addEventListener("resize", place);
    return () => {
      document.removeEventListener("pointerdown", onDown, true);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("resize", place);
    };
  }, [open, close, place]);

  const onPanelBlur = (e: FocusEvent<HTMLDivElement>) => {
    const to = e.relatedTarget as Node | null;
    if (!to || panelRef.current?.contains(to) || btnRef.current?.contains(to)) return;
    close(false);
  };

  const markAll = () => {
    const newest = items.reduce((m, it) => (String(it.at || "") > m ? String(it.at) : m), read.until || "");
    const next = { until: newest || new Date().toISOString().replace(/\.\d{3}Z$/, "Z"), ids: [] };
    // The button disables itself once nothing is unread: hand focus to the first tab first, so
    // a keyboard user is not dropped onto the page behind the panel.
    panelRef.current?.querySelector<HTMLElement>(".inbox-tab")?.focus({ preventScroll: true });
    setRead(next);
    saveRead(readKey, next);
  };
  const openItem = (it: any, href: string | null) => {
    const next = { until: read.until, ids: [...read.ids.filter((x) => x !== it.id), it.id] };
    setRead(next);
    saveRead(readKey, next);
    if (!href) return;
    close(false);
    nav(href);
  };

  const counts = { all: items.length, alarm: 0, person: 0, agent: 0 } as Record<Filter, number>;
  for (const it of items) if (it.group in counts) counts[it.group as Group] += 1;
  const shown = useMemo(() => (filter === "all" ? items : items.filter((it) => it.group === filter)), [items, filter]);
  const badge = unread.length > 99 ? "99+" : String(unread.length);
  const label = unread.length ? `Notifications, ${unread.length} unread` : "Notifications";

  return (
    <>
      <button
        ref={btnRef}
        type="button"
        className={"btn sm topbar-bell" + (open ? " is-open" : "") + (compact ? " compact" : "")}
        aria-expanded={open}
        aria-controls={open ? "inbox-panel" : undefined}
        aria-label={label}
        title={label}
        onClick={() => setOpen((o) => !o)}
      >
        <span
          key={ring}
          className={"topbar-bell-icon" + (ring && !calm ? " is-ringing" : "")}
          aria-hidden="true"
          onAnimationEnd={() => setRing(0)}
        >
          <Bell size={16} strokeWidth={1.75} />
        </span>
        {unread.length > 0 && (
          <span className="topbar-bell-badge" aria-hidden="true">
            {badge}
          </span>
        )}
      </button>
      {open && (
        <div
          ref={panelRef}
          id="inbox-panel"
          className="menu inbox"
          role="dialog"
          aria-label="Notifications"
          style={{ top: pos.top, right: pos.right }}
          onBlur={onPanelBlur}
        >
          <div className="inbox-head">
            <h2>Notifications</h2>
            <button type="button" className="btn sm ghost" onClick={markAll} disabled={!unread.length} data-autofocus={unread.length ? "" : undefined}>
              <CheckCheck size={15} strokeWidth={1.75} aria-hidden="true" />
              Mark all as read
            </button>
          </div>
          <div className="inbox-tabs" role="group" aria-label="Show">
            {FILTERS.map((f) => (
              <button
                key={f.key}
                type="button"
                className={"inbox-tab" + (filter === f.key ? " is-on" : "")}
                aria-pressed={filter === f.key}
                onClick={() => setFilter(f.key)}
                data-autofocus={!unread.length && f.key === "all" ? "" : undefined}
              >
                {f.label}
                <span className="inbox-tab-n">{counts[f.key]}</span>
              </button>
            ))}
          </div>
          {failed && !data ? (
            <p className="inbox-empty" role="alert">
              Couldn't load the notifications. They come back on their own once the server answers.
            </p>
          ) : data == null ? (
            <p className="inbox-empty">Loading…</p>
          ) : shown.length === 0 ? (
            <p className="inbox-empty">
              Nothing needs you here. New P1s, restore clocks that run out, decisions waiting and failed agent runs land in this list.
            </p>
          ) : (
            <ul className="inbox-list">
              {shown.map((it) => {
                const w = wordsOf(it, profile, role);
                const Icon = GROUP_ICON[it.kind] || Bell;
                const fresh = isUnread(it);
                return (
                  <li key={it.id}>
                    <button type="button" className={"inbox-item g-" + it.group + (fresh ? " is-unread" : "")} onClick={() => openItem(it, w.href)}>
                      <span className="inbox-icon" aria-hidden="true">
                        <Icon size={16} strokeWidth={2} />
                      </span>
                      <span className="inbox-text">
                        <span className="inbox-title">{w.title}</span>
                        {w.meta && <span className="inbox-meta">{w.meta}</span>}
                        <span className="inbox-when">{ago(it.at, now)}</span>
                      </span>
                      {fresh && (
                        <span className="inbox-dot">
                          <span className="sr-only">unread</span>
                        </span>
                      )}
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
          <p className="inbox-foot">From the last {data?.window_hours ?? 24} hours; a decision that is still waiting stays here whatever its age.</p>
        </div>
      )}
    </>
  );
}
