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
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy.dialects import sqlite as sqlite_dialect

import noc_agents.db.models as models
from noc_agents.db import migrate
from noc_agents.db.migrate import SCHEMA_VERSION, migrate_additive
from noc_agents.db.models import AuditRow, init_db
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


def test_a_fresh_file_has_the_index(tmp_path, restore_db_globals):
    db = tmp_path / "fresh.db"
    init_db(_url(db), backup_dir=tmp_path / "backups").dispose()
    assert INDEX in _indexes(db, "audit_events")
    assert not any(s.startswith("CREATE INDEX IF NOT EXISTS " + INDEX) and " audit_events" not in s for s in migrate.LAST_REPORT.applied)
