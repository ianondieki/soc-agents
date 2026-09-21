"""The G15 guard for M1 (spec §7.11.6 MEM1/MEM3, §7.11.11 tests 9-10) — the most important
test in this lane.

Memory is **advisory**. It may inform a human reading an approval card. It may never change
what the system decides. From M1 there is exactly one hot-path reader — ``agents/hitl.py``,
which freezes ``memory.advisory_for_incident()`` into
``hitl_tasks.proposed_payload_json["advisory"]`` at task creation (spec line 419) — and that
one insertion is the only place in the whole system where memory touches the golden path. So
this file exists to prove, by running the thing rather than by reading it, that the insertion
is inert.

The proof is three lifecycle runs of the **same alarm**, each in its own fresh database:

* **A — empty store, flag off.** This is today's system: the baseline every literal is
  compared against.
* **B — seeded store, flag ON.** The exit criterion §7.11.10 names: "inertness test green
  with a seeded store". Every decision field, every step row and all 26 run-scoped event
  literals must be **identical to A**, and the HITL payload identical except for the additive
  ``advisory`` key.
* **C — seeded store, flag off.** Byte-identical to A *including* the payload — because a
  store full of history must change nothing at all until somebody switches reads on.

The store is seeded with priors that would, if they were ever honoured, move every one of
those values: prior outages at the same site and fault class that were all P4, all tiny, all
fixed in twenty minutes, against an alarm the engine calls P1. If a single decision field
ever moves between A and B, something has started reading memory to decide with, and that is
the one thing §7.11 forbids outright.

The static half (test 10) is here too: a function-local import inside a branch that only
fires in production would be invisible to an attribute check and is exactly how G15 would be
breached quietly, so the guard walks each engine's AST.
"""

from __future__ import annotations

import ast
import importlib
import re
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from noc_agents.config import clear_settings_cache, get_settings
from noc_agents.db.models import (
    AgentRunRow,
    HitlTaskRow,
    IncidentRow,
    WorkNoteRow,
    get_session,
    init_db,
    utcnow,
)
from noc_agents.db.models_memory import MemoryEpisodeRow
from noc_agents.domain.schemas import EventIngest
from noc_agents.graph.pipeline import process_event
from noc_agents.memory.consolidate import consolidate_incident
from noc_agents.realtime.hub import hub
from noc_agents.services import memory as memory_service

_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

#: The P1 hub alarm the golden path is built around: 450k users on an aggregation hub, which
#: ``services/priority.py`` calls P1 and ``needs_hitl`` gates at L2_GUARDED. A P1 approval
#: card is precisely the card a memory failure must never be able to delay.
HUB_EVENT = dict(
    site_id="SFC-NBIE-HUB-EMB",
    site_name="Embakasi East Aggregation HUB",
    site_type="HUB",
    region_code="NBI_E",
    alarm_code="POWER_GRID_FAIL",
    failure_domain="POWER",
    users_affected=450000,
    access_notes="Genset not started",
)

#: §7.11.11 test 9's nine fields, minus the two SLA timestamps — those are absolute and are
#: compared as offsets from ``created_at`` (see :func:`_snapshot`).
_DECISION_FIELDS = (
    "priority",
    "assignee_type",
    "assignee_name",
    "msp_name",
    "responsible_msp",
    "requires_hitl",
    "hitl_state",
)


