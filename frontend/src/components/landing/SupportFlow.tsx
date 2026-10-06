import { useId, type ReactNode } from "react";
import { useWidth } from "./useWidth";

/**
 * How the Support desk routes a complaint, as a left-anchored tree in the ribbon's idiom: a
 * complaint arrives, triage reads it, and it goes to one of three places. The two agent routes
 * are ink; the route to a person is violet, the one colour that means "a person decides". The
 * three terminals line up with the three HTML columns under the drawing, which carry what each
 * one does and the live counts, so the words wrap like text and never shrink with the picture.
 */
export interface FlowColumn {
  name: string;
  does: string;
  /** The live count line ("12 answered"); null while unknown, so the line holds its space. */
  live: ReactNode;
}

export default function SupportFlow({ columns }: { columns: [FlowColumn, FlowColumn, FlowColumn] }) {
  const [ref, measured] = useWidth<HTMLDivElement>();
  const uid = useId();
  const W = measured || 600;
  const compact = W < 480;
  const gap = compact ? 12 : 20;
  const colW = (W - 2 * gap) / 3;
  // The spine sits on the columns' left rules (2px wide at x 0..2), so each terminal dot lands
  // exactly where its column's rule begins below the drawing.
  const x0 = 1;
  const tx = 16;
  const xs = [0, 1, 2].map((i) => i * (colW + gap) + x0);
  const yA = 8;
  const yB = 58;
  const fanY = yB + 30; // the fan leaves the spine below Triage's second line
  const yC = fanY + 52;
  const H = yC + 8;
  const titleId = `${uid}-t`;

  return (
    <div className="ld-flow">
      {/* On a phone the drawing gives way to this line and the three routes stack. */}
      <p className="ld-flow-short">A complaint arrives, triage reads it, and it goes to one of three places.</p>
      <div ref={ref} className="ld-flow-stage" style={{ minHeight: H }}>
        {measured > 0 && (
          <svg width="100%" height={H} viewBox={`0 0 ${W} ${H}`} role="img" aria-labelledby={titleId}>
            <title id={titleId}>A complaint arrives, triage reads it, and it goes to the resolver, the action agent or a person.</title>
            <line x1={x0} x2={x0} y1={yA} y2={yC} className="ld-flow-line" />
            <path d={`M${x0} ${fanY} C ${x0} ${fanY + 30}, ${xs[1]} ${yC - 26}, ${xs[1]} ${yC}`} className="ld-flow-line" />
            <path d={`M${x0} ${fanY} C ${x0} ${fanY + 30}, ${xs[2]} ${yC - 26}, ${xs[2]} ${yC}`} className="ld-flow-line hitl" />

            <circle cx={x0} cy={yA} r={4.5} className="ld-flow-dot" />
            <text x={tx} y={yA + 4} className="ld-flow-t">
              A complaint arrives
            </text>
            <text x={tx} y={yA + 20} className="ld-flow-s">
              {compact ? "web form, SMS, app, call centre or social" : "by web form, SMS, app, call centre or social; in English, Kiswahili or Sheng"}
            </text>

            <circle cx={x0} cy={yB} r={4.5} className="ld-flow-dot" />
            <text x={tx} y={yB + 4} className="ld-flow-t">
              Triage reads it
            </text>
            <text x={tx} y={yB + 20} className="ld-flow-s">
              category, urgency, sentiment, risk flags, confidence
            </text>

            <circle cx={xs[0]} cy={yC} r={4.5} className="ld-flow-dot" />
            <circle cx={xs[1]} cy={yC} r={4.5} className="ld-flow-dot" />
            <circle cx={xs[2]} cy={yC} r={5} className="ld-flow-dot hitl" />
          </svg>
        )}
      </div>
      <div className="ld-flow-cols" style={{ gap }}>
        {columns.map((c, i) => (
          <div key={c.name} className={"ld-flow-col" + (i === 2 ? " hitl" : "")}>
            <h4>{c.name}</h4>
            <p>{c.does}</p>
            <div className="ld-flow-live">{c.live}</div>
          </div>
        ))}
      </div>
    </div>
  );
}
