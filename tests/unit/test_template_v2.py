"""The `@2` message-template drafts: GSM-7 clean, §6.2-complete, and NOT approved.

`config/templates/*.yaml` carries a `version_2_draft` block next to each channel block it
proposes to replace. Those drafts are what unblocks `ALERT_ENVELOPE_V2`, and this file is the
evidence for every claim made about them in `docs/TEMPLATE_V2_REVIEW.md`. Nothing here is
asserted from a comment: the alphabet comes from `services/gsm7.py`, the §6.2 verdicts from
`services/validators.py`, and the approval state from a real `TemplateRegistry`.

Four properties, one section each:

1. **every `@2` SMS body is GSM-7** — the literal template text by `gsm7.is_gsm7`, and a
   fully rendered body by `sms_cost`, which is also what turns 3 UCS-2 segments into 1. The
   channels that propose **no** `@2` are measured too: their `@1` text is already GSM-7, which
   is *why* no version is proposed, and if that ever stops being true this file fails;
2. **every `@2` email body carries §6.2's four tokens** — incident number, priority, region
   label and the next-update EAT string — on a P1 HUB and a P4 site, checked by
   `validate_email`. `@1` is checked on the same data and fails, so the fix is demonstrated,
   not assumed;
3. **`@1` is untouched and still APPROVED** — the em dash is still in the seeded `site_down_alert`
   SMS body (it is a byte contract, not a bug to tidy), the row is still `APPROVED` by
   `policy:v1_fidelity`, and syncing the drafts alongside it writes a new row instead of
   editing it;
4. **`@2` is NOT approved** — no draft block names an approver, the drafts seed as `DRAFT`,
   and `for_send(..., version=2)` refuses. Decision D3: a named human approves customer-facing
   telecom wording, and a shadow shift runs before the flag flips.

Plus D4 (English only): no draft introduces a `sw` body, and every file keeps its header.

Scope: this file reads `config/templates/*.yaml` and drives `TemplateRegistry` on the `tmp_db`
fixture. It does not touch the golden sequence, the contract file, `main.py`, `agents/` or
`frontend/`.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
import yaml

from noc_agents.db.models import IncidentRow
from noc_agents.services.alerts import V1_SMS_MAX_SEGMENTS, build_alert
from noc_agents.services.composition import compose_sms
from noc_agents.services.gsm7 import is_gsm7, non_gsm7_chars, sms_cost
from noc_agents.services.templates import (
    HITL_NUDGE_TEMPLATE_KEY,
    SENDABLE_STATUSES,
    TemplateNotApproved,
    TemplateRegistry,
    _load_channel,  # the loader's own validation, run against a draft block on purpose
    _static_text,
    context_from_alert,
    render_body,
)
from noc_agents.services.validators import validate_email, validate_sms

TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "config" / "templates"
DRAFT_KEY = "version_2_draft"

#: §6.2's four email body tokens, as `validate_email` names the violations.
EMAIL_BODY_CODES = (
    "email_missing_incident_number",
    "email_missing_priority",
    "email_missing_region_label",
    "email_missing_next_update",
)


# ======================================================================================
# reading the YAML
# ======================================================================================


def _files() -> list[Path]:
    paths = sorted(TEMPLATE_DIR.glob("*.yaml"))
    assert paths, f"no template YAML under {TEMPLATE_DIR}"
    return paths


def _docs() -> dict[str, dict]:
    return {p.stem: yaml.safe_load(p.read_text(encoding="utf-8")) for p in _files()}


def _channels(kind: str) -> list[tuple[str, str, dict]]:
    """``(template_key, channel, block)`` for every channel block of ``kind`` (SMS/EMAIL)."""
    return [
        (stem, channel, block)
        for stem, doc in sorted(_docs().items())
        for channel, block in sorted(doc["channels"].items())
        if channel == kind
    ]


def _drafts(kind: str) -> list[tuple[str, str, dict]]:
    return [(k, c, b[DRAFT_KEY]) for k, c, b in _channels(kind) if DRAFT_KEY in b]


def _en(block: dict) -> dict:
    return block["languages"]["en"]


def _ids(rows: list[tuple[str, str, dict]]) -> list[str]:
    return [f"{k}/{c}" for k, c, _ in rows]


SMS_DRAFTS = _drafts("SMS")
EMAIL_DRAFTS = _drafts("EMAIL")
SMS_CHANNELS = _channels("SMS")


def test_there_is_at_least_one_draft_to_review():
    """A guard on the guards: if the drafts are ever deleted, this file must not pass silently."""
    assert SMS_DRAFTS or EMAIL_DRAFTS, "no version_2_draft block found; there is nothing to review"
    assert EMAIL_DRAFTS, "§6.2's email body rule is the reason the flag is blocked on every email"


# ======================================================================================
# the incidents everything is measured on
# ======================================================================================

# The em dash that forces UCS-2 reaches the SMS from TWO places: this template's own
# `Owner:… — ticket notes` (which `@2` fixes) and `agents/ticket.py`, which builds every
# incident title with one (which `@2` does not and cannot fix — a different file, a different
# change). Measuring `@2` on a title that still carries the second one would measure the
# title, not the template, so the fixture rows below use a hyphen in `title` and
# `test_a_dirty_title_still_defeats_v2` pins the part that is still broken.
P1_TITLE = "HUB POWER - Westlands Hub (Nairobi East)"
P4_TITLE = "ENV ACCESS - Naivasha Kabati 2 (Rift)"
TICKET_PY_TITLE = "HUB POWER — Westlands Hub (Nairobi East)"  # what agents/ticket.py builds today


def _row(**overrides) -> IncidentRow:
    base = dict(
        id="inc-v2-1",
        operator_id="safaricom",
        incident_number="INC000123",
        status="ASSIGNED",
        priority="P1",
        users_affected=620000,
        service_affecting=True,
        site_id="NBIE-HUB-01",
        site_name="Westlands Hub",
        site_type="HUB",
        region_code="NBI_E",
        county="Nairobi",
        title=P1_TITLE,
        narrative=(
            "Westlands Hub (NBIE-HUB-01) lost mains power at 13:41 EAT; the generator failed to "
            "start. About 620,000 subscribers affected; M-PESA corridor at risk. EGYPRO power "
            "desk dispatched."
        ),
        root_cause_hypothesis="Mains failure; genset starter battery flat. Awaiting MSP confirmation.",
        assignee_type="MSP",
        assignee_name="EGYPRO",
        msp_name="EGYPRO",
        fe_name="FE-NBI-E-01",
        rnio_name="RNIO-NBI-E",
        access_notes=None,
        correlation_fingerprint="NBIE-HUB-01|MAINS_FAIL|POWER",
        mpesa_risk=True,
        failure_domain="POWER",
        alarm_code="POWER_GRID_FAIL",
        tt_category="POWER_GRID",
        child_sites_down=0,
        child_site_ids_json="[]",
        radio_oem="Huawei",
        responsible_msp=None,
        next_update_at=datetime(2026, 9, 16, 11, 2, 0),
        outage_start_at=datetime(2026, 9, 16, 10, 41, 0),
        failure_time=None,
        restored_at=None,
        msp_root_cause=None,
    )
    base.update(overrides)
    row = IncidentRow(**base)
    row.services_impacted = ["VOICE", "DATA", "SMS"]
    return row


P4_OVERRIDES = dict(
    id="inc-v2-4",
    incident_number="INC000124",
    priority="P4",
    users_affected=1200,
    service_affecting=False,
    site_id="RFT-CEL-0417",
    site_name="Naivasha Kabati 2",
    site_type="BTS",
    region_code="RFT",
    county="Nakuru",
    title=P4_TITLE,
    narrative="Rack door open alarm at Naivasha Kabati 2 (RFT-CEL-0417); site remains on air.",
    root_cause_hypothesis="Door contact or physical access; site on air throughout.",
    assignee_name="Rift FE on-call",
    msp_name="TETRANET",
    mpesa_risk=False,
    failure_domain="ENVIRONMENT",
    alarm_code="DOOR_OPEN",
    tt_category="ENV_ACCESS",
    next_update_at=datetime(2026, 9, 16, 13, 5, 0),
    outage_start_at=datetime(2026, 9, 16, 10, 47, 0),
)

SENT_AT = datetime(2026, 9, 16, 10, 47, 10)


@pytest.fixture()
def cfg(tmp_db):
    settings, _session = tmp_db
    return settings.operator


def _alert(cfg, row: IncidentRow):
    return build_alert(row, cfg, sent=SENT_AT, alert_id="6f1c0000")


def _render(block: dict, alert) -> tuple[str | None, str]:
    """``(subject, body)`` for one channel block, through the registry's own Jinja env."""
    lang = _en(block)
    context = context_from_alert(alert, "en")
    subject = render_body(lang["subject"], context) if lang.get("subject") else None
    return subject, render_body(lang["body"], context)


