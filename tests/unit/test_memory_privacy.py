"""Agent memory M0 (spec §7.11.8, §7.7.6): no person's name ever leaves a recall.

§9.1 draws the line this file polices. Site ids, alarm codes, regions, counts, timestamps
and ``mpesa_risk`` are **network data** and are outside the Data Protection Act's scope, which
is why most of the memory layer is safe by construction. ``assignee_name``, ``fe_name``,
``rnio_name``, work-note authors and the free text people type into ``resolution_summary`` and
note bodies are **personal data**, and recall is the surface that carries that free text
furthest from the ticket it was typed on: a work note written once in June is read at 3 a.m.
in December, on a different incident, by someone who was not there.

The rule (§7.11.8 rule 1) is role tokens and pseudonyms, never names. The mechanism is the
**existing** ``llm/redaction.py`` — ``NameMap`` + ``scrub_text``, seeded exactly as
``redact_incident`` seeds it. Reusing it rather than writing a second name list is itself
under test below: a name the outbound-LLM path knows how to hide must be a name this path
hides too, and there must be exactly one implementation to fix when someone finds a gap.

``redaction.py`` is honest about its limit — there is no NER — and the last test here pins
that limit rather than pretending it away, so nobody reads this file as a guarantee the code
does not make. The compensating controls are named in §7.11.7: the pre-send
``validate_no_contacts`` check and the daily redaction scan of §9.6.
"""

from __future__ import annotations

import ast
import re
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy import text as sql

from noc_agents.api.routers.memory import get_site_memory
from noc_agents.db.models import IncidentRow, WorkNoteRow, utcnow
from noc_agents.db.models_memory import MemoryEpisodeRow
from noc_agents.llm import redaction
from noc_agents.memory.consolidate import consolidate_incident
from noc_agents.memory.schema import FTS_TABLE, ensure_memory_schema
from noc_agents.services import memory as memory_service
from noc_agents.services.memory import (
    SUMMARY_MAX_CHARS,
    episode_dict,
    memory_enabled,
    recall_similar_episodes,
    recall_site_history,
)

SITE = "SFC-NBIE-HUB-EMB"

#: A shift's worth of names, spread across every field the redactor is seeded from. Each is
#: also referred to by first name alone somewhere in the text, because that is how NOC staff
#: actually write notes ("Kevin took over") and it is the case a naive full-string replace
#: silently misses.
ASSIGNEE = "Peter Kamau"
FIELD_ENGINEER = "Grace Wanjiku"
RNIO = "Joseph Otieno"
NOTE_AUTHOR = "Samuel Mutiso"

FORBIDDEN_SUBSTRINGS = (
    "Peter", "Kamau", "Grace", "Wanjiku", "Joseph", "Otieno", "Samuel", "Mutiso",
)

EMAIL = "grace.wanjiku@egypro.co.ke"
MSISDN = "+254 712 345 678"


def _seed(session, **overrides) -> IncidentRow:
    """One closed outage carrying a name in every place a name can be typed."""
    ended = utcnow() - timedelta(days=3)
    started = ended - timedelta(minutes=180)
    values = dict(
        operator_id="safaricom",
        incident_number="INC-PRIV-1",
        status="CLOSED",
        priority="P2",
        users_affected=450_000,
        site_id=SITE,
        site_name="Embakasi East Aggregation HUB",
        site_type="HUB",
        region_code="NBI_E",
        failure_domain="POWER",
        alarm_code="POWER_GRID_FAIL",
        correlation_fingerprint=f"{SITE}|POWER_GRID_FAIL|POWER",
        created_at=started,
        outage_start_at=started,
        restored_at=ended,
        restored_source="MARK_RESTORED",
        closed_at=ended,
        resolution_code="FIELD_RESTORED",
        resolution_summary=(
            f"{ASSIGNEE} dispatched {FIELD_ENGINEER}; Joseph confirmed mains. "
            f"Vendor desk {NOTE_AUTHOR} on {MSISDN}, {EMAIL}."
        ),
        assignee_name=ASSIGNEE,
        fe_name=FIELD_ENGINEER,
        rnio_name=RNIO,
    )
    values.update(overrides)
    inc = IncidentRow(**values)
    session.add(inc)
    session.flush()
    session.add(
        WorkNoteRow(
            incident_id=inc.id,
            author=NOTE_AUTHOR,
            author_role="MSP",
            body="Samuel escorted Grace to site; generator refuelled, SERVICE RESTORED.",
            created_at=ended,
            source="ui",
        )
    )
    session.commit()
    return inc


