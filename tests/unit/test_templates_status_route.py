"""``PUT /api/v1/templates/{id}/status`` (spec §6.3, §6.4, §9.3) -- conformance item C-09.

``TemplateRegistry.set_status`` is tested in ``test_templates.py``; this file tests the DOOR:

* **who** may use it (§9.3's platform row: admin only), with auth enforced, and that the
  approver recorded is the principal, never a name in the body;
* **what** it governs: EMAIL/SMS/INAPP, not WhatsApp, whose status mirrors Meta's (§6.3);
* the §6.4 hard rule seen through the route: a ``sw`` row is approved only by a caller whose
  own role is legal or management, and only with a sign-off reference -- which, with auth
  enforced and §9.3 admitting admin alone, means not at all (the conflict the router's
  docstring records for the owner);
* that a PUT of the status a row already has changes nothing, so a second "approve" can never
  overwrite who actually approved the words;
* that every real change writes an audit row, and that another operator's id is a 404.
* that the transition is a compare-and-set: a decision made on a stale read is a 409, and
  concurrent approvals produce exactly one approval and one audit row (review
  routes-correctness#5).

Every test runs on its own SQLite file through the reload pattern the other route tests use.
"""

from __future__ import annotations

import importlib
import json
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from noc_agents.api import auth
from noc_agents.db.models import AuditRow, MessageTemplateRow, get_session
from noc_agents.realtime.hub import hub
from noc_agents.services.templates import TEMPLATE_CHANNELS, TemplateRegistry, load_seed_templates

SECRET = "templates-route-secret"
URL = "/api/v1/templates/{id}/status"

