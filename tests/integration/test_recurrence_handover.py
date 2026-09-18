from noc_agents.domain.schemas import EventIngest
from noc_agents.graph.pipeline import process_event
from noc_agents.services.handover import build_handover
from noc_agents.db.models import ProblemRow
from sqlalchemy import select


def test_recurrence_opens_problem(tmp_db):
    settings, session = tmp_db
    base = EventIngest(
        site_id="SFC-RFT-HUB-ELD",
        site_name="Eldoret Rift HUB",
        site_type="HUB",
        region_code="RFT",
        alarm_code="GENSET_FAIL",
        failure_domain="POWER",
        users_affected=160000,
    )
    for i in range(3):
        e = base.model_copy(update={"alarm_code": f"GENSET_FAIL_{i}"})
        process_event(session, settings, e)

    problems = session.scalars(select(ProblemRow)).all()
    assert len(problems) >= 1
    assert problems[0].occurrence_count >= 3
    assert problems[0].problem_number.startswith("PRB")


def test_handover_lists_owners(tmp_db):
    settings, session = tmp_db
    process_event(
        session,
        settings,
        EventIngest(
            site_id="SFC-WNY-HUB-KSM",
            site_name="Kisumu Western-Nyanza HUB",
            site_type="HUB",
            region_code="WNY",
            alarm_code="POWER_OUT",
            failure_domain="POWER",
            users_affected=190000,
        ),
    )
    ho = build_handover(session, settings.operator)
    assert ho["open_total"] >= 1
    assert "NOC Handover" in ho["subject"]
    assert ho["incidents"]
    assert ho["incidents"][0]["owner"]
