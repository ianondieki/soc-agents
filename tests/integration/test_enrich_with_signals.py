"""ENRICH reads the cached weather signal (spec §4.2 sentence 1, §5.3.3, §7.3.3).

§4.2 allows the hot path exactly one new thing here: ENRICH "may *read* an
already-cached ``external_signals`` row (flag-gated, wrapped fail-soft,
byte-identical when the table is empty)". This file pins every clause of that
sentence, in that order of importance:

1. **byte-identical** — with ``WEATHER_ENABLED`` off (the default), and with it on
   over an empty table, the ENRICH step row and the whole context dict are
   character-for-character what they were before this lane existed. With a cached
   row present the step row *still* does not move; only a new ``"weather"`` key
   appears in the context, beside the site catalogue's ``"site"`` key.
2. **fail-soft** — EnrichmentAgent is ``fail_closed``: an exception here rolls the
   run back and no incident survives. So a missing table, an unparseable row, a
   half-landed poller module and a slow/locked database each degrade to "no
   signal", and the full pipeline still produces an incident with the table
   dropped out from under it.
3. **read, never fetch** — a fetch on the hot path would put an external API in the
   critical path of every incident. An autouse fixture breaks ``socket``, and a
   second one makes constructing a weather *provider* an error, so a future edit
   that reached for the network fails here instead of in production.
4. **stale is labelled, not hidden** — a three-hour-old row is still readable, but
   it arrives carrying ``stale=True`` and its age, recomputed against *now* rather
   than trusted from the stored column, because an operator must never see
   three-hour-old rain rendered as current.
5. **county → region comes from config** — the base map is inverted from the
   operator profile's own ``regions[*].counties`` and overridden by
   ``config/operators/<op>/regions.yaml``; nothing is hard-coded in the agent.

**Zero network** (spec §7.3, Phase 3 "recorded fixtures only"). The cached rows in
this file are built from the committed recording
``tests/fixtures/weather/open_meteo_westlands_nbi_w.json`` through the real parser
and the real ``derive_weather_risk``, then written with the poller's own
``upsert_signal`` — so what ENRICH reads is exactly what a poll would have left
behind, with no HTTP client involved at any point.

``tests/unit/test_weather_provider.py`` pins the cache's own shape and the poller;
``tests/unit/test_enrich_sites.py`` pins the site-catalogue half of the context
block. This file pins only ENRICH's read of the cache.
"""

from __future__ import annotations

import json
import socket
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select, text

from noc_agents.adapters import weather as adapters_weather
from noc_agents.adapters.weather import derive_weather_risk, parse_open_meteo
from noc_agents.agents import enrich
from noc_agents.config import clear_settings_cache, get_settings
from noc_agents.db.models import AgentRunRow, ExternalSignalRow, get_session, utcnow
from noc_agents.domain.schemas import EventIngest
from noc_agents.graph.pipeline import process_event
from noc_agents.orchestrator.contract import SUCCEEDED, IncidentState, RunContext
from noc_agents.pollers import weather as poller
from noc_agents.services import signals

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "weather"
OPEN_METEO_FIXTURE = FIXTURES / "open_meteo_westlands_nbi_w.json"

# The golden HITL event, verbatim from tests/integration/test_golden_sequence.py.
HUB_EVENT = dict(
    site_id="SFC-NBIE-HUB-EMB",
    site_name="Embakasi East Aggregation HUB",
    site_type="HUB",
    region_code="NBI_E",
    alarm_code="POWER_GRID_FAIL",
    failure_domain="POWER",
    users_affected=450000,
    access_notes="Genset not started",
)

