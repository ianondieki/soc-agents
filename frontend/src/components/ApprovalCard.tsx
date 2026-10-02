import { Fragment, useEffect, useId, useMemo, useRef, useState } from "react";
import { humanStatus } from "../lib/agents";
import { IconDot } from "../lib/icons";
import { fmtDateTime } from "../lib/time";
import {
  ageMinutes,
  channelsFor,
  extraEntries,
  factsFor,
  fmtAge,
  labelFor,
  ladderBreached,
  rawPayload,
  renderingSource,
  specFor,
} from "../lib/hitl";
import { hitlSubject, hitlSubjectIsIncident } from "../lib/hitlSubject";

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
 * Claim button goes (it could only be disabled) and the page does not toast it.
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

const REJECT_REASONS = ["wording", "wrong MSP", "late", "facts not verified"];

export interface ApprovalCardProps {
  task: any;
  /** Full incident row when the board fetch succeeded; `null` is fine. */
  incident?: any;
  /** Display name of the signed-in operator. */
  who: string;
  /** A request for this task is in flight. */
  busy: boolean;
  /** Already-friendly error text for this task, or `""`. */
  error: string;
  /** Raw error text, shown as a tooltip so nothing is hidden. */
  errorDetail?: string;
  onClaim: () => void;
  onApprove: (reason: string) => void;
  onReject: (reason: string) => void;
}

export default function ApprovalCard({
  task,
  incident,
  who,
  busy,
  error,
  errorDetail,
  onClaim,
  onApprove,
  onReject,
}: ApprovalCardProps) {
  const [reason, setReason] = useState("");
  const [expanded, setExpanded] = useState(false);

  const t = task && typeof task === "object" ? task : {};
  const payload = t.proposed_payload;

  const spec = specFor(t.task_type);
  const label = labelFor(t.task_type);
  const channels = useMemo(() => channelsFor(payload), [payload]);
  const facts = useMemo(() => factsFor(t, incident), [t, incident]);
  const entries = useMemo(() => extraEntries(t, facts), [t, facts]);
  const source = renderingSource(payload);

  const headingId = useId();
  const mins = ageMinutes(t.created_at);
  const late = ladderBreached(mins, t.priority, t.claimed_by);
  const priority = typeof t.priority === "string" && t.priority ? t.priority : "P4";
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
  const canApprove = !busy && (!approveNeedsReason || trimmed.length > 0);
  const canReject = !busy && trimmed.length > 0; // the API 400s on an empty reason

  return (
    <article className="hitl-card" aria-labelledby={headingId}>
      <header className="hitl-card-head">
        <span className="hitl-title">
          <span className={`pill ${priority}`}>{priority}</span>
          {/* Since v8 a maintenance card has no incident; say what it IS about (lib/hitlSubject).
              An incident number is an identifier (mono); a maintenance heading is prose. */}
          <h2 id={headingId} className={hitlSubjectIsIncident(t) ? "hitl-inc" : "hitl-subject"}>
            {hitlSubject(t)}
          </h2>
        </span>
        <span className="hitl-type" title={typeof t.task_type === "string" ? t.task_type : "no task_type"}>
          {label}
        </span>
        {!spec.known && (
          <span
            className="chip warn"
            title="This build has no card for that task_type. Everything the backend sent is shown below, unchanged."
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
          title="Escalation ladder: a P1 or P2 still unclaimed at 5 minutes nudges the on-duty supervisor, at 15 the duty manager, and at 30 it shows red on the Wallboard."
        >
          {fmtAge(mins)}
        </span>
        <span className="hitl-created">raised {fmtDateTime(t.created_at)}</span>
      </header>

      {/* The check sentence sits with what it is about: the channel heading when there are
          channels to read, else under the effect. */}
      <p className="hitl-effect">
        {spec.effect}
        {spec.check && channels.length === 0 && <span className="hitl-check">{spec.check}</span>}
      </p>

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
                  ? "Rendered from the canonical alert envelope."
                  : "Composed draft (pre-envelope). Segment counts below are computed in the browser, not by the backend renderer."
              }
            >
              {source === "envelope" ? "Envelope rendering" : "Draft rendering"}
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
          No rendered message text on this task; decide from the facts above and the payload below.
          {t.task_type === "APPROVE_BROADCAST" &&
            " Approving still releases any held broadcast rows, so if you cannot see what they say, reject."}
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
          while the channels are read. The error block sits here, beside the buttons. */}
      <footer className="hitl-actions">
        {error && (
          <div className="hitl-error" role="alert" title={errorDetail || error}>
            {error}
          </div>
        )}
        <div className="hitl-decide">
          {/* One line that grows with what is typed, so the footer leaves the drafts in view. */}
          <textarea
            ref={reasonRef}
            className="hitl-reason"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            aria-label="Decision reason"
            placeholder={
              approveNeedsReason
                ? "Decision reason — required, goes on the audit row"
                : "Decision reason (required to reject)"
            }
            rows={1}
          />
          <div className="hitl-buttons">
            {/* The head chip already says who holds a claimed card; the button only exists to claim. */}
            {!claimed && (
              <button className="btn" onClick={onClaim} disabled={busy}>
                Claim
              </button>
            )}
            <button
              className="btn good"
              onClick={() => onApprove(trimmed)}
              disabled={!canApprove}
              title={
                canApprove
                  ? spec.effect
                  : "A reason is required before approving — it is recorded on the audit row."
              }
            >
              {busy ? "Working…" : "Approve"}
            </button>
            <button
              className="btn danger"
              onClick={() => onReject(trimmed)}
              disabled={!canReject}
              title={canReject ? "Suppress the drafts and cancel" : "Reject requires a reason."}
            >
              Reject
            </button>
          </div>
        </div>
        <div className="hitl-reason-quick">
          {REJECT_REASONS.map((r) => (
            <button
              key={r}
              type="button"
              className="chip hitl-quick"
              onClick={() => setReason(r)}
              title="Fill the reason box"
            >
              {r}
            </button>
          ))}
          {!claimed && <span className="hitl-hint">Claim first so two supervisors do not both act.</span>}
        </div>
      </footer>
    </article>
  );
}
