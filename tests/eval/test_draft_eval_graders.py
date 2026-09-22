"""Draft-eval graders and fixtures, proved on the deterministic template path (always on).

The nightly eval (``python tests/eval/draft_eval.py``; pytest never runs it) grades model
drafts. That is only worth anything if the fixture's expectations are right and the graders pass
what should pass and fail what should fail. This module proves both in the default suite, with
the LLM off:

* every sequence in ``tests/fixtures/eval/alarm_sequences.yaml`` is replayed through the real
  pipeline, every incident's brief is drafted through ``llm.assist.draft_exec_brief``'s template
  path, and every code grader passes -- so each hand-derived expectation is what the rules in
  code produce, and today's template is a draft the graders accept;
* each draft grader fails on a draft written to break exactly its rule, and each end-state
  grader fails on a wrong expectation -- so a green nightly cannot come from graders that never
  fail;
* the fixture is the shape the spec asks for, and its storm sequences are the demo rain storm,
  alarm for alarm.
"""

from __future__ import annotations

import dataclasses

from sqlalchemy import select

import draft_eval  # tests/eval/draft_eval.py (pytest puts this folder on sys.path)
from noc_agents.db.models import IncidentRow
from noc_agents.domain.schemas import EventIngest
from noc_agents.services.scenarios import RAIN_STORM_EVENTS


def test_every_sequence_passes_the_code_graders_on_the_template_path(tmp_path, monkeypatch):
    # The harness binds and restores DATABASE_URL itself; this makes the restore unconditional.
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'unused.db').as_posix()}")
    report = draft_eval.run_suite(tmp_path)

    assert not report.llm_enabled
    failures = report.failure_lines()
    assert not failures, "code graders failed on the template path:\n" + "\n".join(failures)
    expected_incidents = sum(len(r.sequence.expect) for r in report.results)
    assert len(report.drafts) == expected_incidents
    assert {d.source for d in report.drafts} == {"template"}
    assert {d.fallback_reason for d in report.drafts} == {draft_eval.TEMPLATE_OFF_REASON}
    assert report.pass_hat_k() == (expected_incidents, expected_incidents)
    # The one finding recorded and not scored is the documented one (see draft_eval's docstring).
    assert set(report.ungraded_counts()) <= set(draft_eval.UNGRADED_FINDINGS)


def test_each_grader_fails_on_the_mistake_it_exists_to_catch(tmp_db):
    settings, session = tmp_db
    cfg = settings.operator
    suite = draft_eval.load_suite()
    sequence = suite.by_id("golden-hub-power-nbi-e")
    result = draft_eval.run_sequence(session, settings, sequence, suite)
    assert not result.failures()
    inc = session.scalars(select(IncidentRow)).one()
    good = result.drafts[0].body

    def failed(body: str) -> set[str]:
        grades, _findings, _ungraded = draft_eval.grade_draft(inc, cfg, body, "control")
        return {g.grader for g in grades if not g.passed}

    # draft graders
    assert failed(good) == set()
    assert failed(good + "\nOutage due to POWER loss at the site.") == set()  # names the domain: not invented
    assert "no_invented_cause" in failed(good + "\nOutage due to a fibre cut on the ring.")
    assert "no_invented_cause" in failed(good + "\nThe site went down, caused by vandalism.")
    assert "draft_priority" in failed(good.replace("(P2)", "(P1)"))
    assert "draft_priority" in failed(good + "\nTreat this as P1.")
    assert "draft_incident_number" in failed(good.replace("INC000001", "INC000002"))
    assert "draft_incident_number" in failed(good + "\nSee also INC000042.")
    assert "draft_region" in failed(good.replace("Nairobi East", "the east"))
    assert "no_personal_data" in failed(good + "\nCall the FE on 0712 345 678.")
    assert "draft_length" in failed(good + "x" * 2001)
    assert "draft_length" in failed("   ")

    # end-state graders
    exp = sequence.expect[0]
    minutes = suite.next_update_minutes

    def failed_end(incidents, expected) -> set[str]:
        return {g.grader for g in draft_eval.grade_end_state(incidents, expected, cfg, minutes) if not g.passed}

    assert failed_end([inc], [exp]) == set()
    wrong = dataclasses.replace(
        exp,
        site_id="SFC-NBIE-HUB-EST",
        incident_number="INC000002",
        priority="P1",
        region_label="Coast",
        requires_hitl=False,
        child_sites_down=3,
    )
    assert failed_end([inc], [wrong]) == set(draft_eval.END_STATE_GRADERS) - {"next_update"}
    assert failed_end([], [exp]) == {"incident_set"}
    inc.next_update_at = None  # never flushed: autoflush is off and the fixture closes the session
    assert failed_end([inc], [exp]) == {"next_update"}


def test_the_fixture_has_the_spec_shape_and_the_storm_is_the_demo_storm():
    suite = draft_eval.load_suite()
    assert 20 <= len(suite.sequences) <= 50  # §10.2: "20-50 graded alarm sequences"
    assert (suite.operator, suite.autonomy_level) == ("safaricom", "L2_GUARDED")
    for sequence in suite.sequences:
        numbers = [e.incident_number for e in sequence.expect]
        assert numbers == [f"INC{n:06d}" for n in range(1, len(numbers) + 1)], sequence.id
        assert all(e.why.strip() for e in sequence.expect), sequence.id
    storm = [EventIngest(**e) for e in suite.by_id("storm-full-rain-storm").events]
    assert storm == RAIN_STORM_EVENTS
    assert [EventIngest(**e) for e in suite.by_id("storm-rift-nakuru-mw-cascade").events] == RAIN_STORM_EVENTS[0:3]
    assert [EventIngest(**e) for e in suite.by_id("storm-nbi-e-mw-ring-node").events] == RAIN_STORM_EVENTS[10:11]
