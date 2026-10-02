import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import LiveRunPanel from "../components/LiveRunPanel";
import { LIFECYCLE_NODES, fmtInt, fmtMinutes, fmtMs, humanAutonomy, nodeDoes, nodeLabel } from "../lib/agents";
import { fmtEAT } from "../lib/time";
import "./Showcase.css";

/**
 * The page for the people who decide, read in a minute: what the agents do to an alarm, what
 * it saved, how they sit on the platform the NOC already runs, and where people stay. Every
 * figure is read live from `api.productivity(windowHours)`; nothing on the page is a constant
 * dressed as a measurement.
 *
 * Each idea appears once: the live rail, the three figures, the twelve steps (with the
 * minutes by hand folded in as a column and a total), the platform diagram, the ladder and
 * the never-automated list. Per-agent numbers live on /agents.
 */

/** What a person did for each step before the agents. The "now" sentence is the step's own
 *  `does` in lib/agents.ts, the same sentence the Workflow map prints. */
const BEFORE: Record<string, string> = {
  INGEST: "Read the alarm off the NMS and work out which site it is.",
  CORRELATE: "Search the ticket queue before raising a duplicate.",
  ENRICH: "Open the CMDB and the on-call sheet; guess the impact.",
  SEVERITY: "Judge the priority, argue it later.",
  TICKET: "Type the ticket into the UI, field by field.",
  ASSIGN: "Remember who covers power in Rift tonight.",
  HITL: "Nothing changes here: the decision stays human.",
  BROADCAST: "Write the broadcast, find the numbers, send.",
  EXEC_BRIEF: "Answer the phone, again.",
  LEDGER: "Update the Excel sheet.",
  RECURRENCE: "Notice, eventually, that this mast keeps failing.",
  MONITOR: "Set a reminder; forget it at shift change.",
};

const LADDER = [
  { level: "L1_COPILOT", label: "L1 co-pilot", text: "Agents draft everything; a person approves every send." },
  { level: "L2_GUARDED", label: "L2 guarded", text: "HUB and CORE tickets open on their own; P3 and P4 broadcasts go; P1 and P2 wait for a person." },
  { level: "L3_CONDITIONAL", label: "L3 conditional", text: "Only P1 waits. Note chasing and the handover run unattended. Still never a live network change." },
];

function plural(n: number, word: string): string {
  return `${fmtInt(n)} ${n === 1 ? word : `${word}s`}`;
}

/** How much a productivity answer has on record: alarms, steps and decisions together. */
function recordSize(d: any): number {
  return (
    Number(d?.alarms?.processed || 0) +
    Number(d?.steps?.total || 0) +
    Number(d?.hitl?.approved || 0) +
    Number(d?.hitl?.rejected || 0)
  );
}

type Window = 0 | 24;

