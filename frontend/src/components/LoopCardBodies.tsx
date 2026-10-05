import { Link } from "react-router-dom";
import { customerUpdateOf, possibleOutageOf, smsMetrics } from "../lib/hitl";
import { regionName } from "../lib/agents";
import { fmtClock, restoreSourceWord, surgeOriginWords } from "../lib/support";

/**
 * The bodies of the two close-the-loop approval cards (docs/CLOSE_THE_LOOP.md §5), in the
 * ApprovalCard idiom: the facts as a definition grid between hairlines, then what the decision
 * is about, unboxed except for the text itself. ApprovalCard keeps the head, the effect line,
 * the raw payload and the sticky decision footer.
 *
 *  - APPROVE_CUSTOMER_UPDATE: the exact SMS in English and in Kiswahili side by side, each with
 *    its segment count (the backend's gsm7 count when the payload has it, else estimated here),
 *    the language split, and a collapsed sample of the masked recipients.
 *  - CONFIRM_POSSIBLE_OUTAGE: the place, counts, times and origin, and what customers wrote
 *    (staff view: texts and references, never a number beyond its masked form).
 */

const LANGUAGE: Record<string, string> = { en: "English", sw: "Kiswahili", mixed: "English and Kiswahili" };
const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

function SmsMeta({ text, segments, recipients }: { text: string; segments: number | null; recipients: number | null }) {
  const est = smsMetrics(text);
  const segs = segments ?? est.segments;
  return (
    <>
      <span>
        {plural(segs, "segment")}
        {segments == null ? " (est.)" : ""}
      </span>
      <span>{est.encoding}</span>
      {est.offender && (
        <span className="loop-warn">
          forced by “{est.offender.char}” {est.offender.codepoint}
        </span>
      )}
      {recipients != null && <span>{plural(recipients, "customer")}</span>}
    </>
  );
}

export function CustomerUpdateBody({ task, headingId, check }: { task: any; headingId: string; check: string }) {
  const u = customerUpdateOf(task?.proposed_payload);
  const incidentId = typeof task?.incident_id === "string" ? task.incident_id : "";
  const incidentNumber = u.incidentNumber || (typeof task?.incident_number === "string" ? task.incident_number : "");
  const en = u.languages.en;
  const sw = u.languages.sw;
  const split = [en ? `${en} English` : "", sw ? `${sw} Kiswahili` : ""].filter(Boolean).join(", ");
  const restored = restoreSourceWord(u.restoreSource);
  // A language with nobody in it is not shown; with no split in the payload, every text is.
  const channels = [
    { key: "en", label: "SMS in English", text: u.textEn, segments: u.segmentsEn, n: en, lang: "en" },
    { key: "sw", label: "SMS in Kiswahili", text: u.textSw, segments: u.segmentsSw, n: sw, lang: "sw" },
  ].filter((c) => c.text && (c.n == null || c.n > 0));

  return (
    <>
      <dl className="hitl-facts">
        {incidentNumber && (
          <div className="hitl-fact">
            <dt>Incident</dt>
            <dd className="mono">{incidentId ? <Link className="link" to={`/incidents/${encodeURIComponent(incidentId)}`}>{incidentNumber}</Link> : incidentNumber}</dd>
          </div>
        )}
        {u.recipients != null && (
          <div className="hitl-fact">
            <dt>Customers</dt>
            <dd>{u.recipients}</dd>
          </div>
        )}
        {split && (
          <div className="hitl-fact">
            <dt>Languages</dt>
            <dd>{split}</dd>
          </div>
        )}
        {u.placeSummary && (
          <div className="hitl-fact">
            <dt>Places</dt>
            <dd>{u.placeSummary}</dd>
          </div>
        )}
        {restored && (
          <div className="hitl-fact">
            <dt>Restored</dt>
            <dd>{restored}</dd>
          </div>
        )}
      </dl>

      <div role="group" className="hitl-out" aria-labelledby={`${headingId}-out`}>
        <div className="hitl-section-head">
          <h3 id={`${headingId}-out`}>What goes out</h3>
          <span className="hitl-check-inline">{check}</span>
          <span>Shown for one customer; each gets their own reference</span>
        </div>
        {channels.length > 0 ? (
          <div className="hitl-channels">
            {channels.map((c) => (
              <div role="group" key={c.key} className="hitl-channel" aria-label={c.label}>
                <div className="hitl-channel-head">
                  <span className="hitl-channel-name">{c.label}</span>
                  <SmsMeta text={c.text} segments={c.segments} recipients={c.n} />
                </div>
                <pre className="pre hitl-channel-body full" tabIndex={0} data-keep-tab="" lang={c.lang} aria-label={`${c.label} text`}>
                  {c.text}
                </pre>
              </div>
            ))}
          </div>
        ) : (
          <div className="hitl-nochannel">No message text on this card. If you cannot see what the customers will read, reject.</div>
        )}
      </div>

      {u.sample.length > 0 && (
        <details className="hitl-raw loop-sample">
          <summary>
            Who gets it: {u.recipients != null && u.recipients > u.sample.length ? `${u.sample.length} of ${u.recipients} numbers` : plural(u.sample.length, "number")}, masked
          </summary>
          <ul className="loop-sample-list">
            {u.sample.map((r, i) => (
              <li key={`${r.ref}-${i}`}>
                <span className="mono">{r.ref || "—"}</span>
                <span className="mono">{r.msisdnMasked || "—"}</span>
                <span>{LANGUAGE[r.language] ?? (r.language || "—")}</span>
              </li>
            ))}
          </ul>
        </details>
      )}
    </>
  );
}

