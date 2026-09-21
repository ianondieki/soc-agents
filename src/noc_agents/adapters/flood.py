"""GloFAS river discharge via the Open-Meteo Flood API (spec §7.3.1, §7.3.3, §5.3.13).

This is the *only* module that talks to the flood API. ``pollers/flood.py`` calls it once a day
per **riverine** site and writes ``external_signals`` rows; everything else reads those rows.

The request (§7.3.3, verbatim)
------------------------------
``GET https://flood-api.open-meteo.com/v1/flood?latitude=&longitude=&daily=river_discharge,
river_discharge_mean&forecast_days=7`` — free, no key (§7.3.4), GloFAS (the Copernicus Global
Flood Awareness System) re-served by Open-Meteo (https://open-meteo.com/en/docs/flood-api).
Documented response shape: top-level ``latitude`` / ``longitude`` (the river-network grid cell
the point snapped to — GloFAS runs at 0.05°, ~5 km), ``daily_units`` and ``daily`` =
``{"time": ["YYYY-MM-DD", ...], "river_discharge": [...], "river_discharge_mean": [...]}``
in m³/s, every list the same length, ``null`` where the model has no value.

The rule, and what it actually measures
---------------------------------------
§7.3.1: ``flood_flag = river_discharge_max / river_discharge_mean ≥ flood_ratio (2.0)`` for
riverine sites. Implemented literally with the two variables §7.3.3 requests:

* numerator — the **peak** daily ``river_discharge`` over the horizon (7 days);
* divisor  — the **average** of daily ``river_discharge_mean`` over the same horizon.

Worth stating plainly, because a flag that escalates a Regions tile to ALERT deserves it:
Open-Meteo documents ``river_discharge_mean`` as a statistic over the GloFAS *ensemble
members* of this same forecast — not a long-run climatological normal for the river. So the
ratio as specified detects a **flood pulse inside the forecast week** (a peak day at least
twice the week's average flow). It will *not* flag a river that is already high and stays
high all week, because then peak and average are both high. Whether the operator wants a
climatological baseline instead is owner decision D14; until then the rule is the spec's,
unchanged, and every derived block records ``ratio_basis`` so a later tuning pass can see
exactly what was divided by what. The 2.0 threshold is the spec's **UNVERIFIED operational
starting value**, not a hydrological standard.

Grid snapping is the other honest limit: the site catalogue's coordinates are 2-dp town
centroids (``services/sites.py``), and a centroid can snap to a GloFAS cell that is not on the
river at all. The returned ``latitude``/``longitude`` are stored so that is checkable, and the
catalogue marks only one site riverine today (Kisumu, Winam Gulf / Kano-Nyando flood plain).

Fetch bounds
------------
The same two bounds as ``adapters/kmd_cap.py`` (review findings F06, F15): a total deadline on
the whole response, checked on every chunk (httpx's timeout is per socket read, so a slow drip
never trips it), and ``Accept-Encoding: identity`` with a compressed response refused unread,
so the size cap counts the bytes that would actually be parsed.

Failure handling
----------------
Every failure is one :class:`FloodError` — a :class:`~noc_agents.adapters.weather.WeatherError`
subclass with the same ``kind`` vocabulary (``timeout``, ``tls``, ``network``, ``http``,
``malformed``, ``oversize``, ``config``). Nothing here retries or sleeps. Tests inject an
``httpx.Client`` built on ``httpx.MockTransport`` (``tests/unit/test_flood.py``).
"""

from __future__ import annotations

import json
import logging
import math
import os
import ssl
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping
from urllib.parse import urlencode

import httpx

from noc_agents.adapters.weather import DEFAULT_TIMEOUT_S, WeatherError, default_client

log = logging.getLogger("noc_agents.adapters.flood")

__all__ = [
    "DEFAULT_FLOOD_BASE",
    "FLOOD_DAILY",
    "GLOFAS",
    "MAX_BODY_BYTES",
    "DailyDischarge",
    "FloodError",
    "FloodSnapshot",
    "FloodThresholds",
    "OpenMeteoFloodProvider",
    "derive_flood_risk",
    "glofas_discharge",
    "parse_flood",
    "provider_from_env",
]

