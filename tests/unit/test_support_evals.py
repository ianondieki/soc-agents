"""The support eval suite: metric arithmetic on a hand-computed synthetic set, the golden set's
validation, report storage, the three-split rules, and the regression gate: the contract's default
gates on dev + validation combined (the sets the desk is developed against). The blind holdout is
reported in every run and never asserted here: a held-out set that becomes a CI target stops
being held out."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from noc_agents.db.models import Base
from noc_agents.support.context import default_context
from noc_agents.support.evals import (
    EVAL_EXTRA_INCIDENTS,
    GOLDEN_PATH,
    HEADLINE_SPLIT,
    HOLDOUT_PATH,
    REGRESSION_SPLITS,
    SPLITS,
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


def test_an_empty_denominator_is_null_and_fails_its_gate_with_a_note():
    report = score([_r(1, "network", "resolver", article_ids=["A"], actual_route="resolver", article="A")], ctx=CTX)
    m = report["metrics"]
    assert m["wrong_escalation_rate"] is None and m["missed_escalation_rate"] is None  # no escalation happened
    assert m["safety_missed_escalation_rate"] is None and m["tool_accuracy"] is None
    assert m["resolution_rate"] == 1.0 and m["triage_accuracy"] == 1.0
    gates = {g["metric"]: g for g in report["gates"]}
    # No safety case does not mean no missed safety escalation: the gate fails and says why.
    assert gates["safety_missed_escalation_rate"]["passed"] is False and gates["safety_missed_escalation_rate"]["value"] is None
    assert "empty denominator" in gates["safety_missed_escalation_rate"]["note"]
    assert gates["wrong_escalation_rate"]["passed"] is False
    assert gates["resolution_rate"]["passed"] is True and gates["resolution_rate"]["note"] is None
    assert report["passed"] is False


# ------------------------------------------------------------------------- the golden set


def _write(tmp_path, lines):
    path = tmp_path / "golden.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
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
        ({"split": "test"}, "split"),
        ({"id": ""}, "id"),
    ],
)
def test_a_contradictory_golden_line_is_refused_with_its_line_number(tmp_path, overrides, message):
    with pytest.raises(GoldenSetError, match=message) as err:
        load_golden(_write(tmp_path, ["# header", _line(**overrides)]))
    assert "golden.jsonl:2" in str(err.value)


def test_a_contested_case_may_keep_a_bookkeeping_tool_but_is_never_scored(tmp_path):
    path = _write(tmp_path, [_line(), _line(id="x2", contested=True, expected={"route": "action", "tool": "lookup_account",
                                                                                 "article_ids": []})])
    cases, _ = load_golden(path)
    assert [(c.id, c.contested) for c in cases] == [("x1", False), ("x2", True)]
    report = run_eval(operator_id="safaricom", path=path)
    assert report["dataset"]["size"] == 1 and report["dataset"]["excluded"] == 1
    assert sum(map(sum, report["confusion"]["matrix"])) == 1
    with pytest.raises(GoldenSetError, match="tool"):  # not contested: a bookkeeping tool is still refused
        load_golden(_write(tmp_path, [_line(expected={"route": "action", "tool": "update_ticket", "article_ids": []})]))


def test_duplicate_ids_and_bad_json_are_refused_and_comments_are_skipped(tmp_path):
    with pytest.raises(GoldenSetError, match="duplicated"):
        load_golden(_write(tmp_path, [_line(), _line()]))
    with pytest.raises(GoldenSetError, match="duplicated"):  # across files too
        load_golden([_write(tmp_path / "a", [_line()]), _write(tmp_path / "b", [_line()])])
    with pytest.raises(GoldenSetError, match="not JSON"):
        load_golden(_write(tmp_path, ["{oops"]))
    cases, version = load_golden(_write(tmp_path, ["# c", "", _line()]))
    assert [c.id for c in cases] == ["x1"] and len(version) == 12


def test_the_golden_set_covers_every_route_every_reason_and_all_three_splits():
    cases, version = load_golden()
    assert {c.expected.route for c in cases} == set(ROUTES)
    assert {c.expected.escalation_reason for c in cases} - {None} == set(REASON_CODES)
    assert {c.split for c in cases} == {"dev", "validation", "holdout"}
    assert {c.language for c in cases} == {"en", "sw", "mixed"}
    assert len(cases) >= 30
    assert sum(len(load_golden(split=s)[0]) for s in SPLITS) == len(cases)
    assert load_golden(split="dev+validation")[0] == load_golden(split=("dev", "validation"))[0]
    # provenance: the holdout lives in its own file, and the version covers both files
    holdout_only, _ = load_golden(HOLDOUT_PATH)
    assert {c.split for c in holdout_only} == {"holdout"} and len(holdout_only) >= 90
    assert {c.split for c in load_golden(GOLDEN_PATH)[0]} == {"dev", "validation"}
    assert version != load_golden(GOLDEN_PATH)[1]
    assert [c.id for c in holdout_only if c.contested] == ["h-020", "h-054", "h-067", "h-068"]


@pytest.mark.parametrize("split", SPLITS)
def test_every_split_keeps_safety_cases_so_the_safety_gate_has_evidence(split):
    cases, _ = load_golden(split=split)
    reasons = {c.expected.escalation_reason for c in cases if c.expected.safety}
    assert reasons == {"fraud_or_sim_swap", "legal_or_regulator", "threat_or_safety"}
    assert sum(1 for c in cases if c.expected.safety) >= 3
    assert len([c for c in cases if c.expected.route in ("resolver", "action")]) >= 10  # resolution has a denominator


def test_a_full_run_reports_every_split_and_takes_the_blind_holdout_as_the_headline():
    report = run_eval(operator_id="safaricom")
    assert HEADLINE_SPLIT == "holdout" and report["dataset"]["split"] == "holdout"
    holdout = load_golden(split="holdout")[0]
    assert report["dataset"]["size"] == len([c for c in holdout if not c.contested])
    assert report["dataset"]["excluded"] == len([c for c in holdout if c.contested]) == 4
    assert set(report["by_split"]) == {"dev", "validation", "holdout"}
    assert report["by_split"]["holdout"] == report["metrics"]
    assert set(report["by_split"]["dev"]) == set(report["by_split"]["validation"]) == set(report["metrics"])
    assert {f["case_id"] for f in report["failures"]} <= {c.id for c in holdout if not c.contested}
    assert report["mode"] == "deterministic"
    assert report["dataset"]["name"] == "support_golden" and report["dataset"]["version"] == load_golden()[1]
    only_dev = run_eval(operator_id="safaricom", split="dev")
    assert only_dev["dataset"]["split"] == "dev" and set(only_dev["by_split"]) == {"dev"} and only_dev["dataset"]["excluded"] == 0


def test_a_golden_file_without_a_holdout_is_judged_on_all_of_it(tmp_path):
    report = run_eval(operator_id="safaricom", path=_write(tmp_path, [_line()]))
    assert report["dataset"]["split"] == "all" and report["dataset"]["size"] == 1
    assert set(report["by_split"]) == {"dev"}


def test_the_eval_template_holds_the_storm_and_the_adjudicated_incidents():
    from sqlalchemy import select

    from noc_agents.db.models import IncidentRow
    from noc_agents.support.evals import IsolatedDatabases

    databases = IsolatedDatabases("safaricom")
    try:
        with databases.session() as session:
            names = {row.site_name for row in session.scalars(select(IncidentRow)).all()}
    finally:
        databases.close()
    assert {"Nakuru Rift HUB", "Eldoret Rift HUB", "Thika Mt Kenya HUB", "Embakasi East Aggregation HUB"} <= names
    assert {name for _, name, *_ in EVAL_EXTRA_INCIDENTS} <= names
    assert [town for _, name, *_ in EVAL_EXTRA_INCIDENTS for town in ("Westlands", "Rongai", "Nyali", "Machakos") if town in name] \
        == ["Westlands", "Rongai", "Nyali", "Machakos"]


def test_the_regression_gate_dev_plus_validation_passes_the_contracts_default_gates():
    """The gate: the contract's default gates (config/support/policy.yaml) on the sets the desk is
    developed against, dev and validation combined. The holdout is reported, never asserted."""
    assert REGRESSION_SPLITS == ("dev", "validation")
    report = run_eval(operator_id="safaricom", split=REGRESSION_SPLITS)
    gates = {g["metric"]: g for g in report["gates"]}
    assert set(gates) == {"resolution_rate", "wrong_escalation_rate", "safety_missed_escalation_rate", "triage_accuracy"}
    assert gates["resolution_rate"]["threshold"] == 0.80 and gates["wrong_escalation_rate"]["threshold"] == 0.10
    assert gates["triage_accuracy"]["threshold"] == 0.85 and gates["safety_missed_escalation_rate"]["op"] == "=="
    assert report["dataset"]["split"] == "dev+validation"
    assert report["dataset"]["size"] == len(load_golden(split="dev")[0]) + len(load_golden(split="validation")[0])
    assert set(report["by_split"]) == {"dev", "validation"}
    assert report["passed"], json.dumps({"metrics": report["metrics"], "failures": report["failures"]}, indent=1)


def test_a_run_is_reproducible_case_for_case():
    first, second = run_eval(operator_id="safaricom", split="validation"), run_eval(operator_id="safaricom", split="validation")
    no_timing = lambda m: {k: v for k, v in m.items() if k != "p50_ms"}  # noqa: E731
    strip = lambda r: {k: v for k, v in r.items() if k not in ("run_id", "ran_at")} | {  # noqa: E731
        "metrics": no_timing(r["metrics"]), "by_split": {s: no_timing(m) for s, m in r["by_split"].items()}}
    assert strip(first) == strip(second)
    assert first["dataset"]["split"] == "validation"


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


def test_the_golden_paths_are_the_contracts():
    assert GOLDEN_PATH.as_posix().endswith("tests/fixtures/support_eval/golden.jsonl")
    assert HOLDOUT_PATH.as_posix().endswith("tests/fixtures/support_eval/holdout_blind.jsonl")


# ------------------------------------------------------------------------------- the CLI


def test_the_cli_compare_view_puts_every_split_side_by_side_with_the_top_failure_kinds():
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("support_eval_cli", GOLDEN_PATH.parents[2] / "eval" / "support_eval.py")
    cli = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("support_eval_cli", cli)
    spec.loader.exec_module(cli)
    base = {"mode": "deterministic", "dataset": {"name": "support_golden", "version": "v", "size": 10, "split": "dev", "excluded": 0}}
    dev = base | score(SYNTHETIC, ctx=CTX)
    one = score([_r(1, "network", "resolver", article_ids=["A"], actual_route="resolver", article="A")], ctx=CTX)
    validation = base | {"dataset": base["dataset"] | {"split": "validation", "size": 1}} | one
    holdout = base | {"dataset": base["dataset"] | {"split": "holdout", "size": 1, "excluded": 4}} | one
    regression = dev | {"dataset": base["dataset"] | {"split": "dev+validation", "size": 11}}
    text = cli.format_compare({"dev": dev, "validation": validation, "holdout": holdout}, regression)
    assert "10 (0)" in text and "1 (4)" in text and "resolution_rate" in text and "0.3333" in text
    assert "null" in text and "empty denominator" in text  # one answered case: no escalation to measure
    assert "regression gate (dev+validation, asserted by pytest)" in text and "never asserted" in text
    assert "wrong_escalation 2" in text and "wrong_article 1" in text
    assert text.endswith("FAILED")
    assert cli.failure_kind_counts(dev)[0] == ("wrong_escalation", 2)
    full = cli.format_report(dev | {"by_split": {"dev": dev["metrics"], "holdout": holdout["metrics"]}})
    assert "(headline: dev)" in full and "failures (dev split)" in full
    blind = cli.format_report(dev, failures=False)
    assert "c2" not in blind and "not shown" in blind
