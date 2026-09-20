import { useCallback, useEffect, useState } from "react";
import { api } from "../api";

/**
 * Planned maintenance (spec §7.5) — windows, the tasks booked into them, and the two
 * approval gates.
 *
 * Three things this page is built to show rather than hide.
 *
 * 1. **The two gates are different questions.** A task's APPROVE_SCHEDULE signs off the
 *    programme; a window's APPROVE_MAINTENANCE_WINDOW signs off going ahead on the night.
 *    The window row therefore shows its own approval state and never borrows a task's, and
 *    "Schedule" is disabled with the reason on the button until the window's own card is
 *    approved on the HITL Inbox.
 * 2. **The rain guard's "no forecast" is not "no rain".** The badge renders four verdicts,
 *    not two, and NO_FORECAST / NO_FORECAST_RAIN_SEASON are worded as *the guard could not
 *    check*, never as a pass. WEATHER_ENABLED is off by default, so this is the common case.
 * 3. **The flag is off by default**, and the API answers 404 for the whole lane. That is not
 *    an error to shout about: the page says the lane is off and how to turn it on, the same
 *    way RiskStrip treats a missing weather endpoint.
 *
 * Approving happens on the HITL Inbox, deliberately. This page raises cards and acts on
 * approvals; it does not grow a second approve button with its own rules.
 */

type MaintWindow = {
  id: string;
  scope: string;
  scope_ref: string;
  status: string;
  starts_at_eat: string;
  ends_at_eat: string;
  uid: string;
  sequence: number;
  rain_season_flag: number;
  ca_approval_ref: string | null;
  ca_approval_required: boolean;
  customer_notice_sent_at: string | null;
  approved_by: string | null;
  hitl_task_id: string | null;
  tasks?: any[];
  rain_guard?: { verdict: string; reason: string; blocking: boolean };
};

const RAIN_LABEL: Record<string, string> = {
  CLEAR: "Rain guard: checked, clear",
  STORM_FORECAST: "Rain guard: STORM forecast",
  NO_FORECAST_RAIN_SEASON: "Rain guard: no forecast, rain season",
  NO_FORECAST: "Rain guard: no forecast (did not run)",
};

function rainClass(verdict?: string) {
  if (verdict === "CLEAR") return "chip ok";
  if (verdict === "STORM_FORECAST") return "chip bad";
  return "chip"; // neither good nor bad: the guard had nothing to say
}

function statusClass(status: string) {
  if (status === "SCHEDULED") return "chip ok";
  if (status === "CANCELLED") return "chip bad";
  return "chip";
}

