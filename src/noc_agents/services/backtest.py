"""Early-warning backtest: was the flag right? (spec §10.6, §7.3.3, M6; CONFORMANCE C-14).

§10.6 exists to close "the untested value claim". A wallboard that shows a storm flag beside a
region invites the floor to act on it; if that flag has been wrong nine times in ten, acting
on it is worse than ignoring it, and nobody would know. So the measured precision is shown
*beside* the flag, and this module is where it is measured — by replaying the
``external_signals`` rows the pollers stored against the incidents that actually happened.

Everything here is a database read. No network, no adapter, no poller import.

Three ideas carry the whole module
----------------------------------
**1. A flag is an EPISODE, not a row.** The forecast poller writes a row every 15 minutes; a
three-hour storm warning is twelve flagged rows. Counting rows as "flags raised" would make
the denominator of precision a function of the polling cadence — halve the interval and
precision halves — which is not a measurement of the flag at all. Flagged rows are therefore
merged into episodes: each row claims a window (a forecast row: ``fetched_at`` + its risk
horizon, 6 h; a KMD alert: until its ``expires``; a flood reading: to the end of its forecast
week), and overlapping claims in one region are one episode. A later **calm** reading cuts a
forecast's claim — the forecast was withdrawn and from then on the floor saw calm — so a flag
retracted after fifteen minutes is not credited with the next six hours (review finding F14).
An episode *starts* when the flag was first stored — the moment the floor could first have
known, which is also where the M6 lead time is measured from.

**2. Not enough data is an answer, said out loud** (the ``services/capacity.py`` precedent,
verbatim in spirit). Precision over three episodes is not a precision, it is three anecdotes,
and a 0.67 on a wallboard gets quoted in a meeting. Two floors, both reported on every score
next to what was actually observed, and below either one the verdict is
``INSUFFICIENT_DATA`` and the percentage is **withheld**, not rounded, not greyed:

* ``min_history_days`` = **90** — §10.6's own words: "Until 90 days of data exist, the strip
  shows 'precision: not yet measured'". Measured from the earliest stored row of the family
  for the region, flagged or not (it is evidence the poller was running, not that it fired),
  **to the end of the window** — ``min(until, now)`` — so a historical window is judged on the
  history that existed then, however late it is scored (review finding F19).
* ``min_episodes`` = **10** resolved episodes in the window. This one is a **judgement, not a
  theorem**, and it is configurable. The numbers behind it, as 95 % Wilson intervals: the
  widest interval (a half-right record) is ±0.35 at n = 4, ±0.30 at n = 7, ±0.26 at n = 10
  and ±0.23 at n = 15; an all-miss record's upper bound is 0.49 at n = 4, 0.35 at n = 7,
  0.28 at n = 10. Ten is the round number at which a flag that has never once been right can
  no longer be read as better than about one in four, and at which §10.6's 0.2 line starts to
  mean something. Below it the interval is wide enough to contain almost any answer. The
  interval is published with every measured score so nobody mistakes ten for plenty.

Recall has the same shape with ``min_incidents`` = 10. **Lift and median lead time are withheld
whenever precision is** (review finding F13): they are ratios over the same thin sample, and
one flag with one incident inside it computes to a "lift" of 119.

**The overall row pools only regions that pass the floors on their own** (review finding F05;
:func:`replay` explains), and its lift is computed in region-hours (review finding F12).

**3. An open question is not a wrong answer.** An episode whose claim window has not closed
yet cannot have "failed to be followed by an incident"; it is ``pending`` and excluded from
precision until it resolves. Otherwise every flag raised in the last six hours would count as
a false alarm and a live storm would drag its own precision down while it was happening. The
converse holds too: an episode raised just before ``until`` is judged against incidents up to
the end of its own claim, not cut off at ``until`` (review finding F11).

What precision does not tell you — and what is reported so it can be seen
-------------------------------------------------------------------------
A busy region has *some* incident in most six-hour windows whether or not it rains, so a
precision of 0.8 can be pure base rate. Each score therefore also carries ``lift``: the
incident rate inside flagged windows divided by the rate outside them. Lift ≈ 1 means the
flag is not telling you anything the calendar would not; lift well above 1 is the flag earning
its place. It is not a verdict input — §10.6 defines the label on precision — but a number
shown without its base rate is exactly the kind of untested claim this module exists to stop.

Verdicts: ``MEASURED``, ``LOW_CONFIDENCE`` (measured, below ``min_precision``: §10.6 says
label it "text + grey" rather than hide it, so the number IS published) and
``INSUFFICIENT_DATA``.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from noc_agents.db.models import ExternalSignalRow, IncidentRow, utcnow
from noc_agents.services.signals import CAP_SOURCE, FLOOD_SOURCE, KIND_CAP_ALERT, WEATHER_SOURCES

log = logging.getLogger("noc_agents.services.backtest")

__all__ = [
    "DEFAULT_BACKTEST",
    "FAMILIES",
    "VERDICT_INSUFFICIENT_DATA",
    "VERDICT_LOW_CONFIDENCE",
    "VERDICT_MEASURED",
    "Episode",
    "Score",
    "backtest_config",
    "build_episodes",
    "precision_verdict_30d",
    "replay",
    "score_region",
    "signal_precision_30d",
    "wilson_interval",
]

VERDICT_MEASURED = "MEASURED"
VERDICT_LOW_CONFIDENCE = "LOW_CONFIDENCE"
VERDICT_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"

#: Which rows make up each flag family, and which column is the flag.
#:
#: ``storm`` is the forecast poller's flag — the one the Regions dashboard shows
#: ``precision_30d`` beside, so it is the one :func:`signal_precision_30d` measures. KMD CAP
#: warnings also set ``storm_flag`` (§7.3.1) but are scored as their own family: a Met
#: Department warning and a model threshold are different claims by different authors, and
#: blending them would let a good one hide a bad one.
FAMILIES: dict[str, tuple[tuple[str, ...], str]] = {
    "storm": (WEATHER_SOURCES, "storm_flag"),
    "cap": ((CAP_SOURCE,), "storm_flag"),
    "flood": ((FLOOD_SOURCE,), "flood_flag"),
}

#: The data floors and the label threshold. Every value is reported on every score.
DEFAULT_BACKTEST: dict[str, Any] = {
    # §10.6: "Until 90 days of data exist, the strip shows 'precision: not yet measured'".
    "min_history_days": 90,
    # Idea 2 in the module docstring: a judgement, with the Wilson-interval numbers behind it
    # written out there. UNVERIFIED as policy; not a spec number.
    "min_episodes": 10,
    "min_incidents": 10,
    # §10.6: "if precision for a region stays below cfg.weather.min_precision (default 0.2) for
    # 30 days, the strip labels that region's flag 'LOW CONFIDENCE'".
    "min_precision": 0.2,
    # The forecast flag's claim horizon when a row does not say (WeatherThresholds.horizon_hours).
    "storm_horizon_hours": 6,
    # A flood reading claims its forecast week (FloodThresholds.horizon_days).
    "flood_horizon_days": 7,
}

#: Looking back this far before ``since`` finds rows whose claim window reaches into the
#: window (a flood reading claims seven days). Longer than every family's horizon.
_LOOKBACK_PAD = timedelta(days=8)


def backtest_config(cfg: Any | None = None) -> dict[str, Any]:
    """``cfg.weather``'s backtest keys over :data:`DEFAULT_BACKTEST`.

    ``OperatorConfig`` declares no ``weather`` field yet, and pydantic's default
    ``extra="ignore"`` drops an undeclared block silently — so this reads with ``getattr`` and
    falls back to the defaults, and starts honouring the profile the moment ``config.py``
    grows the field (the pattern ``services/capacity.capacity_config`` uses).
    """
    raw = getattr(cfg, "weather", None) if cfg is not None else None
    block: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    merged = dict(DEFAULT_BACKTEST)
    for key in merged:
        if key in block and isinstance(block[key], (int, float)) and not isinstance(block[key], bool):
            merged[key] = block[key]
    return merged


# ---------------------------------------------------------------------------- statistics


def wilson_interval(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """95 % Wilson score interval for ``k`` successes in ``n``; ``None`` for ``n == 0``.

    Wilson rather than the textbook ``p ± z·√(p(1-p)/n)`` because the textbook interval
    collapses to zero width at 0/n and n/n — it would report "0 % ± 0" for a flag that has
    missed three times — which is the small-sample dishonesty this module exists to avoid.
    """
    if n <= 0:
        return None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (round(max(0.0, centre - half), 3), round(min(1.0, centre + half), 3))


# ---------------------------------------------------------------------------- episodes


@dataclass
class Episode:
    """One flag, as the floor would have experienced it: first stored at ``start``, claiming
    until ``end``, made of ``rows`` stored rows from ``sources``."""

    region_code: str
    start: datetime
    end: datetime
    rows: int = 1
    sources: set[str] = field(default_factory=set)
    incidents: list[str] = field(default_factory=list)

    def covers(self, when: datetime) -> bool:
        return self.start <= when <= self.end


def _claim_end(row: Any, family: str, config: Mapping[str, Any]) -> datetime:
    """How long one flagged row claims danger for, before any later calm reading cuts it."""
    base = row.valid_until or row.fetched_at
    try:
        block = json.loads(row.derived_json or "{}")
    except ValueError:
        block = {}
    if not isinstance(block, dict):
        block = {}
    if family == "storm":
        hours = block.get("horizon_hours") if isinstance(block.get("horizon_hours"), (int, float)) else config["storm_horizon_hours"]
        return max(base, row.fetched_at + timedelta(hours=float(hours)))
    if family == "flood":
        until = block.get("window_until")
        if isinstance(until, str):
            try:
                return max(base, datetime.fromisoformat(until[:10]))
            except ValueError:
                pass
        return max(base, row.fetched_at + timedelta(days=float(config["flood_horizon_days"])))
    return base  # cap: KMD's own expires (already shortened by any Update/Cancel)


def _stream(row: Any, family: str) -> tuple[str, str | None]:
    """Which readings supersede one another. A forecast for a region is superseded by the next
    forecast for that region from ANY weather source — the live strip shows the newest one,
    whichever provider it came from; a flood reading by the next reading of the same site."""
    return (row.region_code, getattr(row, "site_id", None) if family == "flood" else None)


def build_episodes(
    rows: Iterable[Any],
    *,
    family: str,
    config: Mapping[str, Any] | None = None,
) -> list[Episode]:
    """Merge flagged rows into episodes per region (idea 1). Rows need not be sorted.

    ``rows`` may include **calm** readings (flag 0, with a derived block). They are never
    episodes themselves; they *cut* claims. For the forecast families (``storm``, ``flood``) a
    flagged row claims its horizon only until the next calm reading of the same stream — the
    forecast was withdrawn, and from then on the floor saw calm (review finding F14: without
    the cut, a flag retracted at 00:15 kept "warning" until 06:00 and absorbed a separate flag
    raised at 05:00 into one episode). KMD warnings are not cut by anything but KMD: their
    claim is their ``expires``, already shortened by any Update or Cancel.
    """
    config = config or DEFAULT_BACKTEST
    flag_col = FAMILIES[family][1]
    streams: dict[tuple[str, str | None], list[Any]] = {}
    for row in rows:
        if not row.region_code or row.fetched_at is None:
            continue
        streams.setdefault(_stream(row, family), []).append(row)

    spans_by_region: dict[str, list[tuple[datetime, datetime, str]]] = {}
    for (region, _), stream in streams.items():
        stream.sort(key=lambda r: r.fetched_at)
        # A row without the flag attribute is treated as flagged: the older calling contract was
        # "pass the flagged rows", and a caller that still does must not crash.
        calm_times = [r.fetched_at for r in stream if not getattr(r, flag_col, 1) and r.derived_json is not None]
        for row in stream:
            if not getattr(row, flag_col, 1):
                continue
            end = _claim_end(row, family, config)
            if family in ("storm", "flood"):
                cut = next((t for t in calm_times if t > row.fetched_at), None)
                if cut is not None and cut < end:
                    end = cut
            spans_by_region.setdefault(region, []).append((row.fetched_at, end, row.source))

    episodes: list[Episode] = []
    for region, spans in sorted(spans_by_region.items()):
        spans.sort()
        current: Episode | None = None
        for start, end, source in spans:
            if current is not None and start <= current.end:
                current.end = max(current.end, end)
                current.rows += 1
                current.sources.add(source)
                continue
            current = Episode(region_code=region, start=start, end=end, sources={source})
            episodes.append(current)
    return episodes


def _family_rows(
    session: Session,
    operator_id: str,
    *,
    family: str,
    since: datetime,
    until: datetime,
    now: datetime,
    region_code: str | None = None,
) -> list[Any]:
    """The flagged rows of a family, plus — for the forecast families — the calm readings that
    can cut their claims (:func:`build_episodes`). Only the columns the arithmetic needs are
    selected: a forecast row's ``payload_json`` is the provider's whole body, and a 30-day
    region has thousands of calm rows."""
    sources, flag_col = FAMILIES[family]
    flag = getattr(ExternalSignalRow, flag_col)
    cols = (
        ExternalSignalRow.fetched_at, ExternalSignalRow.valid_until, ExternalSignalRow.region_code,
        ExternalSignalRow.site_id, ExternalSignalRow.source, flag.label(flag_col), ExternalSignalRow.derived_json,
    )
    base = [
        ExternalSignalRow.operator_id == operator_id,
        ExternalSignalRow.source.in_(sources),
        ExternalSignalRow.region_code.is_not(None),
    ]
    if region_code is not None:
        base.append(ExternalSignalRow.region_code == region_code)
    flagged = select(*cols).where(
        *base, flag == 1, ExternalSignalRow.fetched_at >= since - _LOOKBACK_PAD, ExternalSignalRow.fetched_at < until,
    )
    if family == "cap":
        # Only real warnings: feed-health rows and tombstones never carry a flag, but say so.
        flagged = flagged.where(ExternalSignalRow.derived_json.like(f'%"kind":"{KIND_CAP_ALERT}"%'))
    rows = list(session.execute(flagged).all())
    if family in ("storm", "flood"):
        # A calm reading after ``until`` still cuts a claim that runs past it.
        calm_until = min(now, until + _LOOKBACK_PAD)
        calm_cols = cols[:6] + (ExternalSignalRow.derived_json.is_not(None).label("derived_json"),)
        calm = select(*calm_cols).where(
            *base, flag == 0, ExternalSignalRow.derived_json.is_not(None),
            ExternalSignalRow.fetched_at >= since - _LOOKBACK_PAD, ExternalSignalRow.fetched_at < calm_until,
        )
        rows += list(session.execute(calm).all())
    return rows


def _history_start(
    session: Session, operator_id: str, *, family: str, until: datetime, region_code: str | None = None
) -> datetime | None:
    """The earliest stored row of the family (flagged or not) before ``until``: when the poller
    started looking, as it stood at the end of the window being judged (review finding F19)."""
    sources, _ = FAMILIES[family]
    stmt = select(func.min(ExternalSignalRow.fetched_at)).where(
        ExternalSignalRow.operator_id == operator_id,
        ExternalSignalRow.source.in_(sources),
        ExternalSignalRow.fetched_at < until,
    )
    if region_code is not None:
        stmt = stmt.where(ExternalSignalRow.region_code == region_code)
    return session.scalar(stmt)


#: The instant an incident started, as the rest of the codebase reads it
#: (``services/scorecard.py``: failure_time, else outage_start_at, else created_at).
_INCIDENT_AT = func.coalesce(IncidentRow.failure_time, IncidentRow.outage_start_at, IncidentRow.created_at)


def _incidents(
    session: Session,
    operator_id: str,
    *,
    since: datetime,
    until: datetime,
    region_codes: Sequence[str] | None = None,
) -> list[tuple[str, str, str, datetime]]:
    """``(id, incident_number, region_code, started_at)`` for incidents that started in ``[since, until)``."""
    stmt = select(IncidentRow.id, IncidentRow.incident_number, IncidentRow.region_code, _INCIDENT_AT).where(
        IncidentRow.operator_id == operator_id,
        _INCIDENT_AT >= since,
        _INCIDENT_AT < until,
    )
    if region_codes is not None:
        stmt = stmt.where(IncidentRow.region_code.in_(tuple(region_codes)))
    return [(r[0], r[1], r[2], r[3]) for r in session.execute(stmt).all() if r[3] is not None]


def _incident_horizon(episodes: Iterable[Episode], *, since: datetime, until: datetime, now: datetime) -> datetime:
    """How far incidents must be loaded to judge every episode raised in the window: to the end
    of the latest such claim (never past now), not merely to ``until`` (review finding F11 — an
    incident one hour after ``until``, inside a claim raised before it, used to be invisible and
    the episode was scored a false alarm)."""
    ends = [e.end for e in episodes if since <= e.start < until]
    return max([until, *[min(end, now) for end in ends]])


# ---------------------------------------------------------------------------- scoring


@dataclass
class Score:
    """One family × one region (or ``overall``) over one window. Every floor travels with it."""

    family: str
    region_code: str
    since: datetime
    until: datetime
    history_days: float | None
    episodes: int = 0
    episodes_resolved: int = 0
    episodes_pending: int = 0
    episodes_hit: int = 0
    incidents: int = 0
    incidents_warned: int = 0
    flagged_hours: float = 0.0
    precision: float | None = None
    precision_ci95: tuple[float, float] | None = None
    recall: float | None = None
    recall_ci95: tuple[float, float] | None = None
    lift: float | None = None
    median_lead_hours: float | None = None
    verdict: str = VERDICT_INSUFFICIENT_DATA
    recall_verdict: str = VERDICT_INSUFFICIENT_DATA
    reason: str | None = None
    label: str | None = None
    contributing_regions: list[str] | None = None  # overall row only: the PRECISION pool
    dropped_regions: dict[str, str] | None = None  # overall row only: region -> why it left the precision pool
    recall_contributing_regions: list[str] | None = None  # overall row only: the RECALL pool
    recall_dropped_regions: dict[str, str] | None = None  # overall row only: region -> why it left the recall pool
    config: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        def z(dt: datetime | None) -> str | None:
            return dt.replace(microsecond=0).isoformat() + "Z" if dt else None

        return {
            "family": self.family,
            "region_code": self.region_code,
            "since": z(self.since),
            "until": z(self.until),
            "verdict": self.verdict,
            "label": self.label,
            "precision": self.precision,
            "precision_ci95": list(self.precision_ci95) if self.precision_ci95 else None,
            "recall": self.recall,
            "recall_ci95": list(self.recall_ci95) if self.recall_ci95 else None,
            "recall_verdict": self.recall_verdict,
            "lift": self.lift,
            "median_lead_hours": self.median_lead_hours,
            "episodes": self.episodes,
            "episodes_resolved": self.episodes_resolved,
            "episodes_pending": self.episodes_pending,
            "episodes_hit": self.episodes_hit,
            "incidents": self.incidents,
            "incidents_warned": self.incidents_warned,
            "flagged_hours": round(self.flagged_hours, 2),
            "history_days": self.history_days,
            "reason": self.reason,
            "contributing_regions": self.contributing_regions,
            "dropped_regions": self.dropped_regions,
            "recall_contributing_regions": self.recall_contributing_regions,
            "recall_dropped_regions": self.recall_dropped_regions,
            "floors": {
                "min_history_days": self.config.get("min_history_days"),
                "min_episodes": self.config.get("min_episodes"),
                "min_incidents": self.config.get("min_incidents"),
                "min_precision": self.config.get("min_precision"),
            },
        }


def _union_hours(episodes: Iterable[Episode], since: datetime, until: datetime) -> float:
    """Hours of ``[since, until)`` covered by at least one episode (episodes may overlap)."""
    spans = sorted((max(e.start, since), min(e.end, until)) for e in episodes)
    total = 0.0
    cur_s: datetime | None = None
    cur_e: datetime | None = None
    for s, e in spans:
        if e <= s:
            continue
        if cur_e is None or s > cur_e:
            if cur_e is not None and cur_s is not None:
                total += (cur_e - cur_s).total_seconds()
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None and cur_s is not None:
        total += (cur_e - cur_s).total_seconds()
    return total / 3600.0


def _score(
    *,
    family: str,
    region_code: str,
    regions: Sequence[str],
    episodes: list[Episode],
    incidents: list[tuple[str, str, str, datetime]],
    since: datetime,
    until: datetime,
    now: datetime,
    history_start: datetime | None,
    config: Mapping[str, Any],
) -> Score:
    """Score ``episodes`` against ``incidents`` over ``[since, until)`` for the given ``regions``.

    ``incidents`` may run past ``until`` (to the end of the latest claim, :func:`_incident_horizon`):
    those later ones can only make an episode a *hit*; recall, lift and the incident counts use
    the window's incidents alone.
    """
    horizon = min(until, now)
    score = Score(
        family=family, region_code=region_code, since=since, until=until,
        # Measured to the end of the window, not to today (review finding F19): a window that
        # ended when the poller had run 40 days is judged on 40 days, however late it is scored.
        history_days=round((horizon - history_start).total_seconds() / 86400.0, 2) if history_start else None,
        config=dict(config),
    )
    scope = set(regions)
    episodes = [e for e in episodes if e.region_code in scope]
    by_region: dict[str, list[Episode]] = {}
    for ep in episodes:
        ep.incidents = []
        by_region.setdefault(ep.region_code, []).append(ep)
    in_scope = [i for i in incidents if i[2] in scope]

    # Hits: every loaded incident, including those after ``until`` but inside a claim (F11).
    for inc_id, number, region, started in in_scope:
        for e in by_region.get(region, ()):
            if e.covers(started):
                e.incidents.append(number or inc_id)

    # Precision: episodes RAISED in the window whose claim has closed.
    raised = [e for e in episodes if since <= e.start < until]
    resolved = [e for e in raised if e.end <= now]
    score.episodes = len(raised)
    score.episodes_pending = len(raised) - len(resolved)
    score.episodes_resolved = len(resolved)
    score.episodes_hit = sum(1 for e in resolved if e.incidents)

    # Recall and lead: the window's incidents only.
    window = [i for i in in_scope if since <= i[3] < until]
    leads: list[float] = []
    warned = 0
    for _, _, region, started in window:
        hit = [e for e in by_region.get(region, ()) if e.covers(started)]
        if hit:
            warned += 1
            leads.append((started - min(hit, key=lambda e: e.start).start).total_seconds() / 3600.0)
    score.incidents = len(window)
    score.incidents_warned = warned

    # Lift in REGION-hours (review finding F12): flagged hours and unflagged hours are summed
    # per region, so an incident in an unflagged region during another region's flag counts
    # against that region's own unflagged time, not against a shared wall clock.
    period_h = max(0.0, (horizon - since).total_seconds() / 3600.0)
    inside_h = sum(_union_hours(eps, since, horizon) for eps in by_region.values())
    outside_h = period_h * len(scope) - inside_h
    score.flagged_hours = inside_h
    outside_n = len(window) - warned
    lift = None
    if inside_h > 0 and outside_h > 0 and outside_n > 0:
        lift = round((warned / inside_h) / (outside_n / outside_h), 2)
    lead = round(median(leads), 2) if leads else None

    # ---- the floors (idea 2): below either, withhold the number and say why
    reasons: list[str] = []
    if score.history_days is None:
        reasons.append("no stored signal history for this scope")
    elif score.history_days < config["min_history_days"]:
        reasons.append(
            f"signal history covers {score.history_days:g} days at the end of the window; §10.6 publishes "
            f"no precision until {config['min_history_days']} days exist"
        )
    if score.episodes_resolved < config["min_episodes"]:
        reasons.append(
            f"{score.episodes_resolved} resolved flag episode(s) in the window "
            f"(+{score.episodes_pending} still open); a precision needs at least {config['min_episodes']}"
        )
    if reasons:
        score.verdict = VERDICT_INSUFFICIENT_DATA
        score.label = "precision: not yet measured"
        score.reason = "; ".join(reasons)
        # Lift and lead are ratios over the same thin sample, withheld with precision (review
        # finding F13): one flag and one incident is a "lift" of 119, and it gets quoted.
    else:
        score.lift, score.median_lead_hours = lift, lead
        score.precision = round(score.episodes_hit / score.episodes_resolved, 3)
        score.precision_ci95 = wilson_interval(score.episodes_hit, score.episodes_resolved)
        if score.precision < config["min_precision"]:
            score.verdict = VERDICT_LOW_CONFIDENCE
            score.label = "LOW CONFIDENCE"
            score.reason = (
                f"{score.episodes_hit} of {score.episodes_resolved} flag episodes were followed by an incident, "
                f"below min_precision {config['min_precision']:g} (§10.6): shown, greyed, never hidden"
            )
        else:
            score.verdict = VERDICT_MEASURED
            score.label = None
            score.reason = f"{score.episodes_hit} of {score.episodes_resolved} flag episodes were followed by an incident"

    history_ok = score.history_days is not None and score.history_days >= config["min_history_days"]
    if history_ok and score.incidents >= config["min_incidents"]:
        score.recall = round(score.incidents_warned / score.incidents, 3)
        score.recall_ci95 = wilson_interval(score.incidents_warned, score.incidents)
        score.recall_verdict = VERDICT_MEASURED
    return score


def _load(
    session: Session,
    operator_id: str,
    *,
    family: str,
    since: datetime,
    until: datetime,
    now: datetime,
    config: Mapping[str, Any],
    region_code: str | None = None,
    regions: Sequence[str] | None = None,
) -> tuple[list[Episode], list[tuple[str, str, str, datetime]]]:
    """Episodes and incidents for a scope, incidents loaded to the latest claim's end (F11)."""
    rows = _family_rows(session, operator_id, family=family, since=since, until=until, now=now, region_code=region_code)
    episodes = build_episodes(rows, family=family, config=config)
    horizon = _incident_horizon(episodes, since=since, until=until, now=now)
    codes = [region_code] if region_code is not None else regions
    return episodes, _incidents(session, operator_id, since=since, until=horizon, region_codes=codes)


