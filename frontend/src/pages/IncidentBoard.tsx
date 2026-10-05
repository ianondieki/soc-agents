import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { Search } from "lucide-react";
import { api } from "../api";
import { MPESA_TITLE, humanEnum, humanStatus, ownerName, priorityTitle, regionName } from "../lib/agents";
import { useNarrow } from "../lib/layout";
import { parseInstant } from "../lib/time";
import { useMinute } from "../lib/useMinute";
import "./IncidentBoard.css";

/** The regions offered when the operator profile has none of its own. */
const REGIONS = ["NBI_E", "NBI_W", "MTK", "CST", "RFT", "WNY"];
const PRIORITIES = ["P1", "P2", "P3", "P4"];
/** Typing settles this long before the board asks the API (`q`), so a ticket number is one request. */
const SEARCH_DEBOUNCE_MS = 250;
/** The longest search the board sends (the box stops there too): a pasted page never becomes a URL. */
const SEARCH_MAX = 200;
const settle = (typed: string) => typed.trim().slice(0, SEARCH_MAX);

/* ownerName lives in lib/agents.ts, shared with Mission control and the Shift desk. */

// The phone-layout helpers moved to lib/layout.ts; re-exported here so older imports still work.
export { NARROW_QUERY, ONE_COL, useNarrow } from "../lib/layout";

/** Where a ticket is in its life, as the board's switch groups it. */
type StateKey = "open" | "restored" | "closed" | "all";
const STATES: Array<{ key: StateKey; label: string }> = [
  { key: "open", label: "Open" },
  { key: "restored", label: "Restored" },
  { key: "closed", label: "Closed" },
  { key: "all", label: "All" },
];
function stateOf(status: unknown): Exclude<StateKey, "all"> {
  const s = String(status ?? "").toUpperCase();
  if (s === "CLOSED" || s === "CANCELLED") return "closed";
  if (s === "RESTORED") return "restored";
  return "open";
}

/** The dot on a status pill: amber while a vendor holds it, blue while the NOC works it, green
 *  once restored, grey when closed. The word is always there; the colour only helps scanning. */
function statusTone(status: unknown): "warn" | "work" | "ok" | "done" {
  const s = String(status ?? "").toUpperCase();
  if (s === "AWAITING_VENDOR") return "warn";
  if (s === "RESTORED") return "ok";
  if (s === "CLOSED" || s === "CANCELLED") return "done";
  return "work";
}

/** "8 min", "2 h 5 min", "3 d 4 h". */
function span(ms: number): string {
  const m = Math.max(0, Math.floor(ms / 60000));
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  if (h < 24) return m % 60 ? `${h} h ${m % 60} min` : `${h} h`;
  const d = Math.floor(h / 24);
  return h % 24 ? `${d} d ${h % 24} h` : `${d} d`;
}

/** How long a ticket has been open, or how long it took, and whether it is past its restore SLA. */
function ageOf(i: any, now: Date): { text: string; late: boolean; done: boolean } | null {
  const opened = parseInstant(i.created_at);
  if (!opened) return null;
  const st = stateOf(i.status);
  if (st !== "open") {
    const end = parseInstant(i.restored_at) || parseInstant(i.closed_at);
    return end ? { text: `took ${span(end.getTime() - opened.getTime())}`, late: false, done: true } : null;
  }
  const due = parseInstant(i.sla_restore_due);
  return { text: span(now.getTime() - opened.getTime()), late: !!due && due.getTime() < now.getTime(), done: false };
}

function StatusPill({ status }: { status: unknown }) {
  return (
    <span className={`ib-status ${statusTone(status)}`}>
      <span className="ib-status-dot" aria-hidden="true" />
      {humanStatus(status)}
    </span>
  );
}

function Age({ i, now }: { i: any; now: Date }) {
  const a = ageOf(i, now);
  if (!a) return <span className="muted">—</span>;
  return (
    <span className={"ib-age" + (a.late ? " late" : "") + (a.done ? " done" : "")}>
      <span className="ib-age-time">{a.text}</span>
      {a.late && <span className="ib-age-late">past restore SLA</span>}
    </span>
  );
}

