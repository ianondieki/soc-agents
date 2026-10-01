"""schema_version 8: the ONE non-additive migration — the rebuild of ``hitl_tasks``.

``db/migrate.py`` is additive-only with a single named exception: making
``hitl_tasks.incident_id`` nullable, which SQLite cannot do in place, so the table is created
again, copied, verified, dropped and renamed inside the migration's own backup-first
transaction (module docstring there, "THE ONE EXCEPTION"). ``hitl_tasks`` is the human approval
trail for customer broadcasts and regulator notices. The failure this file exists to make
impossible is the quiet one: a copy that is *nearly* complete.

What is proven here, in the order the risk runs:

1. a populated v7 file — both operators, every task status, claims, resolutions, reasons,
   awkward payloads, a rowid gap — comes out with every column of every row unchanged
   (value AND storage class), ``operator_id`` backfilled per operator, and a backup that is
   the *old* file, so it was demonstrably taken first;
2. a failure injected at three points — after the copy, BETWEEN the DROP and the RENAME, and
   after the swap — leaves the original table and every row exactly as they were, the version
   unchanged, and the next start succeeds;
3. the verification really refuses: a truncated copy, an altered copy, a wrong owner;
4. it runs once. Twice is a no-op, a fresh database is never rebuilt, and a rebuilt file
   re-stamped with an old version is still left alone — the live catalogue decides;
5. what hangs off the table survives (mapped indexes, a hand-made index, a trigger); a view or
   a trigger on ANOTHER table that mentions hitl_tasks, a NULL in a column the model declares
   NOT NULL, and a column the model does not know are each refused BEFORE the backup, with the
   file untouched, no backup written and the remedy in the message;
6. a task whose incident is missing is preserved, unowned and invisible, and named in the log;
7. a connection that enforces foreign keys still migrates, and gets its pragma back even when
   the write lock cannot be taken.

How the v7 file is built: by reconstruction, the way ``test_phase2_schema.py`` builds its v2
file — current code creates the database, then ``hitl_tasks`` is replaced with the exact DDL
the v7 release produced and the version is re-stamped. Task rows are written with raw SQL,
which is literally what a v7 writer did: an INSERT that has never heard of ``operator_id``.
Two shapes of v7 table exist in the wild and both are exercised: one *created* at v7 and one
that grew up from the v1 fixture by ``ALTER TABLE ADD COLUMN`` (whose Phase 2 columns are
nullable — the additive path never writes NOT NULL).
"""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import OperationalError

import noc_agents.db.models as models
from noc_agents.api.deps import _owned
from noc_agents.config import clear_settings_cache
from noc_agents.db import migrate
from noc_agents.db.migrate import SCHEMA_VERSION, HitlRebuildError, migrate_additive
from noc_agents.db.models import HitlTaskRow, IncidentRow, OutboxRow, get_session, init_db

# The statement a database CREATED by the v7 release got, verbatim from its sqlite_master.
V7_CREATED = (
    "CREATE TABLE hitl_tasks ( id VARCHAR(36) NOT NULL, incident_id VARCHAR(36) NOT NULL, "
    "task_type VARCHAR(64) NOT NULL, proposed_payload_json TEXT NOT NULL, status VARCHAR(16) NOT NULL, "
    "created_at DATETIME NOT NULL, resolved_by VARCHAR(128), resolved_at DATETIME, claimed_by VARCHAR(128), "
    "claimed_at DATETIME, reason TEXT, run_id TEXT, entity_type TEXT DEFAULT 'incident' NOT NULL, "
    "entity_id TEXT, created_by TEXT, edited INTEGER DEFAULT 0 NOT NULL, PRIMARY KEY (id), "
    "FOREIGN KEY(incident_id) REFERENCES incidents (id) )"
)
# ...and one that started as the v1 table and was brought to v7 additively: the five Phase 2
# columns arrive by ALTER TABLE, which db/migrate.py renders without NOT NULL.
V7_GROWN = (
    "CREATE TABLE hitl_tasks (\n\tid VARCHAR(36) NOT NULL, \n\tincident_id VARCHAR(36) NOT NULL, \n\t"
    "task_type VARCHAR(64) NOT NULL, \n\tproposed_payload_json TEXT NOT NULL, \n\tstatus VARCHAR(16) NOT NULL, "
    "\n\tcreated_at DATETIME NOT NULL, \n\tresolved_by VARCHAR(128), \n\tresolved_at DATETIME, \n\t"
    "claimed_by VARCHAR(128), \n\tclaimed_at DATETIME, \n\treason TEXT, \n\tPRIMARY KEY (id), \n\t"
    "FOREIGN KEY(incident_id) REFERENCES incidents (id)\n)",
    "ALTER TABLE hitl_tasks ADD COLUMN run_id TEXT",
    "ALTER TABLE hitl_tasks ADD COLUMN entity_type TEXT DEFAULT 'incident'",
    "ALTER TABLE hitl_tasks ADD COLUMN entity_id TEXT",
    "ALTER TABLE hitl_tasks ADD COLUMN created_by TEXT",
    "ALTER TABLE hitl_tasks ADD COLUMN edited INTEGER DEFAULT 0",
)
V7_SHAPES = {"created-at-v7": (V7_CREATED,), "grown-from-v1": V7_GROWN}
V7_INDEX = "CREATE INDEX ix_hitl_tasks_incident_id ON hitl_tasks (incident_id)"
V7_COLUMNS = (
    "id", "incident_id", "task_type", "proposed_payload_json", "status", "created_at", "resolved_by",
    "resolved_at", "claimed_by", "claimed_at", "reason", "run_id", "entity_type", "entity_id", "created_by", "edited",
)

# incident id -> operator. Three safaricom, two airtel: both operators' approval trails live in
# ONE file (spec §8), which is exactly why the backfill has to be per row and not per file.
INCIDENTS = {
    "inc-saf-1": "safaricom", "inc-saf-2": "safaricom", "inc-saf-3": "safaricom",
    "inc-atl-1": "airtel", "inc-atl-2": "airtel",
}
T = "2026-09-0{d} 0{h}:15:30.123456"  # SQLAlchemy's storage format for DateTime on SQLite

# A payload chosen to be unkind to a careless copy: quotes, a backslash, a newline, non-ASCII,
# an emoji outside the BMP, and something that looks like SQL.
AWKWARD = (
    '{"sms": "Hitilafu ya umeme — Embakasi HUB. Don\'t reply.\\nLine 2", "note": "50% of \\"sites\\"", '
    '"emoji": "\U0001F4E1", "sql": "\'; DROP TABLE hitl_tasks;--", "audiences": ["RNIO", "FIELD_ENGINEER"]}'
)

