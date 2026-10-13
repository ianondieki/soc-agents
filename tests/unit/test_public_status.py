"""``GET /api/v1/public/status``: the customer's service status page (services.public_status).

The contract (exact keys, every profile region, Z timestamps), the state ladder, what counts
as open, the closed service vocabulary, planned works, and the two things a public page must
never do: show another operator's tickets, or carry anything that names a site, a ticket or a
person.
"""

from __future__ import annotations

import importlib
import json
import re
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from noc_agents.db.models import IncidentRow, get_session, new_id
from noc_agents.db.models_maintenance import MaintenanceWindowRow
from noc_agents.realtime.hub import hub
from noc_agents.services import public_status as ps

TIMESTAMP_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
TOP_KEYS = ["generated_at", "operator_display_name", "state", "regions", "planned", "history", "recent"]
#: Both operator profiles use CST, so an isolation leak would merge silently here.
SHARED = "CST"
SITE = "SFC-RFT-HUB-NKR"  # catalogued: Nakuru Rift HUB, Nakuru county, RFT


def _now() -> datetime:
    return datetime.utcnow().replace(microsecond=0)


class Page:
    def __init__(self, client: TestClient) -> None:
        self.client = client
        self._n = 0

    def incident(
        self,
        *,
        region: str = SHARED,
        operator: str = "safaricom",
        priority: str = "P2",
        status: str = "IN_PROGRESS",
        services: list[str] | None = None,
        mpesa: bool = False,
        affecting: bool = True,
        county: str | None = "Mombasa",
        restored_at: datetime | None = None,
        began: datetime | None = None,
        parent: str | None = None,
    ) -> str:
        self._n += 1
        number = f"{'INC' if operator == 'safaricom' else 'AIR'}{self._n:06d}"
        session = get_session()
        try:
            session.add(
                IncidentRow(
                    id=new_id(),
                    operator_id=operator,
                    incident_number=number,
                    status=status,
                    priority=priority,
                    site_id=f"SECRET-SITE-{self._n:03d}",
                    site_name=f"Hidden Tower {self._n}",
                    title=f"PRIVATE ALARM TEXT {self._n}",
                    region_code=region,
                    county=county,
                    services_impacted_json=json.dumps(services if services is not None else ["VOICE", "DATA"]),
                    mpesa_risk=mpesa,
                    service_affecting=affecting,
                    users_affected=123456,
                    fe_name="FE-SECRET-01",
                    msp_name="SecretVendor",
                    created_at=began or _now() - timedelta(hours=1),
                    updated_at=began or _now() - timedelta(hours=1),
                    outage_start_at=began,
                    restored_at=restored_at,
                    parent_incident_id=parent,
                    correlation_fingerprint=f"fp-{self._n}",
                )
            )
            session.commit()
        finally:
            session.close()
        return number

    def window(
        self,
        *,
        scope: str = "REGION",
        ref: str = SHARED,
        status: str = "SCHEDULED",
        starts_in: timedelta = timedelta(days=1),
        hours: int = 4,
        operator: str = "safaricom",
    ) -> None:
        self._n += 1
        start = _now() + starts_in
        session = get_session()
        try:
            session.add(
                MaintenanceWindowRow(
                    id=new_id(),
                    operator_id=operator,
                    scope=scope,
                    scope_ref=ref,
                    starts_at=start,
                    ends_at=start + timedelta(hours=hours),
                    uid=f"uid-{self._n}@test",
                    organizer="planning@test",
                    attendees_ref="audiences.FE_ONCALL",
                    status=status,
                )
            )
            session.commit()
        finally:
            session.close()

    def get(self) -> dict:
        response = self.client.get("/api/v1/public/status")
        assert response.status_code == 200, response.text
        return response.json()

    def region(self, code: str = SHARED) -> dict:
        return next(r for r in self.get()["regions"] if r["region_code"] == code)


@pytest.fixture()
def page(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'status.db').as_posix()}")
    monkeypatch.setenv("OPERATOR_PROFILE", "safaricom")
    import noc_agents.config as cfg
    import noc_agents.db.models as models
    import noc_agents.main as main

    cfg.clear_settings_cache()
    models._engine = None
    models.SessionLocal = None
    importlib.reload(main)
    hub._history.clear()
    with TestClient(main.app) as client:
        yield Page(client)
    hub._history.clear()
    cfg.clear_settings_cache()


# ---------------------------------------------------------------- the contract


def test_the_payload_has_the_exact_keys_and_every_profile_region(page):
    body = page.get()
    assert list(body) == TOP_KEYS
    assert TIMESTAMP_Z.match(body["generated_at"])
    assert body["state"] == "NORMAL"
    assert body["planned"] == []
    from noc_agents.config import get_settings

    codes = sorted(get_settings().operator.regions)
    assert [r["region_code"] for r in body["regions"]] == codes
    for region in body["regions"]:
        assert list(region) == list(ps.REGION_KEYS)
        assert region["state"] == "NORMAL" and region["services"] == [] and region["since"] is None


def test_the_route_is_open_and_cacheable_for_a_short_while(page):
    response = page.client.get("/api/v1/public/status")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=30"
    assert "(demo profile)" not in response.json()["operator_display_name"]


