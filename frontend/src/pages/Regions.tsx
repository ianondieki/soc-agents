/**
 * Regions dashboard (spec §7.4.2) — one card per configured region.
 *
 * WHAT THIS SCREEN IS FOR. A duty manager with six regions and one pair of hands
 * needs to answer "where next?" in about three seconds. So each card carries four
 * things and nothing else: how bad it is (`status`), what is open (P1-P4), what
 * keeps coming back (open problems and the 30-day repeat rate), and whether we
 * can actually see the region at all (the outside-world signals).
 *
 * GREY IS A REAL ANSWER. A region with no recent outside-world signal is STALE, never
 * calm: silence on a NOC screen is equally consistent with "nothing is wrong" and
 * with "the feed died two hours ago". Each idea is said once: when every region is
 * blind (the normal deployment, weather polling off) one sentence under the page head
 * says so and the cards carry no "stale" chip and no row of four "no feed" readings;
 * when only some are blind, those cards keep their chip. The CA QoS baseline is one
 * operator-wide figure, so it is shown once, in its own panel, never as if it were a
 * measurement of each region; a region with its own cluster figure shows it on its card.
 *
 * TOTAL READERS. Nothing here throws. Every field is read defensively — a missing
 * key, a null where a number was expected, or an entirely different response shape
 * degrades to "—", never to a blank screen. The server contract is pinned by
 * `tests/unit/test_dashboard_regions.py`.
 *
 * Layout lives in Regions.css; counts are text in an aligned `dl`, not chips; the one
 * chip on a card is its status when that status is not calm.
 */

import { Fragment, useCallback, useEffect, useId, useState } from "react";
import { Link } from "react-router-dom";
import { priorityTitle } from "../lib/agents";
import { api } from "../api";
import { IconDot } from "../lib/icons";
import { fmtDateTime } from "../lib/time";
import "./Regions.css";

/** Status → chip class. Grey (plain `chip`) for STALE is deliberate: grey reads as
 *  "unknown", which is exactly the claim being made. CALM is the normal state, so it
 *  is a word and earns no chip and no green. ALERT is the static red chip: nothing on
 *  this page pulses (only the Wallboard's escalation does). */
const STATUS_CHIP: Record<string, string | null> = {
  ALERT: "chip danger",
  WATCH: "chip warn",
  STALE: "chip",
  CALM: null,
};

const STATUS_WORD: Record<string, string> = {
  ALERT: "alert",
  WATCH: "watch",
  STALE: "stale",
  CALM: "calm",
};

const STATUS_HINT: Record<string, string> = {
  ALERT: "Open P1, or a live storm/flood flag over the region.",
  WATCH: "Open P2, or a ticket already past its restore SLA.",
  STALE: "No tickets worth flagging, and no fresh signal either. We are blind here, not calm.",
  CALM: "Nothing open, and at least one outside-world signal is fresh.",
};

const PRIORITIES = ["P1", "P2", "P3", "P4"] as const;
/** The four outside-world feeds, named as the floor says them: CAP alerts are flood warnings,
 *  KPLC notices are Kenya Power notices. */
const FEEDS: { key: string; name: string }[] = [
  { key: "weather", name: "Weather" },
  { key: "flood", name: "Flood forecast" },
  { key: "cap", name: "Flood warnings" },
  { key: "kplc", name: "Kenya Power notices" },
];

function num(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function obj(value: unknown): Record<string, any> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, any>) : {};
}

/** `0.125` → `12.5%`; null/absent → `—`. The backend sends null rather than 0 when
 *  nothing opened in the window, because "0% of no faults repeated" is a
 *  measurement nobody made and a zero would read as a clean bill of health. */
function pct(value: unknown): string {
  return typeof value === "number" && Number.isFinite(value) ? `${(value * 100).toFixed(1)}%` : "—";
}

/** True when the region has no outside-world feed answering (the server's own rule). */
function isBlind(region: any): boolean {
  return obj(region).signals_stale !== false;
}

/** At least one feed is configured for the region, so its four readings are worth a row. */
function hasAnyFeed(region: any): boolean {
  const signals = obj(obj(region).signals);
  return FEEDS.some((f) => obj(signals[f.key]).available === true);
}

