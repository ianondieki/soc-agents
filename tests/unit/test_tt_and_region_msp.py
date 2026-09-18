from noc_agents.config import clear_settings_cache, get_settings
from noc_agents.services.assignment import assign
from noc_agents.services.tt_classify import classify_tt
from noc_agents.services.numbering import next_incident_number
from noc_agents.db.models import init_db, get_session


def test_nbi_e_power_msp_is_egypro():
    clear_settings_cache()
    cfg = get_settings("safaricom").operator
    r = assign(failure_domain="POWER", site_type="HUB", region_code="NBI_E", cfg=cfg, alarm_code="POWER_GRID_FAIL")
    assert r.msp_name == "EGYPRO"


def test_coast_power_not_huawei_radio():
    """Coast radio is Huawei; power uses passive pool (ATC primary)."""
    clear_settings_cache()
    cfg = get_settings("safaricom").operator
    r = assign(failure_domain="POWER", site_type="HUB", region_code="CST", cfg=cfg, alarm_code="POWER_GRID_FAIL")
    assert r.msp_name in ("ATC", "CAMUSAT", "EGYPRO")
    assert r.msp_name != "HUAWEI_RADIO"


def test_tt_classify_genset():
    clear_settings_cache()
    cfg = get_settings("safaricom").operator
    tt = classify_tt(
        alarm_code="GENSET_FAIL",
        failure_domain="POWER",
        site_type="HUB",
        technology=["4G", "2G"],
        cfg=cfg,
    )
    assert tt.tt_category == "POWER_GENSET"
    assert tt.site_class == "CRITICAL"


def test_tt_classify_fibre():
    clear_settings_cache()
    cfg = get_settings("safaricom").operator
    tt = classify_tt(
        alarm_code="TX_FIBRE_CUT",
        failure_domain="TRANSMISSION",
        site_type="HUB",
        technology=["MW", "FO"],
        cfg=cfg,
    )
    assert tt.tt_category == "TX_FIBRE"


def test_inc_number_is_nine_chars(tmp_path, monkeypatch):
    db = tmp_path / "num.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db.as_posix()}")
    clear_settings_cache()
    init_db(f"sqlite:///{db.as_posix()}")
    session = get_session()
    n1 = next_incident_number(session, "INC", numbering_style="inc9")
    n2 = next_incident_number(session, "INC", numbering_style="inc9")
    session.commit()
    assert n1 == "INC000001"
    assert n2 == "INC000002"
    assert len(n1) == 9
    session.close()
