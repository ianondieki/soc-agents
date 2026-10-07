"""Regions dashboard rollup (spec §7.4.2, Phase 4 Lane 4B).

What this is for
----------------
The Regions dashboard is the one screen a duty manager looks at when they have to
decide *where* to send attention next. It answers one question per region — "how
bad is it here, and do we even know?" — by composing things that already exist:
open incidents and their priorities, open problems and whether they are known
errors, a 30-day repeat-fault rate, and the outside-world signals the Phase 3
pollers cache in ``external_signals``.

Three properties this module exists to guarantee
------------------------------------------------
**1. Every configured region appears, always.** The row list is driven by the
operator profile's ``regions`` (the same source ``GET /api/v1/profile`` serves),
not by a ``GROUP BY region_code`` over incidents. A region with zero incidents is
the *interesting* case on a wallboard — either it is genuinely quiet or its
alarms are not reaching us — and a GROUP BY silently deletes it. Six regions
configured means six rows, forever.

**2. Silence renders as STALE, never as green.** A region with no open incidents
and no fresh external signal is reported ``status: "STALE"``, not ``"CALM"``.
This is the whole reason the dashboard is worth building: a green tile is a claim
("we looked, it is fine"), and we are not entitled to make it when the only
evidence is an absence of data. ``WEATHER_ENABLED`` is off in the normal
deployment, so most regions will read STALE on day one — that is the honest
answer, and ``signals.weather.reason`` says exactly why so nobody has to guess.

**3. Nothing here recomputes a signal.** ``weather_risk_for_region`` (§7.3.3)
already derives the risk block and already recomputes ``stale`` against *now*
rather than trusting the stored flag. This module calls it. Flood, CAP and KPLC
have no poller yet, so they are read generically out of ``external_signals`` by
source: when those Phase 3 lanes land, their rows light up these blocks with no
change here.

Operator scoping
----------------
This is exactly the surface where an unscoped ``select()`` leaks: an aggregate
over incidents has no id in the response for a reviewer to notice is foreign, so
the other operator's faults would simply be *added to our totals* and look like
our own bad night. Every read of an operator-owned table therefore goes through
``api.deps._owned`` / ``_operator_scoped``, which is the one place the operator
clause is built. ``services`` importing from ``api.deps`` is unusual in this tree,
and deliberate: ``deps`` is a leaf (it imports only ``api.auth``, ``config`` and
``db.models``, never ``main``), and a second hand-written ``WHERE operator_id =``
in here would be a copy that can drift from the one reviewers check.

Timestamps
----------
Every timestamp in this payload is a **string** ending in ``Z``, seconds
precision — the same spelling ``pollers.weather.staleness()`` already produces.
The alternative (``z_utc`` datetimes, as ``api/serializers.py`` uses) would mean
two spellings of a timestamp inside one payload, because the composed weather
block arrives as a string already. One shape per payload beats consistency with a
module the frontend reads separately.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Sequence

import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from noc_agents.api.deps import _operator_scoped, _owned, _settings
from noc_agents.db.models import ExternalSignalRow, IncidentRow, ProblemRow, utcnow
from noc_agents.services.backtest import signal_precision_30d
from noc_agents.services.clock import iso_z
from noc_agents.services.lifecycle import NOT_OPEN_STATUSES
from noc_agents.services.signals import cap_feed_health, cap_severe_in_force, flood_region_state
from noc_agents.pollers.weather import (
    is_stale,
    latest_error,
    weather_enabled,
    weather_risk_for_region,
)

log = logging.getLogger("noc_agents.services.dashboards")

__all__ = [
    "CA_QOS_SEED_PATH",
    "REGION_STATUSES",
    "ROLLUP_WINDOW_DAYS",
    "clear_ca_qos_cache",
    "load_ca_qos",
    "regions_dashboard",
    "regulatory_baseline_for",
]

ROOT = Path(__file__).resolve().parents[3]

#: §7.4.3 names this path literally. Hand-parsed once a year; never fetched.
CA_QOS_SEED_PATH = ROOT / "data" / "seed" / "ca_qos_FY2024_2025.yaml"

#: The rollup window for ``repeat_fault_rate_30d`` and ``incidents_30d``. Reported
#: on the payload as ``window_days`` so the card can label the number instead of
#: leaving a reader to assume it means "ever".
ROLLUP_WINDOW_DAYS = 30

#: How many problems each region card carries. The full count travels as
#: ``problems_open_total``, so truncating the list never hides the scale.
PROBLEMS_PER_REGION = 5

#: The status ladder, worst first. A closed vocabulary because it drives colour on
#: a wallboard, and a frontend that meets an unexpected value has no safe default.
REGION_STATUSES: tuple[str, ...] = ("ALERT", "WATCH", "STALE", "CALM")

#: Incident statuses that are not open: restored (awaiting close), closed or cancelled. The same
#: set ``/metrics/summary`` and the Incident board's Open tab use (``NOT_OPEN_STATUSES``).
_NOT_OPEN = NOT_OPEN_STATUSES

_PRIORITIES: tuple[str, ...] = ("P1", "P2", "P3", "P4")

# Which ``external_signals.source`` values belong to which card block. Weather has
# two providers because the poller falls back between them (§7.3.3). The flood and
# CAP blocks are read through ``services.signals`` (``flood_region_state``,
# ``cap_feed_health``), which own the per-site aggregation and the feed-health
# judgement; KPLC has no poller yet, which is why its block comes back
# ``available: false`` rather than being omitted.
_WEATHER_SOURCES: tuple[str, ...] = ("OPEN_METEO", "MET_NORWAY")
_KPLC_SOURCES: tuple[str, ...] = ("KPLC",)

#: KPLC planned-interruption look-ahead. Two days is the notice period a NOC can
#: still act on: long enough to move a generator, short enough that the list is
#: not a wall of noise.
_KPLC_HORIZON = timedelta(hours=48)


# --------------------------------------------------------------------------- time


def _z(dt: datetime | None) -> str | None:
    """Naive-UTC storage value → ``"2026-09-18T12:00:00Z"``. ``None`` passes through.

    Seconds precision: a dashboard never needs microseconds, and trimming them makes
    the contract test's expected strings readable by a human reviewing a diff.
    """
    return iso_z(dt)


# ------------------------------------------------------------- CA QoS baseline seed


@lru_cache(maxsize=1)
def load_ca_qos(path: str | None = None) -> dict[str, Any]:
    """The parsed ``ca_qos`` block of the seed, or ``{}`` if it is missing/unreadable.

    Never raises. A malformed or absent baseline seed must not take the whole
    Regions dashboard down — the regulator's annual score is the least urgent
    thing on the card, and a night shift losing the fault picture because a YAML
    file was edited badly would be an absurd trade. The failure is logged and the
    ``regulatory_baseline`` block comes back ``None``.
    """
    target = Path(path) if path else CA_QOS_SEED_PATH
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:  # missing file, bad YAML, permissions
        log.warning("CA QoS baseline seed unreadable at %s: %s", target, exc)
        return {}
    block = raw.get("ca_qos")
    return block if isinstance(block, dict) else {}


def clear_ca_qos_cache() -> None:
    """Drop the memoised seed. For tests and for an operator who edits the YAML live."""
    load_ca_qos.cache_clear()


def _cluster_score(seed: dict[str, Any], cluster_name: str, operator_id: str) -> float | None:
    """The cluster's score for this operator, or ``None`` if the cluster is unparsed."""
    for entry in seed.get("clusters") or []:
        if isinstance(entry, dict) and entry.get("name") == cluster_name:
            scores = entry.get("scores")
            if isinstance(scores, dict):
                value = scores.get(operator_id)
                if isinstance(value, (int, float)):
                    return float(value)
    return None


