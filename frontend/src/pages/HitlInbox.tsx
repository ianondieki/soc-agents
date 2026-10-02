import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import ApprovalCard from "../components/ApprovalCard";
import CardBoundary from "../components/CardBoundary";
import {
  ageMinutes,
  friendlyError,
  friendlyLoadError,
  fmtWait,
  isPlainObject,
  labelFor,
  rawPayload,
} from "../lib/hitl";
import { hitlSubject } from "../lib/hitlSubject";
import { IconCheck, IconDot } from "../lib/icons";
import { useRealtimeState } from "../realtime/RealtimeContext";
import "./HitlInbox.css";

/**
 * The shared HITL approval queue.
 *
 * Before: one flat panel per task showing `proposed_payload.sms` (or a raw
 * `JSON.stringify` dump for anything else), with Claim / Approve / Reject and a
 * `window.prompt` for the reject reason. The email — the rendering that reaches
 * MSP leads, the RNIO and the exec list — was never displayed, so it was
 * approved unread.
 *
 * After: one `ApprovalCard` per task showing every rendering side by side with
 * the incident facts that justify the decision (§6.5), and an inline reason that
 * lands on the audit row.
 *
 * STATES: skeleton cards until the first answer; "Couldn't load" + Retry when the
 * first load (or a refetch of an empty queue) fails; a one-line warning + Retry
 * above the last good list when a refetch fails; an empty state that says
 * nothing is waiting for a decision.
 *
 * REFRESH: the page is driven by `tick` (`revisions.hitl` from the WS renderer
 * table, so a burst of `agent.step.*` frames costs nothing here and a
 * `hitl.*` frame costs one refetch). If the socket drops, `tick` stops moving
 * and the queue would freeze — `App.refresh()` only refetches profile, metrics
 * and session. So while `connected` is false this page polls every 15 s and says
 * so. HITL is `critical: true` in the renderer table, so quiet mode does not
 * suppress it.
 */

const DISCONNECTED_POLL_MS = 15000;

type LoadError = { text: string; detail: string };

