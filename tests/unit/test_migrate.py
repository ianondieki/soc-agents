"""Phase 1 (spec §7.0.1): the generic additive migration, proven against a real v1 file.

Since schema_version 8 the migration has ONE non-additive step, the rebuild of ``hitl_tasks``
(``db/migrate.py``, "THE ONE EXCEPTION"), and since 9 a second, narrower one: the CHECK refresh
of EMPTY scorecard tables ("THE SECOND EXCEPTION"). This file pins that a v1 file still gets all
the way to the current version *through* them, and that the hitl_tasks rebuild is the only
destructive thing that runs on such a file — the scorecard tables a v1 file receives are created
current, so the refresh has nothing to do. The rebuild's own proofs are in
``test_migrate_rebuild.py``; the refresh's are in ``test_migrate_checks.py``.

``tests/fixtures/db/v1_baseline.db`` was generated ONCE from the pre-Phase-1
``models.py`` by calling the then-current ``init_db()`` against an empty file and
inserting one incident (INC000001, Westlands Hub) with one MSP work note. It has the
11 original tables, 68 ``incidents`` columns, no ``schema_version`` table and a
rollback journal (page size 1 KB to keep it small). Regenerating it from current code
would silently defeat every test here, which is what ``test_fixture_is_still_v1`` guards.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

import noc_agents.db.models as models
from noc_agents.db import migrate
from noc_agents.db.migrate import SCHEMA_VERSION, migrate_additive
from noc_agents.db.models import Base, IncidentRow, WorkNoteRow, get_session, init_db

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "db" / "v1_baseline.db"
V1_TABLES = {
    "agent_run_steps", "agent_runs", "audit_events", "broadcasts", "daily_sequences",
    "hitl_tasks", "incident_briefs", "incidents", "problems", "shift_ledger", "work_notes",
}
NEW_TABLES = {
    # schema_version 2 (Phase 1)
    "outbox", "scheduler_lease", "scheduled_job_state", "llm_calls", "schema_version",
    # schema_version 3 (Phase 2, §6.3/§6.6)
    "message_templates", "delivery_receipts",
    # schema_version 4 (Phase 3, §7.3.1) — the weather/CAP/flood/KPLC signal cache
    "external_signals",
    # schema_version 5 (Phase 4) — accountability and learning
    "vendors", "incident_clock_events",              # §7.6.1 Lane 4A
    "post_incident_reviews", "pir_action_items",     # §7.7.1 Lane 4B
    "regulatory_notifications", "evidence_packs",    # §7.6.1 Lane 4A
    # schema_version 6 (Phase 5) — scheduling and knowledge
    "maintenance_plans", "maintenance_tasks", "maintenance_windows",   # §7.5 Lane 5A
    "contracts", "contract_clauses", "contract_faq", "contract_queries",  # §7.8 Lane 5B
    "relationship_complaints", "subject_persons",                      # §7.8 Lane 5B
    # schema_version 7 (Phase 5) — capacity, §7.5.1 Lane 5A
    "capacity_observations", "capacity_advisories",
    # schema_version 8 — the additive tables that ride on the same bump as the hitl_tasks rebuild
    "memory_episodes",                                  # §7.11 memory M1 (db/models_memory.py)
    "vendor_scorecards", "vendor_scorecard_lines",      # §7.6 vendor scorecards (db/models_scorecards.py)
    # schema_version 10 — the support desk (docs/SUPPORT_DESK.md, db/models_support.py)
    "support_complaints", "support_steps", "support_tool_calls", "support_messages", "support_eval_runs",
}
# schema_version 8, the one exception to "additive": hitl_tasks is rebuilt so that incident_id
# can be NULL. It appears in neither set above — it is a v1 table that is REPLACED, not added —
# and these are, in order, the only non-additive statements a migration may ever report.
HITL_REBUILD_DESTRUCTIVE = [
    "DROP TABLE hitl_tasks",
    "ALTER TABLE hitl_tasks__v8_rebuild RENAME TO hitl_tasks",
]
# schema_version 9 adds no table and no column (see SCHEMA_VERSION's comment in db/migrate.py):
# the v1 path below must show NO scorecard DROP either. schema_version 10 adds the five support
# tables above and no column, so the ALTER list below is unchanged by it.
NEW_INCIDENT_COLUMNS = {
    "restored_source", "restored_by", "vendor_id", "context_json",
    "planned_maintenance", "access_risk", "child_site_ids_json", "assignment_confidence",
}
INCIDENT_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture()
def restore_db_globals():
    """init_db() rebinds the module-level engine/session factory; put the previous ones back."""
    saved = (models._engine, models.SessionLocal)
    yield
    models._engine, models.SessionLocal = saved


def _copy_fixture(tmp_path: Path) -> Path:
    target = tmp_path / "v1_baseline.db"
    shutil.copy2(FIXTURE, target)
    return target


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


def _notnull(path: Path, table: str, column: str) -> int | None:
    """The column's NOT NULL flag in the live catalogue; None when there is no such column."""
    con = sqlite3.connect(path)
    try:
        return next((r[3] for r in con.execute(f"PRAGMA table_info({table})") if r[1] == column), None)
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