def regulatory_baseline_for(region_code: str, operator_id: str) -> dict[str, Any] | None:
    """The ``regulatory_baseline`` card block for one region (§7.4.3), or ``None``.

    ``None`` when the seed has no entry for this operator at all — the card then
    shows nothing rather than a zero, because "no published score" and "scored
    zero" are opposite claims about a licensee.

    ``granularity`` is the load-bearing field and it is **never** ``"region"``:

    * ``"cluster"`` — this region is mapped to one of the report's five clusters
      and that cluster's score has been transcribed. ``cluster`` names it.
    * ``"operator"`` — the honest fallback. Either nobody has parsed the PDF yet
      (the state this repo ships in) or this region straddles clusters and the
      person who parsed it declined to pick one. ``ca_qos_score`` is then the
      operator-wide figure and the card must say so, because presenting a national
      average as a regional measurement is how a dashboard starts lying.
    """
    seed = load_ca_qos()
    operators = seed.get("operators")
    entry = operators.get(operator_id) if isinstance(operators, dict) else None
    if not isinstance(entry, dict):
        return None

    mapping = seed.get("cluster_to_region")
    per_operator = mapping.get(operator_id) if isinstance(mapping, dict) else None
    cluster = per_operator.get(region_code) if isinstance(per_operator, dict) else None

    score: float | None = None
    granularity = "operator"
    if isinstance(cluster, str) and cluster:
        score = _cluster_score(seed, cluster, operator_id)
        if score is not None:
            granularity = "cluster"
        else:  # mapped to a cluster nobody has transcribed a score for
            cluster = None
    else:
        cluster = None

    if score is None:
        overall = entry.get("overall_pct")
        score = float(overall) if isinstance(overall, (int, float)) else None

    pass_mark = seed.get("pass_mark_pct")
    pass_mark = float(pass_mark) if isinstance(pass_mark, (int, float)) else None

    return {
        "ca_qos_score": score,
        "report": seed.get("report"),
        "report_date": seed.get("report_date"),
        "granularity": granularity,
        "cluster": cluster,
        "pass_mark_pct": pass_mark,
        "meets_pass_mark": None if (score is None or pass_mark is None) else score >= pass_mark,
        "parsed": bool(seed.get("parsed_by")),
        "source_url": seed.get("source_url"),
    }


