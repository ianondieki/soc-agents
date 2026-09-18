"""GSM 03.38: what a NOC alert really costs to send (spec §6.2, Kiswahili §6.4).

Every number in here is a bill. 160 characters is one SMS; one curly quote makes it 70.
An extension character costs two septets, and an escape pair may not straddle a segment
boundary — the two rules that make ``len(body)`` the wrong answer.
"""

from __future__ import annotations

import pytest

from noc_agents.services.gsm7 import (
    GSM7_BASIC,
    GSM7_CONCAT_LIMIT,
    GSM7_EXTENDED,
    GSM7_SINGLE_LIMIT,
    UCS2_CONCAT_LIMIT,
    UCS2_SINGLE_LIMIT,
    gsm7_length,
    is_gsm7,
    non_gsm7_chars,
    sms_cost,
    to_gsm7,
    transliterate,
    ucs2_length,
)

# A real P1 body in the shape the v1 template produces, minus the em dash.
ALERT_EN = (
    "[P1] INC000123 SFC-NBI-001 NBI_E\n"
    "POWER|est.users 450000\n"
    "Mains failure, generator not starting\n"
    "Owner:RNIO-NBI-E - ticket notes for updates"
)

# Ordinary Kiswahili NOC prose (§6.4). Latin script, no diacritics anywhere.
ALERT_SW = (
    "[P1] INC000123 SFC-NBI-001 NBI_E\n"
    "UMEME|wateja 450000\n"
    "Hitilafu ya umeme, jenereta haijawaka\n"
    "Msimamizi:RNIO-NBI-E - taarifa zaidi kwenye tiketi"
)


# ------------------------------------------------------------------ the alphabet


def test_the_basic_table_is_127_characters_because_esc_is_not_one_of_them():
    # 128 code points, minus 0x1B ESC which only ever prefixes an extension character.
    assert len(GSM7_BASIC) == 127
    assert len(GSM7_EXTENDED) == 10
    assert not (GSM7_BASIC & GSM7_EXTENDED)


def test_ordinary_noc_ascii_is_gsm7_and_tab_is_not():
    assert is_gsm7(ALERT_EN)
    assert is_gsm7("[P4] INC000999 restored at 14:05 EAT (est.users 1,200)")
    assert is_gsm7("\n") and is_gsm7("\r")   # LF and CR are basic-table characters
    assert not is_gsm7("\t")                  # tab is not in GSM 03.38 at all
    assert not is_gsm7("café \U0001f6a8")


# ------------------------------------------------------- segments: the plain cases


def test_a_plain_ascii_alert_is_one_segment():
    cost = sms_cost(ALERT_EN)
    assert cost.encoding == "GSM7"
    assert cost.units == gsm7_length(ALERT_EN) <= GSM7_SINGLE_LIMIT
    assert cost.segments == 1
    assert cost.per_segment == GSM7_SINGLE_LIMIT
    assert cost.remaining == GSM7_SINGLE_LIMIT - cost.units
    assert cost.offenders == ()


def test_160_ascii_fits_one_segment_but_161_costs_two_at_153_each():
    at_limit = sms_cost("A" * 160)
    assert (at_limit.segments, at_limit.per_segment, at_limit.remaining) == (1, 160, 0)

    over = sms_cost("A" * 161)
    # Not "one segment and a bit": concatenation spends 6 septets per part on the UDH,
    # so the budget drops to 153 for BOTH parts — 153 + 8.
    assert over.segments == 2
    assert over.per_segment == GSM7_CONCAT_LIMIT == 153
    assert over.units == 161
    assert over.remaining == 153 - 8


def test_the_concatenated_budget_is_153_not_160():
    assert sms_cost("A" * 306).segments == 2      # 2 x 153 exactly
    assert sms_cost("A" * 307).segments == 3      # one character over -> a third PDU
    assert sms_cost("A" * 459).segments == 3      # 3 x 153


# --------------------------------------------- segments: the extension-table trap


def test_extension_characters_cost_two_septets_each():
    for ch in "^{}\\[]~|€":
        assert gsm7_length(ch) == 2, ch
        assert is_gsm7(ch)
    assert gsm7_length("A") == 1
    assert gsm7_length("50€") == 4          # 2 digits + a 2-septet euro sign


def test_the_v1_alert_shape_already_carries_three_extension_characters():
    # "[P1] ... POWER|est.users ..." — the square brackets around the priority token and the
    # pipe between the fields are all ESC pairs, so this body is 3 septets longer than it
    # looks. Harmless at 140, decisive at 158.
    assert [ch for ch in ALERT_EN if ch in GSM7_EXTENDED] == ["[", "]", "|"]
    assert gsm7_length(ALERT_EN) == len(ALERT_EN) + 3


