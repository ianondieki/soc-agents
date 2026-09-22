"""Agent memory M1 (spec §7.11.3-§7.11.8): what the consolidator writes, and what it refuses.

``memory_episodes`` is a **derived index** — every column in it is arithmetic over
``incidents`` and ``work_notes``. That makes the interesting tests here not "did it copy the
row" but the four properties a derived index has to have before anything may be aggregated
over it:

1. **Idempotence** (§7.11.11 test 11). The scheduler retries, the outbox pattern redelivers,
   the backfill is run twice by whoever is not sure it finished. Consolidating the same
   incident twice must leave one row with the same values — including the 3×IQR verdict,
   which is the one value that could have drifted, because it depends on a population the
   first run changed.
2. **Refusal of untrustworthy durations** (§7.11.7). ``VENDOR_NOTE_INFERRED`` is a regex hit
   on the word "RESTORED" in a vendor's free text (brief defect #4) and a NULL provenance is
   ``close_incident``'s ``restored_at = closed_at`` back-fill. Both produce a number that
   looks exactly like an MTTR. A wrong duration admitted here is a wrong median in M2 and an
   engineer told to expect four hours for a twenty-minute job, so it is refused at the source
   — and the episode still exists, because the outage still happened.
3. **Nothing written on the hot path** (MEM5). ``runner._fail_closed`` rolls back the run,
   its steps, the incident and the notes; a memory row written mid-run would die with it, or
   worse, half-survive. The test here is the exit criterion: a fail-closed run writes ZERO
   memory rows.
4. **Operator isolation** (MEM10). Both licensees share one SQLite file. Consolidation is a
   write, and a write into the wrong tenant is worse than a read from it.

Privacy is ``test_memory_privacy.py``; recall and the FTS tier are ``test_memory_recall.py``;
the advisory bundle's inertness is ``test_memory_advisory_is_inert.py``.
"""

from __future__ import annotations

import ast
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy import text as sql

from noc_agents.config import clear_settings_cache, get_settings
from noc_agents.db.models import AuditRow, IncidentRow, ScheduledJobStateRow, WorkNoteRow, utcnow
from noc_agents.db.models_memory import MemoryEpisodeRow
from noc_agents.domain.schemas import EventIngest
from noc_agents.graph.pipeline import process_event
from noc_agents.memory import consolidate as consolidator
from noc_agents.memory.schema import FTS_TABLE, REF_NOTE, REF_RESOLUTION, ensure_memory_schema
from noc_agents.realtime.hub import hub
from noc_agents.services.lifecycle import (
    RESTORE_SOURCE_MARK,
    RESTORE_SOURCE_NOTE,
    RESTORE_SOURCE_SUPERVISOR,
)
from noc_agents.services.memory import (
    EPISODE_SUMMARY_MAX_CHARS,
    fault_class,
    fault_class_prior,
    is_pseudonymised,
    recall_similar_episodes,
    recall_site_history,
    restore_minutes_population,
)

SITE = "SFC-NBIE-HUB-EMB"


def _seed(
    session,
    *,
    number: str,
    site_id: str = SITE,
    operator_id: str = "safaricom",
    status: str = "CLOSED",
    priority: str = "P2",
    users_affected: int = 450_000,
    failure_domain: str = "POWER",
    alarm_code: str = "POWER_GRID_FAIL",
    site_type: str = "HUB",
    region_code: str = "NBI_E",
    days_ago: float = 5,
    restore_minutes: int = 120,
    restored_source: str | None = RESTORE_SOURCE_MARK,
    resolution_code: str = "FIELD_RESTORED",
    resolution_summary: str = "Generator refuelled and mains restored",
    assignee_type: str = "MSP",
    msp_name: str | None = "EGYPRO",
    assignee_name: str | None = None,
    started_at: datetime | None = None,
    notes: tuple[tuple[str, str], ...] = (),
    **overrides,
) -> IncidentRow:
    """One finished incident written straight to ``incidents`` — the consolidator's input.

    Not routed through ``process_event`` on purpose: this file is about what the derivation
    does with rows that are already there, and the pipeline would impose its own numbering,
    correlation and recurrence behaviour on every fixture.
    """
    ended = utcnow() - timedelta(days=days_ago)
    started = started_at if started_at is not None else ended - timedelta(minutes=restore_minutes)
    if started_at is not None:
        ended = started_at + timedelta(minutes=restore_minutes)
    values = dict(
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
        failure_time=started,
        restored_at=ended if status in ("RESTORED", "CLOSED") else None,
        restored_source=restored_source,
        closed_at=ended if status == "CLOSED" else None,
        resolution_code=resolution_code,
        resolution_summary=resolution_summary,
        assignee_type=assignee_type,
        msp_name=msp_name,
        responsible_msp=msp_name,
        assignee_name=assignee_name,
    )
    values.update(overrides)
    inc = IncidentRow(**values)
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


def _episodes(session) -> list[MemoryEpisodeRow]:
    return list(session.scalars(select(MemoryEpisodeRow).order_by(MemoryEpisodeRow.incident_number)).all())


def _fts_rows(session, incident_id: str | None = None) -> list[tuple]:
    ensure_memory_schema(session)
    query = f"SELECT body, ref_kind, incident_id, site_id, fault_class FROM {FTS_TABLE}"
    params: dict = {}
    if incident_id:
        query += " WHERE incident_id = :iid"
        params["iid"] = incident_id
    return list(session.execute(sql(query), params).all())


def _snapshot(row: MemoryEpisodeRow) -> dict:
    """Every column except ``built_at`` — which is a clock reading, not a derivation."""
    return {
        c.name: getattr(row, c.name)
        for c in MemoryEpisodeRow.__table__.columns
        if c.name not in ("built_at",)
    }


# =================================================================================
# Section 1 — idempotence, the exit criterion
# =================================================================================


def test_consolidating_the_same_incident_twice_yields_one_episode(tmp_db):
    """§7.11.11 test 11. The UNIQUE ``incident_id`` is the backstop; the update is the design.

    A retried tick, a redelivered job and a second backfill must converge. Row *count* is the
    headline, but the column-by-column comparison is the real assertion: an index that grows
    one row is obvious, an index whose values wobble between runs is not.
    """
    settings, session = tmp_db
    inc = _seed(session, number="INC-M1-1")

    assert consolidator.consolidate_incident(session, settings=settings, incident_id=inc.id) == 1
    session.commit()
    first = _snapshot(_episodes(session)[0])

    assert consolidator.consolidate_incident(session, settings=settings, incident_id=inc.id) == 1
    session.commit()
    rows = _episodes(session)

    assert len(rows) == 1, "a second consolidation created a second episode"
    assert _snapshot(rows[0]) == first, "a derived value moved between two identical runs"


def test_reconsolidating_does_not_duplicate_the_fts_rows(tmp_db):
    """Delete-then-insert, never merge: the index must equal the derivation.

    A merge would leave the old row behind — and when the edit was somebody removing a name
    from a note, that stale row is the leak (§7.11.8).
    """
    settings, session = tmp_db
    inc = _seed(session, number="INC-M1-1", notes=(("Vendor Desk", "Generator refuelled at site"),))

    for _ in range(3):
        consolidator.consolidate_incident(session, settings=settings, incident_id=inc.id)
    session.commit()

    rows = _fts_rows(session, inc.id)
    kinds = sorted(r[1] for r in rows)
    assert kinds == [REF_NOTE, REF_RESOLUTION], kinds


