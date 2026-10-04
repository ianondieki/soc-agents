"""The triage agent: categories, confidence, risk flags, urgency, sentiment, intents, routes,
and the LLM tie-break (with a fake port -- no test ever reaches a model)."""

from __future__ import annotations

import pytest

from noc_agents.config import get_settings
from noc_agents.support.places import Gazetteer
from noc_agents.support.policy import load_policy
from noc_agents.support.triage import PRIOR, TieBreak, confidence_of, triage

POLICY = load_policy()
GAZETTEER = Gazetteer.from_regions(get_settings().operator.regions)


def _t(text, port=None):
    return triage(text, gazetteer=GAZETTEER, policy=POLICY, port=port)


@pytest.mark.parametrize(
    "text,category",
    [
        ("I sent 1500 to the wrong number, please reverse", "mpesa"),
        ("Nimetuma pesa kwa namba mbaya", "mpesa"),
        ("bundles zimeisha mapema sana", "data_bundles"),
        ("No network in Nakuru since morning", "network"),
        ("internet iko slow sana leo", "network"),
        ("I was charged twice for my bundle, refund me", "billing"),
        ("Nilikopa credo na top up imekatwa", "billing"),
        ("My SIM is asking for a PUK code", "sim_and_fraud"),
        ("I need internet settings for my new phone", "device_settings"),
        ("How do I activate roaming before I travel to Uganda?", "roaming"),
        ("How do I port my number to another network?", "account"),
        ("I am not satisfied with how my complaint was handled, where else can I complain?", "other"),
    ],
)
def test_category(text, category):
    assert _t(text).category == category


def test_confidence_is_top_over_top_plus_runner_up_plus_the_prior():
    assert confidence_of({"a": 3.0, "b": 0.0}) == ("a", round(3 / (3 + PRIOR), 3))
    assert confidence_of({"a": 3.0, "b": 3.0}) == ("a", round(3 / (6 + PRIOR), 3))
    assert confidence_of({"a": 0.0, "b": 0.0}) == ("other", 0.0)
    assert _t("Hello, it is not working, please help").confidence < POLICY.low_confidence_threshold


@pytest.mark.parametrize(
    "text,flag",
    [
        ("Someone did a SIM swap on my line and emptied my M-PESA", "fraud_or_sim_swap"),
        ("Mtu anatumia M-PESA yangu, PIN yangu imebadilishwa", "fraud_or_sim_swap"),
        ("My statement shows a withdrawal I did not make", "fraud_or_sim_swap"),
        ("I will report you to the Communications Authority", "legal_or_regulator"),
        ("My lawyer will contact you about this deduction", "legal_or_regulator"),
        ("I am going to the CA tomorrow", "legal_or_regulator"),
        ("Nitawashtaki mahakamani", "legal_or_regulator"),
        ("I have filed a complaint with the ODPC about my data", "legal_or_regulator"),
        ("I want to end my life because of this debt", "threat_or_safety"),
        ("Kuna mtu ananitishia kwa simu kila siku", "threat_or_safety"),
    ],
)
def test_risk_flags_route_to_a_person(text, flag):
    result = _t(text)
    assert flag in result.risk_flags
    assert result.route == "human" and result.intent is None


def test_ca_is_case_sensitive_and_regulator_alone_is_a_question_not_a_flag():
    assert _t("I bought a phone in ca and it has no network").risk_flags == ()
    question = _t("How do I escalate my complaint to the regulator if you do not resolve it?")
    assert question.risk_flags == () and question.route == "resolver"


def test_a_fraud_flag_is_evidence_for_the_fraud_category():
    assert _t("Mtu anatumia M-PESA yangu bila mimi kujua").category == "sim_and_fraud"


def test_urgency():
    assert _t("Someone did a SIM swap on my line").urgency == "critical"
    assert _t("I sent KES 12,000 to the wrong number").urgency == "high"
    assert _t("Please help urgently, my calls keep dropping").urgency == "high"
    assert _t("How do I activate roaming?").urgency == "low"
    assert _t("My calls keep dropping in town").urgency == "normal"


def test_sentiment():
    assert _t("You are THIEVES!! my bundle expired early").sentiment == "angry"
    assert _t("This is useless, my bundle expired early").sentiment == "angry"
    assert _t("MY BUNDLE EXPIRED EARLY AGAIN, FIX IT NOW!!").sentiment == "angry"
    assert _t("My bundle expired early again").sentiment == "frustrated"
    assert _t("Bundles zimeisha tena, nimechoka").sentiment == "frustrated"
    assert _t("My bundle expired early").sentiment == "calm"


@pytest.mark.parametrize(
    "text,intent,tool",
    [
        ("I sent 1500 to the wrong number, please reverse", "reverse_mpesa", "reverse_mpesa"),
        ("bundles zimeisha mapema", "recredit_bundle", "recredit_bundle"),
        ("I was charged twice for my bundle, refund me", "issue_refund", "issue_refund"),
        ("Salio imekatwa bila sababu", "issue_refund", "issue_refund"),
        ("No network in Nakuru since morning", "link_incident", "link_incident"),
        ("Hakuna network huku Kayole", "link_incident", "link_incident"),
        ("Please send me the internet settings for my new phone", "reset_network_settings", "reset_network_settings"),
    ],
)
def test_actionable_intents_route_to_the_action_agent(text, intent, tool):
    result = _t(text)
    assert (result.intent, result.tool, result.route) == (intent, tool, "action")


def test_no_intent_without_its_preconditions():
    assert _t("No network since morning").route == "resolver"  # no place: nothing to link
    assert _t("I borrowed airtime and the top up was deducted, refund?").intent is None  # repayment, not a refund
    assert _t("How do I stop premium sms").route == "resolver"


