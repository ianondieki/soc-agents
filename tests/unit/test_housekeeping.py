"""HousekeepingAgent (spec §5.3.22, §9.4, §9.6) — the agent that deletes data.

This is the most dangerous code in Phase 4, so these tests are written against the two
failure modes that actually matter, in this order:

**Deleting something it must not.** CA Network Facilities Provider Tier 1 licence Condition
12.2 requires operational records for at least 3 years, so a retention job that removes an
incident record early is a regulatory breach, not a bug. The tests therefore seed rows on
BOTH sides of every boundary and assert the survivors exactly — not "some rows went", but
"these ids are still here and those are not" — prove the shipped policy classifies the
incident tables as never-delete, prove an unclassified table is never touched, prove a
policy that would delete under the floor is refused outright rather than partially honoured,
and prove a second operator's rows are never in scope.

**Sending something twice.** The outbox sweep runs over the same table the dispatcher
drains. Releasing a stale 120 s lease looks like tidying up and is actually a re-dispatch,
so there is a test that the sweep never writes ``outbox.status`` and leaves a stale CLAIMED
row exactly as it found it.

Plus the spec's own exit criteria: idempotency (a second run deletes nothing and changes
nothing), a seeded MSISDN in a SENT payload raising ``redaction.miss``, personal fields
pseudonymised while the network fields beside them stay byte-identical, and the whole job
inert while ``HOUSEKEEPING_ENABLED`` is unset.

No network, no clock dependence: every duty takes an injectable ``now``.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from noc_agents.db.models import AuditRow, ExternalSignalRow, IncidentRow, OutboxRow
from noc_agents.realtime.hub import hub
from noc_agents.scheduler.loop import job_enabled
from noc_agents.services import housekeeping as hk

OTHER_OPERATOR = "another_operator"  # a second controller in the same file (§10.1 multi-tenancy)
NOW = datetime(2026, 9, 18, 0, 30, 0)  # 03:30 EAT, the job's slot


# ------------------------------------------------------------------------------- fixtures


@pytest.fixture()
def clean_hub():
    hub._history.clear()
    yield hub
    hub._history.clear()


@pytest.fixture(autouse=True)
def flags_off(monkeypatch):
    """Both keys deletion needs are explicitly OFF unless a test says otherwise, so a value
    exported in a developer's shell can never turn a dry-run test into a deleting one."""
    monkeypatch.setenv(hk.ENABLED_ENV, "false")
    monkeypatch.setenv(hk.APPLY_ENV, "false")
    monkeypatch.delenv(hk.POLICY_PATH_ENV, raising=False)


def write_policy(tmp_path: Path, body: str, name: str = "retention.yaml") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def signals_policy(tmp_path: Path, *, days: int = 30, dry_run: bool = True) -> hk.RetentionPolicy:
    """A policy that deletes from ``external_signals``.

    The shipped policy deliberately deletes from nothing that exists in this schema yet
    (§9.4's deletable classes are complainant and contract tables the later lanes own), so
    the boundary tests drive the ENGINE against a table that really is there. Using a real
    table with real columns is the point: a purge tested only against a fake one proves
    nothing about the SQL it will run in production.
    """
    return hk.load_policy(
        write_policy(
            tmp_path,
            f"""
version: 1
posture:
  dry_run: {str(dry_run).lower()}
licence_floor_days: 1095
backups: {{dir: data/backups, keep_daily: 14}}
outbox: {{archive_after_days: 90}}
redaction_scan: {{lookback_hours: 24}}
classes:
  cache:
    action: delete
    days: {days}
    legal_basis: test fixture only
tables:
  external_signals:
    class: cache
    timestamp_column: fetched_at
""",
            name=f"signals_{days}_{dry_run}.yaml",
        )
    )


def seed_signal(session, *, operator_id: str, age_days: float, source: str = "OPEN_METEO", now: datetime = NOW) -> str:
    row = ExternalSignalRow(
        operator_id=operator_id,
        source=source,
        source_url="https://example.invalid/none",
        region_code="NBI_E",
        fetched_at=now - timedelta(days=age_days),
        valid_until=now,
        payload_json="{}",
        external_id=f"{source}:{operator_id}:{age_days}",
    )
    session.add(row)
    session.flush()
    return row.id


def seed_incident(
    session,
    *,
    operator_id: str,
    age_days: float,
    number: str,
    assignee: str | None = "Kevin Ochieng",
    fe: str | None = "James Mwangi",
    rnio: str | None = "Grace Wanjiru",
    restored_by: str | None = "Peter Kamau",
    notes: str | None = "Kevin Ochieng holds the gate key; call 0712 345 678 or kevin@example.com",
    now: datetime = NOW,
) -> str:
    row = IncidentRow(
        operator_id=operator_id,
        incident_number=number,
        site_id="SFC-MTK-BTS-MCH04",
        site_name="Machakos Town BTS",
        region_code="MTK",
        correlation_fingerprint=f"fp-{number}",
        created_at=now - timedelta(days=age_days),
        updated_at=now - timedelta(days=age_days),
        users_affected=3200,
        failure_domain="POWER",
        alarm_code="SITE_DOWN",
        assignee_type="FE",
        assignee_name=assignee,
        fe_name=fe,
        rnio_name=rnio,
        restored_by=restored_by,
        access_notes=notes,
    )
    session.add(row)
    session.flush()
    return row.id


def seed_outbox(
    session,
    *,
    operator_id: str,
    status: str,
    age_days: float,
    payload: dict,
    kind: str = "SMS",
    key: str | None = None,
    claimed_age_s: float | None = None,
    now: datetime = NOW,
) -> str:
    ts = now - timedelta(days=age_days)
    row = OutboxRow(
        operator_id=operator_id,
        kind=kind,
        idempotency_key=key or f"{kind}:{operator_id}:{status}:{age_days}:{len(payload)}",
        payload_json=json.dumps(payload),
        envelope_json=json.dumps({"identifier": "env-1"}),
        status=status,
        created_at=ts,
        updated_at=ts,
        sent_at=ts if status == "SENT" else None,
        claimed_at=(now - timedelta(seconds=claimed_age_s)) if claimed_age_s is not None else None,
        claimed_by="worker-1" if claimed_age_s is not None else None,
        attempts=1,
    )
    session.add(row)
    session.flush()
    return row.id


def signal_ids(session, operator_id: str) -> set[str]:
    return set(
        session.scalars(select(ExternalSignalRow.id).where(ExternalSignalRow.operator_id == operator_id)).all()
    )


