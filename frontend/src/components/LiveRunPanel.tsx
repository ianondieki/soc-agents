import { type ReactNode, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import AgentRail from "./AgentRail";
import {
  LIFECYCLE_NODES,
  alarmSite,
  displaySteps,
  fmtMs,
  foldOf,
  isLifecycleNode,
  nodeLabel,
  opensTicket,
  runOutcome,
  runStatusWord,
  sumDurations,
  type RailStep,
} from "../lib/agents";
import { IconAlert, IconPause } from "../lib/icons";
import { fmtTime, parseInstant } from "../lib/time";
import { useQuietMode, useRunFrames } from "../realtime/RealtimeContext";
import type { NocEvent } from "../realtime/renderers";

/**
 * The latest alarm that opened a ticket, through the twelve agents.
 *
 * The rail is pinned to the newest run that opened a ticket. An alarm that folded into an open
 * ticket never replaces it: it is one muted line under the rail ("SFC-RFT-ENB-NKR-A1 folded into
 * INC000006 at Correlate").
 *
 * Two sources, one rule. The backend publishes a run's frames in one burst once the run has
 * committed, so the rail replays a newly arrived run hop by hop (about 100 ms a hop; at once in
 * quiet mode or with reduced motion): the page's one deliberate animation. The stored run
 * (`/runs`, refetched on the `runs` revision) takes over when it is the same run or a newer one,
 * because it carries the tool calls and timestamps the frames leave out; with the socket down
 * the stored run is all there is. An unfinished frame run whose last frame is more than 10 s old
 * is stale and never reads "running".
 *
 * Every slot in the head (ticket, state, steps, the time the agents took, start time, View ticket)
 * holds its width from the first paint, so nothing in the head moves while a run plays. Clicking a
 * hop pins the run on screen; "Follow live", in the hop's detail, lets go.
 */

const HOP_MS = 100;
const STALE_MS = 10_000;
/** A frame stamped longer ago than this is history (the replay a new socket gets), not news. */
const FRESH_MS = 60_000;

interface EvRun {
  runId: string;
  incidentId: string | null;
  incidentNumber: string | null;
  steps: RailStep[];
  finished: boolean;
  status: string | null;
  error: string | null;
  failedNode: string | null;
  ticket: boolean;
  foldInto: string | null;
  foldSite: string | null;
  /** Server time of the first frame (ms), and the stamp itself. */
  firstTs: number | null;
  startedAt: string | null;
  /** When this tab received the first and the last frame. */
  firstAt: number;
  lastAt: number;
}

const msOf = (v: unknown): number | null => parseInstant(v)?.getTime() ?? null;

/** Group the frame feed (oldest first) into runs, newest first. */
function groupRuns(frames: NocEvent[]): EvRun[] {
  const byId = new Map<string, EvRun>();
  const order: string[] = [];
  const at = new Map<string, Map<string, number>>();
  for (const e of frames) {
    const p = e.payload || {};
    const runId = e.runId || (typeof p.run_id === "string" ? p.run_id : null);
    if (!runId) continue;
    const isStep = e.type === "agent.step.started" || e.type === "agent.step.completed";
    // A scheduler job or an assist run records steps through the same tracker under its own
    // node names: those never reach the rail.
    if (isStep && !isLifecycleNode(p.node)) continue;
    let r = byId.get(runId);
    if (!r) {
      r = {
        runId,
        incidentId: null,
        incidentNumber: null,
        steps: [],
        finished: false,
        status: null,
        error: null,
        failedNode: null,
        ticket: false,
        foldInto: null,
        foldSite: null,
        firstTs: msOf(e.ts),
        startedAt: e.ts,
        firstAt: e.receivedAt,
        lastAt: e.receivedAt,
      };
      byId.set(runId, r);
      order.push(runId);
      at.set(runId, new Map());
    }
    r.lastAt = e.receivedAt;
    if (e.incidentId) r.incidentId = e.incidentId;
    if (isStep) {
      const idx = at.get(runId)!;
      const i = idx.get(p.node);
      const prev = i != null ? r.steps[i] : undefined;
      const next: RailStep =
        e.type === "agent.step.started"
          ? {
              ...(prev || {}),
              node_name: p.node,
              agent_name: p.agent,
              status: prev?.status && prev.status !== "STARTED" ? prev.status : "STARTED",
              input_summary: p.input ?? prev?.input_summary,
              started_at: e.ts,
            }
          : {
              ...(prev || { node_name: p.node, agent_name: p.agent }),
              status: p.status || "SUCCEEDED",
              rationale: p.rationale,
              output_summary: p.output,
              duration_ms: typeof p.duration_ms === "number" ? p.duration_ms : null,
              seq: p.seq,
              finished_at: e.ts,
            };
      if (i == null) {
        idx.set(p.node, r.steps.length);
        r.steps.push(next);
      } else r.steps[i] = next;
      if (p.node === "TICKET") r.ticket = true;
      if (p.incident_number) r.incidentNumber = String(p.incident_number);
    } else if (e.type === "agent.run.finished") {
      r.finished = true;
      r.status = p.status || null;
      r.error = p.error || null;
      if (p.node) r.failedNode = String(p.node);
    } else if (e.type === "incident.created") {
      r.ticket = true;
      if (p.incident_number) r.incidentNumber = String(p.incident_number);
    } else if (e.type === "incident.merged") {
      r.foldInto = p.incident_number ? String(p.incident_number) : r.foldInto;
    } else if (e.type === "incident.cascade_child") {
      r.foldInto = p.parent ? String(p.parent) : r.foldInto;
      r.foldSite = p.child_site ? String(p.child_site) : r.foldSite;
    }
  }
  return order
    .map((id) => byId.get(id)!)
    .filter((r) => r.steps.length > 0 || r.ticket || r.foldInto || (r.finished && isLifecycleNode(r.failedNode)))
    .reverse();
}

/** One run as the panel draws it, from either source. */
interface RunView {
  runId: string;
  incidentId: string | null;
  incidentNumber: string | null;
  /** The run's status; null when it is unknown (a stale frame run). */
  status: string | null;
  steps: RailStep[];
  finishedAt: string | null;
  startedAt: string | null;
  /** Nothing more will arrive for it (finished, or stale). */
  done: boolean;
  fromEvents: boolean;
}

function fromStored(r: any): RunView {
  return {
    runId: String(r.id),
    incidentId: r.incident_id || null,
    incidentNumber: null,
    status: r.status || null,
    steps: Array.isArray(r.steps) ? r.steps : [],
    finishedAt: r.finished_at || null,
    startedAt: r.started_at || null,
    done: String(r.status || "").toUpperCase() !== "RUNNING",
    fromEvents: false,
  };
}

function fromEvents(r: EvRun, now: number): RunView {
  const stale = !r.finished && now - r.lastAt > STALE_MS;
  return {
    runId: r.runId,
    incidentId: r.incidentId,
    incidentNumber: r.incidentNumber,
    status: r.finished ? r.status : stale ? null : "RUNNING",
    steps: r.steps,
    finishedAt: null,
    startedAt: r.startedAt,
    done: r.finished || stale,
    fromEvents: true,
  };
}

/** Lifecycle order, so a replay reveals Ingest first and Monitor last. */
const ORDER = new Map(LIFECYCLE_NODES.map((n, i) => [n.id, i]));
function inOrder(steps: RailStep[]): RailStep[] {
  return steps
    .filter((s) => s && s.node_name)
    .slice()
    .sort((a, b) => (ORDER.get(a.node_name) ?? 99) - (ORDER.get(b.node_name) ?? 99));
}

/** A run that did not open a ticket, as the line under the rail says it. */
interface SideRun {
  runId: string;
  at: number;
  kind: "folded" | "failed";
  site: string | null;
  into: string | null;
  node: string | null;
  error: string | null;
}

function sideOfEvent(r: EvRun): SideRun | null {
  if (r.ticket) return null;
  if (r.finished && String(r.status || "").toUpperCase() === "FAILED")
    return { runId: r.runId, at: r.firstTs ?? 0, kind: "failed", site: alarmSite(r.steps), into: null, node: r.failedNode, error: r.error };
  if (!r.finished) return null;
  const fold = r.foldInto ? { into: r.foldInto, site: r.foldSite ?? alarmSite(r.steps) } : foldOf({ steps: r.steps });
  if (!fold) return null;
  return { runId: r.runId, at: r.firstTs ?? 0, kind: "folded", site: fold.site, into: fold.into, node: "CORRELATE", error: null };
}

function sideOfStored(r: any): SideRun | null {
  if (opensTicket(r)) return null;
  const st = String(r?.status || "").toUpperCase();
  const at = msOf(r?.started_at) ?? 0;
  const steps: RailStep[] = Array.isArray(r?.steps) ? r.steps : [];
  if (st === "FAILED") {
    const failed = steps.find((s) => String(s.status).toUpperCase() === "FAILED");
    return { runId: String(r.id), at, kind: "failed", site: alarmSite(steps), into: null, node: failed?.node_name ?? null, error: r.error_summary ?? null };
  }
  const fold = foldOf(r);
  if (!fold) return null;
  return { runId: String(r.id), at, kind: "folded", site: fold.site, into: fold.into, node: "CORRELATE", error: null };
}

function usePrefersReducedMotion(): boolean {
  const query = "(prefers-reduced-motion: reduce)";
  const [reduced, setReduced] = useState(() => {
    try {
      return window.matchMedia(query).matches;
    } catch {
      return false;
    }
  });
  useEffect(() => {
    let mq: MediaQueryList;
    try {
      mq = window.matchMedia(query);
    } catch {
      return;
    }
    const on = () => setReduced(mq.matches);
    mq.addEventListener?.("change", on);
    return () => mq.removeEventListener?.("change", on);
  }, []);
  return reduced;
}

/**
 * A run's status as text: muted words when routine ("done"), the drawn icon and the state
 * colour when it is worth a look (waiting for a decision, failed). Never a chip.
 */
export function RunState({ status, routine = true }: { status: unknown; routine?: boolean }) {
  const out = runOutcome(status);
  if (!out) return routine ? <span>{runStatusWord(status)}</span> : null;
  return (
    <span className={"state" + (out.tone ? ` ${out.tone}` : "")}>
      {out.tone === "hitl" && <IconPause />}
      {out.tone === "danger" && <IconAlert />}
      {out.word}
    </span>
  );
}

type Load = "loading" | "ok" | "error";

export default function LiveRunPanel({
  runs: runsProp,
  runsFailed = false,
  onRetry,
  runsRev,
  onOpen,
  title = "Latest alarm through the agents",
  compact = true,
}: {
  /** The stored runs, newest first, when the page already fetched them (Mission control does,
   *  for its run list): the panel then makes no request of its own. Any graph; it keeps the
   *  lifecycle runs, and fetches `/runs?graph_name=incident_lifecycle` itself only if the list it
   *  was given holds none (other graphs crowded them out of the 50). `null` = still loading.
   *  Leave out to have the panel fetch on its own (the Showcase). */
  runs?: any[] | null;
  /** The page's fetch of `runs` failed before any answer (shows the error with `onRetry`). */
  runsFailed?: boolean;
  onRetry?: () => void;
  runsRev: number;
  onOpen?: (incidentId: string) => void;
  title?: string;
  compact?: boolean;
}) {
  const titleId = useId();
  const isLifecycle = (r: any) => !!r && (!r.graph_name || r.graph_name === "incident_lifecycle");
  // Our own request: no list given, or a list with runs in it but not one lifecycle run.
  const own =
    runsProp === undefined || (Array.isArray(runsProp) && runsProp.length > 0 && !runsProp.some(isLifecycle));
  const [fetched, setFetched] = useState<any[] | null>(null);
  const [fetchLoad, setFetchLoad] = useState<Load>("loading");
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    if (!own) return;
    let cancelled = false;
    api
      .lifecycleRuns()
      .then((rows) => {
        if (cancelled) return;
        setFetched(Array.isArray(rows) ? rows : []);
        setFetchLoad("ok");
      })
      .catch(() => {
        // A failed refetch after a good one keeps the run on screen.
        if (!cancelled) setFetchLoad((l) => (l === "ok" ? l : "error"));
      });
    return () => {
      cancelled = true;
    };
  }, [own, runsRev, retry]);

  const storedAll = own ? fetched : runsProp;
  const load: Load = own ? fetchLoad : runsProp ? "ok" : runsFailed ? "error" : "loading";
  const stored = useMemo(() => (storedAll || []).filter(isLifecycle), [storedAll]);
  const doRetry = () => (own || !onRetry ? setRetry((n) => n + 1) : onRetry());

  const frames = useRunFrames();
  const evRuns = useMemo(() => groupRuns(frames), [frames]);

  // Runs already in the feed when the panel mounted are not news: they draw finished at once.
  const mountedAt = useRef(Date.now());
  const seen = useRef<Set<string> | null>(null);
  if (seen.current == null) seen.current = new Set(evRuns.map((r) => r.runId));

  // A stale frame run must stop reading "running" even if nothing else re-renders the panel.
  const frozenIdRef = useRef<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  // The run the panel will draw: the frozen one, else the newest ticket run, else the newest.
  const watchedEv = (frozenIdRef.current && evRuns.find((r) => r.runId === frozenIdRef.current)) || evRuns.find((r) => r.ticket) || evRuns[0];
  useEffect(() => {
    if (!watchedEv || watchedEv.finished) return;
    const wait = Math.max(0, STALE_MS - (Date.now() - watchedEv.lastAt)) + 50;
    const t = window.setTimeout(() => setNow(Date.now()), wait);
    return () => window.clearTimeout(t);
  }, [watchedEv?.runId, watchedEv?.lastAt, watchedEv?.finished]);

  // INC numbers: frames carry them; a stored run only has the incident id.
  const numbers = useRef(new Map<string, string>());
  for (const r of evRuns) if (r.incidentId && r.incidentNumber && r.ticket) numbers.current.set(r.incidentId, r.incidentNumber);

  // A pin whose run vanished from both sources (a reseeded board) is released after render.
  const frozenGone = useRef(false);
  useEffect(() => {
    if (frozenGone.current) {
      frozenGone.current = false;
      setFrozenId(null);
      setSelected(null);
    }
  });

  // Pinned by a click on a hop: the run stays on screen until "Follow live".
  const [selected, setSelected] = useState<string | null>(null);
  const [frozenId, setFrozenId] = useState<string | null>(null);
  frozenIdRef.current = frozenId;

  const pinned: RunView | null = useMemo(() => {
    const t = Math.max(now, Date.now());
    if (frozenId) {
      const st = stored.find((r) => String(r.id) === frozenId);
      if (st && String(st.status || "").toUpperCase() !== "RUNNING") return fromStored(st);
      const ev = evRuns.find((r) => r.runId === frozenId);
      if (ev) return fromEvents(ev, t);
      if (st) return fromStored(st);
      frozenGone.current = true; // released below, after render
    }
    const storedTicket = stored.find(opensTicket) ?? null;
    const evTicket = evRuns.find((r) => r.ticket) ?? null;
    if (evTicket && storedTicket && evTicket.runId === String(storedTicket.id)) {
      // The same run: the stored row has the tool calls, once it reads finished.
      const st = fromStored(storedTicket);
      return st.done ? { ...st, incidentNumber: evTicket.incidentNumber } : fromEvents(evTicket, t);
    }
    if (evTicket) {
      const evAt = evTicket.firstTs;
      const stAt = msOf(storedTicket?.started_at);
      if (!storedTicket || evAt == null || stAt == null || evAt >= stAt) return fromEvents(evTicket, t);
    }
    if (storedTicket) return fromStored(storedTicket);
    // No run has opened a ticket yet: the newest run of any kind (a fold or a failure).
    const ev0 = evRuns[0];
    const st0 = stored[0];
    if (ev0 && (!st0 || (ev0.firstTs ?? 0) >= (msOf(st0.started_at) ?? 0))) return fromEvents(ev0, t);
    return st0 ? fromStored(st0) : null;
  }, [stored, evRuns, frozenId, now]);

  // The frozen run left both sources (evicted): follow live again.
  useEffect(() => {
    if (frozenId && !pinned) {
      setFrozenId(null);
      setSelected(null);
    }
  }, [frozenId, pinned]);

  const incidentNumber = pinned
    ? pinned.incidentNumber || (pinned.incidentId ? numbers.current.get(pinned.incidentId) ?? null : null)
    : null;

  // A stored run's ticket number, when no frame named it.
  const [fetchedNumber, setFetchedNumber] = useState<{ id: string; number: string } | null>(null);
  const needNumber = pinned && !incidentNumber && pinned.incidentId && opensTicket(pinned) ? pinned.incidentId : null;
  useEffect(() => {
    if (!needNumber || fetchedNumber?.id === needNumber) return;
    let cancelled = false;
    api
      .incident(needNumber)
      .then((inc) => {
        if (cancelled || !inc?.incident_number) return;
        numbers.current.set(needNumber, String(inc.incident_number));
        setFetchedNumber({ id: needNumber, number: String(inc.incident_number) });
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [needNumber]);
  const shownNumber = incidentNumber || (needNumber && fetchedNumber?.id === needNumber ? fetchedNumber.number : null);

  // ----------------------------------------------------------------- replay --
  const quiet = useQuietMode();
  const reduced = usePrefersReducedMotion();
  const calm = quiet || reduced;
  const [replay, setReplay] = useState<{ runId: string; shown: number } | null>(null);
  const ordered = useMemo(() => inOrder(pinned?.steps || []), [pinned]);
  const totalRef = useRef(0);
  totalRef.current = ordered.length;

  // A run is replayed once, when its frames arrive live after the panel mounted: whichever source
  // the panel is drawing it from by then (the stored row can overtake the frames by a moment).
  // A layout effect, so the replay's first frame commits before the browser can paint the
  // finished run even once.
  useLayoutEffect(() => {
    if (!pinned) return;
    const id = pinned.runId;
    const known = seen.current!;
    if (known.has(id)) return;
    const ev = evRuns.find((r) => r.runId === id);
    if (!ev) return; // stored only: nothing has arrived live for it (yet)
    known.add(id);
    const fresh = ev.firstAt >= mountedAt.current && (ev.firstTs == null || Math.abs(Date.now() - ev.firstTs) < FRESH_MS);
    if (calm || !fresh || frozenId) return;
    setReplay({ runId: id, shown: 0 });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pinned?.runId, evRuns]);

  useEffect(() => {
    if (!replay) return;
    if (calm) {
      setReplay(null);
      return;
    }
    const t = window.setInterval(() => {
      setReplay((r) => (!r ? r : r.shown + 1 >= totalRef.current ? null : { ...r, shown: r.shown + 1 }));
    }, HOP_MS);
    return () => window.clearInterval(t);
  }, [replay?.runId, calm]);

  const replaying = !!replay && !!pinned && replay.runId === pinned.runId;
  const visible = replaying ? ordered.slice(0, replay!.shown) : ordered;
  // During a replay the steps shown so far are mapped too, so a P3/P4 Approval never flashes a
  // green check before it settles on "not needed".
  const railSteps = useMemo(
    () =>
      replaying
        ? displaySteps({ status: "RUNNING", steps: visible })
        : pinned
          ? displaySteps({ status: pinned.status, steps: pinned.steps, finished_at: pinned.finishedAt })
          : [],
    // `visible` is derived from these
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [replaying, replay?.shown, pinned]
  );
  const live = replaying || (!!pinned && !pinned.done);
  const status = pinned ? (replaying ? "RUNNING" : pinned.status) : null;

  // ------------------------------------------------------------ side line --
  const side = useMemo(() => {
    const pinnedAt = msOf(pinned?.startedAt) ?? 0;
    const all = new Map<string, SideRun>();
    for (const r of stored) {
      const s = sideOfStored(r);
      if (s && s.at > pinnedAt && s.runId !== pinned?.runId) all.set(s.runId, s);
    }
    for (const r of evRuns) {
      const s = sideOfEvent(r);
      if (s && s.at > pinnedAt && s.runId !== pinned?.runId) all.set(s.runId, s);
    }
    const list = Array.from(all.values()).sort((a, b) => b.at - a.at);
    return { newest: list[0] || null, folded: list.filter((s) => s.kind === "folded").length };
  }, [stored, evRuns, pinned]);

  const pinnedFold = pinned && pinned.done && !opensTicket(pinned) ? foldOf(pinned) : null;
  // A folded run is not "2 of 12 steps, done": it reads "folded" and its unreached steps are
  // drawn skipped (displaySteps).
  const stepCount = pinned && !pinnedFold ? visible.length : null;
  const took = pinned ? sumDurations(visible) : null;
  const started = pinned?.startedAt ? fmtTime(pinned.startedAt, "") : "";
  const openId = pinned?.incidentId || null;

  const onSelect = (node: string) => {
    if (selected === node) {
      setSelected(null);
      setFrozenId(null);
      return;
    }
    setSelected(node);
    if (pinned) setFrozenId(pinned.runId);
  };
  const followLive = () => {
    setSelected(null);
    setFrozenId(null);
  };

  let foot: ReactNode = null;
  if (!pinned && load === "error") {
    foot = (
      <span className="lr-foot-alert" role="alert">
        Couldn't load the latest run.{" "}
        <button type="button" className="btn sm" onClick={doRetry}>
          Retry
        </button>
      </span>
    );
  } else if (!pinned && load === "ok") {
    foot = <span>No alarm has been through the agents yet. Launch the storm and watch each step light up.</span>;
  } else if (pinnedFold) {
    foot = (
      <span>
        <span className="mono">{alarmSite(pinned!.steps) || "This alarm"}</span> folded into{" "}
        <span className="mono">{pinnedFold.into || "an open ticket"}</span> at Correlate
      </span>
    );
  } else if (side.newest && !replaying) {
    const s = side.newest;
    foot =
      s.kind === "folded" ? (
        <>
          <span>
            <span className="mono">{s.site || "An alarm"}</span> folded into <span className="mono">{s.into || "an open ticket"}</span> at Correlate
          </span>
          {side.folded > 1 && <span className="lr-foot-count">{side.folded} alarms folded since this ticket opened</span>}
        </>
      ) : (
        <span className="state danger" title={s.error || undefined}>
          <IconAlert />
          {s.site ? <span className="mono">{s.site}</span> : "An alarm"} failed at {nodeLabel(s.node) || "a step"}
        </span>
      );
  }

  return (
    <section className="panel live-run" aria-labelledby={titleId}>
      <div className="panel-head lr-head">
        <div className="head-row lr-facts">
          <h2 id={titleId} className="panel-title">
            {title}
          </h2>
          <span className="mono lr-inc">{shownNumber || ""}</span>
          <span className="lr-hops">{stepCount != null && stepCount > 0 ? `${stepCount} of 12 steps` : ""}</span>
          <span className="lr-took">
            {took != null && pinned && visible.length > 0 ? (
              <>
                agents took <span className="mono">{fmtMs(took)}</span>
              </>
            ) : null}
          </span>
          <span className="mono lr-time">{started ? `${started} EAT` : ""}</span>
          {/* Last, so the room it holds for "waiting for a decision" is the gap before the button. */}
          <span className="lr-state">{pinnedFold ? <span>folded at Correlate</span> : status ? <RunState status={status} /> : null}</span>
        </div>
        <div className="lr-actions">
          {onOpen && (
            <button
              type="button"
              className={"btn sm lr-open" + (openId ? "" : " lr-hold")}
              onClick={() => openId && onOpen(openId)}
              tabIndex={openId ? undefined : -1}
              aria-hidden={openId ? undefined : true}
            >
              View ticket
            </button>
          )}
        </div>
      </div>
      <AgentRail
        steps={railSteps}
        live={live}
        compact={compact}
        caption={title}
        loading={!pinned && load === "loading"}
        selectedNode={selected}
        onSelect={(node) => onSelect(node)}
        detailAction={
          frozenId ? (
            <button type="button" className="btn sm" onClick={followLive} title="Let the panel move on to the newest ticket again">
              Follow live
            </button>
          ) : null
        }
      />
      <p className="lr-foot">{foot}</p>
    </section>
  );
}
