import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import AgentRail from "../components/AgentRail";
import LiveRunPanel from "../components/LiveRunPanel";
import ToilBars from "../components/ToilBars";
import { agentDisplayName, fmtInt, fmtMinutes, fmtMs } from "../lib/agents";
import type { NocEvent } from "../realtime/renderers";

/**
 * The page for the people who decide: what the agents do to an alarm, how they sit on
 * the platform the NOC already runs, where the humans stay, and what it saved — every
 * number read live from the same database the floor works in.
 *
 * Mode: persuade. The one loud element is the live rail in the hero; everything under it
 * is set quietly so the numbers and the diagram carry the argument.
 */

/** What each hop does, in the floor's words, and what a person used to do instead. */
const HOP_STORY: Record<string, { does: string; instead: string }> = {
  INGEST: { does: "Normalises the alarm: site, domain, technology, fingerprint.", instead: "Read the alarm off the NMS and work out which site it is." },
  CORRELATE: { does: "Finds an open ticket or a parent HUB and folds the alarm into it.", instead: "Search the ticket queue before raising a duplicate." },
  ENRICH: { does: "Looks up the site, region, RNIO and on-call FE; estimates subscribers affected.", instead: "Open the CMDB and the on-call sheet; guess the impact." },
  SEVERITY: { does: "Applies the P1–P4 thresholds, the HUB and CORE floors, the M-PESA corridor tag.", instead: "Judge the priority, argue it later." },
  TICKET: { does: "Allocates the INC number and fills every ticket field and the narrative.", instead: "Type the ticket into the UI, field by field." },
  ASSIGN: { does: "Routes to the MSP or FE from the region-by-domain matrix and stamps the escalation.", instead: "Remember who covers power in Rift tonight." },
  HITL: { does: "Holds P1 and P2 wording for a person; lets P3 and P4 go.", instead: "Nothing changes here: the decision stays human." },
  BROADCAST: { does: "Drafts and addresses the RNIO, FE and MSP SMS and e-mail.", instead: "Write the broadcast, find the numbers, send." },
  EXEC_BRIEF: { does: "Writes and refreshes the status brief managers read instead of calling.", instead: "Answer the phone, again." },
  LEDGER: { does: "Appends the shift ledger row.", instead: "Update the Excel sheet." },
  RECURRENCE: { does: "Counts the site's recent faults and opens a problem record when it is chronic.", instead: "Notice, eventually, that this mast keeps failing." },
  MONITOR: { does: "Sets the note-chase and SLA clocks and chases silence.", instead: "Set a reminder; forget it at shift change." },
};

const LADDER = [
  { level: "L1_COPILOT", label: "L1 co-pilot", text: "Agents draft everything; a person approves every send." },
  { level: "L2_GUARDED", label: "L2 guarded", text: "HUB and CORE tickets open on their own; P3 and P4 broadcasts go; P1 and P2 wait for a person." },
  { level: "L3_CONDITIONAL", label: "L3 conditional", text: "Only P1 waits. Note chasing and the handover run unattended. Still never a live network change." },
];

