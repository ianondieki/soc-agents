import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import PublicFrame from "../components/support/PublicFrame";
import {
  errorDetail,
  fmtClock,
  isNetworkError,
  isSupportApiError,
  msisdnProblem,
  statusOfError,
  supportApi,
  type Tracked,
} from "../lib/support";
import "./Track.css";

/**
 * "Track my complaint" at /track (docs/CLOSE_THE_LOOP.md §2), outside the console shell and the
 * sibling of /complain: the same slim header and footer, the same Kenyan-number check. Two
 * fields (the reference, prefilled from `?ref=`, and the phone number), one button, then a calm
 * status page built only from what the API returned: the headline large, the detail, the outage
 * strip, the timeline oldest first, the conversation, the reply-by time. When the API allows it,
 * one secondary action says service is still down.
 *
 * The phone number lives in memory only: never in the URL (the request is a POST for the same
 * reason), never in storage. The reference goes into `?ref=` so a reload keeps it.
 */

const NOT_FOUND = "We could not find a complaint with that reference and number.";

/** "cmp 123", "CMP000123" and "123" all mean CMP-000123; anything else is sent as typed. */
function normalizeRef(raw: string): string {
  const s = raw.trim().toUpperCase().replace(/\s+/g, "");
  const m = /^(?:CMP-?)?(\d{1,9})$/.exec(s);
  return m ? `CMP-${m[1].padStart(6, "0")}` : s;
}

function refProblem(raw: string): string | null {
  if (!raw.trim()) return "Enter the reference from your SMS or the complaint page.";
  if (!/^CMP-\d{6,9}$/.test(normalizeRef(raw))) return "A reference looks like CMP-000123.";
  return null;
}

function waitWords(e: unknown): string {
  const secs = isSupportApiError(e) ? e.retryAfter : null;
  const mins = secs ? Math.max(1, Math.ceil(secs / 60)) : null;
  return mins ? `about ${mins} ${mins === 1 ? "minute" : "minutes"}` : "a few minutes";
}

interface FormProblem {
  text: string;
  /** The 404's hint: where the reference is and which number to use. */
  hint?: string;
}

function trackProblem(e: unknown): FormProblem {
  const s = statusOfError(e);
  if (s === 404)
    return { text: NOT_FOUND, hint: "Check the reference in the SMS we sent you, or on the page you saw after sending, and use the phone number you complained from." };
  if (s === 429) return { text: `You have checked several times in a short time. Please wait ${waitWords(e)} and try again.` };
  if (s === 409 || s === 422 || s === 400) return { text: errorDetail(e, "Please check what you typed and try again.") };
  if (s != null && s >= 500) return { text: "Something failed on our side. Please try again in a moment." };
  if (isNetworkError(e)) return { text: "We couldn't reach the support desk. Check your connection and try again." };
  return { text: errorDetail(e, "We couldn't check your complaint. Please try again.") };
}

/** The still-down report's failure: the server's own sentence for a 409 ("already reported"…). */
function stillDownProblem(e: unknown): string {
  const s = statusOfError(e);
  if (s === 409) return errorDetail(e, "We can't take a still-down report on this complaint right now.");
  if (s === 429) return `You have sent several reports in a short time. Please wait ${waitWords(e)} and try again.`;
  if (s === 404) return NOT_FOUND;
  if (s != null && s >= 500) return "Something failed on our side; your report was not sent. Please try again in a moment.";
  if (isNetworkError(e)) return "We couldn't reach the support desk; your report was not sent. Check your connection and try again.";
  return errorDetail(e, "Your report was not sent. Please try again.");
}

type Errors = { ref?: string; msisdn?: string; form?: FormProblem };

