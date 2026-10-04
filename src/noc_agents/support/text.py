"""Text handling shared by triage and retrieval: normalisation, tokens, language, MSISDNs.

Complaints on a Kenyan network arrive in English, Kiswahili and Sheng, usually mixed in one
sentence ("bundles zimeisha mapema na sijapata refund"). Two different jobs need the text:

* **phrase rules** (triage) match on :func:`normalise` -- lower case, punctuation gone,
  ``M-PESA`` / ``m pesa`` folded to ``mpesa`` -- so a rule written once matches every spelling;
* **retrieval** (BM25 in :mod:`kb`) works on :func:`tokens`, which also maps a small
  Swahili/Sheng/English synonym table onto one canonical word ("laini" and "sim" are the same
  thing to a customer, so they must be the same term to the index), drops stop words and
  strips an English plural.

The synonym table is deliberately small and literal. It is not a translator: it holds the
words that actually carry a support complaint's meaning, where a miss would send the
resolver to the wrong article. Extending it is a matter of adding a line, and the golden set
(``tests/fixtures/support_eval/golden.jsonl``) is how to check the line helped.

MSISDNs are normalised to E.164 (``+2547XXXXXXXX`` / ``+2541XXXXXXXX``) on the way in and
shown masked everywhere a person reads them (``+254 7•• ••• 412``).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

# ------------------------------------------------------------------------- normalisation

_MPESA = re.compile(r"\bm[\s\-_.]*pesa\b", re.IGNORECASE)
_APOSTROPHES = re.compile(r"['\u2019`]")
_NON_WORD = re.compile(r"[^\w\s]+", re.UNICODE)
_SPACES = re.compile(r"\s+")


def clean(text: str | None) -> str:
    """Trim and collapse whitespace; the form that is stored and shown."""
    return _SPACES.sub(" ", unicodedata.normalize("NFKC", text or "")).strip()


def normalise(text: str | None) -> str:
    """Lower case, ``M-PESA`` folded to ``mpesa``, apostrophes dropped ("Murang'a" = "Muranga"),
    other punctuation replaced by spaces, whitespace collapsed."""
    folded = _APOSTROPHES.sub("", _MPESA.sub("mpesa", clean(text)).lower())
    return _SPACES.sub(" ", _NON_WORD.sub(" ", folded).replace("_", " ")).strip()


def body_hash(text: str | None) -> str:
    """Fingerprint for the two-minute dedupe: the normalised text, so case and spacing do not count."""
    return hashlib.sha256(normalise(text).encode("utf-8")).hexdigest()


def phrase_pattern(phrase: str) -> re.Pattern[str]:
    """A whole-word regex for ``phrase`` over :func:`normalise`-d text."""
    return re.compile(r"(?<!\w)" + re.escape(normalise(phrase)) + r"(?!\w)")


# ---------------------------------------------------------------------------- tokens

#: Swahili / Sheng / English variants -> one canonical term. Keys are single normalised words.
SYNONYMS: dict[str, str] = {
    # network
    "mtandao": "network", "signal": "network", "signals": "network", "netwok": "network",
    "inakatika": "drop", "zinakatika": "drop", "kukatika": "drop", "inakatwa": "drop",
    "dropping": "drop", "dropped": "drop", "drops": "drop",
    "polepole": "slow", "slowly": "slow", "inazunguka": "buffering",
    "fiber": "fibre", "wifi": "wifi",
    # money and M-PESA
    "pesa": "money", "hela": "money", "doh": "money", "ganji": "money", "mullah": "money", "mulla": "money",
    "namba": "number", "nambari": "number", "mbaya": "wrong", "vibaya": "wrong", "kimakosa": "wrong",
    "nimekosea": "wrong", "mistake": "wrong", "mistakenly": "wrong", "wrongly": "wrong",
    "rudisha": "reverse", "rudishiwa": "reverse", "reversal": "reverse", "reversed": "reverse",
    "haijafika": "notreceived", "sijapokea": "notreceived", "hajapokea": "notreceived",
    "imekwama": "pending", "stuck": "pending",
    # airtime, charges, refunds
    "credo": "airtime", "credit": "airtime", "salio": "airtime", "bamba": "airtime",
    "imekatwa": "deducted", "nimekatwa": "deducted", "kukatwa": "deducted", "katwa": "deducted",
    "zimekatwa": "deducted", "deduction": "deducted", "deductions": "deducted",
    "charged": "charge", "charging": "charge", "refunded": "refund",
    "okoa": "loan", "deni": "loan", "mkopo": "loan", "nilikopa": "loan", "borrowed": "loan",
    "advance": "loan", "bili": "bill",
    "msg": "sms", "msgs": "sms", "meseji": "sms", "messages": "sms", "message": "sms", "texts": "sms",
    "subscribed": "subscription", "subscribe": "subscription", "unsubscribe": "subscription",
    # bundles
    "bando": "bundle", "bundles": "bundle", "mbs": "data", "megabytes": "data",
    "imeisha": "expired", "zimeisha": "expired", "imeliwa": "expired", "finished": "expired",
    "expire": "expired", "haijaingia": "notapplied",
    # SIM, phone, fraud
    "laini": "sim", "line": "sim", "simu": "phone", "handset": "phone",
    "imeibiwa": "stolen", "nimeibiwa": "stolen", "imeibwa": "stolen", "stole": "stolen", "snatched": "stolen",
    "nimepoteza": "lost", "imepotea": "lost", "lose": "lost",
    "imeswapiwa": "swap", "swapiwa": "swap", "swapped": "swap", "simswap": "swap",
    "imejifunga": "locked", "imelock": "locked", "lock": "locked",
    # account and porting
    "sajili": "register", "haijasajiliwa": "register", "registration": "register", "registered": "register",
    "hamia": "switch", "kuhamia": "switch", "hama": "switch", "porting": "port", "ported": "port",
    "nibaki": "keep", "kubaki": "keep", "mwingine": "another", "nyingine": "another",
    # roaming
    "nje": "abroad", "travelling": "abroad", "traveling": "abroad", "overseas": "abroad",
}

#: Function words in English and Kiswahili that carry no support meaning.
STOPWORDS: frozenset[str] = frozenset(
    """
    a an the and or but to of in on at for from with by as is are was were be been being am
    it its this that these those i me my mine we our you your he she they them their his her
    have has had do does did not no yes so if then than too very can could would should will
    just also please kindly help hi hello dear sir madam thanks thank regards what when where
    which who why how there here get got any some all again still now today yesterday
    na ya wa kwa ni za la cha vya katika yangu wangu langu changu yako yenu hii hiyo huyo hizi
    kuna iko niko tu sana lakini bado pia au kama sasa leo jana mimi wewe nyinyi sisi tafadhali
    kila nini huku
    naomba nisaidie saidia asante habari jambo bwana mama hapo hapa juu kwenye
    """.split()
)


def _stem(token: str) -> str:
    """Strip an English plural ``s`` from longer words; never touches a Swahili word's vowel ending."""
    if len(token) >= 5 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    return token


