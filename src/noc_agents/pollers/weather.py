"""``weather_regions``: the WeatherRiskAgent's 15-minute poll (spec §5.3.13, §7.3.3).

Why this exists, operationally: Kenyan storms take out power and transmission. A NOC that
knows heavy rain is due over the Rift Valley in three hours can pre-position a generator and
warn the MSP instead of learning about it at 2 a.m. from forty simultaneous alarms. That is
the whole value: **early warning, advisory only, never an automated action**. Nothing here
changes a priority, sends a message or touches an incident; it fills a cache that ENRICH and
the Wallboard read later, and only when ``WEATHER_ENABLED=true``.

What one run does
-----------------
For each region centroid (the mean of the catalogued site coordinates in that region — the
seed's coordinates are 2-dp town/suburb centroids, so the mean is a ~10 km grid point, which
is exactly the resolution any forecast model offers over Kenya) it calls the configured
:class:`~noc_agents.adapters.weather.WeatherProvider`, derives the ``weather_risk`` block and
upserts one ``external_signals`` row keyed by ``<region>:<15-minute bucket>``. Six regions ×
96 polls/day = 576 Open-Meteo calls, 5.8 % of the free tier.

Fail-soft, region by region
---------------------------
A timeout, a 500, malformed JSON, a TLS failure or a misconfigured provider is caught per
region and never propagates: the failure is logged, written to ``last_error`` on the region's
last good row (which keeps its payload), the row is marked ``stale=1`` once ``valid_until`` has
passed, and the next region is tried. A region with no good row at all gets a zero-confidence
*marker* row so the strip can show STALE *with the reason* instead of a blank. The job returns
a :class:`JobResult` whose tools list says per region what happened, so ``/runs`` shows the
truth without the scheduler's circuit ever opening on a dead provider (that is deliberate: a
transient outage must not need a manual circuit reset to recover).

Gating
------
``WEATHER_ENABLED`` defaults to **false** here, regardless of the scheduler's own per-job
default (``job_enabled`` treats an unset flag as on), so wiring :data:`WEATHER_JOB` into
``SCHEDULED_JOBS`` can never start network calls on a machine that did not opt in. Off means
no request, no row, one skipped step.

Reads
-----
:func:`latest_signal` / :func:`weather_risk_for_region` are the cache reads ENRICH and the
Wallboard use (``noc_get_weather_risk`` in §5.3.3): pure database, operator-scoped, with
``stale`` recomputed from ``fetched_at`` / ``valid_until`` against *now*, so a poller that
died an hour ago cannot leave a row looking fresh.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from noc_agents.adapters.weather import (
    ForecastSnapshot,
    WeatherError,
    WeatherProvider,
    WeatherThresholds,
    derive_weather_risk,
    provider_from_env,
    snapshot_payload_json,
)
from noc_agents.config import AppSettings
from noc_agents.db.models import ExternalSignalRow, new_id, utcnow
from noc_agents.realtime.hub import RealtimeEvent, hub
from noc_agents.scheduler import JobCard, JobResult
from noc_agents.services.sites import SiteRecord, all_sites

log = logging.getLogger("noc_agents.pollers.weather")

__all__ = [
    "AGENT",
    "GRAPH_NAME",
    "INTERVAL_S",
    "JOB_NAME",
    "ROW_VALID_FOR",
    "WEATHER_JOB",
    "RegionOutcome",
    "is_stale",
    "latest_error",
    "latest_signal",
    "poll",
    "poll_region",
    "record_failure",
    "region_centroids",
    "staleness",
    "upsert_signal",
    "weather_enabled",
    "weather_risk_for_region",
]

JOB_NAME = "weather_regions"
INTERVAL_S = 900  # 15 min (§5.3.13)
AGENT = "WeatherRiskAgent"
GRAPH_NAME = "weather"  # agent_runs.graph_name, as "outbox" / "monitor" do
ENABLED_ENV = "WEATHER_ENABLED"

#: How long a fetched row stays trustworthy. Four missed 15-minute polls → STALE. This is an
#: **UNVERIFIED operational starting value** chosen so a dead poller is visible within the hour;
#: the *risk horizon* (6 h, ``WeatherThresholds.horizon_hours``) is a different thing and lives
#: in the derived block. Owner decision D14 covers tuning both.
ROW_VALID_FOR = timedelta(hours=1)

_TRUE = {"1", "true", "yes", "on"}
_MARKER_SUFFIX = ":unavailable"


def weather_enabled() -> bool:
    """``WEATHER_ENABLED`` — default **false**. Only an explicit true value polls."""
    return (os.getenv(ENABLED_ENV) or "").strip().lower() in _TRUE


# ---------------------------------------------------------------------------- geography


def region_centroids(sites: Iterable[SiteRecord] | None = None) -> dict[str, tuple[float, float]]:
    """``{region_code: (lat, lon)}`` — the mean of the catalogued coordinates per region.

    Sourced from the site seed as §5.3.13 says ("region centroids ... from the site seed"),
    not from a hand-typed table: when the floor's site register replaces the demo seed, the
    centroids follow. Regions without a single coordinate are omitted (nothing to forecast).
    Rounded to 2 dp to match the seed's own honesty about precision.
    """
    sums: dict[str, list[float]] = {}
    for site in sites if sites is not None else all_sites():
        if not site.has_coordinates or not site.region_code:
            continue
        acc = sums.setdefault(site.region_code.upper(), [0.0, 0.0, 0.0])
        acc[0] += float(site.lat)  # type: ignore[arg-type]
        acc[1] += float(site.lon)  # type: ignore[arg-type]
        acc[2] += 1
    return {
        code: (round(lat / n, 2), round(lon / n, 2))
        for code, (lat, lon, n) in sorted(sums.items())
        if n
    }


# ---------------------------------------------------------------------------- time helpers


def _bucket(now: datetime, interval_s: int = INTERVAL_S) -> datetime:
    """Floor ``now`` to the poll cadence, so a re-run in the same slot upserts the same row."""
    seconds = int((now - now.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds())
    floored = seconds - (seconds % interval_s)
    return now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(seconds=floored)


def _external_id(region_code: str, bucket: datetime) -> str:
    return f"{region_code}:{bucket.strftime('%Y-%m-%dT%H:%MZ')}"


# The cache READS (is_stale, staleness, latest_signal, latest_error, weather_risk_for_region)
# live in services/signals.py, not here. This module imports adapters/weather.py, which imports
# httpx; ENRICH reads the cache from inside run_incident_lifecycle, and guardrail G4 forbids a
# hot-path agent from importing a poller or adapter module at all. They are re-exported below so
# every out-of-band caller (`from noc_agents.pollers.weather import weather_risk_for_region`)
# is unchanged. See services/signals.py for the full reasoning.
from noc_agents.services.signals import (  # noqa: E402,F401  (re-export; see comment above)
    is_stale,
    latest_error,
    latest_signal,
    staleness,
    weather_risk_for_region,
)


# ---------------------------------------------------------------------------- writes


def upsert_signal(
    session: Session,
    *,
    operator_id: str,
    region_code: str,
    snapshot: ForecastSnapshot,
    derived: Mapping[str, Any],
    now: datetime,
    valid_for: timedelta = ROW_VALID_FOR,
) -> ExternalSignalRow:
    """Insert or replace the row for ``(operator, source, <region>:<bucket>)``. Does not commit."""
    bucket = _bucket(now)
    external_id = _external_id(region_code, bucket)
    row = session.scalar(
        select(ExternalSignalRow).where(
            ExternalSignalRow.operator_id == operator_id,
            ExternalSignalRow.source == snapshot.source,
            ExternalSignalRow.external_id == external_id,
        )
    )
    if row is None:
        row = ExternalSignalRow(id=new_id(), operator_id=operator_id, source=snapshot.source, external_id=external_id, created_at=now)
        session.add(row)
    row.source_url = snapshot.source_url
    row.region_code = region_code
    row.county = None  # region-level forecast; county rows come from the CAP poller
    row.site_id = None
    row.fetched_at = snapshot.fetched_at
    row.valid_from = snapshot.fetched_at
    row.valid_until = snapshot.fetched_at + valid_for
    row.stale = 0
    row.confidence = 1.0
    row.storm_flag = 1 if derived.get("storm_flag") else 0
    row.flood_flag = 0  # GloFAS poller's job (§7.3.3)
    row.planned_power = 0  # KPLC poller's job (§5.3.14)
    row.access_risk = 0  # not derived here: whether rain blocks a road is a floor judgement, not a threshold
    row.payload_json = snapshot_payload_json(snapshot)
    row.derived_json = json.dumps(dict(derived), separators=(",", ":"), ensure_ascii=False)
    row.last_error = None
    return row


def record_failure(
    session: Session,
    *,
    operator_id: str,
    source: str,
    region_code: str,
    error: str,
    now: datetime,
) -> ExternalSignalRow:
    """Fail-soft bookkeeping (§7.3.5 "provider down → previous row kept, stale after valid_until").

    Annotates the region's last good row with ``last_error`` and, once ``valid_until`` has
    passed, ``stale=1`` — its payload and derived block are left exactly as they were. With no
    good row at all, writes a zero-confidence marker row (``external_id`` ``<region>:unavailable``,
    already expired, no derived block) so the strip can show STALE *and why*. Does not commit.
    """
    error = error[:2000]
    good = latest_signal(session, operator_id, region_code)
    if good is not None:
        good.last_error = error
        if now >= good.valid_until:
            good.stale = 1
        return good
    external_id = f"{region_code}{_MARKER_SUFFIX}"
    marker = session.scalar(
        select(ExternalSignalRow).where(
            ExternalSignalRow.operator_id == operator_id,
            ExternalSignalRow.source == source,
            ExternalSignalRow.external_id == external_id,
        )
    )
    if marker is None:
        marker = ExternalSignalRow(
            id=new_id(), operator_id=operator_id, source=source, external_id=external_id,
            source_url="", region_code=region_code, payload_json="{}", created_at=now,
        )
        session.add(marker)
    marker.fetched_at = now
    marker.valid_from = None
    marker.valid_until = now  # already expired: never mistaken for a forecast
    marker.stale = 1
    marker.confidence = 0.0
    marker.storm_flag = marker.flood_flag = marker.planned_power = marker.access_risk = 0
    marker.derived_json = None
    marker.last_error = error
    return marker


# ---------------------------------------------------------------------------- the job


@dataclass(frozen=True)
class RegionOutcome:
    region_code: str
    ok: bool
    source: str
    error: str | None = None
    error_kind: str | None = None
    storm_flag: bool = False
    rain_mm_next_6h: float | None = None
    gust_kmh_max: float | None = None
    hours_in_window: int = 0
    from_cache: bool = False
    row_id: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_tool(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": "weather.forecast",
            "ok": self.ok,
            "region_code": self.region_code,
            "source": self.source,
            "storm_flag": self.storm_flag,
            "rain_mm_next_6h": self.rain_mm_next_6h,
            "gust_kmh_max": self.gust_kmh_max,
            "hours_in_window": self.hours_in_window,
            "from_cache": self.from_cache,
        }
        if self.error:
            out["error"] = self.error
            out["error_kind"] = self.error_kind
        return out


def _publish(operator_id: str, source: str, region_code: str, *, stale: bool, storm_flag: bool, error: str | None) -> None:
    """``external_signal.updated`` (§7.3.2). The hub never raises, but a broken hub must not break a poll."""
    try:
        hub.publish_sync(
            RealtimeEvent(
                type="external_signal.updated",
                operator_id=operator_id,
                payload={
                    "source": source,
                    "region_code": region_code,
                    "stale": stale,
                    "storm_flag": storm_flag,
                    "flood_flag": False,
                    "error": error,
                },
            )
        )
    except Exception:  # noqa: BLE001 — advisory fan-out only
        log.exception("weather: publishing external_signal.updated failed (ignored)")


def poll_region(
    session: Session,
    *,
    operator_id: str,
    region_code: str,
    lat: float,
    lon: float,
    provider: WeatherProvider,
    now: datetime,
    thresholds: WeatherThresholds | None = None,
) -> RegionOutcome:
    """Fetch, derive, upsert and commit one region. Never raises: every failure becomes a
    ``RegionOutcome(ok=False)`` and a ``record_failure`` on the region's rows."""
    source = getattr(provider, "source", "OPEN_METEO")
    try:
        snapshot = provider.forecast(lat, lon, hours=48, now=now)
        derived = derive_weather_risk(snapshot, now=now, thresholds=thresholds)
        if derived["hours_in_window"] == 0:
            first, last = snapshot.first_hour, snapshot.last_hour
            raise WeatherError(
                "malformed",
                f"{source} forecast does not cover now ({now:%Y-%m-%dT%H:%MZ}); "
                f"it spans {first:%Y-%m-%dT%H:%MZ}..{last:%Y-%m-%dT%H:%MZ}" if first and last else
                f"{source} forecast has no hours at all",
                source=source,
            )
        row = upsert_signal(session, operator_id=operator_id, region_code=region_code, snapshot=snapshot, derived=derived, now=now)
        session.commit()
        outcome = RegionOutcome(
            region_code=region_code,
            ok=True,
            source=snapshot.source,
            storm_flag=bool(derived["storm_flag"]),
            rain_mm_next_6h=derived["rain_mm_next_6h"],
            gust_kmh_max=derived["gust_kmh_max"],
            hours_in_window=int(derived["hours_in_window"]),
            from_cache=snapshot.from_cache,
            row_id=row.id,
            detail={"storm_reasons": derived["storm_reasons"]},
        )
        _publish(operator_id, snapshot.source, region_code, stale=False, storm_flag=outcome.storm_flag, error=None)
        if outcome.storm_flag:
            log.warning("weather: STORM flag %s via %s: %s", region_code, snapshot.source, "; ".join(derived["storm_reasons"]))
        return outcome
    except WeatherError as exc:
        err, kind = str(exc), exc.kind
    except Exception as exc:  # noqa: BLE001 — a bug in parsing must degrade, not crash the scheduler
        log.exception("weather: unexpected failure polling %s", region_code)
        err, kind = f"unexpected: {type(exc).__name__}: {exc}"[:2000], "unexpected"

    log.warning("weather: %s poll failed for %s: %s", source, region_code, err)
    session.rollback()
    try:
        row = record_failure(session, operator_id=operator_id, source=source, region_code=region_code, error=err, now=now)
        session.commit()
        stale = is_stale(row, now)
        storm = bool(row.storm_flag)
        row_id = row.id
    except Exception:  # noqa: BLE001 — the database itself is unhappy: say so, still do not raise
        log.exception("weather: recording the failure for %s failed", region_code)
        session.rollback()
        stale, storm, row_id = True, False, None
    _publish(operator_id, source, region_code, stale=stale, storm_flag=storm, error=err)
    return RegionOutcome(region_code=region_code, ok=False, source=source, error=err, error_kind=kind, storm_flag=storm, row_id=row_id)


