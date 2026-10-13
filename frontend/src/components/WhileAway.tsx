import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { History } from "lucide-react";
import { api } from "../api";
import { awayFacts, awaySince, dismissAway } from "../lib/away";
import { lastedWords } from "../lib/sitrep";
import { fmtHM } from "../lib/time";

const DAY = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "13:05 EAT" today, "Mon 12 Oct, 18:40 EAT" on an earlier day. */
function sinceWords(ms: number, now: number): string {
  const eat = (t: number) => new Date(t + 3 * 3_600_000);
  const a = eat(ms);
  const sameDay = a.toISOString().slice(0, 10) === eat(now).toISOString().slice(0, 10);
  const hm = `${fmtHM(new Date(ms).toISOString())} EAT`;
  return sameDay ? hm : `${DAY[a.getUTCDay()]} ${a.getUTCDate()} ${MON[a.getUTCMonth()]}, ${hm}`;
}

function names(list: any[], max = 3) {
  const shown = list.slice(0, max);
  const more = list.length - shown.length;
  return (
    <>
      {shown.map((i, n) => (
        <span key={i.id}>
          {n > 0 && (n === shown.length - 1 && more === 0 ? " and " : ", ")}
          <Link to={`/incidents/${i.id}`}>{i.site_name || i.site_id || i.incident_number}</Link>
        </span>
      ))}
      {more > 0 && ` and ${more} more`}
    </>
  );
}

const and = (xs: string[]) => (xs.length <= 1 ? xs[0] ?? "" : `${xs.slice(0, -1).join(", ")} and ${xs[xs.length - 1]}`);

/**
 * Mission control's catch-up, shown when this person comes back after a while (lib/away.ts): what
 * opened, what was restored or closed, which cards were decided and by whom, and which restore
 * deadlines ran out, since the moment they last had the console open. "Got it" puts it away.
 * `incidents` is null until the page has its ticket list, so the band never reads "nothing changed"
 * before it knows.
 */
export default function WhileAway({ incidents }: { incidents: any[] | null }) {
  const [since, setSince] = useState<number | null>(() => awaySince().since);
  useEffect(() => awaySince(setSince).stop, []);
  const [audit, setAudit] = useState<any[] | null>(null);
  useEffect(() => {
    if (since == null) return;
    let live = true;
    api
      .audit(500)
      .then((rows) => live && setAudit(Array.isArray(rows) ? rows : []))
      .catch(() => live && setAudit([])); // a role the trail refuses: the decisions line is left out
    return () => {
      live = false;
    };
  }, [since]);
  const now = useMemo(() => Date.now(), [since, incidents, audit]);
  if (since == null || incidents == null || audit == null) return null;
  const f = awayFacts(incidents, audit, since, now);
  const decided = f.approved + f.rejected;
  const lines: React.ReactNode[] = [];
  if (f.opened.length) {
    lines.push(
      <>
        {f.opened.length === 1 ? "1 ticket opened: " : `${f.opened.length} tickets opened: `}
        {names(f.opened)}.
      </>
    );
  }
  const fixed = [f.restored ? `${f.restored} restored` : "", f.closed ? `${f.closed} closed` : ""].filter(Boolean);
  if (fixed.length) lines.push(<>{`${and(fixed)}.`.replace(/^./, (c) => c.toUpperCase())}</>);
  if (decided) {
    const how = [f.approved ? `${f.approved} approved` : "", f.rejected ? `${f.rejected} rejected` : ""].filter(Boolean);
    lines.push(
      <>
        {decided === 1 ? "1 card decided" : `${decided} cards decided`}: {and(how)}
        {f.deciders.length ? `, by ${and(f.deciders)}` : ""}.
      </>
    );
  }
  if (f.breached.length) {
    lines.push(
      <>
        {f.breached.length === 1 ? "1 restore deadline ran out: " : `${f.breached.length} restore deadlines ran out: `}
        {names(f.breached)}.
      </>
    );
  }
  return (
    <section className="while-away" aria-labelledby="while-away-title">
      <div className="while-away-head">
        <h2 id="while-away-title" className="while-away-title">
          <History size={18} strokeWidth={2} aria-hidden="true" />
          While you were away
        </h2>
        <p className="while-away-when">
          Since {sinceWords(since, now)}, {lastedWords(now - since)} ago
        </p>
      </div>
      {lines.length ? (
        <ul className="while-away-list">
          {lines.map((l, n) => (
            <li key={n}>{l}</li>
          ))}
        </ul>
      ) : (
        <p className="while-away-none">Nothing changed: no ticket opened, restored or closed, and no card was decided.</p>
      )}
      <button type="button" className="btn sm while-away-done" onClick={dismissAway}>
        Got it
      </button>
    </section>
  );
}
