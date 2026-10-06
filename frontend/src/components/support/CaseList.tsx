import { memo } from "react";
import { CATEGORY_WORD, ROUTE_WORD, fmtAge, fmtDue, minutesUntil, needsPerson, reasonWord, statusWordOf, type Complaint } from "../../lib/support";
import { ROUTE_ICON, statusTone } from "./marks";

/**
 * The queue's left column: one button per case, two lines each (reference and age; subject),
 * then its facts: the category and the status. Violet only when a person must act now, with the
 * reply's due time beside it. In the "Needs a person" view a third line says why.
 */
export interface CaseListProps {
  items: Complaint[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  /** The "Needs a person" view: oldest first, with the reason. */
  person?: boolean;
  ariaLabel: string;
}

function dueTone(iso: string): string {
  const m = minutesUntil(iso);
  if (m == null) return "";
  if (m < 0) return " danger";
  if (m <= 60) return " warn";
  return "";
}

const CaseList = memo(function CaseList({ items, selectedId, onSelect, person = false, ariaLabel }: CaseListProps) {
  return (
    <ol className="sd-rows" aria-label={ariaLabel}>
      {items.map((c) => {
        const selected = c.id === selectedId;
        const since = person && c.escalation?.at ? c.escalation.at : c.created_at;
        const act = needsPerson(c.status);
        const overdue = (minutesUntil(c.sla_due_at) ?? 1) < 0;
        const status = statusWordOf(c);
        const tone = statusTone(c.status);
        const RouteIcon = ROUTE_ICON[c.route];
        return (
          <li key={c.id}>
            <button type="button" className="sd-row" data-id={c.id} aria-current={selected ? "true" : undefined} onClick={() => onSelect(c.id)}>
              {/* How the desk handled it, at a glance: the resolver, the action agent or a person. */}
              <span className={"sd-row-mark" + (c.route === "human" ? " human" : "")} title={ROUTE_WORD[c.route]} aria-hidden="true">
                {RouteIcon ? <RouteIcon size={14} strokeWidth={1.75} /> : null}
              </span>
              <span className="sd-row-ref mono">{c.ref}</span>
              <span className="sd-row-age" title={person ? "Waiting for a person" : "Received"}>
                {fmtAge(since)}
              </span>
              <span className="sd-row-subject">{c.subject || c.body}</span>
              <span className="sd-row-facts">
                <span className={"sd-pill sm" + (tone ? ` ${tone}` : "")}>{status}</span>
                <span>{CATEGORY_WORD[c.category] ?? c.category}</span>
                {act && (
                  <span className={"sd-row-due" + dueTone(c.sla_due_at)}>
                    {overdue ? "Overdue, was due" : "Due"} {fmtDue(c.sla_due_at)}
                  </span>
                )}
              </span>
              {person && c.escalation && <span className="sd-row-why">{c.status === "awaiting_approval" ? "a tool call needs approval" : reasonWord(c.escalation.reason_code)}</span>}
            </button>
          </li>
        );
      })}
    </ol>
  );
});

export default CaseList;
