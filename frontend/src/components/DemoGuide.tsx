import { useEffect, useState } from "react";
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

  if (!open) return null;

  const steps: GuideStep[] = [
    {
      title: "Start the storm",
      say:
        "Heavy rain hits Rift, Mt Kenya and Nairobi East. Eleven alarms arrive in about twenty seconds. " +
        "Watch the rail: each hop is one agent finishing its part of the job.",
      action: { label: storming ? "Storm running…" : "Launch the storm", run: onRunStorm },
    },
    {
      title: "Read what the agents decided",
      say:
        "Open the first ticket. Every field a NOC analyst used to type is already filled, and every hop " +
        "says why: the subscriber thresholds, the HUB floor, the region-by-domain MSP matrix.",
      action: firstIncidentId ? { label: "Open the first ticket", run: () => nav(`/incidents/${firstIncidentId}`) } : undefined,
    },
    {
      title: "Approve what matters",
      say:
        "A P2 broadcast never leaves without a person. The inbox shows the SMS and the e-mail exactly as " +
        "they will be sent, with the facts beside them. Claim, read, approve.",
      action: { label: "Open the HITL inbox", run: () => nav("/hitl") },
    },
    {
      title: "Hand the shift over",
      say: "The ledger is already written. Generate the handover: owners, priorities and what the night shift must watch.",
      action: { label: "Open the shift desk", run: () => nav("/shift") },
    },
    {
      title: "Show the numbers",
      say: "Hours of toil taken off the floor, alarms absorbed before they became duplicate tickets, decisions kept human.",
      action: { label: "Open the showcase", run: () => nav("/showcase") },
    },
  ];
  const i = Math.min(step, steps.length - 1);
  const current = steps[i];

  return (
    <aside className="guide" role="dialog" aria-label="Guided demo">
      <div className="guide-head">
        <strong>Guided demo</strong>
        <span className="muted">
          step {i + 1} of {steps.length}
        </span>
        <button type="button" className="btn guide-close" onClick={onClose} aria-label="Close the guided demo">
          Close
        </button>
      </div>
      <h4 className="guide-title">{current.title}</h4>
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
