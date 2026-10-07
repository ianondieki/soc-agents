import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  AirVent,
  BatteryCharging,
  Cable,
  Check,
  CloudRain,
  Fuel,
  RadioTower,
  Wrench,
  Zap,
  type LucideIcon,
} from "lucide-react";
import { api } from "../api";
import LaneOff from "../components/LaneOff";
import { humanEnum, regionName } from "../lib/agents";
import { IconDot } from "../lib/icons";
import { fmtDate, parseInstant } from "../lib/time";
import "./Maintenance.css";

/**
 * Planned maintenance (spec §7.5) — windows, the tasks booked into them, and the two
 * approval gates.
 *
 * Three things this page is built to show rather than hide.
 *
 * 1. **The two gates are different questions.** A task's APPROVE_SCHEDULE signs off the
 *    programme; a window's APPROVE_MAINTENANCE_WINDOW signs off going ahead on the night.
 *    Each window carries its own sign-off steps (proposed, sign-off asked, signed off,
 *    scheduled) and never borrows a task's, and "Schedule" is disabled with the reason on the
 *    button until the window's own card exists on Approvals (the HITL inbox).
 * 2. **The rain guard's "no forecast" is not "no rain".** The guard has four verdicts, not
 *    two, and NO_FORECAST / NO_FORECAST_RAIN_SEASON are worded as *the guard could not
 *    check*, never as a pass. WEATHER_ENABLED is off by default, so this is the common case.
 *    The list does not carry the verdict, so each live window's own record is read for it.
 * 3. **The flag is off by default**, and the API answers 404 for the whole lane. That is not
 *    an error to shout about: the page says the lane is off and how to turn it on.
 *
 * Approving happens on Approvals, deliberately. This page raises cards and acts on
 * approvals; it does not grow a second approve button with its own rules.
 */

type RainGuard = { verdict: string; reason: string; blocking: boolean };

type MaintWindow = {
  id: string;
  scope: string;
  scope_ref: string;
  status: string;
  starts_at: string;
  ends_at: string;
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
  rain_guard?: RainGuard;
};

/** The guard's verdict in words. Only a storm earns colour; "no forecast" is never a pass. */
const RAIN_WORDS: Record<string, string> = {
  CLEAR: "Rain guard checked: clear",
  STORM_FORECAST: "Storm forecast",
  NO_FORECAST_RAIN_SEASON: "Rain season, and no forecast to check",
  NO_FORECAST: "Rain guard did not run: no forecast",
};

/** A task type's icon. Identity only; the words carry the meaning. */
const WORK_ICONS: Record<string, LucideIcon> = {
  GENERATOR_EXERCISE: Zap,
  GENERATOR_LOAD_BANK: Zap,
  FUEL_RUN: Fuel,
  BATTERY_CHECK: BatteryCharging,
  BATTERY_CAPACITY: BatteryCharging,
  TOWER_VISUAL: RadioTower,
  TOWER_STRUCTURAL: RadioTower,
  FIBRE_PATROL: Cable,
  AC_SERVICE: AirVent,
  GROUNDING_CHECK: Zap,
};

/** A task type as the field says it. */
const WORK_WORDS: Record<string, string> = {
  GENERATOR_EXERCISE: "Generator exercise",
  GENERATOR_LOAD_BANK: "Generator load-bank test",
  FUEL_RUN: "Fuel run",
  BATTERY_CHECK: "Battery check",
  BATTERY_CAPACITY: "Battery capacity test",
  TOWER_VISUAL: "Tower visual check",
  TOWER_STRUCTURAL: "Tower structural check",
  FIBRE_PATROL: "Fibre patrol",
  AC_SERVICE: "Air-conditioning service",
  GROUNDING_CHECK: "Earthing check",
};

/** Statuses a task is still live in. */
const LIVE_TASKS = new Set(["PROPOSED", "SCHEDULED", "INVITED", "IN_PROGRESS"]);
/** The task list shows this many until asked for the rest. */
const TASKS_SHOWN = 10;
const DAY_MS = 86_400_000;

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** An instant's calendar parts in EAT. Kenya is UTC+3 all year, so the shift is exact. */
function eat(value: unknown) {
  const d = parseInstant(value);
  if (!d) return null;
  const t = new Date(d.getTime() + 3 * 3_600_000);
  const hm = `${String(t.getUTCHours()).padStart(2, "0")}:${String(t.getUTCMinutes()).padStart(2, "0")}`;
  return {
    ms: d.getTime(),
    wd: WEEKDAYS[t.getUTCDay()],
    day: t.getUTCDate(),
    mon: MONTHS[t.getUTCMonth()],
    hm,
    ymd: `${t.getUTCFullYear()}-${t.getUTCMonth()}-${t.getUTCDate()}`,
  };
}