def _text_blob(payload) -> str:
    """Every string anywhere in a recall result, flattened, so nothing hides in a nested key."""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        return " ".join(_text_blob(v) for v in payload.values())
    if isinstance(payload, (list, tuple)):
        return " ".join(_text_blob(v) for v in payload)
    return ""


def _leaked(blob: str) -> list[str]:
    return [name for name in FORBIDDEN_SUBSTRINGS if name.lower() in blob.lower()]


# =================================================================================
# No name, no contact detail, on any recall surface
# =================================================================================


def test_no_person_name_survives_a_site_history_recall(tmp_db):
    """§7.11.8 rule 1. Full names and first names alike, in the summary and in the note.

    Checked by substring over the whole flattened result rather than field by field, so a
    field added to :class:`SimilarEpisode` later cannot open a hole this test does not see.
    """
    _settings, session = tmp_db
    _seed(session)
    episodes = recall_site_history(session, site_id=SITE)
    assert len(episodes) == 1

    blob = _text_blob(episode_dict(episodes[0]))
    assert _leaked(blob) == [], f"names reached a recall result: {_leaked(blob)}"
    assert redaction.PERSON_PREFIX in blob, "the names were dropped, not tokenised — check the NameMap seeding"


def test_no_person_name_survives_the_exact_tier_either(tmp_db):
    """Every ``recall_*`` is a separate door; both are scrubbed by the same code path."""
    _settings, session = tmp_db
    _seed(session)
    episodes = recall_similar_episodes(
        session, site_id=SITE, failure_domain="POWER", alarm_code="POWER_GRID_FAIL", site_type="HUB"
    )
    assert len(episodes) == 1
    assert _leaked(_text_blob(episode_dict(episodes[0]))) == []


def test_email_addresses_and_kenyan_msisdns_are_replaced_with_tokens(tmp_db):
    """§9.1: MSISDNs never enter the system, and an email address is a contact detail.

    Both are typed into note bodies and resolution summaries constantly. The tokens must be
    present, not merely the raw values absent — a scrubber that deleted the text silently
    would pass an "absent" check while destroying the fact that a contact was recorded.
    """
    _settings, session = tmp_db
    _seed(session)
    blob = _text_blob(episode_dict(recall_site_history(session, site_id=SITE)[0]))

    assert "egypro.co.ke" not in blob and "@" not in blob
    assert "712" not in blob and "254" not in blob
    assert redaction.EMAIL_TOKEN in blob
    assert redaction.PHONE_TOKEN in blob


def test_a_name_known_only_as_a_note_author_is_still_scrubbed_from_the_text(tmp_db):
    """The note-author seeding is load-bearing, not decoration.

    ``NOTE_AUTHOR`` appears in no incident column at all — only as ``work_notes.author``. If
    :func:`services.memory._name_map` stopped registering note authors, the name would
    survive in the resolution text and this is the only test that would notice.
    """
    _settings, session = tmp_db
    _seed(
        session,
        assignee_name=None,
        fe_name=None,
        rnio_name=None,
        resolution_summary=f"{NOTE_AUTHOR} restored mains at site",
    )
    summary = recall_site_history(session, site_id=SITE)[0].resolution_summary
    assert "Mutiso" not in summary and "Samuel" not in summary, summary
    assert redaction.PERSON_PREFIX in summary


def test_the_restoring_note_fallback_is_scrubbed_before_it_is_used(tmp_db):
    """The fallback path reads attacker-reachable vendor free text (MEM9) — scrub it there too.

    An empty ``resolution_summary`` is the common case, so this path is the one most note
    bodies actually travel through; it must not be the one that skipped the scrubber.
    """
    _settings, session = tmp_db
    _seed(session, resolution_summary="")
    summary = recall_site_history(session, site_id=SITE)[0].resolution_summary
    assert "generator refuelled" in summary.lower(), summary
    assert _leaked(summary) == [], summary


