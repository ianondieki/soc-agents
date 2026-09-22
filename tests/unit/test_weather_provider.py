"""Phase 3 weather signal core (spec §5.3.13, §7.3): the ``external_signals`` cache, the
``WeatherProvider`` over Open-Meteo / MET Norway, and the ``weather_regions`` poller.

**Zero network.** Every provider call in this file goes through ``httpx.MockTransport`` fed
from ``tests/fixtures/weather/*.json`` — recorded from the providers' *documented* response
shapes, not captured live (each file says so in ``_provenance``; see the README there). An
autouse fixture additionally breaks ``socket.connect`` / ``getaddrinfo``, so a test that
tried to reach a real API would fail here for the right reason, not for the TLS reason this
machine has (docs/RUNBOOK.md §3). ``respx`` (spec §7.3.7) is not installed in the target
interpreter; ``MockTransport`` is httpx's own equivalent and needs no extra package.

What is pinned:

* both parsers, including UTC conversion (Open-Meteo local time + ``utc_offset_seconds``;
  MET ``Z`` stamps) and unit conversion (MET m/s → km/h);
* the storm rule at its edges (20 mm, 60 km/h, 1500 J/kg, WMO 95/96/99, MET ``*thunder*``);
* fail-soft for a timeout, a 500, malformed JSON, a TLS failure, a forecast that does not
  cover *now*, and a misconfigured provider — none raises, the last good row keeps its
  payload, ``last_error`` says why, ``stale`` flips only once ``valid_until`` passes;
* staleness computed from ``fetched_at`` / ``valid_until`` against *now*, not read off disk;
* ``WEATHER_ENABLED`` off by default → no request, no row;
* the job runs through the real ``run_job`` as SUCCEEDED with the circuit untouched;
* the table's shape (operator_id, unique key, index) and the schema version stamp;
* MET's terms: no User-Agent → no request; ``If-Modified-Since`` and ``Expires`` honoured.
"""

from __future__ import annotations

import json
import socket
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from noc_agents.adapters.weather import (
    MET_NORWAY,
    OPEN_METEO,
    ForecastSnapshot,
    HourlyPoint,
    MetNorwayProvider,
    OpenMeteoProvider,
    WeatherError,
    WeatherThresholds,
    derive_weather_risk,
    parse_met_norway,
    parse_open_meteo,
    provider_from_env,
    resolve_provider_name,
)
from noc_agents.db.migrate import SCHEMA_VERSION
from noc_agents.db.models import ExternalSignalRow
from noc_agents.pollers import weather as poller
from noc_agents.pollers.weather import (
    ROW_VALID_FOR,
    WEATHER_JOB,
    is_stale,
    latest_error,
    latest_signal,
    poll,
    region_centroids,
    staleness,
    weather_risk_for_region,
)
from noc_agents.realtime.hub import hub
from noc_agents.scheduler.loop import read_state, run_job

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "weather"
OPEN_METEO_FIXTURE = FIXTURES / "open_meteo_westlands_nbi_w.json"
MET_FIXTURE = FIXTURES / "met_norway_kisumu_wny.json"

# The Open-Meteo fixture is Westlands (SFC-NBIW-HUB-WLD, -1.27/36.81): storm 14:00-20:00 EAT on
# 2026-09-17, i.e. 11:00-17:00 UTC. The MET fixture is Kisumu (SFC-WNY-HUB-KSM, -0.09/34.77):
# storm 15:00-19:00 UTC the same day.
STORM_NOW_OM = datetime(2026, 9, 17, 11, 0)  # 14:00 EAT
CALM_NOW_OM = datetime(2026, 9, 16, 21, 0)  # 00:00 EAT, first hour of the fixture
STORM_NOW_MET = datetime(2026, 9, 17, 15, 0)
NBI_W = {"NBI_W": (-1.27, 36.81)}
WNY = {"WNY": (-0.09, 34.77)}
OPERATOR = "safaricom"


# ------------------------------------------------------------------------------ fixtures


@pytest.fixture(autouse=True)
def no_sockets(monkeypatch):
    """Any attempt to open a real connection fails loudly. MockTransport never gets here."""

    def boom(*args, **kwargs):
        raise AssertionError("a weather test tried to open a network socket")

    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket.socket, "connect_ex", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)


