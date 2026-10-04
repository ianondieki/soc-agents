import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { BrandMark } from "../components/shell/BrandMark";
import ThemeToggle from "../components/support/ThemeToggle";
import {
  BODY_MAX,
  BODY_MIN,
  customerSteps,
  errorDetail,
  fieldErrors,
  isNetworkError,
  isSupportApiError,
  msisdnProblem,
  statusOfError,
  supportApi,
  type CaseDetail,
} from "../lib/support";
import "./Complain.css";

/**
 * The public complaint form at /complain, outside the console shell. The question first, the
 * phone number second, the optional name and account number behind a disclosure, then one
 * button. After it: an authored result with the reference large in the Mono, what happened to
 * the complaint revealed one quick step at a time (static under reduced motion and quiet mode),
 * and the reply. Written for a customer: the page shows only what the customer typed and what
 * the desk replied, never an account holder's name, a policy line or a staff link.
 */

interface Example {
  label: string;
  text: string;
  /** A demo account the tools know (config/support/accounts.yaml), offered when the phone is empty. */
  msisdn: string;
}

const EXAMPLES: Example[] = [
  { label: "Wrong-number M-PESA transfer", text: "I sent KES 1,500 to the wrong number this morning. The code is SJK4H7QW2L, please reverse it.", msisdn: "0700 000 412" },
  { label: "Hakuna network, Kayole (Sheng)", text: "Manze hakuna network huku Kayole tangu saa nne, kuna shida gani?", msisdn: "0700 000 567" },
  { label: "Bundle imeisha mapema (Kiswahili)", text: "Nilinunua bundle ya Business 50GB lakini imeisha mapema, sijatumia hata nusu.", msisdn: "0700 000 890" },
  { label: "No network in Nakuru", text: "No network in Nakuru since morning, I cannot make any calls.", msisdn: "0700 001 023" },
  { label: "SIM swap on my line", text: "Someone did a SIM swap on my line last night and withdrew KES 20,000 from my M-PESA. I did not authorise this!", msisdn: "0700 001 245" },
];

type Errors = { name?: string; msisdn?: string; account_ref?: string; body?: string; form?: string };

interface Sent {
  detail: CaseDetail;
  /** 200: the same number sent the same words in the last two minutes; nothing new was filed. */
  duplicate: boolean;
  /** The name the caller typed, if any: the only name this page ever shows. */
  typedName: string;
}

function formError(e: unknown): string {
  const s = statusOfError(e);
  if (s === 429) {
    const secs = isSupportApiError(e) ? e.retryAfter : null;
    const mins = secs ? Math.max(1, Math.ceil(secs / 60)) : null;
    return `You have sent several complaints from this number in a short time. Please wait ${mins ? `about ${mins} ${mins === 1 ? "minute" : "minutes"}` : "a few minutes"} and try again.`;
  }
  if (s === 404) return "The complaint desk is switched off right now. Please try again later or call the contact centre.";
  if (s === 422 || s === 400) return errorDetail(e, "Please check what you typed and try again.");
  if (s != null && s >= 500) return "Something failed on our side. Your complaint was not sent; please try again in a moment.";
  if (isNetworkError(e)) return "We couldn't reach the support desk. Check your connection and try again.";
  return errorDetail(e, "Your complaint was not sent. Please try again.");
}

const reducedMotion = () => {
  try {
    return document.documentElement.getAttribute("data-quiet") === "on" || window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  } catch {
    return false;
  }
};

