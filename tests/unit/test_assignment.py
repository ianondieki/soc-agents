from noc_agents.config import get_settings, clear_settings_cache
from noc_agents.domain.enums import AssigneeType
from noc_agents.services.assignment import assign, failure_domain_to_matrix_key, domain_lane


def test_nairobi_east_power_egypro():
    clear_settings_cache()
    cfg = get_settings("safaricom").operator
    r = assign(failure_domain="POWER", site_type="HUB", region_code="NBI_E", cfg=cfg, alarm_code="POWER_GRID_FAIL")
    assert r.msp_name == "EGYPRO"
    assert r.assignee_type == AssigneeType.MSP
    assert r.fe_name.startswith("FE-NBI-E")


def test_mt_kenya_power_egypro_tx_soliton():
    clear_settings_cache()
    cfg = get_settings("safaricom").operator
    p = assign(failure_domain="POWER", site_type="HUB", region_code="MTK", cfg=cfg)
    assert p.msp_name == "EGYPRO"
    t = assign(failure_domain="TRANSMISSION", site_type="TX", region_code="MTK", cfg=cfg, alarm_code="TX_FIBRE_CUT")
    assert t.msp_name in ("SOLITON", "EGYPRO_FIBRE")


def test_nairobi_west_radio_huawei():
    clear_settings_cache()
    cfg = get_settings("safaricom").operator
    r = assign(failure_domain="RADIO", site_type="ENODEB", region_code="NBI_W", cfg=cfg)
    assert r.msp_name == "HUAWEI_RADIO"
    assert r.radio_oem == "HUAWEI"


def test_coast_radio_huawei():
    clear_settings_cache()
    cfg = get_settings("safaricom").operator
    r = assign(failure_domain="RADIO", site_type="ENODEB", region_code="CST", cfg=cfg)
    assert r.msp_name == "HUAWEI_RADIO"
    assert r.radio_oem == "HUAWEI"


def test_rift_power_tetranet():
    clear_settings_cache()
    cfg = get_settings("safaricom").operator
    r = assign(failure_domain="POWER", site_type="HUB", region_code="RFT", cfg=cfg)
    assert r.msp_name == "TETRANET"


def test_western_nyanza_power_tetranet():
    clear_settings_cache()
    cfg = get_settings("safaricom").operator
    r = assign(failure_domain="POWER", site_type="HUB", region_code="WNY", cfg=cfg)
    assert r.msp_name == "TETRANET"
    assert r.radio_oem == "MIXED"


def test_domain_keys():
    assert failure_domain_to_matrix_key("POWER", "HUB") == "power_passive"
    assert failure_domain_to_matrix_key("CORE", "CORE") == "core"
    assert domain_lane("TRANSMISSION", "HUB", "TX_FIBRE_CUT") == "tx_fiber"
