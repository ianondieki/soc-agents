"""NocAlert v1 envelope (spec §6.1) and ``services/alerts.build_alert``.

The headline is the **v1 fidelity proof**: a real incident goes through ``process_event``
(the same HUB and BTS events the golden test uses), ``build_alert`` composes the envelope,
and a renderer that reads ONLY the envelope must reproduce today's ``compose_sms`` /
``compose_email`` output byte for byte — and the exact strings the pipeline persisted in
``HitlTaskRow.proposed_payload`` and ``BroadcastRow.message``. ``ALERT_ENVELOPE_V2`` stays
off throughout; nothing here touches the run, so the golden literals cannot move.

The remaining tests pin the §6.1 mapping table (severity, urgency, lifecycle, timing,
governance), the audience/recipient derivation, the read-only guarantee, the JSON round
trip through ``outbox.envelope_json`` (aware UTC, ``Z`` suffix) and the model's own rules
(``extra="forbid"``, ``content["en"]`` mandatory, the incident-number pattern).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from noc_agents.config import get_settings
from noc_agents.db.models import BroadcastRow, HitlTaskRow, IncidentRow
from noc_agents.domain.alerts import (
    Area,
    AudienceSpec,
    Content,
    Facts,
    IncidentRef,
    NocAlert,
    NocAlertContent,
)
from noc_agents.domain.schemas import EventIngest
from noc_agents.graph.pipeline import process_event
from noc_agents.orchestrator.outbox import _dump_envelope
from noc_agents.realtime.hub import hub
from noc_agents.services import alerts
from noc_agents.services.alerts import (
    V1_INSTRUCTION,
    V1_TEMPLATE_KEY,
    alert_envelope_v2_enabled,
    build_alert,
    default_audiences,
    lifecycle_for,
    note_interval_minutes,
    severity_for,
    urgency_for,
    v1_audience_name,
)
from noc_agents.services.composition import compose_email, compose_sms
from noc_agents.services.worklog_monitor import _note_interval_minutes

# The two events the golden test drives (same literals, kept local so this file stays
# independent of tests/integration/test_golden_sequence.py, which may not be touched).
HUB_EVENT = dict(  # P2 under L2_GUARDED: HITL path, drafts PENDING_HITL
    site_id="SFC-NBIE-HUB-EMB",
    site_name="Embakasi East Aggregation HUB",
    site_type="HUB",
    region_code="NBI_E",
    alarm_code="POWER_GRID_FAIL",
    failure_domain="POWER",
    users_affected=450000,
    access_notes="Genset not started",
)
BTS_EVENT = dict(  # P4 under L2_GUARDED: auto-send path, drafts QUEUED then SENT by the drain
    site_id="SFC-MTK-BTS-MCH04",
    site_name="Machakos Town BTS",
    site_type="BTS",
    region_code="MTK",
    alarm_code="SITE_DOWN",
    failure_domain="POWER",
    users_affected=3200,
)
UTC = timezone.utc
SENT = datetime(2026, 9, 16, 10, 47, 10)  # naive UTC, the storage contract


@pytest.fixture()
def clean_hub():
    hub._history.clear()
    yield hub
    hub._history.clear()


@pytest.fixture()
def cfg():
    return get_settings().operator


# --------------------------------------------------------------------------------------
# The v1 renderer: reads the ENVELOPE ONLY. It is deliberately written against the
# envelope's field tree, not against the IncidentRow, so a passing byte comparison proves
# the envelope carries everything today's wording needs.
# --------------------------------------------------------------------------------------


def render_v1_sms(alert: NocAlert) -> str:
    c, f, a, ref = alert.classification, alert.facts, alert.area, alert.incident
    return (
        f"[{c.priority}] {ref.incident_number} {a.site_id} {a.region_code}\n"
        f"{f.failure_domain}|est.users {f.users_affected}\n"
        f"{alert.content['en'].headline[:80]}\n"
        f"Owner:{f.assignee_name} - ticket notes for updates"
    )


def render_v1_email(alert: NocAlert) -> str:
    en, f = alert.content["en"], alert.facts
    assert alert.rendering.email is not None
    return (
        f"Subject: {alert.rendering.email.subject}\n\n"
        f"Service affecting: {'YES' if f.service_affecting else 'NO'}\n"
        f"Est. users: {f.users_affected}\n"
        f"Services: {', '.join(f.services_impacted)}\n"
        f"Failure domain: {f.failure_domain}\n"
        f"M-PESA corridor risk: {'YES' if f.mpesa_risk else 'NO'}\n\n"
        f"Summary: {en.headline}\n\n"
        f"Narrative:\n{en.body}\n\n"
        f"Owner: {f.assignee_name}\n"
        f"Hypothesis: {f.root_cause_hypothesis}\n\n"
        f"{en.instruction}\n"
    )


def _assert_bytes_equal(rendered: str, expected: str, what: str) -> None:
    got, want = rendered.encode("utf-8"), expected.encode("utf-8")
    assert got == want, f"{what} differs\n--- envelope render ---\n{rendered!r}\n--- composition.py ---\n{expected!r}"


def _row(**overrides) -> IncidentRow:
    """A fully populated, unflushed IncidentRow (SQLAlchemy column defaults apply at flush only)."""
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
        narrative="Service-affecting event detected at Embakasi East Aggregation HUB (HUB).",
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


# --------------------------------------------------------------------------------------
# v1 fidelity: the envelope reproduces today's wording byte for byte
# --------------------------------------------------------------------------------------


def test_v1_fidelity_hitl_path_p2_hub(tmp_db, clean_hub):
    settings, session = tmp_db
    inc = process_event(session, settings, EventIngest(**HUB_EVENT))
    session.refresh(inc)
    assert inc.priority == "P2" and inc.requires_hitl is True  # the golden HITL path

    alert = build_alert(inc, settings.operator)

    sms, email = render_v1_sms(alert), render_v1_email(alert)
    _assert_bytes_equal(sms, compose_sms(inc), "SMS")
    _assert_bytes_equal(email, compose_email(inc, settings.operator), "email")

    # ...and what the pipeline actually persisted for the approver and the drafts.
    task = session.scalar(select(HitlTaskRow).where(HitlTaskRow.incident_id == inc.id))
    assert task is not None
    _assert_bytes_equal(sms, task.proposed_payload["sms"], "HITL proposed SMS")
    _assert_bytes_equal(email, task.proposed_payload["email"], "HITL proposed email")
    assert [v1_audience_name(a.audience) for a in alert.audiences] == task.proposed_payload["audiences"]

    drafts = session.scalars(select(BroadcastRow).where(BroadcastRow.incident_id == inc.id)).all()
    assert drafts and {d.status for d in drafts} == {"PENDING_HITL"}
    for draft in drafts:
        _assert_bytes_equal({"SMS": sms, "EMAIL": email}[draft.channel], draft.message, f"{draft.channel} draft")
    assert sorted({d.audience for d in drafts}) == sorted(v1_audience_name(a.audience) for a in alert.audiences)

    # Governance agrees with the gate that ran.
    assert alert.governance.requires_hitl is True
    assert alert.governance.approved_by is None and alert.governance.approved_at is None
    assert alert.governance.contains_personal_data is True  # fe_name / rnio_name / access_notes are set
    assert alert.classification.priority == "P2" and alert.classification.severity == "SEVERE"


def test_v1_fidelity_auto_path_p4_bts(tmp_db, clean_hub):
    settings, session = tmp_db
    inc = process_event(session, settings, EventIngest(**BTS_EVENT))
    session.refresh(inc)
    assert inc.priority == "P4" and inc.requires_hitl is False  # the golden auto-broadcast path

    alert = build_alert(inc, settings.operator)

    sms, email = render_v1_sms(alert), render_v1_email(alert)
    _assert_bytes_equal(sms, compose_sms(inc), "SMS")
    _assert_bytes_equal(email, compose_email(inc, settings.operator), "email")

    rows = session.scalars(select(BroadcastRow).where(BroadcastRow.incident_id == inc.id)).all()
    assert rows and {r.status for r in rows} <= {"QUEUED", "SENT"}  # written by BROADCAST, flipped by the drain
    for row in rows:
        _assert_bytes_equal({"SMS": sms, "EMAIL": email}[row.channel], row.message, f"{row.channel} row")
    assert sorted({r.audience for r in rows}) == sorted(v1_audience_name(a.audience) for a in alert.audiences)
    assert [a.audience for a in alert.audiences] == ["RNIO", "FE"]  # p4_audiences: [RNIO, FIELD_ENGINEER]

    assert alert.governance.requires_hitl is False
    assert alert.governance.approved_by == "policy:L2_GUARDED"  # what the BROADCAST node stamps on its outbox rows
    assert alert.governance.approved_at == alert.sent
    assert alert.classification.severity == "MINOR"


def test_v1_fidelity_survives_the_outbox_json_round_trip(tmp_db, clean_hub):
    """``outbox.envelope_json`` stores ``_dump_envelope(alert)``; reading it back must render identically."""
    settings, session = tmp_db
    inc = process_event(session, settings, EventIngest(**HUB_EVENT))
    session.refresh(inc)
    alert = build_alert(inc, settings.operator, sent=SENT, alert_id="alert-1")

    stored = _dump_envelope(alert)
    assert stored is not None
    back = NocAlert.model_validate_json(stored)
    assert back == alert
    _assert_bytes_equal(render_v1_sms(back), compose_sms(inc), "SMS after round trip")
    _assert_bytes_equal(render_v1_email(back), compose_email(inc, settings.operator), "email after round trip")

    dumped = json.loads(stored)
    assert dumped["sent"] == "2026-09-16T10:47:10Z"  # aware UTC on the wire, never a naive string
    assert dumped["timing"]["effective"].endswith("Z") and dumped["timing"]["expires"].endswith("Z")
    assert dumped["schema_version"] == 1


def test_v1_content_caps_hold_for_pipeline_incidents(tmp_db, clean_hub):
    """Content caps are §6.1's; fidelity relies on real titles/narratives fitting inside them."""
    settings, session = tmp_db
    for event in (HUB_EVENT, BTS_EVENT):
        inc = process_event(session, settings, EventIngest(**event))
        session.refresh(inc)
        assert len(inc.title) <= 160 and len(inc.narrative) <= 2000
        en = build_alert(inc, settings.operator).content["en"]
        assert en.headline == inc.title and en.body == inc.narrative and en.instruction == V1_INSTRUCTION


