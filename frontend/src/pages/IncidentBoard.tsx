import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";

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
      <h2 style={{ marginTop: 0 }}>Incident Board</h2>
      <div className="form-row">
        <select value={region} onChange={(e) => setRegion(e.target.value)}>
          <option value="">All regions</option>
          {["NBI_E", "NBI_W", "MTK", "CST", "RFT", "WNY"].map((r) => (
            <option key={r} value={r}>
              {r}
            </option>
          ))}
        </select>
        <select value={priority} onChange={(e) => setPriority(e.target.value)}>
          <option value="">All priorities</option>
          {["P1", "P2", "P3", "P4"].map((p) => (
            <option key={p} value={p}>
              {p}
            </option>
          ))}
        </select>
      </div>
      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>Pri</th>
              <th>INC</th>
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
                <td>{i.failure_domain}</td>
                <td>{i.assignee_name}</td>
                <td>{i.status}</td>
                <td>{i.mpesa_risk ? "Y" : "N"}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {rows.length === 0 && <div className="empty">No incidents match filters.</div>}
      </div>
    </div>
  );
}
