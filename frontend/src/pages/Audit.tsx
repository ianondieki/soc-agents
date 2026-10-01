import { useEffect, useState } from "react";
import { api } from "../api";
import { fmtDateTime } from "../lib/time";

export default function Audit({ tick }: { tick: number }) {
  const [rows, setRows] = useState<any[]>([]);
  useEffect(() => {
    api.audit().then(setRows).catch(console.error);
  }, [tick]);
  return (
    <div>
      <h1 className="page-title">Audit Explorer</h1>
      <p className="muted">Every agent step and human decision, with its reason, newest first. This is the regulator-facing record.</p>
      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>Time (EAT)</th>
              <th>Actor</th>
              <th>Action</th>
              <th>Rationale</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((a) => (
              <tr key={a.id}>
                <td className="muted">{fmtDateTime(a.ts)}</td>
                <td>{a.actor}</td>
                <td>{a.action}</td>
                <td className="muted">{a.rationale}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
