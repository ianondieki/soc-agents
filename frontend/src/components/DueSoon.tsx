import { useMemo } from "react";
import { Link } from "react-router-dom";
import { AlarmClock } from "lucide-react";
import { priorityTitle } from "../lib/agents";
import { isOpenTicket } from "../lib/ticket";
import { fmtHM, parseInstant } from "../lib/time";
import { useMinute } from "../lib/useMinute";

/** How far ahead a restore deadline is worth a line (the bell's `restore_due_soon` uses the same). */
export const DUE_SOON_MS = 60 * 60_000;
/** The band names this many tickets; the rest are one link away. */
const SHOWN = 4;

/** Open tickets whose restore deadline falls within the next hour, soonest first. */
export function dueSoon(incidents: any[], nowMs: number): any[] {
  return incidents
    .filter((i) => i && isOpenTicket(i.status))
    .map((i) => ({ i, due: parseInstant(i.sla_restore_due)?.getTime() ?? NaN }))
    .filter(({ due }) => Number.isFinite(due) && due > nowMs && due <= nowMs + DUE_SOON_MS)
    .sort((a, b) => a.due - b.due)
    .map(({ i }) => i);
}

/** "in 23 min", "in under a minute". */
function leftWords(dueIso: unknown, nowMs: number): string {
  const due = parseInstant(dueIso)?.getTime();
  if (due == null) return "";
  const min = Math.floor((due - nowMs) / 60_000);
  return min < 1 ? "in under a minute" : `in ${min} min`;
}

/**
 * The warning before the breach, on Mission control: the tickets whose restore deadline runs out
 * within the hour, soonest first, each with its countdown and a link to the ticket. Shown only
 * when there is one; nothing to warn about takes no room. The countdown moves on the minute.
 */
export default function DueSoon({ incidents }: { incidents: any[] }) {
  const nowMs = useMinute().getTime();
  const list = useMemo(() => dueSoon(incidents, nowMs), [incidents, nowMs]);
  if (list.length === 0) return null;
  const more = list.length - SHOWN;
  return (
    <section className="due-soon" aria-labelledby="due-soon-title">
      <h2 id="due-soon-title" className="due-soon-title">
        <AlarmClock size={18} strokeWidth={2} aria-hidden="true" />
        {list.length === 1
          ? "1 ticket reaches its restore deadline within the hour"
          : `${list.length} tickets reach their restore deadline within the hour`}
      </h2>
      <ul className="due-soon-list">
        {list.slice(0, SHOWN).map((i) => (
          <li key={i.id}>
            <Link to={`/incidents/${i.id}`} className="due-soon-row">
              <span className={`pill ${i.priority}`} title={priorityTitle(i.priority)}>
                {i.priority}
              </span>
              <span className="due-soon-site">{i.site_name || i.site_id || i.incident_number}</span>
              <span className="due-soon-left">
                <strong>{leftWords(i.sla_restore_due, nowMs)}</strong>
                <span className="muted"> at {fmtHM(i.sla_restore_due)}</span>
              </span>
            </Link>
          </li>
        ))}
      </ul>
      {more > 0 && (
        <p className="due-soon-more">
          and {more} more. <Link to="/incidents">The Incident board</Link> lists every open ticket.
        </p>
      )}
    </section>
  );
}
