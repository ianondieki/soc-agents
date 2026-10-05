"""POST /api/v1/shifts/handover is behind an approval now (spec §5.3.12, defect #30).

Two claims are proven here, and the second one is the defect:

* the ``watch_count`` contract is untouched — the package the route returns is the same
  dict ``build_handover`` always returned, with a ``hitl`` block added beside it;
* **no handover email leaves before an approval.** Not "is not sent by this code path":
  the SMTP call itself is replaced by a recorder, so every assertion below is about
  whether the one function that can put a message on the wire was ever entered.

The gate leans on the dispatcher rather than adding a second check of its own
(``outbox.dispatch`` refuses any channel row that requires approval and has none), so
that refusal is exercised directly too — a handover row forced to PENDING without an
approval is still refused.
"""

from __future__ import annotations

import importlib
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from noc_agents.adapters.email_smtp import EmailResult
from noc_agents.db.models import HitlTaskRow, IncidentRow, OutboxRow, get_session
from noc_agents.realtime.hub import hub

HUB_EVENT = {
    "site_id": "SFC-NBIE-HUB-EMB",
    "site_name": "Embakasi East Aggregation HUB",
    "site_type": "HUB",
    "region_code": "NBI_E",
    "alarm_code": "POWER_GRID_FAIL",
    "failure_domain": "POWER",
    "users_affected": 450000,
}
SUPERVISOR = {"resolved_by": "Supervisor A"}


@pytest.fixture()
def sent(monkeypatch):
    """Replace the ONE function that can transmit an email with a recorder.

    ``outbox._transmit_email`` calls ``notify.transmit_email`` through the module object,
    so patching the attribute catches every path into SMTP — the route's own drain, the
    post-commit drain and a manual ``drain_outbox`` alike.
    """
    calls: list[dict] = []
    import noc_agents.services.notify as notify

    def _recorder(payload: dict) -> EmailResult:
        calls.append(payload)
        return EmailResult(ok=True, mode="mock", detail="recorded by the test", to=[])

    monkeypatch.setattr(notify, "transmit_email", _recorder)
    return calls


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "handover.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")
    monkeypatch.setenv("OPERATOR_PROFILE", "safaricom")

    import noc_agents.config as cfg
    import noc_agents.db.models as models
    import noc_agents.main as main

    cfg.clear_settings_cache()
    models._engine = None
    models.SessionLocal = None
    importlib.reload(main)
    hub._history.clear()
    with TestClient(main.app) as c:
        yield c
    hub._history.clear()


def _read(fn):
    session = get_session()
    try:
        return fn(session)
    finally:
        session.close()


def _drain():
    from noc_agents.graph.pipeline import drain_outbox

    session = get_session()
    try:
        return drain_outbox(session)
    finally:
        session.close()


def _handover_rows(session):
    return session.scalars(
        select(OutboxRow).where(OutboxRow.kind == "EMAIL", OutboxRow.incident_id.is_(None))
    ).all()


# ---------------------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------------------


def test_handover_raises_a_task_and_holds_the_mail(client, sent):
    inc = client.post("/api/v1/events", json=HUB_EVENT).json()["incident"]

    ho = client.post("/api/v1/shifts/handover").json()

    # the existing contract, unchanged
    assert ho["watch_count"] >= 1
    assert ho["watch_count"] == len(ho["incidents"])
    assert ho["incidents"][0]["incident_number"] == inc["incident_number"]
    assert "NOC Handover" in ho["subject"]

    # the gate
    assert ho["hitl"]["required"] is True
    assert ho["hitl"]["status"] == "HELD"
    assert ho["hitl"]["task_id"] and ho["hitl"]["alert_id"] and ho["hitl"]["outbox_id"]
    assert ho["email"]["status"] == "HELD"
    assert ho["email"]["ok"] is False

    task = _read(lambda s: s.get(HitlTaskRow, ho["hitl"]["task_id"]))
    assert (task.task_type, task.status) == ("APPROVE_HANDOVER", "PENDING")
    assert task.entity_type == "handover"
    assert task.entity_id == client.get("/api/v1/shifts/current").json()["shift_id"]
    assert task.incident_id == inc["id"]  # the anchor: hitl_tasks.incident_id is NOT NULL
    assert task.created_by == "agent:ShiftHandoverAgent"  # raiser != approver stays satisfiable

    row = _read(lambda s: s.get(OutboxRow, ho["hitl"]["outbox_id"]))
    assert (row.status, row.requires_hitl, row.approved_at) == ("HELD", 1, None)
    assert row.hitl_task_id == task.id
    assert row.alert_id == ho["hitl"]["alert_id"]

    # THE point: the route ran a drain (sync drain is on by default) and nothing was sent.
    assert sent == []
    assert _drain() and sent == []  # and a second, explicit pass sends nothing either
    assert _read(lambda s: s.get(OutboxRow, row.id)).status == "HELD"  # never even claimed