GLOFAS = "GLOFAS"
DEFAULT_FLOOD_BASE = "https://flood-api.open-meteo.com"

#: The two daily variables §7.3.3 names. Nothing more is requested.
FLOOD_DAILY: tuple[str, ...] = ("river_discharge", "river_discharge_mean")

#: A 7-day, 2-variable response is under 2 KB. The cap is three orders of magnitude above that;
#: its job is to stop a runaway body, not to police a real one.
MAX_BODY_BYTES = 1 * 1024 * 1024

#: The clock the total fetch deadline is measured on. Indirected so a test can advance it.
_clock = time.monotonic


class FloodError(WeatherError):
    """One flood-API failure, classified. ``str(err)`` is safe for ``last_error``."""

    def __init__(self, kind: str, message: str, *, status: int | None = None) -> None:
        super().__init__(kind, message, status=status, source=GLOFAS)


@dataclass(frozen=True)
class DailyDischarge:
    """One forecast day. ``None`` means the model gave no value — never zero."""

    day: date
    discharge_m3s: float | None
    mean_m3s: float | None


@dataclass(frozen=True)
class FloodSnapshot:
    source: str
    source_url: str
    latitude: float  # the GloFAS cell the point snapped to, as returned
    longitude: float
    fetched_at: datetime  # naive UTC
    days: tuple[DailyDischarge, ...]
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FloodThresholds:
    """§7.3.1. UNVERIFIED operational starting values (D14); tune from the §10.6 backtest."""

    flood_ratio: float = 2.0
    horizon_days: int = 7

    def as_dict(self) -> dict[str, float | int]:
        return {"flood_ratio": self.flood_ratio, "horizon_days": self.horizon_days}


# ---------------------------------------------------------------------------- helpers


def _float_or_none(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(out) or math.isinf(out)) else out


def _is_tls_failure(exc: BaseException) -> bool:
    cur: BaseException | None = exc
    while cur is not None:
        if isinstance(cur, ssl.SSLError):
            return True
        text = str(cur).upper()
        if "SSL" in text or "CERTIFICATE" in text or "TLS" in text:
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _get_json(client: httpx.Client, url: str, *, timeout_s: float | None = None) -> dict[str, Any]:
    """One bounded GET → a JSON object, or :class:`FloodError`. Bounded in bytes (streams, stops
    at the cap, refuses compression) and in total time (``timeout_s``, checked per chunk)."""
    if timeout_s is None:  # the budget the client was built with, as adapters/kmd_cap.py does
        timeout_s = getattr(getattr(client, "timeout", None), "read", None) or DEFAULT_TIMEOUT_S
    deadline = _clock() + timeout_s

    def check_deadline() -> None:
        if _clock() > deadline:
            raise FloodError("timeout", f"flood API did not finish within the {timeout_s:g} s total deadline; abandoned")

    try:
        with client.stream("GET", url, headers={"Accept": "application/json", "Accept-Encoding": "identity"}) as response:
            status = response.status_code
            encoding = (response.headers.get("Content-Encoding") or "identity").strip().lower()
            if encoding not in {"", "identity"}:
                raise FloodError(
                    "malformed",
                    f"flood API answered with Content-Encoding {encoding!r} although identity was requested; refused unread",
                )
            buf = bytearray()
            declared = response.headers.get("Content-Length")
            if status < 400 and declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
                raise FloodError("oversize", f"flood API declares {declared} bytes, over the {MAX_BODY_BYTES}-byte cap; not read")
            for chunk in response.iter_bytes():
                buf.extend(chunk)
                if len(buf) > MAX_BODY_BYTES:
                    raise FloodError("oversize", f"flood API body passed the {MAX_BODY_BYTES}-byte cap while streaming; abandoned")
                check_deadline()
            check_deadline()
    except FloodError:
        raise
    except httpx.TimeoutException as exc:
        raise FloodError("timeout", f"flood API did not answer within the timeout ({exc.__class__.__name__})") from exc
    except httpx.TransportError as exc:
        if _is_tls_failure(exc):
            raise FloodError(
                "tls",
                f"flood API TLS verification failed ({exc.__class__.__name__}: {exc}); on this machine set "
                "NOC_USE_TRUSTSTORE=1 with truststore installed (docs/RUNBOOK.md §3)",
            ) from exc
        raise FloodError("network", f"flood API unreachable ({exc.__class__.__name__}: {exc})") from exc

    body_bytes = bytes(buf)
    if status >= 400:
        reason = ""
        try:
            body = json.loads(body_bytes)
            if isinstance(body, dict):
                reason = str(body.get("reason") or body.get("error") or "")
        except ValueError:
            reason = body_bytes[:200].decode("utf-8", errors="replace")
        raise FloodError("http", f"flood API returned HTTP {status}{(': ' + reason) if reason else ''}", status=status)
    try:
        body = json.loads(body_bytes)
    except ValueError as exc:
        raise FloodError("malformed", f"flood API body is not JSON ({exc})") from exc
    if not isinstance(body, dict):
        raise FloodError("malformed", f"flood API body is JSON but not an object ({type(body).__name__})")
    return body


