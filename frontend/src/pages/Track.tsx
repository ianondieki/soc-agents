import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { TrackNext } from "../components/support/PublicAside";
import PublicFrame from "../components/support/PublicFrame";
import {
  errorDetail,
  fmtClock,
  isNetworkError,
  isSupportApiError,
  msisdnProblem,
  statusOfError,
  supportApi,
  waitPhrase,
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
  if (!raw.trim()) return "Enter your reference. It is on the page you saw after sending (it starts CMP-).";
  if (!/^CMP-\d{6,9}$/.test(normalizeRef(raw))) return "A reference looks like CMP-000123.";
  return null;
}

/** "Please wait about 3 minutes and try again."; a daily limit (over two hours) says "tomorrow". */
function tryAgain(e: unknown): string {
  const wait = waitPhrase(isSupportApiError(e) ? e.retryAfter : null);
  return wait ? `Please wait ${wait} and try again.` : "Please try again tomorrow.";
}

interface FormProblem {
  text: string;
  /** The 404's hint: where the reference is and which number to use. */
  hint?: string;
}

function trackProblem(e: unknown): FormProblem {
  const s = statusOfError(e);
  if (s === 404)
    return { text: NOT_FOUND, hint: "Check the reference on the page you saw after sending (it starts CMP-), and use the phone number you complained from." };
  if (s === 429) {
    const daily = isSupportApiError(e) && (e.retryAfter ?? 0) > 7200;
    return { text: `${daily ? "This complaint has been checked many times today." : "You have checked several times in a short time."} ${tryAgain(e)}` };
  }
  // 413 (an oversized body) answers with the same words as a 400 or a 404.
  if (s === 409 || s === 422 || s === 400 || s === 413) return { text: errorDetail(e, "Please check what you typed and try again.") };
  if (s != null && s >= 500) return { text: "Something failed on our side. Please try again in a moment." };
  if (isNetworkError(e)) return { text: "We couldn't reach the support desk. Check your connection and try again." };
  return { text: errorDetail(e, "We couldn't check your complaint. Please try again.") };
}

