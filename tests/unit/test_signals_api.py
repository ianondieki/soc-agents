"""``GET /api/v1/signals`` and the county→region check (spec §7.3.2, §7.3.7; CONFORMANCE C-15).

What is pinned:

* §7.3.2's route: rows with ``stale`` (recomputed against now, not read off disk),
  ``valid_until`` and flags; ``active`` filters on the source's own validity; the provider body
  is never on the wire;
* **an empty list never travels alone** — every response carries ``feeds``, so "no rows"
  arrives beside ``never_polled`` / ``unreachable`` and cannot be read as "no warnings";
* a misspelt ``source`` or ``region_code`` is a 422, not an empty (reassuring) list;
* both operators seeded into the region code both profiles use (``CST``); over HTTP, each
  profile sees only its own rows;
* the county→region map: derived from the profile, one-to-many, and **every shipped profile
  validates** — the CI layer of "reject unknown counties at startup";
* the import-time layer logs and never raises, even when the profile cannot load;
* guardrail G4: the read modules this lane added to never load ``httpx``.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from noc_agents.config import get_settings
from noc_agents.db.models import ExternalSignalRow, get_session, new_id, utcnow
from noc_agents.realtime.hub import hub
from noc_agents.services import signals as svc

ROOT = Path(__file__).resolve().parents[2]
SHARED = "CST"  # a region code BOTH operator profiles define — the sharpest isolation case


@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'signals.db').as_posix()}")
    monkeypatch.setenv("OPERATOR_PROFILE", "safaricom")
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
        yield client
    hub._history.clear()
    cfg.clear_settings_cache()


def _row(*, operator: str = "safaricom", source: str = "KMD_CAP", region: str | None = SHARED, eid: str,
         fetched_ago: timedelta = timedelta(hours=1), valid_for: timedelta = timedelta(days=1),
         stale: int = 0, storm: int = 0, flood: int = 0, derived: dict | None = None, last_error: str | None = None) -> str:
    now = utcnow()
    fetched = now - fetched_ago
    row_id = new_id()
    session = get_session()
    try:
        session.add(ExternalSignalRow(
            id=row_id, operator_id=operator, source=source, source_url="mock://", region_code=region,
            fetched_at=fetched, valid_from=fetched, valid_until=fetched + valid_for, stale=stale, confidence=1.0,
            storm_flag=storm, flood_flag=flood, planned_power=0, access_risk=0,
            payload_json=json.dumps({"xml": "<alert>the whole provider body</alert>"}),
            derived_json=json.dumps(derived or {"kind": "cap_alert", "severity": "Severe"}, separators=(",", ":")),
            external_id=eid, last_error=last_error, created_at=fetched,
        ))
        session.commit()
    finally:
        session.close()
    return row_id


# ------------------------------------------------------------------------------ GET /api/v1/signals


def test_an_empty_answer_arrives_with_the_reason_it_is_empty(api):
    body = api.get("/api/v1/signals").json()
    assert body["count"] == 0 and body["signals"] == [] and body["weather_enabled"] is False
    assert set(body["feeds"]) == set(svc.SIGNAL_SOURCES)
    for source, feed in body["feeds"].items():
        assert feed["state"] == "never_polled" and feed["stale"] is True and feed["available"] is False, source
        assert feed["reason"]
    cap = api.get("/api/v1/signals", params={"source": "KMD_CAP", "active": "true"}).json()
    assert cap["signals"] == [] and list(cap["feeds"]) == ["KMD_CAP"]
    assert "nothing here says whether a warning is in force" in cap["feeds"]["KMD_CAP"]["reason"]


@pytest.mark.parametrize("params, fragment", [
    ({"source": "OPENMETEO"}, "unknown source"),
    ({"region_code": "ATLANTIS"}, "unknown region_code"),
    ({"region_code": "NBI"}, "unknown region_code"),  # Airtel's code, not Safaricom's
])
def test_a_misspelt_filter_is_a_422_not_a_reassuring_empty_list(api, params, fragment):
    response = api.get("/api/v1/signals", params=params)
    assert response.status_code == 422 and fragment in response.text


def test_rows_carry_recomputed_staleness_and_flags_but_never_the_provider_body(api):
    live = _row(eid="live#CST", storm=1)
    expired = _row(eid="old#CST", fetched_ago=timedelta(days=3), valid_for=timedelta(days=1))
    # Stored stale=0 but past its validity: must read stale, whatever the column says.
    lapsed = _row(source="GLOFAS", eid="SITE:day", fetched_ago=timedelta(hours=30), valid_for=timedelta(hours=26),
                  flood=1, derived={"kind": "river_discharge", "ratio": 2.6})
    body = api.get("/api/v1/signals", params={"source": "kmd_cap"}).json()  # case-insensitive source
    assert [s["id"] for s in body["signals"]] == [live, expired]  # newest first
    first = body["signals"][0]
    assert set(first) >= {"stale", "valid_until", "storm_flag", "flood_flag", "planned_power", "access_risk", "derived"}
    assert "payload_json" not in first and "the whole provider body" not in json.dumps(body)
    assert first["storm_flag"] is True and first["stale"] is False and first["valid_until"].endswith("Z")
    assert body["signals"][1]["stale"] is True

    flood = api.get("/api/v1/signals", params={"source": "GLOFAS"}).json()
    assert flood["signals"][0]["id"] == lapsed and flood["signals"][0]["stale"] is True
    assert flood["signals"][0]["flood_flag"] is True and flood["feeds"]["GLOFAS"]["state"] == "stale"


def test_active_means_in_force_by_the_sources_own_validity(api):
    live = _row(eid="live#CST")
    expired = _row(eid="old#CST", fetched_ago=timedelta(days=3), valid_for=timedelta(days=1))
    # A KMD warning we cannot currently vouch for (feed down: stale=1) is still in force by
    # KMD's own expires, so it is listed as active — flagged stale, never hidden.
    blind = _row(eid="blind#CST", stale=1, last_error="KMD feed not read since ...")
    active = {s["id"] for s in api.get("/api/v1/signals", params={"active": "true"}).json()["signals"]}
    inactive = {s["id"] for s in api.get("/api/v1/signals", params={"active": "false"}).json()["signals"]}
    assert active == {live, blind} and inactive == {expired}
    blind_row = next(s for s in api.get("/api/v1/signals").json()["signals"] if s["id"] == blind)
    assert blind_row["stale"] is True and blind_row["last_error"].startswith("KMD feed not read")


def test_the_cap_feed_state_travels_with_the_rows(api):
    _row(eid=f"feed:{SHARED}", valid_for=timedelta(0), stale=1, last_error="timeout: no answer",
         derived={"kind": "cap_feed_health", "state": "unreachable", "reachable": False, "feed_stale": True,
                  "reason": "KMD CAP feed unreachable: timeout"})
    body = api.get("/api/v1/signals", params={"source": "KMD_CAP", "region_code": SHARED, "active": "true"}).json()
    assert body["signals"] == []  # the health row is not a warning ...
    feed = body["feeds"]["KMD_CAP"]
    assert feed["state"] == "unreachable" and feed["stale"] is True and feed["last_error"] == "timeout: no answer"  # ... and says why


def test_f02_a_stored_ok_from_a_poller_that_stopped_reads_not_polled_recently(api):
    """F02 at the API: the last run said 'ok'; three days of silence since say nothing."""
    _row(eid=f"feed:{SHARED}", fetched_ago=timedelta(days=3), valid_for=timedelta(0), stale=1,
         derived={"kind": "cap_feed_health", "state": "ok", "reachable": True, "feed_stale": False})
    feed = api.get("/api/v1/signals", params={"source": "KMD_CAP", "region_code": SHARED}).json()["feeds"]["KMD_CAP"]
    assert feed["state"] == "not_polled_recently" and feed["stale"] is True and "has not run for" in feed["reason"]


def test_f09_the_glofas_feed_status_aggregates_every_site(api):
    """F09 at the API: two sites, same run timestamp; the flooding one must not lose the tie."""
    for site, flag in (("SITE-A", 0), ("SITE-Z", 1)):
        _row(source="GLOFAS", eid=f"{site}:day", fetched_ago=timedelta(hours=1), valid_for=timedelta(hours=26),
             flood=flag, derived={"kind": "river_discharge", "flood_flag": bool(flag)})
    session = get_session()
    try:
        for row in session.query(ExternalSignalRow).filter(ExternalSignalRow.source == "GLOFAS"):
            row.site_id = row.external_id.split(":")[0]
        session.commit()
    finally:
        session.close()
    feed = api.get("/api/v1/signals", params={"source": "GLOFAS", "region_code": SHARED}).json()["feeds"]["GLOFAS"]
    assert feed["state"] == "ok" and feed["flag"] is True


def test_each_operator_sees_only_its_own_rows_over_http(api, monkeypatch):
    saf = {_row(eid="a#CST"), _row(source="GLOFAS", eid="s:1", flood=1)}
    air = {_row(operator="airtel", eid="a#CST"), _row(operator="airtel", source="GLOFAS", eid="s:1", flood=1),
           _row(operator="airtel", eid=f"feed:{SHARED}", valid_for=timedelta(0), stale=1,
                derived={"kind": "cap_feed_health", "state": "ok", "reachable": True, "feed_stale": False})}
    body = api.get("/api/v1/signals", params={"region_code": SHARED}).json()
    assert {s["id"] for s in body["signals"]} == saf
    # Airtel's feed-health row must not make Safaricom's CAP feed look polled.
    assert body["feeds"]["KMD_CAP"]["state"] == "never_polled"

    import noc_agents.config as cfg

    monkeypatch.setenv("OPERATOR_PROFILE", "airtel")
    cfg.clear_settings_cache()
    body = api.get("/api/v1/signals", params={"region_code": SHARED}).json()
    assert {s["id"] for s in body["signals"]} == air
    assert body["feeds"]["KMD_CAP"]["state"] == "ok"


# ------------------------------------------------------------------------------ precision route


def test_precision_is_not_yet_measured_on_an_empty_database(api):
    body = api.get("/api/v1/signals/precision").json()
    assert body["family"] == "storm" and body["window_days"] == 30
    assert set(body["regions"]) == {"NBI_E", "NBI_W", "MTK", "CST", "RFT", "WNY"}
    for score in body["regions"].values():
        assert score["verdict"] == "INSUFFICIENT_DATA" and score["precision"] is None
        assert score["label"] == "precision: not yet measured"
    assert api.get("/api/v1/signals/precision", params={"family": "gossip"}).status_code == 422
    assert api.get("/api/v1/signals/precision", params={"region_code": "NBI"}).status_code == 422
    one = api.get("/api/v1/signals/precision", params={"region_code": "wny", "family": "flood"}).json()
    assert list(one["regions"]) == ["WNY"] and one["family"] == "flood"


# ------------------------------------------------------------------------------ county -> region


def test_the_county_map_is_derived_from_the_profile_and_one_to_many(api):
    body = api.get("/api/v1/signals/county-map").json()
    assert body["ok"] is True and body["operator_id"] == "safaricom" and body["gazetteer_size"] == 47
    assert body["counties"]["Nairobi"] == ["NBI_E", "NBI_W"]
    assert body["counties"]["Kiambu"] == ["MTK", "NBI_E", "NBI_W"]
    assert body["counties"]["Homa Bay"] == ["WNY"]  # the profile's "Homabay", canonicalised
    assert body["regions_without_counties"] == [] and body["problems"] == []
    assert isinstance(body["startup_problems"], list)


def test_every_shipped_operator_profile_names_only_real_counties():
    """The CI layer of §7.3.7's "rejects unknown counties at startup": a typo in any shipped
    profile fails here, before it can reach a deployment. Gaps (regions with no counties) are
    allowed — they are reported by the poller and the county-map route, not fatal."""
    profiles = sorted(p.stem for p in (ROOT / "config" / "operators").glob("*.yaml"))
    assert {"safaricom", "airtel"} <= set(profiles)
    for profile in profiles:
        cfg = get_settings.__wrapped__(profile).operator
        fatal = [p.message for p in svc.validate_county_map(cfg) if p.fatal]
        assert fatal == [], (profile, fatal)


def test_the_gazetteer_has_exactly_the_47_counties_and_matches_spellings():
    assert len(svc.all_counties()) == 47 and len(set(svc.all_counties())) == 47
    for spelling, canonical in [("Homabay", "Homa Bay"), ("MURANG’A", "Murang'a"), ("Nairobi City", "Nairobi"),
                                ("Elgeyo/Marakwet", "Elgeyo-Marakwet"), ("Trans Nzoia", "Trans-Nzoia"),
                                ("uasin gishu county", "Uasin Gishu")]:
        assert svc.canonical_county(spelling) == canonical, spelling
    for nonsense in ("Kiamb", "Atlantis", "", None, "Rift Valley"):
        assert svc.canonical_county(nonsense) is None, nonsense


def test_validate_reports_typos_as_fatal_and_empty_regions_as_gaps():
    base = get_settings.__wrapped__("safaricom").operator
    template = next(iter(base.regions.values()))
    cfg = base.model_copy(update={"regions": {
        "WNY": template.model_copy(update={"counties": ["Kisumu", "Kisumuu"]}),
        "EST": template.model_copy(update={"counties": []}),
    }})
    problems = {(p.kind, p.region_code, p.county, p.fatal) for p in svc.validate_county_map(cfg)}
    assert problems == {("unknown_county", "WNY", "Kisumuu", True), ("region_without_counties", "EST", None, False)}
    report = svc.county_map_report(cfg)
    assert report["ok"] is False and report["regions_without_counties"] == ["EST"]


def test_the_import_time_check_logs_loudly_and_never_raises(monkeypatch, caplog):
    import noc_agents.config as config
    from noc_agents.api.routers import signals as router

    base = get_settings.__wrapped__("safaricom")
    template = next(iter(base.operator.regions.values()))
    broken = base.model_copy(update={"operator": base.operator.model_copy(update={"regions": {
        "WNY": template.model_copy(update={"counties": ["Kisumuu"]})}})})

    monkeypatch.setattr(config.get_settings, "__wrapped__", lambda *a, **k: broken)
    with caplog.at_level(logging.WARNING, logger="noc_agents.api.routers.signals"):
        problems = router._check_county_map_at_import()
    assert problems[0]["kind"] == "unknown_county" and problems[0]["fatal"] is True
    assert any(r.levelno == logging.ERROR and "COUNTY MAP REJECTED" in r.getMessage() and "Kisumuu" in r.getMessage()
               for r in caplog.records)

    def cannot_load(*a, **k):
        raise FileNotFoundError("Operator profile not found: config/operators/typo.yaml")

    monkeypatch.setattr(config.get_settings, "__wrapped__", cannot_load)
    caplog.clear()
    with caplog.at_level(logging.ERROR, logger="noc_agents.api.routers.signals"):
        problems = router._check_county_map_at_import()  # must not raise: the app must still start
    assert problems == [{"kind": "check_failed", "message": "FileNotFoundError: Operator profile not found: config/operators/typo.yaml", "fatal": False}]
    assert any("could not run at import" in r.getMessage() for r in caplog.records)


def test_the_import_time_check_does_not_touch_the_settings_cache():
    """Importing the app must not change what a later get_settings() returns."""
    from noc_agents.api.routers import signals as router

    get_settings.cache_clear()
    router._check_county_map_at_import()
    assert get_settings.cache_info().currsize == 0


# ------------------------------------------------------------------------------ guardrail G4


@pytest.mark.parametrize("module", ["noc_agents.services.signals", "noc_agents.services.backtest"])
def test_the_read_modules_this_lane_extended_load_no_network_client(module):
    """services/signals.py is imported by ENRICH from inside run_incident_lifecycle; its part-two
    additions, and the backtest the dashboard will call, must not drag httpx in with them."""
    result = subprocess.run(
        [sys.executable, "-c",
         f"import sys, importlib; importlib.import_module({module!r}); "
         "print('LEAKED=' + ','.join(m for m in ('httpx', 'feedparser', 'pdfplumber', 'defusedxml', 'anthropic', 'mcp') if m in sys.modules))"],
        capture_output=True, text=True, cwd=str(ROOT),
        env={**os.environ, "NOC_SKIP_DOTENV": "1", "PYTHONPATH": str(ROOT / "src")},
    )
    assert result.returncode == 0, result.stderr[-1500:]
    assert result.stdout.strip().rsplit("LEAKED=", 1)[-1] == ""