def test_an_edited_note_replaces_its_index_row_rather_than_adding_one(tmp_db):
    settings, session = tmp_db
    inc = _seed(session, number="INC-M1-1", notes=(("Vendor Desk", "gate locked, waiting"),))
    consolidator.consolidate_incident(session, settings=settings, incident_id=inc.id)
    session.commit()

    note = session.scalars(select(WorkNoteRow).where(WorkNoteRow.incident_id == inc.id)).one()
    note.body = "gate opened, generator refuelled"
    session.commit()
    consolidator.consolidate_incident(session, settings=settings, incident_id=inc.id)
    session.commit()

    bodies = [r[0] for r in _fts_rows(session, inc.id)]
    assert "gate opened, generator refuelled" in bodies
    assert "gate locked, waiting" not in bodies, "the stale index row survived the edit"


# =================================================================================
# Section 2 — what an episode is, and what it is not
# =================================================================================


@pytest.mark.parametrize("status", ["CLOSED", "RESTORED"])
def test_finished_incidents_become_episodes(tmp_db, status):
    settings, session = tmp_db
    inc = _seed(session, number="INC-M1-1", status=status)
    assert consolidator.consolidate_incident(session, settings=settings, incident_id=inc.id) == 1


@pytest.mark.parametrize("status", ["NEW", "IN_PROGRESS", "CANCELLED"])
def test_an_unfinished_or_cancelled_incident_is_not_an_episode(tmp_db, status):
    """A cancelled ticket is NOC bookkeeping, not something that happened at the site.

    Counting it would inflate every "Nth outage here" figure derived from this population —
    the same rule ``services/memory.episode_statuses()`` applies to live-table recall, which
    is why both read it from one place.
    """
    settings, session = tmp_db
    inc = _seed(session, number="INC-M1-1", status=status)
    assert consolidator.consolidate_incident(session, settings=settings, incident_id=inc.id) == 0
    assert _episodes(session) == []


def test_an_unknown_incident_id_writes_nothing_and_does_not_raise(tmp_db):
    settings, session = tmp_db
    assert consolidator.consolidate_incident(session, settings=settings, incident_id="nope") == 0


def test_the_derived_columns_are_the_ones_the_spec_names(tmp_db):
    """One pass over the §7.11.3 column list with values a reader can check by eye."""
    settings, session = tmp_db
    inc = _seed(
        session,
        number="INC-M1-1",
        restore_minutes=90,
        notes=(("Vendor Desk", "Generator refuelled, SERVICE RESTORED"),),
    )
    inc.acknowledged_at = inc.outage_start_at + timedelta(minutes=6)
    inc.sla_ack_due = inc.outage_start_at + timedelta(minutes=15)
    inc.sla_restore_due = inc.outage_start_at + timedelta(minutes=240)
    inc.escalated_at = inc.outage_start_at + timedelta(minutes=10)
    inc.first_vendor_note_at = inc.outage_start_at + timedelta(minutes=40)
    session.commit()

    consolidator.consolidate_incident(session, settings=settings, incident_id=inc.id)
    session.commit()
    row = _episodes(session)[0]

    assert row.operator_id == "safaricom"
    assert row.incident_number == "INC-M1-1"
    assert row.fault_class == fault_class("POWER", "POWER_GRID_FAIL", "HUB") == "POWER|POWER_GRID_FAIL|HUB"
    assert row.restore_minutes == 90
    assert row.ack_minutes == 6
    assert row.vendor_response_minutes == 30  # first_vendor_note_at − escalated_at (§7.6.2)
    assert row.sla_ack_met is True
    assert row.sla_restore_met is True
    assert row.resolution_code == "FIELD_RESTORED"
    assert row.closed_at == inc.closed_at
    assert row.source_version == consolidator.SOURCE_VERSION
    # A ROLE token, never a name (§7.11.8 rule 1).
    assert row.assignee_token == "MSP-EGYPRO-POWER"


def test_the_stored_summary_is_capped_at_five_hundred_not_two_forty(tmp_db):
    """The stored row is the derivation; 240 is the *evidence* cap applied on the way out
    (§7.11.3). Re-deriving a longer summary later must not need a backfill."""
    settings, session = tmp_db
    inc = _seed(session, number="INC-M1-1", resolution_summary="x" * 4000)
    consolidator.consolidate_incident(session, settings=settings, incident_id=inc.id)
    session.commit()
    assert len(_episodes(session)[0].resolution_summary) == EPISODE_SUMMARY_MAX_CHARS


def test_the_restoring_note_is_recorded_beside_the_summary_it_produced(tmp_db):
    """``resolution_summary`` is very often empty — the pipeline writes a generic code and
    leaves the prose to whoever closed the ticket. The fallback note's id is stored so the
    quote is traceable to the note the *lifecycle* called the restore."""
    settings, session = tmp_db
    inc = _seed(
        session,
        number="INC-M1-1",
        resolution_summary="",
        notes=(
            ("Vendor Desk", "Engineer dispatched, gate locked"),
            ("Vendor Desk", "Generator refuelled, SERVICE RESTORED at site"),
        ),
    )
    consolidator.consolidate_incident(session, settings=settings, incident_id=inc.id)
    session.commit()
    row = _episodes(session)[0]

    assert "Generator refuelled" in row.resolution_summary
    assert row.restoring_note_id is not None
    note = session.get(WorkNoteRow, row.restoring_note_id)
    assert "SERVICE RESTORED" in note.body


# =================================================================================
# Section 3 — the duration refusals (§7.11.7, the defect #4 guard)
# =================================================================================


@pytest.mark.parametrize(
    "source, expected",
    [
        (RESTORE_SOURCE_MARK, 120),
        (RESTORE_SOURCE_SUPERVISOR, 120),
        (RESTORE_SOURCE_NOTE, None),
        (None, None),
    ],
)
def test_restore_minutes_is_only_stored_for_trustworthy_provenance(tmp_db, source, expected):
    """§7.11.11 test 13, and §7.0.8's M4 rule applied at the write.

    ``VENDOR_NOTE_INFERRED`` means a regex matched "RESTORED" inside vendor free text;
    a NULL source means ``close_incident`` back-filled ``restored_at = closed_at`` with no
    provenance at all. Recall already refuses both; the stored row must agree, or the median
    would be computed from numbers the panel declines to show.
    """
    settings, session = tmp_db
    inc = _seed(session, number="INC-M1-1", restore_minutes=120, restored_source=source)
    consolidator.consolidate_incident(session, settings=settings, incident_id=inc.id)
    session.commit()
    assert _episodes(session)[0].restore_minutes == expected


def test_a_restore_that_predates_the_outage_yields_no_duration(tmp_db):
    """§7.11.11 test 12. A negative duration is a data error; storing it as a negative MTTR
    hides that, and storing it as an absolute value invents one."""
    settings, session = tmp_db
    inc = _seed(session, number="INC-M1-1")
    inc.restored_at = inc.outage_start_at - timedelta(minutes=5)
    session.commit()
    consolidator.consolidate_incident(session, settings=settings, incident_id=inc.id)
    session.commit()
    assert _episodes(session)[0].restore_minutes is None


def test_an_incident_without_a_duration_still_becomes_an_episode(tmp_db):
    """The outage happened, so it counts toward "how often does this site fail" — it simply
    never feeds a median. Dropping the row instead would lose a real failure."""
    settings, session = tmp_db
    inc = _seed(session, number="INC-M1-1", restored_source=RESTORE_SOURCE_NOTE)
    consolidator.consolidate_incident(session, settings=settings, incident_id=inc.id)
    session.commit()
    rows = _episodes(session)
    assert len(rows) == 1 and rows[0].restore_minutes is None


