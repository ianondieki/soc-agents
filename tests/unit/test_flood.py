"""GloFAS river-discharge early warning (spec §5.3.13, §7.3.1, §7.3.3; CONFORMANCE C-13).

**Zero network.** Every request goes through ``httpx.MockTransport`` fed from
``tests/fixtures/flood/glofas_kisumu_wny.json`` — built from the documented Open-Meteo Flood API
shape, not captured live (its ``_provenance`` says so) — or from variants derived from it
inside the test. An autouse fixture breaks sockets, as the weather tests do.

What is pinned:

* the request is exactly §7.3.3's (``daily=river_discharge,river_discharge_mean&forecast_days=7``);
* the §7.3.1 rule, literally — peak daily discharge over the mean of daily means, ``>= 2.0`` —
  at its edge, with nulls skipped (never zero) and a zero divisor refused rather than turned
  into an infinite ratio;
* only riverine sites in the active operator's regions are ever requested;
* fail-soft for a timeout, a 500, malformed JSON, an oversized body, a TLS failure and a
  forecast that does not cover today — nothing raises, the last good row keeps its payload and
  its flag, ``last_error`` says why, ``stale`` flips only once ``valid_until`` passes; a site
  never read gets a flagless, already-expired marker;
* the Regions dashboard (not this lane's file) reads the row: a fresh flag is ALERT, a stale
  one is not;
* both operators seeded; neither sees the other's rows;
* the job ships disabled and re-checks its own flag.

Review findings pinned here: F06 (total fetch deadline), F09 (a region's flag aggregates its
sites), F15 (identity encoding, compression refused), F18 (operator scoping of the catalogue).
"""

from __future__ import annotations

import copy
import json
import socket
import ssl
import threading
import time
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from noc_agents.adapters import flood as adapter
from noc_agents.adapters.flood import (
    DailyDischarge,
    FloodError,
    FloodSnapshot,
    FloodThresholds,
    OpenMeteoFloodProvider,
    derive_flood_risk,
    parse_flood,
)
from noc_agents.config import get_settings
from noc_agents.db.models import ExternalSignalRow, new_id
from noc_agents.pollers import flood as poller
from noc_agents.pollers.flood import FLOOD_JOB, ROW_VALID_FOR, SITE_SEED_OPERATOR, latest_site_row, poll, riverine_sites
from noc_agents.realtime.hub import hub
from noc_agents.scheduler.loop import job_enabled, read_state, run_job
from noc_agents.services import dashboards
from noc_agents.services import sites as site_catalogue
from noc_agents.services.signals import flood_reading, flood_region_state, is_stale, list_signals

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "flood" / "glofas_kisumu_wny.json"
KISUMU = "SFC-WNY-HUB-KSM"
#: The fixture's first forecast day is 2026-09-17.
NOW = datetime(2026, 9, 17, 6, 0)


#: The real socket functions, captured before any fixture replaces them (see loopback_only).
_REAL_CONNECT = socket.socket.connect
_REAL_CREATE_CONNECTION = socket.create_connection
_REAL_GETADDRINFO = socket.getaddrinfo


@pytest.fixture(autouse=True)
def no_sockets(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("a flood test tried to open a network socket")

    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket.socket, "connect_ex", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)