#: A Kiswahili SMS row and a WhatsApp row, which the shipped seed does not contain. The ``sw``
#: body is a marker, not invented Kiswahili (see services/templates.py on why none is seeded).
EXTRA_SEED = """\
template_key: vendor_notice
channels:
  SMS:
    params: {incident_number: string}
    languages:
      en:
        body: 'Vendor notice {{ incident_number }}'
      sw:
        body: '{{ incident_number }} SW'
  WHATSAPP:
    provider_template_name: noc_vendor_notice
    provider_language_code: en
    params: {incident_number: string}
    languages:
      en:
        body: '{{ incident_number }}'
        approval:
          status: SUBMITTED
"""


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A live API on its own SQLite file, the shipped templates and ``EXTRA_SEED`` synced into it."""
    db = tmp_path / "templates_route.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")
    monkeypatch.setenv("OPERATOR_PROFILE", "safaricom")

    import noc_agents.config as cfg
    import noc_agents.db.models as models
    import noc_agents.main as main

    cfg.clear_settings_cache()
    models._engine = None
    models.SessionLocal = None
    importlib.reload(main)
    auth.reset_sessions()
    hub._history.clear()

    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    (seed_dir / "vendor_notice.yaml").write_text(EXTRA_SEED, encoding="utf-8")
    seeds, pending = load_seed_templates(roots=[seed_dir])
    session = get_session()
    try:
        for operator_id in ("safaricom", "airtel"):  # two operators, one file (spec §8)
            registry = TemplateRegistry(session, operator_id)
            registry.sync()
            registry.sync(seeds, pending=pending)
        session.commit()
    finally:
        session.close()

    c = TestClient(main.app)
    c.__enter__()
    try:
        yield c
    finally:
        c.__exit__(None, None, None)
        hub._history.clear()
        auth.reset_sessions()
        models._engine = None
        models.SessionLocal = None
        cfg.clear_settings_cache()
        importlib.reload(main)


@pytest.fixture()
def enforced(client, monkeypatch):
    """The same app with ``AUTH_DISABLED=false`` for the length of one test."""
    monkeypatch.setenv("AUTH_DISABLED", "false")
    monkeypatch.setenv("NOC_SESSION_SECRET", SECRET)
    client.cookies.clear()
    yield client
    client.cookies.clear()


def _as(client: TestClient, role: str, name: str | None = None) -> None:
    client.cookies.clear()
    claims = {"sub": f"u-{role}", "role": role, "name": name or role}
    client.cookies.set(auth.SESSION_COOKIE, auth.sign_session(claims, SECRET))


def _switch(client: TestClient, role: str, name: str) -> None:
    """The demo role switcher (auth disabled): what the principal is when nobody logs in."""
    r = client.post("/api/v1/session", json={"display_name": name, "role": role})
    assert r.status_code == 200, r.text


def _template(channel: str, key: str, language: str, version: int = 1, operator_id: str = "safaricom") -> MessageTemplateRow:
    session = get_session()
    try:
        row = TemplateRegistry(session, operator_id).get(channel, key, language, version)
        assert row is not None, (channel, key, language, version, operator_id)
        session.expunge(row)
        return row
    finally:
        session.close()


def _reload(row_id: str) -> MessageTemplateRow:
    session = get_session()
    try:
        row = session.get(MessageTemplateRow, row_id)
        session.expunge(row)
        return row
    finally:
        session.close()


def _audits(row_id: str) -> list[AuditRow]:
    session = get_session()
    try:
        rows = list(session.scalars(select(AuditRow).where(AuditRow.entity_id == row_id).order_by(AuditRow.ts, text("rowid"))))
        for row in rows:
            session.expunge(row)
        return rows
    finally:
        session.close()


def _sendable(channel: str, key: str, language: str) -> bool:
    session = get_session()
    try:
        return TemplateRegistry(session, "safaricom").resolve(channel, key, language).ok
    finally:
        session.close()


def _put(client: TestClient, row_id: str, **body):
    return client.put(URL.format(id=row_id), json=body)


# --------------------------------------------------------------------------------------
# The approval, and who it is recorded against
# --------------------------------------------------------------------------------------


def test_admin_approves_a_draft_and_the_approver_is_the_principal(enforced):
    draft = _template("SMS", "incident_update", "en")
    assert draft.approval_status == "DRAFT" and not _sendable("SMS", "incident_update", "en")

    _as(enforced, "admin", "Grace Wanjiru")
    r = _put(enforced, draft.id, status="APPROVED", reason="wording checked against the runbook")

    assert r.status_code == 200, r.text
    out = r.json()
    assert (out["ok"], out["changed"], out["previous_status"]) == (True, True, "DRAFT")
    assert out["template"]["approval_status"] == "APPROVED"
    assert out["template"]["approved_by"] == "Grace Wanjiru"
    row = _reload(draft.id)
    assert (row.approval_status, row.approved_by) == ("APPROVED", "Grace Wanjiru")
    assert row.approved_at is not None
    # The registry now serves it for a real send: the route changed what can leave the building.
    assert _sendable("SMS", "incident_update", "en")

    (audit,) = _audits(draft.id)
    assert (audit.action, audit.actor, audit.entity_type) == ("template.approved", "Grace Wanjiru", "message_template")
    assert audit.operator_id == "safaricom"
    assert audit.rationale == "wording checked against the runbook"
    payload = json.loads(audit.payload_json)
    assert payload["from"] == "DRAFT" and payload["to"] == "APPROVED" and payload["role"] == "admin"
    assert (payload["channel"], payload["template_key"], payload["language"], payload["version"]) == (
        "SMS",
        "incident_update",
        "en",
        1,
    )


def test_the_body_cannot_name_the_approver(enforced):
    """No approver field exists, and an unknown field is refused rather than silently dropped:
    a client that thinks it is naming the approver must find out that it is not."""
    draft = _template("EMAIL", "incident_update", "en")
    _as(enforced, "admin", "Grace Wanjiru")

    for field in ("approved_by", "actor", "reviewer_role"):
        r = _put(enforced, draft.id, status="APPROVED", **{field: "legal" if field == "reviewer_role" else "Mallory"})
        assert r.status_code == 422, (field, r.text)

    row = _reload(draft.id)
    assert (row.approval_status, row.approved_by) == ("DRAFT", None)
    assert _audits(draft.id) == []


def test_with_auth_off_the_demo_role_switcher_is_the_approver(client):
    """``AUTH_DISABLED=true`` (the demo default): the gate is inert and the principal is the
    role switcher, exactly as for every other write."""
    draft = _template("SMS", "incident_restored", "en")
    _switch(client, "admin", "Demo Admin")

    r = _put(client, draft.id, status="approved")  # the status is normalised, as the registry does

    assert r.status_code == 200, r.text
    assert _reload(draft.id).approved_by == "Demo Admin"


def test_only_admin_may_set_a_template_status(enforced):
    """§9.3 platform row: read for four roles, "—" for four, all for admin. The write is admin's."""
    draft = _template("SMS", "incident_update", "en")

    enforced.cookies.clear()
    assert _put(enforced, draft.id, status="APPROVED").status_code == 401

    refused = {}
    for role in sorted(set(auth.ROLES) - {"admin"}):
        _as(enforced, role)
        refused[role] = _put(enforced, draft.id, status="APPROVED").status_code
    assert refused == {role: 403 for role in refused}
    assert _reload(draft.id).approval_status == "DRAFT"

    _as(enforced, "admin")
    assert _put(enforced, draft.id, status="APPROVED").status_code == 200


