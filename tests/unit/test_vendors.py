"""Spec §7.6.1 -- the ``vendors`` table, ``incidents.vendor_id`` resolution, and the
versioned ``sla_terms`` loader (Phase 4 Lane 4A, step 1).

What is pinned here and why:

* **Seeding is a pure function of the operator profile.** One row per ``cfg.msp_contacts``
  key, insert-only on the §7.6.1 natural key, so it can run on every GET and never undo a
  human's edit. The natural key and the placeholder ``active_from`` match
  ``data/seed/v2/vendors.yaml`` so ``noc-seed-v2`` and this seeding recognise each other.
* **Resolution normalises spelling and honours the active window.** "EGYPRO", "egypro" and
  airtel's "Egypro" are one vendor; a re-contracted vendor is a second row and the
  incident's failure time picks the terms in force.
* **The terms are versioned and the key spelling is decided.** ``ack``/``restore``/
  ``note_interval`` (the spec's YAML block, identical to ``cfg.sla_minutes``), never
  ``ack_minutes``; every ``yaml_path`` the loader emits resolves to a real key in the file.
* **The flag is off by default** and the routes are a 404 while it is -- byte-for-byte the
  surface before this lane existed.
"""

from __future__ import annotations

import importlib
from datetime import date, datetime, timedelta

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from noc_agents.api import auth
from noc_agents.config import ROOT, get_settings
from noc_agents.db.models import IncidentRow
from noc_agents.db.models_vendors import SCC_CODES, VENDOR_TYPES, VendorRow
from noc_agents.domain.schemas import EventIngest
from noc_agents.graph.pipeline import process_event
from noc_agents.realtime.hub import hub
from noc_agents.services import vendors as svc
from noc_agents.services.vendors import (
    BAND_KEYS,
    DEFAULT_SLA_TERMS_PATH,
    FLAG,
    PRIORITIES,
    SOURCE_SLA_MINUTES,
    SOURCE_YAML,
    VENDOR_SEED_ACTIVE_FROM,
    attach_vendor,
    backfill_incident_vendor_ids,
    create_vendor,
    display_name_for,
    lane_enabled,
    list_vendors,
    load_sla_terms,
    normalise_code,
    resolve_vendor,
    seed_vendors_from_contacts,
    sla_terms_version,
    vendor_id_for,
)

SEED_VENDORS = ROOT / "data" / "seed" / "v2" / "vendors.yaml"
SEED_TERMS = ROOT / "data" / "seed" / "v2" / "sla_terms.yaml"


def _incident(session, *, msp_name: str | None, operator_id: str = "safaricom", number: str | None = None, **extra) -> IncidentRow:
    inc = IncidentRow(
        operator_id=operator_id,
        incident_number=number or f"INC-{msp_name or 'FE'}-{id(extra) % 100000}",
        site_id="SFC-MTK-HUB-THK",
        region_code=extra.pop("region_code", "MTK"),
        correlation_fingerprint="fp",
        msp_name=msp_name,
        **extra,
    )
    session.add(inc)
    session.flush()
    return inc


# --------------------------------------------------------------------------
# The flag and the vocabularies
# --------------------------------------------------------------------------


def test_the_flag_is_scorecards_enabled_and_defaults_off(monkeypatch):
    assert FLAG == "SCORECARDS_ENABLED"
    monkeypatch.delenv(FLAG, raising=False)
    assert lane_enabled() is False
    for raw in ("1", "true", "yes", "on", "TRUE"):
        monkeypatch.setenv(FLAG, raw)
        assert lane_enabled() is True, raw
    for raw in ("", "0", "false", "off", "nonsense"):
        monkeypatch.setenv(FLAG, raw)
        assert lane_enabled() is False, raw


