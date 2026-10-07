import { Link, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { Presentation, UserRoundCheck } from "lucide-react";
import { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { api } from "./api";
import { apiHealthy, subscribeHealth } from "./realtime/apiHealth";
import { useRealtime } from "./realtime/useRealtime";
import { RealtimeProvider } from "./realtime/RealtimeContext";
import { useTheme } from "./lib/theme";
import { useNarrow } from "./lib/layout";
import { Sidebar } from "./components/shell/Sidebar";
import { PhoneMenu } from "./components/shell/PhoneMenu";
import { DisplayMenu, type DisplayState } from "./components/shell/DisplayMenu";
import AlertSignal from "./components/shell/AlertSignal";
import NotificationCenter, { INBOX_ROLES } from "./components/shell/NotificationCenter";
import { installAlertUnlock } from "./lib/feedback";
import { BrandMark } from "./components/shell/BrandMark";
import TopbarStatus, { type LiveTone } from "./components/shell/TopbarStatus";
import GoTo, { useGoToShortcut } from "./components/shell/GoTo";
import { useSupportCount } from "./components/shell/useSupportCount";
import { readRailPref, writeRailPref, type NavCounts } from "./components/shell/nav";
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
import { WAITING_WORD, autonomyMeaning, humanAutonomy, humanEnum, regionName } from "./lib/agents";
import {
  applyDisplay,
  printedLine,
  readDisplay,
  recallQuietBeforeProjector,
  rememberQuietBeforeProjector,
  usePrinting,
} from "./lib/display";

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
// The front door and the public complaint form render outside the console shell, as the
// Wallboard does; the Support desk is a console page.
const Landing = lazy(() => import("./pages/Landing"));
const SupportDesk = lazy(() => import("./pages/SupportDesk"));
const Complain = lazy(() => import("./pages/Complain"));
const Track = lazy(() => import("./pages/Track"));
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

/* The sidebar's groups live in components/shell/nav.ts; the sidebar, its rail and flyouts in
   components/shell/Sidebar.tsx; the phone sheet in PhoneMenu.tsx; the Display panel in
   DisplayMenu.tsx. */

/** Tablets (761-1099 px) start on the 72 px rail; a person's own choice, once made, wins. */
const TABLET_QUERY = "(max-width: 1099px)";
const PHONE_QUERY = "(max-width: 760px)";

/** "NOC Analyst" -> "NA"; "Grace Wanjiru" -> "GW". */
function initialsOf(name: string): string {
  const parts = String(name || "")
    .trim()
    .split(/\s+/)
    .filter(Boolean);
  if (!parts.length) return "?";
  const first = parts[0][0] || "";
  const last = parts.length > 1 ? parts[parts.length - 1][0] || "" : "";
  return (first + last).toUpperCase();
}

/** "noc_analyst" -> "NOC analyst", "shift_supervisor" -> "Shift supervisor". */
function roleLabel(role: string): string {
  const words = String(role || "")
    .split(/[_\s]+/)
    .filter(Boolean)
    .map((w) => (w.toLowerCase() === "noc" ? "NOC" : w.toLowerCase()));
  if (!words.length) return "";
  if (words[0] !== "NOC") words[0] = words[0][0].toUpperCase() + words[0].slice(1);
  return words.join(" ");
}

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

/** "Printed 02 Oct 2026, 15:24 EAT" at the foot of every printed page (a fixed element repeats on
 *  each sheet); nothing on screen. Re-rendered at the moment of printing. */
function PrintFooter() {
  usePrinting();
  return (
    <div className="print-footer" aria-hidden="true">
      {printedLine()}
    </div>
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

/** How long the rail takes to replay the last run (12 steps at ~100 ms) before "Storm complete". */
const RAIL_SETTLE_MS = 1400;

export default function App() {
  const [profile, setProfile] = useState<any>(null);
  const [metrics, setMetrics] = useState<any>(null);
  const [session, setSession] = useState<any>({ display_name: "NOC Analyst", role: "noc_analyst" });
  // Two consecutive failed calls, any calls, before the page says "API unreachable"
  // (realtime/apiHealth.ts counts them for every request in one place).
  const apiOk = useSyncExternalStore(subscribeHealth, apiHealthy);
  // The metrics call alone failing is not an outage: the KPI strip says its counts are stale.
  const [metricsFailed, setMetricsFailed] = useState(false);
  const [manualTick, setManualTick] = useState(0);
  const [guideOpen, setGuideOpen] = useState(false);
  const nav = useNavigate();
  const loc = useLocation();
  const headerRef = useRef<HTMLElement>(null);
  // The top bar lifts off the page (a hairline shadow) once the page has scrolled under it.
  const [scrolled, setScrolled] = useState(false);
  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 4);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);
  // "Go to a page" (Ctrl+K): only inside the console shell, never over the front page, the public
  // support pages or the Wallboard.
  const [gotoOpen, setGotoOpen] = useState(false);
  const inShell = !(
    loc.pathname === "/" ||
    loc.pathname.startsWith("/complain") ||
    loc.pathname === "/track" ||
    loc.pathname.startsWith("/wallboard")
  );
  const toggleGoto = useCallback(() => {
    if (inShell) setGotoOpen((o) => !o);
  }, [inShell]);
  useGoToShortcut(toggleGoto);
  useEffect(() => {
    if (!inShell) setGotoOpen(false);
  }, [inShell]);

  // The shell's shape: a phone has no sidebar; a tablet starts on the rail; a person's choice
  // (localStorage "noc.nav.rail") wins over the width default once made.
  const phone = useNarrow(PHONE_QUERY);
  const tablet = useNarrow(TABLET_QUERY);
  const [railPref, setRailPref] = useState<boolean | null>(readRailPref);
  const rail = railPref ?? tablet;
  const toggleRail = useCallback(() => {
    setRailPref((p) => {
      const next = !(p ?? tablet);
      writeRailPref(next);
      return next;
    });
  }, [tablet]);

  // The ticker lines and the run frames live in realtime.feed (an external store read by the few
  // components that show them), so App re-renders for a debounced flush, never for a frame.
  const realtime = useRealtime();
  const { connected, link, revisions, quietMode, setQuietMode, feed } = realtime;
  // The first press or key on the page lets a later alarm sound, when sound is on.
  useEffect(() => installAlertUnlock(), []);
  const connectedRef = useRef(connected);
  connectedRef.current = connected;

  // The 8-second heartbeat refetches the metrics only; the lists follow the WS revisions, and
  // fall back to the heartbeat only while the stream is down (manualTick below). A failed metrics
  // call asks one other, small route whether the API is there at all: if it answers, only the
  // counts are stale; if it fails too, that is the second failure in a row and the page says so.
  const refresh = useCallback(() => {
    api
      .metrics()
      .then((m) => {
        setMetrics(m);
        setMetricsFailed(false);
      })
      .catch(() => {
        setMetricsFailed(true);
        api.session().catch(() => undefined);
      });
  }, []);
  const loadIdentity = useCallback(() => {
    api.profile().then(setProfile).catch(() => undefined);
    api.session().then(setSession).catch(() => undefined);
  }, []);

  useEffect(() => {
    // A tick later, so a mount React undoes at once (its development double mount) asks nothing.
    const first = window.setTimeout(() => {
      loadIdentity();
      refresh();
    }, 0);
    const id = window.setInterval(() => {
      refresh();
      if (!connectedRef.current) setManualTick((t) => t + 1);
    }, 8000);
    return () => {
      window.clearTimeout(first);
      window.clearInterval(id);
    };
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
  // that can actually move a count (see realtime/renderers.ts). Through refresh(), so a failure
  // here probes `/session` the same way: two metrics failures alone never call the API down.
  useEffect(() => {
    if (revisions.metrics === 0) return; // the mount load is refresh()'s job
    refresh();
  }, [revisions.metrics, refresh]);

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

  // Projector mode: <html data-display="projector">, set before the first paint by index.html.
  // Switching it on turns quiet mode on too; switching it off gives quiet mode back as it was
  // (remembered across a reload, as the mode itself is).
  const [projector, setProjector] = useState(() => readDisplay() === "projector");
  const toggleProjector = () => {
    const next = !projector;
    applyDisplay(next ? "projector" : "standard");
    setProjector(next);
    if (next) {
      rememberQuietBeforeProjector(quietMode);
      if (!quietMode) setQuietMode(true);
    } else if (recallQuietBeforeProjector() === false) {
      setQuietMode(false);
    }
  };
  const toggleQuiet = () => setQuietMode(!quietMode);

  // The console follows the shift (lib/theme.ts): <html data-theme> re-resolves on every route
  // change (the Wallboard is always Night) and when projector mode flips (also always Night).
  const { pref: themePref, setPref: setThemePref } = useTheme(loc.pathname, projector);
  const display: DisplayState = useMemo(
    () => ({ pref: themePref, setPref: setThemePref, quiet: quietMode, onQuiet: toggleQuiet, projector, onProjector: toggleProjector }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [themePref, setThemePref, quietMode, projector]
  );

  // Counts on the sidebar's groups: approvals waiting (Operate) and support cases with a person
  // (Support; nothing until that route exists).
  const supportCount = useSupportCount();

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
  const isLanding = loc.pathname === "/";
  // The public support pages (the complaint form and Track my complaint) have their own frame.
  const isComplain = loc.pathname.startsWith("/complain") || loc.pathname === "/track";
  const org = profile?.display_name ? String(profile.display_name).replace(" (demo profile)", "") : "Connecting…";
  const autonomy = humanAutonomy(profile?.autonomy_level);
  const shift = profile?.shift ? String(profile.shift).toLowerCase() : "day";
  // "L2 guarded", explained: what the agents may do on their own at this level (Settings says it too).
  const autonomyTitle = autonomyMeaning(profile?.autonomy_level) || `${autonomy}, ${shift} shift`;
  const pending: number | null = typeof metrics?.hitl_pending === "number" ? metrics.hitl_pending : metrics ? 0 : null;
  const navCounts: NavCounts = useMemo(() => ({ operate: pending, support: supportCount }), [pending, supportCount]);
  const userName = String(session?.display_name || "NOC Analyst");
  const userTitle = `${userName}${session?.role ? `, ${roleLabel(String(session.role))}` : ""}`;
  const liveBad = !apiOk || link === "down";
  const liveTone: LiveTone = liveBad ? "bad" : link === "live" ? "ok" : "wait";
  const liveText = !apiOk ? "API unreachable" : link === "live" ? "Live" : link === "connecting" ? "Connecting…" : "Reconnecting";
  const liveTitle = !apiOk ? "The API is not answering" : "Live updates from the agents";

  if (isLanding || isComplain) {
    return (
      <RealtimeProvider value={realtime}>
        <Suspense fallback={null}>
          <Routes>
            <Route path="/" element={<Landing profile={profile} metrics={metrics} runsRev={revisions.runs + manualTick} />} />
            <Route path="/complain" element={<Complain />} />
            <Route path="/track" element={<Track />} />
          </Routes>
        </Suspense>
      </RealtimeProvider>
    );
  }

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
                    profile={profile}
                    metricsStale={metricsFailed}
                    rev={revisions.incidents}
                    signalsRev={revisions.signals}
                  />
                }
              />
            </Routes>
          </Suspense>
        </main>
        <PrintFooter />
      </RealtimeProvider>
    );
  }

  return (
    <RealtimeProvider value={realtime}>
      <div className={"app" + (phone ? " phone" : rail ? " rail" : "")}>
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
        {!phone && <Sidebar rail={rail} onToggleRail={toggleRail} counts={navCounts} onSearch={() => setGotoOpen(true)} />}
        <GoTo open={gotoOpen} onClose={() => setGotoOpen(false)} />
        <div className="main">
          <header className={"topbar" + (scrolled ? " is-scrolled" : "")} ref={headerRef}>
            {/* On a phone the brand moves up here (there is no sidebar); the operator and the
                state of the floor move to the top of the menu sheet. */}
            {phone && (
              <Link to="/" className="brand topbar-brand" title="Front page">
                <BrandMark />
                <span>Kenya NOC</span>
              </Link>
            )}
            <div className="topbar-left">
              {!phone && <span className="topbar-id">{org}</span>}
              {!phone && (
                <TopbarStatus live={liveTone} liveText={liveText} liveTitle={liveTitle} autonomy={autonomy} autonomyTitle={autonomyTitle} shift={shift} />
              )}
              {/* On a phone, live is a dot: green while the stream is up, red when it is broken;
                  the words stay for a screen reader. */}
              {phone && (
                <span className={`topbar-live dot ${liveTone}`} role="status" title={liveTitle}>
                  <span className="sr-only">{liveText}</span>
                </span>
              )}
              {/* The alarm signal (a new P1 or P2, a decision, a failure): over this side of the
                  bar on a desktop, so it hides no page content; under the bar on a phone. */}
              <AlertSignal />
            </div>
            <div className="topbar-right">
              <button
                type="button"
                className={"btn sm topbar-decisions" + ((pending ?? 0) > 0 ? " hitl" : " quiet")}
                onClick={() => nav("/hitl")}
                title="Open Approvals"
              >
                <UserRoundCheck size={16} strokeWidth={1.75} aria-hidden="true" />
                {/* One width for every state on a desktop; the short form below 1100 px. */}
                <span className="topbar-decisions-long">
                  {pending == null ? (
                    "Approvals"
                  ) : pending > 0 ? (
                    <>
                      <b className="topbar-count">{pending}</b> {WAITING_WORD}
                    </>
                  ) : (
                    "No decisions waiting"
                  )}
                </span>
                <span className="topbar-decisions-short">
                  {pending && pending > 0 ? (
                    <>
                      <b className="topbar-count">{pending}</b> waiting
                    </>
                  ) : (
                    "Approvals"
                  )}
                </span>
              </button>
              {!phone && (
                <>
                  <button
                    type="button"
                    className="btn sm topbar-guide"
                    onClick={() => setGuideOpen((o) => !o)}
                    aria-pressed={guideOpen}
                    aria-controls={guideOpen ? "guide-bar" : undefined}
                    data-guide-toggle=""
                    aria-label="Guided demo"
                    title="A five-step walkthrough for presenting the prototype"
                  >
                    <Presentation size={16} strokeWidth={1.75} aria-hidden="true" />
                    <span className="topbar-guide-label">Guided demo</span>
                  </button>
                  {INBOX_ROLES.has(String(session?.role || "")) && (
                    <NotificationCenter
                      rev={revisions.incidents + revisions.hitl + revisions.runs + manualTick}
                      profile={profile}
                      who={String(session?.display_name || "NOC Analyst")}
                      role={String(session?.role || "")}
                    />
                  )}
                  <DisplayMenu d={display} />
                  <span className="topbar-sep" aria-hidden="true" />
                  <span className="avatar" title={userTitle} role="img" aria-label={userTitle}>
                    {initialsOf(userName)}
                  </span>
                </>
              )}
              {phone && INBOX_ROLES.has(String(session?.role || "")) && (
                <NotificationCenter
                  rev={revisions.incidents + revisions.hitl + revisions.runs + manualTick}
                  profile={profile}
                  who={String(session?.display_name || "NOC Analyst")}
                  role={String(session?.role || "")}
                  compact
                />
              )}
              {phone && (
                <PhoneMenu
                  anchorRef={headerRef}
                  counts={navCounts}
                  display={display}
                  guideOpen={guideOpen}
                  onGuide={() => setGuideOpen((o) => !o)}
                  identity={
                    <>
                      <span className="topbar-id">{org}</span>
                      <TopbarStatus
                        live={liveTone}
                        liveText={liveText}
                        liveTitle={liveTitle}
                        autonomy={autonomy}
                        autonomyTitle={autonomyTitle}
                        shift={shift}
                        clock={false}
                        announce={false}
                      />
                    </>
                  }
                />
              )}
            </div>
          </header>
          {/* Two calls in a row failed: one sentence for the floor. How to start the backend is in
              docs/RUNBOOK.md ("The UI says API unreachable"), not on this screen. */}
          {!apiOk && (
            <div className="storm-banner danger app-alert" role="alert">
              <div>
                <strong>API unreachable</strong>
                <div className="muted">Showing the last data received; the page retries every 8 seconds.</div>
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
                path="/mission"
                element={
                  <MissionControl
                    metrics={metrics}
                    metricsStale={metricsFailed}
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
              <Route path="/support" element={<SupportDesk session={session} tick={revisions.support + manualTick} profile={profile} />} />
              <Route
                path="/showcase"
                element={<Showcase profile={profile} metrics={metrics} runsRev={revisions.runs + manualTick} />}
              />
              <Route
                path="/incidents"
                element={<IncidentBoard tick={revisions.incidents + manualTick} profile={profile} />}
              />
              <Route path="/incidents/:id" element={<IncidentWorkspace session={session} profile={profile} />} />
              <Route path="/hitl" element={<HitlInbox session={session} tick={revisions.hitl + manualTick} profile={profile} />} />
              <Route path="/shift" element={<ShiftDesk tick={revisions.ledger + manualTick} profile={profile} metrics={metrics} />} />
              <Route path="/agents" element={<Agents tick={revisions.runs + manualTick} />} />
              <Route path="/workflow" element={<WorkflowMap profile={profile} />} />
              <Route path="/problems" element={<Problems tick={revisions.problems + manualTick} profile={profile} />} />
              {/* Regions dashboard (§7.4.2). Refetches on the incidents, problems and
                  signals slices (the things a region card shows), and on hitl: a region's
                  "possible outage" line goes once its card is decided (docs/CLOSE_THE_LOOP.md §3). */}
              <Route
                path="/regions"
                element={
                  <Regions
                    tick={revisions.incidents + revisions.problems + revisions.signals + revisions.hitl + manualTick}
                  />
                }
              />
              <Route path="/maintenance" element={<Maintenance tick={revisions.incidents + manualTick} profile={profile} />} />
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
      <PrintFooter />
    </RealtimeProvider>
  );
}