def test_fixture_is_still_v1():
    """Guard: the committed file must be a pre-Phase-1 database, or nothing below proves anything."""
    assert _tables(FIXTURE) == V1_TABLES
    assert "schema_version" not in _tables(FIXTURE)
    cols = _columns(FIXTURE, "incidents")
    assert len(cols) == 68 and not (NEW_INCIDENT_COLUMNS & set(cols))
    assert _scalar(FIXTURE, "PRAGMA journal_mode") == "delete"
    assert _scalar(FIXTURE, "SELECT COUNT(*) FROM incidents") == 1
    assert _scalar(FIXTURE, "SELECT COUNT(*) FROM work_notes") == 1


def test_v1_file_migrates_to_current_schema(tmp_path, restore_db_globals):
    db = _copy_fixture(tmp_path)
    backups = tmp_path / "backups"

    engine = init_db(_url(db), backup_dir=backups)
    report = migrate.LAST_REPORT

    # Every mapped table, column and index now exists -- the generic assertion, so a
    # later wave that adds a column to models.py is covered without editing this test.
    assert V1_TABLES | NEW_TABLES <= _tables(db)
    for table in Base.metadata.sorted_tables:
        present = set(_columns(db, table.name))
        missing = {c.name for c in table.columns} - present
        assert not missing, f"{table.name} is missing {sorted(missing)}"
        present_idx = _indexes(db, table.name)
        assert {ix.name for ix in table.indexes} <= present_idx, table.name
    assert NEW_INCIDENT_COLUMNS <= set(_columns(db, "incidents"))
    assert "ix_outbox_status_next" in _indexes(db, "outbox")

    # The report is the startup log: exactly the DDL that ran, nothing that was a no-op.
    assert report is not None
    assert (report.from_version, report.to_version) == (1, SCHEMA_VERSION)
    assert report.changed
    creates = [s for s in report.applied if s.startswith("CREATE TABLE IF NOT EXISTS ")]
    assert {s.split()[5] for s in creates} == NEW_TABLES
    assert "CREATE INDEX IF NOT EXISTS ix_outbox_status_next ON outbox (status, next_attempt_at)" in report.applied
    # An index added to a model after the table shipped reaches an old file through the same
    # additive pass (and, since round 4, a CURRENT file through every start -- test_audit_index.py).
    assert "CREATE INDEX IF NOT EXISTS ix_audit_events_entity_action ON audit_events (entity_type, entity_id, action)" in report.applied
    alters = [s for s in report.applied if s.startswith("ALTER TABLE ")]
    assert alters == [
        "ALTER TABLE incidents ADD COLUMN restored_source TEXT",
        "ALTER TABLE incidents ADD COLUMN restored_by TEXT",
        "ALTER TABLE incidents ADD COLUMN vendor_id TEXT",
        "ALTER TABLE incidents ADD COLUMN context_json TEXT",
        "ALTER TABLE incidents ADD COLUMN planned_maintenance INTEGER DEFAULT 0",
        "ALTER TABLE incidents ADD COLUMN access_risk INTEGER DEFAULT 0",
        "ALTER TABLE incidents ADD COLUMN child_site_ids_json TEXT DEFAULT '[]'",
        "ALTER TABLE incidents ADD COLUMN assignment_confidence TEXT DEFAULT 'high'",
        # schema_version 5 (Phase 4, §7.7.1): the known-error fields on problems. Ordered
        # before hitl_tasks because the list follows Base.metadata.sorted_tables, which is
        # dependency order -- problems has no foreign keys, hitl_tasks depends on incidents.
        "ALTER TABLE problems ADD COLUMN root_cause TEXT",
        "ALTER TABLE problems ADD COLUMN workaround TEXT",
        "ALTER TABLE problems ADD COLUMN is_known_error INTEGER DEFAULT 0",
        "ALTER TABLE problems ADD COLUMN known_error_since DATETIME",
        "ALTER TABLE problems ADD COLUMN permanent_fix_plan TEXT",
        "ALTER TABLE problems ADD COLUMN owner_token TEXT",
        "ALTER TABLE problems ADD COLUMN target_date DATETIME",
        "ALTER TABLE problems ADD COLUMN closed_at DATETIME",
        "ALTER TABLE problems ADD COLUMN closure_summary TEXT",
        "ALTER TABLE hitl_tasks ADD COLUMN run_id TEXT",
        "ALTER TABLE hitl_tasks ADD COLUMN entity_type TEXT DEFAULT 'incident'",
        "ALTER TABLE hitl_tasks ADD COLUMN entity_id TEXT",
        "ALTER TABLE hitl_tasks ADD COLUMN created_by TEXT",
        "ALTER TABLE hitl_tasks ADD COLUMN edited INTEGER DEFAULT 0",
        # schema_version 8: the additive half of "a HITL task owns itself". Added to the OLD
        # table on purpose, so that the rebuild below copies like for like, column for column.
        "ALTER TABLE hitl_tasks ADD COLUMN operator_id VARCHAR(32)",
        # ...and the non-additive half, which is an ALTER only because SQLite spells RENAME so.
        "ALTER TABLE hitl_tasks__v8_rebuild RENAME TO hitl_tasks",
    ]
    # This line used to read "nothing destructive, ever". It now reads "exactly one thing, to
    # exactly one table": the v8 rebuild of hitl_tasks, after the additive pass, in this order.
    # (The v9 CHECK refresh has nothing to do here: a v1 file gets its scorecard tables created
    # from the current model a few statements earlier, so their DDL already matches.)
    assert [s for s in report.applied if s.startswith("DROP") or " RENAME " in s] == HITL_REBUILD_DESTRUCTIVE
    assert not any("vendor_scorecard" in s and s.startswith("DROP") for s in report.applied)
    rebuild_at = report.applied.index("DROP TABLE hitl_tasks")
    assert all(s.startswith(("CREATE INDEX ix_hitl_tasks_", "ALTER TABLE hitl_tasks__v8_rebuild RENAME"))
               for s in report.applied[rebuild_at + 1:]), "nothing but the swap and its indexes follows the DROP"
    assert report.applied[rebuild_at - 2].startswith("CREATE TABLE hitl_tasks__v8_rebuild (")
    assert report.applied[rebuild_at - 1].startswith("INSERT INTO hitl_tasks__v8_rebuild (rowid, ")
    # The v1 table said incident_id NOT NULL; the rebuilt one does not, and owns itself.
    assert _notnull(db, "hitl_tasks", "incident_id") == 0 and _notnull(db, "hitl_tasks", "id") == 1
    assert _notnull(db, "hitl_tasks", "operator_id") == 0  # nullable on purpose: see HitlTaskRow.operator_id
    assert {"ix_hitl_tasks_incident_id", "ix_hitl_tasks_operator_id"} <= _indexes(db, "hitl_tasks")

    # Old rows still read through the ORM, and the new columns carry their DDL defaults.
    session = get_session()
    try:
        inc = session.get(IncidentRow, INCIDENT_ID)
        assert inc is not None
        assert (inc.incident_number, inc.status, inc.priority, inc.site_name) == ("INC000001", "IN_PROGRESS", "P2", "Westlands Hub")
        assert inc.technology == "4G" and inc.child_sites_down == 0
        assert inc.restored_source is None and inc.restored_by is None and inc.vendor_id is None and inc.context_json is None
        assert (inc.planned_maintenance, inc.access_risk) == (0, 0)
        assert inc.child_site_ids_json == "[]" and inc.assignment_confidence == "high"
        assert [(n.author, n.body) for n in inc.notes] == [("Egypro MSP", "Technician dispatched, ETA 45 min.")]
        assert session.get(WorkNoteRow, "22222222-2222-2222-2222-222222222222").incident_id == INCIDENT_ID
    finally:
        session.close()

    # Version stamped, WAL on.
    assert _scalar(db, "SELECT MAX(version) FROM schema_version") == SCHEMA_VERSION
    assert _scalar(db, "SELECT COUNT(*) FROM schema_version") == 1
    assert _scalar(db, "PRAGMA journal_mode") == "wal"

    # A backup was written BEFORE any change: it is the v1 file, byte-for-byte in shape.
    written = sorted(backups.glob(f"v1_baseline.1-to-{SCHEMA_VERSION}.*.db"))
    assert len(written) == 1 and report.backup_path == written[0]
    assert _tables(written[0]) == V1_TABLES
    assert len(_columns(written[0], "incidents")) == 68
    assert _scalar(written[0], "SELECT incident_number FROM incidents") == "INC000001"
    assert _scalar(written[0], "PRAGMA integrity_check") == "ok"
    engine.dispose()


