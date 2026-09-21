"""Print the golden event sequence and step rows, and diff them against ``tests/fixtures/golden/``.

Spec §10.5 step 5 (CONFORMANCE C-21): "``python scripts/golden_diff.py`` prints the golden event
sequence and step rows and diffs against ``tests/fixtures/golden/`` -- the 26 run-scoped and 6
merge/cascade event literals unchanged, the global-history ``email.sent`` position as fixed by
§2.1 R4, and the step-row literals as fixed by R3."

Usage (no project install needed; ``src`` is put on ``sys.path`` the way tests/conftest.py does):

    C:\\Python313\\python.exe scripts\\golden_diff.py                 # capture, print, diff
    C:\\Python313\\python.exe scripts\\golden_diff.py --quiet         # verdicts and diffs only
    C:\\Python313\\python.exe scripts\\golden_diff.py --scenario merge_short_circuit
    C:\\Python313\\python.exe scripts\\golden_diff.py --tmp-dir D:\\scratch   # where the throwaway DBs go
    C:\\Python313\\python.exe scripts\\golden_diff.py --update        # REWRITE the fixtures (see below)

Exit codes:  0 = every scenario matches its fixture (or --update wrote them)
             1 = a scenario differs, a fixture is missing, or a scenario could not be captured
             2 = refused or bad usage (DATABASE_URL or --tmp-dir inside the repo's data folder)

**The same scenario as the test, not an approximation.** The four scenarios mirror the four
golden tests in ``tests/integration/test_golden_sequence.py`` one for one, and nothing about
them is restated here:

* the environment is the suite's own -- ``tests/conftest.py`` is loaded first, so every flag
  and blanked credential it pins before the first ``noc_agents`` import is pinned here too,
  and a flag it pins later is picked up without touching this file. Three more are pinned on
  top (``EXTRA_PINS``), because the golden sequence is defined with every flag off and the
  conftest leaves them to the shell;
* the alarm payloads and the event projection come from the test module itself
  (``HUB_EVENT``, ``BTS_EVENT``, ``PARENT_HUB_EVENT``, ``CHILD_EVENT``, ``_shape``), loaded by
  path, so an edit to the test's inputs shows up here as a diff instead of drifting silently;
* each scenario gets a brand-new SQLite file, exactly as the ``tmp_db`` fixture builds it,
  and the realtime history is cleared where the test clears it.

**Always a fresh temporary database.** Every scenario runs against a new file in a new
temporary folder (``--tmp-dir`` chooses the parent; default: the system temp folder), which
is deleted afterwards unless ``--keep-tmp`` is given. The script refuses to start when
``DATABASE_URL`` -- or ``--tmp-dir`` -- points anywhere under the repository's ``data``
folder, and checks every URL it builds again before opening it. A golden run creates
incidents, runs, audit rows and outbox rows; none of that may ever land in a real database.

**What the fixtures hold.** One JSON file per scenario: the run-scoped event sequence as the
seven-field projection the test literals use (``type, seq, node, agent, status,
incident_number, incident_id is set``); the payload of every event that is not an
``agent.step.*`` event; the payload key set per event type and the envelope key set (G2
compares both exactly); the full realtime history as ``[type, run_scoped]`` pairs, which is
where the ``email.sent`` position of §2.1 R4 lives; the run row; every step row (input,
output, rationale, tools, confidence -- the R3 literals); the new work notes in order (the R4
note order); and the returned incident. Values that change on every run are masked, in the
order ``MASKS`` lists them: uuids, timestamps, dates, the ledger file name's date and shift
(the shift is DAY or NIGHT by the clock), and ``HH:MM EAT`` clock times. Everything else is
literal. Durations are not captured at all.

The fixtures are stricter than the test in places (the test checks some rationales with
``startswith``; the fixture keeps the whole deterministic string) and never looser: every
literal the test pins is in the fixture unmasked, or masked exactly where the test uses a
regex.

**Updating is a decision, not a refresh.** A golden fixture may only move for an enumerated
§2.1 re-baseline (R3-R5), in its own reviewed PR (G1, G2). ``--update`` prints what it is
about to change, rewrites the files and says so loudly; review ``git diff tests/fixtures/golden``
before committing. If the golden moves for any other reason, stop and investigate.

Files are written as UTF-8 with LF endings and one fixed layout (dict keys sorted; a list or
dict that holds only scalars on one line), and compared as parsed data, so a CRLF checkout
or a hand re-indent is reported as a formatting note, never as a golden change.
"""

