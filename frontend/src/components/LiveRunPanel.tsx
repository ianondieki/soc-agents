import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import AgentRail from "./AgentRail";
import { fmtMs, sumDurations, type RailStep } from "../lib/agents";
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
  // Newest first on the wire; find the newest step frame, then replay its run oldest-first.
  const newest = events.find((e) => e.type === "agent.step.started" || e.type === "agent.step.completed");
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
    if (e.type === "agent.step.started" && p.node) {
      if (!steps[p.node]) order.push(p.node);
      steps[p.node] = { node_name: p.node, agent_name: p.agent, status: "STARTED", input_summary: p.input, seq: p.seq };
    } else if (e.type === "agent.step.completed" && p.node) {
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

  useEffect(() => {
    let cancelled = false;
    api
      .lifecycleRuns()
      .then((runs) => {
        if (cancelled || !Array.isArray(runs)) return;
        setStored(runs[0] || null);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [runsRev]);

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
  const nothing = steps.length === 0 && !stored;

  return (
    <div className="panel live-run">
      <div className="panel-head">
        <h3>
          <span className={live ? "live-dot" : "live-dot off"} />
          {title}
        </h3>
        <div className="chips">
          {incidentNumber && <span className="chip accent">{incidentNumber}</span>}
          {status && <span className={"chip " + (status === "FAILED" ? "danger" : status === "WAITING_HITL" ? "hitl" : status === "RUNNING" ? "accent" : "ok")}>{status === "WAITING_HITL" ? "waiting for a human" : status.toLowerCase()}</span>}
          {steps.length > 0 && <span className="chip">{steps.length} of 12 hops · {fmtMs(elapsed)}</span>}
          {!useLive && stored?.started_at && <span className="chip">{fmtTime(stored.started_at)} EAT</span>}
          {incidentId && onOpen && (
            <button className="btn" onClick={() => onOpen(incidentId)}>
              Open ticket
            </button>
          )}
        </div>
      </div>
      {nothing ? (
        <div className="empty">No alarm has been through the agents yet. Launch the storm and this rail lights up hop by hop.</div>
      ) : (
        <AgentRail steps={steps} live={live} compact={compact} caption={title} />
      )}
    </div>
  );
}
