"""``ix_audit_events_entity_action``: the index the scorecard shadow rule needs, and how it
reaches a file that already exists.

``db.models_scorecards.earlier_released_card_exists`` (§7.6.2) asks, for every card it checks,
whether a ``scorecard.published`` audit row exists for THAT card -- a correlated EXISTS on
``audit_events(entity_type, entity_id, action)``. ``audit_events`` had only a ``ts`` index, so
every check was a full scan of a table that retention keeps forever (measured ~1000x slower at
2M rows). The index is declared on ``AuditRow`` so a fresh file gets it, and -- because an index
holds no data -- ``db/migrate.py`` creates a missing one on EVERY start, without a version bump
and without a backup ("ON EVERY START" in its docstring).

Proven here: the query, exactly as the scorecard code builds it today, is answered from the
index (EXPLAIN QUERY PLAN); a current file that lacks the index gets it on a plain start,
with no backup and nothing else touched; the second start is a no-op; a fresh file has it.

And the other half (round 5): ``CREATE INDEX`` on a LARGE table holds the write lock for the
whole build -- two million audit rows took 75-150 s on the reference machine, failing every
other writer and crashing a second worker's start. So above ``INDEX_BUILD_AT_STARTUP_MAX_ROWS``
the build is deferred to ``python -m noc_agents.db.migrate --build-indexes``: startup warns and
carries on, the shadow-rule query still works (a scan, not a probe), and the command builds
the index, reports what it did, and is safe to run twice. Both paths are pinned with a small
threshold; the version-move path defers the same way.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.dialects import sqlite as sqlite_dialect

import noc_agents.db.models as models
from noc_agents.db import migrate
from noc_agents.db.migrate import BUILD_INDEXES_COMMAND, SCHEMA_VERSION, build_indexes, migrate_additive
from noc_agents.db.models import AuditRow, get_session, init_db
from noc_agents.db.models_scorecards import earlier_released_card_exists

INDEX = "ix_audit_events_entity_action"
CREATE = f"CREATE INDEX IF NOT EXISTS {INDEX} ON audit_events (entity_type, entity_id, action)"


@pytest.fixture()
def restore_db_globals():
    saved = (models._engine, models.SessionLocal)
    yield
    models._engine, models.SessionLocal = saved


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _sql(path: Path, sql: str, params: tuple = ()) -> list[tuple]:
    con = sqlite3.connect(path)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def _indexes(path: Path, table: str) -> dict[str, str | None]:
    return dict(_sql(path, "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name=?", (table,)))


class _Recorder:
    """An ``executor`` that keeps the statement instead of running it: the query under test is
    whatever the scorecard code hands over today, not a copy written here."""

    statement = None

    def execute(self, statement):
        self.statement = statement
        return self

    def scalar(self):
        return None


def _shadow_rule_sql() -> str:
    recorder = _Recorder()
    earlier_released_card_exists(recorder, operator_id="safaricom", vendor_id="vendor-1", period="2026-09", sla_terms_version="v1")
    return str(recorder.statement.compile(dialect=sqlite_dialect.dialect(), compile_kwargs={"literal_binds": True}))


def test_the_index_is_declared_on_the_model_in_the_predicates_order():
    ix = next(ix for ix in AuditRow.__table__.indexes if ix.name == INDEX)
    assert [c.name for c in ix.columns] == ["entity_type", "entity_id", "action"]


def test_the_shadow_rule_query_is_answered_from_the_index(tmp_path, restore_db_globals):
    db = tmp_path / "fresh.db"
    init_db(_url(db), backup_dir=tmp_path / "backups").dispose()
    sql = _shadow_rule_sql()
    assert "audit_events" in sql and "scorecard.published" in sql, sql

    con = sqlite3.connect(db)
    try:
        plan = [row[-1] for row in con.execute(f"EXPLAIN QUERY PLAN {sql}").fetchall()]
    finally:
        con.close()
    audit_steps = [step for step in plan if "audit_events" in step]
    assert audit_steps, plan
    assert all(f"INDEX {INDEX}" in step for step in audit_steps), plan  # SEARCH ... USING (COVERING) INDEX
    assert not any(step.startswith("SCAN audit_events") for step in plan), plan

    # And the control: without the index the same query scans the table.
    con = sqlite3.connect(db)
    try:
        con.execute(f"DROP INDEX {INDEX}")
        without = [row[-1] for row in con.execute(f"EXPLAIN QUERY PLAN {sql}").fetchall()]
        con.rollback()
    finally:
        con.close()
    assert any("SCAN audit_events" in step for step in without), without


def test_a_current_file_lacking_the_index_gets_it_on_a_plain_start_without_a_backup(tmp_path, restore_db_globals):
    """What an existing deployment sees: the file is at SCHEMA_VERSION, the model grew an index,
    nothing else changed. No bump, no backup -- one CREATE INDEX IF NOT EXISTS, then quiet."""
    db = tmp_path / "current.db"
    init_db(_url(db), backup_dir=tmp_path / "backups").dispose()
    con = sqlite3.connect(db)
    con.execute(f"DROP INDEX {INDEX}")
    con.execute("INSERT INTO audit_events (id, ts, operator_id, actor, action, entity_type, entity_id, rationale, payload_json) "
                "VALUES ('a-1', '2026-09-01 00:00:00', 'safaricom', 'Grace Wanjiru', 'scorecard.published', 'vendor_scorecard', 'card-1', '', '{}')")
    con.commit()
    con.close()
    assert INDEX not in _indexes(db, "audit_events")
    rows_before = _sql(db, "SELECT * FROM audit_events")

    engine = init_db(_url(db), backup_dir=tmp_path / "backups")
    report = migrate.LAST_REPORT
    try:
        assert (report.from_version, report.to_version) == (SCHEMA_VERSION, SCHEMA_VERSION)
        assert report.applied == [CREATE]
        assert report.backup_path is None and not (tmp_path / "backups").exists()
        assert "without a backup" in report.note
        assert _indexes(db, "audit_events")[INDEX] == CREATE.replace("IF NOT EXISTS ", "")
        assert _sql(db, "SELECT * FROM audit_events") == rows_before
        assert _sql(db, "SELECT version FROM schema_version") == [(SCHEMA_VERSION,)]
        # Second start: the quiet fast path.
        again = migrate_additive(engine, backup_dir=tmp_path / "backups")
        assert again.applied == [] and again.note == "schema already current"
    finally:
        engine.dispose()


AUDIT_ROW = (
    "INSERT INTO audit_events (id, ts, operator_id, actor, action, entity_type, entity_id, rationale, payload_json) "
    "VALUES ('a-{n}', '2026-09-01 00:00:0{n}', 'safaricom', 'Grace Wanjiru', 'scorecard.published', 'vendor_scorecard', 'card-{n}', '', '{{}}')"
)


def _file_without_the_index(tmp_path: Path, *, rows: int, stamp: int = SCHEMA_VERSION) -> Path:
    """A current (or, with ``stamp``, an older) file whose audit_events has rows and no index."""
    db = tmp_path / "noidx.db"
    init_db(_url(db), backup_dir=tmp_path / "scratch").dispose()
    con = sqlite3.connect(db)
    con.execute(f"DROP INDEX {INDEX}")
    for n in range(rows):
        con.execute(AUDIT_ROW.format(n=n))
    if stamp != SCHEMA_VERSION:
        con.execute("DELETE FROM schema_version")
        con.execute(f"INSERT INTO schema_version (version, applied_at) VALUES ({stamp}, '2026-09-21 00:00:00')")
    con.commit()
    con.close()
    assert INDEX not in _indexes(db, "audit_events")
    return db


def _shadow_rule_still_answers(db: Path) -> None:
    """The scorecard query works without the index -- slower, never wrong."""
    session = get_session()
    try:
        assert earlier_released_card_exists(session, operator_id="safaricom", vendor_id="vendor-1", period="2026-09", sla_terms_version="v1") is False
    finally:
        session.close()


def test_a_large_table_defers_the_build_to_the_maintenance_command(tmp_path, caplog, monkeypatch, restore_db_globals):
    monkeypatch.setattr(migrate, "INDEX_BUILD_AT_STARTUP_MAX_ROWS", 3)  # "large" is five rows here
    db = _file_without_the_index(tmp_path, rows=5)
    rows_before = _sql(db, "SELECT * FROM audit_events ORDER BY rowid")
    backups = tmp_path / "backups"

    with caplog.at_level(logging.WARNING, logger="noc_agents.db.migrate"):
        engine = init_db(_url(db), backup_dir=backups)
    report = migrate.LAST_REPORT
    try:
        # Startup did not build it -- no lock taken, nothing applied, no backup -- and said why.
        assert report.applied == [] and report.backup_path is None and not backups.exists()
        assert INDEX not in _indexes(db, "audit_events")
        warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warned) == 1, warned
        assert f"index {INDEX} on audit_events is missing and the table is large (~5 rows, above INDEX_BUILD_AT_STARTUP_MAX_ROWS=3)" in warned[0]
        assert BUILD_INDEXES_COMMAND in warned[0] and warned[0] in report.note
        _shadow_rule_still_answers(db)

        # The command the warning names builds it, says what it did, and is idempotent.
        said: list[str] = []
        built = build_indexes(engine, echo=said.append)
        assert built == [CREATE]
        assert any(f"building: {CREATE}  (~5 rows)" in line for line in said) and any(line.startswith("  done in ") for line in said)
        assert _indexes(db, "audit_events")[INDEX] == CREATE.replace("IF NOT EXISTS ", "")
        assert _sql(db, "SELECT * FROM audit_events ORDER BY rowid") == rows_before
        said.clear()
        assert build_indexes(engine, echo=said.append) == [] and said == ["nothing to build: every mapped index exists"]
        assert not backups.exists(), "no backup unless asked for"
    finally:
        engine.dispose()

    # The next start has nothing to say.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="noc_agents.db.migrate"):
        engine = init_db(_url(db), backup_dir=backups)
    try:
        assert migrate.LAST_REPORT.applied == [] and migrate.LAST_REPORT.note == "schema already current"
        assert not caplog.records
    finally:
        engine.dispose()


def test_the_command_line_entry_point_builds_and_reruns_cleanly(tmp_path, monkeypatch, restore_db_globals):
    db = _file_without_the_index(tmp_path, rows=5)
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"), "PYTHONIOENCODING": "utf-8"}
    env.pop("DATABASE_URL", None)  # the command must take the URL it is given, not the environment's

    first = subprocess.run([sys.executable, "-m", "noc_agents.db.migrate", "--build-indexes", "--database-url", _url(db), "--backup", "--backup-dir", str(tmp_path / "cmd_backups")],
                           env=env, capture_output=True, text=True, encoding="utf-8", timeout=600)
    assert first.returncode == 0, first.stderr[-2000:]
    assert f"building: {CREATE}" in first.stdout and "done in" in first.stdout and "built 1 index(es)" in first.stdout
    assert "backup written to" in first.stdout and len(list((tmp_path / "cmd_backups").glob(f"noidx.{SCHEMA_VERSION}-to-{SCHEMA_VERSION}.*.db"))) == 1
    assert INDEX in _indexes(db, "audit_events")

    second = subprocess.run([sys.executable, "-m", "noc_agents.db.migrate", "--build-indexes", "--database-url", _url(db)],
                            env=env, capture_output=True, text=True, encoding="utf-8", timeout=600)
    assert second.returncode == 0 and "nothing to build" in second.stdout
    assert len(list((tmp_path / "cmd_backups").iterdir())) == 1, "the rerun neither rebuilt nor backed up"

    # And with no action the command refuses rather than guessing.
    none = subprocess.run([sys.executable, "-m", "noc_agents.db.migrate", "--database-url", _url(db)], env=env, capture_output=True, text=True, encoding="utf-8", timeout=600)
    assert none.returncode == 2 and "pass --build-indexes" in none.stderr


def test_a_version_move_defers_a_large_index_the_same_way(tmp_path, caplog, monkeypatch, restore_db_globals):
    """The same threshold guards the additive pass: a release that adds an index to a table with
    millions of rows must not hold the migration's write lock for the build either."""
    monkeypatch.setattr(migrate, "INDEX_BUILD_AT_STARTUP_MAX_ROWS", 3)
    db = _file_without_the_index(tmp_path, rows=5, stamp=SCHEMA_VERSION - 1)
    with caplog.at_level(logging.WARNING, logger="noc_agents.db.migrate"):
        engine = init_db(_url(db), backup_dir=tmp_path / "backups")
    report = migrate.LAST_REPORT
    try:
        assert (report.from_version, report.to_version) == (SCHEMA_VERSION - 1, SCHEMA_VERSION)
        assert CREATE not in report.applied and INDEX not in _indexes(db, "audit_events")
        assert any(f"index {INDEX} on audit_events is missing and the table is large" in r.getMessage() for r in caplog.records)
        assert BUILD_INDEXES_COMMAND in report.note
        assert _sql(db, "SELECT MAX(version) FROM schema_version") == [(SCHEMA_VERSION,)], "the move itself went through"
        _shadow_rule_still_answers(db)
        assert build_indexes(engine, echo=lambda _line: None) == [CREATE]
    finally:
        engine.dispose()


def test_a_small_table_is_still_indexed_at_startup_under_the_threshold(tmp_path, monkeypatch, restore_db_globals):
    monkeypatch.setattr(migrate, "INDEX_BUILD_AT_STARTUP_MAX_ROWS", 3)
    db = _file_without_the_index(tmp_path, rows=3)  # max(rowid) == 3 == threshold: not above it
    engine = init_db(_url(db), backup_dir=tmp_path / "backups")
    try:
        assert migrate.LAST_REPORT.applied == [CREATE] and INDEX in _indexes(db, "audit_events")
    finally:
        engine.dispose()


def test_a_fresh_file_has_the_index(tmp_path, restore_db_globals):
    db = tmp_path / "fresh.db"
    init_db(_url(db), backup_dir=tmp_path / "backups").dispose()
    assert INDEX in _indexes(db, "audit_events")
    assert not any(s.startswith("CREATE INDEX IF NOT EXISTS " + INDEX) and " audit_events" not in s for s in migrate.LAST_REPORT.applied)
