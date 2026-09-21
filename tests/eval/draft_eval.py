"""Draft evals over graded alarm sequences: the harness behind ``tests/eval/``.

Spec §10.1, "Eval" row: "(nightly, needs ``LLM_ENABLED``) graded alarm sequences; ...;
template-fallback rate report". §10.2, "Draft evals": "20-50 graded alarm sequences
(``tests/fixtures/eval/alarm_sequences.yaml``) with code graders on end state first (INC number,
priority, region, next-update time present; length limits; no invented cause), then a
claude-opus-5 judge with a rubric on ~40 items, then human spot checks; report pass^k for
sendable drafts". CONFORMANCE C-21.

**One trial.** A sequence's alarms go through ``graph.pipeline.process_event`` on a brand-new
SQLite file (the hot path is deterministic whatever ``LLM_ENABLED`` says -- G4, G6). The end
state is graded. Then every incident's executive brief is drafted through
``llm.assist.draft_exec_brief`` -- the one assist function today that drafts sendable text:
the model when the LLM layer is on, the ``composition.compose_brief`` template when it is off
or when the model's draft fails the runtime validator -- and the draft is graded. With
``--trials k`` every incident is drafted k times.

**The graders.**

* End state, per expected incident (``END_STATE_GRADERS``): the incidents that exist (site ids
  in creation order), the INC number (exact, and the §6.1 ``^INC\\d{6}$`` shape), the priority,
  the region code and its label, the next-update time (present, and inside
  ``next_update_minutes`` of creation, as ``agents/ticket.py`` sets it), the HITL gate and the
  cascade child count. The expected values are in the fixture, derived by hand from the rules
  it lists. These must hold in both modes: the model never decides any of them (G6).
* Draft (``DRAFT_GRADERS``): ``draft_length`` (non-empty, at most ``llm.assist.MAX_BRIEF_CHARS``)
  and five graders read off ``services/validators.validate_content`` -- the existing §6.1
  content check, reused, not reimplemented. The draft is placed in a ``Content`` block of the
  incident's own envelope (``services/alerts.build_alert(..., ai_content=...,
  validate_ai_content=False)``) and judged with ``content_validation_context`` exactly as the
  ``ai_content`` seam judges a model draft. Each finding code belongs to one grader
  (``DRAFT_FINDING_GRADERS``); a code no grader owns fails ``draft_unmapped_findings`` until
  somebody decides where it belongs, so a new validator rule cannot pass unnoticed.

**One finding is recorded and not scored: ``content_missing_next_update``.** The brief has no
clock time to repeat. ``llm/redaction.ALLOWLIST`` does not pass ``next_update_at`` to the model,
and the template says "Next update: ~15 min or on material change." -- the cadence, not the
time. Demanding "16:09 EAT" in the brief would reward the model for inventing a time, which the
drafting guardrail forbids. So the next-update time is graded where it exists, on the end
state (``incidents.next_update_at``, the envelope's ``timing.expires``), and the finding is
counted in the report (``UNGRADED_FINDINGS``). Scoring it in the draft needs a product change
first: the time in the model's input and in the template (the unapproved email ``@2`` draft in
``config/templates/site_down_alert.yaml`` adds exactly that line; spec §12 D3).

**pass^k.** A task is one incident of one sequence; it passes when all k of its drafts pass
every draft grader. Every draft ``draft_exec_brief`` returns is sendable (a model draft that
fails the runtime validator has already been swapped for the template), so pass^k is over all
of them; the report also counts the model-drafted ones on their own, because template
fallbacks pass by construction.

**Template-fallback rate** (§1.2 M11, "alarm if > 20 %"): drafts that came back as the template
over drafts attempted, with the reason read from the assist step's rationale. With the LLM off
it is 100 % by definition and is not an alarm.

**Model judge and human spot checks.** Not built. See the MODEL-JUDGE SEAM block below; the
``--json`` transcript (every draft, its findings and grades) is what a human spot check reads.

**Running it.**

* ``C:\\Python313\\python.exe -m pytest -q tests/eval`` -- the always-on template-path test
  (``test_draft_eval_graders.py``) plus the nightly module, which skips: ``tests/conftest.py``
  pins ``LLM_ENABLED=false`` and blanks ``ANTHROPIC_API_KEY`` for the whole suite.
* Nightly, with the model: ``LLM_ENABLED=true`` and ``ANTHROPIC_API_KEY`` exported in the shell
  (``.env`` is not read), plus ``NOC_ENV=demo`` or the DPIA/TIA references the §7.0.10 transfer
  gate asks for, then ``C:\\Python313\\python.exe tests\\eval\\draft_eval.py [--trials 2]
  [--json report.json]``. Every draft is one model call -- 36 incidents x k trials with today's
  fixture -- and nothing in the app budgets them (the on-demand assist path writes no
  ``llm_calls`` row, which is what ``LLM_MONTHLY_BUDGET_USD`` sums), so the run refuses to plan
  more than ``--max-model-calls`` (default 100; k=3 needs it raised).
* ``--template-only`` runs the same harness on the deterministic path with the LLM off.

Exit codes (command line):  0 = graded, all passed   1 = graded, something failed (or the
fallback rate is above the M11 alarm)   2 = refused or bad usage   3 = skipped: the LLM layer
is off and ``--template-only`` was not given.

Every database is a new file under a temporary folder; ``DATABASE_URL`` or ``--tmp-dir`` under
the repository's ``data`` folder is refused. No e-mail leaves: the suite's own pins
(``EMAIL_ENABLED=false``, blank SMTP/Gmail credentials) are loaded from ``tests/conftest.py``.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

import yaml

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "eval" / "alarm_sequences.yaml"
CONFTEST_PATH = ROOT / "tests" / "conftest.py"
DATA_DIR = ROOT / "data"

SCHEMA_VERSION = 1
ASSIST_FUNCTION = "exec_brief"  # llm.assist.draft_exec_brief
FALLBACK_ALARM_RATE = 0.20  # §1.2 M11: "tracked; alarm if > 20 %"
INCIDENT_NUMBER_RE = re.compile(r"^INC\d{6}$")  # domain/alerts.IncidentRef.incident_number
DEFAULT_MAX_MODEL_CALLS = 100
TRIALS_ENV = "EVAL_TRIALS"
MAX_CALLS_ENV = "EVAL_MAX_MODEL_CALLS"

END_STATE_GRADERS = (
    "incident_set",
    "incident_number",
    "priority",
    "region",
    "next_update",
    "hitl_gate",
    "child_sites_down",
)
# grader -> the validate_content finding codes (or code prefixes) it owns
DRAFT_FINDING_GRADERS: dict[str, tuple[str, ...]] = {
    "draft_incident_number": ("content_missing_incident_number", "content_conflicting_incident_number"),
    "draft_priority": ("content_missing_priority", "content_conflicting_priority"),
    "draft_region": ("content_missing_region_label",),
    "no_invented_cause": ("content_invented_cause",),
    "no_personal_data": ("content_personal_data_",),
}
DRAFT_GRADERS = ("draft_length", *DRAFT_FINDING_GRADERS, "draft_unmapped_findings")
UNGRADED_FINDINGS = {
    "content_missing_next_update": (
        "the brief is never given next_update_at (llm/redaction.ALLOWLIST) and its template states the "
        "cadence, not a clock time; the next-update time is graded on the end state instead"
    ),
}
TEMPLATE_OFF_REASON = "LLM assist off: deterministic template"

# The suite's pins are loaded from tests/conftest.py; these three it blanks or forces off are put
# back from the shell, because they are exactly what the nightly run needs.
KEEP_FROM_SHELL = ("LLM_ENABLED", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
# Pinned on top, as scripts/golden_diff.py does: every flag off except the post-commit drain.
EXTRA_PINS = {"ALERT_ENVELOPE_V2": "false", "WEATHER_ENABLED": "false", "OUTBOX_SYNC_DRAIN": "true"}

_TOP_KEYS = frozenset({"schema_version", "operator", "autonomy_level", "next_update_minutes", "sequences"})
_SEQUENCE_KEYS = frozenset({"id", "title", "source", "events", "expect"})
_EXPECT_KEYS = frozenset(
    {"incident_number", "site_id", "priority", "region_code", "region_label", "requires_hitl", "child_sites_down", "why"}
)


class FixtureError(ValueError):
    """``alarm_sequences.yaml`` is malformed, or does not match the settings it is run with."""


class Refused(RuntimeError):
    """A safety rule stopped the run before anything was opened or called."""


# ---------------------------------------------------------------------------------------------
# The fixture.
# ---------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ExpectedIncident:
    incident_number: str
    site_id: str
    priority: str
    region_code: str
    region_label: str
    requires_hitl: bool
    child_sites_down: int
    why: str


@dataclass(frozen=True)
class AlarmSequence:
    id: str
    title: str
    source: str
    events: tuple[dict[str, Any], ...]
    expect: tuple[ExpectedIncident, ...]


@dataclass(frozen=True)
class EvalSuite:
    operator: str
    autonomy_level: str
    next_update_minutes: int
    sequences: tuple[AlarmSequence, ...]

    def by_id(self, sequence_id: str) -> AlarmSequence:
        for sequence in self.sequences:
            if sequence.id == sequence_id:
                return sequence
        raise FixtureError(f"no sequence with id {sequence_id!r} in {FIXTURE_PATH.relative_to(ROOT).as_posix()}")

    def select(self, ids: Iterable[str] | None) -> tuple[AlarmSequence, ...]:
        if not ids:
            return self.sequences
        return tuple(self.by_id(i) for i in ids)


def _check_keys(item: Any, required: frozenset[str], allowed: frozenset[str], where: str) -> None:
    if not isinstance(item, dict):
        raise FixtureError(f"{where}: expected a mapping, got {type(item).__name__}")
    missing, unknown = required - set(item), set(item) - allowed
    if missing or unknown:
        raise FixtureError(f"{where}: missing {sorted(missing)}, unknown {sorted(unknown)}")


def load_suite(path: Path = FIXTURE_PATH) -> EvalSuite:
    """Parse and check the fixture. Event keys must be ``EventIngest`` fields (no silent defaults)."""
    from noc_agents.domain.schemas import EventIngest  # the field list is the schema's own

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    _check_keys(raw, _TOP_KEYS, _TOP_KEYS, "top level")
    if raw["schema_version"] != SCHEMA_VERSION:
        raise FixtureError(f"schema_version {raw['schema_version']!r}; this harness reads {SCHEMA_VERSION}")
    fields = frozenset(EventIngest.model_fields)
    sequences: list[AlarmSequence] = []
    for index, item in enumerate(raw["sequences"] or []):
        where = f"sequences[{index}]"
        _check_keys(item, _SEQUENCE_KEYS, _SEQUENCE_KEYS, where)
        where = f"sequence {item['id']!r}"
        if any(s.id == item["id"] for s in sequences):
            raise FixtureError(f"{where}: duplicate id")
        if not item["events"] or not item["expect"]:
            raise FixtureError(f"{where}: needs at least one event and one expected incident")
        for n, event in enumerate(item["events"]):
            _check_keys(event, frozenset({"site_id"}), fields, f"{where} events[{n}]")
            EventIngest(**event)  # type errors surface here, with pydantic's message
        expect = []
        for n, exp in enumerate(item["expect"]):
            _check_keys(exp, _EXPECT_KEYS, _EXPECT_KEYS, f"{where} expect[{n}]")
            if not isinstance(exp["requires_hitl"], bool) or not isinstance(exp["child_sites_down"], int):
                raise FixtureError(f"{where} expect[{n}]: requires_hitl must be a bool, child_sites_down an int")
            expect.append(ExpectedIncident(**exp))
        sequences.append(
            AlarmSequence(
                id=item["id"],
                title=item["title"],
                source=item["source"],
                events=tuple(item["events"]),
                expect=tuple(expect),
            )
        )
    return EvalSuite(
        operator=raw["operator"],
        autonomy_level=raw["autonomy_level"],
        next_update_minutes=int(raw["next_update_minutes"]),
        sequences=tuple(sequences),
    )


# ---------------------------------------------------------------------------------------------
# Graders.
# ---------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Grade:
    grader: str
    subject: str  # the incident number (or "sequence"), plus the trial for a draft
    passed: bool
    detail: str


def grade_end_state(incidents: list[Any], expected: Iterable[ExpectedIncident], cfg: Any, next_update_minutes: int) -> list[Grade]:
    """The deterministic outcome: which incidents exist and the facts every draft must repeat."""
    from noc_agents.services.composition import region_label

    expected = list(expected)
    got_sites = [inc.site_id for inc in incidents]
    want_sites = [exp.site_id for exp in expected]
    grades = [Grade("incident_set", "sequence", got_sites == want_sites, f"incidents at {got_sites}; expected {want_sites}")]
    window = timedelta(minutes=next_update_minutes)
    for inc, exp in zip(incidents, expected):
        subject = exp.incident_number
        number = inc.incident_number or ""
        grades.append(
            Grade(
                "incident_number",
                subject,
                number == exp.incident_number and bool(INCIDENT_NUMBER_RE.match(number)),
                f"got {number!r}; expected {exp.incident_number!r} (^INC\\d{{6}}$)",
            )
        )
        grades.append(Grade("priority", subject, inc.priority == exp.priority, f"got {inc.priority}; expected {exp.priority}"))
        label = region_label(cfg, inc.region_code or "")
        grades.append(
            Grade(
                "region",
                subject,
                (inc.region_code, label) == (exp.region_code, exp.region_label),
                f"got {inc.region_code} / {label!r}; expected {exp.region_code} / {exp.region_label!r}",
            )
        )
        due, created = inc.next_update_at, inc.created_at
        on_time = due is not None and created is not None and timedelta(0) < due - created <= window
        grades.append(
            Grade(
                "next_update",
                subject,
                on_time,
                f"next_update_at={due} created_at={created}; expected present and within {next_update_minutes} min of creation",
            )
        )
        grades.append(
            Grade("hitl_gate", subject, bool(inc.requires_hitl) == exp.requires_hitl, f"requires_hitl={bool(inc.requires_hitl)}; expected {exp.requires_hitl}")
        )
        grades.append(
            Grade(
                "child_sites_down",
                subject,
                (inc.child_sites_down or 0) == exp.child_sites_down,
                f"got {inc.child_sites_down}; expected {exp.child_sites_down}",
            )
        )
    return grades


def _code(finding: str) -> str:
    return finding.split(" ", 1)[0]  # validators format: "<code> (<lang>): <reason>"


def grade_draft(inc: Any, cfg: Any, body: str, subject: str) -> tuple[list[Grade], list[str], list[str]]:
    """``(grades, findings, ungraded)`` for one draft of ``inc``'s executive brief."""
    from pydantic import ValidationError

    from noc_agents.domain.alerts import Content
    from noc_agents.llm.assist import MAX_BRIEF_CHARS
    from noc_agents.services.alerts import build_alert, content_validation_context
    from noc_agents.services.validators import validate_content

    text = body or ""
    grades = [
        Grade(
            "draft_length",
            subject,
            bool(text.strip()) and len(text) <= MAX_BRIEF_CHARS,
            f"{len(text)} chars; limit {MAX_BRIEF_CHARS} (llm.assist.MAX_BRIEF_CHARS), not empty",
        )
    ]
    try:
        alert = build_alert(inc, cfg, ai_content={"en": Content(headline="", body=text)}, validate_ai_content=False)
    except ValidationError as exc:  # too long for a §6.1 content block, or no envelope for this incident
        reason = f"not evaluated: no §6.1 envelope with this draft ({exc.errors()[0].get('msg')})"
        grades += [Grade(name, subject, False, reason) for name in (*DRAFT_FINDING_GRADERS, "draft_unmapped_findings")]
        return grades, [], []
    findings = validate_content(alert, **content_validation_context(inc))
    owned: set[str] = set()
    for name, prefixes in DRAFT_FINDING_GRADERS.items():
        hits = [f for f in findings if _code(f).startswith(prefixes)]
        owned.update(hits)
        grades.append(Grade(name, subject, not hits, "; ".join(hits) or "ok"))
    ungraded = [f for f in findings if _code(f) in UNGRADED_FINDINGS]
    unmapped = [f for f in findings if f not in owned and f not in ungraded]
    grades.append(Grade("draft_unmapped_findings", subject, not unmapped, "; ".join(unmapped) or "ok"))
    return grades, findings, ungraded