def test_vendor_types_and_scc_codes_are_exactly_the_spec_lists():
    assert VENDOR_TYPES == ("MSP", "FE_CONTRACTOR", "OEM", "TOWERCO")
    assert SCC_CODES == (
        "END_USER_REQUEST",
        "OBSERVATION",
        "CONTACT_UNAVAILABLE",
        "WIRING_NOT_OURS",
        "UTILITY_POWER",
        "SITE_ACCESS_DENIED",
        "SECURITY_INCIDENT",
        "PLANNED_MAINTENANCE",
        "FORCE_MAJEURE",
        "AWAITING_THIRD_PARTY_PERMIT",
    )


# --------------------------------------------------------------------------
# The table
# --------------------------------------------------------------------------


def test_the_vendors_table_carries_the_spec_columns_and_natural_key(tmp_db):
    """§7.6.1 DDL, column for column, and UNIQUE (operator_id, code, active_from)."""
    _settings, session = tmp_db
    insp = inspect(session.get_bind())
    assert [c["name"] for c in insp.get_columns("vendors")] == [
        "id", "operator_id", "code", "display_name", "type", "contract_ref",
        "active_from", "active_to", "contacts_json",
    ]
    uniques = {tuple(u["column_names"]) for u in insp.get_unique_constraints("vendors")}
    assert ("operator_id", "code", "active_from") in uniques

    session.add(VendorRow(id="a", operator_id="safaricom", code="X", display_name="X", type="MSP", active_from=date(2026, 1, 1)))
    session.flush()
    session.add(VendorRow(id="b", operator_id="safaricom", code="X", display_name="X again", type="MSP", active_from=date(2026, 1, 1)))
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_the_seed_set_and_the_orm_agree_on_the_natural_key_and_placeholder_date():
    """noc-seed-v2 matches on (operator_id, code, active_from) with active_from
    2026-01-01; a row seeded here must be recognised by it and vice versa."""
    data = yaml.safe_load(SEED_VENDORS.read_text(encoding="utf-8"))
    assert date.fromisoformat(str(data["placeholder_active_from"])) == VENDOR_SEED_ACTIVE_FROM
    seed_codes = {v["code"] for v in data["vendors"] if v["operator_id"] == "safaricom"}
    assert seed_codes == {normalise_code(k) for k in get_settings().operator.msp_contacts}


# --------------------------------------------------------------------------
# Seeding from cfg.msp_contacts
# --------------------------------------------------------------------------


def test_seeding_creates_one_row_per_msp_contact_and_nothing_else(tmp_db):
    settings, session = tmp_db
    cfg = settings.operator
    inserted = seed_vendors_from_contacts(session, cfg)
    rows = list_vendors(session, cfg.operator_id)
    assert len(inserted) == len(rows) == len(cfg.msp_contacts) == 11
    assert {r.code for r in rows} == set(cfg.msp_contacts)  # safaricom keys are already codes
    for row in rows:
        entry = cfg.msp_contacts[row.code]
        assert row.type == "MSP"  # the block is called msp_contacts; nothing else is derivable
        assert row.contacts == entry  # email / sms / domains verbatim
        assert row.active_from == VENDOR_SEED_ACTIVE_FROM and row.active_to is None
        assert row.contract_ref is None  # this repo holds no contract reference and must not pretend to
        assert row.id == vendor_id_for("safaricom", row.code, VENDOR_SEED_ACTIVE_FROM)  # deterministic


def test_seeding_is_insert_only_so_a_human_edit_survives_a_rerun(tmp_db):
    settings, session = tmp_db
    seed_vendors_from_contacts(session, settings.operator)
    ecta = session.scalar(select(VendorRow).where(VendorRow.code == "ECTA"))
    ecta.display_name = "Edited by a human"
    ecta.type = "FE_CONTRACTOR"
    session.flush()
    assert seed_vendors_from_contacts(session, settings.operator) == []
    again = session.scalar(select(VendorRow).where(VendorRow.code == "ECTA"))
    assert (again.display_name, again.type) == ("Edited by a human", "FE_CONTRACTOR")


