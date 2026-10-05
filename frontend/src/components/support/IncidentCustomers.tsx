import { useCallback, useEffect, useId, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { humanStatus } from "../../lib/agents";
import { IconDot } from "../../lib/icons";
import {
  errorDetail,
  fmtClock,
  isLaneOff,
  isNetworkError,
  noticeWords,
  outageStatusWord,
  supportApi,
  withPerson,
  type IncidentCustomers,
} from "../../lib/support";
import { useRealtimeState } from "../../realtime/RealtimeContext";
import "./IncidentCustomers.css";
import SendUpdateNow from "./SendUpdateNow";

/**
 * "Customers on this outage", the incident page's view of the complaints linked to it
 * (docs/CLOSE_THE_LOOP.md §4): how many customers, how many were told, are waiting or say it is
 * still down, the customer notice in words, and the complaints themselves (reference, masked
 * number, status), the first five shown and the rest one press away.
 *
 * The panel is absent, not empty, when nothing is linked or the desk is off (404). It renders
 * nothing while the first answer is on its way: it sits below the fold and may not exist at all,
 * so a skeleton that then vanished would only move the page.
 */

export interface CustomersState {
  state: "loading" | "ok" | "off" | "error";
  data: IncidentCustomers | null;
  error: string;
  reload: () => void;
  /** An answer the page already has (the customer-update POST returns this payload). */
  apply: (d: IncidentCustomers) => void;
}

/** The incident's customers, refetched with the incident's own revision and the desk's. */
export function useIncidentCustomers(incidentId: string | undefined, incidentRev: number): CustomersState {
  const rt = useRealtimeState();
  const supportRev = rt?.revisions.support ?? 0;
  const [state, setState] = useState<CustomersState["state"]>("loading");
  const [data, setData] = useState<IncidentCustomers | null>(null);
  const [error, setError] = useState("");
  const asked = useRef(0);

  const reload = useCallback(() => {
    if (!incidentId) return;
    const mine = ++asked.current;
    supportApi
      .incidentCustomers(incidentId)
      .then((d) => {
        if (mine !== asked.current) return;
        setData(d && typeof d === "object" ? d : null);
        setState("ok");
        setError("");
      })
      .catch((e) => {
        if (mine !== asked.current) return;
        if (isLaneOff(e)) {
          setData(null);
          setState("off");
          return;
        }
        setError(isNetworkError(e) ? "The API is unreachable." : errorDetail(e, "The request failed."));
        setState((s) => (s === "ok" ? s : "error"));
      });
  }, [incidentId]);

  useEffect(reload, [reload, incidentRev, supportRev]);
  const apply = useCallback((d: IncidentCustomers) => {
    asked.current += 1; // an older answer still in flight must not overwrite this one
    setData(d && typeof d === "object" ? d : null);
    setState("ok");
    setError("");
  }, []);
  return { state, data, error, reload, apply };
}

/** True when the panel has something to show. */
export function hasCustomers(s: CustomersState): boolean {
  const d = s.data;
  return s.state === "ok" && !!d && ((Array.isArray(d.complaints) && d.complaints.length > 0) || d.customers > 0);
}

const FIRST = 5;

export default function IncidentCustomersPanel({ incidentId, s, restored }: { incidentId: string; s: CustomersState; restored: boolean }) {
  const headId = useId();
  const [all, setAll] = useState(false);
  const firstHiddenRef = useRef<HTMLAnchorElement>(null);
  const wasAll = useRef(false);

  // Showing the rest puts focus on the first complaint that was hidden, so the keyboard continues there.
  useEffect(() => {
    if (all && !wasAll.current) firstHiddenRef.current?.focus();
    wasAll.current = all;
  }, [all]);

  if (s.state === "error" && !s.data) {
    return (
      <section className="panel" aria-labelledby={headId}>
        <h2 id={headId} className="panel-title">
          Customers on this outage
        </h2>
        <div className="empty" role="alert">
          Couldn't load the customers linked to this ticket. {s.error}
          <button type="button" className="btn sm" onClick={s.reload}>
            Retry
          </button>
        </div>
      </section>
    );
  }
  if (!hasCustomers(s) || !s.data) return null;

  const d = s.data;
  const complaints = Array.isArray(d.complaints) ? d.complaints : [];
  const shown = all ? complaints : complaints.slice(0, FIRST);
  const hidden = complaints.length - FIRST;
  const notice = noticeWords(d.notice);
  // A customer who says it is still down outranks "sent": the notice reads in the watch colour.
  const noticeTone = d.still_down > 0 ? "warn" : notice.tone;
  const deskLink = `/support?incident=${encodeURIComponent(incidentId)}`;
  const noticeId = `ic-notice-${incidentId}`;
  // §7.1: a held-back update can go again; so can one never written once nobody is left waiting on a fix.
  const sendable = d.waiting > 0 && (d.notice?.state === "held_back" || (d.notice?.state === "none" && restored));
  const follow = d.follow_up && d.follow_up.incident_id ? d.follow_up : null;

  return (
    <section className="panel ic-panel" aria-labelledby={headId}>
      <div className="panel-head">
        <h2 id={headId} className="panel-title">
          Customers on this outage
        </h2>
        <Link to={deskLink} className="btn sm">
          Open in Support desk
        </Link>
      </div>

      <dl className="ic-counts">
        <div>
          <dt>Customers</dt>
          <dd>{d.customers}</dd>
        </div>
        <div className={d.told ? undefined : "zero"}>
          <dt>Told</dt>
          <dd>{d.told}</dd>
        </div>
        <div className={d.waiting ? undefined : "zero"}>
          <dt>Waiting</dt>
          <dd>{d.waiting}</dd>
        </div>
        <div className={d.still_down ? "warn" : "zero"}>
          <dt>Still down</dt>
          <dd>
            {d.still_down > 0 ? (
              <span className="attn warn">
                <IconDot />
                {d.still_down}
              </span>
            ) : (
              0
            )}
          </dd>
        </div>
      </dl>

      <div className="ic-notice">
        <span className="ic-notice-label">Customer notice</span>
        {notice.to ? (
          <Link to={notice.to} id={noticeId} tabIndex={-1} className={"ic-notice-state" + (noticeTone ? ` ${noticeTone}` : "")}>
            {notice.text}
          </Link>
        ) : (
          <span id={noticeId} tabIndex={-1} className={"ic-notice-state" + (noticeTone ? ` ${noticeTone}` : "")}>
            {notice.text}
          </span>
        )}
        {notice.sub && <span className="muted">{notice.sub}</span>}
        {sendable && <SendUpdateNow incidentId={incidentId} focusId={noticeId} onDone={s.apply} />}
      </div>

      {follow && (
        <p className="ic-follow">
          <span className="ic-notice-label">Follow-up ticket</span>
          <Link to={`/incidents/${encodeURIComponent(follow.incident_id)}`} className="mono ic-ref">
            {follow.incident_number}
          </Link>
          <span className="muted">opened from still-down reports{follow.status ? `, ${humanStatus(follow.status)}` : ""}</span>
        </p>
      )}

      {complaints.length > 0 && (
        <>
          <ul className="ic-list" aria-label="Complaints linked to this ticket">
            {shown.map((c, i) => {
              const person = withPerson(c.status);
              const when = c.still_down_at ? `still down ${fmtClock(c.still_down_at)}` : c.told_restored_at ? `told ${fmtClock(c.told_restored_at)}` : "";
              return (
                <li key={c.id || c.ref} className="ic-row">
                  <Link
                    to={`/support?case=${encodeURIComponent(c.id)}`}
                    className="mono ic-ref"
                    ref={i === FIRST ? firstHiddenRef : undefined}
                  >
                    {c.ref}
                  </Link>
                  <span className="mono ic-num">{c.msisdn_masked}</span>
                  {/* Where this customer stands on the outage, in plain words (not the desk's "Action taken"). */}
                  <span className={"ic-status" + (person ? " hitl" : "")}>{outageStatusWord(c)}</span>
                  <span className={"ic-when" + (c.still_down_at ? " warn" : "")}>{when}</span>
                </li>
              );
            })}
          </ul>
          {hidden > 0 && (
            <button type="button" className="btn ghost sm ic-more" aria-expanded={all} onClick={() => setAll((v) => !v)}>
              {all ? "Show the first five" : `Show all ${complaints.length} complaints`}
            </button>
          )}
        </>
      )}
    </section>
  );
}