def _seed_history(session, *, count: int = 4) -> None:
    """Prior outages that would move every decision field if memory were ever honoured.

    All P4, all trivially small, all restored in twenty minutes, all at the same site and the
    same fault class as the incoming P1 — and one of them carrying an assignee the engine
    would not pick. They are placed **90+ days back on purpose**: ``agents/recurrence.py``
    counts incidents at the same site and domain inside ``recurrence.lookback_days`` (30), so
    history inside that window would legitimately move ``recurrence_count`` and open a problem
    record. That is a pipeline behaviour that exists today and has nothing to do with memory;
    putting the history outside the recurrence window and inside memory's 365-day one isolates
    the variable this file is actually about.
    """
    settings = get_settings()
    for i in range(count):
        ended = utcnow() - timedelta(days=90 + i * 20)
        started = ended - timedelta(minutes=20)
        inc = IncidentRow(
            operator_id="safaricom",
            # A free-form number, so seeding history never consumes a value from the
            # ``daily_sequences`` counter the lifecycle allocates from — that is what lets
            # both runs produce INC000001.
            incident_number=f"HIST-{i}",
            status="CLOSED",
            priority="P4",
            users_affected=12,
            site_id=HUB_EVENT["site_id"],
            site_name=HUB_EVENT["site_name"],
            site_type="HUB",
            region_code="NBI_E",
            failure_domain="POWER",
            alarm_code="POWER_GRID_FAIL",
            correlation_fingerprint="SFC-NBIE-HUB-EMB|POWER_GRID_FAIL|POWER",
            created_at=started,
            outage_start_at=started,
            failure_time=started,
            restored_at=ended,
            restored_source="MARK_RESTORED",
            closed_at=ended,
            resolution_code="FIELD_RESTORED",
            resolution_summary="Reset the rectifier; back in 20 minutes, no field visit",
            assignee_type="NOC",
            msp_name="SOMEONE-ELSE",
            responsible_msp="SOMEONE-ELSE",
        )
        session.add(inc)
        session.flush()
        session.add(
            WorkNoteRow(
                incident_id=inc.id,
                author="Vendor Desk",
                author_role="MSP",
                body="Rectifier reset, SERVICE RESTORED — no genset needed at this hub",
                created_at=ended,
                source="ui",
            )
        )
        session.commit()
        # Build the derived index too, so the seeded store exercises BOTH halves of the
        # bundle: the episode recall and the fault-class prior computed over memory_episodes.
        consolidate_incident(session, settings=settings, incident_id=inc.id)
    session.commit()


def _scrub_ids(value):
    """Replace uuids so two runs of the same lifecycle compare equal.

    Only ids are normalised. Everything else — statuses, rationales, tool lists, summaries —
    is compared literally, because those are exactly what memory must not be able to move.
    """
    if isinstance(value, str):
        return _UUID_RE.sub("<id>", value)
    if isinstance(value, list):
        return [_scrub_ids(v) for v in value]
    if isinstance(value, dict):
        return {k: _scrub_ids(v) for k, v in value.items()}
    return value


def _snapshot(session, inc: IncidentRow) -> dict:
    """The nine decision fields, every step row, the run status and the HITL task count."""
    run = session.scalars(
        select(AgentRunRow)
        .where(AgentRunRow.incident_id == inc.id, AgentRunRow.graph_name == "incident_lifecycle")
        .order_by(AgentRunRow.started_at.desc())
    ).first()
    assert run is not None, "the lifecycle run row is the thing being compared"
    tasks = list(session.scalars(select(HitlTaskRow).where(HitlTaskRow.incident_id == inc.id)).all())
    return {
        "decision": {f: getattr(inc, f) for f in _DECISION_FIELDS}
        | {
            # Absolute timestamps differ between two runs by construction; the SLA *band*
            # applied to the incident is the decision, and that is the offset. Rounded to
            # whole minutes because sla_due is computed from its own utcnow() a few dozen
            # microseconds after created_at; the bands are whole minutes, so a genuine change
            # of band moves this by at least one.
            "sla_ack_minutes": round((inc.sla_ack_due - inc.created_at).total_seconds() / 60),
            "sla_restore_minutes": round((inc.sla_restore_due - inc.created_at).total_seconds() / 60),
        },
        "steps": [
            (
                s.seq,
                s.node_name,
                s.agent_name,
                s.status,
                _scrub_ids(s.input_summary),
                _scrub_ids(s.output_summary),
                _scrub_ids(s.rationale),
                _scrub_ids(s.tools_called),
                s.confidence,
            )
            for s in run.steps
        ],
        "run_status": run.status,
        "recurrence_count": inc.recurrence_count,
        # MEM3: memory never creates a task and never resolves one.
        "hitl_task_count": len(tasks),
        "hitl_task_types": sorted(t.task_type for t in tasks),
    }


def _event_literals() -> list[tuple]:
    """The run-scoped event sequence, ids and timestamps removed — 26 for this event."""
    return [
        (
            e["type"],
            e["payload"].get("seq"),
            e["payload"].get("node"),
            e["payload"].get("agent"),
            e["payload"].get("status"),
            e["payload"].get("incident_number"),
        )
        for e in list(hub._history)
    ]


