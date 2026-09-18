"""ENRICH: site/region CMDB lookup, user estimate and TT classification.

SITE CATALOGUE WIRING (spec §5.3.3 "Change (P1)", §7.0.7)
---------------------------------------------------------
This node is the one place in the running pipeline that reads
``services/sites.py`` — the catalogue behind ``data/seed/safaricom_sites.json``
that Phase 1 backfilled with ``ward, lat, lon, parent_hub_id, site_class,
riverine, kplc_region, kplc_area_hints``. Until this wave nothing imported it,
so none of that data was reachable from ``noc_agents.main`` and §3.5's note
("ENRICH does not read it") still held. It now does.

WHERE THE CATALOGUE DATA LANDS, AND WHY NOWHERE ELSE
The ENRICH step row (``output_summary``, ``rationale``, ``tools_called``,
``confidence``) and the five state fields this node has always written
(``users``, ``site_name``, ``county``, ``is_hub``, ``tt``) are pinned literally
by ``tests/integration/test_golden_sequence.py``. So none of them is recomputed
from the catalogue. The catalogue goes to exactly one new place: the context
block on the state (:data:`CONTEXT_ATTR`), under :data:`SITE_KEY` — the
JSON-safe mapping destined for ``IncidentRow.context_json``. §5.3.3's Phase 3
lanes add ``weather_risk`` / ``planned_power`` as sibling keys of the same dict.

    NOTE (reported, not guessed): ENRICH is node 3 and TICKET — which creates
    the ``IncidentRow`` — is node 5, so there is no incident row to write
    ``context_json`` on while this node runs. Persisting the block is one
    ``context_json=json.dumps(state.context)`` in ``agents/ticket.py`` plus one
    declared ``context: dict`` field on ``IncidentState`` in
    ``orchestrator/contract.py``. Both files belong to other waves, so this node
    carries the block on the state and stops there.

FAIL-SOFT LOOKUP ON A FAIL-CLOSED NODE
EnrichmentAgent is ``fail_closed``: an exception here rolls the run back and no
incident survives (see ``orchestrator/runner.py``). The catalogue is a *data*
dependency — a JSON file on disk that can be absent, truncated, hand-edited or
unreadable — so every call goes through :func:`_lookup`, which returns ``None``
for any ``Exception``. §5.3.3 is explicit: "a site miss ... is *not* an error".

COORDINATE HONESTY
``lat``/``lon`` are town/suburb/county-centroid approximations rounded to 2 dp,
never surveyed positions; ``geo_precision`` travels with them so a consumer can
resolve it against ``sites.GEO_PRECISION_LEGEND`` and show ``sites.GEO_DISCLAIMER``.
Do not compute distances or access routes from these.

CACHED WEATHER SIGNAL (spec §4.2 sentence 1, §7.3.3), ``WEATHER_ENABLED``
-------------------------------------------------------------------------
§4.2 allows this node exactly one new thing: it "may *read* an already-cached
``external_signals`` row (flag-gated, wrapped fail-soft, byte-identical when the
table is empty)". Every word of that is load-bearing here.

*Read only, never fetch.* ``pollers/weather.py`` is the only thing that talks to
a provider, out of band, on its own 15-minute schedule. This node calls
``weather_risk_for_region`` — one indexed ``LIMIT 1`` select — and nothing else.
A fetch here would put an external API in the critical path of every incident,
which is the precise thing §4.2 forbids: a provider having a slow afternoon would
become a NOC having a slow afternoon.

*Byte-identical.* The signal lands in ONE place: :data:`WEATHER_KEY` inside the
same context dict the site catalogue uses. ``output_summary``, ``rationale``,
``tools_called`` and ``confidence`` are not touched in ANY code path — not even
with the flag on and a row present — so the step row
``tests/integration/test_golden_sequence.py`` pins cannot move whatever the flag
and the table say. (§5.3.3 additionally wants a ``noc_get_weather_risk`` tool
entry, a narrative sentence via ``compose_narrative(context_lines=...)`` and an
``access_risk`` for dispatch. All three DO move the step row or touch other
waves' files, so they are not in this wave — reported, not guessed.)

*Fail-soft on a fail-closed node.* See :func:`read_weather_risk`.

*Stale is shown, not hidden.* A stale row is still returned, carrying
``stale=True`` and ``age_s``, because "no data" and "three-hour-old data" are
different facts for an operator and only one of them is a reason to go look out
of the window. What must never happen is three-hour-old rain rendered as current,
so the label travels with the block and is recomputed against *now*
(``pollers.weather.staleness``) rather than trusted from the stored column.
Note this widens §5.3.3's "latest *non-stale* row" deliberately.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml

from noc_agents.config import OPERATORS_DIR, OperatorConfig
from noc_agents.orchestrator.contract import IncidentState, RunContext, StepResult
from noc_agents.services.composition import region_label
from noc_agents.services.sites import SiteRecord, lookup_site
from noc_agents.services.tt_classify import classify_tt
from noc_agents.services.user_estimate import estimate_users

log = logging.getLogger("noc_agents.agents.enrich")

#: IncidentState attribute holding the JSON-safe dict destined for
#: ``IncidentRow.context_json``. Always a dict after this node runs.
CONTEXT_ATTR = "context"

#: Key inside that dict for the site-catalogue block. Absent when the site is
#: not catalogued or the catalogue could not be read.
SITE_KEY = "site"

#: Key inside that dict for the cached weather block. Absent unless
#: ``WEATHER_ENABLED`` is on AND a row for the region has been cached. Sibling of
#: :data:`SITE_KEY`; neither touches the other.
WEATHER_KEY = "weather"

#: Flag gating the cache read. Default **false**, like the poller's own gate, so a
#: deployment that never opted in behaves exactly as it did before this wave.
WEATHER_ENABLED_ENV = "WEATHER_ENABLED"

#: The field that makes a cached block *risk* information rather than bookkeeping.
#: ``adapters.weather.derive_weather_risk`` always emits it; a block that lacks it came
#: from a row whose ``derived_json`` would not parse. See :func:`attach_weather_context`.
RISK_FIELD = "storm_flag"

#: ``config/operators/<op>/regions.yaml`` — the county -> region overrides.
REGIONS_FILENAME = "regions.yaml"

#: Spellings that read as true. Deliberately the SAME set as
#: ``pollers.weather._TRUE``, duplicated rather than imported so that a flag-off
#: run does not import the poller package at all (see :func:`weather_enabled`).
#: ``test_enrich_with_signals.py`` pins the two readings against each other.
_TRUE = {"1", "true", "yes", "on"}


def weather_enabled() -> bool:
    """``WEATHER_ENABLED`` — default **false**. Only an explicit true value reads.

    Checked before anything else so that the off path costs one ``os.getenv`` and
    imports nothing: no poller module, no SQLAlchemy statement, no YAML.
    """
    return (os.getenv(WEATHER_ENABLED_ENV) or "").strip().lower() in _TRUE


# --------------------------------------------------------------- county -> region


def regions_path(operator_id: str) -> Path:
    """``config/operators/<op>/regions.yaml`` — beside the operator's ``transfers.yaml``."""
    return OPERATORS_DIR / operator_id / REGIONS_FILENAME


