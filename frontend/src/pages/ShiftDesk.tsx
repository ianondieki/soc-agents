import { useEffect, useState } from "react";
import { api } from "../api";
import { fmtDateTime } from "../lib/time";

export default function ShiftDesk({ tick }: { tick: number }) {
  const [ledger, setLedger] = useState<any[]>([]);
  const [handover, setHandover] = useState<any>(null);

  useEffect(() => {
    api.ledger().then(setLedger).catch(console.error);
  }, [tick]);

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Shift Desk</h1>
          <p className="lead">The day and night ledger (EAT), and the handover package for the incoming shift.</p>
        </div>
        <div className="page-actions">
          <button
            className="btn primary"
            onClick={async () => {
              const h = await api.handover();
              setHandover(h);
            }}
          >
            Generate handover preview
          </button>
        </div>
      </div>
      {handover && (
        <div className="panel" style={{ marginBottom: "var(--s4)" }}>
          <h3>{handover.subject}</h3>
          <div className="pre">{handover.body}</div>
        </div>
      )}
      <div className="panel">
        <h3>Shift ledger rows</h3>
        <table>
          <thead>
            <tr>
              <th>Time (EAT)</th>
              <th>Incident</th>
              <th>Priority</th>
              <th>Site</th>
              <th>Region</th>
              <th>Owner</th>
              <th>Shift</th>
            </tr>
          </thead>
          <tbody>
            {ledger.map((r, i) => (
              <tr key={i}>
                <td className="muted">{fmtDateTime(r.row_written_at)}</td>
                <td>{r.incident_number}</td>
                <td>
                  <span className={`pill ${r.priority}`}>{r.priority}</span>
                </td>
                <td>{r.site}</td>
                <td>{r.region_code}</td>
                <td>{r.owner}</td>
                <td>{String(r.shift_type || "").toLowerCase()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