# =============================================================================================
# MODEL-JUDGE SEAM -- deliberately not built.
#
# §10.2 orders the draft evals: code graders on the end state first (above), "then a
# claude-opus-5 judge with a rubric on ~40 items, then human spot checks". The judge plugs in
# here and nowhere else:
#   * input: one DraftRecord plus the facts it was drafted from -- the same REDACTED payload the
#     drafting call saw (llm/redaction.redact_incident), never the raw incident row;
#   * transport: the provider-neutral port (llm/port.py) behind services/external_calls, so the
#     judge's call is redacted, recorded on the transfer register (DPA reg 41(2)) and
#     spend-gated like every other hosted call;
#   * output: a verdict per rubric item stored on DraftRecord.judge and reported next to the code
#     graders -- never in place of them, and never able to pass a draft a code grader failed;
#   * the rubric (~40 items) is product content for the owner to write, not something to invent.
# Until then nothing calls model_judge, the report says "not built", and no model is called to
# judge anything.
# =============================================================================================
def model_judge(draft: "DraftRecord", facts: dict[str, Any]) -> Any:
    """The seam (see the block above). Raises until it is built; nothing calls it today."""
    raise NotImplementedError("model judge not built: see the MODEL-JUDGE SEAM block in tests/eval/draft_eval.py")


