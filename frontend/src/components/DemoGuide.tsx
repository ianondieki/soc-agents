import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { GUIDE_STEP_KEY } from "../lib/demo";

/**
 * A five-step walkthrough for presenting the prototype. It does not automate anything the
 * presenter would rather do by hand: each step names what to do, what to say and offers
 * one button that takes the presenter there. The step survives a page change for the
 * session so the panel follows the presenter around.
 */

export interface GuideStep {
  title: string;
  say: string;
  action?: { label: string; run: () => void | Promise<void> };
}

export default function DemoGuide({
  open,
  onClose,
  onRunStorm,
  storming,
  firstIncidentId,
}: {
  open: boolean;
  onClose: () => void;
  onRunStorm: () => Promise<void> | void;
  storming: boolean;
  firstIncidentId: string | null;
}) {
  const nav = useNavigate();
  const [step, setStep] = useState<number>(() => {
    try {
      return Number(sessionStorage.getItem(GUIDE_STEP_KEY) || 0) || 0;
    } catch {
      return 0;
    }
  });
  useEffect(() => {
    try {
      sessionStorage.setItem(GUIDE_STEP_KEY, String(step));
    } catch {
      /* storage blocked: the step simply does not persist */
    }
  }, [step]);

  // While the panel is open, <html data-guide="on"> and --guide-h (its height) let the CSS keep
  // the Approvals decision footer clear of it (styles.css, "Guided demo").
  const panelRef = useRef<HTMLElement>(null);
  useLayoutEffect(() => {
    const root = document.documentElement;
    if (!open) {
      root.removeAttribute("data-guide");
      root.style.removeProperty("--guide-h");
      return;
    }
    root.setAttribute("data-guide", "on");
    const el = panelRef.current;
    const measure = () => {
      if (el) root.style.setProperty("--guide-h", `${Math.ceil(el.getBoundingClientRect().height)}px`);
    };
    measure();
    const ro = typeof ResizeObserver !== "undefined" && el ? new ResizeObserver(measure) : null;
    if (ro && el) ro.observe(el);
    return () => {
      ro?.disconnect();
      root.removeAttribute("data-guide");
      root.style.removeProperty("--guide-h");
    };
  }, [open]);

  if (!open) return null;

  const steps: GuideStep[] = [
    {
      title: "Heavy rain hits three regions",
      say:
        "Heavy rain hits Rift, Mt Kenya and Nairobi East: microwave hops drop and child sites cascade under " +
        "their HUB majors. Eleven alarms arrive in about twenty seconds. Watch the rail on Mission control: " +
        "each hop is one agent finishing its part of the job.",
      action: { label: storming ? "Storm running…" : "Launch the storm", run: onRunStorm },
    },
    {
      title: "Read what the agents decided",
      say:
        "Open the first ticket. Every field a NOC analyst used to type is already filled, and every hop " +
        "says why: the subscriber thresholds, the HUB floor, the region-by-domain MSP matrix. The Audit " +
        "trail keeps each of those reasons.",
      action: firstIncidentId ? { label: "Open the first ticket", run: () => nav(`/incidents/${firstIncidentId}`) } : undefined,
    },
    {
      title: "Approve what matters",
      say:
        "A P2 broadcast never leaves without a person. Approvals shows the SMS and the email exactly as " +
        "they will be sent, with the facts beside them. Claim, read, approve.",
      action: { label: "Open Approvals", run: () => nav("/hitl") },
    },
    {
      title: "Hand the shift over",
      say: "The ledger is already written. Generate the handover: owners, priorities and what the night shift must watch.",
      action: { label: "Open Shift desk", run: () => nav("/shift") },
    },
    {
      title: "Show the numbers",
      say: "Hours of toil taken off the floor, alarms absorbed before they became duplicate tickets, decisions kept human.",
      action: { label: "Open Showcase", run: () => nav("/showcase") },
    },
  ];
  const i = Math.min(step, steps.length - 1);
  const current = steps[i];

  return (
    <aside className="guide" role="dialog" aria-label="Guided demo" ref={panelRef}>
      <div className="guide-head">
        <strong>Guided demo</strong>
        <span className="muted">
          Step {i + 1} of {steps.length}
        </span>
        <button type="button" className="btn guide-close" onClick={onClose} aria-label="Close the guided demo">
          Close
        </button>
      </div>
      <h2 className="guide-title">{current.title}</h2>
      <p className="guide-say">{current.say}</p>
      <div className="guide-actions">
        {current.action && (
          <button type="button" className="btn primary" disabled={storming && i === 0} onClick={() => current.action!.run()}>
            {current.action.label}
          </button>
        )}
        <span className="guide-nav">
          <button type="button" className="btn" disabled={i === 0} onClick={() => setStep(i - 1)}>
            Back
          </button>
          <button type="button" className="btn" disabled={i === steps.length - 1} onClick={() => setStep(i + 1)}>
            Next
          </button>
        </span>
      </div>
    </aside>
  );
}