def _cases(cfg):
    """``(label, incident_row, alert)`` for the P1 HUB and the P4 site."""
    p1 = _row()
    p4 = _row(**P4_OVERRIDES)
    p4.services_impacted = ["DATA"]
    return [("P1 HUB", p1, _alert(cfg, p1)), ("P4 site", p4, _alert(cfg, p4))]


# ======================================================================================
# 1. every @2 SMS body is GSM-7 — measured by services/gsm7.py
# ======================================================================================


@pytest.mark.parametrize(("key", "channel", "draft"), SMS_DRAFTS, ids=_ids(SMS_DRAFTS))
def test_v2_sms_literal_text_is_gsm7(key, channel, draft):
    """The characters an author actually typed. `sms_cost` cannot save a template from these."""
    body = _en(draft)["body"]
    static = "".join(_static_text(body))
    offenders = non_gsm7_chars(static)
    assert not offenders, (
        f"{key}/{channel}@2 literal text leaves GSM-7: "
        + "; ".join(o.describe() for o in offenders)
    )
    assert is_gsm7(static)


@pytest.mark.parametrize(("key", "channel", "draft"), SMS_DRAFTS, ids=_ids(SMS_DRAFTS))
def test_v2_sms_declares_gsm7_and_the_loader_agrees(key, channel, draft, tmp_path):
    """`encoding: GSM7` is a claim the seeder checks — run the real check against the draft.

    `_load_channel` is the function `sync` uses. Feeding it the draft block proves the block is
    a *valid channel block* (declared params exist in the envelope, every variable is declared,
    the literal text matches the declared encoding, the approval block is well formed), i.e.
    that promoting it is the copy the YAML says it is and not a seed error waiting to happen.
    """
    assert draft["encoding"] == "GSM7", "the whole point of @2 is that it no longer buys UCS-2"
    seeds = _load_channel(tmp_path / f"{key}.yaml", key, channel, draft)
    assert [s.version for s in seeds] == [2]
    assert [s.language for s in seeds] == ["en"]  # D4: English only
    assert seeds[0].encoding == "GSM7"


