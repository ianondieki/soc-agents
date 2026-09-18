import { useEffect, useState } from "react";
import { api } from "../api";

export default function Problems({ tick }: { tick: number }) {
  const [rows, setRows] = useState<any[]>([]);
  useEffect(() => {
    api.problems().then(setRows).catch(console.error);
  }, [tick]);
  return (
    <div>
      <h2 style={{ marginTop: 0 }}>Problem Board</h2>
      <p className="muted">Recurring / chronic sites (power, MW, fibre) across Safaricom regions.</p>
      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>Problem</th>
              <th>Site</th>
              <th>Region</th>
              <th>Count</th>
              <th>Domain</th>
              <th>Summary</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((p) => (
              <tr key={p.id}>
                <td>{p.problem_number}</td>
                <td>{p.site_id}</td>
                <td>{p.region_code}</td>
                <td>{p.occurrence_count}</td>
                <td>{p.dominant_failure_domain}</td>
                <td className="muted">{p.summary}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {rows.length === 0 && <div className="empty">No open problems yet. Repeat a site failure 3× to open one.</div>}
      </div>
    </div>
  );
}
