import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import ApprovalCard, { type CardAction, type CardReceipt } from "../components/ApprovalCard";
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
import { statusOf } from "../lib/apiError";
import { hitlSubject } from "../lib/hitlSubject";
import { IconDot } from "../lib/icons";
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
 * STATES: skeleton cards until the first answer; "Couldn't load" + Retry (the
 * shared `.empty[role=alert]`) when the first load (or a refetch of an empty
 * queue) fails; a one-line warning + Retry above the last good list when a
 * refetch fails; an empty state that says nothing is waiting for a decision.
 *
 * A DECISION LANDS WHERE THE EYE IS. The decided card stays in place for 1.2 s with
 * "Approved by NOC Analyst" and what that did in its footer (where the buttons were, and
 * focused), then folds away; focus moves to the next card's Claim (its heading when it is
 * already claimed, the page heading when the queue is empty). There is no separate
 * confirmation line elsewhere on the page; screen readers also hear the outcome from a
 * visually hidden status line.
 *
 * NO GHOSTS. Every list request is numbered: an answer older than the one on screen is
 * dropped, and a successful decision retires every answer already in flight (they were asked
 * before it). The ids decided here are kept, so no late answer can bring a decided card back.
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
/** How long a decided card shows its receipt before it folds away. */
const RECEIPT_MS = 1200;
/** The fold itself (HitlInbox.css `.hitl-slot`); skipped under quiet mode and reduced motion. */
const FOLD_MS = 200;

type LoadError = { text: string; detail: string };
/** A card decided on this page, kept on screen for its receipt, then folded. */
type Decided = { task: any; receipt: CardReceipt; index: number; folding: boolean };

const reducedMotion = () =>
  document.documentElement.getAttribute("data-quiet") === "on" ||
  (typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches);

const taskId = (t: any, i: number) => (typeof t?.id === "string" && t.id ? t.id : `row-${i}`);