/** One outside-world feed, as words. Only a live storm flag earns the attention dot;
 *  "no feed" and "stale" are said plainly, never painted green. */
function SignalReading({ name, block }: { name: string; block: unknown }) {
  const b = obj(block);
  const available = b.available === true;
  const stale = b.stale !== false;
  const storm = b.storm_flag === true || b.flag === true;

  let word = "no feed";
  if (available && !stale) word = storm ? "storm flag" : "fresh";
  else if (available) word = "stale";
  // The title carries the *reason*, which is the difference between a reading an
  // operator acts on and one they learn to ignore.
  const why = [b.reason, b.last_error, b.fetched_at ? `fetched ${fmtDateTime(b.fetched_at)}` : null]
    .filter(Boolean)
    .join("; ");
  if (available && !stale && storm) {
    return (
      <span className="attn warn" title={why || `${name}: storm flag`}>
        <IconDot /> {name} storm flag
      </span>
    );
  }
  return (
    <span title={why || `${name}: no data`}>
      {name} {word}
    </span>
  );
}

/** Open counts in one aligned row: fixed columns, numbers right-aligned, so the cards read
 *  down as well as across. Nothing open is one phrase, not eight zeros. */
function Counts({ region }: { region: any }) {
  const byPriority = obj(region.open_by_priority);
  const total = num(region.open_total);
  const sla = num(region.sla_breached);
  // Beside a possible outage "Nothing open" would read as "all is well"; it means no ticket.
  const surge = typeof obj(region.complaint_surge).place === "string";
  if (total === 0 && sla === 0) return <p className="region-nothing">{surge ? "No ticket open" : "Nothing open"}</p>;
  return (
    <dl className="region-counts">
      {PRIORITIES.map((p) => {
        const n = num(byPriority[p]);
        // Colour is spent only on a count that is not zero: a red "P1 0" on six quiet
        // regions would make the one real P1 invisible.
        return (
          <div key={p} className={n > 0 ? undefined : "zero"} title={`${p} tickets open now`}>
            <dt>{n > 0 ? <span className={`pill ${p}`} title={priorityTitle(p)}>{p}</span> : p}</dt>
            <dd>{n}</dd>
          </div>
        );
      })}
      <div title="Open tickets, any priority">
        <dt>Open</dt>
        <dd>{total}</dd>
      </div>
      <div className={sla > 0 ? undefined : "zero"} title="Open tickets already past their restore SLA">
        <dt>Past SLA</dt>
        <dd>
          {sla > 0 ? (
            <span className="attn warn">
              <IconDot />
              {sla}
            </span>
          ) : (
            0
          )}
        </dd>
      </div>
    </dl>
  );
}

/**
 * A burst of complaints about a place in this region with no ticket open (docs/CLOSE_THE_LOOP.md
 * §3): one line, violet because a supervisor decides it on Approvals. Absent when the key is null.
 */
function SurgeLine({ surge }: { surge: unknown }) {
  const s = obj(surge);
  const place = typeof s.place === "string" ? s.place.trim() : "";
  if (!place) return null;
  const n = num(s.complaints, NaN);
  const count = Number.isFinite(n) ? `${n} ${n === 1 ? "complaint" : "complaints"}, ` : "";
  // Straight to its card: /hitl opens, scrolls to and focuses the one named in `?task=`.
  const to = typeof s.card_id === "string" && s.card_id ? `/hitl?task=${encodeURIComponent(s.card_id)}` : "/hitl";
  return (
    <p className="region-surge">
      <Link to={to} className="attn hitl region-surge-link">
        <IconDot />
        <span>
          Possible outage in {place}: {count}no ticket
        </span>
      </Link>
    </p>
  );
}

