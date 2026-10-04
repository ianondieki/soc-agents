"""Support desk evals from the command line: the golden set, the metrics, the gates.

The harness itself is ``noc_agents.support.evals`` (the API's ``POST /api/v1/support/evals/run``
calls the same function); this file is the command-line entry point the contract names and
the nightly LLM comparison runs. Definitions of every metric are in that module's docstring.

Usage::

    python tests/eval/support_eval.py                 # deterministic, all splits
    python tests/eval/support_eval.py --split test    # one split
    python tests/eval/support_eval.py --json out.json # the full EvalReport as JSON
    python tests/eval/support_eval.py --llm           # nightly: LLM tie-breaks on (needs LLM_ENABLED)

Every run is isolated: each case gets its own in-memory database (the live database and the
live event hub are never touched). ``--llm`` passes the configured model port to triage, which
uses it only to break a low-confidence tie. The golden texts are synthetic and the numbers
fictional, so no customer data leaves; the run still spends from the same monthly LLM budget
as the app. pytest never runs ``--llm``: ``tests/conftest.py`` pins ``LLM_ENABLED=false``.

Exit codes: 0 = every gate passed; 1 = a gate failed; 2 = bad usage or a malformed golden set;
3 = ``--llm`` asked for but the LLM layer is off.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from noc_agents.config import get_settings  # noqa: E402
from noc_agents.support.evals import GOLDEN_PATH, SPLITS, GoldenSetError, run_eval  # noqa: E402


def format_report(report: dict) -> str:
    """The human summary: dataset, metrics, gates, confusion matrix, failures."""
    ds = report["dataset"]
    lines = [f"support desk eval  {report['mode']}  {ds['name']}@{ds['version']}  split={ds['split']}  n={ds['size']}", ""]
    lines += [f"  {name:<32} {value}" for name, value in report["metrics"].items()]
    lines.append("")
    for gate in report["gates"]:
        mark = "PASS" if gate["passed"] else "FAIL"
        lines.append(f"  [{mark}] {gate['metric']} {gate['op']} {gate['threshold']}  (got {gate['value']})")
    labels = report["confusion"]["labels"]
    lines += ["", "  confusion (rows expected, cols actual): " + " ".join(f"{label:>8}" for label in labels)]
    for label, row in zip(labels, report["confusion"]["matrix"], strict=True):
        lines.append(f"  {label:>39} " + " ".join(f"{n:>8}" for n in row))
    if report["failures"]:
        lines += ["", "  failures:"]
        lines += [f"    {f['case_id']}: {f['kind']}  expected {f['expected']['route']}/{f['expected']['category']}"
                  f"  got {f['actual']['route']}/{f['actual']['category']}" for f in report["failures"]]
    lines += ["", "PASSED" if report["passed"] else "FAILED"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the support desk eval suite.")
    parser.add_argument("--split", choices=SPLITS, default=None, help="only this split (default: all)")
    parser.add_argument("--golden", type=Path, default=GOLDEN_PATH, help="golden set path (JSONL)")
    parser.add_argument("--json", type=Path, default=None, help="also write the full report here")
    parser.add_argument("--llm", action="store_true", help="let triage use the configured LLM for tie-breaks")
    args = parser.parse_args(argv)

    port = None
    if args.llm:
        from noc_agents.llm.client import get_llm_port

        port = get_llm_port()
        if port is None:
            print("--llm: the LLM layer is off (LLM_ENABLED, provider or credential); nothing run", file=sys.stderr)
            return 3
    try:
        report = run_eval(operator_id=get_settings().operator.operator_id, path=args.golden, split=args.split, port=port)
    except GoldenSetError as exc:
        print(f"golden set refused: {exc}", file=sys.stderr)
        return 2
    print(format_report(report))
    if args.json:
        args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
