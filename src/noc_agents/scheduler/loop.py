"""The scheduler loop with a database lease (spec §4.4, §7.0.3).

One ``asyncio.Task`` per process, started in the FastAPI lifespan when ``SCHEDULER_ENABLED``
is true (default false: nothing starts, the suite is untouched). Every ``SCHEDULER_TICK_SECONDS``
(default 5) the task runs :meth:`Scheduler.tick` in a worker thread:

1. **Lease.** ``UPDATE scheduler_lease SET owner=:me, expires_at=:now+30s, renewed_at=:now
   WHERE name='main' AND (owner=:me OR expires_at < :now)``. The row is created once with an
   expiry in 1970 (insert-or-ignore), so the first process to run the UPDATE wins it and every
   other process affects 0 rows and does nothing that tick. The write is one atomic statement
   on the database's own lock: there is no read-then-write window in which two processes can
   both believe they hold it, which is what makes ``uvicorn --workers 2``, or two ``--reload``
   processes overlapping, yield exactly one ticker.
2. **Takeover.** A holder renews every tick, so its ``expires_at`` is always at most 30 s out.
   A process that dies (crash, ``kill``, or the Windows reloader's hard ``TerminateProcess``,
   which runs no lifespan shutdown) stops renewing and its lease expires within 30 s; the next
   UPDATE from any survivor or restarted process then matches ``expires_at < :now`` and takes
   it. A process that shuts down cleanly (``Makefile:14`` / ``scripts/start_demo.ps1:12`` run
   ``--reload``, whose POSIX path is a SIGTERM that does run lifespan shutdown) *releases* the
   lease by setting ``expires_at = now``, so the restarted process takes over on its first tick
   instead of waiting the TTL. Nothing ever waits on a lock or a dead owner: no deadlock.
3. **Jobs.** Only the holder runs jobs. A job is due when ``scheduled_job_state.last_started_at``
   is older than its interval (or absent); that state lives in the database, so a restarted
   process continues the cadence instead of re-running everything at once. The lease is renewed
   again before each job. Each execution is :func:`run_job`: a fresh session, an ``agent_runs``
   row with ``graph_name=<card.graph_name>`` and ``trigger="SCHEDULE"``, one step recorded by
   :class:`RunTracker`. A job that raises is rolled back and recorded as a FAILED run with a
   FAILED step; ``consecutive_failures`` is incremented and at ``>= 3`` the circuit opens
   (``circuit_open=1``, the loop skips the job, ``scheduler.job_failed`` is published).
   ``POST /api/v1/scheduler/run/{job}`` runs a job on demand and resets its circuit first.

No APScheduler, Celery or RQ: the lease *is* the coordination, and it needs nothing but the
existing database (spec §4.4).
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable
from uuid import uuid4

from sqlalchemy import insert, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from noc_agents.config import AppSettings, get_settings
from noc_agents.db.models import (
    AgentRunRow,
    AgentRunStepRow,
    ScheduledJobStateRow,
    SchedulerLeaseRow,
    get_session,
    new_id,
    utcnow,
)
from noc_agents.graph.instrumentation import RunTracker, timed_ms
from noc_agents.orchestrator.contract import FAILED, SUCCEEDED
from noc_agents.orchestrator.outbox import drain_once
from noc_agents.realtime.hub import RealtimeEvent, hub
from noc_agents.scheduler import JobCard, JobResult
from noc_agents.services.clock import z_utc
from noc_agents.services.worklog_monitor import tick_job

log = logging.getLogger("noc_agents.scheduler")

LEASE_NAME = "main"
LEASE_TTL = timedelta(seconds=30)  # spec §4.4: TTL 30 s, tick 5 s
DEFAULT_TICK_SECONDS = 5.0
CIRCUIT_THRESHOLD = 3
TRIGGER = "SCHEDULE"
_EPOCH = datetime(1970, 1, 1)  # "never held": strictly older than any real ``now``


# --------------------------------------------------------------------------------- environment

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    return default


def scheduler_enabled() -> bool:
    """``SCHEDULER_ENABLED`` — default **false**: nothing starts unless someone asks."""
    return _env_bool("SCHEDULER_ENABLED", False)


def job_enabled(card: JobCard) -> bool:
    """The job's own flag (``card.enabled_env``); ``SCHEDULER_ENABLED`` gates all.

    An unset flag falls back to ``card.default_enabled`` rather than to True, so a
    feature lane the spec ships OFF (weather) reports itself off instead of claiming
    to be enabled while its own poller quietly declines to run.
    """
    return _env_bool(card.enabled_env, card.default_enabled)


def tick_seconds() -> float:
    raw = (os.getenv("SCHEDULER_TICK_SECONDS") or "").strip()
    try:
        value = float(raw) if raw else DEFAULT_TICK_SECONDS
    except ValueError:
        value = DEFAULT_TICK_SECONDS
    return max(0.1, value)


def default_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:6]}"


# ---------------------------------------------------------------------------------------- jobs


def outbox_dispatch_job(session: Session, settings: AppSettings) -> JobResult:
    """``outbox_dispatch``: one :func:`drain_once` pass. Reported under ``graph_name="outbox"``."""
    report = drain_once(session)
    return JobResult(
        summary=str(report),
        rationale="Transactional outbox drained after commit; transient failures back off, stale claims reclaimed",
        tools=(
            {
                "name": "outbox.drain_once",
                "ok": report.errors == 0,
                "claimed": report.claimed,
                "sent": report.sent,
                "retried": report.retried,
                "failed": report.failed,
                "dead": report.dead,
                "rejected": report.rejected,
                "reclaimed": report.reclaimed,
                "errors": report.errors,
            },
        ),
    )


def _weather_job() -> JobCard:
    """The Phase 3 weather poller's card, imported lazily to avoid an import cycle."""
    from noc_agents.pollers.weather import WEATHER_JOB

    return WEATHER_JOB