# ---------------------------------------------------------------------------------------------
# Running.
# ---------------------------------------------------------------------------------------------
@dataclass
class DraftRecord:
    sequence_id: str
    incident_number: str
    trial: int
    source: str  # "llm" or "template"
    model: str | None
    fallback_reason: str | None  # the assist step's rationale when the template came back
    body: str
    findings: list[str]
    ungraded: list[str]
    grades: list[Grade]
    judge: Any = None  # MODEL-JUDGE SEAM: stays None until the judge exists

    @property
    def passed(self) -> bool:
        return all(g.passed for g in self.grades)


@dataclass
class SequenceResult:
    sequence: AlarmSequence
    end_state: list[Grade]
    drafts: list[DraftRecord]
    seconds: float

    def failures(self) -> list[Grade]:
        return [g for g in self.end_state if not g.passed] + [g for d in self.drafts for g in d.grades if not g.passed]


def _assist_reason(session: Any, run_id: str | None) -> str:
    from noc_agents.db.models import AgentRunRow

    run = session.get(AgentRunRow, run_id) if run_id else None
    if run is None or not run.steps:
        return "assist run not recorded"
    return run.steps[0].rationale or ""


def run_sequence(session: Any, settings: Any, sequence: AlarmSequence, suite: EvalSuite, *, trials: int = 1) -> SequenceResult:
    """Replay one sequence on ``session`` (a brand-new database), grade the end state and the drafts."""
    from sqlalchemy import select

    from noc_agents.db.models import IncidentRow
    from noc_agents.domain.schemas import EventIngest
    from noc_agents.graph.pipeline import process_event
    from noc_agents.llm.assist import draft_exec_brief

    cfg = settings.operator
    if (cfg.operator_id, cfg.autonomy_level) != (suite.operator, suite.autonomy_level):
        raise FixtureError(
            f"the expectations are for {suite.operator}/{suite.autonomy_level}; "
            f"this run is {cfg.operator_id}/{cfg.autonomy_level}"
        )
    started = time.perf_counter()
    for event in sequence.events:
        process_event(session, settings, EventIngest(**event))
    incidents = session.scalars(
        select(IncidentRow).where(IncidentRow.operator_id == cfg.operator_id).order_by(IncidentRow.incident_number)
    ).all()
    end_state = grade_end_state(list(incidents), sequence.expect, cfg, suite.next_update_minutes)
    drafts: list[DraftRecord] = []
    for inc in incidents:
        number = inc.incident_number
        for trial in range(1, trials + 1):
            response = draft_exec_brief(session, settings, inc)
            source = str(response.get("source"))
            subject = f"{number} draft {trial}/{trials}"
            grades, findings, ungraded = grade_draft(inc, cfg, str(response.get("body") or ""), subject)
            drafts.append(
                DraftRecord(
                    sequence_id=sequence.id,
                    incident_number=number,
                    trial=trial,
                    source=source,
                    model=response.get("model"),
                    fallback_reason=None if source == "llm" else _assist_reason(session, response.get("run_id")),
                    body=str(response.get("body") or ""),
                    findings=findings,
                    ungraded=ungraded,
                    grades=grades,
                )
            )
    return SequenceResult(sequence, end_state, drafts, time.perf_counter() - started)


