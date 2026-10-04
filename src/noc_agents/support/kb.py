"""The knowledge base and its retrieval: a small, pure-Python BM25 (docs/SUPPORT_DESK.md "Flow").

Why BM25 and not embeddings: the corpus is twenty articles, the queries are short and full
of exact product words ("bundle", "PUK", "roaming"), the result has to be explainable to a
supervisor ("it matched *wrong number* and *reverse*"), and it must run identically in CI
with no model and no network. BM25 over a synonym-mapped token stream (:func:`text.tokens`)
does all four. Adjacent-word bigrams (:func:`text.terms`) give phrases their due:
"wrong number" is one term, so an article about it beats one that says "number" and "wrong"
in different paragraphs.

Each article is indexed as one document built from its fields with weights: the title and
the customer-phrased ``keywords`` count twice, the summary and the body once. Keywords carry
the Kiswahili and Sheng phrasing, which is what makes "bundles zimeisha mapema" find the
bundle article at all.

The score is plain Okapi BM25 (k1 = 1.5, b = 0.75, the textbook defaults; idf with the +1
inside the log so it never goes negative), summed over the *distinct* query terms -- a
customer who types "network network network" is not three times as grounded. Grounding is
judged on this raw score against ``grounding_threshold`` in the policy; the optional category
boost only re-orders hits and never makes an ungrounded article look grounded.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from noc_agents.support.policy import SUPPORT_CONFIG_DIR
from noc_agents.support.text import terms
from noc_agents.support.vocab import CATEGORIES, REASON_CODES

DEFAULT_KB_PATH = SUPPORT_CONFIG_DIR / "knowledge_base.yaml"

K1 = 1.5
B = 0.75
#: Field weights: how many times each field's tokens enter the article's document.
FIELD_WEIGHTS: tuple[tuple[str, int], ...] = (("title", 2), ("keywords", 2), ("summary", 1), ("body", 1))
SNIPPET_CHARS = 160


@dataclass(frozen=True)
class Article:
    id: str
    title: str
    category: str
    summary: str
    keywords: tuple[str, ...]
    body: str
    reply: str
    updated_at: str
    #: A reason code when the resolver must never answer from this article (see the KB file).
    escalate: str | None = None

    def field_chunks(self, name: str) -> tuple[str, ...]:
        """The field as separately-tokenised pieces: each keyword on its own, so no bigram spans two."""
        value = getattr(self, name)
        return value if isinstance(value, tuple) else (str(value),)

    def as_dict(self) -> dict[str, str]:
        """``GET /kb``'s article shape."""
        return {
            "id": self.id,
            "title": self.title,
            "category": self.category,
            "summary": self.summary,
            "body": self.body,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class Hit:
    article: Article
    score: float  # raw BM25: what grounding is judged on
    rank_score: float  # score after the category prior: what the order follows
    matched: tuple[str, ...]  # the query terms that matched, for the step trace

    def as_result(self) -> dict[str, object]:
        """``GET /kb/search``'s result shape."""
        return {
            "article_id": self.article.id,
            "title": self.article.title,
            "score": round(self.score, 3),
            "snippet": self.article.summary[:SNIPPET_CHARS],
        }


class KnowledgeBase:
    """The articles plus a BM25 index over them. Immutable once built."""

    def __init__(self, articles: list[Article]) -> None:
        if len({a.id for a in articles}) != len(articles):
            raise ValueError("knowledge base article ids must be unique")
        bad = sorted({a.category for a in articles} - set(CATEGORIES))
        if bad:
            raise ValueError(f"knowledge base uses unknown categories {bad}")
        bad_codes = sorted({a.escalate for a in articles if a.escalate} - set(REASON_CODES))
        if bad_codes:
            raise ValueError(f"knowledge base escalates with unknown reason codes {bad_codes}")
        self.articles: tuple[Article, ...] = tuple(articles)
        self._by_id = {a.id: a for a in articles}
        self._tf: list[Counter[str]] = []
        for article in articles:
            doc: list[str] = []
            for field, weight in FIELD_WEIGHTS:
                for chunk in article.field_chunks(field):
                    doc.extend(terms(chunk) * weight)
            self._tf.append(Counter(doc))
        self._len = [sum(tf.values()) for tf in self._tf]
        self._avg_len = (sum(self._len) / len(self._len)) if self._len else 0.0
        df: Counter[str] = Counter()
        for tf in self._tf:
            df.update(tf.keys())
        n = len(articles)
        self._idf = {term: math.log(1 + (n - count + 0.5) / (count + 0.5)) for term, count in df.items()}

    def get(self, article_id: str) -> Article | None:
        return self._by_id.get(article_id)

    def _bm25(self, index: int, query_terms: set[str]) -> tuple[float, tuple[str, ...]]:
        tf, length = self._tf[index], self._len[index]
        norm = K1 * (1 - B + B * length / self._avg_len) if self._avg_len else K1
        score, matched = 0.0, []
        for term in sorted(query_terms):
            freq = tf.get(term, 0)
            if freq:
                score += self._idf[term] * freq * (K1 + 1) / (freq + norm)
                matched.append(term)
        return score, tuple(matched)

    def search(
        self, query: str, *, limit: int = 5, prefer_category: str | None = None, category_boost: float = 0.0
    ) -> list[Hit]:
        """Articles scoring above zero for ``query``, best first.

        ``prefer_category`` multiplies the score of that category's articles by
        ``1 + category_boost`` for ORDERING only; ``Hit.score`` stays the raw BM25 value.
        """
        query_terms = set(terms(query))
        if not query_terms:
            return []
        hits: list[Hit] = []
        for index, article in enumerate(self.articles):
            score, matched = self._bm25(index, query_terms)
            if score <= 0:
                continue
            boost = 1 + category_boost if prefer_category and article.category == prefer_category else 1.0
            hits.append(Hit(article=article, score=score, rank_score=score * boost, matched=matched))
        hits.sort(key=lambda h: (-h.rank_score, h.article.id))
        return hits[: max(0, limit)]


def _article(raw: dict) -> Article:
    return Article(
        id=str(raw["id"]),
        title=str(raw["title"]),
        category=str(raw["category"]),
        summary=str(raw["summary"]).strip(),
        keywords=tuple(str(k) for k in raw.get("keywords") or ()),
        body=str(raw["body"]).strip(),
        reply=str(raw["reply"]).strip(),
        updated_at=str(raw.get("updated_at") or ""),
        escalate=raw.get("escalate") or None,
    )


@lru_cache(maxsize=8)
def load_kb(path: Path = DEFAULT_KB_PATH) -> KnowledgeBase:
    """Parse the knowledge base file and build the index. Cached per path."""
    with path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    return KnowledgeBase([_article(item) for item in raw.get("articles") or ()])
