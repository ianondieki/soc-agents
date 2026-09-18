"""``ALERT_ENVELOPE_V2`` is a real switch on the live path, in both positions.

Before this wave the flag changed nothing: ``services/render/*``, ``services/templates.py``,
``services/gsm7.py`` and ``services/validators.py`` had passing tests and no import path from
``noc_agents.main``. Now ``services/hitl.render_channels`` throws the switch and the HITL and
BROADCAST nodes persist what it returns. This file pins:

1. **flag off** (the default): a full ``process_event`` writes byte-identical SMS/email to
   ``services/composition`` — and never calls ``services/render`` at all (a spy raises if it
   does). The persisted drafts, the outbox payload shape and the step-row literals are the
   golden ones (copied here, not imported: the golden file may not be touched);
2. **flag on**: the renderers are genuinely used — the spy counts the calls, and the persisted
   rows carry things only that path produces (the measured encoding and segment count, the
   template version, the §6.2 suppression reasons). Delete the wiring and every one of these fails;
3. **flag on, today's template**: the EMAIL is REFUSED for both golden incidents on every
   audience (``email_missing_incident_number``: the v1 body carries the INC number only in the
   subject). That refusal is visible on the draft, the outbox row, the step row and the HITL
   card, and can never read as a send: no QUEUED/SENT status, no ``email.sent``, no WorkNote,
   nothing for the drain to claim. The SMS is NOT refused any more: the owner approved removing
   the em dash from the live SMS path (``services/composition.py``, ``services/render/sms.py``,
   the incident title in ``agents/ticket.py``), so today's SMS measures GSM-7 — 1 part for the
   short BTS incident, 2 for the long HUB one — passes §6.2 and is sent with provenance. These
   tests used to pin ``sms_not_gsm7`` / UCS-2 / 3 parts; the ``site_down_alert@1`` template
   ROW deliberately still carries the em dash as the record of what went out, which is why the
   registry must gain an approved version with the new wording before it ever drives rendering
   (``tests/unit/test_template_v2.py::test_v1_has_been_superseded_by_the_live_code_and_the_registry_must_catch_up``);
4. **flag on, validators satisfied**: relax the one check today's email cannot pass and an OK
   rendering on every channel is queued and sent exactly like today, with §6.3 provenance
   stamped in ``outbox.payload_json`` — so the refusal in (3) is the validators' verdict, not a
   hard-coded "never send";
5. **the registry decides**: pausing ``site_down_alert@1`` in ``message_templates`` turns the
   SMS refusal into ``no_approved_template`` — the table, not only the code, is consulted;
6. **the import graph**: ``services.render`` and ``services.templates`` are reachable from
   ``noc_agents.main`` (checked in a fresh interpreter) and imported by the agent modules
   (checked statically), so this wiring cannot silently rot back out.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import select

from noc_agents.adapters.email_smtp import parse_subject_body
from noc_agents.db.models import AgentRunRow, BroadcastRow, HitlTaskRow, MessageTemplateRow, OutboxRow, WorkNoteRow, utcnow
from noc_agents.domain.alerts import NocAlert
from noc_agents.domain.schemas import EventIngest
from noc_agents.graph.pipeline import process_event
from noc_agents.orchestrator import outbox
from noc_agents.realtime.hub import hub
from noc_agents.services import hitl as hitl_service
from noc_agents.services.alerts import V1_TEMPLATE_KEY, V1_TEMPLATE_VERSION, build_alert
from noc_agents.services.composition import compose_email, compose_sms
from noc_agents.services.gsm7 import sms_cost
from noc_agents.services.hitl import RENDERER_V2, SUPPRESSED_DRAFT, render_channels, rerender_and_release
from noc_agents.services.render import render_for_audience
from noc_agents.services.templates import TemplateRegistry
from noc_agents.services.validators import validate_email

FLAG = "ALERT_ENVELOPE_V2"
ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "noc_agents"

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
# Step-row literals the golden test pins with the flag off (copied, not imported).
GOLDEN_AUTO_BROADCAST_SUMMARY = "queued 3 outbox rows for ['RNIO', 'FIELD_ENGINEER']; email=PENDING"
GOLDEN_AUTO_BROADCAST_TOOLS = [
    {"name": "render_sms", "ok": True, "latency_ms": 1},
    {"name": "render_email", "ok": True, "latency_ms": 1},
    {"name": "outbox.enqueue", "ok": True, "latency_ms": 2, "error": None},
]
GOLDEN_HITL_BROADCAST_SUMMARY = "broadcasts drafted; waiting HITL"
GOLDEN_HITL_TOOLS = [{"name": "create_hitl_task", "ok": True, "latency_ms": 1}]
GOLDEN_HITL_BROADCAST_TOOLS = [{"name": "draft_broadcast", "ok": True, "latency_ms": 2}]
CHANNEL_KINDS = ("SMS", "EMAIL")


@pytest.fixture()
def clean_hub():
    hub._history.clear()
    yield hub
    hub._history.clear()


@pytest.fixture()
def flag_off(monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)


@pytest.fixture()
def flag_on(monkeypatch):
    monkeypatch.setenv(FLAG, "true")


@pytest.fixture()
def render_spy(monkeypatch):
    """Counts the live path's calls into ``services/render`` (the name bound in services/hitl)."""
    calls: list[tuple[str, str]] = []

    def spy(alert, aud):
        calls.append((alert.incident.incident_number, aud.audience))
        return render_for_audience(alert, aud)

    monkeypatch.setattr(hitl_service, "render_for_audience", spy)
    return calls


