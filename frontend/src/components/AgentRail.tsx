import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
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
import { IconAlert, IconCheck, IconPause } from "../lib/icons";

/**
 * The agent rail: one alarm's path through the twelve agents, drawn as a signal route —
 * twelve hops joined by a line, each hop lit by what its agent did.
 *
 * It replaces the row of boxes the workspace used to draw. What it adds is the three
 * things a shift needs to trust an agent at a glance: how long the hop took (ms under
 * every lit hop), what it was sure of (the confidence figure; in the detail when compact)
 * and whether it is waiting for a decision (the lavender hop). Click a hop for the agent's
 * rationale, output and tool calls — the same step row the audit trail keeps.
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
  layout = "route",
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
  /** "route" draws the hops joined by a line (a live alarm); "grid" lays the twelve out as
   *  numbered cards with no connectors, for the training view where nothing is moving. */
  layout?: "route" | "grid";
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
    // A STARTED frame already marks its hop running; only when no hop is, the next pending one
    // after the last finished hop is drawn running so the eye has somewhere to rest.
    if (live && !out.includes("running")) {
      const lastDone = out.reduce((acc, s, i) => (s !== "pending" ? i : acc), -1);
      if (lastDone + 1 < out.length && out[lastDone + 1] === "pending") out[lastDone + 1] = "running";
    }
    return out;
  }, [order, byNode, live]);

  useEffect(() => {
    if (selectedNode !== undefined) setInternal(null);
  }, [selectedNode]);

  // When the route wraps, the connector after the last hop on a row would point into the
  // margin; mark those hops so the CSS can hide it. Re-run on resize.
  const trackRef = useRef<HTMLOListElement>(null);
  useLayoutEffect(() => {
    const ol = trackRef.current;
    if (!ol) return;
    const mark = () => {
      const hops = Array.from(ol.children) as HTMLElement[];
      hops.forEach((li, i) => {
        const next = hops[i + 1];
        if (next && next.offsetTop > li.offsetTop + 4) li.setAttribute("data-wrap", "1");
        else li.removeAttribute("data-wrap");
      });
    };
    mark();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(mark);
    ro.observe(ol);
    return () => ro.disconnect();
  }, [order.length, layout]);

  const current = selected ? byNode[selected] || null : null;
  const currentMeta = selected ? order.find((n) => n.id === selected) : null;

  return (
    <div className={"rail" + (compact ? " rail-compact" : "") + (layout === "grid" ? " rail-grid" : "")}>
      <ol className="rail-track" aria-label={caption || "Agent workflow"} ref={trackRef}>
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
                {layout === "grid" && (
                  <span className="rail-n" aria-hidden="true">
                    {i + 1}
                  </span>
                )}
                <span className="rail-dot" aria-hidden="true">
                  {st === "succeeded" && <IconCheck size={compact ? 10 : 12} />}
                  {st === "failed" && <IconAlert size={compact ? 10 : 12} />}
                  {st === "waiting_hitl" && <IconPause size={compact ? 10 : 12} />}
                </span>
                <span className="rail-label">{n.label}</span>
                {!compact && <span className="rail-agent">{agentDisplayName(n.agent)}</span>}
                <span className="rail-meta" title={st === "waiting_hitl" ? STATUS_WORD[st] : undefined}>
                  {st === "running" && "running"}
                  {st === "pending" && "—"}
                  {st === "skipped" && "skipped"}
                  {st === "waiting_hitl" && "waiting"}
                  {(st === "succeeded" || st === "failed") && (step?.duration_ms != null ? fmtMs(step.duration_ms) : STATUS_WORD[st])}
                  {!compact && conf != null && st === "succeeded" && (
                    <span className="rail-conf" title={`confidence ${Math.round(conf * 100)}%`}>
                      {Math.round(conf * 100)}%
                    </span>
                  )}
                </span>
              </button>
              {layout === "route" && i < order.length - 1 && <span className={`rail-link ${statuses[i + 1]}`} aria-hidden="true" />}
            </li>
          );
        })}
      </ol>
      {selected && (
        <div className="rail-detail" role="region" aria-live="polite">
          <div className="rail-detail-head">
            <strong>{currentMeta?.label || selected}</strong>
            <span className="muted">{agentDisplayName(currentMeta?.agent || current?.agent_name)}</span>
            {current?.status && <HopState status={normaliseStatus(current.status)} />}
            {current?.duration_ms != null && <span className="muted mono">{fmtMs(current.duration_ms)}</span>}
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
              {typeof current.confidence === "number" ? (
                <>
                  <dt>Confidence</dt>
                  <dd className="mono">{Math.round(current.confidence * 100)}%</dd>
                </>
              ) : null}
              <dt>Tools</dt>
              <dd>
                {(current.tools_called || []).length === 0 && "none"}
                {(current.tools_called || []).length > 0 && (
                  <span className="rail-tools">
                    {(current.tools_called || []).map((t, idx) => (
                      <span key={idx}>
                        <span className="mono">{t.name || "tool"}</span>
                        {typeof t.latency_ms === "number" && <span className="muted mono"> {t.latency_ms} ms</span>}
                        {t.ok === false && (
                          <>
                            {" "}
                            <span className="state danger">
                              <IconAlert />
                              failed
                            </span>
                          </>
                        )}
                      </span>
                    ))}
                  </span>
                )}
              </dd>
            </dl>
          )}
        </div>
      )}
    </div>
  );
}

/** A hop's state in the detail: plain words when routine, the drawn icon and colour when not. */
function HopState({ status }: { status: NodeStatus }) {
  if (status === "waiting_hitl")
    return (
      <span className="state hitl">
        <IconPause />
        {STATUS_WORD[status]}
      </span>
    );
  if (status === "failed")
    return (
      <span className="state danger">
        <IconAlert />
        {STATUS_WORD[status]}
      </span>
    );
  return <span className="muted">{STATUS_WORD[status]}</span>;
}
