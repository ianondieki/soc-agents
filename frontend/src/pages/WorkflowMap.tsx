import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  Check,
  Inbox,
  Megaphone,
  Minus,
  Pause,
  Play,
  Repeat2,
  RotateCcw,
  ScanSearch,
  Ticket,
  User,
  UserCheck,
  X,
  type LucideIcon,
} from "lucide-react";
import { api } from "../api";
import {
  LIFECYCLE_NODES,
  STATUS_WORD,
  agentDisplayName,
  displaySteps,
  fmtInt,
  fmtMs,
  foldOf,
  humanAutonomy,
  nodeDoes,
  nodeLabel,
  normaliseStatus,
  runStatusWord,
  ticketNumberOf,
  type NodeStatus,
  type RailStep,
} from "../lib/agents";
import { fibreColour, fibreOf } from "../lib/fibre";
import { useCalm, useCountUp, useSeenOnce } from "../lib/motion";
import { fmtHM } from "../lib/time";
import { useRunFrames } from "../realtime/RealtimeContext";
import "./WorkflowMap.css";

/**
 * The lifecycle, taught and shown. Four parts:
 *
 * 1. The route: the twelve steps on one line in six phases. It replays the latest alarm's real
 *    path, step by step, and stops at Approval while that alarm waits for a person. Every status
 *    on it is the run's own (lib/agents.ts displaySteps), so nothing lights that never ran. It
 *    pauses on request (WCAG 2.2.2), and quiet mode or reduced motion draws the end state.
 * 2. Four figures that count up once.
 * 3. The phases as cards: what each step does, its agent, its minutes by hand and its runs.
 * 4. The reasoning path: any recent alarm's steps as a dark tape, each with the agent's reason.
 *
 * A live step frame refreshes the runs and rings its step on the route for a moment.
 */

/** The phases an alarm goes through, in order: the steps each one holds. */
const PHASES: Array<{ title: string; icon: LucideIcon; nodes: string[] }> = [
  { title: "Take the alarm in", icon: Inbox, nodes: ["INGEST", "CORRELATE"] },
  { title: "Work out what it is", icon: ScanSearch, nodes: ["ENRICH", "SEVERITY"] },
  { title: "Open and assign the ticket", icon: Ticket, nodes: ["TICKET", "ASSIGN"] },
  { title: "A person decides", icon: UserCheck, nodes: ["HITL"] },
  { title: "Tell people and keep the record", icon: Megaphone, nodes: ["BROADCAST", "EXEC_BRIEF", "LEDGER"] },
  { title: "Follow up", icon: Repeat2, nodes: ["RECURRENCE", "MONITOR"] },
];

const ORDER = LIFECYCLE_NODES.map((n) => n.id);

/** The route's labels with soft hyphens, so the smallest phones break a long name tidily. */
const SHY_LABEL: Record<string, string> = {
  CORRELATE: "Corre\u00ADlate",
  SEVERITY: "Sever\u00ADity",
  HITL: "Approv\u00ADal",
  BROADCAST: "Broad\u00ADcast",
  RECURRENCE: "Recur\u00ADrence",
  MONITOR: "Moni\u00ADtor",
};

/** How long the replay dwells on a step, on Approval while it waits, and at the end. */
const STEP_MS = 650;
const HOLD_MS = 2600;
const REST_MS = 5200;
/** How long a live frame rings its step on the route. */
const LIVE_MS = 2400;

type Tone = "done" | "held" | "failed" | "hollow" | "pending";

function toneOf(st: NodeStatus | undefined): Tone {
  if (st === "succeeded" || st === "decided") return "done";
  if (st === "waiting_hitl") return "held";
  if (st === "failed") return "failed";
  if (st === "not_needed" || st === "skipped") return "hollow";
  return "pending";
}

const TONE_ICON: Record<Tone, LucideIcon | null> = { done: Check, held: User, failed: X, hollow: Minus, pending: null };

function startedMs(r: any): number {
  const t = Date.parse(String(r?.started_at ?? ""));
  return Number.isFinite(t) ? t : 0;
}

/** A run's name in a list: its ticket, or the ticket it folded into, then its start time. */
function runName(r: any): string {
  const t = ticketNumberOf(r);
  if (t) return t;
  const fold = foldOf(r);
  if (fold?.into) return `Repeat of ${fold.into}`;
  return "Alarm";
}