#: Wall-clock values that differ between any two runs of the same alarm and say nothing about
#: any decision: the envelope's ``sent`` stamp and the ``HH:MM EAT`` times rendered into the
#: SMS and e-mail drafts. They are normalised for the same reason ``_snapshot`` compares the
#: SLA dues as *offsets* — the band is the decision, the instant is not. Everything else in
#: the payload is compared literally.
_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?")
_EAT_RE = re.compile(r"\d{2}:\d{2} EAT")


def _comparable(value):
    """``_scrub_ids`` plus the volatile clock readings — what two runs may be compared on."""
    if isinstance(value, str):
        return _EAT_RE.sub("<eat>", _TS_RE.sub("<ts>", _UUID_RE.sub("<id>", value)))
    if isinstance(value, list):
        return [_comparable(v) for v in value]
    if isinstance(value, dict):
        return {k: _comparable(v) for k, v in value.items()}
    return value


def _payload(session, inc: IncidentRow) -> dict:
    """The HITL card's ``proposed_payload`` exactly as it was written, uuids and all.

    Returned raw so one test can assert on the advisory's *content*; the comparison tests
    run it through :func:`_comparable` first.
    """
    task = session.scalars(select(HitlTaskRow).where(HitlTaskRow.incident_id == inc.id)).first()
    assert task is not None, "the P1 hub alarm must open an APPROVE_BROADCAST card"
    return task.proposed_payload


def _run(tmp_path, monkeypatch, *, name: str, seed: bool, memory_on: bool) -> dict:
    """One complete lifecycle in its own database file, with or without a seeded store."""
    url = f"sqlite:///{(tmp_path / f'{name}.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("OPERATOR_PROFILE", "safaricom")
    monkeypatch.delenv("MEMORY_ENABLED", raising=False)
    clear_settings_cache()
    init_db(url)
    session = get_session()
    try:
        if seed:
            _seed_history(session)
            assert len(list(session.scalars(select(MemoryEpisodeRow)).all())) == 4, (
                "the derived index must be populated, or this test proves nothing"
            )
        if memory_on:
            # Flipped AFTER seeding: consolidation deliberately does not depend on the read
            # flag (services/memory.py, "WHERE THE FLAG IS CHECKED"), and seeding with it off
            # is what proves that.
            monkeypatch.setenv("MEMORY_ENABLED", "true")
        if seed and memory_on:
            bundle = memory_service.recall_for_incident(
                session,
                site_id=HUB_EVENT["site_id"],
                failure_domain="POWER",
                alarm_code="POWER_GRID_FAIL",
                site_type="HUB",
            )
            assert bundle.similar and bundle.fault_class, (
                "the seeded store must be visible to recall, or the comparison is vacuous"
            )
        hub._history.clear()
        inc = process_event(session, get_settings(), EventIngest(**HUB_EVENT))
        return {
            "snapshot": _snapshot(session, inc),
            "events": _event_literals(),
            "payload": _payload(session, inc),
            "number": inc.incident_number,
        }
    finally:
        session.close()
        hub._history.clear()
        clear_settings_cache()


# =================================================================================
# Section 1 — the inertness proof (§7.11.11 test 9)
# =================================================================================


def test_a_seeded_store_with_the_flag_on_changes_nothing_the_lifecycle_decides(tmp_path, monkeypatch):
    """The exit criterion: nine decision fields, every step row and the 26 event literals.

    Run A is today's system. Run B has four prior P4 twenty-minute outages at the same site
    and fault class, consolidated into ``memory_episodes``, and ``MEMORY_ENABLED=true``. If
    memory were honoured anywhere, the priority, the assignee, the MSP, the SLA band or the
    HITL gate would move. Nothing may.
    """
    empty = _run(tmp_path, monkeypatch, name="empty", seed=False, memory_on=False)
    seeded = _run(tmp_path, monkeypatch, name="seeded_on", seed=True, memory_on=True)

    assert empty["number"] == seeded["number"] == "INC000001"
    assert seeded["snapshot"]["decision"] == empty["snapshot"]["decision"], "a decision field moved"
    assert seeded["snapshot"]["steps"] == empty["snapshot"]["steps"], "a step row moved"
    assert seeded["snapshot"]["run_status"] == empty["snapshot"]["run_status"]
    assert seeded["snapshot"]["recurrence_count"] == empty["snapshot"]["recurrence_count"]
    assert seeded["events"] == empty["events"], "the run-scoped event sequence moved"
    assert len(empty["events"]) == 26, f"expected the 26 golden run-scoped events, got {len(empty['events'])}"