function RegionCard({ region, allBlind }: { region: any; allBlind: boolean }) {
  const headId = useId();
  const status = typeof region.status === "string" ? region.status : "STALE";
  const signals = obj(region.signals);
  const problems: any[] = Array.isArray(region.problems_open) ? region.problems_open : [];
  const problemsTotal = Math.max(num(region.problems_open_total), problems.length);
  const counties: string[] = Array.isArray(region.counties) ? region.counties : [];
  const baseline = obj(region.regulatory_baseline);
  const ownBaseline = baseline.granularity === "cluster" ? baseline : null;
  const chip = status in STATUS_CHIP ? STATUS_CHIP[status] : "chip";
  const word = STATUS_WORD[status] || status.toLowerCase();
  // Every region blind: the page says it once, so a grey "stale" on each card adds nothing.
  const showStatus = !(status === "STALE" && allBlind);
  const rate = region.repeat_fault_rate_30d;

  return (
    <section className="panel region-card" aria-labelledby={headId}>
      <div className="panel-head">
        <h2 id={headId} className="panel-title head-row">
          {region.label || region.region_code}
          <span className="mono">{region.region_code}</span>
        </h2>
        {showStatus &&
          (chip ? (
            <span className={chip} title={STATUS_HINT[status] || ""}>
              {word}
            </span>
          ) : (
            <span className="muted" title={STATUS_HINT[status] || ""}>
              {word}
            </span>
          ))}
      </div>

      <p className="region-counties">{counties.length ? counties.join(", ") : "Counties not configured"}</p>

      <SurgeLine surge={region.complaint_surge} />

      <Counts region={region} />

      {hasAnyFeed(region) && (
        <div className="facts region-signals">
          {FEEDS.map((f) => (
            <SignalReading key={f.key} name={f.name} block={signals[f.key]} />
          ))}
        </div>
      )}

      <dl className="rail-dl region-dl">
        <dt>Repeat faults</dt>
        <dd>
          {typeof rate === "number" && Number.isFinite(rate) ? (
            <>
              <span className="mono">{pct(rate)}</span>, {num(region.repeat_faults_30d)} of{" "}
              {num(region.incidents_30d)} {num(region.incidents_30d) === 1 ? "ticket" : "tickets"}
            </>
          ) : (
            "No tickets in the window"
          )}
        </dd>
        <dt>Open problems</dt>
        <dd>{problemsTotal > 0 ? problemsTotal : "None"}</dd>
        {ownBaseline && (
          <Fragment>
            <dt>CA QoS</dt>
            <dd>
              <span className="mono">
                {typeof ownBaseline.ca_qos_score === "number" ? `${ownBaseline.ca_qos_score}%` : "—"}
              </span>{" "}
              cluster “{ownBaseline.cluster}”
              {ownBaseline.meets_pass_mark === false && (
                <span className="attn warn region-below">
                  <IconDot /> below pass mark
                </span>
              )}
            </dd>
          </Fragment>
        )}
      </dl>

      {problems.length > 0 && (
        <ul className="region-problems">
          {problems.map((p: any, i: number) => (
            // `.row.static`: a problem line here does not navigate anywhere yet, and a pointer
            // that leads nowhere is a small lie the floor notices.
            <li key={p.problem_number || i} className="row static">
              <span className="mono">{p.problem_number}</span>
              <span className="facts">
                <span className="mono">{p.site_id}</span>
                <span>{num(p.occurrence_count)} times</span>
                <span>last {fmtDateTime(p.last_seen)}</span>
              </span>
              {p.known_error === true ? (
                <span className="muted" title="Cause understood and a workaround is recorded">
                  known error
                </span>
              ) : (
                <span />
              )}
            </li>
          ))}
        </ul>
      )}
      {problemsTotal > problems.length && (
        <p className="muted region-more">
          {problemsTotal - problems.length} more open {problemsTotal - problems.length === 1 ? "problem" : "problems"}
        </p>
      )}
    </section>
  );
}