# The ENRICH step row as test_golden_sequence.py pins it (TOOLS_SHARED, CONFIDENCE,
# and the two literal assertions on by_node["ENRICH"]). Copied, not imported, so that
# an edit to either file has to be made in both places on purpose.
GOLDEN_ENRICH_OUTPUT = "users_est=450000, region=NBI_E, hub=True, class=CRITICAL"
GOLDEN_ENRICH_RATIONALE_PREFIX = "CMDB/mock enrich: Nairobi East; FE on-call=FE-NBI-E-01; "
GOLDEN_ENRICH_TOOLS = [
    {"name": "lookup_site", "ok": True, "latency_ms": 3},
    {"name": "estimate_users_affected", "ok": True, "latency_ms": 1},
    {"name": "classify_tt", "ok": True, "latency_ms": 1},
]
GOLDEN_ENRICH_CONFIDENCE = 0.85
GOLDEN_ENRICH_INPUT = "SFC-NBIE-HUB-EMB"
GOLDEN_ENRICH_STATE = (450000, "Embakasi East Aggregation HUB", "Nairobi", True, "CRITICAL")

OPERATOR = "safaricom"
REGION = "NBI_E"  # the golden HUB event's region; also what the cache is keyed by
# The recorded fixture's storm window is 14:00-20:00 EAT on 2026-09-17 = 11:00-17:00 UTC.
STORM_NOW = datetime(2026, 9, 17, 11, 0)
THREE_HOURS = timedelta(hours=3)


# ------------------------------------------------------------------------------ fixtures


@pytest.fixture(autouse=True)
def no_sockets(monkeypatch):
    """ENRICH reads a cache. Any attempt to open a connection fails loudly, here."""

    def boom(*args, **kwargs):
        raise AssertionError("ENRICH tried to open a network socket — it may only read the cache")

    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket.socket, "connect_ex", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)


@pytest.fixture(autouse=True)
def no_provider(monkeypatch):
    """Not even a *configured* provider. Constructing one is a fetch waiting to happen."""

    def boom(*args, **kwargs):
        raise AssertionError("ENRICH built a weather provider — the hot path reads the cache only")

    monkeypatch.setattr(adapters_weather, "provider_from_env", boom)
    monkeypatch.setattr(poller, "provider_from_env", boom)


@pytest.fixture(autouse=True)
def weather_off(monkeypatch):
    """Every test starts from the shipped default: the flag absent, i.e. off."""
    monkeypatch.delenv(enrich.WEATHER_ENABLED_ENV, raising=False)


@pytest.fixture()
def flag_on(monkeypatch):
    monkeypatch.setenv(enrich.WEATHER_ENABLED_ENV, "true")


# ------------------------------------------------------------------------------ helpers


def _ctx(session=None) -> RunContext:
    """ENRICH.run touches ``ctx.cfg`` and ``ctx.session``; the rest is unused here."""
    clear_settings_cache()
    return RunContext(session=session, settings=get_settings(OPERATOR), tracker=None, run=None)


def _run(session=None, **overrides):
    state = IncidentState(event=EventIngest(**{**HUB_EVENT, **overrides}))
    return state, enrich.run(state, _ctx(session))


def _context(state) -> dict:
    return getattr(state, enrich.CONTEXT_ATTR)


def _canonical(state) -> str:
    """The context dict as bytes, so "byte-identical" is asserted literally."""
    return json.dumps(_context(state), sort_keys=True, separators=(",", ":"))


def _assert_golden_step_row(state, result) -> None:
    """The four StepResult fields and the five state fields test_golden_sequence pins."""
    assert result.status == SUCCEEDED
    assert result.output_summary == GOLDEN_ENRICH_OUTPUT
    assert result.rationale.startswith(GOLDEN_ENRICH_RATIONALE_PREFIX)
    assert result.tools == GOLDEN_ENRICH_TOOLS
    assert result.confidence == GOLDEN_ENRICH_CONFIDENCE
    assert enrich.input_summary(state, _ctx()) == GOLDEN_ENRICH_INPUT
    assert (
        state.users,
        state.site_name,
        state.county,
        state.is_hub,
        state.tt.site_class,
    ) == GOLDEN_ENRICH_STATE


