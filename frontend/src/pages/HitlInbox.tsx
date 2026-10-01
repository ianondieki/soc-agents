import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import ApprovalCard from "../components/ApprovalCard";
import CardBoundary from "../components/CardBoundary";
import { ageMinutes, fmtAge, friendlyError, isPlainObject, rawPayload } from "../lib/hitl";
import { useRealtimeState } from "../realtime/RealtimeContext";

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
 * REFRESH: the page is driven by `tick` (`revisions.hitl` from the WS renderer
 * table, so a burst of `agent.step.*` frames costs nothing here and a
 * `hitl.*` frame costs one refetch). If the socket drops, `tick` stops moving
 * and the queue would freeze — `App.refresh()` only refetches profile, metrics
 * and session. So while `connected` is false this page polls every 15 s and says
 * so. HITL is `critical: true` in the renderer table, so quiet mode does not
 * suppress it.
 */

const DISCONNECTED_POLL_MS = 15000;

export default function HitlInbox({ session, tick }: { session: any; tick: number }) {
  const [tasks, setTasks] = useState<any[]>([]);
  const [incidents, setIncidents] = useState<Record<string, any>>({});
  const [msg, setMsg] = useState("");
  const [loadError, setLoadError] = useState("");
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
        setLoadError("");
      })
      .catch((e) => setLoadError(friendlyError(e)));
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

  return (
    <div>
      <h1 className="page-title">HITL Inbox</h1>
      <p className="muted">
        Shared team queue — claim before approve to avoid double action. Every rendering below is
        what actually leaves; read both columns before deciding.
      </p>

      <div className="chips" style={{ marginBottom: "0.85rem" }}>
        <span className="chip hitl">{tasks.length} pending</span>
        {summary.urgent > 0 && <span className="chip danger">{summary.urgent} P1/P2</span>}
        {summary.unclaimed > 0 && <span className="chip">{summary.unclaimed} unclaimed</span>}
        {summary.oldest != null && (
          <span className={"chip " + (summary.oldest >= 30 ? "danger" : summary.oldest >= 5 ? "warn" : "")}>
            oldest {fmtAge(summary.oldest)}
          </span>
        )}
        {!connected && (
          <span className="chip warn" title="No WebSocket. The queue is being polled instead.">
            WS down · polling every {DISCONNECTED_POLL_MS / 1000}s
          </span>
        )}
        <button className="btn" onClick={reload} disabled={busyId !== null}>
          Refresh
        </button>
      </div>

      {msg && <p className="chip ok">{msg}</p>}
      {loadError && (
        <div className="hitl-error" style={{ marginBottom: "0.75rem" }}>
          Could not refresh the queue: {loadError}
          {tasks.length > 0 && " — showing the last list received."}
        </div>
      )}

      <div className="hitl-list">
        {tasks.length === 0 && !loadError && <div className="empty">No pending HITL tasks.</div>}
        {tasks.map((t, i) => {
          const id = typeof t?.id === "string" && t.id ? t.id : `row-${i}`;
          const err = errors[id];
          return (
            <CardBoundary
              key={id}
              fallback={
                <article className="hitl-card">
                  <header className="hitl-card-head">
                    <span className="chip bad">card failed to render</span>
                    <strong className="hitl-inc">{t?.incident_number || id}</strong>
                    <span className="chip">{String(t?.task_type ?? "unknown type")}</span>
                  </header>
                  <p className="muted">
                    This task could not be drawn. The raw payload is below — decide from it, or open
                    the incident.
                  </p>
                  <pre className="pre hitl-field-pre">{rawPayload(t?.proposed_payload)}</pre>
                  <div className="hitl-buttons">
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
                onClaim={() => act(id, () => api.claim(id, who), `Claimed by ${who}`)}
                onApprove={(reason) =>
                  act(
                    id,
                    () => api.approve(id, who, reason),
                    "Approved — held broadcasts released to the dispatcher"
                  )
                }
                onReject={(reason) => act(id, () => api.reject(id, who, reason), "Rejected — drafts suppressed")}
              />
            </CardBoundary>
          );
        })}
      </div>
    </div>
  );
}