#: Negations survive stop-word removal as ``no`` so that a phrase keeps its meaning in a
#: bigram: "no network", "hakuna network" and "no signal" all become ``no_network``.
NEGATIONS: frozenset[str] = frozenset({"no", "not", "hakuna", "sina", "haina", "hamna", "without"})


def _mapped_words(text: str | None) -> list[str]:
    """Normalised words with synonyms mapped and plurals stripped; negations as ``no``; stop words dropped."""
    out: list[str] = []
    for word in normalise(text).split():
        if word in NEGATIONS:
            out.append("no")
        elif word not in STOPWORDS and len(word) >= 2:
            out.append(SYNONYMS.get(word) or SYNONYMS.get(_stem(word)) or _stem(word))
    return out


def tokens(text: str | None) -> list[str]:
    """Retrieval unigrams: normalised words, synonyms mapped, stop words and negations dropped."""
    return [word for word in _mapped_words(text) if word != "no"]


def terms(text: str | None) -> list[str]:
    """What BM25 indexes and queries: the unigrams plus adjacent-word bigrams (``wrong_number``).

    Bigrams are what let "sent to the wrong number" outscore an article that merely mentions
    a number and, elsewhere, something wrong; they are built after stop-word removal, so
    "wrong number" and "namba mbaya" (mapped to the same two words) meet as one term.
    """
    words = _mapped_words(text)
    bigrams = [f"{a}_{b}" for a, b in zip(words, words[1:], strict=False) if b != "no"]
    return tokens(text) + bigrams


# --------------------------------------------------------------------------- language

