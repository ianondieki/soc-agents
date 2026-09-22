"""External-signal read routes (spec §7.3.2; CONFORMANCE C-15).

Thin transport over ``services/signals.py`` and ``services/backtest.py``: this module parses
query strings, applies RBAC and serialises. Every read is a network-free SELECT, operator-scoped
in its WHERE clause, over rows the out-of-band pollers wrote — the same rule the hot path
lives by, for the same reason: a dead provider must degrade a screen, not a request.

Routes
------
* ``GET /api/v1/signals?source=&region_code=&active=`` — §7.3.2: rows with ``stale``,
  ``valid_until`` and flags, newest first. **Never a bare empty list.** Every response also
  carries ``feeds``: for each source in scope, whether it has ever been read and how fresh the
  newest read is, and for KMD CAP the full feed state, judged against now (``ok | stale_feed |
  incomplete | unreachable | misconfigured | unmapped | not_polled_recently | never_polled``).
  ``?source=KMD_CAP&active=true`` returning ``[]`` beside ``state: not_polled_recently`` cannot
  be misread as "no warnings in force"; returned alone, it would be. GLOFAS is aggregated across
  riverine sites, so one calm site never hides another's flood.
* ``GET /api/v1/signals/county-map`` — the county→region map derived from the operator
  profile, the regions no county reaches, and any county that is not one of Kenya's 47.
* ``GET /api/v1/signals/precision?region_code=&family=`` — the §10.6 30-day verdict with its
  counts, interval and reason: where the strip's "LOW CONFIDENCE" and "precision: not yet
  measured" come from.

``GET /api/v1/signals/weather/regions`` is already served by ``main.py`` and is not redeclared.

Where "reject unknown counties at startup" (§7.3.7) lives, and how loud it is
------------------------------------------------------------------------------
This module is imported by ``api/routers/__init__.py``, which ``main.py`` imports to build the
app. So "at startup", read naively, means "at import of this file" — and a check that raised
here would be an import failure of ``noc_agents.main``: incident ingest, HITL approvals, the
wallboard and every other route down, over a one-word typo in a YAML county list, to protect
a lane the spec itself calls **advisory** ("never changes priority", §7.3). A config check
whose blast radius is larger than the feature it guards is not a safety measure; it is a
second outage mode. So the check is split by *audience*, and each layer is as loud as its
audience can bear:

1. **Here, at import: logged, never raised.** One ``ERROR`` line per unknown county (and one
   ``WARNING`` summarising regions with no counties), naming the region, the county and the
   file to fix. Wrapped so that even a profile that fails to load cannot turn into an import
   error from this module. It reads the profile through ``get_settings.__wrapped__()`` so it
   neither populates nor depends on the settings cache — importing the app must not change
   what a later ``get_settings()`` returns.
2. **At the lane's startup — every CAP poll: rejected.** ``pollers/kmd_cap.py`` validates the
   same map before it fetches anything and, if any county is unknown, **refuses to attribute
   a single warning** and writes ``state: misconfigured`` with the reason onto every region's
   feed-health row. That is the literal "rejects the mapping": it is never *used* while wrong.
   It rejects the whole mapping rather than skipping the bad county because a
   partially-applied map produces one region that silently never receives a warning while the
   other five look healthy — the quiet failure this lane exists to prevent. A loud STALE on
   every tile gets a one-word YAML fix in minutes; a quiet gap in one region gets found after
   the flood.
3. **On the wallboard:** those feed-health rows are what the Regions dashboard's CAP block
   reads, so the refusal is visible where the floor looks every minute — louder in practice
   than any log line in a service nobody tails.
4. **In CI:** ``tests/unit/test_signals_api.py`` validates every shipped profile, so a typo
   cannot reach ``main`` in the first place. That is the strongest "reject" of all and costs
   nothing at runtime.
5. **On demand:** ``GET /api/v1/signals/county-map`` answers ``ok: false`` with the problems.

A region with **no** counties (seven of Airtel's eight today) is a gap, not a typo: reported at
every layer, fatal at none — treating it as fatal would mean this lane can never run on that
profile at all.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from noc_agents.api.auth import require_role
# §9.3 row 1 names "signals read"; legal holds R on it. INCIDENT_READERS, the same tuple as
# main.py's /signals/weather/regions, so the strip and the rows behind it cannot disagree.
from noc_agents.api.deps import INCIDENT_READERS, _settings
from noc_agents.db.models import get_session, utcnow
from noc_agents.services import signals as svc
from noc_agents.services.backtest import FAMILIES, precision_verdict_30d

log = logging.getLogger("noc_agents.api.routers.signals")

router = APIRouter(prefix="/api/v1", tags=["signals"])

#: The ``WEATHER_ENABLED`` flag, read the same way the three pollers read it. Spelled here
#: rather than imported from a poller: importing ``pollers/*`` pulls ``httpx`` into a module
#: whose whole job is network-free reads.
_TRUE = {"1", "true", "yes", "on"}


def _weather_enabled() -> bool:
    return (os.getenv("WEATHER_ENABLED") or "").strip().lower() in _TRUE


# ------------------------------------------------------------------ layer 1: import-time check


def _check_county_map_at_import() -> list[dict[str, Any]]:
    """Validate the active profile's counties and LOG the result. Never raises (see docstring)."""
    try:
        from noc_agents.config import get_settings

        # __wrapped__ is the uncached function lru_cache wraps: same resolution rules, no
        # cache side effect, so importing the app cannot pin a profile for later callers.
        cfg = get_settings.__wrapped__().operator
        problems = svc.validate_county_map(cfg)
    except Exception as exc:  # noqa: BLE001 — a config problem must never become an import error
        log.error("signals: the county->region check could not run at import (%s: %s); "
                  "the KMD CAP poller will re-check before it attributes anything", type(exc).__name__, exc)
        return [{"kind": "check_failed", "message": f"{type(exc).__name__}: {exc}", "fatal": False}]
    fatal = [p for p in problems if p.fatal]
    gaps = [p.region_code for p in problems if not p.fatal]
    for p in fatal:
        log.error("signals: COUNTY MAP REJECTED for operator %s: %s. The KMD CAP poller will refuse to "
                  "attribute any warning until this is fixed.", cfg.operator_id, p.message)
    if gaps:
        log.warning("signals: operator %s regions with no counties (no KMD CAP warning can reach them): %s",
                    cfg.operator_id, ", ".join(gaps))
    return [p.as_dict() for p in problems]


