"""The escalation policy table: when a complaint goes to a person, and why (docs/SUPPORT_DESK.md
"Escalation policy").

The ORDER and the plain-English reasons live in ``config/support/policy.yaml``; the
CONDITIONS live here, one small predicate per reason code, over facts the other agents
gathered (:class:`EscalationFacts`). The first rule in policy order whose predicate holds is
the complaint's ``reason_code`` -- every rule that held is still recorded in the step trace,
so the floor sees "fraud, and also angry" rather than only the winner.

Why evaluate once, after the agents have looked, instead of stopping at the first worry: the
order is the policy's, not the pipeline's. ``over_refund_limit`` outranks
``repeat_unresolved`` in the contract, but only the action agent knows a refund is over the
limit -- so the action agent plans its call first (tools here have no side effects until the
call is recorded) and the table is applied to everything at once. Safety flags still stop the
pipeline early: a fraud or threat complaint never reaches the resolver or a tool.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from noc_agents.support.policy import SupportPolicy


@dataclass(frozen=True)
class EscalationFacts:
    risk_flags: tuple[str, ...] = ()
    unverified: bool = False  # a reversal was asked for without a transaction code (policy: needs_verification)
    over_limit: bool = False  # the action agent's planned call needs a person's approval
    repeat_count: int = 1  # complaints in the window on this category, this one included
    sentiment: str = "calm"
    tier: str | None = None
    confidence: float = 1.0
    #: The category came from the LLM tie-break AND the planned call moves money (a reversal, a
    #: refund, a re-credit). Complaint text is the caller's to write, so a model's choice between two
    #: categories may pick the article, never a payment: the low-confidence rule holds the call.
    model_decided_money: bool = False
    grounded: bool | None = None  # None: the resolver did not run
    tool_failed: bool = False


@dataclass(frozen=True)
class Escalation:
    reason_code: str
    reason: str
    advice: str
    evidence: str  # why the predicate held, in words, for the trace


Predicate = Callable[[EscalationFacts, SupportPolicy], str | None]


def _flag(code: str) -> Predicate:
    def predicate(facts: EscalationFacts, policy: SupportPolicy) -> str | None:
        return "triage raised the risk flag" if code in facts.risk_flags else None

    return predicate


def _needs_verification(facts: EscalationFacts, policy: SupportPolicy) -> str | None:
    return "the reversal was asked for without the transaction code, so a person verifies it first" if facts.unverified else None


def _over_limit(facts: EscalationFacts, policy: SupportPolicy) -> str | None:
    return "the planned tool call is above its auto limit" if facts.over_limit else None


def _repeat(facts: EscalationFacts, policy: SupportPolicy) -> str | None:
    rule = policy.repeat
    if facts.repeat_count >= rule.nth:
        return f"complaint number {facts.repeat_count} on this category in {rule.window_days} days"
    return None


def _angry_high_value(facts: EscalationFacts, policy: SupportPolicy) -> str | None:
    if facts.sentiment == "angry" and (facts.tier or "") in policy.high_value_tiers:
        return f"angry customer on a {facts.tier} account"
    return None


def _low_confidence(facts: EscalationFacts, policy: SupportPolicy) -> str | None:
    threshold = policy.low_confidence_threshold
    if facts.confidence < threshold:
        return f"triage confidence {facts.confidence:.2f} is below {threshold:.2f}"
    if facts.model_decided_money:
        return "the category came from the LLM tie-break, and a model's choice never moves money on its own"
    return None


def _not_grounded(facts: EscalationFacts, policy: SupportPolicy) -> str | None:
    return "no article reached the grounding threshold" if facts.grounded is False else None


def _tool_failed(facts: EscalationFacts, policy: SupportPolicy) -> str | None:
    return "the tool call failed or was refused with no safe alternative" if facts.tool_failed else None


#: One predicate per contract reason code (the policy loader guarantees the file lists them all).
PREDICATES: dict[str, Predicate] = {
    "fraud_or_sim_swap": _flag("fraud_or_sim_swap"),
    "legal_or_regulator": _flag("legal_or_regulator"),
    "threat_or_safety": _flag("threat_or_safety"),
    "needs_verification": _needs_verification,
    "over_refund_limit": _over_limit,
    "repeat_unresolved": _repeat,
    "angry_high_value": _angry_high_value,
    "low_confidence": _low_confidence,
    "not_grounded": _not_grounded,
    "tool_failed": _tool_failed,
}


def matching(facts: EscalationFacts, policy: SupportPolicy) -> list[Escalation]:
    """Every rule whose predicate holds, in policy order."""
    out: list[Escalation] = []
    for rule in policy.escalation:
        evidence = PREDICATES[rule.reason_code](facts, policy)
        if evidence:
            out.append(Escalation(rule.reason_code, rule.reason, rule.advice, evidence))
    return out


def evaluate(facts: EscalationFacts, policy: SupportPolicy) -> Escalation | None:
    """The first matching rule in policy order, or None when the desk may finish the case itself."""
    hits = matching(facts, policy)
    return hits[0] if hits else None


#: Reasons decided from the ACCOUNT (its limits, history, tier, state), not from the caller's words.
#: Whoever types a number on the public form reads the reply, so these are never named to the
#: customer: the reply and the public view say :data:`ACCOUNT_REVIEW_CODE` with the policy's
#: ``account_review_reason`` instead. Staff see the real reason. The other reasons (the safety
#: flags, needs_verification, low confidence, not grounded) come from the text the caller typed.
ACCOUNT_REASONS: frozenset[str] = frozenset({"over_refund_limit", "repeat_unresolved", "angry_high_value", "tool_failed"})
ACCOUNT_REVIEW_CODE = "account_review"


def customer_facing(escalation: Escalation, policy: SupportPolicy) -> Escalation:
    """``escalation`` as the customer may read it: an account-derived reason becomes the generic review."""
    if escalation.reason_code not in ACCOUNT_REASONS:
        return escalation
    return Escalation(ACCOUNT_REVIEW_CODE, policy.account_review_reason, "", "")


def holding_reply(escalation: Escalation, *, name: str, ref: str, due: str) -> str:
    """The honest reply a customer gets while a person picks the case up: why, what to do now, when."""
    advice = f" {escalation.advice}" if escalation.advice else ""
    return (
        f"Hi {name}, thank you for contacting us. Your complaint {ref} has been passed to a member of our "
        f"team because {escalation.reason}.{advice} We will get back to you by {due}."
    )