# ------------------------------------------------------------------ signal blocks


def _latest_signal_row(
    session: Session, region_code: str, sources: Sequence[str]
) -> ExternalSignalRow | None:
    """Newest ``external_signals`` row for this region from any of ``sources``, scoped."""
    stmt = (
        _owned(ExternalSignalRow)
        .where(
            ExternalSignalRow.region_code == region_code,
            ExternalSignalRow.source.in_(tuple(sources)),
        )
        .order_by(ExternalSignalRow.fetched_at.desc(), ExternalSignalRow.created_at.desc())
        .limit(1)
    )
    return session.scalars(stmt).first()


def _count_live(
    session: Session,
    region_code: str,
    sources: Sequence[str],
    now: datetime,
    *,
    starts_before: datetime | None = None,
) -> int:
    """How many rows from ``sources`` are still in force for this region right now.

    ``starts_before`` additionally bounds ``valid_from``, which is how the KPLC
    block counts "windows in the next 48 hours" rather than "windows ever".
    """
    stmt = _operator_scoped(
        select(func.count()).select_from(ExternalSignalRow), ExternalSignalRow
    ).where(
        ExternalSignalRow.region_code == region_code,
        ExternalSignalRow.source.in_(tuple(sources)),
        ExternalSignalRow.valid_until > now,
    )
    if starts_before is not None:
        stmt = stmt.where(ExternalSignalRow.valid_from <= starts_before)
    return int(session.scalar(stmt) or 0)


def _weather_block(session: Session, operator_id: str, region_code: str, now: datetime) -> dict[str, Any]:
    """Compose §7.3.3's per-region weather risk — do not recompute it.

    The block the poller stored is reused wholesale; only the framing keys this
    card needs are lifted out of it. ``stale`` defaults to *true* when there is no
    row at all, which is the point of the whole dashboard: never-fetched and
    fetched-and-fine must not look the same.
    """
    enabled = weather_enabled()
    block = weather_risk_for_region(session, operator_id, region_code, now)

    if block is None:
        # No usable forecast has ever been stored for this region. Say why, in the
        # words an operator can act on, rather than leaving a blank tile.
        err = latest_error(session, operator_id, region_code)
        if not enabled:
            reason = "WEATHER_ENABLED is false — the weather poller has never run"
        elif err is not None and err.last_error:
            reason = f"last fetch failed: {err.last_error}"
        else:
            reason = "no forecast stored for this region yet"
        return {
            "available": False,
            "enabled": enabled,
            "stale": True,
            "storm_flag": None,
            "fetched_at": None,
            "age_s": None,
            "last_error": err.last_error if err is not None else None,
            # §10.6 / M6: the measured 30-day precision of past storm warnings,
            # from services/backtest.py. None means "not yet measured" (below the
            # 90-day / 10-episode floor), never zero; a float below min_precision
            # (0.2) is shown greyed as LOW CONFIDENCE, never hidden.
            "precision_30d": signal_precision_30d(session, operator_id, region_code, now=now),
            "reason": reason,
        }

    stale = bool(block.get("stale"))
    if not enabled:
        reason = "WEATHER_ENABLED is false — this reading is frozen, not live"
    elif stale:
        reason = block.get("last_error") or "last reading is past its validity window"
    else:
        reason = None
    return {
        "available": True,
        "enabled": enabled,
        "stale": stale,
        "storm_flag": bool(block.get("storm_flag")),
        "fetched_at": block.get("fetched_at"),
        "age_s": block.get("age_s"),
        "last_error": block.get("last_error"),
        "precision_30d": signal_precision_30d(session, operator_id, region_code, now=now),
        "reason": reason,
    }


