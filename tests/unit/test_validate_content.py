"""Envelope-level content validation (§6.1, CONFORMANCE C-07) and the ``ai_content`` seam.

``services/validators.validate_content(alert)`` judges the ``content{}`` blocks — the only part
of a ``NocAlert`` a model may draft — against the deterministic rest of the envelope. What is
proved here:

* a draft must carry the four facts a reader acts on (incident number, priority, region label,
  next-update EAT time), per language block;
* **the "no invented cause" rule**: a clause opened by ``caused by`` / ``due to`` must name the
  incident's own ``facts.failure_domain`` — "due to a fibre cut" on a POWER incident is refused,
  "POWER outage due to a fibre cut" is refused too (the cause is what FOLLOWS the phrase);
* no MSISDN, e-mail address or assignee name — but an MSP vendor code is not a person;
* findings are codes and facts, never the drafted text (they are stored);
* at the seam, ``build_alert(..., ai_content=...)`` with validation on: a clean draft is used
  and flagged ``ai_assisted``; a violating draft is replaced by the deterministic template,
  ``ai_assisted`` is false, the envelope equals the template build field for field, the
  ``fallback_reason`` reaches ``llm_calls``, and the rendered email carries no trace of it;
* with ``ai_content=None`` (every caller today) the envelope is byte-identical whatever the
  switch says, and the validator is never consulted.
"""

from __future__ import annotations

import logging
from datetime import datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from noc_agents.config import get_settings
from noc_agents.db.models import IncidentRow, LlmCallRow
from noc_agents.domain.alerts import Content
from noc_agents.llm.port import record_llm_call
from noc_agents.llm.structured import LlmCallRecord
from noc_agents.domain.schemas import EventIngest
from noc_agents.graph.pipeline import process_event
from noc_agents.services import alerts, validators
from noc_agents.services.alerts import build_alert, content_validation_context, default_content
from noc_agents.services.render import render_email
from noc_agents.services.render.email import next_update_eat
from noc_agents.services.validators import (
    FALLBACK_REASON_CONTENT_INVALID,
    content_fallback_reason,
    validate_content,
)

SENT = datetime(2026, 9, 16, 10, 47, 10)  # naive UTC = 13:47:10 EAT
INC, PRIORITY, REGION = "INC000123", "P2", "Nairobi East"


@pytest.fixture()
def cfg():
    return get_settings().operator


def _row(**overrides) -> IncidentRow:
    """A populated, unflushed P2 POWER incident (same shape as the renderer tests' row)."""
    base = dict(
        id="inc-0001",
        operator_id="safaricom",
        incident_number=INC,
        status="ASSIGNED",
        priority=PRIORITY,
        users_affected=450000,
        service_affecting=True,
        site_id="SFC-NBIE-HUB-EMB",
        site_name="Embakasi East Aggregation HUB",
        site_type="HUB",
        region_code="NBI_E",
        county="Nairobi",
        title="[POWER_GRID] HUB POWER — Embakasi East Aggregation HUB (Nairobi East)",
        narrative="Service-affecting event detected at Embakasi East Aggregation HUB (HUB) in Nairobi East (NBI_E).",
        root_cause_hypothesis="Suspected Commercial power failure; awaiting field/MSP confirmation.",
        assignee_type="MSP",
        assignee_name="EGYPRO",
        msp_name="EGYPRO",
        fe_name="FE-NBI-E-01",
        rnio_name="RNIO-NBI-E",
        access_notes=None,
        correlation_fingerprint="SFC-NBIE-HUB-EMB|POWER_GRID_FAIL|POWER",
        mpesa_risk=True,
        failure_domain="POWER",
        alarm_code="POWER_GRID_FAIL",
        tt_category="POWER_GRID",
        child_sites_down=0,
        child_site_ids_json="[]",
        radio_oem="MIXED",
        responsible_msp=None,
        next_update_at=None,
        outage_start_at=datetime(2026, 9, 16, 10, 41, 0),
        failure_time=None,
        restored_at=None,
        msp_root_cause=None,
    )
    base.update(overrides)
    row = IncidentRow(**base)
    row.services_impacted = ["VOICE", "DATA", "SMS", "MPESA_CORRIDOR"]
    return row


def _template(cfg, **row) -> object:
    return build_alert(_row(**row), cfg, sent=SENT, alert_id="alert-1")


