"""The nightly draft eval with the model on (spec §10.1 "Eval" row, §10.2) -- a clean skip otherwise.

Runs every sequence in ``tests/fixtures/eval/alarm_sequences.yaml`` with ``LLM_ENABLED=true``:
the executive briefs are drafted by the model through ``llm.assist.draft_exec_brief``, and the
run fails when

* any end-state grader fails -- the model must never move an INC number, priority, region,
  next-update time, HITL gate or cascade count (G6);
* the template-fallback rate is above the §1.2 M11 alarm (20 %);
* any draft fails a code grader (``draft_eval.DRAFT_GRADERS``).

pass^k is reported with k = ``EVAL_TRIALS`` (default 1); ``EVAL_MAX_MODEL_CALLS`` (default 100)
caps the calls one run may plan. The model judge is not built (MODEL-JUDGE SEAM in
``draft_eval.py``); nothing here calls a model to judge.

**Why this module skips in every suite run today.** ``tests/conftest.py`` pins
``LLM_ENABLED=false`` and blanks ``ANTHROPIC_API_KEY`` before any test module is imported, so
the flag this module checks is always off under pytest. The nightly entry point is
``python tests/eval/draft_eval.py``, which loads the same pins but keeps the shell's LLM
switches. No ``eval`` marker is registered in ``pyproject.toml``, hence the module-level
``skipif`` rather than a marker.
"""

from __future__ import annotations

import pytest

import draft_eval  # tests/eval/draft_eval.py (pytest puts this folder on sys.path)
from noc_agents.llm.client import llm_enabled

pytestmark = pytest.mark.skipif(
    not llm_enabled(),
    reason=(
        "nightly draft eval needs LLM_ENABLED=true; tests/conftest.py pins it to 'false' for the suite -- "
        "run `python tests/eval/draft_eval.py` with LLM_ENABLED=true and ANTHROPIC_API_KEY exported"
    ),
)


def test_model_drafts_pass_the_code_graders(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'unused.db').as_posix()}")
    report = draft_eval.run_suite(
        tmp_path,
        trials=draft_eval.trials_from_env(),
        max_model_calls=draft_eval.max_calls_from_env(),
    )
    print(report.format())

    assert report.llm_enabled
    assert not report.end_state_failures(), "\n".join(report.failure_lines())
    assert not report.fallback_alarm(), (
        f"template fallback {100.0 * report.fallback_rate():.1f} % is above the M11 alarm: "
        f"{dict(report.fallback()[2])}"
    )
    assert not report.draft_failures(), "\n".join(report.failure_lines())