def _flood_block(session: Session, operator_id: str, region_code: str, now: datetime) -> dict[str, Any]:
    """GloFAS river flood flag (§7.3.3), aggregated across the region's riverine sites.

    The poller writes one row per site, all stamped with the run's time, so "the newest
    GLOFAS row in the region" used to be settled by a tie-break: one calm site, or one
    site's failure marker, could hide another site's live flood (review finding F09).
    ``flood_region_state`` takes each site's current reading instead: ``flag`` if ANY
    fresh one flags, fresh if ANY site has a fresh reading, ``flag`` None with no reading.
    """
    state = flood_region_state(session, operator_id, region_code, now)
    return {
        "available": state["available"],
        "stale": state["stale"],
        "flag": state["flag"],
        "fetched_at": state["fetched_at"],
    }


def _cap_block(session: Session, operator_id: str, region_code: str, now: datetime) -> dict[str, Any]:
    """KMD CAP warnings in force (§7.3.3), with freshness taken from the FEED, never an alert.

    ``stale`` and ``fetched_at`` come from ``cap_feed_health``, which judges the feed-health
    row against now: a poller that stopped running reads stale, never the "ok" it last
    wrote (review finding F02). They used to come from whichever KMD_CAP row sorted newest,
    so a warning ARRIVING made the block fresh and let the tile read CALM — and it stayed
    fresh after polling stopped (review finding F03). A warning must never make a region
    look calmer; a Severe/Extreme one lifts it to WATCH instead (``_status``).
    ``count`` is the warnings still inside KMD's own validity window: an expired warning
    is history, not a warning.
    """
    health = cap_feed_health(session, operator_id, region_code, now)
    return {
        "available": health["available"],
        "stale": health["stale"],
        "count": health["alerts_in_force"],
        "fetched_at": health["fetched_at"],
    }


def _kplc_block(session: Session, region_code: str, now: datetime) -> dict[str, Any]:
    """KPLC planned interruptions starting within 48 h (§5.3.14). Read from
    ``external_signals`` rather than from a new table — this lane adds none."""
    row = _latest_signal_row(session, region_code, _KPLC_SOURCES)
    if row is None:
        return {"available": False, "stale": True, "windows_next_48h": 0, "fetched_at": None}
    return {
        "available": True,
        "stale": is_stale(row, now),
        "windows_next_48h": _count_live(
            session, region_code, _KPLC_SOURCES, now, starts_before=now + _KPLC_HORIZON
        ),
        "fetched_at": _z(row.fetched_at),
    }


# ----------------------------------------------------------------- region rollups


def _status(
    open_by_priority: dict[str, int],
    sla_breached: int,
    signals: dict[str, dict[str, Any]],
    any_signal_fresh: bool,
    cap_severe_in_force: bool = False,
) -> str:
    """The tile's colour, as a four-rung ladder evaluated worst-first.

    * ``ALERT`` — an open P1, or a live storm/flood flag. Either is a reason to
      wake someone.
    * ``WATCH`` — an open P2, any open incident already past its restore SLA, or a
      KMD CAP warning of severity Severe or Extreme in force for the region.
    * ``STALE`` — none of the above fired **and** no external signal for this
      region is fresh. We are blind here. That is not the same as calm, and a
      wallboard that paints it green is worse than no wallboard: it converts a
      dead poller, a mis-mapped region or a silent alarm feed into reassurance.
      Note this outranks ``CALM`` even when P3/P4 incidents are open — those are
      already visible in ``open_by_priority``; what this rung is asserting is only
      that we cannot vouch for the region.
    * ``CALM`` — nothing above, and at least one signal is fresh: we looked.
    """
    weather = signals["weather"]
    flood = signals["flood"]
    storm_live = bool(weather.get("storm_flag")) and not weather.get("stale")
    flood_live = bool(flood.get("flag")) and not flood.get("stale")

    if open_by_priority.get("P1", 0) > 0 or storm_live or flood_live:
        return "ALERT"
    if open_by_priority.get("P2", 0) > 0 or sla_breached > 0:
        return "WATCH"
    # PRODUCT DEFAULT — the product owner may change it. A KMD CAP alert IN FORCE with
    # severity Severe or Extreme lifts the region to at least WATCH, never to ALERT.
    # "Silence is not good news" cuts both ways: a published Met Department warning must
    # not coexist with a CALM tile. But ALERT stays reserved for what this NOC itself
    # knows is live — our own P1s and fresh storm/flood flags — because a county-level
    # warning, possibly days long and for part of a region, is not a reason to wake
    # someone on its own. "In force" is KMD's expiry, deliberately regardless of whether
    # our feed is reachable: a warning KMD issued does not lapse because we lost the link.
    if cap_severe_in_force:
        return "WATCH"
    if not any_signal_fresh:
        return "STALE"
    return "CALM"


