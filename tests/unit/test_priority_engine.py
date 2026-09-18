from noc_agents.config import get_settings, clear_settings_cache
from noc_agents.domain.enums import Priority
from noc_agents.services.priority import evaluate_severity, users_to_priority


def setup_module():
    clear_settings_cache()


def test_safaricom_p4_under_50k():
    cfg = get_settings("safaricom").operator
    assert users_to_priority(1000, cfg) == Priority.P4
    assert users_to_priority(49999, cfg) == Priority.P4


def test_safaricom_p3_under_100k():
    cfg = get_settings("safaricom").operator
    # ≥50k and <100k → P3
    assert users_to_priority(50000, cfg) == Priority.P3
    assert users_to_priority(99999, cfg) == Priority.P3


def test_safaricom_p2_100k_to_500k():
    cfg = get_settings("safaricom").operator
    assert users_to_priority(100000, cfg) == Priority.P2
    assert users_to_priority(499999, cfg) == Priority.P2


def test_safaricom_p1_over_500k():
    cfg = get_settings("safaricom").operator
    assert users_to_priority(500000, cfg) == Priority.P1
    assert users_to_priority(2_000_000, cfg) == Priority.P1


def test_hub_floor_boosts_to_p2():
    cfg = get_settings("safaricom").operator
    res = evaluate_severity(
        users_affected=5000,
        site_type="HUB",
        region_code="MTK",
        cfg=cfg,
    )
    assert res.priority == Priority.P2
    assert res.floor == Priority.P2


def test_nairobi_east_hub_mpesa_and_p2():
    cfg = get_settings("safaricom").operator
    res = evaluate_severity(
        users_affected=450000,
        site_type="HUB",
        region_code="NBI_E",
        cfg=cfg,
    )
    assert res.priority == Priority.P2
    assert res.mpesa_risk is True


def test_core_forces_p1():
    cfg = get_settings("safaricom").operator
    res = evaluate_severity(
        users_affected=100,
        site_type="CORE",
        region_code="NBI_W",
        cfg=cfg,
    )
    assert res.priority == Priority.P1
    assert res.forced_p1 is True


def test_multi_region_forces_p1():
    cfg = get_settings("safaricom").operator
    res = evaluate_severity(
        users_affected=10000,
        site_type="ENODEB",
        region_code="NBI_E",
        multi_region=True,
        cfg=cfg,
    )
    assert res.priority == Priority.P1
