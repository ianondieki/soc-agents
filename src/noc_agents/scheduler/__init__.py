"""Background scheduler (spec §4.4 / §7.0.3).

This package holds the two value types a job module needs — :class:`JobCard` and
:class:`JobResult` — and nothing else, so ``services.worklog_monitor`` can declare its job
without importing the loop (the loop imports the monitor; the other direction would be a
cycle). The loop, the database lease, the job runner and the HTTP status payload live in
:mod:`noc_agents.scheduler.loop`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from noc_agents.orchestrator.contract import FAIL_SOFT

if TYPE_CHECKING:  # typing only: keeps this module free of the config/session imports
    from sqlalchemy.orm import Session

    from noc_agents.config import AppSettings

__all__ = ["JobCard", "JobResult"]


@dataclass(frozen=True)
class JobResult:
    """What one job execution reports into its step row (``output_summary`` / ``rationale`` /
    ``tools_called``). A job that has nothing to say returns ``JobResult()``."""

    summary: str = ""
    rationale: str = ""
    tools: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class JobCard:
    """One scheduled job (spec §4.4).

    ``fn`` receives a fresh session and the current settings and returns a :class:`JobResult`;
    it may commit (the monitor does), and it may raise — the runner records a FAILED run and
    counts the failure toward the job's circuit breaker. ``enabled_env`` is the per-job flag
    (default per ``default_enabled``); ``SCHEDULER_ENABLED`` gates the loop as a whole. ``graph_name`` is what the
    job's ``agent_runs`` rows carry (``"outbox"``, ``"monitor"`` …) with ``trigger="SCHEDULE"``.
    ``max_seconds`` is the per-run budget: a run that overshoots it is recorded FAILED (its
    step keeps the job's own summary) so the overshoot is visible, but it is not a raise and
    does not count toward the circuit.
    """

    name: str
    interval_s: int
    fn: Callable[["Session", "AppSettings"], JobResult]
    enabled_env: str
    agent: str
    graph_name: str
    criticality: str = FAIL_SOFT
    max_seconds: int = 60
    #: What ``enabled_env`` means when it is UNSET. True for the jobs that should run
    #: whenever the scheduler does (outbox drain, monitor chase); False for feature lanes
    #: the spec ships OFF, so /scheduler/status never reports a job as enabled when its
    #: feature flag is absent -- an operator reading 'enabled' and seeing no data is a
    #: worse failure than a job that plainly says it is off.
    default_enabled: bool = True
