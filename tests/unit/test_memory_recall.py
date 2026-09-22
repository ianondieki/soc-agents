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
import json
import re
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy import text as sql

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
from noc_agents.memory.consolidate import consolidate_incident
from noc_agents.memory.schema import FTS_TABLE
from noc_agents.services.memory import (
    MATCH_FTS_PREFIX,
    MATCH_SAME_SITE,
    MATCH_SAME_SITE_AND_FAULT_CLASS,
    SUMMARY_MAX_CHARS,
    advisory_block,
    fault_class,
    memory_settings,
    recall_for_incident,
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
# Section 3b — the lexical (FTS5) tier and the tier ordering (M1, §7.11.4)
# =================================================================================
#
# The second tier of the cascade. It exists to find the ticket whose *words* match when the
# structure does not — "generator fuel" at a different site — and it is the tier that
# introduces the failure mode §7.11.7 names: the confidently-wrong neighbour. So the two
# things tested hardest here are that it finds things, and that it can never outrank a
# structural hit while doing so.
#
# One property deserves stating on its own: ``memory_note_fts`` carries NO ``operator_id``
# (§7.11.3's DDL has five UNINDEXED columns and none of them is the operator). An FTS hit is
# therefore a *suggestion*, never a proof of ownership, and every candidate is re-fetched
# through ``api.deps._owned`` before it is returned. The isolation test below is the one that
# would catch a future "optimisation" that returned FTS rows directly.


def _consolidate_all(session, settings) -> None:
    """Build the derived index for every seeded incident — the lexical tier's input."""
    for inc in session.scalars(select(IncidentRow)).all():
        consolidate_incident(session, settings=settings, incident_id=inc.id)
    session.commit()


def test_the_lexical_tier_finds_an_episode_by_a_word_in_its_note(tmp_db):
    """§7.11.11 test 17, first half. The note body is what is indexed, and it is indexed
    scrubbed — the same text a human would have read on the ticket."""
    settings, session = tmp_db
    _seed_episode(
        session,
        number="FUEL",
        site_id="SFC-MTK-BTS-MCH04",
        days_ago=5,
        resolution_summary="",
        notes=(("Vendor Desk", "Generator fuel delivered, SERVICE RESTORED"),),
    )
    _consolidate_all(session, settings)

    found = recall_similar_episodes(
        session,
        site_id="SFC-MTK-BTS-MCH04",
        failure_domain="POWER",
        alarm_code="SOMETHING_ELSE",  # no exact hit: the alarm token does not match
        cfg=_cfg(),
        query_text="fuel",
    )
    assert _numbers(found) == ["FUEL"]
    assert found[0].match_reason == "fts: fuel"


def test_an_exact_hit_always_outranks_a_lexical_one_however_good_the_words_are(tmp_db):
    """§7.11.11 test 17, second half, and the §7.11.7 mitigation in one assertion.

    The lexical candidate is deliberately the *better* row on every term of the score: it is
    more recent and a P1 half-million-user outage, against an exact hit that is old and
    trivial. It must still come second, because the tier is a hard ordering key rather than a
    weight — an engineer sent to another site's fault class is the expensive failure here, not
    a slightly mis-ranked panel.
    """
    settings, session = tmp_db
    _seed_episode(
        session,
        number="EXACT-OLD-SMALL",
        days_ago=300,
        priority="P4",
        users_affected=20,
        resolution_summary="",
        notes=(("Vendor Desk", "Generator fuel delivered, SERVICE RESTORED"),),
    )
    _seed_episode(
        session,
        number="LEXICAL-NEW-BIG",
        site_id="SFC-MTK-BTS-MCH04",
        days_ago=1,
        priority="P1",
        users_affected=500_000,
        resolution_summary="",
        notes=(("Vendor Desk", "Generator fuel delivered, SERVICE RESTORED"),),
    )
    _consolidate_all(session, settings)

    found = recall_similar_episodes(
        session,
        site_id=SHARED_SITE,
        failure_domain="POWER",
        alarm_code="POWER_GRID_FAIL",
        cfg=_cfg(),
        query_text="generator fuel",
    )
    assert _numbers(found) == ["EXACT-OLD-SMALL", "LEXICAL-NEW-BIG"], _numbers(found)
    assert found[0].match_reason == MATCH_SAME_SITE_AND_FAULT_CLASS
    assert found[1].match_reason.startswith(MATCH_FTS_PREFIX)
    assert found[1].score > found[0].score, (
        "the fixture is meant to make the lexical row score higher; if it does not, this test "
        "is no longer proving that the tier beats the score"
    )


def test_an_incident_that_matches_both_tiers_appears_once_with_the_stronger_reason(tmp_db):
    """The union is deduplicated by ``incident_id``, and the stronger tier wins the row.

    A hit shown twice is noise; a hit shown once but *demoted* to "fts:" would understate the
    evidence, which on an approval card is the more damaging of the two.
    """
    settings, session = tmp_db
    _seed_episode(
        session,
        number="BOTH",
        days_ago=3,
        resolution_summary="",
        notes=(("Vendor Desk", "Generator fuel delivered, SERVICE RESTORED"),),
    )
    _consolidate_all(session, settings)

    found = recall_similar_episodes(
        session,
        site_id=SHARED_SITE,
        failure_domain="POWER",
        alarm_code="POWER_GRID_FAIL",
        cfg=_cfg(),
        query_text="fuel",
    )
    assert _numbers(found) == ["BOTH"]
    assert found[0].match_reason == MATCH_SAME_SITE_AND_FAULT_CLASS


def test_the_lexical_tier_never_returns_the_other_operators_episodes(tmp_db, monkeypatch):
    """MEM10 on the one table that cannot enforce it itself.

    ``memory_note_fts`` has no ``operator_id`` column, so if FTS rows were returned directly
    this would hand a Safaricom shift an Airtel ticket — at the same colocated mast, with
    matching wording, looking entirely plausible. The re-fetch through ``_owned`` is the only
    thing standing between those two facts, which is why it is asserted here rather than
    assumed from the service-level isolation tests.
    """
    settings, session = tmp_db
    _seed_episode(
        session,
        number="ATL-FUEL",
        operator_id="airtel",
        days_ago=2,
        resolution_summary="",
        notes=(("Vendor Desk", "Generator fuel delivered, SERVICE RESTORED"),),
    )
    airtel = session.scalars(select(IncidentRow)).one()

    # Written into the shared index under airtel's own profile, exactly as its consolidator
    # would write it — the index really does contain another operator's text.
    monkeypatch.setenv("OPERATOR_PROFILE", "airtel")
    clear_settings_cache()
    try:
        consolidate_incident(session, settings=get_settings(), incident_id=airtel.id)
        session.commit()
    finally:
        monkeypatch.setenv("OPERATOR_PROFILE", "safaricom")
        clear_settings_cache()

    assert session.execute(sql(f"SELECT count(*) FROM {FTS_TABLE}")).scalar_one() > 0, (
        "the airtel rows must really be in the shared index, or this test proves nothing"
    )

    found = recall_similar_episodes(
        session,
        site_id=SHARED_SITE,
        failure_domain="POWER",
        alarm_code="ANYTHING",
        cfg=_cfg(),
        query_text="generator fuel",
    )
    assert found == (), f"an airtel episode reached a safaricom recall: {_numbers(found)}"


def test_without_a_query_text_the_lexical_tier_does_not_run(tmp_db):
    """The M0 path is untouched: no ``query_text``, no second tier, no extra query.

    Every M0 test exercises exactly this path, and the hot-path advisory deliberately uses it
    too (see ``services/memory.advisory_for_incident``).
    """
    settings, session = tmp_db
    _seed_episode(
        session,
        number="FUEL",
        site_id="SFC-MTK-BTS-MCH04",
        days_ago=5,
        notes=(("Vendor Desk", "Generator fuel delivered, SERVICE RESTORED"),),
    )
    _consolidate_all(session, settings)
    assert (
        recall_similar_episodes(
            session,
            site_id=SHARED_SITE,
            failure_domain="POWER",
            alarm_code="POWER_GRID_FAIL",
            cfg=_cfg(),
        )
        == ()
    )


@pytest.mark.parametrize(
    "query_text",
    ['"', "NOT AND OR", "heading:*", "site*", "   ", "the and of", "a"],
)
def test_fts_syntax_typed_by_a_human_is_neutralised_rather_than_raising(tmp_db, query_text):
    """Every term is quoted before it reaches FTS5 (MEM9: vendor free text is data, never syntax).

    ``"``, ``*``, ``:`` and the bare boolean keywords are FTS5 *operators*: unquoted, they
    raise a syntax error straight out of an advisory read. Quoted, they are words that match
    nothing. An empty result is the right answer; a 500 on an incident workspace is not.
    """
    settings, session = tmp_db
    _seed_episode(session, number="FUEL", days_ago=2, notes=(("Vendor Desk", "fuel delivered"),))
    _consolidate_all(session, settings)
    found = recall_similar_episodes(
        session,
        site_id="SFC-MTK-BTS-MCH04",
        failure_domain="POWER",
        alarm_code="NOPE",
        cfg=_cfg(),
        query_text=query_text,
    )
    assert found == ()


def test_the_lexical_tier_degrades_to_nothing_before_the_index_exists(tmp_db):
    """A file that has never been consolidated has no index. The exact tier is unaffected —
    it reads ``incidents`` — so recall is weaker, never wrong, and never an error."""
    settings, session = tmp_db
    _seed_episode(session, number="H-0", days_ago=1, notes=(("Vendor Desk", "fuel delivered"),))
    found = recall_similar_episodes(
        session,
        site_id=SHARED_SITE,
        failure_domain="POWER",
        alarm_code="POWER_GRID_FAIL",
        cfg=_cfg(),
        query_text="fuel",
    )
    assert _numbers(found) == ["H-0"]
    assert found[0].match_reason == MATCH_SAME_SITE_AND_FAULT_CLASS


# =================================================================================
# Section 3c — recall_for_incident and the advisory block (M1, §7.11.4)
# =================================================================================


def test_recall_for_incident_with_the_flag_off_returns_a_degraded_bundle(tmp_db, monkeypatch):
    """§7.11.11 test 18. The flag is checked HERE, at the entry point where memory reaches a
    human — and "off" costs not one query, which is what MEM11's hot-path budget needs."""
    settings, session = tmp_db
    _seed_episode(session, number="H-0", days_ago=1)
    monkeypatch.delenv("MEMORY_ENABLED", raising=False)

    bundle = recall_for_incident(
        session, site_id=SHARED_SITE, failure_domain="POWER", alarm_code="POWER_GRID_FAIL"
    )
    assert bundle.degraded is True
    assert bundle.similar == () and bundle.fault_class == ()
    assert bundle.token_estimate == 0


def test_recall_for_incident_on_an_empty_store_is_degraded_but_does_not_raise(tmp_db, monkeypatch):
    settings, session = tmp_db
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    bundle = recall_for_incident(
        session, site_id="NO-SUCH-SITE", failure_domain="POWER", alarm_code="X"
    )
    assert bundle.degraded is True and bundle.similar == ()


def test_recall_for_incident_is_budgeted_and_reports_what_it_costs(tmp_db, monkeypatch):
    """§7.11.5 capability 7: ≤ 8 facts and ≤ 5 episodes, not a 15-25k-token history dump.

    ``token_estimate`` is the number a caller that is about to prompt needs, and it exists so
    the bundle is truncated rather than silently oversized (§7.11.11 test 20).
    """
    settings, session = tmp_db
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    for i in range(12):
        _seed_episode(session, number=f"H-{i}", days_ago=1 + i)

    bundle = recall_for_incident(
        session, site_id=SHARED_SITE, failure_domain="POWER", alarm_code="POWER_GRID_FAIL"
    )
    limits = memory_settings(_cfg())
    assert len(bundle.similar) == limits["similar_limit"] == 5
    assert len(bundle.fault_class) <= limits["recall_limit"]
    assert bundle.degraded is False
    assert 0 < bundle.token_estimate < 2_000, "a bundle this size should be hundreds of tokens"


def test_recall_for_incident_never_returns_the_other_operators_history(tmp_db, monkeypatch):
    """MEM10 through the single entry point every reader uses — the one that matters most,
    because it is the call on the hot path."""
    settings, session = tmp_db
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    for i in range(3):
        _seed_episode(session, number=f"ATL-{i}", operator_id="airtel", days_ago=1 + i)

    bundle = recall_for_incident(
        session, site_id=SHARED_SITE, failure_domain="POWER", alarm_code="POWER_GRID_FAIL"
    )
    assert bundle.similar == ()


#: What no hot-path statement may use (review M03). The operator indexes select "every row
#: this operator owns" — on a single-operator deployment, the whole table — and a full SCAN of
#: any of these tables is the same thing without an index. Every statement must instead be
#: bounded by the site, the fault class or a primary/unique key.
_FORBIDDEN_PLAN_FRAGMENTS = (
    "ix_incidents_operator_id",
    "ix_memory_episodes_operator_id",
    "SCAN incidents",
    "SCAN memory_episodes",
    "SCAN work_notes",
    "SCAN agent_runs",
    "SCAN agent_run_steps",
    # Round 3: the pseudonymisation marker is read on the hot path. It must stay one
    # primary-key probe (its id is derived from the incident id), never a walk of the audit log.
    "SCAN audit_events",
)


def _bulk_history(session, *, site_ids, start: int, fault_alarm: str = "POWER_GRID_FAIL") -> None:
    """Finished incidents plus their consolidated episodes, inserted in bulk.

    Bulk because the point of the tests below is the READ; building 20,000 episodes through
    the consolidator would test the consolidator's speed instead.
    """
    from sqlalchemy import insert

    from noc_agents.db.models import new_id
    from noc_agents.db.models_memory import MemoryEpisodeRow

    now = utcnow()
    incidents, episodes = [], []
    for n, site in enumerate(site_ids):
        i = start + n
        incident_id = new_id()
        ended = now - timedelta(days=1 + i % 700, minutes=i % 1440)
        started = ended - timedelta(minutes=30 + i % 200)
        incidents.append(
            dict(
                id=incident_id, operator_id="safaricom", incident_number=f"SHAPE-{i}", status="CLOSED",
                priority="P3", users_affected=1_000, site_id=site, site_name=site, site_type="BTS",
                region_code="NBI_E", failure_domain="POWER", alarm_code=fault_alarm,
                correlation_fingerprint="shape", created_at=started, updated_at=ended,
                outage_start_at=started, restored_at=ended, restored_source="MARK_RESTORED",
                closed_at=ended, resolution_code="FIELD_RESTORED", resolution_summary="Generator refuelled",
                assignee_type="MSP", msp_name="EGYPRO",
            )
        )
        episodes.append(
            dict(
                id=new_id(), operator_id="safaricom", incident_id=incident_id,
                incident_number=f"SHAPE-{i}", site_id=site, site_type="BTS", region_code="NBI_E",
                failure_domain="POWER", alarm_code=fault_alarm,
                fault_class=f"POWER|{fault_alarm}|BTS", restore_minutes=30 + i % 200,
                closed_at=ended, built_at=now,
            )
        )
    session.execute(insert(IncidentRow), incidents)
    session.execute(insert(MemoryEpisodeRow), episodes)
    session.commit()


def _current_incident() -> IncidentRow:
    """The alarm in front of the approver — not persisted, exactly as the HITL node holds it."""
    return IncidentRow(
        id="00000000-0000-0000-0000-00000000cafe", operator_id="safaricom", incident_number="INC000001",
        status="ASSIGNED", site_id=SHARED_SITE, site_type="BTS", region_code="NBI_E",
        failure_domain="POWER", alarm_code="POWER_GRID_FAIL", correlation_fingerprint="now",
        assignee_type="MSP", msp_name="EGYPRO", responsible_msp="EGYPRO",
    )


def _profile_advisory(session, inc) -> tuple[int, list[tuple[str, tuple]], dict]:
    """``(sqlite_vm_steps, statements, block)`` for one advisory_for_incident call.

    VM steps are counted with ``sqlite3.Connection.set_progress_handler(…, 1)``: SQLite calls it
    once per virtual-machine instruction, so the count is the work the database did — and it is
    the same on an idle laptop and on a CI box running three other suites, which is exactly
    what a wall-clock bound under load could not give (review M07).
    """
    from sqlalchemy import event

    from noc_agents.services.memory import advisory_for_incident

    raw = session.connection().connection.driver_connection
    engine = session.get_bind()
    steps = [0]
    statements: list[tuple[str, tuple]] = []

    def count() -> int:
        steps[0] += 1
        return 0

    def capture(_conn, _cursor, statement, parameters, _context, _many):
        statements.append((statement, tuple(parameters or ())))

    event.listen(engine, "before_cursor_execute", capture)
    raw.set_progress_handler(count, 1)
    try:
        block = advisory_for_incident(session, inc, _cfg())
    finally:
        raw.set_progress_handler(None, 1)
        event.remove(engine, "before_cursor_execute", capture)
    return steps[0], statements, block


def _plans(session, statements) -> list[tuple[str, str]]:
    """``EXPLAIN QUERY PLAN`` for every SELECT the advisory issued, with its real parameters."""
    raw = session.connection().connection.driver_connection
    out = []
    for statement, params in statements:
        if not statement.lstrip().upper().startswith("SELECT"):
            continue
        detail = " | ".join(row[3] for row in raw.execute("EXPLAIN QUERY PLAN " + statement, params).fetchall())
        out.append((statement.split("FROM", 1)[-1][:80].strip(), detail))
    return out


def test_every_hot_path_statement_is_bounded_by_site_fault_class_or_key_never_by_operator(tmp_db, monkeypatch):
    """Review M03: the advisory runs inside the lifecycle's write transaction on every HITL card.

    Every SELECT it issues is captured and its plan checked — before and after ``ANALYZE``, so
    the verdict does not rest on the planner's no-statistics heuristics. The first version's
    ``_load_in_order`` was ``operator_id = ? AND id IN (…5 ids…)``; with no statistics SQLite
    chose ``ix_incidents_operator_id`` and walked every incident the operator owned.
    """
    settings, session = tmp_db
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    _bulk_history(session, site_ids=[SHARED_SITE] * 100, start=0)
    _bulk_history(session, site_ids=[f"SFC-OTHER-{i % 500:03d}" for i in range(2_000)], start=100)

    for label in ("no statistics", "after ANALYZE"):
        _steps, statements, block = _profile_advisory(session, _current_incident())
        assert block is not None and len(block["similar"]) == 5 and block["fault_class"], (
            "the profiled call must do the real work"
        )
        plans = _plans(session, statements)
        assert plans, "no statement was captured"
        offending = [(frm, plan) for frm, plan in plans if any(f in plan for f in _FORBIDDEN_PLAN_FRAGMENTS)]
        assert offending == [], f"{label}: hot-path statement(s) not bounded by site/fault/key: {offending}"
        session.execute(sql("ANALYZE"))
        session.commit()


def test_the_advisory_costs_the_same_with_twenty_thousand_incidents_at_other_sites(tmp_db, monkeypatch):
    """Review M03/M07, measured as query shape rather than wall-clock.

    The advisory's cost must depend on the SITE's history and the fault class's recent sample —
    never on how big the operator's whole table is. So: profile the call, add 20,000 finished
    incidents (with episodes, same fault class) at OTHER sites, and profile it again. A plan
    bounded by site and key grows by a B-tree level at most; the operator-index scan the first
    version ran grew by every one of the 20,000 rows. Counted in SQLite VM steps, which a busy
    machine cannot inflate.
    """
    settings, session = tmp_db
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    _bulk_history(session, site_ids=[SHARED_SITE] * 100, start=0)
    _bulk_history(session, site_ids=[f"SFC-OTHER-{i % 500:03d}" for i in range(1_000)], start=100)
    before, _, block_before = _profile_advisory(session, _current_incident())

    _bulk_history(session, site_ids=[f"SFC-FAR-{i % 4_000:04d}" for i in range(20_000)], start=10_000)
    after, _, block_after = _profile_advisory(session, _current_incident())

    assert [e["incident_number"] for e in block_after["similar"]] == [
        e["incident_number"] for e in block_before["similar"]
    ], "the same site history must produce the same card"
    assert after <= before * 1.25, (
        f"advisory VM steps grew from {before} to {after} (x{after / before:.2f}) when 20,000 incidents "
        "were added at OTHER sites — a hot-path statement is scanning the operator, not the site"
    )


#: The ACCEPTED cost of one site's history, in SQLite VM steps per prior finished incident at
#: that site (fix round 3, secondary). The exact tier reads the site's whole finished history
#: through ``ix_incidents_site_id`` and sorts it (``USE TEMP B-TREE FOR ORDER BY``) before its
#: ``LIMIT``: bounded by the SITE, as review M03 requires, but linear in that site's history.
#: Measured slope: ~30 steps per incident, flat from 100 to 5,000 at one site (10,548 → 39,995 →
#: 69,799 → 158,923 steps). In wall-clock on the shared dev box that is ~15-23 ms at 100 prior
#: outages and best 34 ms / median 94 ms (under load) at 5,000 — a site with 5,000 prior
#: outages of ONE fault class is past MEM11's 25 ms target. Accepted because removing the sort
#: needs an index on ``incidents`` (``db/migrate.py``, not this lane) and because the live-table
#: read is what lets a ticket closed a minute ago be recalled before any job has run. The bound
#: is 40 — a third over the measured slope — so this fails on a regression that makes each
#: site row costlier or the growth super-linear, and not on noise.
_ACCEPTED_VM_STEPS_PER_SITE_INCIDENT = 40


def test_one_sites_growing_history_costs_at_most_the_accepted_steps_per_incident(tmp_db, monkeypatch):
    """The tripwire the first review asked for and nothing had: grow ONE site's history and pin
    what each extra prior outage there may cost the hot path (see the constant above)."""
    settings, session = tmp_db
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    _bulk_history(session, site_ids=[f"SFC-OTHER-{i % 500:03d}" for i in range(1_000)], start=100_000)
    _bulk_history(session, site_ids=[SHARED_SITE] * 100, start=0)
    before, _, block_before = _profile_advisory(session, _current_incident())

    _bulk_history(session, site_ids=[SHARED_SITE] * 1_000, start=100)
    after, _, block_after = _profile_advisory(session, _current_incident())

    assert len(block_before["similar"]) == len(block_after["similar"]) == 5
    per_incident = (after - before) / 1_000
    assert per_incident <= _ACCEPTED_VM_STEPS_PER_SITE_INCIDENT, (
        f"each prior incident at the site now costs {per_incident:.1f} VM steps on the hot path "
        f"(accepted: {_ACCEPTED_VM_STEPS_PER_SITE_INCIDENT}); {before} -> {after} steps for +1,000"
    )


def test_the_lexical_tier_ranks_this_operator_without_the_others_rows_moving_it(tmp_db, monkeypatch):
    """Review M05, the reviewer's reproduction. Two Safaricom episodes compete for one lexical
    slot: SAF-A (strong match, old, P4) and SAF-B (weak match, recent, P3, 225k users).

    The first version normalised bm25 over the top 200 rows of the SHARED index before dropping
    Airtel's, so a single Airtel ticket with 'fuel' in a long body flipped Safaricom's top hit
    from SAF-A to SAF-B and changed the scores on the wire. Ranking and normalisation now run
    over this operator's rows only; the other tenant's text cannot move them.
    """
    settings, session = tmp_db

    def seed(op, number, site, body, days, users, prio):
        ended = utcnow() - timedelta(days=days)
        started = ended - timedelta(minutes=60)
        inc = IncidentRow(
            operator_id=op, incident_number=number, status="CLOSED", priority=prio, users_affected=users,
            site_id=site, site_name=site, site_type="HUB", region_code="NBI_E", failure_domain="POWER",
            alarm_code="X", correlation_fingerprint=f"{site}|x", created_at=started, outage_start_at=started,
            failure_time=started, restored_at=ended, restored_source="MARK_RESTORED", closed_at=ended,
            resolution_code="FIELD_RESTORED", resolution_summary=body, assignee_type="MSP", msp_name="EGYPRO",
        )
        session.add(inc)
        session.commit()
        return inc

    mine = [
        seed("safaricom", "SAF-A", "S1", "genset fuel starvation genset fuel", 60, 1000, "P4"),
        seed("safaricom", "SAF-B", "S2", "fuel pump replaced breaker unrelated words here and more padding text", 1, 225000, "P3"),
    ] + [seed("safaricom", f"SAF-F{i}", "S3", "fibre cut splice repaired by vendor team", 5, 1, "P4") for i in range(8)]
    for inc in mine:
        consolidate_incident(session, settings=settings, incident_id=inc.id)
    session.commit()
    query = dict(site_id="NOWHERE", failure_domain="POWER", alarm_code="X", site_type="HUB",
                 query_text="genset fuel starvation")

    def ranked(limit):
        return [(e.incident_number, round(e.score, 6)) for e in recall_similar_episodes(session, limit=limit, **query)]

    alone_top1, alone_all = ranked(1), ranked(5)
    assert alone_top1[0][0] == "SAF-A"

    theirs = seed("airtel", "ATL-X", "S9", "fuel " + "lorem ipsum dolor sit amet consectetur " * 30, 1, 1, "P4")
    monkeypatch.setenv("OPERATOR_PROFILE", "airtel")
    clear_settings_cache()
    try:
        consolidate_incident(session, settings=get_settings(), incident_id=theirs.id)
        session.commit()
    finally:
        monkeypatch.setenv("OPERATOR_PROFILE", "safaricom")
        clear_settings_cache()

    assert ranked(1) == alone_top1, "another operator's note changed this operator's top hit"
    assert ranked(5) == alone_all, "another operator's note changed this operator's scores or order"


def test_the_advisory_is_withheld_when_the_incident_is_not_the_process_operators(tmp_db, monkeypatch):
    """Review M13, the reviewer's reproduction. Recall takes its operator from the PROCESS
    profile; a Safaricom incident handed to an Airtel-profile process used to get Airtel's
    site history and fault-class median frozen onto its card. It now gets no advisory at all —
    the only safe answer to "whose memory is this?" when the two disagree."""
    from noc_agents.services.memory import advisory_for_incident

    _settings, session = tmp_db
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    for i in range(5):
        _seed_episode(session, number=f"ATL-{i}", operator_id="airtel", days_ago=1 + i)
    safaricom_cfg = _cfg()
    safaricom_incident = IncidentRow(
        operator_id="safaricom", incident_number="INC-S", site_id=SHARED_SITE, site_type="BTS",
        region_code="NBI_E", failure_domain="POWER", alarm_code="POWER_GRID_FAIL", correlation_fingerprint="x",
    )

    monkeypatch.setenv("OPERATOR_PROFILE", "airtel")
    clear_settings_cache()
    try:
        assert advisory_for_incident(session, safaricom_incident, safaricom_cfg) is None
    finally:
        monkeypatch.setenv("OPERATOR_PROFILE", "safaricom")
        clear_settings_cache()
    block = advisory_for_incident(session, safaricom_incident, safaricom_cfg)
    assert block is not None and block["similar"] == [], "Safaricom has no history here; Airtel's must not appear"


def test_the_advisory_block_is_plain_json_with_z_stamped_timestamps(tmp_db, monkeypatch):
    """The block travels on two wires this lane does not own.

    One of them is ``hitl_tasks.proposed_payload_json``, whose setter is a bare
    ``json.dumps`` with no encoder — a ``datetime`` anywhere in this dict would raise inside
    the HITL node and take a P1 approval card down with it. So every timestamp is an ISO
    string ending in ``Z`` (§7.0.6, defect #41) and the whole block is JSON by construction.
    """
    settings, session = tmp_db
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    _seed_episode(session, number="H-0", days_ago=1)

    bundle = recall_for_incident(
        session, site_id=SHARED_SITE, failure_domain="POWER", alarm_code="POWER_GRID_FAIL"
    )
    block = advisory_block(bundle)
    json.dumps(block)  # raises TypeError if anything in here is not JSON
    assert block["similar"][0]["closed_at"].endswith("Z")


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
