import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { detailOf, isLaneOff } from "../lib/apiError";
import { fmtDateTime } from "../lib/time";
import { useIncidentRevision } from "../realtime/RealtimeContext";

/**
 * Stop-clock control — `StopClockControl` in spec §7.10, the workspace side of §7.6.3.
 *
 * A stop clock (SCC) is time the vendor is *not* charged for inside an outage: the site was
 * inaccessible, the power was the utility's, the customer asked for a delay. It moves money,
 * so this panel is built around three rules the backend already enforces and the screen must
 * not undermine:
 *
 * 1. **A reason is mandatory before open** (§7.10: "reason mandatory before open"), and a
 *    reversal needs its own reason. The buttons stay disabled until the reason field has
 *    words in it, so the 400 is never the first time an analyst hears about the rule.
 * 2. **Late openings are the operator's discipline number, not the vendor's.** An SCC recorded
 *    more than 60 minutes after it started is flagged LATE (§7.6.2) — shown on the row and
 *    counted in the header, because a back-dated stop clock is the first thing a vendor
 *    dispute will pull on.
 * 3. **The deduction is shown as a number**, beside the incident window it was computed over,
 *    so nobody has to reconstruct it from the rows.
 *
 * FLAG: every route 404s while `SCORECARDS_ENABLED` is off (the default). This panel then
 * renders **nothing at all** — not an error box, not an "off" line — so the workspace is
 * byte-for-byte what it was before the lane existed. Self-contained like `EarlierAtThisSite`:
 * it owns its fetch and swallows its own failures, so it cannot take the workspace down.
 *
 * Roles are the server's: `noc_analyst`+ opens, `shift_supervisor`+ closes and reverses, and
 * a vendor role is refused even with auth off (§7.6.6). A 403 is shown in the server's words.
 *
 * 3 a.m. rules (§7.10): every state is a chip with a word in it (OPEN / CLOSED / REVERSED /
 * LATE), never colour alone; every time is absolute EAT via `fmtDateTime`; nothing animates.
 */

type ClockEvent = {
  id: string;
  scc_code: string;
  started_at: string | null;
  ended_at: string | null;
  open: boolean;
  opened_by: string;
  opened_role: string;
  opened_at: string | null;
  opening_delay_min: number;
  late: boolean;
  reason: string;
  evidence_note_id: string | null;
  reversed: boolean;
  reversed_at: string | null;
  reversed_by: string | null;
  reversal_reason: string | null;
};

type ClockView = {
  incident_id: string;
  incident_number: string;
  vendor_id: string | null;
  window_start: string | null;
  window_end: string | null;
  restored: boolean;
  deducted_minutes: number;
  late_openings: number;
  events: ClockEvent[];
};

/**
 * `db/models_vendors.SCC_CODES`, in the server's order. The backend exposes no route that
 * lists them, so they are mirrored here; an unknown code is still refused server-side with
 * the full list in the 400, which this panel shows verbatim.
 */
const SCC_CODES = [
  "END_USER_REQUEST",
  "OBSERVATION",
  "CONTACT_UNAVAILABLE",
  "WIRING_NOT_OURS",
  "UTILITY_POWER",
  "SITE_ACCESS_DENIED",
  "SECURITY_INCIDENT",
  "PLANNED_MAINTENANCE",
  "FORCE_MAJEURE",
  "AWAITING_THIRD_PARTY_PERMIT",
];

function fmtMinutes(m: number | null | undefined): string {
  if (m == null || Number.isNaN(m)) return "—";
  const total = Math.round(m);
  if (total < 60) return total + " min";
  const h = Math.floor(total / 60);
  const r = total % 60;
  return h + " h" + (r ? " " + r + " min" : "");
}

function stateChip(ev: ClockEvent): { cls: string; word: string } {
  if (ev.reversed) return { cls: "chip", word: "REVERSED" };
  if (ev.open) return { cls: "chip warn", word: "OPEN · clock stopped" };
  return { cls: "chip ok", word: "CLOSED" };
}

