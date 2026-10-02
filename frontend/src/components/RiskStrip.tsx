import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { fmtEAT } from "../lib/time";
import { friendlyError } from "../lib/hitl";
import {
  ageSecondsOf,
  EMPTY_STRIP,
  FEED_STALLED_AFTER_MS,
  fmtAgeShort,
  normalizeStrip,
  precisionLabel,
  riskLevelOf,
  riskWord,
  stalenessOf,
  tileTitle,
  type WeatherStrip,
} from "../lib/weather";

/**
 * The Wallboard weather risk strip (spec §7.3, Phase 3: "Wallboard risk strip
 * with STALE badges and measured precision").
 *
 * WHO IS READING IT. A night shift, standing, several metres from a wall-mounted
 * screen whose actual job is P1 incidents. Every choice below follows from that:
 *
 *  - **One word per region.** STORM / FLOOD / CLEAR, in caps, wide-tracked. The
 *    millimetres and gusts are underneath in small type for whoever walks up to
 *    the screen; nobody has to read them to get the answer.
 *  - **Fixed alphabetical order.** Six tiles, never reordered, so the strip is
 *    read by position rather than scanned. `normalizeStrip` sorts.
 *  - **It is never the brightest thing on the glass.** No glow, no animation, no
 *    saturated red — a STORM tile is amber at 7 % background over the wallboard's
 *    own surface, which is quieter than a P2 card and far quieter than the
 *    red-glowing P1 cards it sits above. Weather is advisory and §7.3 is explicit
 *    that it never changes a priority, so it must never out-shout one. (Nothing
 *    here animates, so quiet mode has nothing new to suppress.)
 *  - **Colour is a claim, so stale tiles lose it.** A stale tile goes monochrome,
 *    hatched and dashed. From across the room you see *that tile is not current*
 *    before you read a single character.
 *
 * WHY THE BADGE IS THE POINT. `pollers/weather.py` keeps the last good row when a
 * fetch fails (§7.3.5), so the presence of a reading is not evidence of its
 * freshness. Three-hour-old rain presented as current is worse than no data:
 * it is a decision input that is confidently wrong. Age is therefore computed
 * from the row's absolute `fetched_at` on every tick — see `lib/weather.ts` — so
 * the badge keeps climbing even if the poller, the API, or this fetch dies with
 * the tab still open.
 *
 * DEGRADATION — nothing here may blank the wallboard:
 *  - the endpoint 404s (it does not exist yet) → `enabled:false` → renders `null`;
 *  - `WEATHER_ENABLED=false`, the default → the endpoint has no rows, or says
 *    `enabled:false` → renders `null`. No empty box, no placeholder;
 *  - a later fetch fails or comes back empty → the last good rows stay on the
 *    glass, the header says how long the feed has been down, and each tile ages
 *    into its own STALE badge;
 *  - an unknown or malformed response → `normalizeStrip` returns no regions and
 *    the strip disappears rather than throwing;
 *  - the WebSocket drops → the interval poll below still runs.
 */

/** Fallback poll. The poller itself runs every 15 min; this only has to notice. */
const POLL_MS = 60_000;
/** Ages are shown in whole minutes, so re-rendering twice a minute is enough. */
const TICK_MS = 30_000;

export interface RiskStripProps {
  /**
   * The debounced `signals` slice revision from the WS renderer table. An
   * `external_signal.updated` frame bumps it and the strip refetches once per
   * burst — the same contract the rest of the wallboard uses (defect #26).
   */
  rev?: number;
}

