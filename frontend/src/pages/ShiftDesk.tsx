import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { humanEnum, priorityTitle } from "../lib/agents";
import { detailOf } from "../lib/apiError";
import { IconAlert, IconDot, IconPause } from "../lib/icons";
import { fmtDateTime } from "../lib/time";
import { ONE_COL, useNarrow } from "../lib/layout";

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
      .catch(() => {
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
      setHandoverError(`Couldn't generate the handover: ${detailOf(e)}`);
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
          <p className="lead">The day and night ledger; the handover goes to the shift supervisor as an approval card.</p>
        </div>
        <div className="page-actions">
          {handoverError && (
            <span className="state danger" role="alert">
              <IconAlert /> {handoverError}
            </span>
          )}
          <button className="btn primary" disabled={handoverBusy} aria-busy={handoverBusy || undefined} onClick={generateHandover}>
            {handoverBusy ? "Generating the handover…" : "Generate handover and raise the approval"}
          </button>
        </div>
      </div>
      {handover && <Handover h={handover} />}
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
                      <span className={`pill ${r.priority}`} title={priorityTitle(r.priority)}>{r.priority}</span>
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
                <th>Ticket</th>
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
                      <span className={`pill ${r.priority}`} title={priorityTitle(r.priority)}>{r.priority}</span>
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

/**
 * The handover just generated: where it is (waiting for the shift supervisor, or released), the
 * watchlist it carries as a table, and the email text exactly as it will leave. A mock or
 * queued email is never called "sent".
 */
function Handover({ h }: { h: any }) {
  const rows: any[] = Array.isArray(h?.incidents) ? h.incidents : [];
  const gate = h?.hitl || {};
  const to: string[] = Array.isArray(h?.email?.to) ? h.email.to : [];
  return (
    <div className="panel stack">
      <div className="panel-head">
        <h2 className="panel-title">Handover for the {humanEnum(h?.shift) || "current"} shift</h2>
        <div className="facts">
          <span>
            <span className="mono">{h?.watch_count ?? rows.length}</span> on the watchlist
          </span>
          <span>
            <span className="mono">{h?.open_total ?? 0}</span> open
          </span>
        </div>
      </div>
      {gate.task_id ? (
        <div className="note-form-actions">
          <span className="attn hitl">
            <IconPause />
            <span>Waiting for the shift supervisor in Approvals; nothing is sent until it is approved.</span>
          </span>
          <Link className="btn sm" to="/hitl">
            Open Approvals
          </Link>
        </div>
      ) : gate.blocked_reason ? (
        <p>
          <span className="attn warn">
            <IconDot />
            <span>Not queued: {gate.blocked_reason}</span>
          </span>
        </p>
      ) : (
        <p className="muted">Released for sending{to.length ? ` to ${to.join(", ")}` : ""}.</p>
      )}
      {rows.length > 0 ? (
        <table>
          <thead>
            <tr>
              <th>Ticket</th>
              <th>Priority</th>
              <th>Site</th>
              <th>Region</th>
              <th>Owner</th>
              <th>Status</th>
              <th>M‑PESA</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={r.incident_number || i}>
                <td className="mono">{r.incident_number}</td>
                <td>
                  <span className={`pill ${r.priority}`} title={priorityTitle(r.priority)}>{r.priority}</span>
                </td>
                <td className="mono">{r.site_id}</td>
                <td className="mono">{r.region_code}</td>
                <td>{r.owner}</td>
                <td>{humanEnum(r.status)}</td>
                <td>
                  {r.mpesa_risk ? (
                    <span className="attn danger">
                      <IconDot /> at risk
                    </span>
                  ) : (
                    <span className="muted">—</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="muted">Nothing open to hand over.</p>
      )}
      <details>
        <summary>Email text, exactly as it will leave</summary>
        <div className="pre">{h?.body}</div>
      </details>
    </div>
  );
}
