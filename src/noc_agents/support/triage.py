"""The triage agent: what the complaint is about, how urgent, how the customer feels, what is
risky about it, and who should handle it (docs/SUPPORT_DESK.md "Flow", "Categories").

**Weighted phrase rules, not a model.** Each category has a lexicon of phrases with weights,
written in the English, Kiswahili and Sheng that complaints actually arrive in. A category's
score is the sum of the weights of its phrases found in the normalised text; overlapping
phrases both count ("no network" and "network"), which is intended -- a specific phrase is
stronger evidence than the bare word. The rules are data in this module, so a reviewer can
read exactly why a complaint was called *mpesa*, and the step trace lists the phrases that
fired.

**Confidence** is ``top / (top + runner_up + 1)``: one strong phrase alone gives about 0.75,
two agreeing phrases 0.85, and two categories scoring alike drop it towards 0.4 -- below the
policy's ``low_confidence_threshold`` (0.55), which sends the case to a person rather than
letting the desk guess. The ``+ 1`` is a prior: no evidence at all is confidence 0. It is
"calibrated-ish" by construction, not fitted; the golden set's triage accuracy is the check.

**Risk flags** (fraud or SIM swap, legal or regulator, threats or safety) are separate
lexicons and any hit routes to a person, whatever the category. "CA" and "CAK" are matched
case-sensitively on the original text: the Communications Authority is written in capitals,
and lower-case "ca" is noise. Note what is *not* a legal flag: the word "regulator". A
customer asking how to escalate to the regulator is asking a question the knowledge base
answers (``KB-REGULATOR-COMPLAINT``); one who names the Communications Authority, a lawyer
or a court is raising a matter a senior person must answer.

**Route**: ``human`` when any risk flag fired; ``action`` when an intent the action agent
can act on was recognised (a reversal, a bundle re-credit, a refund, an outage in a named
place, device settings); otherwise ``resolver``. The remaining escalation rules (repeat,
angry high-value, low confidence, grounding, tool limits) are applied afterwards by
:mod:`escalation`, which sees what the other agents found.

**The LLM tie-break.** With ``LLM_ENABLED`` and a port the caller is allowed to use,
a confidence below ``llm_tiebreak_below`` with at least two scoring categories is put to the
model as a choice between the top two -- nothing else. A reply naming any other category,
or any failure, leaves the rule result unchanged. An accepted tie-break lifts confidence only
to the low-confidence threshold itself: the model may break a tie, it may not manufacture
certainty. Tests never reach a model (``tests/conftest.py`` pins ``LLM_ENABLED=false``).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from noc_agents.support.places import Gazetteer, PlaceMention
from noc_agents.support.policy import SupportPolicy
from noc_agents.support.text import clean, detect_language, extract_amounts, normalise, phrase_pattern

log = logging.getLogger(__name__)

Lexicon = tuple[tuple[str, float], ...]

# ------------------------------------------------------------------------------ lexicons

CATEGORY_LEXICON: dict[str, Lexicon] = {
    "mpesa": (
        ("mpesa", 2.0), ("wrong number", 2.0), ("wrong person", 2.0), ("wrong recipient", 2.0),
        ("namba mbaya", 2.0), ("nimekosea namba", 2.5), ("kimakosa", 1.5), ("nimetuma pesa", 2.5),
        ("nimetuma", 1.0), ("sent money", 2.0), ("send money", 1.5), ("sent", 0.5), ("reverse", 1.5),
        ("reversal", 2.0), ("rudisha pesa", 1.5), ("transaction code", 1.5), ("transaction", 1.0),
        ("paybill", 2.0), ("till number", 2.0), ("buy goods", 1.5), ("lipa na mpesa", 2.0),
        ("pesa haijafika", 2.5), ("haijafika", 1.0), ("not received", 1.0), ("pending", 1.0),
        ("fuliza", 1.5), ("withdraw", 1.0), ("agent", 0.5), ("pesa", 0.5),
    ),
    "data_bundles": (
        ("bundle", 2.0), ("bundles", 2.0), ("bando", 2.0), ("data bundle", 1.0), ("data", 1.0),
        ("mbs", 1.5), ("gb", 1.0), ("expired early", 2.0), ("imeisha mapema", 2.0),
        ("zimeisha mapema", 2.0), ("zimeisha", 1.5), ("imeisha", 1.0), ("imeliwa", 1.5),
        ("haijaingia", 1.5), ("not applied", 1.5), ("data imeisha", 2.0), ("finished quickly", 1.5),
    ),
    "network": (
        ("no network", 3.0), ("network", 1.5), ("hakuna network", 3.0), ("mtandao", 2.0),
        ("hakuna mtandao", 3.0), ("signal", 2.0), ("no signal", 2.5), ("outage", 2.5),
        ("network down", 3.0), ("network iko chini", 3.0), ("network imepotea", 3.0),
        ("emergency calls only", 2.5), ("no service", 2.0), ("slow internet", 2.5), ("slow data", 2.5),
        ("cant call", 2.5), ("cannot call", 2.5), ("cant make calls", 2.5), ("cannot make calls", 2.5),
        ("receive calls", 1.5), ("make calls", 1.5), ("cant browse", 2.0), ("cannot browse", 2.0),
        ("network inapotea", 3.0), ("network haipo", 3.0),
        ("internet iko slow", 2.5), ("slow", 1.0), ("buffering", 1.5), ("call drops", 2.5),
        ("calls dropping", 2.5), ("dropping", 1.5), ("keep dropping", 2.0), ("dropped calls", 2.5),
        ("call drop", 2.5), ("zinakatika", 2.0),
        ("inakatika", 2.0), ("fibre", 2.5), ("fiber", 2.5), ("router", 2.0), ("wifi", 1.5),
        ("home internet", 2.5), ("internet", 0.5),
    ),
    "billing": (
        ("airtime", 1.5), ("credo", 1.5), ("salio", 1.5), ("deducted", 1.5), ("imekatwa", 1.5),
        ("nimekatwa", 1.5), ("zimekatwa", 1.5), ("refund", 1.5), ("charged", 1.5), ("charge", 1.0),
        ("charged twice", 3.0), ("double charge", 3.0), ("double charged", 3.0), ("deducted twice", 3.0),
        ("mara mbili", 2.0), ("premium", 2.0), ("subscription", 2.0), ("subscribed", 1.5),
        ("unsubscribe", 2.0), ("sms za ajabu", 2.5), ("messages za ajabu", 2.5), ("betting tips", 1.5),
        ("bill", 2.0), ("bili", 2.0), ("postpaid", 1.5), ("invoice", 2.0), ("okoa", 2.5),
        ("okoa jahazi", 1.0), ("airtime advance", 3.0), ("borrowed airtime", 3.0), ("deni", 1.5),
        ("nilikopa", 2.0), ("top up", 0.5),
    ),
    "sim_and_fraud": (
        ("sim swap", 3.0), ("swap", 2.0), ("imeswapiwa", 3.0), ("fraud", 3.0), ("puk", 3.0),
        ("sim", 1.0), ("laini", 1.0), ("stolen", 2.5), ("imeibiwa", 2.5), ("nimeibiwa", 2.5),
        ("lost my phone", 3.0), ("lost phone", 3.0), ("nimepoteza simu", 3.0), ("simu imepotea", 3.0),
        ("block my line", 2.5), ("block the line", 2.5), ("funga laini", 2.5), ("sim replacement", 2.5),
        ("replace my sim", 2.5), ("sim locked", 2.5), ("imejifunga", 2.5), ("hacked", 2.5), ("pin", 1.0),
        ("sim card", 1.5), ("damaged", 1.0), ("not detected", 1.5), ("replace", 1.0), ("laini imeharibika", 2.5),
    ),
    "device_settings": (
        ("apn", 3.0), ("internet settings", 3.0), ("settings", 1.5), ("mms", 2.5), ("configure", 2.0),
        ("configuration", 2.0), ("new phone", 1.5), ("simu mpya", 1.5), ("hotspot", 1.5), ("set up", 1.0),
    ),
    "roaming": (
        ("roaming", 3.5), ("abroad", 2.5), ("nje ya nchi", 2.5), ("travelling", 1.5), ("traveling", 1.5),
        ("travel", 1.0), ("uganda", 1.0), ("tanzania", 1.0), ("rwanda", 1.0), ("dubai", 1.0),
    ),
    "account": (
        ("register", 2.0), ("registration", 2.5), ("haijasajiliwa", 3.0), ("sajili", 2.5),
        ("kuhamia mtandao", 3.0), ("hamia mtandao", 3.0), ("nibaki na namba", 3.0),
        ("ownership", 2.5), ("owner of the line", 3.0), ("owner of my line", 3.0), ("id number", 1.5),
        ("porting", 3.0), ("port my number", 3.5), ("move my number", 3.0), ("keep my number", 3.0),
        ("switch network", 2.5), ("another network", 1.5), ("hamia", 2.0), ("mnp", 3.0),
    ),
    "other": (
        ("regulator", 3.0), ("escalate my complaint", 3.0), ("escalate", 1.5), ("not satisfied", 2.0),
        ("sijaridhika", 2.5), ("where else can i complain", 3.0), ("complaint was handled", 2.0),
    ),
}

#: Risk flags, keyed by the escalation reason code they raise.
RISK_LEXICON: dict[str, tuple[str, ...]] = {
    "fraud_or_sim_swap": (
        "sim swap", "simswap", "swapped my sim", "sim was swapped", "line was swapped", "imeswapiwa",
        "swapiwa", "laini yangu imebadilishwa", "someone is using my mpesa", "someone used my mpesa",
        "mtu anatumia mpesa yangu", "pin changed", "changed my pin", "pin was changed", "pin yangu imebadilishwa",
        "unknown pin change", "did not change my pin", "sikubadilisha pin", "account takeover", "hacked",
        "fraud", "fraudster", "conman", "con man", "mlaghai", "walaghai", "scammed", "unauthorised withdrawal",
        "unauthorized withdrawal", "unauthorised transaction", "unauthorized transaction",
        "without my knowledge", "pretending to be customer care", "pretended to be customer care",
        "withdrawal i did not make", "transaction i did not make", "transactions i did not make",
        "payment i did not make", "did not authorise", "did not authorize", "didnt authorise", "didnt authorize",
        "not authorised by me", "sikutoa pesa", "sijatoa pesa", "transactions i never did",
        "transaction i never did", "transactions i never made", "money missing from my mpesa",
    ),
    "legal_or_regulator": (
        "lawyer", "lawyers", "advocate", "advocates", "court", "sue you", "sue", "suing", "legal action",
        "demand letter", "letter of demand", "formal demand", "communications authority", "odpc",
        "data commissioner", "data protection commissioner", "small claims", "mahakama", "mahakamani", "wakili",
        "nitawashtaki", "kushtaki", "tribunal",
    ),
    "threat_or_safety": (
        "kill myself", "end my life", "suicide", "suicidal", "kujiua", "nitajiua", "harm myself",
        "hurt myself", "self harm", "kill you", "i will kill", "nitakuua", "nitawaua", "burn your",
        "bomb", "threatening me", "threatened me", "threatens me", "threatening messages", "ananitishia",
        "wananitishia", "vitisho", "harassing me", "harassment", "harass", "stalking", "stalker",
        "blackmail", "blackmailing",
    ),
}
#: Abbreviations matched case-sensitively on the ORIGINAL text.
CASE_SENSITIVE_RISK: dict[str, tuple[str, ...]] = {"legal_or_regulator": ("CA", "CAK")}

URGENT_WORDS: tuple[str, ...] = (
    "urgent", "urgently", "emergency", "asap", "immediately", "haraka", "sasa hivi", "business",
    "biashara", "hospital", "losing money", "critical",
)
QUESTION_OPENERS: tuple[str, ...] = (
    "how do i", "how can i", "how to", "what is", "what are", "can i", "is it possible",
    "jinsi ya", "naweza aje", "nawezaje", "ninawezaje", "nifanye nini", "where can i",
)
#: Anger words with weights; ``angry`` needs a total of 2 (one strong word, or two milder).
ANGER_LEXICON: Lexicon = (
    ("thieves", 2.0), ("wezi", 2.0), ("idiots", 2.0), ("matapeli", 2.0), ("furious", 2.0),
    ("fed up", 2.0), ("useless", 2.0), ("nonsense", 2.0), ("upuzi", 2.0), ("ujinga", 2.0),
    ("rubbish", 2.0), ("pathetic", 2.0), ("stupid", 2.0), ("shame on you", 2.0), ("mmeniibia", 2.0),
    ("nimekasirika", 2.0), ("very angry", 2.0), ("angry", 1.0), ("hasira", 1.0), ("worst", 1.0),
    ("terrible", 1.0), ("ridiculous", 1.0), ("disgusted", 1.0), ("unacceptable", 1.0),
)
FRUSTRATION_WORDS: tuple[str, ...] = (
    "again", "still", "bado", "tena", "third time", "mara ya tatu", "disappointed", "nimechoka",
    "kila siku", "since yesterday", "tangu jana", "for days", "siku tatu", "no one is helping",
    "nobody is helping", "waiting", "frustrated", "annoying", "imagine",
)

# ------------------------------------------------------------------------------- intents
# An intent is what the action agent could DO. Each needs its category to have won and one of
# its trigger phrases; "outage" also needs a place the gazetteer knows.

INTENTS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    # intent: (category, tool, trigger phrases)
    "reverse_mpesa": ("mpesa", "reverse_mpesa", (
        "wrong number", "wrong person", "wrong recipient", "namba mbaya", "nimekosea namba", "kimakosa",
        "reverse", "reversal", "rudisha", "mtu mwingine", "mistake", "mistakenly", "by mistake",
    )),
    "recredit_bundle": ("data_bundles", "recredit_bundle", (
        "expired early", "imeisha mapema", "zimeisha mapema", "not applied", "haijaingia", "not received",
        "never received", "didnt get", "did not get", "sikupata", "sijapata", "expired before",
        "finished quickly", "finished fast", "ran out fast", "zimeisha haraka", "imeisha haraka", "recredit",
        "re credit", "within a day", "after one day", "baada ya siku moja",
    )),
    "issue_refund": ("billing", "issue_refund", (
        "refund", "rudisha", "rudisheni", "charged twice", "double charge", "double charged", "deducted twice",
        "mara mbili", "never subscribed", "did not subscribe", "didnt subscribe", "sikujiunga",
        "without my consent", "airtime deducted", "airtime imekatwa", "credo imekatwa", "salio imekatwa",
        "nimekatwa", "deducted without", "bila sababu", "for no reason", "without reason", "nirudishie",
        "mnirudishie", "give me back my airtime",
    )),
    "link_incident": ("network", "link_incident", (
        "no network", "hakuna network", "hakuna mtandao", "network down", "no signal", "hakuna signal",
        "network imepotea", "network iko chini", "outage", "emergency calls only", "no service",
        "network problem", "network issues", "mtandao umepotea", "network inapotea", "network haipo",
        "cant call", "cannot call", "cant make calls", "cannot make calls",
    )),
    "reset_network_settings": ("device_settings", "reset_network_settings", (
        "apn", "internet settings", "mms", "settings", "configure", "configuration", "set up", "setup",
    )),
}
#: Words that mean a billing complaint is NOT asking for a refund (repayment, bill queries).
REFUND_EXCLUSIONS: tuple[str, ...] = ("airtime advance", "borrowed airtime", "okoa", "nilikopa", "postpaid bill", "my bill")


def _compile(phrases: tuple[str, ...]) -> tuple[tuple[str, re.Pattern[str]], ...]:
    return tuple((p, phrase_pattern(p)) for p in phrases)


_CATEGORY_PATTERNS = {
    cat: tuple((p, w, phrase_pattern(p)) for p, w in lex) for cat, lex in CATEGORY_LEXICON.items()
}
_RISK_PATTERNS = {code: _compile(phrases) for code, phrases in RISK_LEXICON.items()}
_CASE_PATTERNS = {
    code: tuple((p, re.compile(r"(?<![A-Za-z])" + re.escape(p) + r"(?![A-Za-z])")) for p in phrases)
    for code, phrases in CASE_SENSITIVE_RISK.items()
}
_URGENT = _compile(URGENT_WORDS)
_QUESTION = _compile(QUESTION_OPENERS)
_ANGER = tuple((p, w, phrase_pattern(p)) for p, w in ANGER_LEXICON)
_FRUSTRATION = _compile(FRUSTRATION_WORDS)
_INTENT_PATTERNS = {name: (cat, tool, _compile(ph)) for name, (cat, tool, ph) in INTENTS.items()}
_REFUND_EXCLUSIONS = _compile(REFUND_EXCLUSIONS)
_SHOUTING = re.compile(r"!{2,}")

PRIOR = 1.0  # the "+ 1" in the confidence formula (module docstring)
#: A fraud flag is also evidence for the fraud CATEGORY: "someone is using my M-PESA" is an
#: account-takeover complaint even though the only category word in it is "M-PESA".
FRAUD_CATEGORY_WEIGHT = 3.0
MONEY_HIGH_URGENCY_KES = 1000


# ------------------------------------------------------------------------------- result


@dataclass
class TriageResult:
    category: str
    confidence: float
    urgency: str
    sentiment: str
    language: str
    risk_flags: tuple[str, ...]
    intent: str | None
    tool: str | None
    route: str
    places: tuple[PlaceMention, ...]
    scores: dict[str, float]
    reasons: list[str] = field(default_factory=list)
    source: str = "rules"  # rules | llm_tiebreak

    def detail(self) -> dict[str, Any]:
        """The step trace's structured detail."""
        return {
            "category": self.category,
            "confidence": self.confidence,
            "urgency": self.urgency,
            "sentiment": self.sentiment,
            "language": self.language,
            "risk_flags": list(self.risk_flags),
            "intent": self.intent,
            "tool": self.tool,
            "route": self.route,
            "places": [{"name": p.name, "regions": list(p.regions)} for p in self.places],
            "scores": {k: v for k, v in sorted(self.scores.items(), key=lambda kv: -kv[1]) if v > 0},
            "reasons": self.reasons,
            "source": self.source,
        }


