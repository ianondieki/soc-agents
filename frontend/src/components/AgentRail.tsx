import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  LIFECYCLE_NODES,
  STATUS_WORD,
  agentDisplayName,
  displaySteps,
  fmtMs,
  fmtWait,
  isLifecycleNode,
  nodeLabel,
  normaliseStatus,
  stepsByNode,
  type NodeStatus,
  type RailStep,
} from "../lib/agents";
import { parseRationale } from "../lib/audit";
import { IconAlert, IconCheck, IconPause } from "../lib/icons";

/**
 * The agent rail: one alarm's path through the twelve agents, drawn as a signal route —
 * twelve steps joined by a line, each step lit by what its agent did.
 *
 * What it gives a shift at a glance: how long each hop took (ms under every lit hop), whether
 * the run is parked on a person (the lavender hop) and what that person decided (the green
 * "approved" hop). Click a hop for the agent's reason, output and tool calls — the same step row
 * the audit trail keeps.
 *
 * Status is read from `steps` (backend step rows or WS frames normalised to that shape); pass
 * `runStatus` and the rail maps them through `displaySteps` itself (a decided Approval reads
 * "approved", a folded run's unreached hops read "skipped"), or pass steps already mapped.
 * `nodes` is optional and defaults to the registry order. A node with no step is pending; while
 * a run is live the first pending hop after the last done one is drawn as running so the eye has
 * somewhere to rest. Reduced motion and quiet mode stop the pulse.
 *
 * Each step button's accessible name is its visible text with no aria-label, so the name always
 * contains what the eye reads. It is read in the order a person needs it: the step, then its state
 * ("done, 12 ms", "waiting for a decision", "not needed"), then the agent. On screen the agent sits
 * between the label and the state (CSS `order`); in the DOM it comes last, and the only sr-only
 * text is a comma after the label plus the state word the measurement leaves unsaid.
 *
 * `not_needed` is the Approval step of a P3/P4 alarm that the autonomy level let through: a
 * neutral dot and the words "not needed", never a green check that reads as a person approving.
 */