def test_a_retired_vendor_stays_retired_and_drops_out_of_the_active_list(tmp_db):
    """Retirement is ``active_to``, never a delete; a rerun must not resurrect it."""
    settings, session = tmp_db
    seed_vendors_from_contacts(session, settings.operator)
    atc = session.scalar(select(VendorRow).where(VendorRow.code == "ATC"))
    atc.active_to = date(2026, 6, 30)
    session.flush()
    assert seed_vendors_from_contacts(session, settings.operator) == []
    assert "ATC" in {r.code for r in list_vendors(session, "safaricom")}
    assert "ATC" in {r.code for r in list_vendors(session, "safaricom", as_of=date(2026, 6, 30))}
    assert "ATC" not in {r.code for r in list_vendors(session, "safaricom", as_of=date(2026, 7, 1))}


def test_airtel_title_case_keys_become_upper_case_codes_but_keep_their_spelling(tmp_db):
    """airtel.yaml spells the same vendors "Camusat" / "Egypro"; the code is normalised so
    resolution works, the display name keeps the profile's own spelling."""
    _settings, session = tmp_db
    cfg = get_settings("airtel").operator
    rows = seed_vendors_from_contacts(session, cfg)
    assert {r.code for r in rows} == {"ATC", "CAMUSAT", "EGYPRO"}
    by_code = {r.code: r for r in rows}
    assert by_code["CAMUSAT"].display_name == "Camusat"
    assert by_code["ATC"].display_name == "ATC"
    assert by_code["EGYPRO"].contacts["email"] == "egypro.airtel.ke@example.com"
    assert all(r.operator_id == "airtel" for r in rows)


@pytest.mark.parametrize(
    "code, key, expected",
    [
        ("EGYPRO", None, "Egypro"),
        ("EGYPRO_FIBRE", None, "Egypro Fibre"),
        ("ATC", None, "ATC"),  # three letters: an acronym, as the seed set spells it
        ("ECTA", None, "Ecta"),  # four letters: a name, as the seed set spells it
        ("HUAWEI_RADIO", None, "Huawei Radio"),
        ("CAMUSAT", "Camusat", "Camusat"),  # the profile's own spelling wins
    ],
)
def test_display_names_follow_the_seed_sets_spelling(code, key, expected):
    assert display_name_for(code, key) == expected


def test_normalise_code_folds_case_and_separators():
    assert normalise_code("Egypro Fibre") == normalise_code("egypro-fibre") == normalise_code(" EGYPRO_FIBRE ") == "EGYPRO_FIBRE"
    assert normalise_code(None) == normalise_code("") == ""


def test_a_profile_entry_with_an_unknown_type_is_refused(tmp_db):
    settings, session = tmp_db
    cfg = settings.operator.model_copy(update={"msp_contacts": {"ZETA": {"email": "z@example.com", "type": "GUESS"}}})
    with pytest.raises(ValueError, match="ZETA.type"):
        seed_vendors_from_contacts(session, cfg)


def test_a_profile_entry_may_carry_type_display_name_contract_and_dates(tmp_db):
    """The seed set's TOWERCO/OEM classifications are flagged REVIEW; once a human confirms
    one it belongs on the profile entry, and the row follows without a code change."""
    settings, session = tmp_db
    cfg = settings.operator.model_copy(
        update={
            "msp_contacts": {
                "ATC": {
                    "email": "atc@example.com",
                    "sms": "+254700000000",
                    "type": "TOWERCO",
                    "display_name": "American Tower Kenya",
                    "contract_ref": "MSA-ATC-2025",
                    "active_from": "2025-06-01",
                    "active_to": "2027-05-31",
                }
            }
        }
    )
    (row,) = seed_vendors_from_contacts(session, cfg)
    assert (row.type, row.display_name, row.contract_ref) == ("TOWERCO", "American Tower Kenya", "MSA-ATC-2025")
    assert (row.active_from, row.active_to) == (date(2025, 6, 1), date(2027, 5, 31))
    assert row.contacts == {"email": "atc@example.com", "sms": "+254700000000"}  # the row fields are not contacts


# --------------------------------------------------------------------------
# Resolving msp_name -> vendors.id
# --------------------------------------------------------------------------


