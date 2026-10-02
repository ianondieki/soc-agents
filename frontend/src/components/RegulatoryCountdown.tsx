import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { isLaneOff } from "../lib/apiError";
import { IconAlert } from "../lib/icons";
import { fmtDateTime, parseInstant } from "../lib/time";
import { useIncidentRevision } from "../realtime/RealtimeContext";

/**
 * Regulatory countdown — `RegulatoryCountdown{due_at}` in spec §7.10, the workspace side of
 * §5.3.20 / §7.6.3. "A regulatory 24-hour countdown that exists only as JSON is not an
 * operational control" (CONFORMANCE C-18); this is the control.
 *
 * WHAT IT READS. `GET /api/v1/incidents/{id}/regulatory` — read-only and inert: calling it
 * opens no clock and raises no card. Each notice carries a server-computed `countdown` block
 * (`due_at`, `due_at_eat`, `minutes_remaining`, `overdue`, `next_threshold_hours`). The
 * remaining time is re-derived here from the absolute `due_at` every 30 s, so the number keeps
 * moving with the tab open and the WS quiet; the server's `minutes_remaining` is the fallback
 * if the instant cannot be parsed.
 *
 * THE ONE STATE THAT MUST BE IMPOSSIBLE TO MISS: `SEND_FAILED`. It means a regulator notice
 * was approved by a named human, released to the outbox, and did **not** go out — the
 * dispatcher reached a terminal non-SENT outcome, `sent_at` is NULL, and the Authority has
 * heard nothing. It renders as a full-width block at the top of the panel, in words
 * ("Regulator notice not sent"), with the dispatcher's recorded reason and whether the
 * deadline had already passed. `QUEUED` is the quieter sibling: approved, released, nothing
 * transmitted *yet* — the countdown still runs, because a queued notice that leaves after
 * the deadline is still a late notice.
 *
 * FLAG. `REGULATORY_ENABLED` defaults to false. Unlike the PIR and stop-clock lanes this read
 * does not 404 when off: it answers 200 with `enabled: false` and an empty list (the memory
 * panel's honesty rule — "off" must not read as "no obligation"). Either way this panel
 * renders **nothing at all** when the lane is off, so the workspace is unchanged in the
 * default configuration. Any other failure also renders nothing on a first load and keeps
 * the last good reading afterwards — it can never put an error box in an outage workspace.
 *
 * 3 a.m. rules (§7.10): every countdown shows the **absolute EAT deadline** beside the
 * relative time ("14 h 22 min left", "due 22 Sep, 09:00:00 EAT") — a relative time nobody can
 * check against a clock is how deadlines get missed; statuses are words, never colour alone;
 * nothing blinks.
 */

type Countdown = {
  notification_id: string;
  kind: string;
  status: string;
  clock_started_at: string | null;
  clock_started_at_eat?: string;
  due_at: string | null;
  due_at_eat?: string;
  minutes_remaining: number;
  hours_remaining?: number;
  overdue: boolean;
  thresholds_fired?: number[];
  next_threshold_hours: number | null;
  hitl_task_id: string | null;
};

type Dispatch = {
  attempt?: number;
  outbox_id?: string;
  outbox_status?: string;
  at?: string;
  provider?: string | null;
  error?: string | null;
  late?: boolean;
  reason_for_delay_recorded?: boolean;
};

type Notice = {
  id: string;
  kind: string;
  status: string;
  clock_started_at: string | null;
  due_at: string | null;
  approved_by: string | null;
  approved_at: string | null;
  sent_at: string | null;
  external_ref: string | null;
  hitl_task_id: string | null;
  significance: { dispatch?: Dispatch; rule_matched?: string | null; [k: string]: any } | null;
  countdown: Countdown | null;
};

type RegulatoryView = {
  incident_id: string;
  incident_number: string;
  enabled: boolean;
  degraded?: boolean;
  notifications: Notice[];
  significance: { significant: boolean; rule_matched: string | null; yaml_path?: string } | null;
};

