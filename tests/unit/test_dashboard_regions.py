"""``GET /api/v1/dashboard/regions`` — the Phase 4 exit criterion "dashboard contract test".

Three things are being protected here, in descending order of how badly they hurt
when they break.

**1. The response shape.** The Regions page is the frontend's only source for the
per-region picture, so every key, every type and the row ordering are a contract.
Section 1 pins them literally — exact key sets, not ``"x" in payload`` — because a
subset assertion passes happily while a key quietly disappears. Adding a key is
allowed and the tests say so; renaming, removing or reordering one fails here
rather than in a browser at 2 a.m.

**2. STALE is a state, not a missing value.** A region with no recent signal must
never render as green. Silence on a NOC wallboard is not good news: it is equally
consistent with "nothing is wrong" and with "the alarm feed died two hours ago",
and a dashboard that resolves that ambiguity in the cheerful direction is worse
than no dashboard. Section 2 pins the status ladder, including the cases where
STALE must win and the cases where it must not (a live P1 is still an ALERT).

**3. The totals are one operator's.** This is the surface where an unscoped
``select()`` does the most damage: an aggregate carries no row id for a reviewer
to notice is foreign, so the other operator's faults are simply *added to our
counts* and read as our own bad night. Section 3 seeds both operators into the one
database — in ``CST``, the region code both profiles happen to use, which is the
sharpest possible case — and asserts the other one's rows are absent from every
number on the card.

Section 4 covers the CA QoS baseline seed, whose whole job is to be honest about
what nobody has read yet.

No network: the CA figures are a seed file and the signal rows are written
directly into ``external_signals``, exactly as the Phase 3 pollers would.
"""

from __future__ import annotations

import importlib
import json
import re
from datetime import datetime, timedelta

import pytest
import yaml
from fastapi.testclient import TestClient

from noc_agents.config import get_settings
from noc_agents.db.models import (
    ExternalSignalRow,
    IncidentRow,
    ProblemRow,
    get_session,
    new_id,
)
from noc_agents.realtime.hub import hub
from noc_agents.services import dashboards

# A fixed instant so every window boundary in this file is arithmetic, not luck.
NOW = datetime(2026, 9, 18, 12, 0, 0)

#: ``CST`` is a region code BOTH operator profiles use (safaricom "Coast", airtel
#: "Coast"). Every isolation test seeds the airtel rows here on purpose: a missing
#: operator clause merges the two silently, with no foreign id on the wire to give
#: it away.
SHARED_REGION = "CST"

TIMESTAMP_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

TOP_LEVEL_KEYS = ["generated_at", "operator_id", "window_days", "weather_enabled", "regions"]

REGION_KEYS = [
    "region_code",
    "label",
    "counties",
    "rnio",
    "status",
    "signals_stale",
    "open_total",
    "open_by_priority",
    "sla_breached",
    "problems_open",
    "problems_open_total",
    "incidents_30d",
    "repeat_faults_30d",
    "repeat_fault_rate_30d",
    "signals",
    "complaint_surge",
    "regulatory_baseline",
]

SIGNAL_KEYS = ["weather", "flood", "cap", "kplc"]

WEATHER_KEYS = [
    "available",
    "enabled",
    "stale",
    "storm_flag",
    "fetched_at",
    "age_s",
    "last_error",
    "precision_30d",
    "reason",
]
FLOOD_KEYS = ["available", "stale", "flag", "fetched_at"]
CAP_KEYS = ["available", "stale", "count", "fetched_at"]
KPLC_KEYS = ["available", "stale", "windows_next_48h", "fetched_at"]

PROBLEM_KEYS = [
    "problem_number",
    "site_id",
    "occurrence_count",
    "last_seen",
    "known_error",
    "status",
]

BASELINE_KEYS = [
    "ca_qos_score",
    "report",
    "report_date",
    "granularity",
    "cluster",
    "pass_mark_pct",
    "meets_pass_mark",
    "parsed",
    "source_url",
]


# ================================================================= fixtures / helpers


@pytest.fixture(autouse=True)
def _fresh_baseline_cache():
    """The seed loader memoises. Clear it around every test so one test's monkeypatched
    path can never become the next test's answer."""
    dashboards.clear_ca_qos_cache()
    yield
    dashboards.clear_ca_qos_cache()