def test_160_characters_are_two_segments_when_one_of_them_is_an_extension_character():
    body = "A" * 159 + "€"
    assert len(body) == 160                       # a naive character count says "1 segment"
    assert gsm7_length(body) == 161                # the SMSC counts 161 septets
    assert sms_cost(body).segments == 2            # ...and bills two


def test_an_escape_pair_is_never_split_across_a_segment_boundary():
    # 152 septets of text, then euro signs. Slot 153 of part 1 cannot hold half an ESC pair,
    # so it is wasted padding and the pair starts part 2.
    body = "A" * 152 + "€" * 5
    cost = sms_cost(body)
    assert cost.units == 152 + 10
    assert cost.segments == 2

    # The same rule, one step further: 306 septets — exactly 2 x 153 on paper — really cost
    # three parts, because each part wastes its last septet on the boundary.
    tipped = "A" * 152 + "€" * 77
    assert gsm7_length(tipped) == 306
    assert 306 // GSM7_CONCAT_LIMIT == 2           # what dividing would tell you
    assert sms_cost(tipped).segments == 3          # what the network charges


# ------------------------------------------------------------- the UCS-2 collapse


def test_one_emoji_collapses_the_whole_budget_to_70_and_67():
    body = "[P1] INC000123 site down \U0001f6a8"
    cost = sms_cost(body)
    assert cost.encoding == "UCS2"
    assert cost.per_segment == UCS2_SINGLE_LIMIT == 70
    assert cost.segments == 1

    # The rest of the message is still plain ASCII — the single emoji re-encodes all of it,
    # and 68 characters that cost 68 septets suddenly cost 70 UTF-16 units.
    assert sms_cost("A" * 160).segments == 1                    # as GSM-7
    assert sms_cost("A" * 68 + "\U0001f6a8").segments == 1      # 70 units, exactly the budget
    assert sms_cost("A" * 69 + "\U0001f6a8").segments == 2      # 71 units -> concatenated
    long_with_emoji = sms_cost("A" * 70 + "\U0001f6a8")
    assert long_with_emoji.encoding == "UCS2"
    assert long_with_emoji.per_segment == UCS2_CONCAT_LIMIT == 67
    assert long_with_emoji.segments == 2           # 72 UTF-16 units over a 67-unit budget


def test_a_curly_quote_or_en_dash_is_enough_on_its_own():
    for offender in ("’", "–", "—", "…", " ", "​"):
        cost = sms_cost("Owner:Grace " + offender + " ticket notes")
        assert cost.encoding == "UCS2", offender
        assert cost.per_segment == 70


def test_an_astral_character_counts_two_ucs2_units_and_is_not_split():
    assert ucs2_length("\U0001f6a8") == 2          # surrogate pair
    assert ucs2_length("é") == 1
    body = "A" * 66 + "\U0001f6a8" * 3
    cost = sms_cost(body)
    assert cost.units == 66 + 6
    assert cost.segments == 2                      # unit 67 of part 1 is padding


def test_gsm7_length_refuses_to_guess_at_a_character_it_cannot_encode():
    with pytest.raises(ValueError) as err:
        gsm7_length("alert \U0001f6a8")
    assert "U+1F6A8" in str(err.value)


# ------------------------------------------------------------------- Kiswahili §6.4


def test_a_real_kiswahili_noc_sentence_is_gsm7():
    sw = (
        "Hitilafu ya mtandao: tovuti ya Mtito Andei imezimika kutokana na hitilafu ya umeme. "
        "Wateja takribani 12,000 wameathirika. Timu ya ufundi ipo njiani. "
        "Taarifa zaidi baada ya dakika 30."
    )
    assert is_gsm7(sw)
    assert non_gsm7_chars(sw) == ()
    assert gsm7_length(sw) == len(sw)              # no extension characters either
    # So a Kiswahili alert costs the same PER CHARACTER as an English one...
    assert sms_cost(ALERT_SW).encoding == sms_cost(ALERT_EN).encoding == "GSM7"
    assert sms_cost(ALERT_SW).per_segment == sms_cost(ALERT_EN).per_segment


def test_the_kiswahili_apostrophe_is_the_one_trap_in_swahili_copy():
    # Kiswahili spells the velar nasal "ng'" with an apostrophe. The ASCII one (U+0027) is a
    # basic-table character; the typographic one a word processor substitutes is U+2019 and
    # turns the whole alert into UCS-2 — the single most likely way a Swahili template
    # silently costs more than its English twin.
    assert is_gsm7("Umeme umekatika, taa zimeng'aa tena saa nne.")
    assert not is_gsm7("Umeme umekatika, taa zimeng’aa tena saa nne.")
    assert to_gsm7("zimeng’aa") == "zimeng'aa"