# ------------------------------------------------------------------------------ helpers


def _hits(norm: str, patterns: tuple[tuple[str, re.Pattern[str]], ...]) -> list[str]:
    return [phrase for phrase, pattern in patterns if pattern.search(norm)]


def score_categories(norm: str) -> tuple[dict[str, float], dict[str, list[str]]]:
    """Every category's summed phrase weight, and which phrases fired for it."""
    scores: dict[str, float] = {}
    fired: dict[str, list[str]] = {}
    for category, patterns in _CATEGORY_PATTERNS.items():
        matched = [(p, w) for p, w, pattern in patterns if pattern.search(norm)]
        scores[category] = round(sum(w for _, w in matched), 3)
        fired[category] = [p for p, _ in matched]
    return scores, fired


def confidence_of(scores: dict[str, float]) -> tuple[str, float]:
    """The winning category and ``top / (top + runner_up + PRIOR)``; no evidence is ``other`` at 0."""
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    top_cat, top = ranked[0]
    if top <= 0:
        return "other", 0.0
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
    return top_cat, round(top / (top + runner_up + PRIOR), 3)


def risk_flags(text: str, norm: str) -> dict[str, list[str]]:
    """Reason code -> the phrases that raised it (only codes that fired)."""
    flags: dict[str, list[str]] = {}
    for code, patterns in _RISK_PATTERNS.items():
        hits = _hits(norm, patterns)
        hits += [p for p, pattern in _CASE_PATTERNS.get(code, ()) if pattern.search(text)]
        if hits:
            flags[code] = hits
    return flags


