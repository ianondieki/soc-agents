import { NavLink, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, runLiveRainStorm } from "./api";
import { useRealtime } from "./realtime/useRealtime";
import { RealtimeProvider } from "./realtime/RealtimeContext";
import DemoGuide from "./components/DemoGuide";
import { AUTO_STORM_KEY } from "./lib/demo";
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
              <div className="muted">Mission Control · Safaricom</div>
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
            <div className="chips">
              <span className="chip accent">{profile?.display_name || "Connecting…"}</span>
              <span className="chip">{profile?.autonomy_level || "L2"}</span>
              <span className="chip chip-wide">
                {(profile?.shift ? String(profile.shift).toUpperCase() : "DAY") + " SHIFT"}
              </span>
              <span className={"chip " + (connected ? "ok" : "bad")}>
                {connected ? "LIVE WS" : "WS reconnecting"}
              </span>
              <span className={"chip " + (apiOk ? "ok" : "bad")}>{apiOk ? "API OK" : "API DOWN"}</span>
              <span className="chip hitl">HITL {metrics?.hitl_pending ?? 0}</span>
              <span className="chip chip-wide">Open {metrics?.open_total ?? 0}</span>
              <span className="chip chip-wide">P1 {metrics?.by_priority?.P1 ?? 0}</span>
              <span className="chip chip-wide">P2 {metrics?.by_priority?.P2 ?? 0}</span>
              {quietMode && suppressed > 0 && (
                <span className="chip" title="Non-critical ticker lines held back by quiet mode">
                  QUIET · {suppressed} held
                </span>
              )}
            </div>
            <div className="chips">
              <span className="chip">
                {session.display_name} · {session.role}
              </span>
              <button
                className={"btn" + (guideOpen ? " good" : "")}
                onClick={() => setGuideOpen((o) => !o)}
                aria-pressed={guideOpen}
                title="A five-step walkthrough for presenting the prototype"
              >
                Guided demo
              </button>
              <button
                className={"btn" + (quietMode ? " good" : "")}
                onClick={() => setQuietMode(!quietMode)}
                title="Night shift: stop animations and non-critical ticker churn. P1/P2 incidents and HITL prompts stay live."
              >
                {quietMode ? "Quiet mode ON" : "Quiet mode"}
              </button>
              <button className="btn" onClick={() => nav("/settings")}>
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
          <div className="content">
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
          </div>
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
