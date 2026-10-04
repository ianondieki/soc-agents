import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api";
import { isStorming, stormCounts, stormStopLine, type StormState } from "../lib/demo";
import LiveRunPanel, { RunState } from "../components/LiveRunPanel";
import { MPESA_TITLE, alarmSite, humanEnum, humanGraph, humanStatus, nodeLabel, ownerName, priorityTitle, regionName, runOutcomeOf, triggerWord } from "../lib/agents";
import { detailOf } from "../lib/apiError";
import { IconDot } from "../lib/icons";
import { labelFor, sortQueue } from "../lib/hitl";
import { fmtTime } from "../lib/time";
// One run-status palette and one error line for both run lists (A-13), so the two pages
// cannot drift apart again.
import { RunError } from "../components/RunError";
import { describeEvent, isDecisionStep, isKeyEvent, isMock, type NocEvent } from "../realtime/renderers";
import { TICKER_MAX } from "../realtime/feed";
import { useQuietMode, useSuppressedCount, useTickerEvents } from "../realtime/RealtimeContext";
import { hitlSubject } from "../lib/hitlSubject";

/** One figure of the strip. Colour only when the number is worth a look; zero stays quiet. */
function Kpi({ label, value, tone }: { label: string; value: number | undefined; tone?: "p1" | "p2" | "hitl" | "warn" }) {
  const n = typeof value === "number" ? value : null;
  return (
    <div className="kpi">
      <div className="label">{label}</div>
      <div className={"value" + (n === 0 ? " zero" : tone && n ? ` ${tone}` : "")}>{n == null ? "—" : n}</div>
    </div>
  );
}

/** Each list on this page shows its first few rows in full; the rest are one link away. */
const PEEK = 5;

/** "Show all 12" under a list, only when the list has more than it shows. */
function ShowAll({ to, total, what, label }: { to: string; total: number; what: string; label?: string }) {
  if (total <= PEEK) return null;
  return (
    <div className="list-foot">
      <Link className="link" to={to}>
        {label || `Show all ${total}`}
        <span className="sr-only"> {what}</span>
      </Link>
    </div>
  );
}

/** A row opens its ticket on a click anywhere, except on a link inside it (which already went). */
function fromLink(e: { target: EventTarget | null }): boolean {
  return e.target instanceof Element && !!e.target.closest("a");
}

/** "stale": a refetch failed after a good answer; the rows on screen stay, with a note. */
type Load = "loading" | "ok" | "error" | "stale";

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

/** A refetch failed but the last answer is still good: keep the rows, say so once. */
function StaleNote() {
  return <div className="list-stale muted">Couldn't refresh; showing the last list received.</div>;
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
  "email.sent": "Email sent", // a real SMTP send only; a mock reads "Email stored" (eventWord)
  "email.failed": "Email failed",
  "outbox.failed": "Delivery failed",
  "external_signal.updated": "Signal updated",
  "power_notice.new": "Power notice",
  "complaint.surge": "Complaint surge",
  "pir.opened": "Review opened",
  "regulatory.deadline": "Regulatory deadline",
  "scheduler.job_failed": "Job failed",
  "security.redaction_miss": "Redaction miss",
  "monitor.chase": "Vendor chase",
  "demo.rain_storm.complete": "Storm complete",
};

function eventWord(e: NocEvent): string {
  const type = e.type;
  // The Approval step parking a run on a person is the moment a decision is needed.
  if (isDecisionStep(e)) return "Decision needed";
  // A mock adapter stored the message and sent nothing: neither the head nor the line says "sent".
  if ((type === "email.sent" || type === "email.failed") && isMock(e.payload)) return "Email stored";
  if (EVENT_WORDS[type]) return EVENT_WORDS[type];
  const s = String(type || "event").replace(/[._]+/g, " ").trim();
  return s ? s[0].toUpperCase() + s.slice(1) : "Event";
}

/** Statuses the ticker names: only the ones worth a look, and only on an agent frame (an email
 *  frame's "SENT" is not a run state, and a mock never sent anything). */
const ROUTINE_STEP = new Set(["", "SUCCEEDED", "STARTED", "RUNNING", "PENDING"]);
const showsRunState = (e: NocEvent) => e.type.startsWith("agent.");

/** "5 tickets opened", "1 alarm folded". */
const count = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