def sentiment_of(text: str, norm: str) -> tuple[str, list[str]]:
    """``angry`` (anger weight >= 2), ``frustrated`` (some anger or a frustration word), else ``calm``."""
    fired = [(p, w) for p, w, pattern in _ANGER if pattern.search(norm)]
    weight = sum(w for _, w in fired)
    signals = [p for p, _ in fired]
    if _SHOUTING.search(text):
        weight += 1
        signals.append("!!")
    letters = [c for c in text if c.isalpha()]
    if len(letters) >= 12 and sum(c.isupper() for c in letters) / len(letters) >= 0.6:
        weight += 1
        signals.append("CAPITALS")
    if weight >= 2:
        return "angry", signals
    frustration = _hits(norm, _FRUSTRATION)
    if weight > 0 or frustration:
        return "frustrated", signals + frustration
    return "calm", []


def urgency_of(norm: str, category: str, flags: dict[str, list[str]], amounts: list[int]) -> tuple[str, list[str]]:
    """critical: fraud or safety; high: legal, urgency words, or money over KES 1,000 at stake;
    low: a plain how-to question; normal otherwise."""
    if "fraud_or_sim_swap" in flags or "threat_or_safety" in flags:
        return "critical", ["risk flag"]
    urgent = _hits(norm, _URGENT)
    big_money = category in ("mpesa", "billing") and any(a >= MONEY_HIGH_URGENCY_KES for a in amounts)
    if "legal_or_regulator" in flags or urgent or big_money:
        return "high", urgent + (["amount >= KES 1,000"] if big_money else []) + (["legal flag"] if "legal_or_regulator" in flags else [])
    if any(pattern.match(norm) for _, pattern in _QUESTION):
        return "low", ["how-to question"]
    return "normal", []