def test_names_are_scrubbed_before_the_240_character_cap_not_after(tmp_db):
    """Truncating first would slice "Kamau" into "Kam" — a fragment the scrubber no longer matches.

    The cap sits at a position that lands mid-name here on purpose; the assertion is that the
    name is gone from the *whole* capped string, including its last characters.
    """
    _settings, session = tmp_db
    padding = "mains fault at the hub site. " * 8
    _seed(session, resolution_summary=f"{padding}{ASSIGNEE} attended and restored the site")
    summary = recall_site_history(session, site_id=SITE)[0].resolution_summary
    assert len(summary) == SUMMARY_MAX_CHARS
    assert _leaked(summary) == [], summary


def test_the_api_payload_carries_no_name_and_exactly_the_reviewed_field_set(tmp_db, monkeypatch):
    """What actually goes on the wire, and a pin on which fields go there at all.

    The key set is asserted literally because adding a field to a memory payload is a privacy
    decision: this assertion is the review gate that makes someone justify it.
    """
    _settings, session = tmp_db
    _seed(session)
    monkeypatch.setenv("MEMORY_ENABLED", "true")
    assert memory_enabled()

    payload = get_site_memory(SITE, lookback_days=365, limit=20)
    assert payload["episodes"], "the fixture incident should be recalled"
    assert set(payload["episodes"][0]) == {
        "incident_id",
        "incident_number",
        "site_id",
        "fault_class",
        "closed_at",
        "restore_minutes",
        "resolution_code",
        "resolution_summary",
        "match_reason",
        "score",
    }
    assert _leaked(_text_blob(payload)) == []


# =================================================================================
# One name list, not two
# =================================================================================


def test_recall_reuses_the_projects_only_redaction_implementation(tmp_db):
    """§7.11.8: the scrubber is ``llm/redaction.py``, imported, not re-implemented.

    A second name list is worse than none: it drifts, it gets fixed in one place, and the
    §9.6 redaction scan is written against the other. Identity, not equality — a copy of
    ``NameMap`` under another name would fail this.
    """
    assert memory_service.NameMap is redaction.NameMap
    assert memory_service.scrub_text is redaction.scrub_text


def test_the_memory_module_defines_no_name_or_contact_pattern_of_its_own(tmp_db):
    """Static guard for the same rule: no regex literal, no hard-coded name list in the module.

    Walks the AST rather than grepping, so a pattern assembled in a nested function or a
    class body is still caught.
    """
    source = Path(memory_service.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    }
    assert "re" not in imports, "services/memory.py must not build its own text patterns"
    assert "noc_agents.llm.redaction" in imports, "the shared redactor must be the one in use"


# =================================================================================
# M1: the same rule, applied to the derived tables (§7.11.11 test 5)
# =================================================================================


def _memory_table_text(session) -> str:
    """Every text value in every ``memory_*`` table, flattened — ``memory_note_fts`` included.

    §7.11.11 test 5 says to assert over the **whole table contents**, not field by field, and
    that is the difference between a test and a formality: a column added to
    ``memory_episodes`` later, or a new ``ref_kind`` in the FTS index, is covered by this the
    day it lands rather than the day somebody remembers to extend an assertion list. The
    tables are discovered from the metadata and the catalogue, so there is no list to forget
    to update.
    """
    parts: list[str] = []
    for row in session.scalars(select(MemoryEpisodeRow)).all():
        for column in MemoryEpisodeRow.__table__.columns:
            value = getattr(row, column.name)
            if isinstance(value, str):
                parts.append(value)
    ensure_memory_schema(session)
    for values in session.execute(sql(f"SELECT * FROM {FTS_TABLE}")).all():
        parts.extend(str(v) for v in values if v is not None)
    return " ".join(parts)


