import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import ApprovalCard, { ReceiptBody, type CardAction, type CardReceipt } from "../components/ApprovalCard";
import CardBoundary from "../components/CardBoundary";
import { priorityTitle } from "../lib/agents";
import {
  ageMinutes,
  decisionFromTimeline,
  elsewhereHeadline,
  fmtAge,
  friendlyError,
  friendlyLoadError,
  fmtWait,
  isPlainObject,
  labelFor,
  ladderBreached,
  queueSummary,
  rawPayload,
  sortQueue,
  verdictOfConflict,
  type ElsewhereDecision,
  type Verdict,
} from "../lib/hitl";
import { statusOf } from "../lib/apiError";
import { hitlSubject, hitlSubjectIsIncident } from "../lib/hitlSubject";
import { IconDot } from "../lib/icons";
import { fmtHM } from "../lib/time";
import { useRealtimeState } from "../realtime/RealtimeContext";
import type { NocEvent } from "../realtime/renderers";
import "./HitlInbox.css";

/**
 * The shared approval queue.
 *
 * THE URGENT CARD FIRST. Cards are ordered P1 first, then the longest wait, and only one is
 * open: every other card is one row (pill, ticket, type, site, wait). The first card is open
 * when the page loads and stays the open one when new cards arrive; a click, or Enter, on a row
 * opens that card and folds the one that was open, so the sticky decision bar exists once. With
 * forty cards waiting the queue is about two screens instead of forty card heights, and the four
 * P1s are at the top instead of 2,700 to 16,000 px down. A reason typed on a card survives its
 * card being folded to a row and opened again.
 *
 * STATES: skeleton rows until the first answer; "Couldn't load" + Retry (the shared
 * `.empty[role=alert]`) when the first load (or a refetch of an empty queue) fails; a one-line
 * warning + Retry above the last good list when a refetch fails; an empty state that says
 * nothing is waiting for a decision.
 *
 * A DECISION LANDS WHERE THE EYE IS. The decided card stays in place for 1.2 s with
 * "Approved by NOC Analyst" and what that did in its footer (where the buttons were, and
 * focused), then folds away; the next card in the queue opens and its Claim takes focus (its
 * heading when it is already claimed, the page heading when the queue is empty). Screen readers
 * hear the outcome from a visually hidden status line.
 *
 * A DECISION TAKEN ELSEWHERE (another tab, another person): the card leaves the next list
 * answer, or a claim or a decision here answers 409/404. Nothing vanishes: the card is replaced
 * in place by the same receipt, worded for it ("Approved by Ian in another session, 15:24"), who
 * and when read from the live `hitl.*` frames this tab saw, else from the ticket's timeline. A
 * reason typed here is kept under it with a Copy button. The receipt takes focus when its card
 * was the open one, is announced, and stays 4 s, longer while the pointer rests on it or its Copy
 * button has focus; then it folds like any decided card.
 *
 * NO GHOSTS. Every list request is numbered: an answer older than the one on screen is dropped,
 * and a successful decision retires every answer already in flight (they were asked before it).
 * The ids decided here or elsewhere are kept, so no late answer can bring a decided card back.
 *
 * REFRESH: the page is driven by `tick` (`revisions.hitl` from the WS renderer table plus App's
 * nudge every 8 s while the socket is down). HITL is `critical: true` in the renderer table, so
 * quiet mode does not suppress it.
 */

const DISCONNECTED_POLL_MS = 8000; // App's nudge while the stream is down
/** How long a card decided here shows its receipt before it folds away. */
const RECEIPT_MS = 1200;
/** How long a card decided in another session shows its receipt: long enough to read and copy. */
const ELSEWHERE_MS = 4000;
/** While the pointer or a focused button holds a receipt from elsewhere, check again after this. */
const HOLD_RECHECK_MS = 1000;
/** The longest a resting pointer keeps a receipt from elsewhere on screen; then it folds anyway. */
const HOVER_HOLD_MAX_MS = 30_000;
/** The longest the page waits for the ticket's timeline to name who decided, before it speaks. */
const LOOKUP_MS = 1500;
/** The fold itself (HitlInbox.css `.hitl-slot`); skipped under quiet mode and reduced motion. */
const FOLD_MS = 200;