def intent_of(norm: str, category: str, places: list[PlaceMention]) -> tuple[str | None, str | None, list[str]]:
    """The action intent for the winning category, its tool, and the trigger phrases that fired."""
    for name, (intent_category, tool, patterns) in _INTENT_PATTERNS.items():
        if intent_category != category:
            continue
        triggers = _hits(norm, patterns)
        if not triggers:
            continue
        if name == "link_incident" and not places:
            continue  # an outage with no place cannot be matched to a ticket: the resolver answers
        if name == "issue_refund" and _hits(norm, _REFUND_EXCLUSIONS):
            continue
        return name, tool, triggers
    return None, None, []


# ------------------------------------------------------------------------- LLM tie-break


class TieBreak(BaseModel):
    category: str
    reason: str = ""


TIEBREAK_SYSTEM = (
    "You sort a mobile network customer's complaint into exactly one of the categories offered. "
    "Reply with JSON: {\"category\": <one of the offered categories>, \"reason\": <at most 15 words>}. "
    "Never repeat personal details from the complaint."
)


def llm_tiebreak(port: Any, text: str, candidates: list[str], *, model: str = "claude-opus-5") -> str | None:
    """Ask the model to choose between ``candidates``; any other answer or any failure is None."""
    from noc_agents.llm.redaction import scrub_contacts

    try:
        parsed, _record = port.draft(
            model=model,
            system=TIEBREAK_SYSTEM,
            user=f"Categories: {', '.join(candidates)}\nComplaint: {scrub_contacts(text) or ''}",
            output_model=TieBreak,
            effort="low",
            max_tokens=256,
        )
    except Exception:  # noqa: BLE001 -- a tie-break must never be why a complaint fails
        log.warning("support triage: LLM tie-break failed; keeping the rule result")
        return None
    choice = getattr(parsed, "category", None) if parsed is not None else None
    return choice if choice in candidates else None