export default function HitlInbox({ session, tick }: { session: any; tick: number }) {
  const [tasks, setTasks] = useState<any[]>([]);
  const [incidents, setIncidents] = useState<Record<string, any>>({});
  // What screen readers hear after a claim or a decision; never drawn.
  const [announce, setAnnounce] = useState("");
  // False until the first answer (rows or an error), so the list can show skeleton cards
  // instead of a misleading "nothing waiting".
  const [loaded, setLoaded] = useState(false);
  const [loadError, setLoadError] = useState<LoadError | null>(null);
  const [busy, setBusy] = useState<Record<string, CardAction>>({});
  const [errors, setErrors] = useState<Record<string, { text: string; detail: string }>>({});
  const [decided, setDecided] = useState<Record<string, Decided>>({});

  // Request numbering: `asked` is the newest request sent, `shown` the newest whose answer
  // may be applied. An answer numbered below `shown` is stale and dropped.
  const asked = useRef(0);
  const shown = useRef(0);
  // Every id decided on this page, for the page's life: a late answer never revives one.
  const decidedIds = useRef<Set<string>>(new Set());
  const headingRef = useRef<HTMLHeadingElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const timers = useRef<number[]>([]);
  useEffect(() => () => timers.current.forEach((t) => window.clearTimeout(t)), []);

  const rt = useRealtimeState();
  const connected = rt?.connected ?? false;

  /**
   * The queue and the incident facts in one round. `/api/v1/hitl/pending` returns only
   * `incident_number`, `priority` and `site_id`; est. users, M-PESA risk, services and the owner
   * live on the incident. Both answers are applied together, before the list first draws, so
   * the facts never arrive later and push the drafts down. A failed incidents call keeps the
   * facts already known (the cards render with what the task rows carry).
   */
  const load = useCallback(() => {
    const mine = ++asked.current;
    Promise.allSettled([api.hitl(), api.incidents()]).then(([queue, facts]) => {
      if (mine < shown.current) return; // an older answer than the one on screen
      if (facts.status === "fulfilled" && Array.isArray(facts.value)) {
        const map: Record<string, any> = {};
        for (const r of facts.value) if (r && typeof r.id === "string") map[r.id] = r;
        setIncidents(map);
      }
      if (queue.status === "fulfilled") {
        shown.current = mine;
        // Only ever *replace* the queue with something we actually received, minus anything
        // decided here. A failed or malformed refetch leaves the last good list on screen.
        const rows = queue.value;
        const list = Array.isArray(rows) ? rows.filter(isPlainObject) : [];
        setTasks(list.filter((t) => !decidedIds.current.has(String(t.id))));
        setLoadError(null);
      } else {
        const e = queue.reason;
        setLoadError({ text: friendlyLoadError(e), detail: e instanceof Error ? e.message : String(e) });
      }
      setLoaded(true);
    });
  }, []);

  const reload = load;

  useEffect(load, [tick, load]);

  // Degraded mode: no socket, no revisions, so poll. Costs nothing while the
  // socket is up because the effect only arms when `connected` is false.
  useEffect(() => {
    if (connected) return;
    const id = window.setInterval(load, DISCONNECTED_POLL_MS);
    return () => window.clearInterval(id);
  }, [connected, load]);

  const who = session?.display_name || "Supervisor";

  /** Focus for the card now at `index` in the list: its Claim, else its heading; the page
   *  heading when the queue is empty. Never <body>. */
  const focusCardAt = useCallback((index: number) => {
    const slots = listRef.current ? Array.from(listRef.current.querySelectorAll<HTMLElement>(".hitl-slot:not(.folding)")) : [];
    const slot = slots[Math.min(index, slots.length - 1)];
    const target = slot?.querySelector<HTMLElement>(".hitl-claim") || slot?.querySelector<HTMLElement>(".hitl-card h2");
    (target || headingRef.current)?.focus();
  }, []);

  // The decided cards as of the last render, for the timers below (a state updater may run
  // after them, so they read the index from here).
  const decidedNow = useRef(decided);
  decidedNow.current = decided;

  /** Show the receipt, fold the card, hand focus on. */
  const retire = useCallback(
    (id: string) => {
      const quick = reducedMotion();
      const fold = () => {
        setDecided((d) => (d[id] ? { ...d, [id]: { ...d[id], folding: true } } : d));
        timers.current.push(window.setTimeout(drop, FOLD_MS));
      };
      const drop = () => {
        const at = decidedNow.current[id]?.index ?? 0;
        setDecided((d) => {
          if (!d[id]) return d;
          const next = { ...d };
          delete next[id];
          return next;
        });
        // Once React has removed the slot, the card now at that position takes focus, unless
        // the presenter has already moved on to something else (focus is no longer on <body>).
        window.requestAnimationFrame(() =>
          window.requestAnimationFrame(() => {
            const active = document.activeElement;
            if (active && active !== document.body) return;
            focusCardAt(at);
          })
        );
      };
      timers.current.push(window.setTimeout(quick ? drop : fold, RECEIPT_MS));
    },
    [focusCardAt]
  );

  const act = useCallback(
    async (task: any, index: number, action: CardAction, reason = "") => {
      const id = taskId(task, index);
      const subject = hitlSubject(task);
      const broadcast = task?.task_type === "APPROVE_BROADCAST";
      setBusy((b) => ({ ...b, [id]: action }));
      setErrors((prev) => {
        if (!prev[id]) return prev;
        const next = { ...prev };
        delete next[id];
        return next;
      });
      try {
        if (action === "claim") await api.claim(id, who);
        else if (action === "approve") await api.approve(id, who, reason);
        else await api.reject(id, who, reason);
        // Every list answer already on its way was asked before this decision: retire them.
        shown.current = asked.current + 1;
        if (action === "claim") {
          setTasks((ts) => ts.map((t) => (t?.id === id ? { ...t, claimed_by: who, status: "CLAIMED" } : t)));
          setAnnounce(`Claimed ${subject}. Write a reason, then approve or reject.`);
        } else {
          const verb = action === "approve" ? "Approved" : "Rejected";
          const effect =
            action === "approve"
              ? broadcast
                ? "The held broadcast is released to the dispatcher."
                : "The change goes ahead."
              : broadcast
                ? "The drafts are suppressed; nothing is sent."
                : "Nothing goes ahead.";
          decidedIds.current.add(id);
          const receipt = { headline: `${verb} by ${who}`, effect, approved: action === "approve" };
          setDecided((d) => ({ ...d, [id]: { task, receipt, index, folding: false } }));
          setTasks((ts) => ts.filter((t) => t?.id !== id));
          setAnnounce(`${verb} ${subject}. ${effect}`);
          retire(id);
        }
      } catch (e) {
        const code = statusOf(e);
        if (action !== "claim" && (code === 409 || code === 404)) {
          // Somebody else decided it first: the card says so where the buttons were, then
          // folds like any decided card, so focus is handed on instead of falling to <body>.
          decidedIds.current.add(id);
          const receipt = { headline: "Already decided by someone else", effect: friendlyError(e), approved: false };
          setDecided((d) => ({ ...d, [id]: { task, receipt, index, folding: false } }));
          setTasks((ts) => ts.filter((t) => t?.id !== id));
          setAnnounce(`${subject} was already decided by someone else.`);
          retire(id);
          return;
        }
        const text = action === "claim" && code === 409 ? "Somebody else claimed this card first." : friendlyError(e);
        setErrors((prev) => ({ ...prev, [id]: { text, detail: e instanceof Error ? e.message : String(e) } }));
        // The pressed button stays; after a failed claim the card's heading takes focus so the
        // error under it is read in context.
        if (action === "claim") {
          window.requestAnimationFrame(() =>
            listRef.current?.querySelector<HTMLElement>(`[data-card-id="${CSS.escape(id)}"] .hitl-card h2`)?.focus()
          );
        }
      } finally {
        setBusy((b) => {
          if (!b[id]) return b;
          const next = { ...b };
          delete next[id];
          return next;
        });
        reload(); // whatever happened, the truth is on the server
      }
    },
    [reload, retire, who]
  );

  /** The list as drawn: the queue, with each card decided here back in its place until it folds. */
  const display = useMemo(() => {
    const out: { id: string; task: any; decided: Decided | null }[] = tasks.map((t, i) => ({
      id: taskId(t, i),
      task: t,
      decided: null,
    }));
    const kept = Object.entries(decided).sort((a, b) => a[1].index - b[1].index);
    for (const [id, d] of kept) out.splice(Math.min(d.index, out.length), 0, { id, task: d.task, decided: d });
    return out;
  }, [tasks, decided]);

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

  const showList = loaded && display.length > 0;

  return (
    <div>
      <div className="page-head">
        <div>
          <h1 ref={headingRef} tabIndex={-1}>
            Approvals
          </h1>
          <p className="lead">Claim a card, read what leaves, then approve or reject with a reason.</p>
        </div>
        <div className="page-actions">
          <button className="btn" onClick={reload}>
            Refresh
          </button>
        </div>
      </div>

      {/* Plain facts, 16 px apart, no chips. */}
      <div className="hitl-summary">
        {loaded && tasks.length > 0 && (
          <>
            <span>{tasks.length} waiting</span>
            {summary.urgent > 0 && <span>{summary.urgent} P1/P2</span>}
            <span>{summary.unclaimed > 0 ? `${summary.unclaimed} unclaimed` : "all claimed"}</span>
            {summary.oldest != null && <span>oldest {fmtWait(summary.oldest)}</span>}
          </>
        )}
        {!connected && (
          <span className="attn warn" title="No WebSocket. The queue is being polled instead.">
            <IconDot />
            Live updates are down; checking every {DISCONNECTED_POLL_MS / 1000}&nbsp;s
          </span>
        )}
      </div>
      {/* Always mounted, so each claim and decision is spoken; the receipt on the card is what
          a sighted presenter reads. */}
      <span className="hitl-sr" role="status">
        {announce}
      </span>

      {loadError && display.length > 0 && (
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
            <div key={n} className="hitl-slot">
              <div className="hitl-card hitl-skel" aria-hidden="true">
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
            </div>
          ))}
        </div>
      )}

      {loaded && display.length === 0 && loadError && (
        <div className="panel">
          <div className="empty" role="alert" title={loadError.detail}>
            Couldn't load the approvals queue. {loadError.text}
            <button className="btn sm" onClick={reload}>
              Retry
            </button>
          </div>
        </div>
      )}

      {loaded && display.length === 0 && !loadError && (
        <div className="panel">
          <div className="empty">
            Nothing is waiting for a decision. Cards arrive here when an agent holds a broadcast or a change for
            a person. <Link to="/">Watch Mission control</Link>
          </div>
        </div>
      )}

      {showList && (
        <div className="hitl-list" ref={listRef}>
          {display.map(({ id, task: t, decided: d }, i) => {
            const err = errors[id];
            return (
              <div key={id} className={"hitl-slot" + (d?.folding ? " folding" : "")} data-card-id={id}>
                <CardBoundary
                  fallback={
                    <article className="hitl-card">
                      <header className="hitl-card-head">
                        <span className="chip danger">card failed to render</span>
                        <h2 className="hitl-inc" tabIndex={-1}>
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
                          aria-disabled={busy[id] || d ? true : undefined}
                          onClick={() => {
                            if (!busy[id] && !d) act(t, i, "reject", "card render failure — rejected unread");
                          }}
                        >
                          {busy[id] === "reject" ? "Rejecting…" : "Reject unread"}
                        </button>
                      </div>
                    </article>
                  }
                >
                  <ApprovalCard
                    task={t}
                    incident={t?.incident_id ? incidents[t.incident_id] : null}
                    who={who}
                    busy={busy[id] ?? null}
                    error={err?.text || ""}
                    errorDetail={err?.detail}
                    receipt={d?.receipt ?? null}
                    onClaim={() => act(t, i, "claim")}
                    onApprove={(reason) => act(t, i, "approve", reason)}
                    onReject={(reason) => act(t, i, "reject", reason)}
                  />
                </CardBoundary>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
