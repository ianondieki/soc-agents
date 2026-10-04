"""The action agent: turns a recognised intent into one tool call with concrete arguments, and
writes the customer's reply when the call succeeds.

**Planning picks the target; the tool judges it.** Triage says *what kind* of fix is wanted
("reverse an M-PESA transfer"); this module finds *which* transfer, bundle or charge, and hands
it to the tool, which applies the policy. When planning cannot identify a target -- no bundle
that expired early, no refundable charge -- there is nothing safe to do, and :func:`plan_call`
returns None so the resolver answers from the knowledge base instead (it explains why the charge
stands). When a target IS identified and the tool refuses it (already reversed, not on this
account, re-credit inside 30 days), a person has to finish the job: ``tool_failed``.

**A reversal needs the code the customer typed** (policy of 2026-10-04, ``needs_verification``).
The public form cannot prove the caller owns the number, and a reversal moves money out of
somebody else's hands, so the transfer is identified ONLY by the 10-character code in the
customer's own message -- the code is the proof of the confirmation SMS. Without one,
:func:`needs_verification` tells the desk to send the case to a person before anything reads the
account. It is never matched by amount or guessed from "the most recent one". Refunds and
re-credits still pick their target from the account: they only ever credit the caller's number.

**The replies never reveal the account.** Whoever typed the number gets the reply, so a reply
states what was done and echoes only what the customer's own message already said: the code
they typed, an amount they wrote, a bundle they named. Never an amount, a charge description or
a bundle name they did not mention (:func:`success_reply`). Staff read the rest on the tool call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from noc_agents.support.accounts import Account
from noc_agents.support.text import extract_amounts, extract_mpesa_codes, normalise, phrase_pattern
from noc_agents.support.triage import TriageResult

#: Tools that need the customer's account to choose their target.
NEEDS_ACCOUNT: frozenset[str] = frozenset({"reverse_mpesa", "recredit_bundle", "issue_refund"})
#: Tools that move money (or its equivalent, data). A model's tie-break may never trigger one.
MONEY_TOOLS: frozenset[str] = NEEDS_ACCOUNT

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


def needs_verification(triage: TriageResult, text: str) -> bool:
    """A reversal asked for without a transaction code in the customer's own message."""
    return triage.tool == "reverse_mpesa" and not extract_mpesa_codes(text)


def _plan_reversal(text: str) -> PlannedCall | None:
    codes = extract_mpesa_codes(text)
    if not codes:
        return None  # the desk checks needs_verification first; this is the belt to its braces
    return PlannedCall("reverse_mpesa", {"transaction_code": codes[0]}, f"code {codes[0]} quoted in the complaint")


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
        return _plan_reversal(text)
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


def _of_typed_amount(amount: int, text: str) -> str:
    """ " of KES 1,500" when the customer wrote that amount, else nothing."""
    return f" of KES {amount:,}" if amount in extract_amounts(text) else ""


def _named(name: str, text: str) -> bool:
    return bool(phrase_pattern(name).search(normalise(text)))


def success_reply(tool: str, result: dict[str, Any], *, text: str, name: str, ref: str, msisdn_masked: str) -> str:
    """What the customer reads after ``tool`` succeeded with ``result``, echoing only what ``text``
    (the customer's own message) already says -- see the module docstring."""
    if tool == "reverse_mpesa":
        code = result["transaction_code"]
        which = f"M-PESA transaction {code}" if code in extract_mpesa_codes(text) else "the M-PESA transaction you quoted"
        return (f"Hi {name}, we have reversed {which}{_of_typed_amount(result['amount_kes'], text)}. "
                f"The money is back in the M-PESA account of {msisdn_masked}. Reference {ref}.")
    if tool == "recredit_bundle":
        bundle = f"Your {result['name']} bundle" if _named(result["name"], text) else "The data bundle on this number"
        return (f"Hi {name}, sorry about your data. {bundle} has been re-credited and a confirmation SMS will follow. "
                f"Reference {ref}.")
    if tool == "issue_refund":
        return (f"Hi {name}, we have refunded the charge{_of_typed_amount(result['amount_kes'], text)} to the airtime "
                f"balance of {msisdn_masked}. Reference {ref}.")
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
