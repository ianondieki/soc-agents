import { NavLink, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, runLiveRainStorm } from "./api";
import { useRealtime } from "./realtime/useRealtime";
import { RealtimeProvider } from "./realtime/RealtimeContext";
import DemoGuide from "./components/DemoGuide";
import { AUTO_STORM_KEY } from "./lib/demo";
import { humanAutonomy } from "./lib/agents";
import MissionControl from "./pages/MissionControl";
import IncidentBoard from "./pages/IncidentBoard";
import IncidentWorkspace from "./pages/IncidentWorkspace";
import HitlInbox from "./pages/HitlInbox";
import ShiftDesk from "./pages/ShiftDesk";
import Wallboard from "./pages/Wallboard";
import Agents from "./pages/Agents";
import Problems from "./pages/Problems";
import Regions from "./pages/Regions";
import Maintenance from "./pages/Maintenance";
import Audit from "./pages/Audit";
import Settings from "./pages/Settings";
import WorkflowMap from "./pages/WorkflowMap";
import Contracts from "./pages/Contracts";
import Pirs from "./pages/Pirs";
import Scorecards from "./pages/Scorecards";
import Showcase from "./pages/Showcase";

/** The sidebar, grouped by who reaches for it: the shift, the agent story, quality, platform. */
const NAV_GROUPS: { title: string; links: { to: string; label: string; end?: boolean }[] }[] = [
  {
    title: "Operate",
    links: [
      { to: "/", label: "Mission Control", end: true },
      { to: "/incidents", label: "Incident Board" },
      { to: "/hitl", label: "HITL Inbox" },
      { to: "/shift", label: "Shift Desk" },
      { to: "/wallboard", label: "Wallboard" },
    ],
  },
  {
    title: "Agents",
    links: [
      { to: "/showcase", label: "Showcase" },
      { to: "/agents", label: "Agent Observatory" },
      { to: "/workflow", label: "Workflow Map" },
    ],
  },
  {
    title: "Quality",
    links: [
      { to: "/problems", label: "Problems" },
      { to: "/regions", label: "Regions" },
      { to: "/maintenance", label: "Maintenance" },
      { to: "/pirs", label: "PIRs" },
      { to: "/scorecards", label: "Vendor Scorecards" },
      { to: "/contracts", label: "Contracts" },
    ],
  },
  {
    title: "Platform",
    links: [
      { to: "/audit", label: "Audit" },
      { to: "/settings", label: "Settings / Inject" },
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
            const head = el.querySelector("h3, h4")?.textContent?.trim();
            if (head) el.setAttribute("aria-label", head);
          }
        } else if (el.getAttribute("tabindex") === "0") {
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
    window.addEventListener("resize", schedule);
    return () => {
      window.clearTimeout(t1);
      window.clearTimeout(t2);
      window.cancelAnimationFrame(raf);
      window.removeEventListener("resize", schedule);
    };
  }, [pathname, revisions]);
}

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

  const realtime = useRealtime();
  const { connected, events, revisions, quietMode, setQuietMode, suppressed } = realtime;

  const refresh = useCallback(() => {
    api
      .profile()
      .then((p) => {
        setProfile(p);
        setApiOk(true);
      })
      .catch(() => setApiOk(false));
    api.metrics().then(setMetrics).catch(() => setApiOk(false));
    api.session().then(setSession).catch(() => undefined);
  }, []);

  useEffect(() => {
    refresh();
    const id = window.setInterval(refresh, 8000);
    return () => window.clearInterval(id);
  }, [refresh]);

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

  // --- the live storm, owned here so the guided demo can start it from any page ------
  const stormingRef = useRef(false);
  const [storming, setStorming] = useState(false);
  const [stormProg, setStormProg] = useState("");
  const [stormErr, setStormErr] = useState("");
  const [firstStormIncident, setFirstStormIncident] = useState<string | null>(null);

  const launchStorm = useCallback(
    async (reason: string) => {
      if (stormingRef.current) return;
      stormingRef.current = true;
      setStorming(true);
      setStormProg(`${reason}: contacting the agents…`);
      setStormErr("");
      try {
        const result = await runLiveRainStorm((i, total, inc) => {
          setStormProg(
            `Alarm ${i} of ${total} · ${inc.incident_number} · ${inc.region_code} · ${inc.site_id} · ` +
              `${inc.failure_domain} → ${inc.responsible_msp || inc.msp_name || inc.assignee_name}`
          );
          if (i === 1 && inc?.id) setFirstStormIncident(inc.id);
          refresh();
          setManualTick((t) => t + 1);
        }, 1500);
        setStormProg(
          `Storm complete: ${result.count} alarms. HUB majors opened; child sites folded under their parents. P2 broadcasts wait in the HITL inbox.`
        );
        refresh();
        setManualTick((t) => t + 1);
        try {
          sessionStorage.setItem(AUTO_STORM_KEY, "1");
        } catch {
          /* ignore */
        }
      } catch (e: any) {
        setStormErr(e?.message || String(e));
        setStormProg("");
      } finally {
        stormingRef.current = false;
        setStorming(false);
      }
    },
    [refresh]
  );

  const isWall = loc.pathname.startsWith("/wallboard");

  if (isWall) {
    return (
      <RealtimeProvider value={realtime}>
        <main>
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
        </main>
      </RealtimeProvider>
    );
  }

  return (
    <RealtimeProvider value={realtime}>
      <div className="app">
        <nav className={"nav" + (navOpen ? " open" : "")} aria-label="Main">
          <div className="nav-brand-row">
            <div className="brand">
              Kenya NOC
              <div className="muted">Mission Control, Safaricom demo</div>
            </div>
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
              <span className="topbar-id">{profile?.display_name ? String(profile.display_name).replace(" (demo profile)", "") : "Connecting…"}</span>
              <span className="topbar-meta chip-wide">
                {humanAutonomy(profile?.autonomy_level)}, {profile?.shift ? String(profile.shift).toLowerCase() : "day"} shift
              </span>
              <span className={"chip " + (connected ? "ok" : "bad")} title="WebSocket to the agent event stream">
                {connected ? "Live" : "Reconnecting"}
              </span>
              <span className={"chip " + (apiOk ? "ok" : "bad")}>{apiOk ? "API ok" : "API down"}</span>
              {quietMode && suppressed > 0 && (
                <span className="chip" title="Non-critical ticker lines held back by quiet mode">
                  {suppressed} held
                </span>
              )}
            </div>
            <div className="topbar-right">
              <button
                type="button"
                className={"chip " + ((metrics?.hitl_pending ?? 0) > 0 ? "hitl" : "")}
                onClick={() => nav("/hitl")}
                title="Decisions waiting for a person"
              >
                {metrics?.hitl_pending ?? 0} waiting for a decision
              </button>
              <span className="topbar-user chip-wide">{session.display_name}</span>
              <button
                className={"btn sm" + (guideOpen ? " good" : "")}
                onClick={() => setGuideOpen((o) => !o)}
                aria-pressed={guideOpen}
                title="A five-step walkthrough for presenting the prototype"
              >
                Guided demo
              </button>
              <button
                className={"btn sm chip-wide" + (quietMode ? " good" : "")}
                onClick={() => setQuietMode(!quietMode)}
                aria-pressed={quietMode}
                title="Night shift: stop animations and non-critical ticker churn. P1/P2 incidents and HITL prompts stay live."
              >
                {quietMode ? "Quiet mode on" : "Quiet mode"}
              </button>
              <button className="btn sm" onClick={() => nav("/settings")}>
                Settings
              </button>
            </div>
          </header>
          {!apiOk && (
            <div className="storm-banner" style={{ margin: "0.75rem 1.5rem 0" }}>
              <div>
                <strong>API not reachable</strong>
                <div className="muted">
                  Start backend:{" "}
                  <code>python -m uvicorn noc_agents.main:app --app-dir src --port 8000</code>
                  {" · "}
                  UI dev: <code>cd frontend && npm run dev</code> → open{" "}
                  <code>http://127.0.0.1:5173</code> (proxy to API). Or build UI and open{" "}
                  <code>http://127.0.0.1:8000</code>.
                </div>
              </div>
            </div>
          )}
          <main className="content" id="main">
            <Routes>
              <Route
                path="/"
                element={
                  <MissionControl
                    metrics={metrics}
                    events={events}
                    incidentsRev={revisions.incidents + manualTick}
                    hitlRev={revisions.hitl + manualTick}
                    runsRev={revisions.runs + manualTick}
                    quietMode={quietMode}
                    suppressed={suppressed}
                    storming={storming}
                    stormProg={stormProg}
                    stormErr={stormErr}
                    onLaunchStorm={launchStorm}
                    onOpenGuide={() => setGuideOpen(true)}
                    onOpen={(id) => nav(`/incidents/${id}`)}
                    onRefresh={refresh}
                  />
                }
              />
              <Route
                path="/showcase"
                element={<Showcase profile={profile} metrics={metrics} events={events} runsRev={revisions.runs + manualTick} />}
              />
              <Route
                path="/incidents"
                element={<IncidentBoard tick={revisions.incidents + manualTick} />}
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
                    onInjected={() => {
                      refresh();
                      // A manual inject is a user action, not a WS burst: refresh
                      // every list at once the way the button always has.
                      setManualTick((t) => t + 1);
                    }}
                  />
                }
              />
            </Routes>
          </main>
        </div>
        <DemoGuide
          open={guideOpen}
          onClose={() => setGuideOpen(false)}
          storming={storming}
          firstIncidentId={firstStormIncident}
          onRunStorm={() => {
            nav("/");
            return launchStorm("Guided demo");
          }}
        />
      </div>
    </RealtimeProvider>
  );
}