def row_snapshot(session, model, row_id: str) -> dict:
    """Every column of one row, as plain values, so "nothing changed" can be asserted on the
    whole row rather than on the handful of columns the test remembered to name."""
    session.expire_all()
    row = session.get(model, row_id)
    return {c.name: getattr(row, c.name) for c in model.__table__.columns}


# ------------------------------------------------------------- the shipped policy is safe


def test_the_shipped_retention_policy_loads_and_passes_its_own_safety_validation():
    policy = hk.load_policy()
    assert hk.validate_policy(policy) == []
    assert policy.licence_floor_days >= hk.LICENCE_FLOOR_DAYS


def test_the_shipped_policy_is_dry_run_so_a_first_release_counts_instead_of_deleting():
    assert hk.load_policy().dry_run is True


def test_the_shipped_policy_never_deletes_rows_from_a_table_it_classes_as_network_facts():
    policy = hk.load_policy()
    assert policy.tables["incidents"].class_name == "network_facts"
    incident_rule = policy.classes["network_facts"]
    assert incident_rule.action == hk.KEEP
    assert incident_rule.licence_floor is True
    deleting = [t for t, entry in policy.tables.items() if policy.classes[entry.class_name].action == hk.DELETE]
    assert "incidents" not in deleting
    assert "audit_events" not in deleting


def test_every_class_in_the_shipped_policy_states_the_legal_basis_it_relies_on():
    # A deletion nobody can justify in writing does not belong in the policy file.
    for rule in hk.load_policy().classes.values():
        assert rule.legal_basis, f"class {rule.name} has no legal_basis"


def test_a_table_absent_from_the_policy_defaults_to_keep_rather_than_to_delete():
    policy = hk.load_policy()
    assert "work_notes" not in policy.tables
    assert policy.rule_for("work_notes").action == hk.KEEP
    assert policy.rule_for("a_table_invented_next_year").action == hk.KEEP


# ------------------------------------------------------------- the licence floor is armed


def test_a_policy_that_would_delete_a_licence_floor_class_is_refused_outright(tmp_path):
    body = """
version: 1
posture: {dry_run: false}
classes:
  network_facts:
    action: delete
    days: 4000
    licence_floor: true
    legal_basis: someone shortened this
tables:
  incidents: {class: network_facts, timestamp_column: created_at}
"""
    with pytest.raises(hk.RetentionPolicyError) as exc:
        hk.load_policy(write_policy(tmp_path, body))
    assert "Condition 12.2" in str(exc.value)


def test_a_delete_rule_under_three_years_on_a_licence_floor_class_is_refused(tmp_path):
    body = """
version: 1
classes:
  network_facts: {action: delete, days: 30, licence_floor: true, legal_basis: too short}
tables:
  incidents: {class: network_facts, timestamp_column: created_at}
"""
    problems = str(hk.validate_policy(_unvalidated(tmp_path, body)))
    assert "licence_floor" in problems or "below the licence floor" in problems


def test_the_yaml_may_raise_the_licence_floor_but_can_never_lower_it(tmp_path):
    lowered = hk.load_policy(write_policy(tmp_path, "version: 1\nlicence_floor_days: 30\nclasses: {}\ntables: {}\n"))
    assert lowered.licence_floor_days == hk.LICENCE_FLOOR_DAYS
    raised = hk.load_policy(
        write_policy(tmp_path, "version: 1\nlicence_floor_days: 2000\nclasses: {}\ntables: {}\n", name="raised.yaml")
    )
    assert raised.licence_floor_days == 2000


def test_an_unknown_action_is_a_policy_error_rather_than_something_the_engine_guesses_at(tmp_path):
    body = "version: 1\nclasses:\n  odd: {action: incinerate, legal_basis: x}\ntables:\n  incidents: {class: odd}\n"
    with pytest.raises(hk.RetentionPolicyError) as exc:
        hk.load_policy(write_policy(tmp_path, body))
    assert "incinerate" in str(exc.value)


def test_a_missing_dry_run_key_reads_as_dry_run_because_the_safe_default_is_not_to_delete(tmp_path):
    policy = hk.load_policy(write_policy(tmp_path, "version: 1\nclasses: {}\ntables: {}\n"))
    assert policy.dry_run is True
    assert hk.applying(policy) is False


def test_a_delete_rule_with_no_timestamp_column_has_no_age_to_measure_and_is_refused(tmp_path):
    body = """
version: 1
classes:
  cache: {action: delete, days: 30, legal_basis: x}
tables:
  external_signals: {class: cache}
"""
    with pytest.raises(hk.RetentionPolicyError) as exc:
        hk.load_policy(write_policy(tmp_path, body))
    assert "timestamp_column" in str(exc.value)


def test_an_unreadable_policy_file_stops_the_purge_instead_of_defaulting_to_anything(tmp_path):
    with pytest.raises(hk.RetentionPolicyError):
        hk.load_policy(tmp_path / "does_not_exist.yaml")


def _unvalidated(tmp_path: Path, body: str) -> hk.RetentionPolicy:
    """Build a policy object without load_policy's raise, so validate_policy can be asserted
    on directly (load_policy is the same check plus a raise)."""
    try:
        return hk.load_policy(write_policy(tmp_path, body, name="unvalidated.yaml"))
    except hk.RetentionPolicyError:
        pass
    import yaml

    raw = yaml.safe_load(body)
    classes = {
        name: hk.RetentionRule(
            name=name,
            action=entry.get("action", hk.KEEP),
            days=entry.get("days"),
            keep=entry.get("keep"),
            licence_floor=bool(entry.get("licence_floor", False)),
            legal_basis=entry.get("legal_basis", ""),
        )
        for name, entry in (raw.get("classes") or {}).items()
    }
    tables = {
        name: hk.TableRule(table=name, class_name=entry.get("class", ""), timestamp_column=entry.get("timestamp_column"))
        for name, entry in (raw.get("tables") or {}).items()
    }
    return hk.RetentionPolicy(
        path=Path("<test>"), version=1, dry_run=True, licence_floor_days=hk.LICENCE_FLOOR_DAYS,
        classes=classes, tables=tables, backup_dir="data/backups", backup_keep_daily=14,
        outbox_archive_after_days=90, redaction_lookback_hours=24,
    )


# ----------------------------------------------------------------- purge: exact survivors