class Dash:
    """The dashboard under test, plus the smallest possible row writers.

    Rows go in directly rather than through ``process_event``: this file is about
    the rollup, and the pipeline would decide the region, priority and site for
    itself. Direct writes let a test say "one open P2 in CST, nothing else" and
    mean it.
    """

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self._n = 0

    def _next(self) -> int:
        self._n += 1
        return self._n

    def incident(
        self,
        *,
        region: str = SHARED_REGION,
        operator: str = "safaricom",
        priority: str = "P3",
        status: str = "IN_PROGRESS",
        created_at: datetime | None = None,
        recurrence_count: int = 1,
        sla_restore_due: datetime | None = None,
    ) -> str:
        n = self._next()
        number = f"{'INC' if operator == 'safaricom' else 'AIR'}{n:06d}"
        session = get_session()
        try:
            session.add(
                IncidentRow(
                    id=new_id(),
                    operator_id=operator,
                    incident_number=number,
                    status=status,
                    priority=priority,
                    site_id=f"SITE-{n:03d}",
                    region_code=region,
                    created_at=created_at or NOW,
                    updated_at=created_at or NOW,
                    recurrence_count=recurrence_count,
                    sla_restore_due=sla_restore_due,
                    correlation_fingerprint=f"fp-{n}",
                )
            )
            session.commit()
        finally:
            session.close()
        return number

    def problem(
        self,
        *,
        region: str = SHARED_REGION,
        operator: str = "safaricom",
        occurrences: int = 1,
        status: str = "OPEN",
        known_error: bool = False,
        last_seen: datetime | None = None,
    ) -> str:
        n = self._next()
        number = f"{'PRB' if operator == 'safaricom' else 'APB'}{n:06d}"
        session = get_session()
        try:
            session.add(
                ProblemRow(
                    id=new_id(),
                    operator_id=operator,
                    problem_number=number,
                    signature=f"SITE-{n:03d}|POWER",
                    site_id=f"SITE-{n:03d}",
                    region_code=region,
                    occurrence_count=occurrences,
                    status=status,
                    is_known_error=1 if known_error else 0,
                    first_seen=NOW - timedelta(days=5),
                    last_seen=last_seen or NOW,
                )
            )
            session.commit()
        finally:
            session.close()
        return number

    def weather_row(
        self,
        *,
        region: str = SHARED_REGION,
        operator: str = "safaricom",
        storm: bool = False,
        fetched_at: datetime | None = None,
        valid_until: datetime | None = None,
    ) -> None:
        """One ``external_signals`` row shaped exactly as ``pollers.weather`` writes it."""
        fetched = fetched_at or (NOW - timedelta(minutes=5))
        derived = {
            "rain_mm_next_6h": 21.0 if storm else 0.2,
            "storm_flag": storm,
            "storm_reasons": ["rain 21 mm/6h >= 20"] if storm else [],
            "flood_flag": False,
            "thunder": storm,
        }
        n = self._next()
        session = get_session()
        try:
            session.add(
                ExternalSignalRow(
                    id=new_id(),
                    operator_id=operator,
                    source="OPEN_METEO",
                    source_url="mock://open-meteo",
                    external_id=f"{region}:{n}",
                    region_code=region,
                    fetched_at=fetched,
                    valid_from=fetched,
                    valid_until=valid_until or (fetched + timedelta(hours=1)),
                    stale=0,
                    storm_flag=1 if storm else 0,
                    payload_json="{}",
                    derived_json=json.dumps(derived),
                    created_at=fetched,
                )
            )
            session.commit()
        finally:
            session.close()

    def get(self, now: datetime | None = NOW) -> dict:
        """The payload, through the service, at a pinned instant."""
        session = get_session()
        try:
            return dashboards.regions_dashboard(session, now=now)
        finally:
            session.close()

    def region(self, code: str = SHARED_REGION, now: datetime | None = NOW) -> dict:
        return {r["region_code"]: r for r in self.get(now)["regions"]}[code]

    def http(self) -> dict:
        """The payload over HTTP, so the route, its gate and the router wiring are covered."""
        response = self.client.get("/api/v1/dashboard/regions")
        assert response.status_code == 200, response.text
        return response.json()


