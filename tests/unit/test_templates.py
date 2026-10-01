"""The template registry (spec §6.3) and its language rules (§6.4).

Four properties the task names, each with its own section below:

1. ``sync`` is idempotent — run it twice, get no second copy of anything, and no write at all;
2. changing a body produces a NEW version; the old row is never mutated, and what would be
   sent does not change until a human approves the new one;
3. an unapproved template cannot be used for a real send;
4. every seeded template has an English body.

Plus the proof that makes the registry worth having at all: ``site_down_alert@1`` rendered
out of the table against ``services/composition.py:compose_sms`` / ``compose_email`` on both
golden incidents. The EMAIL row is **byte-identical**. The SMS row is **one em dash behind**
the live message, and that is pinned exactly: the owner approved removing the em dash from
the live SMS path (composition.py, render/sms.py, the incident title in agents/ticket.py),
and ``@1`` was deliberately NOT edited to follow -- it is the record of what actually went
out, and a versioned registry answers a wording change with a new version, never by
rewriting an approved one. The comparison stays character-precise so that any SECOND
divergence still fails here. Same standard as
``tests/unit/test_alerts_envelope.py::test_v1_fidelity_*``, one layer further out — that one
proves the *envelope* carries the live wording, this one proves what the *table* carries.

Nothing here touches the golden sequence, ``main.py`` or the frozen contract file: the
registry is read and written through its own session, on the ``tmp_db`` fixture.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import pytest
from sqlalchemy import func, select

from noc_agents.db.models import IncidentRow, MessageTemplateRow
from noc_agents.domain.schemas import EventIngest
from noc_agents.graph.pipeline import process_event
from noc_agents.realtime.hub import hub
from noc_agents.services.alerts import V1_TEMPLATE_KEY, V1_TEMPLATE_VERSION, build_alert
from noc_agents.services.composition import compose_email, compose_sms
from noc_agents.services.gsm7 import is_gsm7
from noc_agents.services.templates import (
    ALLOWED_PARAMS,
    APPROVAL_STATUSES,
    DEFAULT_LANGUAGE,
    REVIEWED_LANGUAGES,
    SENDABLE_STATUSES,
    TEMPLATE_KEYS,
    SeedTemplate,
    TemplateNotApproved,
    TemplateNotFound,
    TemplateRegistry,
    TemplateRenderError,
    TemplateSeedError,
    content_fingerprint,
    context_from_alert,
    load_seed_templates,
    params_vocabulary,
    row_fingerprint,
)

# The two events the golden test drives, kept local so this file never depends on
# tests/integration/test_golden_sequence.py (which may not be touched).
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
BTS_EVENT = dict(
    site_id="SFC-MTK-BTS-MCH04",
    site_name="Machakos Town BTS",
    site_type="BTS",
    region_code="MTK",
    alarm_code="SITE_DOWN",
    failure_domain="POWER",
    users_affected=3200,
)
APPROVER = "ops.lead@safaricom.demo"  # a test actor, not a real person


@pytest.fixture()
def clean_hub():
    hub._history.clear()
    yield hub
    hub._history.clear()


@pytest.fixture()
def registry(tmp_db):
    """A synced registry for the safaricom profile, plus its settings and session."""
    settings, session = tmp_db
    reg = TemplateRegistry.for_config(session, settings.operator)
    reg.sync()
    session.commit()
    return settings, session, reg


def _row(**overrides) -> IncidentRow:
    """A fully populated, unflushed IncidentRow (column defaults only apply at flush)."""
    base = dict(
        id="inc-tmpl-1",
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
        next_update_at=datetime(2026, 9, 16, 11, 2, 0),
        outage_start_at=datetime(2026, 9, 16, 10, 41, 0),
        failure_time=None,
        restored_at=None,
        msp_root_cause=None,
    )
    base.update(overrides)
    row = IncidentRow(**base)
    row.services_impacted = ["VOICE", "DATA", "SMS", "MPESA_CORRIDOR"]
    return row


def _seed(session, operator_id: str) -> TemplateRegistry:
    reg = TemplateRegistry(session, operator_id)
    reg.sync()
    return reg


def _count(session, operator_id: str) -> int:
    return session.scalar(
        select(func.count()).select_from(MessageTemplateRow).where(MessageTemplateRow.operator_id == operator_id)
    )


# ======================================================================================
# 1. sync is idempotent
# ======================================================================================


def test_sync_twice_writes_nothing_the_second_time(tmp_db):
    settings, session = tmp_db
    reg = TemplateRegistry.for_config(session, settings.operator)

    first = reg.sync()
    session.commit()
    assert first.inserted, "the first sync must actually seed something"
    assert first.unchanged == [] and first.bumped == []
    after_first = _count(session, settings.operator.operator_id)
    stamps = {r.id: (r.version, r.body, r.created_at, r.updated_at) for r in reg.all_rows()}

    second = reg.sync()
    session.commit()

    assert second.inserted == [], "a second sync inserted rows: it is not idempotent"
    assert second.bumped == []
    assert sorted(second.unchanged) == sorted(first.inserted)
    assert _count(session, settings.operator.operator_id) == after_first
    # Not merely "no duplicates": no WRITE at all. updated_at has onupdate=utcnow, so a
    # touched row would show here even if its content came out the same.
    assert {r.id: (r.version, r.body, r.created_at, r.updated_at) for r in reg.all_rows()} == stamps


def test_sync_is_idempotent_across_sessions(tmp_db):
    """A fresh registry object over the same data reaches the same conclusion."""
    settings, session = tmp_db
    _seed(session, settings.operator.operator_id)
    session.commit()
    before = _count(session, settings.operator.operator_id)

    again = TemplateRegistry(session, settings.operator.operator_id).sync()
    session.commit()

    assert again.inserted == []
    assert _count(session, settings.operator.operator_id) == before


def test_sync_is_operator_scoped(tmp_db):
    """Two operators seed independently and neither can see the other's rows."""
    settings, session = tmp_db
    mine = _seed(session, "safaricom")
    theirs = _seed(session, "airtel")
    session.commit()

    assert len(mine.all_rows()) == len(theirs.all_rows()) > 0
    assert {r.operator_id for r in mine.all_rows()} == {"safaricom"}
    assert {r.operator_id for r in theirs.all_rows()} == {"airtel"}
    # A row id from the other operator is "not found", never "forbidden" (main.py's rule).
    other = theirs.all_rows()[0]
    with pytest.raises(TemplateNotFound):
        mine.set_status(other.id, "APPROVED", actor=APPROVER)