export default function WorkflowMap({ profile }: { profile: any }) {
  const calm = useCalm();
  const [p, setP] = useState<any | null>(null);
  const [runs, setRuns] = useState<any[] | null>(null);
  const [picked, setPicked] = useState<string | null>(null);
  const [incidents, setIncidents] = useState<Record<string, any>>({});

  useEffect(() => {
    api.productivity(0).then(setP).catch(() => undefined);
  }, []);

  const loadRuns = useRef(() => {
    api
      .lifecycleRuns()
      .then((rs) => setRuns((Array.isArray(rs) ? rs : []).slice().sort((a, b) => startedMs(b) - startedMs(a))))
      .catch(() => setRuns((old) => old ?? []));
  });
  useEffect(() => loadRuns.current(), []);

  // Live step frames: refresh the runs (once the burst settles) and ring the step that just ran.
  const frames = useRunFrames();
  const lastStep = useMemo(() => {
    for (let i = frames.length - 1; i >= 0; i--) {
      const f = frames[i];
      if (f.type === "agent.step.completed" || f.type === "agent.step.started") {
        const node = String(f.payload?.node || "");
        if (ORDER.includes(node)) return { node, at: f.receivedAt };
      }
    }
    return null;
  }, [frames]);
  const [liveNode, setLiveNode] = useState<string | null>(null);
  useEffect(() => {
    if (!lastStep) return;
    const left = lastStep.at + LIVE_MS - Date.now();
    if (left <= 0) return;
    setLiveNode(lastStep.node);
    const off = window.setTimeout(() => setLiveNode(null), left);
    const refresh = window.setTimeout(() => loadRuns.current(), 1200);
    return () => {
      window.clearTimeout(off);
      window.clearTimeout(refresh);
    };
  }, [lastStep]);

  const run = useMemo(() => {
    if (!runs?.length) return null;
    return (picked && runs.find((r) => r.id === picked)) || runs[0];
  }, [runs, picked]);

  useEffect(() => {
    const id = run?.incident_id;
    if (!id || incidents[id]) return;
    api
      .incident(id)
      .then((inc) => setIncidents((m) => ({ ...m, [id]: inc })))
      .catch(() => undefined);
  }, [run?.incident_id, incidents]);
  const incident = run?.incident_id ? incidents[run.incident_id] : null;

  // The agent each step belongs to: the rollup's when it has one, else the built-in list.
  const agentOf = useMemo(() => {
    const out: Record<string, string> = {};
    for (const n of LIFECYCLE_NODES) out[n.id] = n.agent;
    for (const n of p?.steps?.by_node || []) if (n?.node && n.agent) out[n.node] = n.agent;
    return out;
  }, [p]);
  const toil: Record<string, number> = p?.toil?.assumptions?.toil_minutes || {};
  const byNode: Record<string, any> = {};
  for (const n of p?.steps?.by_node || []) byNode[n.node] = n;
  const autonomy = humanAutonomy(profile?.autonomy_level);
  const byHand = p
    ? Object.entries(toil)
        .filter(([k]) => k !== "HITL")
        .reduce((sum, [, v]) => sum + (Number(v) || 0), 0)
    : null;

  return (
    <div className="wm">
      <div className="page-head">
        <div>
          <p className="eyebrow">The lifecycle</p>
          <h1>Workflow map</h1>
          <p className="lead">
            Every alarm takes the same twelve steps, each run by its own agent. At {autonomy}, P1 and P2 messages wait
            for a person at Approval.
          </p>
        </div>
      </div>

      <Route run={run} incident={incident} calm={calm} liveNode={liveNode} />

      <Figures byHand={byHand} median={p?.pipeline_ms?.median ?? null} alarms={p?.alarms?.processed ?? null} calm={calm} />

      <section className="wm-phases" aria-labelledby="wm-phases-title">
        <div className="wm-section-head">
          <h2 id="wm-phases-title">The twelve steps</h2>
          <p>What each agent does, how long a person takes over it by hand, and how often it has run.</p>
        </div>
        <ol className="wm-phase-grid">
          {PHASES.map((phase) => (
            <PhaseCard key={phase.title} phase={phase} agentOf={agentOf} toil={toil} byNode={byNode} autonomy={autonomy} />
          ))}
        </ol>
        <p className="wm-note">
          By hand: the floor's own estimate of a person's minutes per step, not a stopwatch study. The Showcase multiplies
          it by the steps the agents completed.
        </p>
      </section>

      <Reasoning runs={runs} run={run} incident={incident} onPick={setPicked} calm={calm} />
    </div>
  );
}

