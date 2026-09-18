"""Nothing personal leaves the box: names → tokens, e-mails/MSISDNs scrubbed, allowlist only."""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

from noc_agents.llm.redaction import (
    ALLOWLIST,
    PSEUDONYMISED,
    SCRUBBED_TEXT,
    NameMap,
    redact_incident,
    restore_names,
    scrub_text,
)

FE = "James Mwangi"
RNIO = "Grace Wanjiru"
AUTHOR = "Peter Otieno"


def _incident():
    return SimpleNamespace(
        incident_number="INC000123",
        priority="P2",
        status="ASSIGNED",
        site_id="SFC-NBI-001",
        site_type="HUB",
        site_class="HUB",
        region_code="NBI_E",
        county="Nairobi",
        failure_domain="POWER",
        alarm_code="MAINS_FAIL",
        tt_category="POWER",
        tt_category_label="Power failure",
        technology="4G",
        users_affected=450000,
        child_sites_down=6,
        mpesa_risk=True,
        is_hub_major=True,
        created_at=datetime(2026, 9, 16, 8, 0, 0),
        outage_start_at=datetime(2026, 9, 16, 7, 55, 0),
        sla_ack_due=None,
        sla_restore_due=None,
        restored_at=None,
        recurrence_count=1,
        msp_name="EGYPRO",
        responsible_msp="EGYPRO",
        radio_oem="HUAWEI",
        vendor_tt_ref=None,
        msp_percent_complete=None,
        assignee_name=FE,
        fe_name=FE,
        rnio_name=RNIO,
        title="MAINS_FAIL at Westlands Hub",
        site_name="Westlands Hub",
        access_notes=f"Call {FE} on 0712345678 or +254733123456, gate key with guard@site.co.ke",
        description="Genset not started",
        narrative=f"Escalated to {RNIO} (grace.wanjiru@example.com).",
        resolution_summary=None,
        msp_root_cause=None,
        msp_action_taken=None,
        root_cause_hypothesis="Mains failure; genset did not start",
        impact_summary="450,000 subscribers",
        # never-sent fields
        correlation_fingerprint="secret-fp",
        operator_id="safaricom",
    )


def _notes():
    return [
        SimpleNamespace(author=AUTHOR, author_role="msp", body=f"{AUTHOR} on site, call 0722000111", created_at=datetime(2026, 9, 16, 8, 30)),
        SimpleNamespace(author=FE, author_role="field_engineer", body="Genset fuel low", created_at=datetime(2026, 9, 16, 9, 0)),
    ]


def test_scrub_text_masks_emails_phones_and_names():
    names = NameMap()
    token = names.token_for(FE)
    out = scrub_text(f"Ask {FE} (james.mwangi@saf.co.ke, +254712345678 / 0112345678) — not 254999 or 12345678901", names)
    assert FE not in out and "james.mwangi@saf.co.ke" not in out
    assert "+254712345678" not in out and "0112345678" not in out
    assert out.count("<PHONE>") == 2 and "<EMAIL>" in out and token in out
    assert "254999" in out and "12345678901" in out  # not MSISDN shapes; untouched
    assert scrub_text(None, names) is None


def test_redact_incident_sends_allowlist_only_and_tokenises_people():
    payload, mapping = redact_incident(_incident(), _notes())
    expected_keys = set(ALLOWLIST) | set(PSEUDONYMISED) | set(SCRUBBED_TEXT) | {"notes"}
    assert set(payload) == expected_keys
    assert "correlation_fingerprint" not in payload and "operator_id" not in payload

    serialised = json.dumps(payload, default=str)
    for secret in (FE, RNIO, AUTHOR, "0712345678", "+254733123456", "0722000111", "guard@site.co.ke", "grace.wanjiru@example.com"):
        assert secret not in serialised, secret
    assert payload["assignee_name"] == payload["fe_name"]  # same person → same token
    assert payload["assignee_name"] != payload["rnio_name"]
    assert payload["msp_name"] == "EGYPRO"  # company name is network data
    assert payload["created_at"] == "2026-09-16T08:00:00"
    assert "<PHONE>" in payload["access_notes"] and "<EMAIL>" in payload["access_notes"]

    # mapping round-trips every token back to the real name
    assert set(mapping.values()) == {FE, RNIO, AUTHOR}
    assert restore_names(payload["narrative"], mapping) == _incident().narrative.replace("grace.wanjiru@example.com", "<EMAIL>")


def test_notes_are_newest_first_limited_and_scrubbed():
    payload, mapping = redact_incident(_incident(), _notes(), notes_limit=1)
    assert len(payload["notes"]) == 1
    note = payload["notes"][0]
    assert note["body"] == "Genset fuel low" and note["author_role"] == "field_engineer"
    assert note["author"] == payload["fe_name"]  # FE authored the newest note → same token
    assert set(note) == {"author", "author_role", "created_at", "body"}
    payload_all, _ = redact_incident(_incident(), _notes())
    assert "0722000111" not in json.dumps(payload_all)


def test_restore_names_is_local_and_idempotent_for_unknown_tokens():
    mapping = {"<PERSON_1>": FE}
    assert restore_names("Owner <PERSON_1>; <PERSON_9> unknown", mapping) == f"Owner {FE}; <PERSON_9> unknown"