# ======================================================================================
# 2. a changed body is a NEW version, never a mutation
# ======================================================================================


def _sms_seed(registry_seeds) -> SeedTemplate:
    return next(s for s in registry_seeds if s.lineage == ("SMS", V1_TEMPLATE_KEY, "en"))


def test_changing_a_body_inserts_a_new_version_and_leaves_the_old_row_alone(registry):
    settings, session, reg = registry
    seeds, _ = load_seed_templates(settings.operator.operator_id)
    original = reg.get("SMS", V1_TEMPLATE_KEY, "en", 1)
    assert original is not None
    before = (original.id, original.body, original.approval_status, original.approved_by, original.created_at)

    edited = replace(_sms_seed(seeds), body=_sms_seed(seeds).body.replace("est.users", "est. users"))
    report = reg.sync([edited])
    session.commit()

    assert report.inserted == ["SMS/site_down_alert/en@2"]
    assert report.bumped and "pinned version 1" in report.bumped[0]

    # The old row is byte-for-byte what it was. This is the regulator answer: the words that
    # went out at 02:14 last Tuesday are still readable after the wording changed.
    old = reg.get("SMS", V1_TEMPLATE_KEY, "en", 1)
    assert (old.id, old.body, old.approval_status, old.approved_by, old.created_at) == before
    assert "est.users" in old.body

    new = reg.get("SMS", V1_TEMPLATE_KEY, "en", 2)
    assert new is not None and new.id != old.id
    assert "est. users" in new.body
    assert [r.version for r in reg.versions("SMS", V1_TEMPLATE_KEY, "en")] == [1, 2]


def test_a_new_version_is_not_approved_by_the_old_approval(registry):
    """A changed body starts DRAFT even though the YAML block says APPROVED.

    This is the fail-safe: editing config cannot put unreviewed words on the wire.
    """
    settings, session, reg = registry
    seeds, _ = load_seed_templates(settings.operator.operator_id)
    seed = _sms_seed(seeds)
    assert seed.approval_status == "APPROVED"  # what the YAML declares for @1

    reg.sync([replace(seed, body=seed.body + "\nExtra line")])
    session.commit()

    assert reg.get("SMS", V1_TEMPLATE_KEY, "en", 2).approval_status == "DRAFT"
    # ...and the send path is unchanged: @1 is still the newest APPROVED version.
    assert reg.latest_approved("SMS", V1_TEMPLATE_KEY, "en").version == 1
    assert reg.latest("SMS", V1_TEMPLATE_KEY, "en").version == 2


