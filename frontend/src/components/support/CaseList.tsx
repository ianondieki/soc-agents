import { memo } from "react";
import { CATEGORY_WORD, fmtAge, fmtDue, minutesUntil, reasonWord, withPerson, type Complaint } from "../../lib/support";
import { RouteMark, StatusWord } from "./marks";

/**
 * The queue's left column: one button per case, two lines each (reference and age; subject),
 * then its facts (category, route, status). In the "Needs a person" view a third line says why
 * it is with a person and when the reply is due. The selected row carries the selection tint.
 */
export interface CaseListProps {
  items: Complaint[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  /** The "Needs a person" view: oldest first, with the reason and the SLA. */
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
        return (
          <li key={c.id}>
            <button type="button" className="sd-row" aria-current={selected ? "true" : undefined} onClick={() => onSelect(c.id)}>
              <span className="sd-row-ref mono">{c.ref}</span>
              <span className="sd-row-age" title={person ? "Waiting for a person" : "Received"}>
                {fmtAge(since)}
              </span>
              <span className="sd-row-subject">{c.subject || c.body}</span>
              <span className="sd-row-facts">
                <span>{CATEGORY_WORD[c.category] ?? c.category}</span>
                <RouteMark route={c.route} />
                <StatusWord status={c.status} claimedBy={c.escalation?.claimed_by} />
              </span>
              {person && withPerson(c.status) && (
                <span className="sd-row-why">
                  {c.status === "awaiting_approval" ? "a tool call needs approval" : reasonWord(c.escalation?.reason_code)}
                  <span className={"sd-row-due" + dueTone(c.sla_due_at)}>
                    {minutesUntil(c.sla_due_at) != null && minutesUntil(c.sla_due_at)! < 0 ? "overdue, was due " : "due "}
                    <span className="mono">{fmtDue(c.sla_due_at)}</span>
                  </span>
                </span>
              )}
            </button>
          </li>
        );
      })}
    </ol>
  );
});

export default CaseList;