@pytest.mark.parametrize(("key", "channel", "draft"), SMS_DRAFTS, ids=_ids(SMS_DRAFTS))
def test_v2_sms_renders_gsm7_in_one_segment_where_v1_needs_three(cfg, key, channel, draft):
    """The measurement the review doc quotes: 3 UCS-2 segments -> 1 GSM-7 segment.

    Same incident, same characters, one substitution. The cost is the SMSC's arithmetic, not a
    style preference: 3 segments is 3x the bill and 3 chances to lose a part on a congested
    network.
    """
    live = {k: b for k, c, b in _channels(channel)}[key]
    for label, row, alert in _cases(cfg):
        _, v1 = _render(live, alert)
        _, v2 = _render(draft, alert)

        before, after = sms_cost(v1), sms_cost(v2)
        assert before.encoding == "UCS2", f"{label}: @1 was expected to be the UCS-2 one"
        assert after.encoding == "GSM7", (
            f"{label}: {key}@2 still leaves GSM-7: " + "; ".join(o.describe() for o in after.offenders)
        )
        assert after.segments == 1, f"{label}: @2 needs {after.segments} segments"
        assert after.segments < before.segments, f"{label}: @2 did not reduce the segment count"
        assert after.remaining >= 0

        # One character changed, and it is the em dash.
        assert len(v1) == len(v2)
        differing = [(a, b) for a, b in zip(v1, v2) if a != b]
        assert differing == [("—", "-")], f"{label}: @2 changed more than the em dash: {differing}"