def test_purge_deletes_only_the_rows_past_the_cutoff_and_leaves_every_boundary_row_standing(tmp_db, tmp_path):
    settings, session = tmp_db
    op = settings.operator.operator_id
    well_past = seed_signal(session, operator_id=op, age_days=31)
    just_past = seed_signal(session, operator_id=op, age_days=30.001, source="MET_NORWAY")
    just_inside = seed_signal(session, operator_id=op, age_days=29.999, source="KMD_CAP")
    fresh = seed_signal(session, operator_id=op, age_days=0, source="GLOFAS")
    session.commit()

    policy = signals_policy(tmp_path, days=30, dry_run=False)
    report = hk.purge_expired(session, settings, policy, now=NOW, apply=True)
    session.commit()

    assert report.applied is True
    assert report.deleted == 2
    # Named survivors, not a count: the boundary rows are the ones a cutoff bug eats first.
    assert signal_ids(session, op) == {just_inside, fresh}
    assert well_past not in signal_ids(session, op)
    assert just_past not in signal_ids(session, op)


def test_a_dry_run_purge_counts_what_it_would_delete_and_removes_nothing(tmp_db, tmp_path):
    settings, session = tmp_db
    op = settings.operator.operator_id
    doomed = seed_signal(session, operator_id=op, age_days=400)
    kept = seed_signal(session, operator_id=op, age_days=1, source="MET_NORWAY")
    session.commit()

    report = hk.purge_expired(session, settings, signals_policy(tmp_path, days=30, dry_run=True), now=NOW)
    session.commit()

    assert report.applied is False
    assert report.matched == 1  # it would have taken exactly one row
    assert report.deleted == 0
    assert signal_ids(session, op) == {doomed, kept}


def test_purge_is_idempotent_so_a_second_run_the_same_night_deletes_nothing(tmp_db, tmp_path):
    settings, session = tmp_db
    op = settings.operator.operator_id
    seed_signal(session, operator_id=op, age_days=90)
    survivor = seed_signal(session, operator_id=op, age_days=2, source="MET_NORWAY")
    session.commit()
    policy = signals_policy(tmp_path, days=30, dry_run=False)

    first = hk.purge_expired(session, settings, policy, now=NOW, apply=True)
    session.commit()
    second = hk.purge_expired(session, settings, policy, now=NOW, apply=True)
    session.commit()

    assert first.deleted == 1
    assert second.deleted == 0  # §5.3.22 exit criterion
    assert signal_ids(session, op) == {survivor}


def test_purge_never_touches_another_operators_rows_however_old_they_are(tmp_db, tmp_path):
    settings, session = tmp_db
    op = settings.operator.operator_id
    mine = seed_signal(session, operator_id=op, age_days=400)
    theirs_old = seed_signal(session, operator_id=OTHER_OPERATOR, age_days=4000, source="MET_NORWAY")
    theirs_new = seed_signal(session, operator_id=OTHER_OPERATOR, age_days=1, source="KMD_CAP")
    session.commit()

    report = hk.purge_expired(session, settings, signals_policy(tmp_path, days=30, dry_run=False), now=NOW, apply=True)
    session.commit()

    assert report.deleted == 1
    assert mine not in signal_ids(session, op)
    # Multi-tenancy is a data-protection boundary: crossing it deletes another controller's records.
    assert signal_ids(session, OTHER_OPERATOR) == {theirs_old, theirs_new}


def test_a_table_with_no_rule_is_never_purged_even_when_every_row_is_ancient(tmp_db, tmp_path):
    settings, session = tmp_db
    op = settings.operator.operator_id
    ancient = seed_signal(session, operator_id=op, age_days=5000)
    session.commit()

    empty = hk.load_policy(write_policy(tmp_path, "version: 1\nposture: {dry_run: false}\nclasses: {}\ntables: {}\n"))
    report = hk.purge_expired(session, settings, empty, now=NOW, apply=True)
    session.commit()

    assert report.tables == []
    assert report.deleted == 0
    assert signal_ids(session, op) == {ancient}


def test_the_shipped_policy_deletes_nothing_at_all_from_the_schema_as_it_stands_today(tmp_db):
    settings, session = tmp_db
    op = settings.operator.operator_id
    incident = seed_incident(session, operator_id=op, age_days=5000, number="INC000001")
    signal = seed_signal(session, operator_id=op, age_days=5000)
    session.commit()

    report = hk.purge_expired(session, settings, hk.load_policy(), now=NOW, apply=True)
    session.commit()

    assert report.deleted == 0
    assert session.get(IncidentRow, incident) is not None
    assert signal_ids(session, op) == {signal}
    # Every §9.4 rule that does delete belongs to a lane whose table does not exist yet; each
    # is reported as skipped rather than silently absent, so the policy stays auditable.
    assert all(t.skipped == "not in this schema" for t in report.tables), [t.as_dict() for t in report.tables]


def test_a_purge_driven_by_an_unsafe_policy_refuses_everything_rather_than_the_legal_subset(tmp_db, tmp_path):
    settings, session = tmp_db
    op = settings.operator.operator_id
    doomed = seed_signal(session, operator_id=op, age_days=400)
    session.commit()

    unsafe = _unvalidated(
        tmp_path,
        """
version: 1
classes:
  cache: {action: delete, days: 30, legal_basis: fine}
  network_facts: {action: delete, days: 30, licence_floor: true, legal_basis: not fine}
tables:
  external_signals: {class: cache, timestamp_column: fetched_at}
  incidents: {class: network_facts, timestamp_column: created_at}
""",
    )
    report = hk.purge_expired(session, settings, unsafe, now=NOW, apply=True)
    session.commit()

    assert report.refused
    assert report.deleted == 0
    # The legal rule is not executed either: a policy nobody can trust authorises nothing.
    assert signal_ids(session, op) == {doomed}


# --------------------------------------------------------------------- pseudonymisation


def test_personal_fields_become_role_tokens_while_the_network_facts_beside_them_are_untouched(tmp_db):
    settings, session = tmp_db
    op = settings.operator.operator_id
    old = seed_incident(session, operator_id=op, age_days=401, number="INC000010")
    session.commit()
    before = row_snapshot(session, IncidentRow, old)

    report = hk.pseudonymise_personal_fields(session, settings, hk.load_policy(), now=NOW, apply=True)
    session.commit()
    after = row_snapshot(session, IncidentRow, old)

    assert report.rows_changed == 1
    assert after["assignee_name"] == "FE-MTK"
    assert after["fe_name"] == "FE-MTK"
    assert after["rnio_name"] == "RNIO-MTK"
    assert after["restored_by"] == "RESTORER-MTK"
    assert "Kevin" not in (after["access_notes"] or "")
    assert "0712" not in (after["access_notes"] or "")
    assert "kevin@example.com" not in (after["access_notes"] or "")
    for column in ("incident_number", "site_id", "site_name", "region_code", "users_affected",
                   "failure_domain", "alarm_code", "created_at", "correlation_fingerprint"):
        assert after[column] == before[column], f"{column} is a network fact and must survive verbatim"


