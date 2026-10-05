import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Search, X } from "lucide-react";
import LaneOff from "../components/LaneOff";
import CaseList from "../components/support/CaseList";
import CasePane from "../components/support/CasePane";
import Evals from "../components/support/Evals";
import KnowledgeBase from "../components/support/KnowledgeBase";
import Outages from "../components/support/Outages";
import { useNarrow } from "../lib/layout";
import {
  CATEGORIES,
  CATEGORY_WORD,
  ROUTES,
  ROUTE_WORD,
  STATUSES,
  STATUS_WORD,
  errorDetail,
  fmtMs,
  gateFor,
  isLaneOff,
  isNetworkError,
  pct,
  statusOfError,
  supportApi,
  withPerson,
  type CaseDetail,
  type Complaint,
  type EvalReport,
  type KbArticle,
  type Metrics,
  type Queue,
} from "../lib/support";
import "./SupportDesk.css";

/**
 * The Support desk at /support (docs/SUPPORT_DESK.md). Operate mode: the figures that matter,
 * then five views on one tab row. The queue is master-detail: the cases on the left, the open
 * case on the right with the customer's words, the verdict, the agent trace and, where a person
 * decides, the decision. "Needs a person" is the same pane over the cases waiting for one. The
 * knowledge base is what the resolver answers from. Evals is the desk scored against its gates.
 * Outages (docs/CLOSE_THE_LOOP.md) is the desk keeping its promise: who waits to hear that an
 * outage is fixed, who was told, who says it is still down, and the outages customers spotted
 * first. The queue takes `?incident=<id>` to show only the complaints linked to one ticket.
 *
 * Refresh: `tick` is `revisions.support` from the WS renderer table (support.created,
 * support.escalated, support.updated) plus App's nudge every 8 s while the stream is down.
 * A 404 on the queue is the lane switched off (`SUPPORT_DESK_ENABLED=false`), not an error.
 */

type Tab = "queue" | "person" | "outages" | "kb" | "evals";
const TABS: { key: Tab; label: string }[] = [
  { key: "queue", label: "Queue" },
  { key: "person", label: "Needs a person" },
  { key: "outages", label: "Outages" },
  { key: "kb", label: "Knowledge base" },
  { key: "evals", label: "Evals" },
];
const isTab = (v: unknown): v is Tab => TABS.some((t) => t.key === v);

type LoadState = "loading" | "ok" | "missing" | "error";

/** The list and the case stack under this width; above it they sit side by side. */
const STACK_QUERY = "(max-width: 999px)";

function loadErrorText(e: unknown): string {
  if (isNetworkError(e)) return "The API is unreachable.";
  return errorDetail(e, "The request failed.");
}

/** What a failed eval run says: the last report stays on screen either way. */
function runErrorText(e: unknown): string {
  const s = statusOfError(e);
  if (s != null && s >= 500) return "The eval run failed on the server; the last report below is unchanged. Try again.";
  if (isNetworkError(e)) return "The API is unreachable; the last report below is unchanged. Try again.";
  return `The eval run did not start: ${errorDetail(e, "the API refused it")}. The last report below is unchanged.`;
}