# (id, incident, task_type, payload, status, created, resolved_by, resolved_at, claimed_by, claimed_at,
#  reason, run_id, entity_type, entity_id, created_by, edited)
TASKS = [
    ("t-01", "inc-saf-1", "APPROVE_BROADCAST", AWKWARD, "PENDING", T.format(d=1, h=1), None, None, None, None,
     None, "run-1", "incident", "inc-saf-1", "agent:SupervisorAgent", 0),
    ("t-02", "inc-saf-1", "GENERIC", '{"reason": "sla_or_silence_escalation"}', "CLAIMED", T.format(d=1, h=2),
     None, None, "Peter Kamau", T.format(d=1, h=3), None, None, "incident", None, None, 0),
    ("t-03", "inc-saf-2", "APPROVE_BROADCAST", '{"priority": "P1"}', "APPROVED", T.format(d=2, h=1),
     "Grace Wanjiru", T.format(d=2, h=4), "Grace Wanjiru", T.format(d=2, h=2), "wording checked with CCC",
     "run-2", "incident", "inc-saf-2", "agent:SupervisorAgent", 1),
    ("t-04", "inc-saf-2", "APPROVE_REGULATORY_NOTICE", '{"kind": "CA_INITIAL"}', "REJECTED", T.format(d=2, h=5),
     "Grace Wanjiru", T.format(d=2, h=6), None, None, "threshold not met — re-evaluate", None,
     "regulatory_notification", "notice-9", "Peter Kamau", 0),
    ("t-05", "inc-saf-3", "APPROVE_HANDOVER", "{}", "APPROVED", T.format(d=3, h=1), "Grace Wanjiru",
     T.format(d=3, h=2), None, None, "", None, "handover", "shift-2026-09-03-A", "agent:ShiftHandoverAgent", 0),
    # The maintenance lane's anchored card: about a window, filed against an unrelated outage.
    ("t-06", "inc-saf-3", "APPROVE_MAINTENANCE_WINDOW", '{"rain_season_flag": 1}', "PENDING", T.format(d=3, h=3),
     None, None, None, None, None, None, "maintenance_window", "win-1", "agent:MaintenancePlanningAgent", 0),
    ("t-07", "inc-atl-1", "APPROVE_BROADCAST", '{"priority": "P2"}', "PENDING", T.format(d=4, h=1), None, None,
     None, None, None, "run-7", "incident", "inc-atl-1", "agent:SupervisorAgent", 0),
    ("t-08", "inc-atl-1", "APPROVE_BROADCAST", '{"priority": "P2", "v": 2}', "REJECTED", T.format(d=4, h=2),
     "Their Manager", T.format(d=4, h=3), "Their Manager", T.format(d=4, h=2), "duplicate", "run-8",
     "incident", "inc-atl-1", "agent:SupervisorAgent", 0),
    ("t-09", "inc-atl-2", "GENERIC", '{"detail": "silent 95 min"}', "CLAIMED", T.format(d=5, h=1), None, None,
     "Their Analyst", T.format(d=5, h=2), None, None, "incident", None, None, 0),
    ("t-10", "inc-atl-2", "APPROVE_BROADCAST", '{"priority": "P1"}', "APPROVED", T.format(d=5, h=3),
     "Their Manager", T.format(d=5, h=4), None, None, None, "run-10", "incident", "inc-atl-2",
     "agent:SupervisorAgent", 1),
]
DELETED_BEFORE_MIGRATION = "t-02"  # leaves a rowid gap, so "the same rowids" is a real claim


@pytest.fixture()
def restore_db_globals():
    """init_db() rebinds the module-level engine/session factory; put the previous ones back."""
    saved = (models._engine, models.SessionLocal)
    yield
    models._engine, models.SessionLocal = saved


# ------------------------------------------------------------------------------ helpers


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _sql(path: Path, sql: str, params: tuple = ()) -> list[tuple]:
    con = sqlite3.connect(path)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def _scalar(path: Path, sql: str):
    return _sql(path, sql)[0][0]


def _table_sql(path: Path) -> str:
    return _scalar(path, "SELECT sql FROM sqlite_master WHERE type='table' AND name='hitl_tasks'")


def _attached(path: Path) -> list[tuple]:
    """Every index and trigger on the table: (type, name, sql). Auto-indexes have NULL sql."""
    return _sql(
        path,
        "SELECT type, name, sql FROM sqlite_master WHERE tbl_name='hitl_tasks' AND type IN ('index','trigger') "
        "ORDER BY type, name",
    )


def _notnull(path: Path, column: str) -> int | None:
    for _cid, name, _type, notnull, _default, _pk in _sql(path, "PRAGMA table_info(hitl_tasks)"):
        if name == column:
            return notnull
    return None


def _snapshot(path: Path) -> list[tuple]:
    """Every v7 column of every row, as stored: rowid, then (value, typeof, hex) per column.

    ``hex`` is the bytes on disk, so "byte for byte" is meant literally; ``typeof`` catches a
    value that came back as a different storage class (1 vs 1.0 vs '1'), which ``==`` forgives.
    Read with sqlite3 directly — nothing of the ORM, and nothing of the code under test.
    """
    cells = ", ".join(f"{c}, typeof({c}), hex({c})" for c in V7_COLUMNS)
    return _sql(path, f"SELECT rowid, {cells} FROM hitl_tasks ORDER BY rowid")


def _build_v7(tmp_path: Path, shape: str = "created-at-v7", *, extra_sql: tuple[str, ...] = ()) -> Path:
    """A populated schema_version 7 file. See the module docstring for why it is built this way."""
    db = tmp_path / "noc_v7.db"
    engine = init_db(_url(db), backup_dir=tmp_path / "scratch")
    session = get_session()
    try:
        for n, (incident_id, operator) in enumerate(INCIDENTS.items(), start=1):
            session.add(IncidentRow(
                id=incident_id, operator_id=operator, incident_number=f"{operator[:3].upper()}-{n:06d}",
                site_id=f"SITE-{n}", region_code="NBI", correlation_fingerprint=f"fp-{n}",
            ))
        # Soft references: outbox.hitl_task_id is plain text, not a foreign key. They keep
        # resolving only if the rebuild preserves every id.
        session.add(OutboxRow(id="ob-1", operator_id="safaricom", kind="EMAIL", idempotency_key="k-ob-1",
                              payload_json="{}", hitl_task_id="t-05", status="HELD"))
        session.add(OutboxRow(id="ob-2", operator_id="airtel", kind="SMS", idempotency_key="k-ob-2",
                              payload_json="{}", hitl_task_id="t-07", status="HELD"))
        session.commit()
    finally:
        session.close()
        engine.dispose()

    con = sqlite3.connect(db)
    try:
        con.execute("DROP TABLE hitl_tasks")  # takes both v8 indexes with it
        for statement in V7_SHAPES[shape]:
            con.execute(statement)
        con.execute(V7_INDEX)
        con.executemany(f"INSERT INTO hitl_tasks ({', '.join(V7_COLUMNS)}) VALUES ({', '.join('?' * len(V7_COLUMNS))})", TASKS)
        con.execute("DELETE FROM hitl_tasks WHERE id = ?", (DELETED_BEFORE_MIGRATION,))
        for statement in extra_sql:
            con.execute(statement)
        con.execute("DELETE FROM schema_version")
        con.execute("INSERT INTO schema_version (version, applied_at) VALUES (7, '2026-09-20 00:00:00')")
        con.commit()
    finally:
        con.close()

    # Guard: the file really is what the v7 release left behind, or nothing below proves anything.
    assert _notnull(db, "incident_id") == 1
    assert _notnull(db, "operator_id") is None, "a v7 file has never heard of hitl_tasks.operator_id"
    assert [r[1] for r in _attached(db) if r[2]] == ["ix_hitl_tasks_incident_id"] or extra_sql
    assert _scalar(db, "SELECT MAX(version) FROM schema_version") == 7
    extra_rows = sum(1 for s in extra_sql if s.startswith("INSERT INTO hitl_tasks"))
    assert _scalar(db, "SELECT COUNT(*) FROM hitl_tasks") == len(TASKS) - 1 + extra_rows
    return db


def _assert_untouched(db: Path, before: list[tuple], table_sql: str, attached: list[tuple]) -> None:
    """The file is exactly the v7 file: original table, every row, same indexes, version 7."""
    assert _table_sql(db) == table_sql, "the ORIGINAL table definition must be back, not a lookalike"
    assert _snapshot(db) == before
    assert _attached(db) == attached
    assert _notnull(db, "incident_id") == 1 and _notnull(db, "operator_id") is None
    assert _sql(db, "SELECT name FROM sqlite_master WHERE name LIKE '%rebuild%'") == []
    assert _scalar(db, "SELECT MAX(version) FROM schema_version") == 7
    assert _scalar(db, "SELECT COUNT(*) FROM schema_version") == 1
    assert _scalar(db, "PRAGMA integrity_check") == "ok"