@pytest.fixture()
def dash(tmp_path, monkeypatch):
    db = tmp_path / "dashboard.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")
    monkeypatch.setenv("OPERATOR_PROFILE", "safaricom")
    # Pinned rather than inherited: WEATHER_ENABLED exported in a developer's shell
    # would flip the degraded-mode assertions, and "off" is the deployment default
    # these tests are mostly about.
    monkeypatch.setenv("WEATHER_ENABLED", "false")

    import noc_agents.config as cfg
    import noc_agents.db.models as models
    import noc_agents.main as main

    cfg.clear_settings_cache()
    models._engine = None
    models.SessionLocal = None
    importlib.reload(main)
    hub._history.clear()
    with TestClient(main.app) as client:
        yield Dash(client)
    hub._history.clear()
    cfg.clear_settings_cache()


# =================================================================================
# Section 1 — the contract. Exact keys, exact types, fixed ordering.
# =================================================================================


def test_the_dashboard_answers_with_the_exact_top_level_envelope_the_frontend_reads(dash):
    payload = dash.http()
    assert list(payload) == TOP_LEVEL_KEYS
    assert TIMESTAMP_Z.match(payload["generated_at"]), payload["generated_at"]
    assert payload["operator_id"] == "safaricom"
    assert payload["window_days"] == 30
    # Reported at the top so "every region is STALE" can be told apart from
    # "the poller is switched off" without reading six identical reasons.
    assert payload["weather_enabled"] is False
    assert isinstance(payload["regions"], list)


def test_every_region_card_carries_the_exact_key_set_in_a_fixed_order(dash):
    """Key ORDER as well as membership. The frontend is free to ignore order, but a
    diff that reorders keys is almost always a rewrite of this serializer, and a
    reviewer should be made to look at it."""
    for region in dash.http()["regions"]:
        assert list(region) == REGION_KEYS, region["region_code"]


def test_the_signal_blocks_carry_the_exact_keys_each_badge_needs(dash):
    region = dash.http()["regions"][0]
    signals = region["signals"]
    assert list(signals) == SIGNAL_KEYS
    assert list(signals["weather"]) == WEATHER_KEYS
    assert list(signals["flood"]) == FLOOD_KEYS
    assert list(signals["cap"]) == CAP_KEYS
    assert list(signals["kplc"]) == KPLC_KEYS


def test_every_region_configured_on_the_profile_gets_a_row(dash):
    """Driven by the operator profile, not by a GROUP BY over incidents — the whole
    point being that a region with nothing in it is the one worth looking at."""
    configured = set(get_settings("safaricom").operator.regions)
    assert {r["region_code"] for r in dash.http()["regions"]} == configured
    assert len(configured) == 6  # the six Safaricom geographical regions (§ profile)


def test_regions_are_ordered_by_region_code_so_a_tile_never_moves_under_the_night_shift(dash):
    """Not by severity (tiles would jump every time an incident opened) and not by
    YAML key order (a harmless config reshuffle would move the wallboard)."""
    dash.incident(region="CST", priority="P1")  # would sort first under any severity rule
    codes = [r["region_code"] for r in dash.http()["regions"]]
    assert codes == sorted(codes)
    assert codes == ["CST", "MTK", "NBI_E", "NBI_W", "RFT", "WNY"]


def test_open_by_priority_always_carries_all_four_bands_even_when_every_count_is_zero(dash):
    """So the card can render four counters unconditionally instead of guarding each one."""
    for region in dash.http()["regions"]:
        assert list(region["open_by_priority"]) == ["P1", "P2", "P3", "P4"]
        assert all(isinstance(v, int) for v in region["open_by_priority"].values())


def test_every_timestamp_on_the_payload_is_an_explicit_utc_string(dash):
    """Defect #41: a naive ISO string is read by a browser as LOCAL time, which in
    Nairobi backdates everything by three hours. One spelling, seconds precision."""
    dash.problem(occurrences=2)
    dash.weather_row()
    payload = dash.get()
    stamps = [payload["generated_at"]]
    for region in payload["regions"]:
        stamps += [p["last_seen"] for p in region["problems_open"]]
        stamps += [b["fetched_at"] for b in region["signals"].values()]
    present = [s for s in stamps if s is not None]
    assert present, "no timestamp was produced — the assertion below would be vacuous"
    assert all(TIMESTAMP_Z.match(s) for s in present), present


def test_a_problem_card_carries_the_exact_keys_including_whether_it_is_a_known_error(dash):
    """``known_error`` is on the card because an operator seeing a repeat fault wants
    to know in one glance whether a workaround already exists (§7.7.1)."""
    dash.problem(occurrences=4, known_error=True)
    cards = dash.region()["problems_open"]
    assert len(cards) == 1
    assert list(cards[0]) == PROBLEM_KEYS
    assert cards[0]["known_error"] is True
    assert cards[0]["occurrence_count"] == 4


