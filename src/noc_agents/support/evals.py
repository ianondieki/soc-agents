"""The support desk's eval suite: the golden set through the real pipeline, scored against the
contract's definitions (docs/SUPPORT_DESK.md "Evals").

**How a run works.** Every case goes through :func:`desk.process_complaint` -- the function the
API calls, not a test double -- on its own throwaway in-memory SQLite database: a template is
built once per run (every table, plus the open storm incidents below) and copied per case with
SQLite's backup API (about a millisecond), so no case can see another's complaints (dedupe,
repeat counts and the re-credit cooldown are per-customer state) and nothing touches the live
database or the live event hub. The template's incidents are the rain-storm scenario's
top-level sites (``services/scenarios.RAIN_STORM_EVENTS`` without a parent hub), open, so
``link_incident`` cases have real tickets to find: Nakuru, Eldoret, Thika, Embakasi, Nairobi East.
The account fixtures are the shipped ``config/support/accounts.yaml``; their times are relative,
so a run is the same today as next year.

**Definitions** (repeated from the contract; "escalated" means the desk's final route is
``human``, "resolvable" means the gold route is ``resolver`` or ``action``):

* ``resolution_rate``        = cases resolved correctly without a person / resolvable cases.
  Correct: the right route and, for ``resolver``, the cited article (the one the reply came
  from) is in the gold ``article_ids``; for ``action``, the gold tool was the call the action
  agent made and it succeeded (status ``ok``, so its arguments validated).
* ``wrong_escalation_rate``  = escalations of resolvable cases / all escalations.
* ``missed_escalation_rate`` = gold ``human`` cases the desk kept / gold ``human`` cases.
* ``safety_missed_escalation_rate`` = the same over gold ``human`` cases marked ``safety``
  (fraud, legal, threats). Gated at exactly 0: a missed safety escalation is the dangerous error.
* ``containment_rate``       = cases closed without a person / all cases.
* ``triage_accuracy``        = category matches gold / all cases.
* ``routing_accuracy``       = final route matches gold / all cases.
* ``grounded_answer_rate``   = resolver answers citing a gold article / resolver answers.
* ``tool_accuracy``          = cases whose action-agent call is the gold tool / cases with a gold tool
  (whatever the call's status: an over-limit refund that chose ``issue_refund`` chose right).
* ``escalation_reason_accuracy`` = gold ``human`` cases escalated with the gold reason code /
  gold ``human`` cases. Not in the contract's list; reported because a right route for the
  wrong reason is still a defect, and the failure kinds have no slot for it.
* ``p50_ms``                 = median wall time of ``process_complaint`` per case.

A rate whose denominator is empty is ``null`` everywhere (``metrics``, ``by_split``,
``by_category``): a split with no safety case has not shown a safety-missed rate of 0, it has
shown nothing, so a gate whose metric is ``null`` **fails** and says why in its ``note``.

**Three splits, two files, one headline.** ``golden.jsonl`` holds ``dev`` (the starter set and its
growth, written beside the rules) and ``validation`` (written blind by the eval author from the
contract and the policy, frozen, scored, and then *seen*: its failures were read, so it is now a
development set too). ``holdout_blind.jsonl`` holds ``holdout``: written by a different author with
no access to the code, scored once before and once after each tuning round, never tuned against.
A full run reports every split in ``by_split`` and takes the **holdout** as the headline
(``metrics``, ``gates``, ``passed``, ``confusion``, ``by_category``, ``failures``;
``dataset.split == "holdout"``). The headline is *reported*, never asserted: a held-out set that
becomes a CI target stops being held out. The regression gate pytest asserts is the contract's
default gates on ``dev + validation`` combined (:data:`REGRESSION_SPLITS`), the sets the desk is
developed against. A holdout case marked ``"contested": true`` (an orchestrator adjudication,
recorded in the file) is loaded but never scored; ``dataset.excluded`` counts them.

**The eval's incidents.** Every case runs against the same open incidents: the rain-storm
scenario's top-level sites (Nakuru, Eldoret, Thika, Embakasi East, Nairobi East) plus four
adjudicated eval-only tickets (Westlands, Rongai, Nyali/Mombasa, Machakos:
:data:`EVAL_EXTRA_INCIDENTS`), added because the blind holdout's author labelled outages in those
towns as linkable and the operator profile's own regions serve them. The live demo database is
not touched by this list.

**The golden set** (``tests/fixtures/support_eval/golden.jsonl``), one JSON object per line::

    {"id": "mpesa-reversal-en-01", "split": "dev" | "validation" | "holdout", "text": "...", "msisdn": "0700000412",
     "language": "en" | "sw" | "mixed",
     "expected": {"category": "mpesa", "route": "resolver" | "action" | "human",
                  "tool": "reverse_mpesa" | null, "article_ids": ["KB-..."],
                  "escalation_reason": "<reason_code>" | null, "safety": false}}

``route`` is ``human`` exactly when ``escalation_reason`` is set; ``safety`` is true exactly for
the three safety reasons; ``article_ids`` lists every acceptable article for a ``resolver``
case (empty otherwise); ``tool`` is the action agent's call for an ``action`` case, and for a
``human`` case whose escalation comes from a tool (``over_refund_limit``, ``tool_failed``).
:func:`load_golden` refuses a line that breaks any of these, so a mislabelled case fails loudly
instead of quietly moving a metric. Lines starting with ``#`` and blank lines are skipped.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import statistics
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from noc_agents.config import ROOT
from noc_agents.db.models import Base, IncidentRow, new_id, utcnow
from noc_agents.db.models_support import SupportEvalRunRow, SupportToolCallRow
from noc_agents.services.clock import iso_z
from noc_agents.support.context import SupportContext, default_context
from noc_agents.support.desk import process_complaint
from noc_agents.support.tools import TOOLS
from noc_agents.support.vocab import CATEGORIES, LANGUAGES, REASON_CODES, ROUTES, SAFETY_REASONS

GOLDEN_PATH = ROOT / "tests" / "fixtures" / "support_eval" / "golden.jsonl"
HOLDOUT_PATH = ROOT / "tests" / "fixtures" / "support_eval" / "holdout_blind.jsonl"
#: The whole golden set: dev and validation in one file, the blind holdout in its own (provenance).
GOLDEN_PATHS: tuple[Path, ...] = (GOLDEN_PATH, HOLDOUT_PATH)
DATASET_NAME = "support_golden"
SPLITS: tuple[str, ...] = ("dev", "validation", "holdout")
#: The split a full run REPORTS as its headline (see :func:`run_eval`): the blind holdout.
HEADLINE_SPLIT = "holdout"
#: The splits the pytest gate ASSERTS the default gates on: the sets the desk is developed against.
REGRESSION_SPLITS: tuple[str, ...] = ("dev", "validation")
#: Tools that are bookkeeping around the action agent's real call, never "the" call.
_BOOKKEEPING_TOOLS: frozenset[str] = frozenset({"lookup_account", "update_ticket"})
_LABELS: tuple[str, ...] = ("resolver", "action", "human")


class GoldenSetError(ValueError):
    """A golden line is malformed or self-contradictory; the message names the line."""


@dataclass(frozen=True)
class Expected:
    category: str
    route: str
    tool: str | None
    article_ids: tuple[str, ...]
    escalation_reason: str | None
    safety: bool


@dataclass(frozen=True)
class GoldenCase:
    id: str
    split: str
    text: str
    msisdn: str
    language: str
    expected: Expected
    #: An adjudicated exclusion (loaded, listed, never scored); the file's ``note`` says why.
    contested: bool = False


@dataclass(frozen=True)
class CaseResult:
    case: GoldenCase
    category: str
    route: str
    status: str
    tool: str | None
    tool_status: str | None
    article_id: str | None
    reason_code: str | None
    ms: float

    @property
    def escalated(self) -> bool:
        return self.route == "human"

    @property
    def resolvable(self) -> bool:
        return self.case.expected.route in ("resolver", "action")

    @property
    def correct(self) -> bool:
        """Resolved correctly without a person (the resolution-rate numerator)."""
        exp = self.case.expected
        if exp.route != self.route:
            return False
        if exp.route == "resolver":
            return self.article_id in exp.article_ids
        if exp.route == "action":
            return self.tool == exp.tool and self.tool_status == "ok"
        return False

    def actual(self) -> dict[str, Any]:
        return {"category": self.category, "route": self.route, "status": self.status, "tool": self.tool,
                "tool_status": self.tool_status, "article_id": self.article_id, "escalation_reason": self.reason_code}


# ---------------------------------------------------------------------------- the golden set


def _expected(raw: dict[str, Any], where: str, *, contested: bool = False) -> Expected:
    """``raw`` validated against the consistency rules. A contested case (an adjudicated exclusion that
    is never scored) may name a bookkeeping tool: the file keeps the author's label as written."""
    exp = Expected(
        category=raw.get("category"),
        route=raw.get("route"),
        tool=raw.get("tool"),
        article_ids=tuple(raw.get("article_ids") or ()),
        escalation_reason=raw.get("escalation_reason"),
        safety=bool(raw.get("safety", False)),
    )
    problems = []
    if exp.category not in CATEGORIES:
        problems.append(f"category {exp.category!r}")
    if exp.route not in ROUTES:
        problems.append(f"route {exp.route!r}")
    if exp.tool is not None and (exp.tool not in TOOLS or (exp.tool in _BOOKKEEPING_TOOLS and not contested)):
        problems.append(f"tool {exp.tool!r}")
    if exp.escalation_reason is not None and exp.escalation_reason not in REASON_CODES:
        problems.append(f"escalation_reason {exp.escalation_reason!r}")
    if (exp.route == "human") != (exp.escalation_reason is not None):
        problems.append("route is 'human' exactly when escalation_reason is set")
    if exp.safety != (exp.escalation_reason in SAFETY_REASONS):
        problems.append("safety must be true exactly for the safety escalation reasons")
    if exp.route == "resolver" and not exp.article_ids:
        problems.append("a resolver case needs at least one accepted article id")
    if exp.route == "action" and exp.tool is None:
        problems.append("an action case needs its tool")
    if problems:
        raise GoldenSetError(f"{where}: " + "; ".join(problems))
    return exp


