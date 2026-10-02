import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { RunState } from "../components/LiveRunPanel";
import { agentDisplayName, fmtInt, fmtMs, humanGraph, nodeLabel, runOutcome, runStatusWord } from "../lib/agents";
import { IconDot } from "../lib/icons";
import { fmtTime } from "../lib/time";
import { ONE_COL } from "../lib/layout";
import { RunError } from "../components/RunError";

// The failed-run line lives in components/RunError.tsx (Mission control imports it without
// this page's chunk); re-exported here so older imports still work.
export { RunError } from "../components/RunError";

/** How a run was started, when it was not the ordinary alarm event ("Incident lifecycle" needs no "event"). */
const TRIGGER_WORDS: Record<string, string> = { SCHEDULE: "scheduled", REQUEST: "on request" };

function Skeleton({ rows }: { rows: number }) {
  return (
    <div role="status">
      <span className="sr-only">Loading</span>
      <div className="skeleton-rows" aria-hidden="true">
        {Array.from({ length: rows }, (_, i) => (
          <span key={i} className="skeleton" />
        ))}
      </div>
    </div>
  );
}

export default function Agents({ tick = 0 }: { tick?: number }) {
  // Both lists are null until their first answer, so loading never reads as "nothing here".
  const [agents, setAgents] = useState<any[] | null>(null);
  const [agentsFailed, setAgentsFailed] = useState(false);
  const [agentsRetry, setAgentsRetry] = useState(0);
  const [runs, setRuns] = useState<any[] | null>(null);
  const [runsFailed, setRunsFailed] = useState(false);
  const [retry, setRetry] = useState(0);
  const [stats, setStats] = useState<Record<string, any>>({});

  useEffect(() => {
    let live = true;
    setAgentsFailed(false);
    api
      .agents()
      .then((r) => {
        if (live) setAgents(Array.isArray(r) ? r : []);
      })
      .catch(() => {
        if (live) setAgentsFailed(true);
      });
    return () => {
      live = false;
    };
  }, [agentsRetry]);

  // Per-agent throughput from the productivity rollup (everything on record): steps,
  // failures, timings. Advisory for the roster; a failure here leaves the rows static.
  // Debounced on the runs revision so a storm costs one rollup per alarm, not per frame.
  useEffect(() => {
    let cancelled = false;
    const t = window.setTimeout(() => {
      api
        .productivity(0)
        .then((p) => {
          if (cancelled) return;
          const map: Record<string, any> = {};
          for (const a of p?.agents || []) if (a?.name) map[a.name] = a;
          setStats(map);
        })
        .catch(() => undefined);
    }, 600);
    return () => {
      cancelled = true;
      window.clearTimeout(t);
    };
  }, [tick]);

  useEffect(() => {
    let live = true;
    const take = (r: any) => {
      if (!live) return;
      setRuns(Array.isArray(r) ? r : []);
      setRunsFailed(false);
    };
    api
      .runs()
      .then(take)
      .catch(() => {
        if (live) setRunsFailed(true);
      });
    const id = window.setInterval(() => {
      api.runs().then(take).catch(() => undefined);
    }, 4000);
    return () => {
      live = false;
      window.clearInterval(id);
    };
    // `tick` is the debounced `runs` slice revision: a run start/finish refreshes
    // this list at once, and the 4 s poll stays as the WS-down fallback.
  }, [tick, retry]);

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Agent observatory</h1>
          <p
            className="lead"
            title="Each agent has a mission, a criticality and the tools it may call. The counts are everything on record; during a storm the run list moves through running, waiting for a decision and succeeded as each alarm is processed."
          >
            Twelve agents under one supervisor: their missions, records and live runs.
          </p>
        </div>
      </div>
      <div className="grid-2">
        <div className="panel">
          <h2 className="panel-title">Agent roster</h2>
          <div aria-busy={agents === null || undefined}>
            {agents === null && agentsFailed && (
              <div className="empty" role="alert">
                Couldn't load the agent roster.{" "}
                <button className="btn sm" onClick={() => setAgentsRetry((n) => n + 1)}>
                  Retry
                </button>
              </div>
            )}
            {agents === null && !agentsFailed && <Skeleton rows={8} />}
            {agents !== null && agents.length === 0 && <div className="empty">No agents are registered.</div>}
            {(agents || []).map((a) => {
              const s = stats[a.name];
              const failed = s?.failed || 0;
              const hops: string[] = (a.node_ids || []).map(nodeLabel);
              return (
                <div key={a.name} className="row static" style={ONE_COL}>
                  <div className="row-main">
                    <div className="head-row">
                      <h3 className="panel-title">{agentDisplayName(a.name)}</h3>
                      <span>{hops.length ? hops.join(", ") : "runs on request"}</span>
                    </div>
                    <p className="hop-what">{a.mission}</p>
                    <div className="agent-stats">
                      <span>
                        <strong className="mono">{fmtInt(s?.steps ?? 0)}</strong> steps
                      </span>
                      <span>
                        avg <strong className="mono">{fmtMs(s?.avg_ms)}</strong>
                      </span>
                      {failed ? (
                        <span className="attn danger">
                          <IconDot /> <strong className="mono">{fmtInt(failed)}</strong> failed
                        </span>
                      ) : (
                        <span>
                          <strong className="mono">0</strong> failed
                        </span>
                      )}
                      {s?.last_step_at && (
                        <span>
                          last <span className="mono">{fmtTime(s.last_step_at)}</span>
                        </span>
                      )}
                    </div>
                    <div className="agent-tags facts">
                      <span>{a.criticality === "fail_closed" ? "an error stops the run" : "an error fails only its step"}</span>
                      {a.mcp?.length ? (
                        <span>
                          {a.mcp.length} tool connection{a.mcp.length === 1 ? "" : "s"} declared
                        </span>
                      ) : null}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
        <div className="panel">
          <div className="panel-head">
            <h2 className="panel-title">Live and recent runs</h2>
            <span className="muted">Times in EAT</span>
          </div>
          <div className="list" aria-busy={runs === null || undefined}>
            {runs === null && runsFailed && (
              <div className="empty" role="alert">
                Couldn't load the runs.{" "}
                <button className="btn sm" onClick={() => setRetry((n) => n + 1)}>
                  Retry
                </button>
              </div>
            )}
            {runs === null && !runsFailed && <Skeleton rows={6} />}
            {runs !== null && runs.length === 0 && (
              <div className="empty">
                No runs yet. Launch the storm from <Link to="/">Mission control</Link>.
              </div>
            )}
            {(runs || []).slice(0, 15).map((r) => {
              const trigger = TRIGGER_WORDS[String(r.trigger || "").toUpperCase()];
              return (
                // Time first: every clock reads HH:MM:SS in mono, so the titles start at one x;
                // the state sits at the right, as on Mission control.
                <div key={r.id} className="row static">
                  <span className="muted dim mono">{fmtTime(r.started_at)}</span>
                  <div className="row-main">
                    <div className="row-title">
                      <span>{humanGraph(r.graph_name)}</span>
                      {trigger && <span className="muted">{trigger}</span>}
                    </div>
                    <div className="facts">
                      {r.current_node && <span>at {nodeLabel(r.current_node)}</span>}
                      <span>
                        <span className="mono">{r.steps?.length ?? 0}</span> steps
                      </span>
                    </div>
                    {/* A failed run keeps current_node at the node that broke; error_summary says why.
                        Without it the operator sees FAILED and has to open the DB to learn anything. */}
                    <RunError status={r.status} summary={r.error_summary} />
                  </div>
                  {/* A success is the normal outcome: a muted word. Waiting or failed: the state colour and icon. */}
                  {runOutcome(r.status) ? (
                    <RunState status={r.status} routine={false} />
                  ) : (
                    <span className="muted">{runStatusWord(r.status)}</span>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
}