def test_build_alert_is_read_only_on_the_incident(tmp_db, clean_hub):
    settings, session = tmp_db
    inc = process_event(session, settings, EventIngest(**HUB_EVENT))
    session.refresh(inc)
    before = {c.key: getattr(inc, c.key) for c in IncidentRow.__table__.columns}

    build_alert(inc, settings.operator)

    after = {c.key: getattr(inc, c.key) for c in IncidentRow.__table__.columns}
    assert after == before
    assert not session.is_modified(inc) and not session.dirty and not session.new


# --------------------------------------------------------------------------------------
# The flag
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [(None, False), ("", False), ("false", False), ("0", False), ("banana", False), ("true", True), ("1", True), ("YES", True)],
)
def test_alert_envelope_v2_defaults_off(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("ALERT_ENVELOPE_V2", raising=False)
    else:
        monkeypatch.setenv("ALERT_ENVELOPE_V2", raw)
    assert alert_envelope_v2_enabled() is expected


def test_no_optional_extra_is_imported_at_module_level():
    assert "noc_agents.domain.alerts" in sys.modules and "noc_agents.services.alerts" in sys.modules
    assert "anthropic" not in sys.modules and "mcp" not in sys.modules


# --------------------------------------------------------------------------------------
# §6.1 mapping rules
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("priority, severity", [("P1", "EXTREME"), ("P2", "SEVERE"), ("P3", "MODERATE"), ("P4", "MINOR")])
def test_severity_follows_priority(priority, severity, cfg):
    assert severity_for(priority) == severity
    assert build_alert(_row(priority=priority), cfg, sent=SENT).classification.severity == severity


def test_severity_of_an_unknown_priority_is_unknown_not_a_crash():
    assert severity_for("P9") == "UNKNOWN" and severity_for(None) == "UNKNOWN"


@pytest.mark.parametrize(
    "status, urgency",
    [("TICKETED", "IMMEDIATE"), ("IN_PROGRESS", "IMMEDIATE"), ("AWAITING_VENDOR", "IMMEDIATE"), ("RESTORED", "PAST"), ("CLOSED", "PAST")],
)
def test_urgency_is_immediate_until_restored(status, urgency, cfg):
    assert urgency_for(status) == urgency
    assert build_alert(_row(status=status), cfg, sent=SENT).classification.urgency == urgency


@pytest.mark.parametrize(
    "status, msp_root_cause, lifecycle",
    [
        ("TICKETED", None, "INVESTIGATING"),
        ("ASSIGNED", None, "INVESTIGATING"),
        ("AWAITING_VENDOR", None, "INVESTIGATING"),
        ("IN_PROGRESS", None, "INVESTIGATING"),
        ("IN_PROGRESS", "   ", "INVESTIGATING"),
        ("IN_PROGRESS", "Mains fuse blown", "IDENTIFIED"),
        ("RESTORED", "Mains fuse blown", "MONITORING"),
        ("CLOSED", None, "RESOLVED"),
        ("NEW", None, "INVESTIGATING"),
    ],
)
def test_lifecycle_follows_status_and_root_cause(status, msp_root_cause, lifecycle, cfg):
    row = _row(status=status, msp_root_cause=msp_root_cause)
    assert lifecycle_for(row) == lifecycle
    assert build_alert(row, cfg, sent=SENT).classification.lifecycle == lifecycle
    assert build_alert(row, cfg, sent=SENT, lifecycle="MONITORING").classification.lifecycle == "MONITORING"  # explicit wins


def test_certainty_category_and_event(cfg):
    alert = build_alert(_row(), cfg, sent=SENT)
    assert alert.classification.certainty == "OBSERVED" and alert.classification.category == "Infra"
    assert alert.classification.event == "SITE_DOWN"
    assert build_alert(_row(service_affecting=False), cfg, sent=SENT).classification.event == "DEGRADED"


def test_timing_onset_prefers_outage_start_then_failure_time(cfg):
    onset = datetime(2026, 9, 16, 10, 41, 0)
    assert build_alert(_row(outage_start_at=onset, failure_time=None), cfg, sent=SENT).timing.onset == onset.replace(tzinfo=UTC)
    assert build_alert(_row(outage_start_at=None, failure_time=onset), cfg, sent=SENT).timing.onset == onset.replace(tzinfo=UTC)
    assert build_alert(_row(outage_start_at=None, failure_time=None), cfg, sent=SENT).timing.onset is None


def test_timing_effective_and_sent_are_aware_utc(cfg):
    alert = build_alert(_row(), cfg, sent=SENT)
    assert alert.sent == SENT.replace(tzinfo=UTC) and alert.timing.effective == alert.sent
    assert alert.sent.tzinfo is not None and alert.sent.utcoffset() == timedelta(0)
    # An aware EAT input lands on the same instant in UTC.
    eat = datetime(2026, 9, 16, 13, 47, 10, tzinfo=timezone(timedelta(hours=3)))
    assert build_alert(_row(), cfg, sent=eat).sent == SENT.replace(tzinfo=UTC)


def test_timing_expires_is_the_stored_next_update_when_present(cfg):
    due = datetime(2026, 9, 16, 11, 2, 10)
    assert build_alert(_row(next_update_at=due), cfg, sent=SENT).timing.expires == due.replace(tzinfo=UTC)


def test_timing_expires_falls_back_to_note_interval_times_region_multiplier(cfg):
    """§6.7.2: P4 in RFT → 120 min × 1.15 = 138 min after `sent`."""
    row = _row(priority="P4", region_code="RFT", next_update_at=None)
    assert note_interval_minutes(row, cfg) == 138
    alert = build_alert(row, cfg, sent=SENT)
    assert alert.timing.expires == (SENT + timedelta(minutes=138)).replace(tzinfo=UTC)
    # §6.7.1: P1 in NBI_E → 15 min × 1.0
    p1 = _row(priority="P1", region_code="NBI_E", next_update_at=None)
    assert build_alert(p1, cfg, sent=SENT).timing.expires == (SENT + timedelta(minutes=15)).replace(tzinfo=UTC)


def test_note_interval_never_drifts_from_the_worklog_monitor(cfg):
    """One cadence, two callers: the monitor's chase window and the envelope's expiry."""
    for priority in ("P1", "P2", "P3", "P4"):
        for region in [*cfg.regions, "ZZZ"]:
            row = _row(priority=priority, region_code=region)
            assert note_interval_minutes(row, cfg) == _note_interval_minutes(row, cfg), (priority, region)


def test_timing_restored_at_passes_through(cfg):
    restored = datetime(2026, 9, 16, 12, 30, 0)
    assert build_alert(_row(status="RESTORED", restored_at=restored), cfg, sent=SENT).timing.restored_at == restored.replace(tzinfo=UTC)
    assert build_alert(_row(), cfg, sent=SENT).timing.restored_at is None


def test_area_from_the_incident_and_config(cfg):
    area = build_alert(_row(), cfg, sent=SENT).area
    assert area == Area(
        region_code="NBI_E",
        region_label="Nairobi East",
        county="Nairobi",
        site_id="SFC-NBIE-HUB-EMB",
        site_name="Embakasi East Aggregation HUB",
        site_type="HUB",
        sites_affected=["SFC-NBIE-HUB-EMB"],
    )
    children = build_alert(_row(child_site_ids_json='["SFC-NBIE-ENB-01", "SFC-NBIE-HUB-EMB", "SFC-NBIE-ENB-02"]'), cfg, sent=SENT)
    assert children.area.sites_affected == ["SFC-NBIE-HUB-EMB", "SFC-NBIE-ENB-01", "SFC-NBIE-ENB-02"]
    assert build_alert(_row(child_site_ids_json="not json"), cfg, sent=SENT).area.sites_affected == ["SFC-NBIE-HUB-EMB"]
    assert build_alert(_row(region_code="XX9"), cfg, sent=SENT).area.region_label == "XX9"  # unknown region: code as label


def test_facts_from_the_incident(cfg):
    facts = build_alert(_row(), cfg, sent=SENT).facts
    assert facts == Facts(
        users_affected=450000,
        child_sites_down=0,
        mpesa_risk=True,
        failure_domain="POWER",
        tt_category="POWER_GRID",
        msp_code="EGYPRO",
        assignee_name="EGYPRO",
        assignee_role_token="MSP-EGYPRO-POWER",
        radio_oem="MIXED",
        planned_power=False,
        weather_context=None,
        service_affecting=True,
        services_impacted=["VOICE", "DATA", "SMS", "MPESA_CORRIDOR"],
        root_cause_hypothesis="Suspected Commercial power failure; awaiting field/MSP confirmation.",
    )


@pytest.mark.parametrize(
    "overrides, token",
    [
        (dict(assignee_type="MSP", msp_name="EGYPRO", failure_domain="POWER"), "MSP-EGYPRO-POWER"),
        (dict(assignee_type="MSP", msp_name=None, responsible_msp="tetranet", failure_domain="POWER"), "MSP-TETRANET-POWER"),
        (dict(assignee_type="MSP", msp_name=None, responsible_msp=None), None),
        (dict(assignee_type="FIELD_ENGINEER", assignee_name="FE-NBI-E-Kamau", region_code="NBI_E"), "FE-NBI-E-01"),
        (dict(assignee_type="FIELD_ENGINEER", region_code="XX9"), "FE-XX9-ONCALL"),
        (dict(assignee_type="NOC", assignee_name="NOC-QUEUE"), "NOC-QUEUE"),
        (dict(assignee_type="UNASSIGNED", assignee_name=None), None),
    ],
)
def test_assignee_role_token_is_a_role_never_a_person(overrides, token, cfg):
    assert build_alert(_row(**overrides), cfg, sent=SENT).facts.assignee_role_token == token


def test_msp_code_comes_from_msp_name_then_responsible_msp(cfg):
    assert build_alert(_row(msp_name="EGYPRO"), cfg, sent=SENT).facts.msp_code == "EGYPRO"
    assert build_alert(_row(msp_name=None, responsible_msp="tetranet"), cfg, sent=SENT).facts.msp_code == "TETRANET"
    assert build_alert(_row(msp_name="", responsible_msp=None), cfg, sent=SENT).facts.msp_code is None


def test_unflushed_row_defaults_do_not_crash(cfg):
    """Column defaults only apply at flush; a bare row must still build (no None where an int/bool/str is due)."""
    bare = IncidentRow(id="bare", operator_id="safaricom", incident_number="INC000009", priority="P3", correlation_fingerprint="fp")
    alert = build_alert(bare, cfg, sent=SENT)
    assert alert.facts.users_affected == 0 and alert.facts.child_sites_down == 0 and alert.facts.mpesa_risk is False
    assert alert.facts.failure_domain == "UNKNOWN" and alert.facts.tt_category == "OTHER"
    assert alert.facts.service_affecting is True and alert.facts.services_impacted == []
    assert alert.area.site_id == "" and alert.area.site_type == "BTS" and alert.area.region_label == ""
    assert alert.classification.lifecycle == "INVESTIGATING" and alert.classification.urgency == "IMMEDIATE"


# --------------------------------------------------------------------------------------
# Content
# --------------------------------------------------------------------------------------


def test_default_content_is_the_deterministic_template(cfg):
    row = _row()
    alert = build_alert(row, cfg, sent=SENT)
    assert set(alert.content) == {"en"}
    assert alert.content["en"] == Content(headline=row.title, body=row.narrative, instruction=V1_INSTRUCTION)
    assert alert.governance.ai_assisted is False


def test_ai_content_replaces_the_template_and_flags_ai_assisted(cfg):
    # §6.1-compliant drafts (validate_content runs at this seam): incident number, priority, region
    # label and the envelope's next-update time in every block.
    drafted = NocAlertContent(
        en=Content(
            headline="P2 INC000123 Embakasi HUB down - mains failure",
            body="Body. Region: Nairobi East.",
            instruction="Next update 14:17 EAT.",
        )
    )
    alert = build_alert(_row(), cfg, sent=SENT, ai_content={"en": drafted.en})
    assert alert.content["en"] == drafted.en and alert.governance.ai_assisted is True
    kituo = Content(headline="Kituo INC000123 P2 Nairobi East", body="Mwili. Taarifa ijayo 14:17 EAT.")
    both = build_alert(_row(), cfg, sent=SENT, ai_content={"en": drafted.en, "sw": kituo})
    assert set(both.content) == {"en", "sw"}


def test_ai_content_without_english_is_refused(cfg):
    with pytest.raises(ValidationError, match='content\\["en"\\] is mandatory'):
        build_alert(_row(), cfg, sent=SENT, ai_content={"sw": Content(headline="Kituo", body="Mwili.")})


def test_over_long_title_and_narrative_are_capped_not_crashed(cfg):
    row = _row(title="T" * 400, narrative="N" * 3000)
    en = build_alert(row, cfg, sent=SENT).content["en"]
    assert len(en.headline) == 160 and len(en.body) == 2000


# --------------------------------------------------------------------------------------
# Audiences, rendering, governance
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("priority", ["P1", "P2", "P3", "P4"])
def test_default_audiences_follow_the_broadcast_config(priority, cfg):
    expected_names = cfg.broadcast[f"{priority.lower()}_audiences"]
    specs = default_audiences(_row(priority=priority), cfg)
    assert [v1_audience_name(s.audience) for s in specs] == expected_names  # today's strings, in config order
    assert all(s.channels == ["EMAIL", "SMS"] and s.language == "en" for s in specs)


def test_default_audiences_fall_back_like_the_broadcast_node(cfg):
    stripped = cfg.model_copy(update={"broadcast": {}})
    specs = default_audiences(_row(priority="P4"), stripped)
    assert [s.audience for s in specs] == ["RNIO", "FE"]
    assert all(s.channels == ["EMAIL", "SMS"] for s in specs)


def test_recipients_refs_are_config_paths_never_addresses(cfg):
    specs = {s.audience: s.recipients_ref for s in default_audiences(_row(priority="P1"), cfg)}
    assert specs == {
        "RNIO": "regions.NBI_E.rnio",
        "FE": "regions.NBI_E.fe_oncall",
        "MSP": "msp_contacts.EGYPRO",
        "MANAGEMENT": "audiences.MANAGEMENT",
    }
    assert {s.audience: s.recipients_ref for s in default_audiences(_row(priority="P1", msp_name=None), cfg)}["MSP"] == "audiences.MSP"
    for ref in specs.values():
        assert "@" not in ref and "+254" not in ref


def test_explicit_audiences_are_used_verbatim(cfg):
    spec = AudienceSpec(audience="NOC_SHIFT", channels=["INAPP"], recipients_ref="audiences.NOC_SHIFT")
    alert = build_alert(_row(priority="P4"), cfg, sent=SENT, audiences=[spec])
    assert alert.audiences == [spec]
    assert alert.rendering.sms is None and alert.rendering.email is None  # no SMS/EMAIL channel selected


def test_rendering_is_v1_template_with_todays_subject(cfg):
    row = _row()
    r = build_alert(row, cfg, sent=SENT).rendering
    assert r.sms is not None and r.sms.template_key == V1_TEMPLATE_KEY and r.sms.encoding == "UCS2"  # the em dash
    assert r.email is not None and r.email.template_key == V1_TEMPLATE_KEY
    assert r.email.subject == "[P2] INC000123 | Embakasi East Aggregation HUB (HUB) | Nairobi East | Safaricom PLC (demo profile)"
    assert compose_email(row, cfg).startswith(f"Subject: {r.email.subject}\n\n")
    assert r.whatsapp is None


@pytest.mark.parametrize("priority, requires", [("P1", True), ("P2", True), ("P3", False), ("P4", False)])
def test_requires_hitl_follows_the_autonomy_gate_under_l2(priority, requires, cfg):
    assert cfg.autonomy_level == "L2_GUARDED"
    g = build_alert(_row(priority=priority), cfg, sent=SENT).governance
    assert g.requires_hitl is requires
    assert g.approved_by == (None if requires else "policy:L2_GUARDED")
    assert g.approved_at == (None if requires else SENT.replace(tzinfo=UTC))


def test_requires_hitl_for_non_internal_scope_and_external_audiences(cfg):
    p4 = _row(priority="P4")
    assert build_alert(p4, cfg, sent=SENT).governance.requires_hitl is False
    assert build_alert(p4, cfg, sent=SENT, scope="RESTRICTED").governance.requires_hitl is True
    for audience in ("REGULATOR", "CUSTOMER", "PUBLIC", "VENDOR_MANAGEMENT"):
        spec = AudienceSpec(audience=audience, channels=["EMAIL"], recipients_ref=f"audiences.{audience}")
        assert build_alert(p4, cfg, sent=SENT, audiences=[spec]).governance.requires_hitl is True, audience
    internal = AudienceSpec(audience="NOC_SHIFT", channels=["INAPP"], recipients_ref="audiences.NOC_SHIFT")
    assert build_alert(p4, cfg, sent=SENT, audiences=[internal]).governance.requires_hitl is False


def test_requires_hitl_under_other_autonomy_levels(cfg):
    l1 = cfg.model_copy(update={"autonomy_level": "L1_COPILOT"})
    l3 = cfg.model_copy(update={"autonomy_level": "L3_CONDITIONAL"})
    assert build_alert(_row(priority="P4"), l1, sent=SENT).governance.requires_hitl is True
    assert build_alert(_row(priority="P2"), l3, sent=SENT).governance.requires_hitl is False
    assert build_alert(_row(priority="P1"), l3, sent=SENT).governance.requires_hitl is True
    assert build_alert(_row(priority="P4"), l3, sent=SENT).governance.approved_by == "policy:L3_CONDITIONAL"


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({}, True),  # fe_name + rnio_name set by assignment today
        (dict(assignee_name=None, fe_name=None, rnio_name=None, access_notes=None), False),
        (dict(assignee_name="Jane Doe", fe_name=None, rnio_name=None, access_notes=None), True),
        (dict(assignee_name=None, fe_name=None, rnio_name=None, access_notes="Genset not started"), True),
        (dict(assignee_name="  ", fe_name="", rnio_name=None, access_notes=None), False),
    ],
)
def test_contains_personal_data_rule(overrides, expected, cfg):
    assert build_alert(_row(**overrides), cfg, sent=SENT).governance.contains_personal_data is expected
    assert alerts.channels_leave_kenya([]) is False  # no jurisdiction data yet: never forces HITL on its own


