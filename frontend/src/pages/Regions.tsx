/**
 * Regions dashboard (spec §7.4.2) — one card per configured region.
 *
 * WHAT THIS SCREEN IS FOR. A duty manager with six regions and one pair of hands
 * needs to answer "where next?" in about three seconds. So each card carries four
 * things and nothing else: how bad it is (`status`), what is open (P1-P4), what
 * keeps coming back (open problems and the 30-day repeat rate), and whether we
 * can actually see the region at all (the signal badges).
 *
 * GREY IS A REAL ANSWER. The single rule this page exists to honour is that a
 * region with no recent signal renders STALE — grey, with the reason spelled out —
 * and never green. Silence on a NOC wallboard is equally consistent with "nothing
 * is wrong" and with "the feed died two hours ago", and a screen that resolves
 * that ambiguity cheerfully is worse than no screen, because it is trusted. With
 * `WEATHER_ENABLED=false` (the normal deployment) most cards will be grey on day
 * one: that is the honest picture, and the banner at the top names the flag an
 * operator can go and flip rather than leaving six identical mysteries.
 *
 * TOTAL READERS. Nothing here throws. Every field is read defensively — a missing
 * key, a null where a number was expected, or an entirely different response shape
 * degrades to "—", never to a blank screen. The server contract is pinned by
 * `tests/unit/test_dashboard_regions.py`, but a page that a NOC relies on should
 * not fall over because a deploy went out in the wrong order.
 *
 * No new CSS: this reuses `.panel`, `.chip`, `.pill` and `.muted` from
 * `styles.css`, with layout done inline, so the page adds no shared-file edits
 * beyond its route.
 */

import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { fmtDateTime } from "../lib/time";

/** Status → chip class. Grey (plain `chip`) for STALE is deliberate: grey reads as
 *  "unknown", which is exactly the claim being made. Green is reserved for CALM,
 *  which means somebody actually looked. */
const STATUS_CHIP: Record<string, string> = {
  ALERT: "chip bad",
  WATCH: "chip warn",
  STALE: "chip",
  CALM: "chip ok",
};

const STATUS_HINT: Record<string, string> = {
  ALERT: "Open P1, or a live storm/flood flag over the region.",
  WATCH: "Open P2, or an incident already past its restore SLA.",
  STALE: "No incidents worth flagging — and no fresh signal either. We are blind here, not calm.",
  CALM: "Nothing open, and at least one outside-world signal is fresh.",
};

const PRIORITIES = ["P1", "P2", "P3", "P4"] as const;