def test_approving_the_new_version_is_what_switches_the_send(registry, clean_hub):
    settings, session, reg = registry
    seeds, _ = load_seed_templates(settings.operator.operator_id)
    seed = _sms_seed(seeds)
    reg.sync([replace(seed, body=seed.body.replace("ticket notes", "TT notes"))])
    session.commit()
    alert = build_alert(_row(), settings.operator)

    assert "ticket notes" in reg.render(alert, "SMS").body  # still @1

    reg.set_status(reg.get("SMS", V1_TEMPLATE_KEY, "en", 2), "APPROVED", actor=APPROVER)
    session.commit()

    rendered = reg.render(alert, "SMS")
    assert "TT notes" in rendered.body
    assert rendered.template_version == "2"
    # The old version is still resolvable by number: that is the whole point of versioning.
    assert "ticket notes" in reg.render(alert, "SMS", version=1).body


def test_pinning_the_next_version_in_yaml_keeps_the_declared_approval(registry):
    """The intended workflow: a human edits the body AND bumps ``version:`` together."""
    settings, session, reg = registry
    seeds, _ = load_seed_templates(settings.operator.operator_id)
    seed = _sms_seed(seeds)

    reg.sync([replace(seed, body=seed.body + "\nReviewed wording", version=2)])
    session.commit()

    new = reg.get("SMS", V1_TEMPLATE_KEY, "en", 2)
    assert new.approval_status == "APPROVED" and new.approved_by == seed.approved_by
    assert reg.latest_approved("SMS", V1_TEMPLATE_KEY, "en").version == 2


def test_a_reverted_body_becomes_its_own_version(registry):
    """Content identical to an OLD version is still a new decision, so it gets a new number."""
    settings, session, reg = registry
    seeds, _ = load_seed_templates(settings.operator.operator_id)
    seed = _sms_seed(seeds)
    reg.sync([replace(seed, body=seed.body + "\ntemporary")])
    session.commit()

    reg.sync([seed])  # reverted to the @1 wording
    session.commit()

    versions = reg.versions("SMS", V1_TEMPLATE_KEY, "en")
    assert [v.version for v in versions] == [1, 2, 3]
    assert versions[2].body == versions[0].body
    assert versions[2].approval_status == "DRAFT"  # the revert needs its own approval
    # ...and it does not then grow without bound: the head now matches, so sync settles.
    assert reg.sync([seed]).inserted == []


def test_sync_never_changes_the_approval_of_an_existing_row(registry):
    """An approval made in the database survives a redeploy of the config.

    The YAML approval block is the status a row is CREATED with, nothing more; transitions
    go through ``set_status``. Without this rule a config redeploy would silently revoke an
    approval a named human made.
    """
    settings, session, reg = registry
    draft = reg.get("SMS", "incident_update", "en", 1)
    assert draft.approval_status == "DRAFT"  # what the YAML seeds it as

    reg.set_status(draft, "APPROVED", actor=APPROVER)
    session.commit()

    report = reg.sync()
    session.commit()

    assert report.inserted == []
    still = reg.get("SMS", "incident_update", "en", 1)
    assert still.approval_status == "APPROVED" and still.approved_by == APPROVER


def test_fingerprint_ignores_approval_and_timestamps(registry):
    """Approving a template does not make it a different template."""
    settings, session, reg = registry
    row = reg.get("SMS", "incident_update", "en", 1)
    before = row_fingerprint(row)

    reg.set_status(row, "APPROVED", actor=APPROVER)
    session.commit()

    assert row_fingerprint(reg.get("SMS", "incident_update", "en", 1)) == before
    assert content_fingerprint("a", None, "{}") != content_fingerprint("b", None, "{}")


# ======================================================================================
# 3. an unapproved template cannot be used for a send
# ======================================================================================


def test_for_send_refuses_a_draft_template(registry):
    settings, session, reg = registry
    assert reg.get("SMS", "incident_update", "en", 1).approval_status == "DRAFT"

    with pytest.raises(TemplateNotApproved) as exc:
        reg.for_send("SMS", "incident_update")

    assert "DRAFT" in str(exc.value) and "refusing to send" in str(exc.value)


@pytest.mark.parametrize("status", [s for s in APPROVAL_STATUSES if s not in SENDABLE_STATUSES])
def test_only_approved_is_sendable(registry, status):
    """DRAFT, SUBMITTED, REJECTED and PAUSED are all equally unsendable."""
    settings, session, reg = registry
    row = reg.get("SMS", V1_TEMPLATE_KEY, "en", 1)
    reg.set_status(row, status)
    session.commit()

    with pytest.raises(TemplateNotApproved):
        reg.for_send("SMS", V1_TEMPLATE_KEY)
    with pytest.raises(TemplateNotApproved):
        reg.render(build_alert(_row(), settings.operator), "SMS")


