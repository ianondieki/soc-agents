"""Per-channel renderers (``services/render/*``, spec §6.2) over the NocAlert envelope.

The headline is ``test_v1_fidelity_*``: with ``ALERT_ENVELOPE_V2`` off (the default, and the
state of every test here unless it says otherwise) ``render_sms`` / ``render_email`` on the
envelope of a real pipeline incident — the same HUB and BTS events the golden test drives —
reproduce ``services/composition.py:compose_sms`` / ``compose_email`` **byte for byte**, SHA-256
and all, and equal what the pipeline persisted for the approver and the drafts. If a byte
differs the renderer is wrong; the expectation is never adjusted.

The rest pins what the renderers add on top of the string: measured GSM-7/UCS-2 encoding and
segment counts, the segment gate, the flag-off "report, do not suppress" rule and its flag-on
opposite, the AI-disclosure footer, the §6.7 in-app key set, the §6.7 ledger row, the
idempotency key formula, the language fallback and the WhatsApp gate.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from noc_agents.config import get_settings
from noc_agents.db.models import BroadcastRow, HitlTaskRow, IncidentRow
from noc_agents.domain.alerts import AudienceSpec, Content, Rendering, SmsRendering, WhatsAppRendering
from noc_agents.domain.schemas import EventIngest
from noc_agents.graph.pipeline import process_event
from noc_agents.orchestrator.outbox import _TRANSMITTERS, _dump_envelope
from noc_agents.domain.alerts import NocAlert
from noc_agents.realtime.hub import hub
from noc_agents.services.alerts import EXTERNAL_AUDIENCES, V1_TEMPLATE_KEY, build_alert
from noc_agents.services.composition import compose_email, compose_sms
from noc_agents.services.gsm7 import sms_cost
from noc_agents.services.ledger import ledger_row_cells
from noc_agents.services.render import (
    AI_REVIEWER_PENDING,
    ChannelPayload,
    idempotency_key,
    render_alert,
    render_email,
    render_for_audience,
    render_inapp,
    render_ledger_cells,
    render_ledger_row,
    render_sms,
    render_whatsapp,
)
from noc_agents.services.render.email import email_text
from noc_agents.services.render.inapp import INAPP_KEYS, inapp_card
from noc_agents.services.render.ledger import LEDGER_KEYS
from noc_agents.services.render.whatsapp import WHATSAPP_FLAG

# The two events the golden test drives (same literals, kept local: the golden file may not be touched).
HUB_EVENT = dict(  # P2 under L2_GUARDED: HITL path
    site_id="SFC-NBIE-HUB-EMB",
    site_name="Embakasi East Aggregation HUB",
    site_type="HUB",
    region_code="NBI_E",
    alarm_code="POWER_GRID_FAIL",
    failure_domain="POWER",
    users_affected=450000,
    access_notes="Genset not started",
)
BTS_EVENT = dict(  # P4 under L2_GUARDED: auto-send path
    site_id="SFC-MTK-BTS-MCH04",
    site_name="Machakos Town BTS",
    site_type="BTS",
    region_code="MTK",
    alarm_code="SITE_DOWN",
    failure_domain="POWER",
    users_affected=3200,
)
UTC = timezone.utc
SENT = datetime(2026, 9, 16, 10, 47, 10)  # naive UTC = 13:47:10 EAT, the §6.7.1 instant
FLAG = "ALERT_ENVELOPE_V2"


@pytest.fixture()
def clean_hub():
    hub._history.clear()
    yield hub
    hub._history.clear()


@pytest.fixture()
def cfg():
    return get_settings().operator


@pytest.fixture(autouse=True)
def _flags_off(monkeypatch):
    """Every test starts from the shipped defaults; a test that wants a flag on says so."""
    monkeypatch.delenv(FLAG, raising=False)
    monkeypatch.delenv(WHATSAPP_FLAG, raising=False)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _assert_bytes_equal(rendered: str, expected: str, what: str) -> None:
    got, want = rendered.encode("utf-8"), expected.encode("utf-8")
    assert got == want, f"{what} differs\n--- render ---\n{rendered!r}\n--- composition.py ---\n{expected!r}"
    assert _sha(rendered) == _sha(expected)


def _row(**overrides) -> IncidentRow:
    """A fully populated, unflushed IncidentRow (column defaults apply at flush only)."""
    base = dict(
        id="inc-0001",
        operator_id="safaricom",
        incident_number="INC000123",
        status="ASSIGNED",
        priority="P2",
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


def _alert(cfg, **kw) -> NocAlert:
    return build_alert(_row(**kw.pop("row", {})), cfg, sent=SENT, alert_id="alert-1", **kw)


def _with_sms(alert: NocAlert, **sms) -> NocAlert:
    base = alert.rendering.sms.model_dump()
    base.update(sms)
    return alert.model_copy(update={"rendering": Rendering(sms=SmsRendering(**base), email=alert.rendering.email)})


# --------------------------------------------------------------------------------------
# v1 fidelity: the renderers reproduce today's wording byte for byte
# --------------------------------------------------------------------------------------


def test_v1_fidelity_hitl_path_p2_hub(tmp_db, clean_hub):
    settings, session = tmp_db
    inc = process_event(session, settings, EventIngest(**HUB_EVENT))
    session.refresh(inc)
    assert inc.priority == "P2" and inc.requires_hitl is True

    alert = build_alert(inc, settings.operator)
    sms, email = render_sms(alert), render_email(alert)

    _assert_bytes_equal(sms.body, compose_sms(inc), "SMS")
    _assert_bytes_equal(email_text(email), compose_email(inc, settings.operator), "email")
    assert email.subject == alert.rendering.email.subject
    assert compose_email(inc, settings.operator) == f"Subject: {email.subject}\n\n{email.body}"

    # The flag is off: today's wording is reported on, never suppressed.
    assert sms.status == "OK" and sms.suppress_reason is None
    assert email.status == "OK" and email.suppress_reason is None

    # ...and equals what the pipeline persisted for the approver and the drafts.
    task = session.scalar(select(HitlTaskRow).where(HitlTaskRow.incident_id == inc.id))
    assert task is not None
    _assert_bytes_equal(sms.body, task.proposed_payload["sms"], "HITL proposed SMS")
    _assert_bytes_equal(email_text(email), task.proposed_payload["email"], "HITL proposed email")
    drafts = session.scalars(select(BroadcastRow).where(BroadcastRow.incident_id == inc.id)).all()
    assert drafts and {d.status for d in drafts} == {"PENDING_HITL"}
    for draft in drafts:
        _assert_bytes_equal({"SMS": sms.body, "EMAIL": email_text(email)}[draft.channel], draft.message, f"{draft.channel} draft")


def test_v1_fidelity_auto_path_p4_bts(tmp_db, clean_hub):
    settings, session = tmp_db
    inc = process_event(session, settings, EventIngest(**BTS_EVENT))
    session.refresh(inc)
    assert inc.priority == "P4" and inc.requires_hitl is False

    alert = build_alert(inc, settings.operator)
    sms, email = render_sms(alert), render_email(alert)
    _assert_bytes_equal(sms.body, compose_sms(inc), "SMS")
    _assert_bytes_equal(email_text(email), compose_email(inc, settings.operator), "email")
    assert sms.status == "OK" and email.status == "OK"

    rows = session.scalars(select(BroadcastRow).where(BroadcastRow.incident_id == inc.id)).all()
    assert rows and {r.status for r in rows} <= {"QUEUED", "SENT"}
    for row in rows:
        _assert_bytes_equal({"SMS": sms.body, "EMAIL": email_text(email)}[row.channel], row.message, f"{row.channel} row")


def test_v1_fidelity_for_every_audience_and_after_the_json_round_trip(tmp_db, clean_hub):
    """Every audience gets the same bytes (v1 has one wording), from the live envelope or its stored JSON."""
    settings, session = tmp_db
    inc = process_event(session, settings, EventIngest(**HUB_EVENT))
    session.refresh(inc)
    alert = build_alert(inc, settings.operator, sent=SENT, alert_id="alert-1")
    back = NocAlert.model_validate_json(_dump_envelope(alert))

    payloads = render_alert(back)
    assert [(p.channel, p.audience) for p in payloads] == [
        (ch, aud.audience) for aud in alert.audiences for ch in aud.channels
    ]
    assert len(payloads) == 8  # p2_audiences: RNIO, FE, MSP, MANAGEMENT × [EMAIL, SMS]
    for p in payloads:
        assert p.status == "OK", (p.channel, p.audience, p.suppress_reason)
        assert p.template_key == V1_TEMPLATE_KEY and p.template_version == "1"
        if p.channel == "SMS":
            _assert_bytes_equal(p.body, compose_sms(inc), f"SMS for {p.audience}")
        else:
            _assert_bytes_equal(email_text(p), compose_email(inc, settings.operator), f"email for {p.audience}")
    assert len({p.idempotency_key for p in payloads}) == 8  # distinct per (channel, audience)


# --------------------------------------------------------------------------------------
# SMS: measured, gated, reported
# --------------------------------------------------------------------------------------


def test_sms_reports_measured_encoding_and_segments(cfg):
    """v1's em dash forces UCS-2 (67 chars/part); the numbers on the payload are gsm7.sms_cost's, not len()."""
    sms = render_sms(_alert(cfg))
    cost = sms_cost(sms.body)
    assert sms.encoding == cost.encoding == "UCS2"
    assert sms.segments == cost.segments == 3 and len(sms.body) == 170  # not "2 segments of 70"
    assert sms.status == "OK"
    assert any("sms_not_gsm7" in w and "EM DASH" in w for w in sms.warnings)  # reported, with the offender named


def test_sms_over_max_segments_is_suppressed_not_sent_anyway(cfg):
    alert = _with_sms(_alert(cfg), max_segments=1)  # today's max is 6; force the gate
    sms = render_sms(alert)
    assert sms.status == "SUPPRESSED" and sms.suppress_reason == "sms_too_many_segments"
    assert sms.segments == 3 and sms.encoding == "UCS2"  # the approver still sees the numbers
    assert sms.body == compose_sms(_row())  # and the text that was refused
    assert render_sms(_with_sms(alert, max_segments=3)).status == "OK"


def test_sms_becomes_fatal_when_the_flag_is_on(cfg, monkeypatch):
    """§6.2: flipping ALERT_ENVELOPE_V2 turns the reported em dash into a suppression (fail closed)."""
    monkeypatch.setenv(FLAG, "true")
    sms = render_sms(_alert(cfg))
    assert sms.status == "SUPPRESSED" and sms.suppress_reason == "sms_not_gsm7"
    assert sms.body == compose_sms(_row())  # the bytes never change; only the verdict does


def test_sms_declared_gsm7_but_measured_ucs2_is_reported(cfg):
    sms = render_sms(_with_sms(_alert(cfg), encoding="GSM7"))
    assert sms.status == "OK" and any("sms_encoding_mismatch" in w for w in sms.warnings)


def test_sms_carries_incident_number_and_priority(cfg):
    sms = render_sms(_alert(cfg))
    assert "INC000123" in sms.body and "P2" in sms.body and "@" not in sms.body


# --------------------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------------------


def test_email_subject_and_body_split_like_the_adapter(cfg):
    email = render_email(_alert(cfg))
    assert email.subject == "[P2] INC000123 | Embakasi East Aggregation HUB (HUB) | Nairobi East | Safaricom PLC (demo profile)"
    assert email.body.startswith("Service affecting: YES\n") and email.body.endswith("wait for next brief.\n")
    assert email.status == "OK" and email.encoding is None and email.segments is None
    assert email.provider_params == {"list_unsubscribe": False}  # internal audience: no List-Unsubscribe


def test_email_v1_tokens_in_the_subject_are_reported_not_fatal(cfg):
    """Today's email carries INC and priority in the subject only and has no next-update line."""
    email = render_email(_alert(cfg))
    codes = [w for w in email.warnings if "email_missing_" in w]
    assert any("email_missing_incident_number" in w and "subject does" in w for w in codes)
    assert any("email_missing_next_update" in w for w in codes)
    assert email.status == "OK"