def _draft(cfg, *, body: str | None = None, headline: str | None = None, **row) -> Content:
    """A draft that carries all four facts; ``body`` / ``headline`` replace the defaults."""
    nu = next_update_eat(_template(cfg, **row))
    return Content(
        headline=headline if headline is not None else f"[{PRIORITY}] {INC} Embakasi East HUB down, {REGION}",
        body=body if body is not None else f"{INC} ({PRIORITY}, {REGION}): the HUB lost mains supply. POWER team dispatched.",
        instruction=f"Next update {nu}.",
    )


def _with_content(cfg, en: Content, sw: Content | None = None, **row):
    """The envelope with the draft in place and NO seam validation — what validate_content judges."""
    content = {"en": en} if sw is None else {"en": en, "sw": sw}
    return build_alert(_row(**row), cfg, sent=SENT, alert_id="alert-1", ai_content=content, validate_ai_content=False)


def _codes(findings: list[str]) -> list[str]:
    return [f.split(" ", 1)[0] for f in findings]


# --- the four facts ---------------------------------------------------------------------------


def test_a_draft_carrying_the_four_facts_passes(cfg):
    assert validate_content(_with_content(cfg, _draft(cfg))) == []


def test_the_next_update_token_is_the_one_the_email_renderer_prints(cfg):
    """One formatter for both, so a draft can never be told "14:02 EAT" by one and "14:17 EAT" by the other."""
    alert = _with_content(cfg, _draft(cfg))
    assert next_update_eat(alert).endswith(" EAT")
    assert next_update_eat(alert) in alert.content["en"].instruction


@pytest.mark.parametrize(
    "missing,code",
    [
        (INC, "content_missing_incident_number"),
        (PRIORITY, "content_missing_priority"),
        (REGION, "content_missing_region_label"),
    ],
)
def test_each_missing_fact_is_named(cfg, missing, code):
    good = _draft(cfg)
    draft = Content(
        headline=good.headline.replace(missing, "…"),
        body=good.body.replace(missing, "…"),
        instruction=good.instruction,
    )
    assert _codes(validate_content(_with_content(cfg, draft))) == [code]


def test_a_stale_next_update_time_is_refused(cfg):
    """The existing renderer fixture's shape: a plausible time that is not the envelope's."""
    draft = _draft(cfg).model_copy(update={"instruction": "Next update 14:02 EAT."})
    findings = validate_content(_with_content(cfg, draft))
    assert _codes(findings) == ["content_missing_next_update"]
    assert next_update_eat(_template(cfg)) in findings[0]


def test_a_missing_expiry_is_not_required(cfg):
    """No ``timing.expires`` means there is no next-update time to demand."""
    alert = _with_content(cfg, _draft(cfg))
    no_expiry = alert.model_copy(update={"timing": alert.timing.model_copy(update={"expires": None})})
    draft = no_expiry.content["en"].model_copy(update={"instruction": None})
    no_expiry = no_expiry.model_copy(update={"content": {"en": draft}})
    assert validate_content(no_expiry) == []


def test_english_is_mandatory(cfg):
    alert = _with_content(cfg, _draft(cfg))
    sw_only = alert.model_copy(update={"content": {"sw": alert.content["en"]}})  # bypasses the model rule
    assert "content_missing_en" in _codes(validate_content(sw_only))


def test_every_language_block_is_checked_and_labelled(cfg):
    sw = Content(headline="Kituo cha Embakasi", body="Hakuna umeme.")  # none of the four facts
    findings = validate_content(_with_content(cfg, _draft(cfg), sw=sw))
    assert findings and all("(sw)" in f for f in findings)
    assert "content_missing_incident_number" in _codes(findings)


# --- the "no invented cause" rule ------------------------------------------------------------


@pytest.mark.parametrize(
    "sentence",
    [
        "Service is down due to a fibre cut.",
        "Outage caused by vandalism at the site.",
        "POWER outage due to a fibre cut on the backhaul.",  # names POWER, but not as the cause
        "Loss of service DUE   TO a transmission fault.",  # any case, any whitespace
        "Traffic dropped, Caused By a radio fault; POWER is being checked.",  # clause ends at ';'
    ],
)
def test_a_cause_that_does_not_name_the_failure_domain_is_invented(cfg, sentence):
    draft = _draft(cfg, body=f"{INC} ({PRIORITY}, {REGION}): {sentence}")
    assert _codes(validate_content(_with_content(cfg, draft))) == ["content_invented_cause"]


