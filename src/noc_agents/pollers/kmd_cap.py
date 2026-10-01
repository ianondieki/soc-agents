"""``kmd_cap``: the WeatherRiskAgent's 30-minute read of KMD's CAP warnings (spec §5.3.13, §7.3.3).

Why this exists, operationally: the Kenya Meteorological Department is the authority on
severe weather in Kenya. When it warns of heavy rain over Migori, Nyamira, Bungoma and Busia,
that is the single most credible reason a NOC has to pre-position a generator in Western.
The forecast poller (``pollers/weather.py``) is a model's opinion at a ~10 km grid point; a
CAP alert is a named government forecaster's statement about named counties. **Advisory
only**: nothing here changes a priority, opens an incident or sends a message.

Two principles, both non-negotiable
-----------------------------------
**1. A CAP alert is the Met Department's statement, not ours.** Every row stores what the
document said — severity, urgency, certainty, event, headline, areas — verbatim, and its
``valid_until`` *is* the document's ``expires``. Nothing is re-scored, softened or extended.
Two consequences that look like interpretation and are not:

* an ``Update`` or ``Cancel`` that ``references`` an earlier alert ends that alert at the new
  message's ``sent`` time. Honouring a cancellation is repeating KMD; ignoring it would be
  extending a warning KMD has withdrawn. Order must not matter (review finding F07): each run
  processes the documents it holds oldest-``sent`` first, and a Cancel/Update whose original
  has not been seen yet leaves a **tombstone** (``tombstone:<identifier>``), which ends the
  original the moment it does arrive — later in the same feed, or on a later run;
* a document with no ``expires`` (optional in CAP 1.2) is held for
  :data:`ALERT_HOLD_WITHOUT_EXPIRES` past each fetch that still finds it in the feed, and the
  row says so (``valid_until_basis``). KMD named no end; the honest reading is "in force for
  as long as KMD keeps publishing it", not an end date somebody here invented. That hold is
  itself capped at ``sent + CAP_STALE_DAYS``: a feed that stopped being maintained keeps
  listing its old items (§7.3 found it 132 days stale), and "still listed" must not turn a
  May advisory into a September warning. ``CAP_STALE_DAYS`` is the operator's own statement of
  how old a KMD item may be and still be current; an alert older than that is not current
  either.

``storm_flag`` is §7.3.1's rule, applied literally: "any KMD alert with severity ∈ {Severe,
Extreme} covering the county". ``confidence`` stays 1.0 — in this table it means "how far we
trust that this row holds what the provider said" (the weather lane writes 1.0 for a good row
and 0.0 for a failure marker), and turning CAP's ordinal ``certainty`` ("Likely") into a
probability would be precisely the re-interpretation principle 1 forbids. ``certainty`` and
``urgency`` travel verbatim in ``derived_json``.

**2. Silence is not good news.** A feed that has not answered for three days, a feed that
answers but whose newest item is months old (§7.3 found it 132 days stale), a region the
profile gives no counties, a profile with a misspelt county — every one of these produces
*zero warnings in force*, and not one of them means "no warnings". So every run writes a
**feed-health row per region** (``external_id = feed:<REGION>``) whose ``derived.state`` says
which of ``ok | stale_feed | incomplete | unreachable | misconfigured | unmapped`` holds, with
the reason. ``incomplete`` (review finding F08) is a feed that answered while CAP documents it
lists could not be read or were deferred: an alert may be missing, so zero is not zero, and
the failures go in ``last_error``.

The feed-health row is written **already expired** (``valid_until = fetched_at``), exactly
like ``pollers/weather.py``'s failure marker, because ``services/dashboards._count_live``
counts every ``KMD_CAP`` row still inside its validity window as a warning in force.

**Where freshness comes from — and where it must not.** The Regions dashboard's CAP block
takes its ``stale`` from :func:`services.signals.cap_feed_health`, which judges the newest
feed-health row *against now*: a poller that stopped running reads ``not_polled_recently``,
never the ``ok`` it last wrote (review finding F02). Freshness never comes from an alert row.
An earlier version let whichever ``KMD_CAP`` row sorted newest decide, so a warning *arriving*
made a region look fresh and CALM, and stayed that way after polling stopped (review finding
F03). A warning must never make a region look calmer: a Severe/Extreme alert in force lifts
the region to at least WATCH instead (``services/dashboards._status``).

Politeness (§7.3.3, §7.3.4: "no terms published — be polite")
-------------------------------------------------------------
Thirty-minute cadence = two feed requests an hour, the spec's ceiling. ``If-Modified-Since``
is sent from the previous run's ``Last-Modified``. A CAP document is fetched **once**: the
links already processed are remembered in the database (the feed-health row, and the
``source_url`` of stored alerts), so a restart is not a burst of re-fetches, and a run
fetches at most :data:`MAX_DOCS_PER_RUN` new documents.

Gating
------
``WEATHER_ENABLED``, default **false**. §5.3.13 lists ``kmd_cap`` as one of the
WeatherRiskAgent's three triggers under that one flag, and ``.env.example`` names no separate
CAP flag, so this lane does not invent one. Off means no request, no row, one skipped step.
``CAP_STALE_DAYS`` (``.env.example``, default 7) sets when an answering feed counts as stale.

County → region
---------------
Through ``services.signals.county_region_map``, which is built from the operator profile's
own ``regions.<CODE>.counties`` — never a second list — and keyed by the *canonical*
county, so KMD's "Nairobi City County" finds the profile's "Nairobi" (review finding F04). A
county in several regions (Nairobi is in NBI_E and NBI_W) gets a row in each. A county in none
is stored with ``region_code=NULL``: KMD said it. Every poll **re-attributes every alert still
in force** against the current map, from the areas stored with it and without a re-fetch
(:func:`reattribute_live`, review finding F10), so a profile fix takes effect on the next run:
new regions get a row, a region that no longer lists the county has its row ended. A
profile that names a county which is not one of Kenya's 47 makes this poller **refuse to
attribute anything** and say so on every region's feed-health row — see
``api/routers/signals.py`` for why that, and not a crash at import, is where §7.3.7's
"rejects unknown counties at startup" is enforced.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from noc_agents.adapters.kmd_cap import (
    XML_PARSER,
    CapAlert,
    FeedFetch,
    KmdCapProvider,
    provider_from_env,
)
from noc_agents.adapters.weather import WeatherError
from noc_agents.config import AppSettings
from noc_agents.db.models import ExternalSignalRow, new_id, utcnow
from noc_agents.realtime.hub import RealtimeEvent, hub
from noc_agents.scheduler import JobCard, JobResult
from noc_agents.services.signals import (
    CAP_SOURCE,
    KIND_CAP_ALERT,
    KIND_CAP_FEED,
    KIND_CAP_TOMBSTONE,
    canonical_county,
    county_key,
    county_region_map,
    latest_row,
    validate_county_map,
)

log = logging.getLogger("noc_agents.pollers.kmd_cap")

__all__ = [
    "AGENT",
    "ALERT_HOLD_WITHOUT_EXPIRES",
    "CAP_JOB",
    "DEFAULT_CAP_STALE_DAYS",
    "ENABLED_ENV",
    "GRAPH_NAME",
    "INTERVAL_S",
    "JOB_NAME",
    "MAX_DOCS_PER_RUN",
    "STORM_SEVERITIES",
    "cap_enabled",
    "cap_stale_days",
    "poll",
    "reattribute_live",
]

JOB_NAME = "kmd_cap"
INTERVAL_S = 1800  # 30 min (§5.3.13) = 2 feed requests/hour, the §7.3.3 ceiling
AGENT = "WeatherRiskAgent"
GRAPH_NAME = "kmd_cap"  # agent_runs.graph_name, so /runs can tell it from the forecast poll
ENABLED_ENV = "WEATHER_ENABLED"  # §5.3.13: one flag for the agent's three triggers
STALE_DAYS_ENV = "CAP_STALE_DAYS"
DEFAULT_CAP_STALE_DAYS = 7  # §7.3.1 cfg.weather.cap_stale_days; .env.example default

#: §7.3.1's storm rule for CAP: "severity ∈ {Severe, Extreme}". Compared case-insensitively
#: because CAP's code values are case-sensitive in the schema but forecasters' tools are not.
STORM_SEVERITIES: frozenset[str] = frozenset({"severe", "extreme"})

#: How long an alert with NO ``expires`` stays in force after a fetch that still found it in
#: the feed. Two poll intervals: one missed poll does not drop a live warning, and a warning
#: KMD has stopped publishing lapses within the hour. UNVERIFIED operational value (D14).
ALERT_HOLD_WITHOUT_EXPIRES = timedelta(seconds=2 * INTERVAL_S)

#: New CAP documents fetched per run, at most. The rest wait for the next run. A feed that
#: suddenly lists hundreds of items is either a bulk re-publication or a broken server, and
#: neither justifies hundreds of requests to a government host in one burst.
MAX_DOCS_PER_RUN = 10

#: How many processed document links a feed-health row remembers. Well above any real feed
#: length; a bound so a pathological feed cannot grow a row without limit.
_SEEN_LINKS_CAP = 500

_TRUE = {"1", "true", "yes", "on"}


def cap_enabled() -> bool:
    """``WEATHER_ENABLED`` — default **false**. Only an explicit true value polls."""
    return (os.getenv(ENABLED_ENV) or "").strip().lower() in _TRUE


def cap_stale_days() -> int:
    """``CAP_STALE_DAYS`` (default 7). A malformed value falls back to 7 with a warning —
    a typo in an env file must not stop the poller, and must not silently mean "never stale"."""
    raw = (os.getenv(STALE_DAYS_ENV) or "").strip()
    if not raw:
        return DEFAULT_CAP_STALE_DAYS
    try:
        value = int(raw)
    except ValueError:
        log.warning("kmd_cap: %s=%r is not an integer; using %d", STALE_DAYS_ENV, raw, DEFAULT_CAP_STALE_DAYS)
        return DEFAULT_CAP_STALE_DAYS
    if value < 1:
        log.warning("kmd_cap: %s=%r must be >= 1; using %d", STALE_DAYS_ENV, raw, DEFAULT_CAP_STALE_DAYS)
        return DEFAULT_CAP_STALE_DAYS
    return value


def _z(dt: datetime | None) -> str | None:
    return dt.replace(microsecond=0).isoformat() + "Z" if dt else None


def _dumps(value: Any) -> str:
    # Compact separators matter: services.signals.latest_row finds a row's kind with a LIKE on
    # '"kind":"..."', so the spacing here is part of that contract.
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _loads(raw: str | None) -> dict[str, Any]:
    try:
        block = json.loads(raw or "{}")
    except ValueError:
        return {}
    return block if isinstance(block, dict) else {}


# ---------------------------------------------------------------------------- outcome


@dataclass
class CapRunOutcome:
    """Everything one run learned, for the JobResult and for the feed-health rows."""

    state: str = "ok"
    reachable: bool = False
    error: str | None = None
    error_kind: str | None = None
    not_modified: bool = False
    items_in_feed: int = 0
    documents_fetched: int = 0
    documents_deferred: int = 0
    alerts_stored: int = 0
    rows_written: int = 0
    alerts_ended: int = 0
    skipped_not_actual: int = 0
    document_failures: list[str] = field(default_factory=list)
    unmapped_areas: list[str] = field(default_factory=list)
    regions_warned: set[str] = field(default_factory=set)
    storm_regions: set[str] = field(default_factory=set)
    newest_sent: datetime | None = None
    feed_age_days: float | None = None
    feed_stale: bool = True
    reason: str | None = None
    seen_links: list[str] = field(default_factory=list)
    pending_links: list[str] = field(default_factory=list)  # failed or deferred: retried next run
    tombstones: int = 0
    reattributed: int = 0
    last_modified: str | None = None
    county_problems: list[dict[str, Any]] = field(default_factory=list)

    def as_tool(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": "kmd_cap.poll",
            "ok": self.state in {"ok", "stale_feed"},
            "state": self.state,
            "reachable": self.reachable,
            "not_modified": self.not_modified,
            "items_in_feed": self.items_in_feed,
            "documents_fetched": self.documents_fetched,
            "documents_deferred": self.documents_deferred,
            "alerts_stored": self.alerts_stored,
            "rows_written": self.rows_written,
            "alerts_ended": self.alerts_ended,
            "tombstones": self.tombstones,
            "reattributed": self.reattributed,
            "skipped_not_actual": self.skipped_not_actual,
            "regions_warned": sorted(self.regions_warned),
            "storm_regions": sorted(self.storm_regions),
            "unmapped_areas": sorted(set(self.unmapped_areas)),
            "newest_sent": _z(self.newest_sent),
            "feed_age_days": self.feed_age_days,
            "feed_stale": self.feed_stale,
            "xml_parser": XML_PARSER,
        }
        if self.document_failures:
            out["document_failures"] = self.document_failures[:20]
        if self.county_problems:
            out["county_problems"] = self.county_problems
        if self.error:
            out["error"] = self.error
            out["error_kind"] = self.error_kind
        return out


# ---------------------------------------------------------------------------- reads (own rows)


def _previous_health(session: Session, operator_id: str) -> dict[str, Any]:
    """The newest feed-health block for this operator, any region: where the last
    ``Last-Modified``, ``newest_sent`` and the processed-link memory are kept."""
    row = latest_row(session, operator_id, source=CAP_SOURCE, kind=KIND_CAP_FEED)
    if row is None:
        return {}
    block = _loads(row.derived_json)
    block["_fetched_at"] = row.fetched_at
    return block


def _known_alert_links(session: Session, operator_id: str) -> set[str]:
    """Every CAP document URL an alert row was already written from (this operator)."""
    rows = session.scalars(
        select(ExternalSignalRow.source_url).where(
            ExternalSignalRow.operator_id == operator_id,
            ExternalSignalRow.source == CAP_SOURCE,
            ExternalSignalRow.derived_json.like(f'%"kind":"{KIND_CAP_ALERT}"%'),
        )
    ).all()
    return {r for r in rows if r}


def _like_escape(text: str) -> str:
    """Escape LIKE's wildcards: an identifier ``a_1`` must not match ``ab1#...``."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _alert_rows(session: Session, operator_id: str, identifier: str) -> list[ExternalSignalRow]:
    """Every row (all regions) written for one CAP identifier."""
    return list(
        session.scalars(
            select(ExternalSignalRow).where(
                ExternalSignalRow.operator_id == operator_id,
                ExternalSignalRow.source == CAP_SOURCE,
                ExternalSignalRow.external_id.like(f"{_like_escape(identifier)}#%", escape="\\"),
            )
        ).all()
    )