def test_an_incident_inside_the_personal_retention_window_keeps_its_names(tmp_db):
    settings, session = tmp_db
    op = settings.operator.operator_id
    recent = seed_incident(session, operator_id=op, age_days=399, number="INC000011")
    session.commit()

    report = hk.pseudonymise_personal_fields(session, settings, hk.load_policy(), now=NOW, apply=True)
    session.commit()

    assert report.rows_changed == 0
    assert row_snapshot(session, IncidentRow, recent)["fe_name"] == "James Mwangi"


def test_pseudonymisation_is_idempotent_so_a_second_pass_rewrites_nothing(tmp_db):
    settings, session = tmp_db
    op = settings.operator.operator_id
    old = seed_incident(session, operator_id=op, age_days=800, number="INC000012")
    session.commit()
    policy = hk.load_policy()

    hk.pseudonymise_personal_fields(session, settings, policy, now=NOW, apply=True)
    session.commit()
    after_first = row_snapshot(session, IncidentRow, old)

    second = hk.pseudonymise_personal_fields(session, settings, policy, now=NOW, apply=True)
    session.commit()
    after_second = row_snapshot(session, IncidentRow, old)

    assert second.rows_changed == 0
    assert second.columns_changed == 0
    # The whole row, not the columns the test remembered: a second pass that re-scrubbed the
    # already-tokenised notes ("FE-MTK" -> "<PERSON_1>") would show up here and nowhere else.
    assert after_second == after_first


def test_a_dry_run_pseudonymisation_reports_the_work_without_doing_it(tmp_db):
    settings, session = tmp_db
    op = settings.operator.operator_id
    old = seed_incident(session, operator_id=op, age_days=800, number="INC000013")
    session.commit()

    report = hk.pseudonymise_personal_fields(session, settings, hk.load_policy(), now=NOW, apply=False)
    session.commit()

    assert report.applied is False
    assert report.matched == 1
    assert report.rows_changed == 0
    assert row_snapshot(session, IncidentRow, old)["assignee_name"] == "Kevin Ochieng"


def test_pseudonymisation_never_reaches_another_operators_incidents(tmp_db):
    settings, session = tmp_db
    op = settings.operator.operator_id
    seed_incident(session, operator_id=op, age_days=800, number="INC000014")
    theirs = seed_incident(session, operator_id=OTHER_OPERATOR, age_days=5000, number="INC000015")
    session.commit()

    hk.pseudonymise_personal_fields(session, settings, hk.load_policy(), now=NOW, apply=True)
    session.commit()

    assert row_snapshot(session, IncidentRow, theirs)["fe_name"] == "James Mwangi"


def test_an_incident_with_no_names_on_it_is_not_counted_as_work(tmp_db):
    settings, session = tmp_db
    op = settings.operator.operator_id
    seed_incident(
        session, operator_id=op, age_days=900, number="INC000016",
        assignee=None, fe=None, rnio=None, restored_by=None, notes=None,
    )
    session.commit()

    report = hk.pseudonymise_personal_fields(session, settings, hk.load_policy(), now=NOW, apply=True)
    session.commit()

    assert report.rows_changed == 0


def test_the_before_argument_overrides_the_configured_personal_window(tmp_db):
    settings, session = tmp_db
    op = settings.operator.operator_id
    recent = seed_incident(session, operator_id=op, age_days=10, number="INC000017")
    session.commit()

    report = hk.pseudonymise_personal_fields(
        session, settings, hk.load_policy(), before=NOW - timedelta(days=5), now=NOW, apply=True
    )
    session.commit()

    assert report.rows_changed == 1
    assert row_snapshot(session, IncidentRow, recent)["fe_name"] == "FE-MTK"


# ------------------------------------------------------------------------- outbox sweep


def test_the_sweep_archives_the_payload_of_a_terminal_row_past_ninety_days(tmp_db):
    settings, session = tmp_db
    op = settings.operator.operator_id
    old = seed_outbox(
        session, operator_id=op, status="SENT", age_days=91,
        payload={"operator_id": op, "incident_number": "INC000100", "audience": "FE", "message": "Site down at Machakos"},
    )
    session.commit()

    report = hk.sweep_outbox(session, settings, hk.load_policy(), now=NOW, apply=True)
    session.commit()

    assert report.archived == 1
    row = session.get(OutboxRow, old)
    payload = json.loads(row.payload_json)
    assert payload[hk.ARCHIVED_KEY] is True
    assert "Site down at Machakos" not in row.payload_json
    assert payload["incident_number"] == "INC000100"  # the delivery record survives
    assert row.envelope_json is None
    assert row.status == "SENT"  # never rewritten


def test_a_terminal_row_inside_the_ninety_day_window_keeps_its_payload(tmp_db):
    settings, session = tmp_db
    op = settings.operator.operator_id
    recent = seed_outbox(
        session, operator_id=op, status="SENT", age_days=89,
        payload={"operator_id": op, "message": "still needed for a dispute"},
    )
    session.commit()

    report = hk.sweep_outbox(session, settings, hk.load_policy(), now=NOW, apply=True)
    session.commit()

    assert report.archived == 0
    assert "still needed for a dispute" in session.get(OutboxRow, recent).payload_json


def test_the_sweep_leaves_a_stale_claimed_row_exactly_as_it_found_it(tmp_db):
    """Releasing a stale lease is not cleanup, it is a re-dispatch.

    ``orchestrator.outbox.drain_once`` reclaims a CLAIMED row older than its 120 s lease and
    the dispatcher then transmits it again, bounded by ``attempts``. If housekeeping did the
    same thing a row whose SMS already reached the customer would be sent a second time, so
    the sweep counts stuck rows for a human and writes nothing.
    """
    settings, session = tmp_db
    op = settings.operator.operator_id
    stuck = seed_outbox(
        session, operator_id=op, status="CLAIMED", age_days=95, claimed_age_s=3600,
        payload={"operator_id": op, "message": "customer SMS mid-flight"},
    )
    session.commit()
    before = row_snapshot(session, OutboxRow, stuck)

    report = hk.sweep_outbox(session, settings, hk.load_policy(), now=NOW, apply=True)
    session.commit()

    assert report.stuck_claimed == 1
    assert row_snapshot(session, OutboxRow, stuck) == before


def test_the_sweep_never_touches_a_row_that_is_still_waiting_to_be_sent(tmp_db):
    settings, session = tmp_db
    op = settings.operator.operator_id
    pending = seed_outbox(session, operator_id=op, status="PENDING", age_days=200, payload={"operator_id": op, "message": "x"})
    held = seed_outbox(session, operator_id=op, status="HELD", age_days=200, payload={"operator_id": op, "message": "y"})
    session.commit()
    snapshots = {rid: row_snapshot(session, OutboxRow, rid) for rid in (pending, held)}

    hk.sweep_outbox(session, settings, hk.load_policy(), now=NOW, apply=True)
    session.commit()

    for rid, before in snapshots.items():
        assert row_snapshot(session, OutboxRow, rid) == before


