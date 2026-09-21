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


# ===========================================================================================
# PART TWO — the rest of the external-signal read side (§7.3.2; CONFORMANCE C-12..C-15)
# ===========================================================================================
#
# Everything below was added by the CAP / flood / backtest lane. It obeys the same rule as
# the five functions above, for the same reason: **no adapter, no poller, no httpx**. The
# county gazetteer and the county→region map are configuration reads (YAML that ``config.py``
# has already parsed); the row readers are indexed SELECTs. ``tests/unit/test_hot_path_imports.py``
# imports this module in a fresh interpreter and asserts ``httpx`` never lands in
# ``sys.modules``, so a convenience import of ``adapters/kmd_cap.py`` here would fail the
# suite, not merely offend a guideline.

import re  # noqa: E402  (part-two imports kept beside the code that needs them)
import unicodedata  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from datetime import timedelta  # noqa: E402
from typing import Iterable, Sequence  # noqa: E402

from sqlalchemy import func  # noqa: E402

#: The non-weather ``external_signals.source`` values this lane writes (§7.3.1's vocabulary).
CAP_SOURCE = "KMD_CAP"
FLOOD_SOURCE = "GLOFAS"

#: Every source §7.3.1 enumerates. ``GET /api/v1/signals?source=`` validates against this, so
#: a typo answers 422 instead of returning an empty list — and an empty list from a misspelt
#: filter reads exactly like "no signals", which is the one thing this whole lane exists to
#: stop anybody believing.
SIGNAL_SOURCES: tuple[str, ...] = WEATHER_SOURCES + (CAP_SOURCE, FLOOD_SOURCE, "KPLC", "COMPLAINTS")

#: ``derived_json["kind"]`` values, so a reader can tell the row shapes apart without guessing
#: from which columns happen to be null.
KIND_CAP_ALERT = "cap_alert"        # one Met Department warning, valid until ITS OWN expiry
KIND_CAP_FEED = "cap_feed_health"   # "we reached (or failed to reach) meteo.go.ke at T"
KIND_FLOOD = "river_discharge"      # one GloFAS reading for one riverine site
KIND_CAP_TOMBSTONE = "cap_tombstone"  # "KMD ended <identifier>" seen before the alert itself

#: The feed states a CAP feed-health row can record. Exactly one of them means "we looked and
#: KMD is not warning": ``ok``. Every other state is a reason a count of zero means nothing.
#: ``incomplete``: the feed answered but some CAP documents it lists could not be read (or were
#: deferred), so an alert may be missing. ``not_polled_recently`` is never stored; it is what
#: :func:`cap_feed_health` reports when the newest row is older than :data:`CAP_FEED_MAX_SILENCE`.
CAP_FEED_STATES: tuple[str, ...] = (
    "ok", "stale_feed", "incomplete", "unreachable", "misconfigured", "unmapped", "not_polled_recently",
)

#: The KMD CAP poll cadence. Spelled here, not imported from ``pollers/kmd_cap.py`` (which
#: would load httpx into the hot path); ``tests/unit/test_kmd_cap.py`` pins that they agree.
CAP_POLL_INTERVAL_S = 1800

#: How long after the last poll ATTEMPT a feed-health row may still be believed: two missed
#: polls plus ten minutes of grace. Past it the lane says ``not_polled_recently`` — the poller
#: has stopped (flag switched off, scheduler dead, job wedged), and a stored ``ok`` from the
#: last run it managed is not evidence of anything now (review finding F02).
CAP_FEED_MAX_SILENCE = timedelta(seconds=2 * CAP_POLL_INTERVAL_S + 600)


