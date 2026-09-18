from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session

from noc_agents.db.models import SequenceRow


def _next_sequence_value(session: Session, key: str) -> int:
    bind = session.get_bind()
    dialect = bind.dialect.name if bind is not None else ""

    if dialect == "sqlite":
        try:
            session.execute(text("BEGIN IMMEDIATE"))
        except Exception:
            pass

    row = session.get(SequenceRow, key)
    if row is None:
        row = SequenceRow(id=key, last_value=0)
        session.add(row)
        session.flush()

    if dialect == "sqlite":
        session.execute(
            text("UPDATE daily_sequences SET last_value = last_value + 1 WHERE id = :id"),
            {"id": key},
        )
        session.flush()
        val = session.execute(
            text("SELECT last_value FROM daily_sequences WHERE id = :id"),
            {"id": key},
        ).scalar_one()
        return int(val)

    row.last_value += 1
    session.flush()
    return int(row.last_value)


def next_incident_number(
    session: Session,
    prefix: str,
    tz_name: str = "Africa/Nairobi",
    numbering_style: str = "inc9",
) -> str:
    """Allocate next incident number.

    Safaricom floor style (inc9): exactly 9 characters starting with INC,
    e.g. INC000001 … INC999999.
    """
    style = (numbering_style or "inc9").lower()
    if style == "inc9" or prefix.upper() == "INC":
        key = "INC:global"
        val = _next_sequence_value(session, key)
        # INC + 6 digits = 9 characters
        return f"INC{val:06d}"

    now = datetime.now(ZoneInfo(tz_name))
    day = now.strftime("%Y%m%d")
    key = f"{prefix}:{day}"
    val = _next_sequence_value(session, key)
    return f"{prefix}-{day}-{val:05d}"


def next_problem_number(
    session: Session,
    prefix: str,
    tz_name: str = "Africa/Nairobi",
    numbering_style: str = "inc9",
) -> str:
    style = (numbering_style or "inc9").lower()
    if style == "inc9" or prefix.upper() in ("INC", "PRB"):
        key = "PRB:global"
        val = _next_sequence_value(session, key)
        return f"PRB{val:06d}"
    return next_incident_number(session, prefix, tz_name, numbering_style="dated")