#: What the import-time check found, kept for ``GET /signals/county-map`` so the answer to
#: "did this process start with a bad map?" does not depend on somebody having read the log.
STARTUP_COUNTY_PROBLEMS: list[dict[str, Any]] = _check_county_map_at_import()


# ------------------------------------------------------------------ helpers


def _operator_id() -> str:
    return _settings().operator.operator_id


def _regions() -> list[str]:
    return sorted(getattr(_settings().operator, "regions", {}) or {})


def _source_status(session, operator_id: str, source: str, region_code: str | None, now) -> dict[str, Any]:
    """Whether a source has ever been read for this scope, and how fresh the newest read is."""
    if source == svc.CAP_SOURCE:
        return svc.cap_feed_health(session, operator_id, region_code, now)
    if source == svc.FLOOD_SOURCE:
        # Per-site rows share the run's timestamp, so "the newest row" is a tie-break; judge
        # every site's current reading instead (review finding F09).
        scopes = [region_code] if region_code is not None else _regions()
        states = [svc.flood_region_state(session, operator_id, code, now) for code in scopes]
        seen = [st for st in states if st["available"]]
        if not seen:
            return {
                "state": "never_polled", "available": False, "stale": True, "fetched_at": None,
                "last_error": None, "flag": None,
                "reason": "no GLOFAS reading has ever been stored for this scope; an empty list here means "
                "nothing is known, not that nothing is happening",
            }
        fresh = [st for st in seen if not st["stale"]]
        errors = [site["last_error"] for st in seen for site in st["sites"] if site["last_error"]]
        return {
            "state": "ok" if fresh else "stale",
            "available": True,
            "stale": not fresh,
            "fetched_at": max((st["fetched_at"] for st in seen if st["fetched_at"]), default=None),
            "last_error": errors[0] if errors else None,
            "flag": any(st["flag"] for st in fresh) if fresh else None,
            "reason": None if fresh else (errors[0] if errors else "no riverine site has a current reading"),
        }
    row = svc.latest_row(session, operator_id, source=source, region_code=region_code)
    if row is None:
        return {
            "state": "never_polled",
            "available": False,
            "stale": True,
            "fetched_at": None,
            "last_error": None,
            "reason": f"no {source} row has ever been stored for this scope; an empty list here means "
            "nothing is known, not that nothing is happening",
        }
    status = svc.staleness(row, now)
    return {
        "state": "stale" if status["stale"] else "ok",
        "available": True,
        "stale": status["stale"],
        "fetched_at": status["fetched_at"],
        "last_error": status["last_error"],
        "reason": status["last_error"] if status["stale"] else None,
    }


