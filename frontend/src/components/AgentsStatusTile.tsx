import { useEffect, useState } from "react";
import { api } from "../api";
import { statusOf } from "../lib/apiError";
import { fmtDateTime, fmtTime, parseInstant } from "../lib/time";
import { useRealtimeState } from "../realtime/RealtimeContext";

/**
 * `AgentsStatusTile{lease, lastTick}` (spec §7.10) — the Wallboard's "AGENTS OFFLINE" badge
 * (§4.6, §10.4). Until this existed the condition was published and never drawn: the
 * `scheduler.job_failed` frame fell through to the generic renderer (CONFORMANCE C-19).
 *
 * TWO SIGNALS, ONE TRUTH. `GET /api/v1/scheduler/status` is the poll-side truth: it is read
 * from the database, so any process answers for the one that ticks. The WS frame
 * `scheduler.job_failed` (published only when a job's circuit opens, `circuit_open: true`)
 * marks the `scheduler` slice dirty and this tile re-reads the status within one debounce —
 * the frame is the doorbell, the route is who is at the door. The 5 s poll stays as the
 * fallback for a dropped socket.
 *
 * THE RULE THIS TILE IS BUILT AROUND: **only a fresh, successful reading may say OFFLINE.**
 * A permission error, a missing sign-in, a dead API or a hung request is "we cannot see", and
 * on a NOC wall that must never be drawn as "the agents are down" — the shift would start a
 * recovery drill for an outage that is not happening. So the failure paths are separate
 * states, grey, and worded as what they are.
 *
 * THE STATES, each in words (never colour alone, §7.10):
 *  - **AGENTS OFFLINE** (red): a reading from the last 15 s says the scheduler is enabled and
 *    its lease has not been renewed for more than 3 ticks. The tick is 5 s by default
 *    (`SCHEDULER_TICK_SECONDS`); the status payload does not report the configured tick, so
 *    3 × 5 s = 15 s is assumed. Decided on the server's `seconds_since_tick` alone — no
 *    client-side drift is added, so a slow poll cannot push a healthy scheduler over the line.
 *  - **CIRCUIT OPEN** (red, per job): a job failed 3 times running and the loop now skips it.
 *    `POST /api/v1/scheduler/run/{job}` (admin) resets it.
 *  - **OFF** (grey): `SCHEDULER_ENABLED` is false — a deliberate configuration, not an outage,
 *    and said as such ("Unattended jobs are off in this demo"). "Off" must not look like
 *    "healthy" either.
 *  - **NOT AVAILABLE TO YOUR ROLE** (grey): 403. With auth enforced, `/scheduler/status` is
 *    limited to platform readers; msp_coordinator, field_engineer, planning and legal get 403.
 *    Any earlier reading is dropped — it belonged to another role or session.
 *  - **SIGN IN** (grey): 401.
 *  - **STATUS UNKNOWN** (grey): the route failed for any other reason, or has not answered for
 *    15 s. Says since when, and what the last good reading said.
 *  - **AGENTS ONLINE** (green, quiet).
 *
 * Every relative age is paired with the absolute EAT instant (§7.10). Nothing blinks: the red
 * states use `.chip.danger`-family styles, which carry no animation, not `.chip.bad`, which
 * pulses.
 */

type Job = {
  name: string;
  interval_s: number;
  enabled: boolean;
  last_started_at: string | null;
  last_status: string | null;
  consecutive_failures: number;
  circuit_open: boolean;
};

type SchedulerStatus = {
  enabled: boolean;
  lease_owner: string | null;
  lease_expires_at: string | null;
  seconds_since_tick: number | null;
  jobs: Job[];
};

type Reading = { status: SchedulerStatus; at: number };
type Failure = { kind: "forbidden" | "signin" | "unreachable"; since: number };

/** §7.10: "red AGENTS OFFLINE when lease stale > 3 ticks"; default tick 5 s (scheduler/loop.py). */
const DEFAULT_TICK_S = 5;
const STALE_AFTER_S = 3 * DEFAULT_TICK_S;
const POLL_MS = 5000;
/** A reading older than three polls is no longer evidence of anything — it becomes UNKNOWN. */
const READING_FRESH_MS = 3 * POLL_MS;
const TICK_MS = 5000; // ages are shown in seconds; re-rendering with the poll is enough

function fmtAge(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return s + " s";
  const m = Math.floor(s / 60);
  if (m < 60) return m + " min " + (s % 60) + " s";
  const h = Math.floor(m / 60);
  return h + " h " + (m % 60) + " min";
}

function isOffline(s: SchedulerStatus): boolean {
  return s.enabled && (s.seconds_since_tick == null || s.seconds_since_tick > STALE_AFTER_S);
}

/** One line for "what the last good reading said", used only in the UNKNOWN state. */
function summarise(s: SchedulerStatus): string {
  const circuits = (s.jobs || []).filter((j) => j.circuit_open).length;
  const state = !s.enabled ? "scheduler off" : isOffline(s) ? "agents offline" : "agents online";
  return state + (circuits ? ", " + circuits + " circuit" + (circuits === 1 ? "" : "s") + " open" : "");
}