def test_the_sweep_is_idempotent_and_does_not_re_archive_what_it_archived(tmp_db):
    settings, session = tmp_db
    op = settings.operator.operator_id
    seed_outbox(session, operator_id=op, status="DEAD", age_days=120, payload={"operator_id": op, "message": "gone"})
    session.commit()
    policy = hk.load_policy()

    first = hk.sweep_outbox(session, settings, policy, now=NOW, apply=True)
    session.commit()
    second = hk.sweep_outbox(session, settings, policy, now=NOW, apply=True)
    session.commit()

    assert first.archived == 1
    assert second.archived == 0
    assert second.archivable == 0


def test_a_dry_run_sweep_counts_the_payloads_it_would_archive_without_writing(tmp_db):
    settings, session = tmp_db
    op = settings.operator.operator_id
    old = seed_outbox(session, operator_id=op, status="SENT", age_days=200, payload={"operator_id": op, "message": "kept"})
    session.commit()

    report = hk.sweep_outbox(session, settings, hk.load_policy(), now=NOW, apply=False)
    session.commit()

    assert report.archivable == 1
    assert report.archived == 0
    assert "kept" in session.get(OutboxRow, old).payload_json


def test_the_sweep_does_not_archive_another_operators_payloads(tmp_db):
    settings, session = tmp_db
    op = settings.operator.operator_id
    theirs = seed_outbox(
        session, operator_id=OTHER_OPERATOR, status="SENT", age_days=900, payload={"operator_id": OTHER_OPERATOR, "message": "theirs"}
    )
    session.commit()

    report = hk.sweep_outbox(session, settings, hk.load_policy(), now=NOW, apply=True)
    session.commit()

    assert report.archived == 0
    assert "theirs" in session.get(OutboxRow, theirs).payload_json


# ---------------------------------------------------------------------- redaction scan


def test_a_seeded_msisdn_in_a_sent_payload_raises_a_redaction_miss(tmp_db):
    settings, session = tmp_db
    op = settings.operator.operator_id
    leaked = seed_outbox(
        session, operator_id=op, status="SENT", age_days=0.1,
        payload={"operator_id": op, "incident_number": "INC000200", "message": "Call the FE on 0712345678 for access"},
    )
    session.commit()

    report, events = hk.post_send_redaction_scan(session, settings, hk.load_policy(), now=NOW)
    session.commit()

    assert report.misses == 1
    assert report.new_audit_rows == 1
    row = session.scalars(select(AuditRow).where(AuditRow.action == hk.REDACTION_MISS_ACTION)).one()
    assert row.entity_id == leaked
    assert [e.type for e in events] == [hk.REDACTION_MISS_EVENT]
    assert json.loads(row.payload_json)["phone_matches"] == 1


def test_the_redaction_miss_record_never_quotes_the_contact_detail_that_leaked(tmp_db):
    """§9.5: an audit payload may not contain an MSISDN. A breach record that quotes the
    breach is a second copy of it, and it is the copy that gets exported to a regulator."""
    settings, session = tmp_db
    op = settings.operator.operator_id
    seed_outbox(
        session, operator_id=op, status="SENT", age_days=0.1,
        payload={"operator_id": op, "message": "reach James on 0722000111 or james@example.com"},
    )
    session.commit()

    report, events = hk.post_send_redaction_scan(session, settings, hk.load_policy(), now=NOW)
    session.commit()

    row = session.scalars(select(AuditRow).where(AuditRow.action == hk.REDACTION_MISS_ACTION)).one()
    serialised = row.payload_json + row.rationale + json.dumps(events[0].to_dict(), default=str)
    assert "0722000111" not in serialised
    assert "james@example.com" not in serialised
    assert "message" in row.payload_json  # the PATH is named, so an engineer knows where to look


def test_a_clean_sent_payload_raises_nothing(tmp_db):
    settings, session = tmp_db
    op = settings.operator.operator_id
    seed_outbox(
        session, operator_id=op, status="SENT", age_days=0.1,
        payload={"operator_id": op, "incident_number": "INC000201", "message": "Site SFC-MTK-BTS-MCH04 down, P2, ETR 14:00 EAT"},
    )
    session.commit()

    report, events = hk.post_send_redaction_scan(session, settings, hk.load_policy(), now=NOW)

    assert report.scanned == 1
    assert report.misses == 0
    assert events == []


def test_the_scan_only_looks_at_the_last_twenty_four_hours(tmp_db):
    settings, session = tmp_db
    op = settings.operator.operator_id
    seed_outbox(
        session, operator_id=op, status="SENT", age_days=3,
        payload={"operator_id": op, "message": "old leak 0712345678"},
    )
    session.commit()

    report, events = hk.post_send_redaction_scan(session, settings, hk.load_policy(), now=NOW)

    assert report.scanned == 0
    assert events == []


def test_the_scan_is_idempotent_and_does_not_raise_the_same_miss_twice(tmp_db):
    settings, session = tmp_db
    op = settings.operator.operator_id
    seed_outbox(
        session, operator_id=op, status="SENT", age_days=0.1,
        payload={"operator_id": op, "message": "0712345678"},
    )
    session.commit()
    policy = hk.load_policy()

    first, first_events = hk.post_send_redaction_scan(session, settings, policy, now=NOW)
    session.commit()
    second, second_events = hk.post_send_redaction_scan(session, settings, policy, now=NOW)
    session.commit()

    assert first.new_audit_rows == 1
    assert second.misses == 1  # still a finding
    assert second.new_audit_rows == 0  # but not a second row, and not a second alarm
    assert len(first_events) == 1 and second_events == []
    assert session.scalar(select(func.count()).select_from(AuditRow).where(AuditRow.action == hk.REDACTION_MISS_ACTION)) == 1


def test_an_email_address_is_counted_as_an_email_and_not_also_as_a_phone_number(tmp_db):
    settings, session = tmp_db
    op = settings.operator.operator_id
    seed_outbox(
        session, operator_id=op, status="SENT", age_days=0.1,
        payload={"operator_id": op, "message": "escalate to 0722123456@example.com"},
    )
    session.commit()

    report, _ = hk.post_send_redaction_scan(session, settings, hk.load_policy(), now=NOW)

    assert report.hits[0].email_matches == 1
    assert report.hits[0].phone_matches == 0