def test_the_regulatory_baseline_keeps_its_keys_and_never_claims_region_granularity(dash):
    """§7.4.3's granularity mismatch: the CA report has five clusters, the dashboard
    has six regions, and they do not map 1:1. ``"region"`` would be a lie about a
    regulator's measurement, which is the worst kind of number to fabricate."""
    for region in dash.http()["regions"]:
        baseline = region["regulatory_baseline"]
        assert list(baseline) == BASELINE_KEYS, region["region_code"]
        assert baseline["granularity"] in {"cluster", "operator"}
        assert baseline["granularity"] != "region"
        assert baseline["report_date"] == "2026-03"


def _surge(*, region: str, status: str = "open", operator: str = "safaricom", place: str = "rongai") -> str:
    """One support surge row, written directly (the rollup is under test, not surge detection)."""
    from noc_agents.db.models_support import SupportSurgeRow

    session = get_session()
    try:
        row = SupportSurgeRow(operator_id=operator, place=place, region_code=region, status=status,
                              open_place=place if status == "open" else None, card_id="card-1", complaints=4,
                              numbers=3, first_at=NOW - timedelta(minutes=26), last_at=NOW - timedelta(minutes=9))
        session.add(row)
        session.commit()
        return row.id
    finally:
        session.close()


def test_complaint_surge_is_null_for_a_region_without_an_open_surge(dash):
    """The key was declared null-only before close the loop; it stays null wherever there is no
    open surge of customer complaints (docs/CLOSE_THE_LOOP.md section 3)."""
    for region in dash.http()["regions"]:
        assert region["complaint_surge"] is None


def test_complaint_surge_is_the_regions_open_surge_and_only_its_own(dash, monkeypatch):
    """First-party complaints, counts and places only: the region's OPEN surge fills the key; a
    dismissed one, another region's and another operator's do not; the desk off reads as none."""
    surge_id = _surge(region=SHARED_REGION)
    _surge(region=SHARED_REGION, status="dismissed", place="kayole")
    _surge(region=SHARED_REGION, operator="airtel", place="kitengela")
    rows = {r["region_code"]: r["complaint_surge"] for r in dash.http()["regions"]}
    assert rows[SHARED_REGION] == {
        "surge_id": surge_id, "place": "Rongai", "complaints": 4, "numbers": 3,
        "first_at": "2026-09-18T11:34:00Z", "last_at": "2026-09-18T11:51:00Z", "card_id": "card-1",
    }
    assert all(value is None for code, value in rows.items() if code != SHARED_REGION)
    monkeypatch.setenv("SUPPORT_DESK_ENABLED", "false")
    assert all(r["complaint_surge"] is None for r in dash.http()["regions"])


def test_the_route_is_reachable_under_its_spec_path_and_is_read_only(dash):
    """Pins the path itself (§7.4.2) and that nothing on this surface mutates."""
    assert dash.client.get("/api/v1/dashboard/regions").status_code == 200
    assert dash.client.post("/api/v1/dashboard/regions").status_code == 405


# =================================================================================
# Section 2 — STALE is a first-class state, and the rest of the status ladder.
# =================================================================================


def test_a_quiet_region_with_no_signal_at_all_reads_stale_and_not_calm(dash):
    """The rule this dashboard exists for. No incidents and no outside signal is not
    evidence of calm; it is evidence of nothing, and a green tile would be a claim
    we cannot support."""
    region = dash.region()
    assert region["open_total"] == 0
    assert region["signals_stale"] is True
    assert region["status"] == "STALE"


def test_a_stale_region_says_why_so_nobody_has_to_guess_at_the_tile(dash):
    """An amber tile with no explanation gets ignored by the second shift. With the
    poller off, the reason names the flag an operator can actually go and flip."""
    weather = dash.region()["signals"]["weather"]
    assert weather["available"] is False
    assert weather["enabled"] is False
    assert weather["stale"] is True
    assert weather["storm_flag"] is None  # not False: we have no reading, not a calm one
    assert "WEATHER_ENABLED" in weather["reason"]