def poll(
    session: Session,
    settings: AppSettings,
    *,
    provider: WeatherProvider | None = None,
    now: datetime | None = None,
    thresholds: WeatherThresholds | None = None,
    centroids: Mapping[str, tuple[float, float]] | None = None,
) -> JobResult:
    """The ``weather_regions`` job (§5.3.13). Never raises.

    Keyword arguments exist for tests and on-demand runs; the scheduler calls
    ``poll(session, settings)``. ``provider`` defaults to :func:`provider_from_env`.
    """
    if not weather_enabled():
        return JobResult(
            summary=f"{JOB_NAME} skipped: {ENABLED_ENV} is not true",
            rationale="Weather early warning is opt-in (§5.3.13); no request was made and no row was written",
            tools=({"name": "weather.poll", "ok": True, "skipped": True, "reason": f"{ENABLED_ENV} unset or false"},),
        )

    now = now or utcnow()
    operator_id = settings.operator.operator_id
    regions = dict(centroids) if centroids is not None else region_centroids()
    if not regions:
        return JobResult(
            summary=f"{JOB_NAME}: no region has a catalogued coordinate; nothing to fetch",
            rationale="Region centroids come from the site seed (§5.3.13); the seed carries no lat/lon",
            tools=({"name": "weather.poll", "ok": False, "skipped": True, "reason": "no centroids"},),
        )

    if provider is None:
        try:
            provider = provider_from_env()
        except WeatherError as exc:  # unknown provider name: every region is unavailable, say why
            err = str(exc)
            log.error("weather: provider unavailable: %s", err)
            outcomes = []
            for code in regions:
                session.rollback()
                try:
                    record_failure(session, operator_id=operator_id, source="OPEN_METEO", region_code=code, error=err, now=now)
                    session.commit()
                except Exception:  # noqa: BLE001
                    log.exception("weather: recording the config failure for %s failed", code)
                    session.rollback()
                outcomes.append(RegionOutcome(region_code=code, ok=False, source="OPEN_METEO", error=err, error_kind=exc.kind))
            return _result(outcomes, "OPEN_METEO", now)

    outcomes = [
        poll_region(
            session,
            operator_id=operator_id,
            region_code=code,
            lat=lat,
            lon=lon,
            provider=provider,
            now=now,
            thresholds=thresholds,
        )
        for code, (lat, lon) in regions.items()
    ]
    return _result(outcomes, getattr(provider, "source", "OPEN_METEO"), now)