# --------------------------------------------------------------------------------------
# Transitions and history
# --------------------------------------------------------------------------------------


def test_leaving_approved_keeps_the_history_and_a_reapproval_names_the_new_approver(enforced):
    row = _template("EMAIL", "incident_restored", "en")

    _as(enforced, "admin", "First Approver")
    assert _put(enforced, row.id, status="APPROVED").status_code == 200
    first_at = _reload(row.id).approved_at

    _as(enforced, "admin", "Night Admin")
    r = _put(enforced, row.id, status="PAUSED", reason="wrong escalation number")
    assert r.status_code == 200, r.text
    paused = _reload(row.id)
    # Pausing stops the send; it does not erase who once approved the words.
    assert (paused.approval_status, paused.approved_by, paused.approved_at) == ("PAUSED", "First Approver", first_at)
    assert not _sendable("EMAIL", "incident_restored", "en")

    _as(enforced, "admin", "Day Admin")
    assert _put(enforced, row.id, status="APPROVED").status_code == 200
    again = _reload(row.id)
    assert (again.approval_status, again.approved_by) == ("APPROVED", "Day Admin")

    audits = _audits(row.id)
    assert [(a.action, a.actor) for a in audits] == [
        ("template.approved", "First Approver"),
        ("template.paused", "Night Admin"),
        ("template.approved", "Day Admin"),
    ]
    reapproval = json.loads(audits[-1].payload_json)
    assert reapproval["from"] == "PAUSED" and reapproval["previous_approved_by"] == "First Approver"


@pytest.mark.parametrize("status", ["SUBMITTED", "REJECTED", "PAUSED"])
def test_every_non_approving_status_is_reachable_and_audited(enforced, status):
    row = _template("SMS", "incident_update", "en")
    _as(enforced, "admin", "Grace Wanjiru")

    r = _put(enforced, row.id, status=status)

    assert r.status_code == 200, r.text
    stored = _reload(row.id)
    assert (stored.approval_status, stored.approved_by) == (status, None)  # only APPROVED records an approver
    (audit,) = _audits(row.id)
    assert audit.action == f"template.{status.lower()}"


def test_asking_for_the_status_a_row_already_has_changes_nothing(enforced):
    """PUT is idempotent. For APPROVED this is the point: a second "approve" must not overwrite
    ``approved_by``/``approved_at`` and so erase who actually approved the words."""
    live = _template("SMS", "site_down_alert", "en")
    assert (live.approval_status, live.approved_by) == ("APPROVED", "policy:v1_fidelity")

    _as(enforced, "admin", "Someone Else")
    r = _put(enforced, live.id, status="APPROVED")

    assert r.status_code == 200, r.text
    assert (r.json()["changed"], r.json()["previous_status"]) == (False, "APPROVED")
    after = _reload(live.id)
    assert (after.approved_by, after.approved_at, after.updated_at) == (live.approved_by, live.approved_at, live.updated_at)
    assert _audits(live.id) == []


def test_an_unknown_status_is_422(enforced):
    row = _template("SMS", "incident_update", "en")
    _as(enforced, "admin")
    for status in ("LIVE", "", "DELETED"):
        assert _put(enforced, row.id, status=status).status_code == 422, status
    assert _put(enforced, row.id).status_code == 422  # no status at all
    assert _reload(row.id).approval_status == "DRAFT"


# --------------------------------------------------------------------------------------
# WhatsApp mirrors Meta
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["APPROVED", "PAUSED", "REJECTED", "DRAFT", "SUBMITTED"])
def test_a_whatsapp_status_is_never_set_by_hand(enforced, status):
    wa = _template("WHATSAPP", "vendor_notice", "en")
    assert wa.approval_status == "SUBMITTED"
    _as(enforced, "admin")

    r = _put(enforced, wa.id, status=status)

    assert r.status_code == 409, r.text
    assert "WHATSAPP" in r.json()["detail"]
    assert _reload(wa.id).approval_status == "SUBMITTED"
    assert _audits(wa.id) == []


