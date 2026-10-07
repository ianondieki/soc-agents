import CountUp from "../CountUp";
import { fmtInt } from "../../lib/agents";

/** Three figures under the hero's actions, counting up once: the agents' record so far. The
 *  hours are the floor's own estimate (the Saved panel says how it is made). */
export default function HeroFigures({ p }: { p: any | null }) {
  if (!p) return <div className="ld-hero-figs is-empty" />;
  const whole = (n: number) => fmtInt(Math.round(n));
  return (
    <dl className="ld-hero-figs">
      <div>
        <dt>alarms through the agents</dt>
        <dd>
          <CountUp value={Number(p.alarms?.processed || 0)} format={whole} ms={1100} />
        </dd>
      </div>
      <div>
        <dt>tickets opened and filled in</dt>
        <dd>
          <CountUp value={Number(p.alarms?.incidents_created || 0)} format={whole} ms={1100} />
        </dd>
      </div>
      <div>
        <dt>hours of analyst work, by the floor's estimate</dt>
        <dd>
          <CountUp value={Math.round(Number(p.toil?.hours_saved || 0))} format={whole} ms={1100} />
        </dd>
      </div>
    </dl>
  );
}
