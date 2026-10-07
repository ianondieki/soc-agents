import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft } from "lucide-react";
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
  ownerName,
  pickCreatingRun,
  priorityTitle,
  regionName,
  sumDurations,
} from "../lib/agents";
import { detailOf, isStatus } from "../lib/apiError";
import { parseRationale } from "../lib/audit";
import { IconAlert, IconCheck, IconDot, IconPause } from "../lib/icons";
import EarlierAtThisSite from "../components/EarlierAtThisSite";
import ContractsDrawer from "../components/ContractsDrawer";
import RegulatoryCountdown from "../components/RegulatoryCountdown";
import StopClockPanel from "../components/StopClockPanel";
import { fmtDateTime, fmtHM, parseInstant } from "../lib/time";
import { confirmCue } from "../lib/feedback";
import { useMinute } from "../lib/useMinute";
import "./IncidentWorkspace.css";
import { useIncidentRevision } from "../realtime/RealtimeContext";
import IncidentCustomersPanel, { useIncidentCustomers } from "../components/support/IncidentCustomers";
import { noticeWaitsForPerson } from "../lib/support";

/** "45 min", "2 h 10 min", "3 d 4 h": a stretch of time at a glance. */
function spanWords(ms: number): string {
  const m = Math.max(0, Math.round(ms / 60_000));
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  if (h < 24) return m % 60 ? `${h} h ${m % 60} min` : `${h} h`;
  const d = Math.floor(h / 24);
  return h % 24 ? `${d} d ${h % 24} h` : `${d} d`;
}

/**
 * The stored impact line as a sentence: "Est. 180,000 users; region Rift Valley; class CRITICAL;
 * children_down=0" reads "Est. 180,000 users, region Rift Valley, critical site". A count of child
 * sites down is said only when it is not zero. The stored text is untouched.
 */
function impactWords(text: string): string {
  const parts = String(text)
    .split(";")
    .map((p) => p.trim())
    .filter(Boolean)
    .map((p) => {
      const cls = /^class\s+([A-Z_]+)$/i.exec(p);
      if (cls) return `${humanEnum(cls[1].toUpperCase())} site`;
      const kids = /^children_down\s*=\s*(\d+)$/i.exec(p);
      if (kids) return Number(kids[1]) ? `${kids[1]} child ${kids[1] === "1" ? "site" : "sites"} down` : "";
      return arrowsToWords(p);
    })
    .filter(Boolean);
  return capFirst(parts.join(", "));
}

/** A status's tone on this page: open work, restored, closed. */
function statusTone(status: unknown): "open" | "vendor" | "restored" | "closed" {
  const s = String(status ?? "").toUpperCase();
  if (s === "CLOSED") return "closed";
  if (s === "RESTORED") return "restored";
  if (s === "AWAITING_VENDOR") return "vendor";
  return "open";
}

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
  // A count of five figures or more reads with separators, alone or in a phrase ("450000 to P2").
  s = s.replace(/(^|[\s(])([1-9]\d{4,})(?=$|[\s,)])/g, (_m, pre: string, n: string) => pre + Number(n).toLocaleString("en-KE"));
  s = s.replace(/^true\b/, "yes").replace(/^false\b/, "no");
  if (/^[a-z]+(?:_[a-z]+)+$/.test(s)) s = humanEnum(s.toUpperCase());
  return s;
}

/** The rationale keys whose values are vendor codes ("primary=EGYPRO", "pool=['EGYPRO', ...]"). */
const VENDOR_FACTS: ReadonlySet<string> = new Set(["primary", "pool", "owner", "msp", "vendor"]);

/** "egypro, egypro_remote" or "['EGYPRO', 'EGYPRO_REMOTE']" -> "Egypro, Egypro Remote": vendor codes as
 *  names, the way the owner reads everywhere else. */
