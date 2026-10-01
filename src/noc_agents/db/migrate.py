"""Generic additive schema migration for the SQLite database (spec §7.0.1) -- and two
named exceptions to "additive": the schema_version 8 rebuild of ``hitl_tasks``, and the
schema_version 9 refresh of CHECK constraints on EMPTY scorecard tables.

The ORM classes in ``db/models.py`` are the single source of truth for the schema.
This module brings a database file up to that shape using only three statements:
``CREATE TABLE IF NOT EXISTS``, ``CREATE INDEX IF NOT EXISTS`` and
``ALTER TABLE ... ADD COLUMN``. With the one exception described below it never drops,
renames or retypes anything, which is what makes the rollback story cheap: an older code
version keeps running against a newer file (extra tables and columns are ignored), so
rolling back a release is a code checkout, and the pre-migration backup is only for the
day the *data* is damaged.

THE ONE EXCEPTION: ``_rebuild_hitl_tasks`` (schema_version 8)
------------------------------------------------------------
This docstring used to say "never". It cannot any more, and the reason is worth keeping.

``hitl_tasks.incident_id`` was NOT NULL, and a task's owning operator was derived by joining
``incidents``. Phase 5 introduced approval cards that are not about an incident at all (a
maintenance programme, a night's planned outage), and the lane had to file each one against
an unrelated "anchor" incident to satisfy the constraint -- and could raise no card at all on
a database with no incidents (``docs/PHASE5.md``, "The one that should be fixed first"). The
fix is a nullable ``incident_id`` plus the table's own ``operator_id``. The column is
additive. The constraint is not: **SQLite cannot drop NOT NULL in place**, so the table has
to be created again, copied, and swapped.

It is built as an exception, not as a facility. There is no rebuild framework here and the
next table that "needs" one should be argued for from scratch. What keeps this one safe:

* **Same backup, same transaction.** The pre-migration copy is taken before the first
  statement, exactly as for an additive release. The rebuild runs inside the same
  ``BEGIN IMMEDIATE`` as everything else, AFTER the additive pass (so old and new table have
  the same columns and the copy is column-for-column). SQLite DDL is transactional --
  CREATE, DROP and RENAME included -- so a failure at any point, even after the old table
  has been dropped, rolls the file back to exactly what it was and the next start retries.
* **Verify before drop.** Row count AND every column of every row (value and storage class,
  by ``rowid``) are compared between old and new before the old table is dropped; any
  difference raises and the transaction rolls back. This table is the approval trail for
  customer broadcasts and regulator notices: a silently truncated copy is the worst outcome
  available, so it is the one the code spends statements on.
* **Detected from the live catalogue**, not from the version number: the rebuild runs only
  when ``PRAGMA table_info(hitl_tasks)`` still reports ``incident_id`` as NOT NULL. A fresh
  database is created with the new shape and never rebuilt; a rebuilt file is never rebuilt
  again; and it is only ever *looked for* on a start that is migrating anyway -- the
  every-start fast path is untouched.
* **Backfill during the copy.** ``operator_id`` comes from the row's incident. A task whose
  incident row is missing (foreign keys are not enforced in this codebase, so it is possible)
  is copied as it is with ``operator_id`` NULL and logged by id. Under the old join that row
  was already invisible to every operator; it stays invisible and nothing is lost. Guessing
  an owner would be a cross-tenant leak, and refusing to start a NOC over an orphan would be
  disproportionate.
* **What hangs off the table.** Indexes and triggers are dropped with a table, so their SQL
  is read from ``sqlite_master`` first and replayed afterwards. Nothing declares a foreign
  key to ``hitl_tasks`` (``outbox.hitl_task_id`` and its siblings are plain text ids, and
  every id is preserved); and because the OLD table is dropped and the NEW one renamed onto
  its name -- never the old one renamed away -- a reference to ``hitl_tasks`` elsewhere in the
  schema is never rewritten. ``PRAGMA foreign_keys`` is off everywhere in this codebase; if a
  connection ever has it on, it is switched off for the migration and restored afterwards, as
  SQLite's own rebuild procedure requires.
* **Refuse up front, before the backup.** Two things make the rebuild fail on EVERY start --
  rolled back each time, so no data is lost, but the NOC never comes up: a view, or a trigger
  on some OTHER table, that refers to ``hitl_tasks`` (SQLite cannot RENAME underneath it:
  "error in trigger ...: no such table"); and a NULL in a column the model declares NOT NULL
  (``entity_type``, ``edited``), which a file that grew from v1 by ADD COLUMN permits on disk
  and which only raw SQL could have written. Both are checked by ``_hitl_rebuild_preflight``
  before the backup is taken, so a refused start does not pile up backups, and the message
  names each object or column and the one statement that fixes it. A NULL is refused rather
  than rewritten during the copy: a migration that quietly changes a value on the approval
  trail is the very thing the verification step exists to prevent.
* **SQLite decides what refers to the table, not a tokenizer.** Two hand-written matchers
  were tried for the first check and both were wrong in opposite directions: a substring
  match refused files whose triggers only touched ``hitl_tasks_archive``; a regex tokenizer
  let seven real references through (an apostrophe in a comment or a quoted alias, ``--``
  inside a quoted alias, ``FROM 'hitl_tasks'`` -- SQLite accepts a string where a table name
  goes). ``_rename_blockers`` now asks the only authority there is: inside a transaction that
  is always rolled back it performs the rebuild's own CREATE, DROP and RENAME, and each
  failure names one blocking view or trigger, which is dropped (inside that same doomed
  transaction) so the next failure names the next one. Whatever SQLite's parser would trip
  over at the real RENAME -- including under a ``legacy_alter_table`` setting, since the
  probe runs on the same connection state -- it trips over here first, and nothing else does.
  A ``BEGIN IMMEDIATE ... ROLLBACK`` probe leaves the file byte for byte as it was (pinned by
  test, in WAL and rollback-journal modes); inside the migration transaction the same probe
  runs in a SAVEPOINT that is rolled back to.

THE SECOND EXCEPTION: ``_refresh_check_constraints`` (schema_version 9)
----------------------------------------------------------------------
Narrower than the first, and destructive only where there is nothing to destroy. A CHECK
constraint is the one part of a table's definition the additive path can never deliver to
an existing file: ``ADD COLUMN`` cannot add one, ``CREATE TABLE IF NOT EXISTS`` is a no-op
on a table that exists, and ``create_all`` never alters. So when the scorecard lane
tightened ``ck_vendor_scorecards_shadow`` (reviewer must be a human name, not whitespace or
"system") and ``ck_vendor_scorecards_dq_counts`` (``dq_gate_threshold_pct < 100``) AFTER
files had been created at v8, those files kept the old CHECKs and every test passed, because
tests build fresh databases. The dev database was exactly such a file, with zero rows.

The rule, for the two tables named in ``_CHECK_REFRESH_TABLES`` and no others: compare the
live ``CREATE TABLE`` text in ``sqlite_master`` with what the CURRENT model compiles to (the
DDL, never a version number, so it is idempotent and a fresh file is never touched). Where
they differ and the table is EMPTY, drop it and create it from the model, inside one
transaction, verified before the drop (row count is zero, re-read under the write lock) and
after the create (live DDL now equals the model's; every index and trigger that hung off the
table before the drop is there again, by name). Where they differ and the table has ROWS,
do nothing to it: log a warning naming the CHECKs the file is not enforcing -- on every start,
because a warning that stops is a warning that gets lost -- and carry the same text in
``MigrationReport.note``. A populated table is never rebuilt here; the ORM mapper guards in
``db/models_scorecards.py`` still refuse every ORM write, and adopting the constraints is a
human's decision: export the rows, empty the table, restart.

ON EVERY START, not only when the version moves
-----------------------------------------------
The comparison above runs on every start, and so does the check for a mapped index that is
missing from the file. Both used to run only inside the version bump -- and that broke the
remedy just described: after the first v9 start had stamped the file, "empty the table and
restart" took the "schema already current" path and the old CHECKs stayed for good. So the
fast path is no longer purely a version read: a start on a current file runs about 45
catalogue statements (``schema_version``, ``sqlite_master`` for the two tables, and one
``PRAGMA index_list`` per mapped table), measured at 11-34 ms against a 4-7 s import. When
it finds nothing (the common case) it is otherwise what it was: no backup, no transaction, no
write lock. When it finds a drifted EMPTY table it takes a backup first (named
``<db>.9-to-9.<ts>.db``: same version, because the version did not move) and recreates the
table in a transaction. When it finds a missing index it creates it -- ``CREATE INDEX IF NOT
EXISTS`` -- without a backup, an index holding no data; but ONLY while the table is small,
see the next section. The bump to 9 itself stays: it records that the refresh exists and puts
one backup behind the first start that could act.

Only a file AT this code's version gets any of that. A file NEWER than this code (a rolled-back
release) is left exactly as the newer release wrote it: the comparison cannot tell a newer
definition from a drifted one, and "drifted and empty" would otherwise mean "drop the newer
table and recreate the older one" -- which a replay caught it doing, silently deleting a
column the newer release had added. Newer means: warn, and touch nothing.

INDEX BUILDS AND THE WRITE LOCK
-------------------------------
``CREATE INDEX`` on a large table holds the write lock for the whole build: two million
``audit_events`` rows (a keep-forever table) took 75-150 s on the reference machine. Every
other process writing to the file fails after its 30 s busy timeout, and a second worker
starting at the same moment crashes instead of "waiting, then finding nothing to do". So a
missing index is built at startup only while its table is small --
``INDEX_BUILD_AT_STARTUP_MAX_ROWS`` rows, estimated from ``max(rowid)`` in O(log n) -- and
otherwise logged, every start, with the command that builds it when the NOC is quiet::

    python -m noc_agents.db.migrate --build-indexes

That command (``build_indexes`` / ``main`` at the bottom of this module) lists what is
missing with a row estimate, builds one index per transaction, reports each duration, takes a
backup first only when asked (``--backup``; an index can always be dropped again) and is safe
to run twice. Until it has run, everything works: an index makes a query faster, never
possible. The same threshold applies inside a version move, for the same reason.

**What the exception costs the rollback story** -- worked out against the v7 code, not assumed:

* An older release still *starts* on a rebuilt file (it logs "newer than this code ...
  carrying on") and still reads and decides every incident task. The rebuilt table has every
  column it had, under the same names, types, ids and rowids; an older ORM ignores the extra
  column and never notices the relaxed constraint.
* It still *writes* incident tasks, because ``operator_id`` is deliberately nullable in the
  DDL -- NOT NULL would have turned every HITL task an older release raises into an
  IntegrityError on the golden path. But it writes them with ``operator_id`` NULL. While the
  older code runs nobody notices (it derives ownership through the join). After rolling
  FORWARD again those rows are unowned, and v8 hides an unowned row from every inbox. So
  going forward after a rollback needs this first (idempotent; orphans stay NULL)::

      UPDATE hitl_tasks
         SET operator_id = (SELECT operator_id FROM incidents WHERE incidents.id = hitl_tasks.incident_id)
       WHERE operator_id IS NULL AND incident_id IS NOT NULL;

  The migration will not do it for you: the file is already at version 8, and the fast path
  deliberately does nothing.
* Tasks with NO incident -- the cards v8 exists to allow -- are invisible to an older
  release, whose ownership join is an INNER JOIN on ``incident_id``. That fails closed (the
  card cannot be seen or approved, so the window it gates stays PROPOSED), but it is lost
  function, and it is the part of a rollback that is no longer "just a code checkout".
* Restoring the *file* is unchanged: the backup is the file exactly as the previous release
  left it, old table included.

Bump ``SCHEMA_VERSION`` once per release that adds tables or columns. The version on
disk lives in the ``schema_version`` table (one row per applied version); a file
without that table is version 1 (every database created before Phase 1); an empty
file is version 0 and is simply created in full, with no backup.

Two SQLite facts shape the code (and see "INDEX BUILDS AND THE WRITE LOCK" for a third):

* pysqlite opens a transaction implicitly only before INSERT/UPDATE/DELETE, never
  before DDL. To make "everything or nothing" true we issue ``BEGIN IMMEDIATE``
  ourselves; SQLite's DDL is transactional, so a failure half-way rolls the file
  back to exactly what it was and the next start retries.
* ``ALTER TABLE ADD COLUMN`` is rendered *without* ``NOT NULL`` (SQLite refuses a
  NOT NULL column that has no default, and the ORM always supplies a value on
  insert anyway); the column's ``server_default`` becomes the ``DEFAULT`` literal,
  which SQLite also returns for the rows that already exist. This is the same DDL
  the retired ``_migrate_sqlite`` produced for the old ``_EXTRA_INCIDENT_COLS``.
"""