@pytest.mark.parametrize(("key", "channel", "draft"), SMS_DRAFTS, ids=_ids(SMS_DRAFTS))
def test_v2_sms_passes_the_full_62_sms_validator_with_the_flag_on(cfg, key, channel, draft):
    """`enforce_encoding=True` is `ALERT_ENVELOPE_V2=true`. @1 is refused; @2 is clean."""
    live = {k: b for k, c, b in _channels(channel)}[key]
    for label, row, alert in _cases(cfg):
        _, v1 = _render(live, alert)
        _, v2 = _render(draft, alert)
        kwargs = dict(
            incident_number=row.incident_number,
            priority=row.priority,
            max_segments=V1_SMS_MAX_SEGMENTS,  # what build_alert puts on the envelope
            enforce_encoding=True,
        )
        before = [f.code for f in validate_sms(v1, **kwargs).findings]
        after = [f.code for f in validate_sms(v2, **kwargs).findings]
        assert "sms_not_gsm7" in before, f"{label}: @1 was expected to be refused today"
        assert after == [], f"{label}: @2 is still refused: {after}"


@pytest.mark.parametrize(
    ("key", "channel", "block"),
    [(k, c, b) for k, c, b in SMS_CHANNELS if DRAFT_KEY not in b],
    ids=[f"{k}/{c}" for k, c, b in SMS_CHANNELS if DRAFT_KEY not in b],
)
def test_sms_channels_without_a_v2_did_not_need_one(key, channel, block):
    """"No @2 here" is a measurement, not an opinion — so it is measured.

    `incident_update` and `incident_restored` were drafted with a hyphen and already carry the
    incident number and the priority token, so there is nothing for a version 2 to fix. If a
    smart quote is ever pasted into one of them, this test fails and the claim in the YAML
    comment stops being true at the same moment.
    """
    static = "".join(_static_text(_en(block)["body"]))
    offenders = non_gsm7_chars(static)
    assert not offenders, (
        f"{key}/{channel}@1 has left GSM-7 and now DOES need a @2: "
        + "; ".join(o.describe() for o in offenders)
    )
    body = _en(block)["body"]
    if key == HITL_NUDGE_TEMPLATE_KEY:
        # §6.5's nudge is not an envelope message: its vocabulary is HITL_NUDGE_PARAMS, it may be
        # about a card with no incident at all, and `subject` already carries the incident number
        # when there is one. §6.2's incident-number rule is about alerts; the GSM-7 measurement
        # above is all the nudge owes this file.
        return
    assert "{{ incident_number }}" in body, "§6.2 requires the incident number in the SMS body"
    assert "{{ priority }}" in body, "§6.2 requires the priority token in the SMS body"


def test_a_dirty_title_still_defeats_v2(cfg):
    """Honesty test: approving `@2` alone does NOT make today's SMS GSM-7.

    `agents/ticket.py` builds every incident title with an em dash of its own, and the title is
    the SMS's third line. `@2` removes the *template's* contribution and nothing else. This is
    pinned so the review doc's "what this does not fix" paragraph cannot quietly go stale — and
    it will fail, loudly and usefully, on the day the title is fixed too.
    """
    key, channel, draft = SMS_DRAFTS[0]
    alert = _alert(cfg, _row(title=TICKET_PY_TITLE))
    _, body = _render(draft, alert)
    cost = sms_cost(body)
    assert cost.encoding == "UCS2"
    assert [o.codepoint for o in cost.offenders] == ["U+2014"]
    assert all(o.first_index < body.index("Owner:") for o in cost.offenders), (
        "the remaining em dash should be the title's, not the template's"
    )


# ======================================================================================
# 2. every @2 email body carries §6.2's four tokens
# ======================================================================================


@pytest.mark.parametrize(("key", "channel", "draft"), EMAIL_DRAFTS, ids=_ids(EMAIL_DRAFTS))
def test_v2_email_body_satisfies_the_62_body_token_rule(cfg, key, channel, draft):
    """§6.2: the BODY carries incident number, priority, region label and next update EAT.

    In the subject is not good enough and that is the point of the rule: phone clients truncate
    subjects, ticket-to-email gateways strip them, and an MSP forwarding the body into their own
    system keeps the body.
    """
    for label, row, alert in _cases(cfg):
        subject, body = _render(draft, alert)
        context = context_from_alert(alert, "en")
        result = validate_email(
            subject or "",
            body,
            incident_number=row.incident_number,
            priority=row.priority,
            region_label=alert.area.region_label,
            next_update=context["next_update_eat"],
        )
        missing = [f.code for f in result.findings if f.code in EMAIL_BODY_CODES]
        assert missing == [], f"{label}: {key}@2 email body still missing {missing}"
        assert [f.code for f in result.findings] == [], f"{label}: {key}@2 email refused: {result.findings}"


