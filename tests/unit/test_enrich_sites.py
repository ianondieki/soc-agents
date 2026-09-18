"""ENRICH reads the site catalogue (spec §5.3.3 "Change (P1)", §7.0.7).

``services/sites.py`` shipped in Phase 1 with tests and no caller: an adversarial
import-graph audit found it unreachable from ``noc_agents.main``. These tests pin
the wiring itself, so removing it fails here rather than going unnoticed:

* the catalogue block (coordinates, ``kplc_region``/``kplc_area_hints``, hub
  topology, provenance markers) reaches the in-flight incident's context dict;
* the block is JSON-safe, i.e. it is what ``IncidentRow.context_json`` takes;
* ENRICH is fail-closed for the runner, so the lookup is fail-soft: a site miss,
  an absent seed, a corrupted seed and a read error each leave the node SUCCEEDED;
* the pinned ENRICH step row and its five state fields do not move — the literals
  below are copied from ``tests/integration/test_golden_sequence.py``.

``tests/unit/test_sites.py`` pins the catalogue's own shape and honesty markers;
this file pins only its use by the agent.
"""

from __future__ import annotations

import json

import pytest

from noc_agents.agents import enrich
from noc_agents.config import clear_settings_cache, get_settings
from noc_agents.domain.schemas import EventIngest
from noc_agents.orchestrator.contract import SUCCEEDED, IncidentState, RunContext
from noc_agents.orchestrator.registry import NODE_CARDS
from noc_agents.services import sites

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
# The golden auto-broadcast event, verbatim.
BTS_EVENT = dict(
    site_id="SFC-MTK-BTS-MCH04",
    site_name="Machakos Town BTS",
    site_type="BTS",
    region_code="MTK",
    alarm_code="SITE_DOWN",
    failure_domain="POWER",
    users_affected=3200,
)
# A catalogued child site, to prove the hub topology travels too.
CHILD_SITE_ID = "SFC-NBIE-ENB-KAY12"

# ENRICH step row as test_golden_sequence.py pins it (TOOLS_SHARED / CONFIDENCE).
GOLDEN_ENRICH_TOOLS = [
    {"name": "lookup_site", "ok": True, "latency_ms": 3},
    {"name": "estimate_users_affected", "ok": True, "latency_ms": 1},
    {"name": "classify_tt", "ok": True, "latency_ms": 1},
]
GOLDEN_ENRICH_CONFIDENCE = 0.85
GOLDEN_HUB_OUTPUT = "users_est=450000, region=NBI_E, hub=True, class=CRITICAL"
GOLDEN_HUB_RATIONALE_PREFIX = "CMDB/mock enrich: Nairobi East; FE on-call=FE-NBI-E-01; "


def _ctx() -> RunContext:
    """ENRICH.run touches only ``ctx.cfg``; the rest of RunContext is unused here."""
    clear_settings_cache()
    return RunContext(session=None, settings=get_settings("safaricom"), tracker=None, run=None)


def _run(**overrides):
    state = IncidentState(event=EventIngest(**{**HUB_EVENT, **overrides}))
    return state, enrich.run(state, _ctx())


def _context(state) -> dict:
    return getattr(state, enrich.CONTEXT_ATTR)


@pytest.fixture(autouse=True)
def restore_catalogue():
    """Any test that repoints SEED_PATH must not leak a poisoned cache."""
    yield
    sites.reload_sites()


# --- the wiring itself ------------------------------------------------------------------


def test_enrich_is_the_catalogue_s_caller_and_is_on_the_hot_path():
    """Reachability from noc_agents.main: enrich holds the function, the graph holds enrich."""
    assert enrich.lookup_site is sites.lookup_site
    assert [c.node_id for c in NODE_CARDS if c.run is enrich.run] == ["ENRICH"]


# --- the data reaches the incident ------------------------------------------------------


def test_known_site_coordinates_and_kplc_hints_reach_the_incident_context():
    record = sites.lookup_site(HUB_EVENT["site_id"])
    assert record is not None, "fixture assumption: the golden HUB site is catalogued"

    state, result = _run()

    assert result.status == SUCCEEDED
    block = _context(state)[enrich.SITE_KEY]
    # Phase 3's weather/power lanes need exactly these.
    assert (block["lat"], block["lon"]) == (record.lat, record.lon)
    assert block["lat"] is not None and block["lon"] is not None
    assert block["kplc_region"] == record.kplc_region
    assert block["kplc_area_hints"] == list(record.kplc_area_hints)
    assert block["kplc_area_hints"], "the golden HUB site should carry KPLC area hints"
    # Coordinate honesty travels with the coordinate (sites.GEO_PRECISION_LEGEND).
    assert block["geo_precision"] in sites.GEO_PRECISION_LEGEND
    assert block["riverine"] is record.riverine
    assert block["site_class"] == record.site_class