def test_memory_creates_no_hitl_task_and_removes_none(tmp_path, monkeypatch):
    """MEM3: "no task, no override". A prior that disagrees with the engine loses, silently.

    Asserted on the count *and* the types, because the failure this guards against is not
    only "memory opened a card" but "memory opened a different kind of card".
    """
    empty = _run(tmp_path, monkeypatch, name="empty", seed=False, memory_on=False)
    seeded = _run(tmp_path, monkeypatch, name="seeded_on", seed=True, memory_on=True)

    assert seeded["snapshot"]["hitl_task_count"] == empty["snapshot"]["hitl_task_count"] == 1
    assert seeded["snapshot"]["hitl_task_types"] == empty["snapshot"]["hitl_task_types"]


def test_only_the_additive_advisory_key_differs_in_the_hitl_payload(tmp_path, monkeypatch):
    """The one permitted difference, stated as an equality rather than as a spot check.

    Everything the approver decides on — the priority, the drafted SMS and email, the
    audiences, the envelope — must be byte-identical; ``advisory`` is added beside it.
    """
    empty = _run(tmp_path, monkeypatch, name="empty", seed=False, memory_on=False)
    seeded = _run(tmp_path, monkeypatch, name="seeded_on", seed=True, memory_on=True)

    assert "advisory" not in empty["payload"], "the flag-off payload gained a key"
    assert set(seeded["payload"]) - set(empty["payload"]) == {"advisory"}
    without = _comparable({k: v for k, v in seeded["payload"].items() if k != "advisory"})
    assert without == _comparable(empty["payload"]), "an existing payload key changed when memory was on"


def test_a_seeded_store_with_the_flag_off_is_byte_identical_including_the_payload(tmp_path, monkeypatch):
    """A store full of history must change **nothing at all** until reads are switched on.

    This is the run that catches an insertion which reads memory before checking the flag:
    run B can differ from A only by the advisory key, but run C may not differ from A at all.
    """
    empty = _run(tmp_path, monkeypatch, name="empty", seed=False, memory_on=False)
    dormant = _run(tmp_path, monkeypatch, name="seeded_off", seed=True, memory_on=False)

    assert dormant["snapshot"] == empty["snapshot"]
    assert dormant["events"] == empty["events"]
    assert _comparable(dormant["payload"]) == _comparable(empty["payload"]), (
        "a flag-off run's payload was not identical"
    )
    assert "advisory" not in dormant["payload"]


def test_the_advisory_that_is_frozen_onto_the_card_actually_says_something(tmp_path, monkeypatch):
    """The other half of the proof: the bundle is non-empty, so "identical" is not trivially
    true because memory produced nothing.

    Also pins the shape the workspace renders — every §7.11.4 bundle key is present even
    where its layer is M2/M3, so a renderer written today keeps working when they fill.
    """
    seeded = _run(tmp_path, monkeypatch, name="seeded_on", seed=True, memory_on=True)
    advisory = seeded["payload"]["advisory"]

    assert advisory["enabled"] is True and advisory["degraded"] is False
    assert set(advisory) == {
        "enabled",
        "degraded",
        "token_estimate",
        "site",
        "fault_class",
        "party",
        "correlation",
        "playbook",
        "similar",
        "memos",
    }
    assert len(advisory["similar"]) == 4
    assert advisory["similar"][0]["match_reason"] == "same site + same fault class"
    assert advisory["similar"][0]["restore_minutes"] == 20
    assert [f["key"] for f in advisory["fault_class"]] == ["median_restore_min", "p90_restore_min"]
    # §7.11.4: never a naked assertion — support, provenance and evidence travel with the fact.
    assert advisory["fault_class"][0]["support_count"] == 4
    assert advisory["fault_class"][0]["evidence"]
    assert advisory["fault_class"][0]["as_of"].endswith("Z"), "defect #41: timestamps carry a Z"
    assert advisory["token_estimate"] > 0


