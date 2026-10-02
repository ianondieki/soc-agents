import type { ReactNode } from "react";

/**
 * The one way a feature lane says it is switched off. Spec §7.10: "off" must never look
 * like "empty", so every flagged lane (PIRs, maintenance, scorecards, contracts) shows this
 * same dashed panel with the flag that turns it on, instead of four different sentences.
 * The flag's value is part of the sentence, not a chip: it is a fact, not a state to scan for.
 */
export default function LaneOff({ title, flag, children }: { title: string; flag: string; children?: ReactNode }) {
  return (
    <div className="panel lane-off" role="status">
      <h2 className="panel-kicker">{title}</h2>
      <p className="lane-off-body">
        <code>{flag}=false</code> is the shipped default: off, not an empty list{children ? <>; {children}</> : "."} Set{" "}
        <code>{flag}=true</code> in <code>.env</code> and restart the API to turn it on.
      </p>
    </div>
  );
}