def test_the_governed_channels_are_every_registry_channel_except_whatsapp():
    """An allow-list, so a channel added to the registry later is refused by this route until
    someone decides who approves it. This test is where that decision gets asked for."""
    from noc_agents.api.routers.templates import OPERATOR_APPROVED_CHANNELS

    assert set(OPERATOR_APPROVED_CHANNELS) == set(TEMPLATE_CHANNELS) - {"WHATSAPP"}


# --------------------------------------------------------------------------------------
# Kiswahili (§6.4 hard rule)
# --------------------------------------------------------------------------------------


def test_a_kiswahili_template_is_not_approved_by_a_caller_who_is_not_a_reviewer(client):
    sw = _template("SMS", "vendor_notice", "sw")
    for role in ("noc_analyst", "admin", "duty_manager"):
        _switch(client, role, "Not A Reviewer")
        r = _put(client, sw.id, status="APPROVED", signoff_ref="docs/SIGNOFF.md#sw-2026-09")
        assert r.status_code == 403, (role, r.text)
        assert "§6.4" in r.json()["detail"]
    assert _reload(sw.id).approval_status == "DRAFT"
    assert _audits(sw.id) == []


def test_a_kiswahili_approval_needs_a_recorded_signoff(client):
    sw = _template("SMS", "vendor_notice", "sw")
    _switch(client, "legal", "Amina Odhiambo")

    r = _put(client, sw.id, status="APPROVED")

    assert r.status_code == 422, r.text
    assert "sign-off" in r.json()["detail"]
    assert _reload(sw.id).approval_status == "DRAFT"


def test_a_reviewer_with_a_signoff_approves_kiswahili_and_the_audit_row_records_both(client):
    sw = _template("SMS", "vendor_notice", "sw")
    assert not _sendable("SMS", "vendor_notice", "sw")  # sw is DRAFT, and so is the en it would fall back to
    _switch(client, "legal", "Amina Odhiambo")

    r = _put(client, sw.id, status="APPROVED", signoff_ref="docs/SIGNOFF.md#sw-2026-09")

    assert r.status_code == 200, r.text
    row = _reload(sw.id)
    assert (row.approval_status, row.approved_by) == ("APPROVED", "Amina Odhiambo")
    (audit,) = _audits(sw.id)
    payload = json.loads(audit.payload_json)
    assert (payload["role"], payload["signoff_ref"], payload["language"]) == ("legal", "docs/SIGNOFF.md#sw-2026-09", "sw")
    # The §6.4 fallback stops: a sw request now resolves to the sw row itself.
    session = get_session()
    try:
        assert TemplateRegistry(session, "safaricom").resolve("SMS", "vendor_notice", "sw").language == "sw"
    finally:
        session.close()


def test_with_auth_enforced_no_caller_can_approve_kiswahili_yet(enforced):
    """The §9.3 / §6.4 conflict, pinned so that resolving it is a visible decision: the route
    admits admin only, and admin is not a reviewer role. Fail closed: no sw row is approved."""
    sw = _template("SMS", "vendor_notice", "sw")
    body = {"status": "APPROVED", "signoff_ref": "docs/SIGNOFF.md#sw-2026-09"}

    _as(enforced, "admin", "Grace Wanjiru")
    assert enforced.put(URL.format(id=sw.id), json=body).status_code == 403  # not a reviewer role
    for reviewer in ("legal", "management"):
        _as(enforced, reviewer, "A Reviewer")
        assert enforced.put(URL.format(id=sw.id), json=body).status_code == 403  # not admitted by §9.3
    assert _reload(sw.id).approval_status == "DRAFT"

    # Only APPROVED is gated by the reviewer rule; admin may still move a sw row elsewhere.
    _as(enforced, "admin", "Grace Wanjiru")
    assert _put(enforced, sw.id, status="REJECTED").status_code == 200


# --------------------------------------------------------------------------------------
# Operator scoping
# --------------------------------------------------------------------------------------


