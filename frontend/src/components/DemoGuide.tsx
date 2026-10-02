import { useEffect, useId, useRef, useState, type KeyboardEvent } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { GUIDE_STEP_KEY } from "../lib/demo";
import { IconCheck } from "../lib/icons";
import "./DemoGuide.css";

/**
 * The guided demo as a bar, one row directly under the top bar and in normal flow, so it
 * never covers what it is describing. Each of the five steps names what to do, keeps what to
 * say in a "Say" disclosure, and offers one button that takes the presenter there; that
 * button is hidden on its own page, where the page's own controls are the beat. The step
 * survives a page change for the session, so the bar follows the presenter around.
 *
 * Keyboard: opening the bar, and every step change, moves focus to the step title; Escape
 * inside the bar (or with nothing focused) closes it and returns focus to the top bar's
 * "Guided demo" button. Back and Next are never `disabled` (focus would drop to <body>):
 * Back at the first step is `aria-disabled`, and the last step's Next is "Finish".
 *
 * App owns the storm and the counts; this component only reads them:
 *  - `storming` / `stormDone`: step 1 reads "Storm running" while it runs and offers
 *    "Storm done · Next" once it has finished (or any incident is open);
 *  - `latestIncidentId`: the ticket step 2 opens (falls back to `firstIncidentId`);
 *  - `pendingCount`: the count on step 3's button;
 *  - `approvedSinceOpen`: step 3 shows a check, and Next turns primary, once the presenter has
 *    approved a card since opening the guide.
 */

export interface GuideStep {
  title: string;
  say: string;
  /** The button that takes the presenter to the step's page, hidden when already there. */
  action?: { label: string; run: () => void };
  /** The step's page: the action hides when the location matches. */
  onPage?: (pathname: string) => boolean;
  /** The step's beat has happened: a check beside the title and a primary Next. */
  done?: boolean;
  /** Next's label when the step is done (default "Next"). */
  doneNext?: string;
  /** A short state word beside the title while something is under way. */
  status?: string;
}

/** Where the "Say" text starts collapsed: the pages whose content the presenter is reading out. */
const SAY_COLLAPSED = (pathname: string) => pathname.startsWith("/hitl") || /^\/incidents\/[^/]+/.test(pathname);

function readStep(): number {
  try {
    return Number(sessionStorage.getItem(GUIDE_STEP_KEY) || 0) || 0;
  } catch {
    return 0;
  }
}

/** The top bar's toggle, to hand focus back to on close. `live` may tag it `data-guide-toggle`. */
function findToggle(opener: HTMLElement | null): HTMLElement | null {
  const tagged = document.querySelector<HTMLElement>("[data-guide-toggle]");
  if (tagged) return tagged;
  if (opener && opener.isConnected) return opener;
  const byText = Array.from(document.querySelectorAll<HTMLButtonElement>(".topbar button")).find(
    (b) => b.textContent?.trim() === "Guided demo"
  );
  return byText || null;
}