def score_region(
    session: Session,
    operator_id: str,
    region_code: str,
    *,
    family: str = "storm",
    since: datetime,
    until: datetime | None = None,
    now: datetime | None = None,
    config: Mapping[str, Any] | None = None,
) -> Score:
    """Precision and recall of one flag family for one region over ``[since, until)``. Read-only."""
    if family not in FAMILIES:
        raise ValueError(f"unknown signal family {family!r}; one of {sorted(FAMILIES)}")
    now = now or utcnow()
    until = until or now
    config = dict(config or backtest_config())
    episodes, incidents = _load(
        session, operator_id, family=family, since=since, until=until, now=now, config=config, region_code=region_code
    )
    return _score(
        family=family, region_code=region_code, regions=[region_code], episodes=episodes, incidents=incidents,
        since=since, until=until, now=now,
        history_start=_history_start(session, operator_id, family=family, until=until, region_code=region_code),
        config=config,
    )


def replay(
    session: Session,
    operator_id: str,
    *,
    since: datetime,
    until: datetime | None = None,
    now: datetime | None = None,
    regions: Sequence[str] | None = None,
    families: Sequence[str] = ("storm", "cap", "flood"),
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The full §10.6 report: per family, per region and overall. Read-only; never writes.

    ``regions`` defaults to every region of the active operator profile, so a region that has
    never raised a flag is still in the report — with ``INSUFFICIENT_DATA`` and its reason —
    rather than silently missing, which would read as "no problems there".

    **The overall row pools only regions that pass the floors on their own** (review finding
    F05). It used to take its history from the oldest row in *any* region, so one old calm row
    in WNY let twelve episodes from a 20-day-old NBI publish a precision NBI itself withheld.
    Now a region whose own score is ``INSUFFICIENT_DATA`` contributes nothing to the pooled
    precision — no episodes, no hours — and ``dropped_regions`` says which and why. Pooling can
    still add information across regions that each stand up; it can no longer launder one that
    does not.

    **Precision and recall are pooled separately** (review finding W14). They have different
    floors — precision needs resolved flag episodes, recall needs incidents — so a region can
    pass one and fail the other, and the pools must differ. Pooling recall over the precision
    pool dropped exactly the regions that matter most to recall: a region that never flags has
    no episodes, fails the precision floor, and is therefore where the missed incidents live.
    With WNY (30 incidents, never flagged) left out, NBI's 12 warned incidents read as a pooled
    recall of 1.0; the truth over both is 12/42. ``recall_contributing_regions`` and
    ``recall_dropped_regions`` show the recall pool the same way the precision pool is shown.
    """
    now = now or utcnow()
    until = until or now
    config = dict(config or backtest_config())
    if regions is None:
        from noc_agents.config import get_settings  # local: the report must work with an explicit list too

        regions = sorted(getattr(get_settings().operator, "regions", {}) or {})
    regions = list(regions)
    report: dict[str, Any] = {
        "operator_id": operator_id,
        "since": since.replace(microsecond=0).isoformat() + "Z",
        "until": until.replace(microsecond=0).isoformat() + "Z",
        "generated_at": now.replace(microsecond=0).isoformat() + "Z",
        "config": config,
        "families": {},
    }
    for family in families:
        if family not in FAMILIES:
            raise ValueError(f"unknown signal family {family!r}; one of {sorted(FAMILIES)}")
        episodes, incidents = _load(
            session, operator_id, family=family, since=since, until=until, now=now, config=config, regions=regions
        )
        per_region: dict[str, Any] = {}
        starts: dict[str, datetime | None] = {}
        for code in regions:
            starts[code] = _history_start(session, operator_id, family=family, until=until, region_code=code)
            per_region[code] = _score(
                family=family, region_code=code, regions=[code], episodes=episodes, incidents=incidents,
                since=since, until=until, now=now, history_start=starts[code], config=config,
            ).as_dict()
        def pooled(pool: list[str]) -> Score:
            return _score(
                family=family, region_code="ALL", regions=pool, episodes=episodes, incidents=incidents,
                since=since, until=until, now=now,
                # The youngest history in the pool: the pool is only as old as its newest member.
                history_start=max(s for c in pool if (s := starts[c]) is not None),
                config=config,
            )

        # Precision pool: regions that pass the PRECISION floors on their own (F05).
        contributors = [c for c in regions if per_region[c]["verdict"] != VERDICT_INSUFFICIENT_DATA]
        dropped = {c: per_region[c]["reason"] for c in regions if c not in contributors}
        # Recall pool: regions that pass the RECALL floors on their own (W14) — a different set.
        recall_pool = [c for c in regions if per_region[c]["recall_verdict"] == VERDICT_MEASURED]
        recall_dropped = {c: _recall_shortfall(per_region[c], config) for c in regions if c not in recall_pool}

        parts: list[str] = []
        if contributors:
            overall = pooled(contributors)
            parts.append(overall.reason or "")
            if dropped:
                parts.append(f"precision pooled from {', '.join(contributors)} only (dropped: {', '.join(sorted(dropped))})")
        else:
            overall = Score(family=family, region_code="ALL", since=since, until=until, history_days=None, config=dict(config))
            overall.label = "precision: not yet measured"
            parts.append(
                "no region passes the precision floors on its own; pooling regions that each fail them would "
                "publish a number none of them supports"
            )

        # The recall half comes from the recall pool alone, never from the precision pool.
        if recall_pool:
            recall = pooled(recall_pool)
            overall.recall, overall.recall_ci95 = recall.recall, recall.recall_ci95
            overall.recall_verdict = recall.recall_verdict
            overall.incidents, overall.incidents_warned = recall.incidents, recall.incidents_warned
            parts.append(
                f"recall pooled from {', '.join(recall_pool)}"
                + (f" (dropped: {', '.join(sorted(recall_dropped))})" if recall_dropped else "")
            )
        else:
            overall.recall = overall.recall_ci95 = None
            overall.recall_verdict = VERDICT_INSUFFICIENT_DATA
            # Counts are always shown, the ratio withheld: every region's incidents, as its own row counts them.
            overall.incidents = sum(per_region[c]["incidents"] for c in regions)
            overall.incidents_warned = sum(per_region[c]["incidents_warned"] for c in regions)
            parts.append(
                f"no region passes the recall floors on its own (>= {config['min_history_days']} days of history and "
                f">= {config['min_incidents']} incidents), so no pooled recall is published"
            )
        overall.reason = "; ".join(p for p in parts if p)
        overall.contributing_regions = contributors
        overall.dropped_regions = dropped
        overall.recall_contributing_regions = recall_pool
        overall.recall_dropped_regions = recall_dropped
        report["families"][family] = {"regions": per_region, "overall": overall.as_dict()}
    return report


def _recall_shortfall(score: Mapping[str, Any], config: Mapping[str, Any]) -> str:
    """Why a region's own recall is withheld, in the words of the floor it missed."""
    reasons = []
    history = score.get("history_days")
    if history is None:
        reasons.append("no stored signal history for this scope")
    elif history < config["min_history_days"]:
        reasons.append(f"signal history covers {history:g} days; recall needs {config['min_history_days']}")
    if score.get("incidents", 0) < config["min_incidents"]:
        reasons.append(f"{score.get('incidents', 0)} incident(s) in the window; recall needs at least {config['min_incidents']}")
    return "; ".join(reasons) or "recall withheld"


# ---------------------------------------------------------------------------- dashboard hooks


def precision_verdict_30d(
    session: Session,
    operator_id: str,
    region_code: str,
    *,
    family: str = "storm",
    now: datetime | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The full 30-day verdict for one region: number, label, interval, counts and reason.

    What ``GET /api/v1/signals/precision`` returns and what the strip's "LOW CONFIDENCE" /
    "precision: not yet measured" text is read from.
    """
    now = now or utcnow()
    return score_region(
        session, operator_id, region_code, family=family, since=now - timedelta(days=30), until=now, now=now,
        config=config,
    ).as_dict()


def signal_precision_30d(
    session: Session,
    operator_id: str,
    region_code: str,
    *,
    now: datetime | None = None,
    config: Mapping[str, Any] | None = None,
) -> float | None:
    """``signal_precision_30d`` (§10.6) for the Regions dashboard: a float, or ``None``.

    ``None`` means **not enough data** (the ``INSUFFICIENT_DATA`` verdict) — never zero, and
    never "no flags so nothing was wrong". A float means measured, including a
    ``LOW_CONFIDENCE`` one: §10.6 wants a low precision shown and greyed, not hidden, and the
    strip can tell it is low from ``min_precision``. The wire type is deliberately the one
    §7.4.2's example already shows (``"precision_30d": 0.4``), so wiring this in is a value
    change for the dashboard, never a contract change. Read-only; never raises on an empty
    database.
    """
    try:
        verdict = precision_verdict_30d(session, operator_id, region_code, now=now, config=config)
    except Exception:  # noqa: BLE001 — a dashboard tile must not 500 over an advisory number
        log.exception("backtest: precision_30d for %s failed; reporting not-measured", region_code)
        return None
    return verdict["precision"] if verdict["verdict"] != VERDICT_INSUFFICIENT_DATA else None