def test_kiswahili_is_longer_than_english_so_the_segment_count_is_still_measured():
    # Equal encoding is not equal cost: the Kiswahili rendering of the same alert is the
    # longer string, so a template that just fits in one segment in English can tip over.
    assert len(ALERT_SW) > len(ALERT_EN)
    filler = "A" * (GSM7_SINGLE_LIMIT - gsm7_length(ALERT_EN))
    assert sms_cost(ALERT_EN + filler).segments == 1
    assert sms_cost(ALERT_SW + filler).segments == 2


# ---------------------------------------------------------------- telling the author


def test_non_gsm7_chars_names_the_offenders_and_the_fix():
    body = "Owner’s note — café \U0001f6a8 \U0001f6a8"
    offenders = {o.char: o for o in non_gsm7_chars(body)}
    assert set(offenders) == {"’", "—", "\U0001f6a8"}   # é IS in GSM-7, so not listed

    apostrophe = offenders["’"]
    assert apostrophe.codepoint == "U+2019"
    assert apostrophe.suggestion == "'"
    assert apostrophe.fixable
    assert apostrophe.count == 1
    assert apostrophe.first_index == body.index("’")

    emoji = offenders["\U0001f6a8"]
    assert emoji.count == 2
    assert emoji.suggestion is None                 # nothing to put there; it would be dropped
    assert not emoji.fixable
    assert not sms_cost(body).fixable               # ...so the body is not auto-fixable
    assert "U+2019" in apostrophe.describe()


def test_a_body_whose_offenders_are_all_punctuation_is_reported_as_fixable():
    cost = sms_cost("Owner:Grace — see ticket…")
    assert cost.encoding == "UCS2"
    assert cost.fixable
    assert is_gsm7(to_gsm7("Owner:Grace — see ticket…"))


# --------------------------------------------------------------- transliteration


def test_to_gsm7_leaves_text_it_was_not_asked_to_change_alone():
    assert to_gsm7(ALERT_EN) == ALERT_EN
    assert to_gsm7(ALERT_SW) == ALERT_SW
    assert transliterate(ALERT_EN).changed is False
    # é, ä and ñ are GSM-7 characters at one septet each: not touched by default.
    assert to_gsm7("café naïve") == "café naive"   # ï is NOT in GSM-7, é is
    assert to_gsm7("ñ") == "ñ"


def test_to_gsm7_applies_the_substitutions_named_in_the_spec():
    assert to_gsm7("’") == "'"
    assert to_gsm7("“quoted”") == '"quoted"'
    assert to_gsm7("a – b — c") == "a - b - c"
    assert to_gsm7("wait…") == "wait..."
    assert to_gsm7("~") == "-"                       # legal but 2 septets; §6.2 asks for '-'
    assert to_gsm7("hard space") == "hard space"
    assert to_gsm7("alert \U0001f6a8 now") == "alert  now"   # emoji dropped, nothing invented
    assert is_gsm7(to_gsm7("Owner’s — café \U0001f6a8 35°C"))


def test_transliterate_shows_its_work_so_a_reviewer_can_see_the_edit():
    report = transliterate("Owner’s note — \U0001f6a8")
    assert report.changed
    assert report.text == "Owner's note - "
    assert [(s.original, s.replacement) for s in report.substitutions] == [
        ("’", "'"),
        ("—", "-"),
        ("\U0001f6a8", ""),
    ]
    assert report.dropped == ("\U0001f6a8",)
    assert report.substitutions[0].index == 5
    assert is_gsm7(report.text)


def test_fold_accents_is_opt_in_because_the_accents_are_already_legal():
    assert to_gsm7("café ñu") == "café ñu"
    assert to_gsm7("café ñu", fold_accents=True) == "cafe nu"
    # A character with no ASCII decomposition is kept rather than mangled.
    assert to_gsm7("ß ø £", fold_accents=True) == "ß ø £"


def test_transliteration_can_turn_a_two_segment_message_back_into_one():
    body = "[P1] INC000123 SFC-NBI-001 mains failure — generator not starting, RNIO notified"
    before = sms_cost(body)
    after = sms_cost(to_gsm7(body))
    assert (before.encoding, before.segments) == ("UCS2", 2)
    assert (after.encoding, after.segments) == ("GSM7", 1)