def _cache_row(
    session,
    *,
    region_code: str = REGION,
    fetched_at: datetime | None = None,
    now: datetime = STORM_NOW,
    operator_id: str = OPERATOR,
) -> ExternalSignalRow:
    """Write the row a successful poll would have left, from the committed recording.

    Real parser, real ``derive_weather_risk``, real ``upsert_signal`` — and no HTTP
    client anywhere: the provider response is read off disk.
    """
    body = json.loads(OPEN_METEO_FIXTURE.read_text(encoding="utf-8"))
    fetched_at = fetched_at or now
    snapshot = adapters_weather.ForecastSnapshot(
        source=adapters_weather.OPEN_METEO,
        source_url="file://" + OPEN_METEO_FIXTURE.name,
        latitude=body["latitude"],
        longitude=body["longitude"],
        fetched_at=fetched_at,
        hours=parse_open_meteo(body),
        raw=body,
    )
    derived = derive_weather_risk(snapshot, now=now)
    row = poller.upsert_signal(
        session,
        operator_id=operator_id,
        region_code=region_code,
        snapshot=snapshot,
        derived=derived,
        now=fetched_at,
    )
    session.commit()
    return row


def _hub_event() -> EventIngest:
    return EventIngest(**HUB_EVENT)


# --------------------------------------------------------- 1. byte-identical (requirement 1)


def test_flag_off_is_the_default(monkeypatch):
    """The shipped default is OFF, and it is the same reading the poller does."""
    assert enrich.weather_enabled() is False
    for spelling in ("", "false", "0", "no", "off", "maybe", "TRUE-ish"):
        monkeypatch.setenv(enrich.WEATHER_ENABLED_ENV, spelling)
        assert enrich.weather_enabled() is False, spelling
    for spelling in ("1", "true", "TRUE", " yes ", "on"):
        monkeypatch.setenv(enrich.WEATHER_ENABLED_ENV, spelling)
        assert enrich.weather_enabled() is True, spelling
    # The duplicated constants are duplicated on purpose (so the off path imports no
    # poller); this is the pin that keeps the two copies saying the same thing.
    assert enrich.WEATHER_ENABLED_ENV == poller.ENABLED_ENV
    assert enrich._TRUE == poller._TRUE


def test_flag_off_with_a_cached_row_present_is_byte_identical(tmp_db):
    """The strongest form: the row is RIGHT THERE and the flag alone keeps it out."""
    _settings, session = tmp_db
    _cache_row(session)
    assert session.scalars(select(ExternalSignalRow)).all(), "fixture assumption: a row is cached"

    state, result = _run(session)

    _assert_golden_step_row(state, result)
    assert enrich.WEATHER_KEY not in _context(state)
    assert enrich.SITE_KEY in _context(state), "the site catalogue's key is untouched"


def test_flag_on_over_an_empty_table_is_byte_identical(tmp_db, flag_on):
    """§4.2's own words: "byte-identical when the table is empty"."""
    _settings, session = tmp_db
    assert not session.scalars(select(ExternalSignalRow)).all()

    off_state, off_result = _run(session)  # flag_on applies to both; the table is the variable
    on_state, on_result = _run(session)

    _assert_golden_step_row(on_state, on_result)
    assert enrich.WEATHER_KEY not in _context(on_state)
    assert _canonical(on_state) == _canonical(off_state)
    assert (on_result.output_summary, on_result.rationale, on_result.tools, on_result.confidence) == (
        off_result.output_summary,
        off_result.rationale,
        off_result.tools,
        off_result.confidence,
    )


def test_a_cached_row_does_not_move_the_step_row_only_the_context(tmp_db, monkeypatch):
    """Flag on + a row present: the context gains one key and NOTHING else changes."""
    _settings, session = tmp_db
    _cache_row(session)

    off_state, off_result = _run(session)
    monkeypatch.setenv(enrich.WEATHER_ENABLED_ENV, "true")
    on_state, on_result = _run(session)

    _assert_golden_step_row(on_state, on_result)
    assert (on_result.output_summary, on_result.rationale, on_result.tools, on_result.confidence) == (
        off_result.output_summary,
        off_result.rationale,
        off_result.tools,
        off_result.confidence,
    )
    # The context differs by exactly one key, and the site block is byte-identical.
    on_ctx, off_ctx = _context(on_state), _context(off_state)
    assert set(on_ctx) - set(off_ctx) == {enrich.WEATHER_KEY}
    assert on_ctx[enrich.SITE_KEY] == off_ctx[enrich.SITE_KEY]
    assert {k: v for k, v in on_ctx.items() if k != enrich.WEATHER_KEY} == off_ctx


