import { useEffect, useState } from "react";
import { api } from "../api";
import CardBoundary from "../components/CardBoundary";
import RiskStrip from "../components/RiskStrip";
import AgentsStatusTile from "../components/AgentsStatusTile";
import RedactionMissChip from "../components/RedactionMissChip";
import { fmtEAT } from "../lib/time";
import "./Wallboard.escalation.css";

/**
 * §6.5 escalation ladder, T+30 rung: the ladder (services/hitl_escalation.py) marks a P1/P2
 * approval card that has sat PENDING and unclaimed past the Wallboard rung by writing
 * `proposed_payload.escalation.wallboard_red = true` on the task, which the existing
 * `/api/v1/hitl/pending` route already returns — so the glass needs no new route.
 *
 * Red is "still waiting": the card must still be PENDING and unclaimed here too. A claim
 * means someone who can decide it is looking, which is what the ladder exists to bring
 * about, so the red clears the moment a name goes on the card.
 */
type RedCard = {
  id: string;
  incident_id: string | null;
  incident_number: string | null;
  priority: string | null;
  task_type: string;
  unclaimed_minutes: number | null;
  since_eat: string | null;
};

function redCards(tasks: any[]): RedCard[] {
  const out: RedCard[] = [];
  for (const t of tasks) {
    const esc = t?.proposed_payload?.escalation;
    if (!esc || esc.wallboard_red !== true) continue;
    if (t.status !== "PENDING" || t.claimed_by) continue;
    const rung = esc.rungs?.[String(esc.level_minutes ?? "")] ?? null;
    out.push({
      id: String(t.id),
      incident_id: t.incident_id ?? null,
      incident_number: t.incident_number ?? null,
      priority: t.priority ?? t?.proposed_payload?.priority ?? null,
      task_type: String(t.task_type ?? "task"),
      unclaimed_minutes: typeof rung?.unclaimed_minutes === "number" ? rung.unclaimed_minutes : null,
      since_eat: typeof esc.red_since_eat === "string" ? esc.red_since_eat : null,
    });
  }
  return out;
}

/** "APPROVE_BROADCAST" → "APPROVE BROADCAST": the glass keeps its capitals, not the underscores. */
const words = (s: unknown) => String(s ?? "").replace(/_/g, " ");

/** Skeleton bar widths for the cards shown before the first answer. */
const SKELETON_WIDTHS = ["46%", "78%", "62%", "54%"];