def test_an_already_archived_payload_is_not_re_scanned(tmp_db):
    settings, session = tmp_db
    op = settings.operator.operator_id
    seed_outbox(
        session, operator_id=op, status="SENT", age_days=0.1,
        payload={hk.ARCHIVED_KEY: True, "kind": "SMS", "note": "payload removed"},
    )
    session.commit()

    report, _ = hk.post_send_redaction_scan(session, settings, hk.load_policy(), now=NOW)

    assert report.scanned == 0


def test_the_scan_does_not_read_another_operators_payloads(tmp_db):
    settings, session = tmp_db
    seed_outbox(
        session, operator_id=OTHER_OPERATOR, status="SENT", age_days=0.1,
        payload={"operator_id": OTHER_OPERATOR, "message": "0712345678"},
    )
    session.commit()

    report, events = hk.post_send_redaction_scan(session, settings, hk.load_policy(), now=NOW)

    assert report.scanned == 0
    assert events == []


# ----------------------------------------------------------------------------- backups


def test_the_daily_backup_is_a_consistent_readable_copy_not_a_file_copy(tmp_db):
    """``db/migrate.py`` uses the sqlite3 backup API because the database runs in WAL mode
    and a plain copy of the ``.db`` file can catch a torn database — committed pages may
    still be in the ``-wal``. Opening the copy and finding the row proves the same path."""
    settings, session = tmp_db
    op = settings.operator.operator_id
    seed_incident(session, operator_id=op, age_days=1, number="INC000300")
    session.commit()

    report = hk.backup_db(session, hk.load_policy(), now=NOW)

    assert report.created is True and not report.error
    copy = sqlite3.connect(report.path)
    try:
        assert copy.execute("SELECT count(*) FROM incidents WHERE incident_number='INC000300'").fetchone()[0] == 1
    finally:
        copy.close()


def test_the_backup_is_named_for_the_eat_date_because_the_job_runs_at_midnight_utc(tmp_db):
    settings, session = tmp_db
    # 00:30 UTC is 03:30 EAT the same morning; naming on UTC would date a 22:00 EAT run yesterday.
    report = hk.backup_db(session, hk.load_policy(), now=NOW)
    assert Path(report.path).name.endswith(".daily.2026-09-18.db")


def test_a_second_backup_on_the_same_day_is_skipped_rather_than_overwriting_the_first(tmp_db):
    settings, session = tmp_db
    policy = hk.load_policy()
    first = hk.backup_db(session, policy, now=NOW)
    second = hk.backup_db(session, policy, now=NOW)

    assert first.created is True
    assert second.created is False
    assert second.skipped
    assert second.path == first.path


def test_rotation_keeps_the_newest_daily_backups_and_never_touches_a_migration_backup(tmp_db):
    settings, session = tmp_db
    policy = hk.load_policy()
    first = hk.backup_db(session, policy, now=NOW)
    backups = Path(first.path).parent
    stem = Path(first.path).name.split(".daily.")[0]
    for day in range(1, 20):
        (backups / f"{stem}.daily.2026-08-{day:02d}.db").write_bytes(b"old")
    migration = backups / f"{stem}.1-to-4.20250101T000000Z.db"
    migration.write_bytes(b"pre-migration")

    report = hk.backup_db(session, policy, now=NOW, apply=True)

    remaining = sorted(p.name for p in backups.glob(f"{stem}.daily.*.db"))
    assert len(remaining) == policy.backup_keep_daily
    assert f"{stem}.daily.2026-09-18.db" in remaining  # today's survives
    assert migration.exists(), "a pre-migration backup is the rollback path and is never rotated"
    assert report.rotated


def test_rotation_obeys_the_dry_run_posture_because_deleting_a_file_is_still_deleting(tmp_db):
    settings, session = tmp_db
    policy = hk.load_policy()
    first = hk.backup_db(session, policy, now=NOW)
    backups = Path(first.path).parent
    stem = Path(first.path).name.split(".daily.")[0]
    for day in range(1, 20):
        (backups / f"{stem}.daily.2026-08-{day:02d}.db").write_bytes(b"old")

    report = hk.backup_db(session, policy, now=NOW, apply=False)

    assert report.rotated  # reported
    assert report.rotate_applied is False
    assert len(list(backups.glob(f"{stem}.daily.*.db"))) == 20  # nothing removed


# ------------------------------------------------------------------------- memory seam


def test_the_memory_expiry_seam_is_wired_to_the_memory_lane_and_honours_its_two_rules(tmp_db, monkeypatch):
    """The seam this module declared in Phase 4 is now filled, by memory M1.

    This test used to be ``..._is_named_but_not_wired_because_lane_4c_owns_it`` and asserted the
    seam was UNAVAILABLE. That was an accurate statement while Lane 4C had not been built, and the
    wrong thing to pin permanently: as an invariant it forbade the memory lane from ever being
    integrated, and failed the moment it was. It joins two earlier cases of the same pattern
    (the complaints and capacity lanes each pinned their own job as absent from SCHEDULED_JOBS).

    What is worth pinning is the contract the seam's docstring states: the lane's real
    ``expire_memory`` is what gets called, it is called REGARDLESS of MEMORY_ENABLED (retention
    must not depend on a read flag), and it is NOT called in dry run (its person-scoped branch is
    a hard delete and it has no dry-run mode of its own).
    """
    from noc_agents.memory import consolidate as lane

    settings, session = tmp_db
    monkeypatch.setenv("MEMORY_ENABLED", "false")  # retention must run with reads switched off
    assert hk.MEMORY_EXPIRY_SEAM == "noc_agents.memory.consolidate:expire_memory"

    dry = hk.expire_memory(session, settings, hk.load_policy(), now=NOW, apply=False)
    assert dry.available is True and dry.called is False

    applied = hk.expire_memory(session, settings, hk.load_policy(), now=NOW, apply=True)
    assert applied.available is True and applied.called is True
    assert set(applied.counts) == {"episodes_pruned", "fts_rows_pruned"}
    assert callable(lane.expire_memory)


def test_the_memory_seam_is_not_called_in_dry_run_because_it_hard_deletes(tmp_db, monkeypatch):
    settings, session = tmp_db
    calls: list[dict] = []

    def expire_memory(session, *, settings, now):  # noqa: ANN001 — mirrors the lane's signature exactly
        calls.append({"now": now})
        return {"facts_deleted": 3}

    lane = types.ModuleType("fake_memory_lane")
    lane.expire_memory = expire_memory
    monkeypatch.setitem(sys.modules, "fake_memory_lane", lane)
    monkeypatch.setattr(hk, "MEMORY_EXPIRY_SEAM", "fake_memory_lane:expire_memory")

    dry = hk.expire_memory(session, settings, hk.load_policy(), now=NOW, apply=False)
    assert dry.available is True and dry.called is False and calls == []

    applied = hk.expire_memory(session, settings, hk.load_policy(), now=NOW, apply=True)
    assert applied.called is True
    assert applied.counts == {"facts_deleted": 3}
    assert len(calls) == 1


