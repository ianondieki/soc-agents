import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { humanEnum } from "../lib/agents";
import { IconAlert } from "../lib/icons";
import { fmtDateTime } from "../lib/time";
import { ONE_COL, useNarrow } from "./IncidentBoard";

const COLS = 7;

/** The ledger stores "SFC-RFT-HUB-NKR Nakuru Rift HUB": the site code, a space, the site name. */
function splitSite(site: unknown): { code: string; name: string } {
  const s = String(site ?? "").trim();
  const cut = s.indexOf(" ");
  return cut < 0 ? { code: s, name: "" } : { code: s.slice(0, cut), name: s.slice(cut + 1) };
}

export default function ShiftDesk({ tick }: { tick: number }) {
  // null until the first answer, so loading never reads as an empty ledger.
  const [ledger, setLedger] = useState<any[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [retry, setRetry] = useState(0);
  const [handover, setHandover] = useState<any>(null);
  const [handoverBusy, setHandoverBusy] = useState(false);
  const [handoverError, setHandoverError] = useState("");
  const narrow = useNarrow();

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

  const generateHandover = async () => {
    setHandoverBusy(true);
    setHandoverError("");
    try {
      const h = await api.handover();
      setHandover(h);
    } catch (e) {
      console.error(e);
      setHandoverError("Couldn't build the handover preview.");
    } finally {
      setHandoverBusy(false);
    }
  };

  const empty = ledger !== null && ledger.length === 0;

  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <h1>Shift desk</h1>
          <p className="lead">The day and night ledger, and the handover package for the incoming shift.</p>
        </div>
        <div className="page-actions">
          {handoverError && (
            <span className="state danger" role="alert">
              <IconAlert /> {handoverError}
            </span>
          )}
          <button className="btn primary" disabled={handoverBusy} aria-busy={handoverBusy || undefined} onClick={generateHandover}>
            {handoverBusy ? "Generating handover…" : "Generate handover preview"}
          </button>
        </div>
      </div>
      {handover && (
        <div className="panel">
          <h2 className="panel-title">{handover.subject}</h2>
          <div className="pre">{handover.body}</div>
        </div>
      )}
      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Shift ledger</h2>
          {/* The table header says "(EAT)"; the stacked phone rows have no header, so they say it here. */}
          {narrow && <span className="muted">Times in EAT</span>}
        </div>
        {failed && ledger === null ? (
          <div className="empty" role="alert">
            Couldn't load the shift ledger.{" "}
            <button className="btn sm" onClick={() => setRetry((n) => n + 1)}>
              Retry
            </button>
          </div>
        ) : empty ? null : narrow ? (
          // Phone: line 1 names the ticket, line 2 says where, who and when.
          <div aria-busy={ledger === null || undefined}>
            {ledger === null && (
              <div className="skeleton-rows" aria-hidden="true">
                {Array.from({ length: 8 }, (_, i) => (
                  <span key={i} className="skeleton" />
                ))}
              </div>
            )}
            {(ledger || []).map((r, i) => {
              const site = splitSite(r.site);
              return (
                <div key={i} className="row static" style={ONE_COL}>
                  <div className="row-main">
                    <div className="row-title">
                      <span className={`pill ${r.priority}`}>{r.priority}</span>
                      <span className="row-id">{r.incident_number}</span>
                      <span className="mono muted">{site.code}</span>
                    </div>
                    <div className="facts">
                      {site.name && <span>{site.name}</span>}
                      <span>{r.owner}</span>
                      <span>{humanEnum(r.shift_type)}</span>
                      <span className="mono">{fmtDateTime(r.row_written_at)}</span>
                    </div>
                  </div>
                </div>
              );
            })}
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
              {(ledger || []).map((r, i) => {
                const site = splitSite(r.site);
                return (
                  <tr key={i}>
                    <td className="muted mono">{fmtDateTime(r.row_written_at)}</td>
                    <td className="mono">{r.incident_number}</td>
                    <td>
                      <span className={`pill ${r.priority}`}>{r.priority}</span>
                    </td>
                    <td>
                      <span className="mono">{site.code}</span>
                      {site.name && <div className="muted">{site.name}</div>}
                    </td>
                    <td className="mono">{r.region_code}</td>
                    <td>{r.owner}</td>
                    <td>{humanEnum(r.shift_type)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
        {empty && (
          <div className="empty">
            No ledger rows yet. Each new ticket writes one; launch the storm from <Link to="/">Mission control</Link>.
          </div>
        )}
      </div>
    </div>
  );
}
