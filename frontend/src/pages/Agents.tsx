import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import {
  LIFECYCLE_NODES,
  agentDisplayName,
  fmtInt,
  fmtMs,
  humanEnum,
  humanGraph,
  runChipClass,
  runStatusWord,
} from "../lib/agents";
import { IconDot } from "../lib/icons";
import { fmtTime } from "../lib/time";

/**
 * Chip class for an agent run's status (A-13). Only SUCCEEDED earns green: the old ternary
 * painted everything that was not in flight green, so a FAILED run looked like a success.
 * FAILED uses the static `chip.danger`, not the pulsing `chip.bad` — a failed run is a
 * finished fact to read, not a live alarm — and the chip text is the status word itself, so
 * the state never rests on colour alone (§7.10). Anything else (CANCELLED, a status added
 * later) falls back to the neutral chip rather than borrowing green.
 */
export function runChip(status: string | undefined): string {
  if (status === "RUNNING" || status === "WAITING_HITL") return "chip accent";
  if (status === "SUCCEEDED") return "chip ok";
  if (status === "FAILED") return "chip danger";
  return "chip";
}

/**
 * The line under a run that says why it did not succeed — one component for both run
 * lists (the Observatory and Mission Control), beside runChip for the same reason: the
 * two pages had already drifted apart once on a FAILED run with no summary.
 *
 * Shown for any status that is not in flight and not a success. That includes CANCELLED,
 * because rejecting a HITL gate ends the run CANCELLED with the rejection reason in
 * error_summary (main.py _finish_waiting_run), and hiding it left the operator with a
 * bare word. A FAILED run says so even with no summary recorded — the absence is itself
 * the finding; any other status with nothing to say shows nothing. The colour is the
 * .chip.danger text literal: styles.css has no danger text token to reference.
 */
export function RunError({ status, summary }: { status?: string; summary?: string | null }) {
  if (!status || status === "RUNNING" || status === "WAITING_HITL" || status === "SUCCEEDED") return null;
  const text = typeof summary === "string" ? summary.trim() : "";
  if (!text && status !== "FAILED") return null;
  return (
    <div className="muted" style={{ color: "#ffb4c0", overflowWrap: "anywhere" }}>
      {status === "FAILED" ? "Error" : "Reason"}: {text || "no error summary recorded"}
    </div>
  );
}

/** "ENRICH" → "Enrich": a node id as the rail labels it. */
function nodeLabel(id: unknown): string {
  return LIFECYCLE_NODES.find((n) => n.id === id)?.label || humanEnum(id);
}

export default function Agents({ tick = 0 }: { tick?: number }) {
  const [agents, setAgents] = useState<any[]>([]);
  // null until the first answer, so loading never reads as "no runs yet".
  const [runs, setRuns] = useState<any[] | null>(null);
  const [runsFailed, setRunsFailed] = useState(false);
  const [retry, setRetry] = useState(0);
  const [stats, setStats] = useState<Record<string, any>>({});

  useEffect(() => {
    api.agents().then(setAgents).catch(console.error);
  }, []);

  // Per-agent throughput from the productivity rollup (everything on record): steps,
  // failures, timings. Advisory for the roster; a failure here leaves the cards static.
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
      .catch((e) => {
        console.error(e);
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
          <h3>Agent roster</h3>
          <div className="grid-2" style={{ gridTemplateColumns: "1fr 1fr" }}>
            {agents.map((a) => {
              const s = stats[a.name];
              const failed = s?.failed || 0;
              return (
                <div key={a.name} className="agent-card">
                  <h4>{agentDisplayName(a.name)}</h4>
                  <p className="muted" style={{ margin: 0 }}>
                    {a.mission}
                  </p>
                  <div className="agent-stats">
                    <span>
                      <strong>{fmtInt(s?.steps ?? 0)}</strong> steps
                    </span>
                    <span>
                      avg <strong>{fmtMs(s?.avg_ms)}</strong>
                    </span>
                    {failed ? (
                      <span className="attn danger">
                        <IconDot /> <strong>{fmtInt(failed)}</strong> failed
                      </span>
                    ) : (
                      <span>
                        <strong>{fmtInt(failed)}</strong> failed
                      </span>
                    )}
                    {s?.last_step_at && <span>last {fmtTime(s.last_step_at)}</span>}
                  </div>
                  <div className="agent-tags">
                    {a.node_ids?.length ? `Hops: ${a.node_ids.map(nodeLabel).join(", ")}` : "Runs on request"}
                    {"; "}
                    {a.criticality === "fail_closed" ? "an error stops the run" : "an error fails only its step"}
                    {a.mcp?.length ? `; ${a.mcp.length} tool connection${a.mcp.length === 1 ? "" : "s"} declared` : ""}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
        <div className="panel">
          <div className="panel-head">
            <h3>Live and recent runs</h3>
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
            {runs === null && !runsFailed && (
              <div className="skeleton-rows" aria-hidden="true">
                {Array.from({ length: 6 }, (_, i) => (
                  <span key={i} className="skeleton" />
                ))}
              </div>
            )}
            {runs !== null && runs.length === 0 && (
              <div className="empty">
                No runs yet. Launch the rain storm from <Link to="/">Mission control</Link>.
              </div>
            )}
            {(runs || []).slice(0, 15).map((r) => (
              <div key={r.id} className="row" style={{ cursor: "default" }}>
                {/* A success is the normal outcome, so it is a word; only the other states are chips. */}
                {String(r.status).toUpperCase() === "SUCCEEDED" ? (
                  <span className="muted">{runStatusWord(r.status)}</span>
                ) : (
                  <span className={runChipClass(r.status)}>{runStatusWord(r.status)}</span>
                )}
                <div>
                  <div>
                    <strong>{humanGraph(r.graph_name)}</strong> <span className="muted">{humanEnum(r.trigger)}</span>
                  </div>
                  <div className="muted">
                    {r.current_node ? `at ${nodeLabel(r.current_node)}, ` : ""}
                    {r.steps?.length ?? 0} steps
                  </div>
                  {/* A failed run keeps current_node at the node that broke; error_summary says why.
                      Without it the operator sees FAILED and has to open the DB to learn anything. */}
                  <RunError status={r.status} summary={r.error_summary} />
                </div>
                <span className="muted dim">{fmtTime(r.started_at)}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