# ---------------------------------------------------------------------------------------------
# Fresh databases.
# ---------------------------------------------------------------------------------------------
def sqlite_file(url: str) -> Path | None:
    if not url.startswith("sqlite:///") or url.endswith(":memory:"):
        return None
    rest = url.removeprefix("sqlite:///")
    return (ROOT / rest[2:]).resolve() if rest.startswith("./") else Path(rest).resolve()


def is_under(path: Path, parent: Path) -> bool:
    child, root = os.path.normcase(str(path.resolve())), os.path.normcase(str(parent.resolve()))
    return child == root or child.startswith(root.rstrip(os.sep) + os.sep)


def refuse_data_dir(url: str, what: str) -> None:
    path = sqlite_file(url)
    if path is not None and is_under(path, DATA_DIR):
        raise Refused(f"{what} points under the repository's data folder ({path}); the eval never opens it")


def _build_template(path: Path) -> None:
    """One migrated, empty database; every sequence starts from a copy of it (init once, not 27 times)."""
    from noc_agents.db.models import init_db

    url = f"sqlite:///{path.as_posix()}"
    refuse_data_dir(url, "the template database")
    init_db(url).dispose()


def _copy_database(source: Path, target: Path) -> None:
    """SQLite's backup API: a consistent copy of a WAL-mode file, sidecars included."""
    src, dst = sqlite3.connect(source), sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


