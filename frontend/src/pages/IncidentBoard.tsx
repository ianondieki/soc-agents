import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api";
import { MPESA_TITLE, humanEnum, humanStatus, priorityTitle, regionName } from "../lib/agents";
import { IconDot } from "../lib/icons";
import { ONE_COL, useNarrow } from "../lib/layout";

const REGIONS = ["NBI_E", "NBI_W", "MTK", "CST", "RFT", "WNY"];
const PRIORITIES = ["P1", "P2", "P3", "P4"];
const COLS = 8;
/** Typing settles this long before the board asks the API (`q`), so a ticket number is one request. */
const SEARCH_DEBOUNCE_MS = 250;

/**
 * Who holds the ticket, as a name: a vendor or role enum through `humanEnum` and capitalised as a
 * name ("EGYPRO_FIBRE" → "Egypro Fibre", "FIELD_ENGINEER" → "Field Engineer"); a code the floor
 * writes as one word or with a hyphen ("TETRANET", "ATC", "NOC-QUEUE") stays as written. The same
 * rule as the Wallboard's owner line.
 */
function ownerName(value: unknown): string {
  const raw = String(value ?? "").trim();
  // A code keeps its hyphen on one line ("NOC‑QUEUE" never splits into "NOC-" and "QUEUE").
  if (!/^[A-Z]+(_[A-Z]+)+$/.test(raw)) return raw.replace(/-/g, "\u2011");
  return humanEnum(raw)
    .split(" ")
    .map((w) => (/^[a-z]/.test(w) ? w[0].toUpperCase() + w.slice(1) : w))
    .join(" ");
}

// The phone-layout helpers moved to lib/layout.ts; re-exported here so older imports still work.
export { NARROW_QUERY, ONE_COL, useNarrow } from "../lib/layout";

export default function IncidentBoard({ tick, profile }: { tick: number; profile?: any }) {
  // null until the first answer: loading is a state of its own, never an empty board.
  const [rows, setRows] = useState<any[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [retry, setRetry] = useState(0);
  const [region, setRegion] = useState("");
  const [priority, setPriority] = useState("");
  // What is typed in the search box, and the settled text the API is asked with.
  const [text, setText] = useState("");
  const [query, setQuery] = useState("");
  useEffect(() => {
    const t = window.setTimeout(() => setQuery(text.trim()), SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(t);
  }, [text]);
  const nav = useNavigate();
  const narrow = useNarrow();

  useEffect(() => {
    let live = true;
    const qs = new URLSearchParams();
    if (region) qs.set("region", region);
    if (priority) qs.set("priority", priority);
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
  }, [tick, region, priority, query, retry]);

  const filtered = Boolean(region || priority || query);
  const clearFilters = () => {
    setRegion("");
    setPriority("");
    setText("");
    setQuery("");
  };

  const mpesa = (i: any, word: string) =>
    i.mpesa_risk ? (
      <span className="attn danger" title={MPESA_TITLE}>
        <IconDot /> {word}
      </span>
    ) : null;

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Incident board</h1>
          <p className="lead">Every open and closed ticket, by priority, oldest first. Open one for the full record.</p>
        </div>
        <div className="page-actions">
          <input
            type="search"
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") setQuery(text.trim()); // no need to wait for the pause
            }}
            placeholder="Search ticket or site"
            aria-label="Search ticket number, site code or site name"
          />
          <select value={region} onChange={(e) => setRegion(e.target.value)} aria-label="Filter by region">
            <option value="">All regions</option>
            {REGIONS.map((r) => (
              <option key={r} value={r}>
                {regionName(r, profile)}
              </option>
            ))}
          </select>
          <select value={priority} onChange={(e) => setPriority(e.target.value)} aria-label="Filter by priority">
            <option value="">All priorities</option>
            {PRIORITIES.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </div>
      </div>
      {/* A search or a filter answers out loud, so a screen reader hears what the board now shows. */}
      <span className="sr-only" role="status">
        {rows !== null && filtered ? `${rows.length} ${rows.length === 1 ? "ticket matches" : "tickets match"}` : ""}
      </span>
      <div className="panel">
        {failed && rows === null ? (
          <div className="empty" role="alert">
            Couldn't load the tickets.{" "}
            <button className="btn sm" onClick={() => setRetry((n) => n + 1)}>
              Retry
            </button>
          </div>
        ) : rows !== null && rows.length === 0 ? null : narrow ? (
          // Phone: two stacked lines per ticket. Line 1 names it, line 2 says where and who.
          <div aria-busy={rows === null || undefined}>
            {rows === null && (
              <div className="skeleton-rows" aria-hidden="true">
                {Array.from({ length: 8 }, (_, i) => (
                  <span key={i} className="skeleton" />
                ))}
              </div>
            )}
            {(rows || []).map((i) => (
              <Link key={i.id} to={`/incidents/${i.id}`} className="row" style={ONE_COL}>
                <div className="row-main">
                  <div className="row-title">
                    <span className={`pill ${i.priority}`} title={priorityTitle(i.priority)}>
                      {i.priority}
                    </span>
                    <span className="row-id">{i.incident_number}</span>
                    <span className="mono muted">{i.site_id}</span>
                  </div>
                  <div className="facts">
                    <span>{i.site_name}</span>
                    <span>{ownerName(i.assignee_name)}</span>
                    <span>{humanStatus(i.status)}</span>
                    {mpesa(i, "M‑PESA at risk")}
                  </div>
                </div>
              </Link>
            ))}
          </div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Priority</th>
                <th>Ticket</th>
                <th>Site</th>
                <th>Region</th>
                <th>Domain</th>
                <th title="Whoever holds the ticket now: the vendor, the field engineer or the NOC queue">Owner</th>
                <th>Status</th>
                <th>M‑PESA</th>
              </tr>
            </thead>
            <tbody aria-busy={rows === null || undefined}>
              {rows === null &&
                Array.from({ length: 8 }, (_, i) => (
                  <tr key={"sk-" + i}>
                    <td colSpan={COLS}>
                      <div className="skeleton" />
                    </td>
                  </tr>
                ))}
              {(rows || []).map((i) => (
                <tr key={i.id} onClick={() => nav(`/incidents/${i.id}`)}>
                  <td>
                    <span className={`pill ${i.priority}`} title={priorityTitle(i.priority)}>
                      {i.priority}
                    </span>
                  </td>
                  <td>
                    {/* The link is the keyboard's way in; the row click stays for the mouse. */}
                    <Link className="mono" to={`/incidents/${i.id}`} onClick={(e) => e.stopPropagation()}>
                      {i.incident_number}
                    </Link>
                  </td>
                  <td>
                    <span className="mono">{i.site_id}</span>
                    <div className="muted">{i.site_name}</div>
                  </td>
                  <td title={i.region_code}>{regionName(i.region_code, profile)}</td>
                  <td>{humanEnum(i.failure_domain)}</td>
                  <td>{ownerName(i.assignee_name)}</td>
                  <td className="status">{humanStatus(i.status)}</td>
                  <td>{mpesa(i, "at risk") ?? <span className="muted">—</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {rows !== null && rows.length === 0 && filtered && (
          <div className="empty">
            No tickets match these filters.{" "}
            <button className="btn sm" onClick={clearFilters}>
              Clear filters
            </button>
          </div>
        )}
        {rows !== null && rows.length === 0 && !filtered && (
          <div className="empty">
            No tickets yet. Launch the storm from <Link to="/">Mission control</Link> to open some.
          </div>
        )}
      </div>
    </div>
  );
}
