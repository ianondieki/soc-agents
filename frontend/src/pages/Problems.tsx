import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { CircleHelp, RadioTower, Server, Thermometer, Wifi, Zap, type LucideIcon } from "lucide-react";
import { api } from "../api";
import { humanEnum, regionName } from "../lib/agents";
import "./Problems.css";

/**
 * Problems (spec §7.7): a site that keeps failing for the same cause. The Recurrence agent opens
 * a problem record when a site fails three times in its lookback window (30 days by default) on
 * one failure domain, so the cause gets fixed rather than the symptom. Each problem is a card:
 * the site by name, how often and why it failed, said as a sentence, and a link to its tickets.
 */

/** A failure domain's icon and the word the floor uses for it. */
const DOMAINS: Record<string, { icon: LucideIcon; word: string }> = {
  POWER: { icon: Zap, word: "Power" },
  TRANSMISSION: { icon: RadioTower, word: "Transmission" },
  RADIO: { icon: Wifi, word: "Radio" },
  CORE: { icon: Server, word: "Core" },
  ACCESS: { icon: Wifi, word: "Access" },
  ENVIRONMENT: { icon: Thermometer, word: "Environment" },
};
const domainOf = (d: unknown) => {
  const key = String(d ?? "").toUpperCase();
  return DOMAINS[key] || { icon: CircleHelp, word: humanEnum(key) ? humanEnum(key)[0].toUpperCase() + humanEnum(key).slice(1) : "Unknown" };
};

/** The lookback the backend wrote into the summary ("… (3x in 30d)"); 30 days when it says nothing. */
const lookbackOf = (summary: unknown) => {
  const m = /in (\d+)d\)/.exec(String(summary ?? ""));
  return m ? Number(m[1]) : 30;
};

/** At most this many dots, one per fault; the number beside them says the rest. */
const MAX_DOTS = 8;

export default function Problems({ tick, profile }: { tick: number; profile?: any }) {
  // null until the first answer, so loading never reads as "no problems".
  const [rows, setRows] = useState<any[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [retry, setRetry] = useState(0);
  // Site names from the tickets: a problem carries the site code only.
  const [names, setNames] = useState<Record<string, string>>({});

  useEffect(() => {
    let live = true;
    api
      .problems()
      .then((r) => {
        if (!live) return;
        setRows(Array.isArray(r) ? r : []);
        setFailed(false);
      })
      .catch(() => {
        if (live) setFailed(true);
      });
    api
      .incidents()
      .then((all) => {
        if (!live || !Array.isArray(all)) return;
        const out: Record<string, string> = {};
        for (const i of all) if (i?.site_id && i?.site_name) out[i.site_id] = i.site_name;
        setNames(out);
      })
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [tick, retry]);

  const list = rows || [];
  const faults = list.reduce((sum, p) => sum + (Number(p.occurrence_count) || 0), 0);
  const sites = new Set(list.map((p) => p.site_id)).size;
  const commonest = useMemo(() => {
    const by: Record<string, number> = {};
    for (const p of list) by[p.dominant_failure_domain] = (by[p.dominant_failure_domain] || 0) + 1;
    const top = Object.entries(by).sort((a, b) => b[1] - a[1])[0];
    return top ? domainOf(top[0]).word : null;
  }, [list]);

  return (
    <div className="pb">
      <div className="page-head">
        <div>
          <h1>Problems</h1>
          <p className="lead">Sites that keep failing for the same cause, so the cause gets fixed instead of the symptom.</p>
        </div>
      </div>

      {rows !== null && rows.length > 0 && (
        <dl className="pb-figures">
          <div>
            <dt>Open problems</dt>
            <dd>{list.length}</dd>
          </div>
          <div>
            <dt>Sites</dt>
            <dd>{sites}</dd>
          </div>
          <div>
            <dt>Faults behind them</dt>
            <dd>{faults}</dd>
          </div>
          <div>
            <dt>Commonest cause</dt>
            <dd className="pb-figure-word">{commonest ?? "—"}</dd>
          </div>
        </dl>
      )}

      {failed && rows === null && (
        <div className="panel empty" role="alert">
          Couldn't load the problem records.{" "}
          <button className="btn sm" onClick={() => setRetry((n) => n + 1)}>
            Retry
          </button>
        </div>
      )}

      {rows === null && !failed && (
        <div className="pb-grid" aria-busy="true">
          {Array.from({ length: 3 }, (_, i) => (
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

      {rows !== null && rows.length === 0 && (
        <section className="panel pb-empty" aria-labelledby="pb-empty-title">
          <div className="pb-rule" aria-hidden="true">
            <span className="pb-dot" />
            <span className="pb-dot" />
            <span className="pb-dot" />
            <span className="pb-arrow" />
            <span className="pb-made">problem</span>
          </div>
          <h2 id="pb-empty-title" className="panel-title">
            No site keeps failing yet
          </h2>
          <p>
            When a site fails three times in 30 days for the same cause (power, transmission or radio), the Recurrence agent
            opens a problem record here, and it stays open until the cause is fixed.
          </p>
          <p className="muted">
            To see one: inject the same alarm three times from <Link to="/settings">Settings</Link>, closing its ticket on the{" "}
            <Link to="/incidents">Incident board</Link> in between (an open ticket folds a repeat into itself).
          </p>
        </section>
      )}

      {list.length > 0 && (
        <div className="pb-grid">
          {list.map((p) => {
            const d = domainOf(p.dominant_failure_domain);
            const Icon = d.icon;
            const n = Number(p.occurrence_count) || 0;
            const days = lookbackOf(p.summary);
            const name = names[p.site_id];
            const monitoring = String(p.status).toUpperCase() === "MONITORING";
            return (
              <article key={p.id} className="panel pb-card" aria-labelledby={`pb-${p.id}`}>
                <header className="pb-card-head">
                  <span className="mono pb-num">{p.problem_number}</span>
                  <span className={"pb-status" + (monitoring ? " watching" : "")}>{monitoring ? "Monitoring" : "Open"}</span>
                </header>
                <h2 id={`pb-${p.id}`} className="pb-site">
                  {name || p.site_id}
                </h2>
                <p className="pb-where">
                  {name && <span className="mono">{p.site_id}</span>}
                  <span title={p.region_code}>{regionName(p.region_code, profile)}</span>
                </p>
                <div className="pb-count">
                  <span className="pb-n">{n}</span>
                  <span className="pb-n-words">
                    {n === 1 ? "fault" : "faults"} in {days} days
                  </span>
                  <span className="pb-dots" aria-hidden="true">
                    {Array.from({ length: Math.min(n, MAX_DOTS) }, (_, i) => (
                      <span key={i} className="pb-dot" />
                    ))}
                    {n > MAX_DOTS && <span className="pb-more">+{n - MAX_DOTS}</span>}
                  </span>
                </div>
                <p className="pb-cause">
                  <Icon size={16} strokeWidth={1.75} aria-hidden="true" />
                  <span>
                    {d.word} failed {n} {n === 1 ? "time" : "times"} here in {days} days.
                  </span>
                </p>
                <footer className="pb-foot">
                  <Link to={`/incidents?state=all&q=${encodeURIComponent(String(p.site_id ?? ""))}`}>
                    See its tickets<span className="sr-only"> for {name || p.site_id}</span>
                  </Link>
                </footer>
              </article>
            );
          })}
        </div>
      )}
    </div>
  );
}
