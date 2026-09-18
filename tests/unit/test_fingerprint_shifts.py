from datetime import datetime
from zoneinfo import ZoneInfo

from noc_agents.config import get_settings, clear_settings_cache
from noc_agents.services.fingerprint import build_fingerprint
from noc_agents.services.shifts import current_shift


def test_fingerprint_stable():
    a = build_fingerprint("sfc-nbi-hub-wld", "power_grid_fail", "power")
    b = build_fingerprint("SFC-NBI-HUB-WLD", "POWER_GRID_FAIL", "POWER")
    assert a == b


def test_day_shift_eat():
    clear_settings_cache()
    cfg = get_settings("safaricom").operator
    noon = datetime(2026, 7, 16, 12, 0, tzinfo=ZoneInfo("Africa/Nairobi"))
    assert current_shift(cfg, noon) == "day"
    night = datetime(2026, 7, 16, 22, 0, tzinfo=ZoneInfo("Africa/Nairobi"))
    assert current_shift(cfg, night) == "night"
