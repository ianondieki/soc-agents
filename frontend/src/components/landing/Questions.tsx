import { Plus } from "lucide-react";

/** What the floor asks in its first week, answered from how the console works. */
const FAQ: { q: string; a: string }[] = [
  {
    q: "Does an agent ever send a message on its own?",
    a: "Only where the autonomy level allows it. At L2 guarded, P3 and P4 broadcasts go on their own; every P1 and P2 message waits at Approval until a named person approves or rejects it. Wording that reaches management or leaves the building on a P1 or P2 is never automated, at any level.",
  },
  {
    q: "Where is the record kept?",
    a: "Every step an agent takes is written to the audit trail with the agent, the time and its reason. A person's decision stays on the approval with their name and the time, and as a note on the ticket's timeline. Each ticket's row also goes into the Excel shift ledger, in EAT, and the Workflow map reads any recent alarm back step by step.",
  },
  {
    q: "How are the minutes saved worked out?",
    a: "From the floor's own estimate of a person's minutes per step, set in the operator profile, multiplied by the steps the agents completed. It is a model to be corrected with the floor, not a stopwatch study, and the Showcase shows the sum step by step.",
  },
  {
    q: "How is the Support desk scored?",
    a: "On a labelled set of complaints in English, Kiswahili and Sheng, against fixed gates for resolution, wrong escalations and missed escalations on safety cases. The headline is a blind holdout written by someone who never saw the code.",
  },
  {
    q: "Can the floor turn the noise down at night?",
    a: "Yes. Quiet mode, in the Display menu, stops animations and routine ticker lines while P1 and P2 tickets and decisions stay live. Alerts can buzz and sound for every event, for alarms only, or not at all.",
  },
  {
    q: "Is this a live operator system?",
    a: "No. It is a demo and training product. It is not an official Safaricom or Airtel system, and its sites, tickets and complaints are test data.",
  },
];

/** The questions as an accordion: a plus that turns to a cross opens each answer. */
export default function Questions() {
  return (
    <div className="ld-faq">
      {FAQ.map((f) => (
        <details key={f.q} className="ld-faq-item">
          <summary>
            <span>{f.q}</span>
            <Plus className="ld-faq-mark" size={18} strokeWidth={2} aria-hidden="true" />
          </summary>
          <div className="ld-faq-a">
            <p>{f.a}</p>
          </div>
        </details>
      ))}
    </div>
  );
}