def _splits_wanted(split: str | Sequence[str] | None) -> tuple[str, ...] | None:
    """``None`` (every split), one name, ``"dev+validation"``, or a sequence of names."""
    if split is None:
        return None
    names = tuple(split.split("+")) if isinstance(split, str) else tuple(split)
    bad = [n for n in names if n not in SPLITS]
    if bad or not names:
        raise GoldenSetError(f"unknown split {bad or names}: choose from {SPLITS}")
    return names


def load_golden(path: Path | Sequence[Path] = GOLDEN_PATHS, *, split: str | Sequence[str] | None = None,
                ) -> tuple[list[GoldenCase], str]:
    """The cases of one file or several (optionally only some splits) and the dataset version: the
    first 12 hex of the sha256 over the files' bytes, in order. Contested cases are returned with
    ``contested=True``; :func:`run_eval` leaves them out of every number."""
    paths = (path,) if isinstance(path, Path) else tuple(path)
    wanted = _splits_wanted(split)
    digest = hashlib.sha256()
    cases: list[GoldenCase] = []
    seen: set[str] = set()
    for file in paths:
        raw_bytes = file.read_bytes()
        digest.update(raw_bytes)
        for number, line in enumerate(raw_bytes.decode("utf-8").splitlines(), start=1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            where = f"{file.name}:{number}"
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise GoldenSetError(f"{where}: not JSON ({exc.msg})") from None
            contested = bool(raw.get("contested", False))
            case = GoldenCase(
                id=str(raw.get("id") or ""), split=raw.get("split"), text=str(raw.get("text") or ""),
                msisdn=str(raw.get("msisdn") or ""), language=raw.get("language"),
                expected=_expected(raw.get("expected") or {}, where, contested=contested), contested=contested,
            )
            if not case.id or case.id in seen:
                raise GoldenSetError(f"{where}: id missing or duplicated")
            if case.split not in SPLITS or case.language not in LANGUAGES:
                raise GoldenSetError(f"{where}: split must be one of {SPLITS} and language one of {LANGUAGES}")
            seen.add(case.id)
            if wanted is None or case.split in wanted:
                cases.append(case)
    return cases, digest.hexdigest()[:12]


# ------------------------------------------------------------------------- isolated databases


#: Eval-only open incidents beside the storm's (orchestrator adjudication 2(b), 2026-10-04): the
#: blind holdout labels outages in these towns as ``link_incident``, and the operator profile's
#: regions serve them (Westlands and Rongai: Nairobi West; Nyali: Coast; Machakos: Nairobi East).
#: ``(site_id, site_name, site_type, region_code, county, failure_domain, users_affected)``.
EVAL_EXTRA_INCIDENTS: tuple[tuple[str, str, str, str, str, str, int], ...] = (
    ("SFC-NBIW-HUB-WLD", "Westlands Nairobi West HUB", "HUB", "NBI_W", "Nairobi", "power", 160000),
    ("SFC-NBIW-ENB-RNG03", "Ongata Rongai eNodeB 03", "ENODEB", "NBI_W", "Kajiado", "transmission_mw", 21000),
    ("SFC-CST-HUB-NYL", "Nyali Coast HUB", "HUB", "CST", "Mombasa", "power", 140000),
    ("SFC-NBIE-HUB-MKS", "Machakos Town HUB", "HUB", "NBI_E", "Machakos", "transmission_fiber", 95000),
)


def _eval_incidents(operator_id: str) -> list[IncidentRow]:
    """The rain-storm scenario's top-level sites (children ride on their hub) plus
    :data:`EVAL_EXTRA_INCIDENTS`, all open."""
    from noc_agents.services.scenarios import RAIN_STORM_EVENTS

    hubs = [e for e in RAIN_STORM_EVENTS if not e.parent_hub_id]
    rows = [
        IncidentRow(
            operator_id=operator_id, incident_number=f"INC{n:06d}", status="AWAITING_VENDOR", priority="P2",
            site_id=e.site_id, site_name=e.site_name, site_type=e.site_type, region_code=e.region_code,
            county=e.county, title=f"{e.failure_domain} - {e.site_name}", description=e.description or "",
            correlation_fingerprint=f"eval:{e.site_id}", users_affected=e.users_affected or 0,
        )
        for n, e in enumerate(hubs, start=1)
    ]
    for n, (site_id, name, kind, region, county, domain, users) in enumerate(EVAL_EXTRA_INCIDENTS, start=len(rows) + 1):
        rows.append(IncidentRow(
            operator_id=operator_id, incident_number=f"INC{n:06d}", status="AWAITING_VENDOR", priority="P2",
            site_id=site_id, site_name=name, site_type=kind, region_code=region, county=county,
            title=f"{domain} - {name}", description="eval scenario: adjudicated open incident for the blind holdout",
            correlation_fingerprint=f"eval:{site_id}", users_affected=users,
        ))
    return rows


_storm_incidents = _eval_incidents  # the name tests and older callers know


class IsolatedDatabases:
    """A template in-memory database, copied fresh for every case."""

    def __init__(self, operator_id: str) -> None:
        from noc_agents.db import models_all  # noqa: F401  (registers every table on Base)

        self._template = sqlite3.connect(":memory:", check_same_thread=False)
        # Kept, not disposed, until close(): disposing a StaticPool engine closes its one
        # connection, and that connection IS the template.
        self._engine = create_engine("sqlite://", poolclass=StaticPool, creator=lambda: self._template)
        Base.metadata.create_all(self._engine)
        with Session(self._engine) as session:
            session.add_all(_eval_incidents(operator_id))
            session.commit()

    @contextmanager
    def session(self) -> Iterator[Session]:
        copy = sqlite3.connect(":memory:", check_same_thread=False)
        self._template.backup(copy)
        engine = create_engine("sqlite://", poolclass=StaticPool, creator=lambda: copy)
        session = sessionmaker(bind=engine, autoflush=False, future=True)()
        try:
            yield session
        finally:
            session.close()
            engine.dispose()
            copy.close()

    def close(self) -> None:
        self._engine.dispose()
        self._template.close()


# ------------------------------------------------------------------------------- one case


def run_case(session: Session, case: GoldenCase, *, operator_id: str, ctx: SupportContext,
             port: Any | None = None) -> CaseResult:
    """Process one golden case and read back what the desk decided."""
    started = perf_counter()
    row = process_complaint(session, operator_id=operator_id, body=case.text, msisdn=case.msisdn,
                            ctx=ctx, port=port, emit_events=False).complaint
    ms = (perf_counter() - started) * 1000
    calls = session.scalars(
        select(SupportToolCallRow).where(SupportToolCallRow.complaint_id == row.id).order_by(SupportToolCallRow.at)
    ).all()
    primary = next((c for c in calls if c.tool not in _BOOKKEEPING_TOOLS), None)
    citations = json.loads(row.citations_json or "[]")
    return CaseResult(
        case=case, category=row.category, route=row.route, status=row.status,
        tool=primary.tool if primary else None, tool_status=primary.status if primary else None,
        article_id=citations[0]["article_id"] if citations else None,
        reason_code=row.escalation_reason_code, ms=ms,
    )


# ---------------------------------------------------------------------------------- scoring


def _rate(numerator: int, denominator: int) -> float | None:
    """A rate; an empty denominator is ``None`` (JSON ``null``), never a number. A split with no
    safety case has not shown a safety-missed rate of 0 -- it has shown nothing, and a gate that
    reads ``None`` fails (:func:`score`)."""
    return round(numerator / denominator, 4) if denominator else None


_rate_or_none = _rate  # the per-category table uses the same rule


def _metrics(results: list[CaseResult]) -> dict[str, float | None]:
    resolvable = [r for r in results if r.resolvable]
    escalated = [r for r in results if r.escalated]
    gold_human = [r for r in results if r.case.expected.route == "human"]
    safety = [r for r in gold_human if r.case.expected.safety]
    answers = [r for r in results if r.route == "resolver" and r.status == "answered"]
    with_tool = [r for r in results if r.case.expected.tool]
    return {
        "resolution_rate": _rate(sum(r.correct for r in resolvable), len(resolvable)),
        "wrong_escalation_rate": _rate(sum(r.resolvable for r in escalated), len(escalated)),
        "missed_escalation_rate": _rate(sum(not r.escalated for r in gold_human), len(gold_human)),
        "safety_missed_escalation_rate": _rate(sum(not r.escalated for r in safety), len(safety)),
        "containment_rate": _rate(sum(not r.escalated for r in results), len(results)),
        "triage_accuracy": _rate(sum(r.category == r.case.expected.category for r in results), len(results)),
        "routing_accuracy": _rate(sum(r.route == r.case.expected.route for r in results), len(results)),
        "grounded_answer_rate": _rate(sum(r.article_id in r.case.expected.article_ids for r in answers), len(answers)),
        "tool_accuracy": _rate(sum(r.tool == r.case.expected.tool for r in with_tool), len(with_tool)),
        "escalation_reason_accuracy": _rate(
            sum(r.reason_code == r.case.expected.escalation_reason for r in gold_human), len(gold_human)),
        "p50_ms": round(statistics.median(r.ms for r in results), 2) if results else 0.0,
    }


def _confusion(results: list[CaseResult]) -> dict[str, Any]:
    matrix = [[0] * len(_LABELS) for _ in _LABELS]
    for r in results:
        matrix[_LABELS.index(r.case.expected.route)][_LABELS.index(r.route)] += 1
    return {"labels": list(_LABELS), "matrix": matrix}


def _by_category(results: list[CaseResult]) -> list[dict[str, Any]]:
    rows = []
    for category in CATEGORIES:
        group = [r for r in results if r.case.expected.category == category]
        if not group:
            continue
        resolvable = [r for r in group if r.resolvable]
        escalated = [r for r in group if r.escalated]
        rows.append({
            "category": category,
            "n": len(group),
            "resolution_rate": _rate_or_none(sum(r.correct for r in resolvable), len(resolvable)),
            "wrong_escalation_rate": _rate_or_none(sum(r.resolvable for r in escalated), len(escalated)),
            "triage_accuracy": _rate_or_none(sum(r.category == category for r in group), len(group)),
        })
    return rows


def failure_kinds(r: CaseResult) -> list[str]:
    """Every contract failure kind this case shows, in a fixed order."""
    exp, kinds = r.case.expected, []
    if r.resolvable and r.escalated:
        kinds.append("wrong_escalation")
    if exp.route == "human" and not r.escalated:
        kinds.append("missed_escalation")
    if r.route != exp.route and not kinds:
        kinds.append("wrong_route")
    if r.category != exp.category:
        kinds.append("wrong_category")
    if exp.route == "resolver" and r.route == "resolver" and r.article_id not in exp.article_ids:
        kinds.append("wrong_article")
    if exp.tool and r.tool != exp.tool:
        kinds.append("wrong_tool")
    if r.resolvable and not r.correct and not kinds:
        kinds.append("unresolved")
    return kinds


def _failures(results: list[CaseResult]) -> list[dict[str, Any]]:
    out = []
    for r in results:
        expected = asdict(r.case.expected)
        expected["article_ids"] = list(r.case.expected.article_ids)
        for kind in failure_kinds(r):
            out.append({"case_id": r.case.id, "text": r.case.text[:200], "kind": kind,
                        "expected": expected, "actual": r.actual()})
    return out


def score(results: list[CaseResult], *, ctx: SupportContext) -> dict[str, Any]:
    """Metrics, gates, confusion matrix, per-category table and failures for ``results``."""
    metrics = _metrics(results)
    gates = []
    for gate in ctx.policy.eval_gates:
        value = metrics.get(gate.metric)
        note = None
        if value is None:
            passed = False
            note = (f"{gate.metric} has no case to measure (empty denominator): the gate cannot pass on no "
                    f"evidence; add cases that exercise it")
        else:
            passed = gate.passes(float(value))
        gates.append({"metric": gate.metric, "op": gate.op, "threshold": gate.threshold, "value": value,
                      "passed": passed, "note": note})
    return {
        "metrics": metrics,
        "gates": gates,
        "passed": all(g["passed"] for g in gates),
        "confusion": _confusion(results),
        "by_category": _by_category(results),
        "failures": _failures(results),
    }


# ------------------------------------------------------------------------------------ a run


def run_eval(
    *,
    operator_id: str,
    ctx: SupportContext | None = None,
    path: Path | Sequence[Path] = GOLDEN_PATHS,
    split: str | Sequence[str] | None = None,
    port: Any | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run the golden set (every split, one, or several) and return the contract's ``EvalReport``.

    ``port`` switches the run to ``mode: "llm"`` (triage tie-breaks may use the model); without
    it the run is deterministic, which is what CI, the API and the gates use.

    **Splits.** ``by_split`` holds the metrics of every split that ran. The headline (``metrics``,
    ``gates``, ``passed``, ``confusion``, ``by_category``, ``failures``) is: the splits asked for,
    combined (``split="dev+validation"`` or ``("dev", "validation")`` -> ``dataset.split ==
    "dev+validation"``); or, with ``split=None``, the blind **holdout** (``dataset.split ==
    "holdout"``) whenever the files have one, else ``"all"``. ``dataset.size`` counts the headline's
    scored cases and ``dataset.excluded`` its contested ones, which are never scored.
    """
    ctx = ctx or default_context()
    loaded, version = load_golden(path, split=split)
    wanted = _splits_wanted(split)
    cases = [c for c in loaded if not c.contested]
    databases = IsolatedDatabases(operator_id)
    try:
        results = []
        for case in cases:
            with databases.session() as session:
                results.append(run_case(session, case, operator_id=operator_id, ctx=ctx, port=port))
    finally:
        databases.close()
    by_split = {name: group for name in SPLITS if (group := [r for r in results if r.case.split == name])}
    if wanted is not None:
        headline_split, headline = "+".join(wanted), results
    elif HEADLINE_SPLIT in by_split:
        headline_split, headline = HEADLINE_SPLIT, by_split[HEADLINE_SPLIT]
    else:
        headline_split, headline = "all", results
    headline_splits = set(headline_split.split("+")) if headline_split != "all" else set(SPLITS)
    excluded = sum(1 for c in loaded if c.contested and c.split in headline_splits)
    return {
        "run_id": new_id(),
        "ran_at": iso_z(now or utcnow()),
        "mode": "llm" if port is not None else "deterministic",
        "dataset": {"name": DATASET_NAME, "version": version, "size": len(headline), "split": headline_split,
                    "excluded": excluded},
        **score(headline, ctx=ctx),
        "by_split": {name: _metrics(group) for name, group in by_split.items()},
    }


def store_report(session: Session, operator_id: str, report: dict[str, Any]) -> SupportEvalRunRow:
    """Keep ``report`` as the operator's latest (the caller commits). Every run is kept, newest wins."""
    row = SupportEvalRunRow(
        id=report["run_id"], operator_id=operator_id, ran_at=utcnow(), mode=report["mode"],
        dataset_name=report["dataset"]["name"], dataset_version=report["dataset"]["version"],
        size=report["dataset"]["size"], passed=int(bool(report["passed"])), report_json=json.dumps(report),
    )
    session.add(row)
    return row


def latest_report(session: Session, operator_id: str) -> dict[str, Any] | None:
    """The operator's most recent stored report, or None when no run has been stored."""
    row = session.scalar(
        select(SupportEvalRunRow).where(SupportEvalRunRow.operator_id == operator_id)
        .order_by(SupportEvalRunRow.ran_at.desc(), SupportEvalRunRow.id.desc()).limit(1)
    )
    return json.loads(row.report_json) if row is not None else None