def test_second_run_is_idempotent(tmp_path, restore_db_globals):
    db = _copy_fixture(tmp_path)
    backups = tmp_path / "backups"
    engine = init_db(_url(db), backup_dir=backups)
    first = migrate.LAST_REPORT
    assert first.changed and len(list(backups.iterdir())) == 1

    # Direct call: nothing to do, nothing written.
    again = migrate_additive(engine, backup_dir=backups)
    assert again.applied == [] and again.backup_path is None and not again.changed
    assert (again.from_version, again.to_version) == (SCHEMA_VERSION, SCHEMA_VERSION)

    # Whole start-up again: still one backup file, still one version row, WAL still on.
    engine.dispose()
    engine = init_db(_url(db), backup_dir=backups)
    assert migrate.LAST_REPORT.applied == [] and migrate.LAST_REPORT.backup_path is None
    assert len(list(backups.iterdir())) == 1
    assert _scalar(db, "SELECT COUNT(*) FROM schema_version") == 1
    assert _scalar(db, "PRAGMA journal_mode") == "wal"
    engine.dispose()


def test_fresh_database_is_created_in_full_without_a_backup(tmp_path, restore_db_globals):
    """Every test and first-ever start goes through here: no backup dir, no backup file."""
    db = tmp_path / "fresh.db"
    backups = tmp_path / "backups"
    engine = init_db(_url(db), backup_dir=backups)
    report = migrate.LAST_REPORT
    assert report.from_version == 0 and report.backup_path is None
    assert not backups.exists()
    assert V1_TABLES | NEW_TABLES <= _tables(db)
    assert _scalar(db, "SELECT MAX(version) FROM schema_version") == SCHEMA_VERSION
    assert _scalar(db, "PRAGMA journal_mode") == "wal"
    # ...and the second start is the fast no-op path.
    assert migrate_additive(engine, backup_dir=backups).applied == []
    engine.dispose()


