import { Link } from "react-router-dom";
import { LifeBuoy, OctagonAlert, Siren, TriangleAlert, UserRoundCheck, X } from "lucide-react";
import { dismissSignal, holdSignal, useAlerts, type CueKind } from "../../lib/feedback";

/**
 * The alarm signal: one card under the top bar, on the right, that says in words what just
 * happened (a new P1 or P2 ticket, a decision waiting, an escalation, a failure), counts repeats
 * ("4 new P2 tickets") and offers the one place to go. It is the screen's half of an alert cue
 * (lib/feedback.ts); the buzz and the tone are the other half.
 *
 * It names its kind in words and an icon as well as its colour, holds while a pointer or focus is
 * on it, and leaves on its own after nine seconds. The entrance and the P1 ring are the only
 * motion; quiet mode and reduced motion keep the card and drop the motion (styles.css, `.signal`).
 * The words also go to a polite live region that is always in the page, so a screen reader hears
 * the first signal as well as the next.
 */

const ICON: Partial<Record<CueKind, typeof Siren>> = {
  "alarm-p1": Siren,
  "alarm-p2": TriangleAlert,
  decision: UserRoundCheck,
  escalation: LifeBuoy,
  "run-failed": OctagonAlert,
};

const TONE: Partial<Record<CueKind, string>> = {
  "alarm-p1": "p1",
  "alarm-p2": "p2",
  decision: "hitl",
  escalation: "danger",
  "run-failed": "danger",
};

export default function AlertSignal() {
  const { signal } = useAlerts();
  const Icon = signal ? ICON[signal.kind] ?? Siren : null;
  const said = signal ? `${signal.text}${signal.ref ? ` ${signal.ref}` : ""}` : "";
  return (
    <>
      <span className="sr-only" aria-live="polite" aria-atomic="true">
        {said}
      </span>
      {signal && Icon && (
        <section
          key={signal.id}
          className={`signal ${TONE[signal.kind] ?? "danger"}`}
          aria-label="Alert"
          title={said}
          onPointerEnter={() => holdSignal(true)}
          onPointerLeave={() => holdSignal(false)}
          onFocus={() => holdSignal(true)}
          onBlur={(e) => {
            if (!e.currentTarget.contains(e.relatedTarget as Node | null)) holdSignal(false);
          }}
        >
          <span className="signal-mark" aria-hidden="true">
            <Icon size={18} strokeWidth={2} />
          </span>
          <p className="signal-text">
            {/* Remounted on each count, so the figure can mark the change. */}
            <span key={signal.count} className={"signal-words" + (signal.count > 1 ? " counted" : "")}>
              {signal.text}
            </span>
            {signal.ref && <span className="signal-ref mono">{signal.ref}</span>}
          </p>
          {signal.href && (
            <Link className="btn sm signal-open" to={signal.href} onClick={dismissSignal}>
              Open
            </Link>
          )}
          <button type="button" className="signal-close" aria-label="Dismiss the alert" onClick={dismissSignal}>
            <X size={16} strokeWidth={2} aria-hidden="true" />
          </button>
          {/* How long is left: a hairline that drains, restarted by every update. */}
          <span key={`t${signal.at}`} className="signal-time" aria-hidden="true" />
        </section>
      )}
    </>
  );
}