def test_a_region_reads_calm_only_once_a_fresh_signal_says_somebody_looked(dash):
    dash.weather_row(fetched_at=NOW - timedelta(minutes=5))
    region = dash.region()
    assert region["signals"]["weather"]["available"] is True
    assert region["signals"]["weather"]["stale"] is False
    assert region["signals_stale"] is False
    assert region["status"] == "CALM"


def test_a_reading_past_its_validity_window_goes_back_to_stale(dash):
    """Staleness is recomputed against *now*, never trusted from the stored flag: the
    poller is fail-soft and keeps the last good row when a provider dies, so the
    presence of a reading is no evidence of its freshness."""
    dash.weather_row(fetched_at=NOW - timedelta(hours=6), valid_until=NOW - timedelta(hours=5))
    region = dash.region()
    assert region["signals"]["weather"]["available"] is True
    assert region["signals"]["weather"]["stale"] is True
    assert region["status"] == "STALE"


def test_an_open_p1_outranks_staleness_because_a_known_fire_beats_a_blind_spot(dash):
    dash.incident(priority="P1")
    assert dash.region()["status"] == "ALERT"


def test_an_open_p2_reads_watch_rather_than_alert(dash):
    dash.incident(priority="P2")
    region = dash.region()
    assert region["status"] == "WATCH"
    assert region["open_by_priority"] == {"P1": 0, "P2": 1, "P3": 0, "P4": 0}


def test_an_incident_past_its_restore_sla_reads_watch_even_at_low_priority(dash):
    dash.incident(priority="P4", sla_restore_due=NOW - timedelta(minutes=30))
    region = dash.region()
    assert region["sla_breached"] == 1
    assert region["status"] == "WATCH"


def test_a_live_storm_flag_raises_a_region_with_nothing_open_to_alert(dash):
    """Early warning is the entire value of the weather lane: a storm over the Rift
    at 21:00 is worth a tile going red before the first alarm arrives."""
    dash.weather_row(storm=True, fetched_at=NOW - timedelta(minutes=5))
    region = dash.region()
    assert region["signals"]["weather"]["storm_flag"] is True
    assert region["status"] == "ALERT"


def test_a_storm_flag_from_a_stale_reading_does_not_raise_the_alert(dash):
    """A six-hour-old storm warning is history. It must not hold a tile red — that is
    how a wallboard trains its readers to ignore red."""
    dash.weather_row(storm=True, fetched_at=NOW - timedelta(hours=8), valid_until=NOW - timedelta(hours=7))
    region = dash.region()
    assert region["signals"]["weather"]["storm_flag"] is True
    assert region["signals"]["weather"]["stale"] is True
    assert region["status"] == "STALE"


def test_low_priority_work_in_a_blind_region_still_reads_stale(dash):
    """Open P3s prove alarms arrived; they prove nothing about what we are missing.
    The counts are on the card either way — the status only claims what it can."""
    dash.incident(priority="P3")
    dash.incident(priority="P4")
    region = dash.region()
    assert region["open_total"] == 2
    assert region["status"] == "STALE"


def test_the_status_vocabulary_stays_closed_so_a_tile_always_has_a_colour(dash):
    dash.incident(priority="P1", region="CST")
    dash.incident(priority="P2", region="MTK")
    dash.weather_row(region="RFT")
    assert {r["status"] for r in dash.get()["regions"]} <= set(dashboards.REGION_STATUSES)


# =================================================================================
# Section 2b — the rollups themselves.
# =================================================================================


def test_the_weather_block_is_composed_from_the_poller_cache_rather_than_recomputed(dash):
    """§7.3.3 already derives the risk block and already recomputes staleness. The
    dashboard reads it through; a second derivation here would be a second set of
    thresholds to keep in step with the first."""
    fetched = NOW - timedelta(minutes=12)
    dash.weather_row(storm=True, fetched_at=fetched)
    weather = dash.region()["signals"]["weather"]
    assert weather["fetched_at"] == fetched.replace(microsecond=0).isoformat() + "Z"
    assert weather["age_s"] == 12 * 60
    assert weather["storm_flag"] is True


def test_repeat_fault_rate_is_null_when_nothing_opened_in_the_window(dash):
    """Not 0.0. "0 % of no faults repeated" is a measurement nobody made, and a zero
    on the card reads as a clean bill of health for a region that may simply not be
    reporting."""
    region = dash.region()
    assert region["incidents_30d"] == 0
    assert region["repeat_fault_rate_30d"] is None