# ----------------------------------------------------------------------------- the job


def test_the_job_card_ships_off_so_an_unset_flag_reads_as_disabled_everywhere():
    card = hk.HOUSEKEEPING_JOB
    assert card.name == "housekeeping"
    assert card.interval_s == 86400
    assert card.enabled_env == "HOUSEKEEPING_ENABLED"
    assert card.agent == "HousekeepingAgent"
    assert card.graph_name == "housekeeping"
    assert card.max_seconds == 600
    assert card.default_enabled is False
    assert job_enabled(card) is False  # with the flag unset, /scheduler/status says off too


def test_with_the_flag_off_the_job_reads_nothing_writes_nothing_and_takes_no_backup(tmp_db, monkeypatch):
    settings, session = tmp_db
    op = settings.operator.operator_id
    incident = seed_incident(session, operator_id=op, age_days=5000, number="INC000400")
    outbox_id = seed_outbox(
        session, operator_id=op, status="SENT", age_days=900, payload={"operator_id": op, "message": "0712345678"}
    )
    session.commit()
    before_incident = row_snapshot(session, IncidentRow, incident)
    before_outbox = row_snapshot(session, OutboxRow, outbox_id)

    result = hk.run(session, settings, now=NOW)

    assert "skipped" in result.summary
    assert row_snapshot(session, IncidentRow, incident) == before_incident
    assert row_snapshot(session, OutboxRow, outbox_id) == before_outbox
    assert session.scalar(select(func.count()).select_from(AuditRow)) == 0
    backups = Path(settings.database_url.replace("sqlite:///", "")).parent / "backups"
    assert not backups.exists()


def test_a_full_enabled_run_is_dry_run_by_default_and_writes_one_retention_audit_row(tmp_db, monkeypatch, clean_hub):
    settings, session = tmp_db
    monkeypatch.setenv(hk.ENABLED_ENV, "true")
    op = settings.operator.operator_id
    incident = seed_incident(session, operator_id=op, age_days=5000, number="INC000500")
    seed_outbox(
        session, operator_id=op, status="SENT", age_days=0.1,
        payload={"operator_id": op, "incident_number": "INC000500", "message": "call 0712345678"},
    )
    session.commit()
    before = row_snapshot(session, IncidentRow, incident)

    result = hk.run(session, settings, now=NOW)

    assert "dry-run" in result.summary
    purge_rows = session.scalars(select(AuditRow).where(AuditRow.action == hk.AUDIT_ACTION)).all()
    assert len(purge_rows) == 1
    payload = json.loads(purge_rows[0].payload_json)
    assert payload["posture"] == "dry_run"
    assert payload["purge"]["deleted"] == 0
    assert payload["licence_floor_days"] == hk.LICENCE_FLOOR_DAYS
    # Nothing was touched, but the leak still raised its alarm: the scan is not gated on posture.
    assert row_snapshot(session, IncidentRow, incident) == before
    assert session.scalar(select(func.count()).select_from(AuditRow).where(AuditRow.action == hk.REDACTION_MISS_ACTION)) == 1
    assert hk.REDACTION_MISS_EVENT in [e["type"] for e in hub.recent(20)]


def test_an_enabled_run_needs_both_keys_before_it_will_delete_anything(tmp_db, monkeypatch, tmp_path):
    monkeypatch.setenv(hk.ENABLED_ENV, "true")
    settings, session = tmp_db
    policy_path = write_policy(
        tmp_path,
        """
version: 1
posture: {dry_run: false}
licence_floor_days: 1095
backups: {dir: data/backups, keep_daily: 14}
outbox: {archive_after_days: 90}
redaction_scan: {lookback_hours: 24}
classes:
  cache: {action: delete, days: 30, legal_basis: test fixture}
tables:
  external_signals: {class: cache, timestamp_column: fetched_at}
""",
        name="both_keys.yaml",
    )
    monkeypatch.setenv(hk.POLICY_PATH_ENV, str(policy_path))
    op = settings.operator.operator_id
    doomed = seed_signal(session, operator_id=op, age_days=400)
    session.commit()

    # YAML says apply, the environment does not: still a dry run.
    hk.run(session, settings, now=NOW)
    assert signal_ids(session, op) == {doomed}

    monkeypatch.setenv(hk.APPLY_ENV, "true")
    result = hk.run(session, settings, now=NOW)
    assert "APPLY" in result.summary
    assert signal_ids(session, op) == set()


def test_a_full_run_is_idempotent_so_the_second_night_changes_nothing(tmp_db, monkeypatch, tmp_path):
    monkeypatch.setenv(hk.ENABLED_ENV, "true")
    monkeypatch.setenv(hk.APPLY_ENV, "true")
    settings, session = tmp_db
    monkeypatch.setenv(
        hk.POLICY_PATH_ENV,
        str(
            write_policy(
                tmp_path,
                """
version: 1
posture: {dry_run: false}
licence_floor_days: 1095
backups: {dir: data/backups, keep_daily: 14}
outbox: {archive_after_days: 90}
redaction_scan: {lookback_hours: 24}
classes:
  network_facts: {action: keep, licence_floor: true, legal_basis: Condition 12.2}
  personal: {action: pseudonymise, days: 400, legal_basis: DPA s.25(g)}
  outbox_terminal: {action: archive_payload, days: 90, legal_basis: DPA s.25(g)}
  cache: {action: delete, days: 30, legal_basis: test fixture}
tables:
  external_signals: {class: cache, timestamp_column: fetched_at}
  outbox: {class: outbox_terminal, timestamp_column: updated_at, statuses: [SENT, DEAD]}
  incidents:
    class: network_facts
    timestamp_column: created_at
    personal:
      class: personal
      role_tokens: {assignee_name: "{assignee_type}-{region_code}", fe_name: "FE-{region_code}"}
      scrub_text_columns: [access_notes]
""",
                name="full_run.yaml",
            )
        ),
    )
    op = settings.operator.operator_id
    incident = seed_incident(session, operator_id=op, age_days=800, number="INC000600")
    survivor = seed_signal(session, operator_id=op, age_days=2)
    seed_signal(session, operator_id=op, age_days=400, source="MET_NORWAY")
    sent = seed_outbox(session, operator_id=op, status="SENT", age_days=120, payload={"operator_id": op, "message": "old"})
    session.commit()

    hk.run(session, settings, now=NOW)
    after_first = {
        "incident": row_snapshot(session, IncidentRow, incident),
        "outbox": row_snapshot(session, OutboxRow, sent),
        "signals": signal_ids(session, op),
    }

    hk.run(session, settings, now=NOW + timedelta(seconds=1))
    after_second = {
        "incident": row_snapshot(session, IncidentRow, incident),
        "outbox": row_snapshot(session, OutboxRow, sent),
        "signals": signal_ids(session, op),
    }

    assert after_first["signals"] == {survivor}
    assert after_first["incident"]["fe_name"] == "FE-MTK"
    assert after_second == after_first  # §5.3.22: a second run deletes nothing and changes nothing
    assert session.get(IncidentRow, incident) is not None  # Condition 12.2: the record stays


