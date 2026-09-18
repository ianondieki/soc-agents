"""Ledger renderer (§6.2, §6.7): the shift-ledger row for one alert.

Two outputs, one source of truth:

* ``render_ledger_row(alert, cfg)`` — the §6.7 ``ShiftLedgerRow`` dict::

      {shift_id: "2026-09-16_DAY", incident_number, priority, site_id, region_code,
       failure_domain, users_affected, assignee_name, status, opened_at_eat: "13:47",
       note: "HITL pending" | "auto-sent per policy" | "approved by <who>"}

  §6.2: every column non-null, and the EAT date and the shift come from **one clock read** —
  here ``alert.sent``, the read ``build_alert`` already made, converted to the operator's zone.
  Nothing here calls ``now()``, so the row is reproducible from the stored envelope.

* ``render_ledger_cells(alert, cfg, shift_type)`` — the xlsx cells, produced by
  ``services/ledger.py:ledger_row_cells`` itself through a duck-typed view of the envelope
  (``_IncidentView``), so the column order and formatting of the Excel ledger stay defined in
  exactly one place. Columns the envelope deliberately does not carry — FE and RNIO names
  (persons; only ``assignee_name`` travels, §6.1), vendor TT ref, escalation and expected-
  resolution times, site class — render empty, and the ``Status`` column carries the CAP
  ``lifecycle`` rather than the incident status the envelope does not have.

Both are pure: no session, no file, no clock.
"""

from __future__ import annotations

from types import SimpleNamespace
from zoneinfo import ZoneInfo

from noc_agents.config import OperatorConfig
from noc_agents.domain.alerts import NocAlert
from noc_agents.services.ledger import ledger_row_cells
from noc_agents.services.shifts import current_shift

__all__ = ["LEDGER_KEYS", "ledger_note", "render_ledger_cells", "render_ledger_row"]

LEDGER_KEYS: tuple[str, ...] = (
    "shift_id",
    "incident_number",
    "priority",
    "site_id",
    "region_code",
    "failure_domain",
    "users_affected",
    "assignee_name",
    "status",
    "opened_at_eat",
    "note",
)


def ledger_note(alert: NocAlert) -> str:
    """§6.7: ``"HITL pending"`` while unapproved, ``"auto-sent per policy"`` for a policy approver."""
    approver = alert.governance.approved_by
    if approver is None:
        return "HITL pending" if alert.governance.requires_hitl else "unapproved"
    if approver.startswith("policy:"):
        return "auto-sent per policy"
    return f"approved by {approver}"


def render_ledger_row(alert: NocAlert, cfg: OperatorConfig) -> dict:
    """The §6.7 ShiftLedgerRow dict, every value non-null, from the envelope's own clock read."""
    when = alert.sent.astimezone(ZoneInfo(cfg.timezone))
    shift = current_shift(cfg, when).upper()
    return {
        "shift_id": f"{when:%Y-%m-%d}_{shift}",
        "incident_number": alert.incident.incident_number,
        "priority": alert.classification.priority,
        "site_id": alert.area.site_id,
        "region_code": alert.area.region_code,
        "failure_domain": alert.facts.failure_domain,
        "users_affected": alert.facts.users_affected,
        "assignee_name": alert.facts.assignee_name or "",
        "status": alert.classification.lifecycle,
        "opened_at_eat": f"{when:%H:%M}",
        "note": ledger_note(alert),
    }


def _incident_view(alert: NocAlert) -> SimpleNamespace:
    """The ``IncidentRow`` attributes ``ledger_row_cells`` reads, filled from the envelope."""
    return SimpleNamespace(
        incident_number=alert.incident.incident_number,
        priority=alert.classification.priority,
        site_id=alert.area.site_id,
        site_name=alert.area.site_name,
        site_type=alert.area.site_type,
        site_class="",
        region_code=alert.area.region_code,
        tt_category=alert.facts.tt_category,
        failure_domain=alert.facts.failure_domain,
        users_affected=alert.facts.users_affected,
        responsible_msp=None,
        msp_name=alert.facts.msp_code or "",
        fe_name="",  # a person's name; the envelope carries role tokens only (§6.1)
        rnio_name="",
        escalated_at="",
        expected_resolution_at="",
        status=alert.classification.lifecycle,
        vendor_tt_ref="",
        mpesa_risk=alert.facts.mpesa_risk,
    )


def render_ledger_cells(alert: NocAlert, cfg: OperatorConfig, shift_type: str) -> tuple[str, list]:
    """``(file name, cells)`` exactly as ``services/ledger.py:ledger_row_cells`` lays them out."""
    return ledger_row_cells(_incident_view(alert), cfg, shift_type)  # type: ignore[arg-type]  # duck-typed view