SCHEDULED_JOBS: tuple[JobCard, ...] = (
    JobCard("outbox_dispatch", 5, outbox_dispatch_job, "OUTBOX_DISPATCH_ENABLED", "BroadcastCommsAgent", "outbox", max_seconds=30),
    JobCard("monitor_tick", 60, tick_job, "SCHEDULER_MONITOR_ENABLED", "WorklogMonitorAgent", "monitor"),
    # Phase 3 weather lane (§7.3). Gated by WEATHER_ENABLED, which defaults OFF — the card
    # carries default_enabled=False so an unset flag reads as off in /scheduler/status too,
    # and the poller re-checks the flag itself. Imported lazily: pollers.weather imports the
    # scheduler's JobCard, so a top-level import either way round is a cycle.
    _weather_job(),
)


def job_card(name: str, jobs: tuple[JobCard, ...] = SCHEDULED_JOBS) -> JobCard | None:
    for card in jobs:
        if card.name == name:
            return card
    return None


# --------------------------------------------------------------------------------------- lease


def _insert_or_ignore(session: Session, model: type, values: dict[str, Any], pk: str) -> None:
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        session.execute(sqlite_insert(model).values(**values).on_conflict_do_nothing(index_elements=[pk]))
    elif dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        session.execute(pg_insert(model).values(**values).on_conflict_do_nothing(index_elements=[pk]))
    else:
        try:
            with session.begin_nested():
                session.execute(insert(model).values(**values))
        except IntegrityError:
            pass


def acquire_lease(session: Session, owner: str, now: datetime | None = None, ttl: timedelta = LEASE_TTL) -> bool:
    """Take or renew the ``main`` lease for ``owner``. True on exactly one row affected. Commits.

    The single conditional UPDATE is the whole algorithm: a row whose lease is live and held
    by someone else matches neither branch of the WHERE, so the caller affects 0 rows and
    stays a bystander. ``now`` is injectable so tests can prove the takeover without sleeping.
    """
    now = now or utcnow()
    _insert_or_ignore(
        session,
        SchedulerLeaseRow,
        {"name": LEASE_NAME, "owner": "", "expires_at": _EPOCH, "renewed_at": _EPOCH},
        "name",
    )
    result = session.execute(
        update(SchedulerLeaseRow)
        .where(
            SchedulerLeaseRow.name == LEASE_NAME,
            or_(SchedulerLeaseRow.owner == owner, SchedulerLeaseRow.expires_at < now),
        )
        .values(owner=owner, expires_at=now + ttl, renewed_at=now)
        .execution_options(synchronize_session=False)
    )
    session.commit()
    return result.rowcount == 1


def release_lease(session: Session, owner: str, now: datetime | None = None) -> bool:
    """Hand the lease back on clean shutdown: expire it now, so a restart need not wait the TTL."""
    now = now or utcnow()
    result = session.execute(
        update(SchedulerLeaseRow)
        .where(SchedulerLeaseRow.name == LEASE_NAME, SchedulerLeaseRow.owner == owner)
        .values(expires_at=now)
        .execution_options(synchronize_session=False)
    )
    session.commit()
    return result.rowcount == 1