from __future__ import annotations

import argparse
import logging
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import Column, Table
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.schema import CreateIndex, CreateTable

from noc_agents.db.models import Base, HitlTaskRow, IncidentRow, SchemaVersionRow, utcnow

# 1 = pre-Phase-1 (no schema_version table); 2 = Phase 1 platform tables (outbox, scheduler,
# llm_calls, incident restore provenance); 3 = Phase 2 message tables (message_templates,
# delivery_receipts) and hitl_tasks.{run_id, entity_type, entity_id, created_by, edited};
# 4 = Phase 3 early-warning cache (external_signals, §7.3.1);
# 5 = Phase 4 accountability and learning: vendors + incident_clock_events (§7.6.1),
#     post_incident_reviews + pir_action_items (§7.7.1), regulatory_notifications +
#     evidence_packs (§7.6.1), and the known-error columns on problems (§7.7.1).
# 6 = Phase 5 scheduling and knowledge: maintenance_plans/tasks/windows (§7.5),
#     contracts + contract_clauses + contract_faq + contract_queries (§7.8),
#     relationship_complaints + subject_persons (§7.8).
# 7 = Phase 5 capacity lane: capacity_observations, capacity_advisories (§7.5.1).
# 8 = HITL tasks own themselves: hitl_tasks.operator_id (additive) and a NULLABLE
#     hitl_tasks.incident_id -- the one NON-additive step, see _rebuild_hitl_tasks and the
#     module docstring. The additive tables landing in the same release (memory M1, vendor
#     scorecards) ride on the same bump.
# 9 = the scorecard CHECK refresh (module docstring, "THE SECOND EXCEPTION"): two CHECKs on
#     vendor_scorecards were tightened after v8 files had been created, and nothing additive
#     can reach an existing table's constraints. _refresh_check_constraints recreates the table
#     from the model when -- and only when -- it is EMPTY; a populated one is left alone and
#     warned about. No new table or column: the bump exists so the step runs once, with a backup.
SCHEMA_VERSION = 9  # bump per release that adds tables/columns

# Why the bump is not optional when a release adds COLUMNS, even though new TABLES seem to
# appear without one: init_db() calls Base.metadata.create_all() after migrate_additive(),
# and create_all creates missing tables but never missing columns. So a release that forgets
# to bump gets its new tables silently (and with NO pre-migration backup, because the backup
# only runs when the version moves) while every new column is quietly absent -- and the first
# query that selects one fails with "no such column" at runtime, not at startup.
#
# That is not hypothetical: this repo's own dev database sat at version 4 with a `vendors`
# table present and `problems.root_cause` missing, and GET /api/v1/dashboard/regions raised
# OperationalError: no such column: problems.root_cause. Bump the version in the SAME change
# that adds the column.

log = logging.getLogger("noc_agents.db.migrate")

# The report of the most recent migrate_additive() call in this process, for the
# startup log (main.py calls init_db() at import time and only gets the engine back).
LAST_REPORT: MigrationReport | None = None