def _problem_cards(session: Session, region_code: str) -> tuple[list[dict[str, Any]], int]:
    """Open problems for a region, worst-recurring first, plus the untruncated count.

    Ordered by ``occurrence_count`` then ``last_seen``: the PRB that has bitten
    four times is the one worth a card, not the one opened most recently.
    ``known_error`` is surfaced because an operator seeing a repeat fault wants to
    know in one glance whether a workaround already exists (§7.7.1).
    """
    base = _owned(ProblemRow).where(
        ProblemRow.region_code == region_code,
        ProblemRow.status.in_(("OPEN", "MONITORING")),
    )
    total = int(
        session.scalar(
            _operator_scoped(select(func.count()).select_from(ProblemRow), ProblemRow).where(
                ProblemRow.region_code == region_code,
                ProblemRow.status.in_(("OPEN", "MONITORING")),
            )
        )
        or 0
    )
    rows = session.scalars(
        base.order_by(ProblemRow.occurrence_count.desc(), ProblemRow.last_seen.desc()).limit(
            PROBLEMS_PER_REGION
        )
    ).all()
    cards = [
        {
            "problem_number": row.problem_number,
            "site_id": row.site_id,
            "occurrence_count": int(row.occurrence_count or 0),
            "last_seen": _z(row.last_seen),
            "known_error": bool(row.is_known_error),
            "status": row.status,
        }
        for row in rows
    ]
    return cards, total


def _incident_rollup(
    session: Session, region_code: str, now: datetime, since: datetime
) -> dict[str, Any]:
    """Open counts by priority, SLA breaches, and the 30-day repeat-fault rate.

    Two passes over two different row sets on purpose: the priority counters are
    about what is open *now* (any age), while the repeat rate is about what
    happened in the window (open or closed). Collapsing them into one query would
    have to pick one of those meanings and would silently answer the other.
    """
    open_rows = session.scalars(
        _owned(IncidentRow).where(
            IncidentRow.region_code == region_code,
            IncidentRow.status.not_in(_NOT_OPEN),
        )
    ).all()

    by_priority = {p: 0 for p in _PRIORITIES}
    sla_breached = 0
    for row in open_rows:
        # ``.get`` guard, not ``by_priority[row.priority]``: a row carrying a
        # priority outside P1-P4 (a bad import, a future band) must not 500 the
        # whole wallboard. It is counted in ``open_total`` either way.
        if row.priority in by_priority:
            by_priority[row.priority] += 1
        if row.sla_restore_due and row.sla_restore_due < now:
            sla_breached += 1

    window_rows = session.scalars(
        _owned(IncidentRow).where(
            IncidentRow.region_code == region_code,
            IncidentRow.created_at >= since,
        )
    ).all()
    repeats = sum(1 for row in window_rows if (row.recurrence_count or 1) > 1)
    # ``None``, not 0.0, when nothing happened in the window: "0 % of no faults
    # repeated" is a measurement nobody made, and a 0 on the card would read as a
    # clean bill of health for a region that may simply not be reporting.
    rate = round(repeats / len(window_rows), 4) if window_rows else None

    return {
        "open_total": len(open_rows),
        "open_by_priority": by_priority,
        "sla_breached": sla_breached,
        "incidents_30d": len(window_rows),
        "repeat_faults_30d": repeats,
        "repeat_fault_rate_30d": rate,
    }