@pytest.mark.parametrize(("key", "channel", "draft"), EMAIL_DRAFTS, ids=_ids(EMAIL_DRAFTS))
def test_v1_email_body_is_the_thing_that_is_broken(cfg, key, channel, draft):
    """The before half of the measurement: `@1` fails the same check on the same data.

    Without this, `@2` passing proves only that the validator can be satisfied — not that it
    was not already.
    """
    live = {k: b for k, c, b in _channels(channel)}[key]
    for label, row, alert in _cases(cfg):
        subject, body = _render(live, alert)
        context = context_from_alert(alert, "en")
        codes = [
            f.code
            for f in validate_email(
                subject or "",
                body,
                incident_number=row.incident_number,
                priority=row.priority,
                region_label=alert.area.region_label,
                next_update=context["next_update_eat"],
            ).findings
        ]
        assert "email_missing_incident_number" in codes, (
            f"{label}: {key}@1 already carries the incident number in the body — @2's first "
            "reason for existing is gone and the review doc is wrong"
        )


def test_v2_email_adds_lines_and_removes_nothing(cfg):
    """`@2` is additive. Every line `@1` wrote is still there, in order."""
    for key, channel, draft in EMAIL_DRAFTS:
        live = {k: b for k, c, b in _channels(channel)}[key]
        for label, _row, alert in _cases(cfg):
            _, before = _render(live, alert)
            _, after = _render(draft, alert)
            kept = [line for line in before.splitlines() if line.strip()]
            remaining = after.splitlines()
            for line in kept:
                assert line in remaining, f"{label}: {key}@2 dropped @1's line {line!r}"
                remaining = remaining[remaining.index(line) + 1 :]  # order preserved too


# ======================================================================================
# 3. @1 is untouched and still APPROVED
# ======================================================================================


def test_site_down_alert_v1_still_carries_the_em_dash_and_declares_ucs2():
    """The byte contract. `@1` is not a bug to tidy; it is what went out, recorded."""
    sms = _docs()["site_down_alert"]["channels"]["SMS"]
    assert sms["version"] == 1
    assert sms["encoding"] == "UCS2"
    assert "—" in _en(sms)["body"], "@1's em dash was edited away; that is a v1 fidelity break"
    approval = _en(sms)["approval"]
    assert approval["status"] == "APPROVED"
    assert approval["approved_by"] == "policy:v1_fidelity"


def test_seeding_the_real_config_writes_only_version_1(tmp_db):
    """The drafts are invisible to the seeder, so today's table cannot change under this work."""
    settings, session = tmp_db
    registry = TemplateRegistry.for_config(session, settings.operator)
    registry.sync()
    session.commit()

    rows = registry.all_rows()
    assert rows, "the seeder wrote nothing"
    assert {r.version for r in rows} == {1}, (
        "a version 2 reached message_templates from the shared config; the drafts are supposed "
        "to be inert until a human promotes them"
    )
    live = registry.get("SMS", "site_down_alert", "en", 1)
    assert live is not None and live.approval_status == "APPROVED"
    assert live.approved_by == "policy:v1_fidelity"
    assert "—" in live.body