/** "4 h" or "2 h 30 min": how long a window takes customers off air. */
function span(fromMs: number, toMs: number): string {
  const mins = Math.max(0, Math.round((toMs - fromMs) / 60_000));
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  if (!h) return `${m} min`;
  return m ? `${h} h ${m} min` : `${h} h`;
}

/** "today", "tomorrow", "in 7 days", "3 days ago". */
function relDays(ms: number, now: number): string {
  const days = Math.round((ms - now) / DAY_MS);
  if (days === 0) return "today";
  if (days === 1) return "tomorrow";
  if (days === -1) return "yesterday";
  return days > 0 ? `in ${days} days` : `${-days} days ago`;
}

/** A window's sign-off, as four steps. The window never borrows a task's approval. */
function stepsOf(w: MaintWindow) {
  const scheduled = w.status === "SCHEDULED" || w.status === "COMPLETED";
  return [
    { key: "proposed", label: "Proposed", done: true },
    { key: "asked", label: "Sign-off asked", done: Boolean(w.hitl_task_id) || scheduled },
    { key: "signed", label: w.approved_by ? `Signed off by ${w.approved_by}` : "Signed off", done: Boolean(w.approved_by) || scheduled },
    { key: "scheduled", label: "Scheduled", done: scheduled },
  ];
}

const sentence = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s);