def test_the_engine_wins_even_when_every_prior_disagrees_with_it(tmp_path, monkeypatch):
    """MEM3 stated as the thing a reader of the card would notice.

    Four consecutive P4 twenty-minute outages at this exact site and fault class; the engine
    still says P1 with the full SLA band and still demands a human. The disagreement is
    visible in the advisory and changes nothing.
    """
    empty = _run(tmp_path, monkeypatch, name="empty", seed=False, memory_on=False)
    seeded = _run(tmp_path, monkeypatch, name="seeded_on", seed=True, memory_on=True)
    decision = seeded["snapshot"]["decision"]

    assert decision["priority"] == empty["snapshot"]["decision"]["priority"]
    assert decision["priority"] != "P4", "the engine adopted the priors' priority"
    assert decision["requires_hitl"] is True
    assert decision["responsible_msp"] != "SOMEONE-ELSE", "the engine adopted the priors' MSP"
    assert {e["fault_class"] for e in seeded["payload"]["advisory"]["similar"]} == {
        "POWER|POWER_GRID_FAIL|HUB"
    }


# =================================================================================
# Section 2 — fail-soft: memory never blocks an approval card
# =================================================================================


def test_a_raising_recall_still_produces_the_task_without_the_key(tmp_db, monkeypatch):
    """MEM4 at the one place where the cost of being wrong is an outage of the tool people
    use to fix outages.

    The helper already swallows everything; this forces it to raise anyway, because the guard
    that matters is the one in ``agents/hitl.py`` — the day somebody "simplifies" the helper
    and lets an exception out, the P1 card must still be created.
    """
    settings, session = tmp_db
    monkeypatch.setenv("MEMORY_ENABLED", "true")

    def boom(*_args, **_kwargs):
        raise RuntimeError("recall exploded")

    monkeypatch.setattr(memory_service, "advisory_for_incident", boom)
    hub._history.clear()
    inc = process_event(session, settings, EventIngest(**HUB_EVENT))

    task = session.scalars(select(HitlTaskRow).where(HitlTaskRow.incident_id == inc.id)).first()
    assert task is not None, "a memory failure prevented a P1 approval card"
    assert "advisory" not in task.proposed_payload
    assert inc.requires_hitl is True
    hub._history.clear()


def test_a_block_that_cannot_be_serialised_is_dropped_rather_than_failing_the_run(tmp_db, monkeypatch):
    """The failure the ``agents/hitl.py`` guard cannot catch, caught one level down.

    ``task.proposed_payload = payload`` is a bare ``json.dumps`` and it sits *outside* the
    node's try/except. A non-JSON value in the advisory would raise there — in a fail-closed
    node — and roll back the entire lifecycle run. That is not hypothetical: an early draft of
    the block carried ``datetime`` objects and this file's seeded run failed on exactly that.
    ``advisory_for_incident`` therefore proves the block serialisable inside its own guard and
    drops it otherwise; this forces a non-serialisable block and requires the P1 card anyway.
    """
    settings, session = tmp_db
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    monkeypatch.setattr(memory_service, "advisory_block", lambda _bundle: {"unserialisable": object()})
    hub._history.clear()
    inc = process_event(session, settings, EventIngest(**HUB_EVENT))

    task = session.scalars(select(HitlTaskRow).where(HitlTaskRow.incident_id == inc.id)).first()
    assert task is not None, "a non-JSON advisory failed the run"
    assert "advisory" not in task.proposed_payload
    hub._history.clear()


def test_a_broken_session_degrades_the_helper_to_none_rather_than_raising(tmp_db, monkeypatch):
    """The helper's own contract, tested directly: whatever happens below it, ``None``."""
    settings, session = tmp_db
    monkeypatch.setenv("MEMORY_ENABLED", "true")

    class _Broken:
        def scalars(self, *_a, **_k):
            raise RuntimeError("database is locked")

        def scalar(self, *_a, **_k):
            raise RuntimeError("database is locked")

        def execute(self, *_a, **_k):
            raise RuntimeError("database is locked")

    inc = IncidentRow(
        operator_id="safaricom",
        incident_number="INC-X",
        site_id="S",
        region_code="NBI_E",
        correlation_fingerprint="x",
        failure_domain="POWER",
        alarm_code="A",
        site_type="HUB",
    )
    # An empty bundle, not an exception: recall_* degrade to (), so the block renders empty
    # and honest rather than breaking the caller.
    block = memory_service.advisory_for_incident(_Broken(), inc, settings.operator)
    assert block is not None and block["degraded"] is True and block["similar"] == []


