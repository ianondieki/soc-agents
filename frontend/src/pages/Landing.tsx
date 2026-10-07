import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { Headset, LockKeyhole, Pause, Play, Plus, Radar, ScrollText, UserRoundCheck, type LucideIcon } from "lucide-react";
import { api } from "../api";
import CountUp from "../components/CountUp";
import AgentDial, { type DialRun } from "../components/landing/AgentDial";
import SupportFlow from "../components/landing/SupportFlow";
import { BrandMark } from "../components/shell/BrandMark";
import {
  alarmSite,
  audienceWord,
  displaySteps,
  fmtInt,
  fmtMinutes,
  fmtMs,
  humanAutonomy,
  humanEnum,
  humanStatus,
  normaliseStatus,
  opensTicket,
  regionName,
  stepsByNode,
  sumDurations,
  ticketNumberOf,
} from "../lib/agents";
import { FIBRES, fibreColour, fibreOf } from "../lib/fibre";
import { useRealtimeState } from "../realtime/RealtimeContext";
import { THEME_KEY, applyTheme, type Theme } from "../lib/theme";
import { fmtDateTime, fmtEAT, fmtHM } from "../lib/time";
import { useCalm } from "../lib/motion";
import { currentShift } from "../lib/shift";
import "./Landing.css";

/**
 * The front door at "/", outside the console shell: the first thing a manager sees. One bold
 * idea, the twelve agents on a dial beside the headline, and everything under it set
 * quietly. Every figure on the page is read from the API; where a call fails or a lane is not
 * switched on, the page says so in a sentence and never shows a number it did not get.
 */

// ------------------------------------------------------------------ loading --

type State = "loading" | "ok" | "missing" | "error";
interface Load<T> {
  data: T | null;
  state: State;
  retry: () => void;
}

/** `api.ts` throws `Error("404: ...")`; the landing's own fetches do the same. */
function isMissing(e: unknown): boolean {
  return /^404\b/.test(String((e as { message?: unknown })?.message ?? e));
}

async function getJson<T>(path: string): Promise<T> {
  const r = await fetch(path, { headers: { Accept: "application/json" } });
  if (!r.ok) throw new Error(`${r.status}: ${r.statusText}`);
  return (await r.json()) as T;
}

/** The support lane is not in api.ts yet (built in parallel): read it here, 404 reads as "missing". */
const support = {
  evals: () => getJson<any>("/api/v1/support/evals/latest"),
  metrics: () => getJson<any>("/api/v1/support/metrics?hours=0"),
  latest: () => getJson<any>("/api/v1/support/complaints?limit=3"),
};

/**
 * One fetch, kept honest: loading until the first answer, "missing" on a 404 (a lane that is
 * off), "error" when it failed before any answer; a failed refetch after a good one keeps the
 * data on screen. Refetches when `deps` move, debounced once it has loaded so a storm costs one
 * call per pause rather than one per alarm. Started a tick later, so React's development double
 * mount asks nothing.
 */
