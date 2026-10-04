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
import AgentTrace, { ToolCallView } from "./AgentTrace";
import { ROUTE_ICON, StatusWord } from "./marks";

/**
 * The case: the customer's message as the hero, one verdict line, the agent trace, the linked
 * NOC incident, the replies, and, where the case waits for a person, the decision. The decision
 * uses the Approvals page's language: Claim, then a reply to resolve; Approve or Reject a held
 * tool call, with a reason to reject. Violet marks every place a person decides.
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

export default function CasePane({ id, who, tick, stacked, onBack, onChanged, titles }: CasePaneProps) {
  const [detail, setDetail] = useState<CaseDetail | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState<Busy>("");
  const [error, setError] = useState<string | null>(null);
  const [announce, setAnnounce] = useState("");
  const [reason, setReason] = useState("");
  const [reply, setReply] = useState("");
  const [note, setNote] = useState("");
  const [missing, setMissing] = useState<"" | "reason" | "reply">("");
  const uid = useId();
  const shownId = useRef<string | null>(null);
  const asked = useRef(0);
  const reasonRef = useRef<HTMLTextAreaElement>(null);
  const replyRef = useRef<HTMLTextAreaElement>(null);
  const headRef = useRef<HTMLHeadingElement>(null);

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
      setReply("");
      setNote("");
      setMissing("");
    }
    load();
  }, [id, tick, load]);

  // A case opened from the list on a phone takes the heading, so the keyboard continues inside it.
  useEffect(() => {
    if (stacked && detail) headRef.current?.focus({ preventScroll: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stacked, detail?.complaint.id]);

  const apply = (d: CaseDetail) => {
    setDetail(d);
    shownId.current = d.complaint.id;
    onChanged(d);
  };

  const run = async (what: Busy, fn: () => Promise<CaseDetail>, said: string) => {
    setBusy(what);
    setError(null);
    try {
      const d = await fn();
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
          <span className="skeleton" style={{ width: "64%" }} />
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

  const claim = () => run("claim", () => supportApi.claim(c.id), `Claimed ${c.ref}. Write a reply, then resolve it.`);
  const resolve = () => {
    if (reply.trim().length < BODY_MIN) {
      setMissing("reply");
      replyRef.current?.focus();
      return;
    }
    void run("resolve", () => supportApi.resolve(c.id, reply.trim(), note), `Resolved ${c.ref}. The reply was sent to the customer.`);
  };
  const approve = (call: ToolCall) => run("approve", () => supportApi.approve(c.id, call.id), `Approved ${toolPhrase(call.tool)} on ${c.ref}; it ran.`);
  const reject = (call: ToolCall) => {
    if (!reason.trim()) {
      setMissing("reason");
      reasonRef.current?.focus();
      return;
    }
    void run("reject", () => supportApi.reject(c.id, call.id, reason.trim()), `Rejected ${toolPhrase(call.tool)} on ${c.ref}. The case is with a person.`);
  };

  const reasonId = `${uid}-reason`;
  const replyId = `${uid}-reply`;
  const noteId = `${uid}-note`;
  const msgId = `${uid}-msg`;

  return (
    <article className="panel sd-case" aria-labelledby={`${uid}-ref`}>
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
        <StatusWord status={c.status} claimedBy={c.escalation?.claimed_by} />
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

      {(person || c.status === "resolved" || (c.status === "action_taken" && detail.tool_calls.some((t) => t.decided_by))) && (
        <footer className={"sd-actions" + (person ? " live" : "")}>
          {error && (
            <div className="sd-error" role="alert">
              {error}
            </div>
          )}

          {c.status === "escalated" && (
            <div className="sd-decide">
              <p className="sd-decide-text">
                Waiting for a person since <span className="mono">{fmtEAT(c.escalation?.at)}</span>
                {heldOnEscalation ? (
                  <>
                    . The action agent had planned <strong>{toolPhrase(heldOnEscalation)}</strong>; it was held, not run, for whoever takes the case
                  </>
                ) : null}
                . Claim it to reply.
              </p>
              <div className="sd-buttons">
                <button type="button" className="btn hitl" onClick={claim} aria-disabled={busy ? true : undefined}>
                  {busy === "claim" ? "Claiming…" : "Claim this case"}
                </button>
              </div>
            </div>
          )}

          {c.status === "in_progress" && (
            <div className="sd-decide sd-decide-form">
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
              <div className="sd-buttons">
                <button type="button" className="btn hitl" onClick={resolve} aria-disabled={busy ? true : undefined}>
                  {busy === "resolve" ? "Resolving…" : "Resolve and send the reply"}
                </button>
              </div>
            </div>
          )}

          {c.status === "awaiting_approval" && held && (
            <div className="sd-decide sd-decide-form">
              <p className="sd-decide-text">A tool call is above its automatic limit. Approve it and it runs now; reject it with a reason and the case goes to a person.</p>
              <ToolCallView call={held} compact />
              <label htmlFor={reasonId} className="sd-label">
                Reason (needed to reject; kept on the case)
              </label>
              <textarea
                id={reasonId}
                ref={reasonRef}
                className="sd-reply"
                rows={1}
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
              <div className="sd-buttons">
                <button type="button" className="btn good" onClick={() => approve(held)} aria-disabled={busy ? true : undefined}>
                  {busy === "approve" ? "Approving…" : "Approve and run it"}
                </button>
                <button type="button" className="btn danger" onClick={() => reject(held)} aria-disabled={busy ? true : undefined}>
                  {busy === "reject" ? "Rejecting…" : "Reject"}
                </button>
              </div>
            </div>
          )}

          {c.status === "awaiting_approval" && !held && (
            <p className="sd-decide-text">The held tool call is no longer on the case. Refresh to see where it stands.</p>
          )}

          {c.status === "resolved" && <Receipt text={v.text} when={detail.steps.find((s) => s.action === "resolved")?.at} />}
          {c.status === "action_taken" && detail.tool_calls.some((t) => t.decided_by) && (
            <Receipt
              text={`Approved by ${detail.tool_calls.find((t) => t.decided_by)?.decided_by}; the tool ran`}
              when={detail.tool_calls.find((t) => t.decided_by)?.at}
            />
          )}
        </footer>
      )}
    </article>
  );
}

function Receipt({ text, when }: { text: string; when?: string }) {
  return (
    <p className="sd-receipt">
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