export default function IncidentBoard({ tick, profile }: { tick: number; profile?: any }) {
  // null until the first answer: loading is a state of its own, never an empty board.
  const [rows, setRows] = useState<any[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [retry, setRetry] = useState(0);
  // Which tickets, the priority and the region live in the address, so a view can be linked
  // ("/incidents?p=P1") and Back returns to it.
  const [params, setParams] = useSearchParams();
  const view: StateKey = (STATES.find((s) => s.key === params.get("state"))?.key as StateKey) || "open";
  const priority = PRIORITIES.includes(String(params.get("p") || "").toUpperCase()) ? String(params.get("p")).toUpperCase() : "";
  const region = String(params.get("region") || "").toUpperCase().slice(0, 16);
  const setParam = (key: string, value: string, fallback = "") => {
    const next = new URLSearchParams(params);
    if (!value || value === fallback) next.delete(key);
    else next.set(key, value);
    setParams(next, { replace: true });
  };
  // What is typed in the search box, and the settled text the API is asked with.
  const [text, setText] = useState("");
  const [query, setQuery] = useState("");
  useEffect(() => {
    const t = window.setTimeout(() => setQuery(settle(text)), SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(t);
  }, [text]);
  const nav = useNavigate();
  const narrow = useNarrow();
  const now = useMinute();

  // The API narrows by region and search; the switch and the priority buttons filter here, so
  // every button can say how many tickets it holds.
  useEffect(() => {
    let live = true;
    const qs = new URLSearchParams();
    if (region) qs.set("region", region);
    if (query) qs.set("q", query); // ticket number, site code or site name; the API matches any case
    const q = qs.toString() ? `?${qs}` : "";
    api
      .incidents(q)
      .then((r) => {
        if (!live) return;
        setRows(Array.isArray(r) ? r : []);
        setFailed(false);
      })
      .catch(() => {
        if (live) setFailed(true);
      });
    return () => {
      live = false;
    };
  }, [tick, region, query, retry]);

  const all = rows || [];
  const byState = useMemo(() => {
    const out: Record<StateKey, number> = { open: 0, restored: 0, closed: 0, all: 0 };
    for (const i of all) {
      out[stateOf(i.status)] += 1;
      out.all += 1;
    }
    return out;
  }, [all]);
  const inView = useMemo(() => (view === "all" ? all : all.filter((i) => stateOf(i.status) === view)), [all, view]);
  const byPriority = useMemo(() => {
    const out: Record<string, number> = {};
    for (const i of inView) out[i.priority] = (out[i.priority] || 0) + 1;
    return out;
  }, [inView]);
  const shown = priority ? inView.filter((i) => i.priority === priority) : inView;

  const regionKeys = profile?.regions && typeof profile.regions === "object" ? Object.keys(profile.regions) : REGIONS;
  const filtered = Boolean(region || priority || query || view !== "open");
  const clearFilters = () => {
    setParams(new URLSearchParams(), { replace: true });
    setText("");
    setQuery("");
  };

  const mpesa = (i: any, word: string) =>
    i.mpesa_risk ? (
      <span className="ib-flag" title={MPESA_TITLE}>
        {word}
      </span>
    ) : null;

  const viewWord = view === "all" ? "" : `${STATES.find((s) => s.key === view)?.label.toLowerCase()} `;

  return (
    <div className="ib">
      <div className="page-head">
        <div>
          <h1>Incident board</h1>
          <p className="lead">Every ticket, worst first and oldest first. Open one for the full record.</p>
        </div>
      </div>

      <div className="ib-toolbar">
        <div className="seg ib-states" role="group" aria-label="Which tickets">
          {STATES.map((s) => (
            <button key={s.key} type="button" aria-pressed={view === s.key} onClick={() => setParam("state", s.key, "open")}>
              {s.label}
              <span className="ib-n">{rows === null ? "" : byState[s.key]}</span>
            </button>
          ))}
        </div>
        <div className="ib-prios" role="group" aria-label="Priority">
          {PRIORITIES.map((p) => {
            const n = byPriority[p] || 0;
            return (
              <button
                key={p}
                type="button"
                className={`ib-prio ${p}`}
                aria-pressed={priority === p}
                title={priorityTitle(p)}
                onClick={() => setParam("p", priority === p ? "" : p)}
              >
                <span className="ib-prio-key">{p}</span>
                <span className="ib-n">{rows === null ? "" : n}</span>
              </button>
            );
          })}
        </div>
        <div className="ib-tools">
          <label className="ib-search">
            <Search size={16} strokeWidth={1.75} aria-hidden="true" />
            <input
              type="search"
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") setQuery(settle(text)); // no need to wait for the pause
              }}
              maxLength={SEARCH_MAX}
              placeholder="Ticket, site code or site"
              aria-label="Search ticket number, site code or site name"
            />
          </label>
          <select value={region} onChange={(e) => setParam("region", e.target.value)} aria-label="Filter by region">
            <option value="">All regions</option>
            {regionKeys.map((r) => (
              <option key={r} value={r}>
                {regionName(r, profile)}
              </option>
            ))}
          </select>
        </div>
      </div>

      <div className="ib-summary">
        {/* A search or a filter answers out loud, so a screen reader hears what the board now shows. */}
        <span role="status">
          {rows === null
            ? ""
            : `${shown.length} ${viewWord}${shown.length === 1 ? "ticket" : "tickets"}${priority ? ` at ${priority}` : ""}${
                region ? ` in ${regionName(region, profile)}` : ""
              }${query ? ` matching “${query}”` : ""}`}
        </span>
        {filtered && (
          <button type="button" className="btn sm quiet" onClick={clearFilters}>
            Clear filters
          </button>
        )}
      </div>

      <div className="panel ib-panel">
        {failed && rows === null ? (
          <div className="empty" role="alert">
            Couldn't load the tickets.{" "}
            <button className="btn sm" onClick={() => setRetry((n) => n + 1)}>
              Retry
            </button>
          </div>
        ) : rows !== null && shown.length === 0 ? null : narrow ? (
          // Phone: one card per ticket. The site and how long it has been open, then the number,
          // then where, who and the state.
          <div className="ib-cards" aria-busy={rows === null || undefined}>
            {rows === null && (
              <div className="skeleton-rows" aria-hidden="true">
                {Array.from({ length: 8 }, (_, i) => (
                  <span key={i} className="skeleton" />
                ))}
              </div>
            )}
            {shown.map((i) => (
              <Link key={i.id} to={`/incidents/${i.id}`} className={`ib-card ${i.priority}`}>
                <div className="ib-card-top">
                  <span className={`pill ${i.priority}`} title={priorityTitle(i.priority)}>
                    {i.priority}
                  </span>
                  <span className="ib-card-site">{i.site_name || i.site_id}</span>
                  <Age i={i} now={now} />
                </div>
                <div className="ib-sub">
                  <span className="mono">{i.incident_number}</span>
                  <span className="mono">{i.site_id}</span>
                </div>
                <div className="ib-card-facts">
                  <span>{regionName(i.region_code, profile)}</span>
                  <span className="owner-cell">{ownerName(i.assignee_name)}</span>
                  <StatusPill status={i.status} />
                  {mpesa(i, "M‑PESA at risk")}
                </div>
              </Link>
            ))}
          </div>
        ) : (
          <table className="ib-table">
            <colgroup>
              <col className="ib-c-prio" />
              <col className="ib-c-site" />
              <col className="ib-c-region" />
              <col className="ib-c-owner" />
              <col className="ib-c-status" />
              <col className="ib-c-age" />
              <col className="ib-c-mpesa" />
            </colgroup>
            <thead>
              <tr>
                <th>Priority</th>
                <th>Site and ticket</th>
                <th className="ib-c-region">Region</th>
                <th className="ib-c-owner" title="Whoever holds the ticket now: the vendor, the field engineer or the NOC queue">
                  Owner
                </th>
                <th>Status</th>
                <th title="How long it has been open; for a restored or closed ticket, how long it took">Open for</th>
                <th>M‑PESA</th>
              </tr>
            </thead>
            <tbody aria-busy={rows === null || undefined}>
              {rows === null &&
                Array.from({ length: 8 }, (_, k) => (
                  <tr key={"sk-" + k}>
                    <td colSpan={7}>
                      <div className="skeleton" />
                    </td>
                  </tr>
                ))}
              {shown.map((i) => (
                <tr key={i.id} className={`ib-row ${i.priority}`} onClick={() => nav(`/incidents/${i.id}`)}>
                  <td>
                    <span className={`pill ${i.priority}`} title={priorityTitle(i.priority)}>
                      {i.priority}
                    </span>
                  </td>
                  <td className="ib-site-cell">
                    {/* The link is the keyboard's way in; the row click stays for the mouse. */}
                    <Link className="ib-site" to={`/incidents/${i.id}`} onClick={(e) => e.stopPropagation()}>
                      {i.site_name || i.site_id || i.incident_number}
                    </Link>
                    {/* The second line: the ticket number, then the site code and the domain. On a
                        narrower board the region (then the owner) column folds in here instead, in
                        the code's and the domain's place. */}
                    <div className="ib-sub">
                      <span className="mono ib-num">{i.incident_number}</span>
                      <span className="ib-fold-region">{regionName(i.region_code, profile)}</span>
                      <span className="ib-fold-owner">{ownerName(i.assignee_name)}</span>
                      <span className="mono ib-code">{i.site_id}</span>
                      {i.failure_domain && <span className="ib-domain">{humanEnum(i.failure_domain)}</span>}
                    </div>
                  </td>
                  <td className="ib-c-region" title={i.region_code}>
                    {regionName(i.region_code, profile)}
                  </td>
                  <td className="ib-c-owner owner-cell">{ownerName(i.assignee_name)}</td>
                  <td>
                    <StatusPill status={i.status} />
                  </td>
                  <td>
                    <Age i={i} now={now} />
                  </td>
                  <td>{mpesa(i, "at risk")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {rows !== null && shown.length === 0 && filtered && (
          <div className="empty">
            No {viewWord}tickets match these filters.{" "}
            <button className="btn sm" onClick={clearFilters}>
              Clear filters
            </button>
          </div>
        )}
        {rows !== null && shown.length === 0 && !filtered && (
          <div className="empty">
            {all.length > 0 ? (
              <>
                No open tickets. Restored and closed ones are under <button className="btn sm" onClick={() => setParam("state", "all", "open")}>All</button>
              </>
            ) : (
              <>
                No tickets yet. Launch the storm from <Link to="/mission">Mission control</Link> to open some.
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
