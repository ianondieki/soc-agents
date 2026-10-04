"""The public form is not an account oracle (review finding 1, policy of 2026-10-04).

Whoever types a number on the public form reads the reply and the public view, so neither may
reveal anything the caller did not type. The core check is general rather than a list of
strings: for every probe, every account-only fact in the fixture for that number -- transaction
codes, counterparties, amounts, charge descriptions, bundle names, tier, account reference,
holder name -- must be absent from the public view unless it appears in the caller's own text.
"""

from __future__ import annotations

import json
import re

import pytest

from noc_agents.support import desk, views
from noc_agents.support.accounts import Account, load_accounts
from noc_agents.support.context import default_context
from noc_agents.support.evals import IsolatedDatabases
from noc_agents.support.text import normalise, normalise_msisdn

OP = "safaricom"
CTX = default_context()
BOOK = load_accounts()


@pytest.fixture()
def session():
    databases = IsolatedDatabases(OP)
    with databases.session() as s:
        yield s
    databases.close()


def _file(session, body, msisdn, **kwargs):
    return desk.process_complaint(session, operator_id=OP, body=body, msisdn=msisdn, ctx=CTX, emit_events=False, **kwargs)


def _secrets(account: Account) -> set[str]:
    """Everything the fixture knows about this line that a stranger should not learn from it."""
    found = {account.name, account.account_ref, account.tier, account.plan}
    if account.device:
        found.add(account.device)
    for t in account.transactions:
        found |= {t.code, t.counterparty, f"{t.amount_kes:,}", str(t.amount_kes)}
    for b in account.bundles:
        found |= {b.name, b.id}
    for c in account.charges:
        found |= {c.description, c.id, f"{c.amount_kes:,}", str(c.amount_kes)}
    for word in ("withdrawn", "KES 500", "KES 5,000", "auto limit", "re-credit per", "days ago"):
        found.add(word)  # policy wording that only appears when the account hit a limit
    return {s for s in found if s}


#: Times the view carries for every account ("2026-10-04T13:50:12Z", "by 13:50 EAT"): without
#: removing them a two-digit amount such as KES 50 would "leak" whenever the clock read :50.
_TIMES = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?|\b\d{1,2}:\d{2}\b")


def _leaks(public: dict, account: Account, typed: str) -> list[str]:
    """Secrets present in ``public`` as whole tokens (not inside a uuid or a time) that the caller did not type."""
    blob = _TIMES.sub(" ", json.dumps(public, ensure_ascii=False))
    typed_norm = normalise(typed)
    return sorted(s for s in _secrets(account)
                  if re.search(r"(?<![\w])" + re.escape(s) + r"(?![\w])", blob) and normalise(s) not in typed_norm)


PROBES = [
    # (the reviewer's probes, then the same shapes for every action the desk can take)
    ("I sent 1500 to the wrong number please reverse it", "0700000412"),
    ("please refund me, I was charged wrongly", "0700000567"),
    ("I was charged for a subscription I never subscribed to, refund me", "0700000789"),
    ("I sent money to the wrong number, code SKL3N8RT5V, please reverse", "0700001245"),
    ("my bundle expired early please recredit", "0700000345"),
    ("my bundle was not applied, please recredit it", "0700000456"),
    ("Nimetuma 12000 kwa namba mbaya, code ni SHR2M9PL4Q. Rudisha.", "0700000118"),
    ("Two days ago I sent money to the wrong number, transaction SGT7KD2PQ1, reverse it", "0700000233"),
    ("I was charged twice for my weekly bundle, please refund", "0700000678"),
    ("You people are THIEVES!! My bundle expired early again, useless", "0700000890"),
    ("My calls keep dropping everywhere", "0700000901"),
    ("I bought a new phone, please send me the internet settings", "0110000112"),
]


@pytest.mark.parametrize("text,msisdn", PROBES, ids=[p[1] + ":" + p[0][:24] for p in PROBES])
def test_the_public_view_reveals_nothing_the_caller_did_not_type(session, text, msisdn):
    row = _file(session, text, msisdn).complaint
    public = views.detail(session, row, public=True, policy=CTX.policy)
    account = BOOK.find(normalise_msisdn(msisdn))
    assert _leaks(public, account, text) == []
    # the reply is stored once and sent to whoever typed the number: it must be just as clean
    assert _leaks({"reply": row.reply}, account, text) == []


@pytest.mark.parametrize("text,msisdn", PROBES, ids=[p[1] + ":" + p[0][:24] for p in PROBES])
def test_the_public_view_is_a_fixed_outline_with_only_the_call_that_fixed_it(session, text, msisdn):
    row = _file(session, text, msisdn).complaint
    public = views.detail(session, row, public=True, policy=CTX.policy)
    steps = [(s["agent"], s["action"]) for s in public["steps"]]
    assert steps[:2] == [("intake", "received"), ("triage", "classified")] and len(steps) == 3
    assert steps[2] in {("resolver", "answered"), ("action", "called_tool"), ("escalation", "escalated")}
    assert all(s["detail"] == {} for s in public["steps"])
    for call in public["tool_calls"]:
        assert call["status"] in ("ok", "approved") and call["tool"] not in ("lookup_account", "update_ticket")
        assert call["args"] == {} and call["result"] is None and call["policy"] is None and call["decided_by"] is None
    assert len(public["tool_calls"]) == (1 if row.status == "action_taken" else 0)
    assert public["complaint"]["status"] != "awaiting_approval"