def test_resolve_normalises_spelling_and_ignores_non_vendors(tmp_db):
    settings, session = tmp_db
    seed_vendors_from_contacts(session, settings.operator)
    egypro = session.scalar(select(VendorRow).where(VendorRow.code == "EGYPRO"))
    for spelling in ("EGYPRO", "egypro", " Egypro ", "egypro-"):
        assert resolve_vendor(session, operator_id="safaricom", msp_name=spelling) is egypro, spelling
    for not_a_vendor in (None, "", "FIELD_ENGINEER", "NOC", "NOC-QUEUE", "UNASSIGNED", "SOME_UNKNOWN_CO"):
        assert resolve_vendor(session, operator_id="safaricom", msp_name=not_a_vendor) is None, not_a_vendor


def test_resolve_is_operator_scoped(tmp_db):
    """Both operators contract ATC. Each resolves to its OWN row; the ids differ."""
    settings, session = tmp_db
    seed_vendors_from_contacts(session, settings.operator)
    seed_vendors_from_contacts(session, get_settings("airtel").operator)
    saf = resolve_vendor(session, operator_id="safaricom", msp_name="ATC")
    atl = resolve_vendor(session, operator_id="airtel", msp_name="ATC")
    assert saf is not None and atl is not None and saf.id != atl.id
    assert (saf.operator_id, atl.operator_id) == ("safaricom", "airtel")
    assert resolve_vendor(session, operator_id="airtel", msp_name="TETRANET") is None  # safaricom-only vendor


def test_resolve_picks_the_terms_in_force_at_the_incident_time(tmp_db):
    """A re-contracted vendor is a second row; the failure time decides which one an
    incident (and so its scorecard) falls under."""
    settings, session = tmp_db
    seed_vendors_from_contacts(session, settings.operator)  # EGYPRO from 2026-01-01, open-ended
    old = create_vendor(
        session, operator_id="safaricom", code="EGYPRO", display_name="Egypro (2024 MSA)", type="MSP",
        active_from=date(2024, 1, 1), active_to=date(2025, 12, 31), contract_ref="MSA-2024",
    )
    new = session.scalar(select(VendorRow).where(VendorRow.code == "EGYPRO", VendorRow.active_from == VENDOR_SEED_ACTIVE_FROM))
    assert resolve_vendor(session, operator_id="safaricom", msp_name="EGYPRO", as_of=date(2025, 6, 1)) is old
    assert resolve_vendor(session, operator_id="safaricom", msp_name="EGYPRO", as_of=datetime(2026, 3, 1, 12)) is new
    assert resolve_vendor(session, operator_id="safaricom", msp_name="EGYPRO", as_of=date(2023, 1, 1)) is None
    assert resolve_vendor(session, operator_id="safaricom", msp_name="EGYPRO") is new  # default: today


def test_create_vendor_validates_and_refuses_a_duplicate_natural_key(tmp_db):
    _settings, session = tmp_db
    create_vendor(session, operator_id="safaricom", code="zeta", display_name=None, type="oem", active_from=date(2026, 2, 1))
    row = session.scalar(select(VendorRow).where(VendorRow.code == "ZETA"))
    assert (row.type, row.display_name) == ("OEM", "Zeta")
    with pytest.raises(LookupError):
        create_vendor(session, operator_id="safaricom", code="ZETA", display_name="x", type="OEM", active_from=date(2026, 2, 1))
    with pytest.raises(ValueError):
        create_vendor(session, operator_id="safaricom", code="ZETA", display_name="x", type="GUESS", active_from=date(2026, 3, 1))
    with pytest.raises(ValueError):
        create_vendor(session, operator_id="safaricom", code="", display_name="x", type="OEM", active_from=date(2026, 3, 1))
    with pytest.raises(ValueError):
        create_vendor(
            session, operator_id="safaricom", code="ZETA", display_name="x", type="OEM",
            active_from=date(2026, 3, 1), active_to=date(2026, 2, 1),
        )