/* ------------------------------------------------------------------------------------------ */
/* The route                                                                                  */
/* ------------------------------------------------------------------------------------------ */

function Route({ run, incident, calm, liveNode }: { run: any | null; incident: any | null; calm: boolean; liveNode: string | null }) {
  const steps = useMemo(() => displaySteps(run), [run]);
  const byId = useMemo(() => {
    const out: Record<string, RailStep> = {};
    for (const s of steps) out[s.node_name] = s;
    return out;
  }, [steps]);
  const tones = useMemo(() => ORDER.map((id) => toneOf(byId[id] ? normaliseStatus(byId[id].status) : undefined)), [byId]);
  // The last step the run reached: the replay ends there.
  const reached = Math.max(-1, ...ORDER.map((id, i) => (byId[id] && tones[i] !== "pending" ? i : -1)));

  const [paused, setPaused] = useState(false);
  const [pos, setPos] = useState(-1);
  const [round, setRound] = useState(0);
  const moving = !calm && !paused && reached >= 0;

  // Start over when the run changes or someone asks for a replay.
  useEffect(() => setPos(-1), [run?.id, round]);
  useEffect(() => {
    if (!moving) return;
    let wait: number;
    if (pos >= reached) wait = REST_MS;
    else if (pos >= 0 && ORDER[pos] === "HITL" && tones[pos] === "held") wait = HOLD_MS;
    else wait = pos < 0 ? 500 : STEP_MS;
    const t = window.setTimeout(() => setPos((x) => (x >= reached ? -1 : x + 1)), wait);
    return () => window.clearTimeout(t);
  }, [moving, pos, reached, tones]);

  // Calm, or nothing to replay: the end state. Paused: where it stopped.
  const at = calm ? reached : pos;
  const cur = at >= 0 && at <= reached ? at : -1;
  const curId = cur >= 0 ? ORDER[cur] : null;
  const curStep = curId ? byId[curId] : null;
  const curTone = cur >= 0 ? tones[cur] : "pending";

  const ticket = incident?.incident_number || ticketNumberOf(run);
  const fold = foldOf(run);
  const total = steps.reduce((s, x) => s + (Number(x.duration_ms) || 0), 0);
  const parkedAt = ORDER.findIndex((id, i) => id === "HITL" && tones[i] === "held");

  let caption: { lead: string; body: string } | null = null;
  if (curStep && curId && !calm && !(paused && cur === reached)) {
    caption = { lead: `${String(cur + 1).padStart(2, "0")} ${nodeLabel(curId)}`, body: String(curStep.rationale || nodeDoes(curId)) };
  } else if (run) {
    caption = {
      lead: "The whole run",
      body:
        `${fmtMs(total)} of agent time across ${steps.filter((s) => normaliseStatus(s.status) !== "skipped").length} steps` +
        (parkedAt >= 0 ? "; the messages wait at Approval for a person." : "."),
    };
  }

  return (
    <section className="panel wm-route" aria-labelledby="wm-route-title">
      <div className="wm-route-head">
        <div className="wm-route-id">
          <p className="wm-kicker">{run ? (calm ? "The latest alarm's path" : "Replaying the latest alarm") : "The route"}</p>
          <h2 id="wm-route-title">
            {run ? (
              <>
                {ticket || runName(run)}
                {incident?.site_name && <span className="wm-route-site"> at {incident.site_name}</span>}
              </>
            ) : (
              "Twelve steps, one line"
            )}
          </h2>
          {run && (
            <p className="wm-route-meta">
              {incident?.priority && <span className={`pill ${incident.priority}`}>{incident.priority}</span>}
              <span className={"wm-state " + (String(run.status).toUpperCase() === "WAITING_HITL" ? "hitl" : String(run.status).toUpperCase() === "FAILED" ? "danger" : "")}>
                {runStatusWord(run.status)}
              </span>
              <span>started {fmtHM(run.started_at)}</span>
              {fold?.into && <span>folded into {fold.into}</span>}
            </p>
          )}
        </div>
        {run && (
          <div className="wm-route-ctrl">
            {!calm && (
              <button type="button" className="btn sm" onClick={() => setPaused((x) => !x)}>
                {paused ? <Play size={14} aria-hidden="true" /> : <Pause size={14} aria-hidden="true" />}
                {paused ? "Play" : "Pause"}
              </button>
            )}
            {!calm && (
              <button
                type="button"
                className="btn sm"
                onClick={() => {
                  setPaused(false);
                  setRound((n) => n + 1);
                }}
              >
                <RotateCcw size={14} aria-hidden="true" />
                Replay
              </button>
            )}
            {run.incident_id && (
              <Link className="btn sm primary" to={`/incidents/${run.incident_id}`}>
                Open the ticket
              </Link>
            )}
          </div>
        )}
      </div>

      <div className="wm-line-wrap">
        <ol className={"wm-line" + (pos < 0 && !calm ? " is-reset" : "")} aria-label="The twelve steps in order, with this alarm's status at each">
          {PHASES.map((phase) => (
            <li key={phase.title} className={"wm-seg" + (phase.nodes.includes("HITL") ? " human" : "")} style={{ ["--n" as any]: phase.nodes.length }}>
              <ol className="wm-seg-nodes">
                {phase.nodes.map((id) => {
                  const i = ORDER.indexOf(id);
                  const f = fibreOf(id);
                  const tone = tones[i];
                  const shownTone: Tone = run ? (i <= at ? tone : "pending") : "pending";
                  const Icon = TONE_ICON[shownTone];
                  const fill = i < at ? 1 : i === at ? 0.5 : 0;
                  const st = byId[id] ? normaliseStatus(byId[id].status) : null;
                  return (
                    <li
                      key={id}
                      className={
                        "wm-node t-" +
                        shownTone +
                        (i === cur && !calm ? " is-here" : "") +
                        (liveNode === id ? " is-live" : "") +
                        (i === 0 ? " first" : "") +
                        (i === ORDER.length - 1 ? " last" : "") +
                        (id === "ASSIGN" ? " row-end" : id === "HITL" ? " row-start" : "")
                      }
                      style={{ ["--fill" as any]: fill }}
                    >
                      <a className="wm-dot" href={`#step-${id}`}>
                        <span className="wm-dot-n" aria-hidden="true">
                          {i + 1}
                        </span>
                        {Icon && (
                          <span className="wm-dot-badge" aria-hidden="true">
                            <Icon size={10} strokeWidth={3} />
                          </span>
                        )}
                        <span className="sr-only">
                          Step {i + 1}, {nodeLabel(id)}
                          {run && st && i <= at ? `: ${STATUS_WORD[st]}` : ""}
                        </span>
                      </a>
                      <span className="wm-node-label" aria-hidden="true">
                        {f && <span className={"wm-fibre" + (f.outlined ? " outlined" : "")} style={{ background: fibreColour(f) }} />}
                        {SHY_LABEL[id] ?? nodeLabel(id)}
                      </span>
                      {id === "HITL" && shownTone === "held" && (
                        <span className="wm-wait-tag" aria-hidden="true">
                          waits for a person
                        </span>
                      )}
                      {liveNode === id && (
                        <span className="wm-live-tag" aria-hidden="true">
                          live
                        </span>
                      )}
                    </li>
                  );
                })}
              </ol>
              <p className="wm-seg-title">{phase.title}</p>
            </li>
          ))}
        </ol>
      </div>

      {caption && (
        <p className={"wm-caption" + (curTone === "held" && !calm ? " hitl" : "")}>
          <span className="wm-caption-lead">{caption.lead}</span>
          <span className="wm-caption-body">{caption.body}</span>
        </p>
      )}
      {!run && <p className="wm-caption">No alarm has been through yet. The first one draws its path here.</p>}
    </section>
  );
}

