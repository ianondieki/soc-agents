from datetime import timedelta

from noc_agents.db.models import IncidentRow, WorkNoteRow, utcnow
from noc_agents.domain.schemas import EventIngest
from noc_agents.graph.pipeline import process_event
from noc_agents.services.lifecycle import apply_work_note_side_effects, close_incident
from noc_agents.services.worklog_monitor import chase_silent_incidents
from sqlalchemy import select


def test_ticket_fields_floor_aligned(tmp_db):
    settings, session = tmp_db
    inc = process_event(
        session,
        settings,
        EventIngest(
            site_id="SFC-NBIE-HUB-EMB",
            site_name="Embakasi East Aggregation HUB",
            site_type="HUB",
            region_code="NBI_E",
            alarm_code="POWER_GRID_FAIL",
            failure_domain="POWER",
            users_affected=450000,
            battery_countdown_min=45,
            technology=["4G", "5G"],
        ),
    )
    assert inc.incident_number.startswith("INC") and len(inc.incident_number) == 9
    assert inc.tt_category == "POWER_GRID"
    assert inc.site_class == "CRITICAL"
    assert inc.responsible_msp == "EGYPRO"
    assert inc.escalated_at is not None
    assert inc.expected_resolution_at is not None
    assert inc.failure_time is not None
    assert inc.rnio_name == "RNIO-NBI-E"


def test_hub_cascade_child_links(tmp_db):
    settings, session = tmp_db
    parent = process_event(
        session,
        settings,
        EventIngest(
            site_id="SFC-NBIW-HUB-WLD",
            site_name="Westlands Aggregation HUB",
            site_type="HUB",
            region_code="NBI_W",
            alarm_code="POWER_GRID_FAIL",
            failure_domain="POWER",
            users_affected=450000,
        ),
    )
    linked = process_event(
        session,
        settings,
        EventIngest(
            site_id="SFC-NBIW-ENB-CBD07",
            site_name="Upper Hill eNodeB",
            site_type="ENODEB",
            region_code="NBI_W",
            alarm_code="SITE_DOWN",
            failure_domain="POWER",
            users_affected=20000,
            parent_hub_id="SFC-NBIW-HUB-WLD",
        ),
    )
    assert linked.id == parent.id
    session.refresh(parent)
    assert (parent.child_sites_down or 0) >= 1


def test_msp_note_progress_fields_and_close(tmp_db):
    settings, session = tmp_db
    inc = process_event(
        session,
        settings,
        EventIngest(
            site_id="SFC-MTK-HUB-THK",
            site_type="HUB",
            region_code="MTK",
            alarm_code="TX_FIBRE_CUT",
            failure_domain="TRANSMISSION",
            users_affected=160000,
        ),
    )
    assert inc.msp_name in ("SOLITON", "EGYPRO_FIBRE")
    note = WorkNoteRow(
        incident_id=inc.id,
        author="Soliton tech",
        author_role="MSP",
        body="Joint located; splicing started",
        source="msp",
    )
    session.add(note)
    apply_work_note_side_effects(
        session,
        inc,
        author_role="MSP",
        body=note.body,
        vendor_tt_ref="SOL-KE-4401",
        msp_root_cause="Fibre cut by road works",
        msp_action_taken="Splicing in progress",
        msp_percent_complete=40,
    )
    session.commit()
    session.refresh(inc)
    assert inc.status == "IN_PROGRESS"
    assert inc.vendor_tt_ref == "SOL-KE-4401"
    assert inc.msp_percent_complete == 40
    assert "Fibre cut" in (inc.msp_root_cause or "")

    apply_work_note_side_effects(
        session, inc, author_role="MSP", body="Service RESTORED", mark_restored=True, msp_percent_complete=100
    )
    session.commit()
    session.refresh(inc)
    assert inc.status == "RESTORED"
    assert inc.msp_percent_complete == 100

    close_incident(session, inc, closed_by="Supervisor", resolution_code="CLOSED_NORMAL", resolution_summary="OK")
    session.commit()
    session.refresh(inc)
    assert inc.status == "CLOSED"


def test_worklog_chase_escalates_silence(tmp_db):
    settings, session = tmp_db
    inc = process_event(
        session,
        settings,
        EventIngest(
            site_id="SFC-RFT-HUB-NKR",
            site_type="HUB",
            region_code="RFT",
            alarm_code="POWER_GRID_FAIL",
            failure_domain="POWER",
            users_affected=160000,
        ),
    )
    inc.created_at = utcnow() - timedelta(hours=5)
    inc.sla_ack_due = utcnow() - timedelta(hours=4)
    session.commit()

    results = chase_silent_incidents(session, settings.operator)
    assert any(r.incident_id == inc.id for r in results)
