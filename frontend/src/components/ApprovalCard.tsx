import { Fragment, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { humanStatus, priorityTitle } from "../lib/agents";
import { IconCheck, IconDot } from "../lib/icons";
import { fmtDateTime } from "../lib/time";
import {
  ageMinutes,
  channelsFor,
  customerUpdateTitle,
  extraEntries,
  factsFor,
  fmtAge,
  labelFor,
  ladderBreached,
  possibleOutageTitle,
  rawPayload,
  renderingSource,
  specFor,
} from "../lib/hitl";
import { hitlSubject, hitlSubjectIsIncident } from "../lib/hitlSubject";
import { CustomerUpdateBody, PossibleOutageBody } from "./LoopCardBodies";

/**
 * The side-by-side approval card.
 *
 * THE OPERATIONAL POINT (spec §6.5): "the inbox card shows every rendering side
 * by side (SMS with segment count and encoding, email subject/body, WhatsApp
 * template + params, in-app) so the approver sees exactly what leaves."
 *
 * A supervisor approving a P1 broadcast at 03:00 gets the incident facts that
 * justify the decision, the SMS and the email next to each other, and the
 * decision controls. The footer (reason, Claim / Approve / Reject) is sticky to
 * the bottom of the viewport inside its card, and the channel bodies cap at
 * 11rem until "Full text", so the controls stay on screen while the drafts are
 * read. Each fact is said once (no MSP that equals the owner, no "HUB major" or
 * status line, no task-fields table repeating the payload), so on the first
 * card at 1280 x 650 the SMS body sits above the footer. A failed request shows
 * in that footer, next to the button that caused it.
 *
 * "Claimed by you" is said once, by the head chip: once a card is claimed the
 * Claim button goes (it could only be disabled) and the page does not toast it. Its slot
 * stays reserved, so the reason box keeps its width.
 *
 * DECIDING: Approve and Reject are never disabled buttons. Pressed with an empty reason they
 * say "Add a reason; it is kept in the Audit trail" under the box, mark it `aria-invalid` and put
 * focus in it. The button that was pressed says what is happening ("Approving…"); the others
 * are `aria-disabled` meanwhile, so focus never drops to <body>. Once decided, the footer
 * shows the receipt ("Approved by NOC Analyst" and what that did) where the buttons were and
 * takes focus; the page collapses the card a moment later (pages/HitlInbox.tsx).
 *
 * DECIDED ELSEWHERE: when another session decided the card first, the page hands it the same
 * receipt worded for that ("Approved by Ian in another session, 15:24"), with the reason typed
 * here kept under it and a Copy button, since it was never recorded. The reason itself is held
 * by the page (`initialReason` / `onReasonChange`), so collapsing the card to its queue row and
 * opening it again keeps what was typed.
 *
 * DEGRADATION RULES — nothing here may blank the inbox:
 *  - unknown `task_type` → `specFor` returns the fallback spec and the card
 *    still renders, labelled "not recognised", with every payload field listed
 *    and the raw payload open;
 *  - missing / malformed `proposed_payload` → the channel list is empty and the
 *    card says so instead of throwing;
 *  - `incident` absent (the facts fetch failed, or the incident is filtered out)
 *    → facts fall back to what the task row itself carries;
 *  - every API error is caught by the parent and shown on the card.
 */

/**
 * §6.5: approve needs a non-empty reason only when
 * `HITL_APPROVE_REASON_REQUIRED=true` on the server (default false) — "and the
 * inbox card makes the field mandatory in the UI regardless". That is what this
 * does. Flip this one constant to `false` if the demo needs one-click approval;
 * the per-type requirement below (v2 task types, §6.5) is unconditional and is
 * not affected.
 */
const APPROVE_REASON_REQUIRED_IN_UI = true;

const REJECT_REASONS = ["wording", "wrong vendor", "late", "facts not verified"];

/** The request in flight for this card, if any; each button says its own. */
export type CardAction = "claim" | "approve" | "reject";

const BUSY_LABEL: Record<CardAction, string> = { claim: "Claiming…", approve: "Approving…", reject: "Rejecting…" };

/** A decided card's footer: who decided ("Approved by NOC Analyst") and what that did. */
export interface CardReceipt {
  headline: string;
  effect: string;
  /** Approved: the check in the done colour. */
  approved: boolean;
  /** Decided in another session; the page keeps this receipt longer (pages/HitlInbox.tsx). */
  elsewhere?: boolean;
  /** The reason typed here that the other session's decision made moot: shown with Copy. */
  lostReason?: string;
}

/**
 * Copies `text`, falling back to selecting `el` (and the legacy copy command) where the
 * Clipboard API is missing or refused. "select" means the text is selected for Ctrl+C.
 */
async function copyText(text: string, el: HTMLElement | null): Promise<"done" | "select"> {
  try {
    if (!navigator.clipboard) throw new Error("no clipboard");
    await navigator.clipboard.writeText(text);
    return "done";
  } catch {
    const sel = window.getSelection();
    if (!el || !sel) return "select";
    const range = document.createRange();
    range.selectNodeContents(el);
    sel.removeAllRanges();
    sel.addRange(range);
    try {
      return document.execCommand("copy") ? "done" : "select";
    } catch {
      return "select";
    }
  }
}

/** The reason typed on a card another session decided first: kept on screen, with Copy. */
function LostReason({ text }: { text: string }) {
  const textRef = useRef<HTMLSpanElement>(null);
  const [copied, setCopied] = useState<"" | "done" | "select">("");
  return (
    <span className="hitl-lost">
      <span className="hitl-lost-label">Your reason, not recorded:</span>
      <span className="hitl-lost-text" ref={textRef}>
        {text}
      </span>
      <button type="button" className="btn sm" onClick={async () => setCopied(await copyText(text, textRef.current))}>
        {copied === "done" ? "Copied" : "Copy"}
      </button>
      {copied === "select" && <span className="hitl-lost-hint">Selected. Press Ctrl+C to copy.</span>}
    </span>
  );
}

/** The receipt's words: who decided and what that did, and a reason this tab could not record.
 *  Shared by the card footer and the one-line queue row (pages/HitlInbox.tsx). */
export function ReceiptBody({ receipt }: { receipt: CardReceipt }) {
  return (
    <>
      <span className={receipt.approved ? "hitl-receipt-verb ok" : "hitl-receipt-verb"}>
        <IconCheck />
        {receipt.headline}
      </span>
      {receipt.effect && <span className="hitl-receipt-effect">{receipt.effect}</span>}
      {receipt.lostReason && <LostReason text={receipt.lostReason} />}
    </>
  );
}

const REASON_NEEDED = "Add a reason; it is kept in the Audit trail";

export interface ApprovalCardProps {
  task: any;
  /** Full incident row when the board fetch succeeded; `null` is fine. */
  incident?: any;
  /** The operator profile, for region names; optional. */
  profile?: any;
  /** Display name of the signed-in operator. */
  who: string;
  /** The request in flight for this task, or `null`. */
  busy: CardAction | null;
  /** Already-friendly error text for this task, or `""`. */
  error: string;
  /** Raw error text, shown as a tooltip so nothing is hidden. */
  errorDetail?: string;
  /** Set once the card is decided: the footer shows the receipt instead of the controls. */
  receipt?: CardReceipt | null;
  /** What was typed in the reason box before this card was last collapsed. */
  initialReason?: string;
  /** Every edit of the reason box, so the page can keep it while the card is collapsed. */
  onReasonChange?: (reason: string) => void;
  onClaim: () => void;
  onApprove: (reason: string) => void;
  onReject: (reason: string) => void;
}

export default function ApprovalCard({
  task,
  incident,
  profile,
  who,
  busy,
  error,
  errorDetail,
  receipt = null,
  initialReason = "",
  onReasonChange,
  onClaim,
  onApprove,
  onReject,
}: ApprovalCardProps) {
  const [reason, setReasonState] = useState(initialReason);
  // Which quick reasons are on the table: the reject ones once Reject has been pressed without a
  // reason, the approve ones (a card type that has them) once Approve has. Never the reject reasons
  // after an Approve: they would put "not restored yet" one tap from an approval.
  const [armed, setArmed] = useState<"" | "approve" | "reject">("");
  const rejectArmed = armed === "reject";
  const setReason = (value: string) => {
    setReasonState(value);
    onReasonChange?.(value);
  };
  const [expanded, setExpanded] = useState(false);
  // Approve or Reject pressed with an empty reason: the box says so and takes focus.
  const [reasonMissing, setReasonMissing] = useState(false);

  const t = task && typeof task === "object" ? task : {};
  const payload = t.proposed_payload;

  const spec = specFor(t.task_type);
  const label = labelFor(t.task_type);
  // The two close-the-loop cards (docs/CLOSE_THE_LOOP.md §5) draw their own body; the head, the
  // effect line, the raw payload and the decision footer stay this card's.
  const loopKind = t.task_type === "APPROVE_CUSTOMER_UPDATE" ? "update" : t.task_type === "CONFIRM_POSSIBLE_OUTAGE" ? "surge" : null;
  const channels = useMemo(() => (loopKind ? [] : channelsFor(payload)), [payload, loopKind]);
  const facts = useMemo(() => (loopKind ? [] : factsFor(t, incident)), [t, incident, loopKind]);
  const entries = useMemo(() => (loopKind ? [] : extraEntries(t, facts)), [t, facts, loopKind]);
  const source = renderingSource(payload);
  const heading =
    loopKind === "update" ? customerUpdateTitle(payload) : loopKind === "surge" ? possibleOutageTitle(payload) : hitlSubject(t);
  const effect = spec.effectFor ? spec.effectFor(payload) : spec.effect;
  const approveReasons = spec.approveReasons ?? [];
  const approveLabel = spec.approveLabel ? spec.approveLabel(payload) : "Approve";
  const rejectLabel = spec.rejectLabel ?? "Reject";
  const rejectReasons = spec.rejectReasons ?? REJECT_REASONS;
  const busyLabel = (a: CardAction) =>
    a === "approve" && spec.approveBusy ? spec.approveBusy : a === "reject" && spec.rejectBusy ? spec.rejectBusy : BUSY_LABEL[a];

  const headingId = useId();
  const mins = ageMinutes(t.created_at);
  const late = ladderBreached(mins, t.priority, t.claimed_by);
  // No pill on a card without a priority (a possible outage, a maintenance window): "P4" would be a guess.
  const priority = typeof t.priority === "string" && /^P[1-4]$/.test(t.priority) ? t.priority : "";
  const claimed = typeof t.claimed_by === "string" && t.claimed_by ? t.claimed_by : "";
  const claimedByMe = claimed && claimed === who;
  // When the claim lands the Claim button goes away; keyboard focus moves to the reason box.
  const reasonRef = useRef<HTMLTextAreaElement>(null);
  const wasMine = useRef(Boolean(claimedByMe));
  useEffect(() => {
    if (claimedByMe && !wasMine.current) reasonRef.current?.focus();
    wasMine.current = Boolean(claimedByMe);
  }, [claimedByMe]);
  // `/hitl/pending` returns PENDING and CLAIMED rows; the claim chip already says which.
  // Anything else is unexpected enough to print, as plain text.
  const oddStatus =
    typeof t.status === "string" && t.status && t.status !== "PENDING" && t.status !== "CLAIMED"
      ? humanStatus(t.status)
      : "";

  const trimmed = reason.trim();
  const approveNeedsReason = spec.reasonRequired || APPROVE_REASON_REQUIRED_IN_UI;
  const reasonMsgId = `${headingId}-reason`;
  const reasonId = `${headingId}-reason-box`;

  // The decision lands where the eye is: the receipt replaces the buttons in the sticky footer
  // and takes focus before the buttons leave, so focus never falls to <body>.
  // Once: a receipt whose words are filled in later (who decided elsewhere) does not take focus
  // back from the Copy button.
  const receiptRef = useRef<HTMLDivElement>(null);
  const hadReceipt = useRef(false);

  // The sticky decision footer covers the bottom of the viewport: everything focusable above it
  // keeps that much clear when focus scrolls it into view (`--hitl-foot`, HitlInbox.css).
  const cardRef = useRef<HTMLElement>(null);
  const footRef = useRef<HTMLElement>(null);
  useLayoutEffect(() => {
    const card = cardRef.current;
    const foot = footRef.current;
    if (!card || !foot || typeof ResizeObserver !== "function") return;
    const set = () => card.style.setProperty("--hitl-foot", `${Math.ceil(foot.getBoundingClientRect().height) + 12}px`);
    set();
    const ro = new ResizeObserver(set);
    ro.observe(foot);
    // Chromium does not apply scroll-margin when Tab moves focus to an element that is already
    // "in view" under a sticky bar, so a focused SMS box could sit behind the footer. Lift it clear.
    const lift = (e: FocusEvent) => {
      const el = e.target as HTMLElement | null;
      if (!el || foot.contains(el)) return;
      const r = el.getBoundingClientRect();
      const f = foot.getBoundingClientRect();
      const over = r.bottom - (f.top - 12);
      if (over > 0 && r.top - over >= 0) window.scrollBy({ top: over, left: 0, behavior: "instant" as ScrollBehavior });
    };
    card.addEventListener("focusin", lift);
    return () => {
      ro.disconnect();
      card.removeEventListener("focusin", lift);
    };
  }, []);
  useLayoutEffect(() => {
    if (receipt && !hadReceipt.current) receiptRef.current?.focus({ preventScroll: true });
    hadReceipt.current = Boolean(receipt);
  }, [receipt]);

  /** An empty reason is said beside the box, never by greying out the button. */
  const needReason = (): boolean => {
    setReasonMissing(true);
    reasonRef.current?.focus();
    return false;
  };
  const approve = () => {
    if (busy || receipt) return;
    if (approveNeedsReason && !trimmed) {
      setArmed("approve");
      return void needReason();
    }
    onApprove(trimmed);
  };
  const reject = () => {
    if (busy || receipt) return;
    if (!trimmed) {
      setArmed("reject");
      return void needReason(); // the API 400s on an empty reason
    }
    onReject(trimmed);
  };
  const quick = rejectArmed ? rejectReasons : armed === "approve" ? approveReasons : [];
  const claim = () => {
    if (busy || receipt) return;
    onClaim();
  };

  return (
    <article className="hitl-card" aria-labelledby={headingId} ref={cardRef}>
      <header className="hitl-card-head">
        <span className="hitl-title">
          {priority && (
            <span className={`pill ${priority}`} title={priorityTitle(priority)}>
              {priority}
            </span>
          )}
          {/* Since v8 a maintenance card has no incident; say what it IS about (lib/hitlSubject).
              An incident number is an identifier (mono); a maintenance heading is prose, and so
              is a close-the-loop card's, which says the decision in words. */}
          {/* Focusable from script only: after a failed claim, or when the card above it is decided. */}
          <h2 id={headingId} className={!loopKind && hitlSubjectIsIncident(t) ? "hitl-inc" : "hitl-subject"} tabIndex={-1}>
            {heading}
          </h2>
        </span>
        <span className="hitl-type" title={typeof t.task_type === "string" ? t.task_type : "no task_type"}>
          {label}
        </span>
        {!spec.known && (
          <span
            className="chip warn"
            title="This build has no layout for this kind of card. Everything the backend sent is shown below, unchanged."
          >
            type not recognised
          </span>
        )}
        {claimed ? (
          <span className={claimedByMe ? "chip hitl" : "chip"}>claimed by {claimedByMe ? "you" : claimed}</span>
        ) : (
          <span className="chip">unclaimed</span>
        )}
        {oddStatus && <span className="hitl-type">{oddStatus}</span>}
        <span
          className={late ? "hitl-age late" : "hitl-age"}
          title="Escalation ladder (off in this demo): a P1 or P2 still unclaimed at 5 minutes nudges the shift supervisor, at 15 the duty manager, and at 30 it shows red on the Wallboard."
        >
          {fmtAge(mins)}
        </span>
        <span className="hitl-created">raised {fmtDateTime(t.created_at)}</span>
      </header>

      {/* The check sentence sits with what it is about: the channel heading when there are
          channels to read, else under the effect. */}
      <p className="hitl-effect">
        {effect}
        {spec.check && channels.length === 0 && !loopKind && <span className="hitl-check">{spec.check}</span>}
      </p>

      {loopKind === "update" && <CustomerUpdateBody task={t} headingId={headingId} check={spec.check} />}
      {loopKind === "surge" && <PossibleOutageBody task={t} headingId={headingId} check={spec.check} profile={profile} />}

      {facts.length > 0 && (
        <dl className="hitl-facts">
          {facts.map((f) => (
            <div key={f.label} className={f.wide ? "hitl-fact wide" : "hitl-fact"}>
              <dt>{f.label}</dt>
              <dd
                className={
                  [f.attention ? `attn ${f.attention}` : "", f.mono ? "mono" : ""].filter(Boolean).join(" ") || undefined
                }
              >
                {f.attention && <IconDot />}
                {f.value}
              </dd>
            </div>
          ))}
        </dl>
      )}

      {channels.length > 0 && (
        <div role="group" className="hitl-out" aria-labelledby={`${headingId}-out`}>
          <div className="hitl-section-head">
            <h3 id={`${headingId}-out`}>What goes out</h3>
            {spec.check && <span className="hitl-check-inline">{spec.check}</span>}
            <span
              title={
                source === "envelope"
                  ? "Rendered by the backend, character for character as it will be sent."
                  : "A draft written before the final rendering. The segment counts below are estimated in the browser."
              }
            >
              {source === "envelope" ? "Exact text as sent" : "Draft; counts estimated here"}
            </span>
            <button
              type="button"
              className="btn sm hitl-expand"
              aria-expanded={expanded}
              onClick={() => setExpanded((v) => !v)}
            >
              {expanded ? "Collapse" : "Full text"}
            </button>
          </div>
          <div className="hitl-channels">
            {channels.map((c) => (
              <div role="group" key={c.key} className="hitl-channel" aria-label={c.label}>
                <div className="hitl-channel-head">
                  <span className="hitl-channel-name">{c.label}</span>
                  {c.meta.map((m, i) => (
                    <span key={`${c.key}-m${i}`}>{m}</span>
                  ))}
                </div>
                {c.heading && <div className="hitl-channel-subject">{c.heading}</div>}
                {/* Focusable so a capped body can be scrolled from the keyboard. */}
                <pre
                  className={`pre hitl-channel-body${expanded ? " full" : ""}`}
                  tabIndex={0}
                  data-keep-tab=""
                  aria-label={`${c.label} text`}
                >
                  {c.text || "(no body, heading only)"}
                </pre>
              </div>
            ))}
          </div>
        </div>
      )}

      {channels.length === 0 && spec.channels && (
        <div className="hitl-nochannel">
          No message text on this card; decide from the facts above and the payload below.
          {t.task_type === "APPROVE_BROADCAST" &&
            " Approving still releases any held messages for sending, so if you cannot see what they say, reject."}
        </div>
      )}

      {/* Payload fields the card does not show elsewhere: none for a broadcast; a GENERIC
          escalation's reason, detail and suggested action; every field of an unrecognised type. */}
      {entries.length > 0 && (
        <dl className="rail-dl hitl-extra">
          {entries.map((e) => (
            <Fragment key={e.key}>
              <dt>{e.label}</dt>
              <dd>
                {e.long ? (
                  <pre className="pre hitl-field-pre" tabIndex={0} data-keep-tab="">
                    {e.value}
                  </pre>
                ) : (
                  e.value
                )}
              </dd>
            </Fragment>
          ))}
        </dl>
      )}

      {/* An unrecognised type opens it: "every field the backend sent, unchanged". */}
      <details className="hitl-raw" open={!spec.known}>
        <summary>Raw proposed payload</summary>
        <pre className="pre hitl-field-pre" tabIndex={0} data-keep-tab="">
          {rawPayload(payload)}
        </pre>
      </details>

      {/* Sticky to the bottom of the viewport inside the card, so the decision stays in reach
          while the channels are read. The error block sits here, beside the buttons; once the
          card is decided the receipt takes the controls' place, at the controls' height. */}
      <footer className={"hitl-actions" + (receipt ? " decided" : "")} ref={footRef}>
        <div className="hitl-controls" aria-hidden={receipt ? true : undefined}>
          {error && (
            <div className="hitl-error" role="alert" title={errorDetail || error}>
              {error}
            </div>
          )}
          {/* A visible label, not a placeholder doing its job: it stays while the reason is typed. */}
          <label htmlFor={reasonId} className="hitl-reason-label">
            {approveNeedsReason ? "Reason (kept in the Audit trail)" : "Reason (needed to reject; kept in the Audit trail)"}
          </label>
          <div className="hitl-decide">
            <div className="hitl-reason-wrap">
              {/* One line that grows with what is typed, so the footer leaves the drafts in view. */}
              <textarea
                id={reasonId}
                ref={reasonRef}
                className="hitl-reason"
                value={reason}
                onChange={(e) => {
                  setReason(e.target.value);
                  if (reasonMissing && e.target.value.trim()) setReasonMissing(false);
                }}
                aria-invalid={reasonMissing || undefined}
                aria-describedby={reasonMissing ? reasonMsgId : undefined}
                rows={1}
                tabIndex={receipt ? -1 : undefined}
              />
              {reasonMissing && (
                <p id={reasonMsgId} className="hitl-invalid">
                  {REASON_NEEDED}
                </p>
              )}
            </div>
            {/* Three fixed slots: Claim's stays reserved once the card is claimed, and a busy
                label fits its button, so the reason box never changes width. */}
            <div className={"hitl-buttons" + (spec.approveLabel || spec.rejectLabel ? " wide" : "")}>
              {/* The head chip already says who holds a claimed card; the button only exists to claim. */}
              {!claimed && (
                <button
                  type="button"
                  className="btn hitl-claim"
                  onClick={claim}
                  aria-disabled={busy ? true : undefined}
                  tabIndex={receipt ? -1 : undefined}
                >
                  {busy === "claim" ? busyLabel("claim") : "Claim"}
                </button>
              )}
              <button
                type="button"
                className="btn primary hitl-approve"
                onClick={approve}
                aria-disabled={busy ? true : undefined}
                title={effect}
                tabIndex={receipt ? -1 : undefined}
              >
                {busy === "approve" ? busyLabel("approve") : approveLabel}
              </button>
              <button
                type="button"
                className="btn danger hitl-reject"
                onClick={reject}
                aria-disabled={busy ? true : undefined}
                title={spec.rejectTitle ?? (loopKind === "surge" ? "Dismiss: the complaints stay as they were" : "Suppress the drafts and cancel")}
                tabIndex={receipt ? -1 : undefined}
              >
                {busy === "reject" ? busyLabel("reject") : rejectLabel}
              </button>
            </div>
          </div>
          {(quick.length > 0 || (rejectArmed && spec.rejectTitle) || !claimed) && (
            <div className="hitl-reason-quick">
              {/* Picking a reason fills the box, it does not decide. Reject reasons show once Reject
                  was pressed without a reason; approve reasons once Approve was (a type that has them). */}
              {rejectArmed && spec.rejectTitle && <span className="hitl-reject-means">{spec.rejectTitle}</span>}
              {quick.length > 0 && (
                <>
                  <span className="hitl-quick-label" id={`${headingId}-quick`}>
                    {rejectArmed ? rejectLabel : "Approve"} because
                  </span>
                  <span className="hitl-quick-list" role="group" aria-labelledby={`${headingId}-quick`}>
                    {quick.map((r) => (
                      <button
                        key={r}
                        type="button"
                        className="chip hitl-quick"
                        onClick={() => {
                          setReason(r);
                          setReasonMissing(false);
                        }}
                        title="Put this reason in the box"
                        tabIndex={receipt ? -1 : undefined}
                      >
                        {r}
                      </button>
                    ))}
                  </span>
                </>
              )}
              {!claimed && <span className="hitl-hint">Claim first so two people do not act on the same card.</span>}
            </div>
          )}
        </div>
        {receipt && (
          <div className="hitl-receipt" tabIndex={-1} ref={receiptRef}>
            <ReceiptBody receipt={receipt} />
          </div>
        )}
      </footer>
    </article>
  );
}
