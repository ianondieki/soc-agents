"""Generic additive schema migration for the SQLite database (spec §7.0.1).

The ORM classes in ``db/models.py`` are the single source of truth for the schema.
This module brings a database file up to that shape using only three statements:
``CREATE TABLE IF NOT EXISTS``, ``CREATE INDEX IF NOT EXISTS`` and
``ALTER TABLE ... ADD COLUMN``. It never drops, renames or retypes anything, which
is what makes the rollback story cheap: an older code version keeps running against
a newer file (extra tables and columns are ignored), so rolling back a release is a
code checkout, and the pre-migration backup is only for the day the *data* is damaged.

Bump ``SCHEMA_VERSION`` once per release that adds tables or columns. The version on
disk lives in the ``schema_version`` table (one row per applied version); a file
without that table is version 1 (every database created before Phase 1); an empty
file is version 0 and is simply created in full, with no backup.

Two SQLite facts shape the code:

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

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import Column, Table
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.schema import CreateIndex, CreateTable

from noc_agents.db.models import Base, SchemaVersionRow, utcnow

# 1 = pre-Phase-1 (no schema_version table); 2 = Phase 1 platform tables (outbox, scheduler,
# llm_calls, incident restore provenance); 3 = Phase 2 message tables (message_templates,
# delivery_receipts) and hitl_tasks.{run_id, entity_type, entity_id, created_by, edited};
# 4 = Phase 3 early-warning cache (external_signals, §7.3.1);
# 5 = Phase 4 accountability and learning: vendors + incident_clock_events (§7.6.1),
#     post_incident_reviews + pir_action_items (§7.7.1), regulatory_notifications +
#     evidence_packs (§7.6.1), and the known-error columns on problems (§7.7.1).
SCHEMA_VERSION = 5  # bump per release that adds tables/columns

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


def _plan(conn: Connection, engine: Engine) -> list[str]:
    """Every statement needed to bring the file up to Base.metadata, in dependency order.

    Reads the live catalogue (sqlite_master, PRAGMA table_info/index_list) so the list
    is exactly what will run -- the report and the startup log show real work only.
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
        present_idx = {r[1] for r in conn.exec_driver_sql(f"PRAGMA index_list({table.name})").fetchall()}
        for ix in indexes:
            if ix.name not in present_idx:
                statements.append(str(CreateIndex(ix, if_not_exists=True).compile(dialect=dialect)))
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


# ------------------------------------------------------------------------ entry point


def migrate_additive(engine: Engine, *, backup_dir: Path) -> MigrationReport:
    """1. Read schema_version (create the table if absent; treat missing as 1).
       2. If SCHEMA_VERSION > stored: copy the DB file to backup_dir/<db>.<stored>-to-<SCHEMA_VERSION>.<ts>.db
          BEFORE any change (sqlite3 backup API, works while WAL is active).
       3. In ONE transaction: for every table in Base.metadata.sorted_tables, CREATE TABLE IF NOT EXISTS;
          for each mapped column missing from PRAGMA table_info(<table>), ALTER TABLE ADD COLUMN with the
          column's DDL default (NULL or a literal). Never drops, renames or changes types.
       4. Write schema_version = SCHEMA_VERSION in the same transaction; commit; PRAGMA journal_mode=WAL.
       5. On any exception: rollback, leave schema_version unchanged, log the backup path, re-raise.
       Returns the list of applied statements for the startup log."""
    global LAST_REPORT

    if engine.dialect.name != "sqlite":
        # The additive path is SQLite-specific (PRAGMA, backup API); init_db() still
        # runs create_all() for other engines.
        LAST_REPORT = MigrationReport(SCHEMA_VERSION, SCHEMA_VERSION, None, note="not sqlite; create_all only")
        return LAST_REPORT

    db_file = sqlite_file(engine)
    with engine.connect() as conn:
        stored = _read_version(conn, _user_tables(conn))

    if stored >= SCHEMA_VERSION:
        # The common case -- every start after the first. No backup, no transaction.
        if stored > SCHEMA_VERSION:
            log.info("database schema_version %s is newer than this code (%s); additive schema, carrying on", stored, SCHEMA_VERSION)
        _enable_wal(engine)
        LAST_REPORT = MigrationReport(stored, SCHEMA_VERSION, None, note="schema already current")
        return LAST_REPORT

    backup_path: Path | None = None
    note = ""
    if stored == 0:
        note = "new database: created in full, nothing to back up"
    elif db_file is None:
        note = "in-memory database: nothing to back up"
    else:
        backup_path = _backup(engine, db_file, backup_dir, stored, SCHEMA_VERSION)
        log.info("schema_version %s -> %s: pre-migration backup written to %s", stored, SCHEMA_VERSION, backup_path)

    applied: list[str] = []
    with engine.connect() as conn:
        # pysqlite would not open a transaction before DDL; take the write lock now so
        # the plan, the DDL and the version stamp are one atomic unit (and a second
        # process starting at the same moment waits here, then finds nothing to do).
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            if _read_version(conn, _user_tables(conn)) >= SCHEMA_VERSION:
                conn.rollback()
                LAST_REPORT = MigrationReport(stored, SCHEMA_VERSION, backup_path, note="migrated by another process")
                return LAST_REPORT
            for sql in _plan(conn, engine):
                conn.exec_driver_sql(sql)
                applied.append(sql)
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

    _enable_wal(engine)
    level = logging.INFO if stored >= 1 else logging.DEBUG  # a brand-new file is routine
    log.log(level, "schema_version %s -> %s: %d statement(s) applied", stored, SCHEMA_VERSION, len(applied))
    for sql in applied:
        log.log(level, "  %s", sql)
    LAST_REPORT = MigrationReport(stored, SCHEMA_VERSION, backup_path, applied, note)
    return LAST_REPORT