def test_governance_defaults_and_passthroughs(cfg):
    g = build_alert(_row(), cfg, sent=SENT, hitl_task_id="task-9").governance
    assert g.hitl_task_id == "task-9" and g.redaction_profile == "role_tokens" and g.transfer_record_id is None
    assert g.template_version == "1"


def test_envelope_identity_fields(cfg):
    row = _row()
    alert = build_alert(row, cfg, sent=SENT, alert_id="alert-1", sequence=3, msg_type="UPDATE", references=["alert-0"])
    assert alert.schema_version == 1 and alert.alert_id == "alert-1" and alert.sender == "noc.safaricom-demo.ke"
    assert alert.status == "ACTUAL" and alert.scope == "INTERNAL" and alert.msg_type == "UPDATE"
    assert alert.sequence == 3 and alert.references == ["alert-0"]
    assert alert.incident == IncidentRef(id="inc-0001", incident_number="INC000123", fingerprint="SFC-NBIE-HUB-EMB|POWER_GRID_FAIL|POWER")
    assert alert.idempotency_seed == "inc-0001|UPDATE|3"
    fresh = build_alert(row, cfg)
    assert len(fresh.alert_id) == 36 and fresh.alert_id != build_alert(row, cfg).alert_id  # uuid4 per call
    assert fresh.idempotency_seed == "inc-0001|ALERT|1"