@pytest.mark.parametrize(
    "sentence",
    [
        "Service is down due to a POWER failure at the HUB.",
        "Outage caused by loss of mains power.",  # word match, any case
        "The HUB is down. Next update due at 14:00.",  # "due at" is not "due to"
    ],
)
def test_a_cause_that_names_the_failure_domain_is_allowed(cfg, sentence):
    draft = _draft(cfg, body=f"{INC} ({PRIORITY}, {REGION}): {sentence}")
    assert validate_content(_with_content(cfg, draft)) == []


def test_the_rule_follows_the_incident_not_a_fixed_word(cfg):
    """A TRANSMISSION incident may say "due to a transmission fault"; the same words on a POWER
    incident are an invented cause."""
    body = f"{INC} ({PRIORITY}, {REGION}): degraded due to a transmission fault."
    tx = _with_content(cfg, _draft(cfg, body=body, failure_domain="TRANSMISSION"), failure_domain="TRANSMISSION")
    power = _with_content(cfg, _draft(cfg, body=body))
    assert validate_content(tx) == []
    assert _codes(validate_content(power)) == ["content_invented_cause"]


def test_an_unknown_domain_admits_only_an_unknown_cause(cfg):
    good = _draft(cfg, body=f"{INC} ({PRIORITY}, {REGION}): down due to an unknown fault.", failure_domain=None)
    bad = _draft(cfg, body=f"{INC} ({PRIORITY}, {REGION}): down due to a fibre cut.", failure_domain=None)
    assert validate_content(_with_content(cfg, good, failure_domain=None)) == []
    assert _codes(validate_content(_with_content(cfg, bad, failure_domain=None))) == ["content_invented_cause"]


def test_a_finding_never_quotes_the_draft(cfg):
    """Findings go to llm_calls.fallback_reason, a stored field (§9.5)."""
    draft = _draft(cfg, body=f"{INC} ({PRIORITY}, {REGION}): down due to a fibre cut near Mlolongo.")
    (finding,) = validate_content(_with_content(cfg, draft))
    assert "fibre" not in finding and "Mlolongo" not in finding and "POWER" in finding


# --- personal data -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra,code",
    [
        (" Call +254711000001 for access.", "content_personal_data_msisdn"),
        (" Contact noc.duty@example.com.", "content_personal_data_email"),
    ],
)
def test_contact_identifiers_are_refused(cfg, extra, code):
    draft = _draft(cfg, body=_draft(cfg).body + extra)
    assert code in _codes(validate_content(_with_content(cfg, draft)))


def test_a_person_assignee_named_in_the_draft_is_refused(cfg):
    row = dict(assignee_type="FIELD_ENGINEER", assignee_name="Wanjiru Kamau", msp_name=None)
    draft = _draft(cfg, body=_draft(cfg).body + " Wanjiru is on the way.", **row)
    assert "content_personal_data_name" in _codes(validate_content(_with_content(cfg, draft, **row)))


def test_an_msp_vendor_code_is_not_a_person(cfg):
    """``assignee_name`` holds the vendor code for an MSP assignment; ``Facts.msp_code`` says it is never a person."""
    draft = _draft(cfg, body=_draft(cfg).body + " EGYPRO is on site.")
    assert validate_content(_with_content(cfg, draft)) == []


# --- review E02: facts are whole tokens, and a second priority / incident is a wrong claim --------


def _vet(cfg, draft: Content, **row) -> tuple[object, list[str]]:
    """Through the real seam, with the row's own validation context."""
    reasons: list[str] = []
    alert = build_alert(
        _row(**row), cfg, sent=SENT, alert_id="alert-1", ai_content={"en": draft}, on_content_fallback=reasons.append
    )
    return alert, reasons