def test_the_persisted_enrich_step_row_does_not_move_with_the_lane_on(tmp_db, flag_on):
    """Through the real pipeline, read back from the database: the golden row is intact."""
    settings, session = tmp_db
    _cache_row(session)

    inc = process_event(session, settings, _hub_event())
    session.commit()

    read_back = get_session()  # a second Session: pin what actually committed
    try:
        run = read_back.scalar(select(AgentRunRow).where(AgentRunRow.incident_id == inc.id))
        step = {s.node_name: s for s in run.steps}["ENRICH"]
        assert step.status == "SUCCEEDED"
        assert step.input_summary == GOLDEN_ENRICH_INPUT
        assert step.output_summary == GOLDEN_ENRICH_OUTPUT
        assert step.rationale.startswith(GOLDEN_ENRICH_RATIONALE_PREFIX)
        assert step.tools_called == GOLDEN_ENRICH_TOOLS
        assert step.confidence == GOLDEN_ENRICH_CONFIDENCE
        # and the incident itself is the same incident it has always been
        assert (inc.priority, inc.region_code, inc.requires_hitl) == ("P2", "NBI_E", True)
    finally:
        read_back.close()


# ---------------------------------------------- 2. the signal reaches the incident (req. 3)


def test_a_cached_row_reaches_the_incident_context(tmp_db, flag_on):
    """The recorded storm arrives, under "weather", beside "site"."""
    _settings, session = tmp_db
    row = _cache_row(session)
    assert row.storm_flag == 1, "fixture assumption: the recording is a storm"

    state, result = _run(session)

    block = _context(state)[enrich.WEATHER_KEY]
    assert block["storm_flag"] is True
    assert block["storm_reasons"], "an advisory flag must say why"
    assert block["region_code"] == REGION
    assert block["source"] == adapters_weather.OPEN_METEO
    assert block["region_source"] == "event"
    assert result.status == SUCCEEDED


def test_the_weather_block_is_what_context_json_takes(tmp_db, flag_on):
    """IncidentRow.context_json is Text written with json.dumps; the block must survive it."""
    _settings, session = tmp_db
    _cache_row(session)

    state, _ = _run(session)

    context = _context(state)
    assert enrich.WEATHER_KEY in context
    assert json.loads(json.dumps(context)) == context


def test_the_cache_is_operator_scoped(tmp_db, flag_on):
    """Another operator's row is not this operator's weather."""
    _settings, session = tmp_db
    _cache_row(session, operator_id="airtel")

    state, result = _run(session)

    assert enrich.WEATHER_KEY not in _context(state)
    _assert_golden_step_row(state, result)


def test_a_row_for_another_region_is_not_read(tmp_db, flag_on):
    """The golden event is NBI_E; a Coast storm stays on the Coast."""
    _settings, session = tmp_db
    _cache_row(session, region_code="CST")

    state, _ = _run(session)

    assert enrich.WEATHER_KEY not in _context(state)


# --------------------------------------------------------- 3. stale is labelled (requirement)


def test_a_stale_row_is_readable_but_labelled_stale(tmp_db, flag_on):
    """Three-hour-old rain is still information — but never rendered as current."""
    _settings, session = tmp_db
    fetched_at = utcnow() - THREE_HOURS
    row = _cache_row(session, fetched_at=fetched_at, now=STORM_NOW)
    assert row.valid_until < utcnow(), "fixture assumption: the validity window has passed"
    # The row as STORED still claims freshness — the poller wrote stale=0 when it
    # succeeded, and the derived block was true at the time it was derived.
    assert row.stale == 0
    assert json.loads(row.derived_json)["stale"] is False

    state, result = _run(session)

    block = _context(state)[enrich.WEATHER_KEY]
    assert block["storm_flag"] is True, "a stale row is still readable"
    assert block["stale"] is True, "and it is never shown as current"
    assert block["age_s"] >= THREE_HOURS.total_seconds() - 5
    assert block["fetched_at"] and block["valid_until"], "the badge can say how old, and until when"
    _assert_golden_step_row(state, result)


