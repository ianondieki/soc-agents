"""Channel validators: a violation suppresses the payload, it never "sends anyway" (§6.2).

The personal-data checks are the shared DPA 2019 scrubber from ``llm/redaction.py`` —
these tests pin the reuse (the same MSISDN spellings that never leave the box for a model
must never leave it on an SMS either), not a second pattern set.
"""

from __future__ import annotations

from types import SimpleNamespace

from noc_agents.services.composition import compose_sms
from noc_agents.services.gsm7 import gsm7_length, sms_cost, to_gsm7
from noc_agents.services.validators import (
    EMAIL_BODY_MAX_BYTES,
    EMAIL_RECIPIENTS_PER_MESSAGE,
    EMAIL_SUBJECT_MAX,
    WHATSAPP_BODY_MAX,
    contains_personal_data,
    validate_email,
    validate_inapp,
    validate_sms,
    validate_whatsapp,
)

GOOD_SMS = (
    "[P1] INC000123 SFC-NBI-001 NBI_E\n"
    "POWER|est.users 450000\n"
    "Mains failure, generator not starting\n"
    "Owner:RNIO-NBI-E - ticket notes for updates"
)


def _incident(**over):
    base = dict(
        priority="P1",
        incident_number="INC000123",
        site_id="SFC-NBI-001",
        region_code="NBI_E",
        failure_domain="POWER",
        users_affected=450000,
        title="Mains failure, generator not starting at Nairobi East hub",
        assignee_name="Grace Wanjiru",
    )
    base.update(over)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------- SMS: happy


def test_a_clean_single_segment_alert_passes():
    result = validate_sms(GOOD_SMS, incident_number="INC000123", priority="P1")
    assert result.ok
    assert result.suppress_reason is None
    assert result.findings == ()
    assert result.cost.encoding == "GSM7"
    assert result.cost.segments == 1
    assert result.contains_personal_data is False


# ------------------------------------------------------- SMS: the MSISDN rejection


def test_the_validator_rejects_a_payload_containing_an_msisdn():
    body = "[P1] INC000123 SFC-NBI-001 power down, call FE on 0712345678"
    result = validate_sms(body, incident_number="INC000123", priority="P1")
    assert not result.ok
    assert result.suppress_reason == "personal_data_msisdn"
    assert result.contains_personal_data is True
    # The finding must not carry the number itself — a suppression reason ends up in the
    # outbox row, the audit log and the HITL card.
    (violation,) = [f for f in result.findings if f.code == "personal_data_msisdn"]
    assert violation.fatal
    assert "0712345678" not in violation.message
    assert "0712345678" not in str(violation.detail)


def test_every_msisdn_spelling_in_the_spec_is_caught():
    for number in (
        "+254712345678",       # §6.2's \+?254\d{9}
        "254712345678",
        "0712345678",          # §6.2's 0[17]\d{8}
        "0112345678",
        "+254 712 345 678",    # how a NOC engineer actually types it
        "0712-345-678",
        "+254 (0)733 123456",
    ):
        result = validate_sms(f"[P1] INC000123 call {number} now", incident_number="INC000123")
        assert result.suppress_reason == "personal_data_msisdn", number


def test_ordinary_noc_numbers_are_not_mistaken_for_msisdns():
    # Incident numbers, EAT clock times, site ids and subscriber counts all live in these
    # bodies; a false positive would suppress a real alert.
    for body in (
        GOOD_SMS,
        "[P4] INC000999 RFT-CEL-0417 restored 14:05 EAT, est.users 1,200",
        "[P2] INC000456 TX ring 2026-01-07 11:02 EAT, 3 child sites down",
    ):
        assert not contains_personal_data(body), body
        assert validate_sms(body, incident_number=body[5:14]).ok, body


# -------------------------------------------------------- SMS: the other §6.2 rules


def test_a_missing_incident_number_or_priority_token_suppresses():
    result = validate_sms("Site down, engineer dispatched", incident_number="INC000123", priority="P1")
    assert result.codes() == ("sms_missing_incident_number", "sms_missing_priority")
    assert result.suppress_reason == "sms_missing_incident_number"


def test_an_at_sign_suppresses_because_an_address_has_leaked_into_an_sms():
    result = validate_sms("[P1] INC000123 escalate to noc.lead@example.com", incident_number="INC000123")
    assert not result.ok
    assert "sms_contains_at_sign" in result.codes()
    assert "personal_data_email" in result.codes()


def test_the_segment_budget_is_153_per_part_not_160():
    body = "[P1] INC000123 " + "A" * 200
    one_segment = validate_sms(body, max_segments=1)
    assert one_segment.suppress_reason == "sms_too_many_segments"
    assert one_segment.cost.segments == 2
    assert validate_sms(body, max_segments=2).ok

    # 306 septets is exactly two parts; 307 is three, and max_segments=2 refuses it.
    assert validate_sms("A" * 306, max_segments=2).ok
    assert validate_sms("A" * 307, max_segments=2).suppress_reason == "sms_too_many_segments"