export default function Maintenance({ tick, profile }: { tick: number; profile?: any }) {
  const [windows, setWindows] = useState<MaintWindow[]>([]);
  const [tasks, setTasks] = useState<any[]>([]);
  const [siteNames, setSiteNames] = useState<Record<string, string>>({});
  const [rain, setRain] = useState<Record<string, RainGuard>>({});
  const [off, setOff] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [failed, setFailed] = useState(false);
  const [allTasks, setAllTasks] = useState(false);
  // Cancel asks for its reason inline, on the window (no browser prompt).
  const [cancelling, setCancelling] = useState<string | null>(null);
  const [cancelReason, setCancelReason] = useState("");

  const load = useCallback(() => {
    Promise.all([api.maintenanceWindows(), api.maintenanceTasks("")])
      .then(([w, t]) => {
        const list: MaintWindow[] = Array.isArray(w) ? w : [];
        setWindows(list);
        setTasks(Array.isArray(t) ? t : []);
        setOff(false);
        setLoaded(true);
        setFailed(false);
        // The rain guard's verdict lives on each window's own record, not on the list.
        for (const x of list) {
          if (x.status !== "PROPOSED" && x.status !== "SCHEDULED") continue;
          api
            .maintenanceWindow(x.id)
            .then((d: any) => {
              if (d?.rain_guard?.verdict) setRain((r) => ({ ...r, [x.id]: d.rain_guard }));
            })
            .catch(() => undefined);
        }
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

  // Site names for windows scoped to one site; the code stays beside the name. The site
  // register first, then the tickets (a site can be on a ticket before it is in the register).
  useEffect(() => {
    const add = (rows: unknown) => {
      const out: Record<string, string> = {};
      for (const s of Array.isArray(rows) ? rows : []) if (s?.site_id && s?.site_name) out[s.site_id] = s.site_name;
      setSiteNames((prev) => ({ ...out, ...prev }));
    };
    api.sites().then(add).catch(() => undefined);
    api.incidents().then(add).catch(() => undefined);
  }, []);

  const act = async (id: string, fn: () => Promise<any>, what: string) => {
    setBusy(id);
    setNote(null);
    try {
      await fn();
      setNote(`${sentence(what)}.`);
    } catch (e: any) {
      // The server distinguishes "nobody approved this", "the Authority has not written
      // back", "there is a storm" and "another crew is already booked". Show its words.
      setNote(String(e.message || e));
    } finally {
      setBusy(null);
      load();
    }
  };

  const now = Date.now();
  const sorted = useMemo(
    () => [...windows].sort((a, b) => (parseInstant(a.starts_at)?.getTime() ?? 0) - (parseInstant(b.starts_at)?.getTime() ?? 0)),
    [windows],
  );
  const ahead = sorted.filter(
    (w) => (w.status === "PROPOSED" || w.status === "SCHEDULED") && (parseInstant(w.ends_at)?.getTime() ?? 0) > now,
  );
  // Waiting for sign-off means a card is on Approvals, so this figure and that queue agree. A proposed
  // window nobody has asked about yet is counted under "Windows ahead" and has its own button.
  const waiting = sorted.filter((w) => w.status === "PROPOSED" && w.hitl_task_id && !w.approved_by).length;
  const scheduled = sorted.filter((w) => w.status === "SCHEDULED").length;
  const sortedTasks = useMemo(
    () => [...tasks].sort((a, b) => (parseInstant(a.due_at)?.getTime() ?? 0) - (parseInstant(b.due_at)?.getTime() ?? 0)),
    [tasks],
  );
  const soon = sortedTasks.filter(
    (t) => LIVE_TASKS.has(t.status) && (parseInstant(t.due_at)?.getTime() ?? Infinity) - now <= 30 * DAY_MS,
  ).length;
  const missed = sortedTasks.filter((t) => t.status === "MISSED").length;
  const next = ahead[0] ? eat(ahead[0].starts_at) : null;
  const shownTasks = allTasks ? sortedTasks : sortedTasks.slice(0, TASKS_SHOWN);

  const head = (
    <div className="page-head">
      <div>
        <h1>Maintenance</h1>
        <p
          className="lead"
          title="Windows take live subscribers off air on purpose, so two people sign off: the programme (approve schedule, per task) and the night itself (approve maintenance window, per window). Both are approved on Approvals."
        >
          Planned windows and the work booked into them; each window is signed off before it goes ahead. Times in EAT.
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
    act(w.id, () => api.maintenanceCancelWindow(w.id, reason), "window cancelled");
  };

  const placeOf = (w: MaintWindow) => {
    const scope = String(w.scope || "").toUpperCase();
    if (scope === "SITE") return { name: siteNames[w.scope_ref] || w.scope_ref, sub: `Site ${w.scope_ref}` };
    if (scope === "REGION") return { name: regionName(w.scope_ref, profile), sub: `Region ${w.scope_ref}` };
    if (scope === "NETWORK") return { name: "Whole network", sub: w.scope_ref ? `Network ${w.scope_ref}` : "Network" };
    return { name: w.scope_ref || "—", sub: sentence(humanEnum(scope)) };
  };

  return (
    <div className="mt">
      {head}

      {loaded && (
        <dl className="mt-figures">
          <div>
            <dt>Windows ahead</dt>
            <dd>{ahead.length}</dd>
          </div>
          <div className={waiting ? "hitl" : undefined}>
            <dt>Waiting for sign-off</dt>
            <dd>{waiting}</dd>
          </div>
          <div className={scheduled ? "ok" : undefined}>
            <dt>Scheduled</dt>
            <dd>{scheduled}</dd>
          </div>
          <div className={missed ? "bad" : undefined}>
            <dt>{missed ? "Tasks missed" : "Tasks due in 30 days"}</dt>
            <dd>{missed || soon}</dd>
          </div>
          <div>
            <dt>Next window</dt>
            <dd className="mt-figure-word">{next ? `${next.wd} ${next.day} ${next.mon}, ${next.hm}` : "None booked"}</dd>
          </div>
        </dl>
      )}

      {note && (
        <div className="panel mt-note" role="status">
          {note}
        </div>
      )}

      <section className="panel mt-panel" aria-labelledby="mt-windows">
        <div className="panel-head">
          <h2 id="mt-windows" className="panel-title">
            Windows
          </h2>
          {loaded && <span className="muted">{sorted.length === 1 ? "1 window" : `${sorted.length} windows`}</span>}
        </div>

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
        {loaded && sorted.length === 0 && (
          <div className="empty">No maintenance windows yet. A planner proposes one; it is signed off on Approvals.</div>
        )}

        {sorted.length > 0 && (
          <ol className="mt-windows">
            {sorted.map((w) => {
              const start = eat(w.starts_at);
              const end = eat(w.ends_at);
              const place = placeOf(w);
              const live = w.status === "PROPOSED" || w.status === "SCHEDULED";
              const steps = stepsOf(w);
              const current = steps.findIndex((s) => !s.done);
              const guard = rain[w.id] || w.rain_guard;
              const rainWords = guard ? RAIN_WORDS[guard.verdict] || humanEnum(guard.verdict) : w.rain_season_flag ? "Rain season" : null;
              const storm = guard?.verdict === "STORM_FORECAST";
              const caMissing = w.ca_approval_required && !w.ca_approval_ref;
              return (
                <li key={w.id} className={"mt-window" + (live ? "" : " past")}>
                  <div className="mt-date" aria-hidden="true">
                    <span className="mt-date-wd">{start?.wd}</span>
                    <span className="mt-date-day">{start?.day}</span>
                    <span className="mt-date-mon">{start?.mon}</span>
                  </div>

                  <div className="mt-main">
                    <div className="mt-title-row">
                      <h3 className="mt-title">{place.name}</h3>
                      {w.status === "SCHEDULED" ? (
                        <span className="chip ok">Scheduled</span>
                      ) : w.status === "CANCELLED" ? (
                        <span className="chip danger">Cancelled</span>
                      ) : w.status === "COMPLETED" ? (
                        <span className="chip">Done</span>
                      ) : null}
                    </div>
                    <p className="mt-when">
                      <span>
                        {start ? `${start.wd} ${start.day} ${start.mon}, ${start.hm}` : w.starts_at_eat} to {end?.hm ?? w.ends_at_eat}
                        {start && end && start.ymd !== end.ymd ? " the next day" : ""}
                      </span>
                      {start && end && <span className="muted">{span(start.ms, end.ms)} off air</span>}
                      <span className="muted mono" title={`Calendar invite ${w.uid}, sequence ${w.sequence}`}>
                        {place.sub}
                      </span>
                    </p>

                    {live && (
                      <ol className="mt-steps" aria-label="Sign-off">
                        {steps.map((s, i) => (
                          <li
                            key={s.key}
                            className={"mt-step" + (s.done ? " done" : "") + (i === current ? " current" : "")}
                            aria-current={i === current ? "step" : undefined}
                          >
                            <span className="mt-step-dot" aria-hidden="true">
                              {s.done ? <Check size={11} strokeWidth={3} /> : null}
                            </span>
                            <span>{s.label}</span>
                          </li>
                        ))}
                      </ol>
                    )}

                    <ul className="mt-facts">
                      {rainWords && (
                        <li className={storm ? "bad" : undefined} title={guard?.reason || "The rain guard's verdict, read from this window's record"}>
                          <CloudRain size={14} strokeWidth={1.75} aria-hidden="true" />
                          {rainWords}
                        </li>
                      )}
                      <li className={caMissing ? "bad" : undefined}>
                        {caMissing ? (
                          <>
                            <IconDot /> Authority approval required, missing
                          </>
                        ) : w.ca_approval_ref ? (
                          <>
                            Authority approval <span className="mono">{w.ca_approval_ref}</span>
                          </>
                        ) : (
                          "No Authority approval needed for one site"
                        )}
                      </li>
                      <li>
                        {w.customer_notice_sent_at
                          ? `Customer notice sent ${fmtDate(w.customer_notice_sent_at)}`
                          : "Customer notice not recorded"}
                      </li>
                    </ul>

                    {w.hitl_task_id && w.status === "PROPOSED" && !w.approved_by && (
                      <p className="mt-hint">
                        Its sign-off card is waiting on{" "}
                        <Link to={`/hitl?task=${encodeURIComponent(w.hitl_task_id)}`}>Approvals</Link>.
                      </p>
                    )}

                    {live && cancelling === w.id && (
                      <div className="mt-cancel" role="group" aria-label="Cancel this window">
                        <input
                          autoFocus
                          aria-label="Why is this window being cancelled?"
                          placeholder="Why is this window being cancelled? (required)"
                          value={cancelReason}
                          disabled={busy === w.id}
                          onChange={(e) => setCancelReason(e.target.value)}
                          onKeyDown={(e) => e.key === "Enter" && cancel(w)}
                        />
                        <button className="btn sm danger" disabled={busy === w.id || !cancelReason.trim()} onClick={() => cancel(w)}>
                          Cancel window
                        </button>
                        <button className="btn sm" onClick={() => setCancelling(null)}>
                          Keep it
                        </button>
                      </div>
                    )}
                  </div>

                  {live && cancelling !== w.id && (
                    <div className="mt-actions">
                      {w.status === "PROPOSED" && !w.hitl_task_id && (
                        <button
                          className="btn sm primary"
                          disabled={busy === w.id}
                          onClick={() => act(w.id, () => api.maintenanceRequestWindowApproval(w.id), "sign-off asked for on Approvals")}
                        >
                          Ask for sign-off
                        </button>
                      )}
                      {w.status === "PROPOSED" && (
                        <button
                          className={"btn sm" + (w.approved_by ? " primary" : "")}
                          disabled={busy === w.id || !w.hitl_task_id}
                          title={
                            w.hitl_task_id
                              ? "Only works once this window's own sign-off card is approved on Approvals"
                              : "Ask for sign-off first"
                          }
                          onClick={() => act(w.id, () => api.maintenanceScheduleWindow(w.id), "window scheduled")}
                        >
                          Schedule
                        </button>
                      )}
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
                    </div>
                  )}
                </li>
              );
            })}
          </ol>
        )}
      </section>

      <section className="panel mt-panel mt-tasks-panel" aria-labelledby="mt-tasks">
        <div className="panel-head">
          <h2 id="mt-tasks" className="panel-title">
            Tasks
          </h2>
          {loaded && <span className="muted">{sortedTasks.length === 1 ? "1 task" : `${sortedTasks.length} tasks`}, soonest first</span>}
        </div>
        {loaded && sortedTasks.length === 0 && (
          <div className="empty">
            No maintenance tasks yet. The <code>maintenance_plan_due</code> job proposes them from active plans once it is
            wired into the scheduler.
          </div>
        )}
        {sortedTasks.length > 0 && (
          // On a tablet the table scrolls inside its panel rather than widening the page.
          <div className="mt-scroll">
            <table className="mt-table">
              <thead>
                <tr>
                  <th>Work</th>
                  <th>Site</th>
                  <th>Due (EAT)</th>
                  <th>Status</th>
                  <th>Assigned to</th>
                  <th>Standard</th>
                </tr>
              </thead>
              <tbody>
                {shownTasks.map((t) => {
                  const Icon = WORK_ICONS[String(t.task_type || "").toUpperCase()] || Wrench;
                  const due = parseInstant(t.due_at);
                  const late = due && LIVE_TASKS.has(t.status) && due.getTime() < now;
                  return (
                    <tr key={t.id} className="mt-task">
                      <td className="mt-work">
                        <span className="mt-work-icon" aria-hidden="true">
                          <Icon size={16} strokeWidth={1.75} />
                        </span>
                        {t.task_type ? WORK_WORDS[String(t.task_type).toUpperCase()] || sentence(humanEnum(t.task_type)) : "—"}
                      </td>
                      <td className="mt-site">
                        <span className="mt-site-name">{t.site_name || siteNames[t.site_id] || t.site_id}</span>
                        <span className="mt-site-sub">
                          {(t.site_name || siteNames[t.site_id]) && <span className="mono">{t.site_id}</span>}
                          {t.region_code && <span>{regionName(t.region_code, profile)}</span>}
                        </span>
                      </td>
                      <td className="mt-due" data-label="Due">
                        <span>{due ? fmtDate(t.due_at) : t.due_at_eat}</span>
                        {due && <span className={"mt-due-rel" + (late ? " late" : "")}>{relDays(due.getTime(), now)}</span>}
                      </td>
                      <td data-label="Status">
                        {t.status === "MISSED" ? (
                          <span className="chip danger">Missed</span>
                        ) : t.status === "DONE" ? (
                          <span className="chip ok">Done</span>
                        ) : t.status === "SCHEDULED" || t.status === "INVITED" ? (
                          <span className="chip accent">{sentence(humanEnum(t.status))}</span>
                        ) : (
                          <span className="mt-status-word">
                            {sentence(humanEnum(t.status))}
                            {t.status === "PROPOSED" && t.hitl_task_id && <span className="muted">sign-off asked</span>}
                          </span>
                        )}
                      </td>
                      {/* A role token, never a person's name (§7.11.8). The proposal is shown
                          beside the approved value, so "the system suggested X, a human chose Y"
                          stays visible instead of being overwritten in place. */}
                      <td data-label="Assigned to">
                        {t.assignee_token ? (
                          <span className="mono">{t.assignee_token}</span>
                        ) : t.proposed_assignee_token ? (
                          <span>
                            <span className="mono">{t.proposed_assignee_token}</span> <span className="muted">proposed</span>
                          </span>
                        ) : (
                          <span className="muted">—</span>
                        )}
                        {t.assignee_token && t.proposed_assignee_token && t.assignee_token !== t.proposed_assignee_token && (
                          <span className="muted"> (proposed {t.proposed_assignee_token})</span>
                        )}
                      </td>
                      <td className="mt-std" data-label="Standard">
                        {t.standard_ref || "—"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        {sortedTasks.length > TASKS_SHOWN && (
          <div className="mt-more">
            <button className="btn sm" onClick={() => setAllTasks((v) => !v)} aria-expanded={allTasks}>
              {allTasks ? "Show the first ten" : `Show all ${sortedTasks.length} tasks`}
            </button>
          </div>
        )}
      </section>
    </div>
  );
}