# --------------------------------------------------------------------------
# Backfilling incidents.vendor_id
# --------------------------------------------------------------------------


def test_backfill_stamps_only_null_vendor_ids_of_resolvable_incidents(tmp_db):
    settings, session = tmp_db
    seed_vendors_from_contacts(session, settings.operator)
    seed_vendors_from_contacts(session, get_settings("airtel").operator)
    egypro = resolve_vendor(session, operator_id="safaricom", msp_name="EGYPRO")

    a = _incident(session, msp_name="EGYPRO", number="A")
    b = _incident(session, msp_name=None, number="B")  # a field-engineer ticket
    c = _incident(session, msp_name="EGYPRO", number="C", vendor_id="keep-me")  # already pointed somewhere
    d = _incident(session, msp_name="Egypro", number="D", operator_id="airtel", region_code="NBI")
    e = _incident(session, msp_name="NOBODY_WE_KNOW", number="E")

    assert backfill_incident_vendor_ids(session, "safaricom") == 1
    assert a.vendor_id == egypro.id
    assert b.vendor_id is None
    assert c.vendor_id == "keep-me"  # only NULLs are touched
    assert d.vendor_id is None  # another operator's incident is not this operator's to stamp
    assert e.vendor_id is None  # unresolvable stays NULL rather than guessed
    assert backfill_incident_vendor_ids(session, "safaricom") == 0  # idempotent

    assert backfill_incident_vendor_ids(session, "airtel") == 1
    assert d.vendor_id == resolve_vendor(session, operator_id="airtel", msp_name="EGYPRO").id


def test_backfill_resolves_what_assign_actually_writes(tmp_db):
    """End to end through the real ASSIGN node: the seeded codes ARE the pool names."""
    settings, session = tmp_db
    seed_vendors_from_contacts(session, settings.operator)
    power_nbi = EventIngest(
        site_id="SFC-NBIE-BTS-01", site_name="Nairobi East BTS", site_type="BTS", region_code="NBI_E",
        alarm_code="POWER_GRID_FAIL", failure_domain="POWER", users_affected=3000,
    )
    power_rft = power_nbi.model_copy(update={"site_id": "SFC-RFT-BTS-01", "region_code": "RFT"})
    inc1 = process_event(session, settings, power_nbi)
    inc2 = process_event(session, settings, power_rft)
    assert (inc1.msp_name, inc2.msp_name) == ("EGYPRO", "TETRANET")
    assert inc1.vendor_id is None and inc2.vendor_id is None  # nothing writes it yet (Phase 1 column)

    assert backfill_incident_vendor_ids(session, "safaricom") == 2
    assert session.get(VendorRow, inc1.vendor_id).code == "EGYPRO"
    assert session.get(VendorRow, inc2.vendor_id).code == "TETRANET"


def test_attach_vendor_seeds_stamps_and_respects_an_existing_id(tmp_db):
    settings, session = tmp_db
    assert list_vendors(session, "safaricom") == []  # a fresh database
    inc = _incident(session, msp_name="TETRANET", number="T1")
    row = attach_vendor(session, inc, settings.operator)
    assert row is not None and row.code == "TETRANET" and inc.vendor_id == row.id
    assert len(list_vendors(session, "safaricom")) == 11  # seeded on the way

    fe = _incident(session, msp_name=None, number="T2")
    assert attach_vendor(session, fe, settings.operator) is None and fe.vendor_id is None

    kept = _incident(session, msp_name="EGYPRO", number="T3", vendor_id=row.id)
    assert attach_vendor(session, kept, settings.operator) is row  # never re-pointed


# --------------------------------------------------------------------------
# sla_terms: versioned, key spelling decided, every yaml_path real
# --------------------------------------------------------------------------


def _all_keys(node) -> set[str]:
    """Every mapping key at any depth of a parsed YAML document."""
    keys: set[str] = set()
    if isinstance(node, dict):
        for k, v in node.items():
            keys.add(str(k))
            keys |= _all_keys(v)
    elif isinstance(node, list):
        for item in node:
            keys |= _all_keys(item)
    return keys