/** The notice's name, and the rule it answers to (shown beside it as its own fact). */
const KIND_WORDS: Record<string, { name: string; ref?: string }> = {
  CA_OUTAGE_24H: { name: "CA 24-hour outage notice", ref: "licence Condition 9.2" },
  ODPC_BREACH_72H: { name: "ODPC 72-hour breach notice", ref: "DPA 2019 s.43" },
  CII_24H: { name: "Critical-infrastructure 24-hour notice" },
  CBK_FACTSHEET: { name: "CBK factsheet", ref: "M‑PESA-affecting outage" },
};

function kindName(kind: string): string {
  return KIND_WORDS[kind]?.name || kind;
}

/**
 * Words for every status the service can write, including the two it added (QUEUED, SEND_FAILED).
 * A chip only where the state is not the settled default: waiting for a decision (lavender),
 * queued, and not sent. Draft, sent and not required are plain words.
 */
const STATUS_WORDS: Record<string, { cls: string | null; word: string; note?: string }> = {
  DRAFT: { cls: null, word: "draft", note: "not yet sent for approval" },
  PENDING_APPROVAL: { cls: "chip hitl", word: "waiting for a decision", note: "on Approvals" },
  QUEUED: { cls: "chip accent", word: "queued", note: "approved, nothing transmitted yet" },
  SENT: { cls: null, word: "sent" },
  SEND_FAILED: { cls: "chip danger", word: "not sent", note: "the send failed" },
  NOT_REQUIRED: { cls: null, word: "not required" },
};

/** Statuses whose deadline is still live. SENT and NOT_REQUIRED are settled. */
const COUNTING = new Set(["DRAFT", "PENDING_APPROVAL", "QUEUED", "SEND_FAILED"]);

const TICK_MS = 30000;

/** "14 h 22 min" — a duration, so no timezone is involved; instants go through `fmtDateTime`. */
function fmtSpan(totalMinutes: number): string {
  const m = Math.abs(Math.trunc(totalMinutes));
  if (m < 60) return m + " min";
  const h = Math.floor(m / 60);
  const r = m % 60;
  return h + " h" + (r ? " " + r + " min" : "");
}

function minutesLeft(cd: Countdown, now: number): number {
  const due = parseInstant(cd.due_at);
  if (!due) return cd.minutes_remaining;
  return Math.floor((due.getTime() - now) / 60000);
}