_SW_WORDS: frozenset[str] = frozenset(
    """
    na ya wa kwa ni za la cha vya yangu wangu langu changu yako hii hiyo kuna iko niko tu sana
    lakini bado pia kama sasa leo jana mimi wewe nyinyi tafadhali naomba nisaidie asante habari
    hakuna sina pesa laini simu mtandao namba mbaya rudisha imeisha zimeisha mapema haraka
    sijapata sijapokea haijafika haijaingia imekatwa nimekatwa nimetuma nimekosea imeibiwa
    nimepoteza imepotea siku wiki mwezi mara mbili tatu tena kila nini gani aje vipi wapi
    nataka ninataka sitaki tumeni nyumbani mtu mwingine kitu hapa pale juu chini ama
    manze msee buda doh ganji mbao form niaje sare vile venye kwani
    """.split()
)
_SW_PREFIXES: tuple[str, ...] = ("nime", "ime", "zime", "haija", "sija", "hatu", "tume", "wame", "ana", "ina", "nita", "uta")
_EN_WORDS: frozenset[str] = frozenset(
    """
    the my is and to i have not please was for it on a an of in with this that you your
    me we our they been has had do did does can will would should what when where why how
    since but or from at by be are am no yes there their just about after before because
    money network sent send bought buy phone line number data bundle airtime charged refund
    """.split()
)


def _is_swahili(word: str) -> bool:
    return word in _SW_WORDS or (len(word) > 5 and word.startswith(_SW_PREFIXES))


def detect_language(text: str | None) -> str:
    """``en`` | ``sw`` | ``mixed`` from function-word and verb-prefix counts.

    Sheng counts with Kiswahili: the point is to choose the reply language and to tell the
    floor what it is reading, and a Sheng complaint gets a Kiswahili-capable agent. A text with
    no recognisable words reads as English (the form's language).
    """
    words = normalise(text).split()
    sw = sum(1 for w in words if _is_swahili(w))
    en = sum(1 for w in words if (w in _EN_WORDS or _stem(w) in _EN_WORDS) and not _is_swahili(w))
    if sw + en == 0:
        return "en"
    share = sw / (sw + en)
    if share >= 0.7:
        return "sw"
    if share <= 0.25:
        return "en"
    return "mixed"


# ----------------------------------------------------------------- amounts and M-PESA codes

#: "KES 1,500", "ksh1500", "1500 bob", "1.5k", or a bare 2-7 digit number that is not part of a
#: phone number. The bare form matters: customers write "I sent 1500 to the wrong number".
_AMOUNT = re.compile(
    r"(?<![\d+.])(?:(?:kes|kshs?|shs?)\.?\s*)?(\d{1,3}(?:,\d{3})+|\d{2,7})(?:\.\d+)?(?![\d,])"
    r"|(?<![\d.])(\d+(?:\.\d+)?)\s?k\b",
    re.IGNORECASE,
)
#: Ten letters and digits with at least two of each: the shape of an M-PESA code, and not the
#: shape of an English word (no digits) or a phone number (no letters).
_CODE = re.compile(r"\b(?=(?:[A-Za-z0-9]*\d){2})(?=(?:[A-Za-z0-9]*[A-Za-z]){2})[A-Za-z0-9]{10}\b")


def extract_amounts(text: str | None) -> list[int]:
    """Shilling amounts mentioned in ``text``, in order (``1.5k`` -> 1500). Phone numbers are skipped."""
    found: list[int] = []
    for plain, thousands in _AMOUNT.findall(clean(text)):
        if thousands:
            found.append(int(float(thousands) * 1000))
        elif plain:
            found.append(int(plain.replace(",", "")))
    return found


def extract_mpesa_codes(text: str | None) -> list[str]:
    """Candidate M-PESA transaction codes in ``text``, upper-cased, in order."""
    return [code.upper() for code in _CODE.findall(clean(text))]


# ----------------------------------------------------------------------------- MSISDN

_MSISDN = re.compile(r"^(?:\+?254|0)?([71]\d{8})$")
_MSISDN_SEPARATORS = re.compile(r"[\s\-().]")


class InvalidMsisdn(ValueError):
    """Not a Kenyan mobile number in any of the accepted spellings."""


def normalise_msisdn(raw: str | None) -> str:
    """``07xx``, ``01xx``, ``+2547xx``, ``2541xx`` (spaces and dashes allowed) -> ``+2547XXXXXXXX``.

    Raises :class:`InvalidMsisdn` for anything else, including landlines and foreign numbers.
    """
    digits = _MSISDN_SEPARATORS.sub("", raw or "")
    match = _MSISDN.match(digits)
    if not match:
        raise InvalidMsisdn("msisdn must be a Kenyan mobile number: 07XXXXXXXX, 01XXXXXXXX or +2547/+2541 followed by 8 digits")
    return "+254" + match.group(1)


def mask_msisdn(msisdn: str) -> str:
    """``+254700000412`` -> ``+254 7•• ••• 412``: the network prefix and the last three digits only."""
    national = normalise_msisdn(msisdn)[4:]
    return f"+254 {national[0]}•• ••• {national[-3:]}"