def test_render_refuses_a_draft_but_can_preview_it_explicitly(registry):
    """A HITL card shows an approver what they are being asked to approve; a send may not."""
    settings, session, reg = registry
    alert = build_alert(_row(), settings.operator)

    with pytest.raises(TemplateNotApproved):
        reg.render(alert, "SMS", template_key="incident_update")

    preview = reg.render(alert, "SMS", template_key="incident_update", allow_unapproved=True)
    assert preview.approval_status == "DRAFT"
    assert preview.sendable is False
    assert preview.body  # the draft is still shown, so the human can judge it


def test_a_missing_template_is_not_found_rather_than_silently_empty(registry):
    settings, session, reg = registry
    with pytest.raises(TemplateNotFound):
        reg.for_send("SMS", "regulatory_ca_24h")


def test_pausing_the_only_approved_version_stops_the_send(registry):
    """PAUSED mirrors Meta's state and must behave like a stop, not a warning."""
    settings, session, reg = registry
    reg.set_status(reg.get("EMAIL", V1_TEMPLATE_KEY, "en", 1), "PAUSED")
    session.commit()

    with pytest.raises(TemplateNotApproved):
        reg.for_send("EMAIL", V1_TEMPLATE_KEY)


def test_set_status_refuses_an_anonymous_approval(registry):
    settings, session, reg = registry
    row = reg.get("SMS", "incident_update", "en", 1)
    with pytest.raises(Exception, match="named approver"):
        reg.set_status(row, "APPROVED", actor="  ")
    assert reg.get("SMS", "incident_update", "en", 1).approval_status == "DRAFT"


def test_set_status_keeps_the_approval_history_when_it_pauses(registry):
    settings, session, reg = registry
    row = reg.set_status(reg.get("SMS", "incident_update", "en", 1), "APPROVED", actor=APPROVER)
    reg.set_status(row, "PAUSED")
    session.commit()

    paused = reg.get("SMS", "incident_update", "en", 1)
    assert paused.approval_status == "PAUSED"
    assert paused.approved_by == APPROVER  # who once approved it is history, not erased


# ======================================================================================
# 4. every seeded template has an English body
# ======================================================================================


def test_every_seeded_template_has_an_english_body(registry):
    settings, session, reg = registry
    rows = reg.all_rows()
    assert rows

    by_channel_key: dict[tuple[str, str], set[str]] = {}
    for row in rows:
        by_channel_key.setdefault((row.channel, row.template_key), set()).add(row.language)
    for (channel, key), languages in by_channel_key.items():
        assert DEFAULT_LANGUAGE in languages, f"{channel}/{key} has no English body ({sorted(languages)})"

    for row in rows:
        assert row.body.strip(), f"{row.channel}/{row.template_key}/{row.language} has an empty body"
        assert row.template_key in TEMPLATE_KEYS
        assert (row.subject is not None) == (row.channel == "EMAIL")


def test_the_seed_refuses_a_template_with_no_english(tmp_path):
    (tmp_path / "chase_reminder.yaml").write_text(
        "template_key: chase_reminder\n"
        "channels:\n"
        "  SMS:\n"
        "    params: {incident_number: string}\n"
        "    languages:\n"
        "      sw:\n"
        "        body: '{{ incident_number }}'\n",
        encoding="utf-8",
    )
    with pytest.raises(TemplateSeedError, match="English is mandatory"):
        load_seed_templates(roots=[tmp_path])


# ======================================================================================
# The reason the registry exists: byte fidelity with the wording that went out
# ======================================================================================

EM_DASH = "—"


def _assert_bytes_equal(got: str, want: str, what: str) -> None:
    assert got.encode("utf-8") == want.encode("utf-8"), f"{what} differs\n--- registry ---\n{got!r}\n--- composition.py ---\n{want!r}"