export default function Wallboard({
  metrics,
  rev = 0,
  signalsRev = 0,
}: {
  metrics: any;
  rev?: number;
  /** Debounced `signals` slice revision — see realtime/renderers.ts. */
  signalsRev?: number;
}) {
  // null until the first answer: the glass never says "quiet" before it has asked.
  const [rows, setRows] = useState<any[] | null>(null);
  const [red, setRed] = useState<RedCard[]>([]);
  // When the incident poll last answered, and whether the latest one failed: a wall that has
  // stopped updating must say so instead of showing old tiles as if they were live.
  const [lastOk, setLastOk] = useState<Date | null>(null);
  const [stale, setStale] = useState(false);
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    const load = () =>
      api
        .incidents()
        .then((all) => {
          setRows(all.filter((i) => ["P1", "P2"].includes(i.priority) && !["CLOSED", "CANCELLED"].includes(i.status)));
          setLastOk(new Date());
          setStale(false);
        })
        // A failed poll leaves the last good rows on the glass rather than blanking the
        // wall, and raises the "Not updating" chip.
        .catch(() => setStale(true));
    // The ladder's red state rides on the pending inbox. A role the inbox refuses (403), or
    // the ladder being off (no card ever carries the mark), both leave the wall exactly as it
    // was: the last good list stays, and an empty list shows nothing extra.
    const loadRed = () =>
      api
        .hitl()
        .then((tasks) => setRed(redCards(Array.isArray(tasks) ? tasks : [])))
        .catch(() => undefined);
    load();
    loadRed();
    // `rev` is the debounced `incidents` slice revision, so a storm refreshes
    // the wall once per burst instead of once per frame. The 5 s poll stays as
    // the fallback for a dropped WS.
    const id = window.setInterval(() => {
      load();
      loadRed();
    }, 5000);
    return () => window.clearInterval(id);
  }, [rev, retry]);

  const list = rows || [];
  const redByIncident = new Map<string, RedCard>();
  for (const c of red) if (c.incident_id && !redByIncident.has(c.incident_id)) redByIncident.set(c.incident_id, c);
  const onGrid = new Set(list.map((i) => String(i.id)));
  const offGrid = red.filter((c) => !c.incident_id || !onGrid.has(c.incident_id));

  return (
    <div className="wallboard">
      <div className="wb-head">
        <h1>
          NOC WALLBOARD <span className="wb-head-sub">SAFARICOM DEMO</span>
        </h1>
        <div className="chips">
          {/* Old tiles on the glass after a failed poll. Before the first answer the grid says so instead. */}
          {stale && lastOk && (
            <span className="chip" role="status">
              Not updating since {fmtEAT(lastOk)}
            </span>
          )}
          <span className="chip">Open {metrics?.open_total ?? "—"}</span>
          <span className="chip hitl">HITL {metrics?.hitl_pending ?? 0}</span>
          {red.length > 0 && <span className="chip bad">ESCALATED {red.length}</span>}
          <span className="chip">P1 {metrics?.by_priority?.P1 ?? 0}</span>
          <span className="chip">P2 {metrics?.by_priority?.P2 ?? 0}</span>
        </div>
      </div>
      {/* Platform alarms (§4.6, §9.6, §10.4): "AGENTS OFFLINE" / circuit-open and the red
          redaction-miss chip. Above everything else on the glass; each boundary's fallback is
          null, so a broken alarm component can never blank the P1/P2 grid. */}
      <CardBoundary fallback={null}>
        <AgentsStatusTile />
      </CardBoundary>
      <CardBoundary fallback={null}>
        <RedactionMissChip />
      </CardBoundary>
      {/*
        Weather context sits above the incident grid so it stays on screen during
        a storm — the one time it is worth anything — but it is deliberately the
        quietest band on the wall (see components/RiskStrip.tsx). It renders
        nothing at all when WEATHER_ENABLED is off or the read endpoint is
        missing, so the wallboard is unchanged in the default configuration.

        The boundary's fallback is `null`: if a future payload shape somehow
        breaks the strip's render, the strip vanishes and the P1/P2 grid below it
        keeps working. Weather advisory may never be able to blank the wallboard.
      */}
      <CardBoundary fallback={null}>
        <RiskStrip rev={signalsRev} />
      </CardBoundary>
      {/* §6.5 T+30: red cards with no tile on the grid below. Rendered only when there are
          any, so the default glass is byte-for-byte what it was. */}
      {offGrid.length > 0 && (
        <div className="wb-escalation-strip" role="alert">
          <div className="wb-line">
            <span>DECISION WAITING</span>
            <span>UNCLAIMED PAST T+30</span>
          </div>
          {offGrid.map((c) => (
            <div key={c.id} className="wb-line muted">
              <span>{c.priority ?? "P?"}</span>
              <span>{words(c.task_type)}</span>
              <span className="mono">{c.incident_number ?? "no incident"}</span>
              {c.unclaimed_minutes != null && <span>unclaimed {c.unclaimed_minutes} min</span>}
              {c.since_eat && <span>red since {c.since_eat}</span>}
            </div>
          ))}
        </div>
      )}
      {rows === null && !stale && (
        <span className="sr-only" role="status">
          Loading the P1 and P2 incidents
        </span>
      )}
      {rows === null && stale && (
        <div className="empty" role="alert">
          Couldn't reach the incident list; the wall retries every 5 seconds.{" "}
          <button className="btn sm" onClick={() => setRetry((n) => n + 1)}>
            Retry
          </button>
        </div>
      )}
      <div className="wb-grid" aria-busy={rows === null || undefined}>
        {rows === null &&
          !stale &&
          Array.from({ length: 4 }, (_, k) => (
            <div key={"sk-" + k} className="wb-card wb-card-skeleton" aria-hidden="true">
              {SKELETON_WIDTHS.map((w, j) => (
                <span key={j} className="skeleton" style={{ width: w }} />
              ))}
            </div>
          ))}
        {rows !== null && rows.length === 0 && <div className="empty">No P1/P2 open — quiet glass.</div>}
        {list.map((i) => {
          const esc = redByIncident.get(String(i.id));
          // The escalated line already says a decision is waiting; the flag would say it twice.
          const decisionFlag = Boolean(i.requires_hitl) && !esc;
          return (
            <div key={i.id} className={`wb-card ${i.priority}${esc ? " escalated" : ""}`}>
              <div className="big wb-line">
                <span>{i.priority}</span>
                <span>{i.incident_number}</span>
              </div>
              <div className="wb-site">{i.site_name}</div>
              <div className="wb-line wb-meta muted">
                <span className="mono">{i.region_code}</span>
                {i.failure_domain && <span>{String(i.failure_domain).toLowerCase()}</span>}
                <span>{i.users_affected?.toLocaleString()} users</span>
              </div>
              <div className="wb-owner">Owner {i.assignee_name}</div>
              <div className="wb-line wb-meta muted">
                <span>{words(i.status)}</span>
                {i.tt_category && <span>{words(i.tt_category)}</span>}
              </div>
              {(i.mpesa_risk || decisionFlag) && (
                <div className="wb-flags">
                  {i.mpesa_risk && <span className="danger">M‑PESA RISK</span>}
                  {decisionFlag && <span className="hitl">DECISION WAITING</span>}
                </div>
              )}
              {esc && (
                <div className="wb-escalated-line wb-line">
                  <span>DECISION WAITING</span>
                  <span>
                    {words(esc.task_type)} UNCLAIMED
                    {esc.unclaimed_minutes != null ? ` ${esc.unclaimed_minutes} MIN` : ""}
                  </span>
                  {esc.since_eat && <span>RED SINCE {esc.since_eat}</span>}
                </div>
              )}
            </div>
          );
        })}
      </div>
      <p className="wb-foot muted">
        <a href="/">Back to Mission control</a>
      </p>
    </div>
  );
}
