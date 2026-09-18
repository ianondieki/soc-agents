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
      <h2 style={{ marginTop: 0 }}>Shift Desk</h2>
      <p className="muted">EAT day/night ledger + handover package for incoming shift.</p>
      <button
        className="btn primary"
        onClick={async () => {
          const h = await api.handover();
          setHandover(h);
        }}
      >
        Generate / send handover preview
      </button>
      {handover && (
        <div className="panel" style={{ marginTop: "1rem" }}>
          <h3>{handover.subject}</h3>
          <div className="pre">{handover.body}</div>
        </div>
      )}
      <div className="panel" style={{ marginTop: "1rem" }}>
        <h3>Shift ledger rows</h3>
        <table>
          <thead>
            <tr>
              <th>Time (EAT)</th>
              <th>INC</th>
              <th>Pri</th>
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
                <td>{r.shift_type}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
