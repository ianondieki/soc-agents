import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
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
import { fmtDateTime, fmtEAT } from "../lib/time";
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
  latest: () => getJson<any>("/api/v1/support/complaints?limit=1"),
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
/** The three sections the header links to, in page order. */
const SECTIONS: { id: string; label: string }[] = [
  { id: "how", label: "How it works" },
  { id: "desks", label: "The desks" },
  { id: "evals", label: "Evals" },
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
            </div>
            <Dial run={run} dialRun={dialRun} counts={counts} load={runs} />
          </div>
        </section>

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
                <ol className="ld-ladder">
                  {LADDER.map((l) => (
                    <li key={l.level} className={"ld-rung" + (l.level === autonomy ? " current" : "")} aria-current={l.level === autonomy ? "true" : undefined}>
                      <strong>{l.label}</strong>
                      {l.text}
                      {l.level === autonomy && <span className="ld-rung-now">This deployment</span>}
                    </li>
                  ))}
                </ol>
              </div>
              <div>
                <h3>Never automated</h3>
                <ul className="ld-never">
                  <li>Wording that reaches management or leaves the building on a P1 or P2.</li>
                  <li>Overriding a priority or disputing an assignment.</li>
                  <li>Sending the shift handover.</li>
                  <li>Any change to a live network element. There is no such tool to call.</li>
                  <li>Any write into another system; each one waits for a named approval.</li>
                </ul>
              </div>
            </div>
          </div>
        </section>
      </main>

      <footer className="ld-foot">
        <div className="ld-wrap">
          <div className="ld-foot-cta">
            <div>
              <h2>Watch the agents work an alarm</h2>
              <p>Launch the heavy-rain storm on Mission control, watch the dial fill, then approve or reject the held broadcasts.</p>
            </div>
            <div className="ld-actions">
              <Link className="ld-btn primary" to="/mission">
                Open mission control
              </Link>
              <Link className="ld-btn secondary" to="/hitl">
                Open Approvals
              </Link>
            </div>
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
    caption = (
      <>
        <span>
          {site ? <span className="ld-mono">{site}</span> : "The latest alarm"} opened {ticket ? <span className="ld-mono">{ticket}</span> : "a ticket"} at{" "}
          <span className="ld-mono">{fmtEAT(run.started_at)}</span>; the agents took <span className="ld-mono">{fmtMs(took)}</span>
          {held ? (
            <>
              ; its broadcast is <span className="hitl">waiting for a person</span>.
            </>
          ) : (
            "."
          )}
        </span>
        {run.incident_id && (
          <Link className="ld-btn text" to={`/incidents/${run.incident_id}`}>
            Open the ticket
          </Link>
        )}
      </>
    );
  } else if (load.state === "error") {
    caption = (
      <span className="ld-note" role="alert">
        Couldn't load the latest run.
        <button type="button" className="ld-btn text" onClick={load.retry}>
          Retry
        </button>
      </span>
    );
  } else if (load.state === "ok" || load.state === "missing") {
    caption = <span>No alarm has been through the agents yet. Launch the storm on Mission control and watch the dial fill.</span>;
  }
  return (
    <figure className="ld-hero-figure">
      <AgentDial run={dialRun} counts={counts} ticket={ticket} tookMs={took} empty={!run && (load.state === "ok" || load.state === "missing")} />
      <figcaption className="ld-fig-caption" aria-live="polite">
        {caption}
      </figcaption>
    </figure>
  );
}

// ----------------------------------------------------- what changes for the floor --