def test_a_code_less_reversal_moves_nothing_and_reads_the_same_with_or_without_a_transfer_on_file(session):
    with_transfer = _file(session, "I sent 1500 to the wrong number please reverse it", "0700000412").complaint
    without = _file(session, "I sent 1500 to the wrong number please reverse it", "0711000777").complaint
    a = views.detail(session, with_transfer, public=True, policy=CTX.policy)
    b = views.detail(session, without, public=True, policy=CTX.policy)
    shape = lambda d: (d["complaint"]["status"], d["complaint"]["escalation"]["reason_code"],  # noqa: E731
                       [s["summary"] for s in d["steps"]], d["tool_calls"])
    assert shape(a) == shape(b)
    assert a["complaint"]["reply"].replace(with_transfer.ref, "X").replace("412", "") == \
        b["complaint"]["reply"].replace(without.ref, "X").replace("777", "")
    assert views.detail(session, with_transfer)["tool_calls"] == []  # nothing ran, not even a lookup


def test_refunds_and_recredits_still_run_but_say_only_what_was_done(session):
    refund = _file(session, "please refund me, I was charged wrongly", "0700000567").complaint
    assert refund.status == "action_taken"
    assert refund.reply.startswith("Hi there, we have refunded the charge to the airtime balance of +254 7•• ••• 567.")
    recredit = _file(session, "my bundle expired early please recredit", "0700000345").complaint
    assert recredit.status == "action_taken" and "The data bundle on this number has been re-credited" in recredit.reply


def test_a_reply_echoes_what_the_customer_did_type(session):
    reversal = _file(session, "I sent KES 1,500 to the wrong number, code SJK4H7QW2L, please reverse.", "0700000412").complaint
    assert "SJK4H7QW2L" in reversal.reply and "KES 1,500" in reversal.reply
    named = _file(session, "Nilinunua Weekly 2GB jana lakini bundles zimeisha mapema", "0700000345").complaint
    assert "Your Weekly 2GB bundle has been re-credited" in named.reply
    amount = _file(session, "I was charged twice for my weekly bundle, please refund the extra KES 99", "0700000678").complaint
    assert "refunded the charge of KES 99" in amount.reply


@pytest.mark.parametrize(
    "text,msisdn,reason",
    [
        ("Nimetuma 12000 kwa namba mbaya, code ni SHR2M9PL4Q. Rudisha.", "0700000118", "over_refund_limit"),
        ("You people are THIEVES!! My bundle expired early again, useless", "0700000890", "angry_high_value"),
        ("My calls keep dropping everywhere", "0700000901", "repeat_unresolved"),
        ("my bundle was not applied, please recredit it", "0700000456", "tool_failed"),
    ],
)
def test_an_account_derived_reason_is_told_to_the_customer_as_a_generic_review(session, text, msisdn, reason):
    row = _file(session, text, msisdn).complaint
    assert row.escalation_reason_code == reason
    staff = views.detail(session, row)["complaint"]["escalation"]
    public = views.detail(session, row, public=True, policy=CTX.policy)["complaint"]["escalation"]
    assert staff["reason_code"] == reason
    assert public == {"reason_code": "account_review", "reason": CTX.policy.account_review_reason,
                      "at": staff["at"], "claimed_by": None}
    assert CTX.policy.account_review_reason in row.reply and CTX.policy.rule(reason).reason not in row.reply


def test_a_text_derived_reason_is_named_to_the_customer(session):
    row = _file(session, "Someone did a SIM swap on my line last night", "0700001245").complaint
    public = views.detail(session, row, public=True, policy=CTX.policy)["complaint"]["escalation"]
    assert public["reason_code"] == "fraud_or_sim_swap" and "PIN" in row.reply


def test_a_public_duplicate_shows_only_the_reference_and_status(session):
    first = _file(session, "My calls keep dropping in town", "0711000901", name="Achieng Atieno").complaint
    dup = views.duplicate_view(first, msisdn_masked="+254 7•• ••• 901")
    c = dup["complaint"]
    assert (c["ref"], c["status"]) == (first.ref, first.status)
    assert c["customer"] == {"name": None, "msisdn_masked": "+254 7•• ••• 901", "account_ref": None}
    assert c["reply"] is None and c["body"] is None and c["id"] is None and c["citations"] == []
    assert dup["steps"] == dup["tool_calls"] == dup["messages"] == []
    assert set(c) == set(views.complaint_out(session, first))  # still the contract's shape
    assert "Achieng" not in json.dumps(dup)


def test_staff_see_the_verified_account_not_what_the_caller_typed(session):
    row = _file(session, "My calls keep dropping in town", "0700000412", name="Mallory", account_ref="ACC-999999").complaint
    staff = views.detail(session, row)
    assert staff["complaint"]["customer"]["account_ref"] == "ACC-100412"
    assert staff["complaint"]["customer"]["name"] == "Wanjiku Kamau"
    assert staff["steps"][0]["detail"]["account_ref_claimed"] == "ACC-999999"
    assert staff["messages"][0]["name"] == "Mallory"  # what the caller called themselves is still on record
    unknown = _file(session, "My calls keep dropping in town", "0711000902", account_ref="ACC-100412").complaint
    assert views.detail(session, unknown)["complaint"]["customer"]["account_ref"] is None
    public = views.detail(session, row, public=True, policy=CTX.policy)["complaint"]["customer"]
    assert public == {"name": "Mallory", "msisdn_masked": "+254 7•• ••• 412", "account_ref": None}