export default function RiskStrip({ rev = 0 }: RiskStripProps) {
  const [strip, setStrip] = useState<WeatherStrip>(EMPTY_STRIP);
  const [receivedAt, setReceivedAt] = useState(0);
  const [lastOkAt, setLastOkAt] = useState<number | null>(null);
  const [feedError, setFeedError] = useState("");
  const [now, setNow] = useState(() => Date.now());

  // Whether anything good has ever arrived. Once it has, a failing fetch keeps
  // the last rows rather than making the strip vanish mid-shift.
  const everLoaded = useRef(false);

  useEffect(() => {
    let alive = true;

    const load = () =>
      api
        .weatherRegions()
        .then((raw) => {
          if (!alive) return;
          const next = normalizeStrip(raw);
          if (next.regions.length > 0) {
            everLoaded.current = true;
            setStrip(next);
            setReceivedAt(Date.now());
            setLastOkAt(Date.now());
            setFeedError("");
          } else if (!everLoaded.current) {
            // Flag off, poller never ran, or the feature is not deployed:
            // stay invisible rather than showing an empty frame.
            setStrip(EMPTY_STRIP);
            setLastOkAt(Date.now());
            setFeedError("");
          } else {
            // Rows disappeared after we had some. Keep the last good ones and
            // let their own badges tell the truth about how old they are.
            setFeedError("feed returned no regions");
          }
        })
        .catch((err) => {
          if (!alive) return;
          // 404 before the read endpoint ships is the expected case, not an
          // error worth putting on a wallboard.
          setFeedError(everLoaded.current ? friendlyError(err) : "");
        });

    load();
    const id = window.setInterval(load, POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, [rev]);

  // Ages must climb on their own, with no network involved.
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), TICK_MS);
    return () => window.clearInterval(id);
  }, []);

  if (strip.regions.length === 0) return null;

  const feedDownMs = lastOkAt === null ? 0 : now - lastOkAt;
  const feedStalled = feedDownMs > FEED_STALLED_AFTER_MS;
  const staleCount = strip.regions.filter(
    (r) => stalenessOf(r, ageSecondsOf(r, receivedAt, now), now).tone === "stale"
  ).length;

  return (
    <section className="wb-risk" aria-label="Regional weather risk (advisory)">
      <div className="wb-risk-head">
        <span className="wb-risk-title">WEATHER RISK</span>
        <span className="wb-risk-sub">advisory · never changes priority</span>
        {staleCount > 0 && (
          <span
            className="wb-risk-badge stale"
            title={`${staleCount} of ${strip.regions.length} regions are showing a kept last-good reading, not a current one.`}
          >
            {staleCount} STALE
          </span>
        )}
        {feedStalled && (
          <span
            className="wb-risk-badge stale"
            title={
              `The wallboard's own fetch of /api/v1/signals/weather/regions last answered ` +
              `${fmtAgeShort(Math.floor(feedDownMs / 1000))} ago. The tiles below are the last ` +
              `readings received.` + (feedError ? `\nLast error: ${feedError}` : "")
            }
          >
            FEED {fmtAgeShort(Math.floor(feedDownMs / 1000))} AGO
          </span>
        )}
        {strip.cap && (
          <span
            className={"wb-risk-cap" + (strip.cap.stale ? " stale" : "")}
            title={
              `Kenya Meteorological Department CAP feed: ${strip.cap.alertCount} alert(s); ` +
              `newest ${strip.cap.newestSent ? fmtEAT(strip.cap.newestSent) : "unknown"}.` +
              (strip.cap.stale ? "\nThe feed itself is stale — treat county alerts as historic." : "")
            }
          >
            KMD CAP {strip.cap.alertCount}
            {strip.cap.stale ? " · STALE" : ""}
          </span>
        )}
      </div>

      <div className="wb-risk-grid">
        {strip.regions.map((r) => {
          const ageS = ageSecondsOf(r, receivedAt, now);
          const s = stalenessOf(r, ageS, now);
          const level = riskLevelOf(r);
          // A stale tile is rendered in the neutral tone: colour is a statement
          // about *now*, and a kept row cannot make one.
          const tone = s.tone === "stale" ? "stale" : level;
          const prec = precisionLabel(r);

          return (
            <div
              key={r.regionCode}
              className={`wb-risk-tile ${tone}${s.tone === "ageing" ? " ageing" : ""}`}
              title={tileTitle(r, s)}
            >
              <div className="wb-risk-tile-head">
                <span className="wb-risk-region">{r.regionCode}</span>
                <span className={`wb-risk-badge ${s.tone}`}>
                  {s.tone === "stale" ? "STALE" : fmtAgeShort(ageS)}
                </span>
              </div>

              <div className="wb-risk-word">
                {riskWord(level)}
                {r.stormFlag && r.floodFlag && <span className="wb-risk-plus">+FLOOD</span>}
              </div>

              <div className="wb-risk-nums">
                {r.rainMm !== null ? `${r.rainMm.toFixed(1)} mm/6h` : "rain —"}
                {" · "}
                {r.gustKmh !== null ? `${Math.round(r.gustKmh)} km/h` : "gust —"}
              </div>

              {/*
                The line the spec asks for: a flag with no measured precision
                beside it is a number nobody has checked, and it says so.
                Stale tiles print the age instead — how old it is matters more
                than how often it has historically been right.
              */}
              <div className="wb-risk-foot">
                {s.tone === "stale" ? `kept · ${fmtAgeShort(ageS)} old` : prec || (r.source || "—")}
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}
