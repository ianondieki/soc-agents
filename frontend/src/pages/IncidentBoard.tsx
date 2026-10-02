import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api";
import { humanEnum, humanStatus } from "../lib/agents";

const REGIONS = ["NBI_E", "NBI_W", "MTK", "CST", "RFT", "WNY"];
const PRIORITIES = ["P1", "P2", "P3", "P4"];
const COLS = 8;

export default function IncidentBoard({ tick }: { tick: number }) {
  // null until the first answer: loading is a state of its own, never an empty board.
  const [rows, setRows] = useState<any[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [retry, setRetry] = useState(0);
  const [region, setRegion] = useState("");
  const [priority, setPriority] = useState("");
  const nav = useNavigate();

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
                <tr key={i.id} style={{ cursor: "pointer" }} onClick={() => nav(`/incidents/${i.id}`)}>
                  <td>
                    <span className={`pill ${i.priority}`}>{i.priority}</span>
                  </td>
                  <td>{i.incident_number}</td>
                  <td>
                    {i.site_id}
                    <div className="muted">{i.site_name}</div>
                  </td>
                  <td>{i.region_code}</td>
                  <td>{humanEnum(i.failure_domain)}</td>
                  <td>{i.assignee_name}</td>
                  <td className="status">{humanStatus(i.status)}</td>
                  <td className="muted">{i.mpesa_risk ? "at risk" : "—"}</td>
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