def test_child_site_carries_its_parent_hub_from_the_catalogue_not_the_event():
    """The event names no parent; the catalogue does. Hub topology reaches the incident."""
    state, result = _run(site_id=CHILD_SITE_ID, site_name=None, site_type="ENODEB", parent_hub_id=None)

    assert result.status == SUCCEEDED
    assert state.event.parent_hub_id is None
    assert _context(state)[enrich.SITE_KEY]["parent_hub_id"] == "SFC-NBIE-HUB-EMB"


def test_context_block_is_what_context_json_takes():
    """IncidentRow.context_json is a Text column written with json.dumps — the block must survive it."""
    state, _ = _run()
    context = _context(state)
    assert json.loads(json.dumps(context)) == context


# --- fail-soft: a data-file problem must not kill a fail-closed node --------------------


def test_uncatalogued_site_id_is_not_an_error():
    state, result = _run(site_id="SFC-ZZZ-NOT-CATALOGUED-01")

    assert result.status == SUCCEEDED
    assert _context(state) == {}
    # the node still did its real work
    assert state.users and state.tt is not None and state.site_name


def test_absent_seed_file_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(sites, "SEED_PATH", tmp_path / "no_such_catalogue.json")
    monkeypatch.setattr(sites, "_CACHE", None)

    state, result = _run()

    assert result.status == SUCCEEDED
    assert _context(state) == {}


def test_corrupted_seed_is_not_an_error(tmp_path, monkeypatch):
    bad = tmp_path / "safaricom_sites.json"
    bad.write_text('[{"site_id": "SFC-NBIE-HUB-EMB", "lat": ', encoding="utf-8")  # truncated
    monkeypatch.setattr(sites, "SEED_PATH", bad)
    monkeypatch.setattr(sites, "_CACHE", None)

    # The catalogue itself raises here — the wrapper in enrich is what saves the run.
    with pytest.raises(json.JSONDecodeError):
        sites.lookup_site(HUB_EVENT["site_id"])
    monkeypatch.setattr(sites, "_CACHE", None)

    state, result = _run()

    assert result.status == SUCCEEDED
    assert _context(state) == {}


def test_read_error_is_not_an_error(monkeypatch):
    def unreadable(_site_id):
        raise OSError("seed file is locked")

    monkeypatch.setattr(enrich, "lookup_site", unreadable)

    state, result = _run()

    assert result.status == SUCCEEDED
    assert _context(state) == {}
    assert result.output_summary == GOLDEN_HUB_OUTPUT  # still the pinned row


# --- the pinned ENRICH step row did not move -------------------------------------------


def test_golden_hub_enrich_step_row_is_unchanged():
    state, result = _run()

    assert result.status == SUCCEEDED
    assert result.output_summary == GOLDEN_HUB_OUTPUT
    assert result.rationale.startswith(GOLDEN_HUB_RATIONALE_PREFIX)
    assert result.tools == GOLDEN_ENRICH_TOOLS
    assert result.confidence == GOLDEN_ENRICH_CONFIDENCE
    assert (state.users, state.site_name, state.county, state.is_hub) == (
        450000,
        "Embakasi East Aggregation HUB",
        "Nairobi",
        True,
    )
    assert state.tt.site_class == "CRITICAL"


def test_golden_bts_enrich_step_row_is_unchanged():
    state = IncidentState(event=EventIngest(**BTS_EVENT))
    result = enrich.run(state, _ctx())

    assert result.status == SUCCEEDED
    assert enrich.input_summary(state, _ctx()) == "SFC-MTK-BTS-MCH04"
    assert result.tools == GOLDEN_ENRICH_TOOLS
    assert result.confidence == GOLDEN_ENRICH_CONFIDENCE
    assert result.output_summary.startswith("users_est=3200, region=MTK, hub=False, class=")
    assert result.rationale.startswith("CMDB/mock enrich: Mt Kenya; FE on-call=FE-MTK-01; ")
    assert (state.site_name, state.is_hub) == ("Machakos Town BTS", False)


def test_enrich_writes_no_state_field_beyond_its_node_card_plus_the_context():
    """ENRICH's NodeCard declares five writes; the context block is the only addition."""
    state, _ = _run()
    declared = {"users", "site_name", "county", "is_hub", "tt"}
    touched = {
        name
        for name, value in vars(state).items()
        if name not in ("event",) and value not in (None, False)
    }
    assert touched <= declared | {enrich.CONTEXT_ATTR}
