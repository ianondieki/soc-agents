import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useParams } from "react-router-dom";
import { api } from "../api";
import AgentRail from "../components/AgentRail";
import { RunState } from "../components/LiveRunPanel";
import {
  MPESA_TITLE,
  WAITING_WORD,
  agentDisplayName,
  audienceWord,
  channelWord,
  countAbsorbed,
  displaySteps,
  fmtMs,
  fmtWait,
  humanEnum,
  humanStatus,
  nodeLabel,
  pickCreatingRun,
  priorityTitle,
  sumDurations,
} from "../lib/agents";
import { detailOf } from "../lib/apiError";
import { parseRationale } from "../lib/audit";
import { IconAlert, IconCheck, IconDot, IconPause } from "../lib/icons";
import EarlierAtThisSite from "../components/EarlierAtThisSite";
import ContractsDrawer from "../components/ContractsDrawer";
import RegulatoryCountdown from "../components/RegulatoryCountdown";
import StopClockPanel from "../components/StopClockPanel";
import { fmtDateTime } from "../lib/time";
import { useIncidentRevision } from "../realtime/RealtimeContext";
import IncidentCustomersPanel, { useIncidentCustomers } from "../components/support/IncidentCustomers";
import { noticeWaitsForPerson } from "../lib/support";

/** Non-breaking space: a fact in the head ("RNIO RNIO-RFT") wraps as a whole, never inside. */
const NB = " ";

const capFirst = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s);

/** "SMS → RNIO" → "SMS to RNIO": the arrow is a word on this page. */
const arrowsToWords = (s: unknown) => String(s ?? "").replace(/\s*→\s*/g, " to ");

/**
 * A rationale value as a person reads it: "80000 to P3" → "80,000 to P3", "TX floor=P3" →
 * "TX, floor P3", "true (CORE/HUB corridor rule)" → "yes (CORE/HUB corridor rule)",
 * "tx_mw" → "TX MW". The stored text is untouched; only the rendering changes.
 */
function tidyValue(value: string): string {
  let s = arrowsToWords(value);
  s = s.replace(/\s+([A-Za-z_]+)=/g, ", $1 ");
  if (/^\d{5,}$/.test(s) && !s.startsWith("0")) s = Number(s).toLocaleString("en-KE");
  s = s.replace(/^true\b/, "yes").replace(/^false\b/, "no");
  if (/^[a-z]+(?:_[a-z]+)+$/.test(s)) s = humanEnum(s.toUpperCase());
  return s;
}

/** "MW,4G" → "MW, 4G": the technology list as written, with room to read it. */
const techList = (v: unknown) =>
  String(v ?? "")
    .split(",")
    .map((t) => t.trim())
    .filter(Boolean)
    .join(", ");

/** "02 Oct, 05:16": the timeline's clock, without the seconds. */
const shortTime = (ts: unknown) => fmtDateTime(ts, "").replace(/(\d{2}:\d{2}):\d{2}$/, "$1");

/** A stored UTC timestamp inside a sentence ("SLA ack due 2026-10-02 05:16:22.085241"). */
const STORED_INSTANT = /\b\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b/g;

/** Stored text as the page prints it: every embedded timestamp in EAT, like the rest of the page.
 *  The stored row is untouched. */
const eatTimes = (s: string) => s.replace(STORED_INSTANT, (m) => shortTime(m) || m);

/** A stored rationale ("Severity why", "Assignment why") as a sentence and a list of facts. */
function Why({ title, text }: { title: string; text: string | null | undefined }) {
  const { sentence, facts } = parseRationale(text);
  if (!sentence && facts.length === 0) return null;
  return (
    <div>
      <h3 className="panel-title">{title}</h3>
      {sentence && <p className="muted">{capFirst(tidyValue(sentence))}</p>}
      {facts.length > 0 && (
        <dl className="rail-dl">
          {facts.map((f, k) => (
            <Fragment key={f.key + k}>
              <dt>{f.label}</dt>
              <dd>{tidyValue(f.value)}</dd>
            </Fragment>
          ))}
        </dl>
      )}
    </div>
  );
}

type Msg = { tone: "ok" | "danger"; text: string } | null;