export default function AgentsStatusTile() {
  const rt = useRealtimeState();
  const schedulerRev = rt?.revisions?.scheduler ?? 0;
  const [reading, setReading] = useState<Reading | null>(null);
  const [failure, setFailure] = useState<Failure | null>(null);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    let live = true;
    const load = () =>
      api
        .schedulerStatus()
        .then((s: SchedulerStatus) => {
          if (!live) return;
          setReading({ status: s, at: Date.now() });
          setFailure(null);
        })
        .catch((e) => {
          if (!live) return;
          const code = statusOf(e);
          const kind: Failure["kind"] = code === 403 ? "forbidden" : code === 401 ? "signin" : "unreachable";
          // A 401/403 says nothing about the agents and everything about this viewer: drop any
          // earlier reading rather than let it age on the glass under a different identity.
          if (kind !== "unreachable") setReading(null);
          setFailure((prev) => (prev && prev.kind === kind ? prev : { kind, since: Date.now() }));
        });
    // A tick later, so a mount React undoes at once (its development double mount) asks nothing.
    const first = window.setTimeout(load, 0);
    const id = window.setInterval(load, POLL_MS);
    return () => {
      live = false;
      window.clearTimeout(first);
      window.clearInterval(id);
    };
  }, [schedulerRev]);

  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), TICK_MS);
    return () => window.clearInterval(id);
  }, []);

  // ---- we cannot see: grey, worded, never OFFLINE ------------------------------------
  if (failure?.kind === "forbidden") {
    return (
      <div className="wb-alarms">
        <span className="wb-alarm-chip grey">
          AGENTS STATUS · NOT AVAILABLE TO YOUR ROLE · scheduler status is limited to platform readers
          (a permission, not an outage)
        </span>
      </div>
    );
  }
  if (failure?.kind === "signin") {
    return (
      <div className="wb-alarms">
        <span className="wb-alarm-chip grey">AGENTS STATUS · SIGN IN to see scheduler status</span>
      </div>
    );
  }

  const fresh = reading != null && now - reading.at <= READING_FRESH_MS;
  if (failure?.kind === "unreachable" || (reading != null && !fresh)) {
    const since = failure?.since ?? reading?.at ?? now;
    return (
      <div className="wb-alarms">
        <span className="wb-alarm-chip grey">
          AGENTS STATUS UNKNOWN · no answer from scheduler status since {fmtTime(since)} EAT (
          {fmtAge((now - since) / 1000)})
          {reading
            ? " · last reading " + fmtTime(reading.at) + " EAT said " + summarise(reading.status)
            : ""}
        </span>
      </div>
    );
  }
  if (!reading) return null; // first load still in flight

  // ---- a fresh, successful reading: the only thing allowed to say OFFLINE --------------
  const status = reading.status;
  const circuitChips = (status.jobs || [])
    .filter((j) => j.circuit_open)
    .map((j) => (
      <span key={j.name} className="wb-alarm-chip red" role="alert">
        <span aria-hidden="true">✕ </span>CIRCUIT OPEN · {j.name} · {j.consecutive_failures} failures in a row
        {j.last_started_at ? " · last run " + fmtDateTime(j.last_started_at) + " EAT" : ""}
      </span>
    ));

  if (!status.enabled) {
    return (
      <div className="wb-alarms">
        {/* A deliberate setting, not an outage: said in the floor's words, without the flag name. */}
        <span className="wb-alarm-chip grey">Unattended jobs are off in this demo</span>
        {circuitChips}
      </div>
    );
  }

  const leaseAt = parseInstant(status.lease_expires_at);
  const tickAge = status.seconds_since_tick;
  // Absolute instant of the last tick = when we read it minus how old the server said it was.
  const lastTickAt = tickAge == null ? null : new Date(reading.at - tickAge * 1000);
  const shownAge = tickAge == null ? null : tickAge + (now - reading.at) / 1000;

  return (
    <div className="wb-alarms">
      {isOffline(status) ? (
        <div className="wb-offline" role="alert">
          <div className="wb-offline-word">
            <span aria-hidden="true">✕ </span>AGENTS OFFLINE
          </div>
          <div className="wb-offline-detail">
            {shownAge == null
              ? "The scheduler has never renewed its lease — no scheduled agent is running."
              : "No scheduler tick for " + fmtAge(shownAge) + " — last tick " + fmtTime(lastTickAt) + " EAT."}
            {leaseAt
              ? (leaseAt.getTime() < now ? " Lease expired " : " Lease expires ") + fmtDateTime(leaseAt) + " EAT."
              : ""}
          </div>
          <div className="wb-offline-do">
            Check <code>/api/v1/scheduler/status</code> for a fresh tick; a restarted process takes the
            lease over within 30 s. RUNBOOK: &ldquo;AGENTS OFFLINE&rdquo;.
          </div>
        </div>
      ) : (
        <span className="wb-alarm-chip ok">
          AGENTS ONLINE · last tick {fmtTime(lastTickAt)} EAT ({fmtAge(shownAge ?? 0)} ago)
        </span>
      )}
      {circuitChips}
    </div>
  );
}