/** The ticker's two views: the moments a shift acts on (default), or every agent step. */
type TickerView = "key" | "all";
const TICKER_VIEW_KEY = "noc_ticker_view_v1";

function readTickerView(): TickerView {
  try {
    return sessionStorage.getItem(TICKER_VIEW_KEY) === "all" ? "all" : "key";
  } catch {
    return "key";
  }
}

function MissionControl({
  metrics,
  metricsStale = false,
  profile,
  incidentsRev,
  hitlRev,
  runsRev,
  storm,
  onLaunchStorm,
  onResumeStorm,
  onOpen,
  onRefresh,
}: {
  metrics: any;
  /** The latest metrics call failed while the API still answers: the strip keeps the last counts
   *  and says they are not updating (nothing else on the page changes). */
  metricsStale?: boolean;
  /** The operator profile: region names for the rows ("Rift Valley", not "RFT"). */
  profile?: any;
  /** Revision of the incidents slice — see realtime/renderers.ts. */
  incidentsRev: number;
  hitlRev: number;
  runsRev: number;
  /** The live storm is owned by App so the guided demo and Settings can start it too. */
  storm: StormState;
  onLaunchStorm: () => void;
  /** Carry on from the alarm after the one that stopped the storm. */
  onResumeStorm: () => void;
  onOpen: (id: string) => void;
  onRefresh?: () => void;
}) {
  const navigate = useNavigate();
  // The ticker reads the feed store directly: a new line re-renders this page, never App.
  const events = useTickerEvents();
  const suppressed = useSuppressedCount();
  const quietMode = useQuietMode();
  const storming = isStorming(storm);
  const [incidents, setIncidents] = useState<any[]>([]);
  const [hitl, setHitl] = useState<any[]>([]);
  const [runs, setRuns] = useState<any[]>([]);
  const [incLoad, setIncLoad] = useState<Load>("loading");
  const [hitlLoad, setHitlLoad] = useState<Load>("loading");
  const [runsLoad, setRunsLoad] = useState<Load>("loading");
  const [chase, setChase] = useState<{ text: string; bad: boolean } | null>(null);
  const [tickerView, setTickerView] = useState<TickerView>(readTickerView);
  const chooseTickerView = (v: TickerView) => {
    setTickerView(v);
    try {
      sessionStorage.setItem(TICKER_VIEW_KEY, v);
    } catch {
      /* storage blocked: the choice lasts until the page is left */
    }
  };

  // Flash a row only when it is new on the board. The ids on the first good answer are the
  // board as found, not news, so they seed the set and nothing flashes on arrival.
  const seenIds = useRef<Set<string> | null>(null);
  const [fresh, setFresh] = useState<Set<string>>(() => new Set());
  useEffect(() => {
    if (incLoad === "loading") return;
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
  }, [incidents, incLoad]);

  // A failed refetch after a good answer keeps the rows ("stale"); only a list that never
  // loaded shows the error with its Retry.
  const failed = (l: Load): Load => (l === "ok" || l === "stale" ? "stale" : "error");
  const loadIncidents = useCallback(
    () =>
      api
        .incidents()
        .then((rows) => {
          setIncidents(rows);
          setIncLoad("ok");
        })
        .catch(() => setIncLoad(failed)),
    []
  );
  const loadHitl = useCallback(
    () =>
      api
        .hitl()
        .then((rows) => {
          setHitl(rows);
          setHitlLoad("ok");
        })
        .catch(() => setHitlLoad(failed)),
    []
  );
  // One /runs request feeds both the run list and the live rail (LiveRunPanel keeps the
  // lifecycle runs), instead of one each.
  const loadRuns = useCallback(
    () =>
      api
        .runs()
        .then((rows) => {
          setRuns(rows);
          setRunsLoad("ok");
        })
        .catch(() => setRunsLoad(failed)),
    []
  );

  const loadLists = () => {
    loadIncidents();
    loadHitl();
    loadRuns();
  };

  // Defect #26: one effect per slice instead of one effect for every WS frame.
  // An agent.step.* burst bumps none of these, so the three panels below hold
  // still while the ticker streams. Each load starts a tick later, so a mount React undoes at
  // once (its development double mount) sends nothing: each list loads once on mount. The
  // socket's replay of old frames on connect bumps no revision (realtime/useRealtime.ts).
  useEffect(() => {
    const t = window.setTimeout(loadIncidents, 0);
    return () => window.clearTimeout(t);
  }, [incidentsRev, loadIncidents]);
  useEffect(() => {
    const t = window.setTimeout(loadHitl, 0);
    return () => window.clearTimeout(t);
  }, [hitlRev, loadHitl]);
  useEffect(() => {
    const t = window.setTimeout(loadRuns, 0);
    return () => window.clearTimeout(t);
  }, [runsRev, loadRuns]);

  const open = incidents.filter((i) => !["CLOSED", "CANCELLED"].includes(i.status));
  // The order Approvals works the queue in: P1 first, then the card that has waited longest.
  const queue = useMemo(() => sortQueue(hitl), [hitl]);
  const regions = Object.entries(metrics?.by_region || {});
  const maxRegion = Math.max(1, ...regions.map(([, v]) => Number(v) || 0));
  // A run row names its ticket by INC number when the board has loaded it.
  const incNumber = new Map<string, string>(incidents.map((i) => [i.id, i.incident_number]));
  const pendingDecisions = typeof metrics?.hitl_pending === "number" ? metrics.hitl_pending : hitlLoad === "ok" ? hitl.length : 0;

  // A storm start retires whatever the banner was saying (a vendor chase result): while it runs
  // the per-alarm progress line is the only thing worth reading.
  useEffect(() => {
    if (storming) setChase(null);
  }, [storming]);

  const showStorm = storm.phase !== "idle" && !(chase && !storming);
  // The ticker: the moments a shift acts on by default, every agent step one click away.
  const keyOnly = tickerView === "key";
  const tickerLines = keyOnly ? events.filter(isKeyEvent) : events;
  const counts = stormCounts(storm);

  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <h1>Mission control</h1>
          <p className="lead">Every alarm goes through twelve agent steps; the panel below follows the newest ticket.</p>
        </div>
        <div className="page-actions">
          <button
            className="btn storm"
            disabled={storming}
            onClick={onLaunchStorm}
            title="Fails microwave links in Rift Valley, Mt Kenya and Nairobi East; the sites behind each HUB go down with it."
          >
            {storming ? "Storm running…" : "Launch heavy-rain storm (live)"}
          </button>
          <button
            className="btn"
            disabled={storming}
            onClick={async () => {
              try {
                const r = await api.monitorTick();
                const n = Number(r?.chased) || 0;
                setChase({
                  text:
                    n > 0
                      ? `Chased the vendor on ${n} ticket${n === 1 ? "" : "s"} with no update.`
                      : "No vendor needed a chase: every open ticket has a recent update.",
                  bad: false,
                });
              } catch (e: any) {
                setChase({ text: `Couldn't chase vendors: ${detailOf(e)}`, bad: true });
              }
              onRefresh?.();
              loadLists();
            }}
          >
            Chase silent vendors
          </button>
        </div>
      </div>

      {showStorm && (
        <div
          className={"storm-banner" + (storm.phase === "failed" ? " danger" : "")}
          role={storm.phase === "failed" ? "alert" : "status"}
        >
          <div className="storm-text">
            <strong className="storm-title">
              {storming && <span className="live-dot" aria-hidden="true" />}
              {storm.phase === "failed" ? "The storm stopped" : storm.phase === "done" ? "Storm complete" : "Heavy-rain storm"}
            </strong>
            <div className="storm-progress">
              {storm.phase === "failed"
                ? stormStopLine(storm)
                : storm.phase === "done"
                  ? `${count(storm.total, "alarm", "alarms")} through the agents.`
                  : storm.text}
            </div>
            <div className="storm-counts">{counts}</div>
          </div>
          {storm.phase === "failed" && (
            <button type="button" className="btn sm" onClick={onResumeStorm}>
              Resume storm
            </button>
          )}
          {storm.phase === "done" && pendingDecisions > 0 && (
            <button type="button" className="btn sm hitl" onClick={() => navigate("/hitl")}>
              Open Approvals ({pendingDecisions})
            </button>
          )}
        </div>
      )}
      {!showStorm && chase && (
        <div className={"storm-banner" + (chase.bad ? " danger" : "")} role={chase.bad ? "alert" : "status"}>
          <div className="storm-text">
            <strong className="storm-title">Vendor chase</strong>
            <div className="storm-progress">{chase.text}</div>
          </div>
        </div>
      )}

      <div className={"kpi-strip" + (metricsStale ? " stale" : "")}>
        <div className="kpis">
          <Kpi label="Open tickets" value={metrics?.open_total} />
          <Kpi label="P1 critical" value={metrics?.by_priority?.P1} tone="p1" />
          <Kpi label="P2 major" value={metrics?.by_priority?.P2} tone="p2" />
          {/* Neutral ink: the violet for "a person decides" is on the top bar and in Approvals. */}
          <Kpi label="Decisions waiting" value={metrics?.hitl_pending} />
          <Kpi label="Past restore SLA" value={metrics?.sla_risk} tone="warn" />
          <Kpi label="Vendor silent" value={metrics?.silent_at_risk} tone="warn" />
          <Kpi label="Open problems" value={metrics?.problems_open} />
        </div>
        {/* Only the counts call failed (the API still answers): the figures stay, and say so. */}
        {metricsStale && (
          <p className="kpis-note" role="status">
            <span className="attn warn">
              <IconDot />
              Counts aren't updating
            </span>
          </p>
        )}
      </div>

      <LiveRunPanel
        runs={runsLoad === "ok" || runsLoad === "stale" ? runs : null}
        runsFailed={runsLoad === "error"}
        onRetry={loadRuns}
        runsRev={runsRev}
        onOpen={onOpen}
      />

      {/* The two lists a shift acts on sit side by side, wide enough for one-line approval rows;
          the two agent logs and the region load follow. */}
      <div className="grid-2 even">
        <div className="panel">
          <div className="panel-head">
            <div className="head-row">
              <h2 className="panel-title">Live tickets</h2>
              {(incLoad === "ok" || incLoad === "stale") && <span>{open.length} open</span>}
            </div>
          </div>
          <div className="list peek">
            {incLoad === "loading" && incidents.length === 0 && <SkeletonRows />}
            {incLoad === "error" && <ListError what="the tickets" onRetry={loadIncidents} />}
            {incLoad === "stale" && <StaleNote />}
            {incLoad === "ok" && open.length === 0 && !storming && (
              <div className="empty">
                No open tickets. Launch the storm above, or see closed ones on the <Link to="/incidents">Incident board</Link>.
              </div>
            )}
            {incLoad !== "loading" && open.length === 0 && storming && (
              <div className="empty">Agents are opening tickets; this list fills as they do.</div>
            )}
            {open.slice(0, PEEK).map((i) => (
              <div
                key={i.id}
                className={"row" + (fresh.has(i.id) ? " flash" : "")}
                onClick={(e) => !fromLink(e) && onOpen(i.id)}
              >
                <span className={`pill ${i.priority}`} title={priorityTitle(i.priority)}>
                  {i.priority}
                </span>
                <div className="row-main">
                  <div className="row-title">
                    <Link className="row-id" to={`/incidents/${i.id}`}>
                      {i.incident_number}
                    </Link>
                    <span>{i.site_name || i.site_id}</span>
                  </div>
                  <div className="facts">
                    {i.region_code && <span title={i.region_code}>{regionName(i.region_code, profile)}</span>}
                    {i.failure_domain && <span>{humanEnum(i.failure_domain)}</span>}
                    {(i.responsible_msp || i.assignee_name) && <span>{ownerName(i.responsible_msp || i.assignee_name)}</span>}
                    {i.child_sites_down ? <span>{i.child_sites_down} child sites down</span> : null}
                    {i.mpesa_risk ? (
                      <span className="attn danger" title={MPESA_TITLE}>
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
          {(incLoad === "ok" || incLoad === "stale") && (
            <ShowAll to="/incidents" total={open.length} what="open tickets on the Incident board" />
          )}
        </div>

        <div className="panel">
          <div className="panel-head">
            <div className="head-row">
              <h2 className="panel-title">Approvals</h2>
              {(hitlLoad === "ok" || hitlLoad === "stale") && <span>{hitl.length} waiting</span>}
            </div>
          </div>
          <div className="list peek">
            {hitlLoad === "loading" && hitl.length === 0 && <SkeletonRows rows={4} />}
            {hitlLoad === "error" && <ListError what="the approvals" onRetry={loadHitl} />}
            {hitlLoad === "stale" && <StaleNote />}
            {hitlLoad === "ok" && hitl.length === 0 && (
              <div className="empty">
                No decisions waiting. Cards land in <Link to="/hitl">Approvals</Link> when an agent needs a person.
              </div>
            )}
            {queue.slice(0, PEEK).map((t) => {
              // Since v8 a maintenance card has no incident: onOpen(null) was /incidents/null.
              // Those rows open Approvals, where the card itself can be decided.
              const to = t.incident_id ? `/incidents/${t.incident_id}` : "/hitl";
              return (
                <div
                  key={t.id}
                  className="row split"
                  title={t.incident_id ? undefined : "Not about a ticket: opens Approvals"}
                  onClick={(e) => !fromLink(e) && (t.incident_id ? onOpen(t.incident_id) : navigate("/hitl"))}
                >
                  {t.priority ? (
                    <span className={`pill ${t.priority}`} title={priorityTitle(t.priority)}>
                      {t.priority}
                    </span>
                  ) : (
                    <span />
                  )}
                  {/* What (INC number) and which decision on the first line; who holds it and the
                      site under them, so the row fits the narrower Approvals column. */}
                  <div className="row-main">
                    <div className="row-title">
                      <Link className={t.incident_number ? "row-id" : undefined} to={to}>
                        {hitlSubject(t)}
                      </Link>
                      <span>{labelFor(t.task_type)}</span>
                    </div>
                    <div className="facts">
                      <span>{t.claimed_by ? `claimed by ${t.claimed_by}` : "unclaimed"}</span>
                      {t.site_id && <span className="mono">{t.site_id}</span>}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
          {(hitlLoad === "ok" || hitlLoad === "stale") && <ShowAll to="/hitl" total={hitl.length} what="decisions in Approvals" />}
        </div>
      </div>

      <div className="grid-3 align-start">
        <div className="panel">
          <div className="panel-head">
            {/* The title says which view is on; the control beside it switches. */}
            <div className="head-row">
              <h2 className="panel-title">{keyOnly ? "Key events" : "Every step"}</h2>
              {quietMode ? (
                <>
                  <span>{`${tickerLines.length} critical`}</span>
                  <span>{suppressed} held back</span>
                </>
              ) : keyOnly ? (
                <span>{`${tickerLines.length} of ${events.length >= TICKER_MAX ? `the latest ${TICKER_MAX}` : events.length}`}</span>
              ) : (
                <span>{events.length >= TICKER_MAX ? `latest ${TICKER_MAX}` : `${events.length} events`}</span>
              )}
              <span>Times in EAT</span>
            </div>
            <div className="seg" role="group" aria-label="Agent activity to show">
              <button type="button" aria-pressed={keyOnly} onClick={() => chooseTickerView("key")}>
                Key events
              </button>
              <button type="button" aria-pressed={!keyOnly} onClick={() => chooseTickerView("all")}>
                Every step
              </button>
            </div>
          </div>
          <div className="ticker peek">
            {tickerLines.length === 0 && (
              <div className="empty">
                {quietMode
                  ? "Quiet mode: only P1 and P2 tickets, decisions and delivery failures appear here."
                  : events.length > 0 && keyOnly
                    ? "No key events in the latest lines. Every step shows each agent's work."
                    : keyOnly
                      ? "Waiting for the agent stream. Tickets opened, alarms folded, decisions and messages appear here."
                      : "Waiting for the agent stream. Each alarm appears here as it moves through Ingest, Correlate, Enrich, Severity, Ticket and Assign."}
              </div>
            )}
            {tickerLines.slice(0, PEEK).map((e) => {
              const p = e.payload || {};
              const st = String(p.status || "").toUpperCase();
              const base = p.rationale || p.output || p.detail;
              // A describer replaces a raw detail written for a developer (realtime/renderers.ts).
              const described = describeEvent(e).replace(/^—\s*/, "");
              const why = described || (base ? String(base).slice(0, 160) : "");
              // "Decision needed" already says what the step and its state would.
              const decision = isDecisionStep(e);
              return (
                <div key={e.uid} className="ticker-line">
                  <span className="ticker-time">{fmtTime(e.ts)}</span>
                  <div>
                    <div className="ticker-head">
                      <span className="ticker-what">{eventWord(e)}</span>
                      {p.incident_number &&
                        (e.incidentId ? (
                          <Link className="mono" to={`/incidents/${e.incidentId}`}>
                            {p.incident_number}
                          </Link>
                        ) : (
                          <span className="mono">{p.incident_number}</span>
                        ))}
                      {p.node && !decision && <span>{nodeLabel(p.node)}</span>}
                      {showsRunState(e) && !decision && !ROUTINE_STEP.has(st) && <RunState status={st} routine={false} />}
                    </div>
                    {why && <div className="ticker-why">{why}</div>}
                  </div>
                </div>
              );
            })}
          </div>
          <ShowAll to="/audit" total={tickerLines.length} what="agent steps and decisions" label="Show all in Audit trail" />
        </div>

        <div className="panel">
          <div className="panel-head">
            <div className="head-row">
              <h2 className="panel-title">Recent agent runs</h2>
              <span>Times in EAT</span>
            </div>
          </div>
          <div className="list peek">
            {runsLoad === "loading" && runs.length === 0 && <SkeletonRows rows={4} />}
            {runsLoad === "error" && <ListError what="the agent runs" onRetry={loadRuns} />}
            {runsLoad === "stale" && <StaleNote />}
            {runs.slice(0, PEEK).map((r) => {
              const inc = r.incident_id ? incNumber.get(r.incident_id) : undefined;
              const trigger = triggerWord(r.trigger);
              // What the run did: "INC000004 opened", "folded into INC000004", or, for a run
              // still on its way or another kind of job, its name.
              const did = runOutcomeOf(r);
              const ticket = did.ticket || inc || null;
              const site = alarmSite(r.steps);
              const to = r.incident_id ? `/incidents/${r.incident_id}` : null;
              const ticketLink = (label: string) =>
                to ? (
                  <Link className="row-id" to={to}>
                    {label}
                  </Link>
                ) : (
                  <span className="row-id">{label}</span>
                );
              return (
                <div
                  key={r.id}
                  className={"row" + (r.incident_id ? "" : " static")}
                  onClick={(e) => r.incident_id && !fromLink(e) && onOpen(r.incident_id)}
                >
                  <span className="muted dim mono">{fmtTime(r.started_at)}</span>
                  <div className="row-main">
                    <div className="row-title">
                      {did.kind === "opened" ? (
                        <span>
                          {ticketLink(ticket || "Ticket")} opened
                        </span>
                      ) : did.kind === "folded" ? (
                        <span>folded into {ticketLink(ticket || "an open ticket")}</span>
                      ) : (
                        <>
                          {to && ticket && ticketLink(ticket)}
                          <span>{humanGraph(r.graph_name)}</span>
                          {site && !ticket && <span className="mono">{site}</span>}
                          {trigger && <span className="muted">{trigger}</span>}
                        </>
                      )}
                    </div>
                    {/* The state sits on the second line, so a long word never squeezes the title. */}
                    <div className="facts">
                      {/* Where a running run is; for any other, how many steps it took. */}
                      {String(r.status || "").toUpperCase() === "RUNNING" && r.current_node ? (
                        <span>at {nodeLabel(r.current_node)}</span>
                      ) : r.steps ? (
                        <span>{r.steps.length} steps</span>
                      ) : null}
                      <RunState status={r.status} routine={false} />
                    </div>
                    {/* Same A-13 fix as the Agent Observatory: FAILED is red with its word, never green. */}
                    <RunError status={r.status} summary={r.error_summary} />
                  </div>
                </div>
              );
            })}
            {runsLoad === "ok" && runs.length === 0 && <div className="empty">Runs appear as the storm executes.</div>}
          </div>
          {(runsLoad === "ok" || runsLoad === "stale") && (
            <ShowAll to="/agents" total={runs.length} what="agent runs" label="Show all in Agent observatory" />
          )}
        </div>

        <div className="panel">
          <h2 className="panel-title">Open tickets by region</h2>
          {metrics == null ? (
            <SkeletonRows rows={4} />
          ) : regions.length === 0 ? (
            <div className="empty">Region load appears when tickets are open.</div>
          ) : (
            <div className="region-bars">
              {regions.map(([k, v]) => (
                <div key={k} className="region-bar-row" title={k}>
                  <span className="muted region-bar-name">{regionName(k, profile)}</span>
                  <div className="region-bar-track">
                    {/* Grows with transform, not width: a compositor animation, no layout. */}
                    <div className="region-bar-fill" style={{ transform: `scaleX(${Math.min(1, (Number(v) || 0) / maxRegion)})` }} />
                  </div>
                  <span className="muted mono">{String(v)}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

/** Memoised: App re-renders on a debounced flush or a metrics answer; this page re-renders when
 *  one of its own props moves, and for a new ticker line through the feed store. */
export default memo(MissionControl);