def test_places_come_from_the_operator_profile_and_prefer_the_longest_name():
    assert [p.name for p in _t("Hakuna network huku Kayole").places] == ["kayole"]
    assert _t("Hakuna network huku Kayole").places[0].regions == ("NBI_E",)
    assert [p.name for p in _t("no network in Nairobi East today").places] == ["nairobi east"]
    assert _t("I want to port my number").places == ()  # "Port" (Changamwe / Port) is not a place


def test_language_and_reasons_are_reported():
    result = _t("Nimetuma pesa kwa namba mbaya, rudisha")
    assert result.language == "sw"
    assert any(r.startswith("category mpesa") for r in result.reasons)
    assert any(r.startswith("intent reverse_mpesa") for r in result.reasons)
    assert result.detail()["route"] == "action"


# ------------------------------------------------------------------------ two issues in one


def test_two_issues_joined_by_a_connector_are_both_recorded_and_the_actionable_one_leads():
    result = _t("Network ya Nakuru imepotea tangu asubuhi na pia nilitaka kuuliza bei ya roaming nikienda Tanzania next week.")
    assert [i.category for i in result.issues] == ["network", "roaming"]
    assert result.primary == 0 and result.category == "network"
    assert (result.intent, result.tool, result.route) == ("link_incident", "link_incident", "action")
    assert result.confidence >= POLICY.low_confidence_threshold  # scored on its own words, the roaming aside no longer ties it
    assert [i.category for i in result.secondary] == ["roaming"] and result.secondary[0].intent is None
    detail = result.detail()
    assert [i["category"] for i in detail["issues"]] == ["network", "roaming"] and detail["primary_issue"] == 0
    assert any(r.startswith("issues: network (link_incident); roaming") for r in result.reasons)


def test_the_actionable_issue_leads_even_when_the_customer_wrote_it_second():
    result = _t("Quick question, roaming ya Uganda inawashwa aje? Also my Weekly 2GB bundle expired after one day, please re-credit it.")
    assert [i.category for i in result.issues] == ["roaming", "data_bundles"]
    assert result.primary == 1 and result.category == "data_bundles" and result.tool == "recredit_bundle"
    assert [i.category for i in result.secondary] == ["roaming"]


def test_two_informational_issues_keep_the_one_the_customer_led_with():
    result = _t("Simu zangu zinakatika kila mara nikipiga. Then another thing, nataka kuhamia network ingine na nibaki na namba yangu.")
    assert [i.category for i in result.issues] == ["network", "account"]
    assert result.primary == 0 and result.category == "network" and result.route == "resolver"


def test_a_connector_inside_one_topic_or_an_aside_without_evidence_is_not_a_second_issue():
    assert _t("My bundle expired early and also it was not even applied properly, please re-credit it.").issues == ()
    assert _t("Hello, it is not working since yesterday, please help me.").issues == ()
    assert _t("No network in Nakuru since morning, I cannot make any calls.").issues == ()


def test_a_risk_flag_outranks_the_issue_split():
    result = _t("No network in Nakuru since morning, and also someone did a SIM swap on my brother's line and emptied his M-PESA.")
    assert "fraud_or_sim_swap" in result.risk_flags and result.route == "human" and result.issues == ()
    assert result.category == "sim_and_fraud"


def test_a_negated_trip_is_not_a_roaming_complaint_and_a_pasted_code_is_mpesa_evidence():
    bill = _t("Bili yangu ya mwezi huu ni 6,200 na kawaida huwa 3,500. Sijaenda nje ya nchi wala sijanunua kitu.")
    assert bill.category == "billing" and bill.scores.get("roaming", 0) == 0 and bill.confidence >= POLICY.low_confidence_threshold
    sheng = _t("Buda doh yangu imeenda kwa mse flani, 1.5k, code ni sjk4h7qw2l. Nisaidie kuirudisha")
    assert sheng.category == "mpesa" and sheng.tool == "reverse_mpesa"
    assert "transaction code present" in " ".join(sheng.reasons)


class _FakePort:
    provider = "fake"

    def __init__(self, answer=None, exc=None):
        self.answer, self.exc, self.calls = answer, exc, []

    def draft(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc:
            raise self.exc
        return (TieBreak(category=self.answer) if self.answer else None), None


AMBIGUOUS = "Nimenunua bundle lakini airtime pia imekatwa"  # billing vs data_bundles, below threshold


def test_the_llm_tiebreak_may_choose_between_the_top_two_and_lifts_confidence_only_to_the_threshold():
    before = _t(AMBIGUOUS)
    assert before.confidence < POLICY.llm_tiebreak_below
    port = _FakePort(answer="data_bundles")
    after = _t(AMBIGUOUS, port=port)
    assert after.category == "data_bundles" and after.source == "llm_tiebreak"
    assert after.confidence == POLICY.low_confidence_threshold
    assert "billing" in port.calls[0]["user"] and "data_bundles" in port.calls[0]["user"]


@pytest.mark.parametrize("port", [_FakePort(answer="roaming"), _FakePort(answer=None), _FakePort(exc=RuntimeError("down"))])
def test_any_other_llm_answer_or_failure_keeps_the_rule_result(port):
    assert _t(AMBIGUOUS, port=port).__dict__ == _t(AMBIGUOUS).__dict__


def test_a_confident_complaint_never_reaches_the_model():
    port = _FakePort(answer="roaming")
    assert _t("I sent 1500 to the wrong number, please reverse", port=port).category == "mpesa"
    assert port.calls == []


def test_the_tiebreak_scrubs_phone_numbers_before_they_leave():
    port = _FakePort(answer="billing")
    _t(AMBIGUOUS + " call me on 0712345678", port=port)
    assert "0712345678" not in port.calls[0]["user"]
