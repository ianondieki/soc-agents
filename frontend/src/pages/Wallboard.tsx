import { useEffect, useState } from "react";
import { api } from "../api";
import CardBoundary from "../components/CardBoundary";
import RiskStrip from "../components/RiskStrip";
import AgentsStatusTile from "../components/AgentsStatusTile";
import RedactionMissChip from "../components/RedactionMissChip";
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
  const [rows, setRows] = useState<any[]>([]);
  const [red, setRed] = useState<RedCard[]>([]);

  useEffect(() => {
    const load = () =>
      api
        .incidents()
        .then((all) => {
          setRows(all.filter((i) => ["P1", "P2"].includes(i.priority) && !["CLOSED", "CANCELLED"].includes(i.status)));
        })
        // A failed poll leaves the last good rows on the glass rather than
        // blanking the wall.
        .catch(() => undefined);
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
  }, [rev]);

  const redByIncident = new Map<string, RedCard>();
  for (const c of red) if (c.incident_id && !redByIncident.has(c.incident_id)) redByIncident.set(c.incident_id, c);
  const onGrid = new Set(rows.map((i) => String(i.id)));
  const offGrid = red.filter((c) => !c.incident_id || !onGrid.has(c.incident_id));

  return (
    <div className="wallboard">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
        <h1>NOC WALLBOARD · SAFARICOM DEMO</h1>
        <div className="chips">
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
          DECISION WAITING · UNCLAIMED PAST T+30
          {offGrid.map((c) => (
            <div key={c.id} className="muted">
              {c.priority ?? "P?"} · {c.task_type} · {c.incident_number ?? "no incident"}
              {c.unclaimed_minutes != null ? ` · unclaimed ${c.unclaimed_minutes} min` : ""}
              {c.since_eat ? ` · red since ${c.since_eat}` : ""}
            </div>
          ))}
        </div>
      )}
      <div className="wb-grid">
        {rows.length === 0 && <div className="empty">No P1/P2 open — quiet glass.</div>}
        {rows.map((i) => {
          const esc = redByIncident.get(String(i.id));
          return (
            <div key={i.id} className={`wb-card ${i.priority}${esc ? " escalated" : ""}`}>
              <div className="big">
                {i.priority} · {i.incident_number}
              </div>
              <div style={{ fontSize: "1.2rem", marginTop: "0.5rem" }}>{i.site_name}</div>
              <div className="muted" style={{ marginTop: "0.4rem", fontSize: "1rem" }}>
                {i.region_code} · {i.failure_domain} · {i.users_affected?.toLocaleString()} users
              </div>
              <div style={{ marginTop: "0.8rem", fontSize: "1.05rem" }}>Owner: {i.assignee_name}</div>
              <div className="muted">
                {i.status}
                {i.mpesa_risk ? " · M-PESA RISK" : ""}
                {i.requires_hitl ? " · HITL WAITING" : ""}
                {i.tt_category ? ` · ${i.tt_category}` : ""}
              </div>
              {esc && (
                <div className="wb-escalated-line">
                  DECISION WAITING · {esc.task_type} UNCLAIMED
                  {esc.unclaimed_minutes != null ? ` ${esc.unclaimed_minutes} MIN` : ""}
                  {esc.since_eat ? ` · RED SINCE ${esc.since_eat}` : ""}
                </div>
              )}
            </div>
          );
        })}
      </div>
      <p className="muted" style={{ marginTop: "2rem" }}>
        <a href="/">← Back to Mission Control</a>
      </p>
    </div>
  );
}