def test_scrub_text_masks_separated_msisdns():
    names = NameMap()
    text = "Call +254 712 345 678, 0712-345-678, 0722 000 111, +254-733-123456 or 254.110.234.567; ref 0800 is not a number"
    out = scrub_text(text, names)
    for raw in ("+254 712 345 678", "0712-345-678", "0722 000 111", "+254-733-123456", "254.110.234.567"):
        assert raw not in out, raw
    assert out.count("<PHONE>") == 5
    assert "ref 0800 is not a number" in out


def test_short_names_replace_whole_words_only():
    names = NameMap()
    atc, ann = names.token_for("ATC"), names.token_for("Ann")
    out = scrub_text("ATC dispatched a batch of technicians; Ann saw the Announcement. Team dispatched at 14:00", names)
    assert out.startswith(f"{atc} dispatched a batch")
    assert f"{ann} saw the Announcement." in out
    assert "dispatched at 14:00" in out and "<PERSON_" not in out.replace(atc, "").replace(ann, "")


def test_notes_without_created_at_sort_without_error():
    notes = _notes() + [SimpleNamespace(author=AUTHOR, author_role="msp", body="undated", created_at=None)]
    payload, _ = redact_incident(_incident(), notes)
    assert [n["body"] for n in payload["notes"]] == ["Genset fuel low", "<PERSON_3> on site, call <PHONE>", "undated"]


def test_first_name_or_surname_alone_is_scrubbed():
    names = NameMap()
    james, grace, kevin = names.token_for(FE), names.token_for(RNIO), names.token_for("Kevin Ochieng")
    out = scrub_text("Kevin took over from Grace at 14:00. Call James on 0712345678; Mwangi confirmed, Ochieng informed.", names)
    for leak in ("Kevin", "Grace", "James", "Mwangi", "Ochieng"):
        assert leak not in out, leak
    assert out == f"{kevin} took over from {grace} at 14:00. Call {james} on <PHONE>; {james} confirmed, {kevin} informed."
    assert restore_names(out, names.token_to_name).startswith("Kevin Ochieng took over from Grace Wanjiru")


def test_name_parts_shorter_than_four_letters_are_not_aliased():
    names = NameMap()
    token = names.token_for("Jo Ann Li")
    out = scrub_text("Jo Ann Li is on site; Jo said Li Ann is fine; Joanna too", names)
    assert out == f"{token} is on site; Jo said Li Ann is fine; Joanna too"


def test_scrub_text_masks_bracketed_trunk_and_local_msisdns():
    names = NameMap()
    for raw in ("+254 (0)733 123456", "+254(0)712345678", "254 (0) 712 345 678", "(0722) 000111", "+254 0712 345 678"):
        assert scrub_text(f"call {raw} now", names) == "call <PHONE> now", raw


def test_typed_allowlist_strings_are_scrubbed_too():
    """vendor_tt_ref / msp_name are typed by vendors and analysts: a phone or e-mail there must not leave the box."""
    inc = _incident()
    inc.vendor_tt_ref = "EGY-TT-991 (Mwangi 0712345678 mwangi@egypro.co.ke)"
    inc.msp_name = "EGYPRO (call 0722000111)"
    inc.assignee_name = "EGYPRO"  # the assignee is often the MSP company itself
    payload, mapping = redact_incident(inc, _notes())
    fe_token = payload["fe_name"]
    assert payload["vendor_tt_ref"] == f"EGY-TT-991 ({fe_token} <PHONE> <EMAIL>)"
    assert payload["msp_name"] == "EGYPRO (call <PHONE>)"  # company field: contacts scrubbed, company name kept
    assert payload["responsible_msp"] == "EGYPRO" and payload["assignee_name"].startswith("<PERSON_")
    serialised = json.dumps(payload, default=str)
    for secret in ("0712345678", "mwangi@egypro.co.ke", "0722000111", "Mwangi"):
        assert secret not in serialised, secret
    # non-string allowlist values are untouched
    assert payload["users_affected"] == 450000 and payload["mpesa_risk"] is True and payload["created_at"] == "2026-09-16T08:00:00"


def test_role_code_names_are_matched_whole_so_the_noc_word_rnio_survives():
    inc = _incident()
    inc.rnio_name = "RNIO-NBI-E"
    inc.narrative = "RNIO briefed; RNIO-NBI-E to confirm. FE-MTK-01 en route."
    inc.fe_name = "FE-MTK-01"
    inc.assignee_name = "FE-MTK-01"
    payload, mapping = redact_incident(inc, [])
    rnio_token, fe_token = payload["rnio_name"], payload["fe_name"]
    assert payload["narrative"] == f"RNIO briefed; {rnio_token} to confirm. {fe_token} en route."
    assert mapping[rnio_token] == "RNIO-NBI-E" and mapping[fe_token] == "FE-MTK-01"
    names = NameMap()
    names.token_for("RNIO-NBI-E")
    assert scrub_text("The RNIO is aware; rnio call pending", names) == "The RNIO is aware; rnio call pending"