def test_terms_load_from_config_with_the_pinned_version_and_key_spelling():
    terms = load_sla_terms()
    assert terms.path == DEFAULT_SLA_TERMS_PATH == ROOT / "config" / "sla_terms.yaml"
    assert terms.version == "2026-09" == sla_terms_version()
    assert BAND_KEYS == ("ack", "restore", "note_interval")
    # DECISION pinned: the spec's YAML-block spelling. Checked on the PARSED document, not
    # the text -- the file's header discusses the rejected "ack_minutes" spelling, which is
    # exactly why it must never appear as a key. The same keys are cfg.sla_minutes' fields.
    assert "ack_minutes" not in _all_keys(terms.raw)
    assert set(terms.default_bands) == set(PRIORITIES)
    for prio in PRIORITIES:
        assert set(terms.raw["sla_terms"]["default"][prio]) == set(BAND_KEYS), prio
    from noc_agents.config import SlaBand

    assert set(SlaBand.model_fields) == set(BAND_KEYS)


def test_default_bands_equal_the_operator_profile_sla_minutes_so_the_two_never_drift():
    """§7.6.1 "defaulting from sla_minutes": the derived block must equal the profile."""
    cfg = get_settings().operator
    terms = load_sla_terms(cfg=cfg)
    for prio in PRIORITIES:
        band = terms.bands_for(None, prio)
        profile = cfg.sla_minutes[prio]
        assert (band.ack, band.restore, band.note_interval) == (profile.ack, profile.restore, profile.note_interval), prio
        assert band.source == SOURCE_YAML
        assert band.yaml_path == f"sla_terms.default.{prio}"


def test_the_seed_copy_and_the_config_copy_agree_on_the_derived_default_block():
    """Both are derived from sla_minutes, so they must agree on it; the commercial blocks
    are deliberately NOT cross-pinned (a real deployment edits config/, not the demo seed)."""
    config = yaml.safe_load(DEFAULT_SLA_TERMS_PATH.read_text(encoding="utf-8"))["sla_terms"]
    seed = yaml.safe_load(SEED_TERMS.read_text(encoding="utf-8"))["sla_terms"]
    assert {p: config["default"][p] for p in PRIORITIES} == {p: seed["default"][p] for p in PRIORITIES}


def test_every_emitted_yaml_path_resolves_to_a_real_key_in_the_file():
    terms = load_sla_terms()
    for vendor_code in (None, "EGYPRO", "TETRANET", "NOBODY"):
        for prio in PRIORITIES:
            band = terms.bands_for(vendor_code, prio)
            assert terms.resolve_path(band.yaml_path) == {k: getattr(band, k) for k in BAND_KEYS}
            for key in BAND_KEYS:
                assert terms.resolve_path(band.path(key)) == getattr(band, key)
    with pytest.raises(KeyError):
        terms.bands_for(None, "P1").path("ack_minutes")
    with pytest.raises(KeyError):
        terms.resolve_path("sla_terms.vendors.EGYPRO.P1.ack_minutes")


def test_vendor_blocks_carry_credit_shapes_but_invent_no_bands():
    terms = load_sla_terms()
    egypro = terms.vendor("egypro")
    assert egypro is not None
    assert (egypro.credit_shape, egypro.credit_pct) == ("escalating_consecutive", (15.0, 30.0, 50.0))
    assert egypro.contract_is_synthetic and not egypro.has_contract  # the card must say "defaults, not contract"
    assert egypro.bands == {}  # no band override: it falls back to default and cites default
    assert terms.bands_for("EGYPRO", "P1") is terms.default_bands["P1"]
    tetranet = terms.vendor("TETRANET")
    assert (tetranet.credit_shape, tetranet.credit_pct) == ("per_occurrence", (25.0,))
    assert terms.vendor("NOBODY") is None
    assert terms.credit_shape_for("NOBODY") == terms.default_credit_shape == "none"
    assert terms.availability_target_pct == 99.5
    assert terms.scorecards["max_inferred_restore_pct"] == 10
    assert terms.regulatory["deadlines_hours"]["CA_OUTAGE_24H"] == 24


