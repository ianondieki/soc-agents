"""The support eval suite: metric arithmetic on a hand-computed synthetic set, the golden set's
validation, report storage, and the starter golden set against the contract's default gates."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from noc_agents.db.models import Base
from noc_agents.support.context import default_context
from noc_agents.support.evals import (
    GOLDEN_PATH,
    CaseResult,
    Expected,
    GoldenCase,
    GoldenSetError,
    failure_kinds,
    latest_report,
    load_golden,
    run_eval,
    score,
    store_report,
)
from noc_agents.support.vocab import REASON_CODES, ROUTES

CTX = default_context()


def _r(n, category, route, *, actual_route, actual_category=None, tool=None, article_ids=(), reason=None,
       got_tool=None, tool_status=None, article=None, got_reason=None, status=None):
    safety = reason in ("fraud_or_sim_swap", "legal_or_regulator", "threat_or_safety")
    case = GoldenCase(id=f"c{n}", split="dev", text=f"case {n}", msisdn="0700000000", language="en",
                      expected=Expected(category, route, tool, tuple(article_ids), reason, safety))
    default_status = {"resolver": "answered", "action": "action_taken", "human": "escalated"}[actual_route]
    return CaseResult(case=case, category=actual_category or category, route=actual_route, status=status or default_status,
                      tool=got_tool, tool_status=tool_status, article_id=article, reason_code=got_reason, ms=float(n))


#: Ten cases whose every metric is worked out by hand in the comments below.
SYNTHETIC = [
    _r(1, "network", "resolver", article_ids=["A"], actual_route="resolver", article="A"),                    # correct
    _r(2, "network", "resolver", article_ids=["A"], actual_route="resolver", article="B"),                    # wrong article
    _r(3, "network", "resolver", article_ids=["A"], actual_route="human", got_reason="not_grounded"),         # wrong escalation
    _r(4, "mpesa", "action", tool="T", actual_route="action", got_tool="T", tool_status="ok"),                # correct
    _r(5, "mpesa", "action", tool="T", actual_route="human", got_tool="T", tool_status="refused",
       got_reason="tool_failed"),                                                                              # wrong escalation
    _r(6, "sim_and_fraud", "human", reason="fraud_or_sim_swap", actual_route="human", got_reason="fraud_or_sim_swap"),
    _r(7, "sim_and_fraud", "human", reason="legal_or_regulator", actual_route="resolver", article="A"),       # missed (safety)
    _r(8, "other", "human", reason="low_confidence", actual_route="human", got_reason="low_confidence"),
    _r(9, "billing", "human", tool="T2", reason="over_refund_limit", actual_route="human", got_tool="T2",
       tool_status="needs_approval", got_reason="over_refund_limit", status="awaiting_approval"),
    _r(10, "data_bundles", "action", tool="T", actual_route="action", actual_category="billing", got_tool="T3",
       tool_status="ok"),                                                                                      # wrong category + tool
]


def test_the_metrics_on_the_synthetic_set_match_the_hand_computation():
    m = score(SYNTHETIC, ctx=CTX)["metrics"]
    assert m["resolution_rate"] == 0.3333          # resolvable 1,2,3,4,5,10; correct 1,4 -> 2/6
    assert m["wrong_escalation_rate"] == 0.4       # escalated 3,5,6,8,9; resolvable among them 3,5 -> 2/5
    assert m["missed_escalation_rate"] == 0.25     # gold human 6,7,8,9; kept 7 -> 1/4
    assert m["safety_missed_escalation_rate"] == 0.5  # safety 6,7; kept 7 -> 1/2
    assert m["containment_rate"] == 0.5            # not escalated 1,2,4,7,10 -> 5/10
    assert m["triage_accuracy"] == 0.9             # only 10 is mis-categorised
    assert m["routing_accuracy"] == 0.7            # 3, 5 and 7 are on the wrong route
    assert m["grounded_answer_rate"] == 0.3333     # answers 1,2,7; gold article cited only in 1
    assert m["tool_accuracy"] == 0.75              # gold tool on 4,5,9,10; right call on 4,5,9
    assert m["escalation_reason_accuracy"] == 0.75  # gold human 6,7,8,9; right reason on 6,8,9
    assert m["p50_ms"] == 5.5                      # median of 1..10


def test_the_confusion_matrix_has_expected_rows_and_actual_columns():
    assert score(SYNTHETIC, ctx=CTX)["confusion"] == {
        "labels": ["resolver", "action", "human"],
        "matrix": [[2, 0, 1], [0, 2, 1], [1, 0, 3]],
    }


def test_failures_name_every_kind_each_case_shows():
    report = score(SYNTHETIC, ctx=CTX)
    got = [(f["case_id"], f["kind"]) for f in report["failures"]]
    assert got == [("c2", "wrong_article"), ("c3", "wrong_escalation"), ("c5", "wrong_escalation"),
                   ("c7", "missed_escalation"), ("c10", "wrong_category"), ("c10", "wrong_tool")]
    first = report["failures"][0]
    assert first["expected"]["article_ids"] == ["A"] and first["actual"]["article_id"] == "B"
    unresolved = _r(11, "mpesa", "action", tool="T", actual_route="action", got_tool="T", tool_status="failed")
    assert failure_kinds(unresolved) == ["unresolved"]
    wrong_route = _r(12, "mpesa", "resolver", article_ids=["A"], actual_route="action", got_tool="T", tool_status="ok")
    assert failure_kinds(wrong_route) == ["wrong_route"]


def test_gates_and_the_per_category_table():
    report = score(SYNTHETIC, ctx=CTX)
    assert [(g["metric"], g["passed"]) for g in report["gates"]] == [
        ("resolution_rate", False), ("wrong_escalation_rate", False),
        ("safety_missed_escalation_rate", False), ("triage_accuracy", True)]
    assert report["passed"] is False
    rows = {row["category"]: row for row in report["by_category"]}
    assert rows["network"] == {"category": "network", "n": 3, "resolution_rate": 0.3333,
                               "wrong_escalation_rate": 1.0, "triage_accuracy": 1.0}
    assert rows["sim_and_fraud"]["resolution_rate"] is None  # nothing resolvable: null, not a misleading 0 %
    assert rows["sim_and_fraud"]["wrong_escalation_rate"] == 0.0
    assert rows["data_bundles"] == {"category": "data_bundles", "n": 1, "resolution_rate": 0.0,
                                    "wrong_escalation_rate": None, "triage_accuracy": 0.0}
    assert "roaming" not in rows


def test_an_empty_denominator_is_zero_in_the_headline_metrics():
    m = score([_r(1, "network", "resolver", article_ids=["A"], actual_route="resolver", article="A")], ctx=CTX)["metrics"]
    assert m["wrong_escalation_rate"] == 0.0 and m["missed_escalation_rate"] == 0.0 and m["resolution_rate"] == 1.0


# ------------------------------------------------------------------------- the golden set


def _write(tmp_path, lines):
    path = tmp_path / "golden.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _line(**overrides):
    case = {"id": "x1", "split": "dev", "text": "My calls keep dropping", "msisdn": "0711000001", "language": "en",
            "expected": {"category": "network", "route": "resolver", "tool": None,
                         "article_ids": ["KB-NETWORK-CALL-DROPS"], "escalation_reason": None, "safety": False}}
    expected = overrides.pop("expected", {})
    case.update(overrides)
    case["expected"].update(expected)
    return json.dumps(case)


@pytest.mark.parametrize(
    "overrides,message",
    [
        ({"expected": {"route": "human"}}, "exactly when escalation_reason"),
        ({"expected": {"route": "human", "escalation_reason": "fraud_or_sim_swap", "article_ids": []}}, "safety"),
        ({"expected": {"article_ids": []}}, "accepted article"),
        ({"expected": {"route": "action", "article_ids": []}}, "needs its tool"),
        ({"expected": {"category": "weather"}}, "category"),
        ({"expected": {"tool": "lookup_account"}}, "tool"),
        ({"split": "train"}, "split"),
        ({"id": ""}, "id"),
    ],
)
def test_a_contradictory_golden_line_is_refused_with_its_line_number(tmp_path, overrides, message):
    with pytest.raises(GoldenSetError, match=message) as err:
        load_golden(_write(tmp_path, ["# header", _line(**overrides)]))
    assert "golden.jsonl:2" in str(err.value)


def test_duplicate_ids_and_bad_json_are_refused_and_comments_are_skipped(tmp_path):
    with pytest.raises(GoldenSetError, match="duplicated"):
        load_golden(_write(tmp_path, [_line(), _line()]))
    with pytest.raises(GoldenSetError, match="not JSON"):
        load_golden(_write(tmp_path, ["{oops"]))
    cases, version = load_golden(_write(tmp_path, ["# c", "", _line()]))
    assert [c.id for c in cases] == ["x1"] and len(version) == 12


def test_the_starter_golden_set_covers_every_route_every_reason_and_both_splits():
    cases, _ = load_golden()
    assert {c.expected.route for c in cases} == set(ROUTES)
    assert {c.expected.escalation_reason for c in cases} - {None} == set(REASON_CODES)
    assert {c.split for c in cases} == {"dev", "test"}
    assert {c.language for c in cases} == {"en", "sw", "mixed"}
    assert len(cases) >= 30
    assert len(load_golden(split="test")[0]) + len(load_golden(split="dev")[0]) == len(cases)


def test_the_starter_golden_set_passes_the_contracts_default_gates():
    report = run_eval(operator_id="safaricom")
    gates = {g["metric"]: g for g in report["gates"]}
    assert set(gates) == {"resolution_rate", "wrong_escalation_rate", "safety_missed_escalation_rate", "triage_accuracy"}
    assert gates["resolution_rate"]["threshold"] == 0.80 and gates["wrong_escalation_rate"]["threshold"] == 0.10
    assert gates["triage_accuracy"]["threshold"] == 0.85 and gates["safety_missed_escalation_rate"]["op"] == "=="
    assert report["passed"], json.dumps(report["failures"], indent=1)
    assert report["mode"] == "deterministic" and report["dataset"]["size"] == len(load_golden()[0])
    assert report["dataset"]["name"] == "support_golden" and report["dataset"]["version"] == load_golden()[1]


def test_a_run_is_reproducible_case_for_case():
    first, second = run_eval(operator_id="safaricom", split="test"), run_eval(operator_id="safaricom", split="test")
    strip = lambda r: {k: v for k, v in r.items() if k not in ("run_id", "ran_at")} | {  # noqa: E731
        "metrics": {k: v for k, v in r["metrics"].items() if k != "p50_ms"}}
    assert strip(first) == strip(second)
    assert first["dataset"]["split"] == "test"


def test_reports_are_stored_per_operator_and_the_latest_wins(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'evals.db'}")
    from noc_agents.db import models_all  # noqa: F401

    Base.metadata.create_all(engine)
    with Session(engine) as session:
        assert latest_report(session, "safaricom") is None
        report = score(SYNTHETIC, ctx=CTX) | {"run_id": "r1", "ran_at": "x", "mode": "deterministic",
                                               "dataset": {"name": "support_golden", "version": "v", "size": 10}}
        store_report(session, "safaricom", report)
        session.commit()
        store_report(session, "safaricom", report | {"run_id": "r2"})
        session.commit()
        assert latest_report(session, "safaricom")["run_id"] == "r2"
        assert latest_report(session, "airtel") is None


def test_the_golden_path_is_the_contracts():
    assert GOLDEN_PATH.as_posix().endswith("tests/fixtures/support_eval/golden.jsonl")
