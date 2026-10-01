import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { AUTO_STORM_KEY } from "../App";
import LiveRunPanel from "../components/LiveRunPanel";
import { fmtTime } from "../lib/time";
// One run-status palette and one error line for both run lists (A-13), so the two pages
// cannot drift apart again.
import { RunError, runChip } from "./Agents";
import { describeEvent, type NocEvent } from "../realtime/renderers";
import { hitlSubject } from "../lib/hitlSubject";

export default function MissionControl({
  metrics,
  events,
  incidentsRev,
  hitlRev,
  runsRev,
  quietMode = false,
  suppressed = 0,
  storming,
  stormProg,
  stormErr,
  onLaunchStorm,
  onOpenGuide,
  onOpen,
  onRefresh,
}: {
  metrics: any;
  events: NocEvent[];
  /** Revision of the incidents slice — see realtime/renderers.ts. */
  incidentsRev: number;
  hitlRev: number;
  runsRev: number;
  quietMode?: boolean;
  suppressed?: number;
  /** The live storm is owned by App so the guided demo can start it from any page. */
  storming: boolean;
  stormProg: string;
  stormErr: string;
  onLaunchStorm: (reason: string) => Promise<void> | void;
  onOpenGuide: () => void;
  onOpen: (id: string) => void;
  onRefresh?: () => void;
}) {
  const navigate = useNavigate();
  const [incidents, setIncidents] = useState<any[]>([]);
  const [hitl, setHitl] = useState<any[]>([]);
  const [runs, setRuns] = useState<any[]>([]);
  const [err, setErr] = useState("");
  const [chase, setChase] = useState("");
  const autoStarted = useRef(false);

  const loadIncidents = () => api.incidents().then(setIncidents).catch((e) => setErr(String(e)));
  const loadHitl = () => api.hitl().then(setHitl).catch(console.error);
  const loadRuns = () => api.runs().then(setRuns).catch(console.error);

  const loadLists = () => {
    loadIncidents();
    loadHitl();
    loadRuns();
  };

  // Defect #26: one effect per slice instead of one effect for every WS frame.
  // An agent.step.* burst bumps none of these, so the three panels below hold
  // still while the ticker streams.
  useEffect(() => {
    loadIncidents();
  }, [incidentsRev]);
  useEffect(() => {
    loadHitl();
  }, [hitlRev]);
  useEffect(() => {
    loadRuns();
  }, [runsRev]);

  const open = incidents.filter((i) => !["CLOSED", "CANCELLED"].includes(i.status));
  const maxRegion = Math.max(1, ...Object.values(metrics?.by_region || { x: 1 }).map(Number));

  // Auto-run storm once when board is empty so first visit always has live demo data
  useEffect(() => {
    if (autoStarted.current) return;
    if (metrics == null) return;
    if ((metrics.open_total ?? 0) > 0) return;
    let skipped = false;
    try {
      skipped = sessionStorage.getItem(AUTO_STORM_KEY) === "1";
    } catch {
      skipped = false;
    }
    // Still auto once per browser session if empty
    if (skipped) return;
    autoStarted.current = true;
    // small delay so WS can connect
    const t = window.setTimeout(() => {
      onLaunchStorm("Auto demo on an empty board");
    }, 900);
    return () => window.clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [metrics?.open_total]);

  const banner = err || stormErr || chase || stormProg;

  return (
    <div>
      <div className="hero">
        <div>
          <h1>Mission Control</h1>
          <p className="lead">
            Live multi-agent NOC for Safaricom-scale ops (~7,000 sites). Every alarm runs the twelve agents below;
            the rail shows the newest one hop by hop. Heavy rain in Rift, Mt Kenya and Nairobi East takes microwave
            hops down and cascades child sites under their HUB majors.
          </p>
        </div>
        <div className="hero-actions">
          <button className="btn storm" disabled={storming} onClick={() => onLaunchStorm("Manual storm launch")}>
            {storming ? "Storm in progress…" : "Launch heavy-rain storm (live)"}
          </button>
          <button className="btn" onClick={onOpenGuide} title="Five steps for presenting the prototype">
            Guided demo
          </button>
          <button
            className="btn"
            disabled={storming}
            onClick={async () => {
              try {
                const r = await api.monitorTick();
                setChase(`Worklog monitor chased ${r.chased} ticket${r.chased === 1 ? "" : "s"} for silence.`);
              } catch (e: any) {
                setChase(`SLA chase failed: ${e?.message || e}`);
              }
              onRefresh?.();
              loadLists();
            }}
          >
            Run SLA chase
          </button>
        </div>
      </div>

      {banner && (
        <div className="storm-banner">
          <div>
            {storming && <span className="live-dot" />}
            <strong>{storming ? "Live scenario executing" : "Scenario status"}</strong>
            <div className="storm-progress">{err || stormErr || chase || stormProg}</div>
          </div>
          {storming && <span className="chip accent">AGENTS EXECUTING</span>}
        </div>
      )}

      <div className="kpis">
        <div className="kpi">
          <div className="label">Open</div>
          <div className="value">{metrics?.open_total ?? "—"}</div>
        </div>
        <div className="kpi">
          <div className="label">P1 critical</div>
          <div className="value" style={{ color: "var(--p1)" }}>
            {metrics?.by_priority?.P1 ?? 0}
          </div>
        </div>
        <div className="kpi">
          <div className="label">P2 major</div>
          <div className="value" style={{ color: "var(--p2)" }}>
            {metrics?.by_priority?.P2 ?? 0}
          </div>
        </div>
        <div className="kpi">
          <div className="label">HITL queue</div>
          <div className="value" style={{ color: "var(--hitl)" }}>
            {metrics?.hitl_pending ?? 0}
          </div>
        </div>
        <div className="kpi">
          <div className="label">SLA risk</div>
          <div className="value" style={{ color: "var(--warn)" }}>
            {metrics?.sla_risk ?? 0}
          </div>
        </div>
        <div className="kpi">
          <div className="label">Problems</div>
          <div className="value">{metrics?.problems_open ?? 0}</div>
        </div>
        <div className="kpi">
          <div className="label">Silent risk</div>
          <div className="value" style={{ color: "var(--warn)" }}>
            {metrics?.silent_at_risk ?? 0}
          </div>
        </div>
      </div>

      <div style={{ marginBottom: "1rem" }}>
        <LiveRunPanel events={events} runsRev={runsRev} onOpen={onOpen} />
      </div>

      <div className="grid-3">
        <div className="panel">
          <div className="panel-head">
            <h3>Live incidents</h3>
            <span className="chip">{open.length} open</span>
          </div>
          <div className="list">
            {open.length === 0 && !storming && (
              <div className="empty">
                No open incidents yet. The demo starts on its own, or click{" "}
                <strong>Launch heavy-rain storm (live)</strong>.
              </div>
            )}
            {open.length === 0 && storming && (
              <div className="empty">
                <span className="live-dot" />
                Agents opening tickets — watch this list fill…
              </div>
            )}
            {open.map((i) => (
              <div key={i.id} className="row flash" onClick={() => onOpen(i.id)}>
                <span className={`pill ${i.priority}`}>{i.priority}</span>
                <div>
                  <div>
                    <strong>{i.incident_number}</strong> · {i.site_id}
                  </div>
                  <div className="muted">
                    {i.region_code} · {i.failure_domain} · {i.responsible_msp || i.assignee_name}
                    {i.child_sites_down ? ` · children ${i.child_sites_down}` : ""}
                    {i.mpesa_risk ? " · M-PESA" : ""}
                  </div>
                </div>
                <span className="muted dim">{i.status}</span>
              </div>
            ))}
          </div>
        </div>

        <div className="panel">
          <div className="panel-head">
            <h3>
              <span className={events.length ? "live-dot" : "live-dot off"} />
              Agent activity (live · EAT)
            </h3>
            <span className="chip accent">
              {quietMode ? `${events.length} critical · ${suppressed} quiet` : `${events.length} events`}
            </span>
          </div>
          <div className="ticker">
            {events.length === 0 && (
              <div className="empty">
                {quietMode
                  ? "Quiet mode — only P1/P2 incidents, HITL prompts and delivery failures appear here."
                  : "Waiting for WebSocket agent stream…"}
                <br />
                You will see INGEST → CORRELATE → ENRICH → SEVERITY → TICKET → ASSIGN …
              </div>
            )}
            {events.map((e, idx) => (
              <div key={`${e.ts}-${idx}-${e.type}`} className="ticker-line">
                <span>{fmtTime(e.ts, "···")}</span>
                <span>
                  <strong>{e.type}</strong>{" "}
                  {e.payload?.incident_number || ""} {e.payload?.node || ""}{" "}
                  {e.payload?.agent ? `· ${e.payload.agent}` : ""}{" "}
                  {e.payload?.status ? `[${e.payload.status}]` : ""}{" "}
                  {e.payload?.rationale
                    ? `— ${String(e.payload.rationale).slice(0, 90)}`
                    : e.payload?.output
                      ? `— ${String(e.payload.output).slice(0, 90)}`
                      : e.payload?.detail
                        ? `— ${String(e.payload.detail).slice(0, 90)}`
                        : ""}
                  {describeEvent(e)}
                </span>
              </div>
            ))}
          </div>
        </div>

        <div className="panel">
          <div className="panel-head">
            <h3>HITL inbox</h3>
            <span className="chip hitl">{hitl.length}</span>
          </div>
          <div className="list">
            {hitl.length === 0 && <div className="empty">No pending human approvals.</div>}
            {hitl.slice(0, 12).map((t) => (
              // Since v8 a maintenance card has no incident: onOpen(null) was /incidents/null.
              // Those rows open the HITL inbox, where the card itself can be decided.
              <div
                key={t.id}
                className="row"
                title={t.incident_id ? undefined : "Not about an incident: opens the HITL inbox"}
                onClick={() => (t.incident_id ? onOpen(t.incident_id) : navigate("/hitl"))}
              >
                <span className={`pill ${t.priority || "P4"}`}>{t.priority}</span>
                <div>
                  <div>
                    <strong>{hitlSubject(t)}</strong>
                  </div>
                  <div className="muted">
                    {t.task_type}
                    {t.claimed_by ? ` · ${t.claimed_by}` : " · unclaimed"}
                  </div>
                </div>
                <span className="muted dim">{t.site_id}</span>
              </div>
            ))}
          </div>
        </div>
      </div>

      <div className="grid-2" style={{ marginTop: "1rem" }}>
        <div className="panel">
          <h3>Open load by region (RFT · MTK · NBI_E storm zones)</h3>
          <div className="region-bars">
            {Object.entries(metrics?.by_region || {}).length === 0 && (
              <div className="empty">Region load appears when incidents are open.</div>
            )}
            {Object.entries(metrics?.by_region || {}).map(([k, v]) => (
              <div key={k} className="region-bar-row">
                <span className="muted">{k}</span>
                <div className="region-bar-track">
                  <div
                    className="region-bar-fill"
                    style={{
                      width: `${(Number(v) / maxRegion) * 100}%`,
                      background:
                        k === "RFT" || k === "MTK" || k === "NBI_E"
                          ? "linear-gradient(90deg, #38bdf8, #a78bfa)"
                          : undefined,
                    }}
                  />
                </div>
                <span className="muted">{String(v)}</span>
              </div>
            ))}
          </div>
        </div>
        <div className="panel">
          <h3>Recent agent runs (EAT)</h3>
          <div className="list" style={{ maxHeight: 220 }}>
            {runs.slice(0, 10).map((r) => (
              <div
                key={r.id}
                className="row"
                style={{ cursor: r.incident_id ? "pointer" : "default" }}
                onClick={() => r.incident_id && onOpen(r.incident_id)}
              >
                {/* Same A-13 fix as the Agent Observatory: FAILED is red with its word, never green. */}
                <span className={runChip(r.status)}>{r.status}</span>
                <div>
                  <div className="muted">
                    {r.graph_name} · {r.trigger}
                  </div>
                  <div className="muted dim">
                    {r.current_node || (r.steps && `${r.steps.length} steps`) || "—"}
                  </div>
                  <RunError status={r.status} summary={r.error_summary} />
                </div>
                <span className="muted dim">{fmtTime(r.started_at)}</span>
              </div>
            ))}
            {runs.length === 0 && <div className="empty">Runs appear as the storm executes.</div>}
          </div>
        </div>
      </div>
    </div>
  );
}
