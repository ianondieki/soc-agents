import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { LIFECYCLE_NODES, agentDisplayName, fmtInt, fmtMs, humanAutonomy, nodeDoes, nodeLabel } from "../lib/agents";

/**
 * The training view of the lifecycle: what each hop does, how long a person spends on it by
 * hand (the operator profile's estimate) and how often the agents have run it. New joiners read
 * this before their first storm. There is no rail here: a rail with every hop lit would show
 * work that never ran, and the table already lists the hops in order.
 */

export default function WorkflowMap({ profile }: { profile: any }) {
  const [p, setP] = useState<any | null>(null);
  useEffect(() => {
    api.productivity(0).then(setP).catch(() => undefined);
  }, []);
  const nodes = useMemo(
    () =>
      (p?.steps?.by_node?.length ? p.steps.by_node : LIFECYCLE_NODES).map((n: any) => ({
        id: n.node || n.id,
        agent: n.agent,
      })),
    [p]
  );
  const toil = p?.toil?.assumptions?.toil_minutes || {};
  const byNode: Record<string, any> = {};
  for (const n of p?.steps?.by_node || []) byNode[n.node] = n;

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Workflow map</h1>
          <p className="lead">
            Every alarm's twelve steps. At {humanAutonomy(profile?.autonomy_level)}, P1 and P2 messages wait for a person.
          </p>
        </div>
      </div>
      <div className="panel stack">
        <div className="table-scroll table-wide">
          <table>
            <thead>
              <tr>
                <th scope="col">Step</th>
                <th scope="col" className="col-agent">
                  Agent
                </th>
                <th scope="col" className="col-what">
                  What it does
                </th>
                <th scope="col" className="num">
                  By hand
                </th>
                <th scope="col" className="num">
                  Runs
                </th>
                <th scope="col" className="num">
                  Avg
                </th>
              </tr>
            </thead>
            <tbody>
              {nodes.map((n: any) => (
                <tr key={n.id}>
                  <td>
                    <strong>{nodeLabel(n.id)}</strong>
                    <p className="hop-what phone-only">{nodeDoes(n.id)}</p>
                  </td>
                  <td className="muted col-agent">{agentDisplayName(n.agent)}</td>
                  <td className="col-what">{nodeDoes(n.id)}</td>
                  <td className="num">
                    {n.id === "HITL" ? (
                      // Words, not a measurement: the sans, not the cell's mono.
                      <span className="muted" style={{ fontFamily: "var(--font)" }}>
                        stays human
                      </span>
                    ) : toil[n.id] != null ? (
                      `${toil[n.id]} min`
                    ) : (
                      "—"
                    )}
                  </td>
                  <td className="num">{fmtInt(byNode[n.id]?.steps ?? 0)}</td>
                  <td className="num">{fmtMs(byNode[n.id]?.avg_ms)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="muted">
          By hand: the floor's own estimate of a person's minutes per step, not a stopwatch study. The Showcase
          multiplies it by the steps the agents completed.
        </p>
      </div>
    </div>
  );
}
