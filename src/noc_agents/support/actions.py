"""The action agent: turns a recognised intent into one tool call with concrete arguments, and
writes the customer's reply when the call succeeds.

**Planning picks the target; the tool judges it.** Triage says *what kind* of fix is wanted
("reverse an M-PESA transfer"); this module finds *which* transfer, bundle or charge, reading
the complaint and the customer's account, and hands it to the tool, which applies the policy.
The split matters for the escalation table: when planning cannot identify a target -- no
transaction code and no amount that matches exactly one transfer, no bundle that expired
early, no refundable charge -- there is nothing safe to do, and :func:`plan_call` returns None
so the resolver answers from the knowledge base instead (it tells the customer what to send
or why the charge stands). When a target IS identified and the tool refuses it (already
reversed, no account on record, re-credit inside 30 days), a person has to finish the job:
``tool_failed``.

Matching is deliberately literal. A transfer is chosen by a 10-character code in the text,
else by an amount in the text that matches exactly ONE outgoing transfer; it is never guessed
from "the most recent one", because reversing the wrong transfer is worse than asking.

The success replies below are what the customer reads after a tool acted. They state what was
done, with the amount and reference; they never promise what a tool did not do.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from noc_agents.support.accounts import Account
from noc_agents.support.text import extract_amounts, extract_mpesa_codes, normalise
from noc_agents.support.triage import TriageResult

#: Tools that need the customer's account to choose their target.
NEEDS_ACCOUNT: frozenset[str] = frozenset({"reverse_mpesa", "recredit_bundle", "issue_refund"})

#: Words that point a refund at one kind of charge.
_CHARGE_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("duplicate", ("twice", "double", "mara mbili", "two times", "duplicate")),
    ("premium_sms", ("premium", "subscription", "subscribed", "sms", "messages", "tips", "horoscope", "jokes", "content")),
    ("airtime_deduction", ("airtime", "credo", "salio", "deducted", "imekatwa", "nimekatwa")),
)


@dataclass(frozen=True)
class PlannedCall:
    tool: str
    args: dict[str, Any]
    why: str  # how the target was chosen, for the trace


def _plan_reversal(text: str, account: Account | None) -> PlannedCall | None:
    codes = extract_mpesa_codes(text)
    if codes:
        return PlannedCall("reverse_mpesa", {"transaction_code": codes[0]}, f"code {codes[0]} quoted in the complaint")
    if account is None:
        return None
    amounts = set(extract_amounts(text))
    matches = [t for t in account.transactions if t.type == "sent" and t.amount_kes in amounts]
    if len(matches) == 1:
        txn = matches[0]
        return PlannedCall("reverse_mpesa", {"transaction_code": txn.code},
                           f"the only transfer of KES {txn.amount_kes:,} on the account")
    return None


def _plan_recredit(account: Account | None) -> PlannedCall | None:
    if account is None:
        return None
    eligible = sorted((b for b in account.bundles if b.status in ("expired_early", "not_applied")),
                      key=lambda b: b.bought_hours_ago)
    if not eligible:
        return None
    bundle = eligible[0]
    return PlannedCall("recredit_bundle", {"bundle_id": bundle.id}, f"{bundle.name} is marked {bundle.status}")


def _plan_refund(text: str, account: Account | None) -> PlannedCall | None:
    if account is None:
        return None
    norm = normalise(text)
    refundable = sorted((c for c in account.charges if c.refundable), key=lambda c: c.hours_ago)
    hinted = [kind for kind, words in _CHARGE_HINTS if any(w in norm for w in words)]
    for kind in hinted:
        for charge in refundable:
            if charge.kind == kind:
                return PlannedCall("issue_refund", {"charge_id": charge.id, "amount_kes": charge.amount_kes,
                                                    "reason": charge.description}, f"refundable {kind} charge")
    if len(refundable) == 1:
        charge = refundable[0]
        return PlannedCall("issue_refund", {"charge_id": charge.id, "amount_kes": charge.amount_kes,
                                            "reason": charge.description}, "the only refundable charge on the account")
    return None


def plan_call(triage: TriageResult, text: str, account: Account | None) -> PlannedCall | None:
    """The one tool call for ``triage.intent``, or None when there is nothing safe to act on."""
    if triage.tool == "reverse_mpesa":
        return _plan_reversal(text, account)
    if triage.tool == "recredit_bundle":
        return _plan_recredit(account)
    if triage.tool == "issue_refund":
        return _plan_refund(text, account)
    if triage.tool == "link_incident" and triage.places:
        place = triage.places[0]
        return PlannedCall("link_incident", {"place": place.name, "regions": list(place.regions)}, f"place named: {place.name}")
    if triage.tool == "reset_network_settings":
        device = account.device if account else None
        return PlannedCall("reset_network_settings", {"device": device}, "device settings requested")
    return None


def success_reply(tool: str, result: dict[str, Any], *, name: str, ref: str, msisdn_masked: str) -> str:
    """What the customer reads after ``tool`` succeeded with ``result``."""
    if tool == "reverse_mpesa":
        return (f"Hi {name}, we have reversed M-PESA transaction {result['transaction_code']} of "
                f"KES {result['amount_kes']:,}. The money is back in your M-PESA account. Reference {ref}.")
    if tool == "recredit_bundle":
        return (f"Hi {name}, sorry about your data. Your {result['name']} bundle has been re-credited and you "
                f"will receive a confirmation SMS. Reference {ref}.")
    if tool == "issue_refund":
        return (f"Hi {name}, we have refunded KES {result['amount_kes']:,} for \"{result['description']}\" to your "
                f"airtime balance. Reference {ref}.")
    if tool == "link_incident":
        return (f"Hi {name}, thank you for reporting the network problem in {result['place'].title()}. It is part of "
                f"a known outage, ticket {result['incident_number']}, and our engineers are already working on it. "
                f"We have linked your complaint {ref} to it and will update you when service is restored.")
    if tool == "reset_network_settings":
        return (f"Hi {name}, we have sent the internet and MMS settings to {msisdn_masked} by SMS. Open the message, "
                f"tap install or save, then restart your phone and switch on mobile data. Reference {ref}.")
    raise ValueError(f"no success reply for tool {tool}")


def ticket_note(tool: str, result: dict[str, Any]) -> str:
    """The note ``update_ticket`` appends after a successful action."""
    if tool == "reverse_mpesa":
        return f"Reversed {result['transaction_code']} (KES {result['amount_kes']:,})."
    if tool == "recredit_bundle":
        return f"Re-credited {result['name']} ({result['bundle_id']})."
    if tool == "issue_refund":
        return f"Refunded KES {result['amount_kes']:,} on {result['charge_id']}."
    if tool == "link_incident":
        return f"Linked to {result['incident_number']} ({result['site_name']}, {result['status']})."
    if tool == "reset_network_settings":
        return "Configuration SMS (APN, MMS) recorded for the customer's device."
    return f"{tool} completed."