def test_the_envelope_is_an_internal_ack_to_the_noc_shift(client, sent):
    client.post("/api/v1/events", json=HUB_EVENT)
    ho = client.post("/api/v1/shifts/handover").json()

    task = _read(lambda s: s.get(HitlTaskRow, ho["hitl"]["task_id"]))
    envelope = task.proposed_payload["envelope"]
    assert envelope["msg_type"] == "ACK"
    assert envelope["scope"] == "INTERNAL"
    assert [a["audience"] for a in envelope["audiences"]] == ["NOC_SHIFT"]
    assert [a["channels"] for a in envelope["audiences"]] == [["EMAIL"]]
    assert envelope["governance"]["requires_hitl"] is True
    assert envelope["governance"]["approved_by"] is None
    assert envelope["content"]["en"]["headline"] == ho["subject"][:160]

    stored = _read(lambda s: s.get(OutboxRow, ho["hitl"]["outbox_id"]))
    assert json.loads(stored.envelope_json)["alert_id"] == envelope["alert_id"]


def test_the_dispatcher_refuses_a_handover_row_that_never_got_its_approval(client, sent):
    """The gate adds no second check — it relies on this one, so this one is tested."""
    client.post("/api/v1/events", json=HUB_EVENT)
    ho = client.post("/api/v1/shifts/handover").json()

    def _force_pending(s):
        row = s.get(OutboxRow, ho["hitl"]["outbox_id"])
        row.status = "PENDING"  # a bug, a bad migration, a hand edit: approval still missing
        s.commit()

    _read(_force_pending)
    _drain()

    assert sent == []
    assert _read(lambda s: s.get(OutboxRow, ho["hitl"]["outbox_id"])).status == "REJECTED_UNAPPROVED"


# ---------------------------------------------------------------------------------------
# The release
# ---------------------------------------------------------------------------------------


def test_approval_releases_the_handover_through_the_outbox(client, sent):
    client.post("/api/v1/events", json=HUB_EVENT)
    ho = client.post("/api/v1/shifts/handover").json()
    assert sent == []

    r = client.post(f"/api/v1/hitl/{ho['hitl']['task_id']}/approve", json=SUPERVISOR)
    assert r.status_code == 200, r.text

    assert [p["subject"] for p in sent] == [ho["subject"]]  # exactly one, and it is the handover
    row = _read(lambda s: s.get(OutboxRow, ho["hitl"]["outbox_id"]))
    assert row.status == "SENT"
    assert row.approved_by == "Supervisor A"
    assert row.approved_at is not None
    task = _read(lambda s: s.get(HitlTaskRow, ho["hitl"]["task_id"]))
    assert (task.status, task.resolved_by) == ("APPROVED", "Supervisor A")
    # and the anchor incident's own broadcast gate is none the wiser
    assert _read(
        lambda s: s.scalar(select(HitlTaskRow).where(HitlTaskRow.task_type == "APPROVE_BROADCAST"))
    ).status == "PENDING"

    _drain()
    assert len(sent) == 1  # idempotent: a SENT row is never claimed again


def test_the_two_gates_on_the_anchor_incident_do_not_release_each_other(client, sent):
    """Approving the P1 broadcast must not drag the handover out with it.

    The handover task is anchored to that same incident, so the two gates meet on one row
    set. They stay apart because the handover's outbox row carries no ``incident_id`` and
    is addressed by ``hitl_task_id`` alone: ``outbox.release_held``, whose blast radius is
    the incident (it SUPPRESSES every other HELD row there), cannot see it, and
    ``release_handover`` cannot see the broadcast's rows.
    """
    client.post("/api/v1/events", json=HUB_EVENT)
    ho = client.post("/api/v1/shifts/handover").json()
    broadcast_task = _read(
        lambda s: s.scalar(select(HitlTaskRow).where(HitlTaskRow.task_type == "APPROVE_BROADCAST"))
    )

    r = client.post(f"/api/v1/hitl/{broadcast_task.id}/approve", json=SUPERVISOR)
    assert r.status_code == 200, r.text

    assert sent, "the approved broadcast should have been transmitted"
    assert ho["subject"] not in [p["subject"] for p in sent]  # the handover was not dragged out
    row = _read(lambda s: s.get(OutboxRow, ho["hitl"]["outbox_id"]))
    assert row.status == "HELD"  # neither released nor SUPPRESSED as a superseded draft
    assert _read(lambda s: s.get(HitlTaskRow, ho["hitl"]["task_id"])).status == "PENDING"