# =================================================================== 1. the populated migration


@pytest.mark.parametrize("shape", sorted(V7_SHAPES))
def test_a_populated_v7_file_migrates_with_every_row_preserved(tmp_path, monkeypatch, restore_db_globals, shape):
    # What a brand-new database's hitl_tasks looks like, to compare the migrated one against.
    # Built FIRST: init_db() rebinds the module-level session factory to whatever it opened last.
    fresh = tmp_path / "fresh.db"
    init_db(_url(fresh), backup_dir=tmp_path / "unused").dispose()
    fresh_columns = _sql(fresh, "PRAGMA table_info(hitl_tasks)")

    db = _build_v7(tmp_path, shape)
    before = _snapshot(db)
    old_table_sql = _table_sql(db)
    backups = tmp_path / "backups"

    engine = init_db(_url(db), backup_dir=backups)
    report = migrate.LAST_REPORT
    try:
        assert (report.from_version, report.to_version) == (7, SCHEMA_VERSION)

        # --- every column of every row, byte for byte, same rowids (the gap included) ---
        assert _snapshot(db) == before
        assert [r[0] for r in before] != list(range(1, len(before) + 1)), "the fixture lost its rowid gap"

        # --- operator_id backfilled from each row's own incident, per operator ---
        owners = dict(_sql(db, "SELECT id, operator_id FROM hitl_tasks"))
        assert owners == {t[0]: INCIDENTS[t[1]] for t in TASKS if t[0] != DELETED_BEFORE_MIGRATION}
        assert set(owners.values()) == {"safaricom", "airtel"}, "both operators must be in the file"

        # --- the new shape, and nothing else changed about the table ---
        assert _notnull(db, "incident_id") == 0
        assert _notnull(db, "operator_id") == 0  # nullable on purpose: see HitlTaskRow.operator_id
        assert {r[1] for r in _attached(db)} == {
            "sqlite_autoindex_hitl_tasks_1", "ix_hitl_tasks_incident_id", "ix_hitl_tasks_operator_id",
        }
        assert _sql(db, "PRAGMA table_info(hitl_tasks)") == fresh_columns, (
            "a migrated file and a brand-new file must have the same hitl_tasks, column for column"
        )
        assert _scalar(db, "PRAGMA integrity_check") == "ok"
        assert _scalar(db, "SELECT MAX(version) FROM schema_version") == SCHEMA_VERSION

        # --- the report says what really ran: one rebuild, in this order ---
        rebuild = [s for s in report.applied if "hitl_tasks" in s and not s.startswith("CREATE TABLE IF NOT EXISTS")]
        assert [s.split(" (")[0].split(" SELECT")[0] for s in rebuild] == [
            "ALTER TABLE hitl_tasks ADD COLUMN operator_id VARCHAR(32)",
            "CREATE INDEX IF NOT EXISTS ix_hitl_tasks_operator_id ON hitl_tasks",
            "CREATE TABLE hitl_tasks__v8_rebuild",
            "INSERT INTO hitl_tasks__v8_rebuild",
            "DROP TABLE hitl_tasks",
            "ALTER TABLE hitl_tasks__v8_rebuild RENAME TO hitl_tasks",
            "CREATE INDEX ix_hitl_tasks_incident_id ON hitl_tasks",
            "CREATE INDEX ix_hitl_tasks_operator_id ON hitl_tasks",
        ]
        # ...and hitl_tasks is the ONLY table anything destructive was done to.
        destructive = [s for s in report.applied if s.startswith("DROP") or " RENAME " in s]
        assert destructive == ["DROP TABLE hitl_tasks", "ALTER TABLE hitl_tasks__v8_rebuild RENAME TO hitl_tasks"]

        # --- the backup was written FIRST: it is the v7 file, old table and all ---
        written = sorted(backups.glob(f"noc_v7.7-to-{SCHEMA_VERSION}.*.db"))
        assert len(written) == 1 and report.backup_path == written[0]
        assert _table_sql(written[0]) == old_table_sql
        assert _notnull(written[0], "incident_id") == 1 and _notnull(written[0], "operator_id") is None
        assert _snapshot(written[0]) == before
        assert _scalar(written[0], "SELECT MAX(version) FROM schema_version") == 7
        assert _scalar(written[0], "PRAGMA integrity_check") == "ok"

        # --- soft references by id still resolve (outbox.hitl_task_id is text, not a FK) ---
        assert _sql(
            db, "SELECT o.id, t.task_type FROM outbox o JOIN hitl_tasks t ON t.id = o.hitl_task_id ORDER BY o.id"
        ) == [("ob-1", "APPROVE_HANDOVER"), ("ob-2", "APPROVE_BROADCAST")]

        # --- and the point of it all: each operator sees exactly its own trail, through _owned ---
        for operator in ("safaricom", "airtel"):
            monkeypatch.setenv("OPERATOR_PROFILE", operator)
            clear_settings_cache()
            session = get_session()
            try:
                seen = {t.id: t for t in session.scalars(_owned(HitlTaskRow))}
            finally:
                session.close()
            assert set(seen) == {i for i, op in owners.items() if op == operator}
            assert all(t.operator_id == operator for t in seen.values())
        # The decision trail reads back through the ORM exactly as it was written.
        session = get_session()
        try:
            t3 = session.get(HitlTaskRow, "t-03")
            assert (t3.status, t3.resolved_by, t3.claimed_by, t3.reason, t3.edited, t3.run_id) == (
                "APPROVED", "Grace Wanjiru", "Grace Wanjiru", "wording checked with CCC", 1, "run-2",
            )
            assert t3.resolved_at.isoformat() == "2026-09-02T04:15:30.123456"
            assert session.get(HitlTaskRow, "t-01").proposed_payload["emoji"] == "\U0001F4E1"
            assert session.get(HitlTaskRow, DELETED_BEFORE_MIGRATION) is None
        finally:
            session.close()
    finally:
        clear_settings_cache()
        engine.dispose()


# ================================================================ 2. failure in the middle of it


def _boom(*_args, **_kwargs):
    raise RuntimeError("simulated crash")


def _drop_then_crash(conn, attached):
    """The most dangerous instant there is: the approval trail's table has just been dropped
    and its replacement does not answer to the name yet."""
    conn.exec_driver_sql("DROP TABLE hitl_tasks")
    raise RuntimeError("simulated crash")


FAILURE_POINTS = {
    "after the copy, before it is verified": ("_verify_hitl_copy", _boom),
    "between DROP TABLE and RENAME": ("_swap_hitl_tables", _drop_then_crash),
    "after the swap, before the version stamp": ("_stamp_version", _boom),
}


@pytest.mark.parametrize("where", sorted(FAILURE_POINTS))
def test_a_failure_mid_rebuild_leaves_the_original_table_and_every_row(tmp_path, monkeypatch, restore_db_globals, where):
    db = _build_v7(tmp_path)
    before, table_sql, attached = _snapshot(db), _table_sql(db), _attached(db)
    backups = tmp_path / "backups"

    name, replacement = FAILURE_POINTS[where]
    monkeypatch.setattr(migrate, name, replacement)
    with pytest.raises(RuntimeError, match="simulated crash"):
        init_db(_url(db), backup_dir=backups)
    models._engine.dispose()

    _assert_untouched(db, before, table_sql, attached)
    assert len(list(backups.glob("noc_v7.7-to-*.db"))) == 1, "the safety net was written before the failure, and stays"

    # The next start simply retries, and this time gets all the way.
    monkeypatch.undo()
    engine = init_db(_url(db), backup_dir=backups)
    try:
        assert migrate.LAST_REPORT.changed
        assert _snapshot(db) == before
        assert _notnull(db, "incident_id") == 0
        assert _scalar(db, "SELECT COUNT(*) FROM hitl_tasks WHERE operator_id IS NULL") == 0
        assert _scalar(db, "SELECT MAX(version) FROM schema_version") == SCHEMA_VERSION
    finally:
        engine.dispose()


