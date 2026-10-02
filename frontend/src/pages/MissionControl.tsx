import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api";
import { AUTO_STORM_KEY } from "../lib/demo";
import LiveRunPanel, { RunState } from "../components/LiveRunPanel";
import { humanEnum, humanGraph, humanStatus, nodeLabel } from "../lib/agents";
import { IconDot } from "../lib/icons";
import { labelFor } from "../lib/hitl";
import { fmtTime } from "../lib/time";
// One run-status palette and one error line for both run lists (A-13), so the two pages
// cannot drift apart again.
import { RunError } from "./Agents";
import { describeEvent, type NocEvent } from "../realtime/renderers";
import { hitlSubject } from "../lib/hitlSubject";

/** One figure of the strip. Colour only when the number is worth a look; zero stays quiet. */
function Kpi({ label, value, tone }: { label: string; value: number | undefined; tone?: string }) {
  const n = typeof value === "number" ? value : null;
  return (
    <div className="kpi">
      <div className="label">{label}</div>
      <div className={"value" + (n === 0 ? " zero" : "")} style={tone && n ? { color: tone } : undefined}>
        {n == null ? "—" : n}
      </div>
    </div>
  );
}

type Load = "loading" | "ok" | "error";

/** Skeleton rows while a list loads for the first time: never a spinner. */
function SkeletonRows({ rows = 5 }: { rows?: number }) {
  const widths = ["72%", "58%", "66%", "50%", "62%", "56%"];
  return (
    <div role="status">
      <span className="sr-only">Loading</span>
      <div className="skeleton-rows" aria-hidden="true">
        {Array.from({ length: rows }, (_, i) => (
          <span key={i} className="skeleton" style={{ width: widths[i % widths.length] }} />
        ))}
      </div>
    </div>
  );
}

/** A list that could not load says so in a sentence and offers the retry. */
function ListError({ what, onRetry }: { what: string; onRetry: () => void }) {
  return (
    <div className="empty" role="alert">
      Couldn't load {what}.{" "}
      <button type="button" className="btn sm" onClick={onRetry}>
        Retry
      </button>
    </div>
  );
}

/** The ticker's first words: what happened, as the floor says it. Unknown types are humanised. */
const EVENT_WORDS: Record<string, string> = {
  "incident.created": "Ticket opened",
  "incident.merged": "Folded into a ticket",
  "incident.cascade_child": "Child site folded",
  "incident.closed": "Ticket closed",
  "incident.reassigned": "Reassigned",
  "incident.updated": "Ticket updated",
  "incident.note": "Note added",
  "agent.run.started": "Run started",
  "agent.run.finished": "Run finished",
  "agent.step.started": "Step started",
  "agent.step.completed": "Step done",
  "hitl.created": "Decision needed",
  "hitl.claimed": "Card claimed",
  "hitl.approved": "Approved",
  "hitl.rejected": "Rejected",
  "email.sent": "E-mail sent",
  "email.failed": "E-mail failed",
  "outbox.failed": "Delivery failed",
  "external_signal.updated": "Signal updated",
  "power_notice.new": "Power notice",
  "complaint.surge": "Complaint surge",
  "pir.opened": "Review opened",
  "regulatory.deadline": "Regulatory deadline",
  "scheduler.job_failed": "Job failed",
  "security.redaction_miss": "Redaction miss",
  "monitor.chase": "Vendor chased",
  "demo.rain_storm.complete": "Storm complete",
};

function eventWord(type: string): string {
  if (EVENT_WORDS[type]) return EVENT_WORDS[type];
  const s = String(type || "event").replace(/[._]+/g, " ").trim();
  return s ? s[0].toUpperCase() + s.slice(1) : "Event";
}