export default function Complain() {
  const [name, setName] = useState("");
  const [msisdn, setMsisdn] = useState("");
  const [account, setAccount] = useState("");
  const [body, setBody] = useState("");
  const [errors, setErrors] = useState<Errors>({});
  const [sending, setSending] = useState(false);
  const [sent, setSent] = useState<Sent | null>(null);
  const [touchedPhone, setTouchedPhone] = useState(false);
  const [moreOpen, setMoreOpen] = useState(false);
  const uid = useId();
  const phoneRef = useRef<HTMLInputElement>(null);
  const bodyRef = useRef<HTMLTextAreaElement>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const formErrorRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    document.title = sent ? `${sent.detail.complaint.ref}, Kenya NOC Support` : "Send a complaint, Kenya NOC Support";
    return () => {
      document.title = "Kenya NOC Mission Control";
    };
  }, [sent]);

  // A form-level error (rate limit, outage) lands beside the button and takes focus there.
  useEffect(() => {
    if (errors.form) formErrorRef.current?.focus({ preventScroll: false });
  }, [errors.form]);

  const phoneProblem = msisdnProblem(msisdn);
  const bodyLen = body.trim().length;

  const fill = (ex: Example) => {
    setBody(ex.text);
    if (!msisdn.trim()) setMsisdn(ex.msisdn);
    setErrors((e) => ({ ...e, body: undefined, msisdn: undefined }));
    bodyRef.current?.focus();
  };

  const submit = async (ev: FormEvent) => {
    ev.preventDefault();
    if (sending) return;
    const next: Errors = {};
    if (bodyLen < BODY_MIN) next.body = bodyLen === 0 ? "Tell us what went wrong." : `Add a little more: at least ${BODY_MIN} characters.`;
    if (bodyLen > BODY_MAX) next.body = `Keep it under ${BODY_MAX.toLocaleString("en-KE")} characters.`;
    if (phoneProblem) next.msisdn = phoneProblem;
    setErrors(next);
    if (next.body) {
      bodyRef.current?.focus();
      return;
    }
    if (next.msisdn) {
      phoneRef.current?.focus();
      return;
    }
    setSending(true);
    try {
      const a = await supportApi.register({
        body: body.trim(),
        msisdn: msisdn.trim(),
        ...(name.trim() ? { name: name.trim() } : {}),
        ...(account.trim() ? { account_ref: account.trim() } : {}),
        channel: "web",
      });
      setSent({ detail: a.data, duplicate: a.status === 200, typedName: name.trim() });
      window.scrollTo({ top: 0 });
      window.setTimeout(() => headingRef.current?.focus({ preventScroll: true }), 0);
    } catch (e) {
      const fields = fieldErrors(e);
      const mapped: Errors = {
        msisdn: fields.msisdn,
        body: fields.body,
        name: fields.name,
        account_ref: fields.account_ref,
      };
      const any = Object.values(mapped).some(Boolean);
      setErrors(any ? mapped : { form: formError(e) });
      if (mapped.name || mapped.account_ref) setMoreOpen(true);
      if (mapped.body) bodyRef.current?.focus();
      else if (mapped.msisdn) phoneRef.current?.focus();
    } finally {
      setSending(false);
    }
  };

  const again = () => {
    setSent(null);
    setBody("");
    setErrors({});
    window.setTimeout(() => bodyRef.current?.focus(), 0);
  };

  return (
    <div className="cp">
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <header className="cp-top">
        <div className="cp-wrap">
          <Link className="cp-brand" to="/complain" aria-label="Kenya NOC Support, the complaint form">
            <BrandMark />
            <span>Kenya NOC Support</span>
          </Link>
          <div className="cp-top-actions">
            <Link className="cp-quiet-link" to="/">
              Front page
            </Link>
            <ThemeToggle className="cp-icon-btn" />
          </div>
        </div>
      </header>

      <main id="main" className="cp-main" tabIndex={-1}>
        <div className="cp-wrap">
          {!sent ? (
            <form className="cp-form" onSubmit={submit} noValidate>
              <h1 ref={headingRef} tabIndex={-1}>
                Tell us what went wrong
              </h1>
              <p className="cp-lead">
                Write it in your own words, in English or Kiswahili. Our agents read it straight away; when it needs a person, one takes it and you
                are told why.
              </p>

              <div className="cp-field">
                <div className="cp-label-row">
                  <label htmlFor={`${uid}-body`}>What went wrong?</label>
                  <span className={"cp-count" + (bodyLen > BODY_MAX ? " over" : "")} aria-live="polite">
                    <span className="cp-mono">{bodyLen.toLocaleString("en-KE")}</span> of {BODY_MAX.toLocaleString("en-KE")}
                  </span>
                </div>
                <textarea
                  id={`${uid}-body`}
                  ref={bodyRef}
                  value={body}
                  onChange={(e) => {
                    setBody(e.target.value);
                    if (errors.body) setErrors((x) => ({ ...x, body: undefined }));
                  }}
                  rows={5}
                  maxLength={BODY_MAX + 200}
                  aria-describedby={`${uid}-body-hint${errors.body ? ` ${uid}-body-err` : ""}`}
                  aria-invalid={!!errors.body || undefined}
                  required
                />
                <p id={`${uid}-body-hint`} className="cp-hint">
                  Say what happened, where, and when. If it's about an M-PESA transfer, include the 10-character code from the confirmation SMS.
                </p>
                {errors.body && (
                  <p id={`${uid}-body-err`} className="cp-error">
                    {errors.body}
                  </p>
                )}
                <div className="cp-examples">
                  <span className="cp-examples-label" id={`${uid}-ex`}>
                    Or start from an example
                  </span>
                  <div className="cp-chips" role="group" aria-labelledby={`${uid}-ex`}>
                    {EXAMPLES.map((ex) => (
                      <button key={ex.label} type="button" className="cp-chip" onClick={() => fill(ex)} title={ex.text}>
                        {ex.label}
                      </button>
                    ))}
                  </div>
                </div>
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
                  The line the problem is on. We keep it masked on our side.
                </p>
                {(errors.msisdn || (touchedPhone && phoneProblem && msisdn)) && (
                  <p id={`${uid}-phone-err`} className="cp-error">
                    {errors.msisdn || phoneProblem}
                  </p>
                )}
              </div>

              <details className="cp-more" open={moreOpen} onToggle={(e) => setMoreOpen((e.currentTarget as HTMLDetailsElement).open)}>
                <summary>Add your name or account number (optional)</summary>
                <div className="cp-more-body">
                  <div className="cp-field">
                    <label htmlFor={`${uid}-name`}>Your name</label>
                    <input id={`${uid}-name`} value={name} onChange={(e) => setName(e.target.value)} autoComplete="name" maxLength={128} />
                    <p className="cp-hint">So the reply can greet you.</p>
                    {errors.name && <p className="cp-error">{errors.name}</p>}
                  </div>
                  <div className="cp-field">
                    <label htmlFor={`${uid}-acct`}>Account number</label>
                    <input id={`${uid}-acct`} value={account} onChange={(e) => setAccount(e.target.value)} autoComplete="off" maxLength={32} placeholder="ACC-100412" className="cp-mono" />
                    <p className="cp-hint">Your account number, if you know it. Put an M-PESA transaction code in the message above instead.</p>
                    {errors.account_ref && <p className="cp-error">{errors.account_ref}</p>}
                  </div>
                </div>
              </details>

              <div className="cp-submit">
                <div className="cp-submit-row">
                  <button type="submit" className="cp-btn primary" aria-disabled={sending || undefined} aria-busy={sending || undefined}>
                    {sending ? "Sending…" : "Send complaint"}
                  </button>
                  {errors.form && (
                    <div className="cp-alert" role="alert" ref={formErrorRef} tabIndex={-1}>
                      {errors.form}
                    </div>
                  )}
                </div>
                <p className="cp-hint">Sending runs your complaint through our agents now. You get a reference and a first reply on this page.</p>
              </div>
            </form>
          ) : (
            <Result sent={sent} onAgain={again} headingRef={headingRef} />
          )}
        </div>
      </main>

      <footer className="cp-foot">
        <div className="cp-wrap">
          <span>Demo and training product. Not an official Safaricom or Airtel system.</span>
        </div>
      </footer>
    </div>
  );
}

