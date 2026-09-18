"""Stage C5: BER / FO are matched as alarm-code tokens, every other rule stays a substring rule."""

from __future__ import annotations

import pytest

from noc_agents.services.assignment import alarm_tokens, domain_lane
from noc_agents.services.tt_classify import _match_category


def test_alarm_tokens_split_on_non_alphanumerics():
    assert alarm_tokens("TX_FIBER_CUT") == {"TX", "FIBER", "CUT"}
    assert alarm_tokens("mw-link.ber high") == {"MW", "LINK", "BER", "HIGH"}
    assert alarm_tokens("") == set()


@pytest.mark.parametrize(
    ("alarm", "domain", "site_type", "lane"),
    [
        ("TX_FIBER_CUT", "TRANSMISSION", "HUB", "tx_fiber"),  # was tx_mw: BER inside FIBER
        ("CELL_FORCED_OFF", "RADIO", "ENODEB", "radio"),  # was tx_fiber: FO inside FORCED
        ("RADIO_VSWR_FORWARD", "RADIO", "BTS", "radio"),  # was tx_fiber: FO inside FORWARD
        ("NUMBER_MISMATCH", "RADIO", "BTS", "radio"),  # was tx_mw: BER inside NUMBER
        ("MW_LINK_BER_HIGH", "TRANSMISSION", "TX", "tx_mw"),
        ("MW_HIGH_BER", "TRANSMISSION", "HUB", "tx_mw"),
        ("MWLINKDOWN", "TRANSMISSION", "TX", "tx_mw"),  # MW stays a substring rule
        ("FO_LOSS", "TRANSMISSION", "HUB", "tx_fiber"),
        ("TX_FIBRE_CUT", "TRANSMISSION", "HUB", "tx_fiber"),
        ("POWER_GRID_FAIL", "POWER", "HUB", "power"),
        ("HVAC_FAIL", "ENVIRONMENT", "BTS", "power"),
    ],
)
def test_domain_lane(alarm, domain, site_type, lane):
    assert domain_lane(domain, site_type, alarm) == lane


@pytest.mark.parametrize(
    ("alarm", "domain", "category"),
    [
        ("TX_FIBER_CUT", "TRANSMISSION", "TX_FIBRE"),
        ("CELL_FORCED_OFF", "RADIO", "RADIO_CELL"),
        ("RADIO_VSWR_FORWARD", "RADIO", "RADIO_VSWR"),
        ("NUMBER_MISMATCH", "CORE", "CORE_NODE"),  # was TX_MW
        ("MW_LINK_BER_HIGH", "TRANSMISSION", "TX_MW"),
        ("MWLINKDOWN", "TRANSMISSION", "TX_MW"),
        ("FO_LOSS", "TRANSMISSION", "TX_FIBRE"),
        ("LOSS_FO", "TRANSMISSION", "TX_FIBRE"),  # FO_ -> FO token: the one deliberately broader rule
        ("HVAC_FAIL", "ENVIRONMENT", "ENV_TEMP"),  # AC stays a substring rule
        ("ACDB_TRIP", "ENVIRONMENT", "ENV_TEMP"),
    ],
)
def test_tt_category(alarm, domain, category):
    assert _match_category(alarm, domain) == category
