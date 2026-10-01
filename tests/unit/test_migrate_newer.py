"""A file NEWER than this code -- the rollback case -- is left exactly as it is.

``db/migrate.py`` promises that rolling back a release is a code checkout: the schema is
additive, so older code keeps running against a newer file. Round 4 broke that promise for
one table: the every-start drift comparison ran on ``stored > SCHEMA_VERSION`` too, and since
"different from the model" cannot tell a NEWER definition from an OLDER one, this code took a
``10-to-9`` backup and dropped an empty ``vendor_scorecards`` that a v10 release had given a
column -- or, when the newer column had an index, crashed on every start replaying it, one
backup per attempt. The decision now: a newer file gets a WARNING and nothing else -- no index,
no CHECK refresh, no backup, no transaction. Refusing to start was the alternative; it was not
taken because the schema IS additive and older code genuinely works on the newer file, which
is the whole point of the rollback promise.

Proven here for four newer-file shapes: the bytes of the database file are identical after a
start, the report says why, the newer objects survive, and the app can read and write.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from pathlib import Path

import pytest

import noc_agents.db.models as models
from noc_agents.db import migrate
from noc_agents.db.migrate import SCHEMA_VERSION
from noc_agents.db.models import AuditRow, IncidentRow, get_session, init_db
from test_migrate_checks import DRIFTED_LINES_DDL, LINES_INDEXES

NEWER = SCHEMA_VERSION + 1

# What a hypothetical v10 release might have done to the file, the additive way (what _plan emits).
SHAPES = {
    "column_added": ("ALTER TABLE vendor_scorecards ADD COLUMN vendor_ack_by VARCHAR(128)",),
    "column_and_index_added": (
        "ALTER TABLE vendor_scorecards ADD COLUMN vendor_ack_by VARCHAR(128)",
        "CREATE INDEX ix_vendor_scorecards_vendor_ack_by ON vendor_scorecards (vendor_ack_by)",
    ),
    # a definition this code would call "drifted" if it dared to compare
    "lines_table_redefined": ("DROP TABLE vendor_scorecard_lines", DRIFTED_LINES_DDL, *LINES_INDEXES),
    # a mapped index this code has and the newer file (for whatever reason) does not: NOT created
    "mapped_index_absent": ("DROP INDEX ix_audit_events_entity_action",),
}


@pytest.fixture()
def restore_db_globals():
    saved = (models._engine, models.SessionLocal)
    yield
    models._engine, models.SessionLocal = saved


def _url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def _sql(path: Path, sql: str) -> list[tuple]:
    con = sqlite3.connect(path)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build_newer(tmp_path: Path, shape: str) -> Path:
    db = tmp_path / "newer.db"
    init_db(_url(db), backup_dir=tmp_path / "scratch").dispose()
    con = sqlite3.connect(db)
    try:
        for statement in SHAPES[shape]:
            con.execute(statement)
        con.execute(f"INSERT INTO schema_version (version, applied_at) VALUES ({NEWER}, '2026-10-01 00:00:00')")
        con.commit()
    finally:
        con.close()
    assert _sql(db, "SELECT MAX(version) FROM schema_version") == [(NEWER,)]
    return db


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_a_newer_file_is_left_byte_identical_and_the_app_starts(tmp_path, caplog, restore_db_globals, shape):
    db = _build_newer(tmp_path, shape)
    before = _sha(db)
    catalogue_before = _sql(db, "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name")
    backups = tmp_path / "backups"

    with caplog.at_level(logging.WARNING, logger="noc_agents.db.migrate"):
        engine = init_db(_url(db), backup_dir=backups)
    report = migrate.LAST_REPORT
    try:
        assert (report.from_version, report.to_version) == (NEWER, SCHEMA_VERSION)
        assert report.applied == [] and report.backup_path is None and not backups.exists()
        assert "newer than this code" in report.note
        warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warned) == 1 and "NEWER than this code" in warned[0] and "no index, no CHECK refresh, no backup" in warned[0]

        assert _sha(db) == before, "a start on a newer file changed the database file"
        assert _sql(db, "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name") == catalogue_before
        assert _sql(db, "SELECT version FROM schema_version ORDER BY version") == [(SCHEMA_VERSION,), (NEWER,)]

        # ...and this older code really does run against it: a read and a write through the ORM.
        session = get_session()
        try:
            assert session.query(IncidentRow).count() == 0
            session.add(AuditRow(operator_id="safaricom", actor="Grace Wanjiru", action="rollback.smoke", entity_type="test", entity_id="1"))
            session.commit()
            assert session.query(AuditRow).filter_by(action="rollback.smoke").count() == 1
        finally:
            session.close()
    finally:
        engine.dispose()

    # The second start is the same: still nothing, still the warning, no backup ever.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="noc_agents.db.migrate"):
        engine = init_db(_url(db), backup_dir=backups)
    try:
        assert migrate.LAST_REPORT.applied == [] and migrate.LAST_REPORT.backup_path is None and not backups.exists()
        assert sum("NEWER than this code" in r.getMessage() for r in caplog.records) == 1
        assert _sql(db, "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name") == catalogue_before
    finally:
        engine.dispose()