def _write_terms(tmp_path, body: dict) -> str:
    path = tmp_path / "terms.yaml"
    path.write_text(yaml.safe_dump(body), encoding="utf-8")
    return str(path)


def test_a_vendor_band_override_wins_and_cites_its_own_path(tmp_path):
    path = _write_terms(
        tmp_path,
        {
            "sla_terms": {
                "version": "2027-01-test",
                "default": {"P1": {"ack": 5, "restore": 60, "note_interval": 15}},
                "vendors": {"EGYPRO": {"credit_shape": "none", "P1": {"ack": 5, "restore": 45, "note_interval": 10}}},
            }
        },
    )
    terms = load_sla_terms(path)
    p1 = terms.bands_for("EGYPRO", "P1")
    assert (p1.restore, p1.note_interval, p1.yaml_path) == (45, 10, "sla_terms.vendors.EGYPRO.P1")
    assert terms.resolve_path(p1.path("restore")) == 45
    assert terms.bands_for("EGYPRO", "P2").yaml_path.startswith("sla_")  # falls through to a default
    assert terms.bands_for("TETRANET", "P1") is terms.default_bands["P1"]


def test_a_missing_default_band_falls_back_to_sla_minutes_and_says_so(tmp_path):
    cfg = get_settings().operator
    path = _write_terms(tmp_path, {"sla_terms": {"version": "v", "default": {"P1": {"ack": 1, "restore": 2, "note_interval": 3}}}})
    terms = load_sla_terms(path, cfg=cfg)
    assert terms.bands_for(None, "P1").source == SOURCE_YAML
    p3 = terms.bands_for(None, "P3")
    assert p3.source == SOURCE_SLA_MINUTES
    assert (p3.ack, p3.restore, p3.note_interval) == (cfg.sla_minutes["P3"].ack, cfg.sla_minutes["P3"].restore, cfg.sla_minutes["P3"].note_interval)
    assert p3.yaml_path == "sla_minutes.P3"  # honest: the value did NOT come from the terms file


def test_an_unversioned_file_is_refused(tmp_path):
    path = _write_terms(tmp_path, {"sla_terms": {"default": {"P1": {"ack": 5, "restore": 60, "note_interval": 15}}}})
    with pytest.raises(ValueError, match="version"):
        load_sla_terms(path)


def test_a_band_with_a_missing_key_is_refused(tmp_path):
    path = _write_terms(tmp_path, {"sla_terms": {"version": "v", "default": {"P1": {"ack": 5, "restore": 60}}}})
    with pytest.raises(ValueError, match="note_interval"):
        load_sla_terms(path)


