import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import AgentRail from "./AgentRail";
import { fmtMs, isLifecycleNode, runOutcome, runStatusWord, sumDurations, type RailStep } from "../lib/agents";
import { IconAlert, IconPause } from "../lib/icons";
import { fmtTime } from "../lib/time";
import type { NocEvent } from "../realtime/renderers";

/**
 * The newest alarm's path through the agents, live.
 *
 * Two sources, one rule. While a run is executing, its `agent.step.*` frames arrive on the
 * socket before any row is readable, so the rail is drawn from the frames (`fromEvents`).
 * Once `agent.run.finished` has arrived for that run, the stored run — refetched on the
 * `runs` slice revision — takes over, because it carries the full rationale and tool calls
 * the frames truncate. If the socket is down there are no frames and the stored run is all
 * there is, so the panel degrades to "latest stored run" rather than to nothing.
 */

interface EventRun {
  runId: string;
  incidentId: string | null;
  incidentNumber: string | null;
  steps: RailStep[];
  finished: boolean;
  status: string | null;
}

function fromEvents(events: NocEvent[]): EventRun | null {
  // Newest first on the wire; find the newest LIFECYCLE step frame (a scheduler job or an
  // assist run records steps through the same tracker under other node names, and must not
  // take over the rail), then replay its run oldest-first.
  const isStep = (e: NocEvent) =>
    (e.type === "agent.step.started" || e.type === "agent.step.completed") && isLifecycleNode(e.payload?.node);
  const newest = events.find(isStep);
  if (!newest) return null;
  const runId = newest.runId || newest.payload?.run_id;
  if (!runId) return null;
  const steps: Record<string, RailStep> = {};
  const order: string[] = [];
  let finished = false;
  let status: string | null = null;
  let incidentNumber: string | null = null;
  let incidentId: string | null = null;
  for (const e of [...events].reverse()) {
    if ((e.runId || e.payload?.run_id) !== runId) continue;
    const p = e.payload || {};
    if (p.incident_number) incidentNumber = String(p.incident_number);
    if (e.incidentId) incidentId = e.incidentId;
    if (e.type === "agent.step.started" && isLifecycleNode(p.node)) {
      if (!steps[p.node]) order.push(p.node);
      steps[p.node] = { node_name: p.node, agent_name: p.agent, status: "STARTED", input_summary: p.input, seq: p.seq };
    } else if (e.type === "agent.step.completed" && isLifecycleNode(p.node)) {
      if (!steps[p.node]) order.push(p.node);
      steps[p.node] = {
        ...(steps[p.node] || { node_name: p.node, agent_name: p.agent }),
        status: p.status || "SUCCEEDED",
        rationale: p.rationale,
        output_summary: p.output,
        duration_ms: typeof p.duration_ms === "number" ? p.duration_ms : null,
        seq: p.seq,
      };
    } else if (e.type === "agent.run.finished") {
      finished = true;
      status = p.status || null;
    }
  }
  return { runId, incidentId, incidentNumber, steps: order.map((n) => steps[n]), finished, status };
}

/**
 * A run's status as text: muted words when routine ("succeeded"), the drawn icon and the state
 * colour when it is worth a look (waiting for a decision, failed). Never a chip.
 */
export function RunState({ status, routine = true }: { status: unknown; routine?: boolean }) {
  const out = runOutcome(status);
  if (!out) return routine ? <span>{runStatusWord(status)}</span> : null;
  return (
    <span className={"state" + (out.tone ? ` ${out.tone}` : "")}>
      {out.tone === "hitl" && <IconPause />}
      {out.tone === "danger" && <IconAlert />}
      {out.word}
    </span>
  );
}

export default function LiveRunPanel({
  events,
  runsRev,
  onOpen,
  title = "Latest alarm through the agents",
  compact = true,
}: {
  events: NocEvent[];
  runsRev: number;
  onOpen?: (incidentId: string) => void;
  title?: string;
  compact?: boolean;
}) {
  const [stored, setStored] = useState<any | null>(null);
  const [storedIncident, setStoredIncident] = useState<any | null>(null);
  // "loading" until the first answer from the stored runs, so the panel shows skeletons instead
  // of claiming nothing has run; "error" only while no answer has ever arrived (a failed refetch
  // after a good one keeps the run that is on screen).
  const [load, setLoad] = useState<"loading" | "ok" | "error">("loading");
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    let cancelled = false;
    api
      .lifecycleRuns()
      .then((runs) => {
        if (cancelled) return;
        setStored(Array.isArray(runs) ? runs[0] || null : null);
        setLoad("ok");
      })
      .catch(() => {
        if (!cancelled) setLoad((l) => (l === "ok" ? l : "error"));
      });
    return () => {
      cancelled = true;
    };
  }, [runsRev, retry]);

  useEffect(() => {
    const id = stored?.incident_id;
    if (!id) {
      setStoredIncident(null);
      return;
    }
    let cancelled = false;
    api
      .incident(id)
      .then((inc) => !cancelled && setStoredIncident(inc))
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [stored?.id, stored?.incident_id]);

  const eventRun = useMemo(() => fromEvents(events), [events]);
  const useLive = !!eventRun && (!stored || stored.id !== eventRun.runId || !eventRun.finished);
  const steps: RailStep[] = useLive ? eventRun!.steps : stored?.steps || [];
  const live = useLive && !eventRun!.finished;
  const status: string | null = useLive ? (eventRun!.finished ? eventRun!.status : "RUNNING") : stored?.status || null;
  const incidentNumber = useLive ? eventRun!.incidentNumber : storedIncident?.incident_number || null;
  const incidentId = useLive ? eventRun!.incidentId : stored?.incident_id || null;
  const elapsed = useLive ? sumDurations(steps) : stored?.finished_at && stored?.started_at ? Math.max(0, +new Date(stored.finished_at) - +new Date(stored.started_at)) : sumDurations(steps);
  // Frames on the socket are an answer too: a live run draws even before the stored list loads.
  const nothing = steps.length === 0 && !stored;

  return (
    <div className="panel live-run">
      <div className="panel-head">
        <div className="head-row">
          <h2 className="panel-title">
            {live && <span className="live-dot" aria-hidden="true" />}
            {title}
          </h2>
          {incidentNumber && <span className="mono">{incidentNumber}</span>}
          {status && <RunState status={status} />}
          {steps.length > 0 && <span>{steps.length} of 12 hops</span>}
          {steps.length > 0 && <span className="mono">{fmtMs(elapsed)}</span>}
          {!useLive && stored?.started_at && <span className="mono">{fmtTime(stored.started_at)} EAT</span>}
        </div>
        {incidentId && onOpen && (
          <button className="btn sm" onClick={() => onOpen(incidentId)}>
            Open ticket
          </button>
        )}
      </div>
      {!nothing ? (
        <AgentRail steps={steps} live={live} compact={compact} caption={title} />
      ) : load === "loading" ? (
        <div role="status">
          <span className="sr-only">Loading the latest run</span>
          <div className="skeleton-rows rail-skeleton" aria-hidden="true">
            {Array.from({ length: 12 }, (_, i) => (
              <span key={i} className="skeleton" />
            ))}
          </div>
        </div>
      ) : load === "error" ? (
        <div className="empty" role="alert">
          Couldn't load the latest run.{" "}
          <button type="button" className="btn sm" onClick={() => setRetry((n) => n + 1)}>
            Retry
          </button>
        </div>
      ) : (
        <div className="empty">No alarm has been through the agents yet. Launch the storm and this rail lights up hop by hop.</div>
      )}
    </div>
  );
}