def _tombstone(session: Session, operator_id: str, identifier: str) -> ExternalSignalRow | None:
    return session.scalar(
        select(ExternalSignalRow).where(
            ExternalSignalRow.operator_id == operator_id,
            ExternalSignalRow.source == CAP_SOURCE,
            ExternalSignalRow.external_id == f"tombstone:{identifier}",
        )
    )


# ---------------------------------------------------------------------------- writes


def _targets(
    pieces: Iterable[str], mapping: Mapping[str, tuple[str, ...]]
) -> tuple[dict[str, tuple[str | None, list[str]]], list[str]]:
    """Where one alert's rows belong: ``{external_id suffix: (region_code | None, [county, ...])}``
    plus the area pieces nobody recognised.

    One row per region of this operator that covers any of the alert's counties (suffix = the
    region code); one row per county that is real but in no region, and per piece that is not
    a county at all (``county=<name>``, region ``NULL`` — each on its own row, so a later
    profile edit that maps one of them can find it); ``no-area`` for an alert with no area.

    The lookup is by :func:`services.signals.county_key` — the canonical county — so the feed's
    spelling and the profile's need only agree with the gazetteer, not with each other (review
    finding F04). Canonical spelling is what gets stored where the gazetteer knows the county;
    KMD's own words where it does not.
    """
    regions: dict[str, list[str]] = {}
    unattributed: list[str] = []
    unknown: list[str] = []
    for piece in pieces:
        if not any(ch.isalpha() for ch in str(piece)):
            continue  # punctuation, not a place (NEW3) — also guards areas stored before that fix
        canonical = canonical_county(piece)
        name = canonical or piece
        if canonical is None:
            unknown.append(piece)
        codes = mapping.get(county_key(piece), ()) if canonical else ()
        if not codes:
            if name not in unattributed:
                unattributed.append(name)
        for code in codes:
            bucket = regions.setdefault(code, [])
            if name not in bucket:
                bucket.append(name)
    targets: dict[str, tuple[str | None, list[str]]] = {code: (code, cs) for code, cs in sorted(regions.items())}
    for name in unattributed:
        targets[f"county={name}"] = (None, [name])
    if not targets:
        targets["no-area"] = (None, [])
    return targets, unknown


