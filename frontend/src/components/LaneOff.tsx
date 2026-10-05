import type { ReactNode } from "react";
import { PowerOff } from "lucide-react";

/**
 * The one way a feature lane says it is switched off. Spec §7.10: "off" must never look
 * like "empty", so every flagged lane (PIRs, maintenance, scorecards, contracts) shows this
 * same panel (a power-off icon, "Switched off", the title) instead of four different sentences.
 *
 * Written for a manager watching the demo: the title names the lane ("Post-incident reviews
 * are off in this demo"), and one sentence says what the flag would turn on. `children` is
 * the end of that sentence, without the full stop: "Turn on <code>FLAG</code> to {children}."
 * No `.env` paths and no job names here; those live in the runbook.
 */
export default function LaneOff({ title, flag, children }: { title: string; flag: string; children?: ReactNode }) {
  return (
    <div className="panel lane-off" role="status">
      <span className="lane-off-icon" aria-hidden="true">
        <PowerOff size={22} strokeWidth={1.75} />
      </span>
      <div className="lane-off-text">
        <span className="lane-off-tag">Switched off</span>
        <h2 className="panel-kicker">{title}</h2>
        <p className="lane-off-body">
          Turn on <code>{flag}</code> to {children ?? "switch this lane on"}.
        </p>
      </div>
    </div>
  );
}
