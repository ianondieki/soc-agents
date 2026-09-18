"""Agent memory M0 (spec §7.11): what recall returns, and what it must never return.

M0 is pure-SQL recall over the ``incidents`` and ``work_notes`` tables that already exist —
no new table, no job, no LLM. Three properties carry the whole step, and they are the three
this file is organised around:

1. **Operator isolation on every ``recall_*``.** Recall is the single most dangerous surface
   in this system for cross-operator leakage, because unlike every other read it *deliberately*
   reaches across incidents. ``config/default.yaml`` points both operator profiles at one
   SQLite file, so a forgotten ``WHERE operator_id = ?`` does not produce an empty screen —
   it produces Safaricom's outage history on an Airtel workspace. Section 1 seeds both
   operators at the **same ``site_id``**, which is the case a naive site-keyed query gets
   wrong, and asserts the rule from both sides.
2. **Advisory and inert (MEM1/G15).** M0 is a read surface. Section 4 proves it changes
   nothing: the same alarm, run against a database with a populated site history and against
   one with none, must produce identical decision fields, identical step rows and an identical
   run-scoped event sequence. The history is seeded *outside* the 30-day recurrence window on
   purpose — see the comment on :func:`test_a_populated_site_history_changes_nothing...`.
3. **Degrades to empty, never to an error (MEM4).** Section 5. A blank panel at 3 a.m. is a
   disappointment; a workspace that will not load because an advisory read raised is an
   outage of the tool people are using to fix an outage.

Names are the subject of ``test_memory_privacy.py``; the API shape is ``test_memory_api.py``.
"""

from __future__ import annotations

import ast
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
from noc_agents.domain.schemas import EventIngest
from noc_agents.graph.pipeline import process_event
from noc_agents.realtime.hub import hub
from noc_agents.services.lifecycle import (
    RESTORE_SOURCE_MARK,
    RESTORE_SOURCE_NOTE,
    RESTORE_SOURCE_SUPERVISOR,
)
from noc_agents.services.memory import (
    MATCH_SAME_SITE,
    MATCH_SAME_SITE_AND_FAULT_CLASS,
    SUMMARY_MAX_CHARS,
    fault_class,
    recall_similar_episodes,
    recall_site_history,
)

#: The colocation case: one physical mast, two licensees, one ``site_id`` string in one
#: database file. Nothing in the schema stops both operators writing incidents here.
SHARED_SITE = "SHARED-COLO-BTS-01"

_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _cfg():
    return get_settings().operator


def _seed_episode(
    session,
    *,
    number: str,
    site_id: str = SHARED_SITE,
    operator_id: str = "safaricom",
    region_code: str = "NBI_E",
    failure_domain: str = "POWER",
    alarm_code: str = "POWER_GRID_FAIL",
    site_type: str = "BTS",
    status: str = "CLOSED",
    priority: str = "P2",
    users_affected: int = 450_000,
    days_ago: float = 10,
    restore_minutes: int = 120,
    restored_source: str | None = RESTORE_SOURCE_MARK,
    resolution_code: str = "FIELD_RESTORED",
    resolution_summary: str = "Generator refuelled and mains restored",
    assignee_name: str | None = None,
    fe_name: str | None = None,
    rnio_name: str | None = None,
    notes: tuple[tuple[str, str], ...] = (),
) -> IncidentRow:
    """One finished incident, written straight to the table M0 reads.

    Deliberately not routed through ``process_event``: the point of these tests is what the
    recall SQL does with rows that are already there, and the pipeline would impose its own
    numbering, correlation and recurrence behaviour on the fixture.

    ``number`` is a free-form string rather than an ``INC%06d``, so seeding history never
    consumes a value from the ``daily_sequences`` counter that the lifecycle allocates from —
    that is what lets section 4 compare two runs that both produce ``INC000001``.
    """
    ended = utcnow() - timedelta(days=days_ago)
    started = ended - timedelta(minutes=restore_minutes)
    inc = IncidentRow(
        operator_id=operator_id,
        incident_number=number,
        status=status,
        priority=priority,
        users_affected=users_affected,
        site_id=site_id,
        site_name=f"{site_id} site",
        site_type=site_type,
        region_code=region_code,
        failure_domain=failure_domain,
        alarm_code=alarm_code,
        correlation_fingerprint=f"{site_id}|{alarm_code}|{failure_domain}",
        created_at=started,
        outage_start_at=started,
        restored_at=ended if status in ("RESTORED", "CLOSED") else None,
        restored_source=restored_source,
        closed_at=ended if status == "CLOSED" else None,
        resolution_code=resolution_code,
        resolution_summary=resolution_summary,
        assignee_name=assignee_name,
        fe_name=fe_name,
        rnio_name=rnio_name,
    )
    session.add(inc)
    session.flush()
    for author, body in notes:
        session.add(
            WorkNoteRow(
                incident_id=inc.id,
                author=author,
                author_role="MSP",
                body=body,
                created_at=ended,
                source="ui",
            )
        )
    session.commit()
    return inc


