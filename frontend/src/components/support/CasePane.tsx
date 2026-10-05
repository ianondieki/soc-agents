import { useCallback, useEffect, useId, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowLeft } from "lucide-react";
import { humanStatus } from "../../lib/agents";
import { IconCheck } from "../../lib/icons";
import {
  BODY_MIN,
  CATEGORY_WORD,
  CHANNEL_WORD,
  LANGUAGE_WORD,
  errorDetail,
  fmtAge,
  fmtDue,
  isNetworkError,
  kes,
  linkStrengthWord,
  minutesUntil,
  statusOfError,
  supportApi,
  toolPhrase,
  verdictOf,
  withPerson,
  type CaseDetail,
  type ToolCall,
} from "../../lib/support";
import { fmtEAT, fmtTime } from "../../lib/time";
import AgentTrace from "./AgentTrace";
import { ROUTE_ICON, StatusWord } from "./marks";

/**
 * The case: the customer's message as the hero, one verdict line, the agent trace as a short
 * story, the linked NOC incident, the replies, and, where a person decides, the decision in the
 * Approvals idiom. The decision's words and fields sit in the page flow; only its buttons stick
 * to the foot of the viewport. Reject asks for a reason once pressed, as Approvals does.
 *
 * Focus never drops to <body>: a claim puts it in the reply box, a decision on the receipt (or
 * the case head), and the parent returns it to the queue row on "Back".
 */

export interface CasePaneProps {
  id: string;
  who: string;
  tick: number;
  /** Phone: the pane replaces the list and offers a way back. */
  stacked: boolean;
  onBack: () => void;
  /** The detail after an action here: the list row and the figures update from it. */
  onChanged: (d: CaseDetail) => void;
  titles: Record<string, string>;
  /** Bumped when a person picked this case from the list: once loaded, the case scrolls to its top. */
  scrollKey?: number;
}

type Busy = "" | "claim" | "resolve" | "approve" | "reject";

function actionError(e: unknown): string {
  const s = statusOfError(e);
  if (s === 409) return "Somebody else got there first; the case has moved on. The view below is the latest.";
  if (s === 404) return "This case is gone; nothing was changed.";
  if (s === 403) return `Not permitted: ${errorDetail(e, "your role cannot decide this case")}`;
  if (s === 422 || s === 400) return errorDetail(e, "The API refused the request.");
  if (s != null && s >= 500) return "The API failed on that request. Nothing was decided; try again.";
  if (isNetworkError(e)) return "The API is unreachable. Nothing was decided; try again.";
  return errorDetail(e);
}

function urgencyTone(u: string): string {
  return u === "critical" ? " danger" : u === "high" ? " warn" : "";
}

/** "the refund of KES 1,500", "the M-PESA reversal (code SHR2M9PL4Q)". */
function heldSummary(call: ToolCall): string {
  const a = call.args ?? {};
  const amount = kes(a.amount_kes ?? a.refund_kes ?? (call.result as any)?.amount_kes);
  const code = typeof a.transaction_code === "string" ? a.transaction_code : "";
  return `${toolPhrase(call.tool)}${amount ? ` of ${amount}` : ""}${code ? ` (code ${code})` : ""}`;
}