def test_a_fresh_row_is_labelled_fresh(tmp_db, flag_on):
    """The stale label is real information, not a constant."""
    _settings, session = tmp_db
    _cache_row(session, fetched_at=utcnow(), now=STORM_NOW)

    state, _ = _run(session)

    block = _context(state)[enrich.WEATHER_KEY]
    assert block["stale"] is False
    assert block["age_s"] < 60


# --------------------------------------------------------- 4. fail-soft (requirement 2)


def test_a_missing_table_is_not_an_error(tmp_db, flag_on):
    """An old DB file, or a migration not yet run. ENRICH is fail_closed; this must not reach it."""
    _settings, session = tmp_db
    session.execute(text("DROP TABLE external_signals"))
    session.commit()

    state, result = _run(session)

    _assert_golden_step_row(state, result)
    assert enrich.WEATHER_KEY not in _context(state)
    # and the read left the session usable for nodes 4-12
    assert session.scalar(text("select count(*) from incidents")) == 0


def test_the_whole_pipeline_survives_a_missing_table(tmp_db, flag_on):
    """The fail-soft claim, end to end: an incident is still created and committed."""
    settings, session = tmp_db
    session.execute(text("DROP TABLE external_signals"))
    session.commit()

    inc = process_event(session, settings, _hub_event())
    session.commit()

    read_back = get_session()
    try:
        run = read_back.scalar(select(AgentRunRow).where(AgentRunRow.incident_id == inc.id))
        step = {s.node_name: s for s in run.steps}["ENRICH"]
        assert (step.status, step.output_summary) == ("SUCCEEDED", GOLDEN_ENRICH_OUTPUT)
        assert step.tools_called == GOLDEN_ENRICH_TOOLS
    finally:
        read_back.close()


def test_a_malformed_row_does_not_raise_and_is_not_shown(tmp_db, flag_on):
    """A corrupt derived_json must not become a silent "no storm"."""
    _settings, session = tmp_db
    row = _cache_row(session)
    session.execute(
        text("UPDATE external_signals SET derived_json = :bad WHERE id = :id"),
        {"bad": '{"storm_flag": tr', "id": row.id},  # truncated JSON
    )
    session.commit()
    session.expire_all()

    state, result = _run(session)

    _assert_golden_step_row(state, result)
    # The poller's reader absorbs the bad JSON and hands back staleness only; ENRICH
    # drops that rather than let a consumer read a missing storm_flag as "no storm".
    assert enrich.read_weather_risk(_ctx(session), REGION) is not None
    assert enrich.RISK_FIELD not in enrich.read_weather_risk(_ctx(session), REGION)
    assert enrich.WEATHER_KEY not in _context(state)


def test_an_uncoercible_column_does_not_raise(tmp_db, flag_on):
    """A hand-edited row: fetched_at is not a date. It raises on read, and is caught."""
    _settings, session = tmp_db
    row = _cache_row(session)
    session.execute(
        text("UPDATE external_signals SET fetched_at = 'not-a-date' WHERE id = :id"),
        {"id": row.id},
    )
    session.commit()
    session.expire_all()

    # The read really does raise once the wrapper is removed...
    with pytest.raises(Exception):
        poller.weather_risk_for_region(session, OPERATOR, REGION)
    session.rollback()

    # ...and the wrapper is what keeps the incident alive.
    state, result = _run(session)

    _assert_golden_step_row(state, result)
    assert enrich.WEATHER_KEY not in _context(state)


def test_a_slow_or_locked_database_does_not_raise(tmp_db, flag_on, monkeypatch):
    """Whatever the driver raises — busy timeout, locked file — ends at "no signal"."""
    from sqlalchemy.exc import OperationalError

    def slow(*args, **kwargs):
        raise OperationalError("SELECT 1", {}, Exception("database is locked"))

    # Patched on services.signals, which is where ENRICH reads it from (guardrail G4: the
    # hot path may not import a poller module). Patching the poller's re-export instead would
    # leave ENRICH calling the real function, and with a cache row seeded below this test
    # would then fail for the right reason rather than pass for the wrong one.
    monkeypatch.setattr(signals, "weather_risk_for_region", slow)
    _settings, session = tmp_db
    _cache_row(session)

    state, result = _run(session)

    _assert_golden_step_row(state, result)
    assert enrich.WEATHER_KEY not in _context(state)