# ------------------------------------------------------------------------------- triage


def triage(text: str, *, gazetteer: Gazetteer, policy: SupportPolicy, port: Any | None = None) -> TriageResult:
    """Classify ``text`` and choose the route. Deterministic unless ``port`` is given."""
    original = clean(text)
    norm = normalise(original)
    flags = risk_flags(original, norm)
    scores, fired = score_categories(norm)
    if "fraud_or_sim_swap" in flags:
        scores["sim_and_fraud"] += FRAUD_CATEGORY_WEIGHT
        fired["sim_and_fraud"].append("fraud flag")
    category, confidence = confidence_of(scores)
    matched = fired.get(category, [])
    reasons = [f"category {category}: {', '.join(matched)}" if matched else "category other: no phrase matched"]
    source = "rules"

    ranked = [c for c, s in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0])) if s > 0]
    if port is not None and confidence < policy.llm_tiebreak_below and len(ranked) >= 2:
        choice = llm_tiebreak(port, original, ranked[:2])
        if choice is not None:
            category, source = choice, "llm_tiebreak"
            confidence = max(confidence, policy.low_confidence_threshold)
            reasons.append(f"LLM tie-break chose {choice} from {ranked[:2]}")

    for code, phrases in flags.items():
        reasons.append(f"risk {code}: " + ", ".join(phrases))
    amounts = extract_amounts(original)
    urgency, urgency_why = urgency_of(norm, category, flags, amounts)
    if urgency_why:
        reasons.append(f"urgency {urgency}: " + ", ".join(urgency_why))
    sentiment, sentiment_why = sentiment_of(original, norm)
    if sentiment_why:
        reasons.append(f"sentiment {sentiment}: " + ", ".join(sentiment_why))
    places = gazetteer.find(original)

    intent, tool, triggers = (None, None, []) if flags else intent_of(norm, category, places)
    if intent:
        reasons.append(f"intent {intent}: " + ", ".join(triggers))
    route = "human" if flags else ("action" if intent else "resolver")

    return TriageResult(
        category=category,
        confidence=confidence,
        urgency=urgency,
        sentiment=sentiment,
        language=detect_language(original),
        risk_flags=tuple(code for code in RISK_LEXICON if code in flags),
        intent=intent,
        tool=tool,
        route=route,
        places=tuple(places),
        scores=scores,
        reasons=reasons,
        source=source,
    )