def _expected(inc, cfg, channel: str) -> str:
    return compose_sms(inc) if channel == "SMS" else compose_email(inc, cfg)


def _drafts(session, inc) -> list[BroadcastRow]:
    return session.scalars(select(BroadcastRow).where(BroadcastRow.incident_id == inc.id).order_by(BroadcastRow.id)).all()


def _channel_rows(session, inc) -> list[OutboxRow]:
    return session.scalars(
        select(OutboxRow)
        .where(OutboxRow.incident_id == inc.id, OutboxRow.kind.in_(CHANNEL_KINDS))
        .order_by(OutboxRow.created_at, OutboxRow.id)
    ).all()


def _steps(session, inc) -> dict[str, object]:
    run = session.scalar(select(AgentRunRow).where(AgentRunRow.incident_id == inc.id).order_by(AgentRunRow.started_at))
    return {s.node_name: s for s in run.steps}


def _payload(row: OutboxRow) -> dict:
    return json.loads(row.payload_json or "{}")


def _email_notes(session, inc) -> list[str]:
    return [n.body for n in session.scalars(select(WorkNoteRow).where(WorkNoteRow.incident_id == inc.id, WorkNoteRow.source == "email"))]


def _email_sent_events() -> list[dict]:
    return [e for e in hub._history if e["type"] == "email.sent"]