def _assert_one_em_dash_behind(recorded: str, live: str, what: str) -> None:
    """``recorded`` (the ``@1`` SMS row) == ``live`` (``compose_sms``) except for ONE character:
    the em dash in the ``Owner:`` tail, which the owner approved removing from the live path
    and which ``@1`` keeps as the record of what went out.

    Deliberately not ``assert recorded != live`` and stop. A second em dash, a moved space, a
    reworded line, or the title em dash coming back on BOTH sides all fail here, so the
    comparison keeps catching an unintended divergence instead of merely tolerating the
    intended one.
    """
    diff = f"\n--- registry @1 ---\n{recorded!r}\n--- composition.py (live) ---\n{live!r}"
    assert recorded != live, f"{what}: @1 matches the live message again -- the live em-dash fix was reverted, or @1 was edited{diff}"
    assert len(recorded) == len(live), f"{what}: @1 and the live message differ by more than one character{diff}"
    differing = [(i, a, b) for i, (a, b) in enumerate(zip(recorded, live)) if a != b]
    assert [(a, b) for _, a, b in differing] == [(EM_DASH, "-")], f"{what}: @1 differs from the live message in more than the em dash: {differing!r}{diff}"
    # ...in exactly one place: the Owner: tail on the last line. The first three lines
    # (priority/INC/site, domain/users, the headline) are byte-identical.
    recorded_head, recorded_tail = recorded.rsplit("\n", 1)
    live_head, live_tail = live.rsplit("\n", 1)
    assert recorded_head.encode("utf-8") == live_head.encode("utf-8"), f"{what}: the lines above Owner: differ{diff}"
    assert recorded_tail.startswith("Owner:") and live_tail.startswith("Owner:")
    assert recorded.count(EM_DASH) == 1 and EM_DASH in recorded_tail, f"{what}: @1's one em dash is not in the Owner: tail{diff}"
    assert EM_DASH not in live, f"{what}: an em dash is back on the live SMS path{diff}"
    assert recorded.replace(EM_DASH, "-", 1).encode("utf-8") == live.encode("utf-8")


@pytest.mark.parametrize("event", [HUB_EVENT, BTS_EVENT], ids=["p2_hub", "p4_bts"])
def test_site_down_alert_v1_renders_the_recorded_wording_one_em_dash_behind_the_live_sms(registry, clean_hub, event):
    """``site_down_alert@1`` served from the TABLE vs ``compose_sms``/``compose_email``.

    This test used to assert both channels byte-identical, under the name
    ``test_site_down_alert_v1_renders_todays_wording_byte_for_byte``. Then the owner approved
    removing the em dash from the live SMS path (services/composition.py, services/render/sms.py
    and the incident title in agents/ticket.py), and ``@1`` was deliberately NOT edited to
    follow: it is the recorded history of what went out, and a versioned registry answers a
    wording change with a NEW version, never by rewriting an approved one.

    So the honest statement today: the EMAIL row is still byte-identical; the SMS row differs
    from the live message by exactly one character, in exactly one place. The requirement that
    the registry gain an approved version with the new wording BEFORE it ever drives rendering
    is pinned in ``tests/unit/test_template_v2.py::test_v1_has_been_superseded_by_the_live_code_and_the_registry_must_catch_up``.
    """
    settings, session, reg = registry
    inc = process_event(session, settings, EventIngest(**event))
    session.refresh(inc)
    alert = build_alert(inc, settings.operator)

    sms = reg.render(alert, "SMS")
    email = reg.render(alert, "EMAIL")

    _assert_one_em_dash_behind(sms.body, compose_sms(inc), "SMS")
    _assert_bytes_equal(f"Subject: {email.subject}\n\n{email.body}", compose_email(inc, settings.operator), "email")

    # ...and the message records which version produced it (§6.3 provenance).
    assert sms.template_key == email.template_key == V1_TEMPLATE_KEY
    assert sms.template_version == email.template_version == V1_TEMPLATE_VERSION
    assert sms.language == email.language == "en" and sms.language_fallback is None
    assert sms.sendable and email.sendable


def test_the_version_the_envelope_declares_still_resolves_after_a_change(registry, clean_hub):
    """An outbox row stamped ``@1`` renders ``@1`` even once ``@2`` is the approved head."""
    settings, session, reg = registry
    inc = process_event(session, settings, EventIngest(**HUB_EVENT))
    session.refresh(inc)
    alert = build_alert(inc, settings.operator)
    was = reg.render(alert, "SMS").body

    seeds, _ = load_seed_templates(settings.operator.operator_id)
    seed = _sms_seed(seeds)
    reg.sync([replace(seed, body=seed.body.replace("Owner:", "Owner: "))])
    reg.set_status(reg.get("SMS", V1_TEMPLATE_KEY, "en", 2), "APPROVED", actor=APPROVER)
    session.commit()

    assert reg.render(alert, "SMS").template_version == "2"
    pinned = reg.render(alert, "SMS", version=int(alert.governance.template_version))
    assert pinned.template_version == "1"
    _assert_bytes_equal(pinned.body, was, "pinned @1 SMS")
    # ...and what came back IS @1, not the live composer's wording relabelled: still exactly
    # one em dash behind services/composition.py, nothing else moved.
    _assert_one_em_dash_behind(pinned.body, compose_sms(inc), "pinned @1 SMS vs composition.py")


