import { useEffect, useState } from "react";
import { api } from "../api";
import { agentDisplayName, fmtInt, fmtMs } from "../lib/agents";
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

export default function Agents({ tick = 0 }: { tick?: number }) {
  const [agents, setAgents] = useState<any[]>([]);
  const [runs, setRuns] = useState<any[]>([]);
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
    api.runs().then(setRuns).catch(console.error);
    const id = window.setInterval(() => {
      api.runs().then(setRuns).catch(() => undefined);
    }, 4000);
    return () => window.clearInterval(id);
    // `tick` is the debounced `runs` slice revision: a run start/finish refreshes
    // this list at once, and the 4 s poll stays as the WS-down fallback.
  }, [tick]);

  return (
    <div>
      <div className="hero">
        <div>
          <h1>Agent Observatory</h1>
          <p className="lead">
            Twelve agents under one supervisor, each with a mission, a criticality and the tools it is allowed
            to call. The counts are everything on record; during a storm the run list flips to RUNNING, WAITING
            HITL and SUCCEEDED as each alarm is processed.
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
                    <span style={failed ? { color: "#ffb4c0" } : undefined}>
                      <strong>{fmtInt(failed)}</strong> failed
                    </span>
                    {s?.last_step_at && <span>last {fmtTime(s.last_step_at)}</span>}
                  </div>
                  <div className="agent-tags">
                    <span className="status-pill">{a.status || "ready"}</span>
                    <span className="chip">{a.criticality === "fail_closed" ? "stops the run on error" : "run continues on error"}</span>
                    {a.node_ids?.length ? <span className="chip">{a.node_ids.join(", ")}</span> : <span className="chip">on request</span>}
                    {a.mcp?.length ? <span className="chip">{a.mcp.length} tool connections declared</span> : null}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
        <div className="panel">
          <h3>Live / recent runs (EAT)</h3>
          <div className="list">
            {runs.length === 0 && (
              <div className="empty">No runs yet — launch the rain storm from Mission Control.</div>
            )}
            {runs.slice(0, 15).map((r) => (
              <div key={r.id} className="row" style={{ cursor: "default" }}>
                <span className={runChip(r.status)}>{r.status}</span>
                <div>
                  <div>
                    <strong>{r.graph_name}</strong>
                  </div>
                  <div className="muted">
                    {r.trigger} · node {r.current_node || "—"} · steps {r.steps?.length ?? "?"}
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