def test_default_backup_dir_sits_beside_the_database(tmp_path, restore_db_globals):
    db = _copy_fixture(tmp_path)
    engine = init_db(_url(db))
    assert migrate.LAST_REPORT.backup_path.parent == tmp_path / "backups"
    engine.dispose()


def test_failure_rolls_back_everything_and_leaves_version_unchanged(tmp_path, monkeypatch, restore_db_globals):
    """DDL + version stamp are one transaction: a failure leaves the v1 file untouched, backup kept."""
    db = _copy_fixture(tmp_path)
    backups = tmp_path / "backups"

    def boom(conn, version):
        raise RuntimeError("simulated crash before the version stamp")

    monkeypatch.setattr(migrate, "_stamp_version", boom)
    with pytest.raises(RuntimeError, match="simulated crash"):
        init_db(_url(db), backup_dir=backups)

    assert _tables(db) == V1_TABLES  # no new tables, no schema_version table -- and no half-built rebuild table
    assert len(_columns(db, "incidents")) == 68  # no new columns
    # The stamp is the LAST thing in the transaction, so by the time it "crashed" the v8
    # rebuild had already dropped and replaced hitl_tasks. All of that is undone too:
    assert "operator_id" not in _columns(db, "hitl_tasks")
    assert _notnull(db, "hitl_tasks", "incident_id") == 1
    assert _scalar(db, "SELECT incident_number FROM incidents") == "INC000001"
    assert len(list(backups.glob(f"v1_baseline.1-to-{SCHEMA_VERSION}.*.db"))) == 1  # the safety net stayed

    # The next start simply retries and succeeds.
    monkeypatch.undo()
    engine = init_db(_url(db), backup_dir=backups)
    assert migrate.LAST_REPORT.changed
    assert _scalar(db, "SELECT MAX(version) FROM schema_version") == SCHEMA_VERSION
    engine.dispose()


