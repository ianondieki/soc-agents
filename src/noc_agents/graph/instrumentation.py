from __future__ import annotations

import json
import os
import time
from typing import Any

from sqlalchemy.orm import Session

from noc_agents.db.models import AgentRunRow, AgentRunStepRow, AuditRow, utcnow, new_id
from noc_agents.realtime.commit_hook import buffer_event
from noc_agents.realtime.hub import RealtimeEvent


def _live_delay() -> None:
    """Optional pause between agent steps so demos feel real (ms)."""
    try:
        ms = int(os.getenv("LIVE_AGENT_DELAY_MS", "0") or "0")
    except ValueError:
        ms = 0
    if ms > 0:
        time.sleep(min(ms, 2000) / 1000.0)


class RunTracker:
    """Writes the step/audit rows of one run and announces them.

    Every ``agent.*`` event is buffered on the session (``realtime.commit_hook``) and
    published only after that session commits, so the UI is never told about a step whose
    row a later rollback removes (spec §7.0.4).
    """

    def __init__(self, session: Session, run: AgentRunRow) -> None:
        self.session = session
        self.run = run
        self.seq = 0
        self.incident_number: str | None = None

    def bind_incident(self, incident_id: str, incident_number: str | None = None) -> None:
        self.run.incident_id = incident_id
        if incident_number:
            self.incident_number = incident_number

    def _payload(self, **extra: Any) -> dict[str, Any]:
        base = {
            "seq": self.seq,
            "incident_number": self.incident_number,
            "run_id": self.run.id,
        }
        base.update(extra)
        return base

    def start_step(
        self,
        node_name: str,
        agent_name: str,
        input_summary: str = "",
    ) -> AgentRunStepRow:
        _live_delay()
        self.seq += 1
        self.run.current_node = node_name
        self.run.status = "RUNNING"
        step = AgentRunStepRow(
            id=new_id(),
            run_id=self.run.id,
            seq=self.seq,
            node_name=node_name,
            agent_name=agent_name,
            status="STARTED",
            started_at=utcnow(),
            input_summary=input_summary,
            tools_called=[],
        )
        self.session.add(step)
        self.session.flush()
        buffer_event(
            self.session,
            RealtimeEvent(
                type="agent.step.started",
                operator_id=self.run.operator_id,
                incident_id=self.run.incident_id,
                run_id=self.run.id,
                payload=self._payload(node=node_name, agent=agent_name, input=input_summary[:120]),
            )
        )
        return step

    def complete_step(
        self,
        step: AgentRunStepRow,
        *,
        status: str = "SUCCEEDED",
        output_summary: str = "",
        rationale: str = "",
        tools: list[dict[str, Any]] | None = None,
        confidence: float | None = 0.9,
    ) -> None:
        step.status = status
        step.finished_at = utcnow()
        if step.started_at:
            step.duration_ms = int((step.finished_at - step.started_at).total_seconds() * 1000)
        step.output_summary = output_summary
        step.rationale = rationale
        if tools is not None:
            step.tools_called = tools
        step.confidence = confidence
        self.session.add(
            AuditRow(
                operator_id=self.run.operator_id,
                actor=step.agent_name,
                action=f"step.{status.lower()}",
                entity_type="incident",
                entity_id=self.run.incident_id or "",
                rationale=rationale,
                # JSON, so GET /api/v1/audit can lift ``node`` and ``run_id`` (the audit trail groups a
                # run's intake steps with the ticket they opened). ``node`` and ``run_id`` come first
                # and the output is cut BEFORE encoding, so the 2000-char cap never truncates the
                # JSON in practice; the reader tolerates it anyway. Rows written before this change
                # hold a Python repr and are read as such.
                payload_json=json.dumps(
                    {"node": step.node_name, "run_id": self.run.id, "output": (output_summary or "")[:1200]},
                    ensure_ascii=False,
                )[:2000],
            )
        )
        self.session.flush()
        buffer_event(
            self.session,
            RealtimeEvent(
                type="agent.step.completed",
                operator_id=self.run.operator_id,
                incident_id=self.run.incident_id,
                run_id=self.run.id,
                payload=self._payload(
                    node=step.node_name,
                    agent=step.agent_name,
                    status=status,
                    rationale=rationale,
                    output=output_summary[:160],
                    duration_ms=step.duration_ms,
                ),
            )
        )

    def finish_run(self, status: str = "SUCCEEDED", error: str | None = None) -> None:
        self.run.status = status
        self.run.finished_at = utcnow()
        self.run.error_summary = error
        if status != "WAITING_HITL":
            self.run.current_node = None if status == "SUCCEEDED" else self.run.current_node
        self.session.flush()
        buffer_event(
            self.session,
            RealtimeEvent(
                type="agent.run.finished",
                operator_id=self.run.operator_id,
                incident_id=self.run.incident_id,
                run_id=self.run.id,
                payload=self._payload(status=status, error=error),
            )
        )


def timed_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)
