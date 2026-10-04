"""The support knowledge base and its BM25 retrieval, and the resolver's grounding rules."""

from __future__ import annotations

import re

import pytest

from noc_agents.support.kb import Article, KnowledgeBase, load_kb
from noc_agents.support.policy import load_policy
from noc_agents.support.resolver import PLACEHOLDERS, fill, resolve
from noc_agents.support.text import detect_language
from noc_agents.support.vocab import CATEGORIES

KB = load_kb()
POLICY = load_policy()

#: The twenty topics the desk must cover (one article each).
REQUIRED = {
    "KB-MPESA-REVERSAL", "KB-MPESA-FAILED", "KB-DATA-BUNDLE-EXPIRED", "KB-AIRTIME-DEDUCTED",
    "KB-BILLING-PREMIUM-SMS", "KB-NETWORK-OUTAGE", "KB-NETWORK-SLOW-DATA", "KB-NETWORK-CALL-DROPS",
    "KB-DEVICE-APN", "KB-SIM-SWAP-FRAUD", "KB-LOST-PHONE-BLOCK", "KB-SIM-PUK-REPLACEMENT", "KB-ROAMING",
    "KB-POSTPAID-BILL", "KB-BILLING-DOUBLE-CHARGE", "KB-AIRTIME-ADVANCE", "KB-HOME-FIBRE",
    "KB-REGULATOR-COMPLAINT", "KB-ACCOUNT-REGISTRATION", "KB-PORTING",
}


def test_the_shipped_knowledge_base_covers_every_required_topic():
    assert {a.id for a in KB.articles} == REQUIRED


@pytest.mark.parametrize("article", KB.articles, ids=lambda a: a.id)
def test_every_article_is_complete_and_its_reply_uses_only_the_allowed_placeholders(article):
    assert article.category in CATEGORIES
    assert article.title and article.summary and article.body and article.reply and article.updated_at
    assert len(article.keywords) >= 8
    assert set(re.findall(r"\{(\w+)\}", article.reply)) <= PLACEHOLDERS
    assert "{ref}" in article.reply  # every reply quotes the customer's reference


@pytest.mark.parametrize("article", KB.articles, ids=lambda a: a.id)
def test_every_article_carries_kiswahili_or_sheng_keywords(article):
    assert any(detect_language(keyword) != "en" for keyword in article.keywords)


@pytest.mark.parametrize(
    "query,article_id",
    [
        ("I sent 1500 to the wrong number please reverse", "KB-MPESA-REVERSAL"),
        ("Nimetuma pesa kwa namba mbaya, naomba mnirudishie", "KB-MPESA-REVERSAL"),
        ("bundles zimeisha mapema sana", "KB-DATA-BUNDLE-EXPIRED"),
        ("Hakuna network huku Mombasa tangu jana", "KB-NETWORK-OUTAGE"),
        ("how do I stop these premium sms messages", "KB-BILLING-PREMIUM-SMS"),
        ("internet iko slow sana leo", "KB-NETWORK-SLOW-DATA"),
        ("My home fibre router has a red LOS light", "KB-HOME-FIBRE"),
        ("I borrowed airtime and my top up was deducted", "KB-AIRTIME-ADVANCE"),
        ("How do I move my number to another network", "KB-PORTING"),
        ("my sim card is damaged and not detected", "KB-SIM-PUK-REPLACEMENT"),
    ],
)
def test_retrieval_puts_the_right_article_first(query, article_id):
    hits = KB.search(query)
    assert hits[0].article.id == article_id
    assert hits[0].score >= POLICY.grounding_threshold


@pytest.mark.parametrize("query", [
    "How do I become an M-PESA agent and what float do I need?",
    "I want to buy a phone on hire purchase, what are the terms",
    "Do you sell solar lanterns? I need a quote for my shop",
])
def test_off_topic_questions_stay_below_the_grounding_threshold(query):
    hits = KB.search(query)
    assert not hits or hits[0].score < POLICY.grounding_threshold


def test_a_repeated_word_is_not_extra_evidence_and_an_empty_query_finds_nothing():
    once = KB.search("roaming")[0].score
    assert KB.search("roaming roaming roaming")[0].score == pytest.approx(once)
    assert KB.search("") == [] and KB.search("the and of") == []


def test_the_category_boost_reorders_but_never_changes_the_raw_score():
    plain = {h.article.id: h.score for h in KB.search("airtime deducted refund")}
    boosted = KB.search("airtime deducted refund", prefer_category="billing", category_boost=1.0)
    assert {h.article.id: h.score for h in boosted} == plain
    assert all(h.rank_score == pytest.approx(h.score * (2.0 if h.article.category == "billing" else 1.0)) for h in boosted)


def test_duplicate_ids_unknown_categories_and_unknown_escalation_codes_are_refused():
    article = KB.articles[0]
    with pytest.raises(ValueError, match="unique"):
        KnowledgeBase([article, article])
    with pytest.raises(ValueError, match="categories"):
        KnowledgeBase([Article(**{**article.__dict__, "category": "weather"})])
    with pytest.raises(ValueError, match="reason codes"):
        KnowledgeBase([Article(**{**article.__dict__, "escalate": "because"})])


def test_fill_fills_only_the_allowed_placeholders_and_folds_line_breaks():
    assert fill("Hi {name},\n  ref {ref} {secret}", name="Wanjiku", ref="CMP-000001", secret="x") == "Hi Wanjiku, ref CMP-000001 {secret}"


def _resolve(text, category):
    return resolve(text, kb=KB, policy=POLICY, category=category, name="there", ref="CMP-000009", msisdn_masked="+254 7•• ••• 412")


def test_the_resolver_answers_from_the_top_grounded_article_and_cites_exactly_it():
    result = _resolve("how do I stop these premium sms messages", "billing")
    assert result.grounded and result.article.id == "KB-BILLING-PREMIUM-SMS"
    assert result.citations() == [{"article_id": "KB-BILLING-PREMIUM-SMS", "title": result.article.title, "score": round(result.score, 3)}]
    assert "CMP-000009" in result.reply and "{" not in result.reply


def test_the_resolver_declines_below_the_threshold():
    result = _resolve("How do I become an M-PESA agent and what float do I need?", "mpesa")
    assert not result.grounded and result.reply is None and result.citations() == []


def test_an_article_outside_triages_category_needs_overwhelming_evidence():
    # "mtandao mwingine" (another network) shares "mwingine" with the reversal article; triage
    # says the complaint is about the network, so a middling cross-category score must not answer.
    weak = "Nataka kuhamia mtandao mwingine"
    hits = KB.search(weak)
    off_category = [h for h in hits if h.article.category not in ("network", "account")]
    assert all(h.score < POLICY.grounding_threshold * POLICY.cross_category_factor for h in off_category)
    result = _resolve("I sent 1500 to the wrong number please reverse", "billing")  # far above 2x: still answers
    assert result.grounded and result.article.id == "KB-MPESA-REVERSAL"


def test_the_fraud_article_is_never_answered_from_whatever_category_triage_chose():
    result = _resolve("someone is using my mpesa, unauthorised withdrawal, money stolen from mpesa", "mpesa")
    assert result.escalate == "fraud_or_sim_swap"
    assert result.reply is None and result.citations() == []
