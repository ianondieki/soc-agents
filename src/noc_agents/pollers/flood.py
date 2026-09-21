"""``flood_daily``: the WeatherRiskAgent's daily GloFAS river-discharge read (spec §5.3.13, §7.3.3).

Why this exists, operationally: rain is a forecast; a river is a consequence. A tower on the
Kano plains does not go down because it rained in Kisumu this afternoon — it goes down when
the Nyando bursts two days after it rained on the Nandi escarpment. GloFAS models that
routing, and the Open-Meteo Flood API serves it for free, so a riverine site can carry a flood
flag days before the water arrives. **Advisory only**: nothing here changes a priority, opens
an incident or dispatches anyone. A live, fresh ``flood_flag`` does turn a Regions tile to
ALERT (``services/dashboards._status``), which is why the rule and its limits are written out
in ``adapters/flood.py`` rather than left implicit.

What one run does
-----------------
For each **riverine** site in the catalogue (``services/sites.py``; ``riverine`` is the seed's
conservative "documented flood-plain exposure" flag — only Kisumu today) that sits in one of
the *active operator's* regions — and only when the active operator is the one the catalogue
belongs to (see "Operator scoping" below) — it calls :class:`~noc_agents.adapters.flood.OpenMeteoFloodProvider`
once, derives the flood block, and upserts one ``external_signals`` row keyed
``<site_id>:<YYYY-MM-DD>``: a re-run the same day replaces the row, and successive days keep a
history for the §10.6 backtest. ``region_code``, ``county`` and ``site_id`` come from the
catalogue, so the Regions dashboard's flood block (which reads ``GLOFAS`` rows by region)
lights up without a change to that file.

Non-riverine sites are not polled at all. That is the spec's rule ("only riverine sites
matter") and it is also the honest one: a flood flag on a hilltop site derived from whatever
river the 5 km grid cell happens to contain would be noise wearing a warning's clothes.

Fail-soft, site by site
-----------------------
Same contract as ``pollers/weather.py``: a timeout, a 500, malformed JSON, an oversized body or
a TLS failure is caught per site and never raised. The site's last good row keeps its payload
and flag, ``last_error`` says why, and it turns ``stale=1`` once ``valid_until`` passes; a site
with no good row gets an already-expired, zero-confidence marker row so the strip can say
STALE *and why* instead of showing a blank. ``pollers/weather.py`` writes ``flood_flag = 0`` on
its own rows and says this poller owns the flag — it does, on these rows, and never on theirs.

Operator scoping
----------------
``SiteRecord`` has no operator field: the catalogue is one file, ``data/seed/safaricom_sites.json``,
and every site in it is Safaricom's. Region codes are **not** a safe proxy for ownership — both
operator profiles define ``CST`` — so filtering by region alone would, the day a Coast site is
marked riverine, write Safaricom's tower under Airtel's ``operator_id`` (review finding F18;
an earlier docstring here claimed the codes differ, which is false). Until sites carry an
operator, the job runs only for :data:`SITE_SEED_OPERATOR` and says why for anyone else.

The Regions dashboard reads the resulting rows **aggregated across sites**
(``services.signals.flood_region_state``): a region flags if ANY site's current, fresh reading
flags, so one calm site cannot hide another site's flood (review finding F09).

Gating
------
``WEATHER_ENABLED``, default **false**: §5.3.13 lists ``flood_daily`` as the third trigger of
the one WeatherRiskAgent under that flag, and ``.env.example`` names no separate flood flag.
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

from noc_agents.adapters.flood import (
    GLOFAS,
    FloodSnapshot,
    FloodThresholds,
    OpenMeteoFloodProvider,
    derive_flood_risk,
    provider_from_env,
    snapshot_payload_json,
)
from noc_agents.adapters.weather import WeatherError
from noc_agents.config import AppSettings
from noc_agents.db.models import ExternalSignalRow, new_id, utcnow
from noc_agents.realtime.hub import RealtimeEvent, hub
from noc_agents.scheduler import JobCard, JobResult
from noc_agents.services.signals import is_stale
from noc_agents.services.sites import SiteRecord, all_sites

log = logging.getLogger("noc_agents.pollers.flood")

__all__ = [
    "AGENT",
    "ENABLED_ENV",
    "FLOOD_JOB",
    "GRAPH_NAME",
    "SITE_SEED_OPERATOR",
    "INTERVAL_S",
    "JOB_NAME",
    "ROW_VALID_FOR",
    "SiteOutcome",
    "flood_enabled",
    "latest_site_row",
    "poll",
    "poll_site",
    "record_failure",
    "riverine_sites",
    "upsert_reading",
]

JOB_NAME = "flood_daily"
INTERVAL_S = 86400  # daily (§5.3.13 "flood_daily"); GloFAS itself updates once a day
AGENT = "WeatherRiskAgent"
GRAPH_NAME = "flood"
ENABLED_ENV = "WEATHER_ENABLED"  # §5.3.13: one flag for the agent's three triggers

#: How long a reading stays trustworthy: one daily cadence plus two hours of grace, so a
#: single late run does not flash STALE but a missed day does. UNVERIFIED operational value (D14).
ROW_VALID_FOR = timedelta(hours=26)

#: The operator the site catalogue belongs to. ``services/sites.py`` loads exactly one seed,
#: ``safaricom_sites.json``; ``tests/unit/test_flood.py`` pins that this matches its file name,
#: so replacing the seed without revisiting this scoping fails a test rather than leaking.
SITE_SEED_OPERATOR = "safaricom"

_TRUE = {"1", "true", "yes", "on"}
_MARKER_SUFFIX = ":unavailable"


def flood_enabled() -> bool:
    """``WEATHER_ENABLED`` — default **false**. Only an explicit true value polls."""
    return (os.getenv(ENABLED_ENV) or "").strip().lower() in _TRUE


def riverine_sites(
    region_codes: Iterable[str] | None = None,
    sites: Iterable[SiteRecord] | None = None,
) -> list[SiteRecord]:
    """Catalogued riverine sites with coordinates, restricted to ``region_codes`` when given.

    This filter is NOT operator scoping: region codes overlap between operators (both define
    ``CST``). :func:`poll` scopes by :data:`SITE_SEED_OPERATOR` before it ever calls this.
    """
    allowed = {c.upper() for c in region_codes} if region_codes is not None else None
    out = []
    for site in sites if sites is not None else all_sites():
        if not site.riverine or not site.has_coordinates or not site.region_code:
            continue
        if allowed is not None and site.region_code.upper() not in allowed:
            continue
        out.append(site)
    return sorted(out, key=lambda s: s.site_id)


# ---------------------------------------------------------------------------- rows


def _external_id(site_id: str, now: datetime) -> str:
    return f"{site_id}:{now.strftime('%Y-%m-%d')}"


def latest_site_row(session: Session, operator_id: str, site_id: str, *, good_only: bool = True) -> ExternalSignalRow | None:
    """Newest GloFAS row for one site (a *good* one — with a derived block — by default)."""
    stmt = select(ExternalSignalRow).where(
        ExternalSignalRow.operator_id == operator_id,
        ExternalSignalRow.source == GLOFAS,
        ExternalSignalRow.site_id == site_id,
    )
    if good_only:
        stmt = stmt.where(ExternalSignalRow.derived_json.is_not(None))
    stmt = stmt.order_by(ExternalSignalRow.fetched_at.desc(), ExternalSignalRow.created_at.desc()).limit(1)
    return session.scalars(stmt).first()


def upsert_reading(
    session: Session,
    *,
    operator_id: str,
    site: SiteRecord,
    snapshot: FloodSnapshot,
    derived: Mapping[str, Any],
    now: datetime,
    valid_for: timedelta = ROW_VALID_FOR,
) -> ExternalSignalRow:
    """Insert or replace the row for ``(operator, GLOFAS, <site>:<day>)``. Does not commit."""
    external_id = _external_id(site.site_id, now)
    row = session.scalar(
        select(ExternalSignalRow).where(
            ExternalSignalRow.operator_id == operator_id,
            ExternalSignalRow.source == GLOFAS,
            ExternalSignalRow.external_id == external_id,
        )
    )
    if row is None:
        row = ExternalSignalRow(id=new_id(), operator_id=operator_id, source=GLOFAS, external_id=external_id, created_at=now)
        session.add(row)
    row.source_url = snapshot.source_url
    row.region_code = site.region_code
    row.county = site.county
    row.site_id = site.site_id
    row.fetched_at = snapshot.fetched_at
    row.valid_from = snapshot.fetched_at
    row.valid_until = snapshot.fetched_at + valid_for
    row.stale = 0
    row.confidence = 1.0
    row.storm_flag = 0  # the forecast poller's flag (§7.3.1); a river reading says nothing about wind
    row.flood_flag = 1 if derived.get("flood_flag") else 0
    row.planned_power = 0
    # access_risk is not derived: whether a flooded river cuts the road to a site is a floor
    # judgement about a specific road, not something a 5 km discharge cell can say.
    row.access_risk = 0
    row.payload_json = snapshot_payload_json(snapshot)
    row.derived_json = json.dumps(
        {**dict(derived), "site_id": site.site_id, "site_name": site.site_name},
        separators=(",", ":"), ensure_ascii=False,
    )
    row.last_error = None
    return row


def record_failure(
    session: Session,
    *,
    operator_id: str,
    site: SiteRecord,
    error: str,
    now: datetime,
) -> ExternalSignalRow:
    """Fail-soft bookkeeping, as ``pollers.weather.record_failure`` does it (§7.3.5).

    The site's last good row gets ``last_error`` and, once ``valid_until`` has passed,
    ``stale=1`` — its payload, derived block and flood flag are left exactly as they were.
    With no good row, an already-expired zero-confidence marker (``<site>:unavailable``, no
    derived block, ``flood_flag=0``) is written so the strip shows STALE *and why*. The marker
    never carries a flag: a site we have never read is not a site we believe is flooding.
    Does not commit.
    """
    error = error[:2000]
    good = latest_site_row(session, operator_id, site.site_id)
    if good is not None:
        good.last_error = error
        if now >= good.valid_until:
            good.stale = 1
        return good
    external_id = f"{site.site_id}{_MARKER_SUFFIX}"
    marker = session.scalar(
        select(ExternalSignalRow).where(
            ExternalSignalRow.operator_id == operator_id,
            ExternalSignalRow.source == GLOFAS,
            ExternalSignalRow.external_id == external_id,
        )
    )
    if marker is None:
        marker = ExternalSignalRow(
            id=new_id(), operator_id=operator_id, source=GLOFAS, external_id=external_id,
            source_url="", payload_json="{}", created_at=now,
        )
        session.add(marker)
    marker.region_code = site.region_code
    marker.county = site.county
    marker.site_id = site.site_id
    marker.fetched_at = now
    marker.valid_from = None
    marker.valid_until = now  # already expired: never mistaken for a reading
    marker.stale = 1
    marker.confidence = 0.0
    marker.storm_flag = marker.flood_flag = marker.planned_power = marker.access_risk = 0
    marker.derived_json = None
    marker.last_error = error
    return marker


# ---------------------------------------------------------------------------- the job


@dataclass(frozen=True)
class SiteOutcome:
    site_id: str
    region_code: str
    ok: bool
    flood_flag: bool = False
    ratio: float | None = None
    peak_m3s: float | None = None
    error: str | None = None
    error_kind: str | None = None
    row_id: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_tool(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": "flood.discharge",
            "ok": self.ok,
            "site_id": self.site_id,
            "region_code": self.region_code,
            "flood_flag": self.flood_flag,
            "ratio": self.ratio,
            "river_discharge_max": self.peak_m3s,
        }
        if self.error:
            out["error"] = self.error
            out["error_kind"] = self.error_kind
        return out


def _publish(operator_id: str, region_code: str, site_id: str, *, stale: bool, flood_flag: bool, error: str | None) -> None:
    """``external_signal.updated`` (§7.3.2). A broken hub must not break a poll."""
    try:
        hub.publish_sync(
            RealtimeEvent(
                type="external_signal.updated",
                operator_id=operator_id,
                payload={
                    "source": GLOFAS,
                    "region_code": region_code,
                    "site_id": site_id,
                    "stale": stale,
                    "storm_flag": False,
                    "flood_flag": flood_flag,
                    "error": error,
                },
            )
        )
    except Exception:  # noqa: BLE001 — advisory fan-out only
        log.exception("flood: publishing external_signal.updated failed (ignored)")


def poll_site(
    session: Session,
    *,
    operator_id: str,
    site: SiteRecord,
    provider: OpenMeteoFloodProvider,
    now: datetime,
    thresholds: FloodThresholds | None = None,
) -> SiteOutcome:
    """Fetch, derive, upsert and commit one site. Never raises."""
    try:
        snapshot = provider.discharge(float(site.lat), float(site.lon), now=now)  # type: ignore[arg-type]
        derived = derive_flood_risk(snapshot, thresholds=thresholds, today=now.date())
        if derived["days_in_window"] == 0:
            raise WeatherError(
                "malformed",
                f"GloFAS forecast does not cover today ({now:%Y-%m-%d}); it spans "
                f"{snapshot.days[0].day.isoformat()}..{snapshot.days[-1].day.isoformat()}" if snapshot.days
                else "GloFAS forecast has no days at all",
                source=GLOFAS,
            )
        row = upsert_reading(session, operator_id=operator_id, site=site, snapshot=snapshot, derived=derived, now=now)
        session.commit()
        outcome = SiteOutcome(
            site_id=site.site_id, region_code=site.region_code, ok=True,
            flood_flag=bool(derived["flood_flag"]), ratio=derived["ratio"], peak_m3s=derived["river_discharge_max"],
            row_id=row.id, detail={"reason": derived.get("reason")},
        )
        _publish(operator_id, site.region_code, site.site_id, stale=False, flood_flag=outcome.flood_flag, error=None)
        if outcome.flood_flag:
            log.warning("flood: FLOOD flag %s (%s): %s", site.site_id, site.region_code, derived.get("reason"))
        return outcome
    except WeatherError as exc:  # FloodError is one
        err, kind = str(exc), exc.kind
    except Exception as exc:  # noqa: BLE001 — a parsing bug must degrade, not crash the scheduler
        log.exception("flood: unexpected failure polling %s", site.site_id)
        err, kind = f"unexpected: {type(exc).__name__}: {exc}"[:2000], "unexpected"

    log.warning("flood: GloFAS poll failed for %s: %s", site.site_id, err)
    session.rollback()
    try:
        row = record_failure(session, operator_id=operator_id, site=site, error=err, now=now)
        session.commit()
        stale, flag, row_id = is_stale(row, now), bool(row.flood_flag), row.id
    except Exception:  # noqa: BLE001 — the database itself is unhappy: say so, still do not raise
        log.exception("flood: recording the failure for %s failed", site.site_id)
        session.rollback()
        stale, flag, row_id = True, False, None
    _publish(operator_id, site.region_code, site.site_id, stale=stale, flood_flag=flag, error=err)
    return SiteOutcome(site_id=site.site_id, region_code=site.region_code, ok=False, flood_flag=flag, error=err, error_kind=kind, row_id=row_id)


def poll(
    session: Session,
    settings: AppSettings,
    *,
    provider: OpenMeteoFloodProvider | None = None,
    now: datetime | None = None,
    thresholds: FloodThresholds | None = None,
    sites: Iterable[SiteRecord] | None = None,
) -> JobResult:
    """The ``flood_daily`` job (§5.3.13). Never raises."""
    if not flood_enabled():
        return JobResult(
            summary=f"{JOB_NAME} skipped: {ENABLED_ENV} is not true",
            rationale="GloFAS flood early warning is opt-in with the rest of the WeatherRiskAgent (§5.3.13); "
            "no request was made and no row was written",
            tools=({"name": "flood.poll", "ok": True, "skipped": True, "reason": f"{ENABLED_ENV} unset or false"},),
        )
    now = now or utcnow()
    cfg = settings.operator
    operator_id = cfg.operator_id
    if operator_id != SITE_SEED_OPERATOR and sites is None:
        return JobResult(
            summary=f"{JOB_NAME}: the site catalogue belongs to {SITE_SEED_OPERATOR}; there is none for "
            f"{operator_id}, so nothing to fetch",
            rationale="SiteRecord carries no operator and region codes overlap between profiles (both define "
            "CST), so writing catalogue sites under another operator_id would attribute one operator's towers "
            "to another (review finding F18)",
            tools=({"name": "flood.poll", "ok": True, "skipped": True, "reason": "no site catalogue for this operator"},),
        )
    targets = riverine_sites(list(getattr(cfg, "regions", {}) or {}), sites)
    if not targets:
        return JobResult(
            summary=f"{JOB_NAME}: no riverine site with coordinates in any {operator_id} region; nothing to fetch",
            rationale="Only riverine sites are polled (§7.3.1). The catalogue's riverine flag is conservative "
            "(services/sites.py) and a flood-plain overlay is still outstanding",
            tools=({"name": "flood.poll", "ok": True, "skipped": True, "reason": "no riverine sites"},),
        )
    provider = provider or provider_from_env()
    outcomes = [
        poll_site(session, operator_id=operator_id, site=site, provider=provider, now=now, thresholds=thresholds)
        for site in targets
    ]
    return _result(outcomes)


def _result(outcomes: list[SiteOutcome]) -> JobResult:
    ok = [o for o in outcomes if o.ok]
    failed = [o for o in outcomes if not o.ok]
    floods = sorted(f"{o.site_id} ({o.region_code})" for o in ok if o.flood_flag)
    parts = [f"{len(ok)}/{len(outcomes)} riverine sites read from GloFAS"]
    if floods:
        parts.append("FLOOD flag: " + ", ".join(floods))
    if failed:
        kinds = sorted({o.error_kind or "error" for o in failed})
        parts.append(f"{len(failed)} failed ({', '.join(kinds)}); last good rows kept and labelled")
    rationale = (
        "Advisory only (§7.3). flood_flag = peak daily river_discharge / mean daily river_discharge_mean over "
        "7 days >= 2.0 (spec §7.3.1, literal; UNVERIFIED threshold, D14). Rows keyed <site>:<day>, valid "
        f"{int(ROW_VALID_FOR.total_seconds() // 3600)} h; a failed site keeps its previous row with last_error set."
    )
    return JobResult(summary="; ".join(parts), rationale=rationale, tools=tuple(o.as_tool() for o in outcomes))


#: The scheduler card. **Not wired into** ``scheduler.loop.SCHEDULED_JOBS`` (another lane's
#: file; the one-line change is in the lane report). ``poll`` re-checks ``WEATHER_ENABLED``
#: itself, and ``default_enabled=False`` makes /scheduler/status report it off when unset.
FLOOD_JOB = JobCard(
    JOB_NAME, INTERVAL_S, poll, ENABLED_ENV, AGENT, GRAPH_NAME,
    max_seconds=60,
    default_enabled=False,  # WEATHER_ENABLED unset means OFF (spec Appendix B)
)