def test_a_three_times_iqr_outlier_is_refused_and_leaves_the_median_alone(tmp_db):
    """§7.11.7's outlier rule. One ticket closed a week late moves a median on its own.

    Four tight episodes establish the quartiles; the fifth is an order of magnitude out and
    is stored with ``restore_minutes = NULL``. The episode is kept — the outage happened —
    and the population the fault-class prior is computed over is untouched by it.
    """
    settings, session = tmp_db
    for i, minutes in enumerate((100, 110, 120, 130)):
        inc = _seed(session, number=f"INC-TIGHT-{i}", restore_minutes=minutes, days_ago=20 + i)
        consolidator.consolidate_incident(session, settings=settings, incident_id=inc.id)
    session.commit()

    freak = _seed(session, number="INC-FREAK", restore_minutes=10_000, days_ago=1)
    consolidator.consolidate_incident(session, settings=settings, incident_id=freak.id)
    session.commit()

    rows = {r.incident_number: r for r in _episodes(session)}
    assert rows["INC-FREAK"].restore_minutes is None, "a 3xIQR outlier reached the index"
    assert rows["INC-FREAK"].restored_source == RESTORE_SOURCE_MARK, "the episode itself is kept"
    key = fault_class("POWER", "POWER_GRID_FAIL", "HUB")
    assert sorted(restore_minutes_population(session, fault_class_key=key)) == [100, 110, 120, 130]


def test_below_four_samples_nothing_is_called_an_outlier(tmp_db):
    """Refusing to judge is the fail-safe direction: a wrong duration kept is visible in the
    evidence ids, a right duration discarded is invisible."""
    settings, session = tmp_db
    for i, minutes in enumerate((100, 110)):
        inc = _seed(session, number=f"INC-{i}", restore_minutes=minutes, days_ago=10 + i)
        consolidator.consolidate_incident(session, settings=settings, incident_id=inc.id)
    session.commit()
    odd = _seed(session, number="INC-ODD", restore_minutes=9_000, days_ago=1)
    consolidator.consolidate_incident(session, settings=settings, incident_id=odd.id)
    session.commit()

    rows = {r.incident_number: r for r in _episodes(session)}
    assert rows["INC-ODD"].restore_minutes == 9_000


def test_the_outlier_verdict_survives_a_second_consolidation(tmp_db):
    """The reason the incident is excluded from its own comparison set.

    Without the exclusion the first run judges a value against a population that does not
    contain it and the second against one that does — and a borderline duration would flip
    between two runs of an operation that is supposed to be idempotent.
    """
    settings, session = tmp_db
    for i, minutes in enumerate((100, 110, 120, 130)):
        inc = _seed(session, number=f"INC-TIGHT-{i}", restore_minutes=minutes, days_ago=20 + i)
        consolidator.consolidate_incident(session, settings=settings, incident_id=inc.id)
    session.commit()

    for number, minutes in (("INC-FREAK", 10_000), ("INC-NORMAL", 115)):
        inc = _seed(session, number=number, restore_minutes=minutes, days_ago=1)
        for _ in range(2):
            consolidator.consolidate_incident(session, settings=settings, incident_id=inc.id)
        session.commit()

    rows = {r.incident_number: r for r in _episodes(session)}
    assert rows["INC-FREAK"].restore_minutes is None
    assert rows["INC-NORMAL"].restore_minutes == 115


def test_sla_restore_met_is_withheld_when_the_restore_provenance_is_not_trusted(tmp_db):
    """A "restore" the system inferred from a substring cannot be the evidence that a restore
    SLA was met. ``None`` is "we do not know", which is a different fact from "missed"."""
    settings, session = tmp_db
    inc = _seed(session, number="INC-M1-1", restored_source=RESTORE_SOURCE_NOTE)
    inc.sla_restore_due = inc.outage_start_at + timedelta(minutes=240)
    session.commit()
    consolidator.consolidate_incident(session, settings=settings, incident_id=inc.id)
    session.commit()
    assert _episodes(session)[0].sla_restore_met is None


# =================================================================================
# Section 4 — EAT derivation (MEM8)
# =================================================================================


def test_the_hour_month_and_shift_are_derived_in_nairobi_time_not_utc(tmp_db):
    """MEM8. The database stores naive UTC; "is this a rain-season pattern?" is an EAT question.

    22:30 UTC on 31 March is 01:30 EAT on **1 April** — a different hour, a different day, a
    different month and the night shift. Getting this wrong would file a third of every year's
    incidents under the wrong month and silently smear the MAM rain-season signal the site
    profile is meant to find.
    """
    settings, session = tmp_db
    inc = _seed(
        session,
        number="INC-M1-1",
        started_at=datetime(2026, 3, 31, 22, 30),
        restore_minutes=30,
        days_ago=0,
    )
    consolidator.consolidate_incident(session, settings=settings, incident_id=inc.id)
    session.commit()
    row = _episodes(session)[0]

    assert row.hour_of_day == 1, "hour_of_day is UTC, not EAT"
    assert row.month_of_year == 4, "month_of_year is UTC, not EAT — the rain-season signal moves"
    assert row.shift_type == "NIGHT"


def test_a_midday_outage_lands_on_the_day_shift(tmp_db):
    settings, session = tmp_db
    inc = _seed(session, number="INC-M1-1", started_at=datetime(2026, 6, 10, 9, 0), days_ago=0)
    consolidator.consolidate_incident(session, settings=settings, incident_id=inc.id)
    session.commit()
    row = _episodes(session)[0]
    assert (row.hour_of_day, row.month_of_year, row.shift_type) == (12, 6, "DAY")


# =================================================================================
# Section 5 — operator isolation on the WRITE path (MEM10)
# =================================================================================


def test_the_consolidator_will_not_write_another_operators_incident(tmp_db):
    """Both licensees share one SQLite file. A write into the wrong tenant is worse than a read.

    The operator clause comes from ``api.deps._owned`` — the single door — so an airtel id
    handed to a safaricom process simply does not come back, and 0 is returned rather than a
    row being written under the wrong ``operator_id``.
    """
    settings, session = tmp_db
    theirs = _seed(session, number="ATL-1", operator_id="airtel")
    mine = _seed(session, number="SAF-1", operator_id="safaricom")

    assert consolidator.consolidate_incident(session, settings=settings, incident_id=theirs.id) == 0
    assert consolidator.consolidate_incident(session, settings=settings, incident_id=mine.id) == 1
    session.commit()
    assert [r.incident_number for r in _episodes(session)] == ["SAF-1"]