def test_with_the_flag_off_the_helper_returns_none_so_no_key_is_added(tmp_db, monkeypatch):
    """``None`` is what makes the two callers differ correctly: the HITL payload gains no key
    at all, while the serializer sets ``advisory: null`` (§7.11.11 test 25)."""
    settings, session = tmp_db
    monkeypatch.delenv("MEMORY_ENABLED", raising=False)
    inc = IncidentRow(
        operator_id="safaricom",
        incident_number="INC-X",
        site_id="S",
        region_code="NBI_E",
        correlation_fingerprint="x",
        failure_domain="POWER",
        alarm_code="A",
        site_type="HUB",
    )
    assert memory_service.advisory_for_incident(session, inc, settings.operator) is None


# =================================================================================
# Section 3 — the static guard (§7.11.11 test 10)
# =================================================================================

#: The eight modules MEM1 names, plus the two places a hot-path import would most plausibly
#: be added by accident. ``agents/hitl.py`` is deliberately NOT here: it is the one sanctioned
#: reader (spec line 419), and section 1 above is what holds it honest.
_ENGINE_MODULES = (
    "noc_agents.services.priority",
    "noc_agents.services.assignment",
    "noc_agents.services.composition",
    "noc_agents.services.numbering",
    "noc_agents.services.lifecycle",
    "noc_agents.agents.correlate",
    "noc_agents.agents.severity",
    "noc_agents.agents.assign",
    "noc_agents.orchestrator.runner",
    "noc_agents.graph.pipeline",
)

#: Both halves of the lane. ``noc_agents.memory`` is the **write** side, and an engine
#: importing it would be a far worse breach than importing the read side: it would put a row
#: inside ``runner._fail_closed``'s rollback (MEM5).
_FORBIDDEN_PREFIXES = ("noc_agents.memory", "noc_agents.services.memory")


def _imported_modules(dotted: str) -> set[str]:
    """Every module named by an ``import``/``from`` anywhere in the file, nesting included.

    Walking the AST rather than reading ``module.__dict__`` is the point: a function-local
    ``import noc_agents.memory.consolidate`` inside a branch that only fires in production
    would be invisible to an attribute check and is exactly how G15 would be breached quietly.
    """
    source = Path(importlib.import_module(dotted).__file__).read_text(encoding="utf-8")
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


@pytest.mark.parametrize("dotted", _ENGINE_MODULES)
def test_no_deterministic_engine_imports_either_half_of_the_memory_lane(dotted):
    """MEM1: the engines stay byte-for-byte deterministic, so they may not even see memory."""
    offending = sorted(
        m
        for m in _imported_modules(dotted)
        for prefix in _FORBIDDEN_PREFIXES
        if m == prefix or m.startswith(prefix + ".")
    )
    assert offending == [], f"{dotted} imports {offending} — G15 forbids memory inside a decision engine"


def test_the_hitl_node_reads_memory_and_does_not_write_it(tmp_db):
    """The sanctioned reader, pinned from both sides.

    ``agents/hitl.py`` may import the **read** module (``services.memory``) and may not import
    the **write** package (``noc_agents.memory``): the first is a query, the second would put
    a row inside the lifecycle transaction that ``runner._fail_closed`` rolls back (MEM5).
    """
    imports = _imported_modules("noc_agents.agents.hitl")
    assert any(m.startswith("noc_agents.services.memory") or m == "noc_agents.services" for m in imports)
    assert not [m for m in imports if m == "noc_agents.memory" or m.startswith("noc_agents.memory.")], (
        "agents/hitl.py must not import the memory WRITE path — MEM5 forbids a memory row on the hot path"
    )


def test_the_hot_path_insertion_is_one_guarded_call(tmp_db):
    """The insertion is small on purpose, and staying small is a property worth pinning.

    ``agents/hitl.py`` is on the golden path: 26 event literals and the full-HITL contract
    test run through it. A future edit that grows this from "one guarded call to one helper"
    into logic is exactly the change that should have to justify itself, and this assertion
    is where it has to.
    """
    source = Path(importlib.import_module("noc_agents.agents.hitl").__file__).read_text(encoding="utf-8")
    assert source.count("advisory_for_incident") == 1, "memory is read more than once on the hot path"
    assert source.count('payload["advisory"]') == 1
    # No write API, no session mutation, no commit anywhere near it.
    for forbidden in ("consolidate_incident", "MemoryEpisodeRow", "session.commit"):
        assert forbidden not in source, f"agents/hitl.py must not contain {forbidden!r}"