def read_lease(session: Session) -> SchedulerLeaseRow | None:
    return session.get(SchedulerLeaseRow, LEASE_NAME)


# ----------------------------------------------------------------------------------- job state


def _ensure_state(session: Session, name: str) -> ScheduledJobStateRow:
    _insert_or_ignore(session, ScheduledJobStateRow, {"name": name, "consecutive_failures": 0, "circuit_open": 0}, "name")
    state = session.get(ScheduledJobStateRow, name)
    assert state is not None  # just inserted or already there
    return state


def read_state(session: Session, name: str) -> ScheduledJobStateRow | None:
    return session.get(ScheduledJobStateRow, name)


# ------------------------------------------------------------------------------------ run_job


@dataclass(frozen=True)
class JobOutcome:
    job: str
    run_id: str
    status: str  # SUCCEEDED | FAILED
    summary: str
    error: str | None
    duration_ms: int
    consecutive_failures: int
    circuit_open: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "job": self.job,
            "run_id": self.run_id,
            "status": self.status,
            "summary": self.summary,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "consecutive_failures": self.consecutive_failures,
            "circuit_open": self.circuit_open,
        }


def _error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:2000]


def _input_summary(card: JobCard) -> str:
    return f"trigger={TRIGGER} interval={card.interval_s}s"


def run_job(
    card: JobCard,
    settings: AppSettings | None = None,
    *,
    reset_circuit: bool = False,
    session_factory: Callable[[], Session] = get_session,
) -> JobOutcome:
    """Execute one job as a run: fresh session, ``agent_runs(graph_name, trigger="SCHEDULE")``,
    one tracked step. Never raises — a raising job becomes a FAILED run row and a failure count.

    ``reset_circuit`` (the on-demand route) zeroes ``consecutive_failures`` / ``circuit_open``
    before running, so a job whose circuit opened can be tried again by hand.
    """
    settings = settings or get_settings()
    operator_id = settings.operator.operator_id
    session = session_factory()
    started = utcnow()
    run_id = new_id()
    t0 = time.perf_counter()
    try:
        try:
            state = _ensure_state(session, card.name)
            if reset_circuit:
                state.consecutive_failures = 0
                state.circuit_open = 0
            state.last_started_at = started
            state.last_status = "RUNNING"
            state.last_error = None
            run = AgentRunRow(
                id=run_id,
                incident_id=None,
                operator_id=operator_id,
                graph_name=card.graph_name,
                trigger=TRIGGER,
                status="RUNNING",
                started_at=started,
            )
            session.add(run)
            session.flush()
            tracker = RunTracker(session, run)
            step = tracker.start_step(card.name, card.agent, _input_summary(card))

            result = card.fn(session, settings)

            elapsed_ms = timed_ms(t0)
            over_budget = elapsed_ms > card.max_seconds * 1000
            status = FAILED if over_budget else SUCCEEDED
            budget_error = (
                f"budget exceeded: {elapsed_ms / 1000:.1f}s > max_seconds={card.max_seconds}s" if over_budget else None
            )
            tracker.complete_step(
                step,
                status=status,
                output_summary=result.summary,
                rationale=budget_error or result.rationale,
                tools=list(result.tools),
                confidence=None,
            )
            tracker.finish_run(status, error=budget_error)
            # Re-fetch: the job may have committed (expiring the instance) or rolled back.
            state = _ensure_state(session, card.name)
            state.last_started_at = started
            state.last_finished_at = utcnow()
            state.last_status = status
            state.last_error = budget_error
            if not over_budget:  # an overshoot is visible, not a raise: it does not feed the circuit
                state.consecutive_failures = 0
                state.circuit_open = 0
            failures, circuit = int(state.consecutive_failures or 0), bool(state.circuit_open)
            session.commit()
            return JobOutcome(card.name, run_id, status, result.summary, budget_error, elapsed_ms, failures, circuit)
        except Exception as exc:  # noqa: BLE001 — the job raised: record it, never propagate
            err = _error_text(exc)
            log.exception("scheduler: job %s failed", card.name)
            session.rollback()
            failures, circuit = _record_failure(session, card, run_id, operator_id, started, err, reset_circuit)
            hub.publish_sync(
                RealtimeEvent(
                    type="agent.run.finished",
                    operator_id=operator_id,
                    incident_id=None,
                    run_id=run_id,
                    payload={
                        "seq": 1,
                        "incident_number": None,
                        "run_id": run_id,
                        "status": FAILED,
                        "error": err,
                        "node": card.name,
                    },
                )
            )
            if circuit:
                hub.publish_sync(
                    RealtimeEvent(
                        type="scheduler.job_failed",
                        operator_id=operator_id,
                        run_id=run_id,
                        payload={
                            "job": card.name,
                            "run_id": run_id,
                            "error": err,
                            "consecutive_failures": failures,
                            "circuit_open": True,
                        },
                    )
                )
            return JobOutcome(card.name, run_id, FAILED, "", err, timed_ms(t0), failures, circuit)
    finally:
        session.close()


