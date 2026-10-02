import { NavLink, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { Suspense, lazy, useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import { useRealtime } from "./realtime/useRealtime";
import { RealtimeProvider, useSuppressedCount } from "./realtime/RealtimeContext";
import {
  STORM_DONE_KEY,
  STORM_IDLE,
  fetchStormTemplates,
  isStorming,
  runStorm,
  stormFailureText,
  stormLine,
  stormStopLine,
  type StormState,
} from "./lib/demo";
import { detailOf } from "./lib/apiError";
import { WAITING_WORD, humanAutonomy, humanEnum, regionName } from "./lib/agents";

// Every page is its own chunk: the first visit downloads the shell and the one page it opened,
// not all seventeen. A page's own stylesheet (Audit.css, Wallboard.escalation.css…) travels
// with its chunk, and Vite holds the page until that stylesheet has loaded.
const MissionControl = lazy(() => import("./pages/MissionControl"));
const IncidentBoard = lazy(() => import("./pages/IncidentBoard"));
const IncidentWorkspace = lazy(() => import("./pages/IncidentWorkspace"));
const HitlInbox = lazy(() => import("./pages/HitlInbox"));
const ShiftDesk = lazy(() => import("./pages/ShiftDesk"));
const Wallboard = lazy(() => import("./pages/Wallboard"));
const Agents = lazy(() => import("./pages/Agents"));
const Problems = lazy(() => import("./pages/Problems"));
const Regions = lazy(() => import("./pages/Regions"));
const Maintenance = lazy(() => import("./pages/Maintenance"));
const Audit = lazy(() => import("./pages/Audit"));
const Settings = lazy(() => import("./pages/Settings"));
const WorkflowMap = lazy(() => import("./pages/WorkflowMap"));
const Contracts = lazy(() => import("./pages/Contracts"));
const Pirs = lazy(() => import("./pages/Pirs"));
const Scorecards = lazy(() => import("./pages/Scorecards"));
const Showcase = lazy(() => import("./pages/Showcase"));
// The guide bar is closed on almost every first paint: its chunk arrives beside the page's,
// behind its own null fallback, instead of in the shell.
const DemoGuide = lazy(() => import("./components/DemoGuide"));

/** What a route shows while its chunk arrives: a page head and a first panel of skeleton rows,
 *  sized like the real ones (see the perf block at the top of styles.css), so the page lands in
 *  the space already held for it. */
function PageSkeleton() {
  return (
    <div className="page-skeleton" role="status">
      <span className="sr-only">Loading</span>
      <div className="page-head" aria-hidden="true">
        <div>
          <span className="skeleton skeleton-title" />
          <span className="skeleton skeleton-lead" />
        </div>
      </div>
      <div className="panel" aria-hidden="true">
        <div className="skeleton-rows">
          {["72%", "58%", "66%", "50%", "62%", "56%"].map((w, i) => (
            <span key={i} className="skeleton" style={{ width: w }} />
          ))}
        </div>
      </div>
    </div>
  );
}

/** The sidebar, grouped by who reaches for it: the shift, the agent story, quality, vendors,
 *  platform. Each label is its page's title, in sentence case. */
const NAV_GROUPS: { title: string; links: { to: string; label: string; end?: boolean }[] }[] = [
  {
    title: "Operate",
    links: [
      { to: "/", label: "Mission control", end: true },
      { to: "/incidents", label: "Incident board" },
      { to: "/hitl", label: "Approvals" },
      { to: "/shift", label: "Shift desk" },
      { to: "/wallboard", label: "Wallboard" },
    ],
  },
  {
    title: "Agents",
    links: [
      { to: "/showcase", label: "Showcase" },
      { to: "/agents", label: "Agent observatory" },
      { to: "/workflow", label: "Workflow map" },
    ],
  },
  {
    title: "Quality",
    links: [
      { to: "/problems", label: "Problems" },
      { to: "/pirs", label: "Post-incident reviews" },
      { to: "/regions", label: "Regions" },
    ],
  },
  {
    title: "Vendors",
    links: [
      { to: "/scorecards", label: "Vendor scorecards" },
      { to: "/contracts", label: "Contracts" },
      { to: "/maintenance", label: "Maintenance" },
    ],
  },
  {
    title: "Platform",
    links: [
      { to: "/audit", label: "Audit trail" },
      { to: "/settings", label: "Settings" },
    ],
  },
];

const SCROLLERS = ".list, .ticker, .table-scroll, .hitl-channel-body, .hitl-field-pre, .panel, .pre";

function useScrollableRegions(pathname: string, revisions: unknown) {
  useEffect(() => {
    let raf = 0;
    const mark = () => {
      for (const el of Array.from(document.querySelectorAll<HTMLElement>(SCROLLERS))) {
        const scrolls = el.scrollHeight > el.clientHeight + 1 || el.scrollWidth > el.clientWidth + 1;
        if (scrolls) {
          if (!el.hasAttribute("tabindex")) el.setAttribute("tabindex", "0");
          if (!el.hasAttribute("aria-label")) {
            // The region's own title (a panel title is an h2.panel-title) or its first sub-section.
            const head = el.querySelector(".panel-title, h3, h4")?.textContent?.trim();
            if (head) el.setAttribute("aria-label", head);
          }
        } else if (el.getAttribute("tabindex") === "0" && !el.hasAttribute("data-keep-tab")) {
          el.removeAttribute("tabindex");
        }
      }
    };
    const schedule = () => {
      window.cancelAnimationFrame(raf);
      raf = window.requestAnimationFrame(mark);
    };
    const t1 = window.setTimeout(schedule, 50);
    const t2 = window.setTimeout(schedule, 900); // after the page's data has arrived
    // A lazily loaded page on a slow link can land after both timers, and its lists fill later
    // still: re-mark when the content under #main changes, at most every 400 ms.
    let pending = 0;
    const throttled = () => {
      if (pending) return;
      pending = window.setTimeout(() => {
        pending = 0;
        schedule();
      }, 400);
    };
    const main = document.getElementById("main");
    const mo = main && typeof MutationObserver !== "undefined" ? new MutationObserver(throttled) : null;
    if (mo && main) mo.observe(main, { childList: true, subtree: true });
    window.addEventListener("resize", schedule);
    return () => {
      window.clearTimeout(t1);
      window.clearTimeout(t2);
      window.clearTimeout(pending);
      mo?.disconnect();
      window.cancelAnimationFrame(raf);
      window.removeEventListener("resize", schedule);
    };
  }, [pathname, revisions]);
}

/** Quiet mode's top-bar toggle. Its own component, so the held-back count it names re-renders
 *  this button only, never App. */
function QuietToggle({ on, onToggle }: { on: boolean; onToggle: () => void }) {
  const suppressed = useSuppressedCount();
  return (
    <button
      type="button"
      className="btn sm chip-wide"
      onClick={onToggle}
      aria-pressed={on}
      title={
        "Night shift: stop animations and non-critical ticker churn. P1 and P2 tickets and decisions stay live." +
        (on && suppressed > 0 ? ` ${suppressed} non-critical lines held back.` : "")
      }
    >
      {on ? "Quiet mode on" : "Quiet mode"}
    </button>
  );
}

/** The Showcase's live panel reads the frame feed from the realtime context; its old `events`
 *  prop is fed a constant so the page does not re-render for every ticker line. */

function prefersReducedMotion(): boolean {
  try {
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  } catch {
    return false;
  }
}

/** What each autonomy level lets the agents do on their own: the top bar's tooltip on "L2 guarded". */
const AUTONOMY_MEANING: Record<string, string> = {
  L1_COPILOT: "Autonomy L1: agents draft every ticket and message; a person approves each one before it goes.",
  L2_GUARDED: "Autonomy L2: agents open tickets and send P3–P4 messages; P1 and P2 wait for a person.",
  L3_CONDITIONAL: "Autonomy L3: agents send everything except P1 messages, which wait for a person.",
};

/** How long the rail takes to replay the last run (12 steps at ~100 ms) before "Storm complete". */
const RAIL_SETTLE_MS = 1400;

export default function App() {
  const [profile, setProfile] = useState<any>(null);
  const [metrics, setMetrics] = useState<any>(null);
  const [session, setSession] = useState<any>({ display_name: "NOC Analyst", role: "noc_analyst" });
  const [apiOk, setApiOk] = useState(true);
  const [manualTick, setManualTick] = useState(0);
  const [navOpen, setNavOpen] = useState(false);
  const [guideOpen, setGuideOpen] = useState(false);
  const nav = useNavigate();
  const loc = useLocation();

  // The ticker lines and the run frames live in realtime.feed (an external store read by the few
  // components that show them), so App re-renders for a debounced flush, never for a frame.
  const realtime = useRealtime();
  const { connected, link, revisions, quietMode, setQuietMode, feed } = realtime;
  const connectedRef = useRef(connected);
  connectedRef.current = connected;

  // The 8-second heartbeat refetches the metrics only; the lists follow the WS revisions, and
  // fall back to the heartbeat only while the stream is down (manualTick below).
  const refresh = useCallback(() => {
    api
      .metrics()
      .then((m) => {
        setMetrics(m);
        setApiOk(true);
      })
      .catch(() => setApiOk(false));
  }, []);
  const loadIdentity = useCallback(() => {
    api.profile().then(setProfile).catch(() => undefined);
    api.session().then(setSession).catch(() => undefined);
  }, []);

  useEffect(() => {
    loadIdentity();
    refresh();
    const id = window.setInterval(() => {
      refresh();
      if (!connectedRef.current) setManualTick((t) => t + 1);
    }, 8000);
    return () => window.clearInterval(id);
  }, [refresh, loadIdentity]);

  // Outage recovery: when the stream reconnects or the API answers again, every list refetches
  // once (it missed the frames in between). The first connection is not a recovery: the pages'
  // own mount loads cover it.
  const upRef = useRef({ connected, apiOk, wasConnected: false });
  useEffect(() => {
    const prev = upRef.current;
    const reconnected = connected && !prev.connected && prev.wasConnected;
    const apiBack = apiOk && !prev.apiOk;
    if (reconnected || apiBack) {
      setManualTick((t) => t + 1);
      refresh();
      if (apiBack) loadIdentity();
    }
    upRef.current = { connected, apiOk, wasConnected: prev.wasConnected || connected };
  }, [connected, apiOk, refresh, loadIdentity]);

  // Defect #26: the KPI row used to refetch on *every* WS frame. It now refetches
  // once per debounced flush of the `metrics` slice — and only for event types
  // that can actually move a count (see realtime/renderers.ts).
  useEffect(() => {
    if (revisions.metrics === 0) return; // the mount load is refresh()'s job
    api.metrics().then(setMetrics).catch(() => undefined);
  }, [revisions.metrics]);

  // Quiet mode is signalled on <html> so it also covers the standalone
  // /wallboard route, which renders its own tree. CSS only suppresses
  // animation; nothing is hidden and no data stops flowing.
  useEffect(() => {
    try {
      const root = document.documentElement;
      if (quietMode) root.setAttribute("data-quiet", "on");
      else root.removeAttribute("data-quiet");
    } catch {
      /* ignore */
    }
  }, [quietMode]);

  // The phone menu closes on every route change.
  useEffect(() => {
    setNavOpen(false);
  }, [loc.pathname]);

  // A region that scrolls must be reachable from the keyboard (WCAG 2.1.1). Lists, tickers,
  // message bodies and, on a phone, panels holding a wide table all scroll; which ones do
  // depends on the data and the viewport, so they are found after render rather than marked
  // by hand in thirty places. Non-scrolling containers stay out of the tab order.
  useScrollableRegions(loc.pathname, revisions);

  // --- the live storm, owned here so the guide, Mission control and Settings share one ------
  // Nothing starts it but a press of Launch: that press is the opening beat of the demo.
  const [storm, setStorm] = useState<StormState>(STORM_IDLE);
  const stormRun = useRef<{ running: boolean; templates: any[] | null; next: number }>({ running: false, templates: null, next: 0 });
  const quietRef = useRef(quietMode);
  quietRef.current = quietMode;
  const profileRef = useRef<any>(profile);
  profileRef.current = profile;
  const storming = isStorming(storm);

  const runStormFrom = useCallback(
    async (resume: boolean) => {
      const run = stormRun.current;
      if (run.running) return;
      run.running = true;
      if (!resume || !run.templates) {
        run.templates = null;
        run.next = 0;
        setStorm({ ...STORM_IDLE, phase: "starting", text: "Starting the heavy-rain storm" });
      } else {
        setStorm((s) => ({ ...s, phase: "running", error: "", text: `Resuming at alarm ${run.next + 1} of ${s.total}` }));
      }
      try {
        if (!run.templates) run.templates = await fetchStormTemplates();
        const total = run.templates.length;
        setStorm((s) => ({ ...s, total, phase: "running", text: s.phase === "starting" ? "Starting the heavy-rain storm" : s.text }));
        await runStorm({
          templates: run.templates,
          from: run.next,
          onAlarm: (i, n, r) => {
            run.next = i;
            const inc = r.incident || {};
            const owner = inc.responsible_msp || inc.msp_name || inc.assignee_name;
            const text =
              r.outcome === "opened"
                ? `Alarm ${i} of ${n}: ${inc.incident_number || "a ticket"} opened at ${r.site}` +
                  `${inc.region_code ? ` (${regionName(inc.region_code, profileRef.current)})` : ""}, ${humanEnum(inc.failure_domain) || "fault"}${owner ? `, owner ${owner}` : ""}.`
                : `Alarm ${i} of ${n}: ${r.site} folded into ${r.into || "an open ticket"} at Correlate.`;
            setStorm((s) => ({
              ...s,
              done: i,
              opened: s.opened + (r.outcome === "opened" ? 1 : 0),
              folded: s.folded + (r.outcome === "folded" ? 1 : 0),
              text,
              firstIncidentId: s.firstIncidentId || (r.outcome === "opened" ? inc.id ?? null : null),
              lastOpenedId: r.outcome === "opened" && inc.id ? inc.id : s.lastOpenedId,
            }));
            refresh();
            // With the stream up the WS revisions refetch the lists; only without it does the
            // storm nudge them itself.
            if (!connectedRef.current) setManualTick((t) => t + 1);
          },
        });
        // The rail replays the last run hop by hop; "Storm complete" waits until it has.
        setStorm((s) => ({ ...s, phase: "settling" }));
        if (!quietRef.current && !prefersReducedMotion()) await new Promise((r) => window.setTimeout(r, RAIL_SETTLE_MS));
        setStorm((s) => ({ ...s, phase: "done", text: "" }));
        try {
          sessionStorage.setItem(STORM_DONE_KEY, "1");
        } catch {
          /* storage blocked: "storm done" falls back to "an incident is open" */
        }
        refresh();
        if (!connectedRef.current) setManualTick((t) => t + 1);
      } catch (e) {
        setStorm((s) => ({ ...s, phase: "failed", error: stormFailureText(detailOf(e, "The request failed.")) }));
      } finally {
        run.running = false;
      }
    },
    [refresh]
  );
  const launchStorm = useCallback(() => void runStormFrom(false), [runStormFrom]);
  const resumeStorm = useCallback(() => void runStormFrom(true), [runStormFrom]);

  const onOpenIncident = useCallback((id: string) => nav(`/incidents/${id}`), [nav]);
  const onInjected = useCallback(() => {
    refresh();
    // A manual inject is a user action, not a WS burst: refresh every list at once the way the
    // button always has.
    setManualTick((t) => t + 1);
  }, [refresh]);

  // --- what the guide reads ------------------------------------------------------------
  // The newest ticket and the approvals since the guide opened come from the frame feed through
  // a tap: no subscription, so App renders only when one of the two values actually changes.
  const [latestIncidentId, setLatestIncidentId] = useState<string | null>(null);
  const [approvedSinceOpen, setApprovedSinceOpen] = useState(0);
  const guideOpenRef = useRef(guideOpen);
  guideOpenRef.current = guideOpen;
  useEffect(
    () =>
      feed.tap((ev) => {
        if (ev.type === "incident.created" && ev.incidentId) setLatestIncidentId(ev.incidentId);
        if (ev.type === "hitl.approved" && guideOpenRef.current) setApprovedSinceOpen((n) => n + 1);
      }),
    [feed]
  );
  useEffect(() => {
    if (!guideOpen) return;
    setApprovedSinceOpen(0);
    // No ticket seen on the stream this session: the newest open one on the board.
    if (latestIncidentId || storm.lastOpenedId) return;
    let cancelled = false;
    api
      .incidents()
      .then((rows) => {
        if (cancelled || !Array.isArray(rows)) return;
        const open = rows.filter((r) => r && !["CLOSED", "CANCELLED"].includes(r.status));
        const newest = open.sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")))[0];
        if (newest?.id) setLatestIncidentId(newest.id);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [guideOpen]);
  const stormDoneThisSession = (() => {
    try {
      return sessionStorage.getItem(STORM_DONE_KEY) === "1";
    } catch {
      return false;
    }
  })();
  const stormDone = storm.phase === "done" || stormDoneThisSession || (metrics?.open_total ?? 0) > 0;

  const isWall = loc.pathname.startsWith("/wallboard");
  const org = profile?.display_name ? String(profile.display_name).replace(" (demo profile)", "") : "Connecting…";
  const orgMeta = `${humanAutonomy(profile?.autonomy_level)}, ${profile?.shift ? String(profile.shift).toLowerCase() : "day"} shift`;
  // "L2 guarded", explained once: what the agents may do on their own at this level.
  const autonomyTitle = AUTONOMY_MEANING[String(profile?.autonomy_level || "L2_GUARDED").toUpperCase()] || orgMeta;
  const pending: number | null = typeof metrics?.hitl_pending === "number" ? metrics.hitl_pending : metrics ? 0 : null;

  if (isWall) {
    return (
      <RealtimeProvider value={realtime}>
        <main>
          <Suspense fallback={null}>
            <Routes>
              <Route
                path="/wallboard"
                element={
                  <Wallboard
                    metrics={metrics}
                    rev={revisions.incidents}
                    signalsRev={revisions.signals}
                  />
                }
              />
            </Routes>
          </Suspense>
        </main>
      </RealtimeProvider>
    );
  }

  return (
    <RealtimeProvider value={realtime}>
      <div className="app">
        <a
          className="skip-link"
          href="#main"
          onClick={(e) => {
            e.preventDefault();
            document.getElementById("main")?.focus();
          }}
        >
          Skip to content
        </a>
        <nav className={"nav" + (navOpen ? " open" : "")} aria-label="Main">
          <div className="nav-brand-row">
            <div className="brand">Kenya NOC</div>
            <button
              type="button"
              className="btn nav-toggle"
              aria-expanded={navOpen}
              aria-controls="nav-links"
              onClick={() => setNavOpen((o) => !o)}
            >
              {navOpen ? "Close" : "Menu"}
            </button>
          </div>
          <div className="nav-links" id="nav-links">
            {NAV_GROUPS.map((g) => (
              <div key={g.title} className="nav-group">
                <div className="nav-group-title">{g.title}</div>
                <div className="nav-group-links">
                  {g.links.map((l) => (
                    <NavLink key={l.to} to={l.to} end={l.end}>
                      {l.label}
                    </NavLink>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </nav>
        <div className="main">
          <header className="topbar">
            <div className="topbar-left">
              <span className="topbar-org">
                <span className="topbar-id">{org}</span>
                <span className="topbar-meta chip-wide" title={autonomyTitle}>
                  {orgMeta}
                </span>
              </span>
              {/* Healthy is not news: "Live" stays a muted word; only a broken link turns red, and
                  never in the first seconds while the stream is still connecting. */}
              <span
                className={"topbar-live" + (!apiOk || link === "down" ? " bad" : "")}
                role="status"
                title={!apiOk ? "The API is not answering" : "Live updates from the agents"}
              >
                {!apiOk ? "API unreachable" : link === "live" ? "Live" : link === "connecting" ? "Connecting…" : "Reconnecting"}
              </span>
            </div>
            <div className="topbar-right">
              <button
                type="button"
                className={"btn sm topbar-decisions" + ((pending ?? 0) > 0 ? " hitl" : " quiet")}
                onClick={() => nav("/hitl")}
                title="Open Approvals"
              >
                {pending == null ? "Approvals" : pending > 0 ? `${pending} ${WAITING_WORD}` : "No decisions waiting"}
              </button>
              <span className="topbar-user chip-wide">{session.display_name}</span>
              <button
                type="button"
                className="btn sm"
                onClick={() => setGuideOpen((o) => !o)}
                aria-pressed={guideOpen}
                aria-controls={guideOpen ? "guide-bar" : undefined}
                data-guide-toggle=""
                title="A five-step walkthrough for presenting the prototype"
              >
                Guided demo
              </button>
              <QuietToggle on={quietMode} onToggle={() => setQuietMode(!quietMode)} />
            </div>
          </header>
          {!apiOk && (
            <div className="storm-banner danger app-alert" role="alert">
              <div>
                <strong>API unreachable</strong>
                {/* One sentence on the floor; the developer's dev-server route lives in the title. */}
                <div
                  className="muted"
                  title="For the UI dev server run `cd frontend && npm run dev` and open http://127.0.0.1:5173, or build the UI and open http://127.0.0.1:8000."
                >
                  The page retries every 8 seconds. Start the backend with{" "}
                  <code>python -m uvicorn noc_agents.main:app --app-dir src --port 8000</code>.
                </div>
              </div>
            </div>
          )}
          {/* The guided demo is a bar in normal flow between the top bar and the page: it pushes
              the page down and covers nothing. */}
          <div id="guide-bar">
            <Suspense fallback={null}>
              <DemoGuide
                open={guideOpen}
                onClose={() => setGuideOpen(false)}
                storming={storming}
                stormDone={stormDone}
                pendingCount={pending ?? 0}
                latestIncidentId={storm.lastOpenedId || latestIncidentId}
                approvedSinceOpen={approvedSinceOpen}
                onLaunchStorm={launchStorm}
                firstIncidentId={storm.firstIncidentId}
              />
            </Suspense>
          </div>
          <main className="content" id="main" tabIndex={-1}>
            <Suspense fallback={<PageSkeleton />}>
            <Routes>
              <Route
                path="/"
                element={
                  <MissionControl
                    metrics={metrics}
                    profile={profile}
                    incidentsRev={revisions.incidents + manualTick}
                    hitlRev={revisions.hitl + manualTick}
                    runsRev={revisions.runs + manualTick}
                    storm={storm}
                    onLaunchStorm={launchStorm}
                    onResumeStorm={resumeStorm}
                    onOpen={onOpenIncident}
                    onRefresh={refresh}
                  />
                }
              />
              <Route
                path="/showcase"
                element={<Showcase profile={profile} metrics={metrics} runsRev={revisions.runs + manualTick} />}
              />
              <Route
                path="/incidents"
                element={<IncidentBoard tick={revisions.incidents + manualTick} profile={profile} />}
              />
              <Route path="/incidents/:id" element={<IncidentWorkspace session={session} />} />
              <Route path="/hitl" element={<HitlInbox session={session} tick={revisions.hitl + manualTick} />} />
              <Route path="/shift" element={<ShiftDesk tick={revisions.ledger + manualTick} />} />
              <Route path="/agents" element={<Agents tick={revisions.runs + manualTick} />} />
              <Route path="/workflow" element={<WorkflowMap profile={profile} />} />
              <Route path="/problems" element={<Problems tick={revisions.problems + manualTick} />} />
              {/* Regions dashboard (§7.4.2). Refetches on the incidents, problems and
                  signals slices — the three things a region card actually shows. */}
              <Route
                path="/regions"
                element={
                  <Regions
                    tick={revisions.incidents + revisions.problems + revisions.signals + manualTick}
                  />
                }
              />
              <Route path="/maintenance" element={<Maintenance tick={revisions.incidents + manualTick} />} />
              <Route path="/audit" element={<Audit tick={revisions.audit + manualTick} />} />
              <Route path="/contracts" element={<Contracts />} />
              {/* Post-incident reviews (§7.7). Refetches on the `pir` slice (pir.opened). */}
              <Route path="/pirs" element={<Pirs tick={revisions.pir + manualTick} />} />
              {/* Vendor scorecards (§7.6). No WS event exists for them, so no tick: the page refetches itself. */}
              <Route path="/scorecards" element={<Scorecards session={session} />} />
              <Route
                path="/settings"
                element={
                  <Settings
                    session={session}
                    onSession={setSession}
                    profile={profile}
                    onInjected={onInjected}
                    storming={storming}
                    stormProg={stormLine(storm)}
                    stormErr={stormStopLine(storm)}
                    onLaunchStorm={launchStorm}
                    onResumeStorm={resumeStorm}
                  />
                }
              />
            </Routes>
            </Suspense>
          </main>
        </div>
      </div>
    </RealtimeProvider>
  );
}