@pytest.mark.parametrize(
    "headline,row,codes",
    [
        # The reviewer's reproductions, each accepted before this change.
        ("P1 INC000123 Embakasi HUB down (was P2)", {}, ["content_conflicting_priority"]),
        ("P10 INC000123 Embakasi HUB down", {"priority": "P1"}, ["content_missing_priority"]),
        ("P2 INC0001234 Embakasi HUB down", {}, ["content_missing_incident_number", "content_conflicting_incident_number"]),
        ("P2P INC000123 Embakasi HUB down", {}, ["content_missing_priority"]),
        ("SFC-P20 INC000123 Embakasi HUB down", {}, ["content_missing_priority"]),
        ("P2 INC000123 Embakasi HUB down, also see INC000124", {}, ["content_conflicting_incident_number"]),
        ("P2 INC000123 Embakasi HUB down, p4 per MSP", {}, ["content_conflicting_priority"]),  # any case
    ],
)
def test_required_facts_are_whole_tokens_and_a_second_claim_is_refused(cfg, headline, row, codes):
    nu = next_update_eat(_template(cfg, **row))
    draft = Content(headline=headline, body=f"{REGION}: the HUB lost mains.", instruction=f"Next update {nu}.")
    findings = validate_content(_with_content(cfg, draft, **row))
    assert _codes(findings) == codes


def test_a_next_update_time_must_be_whole(cfg):
    good = _draft(cfg)
    stretched = good.model_copy(update={"instruction": good.instruction.replace(" EAT.", " EATX.")})
    assert _codes(validate_content(_with_content(cfg, stretched))) == ["content_missing_next_update"]


def test_a_wrong_priority_never_leaves_even_beside_the_right_one(cfg):
    draft = _draft(cfg, headline=f"[P1] {INC} Embakasi East HUB down, {REGION} (P2)")
    alert, reasons = _vet(cfg, draft)
    assert alert.governance.ai_assisted is False and reasons == ["content_invalid: content_conflicting_priority (en)"]


def test_the_severity_rationale_may_be_quoted_verbatim(cfg):
    """The engine's rationale names intermediate priorities by construction. Quoting it is not a
    second claim; the same tokens in the model's own words still are."""
    rationale = "operator=safaricom; users=3200→P4; site_type=HUB floor=P2; final=P2"
    quoting = _draft(cfg, body=f"{INC} ({PRIORITY}, {REGION}): Severity engine: {rationale}.")
    paraphrase = _draft(cfg, body=f"{INC} ({PRIORITY}, {REGION}): users alone would make this P4.")
    assert _vet(cfg, quoting, severity_rationale=rationale)[0].governance.ai_assisted is True
    assert _codes(validate_content(_with_content(cfg, quoting))) == ["content_conflicting_priority"]  # no quote allowance
    assert _vet(cfg, paraphrase, severity_rationale=rationale)[1] == ["content_invalid: content_conflicting_priority (en)"]


# --- review E03: every pseudonymised person, not only the assignee ----------------------------------


@pytest.mark.parametrize(
    "row,mention",
    [
        ({"fe_name": "Kevin Ochieng"}, "Kevin Ochieng on site."),
        ({"fe_name": "Kevin Ochieng"}, "Kevin is on site."),  # a part of the name, as the scrubber matches it
        ({"rnio_name": "Jane Wanjiru"}, "Jane Wanjiru informed."),
        ({"restored_by": "Peter Mwangi"}, "Restored by Peter Mwangi."),
    ],
)
def test_fe_rnio_and_restorer_names_are_refused(cfg, row, mention):
    alert, reasons = _vet(cfg, _draft(cfg, body=_draft(cfg).body + f" {mention}"), **row)
    assert alert.governance.ai_assisted is False
    assert reasons == ["content_invalid: content_personal_data_name (en)"]


def test_role_codes_and_role_words_are_not_names(cfg):
    """``FE-NBI-E-01`` / ``RNIO-NBI-E`` are the role tokens §6.1 puts on channels; ``MSP`` is what a
    restore by role writes. None is personal data, and a draft may name them."""
    row = dict(fe_name="FE-NBI-E-01", rnio_name="RNIO-NBI-E", restored_by="MSP")
    body = _draft(cfg).body + " FE-NBI-E-01 on site; RNIO-NBI-E informed; MSP confirmed restore."
    assert _vet(cfg, _draft(cfg, body=body), **row)[0].governance.ai_assisted is True


# --- review E04: a name part must never forbid a word the draft is required to use ------------------


def test_the_spec_example_desk_name_does_not_forbid_the_cause_the_rule_demands(cfg):
    """§6.7.1's facts: assignee "EGYPRO Power Desk", vendor EGYPRO, a POWER incident."""
    body = f"{INC} ({PRIORITY}, {REGION}): outage due to POWER; EGYPRO Power Desk engaged."
    alert, reasons = _vet(cfg, _draft(cfg, body=body), assignee_name="EGYPRO Power Desk")
    assert alert.governance.ai_assisted is True and reasons == []


