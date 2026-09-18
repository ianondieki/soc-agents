"""Evidence packs — spec §7.6.1, §7.6.4 ``build_evidence_pack``, §7.6.8 "evidence pack hash stable".

An evidence pack is what the CA, a vendor or a tribunal is shown, and ``sha256`` is what makes
"these are still the same bytes" provable three years later (licence Condition 12.2). The exit
criterion for this lane is a **stable hash**, so most of this file is about the three ways a
hash silently stops being stable — dict iteration order, a generation timestamp inside the
hashed content, and unordered rows out of the database — and pins each one shut.

The rest pins what a pack is allowed to claim: a reversed stop-clock deducts nothing, an
unknown restore provenance is reported as unknown rather than guessed, and an older pack is
never rewritten when the facts move.
"""

from __future__ import annotations

import importlib
import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from noc_agents.api import auth
from noc_agents.db.models import BroadcastRow, IncidentRow
from noc_agents.db.models_regulatory import EvidencePackRow
from noc_agents.realtime.hub import hub
from noc_agents.services.evidence import (
    PACK_VERSION,
    SCC_SOURCE_TABLE,
    build_pack,
    canonical_bytes,
    get_or_build_pack,
    latest_pack,
    pack_sha256,
    scc_breakdown,
)

try:  # the stop-clock table belongs to the vendors lane; this lane degrades without it
    from noc_agents.db.models_vendors import ClockEventRow
except Exception:  # pragma: no cover - only while that module is mid-flight
    ClockEventRow = None  # type: ignore[assignment]

# 12:00 EAT on the day of the reference storm, as stored (naive UTC).
FAILURE = datetime(2026, 9, 16, 9, 0, 0)
RESTORED = FAILURE + timedelta(hours=5)  # 300 raw minutes

#: The §7.6.1 ``pack_json`` field list, verbatim. If a key is renamed or dropped, this fails
#: before any consumer of a stored pack does.
SPEC_FIELDS = (
    "outage_start_at",
    "restored_at",
    "restored_source",
    "adjusted_duration_min",
    "scc_breakdown",
    "users_affected",
    "region",
    "county",
    "planned",
    "force_majeure",
    "notification_timestamps",
    "broadcasts",
)