# ============================================================== 3. the verification really bites


def _after_copy(sabotage: str):
    """``_copy_hitl_rows`` as shipped, then one statement of damage to the NEW table."""
    real = migrate._copy_hitl_rows

    def copy_then_damage(conn, columns):
        sql = real(conn, columns)
        conn.exec_driver_sql(sabotage)
        return sql

    return copy_then_damage


@pytest.mark.parametrize(
    ("sabotage", "message"),
    [
        # a silently TRUNCATED copy: the worst outcome available for an approval trail
        ("DELETE FROM hitl_tasks__v8_rebuild WHERE id = 't-10'", r"copied 8 row\(s\) of 9"),
        # same count, one decision quietly different
        ("UPDATE hitl_tasks__v8_rebuild SET resolved_by = 'Somebody Else' WHERE id = 't-03'", "missing from or altered"),
        # the same bytes in a different storage class (TEXT -> BLOB): the count still matches
        ("UPDATE hitl_tasks__v8_rebuild SET reason = CAST(reason AS BLOB) WHERE id = 't-03'", "missing from or altered"),
        # one row swapped for an invented one: the count still matches
        ("UPDATE hitl_tasks__v8_rebuild SET id = 't-99' WHERE id = 't-10'", "missing from or altered"),
        # the rows shuffled: every value still present, but no longer on the row it belonged to
        (
            "UPDATE hitl_tasks__v8_rebuild SET resolved_by = (SELECT resolved_by FROM hitl_tasks__v8_rebuild AS o "
            "WHERE o.id = 't-03') WHERE id = 't-10'",
            "missing from or altered",
        ),
        # an airtel approval handed to safaricom: every v7 column is intact, only the owner is wrong
        ("UPDATE hitl_tasks__v8_rebuild SET operator_id = 'safaricom' WHERE id = 't-07'", "wrong operator_id"),
    ],
)
def test_the_copy_is_verified_before_anything_is_dropped(tmp_path, monkeypatch, restore_db_globals, sabotage, message):
    db = _build_v7(tmp_path)
    before, table_sql, attached = _snapshot(db), _table_sql(db), _attached(db)

    dropped = []
    real_swap = migrate._swap_hitl_tables
    monkeypatch.setattr(migrate, "_swap_hitl_tables", lambda *a, **k: dropped.append(1) or real_swap(*a, **k))
    monkeypatch.setattr(migrate, "_copy_hitl_rows", _after_copy(sabotage))

    with pytest.raises(HitlRebuildError, match=message):
        init_db(_url(db), backup_dir=tmp_path / "backups")
    models._engine.dispose()

    assert dropped == [], "verification must fail BEFORE the old table is dropped, not be rescued by the rollback"
    _assert_untouched(db, before, table_sql, attached)


# ===================================================================== 4. once, and only once


def test_running_the_migration_twice_is_a_no_op_the_second_time(tmp_path, restore_db_globals):
    db = _build_v7(tmp_path)
    before = _snapshot(db)
    backups = tmp_path / "backups"

    engine = init_db(_url(db), backup_dir=backups)
    assert migrate.LAST_REPORT.changed and len(list(backups.iterdir())) == 1
    rebuilt_sql, rebuilt_attached = _table_sql(db), _attached(db)

    # Direct call: nothing to do, nothing written.
    again = migrate_additive(engine, backup_dir=backups)
    assert again.applied == [] and again.backup_path is None and not again.changed
    assert (again.from_version, again.to_version) == (SCHEMA_VERSION, SCHEMA_VERSION)

    # A whole start-up again: still one backup, one new version row, the table not touched.
    engine.dispose()
    engine = init_db(_url(db), backup_dir=backups)
    try:
        assert migrate.LAST_REPORT.applied == [] and migrate.LAST_REPORT.backup_path is None
        assert len(list(backups.iterdir())) == 1
        assert (_table_sql(db), _attached(db)) == (rebuilt_sql, rebuilt_attached)
        assert _snapshot(db) == before
        assert _sql(db, "SELECT version FROM schema_version ORDER BY version") == [(7,), (SCHEMA_VERSION,)]
    finally:
        engine.dispose()


def test_the_live_catalogue_decides_not_the_version_number(tmp_path, restore_db_globals):
    """A rebuilt file whose version stamp says 7 again is migrating, as far as the version can
    tell. The rebuild must still leave it alone: ``incident_id`` is already nullable, and that
    is the only question that matters. (``test_phase2_schema.py`` depends on the same property:
    it re-stamps a current file as version 2 and requires that no DROP or RENAME runs.)"""
    db = _build_v7(tmp_path)
    before = _snapshot(db)
    init_db(_url(db), backup_dir=tmp_path / "backups").dispose()
    owners = _sql(db, "SELECT id, operator_id FROM hitl_tasks ORDER BY id")
    rebuilt_sql = _table_sql(db)

    con = sqlite3.connect(db)
    con.execute("DELETE FROM schema_version")
    con.execute("INSERT INTO schema_version (version, applied_at) VALUES (7, '2026-09-20 00:00:00')")
    con.commit()
    con.close()

    engine = init_db(_url(db), backup_dir=tmp_path / "backups")
    try:
        report = migrate.LAST_REPORT
        assert (report.from_version, report.to_version) == (7, SCHEMA_VERSION)
        assert not any("hitl_tasks" in s for s in report.applied), report.applied
        assert _table_sql(db) == rebuilt_sql
        assert _snapshot(db) == before
        assert _sql(db, "SELECT id, operator_id FROM hitl_tasks ORDER BY id") == owners
    finally:
        engine.dispose()


def test_a_fresh_database_is_created_with_the_new_shape_and_never_rebuilt(tmp_path, restore_db_globals):
    db = tmp_path / "fresh.db"
    engine = init_db(_url(db), backup_dir=tmp_path / "backups")
    try:
        report = migrate.LAST_REPORT
        assert report.from_version == 0 and report.backup_path is None
        assert not any(s.startswith("DROP") or " RENAME " in s or "__v8_rebuild" in s for s in report.applied)
        assert _notnull(db, "incident_id") == 0 and _notnull(db, "operator_id") == 0
        assert _table_sql(db).startswith("CREATE TABLE hitl_tasks ("), "created, not renamed into place"
    finally:
        engine.dispose()


# ================================================================ 5. what hangs off the table

HAND_MADE_INDEX = "CREATE INDEX ix_hand_made_status ON hitl_tasks (status, created_at)"
TRIGGER = "CREATE TRIGGER trg_hitl_touch AFTER UPDATE ON hitl_tasks BEGIN SELECT 1; END"


def test_indexes_and_triggers_on_the_old_table_are_put_back(tmp_path, restore_db_globals):
    """DROP TABLE takes a table's indexes and triggers with it. The mapped index would come back
    from the ORM anyway; an index somebody added by hand, and a trigger, only come back because
    their SQL is read from sqlite_master before the drop and replayed after the rename."""
    db = _build_v7(tmp_path, extra_sql=(HAND_MADE_INDEX, TRIGGER))
    init_db(_url(db), backup_dir=tmp_path / "backups").dispose()

    attached = {name: sql for _type, name, sql in _attached(db)}
    assert attached["ix_hand_made_status"] == HAND_MADE_INDEX
    assert attached["trg_hitl_touch"] == TRIGGER
    assert attached["ix_hitl_tasks_incident_id"] == V7_INDEX
    assert "ix_hitl_tasks_operator_id" in attached
    # The hand-made index is really usable, not merely listed.
    plan = _sql(db, "EXPLAIN QUERY PLAN SELECT id FROM hitl_tasks WHERE status = 'PENDING' ORDER BY created_at")
    assert any("ix_hand_made_status" in row[-1] for row in plan)