# --------------------------------------------------------------------- county gazetteer
#
# WHAT THIS IS, AND WHAT IT IS NOT.
#
# It is **not** a second county→region mapping. That mapping is derived, once, from the
# operator profile (``config/operators/*.yaml``, ``regions.<CODE>.counties``) by
# :func:`county_region_map` below: a county is tied to a region in exactly one place and it
# is the profile.
#
# This is a *gazetteer*: the 47 counties created by the Constitution of Kenya 2010, First
# Schedule. Without it, "unknown county" is undetectable — a profile that says ``Kiamb`` or
# ``Homa Baye`` builds a perfectly well-formed reverse map that simply never matches a CAP
# alert, and that failure is silent for as long as nobody notices one region never receives a
# warning. Comparing the profile against the gazetteer is what turns that into something
# somebody sees (§7.3.7: "county→region mapping rejects unknown counties at startup").
#
# Each entry is ``(canonical, *other accepted spellings)``. Alternates are listed because
# three documents spell these three ways and none of them is wrong: the Constitution writes
# "Nairobi City" and "Elgeyo/Marakwet", the operator profiles here write "Nairobi" and
# "Homabay", and a KMD CAP ``areaDesc`` carries whatever the duty forecaster typed. Matching
# runs through :func:`normalise_county` (case, accents, punctuation and spacing removed), so
# only genuinely different *words* have to be listed.
COUNTY_GAZETTEER: tuple[tuple[str, ...], ...] = (
    ("Mombasa",), ("Kwale",), ("Kilifi",), ("Tana River",), ("Lamu",), ("Taita-Taveta",),
    ("Garissa",), ("Wajir",), ("Mandera",), ("Marsabit",), ("Isiolo",), ("Meru",),
    ("Tharaka-Nithi", "Tharaka Nithi"), ("Embu",), ("Kitui",), ("Machakos",), ("Makueni",),
    ("Nyandarua",), ("Nyeri",), ("Kirinyaga",), ("Murang'a", "Muranga"), ("Kiambu",),
    ("Turkana",), ("West Pokot",), ("Samburu",), ("Trans-Nzoia", "Trans Nzoia"),
    ("Uasin Gishu",), ("Elgeyo-Marakwet", "Elgeyo/Marakwet", "Keiyo-Marakwet"), ("Nandi",),
    ("Baringo",), ("Laikipia",), ("Nakuru",), ("Narok",), ("Kajiado",), ("Kericho",),
    ("Bomet",), ("Kakamega",), ("Vihiga",), ("Bungoma",), ("Busia",), ("Siaya",),
    ("Kisumu",), ("Homa Bay", "Homabay"), ("Migori",), ("Kisii",), ("Nyamira",),
    ("Nairobi", "Nairobi City"),
)

#: Trailing words a CAP ``areaDesc`` or a profile may append without changing which county is
#: meant. Stripped before matching; never stripped from what is *stored*.
_COUNTY_SUFFIXES: tuple[str, ...] = ("sub county", "subcounty", "counties", "county")  # longest first

#: Punctuation that may trail or lead an ``areaDesc`` piece ("Mombasa County.", "(Kilifi").
_EDGE_PUNCT = " \t.,;:!?()[]{}\"'-"

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalise_county(name: str | None) -> str:
    """A county name reduced to the key that two spellings of it share.

    Accents folded, case dropped, a trailing "County" removed, every non-alphanumeric
    character dropped. ``"Murang'a"``, ``"MURANGA"`` and ``"Murang’a County"`` all become
    ``"muranga"``; ``"Homa Bay"`` and ``"Homabay"`` both become ``"homabay"``.

    Deliberately lossy and deliberately **never** stored: what gets written to
    ``external_signals.county`` is the canonical spelling, or — for a county nobody
    recognises — the Met Department's own words. This is a comparison key, not a name.
    """
    if not name:
        return ""
    folded = unicodedata.normalize("NFKD", str(name))
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch)).lower()
    folded = folded.replace("’", "'")
    # Whitespace collapsed and edge punctuation trimmed BEFORE the suffix test, so "Mombasa
    # County." and "Mombasa\nCounty" lose their suffix like "Mombasa County" does (review
    # finding F17: the suffix used to be tested first, leaving the key "mombasacounty").
    folded = re.sub(r"\s+", " ", folded).strip(_EDGE_PUNCT)
    for suffix in _COUNTY_SUFFIXES:
        if folded.endswith(" " + suffix):
            folded = folded[: -(len(suffix) + 1)]
            break
    return _NON_ALNUM.sub("", folded)