def test_a_queue_named_after_the_region_does_not_forbid_the_region_label(cfg):
    """The HITL override path sets ``assignee_name`` alone; "Nairobi East Team" is a queue."""
    alert, reasons = _vet(cfg, _draft(cfg), assignee_name="Nairobi East Team", msp_name=None)
    assert alert.governance.ai_assisted is True and reasons == []


def test_a_person_whose_name_shares_a_required_word_is_still_caught_by_the_rest(cfg):
    row = dict(assignee_type="FIELD_ENGINEER", assignee_name="Kevin East", msp_name=None)
    assert _vet(cfg, _draft(cfg), **row)[0].governance.ai_assisted is True  # "Nairobi East" is the label
    named = _draft(cfg, body=_draft(cfg).body + " Kevin dispatched.")
    assert _vet(cfg, named, **row)[1] == ["content_invalid: content_personal_data_name (en)"]
    powered = dict(assignee_type="FIELD_ENGINEER", assignee_name="Faith Power", msp_name=None)
    cause = _draft(cfg, body=f"{INC} ({PRIORITY}, {REGION}): down due to a POWER failure.")
    assert _vet(cfg, cause, **powered)[0].governance.ai_assisted is True  # the failure domain is never a name part


def test_a_person_typed_over_an_msp_assignment_is_matched_whole(cfg):
    """``_apply_overrides`` can put a person's name on an MSP-type incident. It is refused whole;
    its parts are not (they could be desk words) — the documented cost of the MSP case."""
    row = dict(assignee_name="Kevin Otieno")  # assignee_type stays MSP
    whole = _draft(cfg, body=_draft(cfg).body + " Kevin Otieno engaged.")
    part = _draft(cfg, body=_draft(cfg).body + " Kevin engaged.")
    assert _vet(cfg, whole, **row)[1] == ["content_invalid: content_personal_data_name (en)"]
    assert _vet(cfg, part, **row)[0].governance.ai_assisted is True


# --- the fallback IS the template: run the validator over the real thing ----------------------------

HUB_EVENT = dict(  # the golden test's P2 HUB (HITL path)
    site_id="SFC-NBIE-HUB-EMB", site_name="Embakasi East Aggregation HUB", site_type="HUB", region_code="NBI_E",
    alarm_code="POWER_GRID_FAIL", failure_domain="POWER", users_affected=450000, access_notes="Genset not started",
)
BTS_EVENT = dict(  # the golden test's P4 BTS (auto-send path)
    site_id="SFC-MTK-BTS-MCH04", site_name="Machakos Town BTS", site_type="BTS", region_code="MTK",
    alarm_code="SITE_DOWN", failure_domain="POWER", users_affected=3200,
)
SMALL_HUB_EVENT = dict(  # users alone say P4, the HUB floor says P2: the rationale names both
    site_id="SFC-NBIE-HUB-EMB", site_name="Embakasi East Aggregation HUB", site_type="HUB", region_code="NBI_E",
    alarm_code="POWER_GRID_FAIL", failure_domain="POWER", users_affected=3200,
)
#: What today's v1 wording cannot carry in ``content``, by design (§6.2 fidelity rule, D3): the
#: incident number is in the email subject and the SMS line, not the narrative, and there is no
#: "Next update" line until ``site_down_alert@2``. The email renderer reports the same two facts
#: as flag-off warnings. Everything ELSE — every rule about what content may SAY — must pass.
V1_TEMPLATE_GAPS = {"content_missing_incident_number", "content_missing_next_update"}


@pytest.mark.parametrize("event", [HUB_EVENT, BTS_EVENT, SMALL_HUB_EVENT], ids=["golden-hub-p2", "golden-bts-p4", "hub-floor-p2"])
def test_the_real_template_trips_no_content_rule(tmp_db, cfg, event):
    settings, session = tmp_db
    inc = process_event(session, settings, EventIngest(**event))
    session.refresh(inc)
    template = build_alert(inc, settings.operator)
    findings = validate_content(template, **content_validation_context(inc))
    assert set(_codes(findings)) <= V1_TEMPLATE_GAPS, findings
    if event is SMALL_HUB_EVENT:  # the case the rationale allowance exists for
        assert "→P4" in template.content["en"].body and inc.priority == "P2"
        bare = validate_content(template, **{**content_validation_context(inc), "quoted": ()})
        assert "content_conflicting_priority" in _codes(bare)