/** An action's outcome, under the control that caused it: done is quiet, a failure says so in red. */
function Outcome({ msg }: { msg: Msg }) {
  if (!msg) return null;
  return msg.tone === "danger" ? (
    <span className="state danger" role="alert">
      <IconAlert /> {msg.text}
    </span>
  ) : (
    <span className="muted" role="status">
      <IconCheck /> {msg.text}
    </span>
  );
}

/** One timeline entry's title, second word and state, from the stored `kind`/`title`/`status`. */
function timelineHead(t: any): { title: string; sub: string; state: ReactNode } {
  const kind = String(t.kind || "");
  const raw = String(t.title || "");
  if (kind === "agent_step") {
    // Stored as "IngestCorrelationAgent · INGEST".
    const [agent, node] = raw.split(" · ");
    return {
      title: node ? nodeLabel(node) : raw,
      sub: node ? agentDisplayName(agent) : "",
      state: <RunState status={t.status} routine={false} />,
    };
  }
  if (kind === "note") {
    // Stored as "Author (ROLE)"; an agent's note does not need "(AGENT)" after its name.
    const m = /^(.*) \(([^)]+)\)$/.exec(raw);
    const author = m ? m[1] : raw;
    const role = m ? m[2].toUpperCase() : "";
    const isAgent = role === "AGENT";
    return {
      title: isAgent ? agentDisplayName(author) || author : author,
      sub: isAgent || !role ? "note" : `${humanEnum(role)} note`,
      state: null,
    };
  }
  if (kind === "broadcast") {
    // Stored as "EMAIL → FIELD_ENGINEER": read as "Email to field engineer".
    const m = /^\s*([A-Za-z_]+)\s*→\s*([A-Za-z_]+)\s*$/.exec(raw);
    if (m) {
      return {
        title: `${channelWord(m[1])} to ${audienceWord(m[2])}`,
        sub: "",
        state: t.status ? <span className="muted">{humanStatus(t.status)}</span> : null,
      };
    }
  }
  return {
    title: arrowsToWords(raw),
    sub: "",
    state: t.status ? <span className="muted">{humanStatus(t.status)}</span> : null,
  };
}

/** A timeline detail: a stored rationale reads as its sentence and facts; anything else as written. */
function TimelineDetail({ text }: { text: unknown }) {
  const s = typeof text === "string" ? eatTimes(text.trim()) : "";
  if (!s) return null;
  const { sentence, facts } = parseRationale(s);
  if (facts.length === 0) return <div className="muted">{arrowsToWords(s)}</div>;
  return (
    <div className="facts">
      {sentence && <span>{capFirst(tidyValue(sentence))}</span>}
      {facts.map((f, k) => (
        <span key={f.key + k}>
          <span className="muted dim">{f.label}</span> {tidyValue(f.value)}
        </span>
      ))}
    </div>
  );
}

function Skeleton({ rows, label }: { rows: number; label: string }) {
  return (
    <div role="status">
      <span className="sr-only">{label}</span>
      <div className="skeleton-rows" aria-hidden="true">
        {Array.from({ length: rows }, (_, i) => (
          <span key={i} className="skeleton" />
        ))}
      </div>
    </div>
  );
}

/**
 * One incident. Keyed by the id in the URL, so moving from one ticket to another starts from
 * a clean page (no fields, forms or messages carried over) and an answer that arrives for the
 * ticket just left is dropped with the instance that asked for it.
 */
export default function IncidentWorkspace({ session, profile }: { session: any; profile?: any }) {
  const { id } = useParams();
  return <Workspace key={id || ""} id={id} session={session} profile={profile} />;
}

