import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { humanEnum } from "../lib/agents";
import { fmtDateTime } from "../lib/time";

const COLS = 7;

export default function ShiftDesk({ tick }: { tick: number }) {
  // null until the first answer, so loading never reads as an empty ledger.
  const [ledger, setLedger] = useState<any[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [retry, setRetry] = useState(0);
  const [handover, setHandover] = useState<any>(null);

  useEffect(() => {
    let live = true;
    api
      .ledger()
      .then((r) => {
        if (!live) return;
        setLedger(Array.isArray(r) ? r : []);
        setFailed(false);
      })
      .catch((e) => {
        console.error(e);
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
          <h1>Shift desk</h1>
          <p className="lead">The day and night ledger, and the handover package for the incoming shift.</p>
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
        <div className="panel-head">
          <h3>Shift ledger</h3>
          <span className="muted">Times in EAT</span>
        </div>
        {failed && ledger === null ? (
          <div className="empty" role="alert">
            Couldn't load the shift ledger.{" "}
            <button className="btn sm" onClick={() => setRetry((n) => n + 1)}>
              Retry
            </button>
          </div>
        ) : (
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
            <tbody aria-busy={ledger === null || undefined}>
              {ledger === null &&
                Array.from({ length: 8 }, (_, i) => (
                  <tr key={"sk-" + i}>
                    <td colSpan={COLS}>
                      <div className="skeleton" />
                    </td>
                  </tr>
                ))}
              {(ledger || []).map((r, i) => (
                <tr key={i}>
                  <td className="muted">{fmtDateTime(r.row_written_at)}</td>
                  <td>{r.incident_number}</td>
                  <td>
                    <span className={`pill ${r.priority}`}>{r.priority}</span>
                  </td>
                  <td>{r.site}</td>
                  <td>{r.region_code}</td>
                  <td>{r.owner}</td>
                  <td>{humanEnum(r.shift_type)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {ledger !== null && ledger.length === 0 && (
          <div className="empty">
            No ledger rows yet. Each new ticket writes one; launch the storm from <Link to="/">Mission control</Link>.
          </div>
        )}
      </div>
    </div>
  );
}