def test_repeat_fault_rate_is_the_share_of_window_incidents_that_recurred(dash):
    dash.incident(recurrence_count=1)
    dash.incident(recurrence_count=1)
    dash.incident(recurrence_count=3)
    dash.incident(recurrence_count=2)
    region = dash.region()
    assert (region["incidents_30d"], region["repeat_faults_30d"]) == (4, 2)
    assert region["repeat_fault_rate_30d"] == 0.5


def test_an_incident_older_than_the_window_is_outside_the_repeat_rate(dash):
    """Pinned at the boundary: 30 days exactly is inside, 30 days and a minute is not."""
    dash.incident(created_at=NOW - timedelta(days=30))
    dash.incident(created_at=NOW - timedelta(days=30, minutes=1), recurrence_count=5)
    region = dash.region()
    assert region["incidents_30d"] == 1
    assert region["repeat_faults_30d"] == 0


def test_closed_incidents_leave_the_open_counts_but_stay_in_the_repeat_rate(dash):
    """Two different questions on one card: what is burning now, and how often this
    region catches fire. Collapsing them into one query would answer only one."""
    dash.incident(priority="P2", status="CLOSED", recurrence_count=2)
    region = dash.region()
    assert region["open_total"] == 0
    assert region["open_by_priority"]["P2"] == 0
    assert region["incidents_30d"] == 1
    assert region["repeat_fault_rate_30d"] == 1.0


def test_problems_are_listed_worst_recurring_first_and_the_full_count_is_reported(dash):
    """The PRB that has bitten nine times earns the card, not the one opened most
    recently. ``problems_open_total`` means truncating the list never hides scale."""
    for occurrences in (1, 9, 4, 2, 7, 3):
        dash.problem(occurrences=occurrences)
    region = dash.region()
    assert [p["occurrence_count"] for p in region["problems_open"]] == [9, 7, 4, 3, 2]
    assert region["problems_open_total"] == 6


def test_a_closed_problem_is_off_the_card(dash):
    dash.problem(occurrences=5, status="CLOSED")
    dash.problem(occurrences=2, status="MONITORING")
    region = dash.region()
    assert [p["occurrence_count"] for p in region["problems_open"]] == [2]
    assert region["problems_open_total"] == 1


def test_flood_cap_and_kplc_report_unavailable_rather_than_reassuring_zeroes(dash):
    """Their Phase 3 pollers do not exist yet. ``available: false`` and ``stale: true``
    say "no feed"; a bare ``count: 0`` would say "no warnings in force", which is a
    claim about the weather rather than about us."""
    signals = dash.region()["signals"]
    for name in ("flood", "cap", "kplc"):
        assert signals[name]["available"] is False, name
        assert signals[name]["stale"] is True, name
        assert signals[name]["fetched_at"] is None, name
    assert signals["flood"]["flag"] is None
    assert signals["cap"]["count"] == 0
    assert signals["kplc"]["windows_next_48h"] == 0


# =================================================================================
# Section 3 — operator isolation. Both operators in one file, same region code.
# =================================================================================


def test_the_other_operators_incidents_never_reach_this_operators_region_totals(dash):
    """The leak this dashboard is most exposed to: an aggregate carries no row id, so
    a merged count looks exactly like a busy night rather than like a bug."""
    dash.incident(operator="safaricom", priority="P2", region=SHARED_REGION)
    for _ in range(4):
        dash.incident(operator="airtel", priority="P1", region=SHARED_REGION)
    region = dash.region(SHARED_REGION)
    assert region["open_total"] == 1
    assert region["open_by_priority"] == {"P1": 0, "P2": 1, "P3": 0, "P4": 0}
    assert region["incidents_30d"] == 1
    assert region["status"] == "WATCH"  # not ALERT: those four P1s are not ours


def test_the_other_operators_problems_never_appear_on_a_region_card(dash):
    dash.problem(operator="safaricom", occurrences=2)
    airtel_prb = dash.problem(operator="airtel", occurrences=99)
    region = dash.region(SHARED_REGION)
    assert [p["problem_number"] for p in region["problems_open"]] != []
    assert airtel_prb not in {p["problem_number"] for p in region["problems_open"]}
    assert region["problems_open_total"] == 1