def _build_gazetteer() -> dict[str, str]:
    index: dict[str, str] = {}
    for spellings in COUNTY_GAZETTEER:
        for spelling in spellings:
            index[normalise_county(spelling)] = spellings[0]
    return index


#: ``normalised spelling -> canonical county name``. Built once; the gazetteer is a constant.
_GAZETTEER: dict[str, str] = _build_gazetteer()


def canonical_county(name: str | None) -> str | None:
    """The gazetted county ``name`` refers to, or ``None`` when it is not a Kenyan county."""
    return _GAZETTEER.get(normalise_county(name))


def county_key(name: str | None) -> str:
    """The key :func:`county_region_map` is indexed by: the CANONICAL county's normalised name.

    Keying by canonical county, not by whichever spelling the profile happens to use, is what
    lets KMD write "Nairobi City County" (the Constitution's spelling) against a profile that
    writes "Nairobi", or a profile write "Elgeyo/Marakwet" against KMD's "Elgeyo-Marakwet"
    (review finding F04: the map used to be keyed by the profile's spelling, so a gazetted
    alias was recognised as a county and then silently attributed to no region). A name that
    is not a county keeps its own normalised form, so it simply matches nothing.
    """
    return normalise_county(canonical_county(name) or name)


def all_counties() -> tuple[str, ...]:
    """The 47 canonical county names, in First Schedule order."""
    return tuple(spellings[0] for spellings in COUNTY_GAZETTEER)


# --------------------------------------------------------------------- county -> region


def _regions_cfg(cfg: Any | None = None) -> dict[str, Any]:
    """``{region_code: region_cfg}`` from the active operator profile, or from ``cfg``."""
    if cfg is None:
        from noc_agents.config import get_settings  # local: no I/O, and keeps the import graph flat

        cfg = get_settings().operator
    regions = getattr(cfg, "regions", None)
    return dict(regions) if isinstance(regions, dict) else {}


def county_region_map(cfg: Any | None = None) -> dict[str, tuple[str, ...]]:
    """The reverse of the operator profile: ``{normalised county -> (REGION, ...)}``.

    **One-to-many on purpose, and that is the interesting part.** The illustrative
    ``county_to_region`` in §7.3.1 is one-to-one, but the profiles this system actually runs
    on are not: Safaricom's own regions list ``Nairobi`` under both ``NBI_E`` and ``NBI_W``,
    and ``Kiambu`` under ``NBI_E``, ``NBI_W`` *and* ``MTK``. That is not a configuration
    error — a county is an administrative boundary, a NOC region is an operational one, and
    they genuinely overlap. A map that forced a single answer would have to pick a region to
    silently drop a Met Department warning for, so this one returns all of them and the CAP
    poller writes one row per region.

    Keys are :func:`county_key` keys — the canonical county, normalised — so neither the
    feed's spelling nor the profile's has to match the other's, only the gazetteer. Values are
    sorted, so the row order a poller produces is stable run to run.
    """
    out: dict[str, set[str]] = {}
    for code, region in _regions_cfg(cfg).items():
        for county in getattr(region, "counties", None) or []:
            key = county_key(county)
            if key:
                out.setdefault(key, set()).add(str(code))
    return {key: tuple(sorted(codes)) for key, codes in sorted(out.items())}


