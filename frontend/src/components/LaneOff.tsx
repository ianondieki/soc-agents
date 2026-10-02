import type { ReactNode } from "react";

/**
 * The one way a feature lane says it is switched off. Spec §7.10: "off" must never look
 * like "empty", so every flagged lane (PIRs, maintenance, scorecards, contracts) shows this
 * same dashed panel instead of four different sentences.
 *
 * Written for a manager watching the demo: the title names the lane ("Post-incident reviews
 * are off in this demo"), and one sentence says what the flag would turn on. `children` is
 * the end of that sentence, without the full stop: "Turn on <code>FLAG</code> to {children}."
 * No `.env` paths and no job names here; those live in the runbook.
 */
export default function LaneOff({ title, flag, children }: { title: string; flag: string; children?: ReactNode }) {
  return (
    <div className="panel lane-off" role="status">
      <h2 className="panel-kicker">{title}</h2>
      <p className="lane-off-body">
        Turn on <code>{flag}</code> to {children ?? "switch this lane on"}.
      </p>
    </div>
  );
}
