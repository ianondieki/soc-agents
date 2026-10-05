import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { humanStatus, priorityTitle, regionName } from "../../lib/agents";
import { IconDot } from "../../lib/icons";
import {
  SURGE_STATUS_WORD,
  errorDetail,
  fmtAgeMin,
  fmtClock,
  isLaneOff,
  isNetworkError,
  noticeWords,
  restoreSourceWord,
  supportApi,
  surgeOriginWords,
  type Loop,
  type OutageRow,
  type Surge,
} from "../../lib/support";
import { useRealtimeState } from "../../realtime/RealtimeContext";

/**
 * The Support desk's Outages tab (docs/CLOSE_THE_LOOP.md §3, §4): is the desk keeping its
 * promise to tell customers when an outage is fixed, and are customers telling the NOC about
 * outages it has not seen? Three regions, one job each:
 *
 *  1. the loop in five figures (waiting to hear back; told after restore, with the median and
 *     p90; still-down reports; repeat contacts per outage; outages first spotted by customers),
 *     coloured only where a figure means something;
 *  2. the outages that have customers, newest first: the ticket, the places customers named, the
 *     restore, the customers (told, waiting, still down, repeats) and the customer notice in words;
 *  3. the possible outages raised from complaints, the open ones first, with a "Try again" when
 *     opening the ticket failed.
 *
 * Refresh: `tick` is the desk's support revision (support.customers_told, still_down, surge…);
 * the hitl and incidents revisions also refetch, because a notice is decided on Approvals and a
 * restore happens on the incident page.
 */

type Load = "loading" | "ok" | "missing" | "error";

const cap = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s);
const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

function loadText(e: unknown): string {
  if (isNetworkError(e)) return "The API is unreachable.";
  return errorDetail(e, "The request failed.");
}

function minutes(v: number | null | undefined): string {
  return typeof v === "number" && Number.isFinite(v) ? fmtAgeMin(Math.round(v)) : "";
}

export default function Outages({ tick, profile }: { tick: number; profile?: any }) {
  const rt = useRealtimeState();
  const hitlRev = rt?.revisions.hitl ?? 0;
  const incidentsRev = rt?.revisions.incidents ?? 0;
  const [loop, setLoop] = useState<Loop | null>(null);
  const [loopState, setLoopState] = useState<Load>("loading");
  const [rows, setRows] = useState<OutageRow[]>([]);
  const [rowsState, setRowsState] = useState<Load>("loading");
  const [rowsError, setRowsError] = useState("");
  const [surges, setSurges] = useState<Surge[]>([]);
  const [surgesState, setSurgesState] = useState<Load>("loading");
  const [surgesError, setSurgesError] = useState("");
  const [announce, setAnnounce] = useState("");
  const asked = useRef(0);

  const load = useCallback(() => {
    const mine = ++asked.current;
    Promise.allSettled([supportApi.loop(0), supportApi.outages(), supportApi.surges()]).then(([l, o, s]) => {
      if (mine !== asked.current) return;
      if (l.status === "fulfilled") {
        setLoop(l.value);
        setLoopState("ok");
      } else setLoopState((x) => (x === "ok" ? x : isLaneOff(l.reason) ? "missing" : "error"));
      if (o.status === "fulfilled") {
        setRows(Array.isArray(o.value) ? o.value.filter((r) => r && typeof r === "object") : []);
        setRowsState("ok");
        setRowsError("");
      } else {
        setRowsError(loadText(o.reason));
        setRowsState((x) => (x === "ok" ? x : isLaneOff(o.reason) ? "missing" : "error"));
      }
      if (s.status === "fulfilled") {
        setSurges(Array.isArray(s.value) ? s.value.filter((r) => r && typeof r === "object") : []);
        setSurgesState("ok");
        setSurgesError("");
      } else {
        setSurgesError(loadText(s.reason));
        setSurgesState((x) => (x === "ok" ? x : isLaneOff(s.reason) ? "missing" : "error"));
      }
    });
  }, []);

  // A tick later, so React's development double mount asks nothing twice.
  useEffect(() => {
    const t = window.setTimeout(load, 0);
    return () => window.clearTimeout(t);
  }, [load, tick, hitlRev, incidentsRev]);

  // Open first, then newest first as the API sends them.
  const orderedSurges = useMemo(() => {
    const open = surges.filter((s) => s.status === "open");
    return [...open, ...surges.filter((s) => s.status !== "open")];
  }, [surges]);

  if (loopState === "missing" && rowsState === "missing" && surgesState === "missing") {
    return (
      <div className="panel">
        <div className="empty">
          This API has no outage view yet. It arrives with the close-the-loop lane: customers told when service is back, and complaints that point to an
          outage nobody has ticketed.
        </div>
      </div>
    );
  }

  return (
    <div className="ol">
      <span className="sr-only" role="status">
        {announce}
      </span>
      <Figures loop={loop} state={loopState} />
      <OutageList rows={rows} state={rowsState} error={rowsError} onRetry={load} />
      <SurgeList surges={orderedSurges} state={surgesState} error={surgesError} onRetry={load} onRetried={load} onAnnounce={setAnnounce} profile={profile} />
    </div>
  );
}

