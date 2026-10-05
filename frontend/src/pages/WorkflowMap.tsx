import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { LIFECYCLE_NODES, agentDisplayName, fmtInt, fmtMs, humanAutonomy, nodeDoes, nodeLabel } from "../lib/agents";
import { fibreColour, fibreOf } from "../lib/fibre";
import "./WorkflowMap.css";

/**
 * The training view of the lifecycle: what each step does, how long a person spends on it by
 * hand (the operator profile's estimate) and how often the agents have run it. New joiners read
 * this before their first storm. The twelve steps hang from one line in five phases; the step
 * numbers are neutral and nothing is lit, because a lit step would show work that never ran.
 * Each step wears its agent's fibre colour (identity only, lib/fibre.ts).
 */

/** The phases an alarm goes through, in order: the steps each one holds. */
const PHASES: Array<{ title: string; nodes: string[] }> = [
  { title: "Take the alarm in", nodes: ["INGEST", "CORRELATE"] },
  { title: "Work out what it is", nodes: ["ENRICH", "SEVERITY"] },
  { title: "Open and assign the ticket", nodes: ["TICKET", "ASSIGN"] },
  { title: "A person decides", nodes: ["HITL"] },
  { title: "Tell people and keep the record", nodes: ["BROADCAST", "EXEC_BRIEF", "LEDGER"] },
  { title: "Follow up", nodes: ["RECURRENCE", "MONITOR"] },
];

export default function WorkflowMap({ profile }: { profile: any }) {
  const [p, setP] = useState<any | null>(null);
  useEffect(() => {
    api.productivity(0).then(setP).catch(() => undefined);
  }, []);
  // The agent each step belongs to: the rollup's when it has one, else the built-in list.
  const agentOf = useMemo(() => {
    const out: Record<string, string> = {};
    for (const n of LIFECYCLE_NODES) out[n.id] = n.agent;
    for (const n of p?.steps?.by_node || []) if (n?.node && n.agent) out[n.node] = n.agent;
    return out;
  }, [p]);
  const toil: Record<string, number> = p?.toil?.assumptions?.toil_minutes || {};
  const byNode: Record<string, any> = {};
  for (const n of p?.steps?.by_node || []) byNode[n.node] = n;
  const autonomy = humanAutonomy(profile?.autonomy_level);
  const byHand = Object.entries(toil)
    .filter(([k]) => k !== "HITL")
    .reduce((sum, [, v]) => sum + (Number(v) || 0), 0);

  return (
    <div className="wm">
      <div className="page-head">
        <div>
          <h1>Workflow map</h1>
          <p className="lead">Every alarm's twelve steps, in the order the agents take them. At {autonomy}, P1 and P2 messages wait for a person.</p>
        </div>
      </div>

      <dl className="wm-figures">
        <div>
          <dt>Steps per alarm</dt>
          <dd>12</dd>
        </div>
        <div>
          <dt>By hand, per alarm</dt>
          <dd>{p ? `${fmtInt(Math.round(byHand))} min` : "—"}</dd>
        </div>
        <div>
          <dt>Agents, alarm to ticket (median)</dt>
          <dd>{p?.pipeline_ms?.median != null ? fmtMs(p.pipeline_ms.median) : "—"}</dd>
        </div>
        <div>
          <dt>Alarms through so far</dt>
          <dd>{p?.alarms ? fmtInt(p.alarms.processed) : "—"}</dd>
        </div>
      </dl>

      <div className="panel wm-panel">
        <ol className="wm-phases">
          {PHASES.map((phase) => (
            <li key={phase.title} className={"wm-phase" + (phase.nodes.includes("HITL") ? " human" : "")}>
              <h2 className="wm-phase-title">{phase.title}</h2>
              <ol className="wm-steps">
                {phase.nodes.map((id) => {
                  const f = fibreOf(id);
                  const human = id === "HITL";
                  const stat = byNode[id];
                  return (
                    <li key={id} className={"wm-step" + (human ? " human" : "")}>
                      <span className="wm-n" aria-hidden="true">
                        {f?.n}
                      </span>
                      <div className="wm-main">
                        <div className="wm-head">
                          <h3>
                            <span className="sr-only">Step {f?.n}: </span>
                            {nodeLabel(id)}
                          </h3>
                          <span className="wm-agent">
                            {f && (
                              <span
                                className={"wm-fibre" + (f.outlined ? " outlined" : "")}
                                style={{ background: fibreColour(f) }}
                                title={`${f.colour}, fibre ${f.n}`}
                              />
                            )}
                            {agentDisplayName(agentOf[id])}
                          </span>
                        </div>
                        <p className="wm-does">{nodeDoes(id)}</p>
                        {human && (
                          <p className="wm-human-note">
                            At {autonomy} a P1 or P2 message waits here for a named person; a P3 or P4 one goes on its own.
                          </p>
                        )}
                      </div>
                      <dl className="wm-nums">
                        <div>
                          <dt>By hand</dt>
                          <dd>{human ? "stays human" : toil[id] != null ? `${toil[id]} min` : "—"}</dd>
                        </div>
                        <div>
                          <dt>Runs</dt>
                          <dd>{fmtInt(stat?.steps ?? 0)}</dd>
                        </div>
                        <div>
                          <dt>Average</dt>
                          <dd>{fmtMs(stat?.avg_ms)}</dd>
                        </div>
                      </dl>
                    </li>
                  );
                })}
              </ol>
            </li>
          ))}
        </ol>
        <p className="wm-note">
          By hand: the floor's own estimate of a person's minutes per step, not a stopwatch study. The Showcase multiplies
          it by the steps the agents completed.
        </p>
      </div>
    </div>
  );
}