def load_region_overrides(operator_id: str) -> dict[str, dict[str, str]]:
    """Read the overrides file. Missing or unreadable is EMPTY, never a crash.

    An empty override set is a safe state: the inverted base map still resolves
    every county that exactly one region claims, and an ambiguous county simply
    stays unresolved.
    """
    path = regions_path(operator_id)
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as exc:
        log.warning("region overrides unreadable (%s): %s — treating as empty", path, type(exc).__name__)
        return {}
    if not isinstance(raw, dict):
        log.warning("region overrides at %s is not a mapping — treating as empty", path)
        return {}
    out: dict[str, dict[str, str]] = {}
    for section in ("county_region", "extra"):
        block = raw.get(section)
        if isinstance(block, dict):
            out[section] = {str(k): str(v) for k, v in block.items() if v}
    return out


def _county_key(county: str | None) -> str:
    """Match counties case- and space-insensitively; ``"  nairobi "`` is Nairobi."""
    return (county or "").strip().casefold()


def county_region_map(cfg: OperatorConfig) -> dict[str, str]:
    """``{normalised county: region_code}``, built from config alone.

    Two layers, in order:

    1. **Inverted** from ``cfg.regions[*].counties`` — the operator profile's own
       county lists, so adding a county to a region needs no second edit here.
       A county claimed by MORE THAN ONE region is dropped: in the Safaricom
       profile Nairobi is claimed by NBI_E and NBI_W and Kiambu by NBI_E, NBI_W
       and MTK, and guessing which forecast an operator sees is not this
       function's call.
    2. **Overridden** by ``regions.yaml`` (``county_region`` then ``extra``),
       which is where such an ambiguity gets resolved by a human, and where a
       county no region lists can be given one. An override naming a region the
       profile does not define is ignored rather than trusted.
    """
    claims: dict[str, set[str]] = {}
    names: dict[str, str] = {}
    for code, region in (cfg.regions or {}).items():
        for county in getattr(region, "counties", None) or []:
            key = _county_key(county)
            if not key:
                continue
            claims.setdefault(key, set()).add(code)
            names.setdefault(key, str(county))
    mapping = {key: next(iter(codes)) for key, codes in claims.items() if len(codes) == 1}

    overrides = load_region_overrides(cfg.operator_id)
    for section in ("county_region", "extra"):
        for county, code in (overrides.get(section) or {}).items():
            key, code = _county_key(county), str(code).strip().upper()
            if key and code in (cfg.regions or {}):
                mapping[key] = code
            elif key:
                log.warning("regions.yaml maps %r to unknown region %r — ignored", county, code)
    return mapping


