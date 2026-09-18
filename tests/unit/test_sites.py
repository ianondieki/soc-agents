"""Site catalogue backfill (spec §7.0.7) + services/sites.py lookup.

These pin the catalogue's *shape and honesty markers*, not its business use:
the 15 spec fields exist on every row, coordinates are real Kenyan coordinates
at the declared (deliberately coarse) precision, the hub topology closes, and
the seven pre-existing fields of pre-existing sites were not touched.
"""

from __future__ import annotations

import json

from noc_agents.services.scenarios import RAIN_STORM_EVENTS
from noc_agents.services.sites import (
    GEO_PRECISION_LEGEND,
    SEED_PATH,
    SITE_CLASSES,
    SiteRecord,
    all_sites,
    child_sites,
    lookup_site,
    parent_hub,
    reload_sites,
)

# The 15 fields §7.0.7 requires: the original seven plus the eight backfilled.
BASE_FIELDS = (
    "site_id",
    "site_name",
    "site_type",
    "region_code",
    "county",
    "radio_oem",
    "coverage_note",
)
NEW_FIELDS = (
    "ward",
    "lat",
    "lon",
    "parent_hub_id",
    "site_class",
    "riverine",
    "kplc_region",
    "kplc_area_hints",
)
ALL_FIELDS = BASE_FIELDS + NEW_FIELDS

# Kenya's real bounding box (approx): lat -4.7..5.5, lon 33.9..41.9.
LAT_MIN, LAT_MAX = -4.7, 5.5
LON_MIN, LON_MAX = 33.9, 41.9

# The five storm-scenario sites that were missing from the seed, identified by
# diffing services/scenarios.py RAIN_STORM_EVENTS against the 14-site seed.
ADDED_STORM_SITES = (
    "SFC-RFT-ENB-NKR-A1",
    "SFC-RFT-ENB-NKR-B3",
    "SFC-MTK-ENB-MRI-01",
    "SFC-NBIE-ENB-UMB04",
    "SFC-NBIE-TX-MW-EAST1",
)

# Verbatim copy of a pre-existing seed row (the 7 original fields as they stood
# before the backfill). If the backfill edited an existing field, this fails.
FROZEN_ORIGINALS = {
    "SFC-NBIE-HUB-EMB": {
        "site_id": "SFC-NBIE-HUB-EMB",
        "site_name": "Embakasi East Aggregation HUB",
        "site_type": "HUB",
        "region_code": "NBI_E",
        "county": "Nairobi",
        "radio_oem": "MIXED",
        "coverage_note": "Nairobi East — power Egypro; fibre Egypro Fibre",
    },
    "SFC-MTK-BTS-MRI08": {
        "site_id": "SFC-MTK-BTS-MRI08",
        "site_name": "Meru Rural BTS 08",
        "site_type": "BTS",
        "region_code": "MTK",
        "county": "Meru",
        "radio_oem": "MIXED",
        "coverage_note": "Rural Mt Kenya — long drive",
    },
    "SFC-CST-ENB-NYL12": {
        "site_id": "SFC-CST-ENB-NYL12",
        "site_name": "Nyali eNodeB 12",
        "site_type": "ENODEB",
        "region_code": "CST",
        "county": "Mombasa",
        "radio_oem": "HUAWEI",
        "coverage_note": "Coast Huawei radio",
    },
}