def _numbers(episodes) -> list[str]:
    return [e.incident_number for e in episodes]


# =================================================================================
# Section 1 — operator isolation. Read this section first.
# =================================================================================


def test_site_history_never_returns_the_other_operators_incidents_at_the_same_site(tmp_db):
    """Both licensees fail at one colocated mast; each recall sees only its own outages.

    This is the test the whole module exists for. A site-keyed query without the operator
    clause returns six rows here and looks entirely plausible while doing it — the site id
    matches, the timestamps interleave, nothing in the result announces that half of it
    belongs to a competitor.
    """
    _settings, session = tmp_db
    for i in range(3):
        _seed_episode(session, number=f"SAF-{i}", operator_id="safaricom", days_ago=1 + i)
        _seed_episode(session, number=f"ATL-{i}", operator_id="airtel", days_ago=1.5 + i)

    stored = session.scalars(select(IncidentRow).where(IncidentRow.site_id == SHARED_SITE)).all()
    assert len(stored) == 6, "precondition: both operators' rows really are in one file"

    found = recall_site_history(session, site_id=SHARED_SITE)
    assert _numbers(found) == ["SAF-0", "SAF-1", "SAF-2"], _numbers(found)
    assert not [n for n in _numbers(found) if n.startswith("ATL-")], (
        "airtel incidents reached a safaricom site-history recall"
    )


def test_similar_episode_recall_never_returns_the_other_operators_incidents(tmp_db):
    """The exact tier matches on site + fault class — both of which the two operators share."""
    _settings, session = tmp_db
    _seed_episode(session, number="SAF-A", operator_id="safaricom", days_ago=2)
    _seed_episode(session, number="ATL-A", operator_id="airtel", days_ago=1)
    _seed_episode(session, number="ATL-B", operator_id="airtel", days_ago=3)

    found = recall_similar_episodes(
        session,
        site_id=SHARED_SITE,
        failure_domain="POWER",
        alarm_code="POWER_GRID_FAIL",
        site_type="BTS",
        cfg=_cfg(),
    )
    assert _numbers(found) == ["SAF-A"], _numbers(found)


def test_isolation_holds_from_the_other_side_too(tmp_db, monkeypatch):
    """Switching the process to the airtel profile shows airtel's history and only airtel's.

    Asserted in both directions because a one-directional check passes just as happily
    against a query hard-wired to ``operator_id = 'safaricom'`` as against a correct one.
    """
    _settings, session = tmp_db
    _seed_episode(session, number="SAF-A", operator_id="safaricom", days_ago=2)
    _seed_episode(session, number="ATL-A", operator_id="airtel", days_ago=1)

    monkeypatch.setenv("OPERATOR_PROFILE", "airtel")
    clear_settings_cache()
    try:
        found = recall_site_history(session, site_id=SHARED_SITE)
        assert _numbers(found) == ["ATL-A"], _numbers(found)
    finally:
        monkeypatch.setenv("OPERATOR_PROFILE", "safaricom")
        clear_settings_cache()


# =================================================================================
# Section 2 — what site history returns
# =================================================================================


def test_site_history_is_newest_first(tmp_db):
    """A chronology, not a ranking: the reader wants the last outage at the top."""
    _settings, session = tmp_db
    for i, days in enumerate((30, 2, 11)):
        _seed_episode(session, number=f"H-{i}", days_ago=days)
    assert _numbers(recall_site_history(session, site_id=SHARED_SITE)) == ["H-1", "H-2", "H-0"]


