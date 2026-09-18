"""GSM 03.38 alphabet, segment maths and transliteration — spec §6.2 (SMS hard validator).

Why this module exists at all: **one character changes the bill.** An SMS whose every
character lives in the GSM 7-bit default alphabet is packed 8 characters into 7 bytes, so a
single PDU carries **160** characters. The moment one character falls outside that alphabet
— a curly apostrophe pasted from Word, an en dash, an emoji, a non-breaking space — the
*whole* message is re-encoded as UCS-2 (UTF-16BE) and the single-PDU budget collapses to
**70**. Concatenated messages give up 6 septets (7 bytes) per part to the user-data header,
so the per-part budget drops to **153** (GSM-7) and **67** (UCS-2).

    "[P1] INC000123 ... Owner:Grace — ticket notes"   one em dash → UCS-2 → 70/segment

For a NOC that fires thousands of alerts a month this is not cosmetic: a P1 blast that
quietly becomes three segments instead of one costs triple, and multi-part SMS can arrive
out of order or lose a part on a congested network — an alert that reads
"...est.users 45" because part 2 never landed is worse than no alert.

**The classic off-by-one.** Ten characters (``^ { } \\ [ ] ~ |`` and ``€``, plus form feed)
are *not* in the 128-entry basic table. They are reachable only as an ESC pair, so each one
costs **two** septets, not one. Counting them with ``len()`` under-counts the message and
tells you "160, one segment" for something the SMSC splits in two. Worse, an ESC pair may
not straddle a segment boundary: when only one septet is left in a part, the ESC is pushed
into the next part and that last septet is wasted padding. ``sms_cost`` models both rules —
see ``_pack`` — so ``"A"*152 + "€"*77`` is 306 septets (a naive ``ceil(306/153)`` says two
parts) and really costs **three**.

**Kiswahili** (§6.4) is written in plain Latin script with no diacritics, so ordinary
Kiswahili NOC prose is GSM-7 clean and costs exactly what English costs *per character* —
pinned by ``test_gsm7.py::test_a_real_kiswahili_noc_sentence_is_gsm7``. It is usually the
*longer* of the two translations, though, so the segment count still has to be measured per
language rather than assumed from the English original.

Nothing here mutates anything implicitly: ``is_gsm7`` / ``sms_cost`` / ``non_gsm7_chars``
are pure measurements, and ``to_gsm7`` only ever changes text when the caller asks it to,
reporting every substitution it made through ``transliterate``.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Literal, Sequence

__all__ = [
    "GSM7_BASIC",
    "GSM7_CONCAT_LIMIT",
    "GSM7_EXTENDED",
    "GSM7_SINGLE_LIMIT",
    "TRANSLITERATIONS",
    "UCS2_CONCAT_LIMIT",
    "UCS2_SINGLE_LIMIT",
    "Offender",
    "SmsCost",
    "Substitution",
    "Transliteration",
    "describe_offenders",
    "gsm7_length",
    "is_gsm7",
    "non_gsm7_chars",
    "segments_for",
    "sms_cost",
    "to_gsm7",
    "transliterate",
    "ucs2_length",
]

# --------------------------------------------------------------------------- alphabet

# GSM 03.38 default alphabet, code points 0x00-0x7F in table order. 0x1B is ESC (the
# extension prefix) and is deliberately NOT a usable character, so it is absent below.
GSM7_BASIC: frozenset[str] = frozenset(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ"
    " !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§"
    "¿abcdefghijklmnopqrstuvwxyzäöñüà"
)

# Extension table (ESC + code point). Each of these costs TWO septets. ``\f`` (form feed,
# ext 0x0A) is included for completeness; it never appears in NOC copy.
GSM7_EXTENDED: frozenset[str] = frozenset("\f^{}\\[~]|€")

_GSM7_ALL: frozenset[str] = GSM7_BASIC | GSM7_EXTENDED

# Per-PDU budgets. The concatenated figures are the single-PDU budgets less the 6 septets
# (7 octets) the user-data header takes from the first and every following part.
GSM7_SINGLE_LIMIT = 160
GSM7_CONCAT_LIMIT = 153
UCS2_SINGLE_LIMIT = 70
UCS2_CONCAT_LIMIT = 67

Encoding = Literal["GSM7", "UCS2"]


# --------------------------------------------------------------------- transliteration

# What ``to_gsm7`` will substitute, and what ``non_gsm7_chars`` offers a template author as
# the fix. Deliberately small and explicit: these are the offenders that actually reach NOC
# copy (word processors, chat clients and pasted vendor mails), not a Unicode folding table.
#
# ``~`` is the odd one out: it IS reachable in GSM-7, but only as a 2-septet escape pair and
# several Kenyan aggregators historically mangled it, so §6.2 asks for ``-``. It is the one
# entry here that replaces a *legal* character, which is why ``to_gsm7`` is opt-in.
TRANSLITERATIONS: dict[str, str] = {
    "‘": "'",   # ' left single quote
    "’": "'",   # ' right single quote / apostrophe — the #1 offender
    "‚": "'",   # ‚
    "‛": "'",
    "′": "'",   # ′ prime
    "“": '"',   # " left double quote
    "”": '"',   # " right double quote
    "„": '"',   # „
    "″": '"',   # ″ double prime
    "«": '"',   # «
    "»": '"',   # »
    "‐": "-",   # ‐ hyphen
    "‑": "-",   # ‑ non-breaking hyphen
    "‒": "-",   # ‒ figure dash
    "–": "-",   # – en dash
    "—": "-",   # — em dash — the v1 SMS template's UCS-2 trigger (§6.2)
    "―": "-",   # ―
    "−": "-",   # − minus sign
    "~": "-",        # legal but 2 septets; §6.2 asks for the plain hyphen
    "…": "...",  # … ellipsis (3 septets instead of a UCS-2 message)
    " ": " ",   # non-breaking space — invisible, and it breaks everything
    " ": " ",
    " ": " ",
    " ": " ",
    " ": " ",
    "\t": " ",       # tab is NOT in GSM-7
    "​": "",    # zero-width space — silently forces UCS-2, silently removed
    "﻿": "",    # BOM / zero-width no-break space
    "•": "*",   # • bullet
    "·": ".",   # · middle dot
    "→": "->",  # →
    "×": "x",   # ×
}


@dataclass(frozen=True)
class Substitution:
    """One change ``to_gsm7`` made, so a caller can show its work."""

    index: int          # position in the ORIGINAL string
    original: str
    replacement: str    # "" when the character was dropped

    @property
    def dropped(self) -> bool:
        return self.replacement == ""


@dataclass(frozen=True)
class Transliteration:
    """Result of ``transliterate``: the new text plus every change it required."""

    text: str
    substitutions: tuple[Substitution, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.substitutions)

    @property
    def dropped(self) -> tuple[str, ...]:
        """Characters that had no GSM-7 equivalent and were deleted (emoji, symbols)."""
        return tuple(sub.original for sub in self.substitutions if sub.dropped)


@dataclass(frozen=True)
class Offender:
    """A character that forces UCS-2, described well enough to fix a template by hand."""

    char: str
    codepoint: str      # "U+2019"
    name: str           # Unicode name, "" when the character is unnamed
    count: int          # occurrences in the text
    first_index: int    # where to look first
    suggestion: str | None  # what ``to_gsm7`` would put there; None when it would be dropped

    @property
    def fixable(self) -> bool:
        return self.suggestion is not None

    def describe(self) -> str:
        fix = f"replace with {self.suggestion!r}" if self.fixable else "no GSM-7 equivalent (would be dropped)"
        label = f" {self.name}" if self.name else ""
        return f"{self.char!r} ({self.codepoint}{label}) x{self.count} at index {self.first_index}: {fix}"


@dataclass(frozen=True)
class SmsCost:
    """What a body will actually cost to send."""

    encoding: Encoding
    units: int              # septets (GSM7) or UTF-16 code units (UCS2)
    segments: int
    per_segment: int        # the budget that applied: 160/153 or 70/67
    remaining: int          # free units left in the LAST segment (padding already deducted)
    offenders: tuple[Offender, ...] = ()   # empty for GSM7

    @property
    def is_gsm7(self) -> bool:
        return self.encoding == "GSM7"

    @property
    def fixable(self) -> bool:
        """True when ``to_gsm7`` would bring this back to GSM-7 without deleting anything."""
        return bool(self.offenders) and all(o.fixable for o in self.offenders)

    def summary(self) -> str:
        head = f"{self.encoding} {self.units} units -> {self.segments} segment(s) of {self.per_segment}"
        if not self.offenders:
            return head
        return head + "; forced by " + ", ".join(o.describe() for o in self.offenders)


# ------------------------------------------------------------------------ measurement


def is_gsm7(text: str) -> bool:
    """True when every character is in the basic or the extension table."""
    return all(ch in _GSM7_ALL for ch in text)


def _septets(ch: str) -> int:
    """1 for a basic-table character, 2 for an ESC pair. Raises for anything else."""
    if ch in GSM7_BASIC:
        return 1
    if ch in GSM7_EXTENDED:
        return 2
    raise ValueError(f"not a GSM 03.38 character: {ch!r} (U+{ord(ch):04X})")


def gsm7_length(text: str) -> int:
    """Septet count: basic characters once, extension characters twice (§6.2).

    Raises ``ValueError`` on the first non-GSM-7 character — a septet count for an emoji is
    meaningless, and callers that want a number for *any* text want ``sms_cost`` instead.
    """
    return sum(_septets(ch) for ch in text)


def ucs2_length(text: str) -> int:
    """UTF-16 code units. Astral characters (most emoji) are surrogate pairs and count 2."""
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


def _pack(costs: Sequence[int], single: int, concat: int) -> tuple[int, int]:
    """Greedy PDU packing. Returns ``(segments, units_used_in_the_last_segment)``.

    The whole point of packing character-by-character rather than dividing the total: an
    ESC pair (2 septets) and a surrogate pair (2 code units) may not be split across a
    segment boundary, so a part with one unit left ends one unit short and the next part
    starts with the pair. That wasted unit is exactly the off-by-one that turns a
    "2 segments" estimate into a 3-segment bill.
    """
    total = sum(costs)
    if total <= single:
        return 1, total
    segments, used = 1, 0
    for cost in costs:
        if used + cost > concat:
            segments += 1
            used = cost
        else:
            used += cost
    return segments, used


def non_gsm7_chars(text: str) -> tuple[Offender, ...]:
    """Every distinct character that forces UCS-2, in first-appearance order.

    This is the "tell the template author what to fix" call. It changes nothing.
    """
    seen: dict[str, list[int]] = {}
    for index, ch in enumerate(text):
        if ch in _GSM7_ALL:
            continue
        seen.setdefault(ch, []).append(index)
    offenders: list[Offender] = []
    for ch, positions in seen.items():
        replacement = TRANSLITERATIONS.get(ch)
        if replacement is None:
            folded = _ascii_fold(ch)
            replacement = folded if folded and is_gsm7(folded) else None
        offenders.append(
            Offender(
                char=ch,
                codepoint=f"U+{ord(ch):04X}",
                name=unicodedata.name(ch, ""),
                count=len(positions),
                first_index=positions[0],
                # "" is a real, intended substitution (zero-width space); None means dropped.
                suggestion=replacement,
            )
        )
    return tuple(offenders)


def sms_cost(text: str) -> SmsCost:
    """Encoding, unit count and segment count for ``text`` exactly as an SMSC would bill it.

    Never raises and never modifies the text: one non-GSM-7 character switches the whole
    message to UCS-2, which is reported rather than fixed. An empty body reports one
    segment, because an empty send is still one PDU on the wire; the channel validators
    reject an empty body on its own terms.
    """
    if is_gsm7(text):
        costs = [_septets(ch) for ch in text]
        segments, used = _pack(costs, GSM7_SINGLE_LIMIT, GSM7_CONCAT_LIMIT)
        limit = GSM7_SINGLE_LIMIT if segments == 1 else GSM7_CONCAT_LIMIT
        return SmsCost(
            encoding="GSM7",
            units=sum(costs),
            segments=segments,
            per_segment=limit,
            remaining=limit - used,
        )
    costs = [2 if ord(ch) > 0xFFFF else 1 for ch in text]
    segments, used = _pack(costs, UCS2_SINGLE_LIMIT, UCS2_CONCAT_LIMIT)
    limit = UCS2_SINGLE_LIMIT if segments == 1 else UCS2_CONCAT_LIMIT
    return SmsCost(
        encoding="UCS2",
        units=sum(costs),
        segments=segments,
        per_segment=limit,
        remaining=limit - used,
        offenders=non_gsm7_chars(text),
    )


# --------------------------------------------------------------------- transliteration


def _ascii_fold(ch: str) -> str:
    """Compatibility-decompose and drop combining marks: ``í``→``i``, ``ﬁ``→``fi``, ``🚨``→````."""
    decomposed = unicodedata.normalize("NFKD", ch)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def transliterate(text: str, *, fold_accents: bool = False) -> Transliteration:
    """``to_gsm7`` with a receipt: the new text plus every substitution and drop.

    Order of resort per character: the explicit ``TRANSLITERATIONS`` table, then "already
    GSM-7, leave it alone", then an accent fold, then deletion.

    ``fold_accents`` additionally strips accents from characters that are *already* legal
    GSM-7 (``é``→``e``, ``ñ``→``n``). §6.2 words the rule as "accented Latin→ASCII", but
    those characters cost one septet and folding them rewrites text the author chose, so it
    is off by default — see the module report / D-item. It never folds a character whose
    decomposition is not pure ASCII (``ß``, ``ø``, ``£`` are kept as they are).
    """
    out: list[str] = []
    subs: list[Substitution] = []
    for index, ch in enumerate(text):
        if ch in TRANSLITERATIONS:
            replacement = TRANSLITERATIONS[ch]
        elif ch in _GSM7_ALL:
            folded = _ascii_fold(ch) if (fold_accents and not ch.isascii()) else ""
            replacement = folded if (folded and folded.isascii()) else ch
        else:
            folded = _ascii_fold(ch)
            replacement = folded if (folded and is_gsm7(folded)) else ""
        out.append(replacement)
        if replacement != ch:
            subs.append(Substitution(index=index, original=ch, replacement=replacement))
    return Transliteration(text="".join(out), substitutions=tuple(subs))


def to_gsm7(text: str, *, fold_accents: bool = False) -> str:
    """Return ``text`` rewritten so that ``is_gsm7`` holds (§6.2).

    Only ever called explicitly: nothing in this module rewrites a caller's text on its own,
    and the renderers decide per template whether a body is transliterated or reported.
    Use ``transliterate`` when you need to know what changed.
    """
    return transliterate(text, fold_accents=fold_accents).text


def describe_offenders(text: str) -> tuple[str, ...]:
    """One human line per offending character — for template-lint output and HITL cards."""
    return tuple(offender.describe() for offender in non_gsm7_chars(text))


def segments_for(text: str) -> int:
    """Shorthand for ``sms_cost(text).segments``."""
    return sms_cost(text).segments