export default function Maintenance({ tick }: { tick: number }) {
  const [windows, setWindows] = useState<MaintWindow[]>([]);
  const [tasks, setTasks] = useState<any[]>([]);
  const [off, setOff] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const load = useCallback(() => {
    Promise.all([api.maintenanceWindows(), api.maintenanceTasks("")])
      .then(([w, t]) => {
        setWindows(w);
        setTasks(t);
        setOff(false);
      })
      .catch((e) => {
        // 404 is the whole lane being off, which is the shipped default — not a failure.
        if (String(e.message || "").startsWith("404")) setOff(true);
        else setNote(String(e.message || e));
      });
  }, []);

  useEffect(() => {
    load();
  }, [load, tick]);

  const act = async (id: string, fn: () => Promise<any>, what: string) => {
    setBusy(id);
    setNote(null);
    try {
      await fn();
      setNote(`${what} ok`);
    } catch (e: any) {
      // The server distinguishes "nobody approved this", "the Authority has not written
      // back", "there is a storm" and "another crew is already booked". Show its words.
      setNote(String(e.message || e));
    } finally {
      setBusy(null);
      load();
    }
  };

  if (off) {
    return (
      <div>
        <h2 style={{ marginTop: 0 }}>Planned Maintenance</h2>
        <div className="panel">
          <div className="empty">
            Planned maintenance is not enabled on this deployment. Set{" "}
            <code>MAINTENANCE_ENABLED=true</code> in <code>.env</code> and restart the API.
          </div>
        </div>
      </div>
    );
  }

  return (
    <div>
      <h2 style={{ marginTop: 0 }}>Planned Maintenance</h2>
      <p className="muted">
        Windows take live customers off air on purpose, so two separate people sign off: the
        programme (<code>APPROVE_SCHEDULE</code>, per task) and the night itself (
        <code>APPROVE_MAINTENANCE_WINDOW</code>, per window). Both are approved on the HITL
        Inbox. Times are EAT.
      </p>
      {note && (
        <div className="panel" style={{ marginBottom: "0.75rem" }}>
          <div className="muted">{note}</div>
        </div>
      )}

      <div className="panel">
        <h3 style={{ marginTop: 0 }}>Windows</h3>
        <table>
          <thead>
            <tr>
              <th>Scope</th>
              <th>When (EAT)</th>
              <th>Status</th>
              <th>Rain guard</th>
              <th>CA ref</th>
              <th>Notice</th>
              <th>Approved by</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {windows.map((w) => (
              <tr key={w.id}>
                <td>
                  {w.scope} {w.scope_ref}
                  <div className="muted" style={{ fontSize: "0.75rem" }}>
                    {w.uid} · SEQ {w.sequence}
                  </div>
                </td>
                <td>
                  {w.starts_at_eat} → {w.ends_at_eat}
                </td>
                <td>
                  <span className={statusClass(w.status)}>{w.status}</span>
                </td>
                <td>
                  <span
                    className={rainClass(w.rain_guard?.verdict)}
                    title={w.rain_guard?.reason || "open the window for the live verdict"}
                  >
                    {RAIN_LABEL[w.rain_guard?.verdict || ""] ||
                      (w.rain_season_flag ? "Rain season flagged" : "—")}
                  </span>
                </td>
                <td className="muted">
                  {w.ca_approval_ref || (w.ca_approval_required ? "REQUIRED — missing" : "n/a (SITE)")}
                </td>
                <td className="muted">{w.customer_notice_sent_at ? "sent" : "not recorded"}</td>
                <td className="muted">{w.approved_by || "—"}</td>
                <td>
                  {w.status === "PROPOSED" && !w.hitl_task_id && (
                    <button
                      className="btn"
                      disabled={busy === w.id}
                      onClick={() => act(w.id, () => api.maintenanceRequestWindowApproval(w.id), "approval requested")}
                    >
                      Request approval
                    </button>
                  )}
                  {w.status === "PROPOSED" && (
                    <button
                      className="btn"
                      disabled={busy === w.id || !w.hitl_task_id}
                      title={
                        w.hitl_task_id
                          ? "Only works once this window's own APPROVE_MAINTENANCE_WINDOW card is approved"
                          : "Raise the approval card first"
                      }
                      onClick={() => act(w.id, () => api.maintenanceScheduleWindow(w.id), "scheduled")}
                    >
                      Schedule
                    </button>
                  )}
                  {(w.status === "PROPOSED" || w.status === "SCHEDULED") && (
                    <button
                      className="btn"
                      disabled={busy === w.id}
                      onClick={() => {
                        const reason = window.prompt("Why is this window being cancelled?") || "";
                        if (reason.trim()) act(w.id, () => api.maintenanceCancelWindow(w.id, reason), "cancelled");
                      }}
                    >
                      Cancel
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {windows.length === 0 && <div className="empty">No maintenance windows yet.</div>}
      </div>

      <div className="panel" style={{ marginTop: "1rem" }}>
        <h3 style={{ marginTop: 0 }}>Tasks</h3>
        <table>
          <thead>
            <tr>
              <th>Work</th>
              <th>Site</th>
              <th>Region</th>
              <th>Due (EAT)</th>
              <th>Status</th>
              <th>Assignee token</th>
              <th>Standard</th>
            </tr>
          </thead>
          <tbody>
            {tasks.map((t) => (
              <tr key={t.id}>
                <td>{t.task_type || "—"}</td>
                <td>{t.site_id}</td>
                <td>{t.region_code || "—"}</td>
                <td>{t.due_at_eat}</td>
                <td>
                  <span className={t.status === "MISSED" ? "chip bad" : "chip"}>{t.status}</span>
                </td>
                {/* A role token, never a person's name (§7.11.8). The proposal is shown
                    beside the approved value so "the system suggested X, a human chose Y"
                    stays visible instead of being overwritten in place. */}
                <td className="muted">
                  {t.assignee_token || t.proposed_assignee_token || "—"}
                  {t.assignee_token && t.proposed_assignee_token && t.assignee_token !== t.proposed_assignee_token
                    ? ` (proposed ${t.proposed_assignee_token})`
                    : ""}
                </td>
                <td className="muted">{t.standard_ref || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {tasks.length === 0 && (
          <div className="empty">
            No maintenance tasks yet. The <code>maintenance_plan_due</code> job proposes them from
            active plans once it is wired into the scheduler.
          </div>
        )}
      </div>
    </div>
  );
}