def test_update_and_restore_render_from_the_registry(registry):
    """The two drafted templates render cleanly — they are just not sendable yet."""
    settings, session, reg = registry
    inc = _row(
        status="RESTORED",
        restored_at=datetime(2026, 9, 16, 12, 5, 0),
        # A GSM-7 title, so the encoding assertions below measure the TEMPLATE. Incident
        # content can always drag a body into UCS-2 (a smart quote in a site name, say); what
        # is being asserted here is that these two templates contribute nothing themselves.
        title="[POWER_GRID] HUB POWER - Embakasi East Aggregation HUB (Nairobi East)",
    )
    alert = build_alert(inc, settings.operator, sequence=3)

    update = reg.render(alert, "SMS", template_key="incident_update", allow_unapproved=True)
    assert "UPDATE 3" in update.body and update.template_version == "1"

    restored = reg.render(alert, "EMAIL", template_key="incident_restored", allow_unapproved=True)
    assert "RESTORED" in (restored.subject or "")
    assert "15:05 EAT" in restored.body  # 12:05 UTC in Nairobi, via services/clock.fmt_eat
    assert restored.body.endswith("\n")

    # Both were drafted GSM-7 on purpose, unlike the v1 alert's em dash.
    assert is_gsm7(update.body)
    assert is_gsm7(reg.render(alert, "SMS", template_key="incident_restored", allow_unapproved=True).body)


# ======================================================================================
# §6.4 language handling: the sw slot exists, is empty on purpose, and falls back
# ======================================================================================


def test_no_kiswahili_row_is_seeded_and_every_gap_is_explained(registry):
    """No ``sw`` body is invented; each missing one is recorded as data with a reason."""
    settings, session, reg = registry
    assert [r for r in reg.all_rows() if r.language != "en"] == []

    _, pending = load_seed_templates(settings.operator.operator_id)
    gaps = {(p.channel, p.template_key) for p in pending}
    seeded = {(r.channel, r.template_key) for r in reg.all_rows()}
    assert gaps == seeded, "every seeded template must say where its Kiswahili is"
    for note in pending:
        assert note.language in REVIEWED_LANGUAGES
        assert len(note.reason) > 40, f"{note} has no real explanation"


def test_a_kiswahili_request_falls_back_to_english_and_says_so(registry):
    """§6.4: a missing or unapproved ``sw`` falls back to ``en`` and records the fallback."""
    settings, session, reg = registry
    alert = build_alert(_row(), settings.operator)

    rendered = reg.render(alert, "SMS", language="sw")
    english = reg.render(alert, "SMS")  # the row the request falls back to (DEFAULT_LANGUAGE)

    assert rendered.language == "en"
    assert rendered.language_fallback == "en"
    assert english.language == "en" and english.language_fallback is None
    # The fallback serves the English ROW, byte for byte -- the subject here is §6.4 language
    # handling, not the wording, so the reference is the registry's own English rendering
    # rather than compose_sms (which the SMS row is now one em dash behind; see above).
    _assert_bytes_equal(rendered.body, english.body, "sw-requested SMS vs the English row it fell back to")
    assert rendered.template_key == english.template_key == V1_TEMPLATE_KEY
    assert rendered.template_version == english.template_version == V1_TEMPLATE_VERSION

    resolution = reg.resolve("SMS", V1_TEMPLATE_KEY, "sw")
    assert resolution.ok and resolution.requested_language == "sw" and resolution.language == "en"


def test_english_has_no_fallback_of_its_own(registry):
    settings, session, reg = registry
    reg.set_status(reg.get("SMS", V1_TEMPLATE_KEY, "en", 1), "REJECTED")
    session.commit()

    resolution = reg.resolve("SMS", V1_TEMPLATE_KEY, "sw")
    assert not resolution.ok and "REJECTED" in (resolution.reason or "")