@contextmanager
def fresh_database(workdir: Path, name: str, template: Path | None = None) -> Iterator[tuple[Any, Any]]:
    """``(settings, session)`` on a new file, bound the way ``tests/conftest.py`` ``tmp_db`` binds one."""
    from noc_agents.config import clear_settings_cache, get_settings
    from noc_agents.db.models import get_session, init_db

    path = workdir / f"{name}.db"
    if template is not None:
        _copy_database(template, path)
    url = f"sqlite:///{path.as_posix()}"
    refuse_data_dir(url, "the sequence database")
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    clear_settings_cache()
    settings = get_settings().model_copy(update={"database_url": url})
    engine = init_db(url)
    session = get_session()
    try:
        yield settings, session
    finally:
        session.close()
        engine.dispose()  # Windows will not delete an open SQLite file
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        clear_settings_cache()


# ---------------------------------------------------------------------------------------------
# The report.
# ---------------------------------------------------------------------------------------------
@dataclass
class SuiteReport:
    results: list[SequenceResult]
    trials: int
    llm_enabled: bool
    seconds: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def drafts(self) -> list[DraftRecord]:
        return [d for r in self.results for d in r.drafts]

    def end_state_failures(self) -> list[tuple[str, Grade]]:
        return [(r.sequence.id, g) for r in self.results for g in r.end_state if not g.passed]

    def draft_failures(self) -> list[tuple[str, Grade]]:
        return [(d.sequence_id, g) for d in self.drafts for g in d.grades if not g.passed]

    def failure_lines(self) -> list[str]:
        return [f"{sid}: {g.grader} FAILED for {g.subject}: {g.detail}" for sid, g in self.end_state_failures() + self.draft_failures()]

    def grader_totals(self) -> dict[str, tuple[int, int]]:
        totals: dict[str, list[int]] = {name: [0, 0] for name in (*END_STATE_GRADERS, *DRAFT_GRADERS)}
        grades = [g for r in self.results for g in r.end_state] + [g for d in self.drafts for g in d.grades]
        for g in grades:
            totals[g.grader][0] += int(g.passed)
            totals[g.grader][1] += 1
        return {name: (p, n) for name, (p, n) in totals.items()}

    def pass_hat_k(self) -> tuple[int, int]:
        """Tasks (one incident of one sequence) whose k drafts all pass every draft grader."""
        tasks: dict[tuple[str, str], bool] = {}
        for d in self.drafts:
            key = (d.sequence_id, d.incident_number)
            tasks[key] = tasks.get(key, True) and d.passed
        return sum(tasks.values()), len(tasks)

    def model_drafted(self) -> tuple[int, int]:
        llm = [d for d in self.drafts if d.source == "llm"]
        return sum(d.passed for d in llm), len(llm)

    def fallback(self) -> tuple[int, int, Counter]:
        template = [d for d in self.drafts if d.source != "llm"]
        return len(template), len(self.drafts), Counter(d.fallback_reason or "(no reason)" for d in template)

    def fallback_rate(self) -> float:
        fell_back, attempted, _ = self.fallback()
        return fell_back / attempted if attempted else 0.0

    def ungraded_counts(self) -> Counter:
        return Counter(_code(f) for d in self.drafts for f in d.ungraded)

    def fallback_alarm(self) -> bool:
        return self.llm_enabled and self.fallback_rate() > FALLBACK_ALARM_RATE

    def passed(self) -> bool:
        return not self.end_state_failures() and not self.draft_failures() and not self.fallback_alarm()

    def format(self) -> str:
        incidents = sum(len(r.sequence.expect) for r in self.results)
        mode = "LLM on: model drafts" if self.llm_enabled else "LLM off: deterministic template path"
        lines = [
            f"Draft eval: {len(self.results)} sequences, {incidents} expected incidents, {len(self.drafts)} drafts "
            f"(k={self.trials}); {mode}; {self.seconds:.1f} s",
            "End-state graders",
        ]
        totals = self.grader_totals()
        lines += [f"  {name:<26}{p}/{n}" for name, (p, n) in totals.items() if name in END_STATE_GRADERS]
        lines.append(f"Draft graders (assist function: {ASSIST_FUNCTION})")
        lines += [f"  {name:<26}{p}/{n}" for name, (p, n) in totals.items() if name in DRAFT_GRADERS]
        passing, tasks = self.pass_hat_k()
        share = f"{100.0 * passing / tasks:.1f} %" if tasks else "n/a"
        lines.append(f"pass^{self.trials} over sendable drafts: {passing}/{tasks} tasks ({share})")
        ok, llm = self.model_drafted()
        lines.append(f"Model-drafted: {llm} of {len(self.drafts)} drafts; passing every grader: {ok}/{llm}" if llm else "Model-drafted: none")
        fell_back, attempted, reasons = self.fallback()
        verdict = "ALARM (> 20 %, §1.2 M11)" if self.fallback_alarm() else ("expected with the LLM off" if not self.llm_enabled else "within M11")
        lines.append(f"Template fallback ({ASSIST_FUNCTION}): {fell_back}/{attempted} = {100.0 * self.fallback_rate():.1f} % -- {verdict}")
        lines += [f"  {count} x {reason}" for reason, count in reasons.most_common()]
        ungraded = self.ungraded_counts()
        if ungraded:
            lines.append("Recorded, not scored:")
            lines += [f"  {code} x{count} -- {UNGRADED_FINDINGS[code]}" for code, count in sorted(ungraded.items())]
        lines.append(
            "Model judge: not built (MODEL-JUDGE SEAM in tests/eval/draft_eval.py); "
            "human spot checks read the --json transcript"
        )
        lines += self.notes
        failures = self.failure_lines()
        lines.append("Failures:" if failures else "Failures: none")
        lines += [f"  {line}" for line in failures]
        return "\n".join(lines)

    def to_json(self) -> dict[str, Any]:
        def grade(g: Grade) -> dict[str, Any]:
            return {"grader": g.grader, "subject": g.subject, "passed": g.passed, "detail": g.detail}

        passing, tasks = self.pass_hat_k()
        fell_back, attempted, reasons = self.fallback()
        return {
            "llm_enabled": self.llm_enabled,
            "trials": self.trials,
            "passed": self.passed(),
            "summary": {
                "graders": {name: {"passed": p, "total": n} for name, (p, n) in self.grader_totals().items()},
                "pass_hat_k": {"k": self.trials, "passing_tasks": passing, "tasks": tasks},
                "template_fallback": {
                    "assist_function": ASSIST_FUNCTION,
                    "fell_back": fell_back,
                    "attempted": attempted,
                    "rate": self.fallback_rate(),
                    "alarm": self.fallback_alarm(),
                    "reasons": dict(reasons),
                },
                "recorded_not_scored": dict(self.ungraded_counts()),
                "model_judge": "not built",
            },
            "sequences": [
                {
                    "id": r.sequence.id,
                    "title": r.sequence.title,
                    "seconds": round(r.seconds, 3),
                    "end_state": [grade(g) for g in r.end_state],
                    "drafts": [
                        {
                            "incident_number": d.incident_number,
                            "trial": d.trial,
                            "source": d.source,
                            "model": d.model,
                            "fallback_reason": d.fallback_reason,
                            "body": d.body,
                            "findings": d.findings,
                            "grades": [grade(g) for g in d.grades],
                        }
                        for d in r.drafts
                    ],
                }
                for r in self.results
            ],
        }


