"""The resolver agent: answers from the knowledge base, and only when grounded.

"Grounded" means two things hold for an article: its raw BM25 score reaches the policy's
``grounding_threshold``, and it agrees with triage -- it is in the category triage chose, or
its score is ``cross_category_factor`` times the threshold (overwhelming lexical evidence).
When no article qualifies the resolver does not answer at all: it reports ``grounded=False``
and the escalation table sends the case to a person with ``not_grounded``. A support bot that
answers every question is a bot that invents policy for the questions its knowledge base does
not cover; refusing is the feature. The agreement rule is what stops a weak, accidental
overlap ("mtandao mwingine" -- another network -- against the M-PESA reversal article's
"mtu mwingine" -- another person) from becoming a confident wrong answer.

An article marked ``escalate`` (the SIM-swap and fraud article) is never answered from:
when it is the best article over the plain threshold -- category agreement deliberately NOT
required, safety outranks agreement -- the result is ``escalate``, evidence of that risk which
the desk adds to triage's flags. A customer describing fraud in words triage's lexicon missed
("transactions I never did") must still reach the fraud team, not read a paragraph about it.

When it does answer, the reply is the article's own customer template with three
placeholders filled (``{name}``, ``{ref}``, ``{msisdn_masked}``) -- the words are the
article's, reviewed once, not generated per complaint -- and the complaint cites exactly that
article with its score. One citation, the article the reply came from, so "cited the right
article" (the eval's resolution test) means "answered from the right article".

Ranking may prefer the category triage chose (``category_boost``); grounding never does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from noc_agents.support.kb import Article, Hit, KnowledgeBase
from noc_agents.support.policy import SupportPolicy
from noc_agents.support.text import clean

#: The only placeholders a reply template may use.
PLACEHOLDERS: frozenset[str] = frozenset({"name", "ref", "msisdn_masked"})


class _Placeholders(dict):
    """``format_map`` source that leaves an unknown ``{field}`` as written instead of raising."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def fill(template: str, **values: str) -> str:
    """The template with the allowed placeholders filled and its YAML line breaks folded."""
    allowed = {k: v for k, v in values.items() if k in PLACEHOLDERS}
    return clean(template.format_map(_Placeholders(allowed)))


@dataclass(frozen=True)
class ResolverResult:
    grounded: bool
    article: Article | None
    score: float
    hits: list[Hit] = field(default_factory=list)
    reply: str | None = None
    escalate: str | None = None  # the best article is escalate-only: this reason code

    def citations(self) -> list[dict[str, Any]]:
        if not self.grounded or self.article is None or self.escalate:
            return []
        return [{"article_id": self.article.id, "title": self.article.title, "score": round(self.score, 3)}]

    def detail(self) -> dict[str, Any]:
        return {
            "grounded": self.grounded,
            "escalate": self.escalate,
            "article_id": self.article.id if self.article else None,
            "score": round(self.score, 3),
            "candidates": [
                {"article_id": h.article.id, "score": round(h.score, 3), "matched": list(h.matched[:8])}
                for h in self.hits
            ],
        }


def resolve(
    text: str,
    *,
    kb: KnowledgeBase,
    policy: SupportPolicy,
    category: str | None,
    name: str,
    ref: str,
    msisdn_masked: str,
) -> ResolverResult:
    """Retrieve, judge grounding, and fill the best grounded article's reply (or decline)."""
    hits = kb.search(text, limit=3, prefer_category=category, category_boost=policy.category_boost)
    passing = [h for h in hits if h.score >= policy.grounding_threshold]
    if passing and passing[0].article.escalate:  # safety first: no category agreement needed
        top = passing[0]
        return ResolverResult(True, top.article, top.score, hits, escalate=top.article.escalate)
    grounded = [h for h in passing if _agrees(h, category, policy) and not h.article.escalate]
    if not grounded:
        best = hits[0] if hits else None
        return ResolverResult(False, best.article if best else None, best.score if best else 0.0, hits)
    top = grounded[0]
    reply = fill(top.article.reply, name=name, ref=ref, msisdn_masked=msisdn_masked)
    return ResolverResult(True, top.article, top.score, hits, reply)


def _agrees(hit: Hit, category: str | None, policy: SupportPolicy) -> bool:
    """In triage's category, or scoring ``cross_category_factor`` times the threshold."""
    return hit.article.category == category or hit.score >= policy.grounding_threshold * policy.cross_category_factor