def test_a_view_over_the_table_is_refused_with_the_file_untouched(tmp_path, restore_db_globals):
    """Once the old table is dropped SQLite cannot rename the new one underneath a view that
    mentions it. That would roll back safely but recur on every start; say so up front."""
    view = "CREATE VIEW v_open_cards AS SELECT id, status FROM hitl_tasks WHERE status IN ('PENDING','CLAIMED')"
    db = _build_v7(tmp_path, extra_sql=(view,))
    before, table_sql, attached = _snapshot(db), _table_sql(db), _attached(db)

    with pytest.raises(HitlRebuildError, match=r"view v_open_cards.*DROP VIEW / DROP TRIGGER"):
        init_db(_url(db), backup_dir=tmp_path / "backups")
    models._engine.dispose()
    _assert_untouched(db, before, table_sql, attached)
    assert not (tmp_path / "backups").exists(), "refused before the backup: nothing to pile up"
    assert len(_sql(db, "SELECT * FROM v_open_cards")) == 4  # the view still works: nothing moved

    # Doing what the message says is enough.
    con = sqlite3.connect(db)
    con.execute("DROP VIEW v_open_cards")
    con.commit()
    con.close()
    init_db(_url(db), backup_dir=tmp_path / "backups").dispose()
    assert _snapshot(db) == before and _notnull(db, "incident_id") == 0


OUTBOX_TRIGGER = (
    "CREATE TRIGGER trg_ob AFTER UPDATE ON outbox BEGIN "
    "UPDATE hitl_tasks SET reason = 'outbox row moved' WHERE id = NEW.hitl_task_id; END"
)


def test_a_trigger_on_another_table_that_mentions_hitl_tasks_is_refused_before_the_backup(tmp_path, restore_db_globals):
    """The review's reproduction. A trigger on ``outbox`` whose body mentions ``hitl_tasks`` is
    not attached to the table (``tbl_name`` is outbox), so it survives the DROP -- and then the
    RENAME fails with "error in trigger trg_ob: no such table: main.hitl_tasks". Rolled back,
    no loss, but on EVERY start, with no remedy in the message and a fresh backup per attempt.
    Now: refused up front, named, with the remedy, and nothing written -- three times over."""
    db = _build_v7(tmp_path, extra_sql=(OUTBOX_TRIGGER,))
    before, table_sql, attached = _snapshot(db), _table_sql(db), _attached(db)
    backups = tmp_path / "backups"

    for _attempt in range(3):
        with pytest.raises(HitlRebuildError, match=r"trigger trg_ob.*DROP VIEW / DROP TRIGGER <name>.*no backup was written"):
            init_db(_url(db), backup_dir=backups)
        models._engine.dispose()
    _assert_untouched(db, before, table_sql, attached)
    assert not backups.exists(), "three refused starts, zero backups"
    assert _sql(db, "SELECT name FROM sqlite_master WHERE type='trigger'") == [("trg_ob",)], "nothing was dropped for the caller"

    # The remedy in the message is enough, and the trigger can come back afterwards.
    con = sqlite3.connect(db)
    con.execute("DROP TRIGGER trg_ob")
    con.commit()
    con.close()
    init_db(_url(db), backup_dir=backups).dispose()
    assert _snapshot(db) == before and _notnull(db, "incident_id") == 0
    con = sqlite3.connect(db)
    con.execute(OUTBOX_TRIGGER)
    con.execute("UPDATE outbox SET status = 'SENT' WHERE id = 'ob-1'")  # fires the trigger against the rebuilt table
    con.commit()
    assert con.execute("SELECT reason FROM hitl_tasks WHERE id = 't-05'").fetchone() == ("outbox row moved",)
    con.close()


ARCHIVE_TABLE = "CREATE TABLE hitl_tasks_archive (id TEXT, note TEXT)"
# Every one of these mentions the letters "hitl_tasks" and NONE of them refers to the table, so
# SQLite's RENAME succeeds and refusing any of them would keep a healthy file from starting.
# The first three are the review's reproduction (fp.py / fp3.py); the rest are the other ways
# the substring appears without being the identifier.
NOT_A_REFERENCE = {
    "archive_trigger": (ARCHIVE_TABLE, "CREATE TRIGGER trg_arch AFTER UPDATE ON outbox BEGIN INSERT INTO hitl_tasks_archive (id, note) VALUES (NEW.id, 'moved'); END"),
    "archive_view": (ARCHIVE_TABLE, "CREATE VIEW v_arch AS SELECT id FROM hitl_tasks_archive"),
    "trigger_on_archive": (ARCHIVE_TABLE, "CREATE TABLE audit_x (id TEXT)", "CREATE TRIGGER trg_a2 AFTER INSERT ON hitl_tasks_archive BEGIN INSERT INTO audit_x (id) VALUES (NEW.id); END"),
    "trigger_name_only": (ARCHIVE_TABLE, "CREATE TRIGGER trg_hitl_tasks_mirror AFTER UPDATE ON outbox BEGIN INSERT INTO hitl_tasks_archive (id, note) VALUES (NEW.id, 'mirror'); END"),
    "string_literal": (ARCHIVE_TABLE, "CREATE TRIGGER trg_lit AFTER UPDATE ON outbox BEGIN INSERT INTO hitl_tasks_archive (id, note) VALUES (NEW.id, 'hitl_tasks'); END"),
    "comment_only": (ARCHIVE_TABLE, "CREATE TRIGGER trg_cmt AFTER UPDATE ON outbox BEGIN /* nothing to do with hitl_tasks */ INSERT INTO hitl_tasks_archive (id, note) VALUES (NEW.id, 'c'); END"),
    # an apostrophe in a comment, with NO reference after it: must not be refused either
    "apostrophe_comment_no_ref": (ARCHIVE_TABLE, "CREATE TRIGGER trg_apc AFTER UPDATE ON outbox BEGIN -- the owner's archive\n INSERT INTO hitl_tasks_archive (id, note) VALUES (NEW.id, 'a'); END"),
}
# ...and every one of these DOES refer to the table, however it is spelled, so the RENAME would
# fail ("error in trigger ...: no such table: main.hitl_tasks") and each must still be refused.
A_REFERENCE = {
    "double_quoted": 'CREATE TRIGGER trg_q AFTER UPDATE ON outbox BEGIN UPDATE "hitl_tasks" SET reason = \'q\' WHERE id = NEW.hitl_task_id; END',
    "bracketed": "CREATE TRIGGER trg_b AFTER UPDATE ON outbox BEGIN UPDATE [hitl_tasks] SET reason = 'b' WHERE id = NEW.hitl_task_id; END",
    "backticked": "CREATE TRIGGER trg_t AFTER UPDATE ON outbox BEGIN UPDATE `hitl_tasks` SET reason = 't' WHERE id = NEW.hitl_task_id; END",
    "schema_qualified": "CREATE TRIGGER trg_s AFTER UPDATE ON outbox WHEN EXISTS (SELECT 1 FROM main.hitl_tasks) BEGIN SELECT 1; END",
    "upper_case": "CREATE TRIGGER trg_u AFTER UPDATE ON outbox BEGIN UPDATE HITL_TASKS SET reason = 'u' WHERE id = NEW.hitl_task_id; END",
    "view": "CREATE VIEW v_cards AS SELECT id FROM hitl_tasks WHERE status = 'PENDING'",
    # The seven spellings the round-3 regex tokenizer let through (round-4 review, matcher_probe.py):
    # an apostrophe in a comment or a quoted alias opened a fake string literal that swallowed the
    # reference; '--' inside a quoted alias looked like a comment; and SQLite accepts a
    # single-quoted string where a table name goes. Decided by SQLite itself now.
    "sq_ident_insert": "CREATE TRIGGER t_sqi AFTER UPDATE ON incidents BEGIN INSERT INTO 'hitl_tasks' (id, incident_id, task_type, proposed_payload_json, status, created_at) VALUES (NEW.id, NEW.id, 'G', '{}', 'PENDING', '2026-01-01'); END",
    "sq_ident_from_view": "CREATE VIEW v_sqf AS SELECT id FROM 'hitl_tasks'",
    "apos_line_comment": "CREATE TRIGGER t_alc AFTER UPDATE ON incidents BEGIN -- don't touch\n UPDATE hitl_tasks SET reason = 'x' WHERE incident_id = NEW.id; END",
    "apos_block_comment": "CREATE TRIGGER t_abc AFTER UPDATE ON incidents BEGIN /* it's here */ UPDATE hitl_tasks SET reason = 'x' WHERE incident_id = NEW.id; END",
    "apos_dq_alias": 'CREATE VIEW v_adq AS SELECT id AS "o\'k" FROM hitl_tasks WHERE status = \'PENDING\'',
    "apos_bracket_alias": "CREATE VIEW v_abr AS SELECT id AS [o'k] FROM hitl_tasks WHERE status = 'PENDING'",
    "dashdash_dq_alias": 'CREATE VIEW v_ddq AS SELECT id AS "queue -- owner" FROM hitl_tasks',
    # ...and the controls the same probe used
    "view_comment_owner_s": "CREATE VIEW v_rep AS\n-- the owner's queue\nSELECT id, status FROM hitl_tasks",
    "nl_tokens": "CREATE TRIGGER t_nl AFTER UPDATE ON incidents BEGIN UPDATE\nhitl_tasks\nSET reason = 'x' WHERE incident_id = NEW.id; END",
    "dq_schema_oddcase": 'CREATE VIEW v_dqs AS SELECT id FROM "main"."HiTl_TaSkS"',
}