def _upsert(session: Session, *, operator_id: str, external_id: str, now: datetime) -> tuple[ExternalSignalRow, bool]:
    """The row for ``(operator, KMD_CAP, external_id)`` and whether it was just created."""
    row = session.scalar(
        select(ExternalSignalRow).where(
            ExternalSignalRow.operator_id == operator_id,
            ExternalSignalRow.source == CAP_SOURCE,
            ExternalSignalRow.external_id == external_id,
        )
    )
    if row is None:
        row = ExternalSignalRow(
            id=new_id(), operator_id=operator_id, source=CAP_SOURCE, external_id=external_id,
            source_url="", payload_json="{}", created_at=now,
        )
        session.add(row)
        return row, True
    return row, False


def _held_until(sent: datetime | None, now: datetime, stale_days: int) -> datetime:
    """``valid_until`` for an alert with no ``expires``: the hold window, capped by currency.

    ``now + ALERT_HOLD_WITHOUT_EXPIRES``, but never later than ``sent + CAP_STALE_DAYS``
    (module docstring, principle 1). An alert already older than that comes back with a
    ``valid_until`` in the past: stored, because KMD said it, and not in force.
    """
    held = now + ALERT_HOLD_WITHOUT_EXPIRES
    if sent is not None:
        held = min(held, sent + timedelta(days=stale_days))
    return held


def store_alert(
    session: Session,
    *,
    operator_id: str,
    alert: CapAlert,
    mapping: Mapping[str, tuple[str, ...]],
    now: datetime,
    stale_days: int = DEFAULT_CAP_STALE_DAYS,
) -> tuple[list[ExternalSignalRow], list[str]]:
    """One row per region the alert covers (plus one per unattributable county). Does not commit.

    ``external_id`` is ``<CAP identifier>#<REGION>``. The spec says "external_id = identifier";
    the unique key is ``(operator_id, source, external_id)`` and one alert legitimately covers
    several of *this operator's* regions (Nairobi county is in NBI_E and NBI_W), so the
    identifier alone cannot be the key of a per-region row. The identifier is still what makes
    re-polling idempotent — the suffix only says which region's copy this is — and it is kept
    verbatim in ``derived.identifier``. Unattributed counties use ``#county=<name>``; an alert
    with no ``<area>`` at all uses ``#no-area``.
    """
    session.flush()  # autoflush is off: make this run's tombstones and rows visible to the lookups
    targets, unknown = _targets(alert.counties, mapping)
    storm = (alert.severity or "").strip().lower() in STORM_SEVERITIES
    if alert.expires is not None:
        valid_until, basis = alert.expires, "expires"
    else:
        valid_until = _held_until(alert.sent, now, stale_days)
        basis = (
            f"held: the CAP document has no <expires>; repeated for "
            f"{int(ALERT_HOLD_WITHOUT_EXPIRES.total_seconds() // 60)} min past each fetch that still finds it in the feed, "
            f"and never beyond sent + CAP_STALE_DAYS ({stale_days} d)"
        )
    # A Cancel/Update for this identifier may have arrived BEFORE the alert itself (review
    # finding F07). Its tombstone ends the alert the moment it is stored.
    tomb = _tombstone(session, operator_id, alert.identifier)
    tomb_block = _loads(tomb.derived_json) if tomb is not None else {}
    tomb_end = _parse_z((tomb_block.get("ended_by") or {}).get("sent"))
    if tomb_end is not None:
        valid_until = min(valid_until, tomb_end)
        if alert.effective is not None and valid_until < alert.effective:
            # Never before the span's own start (``row.valid_from`` below): a window that ends
            # before it begins covers no instant, yet reads it as a live claim (review finding
            # CANCEL-INVERSION). Ending it at its start says the same thing honestly. The end may
            # still fall before ``fetched_at`` — an alert already cancelled when we first saw it —
            # and that is not an inversion: it is a true statement about a span nobody could act
            # on, which the backtest declines to count as an episode.
            valid_until = alert.effective
    payload = _dumps({"format": "application/cap+xml", "source_url": alert.source_url, "xml": alert.raw_xml})
    said = alert.as_payload()

    rows: list[ExternalSignalRow] = []
    for sfx, (region_code, cs) in targets.items():
        row, created = _upsert(session, operator_id=operator_id, external_id=f"{alert.identifier}#{sfx}", now=now)
        previous_block = {} if created else _loads(row.derived_json)
        if previous_block.get("detached"):
            # A re-store must not revive a detached span (NEW4); reattribute_live, which runs
            # later in the same poll, opens a new span for this target if the map covers it.
            continue
        row.source_url = alert.source_url
        row.region_code = region_code
        row.county = ", ".join(cs) if cs else None
        row.site_id = None
        # fetched_at is "when did we FIRST know", and a re-store never moves it. A CAP
        # document is immutable (a change is a new identifier that references this one), so
        # re-reading it learns nothing new -- while moving the timestamp would shrink the
        # section 10.1 M6 lead time (failure_time - fetched_at) and flatter the backtest.
        if created or row.fetched_at is None:
            row.fetched_at = now
        row.valid_from = alert.effective
        if previous_block.get("ended_by"):
            # A later Update/Cancel already ended this alert. Re-storing the original must
            # never resurrect it: only ever the earlier of the two ends.
            row.valid_until = min(row.valid_until or valid_until, valid_until)
        else:
            row.valid_until = valid_until
        row.stale = 0
        row.confidence = 1.0
        row.storm_flag = 1 if storm else 0
        row.flood_flag = 0  # the GloFAS poller's flag (§7.3.1); CAP event text is not re-read into it
        row.planned_power = 0
        row.access_risk = 0  # a floor judgement, not something KMD's document states
        row.payload_json = payload
        block = {
            "kind": KIND_CAP_ALERT,
            **said,
            "region_counties": cs,
            "unrecognised_areas": unknown,
            "valid_until_basis": basis,
            "storm_rule": "severity in {Severe, Extreme} (spec §7.3.1)",
        }
        if previous_block.get("ended_by"):
            block["ended_by"] = previous_block["ended_by"]
        elif tomb_block.get("ended_by"):
            block["ended_by"] = tomb_block["ended_by"]
        row.derived_json = _dumps(block)
        row.last_error = None
        rows.append(row)
    return rows, unknown