#: The alarm that opens a real incident through the live pipeline, for the route tests.
HUB_EVENT = {
    "source": "NMS",
    "site_id": "SFC-MTK-HUB-THK",
    "alarm_code": "POWER_FAIL",
    "message": "Thika hub on battery, mains down",
    "severity": "CRITICAL",
    "users_affected": 450000,
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A live API on its own SQLite file (the reload pattern the auth and restore tests use)."""
    db = tmp_path / "evidence.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")
    monkeypatch.setenv("OPERATOR_PROFILE", "safaricom")

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
        importlib.reload(main)


def _open_incident(client: TestClient) -> str:
    r = client.post("/api/v1/events", json=HUB_EVENT)
    assert r.status_code == 200, r.text
    return r.json()["incident"]["id"]


def _incident(session, *, number: str = "INC000501", operator_id: str = "safaricom", **overrides) -> IncidentRow:
    values = dict(
        operator_id=operator_id,
        incident_number=number,
        status="RESTORED",
        priority="P1",
        users_affected=250000,
        site_id="SFC-MTK-HUB-THK",
        site_name="Thika Hub",
        site_type="HUB",
        region_code="MTK",
        county="Kiambu",
        correlation_fingerprint="fp-evidence",
        failure_time=FAILURE,
        restored_at=RESTORED,
        restored_source="MARK_RESTORED",
    )
    values.update(overrides)
    inc = IncidentRow(**values)
    session.add(inc)
    session.flush()
    return inc


def _scc(session, inc, *, code: str, start_offset_h: float, end_offset_h: float | None, reversed_at=None):
    ev = ClockEventRow(
        incident_id=inc.id,
        scc_code=code,
        started_at=FAILURE + timedelta(hours=start_offset_h),
        ended_at=None if end_offset_h is None else FAILURE + timedelta(hours=end_offset_h),
        opened_by="Grace Wanjiru",
        opened_role="shift_supervisor",
        opened_at=FAILURE + timedelta(hours=start_offset_h),
        reason="utility power out at site",
        reversed_at=reversed_at,
        reversal_reason="claim withdrawn" if reversed_at else None,
    )
    session.add(ev)
    session.flush()
    return ev


# ---------------------------------------------------------------------------------------
# The exit criterion: the hash does not move
# ---------------------------------------------------------------------------------------


def test_generating_the_same_pack_twice_produces_the_same_hash(tmp_db):
    """§7.6.8's exit criterion, stated as directly as it can be stated."""
    _settings, session = tmp_db
    inc = _incident(session)

    first = build_pack(session, inc)
    second = build_pack(session, inc)

    assert first == second
    assert pack_sha256(first) == pack_sha256(second)
    assert canonical_bytes(first) == canonical_bytes(second)


def test_the_hash_does_not_depend_on_the_order_keys_were_inserted(tmp_db):
    """Python dicts keep insertion order, so two code paths building the same mapping in
    different orders would otherwise produce different bytes and a different hash."""
    _settings, session = tmp_db
    inc = _incident(session)

    pack = build_pack(session, inc)
    shuffled = {k: pack[k] for k in reversed(list(pack))}

    assert list(shuffled) != list(pack)  # genuinely a different insertion order
    assert pack_sha256(shuffled) == pack_sha256(pack)


def test_no_generation_timestamp_is_inside_the_hashed_content(tmp_db):
    """``generated_at`` / ``generated_by`` are COLUMNS. Inside ``pack_json`` they would make
    every regeneration a new hash and the stability criterion unreachable by construction."""
    _settings, session = tmp_db
    inc = _incident(session)

    row, created = get_or_build_pack(session, inc, generated_by="Grace Wanjiru")

    assert created is True
    stored = json.loads(row.pack_json)
    assert "generated_at" not in stored
    assert "generated_by" not in stored
    assert row.generated_at is not None and row.generated_by == "Grace Wanjiru"
    # and the stored bytes ARE the bytes the hash was taken over
    assert row.sha256 == pack_sha256(stored)
    assert row.pack_json.encode("ascii") == canonical_bytes(stored)


def test_regenerating_an_unchanged_incident_appends_no_second_row(tmp_db):
    """``refresh=true`` on unchanged facts must return the same row, not accumulate rows —
    otherwise "the hash is stable" would depend on nobody pressing refresh."""
    _settings, session = tmp_db
    inc = _incident(session)

    first, created_first = get_or_build_pack(session, inc, generated_by="Grace Wanjiru")
    second, created_second = get_or_build_pack(session, inc, generated_by="Grace Wanjiru", refresh=True)

    assert created_first is True and created_second is False
    assert second.id == first.id and second.sha256 == first.sha256
    assert session.query(EvidencePackRow).count() == 1


def test_a_changed_fact_appends_a_new_pack_and_leaves_the_old_hash_alone(tmp_db):
    """The table is append-only: an older pack keeps its own bytes, because its whole value
    is that the bytes behind its hash cannot have moved."""
    _settings, session = tmp_db
    inc = _incident(session)
    first, _ = get_or_build_pack(session, inc, generated_by="Grace Wanjiru")
    original_sha = first.sha256

    inc.users_affected = 450000
    session.flush()
    second, created = get_or_build_pack(session, inc, generated_by="Grace Wanjiru", refresh=True)

    assert created is True
    assert second.id != first.id
    assert second.sha256 != original_sha
    assert first.sha256 == original_sha  # untouched
    assert session.query(EvidencePackRow).count() == 2
    assert latest_pack(session, inc.id).id == second.id


# ---------------------------------------------------------------------------------------
# What a pack is allowed to say
# ---------------------------------------------------------------------------------------


def test_the_pack_carries_exactly_the_fields_the_spec_lists(tmp_db):
    _settings, session = tmp_db
    inc = _incident(session)

    pack = build_pack(session, inc)

    for key in SPEC_FIELDS:
        assert key in pack, f"§7.6.1 pack_json field {key!r} is missing"
    assert pack["pack_version"] == PACK_VERSION  # hashed, so a shape change cannot collide
    assert pack["incident_number"] == "INC000501"


def test_every_timestamp_in_a_pack_is_stamped_utc(tmp_db):
    """A naive ISO string is read in Nairobi as EAT, which backdates every fact in the pack
    by three hours (defect #41). A pack that is read wrong is not evidence."""
    _settings, session = tmp_db
    inc = _incident(session)

    pack = build_pack(session, inc)

    assert pack["outage_start_at"] == "2026-09-16T09:00:00Z"
    assert pack["restored_at"].endswith("Z")


def test_an_unknown_restore_provenance_is_reported_as_unknown_not_guessed(tmp_db):
    """§7.6.2's data-quality gate exists because "we do not know how the restore time was set"
    is a fact a vendor is entitled to see on the evidence it is billed against."""
    _settings, session = tmp_db
    inc = _incident(session, restored_source=None)

    pack = build_pack(session, inc)

    assert pack["restored_source"] is None
    assert pack["adjusted_duration_min"] == 300  # still computed; the provenance is the caveat


def test_an_unrestored_incident_has_no_duration_rather_than_a_zero(tmp_db):
    """Zero minutes and "not restored yet" are different claims; only one of them is true."""
    _settings, session = tmp_db
    inc = _incident(session, status="IN_PROGRESS", restored_at=None, restored_source=None)

    pack = build_pack(session, inc)

    assert pack["restored_at"] is None
    assert pack["adjusted_duration_min"] is None
    assert pack["raw_duration_min"] is None


def test_broadcast_bodies_are_not_copied_into_the_pack(tmp_db):
    """A broadcast body is already stored on ``broadcasts``. Copying personal-data-bearing
    text into a second record with a three-year retention floor buys no evidential value."""
    _settings, session = tmp_db
    inc = _incident(session)
    session.add(
        BroadcastRow(
            incident_id=inc.id,
            channel="SMS",
            audience="RNIO",
            message="0722000000 please attend Thika Hub",
            status="SENT",
            sent_at=RESTORED,
        )
    )
    session.flush()

    pack = build_pack(session, inc)

    assert len(pack["broadcasts"]) == 1
    assert "message" not in pack["broadcasts"][0]
    assert "0722000000" not in json.dumps(pack)


# ---------------------------------------------------------------------------------------
# Stop clocks (the vendors lane's table; the pack degrades honestly without it)
# ---------------------------------------------------------------------------------------


@pytest.mark.skipif(ClockEventRow is None, reason="incident_clock_events model not available in this tree")
def test_only_a_non_reversed_stop_clock_is_deducted_from_the_duration(tmp_db):
    """§7.6.2: a reversed event stays in the breakdown (the claim was made and must remain
    auditable) but deducts nothing."""
    _settings, session = tmp_db
    inc = _incident(session)
    _scc(session, inc, code="UTILITY_POWER", start_offset_h=1, end_offset_h=2)  # 60 min
    _scc(session, inc, code="SITE_ACCESS_DENIED", start_offset_h=3, end_offset_h=4, reversed_at=RESTORED)

    pack = build_pack(session, inc)

    assert pack["scc_source"] == SCC_SOURCE_TABLE
    assert len(pack["scc_breakdown"]) == 2
    assert pack["scc_minutes_deducted"] == 60
    assert pack["raw_duration_min"] == 300
    assert pack["adjusted_duration_min"] == 240
    reversed_entry = [e for e in pack["scc_breakdown"] if e["reversed"]][0]
    assert reversed_entry["deducted_minutes"] == 0


@pytest.mark.skipif(ClockEventRow is None, reason="incident_clock_events model not available in this tree")
def test_only_the_part_of_a_stop_clock_inside_the_outage_window_is_deducted(tmp_db):
    """An event that started before the failure, or ran past the restore, deducts only its
    overlap — never its whole length."""
    _settings, session = tmp_db
    inc = _incident(session)
    _scc(session, inc, code="UTILITY_POWER", start_offset_h=-2, end_offset_h=1)  # 2 h before, 1 h inside

    pack = build_pack(session, inc)

    assert pack["scc_minutes_deducted"] == 60
    assert pack["adjusted_duration_min"] == 240


@pytest.mark.skipif(ClockEventRow is None, reason="incident_clock_events model not available in this tree")
def test_a_still_open_stop_clock_deducts_nothing_so_the_pack_does_not_depend_on_when_it_was_built(tmp_db):
    """Extrapolating an open event to "now" would make the pack's arithmetic a function of
    generation time — the one thing that destroys hash stability."""
    _settings, session = tmp_db
    inc = _incident(session)
    _scc(session, inc, code="UTILITY_POWER", start_offset_h=1, end_offset_h=None)

    pack = build_pack(session, inc)

    assert pack["scc_breakdown"][0]["ended_at"] is None
    assert pack["scc_breakdown"][0]["deducted_minutes"] == 0
    assert pack["adjusted_duration_min"] == 300
    assert pack_sha256(build_pack(session, inc)) == pack_sha256(pack)


@pytest.mark.skipif(ClockEventRow is None, reason="incident_clock_events model not available in this tree")
def test_the_breakdown_order_is_fixed_by_the_sort_key_not_by_the_query_planner(tmp_db):
    """``SELECT`` without ``ORDER BY`` is unordered by definition, and two events opened in the
    same second would otherwise be free to swap places and move the hash."""
    _settings, session = tmp_db
    inc = _incident(session)
    _scc(session, inc, code="WIRING_NOT_OURS", start_offset_h=2, end_offset_h=3)
    _scc(session, inc, code="UTILITY_POWER", start_offset_h=1, end_offset_h=2)
    _scc(session, inc, code="FORCE_MAJEURE", start_offset_h=1, end_offset_h=2)  # ties on started_at

    rows, _source = scc_breakdown(session, inc)
    codes = [r["scc_code"] for r in rows]

    assert codes == ["FORCE_MAJEURE", "UTILITY_POWER", "WIRING_NOT_OURS"]
    assert [r["scc_code"] for r in scc_breakdown(session, inc)[0]] == codes


@pytest.mark.skipif(ClockEventRow is None, reason="incident_clock_events model not available in this tree")
def test_a_live_force_majeure_stop_clock_raises_the_pack_flag(tmp_db):
    """The first thing a regulator asks about a 24-hour outage. A reversed claim does not count."""
    _settings, session = tmp_db
    inc = _incident(session)
    _scc(session, inc, code="FORCE_MAJEURE", start_offset_h=1, end_offset_h=2)
    assert build_pack(session, inc)["force_majeure"] is True

    other = _incident(session, number="INC000502")
    _scc(session, other, code="FORCE_MAJEURE", start_offset_h=1, end_offset_h=2, reversed_at=RESTORED)
    assert build_pack(session, other)["force_majeure"] is False


# ---------------------------------------------------------------------------------------
# Operator isolation (§8)
# ---------------------------------------------------------------------------------------


def test_another_operators_pack_is_invisible_even_by_incident_id(tmp_db):
    """Every read goes through ``_owned``, so the other operator's rows are excluded by the
    WHERE clause and not by a check after the fetch."""
    _settings, session = tmp_db
    theirs = _incident(session, number="ATL-000001", operator_id="airtel")
    session.add(
        EvidencePackRow(
            operator_id="airtel",
            incident_id=theirs.id,
            generated_by="their supervisor",
            sha256="deadbeef",
            pack_json="{}",
        )
    )
    session.flush()

    assert latest_pack(session, theirs.id) is None


# ---------------------------------------------------------------------------------------
# The route (§7.6.3 "generates or returns; hash stable")
# ---------------------------------------------------------------------------------------


def test_the_route_returns_the_same_hash_on_a_second_read(client):
    inc_id = _open_incident(client)

    first = client.get(f"/api/v1/incidents/{inc_id}/evidence-pack")
    second = client.get(f"/api/v1/incidents/{inc_id}/evidence-pack")

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["created"] is True and second.json()["created"] is False
    assert first.json()["sha256"] == second.json()["sha256"]
    assert first.json()["id"] == second.json()["id"]


def test_the_route_is_available_with_the_regulatory_flag_off(client, monkeypatch):
    """An evidence pack notifies nobody. Being able to produce one for a vendor dispute must
    not depend on whether regulatory notifications are armed."""
    monkeypatch.delenv("REGULATORY_ENABLED", raising=False)
    inc_id = _open_incident(client)

    r = client.get(f"/api/v1/incidents/{inc_id}/evidence-pack")

    assert r.status_code == 200, r.text
    assert len(r.json()["sha256"]) == 64


def test_an_unknown_incident_is_404_not_500(client):
    r = client.get("/api/v1/incidents/does-not-exist/evidence-pack")
    assert r.status_code == 404
