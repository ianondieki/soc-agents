"""Stage C9: SQLite connections wait for a busy lock instead of failing immediately."""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import text

import noc_agents.db.models as models
from noc_agents.db.models import get_session, init_db


@pytest.fixture()
def restore_db_globals():
    """init_db() rebinds the module-level engine/session factory; put the previous ones back."""
    saved = (models._engine, models.SessionLocal)
    yield
    models._engine, models.SessionLocal = saved


def test_init_db_passes_busy_timeout_to_sqlite(tmp_path, monkeypatch, restore_db_globals):
    seen: list[dict] = []
    real_connect = sqlite3.connect

    def spy(*args, **kwargs):
        seen.append(kwargs)
        return real_connect(*args, **kwargs)

    # The pysqlite dialect calls ``sqlite3.dbapi2.connect`` (looked up at call time), not the
    # ``sqlite3`` package re-export, so the spy has to live on the dbapi2 module.
    monkeypatch.setattr(sqlite3.dbapi2, "connect", spy)
    engine = init_db(f"sqlite:///{(tmp_path / 'timeout.db').as_posix()}")

    assert engine.dialect.name == "sqlite"
    assert seen, "init_db must open at least one connection (create_all + migration)"
    for kwargs in seen:
        assert kwargs["timeout"] == 30
        assert kwargs["check_same_thread"] is False

    session = get_session()
    try:
        assert session.execute(text("SELECT 1")).scalar() == 1
    finally:
        session.close()