def test_a_fault_class_prior_never_mixes_the_two_operators(tmp_db, monkeypatch):
    """Asserted from both sides, at the same site and the same fault class — the colocation
    case, which is what a naive site-keyed query gets wrong while looking entirely plausible.

    A one-directional check passes just as happily against a query hard-wired to
    ``operator_id = 'safaricom'`` as against a correct one.
    """
    settings, session = tmp_db
    for i in range(4):
        _seed(session, number=f"SAF-{i}", operator_id="safaricom", restore_minutes=100, days_ago=10 + i)
        _seed(session, number=f"ATL-{i}", operator_id="airtel", restore_minutes=900, days_ago=10 + i)
    key = fault_class("POWER", "POWER_GRID_FAIL", "HUB")

    for incident in session.scalars(select(IncidentRow).where(IncidentRow.operator_id == "safaricom")).all():
        consolidator.consolidate_incident(session, settings=settings, incident_id=incident.id)
    session.commit()
    assert restore_minutes_population(session, fault_class_key=key) == [100] * 4

    monkeypatch.setenv("OPERATOR_PROFILE", "airtel")
    clear_settings_cache()
    try:
        airtel_settings = get_settings()
        for incident in session.scalars(
            select(IncidentRow).where(IncidentRow.operator_id == "airtel")
        ).all():
            consolidator.consolidate_incident(
                session, settings=airtel_settings, incident_id=incident.id
            )
        session.commit()
        assert restore_minutes_population(session, fault_class_key=key) == [900] * 4
        assert [h.numeric for h in fault_class_prior(session, fault_class_key=key)] == [900.0, 900.0]
    finally:
        monkeypatch.setenv("OPERATOR_PROFILE", "safaricom")
        clear_settings_cache()

    assert restore_minutes_population(session, fault_class_key=key) == [100] * 4
    assert [h.numeric for h in fault_class_prior(session, fault_class_key=key)] == [100.0, 100.0]


def test_a_prior_below_min_support_is_never_returned(tmp_db):
    """§7.11.4: "No hit with ``support_count < min_support`` is ever returned."

    Two prior outages are an anecdote. Rendering "median 214 min" from them on a P1 approval
    card is how an advisory becomes a wrong expectation, which is the confidently-wrong
    neighbour in its most expensive form.
    """
    settings, session = tmp_db
    key = fault_class("POWER", "POWER_GRID_FAIL", "HUB")
    for i in range(2):
        inc = _seed(session, number=f"INC-{i}", restore_minutes=100, days_ago=5 + i)
        consolidator.consolidate_incident(session, settings=settings, incident_id=inc.id)
    session.commit()
    assert fault_class_prior(session, fault_class_key=key) == ()

    inc = _seed(session, number="INC-3", restore_minutes=100, days_ago=2)
    consolidator.consolidate_incident(session, settings=settings, incident_id=inc.id)
    session.commit()
    hits = fault_class_prior(session, fault_class_key=key)
    assert [h.key for h in hits] == ["median_restore_min", "p90_restore_min"]
    assert hits[0].support_count == 3
    assert hits[0].evidence, "a fact with no evidence ids is a naked assertion (§7.11.4)"


# =================================================================================
# Section 6 — MEM5: nothing is written on the hot path
# =================================================================================


HUB_EVENT = dict(
    site_id=SITE,
    site_name="Embakasi East Aggregation HUB",
    site_type="HUB",
    region_code="NBI_E",
    alarm_code="POWER_GRID_FAIL",
    failure_domain="POWER",
    users_affected=450000,
    access_notes="Genset not started",
)


def _boom(*_args, **_kwargs):
    raise RuntimeError("boom")


def test_a_fail_closed_run_writes_zero_memory_rows(tmp_db, monkeypatch):
    """The MEM5 exit criterion, asserted the only way that means anything: force a real
    rollback and count.

    ``runner._fail_closed`` rolls back the run, its steps, the audit rows, the incident, the
    sequence bump and the notes. A memory row written inside an agent would die with it — and
    a *partially* consolidated episode could survive a later partial commit and be
    indistinguishable from a real one. Consolidation therefore runs post-commit only, and this
    test is what keeps that true when somebody decides it would be convenient to "just write
    the episode while we are here".
    """
    settings, session = tmp_db
    survivor = _seed(session, number="INC-SURVIVOR")
    hub._history.clear()
    monkeypatch.setattr("noc_agents.agents.ticket.next_incident_number", _boom)

    with pytest.raises(RuntimeError, match="boom"):
        process_event(session, settings, EventIngest(**HUB_EVENT))
    session.rollback()

    assert _episodes(session) == [], "a memory row was written during a fail-closed run"
    assert _fts_rows(session) == [], "an FTS row was written during a fail-closed run"

    # ... and the surviving incident still consolidates afterwards (§7.11.11 test 15).
    assert consolidator.consolidate_incident(session, settings=settings, incident_id=survivor.id) == 1
    hub._history.clear()


def test_a_successful_run_also_writes_no_memory_row(tmp_db):
    """MEM5 is not only about rollback: the hot path never writes memory at all.

    The HITL node *reads* memory (spec line 419) and this is the other half of that statement
    — the read is a read.
    """
    settings, session = tmp_db
    hub._history.clear()
    process_event(session, settings, EventIngest(**HUB_EVENT))
    assert _episodes(session) == []
    assert _fts_rows(session) == []
    hub._history.clear()


# =================================================================================
# Section 7 — the scheduled job
# =================================================================================


def test_the_job_card_ships_disabled(tmp_db):
    """``default_enabled=False`` so ``/scheduler/status`` reports the job as off while
    ``MEMORY_ENABLED`` is unset, rather than claiming it is enabled and producing nothing.

    This asserts the card's own posture, not its absence from ``SCHEDULED_JOBS``: the card is
    built to be wired in, and a test that forbade wiring it in would forbid the lane from ever
    shipping.
    """
    card = consolidator.MEMORY_CONSOLIDATE_JOB
    assert card.name == "memory_consolidate"
    assert card.default_enabled is False
    assert card.enabled_env == "MEMORY_ENABLED"
    assert (card.agent, card.graph_name) == ("RecurrenceProblemAgent", "memory")
    assert card.interval_s == 300


def test_the_job_rechecks_its_own_flag_and_writes_nothing_when_it_is_off(tmp_db, monkeypatch):
    """A card wired into the loop on a machine that never opted in must be **inert**, not
    merely unscheduled — the same posture ``pollers.weather.poll`` and ``services.pir.auto_open``
    take. Off means no read, no write, one skipped step."""
    settings, session = tmp_db
    _seed(session, number="INC-M1-1")
    monkeypatch.delenv("MEMORY_ENABLED", raising=False)

    result = consolidator.consolidate_recent(session, settings)
    session.commit()

    assert "MEMORY_ENABLED=false" in result.summary
    assert _episodes(session) == []


def test_the_job_consolidates_what_finished_since_the_last_tick(tmp_db, monkeypatch):
    settings, session = tmp_db
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    _seed(session, number="INC-NEW", days_ago=0.01)
    _seed(session, number="INC-OPEN", status="IN_PROGRESS", days_ago=0.01)

    result = consolidator.consolidate_recent(session, settings)
    session.commit()

    assert [r.incident_number for r in _episodes(session)] == ["INC-NEW"]
    assert "episodes=1" in result.summary


def test_the_job_picks_up_an_incident_that_closed_while_the_previous_tick_ran(tmp_db, monkeypatch):
    """Why the work queue is derived from state rather than from a cursor.

    The scheduler stamps ``last_finished_at`` *after* the job returns, so an incident that
    closed while the previous tick was running is older than that stamp. A pure
    ``>= last_finished_at`` cursor would skip it forever; the "not current" filter cannot.
    """
    settings, session = tmp_db
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    _seed(session, number="INC-RACE", days_ago=0.01)  # closed ~15 minutes ago
    now = utcnow()
    session.add(
        ScheduledJobStateRow(name=consolidator.JOB_NAME, last_started_at=now, last_finished_at=now)
    )
    session.commit()

    consolidator.consolidate_recent(session, settings)
    session.commit()
    assert [r.incident_number for r in _episodes(session)] == ["INC-RACE"]


