"""Spec §7.8 — the confidential complaint intake: who may read it, and what leaks.

This lane holds the most sensitive data in the system, and the sensitivity runs sideways:
a complaint is confidential from *colleagues*, above all from the person it is about. The
sections below are ordered by what would do the most damage if it broke.

**The flag.** §7.8 ships the lane off. The whole surface must be invisible with
``COMPLAINTS_ENABLED`` unset, and the job inert even if its card were wired in early.

**The one rule.** The person complained about can never read the complaint. Tested from
both directions: a subject cannot read, list, count or be assigned their own complaint even
as ``duty_manager`` or ``admin``; and a complaint cannot be filed against its own filer,
which would produce a row its subject can read by definition.

**Minimisation.** DPA 2019 s.25 via §7.8.6: enumerated categories, short free text, no
MSISDN or e-mail, and no naming the subject in prose — with every failure returned at once,
because a form that rejects one field per round trip is a form people give up on.

**The classifier decides nothing.** DPA 2019 s.35: it suggests a category and stops. It
files nothing, it never sees an unscrubbed contact detail, and an answer outside the
vocabulary falls back to the deterministic classifier rather than reaching a form.

**Reminders and retention.** A reminder that quotes the complaint is a second copy of it in
an inbox (§9.5's rule, applied here); retention reduces free text at 24 months and keeps the
counts (§9.4), under the same posture gate as every other deletion in this system.

**The firewall.** §7.8.6: complaints are never an input to scorecards or individual
metrics. Proved by a static sweep of the source tree, the way ``test_pir.py`` does it.

No network, no model call: the classifier is exercised through a fake port, as
``tests/unit/test_llm_port.py`` does.
"""

from __future__ import annotations

import importlib
import json
import pathlib
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from noc_agents.api import auth
from noc_agents.db.models import AuditRow, OutboxRow
from noc_agents.db.models_complaints import RelationshipComplaintRow, SubjectPersonRow
from noc_agents.services import complaints as svc

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "noc_agents"
SECRET = "test-secret-complaints"

T0 = datetime(2026, 9, 16, 8, 0, 0)  # a Wednesday; naive UTC, the storage contract

FILER = "Grace Wanjiru"
SUBJECT = "Kevin Ochieng"
MANAGER = "Duty Manager"


# --------------------------------------------------------------------------
# Fixtures and builders
# --------------------------------------------------------------------------


@pytest.fixture()
def on(monkeypatch):
    """The lane switched on. Every test that wants a route to answer asks for this."""
    monkeypatch.setenv(svc.ENABLED_ENV, "true")