export default function Track() {
  const [params, setParams] = useSearchParams();
  const [ref, setRef] = useState(() => normalizeRef(params.get("ref") ?? ""));
  const [msisdn, setMsisdn] = useState("");
  const [touchedPhone, setTouchedPhone] = useState(false);
  const [errors, setErrors] = useState<Errors>({});
  const [sending, setSending] = useState(false);
  const [result, setResult] = useState<Tracked | null>(null);
  /** The number the shown result was found with: the still-down report sends it again. */
  const [foundWith, setFoundWith] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const uid = useId();
  const refRef = useRef<HTMLInputElement>(null);
  const phoneRef = useRef<HTMLInputElement>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const formErrorRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    document.title = result ? `${result.ref}, Kenya NOC Support` : "Track a complaint, Kenya NOC Support";
    return () => {
      document.title = "Kenya NOC Mission Control";
    };
  }, [result]);

  // A reference from the link (the SMS, the complaint page) fills the box; the phone is next.
  useEffect(() => {
    if (params.get("ref")) phoneRef.current?.focus({ preventScroll: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (errors.form) formErrorRef.current?.focus({ preventScroll: false });
  }, [errors.form]);

  const phoneProblem = msisdnProblem(msisdn);

  const submit = async (ev: FormEvent) => {
    ev.preventDefault();
    if (sending) return;
    const next: Errors = {};
    const rp = refProblem(ref);
    if (rp) next.ref = rp;
    if (phoneProblem) next.msisdn = phoneProblem;
    setErrors(next);
    if (next.ref) return void refRef.current?.focus();
    if (next.msisdn) return void phoneRef.current?.focus();
    const cleanRef = normalizeRef(ref);
    setSending(true);
    try {
      const t = await supportApi.track(cleanRef, msisdn.trim());
      setRef(t.ref || cleanRef);
      setFoundWith(msisdn.trim());
      setConfirmation("");
      setResult(t);
      const nextParams = new URLSearchParams(params);
      nextParams.set("ref", t.ref || cleanRef);
      setParams(nextParams, { replace: true });
      window.scrollTo({ top: 0 });
      window.setTimeout(() => headingRef.current?.focus({ preventScroll: true }), 0);
    } catch (e) {
      setErrors({ form: trackProblem(e) });
    } finally {
      setSending(false);
    }
  };

  const another = () => {
    setResult(null);
    setConfirmation("");
    setErrors({});
    window.setTimeout(() => refRef.current?.focus(), 0);
  };

  return (
    <PublicFrame>
      {!result ? (
        <form className="cp-form" onSubmit={submit} noValidate>
          <h1 ref={headingRef} tabIndex={-1}>
            Track your complaint
          </h1>
          <p className="cp-lead">Enter the reference we gave you and the phone number you complained from. We show where your complaint is now.</p>

          <div className="cp-field">
            <label htmlFor={`${uid}-ref`}>Reference</label>
            <input
              id={`${uid}-ref`}
              ref={refRef}
              className="cp-mono tk-ref-input"
              value={ref}
              onChange={(e) => {
                setRef(e.target.value);
                if (errors.ref) setErrors((x) => ({ ...x, ref: undefined }));
              }}
              onBlur={() => ref.trim() && setRef(normalizeRef(ref))}
              autoComplete="off"
              autoCapitalize="characters"
              spellCheck={false}
              placeholder="CMP-000123"
              maxLength={24}
              aria-describedby={`${uid}-ref-hint${errors.ref ? ` ${uid}-ref-err` : ""}`}
              aria-invalid={!!errors.ref || undefined}
              required
            />
            <p id={`${uid}-ref-hint`} className="cp-hint">
              It is in the SMS we sent you and on the page you saw after sending.
            </p>
            {errors.ref && (
              <p id={`${uid}-ref-err`} className="cp-error">
                {errors.ref}
              </p>
            )}
          </div>

          <div className="cp-field">
            <label htmlFor={`${uid}-phone`}>Phone number</label>
            <input
              id={`${uid}-phone`}
              ref={phoneRef}
              value={msisdn}
              onChange={(e) => {
                setMsisdn(e.target.value);
                if (errors.msisdn) setErrors((x) => ({ ...x, msisdn: undefined }));
              }}
              onBlur={() => setTouchedPhone(true)}
              inputMode="tel"
              autoComplete="tel"
              placeholder="0712 345 678"
              aria-describedby={`${uid}-phone-hint${errors.msisdn || (touchedPhone && phoneProblem && msisdn) ? ` ${uid}-phone-err` : ""}`}
              aria-invalid={!!errors.msisdn || (touchedPhone && !!phoneProblem && !!msisdn) || undefined}
              required
            />
            <p id={`${uid}-phone-hint`} className="cp-hint">
              The number you complained from. It must match the reference.
            </p>
            {(errors.msisdn || (touchedPhone && phoneProblem && msisdn)) && (
              <p id={`${uid}-phone-err`} className="cp-error">
                {errors.msisdn || phoneProblem}
              </p>
            )}
          </div>

          <div className="cp-submit">
            <div className="cp-submit-row">
              <button type="submit" className="cp-btn primary" aria-disabled={sending || undefined} aria-busy={sending || undefined}>
                {sending ? "Checking…" : "Check my complaint"}
              </button>
              {errors.form && (
                <div className="cp-alert" role="alert" ref={formErrorRef} tabIndex={-1}>
                  {errors.form.text}
                  {errors.form.hint && <span className="tk-alert-hint">{errors.form.hint}</span>}
                </div>
              )}
            </div>
            <p className="cp-hint">
              No reference yet?{" "}
              <Link className="tk-link" to="/complain">
                Send a complaint
              </Link>
            </p>
          </div>
        </form>
      ) : (
        <Status
          t={result}
          msisdn={foundWith}
          headingRef={headingRef}
          confirmation={confirmation}
          onUpdated={(t, said) => {
            setResult(t);
            setConfirmation(said);
          }}
          onAnother={another}
        />
      )}
    </PublicFrame>
  );
}

/** Which colour the newest timeline entry carries: the stage, as a signal. */
function stageTone(stage: Tracked["stage"]): string {
  if (stage === "with_a_person") return " hitl";
  if (stage === "outage_known") return " warn";
  if (stage === "restored" || stage === "closed" || stage === "fixed") return " ok";
  return "";
}

interface StatusProps {
  t: Tracked;
  msisdn: string;
  headingRef: React.RefObject<HTMLHeadingElement>;
  confirmation: string;
  onUpdated: (t: Tracked, said: string) => void;
  onAnother: () => void;
}

function Status({ t, msisdn, headingRef, confirmation, onUpdated, onAnother }: StatusProps) {
  const uid = useId();
  const timeline = Array.isArray(t.timeline) ? t.timeline.filter((x) => x && x.text) : [];
  const messages = Array.isArray(t.messages) ? t.messages.filter((m) => m && m.body) : [];
  const received = fmtClock(t.received_at);
  // The reply-by time, unless the detail sentence already says it ("We will get back to you by 16:40").
  const dueAt = t.reply_due_at ? fmtClock(t.reply_due_at) : "";
  const due = dueAt && !(t.detail || "").includes(dueAt.slice(-5)) ? dueAt : "";
  const confirmRef = useRef<HTMLParagraphElement>(null);

  useEffect(() => {
    if (confirmation) {
      window.scrollTo({ top: 0 });
      window.setTimeout(() => confirmRef.current?.focus({ preventScroll: true }), 0);
    }
  }, [confirmation]);

  return (
    <div className="tk-result">
      {confirmation && (
        <p className="tk-confirm" ref={confirmRef} tabIndex={-1} role="status">
          {confirmation}
        </p>
      )}
      <p className="tk-ref">
        Complaint <span className="cp-mono tk-ref-num">{t.ref}</span>
        {received && (
          <span>
            , received <span className="cp-mono">{received}</span>
          </span>
        )}
      </p>
      <h1 ref={headingRef} tabIndex={-1} className="tk-headline">
        {t.headline}
      </h1>
      {t.detail && <p className="cp-lead tk-detail">{t.detail}</p>}
      {due && (
        <p className="tk-due">
          We will reply by <span className="cp-mono">{due}</span>.
        </p>
      )}

      {t.outage && (
        <div className={"tk-outage" + (t.outage.state === "restored" ? " ok" : " warn")}>
          <div className="tk-outage-cell">
            <span className="tk-outage-label">Outage</span>
            <span className="tk-outage-value">{t.outage.place}</span>
          </div>
          <div className="tk-outage-cell">
            <span className="tk-outage-label">Ticket</span>
            <span className="tk-outage-value cp-mono">{t.outage.ticket}</span>
          </div>
          <div className="tk-outage-cell">
            <span className="tk-outage-label">Now</span>
            <span className="tk-outage-value tk-outage-state">
              <span className="tk-dot" aria-hidden="true" />
              {t.outage.state === "restored" ? (
                t.outage.restored_at ? (
                  <span>
                    Restored at <span className="cp-mono">{fmtClock(t.outage.restored_at)}</span>
                  </span>
                ) : (
                  "Restored"
                )
              ) : (
                "Engineers working"
              )}
            </span>
          </div>
        </div>
      )}

      {/* Right under the answer it questions: "service is back", and for this customer it is not. */}
      {t.can_report_still_down && <StillDown t={t} msisdn={msisdn} onUpdated={onUpdated} />}

      {timeline.length > 0 && (
        <section className="tk-section" aria-labelledby={`${uid}-tl`}>
          <div className="tk-section-head">
            <h2 id={`${uid}-tl`}>What has happened</h2>
            <span className="cp-hint">Kenyan time</span>
          </div>
          <ol className="tk-timeline">
            {timeline.map((x, i) => {
              const last = i === timeline.length - 1;
              return (
                <li key={`${x.at}-${i}`} className={"tk-step" + (last ? ` now${stageTone(t.stage)}` : "")}>
                  <span className="tk-step-dot" aria-hidden="true" />
                  <span className="tk-step-text">{x.text}</span>
                  {fmtClock(x.at) && <span className="tk-step-at cp-mono">{fmtClock(x.at)}</span>}
                </li>
              );
            })}
          </ol>
        </section>
      )}

      {messages.length > 0 && (
        <section className="tk-section" aria-labelledby={`${uid}-msg`}>
          <div className="tk-section-head">
            <h2 id={`${uid}-msg`}>Messages</h2>
          </div>
          <ol className="tk-msgs">
            {messages.map((m, i) => (
              <li key={`${m.at}-${i}`} className={"tk-msg" + (m.from === "you" ? " you" : " us")}>
                <div className="tk-msg-head">
                  <span className="tk-msg-who">{m.from === "you" ? "You" : "Us"}</span>
                  {fmtClock(m.at) && <span className="cp-mono">{fmtClock(m.at)}</span>}
                </div>
                <p className="tk-msg-body">{m.body}</p>
              </li>
            ))}
          </ol>
        </section>
      )}

      <div className="cp-result-actions tk-actions">
        <button type="button" className="cp-btn secondary" onClick={onAnother}>
          Check another complaint
        </button>
        <Link className="tk-link tk-new" to="/complain">
          Send a new complaint
        </Link>
      </div>
    </div>
  );
}

/** "It's still not working": one secondary button that opens an optional note and a confirm. */
function StillDown({ t, msisdn, onUpdated }: { t: Tracked; msisdn: string; onUpdated: (t: Tracked, said: string) => void }) {
  const uid = useId();
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const noteRef = useRef<HTMLTextAreaElement>(null);
  const openerRef = useRef<HTMLButtonElement>(null);
  const errorRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (error) errorRef.current?.focus({ preventScroll: false });
  }, [error]);

  const reveal = () => {
    setOpen(true);
    window.requestAnimationFrame(() => noteRef.current?.focus());
  };
  const cancel = () => {
    if (busy) return;
    setOpen(false);
    setError("");
    window.requestAnimationFrame(() => openerRef.current?.focus());
  };
  const send = async () => {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      const next = await supportApi.stillDown(t.ref, msisdn, note);
      onUpdated(next, "Thank you for telling us. Your complaint is open again and a person will check it.");
    } catch (e) {
      setError(stillDownProblem(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="tk-section tk-still" aria-labelledby={`${uid}-h`}>
      <div className="tk-section-head">
        <h2 id={`${uid}-h`}>Still no service?</h2>
      </div>
      <p className="tk-still-text">If service is not back for you, tell us. Your complaint opens again and a person checks it.</p>
      {!open ? (
        <button type="button" className="cp-btn secondary" onClick={reveal} ref={openerRef} aria-expanded={false}>
          It's still not working
        </button>
      ) : (
        <div className="tk-still-form">
          <label htmlFor={`${uid}-note`}>What are you seeing? (optional)</label>
          <textarea
            id={`${uid}-note`}
            ref={noteRef}
            value={note}
            onChange={(e) => setNote(e.target.value)}
            rows={3}
            maxLength={500}
            aria-describedby={`${uid}-note-hint`}
          />
          <p id={`${uid}-note-hint`} className="cp-hint">
            For example: no signal at all, or calls drop after a minute.
          </p>
          <div className="cp-submit-row tk-still-buttons">
            <button type="button" className="cp-btn primary" onClick={send} aria-disabled={busy || undefined} aria-busy={busy || undefined}>
              {busy ? "Sending…" : "Report that it's still down"}
            </button>
            <button type="button" className="cp-btn ghost" onClick={cancel} aria-disabled={busy || undefined}>
              Cancel
            </button>
          </div>
          {error && (
            <div className="cp-alert tk-still-error" role="alert" ref={errorRef} tabIndex={-1}>
              {error}
            </div>
          )}
        </div>
      )}
    </section>
  );
}