def _result(outcomes: list[RegionOutcome], source: str, now: datetime) -> JobResult:
    ok = [o for o in outcomes if o.ok]
    failed = [o for o in outcomes if not o.ok]
    storms = sorted(o.region_code for o in ok if o.storm_flag)
    parts = [f"{len(ok)}/{len(outcomes)} regions fetched via {source}"]
    if storms:
        parts.append("STORM flag: " + ", ".join(storms))
    if failed:
        kinds = sorted({o.error_kind or "error" for o in failed})
        parts.append(f"{len(failed)} failed ({', '.join(kinds)}); last good rows kept and labelled")
    rationale = (
        "Advisory only: weather never changes priority or triggers an action (§7.3). "
        f"Rows keyed <region>:<15-min bucket>, valid {int(ROW_VALID_FOR.total_seconds() // 60)} min from fetch; "
        "a failed region keeps its previous row with last_error set and stale=1 once valid_until passes."
    )
    return JobResult(summary="; ".join(parts), rationale=rationale, tools=tuple(o.as_tool() for o in outcomes))


#: The scheduler card (§4.4 roster). Not yet wired into ``scheduler.loop.SCHEDULED_JOBS`` —
#: that file belongs to another wave; adding ``pollers.weather.WEATHER_JOB`` to the tuple is
#: the whole change. ``poll`` gates on ``WEATHER_ENABLED`` itself, so wiring it is safe by default.
WEATHER_JOB = JobCard(
    JOB_NAME, INTERVAL_S, poll, ENABLED_ENV, AGENT, GRAPH_NAME,
    max_seconds=60,
    default_enabled=False,  # WEATHER_ENABLED unset means OFF (spec Appendix B)
)