# ------------------------------------------------------------------ routes


@router.get("/signals", dependencies=[Depends(require_role(*INCIDENT_READERS))])
def list_external_signals(
    source: str | None = Query(None, description="OPEN_METEO | MET_NORWAY | KMD_CAP | GLOFAS | KPLC | COMPLAINTS"),
    region_code: str | None = Query(None),
    active: bool | None = Query(None, description="true: still in force (valid_until > now); false: expired"),
    site_id: str | None = Query(None),
    limit: int = Query(200, ge=1, le=1000),
) -> dict[str, Any]:
    """§7.3.2 ``GET /api/v1/signals?source=&region_code=&active=`` — rows plus the state of
    every feed in scope, so an empty list always travels with the reason it is empty."""
    if source is not None:
        source = source.strip().upper()
        if source not in svc.SIGNAL_SOURCES:
            # 422, not []: a misspelt filter that answered "nothing" would read as "no signals".
            raise HTTPException(422, f"unknown source {source!r}; one of {', '.join(svc.SIGNAL_SOURCES)}")
    regions = _regions()
    if region_code is not None:
        region_code = region_code.strip().upper()
        if region_code not in regions:
            raise HTTPException(422, f"unknown region_code {region_code!r} for this operator; one of {', '.join(regions)}")
    operator_id = _operator_id()
    now = utcnow()
    session = get_session()
    try:
        rows = svc.list_signals(
            session, operator_id, source=source, region_code=region_code, active=active,
            site_id=site_id, now=now, limit=limit,
        )
        in_scope = (source,) if source else svc.SIGNAL_SOURCES
        feeds = {s: _source_status(session, operator_id, s, region_code, now) for s in in_scope}
        return {
            "generated_at": svc.iso_z(now),
            "weather_enabled": _weather_enabled(),
            "filters": {"source": source, "region_code": region_code, "active": active, "site_id": site_id},
            "count": len(rows),
            "signals": [svc.signal_out(r, now) for r in rows],
            "feeds": feeds,
        }
    finally:
        session.close()


@router.get("/signals/county-map", dependencies=[Depends(require_role(*INCIDENT_READERS))])
def county_map() -> dict[str, Any]:
    """The county→region reverse map, derived from the operator profile, with its problems."""
    report = svc.county_map_report(_settings().operator)
    report["operator_id"] = _operator_id()
    report["startup_problems"] = STARTUP_COUNTY_PROBLEMS
    return report


@router.get("/signals/precision", dependencies=[Depends(require_role(*INCIDENT_READERS))])
def signal_precision(
    region_code: str | None = Query(None, description="one region; default every region of the operator"),
    family: str = Query("storm", description="storm | cap | flood"),
) -> dict[str, Any]:
    """§10.6: the 30-day precision verdict per region, with the counts and the reason.

    ``precision`` is ``null`` under ``INSUFFICIENT_DATA`` — the house rule (``services/capacity``)
    is that below the data floor no number is published at all, because a number gets quoted.
    """
    family = family.strip().lower()
    if family not in FAMILIES:
        raise HTTPException(422, f"unknown family {family!r}; one of {', '.join(sorted(FAMILIES))}")
    regions = _regions()
    if region_code is not None:
        region_code = region_code.strip().upper()
        if region_code not in regions:
            raise HTTPException(422, f"unknown region_code {region_code!r} for this operator; one of {', '.join(regions)}")
        regions = [region_code]
    operator_id = _operator_id()
    now = utcnow()
    session = get_session()
    try:
        return {
            "generated_at": svc.iso_z(now),
            "family": family,
            "window_days": 30,
            "regions": {code: precision_verdict_30d(session, operator_id, code, family=family, now=now) for code in regions},
        }
    finally:
        session.close()