def test_a_half_landed_signals_module_does_not_raise(tmp_db, flag_on, monkeypatch):
    """The import is lazy and inside the try, so a broken reader degrades to "no signal"."""
    monkeypatch.setitem(sys.modules, "noc_agents.services.signals", None)
    _settings, session = tmp_db
    _cache_row(session)  # a row IS there: what is broken is the code that would read it

    state, result = _run(session)

    _assert_golden_step_row(state, result)
    assert enrich.WEATHER_KEY not in _context(state)


def test_a_broken_poller_module_cannot_touch_the_hot_path_at_all(tmp_db, flag_on, monkeypatch):
    """ENRICH no longer imports the poller, so a broken poller costs it nothing.

    This used to be ``test_a_half_landed_poller_module_does_not_raise``, which asserted that a
    broken ``pollers.weather`` DEGRADED enrichment to "no weather". That was the right test for
    the old arrangement and the wrong arrangement: the hot path was importing the module that
    fetches forecasts over the network (and, through it, httpx) just to run one SELECT. With
    the cache reads moved to ``services.signals`` the stronger statement holds -- the poller can
    be missing, half-written or unimportable and an incident still gets its weather context,
    because the row was already written and reading it needs nothing from the poller.
    """
    monkeypatch.setitem(sys.modules, "noc_agents.pollers.weather", None)
    _settings, session = tmp_db
    _cache_row(session)

    state, _result = _run(session)

    assert enrich.WEATHER_KEY in _context(state)


def test_no_session_is_not_an_error(flag_on):
    """Unit callers build RunContext(session=None); that is a miss, not a crash."""
    state, result = _run(None)

    _assert_golden_step_row(state, result)
    assert enrich.WEATHER_KEY not in _context(state)


def test_a_broken_regions_file_does_not_raise(tmp_db, flag_on, tmp_path, monkeypatch):
    """Resolving the region reads YAML; a corrupt file is as harmless as a corrupt row."""
    bad = tmp_path / OPERATOR
    bad.mkdir()
    (bad / enrich.REGIONS_FILENAME).write_text("county_region: [not, a, mapping\n", encoding="utf-8")
    monkeypatch.setattr(enrich, "OPERATORS_DIR", tmp_path)
    _settings, session = tmp_db
    _cache_row(session)

    assert enrich.load_region_overrides(OPERATOR) == {}
    state, result = _run(session)

    _assert_golden_step_row(state, result)
    # The event's own region code still resolves, so the signal is not lost to a bad file.
    assert _context(state)[enrich.WEATHER_KEY]["region_source"] == "event"


def test_the_read_is_wrapped_at_the_agent_boundary():
    """A structural pin: run() calls the attach helper, which is the wrapped entry point."""
    assert enrich.attach_weather_context.__doc__
    assert enrich.read_weather_risk.__doc__
    # Nothing in this module reaches for a provider, a client or an HTTP verb.
    source = Path(enrich.__file__).read_text(encoding="utf-8")
    body = source.split('"""', 2)[-1]  # skip the module docstring, which discusses fetching
    for forbidden in ("httpx", "requests", "urllib", "provider_from_env", "WeatherProvider("):
        assert forbidden not in body, forbidden


# --------------------------------------------------- 5. county -> region from config (req. 4)


def test_the_base_map_is_inverted_from_the_operator_profile_not_hard_coded():
    """Change the profile's county lists and the map follows — no literal in the agent."""
    cfg = get_settings(OPERATOR).operator
    mapping = enrich.county_region_map(cfg)
    # Counties exactly one region claims resolve...
    assert mapping["mombasa"] == "CST"
    assert mapping["kisumu"] == "WNY"
    assert enrich.region_for_county(cfg, "  MOMBASA ") == "CST", "case/space insensitive"
    # ...and the source really is the profile, not a table in the agent.
    stub = cfg.model_copy(
        update={"regions": {code: r for code, r in cfg.regions.items() if code != "CST"}}
    )
    assert "mombasa" not in enrich.county_region_map(stub)