# ---------------------------------------------------------------- states


@pytest.mark.parametrize(("priority", "state"), [("P1", "MAJOR"), ("P2", "PARTIAL"), ("P3", "MINOR"), ("P4", "MINOR")])
def test_a_region_takes_the_state_of_its_worst_open_ticket(page, priority, state):
    page.incident(priority=priority)
    assert page.region()["state"] == state
    assert page.get()["state"] == state


def test_the_headline_is_the_worst_region(page):
    page.incident(priority="P3", region="MTK")
    page.incident(priority="P1", region=SHARED)
    body = page.get()
    assert body["state"] == "MAJOR"
    assert page.region("MTK")["state"] == "MINOR"


@pytest.mark.parametrize("status", ["RESTORED", "CLOSED", "CANCELLED"])
def test_a_ticket_that_is_not_open_leaves_the_region_normal(page, status):
    page.incident(priority="P1", status=status)
    assert page.region()["state"] == "NORMAL"


def test_a_ticket_that_affects_no_service_does_not_move_the_page(page):
    page.incident(priority="P1", affecting=False)
    assert page.region()["state"] == "NORMAL"


def test_since_is_when_the_oldest_open_ticket_began(page):
    page.incident(priority="P2")
    assert TIMESTAMP_Z.match(page.region()["since"])


def test_a_restoration_in_the_last_day_is_said_and_an_older_one_is_not(page):
    page.incident(status="RESTORED", restored_at=_now() - timedelta(hours=2))
    assert TIMESTAMP_Z.match(page.region()["restored_at"])
    page.incident(status="RESTORED", restored_at=_now() - timedelta(hours=30), region="MTK")
    assert page.region("MTK")["restored_at"] is None


# ---------------------------------------------------------------- services


def test_services_come_from_a_closed_vocabulary_in_a_fixed_order(page):
    page.incident(services=["SMS", "MPESA_CORRIDOR", "VOICE", "<script>alert(1)</script>", "FIBRE_CUT"])
    assert page.region()["services"] == ["VOICE", "SMS", "MPESA"]


def test_an_mpesa_risk_ticket_names_mpesa_even_without_the_code(page):
    page.incident(services=["DATA"], mpesa=True)
    assert page.region()["services"] == ["DATA", "MPESA"]


def test_counties_are_the_open_tickets_counties_sorted_and_distinct(page):
    page.incident(county="Mombasa")
    page.incident(county="Kilifi")
    page.incident(county="Mombasa")
    assert page.region()["counties"] == ["Kilifi", "Mombasa"]


def test_a_county_outside_the_region_is_never_named_under_it(page):
    """A ticket's county comes from the site record; a public page must not say "around Nairobi"
    under the Coast because one record is wrong."""
    page.incident(county="Mombasa")
    page.incident(county="Nairobi")
    assert page.region()["counties"] == ["Mombasa"]


# ---------------------------------------------------------------- planned works


def test_an_approved_window_in_the_next_week_is_listed_at_region_level(page):
    page.window()
    planned = page.get()["planned"]
    assert len(planned) == 1
    assert list(planned[0]) == list(ps.PLANNED_KEYS)
    assert planned[0]["scope"] == "REGION" and planned[0]["region_code"] == SHARED
    assert TIMESTAMP_Z.match(planned[0]["starts_at"]) and TIMESTAMP_Z.match(planned[0]["ends_at"])


@pytest.mark.parametrize("status", ["PROPOSED", "CANCELLED", "COMPLETED"])
def test_a_window_nobody_has_approved_or_that_is_over_is_not_listed(page, status):
    page.window(status=status)
    assert page.get()["planned"] == []


def test_a_window_beyond_the_week_or_already_ended_is_not_listed(page):
    page.window(starts_in=timedelta(days=ps.PLANNED_DAYS + 1))
    page.window(starts_in=-timedelta(hours=6), hours=2)
    assert page.get()["planned"] == []


def test_a_single_site_window_names_its_county_and_never_the_site(page):
    page.window(scope="SITE", ref=SITE)
    planned = page.get()["planned"]
    assert planned[0]["scope"] == "LOCAL" and planned[0]["region_code"] == "RFT"
    assert planned[0]["county"] == "Nakuru"
    assert SITE not in json.dumps(planned) and "Nakuru Rift HUB" not in json.dumps(planned)


def test_a_window_in_progress_with_no_ticket_reads_maintenance(page):
    page.window(starts_in=-timedelta(hours=1))
    assert page.region()["state"] == "MAINTENANCE"
    page.incident(priority="P3")
    assert page.region()["state"] == "MINOR"


def test_a_network_window_covers_every_region_without_a_label(page):
    page.window(scope="NETWORK", ref="ALL", starts_in=-timedelta(minutes=30))
    body = page.get()
    assert body["planned"][0]["scope"] == "NETWORK" and body["planned"][0]["region_code"] is None
    assert {r["state"] for r in body["regions"]} == {"MAINTENANCE"}


# ---------------------------------------------------------------- what a public page must never do


