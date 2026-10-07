import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useCalm } from "../../lib/motion";
import { currentShift } from "../../lib/shift";
import { fmtHM } from "../../lib/time";

const pad2 = (n: number) => String(n).padStart(2, "0");

/** The next shift handover as a countdown, from the operator's shift hours. It ticks each second;
 *  calm, it shows hours and minutes only and moves on the half minute. */
export default function Handover({ profile }: { profile: any }) {
  const calm = useCalm();
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const id = window.setInterval(() => setNow(new Date()), calm ? 30_000 : 1000);
    return () => window.clearInterval(id);
  }, [calm]);
  const { shift, next, end } = currentShift(profile, now);
  const left = Math.max(0, end.getTime() - now.getTime());
  const h = Math.floor(left / 3_600_000);
  const m = Math.floor(left / 60_000) % 60;
  const sec = Math.floor(left / 1000) % 60;
  return (
    <div className="ld-handover">
      <p className="ld-handover-label">
        The {shift} shift hands over to {next} at {fmtHM(end)} EAT
      </p>
      <p className="ld-handover-clock" role="timer" aria-label={`Handover in ${h} hours and ${m} minutes`}>
        <span>
          <b>{pad2(h)}</b>
          <i>hours</i>
        </span>
        <span>
          <b>{pad2(m)}</b>
          <i>minutes</i>
        </span>
        {!calm && (
          <span>
            <b>{pad2(sec)}</b>
            <i>seconds</i>
          </span>
        )}
      </p>
      <p className="ld-handover-note">
        The ledger fills itself all shift; the handover is the one thing a person sends.{" "}
        <Link className="ld-btn text" to="/shift">
          Open the Shift desk
        </Link>
      </p>
    </div>
  );
}