function Workspace({ id, session, profile }: { id: string | undefined; session: any; profile?: any }) {
  // Defect #26: this page reloads when an event names *this* incident (or when
  // an unrecognised event forces a full resync), not on every WS frame.
  const rev = useIncidentRevision(id);
  // The customers whose complaints are linked to this ticket (docs/CLOSE_THE_LOOP.md §4).
  const customers = useIncidentCustomers(id, rev);
  const [inc, setInc] = useState<any>(null);
  const [wf, setWf] = useState<any>(null);
  // null until the first answer, so the timeline never reads as empty while it loads.
  const [timeline, setTimeline] = useState<any[] | null>(null);
  const [timelineFailed, setTimelineFailed] = useState(false);
  // null until the first answer: "no run recorded" is said only once the runs have answered.
  const [runs, setRuns] = useState<any[] | null>(null);
  const [runsFailed, setRunsFailed] = useState(false);
  const [brief, setBrief] = useState<string>("");
  const [note, setNote] = useState("");
  const [vendorRef, setVendorRef] = useState("");
  const [mspRoot, setMspRoot] = useState("");
  const [mspAction, setMspAction] = useState("");
  const [mspPct, setMspPct] = useState("");
  const [markRestored, setMarkRestored] = useState(false);
  const [noteBusy, setNoteBusy] = useState(false);
  const [noteMsg, setNoteMsg] = useState<Msg>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  // The narrative panel's actions (close, reassign) report under themselves, not in the note form.
  const [actionMsg, setActionMsg] = useState<Msg>(null);
  const [closeBusy, setCloseBusy] = useState(false);
  // Reassign is an inline form in the narrative panel (no browser prompt): who, and why.
  const [reassigning, setReassigning] = useState(false);
  const [reassignMsp, setReassignMsp] = useState("");
  const [reassignReason, setReassignReason] = useState("");
  const [reassignBusy, setReassignBusy] = useState(false);

  // Answers that land after the page has moved on (another ticket, or away) are dropped.
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  // Expected failures (a lane that is off answers 404, a brief not written yet) are states on
  // the page, never console errors.
  const load = useCallback(() => {
    if (!id) return;
    const mine = <T,>(f: (v: T) => void) => (v: T) => {
      if (alive.current) f(v);
    };
    api
      .incident(id)
      .then(
        mine((row: any) => {
          setInc(row);
          setLoadFailed(false);
        })
      )
      .catch(mine(() => setLoadFailed(true)));
    api
      .workflow(id)
      .then(mine(setWf))
      .catch(() => undefined);
    api
      .timeline(id)
      .then(
        mine((r: any) => {
          setTimeline(Array.isArray(r) ? r : []);
          setTimelineFailed(false);
        })
      )
      .catch(mine(() => setTimelineFailed(true)));
    // The run that opened the ticket, not the newest (usually a two-step merge): lib/agents.
    api
      .runsFor(id)
      .then(
        mine((r: any) => {
          setRuns(Array.isArray(r) ? r : []);
          setRunsFailed(false);
        })
      )
      .catch(mine(() => setRunsFailed(true)));
    api
      .brief(id)
      .then(mine((b: any) => setBrief(b?.body || "")))
      .catch(mine(() => setBrief("")));
  }, [id]);

  useEffect(load, [load, rev]);

  const creating = useMemo(() => pickCreatingRun(runs), [runs]);
  const absorbed = useMemo(() => countAbsorbed(runs), [runs]);
  // Decided hops read as decided (the backend leaves them at WAITING_HITL), and the Approval hop
  // carries how long the run waited for a person: lib/agents.displaySteps.
  const railSteps = useMemo(
    () => (creating ? displaySteps(creating) : wf?.steps ? displaySteps({ status: wf.status, steps: wf.steps }) : []),
    [creating, wf]
  );
  // One time for one run: what the agents worked (the sum of the step durations), and, apart
  // from it, the time spent waiting for a decision.
  const worked = sumDurations(railSteps);
  const approvalHop = railSteps.find((st) => st.node_name === "HITL" && typeof st.waited_ms === "number");
  const stillWaiting = String(creating?.status || "").toUpperCase() === "WAITING_HITL";
  const noRuns = runs !== null && runs.length === 0 && railSteps.length === 0;

  const who = session?.display_name || "NOC";

  const addNote = async () => {
    if (!id) return;
    if (!note.trim()) {
      setNoteMsg({ tone: "danger", text: "Write the note before posting it." });
      return;
    }
    setNoteBusy(true);
    setNoteMsg(null);
    try {
      const res = await api.addNote(id, {
        author: who,
        author_role: session?.role?.includes("msp") ? "MSP" : "NOC",
        body: note,
        source: "ui",
        mark_restored: markRestored,
        vendor_tt_ref: vendorRef || null,
        msp_root_cause: mspRoot || null,
        msp_action_taken: mspAction || null,
        msp_percent_complete: mspPct ? Number(mspPct) : null,
      });
      setNote("");
      setMspRoot("");
      setMspAction("");
      setMspPct("");
      setMarkRestored(false);
      setNoteMsg({ tone: "ok", text: `Note posted. Status is now ${humanStatus(res.status)}.` });
      load();
    } catch (e: any) {
      setNoteMsg({ tone: "danger", text: `Couldn't post the note: ${detailOf(e)}` });
    } finally {
      setNoteBusy(false);
    }
  };

  const closeTicket = async () => {
    if (!id) return;
    setCloseBusy(true);
    setActionMsg(null);
    try {
      await api.close(id, {
        closed_by: who,
        resolution_code: "CLOSED_NORMAL",
        resolution_summary: "Closed from Mission Control",
      });
      setActionMsg({ tone: "ok", text: "Ticket closed." });
      load();
    } catch (e: any) {
      setActionMsg({ tone: "danger", text: `Couldn't close the ticket: ${detailOf(e)}` });
    } finally {
      setCloseBusy(false);
    }
  };

  const reassign = async () => {
    const msp = reassignMsp.trim();
    if (!id || !msp) return;
    setReassignBusy(true);
    setActionMsg(null);
    try {
      await api.reassign(id, {
        by: who,
        reason: reassignReason.trim() || "Wrong vendor pool",
        assignee_type: "MSP",
        assignee_name: msp,
        msp_name: msp,
      });
      setActionMsg({ tone: "ok", text: `Reassigned to ${msp}.` });
      setReassigning(false);
      setReassignReason("");
      load();
    } catch (e: any) {
      setActionMsg({ tone: "danger", text: `Couldn't reassign: ${detailOf(e)}` });
    } finally {
      setReassignBusy(false);
    }
  };

  if (!inc) {
    return loadFailed ? (
      <div className="empty" role="alert">
        Couldn't load this ticket.{" "}
        <button className="btn sm" onClick={load}>
          Retry
        </button>
      </div>
    ) : (
      <Skeleton rows={8} label="Loading the ticket" />
    );
  }

  const hitlState = String(inc.hitl_state || "").toUpperCase();
  const waiting = Boolean(inc.requires_hitl) && hitlState === "PENDING";
  const decided = Boolean(inc.requires_hitl) && (hitlState === "APPROVED" || hitlState === "REJECTED");
  // The site class word the backend gives, only when it says something: never "standard", and
  // never a priority's own word on a ticket of another priority ("P1 critical" and "P2 major" are
  // what those words mean on this floor, so a P2 never reads "Critical site").
  const siteClass = String(inc.site_class || "").toUpperCase();
  const showClass =
    !!siteClass &&
    siteClass !== "STANDARD" &&
    !(siteClass === "CRITICAL" && inc.priority !== "P1") &&
    !(siteClass === "MAJOR" && inc.priority !== "P2");
  // What restoring or closing does for the customers still waiting to hear (docs/CLOSE_THE_LOOP.md
  // §1): nothing to say once a notice is sent, waits for approval or was rejected.
  const cust = customers.state === "ok" ? customers.data : null;
  const toTell = cust ? Math.max(0, Number(cust.waiting) || 0) : 0;
  const noticeOpen = !cust?.notice || cust.notice.state === "none" || cust.notice.state === "waiting_for_restore";
  // §7.1: an update held back at restore is raised again when the ticket closes.
  const closeOpen = noticeOpen || cust?.notice?.state === "held_back";
  const isRestored = !!inc.restored_at || ["RESTORED", "CLOSED"].includes(String(inc.status || "").toUpperCase());
  const tellWaits = noticeWaitsForPerson(inc.priority, profile?.autonomy_level, toTell);
  const tellTail = `${toTell} ${toTell === 1 ? "customer" : "customers"} service is back${tellWaits ? ", after a supervisor approves" : ""}.`;
  const restoreTells = toTell > 0 && noticeOpen ? `Restoring tells ${tellTail}` : "";
  const closeTells = toTell > 0 && closeOpen ? `Closing tells ${tellTail}` : "";
  const dash = "—";
  const restored = inc.restored_at ? `restored ${fmtDateTime(inc.restored_at)}` : "";
  const resolution = [inc.resolution_code ? capFirst(humanEnum(inc.resolution_code)) : "", restored]
    .filter(Boolean)
    .join(", ");

  // The agents' fields, in pairs that read across (`.two-up` sets two pairs per line on a wide panel).
  const fields: [string, ReactNode][] = [
    [
      "TT category",
      inc.tt_category_label ? (
        <>
          {inc.tt_category_label} <span className="mono muted">{inc.tt_category}</span>
        </>
      ) : (
        capFirst(humanEnum(inc.tt_category)) || dash
      ),
    ],
    ["Symptom", inc.symptom_code ? <span className="mono">{inc.symptom_code}</span> : dash],
    ["Technology", techList(inc.technology) || dash],
    ["Network element", inc.network_element || dash],
    ["Site type", humanEnum(inc.site_type) || dash],
    ["Parent HUB", inc.parent_hub_id ? <span className="mono">{inc.parent_hub_id}</span> : dash],
    ["Child sites down", String(inc.child_sites_down ?? 0)],
    ["Battery left", inc.battery_countdown_min != null ? `${inc.battery_countdown_min} min` : dash],
    ["Outage start", fmtDateTime(inc.outage_start_at)],
    ["Failure time", fmtDateTime(inc.failure_time)],
    ["Escalated", fmtDateTime(inc.escalated_at)],
    ["Expected resolution", fmtDateTime(inc.expected_resolution_at)],
    ["Vendor", inc.responsible_msp || inc.msp_name || dash],
    ["Field engineer", inc.fe_name ? <span className="mono wrap">{inc.fe_name}</span> : dash],
    ["Radio OEM", humanEnum(inc.radio_oem) || dash],
    ["Vendor TT ref", inc.vendor_tt_ref ? <span className="mono wrap">{inc.vendor_tt_ref}</span> : dash],
    ["Vendor progress", inc.msp_percent_complete != null ? `${inc.msp_percent_complete}%` : dash],
    ["Resolution", resolution || dash],
    ["Vendor root cause", inc.msp_root_cause || dash],
    ["Vendor action", inc.msp_action_taken || dash],
  ];

  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <h1>
            <span className={`pill ${inc.priority}`} title={priorityTitle(inc.priority)}>
              {inc.priority}
            </span>{" "}
            {inc.incident_number}
          </h1>
          <p className="lead facts">
            <span>{capFirst(humanStatus(inc.status))}</span>
            <span>{inc.site_name}</span>
            <span>
              <span className="mono">{inc.region_code}</span>
              {inc.county ? `,${NB}${inc.county}` : ""}
            </span>
            {showClass && <span>{`${capFirst(humanEnum(siteClass))}${NB}site`}</span>}
            <span>{`${inc.users_affected?.toLocaleString() ?? dash}${NB}subscribers${NB}(est.)`}</span>
            <span>
              {`owner${NB}`}
              {inc.assignee_name}
            </span>
            {inc.rnio_name && (
              <span>
                {`RNIO${NB}`}
                <span className="mono">{inc.rnio_name}</span>
              </span>
            )}
            {decided && <span>{`decision${NB}${humanStatus(hitlState)}`}</span>}
          </p>
        </div>
        <div className="page-actions">
          {inc.mpesa_risk && (
            <span className="attn danger" title={MPESA_TITLE}>
              <IconDot /> M‑PESA at risk
            </span>
          )}
          {waiting && <span className="chip hitl">{WAITING_WORD}</span>}
        </div>
      </div>
      {/* Regulatory countdown (§5.3.20, §7.10) — at the top, because a SEND_FAILED regulator
          notice must be impossible to miss. Self-contained; renders nothing while
          REGULATORY_ENABLED is off or on any failure, so it cannot affect the page. */}
      <RegulatoryCountdown incidentId={inc.id} />

      <div className="panel stack">
        <div className="panel-head">
          <h2 className="panel-title">NOC ticket fields</h2>
          <span className="muted">Filled by the agents. Times in EAT.</span>
        </div>
        <dl className="rail-dl two-up">
          {fields.map(([label, value]) => (
            <Fragment key={label}>
              <dt>{label}</dt>
              <dd>{value}</dd>
            </Fragment>
          ))}
        </dl>
        <div className="detail-grid">
          <Why title="Why this priority" text={inc.severity_rationale} />
          <Why title="Why this owner" text={inc.assignment_rationale} />
        </div>
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">How the agents handled this alarm</h2>
          {!noRuns && runs !== null && (
            <div className="facts">
              {railSteps.length > 0 && (
                <span>
                  agents took <span className="mono">{fmtMs(worked)}</span>
                </span>
              )}
              {/* Still parked on a person: one state, with how long ("waiting 4 h 31 m for a
                  decision"). Decided: the wait as a plain fact beside the run's outcome. */}
              {approvalHop && stillWaiting ? (
                <span className="state hitl">
                  <IconPause />
                  waiting <span className="mono">{fmtWait(approvalHop.waited_ms)}</span> for a decision
                </span>
              ) : (
                <>
                  {approvalHop && (
                    <span>
                      waited <span className="mono">{fmtWait(approvalHop.waited_ms)}</span> for a decision
                    </span>
                  )}
                  {/* Success is the normal outcome: a plain word. Failed: the state colour and icon. */}
                  {creating?.status && <RunState status={creating.status} />}
                </>
              )}
              {absorbed > 0 && (
                <span title="Later alarms the correlation step folded into this ticket instead of opening a duplicate">
                  {absorbed} later alarm{absorbed === 1 ? "" : "s"} folded in
                </span>
              )}
            </div>
          )}
        </div>
        {runs === null && runsFailed ? (
          <div className="empty" role="alert">
            Couldn't load this ticket's agent run.{" "}
            <button className="btn sm" onClick={load}>
              Retry
            </button>
          </div>
        ) : noRuns ? (
          <p className="muted">
            No agent run is recorded for this ticket, so there are no steps to show. It was opened outside the
            alarm pipeline.
          </p>
        ) : (
          <>
            <p className="muted">Select a step for the agent's reasoning, what it produced and the tools it called.</p>
            <AgentRail steps={railSteps} nodes={wf?.nodes} caption="This ticket's run" loading={runs === null} runStatus={creating?.status} />
          </>
        )}
      </div>

      <div className="detail-grid">
        <div className="panel stack">
          <h2 className="panel-title">Ticket narrative</h2>
          <p>
            <strong>{inc.title}</strong>
          </p>
          <div className="pre">{inc.narrative}</div>
          {/* A wrapper, because `.rail-dl { margin: 0 }` would cancel the stack's spacing. */}
          <div>
            <dl className="rail-dl">
              <dt>Hypothesis</dt>
              <dd>{inc.root_cause_hypothesis || dash}</dd>
              <dt>Impact</dt>
              <dd>{inc.impact_summary || dash}</dd>
              {inc.access_notes && (
                <>
                  <dt>Access</dt>
                  <dd>{inc.access_notes}</dd>
                </>
              )}
            </dl>
          </div>
          <div className="note-form-actions">
            <button
              className="btn danger"
              disabled={closeBusy}
              aria-busy={closeBusy || undefined}
              onClick={closeTicket}
            >
              {closeBusy ? "Closing…" : "Close ticket"}
            </button>
            {!reassigning && (
              <button
                className="btn"
                onClick={() => {
                  setReassignMsp(inc.msp_name || "Camusat");
                  setReassignReason("");
                  setActionMsg(null);
                  setReassigning(true);
                }}
              >
                Reassign vendor
              </button>
            )}
          </div>
          {closeTells && !reassigning && <p className={"ic-consequence" + (tellWaits ? " hitl" : "")}>{closeTells}</p>}
          {reassigning && (
            <div className="note-form" role="group" aria-label="Reassign to another vendor">
              <div className="note-form-row">
                <label>
                  Vendor to reassign to
                  <input
                    autoFocus
                    aria-label="Vendor to reassign to"
                    placeholder="e.g. Camusat"
                    value={reassignMsp}
                    disabled={reassignBusy}
                    onChange={(e) => setReassignMsp(e.target.value)}
                  />
                </label>
                <label>
                  Reason
                  <input
                    aria-label="Reason for reassigning"
                    placeholder="Default: wrong vendor pool"
                    value={reassignReason}
                    disabled={reassignBusy}
                    onChange={(e) => setReassignReason(e.target.value)}
                    onKeyDown={(e) => e.key === "Enter" && reassign()}
                  />
                </label>
              </div>
              <div className="note-form-actions">
                <button className="btn" disabled={reassignBusy || !reassignMsp.trim()} onClick={reassign}>
                  {reassignBusy ? "Reassigning…" : "Reassign"}
                </button>
                <button className="btn" disabled={reassignBusy} onClick={() => setReassigning(false)}>
                  Cancel
                </button>
              </div>
            </div>
          )}
          {actionMsg && (
            <div>
              <Outcome msg={actionMsg} />
            </div>
          )}
        </div>
        <div className="panel stack">
          <h2 className="panel-title">Exec brief</h2>
          <div className="pre">{brief || "No brief yet."}</div>
          <div>
            <h2 className="panel-title">Work note</h2>
            <div className="note-form">
              <div className="note-form-row">
                <label>
                  Vendor TT ref
                  <input
                    aria-label="Vendor TT ref"
                    value={vendorRef}
                    onChange={(e) => setVendorRef(e.target.value)}
                  />
                </label>
                <label>
                  Percent complete
                  <input
                    aria-label="Percent complete, 0 to 100"
                    inputMode="numeric"
                    placeholder="0 to 100"
                    value={mspPct}
                    onChange={(e) => setMspPct(e.target.value)}
                  />
                </label>
              </div>
              <div className="note-form-row">
                <label>
                  Vendor root cause
                  <input aria-label="Vendor root cause" value={mspRoot} onChange={(e) => setMspRoot(e.target.value)} />
                </label>
                <label>
                  Vendor action taken
                  <input
                    aria-label="Vendor action taken"
                    value={mspAction}
                    onChange={(e) => setMspAction(e.target.value)}
                  />
                </label>
              </div>
              <label>
                Work note
                <textarea
                  aria-label="Work note"
                  value={note}
                  onChange={(e) => setNote(e.target.value)}
                  placeholder="Work note until closure…"
                />
              </label>
              <label className="check">
                <input
                  type="checkbox"
                  checked={markRestored}
                  onChange={(e) => setMarkRestored(e.target.checked)}
                  aria-describedby={markRestored && restoreTells ? "restore-tells" : undefined}
                />
                Mark service restored
              </label>
              {/* Said while the box is ticked: that is when posting the note restores the ticket. */}
              {markRestored && restoreTells && (
                <p id="restore-tells" className={"ic-consequence" + (tellWaits ? " hitl" : "")}>
                  {restoreTells}
                </p>
              )}
              <div className="note-form-actions">
                <button className="btn primary" disabled={noteBusy} aria-busy={noteBusy || undefined} onClick={addNote}>
                  {noteBusy ? "Posting…" : "Post note"}
                </button>
                <Outcome msg={noteMsg} />
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Customers on this outage: absent when nothing is linked or the Support desk is off. */}
      {id && <IncidentCustomersPanel incidentId={id} s={customers} restored={isRestored} />}

      {/* Agent memory M0 (spec §7.11): what has happened at this mast before, read from the
          incidents already in the database. Self-contained — it owns its fetch and swallows its
          own failures — so it cannot affect anything above it, and removing it is deleting this
          line and the import. Advisory only (MEM1): it sets no field on this ticket. */}
      <EarlierAtThisSite siteId={inc.site_id} />
      {/* Contract clause lookup (spec 7.8), scoped to THIS incident: the drawer passes
          incidentId so the allow-set is derived from the incident's vendor and the
          asker's role. It cannot widen that scope -- the route ignores any allow-set in
          the request body. Self-contained and advisory, like the panel above it. */}
      <ContractsDrawer incidentId={inc.id} />
      {/* Stop clock / SCC (§7.6.3, §7.10): reason mandatory before open. Renders nothing while
          SCORECARDS_ENABLED is off; owns its fetch and swallows its own failures. */}
      <StopClockPanel incidentId={inc.id} />

      <div className="panel">
        <div className="panel-head">
          <h2 className="panel-title">Timeline</h2>
          <span className="muted">Times in EAT</span>
        </div>
        <div className="list" aria-busy={timeline === null || undefined}>
          {timeline === null && timelineFailed && (
            <div className="empty" role="alert">
              Couldn't load the timeline.{" "}
              <button className="btn sm" onClick={load}>
                Retry
              </button>
            </div>
          )}
          {timeline === null && !timelineFailed && <Skeleton rows={6} label="Loading the timeline" />}
          {timeline !== null && timeline.length === 0 && (
            <div className="empty">Nothing on this ticket's timeline yet.</div>
          )}
          {(timeline || []).map((t, i) => {
            const head = timelineHead(t);
            return (
              // The clock is "02 Oct, 05:16" in mono on every row, so the titles start at one x.
              <div key={i} className="row static">
                <span className="muted dim mono">{shortTime(t.ts)}</span>
                <div className="row-main">
                  <div className="head-row">
                    <strong>{head.title}</strong>
                    {head.sub && <span>{head.sub}</span>}
                    {head.state}
                  </div>
                  <TimelineDetail text={t.detail} />
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