@pytest.fixture()
def off(monkeypatch):
    """The shipped default, stated explicitly rather than inherited from the environment."""
    monkeypatch.setenv(svc.ENABLED_ENV, "false")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A live API on its own SQLite file (the reload pattern the other route tests use)."""
    db = tmp_path / "complaints.db"
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
    c = TestClient(main.app)
    c.__enter__()
    try:
        yield c
    finally:
        c.__exit__(None, None, None)
        auth.reset_sessions()
        models._engine = None
        models.SessionLocal = None
        cfg.clear_settings_cache()
        importlib.reload(main)


def _api_session():
    from noc_agents.db.models import get_session

    return get_session()


def _as(client: TestClient, role: str, display_name: str) -> None:
    """The demo role switcher: with AUTH_DISABLED=true the principal is what it says."""
    r = client.post("/api/v1/session", json={"display_name": display_name, "role": role})
    assert r.status_code == 200, r.text


def _login(client: TestClient, role: str, display_name: str, sub: str = "u-1") -> None:
    """A real signed session, for the tests where the gates must actually bite."""
    client.cookies.set(
        auth.SESSION_COOKIE, auth.sign_session({"sub": sub, "role": role, "name": display_name}, SECRET)
    )


@pytest.fixture()
def auth_on(monkeypatch):
    monkeypatch.setenv("AUTH_DISABLED", "false")
    monkeypatch.setenv("NOC_SESSION_SECRET", SECRET)


def _register_subject(session, *, name: str = SUBJECT, operator_id: str = "safaricom") -> SubjectPersonRow:
    row = SubjectPersonRow(operator_id=operator_id, display_name=name, created_at=T0)
    session.add(row)
    session.flush()
    return row


def _payload(**overrides) -> dict:
    body = {
        "subject_type": "VENDOR",
        "vendor_id": "vendor-egypro",
        "category": "NO_SHOW",
        "severity": "MEDIUM",
        "description": "Crew did not attend the 09:00 SLA visit; site stayed down all morning.",
    }
    body.update(overrides)
    return body


def _file(client: TestClient, **overrides) -> dict:
    r = client.post("/api/v1/complaints", json=_payload(**overrides))
    assert r.status_code == 200, r.text
    return r.json()


def _row(session, *, filed_by: str = FILER, operator_id: str = "safaricom", **overrides):
    values = dict(
        operator_id=operator_id,
        filed_by=filed_by,
        filed_at=T0,
        subject_type="VENDOR",
        vendor_id="vendor-egypro",
        category="NO_SHOW",
        severity="MEDIUM",
        description="Crew did not attend the 09:00 SLA visit.",
        status=svc.OPEN,
        follow_up_due_at=svc.add_working_days(T0, svc.FOLLOW_UP_WORKING_DAYS),
        retention_until=T0 + timedelta(days=svc.RETENTION_DAYS),
        updated_at=T0,
    )
    values.update(overrides)
    row = RelationshipComplaintRow(**values)
    session.add(row)
    session.flush()
    return row


# --------------------------------------------------------------------------
# The flag: the lane ships OFF
# --------------------------------------------------------------------------


def test_every_route_is_invisible_while_the_flag_is_off(client, off):
    """404, not 403: with the lane off the feature does not exist to announce."""
    _as(client, "noc_analyst", FILER)
    assert client.get("/api/v1/complaints").status_code == 404
    assert client.get("/api/v1/complaints/stats").status_code == 404
    assert client.post("/api/v1/complaints", json=_payload()).status_code == 404
    assert client.post("/api/v1/complaints/classify", json={"text": "late"}).status_code == 404
    assert client.get("/api/v1/complaints/subject-access/whatever").status_code == 404
    assert client.get("/api/v1/complaints/anything").status_code == 404


def test_the_job_card_ships_off_and_the_job_is_inert(tmp_db, off):
    """``default_enabled=False`` so /scheduler/status reports it off, and the job re-checks."""
    settings, session = tmp_db
    assert svc.COMPLAINTS_JOB.default_enabled is False
    assert svc.COMPLAINTS_JOB.enabled_env == "COMPLAINTS_ENABLED"
    _row(session, follow_up_due_at=T0 - timedelta(days=30))
    session.commit()
    result = svc.followup_job(session, settings)
    assert "COMPLAINTS_ENABLED=false" in result.summary
    assert session.query(OutboxRow).count() == 0


def test_the_lane_is_registered_in_the_scheduler_and_ships_disabled():
    """The card is wired in, and wiring it in is safe because it ships off.

    This test replaces an earlier one that asserted the OPPOSITE -- that ``loop.py`` did not
    mention this lane at all. That was true while the lane was being built (its author was
    not permitted to edit the shared scheduler module, so "I did not wire this in" was an
    accurate statement about their own work), but it is the wrong thing to pin permanently:
    as an invariant it forbids the lane from ever being integrated, and the first person to
    wire it up correctly would be greeted by a red test telling them not to.

    The property actually worth protecting is not "unregistered" but "registered and inert":
    a card in ``SCHEDULED_JOBS`` that carries ``default_enabled=False`` and whose job
    re-checks its own flag cannot run on a deployment that did not opt in. That is what is
    asserted here, and the sibling test above covers the job's own flag re-check.
    """
    from noc_agents.scheduler.loop import SCHEDULED_JOBS, job_enabled

    card = next((c for c in SCHEDULED_JOBS if c.name == svc.COMPLAINTS_JOB.name), None)
    assert card is not None, (
        f"{svc.COMPLAINTS_JOB.name!r} is not in SCHEDULED_JOBS; the lane's reminders and its"
        " 24-month text reduction would never run"
    )
    assert card is svc.COMPLAINTS_JOB  # the registered card is the lane's own, not a copy
    assert card.default_enabled is False
    assert job_enabled(card) is False  # COMPLAINTS_ENABLED is unset in the suite (conftest)


# --------------------------------------------------------------------------
# The one rule: the subject can never read the complaint
# --------------------------------------------------------------------------


def test_the_subject_cannot_read_a_complaint_about_themselves_even_as_duty_manager(client, on):
    """Direction 1 — read. The subject is excluded in the WHERE clause, whatever the role.

    A ``duty_manager`` sees every complaint (§9.3 "view all") except the ones about them:
    that exception is the product. And the answer is 404, not 403, because 403 would confirm
    to the subject that a complaint about them is on file.
    """
    session = _api_session()
    try:
        subject = _register_subject(session, name=MANAGER)
        session.commit()
        ref = subject.ref
    finally:
        session.close()

    _as(client, "noc_analyst", FILER)
    about_the_manager = _file(
        client,
        subject_type="INDIVIDUAL",
        subject_person_ref=ref,
        category="CONDUCT",
        severity="HIGH",
        description="Shouted at the field crew on an open bridge; twice this month.",
    )
    other = _file(client)

    _as(client, "duty_manager", MANAGER)
    assert client.get(f"/api/v1/complaints/{about_the_manager['id']}").status_code == 404
    assert client.get(f"/api/v1/complaints/{other['id']}").status_code == 200
    ids = [row["id"] for row in client.get("/api/v1/complaints").json()]
    assert about_the_manager["id"] not in ids and other["id"] in ids
    # Not even the counts: a total that moves is a disclosure that something exists.
    stats = client.get("/api/v1/complaints/stats").json()
    assert stats["total"] == 1 and "CONDUCT" not in stats["by_category"]


def test_admin_gets_no_bypass_of_the_subject_rule(client, on):
    """``admin`` is a role, not an exemption — the predicate is in the query, not the gate."""
    session = _api_session()
    try:
        subject = _register_subject(session, name="The Admin")
        session.commit()
        ref = subject.ref
    finally:
        session.close()
    _as(client, "noc_analyst", FILER)
    filed = _file(
        client,
        subject_type="INDIVIDUAL",
        subject_person_ref=ref,
        category="UNSAFE_PRACTICE",
        severity="HIGH",
        description="Climbed the tower without a harness after being told to stop.",
    )
    _as(client, "admin", "The Admin")
    assert client.get(f"/api/v1/complaints/{filed['id']}").status_code == 404
    assert client.get("/api/v1/complaints").json() == []


def test_a_complaint_cannot_be_filed_against_its_own_filer(client, on):
    """Direction 2 — write. Without this bar the filer IS the subject, and a filer can always
    read what they filed: the one thing this lane must make impossible."""
    session = _api_session()
    try:
        subject = _register_subject(session, name=FILER)
        session.commit()
        ref = subject.ref
    finally:
        session.close()
    _as(client, "noc_analyst", FILER)
    r = client.post(
        "/api/v1/complaints",
        json=_payload(subject_type="INDIVIDUAL", subject_person_ref=ref, vendor_id=None),
    )
    assert r.status_code == 422
    assert svc.MSG_SELF in r.json()["detail"]


def test_a_complaint_cannot_be_assigned_to_its_own_subject(client, on):
    """The write-side half of the read rule: a subject cannot be handed their own complaint."""
    session = _api_session()
    try:
        subject = _register_subject(session, name=MANAGER)
        session.commit()
        ref = subject.ref
    finally:
        session.close()
    _as(client, "noc_analyst", FILER)
    filed = _file(
        client,
        subject_type="INDIVIDUAL",
        subject_person_ref=ref,
        vendor_id=None,
        category="POOR_COMMUNICATION",
        description="No update for six hours on a P1 despite three chases.",
    )
    _as(client, "shift_supervisor", "Sup")
    r = client.post(f"/api/v1/complaints/{filed['id']}/assign", json={"manager": MANAGER})
    assert r.status_code == 422
    assert svc.MSG_MANAGER_IS_SUBJECT in r.json()["detail"]


def test_an_engineer_sees_only_their_own_filings(client, on):
    """§7.8.2: "own for engineers; all for duty_manager/management"."""
    _as(client, "field_engineer", "Peter Mutua")
    mine = _file(client)
    _as(client, "noc_analyst", FILER)
    theirs = _file(client, category="ACCESS_ISSUE")

    _as(client, "field_engineer", "Peter Mutua")
    ids = [row["id"] for row in client.get("/api/v1/complaints").json()]
    assert ids == [mine["id"]]
    assert client.get(f"/api/v1/complaints/{theirs['id']}").status_code == 404

    _as(client, "duty_manager", MANAGER)
    assert len(client.get("/api/v1/complaints").json()) == 2


def test_only_the_filer_may_withdraw(client, on):
    _as(client, "noc_analyst", FILER)
    filed = _file(client)
    _as(client, "duty_manager", MANAGER)
    assert client.post(f"/api/v1/complaints/{filed['id']}/withdraw", json={}).status_code == 403
    _as(client, "noc_analyst", FILER)
    r = client.post(f"/api/v1/complaints/{filed['id']}/withdraw", json={"reason": "sorted directly"})
    assert r.status_code == 200 and r.json()["status"] == svc.WITHDRAWN


def test_the_list_view_does_not_carry_the_description(client, on):
    """A queue is read over shoulders in an open-plan NOC; opening one is a deliberate act."""
    _as(client, "noc_analyst", FILER)
    _file(client)
    rows = client.get("/api/v1/complaints").json()
    assert rows and "description" not in rows[0]


# --------------------------------------------------------------------------
# RBAC with the gates biting (AUTH_DISABLED=false)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["msp_coordinator", "planning", "legal"])
def test_roles_absent_from_the_complaints_row_of_9_3_are_refused(client, on, auth_on, role):
    """§9.3 gives complaints to nobody else — an MSP coordinator reading complaints about
    MSPs is the conflict of interest this table exists to manage."""
    _login(client, role, "Someone")
    assert client.get("/api/v1/complaints").status_code == 403
    assert client.post("/api/v1/complaints", json=_payload()).status_code == 403


def test_subject_access_is_legal_and_admin_only(client, on, auth_on):
    session = _api_session()
    try:
        subject = _register_subject(session)
        session.commit()
        ref = subject.ref
    finally:
        session.close()
    _login(client, "noc_analyst", FILER)
    assert client.get(f"/api/v1/complaints/subject-access/{ref}").status_code == 403
    _login(client, "legal", "Legal Counsel", sub="u-legal")
    assert client.get(f"/api/v1/complaints/subject-access/{ref}").status_code == 200


def test_a_filer_cannot_assign_and_a_manager_cannot_file(client, on, auth_on):
    """The two readings of §9.3's cell that had to be settled, pinned so a later edit is a
    deliberate decision rather than a drift."""
    _login(client, "field_engineer", "Peter Mutua")
    filed = client.post("/api/v1/complaints", json=_payload())
    assert filed.status_code == 200
    assert client.post(
        f"/api/v1/complaints/{filed.json()['id']}/assign", json={"manager": MANAGER}
    ).status_code == 403
    _login(client, "duty_manager", MANAGER, sub="u-dm")
    assert client.post("/api/v1/complaints", json=_payload()).status_code == 403
    assert client.get("/api/v1/complaints").status_code == 200


def test_the_routes_do_not_exist_at_all_in_an_unauthenticated_production_deployment(monkeypatch):
    """§7.8.6 / §7.0.5: "the routes cannot exist" is stronger than "the routes answer 403"."""
    import noc_agents.api.routers.complaints as router_module

    monkeypatch.setenv("AUTH_DISABLED", "true")
    monkeypatch.setenv("NOC_ENV", "production")
    guarded = importlib.reload(router_module)
    try:
        assert guarded.router.routes == []
    finally:
        monkeypatch.setenv("NOC_ENV", "demo")
        importlib.reload(router_module)
    assert router_module.router.routes  # restored for the rest of the suite


# --------------------------------------------------------------------------
# Minimisation (DPA s.25) — the validator
# --------------------------------------------------------------------------


def test_a_phone_number_or_an_email_is_refused(client, on):
    _as(client, "noc_analyst", FILER)
    for text in (
        "Crew never arrived; called 0712 345 678 four times.",
        "Crew never arrived; escalated to fred@egypro.co.ke with no reply.",
    ):
        r = client.post("/api/v1/complaints", json=_payload(description=text))
        assert r.status_code == 422
        assert svc.MSG_NO_CONTACTS in r.json()["detail"]


def test_a_description_that_names_the_subject_is_refused(client, on):
    """The name lives in ``subject_persons`` and nowhere else; free text quietly undoes that."""
    session = _api_session()
    try:
        subject = _register_subject(session)
        session.commit()
        ref = subject.ref
    finally:
        session.close()
    _as(client, "noc_analyst", FILER)
    r = client.post(
        "/api/v1/complaints",
        json=_payload(
            subject_type="INDIVIDUAL",
            vendor_id=None,
            subject_person_ref=ref,
            description="Kevin arrived four hours after the SLA and left the cabinet open.",
        ),
    )
    assert r.status_code == 422
    assert svc.MSG_NAMES_SUBJECT in r.json()["detail"]
    # First-name-only is caught too: the shared NameMap matches name parts (llm/redaction.py).
    assert "Ochieng" not in r.json()["detail"]  # and the offending text is never echoed back


def test_every_failure_comes_back_at_once(client, on):
    """The ``services/pir.py`` house style: one round trip, every problem."""
    _as(client, "noc_analyst", FILER)
    r = client.post(
        "/api/v1/complaints",
        json=_payload(
            subject_type="ALIEN",
            category="WHATEVER",
            severity="URGENT",
            description="call 0722000111 or mail a@b.co.ke",
        ),
    )
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "subject_type" in detail and "category" in detail and "severity" in detail
    assert svc.MSG_NO_CONTACTS in detail


def test_free_text_is_capped(client, on):
    _as(client, "noc_analyst", FILER)
    r = client.post("/api/v1/complaints", json=_payload(description="x" * (svc.MAX_DESCRIPTION_CHARS + 1)))
    assert r.status_code == 422 and "minimisation" in r.json()["detail"]


def test_a_vendor_complaint_may_not_name_an_individual(client, on):
    """Naming a person inside a complaint about their employer turns a contract dispute into
    a disciplinary one (Employment Act 2007 s.41/s.43)."""
    session = _api_session()
    try:
        subject = _register_subject(session)
        session.commit()
        ref = subject.ref
    finally:
        session.close()
    _as(client, "noc_analyst", FILER)
    r = client.post("/api/v1/complaints", json=_payload(subject_type="VENDOR", subject_person_ref=ref))
    assert r.status_code == 422 and "subject_person_ref" in r.json()["detail"]


def test_an_individual_complaint_needs_a_token_or_a_ref_and_a_token_may_not_be_a_name(client, on):
    _as(client, "noc_analyst", FILER)
    r = client.post("/api/v1/complaints", json=_payload(subject_type="INDIVIDUAL", vendor_id=None))
    assert r.status_code == 422 and "subject_role_token or subject_person_ref" in r.json()["detail"]
    r = client.post(
        "/api/v1/complaints",
        json=_payload(subject_type="INDIVIDUAL", vendor_id=None, subject_role_token="Kevin Ochieng"),
    )
    assert r.status_code == 422 and "role token" in r.json()["detail"]
    assert svc.is_role_token("FE-MTK-01") and svc.is_role_token("MSP-EGYPRO-POWER")
    assert not svc.is_role_token("Kevin") and not svc.is_role_token("Kevin Ochieng")


def test_a_resolution_is_mandatory_and_is_validated_like_the_description(client, on):
    _as(client, "noc_analyst", FILER)
    filed = _file(client)
    _as(client, "duty_manager", MANAGER)
    assert client.post(f"/api/v1/complaints/{filed['id']}/resolve", json={"resolution": " "}).status_code == 422
    bad = client.post(
        f"/api/v1/complaints/{filed['id']}/resolve",
        json={"resolution": "Spoke to the crew lead on 0712 345 678."},
    )
    assert bad.status_code == 422
    ok = client.post(
        f"/api/v1/complaints/{filed['id']}/resolve",
        json={"resolution": "Vendor briefed; SLA reminder issued; no repeat in 30 days."},
    )
    assert ok.status_code == 200 and ok.json()["status"] == svc.RESOLVED
    # Terminal means terminal: a closed complaint is the record of what a manager decided.
    assert client.post(
        f"/api/v1/complaints/{filed['id']}/resolve", json={"resolution": "again"}
    ).status_code == 422


# --------------------------------------------------------------------------
# The classifier: advisory only (DPA s.35), and it never files
# --------------------------------------------------------------------------


class FakePort:
    """Stands in for an ``LlmPort``; records the text it was given and returns what it is told.

    No SDK, no socket, no credential — the ``tests/unit/test_llm_port.py`` pattern.
    """

    provider = "anthropic"

    def __init__(self, category="LATE_ARRIVAL", severity="LOW", reason="looks late"):
        self.seen: list[str] = []
        self._answer = (category, severity, reason)

    def draft(self, *, model, system, user, output_model, **kwargs):
        self.seen.append(user)
        category, severity, reason = self._answer
        return output_model(category=category, severity=severity, reason=reason), object()


def test_the_keyword_classifier_answers_offline_and_falls_back_to_other():
    hot = svc.keyword_draft("Technician climbed the tower with no PPE at all")
    assert (hot.category, hot.severity, hot.source) == (svc.UNSAFE_PRACTICE, svc.HIGH, "keyword")
    cold = svc.keyword_draft("The paperwork reference was written in the wrong column")
    assert (cold.category, cold.severity) == (svc.OTHER, svc.LOW)
    assert cold.advisory_only is True and cold.ai_assisted is False


def test_the_classifier_scrubs_contacts_before_the_model_sees_them():
    port = FakePort()
    svc.classify("Called 0712 345 678 and mailed fred@egypro.co.ke, no answer", port=port)
    assert "0712" not in port.seen[0] and "@egypro" not in port.seen[0]
    assert "<PHONE>" in port.seen[0] and "<EMAIL>" in port.seen[0]


def test_a_model_answer_outside_the_vocabulary_falls_back_to_the_keyword_draft():
    """A category the system does not have is not a suggestion; it is a bug reaching a form."""
    draft = svc.classify("nobody came to the site", port=FakePort(category="DISCIPLINARY_ACTION"))
    assert draft.source == "keyword" and draft.category == svc.NO_SHOW


def test_a_model_that_raises_never_stops_a_filing():
    class Exploding(FakePort):
        def draft(self, **kwargs):
            raise RuntimeError("no network here")

    draft = svc.classify("arrived three hours late", port=Exploding())
    assert draft.source == "keyword" and draft.category == svc.LATE_ARRIVAL


def test_classify_files_nothing_and_says_it_decides_nothing(client, on):
    """§5.3.21 autonomy A2 and DPA s.35: a suggestion for a human, never a row."""
    _as(client, "noc_analyst", FILER)
    r = client.post("/api/v1/complaints/classify", json={"text": "Crew never arrived for the 09:00 visit"})
    assert r.status_code == 200
    body = r.json()
    assert body["category"] == svc.NO_SHOW
    assert body["advisory_only"] is True
    assert "s.35" in body["disclosure"]
    session = _api_session()
    try:
        assert session.query(RelationshipComplaintRow).count() == 0
        actions = {row.action for row in session.query(AuditRow).all()}
        assert svc.AUDIT_CLASSIFIED in actions
    finally:
        session.close()


def test_the_classifier_never_reaches_a_hosted_model_while_llm_is_off(client, on, monkeypatch):
    """``LLM_ENABLED=false`` is the §9.2 s.49(2) suspension case, not a degraded mode."""
    monkeypatch.setenv("LLM_ENABLED", "false")
    called = []
    monkeypatch.setattr(
        "noc_agents.api.routers.complaints.get_llm_port", lambda *a, **k: called.append(1)
    )
    _as(client, "noc_analyst", FILER)
    r = client.post("/api/v1/complaints/classify", json={"text": "unsafe work at height"})
    assert r.status_code == 200 and r.json()["source"] == "keyword"
    assert called == []


# --------------------------------------------------------------------------
# Manager reminders: counts and references, never content
# --------------------------------------------------------------------------


def test_the_follow_up_clock_counts_working_days():
    friday = datetime(2026, 9, 18, 9, 0)
    assert svc.add_working_days(friday, 5) == datetime(2026, 9, 25, 9, 0)  # the next Friday
    saturday_start = datetime(2026, 9, 19, 9, 0)
    assert svc.add_working_days(saturday_start, 1) == datetime(2026, 9, 21, 9, 0)  # Monday


def test_an_overdue_complaint_produces_one_reminder_per_manager_per_day(tmp_db, on):
    settings, session = tmp_db
    overdue = T0 - timedelta(days=3)
    _row(session, follow_up_due_at=overdue, assigned_manager=MANAGER)
    _row(session, follow_up_due_at=overdue, assigned_manager=MANAGER, category="ACCESS_ISSUE")
    _row(session, follow_up_due_at=overdue)  # unassigned: its own bucket
    session.commit()

    report = svc.send_due_reminders(session, settings, now=T0)
    session.commit()
    assert (report.groups, report.complaints, report.queued) == (2, 3, 2)
    rows = session.query(OutboxRow).all()
    assert len(rows) == 2 and {r.kind for r in rows} == {"EMAIL"}

    # A second sweep the same day adds nothing: the outbox key is the deduplication.
    again = svc.send_due_reminders(session, settings, now=T0 + timedelta(hours=2))
    session.commit()
    assert again.queued == 0 and again.already_queued == 2
    assert session.query(OutboxRow).count() == 2


def test_a_reminder_never_quotes_the_complaint(tmp_db, on):
    """§9.5 forbids the breach record from quoting the breach; the same rule here. A reminder
    that quotes the complaint is a second copy of it, in an inbox, read on a phone."""
    settings, session = tmp_db
    row = _row(
        session,
        follow_up_due_at=T0 - timedelta(days=1),
        assigned_manager=MANAGER,
        subject_type="INDIVIDUAL",
        vendor_id=None,
        subject_role_token="FE-MTK-01",
        category="UNSAFE_PRACTICE",
        severity="HIGH",
        description="Climbed the tower without a harness after being told to stop.",
    )
    session.commit()
    svc.send_due_reminders(session, settings, now=T0)
    session.commit()
    payload = json.loads(session.query(OutboxRow).one().payload_json)
    blob = payload["subject"] + payload["body"]
    for forbidden in ("harness", "UNSAFE_PRACTICE", "HIGH", "FE-MTK-01", "vendor-egypro", row.description):
        assert forbidden not in blob, forbidden
    assert row.id in blob  # a reference, and a count, and nothing else
    assert "1 confidential complaint(s)" in blob
    assert payload["count"] == 1 and payload["complaint_ids"] == [row.id]


def test_acknowledging_restarts_the_clock_so_nothing_is_chased_twice(client, on):
    _as(client, "noc_analyst", FILER)
    filed = _file(client)
    _as(client, "duty_manager", MANAGER)
    ack = client.post(f"/api/v1/complaints/{filed['id']}/acknowledge", json={})
    assert ack.status_code == 200
    body = ack.json()
    assert body["status"] == svc.ACKNOWLEDGED and body["acknowledged_at"]
    assert body["follow_up_due_at"] > filed["follow_up_due_at"]


def test_a_resolved_complaint_is_never_chased(tmp_db, on):
    settings, session = tmp_db
    _row(session, follow_up_due_at=T0 - timedelta(days=5), status=svc.RESOLVED)
    _row(session, follow_up_due_at=T0 - timedelta(days=5), status=svc.WITHDRAWN)
    session.commit()
    assert svc.send_due_reminders(session, settings, now=T0).complaints == 0


# --------------------------------------------------------------------------
# Retention (§9.4, DPA s.25(g))
# --------------------------------------------------------------------------


def test_the_licence_three_year_floor_does_not_reach_this_table():
    """§9.4 confines Condition 12.2 to the network/QoS class, and purge "never touches the
    3-year network facts". A complaint is not an operational record: 24 months is a ceiling
    here, not a number in tension with a floor."""
    from noc_agents.services import housekeeping

    policy = housekeeping.load_policy()
    rule = policy.classes["relationship_complaints"]
    assert rule.action == "pseudonymise"  # the row survives; the free text does not
    assert rule.days == svc.RETENTION_DAYS == 730
    assert rule.licence_floor is False
    assert rule.days < housekeeping.LICENCE_FLOOR_DAYS  # and the policy still validates
    assert housekeeping.validate_policy(policy) == []


def test_retention_is_dry_run_by_default_and_reduces_free_text_when_applied(tmp_db, on):
    settings, session = tmp_db
    row = _row(
        session,
        retention_until=T0 - timedelta(days=1),
        status=svc.RESOLVED,
        resolution="Vendor briefed; SLA reminder issued.",
    )
    session.commit()
    original = row.description

    dry = svc.pseudonymise_expired(session, settings, now=T0)
    assert dry.applied is False and dry.matched == 1 and dry.changed == 0
    assert row.description == original and row.pseudonymised_at is None

    applied = svc.pseudonymise_expired(session, settings, now=T0, apply=True)
    session.commit()
    assert applied.changed == 1
    assert row.pseudonymised_at == T0
    assert original not in row.description
    assert "NO_SHOW" in row.description and "Vendor briefed" in row.description
    assert "s.25(g)" in row.description

    # Idempotent: the second pass finds nothing, so the text is never reduced twice.
    second = svc.pseudonymise_expired(session, settings, now=T0, apply=True)
    assert second.matched == 0 and second.changed == 0


def test_the_generic_housekeeping_pass_cannot_reduce_a_complaint_and_never_corrupts_one(tmp_db, on):
    """Why this lane owns its own retention pass.

    ``config/retention.yaml`` classes this table ``pseudonymise`` with an empty
    ``role_tokens`` map and ``description`` as the only scrubbed column. The generic engine
    then scrubs that text with an *empty* ``NameMap``, which removes exactly e-mails and
    MSISDNs — the two things the intake validator already refuses — so the rule is a
    provable no-op on a complaint, whatever its age. §7.8.3 wants the description reduced to
    its category and outcome, which needs the row's own fields; that is
    :func:`services.complaints.pseudonymise_expired`.

    What is pinned here is the safety property, not the gap: running the shared housekeeping
    pass over an expired complaint must never mangle it or half-reduce it. Whether the
    engine skips the table or walks it, the text is the lane's to change.
    """
    from noc_agents.services import housekeeping

    settings, session = tmp_db
    row = _row(session, retention_until=T0 - timedelta(days=400), filed_at=T0 - timedelta(days=900))
    session.commit()
    original = row.description
    housekeeping.pseudonymise_personal_fields(session, settings, now=T0, apply=True)
    session.commit()
    assert row.description == original and row.pseudonymised_at is None
    svc.pseudonymise_expired(session, settings, now=T0, apply=True)
    session.commit()
    assert row.description != original and row.pseudonymised_at == T0


def test_retention_leaves_a_complaint_inside_its_window_alone(tmp_db, on):
    settings, session = tmp_db
    row = _row(session, retention_until=T0 + timedelta(days=30))
    session.commit()
    assert svc.pseudonymise_expired(session, settings, now=T0, apply=True).matched == 0
    assert row.pseudonymised_at is None


def test_the_row_and_its_counts_survive_the_reduction(tmp_db, on):
    """§9.4 says pseudonymise, not delete: the category counts are what the operator's
    quarterly statistics (Consumer Protection Regs 2010 reg 7(13)) are built from."""
    settings, session = tmp_db
    _row(session, retention_until=T0 - timedelta(days=1))
    session.commit()
    svc.pseudonymise_expired(session, settings, now=T0, apply=True)
    session.commit()
    assert session.query(RelationshipComplaintRow).count() == 1
    counts = svc.stats(session, operator_id="safaricom", actor="Anyone", all_complaints=True)
    assert counts["by_category"]["NO_SHOW"] == 1


def test_the_filing_stamps_both_clocks_from_the_same_moment(client, on):
    _as(client, "noc_analyst", FILER)
    filed = _file(client)
    assert filed["retention_until"] and filed["follow_up_due_at"]
    session = _api_session()
    try:
        row = session.query(RelationshipComplaintRow).one()
        assert (row.retention_until - row.filed_at).days == svc.RETENTION_DAYS
    finally:
        session.close()


# --------------------------------------------------------------------------
# Subject access (DPA s.26)
# --------------------------------------------------------------------------


def test_subject_access_returns_what_is_held_and_withholds_who_complained(client, on):
    """s.26 gives the subject what is held about them; naming the complainant to them would
    make the intake unusable, so the export says it is withheld rather than pretending."""
    session = _api_session()
    try:
        subject = _register_subject(session)
        session.commit()
        ref = subject.ref
    finally:
        session.close()
    _as(client, "noc_analyst", FILER)
    _file(
        client,
        subject_type="INDIVIDUAL",
        vendor_id=None,
        subject_person_ref=ref,
        category="LATE_ARRIVAL",
        description="Arrived four hours after the SLA window closed; Grace Wanjiru logged it.",
    )
    _file(client)  # a vendor complaint, about nobody: must not appear in the export

    _as(client, "legal", "Legal Counsel")
    body = client.get(f"/api/v1/complaints/subject-access/{ref}").json()
    assert body["count"] == 1
    item = body["complaints"][0]
    assert item["category"] == "LATE_ARRIVAL"
    assert "Arrived four hours" in item["allegation"]
    assert FILER not in json.dumps(body)  # the complainant is scrubbed out of the text too
    assert body["complainant_identity_withheld"] is True
    assert "unstructured_text_not_searched" in body
    assert "s.26" in body["legal_basis"]


def test_subject_access_is_indexed_rather_than_a_free_text_scan():
    """The right has to be answerable in practice, not just in principle."""
    indexes = {ix.name: [c.name for c in ix.columns] for ix in RelationshipComplaintRow.__table__.indexes}
    assert ["operator_id", "subject_person_ref"] == indexes["ix_complaints_subject_ref"]


def test_an_unknown_subject_reference_is_a_404(client, on):
    _as(client, "legal", "Legal Counsel")
    assert client.get("/api/v1/complaints/subject-access/not-a-ref").status_code == 404


def test_subject_access_records_that_it_was_answered(client, on):
    session = _api_session()
    try:
        subject = _register_subject(session)
        session.commit()
        ref = subject.ref
    finally:
        session.close()
    _as(client, "legal", "Legal Counsel")
    assert client.get(f"/api/v1/complaints/subject-access/{ref}").status_code == 200
    session = _api_session()
    try:
        row = session.query(AuditRow).filter(AuditRow.action == svc.AUDIT_SUBJECT_ACCESS).one()
        assert row.entity_id == ref and "s.26" in row.rationale
    finally:
        session.close()


# --------------------------------------------------------------------------
# The audit trail, and what must never be in it
# --------------------------------------------------------------------------


def test_every_view_and_edit_writes_an_audit_row_that_never_quotes_the_complaint(client, on):
    """§7.8.3 wants the audit; §9.3 gives ``audit_events`` to MORE roles than the complaint
    itself, so a payload that copied the description would widen its audience."""
    _as(client, "noc_analyst", FILER)
    filed = _file(client, description="Left the cabinet unlocked overnight; second time.")
    _as(client, "duty_manager", MANAGER)
    client.get(f"/api/v1/complaints/{filed['id']}")
    client.get("/api/v1/complaints")
    client.post(f"/api/v1/complaints/{filed['id']}/assign", json={"manager": MANAGER})
    client.post(f"/api/v1/complaints/{filed['id']}/acknowledge", json={})
    client.post(f"/api/v1/complaints/{filed['id']}/resolve", json={"resolution": "Locks re-issued."})

    session = _api_session()
    try:
        rows = session.query(AuditRow).filter(AuditRow.entity_type == svc.ENTITY).all()
        actions = {row.action for row in rows}
        assert {
            svc.AUDIT_FILED,
            svc.AUDIT_VIEWED,
            svc.AUDIT_LISTED,
            svc.AUDIT_ASSIGNED,
            svc.AUDIT_ACKNOWLEDGED,
            svc.AUDIT_RESOLVED,
        } <= actions
        blob = " ".join((row.payload_json or "") + (row.rationale or "") for row in rows)
        assert "cabinet unlocked" not in blob and "Locks re-issued" not in blob
    finally:
        session.close()


# --------------------------------------------------------------------------
# The firewall: complaints never reach a scorecard or an individual metric
# --------------------------------------------------------------------------


def test_nothing_outside_this_lane_reads_the_complaint_tables():
    """§7.8.6 and §5.3.21: "complaints are **never** an input to scorecards or individual
    metrics". The static sweep ``test_pir.py`` uses, applied to this lane's two tables."""
    allowed = {
        "db/models_complaints.py",
        "db/models_all.py",
        "services/complaints.py",
        "api/routers/complaints.py",
    }
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel in allowed:
            continue
        text = path.read_text(encoding="utf-8")
        for needle in ("models_complaints", "RelationshipComplaintRow", "SubjectPersonRow"):
            # A comment or docstring mentioning the lane is fine; an import or a query is not.
            for line in text.splitlines():
                stripped = line.strip()
                if needle in stripped and (stripped.startswith("from ") or stripped.startswith("import ")):
                    offenders.append(f"{rel}: {stripped}")
    assert offenders == [], offenders


def test_the_retention_policy_still_classifies_the_lane_s_tables():
    """The YAML is the document Legal signs; the table names must stay in step with it."""
    from noc_agents.services import housekeeping

    policy = housekeeping.load_policy()
    assert policy.tables["relationship_complaints"].class_name == "relationship_complaints"
    assert policy.tables["subject_persons"].class_name == "personal_staff_vendor"
