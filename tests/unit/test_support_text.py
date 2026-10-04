"""Support desk text handling: MSISDNs, normalisation, tokens, language, amounts, M-PESA codes."""

from __future__ import annotations

import pytest

from noc_agents.support.text import (
    InvalidMsisdn,
    body_hash,
    detect_language,
    extract_amounts,
    extract_mpesa_codes,
    mask_msisdn,
    normalise,
    normalise_msisdn,
    terms,
    tokens,
)


@pytest.mark.parametrize(
    "raw",
    ["0712345412", "0712 345 412", "0712-345-412", "+254712345412", "254712345412", "+254 712 345 412", "712345412"],
)
def test_every_accepted_spelling_of_a_safaricom_number_normalises_to_e164(raw):
    assert normalise_msisdn(raw) == "+254712345412"


@pytest.mark.parametrize("raw,expected", [("0110000112", "+254110000112"), ("+254110000112", "+254110000112"),
                                          ("254110000112", "+254110000112")])
def test_the_01xx_range_is_accepted_too(raw, expected):
    assert normalise_msisdn(raw) == expected


@pytest.mark.parametrize("raw", ["", "12345", "0201234567", "+255712345678", "07123454120", "07abc45412", "+2547123", None])
def test_anything_else_is_refused(raw):
    with pytest.raises(InvalidMsisdn):
        normalise_msisdn(raw)


def test_masking_keeps_the_network_prefix_and_the_last_three_digits_only():
    assert mask_msisdn("0700000412") == "+254 7•• ••• 412"
    assert mask_msisdn("+254110000112") == "+254 1•• ••• 112"


def test_normalise_folds_mpesa_spellings_apostrophes_and_punctuation():
    assert normalise("M-PESA haifanyi!! M PESA, m-pesa.") == "mpesa haifanyi mpesa mpesa"
    assert normalise("Murang'a") == normalise("Muranga") == "muranga"


def test_the_dedupe_hash_ignores_case_and_spacing_but_not_words():
    assert body_hash("No network  in Nakuru!") == body_hash("no network in nakuru")
    assert body_hash("No network in Nakuru") != body_hash("No network in Thika")


def test_tokens_map_swahili_and_sheng_onto_one_term():
    assert tokens("laini") == tokens("sim") == ["sim"]
    assert tokens("bando zimeisha") == ["bundle", "expired"]
    assert "network" in tokens("Hakuna mtandao")
    assert "no" not in tokens("hakuna network")  # negations live in bigrams only


def test_terms_add_negation_aware_bigrams():
    assert "no_network" in terms("Hakuna network huku Kayole")
    assert "no_network" in terms("no signal at all")  # signal -> network
    # Word order is kept: Kiswahili puts the adjective after the noun, which is why the knowledge
    # base lists "namba mbaya" beside "wrong number" -- each phrasing meets its own bigram.
    assert "wrong_number" in terms("sent to the wrong number")
    assert "number_wrong" in terms("nimetuma kwa namba mbaya")


@pytest.mark.parametrize(
    "text,language",
    [
        ("I sent money to the wrong number and I want it back", "en"),
        ("Nimetuma pesa kwa namba mbaya, naomba mnirudishie", "sw"),
        ("bundles zimeisha mapema na sijapata refund", "mixed"),
        ("", "en"),
    ],
)
def test_language_detection(text, language):
    assert detect_language(text) == language


def test_amounts_are_read_in_every_customer_spelling_and_phone_numbers_are_not_amounts():
    assert extract_amounts("I sent KES 1,500 to 0712345678, then 2k, then 300 bob") == [1500, 2000, 300]
    assert extract_amounts("Ksh12000 and 1.5k") == [12000, 1500]
    assert extract_amounts("call me on +254712345678") == []


def test_mpesa_codes_need_letters_and_digits():
    assert extract_mpesa_codes("code SJK4H7QW2L and sjk2p9lm4r") == ["SJK4H7QW2L", "SJK2P9LM4R"]
    assert extract_mpesa_codes("everything 0712345678 ABCDEFGHIJ") == []


@pytest.mark.parametrize("raw", ["071234567８", "０712345678", "07123٤٥678", "+25471234१67８"])
def test_only_ascii_digits_make_a_number(raw):
    """Review finding 3: ``\\d`` matched fullwidth and Arabic-Indic digits, so one number had many
    spellings -- many rate-limit keys, dedupe hashes and repeat counts."""
    with pytest.raises(InvalidMsisdn):
        normalise_msisdn(raw)