def test_the_other_operators_forecast_never_makes_a_blind_region_look_fresh(dash):
    """Signal rows are operator-owned too. Reading airtel's forecast would turn our
    STALE tile green on the strength of somebody else's poller."""
    dash.weather_row(operator="airtel", storm=True, fetched_at=NOW - timedelta(minutes=2))
    region = dash.region(SHARED_REGION)
    assert region["signals"]["weather"]["available"] is False
    assert region["signals_stale"] is True
    assert region["status"] == "STALE"


def test_the_other_operators_repeat_faults_never_move_our_rate(dash):
    dash.incident(operator="safaricom", recurrence_count=1)
    for _ in range(3):
        dash.incident(operator="airtel", recurrence_count=9)
    region = dash.region(SHARED_REGION)
    assert region["incidents_30d"] == 1
    assert region["repeat_fault_rate_30d"] == 0.0


def test_both_operators_rows_really_do_share_one_database_and_one_region_code(dash):
    """The precondition for every test above. Without it they would all pass vacuously,
    which is the usual way an isolation suite comes to be worth nothing."""
    dash.incident(operator="safaricom", region=SHARED_REGION)
    dash.incident(operator="airtel", region=SHARED_REGION)
    dash.problem(operator="airtel", region=SHARED_REGION)
    dash.weather_row(operator="airtel", region=SHARED_REGION)
    session = get_session()
    try:
        assert {r.operator_id for r in session.query(IncidentRow).all()} == {"safaricom", "airtel"}
        assert {r.region_code for r in session.query(IncidentRow).all()} == {SHARED_REGION}
        assert {r.operator_id for r in session.query(ProblemRow).all()} == {"airtel"}
        assert {r.operator_id for r in session.query(ExternalSignalRow).all()} == {"airtel"}
    finally:
        session.close()
    # And the dashboard, reading the same file, sees only its own.
    region = dash.region(SHARED_REGION)
    assert (region["open_total"], region["problems_open_total"]) == (1, 0)
    assert region["signals"]["weather"]["available"] is False


# =================================================================================
# Section 4 — the CA QoS baseline seed (§7.4.3).
# =================================================================================


def test_the_ca_qos_seed_carries_the_published_figures_and_its_report_date():
    """The three overall scores and the 80 % pass mark are the only figures §7.4.3
    cites. ``report_date`` is on the card because a 2026-03 publication about FY
    2024-25 is already historic when a night shift reads it."""
    seed = dashboards.load_ca_qos()
    assert seed["report"] == "FY2024-2025"
    assert seed["report_date"] == "2026-03"
    assert seed["pass_mark_pct"] == 80.0
    assert seed["operators"]["safaricom"]["overall_pct"] == 89.72
    assert seed["operators"]["airtel"]["overall_pct"] == 81.14
    assert seed["operators"]["telkom"]["overall_pct"] == 52.76
    assert "ca.go.ke" in seed["source_url"]


def test_the_ca_qos_seed_names_no_cluster_it_has_not_transcribed():
    """The invariant that survives a human finishing the file: every cluster named in
    ``cluster_to_region`` must exist in ``clusters``. Today both sides are empty —
    nobody has opened the PDF — and inventing five plausible cluster names would be
    fabricating a regulatory citation, which is the kind of wrong number that
    survives review because it looks authoritative."""
    seed = dashboards.load_ca_qos()
    declared = {c["name"] for c in seed["clusters"]}
    used = {
        cluster
        for per_operator in seed["cluster_to_region"].values()
        for cluster in per_operator.values()
        if cluster is not None
    }
    assert used <= declared, sorted(used - declared)
    for cluster in seed["clusters"]:
        for score in cluster.get("scores", {}).values():
            assert 0.0 <= float(score) <= 100.0, cluster["name"]


def test_the_seed_offers_the_parser_the_real_region_codes_and_invents_none():
    """The mapping doubles as the form a human fills in, so it must list exactly the
    region codes the operator profiles define — a typo'd code there would map a
    regulator's score onto a region that does not exist."""
    seed = dashboards.load_ca_qos()
    for profile in ("safaricom", "airtel"):
        assert set(seed["cluster_to_region"][profile]) == set(
            get_settings(profile).operator.regions
        )


def test_an_unparsed_cluster_mapping_falls_back_to_the_operator_wide_score(dash):
    """The state this repo ships in. The card still shows a number — and says, in
    ``granularity``, that it is the national figure and not a measurement of this
    region. Presenting an average as a local reading is how a dashboard starts lying."""
    baseline = dash.region()["regulatory_baseline"]
    assert baseline["ca_qos_score"] == 89.72
    assert baseline["granularity"] == "operator"
    assert baseline["cluster"] is None
    assert baseline["parsed"] is False
    assert baseline["meets_pass_mark"] is True


