import { useEffect, useState } from "react";
import { Gauge, Moon, Sun } from "lucide-react";
import { fmtHM } from "../../lib/time";

export type LiveTone = "ok" | "wait" | "bad";

const ICON = { size: 15, strokeWidth: 1.75, "aria-hidden": true } as const;

/** The minute in Nairobi, re-rendered on the minute (not every second: nothing here ticks). */
function useMinute(): Date {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    let timer = 0;
    const arm = () => {
      const d = new Date();
      timer = window.setTimeout(() => {
        setNow(new Date());
        arm();
      }, 60_000 - (d.getSeconds() * 1000 + d.getMilliseconds()) + 50);
    };
    arm();
    return () => window.clearTimeout(timer);
  }, []);
  return now;
}

function Clock() {
  const now = useMinute();
  return (
    <span className="tb-item tb-clock" title="Time in Nairobi (EAT)">
      <time dateTime={now.toISOString()}>{fmtHM(now)}</time>
      <span className="tb-zone">EAT</span>
    </span>
  );
}

/**
 * The top bar's state of the floor, in one quiet track: the live link (a green dot while the
 * stream is up; red words only when it is broken), the autonomy level (its meaning on hover),
 * the shift and the time in Nairobi. The phone menu sheet shows the same track, without the clock.
 * Styles: styles.css (`.tb-*`).
 */
export default function TopbarStatus({
  live,
  liveText,
  liveTitle,
  autonomy,
  autonomyTitle,
  shift,
  clock = true,
  announce = true,
}: {
  live: LiveTone;
  liveText: string;
  liveTitle: string;
  autonomy: string;
  autonomyTitle: string;
  shift: string;
  clock?: boolean;
  /** False for the copy in the phone sheet: the bar's dot is already the live region. */
  announce?: boolean;
}) {
  const night = shift.toLowerCase() === "night";
  return (
    <div className="tb-status">
      <span className={`tb-item tb-live ${live}`} role={announce ? "status" : undefined} title={liveTitle}>
        <span className="tb-dot" aria-hidden="true" />
        {liveText}
      </span>
      <span className="tb-item tb-autonomy" title={autonomyTitle}>
        <Gauge {...ICON} />
        {autonomy}
      </span>
      <span className="tb-item tb-shift">
        {night ? <Moon {...ICON} /> : <Sun {...ICON} />}
        {night ? "Night shift" : "Day shift"}
      </span>
      {clock && <Clock />}
    </div>
  );
}