/* ------------------------------------------------------------------------------------------ */
/* Figures                                                                                    */
/* ------------------------------------------------------------------------------------------ */

function Figures({ byHand, median, alarms, calm }: { byHand: number | null; median: number | null; alarms: number | null; calm: boolean }) {
  const steps = useCountUp(12, calm);
  const hand = useCountUp(byHand == null ? null : Math.round(byHand), calm);
  const through = useCountUp(alarms, calm);
  return (
    <dl className="wm-figures">
      <div>
        <dt>Steps per alarm</dt>
        <dd>{steps == null ? "—" : Math.round(steps)}</dd>
      </div>
      <div>
        <dt>By hand, per alarm</dt>
        <dd>{hand == null ? "—" : `${fmtInt(Math.round(hand))} min`}</dd>
      </div>
      <div>
        <dt>Agents, alarm to ticket (median)</dt>
        <dd>{median != null ? fmtMs(median) : "—"}</dd>
      </div>
      <div>
        <dt>Alarms through so far</dt>
        <dd>{through == null ? "—" : fmtInt(Math.round(through))}</dd>
      </div>
    </dl>
  );
}

/* ------------------------------------------------------------------------------------------ */
/* Phase cards                                                                                */
/* ------------------------------------------------------------------------------------------ */

function PhaseCard({
  phase,
  agentOf,
  toil,
  byNode,
  autonomy,
}: {
  phase: (typeof PHASES)[number];
  agentOf: Record<string, string>;
  toil: Record<string, number>;
  byNode: Record<string, any>;
  autonomy: string;
}) {
  const human = phase.nodes.includes("HITL");
  const first = ORDER.indexOf(phase.nodes[0]) + 1;
  const last = ORDER.indexOf(phase.nodes[phase.nodes.length - 1]) + 1;
  const Icon = phase.icon;
  return (
    <li className={"wm-card" + (human ? " human" : "")}>
      <div className="wm-card-head">
        <span className="wm-card-icon" aria-hidden="true">
          <Icon size={18} strokeWidth={2} />
        </span>
        <h3>{phase.title}</h3>
        <span className="wm-card-range">{first === last ? `Step ${first}` : `Steps ${first}–${last}`}</span>
      </div>
      <ol className="wm-steps">
        {phase.nodes.map((id) => {
          const f = fibreOf(id);
          const isHuman = id === "HITL";
          const stat = byNode[id];
          return (
            <li key={id} id={`step-${id}`} className={"wm-step" + (isHuman ? " human" : "")} tabIndex={-1}>
              <span className="wm-n" aria-hidden="true">
                {f?.n}
              </span>
              <div className="wm-main">
                <div className="wm-head">
                  <h4>
                    <span className="sr-only">Step {f?.n}: </span>
                    {nodeLabel(id)}
                  </h4>
                  <span className="wm-agent">
                    {f && (
                      <span
                        className={"wm-fibre" + (f.outlined ? " outlined" : "")}
                        style={{ background: fibreColour(f) }}
                        title={`${f.colour}, fibre ${f.n}`}
                      />
                    )}
                    {agentDisplayName(agentOf[id])}
                  </span>
                </div>
                <p className="wm-does">{nodeDoes(id)}</p>
                {isHuman && (
                  <p className="wm-human-note">
                    At {autonomy} a P1 or P2 message waits here for a named person; a P3 or P4 one goes on its own.
                  </p>
                )}
                <dl className="wm-nums">
                  <div>
                    <dt>By hand</dt>
                    <dd>{isHuman ? "stays human" : toil[id] != null ? `${toil[id]} min` : "—"}</dd>
                  </div>
                  <div>
                    <dt>Runs</dt>
                    <dd>{fmtInt(stat?.steps ?? 0)}</dd>
                  </div>
                  <div>
                    <dt>Average</dt>
                    <dd>{fmtMs(stat?.avg_ms)}</dd>
                  </div>
                </dl>
              </div>
            </li>
          );
        })}
      </ol>
    </li>
  );
}

