import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import LaneOff from "../components/LaneOff";
import { humanEnum } from "../lib/agents";
import { IconDot } from "../lib/icons";

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
 *    approved on Approvals (the HITL inbox).
 * 2. **The rain guard's "no forecast" is not "no rain".** The badge renders four verdicts,
 *    not two, and NO_FORECAST / NO_FORECAST_RAIN_SEASON are worded as *the guard could not
 *    check*, never as a pass. WEATHER_ENABLED is off by default, so this is the common case.
 * 3. **The flag is off by default**, and the API answers 404 for the whole lane. That is not
 *    an error to shout about: the page says the lane is off and how to turn it on, the same
 *    way RiskStrip treats a missing weather endpoint.
 *
 * Approving happens on Approvals, deliberately. This page raises cards and acts on
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

/** The column is headed "Rain guard", so the words are the verdict alone. */
const RAIN_LABEL: Record<string, string> = {
  CLEAR: "checked, clear",
  STORM_FORECAST: "storm forecast",
  NO_FORECAST_RAIN_SEASON: "no forecast, rain season",
  NO_FORECAST: "no forecast (did not run)",
};

/** Only a storm earns a chip; "no forecast" is said in words and is never painted as a pass. */
function rainClass(verdict?: string): string | null {
  if (verdict === "STORM_FORECAST") return "chip danger";
  return null;
}

/** PROPOSED is where every window starts, so it is a word; the states after it are chips. */
function statusClass(status: string): string | null {
  if (status === "SCHEDULED") return "chip ok";
  if (status === "CANCELLED") return "chip danger";
  return null;
}