export default function Showcase({
  profile,
  metrics,
  runsRev,
}: {
  profile: any;
  metrics: any;
  runsRev: number;
}) {
  const nav = useNavigate();
  const [windowHours, setWindowHours] = useState<Window>(0);
  const [p, setP] = useState<any | null>(null);
  const [err, setErr] = useState("");
  const [retry, setRetry] = useState(0);
  // null until `api.agents()` answers: the tool-connection sentence and the agent count are
  // drawn only from a real answer, never from a default.
  const [agents, setAgents] = useState<any[] | null>(null);
  // Whether anything on record is older than 24 hours. Until it is known (or when it is not),
  // the two windows show the same numbers, so the page says "All time" instead of offering a
  // toggle that changes nothing.
  const [spansDay, setSpansDay] = useState(false);
  const lastKey = useRef("");
  const fired = useRef(0); // rollups requested
  const applied = useRef(0); // the newest request whose answer is on screen
  const mounted = useRef(false);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    // `runsRev` moves with every processed alarm (and again from the storm's own tick), so a
    // refresh trails the burst by a moment and a storm costs one rollup per pause, not one per
    // alarm. A new window or a Retry answers at once.
    const key = `${windowHours}:${retry}`;
    const delay = key === lastKey.current ? 600 : 0;
    lastKey.current = key;
    const t = window.setTimeout(() => {
      const mine = ++fired.current;
      api
        .productivity(windowHours)
        .then((d) => {
          // A newer refresh may already be queued; this answer is still newer than the one on
          // screen, so it is shown rather than dropped (a busy storm never starves the page).
          if (!mounted.current || mine < applied.current) return;
          applied.current = mine;
          setP(d);
          if (mine === fired.current) setErr("");
          // An all-time answer, and the toggle is still hidden: ask the last 24 hours straight
          // after and compare the pair. More on record than in the last day means older data.
          if (windowHours === 0 && !spansDayRef.current && Date.now() - lastDayCheck.current > 60_000) {
            lastDayCheck.current = Date.now();
            api
              .productivity(24)
              .then((recent) => {
                if (mounted.current && recordSize(d) > recordSize(recent)) setSpansDay(true);
              })
              .catch(() => undefined);
          }
        })
        .catch((e) => {
          if (mounted.current && mine === fired.current) setErr(String(e?.message || e));
        });
    }, delay);
    return () => window.clearTimeout(t);
  }, [windowHours, runsRev, retry]);

  const spansDayRef = useRef(spansDay);
  spansDayRef.current = spansDay;
  const lastDayCheck = useRef(0);

  useEffect(() => {
    let cancelled = false;
    api
      .agents()
      .then((a) => {
        if (!cancelled) setAgents(Array.isArray(a) ? a : []);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);

  const mcp = useMemo(() => {
    if (!agents) return null;
    let cards = 0;
    const servers = new Set<string>();
    const writeGated = new Set<string>();
    for (const a of agents) {
      for (const m of a?.mcp || []) {
        cards += 1;
        if (m?.server) servers.add(m.server);
        if ((m?.write_tools || []).length > 0 && m?.hitl_task_type) writeGated.add(m.hitl_task_type);
      }
    }
    return { agents: agents.length, cards, servers: servers.size, writeGated: writeGated.size };
  }, [agents]);

  const retryNow = () => {
    setErr("");
    setRetry((r) => r + 1);
  };

  // The figures belong to one window; an answer for the other window is not shown under this one.
  const fresh = p && Number(p.window_hours) === windowHours ? p : null;
  const autonomy = String(profile?.autonomy_level || p?.autonomy_level || "L2_GUARDED");
  const waitingNow: number | null =
    typeof metrics?.hitl_pending === "number" ? metrics.hitl_pending : typeof fresh?.hitl?.pending === "number" ? fresh.hitl.pending : null;

  return (
    <div className="showcase">
      <section className="sc-hero" aria-labelledby="sc-title">
        <div className="sc-hero-text">
          <h1 id="sc-title">Alarm to filled-in ticket in under a second.</h1>
          <p className="sc-lead">
            <span>Agents fill every field, choose the vendor, draft the SMS and email, and chase the reply.</span>{" "}
            <span>P1 and P2 messages wait for a named person, and every step is on record.</span>
          </p>
          <button type="button" className="btn primary" onClick={() => nav("/")}>
            Watch it live on Mission control
          </button>
        </div>

        <div className="sc-figures">
          {spansDay ? (
            <div className="seg" role="group" aria-label="Numbers window">
              <button type="button" aria-pressed={windowHours === 24} onClick={() => setWindowHours(24)}>
                Last 24 h
              </button>
              <button type="button" aria-pressed={windowHours === 0} onClick={() => setWindowHours(0)}>
                All time
              </button>
            </div>
          ) : (
            <p className="sc-window">All time</p>
          )}
          {err && !fresh ? (
            <div className="empty sc-alert" role="alert" title={err}>
              <span>Couldn't load the numbers.</span>
              <button type="button" className="btn sm" onClick={retryNow}>
                Retry
              </button>
            </div>
          ) : (
            <>
              {err && fresh && (
                <div className="empty sc-alert" role="alert" title={err}>
                  <span>Couldn't refresh; these are from {fmtEAT(fresh.generated_at)}.</span>
                  <button type="button" className="btn sm" onClick={retryNow}>
                    Retry
                  </button>
                </div>
              )}
              <Figures p={fresh} windowHours={windowHours} waitingNow={waitingNow} />
            </>
          )}
        </div>
      </section>

      <div className="sc-hero-rail">
        <LiveRunPanel runsRev={runsRev} onOpen={(id) => nav(`/incidents/${id}`)} compact />
      </div>

      <section className="sc-section" aria-labelledby="sc-steps-title">
        <h2 id="sc-steps-title">What changed for the floor</h2>
        <p className="sc-lead">The twelve steps of every service-affecting alarm, before and now.</p>
        <Steps p={p} fresh={fresh} failed={!!err && !p} windowHours={windowHours} />
      </section>

      <section className="sc-section" aria-labelledby="sc-arch-title">
        <h2 id="sc-arch-title">How it sits on the platform</h2>
        <p className="sc-lead">Nothing is replaced: agents work through adapters to what you already run.</p>
        <Architecture mcp={mcp} />
      </section>

      <section className="sc-section sc-two">
        <div>
          <h2>Where people decide</h2>
          <p className="sc-lead">This deployment runs at {humanAutonomy(autonomy)}; the level is one setting.</p>
          <ol className="sc-ladder">
            {LADDER.map((l) => (
              <li
                key={l.level}
                className={"sc-rung" + (l.level === autonomy ? " current" : "")}
                aria-current={l.level === autonomy ? "true" : undefined}
              >
                <strong>{l.label}</strong>
                <span>{l.text}</span>
              </li>
            ))}
          </ol>
        </div>
        <div>
          <h2>What is never automated</h2>
          <ul className="sc-never">
            <li>Wording that reaches management or leaves the building on a P1 or P2.</li>
            <li>Overriding a priority or disputing an assignment.</li>
            <li>Sending the shift handover.</li>
            <li>Any change to a live network element. There is no such tool to call.</li>
            <li>
              {mcp && mcp.writeGated > 0
                ? `Any write into another system: ${plural(mcp.writeGated, "write type")}, each behind a named approval.`
                : "Any write into another system; each one waits for a named approval."}
            </li>
          </ul>
        </div>
      </section>

      <div className="sc-try">
        <p>Try it yourself: launch the storm on Mission control, then approve or reject the held broadcasts.</p>
        <button type="button" className="btn" onClick={() => nav("/hitl")}>
          Open Approvals
        </button>
      </div>
    </div>
  );
}

/** The three figures, in the sans, each a value and one line that says what it counts. */
function Figures({ p, windowHours, waitingNow }: { p: any | null; windowHours: Window; waitingNow: number | null }) {
  if (!p) {
    return (
      <ul className="sc-fig-list" aria-busy="true" aria-label="Loading the numbers">
        {[0, 1, 2].map((i) => (
          <li key={i} className="sc-fig">
            <span className="skeleton sc-skel-value" />
            <span className="skeleton sc-skel-label" />
          </li>
        ))}
      </ul>
    );
  }

  const toil = p.toil || {};
  const alarms = p.alarms || {};
  const hitl = p.hitl || {};
  const processed = Number(alarms.processed || 0);
  const created = Number(alarms.incidents_created || 0);
  const absorbed = Math.max(0, Math.round(Number(alarms.absorbed) || 0));
  const made = Number(hitl.approved || 0) + Number(hitl.rejected || 0);
  const span = windowHours ? "in the last 24 hours" : "on record";

  const minutesTitle =
    `${fmtMinutes(toil.minutes_saved)} of steps taken over, less ${fmtMinutes(toil.human_minutes_spent)} people spent deciding. ` +
    "Minutes by hand are the floor's own estimates, not a stopwatch study.";

  // Always both numbers: decided by a person, and waiting now (0 is a real answer).
  const decisionsLabel = "by a person" + (waitingNow == null ? "" : `; ${fmtInt(waitingNow)} waiting now`);

  return (
    <ul className="sc-fig-list" aria-live="polite">
      <li className="sc-fig" title={minutesTitle}>
        <span className="sc-fig-value">{fmtMinutes(Math.max(0, Number(toil.net_minutes_saved) || 0))}</span>{" "}
        <span className="sc-fig-label">of analyst work saved (estimate, after approval time)</span>
      </li>
      <li className="sc-fig">
        {processed > 0 ? (
          <>
            <span className="sc-fig-value">{plural(created, "ticket")}</span>{" "}
            {/* Whole numbers a manager can say aloud: "6 of 11 alarms", never "54.5 %". */}
            <span className="sc-fig-label">
              {absorbed > 0
                ? `from ${plural(processed, "alarm")}; ${fmtInt(absorbed)} folded into a ticket already open`
                : `from ${plural(processed, "alarm")}`}
            </span>
          </>
        ) : (
          <>
            <span className="sc-fig-value">No alarms yet</span>{" "}
            <span className="sc-fig-label">{windowHours ? span : "launch the storm on Mission control"}</span>
          </>
        )}
      </li>
      <li className="sc-fig" title={hitl.median_decision_minutes != null ? `A decision takes ${hitl.median_decision_minutes} min on median.` : undefined}>
        <span className="sc-fig-value">{fmtInt(made)} decided</span>{" "}
        <span className="sc-fig-label">{decisionsLabel}</span>
      </li>
    </ul>
  );
}

/** The twelve steps, before and now, with the minutes by hand as a column and a total. */
function Steps({ p, fresh, failed, windowHours }: { p: any | null; fresh: any | null; failed: boolean; windowHours: Window }) {
  // The live node list wins; until it answers, the registry order keeps the copy on screen and
  // only the minutes wait.
  const byNode: any[] = Array.isArray(p?.steps?.by_node) && p.steps.by_node.length ? p.steps.by_node : [];
  const live = new Map<string, any>(byNode.map((n) => [String(n.node), n]));
  const ids: string[] = byNode.length ? byNode.map((n) => String(n.node)) : LIFECYCLE_NODES.map((n) => n.id);
  const totalByHand = byNode.reduce((sum, n) => sum + Number(n.toil_minutes_each || 0), 0);
  // One time for one run (the rule the rail and the ticket follow): what the agents worked, the
  // sum of their step times, never started-to-finished, which grows by hours when a person
  // approves later. Per ticket: each step's average time, added up.
  const freshNodes: any[] = Array.isArray(fresh?.steps?.by_node) ? fresh.steps.by_node : [];
  const agentMs = freshNodes.length ? freshNodes.reduce((sum, n) => sum + (Number(n.avg_ms) || 0), 0) : null;
  const span = windowHours ? "in the last 24 hours" : "on record";

  const minutes = (content: string) => (p ? content : failed ? "—" : <span className="skeleton sc-skel-min" aria-hidden="true" />);

  return (
    <table className="sc-steps">
      <thead>
        <tr>
          <th scope="col">Step</th>
          <th scope="col">Before</th>
          <th scope="col">Now</th>
          <th scope="col" className="sc-num">
            Minutes by hand
          </th>
        </tr>
      </thead>
      <tbody>
        {ids.map((id, i) => {
          const n = live.get(id);
          const human = id === "HITL";
          const each = Number(n?.toil_minutes_each || 0);
          const fn = fresh ? (fresh.steps?.by_node || []).find((x: any) => x.node === id) : null;
          const done = fn ? Number(fn.succeeded || 0) + Number(fn.waiting_hitl || 0) : null;
          return (
            <tr key={id} className={human ? "human" : undefined}>
              <th scope="row">
                <span className="sc-step-no">{i + 1}</span>
                {nodeLabel(id)}
              </th>
              <td className="sc-before" data-label="Before">
                {BEFORE[id] || ""}
              </td>
              <td data-label="Now">{nodeDoes(id)}</td>
              <td
                className="sc-num"
                title={done != null && !human ? `${fmtInt(done)} × ${fmtMinutes(each)} = ${fmtMinutes(fn.minutes_saved)} taken over ${span}` : undefined}
              >
                {minutes(human ? "stays human" : fmtMinutes(each))}
              </td>
            </tr>
          );
        })}
      </tbody>
      <tfoot>
        <tr>
          <th scope="row">Per alarm</th>
          <td colSpan={2} className="sc-total-note">
            Minutes by hand are the floor's own estimates, not a stopwatch study.
            {agentMs != null && agentMs > 0 ? ` The agents' own steps take ${fmtMs(agentMs)} per ticket, on average.` : ""}
          </td>
          <td className="sc-num">{minutes(fmtMinutes(totalByHand))}</td>
        </tr>
      </tfoot>
    </table>
  );
}

/** The platform diagram: existing systems on the left, adapters, the agents, people on the right.
 *  Labels are horizontal and sized to their boxes; under 880 px the figure scrolls sideways. */
function Architecture({ mcp }: { mcp: { agents: number; cards: number; servers: number } | null }) {
  const left = ["NMS and EMS alarm feeds", "Ticketing system", "Site catalogue and CMDB", "Mail and SMS gateways", "Excel shift ledger"];
  const right = ["Approvals: read and decide", "Wallboard and Mission control", "Exec brief readers", "Regional office, engineer, vendor"];
  const rightY = [34, 103, 173, 242];
  return (
    <figure className="sc-arch" tabIndex={0} aria-label="Platform diagram">
      <svg viewBox="0 0 980 292" role="img" aria-labelledby="sc-arch-svg-title sc-arch-svg-desc">
        <title id="sc-arch-svg-title">How the agents sit on the existing platform</title>
        <desc id="sc-arch-svg-desc">
          Existing systems on the left connect through adapters, mock today and real later, to the agents and their
          approval gate, which hand decisions and messages to people on the right.
        </desc>
        <defs>
          <marker id="sc-arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M0 0 L10 5 L0 10 z" className="sc-arch-head" />
          </marker>
        </defs>

        <text x="1" y="18" className="sc-arch-h">
          Existing systems
        </text>
        {left.map((t, i) => {
          const y = 34 + i * 52;
          return (
            <g key={t}>
              <rect x="1" y={y} width="219" height="40" rx="8" className="sc-arch-box" />
              <text x="15" y={y + 25} className="sc-arch-t">
                {t}
              </text>
              <line x1="221" y1={y + 20} x2="261" y2={y + 20} className="sc-arch-line" markerStart="url(#sc-arr)" markerEnd="url(#sc-arr)" />
            </g>
          );
        })}

        <rect x="262" y="34" width="90" height="248" rx="10" className="sc-arch-adapt" />
        <text x="307" y="142" textAnchor="middle" className="sc-arch-t strong">
          Adapters
        </text>
        <text x="307" y="162" textAnchor="middle" className="sc-arch-s">
          mock today,
        </text>
        <text x="307" y="178" textAnchor="middle" className="sc-arch-s">
          real later
        </text>
        <line x1="353" y1="158" x2="391" y2="158" className="sc-arch-line" markerStart="url(#sc-arr)" markerEnd="url(#sc-arr)" />

        <rect x="392" y="34" width="310" height="248" rx="12" className="sc-arch-agents" />
        <text x="547" y="66" textAnchor="middle" className="sc-arch-h">
          {mcp && mcp.agents > 0 ? `${mcp.agents} agents` : "The agents"}
        </text>
        <rect x="420" y="86" width="254" height="38" rx="8" className="sc-arch-gate" />
        <text x="547" y="110" textAnchor="middle" className="sc-arch-t">
          Approval gate
        </text>
        <text x="547" y="156" textAnchor="middle" className="sc-arch-s">
          Every step logs its reason, tools
        </text>
        <text x="547" y="173" textAnchor="middle" className="sc-arch-s">
          and timing in the audit trail
        </text>
        {mcp &&
          (mcp.cards > 0 ? (
            <>
              <text x="547" y="207" textAnchor="middle" className="sc-arch-s">
                {`${fmtInt(mcp.cards)} tool connections to ${fmtInt(mcp.servers)} systems,`}
              </text>
              <text x="547" y="224" textAnchor="middle" className="sc-arch-s">
                read-only or behind an approval;
              </text>
              <text x="547" y="241" textAnchor="middle" className="sc-arch-s">
                none switched on in this demo
              </text>
            </>
          ) : (
            <text x="547" y="215" textAnchor="middle" className="sc-arch-s">
              No external tool connections declared
            </text>
          ))}

        <text x="742" y="18" className="sc-arch-h">
          People
        </text>
        {right.map((t, i) => {
          const y = rightY[i];
          return (
            <g key={t}>
              <line x1="703" y1={y + 20} x2="741" y2={y + 20} className="sc-arch-line" markerEnd="url(#sc-arr)" />
              <rect x="742" y={y} width="237" height="40" rx="8" className={"sc-arch-box" + (i === 0 ? " human" : "")} />
              <text x="756" y={y + 25} className="sc-arch-t">
                {t}
              </text>
            </g>
          );
        })}
      </svg>
    </figure>
  );
}