# --- the fallback reason -----------------------------------------------------------------------


def test_the_fallback_reason_is_codes_only_and_deduplicated():
    findings = [
        "content_invented_cause (en): states a cause …",
        "content_missing_region_label (en): does not carry …",
        "content_invented_cause (en): states a cause …",
        "content_missing_region_label (sw): does not carry …",
    ]
    assert content_fallback_reason(findings) == (
        "content_invalid: content_invented_cause (en), content_missing_region_label (en), content_missing_region_label (sw)"
    )
    assert content_fallback_reason([]) == FALLBACK_REASON_CONTENT_INVALID == "content_invalid"


# --- the seam: build_alert(..., ai_content=...) --------------------------------------------------


def test_without_ai_content_the_envelope_is_byte_identical_and_the_validator_is_never_called(cfg, monkeypatch):
    before = build_alert(_row(), cfg, sent=SENT, alert_id="alert-1").model_dump_json()
    monkeypatch.setattr(alerts, "validate_content", lambda *_a, **_k: pytest.fail("validator consulted on the template path"))
    calls: list[str] = []
    for enforce in (None, True, False):
        after = build_alert(
            _row(), cfg, sent=SENT, alert_id="alert-1", validate_ai_content=enforce, on_content_fallback=calls.append
        )
        assert after.model_dump_json() == before
    assert calls == []


def test_a_clean_draft_is_used_and_flagged_ai_assisted(cfg):
    reasons: list[str] = []
    draft = _draft(cfg)
    alert = build_alert(
        _row(), cfg, sent=SENT, alert_id="alert-1", ai_content={"en": draft}, validate_ai_content=True,
        on_content_fallback=reasons.append,
    )
    assert alert.content == {"en": draft} and alert.governance.ai_assisted is True
    assert reasons == []


def test_an_invented_cause_falls_back_to_the_template_exactly(cfg):
    reasons: list[str] = []
    draft = _draft(cfg, body=f"{INC} ({PRIORITY}, {REGION}): down due to a fibre cut.")
    alert = build_alert(
        _row(), cfg, sent=SENT, alert_id="alert-1", ai_content={"en": draft}, validate_ai_content=True,
        on_content_fallback=reasons.append,
    )
    assert alert.content == {"en": default_content(_row())}
    assert alert.governance.ai_assisted is False
    assert reasons == ["content_invalid: content_invented_cause (en)"]
    # Field for field the envelope a no-AI build produces: nothing of the draft survives.
    assert alert.model_dump_json() == _template(cfg).model_dump_json()
    email = render_email(alert)
    assert "fibre" not in email.body and "AI assistance" not in email.body


def test_the_fallback_reason_lands_in_llm_calls(tmp_db, cfg):
    """The seam writes nothing itself; its callback is how the caller fills ``fallback_reason``."""
    _settings, session = tmp_db

    def record(reason: str) -> None:
        rec = LlmCallRecord(model_requested="claude-test", fallback_used=True)
        record_llm_call(
            session, operator_id="safaricom", agent="BroadcastCommsAgent", purpose="alert_content",
            provider="none", rec=rec, audit_id="audit-1", incident_id="inc-0001", fallback_reason=reason,
        )

    draft = _draft(  # no region label anywhere, and an invented cause
        cfg, headline=f"[{PRIORITY}] {INC} Embakasi East HUB down", body=f"{INC} ({PRIORITY}): the HUB is down due to a fibre cut."
    )
    build_alert(_row(), cfg, sent=SENT, ai_content={"en": draft}, validate_ai_content=True, on_content_fallback=record)
    session.commit()
    (row,) = session.scalars(select(LlmCallRow)).all()
    assert row.fallback_reason == "content_invalid: content_missing_region_label (en), content_invented_cause (en)"
    assert row.fallback_used == 1


def test_a_refusal_with_nobody_to_record_it_is_logged(cfg, caplog):
    draft = _draft(cfg, body=f"{INC} ({PRIORITY}, {REGION}): down due to a fibre cut.")
    with caplog.at_level(logging.WARNING, logger="noc_agents.services.alerts"):
        alert = build_alert(_row(), cfg, sent=SENT, ai_content={"en": draft}, validate_ai_content=True)
    assert alert.governance.ai_assisted is False
    assert "content_invented_cause" in caplog.text and "fibre" not in caplog.text