def test_a_current_episode_leaves_the_queue_and_an_edit_puts_it_back(tmp_db, monkeypatch):
    """The queue is "missing or stale", so a quiet incident is read once and never again, and
    a corrected resolution text is re-derived on the next tick without anyone asking."""
    settings, session = tmp_db
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    inc = _seed(session, number="INC-1", days_ago=0.01)

    consolidator.consolidate_recent(session, settings)
    session.commit()
    assert consolidator.pending_incidents(session, since=None) == []

    inc.resolution_summary = "Corrected: the fix was a rectifier reset, not fuel"
    session.commit()  # onupdate=utcnow moves updated_at past the episode's built_at
    assert consolidator.pending_incidents(session, since=None) == [inc.id]

    consolidator.consolidate_recent(session, settings)
    session.commit()
    assert "rectifier reset" in _episodes(session)[0].resolution_summary
    assert len(_episodes(session)) == 1


def test_a_failed_tick_leaves_its_work_for_the_next_one(tmp_db, monkeypatch):
    """A tick that raises is rolled back by the scheduler's runner. Its incidents are still
    "not current" afterwards, so the next tick redoes them — nothing is lost to a bad tick."""
    settings, session = tmp_db
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    _seed(session, number="INC-1", days_ago=0.01)

    consolidator.consolidate_recent(session, settings)
    session.rollback()  # what run_job does when the job raises after writing
    assert _episodes(session) == []

    consolidator.consolidate_recent(session, settings)
    session.commit()
    assert [r.incident_number for r in _episodes(session)] == ["INC-1"]


def test_the_backfill_dry_run_counts_with_the_same_predicate_and_writes_nothing(tmp_db):
    settings, session = tmp_db
    for i in range(3):
        _seed(session, number=f"INC-{i}", days_ago=10 + i)
    first = _seed(session, number="INC-DONE", days_ago=5)
    consolidator.consolidate_incident(session, settings=settings, incident_id=first.id)
    session.commit()

    assert consolidator.backfill_plan(session) == {"finished": 4, "to_build": 3, "current": 1}
    assert len(_episodes(session)) == 1


def test_the_job_writes_one_audit_row_per_batch_with_real_json(tmp_db, monkeypatch):
    """§7.11.8 auditability. ``payload_json`` is ``json.dumps``, not ``str(dict)`` — the
    existing ``instrumentation.py`` form is brief defect #33 and §4.5 requires real JSON for
    new code, so this is the correct shape rather than the prevailing one."""
    settings, session = tmp_db
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    _seed(session, number="INC-NEW", days_ago=0.01)

    consolidator.consolidate_recent(session, settings)
    session.commit()

    rows = list(session.scalars(select(AuditRow).where(AuditRow.action == "memory.consolidate")).all())
    assert len(rows) == 1
    payload = json.loads(rows[0].payload_json)  # raises if it is a str(dict)
    assert payload["written"] == 1
    assert rows[0].operator_id == "safaricom"


def test_one_bad_incident_does_not_end_a_tick_and_leaves_nothing_half_built(tmp_db, monkeypatch):
    """Review M06. Fail-soft per incident (§4.4) — and atomic per incident.

    The failure is injected INSIDE the build, after the episode row has been added to the
    session (``assignee_role_token`` runs after ``session.add``). The first version had no
    savepoint, so the half-filled row — default ``closed_at``, empty text, ``built_at=now`` —
    was committed with the tick, counted as current, and never offered again. Now the bad
    incident leaves no row at all, stays in the queue, and the good one is built.
    """
    settings, session = tmp_db
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    _seed(session, number="INC-GOOD", days_ago=0.01)
    bad = _seed(session, number="INC-BAD", days_ago=0.02)

    real = consolidator.assignee_role_token

    def flaky(inc, cfg):
        if inc.id == bad.id:
            raise RuntimeError("boom")
        return real(inc, cfg)

    monkeypatch.setattr(consolidator, "assignee_role_token", flaky)
    result = consolidator.consolidate_recent(session, settings)
    session.commit()

    assert "episodes=1" in result.summary and "failed=1" in result.summary, result.summary
    assert [r.incident_number for r in _episodes(session)] == ["INC-GOOD"], "a half-built episode survived"
    assert _fts_rows(session, bad.id) == []
    assert bad.id in consolidator.pending_incidents(session, since=None), (
        "the failed incident must stay in the queue, not be marked current"
    )


