import { useEffect, useId, useState } from "react";
import { LIFECYCLE_NODES, fmtMs, fmtWait, normaliseStatus, stepsByNode, type NodeStatus, type RailStep } from "../../lib/agents";
import { useQuietMode } from "../../realtime/RealtimeContext";
import { useWidth } from "./useWidth";

/**
 * The twelve agents on a clock face, for a NOC that runs on shifts. Ingest sits at 12, the
 * pipeline runs clockwise, and Approval, the one step a person owns, anchors the bottom at 6.
 *
 * A hairline track carries the latest real run: an accent arc sweeps from 12 o'clock to the
 * last step that completed and each node lights as the arc reaches it (the page's one authored
 * motion; it replays for a new run and every few seconds, and is static under reduced motion and
 * Quiet mode). A run parked on a person stops the arc at 6 o'clock, where the Approval node
 * pulses in violet under a small person. Inside the ring, a bar from each node points at the
 * centre, as long as the share of alarms that agent has handled on record: Ingest and Correlate
 * see every alarm, the rest only those that became tickets. The centre holds the result: the
 * ticket number, the time the agents took, and the one state that matters.
 *
 * One SVG unit is one CSS pixel (the viewBox follows the measured width, capped), so labels keep
 * the size the stylesheet gives them at every width. Under 440 px the bars and the per-step
 * times go, so the labels keep their room.
 */

export interface DialRun {
  id: string;
  /** The run's steps after `displaySteps` (decided, not needed and skipped already mapped). */
  steps: RailStep[];
  status: string | null;
}

export interface DialProps {
  run: DialRun | null;
  /** Steps handled per node on record (`productivity.steps.by_node[].steps`); null draws no bars. */
  counts: Record<string, number> | null;
  ticket: string | null;
  tookMs: number | null;
  /** The runs loaded and there is none: an honest empty centre. */
  empty: boolean;
}

/** A full sweep (12 o'clock to Monitor at 11) takes this long; shorter arcs keep the same pace. */
const SWEEP_FULL_MS = 1600;
const REST_MS = 4000;
const MAX_SIZE = 520;
const FULL_DEG = 330;
const RAD = Math.PI / 180;

function pt(cx: number, cy: number, r: number, deg: number): { x: number; y: number } {
  return { x: cx + r * Math.sin(deg * RAD), y: cy - r * Math.cos(deg * RAD) };
}

type Kind = "done" | "waiting" | "hollow" | "failed" | "pending";

function kindOf(node: string, st: NodeStatus): Kind {
  if (st === "succeeded" || st === "decided") return "done";
  if (st === "failed") return "failed";
  if (st === "not_needed" || st === "skipped") return "hollow";
  if (st === "waiting_hitl") return node === "HITL" ? "waiting" : "pending";
  return "pending";
}

function usePrefersReducedMotion(): boolean {
  const query = "(prefers-reduced-motion: reduce)";
  const [reduced, setReduced] = useState(() => {
    try {
      return window.matchMedia(query).matches;
    } catch {
      return false;
    }
  });
  useEffect(() => {
    let mq: MediaQueryList;
    try {
      mq = window.matchMedia(query);
    } catch {
      return;
    }
    const on = () => setReduced(mq.matches);
    mq.addEventListener?.("change", on);
    return () => mq.removeEventListener?.("change", on);
  }, []);
  return reduced;
}