export default function Showcase({
  profile,
  metrics,
  events,
  runsRev,
}: {
  profile: any;
  metrics: any;
  events: NocEvent[];
  runsRev: number;
}) {
  const nav = useNavigate();
  const [windowHours, setWindowHours] = useState<0 | 24>(0);
  const [p, setP] = useState<any | null>(null);
  const [agents, setAgents] = useState<any[]>([]);
  const [err, setErr] = useState("");

  useEffect(() => {
    // `runsRev` moves with every processed alarm (and again from the storm's own tick), so the
    // fetch trails the burst by a moment and a storm costs one rollup per alarm, not several.
    let cancelled = false;
    const t = window.setTimeout(() => {
      api
        .productivity(windowHours)
        .then((d) => {
          if (cancelled) return;
          setP(d);
          setErr("");
        })
        .catch((e) => !cancelled && setErr(String(e?.message || e)));
    }, 600);
    return () => {
      cancelled = true;
      window.clearTimeout(t);
    };
  }, [windowHours, runsRev]);

  useEffect(() => {
    api.agents().then((a) => setAgents(Array.isArray(a) ? a : [])).catch(() => undefined);
  }, []);

  const toilRows = useMemo(
    () =>
      (p?.steps?.by_node || []).map((n: any) => ({
        key: n.node,
        label: n.label,
        value: Number(n.minutes_saved || 0),
        note: n.steps ? `${n.toil_minutes_each} min × ${n.succeeded + n.waiting_hitl}` : "no steps yet",
        title: `${n.label}: ${n.minutes_saved} min saved (${n.toil_minutes_each} min by hand, ${n.succeeded + n.waiting_hitl} done)`,
      })),
    [p]
  );

  const mcp = useMemo(() => {
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
    return { cards, servers: servers.size, writeGated: writeGated.size };
  }, [agents]);

  const autonomy = String(profile?.autonomy_level || p?.autonomy_level || "L2_GUARDED");
  const hours = p?.toil?.hours_saved;
  const alarms = p?.alarms;

  return (
    <div className="showcase">
      <section className="sc-hero">
        <div className="sc-hero-text">
          <h1>One alarm in. Ticket, owner, broadcast, brief and ledger out.</h1>
          <p className="sc-lead">
            Twelve agents sit on the NOC platform {profile?.display_name ? `${profile.display_name.replace(" (demo profile)", "")} ` : ""}
            already runs. They do the typing, the matrix lookups and the chasing. People keep every decision that leaves
            the building.
          </p>
          <div className="sc-hero-actions">
            <button className="btn primary" onClick={() => nav("/")}>
              Watch it live on Mission Control
            </button>
            <span className="sc-window" role="group" aria-label="Numbers window">
              <button className={"btn" + (windowHours === 24 ? " good" : "")} onClick={() => setWindowHours(24)} aria-pressed={windowHours === 24}>
                Last 24 h
              </button>
              <button className={"btn" + (windowHours === 0 ? " good" : "")} onClick={() => setWindowHours(0)} aria-pressed={windowHours === 0}>
                All time
              </button>
            </span>
          </div>
        </div>
        <div className="sc-numbers" aria-live="polite">
          <div className="sc-number">
            <span className="sc-number-value">{hours == null ? "—" : hours < 1 ? fmtMinutes(p?.toil?.minutes_saved) : `${hours < 10 ? hours.toFixed(1) : Math.round(hours)} h`}</span>
            <span className="sc-number-label">of floor toil taken over, by the operator's own estimate</span>
          </div>
          <div className="sc-number">
            <span className="sc-number-value">
              {fmtInt(alarms?.processed)} → {fmtInt(alarms?.incidents_created)}
            </span>
            <span className="sc-number-label">
              alarms into tickets{alarms?.noise_reduction_pct != null ? `; ${alarms.noise_reduction_pct}% absorbed as duplicates or cascades` : ""}
            </span>
          </div>
          <div className="sc-number">
            <span className="sc-number-value">{fmtInt(p?.hitl?.raised)}</span>
            <span className="sc-number-label">
              decisions asked of a person{p?.hitl?.pending ? `, ${p.hitl.pending} waiting now` : ""}; nothing external sent without one
            </span>
          </div>
        </div>
        {err && <div className="hitl-error">Numbers unavailable: {err}</div>}
      </section>

      <LiveRunPanel events={events} runsRev={runsRev} onOpen={(id) => nav(`/incidents/${id}`)} title="The newest alarm, hop by hop" compact={false} />

      <section className="sc-section">
        <h2>What changed for the floor</h2>
        <p className="sc-lead">
          The twelve steps a NOC analyst does for every service-affecting alarm, and who does them now. Minutes are the
          operator profile's estimate of the manual work, not a stopwatch.
        </p>
        <ol className="sc-steps">
          {(p?.steps?.by_node || []).map((n: any) => {
            const story = HOP_STORY[n.node] || { does: "", instead: "" };
            const human = n.node === "HITL";
            return (
              <li key={n.node} className={"sc-step" + (human ? " human" : "")}>
                <div className="sc-step-head">
                  <strong>{n.label}</strong>
                  <span className="muted">{agentDisplayName(n.agent)}</span>
                  <span className="sc-step-min">{human ? "stays human" : `${n.toil_minutes_each} min by hand`}</span>
                </div>
                <div className="sc-step-body">
                  <p>
                    <span className="sc-k">Now</span> {story.does}
                  </p>
                  <p className="muted">
                    <span className="sc-k">Before</span> {story.instead}
                  </p>
                </div>
                <div className="sc-step-stat muted">
                  {n.steps ? `${n.steps} runs · avg ${fmtMs(n.avg_ms)}${n.failed ? ` · ${n.failed} failed` : ""}` : "not run yet"}
                </div>
              </li>
            );
          })}
        </ol>
      </section>

      <section className="sc-section">
        <h2>How it sits on what you already run</h2>
        <p className="sc-lead">
          Nothing is replaced. Alarms still come from the NMS, tickets still live in the ticket system, the ledger is still
          Excel and the broadcasts still leave through the same mail and SMS gateways. The agents read and write through
          adapters; today's adapters are mocks, the interfaces are the real ones.
        </p>
        <Architecture autonomy={autonomy} agentsCount={agents.length || 12} mcp={mcp} />
      </section>

      <section className="sc-section sc-two">
        <div>
          <h2>People keep the decisions</h2>
          <p className="sc-lead">The autonomy level is one setting. This deployment runs at {autonomy.replace("_", " ")}.</p>
          <ol className="sc-ladder">
            {LADDER.map((l) => (
              <li key={l.level} className={"sc-rung" + (l.level === autonomy ? " current" : "")} aria-current={l.level === autonomy ? "true" : undefined}>
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
            <li>Approving an agent's write into another system: {mcp.writeGated || "every"} gated write type{mcp.writeGated === 1 ? "" : "s"}, each behind a named approval.</li>
          </ul>
          <p className="muted">
            {fmtInt(metrics?.hitl_pending ?? p?.hitl?.pending)} approval{(metrics?.hitl_pending ?? p?.hitl?.pending) === 1 ? "" : "s"} waiting in the inbox right now
            {p?.hitl?.median_decision_minutes != null ? `; a decision takes ${p.hitl.median_decision_minutes} min on median` : ""}.
          </p>
        </div>
      </section>

      <section className="sc-section">
        <h2>Where the minutes go</h2>
        <p className="sc-lead">
          {fmtInt(p?.steps?.total)} agent steps {windowHours ? "in the last 24 hours" : "on record"}, {fmtInt(p?.ticket_fields?.auto_filled)} ticket
          fields filled ({p?.ticket_fields?.per_incident ?? "—"} per ticket), {fmtInt(p?.broadcasts?.drafted)} broadcasts drafted,{" "}
          {fmtInt(p?.records?.exec_briefs)} executive briefs and {fmtInt(p?.records?.ledger_rows)} ledger rows written. A full run takes{" "}
          {fmtMs(p?.pipeline_ms?.median)} on median.
        </p>
        <ToilBars rows={toilRows} unit="minutes" caption="Minutes of manual work taken over, by step" />
        <p className="muted sc-footnote">
          {p?.toil?.assumptions?.note} Edit <code>productivity.toil_minutes</code> in the operator profile to use your floor's
          own numbers; the page recalculates. Decided approvals are charged back at {p?.toil?.assumptions?.human_minutes?.hitl_decision ?? 2} min
          each: {fmtMinutes(p?.toil?.human_minutes_spent)} so far, net {fmtMinutes(p?.toil?.net_minutes_saved)} saved.
        </p>
      </section>

      <section className="sc-section">
        <h2>Each agent, by the numbers</h2>
        <div className="panel table-scroll">
          <table className="sc-table">
            <thead>
              <tr>
                <th scope="col">Agent</th>
                <th scope="col">Does</th>
                <th scope="col">Hops</th>
                <th scope="col" style={{ textAlign: "right" }}>
                  Steps
                </th>
                <th scope="col" style={{ textAlign: "right" }}>
                  Failed
                </th>
                <th scope="col" style={{ textAlign: "right" }}>
                  Avg
                </th>
              </tr>
            </thead>
            <tbody>
              {(p?.agents || []).map((a: any) => (
                <tr key={a.name}>
                  <td>
                    <strong>{agentDisplayName(a.name)}</strong>
                  </td>
                  <td className="muted">{a.mission}</td>
                  <td className="muted">{a.nodes?.length ? a.nodes.join(", ") : "on request"}</td>
                  <td style={{ textAlign: "right", fontFamily: "var(--mono)" }}>{fmtInt(a.steps)}</td>
                  <td style={{ textAlign: "right", fontFamily: "var(--mono)", color: a.failed ? "#ffb4c0" : undefined }}>{fmtInt(a.failed)}</td>
                  <td style={{ textAlign: "right", fontFamily: "var(--mono)" }}>{fmtMs(a.avg_ms)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="sc-section sc-two">
        <div>
          <h2>Adding the next agent</h2>
          <ol className="sc-plain">
            <li>Write one module that exposes <code>run(state, ctx)</code> and <code>input_summary(state, ctx)</code>.</li>
            <li>Add one card to the registry: its node, its agent profile, whether a failure stops the run.</li>
            <li>Write the tests from the acceptance rows. The rail, the audit trail and this page pick it up unchanged.</li>
          </ol>
          <p className="muted">
            {mcp.cards} external tool connections are already declared across {mcp.servers} systems (monitoring, CMDB,
            ticketing, on-call, chat, documents), each read-only or behind a named approval; none is switched on in this
            demo.
          </p>
        </div>
        <div>
          <h2>Try it yourself</h2>
          <p className="sc-lead">Everything on this page is read from the running system. The storm is repeatable.</p>
          <div className="sc-hero-actions">
            <button className="btn primary" onClick={() => nav("/")}>
              Mission Control
            </button>
            <button className="btn" onClick={() => nav("/hitl")}>
              HITL inbox
            </button>
            <button className="btn" onClick={() => nav("/agents")}>
              Agent observatory
            </button>
          </div>
          <div style={{ marginTop: "1rem" }}>
            <AgentRail steps={null} nodes={(p?.steps?.by_node || []).map((n: any) => ({ id: n.node, label: n.label, agent: n.agent, status: "succeeded" }))} compact caption="The twelve hops" />
          </div>
        </div>
      </section>
    </div>
  );
}

/** The platform diagram: existing systems on the left, agents in the middle, people on the right. */
function Architecture({ autonomy, agentsCount, mcp }: { autonomy: string; agentsCount: number; mcp: { cards: number; servers: number } }) {
  const left = ["NMS and EMS alarm feeds", "Ticketing system", "Site catalogue and CMDB", "Mail and SMS gateways", "Excel shift ledger"];
  const right = ["HITL inbox: claim, read, approve", "Wallboard and Mission Control", "Executive brief readers", "RNIO, FE and MSP recipients"];
  return (
    <figure className="sc-arch">
      <svg viewBox="0 0 980 360" role="img" aria-labelledby="arch-title arch-desc">
        <title id="arch-title">How the agents sit on the existing platform</title>
        <desc id="arch-desc">
          Existing systems on the left connect through adapters to a layer of {agentsCount} agents under one supervisor, which
          hands decisions to people on the right.
        </desc>
        <defs>
          <marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M0 0 L10 5 L0 10 z" fill="var(--muted-dim)" />
          </marker>
        </defs>
        {/* left column */}
        <text x="20" y="28" className="sc-arch-h">
          What you already run
        </text>
        {left.map((t, i) => (
          <g key={t} transform={`translate(20, ${48 + i * 58})`}>
            <rect width="250" height="42" rx="9" className="sc-arch-box" />
            <text x="14" y="26" className="sc-arch-t">
              {t}
            </text>
            <line x1="250" y1="21" x2="318" y2="21" className="sc-arch-line" markerEnd="url(#arr)" markerStart="url(#arr)" />
          </g>
        ))}
        {/* adapters */}
        <g transform="translate(320, 48)">
          <rect width="96" height="274" rx="10" className="sc-arch-adapt" />
          <text x="48" y="140" className="sc-arch-t" textAnchor="middle" transform="rotate(-90 48 140)">
            adapters: mock today, real later
          </text>
        </g>
        {/* agents */}
        <g transform="translate(440, 48)">
          <rect width="280" height="274" rx="12" className="sc-arch-agents" />
          <text x="140" y="34" className="sc-arch-h" textAnchor="middle">
            {agentsCount} agents, one supervisor
          </text>
          <text x="140" y="60" className="sc-arch-s" textAnchor="middle">
            ingest, correlate, enrich, severity, ticket,
          </text>
          <text x="140" y="78" className="sc-arch-s" textAnchor="middle">
            assign, broadcast, brief, ledger, recurrence,
          </text>
          <text x="140" y="96" className="sc-arch-s" textAnchor="middle">
            monitor, handover
          </text>
          <rect x="24" y="118" width="232" height="40" rx="8" className="sc-arch-gate" />
          <text x="140" y="143" className="sc-arch-t" textAnchor="middle">
            autonomy {autonomy.replace("_", " ")}
          </text>
          <text x="140" y="188" className="sc-arch-s" textAnchor="middle">
            every step records its reason, its tools
          </text>
          <text x="140" y="206" className="sc-arch-s" textAnchor="middle">
            and its timing in the audit trail
          </text>
          <text x="140" y="240" className="sc-arch-s" textAnchor="middle">
            {mcp.cards} tool connections declared across {mcp.servers} systems,
          </text>
          <text x="140" y="258" className="sc-arch-s" textAnchor="middle">
            read-only or behind a named approval
          </text>
        </g>
        <line x1="416" y1="185" x2="438" y2="185" className="sc-arch-line" markerEnd="url(#arr)" markerStart="url(#arr)" />
        {/* right column */}
        <text x="760" y="28" className="sc-arch-h">
          People
        </text>
        {right.map((t, i) => (
          <g key={t} transform={`translate(760, ${48 + i * 70})`}>
            <line x1="-40" y1="21" x2="-2" y2="21" className="sc-arch-line" markerEnd="url(#arr)" />
            <rect width="200" height="42" rx="9" className={"sc-arch-box" + (i === 0 ? " human" : "")} />
            <text x="12" y="26" className="sc-arch-t">
              {t}
            </text>
          </g>
        ))}
      </svg>
      <figcaption className="muted">
        Arrows are reads and writes through adapter interfaces. A write into another system is never made by a model; the
        orchestrator makes it after a person approves the matching card.
      </figcaption>
    </figure>
  );
}