def test_no_memory_table_anywhere_contains_a_persons_name(tmp_db):
    """§7.11.8 rule 1, at the write: names are scrubbed BEFORE the row exists, never after.

    The incident here carries a name in every place a name can be typed — the three
    pseudonymised incident columns, the note author, the resolution prose and a first-name-only
    mention inside a note body. After consolidation, no substring of any of them may appear
    anywhere in ``memory_episodes`` or ``memory_note_fts``.

    Scrubbing at the write rather than at the read is the whole design: the FTS index is a
    *searchable* copy of vendor free text, so a name that reached it would be findable by
    anyone with the route, forever, and no amount of care on the read path would help.
    """
    settings, session = tmp_db
    _seed(session)
    incident = session.scalars(select(IncidentRow)).one()

    assert consolidate_incident(session, settings=settings, incident_id=incident.id) == 1
    session.commit()

    blob = _memory_table_text(session)
    assert blob, "nothing was written, so this assertion would pass vacuously"
    assert _leaked(blob) == [], f"names reached a memory table: {_leaked(blob)}"
    assert redaction.PERSON_PREFIX in blob, "the names were dropped, not tokenised — check the NameMap seeding"


def test_contact_details_are_tokenised_in_the_derived_tables_too(tmp_db):
    """MSISDNs and e-mail addresses are typed into notes constantly (§9.1).

    The tokens must be *present*, not merely the raw values absent: a scrubber that deleted
    the text would pass an "absent" check while destroying the fact that a contact was
    recorded at all.
    """
    settings, session = tmp_db
    _seed(session)
    incident = session.scalars(select(IncidentRow)).one()
    consolidate_incident(session, settings=settings, incident_id=incident.id)
    session.commit()

    blob = _memory_table_text(session)
    assert "egypro.co.ke" not in blob and "@" not in blob
    assert "712" not in blob and "254" not in blob
    assert redaction.EMAIL_TOKEN in blob and redaction.PHONE_TOKEN in blob


def test_the_assignee_is_stored_as_a_role_token_not_as_a_person(tmp_db):
    """§7.11.8 rule 1 again, for the one column that is *about* a person.

    ``assignee_token`` holds the §6.1 ``assignee_role_token`` vocabulary — "MSP-EGYPRO-POWER",
    "FE-NBI-E-ONCALL", "NOC-QUEUE". The token↔name mapping never lives in a memory table, so
    even someone holding the whole index cannot turn a token back into a person.
    """
    settings, session = tmp_db
    _seed(session, assignee_type="MSP", msp_name="EGYPRO", responsible_msp="EGYPRO")
    incident = session.scalars(select(IncidentRow)).one()
    consolidate_incident(session, settings=settings, incident_id=incident.id)
    session.commit()

    row = session.scalars(select(MemoryEpisodeRow)).one()
    assert row.assignee_token == "MSP-EGYPRO-POWER"
    assert ASSIGNEE not in (row.assignee_token or ""), "a name reached the role-token column"


def test_one_name_map_is_shared_across_every_field_of_an_incident(tmp_db):
    """The same engineer must be the same token in the summary and in every indexed note.

    Two maps would give one person two tokens, and a reader comparing "<PERSON_1> escorted
    <PERSON_2>" across two rows would conclude four people were on site. This is also why
    ``services/memory.name_map_for`` exists rather than each writer building its own.
    """
    settings, session = tmp_db
    _seed(session, resolution_summary=f"{NOTE_AUTHOR} restored mains at site")
    incident = session.scalars(select(IncidentRow)).one()
    consolidate_incident(session, settings=settings, incident_id=incident.id)
    session.commit()

    row = session.scalars(select(MemoryEpisodeRow)).one()
    ensure_memory_schema(session)
    bodies = [r[0] for r in session.execute(sql(f"SELECT body FROM {FTS_TABLE}")).all()]
    tokens_in_summary = set(re.findall(r"<PERSON_\d+>", row.resolution_summary))
    tokens_in_notes = set(re.findall(r"<PERSON_\d+>", " ".join(bodies)))
    assert tokens_in_summary and tokens_in_notes
    assert tokens_in_summary & tokens_in_notes, (
        "the summary and the indexed notes used different tokens for the same shift"
    )


# =================================================================================
# Review findings M01, M02, M04, M11 — names the first M1 version let through
# =================================================================================