def test_only_finished_incidents_count_as_history(tmp_db):
    """An open ticket is the present, not the past; a cancelled one never happened.

    Counting either would inflate every "Nth outage at this site" figure M1 derives from the
    same population, and an open incident has no outcome to learn from yet.
    """
    _settings, session = tmp_db
    _seed_episode(session, number="CLOSED", status="CLOSED", days_ago=1)
    _seed_episode(session, number="RESTORED", status="RESTORED", days_ago=2)
    _seed_episode(session, number="OPEN", status="IN_PROGRESS", days_ago=3)
    _seed_episode(session, number="CANCELLED", status="CANCELLED", days_ago=4)
    assert _numbers(recall_site_history(session, site_id=SHARED_SITE)) == ["CLOSED", "RESTORED"]


def test_the_lookback_window_excludes_older_outages(tmp_db):
    """``lookback_days`` is a real filter, and 0 means "no window", not "no history"."""
    _settings, session = tmp_db
    _seed_episode(session, number="RECENT", days_ago=10)
    _seed_episode(session, number="ANCIENT", days_ago=800)

    assert _numbers(recall_site_history(session, site_id=SHARED_SITE, lookback_days=365)) == ["RECENT"]
    assert _numbers(recall_site_history(session, site_id=SHARED_SITE, lookback_days=0)) == ["RECENT", "ANCIENT"]


def test_the_limit_caps_the_rows_returned(tmp_db):
    _settings, session = tmp_db
    for i in range(6):
        _seed_episode(session, number=f"H-{i}", days_ago=1 + i)
    assert _numbers(recall_site_history(session, site_id=SHARED_SITE, limit=2)) == ["H-0", "H-1"]
    assert recall_site_history(session, site_id=SHARED_SITE, limit=0) == ()


def test_a_site_with_no_history_returns_empty_and_an_unknown_site_does_too(tmp_db):
    """The blank-panel contract: "nothing here" and "no such site" are the same answer."""
    _settings, session = tmp_db
    _seed_episode(session, number="H-0", site_id="SFC-MTK-BTS-MCH04", days_ago=1)
    assert recall_site_history(session, site_id="SFC-NBIE-HUB-EMB") == ()
    assert recall_site_history(session, site_id="NO-SUCH-SITE-AT-ALL") == ()
    assert recall_site_history(session, site_id="") == ()


def test_every_history_row_carries_the_match_reason_the_ui_shows(tmp_db):
    _settings, session = tmp_db
    _seed_episode(session, number="H-0", days_ago=1)
    assert [e.match_reason for e in recall_site_history(session, site_id=SHARED_SITE)] == [MATCH_SAME_SITE]


# =================================================================================
# Section 3 — the exact tier, fault classes, durations
# =================================================================================


def test_fault_class_is_the_three_columns_canonicalised(tmp_db):
    """M1 must rebuild the identical string from the identical columns or the tier stops matching."""
    assert fault_class("power", "site_down", "bts") == "POWER|SITE_DOWN|BTS"
    assert fault_class(None, None, None) == "UNKNOWN||BTS"
    assert fault_class("  POWER  ", "  ", " HUB ") == "POWER||HUB"


def test_the_exact_tier_matches_on_site_and_fault_class_and_nothing_looser(tmp_db):
    """Each of the three fault-class components must disqualify a near-miss on its own.

    The failure mode being designed against (§7.11.7) is the confidently-wrong neighbour: a
    similar-looking prior incident that sends an engineer to the wrong fault class. A tier
    called "exact" that quietly matches on two of three columns is that failure mode.
    """
    _settings, session = tmp_db
    _seed_episode(session, number="MATCH", days_ago=1)
    _seed_episode(session, number="OTHER-DOMAIN", failure_domain="TRANSMISSION", days_ago=1)
    _seed_episode(session, number="OTHER-ALARM", alarm_code="SITE_DOWN", days_ago=1)
    _seed_episode(session, number="OTHER-TYPE", site_type="HUB", days_ago=1)
    _seed_episode(session, number="OTHER-SITE", site_id="SFC-MTK-BTS-MCH04", days_ago=1)

    found = recall_similar_episodes(
        session,
        site_id=SHARED_SITE,
        failure_domain="POWER",
        alarm_code="POWER_GRID_FAIL",
        site_type="BTS",
        cfg=_cfg(),
    )
    assert _numbers(found) == ["MATCH"]
    assert found[0].match_reason == MATCH_SAME_SITE_AND_FAULT_CLASS
    assert found[0].fault_class == "POWER|POWER_GRID_FAIL|BTS"