# --------------------------------------------------------------------------------------
# 1. flag off: today's code path, byte for byte, and services/render is never called
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("event", [HUB_EVENT, BTS_EVENT], ids=["p2_hub_hitl", "p4_bts_auto"])
def test_flag_off_renders_through_composition_and_never_touches_services_render(tmp_db, clean_hub, flag_off, monkeypatch, event):
    settings, session = tmp_db
    cfg = settings.operator

    def forbidden(alert, aud):  # pragma: no cover - only reached when the wiring leaks
        raise AssertionError("services/render was called with ALERT_ENVELOPE_V2 off")

    monkeypatch.setattr(hitl_service, "render_for_audience", forbidden)
    inc = process_event(session, settings, EventIngest(**event))
    session.refresh(inc)

    # The persisted wording is composition.py's, byte for byte, on every draft.
    drafts = _drafts(session, inc)
    assert drafts
    for d in drafts:
        assert d.message.encode("utf-8") == _expected(inc, cfg, d.channel).encode("utf-8"), (d.channel, d.audience)
        assert d.status != SUPPRESSED_DRAFT
    # The switch itself reports the v1 path: no payloads, no registry verdicts.
    rendering = render_channels(build_alert(inc, cfg), inc, cfg, session=session)
    assert rendering.v2 is False and rendering.payloads == [] and rendering.templates == {}
    assert rendering.sms == compose_sms(inc) and rendering.email == compose_email(inc, cfg)
    # No §6.3 provenance is stamped on the v1 outbox payloads, and no registry seed happened.
    for row in _channel_rows(session, inc):
        assert "template_version" not in _payload(row) and "renderer" not in _payload(row)
    assert session.scalar(select(MessageTemplateRow.id).limit(1)) is None

    steps = _steps(session, inc)
    if inc.requires_hitl:
        task = session.scalar(select(HitlTaskRow).where(HitlTaskRow.incident_id == inc.id))
        assert set(task.proposed_payload) == {"priority", "sms", "email", "audiences", "assignee", "alert_id", "envelope"}
        assert task.proposed_payload["sms"] == compose_sms(inc) and task.proposed_payload["email"] == compose_email(inc, cfg)
        assert {d.status for d in drafts} == {"PENDING_HITL"}
        assert steps["HITL"].tools_called == GOLDEN_HITL_TOOLS
        assert steps["BROADCAST"].output_summary == GOLDEN_HITL_BROADCAST_SUMMARY
        assert steps["BROADCAST"].tools_called == GOLDEN_HITL_BROADCAST_TOOLS
    else:
        assert {d.status for d in drafts} == {"SENT"}
        assert steps["HITL"].tools_called == []
        assert steps["BROADCAST"].output_summary == GOLDEN_AUTO_BROADCAST_SUMMARY
        assert steps["BROADCAST"].tools_called == GOLDEN_AUTO_BROADCAST_TOOLS
        assert len(_email_sent_events()) == 1


# --------------------------------------------------------------------------------------
# 2 + 3. flag on, today's template: services/render is used; the email is refused — visibly —
#        and the (now GSM-7) SMS is sent with provenance
# --------------------------------------------------------------------------------------