def planned_model_calls(sequences: Iterable[AlarmSequence], trials: int) -> int:
    """One drafting call per expected incident per trial (an upper bound on what leaves the box)."""
    return sum(len(s.expect) for s in sequences) * trials


def trials_from_env(default: int = 1) -> int:
    return max(1, int(os.getenv(TRIALS_ENV) or default))


def max_calls_from_env() -> int:
    return int(os.getenv(MAX_CALLS_ENV) or DEFAULT_MAX_MODEL_CALLS)


def run_suite(
    workdir: Path,
    *,
    suite: EvalSuite | None = None,
    trials: int = 1,
    sequence_ids: Iterable[str] | None = None,
    max_model_calls: int = DEFAULT_MAX_MODEL_CALLS,
    progress: Callable[[SequenceResult], None] | None = None,
) -> SuiteReport:
    """Every selected sequence on its own new database under ``workdir``."""
    from noc_agents.llm.client import llm_enabled

    suite = suite or load_suite()
    selected = suite.select(list(sequence_ids) if sequence_ids else None)
    llm_on = llm_enabled()
    planned = planned_model_calls(selected, trials)
    if llm_on and planned > max_model_calls:
        raise Refused(
            f"{planned} model calls planned ({planned // trials} incidents x {trials} trials); the limit is {max_model_calls} "
            f"(--max-model-calls / {MAX_CALLS_ENV}). The on-demand assist path writes no llm_calls row, so "
            "LLM_MONTHLY_BUDGET_USD does not see these calls."
        )
    if is_under(workdir, DATA_DIR):
        raise Refused(f"work folder {workdir} is under the repository's data folder")
    started = time.perf_counter()
    template = workdir / "_template.db"
    _build_template(template)
    results: list[SequenceResult] = []
    for sequence in selected:
        with fresh_database(workdir, sequence.id, template) as (settings, session):
            results.append(run_sequence(session, settings, sequence, suite, trials=trials))
        if progress is not None:
            progress(results[-1])
    report = SuiteReport(results=results, trials=trials, llm_enabled=llm_on, seconds=time.perf_counter() - started)
    if llm_on:
        report.notes.append(f"Model calls attempted: up to {planned} (one per draft)")
    return report


