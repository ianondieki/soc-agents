import { useEffect, useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import { api } from "../api";
import AgentRail from "../components/AgentRail";
import { countAbsorbed, fmtMs, humanStatus, pickCreatingRun, runChipClass, runStatusWord, sumDurations } from "../lib/agents";
import EarlierAtThisSite from "../components/EarlierAtThisSite";
import ContractsDrawer from "../components/ContractsDrawer";
import RegulatoryCountdown from "../components/RegulatoryCountdown";
import StopClockPanel from "../components/StopClockPanel";
import { fmtDateTime } from "../lib/time";
import { useIncidentRevision } from "../realtime/RealtimeContext";

export default function IncidentWorkspace({ session }: { session: any }) {
  const { id } = useParams();
  // Defect #26: this page reloads when an event names *this* incident (or when
  // an unrecognised event forces a full resync), not on every WS frame.
  const rev = useIncidentRevision(id);
  const [inc, setInc] = useState<any>(null);
  const [wf, setWf] = useState<any>(null);
  const [timeline, setTimeline] = useState<any[]>([]);
  const [runs, setRuns] = useState<any[]>([]);
  const [brief, setBrief] = useState<string>("");
  const [note, setNote] = useState("");
  const [vendorRef, setVendorRef] = useState("");
  const [mspRoot, setMspRoot] = useState("");
  const [mspAction, setMspAction] = useState("");
  const [mspPct, setMspPct] = useState("");
  const [markRestored, setMarkRestored] = useState(false);
  const [msg, setMsg] = useState("");

  const load = () => {
    if (!id) return;
    api.incident(id).then(setInc).catch(console.error);
    api.workflow(id).then(setWf).catch(console.error);
    api.timeline(id).then(setTimeline).catch(console.error);
    // The run that opened the ticket, not the newest (usually a two-step merge): lib/agents.
    api.runsFor(id).then((r) => setRuns(Array.isArray(r) ? r : [])).catch(() => setRuns([]));
    api
      .brief(id)
      .then((b) => setBrief(b.body))
      .catch(() => setBrief(""));
  };

  useEffect(load, [id, rev]);

  const creating = useMemo(() => pickCreatingRun(runs), [runs]);
  const absorbed = useMemo(() => countAbsorbed(runs), [runs]);
  const railSteps = creating?.steps || wf?.steps || [];
  const elapsed = creating?.finished_at && creating?.started_at ? Math.max(0, +new Date(creating.finished_at) - +new Date(creating.started_at)) : sumDurations(railSteps);

  const who = session?.display_name || "NOC";

  const addNote = async () => {
    if (!id || !note.trim()) return;
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
    setMsg(`Note posted · status ${res.status}`);
    load();
  };

  if (!inc) return <div className="empty">Loading incident…</div>;

  return (
    <div>
      <div style={{ display: "flex", gap: "0.6rem", alignItems: "center", flexWrap: "wrap" }}>
        <span className={`pill ${inc.priority}`}>{inc.priority}</span>
        <h1 className="page-title">{inc.incident_number}</h1>
        <span className="chip accent">{humanStatus(inc.status)}</span>
        <span className="chip">{String(inc.site_class || "standard").toLowerCase()} site</span>
        {inc.mpesa_risk && <span className="chip hitl">M-PESA corridor risk</span>}
        {inc.requires_hitl && <span className="chip hitl">decision {humanStatus(inc.hitl_state)}</span>}
      </div>
      <p className="muted">
        {inc.site_name} ({inc.site_type}) · {inc.region_code}
        {inc.county ? ` / ${inc.county}` : ""} · est. users {inc.users_affected?.toLocaleString()} · owner{" "}
        {inc.assignee_name} · RNIO {inc.rnio_name || "—"}
      </p>
      {/* Regulatory countdown (§5.3.20, §7.10) — at the top, because a SEND_FAILED regulator
          notice must be impossible to miss. Self-contained; renders nothing while
          REGULATORY_ENABLED is off or on any failure, so it cannot affect the page. */}
      <RegulatoryCountdown incidentId={inc.id} />

      <div className="panel" style={{ marginBottom: "1rem" }}>
        <h3>NOC ticket fields (agent-filled · times in EAT)</h3>
        <table>
          <tbody>
            <tr>
              <td className="muted">TT category</td>
              <td>
                {inc.tt_category} — {inc.tt_category_label}
              </td>
              <td className="muted">Symptom</td>
              <td>{inc.symptom_code}</td>
            </tr>
            <tr>
              <td className="muted">Technology</td>
              <td>{inc.technology}</td>
              <td className="muted">Network element</td>
              <td>{inc.network_element}</td>
            </tr>
            <tr>
              <td className="muted">Outage start</td>
              <td>{fmtDateTime(inc.outage_start_at)}</td>
              <td className="muted">Battery countdown</td>
              <td>{inc.battery_countdown_min != null ? `${inc.battery_countdown_min} min` : "—"}</td>
            </tr>
            <tr>
              <td className="muted">Parent HUB</td>
              <td>{inc.parent_hub_id || "—"}</td>
              <td className="muted">Children down</td>
              <td>{inc.child_sites_down ?? 0}</td>
            </tr>
            <tr>
              <td className="muted">Responsible MSP</td>
              <td>{inc.responsible_msp || inc.msp_name || "—"}</td>
              <td className="muted">Radio OEM</td>
              <td>{inc.radio_oem || "—"}</td>
            </tr>
            <tr>
              <td className="muted">Failure time</td>
              <td>{fmtDateTime(inc.failure_time)}</td>
              <td className="muted">Expected resolution</td>
              <td>{fmtDateTime(inc.expected_resolution_at)}</td>
            </tr>
            <tr>
              <td className="muted">Escalated to MSP/FE</td>
              <td>{fmtDateTime(inc.escalated_at)}</td>
              <td className="muted">Field engineer</td>
              <td>{inc.fe_name || "—"}</td>
            </tr>
            <tr>
              <td className="muted">Vendor TT ref</td>
              <td>{inc.vendor_tt_ref || "—"}</td>
              <td className="muted">MSP % complete</td>
              <td>{inc.msp_percent_complete != null ? `${inc.msp_percent_complete}%` : "—"}</td>
            </tr>
            <tr>
              <td className="muted">MSP root cause</td>
              <td colSpan={3}>{inc.msp_root_cause || "—"}</td>
            </tr>
            <tr>
              <td className="muted">MSP action taken</td>
              <td colSpan={3}>{inc.msp_action_taken || "—"}</td>
            </tr>
            <tr>
              <td className="muted">Resolution</td>
              <td colSpan={3}>
                {inc.resolution_code || "—"} {inc.restored_at ? `(restored ${fmtDateTime(inc.restored_at)})` : ""}
              </td>
            </tr>
            <tr>
              <td className="muted">Severity why</td>
              <td colSpan={3} className="muted">
                {inc.severity_rationale}
              </td>
            </tr>
            <tr>
              <td className="muted">Assignment why</td>
              <td colSpan={3} className="muted">
                {inc.assignment_rationale}
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div className="panel" style={{ marginBottom: "1rem" }}>
        <div className="panel-head">
          <h3>How the agents handled this alarm</h3>
          <div className="chips">
            {railSteps.length > 0 && <span className="chip">{railSteps.length} hops · {fmtMs(elapsed)}</span>}
            {creating?.status && <span className={runChipClass(creating.status)}>{runStatusWord(creating.status)}</span>}
            {absorbed > 0 && (
              <span className="chip accent" title="Later alarms the correlation step folded into this ticket instead of opening a duplicate">
                {absorbed} later alarm{absorbed === 1 ? "" : "s"} folded in
              </span>
            )}
          </div>
        </div>
        <p className="muted">Click a hop for the agent's reasoning, what it produced and the tools it called. The whole shift can audit the agents.</p>
        <AgentRail steps={railSteps} nodes={wf?.nodes} caption="This ticket's run" />
      </div>

      <div className="detail-grid">
        <div className="panel">
          <h3>Ticket / narrative</h3>
          <p>
            <strong>{inc.title}</strong>
          </p>
          <div className="pre">{inc.narrative}</div>
          <p className="muted" style={{ marginTop: "0.75rem" }}>
            Hypothesis: {inc.root_cause_hypothesis}
          </p>
          <p className="muted">Impact: {inc.impact_summary}</p>
          {inc.access_notes && <p className="muted">Access: {inc.access_notes}</p>}
          <div className="form-row" style={{ marginTop: "0.75rem" }}>
            <button
              className="btn good"
              onClick={async () => {
                await api.close(id!, {
                  closed_by: who,
                  resolution_code: "CLOSED_NORMAL",
                  resolution_summary: "Closed from Mission Control",
                });
                setMsg("Ticket closed");
                load();
              }}
            >
              Close ticket
            </button>
            <button
              className="btn"
              onClick={async () => {
                const msp = window.prompt("Reassign to MSP name (e.g. Camusat)", inc.msp_name || "Camusat");
                if (!msp) return;
                const reason = window.prompt("Reason") || "Wrong vendor pool";
                await api.reassign(id!, {
                  by: who,
                  reason,
                  assignee_type: "MSP",
                  assignee_name: msp,
                  msp_name: msp,
                });
                setMsg(`Reassigned to ${msp}`);
                load();
              }}
            >
              Reassign MSP
            </button>
          </div>
        </div>
        <div className="panel">
          <h3>Exec brief</h3>
          <div className="pre">{brief || "No brief yet."}</div>
          <h3 style={{ marginTop: "1rem" }}>MSP / FE / NOC work note</h3>
          {msg && <p className="chip ok">{msg}</p>}
          <input
            placeholder="Vendor TT ref (MSP)"
            value={vendorRef}
            onChange={(e) => setVendorRef(e.target.value)}
            style={{ width: "100%", marginBottom: "0.4rem" }}
          />
          <input
            placeholder="MSP root cause"
            value={mspRoot}
            onChange={(e) => setMspRoot(e.target.value)}
            style={{ width: "100%", marginBottom: "0.4rem" }}
          />
          <input
            placeholder="MSP action taken"
            value={mspAction}
            onChange={(e) => setMspAction(e.target.value)}
            style={{ width: "100%", marginBottom: "0.4rem" }}
          />
          <input
            placeholder="% complete (0-100)"
            value={mspPct}
            onChange={(e) => setMspPct(e.target.value)}
            style={{ width: "100%", marginBottom: "0.4rem" }}
          />
          <textarea value={note} onChange={(e) => setNote(e.target.value)} placeholder="Work note until closure…" />
          <label className="muted" style={{ display: "flex", gap: "0.4rem", alignItems: "center", marginTop: "0.4rem" }}>
            <input type="checkbox" checked={markRestored} onChange={(e) => setMarkRestored(e.target.checked)} />
            Mark service restored
          </label>
          <div style={{ marginTop: "0.5rem" }}>
            <button className="btn primary" onClick={addNote}>
              Post note
            </button>
          </div>
        </div>
      </div>

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

      <div className="panel" style={{ marginTop: "1rem" }}>
        <h3>Unified timeline (EAT)</h3>
        <div className="list">
          {timeline.map((t, i) => (
            <div key={i} className="row" style={{ cursor: "default" }}>
              <span className="chip">{t.kind}</span>
              <div>
                <div>
                  <strong>{t.title}</strong> · {t.status}
                </div>
                <div className="muted">{t.detail}</div>
              </div>
              <span className="muted">{fmtDateTime(t.ts, "")}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