export default function Maintenance({ tick }: { tick: number }) {
  const [windows, setWindows] = useState<MaintWindow[]>([]);
  const [tasks, setTasks] = useState<any[]>([]);
  const [off, setOff] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [failed, setFailed] = useState(false);
  // Cancel asks for its reason inline, in the row (no browser prompt).
  const [cancelling, setCancelling] = useState<string | null>(null);
  const [cancelReason, setCancelReason] = useState("");

  const load = useCallback(() => {
    Promise.all([api.maintenanceWindows(), api.maintenanceTasks("")])
      .then(([w, t]) => {
        setWindows(w);
        setTasks(t);
        setOff(false);
        setLoaded(true);
        setFailed(false);
      })
      .catch((e) => {
        // 404 is the whole lane being off, which is the shipped default — not a failure.
        if (String(e.message || "").startsWith("404")) setOff(true);
        else {
          setNote(String(e.message || e));
          setFailed(true);
        }
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

  const head = (
    <div className="page-head">
      <div>
        <h1>Maintenance</h1>
        <p
          className="lead"
          title="Windows take live customers off air on purpose, so two people sign off: the programme (approve schedule, per task) and the night itself (approve maintenance window, per window). Both are approved on Approvals."
        >
          Planned windows and their tasks; two people sign off each. Times in EAT.
        </p>
      </div>
    </div>
  );

  if (off) {
    return (
      <div>
        {head}
        <LaneOff title="Maintenance is off in this demo" flag="MAINTENANCE_ENABLED">
          plan maintenance windows and their tasks, each signed off by two people
        </LaneOff>
      </div>
    );
  }

  const cancel = (w: MaintWindow) => {
    const reason = cancelReason.trim();
    if (!reason) return;
    setCancelling(null);
    setCancelReason("");
    act(w.id, () => api.maintenanceCancelWindow(w.id, reason), "cancelled");
  };

  return (
    <div>
      {head}
      {note && (
        <div className="panel" style={{ marginBottom: "0.75rem" }} role="status">
          <div className="muted">{note}</div>
        </div>
      )}

      <div className="panel">
        <h2 className="panel-title">Windows</h2>
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
                  {humanEnum(w.scope)} {w.scope_ref}
                  <div className="facts">
                    <span className="mono">{w.uid}</span>
                    <span>sequence {w.sequence}</span>
                  </div>
                </td>
                <td>
                  {w.starts_at_eat} <span className="muted">to</span> {w.ends_at_eat}
                </td>
                <td>
                  {statusClass(w.status) ? (
                    <span className={statusClass(w.status) as string}>{humanEnum(w.status)}</span>
                  ) : (
                    humanEnum(w.status)
                  )}
                </td>
                <td>
                  <span
                    className={rainClass(w.rain_guard?.verdict) || "muted"}
                    title={w.rain_guard?.reason || "open the window for the live verdict"}
                  >
                    {RAIN_LABEL[w.rain_guard?.verdict || ""] ||
                      (w.rain_season_flag ? "rain season flagged" : "—")}
                  </span>
                </td>
                <td className="muted">
                  {w.ca_approval_ref ||
                    (w.ca_approval_required ? (
                      <span className="attn danger">
                        <IconDot /> required, missing
                      </span>
                    ) : (
                      "not needed (site)"
                    ))}
                </td>
                <td className="muted">{w.customer_notice_sent_at ? "sent" : "not recorded"}</td>
                <td className="muted">{w.approved_by || "—"}</td>
                <td>
                  {w.status === "PROPOSED" && !w.hitl_task_id && (
                    <button
                      className="btn sm"
                      disabled={busy === w.id}
                      onClick={() => act(w.id, () => api.maintenanceRequestWindowApproval(w.id), "approval requested")}
                    >
                      Request approval
                    </button>
                  )}
                  {w.status === "PROPOSED" && (
                    <button
                      className="btn sm"
                      disabled={busy === w.id || !w.hitl_task_id}
                      title={
                        w.hitl_task_id
                          ? "Only works once this window's own approve-maintenance-window card is approved on Approvals"
                          : "Raise the approval card first"
                      }
                      onClick={() => act(w.id, () => api.maintenanceScheduleWindow(w.id), "scheduled")}
                    >
                      Schedule
                    </button>
                  )}
                  {(w.status === "PROPOSED" || w.status === "SCHEDULED") && cancelling !== w.id && (
                    <button
                      className="btn sm"
                      disabled={busy === w.id}
                      onClick={() => {
                        setCancelling(w.id);
                        setCancelReason("");
                      }}
                    >
                      Cancel…
                    </button>
                  )}
                  {(w.status === "PROPOSED" || w.status === "SCHEDULED") && cancelling === w.id && (
                    <div className="scc-reverse" role="group" aria-label="Cancel this window">
                      <input
                        autoFocus
                        aria-label="Why is this window being cancelled?"
                        placeholder="Why is this window being cancelled? (required)"
                        value={cancelReason}
                        disabled={busy === w.id}
                        onChange={(e) => setCancelReason(e.target.value)}
                        onKeyDown={(e) => e.key === "Enter" && cancel(w)}
                      />
                      <button
                        className="btn sm danger"
                        disabled={busy === w.id || !cancelReason.trim()}
                        onClick={() => cancel(w)}
                      >
                        Cancel window
                      </button>
                      <button className="btn sm" onClick={() => setCancelling(null)}>
                        Keep
                      </button>
                    </div>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {!loaded && !failed && (
          <div className="skeleton-rows" aria-hidden="true">
            <span className="skeleton" />
            <span className="skeleton" />
            <span className="skeleton" />
          </div>
        )}
        {!loaded && failed && (
          <div className="empty" role="alert">
            Couldn't load the maintenance windows.{" "}
            <button className="btn sm" onClick={load}>
              Retry
            </button>
          </div>
        )}
        {loaded && windows.length === 0 && <div className="empty">No maintenance windows yet.</div>}
      </div>

      <div className="panel" style={{ marginTop: "1rem" }}>
        <h2 className="panel-title">Tasks</h2>
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
                <td>{t.task_type ? humanEnum(t.task_type) : "—"}</td>
                <td>{t.site_id}</td>
                <td>{t.region_code || "—"}</td>
                <td>{t.due_at_eat}</td>
                <td>
                  {t.status === "MISSED" ? <span className="chip danger">missed</span> : humanEnum(t.status)}
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
        {loaded && tasks.length === 0 && (
          <div className="empty">
            No maintenance tasks yet. The <code>maintenance_plan_due</code> job proposes them from
            active plans once it is wired into the scheduler.
          </div>
        )}
      </div>
    </div>
  );
}
