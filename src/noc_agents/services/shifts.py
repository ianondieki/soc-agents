from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from noc_agents.config import OperatorConfig


def current_shift(cfg: OperatorConfig, now: datetime | None = None) -> str:
    tz = ZoneInfo(cfg.timezone)
    now = now or datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)
    minutes = now.hour * 60 + now.minute
    # day 08:00-20:00
    day = cfg.shifts.get("day")
    if day:
        sh, sm = map(int, day.start.split(":"))
        eh, em = map(int, day.end.split(":"))
        start_m = sh * 60 + sm
        end_m = eh * 60 + em
        if start_m <= minutes < end_m:
            return "day"
    return "night"


def shift_id(cfg: OperatorConfig, now: datetime | None = None) -> str:
    tz = ZoneInfo(cfg.timezone)
    now = now or datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    else:
        now = now.astimezone(tz)
    st = current_shift(cfg, now)
    return f"{cfg.operator_id}:{now.strftime('%Y-%m-%d')}:{st.upper()}"