export default function RegulatoryCountdown({ incidentId }: { incidentId?: string | null }) {
  const [view, setView] = useState<RegulatoryView | null>(null);
  const [now, setNow] = useState(() => Date.now());

  const load = useCallback(() => {
    if (!incidentId) return;
    api
      .incidentRegulatory(incidentId)
      .then((v: RegulatoryView) => setView(v))
      .catch((e) => {
        // 404: not ours / not there — render nothing. Anything else keeps the last reading.
        if (isLaneOff(e)) setView(null);
      });
  }, [incidentId]);

  useEffect(() => {
    setView(null);
  }, [incidentId]);

  // `regulatory.deadline` is incident-scoped in the renderer table, so a threshold crossing
  // bumps this incident's revision and the panel refetches without a page-level prop.
  const rev = useIncidentRevision(incidentId);
  useEffect(() => {
    load();
  }, [load, rev]);

  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), TICK_MS);
    return () => window.clearInterval(id);
  }, []);

  if (!incidentId || !view || !view.enabled) return null;

  const notices = view.notifications || [];
  const failed = notices.filter((n) => n.status === "SEND_FAILED");

  if (notices.length === 0) {
    const sig = view.significance;
    return (
      <div className="reg-strip">
        <span>No regulatory clock open</span>
        {sig?.significant && <span className="chip warn">significant</span>}
        <span className="muted">
          {sig == null
            ? "Significance could not be evaluated."
            : sig.significant
              ? "The significance rule matches this incident (" +
                (sig.rule_matched || "rule") +
                "), but no clock is open yet. Opening one is a supervisor decision; viewing this page never opens one."
              : "The significance rule does not match this incident, so no statutory notice is due."}
        </span>
      </div>
    );
  }

  return (
    <div className="panel reg-panel">
      {failed.map((n) => {
        const d = n.significance?.dispatch || {};
        return (
          <div key={"failed-" + n.id} className="reg-failed" role="alert">
            <div className="reg-failed-head">
              <span className="reg-failed-mark" aria-hidden="true">
                <IconAlert size={14} />
              </span>
              <span>Regulator notice not sent</span>
            </div>
            <div className="reg-failed-body">
              {kindName(n.kind)} was approved
              {n.approved_by ? " by " + n.approved_by : ""}
              {n.approved_at ? " at " + fmtDateTime(n.approved_at) + " EAT" : ""} and released to
              the outbox, but it <strong>did not go out</strong>. Nothing has reached the regulator.
            </div>
            <div className="reg-failed-facts">
              <span>
                Dispatcher outcome: <strong>{d.outbox_status || "unknown"}</strong>
                {d.at ? " at " + fmtDateTime(d.at) + " EAT" : ""}
              </span>
              {d.error ? (
                <span>
                  Reason recorded: <strong>{d.error}</strong>
                </span>
              ) : null}
              {d.late ? (
                <span>
                  <strong>The deadline had already passed</strong> when this failed
                  {d.reason_for_delay_recorded ? "" : " — no reason for delay is on record"}.
                </span>
              ) : null}
            </div>
            <div className="reg-failed-do">
              Until it is re-sent, the regulator has not been notified. A supervisor re-sends through{" "}
              <code>POST /api/v1/regulatory/{"{id}"}/send</code> — a failed attempt may be retried, and a
              send after the deadline must carry the reason for the delay.
            </div>
          </div>
        );
      })}

      <div className="panel-head">
        <h2 className="panel-title">Regulatory clock</h2>
        <span className="muted">times in EAT</span>
      </div>

      <div className="reg-list">
        {notices.map((n) => {
          const cd = n.countdown;
          const st = STATUS_WORDS[n.status] || { cls: "chip", word: n.status };
          const kind = KIND_WORDS[n.kind];
          const live = COUNTING.has(n.status) && cd != null;
          const left = live && cd ? minutesLeft(cd, now) : null;
          const overdue = left != null && left < 0;
          return (
            <div key={n.id} className={"reg-row" + (n.status === "SEND_FAILED" ? " failed" : "")}>
              <div className="reg-row-head">
                <strong>{kind?.name || n.kind}</strong>
                {kind?.ref && <span className="muted">{kind.ref}</span>}
                {st.cls ? <span className={st.cls}>{st.word}</span> : <span>{st.word}</span>}
                {st.note && <span className="muted">{st.note}</span>}
              </div>
              {live && cd ? (
                <div className="reg-countdown">
                  <div className={"reg-left" + (overdue ? " overdue" : "")}>
                    {overdue ? "Overdue by " + fmtSpan(left as number) : fmtSpan(left as number) + " left"}
                  </div>
                  {/* §7.10: the absolute deadline, always beside the relative one. */}
                  <div className="reg-due head-row">
                    <div>
                      due <strong>{fmtDateTime(cd.due_at)} EAT</strong>
                    </div>
                    <span className="muted">clock started {fmtDateTime(cd.clock_started_at)} EAT</span>
                    {cd.next_threshold_hours != null && !overdue ? (
                      <span className="muted">next alert at {cd.next_threshold_hours} h remaining</span>
                    ) : null}
                  </div>
                </div>
              ) : (
                <div className="facts">
                  {n.status === "SENT" ? (
                    <>
                      <span>Sent {fmtDateTime(n.sent_at)} EAT</span>
                      {n.external_ref ? <span>ref {n.external_ref}</span> : null}
                    </>
                  ) : n.status === "NOT_REQUIRED" ? (
                    <span>Ruled not required.</span>
                  ) : (
                    <span>No countdown available.</span>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