function useLoad<T>(fn: () => Promise<T>, deps: readonly unknown[], debounceMs = 0): Load<T> {
  const [data, setData] = useState<T | null>(null);
  const [state, setState] = useState<State>("loading");
  const [tick, setTick] = useState(0);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const loaded = useRef(false);
  useEffect(() => {
    let cancelled = false;
    const t = window.setTimeout(
      () => {
        fnRef
          .current()
          .then((d) => {
            if (cancelled) return;
            loaded.current = true;
            setData(d);
            setState("ok");
          })
          .catch((e) => {
            if (cancelled) return;
            if (isMissing(e)) {
              setData(null);
              setState("missing");
            } else {
              setState((s) => (s === "ok" ? s : "error"));
            }
          });
      },
      loaded.current ? debounceMs : 0
    );
    return () => {
      cancelled = true;
      window.clearTimeout(t);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);
  const retry = useCallback(() => {
    setState((s) => (s === "ok" ? s : "loading"));
    setTick((n) => n + 1);
  }, []);
  return { data, state, retry };
}

// -------------------------------------------------------------------- words --

/** "EGYPRO_FIBRE" → "Egypro Fibre": a vendor's name as a name, not a shout. */
function vendorName(v: unknown): string {
  const s = String(v ?? "").trim();
  if (!s) return "";
  if (/[a-z]/.test(s)) return s;
  return s
    .split(/[_\s]+/)
    .map((w) => (w.length <= 3 ? w : w[0] + w.slice(1).toLowerCase()))
    .join(" ");
}

function plural(n: number, word: string, pluralWord = `${word}s`): string {
  return `${fmtInt(n)} ${n === 1 ? word : pluralWord}`;
}

function pct(v: unknown): string {
  const n = Number(v);
  return Number.isFinite(n) ? `${Math.round(n * 1000) / 10}%` : "—";
}

/** The newest lifecycle run that opened a ticket (the dial's run), finished or still waiting. */
function latestTicketRun(rows: any[] | null): any | null {
  if (!Array.isArray(rows)) return null;
  const sorted = rows
    .filter((r) => r && (!r.graph_name || r.graph_name === "incident_lifecycle"))
    .sort((a, b) => String(b.started_at || "").localeCompare(String(a.started_at || "")));
  return sorted.find((r) => opensTicket(r) && String(r.status || "").toUpperCase() !== "RUNNING") ?? sorted.find(opensTicket) ?? null;
}

/** The press of the sun or moon: pin the other theme (the console's Display menu reads the same key). */
/** The sections the header links to, in page order. */
const SECTIONS: { id: string; label: string }[] = [
  { id: "how", label: "How it works" },
  { id: "desks", label: "The desks" },
  { id: "evals", label: "Evals" },
  { id: "faq", label: "Questions" },
];

/** Whether the page is hearing from the agents right now, and how many tickets are open. Read
 *  from the live stream's own state, so "Live" is only said while it is true. */
function LiveChip({ open }: { open: number | null }) {
  const rt = useRealtimeState();
  const link = rt?.link ?? "connecting";
  const word = link === "live" ? "Live" : link === "down" ? "Reconnecting" : "Connecting";
  return (
    <span className={`ld-live ${link}`} role="status" title={link === "live" ? "Live updates from the agents" : "Waiting for the live stream"}>
      <span className="ld-live-dot" aria-hidden="true" />
      <span className="ld-live-word">{word}</span>
      {open != null && (
        <span className="ld-live-count">
          {open} {open === 1 ? "ticket" : "tickets"} open
        </span>
      )}
    </span>
  );
}

function ThemeButton() {
  const [theme, setTheme] = useState<Theme>(() => {
    try {
      return document.documentElement.getAttribute("data-theme") === "day" ? "day" : "night";
    } catch {
      return "night";
    }
  });
  const flip = () => {
    const next: Theme = theme === "day" ? "night" : "day";
    try {
      window.localStorage.setItem(THEME_KEY, next);
    } catch {
      /* storage blocked: the choice lasts until the tab closes */
    }
    applyTheme(next);
    setTheme(next);
  };
  const label = theme === "day" ? "Switch to the night theme" : "Switch to the day theme";
  return (
    <button type="button" className="ld-icon-btn" onClick={flip} aria-label={label} title={label}>
      {theme === "day" ? (
        <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" aria-hidden="true">
          <path d="M15.5 12.2A6.5 6.5 0 0 1 7.8 4.5a6.5 6.5 0 1 0 7.7 7.7Z" />
        </svg>
      ) : (
        <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" aria-hidden="true">
          <circle cx="10" cy="10" r="3.4" />
          <path d="M10 2.5v2M10 15.5v2M2.5 10h2M15.5 10h2M4.7 4.7l1.4 1.4M13.9 13.9l1.4 1.4M4.7 15.3l1.4-1.4M13.9 6.1l1.4-1.4" />
        </svg>
      )}
    </button>
  );
}

// -------------------------------------------------------------------- page --

export default function Landing({ profile, metrics, runsRev }: { profile: any; metrics: any; runsRev: number }) {
  const runs = useLoad(() => api.lifecycleRuns(), [runsRev], 600);
  const productivity = useLoad(() => api.productivity(0), [runsRev], 600);
  const hitl = useLoad(() => api.hitl(), [runsRev], 600);
  const ledger = useLoad(() => api.ledger(), [runsRev], 600);
  const incidents = useLoad(() => api.incidents(), [runsRev], 600);
  const evals = useLoad(support.evals, []);
  const supportMetrics = useLoad(support.metrics, []);
  const supportLatest = useLoad(support.latest, []);

  const run = useMemo(() => latestTicketRun(runs.data), [runs.data]);
  const dialRun: DialRun | null = useMemo(
    () => (run ? { id: String(run.id), steps: displaySteps(run), status: run.status ?? null } : null),
    [run]
  );
  // Steps handled per agent on record, for the dial's bars: Ingest and Correlate see every alarm,
  // the rest only the ones that became tickets. Null until the rollup answers; no bars until then.
  const counts = useMemo(() => {
    const byNode = productivity.data?.steps?.by_node;
    if (!Array.isArray(byNode) || byNode.length === 0) return null;
    const out: Record<string, number> = {};
    for (const n of byNode) if (n?.node) out[String(n.node)] = Number(n.steps || 0);
    return out;
  }, [productivity.data]);
  const autonomy = String(profile?.autonomy_level || productivity.data?.autonomy_level || "L2_GUARDED");

  // The header lifts off the page (a frosted surface and a hairline) only once content scrolls
  // under it; at the top it sits flush with the hero.
  const [scrolled, setScrolled] = useState(false);
  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 4);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  // Which section the reader is in, for the header's links: the one crossing the upper middle of
  // the screen. None while the hero fills the view.
  const [section, setSection] = useState<string | null>(null);
  useEffect(() => {
    if (typeof IntersectionObserver === "undefined") return;
    const els = SECTIONS.map((s) => document.getElementById(s.id)).filter((el): el is HTMLElement => !!el);
    const seen = new Map<string, boolean>();
    const io = new IntersectionObserver(
      (entries) => {
        for (const e of entries) seen.set(e.target.id, e.isIntersecting);
        setSection(SECTIONS.find((s) => seen.get(s.id))?.id ?? null);
      },
      { rootMargin: "-35% 0px -60% 0px" }
    );
    els.forEach((el) => io.observe(el));
    return () => io.disconnect();
  }, []);

  return (
    <div className="landing">
      <a className="ld-skip" href="#main">
        Skip to content
      </a>
      <header className={"ld-top" + (scrolled ? " is-scrolled" : "")}>
        <div className="ld-wrap ld-top-row">
          <a className="ld-brand" href="/" aria-label="Kenya NOC Mission Control, front page">
            <BrandMark size={28} />
            <span className="ld-brand-name">Kenya NOC</span>
            <span className="ld-brand-sub">Mission Control</span>
          </a>
          <nav className="ld-topnav" aria-label="Sections">
            {SECTIONS.map((s) => (
              <a key={s.id} href={`#${s.id}`} className={section === s.id ? "is-active" : undefined} aria-current={section === s.id ? "location" : undefined}>
                {s.label}
              </a>
            ))}
          </nav>
          <div className="ld-top-actions">
            <LiveChip open={typeof metrics?.open_total === "number" ? metrics.open_total : null} />
            <ThemeButton />
            <Link className="ld-btn primary sm ld-top-cta" to="/mission">
              Open mission control
            </Link>
          </div>
        </div>
      </header>

      <main id="main">
        <section className="ld-hero" aria-labelledby="ld-h1">
          <div className="ld-wrap">
            <div className="ld-hero-copy">
              <p className="eyebrow">Network operations, Kenya</p>
              <h1 id="ld-h1">
                <span className="agents">Twelve agents work every alarm.</span> <span className="people">People make the call.</span>
              </h1>
              <p className="ld-lead">
                An alarm on the network becomes a filled-in ticket, a named vendor, a drafted broadcast and an exec brief in under a
                second. P1 and P2 messages wait for a person, and every step is on record.
              </p>
              <div className="ld-actions">
                <Link className="ld-btn primary" to="/mission">
                  Open mission control
                </Link>
                <Link className="ld-btn secondary" to="/complain">
                  Try the complaint desk
                </Link>
              </div>
              <HeroFigures p={productivity.data} />
            </div>
            <div className="ld-preview">
              <div className="ld-preview-card">
                <Dial run={run} dialRun={dialRun} counts={counts} load={runs} />
              </div>
              {typeof productivity.data?.pipeline_ms?.median === "number" && (
                <p className="ld-float a">
                  <span className="ld-float-dot" aria-hidden="true" />
                  Alarm to ticket: {fmtMs(productivity.data.pipeline_ms.median)}, median
                </p>
              )}
              {typeof metrics?.hitl_pending === "number" && metrics.hitl_pending > 0 && (
                <p className="ld-float b">
                  <span className="ld-float-dot hitl" aria-hidden="true" />
                  {fmtInt(metrics.hitl_pending)} waiting for a person
                </p>
              )}
            </div>
          </div>
        </section>

        <Tape incidents={incidents} profile={profile} />

        <section className="ld-section" id="how" aria-labelledby="ld-how">
          <div className="ld-wrap">
            <div className="ld-section-head">
              <h2 id="ld-how">What changes for the floor</h2>
              <p className="ld-section-lead">
                Four of the twelve steps, before and now. The figures are this deployment's own record; the Showcase has all twelve.
              </p>
            </div>
            <Moments p={productivity.data} waiting={typeof metrics?.hitl_pending === "number" ? metrics.hitl_pending : null} />
          </div>
        </section>

        <section className="ld-section" id="desks" aria-labelledby="ld-desks">
          <div className="ld-wrap">
            <div className="ld-section-head">
              <h2 id="ld-desks">Four desks, one record</h2>
              <p className="ld-section-lead">
                The floor works from Mission control, decides on Approvals, hands over from the Shift desk, and now answers customers from
                the Support desk. Each one below shows what it holds right now.
              </p>
            </div>
            <div className="ld-desks-grid">
              <SupportDesk metrics={supportMetrics} latest={supportLatest} />
              <MissionDesk metrics={metrics} incidents={incidents} profile={profile} />
              <ApprovalsDesk metrics={metrics} hitl={hitl} />
              <ShiftDesk ledger={ledger} profile={profile} />
            </div>
          </div>
        </section>

        <section className="ld-section" id="evals" aria-labelledby="ld-evals">
          <div className="ld-wrap">
            <div className="ld-section-head">
              <h2 id="ld-evals">Measured, not claimed</h2>
              <p className="ld-section-lead">
                The Support desk is scored on a labelled set of complaints, in English, Kiswahili and Sheng, against fixed gates. The NOC
                side reports what its agents did and what the floor says that would have cost by hand.
              </p>
            </div>
            <div className="ld-evals-grid">
              <Evals evals={evals} />
              <Saved p={productivity} />
            </div>
          </div>
        </section>

        <section className="ld-section" aria-labelledby="ld-decide">
          <div className="ld-wrap">
            <div className="ld-section-head">
              <h2 id="ld-decide">Where people decide</h2>
              <p className="ld-section-lead">
                This deployment runs at {humanAutonomy(autonomy)}. The level is one setting; what is never automated does not move with it.
              </p>
            </div>
            <div className="ld-decide-grid">
              <div>
                <h3>The autonomy ladder</h3>
                {/* Three rungs on one track: the line is lit up to this deployment's rung. */}
                <ol className="ld-ladder">
                  {LADDER.map((l, i) => {
                    const at = LADDER.findIndex((x) => x.level === autonomy);
                    const state = l.level === autonomy ? "current" : at >= 0 && i < at ? "below" : "above";
                    return (
                      <li key={l.level} className={`ld-rung ${state}`} aria-current={state === "current" ? "true" : undefined}>
                        <span className="ld-rung-dot" aria-hidden="true" />
                        <strong>{l.label}</strong>
                        <span className="ld-rung-text">{l.text}</span>
                        {state === "current" && <span className="ld-rung-now">This deployment</span>}
                      </li>
                    );
                  })}
                </ol>
              </div>
              <div className="ld-never-panel">
                <h3>Never automated, at any level</h3>
                <ul className="ld-never">
                  {NEVER.map((t) => (
                    <li key={t}>
                      <LockKeyhole size={16} strokeWidth={1.75} aria-hidden="true" />
                      <span>{t}</span>
                    </li>
                  ))}
                </ul>
              </div>
            </div>
          </div>
        </section>

        <section className="ld-section" id="faq" aria-labelledby="ld-faq">
          <div className="ld-wrap ld-faq-grid">
            <div className="ld-faq-head">
              <h2 id="ld-faq">Questions the floor asks</h2>
              <p className="ld-section-lead">What supervisors and duty managers ask in their first week, answered from how the console works.</p>
            </div>
            <div className="ld-faq">
              {FAQ.map((f) => (
                <details key={f.q} className="ld-faq-item">
                  <summary>
                    <span>{f.q}</span>
                    <Plus className="ld-faq-mark" size={18} strokeWidth={2} aria-hidden="true" />
                  </summary>
                  <div className="ld-faq-a">
                    <p>{f.a}</p>
                  </div>
                </details>
              ))}
            </div>
          </div>
        </section>
      </main>

      <footer className="ld-foot">
        <div className="ld-wrap">
          <div className="ld-foot-cta">
            <div>
              <h2>
                Watch the agents work an alarm. <span className="people">Then make the call.</span>
              </h2>
              <p>Launch the heavy-rain storm on Mission control, watch the dial fill, then approve or reject the held broadcasts.</p>
              <div className="ld-actions">
                <Link className="ld-btn primary" to="/mission">
                  Open mission control
                </Link>
                <Link className="ld-btn secondary" to="/hitl">
                  Open Approvals
                </Link>
              </div>
            </div>
            <Handover profile={profile} />
          </div>
          <div className="ld-foot-legal">
            <span>Demo and training product. Not an official Safaricom or Airtel system.</span>
            <nav className="ld-foot-links" aria-label="More pages">
              <Link to="/showcase">Showcase</Link>
              <Link to="/workflow">Workflow map</Link>
              <Link to="/agents">Agent observatory</Link>
              <Link to="/support">Support desk</Link>
            </nav>
          </div>
        </div>
      </footer>
    </div>
  );
}

// -------------------------------------------------------------------- hero --

function Dial({ run, dialRun, counts, load }: { run: any | null; dialRun: DialRun | null; counts: Record<string, number> | null; load: Load<any[]> }) {
  const ticket = run ? ticketNumberOf(run) : null;
  const took = run ? sumDurations(run.steps) : null;
  let caption: ReactNode = null;
  if (run && dialRun) {
    const site = alarmSite(run.steps);
    const held = normaliseStatus(stepsByNode(dialRun.steps).HITL?.status) === "waiting_hitl";
    // The latest alarm as four facts on one line, then its state in a sentence with the way in.
    caption = (
      <>
        <dl className="ld-fig-facts">
          <div>
            <dt>Latest alarm</dt>
            <dd className="ld-mono">{site || "an alarm"}</dd>
          </div>
          <div>
            <dt>Ticket</dt>
            <dd className="ld-mono">{ticket || "opened"}</dd>
          </div>
          <div>
            <dt>Opened</dt>
            <dd className="ld-mono">{fmtEAT(run.started_at)}</dd>
          </div>
          <div>
            <dt>The agents took</dt>
            <dd className="ld-mono">{fmtMs(took)}</dd>
          </div>
        </dl>
        <p className="ld-fig-state" aria-live="polite">
          {held ? <span className="hitl">Its broadcast is waiting for a person.</span> : <span>Every step is done and on record.</span>}
          {run.incident_id && (
            <Link className="ld-btn text" to={`/incidents/${run.incident_id}`}>
              Open the ticket
            </Link>
          )}
        </p>
      </>
    );
  } else if (load.state === "error") {
    caption = (
      <p className="ld-note" role="alert">
        Couldn't load the latest run.
        <button type="button" className="ld-btn text" onClick={load.retry}>
          Retry
        </button>
      </p>
    );
  } else if (load.state === "ok" || load.state === "missing") {
    caption = <p className="ld-fig-state">No alarm has been through the agents yet. Launch the storm on Mission control and watch the dial fill.</p>;
  }
  return (
    <figure className="ld-hero-figure">
      <AgentDial run={dialRun} counts={counts} ticket={ticket} tookMs={took} empty={!run && (load.state === "ok" || load.state === "missing")} />
      {/* How to read the dial, in the dial's own marks. */}
      <ul className="ld-dial-key" aria-label="How to read the dial">
        <li>
          <svg viewBox="0 0 22 10" aria-hidden="true">
            <path d="M1 8 Q 11 0 21 8" className="k-arc" />
          </svg>
          This alarm's path
        </li>
        {counts && (
          <li className="ld-key-bars">
            <svg viewBox="0 0 22 10" aria-hidden="true">
              <line x1="4" y1="5" x2="18" y2="5" className="k-bar" />
            </svg>
            Alarms each agent has worked
          </li>
        )}
        <li>
          <svg viewBox="0 0 22 10" aria-hidden="true">
            <circle cx="11" cy="5" r="3.6" className="k-hitl" />
          </svg>
          Waits for a person
        </li>
      </ul>
      <figcaption className="ld-fig-caption">{caption}</figcaption>
    </figure>
  );
}

// ----------------------------------------------------- what changes for the floor --

/** Four moments, before and now, each with what is on record for it. The figures are drawn from
 *  the productivity rollup; a figure not yet earned holds its room empty. */
function Moments({ p, waiting }: { p: any | null; waiting: number | null }) {
  const alarms = p?.alarms || {};
  const fields = p?.ticket_fields || {};
  const records = p?.records || {};
  const broadcasts = p?.broadcasts || {};
  const processed = Number(alarms.processed || 0);
  const absorbed = Math.max(0, Math.round(Number(alarms.absorbed) || 0));
  const perTicket = Number(fields.per_incident || 0);
  const filled = Number(fields.auto_filled || 0);
  const briefs = Number(records.exec_briefs || 0);
  const held = Number(broadcasts.held_for_approval || 0);
  const pending = waiting ?? (typeof p?.hitl?.pending === "number" ? p.hitl.pending : null);

  type Fig = { value: string; words: string; also?: string } | null;
  const rows: { node: string; before: string; now: string; fig: Fig; tone?: "hitl" }[] = [
    {
      node: "CORRELATE",
      before: "Search the ticket queue before raising a duplicate.",
      now: "A repeat alarm, or a site behind a failed HUB, folds into the ticket already open.",
      fig: p && processed > 0 ? { value: fmtInt(absorbed), words: `of ${plural(processed, "alarm")} folded into an open ticket` } : null,
    },
    {
      node: "TICKET",
      before: "Type the ticket into the UI, field by field.",
      now: "The INC number, every TT field, the narrative and the SLA clocks are filled before anyone opens the ticket.",
      fig: p && filled > 0 ? { value: fmtInt(filled), words: `fields filled, ${fmtInt(perTicket)} on every ticket` } : null,
    },
    {
      node: "EXEC_BRIEF",
      before: "Answer the phone, again.",
      now: "Management reads a brief written the moment the ticket opens, instead of phoning the NOC.",
      fig: p && briefs > 0 ? { value: fmtInt(briefs), words: briefs === 1 ? "brief written" : "briefs written" } : null,
    },
    {
      node: "HITL",
      before: "Decide, then write the broadcast, find the numbers, send.",
      now: "Decide. The SMS and email are drafted, addressed and held until a named person approves them.",
      fig:
        p && held > 0
          ? {
              value: fmtInt(held),
              words: held === 1 ? "message held for approval" : "messages held for approval",
              also: pending != null ? `${plural(pending, "decision")} waiting now` : undefined,
            }
          : null,
      tone: "hitl",
    },
  ];

  return (
    <table className="ld-moments">
      <thead>
        <tr>
          <th scope="col" className="ld-m-step">
            <span className="ld-sr">Step</span>
          </th>
          <th scope="col" className="ld-m-before">
            By hand, before
          </th>
          <th scope="col" className="ld-m-now">
            With the agents
          </th>
          <th scope="col" className="ld-m-fig">
            On record here
          </th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => {
          const f = fibreOf(r.node);
          return (
            <tr key={r.node}>
              <th scope="row" className="ld-m-step">
                <span className="ld-m-name">
                  {f && <span className={"ld-m-strand" + (f.outlined ? " outlined" : "")} style={{ ["--fibre" as string]: fibreColour(f) }} aria-hidden="true" />}
                  {f?.label}
                </span>
              </th>
              <td className="ld-m-before" data-label="By hand, before">
                {r.before}
              </td>
              <td className="ld-m-now" data-label="With the agents">
                {r.now}
              </td>
              <td className={"ld-m-fig" + (r.tone ? ` ${r.tone}` : "")} data-label="On record here">
                {r.fig ? (
                  <>
                    <span className="ld-m-value">{r.fig.value}</span>
                    <span className="ld-m-words">{r.fig.words}</span>
                    {r.fig.also && <span className="ld-m-also">{r.fig.also}</span>}
                  </>
                ) : (
                  <span className="ld-m-words none">{p ? "Nothing yet" : ""}</span>
                )}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

// --------------------------------------------------------------- the desks --

/**
 * A desk as a window onto it: its mark, its name and what it is for, the one figure it holds right
 * now (set in the display face), what it holds in the product's own marks, and the way in.
 */
function Desk({
  cls,
  title,
  icon: Icon,
  to,
  open,
  isNew,
  sub,
  figure,
  children,
}: {
  cls: string;
  title: string;
  icon: LucideIcon;
  to: string;
  open: string;
  isNew?: boolean;
  sub: string;
  figure: ReactNode;
  children?: ReactNode;
}) {
  return (
    <article className={`ld-desk ${cls}`} aria-labelledby={`ld-desk-${cls}`}>
      <header className="ld-desk-head">
        <span className="ld-desk-icon" aria-hidden="true">
          <Icon size={18} strokeWidth={1.75} />
        </span>
        <h3 id={`ld-desk-${cls}`}>{title}</h3>
        {isNew && <span className="ld-new">New</span>}
      </header>
      <p className="ld-desk-sub">{sub}</p>
      <div className="ld-desk-figure">{figure}</div>
      <div className="ld-desk-live">{children}</div>
      <footer className="ld-desk-foot">
        <Link className="ld-btn text" to={to}>
          {open}
        </Link>
      </footer>
    </article>
  );
}

/** A figure and its words: "<b>33</b> tickets open". */
function Fig({ n, words, tone }: { n: ReactNode; words: ReactNode; tone?: "hitl" }) {
  return (
    <p className={"ld-fig" + (tone ? ` ${tone}` : "")}>
      <b>{n}</b> <span>{words}</span>
    </p>
  );
}

function openIncidents(rows: any[] | null): any[] {
  if (!Array.isArray(rows)) return [];
  return rows
    .filter((r) => r && !["CLOSED", "CANCELLED"].includes(String(r.status || "").toUpperCase()))
    .sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
}

function MissionDesk({ metrics, incidents, profile }: { metrics: any; incidents: Load<any[]>; profile: any }) {
  const open = useMemo(() => openIncidents(incidents.data), [incidents.data]);
  const byP: Record<string, number> = metrics?.by_priority || {};
  const counts = metrics
    ? { P1: Number(byP.P1 || 0), P2: Number(byP.P2 || 0), P3: Number(byP.P3 || 0), P4: Number(byP.P4 || 0) }
    : incidents.state === "ok"
      ? open.reduce(
          (acc, r) => {
            const k = String(r.priority || "").toUpperCase();
            if (k in acc) acc[k as keyof typeof acc] += 1;
            return acc;
          },
          { P1: 0, P2: 0, P3: 0, P4: 0 }
        )
      : null;
  const total = counts ? counts.P1 + counts.P2 + counts.P3 + counts.P4 : null;
  const legend = counts ? (["P1", "P2", "P3", "P4"] as const).filter((k) => counts[k] > 0) : [];
  const label = total != null ? (total > 0 ? `${plural(total, "ticket")} open: ${legend.map((k) => `${counts![k]} ${k}`).join(", ")}` : "Nothing open") : "";
  // The newest tickets as the board lists them: priority, site, region and when.
  const newest = open.slice(0, 3);
  return (
    <Desk
      cls="mission"
      title="Mission control"
      icon={Radar}
      to="/mission"
      open="Open Mission control"
      sub="The live board: open tickets by priority, the newest alarm through the agents, and the storm."
      figure={
        total != null ? (
          total > 0 ? (
            <Fig n={fmtInt(total)} words={total === 1 ? "ticket open" : "tickets open"} />
          ) : (
            <Fig n="0" words="tickets open" />
          )
        ) : incidents.state === "error" ? (
          <span className="ld-note">Couldn't load the board.</span>
        ) : null
      }
    >
      {counts && total! > 0 && (
        <>
          <div className="ld-prio" role="img" aria-label={label}>
            {legend.map((k) => (
              <span key={k} className={k.toLowerCase()} style={{ flexGrow: counts[k] }} />
            ))}
          </div>
          <div className="ld-prio-legend" aria-hidden="true">
            {legend.map((k) => (
              <span key={k}>
                <i style={{ background: `var(--${k.toLowerCase()})` }} />
                {counts[k]} {k}
              </span>
            ))}
          </div>
        </>
      )}
      {newest.length > 0 ? (
        <ul className="ld-rows" aria-label="Newest tickets">
          {newest.map((i) => (
            <li key={i.id}>
              <span className={`ld-pill ${String(i.priority || "").toUpperCase()}`}>{i.priority}</span>
              <span className="site">{i.site_name || i.site_id}</span>
              <span className="where">{regionName(i.region_code, profile)}</span>
              <span className="since ld-mono">{fmtEAT(i.created_at)}</span>
            </li>
          ))}
        </ul>
      ) : incidents.state === "ok" && total === 0 ? (
        <p className="ld-latest">Launch the storm to put the first alarm through.</p>
      ) : null}
    </Desk>
  );
}

function ApprovalsDesk({ metrics, hitl }: { metrics: any; hitl: Load<any[]> }) {
  const queue = useMemo(
    () =>
      (Array.isArray(hitl.data) ? hitl.data : [])
        .filter((h) => h && String(h.status || "PENDING").toUpperCase() === "PENDING")
        .sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || ""))),
    [hitl.data]
  );
  const count: number | null = typeof metrics?.hitl_pending === "number" ? metrics.hitl_pending : hitl.state === "ok" ? queue.length : null;
  return (
    <Desk
      cls="approvals"
      title="Approvals"
      icon={UserRoundCheck}
      to="/hitl"
      open="Open Approvals"
      sub="Every P1 and P2 message, with its facts and reason, waiting for a named person to approve or reject it."
      figure={
        count == null ? (
          hitl.state === "error" ? <span className="ld-note">Couldn't load the queue.</span> : null
        ) : count > 0 ? (
          <Fig n={fmtInt(count)} words="waiting for a decision" tone="hitl" />
        ) : (
          <Fig n="0" words="decisions waiting" />
        )
      }
    >
      {queue.length > 0 ? (
        <ul className="ld-queue" aria-label="Newest first">
          {queue.slice(0, 3).map((h) => {
            const site = h.proposed_payload?.envelope?.area?.site_name || h.site_id;
            const audiences: string[] = Array.isArray(h.proposed_payload?.audiences) ? h.proposed_payload.audiences.map(audienceWord) : [];
            return (
              <li key={h.id} title={audiences.length ? `To the ${audiences.join(", ")}` : undefined}>
                <span className={`ld-pill ${String(h.priority || "").toUpperCase()}`}>{h.priority}</span>
                <span className="what">{humanEnum(h.task_type).replace(/^approve /, "")}</span>
                <span className="site">{site}</span>
                <span className="since ld-mono">{fmtEAT(h.created_at)}</span>
              </li>
            );
          })}
        </ul>
      ) : (
        count === 0 && <p className="ld-latest">P3 and P4 messages go on their own at this level; the next P1 or P2 will stop here.</p>
      )}
    </Desk>
  );
}

function SupportDesk({ metrics, latest }: { metrics: Load<any>; latest: Load<any> }) {
  const m = metrics.data;
  const off = metrics.state === "missing";
  // The counts appear only from a real answer; a lane that is off is said once, under the tree.
  const live = (text: ReactNode) => (m ? text : "");
  const c = (n: unknown) => fmtInt(Number(n || 0));
  const items: any[] = Array.isArray(latest.data?.items) ? latest.data.items.slice(0, 3) : [];
  const total = m ? Number(m.total) || 0 : 0;
  const routeWords = (r: unknown) => (r === "human" ? "with a person" : r === "action" ? "fixed by the action agent" : "answered by the resolver");
  return (
    <Desk
      cls="support"
      title="Support desk"
      icon={Headset}
      to="/support"
      open="Open the Support desk"
      isNew
      sub="A customer's complaint, read and routed by agents: answered from the knowledge base, fixed through tools, or handed to a person with the reason."
      figure={
        m && total > 0 ? (
          <Fig n={pct(m.resolution_rate)} words={`resolved without a person, of ${plural(total, "complaint")}`} />
        ) : metrics.state === "error" ? (
          <span className="ld-note" role="alert">
            Couldn't reach the Support desk.
            <button type="button" className="ld-btn text" onClick={metrics.retry}>
              Retry
            </button>
          </span>
        ) : null
      }
    >
      <SupportFlow
        columns={[
          { name: "Resolver", does: "Answers from the knowledge base and cites the article it used; escalates when nothing grounds the answer.", live: live(m ? `${c(m.auto_resolved)} answered` : "") },
          {
            name: "Action agent",
            does: "Refunds, M-PESA reversals, bundle re-credits and ticket updates through tools, within policy limits; a call over a limit waits for an approval.",
            live: live(m ? `${c(m.action_completed)} fixed${Number(m.awaiting_approval) > 0 ? `, ${c(m.awaiting_approval)} waiting for approval` : ""}` : ""),
          },
          {
            name: "A person",
            does: "Fraud, SIM swaps, legal or regulator mentions, threats, large refunds and repeat complaints go straight to the queue with a reason code.",
            live: live(m ? `${c(m.escalated)} waiting, ${c(m.human_resolved)} resolved` : ""),
          },
        ]}
      />
      {/* The newest complaints, in the customers' own words (the serif that marks a person), each
          with where it went. */}
      {items.length > 0 ? (
        <div className="ld-said">
          <h4 className="ld-said-title">Newest complaints</h4>
          <ul>
            {items.map((it) => (
              <li key={it.ref || it.id}>
                <blockquote>{it.subject}</blockquote>
                <p className={"ld-said-meta" + (it.route === "human" ? " hitl" : "")}>
                  <span className="ld-mono">{it.ref}</span> {routeWords(it.route)}
                </p>
              </li>
            ))}
          </ul>
        </div>
      ) : m && total === 0 ? (
        <p className="ld-support-foot">
          No complaints yet.{" "}
          <Link className="ld-btn text" to="/complain">
            Register the first one
          </Link>
        </p>
      ) : off ? (
        <p className="ld-support-foot">
          The desk is being wired up beside the NOC and is not switched on in this build; the form at{" "}
          <Link className="ld-btn text" to="/complain">
            /complain
          </Link>{" "}
          shows what a customer will see.
        </p>
      ) : null}
    </Desk>
  );
}

function ShiftDesk({ ledger, profile }: { ledger: Load<any[]>; profile: any }) {
  const rows = useMemo(
    () => (Array.isArray(ledger.data) ? ledger.data : []).slice().sort((a, b) => String(b.row_written_at || "").localeCompare(String(a.row_written_at || ""))),
    [ledger.data]
  );
  const shift = profile?.shift ? String(profile.shift).toLowerCase() : null;
  const newest = rows[0];
  const siteCode = newest ? String(newest.site || "").split(" ")[0] : "";
  const siteName = newest ? String(newest.site || "").slice(siteCode.length).trim() : "";
  return (
    <Desk
      cls="shift"
      title="Shift desk"
      icon={ScrollText}
      to="/shift"
      open="Open the Shift desk"
      sub="The ledger every ticket writes its own row to, and the handover a person sends at the end of the shift."
      figure={
        ledger.state === "ok" ? (
          <Fig n={fmtInt(rows.length)} words={`${rows.length === 1 ? "row" : "rows"} in the ledger${shift ? `, ${shift} shift` : ""}`} />
        ) : ledger.state === "error" ? (
          <span className="ld-note">Couldn't load the ledger.</span>
        ) : null
      }
    >
      {newest ? (
        <div className="ld-ledger-scroll">
          <table className="ld-ledger">
            <caption className="ld-sr">The newest row</caption>
            <thead>
              <tr>
                <th scope="col">Ticket</th>
                <th scope="col">Priority</th>
                <th scope="col">Site</th>
                <th scope="col">Region</th>
                <th scope="col">Owner</th>
                <th scope="col">Status</th>
                <th scope="col">Written</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td data-label="Ticket">
                  <span className="v ld-mono">{newest.incident_number}</span>
                </td>
                <td data-label="Priority">
                  <span className="v">
                    <span className={`ld-pill ${String(newest.priority || "").toUpperCase()}`}>{newest.priority}</span>
                  </span>
                </td>
                <td className="site" data-label="Site">
                  <span className="v">
                    <span className="ld-mono">{siteCode}</span> {siteName}
                  </span>
                </td>
                <td data-label="Region">
                  <span className="v">{regionName(newest.region_code, profile)}</span>
                </td>
                <td data-label="Owner">
                  <span className="v">{vendorName(newest.owner)}</span>
                </td>
                <td data-label="Status">
                  <span className="v">{humanEnum(newest.status)}</span>
                </td>
                <td data-label="Written">
                  <span className="v ld-mono">{fmtEAT(newest.row_written_at)}</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      ) : (
        ledger.state === "ok" && <p className="ld-latest">The first ticket writes the first row.</p>
      )}
    </Desk>
  );
}

// --------------------------------------------------------------- the evals --

interface GateDef {
  key: string;
  name: string;
  op: ">=" | "<=" | "==";
  threshold: number;
  def: string;
  gateWords: string;
}
const GATES: GateDef[] = [
  {
    key: "resolution_rate",
    name: "Resolution rate",
    op: ">=",
    threshold: 0.8,
    def: "Of the complaints the gold set marks resolvable, the share the desk resolved correctly without a person: the right route, and the right article cited or the right tool called.",
    gateWords: "at least 80%",
  },
  {
    key: "wrong_escalation_rate",
    name: "Wrong-escalation rate",
    op: "<=",
    threshold: 0.1,
    def: "Of the complaints sent to a person, the share that did not need one.",
    gateWords: "at most 10%",
  },
  {
    key: "missed_escalation_rate",
    name: "Missed escalations on safety cases",
    op: "==",
    threshold: 0,
    def: "Fraud, SIM swap, legal, regulator and threat cases the desk kept instead of handing to a person. The dangerous error, so the gate is none.",
    gateWords: "none",
  },
];

function Evals({ evals }: { evals: Load<any> }) {
  const r = evals.data;
  const gates: any[] = Array.isArray(r?.gates) ? r.gates : [];
  return (
    <div className="ld-evals-col">
      <h3>Support desk evals</h3>
      <p className="ld-evals-sub">Resolution rate, wrong-escalation rate and missed escalations, each against its gate.</p>
      {r ? (
        <>
          <ul className="ld-gates">
            {GATES.map((g) => {
              const gate = gates.find((x) => x?.metric === g.key || (g.key === "missed_escalation_rate" && String(x?.metric || "").startsWith("missed_escalation")));
              const value = Number(gate?.value ?? r.metrics?.[g.key]);
              const threshold = Number(gate?.threshold ?? g.threshold);
              const passed = typeof gate?.passed === "boolean" ? gate.passed : g.op === ">=" ? value >= threshold : g.op === "<=" ? value <= threshold : value === threshold;
              const width = Number.isFinite(value) ? Math.max(0, Math.min(1, value)) * 100 : 0;
              return (
                <li key={g.key}>
                  <div className="ld-gate-head">
                    <span className="ld-gate-name">{g.name}</span>
                    <span className={"ld-gate-verdict " + (passed ? "ok" : "bad")}>{passed ? "passes the gate" : "fails the gate"}</span>
                    <span className="ld-gate-val">{Number.isFinite(value) ? pct(value) : "—"}</span>
                  </div>
                  <div className="ld-gate-bar" role="img" aria-label={`${g.name} ${pct(value)}, gate ${g.gateWords}`}>
                    <div className={"ld-gate-fill" + (passed ? "" : " bad")} style={{ width: `${width}%` }} />
                    <div className="ld-gate-tick" style={{ left: `${Math.max(0, Math.min(1, threshold)) * 100}%` }} />
                  </div>
                  <p className="ld-gate-def">
                    {g.def} <span className="gate">Gate: {g.gateWords}.</span>
                  </p>
                </li>
              );
            })}
          </ul>
          <p className="ld-eval-meta">
            {r.dataset?.size ? <>{plural(Number(r.dataset.size), r.dataset.split === "holdout" ? "blind holdout case" : "labelled case")}</> : "Labelled set"}
            {r.dataset?.split === "holdout" ? <> written by an author who never saw the code</> : null}
            {r.dataset?.name ? <>, {r.dataset.name}{r.dataset.version ? ` v${r.dataset.version}` : ""}</> : null}; {r.mode === "llm" ? "with the LLM" : "deterministic"}; ran{" "}
            <span className="ld-mono">{fmtDateTime(r.ran_at)}</span> EAT
            {typeof r.metrics?.p50_ms === "number" ? <>; median {fmtMs(r.metrics.p50_ms)} a case</> : null}.{" "}
            <Link className="ld-btn text" to="/support">
              Open the report
            </Link>
          </p>
        </>
      ) : (
        <>
          <ul className="ld-defs">
            {GATES.map((g) => (
              <li key={g.key}>
                <strong>{g.name}</strong>
                <p>
                  {g.def} <span className="gate">Gate: {g.gateWords}.</span>
                </p>
              </li>
            ))}
          </ul>
          <p className="ld-evals-action">
            {evals.state === "loading" ? (
              <span className="ld-note">Looking for the latest report.</span>
            ) : evals.state === "error" ? (
              <span className="ld-note" role="alert">
                Couldn't reach the eval report.
                <button type="button" className="ld-btn text" onClick={evals.retry}>
                  Retry
                </button>
              </span>
            ) : (
              <>
                <span className="ld-note">No eval report yet. </span>
                <Link className="ld-btn text" to="/support">
                  Run the evals on the Support desk
                </Link>
              </>
            )}
          </p>
        </>
      )}
    </div>
  );
}

function Saved({ p }: { p: Load<any> }) {
  const d = p.data;
  const toil = d?.toil || {};
  const alarms = d?.alarms || {};
  const steps = d?.steps || {};
  const pipe = d?.pipeline_ms || {};
  const processed = Number(alarms.processed || 0);
  const net = Math.max(0, Number(toil.net_minutes_saved) || 0);
  return (
    <div className="ld-evals-col">
      <h3>The NOC side</h3>
      <p className="ld-evals-sub">What the agents have done on this deployment, all time, and the analyst minutes the floor says each step takes by hand.</p>
      <div className="ld-saved" aria-live="polite">
        {d && processed > 0 ? (
          <>
            <span className="ld-saved-value">{fmtMinutes(net)}</span>
            <span className="ld-saved-label">of analyst work taken over, after the time people spent deciding</span>
            <ul className="ld-saved-facts">
              <li>
                <span className="ld-mono">{fmtInt(processed)}</span> alarms, <span className="ld-mono">{fmtInt(Number(alarms.incidents_created || 0))}</span> tickets opened,{" "}
                <span className="ld-mono">{fmtInt(Number(steps.total || 0))}</span> agent steps on record.
              </li>
              {typeof toil.minutes_saved_per_alarm === "number" && (
                <li>
                  About <span className="ld-mono">{fmtMinutes(toil.minutes_saved_per_alarm)}</span> of hand work an alarm.
                </li>
              )}
              {typeof pipe.median === "number" && (
                <li>
                  The agents' own work: median <span className="ld-mono">{fmtMs(pipe.median)}</span> a run, <span className="ld-mono">{fmtMs(pipe.p95)}</span> at the 95th percentile.
                </li>
              )}
            </ul>
            <p className="ld-saved-note">Minutes by hand are the operator profile's estimates, to be corrected with the floor; they are not a stopwatch study.</p>
          </>
        ) : d ? (
          <p className="ld-note">No alarms on record yet. Launch the storm on Mission control and the figures appear here.</p>
        ) : p.state === "error" ? (
          <p className="ld-note" role="alert">
            Couldn't load the numbers.
            <button type="button" className="ld-btn text" onClick={p.retry}>
              Retry
            </button>
          </p>
        ) : (
          <span className="ld-saved-value" aria-hidden="true">
            &nbsp;
          </span>
        )}
      </div>
    </div>
  );
}

// ------------------------------------------------------------ hero extras --

/** Three figures under the hero's actions, counting up once: the agents' record so far. The
 *  hours are the floor's own estimate (the Saved panel says how it is made). */
function HeroFigures({ p }: { p: any | null }) {
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

function Tape({ incidents, profile }: { incidents: Load<any[]>; profile: any }) {
  const calm = useCalm();
  const [paused, setPaused] = useState(false);
  const windowRef = useRef<HTMLDivElement>(null);
  const rows = useMemo(() => openIncidents(incidents.data).slice(0, 12), [incidents.data]);
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

// ------------------------------------------------------------------ footer --

const pad2 = (n: number) => String(n).padStart(2, "0");

/** The next shift handover as a countdown, from the operator's shift hours. It ticks each second;
 *  calm, it shows hours and minutes only and moves on the half minute. */
function Handover({ profile }: { profile: any }) {
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

// --------------------------------------------------------------- questions --

/** What the floor asks in its first week, answered from how the console works. */
const FAQ: { q: string; a: string }[] = [
  {
    q: "Does an agent ever send a message on its own?",
    a: "Only where the autonomy level allows it. At L2 guarded, P3 and P4 broadcasts go on their own; every P1 and P2 message waits at Approval until a named person approves or rejects it. Wording that reaches management or leaves the building on a P1 or P2 is never automated, at any level.",
  },
  {
    q: "Where is the record kept?",
    a: "Every step an agent takes is written to the audit trail with the agent, the time and its reason. A person's decision stays on the approval with their name and the time, and as a note on the ticket's timeline. Each ticket's row also goes into the Excel shift ledger, in EAT, and the Workflow map reads any recent alarm back step by step.",
  },
  {
    q: "How are the minutes saved worked out?",
    a: "From the floor's own estimate of a person's minutes per step, set in the operator profile, multiplied by the steps the agents completed. It is a model to be corrected with the floor, not a stopwatch study, and the Showcase shows the sum step by step.",
  },
  {
    q: "How is the Support desk scored?",
    a: "On a labelled set of complaints in English, Kiswahili and Sheng, against fixed gates for resolution, wrong escalations and missed escalations on safety cases. The headline is a blind holdout written by someone who never saw the code.",
  },
  {
    q: "Can the floor turn the noise down at night?",
    a: "Yes. Quiet mode, in the Display menu, stops animations and routine ticker lines while P1 and P2 tickets and decisions stay live. Alerts can buzz and sound for every event, for alarms only, or not at all.",
  },
  {
    q: "Is this a live operator system?",
    a: "No. It is a demo and training product. It is not an official Safaricom or Airtel system, and its sites, tickets and complaints are test data.",
  },
];

// ------------------------------------------------------ where people decide --

/** What no autonomy level hands to the agents: a person always does these. */
const NEVER = [
  "Wording that reaches management or leaves the building on a P1 or P2.",
  "Overriding a priority or disputing an assignment.",
  "Sending the shift handover.",
  "Any change to a live network element. There is no such tool to call.",
  "Any write into another system; each one waits for a named approval.",
];

const LADDER = [
  { level: "L1_COPILOT", label: "L1 co-pilot", text: "Agents draft everything; a person approves every send." },
  { level: "L2_GUARDED", label: "L2 guarded", text: "HUB and CORE tickets open on their own; P3 and P4 broadcasts go; P1 and P2 wait for a person." },
  { level: "L3_CONDITIONAL", label: "L3 conditional", text: "Only P1 waits. Note chasing and the handover run unattended. Still never a live network change." },
];