/** The still-down report's failure: the server's own sentence for a 409 ("already reported"…). */
function stillDownProblem(e: unknown): string {
  const s = statusOfError(e);
  if (s === 409) return errorDetail(e, "We can't take a still-down report on this complaint right now.");
  if (s === 429) {
    const daily = isSupportApiError(e) && (e.retryAfter ?? 0) > 7200;
    return `${daily ? "We have had many reports from this number today." : "You have sent several reports in a short time."} ${tryAgain(e)}`;
  }
  if (s === 413 || s === 400) return errorDetail(e, "Your note is too long. Shorten it and try again.");
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
  const [errors, setErrors] = useState<Errors>({});
  const [sending, setSending] = useState(false);
  const [result, setResult] = useState<Tracked | null>(null);
  /** The number the shown result was found with: the still-down report sends it again. */
  const [foundWith, setFoundWith] = useState("");
  const [confirmation, setConfirmation] = useState("");
  /** When the result on screen was fetched: the page says so, and "Check again" refreshes it. */
  const [checkedAt, setCheckedAt] = useState<Date | null>(null);
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

  // A reference from a link (the complaint page, the restore SMS) fills the box; the phone is next.
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
      setCheckedAt(new Date());
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
        <div className="cp-split">
        <form className="cp-form" onSubmit={submit} noValidate>
          <p className="eyebrow">Customer support</p>
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
              It is on the page you saw after sending (it starts CMP-).
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
              inputMode="tel"
              autoComplete="tel"
              placeholder="0712 345 678"
              aria-describedby={`${uid}-phone-hint${errors.msisdn ? ` ${uid}-phone-err` : ""}`}
              aria-invalid={!!errors.msisdn || undefined}
              required
            />
            <p id={`${uid}-phone-hint`} className="cp-hint">
              The number you complained from. We use your number only to find your complaint.
            </p>
            {/* Checked when the form is sent, never on blur: an error appearing on blur moved the
                button from under the pointer and the first click was lost. */}
            {errors.msisdn && (
              <p id={`${uid}-phone-err`} className="cp-error">
                {errors.msisdn}
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
        <TrackNext />
        </div>
      ) : (
        <Status
          t={result}
          msisdn={foundWith}
          headingRef={headingRef}
          confirmation={confirmation}
          onUpdated={(t, said) => {
            setResult(t);
            setConfirmation(said);
            setCheckedAt(new Date());
          }}
          checkedAt={checkedAt}
          onRechecked={(t) => {
            setResult(t);
            setCheckedAt(new Date());
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
  checkedAt: Date | null;
  onRechecked: (t: Tracked) => void;
  onAnother: () => void;
}

function Status({ t, msisdn, headingRef, confirmation, onUpdated, checkedAt, onRechecked, onAnother }: StatusProps) {
  const [rechecking, setRechecking] = useState(false);
  const [recheckSaid, setRecheckSaid] = useState("");
  const recheck = async () => {
    if (rechecking) return;
    setRechecking(true);
    setRecheckSaid("");
    try {
      const next = await supportApi.track(t.ref, msisdn);
      onRechecked(next);
      setRecheckSaid(next.headline === t.headline ? "Checked again: nothing has changed." : `Checked again: ${next.headline}.`);
    } catch (e) {
      setRecheckSaid(trackProblem(e).text);
    } finally {
      setRechecking(false);
    }
  };
  const checked = checkedAt ? fmtClock(checkedAt.toISOString()) : "";
  // The strip says only what the headline does not: the place when the headline does not name it.
  const placeInHeadline = !!t.outage?.place && t.headline.includes(t.outage.place);
  const uid = useId();
  const timeline = Array.isArray(t.timeline) ? t.timeline.filter((x) => x && x.text) : [];
  const messages = Array.isArray(t.messages) ? t.messages.filter((m) => m && m.body) : [];
  const received = fmtClock(t.received_at);
  // The reply-by time, unless the detail sentence already gives a time ("…and reply by 13:05 EAT").
  const dueAt = t.reply_due_at ? fmtClock(t.reply_due_at) : "";
  const due = dueAt && !/\b\d{1,2}:\d{2}\b/.test(t.detail || "") ? dueAt : "";
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
        {checked && (
          <span>
            , checked <span className="cp-mono">{checked}</span>
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
        // One line: the state in its signal colour, then the ticket to quote if you call. A customer
        // who reported it still down never sees a green "Restored" (§7.3).
        <p className={"tk-outage" + (t.outage.state === "restored" ? " ok" : " warn")}>
          <span className="tk-dot" aria-hidden="true" />
          <span className="tk-outage-state">
            {t.outage.state === "still_down" ? (
              "You told us it is still down"
            ) : t.outage.state === "restored" ? (
              t.outage.restored_at ? (
                <>
                  Restored at <span className="cp-mono">{fmtClock(t.outage.restored_at)}</span>
                </>
              ) : (
                "Restored"
              )
            ) : (
              "Engineers working"
            )}
            {!placeInHeadline && t.outage.place ? ` in ${t.outage.place}` : ""}
          </span>
          {t.outage.ticket && (
            <span className="tk-outage-ticket">
              Ticket <span className="cp-mono">{t.outage.ticket}</span>
            </span>
          )}
        </p>
      )}

      {/* Right under the answer it questions: "service is back", and for this customer it is not. */}
      {t.can_report_still_down && <StillDown t={t} msisdn={msisdn} onUpdated={onUpdated} />}

      {timeline.length > 0 && (
        <section className="tk-section" aria-labelledby={`${uid}-tl`}>
          <div className="tk-section-head">
            <h2 id={`${uid}-tl`}>What has happened</h2>
            <span className="cp-hint">Times in EAT</span>
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
                  <span className="tk-msg-who">{m.from === "you" ? "You" : "Kenya NOC Support"}</span>
                  {fmtClock(m.at) && <span className="cp-mono">{fmtClock(m.at)}</span>}
                </div>
                <p className="tk-msg-body">{m.body}</p>
              </li>
            ))}
          </ol>
        </section>
      )}

      <div className="cp-result-actions tk-actions">
        <button type="button" className="cp-btn secondary" onClick={recheck} aria-disabled={rechecking || undefined} aria-busy={rechecking || undefined}>
          {rechecking ? "Checking…" : "Check again"}
        </button>
        <button type="button" className="cp-btn secondary" onClick={onAnother}>
          Check another complaint
        </button>
        <Link className="tk-link tk-new" to="/complain">
          Send a new complaint
        </Link>
      </div>
      <p className="tk-recheck" role="status">
        {recheckSaid}
      </p>
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