export default function AgentDial({ run, counts, ticket, tookMs, empty }: DialProps) {
  const [ref, measured] = useWidth<HTMLDivElement>();
  const uid = useId();
  const quiet = useQuietMode();
  const reduced = usePrefersReducedMotion();
  const calm = quiet || reduced;

  const S = Math.min(measured || MAX_SIZE, MAX_SIZE);
  const compact = S < 440;
  // Room for the longest label ("Shift ledger") at 3 and 9 o'clock, and for two text lines
  // above and below the ring; the stage is as tall as the ring needs, not as tall as it is wide.
  const labelPad = compact ? 80 : 96;
  const cx = S / 2;
  const R = Math.max(40, cx - labelPad);
  const nodeR = compact ? 5 : 6;

  const by = run ? stepsByNode(run.steps) : {};
  const nodes = LIFECYCLE_NODES.map((n, i) => {
    const st: NodeStatus = run ? normaliseStatus(by[n.id]?.status) : "pending";
    return { id: n.id, label: n.label, deg: i * 30, st, kind: kindOf(n.id, st), step: by[n.id] as RailStep | undefined };
  });

  // The arc runs to the last step that completed; it passes a "not needed" Approval, and stops
  // on a held one or a failure.
  let endIdx = -1;
  for (const [i, n] of nodes.entries()) {
    if (n.kind === "done" || (n.kind === "hollow" && n.st === "not_needed")) {
      endIdx = i;
      continue;
    }
    if (n.kind === "waiting" || n.kind === "failed") endIdx = i;
    break;
  }
  const endDeg = endIdx * 30;
  const approval = nodes[6];
  const held = approval.kind === "waiting";
  const vPad = (compact ? 46 : 58) + (held ? 20 : 0);
  const H = Math.round(2 * (R + vPad));
  const cy = H / 2;
  const c = cx; // the x centre; `cy` is the y centre
  const failedAt = nodes.find((n) => n.kind === "failed") ?? null;
  const sweepMs = Math.round((SWEEP_FULL_MS * Math.max(0, endDeg)) / FULL_DEG);
  const animate = !calm && run != null && endDeg > 0;

  // The replay: the animated group is keyed by run and cycle, so a new run id, or the next
  // cycle after the rest, mounts it afresh and its CSS animations start again.
  const [cycle, setCycle] = useState(0);
  useEffect(() => {
    if (!animate) return;
    const id = window.setInterval(() => setCycle((n) => n + 1), sweepMs + REST_MS);
    return () => window.clearInterval(id);
  }, [animate, sweepMs, run?.id]);

  const arcEnd = pt(c, cy, R, endDeg);
  const arcD = endDeg > 0 ? `M${c} ${cy - R} A${R} ${R} 0 ${endDeg > 180 ? 1 : 0} 1 ${arcEnd.x} ${arcEnd.y}` : "";

  const maxCount = counts ? Math.max(0, ...Object.values(counts).map((v) => Number(v) || 0)) : 0;
  const clear = 84; // the centre's text needs this much radius free
  const barMax = R - nodeR - 8 - clear;
  const showBars = !!counts && maxCount > 0 && !compact && barMax > 16;

  const stateLine: { text: string; tone: "" | "hitl" | "danger" } = held
    ? { text: "waiting for a person", tone: "hitl" }
    : failedAt
      ? { text: `failed at ${failedAt.label}`, tone: "danger" }
      : approval.st === "decided"
        ? { text: `${approval.step?.decision ?? "decided"} by a person`, tone: "hitl" }
        : approval.st === "not_needed"
          ? { text: "no approval needed", tone: "" }
          : { text: "", tone: "" };

  const msOf = (n: (typeof nodes)[number]): { text: string; tone: "" | "hitl" } => {
    switch (n.st) {
      case "succeeded":
      case "decided":
        return { text: n.id === "HITL" && n.st === "decided" ? n.step?.decision ?? "decided" : fmtMs(n.step?.duration_ms), tone: "" };
      case "waiting_hitl":
        return n.id === "HITL" ? { text: fmtWait(n.step?.waited_ms), tone: "hitl" } : { text: "held", tone: "hitl" };
      case "not_needed":
        return { text: "not needed", tone: "" };
      case "failed":
        return { text: "failed", tone: "" };
      default:
        return { text: "", tone: "" };
    }
  };

  const titleId = `${uid}-t`;
  const descId = `${uid}-d`;
  const desc = run
    ? `${ticket ?? "The latest ticket"}: the agents took ${fmtMs(tookMs)}. ` +
      nodes
        .map((n) => `${n.label} ${msOf(n).text || "pending"}`)
        .join(", ") +
      (stateLine.text ? `. ${stateLine.text[0].toUpperCase()}${stateLine.text.slice(1)}.` : ".")
    : empty
      ? "No alarm has been through the agents yet."
      : "Loading the latest run.";

  const groupKey = `${run?.id ?? "none"}:${cycle}`;

  return (
    <div ref={ref} className="ld-dial-stage" style={{ minHeight: H }}>
      {measured > 0 && (
        <svg
          className={"ld-dial" + (compact ? " compact" : "")}
          width={S}
          height={H}
          viewBox={`0 0 ${S} ${H}`}
          role="img"
          aria-labelledby={`${titleId} ${descId}`}
        >
          <title id={titleId}>The twelve agents on a dial, with the latest alarm's run</title>
          <desc id={descId}>{desc}</desc>

          <circle cx={c} cy={cy} r={R} className="ld-dial-track" />

          {showBars &&
            nodes.map((n) => {
              const count = Number(counts?.[n.id] || 0);
              if (count <= 0) return null;
              const len = (barMax * count) / maxCount;
              const a = pt(c, cy, R - nodeR - 4, n.deg);
              const b = pt(c, cy, R - nodeR - 4 - len, n.deg);
              return <line key={`b${n.id}`} x1={a.x} y1={a.y} x2={b.x} y2={b.y} className={"ld-dial-bar" + (n.id === "HITL" ? " hitl" : "")} />;
            })}

          <g key={groupKey}>
            {arcD && (
              <path
                d={arcD}
                pathLength={1000}
                className={"ld-dial-arc" + (animate ? " sweep" : "")}
                style={animate ? { animationDuration: `${sweepMs}ms` } : undefined}
              />
            )}
            {nodes.map((n, i) => {
              const p = pt(c, cy, R, n.deg);
              const reached = i <= endIdx;
              const lights = animate && reached;
              const delay = endDeg > 0 ? Math.round((sweepMs * n.deg) / endDeg) : 0;
              return (
                <g key={n.id}>
                  {n.kind === "waiting" && !calm && (
                    <circle cx={p.x} cy={p.y} r={nodeR + 1} className="ld-dial-halo" style={{ animationDelay: `${sweepMs + 300}ms` }} />
                  )}
                  <circle
                    cx={p.x}
                    cy={p.y}
                    r={n.kind === "waiting" ? nodeR + 1 : nodeR}
                    className={`ld-dial-node ${n.kind}${lights ? " will-light" : ""}`}
                    style={lights ? { animationDelay: `${delay}ms` } : undefined}
                  />
                </g>
              );
            })}
          </g>

          {held && (
            <g className="ld-dial-person" transform={`translate(${c} ${cy + R + (compact ? 15 : 18)})`} aria-hidden="true">
              <circle cx={0} cy={-4} r={3} />
              <path d="M-6 7a6 6 0 0 1 12 0" />
            </g>
          )}

          {nodes.map((n) => {
            const gap = compact ? 12 : 14;
            const lp = pt(c, cy, R + gap, n.deg);
            const anchor = n.deg === 0 || n.deg === 180 ? "middle" : n.deg < 180 ? "start" : "end";
            const ms = run && !compact ? msOf(n) : { text: "", tone: "" as const };
            let labelY: number;
            if (n.deg === 0) labelY = ms.text ? lp.y - 19 : lp.y - 6;
            else if (n.deg === 180) labelY = lp.y + 14 + (held ? 20 : 0);
            // At 1 and 11 o'clock the ring falls away under the label, so a two-line block hung
            // below the anchor lands its time on the ring: lift the block above the anchor instead.
            else if ((n.deg === 30 || n.deg === 330) && ms.text) labelY = lp.y - 10;
            else labelY = lp.y + 4;
            const msY = n.deg === 0 ? lp.y - 6 : labelY + 14;
            const hot = n.id === "HITL" && held;
            return (
              <g key={`l${n.id}`}>
                <text x={lp.x} y={labelY} textAnchor={anchor} className={"ld-dial-label" + (hot ? " hitl" : "")}>
                  {n.label}
                </text>
                {ms.text && (
                  <text x={lp.x} y={msY} textAnchor={anchor} className={"ld-dial-ms" + (ms.tone ? ` ${ms.tone}` : "")}>
                    {ms.text}
                  </text>
                )}
              </g>
            );
          })}

          {run ? (
            <g>
              <text x={c} y={cy + (compact ? 4 : 6)} textAnchor="middle" className="ld-dial-ticket">
                {ticket ?? "Ticket"}
              </text>
              <text x={c} y={cy + (compact ? 22 : 30)} textAnchor="middle" className="ld-dial-took">
                the agents took <tspan className="n">{fmtMs(tookMs)}</tspan>
              </text>
              {stateLine.text && (
                <text x={c} y={cy + (compact ? 38 : 50)} textAnchor="middle" className={"ld-dial-state" + (stateLine.tone ? ` ${stateLine.tone}` : "")}>
                  {stateLine.text}
                </text>
              )}
            </g>
          ) : empty ? (
            <g>
              <text x={c} y={cy + 2} textAnchor="middle" className="ld-dial-empty">
                No alarm yet
              </text>
              <text x={c} y={cy + (compact ? 18 : 22)} textAnchor="middle" className="ld-dial-took">
                launch the storm
              </text>
            </g>
          ) : null}
        </svg>
      )}
    </div>
  );
}