def test_nothing_that_names_a_site_a_ticket_or_a_person_is_on_the_page(page):
    number = page.incident(priority="P1", mpesa=True)
    blob = json.dumps(page.get())
    for secret in (number, "SECRET-SITE", "Hidden Tower", "PRIVATE ALARM TEXT", "FE-SECRET", "SecretVendor", "123456"):
        assert secret not in blob, secret


def test_another_operators_tickets_and_windows_never_move_this_page(page):
    page.incident(priority="P1", operator="airtel")
    page.window(operator="airtel", starts_in=-timedelta(minutes=10))
    body = page.get()
    assert body["state"] == "NORMAL"
    assert page.region()["state"] == "NORMAL"
    assert body["planned"] == []


# ---------------------------------------------------------------- the last seven days


def _eat_today():
    return (datetime.utcnow() + timedelta(hours=3)).date()


def _day(body: dict, code: str, days_ago: int) -> str:
    row = next(h for h in body["history"] if h["region_code"] == code)
    return next(d["state"] for d in row["days"] if d["date"] == (_eat_today() - timedelta(days=days_ago)).isoformat())


def test_history_has_seven_days_for_every_region_ending_today(page):
    body = page.get()
    assert [h["region_code"] for h in body["history"]] == [r["region_code"] for r in body["regions"]]
    for row in body["history"]:
        dates = [d["date"] for d in row["days"]]
        assert len(dates) == ps.HISTORY_DAYS
        assert dates[-1] == _eat_today().isoformat()
        assert dates == sorted(dates)
        assert {d["state"] for d in row["days"]} == {"NORMAL"}
    assert body["recent"] == []


def test_an_open_ticket_marks_today_with_its_state(page):
    page.incident(priority="P1")
    body = page.get()
    assert _day(body, SHARED, 0) == "MAJOR"
    assert _day(body, "MTK", 0) == "NORMAL"


def test_a_restored_outage_marks_its_day_and_is_listed_at_region_level(page):
    began = _now() - timedelta(days=2)
    number = page.incident(priority="P2", status="CLOSED", began=began, restored_at=began + timedelta(hours=3), mpesa=True)
    body = page.get()
    assert _day(body, SHARED, 2) == "PARTIAL" or _day(body, SHARED, 3) == "PARTIAL"
    assert _day(body, SHARED, 0) == "NORMAL"
    assert len(body["recent"]) == 1
    row = body["recent"][0]
    assert list(row) == list(ps.RECENT_KEYS)
    assert row["region_code"] == SHARED and row["state"] == "PARTIAL"
    assert row["services"] == ["VOICE", "DATA", "MPESA"]
    assert TIMESTAMP_Z.match(row["started_at"]) and TIMESTAMP_Z.match(row["restored_at"])
    blob = json.dumps(body["recent"])
    for secret in (number, "SECRET-SITE", "Hidden Tower", "PRIVATE ALARM TEXT", "SecretVendor", "123456"):
        assert secret not in blob, secret


def test_a_cancelled_ticket_was_no_outage(page):
    page.incident(priority="P1", status="CANCELLED", began=_now() - timedelta(days=1))
    assert _day(page.get(), SHARED, 1) == "NORMAL"


def test_a_cascade_child_is_part_of_its_parents_outage_not_a_row_of_its_own(page):
    began = _now() - timedelta(days=1)
    page.incident(priority="P2", status="CLOSED", began=began, restored_at=began + timedelta(hours=2))
    page.incident(priority="P4", status="CLOSED", began=began, restored_at=began + timedelta(hours=2), parent="parent-id")
    assert len(page.get()["recent"]) == 1


def test_nothing_older_than_the_window_is_shown(page):
    began = _now() - timedelta(days=ps.HISTORY_DAYS + 3)
    page.incident(priority="P1", status="CLOSED", began=began, restored_at=began + timedelta(hours=2))
    body = page.get()
    assert body["recent"] == []
    assert all(d["state"] == "NORMAL" for h in body["history"] for d in h["days"])


def test_another_operators_outages_never_reach_this_history(page):
    began = _now() - timedelta(days=1)
    page.incident(priority="P1", operator="airtel", status="CLOSED", began=began, restored_at=began + timedelta(hours=1))
    body = page.get()
    assert body["recent"] == []
    assert _day(body, SHARED, 1) == "NORMAL"


def test_overlapping_outages_in_one_region_are_one_disruption(page):
    began = _now() - timedelta(days=1)
    page.incident(priority="P3", status="CLOSED", began=began, restored_at=began + timedelta(hours=2), services=["SMS"])
    page.incident(priority="P1", status="CLOSED", began=began + timedelta(hours=1), restored_at=began + timedelta(hours=4),
                  services=["VOICE"], county="Kilifi")
    page.incident(priority="P2", status="CLOSED", began=began + timedelta(hours=6), restored_at=began + timedelta(hours=7))
    recent = page.get()["recent"]
    assert len(recent) == 2
    merged = recent[1]
    assert merged["state"] == "MAJOR"
    assert merged["services"] == ["VOICE", "SMS"]
    assert merged["counties"] == ["Kilifi", "Mombasa"]
    assert merged["restored_at"] < recent[0]["restored_at"]