def _record_failure(
    session: Session,
    card: JobCard,
    run_id: str,
    operator_id: str,
    started: datetime,
    err: str,
    reset_circuit: bool,
) -> tuple[int, bool]:
    """Fresh transaction after the rollback: a FAILED run row + its FAILED step, and the job
    state bumped. The run/step rows may already exist when the job committed before raising
    (the monitor commits inside its pass), so this updates in place rather than inserting blind."""
    now = utcnow()
    try:
        run = session.get(AgentRunRow, run_id)
        if run is None:
            run = AgentRunRow(
                id=run_id,
                incident_id=None,
                operator_id=operator_id,
                graph_name=card.graph_name,
                trigger=TRIGGER,
                started_at=started,
            )
            session.add(run)
        run.status = FAILED
        run.finished_at = now
        run.current_node = card.name
        run.error_summary = err

        step = session.scalar(select(AgentRunStepRow).where(AgentRunStepRow.run_id == run_id, AgentRunStepRow.seq == 1))
        if step is None:
            step = AgentRunStepRow(
                id=new_id(),
                run_id=run_id,
                seq=1,
                node_name=card.name,
                agent_name=card.agent,
                started_at=started,
                input_summary=_input_summary(card),
                tools_called=[],
            )
            session.add(step)
        step.status = FAILED
        step.finished_at = now
        step.duration_ms = int((now - started).total_seconds() * 1000)
        step.output_summary = f"{card.name} failed"
        step.rationale = err
        step.confidence = None

        state = _ensure_state(session, card.name)
        failures = (0 if reset_circuit else int(state.consecutive_failures or 0)) + 1
        state.consecutive_failures = failures
        state.circuit_open = 1 if failures >= CIRCUIT_THRESHOLD else 0
        state.last_started_at = started
        state.last_finished_at = now
        state.last_status = FAILED
        state.last_error = err
        session.commit()
        return failures, bool(state.circuit_open)
    except Exception:  # noqa: BLE001 — the DB itself is broken: log, do not mask the job's error
        session.rollback()
        log.exception("scheduler: recording the failure of job %s failed", card.name)
        return 0, False


# ---------------------------------------------------------------------------------- the loop


@dataclass
class TickReport:
    owner: str
    held: bool
    ran: list[JobOutcome] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)  # job -> disabled | circuit_open | not_due
    lost_lease: bool = False