export default function HitlInbox({ session, tick }: { session: any; tick: number }) {
  const [tasks, setTasks] = useState<any[]>([]);
  const [incidents, setIncidents] = useState<Record<string, any>>({});
  const [msg, setMsg] = useState("");
  // False until the first answer (rows or an error), so the list can show skeleton cards
  // instead of a misleading "nothing waiting".
  const [loaded, setLoaded] = useState(false);
  const [loadError, setLoadError] = useState<LoadError | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [errors, setErrors] = useState<Record<string, { text: string; detail: string }>>({});

  // Incident ids we have already asked the board for. Without this the "fetch
  // the facts for an incident I don't have" effect re-runs forever when an
  // incident is genuinely absent from `/api/v1/incidents`.
  const factsAsked = useRef<Set<string>>(new Set());

  const rt = useRealtimeState();
  const connected = rt?.connected ?? false;

  const load = useCallback(() => {
    api
      .hitl()
      .then((rows) => {
        // Only ever *replace* the queue with something we actually received.
        // A failed or malformed refetch leaves the last good list on screen.
        setTasks(Array.isArray(rows) ? rows.filter(isPlainObject) : []);
        setLoadError(null);
      })
      .catch((e) =>
        setLoadError({ text: friendlyLoadError(e), detail: e instanceof Error ? e.message : String(e) })
      )
      .finally(() => setLoaded(true));
  }, []);

  const reload = useCallback(() => {
    factsAsked.current.clear();
    load();
  }, [load]);

  useEffect(reload, [tick, reload]);

  // Degraded mode: no socket, no revisions, so poll. Costs nothing while the
  // socket is up because the effect only arms when `connected` is false.
  useEffect(() => {
    if (connected) return;
    const id = window.setInterval(reload, DISCONNECTED_POLL_MS);
    return () => window.clearInterval(id);
  }, [connected, reload]);

  /**
   * Incident facts. `/api/v1/hitl/pending` returns only `incident_number`,
   * `priority` and `site_id`; est. users, M-PESA risk, services and the owner
   * live on the incident. One list call covers every card, and a failure is
   * swallowed — the cards render with whatever the task rows carry.
   */
  useEffect(() => {
    const need = tasks
      .map((t) => t?.incident_id)
      .filter(
        (id): id is string =>
          typeof id === "string" && Boolean(id) && !incidents[id] && !factsAsked.current.has(id)
      );
    if (need.length === 0) return;
    for (const id of need) factsAsked.current.add(id);
    let cancelled = false;
    api
      .incidents()
      .then((rows) => {
        if (cancelled || !Array.isArray(rows)) return;
        const map: Record<string, any> = {};
        for (const r of rows) if (r && typeof r.id === "string") map[r.id] = r;
        setIncidents(map);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [tasks, incidents]);

  const who = session?.display_name || "Supervisor";

  const act = useCallback(
    async (id: string, run: () => Promise<any>, okMsg: string) => {
      setBusyId(id);
      setErrors((prev) => {
        if (!prev[id]) return prev;
        const next = { ...prev };
        delete next[id];
        return next;
      });
      try {
        await run();
        setMsg(okMsg);
      } catch (e) {
        setMsg("");
        setErrors((prev) => ({
          ...prev,
          [id]: {
            text: friendlyError(e),
            detail: e instanceof Error ? e.message : String(e),
          },
        }));
      } finally {
        setBusyId(null);
        reload(); // whatever happened, the truth is on the server
      }
    },
    [reload]
  );

  const summary = useMemo(() => {
    let urgent = 0;
    let unclaimed = 0;
    let oldest: number | null = null;
    for (const t of tasks) {
      if (t?.priority === "P1" || t?.priority === "P2") urgent += 1;
      if (!t?.claimed_by) unclaimed += 1;
      const m = ageMinutes(t?.created_at);
      if (m != null && (oldest == null || m > oldest)) oldest = m;
    }
    return { urgent, unclaimed, oldest };
  }, [tasks]);

  const showList = loaded && tasks.length > 0;

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Approvals</h1>
          <p className="lead">Claim a card, read what leaves, then approve or reject with a reason.</p>
        </div>
        <div className="page-actions">
          <button className="btn" onClick={reload} disabled={busyId !== null}>
            Refresh
          </button>
        </div>
      </div>

      {/* Plain facts, 16 px apart, no chips. The status span is always mounted so a screen
          reader hears each decision's outcome. */}
      <div className="hitl-summary">
        {showList && (
          <>
            <span>{tasks.length} waiting</span>
            {summary.urgent > 0 && <span>{summary.urgent} P1/P2</span>}
            <span>{summary.unclaimed > 0 ? `${summary.unclaimed} unclaimed` : "all claimed"}</span>
            {summary.oldest != null && <span>oldest {fmtWait(summary.oldest)}</span>}
          </>
        )}
        {!connected && (
          <span className="hitl-attn" title="No WebSocket. The queue is being polled instead.">
            <IconDot className="hitl-dot warn" />
            Live updates are down; checking every {DISCONNECTED_POLL_MS / 1000}&nbsp;s
          </span>
        )}
        <span className="hitl-done" role="status">
          {msg && (
            <>
              <IconCheck className="hitl-ok" />
              {msg}
            </>
          )}
        </span>
      </div>

      {loadError && tasks.length > 0 && (
        <div className="hitl-error hitl-stale" role="alert" title={loadError.detail}>
          <span>Couldn't refresh the queue; showing the last list received. {loadError.text}</span>
          <button className="btn sm" onClick={reload}>
            Retry
          </button>
        </div>
      )}

      {!loaded && (
        <div className="hitl-list" aria-busy="true">
          <span className="hitl-sr" role="status">
            Loading the approvals queue.
          </span>
          {[0, 1].map((n) => (
            <div key={n} className="hitl-card hitl-skel" aria-hidden="true">
              <div className="skeleton w30" />
              <div className="skeleton w55" />
              <div className="hitl-skel-cols">
                <div>
                  <div className="skeleton" />
                  <div className="skeleton" />
                  <div className="skeleton w70" />
                </div>
                <div>
                  <div className="skeleton" />
                  <div className="skeleton w70" />
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {loaded && tasks.length === 0 && loadError && (
        <div className="panel hitl-state" title={loadError.detail}>
          <p className="hitl-state-title">Couldn't load the approvals queue.</p>
          <p>{loadError.text}</p>
          <button className="btn" onClick={reload}>
            Retry
          </button>
        </div>
      )}

      {loaded && tasks.length === 0 && !loadError && (
        <div className="panel hitl-state">
          <p className="hitl-state-title">Nothing is waiting for a decision.</p>
          <p>
            Cards arrive here when an agent holds a broadcast or a change for a person.{" "}
            <Link to="/">Watch Mission control</Link>
          </p>
        </div>
      )}

      {showList && (
        <div className="hitl-list">
          {tasks.map((t, i) => {
            const id = typeof t?.id === "string" && t.id ? t.id : `row-${i}`;
            const err = errors[id];
            const subject = hitlSubject(t);
            const broadcast = t?.task_type === "APPROVE_BROADCAST";
            return (
              <CardBoundary
                key={id}
                fallback={
                  <article className="hitl-card">
                    <header className="hitl-card-head">
                      <span className="chip danger">card failed to render</span>
                      <h2 className="hitl-inc">
                        {typeof t?.incident_number === "string" && t.incident_number ? t.incident_number : id}
                      </h2>
                      <span className="hitl-type">{labelFor(t?.task_type)}</span>
                    </header>
                    <p className="hitl-effect">
                      This task could not be drawn. The raw payload is below; decide from it, or open the
                      incident.
                    </p>
                    <pre className="pre hitl-field-pre" tabIndex={0}>
                      {rawPayload(t?.proposed_payload)}
                    </pre>
                    <div className="hitl-buttons hitl-fallback-actions">
                      <button
                        className="btn danger"
                        disabled={busyId === id}
                        onClick={() =>
                          act(
                            id,
                            () => api.reject(id, who, "card render failure — rejected unread"),
                            "Rejected"
                          )
                        }
                      >
                        Reject unread
                      </button>
                    </div>
                  </article>
                }
              >
                <ApprovalCard
                  task={t}
                  incident={t?.incident_id ? incidents[t.incident_id] : null}
                  who={who}
                  busy={busyId === id}
                  error={err?.text || ""}
                  errorDetail={err?.detail}
                  onClaim={() => act(id, () => api.claim(id, who), `Claimed ${subject}`)}
                  onApprove={(reason) =>
                    act(
                      id,
                      () => api.approve(id, who, reason),
                      broadcast
                        ? `Approved ${subject}; the held broadcast is released to the dispatcher`
                        : `Approved ${subject}`
                    )
                  }
                  onReject={(reason) =>
                    act(
                      id,
                      () => api.reject(id, who, reason),
                      broadcast ? `Rejected ${subject}; the drafts are suppressed` : `Rejected ${subject}`
                    )
                  }
                />
              </CardBoundary>
            );
          })}
        </div>
      )}
    </div>
  );
}