def test_flag_on_auto_path_renders_through_services_render_sends_the_sms_and_refuses_todays_email(tmp_db, clean_hub, flag_on, render_spy):
    """This test used to expect every channel refused, under the name
    ``..._and_refuses_todays_template`` (SMS ``sms_not_gsm7`` at UCS-2 / 3 parts). Then the
    owner approved removing the em dash from the live SMS path, so today's SMS is GSM-7 and
    passes §6.2 on its own; only the email is still refused (``email_missing_incident_number``:
    the v1 body carries the INC number only in the subject).

    What is pinned is unchanged in kind: the renderers are genuinely used, an OK rendering is
    sent exactly like today with §6.3 provenance and a MEASURED cost, and a refused one is
    SUPPRESSED on the draft, the outbox row and the step row and never looks like a send.
    """
    settings, session = tmp_db
    cfg = settings.operator
    inc = process_event(session, settings, EventIngest(**BTS_EVENT))
    session.refresh(inc)
    assert inc.priority == "P4" and inc.requires_hitl is False

    # The renderers ran, once per audience, and the hot path seeded the registry on first use.
    assert render_spy == [(inc.incident_number, "RNIO"), (inc.incident_number, "FE")]
    assert session.scalar(select(MessageTemplateRow.id).limit(1)) is not None

    # The SMS drafts went out. The EMAIL drafts are SUPPRESSED — never QUEUED, never SENT — and
    # keep the refused text so the timeline shows what did not go out. Every draft still
    # carries composition.py's bytes.
    drafts = _drafts(session, inc)
    assert sorted((d.channel, d.audience, d.status) for d in drafts) == [
        ("EMAIL", "FIELD_ENGINEER", SUPPRESSED_DRAFT),
        ("EMAIL", "RNIO", SUPPRESSED_DRAFT),
        ("SMS", "FIELD_ENGINEER", "SENT"),
        ("SMS", "RNIO", "SENT"),
    ]
    for d in drafts:
        assert d.message.encode("utf-8") == _expected(inc, cfg, d.channel).encode("utf-8")
        assert (d.sent_at is None) == (d.channel == "EMAIL")

    # The outbox ledger: the SMS rows SENT with the measured GSM-7 cost, the EMAIL row closed
    # SUPPRESSED with an actionable reason, all carrying the §6.3 provenance only the renderer
    # path produces. The dispatcher has nothing left to claim: the SMS already went with the
    # run, and a SUPPRESSED row is never claimed.
    rows = _channel_rows(session, inc)
    assert sorted((r.kind, r.status) for r in rows) == [("EMAIL", outbox.SUPPRESSED), ("SMS", outbox.SENT), ("SMS", outbox.SENT)]
    cost = sms_cost(compose_sms(inc))
    # This BTS incident is a SHORT one and fits one part (with only a few septets to spare);
    # the long HUB incident in the next test needs two. Not "always 1".
    assert (cost.encoding, cost.segments) == ("GSM7", 1)
    for r in rows:
        p = _payload(r)
        assert p["renderer"] == RENDERER_V2
        assert (p["template_key"], p["template_version"]) == (V1_TEMPLATE_KEY, V1_TEMPLATE_VERSION)
        assert len(p["channel_idempotency_key"]) == 40
        if r.kind == "SMS":
            assert (p["rendering_status"], p["suppress_reason"]) == ("OK", None)
            assert (p["encoding"], p["segments"]) == (cost.encoding, cost.segments)  # measured, only the renderer path knows this
            assert p["message"] == compose_sms(inc)
            assert r.last_error is None and r.sent_at is not None
        else:
            assert (p["rendering_status"], p["suppress_reason"]) == ("SUPPRESSED", "email_missing_incident_number")
            assert r.last_error.startswith("email_missing_incident_number: ") and "D3" in r.last_error
            assert (p["subject"], p["body"]) == parse_subject_body(compose_email(inc, cfg))  # the v1 payload shape
            assert r.sent_at is None
        assert r.approved_by == "policy:L2_GUARDED"
    report = outbox.drain_once(session)
    assert (report.claimed, report.sent) == (0, 0)
    assert sorted((r.kind, r.status) for r in _channel_rows(session, inc)) == [("EMAIL", outbox.SUPPRESSED), ("SMS", outbox.SENT), ("SMS", outbox.SENT)]

    # Nothing that follows an email send happened: no email.sent, no WorkNote, no SENT email draft.
    assert _email_sent_events() == [] and _email_notes(session, inc) == []
    assert {d.status for d in _drafts(session, inc) if d.channel == "EMAIL"} == {SUPPRESSED_DRAFT}

    # The step rows say so, in words an operator reads on the run view and the timeline.
    steps = _steps(session, inc)
    assert steps["HITL"].tools_called == [
        {"name": "render_channels", "ok": False, "latency_ms": 1, "error": "2/4 refused (EMAIL: email_missing_incident_number x2)"}
    ]
    broadcast = steps["BROADCAST"]
    assert broadcast.status == "SUCCEEDED"
    assert broadcast.output_summary.startswith("queued 2 outbox rows for ['RNIO', 'FIELD_ENGINEER']; email=SUPPRESSED; 2/4 renderings refused by §6.2 (")
    assert broadcast.output_summary.endswith("nothing sent for them")
    assert f"rendered by {RENDERER_V2}" in broadcast.rationale and "site_down_alert@1" in broadcast.rationale
    by_name = {t["name"]: t for t in broadcast.tools_called}
    assert by_name["template_registry.resolve"] == {"name": "template_registry.resolve", "ok": True, "latency_ms": 1, "error": None}
    assert by_name["render_sms"] == {"name": "render_sms", "ok": True, "latency_ms": 1, "error": None}
    assert by_name["render_email"] == {"name": "render_email", "ok": False, "latency_ms": 1, "error": "email_missing_incident_number x2"}
    assert by_name["outbox.enqueue"]["ok"] is True