def test_v1_has_been_superseded_by_the_live_code_and_the_registry_must_catch_up(tmp_db):
    """`@1` no longer matches the live message, and that is a REQUIREMENT, not a defect.

    This test previously asserted the two were byte-identical. Then the owner approved
    removing the em dash from the live SMS path (services/composition.py and
    services/render/sms.py), taking the standard alert from 3 segments to 1. `@1` was
    deliberately NOT edited to follow: it is the recorded history of what actually went
    out, and a versioned registry answers a wording change with a NEW VERSION, never by
    rewriting an approved one.

    So the live code has moved ahead of the newest APPROVED template. That is safe today
    only because the registry is an approval gate and not yet the renderer: bodies are
    built in code (`services/render/sms.py::v1_sms_body`). The day the registry drives
    rendering, an approved version carrying the new wording must exist first -- otherwise
    every send either reverts to the em dash or suppresses.

    This test fails the moment someone closes that gap without adding the version, which
    is exactly when a loud failure is worth having.
    """
    settings, session = tmp_db
    registry = TemplateRegistry.for_config(session, settings.operator)
    registry.sync()
    session.commit()

    row = _row()
    alert = _alert(settings.operator, row)
    live = registry.get("SMS", "site_down_alert", "en", 1)
    assert live is not None
    rendered_v1 = render_body(live.body, context_from_alert(alert, "en"))
    live_message = compose_sms(row)

    assert rendered_v1 != live_message, (
        "@1 now matches the live message again -- either the live em-dash fix was reverted, "
        "or @1 was edited, which destroys its value as the record of what was actually sent"
    )
    # The divergence is exactly one character, in exactly one place.
    assert rendered_v1.replace("—", "-") == live_message
    assert "—" in rendered_v1 and "—" not in live_message


# ======================================================================================
# 4. @2 is NOT approved — a send with it still refuses
# ======================================================================================


@pytest.mark.parametrize(
    ("key", "channel", "draft"),
    SMS_DRAFTS + EMAIL_DRAFTS,
    ids=_ids(SMS_DRAFTS + EMAIL_DRAFTS),
)
def test_no_draft_names_an_approver(key, channel, draft):
    """D3: a named human approves customer-facing telecom wording — not an agent, not a policy string.

    `approved_by: "policy:…"` is how `@1` records "this is the wording that was already live";
    reusing it on new wording nobody has read would manufacture an audit trail.
    """
    approval = _en(draft)["approval"]
    assert approval["status"] == "DRAFT", f"{key}/{channel}@2 is {approval['status']}, not DRAFT"
    assert "approved_by" not in approval, f"{key}/{channel}@2 names an approver: {approval.get('approved_by')!r}"
    assert "approved_at" not in approval
    assert approval["status"] not in SENDABLE_STATUSES
    assert str(approval.get("note", "")).strip(), "a draft with no note is indistinguishable from an oversight"


def test_promoting_the_drafts_seeds_them_as_draft_beside_an_untouched_v1(tmp_db, tmp_path):
    """The rehearsal: sync the drafts into a real registry and read the consequences.

    This is what the approval command will face. `@1` keeps its id, body, status, approver and
    timestamps; `@2` arrives `DRAFT` with no approver; the send path still resolves to `@1`; and
    asking for `@2` by version is refused rather than quietly downgraded.
    """
    settings, session = tmp_db
    registry = TemplateRegistry.for_config(session, settings.operator)
    registry.sync()
    session.commit()
    before = {
        r.id: (r.version, r.body, r.subject, r.approval_status, r.approved_by, r.created_at, r.updated_at)
        for r in registry.all_rows()
    }

    seeds = []
    for key, channel, draft in SMS_DRAFTS + EMAIL_DRAFTS:
        seeds.extend(_load_channel(tmp_path / f"{key}.yaml", key, channel, draft))
    assert seeds, "no draft seeds were built"

    report = registry.sync(seeds, pending=[])
    session.commit()

    assert report.bumped == [], f"a draft collided with a stored version: {report.bumped}"
    assert report.inserted and all(entry.endswith("@2") for entry in report.inserted), report.inserted

    # @1 untouched, down to updated_at (the column has onupdate=utcnow, so any write shows).
    after = {
        r.id: (r.version, r.body, r.subject, r.approval_status, r.approved_by, r.created_at, r.updated_at)
        for r in registry.all_rows()
    }
    for row_id, snapshot in before.items():
        assert after[row_id] == snapshot, f"syncing the drafts modified the stored @{snapshot[0]} row"

    for key, channel, _draft in SMS_DRAFTS + EMAIL_DRAFTS:
        v2 = registry.get(channel, key, "en", 2)
        assert v2 is not None, f"{channel}/{key}@2 was not written"
        assert v2.approval_status == "DRAFT"
        assert v2.approved_by is None and v2.approved_at is None

        # The send path is unmoved: it still picks the highest APPROVED version, never the head.
        approved = registry.latest_approved(channel, key, "en")
        v1 = registry.get(channel, key, "en", 1)
        assert approved is None or approved.version == 1, (
            f"{channel}/{key}: a send would now use @{approved.version}"
        )
        assert registry.latest(channel, key, "en").version == 2  # @2 IS the head, and still not sendable
        if v1 is not None and v1.approval_status == "APPROVED":
            assert approved is not None and approved.id == v1.id

        with pytest.raises(TemplateNotApproved):
            registry.for_send(channel, key, "en", version=2)


