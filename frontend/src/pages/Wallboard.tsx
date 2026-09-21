import { useEffect, useState } from "react";
import { api } from "../api";
import CardBoundary from "../components/CardBoundary";
import RiskStrip from "../components/RiskStrip";
import AgentsStatusTile from "../components/AgentsStatusTile";
import RedactionMissChip from "../components/RedactionMissChip";

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
    load();
    // `rev` is the debounced `incidents` slice revision, so a storm refreshes
    // the wall once per burst instead of once per frame. The 5 s poll stays as
    // the fallback for a dropped WS.
    const id = window.setInterval(load, 5000);
    return () => window.clearInterval(id);
  }, [rev]);

  return (
    <div className="wallboard">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
        <h1>NOC WALLBOARD · SAFARICOM DEMO</h1>
        <div className="chips">
          <span className="chip">Open {metrics?.open_total ?? "—"}</span>
          <span className="chip hitl">HITL {metrics?.hitl_pending ?? 0}</span>
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
      <div className="wb-grid">
        {rows.length === 0 && <div className="empty">No P1/P2 open — quiet glass.</div>}
        {rows.map((i) => (
          <div key={i.id} className={`wb-card ${i.priority}`}>
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
          </div>
        ))}
      </div>
      <p className="muted" style={{ marginTop: "2rem" }}>
        <a href="/">← Back to Mission Control</a>
      </p>
    </div>
  );
}