def test_flag_on_hitl_path_shows_every_verdict_on_the_card_and_the_release_sends_the_sms_but_keeps_the_email_refused(
    tmp_db, clean_hub, flag_on, render_spy
):
    """Used to be ``..._and_the_release_stays_refused`` with 8/8 suppressed; the auto-path test
    above says why the SMS half now passes. Still pinned: the §6.5 card shows every rendering
    with its verdict, reason, measured cost and template; nothing reaches the outbox while the
    task is open; the approve re-render runs through the same renderers; the email stays
    refused all the way through the release AND a drain, producing no ``email.sent`` and no
    WorkNote; and the release records exactly what it did.
    """
    settings, session = tmp_db
    cfg = settings.operator
    inc = process_event(session, settings, EventIngest(**HUB_EVENT))
    session.refresh(inc)
    assert inc.priority == "P2" and inc.requires_hitl is True
    assert [aud for _, aud in render_spy] == ["RNIO", "FE", "MSP", "MANAGEMENT"]

    # The §6.5 side-by-side card: every rendering with its verdict, reason, cost and template.
    task = session.scalar(select(HitlTaskRow).where(HitlTaskRow.incident_id == inc.id))
    card = task.proposed_payload["channels"]
    assert card["renderer"] == RENDERER_V2 and (card["total"], card["suppressed"]) == (8, 4)
    assert [(p["channel"], p["audience"], p["status"], p["suppress_reason"]) for p in card["payloads"]] == [
        (ch, aud, "SUPPRESSED", "email_missing_incident_number") if ch == "EMAIL" else (ch, aud, "OK", None)
        for aud in ("RNIO", "FE", "MSP", "MANAGEMENT")
        for ch in ("EMAIL", "SMS")
    ]
    cost = sms_cost(compose_sms(inc))
    assert (cost.encoding, cost.segments) == ("GSM7", 2)  # a LONG incident: GSM-7 now, but still two parts
    for p in card["payloads"]:
        assert (p["template_key"], p["template_version"]) == (V1_TEMPLATE_KEY, V1_TEMPLATE_VERSION)
        if p["channel"] == "SMS":
            assert p["reason"] is None
            assert (p["encoding"], p["segments"]) == (cost.encoding, cost.segments)
            assert p["body"] == compose_sms(inc)
        else:
            assert p["reason"].startswith(p["suppress_reason"] + ": ")
            assert f"Subject: {p['subject']}\n\n{p['body']}" == compose_email(inc, cfg)
    for channel in ("SMS", "EMAIL"):
        verdict = card["templates"][channel]
        assert (verdict["template_key"], verdict["template_version"], verdict["sendable"]) == (V1_TEMPLATE_KEY, V1_TEMPLATE_VERSION, True)
        assert (verdict["approval_status"], verdict["approved_by"], verdict["reason"]) == ("APPROVED", "policy:v1_fidelity", None)
    # The strings the legacy card fields carry are unchanged bytes; the drafts wait for the human.
    assert task.proposed_payload["sms"] == compose_sms(inc) and task.proposed_payload["email"] == compose_email(inc, cfg)
    drafts = _drafts(session, inc)
    assert len(drafts) == 8 and {d.status for d in drafts} == {"PENDING_HITL"}
    assert _channel_rows(session, inc) == []  # M1: nothing reaches the outbox while the task is open
    steps = _steps(session, inc)
    assert steps["HITL"].tools_called[0] == GOLDEN_HITL_TOOLS[0]
    assert steps["HITL"].tools_called[1]["name"] == "render_channels" and steps["HITL"].tools_called[1]["ok"] is False
    assert steps["BROADCAST"].output_summary == (
        "broadcasts drafted; waiting HITL; 4/8 renderings refused by §6.2 (EMAIL: email_missing_incident_number x4)"
    )

    # Approve with an override: the re-render runs through the same renderers. The four SMS
    # renderings are released (HELD → PENDING with the human stamped); the email is refused
    # again — the approval releases nothing for it, and says so.
    inc.priority = "P1"
    task.status, task.resolved_by, task.resolved_at = "APPROVED", "Supervisor A", utcnow()
    result = rerender_and_release(session, inc, cfg, task=task, approved_by="Supervisor A", approved_at=utcnow())
    session.commit()
    assert (result.released, result.suppressed, result.edited) == (4, 1, True)
    assert result.rendering is not None and result.rendering.v2 and len(result.rendering.suppressed) == 4
    assert {p.channel for p in result.rendering.suppressed} == {"EMAIL"}
    assert result.sms == compose_sms(inc) and result.sms.startswith("[P1] ")
    released_cost = sms_cost(result.sms)
    assert (released_cost.encoding, released_cost.segments) == ("GSM7", 2)

    drafts = _drafts(session, inc)
    assert sorted((d.channel, d.status) for d in drafts) == [("EMAIL", SUPPRESSED_DRAFT)] * 4 + [("SMS", "QUEUED")] * 4
    assert {d.message for d in drafts if d.channel == "SMS"} == {compose_sms(inc)}
    rows = _channel_rows(session, inc)
    assert sorted((r.kind, r.status) for r in rows) == [("EMAIL", outbox.SUPPRESSED)] + [("SMS", outbox.PENDING)] * 4
    (mail,) = [r for r in rows if r.kind == "EMAIL"]
    assert mail.last_error.startswith("email_missing_incident_number: ")
    for r in rows:
        env = NocAlert.model_validate_json(r.envelope_json)
        assert env.sequence == 2 and env.governance.approved_by == "Supervisor A" and env.classification.priority == "P1"
        p = _payload(r)
        assert p["template_version"] == V1_TEMPLATE_VERSION
        if r.kind == "SMS":
            assert (p["rendering_status"], p["suppress_reason"]) == ("OK", None)
            assert (p["encoding"], p["segments"]) == (released_cost.encoding, released_cost.segments)
            assert r.last_error is None
        else:
            assert (p["rendering_status"], p["suppress_reason"]) == ("SUPPRESSED", "email_missing_incident_number")
        assert r.approved_by == "Supervisor A" and r.sent_at is None
    # The drain transmits exactly the four released SMS rows and never touches the email.
    assert outbox.drain_once(session).sent == 4
    assert sorted((r.kind, r.status) for r in _channel_rows(session, inc)) == [("EMAIL", outbox.SUPPRESSED)] + [("SMS", outbox.SENT)] * 4
    assert sorted((d.channel, d.status) for d in _drafts(session, inc)) == [("EMAIL", SUPPRESSED_DRAFT)] * 4 + [("SMS", "SENT")] * 4
    assert _email_sent_events() == [] and _email_notes(session, inc) == []
    released = task.proposed_payload["released"]
    assert (released["channels"]["total"], released["channels"]["suppressed"]) == (8, 4)
    assert released["sequence"] == 2 and released["priority"] == "P1"