def regions_for_county(county: str | None, cfg: Any | None = None) -> tuple[str, ...]:
    """Every region of the active operator that covers ``county``; ``()`` when none does.

    ``()`` is a normal answer, not an error. The Met Department warns for all 47 counties and
    no profile here covers 47 — Turkana has no Safaricom region in the demo profile at all.
    The poller stores such an alert with ``region_code=NULL`` rather than discarding it: what
    KMD said is a fact whether or not we have a region for it — and ``pollers/kmd_cap.py``
    re-attributes every alert still in force against the current map on each poll, so a later
    profile change lights it up without a re-fetch.
    """
    return county_region_map(cfg).get(county_key(county), ())


@dataclass(frozen=True)
class CountyMapProblem:
    """One thing wrong with the profile's county lists. ``fatal`` decides how loud it is."""

    kind: str  # unknown_county | region_without_counties
    region_code: str
    county: str | None
    message: str

    @property
    def fatal(self) -> bool:
        """An unknown county is a typo somebody must fix. An empty county list is a gap.

        A gap is real information — Airtel's profile maps only ``NBI``; its other seven
        regions carry no counties at all, so no CAP warning can ever reach them — but it is
        not a defect in the sense of "this mapping is wrong". It is a mapping nobody has
        written yet, and treating it as fatal would mean this lane can never run on that
        profile at all. Reported, never fatal.
        """
        return self.kind == "unknown_county"

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "region_code": self.region_code,
            "county": self.county,
            "message": self.message,
            "fatal": self.fatal,
        }


def validate_county_map(cfg: Any | None = None) -> list[CountyMapProblem]:
    """Check every county on the profile against the gazetteer (§7.3.7).

    Pure: it returns problems and raises nothing. Turning a problem into a log line, a
    refused poll or a red test is the caller's decision, and the three callers deliberately
    make three different ones — ``api/routers/signals.py`` carries the argument.
    """
    problems: list[CountyMapProblem] = []
    for code, region in sorted(_regions_cfg(cfg).items()):
        counties = list(getattr(region, "counties", None) or [])
        if not counties:
            problems.append(
                CountyMapProblem(
                    "region_without_counties",
                    str(code),
                    None,
                    f"region {code} lists no counties, so no KMD CAP warning can ever be "
                    f"attributed to it: the region is invisible to the early-warning lane",
                )
            )
            continue
        for county in counties:
            if canonical_county(county) is None:
                problems.append(
                    CountyMapProblem(
                        "unknown_county",
                        str(code),
                        str(county),
                        f"region {code} lists county {county!r}, which is not one of Kenya's "
                        f"47 counties (Constitution of Kenya 2010, First Schedule). No CAP "
                        f"warning can ever match it — check the spelling in "
                        f"config/operators/<profile>.yaml",
                    )
                )
    return problems


def county_map_report(cfg: Any | None = None) -> dict[str, Any]:
    """What ``GET /api/v1/signals/county-map`` returns: the map, the gaps and the typos."""
    mapping = county_region_map(cfg)
    problems = validate_county_map(cfg)
    covered = {code for codes in mapping.values() for code in codes}
    regions = _regions_cfg(cfg)
    return {
        "counties": {
            (canonical_county(key) or key): list(codes) for key, codes in mapping.items()
        },
        "regions_covered": sorted(covered),
        "regions_without_counties": sorted(set(regions) - covered),
        "problems": [p.as_dict() for p in problems],
        "ok": not any(p.fatal for p in problems),
        "gazetteer_size": len(COUNTY_GAZETTEER),
    }


# --------------------------------------------------------------------- generic row reads