def end_referenced(
    session: Session,
    *,
    operator_id: str,
    alert: CapAlert,
) -> int:
    """An ``Update``/``Cancel`` ends the alerts it ``references`` at its own ``sent`` time.

    Only ever *shortens* ``valid_until`` (``min``), never lengthens it: a late-arriving update
    must not resurrect a warning that had already expired on its own terms. The superseding
    message is recorded on the ended row so the history says who ended it and how.
    """
    ended, _ = _end_referenced(session, operator_id=operator_id, alert=alert, now=None)
    return ended


def _end_referenced(
    session: Session,
    *,
    operator_id: str,
    alert: CapAlert,
    now: datetime | None,
) -> tuple[int, int]:
    """:func:`end_referenced`, plus a tombstone for each referenced identifier not yet stored.

    The tombstone is a ``KMD_CAP`` row keyed ``tombstone:<identifier>``, region ``NULL``,
    already expired (``valid_until`` = the ending message's ``sent``), so nothing counts it as a
    warning. :func:`store_alert` consults it, so a Cancel read before its original — earlier in
    the same feed, or because ``MAX_DOCS_PER_RUN`` deferred the original — still wins (review
    finding F07). When two messages end the same identifier, the earlier end is kept.
    Returns ``(rows ended, tombstones written)``.
    """
    if not alert.references or alert.sent is None:
        return 0, 0
    # The session is autoflush=False (db/models.py): an original stored earlier in THIS run
    # is invisible to the query below until flushed, and the Cancel would miss it.
    session.flush()
    ending = {"identifier": alert.identifier, "msgType": alert.msg_type, "sent": _z(alert.sent)}
    ended = tombs = 0
    for ref in alert.references:
        rows = [r for r in _alert_rows(session, operator_id, ref) if not r.external_id.startswith("tombstone:")]
        for row in rows:
            # Never earlier than the row's own start: a Cancel sent at 13:00 that is only fetched
            # after a 13:30 re-attach would otherwise leave the span (13:30 .. 13:00), an inverted
            # window that can cover no incident yet counts as a resolved, never-hit episode — a
            # guaranteed false alarm in CAP precision (review finding CANCEL-INVERSION). Ending it
            # at its own start says the same thing honestly: this span was never in force.
            # The start is ``valid_from``, not ``fetched_at``: an alert whose Cancel we read after
            # its sent time — the ordinary case — still ends when KMD said it ended (F07).
            start_at = row.valid_from or row.fetched_at
            end_at = max(alert.sent, start_at) if start_at is not None else alert.sent
            if row.valid_until is None or row.valid_until > end_at:
                row.valid_until = end_at
                ended += 1
            block = _loads(row.derived_json)
            block["ended_by"] = ending
            row.derived_json = _dumps(block)
        if rows or now is None:
            continue
        tomb, created = _upsert(session, operator_id=operator_id, external_id=f"tombstone:{ref}", now=now)
        earlier = _parse_z((_loads(tomb.derived_json).get("ended_by") or {}).get("sent")) if not created else None
        if earlier is not None and earlier <= alert.sent:
            continue  # an earlier end is already recorded
        tomb.source_url = alert.source_url
        tomb.region_code = None
        tomb.county = None
        tomb.site_id = None
        tomb.fetched_at = now
        tomb.valid_from = None
        tomb.valid_until = alert.sent  # already over: never a warning, never counted
        tomb.stale = 1
        tomb.confidence = 1.0
        tomb.storm_flag = tomb.flood_flag = tomb.planned_power = tomb.access_risk = 0
        tomb.payload_json = "{}"
        tomb.derived_json = _dumps({"kind": KIND_CAP_TOMBSTONE, "identifier": ref, "ended_by": ending})
        tomb.last_error = None
        tombs += 1
    return ended, tombs


#: A re-attached span's ``external_id`` suffix: ``<target>@<YYYYmmddTHHMMSSZ>`` (review finding NEW4).
_SPAN = re.compile(r"(?P<target>.*)@(?P<at>\d{8}T\d{6}Z)")


def _span_target(identifier: str, row: ExternalSignalRow) -> str:
    """Which target (region code, ``county=<name>`` or ``no-area``) a row is a span of."""
    rest = (row.external_id or "")[len(identifier) + 1:]
    match = _SPAN.fullmatch(rest)
    return match.group("target") if match else rest


def _identifier_of(row: ExternalSignalRow) -> str:
    return str(_loads(row.derived_json).get("identifier") or (row.external_id or "").split("#", 1)[0])