@dataclass(frozen=True)
class MigrationReport:
    """What one start-up did to the database."""

    from_version: int  # version found on disk: 0 = empty file, 1 = no schema_version table
    to_version: int  # SCHEMA_VERSION of this code
    backup_path: Path | None  # pre-migration copy; None when nothing needed migrating
    applied: list[str] = field(default_factory=list)  # DDL executed, in order; [] on a no-op start
    note: str = ""  # why a step was skipped (new database, not SQLite, ...)

    @property
    def changed(self) -> bool:
        return bool(self.applied)


# --------------------------------------------------------------------------- helpers


def sqlite_file(engine: Engine) -> Path | None:
    """The database file behind the engine, or None for non-SQLite / in-memory."""
    if engine.dialect.name != "sqlite":
        return None
    database = engine.url.database
    if not database or database == ":memory:":
        return None
    return Path(database).resolve()


def default_backup_dir(engine: Engine) -> Path:
    """``<database file's folder>/backups`` -- ``data/backups`` for the shipped DB."""
    db_file = sqlite_file(engine)
    if db_file is None:
        return Path("data") / "backups"
    return db_file.parent / "backups"


def _user_tables(conn: Connection) -> set[str]:
    rows = conn.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {r[0] for r in rows}


def _read_version(conn: Connection, tables: set[str]) -> int:
    """0 for an empty file, 1 when schema_version is absent, else the highest row."""
    if not tables:
        return 0
    if SchemaVersionRow.__tablename__ not in tables:
        return 1
    stored = conn.exec_driver_sql(
        f"SELECT MAX(version) FROM {SchemaVersionRow.__tablename__}"
    ).scalar()
    return int(stored) if stored is not None else 1


def _backup(engine: Engine, db_file: Path, backup_dir: Path, from_v: int, to_v: int) -> Path:
    """Copy the live file with the sqlite3 backup API (consistent even with WAL on).

    The spec names the copy ``noc_agents.<from>-><to>.<ts>.db``; ``>`` is not a legal
    filename character on Windows, so the arrow is spelled ``-to-``. The prefix is the
    database file's own stem, so ``noc_agents.db`` and ``demo_safaricom.db`` never
    collide in a shared backups folder.
    """
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stem = f"{db_file.stem}.{from_v}-to-{to_v}.{ts}"
    target = backup_dir / f"{stem}.db"
    n = 1
    while target.exists():  # same second, same file: never overwrite a backup
        target = backup_dir / f"{stem}.{n}.db"
        n += 1
    raw = engine.raw_connection()
    try:
        source: sqlite3.Connection = raw.driver_connection  # the pooled sqlite3 connection
        dest = sqlite3.connect(str(target))
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        raw.close()
    return target


def _add_column_sql(table: Table, col: Column, engine: Engine) -> str:
    dialect = engine.dialect
    quote = dialect.identifier_preparer.quote
    sql = f"ALTER TABLE {quote(table.name)} ADD COLUMN {quote(col.name)} {col.type.compile(dialect=dialect)}"
    default = dialect.ddl_compiler(dialect, None).get_column_default_string(col)
    if default is not None:
        sql += f" DEFAULT {default}"
    return sql  # NOT NULL deliberately omitted -- see the module docstring


def _one_line(sql: str) -> str:
    return " ".join(sql.split())


def _plan(conn: Connection, engine: Engine, deferrals: list[str] | None = None) -> list[str]:
    """Every statement needed to bring the file up to Base.metadata, in dependency order.

    Reads the live catalogue (sqlite_master, PRAGMA table_info/index_list) so the list
    is exactly what will run -- the report and the startup log show real work only. An index
    on a large existing table is left out and explained in ``deferrals`` (see
    ``_missing_index_sql``); the maintenance command builds it later.
    """
    dialect = engine.dialect
    existing = _user_tables(conn)
    statements: list[str] = []
    for table in Base.metadata.sorted_tables:
        indexes = sorted(table.indexes, key=lambda ix: ix.name or "")
        if table.name not in existing:
            statements.append(_one_line(str(CreateTable(table, if_not_exists=True).compile(dialect=dialect))))
            statements.extend(str(CreateIndex(ix, if_not_exists=True).compile(dialect=dialect)) for ix in indexes)
            continue
        present_cols = {r[1] for r in conn.exec_driver_sql(f"PRAGMA table_info({table.name})").fetchall()}
        for col in table.columns:
            if col.name not in present_cols:
                statements.append(_add_column_sql(table, col, engine))
        statements.extend(_missing_index_sql(conn, engine, table, deferrals=deferrals if deferrals is not None else []))
    return statements


#: Above this many rows (estimated), a missing index is NOT built at startup but left to the
#: maintenance command (module docstring, "INDEX BUILDS AND THE WRITE LOCK"). The arithmetic:
#: 2M audit_events rows took 75-150 s under the write lock on the reference machine, so 100k
#: rows is roughly 4-8 s -- well inside the 30 s busy timeout every other writer waits, and
#: short enough that a second worker starting at the same moment waits rather than crashes.
INDEX_BUILD_AT_STARTUP_MAX_ROWS = 100_000
#: What the warning tells the operator to run. Spelled once, here.
BUILD_INDEXES_COMMAND = "python -m noc_agents.db.migrate --build-indexes"


def _row_estimate(conn: Connection, table: str) -> int:
    """An UPPER bound on the row count in O(log n): ``max(rowid)``. Rowids are never reused
    after a delete, so a pruned table reads larger than it is -- which only ever defers a build
    that would have been quick, never the other way round. Anything unreadable counts as large."""
    try:
        return int(conn.exec_driver_sql(f"SELECT COALESCE(MAX(rowid), 0) FROM {table}").scalar() or 0)
    except Exception:  # noqa: BLE001 -- a WITHOUT ROWID table, a virtual table: treat as large
        return sys.maxsize


def _missing_index_sql(
    conn: Connection,
    engine: Engine,
    only: Table | None = None,
    *,
    threshold: int | None = None,
    deferrals: list[str] | None = None,
) -> list[str]:
    """``CREATE INDEX IF NOT EXISTS`` for every mapped index a table on disk does not have.

    Part of ``_plan`` -- and, since an index holds no data, also run on EVERY start on its own
    (module docstring, "ON EVERY START"), so an index added to a model reaches existing files
    without a version bump or a backup. Tables not on disk are skipped: ``_plan`` creates them
    whole, indexes included.

    ``threshold`` (default ``INDEX_BUILD_AT_STARTUP_MAX_ROWS``) keeps a build off the startup
    write lock when the table is large: such an index is left out of the statements and a
    warning naming it, the table, the estimate and ``BUILD_INDEXES_COMMAND`` is appended to
    ``deferrals``. ``threshold=None`` -- the maintenance command -- never defers.
    """
    if threshold is None and deferrals is not None:
        threshold = INDEX_BUILD_AT_STARTUP_MAX_ROWS
    existing = _user_tables(conn)
    statements: list[str] = []
    for table in Base.metadata.sorted_tables if only is None else [only]:
        if table.name not in existing:
            continue
        present_idx = {r[1] for r in conn.exec_driver_sql(f"PRAGMA index_list({table.name})").fetchall()}
        missing = [ix for ix in sorted(table.indexes, key=lambda ix: ix.name or "") if ix.name not in present_idx]
        if not missing:
            continue
        estimate = _row_estimate(conn, table.name) if threshold is not None else 0
        for ix in missing:
            sql = str(CreateIndex(ix, if_not_exists=True).compile(dialect=engine.dialect))
            if threshold is not None and estimate > threshold:
                assert deferrals is not None
                deferrals.append(
                    f"index {ix.name} on {table.name} is missing and the table is large (~{estimate:,} rows, above "
                    f"INDEX_BUILD_AT_STARTUP_MAX_ROWS={threshold:,}): not built at startup, where it would hold the "
                    f"write lock for minutes and fail every other writer. Build it when the NOC is quiet: "
                    f"{BUILD_INDEXES_COMMAND}"
                )
                continue
            statements.append(sql)
    return statements


def _stamp_version(conn: Connection, version: int) -> None:
    conn.execute(SchemaVersionRow.__table__.insert().values(version=version, applied_at=utcnow()))