def _region_row(
    session: Session,
    *,
    region_code: str,
    region_cfg: Any,
    operator_id: str,
    now: datetime,
    since: datetime,
    complaint_surge: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One region card. Key order here is the contract the frontend reads."""
    signals = {
        "weather": _weather_block(session, operator_id, region_code, now),
        "flood": _flood_block(session, operator_id, region_code, now),
        "cap": _cap_block(session, operator_id, region_code, now),
        "kplc": _kplc_block(session, region_code, now),
    }
    # "Fresh" means at least one outside-world source answered recently. Our own
    # incident table does not count: it is evidence that alarms arrived, never
    # evidence that none were missed.
    any_fresh = any(b.get("available") and not b.get("stale") for b in signals.values())

    rollup = _incident_rollup(session, region_code, now, since)
    problems, problems_total = _problem_cards(session, region_code)

    return {
        "region_code": region_code,
        "label": getattr(region_cfg, "label", region_code),
        "counties": list(getattr(region_cfg, "counties", []) or []),
        "rnio": getattr(region_cfg, "rnio", None),
        "status": _status(
            rollup["open_by_priority"],
            rollup["sla_breached"],
            signals,
            any_fresh,
            cap_severe_in_force(session, operator_id, region_code, now) > 0,
        ),
        "signals_stale": not any_fresh,
        "open_total": rollup["open_total"],
        "open_by_priority": rollup["open_by_priority"],
        "sla_breached": rollup["sla_breached"],
        "problems_open": problems,
        "problems_open_total": problems_total,
        "incidents_30d": rollup["incidents_30d"],
        "repeat_faults_30d": rollup["repeat_faults_30d"],
        "repeat_fault_rate_30d": rollup["repeat_fault_rate_30d"],
        "signals": signals,
        # The region's open surge of customer complaints (docs/CLOSE_THE_LOOP.md section 3):
        # ``{surge_id, place, complaints, numbers, first_at, last_at, card_id}``, else null.
        # These are FIRST-PARTY complaints -- customers telling us, through our own complaint
        # form, about their own service -- not social-media posts, so they need no new notice;
        # §7.4's Phase 6 social lane (which does need the DPIA and a transparency notice) is a
        # different source and is still not built. Counts and places only, never a number.
        "complaint_surge": complaint_surge,
        "regulatory_baseline": regulatory_baseline_for(region_code, operator_id),
    }


def _complaint_surges(session: Session, operator_id: str) -> dict[str, dict[str, Any]]:
    """Each region's open complaint surge, from the support desk -- or none at all while the desk
    is switched off. A support failure must never take the Regions dashboard down with it: it
    reads as "no surge" and is logged."""
    from noc_agents.support.context import support_desk_enabled  # the support lane is optional here

    if not support_desk_enabled():
        return {}
    try:
        from noc_agents.support.surge import region_surges

        return region_surges(session, operator_id)
    except Exception:  # noqa: BLE001
        log.exception("regions dashboard: complaint surges unavailable")
        return {}


def regions_dashboard(session: Session, now: datetime | None = None) -> dict[str, Any]:
    """The ``GET /api/v1/dashboard/regions`` payload (§7.4.2).

    Rows are sorted by ``region_code``, not by severity and not by the profile
    YAML's key order. Severity ordering would make tiles jump position whenever an
    incident opened, which is exactly wrong on a screen someone watches for eight
    hours and navigates by muscle memory; YAML order would make a harmless config
    reshuffle move the wallboard. Alphabetical is boring, and boring is the point.
    """
    now = now or utcnow()
    since = now - timedelta(days=ROLLUP_WINDOW_DAYS)
    cfg = _settings().operator
    surges = _complaint_surges(session, cfg.operator_id)

    regions = [
        _region_row(
            session,
            region_code=code,
            region_cfg=cfg.regions[code],
            operator_id=cfg.operator_id,
            now=now,
            since=since,
            complaint_surge=surges.get(code),
        )
        for code in sorted(cfg.regions)
    ]
    return {
        "generated_at": _z(now),
        "operator_id": cfg.operator_id,
        "window_days": ROLLUP_WINDOW_DAYS,
        # Reported at the top so a reader can tell "every region is STALE" apart
        # from "the poller is off", without inspecting six identical reasons.
        "weather_enabled": weather_enabled(),
        "regions": regions,
    }