export function PossibleOutageBody({ task, headingId, check, profile }: { task: any; headingId: string; check: string; profile?: any }) {
  const o = possibleOutageOf(task?.proposed_payload);
  const region = o.regionCode ? regionName(o.regionCode, profile) : "";
  const first = o.firstAt ? fmtClock(o.firstAt) : "";
  const last = o.lastAt ? fmtClock(o.lastAt) : "";
  const origin = surgeOriginWords(o.origin, o.parentIncidentNumber);
  return (
    <>
      <dl className="hitl-facts">
        {o.place && (
          <div className="hitl-fact">
            <dt>Place</dt>
            <dd>{o.place}</dd>
          </div>
        )}
        {o.regionCode && (
          <div className="hitl-fact">
            <dt>Region</dt>
            <dd className={region === o.regionCode ? "mono" : undefined}>{region}</dd>
          </div>
        )}
        {o.complaints != null && (
          <div className="hitl-fact">
            <dt>Complaints</dt>
            <dd>{o.complaints}</dd>
          </div>
        )}
        {o.numbers != null && (
          <div className="hitl-fact">
            <dt>Numbers</dt>
            <dd>{o.numbers}</dd>
          </div>
        )}
        {(first || last) && (
          <div className="hitl-fact">
            <dt>First and last</dt>
            <dd className="mono">{first && last && first !== last ? `${first} to ${last}` : first || last}</dd>
          </div>
        )}
        <div className="hitl-fact wide">
          <dt>Origin</dt>
          <dd>{origin[0].toUpperCase() + origin.slice(1)}</dd>
        </div>
      </dl>

      <div role="group" className="hitl-out" aria-labelledby={`${headingId}-said`}>
        <div className="hitl-section-head">
          <h3 id={`${headingId}-said`}>What customers wrote</h3>
          <span className="hitl-check-inline">{check}</span>
        </div>
        {o.excerpts.length > 0 ? (
          <ul className="loop-excerpts">
            {o.excerpts.map((x, i) => (
              <li key={`${x.ref}-${i}`}>
                <blockquote className="loop-excerpt">{x.text}</blockquote>
                <span className="loop-excerpt-meta">
                  {x.ref && <span className="mono">{x.ref}</span>}
                  {x.at && <span className="mono">{fmtClock(x.at)}</span>}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <div className="hitl-nochannel">No excerpts on this card; decide from the counts above, or open the complaints on the Support desk.</div>
        )}
      </div>
    </>
  );
}
