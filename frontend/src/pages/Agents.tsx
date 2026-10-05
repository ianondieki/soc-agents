import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { RunState } from "../components/LiveRunPanel";
import { agentDisplayName, alarmSite, fmtInt, fmtMs, humanEnum, humanGraph, nodeLabel, runOutcome, runOutcomeOf, runStatusWord } from "../lib/agents";
import { fibreColour, fibreOf } from "../lib/fibre";
import { fmtTime } from "../lib/time";
import { RunError } from "../components/RunError";
import "./Agents.css";

// The failed-run line lives in components/RunError.tsx (Mission control imports it without
// this page's chunk); re-exported here so older imports still work.
export { RunError } from "../components/RunError";

/** The data classes an agent declares, as the floor says them. */
const DATA_WORDS: Record<string, string> = {
  network: "network data",
  counts: "counts",
  timestamps: "timestamps",
  msisdn: "phone numbers",
  customer: "customer records",
  cdr: "call records",
  location_trace: "location traces",
  mpesa: "M‑PESA data",
  raw_names: "people's names",
};
const dataWord = (k: unknown) => DATA_WORDS[String(k)] || humanEnum(String(k));

/** The agent's fibre marks: one per step it runs, in the ribbon's colours (lib/fibre.ts). */
function FibreMarks({ nodes }: { nodes: string[] }) {
  const fibres = nodes.map(fibreOf).filter(Boolean) as NonNullable<ReturnType<typeof fibreOf>>[];
  if (!fibres.length) return <span className="ag-fibre none" aria-hidden="true" />;
  return (
    <span className="ag-fibres" aria-hidden="true">
      {fibres.map((f) => (
        <span key={f.n} className={"ag-fibre" + (f.outlined ? " outlined" : "")} style={{ background: fibreColour(f) }} />
      ))}
    </span>
  );
}

/** How a run was started, when it was not the ordinary alarm event ("Alarm run" needs no "event"). */
const TRIGGER_WORDS: Record<string, string> = { SCHEDULE: "scheduled", REQUEST: "on request" };

function Skeleton({ rows }: { rows: number }) {
  return (
    <div role="status">
      <span className="sr-only">Loading</span>
      <div className="skeleton-rows" aria-hidden="true">
        {Array.from({ length: rows }, (_, i) => (
          <span key={i} className="skeleton" />
        ))}
      </div>
    </div>
  );
}