class Scheduler:
    """One process's ticker. The sync core (:meth:`tick`, :meth:`acquire`, :meth:`release`)
    is callable from tests without an event loop; :meth:`start` / :meth:`stop` wrap it in the
    lifespan task."""

    def __init__(
        self,
        jobs: tuple[JobCard, ...] = SCHEDULED_JOBS,
        *,
        owner: str | None = None,
        tick_s: float | None = None,
        ttl: timedelta = LEASE_TTL,
        stop_timeout_s: float = 10.0,
        session_factory: Callable[[], Session] = get_session,
        settings_factory: Callable[[], AppSettings] = get_settings,
    ) -> None:
        self.jobs = tuple(jobs)
        self.owner = owner or default_owner()
        self.tick_s = float(tick_s) if tick_s is not None else tick_seconds()
        self.ttl = ttl
        self.stop_timeout_s = stop_timeout_s
        self._session_factory = session_factory
        self._settings_factory = settings_factory
        self._task: asyncio.Task | None = None
        self._stop: asyncio.Event | None = None
        self.holding = False
        self.last_tick_at: datetime | None = None
        self.ticks = 0
        self.jobs_run = 0

    # --- sync core --------------------------------------------------------------------------

    def acquire(self, now: datetime | None = None) -> bool:
        session = self._session_factory()
        try:
            return acquire_lease(session, self.owner, now, self.ttl)
        finally:
            session.close()

    def release(self, now: datetime | None = None) -> bool:
        session = self._session_factory()
        try:
            return release_lease(session, self.owner, now)
        finally:
            session.close()

    def _due(self, card: JobCard, now: datetime) -> tuple[bool, str]:
        session = self._session_factory()
        try:
            state = read_state(session, card.name)
            if state is None:
                return True, ""
            if state.circuit_open:
                return False, "circuit_open"
            if state.last_started_at is not None and now - state.last_started_at < timedelta(seconds=card.interval_s):
                return False, "not_due"
            return True, ""
        finally:
            session.close()

    def tick(self, now: datetime | None = None) -> TickReport:
        """Take/renew the lease; when held, run every enabled, due, closed-circuit job."""
        now = now or utcnow()
        self.ticks += 1
        held = self.acquire(now)
        self.holding = held
        report = TickReport(owner=self.owner, held=held)
        if not held:
            return report
        self.last_tick_at = now
        settings = self._settings_factory()
        for card in self.jobs:
            if not job_enabled(card):
                report.skipped[card.name] = "disabled"
                continue
            due, why = self._due(card, now)
            if not due:
                report.skipped[card.name] = why
                continue
            if not self.acquire(now):  # renew before each job: a slow job must not outlive the lease unnoticed
                self.holding = False
                report.lost_lease = True
                log.warning("scheduler: lease lost mid-tick (owner=%s); remaining jobs skipped", self.owner)
                break
            outcome = run_job(card, settings, session_factory=self._session_factory)
            report.ran.append(outcome)
            self.jobs_run += 1
        return report

    # --- asyncio wrapper --------------------------------------------------------------------

    def start(self) -> asyncio.Task:
        """Create the ticker task on the running loop (call from the lifespan)."""
        loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        self._task = loop.create_task(self._run(), name="noc-scheduler")
        return self._task

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _run(self) -> None:
        assert self._stop is not None
        log.info(
            "scheduler: started owner=%s tick=%.1fs ttl=%ss jobs=%s",
            self.owner,
            self.tick_s,
            int(self.ttl.total_seconds()),
            [c.name for c in self.jobs],
        )
        while not self._stop.is_set():
            try:
                report = await asyncio.to_thread(self.tick)
                if report.ran:
                    log.info("scheduler: ran %s", [(o.job, o.status) for o in report.ran])
            except Exception:  # noqa: BLE001 — a broken tick must not kill the loop
                log.exception("scheduler: tick failed; retrying next tick")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.tick_s)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        """Stop ticking, wait (bounded) for the in-flight tick, then release the lease."""
        if self._stop is not None:
            self._stop.set()
        task, self._task = self._task, None
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=self.stop_timeout_s)
            except asyncio.TimeoutError:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        try:
            await asyncio.to_thread(self.release)
        except Exception:  # noqa: BLE001 — the DB may be gone at shutdown; the TTL covers it
            log.exception("scheduler: releasing the lease failed (it expires within %ss)", int(self.ttl.total_seconds()))
        self.holding = False
        log.info("scheduler: stopped owner=%s", self.owner)


# -------------------------------------------------------------------------------------- status


def status_payload(
    session: Session,
    jobs: tuple[JobCard, ...] = SCHEDULED_JOBS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """``GET /api/v1/scheduler/status`` (spec §7.0.3). Read from the database, so any process
    answers for the one that ticks: ``seconds_since_tick`` is the age of the lease's last renewal."""
    now = now or utcnow()
    lease = read_lease(session)
    states = {s.name: s for s in session.scalars(select(ScheduledJobStateRow)).all()}
    since = None
    if lease is not None and lease.renewed_at is not None and lease.renewed_at > _EPOCH:
        since = round((now - lease.renewed_at).total_seconds(), 1)
    out_jobs = []
    for card in jobs:
        st = states.get(card.name)
        out_jobs.append(
            {
                "name": card.name,
                "interval_s": card.interval_s,
                "enabled": job_enabled(card),
                "last_started_at": z_utc(st.last_started_at) if st else None,
                "last_status": st.last_status if st else None,
                "consecutive_failures": int(st.consecutive_failures or 0) if st else 0,
                "circuit_open": bool(st.circuit_open) if st else False,
            }
        )
    return {
        "enabled": scheduler_enabled(),
        "lease_owner": (lease.owner or None) if lease is not None else None,
        "lease_expires_at": z_utc(lease.expires_at) if lease is not None and lease.expires_at > _EPOCH else None,
        "seconds_since_tick": since,
        "jobs": out_jobs,
    }
