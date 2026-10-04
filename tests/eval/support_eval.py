"""Support desk evals from the command line: the golden set, the metrics, the gates.

The harness itself is ``noc_agents.support.evals`` (the API's ``POST /api/v1/support/evals/run``
calls the same function); this file is the command-line entry point the contract names and
the nightly LLM comparison runs. Definitions of every metric are in that module's docstring.

Usage::

    python tests/eval/support_eval.py                      # every case; the headline is the blind HOLDOUT
    python tests/eval/support_eval.py --split dev          # one split
    python tests/eval/support_eval.py --split dev+validation   # the regression gate's splits, combined
    python tests/eval/support_eval.py --compare            # dev, validation and holdout side by side
    python tests/eval/support_eval.py --json out.json      # the full EvalReport as JSON (per split with --compare)
    python tests/eval/support_eval.py --no-failures        # metrics and gates only (a blind measurement)
    python tests/eval/support_eval.py --llm                # nightly: LLM tie-breaks on (needs LLM_ENABLED)

Every run is isolated: each case gets its own in-memory database (the live database and the
live event hub are never touched). ``--llm`` passes the configured model port to triage, which
uses it only to break a low-confidence tie. The golden texts are synthetic and the numbers
fictional, so no customer data leaves; the run still spends from the same monthly LLM budget
as the app. pytest never runs ``--llm``: ``tests/conftest.py`` pins ``LLM_ENABLED=false``.

Exit codes: 0 = every gate passed on the headline split (the holdout by default; with
``--compare``, the regression splits dev+validation); 1 = a gate failed; 2 = bad usage or a
malformed golden set; 3 = ``--llm`` asked for but the LLM layer is off.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from noc_agents.config import get_settings  # noqa: E402
from noc_agents.support.evals import (  # noqa: E402
    GOLDEN_PATHS,
    REGRESSION_SPLITS,
    SPLITS,
    GoldenSetError,
    run_eval,
)


def _fmt(value: object) -> str:
    return "null" if value is None else str(value)


def _gate_lines(report: dict) -> list[str]:
    lines = []
    for gate in report["gates"]:
        mark = "PASS" if gate["passed"] else "FAIL"
        line = f"  [{mark}] {gate['metric']} {gate['op']} {gate['threshold']}  (got {_fmt(gate['value'])})"
        if gate.get("note"):
            line += f"\n         note: {gate['note']}"
        lines.append(line)
    return lines


def _dataset_line(report: dict) -> str:
    ds = report["dataset"]
    excluded = f"  excluded={ds['excluded']}" if ds.get("excluded") else ""
    return f"support desk eval  {report['mode']}  {ds['name']}@{ds['version']}  split={ds['split']}  n={ds['size']}{excluded}"


def format_report(report: dict, *, failures: bool = True) -> str:
    """The human summary: dataset, metrics (per split when more than one ran), gates, confusion, failures."""
    ds = report["dataset"]
    lines = [_dataset_line(report), ""]
    by_split = report.get("by_split") or {}
    if len(by_split) > 1:
        names = [s for s in SPLITS if s in by_split]
        lines.append(f"  {'metric':<32} " + " ".join(f"{n:>11}" for n in names) + "   (headline: " + ds["split"] + ")")
        for name in report["metrics"]:
            lines.append(f"  {name:<32} " + " ".join(f"{_fmt(by_split[s].get(name)):>11}" for s in names))
    else:
        lines += [f"  {name:<32} {_fmt(value)}" for name, value in report["metrics"].items()]
    lines.append("")
    lines += _gate_lines(report)
    labels = report["confusion"]["labels"]
    lines += ["", "  confusion (rows expected, cols actual): " + " ".join(f"{label:>8}" for label in labels)]
    for label, row in zip(labels, report["confusion"]["matrix"], strict=True):
        lines.append(f"  {label:>39} " + " ".join(f"{n:>8}" for n in row))
    if report["failures"] and failures:
        lines += ["", f"  failures ({ds['split']} split):"]
        lines += [f"    {f['case_id']}: {f['kind']}  expected {f['expected']['route']}/{f['expected']['category']}"
                  f"  got {f['actual']['route']}/{f['actual']['category']}" for f in report["failures"]]
    elif report["failures"]:
        lines += ["", f"  failures ({ds['split']} split): {len(report['failures'])} (not shown: --no-failures)"]
    lines += ["", "PASSED" if report["passed"] else "FAILED"]
    return "\n".join(lines)


def failure_kind_counts(report: dict) -> list[tuple[str, int]]:
    """Failure kinds in a report, most frequent first."""
    return Counter(f["kind"] for f in report["failures"]).most_common()


def format_compare(reports: dict[str, dict], regression: dict, *, top: int = 5) -> str:
    """Every split side by side; the regression gate (dev+validation, asserted by pytest); the holdout's
    gates (reported, never asserted); the top failure kinds of each split."""
    names = [s for s in SPLITS if s in reports]
    first = next(iter(reports.values()))
    ds = first["dataset"]
    lines = [f"support desk eval  {first['mode']}  {ds['name']}@{ds['version']}  compare", "",
             f"  {'metric':<32} " + " ".join(f"{n:>11}" for n in names)]
    lines.append(f"  {'n (excluded)':<32} " + " ".join(
        f"{str(reports[n]['dataset']['size']) + ' (' + str(reports[n]['dataset'].get('excluded', 0)) + ')':>11}" for n in names))
    for name in first["metrics"]:
        lines.append(f"  {name:<32} " + " ".join(f"{_fmt(reports[n]['metrics'].get(name)):>11}" for n in names))
    lines += ["", f"  regression gate ({regression['dataset']['split']}, asserted by pytest):"] + _gate_lines(regression)
    if "holdout" in reports:
        lines += ["", "  holdout gates (reported, never asserted: a held-out set is not a CI target):"]
        lines += _gate_lines(reports["holdout"])
    lines += ["", f"  top failure kinds (up to {top}):"]
    for name in names:
        kinds = failure_kind_counts(reports[name])[:top]
        lines.append(f"    {name:<11} " + (", ".join(f"{kind} {n}" for kind, n in kinds) if kinds else "none"))
    lines += ["", "PASSED" if regression["passed"] else "FAILED"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the support desk eval suite.")
    parser.add_argument("--split", default=None,
                        help="one split or several joined with '+' (dev, validation, holdout); default: all, headline = holdout")
    parser.add_argument("--compare", action="store_true", help="run every split and print them side by side")
    parser.add_argument("--golden", type=Path, action="append", default=None,
                        help="golden set path (JSONL); repeatable; default: golden.jsonl + holdout_blind.jsonl")
    parser.add_argument("--json", type=Path, default=None, help="also write the full report here")
    parser.add_argument("--no-failures", action="store_true", help="print metrics and gates only, never the failing cases")
    parser.add_argument("--llm", action="store_true", help="let triage use the configured LLM for tie-breaks")
    args = parser.parse_args(argv)
    if args.compare and args.split:
        parser.error("--compare runs every split; drop --split")

    port = None
    if args.llm:
        from noc_agents.llm.client import get_llm_port

        port = get_llm_port()
        if port is None:
            print("--llm: the LLM layer is off (LLM_ENABLED, provider or credential); nothing run", file=sys.stderr)
            return 3
    operator_id = get_settings().operator.operator_id
    paths = tuple(args.golden) if args.golden else GOLDEN_PATHS
    try:
        if args.compare:
            reports = {name: run_eval(operator_id=operator_id, path=paths, split=name, port=port) for name in SPLITS}
            reports = {name: report for name, report in reports.items() if report["dataset"]["size"]}
            regression = run_eval(operator_id=operator_id, path=paths, split=REGRESSION_SPLITS, port=port)
            print(format_compare(reports, regression))
            if args.json:
                args.json.write_text(json.dumps({**reports, "regression": regression}, indent=2), encoding="utf-8")
            return 0 if regression["passed"] else 1
        report = run_eval(operator_id=operator_id, path=paths, split=args.split, port=port)
    except GoldenSetError as exc:
        print(f"golden set refused: {exc}", file=sys.stderr)
        return 2
    print(format_report(report, failures=not args.no_failures))
    if args.json:
        args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