def _raw_seed() -> list[dict]:
    return json.loads(SEED_PATH.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# seed file shape
# --------------------------------------------------------------------------


def test_seed_is_still_a_flat_json_list_of_objects():
    # GET /api/v1/sites returns this file verbatim; it must stay a list[dict].
    raw = _raw_seed()
    assert isinstance(raw, list)
    assert all(isinstance(row, dict) for row in raw)
    assert len(raw) == len({row["site_id"] for row in raw})


def test_every_site_has_all_fifteen_fields():
    for row in _raw_seed():
        missing = [f for f in ALL_FIELDS if f not in row]
        assert not missing, f"{row.get('site_id')} missing {missing}"


def test_backfilled_field_types():
    for row in _raw_seed():
        sid = row["site_id"]
        assert isinstance(row["riverine"], bool), sid
        assert isinstance(row["kplc_area_hints"], list), sid
        assert all(isinstance(h, str) and h for h in row["kplc_area_hints"]), sid
        assert row["ward"] is None or isinstance(row["ward"], str), sid
        assert row["kplc_region"] is None or isinstance(row["kplc_region"], str), sid
        assert row["site_class"] in SITE_CLASSES, sid


# --------------------------------------------------------------------------
# coordinates: real, inside Kenya, and honest about their precision
# --------------------------------------------------------------------------


def test_coordinates_are_inside_kenyas_bounding_box():
    for site in all_sites():
        assert site.lat is not None and site.lon is not None, site.site_id
        assert LAT_MIN <= site.lat <= LAT_MAX, f"{site.site_id} lat {site.lat}"
        assert LON_MIN <= site.lon <= LON_MAX, f"{site.site_id} lon {site.lon}"


def test_coordinates_declare_their_precision_and_are_rounded_to_2dp():
    # 2-dp rounding is the honesty signal: these are county/town/suburb
    # centroids, not surveyed site positions. A 4-dp value would imply a
    # precision the catalogue does not have.
    for row in _raw_seed():
        sid = row["site_id"]
        assert row["geo_precision"] in GEO_PRECISION_LEGEND, f"{sid}: {row['geo_precision']}"
        for axis in ("lat", "lon"):
            value = row[axis]
            assert isinstance(value, (int, float)), sid
            assert round(float(value), 2) == float(value), f"{sid} {axis}={value} is finer than 2 dp"


def test_kplc_fields_carry_a_source_marker():
    for row in _raw_seed():
        assert isinstance(row["kplc_source"], str) and row["kplc_source"], row["site_id"]
        # a site with no established region must not carry area hints implying one
        if row["kplc_region"] is None:
            assert "unresolved" in row["kplc_source"], row["site_id"]


# --------------------------------------------------------------------------
# topology
# --------------------------------------------------------------------------


def test_every_parent_hub_id_resolves_to_an_existing_hub():
    for site in all_sites():
        if not site.parent_hub_id:
            continue
        parent = lookup_site(site.parent_hub_id)
        assert parent is not None, f"{site.site_id} -> unknown {site.parent_hub_id}"
        assert parent.site_class == "HUB", f"{site.site_id} -> {parent.site_id} is not a HUB"
        assert parent.site_id != site.site_id


def test_hub_rows_have_no_parent_and_children_resolve_back():
    hubs = [s for s in all_sites() if s.site_class == "HUB"]
    assert hubs
    for hub in hubs:
        assert hub.parent_hub_id is None, hub.site_id
    for child in child_sites("SFC-NBIE-HUB-EMB"):
        assert parent_hub(child.site_id).site_id == "SFC-NBIE-HUB-EMB"
    assert {s.site_id for s in child_sites("SFC-RFT-HUB-NKR")} == {
        "SFC-RFT-ENB-NKR-A1",
        "SFC-RFT-ENB-NKR-B3",
    }


# --------------------------------------------------------------------------
# storm scenario coverage
# --------------------------------------------------------------------------


def test_the_five_missing_storm_sites_are_present():
    catalogued = {s.site_id for s in all_sites()}
    for site_id in ADDED_STORM_SITES:
        assert site_id in catalogued, site_id


def test_every_rain_storm_event_site_is_catalogued():
    for event in RAIN_STORM_EVENTS:
        site = lookup_site(event.site_id)
        assert site is not None, f"storm site not catalogued: {event.site_id}"
        assert site.site_type == event.site_type, event.site_id
        assert site.region_code == event.region_code, event.site_id
        if event.parent_hub_id:
            assert site.parent_hub_id == event.parent_hub_id, event.site_id


# --------------------------------------------------------------------------
# the original seven fields are untouched
# --------------------------------------------------------------------------


def test_pre_existing_sites_keep_their_original_seven_fields():
    rows = {row["site_id"]: row for row in _raw_seed()}
    for site_id, original in FROZEN_ORIGINALS.items():
        row = rows.get(site_id)
        assert row is not None, site_id
        for field, value in original.items():
            assert row[field] == value, f"{site_id}.{field} changed: {row[field]!r} != {value!r}"


def test_seed_still_holds_the_original_fourteen_sites():
    rows = {row["site_id"] for row in _raw_seed()}
    assert len(rows - set(ADDED_STORM_SITES)) == 14


# --------------------------------------------------------------------------
# lookup service
# --------------------------------------------------------------------------


def test_lookup_site_returns_a_typed_record():
    site = lookup_site("SFC-RFT-HUB-NKR")
    assert isinstance(site, SiteRecord)
    assert site.site_name == "Nakuru Rift HUB"
    assert site.county == "Nakuru"
    assert site.site_class == "HUB" and site.is_hub
    assert site.has_coordinates
    assert site.kplc_region == "Central Rift"
    assert "Nakuru Town" in site.kplc_area_hints


def test_lookup_site_is_none_for_unknown_or_empty_ids():
    assert lookup_site("SFC-NOPE-0001") is None
    assert lookup_site("") is None
    assert lookup_site(None) is None


def test_lookup_site_normalises_whitespace_and_case():
    assert lookup_site("  sfc-rft-hub-nkr ") is lookup_site("SFC-RFT-HUB-NKR")


def test_seed_is_loaded_once_and_cached():
    first = lookup_site("SFC-CST-HUB-MSA")
    assert lookup_site("SFC-CST-HUB-MSA") is first  # same cached instance
    assert all_sites() == all_sites()
    assert reload_sites() == len(_raw_seed())
    assert lookup_site("SFC-CST-HUB-MSA") == first  # equal after an explicit reload


def test_record_round_trips_through_as_dict():
    site = lookup_site("SFC-NBIE-ENB-UMB04")
    payload = site.as_dict()
    assert all(field in payload for field in ALL_FIELDS)
    assert SiteRecord.from_dict(payload) == site