def _open_incident(session, **overrides) -> IncidentRow:
    """An incident still being worked, so the real lifecycle functions can act on it."""
    started = utcnow() - timedelta(hours=3)
    values = dict(
        operator_id="safaricom",
        incident_number="INC-LIVE-1",
        status="ASSIGNED",
        priority="P2",
        users_affected=450_000,
        site_id=SITE,
        site_name="Embakasi East Aggregation HUB",
        site_type="HUB",
        region_code="NBI_E",
        failure_domain="POWER",
        alarm_code="POWER_GRID_FAIL",
        correlation_fingerprint=f"{SITE}|POWER_GRID_FAIL|POWER",
        created_at=started,
        outage_start_at=started,
        assignee_type="FIELD_ENGINEER",
        assignee_name=ASSIGNEE,
    )
    values.update(overrides)
    inc = IncidentRow(**values)
    session.add(inc)
    session.commit()
    return inc


def test_a_reassigned_incidents_previous_assignee_never_reaches_memory(tmp_db):
    """Review M01, reproduced with the REAL lifecycle functions, then fixed.

    ``reassign_incident`` overwrites ``assignee_name`` and writes
    ``"Reassigned Peter Kamau → Grace Wanjiku (FIELD_ENGINEER). Reason: …"`` into a note. A
    NameMap seeded only from today's columns no longer knew Peter Kamau, so that note — and a
    human's "Peter en route" typed before the handover — reached ``memory_note_fts`` verbatim.
    The NameMap now reads both sides of every reassign note, from its structure.
    """
    from noc_agents.services.lifecycle import close_incident, reassign_incident

    settings, session = tmp_db
    inc = _open_incident(session)
    session.add(
        WorkNoteRow(incident_id=inc.id, author="Vendor Desk", author_role="MSP",
                    body="Peter en route, ETA 40 min", source="ui")
    )
    session.commit()
    reassign_incident(
        session, inc, assignee_type="FIELD_ENGINEER", assignee_name=FIELD_ENGINEER,
        msp_name=None, fe_name=FIELD_ENGINEER, by="NOC Desk", reason="shift change",
    )
    session.commit()
    close_incident(session, inc, closed_by="NOC Desk", resolution_summary="Generator refuelled")
    session.commit()

    assert consolidate_incident(session, settings=settings, incident_id=inc.id) == 1
    session.commit()
    blob = _memory_table_text(session)
    assert "Reassigned" in blob, "precondition: the reassign note was indexed"
    assert _leaked(blob) == [], f"a former assignee reached memory: {_leaked(blob)}"
    # And through recall, which builds its NameMap the same way.
    assert _leaked(_text_blob([episode_dict(e) for e in recall_site_history(session, site_id=SITE)])) == []


def test_the_reassign_note_template_is_still_the_one_memory_reads(tmp_db):
    """The anchors in ``services/memory`` are a copy of a string ``lifecycle`` owns.

    This drives the real ``reassign_incident`` and checks both names come back out of the note
    it wrote, so the day that template changes shape, this fails — rather than memory quietly
    going back to leaking every former assignee.
    """
    from noc_agents.services.lifecycle import reassign_incident

    _settings, session = tmp_db
    inc = _open_incident(session)
    reassign_incident(
        session, inc, assignee_type="MSP", assignee_name="EGYPRO", msp_name="EGYPRO",
        fe_name=None, by="NOC Desk", reason="vendor owns power",
    )
    session.commit()
    notes = session.scalars(select(WorkNoteRow).where(WorkNoteRow.incident_id == inc.id)).all()
    assert memory_service._reassigned_names(notes) == [ASSIGNEE, "EGYPRO"]