def region_for_county(cfg: OperatorConfig, county: str | None) -> str | None:
    """The cached-signal region for a county, or ``None`` when unknown or ambiguous."""
    return county_region_map(cfg).get(_county_key(county))


def cache_region(cfg: OperatorConfig, region_code: str | None, county: str | None) -> tuple[str | None, str]:
    """Which region's cache to read, and where that choice came from.

    ``region_code`` from the event wins whenever it names a configured region: it
    is the exact key ``pollers/weather.py`` caches under, and it is unambiguous
    where a county is not — the golden HUB event sits in Nairobi county, which two
    regions claim, so resolving it by county would be strictly worse information.
    The county map is the fallback for an event whose region code is blank or
    unrecognised. Returns ``(None, "none")`` when neither resolves.
    """
    code = (region_code or "").strip().upper()
    if code and code in (cfg.regions or {}):
        return code, "event"
    by_county = region_for_county(cfg, county)
    if by_county:
        return by_county, "county"
    return None, "none"


def _lookup(site_id: str | None) -> SiteRecord | None:
    """``lookup_site`` that can never fail this fail-closed node.

    A miss returns ``None`` on its own; this wrapper additionally absorbs a
    missing / malformed / unreadable seed file (``json.JSONDecodeError``,
    ``OSError``, ``KeyError`` from a row with no ``site_id``, ...).
    ``BaseException`` is deliberately not caught.
    """
    try:
        return lookup_site(site_id)
    except Exception:  # noqa: BLE001 — a data-file problem must not kill the incident
        return None


def site_block(site: SiteRecord) -> dict[str, Any]:
    """The catalogue fields Phase 1 added, as a JSON-safe mapping.

    Only the backfilled fields plus the provenance markers: ``county``,
    ``site_name`` and ``region_code`` are already incident columns and are not
    duplicated here.
    """
    return {
        "site_id": site.site_id,
        "lat": site.lat,
        "lon": site.lon,
        "geo_precision": site.geo_precision,
        "ward": site.ward,
        "site_class": site.site_class,
        "parent_hub_id": site.parent_hub_id,
        "riverine": site.riverine,
        "kplc_region": site.kplc_region,
        "kplc_area_hints": list(site.kplc_area_hints),
        "kplc_source": site.kplc_source,
    }


def attach_site_context(state: IncidentState, site_id: str | None) -> SiteRecord | None:
    """Merge the catalogue block into the state's context dict; return the record.

    The context dict is created if absent and left empty on a miss, so a caller
    can always rely on ``getattr(state, CONTEXT_ATTR)`` being a dict.
    """
    context: dict[str, Any] = dict(getattr(state, CONTEXT_ATTR, None) or {})
    site = _lookup(site_id)
    if site is not None:
        context[SITE_KEY] = site_block(site)
    setattr(state, CONTEXT_ATTR, context)
    return site


# ------------------------------------------------------------- cached weather read


def read_weather_risk(ctx: RunContext, region_code: str) -> dict[str, Any] | None:
    """The cached ``weather_risk`` block for a region, or ``None``. Cannot raise.

    EnrichmentAgent is ``fail_closed``: an exception escaping this node rolls the
    whole run back and no incident survives (``orchestrator/runner.py``). An
    advisory forecast is never worth an incident, so every failure mode below ends
    at ``None`` and the node carries on as if nothing had been cached:

    * **poller module absent or broken** — the import is lazy and inside the
      ``try``, so a half-landed ``pollers/weather.py`` (``ImportError``) degrades
      to "no signal" instead of breaking ENRICH's import graph. It also keeps the
      flag-off path from importing the scheduler and realtime hub at all.
    * **table absent** (an old DB file, a migration not yet run) —
      ``OperationalError: no such table``, caught here. Verified against a live
      session that this leaves the surrounding transaction usable, so nodes 4-12
      proceed normally; ``no_autoflush`` additionally guarantees this speculative
      read can never flush another node's in-flight objects.
    * **malformed row** — a bad ``derived_json`` is already absorbed upstream (it
      comes back as staleness metadata with no risk fields, which
      :func:`attach_weather_context` then drops), but a column that will not coerce
      (a ``fetched_at`` that is not a date, a row hand-edited in ``sqlite3``) raises
      on attribute access and is caught here.
    * **slow query / locked DB** — ``OperationalError`` after SQLite's busy
      timeout, caught. The read is one ``LIMIT 1`` on ``ix_signals_region_valid``,
      which is the cheapest shape available; there is no unbounded scan to be slow.
    * **no session** — unit callers build a ``RunContext(session=None)``; that is
      a miss, not a crash.

    ``BaseException`` is deliberately not caught: a ``KeyboardInterrupt`` or a
    cancellation must still stop the run.
    """
    session = getattr(ctx, "session", None)
    if session is None:
        return None
    try:
        from noc_agents.pollers.weather import weather_risk_for_region

        with session.no_autoflush:
            return weather_risk_for_region(session, ctx.cfg.operator_id, region_code)
    except Exception as exc:  # noqa: BLE001 — advisory data must never fail the incident
        log.warning("weather cache read failed for %s: %s — continuing without it", region_code, type(exc).__name__)
        return None