export default function SupportDesk({ session, tick = 0, profile }: { session: any; tick?: number; profile?: any }) {
  const [params, setParams] = useSearchParams();
  const tab: Tab = isTab(params.get("tab")) ? (params.get("tab") as Tab) : "queue";
  const caseParam = params.get("case");
  const incidentParam = params.get("incident");
  const stacked = useNarrow(STACK_QUERY);
  const who = String(session?.display_name || "NOC Analyst");

  const [queue, setQueue] = useState<Queue | null>(null);
  const [queueState, setQueueState] = useState<LoadState>("loading");
  const [queueError, setQueueError] = useState<string | null>(null);
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [evals, setEvals] = useState<EvalReport | null>(null);
  const [evalsState, setEvalsState] = useState<LoadState>("loading");
  const [evalsError, setEvalsError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);
  const [kb, setKb] = useState<KbArticle[]>([]);
  const [kbState, setKbState] = useState<"loading" | "ok" | "error">("loading");
  const [kbError, setKbError] = useState<string | null>(null);
  const [seeding, setSeeding] = useState(false);
  const [notice, setNotice] = useState("");
  const [filters, setFilters] = useState({ q: "", status: "", route: "", category: "" });
  const [tabsMore, setTabsMore] = useState(false);
  const asked = useRef(0);
  const tabRefs = useRef<Record<string, HTMLButtonElement | null>>({});
  const tabsRef = useRef<HTMLDivElement>(null);

  const loadQueue = useCallback(() => {
    const mine = ++asked.current;
    Promise.allSettled([supportApi.queue({ limit: 200 }), supportApi.metrics(0)]).then(([q, m]) => {
      if (mine !== asked.current) return;
      if (q.status === "fulfilled") {
        setQueue(q.value);
        setQueueState("ok");
        setQueueError(null);
      } else if (isLaneOff(q.reason)) {
        setQueueState("missing");
      } else {
        setQueueError(loadErrorText(q.reason));
        setQueueState((s) => (s === "ok" ? s : "error"));
      }
      if (m.status === "fulfilled") setMetrics(m.value);
    });
  }, []);

  const loadEvals = useCallback(() => {
    supportApi
      .evalsLatest()
      .then((r) => {
        setEvals(r);
        setEvalsState("ok");
        setEvalsError(null);
      })
      .catch((e) => {
        if (isLaneOff(e)) {
          setEvals(null);
          setEvalsState("missing");
        } else {
          setEvalsError(loadErrorText(e));
          setEvalsState((s) => (s === "ok" ? s : "error"));
        }
      });
  }, []);

  const loadKb = useCallback(() => {
    setKbState((s) => (s === "ok" ? s : "loading"));
    supportApi
      .kb()
      .then((r) => {
        setKb(Array.isArray(r?.articles) ? r.articles : []);
        setKbState("ok");
        setKbError(null);
      })
      .catch((e) => {
        setKbError(loadErrorText(e));
        setKbState("error");
      });
  }, []);

  // A tick later, so React's development double mount asks nothing twice.
  useEffect(() => {
    const t = window.setTimeout(() => {
      loadQueue();
      loadEvals();
      loadKb();
    }, 0);
    return () => window.clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => {
    if (tick > 0) loadQueue();
  }, [tick, loadQueue]);

  // The tab row scrolls sideways on a phone; a fade at its edge says there is more.
  useEffect(() => {
    const el = tabsRef.current;
    if (!el) return;
    const check = () => setTabsMore(el.scrollWidth - el.clientWidth - el.scrollLeft > 4);
    check();
    el.addEventListener("scroll", check, { passive: true });
    window.addEventListener("resize", check);
    return () => {
      el.removeEventListener("scroll", check);
      window.removeEventListener("resize", check);
    };
  }, []);

  const items = queue?.items ?? [];
  const titles = useMemo(() => Object.fromEntries(kb.map((a) => [a.id, a.title])), [kb]);

  const visible = useMemo(() => {
    if (tab === "person") {
      return items
        .filter((c) => withPerson(c.status))
        .sort((a, b) => String(a.escalation?.at ?? a.created_at).localeCompare(String(b.escalation?.at ?? b.created_at)));
    }
    const q = filters.q.trim().toLowerCase();
    return items.filter((c) => {
      if (incidentParam && c.linked_incident?.id !== incidentParam) return false;
      if (filters.status && c.status !== filters.status) return false;
      if (filters.route && c.route !== filters.route) return false;
      if (filters.category && c.category !== filters.category) return false;
      if (q) {
        const hay = [c.ref, c.subject, c.body, c.customer.name ?? "", c.customer.msisdn_masked, c.customer.account_ref ?? ""].join(" ").toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
  }, [items, tab, filters, incidentParam]);

  const personCount = useMemo(() => items.filter((c) => withPerson(c.status)).length, [items]);
  // The open case: the URL's, else the first visible one on a wide screen (never on a phone, where
  // the pane would hide the list).
  const selectedId = caseParam ?? (!stacked && visible.length ? visible[0].id : null);

  const setTab = (t: Tab) => {
    const next = new URLSearchParams(params);
    if (t === "queue") next.delete("tab");
    else next.set("tab", t);
    setParams(next, { replace: true });
  };
  // The ticket the queue is narrowed to, by its number when a linked case names it.
  const incidentNumber = incidentParam
    ? items.find((c) => c.linked_incident?.id === incidentParam)?.linked_incident?.incident_number ?? null
    : null;
  const clearIncident = () => {
    const next = new URLSearchParams(params);
    next.delete("incident");
    next.delete("case");
    setParams(next, { replace: true });
  };
  const selectCase = (id: string | null) => {
    const next = new URLSearchParams(params);
    if (id) next.set("case", id);
    else next.delete("case");
    setParams(next, { replace: true });
  };

  const onTabKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const i = TABS.findIndex((t) => t.key === tab);
    let n = -1;
    if (e.key === "ArrowRight") n = (i + 1) % TABS.length;
    else if (e.key === "ArrowLeft") n = (i - 1 + TABS.length) % TABS.length;
    else if (e.key === "Home") n = 0;
    else if (e.key === "End") n = TABS.length - 1;
    if (n < 0) return;
    e.preventDefault();
    setTab(TABS[n].key);
    tabRefs.current[TABS[n].key]?.focus();
  };

  const onChanged = useCallback((d: CaseDetail) => {
    setQueue((q) => (q ? { ...q, items: q.items.map((c) => (c.id === d.complaint.id ? d.complaint : c)) } : q));
    supportApi
      .metrics(0)
      .then(setMetrics)
      .catch(() => undefined);
  }, []);

  const seed = () => {
    setSeeding(true);
    setNotice("");
    supportApi
      .seed()
      .then((r) => {
        setNotice(`Loaded ${r.created} sample ${r.created === 1 ? "complaint" : "complaints"}.`);
        loadQueue();
      })
      .catch((e) => setNotice(`Couldn't load the samples. ${loadErrorText(e)}`))
      .finally(() => setSeeding(false));
  };

  const runEvals = () => {
    setRunning(true);
    setRunError(null);
    supportApi
      .evalsRun()
      .then((r) => {
        setEvals(r);
        setEvalsState("ok");
        setNotice(r.passed ? `Evals ran: all ${r.gates.length} gates pass.` : `Evals ran: ${r.gates.filter((g) => !g.passed).length} of ${r.gates.length} gates fail.`);
      })
      .catch((e) => setRunError(runErrorText(e)))
      .finally(() => setRunning(false));
  };

  if (queueState === "missing") {
    return (
      <div>
        <PageHead />
        <LaneOff title="The Support desk is off in this demo" flag="SUPPORT_DESK_ENABLED">
          take complaints from customers, route them through the triage, resolver and action agents, and score the desk on its golden set
        </LaneOff>
      </div>
    );
  }

  const resolvedCount = (metrics?.auto_resolved ?? 0) + (metrics?.action_completed ?? 0);
  const personNow = metrics ? metrics.escalated + metrics.awaiting_approval : null;
  const wrongGate = evals ? gateFor(evals, "wrong_escalation_rate") : null;
  const wrongHas = !!wrongGate && typeof wrongGate.value === "number";
  const emptyQueue = queueState === "ok" && items.length === 0;

  return (
    <div className="sd">
      <PageHead />
      <span className="sr-only" role="status">
        {notice}
      </span>

      <div className="kpis sd-kpis" aria-label="The desk today">
        <div className="kpi">
          <div className="label">Resolved by agents</div>
          <div className={"value" + (metrics ? (resolvedCount === 0 ? " zero" : "") : " zero")}>{metrics ? pct(metrics.resolution_rate) : "—"}</div>
          <div className="sd-kpi-sub">{metrics ? `${resolvedCount} of ${metrics.total} ${metrics.total === 1 ? "case" : "cases"}` : "loading"}</div>
        </div>
        <div className="kpi">
          <div className="label">With a person</div>
          <div className={"value" + (personNow == null || personNow === 0 ? " zero" : " hitl")}>{personNow ?? "—"}</div>
          <div className="sd-kpi-sub">{metrics ? (metrics.awaiting_approval ? `${metrics.awaiting_approval} awaiting approval` : "none awaiting approval") : "loading"}</div>
        </div>
        <div className="kpi">
          <div className="label">Wrong escalations</div>
          <div className={"value" + (wrongHas ? (wrongGate!.passed ? (wrongGate!.value === 0 ? " zero" : "") : " p1") : " zero")}>{wrongHas ? pct(wrongGate!.value) : "—"}</div>
          <div className="sd-kpi-sub">
            {wrongHas ? (
              <>
                {wrongGate!.passed ? "passes" : "fails"} the {pct(wrongGate!.threshold)} gate,{" "}
                <button type="button" className="sd-kpi-link" onClick={() => setTab("evals")}>
                  see the evals
                </button>
              </>
            ) : wrongGate ? (
              wrongGate.note || "no evidence in the latest eval"
            ) : evalsState === "missing" ? (
              <button type="button" className="sd-kpi-link" onClick={() => setTab("evals")}>
                no eval run yet
              </button>
            ) : (
              "from the latest eval"
            )}
          </div>
        </div>
        <div className="kpi">
          <div className="label">Median handling time</div>
          <div className={"value" + (metrics && metrics.total ? "" : " zero")}>{metrics && metrics.total ? fmtMs(metrics.median_handle_ms) : "—"}</div>
          <div className="sd-kpi-sub">from intake to the reply</div>
        </div>
      </div>
      {/* Phones: the same four figures as one line, so the queue starts on the first screen. */}
      <p className="sd-kpis-line" aria-label="The desk today">
        {metrics ? (
          <>
            <span>
              Agents resolved <span className="mono">{resolvedCount}</span> of <span className="mono">{metrics.total}</span>
            </span>
            <span className={personNow ? "hitl" : undefined}>
              <span className="mono">{personNow ?? 0}</span> with a person
            </span>
            <span>
              wrong escalations <span className="mono">{wrongHas ? pct(wrongGate!.value) : "—"}</span>
              {wrongHas && (
                <>
                  ,{" "}
                  <button type="button" className="sd-kpi-link" onClick={() => setTab("evals")}>
                    evals
                  </button>
                </>
              )}
            </span>
            <span>
              median <span className="mono">{metrics.total ? fmtMs(metrics.median_handle_ms) : "—"}</span>
            </span>
          </>
        ) : (
          <span>Loading the desk's figures</span>
        )}
      </p>

      <div className="sd-tabs" role="tablist" aria-label="Support desk views" onKeyDown={onTabKey} ref={tabsRef} data-more={tabsMore ? "1" : undefined}>
        {TABS.map((t) => {
          const on = t.key === tab;
          const count = t.key === "person" ? personCount : null;
          return (
            <button
              key={t.key}
              ref={(el) => {
                tabRefs.current[t.key] = el;
              }}
              type="button"
              role="tab"
              id={`sd-tab-${t.key}`}
              aria-selected={on}
              aria-controls={`sd-panel-${t.key}`}
              tabIndex={on ? 0 : -1}
              className="sd-tab"
              onClick={() => setTab(t.key)}
            >
              {t.label}
              {count != null && count > 0 && <span className="sd-tab-count hitl">{count}</span>}
            </button>
          );
        })}
      </div>

      <div role="tabpanel" id={`sd-panel-${tab}`} aria-labelledby={`sd-tab-${tab}`} className="sd-panel">
        {(tab === "queue" || tab === "person") && (
          <QueueView
            tab={tab}
            stacked={stacked}
            state={queueState}
            error={queueError}
            onRetry={loadQueue}
            all={items}
            visible={visible}
            filters={filters}
            onFilters={setFilters}
            selectedId={selectedId}
            onSelect={selectCase}
            who={who}
            tick={tick}
            titles={titles}
            onChanged={onChanged}
            seeding={seeding}
            onSeed={seed}
            emptyQueue={emptyQueue}
            incident={incidentParam ? { id: incidentParam, number: incidentNumber } : null}
            onClearIncident={clearIncident}
          />
        )}
        {tab === "outages" && <Outages tick={tick} profile={profile} />}
        {tab === "kb" && <KnowledgeBase stacked={stacked} articles={kb} state={kbState} error={kbError} onRetry={loadKb} />}
        {tab === "evals" && <Evals report={evals} state={evalsState} error={evalsError} running={running} onRun={runEvals} onRetry={loadEvals} runError={runError} />}
      </div>
    </div>
  );
}

function PageHead() {
  return (
    <div className="page-head">
      <div>
        <h1>Support desk</h1>
        <p className="lead">Customer complaints, read and routed by agents; the hard ones come to a person, with the reason.</p>
      </div>
      <div className="page-actions">
        <Link to="/complain" className="btn">
          Open the complaint form
        </Link>
      </div>
    </div>
  );
}

interface QueueViewProps {
  tab: Tab;
  stacked: boolean;
  state: LoadState;
  error: string | null;
  onRetry: () => void;
  all: Complaint[];
  visible: Complaint[];
  filters: { q: string; status: string; route: string; category: string };
  onFilters: (f: { q: string; status: string; route: string; category: string }) => void;
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  who: string;
  tick: number;
  titles: Record<string, string>;
  onChanged: (d: CaseDetail) => void;
  seeding: boolean;
  onSeed: () => void;
  emptyQueue: boolean;
  /** `?incident=`: only the complaints linked to this ticket. */
  incident: { id: string; number: string | null } | null;
  onClearIncident: () => void;
}

function QueueView(p: QueueViewProps) {
  const person = p.tab === "person";
  const hasCase = !!p.selectedId;
  const showList = !p.stacked || !hasCase;
  const filtered = p.filters.q || p.filters.status || p.filters.route || p.filters.category || p.incident;
  const caseCol = useRef<HTMLDivElement>(null);
  // A case a person picked scrolls to its top; the one the page opened on its own does not move
  // the view. "Back" returns focus to the row that was open.
  const picked = useRef(false);
  const backTo = useRef<string | null>(null);
  const [scrollKey, setScrollKey] = useState(0);

  useEffect(() => {
    if (p.selectedId && picked.current) {
      picked.current = false;
      caseCol.current?.scrollIntoView({ block: "start" });
      setScrollKey((k) => k + 1);
    }
    if (!p.selectedId && backTo.current) {
      const id = backTo.current;
      backTo.current = null;
      window.requestAnimationFrame(() => {
        document.querySelector<HTMLElement>(`.sd-row[data-id="${CSS.escape(id)}"]`)?.focus({ preventScroll: true });
      });
    }
  }, [p.selectedId]);

  const pick = (id: string) => {
    picked.current = true;
    p.onSelect(id);
  };
  const back = () => {
    backTo.current = p.selectedId;
    p.onSelect(null);
  };

  if (p.state === "loading" && !p.all.length) {
    return (
      <div className="sd-split" aria-busy="true">
        <div className="panel sd-list-col">
          <span className="sr-only" role="status">
            Loading the queue.
          </span>
          <div className="skeleton-rows" aria-hidden="true">
            {["62%", "48%", "70%", "44%", "66%", "52%", "58%"].map((w, i) => (
              <span key={i} className="skeleton" style={{ width: w }} />
            ))}
          </div>
        </div>
        {!p.stacked && (
          <div className="panel sd-case" aria-hidden="true">
            <div className="skeleton-rows">
              <span className="skeleton" style={{ width: "28%" }} />
              <span className="skeleton" style={{ width: "92%", height: 20 }} />
              <span className="skeleton" style={{ width: "70%", height: 20 }} />
              <span className="skeleton" style={{ width: "48%" }} />
              <span className="skeleton" style={{ width: "84%" }} />
            </div>
          </div>
        )}
      </div>
    );
  }

  if (p.state === "error" && !p.all.length) {
    return (
      <div className="panel">
        <div className="empty" role="alert">
          Couldn't load the queue. {p.error}
          <button type="button" className="btn sm" onClick={p.onRetry}>
            Retry
          </button>
        </div>
      </div>
    );
  }

  if (p.emptyQueue) {
    return (
      <div className="panel">
        <div className="empty sd-empty">
          <p>No complaints on the desk yet. Load a dozen sample complaints, across every route, to watch the agents work them; or send one yourself from the public form.</p>
          <p className="sd-empty-actions">
            <button type="button" className="btn primary" onClick={() => !p.seeding && p.onSeed()} aria-disabled={p.seeding ? true : undefined}>
              {p.seeding ? "Loading samples…" : "Load sample complaints"}
            </button>
            <Link to="/complain" className="btn">
              Open the complaint form
            </Link>
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className={"sd-split" + (hasCase && p.stacked ? " has-case" : "")}>
      {showList && (
        <div className="panel sd-list-col">
          {p.state === "error" && p.error && (
            <div className="sd-stale" role="alert">
              <span>Couldn't refresh; showing the last list received. {p.error}</span>
              <button type="button" className="btn sm" onClick={p.onRetry}>
                Retry
              </button>
            </div>
          )}
          {person ? (
            p.visible.length > 0 && (
              <div className="sd-filters sd-filters-note">
                <span className="muted">
                  {p.visible.length} {p.visible.length === 1 ? "case" : "cases"} with a person, oldest first.
                </span>
              </div>
            )
          ) : (
            <div className="sd-filters" role="search">
              {p.incident && (
                <div className="sd-scope">
                  <span>
                    Linked to <span className="mono">{p.incident.number ?? "this ticket"}</span>
                  </span>
                  <button type="button" className="btn ghost sm sd-scope-clear" onClick={p.onClearIncident}>
                    <X size={14} strokeWidth={1.75} aria-hidden="true" />
                    <span>Show every complaint</span>
                  </button>
                </div>
              )}
              <label className="sd-search">
                <Search size={16} strokeWidth={1.75} aria-hidden="true" />
                <span className="sr-only">Search complaints</span>
                <input type="search" value={p.filters.q} onChange={(e) => p.onFilters({ ...p.filters, q: e.target.value })} placeholder="Search ref, words, name" autoComplete="off" />
              </label>
              <label className="sr-only" htmlFor="sd-f-status">
                Status
              </label>
              <select id="sd-f-status" value={p.filters.status} onChange={(e) => p.onFilters({ ...p.filters, status: e.target.value })}>
                <option value="">Status</option>
                {STATUSES.map((s) => (
                  <option key={s} value={s}>
                    {STATUS_WORD[s]}
                  </option>
                ))}
              </select>
              <label className="sr-only" htmlFor="sd-f-route">
                Route
              </label>
              <select id="sd-f-route" value={p.filters.route} onChange={(e) => p.onFilters({ ...p.filters, route: e.target.value })}>
                <option value="">Route</option>
                {ROUTES.map((r) => (
                  <option key={r} value={r}>
                    {ROUTE_WORD[r]}
                  </option>
                ))}
              </select>
              <label className="sr-only" htmlFor="sd-f-cat">
                Category
              </label>
              <select id="sd-f-cat" value={p.filters.category} onChange={(e) => p.onFilters({ ...p.filters, category: e.target.value })}>
                <option value="">Category</option>
                {CATEGORIES.map((c) => (
                  <option key={c} value={c}>
                    {CATEGORY_WORD[c]}
                  </option>
                ))}
              </select>
            </div>
          )}
          {p.visible.length === 0 ? (
            <div className="empty">
              {person ? (
                <>Nobody is waiting for a person; escalations and held tool calls appear here, oldest first.</>
              ) : filtered ? (
                <>
                  No case matches these filters.{" "}
                  <button
                    type="button"
                    className="link sd-clear"
                    onClick={() => {
                      p.onFilters({ q: "", status: "", route: "", category: "" });
                      if (p.incident) p.onClearIncident();
                    }}
                  >
                    Clear them
                  </button>
                </>
              ) : (
                <>No complaints yet.</>
              )}
            </div>
          ) : (
            <CaseList items={p.visible} selectedId={p.selectedId} onSelect={pick} person={person} ariaLabel={person ? "Cases waiting for a person" : "Complaints"} />
          )}
          <div className="sd-list-foot">{person ? "" : `${p.visible.length} of ${p.all.length} ${p.all.length === 1 ? "case" : "cases"}`}</div>
        </div>
      )}
      {hasCase && p.selectedId && (
        <div className="sd-case-col" ref={caseCol}>
          <CasePane id={p.selectedId} who={p.who} tick={p.tick} stacked={p.stacked} onBack={back} onChanged={p.onChanged} titles={p.titles} scrollKey={scrollKey} />
        </div>
      )}
      {!p.stacked && !hasCase && (
        <div className="panel sd-case">
          <div className="empty">Pick a case to read it here.</div>
        </div>
      )}
    </div>
  );
}