# --------------------------------------------------------------------------------------
# 4. flag on, validators satisfied: an OK rendering is sent, with provenance
# --------------------------------------------------------------------------------------


def test_flag_on_ok_rendering_is_queued_and_sent_with_provenance(tmp_db, clean_hub, flag_on, render_spy, monkeypatch):
    """The refusal above is the validators' verdict, not a hard-coded refusal: relax the ONE
    check today's template cannot pass (the email body tokens) and the same path queues, drains
    and sends every channel.

    This used to relax ``validate_sms``'s encoding check as well. It no longer does: since the
    owner-approved em-dash removal the SMS passes the real validator, and relaxing it here
    would hide the day an em dash came back on the live path."""
    import noc_agents.services.render.email as email_renderer

    monkeypatch.setattr(email_renderer, "validate_email", lambda subject, body, **kw: validate_email(subject, body))

    settings, session = tmp_db
    cfg = settings.operator
    inc = process_event(session, settings, EventIngest(**BTS_EVENT))
    session.refresh(inc)
    assert len(render_spy) == 2

    drafts = _drafts(session, inc)
    assert sorted((d.channel, d.audience, d.status) for d in drafts) == [
        ("EMAIL", "FIELD_ENGINEER", "SENT"),
        ("EMAIL", "RNIO", "SENT"),
        ("SMS", "FIELD_ENGINEER", "SENT"),
        ("SMS", "RNIO", "SENT"),
    ]
    for d in drafts:
        assert d.message.encode("utf-8") == _expected(inc, cfg, d.channel).encode("utf-8")
    rows = _channel_rows(session, inc)
    assert sorted(r.kind for r in rows) == ["EMAIL", "SMS", "SMS"] and {r.status for r in rows} == {outbox.SENT}
    assert {r.idempotency_key for r in rows} == {
        f"SMS:{inc.id}:RNIO",
        f"SMS:{inc.id}:FIELD_ENGINEER",
        f"EMAIL:{inc.id}:RNIO,FIELD_ENGINEER",
    }  # the v1 key space: flipping the flag cannot double-send an incident
    for r in rows:
        p = _payload(r)
        assert (p["renderer"], p["template_key"], p["template_version"], p["rendering_status"], p["suppress_reason"]) == (
            RENDERER_V2, V1_TEMPLATE_KEY, V1_TEMPLATE_VERSION, "OK", None
        )
        if r.kind == "SMS":
            cost = sms_cost(compose_sms(inc))
            assert (cost.encoding, cost.segments) == ("GSM7", 1)  # the short BTS incident; a long one still needs 2
            assert (p["encoding"], p["segments"]) == (cost.encoding, cost.segments)  # measured, only the renderer path knows this
    assert len(_email_sent_events()) == 1 and len(_email_notes(session, inc)) == 1
    steps = _steps(session, inc)
    assert steps["BROADCAST"].output_summary == GOLDEN_AUTO_BROADCAST_SUMMARY  # same shape as the golden literal
    assert steps["HITL"].tools_called == [{"name": "render_channels", "ok": True, "latency_ms": 1, "error": None}]
    assert {t["name"]: t["ok"] for t in steps["BROADCAST"].tools_called} == {
        "template_registry.resolve": True, "render_sms": True, "render_email": True, "outbox.enqueue": True
    }


