import { useId } from "react";
import { fmtMs, fmtWait, normaliseStatus, stepsByNode, type NodeStatus, type RailStep } from "../../lib/agents";
import { FIBRES, fibreColour } from "../../lib/fibre";
import { useWidth } from "./useWidth";

/**
 * The twelve agents as a fibre ribbon: twelve strands in TIA-598 colour order, one per agent,
 * drawn as an engineered sheet. An alarm enters on the first strand and descends a staircase of
 * twelve stations, one on each strand, and leaves on the last. Between Approval and Broadcast a
 * person sits on the connector: dashed when the autonomy level let the message through, violet
 * when a decision is held or was made. The latest real run lights the stations it reached, prints
 * each step's time at the right end of its strand, and sends one pulse down the staircase (the
 * page's one authored motion; static under reduced motion and Quiet mode). With a run parked on
 * a person the pulse stops at the person.
 *
 * One user unit is one pixel (the viewBox follows the measured width), so the drawing is crisp
 * and the labels keep their size at every width; under 560 px the pitch tightens.
 */

export interface RibbonRun {
  id: string;
  /** The run's steps after `displaySteps` (decided, not needed and skipped already mapped). */
  steps: RailStep[];
  status: string | null;
}

function rightText(node: string, step: RailStep | undefined, st: NodeStatus): { text: string; tone: "" | "hitl" | "danger" } {
  switch (st) {
    case "succeeded":
      return { text: fmtMs(step?.duration_ms), tone: "" };
    case "waiting_hitl":
      return node === "HITL" ? { text: fmtWait(step?.waited_ms), tone: "hitl" } : { text: "held", tone: "hitl" };
    case "decided":
      return node === "HITL" ? { text: step?.decision ?? "decided", tone: "hitl" } : { text: "released", tone: "" };
    case "not_needed":
      return { text: "not needed", tone: "" };
    case "skipped":
      return { text: "skipped", tone: "" };
    case "failed":
      return { text: "failed", tone: "danger" };
    case "running":
      return { text: "running", tone: "" };
    default:
      return { text: "", tone: "" };
  }
}

function personText(st: NodeStatus, decision: string | undefined): string {
  if (st === "waiting_hitl") return "waiting for a person";
  if (st === "decided") return `${decision ?? "decided"} by a person`;
  if (st === "not_needed") return ""; // the ms column already says "not needed"
  return "a person decides";
}

export default function FibreRibbon({ run }: { run: RibbonRun | null }) {
  const [ref, measured] = useWidth<HTMLDivElement>();
  const uid = useId();
  const W = measured || 640;
  const compact = W < 560;
  const pitch = compact ? 24 : 32;
  const labelW = compact ? 84 : 100;
  // Wide enough for the longest word the column prints ("not needed", "released") in the Mono.
  const msW = compact ? 80 : 84;
  const r = compact ? 4.5 : 5.5;
  const H = pitch * 13;
  const x0 = labelW + 12;
  const x1 = W - msW - 12;
  const stepX = (x1 - x0) / 13;
  const sx = (k: number) => x0 + k * stepX; // station k, 1..12
  const y = (i: number) => (i + 1) * pitch; // strand i, 0..11

  const by = run ? stepsByNode(run.steps) : {};
  const statusOf = (node: string): NodeStatus => (run ? normaliseStatus(by[node]?.status) : "pending");
  const hitlStatus = statusOf("HITL");
  const held = hitlStatus === "waiting_hitl";
  const personLit = held || hitlStatus === "decided";
  const failedAt = FIBRES.findIndex((f) => statusOf(f.node) === "failed");
  const personY = (y(6) + y(7)) / 2;

  // The pulse's path: along each strand to its station, down to the next; to the person when
  // the run is parked there, to the failed station when it broke, else out on the last strand.
  const last = failedAt >= 0 ? failedAt : held ? 6 : 11;
  const parts = [`M${x0} ${y(0)}`];
  for (let i = 0; i <= last; i++) {
    parts.push(`H${sx(i + 1)}`);
    if (i < last) parts.push(`V${y(i + 1)}`);
  }
  if (failedAt < 0) parts.push(held ? `V${personY}` : `H${x1}`);
  const pulsePath = parts.join(" ");

  const titleId = `${uid}-t`;
  const descId = `${uid}-d`;
  const desc = run
    ? "Twelve parallel strands, one per agent in fibre colour order, with the latest alarm's path stepping down from Ingest to Monitor and each step's time at the right."
    : "Twelve parallel strands, one per agent in fibre colour order; no alarm has run yet.";

  return (
    <div ref={ref} className="ld-rb-stage" style={{ minHeight: H }}>
      {measured > 0 && (
        <svg width="100%" height={H} viewBox={`0 0 ${W} ${H}`} role="img" aria-labelledby={`${titleId} ${descId}`}>
          <title id={titleId}>The twelve agents as a fibre ribbon</title>
          <desc id={descId}>{desc}</desc>

          {/* Connectors: the staircase, under the strands. */}
          {FIBRES.slice(0, 11).map((f, i) => (
            <line key={`c${f.n}`} x1={sx(i + 1)} x2={sx(i + 1)} y1={y(i)} y2={y(i + 1)} className="ld-rb-conn" />
          ))}

          {/* Strands: white and black get the outline underlay. */}
          {FIBRES.map((f, i) => (
            <g key={f.node}>
              {f.outlined && <line x1={x0} x2={x1} y1={y(i)} y2={y(i)} className="ld-rb-strand-outline" />}
              <line x1={x0} x2={x1} y1={y(i)} y2={y(i)} className="ld-rb-strand" style={{ stroke: fibreColour(f) }} />
            </g>
          ))}

          {/* The person between Approval and Broadcast. */}
          <g className={"ld-rb-person" + (personLit ? " lit" : "")}>
            <circle cx={sx(7)} cy={personY} r={compact ? 5.5 : 6.5} />
            {personText(hitlStatus, by["HITL"]?.decision) && (
              <text x={sx(7) + (compact ? 11 : 13)} y={personY + 4}>
                {personText(hitlStatus, by["HITL"]?.decision)}
              </text>
            )}
          </g>

          {/* The pulse: remounted for each new run so its animation starts again. */}
          {run && <path key={run.id} d={pulsePath} pathLength={1000} className="ld-rb-pulse" />}

          {/* Stations. */}
          {FIBRES.map((f, i) => {
            const st = statusOf(f.node);
            const reached = run != null && st !== "pending" && st !== "skipped";
            const fill = st === "waiting_hitl" && f.node === "HITL" ? "var(--hitl)" : st === "failed" ? "var(--danger)" : reached ? fibreColour(f) : "var(--bg)";
            const stroke = f.outlined ? "var(--fibre-outline)" : reached ? "var(--bg)" : fibreColour(f);
            return <circle key={`s${f.n}`} cx={sx(i + 1)} cy={y(i)} r={r} className="ld-rb-station" style={{ fill, stroke }} />;
          })}

          {/* Labels and the step times. */}
          {FIBRES.map((f, i) => {
            const st = statusOf(f.node);
            const right = run ? rightText(f.node, by[f.node], st) : { text: "", tone: "" as const };
            return (
              <g key={`l${f.n}`}>
                <text x={labelW} y={y(i) + 4} textAnchor="end" className="ld-rb-label">
                  {f.label}
                </text>
                <text x={W} y={y(i) + 4} textAnchor="end" className={"ld-rb-ms" + (right.tone ? ` ${right.tone}` : "")}>
                  {right.text}
                </text>
              </g>
            );
          })}
        </svg>
      )}
    </div>
  );
}