def test_an_unapproved_v2_cannot_be_rendered_for_a_send(tmp_db, tmp_path):
    """`render` is the other door into the send path, and it is locked the same way.

    `allow_unapproved=True` opens it for a *preview* — the HITL card, the template admin — and
    the rendering that comes back still says `DRAFT`, so a preview can never be mistaken for a
    message that may leave.
    """
    settings, session = tmp_db
    registry = TemplateRegistry.for_config(session, settings.operator)
    registry.sync()
    key, channel, draft = SMS_DRAFTS[0]
    registry.sync(_load_channel(tmp_path / f"{key}.yaml", key, channel, draft), pending=[])
    session.commit()

    alert = _alert(settings.operator, _row())
    with pytest.raises(TemplateNotApproved):
        registry.render(alert, channel, template_key=key, version=2)

    preview = registry.render(alert, channel, template_key=key, version=2, allow_unapproved=True)
    assert preview.template_version == "2"
    assert preview.approval_status == "DRAFT"
    assert preview.sendable is False
    assert is_gsm7(preview.body)


# ======================================================================================
# 5. the caveat the review page leads with: approval alone changes no rendered byte
# ======================================================================================


def test_the_renderer_still_only_renders_v1_from_code():
    """`docs/TEMPLATE_V2_REVIEW.md` §7 tells the approver that approving `@2` sends nothing new.

    That is true because the table is an approval *gate* today: the words still come from
    `services/composition.py` via `render/sms.py:v1_sms_body`, and `is_v1_template` accepts
    exactly one template. Pinning it here means the day someone wires the renderers to the
    table, this test fails and the review page gets corrected instead of quietly misleading the
    next approver.
    """
    from noc_agents.services.render import is_v1_template

    assert is_v1_template("site_down_alert", "1") is True
    assert is_v1_template("site_down_alert", "2") is False, (
        "the renderer now accepts @2 — §7 of docs/TEMPLATE_V2_REVIEW.md is out of date, and so "
        "is its warning about the no_template trap"
    )
    for key, _channel, _draft in EMAIL_DRAFTS:
        assert is_v1_template(key, "2") is False


# ======================================================================================
# D4 — English only, and the header that says so
# ======================================================================================


@pytest.mark.parametrize("path", _files(), ids=lambda p: p.name)
def test_owner_decision_d4_header_is_preserved(path):
    head = path.read_text(encoding="utf-8").splitlines()[0]
    assert head.startswith("# OWNER DECISION D4"), f"{path.name} lost its D4 header: {head!r}"


@pytest.mark.parametrize(
    ("key", "channel", "draft"),
    SMS_DRAFTS + EMAIL_DRAFTS,
    ids=_ids(SMS_DRAFTS + EMAIL_DRAFTS),
)
def test_no_draft_introduces_a_kiswahili_body(key, channel, draft):
    """D4: Kiswahili is out of scope. A draft may not smuggle one in as "just a placeholder"."""
    assert set(draft["languages"]) == {"en"}, f"{key}/{channel}@2 declares {sorted(draft['languages'])}"
    assert DRAFT_KEY not in draft, "a draft inside a draft"


@pytest.mark.parametrize("path", _files(), ids=lambda p: p.name)
def test_translations_pending_blocks_and_their_reasons_survive(path):
    """The placeholders are permanent records of a decision, not a to-do list to tidy away."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    found = 0
    for channel, block in doc["channels"].items():
        for language, note in (block.get("translations_pending") or {}).items():
            found += 1
            assert language == "sw"
            reason = note.get("reason") if isinstance(note, dict) else note
            assert str(reason or "").strip(), f"{path.name}: {channel}/{language} placeholder lost its reason"
            assert "body" not in (note if isinstance(note, dict) else {}), (
                f"{path.name}: {channel}/{language} has grown a body; D4 says English only"
            )
    assert found, f"{path.name}: the translations_pending placeholders were removed"