function vendorList(value: string): string {
  return String(value ?? "")
    .replace(/[[\]'"]/g, "")
    .split(",")
    .map((v) => v.trim())
    .filter(Boolean)
    .map((v) => ownerName(v.toUpperCase().replace(/\s+/g, "_")).replace(/\b([a-z])/g, (c) => c.toUpperCase()))
    .join(", ");
}

/** "Power grid fail at Test Kayole HUB 4922": the alarm in words and the site, as the story's
 *  headline. Falls back to the stored summary when the alarm code is missing. */
function storyTitle(inc: any): string {
  // The alarm code: its own field, else the one the stored description opens with
  // ("POWER_GRID_FAIL at Test Kayole HUB 4922").
  const alarm = String(inc?.alarm_code || "").trim() || (/^([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\b/.exec(String(inc?.description || ""))?.[1] ?? "");
  const site = String(inc?.site_name || inc?.site_id || "").trim();
  if (!alarm) return String(inc?.title || "");
  const words = humanEnum(alarm);
  return `${words.charAt(0).toUpperCase()}${words.slice(1)}${site ? ` at ${site}` : ""}`;
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
              <dd>{VENDOR_FACTS.has(f.key) ? vendorList(f.value) : tidyValue(f.value)}</dd>
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
function timelineHead(t: any): { title: string; sub: string; state: ReactNode; human?: boolean } {
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
      human: !isAgent,
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
function TimelineDetail({ text, human = false }: { text: unknown; human?: boolean }) {
  const s = typeof text === "string" ? eatTimes(text.trim()) : "";
  if (!s) return null;
  // A person's work note is their words as written: set in the serif that marks people, never
  // parsed into facts or rewritten.
  if (human) return <p className="iw-tl-words">{s}</p>;
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
  // The age and the time to the restore deadline move with the clock.
  const nowMs = useMinute().getTime();
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
  // Why the ticket did not load: a 404 is an address with no ticket behind it, which Retry cannot fix.
  const [loadFailed, setLoadFailed] = useState<false | "error" | "missing">(false);
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
      .catch(mine((e: unknown) => setLoadFailed(isStatus(e, 404) ? "missing" : "error")));
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
      confirmCue();
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

  if (!inc && loadFailed === "missing") {
    return (
      <div className="stack">
        <div className="page-head">
          <div>
            <h1>No ticket at this address</h1>
            <p className="lead">
              This operator has no ticket with the id <span className="mono iw-missing-id">{id}</span>. The link may be wrong or out of date.
            </p>
          </div>
        </div>
        <div className="nf-actions">
          <Link className="btn primary" to="/incidents">
            Open the Incident board
          </Link>
        </div>
      </div>
    );
  }

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

  // The five figures under the head.
  const opened = parseInstant(inc.outage_start_at) || parseInstant(inc.created_at);
  const ended = parseInstant(inc.restored_at) || parseInstant(inc.closed_at);
  const due = parseInstant(inc.sla_restore_due);
  const late = !isRestored && !!due && due.getTime() < nowMs;
  const ownerWord = ownerName(inc.responsible_msp || inc.assignee_name || inc.msp_name) || dash;
  const tone = statusTone(inc.status);
  const place = [regionName(inc.region_code, profile), inc.county].filter(Boolean).join(", ");
  const decisionWord = waiting ? "Waiting" : decided ? capFirst(humanStatus(hitlState)) : inc.requires_hitl ? capFirst(humanStatus(hitlState)) || "Needed" : "Not needed";

  return (
    <div className="iw">
      <div className="iw-back">
        <Link to="/incidents">
          <ArrowLeft size={15} strokeWidth={1.75} aria-hidden="true" />
          Incident board
        </Link>
      </div>

      <header className="iw-head">
        <div className="iw-head-main">
          <h1 className="iw-title">
            <span className={`pill ${inc.priority}`} title={priorityTitle(inc.priority)}>
              {inc.priority}
            </span>
            <span>{inc.site_name || inc.site_id}</span>
          </h1>
          <p className="iw-sub">
            <span className="iw-num">{inc.incident_number}</span>
            <span className={`iw-status ${tone}`}>
              <span className="iw-status-dot" aria-hidden="true" />
              {capFirst(humanStatus(inc.status))}
            </span>
            {place && <span>{place}</span>}
            {showClass && <span>{`${capFirst(humanEnum(siteClass))}${NB}site`}</span>}
            <span className="mono iw-code">{inc.site_id}</span>
            {inc.rnio_name && <span className="mono iw-code">{inc.rnio_name}</span>}
          </p>
        </div>
        <div className="iw-flags">
          {inc.mpesa_risk && (
            <span className="iw-flag danger" title={MPESA_TITLE}>
              <IconDot /> M‑PESA at risk
            </span>
          )}
          {waiting && <span className="chip hitl">{WAITING_WORD}</span>}
          {decided && <span className="chip">{`Decision${NB}${humanStatus(hitlState)}`}</span>}
        </div>
      </header>

      <dl className="iw-figures">
        <div className={late ? "bad" : undefined}>
          <dt>{ended ? "Took" : "Open for"}</dt>
          <dd>{opened ? spanWords((ended ? ended.getTime() : nowMs) - opened.getTime()) : dash}</dd>
          <dd className="iw-fig-note">{opened ? `since ${shortTime(opened.toISOString())}` : " "}</dd>
        </div>
        <div className={late ? "bad" : undefined}>
          <dt>{isRestored ? "Restored" : "Restore due"}</dt>
          <dd>{isRestored ? (inc.restored_at ? fmtHM(inc.restored_at) : "Yes") : due ? fmtHM(due) : dash}</dd>
          <dd className="iw-fig-note">
            {isRestored
              ? inc.restored_at
                ? shortTime(inc.restored_at)
                : " "
              : due
                ? late
                  ? `late by ${spanWords(nowMs - due.getTime())}`
                  : `in ${spanWords(due.getTime() - nowMs)}`
                : "no restore SLA recorded"}
          </dd>
        </div>
        <div>
          <dt>Subscribers</dt>
          <dd>{inc.users_affected != null ? Number(inc.users_affected).toLocaleString("en-KE") : dash}</dd>
          <dd className="iw-fig-note">estimated</dd>
        </div>
        <div>
          <dt>Owner</dt>
          <dd className="iw-fig-word">{ownerWord}</dd>
          <dd className="iw-fig-note">{inc.fe_name ? `Field engineer ${inc.fe_name}` : " "}</dd>
        </div>
        <div className={waiting ? "hitl" : undefined}>
          <dt>Decision</dt>
          <dd className="iw-fig-word">{decisionWord}</dd>
          <dd className="iw-fig-note">
            {approvalHop && typeof approvalHop.waited_ms === "number"
              ? `${stillWaiting ? "waiting" : "waited"} ${fmtWait(approvalHop.waited_ms)}`
              : waiting
                ? "on Approvals"
                : " "}
          </dd>
        </div>
      </dl>

      {/* Regulatory countdown (§5.3.20, §7.10) — at the top, because a SEND_FAILED regulator
          notice must be impossible to miss. Self-contained; renders nothing while
          REGULATORY_ENABLED is off or on any failure, so it cannot affect the page. */}
      <RegulatoryCountdown incidentId={inc.id} />

      <div className="iw-grid">
        <div className="iw-main">
          <section className="panel iw-story" aria-labelledby="iw-story-title">
            <h2 id="iw-story-title" className="panel-title">
              What happened
            </h2>
            {/* The headline in words (the alarm and the site); the stored summary, with its
                category tag, stays under it in the mono face, as written. */}
            <p className="iw-story-title">{storyTitle(inc)}</p>
            {inc.title && <p className="iw-story-raw">{inc.title}</p>}
            <dl className="iw-story-facts">
              <div>
                <dt>Likely cause</dt>
                <dd>{inc.root_cause_hypothesis || dash}</dd>
              </div>
              <div>
                <dt>Impact</dt>
                <dd>{inc.impact_summary ? impactWords(inc.impact_summary) : dash}</dd>
              </div>
              {inc.access_notes && (
                <div>
                  <dt>Access</dt>
                  <dd>{inc.access_notes}</dd>
                </div>
              )}
            </dl>
            <details className="iw-narrative">
              <summary>The ticket narrative, as the agents wrote it</summary>
              <div className="pre">{inc.narrative}</div>
            </details>
            <div className="iw-whys">
              <Why title="Why this priority" text={inc.severity_rationale} />
              <Why title="Why this owner" text={inc.assignment_rationale} />
            </div>
          </section>

          <section className="panel" aria-labelledby="iw-run-title">
            <div className="panel-head">
              <h2 id="iw-run-title" className="panel-title">
                How the agents handled this alarm
              </h2>
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
                No agent run is recorded for this ticket, so there are no steps to show. It was opened outside the alarm
                pipeline.
              </p>
            ) : (
              <>
                <p className="muted">Select a step for the agent's reasoning, what it produced and the tools it called.</p>
                <AgentRail steps={railSteps} nodes={wf?.nodes} caption="This ticket's run" loading={runs === null} runStatus={creating?.status} />
              </>
            )}
          </section>

          {/* Customers on this outage: absent when nothing is linked or the Support desk is off. */}
          {id && <IncidentCustomersPanel incidentId={id} s={customers} restored={isRestored} />}

          <section className="panel" aria-labelledby="iw-fields-title">
            <div className="panel-head">
              <h2 id="iw-fields-title" className="panel-title">
                Ticket fields
              </h2>
              <span className="muted">Filled by the agents. Times in EAT.</span>
            </div>
            <dl className="iw-fields">
              {fields.map(([label, value]) => (
                <div key={label}>
                  <dt>{label}</dt>
                  <dd>{value}</dd>
                </div>
              ))}
            </dl>
          </section>

          <section className="panel" aria-labelledby="iw-timeline-title">
            <div className="panel-head">
              <h2 id="iw-timeline-title" className="panel-title">
                Timeline
              </h2>
              <span className="muted">Times in EAT</span>
            </div>
            <ol className="iw-timeline" aria-busy={timeline === null || undefined}>
              {timeline === null && timelineFailed && (
                <li className="empty" role="alert">
                  Couldn't load the timeline.{" "}
                  <button className="btn sm" onClick={load}>
                    Retry
                  </button>
                </li>
              )}
              {timeline === null && !timelineFailed && (
                <li>
                  <Skeleton rows={6} label="Loading the timeline" />
                </li>
              )}
              {timeline !== null && timeline.length === 0 && <li className="empty">Nothing on this ticket's timeline yet.</li>}
              {(timeline || []).map((t, i) => {
                const head = timelineHead(t);
                const kind = String(t.kind || "");
                return (
                  <li
                    key={i}
                    className={`iw-tl ${kind === "note" ? "note" : kind === "broadcast" ? "broadcast" : kind === "agent_step" ? "step" : "other"}${head.human ? " human" : ""}`}
                  >
                    <span className="iw-tl-time">{shortTime(t.ts)}</span>
                    <span className="iw-tl-dot" aria-hidden="true" />
                    <div className="iw-tl-main">
                      <div className="head-row">
                        <strong>{head.title}</strong>
                        {head.sub && <span>{head.sub}</span>}
                        {head.state}
                      </div>
                      <TimelineDetail text={t.detail} human={head.human} />
                    </div>
                  </li>
                );
              })}
            </ol>
          </section>
        </div>

        <aside className="iw-side" aria-label="Act on this ticket">
          <section className="panel iw-act" aria-labelledby="iw-note-title">
            <h2 id="iw-note-title" className="panel-title">
              Work note
            </h2>
            <div className="note-form">
              <label>
                What happened, what's next
                <textarea
                  aria-label="Work note"
                  value={note}
                  onChange={(e) => setNote(e.target.value)}
                  placeholder="Work note until closure…"
                />
              </label>
              <details className="iw-vendor-fields">
                <summary>Vendor update (optional)</summary>
                <div className="iw-vendor-grid">
                  <label>
                    Vendor TT ref
                    <input aria-label="Vendor TT ref" value={vendorRef} onChange={(e) => setVendorRef(e.target.value)} />
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
                  <label>
                    Vendor root cause
                    <input aria-label="Vendor root cause" value={mspRoot} onChange={(e) => setMspRoot(e.target.value)} />
                  </label>
                  <label>
                    Vendor action taken
                    <input aria-label="Vendor action taken" value={mspAction} onChange={(e) => setMspAction(e.target.value)} />
                  </label>
                </div>
              </details>
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

            <div className="iw-act-more">
              <div className="note-form-actions">
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
                <button className="btn danger" disabled={closeBusy} aria-busy={closeBusy || undefined} onClick={closeTicket}>
                  {closeBusy ? "Closing…" : "Close ticket"}
                </button>
              </div>
              {closeTells && !reassigning && <p className={"ic-consequence" + (tellWaits ? " hitl" : "")}>{closeTells}</p>}
              {reassigning && (
                <div className="note-form" role="group" aria-label="Reassign to another vendor">
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
          </section>

          <section className="panel" aria-labelledby="iw-brief-title">
            <h2 id="iw-brief-title" className="panel-title">
              Exec brief
            </h2>
            {brief ? <div className="pre iw-brief">{brief}</div> : <p className="muted">No brief yet. The briefing agent writes one for P1 and P2 tickets.</p>}
          </section>

          {/* Stop clock / SCC (§7.6.3, §7.10): reason mandatory before open. Renders nothing while
              SCORECARDS_ENABLED is off; owns its fetch and swallows its own failures. */}
          <StopClockPanel incidentId={inc.id} />
          {/* Agent memory M0 (spec §7.11): what has happened at this mast before, read from the
              incidents already in the database. Self-contained — it owns its fetch and swallows its
              own failures — so it cannot affect anything above it. Advisory only (MEM1). */}
          <EarlierAtThisSite siteId={inc.site_id} />
          {/* Contract clause lookup (spec 7.8), scoped to THIS incident: the drawer passes
              incidentId so the allow-set is derived from the incident's vendor and the
              asker's role. It cannot widen that scope. Self-contained and advisory. */}
          <ContractsDrawer incidentId={inc.id} flush />
        </aside>
      </div>
    </div>
  );
}