/** Four moments, before and now. The "now" figure is drawn from the productivity rollup. */
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

  const rows: { node: string; before: string; now: string; fig: ReactNode; tone?: "hitl" }[] = [
    {
      node: "CORRELATE",
      before: "Search the ticket queue before raising a duplicate.",
      now: "A repeat alarm, or a site behind a failed HUB, folds into the ticket already open.",
      fig: p && processed > 0 ? `${fmtInt(absorbed)} of ${plural(processed, "alarm")} folded into an open ticket` : null,
    },
    {
      node: "TICKET",
      before: "Type the ticket into the UI, field by field.",
      now: "The INC number, every TT field, the narrative and the SLA clocks are filled before anyone opens the ticket.",
      fig: p && filled > 0 ? `${fmtInt(perTicket)} fields a ticket, ${fmtInt(filled)} filled so far` : null,
    },
    {
      node: "EXEC_BRIEF",
      before: "Answer the phone, again.",
      now: "Management reads a brief written the moment the ticket opens, instead of phoning the NOC.",
      fig: p && briefs > 0 ? plural(briefs, "brief written", "briefs written") : null,
    },
    {
      node: "HITL",
      before: "Decide, then write the broadcast, find the numbers, send.",
      now: "Decide. The SMS and email are drafted, addressed and held until a named person approves them.",
      fig:
        p && held > 0
          ? `${plural(held, "message")} held for approval${pending != null ? `, ${plural(pending, "decision")} waiting now` : ""}`
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
            Before
          </th>
          <th scope="col">Now</th>
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
              <td className="ld-m-before" data-label="Before">
                {r.before}
              </td>
              <td className="ld-m-now" data-label="Now">
                {r.now}
                <span className={"ld-m-fig" + (r.tone ? ` ${r.tone}` : "")}>{r.fig}</span>
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

// --------------------------------------------------------------- the desks --

function Desk({ cls, title, to, isNew, sub, children }: { cls: string; title: string; to: string; isNew?: boolean; sub: string; children: ReactNode }) {
  return (
    <article className={`ld-desk ${cls}`}>
      <h3>
        <Link to={to}>{title}</Link>
        {isNew && <span className="ld-new">new</span>}
      </h3>
      <p className="ld-desk-sub">{sub}</p>
      <div className="ld-desk-live">{children}</div>
    </article>
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
  const newest = open[0];
  const legend = counts ? (["P1", "P2", "P3", "P4"] as const).filter((k) => counts[k] > 0) : [];
  const label = total != null ? (total > 0 ? `${plural(total, "ticket")} open: ${legend.map((k) => `${counts![k]} ${k}`).join(", ")}` : "Nothing open") : "";
  return (
    <Desk cls="mission" title="Mission control" to="/mission" sub="The live board: open tickets by priority, the newest alarm through the agents, and the storm.">
      <div className="ld-desk-count">
        {total != null ? (total > 0 ? `${plural(total, "ticket")} open` : "Nothing open") : incidents.state === "error" ? <span className="ld-note">Couldn't load the board.</span> : ""}
      </div>
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
                <i className={k.toLowerCase()} style={{ background: `var(--${k.toLowerCase()})` }} />
                {counts[k]} {k}
              </span>
            ))}
          </div>
        </>
      )}
      <p className="ld-latest">
        {newest ? (
          <>
            Newest: <span className="ld-mono">{newest.incident_number}</span>, {newest.site_name || newest.site_id}, {regionName(newest.region_code, profile)},{" "}
            {humanEnum(newest.status)}
            {newest.assignee_name ? <>, owner <span className="who">{vendorName(newest.assignee_name)}</span></> : null}.
          </>
        ) : incidents.state === "ok" && total === 0 ? (
          "Launch the storm to put the first alarm through."
        ) : null}
      </p>
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
    <Desk cls="approvals" title="Approvals" to="/hitl" sub="Every P1 and P2 message, with its facts and reason, waiting for a named person to approve or reject it.">
      <div className={"ld-desk-count" + (count ? " hitl" : "")}>
        {count == null ? (hitl.state === "error" ? <span className="ld-note">Couldn't load the queue.</span> : "") : count > 0 ? `${count} waiting for a decision` : "No decisions waiting"}
      </div>
      {queue.length > 0 ? (
        <ul className="ld-queue" aria-label="Waiting longest first">
          {queue.slice(0, 3).map((h) => {
            const site = h.proposed_payload?.envelope?.area?.site_name || h.site_id;
            const audiences: string[] = Array.isArray(h.proposed_payload?.audiences) ? h.proposed_payload.audiences.map(audienceWord) : [];
            return (
              <li key={h.id} title={audiences.length ? `To the ${audiences.join(", ")}` : undefined}>
                <span className="ld-mono">{h.incident_number}</span>
                <span>{h.priority} {humanEnum(h.task_type).replace(/^approve /, "")}</span>
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
  const item = Array.isArray(latest.data?.items) ? latest.data.items[0] : null;
  return (
    <Desk
      cls="support"
      title="Support desk"
      to="/support"
      isNew
      sub="A customer's complaint, read and routed by agents: answered from the knowledge base, fixed through tools, or handed to a person with the reason."
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
      <p className="ld-support-foot">
        {m && Number(m.total) > 0 ? (
          <>
            {plural(Number(m.total), "complaint")} so far, {pct(m.resolution_rate)} resolved without a person.
            {item ? (
              <>
                {" "}
                Latest: <span className="ld-mono">{item.ref}</span>, {item.subject}, {item.route === "human" ? "with a person" : item.route === "action" ? "fixed by the action agent" : "answered by the resolver"}.
              </>
            ) : null}
          </>
        ) : m ? (
          <>
            No complaints yet. <Link className="ld-btn text" to="/complain">Register the first one</Link>
          </>
        ) : off ? (
          <>
            The desk is being wired up beside the NOC and is not switched on in this build; the form at <Link className="ld-btn text" to="/complain">/complain</Link> shows what a customer will see.
          </>
        ) : metrics.state === "error" ? (
          <span className="ld-note" role="alert">
            Couldn't reach the Support desk.
            <button type="button" className="ld-btn text" onClick={metrics.retry}>
              Retry
            </button>
          </span>
        ) : null}
      </p>
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
    <Desk cls="shift" title="Shift desk" to="/shift" sub="The ledger every ticket writes its own row to, and the handover a person sends at the end of the shift.">
      <div className="ld-desk-count">
        {ledger.state === "ok" ? `${plural(rows.length, "row")} in the ledger${shift ? `, ${shift} shift` : ""}` : ledger.state === "error" ? <span className="ld-note">Couldn't load the ledger.</span> : ""}
      </div>
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

// ------------------------------------------------------ where people decide --

const LADDER = [
  { level: "L1_COPILOT", label: "L1 co-pilot", text: "Agents draft everything; a person approves every send." },
  { level: "L2_GUARDED", label: "L2 guarded", text: "HUB and CORE tickets open on their own; P3 and P4 broadcasts go; P1 and P2 wait for a person." },
  { level: "L3_CONDITIONAL", label: "L3 conditional", text: "Only P1 waits. Note chasing and the handover run unattended. Still never a live network change." },
];