# --------------------------------------------------------------------------------------
# 5. flag on: the template registry is consulted, not only the code
# --------------------------------------------------------------------------------------


def test_flag_on_a_paused_template_row_refuses_the_channel(tmp_db, clean_hub, flag_on):
    settings, session = tmp_db
    registry = TemplateRegistry.for_config(session, settings.operator)
    registry.sync()
    registry.set_status(registry.get("SMS", V1_TEMPLATE_KEY, "en", int(V1_TEMPLATE_VERSION)), "PAUSED", actor="ops.lead")
    session.commit()

    inc = process_event(session, settings, EventIngest(**BTS_EVENT))
    session.refresh(inc)
    rendering = render_channels(build_alert(inc, settings.operator), inc, settings.operator, session=session)
    assert rendering.v2
    sms_verdict = rendering.templates["SMS"]
    assert (sms_verdict["sendable"], sms_verdict["approval_status"]) == (False, "PAUSED")
    assert sms_verdict["reason"] == "SMS/site_down_alert/en@1 is PAUSED, not APPROVED"
    assert rendering.templates["EMAIL"]["sendable"] is True
    cost = sms_cost(compose_sms(inc))
    for p in rendering.payloads:
        if p.channel == "SMS":
            assert (p.status, p.suppress_reason) == ("SUPPRESSED", "no_approved_template")
            # Both verdicts are still shown: the registry's (PAUSED) as a warning, and the
            # validator's as the measured cost. The validator's verdict today is CLEAN — the
            # live SMS is GSM-7 — so there is no ``sms_not_gsm7`` warning beside it; one
            # appearing here would mean the em dash is back on the live path. (This assertion
            # used to require that warning, when the SMS was UCS-2.)
            assert any("PAUSED" in w for w in p.warnings)
            assert not any("sms_not_gsm7" in w for w in p.warnings)
            assert (p.encoding, p.segments) == (cost.encoding, cost.segments) == ("GSM7", 1)
        else:
            assert (p.status, p.suppress_reason) == ("SUPPRESSED", "email_missing_incident_number")

    # ...and that is what the run persisted.
    rows = _channel_rows(session, inc)
    assert {r.last_error.split(":")[0] for r in rows if r.kind == "SMS"} == {"no_approved_template"}
    assert {_payload(r)["suppress_reason"] for r in rows if r.kind == "SMS"} == {"no_approved_template"}
    steps = _steps(session, inc)
    by_name = {t["name"]: t for t in steps["BROADCAST"].tools_called}
    assert by_name["template_registry.resolve"]["ok"] is False
    assert by_name["template_registry.resolve"]["error"] == "SMS: SMS/site_down_alert/en@1 is PAUSED, not APPROVED"
    assert by_name["render_sms"]["error"] == "no_approved_template x2"