export default function DemoGuide({
  open,
  onClose,
  storming,
  stormDone = false,
  pendingCount = 0,
  latestIncidentId = null,
  approvedSinceOpen = 0,
  onLaunchStorm,
  onRunStorm,
  firstIncidentId = null,
}: {
  open: boolean;
  onClose: () => void;
  storming: boolean;
  /** The storm has finished this session, or any incident is open. */
  stormDone?: boolean;
  /** Approval cards waiting for a decision. */
  pendingCount?: number;
  /** The newest incident, for step 2. */
  latestIncidentId?: string | null;
  /** `hitl.approved` events since the guide was opened. */
  approvedSinceOpen?: number;
  /** Starts the storm (App navigates nowhere; the bar takes the presenter to Mission control first). */
  onLaunchStorm?: () => void;
  /** Older contract: navigates to Mission control and starts the storm. Used when `onLaunchStorm` is absent. */
  onRunStorm?: () => Promise<void> | void;
  /** Older contract: the storm's first incident. `latestIncidentId` wins. */
  firstIncidentId?: string | null;
}) {
  const nav = useNavigate();
  const { pathname } = useLocation();
  const titleId = useId();
  const sayId = useId();
  const titleRef = useRef<HTMLParagraphElement>(null);
  const openerRef = useRef<HTMLElement | null>(null);
  const [step, setStep] = useState<number>(readStep);
  const [sayOpen, setSayOpen] = useState(() => !SAY_COLLAPSED(pathname));

  useEffect(() => {
    try {
      sessionStorage.setItem(GUIDE_STEP_KEY, String(step));
    } catch {
      /* storage blocked: the step simply does not persist */
    }
  }, [step]);

  // A new page sets the disclosure to that page's default: collapsed where the presenter reads
  // from the screen (Approvals, a ticket), open elsewhere.
  useEffect(() => {
    setSayOpen(!SAY_COLLAPSED(pathname));
  }, [pathname]);

  // Opening the bar and changing step both put focus on the step title, so a keyboard or
  // screen-reader presenter lands on what the step is. The element that had focus when the bar
  // opened is remembered as the place to return to.
  const wasOpen = useRef(false);
  useEffect(() => {
    if (open && !wasOpen.current) {
      const active = document.activeElement;
      openerRef.current = active instanceof HTMLElement && active !== document.body ? active : null;
    }
    wasOpen.current = open;
    if (open) titleRef.current?.focus({ preventScroll: true });
  }, [open, step]);

  const close = () => {
    const toggle = findToggle(openerRef.current);
    onClose();
    toggle?.focus();
  };

  // Escape with nothing focused (focus fell to <body>) still closes the bar; inside the bar the
  // section's own handler does it, so other components keep their Escape.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key !== "Escape" || e.defaultPrevented) return;
      if (document.activeElement && document.activeElement !== document.body) return;
      close();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
    // `close` reads refs and props only; re-binding on every render is unnecessary.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  if (!open) return null;

  const incidentId = latestIncidentId || firstIncidentId;
  const launch = () => {
    if (onLaunchStorm) {
      if (pathname !== "/") nav("/");
      onLaunchStorm();
    } else if (onRunStorm) {
      void onRunStorm();
    }
  };

  const steps: GuideStep[] = [
    {
      title: "Heavy rain hits three regions",
      say:
        "Press Launch the storm on Mission control. Eleven alarms arrive from Rift, Mt Kenya and Nairobi East in " +
        "about twenty seconds; watch each hop light up on the rail as its agent finishes its part. Child sites " +
        "fold under their HUB majors instead of opening duplicate tickets.",
      action: storming || stormDone ? undefined : { label: "Launch the storm", run: launch },
      onPage: (p) => p === "/",
      status: storming ? "Storm running" : undefined,
      done: stormDone && !storming,
      doneNext: "Storm done · Next",
    },
    {
      title: "Read what the agents decided",
      say:
        "Open the ticket. Every field a NOC analyst used to type is already filled, and every hop says why: the " +
        "subscriber thresholds, the HUB floor, the region-by-domain MSP matrix. Select a hop for its reasoning; " +
        "the Audit trail keeps each one.",
      action: incidentId
        ? { label: "Open the latest ticket", run: () => nav(`/incidents/${incidentId}`) }
        : { label: "Open Incident board", run: () => nav("/incidents") },
      onPage: (p) => (incidentId ? p.startsWith("/incidents/") : p === "/incidents"),
    },
    {
      title: "Approve what matters",
      say:
        "A P2 broadcast never leaves without a person. Approvals shows the SMS and the email exactly as they will " +
        "be sent, with the facts beside them. Claim, read, give a reason, approve.",
      action: { label: pendingCount > 0 ? `Open Approvals (${pendingCount})` : "Open Approvals", run: () => nav("/hitl") },
      onPage: (p) => p.startsWith("/hitl"),
      done: approvedSinceOpen > 0,
    },
    {
      title: "Hand the shift over",
      say:
        "The ledger is already written. Generate the handover: owners, priorities and what the night shift must " +
        "watch. It goes to the shift lead as an approval card before anything is sent.",
      action: { label: "Open Shift desk", run: () => nav("/shift") },
      onPage: (p) => p.startsWith("/shift"),
    },
    {
      title: "Show the numbers",
      say:
        "Hours of toil taken off the floor, alarms folded into an open ticket before they became duplicates, and " +
        "every decision that leaves the building kept with a person.",
      action: { label: "Open Showcase", run: () => nav("/showcase") },
      onPage: (p) => p.startsWith("/showcase"),
    },
  ];

  const last = steps.length - 1;
  const i = Math.min(Math.max(step, 0), last);
  const current = steps[i];
  const showAction = current.action && !(current.onPage && current.onPage(pathname));
  const atStart = i === 0;
  const atEnd = i === last;
  const nextLabel = atEnd ? "Finish" : current.done ? current.doneNext || "Next" : "Next";

  const goBack = () => {
    if (!atStart) setStep(i - 1);
  };
  const goNext = () => {
    if (atEnd) {
      setStep(0);
      close();
    } else setStep(i + 1);
  };

  // The action button hides once the presenter is on its page (or the storm is running), so
  // focus would fall to <body>: it goes to the step title instead.
  const runAction = () => {
    current.action?.run();
    window.requestAnimationFrame(() => {
      const active = document.activeElement;
      if (!active || active === document.body) titleRef.current?.focus({ preventScroll: true });
    });
  };

  const onKeyDown = (e: KeyboardEvent<HTMLElement>) => {
    if (e.key === "Escape") {
      e.preventDefault();
      close();
    }
  };

  return (
    <section className="guide-bar" role="region" aria-labelledby={titleId} onKeyDown={onKeyDown}>
      <div className="guide-bar-row">
        <span className="guide-bar-step">
          Guided demo, step {i + 1} of {steps.length}
        </span>
        <p id={titleId} className="guide-bar-title" tabIndex={-1} ref={titleRef}>
          {current.title}
          {current.done && (
            <span className="guide-bar-done">
              <IconCheck />
              <span className="sr-only">, done</span>
            </span>
          )}
        </p>
        {current.status && (
          <span className="guide-bar-status" role="status">
            {current.status}
          </span>
        )}
        <button
          type="button"
          className="btn sm quiet guide-bar-say-toggle"
          aria-expanded={sayOpen}
          aria-controls={sayId}
          onClick={() => setSayOpen((o) => !o)}
        >
          Say
        </button>
        <span className="guide-bar-actions">
          {showAction && current.action && (
            <button type="button" className="btn sm" onClick={runAction}>
              {current.action.label}
            </button>
          )}
          <button type="button" className="btn sm" aria-disabled={atStart || undefined} onClick={goBack}>
            Back
          </button>
          <button type="button" className={"btn sm" + (current.done || atEnd ? " primary" : "")} onClick={goNext}>
            {nextLabel}
          </button>
          <button type="button" className="btn sm quiet" onClick={close}>
            Close
          </button>
        </span>
      </div>
      <p id={sayId} className="guide-bar-say" hidden={!sayOpen}>
        {current.say}
      </p>
    </section>
  );
}