def test_the_original_assignee_recorded_by_the_assign_step_is_still_scrubbed(tmp_db):
    """Review M01, the other half of the history: an ``fe_name`` the ticket no longer carries.

    The ASSIGN node's step row records ``"FIELD_ENGINEER:<fe>"`` and ``"...; FE=<fe>"`` in its
    rationale. When a later reassign or a correction changes ``fe_name``, that step row is the
    only place the original engineer is still named — and a note typed while they were on the
    ticket still mentions them. ``person_name_history`` reads it back.
    """
    from noc_agents.db.models import AgentRunRow, AgentRunStepRow

    settings, session = tmp_db
    _seed(
        session,
        fe_name="Linet Chebet",  # today's FE — not the one the notes mention
        assignee_name=None,
        rnio_name=None,
        resolution_summary="Otieno Were swapped the rectifier; Otieno signed off",
    )
    inc = session.scalars(select(IncidentRow)).one()
    run = AgentRunRow(incident_id=inc.id, operator_id="safaricom", graph_name="incident_lifecycle")
    session.add(run)
    session.flush()
    session.add(
        AgentRunStepRow(
            run_id=run.id, seq=6, node_name="ASSIGN", agent_name="DispatchAssignmentAgent",
            status="SUCCEEDED", output_summary="FIELD_ENGINEER:Otieno Were",
            rationale="region=NBI_E (Nairobi East); lane=power; pool=['FIELD_ENGINEER']; "
            "primary=FIELD_ENGINEER; radio_oem=MIXED; FE=Otieno Were",
        )
    )
    session.commit()

    consolidate_incident(session, settings=settings, incident_id=inc.id)
    session.commit()
    blob = _memory_table_text(session) + _text_blob(
        [episode_dict(e) for e in recall_site_history(session, site_id=SITE)]
    )
    assert "otieno" not in blob.lower() and "were swapped" not in blob.lower(), blob


def test_restored_by_is_scrubbed_even_when_that_person_wrote_no_note(tmp_db):
    """Review M11. ``restored_by`` is personal data (§9.4) and ``services/pir.py`` already treats
    it as a name; an imported or back-filled ticket can carry it without a matching note."""
    settings, session = tmp_db
    _seed(
        session,
        restored_by="Mary Njeri",
        resolution_summary="Mary Njeri replaced the rectifier module; Mary confirmed",
    )
    inc = session.scalars(select(IncidentRow)).one()
    consolidate_incident(session, settings=settings, incident_id=inc.id)
    session.commit()
    blob = (_memory_table_text(session) + " " + _text_blob(
        [episode_dict(e) for e in recall_site_history(session, site_id=SITE)]
    )).lower()
    assert not re.search(r"\bmary\b", blob) and "njeri" not in blob, blob


def test_a_role_label_in_restored_by_is_not_registered_as_a_person(tmp_db):
    """``lifecycle`` writes ``restored_by = author or author_role``, so an unsigned restoring
    note leaves ``"MSP"`` there. Registering that as a name would tokenise the ordinary NOC
    word everywhere; it is skipped when it matches a note's ``author_role``."""
    _settings, session = tmp_db
    _seed(session, restored_by="MSP", resolution_summary="MSP confirmed mains restored")
    summary = recall_site_history(session, site_id=SITE)[0].resolution_summary
    assert "MSP confirmed" in summary, summary


@pytest.mark.parametrize(
    "fragment, name",
    [
        ("peter_kamau", "kamau"),
        ("Kamau_ok", "kamau"),
        ("Wanjiku2", "wanjiku"),
        ("grace_wanjiku at egypro dot com", "wanjiku"),
        ("2Otieno", "otieno"),
    ],
)
def test_a_known_name_next_to_an_underscore_or_a_digit_is_still_scrubbed(tmp_db, fragment, name):
    """Review M04. The shared scrubber's boundaries were ``\\w``, which counts ``_`` and digits as
    part of a word, so ``peter_kamau`` and ``Wanjiku2`` — handles, usernames, spelled-out
    e-mails — kept a seeded name verbatim. They are letter-only boundaries now
    (``llm/redaction.py``), so ``_`` and digits separate words the way people type them."""
    settings, session = tmp_db
    _seed(session, resolution_summary=f"Escalated to {fragment} on Teams")
    inc = session.scalars(select(IncidentRow)).one()
    consolidate_incident(session, settings=settings, incident_id=inc.id)
    session.commit()
    blob = (_memory_table_text(session) + " " + _text_blob(
        [episode_dict(e) for e in recall_site_history(session, site_id=SITE)]
    )).lower()
    assert name not in blob, blob


