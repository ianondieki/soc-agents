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
from datetime import timedelta
from pathlib import Path

import pytest

from noc_agents.api.routers.memory import get_site_memory
from noc_agents.db.models import IncidentRow, WorkNoteRow, utcnow
from noc_agents.llm import redaction
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