@pytest.fixture(autouse=True)
def weather_env(monkeypatch):
    """Start every test with the feature off and no provider configuration leaking in."""
    for key in ("WEATHER_ENABLED", "WEATHER_PROVIDER", "WEATHER_API_BASE", "WEATHER_API_KEY", "MET_NO_USER_AGENT", "NOC_USE_TRUSTSTORE"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture()
def clean_hub():
    hub._history.clear()
    yield hub
    hub._history.clear()


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _fixture_handler(path: Path, *, status: int = 200, headers: dict | None = None, calls: list | None = None):
    body = path.read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        return httpx.Response(status, content=body, headers={"content-type": "application/json", **(headers or {})})

    return handler


def _open_meteo(handler=None, calls=None) -> OpenMeteoProvider:
    return OpenMeteoProvider(client=_client(handler or _fixture_handler(OPEN_METEO_FIXTURE, calls=calls)))


def _snap(points: list[HourlyPoint], *, source: str = OPEN_METEO, fetched_at: datetime = STORM_NOW_OM) -> ForecastSnapshot:
    return ForecastSnapshot(source=source, source_url="mock://", latitude=0.0, longitude=0.0, fetched_at=fetched_at, hours=tuple(points))


def _hours(start: datetime, n: int = 6, **fields) -> list[HourlyPoint]:
    return [HourlyPoint(time=start + timedelta(hours=i), **fields) for i in range(n)]


def _rows(session, region: str | None = None) -> list[ExternalSignalRow]:
    stmt = select(ExternalSignalRow).where(ExternalSignalRow.operator_id == OPERATOR)
    if region:
        stmt = stmt.where(ExternalSignalRow.region_code == region)
    return list(session.scalars(stmt.order_by(ExternalSignalRow.created_at)).all())


# ------------------------------------------------------------------------------ fixtures are honest


def test_fixtures_say_they_were_constructed_not_captured():
    for path in (OPEN_METEO_FIXTURE, MET_FIXTURE):
        prov = json.loads(path.read_text(encoding="utf-8"))["_provenance"]
        assert prov["captured_live"] is False
        assert "NOT a live capture" in prov["how"]
        assert prov["site"]["site_id"] in {"SFC-NBIW-HUB-WLD", "SFC-WNY-HUB-KSM"}


def test_no_module_level_anthropic_or_mcp_imports():
    root = Path(__file__).resolve().parents[2] / "src" / "noc_agents"
    for rel in ("adapters/weather.py", "pollers/weather.py", "pollers/__init__.py"):
        for line in (root / rel).read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            assert not stripped.startswith(("import anthropic", "from anthropic", "import mcp", "from mcp")), (rel, line)


# ------------------------------------------------------------------------------ parsing: Open-Meteo


def test_parse_open_meteo_fixture_converts_local_time_to_naive_utc():
    points = parse_open_meteo(json.loads(OPEN_METEO_FIXTURE.read_text(encoding="utf-8")))
    assert len(points) == 48
    assert points[0].time == datetime(2026, 9, 16, 21, 0) and points[0].time.tzinfo is None  # 00:00 EAT
    assert points[1].time - points[0].time == timedelta(hours=1)
    peak = next(p for p in points if p.time == datetime(2026, 9, 17, 13, 0))  # 16:00 EAT
    assert (peak.precip_mm, peak.precip_prob_pct, peak.wind_kmh, peak.gust_kmh, peak.cape_jkg, peak.weather_code) == (7.9, 90.0, 22.7, 68.0, 1760.0, 96)
    assert peak.thunder is True
    calm = points[0]
    assert (calm.precip_mm, calm.gust_kmh, calm.weather_code, calm.thunder) == (0.0, 16.2, 1, False)


@pytest.mark.parametrize(
    "mutate, fragment",
    [
        (lambda b: b.pop("hourly"), "no 'hourly'"),
        (lambda b: b["hourly"].update(time=[]), "missing or empty"),
        (lambda b: b["hourly"].update(precipitation=[1.0, 2.0]), "does not line up"),
        (lambda b: b["hourly"]["time"].__setitem__(0, "not-a-time"), "not ISO 8601"),
    ],
)
def test_parse_open_meteo_rejects_a_half_shaped_body(mutate, fragment):
    body = json.loads(OPEN_METEO_FIXTURE.read_text(encoding="utf-8"))
    mutate(body)
    with pytest.raises(WeatherError) as info:
        parse_open_meteo(body)
    assert info.value.kind == "malformed" and fragment in str(info.value)


def test_parse_open_meteo_tolerates_a_missing_variable_and_nulls():
    body = json.loads(OPEN_METEO_FIXTURE.read_text(encoding="utf-8"))
    del body["hourly"]["cape"]  # a model without CAPE
    body["hourly"]["wind_gusts_10m"][3] = None
    points = parse_open_meteo(body)
    assert all(p.cape_jkg is None for p in points)
    assert points[3].gust_kmh is None and points[4].gust_kmh == 16.2


# ------------------------------------------------------------------------------ parsing: MET Norway


def test_parse_met_norway_fixture_converts_units_and_flags_thunder():
    points = parse_met_norway(json.loads(MET_FIXTURE.read_text(encoding="utf-8")))
    assert len(points) == 52  # 48 hourly + 4 six-hourly
    assert points[0].time == datetime(2026, 9, 16, 21, 0) and points[0].time.tzinfo is None
    assert points[0].wind_kmh == pytest.approx(2.8 * 3.6) and points[0].gust_kmh == pytest.approx(5.1 * 3.6)
    assert (points[0].cape_jkg, points[0].weather_code, points[0].thunder) == (None, None, False)
    peak = next(p for p in points if p.time == datetime(2026, 9, 17, 16, 0))
    assert peak.precip_mm == 9.6 and peak.precip_prob_pct == 80.0 and peak.gust_kmh == pytest.approx(65.16)
    assert peak.thunder is True  # symbol_code "heavyrainandthunder"
    six_hourly = points[48]
    assert six_hourly.precip_mm is None and six_hourly.wind_kmh == pytest.approx(3.1 * 3.6)  # never inflates a 6 h sum


@pytest.mark.parametrize(
    "mutate, fragment",
    [
        (lambda b: b.pop("properties"), "no 'properties'"),
        (lambda b: b["properties"].update(timeseries=[]), "missing or empty"),
        (lambda b: b["properties"]["timeseries"][0].update(time="soon"), "not a timestamp"),
    ],
)
def test_parse_met_norway_rejects_a_half_shaped_body(mutate, fragment):
    body = json.loads(MET_FIXTURE.read_text(encoding="utf-8"))
    mutate(body)
    with pytest.raises(WeatherError) as info:
        parse_met_norway(body)
    assert info.value.kind == "malformed" and fragment in str(info.value)


# ------------------------------------------------------------------------------ derivation


def test_derive_flags_the_storm_window_and_not_the_calm_one():
    snapshot = _open_meteo().forecast(-1.27, 36.81, now=STORM_NOW_OM)
    storm = derive_weather_risk(snapshot, now=STORM_NOW_OM)
    assert storm["hours_in_window"] == 6 and storm["horizon_hours"] == 6
    assert storm["rain_mm_next_6h"] == pytest.approx(22.4)
    assert (storm["gust_kmh_max"], storm["cape_max_jkg"], storm["weather_code_max"], storm["precip_prob_max_pct"]) == (68.0, 1820.0, 96, 90.0)
    assert storm["thunder"] is True and storm["storm_flag"] is True
    assert len(storm["storm_reasons"]) == 4  # rain, gusts, CAPE and thunder all fire in this window
    assert storm["flood_flag"] is False and storm["cap_alert_ids"] == [] and storm["stale"] is False
    assert storm["source"] == OPEN_METEO and storm["fetched_at"] == "2026-09-17T11:00:00Z"
    assert storm["window_from"] == "2026-09-17T11:00:00Z" and storm["window_until"] == "2026-09-17T17:00:00Z"

    calm = derive_weather_risk(snapshot, now=CALM_NOW_OM)
    assert calm["hours_in_window"] == 6 and calm["rain_mm_next_6h"] == 0.0
    assert calm["storm_flag"] is False and calm["storm_reasons"] == [] and calm["thunder"] is False


def test_derive_met_snapshot_flags_on_rain_gust_and_thunder_without_cape_or_codes():
    client = _client(_fixture_handler(MET_FIXTURE))
    snapshot = MetNorwayProvider(user_agent="NOC-test/0 (test@example.invalid)", client=client).forecast(-0.09, 34.77, now=STORM_NOW_MET)
    risk = derive_weather_risk(snapshot, now=STORM_NOW_MET)
    assert risk["rain_mm_next_6h"] == pytest.approx(23.6) and risk["gust_kmh_max"] == pytest.approx(65.16)
    assert risk["cape_max_jkg"] is None and risk["weather_code_max"] is None
    assert risk["thunder"] is True and risk["storm_flag"] is True
    assert any(r.startswith("thunderstorm forecast") for r in risk["storm_reasons"])


@pytest.mark.parametrize(
    "fields, expect",
    [
        ({"precip_mm": 20.0 / 6}, True),  # sums to exactly 20.0 over six hours: the threshold is the first flagged value
        ({"precip_mm": 19.99 / 6}, False),
        ({"gust_kmh": 60.0}, True),
        ({"gust_kmh": 59.9}, False),
        ({"cape_jkg": 1500.0}, True),
        ({"cape_jkg": 1499.9}, False),
        ({"weather_code": 95, "thunder": True}, True),
        ({"weather_code": 80}, False),  # rain showers, no thunder
        ({}, False),  # nothing known: nothing flagged
    ],
)
def test_threshold_edges(fields, expect):
    risk = derive_weather_risk(_snap(_hours(STORM_NOW_OM, **fields)), now=STORM_NOW_OM)
    assert risk["storm_flag"] is expect, risk["storm_reasons"]


def test_thresholds_are_parameters_not_constants():
    points = _hours(STORM_NOW_OM, precip_mm=2.0)  # 12 mm / 6 h
    assert derive_weather_risk(_snap(points), now=STORM_NOW_OM)["storm_flag"] is False
    tuned = WeatherThresholds(storm_rain_mm=10.0)
    risk = derive_weather_risk(_snap(points), now=STORM_NOW_OM, thresholds=tuned)
    assert risk["storm_flag"] is True and risk["thresholds"]["storm_rain_mm"] == 10.0


def test_window_includes_the_hour_that_is_running_and_stops_at_the_horizon():
    snapshot = _open_meteo().forecast(-1.27, 36.81, now=STORM_NOW_OM)
    half_past = derive_weather_risk(snapshot, now=STORM_NOW_OM + timedelta(minutes=30))
    assert half_past["hours_in_window"] == 7  # 11:00 (still running) .. 17:00 (starts before 17:30)
    beyond = derive_weather_risk(snapshot, now=datetime(2026, 9, 20, 0, 0))
    assert beyond["hours_in_window"] == 0 and beyond["storm_flag"] is False and beyond["rain_mm_next_6h"] is None


# ------------------------------------------------------------------------------ providers


def test_open_meteo_builds_the_documented_request_and_redacts_the_key():
    provider = OpenMeteoProvider(api_key="secret-key")
    url = provider.forecast_url(-1.27, 36.81, hours=48)
    assert url.startswith("https://api.open-meteo.com/v1/forecast?latitude=-1.2700&longitude=36.8100&hourly=")
    assert "precipitation,precipitation_probability,wind_speed_10m,wind_gusts_10m,cape,weather_code" in url
    assert "forecast_days=2" in url and "timezone=Africa%2FNairobi" in url
    assert "apikey=***" in url and "secret-key" not in url
    assert "apikey=secret-key" in provider.forecast_url(-1.27, 36.81, redact=False)
    assert "apikey" not in OpenMeteoProvider().forecast_url(-1.27, 36.81)
    assert "forecast_days=1" in OpenMeteoProvider().forecast_url(0, 0, hours=6)


def test_open_meteo_provider_parses_through_a_mock_transport():
    calls: list[httpx.Request] = []
    snapshot = _open_meteo(calls=calls).forecast(-1.27, 36.81, now=STORM_NOW_OM)
    assert len(calls) == 1 and calls[0].url.host == "api.open-meteo.com" and calls[0].url.params["latitude"] == "-1.2700"
    assert snapshot.source == OPEN_METEO and (snapshot.latitude, snapshot.longitude) == (-1.25, 36.75)  # grid point, not the request
    assert len(snapshot.hours) == 48 and snapshot.fetched_at == STORM_NOW_OM and snapshot.from_cache is False
    assert snapshot.raw["timezone"] == "Africa/Nairobi"


def test_open_meteo_error_body_reason_is_surfaced():
    def handler(request):
        return httpx.Response(400, json={"error": True, "reason": "Latitude must be in range of -90 to 90°"})

    with pytest.raises(WeatherError) as info:
        _open_meteo(handler).forecast(91, 0)
    assert info.value.kind == "http" and info.value.status == 400 and "Latitude must be" in str(info.value)


def test_met_norway_refuses_to_call_without_an_identifying_user_agent():
    calls: list[httpx.Request] = []
    provider = MetNorwayProvider(user_agent="", client=_client(_fixture_handler(MET_FIXTURE, calls=calls)))
    with pytest.raises(WeatherError) as info:
        provider.forecast(-0.09, 34.77)
    assert info.value.kind == "config" and "MET_NO_USER_AGENT" in str(info.value) and "TermsOfService" in str(info.value)
    assert calls == []  # the terms are honoured before a byte leaves


def test_met_norway_sends_user_agent_and_honours_if_modified_since_and_expires():
    ua = "KenyaNOCMissionControl/2.0 (noc@example.invalid)"
    calls: list[httpx.Request] = []
    body = MET_FIXTURE.read_bytes()
    state = {"mode": "full"}

    def handler(request):
        calls.append(request)
        if state["mode"] == "304":
            return httpx.Response(304, headers={"Expires": "Wed, 16 Sep 2026 22:30:00 GMT"})
        return httpx.Response(
            200,
            content=body,
            headers={"content-type": "application/json", "Last-Modified": "Wed, 16 Sep 2026 20:31:07 GMT", "Expires": "Wed, 16 Sep 2026 21:05:00 GMT"},
        )

    provider = MetNorwayProvider(user_agent=ua, client=_client(handler))
    t0 = datetime(2026, 9, 16, 21, 0)
    first = provider.forecast(-0.09, 34.77, now=t0)
    assert calls[0].headers["User-Agent"] == ua and "If-Modified-Since" not in calls[0].headers
    assert calls[0].url.host == "api.met.no" and calls[0].url.path == "/weatherapi/locationforecast/2.0/complete"
    assert first.from_cache is False and first.expires_at == datetime(2026, 9, 16, 21, 5)
    assert first.provider_updated_at == datetime(2026, 9, 16, 20, 31, 7) and (first.latitude, first.longitude) == (-0.09, 34.77)

    # Before Expires: no request at all (MET's terms), the cached snapshot comes back.
    again = provider.forecast(-0.09, 34.77, now=t0 + timedelta(minutes=2))
    assert len(calls) == 1 and again.from_cache is True and again.hours == first.hours

    # After Expires: a conditional request; 304 → cached forecast, new Expires remembered.
    state["mode"] = "304"
    later = provider.forecast(-0.09, 34.77, now=t0 + timedelta(minutes=10))
    assert len(calls) == 2 and calls[1].headers["If-Modified-Since"] == "Wed, 16 Sep 2026 20:31:07 GMT"
    assert later.from_cache is True and later.hours == first.hours and later.expires_at == datetime(2026, 9, 16, 22, 30)


def test_provider_selection_from_env(monkeypatch):
    assert resolve_provider_name(None) == OPEN_METEO
    for alias in ("met_no", "met_norway", "MET_NORWAY", "metno"):
        assert resolve_provider_name(alias) == MET_NORWAY
    with pytest.raises(WeatherError) as info:
        resolve_provider_name("openweather")
    assert info.value.kind == "config"

    assert isinstance(provider_from_env(), OpenMeteoProvider)
    monkeypatch.setenv("WEATHER_PROVIDER", "met_no")
    monkeypatch.setenv("MET_NO_USER_AGENT", "NOC/0 (a@b.invalid)")
    met = provider_from_env()
    assert isinstance(met, MetNorwayProvider) and met.base_url == "https://api.met.no" and met.user_agent == "NOC/0 (a@b.invalid)"
    monkeypatch.setenv("WEATHER_API_BASE", "https://customer-api.open-meteo.com/")
    monkeypatch.setenv("WEATHER_PROVIDER", "open_meteo")
    monkeypatch.setenv("WEATHER_API_KEY", "k")
    om = provider_from_env()
    assert isinstance(om, OpenMeteoProvider) and om.base_url == "https://customer-api.open-meteo.com" and om.api_key == "k"


# ------------------------------------------------------------------------------ the table


def test_external_signals_table_shape_and_schema_version(tmp_db):
    settings, session = tmp_db
    cols = {r[1]: r for r in session.execute(text("PRAGMA table_info(external_signals)")).fetchall()}
    for name in (
        "id", "operator_id", "source", "source_url", "region_code", "county", "site_id", "fetched_at", "valid_from",
        "valid_until", "stale", "confidence", "storm_flag", "flood_flag", "planned_power", "access_risk",
        "payload_json", "derived_json", "external_id", "last_error", "created_at",
    ):
        assert name in cols, name
    assert cols["operator_id"][3] == 1  # NOT NULL: every operator-owned table carries it
    indexes = {r[1]: r[2] for r in session.execute(text("PRAGMA index_list(external_signals)")).fetchall()}
    assert "ix_signals_region_valid" in indexes
    assert any(unique for name, unique in indexes.items() if name.startswith("sqlite_autoindex") or "uq_" in name)
    # Two separate claims. The first -- stamped version == SCHEMA_VERSION -- is the real
    # invariant: a freshly created file is stamped with the version the code believes it
    # wrote. The trailing literal is a CANARY: it fires on any bump so that somebody has to
    # look at the migration rather than let a schema change ride along unnoticed. It fired
    # on 4 -> 5 (Phase 4: vendors, clock events, PIR, regulatory, evidence packs, and the
    # known-error columns on problems), the migration was reviewed, and the literal moved.
    # It fired again on 7 -> 8: the one non-additive step, the hitl_tasks rebuild that makes
    # incident_id nullable and gives the table its own operator_id (see db/migrate.py
    # _rebuild_hitl_tasks and tests/unit/test_migrate_rebuild.py), reviewed, literal moved.
    # It fired again on 8 -> 9: no new table or column, but two CHECK constraints on
    # vendor_scorecards were tightened after v8 files had been created, and nothing additive
    # can reach an existing table's constraints; the bump makes _refresh_check_constraints run
    # once, behind a backup -- recreating an EMPTY table from the model, warning about a
    # populated one (db/migrate.py "THE SECOND EXCEPTION", tests/unit/test_migrate_checks.py).
    # Keep the canary; move it deliberately, with the reason recorded, every time.
    assert session.execute(text("SELECT MAX(version) FROM schema_version")).scalar() == SCHEMA_VERSION == 9

    now = datetime(2026, 9, 17, 11, 0)
    common = dict(operator_id=OPERATOR, source=OPEN_METEO, source_url="mock://", region_code="NBI_W", fetched_at=now, valid_until=now, payload_json="{}", external_id="NBI_W:2026-09-17T11:00Z")
    session.add(ExternalSignalRow(id="a", **common))
    session.commit()
    session.add(ExternalSignalRow(id="b", **common))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
    row = session.get(ExternalSignalRow, "a")
    assert (row.stale, row.confidence, row.storm_flag, row.flood_flag, row.planned_power, row.access_risk) == (0, 1.0, 0, 0, 0, 0)


# ------------------------------------------------------------------------------ the poller


def test_poll_is_off_by_default_and_makes_no_request(tmp_db):
    settings, session = tmp_db
    calls: list[httpx.Request] = []
    result = poll(session, settings, provider=_open_meteo(calls=calls), now=STORM_NOW_OM, centroids=NBI_W)
    assert "skipped" in result.summary and "WEATHER_ENABLED" in result.summary
    assert result.tools[0]["skipped"] is True and calls == [] and _rows(session) == []


def test_poll_fetches_derives_and_upserts_one_row_per_region_bucket(tmp_db, monkeypatch, clean_hub):
    settings, session = tmp_db
    monkeypatch.setenv("WEATHER_ENABLED", "true")
    calls: list[httpx.Request] = []
    provider = _open_meteo(calls=calls)

    result = poll(session, settings, provider=provider, now=STORM_NOW_OM, centroids=NBI_W)
    assert result.summary.startswith("1/1 regions fetched via OPEN_METEO; STORM flag: NBI_W")
    tool = result.tools[0]
    assert tool["name"] == "weather.forecast" and tool["ok"] is True and tool["storm_flag"] is True
    assert tool["rain_mm_next_6h"] == pytest.approx(22.4) and tool["hours_in_window"] == 6

    rows = _rows(session, "NBI_W")
    assert len(rows) == 1
    row = rows[0]
    assert (row.operator_id, row.source, row.region_code, row.external_id) == (OPERATOR, OPEN_METEO, "NBI_W", "NBI_W:2026-09-17T11:00Z")
    assert row.fetched_at == STORM_NOW_OM and row.valid_from == STORM_NOW_OM and row.valid_until == STORM_NOW_OM + ROW_VALID_FOR
    assert (row.stale, row.confidence, row.storm_flag, row.flood_flag, row.planned_power, row.access_risk, row.last_error) == (0, 1.0, 1, 0, 0, 0, None)
    assert json.loads(row.payload_json)["hourly"]["time"][0] == "2026-09-17T00:00"  # the body as received
    derived = json.loads(row.derived_json)
    assert derived["storm_flag"] is True and derived["rain_mm_next_6h"] == pytest.approx(22.4)
    assert row.source_url.startswith("https://api.open-meteo.com/v1/forecast?latitude=-1.2700")

    # Same 15-minute bucket → the same row is updated, not duplicated.
    poll(session, settings, provider=provider, now=STORM_NOW_OM + timedelta(minutes=7), centroids=NBI_W)
    rows = _rows(session, "NBI_W")
    assert len(rows) == 1 and rows[0].id == row.id and rows[0].fetched_at == STORM_NOW_OM + timedelta(minutes=7)

    # Next bucket → a second row (history for the backtest); the newest wins the read.
    poll(session, settings, provider=provider, now=STORM_NOW_OM + timedelta(minutes=15), centroids=NBI_W)
    rows = _rows(session, "NBI_W")
    assert len(rows) == 2 and {r.external_id for r in rows} == {"NBI_W:2026-09-17T11:00Z", "NBI_W:2026-09-17T11:15Z"}
    assert latest_signal(session, OPERATOR, "NBI_W").fetched_at == STORM_NOW_OM + timedelta(minutes=15)
    assert len(calls) == 3

    events = [r for r in clean_hub.recent(10) if r["type"] == "external_signal.updated"]
    assert events and events[-1]["payload"] == {"source": OPEN_METEO, "region_code": "NBI_W", "stale": False, "storm_flag": True, "flood_flag": False, "error": None}


def test_poll_reads_back_without_the_network(tmp_db, monkeypatch):
    settings, session = tmp_db
    monkeypatch.setenv("WEATHER_ENABLED", "true")
    poll(session, settings, provider=_open_meteo(), now=STORM_NOW_OM, centroids=NBI_W)

    def dead(request):  # any read that touched the provider would fail here
        raise AssertionError("cache read touched the provider")

    block = weather_risk_for_region(session, OPERATOR, "NBI_W", now=STORM_NOW_OM + timedelta(minutes=5))
    assert block["storm_flag"] is True and block["stale"] is False and block["age_s"] == 300 and block["region_code"] == "NBI_W"
    assert weather_risk_for_region(session, OPERATOR, "CST") is None  # never fetched
    assert latest_signal(session, "airtel", "NBI_W") is None  # operator-scoped: another operator sees nothing


def test_poll_with_met_norway_provider(tmp_db, monkeypatch):
    settings, session = tmp_db
    monkeypatch.setenv("WEATHER_ENABLED", "true")
    provider = MetNorwayProvider(user_agent="KenyaNOC/2.0 (noc@example.invalid)", client=_client(_fixture_handler(MET_FIXTURE)))
    result = poll(session, settings, provider=provider, now=STORM_NOW_MET, centroids=WNY)
    assert result.summary.startswith("1/1 regions fetched via MET_NORWAY; STORM flag: WNY")
    row = latest_signal(session, OPERATOR, "WNY")
    assert row.source == MET_NORWAY and row.storm_flag == 1 and row.external_id == "WNY:2026-09-17T15:00Z"
    assert json.loads(row.derived_json)["cape_max_jkg"] is None


@pytest.mark.parametrize(
    "kind, make_handler, fragment",
    [
        ("timeout", lambda: (lambda req: (_ for _ in ()).throw(httpx.ReadTimeout("read timed out", request=req))), "did not answer within the timeout"),
        ("http", lambda: (lambda req: httpx.Response(500, text="Internal Server Error")), "HTTP 500"),
        ("malformed", lambda: (lambda req: httpx.Response(200, content=b"<html>Service Unavailable</html>", headers={"content-type": "text/html"})), "not JSON"),
        (
            "tls",
            lambda: (lambda req: (_ for _ in ()).throw(httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate (_ssl.c:1028)", request=req))),
            "NOC_USE_TRUSTSTORE=1",
        ),
        ("network", lambda: (lambda req: (_ for _ in ()).throw(httpx.ConnectError("[Errno 11001] getaddrinfo failed", request=req))), "unreachable"),
    ],
)
def test_provider_failures_are_fail_soft_and_keep_the_last_good_row(tmp_db, monkeypatch, kind, make_handler, fragment):
    settings, session = tmp_db
    monkeypatch.setenv("WEATHER_ENABLED", "true")
    good = poll(session, settings, provider=_open_meteo(), now=STORM_NOW_OM, centroids=NBI_W)
    assert good.tools[0]["ok"] is True
    before = latest_signal(session, OPERATOR, "NBI_W")
    payload, derived, row_id = before.payload_json, before.derived_json, before.id

    broken = _open_meteo(make_handler())
    t1 = STORM_NOW_OM + timedelta(minutes=20)  # inside valid_until
    result = poll(session, settings, provider=broken, now=t1, centroids=NBI_W)  # must not raise
    assert result.summary.startswith("0/1 regions fetched via OPEN_METEO; 1 failed (" + kind + ")")
    tool = result.tools[0]
    assert tool["ok"] is False and tool["error_kind"] == kind and fragment in tool["error"]

    session.expire_all()
    row = session.get(ExternalSignalRow, row_id)
    assert row.payload_json == payload and row.derived_json == derived  # the last good row is untouched...
    assert row.last_error.startswith(kind) and fragment in row.last_error  # ...but says what went wrong
    assert row.stale == 0 and is_stale(row, t1) is False  # still inside its window
    assert latest_signal(session, OPERATOR, "NBI_W").id == row_id
    assert len(_rows(session, "NBI_W")) == 1  # no marker row while a good row exists

    t2 = STORM_NOW_OM + ROW_VALID_FOR + timedelta(minutes=1)  # past valid_until
    poll(session, settings, provider=broken, now=t2, centroids=NBI_W)
    session.expire_all()
    row = session.get(ExternalSignalRow, row_id)
    assert row.stale == 1 and row.payload_json == payload
    block = weather_risk_for_region(session, OPERATOR, "NBI_W", now=t2)
    assert block["stale"] is True and block["storm_flag"] is True and block["last_error"].startswith(kind)


def test_failure_with_no_good_row_leaves_a_labelled_marker(tmp_db, monkeypatch, clean_hub):
    settings, session = tmp_db
    monkeypatch.setenv("WEATHER_ENABLED", "true")

    def five_hundred(request):
        return httpx.Response(503, text="upstream")

    result = poll(session, settings, provider=_open_meteo(five_hundred), now=STORM_NOW_OM, centroids=NBI_W)
    assert result.tools[0]["ok"] is False and result.tools[0]["error_kind"] == "http"
    assert latest_signal(session, OPERATOR, "NBI_W") is None and weather_risk_for_region(session, OPERATOR, "NBI_W") is None
    marker = latest_error(session, OPERATOR, "NBI_W")
    assert marker is not None and marker.external_id == "NBI_W:unavailable"
    assert (marker.stale, marker.confidence, marker.derived_json, marker.payload_json) == (1, 0.0, None, "{}")
    assert marker.valid_until == STORM_NOW_OM and is_stale(marker, STORM_NOW_OM) is True
    assert marker.last_error == "http 503: OPEN_METEO returned HTTP 503: upstream"
    # A second failure updates the marker instead of stacking rows.
    poll(session, settings, provider=_open_meteo(five_hundred), now=STORM_NOW_OM + timedelta(minutes=15), centroids=NBI_W)
    assert len(_rows(session, "NBI_W")) == 1
    event = [r for r in clean_hub.recent(10) if r["type"] == "external_signal.updated"][-1]
    assert event["payload"]["stale"] is True and event["payload"]["error"].startswith("http 503")


def test_a_forecast_that_does_not_cover_now_is_a_failure_not_a_row(tmp_db, monkeypatch):
    settings, session = tmp_db
    monkeypatch.setenv("WEATHER_ENABLED", "true")
    result = poll(session, settings, provider=_open_meteo(), now=datetime(2026, 9, 25, 0, 0), centroids=NBI_W)
    assert result.tools[0]["ok"] is False and result.tools[0]["error_kind"] == "malformed"
    assert "does not cover now" in result.tools[0]["error"]
    assert latest_signal(session, OPERATOR, "NBI_W") is None


def test_one_bad_region_does_not_stop_the_others(tmp_db, monkeypatch):
    settings, session = tmp_db
    monkeypatch.setenv("WEATHER_ENABLED", "true")
    body = OPEN_METEO_FIXTURE.read_bytes()

    def handler(request):
        if request.url.params["latitude"].startswith("-4."):  # CST times out, the others answer
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    centroids = {"NBI_W": (-1.27, 36.81), "CST": (-4.04, 39.67), "RFT": (-0.30, 36.08)}
    result = poll(session, settings, provider=_open_meteo(handler), now=STORM_NOW_OM, centroids=centroids)
    assert result.summary.startswith("2/3 regions fetched via OPEN_METEO")
    assert [(t["region_code"], t["ok"]) for t in result.tools] == [("NBI_W", True), ("CST", False), ("RFT", True)]
    assert latest_signal(session, OPERATOR, "RFT") is not None and latest_signal(session, OPERATOR, "CST") is None


def test_unexpected_exception_inside_the_provider_is_still_fail_soft(tmp_db, monkeypatch):
    settings, session = tmp_db
    monkeypatch.setenv("WEATHER_ENABLED", "true")

    class Exploding:
        source = OPEN_METEO

        def forecast(self, lat, lon, hours=48, *, now=None):
            raise KeyError("a bug, not a provider problem")

    result = poll(session, settings, provider=Exploding(), now=STORM_NOW_OM, centroids=NBI_W)
    assert result.tools[0]["ok"] is False and result.tools[0]["error_kind"] == "unexpected"
    assert "KeyError" in result.tools[0]["error"]


def test_misconfigured_provider_marks_every_region_and_never_raises(tmp_db, monkeypatch):
    settings, session = tmp_db
    monkeypatch.setenv("WEATHER_ENABLED", "true")
    monkeypatch.setenv("WEATHER_PROVIDER", "openweather")  # not a supported provider
    result = poll(session, settings, now=STORM_NOW_OM, centroids={"NBI_W": (-1.27, 36.81), "CST": (-4.04, 39.67)})
    assert result.summary.startswith("0/2 regions fetched")
    assert {t["error_kind"] for t in result.tools} == {"config"}
    assert {m.region_code for m in _rows(session)} == {"NBI_W", "CST"} and all(m.confidence == 0.0 for m in _rows(session))


def test_met_without_user_agent_is_a_config_failure_with_no_request(tmp_db, monkeypatch):
    settings, session = tmp_db
    monkeypatch.setenv("WEATHER_ENABLED", "true")
    monkeypatch.setenv("WEATHER_PROVIDER", "met_no")  # MET_NO_USER_AGENT deliberately unset
    calls: list[httpx.Request] = []
    monkeypatch.setattr(poller, "provider_from_env", lambda: MetNorwayProvider(user_agent="", client=_client(_fixture_handler(MET_FIXTURE, calls=calls))))
    result = poll(session, settings, now=STORM_NOW_MET, centroids=WNY)
    assert result.tools[0]["error_kind"] == "config" and "MET_NO_USER_AGENT" in result.tools[0]["error"] and calls == []


# ------------------------------------------------------------------------------ staleness


def test_staleness_is_computed_from_fetch_time_against_now(tmp_db, monkeypatch):
    settings, session = tmp_db
    monkeypatch.setenv("WEATHER_ENABLED", "true")
    poll(session, settings, provider=_open_meteo(), now=STORM_NOW_OM, centroids=NBI_W)
    row = latest_signal(session, OPERATOR, "NBI_W")
    assert row.stale == 0  # what is on disk...
    assert is_stale(row, STORM_NOW_OM + timedelta(minutes=59)) is False
    assert is_stale(row, STORM_NOW_OM + ROW_VALID_FOR) is True  # ...is not what a reader trusts an hour later
    late = staleness(row, STORM_NOW_OM + timedelta(minutes=59))
    assert late == {
        "stale": False,
        "age_s": 59 * 60,
        "fetched_at": "2026-09-17T11:00:00Z",
        "valid_until": "2026-09-17T12:00:00Z",
        "last_error": None,
    }
    assert staleness(row, STORM_NOW_OM + timedelta(hours=3))["stale"] is True
    assert weather_risk_for_region(session, OPERATOR, "NBI_W", now=STORM_NOW_OM + timedelta(hours=3))["stale"] is True


# ------------------------------------------------------------------------------ scheduler integration


def test_job_card_is_gated_by_weather_enabled_with_a_fifteen_minute_cadence():
    assert (WEATHER_JOB.name, WEATHER_JOB.interval_s, WEATHER_JOB.enabled_env) == ("weather_regions", 900, "WEATHER_ENABLED")
    assert (WEATHER_JOB.agent, WEATHER_JOB.graph_name, WEATHER_JOB.max_seconds) == ("WeatherRiskAgent", "weather", 60)
    assert WEATHER_JOB.fn is poll


def test_run_job_records_a_succeeded_run_even_when_the_provider_is_down(tmp_db, monkeypatch, clean_hub):
    settings, session = tmp_db
    monkeypatch.setenv("WEATHER_ENABLED", "true")

    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    monkeypatch.setattr(poller, "provider_from_env", lambda: _open_meteo(timeout))
    monkeypatch.setattr(poller, "region_centroids", lambda: dict(NBI_W))
    outcome = run_job(WEATHER_JOB, settings)
    assert outcome.status == "SUCCEEDED" and outcome.error is None
    assert outcome.consecutive_failures == 0 and outcome.circuit_open is False  # fail-soft never feeds the circuit
    assert "1 failed (timeout)" in outcome.summary
    assert read_state(session, "weather_regions").last_status == "SUCCEEDED"
    assert not [r for r in clean_hub.recent(20) if r["type"] == "scheduler.job_failed"]


def test_run_job_off_by_default_writes_nothing(tmp_db, monkeypatch):
    settings, session = tmp_db
    monkeypatch.setattr(poller, "provider_from_env", lambda: (_ for _ in ()).throw(AssertionError("provider built while disabled")))
    outcome = run_job(WEATHER_JOB, settings)
    assert outcome.status == "SUCCEEDED" and "skipped" in outcome.summary and _rows(session) == []


# ------------------------------------------------------------------------------ geography


def test_region_centroids_come_from_the_site_seed():
    centroids = region_centroids()
    assert set(centroids) == {"NBI_E", "NBI_W", "MTK", "CST", "RFT", "WNY"}
    for code, (lat, lon) in centroids.items():
        assert -5.0 <= lat <= 5.0 and 33.0 <= lon <= 42.0, (code, lat, lon)  # inside Kenya
    assert centroids["CST"] == (-4.04, 39.67)  # both Coast sites share the Mombasa town centroid
    assert region_centroids(sites=[]) == {}
