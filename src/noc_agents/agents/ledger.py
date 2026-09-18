"""LEDGER: mirror the incident in the DB shift ledger and queue the Excel append.

The ``ShiftLedgerRow`` is written inside the transaction, as before. The xlsx append is an
``EXCEL_ROW`` outbox row (spec §5.3.9): the dispatcher appends it under a file lock after
commit and treats a locked workbook (PermissionError, an OSError) as transient, retrying
it on later drains. The node therefore no longer touches the file system, and a workbook
open in Excel can no longer affect the run at all.

The outbox row is queued before the DB row is added, so a failure leaves nothing
half-done in the session.
"""

from __future__ import annotations

from noc_agents.db.models import ShiftLedgerRow
from noc_agents.orchestrator.contract import IncidentState, RunContext, StepResult
from noc_agents.orchestrator.outbox import EXCEL_ROW, enqueue
from noc_agents.services.ledger import ledger_row_cells
from noc_agents.services.shifts import current_shift, shift_id


def input_summary(state: IncidentState, ctx: RunContext) -> str:
    return "excel"


def run(state: IncidentState, ctx: RunContext) -> StepResult:
    cfg, inc, session = ctx.cfg, state.incident, ctx.session
    st = current_shift(cfg)
    sid = shift_id(cfg)
    file_name, cells = ledger_row_cells(inc, cfg, st)  # pure render: no file I/O in the node
    enqueue(
        session,
        kind=EXCEL_ROW,
        idempotency_key=f"EXCEL_ROW:{inc.id}:{sid}",
        payload={"operator_id": cfg.operator_id, "incident_number": inc.incident_number, "file": file_name, "cells": cells},
        incident_id=inc.id,
        run_id=ctx.run.id,
        operator_id=cfg.operator_id,
    )
    session.add(
        ShiftLedgerRow(
            operator_id=cfg.operator_id,
            shift_id=sid,
            shift_type=st.upper(),
            incident_number=inc.incident_number,
            priority=inc.priority,
            site=f"{inc.site_id} {inc.site_name}",
            site_type=inc.site_type,
            region_code=inc.region_code,
            owner=inc.assignee_name or "",
            status=inc.status,
            mpesa_risk=inc.mpesa_risk,
        )
    )
    return StepResult(
        output_summary=file_name,
        rationale="Shift failure ledger row appended for supervisor scan",
        tools=[{"name": "outbox.enqueue", "ok": True, "latency_ms": 2, "error": None}],
    )
