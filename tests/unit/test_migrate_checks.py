"""schema_version 9: the CHECK refresh of EMPTY scorecard tables (``db/migrate.py``, "THE SECOND
EXCEPTION").

The finding this closes: the scorecard lane tightened two CHECK constraints on
``vendor_scorecards`` -- ``ck_vendor_scorecards_shadow`` (a released card's reviewer must be a
human name, not whitespace or "system") and ``ck_vendor_scorecards_dq_counts``
(``dq_gate_threshold_pct < 100``) -- AFTER files had been created at schema_version 8. Nothing
additive can reach an existing table's constraints (``ADD COLUMN`` cannot add one,
``CREATE TABLE IF NOT EXISTS`` is a no-op, ``create_all`` never alters), so those files kept the
old CHECKs while every test passed on a fresh database. The dev database was such a file, with
zero rows.

Proven here:

1. a v8 file whose ``vendor_scorecards`` still has the OLD definition and no rows comes out with
   the model's definition -- and the new CHECKs really bite where the old ones accepted;
2. one with rows keeps every row byte for byte, keeps its old definition, starts, and says so
   once (a WARNING naming the CHECKs, the same text on the report);
3. a second start does nothing -- the DDL decides, not the version;
4. both tables drifted and empty: the dependent table is dropped first and created last, and
   its foreign key still points at the parent;
5. one table populated and the other drifted-and-empty: each gets its own treatment;
6. a v8 file whose definitions already match is only re-stamped.

The OLD definition is the literal ``sqlite_master.sql`` of the dev database (byte copy, read
only), which is what a v8 file created before the tightening carries.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.schema import CreateTable

import noc_agents.db.models as models
from noc_agents.db import migrate
from noc_agents.db.migrate import SCHEMA_VERSION, migrate_additive
from noc_agents.db.models import Base, init_db

CARDS = "vendor_scorecards"
LINES = "vendor_scorecard_lines"

# Verbatim from a v8 file created before the tightening (the dev database's byte copy). Both
# CHECKs below are the OLD ones: no reviewer-name rule, no upper bound on the threshold.
OLD_CARDS_DDL = (
    "CREATE TABLE vendor_scorecards ( id TEXT NOT NULL, operator_id TEXT NOT NULL, vendor_id TEXT NOT NULL, "
    "period TEXT NOT NULL, period_start DATETIME NOT NULL, period_end DATETIME NOT NULL, status TEXT DEFAULT 'DRAFT' "
    "NOT NULL, computed_at DATETIME NOT NULL, dispute_window_ends_at DATETIME, published_at DATETIME, finalised_at "
    "DATETIME, data_quality_json TEXT NOT NULL, dq_restored_incidents INTEGER NOT NULL, dq_inferred_restores INTEGER "
    "NOT NULL, dq_gate_threshold_pct REAL NOT NULL, discipline_json TEXT NOT NULL, shadow_required INTEGER DEFAULT 1 "
    "NOT NULL, shadow_reviewed_by TEXT, shadow_reviewed_at DATETIME, sla_terms_version TEXT NOT NULL, terms_json TEXT "
    "DEFAULT '{}' NOT NULL, narrative TEXT, narrative_ai_assisted INTEGER DEFAULT 0 NOT NULL, computed_by_run_id TEXT "
    "NOT NULL, PRIMARY KEY (id), CONSTRAINT uq_vendor_scorecards_operator_vendor_period UNIQUE (operator_id, "
    "vendor_id, period), CONSTRAINT ck_vendor_scorecards_status CHECK (status IN ('DRAFT', 'SHADOW', 'WITHHELD', "
    "'PUBLISHED', 'FINAL')), CONSTRAINT ck_vendor_scorecards_gate CHECK (status = 'WITHHELD' OR (dq_inferred_restores "
    "* 100.0 <= dq_gate_threshold_pct * dq_restored_incidents)), CONSTRAINT ck_vendor_scorecards_shadow CHECK (status "
    "NOT IN ('PUBLISHED', 'FINAL') OR shadow_required = 0 OR (shadow_reviewed_by IS NOT NULL AND "
    "length(trim(shadow_reviewed_by)) > 0)), CONSTRAINT ck_vendor_scorecards_shadow_flag CHECK (shadow_required IN "
    "(0, 1)), CONSTRAINT ck_vendor_scorecards_dq_counts CHECK (dq_restored_incidents >= 0 AND dq_inferred_restores >= "
    "0 AND dq_inferred_restores <= dq_restored_incidents) )"
)
OLD_CARDS_INDEXES = (
    "CREATE INDEX ix_vendor_scorecards_operator_id ON vendor_scorecards (operator_id)",
    "CREATE INDEX ix_vendor_scorecards_operator_period ON vendor_scorecards (operator_id, period)",
    "CREATE INDEX ix_vendor_scorecards_vendor_id ON vendor_scorecards (vendor_id)",
)
LINES_INDEXES = ("CREATE INDEX ix_vendor_scorecard_lines_scorecard_id ON vendor_scorecard_lines (scorecard_id)",)

# A card the OLD checks accept and the NEW ones would refuse: released, reviewed by "system".
OLD_ONLY_CARD = (
    "INSERT INTO vendor_scorecards (id, operator_id, vendor_id, period, period_start, period_end, status, computed_at, "
    "data_quality_json, dq_restored_incidents, dq_inferred_restores, dq_gate_threshold_pct, discipline_json, "
    "shadow_required, shadow_reviewed_by, shadow_reviewed_at, sla_terms_version, terms_json, narrative, "
    "narrative_ai_assisted, computed_by_run_id) VALUES ('card-1', 'safaricom', 'vendor-1', '2026-08', "
    "'2026-07-31 21:00:00.000000', '2026-08-31 21:00:00.000000', 'PUBLISHED', '2026-09-01 03:00:00.000000', '{}', 10, "
    "1, 10.0, '{}', 1, 'system', '2026-09-02 06:00:00.000000', 'v1', '{}', 'Ok-ish month — see lines', 0, 'run-1')"
)
A_LINE = (
    "INSERT INTO vendor_scorecard_lines (id, scorecard_id, seq, kpi, priority, raw_value, unit, band, "
    "eligible_incidents, excluded_incidents, scc_minutes_deducted, formula, yaml_path, evidence_json, credit_status) "
    "VALUES ('line-1', 'card-1', 0, 'MTTR_MIN', 'P1', 42.5, 'min', 'GREEN', 7, 1, 30, 'sum/count', 'sla.mttr', '{}', 'NONE')"
)


@pytest.fixture()
def restore_db_globals():
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


def _ddl(path: Path, table: str) -> str:
    return " ".join(_scalar(path, f"SELECT sql FROM sqlite_master WHERE type='table' AND name='{table}'").split())


def _model_ddl(table: str) -> str:
    return " ".join(str(CreateTable(Base.metadata.tables[table]).compile(dialect=create_engine("sqlite://").dialect)).split())


def _indexes(path: Path, table: str) -> set[str]:
    return {r[0] for r in _sql(path, "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?", (table,))}


def _snapshot(path: Path, table: str) -> list[tuple]:
    """Every column of every row with its storage class and bytes, by rowid."""
    columns = [r[1] for r in _sql(path, f"PRAGMA table_info({table})")]
    cells = ", ".join(f"{c}, typeof({c}), hex({c})" for c in columns)
    return _sql(path, f"SELECT rowid, {cells} FROM {table} ORDER BY rowid")


def _write(path: Path, *statements: str) -> None:
    con = sqlite3.connect(path)
    try:
        for s in statements:
            con.execute(s)
        con.commit()
    finally:
        con.close()


def _build_v8(tmp_path: Path, *, cards_sql: tuple[str, ...] = (), lines_ddl: str | None = None, lines_sql: tuple[str, ...] = ()) -> Path:
    """A schema_version 8 file whose vendor_scorecards has the OLD definition.

    Built from a current file: both scorecard tables are dropped and recreated -- the cards with
    the literal DDL a pre-tightening v8 release produced, the lines with the current DDL (it did
    not change) unless a drifted one is given -- rows are added by raw SQL, and the version is
    stamped back to 8. Reconstructing beats committing a binary: it cannot drift from models.py
    anywhere except the one place this test is about.
    """
    db = tmp_path / "v8.db"
    init_db(_url(db), backup_dir=tmp_path / "scratch").dispose()
    current_lines_ddl = _scalar(db, f"SELECT sql FROM sqlite_master WHERE type='table' AND name='{LINES}'")
    _write(
        db,
        f"DROP TABLE {LINES}",
        f"DROP TABLE {CARDS}",
        OLD_CARDS_DDL, *OLD_CARDS_INDEXES,
        lines_ddl or current_lines_ddl, *LINES_INDEXES,
        *cards_sql, *lines_sql,
        "DELETE FROM schema_version",
        "INSERT INTO schema_version (version, applied_at) VALUES (8, '2026-09-21 05:07:36')",
    )
    # Guard: the file is what a pre-tightening v8 release left behind, or nothing below proves anything.
    assert _ddl(db, CARDS) == OLD_CARDS_DDL and _ddl(db, CARDS) != _model_ddl(CARDS)
    assert "dq_gate_threshold_pct < 100" not in _ddl(db, CARDS) and "char(9)" not in _ddl(db, CARDS)
    assert (_ddl(db, LINES) == _model_ddl(LINES)) == (lines_ddl is None)
    assert _scalar(db, "SELECT MAX(version) FROM schema_version") == 8
    return db


def _new_checks_bite(path: Path) -> None:
    """The two tightened CHECKs refuse what the old definition accepted (raw SQL, like the finding)."""
    con = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="ck_vendor_scorecards_shadow"):
            con.execute(OLD_ONLY_CARD)  # PUBLISHED, reviewed by 'system'
        con.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="ck_vendor_scorecards_dq_counts"):
            con.execute(OLD_ONLY_CARD.replace("'system'", "'Grace Wanjiru'").replace(", 10.0, ", ", 100.0, "))
        con.rollback()
        con.execute(OLD_ONLY_CARD.replace("'system'", "'Grace Wanjiru'"))  # a human, under the bound: accepted
        con.rollback()
    finally:
        con.close()


# ================================================================ 1. empty: recreated from the model


def test_an_empty_v8_table_with_the_old_checks_is_recreated_from_the_model(tmp_path, restore_db_globals):
    db = _build_v8(tmp_path)
    old_lines_ddl = _ddl(db, LINES)
    backups = tmp_path / "backups"

    # Control: the OLD definition really does accept what the new one refuses.
    _write(db, OLD_ONLY_CARD, "DELETE FROM vendor_scorecards")

    engine = init_db(_url(db), backup_dir=backups)
    report = migrate.LAST_REPORT
    try:
        assert (report.from_version, report.to_version) == (8, SCHEMA_VERSION)
        assert _ddl(db, CARDS) == _model_ddl(CARDS), "the live definition is now the model's"
        assert _scalar(db, f"SELECT COUNT(*) FROM {CARDS}") == 0
        assert _indexes(db, CARDS) >= {
            "ix_vendor_scorecards_operator_id", "ix_vendor_scorecards_operator_period", "ix_vendor_scorecards_vendor_id",
        }
        _new_checks_bite(db)

        # Exactly the cards table was touched; the lines table (its DDL was already current) was not.
        destructive = [s for s in report.applied if s.startswith("DROP")]
        assert destructive == [f"DROP TABLE {CARDS}"]
        assert report.applied[report.applied.index(f"DROP TABLE {CARDS}") + 1].startswith(f"CREATE TABLE {CARDS} (")
        assert _ddl(db, LINES) == old_lines_ddl
        assert "vendor_scorecards" not in report.note

        # Backup first: it carries the OLD definition.
        written = sorted(backups.glob(f"v8.8-to-{SCHEMA_VERSION}.*.db"))
        assert len(written) == 1 and report.backup_path == written[0]
        assert _ddl(written[0], CARDS) == OLD_CARDS_DDL
        assert _scalar(db, "SELECT MAX(version) FROM schema_version") == SCHEMA_VERSION
        assert _scalar(db, "PRAGMA integrity_check") == "ok"
        assert _sql(db, "PRAGMA foreign_key_check") == []
    finally:
        engine.dispose()

    # And a second start does nothing at all: the DDL decides.
    engine = init_db(_url(db), backup_dir=backups)
    try:
        assert migrate.LAST_REPORT.applied == [] and migrate.LAST_REPORT.backup_path is None
        assert len(list(backups.iterdir())) == 1
    finally:
        engine.dispose()


# ============================================================ 2. populated: untouched, warned about


def test_a_populated_table_keeps_every_row_keeps_running_and_warns_once(tmp_path, caplog, restore_db_globals):
    db = _build_v8(tmp_path, cards_sql=(OLD_ONLY_CARD,), lines_sql=(A_LINE,))
    cards_before, lines_before = _snapshot(db, CARDS), _snapshot(db, LINES)
    backups = tmp_path / "backups"

    with caplog.at_level(logging.WARNING, logger="noc_agents.db.migrate"):
        engine = init_db(_url(db), backup_dir=backups)
    report = migrate.LAST_REPORT
    try:
        # Rows and definition exactly as they were; the version moved; the app is up.
        assert _snapshot(db, CARDS) == cards_before and _snapshot(db, LINES) == lines_before
        assert _ddl(db, CARDS) == OLD_CARDS_DDL
        assert not any(s.startswith("DROP") for s in report.applied)
        assert _scalar(db, "SELECT MAX(version) FROM schema_version") == SCHEMA_VERSION

        # Said once, naming exactly the CHECKs this file is not enforcing, in the log AND on the report.
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and CARDS in r.getMessage()]
        assert len(warnings) == 1, warnings
        assert "1 row(s)" in warnings[0]
        assert "ck_vendor_scorecards_dq_counts, ck_vendor_scorecards_shadow" in warnings[0]
        assert "ck_vendor_scorecards_gate" not in warnings[0], "an unchanged CHECK is not reported"
        assert "export the rows, empty the table, start once" in warnings[0]
        assert warnings[0] in report.note
    finally:
        engine.dispose()

    # Second start: nothing applied, no second backup -- and no second warning either.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="noc_agents.db.migrate"):
        engine = init_db(_url(db), backup_dir=backups)
    try:
        assert migrate.LAST_REPORT.applied == [] and migrate.LAST_REPORT.backup_path is None
        assert len(list(backups.iterdir())) == 1
        assert not [r for r in caplog.records if CARDS in r.getMessage()]
        assert _snapshot(db, CARDS) == cards_before
    finally:
        engine.dispose()

    # Doing what the warning says is enough: emptied, the next start recreates it from the model.
    _write(db, f"DELETE FROM {LINES}", f"DELETE FROM {CARDS}", "DELETE FROM schema_version WHERE version > 8")
    engine = init_db(_url(db), backup_dir=backups)
    try:
        assert f"DROP TABLE {CARDS}" in migrate.LAST_REPORT.applied
        assert _ddl(db, CARDS) == _model_ddl(CARDS)
        _new_checks_bite(db)
    finally:
        engine.dispose()


# ==================================================== 4. both drifted and empty: dependency order

# The lines table with one extra CHECK: a definition that differs from the model's.
DRIFTED_LINES_DDL = (
    "CREATE TABLE vendor_scorecard_lines ( id TEXT NOT NULL, scorecard_id TEXT NOT NULL, seq INTEGER DEFAULT 0 NOT "
    "NULL, kpi TEXT NOT NULL, priority TEXT, raw_value REAL, normalised_value REAL, region_multiplier_applied REAL, "
    "unit TEXT NOT NULL, band TEXT NOT NULL, eligible_incidents INTEGER NOT NULL, excluded_incidents INTEGER NOT NULL, "
    "scc_minutes_deducted INTEGER NOT NULL, formula TEXT NOT NULL, yaml_path TEXT NOT NULL, evidence_json TEXT DEFAULT "
    "'{}' NOT NULL, proposed_credit_pct REAL, credit_status TEXT DEFAULT 'NONE' NOT NULL, dispute_task_id TEXT, "
    "dispute_status TEXT, adjusted_value REAL, adjudicated_by TEXT, adjudication_reason TEXT, PRIMARY KEY (id), "
    "CONSTRAINT uq_vendor_scorecard_lines_card_kpi_priority UNIQUE (scorecard_id, kpi, priority), CONSTRAINT "
    "ck_lines_seq CHECK (seq >= 0), FOREIGN KEY(scorecard_id) REFERENCES vendor_scorecards (id) )"
)


def test_both_tables_drifted_and_empty_are_recreated_in_dependency_order(tmp_path, restore_db_globals):
    db = _build_v8(tmp_path, lines_ddl=DRIFTED_LINES_DDL)
    engine = init_db(_url(db), backup_dir=tmp_path / "backups")
    report = migrate.LAST_REPORT
    try:
        order = [s.split(" (")[0] for s in report.applied if s.startswith(("DROP TABLE", "CREATE TABLE vendor_scorecard"))]
        assert order == [f"DROP TABLE {LINES}", f"DROP TABLE {CARDS}", f"CREATE TABLE {CARDS}", f"CREATE TABLE {LINES}"], (
            "the dependent table goes first on the way down and last on the way up"
        )
        assert _ddl(db, CARDS) == _model_ddl(CARDS) and _ddl(db, LINES) == _model_ddl(LINES)
        assert "ck_lines_seq" not in _ddl(db, LINES)
        assert f"REFERENCES {CARDS} (id)" in _ddl(db, LINES), "the foreign key still points at the parent"
        assert _indexes(db, LINES) >= {"ix_vendor_scorecard_lines_scorecard_id"}
        assert _sql(db, "PRAGMA foreign_key_check") == [] and _scalar(db, "PRAGMA integrity_check") == "ok"
        assert report.note == ""
    finally:
        engine.dispose()


# ============================================= 5. one populated, the other drifted-and-empty


def test_a_populated_parent_is_warned_about_while_its_empty_drifted_child_is_recreated(tmp_path, caplog, restore_db_globals):
    db = _build_v8(tmp_path, cards_sql=(OLD_ONLY_CARD,), lines_ddl=DRIFTED_LINES_DDL)
    cards_before = _snapshot(db, CARDS)
    with caplog.at_level(logging.WARNING, logger="noc_agents.db.migrate"):
        engine = init_db(_url(db), backup_dir=tmp_path / "backups")
    report = migrate.LAST_REPORT
    try:
        assert [s for s in report.applied if s.startswith("DROP")] == [f"DROP TABLE {LINES}"]
        assert _ddl(db, LINES) == _model_ddl(LINES) and _ddl(db, CARDS) == OLD_CARDS_DDL
        assert _snapshot(db, CARDS) == cards_before
        assert f"{CARDS} has 1 row(s)" in report.note and LINES not in report.note
        warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warned) == 1 and warned[0].startswith(f"{CARDS} has 1 row(s)"), warned
    finally:
        engine.dispose()


# ====================================================== 6. already current: only the stamp moves


def test_a_v8_file_created_after_the_tightening_is_only_restamped(tmp_path, restore_db_globals):
    db = tmp_path / "current_v8.db"
    init_db(_url(db), backup_dir=tmp_path / "scratch").dispose()
    _write(db, "DELETE FROM schema_version", "INSERT INTO schema_version (version, applied_at) VALUES (8, '2026-09-21 12:00:00')")
    assert _ddl(db, CARDS) == _model_ddl(CARDS)

    engine = init_db(_url(db), backup_dir=tmp_path / "backups")
    report = migrate.LAST_REPORT
    try:
        assert (report.from_version, report.to_version) == (8, SCHEMA_VERSION)
        assert report.applied == [] and report.note == ""
        assert report.backup_path is not None, "a version move still takes its backup"
        assert _scalar(db, "SELECT MAX(version) FROM schema_version") == SCHEMA_VERSION
        assert migrate_additive(engine, backup_dir=tmp_path / "backups").applied == []
    finally:
        engine.dispose()


def test_a_fresh_database_is_created_current_and_never_refreshed(tmp_path, restore_db_globals):
    db = tmp_path / "fresh.db"
    engine = init_db(_url(db), backup_dir=tmp_path / "backups")
    try:
        assert migrate.LAST_REPORT.from_version == 0
        assert not any(s.startswith("DROP") for s in migrate.LAST_REPORT.applied)
        assert _ddl(db, CARDS) == _model_ddl(CARDS) and _ddl(db, LINES) == _model_ddl(LINES)
        _new_checks_bite(db)
    finally:
        engine.dispose()