export default function AgentRail({
  steps,
  nodes,
  runStatus,
  live = false,
  compact = false,
  selectedNode,
  onSelect,
  caption,
  layout = "route",
  loading = false,
  detailAction,
}: {
  steps: RailStep[] | null | undefined;
  nodes?: { id: string; label: string; agent: string; status?: string }[] | null;
  /** The run's own status. When given, `steps` are mapped through `displaySteps` (decided and
   *  folded hops); leave it out when the steps are already mapped. */
  runStatus?: string | null;
  /** The run is still executing (or being replayed): the next hop pulses. */
  live?: boolean;
  /** Smaller hops without the agent name — for the Mission Control panel. */
  compact?: boolean;
  selectedNode?: string | null;
  onSelect?: (nodeId: string, step: RailStep | null) => void;
  caption?: string;
  /** "route" draws the hops joined by a line (a live alarm); "grid" lays the twelve out as
   *  numbered cards with no connectors, for the training view where nothing is moving. */
  layout?: "route" | "grid";
  /** The first answer is on its way: the twelve hops as skeleton blocks of the same size, so
   *  nothing moves when the run lands. */
  loading?: boolean;
  /** A control for the open hop detail's head (the live panel's "Follow live"). */
  detailAction?: ReactNode;
}) {
  const order: readonly { id: string; label: string; agent: string; status?: string }[] = nodes && nodes.length > 0 ? nodes : LIFECYCLE_NODES;
  // One name per hop on every page: the twelve take theirs from nodeLabel(); a node outside the
  // twelve keeps the label it was given, humanised if it has none.
  const labelOf = (n: { id: string; label?: string }) => (isLifecycleNode(n.id) ? nodeLabel(n.id) : n.label || nodeLabel(n.id));
  const shown = useMemo(
    () => (runStatus !== undefined ? displaySteps({ status: runStatus, steps: steps || [] }) : steps || []),
    [steps, runStatus]
  );
  const byNode = useMemo(() => stepsByNode(shown), [shown]);
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
  const statusKey = statuses.join(",");

  useEffect(() => {
    if (selectedNode !== undefined) setInternal(null);
  }, [selectedNode]);

  // When the route wraps, the connector after the last hop on a row would point into the
  // margin; mark those hops so the CSS can hide it. A hop's text changes with its state
  // ("—", "running", "1.2 s") and can move the wrap point, so re-mark when the states or the
  // size change, and on resize.
  const trackRef = useRef<HTMLOListElement>(null);
  const markWraps = () => {
    const ol = trackRef.current;
    if (!ol) return;
    const hops = Array.from(ol.children) as HTMLElement[];
    hops.forEach((li, i) => {
      const next = hops[i + 1];
      if (next && next.offsetTop > li.offsetTop + 4) li.setAttribute("data-wrap", "1");
      else li.removeAttribute("data-wrap");
    });
  };
  useLayoutEffect(markWraps, [statusKey, compact]);
  useLayoutEffect(() => {
    const ol = trackRef.current;
    if (!ol || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(markWraps);
    ro.observe(ol);
    return () => ro.disconnect();
  }, [order.length, layout]);

  const current = selected ? byNode[selected] || null : null;
  const currentMeta = selected ? order.find((n) => n.id === selected) : null;
  const iconSize = compact ? 10 : 12;

  return (
    <div className={"rail" + (compact ? " rail-compact" : "") + (layout === "grid" ? " rail-grid" : "") + (loading ? " rail-loading" : "")}>
      <ol className="rail-track" aria-label={caption || "Agent workflow"} aria-hidden={loading || undefined} ref={trackRef}>
        {order.map((n, i) => {
          const st = statuses[i];
          const step = byNode[n.id];
          const isSel = selected === n.id;
          const conf = typeof step?.confidence === "number" ? step.confidence : null;
          const label = labelOf(n);
          const ms = step?.duration_ms != null ? fmtMs(step.duration_ms) : null;
          const timed = (st === "succeeded" || st === "failed") && ms != null;
          // The state as words for a screen reader, where the visible meta does not already say
          // it: before a measurement ("done, 12 ms"), for the pending dash, after "waiting".
          const srBefore =
            st === "succeeded" && timed ? "done, " : st === "failed" && timed ? "failed, " : st === "pending" ? (compact ? "pending" : "pending,") : "";
          // A comma before the agent name, which only the full rail shows: inside the last sr-only
          // words when there are any, else zero-size punctuation (`.sr-punct`) that adds no space.
          const srAfter = st === "waiting_hitl" ? ` for a decision${compact ? "" : ","}` : "";
          // The meta is a flex row, so the comma goes inside its last piece (a flex item is a
          // block, and a separate one would put a space before the comma).
          const comma = compact ? null : <span className="sr-punct">,</span>;
          const showConf = !compact && conf != null && st === "succeeded";
          return (
            <li key={n.id} className={`rail-hop ${st}` + (st === "decided" && step?.decision === "rejected" ? " rejected" : "") + (isSel ? " selected" : "")} data-node={n.id}>
              <button
                type="button"
                className="rail-btn"
                aria-pressed={isSel}
                tabIndex={loading ? -1 : undefined}
                disabled={loading || undefined}
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
                  {(st === "succeeded" || (st === "decided" && step?.decision !== "rejected")) && <IconCheck size={iconSize} />}
                  {st === "decided" && step?.decision === "rejected" && <IconAlert size={iconSize} />}
                  {st === "failed" && <IconAlert size={iconSize} />}
                  {st === "waiting_hitl" && <IconPause size={iconSize} />}
                </span>
                {/* The commas live inside the inline spans, so the name reads "Ingest, done, 1 ms"
                    with no space before them. */}
                <span className="rail-label">
                  {label}
                  <span className="sr-punct">,</span>
                </span>{" "}
                {/* Words in the sans; only the measurement (ms, %) is mono. */}
                <span className="rail-meta">
                  {srBefore && <span className="sr-only">{srBefore}</span>}
                  {st === "running" && <span>running{comma}</span>}
                  {st === "pending" && <span aria-hidden="true">—</span>}
                  {st === "skipped" && <span>skipped{comma}</span>}
                  {st === "not_needed" && (
                    <span>
                      {STATUS_WORD.not_needed}
                      {comma}
                    </span>
                  )}
                  {st === "waiting_hitl" && <span>waiting</span>}
                  {srAfter && <span className="sr-only">{srAfter}</span>}
                  {st === "decided" && (
                    <span>
                      {step?.decision === "rejected" ? "rejected" : "approved"}
                      {comma}
                    </span>
                  )}
                  {(st === "succeeded" || st === "failed") &&
                    (timed ? (
                      <span className="mono">
                        {ms}
                        {!showConf && comma}
                      </span>
                    ) : (
                      <span>
                        {STATUS_WORD[st]}
                        {!showConf && comma}
                      </span>
                    ))}
                  {showConf && (
                    <span className="rail-conf mono" title={`confidence ${Math.round(conf! * 100)}%`}>
                      <span className="sr-only">confidence </span>
                      {Math.round(conf! * 100)}%{comma}
                    </span>
                  )}
                </span>
                {/* Last in the DOM, so a screen reader hears the step and its state first; shown
                    between the two by `order` in styles.css. */}
                {!compact && (
                  <>
                    {" "}
                    <span className="rail-agent">{agentDisplayName(n.agent)}</span>
                  </>
                )}
              </button>
              {layout === "route" && i < order.length - 1 && <span className={`rail-link ${statuses[i + 1]}`} aria-hidden="true" />}
            </li>
          );
        })}
      </ol>
      {selected && !loading && (
        <div className="rail-detail" role="region" aria-label={`${currentMeta ? labelOf(currentMeta) : nodeLabel(selected)}: what the agent did`}>
          <div className="rail-detail-head">
            <strong>{currentMeta ? labelOf(currentMeta) : nodeLabel(selected)}</strong>
            <span className="muted">{agentDisplayName(currentMeta?.agent || current?.agent_name)}</span>
            {current?.status && <HopState step={current} />}
            {current?.duration_ms != null && <span className="muted mono">{fmtMs(current.duration_ms)}</span>}
            {detailAction && <span className="rail-detail-action">{detailAction}</span>}
          </div>
          {!current && <p className="muted">This agent has not run for this alarm yet.</p>}
          {current && normaliseStatus(current.status) === "skipped" && current.duration_ms == null && !current.rationale && (
            <p className="muted">Not needed for this alarm: the run ended before this step.</p>
          )}
          {current && normaliseStatus(current.status) === "not_needed" && (
            <p className="muted">Not needed: at this autonomy level a P3 or P4 message goes out without a person.</p>
          )}
          {current && (current.rationale || current.output_summary || current.input_summary || current.duration_ms != null) && (
            <dl className="rail-dl">
              <Why text={current.rationale} />
              <dt>Did</dt>
              <dd>{arrowsToWords(current.output_summary) || "—"}</dd>
              {current.input_summary ? (
                <>
                  <dt>Given</dt>
                  <dd>{arrowsToWords(current.input_summary)}</dd>
                </>
              ) : null}
              {typeof current.waited_ms === "number" ? (
                <>
                  <dt>Decision</dt>
                  <dd>
                    {normaliseStatus(current.status) === "waiting_hitl"
                      ? `waiting ${fmtWait(current.waited_ms)} so far`
                      : `waited ${fmtWait(current.waited_ms)} for a decision`}
                  </dd>
                </>
              ) : null}
              {typeof current.confidence === "number" ? (
                <>
                  <dt>Confidence</dt>
                  <dd className="mono">{Math.round(current.confidence * 100)}%</dd>
                </>
              ) : null}
              {Array.isArray(current.tools_called) ? (
                <>
                  <dt>Tools</dt>
                  <dd>
                    {current.tools_called.length === 0 && "none"}
                    {current.tools_called.length > 0 && (
                      <span className="rail-tools">
                        {current.tools_called.map((t, idx) => (
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
                </>
              ) : null}
            </dl>
          )}
        </div>
      )}
    </div>
  );
}

/** "users=80000→P3" reads "users=80000 to P3"; "HITL task 3f2a…" (a row id) reads as what it is. */
function arrowsToWords(text: string | null | undefined): string {
  return String(text ?? "")
    .replace(/\bHITL task [0-9a-f]{8}-[0-9a-f-]{27,}/gi, "Raised an approval card")
    .replace(/\s*→\s*/g, " to ")
    .trim();
}

/** The agent's reason: the sentence, then the key=value facts it recorded, keys humanised. */
function Why({ text }: { text: string | null | undefined }) {
  const parsed = parseRationale(text);
  const sentence = arrowsToWords(parsed.sentence);
  return (
    <>
      <dt>Why</dt>
      <dd>
        {sentence || (parsed.facts.length === 0 ? "—" : null)}
        {parsed.facts.length > 0 && (
          <span className="rail-facts">
            {parsed.facts.map((f, i) => (
              <span key={`${f.key}-${i}`}>
                <span className="rail-fact-key">{f.label}</span> {arrowsToWords(f.value)}
              </span>
            ))}
          </span>
        )}
      </dd>
    </>
  );
}

/** A step's state in the detail: plain words when routine, the drawn icon and colour when not. */
function HopState({ step }: { step: RailStep }) {
  const status = normaliseStatus(step.status);
  if (status === "waiting_hitl")
    return (
      <span className="state hitl">
        <IconPause />
        {STATUS_WORD[status]}
      </span>
    );
  if (status === "decided")
    return (
      <span className="state ok">
        <IconCheck />
        {step.decision === "rejected" ? "rejected" : "approved"}
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