from __future__ import annotations

import argparse
import difflib
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import traceback
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "golden"
CONFTEST_PATH = ROOT / "tests" / "conftest.py"
GOLDEN_TEST_PATH = ROOT / "tests" / "integration" / "test_golden_sequence.py"
GOLDEN_TEST_ID = "tests/integration/test_golden_sequence.py"
UPDATE_COMMAND = "python scripts/golden_diff.py --update"
FIXTURE_FORMAT = 1

# Pinned on top of tests/conftest.py. The golden sequence is defined with every flag OFF (G2,
# §10.1) and with the synchronous post-commit drain on (§10.1 integration row), but the conftest
# leaves these three to the shell, where an exported value would change what the golden shows:
# ALERT_ENVELOPE_V2 switches the HITL node's wording (services/hitl.render_channels),
# WEATHER_ENABLED switches ENRICH's cache read, OUTBOX_SYNC_DRAIN moves email.sent (R4).
EXTRA_PINS = {
    "ALERT_ENVELOPE_V2": "false",
    "WEATHER_ENABLED": "false",
    "OUTBOX_SYNC_DRAIN": "true",
}

# Applied to every captured string, in this order (the ledger name first: its date would
# otherwise be masked as a bare date and its shift left behind).
MASKS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    ("ledger file name", re.compile(r"ledger_\d{4}-\d{2}-\d{2}_(?:DAY|NIGHT)\.xlsx"), "ledger_<date>_<shift>.xlsx"),
    ("uuid", re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"), "<uuid>"),
    ("timestamp", re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?"), "<timestamp>"),
    ("date", re.compile(r"\d{4}-\d{2}-\d{2}"), "<date>"),
    ("EAT clock time", re.compile(r"\b\d{2}:\d{2} EAT\b"), "<HH:MM> EAT"),
)


@dataclass(frozen=True)
class Scenario:
    """One golden test, as data: the alarms to replay and which one is measured."""

    name: str
    test: str
    setup: tuple[str, ...]  # names of the test module's event dicts processed before the measured one
    measured: str  # name of the event dict whose run is captured


SCENARIOS: tuple[Scenario, ...] = (
    Scenario("full_lifecycle_hitl", "test_golden_full_lifecycle_with_hitl", (), "HUB_EVENT"),
    Scenario("full_lifecycle_auto_broadcast", "test_golden_full_lifecycle_auto_broadcast", (), "BTS_EVENT"),
    Scenario("merge_short_circuit", "test_golden_merge_short_circuit", ("HUB_EVENT",), "HUB_EVENT"),
    Scenario("cascade_child_short_circuit", "test_golden_cascade_child_short_circuit", ("PARENT_HUB_EVENT",), "CHILD_EVENT"),
)


class Refused(Exception):
    """A safety check failed; nothing was opened."""


def err(message: str) -> None:
    """To stderr, after flushing stdout, so a piped log keeps the two in order."""
    sys.stdout.flush()
    print(message, file=sys.stderr)


# ---------------------------------------------------------------------------------------------
# Safety: never a database under data/.
# ---------------------------------------------------------------------------------------------
def sqlite_file(url: str) -> Path | None:
    """The file a ``sqlite:///`` URL names, resolved the way config.py resolves it; else None."""
    if not url.startswith("sqlite:///") or url.endswith(":memory:"):
        return None
    rest = url.removeprefix("sqlite:///")
    if rest.startswith("./"):
        return (ROOT / rest[2:]).resolve()
    return Path(rest).resolve()


def is_under(path: Path, parent: Path) -> bool:
    """Case-insensitive on Windows, where ``C:\\Repo\\Data`` and ``c:\\repo\\data`` are one folder."""
    child = os.path.normcase(str(path.resolve()))
    root = os.path.normcase(str(parent.resolve()))
    return child == root or child.startswith(root.rstrip(os.sep) + os.sep)


def refuse_data_dir(url: str, *, what: str) -> None:
    path = sqlite_file(url)
    if path is not None and is_under(path, DATA_DIR):
        raise Refused(f"{what} points under the repository's data folder ({path}); golden_diff never opens it")


# ---------------------------------------------------------------------------------------------
# Loading the suite's environment and the golden test's inputs.
# ---------------------------------------------------------------------------------------------
def load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_suite_environment() -> None:
    """Run tests/conftest.py's module body: the env pins it sets before any noc_agents import."""
    load_module("_golden_diff_conftest", CONFTEST_PATH)
    os.environ.update(EXTRA_PINS)


# ---------------------------------------------------------------------------------------------
# Masking and the canonical text form.
# ---------------------------------------------------------------------------------------------
def mask(value: Any) -> Any:
    if isinstance(value, str):
        for _label, pattern, replacement in MASKS:
            value = pattern.sub(replacement, value)
        return value
    if isinstance(value, dict):
        return {k: mask(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [mask(v) for v in value]
    return value


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def dumps(value: Any, indent: int = 0) -> str:
    """Stable JSON text: sorted keys; a container holding only scalars sits on one line."""
    pad, inner = "  " * indent, "  " * (indent + 1)
    if isinstance(value, dict):
        if not value:
            return "{}"
        if all(_is_scalar(v) for v in value.values()):
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        items = [f"{inner}{json.dumps(k, ensure_ascii=False)}: {dumps(value[k], indent + 1)}" for k in sorted(value)]
        return "{\n" + ",\n".join(items) + "\n" + pad + "}"
    if isinstance(value, list):
        if not value:
            return "[]"
        if all(_is_scalar(v) for v in value):
            return json.dumps(value, ensure_ascii=False)
        return "[\n" + ",\n".join(f"{inner}{dumps(v, indent + 1)}" for v in value) + "\n" + pad + "]"
    return json.dumps(value, ensure_ascii=False)


def canonical_text(data: Any) -> str:
    return dumps(data) + "\n"


# ---------------------------------------------------------------------------------------------
# Capture: one scenario on one brand-new database.
# ---------------------------------------------------------------------------------------------
def _distinct_key_sets(records: list[dict], field: str | None) -> dict[str, list[list[str]]]:
    """Per event type, every distinct key set seen (normally exactly one)."""
    seen: dict[str, list[list[str]]] = {}
    for record in records:
        keys = sorted(record[field] if field else record)
        bucket = seen.setdefault(record["type"], [])
        if keys not in bucket:
            bucket.append(keys)
    return {t: sorted(v) for t, v in sorted(seen.items())}


def capture(scenario: Scenario, golden: ModuleType, workdir: Path) -> dict[str, Any]:
    """Replay the scenario exactly as its test does and return the masked golden record."""
    from sqlalchemy import select

    from noc_agents.config import clear_settings_cache, get_settings
    from noc_agents.db.models import AgentRunRow, IncidentRow, WorkNoteRow, get_session, init_db
    from noc_agents.domain.schemas import EventIngest
    from noc_agents.graph.pipeline import process_event
    from noc_agents.realtime.hub import hub

    folder = workdir / scenario.name
    folder.mkdir(parents=True, exist_ok=False)
    url = f"sqlite:///{(folder / 'test.db').as_posix()}"
    refuse_data_dir(url, what="the scenario database")
    # The tmp_db fixture, step for step.
    os.environ["DATABASE_URL"] = url
    os.environ["OPERATOR_PROFILE"] = "safaricom"
    clear_settings_cache()
    settings = get_settings().model_copy(update={"database_url": url})
    refuse_data_dir(settings.database_url, what="the resolved settings")
    engine = init_db(url)
    session = get_session()
    try:
        hub._history.clear()  # the clean_hub fixture
        setup_incident = None
        for name in scenario.setup:
            setup_incident = process_event(session, settings, EventIngest(**getattr(golden, name)))
        runs_before = {r.id for r in session.scalars(select(AgentRunRow)).all()}
        notes_before = {n.id for n in session.scalars(select(WorkNoteRow)).all()}
        if scenario.setup:
            hub._history.clear()  # the merge/cascade tests clear again before the measured run
        measured = getattr(golden, scenario.measured)
        incident = process_event(session, settings, EventIngest(**measured))

        new_runs = [r for r in session.scalars(select(AgentRunRow)).all() if r.id not in runs_before]
        lifecycle = [r for r in new_runs if r.graph_name == "incident_lifecycle"]
        if not lifecycle:
            raise RuntimeError(f"{scenario.name}: the measured event produced no incident_lifecycle run")
        run = lifecycle[0]
        history = [dict(e) for e in hub._history]
        run_events = [e for e in history if e.get("run_id") == run.id]
        notes = [n for n in session.scalars(select(WorkNoteRow)).all() if n.id not in notes_before]

        record = {
            "format": FIXTURE_FORMAT,
            "scenario": scenario.name,
            "test": f"{GOLDEN_TEST_ID}::{scenario.test}",
            "masks": [f"{label} -> {replacement}" for label, _pattern, replacement in MASKS],
            "input_events": [getattr(golden, name) for name in (*scenario.setup, scenario.measured)],
            "runs_created": len(new_runs),
            "run": {
                "graph_name": run.graph_name,
                "status": run.status,
                "current_node": run.current_node,
                "error_summary": run.error_summary,
                "bound_to_returned_incident": run.incident_id == incident.id,
            },
            "incident": {
                "incident_number": incident.incident_number,
                "incidents_in_db": len(session.scalars(select(IncidentRow)).all()),
                "child_sites_down": incident.child_sites_down,
                "same_as_setup_incident": None if setup_incident is None else incident.id == setup_incident.id,
            },
            "events": [list(golden._shape(e)) for e in run_events],
            "event_payloads": [
                {"type": e["type"], "payload": e["payload"]} for e in run_events if not e["type"].startswith("agent.step.")
            ],
            "global_history": [[e["type"], e.get("run_id") == run.id] for e in history],
            "global_events": [
                {"type": e["type"], "incident_id_set": e["incident_id"] is not None, "payload": e["payload"]}
                for e in history
                if e.get("run_id") != run.id
            ],
            "envelope_keys": sorted({tuple(sorted(e)) for e in history}),
            "payload_keys": _distinct_key_sets(history, "payload"),
            "steps": [
                {
                    "seq": s.seq,
                    "node": s.node_name,
                    "agent": s.agent_name,
                    "status": s.status,
                    "input_summary": s.input_summary,
                    "output_summary": s.output_summary,
                    "rationale": s.rationale,
                    "tools_called": s.tools_called,
                    "confidence": s.confidence,
                }
                for s in run.steps
            ],
            "work_notes": [[n.author, n.author_role, n.source, n.body] for n in notes],
        }
    finally:
        session.close()
        engine.dispose()  # Windows will not delete an open SQLite file
        hub._history.clear()
        clear_settings_cache()
    # One JSON round trip turns tuples into lists, so a fresh capture compares equal to a load.
    return json.loads(json.dumps(mask(record), ensure_ascii=False))


# ---------------------------------------------------------------------------------------------
# Printing.
# ---------------------------------------------------------------------------------------------
def _cell(value: Any) -> str:
    return "-" if value is None else str(value)


def print_scenario(record: dict[str, Any]) -> None:
    run = record["run"]
    print(f"== {record['scenario']}  ({record['test']})")
    print(
        f"   run: {run['status']}  current_node={_cell(run['current_node'])}  error={_cell(run['error_summary'])}  "
        f"runs_created={record['runs_created']}  incident={record['incident']['incident_number']}"
    )
    history = record["global_history"]
    print(f"   events: {len(record['events'])} run-scoped, {sum(1 for _t, scoped in history if not scoped)} global")
    header = ("#", "type", "seq", "node", "agent", "status", "incident", "id")
    print(f"   {header[0]:>3}  {header[1]:<24}{header[2]:>4}  {header[3]:<11}{header[4]:<25}{header[5]:<13}{header[6]:<11}{header[7]}")
    for i, (etype, seq, node, agent, status, number, has_id) in enumerate(record["events"], 1):
        print(
            f"   {i:>3}  {etype:<24}{_cell(seq):>4}  {_cell(node):<11}{_cell(agent):<25}{_cell(status):<13}"
            f"{_cell(number):<11}{'yes' if has_id else 'no'}"
        )
    for position, (etype, scoped) in enumerate(history, 1):
        if not scoped:
            print(f"   global history: {etype} at position {position} of {len(history)} (not run-scoped)")
    print(f"   steps: {len(record['steps'])}")
    for step in record["steps"]:
        tools = ", ".join(
            f"{t.get('name')}({'ok' if t.get('ok') else 'FAILED'},{t.get('latency_ms')}ms)" for t in step["tools_called"] or []
        )
        print(f"   {step['seq']:>3}  {step['node']:<11}{step['agent']:<25}{step['status']:<13}confidence={_cell(step['confidence'])}")
        print(f"          input : {_cell(step['input_summary'])}")
        print(f"          output: {_cell(step['output_summary'])}")
        print(f"          reason: {_cell(step['rationale'])}")
        print(f"          tools : {tools or '(none)'}")
    for author, role, source, _body in record["work_notes"]:
        print(f"   work note: {author} / {role} / {source}")
    print()


# ---------------------------------------------------------------------------------------------
# Compare / update.
# ---------------------------------------------------------------------------------------------
def fixture_path(scenario: Scenario) -> Path:
    return FIXTURE_DIR / f"{scenario.name}.json"


def diff_text(expected: str, actual: str, name: str) -> str:
    return "".join(
        difflib.unified_diff(
            expected.splitlines(keepends=True),
            actual.splitlines(keepends=True),
            fromfile=f"tests/fixtures/golden/{name}.json (fixture)",
            tofile=f"{name} (captured now)",
        )
    )


def compare(scenario: Scenario, record: dict[str, Any]) -> tuple[bool, str]:
    """(matches, message). Parsed data decides; the canonical text is only for the diff."""
    path = fixture_path(scenario)
    if not path.is_file():
        return False, f"   MISSING fixture {path.relative_to(ROOT).as_posix()} -- create it with {UPDATE_COMMAND}\n"
    raw = path.read_text(encoding="utf-8")
    expected = json.loads(raw)
    if expected == record:
        note = ""
        if raw != canonical_text(expected):
            note = "   note: fixture text is not in canonical layout (CRLF or hand edit); data is identical\n"
        return True, note
    return False, diff_text(canonical_text(expected), canonical_text(record), scenario.name)


def update(selected: list[Scenario], records: dict[str, dict[str, Any]]) -> int:
    banner = "!" * 78
    err(banner)
    err("!! golden_diff --update: REWRITING the golden fixtures in tests/fixtures/golden/")
    err("!! A golden fixture may only move for an enumerated spec §2.1 re-baseline (R3-R5),")
    err("!! in its own reviewed PR that shows the old and new literal side by side (G1, G2).")
    err("!! Anything else that moves the golden is a bug to investigate, not to re-baseline.")
    err(banner)
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    changed = 0
    for scenario in selected:
        path = fixture_path(scenario)
        text = canonical_text(records[scenario.name])
        old = path.read_text(encoding="utf-8") if path.is_file() else None
        if old == text:
            print(f"   unchanged  {path.relative_to(ROOT).as_posix()}")
            continue
        changed += 1
        if old is None:
            print(f"   CREATED    {path.relative_to(ROOT).as_posix()}")
        else:
            print(f"   REWRITTEN  {path.relative_to(ROOT).as_posix()}")
            sys.stdout.write(diff_text(old, text, scenario.name))
        path.write_text(text, encoding="utf-8", newline="\n")
    err(banner)
    err(f"!! {changed} fixture file(s) written. Review `git diff tests/fixtures/golden` before committing.")
    err(banner)
    return 0


# ---------------------------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # a legacy Windows code page cannot print "→" or "—"
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    names = [s.name for s in SCENARIOS]
    parser = argparse.ArgumentParser(description="Print the golden sequence and diff it against tests/fixtures/golden/")
    parser.add_argument("--update", action="store_true", help="rewrite the fixtures from the current code (loud; review the diff)")
    parser.add_argument("--quiet", action="store_true", help="print verdicts and diffs only, not the sequences")
    parser.add_argument("--scenario", action="append", choices=names, help="run only this scenario (repeatable)")
    parser.add_argument("--tmp-dir", type=Path, default=None, help="parent folder for the throwaway databases (default: system temp)")
    parser.add_argument("--keep-tmp", action="store_true", help="keep the throwaway folder for inspection")
    args = parser.parse_args(argv)
    selected = [s for s in SCENARIOS if not args.scenario or s.name in args.scenario]

    try:
        refuse_data_dir(os.environ.get("DATABASE_URL", ""), what="DATABASE_URL")
        if args.tmp_dir is not None and is_under(args.tmp_dir, DATA_DIR):
            raise Refused(f"--tmp-dir {args.tmp_dir} is under the repository's data folder")
    except Refused as exc:
        err(f"golden_diff: refused: {exc}")
        return 2

    if args.tmp_dir is not None:
        args.tmp_dir.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="golden_diff_", dir=args.tmp_dir))
    os.environ["LEDGER_DIR"] = str(workdir / "shift_ledgers")  # the isolated_ledger_dir fixture
    load_suite_environment()
    golden = load_module("_golden_diff_golden_sequence", GOLDEN_TEST_PATH)

    records: dict[str, dict[str, Any]] = {}
    failures = 0
    try:
        for scenario in selected:
            try:
                records[scenario.name] = capture(scenario, golden, workdir)
            except Refused as exc:
                err(f"golden_diff: refused: {exc}")
                return 2
            except Exception:  # noqa: BLE001 -- a crash is a golden failure; report it and go on
                failures += 1
                err(f"== {scenario.name}: CAPTURE FAILED")
                traceback.print_exc()
    finally:
        if args.keep_tmp:
            print(f"(throwaway folder kept: {workdir})")
        else:
            shutil.rmtree(workdir, ignore_errors=True)

    if failures:
        err(f"golden_diff: {failures} scenario(s) could not be captured; fixtures NOT compared or written")
        return 1
    if args.update:
        return update(selected, records)

    mismatches = 0
    for scenario in selected:
        record = records[scenario.name]
        if not args.quiet:
            print_scenario(record)
        ok, message = compare(scenario, record)
        print(f"{'MATCH   ' if ok else 'MISMATCH'} {scenario.name}")
        if message:
            sys.stdout.write(message)
        mismatches += 0 if ok else 1
    if mismatches:
        err(
            f"golden_diff: {mismatches} of {len(selected)} scenario(s) differ from tests/fixtures/golden/. "
            "Stop and investigate; re-baseline only for an enumerated §2.1 change."
        )
        return 1
    print(f"golden_diff: all {len(selected)} scenario(s) match tests/fixtures/golden/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