/** The CA QoS baseline, once: it is an operator-wide figure, not a reading of any one region. */
function QosPanel({ baseline }: { baseline: Record<string, any> }) {
  const headId = useId();
  const score = typeof baseline.ca_qos_score === "number" ? `${baseline.ca_qos_score}%` : "—";
  const pass = typeof baseline.pass_mark_pct === "number" ? `${baseline.pass_mark_pct}%` : "";
  return (
    <section className="panel" aria-labelledby={headId}>
      <h2 id={headId} className="panel-title">
        CA quality of service
      </h2>
      <dl className="rail-dl">
        <dt>Score</dt>
        <dd className="region-qos-score">
          <span className="mono">{score}</span>
          {baseline.meets_pass_mark === false ? (
            <span className="attn warn">
              <IconDot /> below the {pass || "pass"} mark
            </span>
          ) : baseline.meets_pass_mark === true && pass ? (
            <span className="muted">pass mark {pass}</span>
          ) : null}
        </dd>
        <dt>Scope</dt>
        {/* Granularity is the load-bearing word: the report has five clusters and this
            dashboard has six regions, so an operator-wide figure is never shown per region. */}
        <dd>Operator-wide; no cluster is mapped to a region yet</dd>
        {baseline.report && (
          <>
            <dt>Report</dt>
            <dd>
              {baseline.report}
              {baseline.report_date ? `, published ${baseline.report_date}` : ""}
              {typeof baseline.source_url === "string" && /^https:\/\//.test(baseline.source_url) && (
                <>
                  {" "}
                  <a className="region-link" href={baseline.source_url} target="_blank" rel="noreferrer">
                    Source
                  </a>
                </>
              )}
            </dd>
          </>
        )}
      </dl>
    </section>
  );
}

export default function Regions({ tick }: { tick: number }) {
  const [data, setData] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    api
      .dashboardRegions()
      .then((d: any) => {
        setData(d);
        setError(null);
      })
      .catch((e: any) => setError(String(e?.message || e)));
  }, []);

  useEffect(() => {
    load();
  }, [load, tick]);

  const regions: any[] = Array.isArray(data?.regions) ? data.regions.filter((r: unknown) => r && typeof r === "object") : [];
  const blind = regions.filter(isBlind).length;
  const allBlind = regions.length > 0 && blind === regions.length;
  const operatorBaseline = regions
    .map((r) => obj(r.regulatory_baseline))
    .find((b) => Object.keys(b).length > 0 && b.granularity !== "cluster");
  const blindWhy =
    STATUS_HINT.STALE + (data?.weather_enabled === false ? " Weather polling is off on this deployment." : "");

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Regions</h1>
          <p
            className="lead"
            title="Every region on the operator profile appears, even with nothing open: a region that vanishes from a wallboard is the one nobody checks."
          >
            Fault load, repeat faults and signal freshness for every region.
          </p>
        </div>
        {data && (
          <div className="page-actions facts">
            <span>{num(data.window_days, 30)}-day window</span>
            <span title="When this snapshot was built">built {fmtDateTime(data.generated_at)}</span>
          </div>
        )}
      </div>

      {/* Said once, here, instead of a row of four "no feed" readings on every card. */}
      {data && blind > 0 && (
        <p className="region-note" title={blindWhy}>
          <span className="attn warn">
            <IconDot />
            {allBlind
              ? "No live feed from weather, flood warnings or Kenya Power notices; counts come from the ticket store."
              : `${blind} of ${regions.length} regions have no live feed; their counts come from the ticket store.`}
          </span>
        </p>
      )}

      <div className="stack">
        {error && (
          <div className="panel">
            <div className="empty" role="alert" title={error}>
              {data
                ? `Couldn't refresh the regions; the cards show the snapshot built ${fmtDateTime(data.generated_at)}.`
                : "Couldn't load the regions."}
              <button className="btn sm" onClick={load}>
                Retry
              </button>
            </div>
          </div>
        )}

        {!data && !error && (
          <div className="region-grid" aria-busy="true" aria-label="Loading the regions">
            {Array.from({ length: 6 }, (_, i) => (
              <div key={i} className="panel">
                <div className="skeleton-rows" aria-hidden="true">
                  <span className="skeleton" style={{ width: "40%" }} />
                  <span className="skeleton" />
                  <span className="skeleton" style={{ width: "70%" }} />
                </div>
              </div>
            ))}
          </div>
        )}

        {regions.length > 0 && (
          <div className="region-grid">
            {regions.map((r: any, i: number) => (
              <RegionCard key={r.region_code || i} region={r} allBlind={allBlind} />
            ))}
          </div>
        )}

        {data && regions.length === 0 && (
          <div className="panel">
            <div className="empty">No regions configured on this operator profile.</div>
          </div>
        )}

        {operatorBaseline && <QosPanel baseline={operatorBaseline} />}
      </div>
    </div>
  );
}
