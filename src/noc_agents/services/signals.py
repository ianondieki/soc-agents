"""Cached external-signal READS — the half of the weather lane the hot path is allowed to touch.

Guardrail G4 (spec §2): *nothing on the ``POST /api/v1/events`` hot path does I/O*, enforced in
two layers — a runtime test that blocks every socket during ``process_event``, and a grep rule
that ``agents/*.py`` and ``orchestrator/runner.py`` import "neither ``httpx`` nor ``anthropic``
nor ``mcp`` nor any poller/adapter module".

These five functions used to live in ``pollers/weather.py``, beside the code that fetches
forecasts. That module imports ``adapters/weather.py``, which imports ``httpx`` at module level.
So ENRICH's cache read — a single indexed ``SELECT ... LIMIT 1`` that performs no I/O at all —
nevertheless dragged the whole network stack into the process from the middle of
``run_incident_lifecycle`` the first time ``WEATHER_ENABLED`` was on. The runtime layer of G4
held (nothing was ever sent), but the grep layer did not, and the import was function-local,
which is precisely the case the spec warns "defeats a grep". Nothing caught it because the
grep-layer test did not exist yet; ``tests/unit/test_hot_path_imports.py`` is that test, and it
found this on its first run.

The split is along the line the spec already draws: *"Hot-path agents may only read rows written
by out-of-band jobs."* Out-of-band jobs (``pollers/``) write ``external_signals``; this module
reads it. Keep it that way — **this module must never import an adapter, a poller, ``httpx``, or
anything else that can open a connection**, and the test above asserts, in a fresh interpreter,
that importing it leaves ``httpx`` out of ``sys.modules``.

``pollers/weather.py`` re-exports every name here, so ``from noc_agents.pollers.weather import
weather_risk_for_region`` still works for the out-of-band callers that were never the problem
(the Wallboard route, the Regions dashboard, the maintenance rain guard).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from noc_agents.db.models import ExternalSignalRow, utcnow

#: The ``external_signals.source`` values that are weather forecasts. Spelled as literals rather
#: than imported from ``adapters/weather.py`` (which owns ``OPEN_METEO`` / ``MET_NORWAY``) because
#: importing that module is the very thing this one exists to avoid.
WEATHER_SOURCES: tuple[str, ...] = ("OPEN_METEO", "MET_NORWAY")


# ---------------------------------------------------------------------------- staleness


def is_stale(row: ExternalSignalRow, now: datetime | None = None) -> bool:
    """Stored flag OR past ``valid_until`` — computed against *now*, never trusted from disk alone."""
    now = now or utcnow()
    return bool(row.stale) or row.valid_until is None or now >= row.valid_until


def staleness(row: ExternalSignalRow, now: datetime | None = None) -> dict[str, Any]:
    """What a badge needs: ``stale``, ``age_s`` since the fetch, and the window bounds."""
    now = now or utcnow()
    age_s = max(0, int((now - row.fetched_at).total_seconds())) if row.fetched_at else None
    return {
        "stale": is_stale(row, now),
        "age_s": age_s,
        "fetched_at": row.fetched_at.replace(microsecond=0).isoformat() + "Z" if row.fetched_at else None,
        "valid_until": row.valid_until.replace(microsecond=0).isoformat() + "Z" if row.valid_until else None,
        "last_error": row.last_error,
    }


# ---------------------------------------------------------------------------- cache reads


def latest_signal(
    session: Session,
    operator_id: str,
    region_code: str,
    *,
    source: str | None = None,
) -> ExternalSignalRow | None:
    """The newest *good* weather row for a region (has a derived block), operator-scoped.

    Pure database read: this is what ENRICH and the Wallboard call, and it works with the
    network down. ``source`` narrows to one provider; by default any weather source counts.
    """
    stmt = (
        select(ExternalSignalRow)
        .where(
            ExternalSignalRow.operator_id == operator_id,
            ExternalSignalRow.region_code == region_code,
            ExternalSignalRow.derived_json.is_not(None),
        )
        .order_by(ExternalSignalRow.fetched_at.desc(), ExternalSignalRow.created_at.desc())
        .limit(1)
    )
    if source is not None:
        stmt = stmt.where(ExternalSignalRow.source == source)
    else:
        stmt = stmt.where(ExternalSignalRow.source.in_(WEATHER_SOURCES))
    return session.scalars(stmt).first()


def latest_error(session: Session, operator_id: str, region_code: str) -> ExternalSignalRow | None:
    """The newest weather row for the region that carries a ``last_error`` (good or marker)."""
    stmt = (
        select(ExternalSignalRow)
        .where(
            ExternalSignalRow.operator_id == operator_id,
            ExternalSignalRow.region_code == region_code,
            ExternalSignalRow.source.in_(WEATHER_SOURCES),
            ExternalSignalRow.last_error.is_not(None),
        )
        .order_by(ExternalSignalRow.created_at.desc())
        .limit(1)
    )
    return session.scalars(stmt).first()


def weather_risk_for_region(
    session: Session,
    operator_id: str,
    region_code: str,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """The stored ``weather_risk`` block with ``stale`` recomputed, or ``None`` when the region
    has never been fetched successfully. The ``noc_get_weather_risk`` cache read of §5.3.3."""
    now = now or utcnow()
    row = latest_signal(session, operator_id, region_code)
    if row is None:
        return None
    try:
        block = json.loads(row.derived_json or "{}")
    except ValueError:
        block = {}
    if not isinstance(block, dict):
        block = {}
    block.update(staleness(row, now))
    block["region_code"] = region_code
    block["source"] = row.source
    return block
