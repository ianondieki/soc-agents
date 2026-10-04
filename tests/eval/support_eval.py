"""Support desk evals from the command line: the golden set, the metrics, the gates.

The harness itself is ``noc_agents.support.evals`` (the API's ``POST /api/v1/support/evals/run``
calls the same function); this file is the command-line entry point the contract names and
the nightly LLM comparison runs. Definitions of every metric are in that module's docstring.

Usage::

    python tests/eval/support_eval.py                 # every case; the headline is the held-out TEST split
    python tests/eval/support_eval.py --split dev     # one split
    python tests/eval/support_eval.py --compare       # dev and test side by side, plus the top failure kinds
    python tests/eval/support_eval.py --json out.json # the full EvalReport as JSON ({"dev", "test"} with --compare)
    python tests/eval/support_eval.py --llm           # nightly: LLM tie-breaks on (needs LLM_ENABLED)

Every run is isolated: each case gets its own in-memory database (the live database and the
live event hub are never touched). ``--llm`` passes the configured model port to triage, which
uses it only to break a low-confidence tie. The golden texts are synthetic and the numbers
fictional, so no customer data leaves; the run still spends from the same monthly LLM budget
as the app. pytest never runs ``--llm``: ``tests/conftest.py`` pins ``LLM_ENABLED=false``.

Exit codes: 0 = every gate passed (on the test split, unless ``--split dev``); 1 = a gate
failed; 2 = bad usage or a malformed golden set; 3 = ``--llm`` asked for but the LLM layer is off.
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
from noc_agents.support.evals import GOLDEN_PATH, SPLITS, GoldenSetError, run_eval  # noqa: E402


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


def format_report(report: dict) -> str:
    """The human summary: dataset, metrics (per split when more than one ran), gates, confusion, failures."""
    ds = report["dataset"]
    lines = [f"support desk eval  {report['mode']}  {ds['name']}@{ds['version']}  split={ds['split']}  n={ds['size']}", ""]
    by_split = report.get("by_split") or {}
    if len(by_split) > 1:
        names = [s for s in SPLITS if s in by_split]
        lines.append(f"  {'metric':<32} " + " ".join(f"{n:>10}" for n in names) + "   (headline: " + ds["split"] + ")")
        for name in report["metrics"]:
            lines.append(f"  {name:<32} " + " ".join(f"{_fmt(by_split[s].get(name)):>10}" for s in names))
    else:
        lines += [f"  {name:<32} {_fmt(value)}" for name, value in report["metrics"].items()]
    lines.append("")
    lines += _gate_lines(report)
    labels = report["confusion"]["labels"]
    lines += ["", "  confusion (rows expected, cols actual): " + " ".join(f"{label:>8}" for label in labels)]
    for label, row in zip(labels, report["confusion"]["matrix"], strict=True):
        lines.append(f"  {label:>39} " + " ".join(f"{n:>8}" for n in row))
    if report["failures"]:
        lines += ["", f"  failures ({ds['split']} split):"]
        lines += [f"    {f['case_id']}: {f['kind']}  expected {f['expected']['route']}/{f['expected']['category']}"
                  f"  got {f['actual']['route']}/{f['actual']['category']}" for f in report["failures"]]
    lines += ["", "PASSED" if report["passed"] else "FAILED"]
    return "\n".join(lines)


def failure_kind_counts(report: dict) -> list[tuple[str, int]]:
    """Failure kinds in a report, most frequent first."""
    return Counter(f["kind"] for f in report["failures"]).most_common()


def format_compare(dev: dict, test: dict, *, top: int = 5) -> str:
    """Dev and test side by side: every metric, the gates on the test split, the top failure kinds of each."""
    ds = test["dataset"]
    lines = [f"support desk eval  {test['mode']}  {ds['name']}@{ds['version']}  compare", "",
             f"  {'metric':<32} {'dev':>10} {'test':>10}   dev n={dev['dataset']['size']}  test n={ds['size']}"]
    for name in test["metrics"]:
        lines.append(f"  {name:<32} {_fmt(dev['metrics'].get(name)):>10} {_fmt(test['metrics'].get(name)):>10}")
    lines += ["", "  gates (judged on the test split):"] + _gate_lines(test)
    lines += ["", f"  top failure kinds (up to {top}):"]
    for label, report in (("dev", dev), ("test", test)):
        kinds = failure_kind_counts(report)[:top]
        lines.append(f"    {label:<5} " + (", ".join(f"{kind} {n}" for kind, n in kinds) if kinds else "none"))
    lines += ["", "PASSED" if test["passed"] else "FAILED"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the support desk eval suite.")
    parser.add_argument("--split", choices=SPLITS, default=None, help="only this split (default: all; headline = test)")
    parser.add_argument("--compare", action="store_true", help="run dev and test and print them side by side")
    parser.add_argument("--golden", type=Path, default=GOLDEN_PATH, help="golden set path (JSONL)")
    parser.add_argument("--json", type=Path, default=None, help="also write the full report here")
    parser.add_argument("--llm", action="store_true", help="let triage use the configured LLM for tie-breaks")
    args = parser.parse_args(argv)
    if args.compare and args.split:
        parser.error("--compare runs both splits; drop --split")

    port = None
    if args.llm:
        from noc_agents.llm.client import get_llm_port

        port = get_llm_port()
        if port is None:
            print("--llm: the LLM layer is off (LLM_ENABLED, provider or credential); nothing run", file=sys.stderr)
            return 3
    operator_id = get_settings().operator.operator_id
    try:
        if args.compare:
            dev = run_eval(operator_id=operator_id, path=args.golden, split="dev", port=port)
            report = run_eval(operator_id=operator_id, path=args.golden, split="test", port=port)
            print(format_compare(dev, report))
            if args.json:
                args.json.write_text(json.dumps({"dev": dev, "test": report}, indent=2), encoding="utf-8")
            return 0 if report["passed"] else 1
        report = run_eval(operator_id=operator_id, path=args.golden, split=args.split, port=port)
    except GoldenSetError as exc:
        print(f"golden set refused: {exc}", file=sys.stderr)
        return 2
    print(format_report(report))
    if args.json:
        args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