def test_the_seed_refuses_an_approved_kiswahili_row_without_a_named_reviewer(tmp_path):
    """§6.4 hard rule, made executable: sw cannot reach APPROVED without a human.

    Three separate refusals, because all three facts have to be on the record before a
    Kiswahili message may reach a real recipient: who approved it, that their role is one
    that may, and where the sign-off is written down.
    """
    body = "template_key: chase_reminder\nchannels:\n  SMS:\n    params: {incident_number: string}\n    languages:\n      en:\n        body: '{{ incident_number }}'\n      sw:\n        body: '{{ incident_number }}'\n        approval:\n          status: APPROVED\n"
    path = tmp_path / "chase_reminder.yaml"

    path.write_text(body, encoding="utf-8")
    with pytest.raises(TemplateSeedError, match="APPROVED with no approved_by"):
        load_seed_templates(roots=[tmp_path])

    path.write_text(body + "          approved_by: 'someone'\n", encoding="utf-8")
    with pytest.raises(TemplateSeedError, match="reviewed language"):
        load_seed_templates(roots=[tmp_path])

    path.write_text(body + "          approved_by: 'someone'\n          reviewer_role: legal\n", encoding="utf-8")
    with pytest.raises(TemplateSeedError, match="signoff_ref"):
        load_seed_templates(roots=[tmp_path])

    path.write_text(
        body + "          approved_by: 'someone'\n          reviewer_role: legal\n          signoff_ref: 'docs/SIGNOFF.md#sw-2026-09'\n",
        encoding="utf-8",
    )
    seeds, _ = load_seed_templates(roots=[tmp_path])
    sw = next(s for s in seeds if s.language == "sw")
    assert sw.approval_status == "APPROVED" and sw.approved_at is not None


def test_set_status_applies_the_same_kiswahili_rule_at_runtime(tmp_db, tmp_path):
    """The hard rule cannot be walked around by approving through the API instead."""
    settings, session = tmp_db
    (tmp_path / "chase_reminder.yaml").write_text(
        "template_key: chase_reminder\nchannels:\n  SMS:\n    params: {incident_number: string}\n"
        "    languages:\n      en:\n        body: '{{ incident_number }}'\n      sw:\n        body: '{{ incident_number }} SW'\n",
        encoding="utf-8",
    )
    seeds, pending = load_seed_templates(roots=[tmp_path])
    reg = TemplateRegistry.for_config(session, settings.operator)
    reg.sync(seeds, pending=pending)
    session.commit()
    sw = reg.get("SMS", "chase_reminder", "sw", 1)

    with pytest.raises(Exception, match="legal"):
        reg.set_status(sw, "APPROVED", actor=APPROVER)
    with pytest.raises(Exception, match="sign-off"):
        reg.set_status(sw, "APPROVED", actor=APPROVER, reviewer_role="management")

    reg.set_status(sw, "APPROVED", actor=APPROVER, reviewer_role="management", signoff_ref="docs/SIGNOFF.md#sw-2026-09")
    session.commit()
    assert reg.get("SMS", "chase_reminder", "sw", 1).approval_status == "APPROVED"
    # Now the fallback stops: a sw request gets the sw row.
    assert reg.resolve("SMS", "chase_reminder", "sw").language == "sw"


# ======================================================================================
# Seed-time contract: what the YAML may say, checked on a laptop and not at 02:14
# ======================================================================================


def test_declared_params_must_be_variables_the_envelope_can_supply(tmp_path):
    (tmp_path / "chase_reminder.yaml").write_text(
        "template_key: chase_reminder\nchannels:\n  SMS:\n    params: {operator_bank_account: string}\n"
        "    languages:\n      en:\n        body: '{{ operator_bank_account }}'\n",
        encoding="utf-8",
    )
    with pytest.raises(TemplateSeedError, match="the envelope cannot supply"):
        load_seed_templates(roots=[tmp_path])


def test_an_undeclared_variable_is_refused_at_seed_time_not_at_send_time(tmp_path):
    """``StrictUndefined`` would fail the render mid-incident; this fails on a laptop."""
    (tmp_path / "chase_reminder.yaml").write_text(
        "template_key: chase_reminder\nchannels:\n  SMS:\n    params: {incident_number: string}\n"
        "    languages:\n      en:\n        body: '{{ incident_number }} {{ site_id }}'\n",
        encoding="utf-8",
    )
    with pytest.raises(TemplateSeedError, match="undeclared variable"):
        load_seed_templates(roots=[tmp_path])


def test_an_unused_declared_param_is_refused(tmp_path):
    (tmp_path / "chase_reminder.yaml").write_text(
        "template_key: chase_reminder\nchannels:\n  SMS:\n    params: {incident_number: string, site_id: string}\n"
        "    languages:\n      en:\n        body: '{{ incident_number }}'\n",
        encoding="utf-8",
    )
    with pytest.raises(TemplateSeedError, match="never uses"):
        load_seed_templates(roots=[tmp_path])