def test_a_failure_while_reindexing_keeps_the_previous_episode_and_its_index_rows(tmp_db, monkeypatch):
    """Review M06, the second shape: the FTS rows are deleted, then an insert fails.

    Without a savepoint the tick committed an episode with NO index rows, marked current. With
    it, the delete and the episode refresh roll back together: the old rows are still there,
    unchanged, and the incident is still owed a rebuild.
    """
    settings, session = tmp_db
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    inc = _seed(session, number="INC-1", days_ago=0.01, notes=(("Vendor Desk", "Generator refuelled"),))
    consolidator.consolidate_recent(session, settings)
    session.commit()
    before_rows = sorted(_fts_rows(session, inc.id))
    before_episode = _snapshot(_episodes(session)[0])
    assert len(before_rows) == 2

    inc.resolution_summary = "Corrected summary"
    session.commit()  # the incident changed, so it is owed a rebuild

    def broken_insert(*_args, **_kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(consolidator, "_fts_insert", broken_insert)
    result = consolidator.consolidate_recent(session, settings)
    session.commit()

    assert "failed=1" in result.summary, result.summary
    assert sorted(_fts_rows(session, inc.id)) == before_rows, "the index was wiped by a failed rebuild"
    assert _snapshot(_episodes(session)[0]) == before_episode, "a half-refreshed episode was committed"
    assert inc.id in consolidator.pending_incidents(session, since=None)


def test_a_savepoint_nests_inside_the_callers_transaction_rather_than_committing(tmp_db, monkeypatch):
    """The trap the savepoint fix nearly fell into, pinned.

    The ``sqlite3`` driver opens SQLite's transaction lazily, at the first write. A session
    that has only read is not, at the SQLite level, in a transaction — and a SAVEPOINT taken
    then starts one that its RELEASE *commits*. Measured before the fix: consolidate, then
    ``rollback()``, and the episode was still there. The caller must own the transaction.
    """
    settings, session = tmp_db
    inc = _seed(session, number="INC-1", days_ago=1)
    session.commit()

    assert consolidator.consolidate_incident(session, settings=settings, incident_id=inc.id) == 1
    session.rollback()
    assert _episodes(session) == [], "consolidate_incident committed behind its caller's back"


# =================================================================================
# Section 8 — retention (§7.11.8, the housekeeping seam) and the 24-month horizon
# =================================================================================

#: expire_memory is called with a ``now`` this far in the future, so episodes that were
#: legitimately built (inside the horizon today) have aged past it by then. Building them
#: already-ancient is no longer possible — review M12's guard refuses to — which is the point.
_LATER = timedelta(days=100)


def test_expire_memory_prunes_episodes_past_the_twenty_four_month_horizon(tmp_db):
    settings, session = tmp_db
    old = _seed(session, number="INC-OLD", days_ago=700, notes=(("Vendor Desk", "mains restored"),))
    recent = _seed(session, number="INC-RECENT", days_ago=30)
    for inc in (old, recent):
        assert consolidator.consolidate_incident(session, settings=settings, incident_id=inc.id) == 1
    session.commit()

    counts = consolidator.expire_memory(session, settings=settings, now=utcnow() + _LATER)
    session.commit()

    assert counts["episodes_pruned"] == 1
    assert counts["fts_rows_pruned"] >= 1
    assert [r.incident_number for r in _episodes(session)] == ["INC-RECENT"]
    assert _fts_rows(session, old.id) == [], "the pruned episode's index rows survived"


def test_expire_memory_runs_regardless_of_the_read_flag(tmp_db, monkeypatch):
    """§9.4, and the property ``services/housekeeping.py`` states for this seam.

    Retention must never depend on a **read** flag. A lane switched off for reading still
    holds derived rows, and "we stopped looking at it" is not a defence for keeping them.
    """
    settings, session = tmp_db
    old = _seed(session, number="INC-OLD", days_ago=700)
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    consolidator.consolidate_incident(session, settings=settings, incident_id=old.id)
    session.commit()

    monkeypatch.delenv("MEMORY_ENABLED", raising=False)
    counts = consolidator.expire_memory(session, settings=settings, now=utcnow() + _LATER)
    session.commit()
    assert counts["episodes_pruned"] == 1


def test_expire_memory_writes_an_audit_row_and_says_it_ignored_the_flag(tmp_db):
    settings, session = tmp_db
    old = _seed(session, number="INC-OLD", days_ago=700)
    consolidator.consolidate_incident(session, settings=settings, incident_id=old.id)
    session.commit()
    consolidator.expire_memory(session, settings=settings, now=utcnow() + _LATER)
    session.commit()

    row = session.scalars(select(AuditRow).where(AuditRow.action == "memory.expire")).one()
    payload = json.loads(row.payload_json)
    assert payload["episodes_pruned"] == 1
    assert payload["gated_on_memory_enabled"] is False


def test_expire_memory_on_an_empty_store_is_a_no_op(tmp_db):
    """Housekeeping runs nightly on every deployment, including ones that never consolidated."""
    settings, session = tmp_db
    assert consolidator.expire_memory(session, settings=settings) == {
        "episodes_pruned": 0,
        "fts_rows_pruned": 0,
    }


def test_the_housekeeping_seam_now_resolves_to_this_module(tmp_db):
    """``services/housekeeping.MEMORY_EXPIRY_SEAM`` names this exact path, and housekeeping
    imports it lazily. The seam is only real if the dotted path resolves and the signature
    matches what the seam calls: ``lane_expire(session, settings=settings, now=...)``."""
    from noc_agents.services.housekeeping import MEMORY_EXPIRY_SEAM

    module_path, _, func_name = MEMORY_EXPIRY_SEAM.partition(":")
    module = __import__(module_path, fromlist=[func_name])
    lane_expire = getattr(module, func_name)

    settings, session = tmp_db
    counts = lane_expire(session, settings=settings, now=utcnow())
    assert isinstance(counts, dict)


def test_housekeepings_own_seam_calls_this_function_in_apply_and_skips_it_in_dry_run(tmp_db):
    """End to end through ``services/housekeeping.expire_memory`` — the real caller, unmocked.

    The seam documents two properties and this lane must keep both: it runs regardless of
    ``MEMORY_ENABLED`` (the conftest pins the flag off, so this whole test runs with reads
    disabled) and it is NOT called in dry run, because the lane's function has no dry-run mode.
    """
    from noc_agents.services import housekeeping as hk

    settings, session = tmp_db
    old = _seed(session, number="INC-OLD", days_ago=700)
    consolidator.consolidate_incident(session, settings=settings, incident_id=old.id)
    session.commit()

    later = utcnow() + _LATER
    dry = hk.expire_memory(session, settings, hk.load_policy(), now=later, apply=False)
    assert dry.available is True and dry.called is False
    assert len(_episodes(session)) == 1, "a dry run pruned memory"

    applied = hk.expire_memory(session, settings, hk.load_policy(), now=later, apply=True)
    session.commit()  # housekeeping's duty wrapper commits after each duty; this stands in for it
    assert applied.called is True
    assert applied.counts["episodes_pruned"] == 1
    assert _episodes(session) == []


def _index_blob_text(session) -> str:
    """Every byte of FTS5's own storage — the segment b-trees as well as the content table.

    ``memory_note_fts_data`` holds the inverted index; a deleted row's terms linger there
    until a merge unless FTS5 secure-delete is on. Decoded latin-1 so a term stored as raw
    bytes is still found by a substring check.
    """
    parts: list[str] = []
    for shadow in (f"{FTS_TABLE}_data", f"{FTS_TABLE}_content"):
        for row in session.execute(sql(f"SELECT * FROM {shadow}")).all():
            parts.extend(v.decode("latin-1") if isinstance(v, bytes) else str(v) for v in row)
    return " ".join(parts).lower()


def test_a_reconsolidation_erases_the_old_text_from_the_index_storage_not_just_the_rows(tmp_db):
    """Review M09 (contested; checked and true). A name written into the index before it was
    known — the documented no-NER case — and scrubbed on a later rebuild once it is known,
    must be gone from FTS5's segment storage, not merely from ``MATCH`` results.

    Reproduced first: without secure-delete, ``memory_note_fts_data`` still held "wekesa"
    after the rebuild. ``ensure_memory_schema`` now turns FTS5 secure-delete on.
    """
    settings, session = tmp_db
    inc = _seed(
        session,
        number="INC-1",
        assignee_type="NOC",
        msp_name=None,
        resolution_summary="Fixed by the team",
        notes=(("NOC", "Wekesa Barasa refuelled genset"),),
    )
    consolidator.consolidate_incident(session, settings=settings, incident_id=inc.id)
    session.commit()
    assert "wekesa" in _index_blob_text(session), "precondition: the unknown name was indexed"

    inc.fe_name = "Wekesa Barasa"  # the ticket is corrected; the name becomes known
    session.commit()
    consolidator.consolidate_incident(session, settings=settings, incident_id=inc.id)
    session.commit()

    blob = _index_blob_text(session)
    assert "wekesa" not in blob and "barasa" not in blob, "the old terms survived in FTS5 storage"


def test_the_prune_erases_the_pruned_text_from_the_index_storage(tmp_db):
    """Review M09 for ``expire_memory``: a prune that leaves the terms in the segments has not
    erased anything. secure-delete removes them at the DELETE, and the prune also runs FTS5
    'optimize' as the backstop for an index built before secure-delete was switched on."""
    settings, session = tmp_db
    inc = _seed(session, number="INC-OLD", days_ago=700, notes=(("NOC", "Kiprono Tanui opened the gate"),))
    consolidator.consolidate_incident(session, settings=settings, incident_id=inc.id)
    session.commit()
    assert "kiprono" in _index_blob_text(session)

    consolidator.expire_memory(session, settings=settings, now=utcnow() + _LATER)
    session.commit()
    assert "kiprono" not in _index_blob_text(session), "the pruned text is still in FTS5 storage"


def test_the_backfill_and_the_job_never_build_beyond_the_horizon(tmp_db, monkeypatch):
    """Review M12. An incident that ended beyond ``episode_max_age_days`` (730) is still a
    statutory record in ``incidents`` (licence floor 1,095 days) — but memory must not hold it.
    The first version's backfill rebuilt whatever expire_memory had just pruned; with
    housekeeping in its default dry-run posture, nothing would ever prune it again."""
    settings, session = tmp_db
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    ancient = _seed(session, number="INC-ANCIENT", days_ago=800)
    _seed(session, number="INC-RECENT", days_ago=10)

    assert consolidator.consolidate_incident(session, settings=settings, incident_id=ancient.id) == 0
    counts = consolidator.consolidate_all(session, settings=settings)
    assert counts["written"] == 1 and counts["considered"] == 1, counts
    assert [r.incident_number for r in _episodes(session)] == ["INC-RECENT"]
    assert consolidator.backfill_plan(
        session, horizon=utcnow() - timedelta(days=730)
    ) == {"finished": 1, "to_build": 0, "current": 1}

    ancient.resolution_summary = "touched today, still 800 days old"
    session.commit()
    consolidator.consolidate_recent(session, settings)
    session.commit()
    assert "INC-ANCIENT" not in [r.incident_number for r in _episodes(session)]


# =================================================================================
# Section 8b — pseudonymised incidents (review M02)
# =================================================================================


def _pseudonymise(session, settings, inc) -> None:
    """Run housekeeping's REAL pseudonymisation on this one incident, in apply posture."""
    from noc_agents.services import housekeeping as hk

    report = hk.pseudonymise_personal_fields(
        session, settings, hk.load_policy(), before=utcnow() + timedelta(days=1), apply=True
    )
    session.commit()
    session.refresh(inc)
    assert report.rows_changed >= 1, "precondition: housekeeping pseudonymised the incident"


@pytest.mark.parametrize("region, rnio", [("NBI_E", "RNIO-NBI-E"), ("MTK", "RNIO-MTK")])
def test_a_pseudonymised_incident_is_never_rebuilt_from_its_source_text(tmp_db, monkeypatch, region, rnio):
    """Review M02, the reviewer's reproduction end to end with the real housekeeping duty.

    Consolidated while ``fe_name`` was a name, the episode and its FTS rows hold tokens.
    Housekeeping then pseudonymises the row — ``fe_name`` becomes ``FE-<region>`` — but leaves
    the notes and ``resolution_summary`` alone, and moves ``updated_at``, so the job sees the
    incident as stale. Re-deriving the text now would be scrubbed by a NameMap that no longer
    knows the name, and write it back. The rule: after pseudonymisation the text is frozen.

    Run in MTK as well as NBI_E (round 3): MTK's live ``rnio_name`` is ``RNIO-MTK`` from the
    very first second, so what freezes the text here must be housekeeping's durable marker —
    not the columns' shape, which was identical before and after pseudonymisation in MTK.
    """
    settings, session = tmp_db
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    inc = _seed(
        session,
        number="INC-PSEUDO",
        days_ago=0.01,
        region_code=region,
        assignee_type="FIELD_ENGINEER",
        msp_name=None,
        fe_name="John Kamau",
        rnio_name=rnio,
        resolution_summary="John Kamau refuelled generator",
        notes=(("NOC", "John Kamau on site, gate opened"),),
    )
    first = consolidator.consolidate_recent(session, settings)
    session.commit()
    assert "text_frozen=0" in first.summary, "a live incident must never be read as pseudonymised"
    assert "kamau" not in _memory_text(session)

    _pseudonymise(session, settings, inc)
    assert inc.fe_name == f"FE-{region}"
    assert inc.id in consolidator.pending_incidents(session, since=None), (
        "precondition: pseudonymisation made the episode stale, so the job WILL revisit it"
    )

    result = consolidator.consolidate_recent(session, settings)
    session.commit()

    assert "text_frozen=1" in result.summary, result.summary
    assert "kamau" not in _memory_text(session), "the rebuild wrote the real name back"
    assert _episodes(session)[0].resolution_summary.startswith("<PERSON_"), "the scrubbed text was lost"
    assert inc.id not in consolidator.pending_incidents(session, since=None)

    consolidator.consolidate_all(session, settings=settings, rebuild=True)  # backfill --rebuild
    assert "kamau" not in _memory_text(session)


def test_an_incident_pseudonymised_before_its_first_consolidation_gets_numbers_but_no_text(tmp_db, monkeypatch):
    """The other half of M02: never consolidated while the names were known, so there is no
    scrubbed text to keep. The episode is built from the network columns only — its durations
    still count toward the fault class — with no summary and no index rows, and it leaves the
    queue instead of being re-offered every tick."""
    settings, session = tmp_db
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    inc = _seed(
        session,
        number="INC-PSEUDO",
        days_ago=0.01,
        assignee_type="FIELD_ENGINEER",
        msp_name=None,
        fe_name="John Kamau",
        resolution_summary="John Kamau refuelled generator",
        notes=(("NOC", "John Kamau on site"),),
    )
    _pseudonymise(session, settings, inc)

    result = consolidator.consolidate_recent(session, settings)
    session.commit()

    row = _episodes(session)[0]
    assert "text_frozen=1" in result.summary
    assert row.resolution_summary == "" and row.restore_minutes == 120
    assert _fts_rows(session, inc.id) == []
    assert "kamau" not in _memory_text(session)
    assert consolidator.pending_incidents(session, since=None) == []


#: Every region the Safaricom profile lists, plus one it does not. For an unlisted region
#: ``services/assignment.py`` falls back to ``f"RNIO-{reg}"`` — the same string retention.yaml's
#: ``RNIO-{region_code}`` renders — which is why it is here.
_PROFILE_REGIONS = sorted(get_settings("safaricom").operator.regions)
_UNLISTED_REGION = "XYZ"


def _assigned_and_closed(session, settings, region: str) -> IncidentRow:
    """An incident whose person columns were written by the REAL ASSIGN node, then closed.

    ``agents/assign.run`` is driven with the two attributes it reads from the state and the
    context, exactly as the reviewers' reproduction did, so the rnio/fe/assignee values are the
    ones production writes for that region — not values a test chose.
    """
    from types import SimpleNamespace

    from noc_agents.agents import assign as assign_node

    ended = utcnow() - timedelta(hours=6)
    started = ended - timedelta(hours=2)
    inc = IncidentRow(
        operator_id="safaricom", incident_number=f"INC-{region}", status="NEW", site_id=f"S-{region}",
        site_type="BTS", region_code=region, failure_domain="POWER", alarm_code="MAINS_FAIL",
        correlation_fingerprint=f"fp-{region}", created_at=started, outage_start_at=started,
    )
    session.add(inc)
    session.flush()
    state = SimpleNamespace(
        event=SimpleNamespace(failure_domain="POWER", site_type="BTS", region_code=region, alarm_code="MAINS_FAIL"),
        incident=inc,
        outage_start=started,
        sla_restore_due=ended,
    )
    assign_node.run(state, SimpleNamespace(cfg=settings.operator))
    inc.status = "CLOSED"
    inc.restored_at = ended
    inc.closed_at = ended
    inc.restored_source = RESTORE_SOURCE_MARK
    inc.resolution_summary = "Generator refuelled and ATS reset after genset fuel starvation"
    session.add(
        WorkNoteRow(incident_id=inc.id, author="Vendor Desk", author_role="MSP",
                    body="genset fuel starvation, refuelled; SERVICE RESTORED", created_at=ended, source="ui")
    )
    session.commit()
    return inc


def test_the_profile_really_has_regions_whose_live_rnio_looks_like_a_retention_token(tmp_db):
    """Keeps the regression test below honest: if no region's ASSIGN output collided with a
    retention token any more, that test would pass whether or not the fix existed."""
    settings, session = tmp_db
    colliding = [
        r for r in _PROFILE_REGIONS + [_UNLISTED_REGION]
        if _assigned_and_closed(session, settings, r).rnio_name == f"RNIO-{r}"
    ]
    assert _UNLISTED_REGION in colliding and len(colliding) >= 2, colliding


@pytest.mark.parametrize("region", _PROFILE_REGIONS + [_UNLISTED_REGION])
def test_every_region_keeps_its_memory_text_through_a_real_tick(tmp_db, monkeypatch, region):
    """Review round 3, the defect itself. The first M02 fix inferred "pseudonymised" from a
    person column looking like its retention token, and the ASSIGN node's own ``RNIO-MTK`` …
    ``RNIO-WNY`` (and ``RNIO-<reg>`` for any unlisted region) looked exactly like one — so in
    four of Safaricom's six regions a brand-new incident was stored with NO text and NO index
    rows and recalled as ''. Every region, through the real assign step and the real job tick,
    must store its summary, index it, and recall it.
    """
    settings, session = tmp_db
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    inc = _assigned_and_closed(session, settings, region)

    result = consolidator.consolidate_recent(session, settings)
    session.commit()

    assert "episodes=1" in result.summary and "text_frozen=0" in result.summary, result.summary
    row = _episodes(session)[0]
    assert row.resolution_summary.startswith("Generator refuelled"), (region, inc.rnio_name, row.resolution_summary)
    assert len(_fts_rows(session, inc.id)) == 2, "the resolution and the note must both be indexed"
    recalled = recall_site_history(session, site_id=f"S-{region}")
    assert recalled and recalled[0].resolution_summary.startswith("Generator refuelled"), recalled
    found = recall_similar_episodes(
        session, site_id="NOWHERE", failure_domain="POWER", alarm_code="MAINS_FAIL",
        query_text="genset fuel starvation",
    )
    assert [e.incident_number for e in found] == [f"INC-{region}"], "the lexical tier lost the incident"


def test_columns_that_merely_look_like_tokens_do_not_freeze_the_text(tmp_db, monkeypatch):
    """The distinction the marker exists for, stated directly: every person column equal to
    its retention token, and no pseudonymisation ever run. Shape proves nothing; only
    housekeeping's record does."""
    settings, session = tmp_db
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    inc = _seed(
        session, number="INC-SHAPE", days_ago=0.01, assignee_type="FIELD_ENGINEER", msp_name=None,
        assignee_name="FIELD_ENGINEER-NBI_E", fe_name="FE-NBI_E", rnio_name="RNIO-NBI_E",
        restored_by="RESTORER-NBI_E", resolution_summary="Rectifier module swapped",
    )
    assert not is_pseudonymised(session, inc)
    consolidator.consolidate_recent(session, settings)
    session.commit()
    assert _episodes(session)[0].resolution_summary == "Rectifier module swapped"


def _memory_text(session) -> str:
    """Every text value in memory_episodes and memory_note_fts, lower-cased."""
    parts: list[str] = []
    for row in session.scalars(select(MemoryEpisodeRow)).all():
        parts.extend(str(getattr(row, c.name)) for c in MemoryEpisodeRow.__table__.columns)
    parts.extend(str(v) for r in _fts_rows(session) for v in r)
    return " ".join(parts).lower()


# =================================================================================
# Section 9 — the backfill
# =================================================================================


def test_the_backfill_builds_every_finished_incident_and_is_idempotent(tmp_db):
    """§7.11.5's backfill row: one-off, idempotent, resumable, safe to re-run."""
    settings, session = tmp_db
    for i in range(7):
        _seed(session, number=f"INC-{i}", days_ago=10 + i)
    _seed(session, number="INC-OPEN", status="IN_PROGRESS")

    first = consolidator.consolidate_all(session, settings=settings, batch=3)
    assert first["written"] == 7
    assert len(_episodes(session)) == 7

    second = consolidator.consolidate_all(session, settings=settings, batch=3)
    assert second["written"] == 0, "a second pass rebuilt rows that were already current"
    assert len(_episodes(session)) == 7


def test_the_backfill_resumes_rather_than_restarting(tmp_db):
    """Progress lives in the rows (``built_at``/``source_version``), not in a cursor file, so
    an interrupted pass is continued by running it again."""
    settings, session = tmp_db
    for i in range(6):
        _seed(session, number=f"INC-{i}", days_ago=10 + i)

    partial = consolidator.consolidate_all(session, settings=settings, batch=2, limit=2)
    assert partial["written"] == 2

    rest = consolidator.consolidate_all(session, settings=settings, batch=2)
    assert rest["written"] == 4
    assert len(_episodes(session)) == 6


def test_rebuild_re_derives_rows_that_are_already_current(tmp_db):
    """What a ``SOURCE_VERSION`` bump needs, and what no schema migration can do."""
    settings, session = tmp_db
    _seed(session, number="INC-0")
    consolidator.consolidate_all(session, settings=settings)
    assert consolidator.consolidate_all(session, settings=settings)["written"] == 0
    assert consolidator.consolidate_all(session, settings=settings, rebuild=True)["written"] == 1


def test_the_backfill_script_runs_end_to_end(tmp_db, monkeypatch, capsys):
    """The CLI itself, not just its engine: an argument-parsing slip in a one-off script is
    only ever found at 02:00 by whoever is running it.

    Loaded by path through ``importlib``, as ``test_agent_docs_render.py`` loads its script:
    ``scripts/`` is not a package, and ``import scripts.x`` would only work when the working
    directory happens to be on ``sys.path``.
    """
    import importlib.util

    script = Path(__file__).resolve().parents[2] / "scripts" / "backfill_memory.py"
    spec = importlib.util.spec_from_file_location("backfill_memory_under_test", script)
    backfill = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(backfill)

    settings, session = tmp_db
    _seed(session, number="INC-0")
    session.commit()
    monkeypatch.setenv("DATABASE_URL", settings.database_url)

    assert backfill.main(["--dry-run"]) == 0
    assert "dry-run" in capsys.readouterr().out
    assert backfill.main([]) == 0
    assert "done" in capsys.readouterr().out


def test_episode_counts_reports_both_tables(tmp_db):
    """The unbounded-growth tripwire of §7.11.4's ``/memory/stats`` (the route itself is M2)."""
    settings, session = tmp_db
    inc = _seed(session, number="INC-0", notes=(("Vendor Desk", "mains restored"),))
    consolidator.consolidate_incident(session, settings=settings, incident_id=inc.id)
    session.commit()
    counts = consolidator.episode_counts(session)
    assert counts["memory_episodes"] == 1
    assert counts[FTS_TABLE] == 2  # the resolution plus one note


# =================================================================================
# Section 10 — one scrubber, not two (the static half of §7.11.8)
# =================================================================================


def test_the_consolidator_defines_no_name_or_contact_pattern_of_its_own(tmp_db):
    """The same static guard ``test_memory_privacy.py`` applies to ``services/memory.py``.

    A second name list is worse than none: it drifts, it gets fixed in one place, and the §9.6
    redaction scan is written against the other. Walks the AST rather than grepping, so a
    pattern assembled inside a nested function is still caught.
    """
    source = Path(consolidator.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module} | {
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    }
    assert "re" not in imports, "memory/consolidate.py must not build its own text patterns"
    assert "noc_agents.llm.redaction" in imports, "the shared redactor must be the one in use"