def _enable_wal(engine: Engine) -> str:
    """Write-ahead logging: readers never block the writer. Persistent in the file."""
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        mode = str(conn.exec_driver_sql("PRAGMA journal_mode=WAL").scalar() or "")
    if mode.lower() != "wal":
        log.warning("could not switch the database to WAL (journal_mode=%s); continuing", mode)
    return mode


# ------------------------------------------------ the ONE non-additive step (schema_version 8)
#
# Everything between this line and "entry point" exists for one table and one release. Read
# the module docstring first. It is deliberately NOT parameterised by table: a second caller
# would need its own argument about verification, backfill and rollback, and a generic
# ``rebuild(table)`` would make that argument look already won.

_HITL = HitlTaskRow.__tablename__
_HITL_REBUILD = f"{_HITL}__v8_rebuild"  # lives only inside the transaction; never survives a commit
_INCIDENTS = IncidentRow.__tablename__


class HitlRebuildError(RuntimeError):
    """The hitl_tasks rebuild refused to go on. Raised inside the migration transaction, so the
    caller rolls back and the file is exactly what it was."""


def _table_columns(conn: Connection, table: str) -> list[tuple[str, int]]:
    """``(name, notnull)`` per column, in table order, from the live catalogue."""
    return [(r[1], int(r[3])) for r in conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()]


def _count(conn: Connection, table: str) -> int:
    return int(conn.exec_driver_sql(f"SELECT COUNT(*) FROM {table}").scalar() or 0)


def _q(column: str) -> str:
    """A column name as a quoted SQL identifier (``status`` and friends are harmless today;
    the next column added to the model might not be)."""
    return '"' + column.replace('"', '""') + '"'


def _hitl_tasks_needs_rebuild(conn: Connection) -> bool:
    """True while the file's ``hitl_tasks.incident_id`` is still NOT NULL.

    The live catalogue decides, not the version number. That is what makes the step idempotent
    (a rebuilt table reports 0 and is left alone), what keeps it off a fresh database (created
    nullable by CREATE TABLE a moment earlier) and what keeps it honest about files whose
    version stamp and shape disagree -- tests/unit/test_phase2_schema.py builds exactly such a
    file on purpose (v8 shape, stamped 2) and requires that no DROP or RENAME runs against it.
    """
    return any(name == "incident_id" and notnull for name, notnull in _table_columns(conn, _HITL))


def _attached_sql(conn: Connection, table: str = _HITL) -> list[str]:
    """The CREATE statements of every index and trigger on a table, to replay after a rebuild.

    DROP TABLE takes them with it. Read from ``sqlite_master`` rather than from the ORM so an
    index somebody added by hand survives too. Rows with NULL sql are SQLite's own
    auto-indexes (the primary key's, a UNIQUE's), which the new table's DDL recreates itself.
    ``COLLATE NOCASE`` because SQLite stores a trigger's ``tbl_name`` as the CREATE spelled it
    (``ON VENDOR_SCORECARDS``, ``ON "Vendor_Scorecards"``) while matching the table itself
    case-insensitively -- so the DROP takes such a trigger too, and a case-sensitive capture
    would silently lose it.
    """
    rows = conn.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE tbl_name = ? COLLATE NOCASE AND type IN ('index', 'trigger') "
        "AND sql IS NOT NULL ORDER BY type, name",
        (table,),
    ).fetchall()
    return [r[0] for r in rows]


def _attached_names(conn: Connection, table: str) -> set[tuple[str, str]]:
    """``{(type, name)}`` of EVERYTHING hanging off a table -- auto-indexes included -- which is
    what a DROP TABLE removes and what must exist again after a rebuild. The post-check compares
    against this, not against what ``_attached_sql`` chose to replay."""
    rows = conn.exec_driver_sql(
        "SELECT type, name FROM sqlite_master WHERE tbl_name = ? COLLATE NOCASE AND type IN ('index', 'trigger')",
        (table,),
    ).fetchall()
    return {(r[0], r[1]) for r in rows}


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


_PROBE_SAVEPOINT = "hitl_v8_rename_probe"


def _blocker_named_by(conn: Connection, message: str) -> tuple[str, str, str] | None:
    """``(kind, name, detail)`` for SQLite's ``error in <view|trigger> <name>: <detail>``.

    The name is resolved against ``sqlite_master``, never split out of the text: a view called
    ``"odd: name"`` would otherwise be cut at its first ``': '`` and the probe would try to drop
    a view that does not exist. Every view and trigger whose ``error in <kind> <name>: `` the
    message starts with is a candidate; the longest name wins.
    """
    best: tuple[str, str, str] | None = None
    for kind, name in conn.exec_driver_sql("SELECT type, name FROM sqlite_master WHERE type IN ('view', 'trigger')").fetchall():
        prefix = f"error in {kind} {name}: "
        if message.startswith(prefix) and (best is None or len(name) > len(best[1])):
            best = (kind, name, message[len(prefix):])
    return best


def _rename_blockers(conn: Connection, engine: Engine) -> list[tuple[str, str, str]]:
    """Every view and trigger that would make the rebuild's RENAME fail, decided by SQLite itself,
    as ``(kind, name, what SQLite said about it)``.

    Inside a transaction that is always rolled back: CREATE the rebuild table (the exact DDL the
    rebuild uses), DROP ``hitl_tasks``, RENAME. If the RENAME fails naming a view or trigger,
    drop that object -- inside the same doomed transaction -- and try again, so the list is
    complete and not just the first thing SQLite happened to parse. Then roll everything back.

    Two hand-written matchers preceded this and both were wrong (module docstring). SQLite's
    parser is the only correct oracle for "does this SQL refer to that table", and running the
    real statements on the real connection also inherits whatever ``legacy_alter_table`` or
    other pragma state the real RENAME will run under.

    Outside a transaction the probe is ``BEGIN IMMEDIATE ... ROLLBACK``, which leaves the file
    byte for byte as it was in both journal modes (pinned by test). Inside the migration's own
    transaction it nests as a SAVEPOINT that is rolled back to.
    """
    inside = bool(getattr(conn.connection.driver_connection, "in_transaction", False))
    begin, undo, done = (
        (f"SAVEPOINT {_PROBE_SAVEPOINT}", f"ROLLBACK TO {_PROBE_SAVEPOINT}", f"RELEASE {_PROBE_SAVEPOINT}")
        if inside
        else ("BEGIN IMMEDIATE", "ROLLBACK", None)
    )
    blockers: list[tuple[str, str, str]] = []
    conn.exec_driver_sql(begin)
    try:
        conn.exec_driver_sql(_rebuild_table_ddl(engine))
        conn.exec_driver_sql(f"DROP TABLE {_HITL}")
        for _ in range(500):
            try:
                conn.exec_driver_sql(f"ALTER TABLE {_HITL_REBUILD} RENAME TO {_HITL}")
                break
            except OperationalError as exc:
                found = _blocker_named_by(conn, str(exc.orig))
                if found is None:
                    raise HitlRebuildError(
                        f"SQLite refused the {_HITL} rebuild's RENAME for a reason other than a view or trigger: "
                        f"{exc.orig}. Nothing was changed and no backup was written."
                    ) from exc
                kind, name, _detail = found
                blockers.append(found)
                conn.exec_driver_sql(f"DROP {kind.upper()} {_quote_ident(name)}")
        else:
            raise HitlRebuildError(f"more than 500 views/triggers block the {_HITL} rebuild; giving up the count")
    finally:
        conn.exec_driver_sql(undo)
        if done:
            conn.exec_driver_sql(done)
    return blockers