# --------------------------------------------------------------------------------------
# 6. the import graph: services/render is on the live path and stays there
# --------------------------------------------------------------------------------------


def _imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def test_agent_modules_import_services_render_statically():
    """The wiring is module-level imports in the agents, not a lazy import a refactor can drop."""
    assert "noc_agents.services.render" in _imports_of(SRC / "agents" / "broadcast.py")
    assert "noc_agents.services.render.email" in _imports_of(SRC / "agents" / "broadcast.py")
    assert "noc_agents.services.render" in _imports_of(SRC / "agents" / "hitl.py")
    assert {"noc_agents.services.render", "noc_agents.services.render.email", "noc_agents.services.templates"} <= _imports_of(
        SRC / "services" / "hitl.py"
    )
    assert "noc_agents.services.hitl" in _imports_of(SRC / "agents" / "hitl.py")
    assert "noc_agents.services.hitl" in _imports_of(SRC / "agents" / "broadcast.py")


def test_services_render_and_templates_are_reachable_from_main(tmp_path):
    """In a fresh interpreter, importing ``noc_agents.main`` transitively imports the modules
    the conformance audit found unreachable — and still no optional extra."""
    probe = (
        "import json, sys\n"
        "import noc_agents.main\n"
        "want = ['noc_agents.services.render', 'noc_agents.services.render.sms', 'noc_agents.services.render.email',\n"
        "        'noc_agents.services.render.inapp', 'noc_agents.services.render.ledger', 'noc_agents.services.render.whatsapp',\n"
        "        'noc_agents.services.templates', 'noc_agents.services.gsm7', 'noc_agents.services.validators',\n"
        "        'noc_agents.agents.hitl', 'noc_agents.agents.broadcast', 'noc_agents.services.hitl']\n"
        "print('PROBE ' + json.dumps({'missing': [m for m in want if m not in sys.modules],\n"
        "                            'extras': [m for m in ('anthropic', 'mcp') if m in sys.modules]}))\n"
    )
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "src"),
        "DATABASE_URL": f"sqlite:///{(tmp_path / 'probe.db').as_posix()}",
        "LEDGER_DIR": str(tmp_path),
    }
    env.pop(FLAG, None)
    done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, env=env, timeout=180, cwd=str(ROOT))
    assert done.returncode == 0, done.stderr[-2000:]
    line = next(ln for ln in done.stdout.splitlines() if ln.startswith("PROBE "))
    assert json.loads(line[len("PROBE ") :]) == {"missing": [], "extras": []}