def test_another_operators_template_is_a_404_and_is_left_alone(enforced):
    theirs = _template("SMS", "incident_update", "en", operator_id="airtel")
    _as(enforced, "admin")

    for target in (theirs.id, "no-such-template"):
        r = _put(enforced, target, status="APPROVED")
        assert r.status_code == 404, r.text
        assert r.json()["detail"] == "template not found"

    assert _reload(theirs.id).approval_status == "DRAFT"
    assert _audits(theirs.id) == []


# --------------------------------------------------------------------------------------
# Concurrency: the transition is a compare-and-set (review routes-correctness#5)
# --------------------------------------------------------------------------------------


def _paused(key: str = "incident_update", channel: str = "SMS") -> MessageTemplateRow:
    """A template approved once and then paused: the state a re-approval race starts from."""
    session = get_session()
    try:
        registry = TemplateRegistry(session, "safaricom")
        row = registry.get(channel, key, "en", 1)
        registry.set_status(row, "APPROVED", actor="Original Approver")
        registry.set_status(row, "PAUSED", actor="Original Approver")
        session.commit()
        session.refresh(row)  # the commit expired it; load it before detaching
        session.expunge(row)
        return row
    finally:
        session.close()


def _admin(name: str) -> auth.Principal:
    return auth.Principal(role="admin", display_name=name, authenticated=True, source="cookie", subject=f"u-{name}")


def test_a_transition_decided_on_a_stale_read_is_a_409_and_overwrites_nobody(enforced, monkeypatch):
    """Alice's approval commits between Bob's read and Bob's write. Bob decided on PAUSED, which
    is no longer true: he gets a 409 naming what he lost to, and Alice stays the approver."""
    from noc_agents.api.routers import templates as templates_router

    row = _paused()
    real = templates_router._get_owned

    def read_then_alice_approves(session, model, row_id, *, what):
        found = real(session, model, row_id, what=what)
        other = get_session()
        try:
            TemplateRegistry(other, "safaricom").set_status(other.get(MessageTemplateRow, row_id), "APPROVED", actor="Alice Admin")
            other.commit()
        finally:
            other.close()
        return found

    monkeypatch.setattr(templates_router, "_get_owned", read_then_alice_approves)
    _as(enforced, "admin", "Bob Admin")
    r = _put(enforced, row.id, status="APPROVED")

    assert r.status_code == 409, r.text
    assert "was PAUSED and is now APPROVED" in r.json()["detail"]
    after = _reload(row.id)
    assert (after.approval_status, after.approved_by) == ("APPROVED", "Alice Admin")
    assert [a.actor for a in _audits(row.id)] == []  # Bob wrote nothing; Alice went round the route


def test_concurrent_approvals_produce_exactly_one_approval(client):
    """No injection: six admins approve the same PAUSED template at once, five times over. Each
    time exactly one request changes it, one audit row records it, and ``approved_by`` names
    that request's admin. The others are 409s, or no-ops if they read after the commit."""
    from fastapi import HTTPException

    from noc_agents.api.routers.templates import TemplateStatusIn, set_template_status

    for trial in range(5):
        row = _paused("incident_restored", "EMAIL") if trial == 0 else _reload(row.id)
        if trial:
            session = get_session()
            try:
                TemplateRegistry(session, "safaricom").set_status(session.get(MessageTemplateRow, row.id), "PAUSED", actor="Pauser")
                session.commit()
            finally:
                session.close()
        before = len(_audits(row.id))
        start = threading.Barrier(6)
        outcomes: dict[str, object] = {}

        def approve(name: str) -> None:
            start.wait(timeout=30)
            try:
                out = set_template_status(row.id, TemplateStatusIn(status="APPROVED"), principal=_admin(name))
                outcomes[name] = "changed" if out["changed"] else "no-op"
            except HTTPException as exc:
                outcomes[name] = exc.status_code

        threads = [threading.Thread(target=approve, args=(f"Admin{i}",)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        winners = [name for name, outcome in outcomes.items() if outcome == "changed"]
        assert len(outcomes) == 6, (trial, outcomes)
        assert len(winners) == 1, (trial, outcomes)
        assert all(outcome in ("no-op", 409) for name, outcome in outcomes.items() if name not in winners), outcomes
        new_audits = _audits(row.id)[before:]
        assert [(a.action, a.actor) for a in new_audits] == [("template.approved", winners[0])], (trial, outcomes)
        assert _reload(row.id).approved_by == winners[0]