def test_a_missing_file_is_refused_not_defaulted(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_sla_terms(tmp_path / "nope.yaml")


def test_sla_terms_path_env_override_is_honoured(tmp_path, monkeypatch):
    path = _write_terms(tmp_path, {"sla_terms": {"version": "override-1", "default": {}}})
    monkeypatch.setenv("SLA_TERMS_PATH", path)
    assert sla_terms_version() == "override-1"
    assert load_sla_terms().bands_for(None, "P1").source == SOURCE_SLA_MINUTES
    monkeypatch.delenv("SLA_TERMS_PATH")
    assert sla_terms_version() == "2026-09"


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

HUB_EVENT = {
    "site_id": "SFC-NBIE-HUB-EMB",
    "site_name": "Embakasi East Aggregation HUB",
    "site_type": "HUB",
    "region_code": "NBI_E",
    "alarm_code": "POWER_GRID_FAIL",
    "failure_domain": "POWER",
    "users_affected": 450000,
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A live API on its own SQLite file with the lane ON (same reload pattern as the
    restore-provenance tests). Tests that need it off flip the env themselves."""
    db = tmp_path / "vendors.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")
    monkeypatch.setenv("OPERATOR_PROFILE", "safaricom")
    monkeypatch.setenv(FLAG, "true")

    import noc_agents.config as cfg
    import noc_agents.db.models as models
    import noc_agents.main as main

    cfg.clear_settings_cache()
    models._engine = None
    models.SessionLocal = None
    importlib.reload(main)
    auth.reset_sessions()
    hub._history.clear()
    c = TestClient(main.app)
    c.__enter__()
    try:
        yield c
    finally:
        c.__exit__(None, None, None)
        hub._history.clear()
        auth.reset_sessions()
        models._engine = None
        models.SessionLocal = None
        cfg.clear_settings_cache()
        monkeypatch.delenv(FLAG, raising=False)
        importlib.reload(main)


def test_routes_are_a_404_while_the_flag_is_off(client, monkeypatch):
    """Off = exactly the surface before this lane existed: an unknown path."""
    monkeypatch.setenv(FLAG, "false")
    assert client.get("/api/v1/vendors").status_code == 404
    assert client.post("/api/v1/vendors", json={"code": "X", "active_from": "2026-01-01"}).status_code == 404
    assert client.post("/api/v1/vendors/backfill").status_code == 404
    assert FLAG in client.get("/api/v1/vendors").json()["detail"]
    monkeypatch.setenv(FLAG, "true")  # read per request, no reload needed
    assert client.get("/api/v1/vendors").status_code == 200


def test_get_vendors_seeds_from_the_profile_and_lists_only_this_operator(client):
    from noc_agents.db.models import get_session

    session = get_session()
    try:
        seed_vendors_from_contacts(session, get_settings("airtel").operator)  # the other tenant, same file
        session.commit()
    finally:
        session.close()

    rows = client.get("/api/v1/vendors").json()
    assert {r["code"] for r in rows} == set(get_settings().operator.msp_contacts)
    assert {r["operator_id"] for r in rows} == {"safaricom"}
    assert all(r["active_from"] == "2026-01-01" and r["active_to"] is None for r in rows)
    assert rows == sorted(rows, key=lambda r: r["code"])
    assert client.get("/api/v1/vendors").json() == rows  # idempotent: a second GET seeds nothing new
    assert len(client.get("/api/v1/vendors", params={"active_only": "true"}).json()) == len(rows)


def test_post_vendor_creates_then_409s_on_the_same_key_and_400s_on_a_bad_type(client):
    body = {"code": "zte", "display_name": "ZTE Kenya", "type": "OEM", "active_from": "2026-03-01", "contacts": {"email": "zte@example.com"}}
    r = client.post("/api/v1/vendors", json=body)
    assert r.status_code == 200, r.text
    v = r.json()["vendor"]
    assert (v["code"], v["type"], v["display_name"], v["contacts"]) == ("ZTE", "OEM", "ZTE Kenya", {"email": "zte@example.com"})
    assert client.post("/api/v1/vendors", json=body).status_code == 409
    assert client.post("/api/v1/vendors", json={**body, "active_from": "2027-01-01", "type": "GUESS"}).status_code == 400
    assert client.post("/api/v1/vendors", json={**body, "active_from": "2027-01-01", "active_to": "2026-01-01"}).status_code == 400
    assert "ZTE" in {r["code"] for r in client.get("/api/v1/vendors").json()}


def test_backfill_route_stamps_incidents_and_is_idempotent(client):
    from noc_agents.db.models import get_session

    inc = client.post("/api/v1/events", json=HUB_EVENT).json()["incident"]
    assert inc["msp_name"] == "EGYPRO"
    assert client.post("/api/v1/vendors/backfill").json() == {"ok": True, "stamped": 1}
    assert client.post("/api/v1/vendors/backfill").json() == {"ok": True, "stamped": 0}
    session = get_session()
    try:
        row = session.get(IncidentRow, inc["id"])
        assert session.get(VendorRow, row.vendor_id).code == "EGYPRO"
    finally:
        session.close()