@pytest.mark.parametrize("case", sorted(NOT_A_REFERENCE))
def test_a_trigger_or_view_that_only_resembles_a_reference_is_not_refused(tmp_path, restore_db_globals, case):
    """The review's reproduction: a v7 file whose trigger writes to ``hitl_tasks_archive`` was
    refused on every start by a substring match, although the rebuild would have succeeded.
    The identifier is now matched as a whole token, outside string literals and comments."""
    db = _build_v7(tmp_path, extra_sql=NOT_A_REFERENCE[case])
    before = _snapshot(db)

    init_db(_url(db), backup_dir=tmp_path / "backups").dispose()

    assert _snapshot(db) == before and _notnull(db, "incident_id") == 0
    assert _scalar(db, "PRAGMA integrity_check") == "ok"
    assert _scalar(db, "SELECT MAX(version) FROM schema_version") == SCHEMA_VERSION
    # Whatever was defined still exists and still works against the rebuilt schema.
    con = sqlite3.connect(db)
    try:
        con.execute("UPDATE outbox SET status = 'SENT' WHERE id = 'ob-1'")
        con.execute("INSERT INTO hitl_tasks_archive (id, note) VALUES ('a1', 'n')")
        con.commit()
        if case == "archive_view":
            assert con.execute("SELECT COUNT(*) FROM v_arch").fetchone()[0] == 1
        elif case == "trigger_on_archive":
            assert con.execute("SELECT COUNT(*) FROM audit_x").fetchone()[0] == 1
        else:
            assert con.execute("SELECT COUNT(*) FROM hitl_tasks_archive").fetchone()[0] == 2, "the trigger fired"
    finally:
        con.close()


@pytest.mark.parametrize("case", sorted(A_REFERENCE))
def test_a_real_reference_in_any_spelling_is_still_refused_before_the_backup(tmp_path, restore_db_globals, case):
    db = _build_v7(tmp_path, extra_sql=(A_REFERENCE[case],))
    before, table_sql, attached = _snapshot(db), _table_sql(db), _attached(db)

    with pytest.raises(HitlRebuildError, match=r"cannot re-parse.*no such table: main\.[Hh][Ii][Tt][Ll]_[Tt][Aa][Ss][Kk][Ss].*no backup was written"):
        init_db(_url(db), backup_dir=tmp_path / "backups")
    models._engine.dispose()
    _assert_untouched(db, before, table_sql, attached)
    assert not (tmp_path / "backups").exists()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# Names SQLite reports as 'error in view <name>: <detail>' -- with ': ' INSIDE the name. Round 4
# split the message at its first ': ', tried to DROP VIEW "odd", and startup failed with
# 'no such view: odd' instead of the refusal. The name is now resolved against sqlite_master.
ODD_NAMES = {
    "view_colon_space": ('CREATE VIEW "odd: name" AS SELECT id FROM hitl_tasks',),
    "trigger_colon_space": ('CREATE TRIGGER "t: x" AFTER INSERT ON incidents BEGIN UPDATE hitl_tasks SET status = status WHERE 0; END',),
    "two_blockers_one_odd": ("CREATE VIEW v_plain AS SELECT id FROM hitl_tasks", 'CREATE VIEW "a: b: c" AS SELECT id FROM hitl_tasks'),
    "prefix_of_another": ('CREATE VIEW "a" AS SELECT id FROM hitl_tasks', 'CREATE VIEW "a: longer" AS SELECT id FROM hitl_tasks'),
}
ODD_EXPECTED = {
    "view_colon_space": ["view odd: name"],
    "trigger_colon_space": ["trigger t: x"],
    "two_blockers_one_odd": ["view a: b: c", "view v_plain"],
    "prefix_of_another": ["view a", "view a: longer"],
}


@pytest.mark.parametrize("case", sorted(ODD_NAMES))
def test_a_blocker_whose_name_contains_colon_space_is_named_correctly(tmp_path, restore_db_globals, case):
    db = _build_v7(tmp_path, extra_sql=ODD_NAMES[case])
    before, table_sql, attached = _snapshot(db), _table_sql(db), _attached(db)

    with pytest.raises(HitlRebuildError) as refused:
        init_db(_url(db), backup_dir=tmp_path / "backups")
    models._engine.dispose()
    message = str(refused.value)
    for named in ODD_EXPECTED[case]:
        assert named in message, message
    assert "DROP VIEW / DROP TRIGGER <name>" in message and "no backup was written" in message
    _assert_untouched(db, before, table_sql, attached)
    assert not (tmp_path / "backups").exists()
    assert _sql(db, "SELECT COUNT(*) FROM sqlite_master WHERE type IN ('view','trigger')") == [(len(ODD_NAMES[case]),)]


def test_an_unrelated_broken_view_is_refused_in_words_that_are_true_for_it(tmp_path, restore_db_globals):
    """The RENAME re-parses every view and trigger in the schema, so a view over a table dropped
    long ago blocks the rebuild although it never mentions hitl_tasks. Round 4 called that a
    'reference to hitl_tasks'. The refusal now says what SQLite said, and offers both causes."""
    db = _build_v7(tmp_path, extra_sql=(
        "CREATE TABLE gone (id TEXT)", "CREATE VIEW v_orphan AS SELECT id FROM gone", "DROP TABLE gone",
    ))
    before, table_sql, attached = _snapshot(db), _table_sql(db), _attached(db)

    with pytest.raises(HitlRebuildError) as refused:
        init_db(_url(db), backup_dir=tmp_path / "backups")
    models._engine.dispose()
    message = str(refused.value)
    assert message.startswith("view v_orphan: SQLite cannot re-parse it once the rebuilt hitl_tasks is renamed into place")
    assert "broken in its own right" in message
    assert "view v_orphan: no such table: main.gone" in message, "SQLite's own reason is quoted, not a guess"
    assert "reference(s) to hitl_tasks" not in message
    _assert_untouched(db, before, table_sql, attached)
    assert not (tmp_path / "backups").exists()

    # Following the message is enough.
    con = sqlite3.connect(db)
    con.execute("DROP VIEW v_orphan")
    con.commit()
    con.close()
    init_db(_url(db), backup_dir=tmp_path / "backups").dispose()
    assert _snapshot(db) == before and _notnull(db, "incident_id") == 0