/** Statuses the ticker names: only the ones worth a look. */
const ROUTINE_STEP = new Set(["", "SUCCEEDED", "STARTED", "RUNNING", "PENDING"]);

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
  const [incLoad, setIncLoad] = useState<Load>("loading");
  const [hitlLoad, setHitlLoad] = useState<Load>("loading");
  const [runsLoad, setRunsLoad] = useState<Load>("loading");
  const [chase, setChase] = useState<{ text: string; bad: boolean } | null>(null);
  const autoStarted = useRef(false);

  // Flash a row only when it is new on the board. The class used to be on every row, so
  // every refetch re-ran the animation across the whole list — motion that meant nothing.
  const seenIds = useRef<Set<string> | null>(null);
  const [fresh, setFresh] = useState<Set<string>>(() => new Set());
  useEffect(() => {
    if (seenIds.current === null) {
      seenIds.current = new Set(incidents.map((i) => i.id));
      return;
    }
    const seen = seenIds.current;
    const added = incidents.map((i) => i.id).filter((id) => !seen.has(id));
    if (added.length === 0) return;
    for (const id of added) seen.add(id);
    setFresh(new Set(added));
    const t = window.setTimeout(() => setFresh(new Set()), 1700);
    return () => window.clearTimeout(t);
  }, [incidents]);

  const loadIncidents = () =>
    api
      .incidents()
      .then((rows) => {
        setIncidents(rows);
        setIncLoad("ok");
      })
      .catch(() => setIncLoad("error"));
  const loadHitl = () =>
    api
      .hitl()
      .then((rows) => {
        setHitl(rows);
        setHitlLoad("ok");
      })
      .catch(() => setHitlLoad("error"));
  const loadRuns = () =>
    api
      .runs()
      .then((rows) => {
        setRuns(rows);
        setRunsLoad("ok");
      })
      .catch(() => setRunsLoad("error"));

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

  // A storm start retires whatever the banner was saying (an SLA chase result): while it runs
  // the per-alarm progress line is the only thing worth reading.
  useEffect(() => {
    if (storming) setChase(null);
  }, [storming]);
  const bannerText = storming ? stormProg : stormErr || chase?.text || stormProg;
  const bannerBad = !storming && (!!stormErr || !!chase?.bad);

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Mission control</h1>
          <p className="lead">Every alarm runs the twelve agents; the rail follows the newest one.</p>
        </div>
        <div className="page-actions">
          <button
            className="btn storm"
            disabled={storming}
            onClick={() => onLaunchStorm("Manual storm launch")}
            title="Drops microwave hops in Rift, Mt Kenya and Nairobi East and cascades child sites under their HUB majors."
          >
            {storming ? "Storm in progress…" : "Launch heavy-rain storm (live)"}
          </button>
          <button
            className="btn"
            disabled={storming}
            onClick={async () => {
              try {
                const r = await api.monitorTick();
                setChase({ text: `Worklog monitor chased ${r.chased} ticket${r.chased === 1 ? "" : "s"} for silence.`, bad: false });
              } catch (e: any) {
                setChase({ text: `SLA chase failed: ${e?.message || e}`, bad: true });
              }
              onRefresh?.();
              loadLists();
            }}
          >
            Run SLA chase
          </button>
        </div>
      </div>

      {bannerText && (
        <div className={"storm-banner" + (bannerBad ? " danger" : "")}>
          <div>
            {storming && <span className="live-dot" aria-hidden="true" />}
            <strong>{storming ? "Heavy-rain storm" : stormErr ? "The storm stopped" : "Scenario status"}</strong>
            <div className="storm-progress">{bannerText}</div>
          </div>
          {storming && <span className="chip accent">Agents executing</span>}
        </div>
      )}

      <div className="kpis">
        <Kpi label="Open incidents" value={metrics?.open_total} />
        <Kpi label="P1 critical" value={metrics?.by_priority?.P1} tone="var(--p1)" />
        <Kpi label="P2 major" value={metrics?.by_priority?.P2} tone="var(--p2)" />
        <Kpi label="Decisions waiting" value={metrics?.hitl_pending} tone="var(--hitl)" />
        <Kpi label="Past restore SLA" value={metrics?.sla_risk} tone="var(--warn)" />
        <Kpi label="Vendor silent" value={metrics?.silent_at_risk} tone="var(--warn)" />
        <Kpi label="Open problems" value={metrics?.problems_open} />
      </div>

      <div style={{ marginBottom: "var(--s4)" }}>
        <LiveRunPanel events={events} runsRev={runsRev} onOpen={onOpen} />
      </div>

      <div className="grid-3">
        <div className="panel">
          <div className="panel-head">
            <div className="head-row">
              <h2 className="panel-title">Live incidents</h2>
              {incLoad === "ok" && <span>{open.length} open</span>}
            </div>
          </div>
          <div className="list">
            {incLoad === "loading" && incidents.length === 0 && <SkeletonRows />}
            {incLoad === "error" && <ListError what="the incidents" onRetry={loadIncidents} />}
            {incLoad === "ok" && open.length === 0 && !storming && (
              <div className="empty">
                No open incidents. Launch the heavy-rain storm above, or see closed tickets on the{" "}
                <Link to="/incidents">Incident board</Link>.
              </div>
            )}
            {open.length === 0 && storming && (
              <div className="empty">
                <span className="live-dot" aria-hidden="true" />
                Agents are opening tickets; this list fills as they do.
              </div>
            )}
            {open.map((i) => (
              <div key={i.id} className={"row" + (fresh.has(i.id) ? " flash" : "")} onClick={() => onOpen(i.id)}>
                <span className={`pill ${i.priority}`}>{i.priority}</span>
                <div className="row-main">
                  <div className="row-title">
                    <span className="row-id">{i.incident_number}</span>
                    <span>{i.site_name || i.site_id}</span>
                  </div>
                  <div className="facts">
                    {i.region_code && <span>{i.region_code}</span>}
                    {i.failure_domain && <span>{humanEnum(i.failure_domain)}</span>}
                    {(i.responsible_msp || i.assignee_name) && <span>{i.responsible_msp || i.assignee_name}</span>}
                    {i.child_sites_down ? <span>{i.child_sites_down} child sites</span> : null}
                    {i.mpesa_risk ? (
                      <span className="attn danger">
                        <IconDot />
                        M‑PESA at risk
                      </span>
                    ) : null}
                  </div>
                </div>
                <span className="status">{humanStatus(i.status)}</span>
              </div>
            ))}
          </div>
        </div>

        <div className="panel">
          <div className="panel-head">
            <div className="head-row">
              <h2 className="panel-title">Agent activity</h2>
              {quietMode ? (
                <>
                  <span>{events.length} critical</span>
                  <span>{suppressed} held back</span>
                </>
              ) : (
                <span>{events.length} events</span>
              )}
              <span>times in EAT</span>
            </div>
          </div>
          <div className="ticker">
            {events.length === 0 && (
              <div className="empty">
                {quietMode
                  ? "Quiet mode: only P1 and P2 incidents, decisions and delivery failures appear here."
                  : "Waiting for the agent stream. Each alarm appears here as it moves through Ingest, Correlate, Enrich, Severity, Ticket and Assign."}
              </div>
            )}
            {events.map((e, idx) => {
              const p = e.payload || {};
              const st = String(p.status || "").toUpperCase();
              const base = p.rationale || p.output || p.detail;
              const described = describeEvent(e).replace(/^—\s*/, "");
              const why = described || (base ? String(base).slice(0, 160) : "");
              return (
                <div key={`${e.ts}-${idx}-${e.type}`} className="ticker-line">
                  <span className="ticker-time">{fmtTime(e.ts)}</span>
                  <div>
                    <div className="ticker-head">
                      <span className="ticker-what">{eventWord(e.type)}</span>
                      {p.incident_number && <span className="mono">{p.incident_number}</span>}
                      {p.node && <span>{nodeLabel(p.node)}</span>}
                      {!ROUTINE_STEP.has(st) && <RunState status={st} routine={false} />}
                    </div>
                    {why && <div className="ticker-why">{why}</div>}
                  </div>
                </div>
              );
            })}
          </div>
        </div>

        <div className="panel">
          <div className="panel-head">
            <div className="head-row">
              <h2 className="panel-title">Approvals</h2>
              {hitlLoad === "ok" && <span>{hitl.length} waiting</span>}
            </div>
          </div>
          <div className="list">
            {hitlLoad === "loading" && hitl.length === 0 && <SkeletonRows rows={4} />}
            {hitlLoad === "error" && <ListError what="the approvals" onRetry={loadHitl} />}
            {hitlLoad === "ok" && hitl.length === 0 && (
              <div className="empty">
                No decisions waiting. Cards land in <Link to="/hitl">Approvals</Link> when an agent needs a person.
              </div>
            )}
            {hitl.slice(0, 12).map((t) => (
              // Since v8 a maintenance card has no incident: onOpen(null) was /incidents/null.
              // Those rows open Approvals, where the card itself can be decided.
              <div
                key={t.id}
                className="row"
                title={t.incident_id ? undefined : "Not about an incident: opens Approvals"}
                onClick={() => (t.incident_id ? onOpen(t.incident_id) : navigate("/hitl"))}
              >
                {t.priority ? <span className={`pill ${t.priority}`}>{t.priority}</span> : <span />}
                <div className="row-main">
                  <div className="row-title">
                    <span className={t.incident_number ? "row-id" : undefined}>{hitlSubject(t)}</span>
                  </div>
                  <div className="facts">
                    <span>{labelFor(t.task_type)}</span>
                    <span>{t.claimed_by ? `claimed by ${t.claimed_by}` : "unclaimed"}</span>
                  </div>
                </div>
                <span className="muted dim mono">{t.site_id}</span>
              </div>
            ))}
          </div>
        </div>
      </div>

      <div className="grid-2" style={{ marginTop: "var(--s4)", alignItems: "start" }}>
        <div className="panel">
          <h2 className="panel-title">Open incidents by region</h2>
          <div className="region-bars">
            {Object.entries(metrics?.by_region || {}).length === 0 && (
              <div className="empty">Region load appears when incidents are open.</div>
            )}
            {Object.entries(metrics?.by_region || {}).map(([k, v]) => (
              <div key={k} className="region-bar-row">
                <span className="muted">{k}</span>
                <div className="region-bar-track">
                  <div className="region-bar-fill" style={{ width: `${(Number(v) / maxRegion) * 100}%` }} />
                </div>
                <span className="muted mono">{String(v)}</span>
              </div>
            ))}
          </div>
        </div>
        <div className="panel">
          <div className="panel-head">
            <div className="head-row">
              <h2 className="panel-title">Recent agent runs</h2>
              <span>times in EAT</span>
            </div>
          </div>
          <div className="list" style={{ maxHeight: 220 }}>
            {runsLoad === "loading" && runs.length === 0 && <SkeletonRows rows={4} />}
            {runsLoad === "error" && <ListError what="the agent runs" onRetry={loadRuns} />}
            {runs.slice(0, 10).map((r) => (
              <div
                key={r.id}
                className="row"
                style={{ cursor: r.incident_id ? "pointer" : "default" }}
                onClick={() => r.incident_id && onOpen(r.incident_id)}
              >
                <span className="muted dim mono">{fmtTime(r.started_at)}</span>
                <div className="row-main">
                  <div className="row-title">
                    <span>{humanGraph(r.graph_name)}</span>
                    {r.trigger && <span className="muted">{humanEnum(r.trigger)}</span>}
                  </div>
                  <div className="facts">
                    {r.current_node ? <span>at {nodeLabel(r.current_node)}</span> : r.steps ? <span>{r.steps.length} steps</span> : null}
                  </div>
                  {/* Same A-13 fix as the Agent Observatory: FAILED is red with its word, never green. */}
                  <RunError status={r.status} summary={r.error_summary} />
                </div>
                <RunState status={r.status} routine={false} />
              </div>
            ))}
            {runsLoad === "ok" && runs.length === 0 && <div className="empty">Runs appear as the storm executes.</div>}
          </div>
        </div>
      </div>
    </div>
  );
}