export default function Agents({ tick = 0 }: { tick?: number }) {
  // Both lists are null until their first answer, so loading never reads as "nothing here".
  const [agents, setAgents] = useState<any[] | null>(null);
  const [agentsFailed, setAgentsFailed] = useState(false);
  const [agentsRetry, setAgentsRetry] = useState(0);
  const [runs, setRuns] = useState<any[] | null>(null);
  const [runsFailed, setRunsFailed] = useState(false);
  const [retry, setRetry] = useState(0);
  const [stats, setStats] = useState<Record<string, any>>({});
  const [totals, setTotals] = useState<any>(null);

  useEffect(() => {
    let live = true;
    setAgentsFailed(false);
    api
      .agents()
      .then((r) => {
        if (live) setAgents(Array.isArray(r) ? r : []);
      })
      .catch(() => {
        if (live) setAgentsFailed(true);
      });
    return () => {
      live = false;
    };
  }, [agentsRetry]);

  // Per-agent throughput from the productivity rollup (everything on record): steps,
  // failures, timings. Advisory for the roster; a failure here leaves the rows static.
  // Debounced on the runs revision so a storm costs one rollup per alarm, not per frame.
  useEffect(() => {
    let cancelled = false;
    const t = window.setTimeout(() => {
      api
        .productivity(0)
        .then((p) => {
          if (cancelled) return;
          const map: Record<string, any> = {};
          for (const a of p?.agents || []) if (a?.name) map[a.name] = a;
          setStats(map);
          setTotals(p ?? null);
        })
        .catch(() => undefined);
    }, 600);
    return () => {
      cancelled = true;
      window.clearTimeout(t);
    };
  }, [tick]);

  useEffect(() => {
    let live = true;
    const take = (r: any) => {
      if (!live) return;
      setRuns(Array.isArray(r) ? r : []);
      setRunsFailed(false);
    };
    api
      .runs()
      .then(take)
      .catch(() => {
        if (live) setRunsFailed(true);
      });
    const id = window.setInterval(() => {
      api.runs().then(take).catch(() => undefined);
    }, 4000);
    return () => {
      live = false;
      window.clearInterval(id);
    };
    // `tick` is the debounced `runs` slice revision: a run start/finish refreshes
    // this list at once, and the 4 s poll stays as the WS-down fallback.
  }, [tick, retry]);

  const steps = totals?.steps;
  return (
    <div className="ag">
      <div className="page-head">
        <div>
          <h1>Agent observatory</h1>
          <p
            className="lead"
            title="Each agent has a mission, a criticality and the tools it may call. The counts are everything on record; during a storm the run list moves through running, waiting for a decision and done as each alarm is processed."
          >
            The twelve agents: what each one does, how it has done, and what it is allowed to touch.
          </p>
        </div>
      </div>
      <dl className="ag-figures">
        <div>
          <dt>Agents</dt>
          <dd>{agents === null ? "—" : agents.length}</dd>
        </div>
        <div>
          <dt>Steps run</dt>
          <dd>{steps ? fmtInt(steps.total) : "—"}</dd>
        </div>
        <div className={steps?.failed ? "bad" : undefined}>
          <dt>Steps failed</dt>
          <dd>{steps ? fmtInt(steps.failed) : "—"}</dd>
        </div>
        <div className={steps?.waiting_hitl ? "hitl" : undefined}>
          <dt>Waiting for a person</dt>
          <dd>{steps ? fmtInt(steps.waiting_hitl) : "—"}</dd>
        </div>
        <div>
          <dt>Alarm to ticket, median</dt>
          <dd>{totals?.pipeline_ms?.median != null ? fmtMs(totals.pipeline_ms.median) : "—"}</dd>
        </div>
      </dl>
      <div className="ag-grid">
        <section aria-labelledby="ag-roster-title">
          <h2 id="ag-roster-title" className="sr-only">
            The agents
          </h2>
          <div className="ag-cards" aria-busy={agents === null || undefined}>
            {agents === null && agentsFailed && (
              <div className="panel empty" role="alert">
                Couldn't load the agent roster.{" "}
                <button className="btn sm" onClick={() => setAgentsRetry((n) => n + 1)}>
                  Retry
                </button>
              </div>
            )}
            {agents === null && !agentsFailed && (
              <div className="panel">
                <Skeleton rows={8} />
              </div>
            )}
            {agents !== null && agents.length === 0 && <div className="panel empty">No agents are registered.</div>}
            {/* In pipeline order (the fibre ribbon's), an agent that runs on request last. */}
            {(agents || [])
              .slice()
              .sort((x, y) => (fibreOf(x?.node_ids?.[0])?.n ?? 99) - (fibreOf(y?.node_ids?.[0])?.n ?? 99))
              .map((a) => {
                const s = stats[a.name];
                const failed = s?.failed || 0;
                const nodes: string[] = Array.isArray(a.node_ids) ? a.node_ids : [];
                const stepNames = nodes.map(nodeLabel);
                const first = fibreOf(nodes[0]);
                const last = fibreOf(nodes[nodes.length - 1]);
                const where = !first ? "Runs on request" : first.n === last?.n ? `Step ${first.n}` : `Steps ${first.n} and ${last?.n}`;
                const mcp: any[] = Array.isArray(a.mcp) ? a.mcp : [];
                const abroad = mcp.filter((m) => m?.residency === "abroad").length;
                const writes = mcp.filter((m) => Array.isArray(m?.write_tools) && m.write_tools.length).length;
                const tools: string[] = Array.isArray(a.tools) ? a.tools : [];
                const may: string[] = Array.isArray(a.data_may_see) ? a.data_may_see : [];
                const never: string[] = Array.isArray(a.data_must_not_see) ? a.data_must_not_see : [];
                return (
                  <article key={a.name} className="panel ag-card" aria-labelledby={`ag-${a.name}`}>
                    <header className="ag-card-head">
                      <FibreMarks nodes={nodes} />
                      <div className="ag-card-title">
                        <h3 id={`ag-${a.name}`}>{agentDisplayName(a.name)}</h3>
                        <p>
                          <span>{where}</span>
                          {stepNames.length > 0 && <span>{stepNames.join(", ")}</span>}
                        </p>
                      </div>
                    </header>
                    <p className="ag-mission">{a.mission}</p>
                    <dl className="ag-stats">
                      <div>
                        <dt>Steps</dt>
                        <dd>{fmtInt(s?.steps ?? 0)}</dd>
                      </div>
                      <div>
                        <dt>Average</dt>
                        <dd>{fmtMs(s?.avg_ms)}</dd>
                      </div>
                      <div className={failed ? "bad" : undefined}>
                        <dt>Failed</dt>
                        <dd>{fmtInt(failed)}</dd>
                      </div>
                      <div>
                        <dt>Last step</dt>
                        <dd>{s?.last_step_at ? fmtTime(s.last_step_at) : "—"}</dd>
                      </div>
                    </dl>
                    <p className="ag-tags">
                      <span className={a.criticality === "fail_closed" ? "ag-tag strict" : "ag-tag"}>
                        {a.criticality === "fail_closed" ? "An error stops the run" : "An error fails only its step"}
                      </span>
                      {mcp.length > 0 && (
                        <span className="ag-tag">
                          {mcp.length} connection{mcp.length === 1 ? "" : "s"}
                          {writes ? `, ${writes} can write with approval` : ", read only"}
                        </span>
                      )}
                      {abroad > 0 && <span className="ag-tag warn">{abroad} hosted abroad</span>}
                    </p>
                    {(tools.length > 0 || mcp.length > 0 || never.length > 0) && (
                      <details className="ag-more">
                        <summary>Tools and data</summary>
                        <div className="ag-more-body">
                          {tools.length > 0 && (
                            <div>
                              <h4>Its own tools</h4>
                              <p className="ag-chips">
                                {tools.map((t) => (
                                  <code key={t}>{t}</code>
                                ))}
                              </p>
                            </div>
                          )}
                          {mcp.length > 0 && (
                            <div>
                              <h4>Connections declared (none switched on in this demo)</h4>
                              <ul className="ag-conns">
                                {mcp.map((m) => (
                                  <li key={m.server}>
                                    <span className="ag-conn-name">{m.server}</span>
                                    <span className="ag-conn-facts">
                                      <span>{Array.isArray(m.write_tools) && m.write_tools.length ? "writes behind an approval" : "read only"}</span>
                                      {m.maturity && <span>{humanEnum(m.maturity)}</span>}
                                      <span className={m.residency === "abroad" ? "warn" : undefined}>
                                        {m.residency === "abroad" ? "hosted abroad" : "runs locally"}
                                      </span>
                                    </span>
                                    {m.purpose && <span className="ag-conn-why">{m.purpose}</span>}
                                  </li>
                                ))}
                              </ul>
                            </div>
                          )}
                          {(may.length > 0 || never.length > 0) && (
                            <div className="ag-data">
                              {may.length > 0 && (
                                <p>
                                  <strong>May see</strong> {may.map(dataWord).join(", ")}
                                </p>
                              )}
                              {never.length > 0 && (
                                <p>
                                  <strong>Never sees</strong> {never.map(dataWord).join(", ")}
                                </p>
                              )}
                            </div>
                          )}
                        </div>
                      </details>
                    )}
                  </article>
                );
              })}
          </div>
        </section>
        <section className="panel ag-runs" aria-labelledby="ag-runs-title">
          <div className="panel-head">
            <h2 id="ag-runs-title" className="panel-title">
              Live and recent runs
            </h2>
            <span className="muted">Times in EAT</span>
          </div>
          <div className="ag-runs-list" aria-busy={runs === null || undefined}>
            {runs === null && runsFailed && (
              <div className="empty" role="alert">
                Couldn't load the runs.{" "}
                <button className="btn sm" onClick={() => setRetry((n) => n + 1)}>
                  Retry
                </button>
              </div>
            )}
            {runs === null && !runsFailed && <Skeleton rows={6} />}
            {runs !== null && runs.length === 0 && (
              <div className="empty">
                No runs yet. Launch the storm from <Link to="/mission">Mission control</Link>.
              </div>
            )}
            {(runs || []).slice(0, 15).map((r) => {
              const trigger = TRIGGER_WORDS[String(r.trigger || "").toUpperCase()];
              // What the run did: "INC000004 opened", "folded into INC000004", else its name.
              const did = runOutcomeOf(r);
              const site = alarmSite(r.steps);
              const ticketLink = (label: string) =>
                r.incident_id ? (
                  <Link className="row-id" to={`/incidents/${r.incident_id}`}>
                    {label}
                  </Link>
                ) : (
                  <span className="row-id">{label}</span>
                );
              return (
                // Time first: every clock reads HH:MM:SS in mono, so the titles start at one x;
                // the state sits at the right, as on Mission control.
                <div key={r.id} className="row static">
                  <span className="muted dim mono">{fmtTime(r.started_at)}</span>
                  <div className="row-main">
                    <div className="row-title">
                      {did.kind === "opened" ? (
                        <span>{ticketLink(did.ticket || "Ticket")} opened</span>
                      ) : did.kind === "folded" ? (
                        <span>folded into {ticketLink(did.ticket || "an open ticket")}</span>
                      ) : (
                        <>
                          <span>{humanGraph(r.graph_name)}</span>
                          {site && <span className="mono">{site}</span>}
                          {trigger && <span className="muted">{trigger}</span>}
                        </>
                      )}
                    </div>
                    <div className="facts">
                      {String(r.status || "").toUpperCase() === "RUNNING" && r.current_node && <span>at {nodeLabel(r.current_node)}</span>}
                      <span>
                        <span className="mono">{r.steps?.length ?? 0}</span> steps
                      </span>
                    </div>
                    {/* A failed run keeps current_node at the node that broke; error_summary says why.
                        Without it the operator sees FAILED and has to open the DB to learn anything. */}
                    <RunError status={r.status} summary={r.error_summary} />
                  </div>
                  {/* A success is the normal outcome: a muted word. Waiting or failed: the state colour and icon. */}
                  {runOutcome(r.status) ? (
                    <RunState status={r.status} routine={false} />
                  ) : (
                    <span className="muted">{runStatusWord(r.status)}</span>
                  )}
                </div>
              );
            })}
          </div>
        </section>
      </div>
    </div>
  );
}