def reattribute_live(
    session: Session,
    *,
    operator_id: str,
    mapping: Mapping[str, tuple[str, ...]],
    now: datetime,
) -> int:
    """Re-attribute every alert still in force against the CURRENT county map (review finding F10).

    From what is already stored — each row's ``derived.areas`` — never a re-fetch. For each
    identifier with a row in force:

    * a target (region) the current map covers but that has no row in force gets a **new span**
      whose ``fetched_at`` is *now* — that region only learned of the warning now, and the M6
      lead time must not pretend otherwise;
    * a row in force whose target the map no longer produces is **ended at now**
      (``valid_until = now``, ``derived.detached``), never deleted, so the backtest keeps what
      the floor actually saw;
    * rows that stay get their county list refreshed.

    **A detached row is never revived** (review finding NEW4). Reviving it restored one
    continuous validity span from its first fetch to KMD's expiry, so the backtest credited the
    region with a warning through the hours it was not mapped to the county at all. When the
    map covers the target again, a new row is written instead — ``<identifier>#<target>@<now>``
    — so the history reads "warned 12:00-12:30, not warned 12:30-14:30, warned again from 14:30",
    which is what the floor saw. Nothing about what KMD said changes. Returns the number of rows
    created or ended. Does not commit.
    """
    session.flush()  # autoflush is off: see this run's own writes
    live = session.scalars(
        select(ExternalSignalRow).where(
            ExternalSignalRow.operator_id == operator_id,
            ExternalSignalRow.source == CAP_SOURCE,
            ExternalSignalRow.valid_until > now,
            ExternalSignalRow.derived_json.like(f'%"kind":"{KIND_CAP_ALERT}"%'),
        )
    ).all()
    by_identifier: dict[str, list[ExternalSignalRow]] = {}
    for row in live:
        by_identifier.setdefault(_identifier_of(row), []).append(row)
    changed = 0
    for ident, rows in by_identifier.items():
        # A detached row is never the template: its areas are the same, but its validity is
        # this region's ended one, not the alert's (review finding NEW2).
        template = next((r for r in rows if not _loads(r.derived_json).get("detached")), rows[0])
        block = _loads(template.derived_json)
        pieces: list[str] = []
        for area in block.get("areas") or []:
            for piece in area.get("counties") or []:
                if piece not in pieces:
                    pieces.append(piece)
        targets, unknown = _targets(pieces, mapping)
        spans: dict[str, list[ExternalSignalRow]] = {}
        for row in _alert_rows(session, operator_id, ident):
            spans.setdefault(_span_target(ident, row), []).append(row)

        for target, (region_code, cs) in targets.items():
            in_force = [
                r for r in spans.get(target, ())
                if r.valid_until is not None and r.valid_until > now and not _loads(r.derived_json).get("detached")
            ]
            if in_force:
                for row in in_force:
                    rb = _loads(row.derived_json)
                    if rb.get("region_counties") != cs:
                        rb["region_counties"] = cs
                        row.derived_json = _dumps(rb)
                        row.county = ", ".join(cs) if cs else None
                continue
            if any(_loads(r.derived_json).get("ended_by") for r in spans.get(target, ())):
                continue  # KMD itself ended this alert here: a profile edit must not restart it
            # First span for this target, or a NEW span after a detachment (never a revival).
            external_id = f"{ident}#{target}" if not spans.get(target) else f"{ident}#{target}@{now:%Y%m%dT%H%M%SZ}"
            new, _ = _upsert(session, operator_id=operator_id, external_id=external_id, now=now)
            nb = dict(block, region_counties=cs, unrecognised_areas=unknown)
            nb.pop("detached", None)
            nb["attributed_at"] = _z(now)
            new.source_url = template.source_url
            new.region_code = region_code
            new.county = ", ".join(cs) if cs else None
            new.site_id = None
            new.fetched_at = now
            # A span's window starts when this region was attributed, never at the alert's
            # effective time: the row did not exist before, and anything reading
            # valid_from <= t <= valid_until — the PIR timeline does — would otherwise place
            # this span hours before it began, listing one alert twice and crediting the
            # region through the gap NEW4 exists to stop crediting (review finding PIR-SPAN).
            new.valid_from = max(template.valid_from, now) if template.valid_from else now
            new.valid_until = template.valid_until
            new.stale = template.stale
            new.confidence = template.confidence
            new.storm_flag = template.storm_flag
            new.flood_flag = new.planned_power = new.access_risk = 0
            new.payload_json = template.payload_json
            new.derived_json = _dumps(nb)
            new.last_error = template.last_error
            changed += 1

        for target, target_rows in spans.items():
            if target in targets:
                continue
            for row in target_rows:
                if row.valid_until is None or row.valid_until <= now:
                    continue
                rb = _loads(row.derived_json)
                rb["detached"] = {"at": _z(now), "reason": "the operator profile no longer maps this county to this scope"}
                row.derived_json = _dumps(rb)
                row.valid_until = now
                changed += 1
    return changed


def write_feed_health(
    session: Session,
    *,
    operator_id: str,
    regions: Iterable[str],
    unmapped_regions: Iterable[str],
    outcome: CapRunOutcome,
    feed_url: str,
    now: datetime,
    previous: Mapping[str, Any],
    stale_days: int,
) -> list[ExternalSignalRow]:
    """Upsert ``feed:<REGION>`` for every region of the operator. Does not commit.

    ``fetched_at`` is the last time the feed was actually **reached**: on a failed run the
    previous success time is kept, so the row's age says how long we have been blind, which
    is the number an operator needs ("KMD unreachable for 3 days"), not how long since we
    last tried. ``attempted_at`` in the block records the try.
    """
    unmapped = set(unmapped_regions)
    last_success = now if outcome.reachable else _parse_z(previous.get("last_success_at"))
    rows: list[ExternalSignalRow] = []
    failures = "; ".join(outcome.document_failures)[:2000] or None
    for code in sorted(set(regions)):
        state = outcome.state
        reason = outcome.reason
        if state in {"ok", "stale_feed", "incomplete"} and code in unmapped:
            state = "unmapped"
            reason = (
                f"region {code} lists no counties in the operator profile, so no KMD warning can "
                "be attributed to it however healthy the feed is"
            )
        row, _ = _upsert(session, operator_id=operator_id, external_id=f"feed:{code}", now=now)
        row.source_url = feed_url
        row.region_code = code
        row.county = None
        row.site_id = None
        row.fetched_at = last_success or now
        row.valid_from = None
        # ALREADY EXPIRED, on purpose — see the module docstring. Never counted as a warning
        # by services/dashboards._count_live; never lets the CAP block read as fresh.
        row.valid_until = row.fetched_at
        row.stale = 1
        row.confidence = 1.0 if state in {"ok", "stale_feed", "incomplete", "unmapped"} else 0.0
        row.storm_flag = row.flood_flag = row.planned_power = row.access_risk = 0
        if outcome.reachable:
            row.payload_json = _dumps(
                {"feed_url": feed_url, "items_in_feed": outcome.items_in_feed, "not_modified": outcome.not_modified}
            )
        # On failure payload_json is left as it was: the last good row keeps its payload.
        row.derived_json = _dumps(
            {
                "kind": KIND_CAP_FEED,
                "state": state,
                "reachable": outcome.reachable,
                "feed_stale": outcome.feed_stale,
                "newest_sent": _z(outcome.newest_sent),
                "feed_age_days": outcome.feed_age_days,
                "cap_stale_days": stale_days,
                "reason": reason,
                "attempted_at": _z(now),
                "last_success_at": _z(last_success),
                "last_modified": outcome.last_modified,
                "seen_links": outcome.seen_links[-_SEEN_LINKS_CAP:],
                "pending_links": outcome.pending_links[-_SEEN_LINKS_CAP:],
                "document_failures": outcome.document_failures[:20],
                "documents_deferred": outcome.documents_deferred,
                "xml_parser": XML_PARSER,
            }
        )
        # A run-level error (unreachable, misconfigured) wins; otherwise the documents that
        # could not be read are the error (review finding F08): never a silent None beside 0.
        row.last_error = outcome.error or failures
        rows.append(row)
    return rows


