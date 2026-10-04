"""The action agent's tools: validation, policy limits, idempotency, and the NOC incident link."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from noc_agents.db.models import IncidentRow, new_id, utcnow
from noc_agents.db.models_support import SupportComplaintRow, SupportToolCallRow
from noc_agents.support.accounts import load_accounts
from noc_agents.support.evals import IsolatedDatabases
from noc_agents.support.policy import load_policy
from noc_agents.support.text import normalise_msisdn
from noc_agents.support.tools import TOOLS, ToolEnv, run_tool

OP = "safaricom"
POLICY = load_policy()
BOOK = load_accounts()


@pytest.fixture()
def session():
    databases = IsolatedDatabases(OP)  # every table, plus the open rain-storm hub incidents
    with databases.session() as s:
        yield s
    databases.close()


def _env(session, msisdn: str, *, account: bool = True, operator_id: str = OP) -> ToolEnv:
    e164 = normalise_msisdn(msisdn)
    return ToolEnv(session, operator_id, e164, BOOK.find(e164) if account else None, POLICY, utcnow())


def _done(session, msisdn: str, tool: str, subject_ref: str | None, *, days_ago: float = 0, status: str = "ok",
          operator_id: str = OP) -> None:
    """A prior complaint from ``msisdn`` on which ``tool`` already ran (or, with a status, stands)."""
    complaint = SupportComplaintRow(operator_id=operator_id, ref=f"CMP-{new_id()[:8]}", msisdn=normalise_msisdn(msisdn),
                                    msisdn_masked="x", body_hash="h", sla_due_at=utcnow())
    session.add(complaint)
    session.flush()
    session.add(SupportToolCallRow(complaint_id=complaint.id, tool=tool, status=status, subject_ref=subject_ref,
                                   at=utcnow() - timedelta(days=days_ago)))
    session.flush()


def test_the_registry_is_exactly_the_contracts_tool_table():
    assert set(TOOLS) == {"lookup_account", "issue_refund", "reverse_mpesa", "recredit_bundle", "link_incident",
                          "update_ticket", "reset_network_settings"}


# ------------------------------------------------------------------------- lookup_account


def test_lookup_account_reads_the_fixture_and_never_fails(session):
    found = run_tool("lookup_account", _env(session, "0700000412"), {})
    assert found.status == "ok" and found.result["account_ref"] == "ACC-100412"
    assert found.result["recent_transactions"][0]["code"] == "SJK4H7QW2L"
    missing = run_tool("lookup_account", _env(session, "0711999999"), {})
    assert missing.status == "ok" and missing.result == {"found": False, "msisdn": "+254 7•• ••• 999"}


# --------------------------------------------------------------------------- issue_refund


def test_a_refund_within_the_auto_limit_runs(session):
    out = run_tool("issue_refund", _env(session, "0700000678"), {"charge_id": "CHG-678-02"})
    assert out.status == "ok" and out.result["amount_kes"] == 99 and out.subject_ref == "CHG-678-02"
    assert "500" in out.policy


def test_a_refund_over_the_limit_needs_approval_and_runs_once_approved(session):
    env = _env(session, "0700000789")
    assert run_tool("issue_refund", env, {"charge_id": "CHG-789-01"}).status == "needs_approval"
    approved = run_tool("issue_refund", env, {"charge_id": "CHG-789-01"}, approved=True)
    assert approved.status == "ok" and approved.result["amount_kes"] == 1500 and "approved" in approved.policy


def test_a_charge_is_never_refunded_twice(session):
    _done(session, "0700000678", "issue_refund", "CHG-678-02")
    out = run_tool("issue_refund", _env(session, "0700000678"), {"charge_id": "CHG-678-02"})
    assert out.status == "refused" and not out.fallback and "already" in out.policy


def test_a_delivered_service_is_not_refundable_but_the_resolver_may_explain(session):
    out = run_tool("issue_refund", _env(session, "0700000678"), {"charge_id": "CHG-678-01"})
    assert out.status == "refused" and out.fallback


@pytest.mark.parametrize("args", [{}, {"charge_id": "CHG-NOPE"}, {"charge_id": "CHG-678-02", "amount_kes": 5000},
                                  {"charge_id": "CHG-678-02", "amount_kes": -1}])
def test_refund_arguments_are_validated(session, args):
    out = run_tool("issue_refund", _env(session, "0700000678"), args)
    assert out.status == "refused" and out.policy.startswith("invalid arguments")


# -------------------------------------------------------------------------- reverse_mpesa


def test_a_recent_small_reversal_runs(session):
    out = run_tool("reverse_mpesa", _env(session, "0700000412"), {"transaction_code": "sjk4h7qw2l"})
    assert out.status == "ok" and out.result["amount_kes"] == 1500 and out.subject_ref == "SJK4H7QW2L"


@pytest.mark.parametrize(
    "msisdn,code,why",
    [
        ("0700000118", "SHR2M9PL4Q", "above the KES 5,000 auto limit"),
        ("0700000233", "SGT7KD2PQ1", "40h old"),
        ("0700001245", "SKL3N8RT5V", "already withdrawn"),
    ],
)
def test_a_reversal_past_any_limit_needs_approval(session, msisdn, code, why):
    out = run_tool("reverse_mpesa", _env(session, msisdn), {"transaction_code": code})
    assert out.status == "needs_approval" and why in out.policy
    assert run_tool("reverse_mpesa", _env(session, msisdn), {"transaction_code": code}, approved=True).status == "ok"


def test_a_reversal_is_never_paid_twice(session):
    _done(session, "0700000412", "reverse_mpesa", "SJK4H7QW2L")
    out = run_tool("reverse_mpesa", _env(session, "0700000412"), {"transaction_code": "SJK4H7QW2L"})
    assert out.status == "refused" and "already been reversed" in out.policy
    # ...not even with a person's approval
    assert run_tool("reverse_mpesa", _env(session, "0700000412"), {"transaction_code": "SJK4H7QW2L"}, approved=True).status == "refused"


@pytest.mark.parametrize("msisdn,operator_id", [("0700000999", OP), ("0700000412", "airtel")])
def test_the_same_code_or_charge_done_for_another_number_or_operator_is_not_already_done(session, msisdn, operator_id):
    _done(session, msisdn, "reverse_mpesa", "SJK4H7QW2L", operator_id=operator_id)
    _done(session, msisdn, "issue_refund", "CHG-567-01", operator_id=operator_id)
    _done(session, msisdn, "recredit_bundle", "BND-345-01", days_ago=1, operator_id=operator_id)
    assert run_tool("reverse_mpesa", _env(session, "0700000412"), {"transaction_code": "SJK4H7QW2L"}).status == "ok"
    assert run_tool("issue_refund", _env(session, "0700000567"), {"charge_id": "CHG-567-01"}).status == "ok"
    assert run_tool("recredit_bundle", _env(session, "0700000345"), {"bundle_id": "BND-345-01"}).status == "ok"


def test_a_call_already_waiting_for_approval_blocks_a_second_one_but_not_its_own_approval(session):
    _done(session, "0700000118", "reverse_mpesa", "SHR2M9PL4Q", status="needs_approval")
    waiting = run_tool("reverse_mpesa", _env(session, "0700000118"), {"transaction_code": "SHR2M9PL4Q"}, approved=True)
    assert waiting.status == "refused" and "already waiting for a person's approval" in waiting.policy
    parked = session.scalar(select(SupportToolCallRow).where(SupportToolCallRow.status == "needs_approval"))
    own = ToolEnv(session, OP, "+254700000118", BOOK.find("+254700000118"), POLICY, utcnow(), approving_call_id=parked.id)
    assert run_tool("reverse_mpesa", own, {"transaction_code": "SHR2M9PL4Q"}, approved=True).status == "ok"


def test_only_a_transfer_sent_from_this_number_can_be_reversed(session):
    assert run_tool("reverse_mpesa", _env(session, "0700000412"), {"transaction_code": "SJK2P9LM4R"}).status == "refused"  # a paybill
    assert run_tool("reverse_mpesa", _env(session, "0700000412"), {"transaction_code": "TQX9P2LM7K"}).status == "refused"
    no_account = run_tool("reverse_mpesa", _env(session, "0711999999"), {"transaction_code": "TQX9P2LM7K"})
    assert no_account.status == "refused" and not no_account.fallback and "no account" in no_account.policy
    bad = run_tool("reverse_mpesa", _env(session, "0700000412"), {"transaction_code": "SHORT"})
    assert bad.status == "refused" and "10-character" in bad.policy


# ------------------------------------------------------------------------ recredit_bundle


def test_a_first_recredit_runs(session):
    out = run_tool("recredit_bundle", _env(session, "0700000345"), {"bundle_id": "BND-345-01"})
    assert out.status == "ok" and out.result["name"] == "Weekly 2GB"


def test_one_recredit_per_thirty_days_from_the_fixture_or_from_the_desks_own_record(session):
    from_fixture = run_tool("recredit_bundle", _env(session, "0700000456"), {"bundle_id": "BND-456-01"})
    assert from_fixture.status == "refused" and not from_fixture.fallback and "10 days ago" in from_fixture.policy
    _done(session, "0700000345", "recredit_bundle", "BND-345-00", days_ago=12)
    assert run_tool("recredit_bundle", _env(session, "0700000345"), {"bundle_id": "BND-345-01"}).status == "refused"


def test_an_old_recredit_does_not_count(session):
    _done(session, "0700000345", "recredit_bundle", "BND-345-00", days_ago=31)
    assert run_tool("recredit_bundle", _env(session, "0700000345"), {"bundle_id": "BND-345-01"}).status == "ok"


def test_a_bundle_that_ran_its_course_is_not_eligible_and_the_resolver_may_explain(session):
    from noc_agents.support.accounts import Account, Bundle

    base = BOOK.find("+254700000345")
    used = Account(**{**base.model_dump(), "bundles": [Bundle(**{**base.bundles[0].model_dump(), "status": "expired"})]})
    env = ToolEnv(session, OP, base.msisdn, used, POLICY, utcnow())
    out = run_tool("recredit_bundle", env, {"bundle_id": "BND-345-01"})
    assert out.status == "refused" and out.fallback


# -------------------------------------------------------------------------- link_incident


def test_link_incident_finds_the_storm_hub_by_site_name(session):
    out = run_tool("link_incident", _env(session, "0700001023", account=False), {"place": "Nakuru", "regions": ["RFT"]})
    assert out.status == "ok" and out.result["found"] and out.result["site_name"] == "Nakuru Rift HUB"
    assert out.result["match"] == "site name" and out.subject_ref == out.result["incident_id"]


def test_link_incident_falls_back_to_the_region_and_prefers_the_biggest_outage(session):
    out = run_tool("link_incident", _env(session, "0700001023"), {"place": "kayole", "regions": ["NBI_E"]})
    assert out.result["site_name"] == "Embakasi East Aggregation HUB" and out.result["match"] == "region"


def test_link_incident_ignores_closed_incidents_children_and_other_operators(session):
    for inc in session.query(IncidentRow).filter(IncidentRow.site_name == "Nakuru Rift HUB"):
        inc.status = "RESTORED"
    session.add(IncidentRow(operator_id="airtel", incident_number="AIR000001", status="NEW", site_id="X",
                            site_name="Nakuru Airtel Hub", region_code="RFT", correlation_fingerprint="x"))
    session.add(IncidentRow(operator_id=OP, incident_number="INC000099", status="NEW", site_id="Y",
                            site_name="Nakuru child eNodeB", region_code="RFT", correlation_fingerprint="y",
                            parent_incident_id="some-parent"))
    session.flush()
    out = run_tool("link_incident", _env(session, "0700001023"), {"place": "nakuru", "regions": ["RFT"]})
    assert out.result["site_name"] == "Eldoret Rift HUB"  # the only open, own, top-level ticket left in the region


def test_no_matching_incident_is_ok_with_a_fallback(session):
    out = run_tool("link_incident", _env(session, "0700001023"), {"place": "kisumu", "regions": ["WNY"]})
    assert out.status == "ok" and out.fallback and out.result["found"] is False


# ------------------------------------------------------------------ update_ticket and settings


def test_update_ticket_validates_its_status_and_note(session):
    env = _env(session, "0700000412")
    assert run_tool("update_ticket", env, {"status": "action_taken", "note": "Reversed."}).status == "ok"
    assert run_tool("update_ticket", env, {"status": "done", "note": "x"}).status == "refused"
    assert run_tool("update_ticket", env, {"status": "resolved", "note": "x" * 1001}).status == "refused"


def test_reset_network_settings_records_the_sms_and_never_sends_it(session):
    out = run_tool("reset_network_settings", _env(session, "0110000112"), {})
    assert out.status == "ok" and out.result["device"] == "Samsung Galaxy A05"
    assert out.result["to"] == "+254 1•• ••• 112" and "never sends" in out.result["delivery"]


def test_an_unknown_tool_is_refused_and_an_exception_is_a_failure_not_a_crash(session, monkeypatch):
    assert run_tool("format_disk", _env(session, "0700000412"), {}).status == "refused"
    from noc_agents.support import tools

    def boom(env, args, approved=False):
        raise RuntimeError("fixture store down")

    monkeypatch.setitem(tools.TOOLS, "lookup_account", tools.ToolSpec("lookup_account", "", "", boom))
    out = run_tool("lookup_account", _env(session, "0700000412"), {})
    assert out.status == "failed" and "RuntimeError" in out.policy
