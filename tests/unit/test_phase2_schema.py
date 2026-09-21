"""Phase 2 schema seam (spec §6.3, §6.6, §8 Phase 2, Appendix A).

This wave adds *storage only*: the ``message_templates`` registry, the ``delivery_receipts``
log, the five ``hitl_tasks`` columns that "raiser != approver" and the zero-edit metric need,
and the additive ``broadcasts.status`` vocabulary. **No code reads or writes any of it yet** --
``TemplateRegistry.sync``, the delivery webhooks and the re-render/approval handlers land in
later Phase 2 waves. So everything asserted here is a property of the schema, never of a
feature, and each assertion answers one of three questions:

1. does a database created from scratch have every new table, column, index and constraint?
2. is ``migrate_additive`` still idempotent after the version bump (second run: no DDL, no backup)?
3. does a database at the PREVIOUS schema version move forward -- backup written first, old
   rows still readable, the new columns carrying the defaults the migration could fill?

``tests/fixtures/db/v1_baseline.db`` is the pre-Phase-1 file (see ``test_migrate.py``); the
previous-version file for (3) is reconstructed from it here rather than committed, so it can
never drift out of step with ``models.py``.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import select

import noc_agents.db.models as models
from noc_agents.db import migrate
from noc_agents.db.migrate import SCHEMA_VERSION, migrate_additive
from noc_agents.db.models import (
    BROADCAST_STATUSES,
    Base,
    BroadcastRow,
    DeliveryReceiptRow,
    HitlTaskRow,
    IncidentRow,
    MessageTemplateRow,
    OutboxRow,
    WorkNoteRow,
    get_session,
    init_db,
    new_id,
    utcnow,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "db" / "v1_baseline.db"
INCIDENT_ID = "11111111-1111-1111-1111-111111111111"  # the single row in the v1 fixture

# What this wave adds. These are the literals the migration has to produce; the generic
# "every mapped table and column exists" loop below is what protects the rest.
PHASE2_TABLES = {"message_templates", "delivery_receipts"}
PHASE2_HITL_COLUMNS = ("run_id", "entity_type", "entity_id", "created_by", "edited")
PHASE2_HITL_ALTERS = [
    "ALTER TABLE hitl_tasks ADD COLUMN run_id TEXT",
    "ALTER TABLE hitl_tasks ADD COLUMN entity_type TEXT DEFAULT 'incident'",
    "ALTER TABLE hitl_tasks ADD COLUMN entity_id TEXT",
    "ALTER TABLE hitl_tasks ADD COLUMN created_by TEXT",
    "ALTER TABLE hitl_tasks ADD COLUMN edited INTEGER DEFAULT 0",
]
# Phase 1 left the file at 2; this wave is 3. Kept as its own name so the reconstruction below
# is checked against reality: when a later wave bumps SCHEMA_VERSION again, this assertion
# fires and whoever bumps it re-reads this file instead of silently testing nothing.
PREVIOUS_SCHEMA_VERSION = SCHEMA_VERSION - 1
#: The version this module's fixture builder actually produces (Phase 1's shape).
PHASE1_SCHEMA_VERSION = 2

# §6.6: additive strings only. CANCELLED is NOT renamed (§2.1 R7).
PHASE2_BROADCAST_STATUSES = ("HELD", "QUEUED", "SUPPRESSED", "DELIVERED")
PRE_PHASE2_BROADCAST_STATUSES = ("DRAFTED", "QUEUED", "PENDING_HITL", "SENT", "FAILED", "CANCELLED")


@pytest.fixture()
def restore_db_globals():
    """init_db() rebinds the module-level engine/session factory; put the previous ones back."""
    saved = (models._engine, models.SessionLocal)
    yield
    models._engine, models.SessionLocal = saved


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _tables(path: Path) -> set[str]:
    con = sqlite3.connect(path)
    try:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()


def _columns(path: Path, table: str) -> list[str]:
    con = sqlite3.connect(path)
    try:
        return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
    finally:
        con.close()


def _indexes(path: Path, table: str) -> set[str]:
    con = sqlite3.connect(path)
    try:
        return {r[1] for r in con.execute(f"PRAGMA index_list({table})")}
    finally:
        con.close()


def _scalar(path: Path, sql: str):
    con = sqlite3.connect(path)
    try:
        return con.execute(sql).fetchone()[0]
    finally:
        con.close()


def _fresh_db(tmp_path: Path, name: str = "phase2.db") -> tuple[Path, object]:
    db = tmp_path / name
    engine = init_db(_url(db), backup_dir=tmp_path / "backups")
    return db, engine


# --------------------------------------------------------------------------- 1. the shape


def test_schema_version_was_bumped_past_phase_1():
    """The additive migration only runs when the code's version is ahead of the file's."""
    assert SCHEMA_VERSION >= 3, "Phase 2 tables/columns are invisible to db/migrate.py without a bump"


def test_phase2_tables_and_columns_exist_after_init_db(tmp_path, restore_db_globals):
    db, engine = _fresh_db(tmp_path)
    try:
        assert PHASE2_TABLES <= _tables(db)

        # §6.3, column for column.
        assert _columns(db, "message_templates") == [
            "id", "operator_id", "channel", "template_key", "language", "version", "body",
            "subject", "params_schema_json", "provider_template_name", "provider_language_code",
            "approval_status", "approved_by", "approved_at", "created_at", "updated_at",
        ]
        # §6.6, column for column.
        assert _columns(db, "delivery_receipts") == [
            "id", "outbox_id", "provider", "provider_message_id", "provider_status",
            "received_at", "raw_json",
        ]
        assert {"ix_delivery_receipts_outbox_id", "ix_delivery_receipts_provider_message_id"} <= _indexes(db, "delivery_receipts")

        # Appendix A: hitl_tasks keeps every column it had and gains exactly five in Phase 2.
        # schema_version 8 then adds a sixth, operator_id (the task's own owner, so that an
        # approval card that is not about an incident can exist -- see db/migrate.py, "THE ONE
        # EXCEPTION"). It is pinned here explicitly rather than by loosening the slice, so the
        # Phase 2 five are still asserted exactly and any seventh column still fails this test.
        hitl = _columns(db, "hitl_tasks")
        assert hitl[:11] == [
            "id", "incident_id", "task_type", "proposed_payload_json", "status", "created_at",
            "resolved_by", "resolved_at", "claimed_by", "claimed_at", "reason",
        ]
        assert tuple(hitl[11:]) == PHASE2_HITL_COLUMNS + ("operator_id",)

        # ...and nothing else moved: every mapped table, column and index is on disk.
        for table in Base.metadata.sorted_tables:
            present = set(_columns(db, table.name))
            assert not {c.name for c in table.columns} - present, f"{table.name} is incomplete"
            assert {ix.name for ix in table.indexes} <= _indexes(db, table.name), table.name
    finally:
        engine.dispose()


def test_new_tables_round_trip_through_the_orm(tmp_path, restore_db_globals):
    """Nothing writes these tables yet, so this is the only proof the mapping is usable."""
    db, engine = _fresh_db(tmp_path)
    session = get_session()
    try:
        session.add(
            MessageTemplateRow(
                operator_id="safaricom", channel="SMS", template_key="site_down_alert",
                language="en", version=1, body="{{ site_name }} down", params_schema_json='{"site_name": "string"}',
            )
        )
        session.add(
            DeliveryReceiptRow(
                outbox_id="outbox-1", provider="africastalking",
                provider_message_id="ATXid_1", provider_status="Success",
            )
        )
        session.commit()

        tpl = session.scalars(select(MessageTemplateRow)).one()
        assert (tpl.channel, tpl.template_key, tpl.language, tpl.version) == ("SMS", "site_down_alert", "en", 1)
        assert tpl.approval_status == "DRAFT"  # §6.3 default: a seeded template is not approved
        assert tpl.approved_by is None and tpl.approved_at is None  # §6.4: `sw` sign-off has somewhere to land
        assert tpl.subject is None and tpl.provider_template_name is None
        assert tpl.created_at is not None and tpl.updated_at is not None
        assert isinstance(tpl.id, str) and len(tpl.id) == 36  # new_id() default

        receipt = session.scalars(select(DeliveryReceiptRow)).one()
        assert (receipt.outbox_id, receipt.provider_status) == ("outbox-1", "Success")
        assert receipt.raw_json == "{}" and receipt.received_at is not None
    finally:
        session.close()
        engine.dispose()


def test_a_receipt_may_record_a_provider_with_no_message_id(tmp_path, restore_db_globals):
    """SMTP has no delivery receipt (§6.6), so provider_message_id must be nullable."""
    db, engine = _fresh_db(tmp_path)
    session = get_session()
    try:
        session.add(
            DeliveryReceiptRow(
                outbox_id="outbox-2", provider="smtp", provider_status="ACCEPTED_BY_RELAY",
            )
        )
        session.commit()
        assert session.scalars(select(DeliveryReceiptRow)).one().provider_message_id is None
    finally:
        session.close()
        engine.dispose()


def test_template_version_key_is_unique_per_operator(tmp_path, restore_db_globals):
    """§6.3 UNIQUE (operator_id, channel, template_key, language, version): the seed is an
    idempotent upsert on that key, and a content change bumps `version` instead of overwriting."""
    db, engine = _fresh_db(tmp_path)
    engine.dispose()
    key = dict(channel="EMAIL", template_key="incident_update", language="en")
    con = sqlite3.connect(db)
    try:
        insert = (
            "INSERT INTO message_templates (id, operator_id, channel, template_key, language, "
            "version, body, params_schema_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'b', '{}', '2026-09-17 00:00:00', '2026-09-17 00:00:00')"
        )
        con.execute(insert, (new_id(), "safaricom", *key.values(), 1))
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(insert, (new_id(), "safaricom", *key.values(), 1))
        # A new version of the same template, and the same template for another operator, are fine.
        con.execute(insert, (new_id(), "safaricom", *key.values(), 2))
        con.execute(insert, (new_id(), "airtel", *key.values(), 1))
        assert con.execute("SELECT COUNT(*) FROM message_templates").fetchone()[0] == 3
        # approval_status carries its DDL default even for a raw insert.
        assert {r[0] for r in con.execute("SELECT approval_status FROM message_templates")} == {"DRAFT"}
    finally:
        con.close()


def test_hitl_task_gains_run_id_entity_pointer_raiser_and_edit_flag(tmp_path, restore_db_globals):
    """The four columns §3 says are missing today, plus run_id. Nothing writes them yet, so the
    defaults are what every row will have until the Phase 2 approval handlers land."""
    db, engine = _fresh_db(tmp_path)
    session = get_session()
    try:
        session.add(IncidentRow(
            id=INCIDENT_ID, operator_id="safaricom", incident_number="INC000001",
            site_id="SFC-1", region_code="NBI", correlation_fingerprint="fp",
        ))
        bare = HitlTaskRow(incident_id=INCIDENT_ID, task_type="APPROVE_BROADCAST")
        filled = HitlTaskRow(
            incident_id=INCIDENT_ID, task_type="APPROVE_BROADCAST",
            run_id="run-1", entity_type="power_notice", entity_id="pn-1",
            created_by="noc.analyst", edited=1,
        )
        session.add_all([bare, filled])
        session.commit()

        # A task raised with no extra information still points at an incident (§6.5 line 875).
        assert (bare.run_id, bare.entity_type, bare.entity_id) == (None, "incident", None)
        # NULL created_by is the fail-open value for "raiser != approver": it equals no actor.
        assert bare.created_by is None and bare.created_by != "noc.analyst"
        # M15's zero-edit counter reads this; an unedited approval is 0, not NULL.
        assert bare.edited == 0

        # ...and a task can point at something that is not an incident at all (§7.5).
        assert (filled.entity_type, filled.entity_id, filled.created_by, filled.edited) == (
            "power_notice", "pn-1", "noc.analyst", 1,
        )
    finally:
        session.close()
        engine.dispose()


def test_broadcast_status_vocabulary_is_additive_and_still_fits_the_column(tmp_path, restore_db_globals):
    """§6.6 adds HELD|QUEUED|SUPPRESSED|DELIVERED as *strings*. §2.1 R7 keeps CANCELLED as the
    rejected-draft status. Nothing is renamed and String(16) does not need widening."""
    # Every status today survives, in the same spelling.
    for status in PRE_PHASE2_BROADCAST_STATUSES:
        assert status in BROADCAST_STATUSES
    for status in PHASE2_BROADCAST_STATUSES:
        assert status in BROADCAST_STATUSES
    assert "CANCELLED" in BROADCAST_STATUSES, "R7: the rejected draft is CANCELLED, never SUPPRESSED"

    column = BroadcastRow.__table__.c.status
    assert column.type.length == 16, "widening is invisible to migrate_additive, which never retypes"
    assert max(len(s) for s in BROADCAST_STATUSES) <= column.type.length

    # And each one really stores and reads back.
    db, engine = _fresh_db(tmp_path)
    session = get_session()
    try:
        session.add(IncidentRow(
            id=INCIDENT_ID, operator_id="safaricom", incident_number="INC000001",
            site_id="SFC-1", region_code="NBI", correlation_fingerprint="fp",
        ))
        for status in BROADCAST_STATUSES:
            session.add(BroadcastRow(
                incident_id=INCIDENT_ID, channel="SMS", audience="RNIO", message="m", status=status,
            ))
        session.commit()
        stored = {b.status for b in session.scalars(select(BroadcastRow))}
        assert stored == set(BROADCAST_STATUSES)
    finally:
        session.close()
        engine.dispose()


# ------------------------------------------------- 2./3. the migration, from the version before


def _build_previous_version_db(tmp_path: Path) -> Path:
    """A database exactly as the Phase 1 release left it, with rows in it.

    Built by migrating the committed v1 fixture, seeding rows, then removing precisely what
    this wave adds and re-stamping the version. Reconstructing beats committing a second
    binary: it cannot drift away from ``models.py``, and dropping only the Phase 2 items is
    the definition of "the previous version" that the migration is supposed to close.
    """
    # This helper reconstructs the PHASE 1 shape by dropping exactly what Phase 2 added,
    # so the database it builds is version 2 -- whatever SCHEMA_VERSION happens to be now.
    # It used to assert PREVIOUS_SCHEMA_VERSION == 2 and stamp that; once Phase 3 bumped
    # SCHEMA_VERSION to 4 that would have stamped "3" onto a file with no message_templates,
    # i.e. a version that never existed. Stamping the real Phase 1 version keeps the test
    # honest AND makes it stronger as versions accumulate: it now proves a v2 database
    # migrates all the way forward, not merely one step.
    assert PHASE1_SCHEMA_VERSION < SCHEMA_VERSION, "Phase 1 must predate the current schema"
    db = tmp_path / "previous_version.db"
    shutil.copy2(FIXTURE, db)
    engine = init_db(_url(db), backup_dir=tmp_path / "scratch")
    session = get_session()
    try:
        session.add(HitlTaskRow(
            id="33333333-3333-3333-3333-333333333333",
            incident_id=INCIDENT_ID, task_type="APPROVE_BROADCAST", status="PENDING",
        ))
        session.add(OutboxRow(
            id="44444444-4444-4444-4444-444444444444", operator_id="safaricom",
            kind="EMAIL", idempotency_key="k1", payload_json="{}",
        ))
        session.commit()
    finally:
        session.close()
        engine.dispose()

    con = sqlite3.connect(db)
    try:
        for table in sorted(PHASE2_TABLES):
            con.execute(f"DROP TABLE IF EXISTS {table}")
        for column in PHASE2_HITL_COLUMNS:
            con.execute(f"ALTER TABLE hitl_tasks DROP COLUMN {column}")
        con.execute("DELETE FROM schema_version")
        con.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, '2026-09-16 00:00:00')",
            (PHASE1_SCHEMA_VERSION,),
        )
        con.commit()
    finally:
        con.close()
    shutil.rmtree(tmp_path / "scratch", ignore_errors=True)

    # Guard: the file really is missing exactly what the migration must add.
    assert not (PHASE2_TABLES & _tables(db))
    assert not (set(PHASE2_HITL_COLUMNS) & set(_columns(db, "hitl_tasks")))
    assert _scalar(db, "SELECT MAX(version) FROM schema_version") == PHASE1_SCHEMA_VERSION
    return db


def test_previous_version_database_migrates_forward_with_a_backup(tmp_path, restore_db_globals):
    db = _build_previous_version_db(tmp_path)
    before = _columns(db, "hitl_tasks")
    backups = tmp_path / "backups"

    engine = init_db(_url(db), backup_dir=backups)
    report = migrate.LAST_REPORT
    try:
        assert (report.from_version, report.to_version) == (PHASE1_SCHEMA_VERSION, SCHEMA_VERSION)
        assert report.changed

        # Exactly this wave's DDL, and nothing destructive.
        creates = [s for s in report.applied if s.startswith("CREATE TABLE IF NOT EXISTS ")]
        assert {s.split()[5] for s in creates} == PHASE2_TABLES
        assert [s for s in report.applied if s.startswith("ALTER TABLE ")] == PHASE2_HITL_ALTERS
        for index in ("ix_delivery_receipts_outbox_id", "ix_delivery_receipts_provider_message_id"):
            assert f"CREATE INDEX IF NOT EXISTS {index} ON delivery_receipts" in " ".join(report.applied)
        assert not any(s.startswith("DROP") or " RENAME " in s for s in report.applied)

        # A backup was written BEFORE any of it, and it is the pre-migration file.
        written = sorted(backups.glob(f"previous_version.{PHASE1_SCHEMA_VERSION}-to-{SCHEMA_VERSION}.*.db"))
        assert len(written) == 1 and report.backup_path == written[0]
        assert not (PHASE2_TABLES & _tables(written[0]))
        assert _columns(written[0], "hitl_tasks") == before
        assert _scalar(written[0], "SELECT incident_number FROM incidents") == "INC000001"
        assert _scalar(written[0], "PRAGMA integrity_check") == "ok"

        # The file is now current: every mapped table and column, version stamped, WAL on.
        assert PHASE2_TABLES <= _tables(db)
        assert set(PHASE2_HITL_COLUMNS) <= set(_columns(db, "hitl_tasks"))
        assert _scalar(db, "SELECT MAX(version) FROM schema_version") == SCHEMA_VERSION
        assert _scalar(db, "PRAGMA journal_mode") == "wal"

        # The rows that were there are still there, and the columns added around them carry the
        # DDL defaults SQLite fills in for existing rows.
        session = get_session()
        try:
            inc = session.get(IncidentRow, INCIDENT_ID)
            assert (inc.incident_number, inc.status, inc.priority, inc.site_name) == (
                "INC000001", "IN_PROGRESS", "P2", "Westlands Hub",
            )
            assert [(n.author, n.body) for n in inc.notes] == [("Egypro MSP", "Technician dispatched, ETA 45 min.")]
            assert session.get(WorkNoteRow, "22222222-2222-2222-2222-222222222222").incident_id == INCIDENT_ID
            assert session.get(OutboxRow, "44444444-4444-4444-4444-444444444444").idempotency_key == "k1"

            task = session.get(HitlTaskRow, "33333333-3333-3333-3333-333333333333")
            assert (task.incident_id, task.task_type, task.status) == (INCIDENT_ID, "APPROVE_BROADCAST", "PENDING")
            # An old task is an incident task, has no raiser and was not edited -- so the
            # Phase 2 rules read something true about it instead of tripping over NULL.
            assert task.entity_type == "incident"
            assert task.entity_id is None  # readers fall back to incident_id
            assert task.run_id is None
            assert task.created_by is None
            assert task.edited == 0
            # The new tables exist and are empty: a schema seam, not a feature.
            assert session.scalars(select(MessageTemplateRow)).all() == []
            assert session.scalars(select(DeliveryReceiptRow)).all() == []
        finally:
            session.close()
    finally:
        engine.dispose()


def test_second_run_after_the_phase2_migration_is_idempotent(tmp_path, restore_db_globals):
    db = _build_previous_version_db(tmp_path)
    backups = tmp_path / "backups"

    engine = init_db(_url(db), backup_dir=backups)
    assert migrate.LAST_REPORT.changed and len(list(backups.iterdir())) == 1

    # Direct call: nothing to do, nothing written.
    again = migrate_additive(engine, backup_dir=backups)
    assert again.applied == [] and again.backup_path is None and not again.changed
    assert (again.from_version, again.to_version) == (SCHEMA_VERSION, SCHEMA_VERSION)

    # Whole start-up again: still one backup, still one version row per applied version.
    engine.dispose()
    engine = init_db(_url(db), backup_dir=backups)
    try:
        assert migrate.LAST_REPORT.applied == [] and migrate.LAST_REPORT.backup_path is None
        assert len(list(backups.iterdir())) == 1
        assert _scalar(db, "SELECT COUNT(*) FROM schema_version") == 2  # the v2 stamp and this one
        assert _scalar(db, "SELECT MAX(version) FROM schema_version") == SCHEMA_VERSION
        assert _columns(db, "hitl_tasks").count("created_by") == 1  # no column added twice
    finally:
        engine.dispose()


def test_fresh_database_gets_phase2_in_full_without_a_backup(tmp_path, restore_db_globals):
    """The path every test and every first start takes: created whole, nothing to back up."""
    db, engine = _fresh_db(tmp_path, "brand_new.db")
    try:
        report = migrate.LAST_REPORT
        assert report.from_version == 0 and report.backup_path is None
        assert not (tmp_path / "backups").exists()
        assert PHASE2_TABLES <= _tables(db)
        assert set(PHASE2_HITL_COLUMNS) <= set(_columns(db, "hitl_tasks"))
        assert _scalar(db, "SELECT MAX(version) FROM schema_version") == SCHEMA_VERSION
        assert migrate_additive(engine, backup_dir=tmp_path / "backups").applied == []
    finally:
        engine.dispose()