# --------------------------------------------------------------------------------------
# The model's own rules
# --------------------------------------------------------------------------------------


def test_extra_keys_are_forbidden_everywhere(cfg):
    alert = build_alert(_row(), cfg, sent=SENT)
    dumped = alert.model_dump(mode="json")
    with pytest.raises(ValidationError):
        NocAlert.model_validate({**dumped, "bogus": 1})
    for path in ("incident", "classification", "timing", "area", "facts", "audiences", "rendering", "governance"):
        broken = json.loads(json.dumps(dumped))
        target = broken[path][0] if isinstance(broken[path], list) else broken[path]
        target["bogus"] = 1
        with pytest.raises(ValidationError):
            NocAlert.model_validate(broken)
    with pytest.raises(ValidationError):
        Facts(users_affected=1, failure_domain="POWER", tt_category="OTHER", fe_name="x")  # no person fields beyond the spec


def test_english_content_is_mandatory(cfg):
    dumped = build_alert(_row(), cfg, sent=SENT).model_dump(mode="json")
    dumped["content"] = {"sw": dumped["content"]["en"]}
    with pytest.raises(ValidationError, match='content\\["en"\\] is mandatory'):
        NocAlert.model_validate(dumped)


def test_content_length_caps_are_enforced_by_the_model():
    Content(headline="h" * 160, body="b" * 2000, instruction="i" * 500)
    for bad in (dict(headline="h" * 161, body="b"), dict(headline="h", body="b" * 2001), dict(headline="h", body="b", instruction="i" * 501)):
        with pytest.raises(ValidationError):
            Content(**bad)


def test_incident_number_pattern_is_the_spec_pattern():
    """§6.1 pins ``^INC\\d{6}$`` (the inc9 style). The airtel profile's dated style does not fit it —
    reported to the owner as a spec/config contradiction, not widened here."""
    IncidentRef(id="x", incident_number="INC000001", fingerprint="fp")
    for bad in ("INC1", "INC0000001", "ATL-20260916-00001", "inc000001"):
        with pytest.raises(ValidationError):
            IncidentRef(id="x", incident_number=bad, fingerprint="fp")


def test_literals_reject_unknown_vocabulary():
    good = dict(audience="RNIO", channels=["SMS"], recipients_ref="regions.NBI_E.rnio")
    AudienceSpec(**good)
    with pytest.raises(ValidationError):
        AudienceSpec(**{**good, "audience": "FIELD_ENGINEER"})  # today's name is mapped, not accepted raw
    with pytest.raises(ValidationError):
        AudienceSpec(**{**good, "channels": ["PIGEON"]})
    with pytest.raises(ValidationError):
        AudienceSpec(**{**good, "language": "fr"})
