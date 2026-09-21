import { useMemo, useState } from "react";
import { fmtDateTime } from "../lib/time";
import {
  ageMinutes,
  ageTone,
  channelsFor,
  factsFor,
  fmtAge,
  labelFor,
  payloadEntries,
  rawPayload,
  renderingSource,
  specFor,
} from "../lib/hitl";
import { hitlSubject } from "../lib/hitlSubject";

/**
 * The side-by-side approval card.
 *
 * THE OPERATIONAL POINT (spec §6.5): "the inbox card shows every rendering side
 * by side (SMS with segment count and encoding, email subject/body, WhatsApp
 * template + params, in-app) so the approver sees exactly what leaves."
 *
 * A supervisor approving a P1 broadcast at 03:00 gets, above the fold and
 * without scrolling the page: the incident facts that justify the decision, the
 * SMS and the email next to each other, and the decision controls. The card
 * bodies scroll inside themselves precisely so that the Approve / Reject bar can
 * never be pushed off the bottom of the screen by a long email.
 *
 * DEGRADATION RULES — nothing here may blank the inbox:
 *  - unknown `task_type` → `specFor` returns the fallback spec and the card
 *    still renders, labelled "not recognised", with every payload field shown;
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
  const entries = useMemo(() => payloadEntries(payload), [payload]);
  const source = renderingSource(payload);

  const mins = ageMinutes(t.created_at);
  const tone = ageTone(mins);
  const priority = typeof t.priority === "string" && t.priority ? t.priority : "P4";
  const claimed = typeof t.claimed_by === "string" && t.claimed_by ? t.claimed_by : "";
  const claimedByMe = claimed && claimed === who;

  const trimmed = reason.trim();
  const approveNeedsReason = spec.reasonRequired || APPROVE_REASON_REQUIRED_IN_UI;
  const canApprove = !busy && (!approveNeedsReason || trimmed.length > 0);
  const canReject = !busy && trimmed.length > 0; // the API 400s on an empty reason

  return (
    <article className={`hitl-card ${priority}`}>
      <header className="hitl-card-head">
        <span className={`pill ${priority}`}>{priority}</span>
        {/* Since v8 a maintenance card has no incident; say what it IS about (lib/hitlSubject). */}
        <strong className="hitl-inc">{hitlSubject(t)}</strong>
        <span className="chip hitl" title={typeof t.task_type === "string" ? t.task_type : "no task_type"}>
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
        <span className="chip">{t.status || "PENDING"}</span>
        {claimed ? (
          <span className="chip ok">claimed: {claimedByMe ? "you" : claimed}</span>
        ) : (
          <span className="chip">unclaimed</span>
        )}
        <span
          className={`chip ${tone}`}
          title="§6.5 escalation ladder: a P1/P2 task unclaimed at T+5 nudges the on-duty supervisor, T+15 the duty manager, T+30 shows red on the Wallboard."
        >
          {fmtAge(mins)}
        </span>
        <span className="muted dim hitl-created">raised {fmtDateTime(t.created_at)}</span>
      </header>

      <p className="muted hitl-effect">
        <strong>{spec.effect}</strong> {spec.check}
      </p>

      {facts.length > 0 && (
        <div className="hitl-facts">
          {facts.map((f) => (
            <span key={f.label} className={`hitl-fact ${f.tone || ""}`}>
              <span className="hitl-fact-label">{f.label}</span>
              <span className="hitl-fact-value">{f.value}</span>
            </span>
          ))}
        </div>
      )}

      {channels.length > 0 && (
        <>
          <div className="hitl-section-head">
            <h4>What goes out — {channels.length} channel{channels.length === 1 ? "" : "s"}</h4>
            <span
              className={`chip ${source === "envelope" ? "ok" : ""}`}
              title={
                source === "envelope"
                  ? "Rendered from the canonical alert envelope."
                  : "Composed draft (pre-envelope). Segment counts below are computed in the browser, not by the backend renderer."
              }
            >
              {source === "envelope" ? "envelope rendering" : "draft rendering"}
            </span>
            <button type="button" className="btn hitl-expand" onClick={() => setExpanded((v) => !v)}>
              {expanded ? "Collapse" : "Full text"}
            </button>
          </div>
          <div className="hitl-channels">
            {channels.map((c) => (
              <section key={c.key} className="hitl-channel">
                <div className="hitl-channel-head">
                  <span className="hitl-channel-name">{c.label}</span>
                  {c.meta.map((m, i) => (
                    <span key={`${c.key}-m${i}`} className="chip">
                      {m}
                    </span>
                  ))}
                </div>
                {c.heading && <div className="hitl-channel-subject">{c.heading}</div>}
                <pre className={`pre hitl-channel-body${expanded ? " full" : ""}`}>
                  {c.text || "(no body — heading only)"}
                </pre>
              </section>
            ))}
          </div>
        </>
      )}

      {channels.length === 0 && spec.channels && (
        <div className="hitl-nochannel">
          No rendered message text on this task — decide from the fields below.
          {t.task_type === "APPROVE_BROADCAST" &&
            " Approving still releases any held broadcast rows, so if you cannot see what they say, reject."}
        </div>
      )}

      {entries.length > 0 && (
        <div className="hitl-fields">
          <h4>Task fields</h4>
          <table>
            <tbody>
              {entries.map((e) => (
                <tr key={e.key}>
                  <td className="muted hitl-field-key">{e.label}</td>
                  <td>{e.long ? <pre className="pre hitl-field-pre">{e.value}</pre> : e.value}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <details className="hitl-raw">
        <summary className="muted">Raw proposed payload</summary>
        <pre className="pre hitl-field-pre">{rawPayload(payload)}</pre>
      </details>

      {error && (
        <div className="hitl-error" title={errorDetail || error}>
          {error}
        </div>
      )}

      <footer className="hitl-actions">
        <textarea
          className="hitl-reason"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          placeholder={
            approveNeedsReason
              ? "Decision reason — required, goes on the audit row"
              : "Decision reason (required to reject)"
          }
          rows={2}
        />
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
        </div>
        <div className="hitl-buttons">
          <button className="btn" onClick={onClaim} disabled={busy || Boolean(claimed)}>
            {claimed ? (claimedByMe ? "Claimed by you" : `Held by ${claimed}`) : "Claim"}
          </button>
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
          {!claimed && (
            <span className="muted dim">Shared queue — claim first so two supervisors do not both act.</span>
          )}
        </div>
      </footer>
    </article>
  );
}