def test_an_sms_that_silently_leaves_gsm7_is_refused(tmp_path):
    """One pasted smart quote roughly triples the segment cost of every future send."""
    (tmp_path / "chase_reminder.yaml").write_text(
        "template_key: chase_reminder\nchannels:\n  SMS:\n    params: {incident_number: string}\n"
        "    languages:\n      en:\n        body: \"{{ incident_number }} — chase\"\n",
        encoding="utf-8",
    )
    with pytest.raises(TemplateSeedError, match="forces UCS-2"):
        load_seed_templates(roots=[tmp_path])

    (tmp_path / "chase_reminder.yaml").write_text(
        "template_key: chase_reminder\nchannels:\n  SMS:\n    encoding: UCS2\n    params: {incident_number: string}\n"
        "    languages:\n      en:\n        body: \"{{ incident_number }} — chase\"\n",
        encoding="utf-8",
    )
    assert load_seed_templates(roots=[tmp_path])[0], "declaring UCS2 accepts the cost deliberately"


def test_the_shipped_site_down_alert_is_the_one_ucs2_template(registry):
    """v1's em dash is the known, documented exception, not a pattern."""
    settings, session, reg = registry
    seeds, _ = load_seed_templates(settings.operator.operator_id)
    ucs2 = {s.label for s in seeds if s.channel == "SMS" and s.encoding == "UCS2"}
    assert ucs2 == {"SMS/site_down_alert/en@1"}


@pytest.mark.parametrize(
    ("yaml_text", "match"),
    [
        ("template_key: not_a_key\nchannels: {}\n", "not one of"),
        ("template_key: handover\nchannels:\n  PIGEON:\n    params: {}\n", "not one of"),
        ("template_key: handover\nchannels:\n  EMAIL:\n    params: {incident_number: string}\n    languages:\n      en:\n        body: '{{ incident_number }}'\n", "needs a subject"),
        ("template_key: handover\nchannels:\n  SMS:\n    params: {incident_number: string}\n    languages:\n      en:\n        body: '{{ incident_number }}'\n        subject: 'x'\n", "must not declare a subject"),
        ("template_key: handover\nchannels:\n  SMS:\n    params: {incident_number: string}\n    languages:\n      en:\n        body: '{{ incident_number }}'\n    translations_pending:\n      sw: ''\n", "no reason"),
        ("template_key: handover\nchannels:\n  SMS:\n    params: {incident_number: string}\n    languages:\n      en:\n        body: '{{ incident_number }}'\n        approval:\n          status: DRAFT\n          approved_by: 'ghost'\n", "only an APPROVED template"),
    ],
    ids=["bad_key", "bad_channel", "email_needs_subject", "sms_has_no_subject", "unexplained_gap", "draft_with_approver"],
)
def test_the_seed_refuses_malformed_declarations(tmp_path, yaml_text, match):
    (tmp_path / "handover.yaml").write_text(yaml_text, encoding="utf-8")
    with pytest.raises(TemplateSeedError, match=match):
        load_seed_templates(roots=[tmp_path])


# ======================================================================================
# The render context: exactly what the envelope can supply, nothing more
# ======================================================================================


def test_the_context_keys_are_exactly_the_allowed_params(tmp_db):
    settings, _ = tmp_db
    context = context_from_alert(build_alert(_row(), settings.operator))
    assert set(context) == set(ALLOWED_PARAMS)
    assert len(ALLOWED_PARAMS) == len(set(ALLOWED_PARAMS)), "ALLOWED_PARAMS has a duplicate"


def test_the_context_reads_only_the_envelope(tmp_db):
    """Same envelope in, same context out — no session, no config, no IncidentRow."""
    settings, _ = tmp_db
    alert = build_alert(_row(), settings.operator, sent=datetime(2026, 9, 16, 10, 47, 10), alert_id="a-1")
    assert context_from_alert(alert) == context_from_alert(alert.model_copy(deep=True))


def test_a_missing_variable_fails_the_render_rather_than_blanking_it(registry):
    """§6.3 ``StrictUndefined``: never a blank in a message about a national outage."""
    settings, session, reg = registry
    with pytest.raises(TemplateRenderError, match="variable"):
        from noc_agents.services.templates import render_body

        render_body("{{ incident_number }} {{ nothing_supplies_this }}", {"incident_number": "INC000123"})


def test_the_stored_params_schema_is_real_json_schema(registry):
    import json

    settings, session, reg = registry
    for row in reg.all_rows():
        schema = json.loads(row.params_schema_json)
        assert schema["type"] == "object" and schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
        # The envelope vocabulary for every §6.3 key; the card vocabulary for §6.5's hitl_nudge,
        # whose variables are deliberately not envelope fields (templates.params_vocabulary).
        assert set(schema["properties"]) <= set(params_vocabulary(row.template_key))