def test_email_becomes_fatal_when_the_flag_is_on(cfg, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    email = render_email(_alert(cfg))
    assert email.status == "SUPPRESSED" and email.suppress_reason == "email_missing_incident_number"
    assert email_text(email) == compose_email(_row(), cfg)  # bytes unchanged; verdict changed


def test_email_subject_over_200_is_suppressed(cfg):
    alert = _alert(cfg)
    long_subject = alert.rendering.email.model_copy(update={"subject": "S" * 201})
    email = render_email(alert.model_copy(update={"rendering": Rendering(sms=alert.rendering.sms, email=long_subject)}))
    assert email.status == "SUPPRESSED" and email.suppress_reason == "email_subject_too_long"


def test_email_external_audience_gets_the_list_unsubscribe_hint(cfg):
    spec = AudienceSpec(audience="REGULATOR", channels=["EMAIL"], recipients_ref="audiences.REGULATOR")
    email = render_email(_alert(cfg, audiences=[spec]))
    assert email.audience == "REGULATOR" and email.provider_params == {"list_unsubscribe": True}


# --------------------------------------------------------------------------------------
# AI disclosure footer (§6.2)
# --------------------------------------------------------------------------------------

# §6.1-compliant drafts (validate_content runs at the build_alert seam): each carries the incident
# number, its own priority, the region label and the envelope's next-update time for its row.
AI_EN = Content(
    headline="P2 INC000123 Embakasi HUB down - mains failure",
    body="Model-drafted body. Region: Nairobi East.",
    instruction="Next update 14:17 EAT.",
)
AI_EN_P4 = Content(  # the P4 row's next update is later: P4's note interval
    headline="P4 INC000123 Embakasi HUB down - mains failure",
    body="Model-drafted body. Region: Nairobi East.",
    instruction="Next update 15:47 EAT.",
)


def test_no_footer_without_ai_assistance(cfg):
    assert "AI assistance" not in render_email(_alert(cfg)).body
    assert "AI assistance" not in render_sms(_alert(cfg)).body


def test_ai_footer_on_email_names_the_approver_or_says_pending(cfg):
    pending = render_email(_alert(cfg, ai_content={"en": AI_EN}))  # P2 → HITL → no approver yet
    assert pending.body.endswith(f"\nDrafted with AI assistance; reviewed by {AI_REVIEWER_PENDING}.\n")
    assert any("ai_disclosure_pending" in w for w in pending.warnings)

    auto = render_email(_alert(cfg, row=dict(priority="P4"), ai_content={"en": AI_EN_P4}))  # policy approver
    assert auto.body.endswith("\nDrafted with AI assistance; reviewed by policy:L2_GUARDED.\n")
    assert not any("ai_disclosure_pending" in w for w in auto.warnings)


@pytest.mark.parametrize("audience", sorted(EXTERNAL_AUDIENCES))
def test_ai_footer_on_sms_for_external_audiences_only(cfg, audience):
    external = AudienceSpec(audience=audience, channels=["SMS"], recipients_ref=f"audiences.{audience}")
    internal = AudienceSpec(audience="RNIO", channels=["SMS"], recipients_ref="regions.NBI_E.rnio")
    alert = _alert(cfg, audiences=[external, internal], ai_content={"en": AI_EN})
    ext, intl = render_sms(alert, external), render_sms(alert, internal)
    assert ext.body.endswith(f"\nDrafted with AI assistance; reviewed by {AI_REVIEWER_PENDING}.")
    assert "AI assistance" not in intl.body  # a footer costs a segment; §6.2's SMS row does not ask for it
    assert ext.segments >= intl.segments


def test_ai_footer_that_overflows_the_segment_budget_is_refused(cfg):
    external = AudienceSpec(audience="REGULATOR", channels=["SMS"], recipients_ref="audiences.REGULATOR")
    alert = _alert(cfg, audiences=[external], ai_content={"en": AI_EN})
    with_footer = render_sms(alert)  # v1 max_segments=6: fits
    assert with_footer.status == "OK" and "AI assistance" in with_footer.body
    budget = with_footer.segments - 1  # one part fewer than the footered body needs
    refused = render_sms(_with_sms(alert, max_segments=budget))
    assert refused.status == "SUPPRESSED" and refused.suppress_reason == "sms_too_many_segments"
    assert "AI assistance" in refused.body  # the disclosure is never dropped to make room


# --------------------------------------------------------------------------------------
# In-app card (§6.7)
# --------------------------------------------------------------------------------------

P1_AUDIENCES = [
    AudienceSpec(audience="RNIO", channels=["SMS", "WHATSAPP"], recipients_ref="regions.NBI_E.rnio"),
    AudienceSpec(audience="FE", channels=["SMS"], recipients_ref="regions.NBI_E.fe_oncall"),
    AudienceSpec(audience="MSP", channels=["EMAIL", "SMS"], recipients_ref="msp_contacts.EGYPRO"),
    AudienceSpec(audience="MANAGEMENT", channels=["EMAIL", "INAPP"], recipients_ref="audiences.MANAGEMENT"),
]


def test_inapp_card_has_exactly_the_pinned_key_set(cfg):
    alert = _alert(cfg, row=dict(priority="P1"), audiences=P1_AUDIENCES)
    card = inapp_card(alert)
    assert tuple(card) == INAPP_KEYS
    assert set(card) == {"incident_number", "alert_id", "priority", "lifecycle", "headline", "region_code", "next_update_at", "channels", "requires_hitl"}
    assert card["channels"] == ["SMS", "WHATSAPP", "EMAIL", "INAPP"]  # first-appearance order, §6.7.1
    assert card["requires_hitl"] is True and card["priority"] == "P1" and card["lifecycle"] == "INVESTIGATING"
    assert card["next_update_at"] == "2026-09-16T11:02:10Z"  # P1 in NBI_E: 15 min after SENT, spelled as the envelope JSON
    assert card["headline"] == alert.content["en"].headline and card["alert_id"] == "alert-1"


def test_inapp_payload_is_the_serialised_card(cfg):
    alert = _alert(cfg, row=dict(priority="P4"), audiences=[
        AudienceSpec(audience="FE", channels=["SMS"], recipients_ref="regions.NBI_E.fe_oncall"),
        AudienceSpec(audience="NOC_SHIFT", channels=["INAPP"], recipients_ref="audiences.NOC_SHIFT"),
    ])
    payload = render_inapp(alert)
    assert payload.channel == "INAPP" and payload.audience == "NOC_SHIFT" and payload.status == "OK"
    card = json.loads(payload.body)
    assert card == inapp_card(alert)
    assert card["channels"] == ["SMS", "INAPP"] and card["requires_hitl"] is False  # §6.7.2


def test_inapp_is_not_rendered_when_no_audience_lists_it(cfg):
    with pytest.raises(LookupError, match="INAPP"):
        render_inapp(_alert(cfg))  # v1 audiences: EMAIL + SMS only


# --------------------------------------------------------------------------------------
# Ledger row (§6.7)
# --------------------------------------------------------------------------------------


def test_ledger_row_matches_the_spec_shape_from_one_clock_read(cfg):
    row = render_ledger_row(_alert(cfg), cfg)
    assert tuple(row) == LEDGER_KEYS and all(v is not None for v in row.values())
    assert row["shift_id"] == "2026-09-16_DAY" and re.fullmatch(r"\d{4}-\d{2}-\d{2}_(DAY|NIGHT)", row["shift_id"])
    assert row["opened_at_eat"] == "13:47"  # SENT is 10:47:10Z; the date and the shift come from the same instant
    assert row["note"] == "HITL pending" and row["status"] == "INVESTIGATING"
    assert row["users_affected"] == 450000 and row["assignee_name"] == "EGYPRO" and row["site_id"] == "SFC-NBIE-HUB-EMB"


def test_ledger_row_note_and_night_shift(cfg):
    auto = render_ledger_row(_alert(cfg, row=dict(priority="P4")), cfg)
    assert auto["note"] == "auto-sent per policy"
    night = build_alert(_row(), cfg, sent=datetime(2026, 9, 16, 20, 30, 0))  # 23:30 EAT
    assert render_ledger_row(night, cfg)["shift_id"] == "2026-09-16_NIGHT"
    late = build_alert(_row(), cfg, sent=datetime(2026, 9, 16, 22, 30, 0))  # 01:30 EAT next day
    assert render_ledger_row(late, cfg)["shift_id"] == "2026-09-17_NIGHT"
    unnamed = render_ledger_row(_alert(cfg, row=dict(assignee_name=None)), cfg)
    assert unnamed["assignee_name"] == ""  # non-null columns, §6.2


def test_ledger_cells_come_from_services_ledger(cfg):
    """Same layout as the LEDGER node's xlsx row; only the columns the envelope lacks differ."""
    inc = _row()
    file_name, cells = render_ledger_cells(build_alert(inc, cfg, sent=SENT), cfg, "day")
    want_name, want = ledger_row_cells(inc, cfg, "day")
    assert file_name == want_name and len(cells) == len(want) == 20
    same = [1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 18, 19]  # INC, priority, site, name, type, region, TT, domain, users, MSP, M-PESA, shift
    assert [cells[i] for i in same] == [want[i] for i in same]
    assert cells[12] == cells[13] == "" and want[12] == "FE-NBI-E-01"  # persons stay out of the envelope
    assert cells[16] == "INVESTIGATING" and want[16] == "ASSIGNED"  # lifecycle, not incident status


# --------------------------------------------------------------------------------------
# WhatsApp: present, gated, draft-only
# --------------------------------------------------------------------------------------

WA = WhatsAppRendering(
    template_name="site_down_alert_v1",
    language_code="en",
    params={"priority": "P1", "incident_number": "INC000123", "site_name": "Westlands Hub", "region_label": "Nairobi East",
            "failure_domain": "POWER", "users_affected": "620,000", "next_update_eat": "14:02"},
)


def _wa_alert(cfg, rendering: WhatsAppRendering | None = WA) -> NocAlert:
    alert = _alert(cfg, row=dict(priority="P1"), audiences=P1_AUDIENCES)
    return alert.model_copy(update={"rendering": Rendering(sms=alert.rendering.sms, email=alert.rendering.email, whatsapp=rendering)})


def test_whatsapp_is_unreachable_while_the_flag_is_off(cfg):
    wa = render_whatsapp(_wa_alert(cfg))
    assert wa.status == "SUPPRESSED" and wa.suppress_reason == "whatsapp_disabled"
    assert wa.body == "" and wa.provider_params == {}  # nothing is even built


@pytest.mark.parametrize("raw", ["true", "1", "YES"])
def test_whatsapp_draft_is_built_but_still_suppressed_with_the_flag_on(cfg, monkeypatch, raw):
    monkeypatch.setenv(WHATSAPP_FLAG, raw)
    wa = render_whatsapp(_wa_alert(cfg))
    assert wa.audience == "RNIO" and wa.template_key == "site_down_alert_v1"
    assert wa.status == "SUPPRESSED" and wa.suppress_reason == "no_approved_template"  # no registry can confirm APPROVED
    tpl = wa.provider_params["template"]
    assert wa.provider_params["messaging_product"] == "whatsapp" and wa.provider_params["type"] == "template"
    assert "to" not in wa.provider_params  # recipients are resolved in the dispatcher
    assert tpl["name"] == "site_down_alert_v1" and tpl["language"] == {"code": "en"}
    assert tpl["components"] == [{"type": "body", "parameters": [
        {"type": "text", "parameter_name": k, "text": v} for k, v in WA.params.items()
    ]}]
    assert "site_down_alert_v1 [en] priority=P1" in wa.body
    assert render_whatsapp(_wa_alert(cfg, None)).suppress_reason == "no_approved_template"


def test_whatsapp_blank_named_param_is_flagged(cfg, monkeypatch):
    monkeypatch.setenv(WHATSAPP_FLAG, "true")
    blank = WA.model_copy(update={"params": {**WA.params, "site_name": ""}})
    assert render_whatsapp(_wa_alert(cfg, blank)).suppress_reason == "whatsapp_missing_param"


def test_nothing_can_transmit_whatsapp_today():
    assert "WHATSAPP" not in _TRANSMITTERS


# --------------------------------------------------------------------------------------
# Payload plumbing: idempotency, language, templates, audiences
# --------------------------------------------------------------------------------------


def test_idempotency_key_is_the_spec_formula(cfg):
    alert = _alert(cfg)
    sms = render_sms(alert)
    raw = f"{alert.idempotency_seed}|SMS|RNIO|regions.NBI_E.rnio|site_down_alert@1"
    assert sms.idempotency_key == hashlib.sha256(raw.encode()).hexdigest()[:40]
    assert sms.idempotency_key == idempotency_key(alert, "SMS", "RNIO", "regions.NBI_E.rnio", "site_down_alert", "1")
    assert len(sms.idempotency_key) == 40 and sms.idempotency_key != render_email(alert).idempotency_key
    assert render_sms(alert).idempotency_key == sms.idempotency_key  # deterministic


def test_kiswahili_request_falls_back_to_english_and_says_so(cfg):
    sw = AudienceSpec(audience="RNIO", channels=["SMS", "EMAIL"], language="sw", recipients_ref="regions.NBI_E.rnio")
    kituo = Content(headline="Kituo INC000123 P2 Nairobi East", body="Mwili. Taarifa ijayo 14:17 EAT.")  # §6.1-compliant
    alert = _alert(cfg, audiences=[sw], ai_content={"en": AI_EN, "sw": kituo})
    assert alert.content["sw"] == kituo  # the Kiswahili really is in the envelope; the renderer must withhold it
    for payload in render_for_audience(alert, sw):
        assert payload.language == "sw" and payload.language_fallback == "en" and payload.rendered_language == "en"
        assert any("language_fallback" in w for w in payload.warnings)
        assert "Kituo" not in payload.body  # unreviewed Kiswahili never leaves (§6.4)


def test_unknown_template_version_is_suppressed_not_guessed(cfg):
    alert = _alert(cfg)
    v2 = alert.model_copy(update={"governance": alert.governance.model_copy(update={"template_version": "2"})})
    for payload in (render_sms(v2), render_email(v2)):
        assert payload.status == "SUPPRESSED" and payload.suppress_reason == "no_template" and payload.body == ""
        assert payload.template_version == "2"


def test_explicit_audience_must_list_the_channel(cfg):
    alert = _alert(cfg)
    email_only = AudienceSpec(audience="MANAGEMENT", channels=["EMAIL"], recipients_ref="audiences.MANAGEMENT")
    with pytest.raises(LookupError, match="does not list channel SMS"):
        render_sms(alert, email_only)


def test_payload_model_forbids_extras_and_needs_no_addresses(cfg):
    payload = render_sms(_alert(cfg))
    assert payload.recipient_ref == "regions.NBI_E.rnio" and "@" not in payload.recipient_ref
    with pytest.raises(Exception):
        ChannelPayload(**{**payload.model_dump(), "to": "+254700000000"})


def test_no_optional_extra_is_imported_at_module_level():
    """Importing noc_agents.services.render must not pull in an optional extra.

    Checked in a fresh interpreter on purpose. Once ``anthropic`` is installed, other tests in
    the same session import it deliberately (the LLM client and port tests), so ``sys.modules``
    of THIS process says nothing about what importing the module does; the old in-process
    assertion only ever passed on a machine without the extra.
    """
    code = (
        "import sys; import noc_agents.services.render; "
        "assert 'noc_agents.services.render' in sys.modules; "
        "bad = sorted(m for m in sys.modules if m in ('anthropic', 'mcp') or m.startswith(('anthropic.', 'mcp.'))); "
        "assert not bad, bad"
    )
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "NOC_SKIP_DOTENV": "1", "PYTHONPATH": str(root / "src")}
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=root)
    assert proc.returncode == 0, proc.stderr