def _parse_z(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value[:-1] if value.endswith("Z") else value)
    except ValueError:
        return None


def _mark_live_alerts(session: Session, operator_id: str, *, blind_reason: str | None, now: datetime) -> int:
    """While the feed cannot be read, every alert still in force is marked ``stale`` and says why.

    NOT touched: the payload, the severity and ``valid_until`` -- KMD's own ``expires``. A
    warning KMD issued does not stop being in force because we cannot reach KMD, so it keeps
    being counted (``services/dashboards._count_live`` counts on ``valid_until`` alone).

    What changes is our ability to vouch for it: a cancellation issued since the outage began
    would be invisible. ``stale=1`` is this table's word for exactly that, and it is what stops
    a three-day-old alert row from being the "fresh signal" that lets the Regions dashboard
    paint a region CALM while the feed is dark. When the feed answers again both are cleared.
    Returns how many rows were touched.
    """
    rows = session.scalars(
        select(ExternalSignalRow).where(
            ExternalSignalRow.operator_id == operator_id,
            ExternalSignalRow.source == CAP_SOURCE,
            ExternalSignalRow.valid_until > now,
            ExternalSignalRow.derived_json.like(f'%"kind":"{KIND_CAP_ALERT}"%'),
        )
    ).all()
    note = None
    if blind_reason is not None:
        note = (
            f"KMD feed not read since this alert was fetched ({blind_reason}); a cancellation or update "
            "issued meanwhile would not be visible here"
        )[:2000]
    for row in rows:
        row.stale = 0 if note is None else 1
        row.last_error = note
    return len(rows)


# ---------------------------------------------------------------------------- the job


def _publish(operator_id: str, region_code: str, *, stale: bool, storm_flag: bool, error: str | None, state: str) -> None:
    """``external_signal.updated`` (§7.3.2). A broken hub must not break a poll."""
    try:
        hub.publish_sync(
            RealtimeEvent(
                type="external_signal.updated",
                operator_id=operator_id,
                payload={
                    "source": CAP_SOURCE,
                    "region_code": region_code,
                    "stale": stale,
                    "storm_flag": storm_flag,
                    "flood_flag": False,
                    "state": state,
                    "error": error,
                },
            )
        )
    except Exception:  # noqa: BLE001 — advisory fan-out only
        log.exception("kmd_cap: publishing external_signal.updated failed (ignored)")


def _process_feed(
    session: Session,
    *,
    operator_id: str,
    provider: KmdCapProvider,
    feed: FeedFetch,
    mapping: Mapping[str, tuple[str, ...]],
    previous: Mapping[str, Any],
    outcome: CapRunOutcome,
    now: datetime,
    stale_days: int = DEFAULT_CAP_STALE_DAYS,
) -> None:
    """Fetch the documents not yet held, then store what they say **oldest first**.

    Two passes. The first gathers every document this run can read (fetching at most
    :data:`MAX_DOCS_PER_RUN`); the second processes them in ascending ``sent`` order, so an
    original is stored before the Update or Cancel that ends it even though an RSS feed lists
    newest first (review finding F07). What cannot be ordered away — the original arriving on a
    later run — is covered by the tombstone :func:`_end_referenced` leaves.
    """
    seen_before = set(previous.get("seen_links") or [])
    known = _known_alert_links(session, operator_id) | seen_before
    # The feed is authoritative for what is pending: this run's failures and deferrals replace
    # whatever the previous run carried (a link KMD has dropped from the feed is no longer owed).
    outcome.pending_links = []
    seen_now: list[str] = []
    sent_stamps: list[datetime] = []
    batch: list[tuple[datetime, int, str, CapAlert]] = []

    for position, item in enumerate(feed.items):
        link = item.link or f"inline:{item.guid}"
        alert: CapAlert | None = item.embedded_alert
        if alert is not None and (link in known or _alert_rows(session, operator_id, alert.identifier)):
            seen_now.append(link)
            if alert.sent is not None:
                sent_stamps.append(alert.sent)
            _refresh_hold(session, operator_id, alert.source_url or link, now, stale_days)
            continue
        if alert is None:
            if item.link in known:
                seen_now.append(item.link)
                _refresh_hold(session, operator_id, item.link, now, stale_days)
                continue
            if outcome.documents_fetched >= MAX_DOCS_PER_RUN:
                outcome.documents_deferred += 1
                outcome.pending_links.append(item.link)
                continue  # not in seen_now: fetched on a later run
            try:
                alert = provider.fetch_alert(item.link)
                outcome.documents_fetched += 1
            except WeatherError as exc:  # CapError is one; per-document failures never stop the run
                outcome.document_failures.append(f"{item.link}: {exc}")
                outcome.pending_links.append(item.link)
                continue
            except Exception as exc:  # noqa: BLE001 — a parser bug degrades, it does not crash
                log.exception("kmd_cap: unexpected failure reading %s", item.link)
                outcome.document_failures.append(f"{item.link}: unexpected {type(exc).__name__}: {exc}")
                outcome.pending_links.append(item.link)
                continue
        if alert.sent is not None:
            sent_stamps.append(alert.sent)
        seen_now.append(link)
        order = alert.sent or item.published or datetime.min
        batch.append((order, position, link, alert))

    for _, _, _, alert in sorted(batch, key=lambda b: (b[0], -b[1])):  # oldest first; ties: feed order reversed
        if not alert.is_actual:
            # Test / Exercise / System / Draft: KMD's own status says it is not a warning.
            outcome.skipped_not_actual += 1
            continue
        msg_type = (alert.msg_type or "").strip().lower()
        if msg_type in {"update", "cancel"}:
            ended, tombs = _end_referenced(session, operator_id=operator_id, alert=alert, now=now)
            outcome.alerts_ended += ended
            outcome.tombstones += tombs
        if msg_type == "cancel":
            continue  # a Cancel withdraws; it is not itself a warning
        rows, unknown = store_alert(
            session, operator_id=operator_id, alert=alert, mapping=mapping, now=now, stale_days=stale_days
        )
        outcome.alerts_stored += 1
        outcome.rows_written += len(rows)
        outcome.unmapped_areas.extend(unknown)
        for row in rows:
            if row.region_code and row.valid_until > now:
                outcome.regions_warned.add(row.region_code)
                if row.storm_flag:
                    outcome.storm_regions.add(row.region_code)

    outcome.seen_links = list(dict.fromkeys(seen_now))
    newest_candidates = [s for s in (feed.newest_published, *sent_stamps) if s is not None]
    outcome.newest_sent = max(newest_candidates) if newest_candidates else None