function num(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

/** `0.125` → `12.5%`; null/absent → `—`. The backend sends null rather than 0 when
 *  nothing opened in the window, because "0% of no faults repeated" is a
 *  measurement nobody made and a zero would read as a clean bill of health. */
function pct(value: unknown): string {
  return typeof value === "number" && Number.isFinite(value)
    ? `${(value * 100).toFixed(1)}%`
    : "—";
}

function SignalBadge({ name, block }: { name: string; block: any }) {
  const b = block && typeof block === "object" ? block : {};
  const available = b.available === true;
  const stale = b.stale !== false;
  const storm = b.storm_flag === true || b.flag === true;

  let cls = "chip";
  let text = `${name} · NO FEED`;
  if (available && !stale) {
    cls = storm ? "chip warn" : "chip ok";
    text = `${name} · ${storm ? "FLAG" : "OK"}`;
  } else if (available) {
    text = `${name} · STALE`;
  }
  // The title carries the *reason*, which is the difference between a badge an
  // operator acts on and one they learn to ignore.
  const why = [b.reason, b.last_error, b.fetched_at ? `fetched ${fmtDateTime(b.fetched_at)}` : null]
    .filter(Boolean)
    .join(" — ");
  return (
    <span className={cls} title={why || `${name}: no data`}>
      {text}
    </span>
  );
}

function RegionCard({ region }: { region: any }) {
  const status = typeof region.status === "string" ? region.status : "STALE";
  const signals = region.signals && typeof region.signals === "object" ? region.signals : {};
  const problems: any[] = Array.isArray(region.problems_open) ? region.problems_open : [];
  const baseline = region.regulatory_baseline;
  const counties: string[] = Array.isArray(region.counties) ? region.counties : [];
  const byPriority = region.open_by_priority && typeof region.open_by_priority === "object"
    ? region.open_by_priority
    : {};

  return (
    <div className="panel">
      <div className="panel-head">
        <h3>
          {region.region_code} · {region.label || region.region_code}
        </h3>
        <span className={STATUS_CHIP[status] || "chip"} title={STATUS_HINT[status] || ""}>
          {status}
        </span>
      </div>

      <div className="muted" style={{ fontSize: "0.72rem", marginBottom: "0.6rem" }}>
        {counties.length ? counties.join(" · ") : "counties not configured"}
      </div>

      <div className="chips" style={{ marginBottom: "0.6rem" }}>
        {PRIORITIES.map((p) => (
          <span key={p} className={`pill ${p}`} title={`${p} incidents open now`}>
            {p} {num(byPriority[p])}
          </span>
        ))}
        <span className="chip">Open {num(region.open_total)}</span>
        {num(region.sla_breached) > 0 && (
          <span className="chip warn" title="Open incidents already past their restore SLA">
            SLA {num(region.sla_breached)}
          </span>
        )}
      </div>

      <div className="chips" style={{ marginBottom: "0.7rem" }}>
        <SignalBadge name="WX" block={signals.weather} />
        <SignalBadge name="FLOOD" block={signals.flood} />
        <SignalBadge name="CAP" block={signals.cap} />
        <SignalBadge name="KPLC" block={signals.kplc} />
      </div>

      <div className="muted" style={{ fontSize: "0.72rem", marginBottom: "0.5rem" }}>
        Repeat faults (30d): <strong>{pct(region.repeat_fault_rate_30d)}</strong>{" "}
        ({num(region.repeat_faults_30d)} of {num(region.incidents_30d)} incidents)
      </div>

      {problems.length > 0 ? (
        <div className="list" style={{ maxHeight: "170px" }}>
          {problems.map((p: any) => (
            // Not `.row`: that class carries `cursor: pointer`, and a problem line
            // here does not navigate anywhere yet. A pointer that leads nowhere is a
            // small lie the floor notices.
            <div
              key={p.problem_number}
              style={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                gap: "0.6rem",
                padding: "0.45rem 0.6rem",
                borderRadius: "9px",
                border: "1px solid var(--border)",
                fontSize: "0.74rem",
              }}
            >
              <span>
                <strong>{p.problem_number}</strong>{" "}
                <span className="muted">
                  {p.site_id} · ×{num(p.occurrence_count)} · last {fmtDateTime(p.last_seen)}
                </span>
              </span>
              {p.known_error === true && (
                <span className="chip ok" title="Cause understood and a workaround is recorded">
                  KNOWN ERROR
                </span>
              )}
            </div>
          ))}
          {num(region.problems_open_total) > problems.length && (
            <div className="muted" style={{ fontSize: "0.7rem" }}>
              + {num(region.problems_open_total) - problems.length} more open problem(s)
            </div>
          )}
        </div>
      ) : (
        <div className="muted" style={{ fontSize: "0.72rem" }}>No open problems.</div>
      )}

      <div
        className="muted"
        style={{ fontSize: "0.68rem", marginTop: "0.7rem", borderTop: "1px solid var(--border)", paddingTop: "0.5rem" }}
      >
        {baseline ? (
          <>
            CA QoS {typeof baseline.ca_qos_score === "number" ? `${baseline.ca_qos_score}%` : "—"}{" "}
            {baseline.meets_pass_mark === false && <span className="chip warn">BELOW PASS MARK</span>}
            <br />
            {/* Granularity is the load-bearing word: the report has five clusters and
                this dashboard has six regions, so an operator-wide figure must never
                be presented as a measurement of this region. */}
            {baseline.granularity === "cluster"
              ? `cluster “${baseline.cluster}”`
              : "operator-wide figure — no cluster mapped to this region yet"}
            {" · "}
            {baseline.report} (published {baseline.report_date})
          </>
        ) : (
          <>No CA QoS baseline seeded for this operator.</>
        )}
      </div>
    </div>
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

  const regions: any[] = Array.isArray(data?.regions) ? data.regions : [];
  const staleCount = regions.filter((r) => r?.signals_stale !== false).length;

  return (
    <div>
      <h2 style={{ marginTop: 0 }}>Regions</h2>
      <p className="muted">
        Per-region fault load, repeat faults and outside-world signal freshness. Rows are every
        region on the operator profile — a region with nothing open still appears, because a
        region that vanishes from a wallboard is the one nobody checks.
      </p>

      {error && (
        <div className="storm-banner" style={{ marginBottom: "1rem" }}>
          <div>
            <strong>Regions dashboard unavailable</strong>
            <div className="muted">{error}</div>
          </div>
        </div>
      )}

      {data && data.weather_enabled === false && (
        <div className="storm-banner" style={{ marginBottom: "1rem" }}>
          <div>
            <strong>Weather polling is off — {staleCount} of {regions.length} regions are blind</strong>
            <div className="muted">
              <code>WEATHER_ENABLED=false</code>, so no forecast is being fetched and those regions
              show STALE rather than green. That is the honest reading, not a fault on this page.
            </div>
          </div>
        </div>
      )}

      {data && (
        <div className="chips" style={{ marginBottom: "1rem" }}>
          <span className="chip accent">{data.operator_id}</span>
          <span className="chip">Rollup window {num(data.window_days, 30)}d</span>
          <span className="chip" title="When this snapshot was built">
            {fmtDateTime(data.generated_at)}
          </span>
        </div>
      )}

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fill, minmax(340px, 1fr))",
          gap: "1rem",
        }}
      >
        {regions.map((r: any) => (
          <RegionCard key={r.region_code} region={r} />
        ))}
      </div>

      {data && regions.length === 0 && (
        <div className="empty">No regions configured on this operator profile.</div>
      )}
    </div>
  );
}
