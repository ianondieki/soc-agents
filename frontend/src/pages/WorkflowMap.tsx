import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import AgentRail from "../components/AgentRail";
import { LIFECYCLE_NODES, agentDisplayName, fmtInt, fmtMs } from "../lib/agents";

/**
 * The training view of the lifecycle: the rail with every hop lit, and under it what each
 * hop does, how long a person spends on it by hand (the operator profile's estimate) and
 * when it stops for one. New joiners read this before their first storm.
 */
const WHAT: Record<string, string> = {
  INGEST: "Normalise the alarm and fingerprint it (site, alarm code, domain).",
  CORRELATE: "Fold a repeat or a child site into the open ticket or its parent HUB.",
  ENRICH: "Site catalogue: region, RNIO, FE on call, subscribers affected, TT classification.",
  SEVERITY: "P4 under 50k users; P3, P2, P1 above; HUB floor P2; CORE floor P1; M-PESA corridor tag.",
  TICKET: "Allocate the INC number, fill the TT fields, write the narrative and set the SLA clocks.",
  ASSIGN: "Region × domain matrix: power to Egypro or Tetranet, fibre to Egypro Fibre, radio to the FE.",
  HITL: "Hold P1 and P2 wording for the shift. Nothing external leaves without a named person.",
  BROADCAST: "Draft and queue the RNIO, FE and MSP SMS and e-mail through the outbox.",
  EXEC_BRIEF: "Write the status brief management reads instead of phoning the NOC.",
  LEDGER: "Append the Excel shift ledger row (EAT).",
  RECURRENCE: "Count faults at this site in the window; open or update a problem record.",
  MONITOR: "Set the note-chase and SLA clocks; chase a silent vendor.",
};

export default function WorkflowMap({ profile }: { profile: any }) {
  const [p, setP] = useState<any | null>(null);
  useEffect(() => {
    api.productivity(0).then(setP).catch(() => undefined);
  }, []);
  const nodes = useMemo(
    () =>
      (p?.steps?.by_node?.length ? p.steps.by_node : LIFECYCLE_NODES).map((n: any) => ({
        id: n.node || n.id,
        label: n.label,
        agent: n.agent,
        status: n.node === "HITL" || n.id === "HITL" ? "waiting_hitl" : "succeeded",
      })),
    [p]
  );
  const toil = p?.toil?.assumptions?.toil_minutes || {};
  const byNode: Record<string, any> = {};
  for (const n of p?.steps?.by_node || []) byNode[n.node] = n;

  return (
    <div>
      <h1 className="page-title">Workflow Map</h1>
      <p className="muted">
        The twelve hops every alarm takes, in order. Autonomy {String(profile?.autonomy_level || "L2_GUARDED").replace("_", " ")}:
        the gate holds P1 and P2 external broadcasts for a person.
      </p>
      <div className="panel" style={{ marginBottom: "1rem" }}>
        <AgentRail steps={null} nodes={nodes} caption="The lifecycle" />
      </div>
      <div className="panel table-scroll">
        <table>
          <thead>
            <tr>
              <th scope="col">Hop</th>
              <th scope="col">Agent</th>
              <th scope="col">What it does</th>
              <th scope="col" style={{ textAlign: "right" }}>
                By hand
              </th>
              <th scope="col" style={{ textAlign: "right" }}>
                Runs
              </th>
              <th scope="col" style={{ textAlign: "right" }}>
                Avg
              </th>
            </tr>
          </thead>
          <tbody>
            {nodes.map((n: any) => (
              <tr key={n.id}>
                <td>
                  <strong>{n.label}</strong>
                </td>
                <td className="muted">{agentDisplayName(n.agent)}</td>
                <td>{WHAT[n.id] || ""}</td>
                <td style={{ textAlign: "right", fontFamily: "var(--mono)" }}>
                  {n.id === "HITL" ? "stays human" : toil[n.id] != null ? `${toil[n.id]} min` : "—"}
                </td>
                <td style={{ textAlign: "right", fontFamily: "var(--mono)" }}>{fmtInt(byNode[n.id]?.steps ?? 0)}</td>
                <td style={{ textAlign: "right", fontFamily: "var(--mono)" }}>{fmtMs(byNode[n.id]?.avg_ms)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="muted" style={{ marginTop: "0.75rem" }}>
          "By hand" is the operator profile's estimate (<code>productivity.toil_minutes</code>), the same figure the
          Showcase page multiplies by the steps the agents completed.
        </p>
      </div>
    </div>
  );
}
