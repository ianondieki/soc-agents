"""Timezone helper — spec §7.0.6, defect #41.

Kenya runs on EAT (UTC+3, no DST, ever). The DB keeps naive UTC; the helper converts
on the way out only, and the serializers label what they emit so a browser cannot read
a naive string as local time.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from noc_agents.api.serializers import incident_out, run_out, step_out
from noc_agents.services.clock import (
    DEFAULT_TIMEZONE,
    eat_date,
    eat_tz,
    fmt_eat,
    to_eat,
    to_utc,
    utcnow,
    z_utc,
)

# The reference case from the defect report: the 09:00 UTC storm alarm is 12:00 in Nairobi.
STORM_UTC = datetime(2026, 9, 16, 9, 0, 0)


# ------------------------------------------------------------------ conversion


def test_to_eat_adds_the_three_hour_offset():
    eat = to_eat(STORM_UTC)
    assert (eat.hour, eat.minute) == (12, 0)
    assert eat.utcoffset() == timedelta(hours=3)
    assert eat.tzname() == "EAT"


def test_fmt_eat_renders_what_an_operator_reads():
    assert fmt_eat(STORM_UTC) == "12:00 EAT"
    assert fmt_eat(STORM_UTC, "%Y-%m-%d %H:%M") == "2026-09-16 12:00 EAT"


def test_eat_date_rolls_over_because_22_00_utc_is_already_tomorrow_in_nairobi():
    assert eat_date(datetime(2026, 9, 16, 22, 0)) == date(2026, 9, 17)
    assert eat_date(STORM_UTC) == date(2026, 9, 16)


def test_an_aware_input_is_converted_not_relabelled():
    """A tz-aware value must be honoured, never stamped over: 09:00Z and 12:00+03:00 are one instant."""
    aware = datetime(2026, 9, 16, 9, 0, tzinfo=timezone.utc)
    assert to_eat(aware) == to_eat(STORM_UTC)
    already_eat = to_eat(STORM_UTC)
    assert fmt_eat(already_eat) == "12:00 EAT"


def test_eat_has_no_dst_so_midwinter_and_midsummer_agree():
    """Guards against a future refactor reaching for a fixed UTC+3 *or* a DST-bearing zone."""
    for month in (1, 4, 7, 10):
        assert to_eat(datetime(2026, month, 15, 9, 0)).hour == 12


def test_none_passes_through_so_a_missing_timestamp_never_crashes_a_render():
    assert to_eat(None) is None and to_utc(None) is None and eat_date(None) is None
    assert fmt_eat(None) == ""


# ------------------------------------------------------------------- storage


def test_utcnow_keeps_the_naive_utc_storage_contract():
    """The DB contract is naive UTC — utcnow() must stay a drop-in for db.models.utcnow()."""
    from noc_agents.db.models import utcnow as db_utcnow

    now = utcnow()
    assert now.tzinfo is None
    assert abs(now - db_utcnow()) < timedelta(seconds=5)


def test_to_utc_assumes_a_naive_value_is_utc_which_is_the_db_contract():
    assert to_utc(STORM_UTC) == datetime(2026, 9, 16, 9, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------- zone


def test_zone_comes_from_the_operator_profile():
    from noc_agents.config import get_settings

    assert eat_tz().key == get_settings().operator.timezone == DEFAULT_TIMEZONE


def test_an_unresolvable_zone_falls_back_instead_of_crashing_a_render(monkeypatch):
    monkeypatch.setattr("noc_agents.services.clock._tz_name", lambda: "Mars/Olympus_Mons")
    assert eat_tz().key == DEFAULT_TIMEZONE
    assert fmt_eat(STORM_UTC) == "12:00 EAT"


# ---------------------------------------------------------------- serializing


def test_z_utc_labels_a_stored_timestamp_without_moving_it():
    """defect #41: the instant is unchanged, only made unambiguous."""
    z = z_utc(STORM_UTC)
    assert z.isoformat() == "2026-09-16T09:00:00Z"
    assert z == to_utc(STORM_UTC)  # same instant, still a real datetime
    assert z_utc(None) is None


