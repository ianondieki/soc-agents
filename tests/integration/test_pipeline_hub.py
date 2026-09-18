from noc_agents.domain.schemas import EventIngest
from noc_agents.graph.pipeline import process_event
from noc_agents.db.models import AgentRunRow, HitlTaskRow, IncidentRow
from sqlalchemy import select


def test_nairobi_east_hub_outage_full_pipeline(tmp_db):
    settings, session = tmp_db
    event = EventIngest(
        site_id="SFC-NBIE-HUB-EMB",
        site_name="Embakasi East Aggregation HUB",
        site_type="HUB",
        region_code="NBI_E",
        alarm_code="POWER_GRID_FAIL",
        failure_domain="POWER",
        users_affected=450000,
        access_notes="Genset not started",
    )
    inc = process_event(session, settings, event)
    assert inc.incident_number.startswith("INC")
    assert len(inc.incident_number) == 9
    assert inc.priority == "P2"  # 450k users → P2 (<500k)
    assert inc.mpesa_risk is True
    assert inc.msp_name == "EGYPRO"
    assert inc.responsible_msp == "EGYPRO"
    assert inc.region_code == "NBI_E"
    assert inc.escalated_at is not None
    assert inc.failure_time is not None
    assert inc.expected_resolution_at is not None
    assert inc.fe_name
    assert inc.requires_hitl is True
    assert inc.hitl_state == "PENDING"

    run = session.scalar(select(AgentRunRow).where(AgentRunRow.incident_id == inc.id))
    assert run is not None
    assert len(run.steps) >= 8
    nodes = {s.node_name for s in run.steps}
    assert "SEVERITY" in nodes
    assert "TICKET" in nodes
    assert "ASSIGN" in nodes

    hitl = session.scalar(select(HitlTaskRow).where(HitlTaskRow.incident_id == inc.id))
    assert hitl is not None


def test_idempotent_correlation(tmp_db):
    settings, session = tmp_db
    event = EventIngest(
        site_id="SFC-CST-HUB-MSA",
        site_name="Mombasa Island HUB",
        site_type="HUB",
        region_code="CST",
        alarm_code="TX_FIBRE_CUT",
        failure_domain="TRANSMISSION",
        users_affected=200000,
    )
    a = process_event(session, settings, event)
    b = process_event(session, settings, event)
    assert a.id == b.id
    open_ones = session.scalars(
        select(IncidentRow).where(
            IncidentRow.site_id == "SFC-CST-HUB-MSA",
            IncidentRow.status.not_in(["CLOSED", "CANCELLED"]),
        )
    ).all()
    assert len(open_ones) == 1


def test_rift_tetranet_and_wny(tmp_db):
    settings, session = tmp_db
    rift = process_event(
        session,
        settings,
        EventIngest(
            site_id="SFC-RFT-HUB-NKR",
            site_type="HUB",
            region_code="RFT",
            alarm_code="POWER_GRID_FAIL",
            failure_domain="POWER",
            users_affected=180000,
        ),
    )
    assert rift.msp_name == "TETRANET"
    wny = process_event(
        session,
        settings,
        EventIngest(
            site_id="SFC-WNY-HUB-KSM",
            site_type="HUB",
            region_code="WNY",
            alarm_code="POWER_GRID_FAIL",
            failure_domain="POWER",
            users_affected=190000,
        ),
    )
    assert wny.msp_name == "TETRANET"
    assert wny.radio_oem == "MIXED"