def test_a_county_two_regions_claim_stays_unresolved_until_a_human_decides():
    """Nairobi is claimed by NBI_E and NBI_W. Guessing would show the wrong half of the city."""
    cfg = get_settings(OPERATOR).operator
    claimants = [code for code, r in cfg.regions.items() if "Nairobi" in (r.counties or [])]
    assert len(claimants) > 1, "fixture assumption: Nairobi is ambiguous in this profile"
    assert enrich.region_for_county(cfg, "Nairobi") is None
    # Unresolved is not a loss: the event's region code is what the hot path uses.
    assert enrich.cache_region(cfg, "NBI_E", "Nairobi") == ("NBI_E", "event")


def test_the_shipped_regions_file_parses_and_is_the_override_layer():
    """config/operators/safaricom/regions.yaml exists, parses, and holds only overrides."""
    path = enrich.regions_path(OPERATOR)
    assert path.exists(), path
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    assert set(raw) <= {"county_region", "extra"}
    overrides = enrich.load_region_overrides(OPERATOR)
    assert all(isinstance(v, dict) for v in overrides.values())


def test_an_override_file_resolves_an_ambiguous_county(tmp_path, monkeypatch):
    """This is how a human resolves Nairobi — in YAML, never in code."""
    folder = tmp_path / OPERATOR
    folder.mkdir()
    (folder / enrich.REGIONS_FILENAME).write_text(
        yaml.safe_dump({"county_region": {"Nairobi": "NBI_W"}, "extra": {"Turkana": "RFT"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(enrich, "OPERATORS_DIR", tmp_path)
    cfg = get_settings(OPERATOR).operator

    assert enrich.region_for_county(cfg, "Nairobi") == "NBI_W"
    assert enrich.region_for_county(cfg, "Turkana") == "RFT", "a county no region lists still has weather"
    assert enrich.cache_region(cfg, "", "Turkana") == ("RFT", "county")


def test_an_override_naming_an_unknown_region_is_ignored(tmp_path, monkeypatch):
    """A typo must not point ENRICH at a region the cache will never hold."""
    folder = tmp_path / OPERATOR
    folder.mkdir()
    (folder / enrich.REGIONS_FILENAME).write_text(
        yaml.safe_dump({"county_region": {"Nairobi": "NOT_A_REGION"}}), encoding="utf-8"
    )
    monkeypatch.setattr(enrich, "OPERATORS_DIR", tmp_path)
    cfg = get_settings(OPERATOR).operator

    assert enrich.region_for_county(cfg, "Nairobi") is None


def test_a_missing_regions_file_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(enrich, "OPERATORS_DIR", tmp_path / "nothing-here")
    cfg = get_settings(OPERATOR).operator

    assert enrich.load_region_overrides(OPERATOR) == {}
    assert enrich.region_for_county(cfg, "Mombasa") == "CST", "the inverted base map still works"


def test_the_county_fallback_carries_its_provenance(tmp_db, flag_on):
    """An event with no region code, resolved by county, says so in the block."""
    _settings, session = tmp_db
    _cache_row(session, region_code="CST")

    state, result = _run(
        session,
        site_id="SFC-CST-HUB-MSA",
        site_name="Mombasa Island HUB",
        region_code="",
        county="Mombasa",
    )

    block = _context(state)[enrich.WEATHER_KEY]
    assert (block["region_code"], block["region_source"]) == ("CST", "county")
    assert result.status == SUCCEEDED


def test_an_unresolvable_region_is_a_miss_not_a_crash(tmp_db, flag_on):
    _settings, session = tmp_db
    _cache_row(session)

    state, result = _run(session, region_code="", county="Nairobi")  # ambiguous county

    assert result.status == SUCCEEDED
    assert enrich.WEATHER_KEY not in _context(state)
