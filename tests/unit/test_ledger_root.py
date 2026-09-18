from __future__ import annotations

import os
from pathlib import Path

from noc_agents.graph import pipeline


def test_ledger_root_defaults_to_project_data_dir(monkeypatch):
    monkeypatch.delenv("LEDGER_DIR", raising=False)
    assert pipeline.ledger_root() == pipeline.ROOT / "data" / "shift_ledgers"


def test_ledger_root_honours_env_var(monkeypatch, tmp_path):
    monkeypatch.setenv("LEDGER_DIR", str(tmp_path / "ledgers"))
    assert pipeline.ledger_root() == tmp_path / "ledgers"


def test_ledger_root_ignores_blank_env_var(monkeypatch):
    monkeypatch.setenv("LEDGER_DIR", "   ")
    assert pipeline.ledger_root() == pipeline.ROOT / "data" / "shift_ledgers"


def test_test_session_ledger_dir_is_isolated():
    """The autouse conftest fixture must keep every test run out of the real ledger folder."""
    configured = Path(os.environ["LEDGER_DIR"]).resolve()
    assert configured == pipeline.ledger_root().resolve()
    assert configured != (pipeline.ROOT / "data" / "shift_ledgers").resolve()