# ---------------------------------------------------------------------------- parse / derive


def parse_flood(payload: Mapping[str, Any]) -> tuple[DailyDischarge, ...]:
    """Flood API body → daily discharge rows. A half-shaped body is ``malformed``, whole.

    The poller never stores a half-parsed forecast: a ``daily.river_discharge`` list that does
    not line up with ``daily.time`` is not a forecast with gaps, it is a response nobody
    understood, and flagging a flood from it would be a guess.
    """
    daily = payload.get("daily")
    if not isinstance(daily, Mapping):
        raise FloodError("malformed", "flood API body has no 'daily' object")
    times = daily.get("time")
    if not isinstance(times, list) or not times:
        raise FloodError("malformed", "flood API 'daily.time' is missing or empty")
    series: dict[str, list[Any]] = {}
    for name in FLOOD_DAILY:
        values = daily.get(name)
        if values is None:
            raise FloodError("malformed", f"flood API 'daily.{name}' is missing (requested, not returned)")
        if not isinstance(values, list) or len(values) != len(times):
            raise FloodError("malformed", f"flood API 'daily.{name}' does not line up with 'daily.time'")
        series[name] = values
    out: list[DailyDischarge] = []
    for i, stamp in enumerate(times):
        try:
            day = date.fromisoformat(str(stamp)[:10])
        except ValueError as exc:
            raise FloodError("malformed", f"flood API 'daily.time[{i}]'={stamp!r} is not a date") from exc
        out.append(
            DailyDischarge(
                day=day,
                discharge_m3s=_float_or_none(series["river_discharge"][i]),
                mean_m3s=_float_or_none(series["river_discharge_mean"][i]),
            )
        )
    return tuple(out)