def test_the_seam_is_on_as_shipped():
    assert alerts.VALIDATE_AI_CONTENT is True


# The drafts the renderer and envelope tests used before this seam validated: plausible-looking,
# and missing the incident number, the region label and the envelope's real next-update time
# (the P4 case also claims "P2"). Kept verbatim here as the negative case they now are.
LEGACY_RENDERER_DRAFT = Content(headline="P2 Embakasi HUB down - mains failure", body="Model-drafted body.", instruction="Next update 14:02 EAT.")
LEGACY_ENVELOPE_DRAFT = Content(headline="P2 Embakasi HUB down - mains failure", body="Body.", instruction=None)


@pytest.mark.parametrize(
    "draft,row,expected",
    [
        (
            LEGACY_RENDERER_DRAFT,
            {},
            "content_invalid: content_missing_incident_number (en), content_missing_region_label (en), "
            "content_missing_next_update (en)",
        ),
        (
            LEGACY_RENDERER_DRAFT,
            {"priority": "P4"},  # and it says "P2" on a P4 incident: a conflicting priority too
            "content_invalid: content_missing_incident_number (en), content_missing_priority (en), "
            "content_missing_region_label (en), content_missing_next_update (en), content_conflicting_priority (en)",
        ),
        (
            LEGACY_ENVELOPE_DRAFT,
            {},
            "content_invalid: content_missing_incident_number (en), content_missing_region_label (en), "
            "content_missing_next_update (en)",
        ),
    ],
)
def test_a_non_compliant_draft_falls_back_by_default(cfg, draft, row, expected):
    """No override argument: the shipped default refuses the draft, keeps the template, clears
    ``ai_assisted`` and hands over the reason — and nothing of the draft reaches a renderer."""
    reasons: list[str] = []
    alert = build_alert(_row(**row), cfg, sent=SENT, alert_id="alert-1", ai_content={"en": draft}, on_content_fallback=reasons.append)
    assert alert.content == {"en": default_content(_row(**row))}
    assert alert.governance.ai_assisted is False
    assert reasons == [expected]
    assert alert.model_dump_json() == _template(cfg, **row).model_dump_json()
    assert "Model-drafted" not in render_email(alert).body and "AI assistance" not in render_email(alert).body


def test_one_bad_language_block_refuses_the_whole_draft(cfg):
    """A compliant English block does not carry a non-compliant Kiswahili one through."""
    reasons: list[str] = []
    alert = build_alert(
        _row(), cfg, sent=SENT, ai_content={"en": _draft(cfg), "sw": Content(headline="Kituo", body="Mwili.")},
        on_content_fallback=reasons.append,
    )
    assert set(alert.content) == {"en"} and alert.governance.ai_assisted is False
    assert reasons and "(sw)" in reasons[0] and "(en)" not in reasons[0]


def test_the_module_switch_is_the_default_for_every_call(cfg, monkeypatch):
    """``VALIDATE_AI_CONTENT`` is read at call time, so flipping it enforces every caller at once."""
    draft = _draft(cfg, body=f"{INC} ({PRIORITY}, {REGION}): down due to a fibre cut.")
    monkeypatch.setattr(alerts, "VALIDATE_AI_CONTENT", True)
    assert build_alert(_row(), cfg, sent=SENT, ai_content={"en": draft}).governance.ai_assisted is False
    monkeypatch.setattr(alerts, "VALIDATE_AI_CONTENT", False)
    assert build_alert(_row(), cfg, sent=SENT, ai_content={"en": draft}).governance.ai_assisted is True


def test_a_draft_with_no_english_block_is_still_a_caller_error(cfg):
    """Shape, not content: ``NocAlertContent`` makes ``en`` required, so a dict without it is a bug
    in the caller and raises, as before — it is not something to paper over with the template."""
    with pytest.raises(ValidationError, match='content\\["en"\\] is mandatory'):
        build_alert(
            _row(), cfg, sent=SENT, ai_content={"sw": Content(headline="Kituo", body="Mwili.")}, validate_ai_content=True
        )


def test_validators_stays_importable_without_the_envelope_models():
    """``validate_content`` is duck-typed; the model import is for annotations only."""
    assert "NocAlert" not in vars(validators)