def test_an_extension_character_can_tip_a_body_over_the_one_segment_budget():
    body = "A" * 159 + "€"
    assert len(body) == 160                     # looks like it fits
    assert gsm7_length(body) == 161             # it does not
    assert validate_sms(body, max_segments=1).suppress_reason == "sms_too_many_segments"


# ------------------------------------------- SMS: the encoding flag (fidelity rule)


def test_a_ucs2_body_is_reported_but_still_sent_while_the_envelope_flag_is_off():
    body = "Owner:Grace — ticket notes"     # em dash -> UCS-2
    result = validate_sms(body, enforce_encoding=False)
    assert result.ok                             # §6.2: must NOT suppress today's messages
    assert result.suppress_reason is None
    assert [f.code for f in result.warnings] == ["sms_not_gsm7"]
    assert result.cost.encoding == "UCS2"
    assert result.cost.per_segment == 70
    assert result.warnings[0].detail["fixable"] is True


def test_the_same_body_is_suppressed_once_the_flag_is_on():
    body = "Owner:Grace — ticket notes"
    result = validate_sms(body, enforce_encoding=True)
    assert result.suppress_reason == "sms_not_gsm7"
    assert "U+2014" in str(result.violations[0].detail)


def test_enforce_encoding_defaults_to_the_alert_envelope_v2_environment_flag(monkeypatch):
    body = "Owner:Grace — ticket notes"
    monkeypatch.delenv("ALERT_ENVELOPE_V2", raising=False)
    assert validate_sms(body).ok                              # absent -> false -> report only
    monkeypatch.setenv("ALERT_ENVELOPE_V2", "false")
    assert validate_sms(body).ok
    monkeypatch.setenv("ALERT_ENVELOPE_V2", "true")
    assert validate_sms(body).suppress_reason == "sms_not_gsm7"


def test_the_live_sms_is_now_gsm7_and_fits_one_segment():
    """This test used to assert the opposite, and the change is the point.

    The live SMS carried an em dash in THREE independent places -- the incident title
    (agents/ticket.py), the "Owner: ... ticket notes" tail (services/composition.py and
    services/render/sms.py), and the seeded @1 template. Any one of them forced the whole
    message out of GSM-7 (160 chars/segment) into UCS-2 (70), making the standard
    site-down alert 3 segments instead of 1: triple the cost per recipient, and multipart
    SMS can arrive out of order.

    The first two were fixed on the owner's explicit approval, because they alter bytes
    that reach customers. The @1 template deliberately still carries its em dash -- it is
    the recorded history of what actually went out, not a bug to tidy.
    """
    body = compose_sms(_incident())
    assert "—" not in body, "an em dash came back on the live SMS path"
    cost = sms_cost(body)
    assert cost.encoding == "GSM7"
    result = validate_sms(body, incident_number="INC000123", priority="P1", max_segments=4)
    assert result.ok and not [f.code for f in result.warnings if f.code == "sms_not_gsm7"]

    # How much the fix actually saves depends on LENGTH, and it is worth being exact rather
    # than quoting a single flattering number. GSM-7 packs 153 septets per concatenated part
    # against UCS-2's 67, so the same body drops from 3 parts to 1 or 2:
    ucs2_equivalent = sms_cost(body.replace("-", "—", 1))
    assert ucs2_equivalent.encoding == "UCS2"
    assert cost.segments < ucs2_equivalent.segments, "the GSM-7 fix must reduce the part count"

    # This incident is a LONG one (163 septets) and still needs 2 parts. A short one fits in
    # 1. The residual risk is real and named in docs/TEMPLATE_V2_REVIEW.md: the 26-character
    # "- ticket notes for updates" tail leaves only a few characters of headroom, so a long
    # site or assignee name tips the message into a second part. Shortening that tail is a
    # wording decision for the operator, not an encoding fix.
    assert cost.segments <= 2


# ----------------------------------------------------------------- SMS: names


def test_a_forbidden_name_suppresses_while_a_disclosed_one_is_only_recorded():
    body = compose_sms(_incident()).replace("—", "-")
    assert "Grace Wanjiru" in body

    silent = validate_sms(body, max_segments=4)
    assert silent.ok and silent.contains_personal_data is False   # no names supplied, no check

    disclosed = validate_sms(body, max_segments=4, disclosed_names=["Grace Wanjiru"])
    assert disclosed.ok                                            # governance signal, not a block
    assert disclosed.contains_personal_data is True
    assert [f.code for f in disclosed.warnings] == ["personal_data_disclosed_name"]

    forbidden = validate_sms(body, max_segments=4, forbidden_names=["Grace Wanjiru"])
    assert forbidden.suppress_reason == "personal_data_name"


def test_a_first_name_alone_is_caught_because_the_shared_scrubber_matches_parts():
    body = "[P1] INC000123 Wanjiru took over the site visit"
    assert validate_sms(body, forbidden_names=["Grace Wanjiru"]).suppress_reason == "personal_data_name"
    # ...and the scrubber's word-boundary rule still applies, so ordinary words survive.
    assert validate_sms("[P1] INC000123 power restored", forbidden_names=["Grace Wanjiru"]).ok