@pytest.mark.parametrize("journal", ["wal", "delete"])
@pytest.mark.parametrize("blocked", [True, False], ids=["blocked", "clear"])
def test_the_rename_probe_leaves_the_file_byte_identical(tmp_path, restore_db_globals, journal, blocked):
    """The decision "does anything refer to hitl_tasks" is made by performing the rebuild's own
    CREATE / DROP / RENAME inside a transaction that is rolled back. That must cost the file
    nothing: same bytes afterwards, in both journal modes, whether or not SQLite objected."""
    extra = (A_REFERENCE["apos_dq_alias"], A_REFERENCE["sq_ident_insert"]) if blocked else NOT_A_REFERENCE["archive_trigger"]
    db = _build_v7(tmp_path, extra_sql=extra)
    con = sqlite3.connect(db)
    con.execute(f"PRAGMA journal_mode={journal}")
    con.close()
    assert _scalar(db, "PRAGMA journal_mode") == journal
    before_bytes, before_rows, before_attached = _sha(db), _snapshot(db), _attached(db)

    import noc_agents.db.models_all  # noqa: F401

    engine = create_engine(_url(db), future=True, connect_args={"check_same_thread": False, "timeout": 30})
    try:
        with engine.connect() as conn:
            blockers = migrate._rename_blockers(conn, engine)
            assert not conn.connection.driver_connection.in_transaction, "the probe must not leave a transaction open"
    finally:
        engine.dispose()

    assert [(kind, name) for kind, name, _said in blockers] == ([("view", "v_adq"), ("trigger", "t_sqi")] if blocked else [])
    assert all(said == "no such table: main.hitl_tasks" for _k, _n, said in blockers)
    assert _sha(db) == before_bytes, "the probe changed the database file"
    assert (_snapshot(db), _attached(db)) == (before_rows, before_attached)
    assert _notnull(db, "incident_id") == 1 and _scalar(db, "PRAGMA integrity_check") == "ok"
    assert _sql(db, "SELECT name FROM sqlite_master WHERE name LIKE '%rebuild%'") == []


def test_every_blocker_is_named_not_only_the_first(tmp_path, restore_db_globals):
    """SQLite reports one offender per failed RENAME. Inside the doomed probe transaction each one
    is dropped and the RENAME retried, so the operator gets the whole list in one refusal and
    fixes the file in one go instead of once per start."""
    db = _build_v7(tmp_path, extra_sql=(A_REFERENCE["apos_line_comment"], A_REFERENCE["view"], A_REFERENCE["dashdash_dq_alias"]))
    before, table_sql, attached = _snapshot(db), _table_sql(db), _attached(db)

    with pytest.raises(HitlRebuildError) as refused:
        init_db(_url(db), backup_dir=tmp_path / "backups")
    models._engine.dispose()
    message = str(refused.value)
    assert "trigger t_alc" in message and "view v_cards" in message and "view v_ddq" in message
    assert "no backup was written" in message and not (tmp_path / "backups").exists()
    _assert_untouched(db, before, table_sql, attached)
    assert _sql(db, "SELECT COUNT(*) FROM sqlite_master WHERE type IN ('view','trigger')") == [(3,)], "nothing was dropped for the caller"

    # Following the message for all three at once is enough.
    con = sqlite3.connect(db)
    for stmt in ("DROP TRIGGER t_alc", "DROP VIEW v_cards", "DROP VIEW v_ddq"):
        con.execute(stmt)
    con.commit()
    con.close()
    init_db(_url(db), backup_dir=tmp_path / "backups").dispose()
    assert _snapshot(db) == before and _notnull(db, "incident_id") == 0


ON_THE_TABLE_IN_ANOTHER_CASE = {
    "UPPER": "CREATE TRIGGER trg_uc AFTER INSERT ON HITL_TASKS BEGIN INSERT INTO hitl_tasks_archive (id, note) VALUES (NEW.id, 'uc'); END",
    "quoted_mixed": 'CREATE TRIGGER trg_qm AFTER INSERT ON "Hitl_Tasks" BEGIN INSERT INTO hitl_tasks_archive (id, note) VALUES (NEW.id, \'qm\'); END',
    "bracket_lower": "CREATE TRIGGER trg_br AFTER INSERT ON [hitl_tasks] BEGIN INSERT INTO hitl_tasks_archive (id, note) VALUES (NEW.id, 'br'); END",
}


@pytest.mark.parametrize("case", sorted(ON_THE_TABLE_IN_ANOTHER_CASE))
def test_a_trigger_declared_on_the_table_in_another_case_is_replayed_after_the_rebuild(tmp_path, restore_db_globals, case):
    """SQLite stores a trigger's tbl_name as the CREATE spelled it ('HITL_TASKS') but drops it
    with the table regardless of case. A case-sensitive capture missed it, DROP took it, and the
    rebuild reported success with the trigger gone. Captured case-insensitively now, replayed,
    and the post-check compares by name against everything that was attached before the drop."""
    db = _build_v7(tmp_path, extra_sql=(ARCHIVE_TABLE, ON_THE_TABLE_IN_ANOTHER_CASE[case]))
    name = ON_THE_TABLE_IN_ANOTHER_CASE[case].split()[2]
    note = re.search(r"VALUES \(NEW\.id, '(\w+)'\)", ON_THE_TABLE_IN_ANOTHER_CASE[case]).group(1)
    assert _sql(db, "SELECT name FROM sqlite_master WHERE type='trigger'") == [(name,)]

    init_db(_url(db), backup_dir=tmp_path / "backups").dispose()

    assert _notnull(db, "incident_id") == 0
    assert _sql(db, "SELECT name FROM sqlite_master WHERE type='trigger'") == [(name,)], "the trigger survived the rebuild"
    con = sqlite3.connect(db)
    try:
        con.execute(
            "INSERT INTO hitl_tasks (id, operator_id, task_type, proposed_payload_json, status, created_at) "
            "VALUES ('t-new', 'safaricom', 'GENERIC', '{}', 'PENDING', '2026-09-22 01:00:00')"
        )
        assert con.execute("SELECT note FROM hitl_tasks_archive WHERE id = 't-new'").fetchall() == [(note,)], "and it still fires"
        con.rollback()
    finally:
        con.close()


def test_a_null_the_model_forbids_is_refused_before_the_backup_with_the_fix_spelled_out(tmp_path, restore_db_globals):
    """A file that grew from v1 has ``entity_type``/``edited`` nullable on disk (ADD COLUMN is
    rendered without NOT NULL); the rebuilt table takes the model's NOT NULL. A NULL there --
    only raw SQL can put one there -- would fail the copy on every start. The decision is to
    REFUSE and name the one UPDATE that fixes it, not to COALESCE a value in during the copy:
    the migration must not rewrite a cell of the approval trail on its own authority."""
    db = _build_v7(tmp_path, "grown-from-v1", extra_sql=(
        "UPDATE hitl_tasks SET entity_type = NULL WHERE id IN ('t-05', 't-06')",
        "UPDATE hitl_tasks SET edited = NULL WHERE id = 't-09'",
    ))
    before, table_sql, attached = _snapshot(db), _table_sql(db), _attached(db)
    backups = tmp_path / "backups"

    with pytest.raises(HitlRebuildError) as refused:
        init_db(_url(db), backup_dir=backups)
    models._engine.dispose()
    message = str(refused.value)
    assert "entity_type: 2 NULL row(s)" in message and "edited: 1 NULL row(s)" in message
    assert "no backup was written" in message and not backups.exists()
    _assert_untouched(db, before, table_sql, attached)

    # The statements in the message ARE the remedy: run them verbatim, nothing else.
    fixes = re.findall(r"UPDATE hitl_tasks SET .*? IS NULL", message)
    assert len(fixes) == 2, message
    con = sqlite3.connect(db)
    for fix in fixes:
        con.execute(fix)
    con.commit()
    con.close()
    init_db(_url(db), backup_dir=backups).dispose()
    assert _notnull(db, "incident_id") == 0
    assert _sql(db, "SELECT id, entity_type, edited FROM hitl_tasks WHERE id IN ('t-05','t-06','t-09') ORDER BY id") == [
        ("t-05", "incident", 0), ("t-06", "incident", 0), ("t-09", "incident", 0),
    ]
    # ...and every other row is byte for byte what it was.
    touched = {"t-05", "t-06", "t-09"}
    assert [r for r in _snapshot(db) if r[1] not in touched] == [r for r in before if r[1] not in touched]


