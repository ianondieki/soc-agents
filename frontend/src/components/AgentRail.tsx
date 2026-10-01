import { useEffect, useMemo, useState } from "react";
import {
  LIFECYCLE_NODES,
  STATUS_WORD,
  agentDisplayName,
  fmtMs,
  normaliseStatus,
  stepsByNode,
  type NodeStatus,
  type RailStep,
} from "../lib/agents";

/**
 * The agent rail: one alarm's path through the twelve agents, drawn as a signal route —
 * twelve hops joined by a line, each hop lit by what its agent did.
 *
 * It replaces the row of boxes the workspace used to draw. What it adds is the three
 * things a shift needs to trust an agent at a glance: how long the hop took (ms under
 * every lit hop), what it was sure of (the confidence tick) and whether it is waiting on
 * a person (the lavender hop). Click a hop for the agent's rationale, output and tool
 * calls — the same step row the audit trail keeps.
 *
 * Status is read from `steps` (backend step rows or WS step events normalised to that
 * shape); `nodes` is optional and defaults to the registry order. A node with no step is
 * pending; while a run is live the first pending hop after the last done one is drawn as
 * running so the eye has somewhere to rest. Reduced motion and quiet mode stop the sweep.
 */
export default function AgentRail({
  steps,
  nodes,
  live = false,
  compact = false,
  selectedNode,
  onSelect,
  caption,
}: {
  steps: RailStep[] | null | undefined;
  nodes?: { id: string; label: string; agent: string; status?: string }[] | null;
  /** The run is still executing: the next hop pulses. */
  live?: boolean;
  /** Smaller hops without the agent name — for the Mission Control panel. */
  compact?: boolean;
  selectedNode?: string | null;
  onSelect?: (nodeId: string, step: RailStep | null) => void;
  caption?: string;
}) {
  const order = nodes && nodes.length > 0 ? nodes : (LIFECYCLE_NODES as { id: string; label: string; agent: string; status?: string }[]);
  const byNode = useMemo(() => stepsByNode(steps), [steps]);
  const [internal, setInternal] = useState<string | null>(null);
  const selected = selectedNode === undefined ? internal : selectedNode;

  // The hop after the last completed one is "running" while the run is live.
  const statuses: NodeStatus[] = useMemo(() => {
    const out: NodeStatus[] = order.map((n) => {
      const step = byNode[n.id];
      if (step) return normaliseStatus(step.status);
      return normaliseStatus(n.status);
    });
    if (live) {
      const lastDone = out.reduce((acc, s, i) => (s !== "pending" ? i : acc), -1);
      if (lastDone + 1 < out.length && out[lastDone + 1] === "pending") out[lastDone + 1] = "running";
    }
    return out;
  }, [order, byNode, live]);

  useEffect(() => {
    if (selectedNode !== undefined) setInternal(null);
  }, [selectedNode]);

  const current = selected ? byNode[selected] || null : null;
  const currentMeta = selected ? order.find((n) => n.id === selected) : null;

  return (
    <div className={"rail" + (compact ? " rail-compact" : "")}>
      <ol className="rail-track" aria-label={caption || "Agent workflow"}>
        {order.map((n, i) => {
          const st = statuses[i];
          const step = byNode[n.id];
          const isSel = selected === n.id;
          const conf = typeof step?.confidence === "number" ? step.confidence : null;
          return (
            <li key={n.id} className={`rail-hop ${st}` + (isSel ? " selected" : "")} data-node={n.id}>
              <button
                type="button"
                className="rail-btn"
                aria-pressed={isSel}
                aria-label={`${n.label}: ${STATUS_WORD[st]}${step?.duration_ms != null ? `, ${fmtMs(step.duration_ms)}` : ""}`}
                onClick={() => {
                  const next = isSel ? null : n.id;
                  if (selectedNode === undefined) setInternal(next);
                  onSelect?.(n.id, step || null);
                }}
              >
                <span className="rail-dot" aria-hidden="true">
                  {st === "succeeded" && "✓"}
                  {st === "failed" && "!"}
                  {st === "waiting_hitl" && "⏸"}
                </span>
                <span className="rail-label">{n.label}</span>
                {!compact && <span className="rail-agent">{agentDisplayName(n.agent)}</span>}
                <span className="rail-meta">
                  {st === "running" && "running"}
                  {st === "pending" && "—"}
                  {st === "waiting_hitl" && "human"}
                  {(st === "succeeded" || st === "failed") && (step?.duration_ms != null ? fmtMs(step.duration_ms) : STATUS_WORD[st])}
                  {conf != null && st === "succeeded" && (
                    <span className="rail-conf" title={`confidence ${Math.round(conf * 100)}%`}>
                      {Math.round(conf * 100)}%
                    </span>
                  )}
                </span>
              </button>
              {i < order.length - 1 && <span className={`rail-link ${statuses[i + 1]}`} aria-hidden="true" />}
            </li>
          );
        })}
      </ol>
      {selected && (
        <div className="rail-detail" role="region" aria-live="polite">
          <div className="rail-detail-head">
            <strong>{currentMeta?.label || selected}</strong>
            <span className="muted">{agentDisplayName(currentMeta?.agent || current?.agent_name)}</span>
            {current?.status && <span className={`chip ${chipFor(normaliseStatus(current.status))}`}>{STATUS_WORD[normaliseStatus(current.status)]}</span>}
            {current?.duration_ms != null && <span className="chip">{fmtMs(current.duration_ms)}</span>}
          </div>
          {!current && <p className="muted">This agent has not run for this alarm yet.</p>}
          {current && (
            <dl className="rail-dl">
              <dt>Why</dt>
              <dd>{current.rationale || "—"}</dd>
              <dt>Did</dt>
              <dd>{current.output_summary || "—"}</dd>
              {current.input_summary ? (
                <>
                  <dt>Given</dt>
                  <dd>{current.input_summary}</dd>
                </>
              ) : null}
              <dt>Tools</dt>
              <dd>
                {(current.tools_called || []).length === 0 && "none"}
                {(current.tools_called || []).map((t, idx) => (
                  <span key={idx} className={"chip " + (t.ok === false ? "danger" : "")} style={{ marginRight: "0.3rem" }}>
                    {t.name || "tool"}
                    {typeof t.latency_ms === "number" ? ` ${t.latency_ms} ms` : ""}
                    {t.ok === false ? " failed" : ""}
                  </span>
                ))}
              </dd>
            </dl>
          )}
        </div>
      )}
    </div>
  );
}

function chipFor(st: NodeStatus): string {
  if (st === "succeeded") return "ok";
  if (st === "failed") return "danger";
  if (st === "waiting_hitl") return "hitl";
  if (st === "running") return "accent";
  return "";
}