/* ------------------------------------------------------------------------------- figures */

function Figures({ loop, state }: { loop: Loop | null; state: Load }) {
  if (!loop) {
    return (
      <div className="kpis ol-kpis" aria-label="The loop" aria-busy={state === "loading" || undefined}>
        {["Waiting to hear back", "Told after restore", "Still-down reports", "Repeat contacts per outage", "Spotted by customers"].map((label) => (
          <div key={label} className="kpi">
            <div className="label">{label}</div>
            <div className="value zero">—</div>
            <div className="sd-kpi-sub">{state === "loading" ? "loading" : "not available"}</div>
          </div>
        ))}
      </div>
    );
  }
  const median = minutes(loop.told_median_minutes);
  const p90 = minutes(loop.told_p90_minutes);
  const perOutage = typeof loop.repeat_contacts_per_outage === "number" ? loop.repeat_contacts_per_outage : null;
  const openSurges = loop.surges?.open ?? 0;
  return (
    <div className="kpis ol-kpis" aria-label="The loop">
      <div className="kpi">
        <div className="label">Waiting to hear back</div>
        <div className={"value" + (loop.waiting_to_hear ? "" : " zero")}>{loop.waiting_to_hear}</div>
        <div className="sd-kpi-sub">
          {loop.notices_waiting > 0 ? (
            <Link to="/hitl" className="ol-kpi-link hitl">
              {plural(loop.recipients_waiting, "customer")} in {plural(loop.notices_waiting, "notice")} waiting for approval
            </Link>
          ) : (
            "on outages not yet restored"
          )}
        </div>
      </div>
      <div className="kpi">
        <div className="label">Told after restore</div>
        <div className={"value" + (loop.told ? "" : " zero")}>{loop.told}</div>
        <div className="sd-kpi-sub">
          {!median
            ? "nobody told yet"
            : (loop.told_p90_minutes ?? loop.told_median_minutes ?? 0) < 1
              ? "all within a minute of the restore"
              : `median ${median}${p90 ? `, p90 ${p90}` : ""} after the restore`}
        </div>
      </div>
      <div className="kpi">
        <div className="label">Still-down reports</div>
        <div className={"value" + (loop.still_down_reports ? " warn" : " zero")}>{loop.still_down_reports}</div>
        <div className="sd-kpi-sub">customers who said it was not fixed</div>
      </div>
      <div className="kpi">
        <div className="label">Repeat contacts per outage</div>
        <div className={"value" + (perOutage ? "" : " zero")}>{perOutage == null ? "—" : perOutage.toFixed(1)}</div>
        <div className="sd-kpi-sub">
          {loop.outages_with_complaints
            ? `${plural(loop.repeat_contacts, "repeat")} over ${plural(loop.outages_with_complaints, "outage")}`
            : "no outage with complaints yet"}
        </div>
      </div>
      <div className="kpi">
        <div className="label">Spotted by customers</div>
        <div className={"value" + (loop.spotted_by_customers ? "" : " zero")}>{loop.spotted_by_customers}</div>
        <div className="sd-kpi-sub">
          {openSurges > 0 ? (
            <Link to="/hitl" className="ol-kpi-link hitl">
              {plural(openSurges, "possible outage")} waiting for a decision
            </Link>
          ) : (
            "outages opened from complaints"
          )}
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------------------- outages */

function ListSkeleton() {
  return (
    <div className="skeleton-rows" aria-hidden="true">
      {["68%", "54%", "72%"].map((w, i) => (
        <span key={i} className="skeleton" style={{ width: w }} />
      ))}
    </div>
  );
}

function OutageList({ rows, state, error, onRetry }: { rows: OutageRow[]; state: Load; error: string; onRetry: () => void }) {
  const headId = useId();
  return (
    <section className="panel ol-panel" aria-labelledby={headId} aria-busy={(state === "loading" && !rows.length) || undefined}>
      <div className="panel-head">
        <h2 id={headId} className="panel-title">
          Outages with customers
        </h2>
        <span className="muted">Newest first. Times in EAT.</span>
      </div>
      {state === "error" && rows.length > 0 && (
        <div className="sd-stale ol-stale" role="alert">
          <span>Couldn't refresh; showing the last list received. {error}</span>
          <button type="button" className="btn sm" onClick={onRetry}>
            Retry
          </button>
        </div>
      )}
      {state === "loading" && !rows.length ? (
        <ListSkeleton />
      ) : state === "error" && !rows.length ? (
        <div className="empty" role="alert">
          Couldn't load the outages. {error}
          <button type="button" className="btn sm" onClick={onRetry}>
            Retry
          </button>
        </div>
      ) : state === "missing" ? (
        <div className="empty">This API has no outage list yet.</div>
      ) : rows.length === 0 ? (
        <div className="empty">
          No outage has complaints linked to it yet. When a customer's complaint links to an open ticket, the outage appears here with who is waiting to
          hear back.
        </div>
      ) : (
        <>
          <div className="ol-cols" aria-hidden="true">
            <span>Outage</span>
            <span>Ticket</span>
            <span>Customers</span>
            <span>Customer notice</span>
            <span />
          </div>
          <ul className="ol-rows">
            {rows.map((r) => (
              <OutageItem key={r.incident_id} r={r} />
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

function OutageItem({ r }: { r: OutageRow }) {
  const notice = noticeWords(r.notice);
  const places = Array.isArray(r.places) ? r.places.filter(Boolean) : [];
  const priority = /^P[1-4]$/.test(String(r.priority)) ? r.priority : "";
  const restoredAt = r.restored_at ? fmtClock(r.restored_at) : "";
  const source = restoreSourceWord(r.restore_source);
  const inferred = String(r.restore_source || "").toUpperCase() === "VENDOR_NOTE_INFERRED";
  const parts: { text: string; tone?: string }[] = [
    { text: `${r.told} told` },
    { text: `${r.waiting} waiting` },
  ];
  if (r.still_down > 0) parts.push({ text: `${r.still_down} still down`, tone: "warn" });
  if (r.repeat_contacts > 0) parts.push({ text: plural(r.repeat_contacts, "repeat") });
  return (
    <li className="ol-row">
      <div className="ol-what">
        <div className="ol-what-head">
          {priority && (
            <span className={`pill ${priority}`} title={priorityTitle(priority)}>
              {priority}
            </span>
          )}
          <Link to={`/incidents/${encodeURIComponent(r.incident_id)}`} className="mono ol-inc">
            {r.incident_number}
          </Link>
          {r.from_customer_reports && <span className="chip">from customer reports</span>}
        </div>
        {r.title && <div className="ol-title">{r.title}</div>}
        {places.length > 0 && <div className="ol-sub">Customers named {places.join(", ")}</div>}
      </div>

      <div className="ol-cell">
        <span className="ol-k">Ticket</span>
        {restoredAt ? (
          <span className="ol-v">
            Restored <span className="mono">{restoredAt}</span>
          </span>
        ) : (
          <span className="ol-v">{cap(humanStatus(r.status) || "open")}, not restored</span>
        )}
        {restoredAt && source && <span className={"ol-sub" + (inferred ? " warn" : "")}>{source}</span>}
      </div>

      <div className="ol-cell">
        <span className="ol-k">Customers</span>
        <span className="ol-v">{plural(r.customers, "customer")}</span>
        <span className="ol-sub">
          {parts.map((p, i) => (
            <span key={p.text} className={p.tone ? `ol-part ${p.tone}` : "ol-part"}>
              {p.text}
              {i < parts.length - 1 ? ", " : ""}
            </span>
          ))}
        </span>
      </div>

      <div className="ol-cell">
        <span className="ol-k">Customer notice</span>
        {notice.to ? (
          <Link to={notice.to} className={"ol-v ol-notice" + (notice.tone ? ` ${notice.tone}` : "")}>
            {notice.text}
          </Link>
        ) : (
          <span className={"ol-v ol-notice" + (notice.tone ? ` ${notice.tone}` : "")}>{notice.text}</span>
        )}
        {notice.sub && <span className="ol-sub">{notice.sub}</span>}
      </div>

      <div className="ol-go">
        <Link to={`/support?incident=${encodeURIComponent(r.incident_id)}`} className="btn sm">
          <span>
            Complaints<span className="sr-only"> on {r.incident_number}</span>
          </span>
        </Link>
      </div>
    </li>
  );
}

/* ------------------------------------------------------------------------------- surges */

interface SurgeListProps {
  surges: Surge[];
  state: Load;
  error: string;
  onRetry: () => void;
  onRetried: () => void;
  onAnnounce: (s: string) => void;
  profile?: any;
}

function SurgeList({ surges, state, error, onRetry, onRetried, onAnnounce, profile }: SurgeListProps) {
  const headId = useId();
  return (
    <section className="panel ol-panel" aria-labelledby={headId} aria-busy={(state === "loading" && !surges.length) || undefined}>
      <div className="panel-head">
        <h2 id={headId} className="panel-title">
          Possible outages
        </h2>
        <span className="muted">Places customers report with no ticket open. Open ones first.</span>
      </div>
      {state === "error" && surges.length > 0 && (
        <div className="sd-stale ol-stale" role="alert">
          <span>Couldn't refresh; showing the last list received. {error}</span>
          <button type="button" className="btn sm" onClick={onRetry}>
            Retry
          </button>
        </div>
      )}
      {state === "loading" && !surges.length ? (
        <ListSkeleton />
      ) : state === "error" && !surges.length ? (
        <div className="empty" role="alert">
          Couldn't load the possible outages. {error}
          <button type="button" className="btn sm" onClick={onRetry}>
            Retry
          </button>
        </div>
      ) : state === "missing" ? (
        <div className="empty">This API has no possible-outage list yet.</div>
      ) : surges.length === 0 ? (
        <div className="empty">
          No possible outages. When several numbers complain about the same place in a short time and no ticket is open there, one appears here and a
          supervisor decides on <Link to="/hitl">Approvals</Link>.
        </div>
      ) : (
        <ul className="ol-rows">
          {surges.map((s) => (
            <SurgeItem key={s.id} s={s} onRetried={onRetried} onAnnounce={onAnnounce} region={s.region_code ? regionName(s.region_code, profile) : ""} />
          ))}
        </ul>
      )}
    </section>
  );
}

function retryProblem(e: unknown): string {
  if (isNetworkError(e)) return "The API is unreachable; nothing was opened. Try again.";
  return `It failed again: ${errorDetail(e, "the API refused it")}.`;
}

function SurgeItem({ s, onRetried, onAnnounce, region }: { s: Surge; onRetried: () => void; onAnnounce: (t: string) => void; region: string }) {
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const statusRef = useRef<HTMLDivElement>(null);
  const first = fmtClock(s.first_at);
  const last = fmtClock(s.last_at);
  const span = first && last && first !== last ? `${first} to ${last}` : first || last;
  const refs = Array.isArray(s.complaint_refs) ? s.complaint_refs.filter(Boolean) : [];
  const decidedAt = fmtClock(s.decided_at);
  const origin = surgeOriginWords(s.origin, s.parent_incident_number);
  const failed = s.status === "confirmed" && !!s.error;

  const retry = async () => {
    if (busy) return;
    setBusy(true);
    setProblem("");
    try {
      // 200 with the surge as it ended: `error` is set again when the ingest failed again.
      const after = (await supportApi.retrySurge(s.id)) as Partial<Surge> | null;
      if (after && typeof after.error === "string" && after.error) {
        setProblem(`It failed again: ${after.error}. The complaints are not linked yet and nobody was told.`);
      } else {
        onAnnounce(`The ticket for ${s.place} is open${after?.incident_number ? `: ${after.incident_number}` : ""}.`);
        window.requestAnimationFrame(() => statusRef.current?.focus({ preventScroll: true }));
      }
      onRetried();
    } catch (e) {
      setProblem(retryProblem(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <li className="ol-row ol-surge">
      <div className="ol-what">
        <div className="ol-what-head">
          <span className="ol-place">{s.place}</span>
          {/* The region's name from the operator profile; its code when the profile has none. */}
          {region && <span className={"ol-region" + (region === s.region_code ? " mono" : "")}>{region}</span>}
        </div>
        <div className="ol-title">
          {plural(s.complaints, "complaint")} from {plural(s.numbers, "number")}
          {span ? (
            <>
              , <span className="mono">{span}</span>
            </>
          ) : null}
        </div>
        <div className="ol-sub">{cap(origin)}</div>
        {refs.length > 0 && (
          <details className="ol-refs">
            <summary>{refs.length === 1 ? "The complaint" : `The ${refs.length} complaints`}</summary>
            <span className="mono ol-ref-list">{refs.join(", ")}</span>
          </details>
        )}
      </div>

      <div className="ol-cell ol-surge-state" ref={statusRef} tabIndex={-1}>
        <span className="ol-k">Status</span>
        {s.status === "open" ? (
          <Link to="/hitl" className="ol-v ol-notice hitl">
            {SURGE_STATUS_WORD.open}
          </Link>
        ) : s.status === "confirmed" ? (
          <>
            <span className={"ol-v ol-notice" + (failed ? "" : " ok")}>
              {failed ? "Confirmed, but the ticket did not open" : s.incident_number ? "Confirmed, ticket opened" : SURGE_STATUS_WORD.confirmed}
            </span>
            {s.incident_id && s.incident_number && (
              <Link to={`/incidents/${encodeURIComponent(s.incident_id)}`} className="mono ol-inc">
                {s.incident_number}
              </Link>
            )}
            {(s.decided_by || decidedAt) && (
              <span className="ol-sub">
                {s.decided_by ? `by ${s.decided_by}` : ""}
                {s.decided_by && decidedAt ? ", " : ""}
                {decidedAt && <span className="mono">{decidedAt}</span>}
              </span>
            )}
          </>
        ) : s.status === "dismissed" ? (
          <>
            <span className="ol-v">
              {SURGE_STATUS_WORD.dismissed}
              {s.decided_by ? ` by ${s.decided_by}` : ""}
            </span>
            {s.reason && <span className="ol-sub">“{s.reason}”</span>}
          </>
        ) : (
          <span className="ol-v">{cap(String(s.status || "").replace(/_/g, " ")) || "Unknown"}</span>
        )}
      </div>

      <div className="ol-go">
        {failed && (
          <button type="button" className="btn sm" onClick={retry} aria-disabled={busy || undefined} aria-busy={busy || undefined}>
            {busy ? "Trying again…" : "Try again"}
          </button>
        )}
      </div>

      {failed && (
        <div className="ol-error" role="alert">
          <IconDot />
          <span>
            {problem || (
              <>
                Opening the ticket failed: {s.error}. The complaints are not linked yet and nobody was told.
              </>
            )}
          </span>
        </div>
      )}
    </li>
  );
}
