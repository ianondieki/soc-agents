"""The guards of the nightly draft-eval command, tested in the default suite (no model, no network).

The nightly eval itself is NOT a pytest test and pytest never runs it: ``tests/conftest.py`` pins
``LLM_ENABLED=false`` and blanks ``ANTHROPIC_API_KEY`` for the whole suite, so a model-drafted run
cannot happen under pytest. The nightly entry point is the command line::

    LLM_ENABLED=true ANTHROPIC_API_KEY=... python tests/eval/draft_eval.py [--trials k] [--json out.json]

What can and should run on every suite run is the behaviour that keeps that command honest and
cheap, and that is what this module pins:

* with the LLM layer off it exits 3 ("skipped") instead of silently grading the template and
  reporting a green nightly;
* it refuses (exit 2) to open anything under the repository's ``data`` folder;
* it refuses to plan more model calls than its cap, before it builds a single database. Since
  A-15 the assist path does write an ``llm_calls`` row, so ``LLM_MONTHLY_BUDGET_USD`` sees
  these calls as well; the cap is what stops a run BEFORE it spends, rather than the only brake.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import draft_eval  # tests/eval/draft_eval.py (pytest puts this folder on sys.path)
import noc_agents.llm.client as llm_client

SCRIPT = Path(draft_eval.__file__).resolve()


def _run_command(tmp_path: Path, *args: str, database_url: str | None = None) -> subprocess.CompletedProcess:
    """The real command in a child process, so nothing it pins leaks into this test session."""
    env = dict(os.environ)
    env.pop("LLM_ENABLED", None)  # the child reloads tests/conftest.py, which pins it to "false"
    env["DATABASE_URL"] = database_url or f"sqlite:///{(tmp_path / 'unused.db').as_posix()}"
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--tmp-dir", str(tmp_path), *args],
        cwd=draft_eval.ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
    )


def test_the_nightly_command_skips_with_exit_3_when_the_llm_layer_is_off(tmp_path):
    result = _run_command(tmp_path)
    assert result.returncode == 3, result.stdout + result.stderr
    assert "skipped" in result.stdout
    assert not list(tmp_path.glob("draft_eval_*")), "a skipped run must not build a work folder"


def test_the_nightly_command_refuses_a_database_under_the_data_folder(tmp_path):
    result = _run_command(tmp_path, "--template-only", database_url="sqlite:///./data/noc_agents.db")
    assert result.returncode == 2, result.stdout + result.stderr
    assert "data folder" in result.stderr


def test_the_model_call_cap_refuses_before_any_work(tmp_path, monkeypatch):
    # Pretend the layer is on. LLM_ENABLED itself stays "false" and ANTHROPIC_API_KEY stays blank
    # (tests/conftest.py), so even a broken cap could not reach a model: get_llm() cannot build a
    # client without a key.
    monkeypatch.setattr(llm_client, "llm_enabled", lambda: True)
    suite = draft_eval.load_suite()
    planned = draft_eval.planned_model_calls(suite.sequences, 3)
    assert planned > draft_eval.DEFAULT_MAX_MODEL_CALLS  # 36 incidents x 3 trials with today's fixture
    with pytest.raises(draft_eval.Refused, match="model calls planned"):
        draft_eval.run_suite(tmp_path, suite=suite, trials=3, max_model_calls=draft_eval.DEFAULT_MAX_MODEL_CALLS)
    assert not (tmp_path / "_template.db").exists(), "the cap must refuse before any database is built"
