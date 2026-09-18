import { useEffect, useState } from "react";
import { api } from "../api";
import { fmtTime } from "../lib/time";

export default function Agents({ tick = 0 }: { tick?: number }) {
  const [agents, setAgents] = useState<any[]>([]);
  const [runs, setRuns] = useState<any[]>([]);

  useEffect(() => {
    api.agents().then(setAgents).catch(console.error);
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
            Supervisor–worker agents that replace NOC toil. During a live storm you will see runs flip to
            RUNNING / WAITING_HITL / SUCCEEDED as each alarm is processed.
          </p>
        </div>
      </div>
      <div className="grid-2">
        <div className="panel">
          <h3>Agent roster</h3>
          <div className="grid-2" style={{ gridTemplateColumns: "1fr 1fr" }}>
            {agents.map((a) => (
              <div key={a.name} className="agent-card">
                <h4>{a.name}</h4>
                <p className="muted" style={{ margin: 0 }}>
                  {a.mission}
                </p>
                <span className="status-pill">{a.status || "ready"}</span>
              </div>
            ))}
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
                <span className={`chip ${r.status === "RUNNING" || r.status === "WAITING_HITL" ? "accent" : "ok"}`}>
                  {r.status}
                </span>
                <div>
                  <div>
                    <strong>{r.graph_name}</strong>
                  </div>
                  <div className="muted">
                    {r.trigger} · node {r.current_node || "—"} · steps {r.steps?.length ?? "?"}
                  </div>
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