/* ------------------------------------------------------------------------------------------ */
/* The reasoning path                                                                         */
/* ------------------------------------------------------------------------------------------ */

function Reasoning({
  runs,
  run,
  incident,
  onPick,
  calm,
}: {
  runs: any[] | null;
  run: any | null;
  incident: any | null;
  onPick: (id: string) => void;
  calm: boolean;
}) {
  const [ref, seen] = useSeenOnce<HTMLElement>(0.25);
  const steps = useMemo(() => {
    const byId: Record<string, RailStep> = {};
    for (const s of displaySteps(run)) byId[s.node_name] = s;
    return ORDER.map((id) => byId[id]).filter(Boolean) as RailStep[];
  }, [run]);
  // The tape types itself out once it is in view; a new alarm types out again.
  const [shown, setShown] = useState(calm ? steps.length : 0);
  useEffect(() => {
    if (calm) {
      setShown(steps.length);
      return;
    }
    if (!seen) return;
    setShown(0);
    let n = 0;
    const t = window.setInterval(() => {
      n += 1;
      setShown(n);
      if (n >= steps.length) window.clearInterval(t);
    }, 140);
    return () => window.clearInterval(t);
  }, [calm, seen, steps.length, run?.id]);

  const options = (runs || []).slice(0, 30);
  const ticket = incident?.incident_number || ticketNumberOf(run);

  return (
    <section className="wm-reason" aria-labelledby="wm-reason-title" ref={ref}>
      <div className="wm-reason-side">
        <p className="wm-reason-kicker">The reasoning path</p>
        <h2 id="wm-reason-title">Every step names its agent and its reason.</h2>
        <p className="wm-reason-lead">
          Nothing happens off the record. Pick an alarm to read what each agent saw, what it decided and why, in the
          words it wrote to the audit trail.
        </p>
        {options.length > 0 && (
          <label className="wm-reason-pick">
            <span>Alarm</span>
            <select value={run?.id ?? ""} onChange={(e) => onPick(e.target.value)}>
              {options.map((r) => (
                <option key={r.id} value={r.id}>
                  {runName(r)}, {fmtHM(r.started_at)}, {runStatusWord(r.status)}
                </option>
              ))}
            </select>
          </label>
        )}
        {run && (
          <dl className="wm-reason-facts">
            <div>
              <dt>Ticket</dt>
              <dd>{ticket || "none"}</dd>
            </div>
            <div>
              <dt>Priority</dt>
              <dd>{incident?.priority || "—"}</dd>
            </div>
            <div>
              <dt>Agent time</dt>
              <dd>{fmtMs(steps.reduce((s, x) => s + (Number(x.duration_ms) || 0), 0))}</dd>
            </div>
          </dl>
        )}
      </div>

      <div className="wm-tape" role="region" aria-label="The steps of this alarm, with each agent's reason" tabIndex={0}>
        {!run && <p className="wm-tape-empty">{runs == null ? "Loading the latest alarms…" : "No alarm has been through yet."}</p>}
        {run && (
          <ol>
            {steps.map((s, i) => {
              const st = normaliseStatus(s.status);
              const tone = toneOf(st);
              const Icon = TONE_ICON[tone];
              const f = fibreOf(s.node_name);
              return (
                <li key={s.node_name} className={"wm-tline t-" + tone + (i < shown ? " is-in" : "")}>
                  <span className="wm-tline-n">{String(ORDER.indexOf(s.node_name) + 1).padStart(2, "0")}</span>
                  <div className="wm-tline-main">
                    <p className="wm-tline-head">
                      {f && <span className={"wm-fibre" + (f.outlined ? " outlined" : "")} style={{ background: fibreColour(f) }} />}
                      <strong>{nodeLabel(s.node_name)}</strong>
                      <span className="wm-tline-agent">{agentDisplayName(s.agent_name)}</span>
                    </p>
                    {s.rationale && <p className="wm-tline-why">{s.rationale}</p>}
                    {s.output_summary && <p className="wm-tline-out">{s.output_summary}</p>}
                  </div>
                  <span className="wm-tline-meta">
                    <span className="wm-tline-state">
                      {Icon && <Icon size={12} strokeWidth={3} aria-hidden="true" />}
                      {STATUS_WORD[st]}
                    </span>
                    {s.duration_ms != null && st !== "skipped" && <span>{fmtMs(s.duration_ms)}</span>}
                  </span>
                </li>
              );
            })}
          </ol>
        )}
      </div>
    </section>
  );
}
