"""Timezone helper — spec §7.0.6, defect #41.

Kenya runs on EAT (UTC+3, no DST). The database keeps **naive UTC** datetimes and
that is an existing contract this module does not touch: nothing here writes, and
``utcnow()`` returns exactly what ``db.models.utcnow()`` returns so it is a drop-in
for storage. Conversion happens on the way *out* only:

    row.created_at            # naive UTC, as stored
    fmt_eat(row.created_at)   # "12:00 EAT", what a Nairobi operator reads

``z_utc()`` is the serializer side of the same defect: a naive ISO string such as
``"2026-09-16T09:00:00"`` is read by a browser as *local* time, so every timestamp
leaving the API is stamped with an explicit ``Z``.

The zone name comes from the operator profile (``config.OperatorConfig.timezone``),
falling back to the app-level setting and then to ``Africa/Nairobi``. ``tzdata`` is a
declared dependency, so ``ZoneInfo`` resolves on Windows as well as Linux.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from noc_agents.config import get_settings

__all__ = [
    "DEFAULT_TIMEZONE",
    "UTC",
    "eat_date",
    "eat_tz",
    "fmt_eat",
    "to_eat",
    "to_utc",
    "utcnow",
    "z_utc",
]

DEFAULT_TIMEZONE = "Africa/Nairobi"
UTC = timezone.utc


# --------------------------------------------------------------------------- zone


def _tz_name() -> str:
    """Configured operator timezone, with the profile as the source of truth."""
    try:
        settings = get_settings()
    except Exception:  # config unreadable (bad profile, partial test env) — never crash a render
        return DEFAULT_TIMEZONE
    operator = getattr(settings, "operator", None)
    return getattr(operator, "timezone", None) or getattr(settings, "timezone", None) or DEFAULT_TIMEZONE


@lru_cache(maxsize=8)
def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return ZoneInfo(DEFAULT_TIMEZONE)


def eat_tz() -> ZoneInfo:
    """The operator's display zone. Named for its only real-world value (EAT, UTC+3)."""
    # Resolved per call, not cached on the module, so clear_settings_cache() is honoured.
    return _zone(_tz_name())


# --------------------------------------------------------------------- conversion


def utcnow() -> datetime:
    """Naive UTC 'now' — the storage contract. Identical to ``db.models.utcnow()``."""
    return datetime.now(UTC).replace(tzinfo=None)


def to_utc(dt: datetime | None) -> datetime | None:
    """Aware UTC. A naive input is assumed to be UTC, which is the DB contract."""
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def to_eat(dt: datetime | None) -> datetime | None:
    """Aware EAT. ``to_eat(datetime(2026, 9, 16, 9, 0))`` -> 12:00 +03:00."""
    if dt is None:
        return None
    return to_utc(dt).astimezone(eat_tz())


def fmt_eat(dt: datetime | None, fmt: str = "%H:%M") -> str:
    """Render for a human in Nairobi: ``"12:00 EAT"``. Empty string for a missing time."""
    if dt is None:
        return ""
    return f"{to_eat(dt).strftime(fmt)} EAT"


def eat_date(dt: datetime | None) -> date | None:
    """Calendar date *in EAT* — 22:00 UTC is already tomorrow in Nairobi."""
    if dt is None:
        return None
    return to_eat(dt).date()


# --------------------------------------------------------------------- serializing


class _UtcZ(datetime):
    """A UTC datetime that renders with a literal ``Z``.

    ``IncidentOut.created_at`` and friends are typed ``datetime``, and the routes dump
    in python mode, so the value FastAPI finally encodes is a datetime object and the
    suffix has to travel *with* it. ``jsonable_encoder`` reaches every datetime through
    ``.isoformat()``, so overriding that one method is enough to turn ``+00:00`` into
    ``Z`` without changing a schema or a route.

    It is a real ``datetime`` in every other respect: comparisons, arithmetic and
    pydantic validation all behave normally. If a future pydantic ever rebuilt the
    value as a plain ``datetime``, the output degrades to ``"…+00:00"`` — a different
    spelling of the same instant, still unambiguously UTC, never a naive string.
    """

    __slots__ = ()

    def isoformat(self, *args, **kwargs) -> str:  # type: ignore[override]
        return super().isoformat(*args, **kwargs).replace("+00:00", "Z")


def z_utc(dt: datetime | None) -> datetime | None:
    """Tag a stored (naive UTC) timestamp so it serializes as ``2026-09-16T09:00:00Z``.

    Use on every datetime a serializer hands to the API. ``None`` passes through.
    """
    if dt is None:
        return None
    aware = to_utc(dt)
    return _UtcZ(
        aware.year,
        aware.month,
        aware.day,
        aware.hour,
        aware.minute,
        aware.second,
        aware.microsecond,
        tzinfo=UTC,
        fold=aware.fold,
    )