# ---------------------------------------------------------------------------------------
# The flag, and the case with nothing to anchor on
# ---------------------------------------------------------------------------------------


def test_flag_off_restores_the_direct_send(client, sent, monkeypatch):
    monkeypatch.setenv("HANDOVER_REQUIRES_HITL", "false")
    client.post("/api/v1/events", json=HUB_EVENT)

    ho = client.post("/api/v1/shifts/handover").json()

    assert ho["hitl"]["required"] is False
    assert ho["email"]["status"] == "SENT"
    assert [p["subject"] for p in sent] == [ho["subject"]]
    assert _read(lambda s: s.scalars(select(HitlTaskRow).where(HitlTaskRow.task_type == "APPROVE_HANDOVER")).all()) == []


def test_the_default_is_on(monkeypatch):
    from noc_agents.services.handover import handover_requires_hitl

    monkeypatch.delenv("HANDOVER_REQUIRES_HITL", raising=False)
    assert handover_requires_hitl() is True
    monkeypatch.setenv("HANDOVER_REQUIRES_HITL", "")  # unset-but-present is still on
    assert handover_requires_hitl() is True
    monkeypatch.setenv("HANDOVER_REQUIRES_HITL", "off")
    assert handover_requires_hitl() is False


# ---------------------------------------------------------------------------------------
# Exec brief refresh (spec §5.3.8, defect #24)
# ---------------------------------------------------------------------------------------
# These live in this file because HITL approve — one of the three call sites — is the
# handover approval this file already drives, and because only two new test files were in
# scope for this wave. The other two sites (work note, close) are covered here too rather
# than left untested.


def _brief(inc_id: str) -> str:
    from noc_agents.db.models import IncidentBriefRow

    return _read(
        lambda s: s.scalar(
            select(IncidentBriefRow)
            .where(IncidentBriefRow.incident_id == inc_id)
            .order_by(IncidentBriefRow.updated_at.desc())
        ).body
    )


@pytest.fixture()
def stamped_brief(monkeypatch):
    """Make each re-compose distinguishable, so "was it refreshed?" has a yes/no answer.

    The composer is patched rather than the incident, because today's ``compose_brief``
    template prints no status and no restore time: a real close changes the incident but
    not one character of the brief text. That gap is reported, not papered over — this
    fixture asks only whether the refresh RAN where it was wired.
    """
    import noc_agents.services.lifecycle as lifecycle

    counter = {"n": 0}

    def _stamped(cfg, inc):
        counter["n"] += 1
        return f"brief v{counter['n']} for {inc.incident_number} ({inc.priority}/{inc.status})"

    monkeypatch.setattr(lifecycle, "compose_brief", _stamped)
    return counter


def test_a_work_note_refreshes_the_brief(client, stamped_brief):
    inc = client.post("/api/v1/events", json=HUB_EVENT).json()["incident"]
    before = _brief(inc["id"])

    r = client.post(
        f"/api/v1/incidents/{inc['id']}/notes",
        json={"author": "EGYPRO FE", "author_role": "MSP", "body": "On site, genset refuelling"},
    )
    assert r.status_code == 200

    assert _brief(inc["id"]) != before
    assert _brief(inc["id"]).endswith(f"({inc['priority']}/IN_PROGRESS)")


def test_closing_refreshes_the_brief(client, stamped_brief):
    inc = client.post("/api/v1/events", json=HUB_EVENT).json()["incident"]
    before = _brief(inc["id"])

    client.post(f"/api/v1/incidents/{inc['id']}/close", json={"closed_by": "NOC Lead"})

    assert _brief(inc["id"]) != before
    assert _brief(inc["id"]).endswith("/CLOSED)")


def test_a_hitl_approval_refreshes_the_brief(client, sent):
    """No stamping needed here: a priority override changes the real brief text."""
    inc = client.post("/api/v1/events", json=HUB_EVENT).json()["incident"]
    before = _brief(inc["id"])
    assert f"{inc['incident_number']} ({inc['priority']})" in before
    raised = "P1" if inc["priority"] != "P1" else "P2"
    task = _read(lambda s: s.scalar(select(HitlTaskRow).where(HitlTaskRow.task_type == "APPROVE_BROADCAST")))

    r = client.post(
        f"/api/v1/hitl/{task.id}/approve",
        json={"resolved_by": "Supervisor A", "overrides": {"priority": raised}},
    )
    assert r.status_code == 200, r.text

    after = _brief(inc["id"])
    assert after != before
    assert f"{inc['incident_number']} ({raised})" in after