def test_a_mapped_and_transcribed_cluster_is_reported_at_cluster_granularity(dash, tmp_path, monkeypatch):
    """What the card looks like once somebody reads the PDF: the cluster's own score,
    and the cluster named, so a reader can check it against the report."""
    seed = tmp_path / "ca_qos.yaml"
    seed.write_text(
        yaml.safe_dump(
            {
                "ca_qos": {
                    "report": "FY2024-2025",
                    "report_date": "2026-03",
                    "source_url": "https://example.invalid/report.pdf",
                    "pass_mark_pct": 80.0,
                    "parsed_by": "RNIO",
                    "clusters": [{"name": "Coast Cluster", "scores": {"safaricom": 91.4}}],
                    "operators": {"safaricom": {"overall_pct": 89.72}},
                    "cluster_to_region": {"safaricom": {"CST": "Coast Cluster"}},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(dashboards, "CA_QOS_SEED_PATH", seed)
    dashboards.clear_ca_qos_cache()

    coast = dash.region("CST")["regulatory_baseline"]
    assert coast["ca_qos_score"] == 91.4
    assert coast["granularity"] == "cluster"
    assert coast["cluster"] == "Coast Cluster"
    assert coast["parsed"] is True
    # An unmapped region in the same file still falls back, rather than borrowing
    # the Coast score or vanishing from the dashboard.
    rift = dash.region("RFT")["regulatory_baseline"]
    assert (rift["granularity"], rift["cluster"], rift["ca_qos_score"]) == ("operator", None, 89.72)


def test_a_region_mapped_to_a_cluster_nobody_scored_does_not_borrow_a_number(dash, tmp_path, monkeypatch):
    """Half-finished parsing is the likely real state of this file for a while. A
    mapping without a transcribed score falls back rather than showing the previous
    cluster's figure under this cluster's name."""
    seed = tmp_path / "ca_qos.yaml"
    seed.write_text(
        yaml.safe_dump(
            {
                "ca_qos": {
                    "report": "FY2024-2025",
                    "report_date": "2026-03",
                    "pass_mark_pct": 80.0,
                    "clusters": [{"name": "Coast Cluster", "scores": {}}],
                    "operators": {"safaricom": {"overall_pct": 89.72}},
                    "cluster_to_region": {"safaricom": {"CST": "Coast Cluster"}},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(dashboards, "CA_QOS_SEED_PATH", seed)
    dashboards.clear_ca_qos_cache()

    coast = dash.region("CST")["regulatory_baseline"]
    assert coast["granularity"] == "operator"
    assert coast["cluster"] is None
    assert coast["ca_qos_score"] == 89.72


def test_a_missing_or_broken_baseline_seed_never_takes_the_dashboard_down(dash, tmp_path, monkeypatch):
    """The regulator's annual score is the least urgent thing on this screen. A night
    shift losing the fault picture because a YAML file was edited badly would be an
    absurd trade, so the block degrades to null and everything else still renders."""
    broken = tmp_path / "not_yaml.yaml"
    broken.write_text("ca_qos: [this: is: not: a: mapping", encoding="utf-8")
    for candidate in (tmp_path / "absent.yaml", broken):
        monkeypatch.setattr(dashboards, "CA_QOS_SEED_PATH", candidate)
        dashboards.clear_ca_qos_cache()
        payload = dash.get()
        assert len(payload["regions"]) == 6
        assert all(r["regulatory_baseline"] is None for r in payload["regions"])
        assert all(list(r) == REGION_KEYS for r in payload["regions"])


def test_an_operator_absent_from_the_seed_gets_no_baseline_rather_than_a_zero(tmp_path, monkeypatch):
    """"No published score" and "scored zero" are opposite claims about a licensee."""
    seed = tmp_path / "ca_qos.yaml"
    seed.write_text(
        yaml.safe_dump({"ca_qos": {"report": "FY2024-2025", "operators": {"airtel": {"overall_pct": 81.14}}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(dashboards, "CA_QOS_SEED_PATH", seed)
    dashboards.clear_ca_qos_cache()
    assert dashboards.regulatory_baseline_for("CST", "safaricom") is None
    assert dashboards.regulatory_baseline_for("CST", "airtel")["ca_qos_score"] == 81.14