def _refuse_rename_blockers(conn: Connection, engine: Engine) -> None:
    """A view, or a trigger on ANOTHER table, that refers to ``hitl_tasks`` makes the rebuild's
    ``ALTER TABLE ... RENAME`` fail once the old table is gone. That failure is safe -- it rolls
    back -- but it recurs on every start with a message that does not say what to do, and each
    attempt writes another backup. Refuse first, name every one, and say what to do. (Triggers
    ON hitl_tasks itself are fine: they go with the old table and ``_attached_sql`` replays
    them.) No view or trigger exists in this codebase; this is for the file somebody has been
    reporting from or patching by hand."""
    blockers = _rename_blockers(conn, engine)
    if blockers:
        named = ", ".join(f"{kind} {name}" for kind, name, _detail in blockers)
        said = "; ".join(f"{kind} {name}: {detail}" for kind, name, detail in blockers)
        # Worded to be true for every blocker SQLite can name: one that refers to hitl_tasks, and
        # one that is simply broken (a view over a table dropped long ago) -- the RENAME re-parses
        # every view and trigger in the schema and fails on either.
        raise HitlRebuildError(
            f"{named}: SQLite cannot re-parse {'it' if len(blockers) == 1 else 'them'} once the rebuilt {_HITL} is "
            f"renamed into place, so the rebuild would fail on every start -- because of a reference to {_HITL}, "
            f"or because the object is broken in its own right (SQLite said: {said}). Drop each one "
            "(DROP VIEW / DROP TRIGGER <name>), start again, then recreate it against the rebuilt table. "
            "Nothing was changed and no backup was written."
        )


def _refuse_nulls_the_model_forbids(conn: Connection, engine: Engine) -> None:
    """A file that grew from v1 has ``entity_type`` and ``edited`` NULLABLE on disk: ADD COLUMN
    is rendered without NOT NULL (module docstring). The rebuilt table takes the model's DDL,
    where both are NOT NULL, so a NULL in either -- impossible through the ORM, which always
    supplies a value, but one raw UPDATE away -- would fail the copy on every start. Refuse
    before anything is done, naming the column, the count and the statement that fixes it.
    The alternative, COALESCE-ing the value in during the copy, would have the migration
    rewrite a cell of the approval trail on its own authority; a human runs that UPDATE."""
    on_disk = dict(_table_columns(conn, _HITL))  # name -> notnull; a column not yet on disk arrives with its DEFAULT
    compiler = engine.dialect.ddl_compiler(engine.dialect, None)
    offenders: list[str] = []
    for col in HitlTaskRow.__table__.columns:
        if col.nullable or on_disk.get(col.name, 1):
            continue
        nulls = int(conn.exec_driver_sql(f"SELECT COUNT(*) FROM {_HITL} WHERE {_q(col.name)} IS NULL").scalar() or 0)
        if nulls:
            default = compiler.get_column_default_string(col)
            value = default if default is not None else "<a value you choose>"
            offenders.append(
                f"{col.name}: {nulls} NULL row(s); fix: UPDATE {_HITL} SET {_q(col.name)} = {value} WHERE {_q(col.name)} IS NULL"
            )
    if offenders:
        raise HitlRebuildError(
            f"{_HITL} holds NULL in column(s) the model declares NOT NULL, so the rebuilt table would refuse the "
            "copy. Run the statement(s) below (each is one deliberate change to the approval trail), then start "
            "again. Nothing was changed and no backup was written. " + " | ".join(offenders)
        )


def _hitl_rebuild_preflight(conn: Connection, engine: Engine) -> None:
    """Everything that would make the rebuild fail on EVERY start, checked read-only BEFORE the
    backup is taken (so a refused start does not pile up backups) and again inside the
    transaction (so a change made between the two is still caught). No-op unless the file's
    ``hitl_tasks`` still needs the rebuild."""
    if not _hitl_tasks_needs_rebuild(conn):
        return
    unknown = sorted(set(name for name, _ in _table_columns(conn, _HITL)) - {c.name for c in HitlTaskRow.__table__.columns})
    if unknown:
        # The rebuild copies the columns the model knows. One it does not know would be dropped
        # from the approval trail without a word. A human decides what it was.
        raise HitlRebuildError(
            f"{_HITL} has column(s) the model does not know: {unknown}; a rebuild would drop them. Refusing; "
            "nothing was changed and no backup was written."
        )
    _refuse_rename_blockers(conn, engine)
    _refuse_nulls_the_model_forbids(conn, engine)


def _rebuild_table_ddl(engine: Engine) -> str:
    """The new table's CREATE, from the ORM's own DDL -- the statement a fresh database gets, with
    only the table name changed -- so a migrated file and a new file cannot drift apart. Shared by
    the rebuild and by the rename probe, so the probe exercises exactly what the rebuild runs."""
    ddl = _one_line(str(CreateTable(HitlTaskRow.__table__).compile(dialect=engine.dialect)))
    head = f"CREATE TABLE {_HITL} ("
    if not ddl.startswith(head) or ddl.count(head) != 1:
        raise HitlRebuildError(f"unexpected DDL for {_HITL}; refusing to rewrite it: {ddl[:80]!r}")
    return f"CREATE TABLE {_HITL_REBUILD} (" + ddl[len(head):]


def _create_rebuild_table(conn: Connection, engine: Engine) -> str:
    sql = _rebuild_table_ddl(engine)
    conn.exec_driver_sql(sql)
    return sql


def _copy_hitl_rows(conn: Connection, columns: list[str]) -> str:
    """Copy every row, ``rowid`` included, filling ``operator_id`` from the row's incident.

    ``rowid`` is copied so that an unordered SELECT returns the rows in the order it always
    did. ``incidents.id`` is a primary key, so the LEFT JOIN matches at most one incident and
    can never duplicate a task; a task with no matching incident keeps ``operator_id`` NULL
    (see the module docstring for why that is the right answer). COALESCE keeps an owner the
    row already carries: on a v7 file the column was added, all NULL, a few statements ago,
    but a file where ``operator_id`` landed first must not have its values re-derived.
    """
    targets = ", ".join(_q(c) for c in columns)
    sources = ", ".join(
        'COALESCE(t."operator_id", i."operator_id")' if c == "operator_id" else f"t.{_q(c)}" for c in columns
    )
    sql = (
        f"INSERT INTO {_HITL_REBUILD} (rowid, {targets}) SELECT t.rowid, {sources} "
        f"FROM {_HITL} AS t LEFT JOIN {_INCIDENTS} AS i ON i.id = t.incident_id ORDER BY t.rowid"
    )
    conn.exec_driver_sql(sql)
    return sql


def _verify_hitl_copy(conn: Connection, columns: list[str]) -> tuple[int, list[str]]:
    """Prove the copy before the original is dropped. Returns ``(rows, unowned_ids)``.

    Three checks, all inside the transaction, any failure raising so it rolls back:

    1. the row counts are equal;
    2. every column except ``operator_id`` is identical in both tables, row by row: no row of
       the original is missing from, or different in, the copy. Rows are matched on ``rowid``
       and compared on value AND ``typeof`` -- a bare EXCEPT treats integer 1 and real 1.0 as
       equal, and "byte for byte" should mean it. (EXCEPT treats NULLs as equal to each other,
       which is what is wanted here.) One direction is the whole proof: ``rowid`` is unique in
       each table, so equal counts plus "every original row is in the copy" leaves no room for
       an extra or invented row in the copy;
    3. ``operator_id`` is what the rule says: unchanged where the old row had one, otherwise
       the incident's operator, otherwise (no such incident) NULL.
    """
    before, after = _count(conn, _HITL), _count(conn, _HITL_REBUILD)
    if before != after:
        raise HitlRebuildError(f"{_HITL} rebuild copied {after} row(s) of {before}; rolled back, nothing dropped")

    compared = ", ".join(f"{_q(c)}, typeof({_q(c)})" for c in columns if c != "operator_id")
    differing = conn.exec_driver_sql(
        f"SELECT COUNT(*) FROM (SELECT rowid, {compared} FROM {_HITL} "
        f"EXCEPT SELECT rowid, {compared} FROM {_HITL_REBUILD})"
    ).scalar()
    if differing:
        raise HitlRebuildError(
            f"{_HITL} rebuild: {differing} row(s) missing from or altered in the copy; rolled back, nothing dropped"
        )

    wrong_owner = conn.exec_driver_sql(
        f"SELECT COUNT(*) FROM {_HITL} AS t JOIN {_HITL_REBUILD} AS n ON n.rowid = t.rowid "
        f"LEFT JOIN {_INCIDENTS} AS i ON i.id = t.incident_id "
        'WHERE n."operator_id" IS NOT COALESCE(t."operator_id", i."operator_id")'
    ).scalar()
    if wrong_owner:
        raise HitlRebuildError(
            f"{_HITL} rebuild: {wrong_owner} row(s) were given the wrong operator_id; rolled back, nothing dropped"
        )

    unowned = [
        r[0]
        for r in conn.exec_driver_sql(
            f"SELECT id FROM {_HITL_REBUILD} WHERE operator_id IS NULL ORDER BY rowid"
        ).fetchall()
    ]
    return before, unowned