def test_three_prior_outages_of_the_same_class_all_come_back_newest_first(tmp_db):
    """Acceptance §7.11.11 test 16. Equal impact, so ranking reduces to the recency term."""
    _settings, session = tmp_db
    for i, days in enumerate((40, 3, 17)):
        _seed_episode(session, number=f"P-{i}", days_ago=days)

    found = recall_similar_episodes(
        session, site_id=SHARED_SITE, failure_domain="POWER", alarm_code="POWER_GRID_FAIL", cfg=_cfg()
    )
    assert _numbers(found) == ["P-1", "P-2", "P-0"]
    assert [round(e.score, 6) for e in found] == sorted((round(e.score, 6) for e in found), reverse=True)


def test_a_much_larger_prior_outage_can_outrank_a_smaller_more_recent_one(tmp_db):
    """``score = recency + relevance + impact`` (§7.11.4): impact is a real term, not decoration.

    The 3 a.m. question is not only "what happened last" but "what is the worst thing that
    has happened here", and a P1 half-million-user outage a month ago is more worth reading
    than a P4 blip yesterday.
    """
    _settings, session = tmp_db
    _seed_episode(session, number="BIG", days_ago=30, priority="P1", users_affected=500_000)
    _seed_episode(session, number="SMALL", days_ago=1, priority="P4", users_affected=50)

    found = recall_similar_episodes(
        session, site_id=SHARED_SITE, failure_domain="POWER", alarm_code="POWER_GRID_FAIL", cfg=_cfg()
    )
    assert _numbers(found) == ["BIG", "SMALL"], _numbers(found)


def test_the_exact_tier_respects_its_limit(tmp_db):
    _settings, session = tmp_db
    for i in range(7):
        _seed_episode(session, number=f"P-{i}", days_ago=1 + i)
    found = recall_similar_episodes(
        session, site_id=SHARED_SITE, failure_domain="POWER", alarm_code="POWER_GRID_FAIL", cfg=_cfg(), limit=3
    )
    assert _numbers(found) == ["P-0", "P-1", "P-2"]


@pytest.mark.parametrize(
    "source, expected",
    [
        (RESTORE_SOURCE_MARK, 120),
        (RESTORE_SOURCE_SUPERVISOR, 120),
        (RESTORE_SOURCE_NOTE, None),
        (None, None),
    ],
)
def test_restore_minutes_is_only_computed_from_trustworthy_provenance(tmp_db, source, expected):
    """§7.0.8's M4 rule, and the guard against brief defect #4.

    ``VENDOR_NOTE_INFERRED`` is a regex hit on the word "RESTORED" inside a vendor's free
    text, and a NULL source is ``close_incident``'s ``restored_at = closed_at`` back-fill.
    Both yield a number that looks exactly like an MTTR and is not one; a wrong duration
    here becomes a wrong median the moment M2 starts aggregating these rows.
    """
    _settings, session = tmp_db
    _seed_episode(session, number="H-0", days_ago=1, restore_minutes=120, restored_source=source)
    assert recall_site_history(session, site_id=SHARED_SITE)[0].restore_minutes == expected


def test_a_restore_that_predates_the_outage_yields_no_duration(tmp_db):
    """A negative duration is a data error, and rendering it as a negative MTTR hides that."""
    _settings, session = tmp_db
    inc = _seed_episode(session, number="H-0", days_ago=1)
    inc.restored_at = inc.outage_start_at - timedelta(minutes=5)
    session.commit()
    assert recall_site_history(session, site_id=SHARED_SITE)[0].restore_minutes is None


def test_the_restoring_work_note_is_the_fallback_for_an_empty_resolution_summary(tmp_db):
    """Why M0 reads ``work_notes`` at all.

    The pipeline guarantees a generic ``resolution_code`` on every close and leaves the prose
    to whoever closed the ticket, so ``resolution_summary`` is very often empty — and "what
    fixed it" is the single most useful thing a memory panel can say. The fallback uses the
    lifecycle's own ``note_declares_restored`` predicate, so the note memory calls "the fix"
    is the same note the lifecycle called "the restore".
    """
    _settings, session = tmp_db
    _seed_episode(
        session,
        number="H-0",
        days_ago=1,
        resolution_summary="",
        notes=(
            ("Vendor Desk", "Engineer dispatched, gate locked"),
            ("Vendor Desk", "Generator refuelled, SERVICE RESTORED at site"),
        ),
    )
    summary = recall_site_history(session, site_id=SHARED_SITE)[0].resolution_summary
    assert "Generator refuelled" in summary
    assert "gate locked" not in summary, "a non-restoring note was used as the resolution"


