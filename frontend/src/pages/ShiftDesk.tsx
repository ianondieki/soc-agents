import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Download, Moon, Sun } from "lucide-react";
import { api } from "../api";
import { humanEnum, humanStatus, ownerName, priorityTitle, regionName } from "../lib/agents";
import { detailOf } from "../lib/apiError";
import { IconAlert, IconDot, IconPause } from "../lib/icons";
import { useNarrow } from "../lib/layout";
import { fmtHM, parseInstant } from "../lib/time";
import { useMinute } from "../lib/useMinute";
import "./ShiftDesk.css";
import { DEFAULT_HOURS, shiftAt, shiftWindow } from "../lib/shift";

/** The ledger stores "SFC-RFT-HUB-NKR Nakuru Rift HUB": the site code, a space, the site name. */
function splitSite(site: unknown): { code: string; name: string } {
  const s = String(site ?? "").trim();
  const cut = s.indexOf(" ");
  return cut < 0 ? { code: s, name: "" } : { code: s.slice(0, cut), name: s.slice(cut + 1) };
}

/** "2 h 5 min", "45 min". */
function span(ms: number): string {
  const m = Math.max(0, Math.round(ms / 60000));
  if (m < 60) return `${m} min`;
  return m % 60 ? `${Math.floor(m / 60)} h ${m % 60} min` : `${Math.floor(m / 60)} h`;
}

const DAY_FMT = (() => {
  try {
    return new Intl.DateTimeFormat("en-GB", { weekday: "short", day: "numeric", month: "short", timeZone: "Africa/Nairobi" });
  } catch {
    return null;
  }
})();
const dayOf = (d: Date) => (DAY_FMT ? DAY_FMT.format(d) : d.toISOString().slice(0, 10));

/** The ledger link for a row: the ticket found on the Incident board (the ledger has no ticket id). */
const boardLink = (number: unknown) => `/incidents?state=all&q=${encodeURIComponent(String(number ?? ""))}`;