def test_a_column_the_model_does_not_know_stops_the_rebuild(tmp_path, restore_db_globals):
    """A rebuild copies the columns it knows about. A column it does not know would be dropped
    without a word — from the approval trail. Refuse instead; a human decides."""
    db = _build_v7(tmp_path, extra_sql=(
        "ALTER TABLE hitl_tasks ADD COLUMN legal_hold_ref TEXT",
        "UPDATE hitl_tasks SET legal_hold_ref = 'LH-2026-014' WHERE id = 't-04'",
    ))
    with pytest.raises(HitlRebuildError, match=r"does not know: \['legal_hold_ref'\].*no backup was written"):
        init_db(_url(db), backup_dir=tmp_path / "backups")
    models._engine.dispose()
    assert not (tmp_path / "backups").exists()
    assert _notnull(db, "incident_id") == 1
    assert _sql(db, "SELECT legal_hold_ref FROM hitl_tasks WHERE id = 't-04'") == [("LH-2026-014",)]
    assert _scalar(db, "SELECT MAX(version) FROM schema_version") == 7


# ========================================================= 6. a task whose incident is missing

ORPHAN = ("t-orphan", "inc-deleted-long-ago", "APPROVE_BROADCAST", '{"priority": "P1"}', "APPROVED",
          T.format(d=6, h=1), "Grace Wanjiru", T.format(d=6, h=2), None, None, "sent", None, "incident", None, None, 0)


def _insert_orphan() -> tuple[str, ...]:
    values = ", ".join("NULL" if v is None else (str(v) if isinstance(v, int) else "'" + v.replace("'", "''") + "'") for v in ORPHAN)
    return (f"INSERT INTO hitl_tasks ({', '.join(V7_COLUMNS)}) VALUES ({values})",)


def test_a_task_whose_incident_is_missing_is_kept_unowned_and_invisible(tmp_path, monkeypatch, caplog, restore_db_globals):
    """It should not happen — but SQLite foreign keys are not enforced in this codebase, so it
    can. The decision (db/migrate.py docstring): keep the row exactly as it is, leave its owner
    NULL, say so in the log. Under the v7 join that row was already invisible to everyone, so
    nothing is lost; inventing an owner would be a cross-tenant leak, and refusing to start a
    NOC over one orphan would be disproportionate."""
    db = _build_v7(tmp_path, extra_sql=_insert_orphan())
    before = _snapshot(db)

    with caplog.at_level(logging.WARNING, logger="noc_agents.db.migrate"):
        engine = init_db(_url(db), backup_dir=tmp_path / "backups")
    try:
        assert _snapshot(db) == before, "the orphan is part of the approval trail too: copied, not dropped"
        assert _sql(db, "SELECT id FROM hitl_tasks WHERE operator_id IS NULL") == [("t-orphan",)]
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("t-orphan" in m and "1 task(s)" in m for m in warnings), warnings

        for operator in ("safaricom", "airtel"):
            monkeypatch.setenv("OPERATOR_PROFILE", operator)
            clear_settings_cache()
            session = get_session()
            try:
                visible = {t.id for t in session.scalars(_owned(HitlTaskRow))}
                assert visible and "t-orphan" not in visible
                # ...while the row itself is still there for whoever goes looking by id.
                assert session.scalar(select(HitlTaskRow.resolved_by).where(HitlTaskRow.id == "t-orphan")) == "Grace Wanjiru"
            finally:
                session.close()
    finally:
        clear_settings_cache()
        engine.dispose()


# ============================================== 7. a connection that DOES enforce foreign keys


def test_a_connection_enforcing_foreign_keys_still_migrates_and_gets_its_pragma_back(tmp_path, restore_db_globals):
    """Nothing in this codebase turns ``PRAGMA foreign_keys`` on. If a connection hook ever does,
    the rebuild must not depend on it being off: with enforcement on, copying the orphan below
    is a FOREIGN KEY failure, so the migration would refuse to start on a file the app has been
    running on for months. SQLite ignores the pragma inside a transaction, which is why
    migrate_additive switches it off BEFORE ``BEGIN`` and puts it back afterwards."""
    db = _build_v7(tmp_path, extra_sql=_insert_orphan())
    before = _snapshot(db)

    import noc_agents.db.models_all  # noqa: F401  init_db normally does this

    engine = create_engine(_url(db), future=True, connect_args={"check_same_thread": False, "timeout": 30})
    connections = []

    @event.listens_for(engine, "connect")
    def _enforce(dbapi_connection, _record):
        connections.append(dbapi_connection)
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    try:
        report = migrate_additive(engine, backup_dir=tmp_path / "backups")
        assert report.changed and _snapshot(db) == before and _notnull(db, "incident_id") == 0
        # The pooled connection the migration used is enforcing again, exactly as it was handed over.
        assert connections, "the hook never ran"
        assert [c.execute("PRAGMA foreign_keys").fetchone()[0] for c in connections] == [1] * len(connections)
    finally:
        engine.dispose()


def test_the_pragma_comes_back_even_when_the_write_lock_cannot_be_taken(tmp_path, restore_db_globals):
    """``PRAGMA foreign_keys`` is switched off before ``BEGIN IMMEDIATE`` (SQLite ignores it inside
    a transaction). If the BEGIN itself fails -- another writer holds the lock -- the connection
    still goes back to the pool, and it must go back enforcing. So the BEGIN is inside the
    ``try`` whose ``finally`` restores the pragma."""
    db = _build_v7(tmp_path)
    before = _snapshot(db)

    import noc_agents.db.models_all  # noqa: F401

    engine = create_engine(_url(db), future=True, connect_args={"check_same_thread": False, "timeout": 0.2})
    connections = []

    @event.listens_for(engine, "connect")
    def _enforce(dbapi_connection, _record):
        connections.append(dbapi_connection)
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    other_writer = sqlite3.connect(db, isolation_level=None)
    other_writer.execute("BEGIN IMMEDIATE")  # holds the write lock for the whole attempt
    try:
        with pytest.raises(OperationalError, match="locked"):
            migrate_additive(engine, backup_dir=tmp_path / "backups")
        assert connections, "the hook never ran"
        assert [c.execute("PRAGMA foreign_keys").fetchone()[0] for c in connections] == [1] * len(connections), (
            "a connection went back to the pool with foreign keys OFF"
        )
        assert _snapshot(db) == before and _notnull(db, "incident_id") == 1  # nothing happened to the file
    finally:
        other_writer.execute("ROLLBACK")
        other_writer.close()

    # Lock released: the same engine gets all the way through, pragma still on afterwards.
    try:
        report = migrate_additive(engine, backup_dir=tmp_path / "backups")
        assert report.changed and _snapshot(db) == before and _notnull(db, "incident_id") == 0
        assert [c.execute("PRAGMA foreign_keys").fetchone()[0] for c in connections] == [1] * len(connections)
    finally:
        engine.dispose()