type LoadError = { text: string; detail: string };
/** A card decided here or elsewhere, kept on screen for its receipt, then folded. */
type Decided = { task: any; receipt: CardReceipt; folding: boolean };
type Entry = { id: string; task: any; decided: Decided | null };

const reducedMotion = () =>
  document.documentElement.getAttribute("data-quiet") === "on" ||
  (typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches);

/** A pointer that can rest on something (a mouse, a trackpad); a finger never "hovers". */
const canHover = () =>
  typeof window.matchMedia === "function" && window.matchMedia("(hover: hover)").matches;

const taskId = (t: any, i: number) => (typeof t?.id === "string" && t.id ? t.id : `row-${i}`);

/** What a decision did, in one sentence (the receipt's second line). */
function effectOf(verdict: Verdict | null, broadcast: boolean): string {
  if (verdict === "approved") return broadcast ? "The SMS and email are released for sending." : "The change goes ahead.";
  if (verdict === "rejected") return broadcast ? "The drafts are suppressed; nothing is sent." : "Nothing goes ahead.";
  return "";
}

const hm = (ts: unknown) => fmtHM(ts, "");

export default function HitlInbox({ session, tick }: { session: any; tick: number }) {
  const [tasks, setTasks] = useState<any[]>([]);
  const [incidents, setIncidents] = useState<Record<string, any>>({});
  // What screen readers hear after a claim or a decision; never drawn.
  const [announce, setAnnounce] = useState("");
  // False until the first answer (rows or an error), so the list can show skeletons instead of
  // a misleading "nothing waiting".
  const [loaded, setLoaded] = useState(false);
  const [loadError, setLoadError] = useState<LoadError | null>(null);
  const [busy, setBusy] = useState<Record<string, CardAction>>({});
  const [errors, setErrors] = useState<Record<string, { text: string; detail: string }>>({});
  const [decided, setDecided] = useState<Record<string, Decided>>({});
  // The open card. Pinned on first sight, so a card that arrives later never takes its place.
  const [openPick, setOpenPick] = useState<string | null>(null);

  // Request numbering: `asked` is the newest request sent, `shown` the newest whose answer may be
  // applied. An answer numbered below `shown` is stale and dropped.
  const asked = useRef(0);
  const shown = useRef(0);
  // Every id decided here or elsewhere, for the page's life: a late answer never revives one.
  const decidedIds = useRef<Set<string>>(new Set());
  // Reasons typed per card, kept while the card is a row; the card reads its own on mount.
  const reasons = useRef<Record<string, string>>({});
  // `hitl.approved` / `hitl.rejected` frames this tab has seen: who decided a card, and when.
  const seen = useRef<Map<string, ElsewhereDecision>>(new Map());
  // The cards on screen as of the last answer, to see which ones the next answer drops.
  const onScreen = useRef<any[]>([]);
  // Set by a row's click or Enter: the card to scroll to and focus once it has opened.
  const focusOnOpen = useRef<string | null>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const timers = useRef<number[]>([]);
  useEffect(() => () => timers.current.forEach((t) => window.clearTimeout(t)), []);
  const later = (fn: () => void, ms: number) => {
    timers.current.push(window.setTimeout(fn, ms));
  };

  const rt = useRealtimeState();
  const connected = rt?.connected ?? false;
  const feed = rt?.feed ?? null;

  // Who decided a card, read off the live frames as they arrive (and the ones already in the
  // ticker when the page opened). A tap re-renders nothing.
  useEffect(() => {
    if (!feed) return;
    const note = (ev: NocEvent) => {
      if (ev.type !== "hitl.approved" && ev.type !== "hitl.rejected") return;
      const id = ev.payload?.task_id;
      if (typeof id !== "string" || !id) return;
      const by = ev.payload?.resolved_by;
      seen.current.set(id, {
        verdict: ev.type === "hitl.approved" ? "approved" : "rejected",
        by: typeof by === "string" && by ? by : null,
        at: ev.ts,
      });
    };
    for (const ev of feed.getEvents()) note(ev);
    return feed.tap(note);
  }, [feed]);

  const who = session?.display_name || "Supervisor";

  /** The queue as drawn: the cards waiting, with each decided card back in its place (the sort
   *  puts it there) until it folds. */
  const display = useMemo<Entry[]>(() => {
    const live: Entry[] = tasks.map((t, i) => ({ id: taskId(t, i), task: t, decided: null }));
    const kept: Entry[] = Object.entries(decided).map(([id, d]) => ({ id, task: d.task, decided: d }));
    const all = [...live, ...kept];
    const order = sortQueue(all.map((e) => e.task));
    const byTask = new Map(all.map((e) => [e.task, e]));
    return order.map((t) => byTask.get(t)!).filter(Boolean);
  }, [tasks, decided]);

  // The open card: the pinned one while it is on screen, else the first card still waiting.
  const openId = useMemo(() => {
    if (openPick && display.some((e) => e.id === openPick)) return openPick;
    return display.find((e) => !e.decided)?.id ?? null;
  }, [display, openPick]);

  // Read by timers and async answers, which run after the render that scheduled them.
  const displayNow = useRef(display);
  displayNow.current = display;
  const openNow = useRef(openId);
  openNow.current = openId;

  const slotOf = (id: string) =>
    listRef.current?.querySelector<HTMLElement>(`.hitl-slot[data-card-id="${CSS.escape(id)}"]`) ?? null;

  /**
   * Focus for a card: an open card's Claim, else its heading; a row's button. The page heading
   * only when `orHeading` (the open card left and nothing is waiting). Never scrolls the page:
   * the focus follows the presenter's place, the view does not jump to it.
   */
  const focusCard = useCallback((id: string | null, orHeading: boolean) => {
    const slot = id ? slotOf(id) : null;
    const target =
      slot?.querySelector<HTMLElement>(".hitl-claim") ||
      slot?.querySelector<HTMLElement>(".hitl-card h2") ||
      slot?.querySelector<HTMLElement>(".hitl-row");
    (target || (orHeading ? headingRef.current : null))?.focus({ preventScroll: true });
  }, []);

  /** The card after `id` in the queue that is still waiting, else the one before it. */
  const neighbourOf = (id: string): string | null => {
    const list = displayNow.current;
    const at = list.findIndex((e) => e.id === id);
    if (at < 0) return list.find((e) => !e.decided && e.id !== id)?.id ?? null;
    for (let i = at + 1; i < list.length; i++) if (!list[i].decided) return list[i].id;
    for (let i = at - 1; i >= 0; i--) if (!list[i].decided) return list[i].id;
    return null;
  };

  /**
   * Show the receipt for `ms`, fold the card, hand the open slot and focus on. A receipt from
   * elsewhere (`hold`) is not folded while its Copy button has focus, nor, on a screen with a
   * pointer that can rest (`(hover: hover)`), from under the pointer for up to 30 s: it is checked
   * again a second later instead. A touch screen's sticky :hover holds nothing.
   */
  const retire = useCallback(
    (id: string, ms: number, hold: boolean) => {
      const quick = reducedMotion();
      let hoverSince: number | null = null;
      const drop = () => {
        const wasOpen = openNow.current === id;
        const next = neighbourOf(id);
        // Focus moves only when it was inside the slot that is leaving (its receipt, its Copy
        // button); a presenter reading elsewhere on the page keeps their focus and their scroll.
        const slot = slotOf(id);
        const active = document.activeElement;
        const hadFocus = slot != null && active != null && active !== document.body && slot.contains(active);
        setDecided((d) => {
          if (!d[id]) return d;
          const rest = { ...d };
          delete rest[id];
          return rest;
        });
        if (wasOpen) setOpenPick(next);
        if (!hadFocus) return;
        // Once React has removed the slot, the next card takes focus, unless focus has already
        // gone somewhere else meanwhile. The page heading only for the open card's slot.
        window.requestAnimationFrame(() =>
          window.requestAnimationFrame(() => {
            const now = document.activeElement;
            if (now && now !== document.body && now.isConnected) return;
            focusCard(next, wasOpen);
          })
        );
      };
      const fold = () => {
        setDecided((d) => (d[id] ? { ...d, [id]: { ...d[id], folding: true } } : d));
        later(drop, FOLD_MS);
      };
      const expire = () => {
        if (hold) {
          const slot = slotOf(id);
          const active = document.activeElement;
          const focused = slot != null && active instanceof HTMLButtonElement && slot.contains(active);
          let hovered = slot != null && canHover() && slot.matches(":hover");
          if (hovered) {
            const now = Date.now();
            if (hoverSince == null) hoverSince = now;
            if (now - hoverSince >= HOVER_HOLD_MAX_MS) hovered = false;
          }
          if (focused || hovered) return later(expire, HOLD_RECHECK_MS);
        }
        if (quick) drop();
        else fold();
      };
      later(expire, ms);
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [focusCard]
  );

  /**
   * Another session decided `task` first. Its card is replaced in place by a receipt (never
   * dropped), which names who and when as soon as either is known; the ticket's timeline is
   * asked when the live frames did not say. Spoken once, when the lookup has answered.
   */
  const decidedElsewhere = useCallback(
    (task: any, hint: Verdict | null) => {
      const id = typeof task?.id === "string" ? task.id : "";
      if (!id || decidedIds.current.has(id)) return;
      decidedIds.current.add(id);
      const wasOpen = openNow.current === id;
      const lost = (reasons.current[id] || "").trim();
      const broadcast = task?.task_type === "APPROVE_BROADCAST";
      const known = seen.current.get(id);
      let d: ElsewhereDecision = { verdict: known?.verdict ?? hint, by: known?.by ?? null, at: known?.at ?? null };
      const receiptOf = (x: ElsewhereDecision): CardReceipt => ({
        headline: elsewhereHeadline(x, hm),
        effect: effectOf(x.verdict, broadcast),
        approved: x.verdict === "approved",
        elsewhere: true,
        lostReason: lost || undefined,
      });
      setDecided((m) => ({ ...m, [id]: { task, receipt: receiptOf(d), folding: false } }));
      setTasks((ts) => ts.filter((t) => t?.id !== id));
      if (wasOpen) setOpenPick(id); // it stays the open card, in place, until it folds
      setBusy((b) => {
        if (!b[id]) return b;
        const rest = { ...b };
        delete rest[id];
        return rest;
      });
      setErrors((prev) => {
        if (!prev[id]) return prev;
        const rest = { ...prev };
        delete rest[id];
        return rest;
      });
      // The receipt took the open card's footer: it has focus (ApprovalCard). A row only takes
      // focus when focus was on that row (its button is replaced by the receipt, which would drop
      // focus to <body>); focus already on <body>, or anywhere else, stays where it is.
      if (!wasOpen) {
        const before = slotOf(id);
        const active = document.activeElement;
        const onRow = before != null && active != null && active !== document.body && before.contains(active);
        if (onRow) {
          window.requestAnimationFrame(() => {
            const slot = slotOf(id);
            const now = document.activeElement;
            if (slot && (now === document.body || !now?.isConnected || slot.contains(now)))
              slot.querySelector<HTMLElement>(".hitl-row")?.focus({ preventScroll: true });
          });
        }
      }

      let spoken = false;
      const speak = () => {
        if (spoken) return;
        spoken = true;
        const r = receiptOf(d);
        setAnnounce(
          `${hitlSubject(task)}: ${r.headline}. ${r.effect}${lost ? " Your reason was not recorded; it is on the card, with Copy." : ""}`.trim()
        );
      };
      retire(id, ELSEWHERE_MS, true);

      const incidentId = typeof task?.incident_id === "string" ? task.incident_id : "";
      if ((d.by && d.verdict && d.at) || !incidentId) return speak();
      later(speak, LOOKUP_MS);
      api
        .timeline(incidentId)
        .then((items) => {
          const found = decisionFromTimeline(items, task?.created_at);
          if (!found) return;
          d = { verdict: d.verdict ?? found.verdict, by: d.by ?? found.by, at: d.at ?? found.at };
          setDecided((m) => (m[id] ? { ...m, [id]: { ...m[id], receipt: receiptOf(d) } } : m));
        })
        .catch(() => undefined) // the receipt already says what is known
        .finally(speak);
    },
    [retire]
  );

  /**
   * The queue and the incident facts in one round. `/api/v1/hitl/pending` returns only
   * `incident_number`, `priority` and `site_id`; est. subscribers, M-PESA risk, services and the
   * owner live on the incident. Both answers are applied together, before the list first draws,
   * so the facts never arrive later and push the drafts down. A failed incidents call keeps the
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
        // Only ever *replace* the queue with something actually received, minus anything decided
        // here or elsewhere. A failed or malformed refetch leaves the last good list on screen.
        const rows = queue.value;
        const list = (Array.isArray(rows) ? rows.filter(isPlainObject) : []).filter(
          (t) => !decidedIds.current.has(String(t.id))
        );
        // A card on screen that this answer no longer has was decided in another session.
        const now = new Set(list.map((t) => t.id));
        const gone = onScreen.current.filter((t) => typeof t?.id === "string" && !now.has(t.id) && !decidedIds.current.has(t.id));
        onScreen.current = list;
        setTasks(list);
        setLoadError(null);
        for (const t of gone) decidedElsewhere(t, null);
        // Pin the open card the first time there is one.
        setOpenPick((pick) => pick ?? (list.length ? taskId(sortQueue(list)[0], 0) : null));
      } else {
        const e = queue.reason;
        setLoadError({ text: friendlyLoadError(e), detail: e instanceof Error ? e.message : String(e) });
      }
      setLoaded(true);
    });
  }, [decidedElsewhere]);

  const reload = load;

  useEffect(load, [tick, load]);

  const act = useCallback(
    async (task: any, action: CardAction, reason = "") => {
      const id = taskId(task, 0);
      const subject = hitlSubject(task);
      const broadcast = task?.task_type === "APPROVE_BROADCAST";
      setOpenPick(id);
      setBusy((b) => ({ ...b, [id]: action }));
      setErrors((prev) => {
        if (!prev[id]) return prev;
        const rest = { ...prev };
        delete rest[id];
        return rest;
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
          const effect = effectOf(action === "approve" ? "approved" : "rejected", broadcast);
          decidedIds.current.add(id);
          const receipt = { headline: `${verb} by ${who}`, effect, approved: action === "approve" };
          setDecided((d) => ({ ...d, [id]: { task, receipt, folding: false } }));
          setTasks((ts) => ts.filter((t) => t?.id !== id));
          setAnnounce(`${verb} ${subject}. ${effect}`);
          retire(id, RECEIPT_MS, false);
        }
      } catch (e) {
        const code = statusOf(e);
        if (code === 409 || code === 404) {
          // Somebody else decided it first (a claim only fails this way once the card is decided):
          // the card says so in place, keeps the reason typed here, and folds like any decided card.
          decidedElsewhere(task, verdictOfConflict(e));
          return;
        }
        setErrors((prev) => ({ ...prev, [id]: { text: friendlyError(e), detail: e instanceof Error ? e.message : String(e) } }));
        // The pressed button stays; after a failed claim the card's heading takes focus so the
        // error under it is read in context.
        if (action === "claim") {
          window.requestAnimationFrame(() =>
            slotOf(id)?.querySelector<HTMLElement>(".hitl-card h2")?.focus({ preventScroll: true })
          );
        }
      } finally {
        setBusy((b) => {
          if (!b[id]) return b;
          const rest = { ...b };
          delete rest[id];
          return rest;
        });
        reload(); // whatever happened, the truth is on the server
      }
    },
    [reload, retire, decidedElsewhere, who]
  );

  /** A row's click or Enter: that card opens, the open one folds to its row. */
  const openCard = useCallback((id: string) => {
    focusOnOpen.current = id;
    setOpenPick(id);
  }, []);

  // The card a row opened is brought to the top of the view (under the sticky top bar) and its
  // heading takes focus, so the keyboard continues inside it. The first card, open on load, is not.
  useLayoutEffect(() => {
    const id = focusOnOpen.current;
    if (!id || id !== openId) return;
    focusOnOpen.current = null;
    const slot = slotOf(id);
    slot?.scrollIntoView({ block: "start" });
    slot?.querySelector<HTMLElement>(".hitl-card h2")?.focus({ preventScroll: true });
  }, [openId]);

  const summary = useMemo(() => queueSummary(tasks), [tasks]);
  // The escalation ladder's watch colour on a row's wait says "this one is late". When most cards
  // are late it says nothing about any one of them, so it is said once, on the oldest wait.
  const mostlyLate = useMemo(() => {
    const late = tasks.filter((t) => ladderBreached(ageMinutes(t?.created_at), t?.priority, t?.claimed_by)).length;
    return tasks.length > 1 && late > tasks.length / 2;
  }, [tasks]);
  const showList = loaded && display.length > 0;

  return (
    <div>
      <div className="page-head">
        <div>
          <h1 ref={headingRef} tabIndex={-1}>
            Approvals
          </h1>
          <p className="lead">Each card is a decision for a person: claim it, read what goes out, approve or reject.</p>
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
            {summary.byPriority && <span>{summary.byPriority}</span>}
            <span>{summary.waiting} waiting</span>
            {summary.oldest != null && (
              <span
                className={mostlyLate ? "hitl-summary-late" : undefined}
                title={mostlyLate ? "Most cards are past the escalation ladder: unclaimed at 5 minutes or more." : undefined}
              >
                oldest {fmtWait(summary.oldest)}
              </span>
            )}
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
          <div className="hitl-slot is-open">
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
          {[0, 1, 2, 3, 4].map((n) => (
            <div key={n} className={"hitl-slot is-row" + (n === 0 ? " run-start" : "") + (n === 4 ? " run-end" : "")} aria-hidden="true">
              <div className="hitl-row hitl-row-skel">
                <span className="skeleton" />
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
            a person. <Link to="/mission">Watch Mission control</Link>
          </div>
        </div>
      )}

      {showList && (
        <div className="hitl-list" ref={listRef}>
          {display.map(({ id, task: t, decided: d }, i) => {
            const open = id === openId;
            const rowBefore = i > 0 && display[i - 1].id !== openId;
            const rowAfter = i < display.length - 1 && display[i + 1].id !== openId;
            const cls = [
              "hitl-slot",
              open ? "is-open" : "is-row",
              !open && !rowBefore ? "run-start" : "",
              !open && !rowAfter ? "run-end" : "",
              d?.folding ? "folding" : "",
            ]
              .filter(Boolean)
              .join(" ");
            const err = errors[id];
            return (
              <div key={id} className={cls} data-card-id={id}>
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
                        This card could not be drawn. The raw payload is below; decide from it, or open the
                        ticket.
                      </p>
                      <pre className="pre hitl-field-pre" tabIndex={0}>
                        {rawPayload(t?.proposed_payload)}
                      </pre>
                      <div className="hitl-buttons hitl-fallback-actions">
                        <button
                          className="btn danger"
                          aria-disabled={busy[id] || d ? true : undefined}
                          onClick={() => {
                            if (!busy[id] && !d) act(t, "reject", "card render failure — rejected unread");
                          }}
                        >
                          {busy[id] === "reject" ? "Rejecting…" : "Reject unread"}
                        </button>
                      </div>
                    </article>
                  }
                >
                  {open ? (
                    <ApprovalCard
                      task={t}
                      incident={t?.incident_id ? incidents[t.incident_id] : null}
                      who={who}
                      busy={busy[id] ?? null}
                      error={err?.text || ""}
                      errorDetail={err?.detail}
                      receipt={d?.receipt ?? null}
                      initialReason={reasons.current[id] ?? ""}
                      onReasonChange={(r) => {
                        reasons.current[id] = r;
                      }}
                      onClaim={() => act(t, "claim")}
                      onApprove={(reason) => act(t, "approve", reason)}
                      onReject={(reason) => act(t, "reject", reason)}
                    />
                  ) : (
                    <QueueRow
                      id={id}
                      task={t}
                      incident={t?.incident_id ? incidents[t.incident_id] : null}
                      who={who}
                      receipt={d?.receipt ?? null}
                      markLate={!mostlyLate}
                      onOpen={openCard}
                    />
                  )}
                </CardBoundary>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

/**
 * One card folded to a row: priority, ticket, type, site, wait (and who holds it, when someone
 * does). A heading, so the queue reads as a list of cards; its button opens the card. A card
 * decided in another session while folded shows its receipt here instead.
 */
const QueueRow = memo(function QueueRow({
  id,
  task,
  incident,
  who,
  receipt,
  markLate,
  onOpen,
}: {
  id: string;
  task: any;
  incident: any;
  who: string;
  receipt: CardReceipt | null;
  /** False when most of the queue is late and the summary says so once. */
  markLate: boolean;
  onOpen: (id: string) => void;
}) {
  const t = task && typeof task === "object" ? task : {};
  const priority = typeof t.priority === "string" && t.priority ? t.priority : "";
  const pill = priority && (
    <span className={`pill ${priority}`} title={priorityTitle(priority)}>
      {priority}
    </span>
  );
  const subject = (
    <span className={hitlSubjectIsIncident(t) ? "hitl-row-id" : "hitl-row-subject"}>{hitlSubject(t)}</span>
  );
  if (receipt) {
    return (
      <div className="hitl-row decided" tabIndex={-1}>
        {pill}
        {subject}
        <span className="hitl-row-receipt">
          <ReceiptBody receipt={receipt} />
        </span>
      </div>
    );
  }
  const siteId = typeof t.site_id === "string" && t.site_id ? t.site_id : typeof incident?.site_id === "string" ? incident.site_id : "";
  const siteName = typeof incident?.site_name === "string" ? incident.site_name : "";
  const claimed = typeof t.claimed_by === "string" && t.claimed_by ? t.claimed_by : "";
  const mins = ageMinutes(t.created_at);
  const late = markLate && ladderBreached(mins, t.priority, t.claimed_by);
  return (
    <h2 className="hitl-row-h">
      <button type="button" className="hitl-row" aria-expanded={false} onClick={() => onOpen(id)}>
        {pill}
        {subject}
        <span className="hitl-row-type">{labelFor(t.task_type)}</span>
        {(siteId || siteName) && (
          <span className="hitl-row-site">
            {siteId && <span className="mono">{siteId}</span>}
            {siteName && <span className="hitl-row-name">{siteName}</span>}
          </span>
        )}
        {claimed && <span className="hitl-row-claim">claimed by {claimed === who ? "you" : claimed}</span>}
        <span className={late ? "hitl-row-wait late" : "hitl-row-wait"}>{fmtAge(mins)}</span>
      </button>
    </h2>
  );
});