def test_folded_extra_incident_columns_still_migrate_with_their_defaults(tmp_path, restore_db_globals):
    """The retired _EXTRA_INCIDENT_COLS list is now server_default on the mapped columns:
    a file older than those columns gets the same ALTER statements it always did."""
    db = _copy_fixture(tmp_path)
    con = sqlite3.connect(db)
    for col in ("technology", "child_sites_down", "assignment_rationale", "msp_eta_at"):
        con.execute(f"ALTER TABLE incidents DROP COLUMN {col}")
    con.commit()
    con.close()

    engine = init_db(_url(db), backup_dir=tmp_path / "backups")
    applied = migrate.LAST_REPORT.applied
    for expected in (
        "ALTER TABLE incidents ADD COLUMN technology VARCHAR(64) DEFAULT '4G'",
        "ALTER TABLE incidents ADD COLUMN child_sites_down INTEGER DEFAULT 0",
        "ALTER TABLE incidents ADD COLUMN assignment_rationale TEXT DEFAULT ''",
        "ALTER TABLE incidents ADD COLUMN msp_eta_at DATETIME",
    ):
        assert expected in applied
    session = get_session()
    try:
        inc = session.get(IncidentRow, INCIDENT_ID)
        assert (inc.technology, inc.child_sites_down, inc.assignment_rationale, inc.msp_eta_at) == ("4G", 0, "", None)
    finally:
        session.close()
    engine.dispose()


def test_outbox_idempotency_key_is_unique(tmp_path, restore_db_globals):
    db = _copy_fixture(tmp_path)
    engine = init_db(_url(db), backup_dir=tmp_path / "backups")
    con = sqlite3.connect(db)
    try:
        row = "INSERT INTO outbox (id, operator_id, created_at, updated_at, kind, idempotency_key, payload_json) VALUES (?, 'safaricom', '2026-09-16 00:00:00', '2026-09-16 00:00:00', 'EMAIL', 'k1', '{}')"
        con.execute(row, ("a",))
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(row, ("b",))
        con.rollback()
        # DDL defaults hold for a raw insert too.
        con.execute(row, ("c",))
        status, attempts, max_attempts, requires_hitl = con.execute(
            "SELECT status, attempts, max_attempts, requires_hitl FROM outbox WHERE id='c'"
        ).fetchone()
        assert (status, attempts, max_attempts, requires_hitl) == ("PENDING", 0, 3, 0)
        con.rollback()
    finally:
        con.close()
    engine.dispose()