export default function ShiftDesk({ tick, profile, metrics }: { tick: number; profile?: any; metrics?: any }) {
  // null until the first answer, so loading never reads as an empty ledger.
  const [ledger, setLedger] = useState<any[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [retry, setRetry] = useState(0);
  const [handover, setHandover] = useState<any>(null);
  const [handoverBusy, setHandoverBusy] = useState(false);
  const [handoverError, setHandoverError] = useState("");
  // Below 1100 px the handover comes before the ledger, so a prepared one is seen at once.
  const stacked = useNarrow("(max-width: 1099px)");
  const now = useMinute();

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
      setHandoverError(`Couldn't prepare the handover: ${detailOf(e)}`);
    } finally {
      setHandoverBusy(false);
    }
  };

  // The shift: read from the clock and the operator's hours (the backend's rule), so a desk left
  // open across a handover moves on by itself; never the name cached when the profile loaded.
  const shift = shiftAt(profile, now);
  const hours = profile?.shift_hours?.[shift] || DEFAULT_HOURS[shift];
  const nextShift = shift === "day" ? "night" : "day";
  const nextHours = profile?.shift_hours?.[nextShift] || DEFAULT_HOURS[nextShift];
  const win = shiftWindow(hours, now);
  const total = win.end.getTime() - win.start.getTime();
  const elapsed = Math.min(Math.max(now.getTime() - win.start.getTime(), 0), total);
  const pct = total > 0 ? Math.round((elapsed / total) * 100) : 0;
  const left = win.end.getTime() - now.getTime();

  // The ledger in shifts: one group per date and shift, newest first, as the API orders it.
  const groups = useMemo(() => {
    const out: Array<{ key: string; label: string; rows: any[] }> = [];
    for (const r of ledger || []) {
      const at = parseInstant(r.row_written_at);
      const word = String(r.shift_type || "").toUpperCase() === "NIGHT" ? "Night" : "Day";
      const label = `${word} shift, ${at ? dayOf(at) : "undated"}`;
      const key = `${at ? dayOf(at) : ""}|${r.shift_type}`;
      const last = out[out.length - 1];
      if (last && last.key === key) last.rows.push(r);
      else out.push({ key, label, rows: [r] });
    }
    return out;
  }, [ledger]);
  const thisShift = (ledger || []).filter((r) => {
    const at = parseInstant(r.row_written_at);
    return at && at.getTime() >= win.start.getTime() && at.getTime() < win.end.getTime();
  }).length;
  const p12 = metrics ? (metrics.by_priority?.P1 ?? 0) + (metrics.by_priority?.P2 ?? 0) : null;

  // The workbook of this shift's ledger: `safaricom:2026-10-05:DAY` → `2026-10-05_DAY`.
  const sid = String(profile?.shift_id || "").split(":");
  const xlsx = sid.length === 3 ? `/api/v1/shifts/ledger/${sid[1]}_${sid[2]}.xlsx` : null;

  const empty = ledger !== null && ledger.length === 0;

  const ledgerPanel = (
    <section className="panel sd-ledger" aria-labelledby="sd-ledger-title">
      <div className="panel-head">
        <div className="head-row">
          <h2 id="sd-ledger-title" className="panel-title">
            Shift ledger
          </h2>
          <span>Every ticket the agents logged, newest first. Times in EAT.</span>
        </div>
      </div>
      {failed && ledger === null ? (
        <div className="empty" role="alert">
          Couldn't load the shift ledger.{" "}
          <button className="btn sm" onClick={() => setRetry((n) => n + 1)}>
            Retry
          </button>
        </div>
      ) : empty ? (
        <div className="empty">
          No ledger rows yet. Each new ticket writes one; launch the storm from <Link to="/mission">Mission control</Link>.
        </div>
      ) : ledger === null ? (
        <div className="skeleton-rows" aria-busy="true">
          {Array.from({ length: 6 }, (_, i) => (
            <span key={i} className="skeleton" />
          ))}
        </div>
      ) : (
        groups.map((g) => (
          <div key={g.key} className="sd-group">
            <h3 className="sd-group-title">
              {g.label} <span>{g.rows.length}</span>
            </h3>
            <ol className="sd-timeline">
              {g.rows.map((r, i) => {
                const site = splitSite(r.site);
                const at = parseInstant(r.row_written_at);
                return (
                  <li key={`${r.incident_number}-${i}`} className={`sd-entry ${r.priority}`}>
                    <time className="sd-time" dateTime={at ? at.toISOString() : undefined}>
                      {at ? fmtHM(at) : "—"}
                    </time>
                    <span className="sd-dot" aria-hidden="true" />
                    <div className="sd-body">
                      <div className="sd-line">
                        <span className={`pill ${r.priority}`} title={priorityTitle(r.priority)}>
                          {r.priority}
                        </span>
                        <Link className="sd-site" to={boardLink(r.incident_number)}>
                          {site.name || site.code || r.incident_number}
                        </Link>
                        {r.status && <span className="sd-status">{humanStatus(r.status)}</span>}
                      </div>
                      <div className="sd-meta">
                        <span className="mono sd-num">{r.incident_number}</span>
                        {site.code && site.name && <span className="mono">{site.code}</span>}
                        <span title={r.region_code}>{regionName(r.region_code, profile)}</span>
                        <span>{ownerName(r.owner)}</span>
                        {!!r.mpesa_risk && <span className="sd-flag">M‑PESA at risk</span>}
                      </div>
                    </div>
                  </li>
                );
              })}
            </ol>
          </div>
        ))
      )}
    </section>
  );

  const handoverPanel = (
    <section className="panel sd-handover" aria-labelledby="sd-handover-title">
      <h2 id="sd-handover-title" className="panel-title">
        Handover to the {nextShift} shift
      </h2>
      {handover ? (
        <Handover h={handover} profile={profile} />
      ) : (
        <>
          <ol className="sd-steps">
            <li>The agents write the watchlist and the handover email from what is open now.</li>
            <li>It waits in Approvals for the shift supervisor; nothing is sent before that.</li>
            <li>Once approved it goes to the {nextShift} shift's list.</li>
          </ol>
          <p className="sd-due">
            Due at <span className="mono">{hours.end}</span> EAT, in {span(left)}.
          </p>
        </>
      )}
      {handoverError && (
        <p className="state danger sd-error" role="alert">
          <IconAlert /> {handoverError}
        </p>
      )}
      <div className="sd-actions">
        <button className="btn primary" disabled={handoverBusy} aria-busy={handoverBusy || undefined} onClick={generateHandover}>
          {handoverBusy ? "Preparing the handover…" : handover ? "Prepare it again" : "Prepare the handover"}
        </button>
      </div>
    </section>
  );

  return (
    <div className="stack sd">
      <div className="page-head">
        <div>
          <h1>Shift desk</h1>
          <p className="lead">This shift at a glance, the ledger of every ticket, and the handover to the next shift.</p>
        </div>
        {xlsx && (
          <div className="page-actions">
            <a className="btn" href={xlsx} download>
              <Download size={16} strokeWidth={1.75} aria-hidden="true" />
              Download the ledger
            </a>
          </div>
        )}
      </div>

      <section className="panel sd-shift" aria-label="This shift">
        <div className="sd-shift-main">
          <span className={`sd-shift-icon ${shift}`} aria-hidden="true">
            {shift === "night" ? <Moon size={22} strokeWidth={1.75} /> : <Sun size={22} strokeWidth={1.75} />}
          </span>
          <div className="sd-shift-text">
            <h2 className="sd-shift-name">{shift === "night" ? "Night shift" : "Day shift"}</h2>
            <p className="sd-shift-hours">
              <span className="mono">{hours.start}</span> to <span className="mono">{hours.end}</span> EAT
            </p>
          </div>
          <div className="sd-progress">
            <div
              className="sd-bar"
              role="progressbar"
              aria-label="How much of the shift has gone"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={pct}
              aria-valuetext={`${pct}% of the shift gone, ${span(left)} left`}
            >
              <span className="sd-bar-fill" style={{ transform: `scaleX(${pct / 100})` }} />
            </div>
            <p className="sd-left">
              <strong>{span(left)} left</strong>
              <span>
                then the {nextShift} shift, from <span className="mono">{nextHours.start}</span>
              </span>
            </p>
          </div>
        </div>
        <dl className="sd-figures">
          <div>
            <dt>Logged this shift</dt>
            <dd>{ledger === null ? "—" : thisShift}</dd>
          </div>
          <div>
            <dt>Open now</dt>
            <dd>{metrics?.open_total ?? "—"}</dd>
          </div>
          <div className={p12 ? "p2" : undefined}>
            <dt>P1 and P2 open</dt>
            <dd>{p12 ?? "—"}</dd>
          </div>
          <div className={metrics?.hitl_pending ? "hitl" : undefined}>
            <dt>Approvals waiting</dt>
            <dd>{metrics ? metrics.hitl_pending ?? 0 : "—"}</dd>
          </div>
        </dl>
      </section>

      <div className="sd-grid">
        {stacked ? (
          <>
            {handoverPanel}
            {ledgerPanel}
          </>
        ) : (
          <>
            {ledgerPanel}
            {handoverPanel}
          </>
        )}
      </div>
    </div>
  );
}

