"""Lane guard: did parallel feature work stay inside its lane?

Phase 4 was built by several agents at once, each owning a disjoint set of files. That
only holds if nobody quietly edits a shared file -- `main.py`, `db/models.py`,
`db/migrate.py`, the golden contract test -- because two agents editing one file in
parallel means the second one silently discards the first one's work.

This script answers three questions against git, in the order that matters:

  1. Did any PROTECTED file change? Those are the shared files and the frozen contracts.
     A change here is not automatically wrong -- I land some of them deliberately at
     integration -- but it must be a decision, never a surprise.
  2. Did the frozen golden contract move? `tests/system/test_contracts.py` pins 26 event
     literals and the full-HITL golden path. It has been byte-identical since before
     Phase 0 and a change to it is a product decision, not a refactor.
  3. What did change, grouped by lane, so a human reviewer knows where to look.

Usage:
    python scripts/check_lanes.py                # working tree vs HEAD
    python scripts/check_lanes.py --since v2-phase-3-stopline

Exit code 1 if a protected file or the golden contract moved, else 0.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Files that more than one lane would otherwise want to edit, plus the frozen contracts.
# Editing these is an INTEGRATION act: done once, by one person, deliberately.
PROTECTED = (
    "src/noc_agents/main.py",
    "src/noc_agents/db/models.py",
    "src/noc_agents/db/migrate.py",
    "src/noc_agents/db/models_all.py",
    "src/noc_agents/api/deps.py",
    "src/noc_agents/api/routers/__init__.py",
    "src/noc_agents/scheduler/loop.py",
    "src/noc_agents/orchestrator/registry.py",
    "src/noc_agents/domain/enums.py",
    "pyproject.toml",
)

# Byte-frozen. A diff here is never a refactor; it is a change to what the system promises.
FROZEN = ("tests/system/test_contracts.py",)

# Which lane owns what, by path prefix or exact name. Only used to group the report.
LANES = {
    "4A vendors + stop clocks": (
        "db/models_vendors.py", "services/vendors.py", "services/clock_events.py",
        "routers/vendors.py", "routers/clocks.py", "config/sla_terms.yaml",
        "test_vendors.py", "test_clock_events.py",
    ),
    "4A regulatory + evidence": (
        "db/models_regulatory.py", "services/regulatory.py", "services/evidence.py",
        "routers/regulatory.py", "test_regulatory_clock.py", "test_evidence_pack.py",
    ),
    "4A housekeeping": (
        "services/housekeeping.py", "agents/housekeeping.py", "config/retention.yaml",
        "test_housekeeping.py",
    ),
    "4B post-incident reviews": (
        "db/models_pir.py", "services/pir.py", "routers/pir.py",
        "test_pir.py", "test_known_error.py",
    ),
    "4B regions dashboard": (
        "routers/dashboards.py", "services/dashboards.py", "Regions.tsx",
        "ca_qos", "test_dashboard_regions.py",
    ),
    "4C memory recall (M0)": (
        "services/memory.py", "routers/memory.py", "db/models_memory.py",
        "EarlierAtThisSite.tsx", "test_memory_",
    ),
    "realtime after-commit fix": (
        "services/lifecycle.py", "services/worklog_monitor.py",
        "test_events_after_commit_lifecycle.py",
    ),
}


def git(*args: str) -> str:
    out = subprocess.run(("git", *args), cwd=ROOT, capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(f"git {' '.join(args)} failed:\n{out.stderr.strip()}")
    return out.stdout


def changed_files(since: str | None) -> list[str]:
    """Every path that differs, tracked or not. Untracked files count: a new lane file is
    the normal case here, and a report that silently omitted them would be useless."""
    tracked = git("diff", "--name-only", since) if since else git("diff", "--name-only", "HEAD")
    untracked = git("ls-files", "--others", "--exclude-standard")
    return sorted({p for p in (tracked + untracked).splitlines() if p.strip()})


def lane_of(path: str) -> str:
    for lane, markers in LANES.items():
        if any(m in path for m in markers):
            return lane
    return "unassigned"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", help="compare against this ref instead of HEAD (e.g. v2-phase-3-stopline)")
    args = ap.parse_args()

    files = changed_files(args.since)
    if not files:
        print("nothing changed.")
        return 0

    violations = [p for p in files if p in PROTECTED]
    frozen_moved = [p for p in files if p in FROZEN]

    print(f"{len(files)} file(s) changed vs {args.since or 'HEAD'}\n")
    by_lane: dict[str, list[str]] = {}
    for p in files:
        by_lane.setdefault(lane_of(p), []).append(p)
    for lane in sorted(by_lane):
        print(f"  {lane}")
        for p in by_lane[lane]:
            mark = "  !! PROTECTED" if p in PROTECTED else ("  !! FROZEN" if p in FROZEN else "")
            print(f"      {p}{mark}")
        print()

    if frozen_moved:
        print("FROZEN CONTRACT MOVED -- stop and read the diff before anything else:")
        for p in frozen_moved:
            print(f"  {p}")
        print("  This file has been byte-identical since before Phase 0. Changing what it")
        print("  asserts changes what the system promises its operators. That is a product")
        print("  decision; it is not something a refactor may do as a side effect.\n")

    if violations:
        print("SHARED FILES TOUCHED -- fine if you did it on purpose at integration,")
        print("a silent lost update if a lane agent did it:")
        for p in violations:
            print(f"  {p}")
        print()

    if frozen_moved or violations:
        print("Review the diffs above, then re-run. Exit 1.")
        return 1
    print("No protected or frozen file touched. Lanes stayed in their lanes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
