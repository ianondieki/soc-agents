"""The escalation policy table and the policy file's validation."""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from noc_agents.support.escalation import PREDICATES, Escalation, EscalationFacts, evaluate, holding_reply, matching
from noc_agents.support.policy import DEFAULT_POLICY_PATH, SupportPolicy, load_policy
from noc_agents.support.vocab import REASON_CODES

POLICY = load_policy()
CALM = EscalationFacts(confidence=0.9)


def test_the_shipped_policy_lists_every_reason_in_the_contracts_order_with_the_contracts_limits():
    assert [r.reason_code for r in POLICY.escalation] == list(REASON_CODES)
    assert set(PREDICATES) == set(REASON_CODES)
    assert (POLICY.refund_auto_limit_kes, POLICY.reversal_auto_limit_kes, POLICY.reversal_window_hours) == (500, 5000, 24)
    assert (POLICY.recredit_cooldown_days, POLICY.low_confidence_threshold) == (30, 0.55)
    assert (POLICY.repeat.nth, POLICY.repeat.window_days, POLICY.dedupe_window_seconds) == (3, 7, 120)
    assert all(rule.reason for rule in POLICY.escalation)


@pytest.mark.parametrize(
    "facts,reason",
    [
        (EscalationFacts(risk_flags=("fraud_or_sim_swap",), confidence=0.9), "fraud_or_sim_swap"),
        (EscalationFacts(risk_flags=("legal_or_regulator",), confidence=0.9), "legal_or_regulator"),
        (EscalationFacts(risk_flags=("threat_or_safety",), confidence=0.9), "threat_or_safety"),
        (EscalationFacts(unverified=True, confidence=0.9), "needs_verification"),
        (EscalationFacts(over_limit=True, confidence=0.9), "over_refund_limit"),
        (EscalationFacts(repeat_count=3, confidence=0.9), "repeat_unresolved"),
        (EscalationFacts(sentiment="angry", tier="platinum", confidence=0.9), "angry_high_value"),
        (EscalationFacts(confidence=0.54), "low_confidence"),
        (EscalationFacts(grounded=False, confidence=0.9), "not_grounded"),
        (EscalationFacts(tool_failed=True, confidence=0.9), "tool_failed"),
    ],
)
def test_each_rule_fires_on_its_own_condition(facts, reason):
    assert evaluate(facts, POLICY).reason_code == reason


def test_nothing_fires_for_a_confident_calm_first_complaint():
    assert evaluate(CALM, POLICY) is None
    assert evaluate(EscalationFacts(repeat_count=2, confidence=0.9), POLICY) is None  # the second is not a repeat
    assert evaluate(EscalationFacts(sentiment="angry", tier="silver", confidence=0.9), POLICY) is None
    assert evaluate(EscalationFacts(sentiment="frustrated", tier="platinum", confidence=0.9), POLICY) is None
    assert evaluate(EscalationFacts(confidence=0.55), POLICY) is None  # the threshold itself passes
    assert evaluate(EscalationFacts(grounded=None, confidence=0.9), POLICY) is None  # resolver did not run


def test_the_first_matching_rule_wins_and_every_match_is_reported():
    facts = EscalationFacts(risk_flags=("threat_or_safety", "fraud_or_sim_swap"), over_limit=True,
                            sentiment="angry", tier="gold", confidence=0.3)
    hits = matching(facts, POLICY)
    assert [h.reason_code for h in hits] == ["fraud_or_sim_swap", "threat_or_safety", "over_refund_limit",
                                             "angry_high_value", "low_confidence"]
    assert evaluate(facts, POLICY).reason_code == "fraud_or_sim_swap"
    # over the limit outranks a repeat, as the contract orders them
    assert evaluate(EscalationFacts(over_limit=True, repeat_count=5, confidence=0.9), POLICY).reason_code == "over_refund_limit"


def test_the_order_is_the_policy_files_to_decide():
    raw = yaml.safe_load(DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))
    raw["escalation"] = list(reversed(raw["escalation"]))
    reordered = SupportPolicy.model_validate(raw)
    facts = EscalationFacts(risk_flags=("fraud_or_sim_swap",), tool_failed=True, confidence=0.9)
    assert evaluate(facts, reordered).reason_code == "tool_failed"


def _raw():
    return yaml.safe_load(DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))


def test_a_policy_missing_a_reason_code_is_refused():
    raw = _raw()
    raw["escalation"] = [r for r in raw["escalation"] if r["reason_code"] != "threat_or_safety"]
    with pytest.raises(ValidationError, match="missing"):
        SupportPolicy.model_validate(raw)


def test_a_policy_with_an_unknown_reason_or_key_is_refused():
    raw = _raw()
    raw["escalation"].append({"reason_code": "bad_vibes", "reason": "x"})
    with pytest.raises(ValidationError, match="unknown"):
        SupportPolicy.model_validate(raw)
    typo = _raw()
    typo["refund_auto_limt_kes"] = 10_000
    with pytest.raises(ValidationError):
        SupportPolicy.model_validate(typo)


def test_the_holding_reply_is_honest_and_carries_the_advice():
    fraud = Escalation("fraud_or_sim_swap", POLICY.rule("fraud_or_sim_swap").reason, POLICY.rule("fraud_or_sim_swap").advice, "")
    reply = holding_reply(fraud, name="Wanjiku", ref="CMP-000042", due="14:30 EAT on Sat 4 Oct")
    assert reply.startswith("Hi Wanjiku,") and "CMP-000042" in reply and "14:30 EAT on Sat 4 Oct" in reply
    assert "PIN" in reply
    safety = POLICY.rule("threat_or_safety")
    assert "999" in holding_reply(Escalation(safety.reason_code, safety.reason, safety.advice, ""), name="there", ref="CMP-1", due="x")