def _refresh_hold(session: Session, operator_id: str, link: str, now: datetime, stale_days: int) -> None:
    """A held alert (no ``expires``) still in the feed stays in force for another hold window
    — capped, like the first one, at ``sent + CAP_STALE_DAYS``."""
    rows = session.scalars(
        select(ExternalSignalRow).where(
            ExternalSignalRow.operator_id == operator_id,
            ExternalSignalRow.source == CAP_SOURCE,
            ExternalSignalRow.source_url == link,
        )
    ).all()
    for row in rows:
        block = _loads(row.derived_json)
        if block.get("kind") != KIND_CAP_ALERT or block.get("expires") or block.get("ended_by"):
            continue  # KMD gave an end, or withdrew it: never extended here
        if block.get("detached"):
            # The profile no longer maps this county to this region (reattribute_live). Extending
            # it would revive the region's copy every poll: its valid_until crept forward each run,
            # the backtest kept crediting the old region, and every poll reported a re-attribution
            # (review finding NEW2). Only a profile change that maps it again brings it back.
            continue
        sent = _parse_z(block.get("sent"))
        row.valid_until = max(row.valid_until or now, _held_until(sent, now, stale_days))


def _judge_staleness(outcome: CapRunOutcome, *, now: datetime, stale_days: int, previous: Mapping[str, Any]) -> None:
    """Set ``feed_stale`` / ``feed_age_days`` / ``state`` / ``reason`` for a feed that answered."""
    if outcome.not_modified:
        outcome.newest_sent = _parse_z(previous.get("newest_sent"))
    if outcome.newest_sent is None:
        outcome.feed_stale = True
        outcome.feed_age_days = None
        outcome.state = "stale_feed"
        outcome.reason = (
            "the feed answered but carries no dated item, so its currency cannot be judged; an "
            "empty feed is indistinguishable from a broken one and is not read as 'no warnings'"
        )
        _judge_completeness(outcome)
        return
    age_days = round((now - outcome.newest_sent).total_seconds() / 86400.0, 2)
    outcome.feed_age_days = age_days
    outcome.feed_stale = age_days > stale_days
    if outcome.feed_stale:
        outcome.state = "stale_feed"
        outcome.reason = (
            f"the newest item in the KMD feed is {age_days:g} days old (CAP_STALE_DAYS={stale_days}); "
            "the feed answers but is not current, so its silence is not evidence of calm"
        )
    else:
        outcome.state = "ok"
        outcome.reason = None
    _judge_completeness(outcome)


def _judge_completeness(outcome: CapRunOutcome) -> None:
    """A feed that answered while its documents could not all be read is not ``ok`` (F08).

    The one warning KMD published may be exactly the document that returned 503, so "0 alerts
    in force" beside ``ok`` would be the reassuring lie this lane exists to prevent. Documents
    still pending from an earlier run count too: nothing is ``ok`` while one is unread (NEW1).
    """
    failed, deferred = len(outcome.document_failures), outcome.documents_deferred
    carried = len(set(outcome.pending_links)) if not failed and not deferred else 0
    if not failed and not deferred and not carried:
        return
    if failed:
        missing = f"{failed} CAP document(s) listed in the feed could not be read" + (
            f" and {deferred} were deferred to the next run" if deferred else ""
        )
    elif deferred:
        missing = f"{deferred} CAP document(s) listed in the feed were deferred to the next run"
    else:
        # A 304 (or any run that did not read the feed) says nothing about documents an earlier
        # run could not read: they are still unread (review finding NEW1).
        missing = f"{carried} CAP document(s) an earlier run could not read have still not been read"
    note = f"{missing}; an alert may be missing, so a count of zero is not a statement that KMD is silent"
    if outcome.state == "ok":
        outcome.state = "incomplete"
        outcome.reason = note
    else:
        outcome.reason = f"{outcome.reason}; also: {note}" if outcome.reason else note