/**
 * The handover just prepared: where it is (waiting for the shift supervisor, or released), the
 * watchlist it carries, and the email text exactly as it will leave. A mock or queued email is
 * never called "sent".
 */
function Handover({ h, profile }: { h: any; profile?: any }) {
  const rows: any[] = Array.isArray(h?.incidents) ? h.incidents : [];
  const gate = h?.hitl || {};
  const to: string[] = Array.isArray(h?.email?.to) ? h.email.to : [];
  return (
    <div className="sd-prepared">
      {gate.task_id ? (
        <div className="sd-gate">
          <span className="attn hitl">
            <IconPause />
            <span>Waiting for the shift supervisor in Approvals; nothing is sent until it is approved.</span>
          </span>
          <Link className="btn sm" to={`/hitl?task=${encodeURIComponent(String(gate.task_id))}`}>
            Open it in Approvals
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
      <p className="sd-counts">
        <span>
          <strong>{h?.watch_count ?? rows.length}</strong> on the watchlist
        </span>
        <span>
          <strong>{h?.open_total ?? 0}</strong> open
        </span>
      </p>
      {/* The watchlist scrolls when long, so it takes focus: a keyboard can scroll it too. */}
      {rows.length > 0 ? (
        <ul className="sd-watch" tabIndex={0} aria-label="The watchlist">
          {rows.map((r, i) => (
            <li key={r.incident_number || i}>
              <span className={`pill ${r.priority}`} title={priorityTitle(r.priority)}>
                {r.priority}
              </span>
              <div className="sd-watch-main">
                {/* A long number is cut at its start, so its end (the part that differs) stays. */}
                <span className="mono sd-num sd-watch-num" title={String(r.incident_number ?? "")}>
                  <bdi>{r.incident_number}</bdi>
                </span>
                <span className="sd-watch-meta">
                  <span className="mono">{r.site_id}</span>
                  <span title={r.region_code}>{regionName(r.region_code, profile)}</span>
                  <span>{ownerName(r.owner)}</span>
                </span>
              </div>
              <span className="sd-watch-side">
                <span className="sd-status">{humanEnum(r.status)}</span>
                {!!r.mpesa_risk && <span className="sd-flag">M‑PESA</span>}
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted">Nothing open to hand over.</p>
      )}
      <details className="sd-email">
        <summary>Email text, exactly as it will leave</summary>
        <div className="pre">{h?.body}</div>
      </details>
    </div>
  );
}
