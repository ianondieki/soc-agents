import type { ReactNode } from "react";

/**
 * The one way a feature lane says it is switched off. Spec §7.10: "off" must never look
 * like "empty", so every flagged lane (PIRs, maintenance, scorecards, contracts) shows this
 * same dashed panel with the flag that turns it on, instead of four different sentences.
 */
export default function LaneOff({ title, flag, children }: { title: string; flag: string; children?: ReactNode }) {
  return (
    <div className="panel lane-off" role="status">
      <div className="panel-head">
        <h2 className="panel-kicker">{title}</h2>
        <span className="chip">{flag}=false</span>
      </div>
      <p className="lane-off-body">
        Set <code>{flag}=true</code> in <code>.env</code> and restart the API. Off is the shipped default, not an empty
        list{children ? <>: {children}</> : "."}
      </p>
    </div>
  );
}