def poll(
    session: Session,
    settings: AppSettings,
    *,
    provider: KmdCapProvider | None = None,
    now: datetime | None = None,
) -> JobResult:
    """The ``kmd_cap`` job (§5.3.13). Never raises.

    The scheduler calls ``poll(session, settings)``; the keyword arguments are for tests and
    on-demand runs. ``provider`` defaults to :func:`~noc_agents.adapters.kmd_cap.provider_from_env`.
    """
    if not cap_enabled():
        return JobResult(
            summary=f"{JOB_NAME} skipped: {ENABLED_ENV} is not true",
            rationale="KMD CAP early warning is opt-in with the rest of the WeatherRiskAgent (§5.3.13); "
            "no request was made and no row was written",
            tools=({"name": "kmd_cap.poll", "ok": True, "skipped": True, "reason": f"{ENABLED_ENV} unset or false"},),
        )

    now = now or utcnow()
    cfg = settings.operator
    operator_id = cfg.operator_id
    regions = list(getattr(cfg, "regions", {}) or {})
    stale_days = cap_stale_days()
    outcome = CapRunOutcome()

    try:
        previous = _previous_health(session, operator_id)
    except Exception:  # noqa: BLE001 — a DB hiccup reading our own memory must not stop the run
        log.exception("kmd_cap: reading the previous feed-health row failed; continuing without it")
        session.rollback()
        previous = {}
    outcome.last_modified = previous.get("last_modified")
    outcome.seen_links = list(previous.get("seen_links") or [])
    # Documents an earlier run could not read stay owed until a run that reads the feed accounts
    # for them. They are carried through every run that does not — unreachable, misconfigured, a
    # 304 — so an outage can no longer erase them (review finding NEW1: an unreachable run wiped
    # the list, and the next conditional 304 then read "ok, 0 in force" with a warning unread).
    outcome.pending_links = list(previous.get("pending_links") or [])
    # While any are pending the conditional header is withheld, so the server must send the feed;
    # a 304 to that unconditional request is refused by the adapter as a protocol violation.
    retry_pending = bool(outcome.pending_links)

    problems = validate_county_map(cfg)
    fatal = [p for p in problems if p.fatal]
    outcome.county_problems = [p.as_dict() for p in problems]
    unmapped_regions = {p.region_code for p in problems if p.kind == "region_without_counties"}

    feed_url = getattr(provider, "feed_url", "") if provider is not None else ""
    if fatal:
        # §7.3.7 "rejects unknown counties at startup", enforced at the LANE's startup: nothing
        # is fetched or attributed while the mapping is wrong, and every region's feed-health
        # row says why, so the Regions dashboard shows CAP as STALE with the reason on it.
        outcome.state = "misconfigured"
        outcome.error = "config: " + "; ".join(p.message for p in fatal)
        outcome.error_kind = "config"
        outcome.reason = outcome.error
        log.error("kmd_cap: refusing to run — %s", outcome.error)
    else:
        try:
            provider = provider or provider_from_env()
            feed_url = provider.feed_url
            feed = provider.fetch_feed(if_modified_since=None if retry_pending else outcome.last_modified, now=now)
            outcome.reachable = True
            outcome.not_modified = feed.not_modified
            outcome.items_in_feed = len(feed.items)
            outcome.last_modified = feed.last_modified or outcome.last_modified
            if not feed.not_modified:
                _process_feed(
                    session, operator_id=operator_id, provider=provider, feed=feed,
                    mapping=county_region_map(cfg), previous=previous, outcome=outcome, now=now,
                    stale_days=stale_days,
                )
            else:
                for link in outcome.seen_links:
                    _refresh_hold(session, operator_id, link, now, stale_days)
            _judge_staleness(outcome, now=now, stale_days=stale_days, previous=previous)
            _mark_live_alerts(session, operator_id, blind_reason=None, now=now)
            session.commit()
        except WeatherError as exc:  # CapError is one
            session.rollback()
            outcome.reachable = False
            outcome.state = "unreachable"
            outcome.error, outcome.error_kind = str(exc)[:2000], exc.kind
            outcome.reason = f"KMD CAP feed unreachable: {outcome.error}"
        except Exception as exc:  # noqa: BLE001 — a bug must degrade to a labelled STALE, not a crash
            log.exception("kmd_cap: unexpected failure")
            session.rollback()
            outcome.reachable = False
            outcome.state = "unreachable"
            outcome.error = f"unexpected: {type(exc).__name__}: {exc}"[:2000]
            outcome.error_kind = "unexpected"
            outcome.reason = outcome.error
        if not outcome.reachable:
            log.warning("kmd_cap: %s", outcome.reason)

    if not outcome.reachable:
        # Blind this run (unreachable, or refused over the county map): what we last knew about
        # the feed carries forward unchanged, so its age keeps growing and nobody can mistake
        # it for a fresh read.
        outcome.newest_sent = _parse_z(previous.get("newest_sent"))
        outcome.feed_stale = True
        if outcome.newest_sent is not None:
            outcome.feed_age_days = round((now - outcome.newest_sent).total_seconds() / 86400.0, 2)

    if not fatal:
        # Every poll, reachable or not: the county map is local, so a profile fix must take
        # effect on the next run even while KMD is down (F10). Never while the map is invalid.
        try:
            outcome.reattributed = reattribute_live(
                session, operator_id=operator_id, mapping=county_region_map(cfg), now=now
            )
            session.commit()
        except Exception:  # noqa: BLE001 — re-attribution is best-effort; say so, never raise
            log.exception("kmd_cap: re-attributing live alerts failed")
            session.rollback()
            outcome.document_failures.append("re-attribution of live alerts failed (database error; see log)")

    try:
        if not outcome.reachable:
            _mark_live_alerts(session, operator_id, blind_reason=outcome.error, now=now)
        write_feed_health(
            session, operator_id=operator_id, regions=regions, unmapped_regions=unmapped_regions,
            outcome=outcome, feed_url=feed_url or os.getenv("KMD_CAP_FEED_URL") or "https://meteo.go.ke/api/cap/rss.xml",
            now=now, previous=previous, stale_days=stale_days,
        )
        session.commit()
    except Exception:  # noqa: BLE001 — the database itself is unhappy: say so, still do not raise
        log.exception("kmd_cap: writing feed-health rows failed")
        session.rollback()
        outcome.document_failures.append("feed-health rows could not be written (database error; see log)")

    for code in regions:
        state = outcome.state
        if state in {"ok", "stale_feed", "incomplete"} and code in unmapped_regions:
            state = "unmapped"
        _publish(
            operator_id, code, stale=state != "ok", storm_flag=code in outcome.storm_regions,
            error=outcome.error, state=state,
        )
    return _result(outcome, stale_days)


def _result(outcome: CapRunOutcome, stale_days: int) -> JobResult:
    parts = [f"KMD CAP feed {outcome.state}"]
    if outcome.reachable:
        if outcome.not_modified:
            parts.append("not modified since last poll")
        else:
            parts.append(f"{outcome.items_in_feed} items, {outcome.documents_fetched} documents fetched")
        if outcome.alerts_stored:
            parts.append(f"{outcome.alerts_stored} alerts stored as {outcome.rows_written} rows")
        if outcome.storm_regions:
            parts.append("STORM (KMD Severe/Extreme): " + ", ".join(sorted(outcome.storm_regions)))
        if outcome.feed_age_days is not None:
            parts.append(f"newest item {outcome.feed_age_days:g} d old (stale after {stale_days})")
    if outcome.document_failures:
        parts.append(f"{len(outcome.document_failures)} document failures")
    if outcome.documents_deferred:
        parts.append(f"{outcome.documents_deferred} deferred to the next run")
    if outcome.tombstones:
        parts.append(f"{outcome.tombstones} tombstone(s) for alerts ended before they were seen")
    if outcome.reattributed:
        parts.append(f"{outcome.reattributed} row(s) re-attributed to the current county map")
    if outcome.unmapped_areas:
        parts.append("unattributed areas: " + ", ".join(sorted(set(outcome.unmapped_areas))[:5]))
    if outcome.error:
        parts.append(outcome.error[:300])
    rationale = (
        "Advisory only (§7.3): KMD's warnings are stored verbatim with valid_until = the document's expires; "
        "nothing is re-scored or extended. Every region gets a feed-health row (already expired, never counted "
        "as a warning) stating whether the feed was reached and whether it is current, so an unreachable or "
        f"stale feed reads STALE, never 'no warnings'. XML parsed with {XML_PARSER} behind a size cap and an "
        "entity pre-scan."
    )
    return JobResult(summary="; ".join(parts), rationale=rationale, tools=(outcome.as_tool(),))


#: The scheduler card (§4.4 roster). **Not wired into** ``scheduler.loop.SCHEDULED_JOBS`` —
#: that file belongs to another lane; the one-line change is in the lane report. ``poll``
#: re-checks ``WEATHER_ENABLED`` itself, so wiring it can never start requests on a machine
#: that did not opt in, and ``default_enabled=False`` makes /scheduler/status say "off" too.
CAP_JOB = JobCard(
    JOB_NAME, INTERVAL_S, poll, ENABLED_ENV, AGENT, GRAPH_NAME,
    max_seconds=120,  # 1 feed + MAX_DOCS_PER_RUN (10) documents x the 10 s per-call budget = 110 s worst case
    default_enabled=False,  # WEATHER_ENABLED unset means OFF (spec Appendix B)
)