def derive_flood_risk(
    snapshot: FloodSnapshot,
    *,
    thresholds: FloodThresholds | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """The flood half of §7.3.1's ``weather_risk`` for one riverine site.

    Window: ``horizon_days`` starting today (UTC date of the fetch). Days with a ``null`` value
    are skipped, never read as zero — a zero divisor would manufacture an infinite ratio, and a
    zero peak would hide one. ``flood_flag`` is ``ratio >= flood_ratio``; ``>=`` so the
    threshold itself is the first flagged value (pinned by the unit tests).
    """
    th = thresholds or FloodThresholds()
    start = today or snapshot.fetched_at.date()
    end = start + timedelta(days=th.horizon_days)
    window = [d for d in snapshot.days if start <= d.day < end]
    peaks = [(d.discharge_m3s, d.day) for d in window if d.discharge_m3s is not None]
    means = [d.mean_m3s for d in window if d.mean_m3s is not None]

    peak, peak_day = (max(peaks, key=lambda p: p[0]) if peaks else (None, None))
    mean_ref = round(sum(means) / len(means), 4) if means else None
    ratio: float | None = None
    reason: str | None = None
    if peak is None or mean_ref is None:
        reason = "no usable discharge values in the window"
    elif mean_ref <= 0:
        reason = "mean discharge is zero: a ratio against it would be meaningless, so none is taken"
    else:
        ratio = round(peak / mean_ref, 3)
    flag = ratio is not None and ratio >= th.flood_ratio
    if flag:
        reason = (
            f"peak {peak:g} m3/s on {peak_day.isoformat()} is {ratio:g}x the week's mean "
            f"{mean_ref:g} m3/s (>= {th.flood_ratio:g})"
        )
    return {
        "kind": "river_discharge",
        "river_discharge_max": peak,
        "river_discharge_max_day": peak_day.isoformat() if peak_day else None,
        "river_discharge_mean": mean_ref,
        "ratio": ratio,
        "ratio_basis": "max(daily river_discharge) / mean(daily river_discharge_mean) over the horizon (spec 7.3.1, literal)",
        "flood_flag": flag,
        "reason": reason,
        "days_in_window": len(window),
        "window_from": start.isoformat(),
        "window_until": end.isoformat(),
        "thresholds": th.as_dict(),
        "grid_point": {"latitude": snapshot.latitude, "longitude": snapshot.longitude},
        "source": snapshot.source,
        "fetched_at": snapshot.fetched_at.replace(microsecond=0).isoformat() + "Z",
    }


# ---------------------------------------------------------------------------- provider


class OpenMeteoFloodProvider:
    """GloFAS discharge through Open-Meteo. Free, no key (§7.3.4)."""

    source = GLOFAS

    def __init__(
        self,
        base_url: str | None = None,
        *,
        client: httpx.Client | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        forecast_days: int = 7,
    ) -> None:
        self.base_url = (base_url or DEFAULT_FLOOD_BASE).rstrip("/")
        self.forecast_days = max(1, min(int(forecast_days), 210))
        self._client = client
        self._timeout_s = timeout_s

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = default_client(self._timeout_s)
        return self._client

    def flood_url(self, lat: float, lon: float) -> str:
        params = [
            ("latitude", f"{lat:.4f}"),
            ("longitude", f"{lon:.4f}"),
            ("daily", ",".join(FLOOD_DAILY)),
            ("forecast_days", str(self.forecast_days)),
        ]
        return f"{self.base_url}/v1/flood?{urlencode(params, safe=',')}"

    def discharge(self, lat: float, lon: float, *, now: datetime | None = None) -> FloodSnapshot:
        fetched_at = now or datetime.now(timezone.utc).replace(tzinfo=None)
        url = self.flood_url(lat, lon)
        body = _get_json(self.client, url, timeout_s=self._timeout_s)
        days = parse_flood(body)
        lat_out = _float_or_none(body.get("latitude"))
        lon_out = _float_or_none(body.get("longitude"))
        return FloodSnapshot(
            source=self.source,
            source_url=url,
            latitude=lat if lat_out is None else lat_out,
            longitude=lon if lon_out is None else lon_out,
            fetched_at=fetched_at,
            days=days,
            raw=dict(body),
        )


def provider_from_env(*, client: httpx.Client | None = None, timeout_s: float = DEFAULT_TIMEOUT_S) -> OpenMeteoFloodProvider:
    """``FLOOD_API_BASE`` overrides the host (not in ``.env.example``: nobody should need it;
    it exists so a mirror or the paid Open-Meteo host can be pointed at without a code change)."""
    return OpenMeteoFloodProvider(os.getenv("FLOOD_API_BASE") or None, client=client, timeout_s=timeout_s)


def glofas_discharge(lat: float, lon: float, *, provider: OpenMeteoFloodProvider | None = None) -> FloodSnapshot:
    """§5.3.13's ``glofas_discharge(lat, lon) -> FloodSnapshot``, for ad-hoc and diagnostic use."""
    return (provider or provider_from_env()).discharge(lat, lon)


def snapshot_payload_json(snapshot: FloodSnapshot) -> str:
    """The provider body as compact JSON for ``external_signals.payload_json``."""
    return json.dumps(snapshot.raw, separators=(",", ":"), ensure_ascii=False)
