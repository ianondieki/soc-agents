import { useEffect, useState, type CSSProperties } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api";
import { humanEnum, humanStatus } from "../lib/agents";
import { IconDot } from "../lib/icons";

const REGIONS = ["NBI_E", "NBI_W", "MTK", "CST", "RFT", "WNY"];
const PRIORITIES = ["P1", "P2", "P3", "P4"];
const COLS = 8;

/** Phones: below this width a ticket list reads as two stacked lines per row, not a sideways table. */
export const NARROW_QUERY = "(max-width: 600px)";

/** A `.row` whose one child takes the whole width (the stacked phone rows). */
export const ONE_COL: CSSProperties = { gridTemplateColumns: "minmax(0, 1fr)" };

/**
 * True while the viewport matches `query` (default: phone width). Shared by the Incident board
 * and the Shift desk, which swap their table for stacked rows on a phone.
 */
export function useNarrow(query: string = NARROW_QUERY): boolean {
  const read = () => typeof window !== "undefined" && typeof window.matchMedia === "function" && window.matchMedia(query).matches;
  const [narrow, setNarrow] = useState<boolean>(read);
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const mq = window.matchMedia(query);
    const sync = () => setNarrow(mq.matches);
    sync();
    if (typeof mq.addEventListener === "function") {
      mq.addEventListener("change", sync);
      return () => mq.removeEventListener("change", sync);
    }
    mq.addListener(sync); // Safari < 14
    return () => mq.removeListener(sync);
  }, [query]);
  return narrow;
}

export default function IncidentBoard({ tick }: { tick: number }) {
  // null until the first answer: loading is a state of its own, never an empty board.
  const [rows, setRows] = useState<any[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [retry, setRetry] = useState(0);
  const [region, setRegion] = useState("");
  const [priority, setPriority] = useState("");
  const nav = useNavigate();
  const narrow = useNarrow();

  useEffect(() => {
    let live = true;
    const qs = new URLSearchParams();
    if (region) qs.set("region", region);
    if (priority) qs.set("priority", priority);
    const q = qs.toString() ? `?${qs}` : "";
    api
      .incidents(q)
      .then((r) => {
        if (!live) return;
        setRows(Array.isArray(r) ? r : []);
        setFailed(false);
      })
      .catch((e) => {
        console.error(e);
        if (live) setFailed(true);
      });
    return () => {
      live = false;
    };
  }, [tick, region, priority, retry]);

  const filtered = Boolean(region || priority);
  const clearFilters = () => {
    setRegion("");
    setPriority("");
  };

  const mpesa = (i: any, word: string) =>
    i.mpesa_risk ? (
      <span className="attn danger">
        <IconDot /> {word}
      </span>
    ) : null;

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Incident board</h1>
          <p className="lead">Every open and closed ticket, newest first. Open one for the full record.</p>
        </div>
        <div className="page-actions">
          <select value={region} onChange={(e) => setRegion(e.target.value)} aria-label="Filter by region">
            <option value="">All regions</option>
            {REGIONS.map((r) => (
              <option key={r} value={r}>
                {r}
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
      <div className="panel">
        {failed && rows === null ? (
          <div className="empty" role="alert">
            Couldn't load the incident board.{" "}
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
                    <span className={`pill ${i.priority}`}>{i.priority}</span>
                    <span className="row-id">{i.incident_number}</span>
                    <span className="mono muted">{i.site_id}</span>
                  </div>
                  <div className="facts">
                    <span>{i.site_name}</span>
                    <span>{i.assignee_name}</span>
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
                <th>Incident</th>
                <th>Site</th>
                <th>Region</th>
                <th>Domain</th>
                <th>Owner</th>
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
                    <span className={`pill ${i.priority}`}>{i.priority}</span>
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
                  <td className="mono">{i.region_code}</td>
                  <td>{humanEnum(i.failure_domain)}</td>
                  <td>{i.assignee_name}</td>
                  <td className="status">{humanStatus(i.status)}</td>
                  <td>{mpesa(i, "at risk") ?? <span className="muted">—</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {rows !== null && rows.length === 0 && filtered && (
          <div className="empty">
            No incidents match these filters.{" "}
            <button className="btn sm" onClick={clearFilters}>
              Clear filters
            </button>
          </div>
        )}
        {rows !== null && rows.length === 0 && !filtered && (
          <div className="empty">
            No incidents yet. Launch the storm from <Link to="/">Mission control</Link> to open some.
          </div>
        )}
      </div>
    </div>
  );
}
