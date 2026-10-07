import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Pause, Play } from "lucide-react";
import { humanStatus, regionName } from "../../lib/agents";
import { useCalm } from "../../lib/motion";
import { fmtHM } from "../../lib/time";

/**
 * The open tickets as a slow tape under the hero: priority, site, region, state and when. It moves
 * on its own, so it pauses on hover and on its own Pause button (WCAG 2.2.2), and it STOPS (the
 * Pause button's state) the moment keyboard focus enters it: a link inside a moving, clipped
 * track can sit off screen with its focus ring hidden, so focus turns it into the still row.
 * Calm (quiet mode or reduced motion) or paused, it is one row of the tickets that scrolls by
 * hand. Moving, the rows repeat until one copy is wider than a wide screen, then the whole copy
 * repeats once so the loop is seamless; every repeat is hidden from assistive tech and the tab
 * order.
 */
const TAPE_MIN_ITEMS = 10;

export default function OpenTape({ rows, profile }: { rows: any[]; profile: any }) {
  const calm = useCalm();
  const [paused, setPaused] = useState(false);
  const windowRef = useRef<HTMLDivElement>(null);
  const moving = !calm && !paused;
  // Back to the start of the row when it moves again, so the clipped track is not left scrolled.
  useEffect(() => {
    if (moving && windowRef.current) windowRef.current.scrollLeft = 0;
  }, [moving]);
  if (rows.length < 3) return null;
  const repeats = moving ? Math.ceil(TAPE_MIN_ITEMS / rows.length) : 1;
  const copy = Array.from({ length: repeats }, (_, r) => rows.map((row) => ({ row, r }))).flat();
  const item = ({ row: i, r }: { row: any; r: number }, loop: number) => {
    const pr = String(i.priority || "").toUpperCase();
    const hidden = loop > 0 || r > 0;
    return (
      <li key={`${loop}-${r}-${i.id}`} aria-hidden={hidden || undefined}>
        <span className={`ld-pill ${pr}`}>{pr}</span>
        <Link to={`/incidents/${i.id}`} tabIndex={hidden ? -1 : undefined}>
          {i.site_name || i.site_id}
        </Link>
        <span className="where">{regionName(i.region_code, profile)}</span>
        <span className="state">{humanStatus(i.status)}</span>
        <span className="when ld-mono">{fmtHM(i.created_at)}</span>
      </li>
    );
  };
  return (
    <section className={"ld-tape" + (moving ? " is-moving" : "")} aria-labelledby="ld-tape-title">
      <div className="ld-wrap ld-tape-head">
        <h2 id="ld-tape-title" className="ld-tape-title">
          <span className="ld-tape-dot" aria-hidden="true" />
          Open on the board now
        </h2>
        {!calm && (
          <button type="button" className="ld-tape-btn" onClick={() => setPaused((x) => !x)}>
            {paused ? <Play size={14} aria-hidden="true" /> : <Pause size={14} aria-hidden="true" />}
            {paused ? "Play" : "Pause"}
          </button>
        )}
      </div>
      <div
        ref={windowRef}
        className="ld-tape-window"
        role="region"
        aria-label="Open tickets"
        tabIndex={moving ? undefined : 0}
        onFocusCapture={() => setPaused(true)}
      >
        <ul className="ld-tape-track" style={{ ["--tape-s" as any]: `${Math.max(36, copy.length * 6)}s` }}>
          {copy.map((c) => item(c, 0))}
          {moving && copy.map((c) => item(c, 1))}
        </ul>
      </div>
    </section>
  );
}