def test_letter_only_boundaries_still_protect_ordinary_words(tmp_db):
    """What the ``\\w`` boundary was there for must survive the M04 fix: a short name must not
    eat a longer word that merely contains it."""
    names = redaction.NameMap()
    names.token_for("Ann")
    names.token_for("ATC")
    out = redaction.scrub_text("Announcement: dispatched to Ann; ATC confirmed", names)
    assert "Announcement" in out and "dispatched" in out, out
    assert "Ann;" not in out and "ATC confirmed" not in out, out


def test_a_pseudonymised_incident_is_recalled_from_its_stored_text_never_rederived(tmp_db):
    """Review M02 on the READ path. After housekeeping pseudonymises ``fe_name`` the live
    NameMap no longer knows the name still sitting in ``resolution_summary``; recall must show
    what the episode stored while the name was known — or nothing — and never re-derive."""
    from noc_agents.services import housekeeping as hk

    settings, session = tmp_db
    _seed(
        session,
        assignee_name=None,
        rnio_name=None,
        fe_name="John Kamau",
        resolution_summary="John Kamau refuelled the generator",
    )
    stored, fresh = session.scalars(select(IncidentRow)).one(), None
    consolidate_incident(session, settings=settings, incident_id=stored.id)
    session.commit()
    fresh = _seed(
        session,
        incident_number="INC-PRIV-2",
        assignee_name=None,
        rnio_name=None,
        fe_name="John Kamau",
        resolution_summary="John Kamau refuelled the generator",
    )  # never consolidated

    hk.pseudonymise_personal_fields(
        session, settings, hk.load_policy(), before=utcnow() + timedelta(days=1), apply=True
    )
    session.commit()
    for inc in (stored, fresh):
        session.refresh(inc)
        assert memory_service.is_pseudonymised(session, inc), inc.fe_name

    by_number = {e.incident_number: e for e in recall_site_history(session, site_id=SITE)}
    assert "kamau" not in _text_blob([episode_dict(e) for e in by_number.values()]).lower()
    assert by_number["INC-PRIV-1"].resolution_summary.startswith("<PERSON_")
    assert by_number["INC-PRIV-2"].resolution_summary == ""


@pytest.mark.xfail(
    reason=(
        "Known and accepted (review M08): NameMap registers name PARTS of four letters or more "
        "(redaction.py _NAME_PART_RE), so a three-letter first name used alone — Ann, Ian, "
        "Joy, Eve — is not scrubbed even when the full name is a seeded field. A deliberate "
        "false-positive trade-off: several of those names are ordinary English words that "
        "would otherwise be tokenised out of every note. Kept visible as a strict xfail beside "
        "the no-NER one; it will XPASS the day the rule changes."
    ),
    strict=True,
)
def test_a_three_letter_first_name_used_alone_is_not_recognised(tmp_db):
    settings, session = tmp_db
    _seed(
        session,
        assignee_name="Ann Wambui",
        fe_name="Ian Ochieng",
        rnio_name=None,
        resolution_summary="Ann and Ian on site; ann checked the rectifier",
    )
    inc = session.scalars(select(IncidentRow)).one()
    consolidate_incident(session, settings=settings, incident_id=inc.id)
    session.commit()
    blob = _memory_table_text(session).lower()
    assert not re.search(r"\b(ann|ian)\b", blob), blob


# =================================================================================
# The limit this code does NOT clear — pinned so nobody over-claims it
# =================================================================================


@pytest.mark.xfail(
    reason=(
        "Known and accepted (§7.11.12, redaction.py:21): there is no NER. A person named "
        "only inside free text is registered nowhere and is not recognised. The compensating "
        "controls are validate_no_contacts before any send and the §9.6 daily redaction scan. "
        "This test is xfail rather than deleted so the limit stays visible, and it will "
        "XPASS the day name detection is added."
    ),
    strict=True,
)
def test_a_name_that_appears_only_in_free_text_is_not_recognised(tmp_db):
    _settings, session = tmp_db
    _seed(
        session,
        assignee_name=None,
        fe_name=None,
        rnio_name=None,
        resolution_summary="Benson Kiprotich from the county office opened the gate",
    )
    summary = recall_site_history(session, site_id=SITE)[0].resolution_summary
    assert "Kiprotich" not in summary