@pytest.fixture(autouse=True)
def flood_env(monkeypatch):
    for key in ("WEATHER_ENABLED", "FLOOD_API_BASE", "NOC_USE_TRUSTSTORE"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture()
def clean_hub():
    hub._history.clear()
    yield hub
    hub._history.clear()


def _body() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _provider(respond=None, calls: list | None = None) -> OpenMeteoFloodProvider:
    raw = FIXTURE.read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        if respond is not None:
            return respond(request)
        return httpx.Response(200, content=raw, headers={"content-type": "application/json"})

    return OpenMeteoFloodProvider(client=httpx.Client(transport=httpx.MockTransport(handler)))


def _json(body: dict):
    data = json.dumps(body).encode()
    return lambda request: httpx.Response(200, content=data)


def _snap(discharge, mean, *, start=date(2026, 9, 17)) -> FloodSnapshot:
    days = tuple(DailyDischarge(start + timedelta(days=i), d, m) for i, (d, m) in enumerate(zip(discharge, mean)))
    return FloodSnapshot("GLOFAS", "mock://", 0.0, 0.0, datetime.combine(start, datetime.min.time()), days)


def _rows(session, operator: str = "safaricom") -> list[ExternalSignalRow]:
    return list(session.scalars(
        select(ExternalSignalRow).where(ExternalSignalRow.operator_id == operator, ExternalSignalRow.source == "GLOFAS")
        .order_by(ExternalSignalRow.created_at)
    ).all())


# ------------------------------------------------------------------------------ fixture is honest


def test_fixture_says_it_was_constructed_not_captured():
    prov = _body()["_provenance"]
    assert prov["captured_live"] is False and "NOT a live capture" in prov["how"]
    assert prov["site"]["site_id"] == KISUMU and prov["site"]["riverine"] is True
    assert "constructed, not captured" in (FIXTURE.parent / "README.md").read_text(encoding="utf-8")


# ------------------------------------------------------------------------------ request, parse, derive


def test_the_request_is_exactly_the_one_the_spec_names():
    calls: list[httpx.Request] = []
    snap = _provider(calls=calls).discharge(-0.09, 34.77, now=NOW)
    assert str(calls[0].url) == (
        "https://flood-api.open-meteo.com/v1/flood?latitude=-0.0900&longitude=34.7700"
        "&daily=river_discharge,river_discharge_mean&forecast_days=7"
    )
    assert (snap.latitude, snap.longitude) == (-0.075, 34.775)  # the grid cell GloFAS snapped to, as returned
    assert snap.source == "GLOFAS" and snap.fetched_at == NOW and len(snap.days) == 7


def test_parse_reads_the_documented_daily_shape():
    days = parse_flood(_body())
    assert days[0] == DailyDischarge(date(2026, 9, 17), 38.2, 44.1)
    assert days[4].discharge_m3s == 163.8 and days[-1].day == date(2026, 9, 23)


@pytest.mark.parametrize(
    "mutate, fragment",
    [
        (lambda b: b.pop("daily"), "no 'daily'"),
        (lambda b: b["daily"].update(time=[]), "'daily.time' is missing or empty"),
        (lambda b: b["daily"]["river_discharge"].pop(), "does not line up"),
        (lambda b: b["daily"].pop("river_discharge_mean"), "requested, not returned"),
        (lambda b: b["daily"]["time"].__setitem__(0, "not-a-date"), "is not a date"),
    ],
)
def test_parse_rejects_a_half_shaped_body_whole(mutate, fragment):
    body = _body()
    mutate(body)
    with pytest.raises(FloodError) as err:
        parse_flood(body)
    assert err.value.kind == "malformed" and fragment in str(err.value)


def test_derive_flags_the_fixture_pulse_and_records_what_was_divided_by_what():
    snap = OpenMeteoFloodProvider(client=_provider().client).discharge(-0.09, 34.77, now=NOW)
    risk = derive_flood_risk(snap, today=NOW.date())
    assert risk["flood_flag"] is True and risk["river_discharge_max"] == 163.8
    assert risk["river_discharge_max_day"] == "2026-09-21"
    assert risk["river_discharge_mean"] == pytest.approx(62.3286, abs=1e-3)
    assert risk["ratio"] == pytest.approx(2.628, abs=1e-3)
    assert "spec 7.3.1, literal" in risk["ratio_basis"] and risk["days_in_window"] == 7
    assert risk["thresholds"] == {"flood_ratio": 2.0, "horizon_days": 7}


@pytest.mark.parametrize(
    "discharge, mean, flag",
    [
        ([20.0] + [10.0] * 6, [10.0] * 7, True),   # exactly 2.0: the threshold is the first flagged value
        ([19.9] + [10.0] * 6, [10.0] * 7, False),  # 1.99
        ([10.0] * 7, [10.0] * 7, False),           # a river running steadily: no pulse
    ],
)
def test_the_threshold_edge(discharge, mean, flag):
    assert derive_flood_risk(_snap(discharge, mean))["flood_flag"] is flag


def test_nulls_are_skipped_never_read_as_zero_and_a_zero_mean_is_refused():
    risk = derive_flood_risk(_snap([None, 30.0, None], [10.0, None, 20.0]))
    assert risk["river_discharge_max"] == 30.0 and risk["river_discharge_mean"] == 15.0 and risk["ratio"] == 2.0
    zero = derive_flood_risk(_snap([5.0, 6.0], [0.0, 0.0]))
    assert zero["ratio"] is None and zero["flood_flag"] is False and "meaningless" in zero["reason"]
    empty = derive_flood_risk(_snap([None, None], [None, None]))
    assert empty["flood_flag"] is False and "no usable" in empty["reason"]


def test_the_window_is_the_horizon_from_today_and_thresholds_are_parameters():
    snap = _snap([100.0, 10.0, 10.0, 10.0], [10.0] * 4)
    assert derive_flood_risk(snap, today=date(2026, 9, 18))["flood_flag"] is False  # the peak day is behind us
    assert derive_flood_risk(snap)["ratio"] == 10.0 and derive_flood_risk(snap)["flood_flag"] is True
    assert derive_flood_risk(snap, thresholds=FloodThresholds(flood_ratio=10.5))["flood_flag"] is False
    assert derive_flood_risk(snap, thresholds=FloodThresholds(horizon_days=1))["days_in_window"] == 1


# ------------------------------------------------------------------------------ which sites


def test_only_riverine_sites_in_the_operators_regions_are_polled():
    assert [s.site_id for s in riverine_sites()] == [KISUMU]  # the catalogue's only riverine site
    assert [s.site_id for s in riverine_sites(["WNY"])] == [KISUMU]
    airtel_regions = list(get_settings("airtel").operator.regions)
    assert riverine_sites(airtel_regions) == []  # Safaricom's river is never written under Airtel


# ------------------------------------------------------------------------------ the poller


def test_poll_is_off_by_default_and_makes_no_request(tmp_db):
    settings, session = tmp_db
    calls: list[httpx.Request] = []
    result = poll(session, settings, provider=_provider(calls=calls), now=NOW)
    assert "skipped" in result.summary and "WEATHER_ENABLED" in result.summary
    assert calls == [] and _rows(session) == []


def test_poll_writes_one_row_per_riverine_site_per_day(tmp_db, monkeypatch, clean_hub):
    settings, session = tmp_db
    monkeypatch.setenv("WEATHER_ENABLED", "true")
    calls: list[httpx.Request] = []
    provider = _provider(calls=calls)
    result = poll(session, settings, provider=provider, now=NOW)
    assert result.summary.startswith("1/1 riverine sites read from GloFAS; FLOOD flag: SFC-WNY-HUB-KSM (WNY)")
    assert len(calls) == 1  # one site, one request: nothing non-riverine is ever asked about

    (row,) = _rows(session)
    assert (row.region_code, row.county, row.site_id, row.external_id) == ("WNY", "Kisumu", KISUMU, f"{KISUMU}:2026-09-17")
    assert (row.flood_flag, row.storm_flag, row.access_risk, row.stale, row.confidence, row.last_error) == (1, 0, 0, 0, 1.0, None)
    assert row.fetched_at == NOW and row.valid_until == NOW + ROW_VALID_FOR
    assert json.loads(row.payload_json)["daily"]["time"][0] == "2026-09-17"  # the body as received
    assert json.loads(row.derived_json)["site_name"] and json.loads(row.derived_json)["flood_flag"] is True

    poll(session, settings, provider=provider, now=NOW + timedelta(hours=3))  # same day: same row
    assert [r.id for r in _rows(session)] == [row.id]
    poll(session, settings, provider=provider, now=NOW + timedelta(days=1))  # next day: history
    assert [r.external_id for r in _rows(session)] == [f"{KISUMU}:2026-09-17", f"{KISUMU}:2026-09-18"]

    events = [r["payload"] for r in clean_hub.recent(10) if r["type"] == "external_signal.updated"]
    assert events[-1] == {"source": "GLOFAS", "region_code": "WNY", "site_id": KISUMU, "stale": False,
                          "storm_flag": False, "flood_flag": True, "error": None}


def test_the_regions_dashboard_reads_the_flag_and_lets_it_go_stale(tmp_db, monkeypatch):
    """The rows this lane writes, read by the dashboard it does not own."""
    settings, session = tmp_db
    monkeypatch.setenv("WEATHER_ENABLED", "true")
    poll(session, settings, provider=_provider(), now=NOW)

    wny = {r["region_code"]: r for r in dashboards.regions_dashboard(session, now=NOW + timedelta(hours=1))["regions"]}["WNY"]
    assert wny["signals"]["flood"] == {"available": True, "stale": False, "flag": True, "fetched_at": "2026-09-17T06:00:00Z"}
    assert wny["status"] == "ALERT"

    later = NOW + ROW_VALID_FOR + timedelta(minutes=1)  # the daily run never came
    wny = {r["region_code"]: r for r in dashboards.regions_dashboard(session, now=later)["regions"]}["WNY"]
    assert wny["signals"]["flood"]["stale"] is True and wny["status"] != "ALERT"  # a stale flag wakes nobody


@pytest.mark.parametrize(
    "kind, respond",
    [
        ("timeout", lambda req: (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=req))),
        ("http", lambda req: httpx.Response(500, json={"error": True, "reason": "upstream"})),
        ("malformed", lambda req: httpx.Response(200, content=b"{not json")),
        ("malformed", lambda req: httpx.Response(200, content=b"[1, 2, 3]")),
        ("tls", lambda req: (_ for _ in ()).throw(httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED]", request=req))),
        ("network", lambda req: (_ for _ in ()).throw(httpx.ConnectError("refused", request=req))),
    ],
    ids=["timeout", "500", "not-json", "not-an-object", "tls", "refused"],
)
def test_failures_are_fail_soft_and_keep_the_last_good_row(tmp_db, monkeypatch, kind, respond):
    settings, session = tmp_db
    monkeypatch.setenv("WEATHER_ENABLED", "true")
    poll(session, settings, provider=_provider(), now=NOW)
    (good,) = _rows(session)
    payload, derived = good.payload_json, good.derived_json

    result = poll(session, settings, provider=_provider(respond), now=NOW + timedelta(hours=2))  # must not raise
    tool = result.tools[0]
    assert tool["ok"] is False and tool["error_kind"] == kind and tool["flood_flag"] is True
    session.refresh(good)
    assert (good.payload_json, good.derived_json, good.flood_flag) == (payload, derived, 1)  # untouched
    assert good.last_error.startswith(kind) and good.stale == 0  # not stale while still valid ...
    assert len(_rows(session)) == 1

    poll(session, settings, provider=_provider(respond), now=NOW + ROW_VALID_FOR + timedelta(minutes=5))
    session.refresh(good)
    assert good.stale == 1 and is_stale(good, NOW + ROW_VALID_FOR + timedelta(minutes=5))  # ... stale once it expires


def test_an_oversized_body_is_abandoned_and_fail_soft(tmp_db, monkeypatch):
    settings, session = tmp_db
    monkeypatch.setenv("WEATHER_ENABLED", "true")
    monkeypatch.setattr(adapter, "MAX_BODY_BYTES", 500)
    result = poll(session, settings, provider=_provider(lambda req: httpx.Response(200, content=iter([b" " * 400] * 5))), now=NOW)
    assert result.tools[0]["error_kind"] == "oversize"
    result = poll(session, settings, provider=_provider(lambda req: httpx.Response(200, content=b" " * 5000)), now=NOW)
    assert result.tools[0]["error_kind"] == "oversize" and "declares" in result.tools[0]["error"]


def test_a_site_never_read_gets_a_flagless_expired_marker(tmp_db, monkeypatch):
    settings, session = tmp_db
    monkeypatch.setenv("WEATHER_ENABLED", "true")
    poll(session, settings, provider=_provider(lambda req: httpx.Response(503, content=b"down")), now=NOW)
    (marker,) = _rows(session)
    assert marker.external_id == f"{KISUMU}:unavailable" and marker.region_code == "WNY"
    assert (marker.flood_flag, marker.confidence, marker.stale, marker.derived_json) == (0, 0.0, 1, None)
    assert marker.valid_until == NOW and "HTTP 503" in marker.last_error
    assert latest_site_row(session, "safaricom", KISUMU) is None  # a marker is not a reading


def test_a_forecast_that_does_not_cover_today_is_a_failure_not_a_row(tmp_db, monkeypatch):
    settings, session = tmp_db
    monkeypatch.setenv("WEATHER_ENABLED", "true")
    result = poll(session, settings, provider=_provider(), now=NOW + timedelta(days=30))
    assert result.tools[0]["error_kind"] == "malformed" and "does not cover today" in result.tools[0]["error"]
    assert latest_site_row(session, "safaricom", KISUMU) is None


def test_an_unexpected_bug_is_still_fail_soft(tmp_db, monkeypatch):
    settings, session = tmp_db
    monkeypatch.setenv("WEATHER_ENABLED", "true")
    monkeypatch.setattr(poller, "derive_flood_risk", lambda *a, **k: 1 / 0)
    result = poll(session, settings, provider=_provider(), now=NOW)
    assert result.tools[0]["error_kind"] == "unexpected" and "ZeroDivisionError" in result.tools[0]["error"]


def test_f18_the_catalogue_is_safaricoms_so_airtel_never_writes_its_rivers(tmp_db, monkeypatch):
    """F18: region codes are not ownership — both profiles define CST — so scoping by region
    alone would write a Safaricom Coast tower under Airtel the day it was marked riverine."""
    settings, session = tmp_db
    monkeypatch.setenv("WEATHER_ENABLED", "true")
    airtel = get_settings("airtel").model_copy(update={"database_url": settings.database_url})
    assert "CST" in airtel.operator.regions and "CST" in settings.operator.regions  # the shared code
    kisumu = next(s for s in site_catalogue.all_sites() if s.site_id == KISUMU)
    coast = replace(kisumu, site_id="SFC-CST-HUB-MSA", region_code="CST", county="Mombasa", lat=-4.04, lon=39.67)
    monkeypatch.setattr(poller, "all_sites", lambda: [kisumu, coast])  # a riverine Coast site in the catalogue
    assert [s.site_id for s in riverine_sites(list(airtel.operator.regions), [kisumu, coast])] == ["SFC-CST-HUB-MSA"]
    calls: list[httpx.Request] = []
    result = poll(session, airtel, provider=_provider(calls=calls), now=NOW)
    assert calls == [] and _rows(session, "airtel") == []
    assert "belongs to safaricom" in result.summary and result.tools[0]["skipped"] is True


def test_the_site_seed_operator_is_the_seed_files_owner():
    assert site_catalogue.SEED_PATH.name == f"{SITE_SEED_OPERATOR}_sites.json"


def test_operators_never_see_each_others_flood_rows(tmp_db, monkeypatch):
    settings, session = tmp_db
    monkeypatch.setenv("WEATHER_ENABLED", "true")
    # Airtel's row for the same site and region, written as the poller would (a future Airtel
    # catalogue): the sharpest case, same site_id, same region code, same day.
    session.add(ExternalSignalRow(
        id=new_id(), operator_id="airtel", source="GLOFAS", source_url="mock://", region_code="WNY",
        county="Kisumu", site_id=KISUMU, fetched_at=NOW, valid_from=NOW, valid_until=NOW + ROW_VALID_FOR,
        stale=0, confidence=1.0, storm_flag=0, flood_flag=0, planned_power=0, access_risk=0,
        payload_json="{}", derived_json='{"kind":"river_discharge"}', external_id=f"{KISUMU}:2026-09-17", created_at=NOW,
    ))
    session.commit()
    assert latest_site_row(session, "safaricom", KISUMU) is None
    assert flood_reading(session, "safaricom", "WNY", NOW) is None

    # A Safaricom failure must annotate Safaricom's rows only — here, a Safaricom marker.
    poll(session, settings, provider=_provider(lambda req: httpx.Response(500)), now=NOW)
    airtel_row = latest_site_row(session, "airtel", KISUMU)
    assert airtel_row.last_error is None and airtel_row.flood_flag == 0

    poll(session, settings, provider=_provider(), now=NOW + timedelta(minutes=5))
    saf = flood_reading(session, "safaricom", "WNY", NOW + timedelta(minutes=5))
    assert saf["flag"] is True and [s["site_id"] for s in saf["sites"]] == [KISUMU]
    assert {r.operator_id for r in list_signals(session, "safaricom", source="GLOFAS", now=NOW)} == {"safaricom"}
    assert {r.operator_id for r in list_signals(session, "airtel", source="GLOFAS", now=NOW)} == {"airtel"}


class _Clock:
    def __init__(self, step: float) -> None:
        self.t, self.step = 0.0, step

    def __call__(self) -> float:
        self.t += self.step
        return self.t


def test_f06_a_slow_drip_is_abandoned_at_the_total_deadline(tmp_db, monkeypatch):
    settings, session = tmp_db
    monkeypatch.setenv("WEATHER_ENABLED", "true")
    monkeypatch.setattr(adapter, "_clock", _Clock(step=4.0))
    result = poll(session, settings, provider=_provider(lambda req: httpx.Response(200, content=iter([b" "] * 1000))), now=NOW)
    tool = result.tools[0]
    assert tool["ok"] is False and tool["error_kind"] == "timeout" and "total deadline" in tool["error"]


def test_f15_identity_is_requested_and_compression_is_refused_unread(tmp_db, monkeypatch):
    import gzip

    settings, session = tmp_db
    monkeypatch.setenv("WEATHER_ENABLED", "true")
    consumed: list[bool] = []

    def body():
        consumed.append(True)
        yield gzip.compress(FIXTURE.read_bytes())

    calls: list[httpx.Request] = []
    result = poll(session, settings, provider=_provider(
        lambda req: httpx.Response(200, content=body(), headers={"Content-Encoding": "gzip"}), calls=calls), now=NOW)
    assert calls[0].headers["Accept-Encoding"] == "identity"
    assert result.tools[0]["error_kind"] == "malformed" and "Content-Encoding" in result.tools[0]["error"]
    assert consumed == []


def _two_sites(monkeypatch):
    """Kisumu (flooding, per the fixture) plus a second WNY riverine site whose id sorts FIRST
    — the arrangement in which a newest-row tie-break used to hide Kisumu's flag."""
    kisumu = next(s for s in site_catalogue.all_sites() if s.site_id == KISUMU)
    other = replace(kisumu, site_id="SFC-WNY-AAA", lat=0.50, lon=34.50)
    calm = _body()
    calm["daily"]["river_discharge"] = [40.0] * 7
    calm["daily"]["river_discharge_mean"] = [40.0] * 7
    return kisumu, other, json.dumps(calm).encode()


def _route(calm_body: bytes, *, other_fails: bool = False):
    raw = FIXTURE.read_bytes()

    def respond(request):
        if "latitude=0.5000" in str(request.url):
            return httpx.Response(503, content=b"down") if other_fails else httpx.Response(200, content=calm_body)
        return httpx.Response(200, content=raw)

    return respond


@pytest.mark.parametrize("other_fails", [False, True], ids=["other-site-calm", "other-site-fails"])
def test_f09_one_calm_or_failing_site_never_hides_another_sites_flood(tmp_db, monkeypatch, other_fails):
    """F09: every site's row shares the run's timestamp, so "the newest GLOFAS row in the
    region" was a tie-break, and SFC-WNY-AAA (calm, or its failure marker) won it."""
    settings, session = tmp_db
    monkeypatch.setenv("WEATHER_ENABLED", "true")
    kisumu, other, calm_body = _two_sites(monkeypatch)
    poll(session, settings, provider=_provider(_route(calm_body, other_fails=other_fails)), now=NOW, sites=[kisumu, other])
    state = flood_region_state(session, "safaricom", "WNY", NOW + timedelta(minutes=5))
    assert state["flag"] is True and state["stale"] is False and state["available"] is True
    wny = {r["region_code"]: r for r in dashboards.regions_dashboard(session, now=NOW + timedelta(minutes=5))["regions"]}["WNY"]
    assert wny["signals"]["flood"]["flag"] is True and wny["status"] == "ALERT"


def test_f09_a_region_is_fresh_if_any_site_is_and_flags_only_on_fresh_readings(tmp_db, monkeypatch):
    settings, session = tmp_db
    monkeypatch.setenv("WEATHER_ENABLED", "true")
    kisumu, other, calm_body = _two_sites(monkeypatch)
    poll(session, settings, provider=_provider(_route(calm_body)), now=NOW, sites=[kisumu, other])
    # Next day only the calm site is read: Kisumu's flag is now stale and must not flag the region.
    poll(session, settings, provider=_provider(_route(calm_body)), now=NOW + timedelta(days=1, hours=3), sites=[other])
    state = flood_region_state(session, "safaricom", "WNY", NOW + timedelta(days=1, hours=3))
    assert state["stale"] is False and state["flag"] is False
    assert {s["site_id"]: s["stale"] for s in state["sites"]} == {"SFC-WNY-AAA": False, KISUMU: True}
    # And a region with only failure markers is available, stale, and has no flag at all.
    poll(session, settings, provider=_provider(lambda req: httpx.Response(500)), now=NOW, sites=[replace(kisumu, region_code="CST", site_id="SFC-CST-X")])
    cst = flood_region_state(session, "safaricom", "CST", NOW)
    assert (cst["available"], cst["stale"], cst["flag"]) == (True, True, None)


@pytest.fixture()
def loopback_only(monkeypatch):
    """Re-admit 127.0.0.1 only, for the W02 test: the header-phase deadline lives below httpx,
    in socket reads, so MockTransport cannot exercise it. Every other host still fails."""

    def check(host):
        if str(host) not in {"127.0.0.1", "localhost"}:
            raise AssertionError(f"a flood test tried to reach {host!r}")

    def connect(self, address):
        check(address[0])
        return _REAL_CONNECT(self, address)

    def create_connection(address, *args, **kwargs):
        check(address[0])
        return _REAL_CREATE_CONNECTION(address, *args, **kwargs)

    def getaddrinfo(host, *args, **kwargs):
        check(host)
        return _REAL_GETADDRINFO(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


def _header_drip_server(gap: float) -> int:
    """A one-shot loopback server that drips a 200's HEADERS a byte at a time (~6 s in all)."""
    response = b"HTTP/1.1 200 OK" + bytes([13, 10]) + b"X-Pad: " + b"a" * 40 + bytes([13, 10]) + b"Content-Length: 2" + bytes([13, 10, 13, 10]) + b"{}"
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def run():
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        try:
            data = b""
            while bytes([13, 10, 13, 10]) not in data:
                data += conn.recv(4096)
            for byte in response:
                conn.sendall(bytes([byte]))
                time.sleep(gap)
        except OSError:
            pass  # the client's watchdog shut the connection
        finally:
            conn.close()
            srv.close()

    threading.Thread(target=run, daemon=True).start()
    return port


def test_w02_the_flood_deadline_covers_the_header_phase_over_a_real_socket(loopback_only):
    """W02: _get_json checked the deadline only in the body loop; a server dripping its headers
    ran ~10 s against a 1 s budget. The shared DeadlineWatchdog now ends it at the deadline."""
    port = _header_drip_server(gap=0.07)
    client = httpx.Client(timeout=httpx.Timeout(1.0))
    started = time.monotonic()
    with pytest.raises(FloodError) as err:
        adapter._get_json(client, f"http://127.0.0.1:{port}/v1/flood", timeout_s=0.6)
    elapsed = time.monotonic() - started
    assert err.value.kind == "timeout" and "total deadline" in str(err.value)
    assert elapsed < 2.5, f"{elapsed:.2f}s against a 0.6 s budget"


TLS_CERT = Path(__file__).resolve().parents[1] / "fixtures" / "tls" / "test_only_cert.pem"
TLS_KEY = Path(__file__).resolve().parents[1] / "fixtures" / "tls" / "test_only_key.pem"
_CRLF = bytes([13, 10])


def _tls_server(payload: bytes, gap: float, *, silent: bool = False) -> int:
    """A one-shot loopback HTTPS server on the committed TEST-ONLY certificate (see its README)."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(TLS_CERT), str(TLS_KEY))

    def run():
        conn = None
        try:
            raw, _ = listener.accept()
            conn = context.wrap_socket(raw, server_side=True)
            data = b""
            while _CRLF + _CRLF not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                data += chunk
            if silent:
                time.sleep(8.0)
                return
            for byte in payload:
                conn.sendall(bytes([byte]))
                time.sleep(gap)
        except (OSError, ssl.SSLError):
            pass  # the client's watchdog shut the connection
        finally:
            if conn is not None:
                conn.close()
            listener.close()

    threading.Thread(target=run, daemon=True).start()
    return listener.getsockname()[1]


def _slow_proxy(upstream_port: int, gap: float) -> int:
    """Forwards client bytes at once and server bytes one every ``gap`` s: a dripped TLS handshake."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def run():
        try:
            client, _ = listener.accept()
            upstream = socket.create_connection(("127.0.0.1", upstream_port))

            def forward_up():
                try:
                    while chunk := client.recv(4096):
                        upstream.sendall(chunk)
                except OSError:
                    pass

            threading.Thread(target=forward_up, daemon=True).start()
            while chunk := upstream.recv(4096):
                for byte in chunk:
                    client.sendall(bytes([byte]))
                    time.sleep(gap)
        except OSError:
            pass
        finally:
            listener.close()

    threading.Thread(target=run, daemon=True).start()
    return listener.getsockname()[1]


_JSON_HEADERS = b"HTTP/1.1 200 OK" + _CRLF + b"X-Pad: " + b"z" * 80 + _CRLF + b"Content-Length: 2" + _CRLF + _CRLF + b"{}"
_STORM = (b"HTTP/1.1 100 Continue" + _CRLF + _CRLF) * 80 + b"HTTP/1.1 200 OK" + _CRLF + b"Content-Length: 2" + _CRLF + _CRLF + b"{}"
_OK = b"HTTP/1.1 200 OK" + _CRLF + b"Content-Length: 2" + _CRLF + _CRLF + b"{}"

TLS_CASES = {
    "tls-headers-dripped": (lambda: _tls_server(_JSON_HEADERS, 0.05), 1.0),
    "tls-100-continue-storm": (lambda: _tls_server(_STORM, 0.002), 1.0),
    # Regression guard: CPython bounds a handshake by the socket timeout as one total deadline.
    "tls-handshake-dripped": (lambda: _slow_proxy(_tls_server(_OK, 0.0), 0.005), 1.0),
    # Regression guard: already bounded before this fix by the round-3 per-wait cap.
    "tls-silent-after-request": (lambda: _tls_server(b"", 0.0, silent=True), 6.0),
}


@pytest.mark.parametrize("label", sorted(TLS_CASES))
def test_w02_tls_the_flood_deadline_holds_over_https(loopback_only, label):
    """W02-TLS for the flood adapter: the production host (DEFAULT_FLOOD_BASE) is https, and the
    watchdog's recorded plain socket was detached by the TLS wrap, so nothing bounded the drip."""
    make_server, per_read = TLS_CASES[label]
    port = make_server()
    client = httpx.Client(timeout=httpx.Timeout(per_read), verify=ssl.create_default_context(cafile=str(TLS_CERT)))
    started = time.monotonic()
    with pytest.raises(FloodError) as err:
        adapter._get_json(client, f"https://127.0.0.1:{port}/v1/flood", timeout_s=0.6)
    elapsed = time.monotonic() - started
    assert err.value.kind == "timeout" and "total deadline" in str(err.value), str(err.value)
    assert elapsed < 2.5, f"{label}: {elapsed:.2f}s against a 0.6 s budget"


def test_w02_tls_a_normal_https_flood_fetch_still_works(loopback_only):
    body = FIXTURE.read_bytes()
    ok = b"HTTP/1.1 200 OK" + _CRLF + b"Content-Length: " + str(len(body)).encode() + _CRLF + _CRLF + body
    port = _tls_server(ok, 0.0)
    client = httpx.Client(timeout=httpx.Timeout(5.0), verify=ssl.create_default_context(cafile=str(TLS_CERT)))
    snapshot = OpenMeteoFloodProvider(f"https://127.0.0.1:{port}", client=client).discharge(-0.09, 34.77, now=NOW)
    assert len(snapshot.days) == 7
    alive = [t for t in threading.enumerate() if t.name == "noc-deadline-watchdog" and t.is_alive()]
    assert alive == []  # the watchdog's timer thread was joined when the exchange ended


def test_w02_flood_requests_open_their_own_traced_connection():
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=FIXTURE.read_bytes())

    OpenMeteoFloodProvider(client=httpx.Client(transport=httpx.MockTransport(handler))).discharge(-0.09, 34.77, now=NOW)
    assert seen[0].headers["Connection"] == "close" and callable(seen[0].extensions.get("trace"))


# ------------------------------------------------------------------------------ the scheduler card


def test_job_card_ships_disabled_under_the_weather_agents_flag():
    assert (FLOOD_JOB.name, FLOOD_JOB.interval_s, FLOOD_JOB.enabled_env) == ("flood_daily", 86400, "WEATHER_ENABLED")
    assert (FLOOD_JOB.agent, FLOOD_JOB.graph_name, FLOOD_JOB.default_enabled) == ("WeatherRiskAgent", "flood", False)
    assert FLOOD_JOB.fn is poll and job_enabled(FLOOD_JOB) is False


def test_the_job_re_checks_its_own_flag_even_when_run_directly(tmp_db, monkeypatch):
    settings, session = tmp_db
    monkeypatch.setattr(poller, "provider_from_env", lambda: (_ for _ in ()).throw(AssertionError("provider built while disabled")))
    outcome = run_job(FLOOD_JOB, settings)
    assert outcome.status == "SUCCEEDED" and "skipped" in outcome.summary and _rows(session) == []


def test_run_job_records_a_succeeded_run_when_the_flood_api_is_down(tmp_db, monkeypatch, clean_hub):
    settings, session = tmp_db
    monkeypatch.setenv("WEATHER_ENABLED", "true")

    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    monkeypatch.setattr(poller, "provider_from_env", lambda: _provider(timeout))
    outcome = run_job(FLOOD_JOB, settings)
    assert outcome.status == "SUCCEEDED" and outcome.error is None and outcome.circuit_open is False
    assert "1 failed (timeout)" in outcome.summary
    assert read_state(session, "flood_daily").last_status == "SUCCEEDED"
    assert not [r for r in clean_hub.recent(20) if r["type"] == "scheduler.job_failed"]


def test_derived_block_is_a_copy_not_the_fixture():
    """Guard against a parse that aliases the provider body: mutating the input after the
    fact must not change what was derived (the stored payload is a separate JSON dump)."""
    body = _body()
    days = parse_flood(copy.deepcopy(body))
    body["daily"]["river_discharge"][4] = 0.0
    assert days[4].discharge_m3s == 163.8