def _swap_hitl_tables(conn: Connection, attached: list[str]) -> list[str]:
    """Drop the old table, rename the new one onto its name, put the indexes/triggers back.

    The order matters. Renaming the OLD table out of the way first would make SQLite rewrite
    every reference to it elsewhere in the schema to follow it to its new name; dropping it
    and renaming the new table INTO the name leaves every such reference alone.
    """
    statements = [f"DROP TABLE {_HITL}", f"ALTER TABLE {_HITL_REBUILD} RENAME TO {_HITL}", *attached]
    for sql in statements:
        conn.exec_driver_sql(sql)
    return statements


def _rebuild_hitl_tasks(conn: Connection, engine: Engine) -> list[str]:
    """Make ``hitl_tasks.incident_id`` nullable. The one non-additive step; see the module docstring.

    Must run inside the migration transaction and AFTER the additive pass, which has by then
    added every missing column (``operator_id`` among them) to the old table -- so the two
    tables have the same columns and the copy and its verification are column-for-column.
    Returns the statements executed, for the report; ``[]`` when the table needs nothing.
    """
    if not _hitl_tasks_needs_rebuild(conn):
        return []

    _hitl_rebuild_preflight(conn, engine)  # already ran before the backup; cheap, and now under the write lock
    old_columns = [name for name, _ in _table_columns(conn, _HITL)]
    new_columns = [c.name for c in HitlTaskRow.__table__.columns]
    if set(old_columns) != set(new_columns):
        # The preflight refused old-only columns; new-only ones mean the additive pass did not
        # run first. Not survivable by guessing either way.
        raise HitlRebuildError(
            f"{_HITL} columns differ from the model (file only: {sorted(set(old_columns) - set(new_columns))}, "
            f"model only: {sorted(set(new_columns) - set(old_columns))}); refusing to rebuild"
        )
    attached = _attached_sql(conn)
    attached_names = _attached_names(conn, _HITL)  # everything the DROP will take, replayed or not

    applied = [_create_rebuild_table(conn, engine), _copy_hitl_rows(conn, new_columns)]
    rows, unowned = _verify_hitl_copy(conn, new_columns)
    applied.extend(_swap_hitl_tables(conn, attached))

    # Post-conditions, still inside the transaction: the table that now answers to the name is
    # the new one, it has every row, and nothing that hung off the old one went missing.
    if _hitl_tasks_needs_rebuild(conn) or _count(conn, _HITL) != rows:
        raise HitlRebuildError(f"{_HITL} is not in the expected state after the swap; rolled back")
    lost = attached_names - _attached_names(conn, _HITL)
    if lost or sorted(_attached_sql(conn)) != sorted(attached):
        raise HitlRebuildError(f"{_HITL} lost an index or trigger in the swap ({sorted(lost)}); rolled back")

    log.info("%s rebuilt: %d row(s) copied and verified, incident_id is now nullable", _HITL, rows)
    if unowned:
        log.warning(
            "%s: %d task(s) name an incident that does not exist, so no operator_id could be derived. "
            "They were copied unchanged and stay invisible to every operator, as they already were: %s",
            _HITL, len(unowned), ", ".join(unowned[:50]) + (" ..." if len(unowned) > 50 else ""),
        )
    return applied


# ------------------------------------- the SECOND named exception (schema_version 9): CHECKs
#
# Read the module docstring ("THE SECOND EXCEPTION") first. Two tables, by name, and nothing
# that would let a third join them without its own argument.

#: The tables whose CHECK constraints were tightened after v8 files existed. The lines table is
#: listed because it hangs off the cards table by foreign key and is refreshed in dependency
#: order with it; today its DDL has not changed, so it is compared and left alone.
_CHECK_REFRESH_TABLES = ("vendor_scorecards", "vendor_scorecard_lines")


class CheckRefreshError(RuntimeError):
    """The CHECK refresh found the file in a state it will not act on. Raised inside the
    migration transaction, so the caller rolls back and the file is exactly what it was."""


def _normalised_ddl(sql: str) -> str:
    """One CREATE TABLE statement, in the form both sides can be compared in: whitespace
    collapsed (the additive pass one-lines its DDL, ``create_all`` does not), ``IF NOT EXISTS``
    dropped (``sqlite_master`` never stores it), the table name unquoted (a table renamed into
    place is stored as ``CREATE TABLE "name"``)."""
    s = " ".join(sql.split())
    s = s.replace("CREATE TABLE IF NOT EXISTS ", "CREATE TABLE ", 1)
    return re.sub(r'^CREATE TABLE "([^"]+)"', r"CREATE TABLE \1", s)


def _live_ddl(conn: Connection, table: str) -> str | None:
    return conn.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).scalar()


def _compiled_ddl(table: Table, engine: Engine) -> str:
    return _normalised_ddl(str(CreateTable(table).compile(dialect=engine.dialect)))


def _check_clauses(ddl: str) -> dict[str, str]:
    """``{constraint name: expression}`` for every named CHECK in a normalised CREATE TABLE."""
    return dict(re.findall(r"CONSTRAINT (\w+) CHECK \((.*?)\)(?=, CONSTRAINT |, FOREIGN KEY|, PRIMARY KEY| \)$)", ddl))


def _check_drift(conn: Connection, engine: Engine) -> dict[str, tuple[Table, str, str, int]]:
    """``{table: (Table, live DDL, model DDL, row count)}`` for every table in
    ``_CHECK_REFRESH_TABLES`` whose live definition differs from the model's. Read-only; runs on
    every start. A table not on disk is skipped: the additive pass creates it current."""
    drifted: dict[str, tuple[Table, str, str, int]] = {}
    for table in Base.metadata.sorted_tables:
        if table.name not in _CHECK_REFRESH_TABLES:
            continue
        live = _live_ddl(conn, table.name)
        if live is None:
            continue
        compiled = _compiled_ddl(table, engine)
        if _normalised_ddl(live) != compiled:
            drifted[table.name] = (table, _normalised_ddl(live), compiled, _count(conn, table.name))
    return drifted


def _drift_warning(name: str, live: str, compiled: str, rows: int) -> str:
    """The one message a populated, drifted table gets -- on every start, until it is dealt with."""
    old, new = _check_clauses(live), _check_clauses(compiled)
    changed = sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))
    return (
        f"{name} has {rows} row(s) and a definition older than the model's: CHECK constraint(s) "
        f"{', '.join(changed) if changed else '<a difference outside the CHECK clauses>'} are not enforced by "
        "this file. Left as it is -- a populated table is never rebuilt here; the ORM mapper guards in "
        "db/models_scorecards.py still refuse every ORM write. To adopt the current constraints: export the "
        "rows, empty the table, restart (the table is then recreated from the model, behind a backup taken at "
        "that start), reload."
    )