def test_an_unusable_policy_fails_the_run_and_deletes_nothing_but_still_scans_for_leaks(tmp_db, monkeypatch, tmp_path):
    monkeypatch.setenv(hk.ENABLED_ENV, "true")
    monkeypatch.setenv(hk.APPLY_ENV, "true")
    settings, session = tmp_db
    monkeypatch.setenv(hk.POLICY_PATH_ENV, str(write_policy(tmp_path, "classes: [this is not a mapping]\n", name="broken.yaml")))
    op = settings.operator.operator_id
    ancient = seed_signal(session, operator_id=op, age_days=5000)
    seed_outbox(
        session, operator_id=op, status="SENT", age_days=0.1,
        payload={"operator_id": op, "message": "0712345678"},
    )
    session.commit()

    with pytest.raises(hk.RetentionPolicyError):
        hk.run(session, settings, now=NOW)

    assert signal_ids(session, op) == {ancient}  # a broken policy is not a licence to delete
    assert session.scalar(select(func.count()).select_from(AuditRow).where(AuditRow.action == hk.REDACTION_MISS_ACTION)) == 1


def test_the_freshness_report_answers_the_three_questions_asked_at_three_in_the_morning(tmp_db):
    settings, session = tmp_db
    op = settings.operator.operator_id
    seed_signal(session, operator_id=op, age_days=0.5)
    seed_outbox(session, operator_id=op, status="PENDING", age_days=2, payload={"operator_id": op, "message": "waiting"})
    session.commit()

    report = hk.freshness_report(session, settings, now=NOW)

    assert report["signals"][0]["source"] == "OPEN_METEO"
    assert report["signals"][0]["age_minutes"] == pytest.approx(720, abs=1)
    assert report["outbox"]["by_status"]["PENDING"] == 1
    assert report["outbox"]["oldest_undelivered_age_minutes"] == pytest.approx(2880, abs=1)
    assert report["housekeeping"]["enabled"] is False


def test_the_freshness_report_does_not_count_another_operators_outbox(tmp_db):
    settings, session = tmp_db
    seed_outbox(session, operator_id=OTHER_OPERATOR, status="PENDING", age_days=2, payload={"operator_id": OTHER_OPERATOR})
    session.commit()

    assert hk.freshness_report(session, settings, now=NOW).get("outbox", {}).get("by_status", {}) == {}


def test_role_tokens_render_unknown_for_a_field_the_row_does_not_carry():
    assert hk.role_token("FE-{region_code}", {"region_code": "MTK"}) == "FE-MTK"
    assert hk.role_token("FE-{region_code}", {"region_code": None}) == "FE-UNKNOWN"
    assert hk.role_token("{assignee_type}-{region_code}", {}) == "UNKNOWN-UNKNOWN"


def test_one_failing_duty_does_not_stop_the_others_but_does_fail_the_run(tmp_db, monkeypatch):
    """Fail-soft is per duty, not per run (§5.3.22 autonomy A0, criticality fail_soft).

    A broken backup must not cost the operator the redaction scan, and it must not be
    swallowed either: the run is recorded FAILED so the scheduler's circuit breaker counts
    it and ``/scheduler/status`` shows it.
    """
    monkeypatch.setenv(hk.ENABLED_ENV, "true")
    settings, session = tmp_db
    op = settings.operator.operator_id
    seed_outbox(
        session, operator_id=op, status="SENT", age_days=0.1,
        payload={"operator_id": op, "message": "0712345678"},
    )
    session.commit()

    def boom(*args, **kwargs):
        raise OSError("the backups volume is full")

    monkeypatch.setattr(hk, "backup_db", boom)

    with pytest.raises(hk.HousekeepingError) as exc:
        hk.run(session, settings, now=NOW)

    assert "backup_db" in str(exc.value)
    # The later duties still ran and their work is committed.
    assert session.scalar(select(func.count()).select_from(AuditRow).where(AuditRow.action == hk.REDACTION_MISS_ACTION)) == 1
    audit = session.scalars(select(AuditRow).where(AuditRow.action == hk.AUDIT_ACTION)).one()
    assert json.loads(audit.payload_json)["errors"]


def test_a_failing_duty_cannot_roll_back_the_work_an_earlier_duty_already_committed(tmp_db, monkeypatch, tmp_path):
    """Each duty is its own short transaction (§4.5). Sharing one across all of them would
    mean a rollback in the redaction scan silently undid the purge that ran before it, and
    the run would report deletions that never happened."""
    monkeypatch.setenv(hk.ENABLED_ENV, "true")
    monkeypatch.setenv(hk.APPLY_ENV, "true")
    settings, session = tmp_db
    monkeypatch.setenv(
        hk.POLICY_PATH_ENV,
        str(
            write_policy(
                tmp_path,
                """
version: 1
posture: {dry_run: false}
licence_floor_days: 1095
backups: {dir: data/backups, keep_daily: 14}
outbox: {archive_after_days: 90}
redaction_scan: {lookback_hours: 24}
classes:
  cache: {action: delete, days: 30, legal_basis: test fixture}
tables:
  external_signals: {class: cache, timestamp_column: fetched_at}
""",
                name="duty_isolation.yaml",
            )
        ),
    )
    op = settings.operator.operator_id
    seed_signal(session, operator_id=op, age_days=400)
    survivor = seed_signal(session, operator_id=op, age_days=1, source="MET_NORWAY")
    session.commit()

    def boom(*args, **kwargs):
        raise RuntimeError("the scan blew up after the purge committed")

    monkeypatch.setattr(hk, "post_send_redaction_scan", boom)

    with pytest.raises(hk.HousekeepingError):
        hk.run(session, settings, now=NOW)

    assert signal_ids(session, op) == {survivor}  # the purge's work survived the later failure