def test_the_brief_count_never_moves(client, stamped_brief):
    """Refresh, not re-publication: the §2.1 register pins one brief per incident."""
    from noc_agents.db.models import IncidentBriefRow

    inc = client.post("/api/v1/events", json=HUB_EVENT).json()["incident"]
    client.post(
        f"/api/v1/incidents/{inc['id']}/notes",
        json={"author": "EGYPRO FE", "author_role": "MSP", "body": "still working"},
    )
    client.post(f"/api/v1/incidents/{inc['id']}/close", json={"closed_by": "NOC Lead"})

    assert _read(lambda s: len(s.scalars(select(IncidentBriefRow)).all())) == 1


def test_an_incident_with_no_brief_does_not_acquire_one(client):
    """``upsert_brief`` refreshes and never inserts — see its docstring for why."""
    from noc_agents.db.models import IncidentBriefRow
    from noc_agents.services.lifecycle import upsert_brief

    inc = client.post("/api/v1/events", json=HUB_EVENT).json()["incident"]
    session = get_session()
    try:
        row = session.get(IncidentRow, inc["id"])
        for b in session.scalars(select(IncidentBriefRow).where(IncidentBriefRow.incident_id == row.id)):
            session.delete(b)
        session.commit()
        assert upsert_brief(session, row) is None
        session.commit()
        assert session.scalars(select(IncidentBriefRow).where(IncidentBriefRow.incident_id == row.id)).all() == []
    finally:
        session.close()


def test_with_nothing_open_the_handover_fails_closed(client, sent):
    """No open incident → no anchor → no task can be raised → nothing is queued or sent.

    ``hitl_tasks.incident_id`` is NOT NULL and task ownership is derived by joining it to
    ``incidents``, so a handover task with no incident cannot be stored or fetched today.
    The route refuses rather than sending an unapproved handover; the schema change that
    would let it raise a real task is a reported product decision, not a guess.
    """
    inc = client.post("/api/v1/events", json=HUB_EVENT).json()["incident"]
    client.post(f"/api/v1/incidents/{inc['id']}/close", json={"closed_by": "NOC"})
    assert _read(lambda s: s.get(IncidentRow, inc["id"]).status) == "CLOSED"

    ho = client.post("/api/v1/shifts/handover").json()

    assert ho["watch_count"] == 0
    assert ho["hitl"]["status"] == "NOT_QUEUED"
    assert ho["hitl"]["task_id"] is None
    assert "no open incident" in ho["hitl"]["blocked_reason"]
    assert ho["email"]["ok"] is False
    assert _read(_handover_rows) == []
    assert sent == []


def _renumber(incident_id: str, number: str) -> None:
    """Give a ticket a number in another style, as a database from before inc9 holds them."""
    session = get_session()
    try:
        session.get(IncidentRow, incident_id).incident_number = number
        session.commit()
    finally:
        session.close()


def test_a_ticket_numbered_in_another_style_never_anchors_the_handover(client, sent):
    """The §6.1 envelope accepts ``INC`` and six digits only, and the handover's envelope is
    built from its anchor: a dated or legacy number at the top of the watchlist used to fail
    the whole route with a 500. The next ticket the envelope accepts anchors it instead."""
    legacy = client.post("/api/v1/events", json=HUB_EVENT).json()["incident"]
    other = client.post(
        "/api/v1/events",
        json={**HUB_EVENT, "site_id": "SFC-RFT-HUB-NKR", "site_name": "Nakuru Rift HUB", "region_code": "RFT", "users_affected": 280000},
    ).json()["incident"]
    _renumber(legacy["id"], "SFC-INC-20260716-00028")

    res = client.post("/api/v1/shifts/handover")

    assert res.status_code == 200
    ho = res.json()
    assert ho["hitl"]["task_id"]
    task = _read(lambda s: s.get(HitlTaskRow, ho["hitl"]["task_id"]))
    assert task.incident_id == other["id"]
    assert task.proposed_payload["anchor_incident_number"] == other["incident_number"]
    assert sent == []


def test_with_only_other_style_numbers_open_the_handover_fails_closed_and_says_why(client, sent):
    inc = client.post("/api/v1/events", json=HUB_EVENT).json()["incident"]
    _renumber(inc["id"], "SFC-INC-20260716-00028")

    res = client.post("/api/v1/shifts/handover")

    assert res.status_code == 200
    ho = res.json()
    assert ho["watch_count"] == 1
    assert ho["hitl"]["status"] == "NOT_QUEUED"
    assert ho["hitl"]["task_id"] is None
    assert "INC and six digits" in ho["hitl"]["blocked_reason"]
    assert _read(_handover_rows) == []
    assert sent == []