# -------------------------------------------------------------------------- EMAIL


def test_a_clean_email_passes_every_required_field_check():
    subject = "[P1] INC000123 | Nairobi East Hub (HUB) | Nairobi East | Safaricom"
    body = (
        "Priority: P1\nIncident: INC000123\nRegion: Nairobi East\n"
        "Next update: 11:02 EAT\nSummary: mains failure.\n"
    )
    result = validate_email(
        subject,
        body,
        incident_number="INC000123",
        priority="P1",
        region_label="Nairobi East",
        next_update="11:02 EAT",
        recipients=["a@example.com"] * EMAIL_RECIPIENTS_PER_MESSAGE,
    )
    assert result.ok and result.findings == ()


def test_email_limits_and_missing_facts_are_all_reported():
    result = validate_email(
        "S" * (EMAIL_SUBJECT_MAX + 1),
        "<p>Site down</p>" + "x" * EMAIL_BODY_MAX_BYTES,
        incident_number="INC000123",
        priority="P1",
        region_label="Nairobi East",
        next_update="11:02 EAT",
        recipients=["a@example.com"] * (EMAIL_RECIPIENTS_PER_MESSAGE + 1),
    )
    assert set(result.codes()) == {
        "email_subject_too_long",
        "email_body_too_large",
        "email_contains_html",
        "email_missing_incident_number",
        "email_missing_priority",
        "email_missing_region_label",
        "email_missing_next_update",
        "email_too_many_recipients",
    }
    assert result.suppress_reason == "email_subject_too_long"


def test_a_less_than_sign_in_prose_is_not_html():
    body = "Priority: P1\nIncident: INC000123\nRestored in <5 min, no HTML here.\n"
    result = validate_email("[P1] INC000123", body, incident_number="INC000123", priority="P1")
    assert result.ok


def test_an_msisdn_in_an_email_body_suppresses_it_too():
    body = "Priority: P1\nIncident: INC000123\nFE reachable on +254712345678.\n"
    result = validate_email("[P1] INC000123", body, incident_number="INC000123", priority="P1")
    assert result.suppress_reason == "personal_data_msisdn"


def test_a_staff_email_address_in_an_email_body_is_a_warning_not_a_block():
    body = "Priority: P1\nIncident: INC000123\nReply to noc.lead@example.com.\n"
    result = validate_email("[P1] INC000123", body, incident_number="INC000123", priority="P1")
    assert result.ok
    assert [f.code for f in result.warnings] == ["personal_data_email"]
    assert result.contains_personal_data is True


# ----------------------------------------------------------------------- WHATSAPP


def test_whatsapp_component_limits_follow_the_meta_template_rules():
    result = validate_whatsapp(
        "x" * (WHATSAPP_BODY_MAX + 1),
        header="h" * 61,
        footer="f" * 61,
        buttons=["ok", "b" * 26],
        params={"incident_number": "INC000123", "region": ""},
        required_params=["incident_number", "region", "next_update"],
    )
    assert set(result.codes()) == {
        "whatsapp_header_too_long",
        "whatsapp_body_too_long",
        "whatsapp_footer_too_long",
        "whatsapp_button_too_long",
        "whatsapp_missing_param",
    }
    missing = [f for f in result.findings if f.code == "whatsapp_missing_param"][0]
    assert missing.detail["missing"] == ["next_update", "region"]   # blank counts as missing


def test_a_valid_whatsapp_template_render_passes():
    result = validate_whatsapp(
        "INC000123 (P1): mains failure at Nairobi East Hub. Next update 11:02 EAT.",
        header="Safaricom NOC alert",
        footer="Do not reply",
        buttons=["View ticket"],
        params={"incident_number": "INC000123", "region": "Nairobi East"},
        required_params=["incident_number", "region"],
    )
    assert result.ok


# -------------------------------------------------------------------------- IN-APP


def test_inapp_requires_json_and_the_pinned_keys():
    ok = validate_inapp(
        {"incident_number": "INC000123", "priority": "P1", "region_code": "NBI_E"},
        required_keys=["incident_number", "priority", "region_code"],
    )
    assert ok.ok

    missing = validate_inapp({"incident_number": "INC000123"}, required_keys=["incident_number", "priority"])
    assert missing.suppress_reason == "inapp_missing_key"
    assert missing.findings[0].detail["missing"] == ["priority"]

    unserialisable = validate_inapp({"when": SimpleNamespace(x=1)})
    assert unserialisable.suppress_reason == "inapp_not_json_serialisable"


def test_personal_data_is_found_inside_a_nested_inapp_payload():
    result = validate_inapp({"incident": {"notes": ["call the FE on 0722000111"]}})
    assert result.suppress_reason == "personal_data_msisdn"
    assert result.contains_personal_data is True