def _refresh_check_constraints(conn: Connection, engine: Engine) -> tuple[list[str], list[str]]:
    """Bring the CHECKs of the tables in ``_CHECK_REFRESH_TABLES`` up to the model -- where that
    costs nothing. Returns ``(statements applied, notes for the report)``.

    Runs inside a write transaction: the migration's own after the additive pass (so every table
    it looks at exists), or the fast path's when a start finds a drifted empty table (module
    docstring, "ON EVERY START"). The live DDL decides, never the version: a fresh file, a file
    created after the tightening and a file this step already refreshed all compare equal.

    * DDL differs and the table is EMPTY: drop and create from the model, dependent tables
      first for the drop and last for the create. The count is re-read immediately before each
      DROP, under the write lock; a row appearing there aborts the whole transaction. Indexes and
      triggers that hung off the table are replayed from ``sqlite_master`` (matched
      case-insensitively, as SQLite matches them); an ORM index that still is not there
      afterwards is created. Verified afterwards: the live DDL now equals the model's, the table
      is still empty, and every index and trigger that existed before the drop exists again --
      compared by name against what WAS there, not against what was captured for replay.
    * DDL differs and the table has ROWS: untouched, warned about (``_drift_warning``), noted.
    """
    applied: list[str] = []
    notes: list[str] = []
    drifted = _check_drift(conn, engine)
    if not drifted:
        return applied, notes

    rebuild = [table for table, _live, _compiled, rows in drifted.values() if rows == 0]
    rebuilding = {t.name for t in rebuild}  # by name: Table objects are SQL expressions, not values
    for name, (table, live, compiled, rows) in drifted.items():
        if name not in rebuilding:
            message = _drift_warning(name, live, compiled, rows)
            log.warning(message)
            notes.append(message)

    if not rebuild:
        return applied, notes
    # Drop dependents first, create them last: sorted_tables is dependency order.
    attached = {t.name: _attached_sql(conn, t.name) for t in rebuild}
    attached_names = {t.name: _attached_names(conn, t.name) for t in rebuild}
    for table in reversed(rebuild):
        if _count(conn, table.name) != 0:
            raise CheckRefreshError(f"{table.name} gained a row while the migration held the write lock; rolled back")
        sql = f"DROP TABLE {table.name}"
        conn.exec_driver_sql(sql)
        applied.append(sql)
    for table in rebuild:
        statements = [_one_line(str(CreateTable(table).compile(dialect=engine.dialect))), *attached[table.name]]
        for sql in statements:
            conn.exec_driver_sql(sql)
            applied.append(sql)
        present = {r[1] for r in conn.exec_driver_sql(f"PRAGMA index_list({table.name})").fetchall()}
        for ix in sorted(table.indexes, key=lambda ix: ix.name or ""):
            if ix.name not in present:  # an ORM index the old table never had
                sql = str(CreateIndex(ix).compile(dialect=engine.dialect))
                conn.exec_driver_sql(sql)
                applied.append(sql)
        # Post-conditions, still inside the transaction.
        live = _live_ddl(conn, table.name)
        if live is None or _normalised_ddl(live) != _compiled_ddl(table, engine):
            raise CheckRefreshError(f"{table.name} does not match the model after being recreated; rolled back")
        if _count(conn, table.name) != 0:
            raise CheckRefreshError(f"{table.name} is not empty after being recreated; rolled back")
        present = {r[1] for r in conn.exec_driver_sql(f"PRAGMA index_list({table.name})").fetchall()}
        missing = {ix.name for ix in table.indexes} - present
        lost = attached_names[table.name] - _attached_names(conn, table.name)
        if missing or lost or not set(attached[table.name]) <= set(_attached_sql(conn, table.name)):
            raise CheckRefreshError(
                f"{table.name} lost an index or trigger in the refresh (ORM indexes missing: {sorted(missing)}; "
                f"objects gone: {sorted(lost)}); rolled back"
            )
        log.info("%s was empty and its CHECK constraints were older than the model's: recreated from the model", table.name)
    return applied, notes


# ------------------------------------------------------------ what every start looks for


@dataclass(frozen=True)
class _RoutineWork:
    """What a start on a file that is already at SCHEMA_VERSION still has to do (usually nothing)."""

    indexes: list[str]  # CREATE INDEX IF NOT EXISTS for mapped indexes the file lacks
    refresh: bool  # a drifted, EMPTY table in _CHECK_REFRESH_TABLES: recreate it, behind a backup
    warnings: list[str]  # drifted, POPULATED tables: say so, every start


def _routine_work(conn: Connection, engine: Engine) -> _RoutineWork:
    """Only ever called for a file AT this code's version (a newer file gets nothing, see
    ``migrate_additive``). An index on a large table is not in ``indexes`` but in ``warnings``."""
    deferred: list[str] = []
    indexes = _missing_index_sql(conn, engine, deferrals=deferred)
    drifted = _check_drift(conn, engine)
    return _RoutineWork(
        indexes=indexes,
        refresh=any(rows == 0 for _t, _l, _c, rows in drifted.values()),
        warnings=[_drift_warning(name, live, compiled, rows) for name, (_t, live, compiled, rows) in drifted.items() if rows] + deferred,
    )


# ------------------------------------------------------------------------ entry point