def _incident_row(**over):
    row = SimpleNamespace(
        id="i1",
        operator_id="safaricom",
        incident_number="INC000123",
        status="ASSIGNED",
        priority="P2",
        users_affected=450000,
        service_affecting=True,
        services_impacted=["DATA"],
        site_id="SFC-NBI-001",
        site_name="Embakasi",
        site_type="HUB",
        region_code="NBI_E",
        county="Nairobi",
        title="t",
        description="d",
        narrative="n",
        root_cause_hypothesis="r",
        impact_summary="i",
        assignee_type="MSP",
        assignee_name="EGYPRO",
        msp_name="EGYPRO",
        fe_name=None,
        access_notes="",
        created_at=STORM_UTC,
        updated_at=STORM_UTC,
        is_hub_major=True,
        recurrence_count=0,
        problem_id=None,
        mpesa_risk=True,
        autonomy_level_applied="L2_GUARDED",
        requires_hitl=True,
        hitl_state="PENDING",
        failure_domain="POWER",
        correlation_fingerprint="fp",
        closed_at=None,
        sla_ack_due=STORM_UTC,
        sla_restore_due=STORM_UTC,
    )
    for k, v in over.items():
        setattr(row, k, v)
    return row


def test_incident_serializer_emits_z_on_every_timestamp():
    out = incident_out(_incident_row()).model_dump()
    stamped = {k: v for k, v in out.items() if isinstance(v, datetime)}
    assert stamped, "expected at least one timestamp on the incident payload"
    for field, value in stamped.items():
        assert value.isoformat().endswith("Z"), f"{field} would be read as local time"
    assert out["created_at"].isoformat() == "2026-09-16T09:00:00Z"
    assert out["closed_at"] is None  # a missing timestamp stays null, not ""


def test_run_and_step_serializers_emit_z():
    step = SimpleNamespace(
        id="s1",
        seq=1,
        node_name="CORRELATE",
        agent_name="IngestCorrelationAgent",
        status="SUCCEEDED",
        started_at=STORM_UTC,
        finished_at=STORM_UTC + timedelta(seconds=2),
        duration_ms=2000,
        input_summary="",
        output_summary="",
        rationale="",
        tools_called=[],
        confidence=None,
    )
    run = SimpleNamespace(
        id="r1",
        incident_id="i1",
        operator_id="safaricom",
        graph_name="incident_lifecycle",
        trigger="API",
        status="SUCCEEDED",
        started_at=STORM_UTC,
        finished_at=None,
        current_node="MONITOR",
        error_summary=None,
        steps=[step],
    )

    s = step_out(step).model_dump()
    assert s["started_at"].isoformat() == "2026-09-16T09:00:00Z"
    assert s["finished_at"].isoformat() == "2026-09-16T09:00:02Z"

    r = run_out(run).model_dump()
    assert r["started_at"].isoformat() == "2026-09-16T09:00:00Z"
    assert r["finished_at"] is None
    assert r["steps"][0]["started_at"].isoformat().endswith("Z")  # nested steps too


@pytest.mark.parametrize("mode", ["python", "json"])
def test_the_z_survives_both_dump_modes_so_the_wire_format_is_unambiguous(mode):
    """The routes dump in python mode and FastAPI encodes after; json mode is the direct path.

    Both must keep the suffix, or the defect returns silently for one of them.
    """
    out = incident_out(_incident_row()).model_dump(mode=mode)
    value = out["created_at"]
    rendered = value if isinstance(value, str) else value.isoformat()
    assert rendered == "2026-09-16T09:00:00Z"


def test_the_stored_value_is_still_naive_utc_the_serializer_only_labels_it():
    """The DB contract is not negotiable: serializing must not write EAT back into a row."""
    row = _incident_row()
    incident_out(row)
    assert row.created_at is STORM_UTC and row.created_at.tzinfo is None


def test_z_and_eat_describe_the_same_instant():
    """The end-to-end defect #41 statement: 09:00Z on the wire is 12:00 EAT on screen."""
    row = _incident_row()
    wire = incident_out(row).model_dump()["created_at"]
    assert wire.isoformat() == "2026-09-16T09:00:00Z"
    assert fmt_eat(row.created_at) == "12:00 EAT"