def _decode(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        block = json.loads(raw)
    except ValueError:
        return None
    return block if isinstance(block, dict) else None


def iso_z(value: datetime | None) -> str | None:
    """Naive-UTC storage value → ``"2026-09-18T12:00:00Z"``, seconds precision.

    Same spelling as ``services/dashboards.py`` and ``api/serializers.py``: a naive ISO string
    is read by a browser as LOCAL time, which in Nairobi backdates everything by three hours
    (defect #41).
    """
    return value.replace(microsecond=0).isoformat() + "Z" if value else None


def signal_out(row: ExternalSignalRow, now: datetime | None = None) -> dict[str, Any]:
    """One row as §7.3.2 describes it: "rows with ``stale``, ``valid_until``, flags".

    ``payload_json`` is **not** on the wire. It is the provider's entire response body — a
    48-hour forecast, or a full CAP document — and a list endpoint that inlines it turns a
    50-row page into megabytes. ``derived`` carries the block a screen actually reads, and the
    single-row payload stays reachable through the database for the backtest. ``stale`` is
    recomputed against *now* by :func:`is_stale`, never taken from the stored column alone.
    """
    now = now or utcnow()
    return {
        "id": row.id,
        "source": row.source,
        "source_url": row.source_url,
        "region_code": row.region_code,
        "county": row.county,
        "site_id": row.site_id,
        "external_id": row.external_id,
        "fetched_at": iso_z(row.fetched_at),
        "valid_from": iso_z(row.valid_from),
        "valid_until": iso_z(row.valid_until),
        "stale": is_stale(row, now),
        "age_s": max(0, int((now - row.fetched_at).total_seconds())) if row.fetched_at else None,
        "confidence": float(row.confidence if row.confidence is not None else 1.0),
        "storm_flag": bool(row.storm_flag),
        "flood_flag": bool(row.flood_flag),
        "planned_power": bool(row.planned_power),
        "access_risk": bool(row.access_risk),
        "last_error": row.last_error,
        "derived": _decode(row.derived_json),
    }


def list_signals(
    session: Session,
    operator_id: str,
    *,
    source: str | None = None,
    region_code: str | None = None,
    active: bool | None = None,
    site_id: str | None = None,
    now: datetime | None = None,
    limit: int = 200,
) -> list[ExternalSignalRow]:
    """``GET /api/v1/signals?source=&region_code=&active=`` (§7.3.2), newest first.

    ``active=True`` means **in force by the source's own validity**: ``valid_until > now``.
    That is deliberately the same definition ``services/dashboards._count_live`` uses, so the
    list and the tile never disagree about how many warnings there are. It deliberately does
    NOT also require ``stale == 0``: while the KMD feed is unreachable its live alerts are
    marked stale (we cannot see a cancellation), and hiding a warning the Met Department
    issued, and never withdrew, because *our* link to them is down would be exactly backwards.
    Staleness is reported on every row instead, recomputed against now. ``active=False`` is
    the complement (expired), ``active=None`` returns both.

    Operator scoping is the first clause of the WHERE and not a check after the fact: both
    operators' rows live in one file (``api/deps`` explains why at length), so isolation is a
    property of this query and of every query like it.
    """
    now = now or utcnow()
    stmt = select(ExternalSignalRow).where(ExternalSignalRow.operator_id == operator_id)
    if source is not None:
        stmt = stmt.where(ExternalSignalRow.source == source)
    if region_code is not None:
        stmt = stmt.where(ExternalSignalRow.region_code == region_code)
    if site_id is not None:
        stmt = stmt.where(ExternalSignalRow.site_id == site_id)
    if active is True:
        stmt = stmt.where(ExternalSignalRow.valid_until > now)
    elif active is False:
        stmt = stmt.where(ExternalSignalRow.valid_until <= now)
    stmt = stmt.order_by(
        ExternalSignalRow.fetched_at.desc(), ExternalSignalRow.created_at.desc()
    ).limit(max(1, min(int(limit), 1000)))
    return list(session.scalars(stmt).all())


def latest_row(
    session: Session,
    operator_id: str,
    *,
    source: str,
    region_code: str | None = None,
    site_id: str | None = None,
    kind: str | None = None,
) -> ExternalSignalRow | None:
    """The newest row for one source, optionally narrowed to a region, a site or a ``kind``."""
    stmt = select(ExternalSignalRow).where(
        ExternalSignalRow.operator_id == operator_id,
        ExternalSignalRow.source == source,
    )
    if region_code is not None:
        stmt = stmt.where(ExternalSignalRow.region_code == region_code)
    if site_id is not None:
        stmt = stmt.where(ExternalSignalRow.site_id == site_id)
    if kind is not None:
        # ``kind`` lives inside derived_json. A LIKE over the compact separators the pollers
        # write picks it out without a JSON function: SQLite builds vary on whether JSON1 is
        # compiled in, and this lane is not the place to find that out in production.
        stmt = stmt.where(ExternalSignalRow.derived_json.like(f'%"kind":"{kind}"%'))
    stmt = stmt.order_by(
        ExternalSignalRow.fetched_at.desc(), ExternalSignalRow.created_at.desc()
    ).limit(1)
    return session.scalars(stmt).first()


def active_signals_count(
    session: Session,
    operator_id: str,
    *,
    source: str,
    region_code: str | None = None,
    now: datetime | None = None,
    kind: str | None = None,
    storm_only: bool = False,
) -> int:
    """How many rows from ``source`` are still in force for a region right now.

    Same predicate as ``services/dashboards._count_live`` (``valid_until > now``), so the API
    and the Regions tile report the same number. CAP feed-health rows are written already
    expired and therefore never counted as warnings.
    """
    now = now or utcnow()
    stmt = (
        select(func.count())
        .select_from(ExternalSignalRow)
        .where(
            ExternalSignalRow.operator_id == operator_id,
            ExternalSignalRow.source == source,
            ExternalSignalRow.valid_until > now,
        )
    )
    if region_code is not None:
        stmt = stmt.where(ExternalSignalRow.region_code == region_code)
    if kind is not None:
        stmt = stmt.where(ExternalSignalRow.derived_json.like(f'%"kind":"{kind}"%'))
    if storm_only:
        stmt = stmt.where(ExternalSignalRow.storm_flag == 1)
    return int(session.scalar(stmt) or 0)


# --------------------------------------------------------------------- CAP-specific reads


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value[:-1] if value.endswith("Z") else value)
    except ValueError:
        return None