def migrate_additive(engine: Engine, *, backup_dir: Path) -> MigrationReport:
    """1. Read schema_version (create the table if absent; treat missing as 1).
       2. If SCHEMA_VERSION > stored: copy the DB file to backup_dir/<db>.<stored>-to-<SCHEMA_VERSION>.<ts>.db
          BEFORE any change (sqlite3 backup API, works while WAL is active).
       3. In ONE transaction: for every table in Base.metadata.sorted_tables, CREATE TABLE IF NOT EXISTS;
          for each mapped column missing from PRAGMA table_info(<table>), ALTER TABLE ADD COLUMN with the
          column's DDL default (NULL or a literal). Never drops, renames or changes types -- with the ONE
          exception of step 3b.
       3b. Same transaction: _rebuild_hitl_tasks(), a no-op unless the file's hitl_tasks.incident_id is
          still NOT NULL (module docstring, "THE ONE EXCEPTION").
       3c. Same transaction: _refresh_check_constraints(), a no-op unless a table in _CHECK_REFRESH_TABLES
          has a definition older than the model's -- recreated if empty, warned about if not ("THE SECOND
          EXCEPTION").
       4. Write schema_version = SCHEMA_VERSION in the same transaction; commit; PRAGMA journal_mode=WAL.
       5. On any exception: rollback, leave schema_version unchanged, log the backup path, re-raise.
       6. If SCHEMA_VERSION == stored (every later start): still look for a mapped index the file lacks
          (created, no backup) and for a drifted table in _CHECK_REFRESH_TABLES (recreated behind a backup
          when empty, warned about when not) -- module docstring, "ON EVERY START". Usually nothing.
       Returns the list of applied statements for the startup log."""
    global LAST_REPORT

    if engine.dialect.name != "sqlite":
        # The additive path is SQLite-specific (PRAGMA, backup API); init_db() still
        # runs create_all() for other engines.
        LAST_REPORT = MigrationReport(SCHEMA_VERSION, SCHEMA_VERSION, None, note="not sqlite; create_all only")
        return LAST_REPORT

    db_file = sqlite_file(engine)
    routine: _RoutineWork | None = None
    with engine.connect() as conn:
        stored = _read_version(conn, _user_tables(conn))
        if stored < SCHEMA_VERSION:
            # BEFORE the backup, and read-only in effect (the rename probe rolls back): anything
            # that would make the hitl_tasks rebuild fail on every start is refused here, so a
            # refused start does not write a backup per attempt.
            _hitl_rebuild_preflight(conn, engine)
        elif stored == SCHEMA_VERSION:
            # Every start after the first: the two things that must not wait for a version bump
            # (module docstring, "ON EVERY START"), found with a few catalogue reads.
            routine = _routine_work(conn, engine)

    if stored > SCHEMA_VERSION:
        # Rolled-back code on a newer file. Nothing is compared, created or changed: this code
        # cannot tell a definition the newer release added from one that drifted, and "empty and
        # different" would mean dropping the newer table for the older one (module docstring).
        # Older code keeps running against a newer file because the schema is additive; that
        # promise is kept by doing nothing at all here.
        log.warning(
            "database schema_version %s is NEWER than this code (%s): a rolled-back release. The file is left "
            "exactly as the newer release wrote it -- no index, no CHECK refresh, no backup",
            stored, SCHEMA_VERSION,
        )
        _enable_wal(engine)
        LAST_REPORT = MigrationReport(stored, SCHEMA_VERSION, None, note="schema newer than this code; left as it is")
        return LAST_REPORT

    if routine is not None:
        if not routine.indexes and not routine.refresh:
            # The common case -- no backup, no transaction. A populated, drifted table is
            # named every start; the refresh below would say it too, so it is said here only
            # when nothing else is going to run.
            for warning in routine.warnings:
                log.warning(warning)
            _enable_wal(engine)
            LAST_REPORT = MigrationReport(stored, SCHEMA_VERSION, None, note="; ".join(["schema already current", *routine.warnings]))
            return LAST_REPORT

    backup_path: Path | None = None
    note = ""
    if stored == 0:
        note = "new database: created in full, nothing to back up"
    elif db_file is None:
        note = "in-memory database: nothing to back up"
    elif routine is not None and not routine.refresh:
        note = "mapped index(es) missing from a current file: created without a backup (an index holds no data)"
    else:
        # A version move, or a drifted empty table about to be recreated on a current file --
        # the latter is named <db>.9-to-9.<ts>.db: same version, because the version did not move.
        backup_path = _backup(engine, db_file, backup_dir, stored, SCHEMA_VERSION)
        log.info("schema_version %s -> %s: pre-migration backup written to %s", stored, SCHEMA_VERSION, backup_path)

    applied: list[str] = []
    with engine.connect() as conn:
        # Foreign-key enforcement is OFF on every connection this codebase opens (nothing sets
        # the pragma), and the table rebuild below needs it off: with it on, DROP TABLE runs an
        # implicit DELETE and the copy re-checks every incident_id. SQLite ignores this pragma
        # inside a transaction, so it is read -- and, only if some future connection hook turned
        # it on, switched off -- BEFORE the BEGIN, and put back in the ``finally``. The
        # connection goes back to the pool, so leaving it changed would leak into the app;
        # that is why the BEGIN itself is inside the ``try``: a "database is locked" there
        # must still reach the ``finally``.
        enforcing_fks = bool(conn.exec_driver_sql("PRAGMA foreign_keys").scalar())
        if enforcing_fks:
            conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        try:
            # pysqlite would not open a transaction before DDL; take the write lock now so
            # the plan, the DDL and the version stamp are one atomic unit (and a second
            # process starting at the same moment waits here, then finds nothing to do).
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            if routine is None:
                if _read_version(conn, _user_tables(conn)) >= SCHEMA_VERSION:
                    conn.rollback()
                    LAST_REPORT = MigrationReport(stored, SCHEMA_VERSION, backup_path, note="migrated by another process")
                    return LAST_REPORT
                deferred: list[str] = []
                for sql in _plan(conn, engine, deferred):
                    conn.exec_driver_sql(sql)
                    applied.append(sql)
                for warning in deferred:  # an index on a large table: built by the command, not here
                    log.warning(warning)
                note = "; ".join(part for part in (note, *deferred) if part)
                # The one non-additive step. After the additive pass on purpose (the old table has
                # every column by now), inside the same transaction on purpose (a failure in it, or
                # after it, undoes it). Returns [] on every file that does not need it.
                applied.extend(_rebuild_hitl_tasks(conn, engine))
            else:
                # A current file with work left over: indexes the model has and the file lacks.
                # Re-derived under the write lock; another process may have got here first.
                deferred = []
                for sql in _missing_index_sql(conn, engine, deferrals=deferred):
                    conn.exec_driver_sql(sql)
                    applied.append(sql)
                for warning in deferred:
                    log.warning(warning)
                note = "; ".join(part for part in (note, *deferred) if part)
            # The second exception: CHECKs the additive path can never deliver. Destructive only
            # on an EMPTY table; a populated one is warned about, and the warning rides on the
            # report. Re-derives the drift itself, so a file another process already refreshed
            # is left alone.
            refreshed, refresh_notes = _refresh_check_constraints(conn, engine)
            applied.extend(refreshed)
            note = "; ".join(part for part in (note, *refresh_notes) if part)
            if routine is None:
                _stamp_version(conn, SCHEMA_VERSION)
            conn.commit()
        except BaseException:
            conn.rollback()
            log.error(
                "schema migration %s -> %s FAILED and was rolled back; schema_version is still %s; "
                "pre-migration backup: %s",
                stored, SCHEMA_VERSION, stored, backup_path,
            )
            raise
        finally:
            if enforcing_fks:
                conn.exec_driver_sql("PRAGMA foreign_keys=ON")

    _enable_wal(engine)
    level = logging.INFO if stored >= 1 else logging.DEBUG  # a brand-new file is routine
    log.log(level, "schema_version %s -> %s: %d statement(s) applied", stored, SCHEMA_VERSION, len(applied))
    for sql in applied:
        log.log(level, "  %s", sql)
    LAST_REPORT = MigrationReport(stored, SCHEMA_VERSION, backup_path, applied, note)
    return LAST_REPORT


# ------------------------------------------------------- the maintenance command: build-indexes


def build_indexes(engine: Engine, *, take_backup: bool = False, backup_dir: Path | None = None, echo=print) -> list[str]:
    """Build every mapped index the file lacks, however large the table -- one index per
    transaction, each duration reported, a backup first only when asked. Safe to run twice:
    the second run finds nothing. This is what the startup warning points at (module
    docstring, "INDEX BUILDS AND THE WRITE LOCK"); it is never run by startup itself."""
    from noc_agents.db import models_all  # noqa: F401  every model module's indexes, like init_db

    with engine.connect() as conn:
        pending = _missing_index_sql(conn, engine, threshold=None)
        estimates = {t.name: _row_estimate(conn, t.name) for t in Base.metadata.sorted_tables if t.name in _user_tables(conn)}
    if not pending:
        echo("nothing to build: every mapped index exists")
        return []
    if take_backup:
        db_file = sqlite_file(engine)
        if db_file is not None:
            target = _backup(engine, db_file, backup_dir or default_backup_dir(engine), SCHEMA_VERSION, SCHEMA_VERSION)
            echo(f"backup written to {target}")
    built: list[str] = []
    for sql in pending:
        table = sql.split(" ON ", 1)[1].split(" ", 1)[0]
        echo(f"building: {sql}  (~{estimates.get(table, 0):,} rows) ...")
        started = time.perf_counter()
        with engine.connect() as conn:
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            conn.exec_driver_sql(sql)
            conn.commit()
        echo(f"  done in {time.perf_counter() - started:.1f}s")
        built.append(sql)
    echo(f"built {len(built)} index(es)")
    return built


def main(argv: list[str] | None = None) -> int:
    """``python -m noc_agents.db.migrate --build-indexes [--database-url URL] [--backup [--backup-dir DIR]]``"""
    parser = argparse.ArgumentParser(prog="python -m noc_agents.db.migrate", description=build_indexes.__doc__)
    parser.add_argument("--build-indexes", action="store_true", help="build every mapped index the database lacks")
    parser.add_argument("--database-url", help="defaults to DATABASE_URL / config, as the app resolves it")
    parser.add_argument("--backup", action="store_true", help="copy the database file first (an index can always be dropped; off by default)")
    parser.add_argument("--backup-dir", type=Path, help="where that copy goes; default <database folder>/backups")
    args = parser.parse_args(argv)
    if not args.build_indexes:
        parser.error("nothing to do: pass --build-indexes")
    from sqlalchemy import create_engine

    from noc_agents.config import get_settings

    url = args.database_url or get_settings().database_url
    engine = create_engine(url, future=True, connect_args={"check_same_thread": False, "timeout": 30} if url.startswith("sqlite") else {})
    try:
        build_indexes(engine, take_backup=args.backup, backup_dir=args.backup_dir)
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":  # pragma: no cover -- exercised through a subprocess in tests/unit/test_audit_index.py
    sys.exit(main())