function Result({ sent, onAgain, headingRef }: { sent: Sent; onAgain: () => void; headingRef: React.RefObject<HTMLHeadingElement> }) {
  const d = sent.detail;
  const c = d.complaint;
  const steps = customerSteps(d);
  const still = reducedMotion();
  // One quick beat per step, then the reply; nothing moves under reduced motion or quiet mode.
  const beat = still ? 0 : 180;
  const replyDelay = still ? 0 : 200 + steps.length * beat;
  return (
    <div className={"cp-result" + (still ? " still" : "")}>
      <h1 ref={headingRef} tabIndex={-1}>
        {sent.duplicate ? "We already have this complaint" : "We have your complaint"}
      </h1>
      <p className="cp-lead">
        {sent.duplicate
          ? "This number sent the same words in the last two minutes, so nothing new was filed. Here is where that complaint stands."
          : sent.typedName
            ? `Thank you, ${sent.typedName}. Keep the reference for any follow-up.`
            : "Keep the reference for any follow-up."}
      </p>

      <div className="cp-ref">
        <span className="cp-ref-label">Your reference</span>
        <span className="cp-ref-num cp-mono">{c.ref}</span>
      </div>

      <section className="cp-what" aria-labelledby="cp-what-h">
        <h2 id="cp-what-h">What happened to it</h2>
        <ol className="cp-steps">
          {steps.map((s, i) => (
            <li key={s.key} className={"cp-step" + (s.tone ? ` ${s.tone}` : "")} style={{ animationDelay: `${i * beat}ms` }}>
              <span className="cp-step-dot" aria-hidden="true" />
              <span className="cp-step-head">{s.head}</span>
              {s.line && <span className="cp-step-line">{s.line}</span>}
            </li>
          ))}
        </ol>
      </section>

      {c.reply && (
        <section className="cp-reply" aria-labelledby="cp-reply-h" style={{ animationDelay: `${replyDelay}ms` }}>
          <h2 id="cp-reply-h">Our reply</h2>
          <blockquote className="cp-reply-body">{c.reply}</blockquote>
          <p className="cp-hint">
            Sent to <span className="cp-mono">{c.customer.msisdn_masked}</span>.
          </p>
        </section>
      )}

      <div className="cp-result-actions" style={{ animationDelay: `${replyDelay}ms` }}>
        <button type="button" className="cp-btn secondary" onClick={onAgain}>
          Send another complaint
        </button>
      </div>
    </div>
  );
}