export default function CasePane({ id, who, tick, stacked, onBack, onChanged, titles, scrollKey = 0 }: CasePaneProps) {
  const [detail, setDetail] = useState<CaseDetail | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState<Busy>("");
  const [error, setError] = useState<string | null>(null);
  const [announce, setAnnounce] = useState("");
  const [reason, setReason] = useState("");
  const [rejectArmed, setRejectArmed] = useState(false);
  const [reply, setReply] = useState("");
  const [note, setNote] = useState("");
  const [missing, setMissing] = useState<"" | "reason" | "reply">("");
  const uid = useId();
  const shownId = useRef<string | null>(null);
  const asked = useRef(0);
  const focusAfter = useRef<"" | "reply" | "receipt">("");
  const reasonRef = useRef<HTMLTextAreaElement>(null);
  const replyRef = useRef<HTMLTextAreaElement>(null);
  const headRef = useRef<HTMLHeadingElement>(null);
  const receiptRef = useRef<HTMLParagraphElement>(null);
  const articleRef = useRef<HTMLElement>(null);

  const load = useCallback(() => {
    const mine = ++asked.current;
    supportApi
      .complaint(id)
      .then((d) => {
        if (mine !== asked.current) return;
        setDetail(d);
        setLoadError(null);
        shownId.current = id;
      })
      .catch((e) => {
        if (mine !== asked.current) return;
        setLoadError(statusOfError(e) === 404 ? "This case is not on the desk any more." : actionError(e));
      });
  }, [id]);

  useEffect(() => {
    if (shownId.current !== id) {
      setDetail(null);
      setLoadError(null);
      setError(null);
      setReason("");
      setRejectArmed(false);
      setReply("");
      setNote("");
      setMissing("");
    }
    load();
  }, [id, tick, load]);

  // A case opened from the list on a phone takes the heading, so the keyboard continues inside it.
  useEffect(() => {
    if (stacked && detail && !focusAfter.current) headRef.current?.focus({ preventScroll: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stacked, detail?.complaint.id]);

  // A case a person picked scrolls to its top once it has loaded (the skeleton was shorter than
  // the screen, so an earlier scroll could not reach it).
  useEffect(() => {
    if (scrollKey > 0 && detail) articleRef.current?.scrollIntoView({ block: "start" });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scrollKey, detail?.complaint.id]);

  // After an action here, focus goes where the next thing happens: the reply box after a claim,
  // the receipt (or the head) after a decision.
  useEffect(() => {
    if (!detail || !focusAfter.current) return;
    const want = focusAfter.current;
    focusAfter.current = "";
    window.requestAnimationFrame(() => {
      if (want === "reply" && replyRef.current) replyRef.current.focus();
      else (receiptRef.current ?? headRef.current)?.focus({ preventScroll: true });
    });
  }, [detail]);

  const apply = (d: CaseDetail) => {
    setDetail(d);
    shownId.current = d.complaint.id;
    onChanged(d);
  };

  const run = async (what: Busy, fn: () => Promise<CaseDetail>, said: string, then: "reply" | "receipt") => {
    setBusy(what);
    setError(null);
    try {
      const d = await fn();
      focusAfter.current = then;
      apply(d);
      setAnnounce(said);
    } catch (e) {
      setError(actionError(e));
      if (statusOfError(e) === 409 || statusOfError(e) === 404) load();
    } finally {
      setBusy("");
    }
  };

  if (loadError) {
    return (
      <div className="panel sd-case">
        <BackRow stacked={stacked} onBack={onBack} />
        <div className="empty" role="alert">
          Couldn't open this case. {loadError}
          <button type="button" className="btn sm" onClick={load}>
            Retry
          </button>
        </div>
      </div>
    );
  }

  if (!detail) {
    return (
      <div className="panel sd-case" aria-busy="true">
        <BackRow stacked={stacked} onBack={onBack} />
        <span className="sr-only" role="status">
          Opening the case.
        </span>
        <div className="skeleton-rows" aria-hidden="true">
          <span className="skeleton" style={{ width: "28%" }} />
          <span className="skeleton" style={{ width: "92%", height: 20 }} />
          <span className="skeleton" style={{ width: "70%", height: 20 }} />
          <span className="skeleton" style={{ width: "48%" }} />
          <span className="skeleton" style={{ width: "84%" }} />
          <span className="skeleton" style={{ width: "76%" }} />
        </div>
      </div>
    );
  }

  const c = detail.complaint;
  const v = verdictOf(detail);
  const Icon = ROUTE_ICON[v.route];
  const held = detail.tool_calls.find((t) => t.status === "needs_approval") ?? null;
  const replies = detail.messages.filter((m) => m.author !== "customer").sort((a, b) => String(a.at).localeCompare(String(b.at)));
  const person = withPerson(c.status);
  const dueMin = minutesUntil(c.sla_due_at);
  const dueTone = dueMin == null ? "" : dueMin < 0 ? " danger" : dueMin <= 60 ? " warn" : "";
  const claimedByMe = c.escalation?.claimed_by === who;
  const heldOnEscalation = detail.steps.find((s) => s.agent === "escalation")?.detail?.held_tool;
  const approvedStep = [...detail.steps].reverse().find((s) => s.agent === "human" && s.action === "approved");
  const approvedCall = detail.tool_calls.find((t) => t.decided_by && (t.status === "ok" || t.status === "approved"));
  const resolvedStep = [...detail.steps].reverse().find((s) => s.agent === "human" && s.action === "resolved");

  const claim = () => run("claim", () => supportApi.claim(c.id), `Claimed ${c.ref}. Write a reply, then resolve it.`, "reply");
  const resolve = () => {
    if (reply.trim().length < BODY_MIN) {
      setMissing("reply");
      replyRef.current?.focus();
      return;
    }
    void run("resolve", () => supportApi.resolve(c.id, reply.trim(), note), `Resolved ${c.ref}. The reply was sent to the customer.`, "receipt");
  };
  const approve = (call: ToolCall) => run("approve", () => supportApi.approve(c.id, call.id), `Approved ${toolPhrase(call.tool)} on ${c.ref}; it ran.`, "receipt");
  const reject = (call: ToolCall) => {
    // First press: ask for the reason. Second press: reject with it.
    if (!rejectArmed) {
      setRejectArmed(true);
      window.requestAnimationFrame(() => reasonRef.current?.focus());
      return;
    }
    if (!reason.trim()) {
      setMissing("reason");
      reasonRef.current?.focus();
      return;
    }
    void run("reject", () => supportApi.reject(c.id, call.id, reason.trim()), `Rejected ${toolPhrase(call.tool)} on ${c.ref}. The case is with a person.`, "receipt");
  };

  const reasonId = `${uid}-reason`;
  const replyId = `${uid}-reply`;
  const noteId = `${uid}-note`;
  const msgId = `${uid}-msg`;
  const decided = c.status === "resolved" || (c.status === "action_taken" && !!approvedCall);

  return (
    <article className="panel sd-case" aria-labelledby={`${uid}-ref`} ref={articleRef}>
      <BackRow stacked={stacked} onBack={onBack} />
      <span className="sr-only" role="status">
        {announce}
      </span>

      <header className="sd-case-head">
        <h2 id={`${uid}-ref`} ref={headRef} tabIndex={-1} className="sd-case-ref mono">
          {c.ref}
        </h2>
        <span className="facts sd-case-facts">
          <span>{CHANNEL_WORD[c.channel] ?? c.channel}</span>
          <span>{LANGUAGE_WORD[c.language] ?? c.language}</span>
          <span>
            received <span className="mono">{fmtEAT(c.created_at)}</span>, {fmtAge(c.created_at)} ago
          </span>
        </span>
        <StatusWord status={c.status} claimedBy={c.escalation?.claimed_by} closureReason={c.closure_reason} />
      </header>

      <div className="sd-hero">
        <div className="sd-hero-who">
          <span className="sd-hero-name">{c.customer.name || "Customer"}</span>
          <span className="mono">{c.customer.msisdn_masked}</span>
          {c.customer.account_ref && <span className="mono">{c.customer.account_ref}</span>}
        </div>
        <p className="sd-hero-body" lang={c.language === "sw" ? "sw" : undefined}>
          {c.body}
        </p>
      </div>

      <div className={"sd-verdict" + (v.tone ? ` ${v.tone}` : "")}>
        <Icon size={18} strokeWidth={1.75} aria-hidden="true" />
        <div>
          <p className="sd-verdict-text">{v.text}</p>
          {v.sub && <p className="sd-verdict-sub">{v.sub}</p>}
          <div className="facts sd-verdict-facts">
            {/* How a linked complaint was matched to its incident: a weak match reads as one. */}
            {c.linked_incident && linkStrengthWord(c.link_strength) && (
              <span className={c.link_strength === "region" ? "attn warn" : undefined}>
                <span className="mono">{c.linked_incident.incident_number}</span>, {linkStrengthWord(c.link_strength)}
              </span>
            )}
            <span>{CATEGORY_WORD[c.category] ?? c.category}</span>
            <span className={urgencyTone(c.urgency) ? `attn${urgencyTone(c.urgency)}` : undefined}>urgency {c.urgency}</span>
            <span>{c.sentiment === "calm" ? "calm" : `sounds ${c.sentiment}`}</span>
            {person && (
              <span className={dueTone ? `attn${dueTone}` : undefined}>
                {dueMin != null && dueMin < 0 ? "reply overdue, was due" : "reply due"} <span className="mono">{fmtDue(c.sla_due_at)}</span>
              </span>
            )}
          </div>
        </div>
      </div>

      <AgentTrace detail={detail} titles={titles} />

      {c.linked_incident && (
        <section className="sd-section" aria-labelledby={`${uid}-inc`}>
          <div className="head-row sd-section-head">
            <h3 id={`${uid}-inc`}>Linked network incident</h3>
          </div>
          <div className="sd-incident">
            <Link to={`/incidents/${c.linked_incident.id}`} className="mono sd-incident-num">
              {c.linked_incident.incident_number}
            </Link>
            <span className="sd-incident-title">{c.linked_incident.title}</span>
            <span className="muted">{humanStatus(c.linked_incident.status)}</span>
            <Link to={`/incidents/${c.linked_incident.id}`} className="btn sm sd-incident-open">
              Open the ticket
            </Link>
          </div>
        </section>
      )}

      <section className="sd-section" aria-labelledby={`${uid}-rep`}>
        <div className="head-row sd-section-head">
          <h3 id={`${uid}-rep`}>{replies.length > 1 ? "Replies to the customer" : "Reply to the customer"}</h3>
          {replies.length === 0 && <span>none sent</span>}
        </div>
        {replies.map((m) => (
          <div key={m.id} className="sd-msg">
            <div className="sd-msg-head">
              <span className="sd-msg-who">{m.author === "staff" ? m.name || "A member of staff" : "Support desk"}</span>
              <span className="mono">{fmtTime(m.at)}</span>
              {m.author === "staff" && <span>a person</span>}
            </div>
            <p className="sd-msg-body">{m.body}</p>
          </div>
        ))}
      </section>

      {/* The decision: words and fields in the flow, the buttons on a short sticky bar. */}
      {person && (
        <section className="sd-section sd-decide" aria-labelledby={`${uid}-dec`}>
          <div className="head-row sd-section-head">
            <h3 id={`${uid}-dec`}>Your decision</h3>
          </div>

          {c.status === "escalated" && (
            <p className="sd-decide-text">
              Waiting for a person since <span className="mono">{fmtEAT(c.escalation?.at)}</span>
              {heldOnEscalation ? (
                <>
                  . The action agent planned <strong>{toolPhrase(heldOnEscalation)}</strong> and held it, not run, for whoever takes the case
                </>
              ) : null}
              . Claim it to reply.
            </p>
          )}

          {c.status === "in_progress" && (
            <div className="sd-decide-form">
              <p className="sd-decide-text">
                {claimedByMe ? "Claimed by you" : `Claimed by ${c.escalation?.claimed_by ?? "a person"}`}. Resolving sends your reply to the customer and
                closes the case
                {heldOnEscalation ? `; ${toolPhrase(heldOnEscalation)} the agent held is not run` : ""}.
              </p>
              <label htmlFor={replyId} className="sd-label">
                Reply to the customer
              </label>
              <textarea
                id={replyId}
                ref={replyRef}
                className="sd-reply"
                rows={3}
                value={reply}
                onChange={(e) => {
                  setReply(e.target.value);
                  if (missing === "reply" && e.target.value.trim().length >= BODY_MIN) setMissing("");
                }}
                aria-invalid={missing === "reply" || undefined}
                aria-describedby={missing === "reply" ? msgId : undefined}
              />
              {missing === "reply" && (
                <p id={msgId} className="sd-invalid">
                  Write the reply the customer will receive, at least {BODY_MIN} characters.
                </p>
              )}
              <label htmlFor={noteId} className="sd-label">
                Note for the record (optional)
              </label>
              <input id={noteId} className="sd-note" value={note} onChange={(e) => setNote(e.target.value)} maxLength={1000} />
            </div>
          )}

          {c.status === "awaiting_approval" && held && (
            <div className="sd-decide-form">
              <p className="sd-decide-text">
                The action agent planned <strong>{heldSummary(held)}</strong>; it is above the automatic limit. Approve it and it runs now; reject it with
                a reason and the case goes to a person.
              </p>
              {rejectArmed && (
                <>
                  <label htmlFor={reasonId} className="sd-label">
                    Reason for rejecting (kept on the case)
                  </label>
                  <textarea
                    id={reasonId}
                    ref={reasonRef}
                    className="sd-reply"
                    rows={2}
                    value={reason}
                    onChange={(e) => {
                      setReason(e.target.value);
                      if (missing === "reason" && e.target.value.trim()) setMissing("");
                    }}
                    aria-invalid={missing === "reason" || undefined}
                    aria-describedby={missing === "reason" ? msgId : undefined}
                  />
                  {missing === "reason" && (
                    <p id={msgId} className="sd-invalid">
                      Add a reason to reject; it is kept on the case.
                    </p>
                  )}
                </>
              )}
            </div>
          )}

          {c.status === "awaiting_approval" && !held && (
            <p className="sd-decide-text">The held tool call is no longer on the case. Refresh to see where it stands.</p>
          )}
        </section>
      )}

      {person && (
        <footer className="sd-actions live">
          {error && (
            <div className="sd-error" role="alert">
              {error}
            </div>
          )}
          <div className="sd-buttons">
            {c.status === "escalated" && (
              <button type="button" className="btn primary" onClick={claim} aria-disabled={busy ? true : undefined}>
                {busy === "claim" ? "Claiming…" : "Claim this case"}
              </button>
            )}
            {c.status === "in_progress" && (
              <button type="button" className="btn primary" onClick={resolve} aria-disabled={busy ? true : undefined}>
                {busy === "resolve" ? "Resolving…" : "Resolve and send the reply"}
              </button>
            )}
            {c.status === "awaiting_approval" && held && (
              <>
                <button type="button" className="btn primary" onClick={() => approve(held)} aria-disabled={busy ? true : undefined}>
                  {busy === "approve" ? "Approving…" : "Approve and run it"}
                </button>
                <button type="button" className="btn danger" onClick={() => reject(held)} aria-disabled={busy ? true : undefined}>
                  {busy === "reject" ? "Rejecting…" : rejectArmed ? "Reject with this reason" : "Reject"}
                </button>
              </>
            )}
          </div>
        </footer>
      )}

      {decided && (
        <footer className="sd-actions">
          {c.status === "resolved" ? (
            <Receipt text={v.text} when={resolvedStep?.at} refEl={receiptRef} />
          ) : (
            <Receipt text={`Approved by ${approvedCall?.decided_by}; ${toolPhrase(approvedCall?.tool)} ran`} when={approvedStep?.at ?? approvedCall?.at} refEl={receiptRef} />
          )}
        </footer>
      )}
    </article>
  );
}

function Receipt({ text, when, refEl }: { text: string; when?: string; refEl: React.RefObject<HTMLParagraphElement> }) {
  return (
    <p className="sd-receipt" ref={refEl} tabIndex={-1}>
      <span className="sd-receipt-verb">
        <IconCheck />
        {text}
      </span>
      {when && <span className="mono">{fmtEAT(when)}</span>}
    </p>
  );
}

function BackRow({ stacked, onBack }: { stacked: boolean; onBack: () => void }) {
  if (!stacked) return null;
  return (
    <div className="sd-back">
      <button type="button" className="btn ghost sm" onClick={onBack}>
        <ArrowLeft size={16} strokeWidth={1.75} aria-hidden="true" />
        Back to the list
      </button>
    </div>
  );
}