export default function StopClockPanel({ incidentId }: { incidentId?: string | null }) {
  const [view, setView] = useState<ClockView | null>(null);
  const [hidden, setHidden] = useState(true);
  const [code, setCode] = useState(SCC_CODES[0]);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [reversing, setReversing] = useState<string | null>(null);
  const [reverseReason, setReverseReason] = useState("");

  const load = useCallback(() => {
    if (!incidentId) return;
    api
      .incidentClock(incidentId)
      .then((v: ClockView) => {
        setView(v);
        setHidden(false);
      })
      .catch((e) => {
        // 404 = SCORECARDS_ENABLED is off (or the incident is not ours): render nothing.
        // Any other failure keeps whatever was last on screen (nothing, on a first load) —
        // a stop-clock read failing must never put an error box in an outage workspace.
        if (isLaneOff(e)) {
          setView(null);
          setHidden(true);
        }
      });
  }, [incidentId]);

  useEffect(() => {
    setView(null);
    setHidden(true);
  }, [incidentId]);

  // The deduction is live until restore, so the panel follows this incident's own WS
  // revision (the workspace's own contract, defect #26) rather than a prop from the page.
  const rev = useIncidentRevision(incidentId);
  useEffect(() => {
    load();
  }, [load, rev]);

  const act = async (fn: () => Promise<any>, done: string) => {
    setBusy(true);
    setNote(null);
    setProblem(null);
    try {
      const res = await fn();
      if (res?.clock) setView(res.clock as ClockView);
      setNote(done);
      return true;
    } catch (e) {
      setProblem(detailOf(e, "The stop clock was not changed."));
      return false;
    } finally {
      setBusy(false);
    }
  };

  if (!incidentId || hidden || !view) return null;

  const openClock = async () => {
    const why = reason.trim();
    if (!why) return;
    const ok = await act(() => api.clockOpen(incidentId, { scc_code: code, reason: why }), "Stop clock opened");
    if (ok) setReason("");
  };

  const closeClock = (ev: ClockEvent) =>
    act(() => api.clockClose(incidentId, ev.id, {}), "Stop clock closed");

  const reverseClock = async (ev: ClockEvent) => {
    const why = reverseReason.trim();
    if (!why) return;
    const ok = await act(() => api.clockReverse(incidentId, ev.id, { reason: why }), "Stop clock reversed");
    if (ok) {
      setReversing(null);
      setReverseReason("");
    }
  };

  const openCount = view.events.filter((e) => e.open && !e.reversed).length;

  return (
    <div className="panel" style={{ marginTop: "1rem" }}>
      <div className="panel-head">
        <h3>Stop clock (SCC)</h3>
        {openCount > 0 ? (
          <span className="chip warn">{openCount} OPEN · vendor clock stopped</span>
        ) : (
          <span className="chip">NO OPEN SCC · vendor clock running</span>
        )}
        {view.late_openings > 0 && (
          <span className="chip danger">
            {view.late_openings} LATE OPENING{view.late_openings === 1 ? "" : "S"} (&gt; 60 min)
          </span>
        )}
      </div>

      <div className="scc-summary">
        <div>
          <div className="pir-metric-label">Deducted so far</div>
          <div className="pir-metric-value">{fmtMinutes(view.deducted_minutes)}</div>
        </div>
        <div>
          <div className="pir-metric-label">Outage window (EAT)</div>
          <div className="scc-window">
            {fmtDateTime(view.window_start)} → {view.restored ? fmtDateTime(view.window_end) : "still open"}
          </div>
        </div>
        <div className="muted" style={{ alignSelf: "end" }}>
          {view.restored
            ? "Final for this incident. The scorecard recomputes over its own period."
            : "Live until the incident is restored."}
        </div>
      </div>

      {view.events.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Code</th>
              <th>Started → ended (EAT)</th>
              <th>State</th>
              <th>Recorded</th>
              <th>Reason</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {view.events.map((ev) => {
              const st = stateChip(ev);
              return (
                <tr key={ev.id}>
                  <td className="pir-mono">{ev.scc_code}</td>
                  <td>
                    {fmtDateTime(ev.started_at)} → {ev.ended_at ? fmtDateTime(ev.ended_at) : "open"}
                  </td>
                  <td>
                    <span className={st.cls}>{st.word}</span>
                  </td>
                  <td>
                    <div className="muted">
                      {ev.opened_by} · {ev.opened_role}
                    </div>
                    <div className="muted">
                      {fmtDateTime(ev.opened_at)} · {ev.opening_delay_min} min after start
                    </div>
                    {ev.late && <span className="chip danger">LATE OPENING</span>}
                  </td>
                  <td>
                    {ev.reason}
                    {ev.reversed && (
                      <div className="muted">
                        Reversed {fmtDateTime(ev.reversed_at)} by {ev.reversed_by || "—"}:{" "}
                        {ev.reversal_reason || "—"}
                      </div>
                    )}
                  </td>
                  <td>
                    {!ev.reversed && ev.open && (
                      <button className="btn" disabled={busy} onClick={() => closeClock(ev)}>
                        Close
                      </button>
                    )}
                    {!ev.reversed && reversing !== ev.id && (
                      <button
                        className="btn"
                        disabled={busy}
                        onClick={() => {
                          setReversing(ev.id);
                          setReverseReason("");
                        }}
                      >
                        Reverse…
                      </button>
                    )}
                    {!ev.reversed && reversing === ev.id && (
                      <div className="scc-reverse">
                        <input
                          autoFocus
                          placeholder="Why is this SCC being reversed? (required)"
                          value={reverseReason}
                          disabled={busy}
                          onChange={(e) => setReverseReason(e.target.value)}
                        />
                        <button
                          className="btn danger"
                          disabled={busy || !reverseReason.trim()}
                          onClick={() => reverseClock(ev)}
                        >
                          Reverse
                        </button>
                        <button className="btn" disabled={busy} onClick={() => setReversing(null)}>
                          Keep
                        </button>
                      </div>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      {view.events.length === 0 && (
        <p className="muted">No stop clock recorded on this incident. The vendor is charged for the whole window.</p>
      )}

      <div className="scc-open">
        <select value={code} disabled={busy} onChange={(e) => setCode(e.target.value)}>
          {SCC_CODES.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
        <input
          placeholder="Reason — required before the clock can be stopped"
          value={reason}
          disabled={busy}
          onChange={(e) => setReason(e.target.value)}
        />
        <button
          className="btn primary"
          disabled={busy || !reason.trim()}
          title={reason.trim() ? "Stop the vendor clock from now" : "Type the reason first — it is mandatory"}
          onClick={openClock}
        >
          Stop clock
        </button>
      </div>
      {note && <p className="chip ok">{note}</p>}
      {problem && (
        <div className="pir-error">
          <span className="chip warn">REFUSED</span>
          <div>{problem}</div>
        </div>
      )}
    </div>
  );
}