# ---------------------------------------------------------------------------------------------
# Command line (the nightly entry point).
# ---------------------------------------------------------------------------------------------
def _load_suite_environment() -> None:
    """The suite's own pins from tests/conftest.py, with the LLM switches put back from the shell."""
    kept = {key: os.environ.get(key) for key in KEEP_FROM_SHELL}
    spec = importlib.util.spec_from_file_location("_draft_eval_conftest", CONFTEST_PATH)
    if spec is None or spec.loader is None:
        raise Refused(f"cannot load {CONFTEST_PATH}")
    spec.loader.exec_module(importlib.util.module_from_spec(spec))
    for key, value in kept.items():
        if value is not None:
            os.environ[key] = value
    os.environ.update(EXTRA_PINS)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # a legacy Windows code page cannot print "→" or "—"
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(description="Nightly draft eval over tests/fixtures/eval/alarm_sequences.yaml")
    parser.add_argument("--trials", type=int, default=None, help=f"drafts per incident, k in pass^k (default {TRIALS_ENV} or 1)")
    parser.add_argument("--sequence", action="append", help="run only this sequence id (repeatable)")
    parser.add_argument("--json", type=Path, default=None, help="write the full transcript and summary here")
    parser.add_argument("--template-only", action="store_true", help="run on the deterministic template path with the LLM off")
    parser.add_argument("--max-model-calls", type=int, default=None, help=f"refuse to plan more calls (default {MAX_CALLS_ENV} or {DEFAULT_MAX_MODEL_CALLS})")
    parser.add_argument("--tmp-dir", type=Path, default=None, help="parent folder for the throwaway databases (default: system temp)")
    parser.add_argument("--keep-tmp", action="store_true", help="keep the throwaway folder for inspection")
    args = parser.parse_args(argv)

    try:
        refuse_data_dir(os.environ.get("DATABASE_URL", ""), "DATABASE_URL")
        if args.tmp_dir is not None and is_under(args.tmp_dir, DATA_DIR):
            raise Refused(f"--tmp-dir {args.tmp_dir} is under the repository's data folder")
        _load_suite_environment()
        if args.template_only:
            os.environ["LLM_ENABLED"] = "false"
        from noc_agents.llm.client import llm_enabled  # after the pins: nothing imported noc_agents before

        if not llm_enabled() and not args.template_only:
            print(
                "draft_eval: skipped -- LLM_ENABLED is not true in this shell. Export LLM_ENABLED=true and "
                "ANTHROPIC_API_KEY for the nightly run, or pass --template-only for the deterministic path."
            )
            return 3
        suite = load_suite()
        trials = args.trials if args.trials is not None else trials_from_env()
        max_calls = args.max_model_calls if args.max_model_calls is not None else max_calls_from_env()
        if args.tmp_dir is not None:
            args.tmp_dir.mkdir(parents=True, exist_ok=True)
        workdir = Path(tempfile.mkdtemp(prefix="draft_eval_", dir=args.tmp_dir))
    except (Refused, ValueError) as exc:  # FixtureError and pydantic's ValidationError are ValueErrors
        sys.stdout.flush()
        print(f"draft_eval: refused: {exc}", file=sys.stderr)
        return 2

    os.environ["LEDGER_DIR"] = str(workdir / "shift_ledgers")
    try:
        report = run_suite(
            workdir,
            suite=suite,
            trials=max(1, trials),
            sequence_ids=args.sequence,
            max_model_calls=max_calls,
            progress=lambda r: print(f"  {r.sequence.id:<40}{'ok' if not r.failures() else 'FAILED'}  {r.seconds:.1f} s", flush=True),
        )
    except (Refused, FixtureError) as exc:
        sys.stdout.flush()
        print(f"draft_eval: refused: {exc}", file=sys.stderr)
        return 2
    finally:
        if args.keep_tmp:
            print(f"(throwaway folder kept: {workdir})")
        else:
            shutil.rmtree(workdir, ignore_errors=True)
    print(report.format())
    if args.json is not None:
        args.json.write_text(json.dumps(report.to_json(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        print(f"transcript written to {args.json}")
    return 0 if report.passed() else 1


if __name__ == "__main__":
    sys.exit(main())
