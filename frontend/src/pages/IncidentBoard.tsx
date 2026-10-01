import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { humanStatus } from "../lib/agents";

export default function IncidentBoard({ tick }: { tick: number }) {
  const [rows, setRows] = useState<any[]>([]);
  const [region, setRegion] = useState("");
  const [priority, setPriority] = useState("");
  const nav = useNavigate();

  useEffect(() => {
    const qs = new URLSearchParams();
    if (region) qs.set("region", region);
    if (priority) qs.set("priority", priority);
    const q = qs.toString() ? `?${qs}` : "";
    api.incidents(q).then(setRows).catch(console.error);
  }, [tick, region, priority]);

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Incident Board</h1>
          <p className="lead">Every open and closed ticket, newest first. Open one for the fields, the agents' reasoning and the timeline.</p>
        </div>
        <div className="page-actions">
        <select value={region} onChange={(e) => setRegion(e.target.value)} aria-label="Filter by region">
          <option value="">All regions</option>
          {["NBI_E", "NBI_W", "MTK", "CST", "RFT", "WNY"].map((r) => (
            <option key={r} value={r}>
              {r}
            </option>
          ))}
        </select>
        <select value={priority} onChange={(e) => setPriority(e.target.value)} aria-label="Filter by priority">
          <option value="">All priorities</option>
          {["P1", "P2", "P3", "P4"].map((p) => (
            <option key={p} value={p}>
              {p}
            </option>
          ))}
        </select>
        </div>
      </div>
      <div className="panel">
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
              <th>M-PESA</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((i) => (
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
                <td>{String(i.failure_domain || "").toLowerCase()}</td>
                <td>{i.assignee_name}</td>
                <td className="status">{humanStatus(i.status)}</td>
                <td className="muted">{i.mpesa_risk ? "at risk" : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {rows.length === 0 && <div className="empty">No incidents match filters.</div>}
      </div>
    </div>
  );
}