def test_the_resolution_summary_is_capped(tmp_db):
    """§7.11.3 caps evidence text at 240 characters — a cap on how much attacker-reachable
    vendor free text travels with a hit (MEM9), not a display preference."""
    _settings, session = tmp_db
    _seed_episode(session, number="H-0", days_ago=1, resolution_summary="x" * 4000)
    assert len(recall_site_history(session, site_id=SHARED_SITE)[0].resolution_summary) == SUMMARY_MAX_CHARS


# =================================================================================
# Section 4 — advisory and inert (MEM1 / G15)
# =================================================================================

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

#: The nine fields §7.11.11 test 9 names, minus the two SLA timestamps, which are absolute
#: and therefore compared as offsets from ``created_at`` (see :func:`_snapshot`).
_DECISION_FIELDS = (
    "priority",
    "assignee_type",
    "assignee_name",
    "msp_name",
    "responsible_msp",
    "requires_hitl",
    "hitl_state",
)


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
    """The nine decision fields, every step row, and the HITL task count."""
    run = session.scalars(
        select(AgentRunRow)
        .where(AgentRunRow.incident_id == inc.id, AgentRunRow.graph_name == "incident_lifecycle")
        .order_by(AgentRunRow.started_at.desc())
    ).first()
    assert run is not None, "the lifecycle run row is the thing being compared"
    return {
        "decision": {f: getattr(inc, f) for f in _DECISION_FIELDS}
        | {
            # Absolute timestamps differ between two runs by construction; the SLA *band*
            # applied to the incident is the decision, and that is the offset. Rounded to
            # whole minutes because ``sla_due`` is computed from its own ``utcnow()`` a few
            # dozen microseconds after ``created_at``, so the raw offsets differ run to run
            # by sub-millisecond noise that says nothing about any decision. The bands
            # themselves are whole minutes (``sla_minutes`` in the operator profile), so a
            # genuine change of band moves this by at least one.
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
        "hitl_tasks": session.scalar(
            select(HitlTaskRow).where(HitlTaskRow.incident_id == inc.id)
        )
        is not None,
    }


def _event_literals() -> list[tuple]:
    """The run-scoped event sequence, ids and timestamps removed — 26 of them for this event."""
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


def _run_lifecycle(tmp_path, monkeypatch, *, name: str, seed_history: bool):
    """One complete lifecycle in its own database file, with or without a site history."""
    url = f"sqlite:///{(tmp_path / f'{name}.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("OPERATOR_PROFILE", "safaricom")
    clear_settings_cache()
    init_db(url)
    session = get_session()
    try:
        if seed_history:
            # 90+ days back on purpose. ``agents/recurrence.py`` counts incidents at the same
            # site and domain within ``recurrence.lookback_days`` (30), so history INSIDE that
            # window would legitimately change ``recurrence_count`` and open a problem record —
            # a pipeline behaviour that exists today and has nothing to do with memory. Placing
            # the history outside the recurrence window and inside memory's 365-day one isolates
            # the variable this test is actually about.
            for i in range(3):
                _seed_episode(
                    session,
                    number=f"HIST-{i}",
                    site_id=HUB_EVENT["site_id"],
                    site_type="HUB",
                    region_code="NBI_E",
                    days_ago=90 + i * 20,
                )
            assert len(recall_site_history(session, site_id=HUB_EVENT["site_id"])) == 3, (
                "the history must be visible to memory, or this test proves nothing"
            )
        hub._history.clear()
        inc = process_event(session, get_settings(), EventIngest(**HUB_EVENT))
        return _snapshot(session, inc), _event_literals(), inc.incident_number
    finally:
        session.close()
        hub._history.clear()
        clear_settings_cache()


def test_a_populated_site_history_changes_nothing_the_lifecycle_decides(tmp_path, monkeypatch):
    """The G15 guard for M0: memory is a read surface and moves nothing.

    Run the same alarm twice, in two fresh databases — once against a site with three prior
    POWER outages on record, once against a site with none. Every decision field, every step
    row (input, output, rationale, tools, confidence) and the whole run-scoped event sequence
    must be identical. If any of them ever differs, something has started reading memory on
    the hot path, which is the one thing §7.11 forbids outright.
    """
    empty_snap, empty_events, empty_number = _run_lifecycle(
        tmp_path, monkeypatch, name="empty", seed_history=False
    )
    seeded_snap, seeded_events, seeded_number = _run_lifecycle(
        tmp_path, monkeypatch, name="seeded", seed_history=True
    )

    assert empty_number == seeded_number == "INC000001"
    assert seeded_snap["decision"] == empty_snap["decision"], "a decision field moved"
    assert seeded_snap["steps"] == empty_snap["steps"], "a step row moved"
    assert seeded_snap["run_status"] == empty_snap["run_status"]
    assert seeded_snap["recurrence_count"] == empty_snap["recurrence_count"]
    assert seeded_snap["hitl_tasks"] == empty_snap["hitl_tasks"]
    assert seeded_events == empty_events, "the run-scoped event sequence moved"
    assert len(empty_events) == 26, f"expected the 26 golden run-scoped events, got {len(empty_events)}"


#: §7.11.11 test 10. The eight modules MEM1 names, plus the two places a hot-path import
#: would most plausibly be added by accident.
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


def _imported_modules(dotted: str) -> set[str]:
    """Every module named by an ``import``/``from`` anywhere in the file, nesting included.

    Walking the AST rather than reading ``module.__dict__`` is the point: a function-local
    ``import noc_agents.services.memory`` inside a branch that only fires in production would
    be invisible to an attribute check and is exactly how G15 would be breached quietly.
    """
    import importlib

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
def test_no_deterministic_engine_imports_the_memory_module(dotted):
    """MEM1: the engines stay byte-for-byte deterministic, so they may not even see memory."""
    offending = sorted(m for m in _imported_modules(dotted) if m.startswith("noc_agents.services.memory"))
    assert offending == [], f"{dotted} imports {offending} — G15 forbids memory inside a decision engine"


# =================================================================================
# Section 5 — degrade to empty, never to an error (MEM4)
# =================================================================================


class _BrokenSession:
    """A session that fails the way a half-migrated or locked database fails."""

    def scalars(self, *_args, **_kwargs):
        raise RuntimeError("database is locked")


def test_recall_returns_empty_rather_than_raising_when_the_database_misbehaves():
    """The workspace must still load. An empty panel is a disappointment, not an outage."""
    broken = _BrokenSession()
    assert recall_site_history(broken, site_id=SHARED_SITE) == ()
    assert recall_similar_episodes(broken, site_id=SHARED_SITE, failure_domain="POWER", cfg=_cfg()) == ()


def test_the_recall_functions_are_deliberately_not_gated_by_the_flag(tmp_db, monkeypatch):
    """``MEMORY_ENABLED`` is checked by the entry points, not inside ``recall_*``.

    Pinned because it looks like an omission and is not. §7.11.4 gives ``recall_site_history``
    no config argument at all, and M1's consolidator calls these same functions to *build* the
    ``memory_episodes`` index — work that must keep running whether or not reads are switched
    on for humans. The flag belongs where memory reaches a person: today
    ``GET /api/v1/memory/sites/{site_id}`` (``test_memory_api.py``), in M1 also
    ``recall_for_incident()`` and the ``advisory`` serializer key. Adding a check here would
    look like tightening and would quietly disable consolidation.

    This is safe today only because nothing else calls them: the module is imported by the
    router alone, which ``test_no_deterministic_engine_imports_the_memory_module`` above keeps
    true for every decision engine.
    """
    _settings, session = tmp_db
    _seed_episode(session, number="H-0", days_ago=1)
    monkeypatch.delenv("MEMORY_ENABLED", raising=False)
    assert len(recall_site_history(session, site_id=SHARED_SITE)) == 1


def test_a_nonsense_limit_returns_empty_rather_than_raising(tmp_db):
    _settings, session = tmp_db
    _seed_episode(session, number="H-0", days_ago=1)
    assert recall_site_history(session, site_id=SHARED_SITE, limit="not a number") == ()  # type: ignore[arg-type]
    assert recall_site_history(session, site_id=SHARED_SITE, limit=-5) == ()