def attach_weather_context(state: IncidentState, ctx: RunContext) -> dict[str, Any] | None:
    """Merge the cached weather block into the state's context dict; return it.

    Adds :data:`WEATHER_KEY` only when the flag is on AND a region resolves AND a
    row exists AND that row's derived block is readable. In every other case the
    context dict is left exactly as the site catalogue left it, which is what makes
    the empty-table case byte-identical.

    :data:`RISK_FIELD` is the readability test. A row whose ``derived_json`` will not
    parse comes back from ``weather_risk_for_region`` as staleness metadata with no
    risk fields at all, and attaching *that* would be worse than attaching nothing:
    a consumer reading ``block.get("storm_flag")`` would get a falsy value and show
    "no storm" on the strength of a corrupted row. A corrupt block is a failure mode,
    so it degrades the same way every other failure mode here does — to silence.
    """
    if not weather_enabled():
        return None
    try:
        # Second guard, outside read_weather_risk's own: resolving the region reads
        # config and YAML, and a broken regions.yaml must be as harmless as a
        # broken row. Everything from here to the merge is advisory.
        region_code, source = cache_region(ctx.cfg, state.event.region_code, state.county)
        if region_code is None:
            return None
        block = read_weather_risk(ctx, region_code)
        if not block or RISK_FIELD not in block:
            return None
        # Provenance: an operator seeing Coast weather on a site whose event carried
        # no region should be able to tell it was inferred from the county.
        block["region_source"] = source
        context: dict[str, Any] = dict(getattr(state, CONTEXT_ATTR, None) or {})
        context[WEATHER_KEY] = block
        setattr(state, CONTEXT_ATTR, context)
        return block
    except Exception as exc:  # noqa: BLE001 — advisory data must never fail the incident
        log.warning("weather context skipped: %s — continuing without it", type(exc).__name__)
        return None


def input_summary(state: IncidentState, ctx: RunContext) -> str:
    return state.event.site_id


def run(state: IncidentState, ctx: RunContext) -> StepResult:
    cfg, event = ctx.cfg, state.event
    # Catalogue read first, so a seed problem surfaces through the wrapper and not
    # halfway through the derived values below. Nothing below reads `site`: the
    # golden ENRICH step row and its five state fields are computed exactly as before.
    attach_site_context(state, event.site_id)
    state.users = estimate_users(event.site_type, event.region_code, cfg, event.users_affected)
    region = cfg.regions.get(event.region_code.upper())
    state.site_name = event.site_name or event.site_id
    state.county = event.county or (region.counties[0] if region and region.counties else None)
    state.is_hub = event.site_type.upper() in ("HUB", "CORE")
    state.tt = classify_tt(
        alarm_code=event.alarm_code,
        failure_domain=event.failure_domain,
        site_type=event.site_type,
        technology=event.technology,
        cfg=cfg,
    )
    # Cache read LAST, after state.county is settled, so the county the fallback maps
    # from is the same one the incident will carry. Nothing below reads the block: it
    # goes to the context dict only, so the four StepResult fields and the five state
    # fields the golden test pins are computed identically whatever the flag says.
    attach_weather_context(state, ctx)
    coverage = ""
    if region and region.coverage_areas:
        coverage = "; coverage=" + ", ".join(region.coverage_areas[:3])
    return StepResult(
        output_summary=(
            f"users_est={state.users}, region={event.region_code}, hub={state.is_hub}, class={state.tt.site_class}"
        ),
        rationale=(
            f"CMDB/mock enrich: {region_label(cfg, event.region_code)}; "
            f"FE on-call={(region.fe_oncall if region else 'n/a')}; "
            f"{state.tt.rationale}{coverage}"
        ),
        tools=[
            {"name": "lookup_site", "ok": True, "latency_ms": 3},
            {"name": "estimate_users_affected", "ok": True, "latency_ms": 1},
            {"name": "classify_tt", "ok": True, "latency_ms": 1},
        ],
        confidence=0.85,
    )