def cap_feed_health(
    session: Session,
    operator_id: str,
    region_code: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """**Silence is not good news.** What the CAP lane knows, said so it cannot be misread.

    A naive "count the alerts in force" read collapses very different states into the single
    number zero. This one keeps them apart, and **judges every one of them against now** —
    never by trusting a state string the poller wrote on its last run:

    * ``never_polled`` — the job has never run for this scope. We have no idea whether a
      severe-weather warning is in force anywhere in Kenya.
    * ``not_polled_recently`` — the newest feed-health row's poll attempt is older than
      :data:`CAP_FEED_MAX_SILENCE`. ``WEATHER_ENABLED`` was switched off, the scheduler died or
      the job wedged. Whatever the row said then, it says nothing now (review finding F02: a
      stored ``ok`` used to read ``ok`` forever once the poller stopped).
    * ``unreachable`` — the poller ran and could not reach meteo.go.ke; ``last_error`` says why.
    * ``stale_feed`` — every fetch succeeded but the newest item in the feed is older than
      ``CAP_STALE_DAYS``, **recomputed from** ``newest_sent`` **against now**. Not hypothetical:
      §7.3 records the KMD feed as 132 days stale on the day it was checked.
    * ``incomplete`` — the feed answered, but CAP documents it lists could not be read or were
      deferred; an alert may be missing, so a zero is not a zero.
    * ``misconfigured`` / ``unmapped`` — the profile names a county that does not exist (the
      poller refused to attribute anything), or this region lists no counties at all.
    * ``ok`` — polled recently, feed current, every listed document read. Only in this state
      does "no alerts in force" mean "the Met Department is not warning about anything".

    ``stale`` is ``state != "ok"``, and it is the ONLY freshness the Regions dashboard's CAP
    block uses (review finding F03): an alert row arriving can never make a region look calmer.
    """
    now = now or utcnow()
    row = latest_row(
        session, operator_id, source=CAP_SOURCE, region_code=region_code, kind=KIND_CAP_FEED
    )
    alerts = active_signals_count(
        session, operator_id, source=CAP_SOURCE, region_code=region_code, now=now, kind=KIND_CAP_ALERT
    )
    if row is None:
        return {
            "state": "never_polled",
            "available": False,
            "stale": True,
            "alerts_in_force": alerts,
            "fetched_at": None,
            "attempted_at": None,
            "newest_sent": None,
            "feed_age_days": None,
            "cap_stale_days": None,
            "last_error": None,
            "reason": "the KMD CAP poller has never stored a reading for this scope; nothing "
            "here says whether a warning is in force",
        }
    block = _decode(row.derived_json) or {}
    stored = block.get("state")
    if stored not in CAP_FEED_STATES:
        # A row written before ``state`` was stored, or a hand-edited one: derive it from the
        # two facts every feed-health row carries, and never default to "ok".
        if not block.get("reachable"):
            stored = "unreachable"
        elif block.get("feed_stale", True):
            stored = "stale_feed"
        else:
            stored = "ok"
    state, reason = stored, block.get("reason")

    stale_days = block.get("cap_stale_days")
    newest = _parse_iso(block.get("newest_sent"))
    feed_age_days = round((now - newest).total_seconds() / 86400.0, 2) if newest else block.get("feed_age_days")
    if state == "ok" and isinstance(stale_days, (int, float)) and feed_age_days is not None and feed_age_days > stale_days:
        state = "stale_feed"
        reason = (
            f"the newest item in the KMD feed is now {feed_age_days:g} days old (CAP_STALE_DAYS={stale_days}); "
            "the feed was current when last read, and has not been shown to be since"
        )
    attempted = _parse_iso(block.get("attempted_at")) or row.fetched_at
    if attempted is not None and now - attempted > CAP_FEED_MAX_SILENCE:
        silent_min = int((now - attempted).total_seconds() // 60)
        reason = (
            f"the KMD CAP poller has not run for {silent_min} min (last attempt {iso_z(attempted)}, when the "
            f"feed read '{stored}'); nothing here is current — is WEATHER_ENABLED on and the scheduler alive?"
        )
        state = "not_polled_recently"
    return {
        "state": state,
        "available": True,
        # A feed-health row is written already expired (pollers/kmd_cap.py says why), so
        # ``is_stale()`` is unconditionally true for it; the feed's staleness is this.
        "stale": state != "ok",
        "alerts_in_force": alerts,
        "fetched_at": iso_z(row.fetched_at),
        "attempted_at": iso_z(attempted),
        "newest_sent": block.get("newest_sent"),
        "feed_age_days": feed_age_days,
        "cap_stale_days": stale_days,
        "last_error": row.last_error,
        "reason": reason,
    }


def cap_severe_in_force(
    session: Session,
    operator_id: str,
    region_code: str,
    now: datetime | None = None,
) -> int:
    """How many KMD warnings of severity Severe or Extreme are in force for a region now.

    "In force" is KMD's own ``expires`` (``valid_until > now``), deliberately regardless of
    whether our link to KMD is up: a warning the Met Department issued and never withdrew does
    not stop applying because we cannot reach them. ``storm_flag`` on a CAP alert row is set
    exactly for Severe/Extreme (§7.3.1), so it is the column this counts on. What it drives is
    the Regions dashboard's WATCH rung (``services/dashboards._status``).
    """
    return active_signals_count(
        session, operator_id, source=CAP_SOURCE, region_code=region_code, now=now,
        kind=KIND_CAP_ALERT, storm_only=True,
    )


def cap_alerts(
    session: Session,
    operator_id: str,
    region_code: str | None = None,
    *,
    now: datetime | None = None,
    active_only: bool = True,
) -> list[dict[str, Any]]:
    """The Met Department's warnings **as stored** — newest first.

    Nothing here re-reads, re-scores, softens, extends or re-interprets a warning. A CAP
    alert is the Kenya Meteorological Department's statement under its own name; this
    system's only job is to repeat it accurately and to say when it expires, which is
    ``valid_until``, taken verbatim from the document's ``expires``. If a severity looks
    wrong to the floor, the remedy is a conversation with KMD, not an adjustment here.
    """
    now = now or utcnow()
    rows = list_signals(
        session,
        operator_id,
        source=CAP_SOURCE,
        region_code=region_code,
        active=True if active_only else None,
        now=now,
        limit=200,
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        block = _decode(row.derived_json) or {}
        if block.get("kind") != KIND_CAP_ALERT:
            continue  # feed-health rows are not warnings
        item = signal_out(row, now)
        item["alert"] = block
        out.append(item)
    return out


def flood_region_state(
    session: Session,
    operator_id: str,
    region_code: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """A region's flood picture, aggregated across ALL its riverine sites (review finding F09).

    The poller writes one row per site, and every site's rows share the run's timestamp, so
    "the newest GLOFAS row in the region" is decided by a tie-break — and one calm site, or one
    site's failure marker, used to hide another site's live flood flag. Instead:

    * each site's **current** reading is its newest good row (one with a derived block —
      failure markers are not readings);
    * ``flag`` is true if ANY site's current reading flags and is not stale;
    * ``stale`` is false if ANY site has a fresh current reading (somebody looked);
    * ``available`` is true if any GLOFAS row (reading or marker) exists for the region.

    ``flag`` is ``None`` when no site has a current reading at all: we have no reading, not a
    calm one.
    """
    now = now or utcnow()
    good = session.scalars(
        select(ExternalSignalRow)
        .where(
            ExternalSignalRow.operator_id == operator_id,
            ExternalSignalRow.source == FLOOD_SOURCE,
            ExternalSignalRow.region_code == region_code,
            ExternalSignalRow.derived_json.is_not(None),
        )
        .order_by(ExternalSignalRow.fetched_at.desc(), ExternalSignalRow.created_at.desc())
        .limit(500)
    ).all()
    current: dict[str, ExternalSignalRow] = {}
    for row in good:
        current.setdefault(row.site_id or row.id, row)
    # With no reading at all, a failure marker still proves the poller tried: available, stale.
    marker = None if current else latest_row(session, operator_id, source=FLOOD_SOURCE, region_code=region_code)
    sites = [
        {
            "site_id": row.site_id,
            "flood_flag": bool(row.flood_flag),
            "stale": is_stale(row, now),
            "fetched_at": iso_z(row.fetched_at),
            "last_error": row.last_error,
        }
        for _, row in sorted(current.items())
    ]
    fresh = [r for r in current.values() if not is_stale(r, now)]
    newest = max((r.fetched_at for r in current.values()), default=None)
    if newest is None and marker is not None:
        newest = marker.fetched_at
    return {
        "available": bool(current) or marker is not None,
        "stale": not fresh,
        "flag": (any(bool(r.flood_flag) for r in fresh) if current else None),
        "fetched_at": iso_z(newest),
        "sites": sites,
    }


def flood_reading(
    session: Session,
    operator_id: str,
    region_code: str,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """The region's aggregated flood state (:func:`flood_region_state`), or ``None`` if no
    GLOFAS row of any kind has ever been stored for it."""
    state = flood_region_state(session, operator_id, region_code, now)
    return state if state["available"] else None


def signals_summary(
    session: Session,
    operator_id: str,
    regions: Sequence[str] | Iterable[str],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Per-region CAP and flood state for the Wallboard strip. Network-free, like everything
    else in this module: it reads what the out-of-band pollers wrote and nothing else."""
    now = now or utcnow()
    return {
        code: {
            "cap": cap_feed_health(session, operator_id, code, now),
            "flood": flood_reading(session, operator_id, code, now),
        }
        for code in regions
    }
