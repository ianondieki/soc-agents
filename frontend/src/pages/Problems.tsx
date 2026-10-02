import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { humanEnum } from "../lib/agents";

const COLS = 6;

export default function Problems({ tick }: { tick: number }) {
  // null until the first answer, so loading never reads as "no problems".
  const [rows, setRows] = useState<any[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    let live = true;
    api
      .problems()
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
  }, [tick, retry]);

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Problems</h1>
          <p className="lead">Sites that keep failing on power, microwave or fibre, across the regions.</p>
        </div>
      </div>
      <div className="panel">
        {failed && rows === null ? (
          <div className="empty" role="alert">
            Couldn't load the problem records.{" "}
            <button className="btn sm" onClick={() => setRetry((n) => n + 1)}>
              Retry
            </button>
          </div>
        ) : rows !== null && rows.length === 0 ? null : (
          <table>
            <thead>
              <tr>
                <th>Problem</th>
                <th>Site</th>
                <th>Region</th>
                <th className="num">Count</th>
                <th>Domain</th>
                <th>Summary</th>
              </tr>
            </thead>
            <tbody aria-busy={rows === null || undefined}>
              {rows === null &&
                Array.from({ length: 6 }, (_, i) => (
                  <tr key={"sk-" + i}>
                    <td colSpan={COLS}>
                      <div className="skeleton" />
                    </td>
                  </tr>
                ))}
              {(rows || []).map((p) => (
                <tr key={p.id}>
                  <td className="mono">{p.problem_number}</td>
                  <td className="mono">{p.site_id}</td>
                  <td className="mono">{p.region_code}</td>
                  <td className="num">{p.occurrence_count}</td>
                  <td>{humanEnum(p.dominant_failure_domain)}</td>
                  <td className="muted">{p.summary}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {rows !== null && rows.length === 0 && (
          <div className="empty">
            No open problems yet. A site that fails three times opens one; inject repeats from{" "}
            <Link to="/settings">Settings</Link>.
          </div>
        )}
      </div>
    </div>
  );
}
