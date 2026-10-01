"""WeatherProvider: hourly forecasts for a coordinate, from Open-Meteo or MET Norway (spec §7.3, §5.3.13).

This is the *only* module that talks to a weather API. Everything else in the system reads
the ``external_signals`` cache that ``pollers/weather.py`` fills, so an incident on the hot
path never waits on the network and a dead provider degrades to a STALE badge, not a crash.

Two providers, one shape
------------------------
* **Open-Meteo** (default; free, no key; https://open-meteo.com/en/docs) —
  ``GET {base}/v1/forecast?latitude=&longitude=&hourly=precipitation,precipitation_probability,
  wind_speed_10m,wind_gusts_10m,cape,weather_code&forecast_days=2&timezone=Africa%2FNairobi``.
  Six variables × 2 days is exactly 1.0 call against the free tier (10,000/day, dev/demo only:
  the free tier is **non-commercial**; production buys the Standard plan and sets
  ``WEATHER_API_BASE=https://customer-api.open-meteo.com`` plus ``WEATHER_API_KEY`` — §7.3.4).
  Hourly ``time`` values come back in the requested zone *without* an offset, so the parser
  applies ``utc_offset_seconds`` to store naive UTC, which is the database contract.
* **MET Norway** (fallback; CC BY 4.0; https://api.met.no/weatherapi/locationforecast/2.0/documentation)
  — ``GET {base}/weatherapi/locationforecast/2.0/complete?lat=&lon=``. **MET's Terms of Service
  (https://api.met.no/doc/TermsOfService) make an identifying ``User-Agent`` with contact
  information mandatory, prohibit fake or random UA strings, and warn that a missing or
  anonymous UA is throttled (429) or blocked without notice.** ``MET_NO_USER_AGENT`` is therefore
  required: :class:`MetNorwayProvider` refuses to send a request without it rather than
  identify the operator falsely. The same terms ask clients to honour ``Expires`` and send
  ``If-Modified-Since``; the provider keeps the last response per coordinate and does both.
  MET reports wind in m/s (converted to km/h here), has no CAPE and no WMO code — the
  thunderstorm proxy is a ``symbol_code`` containing ``thunder``.

The parsed result is a :class:`ForecastSnapshot` of :class:`HourlyPoint` rows (naive UTC), and
:func:`derive_weather_risk` turns the next ``horizon_hours`` of it into the spec's
``weather_risk`` block. The thresholds (20 mm / 6 h, 60 km/h gusts, 1500 J/kg CAPE, WMO codes
95/96/99) are the spec's **UNVERIFIED operational starting values**, not meteorological
standards; they are tuned from the §10.6 backtest, never hard-coded into a rule elsewhere.

Bounded in time
---------------
Every fetch carries a total deadline (``timeout_s``, 10 s — spec §9), not merely a per-read one:
name resolution is bounded by :func:`resolve_within_deadline` and the exchange by
:class:`DeadlineWatchdog`, which shuts the connection when the deadline passes. Both are shared
with ``adapters/kmd_cap.py`` and ``adapters/flood.py`` and defined here, because those two import
this module (CONFORMANCE A-18: this adapter had the per-read-only timeout the other two had).

Failure handling
----------------
Every failure surfaces as one :class:`WeatherError` with a ``kind`` the poller can record:
``timeout``, ``tls``, ``network``, ``http``, ``malformed``, ``config``. Nothing here retries,
sleeps or raises anything else on purpose — the poller decides what to do (keep the last good
row, label it stale) and the scheduler's per-call budget is 10 s (§9).

**TLS on this machine:** Avast's Web/Mail Shield re-signs HTTPS and certifi does not carry its
root, so a bare ``httpx.get`` fails with ``CERTIFICATE_VERIFY_FAILED`` (docs/RUNBOOK.md §3).
When ``NOC_USE_TRUSTSTORE`` is set the default client injects ``truststore`` so Python verifies
against the Windows store. That is an operator remediation, not a declared dependency (§7.0.11);
a TLS failure is reported as ``kind="tls"`` with the runbook pointer in the message.

No test may reach the network: every provider takes an injectable ``httpx.Client``, and the
suite passes one built on ``httpx.MockTransport`` with recorded fixtures (see
``tests/unit/test_weather_provider.py``).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import math
import os
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, parsedate_to_datetime
from typing import Any, Mapping, Protocol
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx

log = logging.getLogger("noc_agents.adapters.weather")

__all__ = [
    "DEFAULT_MET_NORWAY_BASE",
    "DeadlineWatchdog",
    "DnsTimeout",
    "DEFAULT_OPEN_METEO_BASE",
    "DEFAULT_TIMEOUT_S",
    "ForecastSnapshot",
    "HourlyPoint",
    "MET_NORWAY",
    "MetNorwayProvider",
    "OPEN_METEO",
    "OPEN_METEO_HOURLY",
    "OpenMeteoProvider",
    "THUNDER_CODES",
    "WeatherError",
    "WeatherProvider",
    "WeatherThresholds",
    "default_client",
    "derive_weather_risk",
    "parse_met_norway",
    "parse_open_meteo",
    "provider_from_env",
    "resolve_provider_name",
]

# ---------------------------------------------------------------------------- constants

OPEN_METEO = "OPEN_METEO"
MET_NORWAY = "MET_NORWAY"

DEFAULT_OPEN_METEO_BASE = "https://api.open-meteo.com"
DEFAULT_MET_NORWAY_BASE = "https://api.met.no"
DEFAULT_TIMEOUT_S = 10.0  # spec §9: weather/CAP/flood pollers ≤ 10 s per call
FORECAST_TIMEZONE = "Africa/Nairobi"

#: The six hourly variables the NOC needs (§7.3.3). Six × 2 days = 1.0 Open-Meteo call.
OPEN_METEO_HOURLY: tuple[str, ...] = (
    "precipitation",
    "precipitation_probability",
    "wind_speed_10m",
    "wind_gusts_10m",
    "cape",
    "weather_code",
)

#: WMO weather codes that mean thunderstorm (95 = thunderstorm, 96/99 = with hail). Open-Meteo
#: has no lightning variable; these codes and CAPE are the proxies (§7.3).
THUNDER_CODES: frozenset[int] = frozenset({95, 96, 99})

#: ``WEATHER_PROVIDER`` spellings accepted. The spec (§5.3.13) says ``met_norway``; ``.env.example``
#: says ``met_no``. Both resolve to the same provider so neither document is "wrong" at runtime.
PROVIDER_ALIASES: dict[str, str] = {
    "open_meteo": OPEN_METEO,
    "open-meteo": OPEN_METEO,
    "openmeteo": OPEN_METEO,
    "met_norway": MET_NORWAY,
    "met_no": MET_NORWAY,
    "metno": MET_NORWAY,
    "met.no": MET_NORWAY,
}

_TRUE = {"1", "true", "yes", "on"}
_MS_TO_KMH = 3.6

# ---------------------------------------------------------------------------- value types


class WeatherError(Exception):
    """One provider failure, classified so the poller can record it without guessing.

    ``kind`` ∈ {``timeout``, ``tls``, ``network``, ``http``, ``malformed``, ``config``};
    ``status`` is the HTTP status for ``http``. ``str(err)`` is safe to store in
    ``external_signals.last_error`` (no secrets: the URL never carries the API key).
    """

    def __init__(self, kind: str, message: str, *, status: int | None = None, source: str | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.source = source

    def __str__(self) -> str:  # "timeout: ..." reads well in a last_error column and a log line
        prefix = self.kind if self.status is None else f"{self.kind} {self.status}"
        return f"{prefix}: {self.args[0]}"


@dataclass(frozen=True)
class HourlyPoint:
    """One forecast hour, provider-neutral. ``time`` is the start of the hour, **naive UTC**.

    ``None`` means "the provider did not give this value" (MET has no CAPE or WMO code;
    Open-Meteo returns ``null`` for a variable the model lacks) — never zero.
    """

    time: datetime
    precip_mm: float | None = None
    precip_prob_pct: float | None = None
    wind_kmh: float | None = None
    gust_kmh: float | None = None
    cape_jkg: float | None = None
    weather_code: int | None = None
    thunder: bool = False


@dataclass(frozen=True)
class ForecastSnapshot:
    """A parsed forecast for one coordinate: what the poller stores and derives from."""

    source: str  # OPEN_METEO | MET_NORWAY
    source_url: str  # the request URL, key redacted
    latitude: float  # as returned by the provider (grid-snapped, not the requested point)
    longitude: float
    fetched_at: datetime  # naive UTC
    hours: tuple[HourlyPoint, ...]
    raw: dict[str, Any] = field(default_factory=dict)  # the response body as received
    provider_updated_at: datetime | None = None  # MET ``meta.updated_at``; None for Open-Meteo
    expires_at: datetime | None = None  # MET ``Expires`` header; None for Open-Meteo
    from_cache: bool = False  # True when served from the If-Modified-Since / Expires cache

    @property
    def first_hour(self) -> datetime | None:
        return self.hours[0].time if self.hours else None

    @property
    def last_hour(self) -> datetime | None:
        return self.hours[-1].time if self.hours else None


@dataclass(frozen=True)
class WeatherThresholds:
    """§7.3.1 storm rules. UNVERIFIED operational starting values (D14): tune from the backtest."""

    storm_rain_mm: float = 20.0  # rain over the horizon window
    storm_gust_kmh: float = 60.0
    storm_cape_jkg: float = 1500.0
    horizon_hours: int = 6  # "rain_mm_next_6h"

    def as_dict(self) -> dict[str, float | int]:
        return {
            "storm_rain_mm": self.storm_rain_mm,
            "storm_gust_kmh": self.storm_gust_kmh,
            "storm_cape_jkg": self.storm_cape_jkg,
            "horizon_hours": self.horizon_hours,
        }


class WeatherProvider(Protocol):
    """What the poller needs from a provider (§5.3.13 ``WeatherProvider.forecast``)."""

    source: str

    def forecast(self, lat: float, lon: float, hours: int = 48, *, now: datetime | None = None) -> ForecastSnapshot: ...


# ---------------------------------------------------------------------------- helpers


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _to_naive_utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo is not None else dt


def _float_or_none(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) else out


def _int_or_none(value: Any) -> int | None:
    out = _float_or_none(value)
    return None if out is None else int(out)


def _env_true(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in _TRUE


def _http_date(dt: datetime) -> str:
    return format_datetime(dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt, usegmt=True)


def _parse_http_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return _to_naive_utc(parsedate_to_datetime(value))
    except (TypeError, ValueError, IndexError):
        return None


def _parse_iso_utc(value: Any) -> datetime | None:
    """``2026-09-17T06:00:00Z`` → naive UTC. ``None`` when it is not a timestamp."""
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return _to_naive_utc(parsed) if parsed.tzinfo is not None else parsed


_truststore_done = False


def _maybe_inject_truststore() -> None:
    """``NOC_USE_TRUSTSTORE`` → verify against the OS certificate store (docs/RUNBOOK.md §3).

    Runs once per process, only when the flag is set, and never at import time. A missing
    package is a warning, not a crash: the next request then fails as ``kind="tls"`` with
    the runbook pointer, which is the honest outcome.
    """
    global _truststore_done
    if _truststore_done or not _env_true("NOC_USE_TRUSTSTORE"):
        return
    _truststore_done = True
    try:
        import truststore  # type: ignore[import-not-found]  # operator remediation, not a declared dependency

        truststore.inject_into_ssl()
        log.info("weather: NOC_USE_TRUSTSTORE set; truststore injected into ssl")
    except ImportError:
        log.warning("weather: NOC_USE_TRUSTSTORE is set but truststore is not installed (docs/RUNBOOK.md §3)")


def default_client(timeout_s: float = DEFAULT_TIMEOUT_S, *, user_agent: str | None = None) -> httpx.Client:
    """A real HTTP client for production use. Tests never call this: they inject a MockTransport."""
    _maybe_inject_truststore()
    headers = {"Accept": "application/json"}
    if user_agent:
        headers["User-Agent"] = user_agent
    return httpx.Client(timeout=httpx.Timeout(timeout_s), headers=headers, follow_redirects=False)


#: The clock the total fetch deadline is measured on. Indirected so a test can advance it.
_clock = time.monotonic


class DnsTimeout(Exception):
    """Name resolution did not finish inside the caller's total deadline."""


def resolve_within_deadline(url: str, timeout_s: float) -> None:
    """Make sure ``url``'s host can be resolved inside ``timeout_s``, or raise :class:`DnsTimeout`.

    Why this exists (review finding DNS, pre-existing): resolution happens *before* any socket
    exists, so :class:`DeadlineWatchdog` has nothing to shut down, and httpx's connect timeout
    does not cover ``getaddrinfo`` either. A stalled resolver ran 3.35 s against a 0.5 s budget,
    and both production feeds use hostnames.

    So the lookup runs in a worker thread and is waited for only as long as the budget allows.
    Past that the caller is told and the exchange ends without ever connecting. ``getaddrinfo``
    cannot be cancelled, so the worker is left to finish on its own: it is a daemon, it holds
    nothing, and the next poll starts a new one.

    **Why this does not hand httpx an address to connect to.** Pinning the address we resolved
    would make the bound airtight — no second lookup could stall — and SNI and certificate
    verification could be preserved through ``sni_hostname`` and the ``Host`` header. It would
    also throw away something worth more: ``socket.create_connection`` tries *every* address a
    host resolves to, in order, and gives up only when all fail. A host whose first address is an
    unreachable AAAA (measured here: ``localhost`` resolves to ``::1`` first) would start failing
    where httpx would quietly have fallen back to IPv4. So the lookup is bounded and the
    connection is left to httpx, whose own lookup is then answered from the resolver cache.

    What that costs, stated plainly: if the cache does not keep the answer (TTL 0, no caching
    resolver) a second lookup could stall, so the worst case is the budget for the lookup plus
    the budget for the exchange, rather than one budget. That is the same order as the Windows
    ``shutdown`` caveat in :class:`DeadlineWatchdog`, and it replaces an unbounded stall.

    A host that is already an IP address is left alone: there is nothing to resolve.
    """
    host = urlsplit(url).hostname
    if not host:
        return
    try:
        ipaddress.ip_address(host)
        return  # already an address
    except ValueError:
        pass

    done = threading.Event()

    def lookup() -> None:
        try:
            socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except BaseException:  # noqa: BLE001 — including a test's blocked-socket guard
            pass  # any failure is httpx's to meet and classify, exactly as before
        finally:
            done.set()

    threading.Thread(target=lookup, name="noc-dns-lookup", daemon=True).start()
    if not done.wait(timeout=max(0.0, timeout_s)):
        raise DnsTimeout(f"name resolution for {host!r} did not finish within the {timeout_s:g} s total deadline")


class DeadlineWatchdog:
    """A total deadline on one HTTP exchange, enforced below httpx (review findings W02, W02-TLS).

    Shared by all three early-warning adapters: this one, ``adapters/kmd_cap.py`` and
    ``adapters/flood.py`` (CONFORMANCE A-18). It lives here because the other two already import
    this module, and the reverse would be a cycle.

    Why this and not a timeout setting: httpx (and httpcore beneath it) apply ``timeout`` to each
    individual socket operation. No per-request value bounds a server that answers every read
    promptly with one byte — in the body, in the response headers, or as an endless series of
    ``100 Continue`` interim responses, all confirmed over a real socket to run 8-11 s against a
    1 s budget. The only thing that ends such an exchange at a fixed time is shutting the
    connection down at that time.

    **The watchdog owns its own handle on the connection.** :meth:`trace` is passed as httpx's
    documented ``trace`` request extension. On ``connection.connect_tcp.complete`` (and again on
    ``connection.start_tls.complete``) it takes a *duplicate descriptor* of the connection's
    socket (``socket.fromfd``), which this object owns. At the deadline a ``threading.Timer``
    calls ``shutdown(SHUT_RDWR)`` on those duplicates. ``shutdown`` acts on the connection, not on
    one descriptor, so every reader of that connection — plain or TLS, in the handshake, the
    headers or the body — is cut off.

    Why a duplicate and not the socket httpcore hands over (review finding W02-TLS, the reason
    this class was rewritten): for ``https`` httpcore calls ``ssl_context.wrap_socket(sock)``, and
    ``SSLSocket`` *detaches* the plain socket — the object recorded at ``connect_tcp`` then has
    ``fileno() == -1``, ``shutdown`` failed with WinError 10038 / EBADF, the error was swallowed, and
    both production feeds (both ``https``) had no header-phase bound at all. A descriptor the
    watchdog duplicated at ``connect_tcp`` stays valid through the wrap and everything after it.
    (The handshake itself was never the gap: CPython's ``ssl`` applies the socket timeout to
    ``do_handshake`` as one *total* deadline — measured, a handshake dripped over ~7 s aborts at a
    1.0 s timeout — and :func:`_bounded_get` caps that timeout at the budget. The gap is after the
    handshake: every TLS read gets a fresh per-read timeout, so a drip of response headers or of
    ``1xx`` responses is bounded by nothing but this watchdog.)

    **Never another request's socket.** The duplicates belong to this object and are closed by
    :meth:`cancel`, under the same lock :meth:`_fire` holds while it shuts them down — so a timer
    that fires late finds an empty list (and a closed duplicate reports ``fileno() == -1``; it can
    never come to name a descriptor some later request was given). A trace callback is per
    request, and requests send ``Connection: close`` so each opens, and so exposes, its own
    connection; a connection this exchange did not open is never adopted.

    **Bound.** On Linux a blocked read fails as soon as ``shutdown`` runs. On Windows, measured
    here, it fails when the next byte arrives: a dripping server is ended within one inter-byte
    gap of the deadline, and a silent one by the per-request timeout, which
    :func:`_bounded_get` caps at the budget — so at most ``2 x timeout_s`` there. httpx then
    raises a ``TransportError`` and the caller, seeing :attr:`fired`, reports ``timeout``.

    **No thread outlives the exchange.** :meth:`cancel` (always called in the caller's
    ``finally``) cancels the timer and joins its thread; the timer thread is a daemon named
    ``noc-deadline-watchdog`` so a leak would be visible in ``threading.enumerate()``.
    """

    THREAD_NAME = "noc-deadline-watchdog"
    _EVENTS = frozenset({"connection.connect_tcp.complete", "connection.start_tls.complete"})

    def __init__(self, seconds: float) -> None:
        self.fired = False
        self._closed = False
        self._lock = threading.Lock()
        self._handles: list[socket.socket] = []
        self._timer = threading.Timer(max(0.0, seconds), self._fire)
        self._timer.name = self.THREAD_NAME
        self._timer.daemon = True
        self._timer.start()

    def trace(self, event_name: str, info: Mapping[str, Any]) -> None:
        if event_name not in self._EVENTS:
            return
        stream = info.get("return_value")
        sock = stream.get_extra_info("socket") if stream is not None else None
        handle = self._adopt(sock)
        if handle is None:
            return
        with self._lock:
            if self._closed:  # the exchange already ended: never keep a handle past cancel()
                handle.close()
                return
            self._handles.append(handle)
            if self.fired:  # connected (or finished TLS) after the deadline had passed
                self._shutdown(handle)

    @staticmethod
    def _adopt(sock: Any) -> socket.socket | None:
        """A duplicate descriptor of ``sock``'s connection, owned by the watchdog, or ``None``."""
        try:
            fd = sock.fileno() if sock is not None else -1
            if fd < 0:
                return None
            return socket.fromfd(fd, sock.family, sock.type)
        except (OSError, ValueError, AttributeError):
            return None  # cannot duplicate: the per-chunk check and per-wait cap still bound it

    def _fire(self) -> None:
        with self._lock:
            if self._closed:
                return
            self.fired = True
            for handle in self._handles:
                self._shutdown(handle)

    @staticmethod
    def _shutdown(handle: socket.socket) -> None:
        try:
            handle.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass  # the connection is already gone: nothing left to interrupt

    def cancel(self) -> None:
        """End the watch: stop the timer, close every handle, and join the timer thread."""
        self._timer.cancel()
        with self._lock:
            self._closed = True
            handles, self._handles = self._handles, []
        for handle in handles:
            try:
                handle.close()
            except OSError:
                pass
        if threading.current_thread() is not self._timer:
            self._timer.join(timeout=1.0)


def _per_wait(client: httpx.Client, timeout_s: float) -> httpx.Timeout:
    """A per-request httpx timeout no longer than the total budget, nor than the client's own."""
    own = getattr(getattr(client, "timeout", None), "read", None)
    return httpx.Timeout(min(timeout_s, own) if own else timeout_s)


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


def _get_json(client: httpx.Client, url: str, *, headers: Mapping[str, str] | None, source: str) -> tuple[int, Any, httpx.Headers]:
    """One GET, classified. Returns ``(status, body_or_None, headers)``; a 304 has no body.

    Every transport problem becomes a :class:`WeatherError`; a non-2xx/304 status is ``http``
    with the provider's own reason when the body carries one (Open-Meteo: ``{"error": true,
    "reason": ...}``); an unparseable body is ``malformed``.
    """
    timeout_s = getattr(getattr(client, "timeout", None), "read", None) or DEFAULT_TIMEOUT_S
    watchdog = DeadlineWatchdog(timeout_s)
    per_wait = _per_wait(client, timeout_s)
    try:
        # The same two bounds the CAP and flood adapters use (CONFORMANCE A-18): resolution inside
        # the budget, then a watchdog that shuts the connection at the deadline. httpx's timeout is
        # per socket read, so without them a server dripping one byte per read — or a stalled
        # resolver — holds the forecast poll open indefinitely.
        try:
            resolve_within_deadline(url, timeout_s)
        except DnsTimeout as exc:
            raise WeatherError("timeout", f"{source}: {exc}", source=source) from exc
        response = client.get(
            url,
            headers={"Connection": "close", **dict(headers or {})},
            timeout=per_wait,
            extensions={"trace": watchdog.trace},
        )
    except WeatherError:
        raise
    except (httpx.TransportError, OSError) as exc:
        if watchdog.fired:  # the watchdog shut the connection: the deadline, not a network fault
            raise WeatherError(
                "timeout",
                f"{source} did not finish within the {timeout_s:g} s total deadline; connection closed",
                source=source,
            ) from exc
        # A plain read timeout keeps its own wording. It is already bounded by the budget
        # (``per_wait``), and this adapter's failure messages are pinned by existing tests.
        if isinstance(exc, httpx.TimeoutException):
            raise WeatherError("timeout", f"{source} did not answer within the timeout ({exc.__class__.__name__})", source=source) from exc
        if _is_tls_failure(exc):
            raise WeatherError(
                "tls",
                f"{source} TLS verification failed ({exc.__class__.__name__}: {exc}); "
                "on this machine set NOC_USE_TRUSTSTORE=1 with truststore installed (docs/RUNBOOK.md §3)",
                source=source,
            ) from exc
        raise WeatherError("network", f"{source} unreachable ({exc.__class__.__name__}: {exc})", source=source) from exc
    finally:
        watchdog.cancel()

    status = response.status_code
    if status == 304:
        return status, None, response.headers
    if status >= 400:
        reason = ""
        try:
            body = response.json()
            if isinstance(body, dict):
                reason = str(body.get("reason") or body.get("error") or body.get("message") or "")
        except ValueError:
            reason = response.text[:200]
        raise WeatherError("http", f"{source} returned HTTP {status}{(': ' + reason) if reason else ''}", status=status, source=source)
    try:
        body = response.json()
    except ValueError as exc:
        raise WeatherError("malformed", f"{source} body is not JSON ({exc})", source=source) from exc
    if not isinstance(body, dict):
        raise WeatherError("malformed", f"{source} body is JSON but not an object ({type(body).__name__})", source=source)
    return status, body, response.headers


def resolve_provider_name(raw: str | None) -> str:
    """``WEATHER_PROVIDER`` → ``OPEN_METEO`` | ``MET_NORWAY``. Unknown values are a config error."""
    key = (raw or "open_meteo").strip().lower()
    try:
        return PROVIDER_ALIASES[key]
    except KeyError:
        raise WeatherError("config", f"WEATHER_PROVIDER={raw!r} is not one of {sorted(PROVIDER_ALIASES)}") from None


# ---------------------------------------------------------------------------- parsers


def parse_open_meteo(payload: Mapping[str, Any]) -> tuple[HourlyPoint, ...]:
    """Open-Meteo ``/v1/forecast`` body → hourly points in naive UTC.

    Documented shape (https://open-meteo.com/en/docs): top-level ``latitude``, ``longitude``,
    ``utc_offset_seconds``, ``timezone``, ``hourly_units`` and ``hourly`` = ``{"time": [...],
    "<variable>": [...]}`` with every list the same length; missing values are ``null``.
    A body without that shape raises ``WeatherError("malformed")`` — the poller never stores
    a half-parsed forecast.
    """
    hourly = payload.get("hourly")
    if not isinstance(hourly, Mapping):
        raise WeatherError("malformed", "Open-Meteo body has no 'hourly' object", source=OPEN_METEO)
    times = hourly.get("time")
    if not isinstance(times, list) or not times:
        raise WeatherError("malformed", "Open-Meteo 'hourly.time' is missing or empty", source=OPEN_METEO)
    offset = timedelta(seconds=int(_float_or_none(payload.get("utc_offset_seconds")) or 0))

    series: dict[str, list[Any]] = {}
    for name in OPEN_METEO_HOURLY:
        values = hourly.get(name)
        if values is None:
            series[name] = [None] * len(times)  # a variable the model lacks: absent, not wrong
            continue
        if not isinstance(values, list) or len(values) != len(times):
            raise WeatherError("malformed", f"Open-Meteo 'hourly.{name}' does not line up with 'hourly.time'", source=OPEN_METEO)
        series[name] = values

    points: list[HourlyPoint] = []
    for i, stamp in enumerate(times):
        if not isinstance(stamp, str):
            raise WeatherError("malformed", f"Open-Meteo 'hourly.time[{i}]' is not a string", source=OPEN_METEO)
        try:
            local = datetime.fromisoformat(stamp)
        except ValueError as exc:
            raise WeatherError("malformed", f"Open-Meteo 'hourly.time[{i}]'={stamp!r} is not ISO 8601", source=OPEN_METEO) from exc
        when = _to_naive_utc(local) if local.tzinfo is not None else local - offset
        code = _int_or_none(series["weather_code"][i])
        points.append(
            HourlyPoint(
                time=when,
                precip_mm=_float_or_none(series["precipitation"][i]),
                precip_prob_pct=_float_or_none(series["precipitation_probability"][i]),
                wind_kmh=_float_or_none(series["wind_speed_10m"][i]),
                gust_kmh=_float_or_none(series["wind_gusts_10m"][i]),
                cape_jkg=_float_or_none(series["cape"][i]),
                weather_code=code,
                thunder=code in THUNDER_CODES if code is not None else False,
            )
        )
    return tuple(points)


def parse_met_norway(payload: Mapping[str, Any]) -> tuple[HourlyPoint, ...]:
    """MET Norway Locationforecast 2.0 body → hourly points in naive UTC.

    Documented shape (https://api.met.no/weatherapi/locationforecast/2.0/documentation, and the
    ``METJSONForecast`` schema at ``/weatherapi/locationforecast/2.0/swagger``): a GeoJSON
    ``Feature`` whose ``properties.timeseries[]`` entries carry ``time`` (UTC, ``Z``),
    ``data.instant.details`` (``wind_speed`` and ``wind_speed_of_gust`` in **m/s**) and, for the
    hourly part of the series, ``data.next_1_hours`` with ``summary.symbol_code`` and
    ``details.precipitation_amount`` (mm) plus, on the ``complete`` endpoint,
    ``probability_of_precipitation``. Beyond roughly 48 h MET switches to 6-hourly entries that
    carry only ``next_6_hours``; those keep their instant wind but get no hourly rain, so they
    can never inflate a 6-hour rain sum. No CAPE, no WMO code: ``thunder`` is the proxy.
    """
    props = payload.get("properties")
    if not isinstance(props, Mapping):
        raise WeatherError("malformed", "MET Norway body has no 'properties' object", source=MET_NORWAY)
    series = props.get("timeseries")
    if not isinstance(series, list) or not series:
        raise WeatherError("malformed", "MET Norway 'properties.timeseries' is missing or empty", source=MET_NORWAY)
    meta = props.get("meta") if isinstance(props.get("meta"), Mapping) else {}
    units = meta.get("units") if isinstance(meta.get("units"), Mapping) else {}
    wind_factor = 1.0 if str(units.get("wind_speed", "m/s")).lower() in {"km/h", "kmh"} else _MS_TO_KMH
    gust_factor = 1.0 if str(units.get("wind_speed_of_gust", "m/s")).lower() in {"km/h", "kmh"} else _MS_TO_KMH

    points: list[HourlyPoint] = []
    for i, entry in enumerate(series):
        if not isinstance(entry, Mapping):
            raise WeatherError("malformed", f"MET Norway 'timeseries[{i}]' is not an object", source=MET_NORWAY)
        when = _parse_iso_utc(entry.get("time"))
        if when is None:
            raise WeatherError("malformed", f"MET Norway 'timeseries[{i}].time' is not a timestamp", source=MET_NORWAY)
        data = entry.get("data") if isinstance(entry.get("data"), Mapping) else {}
        instant = data.get("instant") if isinstance(data.get("instant"), Mapping) else {}
        details = instant.get("details") if isinstance(instant.get("details"), Mapping) else {}
        wind = _float_or_none(details.get("wind_speed"))
        gust = _float_or_none(details.get("wind_speed_of_gust"))

        next_1h = data.get("next_1_hours") if isinstance(data.get("next_1_hours"), Mapping) else None
        next_6h = data.get("next_6_hours") if isinstance(data.get("next_6_hours"), Mapping) else None
        precip = prob = None
        symbol = ""
        if next_1h is not None:
            d1 = next_1h.get("details") if isinstance(next_1h.get("details"), Mapping) else {}
            precip = _float_or_none(d1.get("precipitation_amount"))
            prob = _float_or_none(d1.get("probability_of_precipitation"))
            s1 = next_1h.get("summary") if isinstance(next_1h.get("summary"), Mapping) else {}
            symbol = str(s1.get("symbol_code") or "")
        elif next_6h is not None:
            s6 = next_6h.get("summary") if isinstance(next_6h.get("summary"), Mapping) else {}
            symbol = str(s6.get("symbol_code") or "")
        points.append(
            HourlyPoint(
                time=when,
                precip_mm=precip,
                precip_prob_pct=prob,
                wind_kmh=None if wind is None else round(wind * wind_factor, 2),
                gust_kmh=None if gust is None else round(gust * gust_factor, 2),
                cape_jkg=None,
                weather_code=None,
                thunder="thunder" in symbol.lower(),
            )
        )
    return tuple(points)


# ---------------------------------------------------------------------------- derivation


def _max_or_none(values: list[float]) -> float | None:
    return max(values) if values else None


def derive_weather_risk(
    snapshot: ForecastSnapshot,
    *,
    now: datetime | None = None,
    thresholds: WeatherThresholds | None = None,
) -> dict[str, Any]:
    """The §7.3.1 ``weather_risk`` block for the ``horizon_hours`` after ``now``.

    An hour is inside the window when it overlaps ``[now, now + horizon)``, so the hour that
    is *currently running* counts. ``storm_flag`` is the spec rule, evaluated on the window
    only::

        rain_mm_next_6h ≥ storm_rain_mm  or  gust_kmh_max ≥ storm_gust_kmh
        or  cape_max_jkg ≥ storm_cape_jkg  or  any thunderstorm hour (WMO 95/96/99 / MET "thunder")

    ``flood_flag`` is always False here — it comes from the GloFAS poller (§7.3.3), which is a
    separate job; ``cap_alert_ids`` likewise stays empty until the KMD CAP poller joins in.
    ``hours_in_window`` says how much of the window the forecast actually covered; the poller
    treats 0 as a failure, because a forecast that does not cover now is not a forecast.
    Comparisons are ``>=`` so the spec's thresholds are the first flagged values, and a
    threshold edge is pinned by the unit tests.
    """
    now = now or snapshot.fetched_at
    th = thresholds or WeatherThresholds()
    end = now + timedelta(hours=th.horizon_hours)
    window = [p for p in snapshot.hours if p.time < end and p.time + timedelta(hours=1) > now]

    rain_values = [p.precip_mm for p in window if p.precip_mm is not None]
    rain = round(sum(rain_values), 2) if rain_values else None
    prob_max = _max_or_none([p.precip_prob_pct for p in window if p.precip_prob_pct is not None])
    gust_max = _max_or_none([p.gust_kmh for p in window if p.gust_kmh is not None])
    cape_max = _max_or_none([p.cape_jkg for p in window if p.cape_jkg is not None])
    codes = [p.weather_code for p in window if p.weather_code is not None]
    code_max = max(codes) if codes else None
    thunder = any(p.thunder for p in window)

    reasons: list[str] = []
    if rain is not None and rain >= th.storm_rain_mm:
        reasons.append(f"rain {rain:g} mm/{th.horizon_hours}h >= {th.storm_rain_mm:g}")
    if gust_max is not None and gust_max >= th.storm_gust_kmh:
        reasons.append(f"gusts {gust_max:g} km/h >= {th.storm_gust_kmh:g}")
    if cape_max is not None and cape_max >= th.storm_cape_jkg:
        reasons.append(f"CAPE {cape_max:g} J/kg >= {th.storm_cape_jkg:g}")
    if thunder:
        reasons.append("thunderstorm forecast" + (f" (WMO {code_max})" if code_max in THUNDER_CODES else ""))

    return {
        "rain_mm_next_6h": rain,
        "precip_prob_max_pct": prob_max,
        "gust_kmh_max": gust_max,
        "cape_max_jkg": cape_max,
        "weather_code_max": code_max,
        "thunder": thunder,
        "storm_flag": bool(reasons),
        "storm_reasons": reasons,
        "flood_flag": False,
        "cap_alert_ids": [],
        "source": snapshot.source,
        "fetched_at": snapshot.fetched_at.replace(microsecond=0).isoformat() + "Z",
        "stale": False,
        "window_from": now.replace(microsecond=0).isoformat() + "Z",
        "window_until": end.replace(microsecond=0).isoformat() + "Z",
        "horizon_hours": th.horizon_hours,
        "hours_in_window": len(window),
        "thresholds": th.as_dict(),
        "grid_point": {"latitude": snapshot.latitude, "longitude": snapshot.longitude},
    }


# ---------------------------------------------------------------------------- providers


class OpenMeteoProvider:
    """Open-Meteo forecast client. Free tier needs no key; the paid host takes ``apikey=``."""

    source = OPEN_METEO

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        *,
        client: httpx.Client | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        timezone_name: str = FORECAST_TIMEZONE,
    ) -> None:
        self.base_url = (base_url or DEFAULT_OPEN_METEO_BASE).rstrip("/")
        self.api_key = (api_key or "").strip() or None
        self.timezone_name = timezone_name
        self._client = client
        self._timeout_s = timeout_s

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = default_client(self._timeout_s)
        return self._client

    def forecast_url(self, lat: float, lon: float, hours: int = 48, *, redact: bool = True) -> str:
        days = max(1, min(16, math.ceil(max(1, hours) / 24)))
        params: list[tuple[str, str]] = [
            ("latitude", f"{lat:.4f}"),
            ("longitude", f"{lon:.4f}"),
            ("hourly", ",".join(OPEN_METEO_HOURLY)),
            ("forecast_days", str(days)),
            ("timezone", self.timezone_name),
        ]
        if self.api_key:
            params.append(("apikey", "***" if redact else self.api_key))
        return f"{self.base_url}/v1/forecast?{urlencode(params, safe=',*')}"

    def forecast(self, lat: float, lon: float, hours: int = 48, *, now: datetime | None = None) -> ForecastSnapshot:
        fetched_at = now or _utcnow()
        url = self.forecast_url(lat, lon, hours, redact=False)
        _, body, _ = _get_json(self.client, url, headers=None, source=self.source)
        assert body is not None  # a 304 cannot happen: no conditional headers were sent
        points = parse_open_meteo(body)
        return ForecastSnapshot(
            source=self.source,
            source_url=self.forecast_url(lat, lon, hours, redact=True),
            latitude=_float_or_none(body.get("latitude")) if _float_or_none(body.get("latitude")) is not None else lat,
            longitude=_float_or_none(body.get("longitude")) if _float_or_none(body.get("longitude")) is not None else lon,
            fetched_at=fetched_at,
            hours=points,
            raw=dict(body),
        )


@dataclass
class _MetCacheEntry:
    payload: dict[str, Any]
    points: tuple[HourlyPoint, ...]
    last_modified: str | None  # verbatim header, echoed back as If-Modified-Since
    expires_at: datetime | None  # naive UTC
    fetched_at: datetime


class MetNorwayProvider:
    """MET Norway Locationforecast 2.0 client — the fallback, and the one with terms to honour.

    MET's Terms of Service (https://api.met.no/doc/TermsOfService) require every request to
    carry a ``User-Agent`` that identifies the application *and gives contact information*
    (an e-mail address or a URL), forbid fake, generic or random UA strings, cap traffic at
    20 requests/second per application, and say that violators are throttled (429) or blocked
    without warning. ``MET_NO_USER_AGENT`` (``.env.example``) is that string; without one this
    provider raises ``WeatherError("config")`` *before* opening a connection, because sending
    an anonymous or invented UA would breach the terms in the operator's name. The same terms
    ask clients not to re-fetch before ``Expires`` and to send ``If-Modified-Since``; both are
    honoured through a per-coordinate cache, and a 304 serves the cached forecast.
    """

    source = MET_NORWAY

    def __init__(
        self,
        base_url: str | None = None,
        user_agent: str | None = None,
        *,
        client: httpx.Client | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        endpoint: str = "complete",
    ) -> None:
        self.base_url = (base_url or DEFAULT_MET_NORWAY_BASE).rstrip("/")
        self.user_agent = (user_agent or "").strip()
        self.endpoint = endpoint
        self._client = client
        self._timeout_s = timeout_s
        self._cache: dict[tuple[str, str], _MetCacheEntry] = {}

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = default_client(self._timeout_s, user_agent=self.user_agent or None)
        return self._client

    def forecast_url(self, lat: float, lon: float) -> str:
        # MET asks for at most 4 decimals (more is rejected / pointless at 1 km grid).
        return f"{self.base_url}/weatherapi/locationforecast/2.0/{self.endpoint}?lat={lat:.4f}&lon={lon:.4f}"

    def _check_user_agent(self) -> None:
        if not self.user_agent:
            raise WeatherError(
                "config",
                "MET_NO_USER_AGENT is not set: MET Norway's terms require a User-Agent identifying the "
                "operator with contact information (https://api.met.no/doc/TermsOfService); refusing to call "
                "with an anonymous or invented one",
                source=self.source,
            )
        if "@" not in self.user_agent and "http" not in self.user_agent.lower():
            log.warning(
                "weather: MET_NO_USER_AGENT=%r carries no contact address or URL; MET's terms ask for one",
                self.user_agent,
            )

    def forecast(self, lat: float, lon: float, hours: int = 48, *, now: datetime | None = None) -> ForecastSnapshot:
        self._check_user_agent()
        fetched_at = now or _utcnow()
        key = (f"{lat:.4f}", f"{lon:.4f}")
        cached = self._cache.get(key)
        url = self.forecast_url(lat, lon)

        if cached is not None and cached.expires_at is not None and fetched_at < cached.expires_at:
            # Terms: do not re-fetch before Expires. Same body, so the same snapshot; the fetch
            # time stays the cached one because nothing was fetched.
            return self._snapshot(cached, url, from_cache=True)

        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        if cached is not None and cached.last_modified:
            headers["If-Modified-Since"] = cached.last_modified
        status, body, resp_headers = _get_json(self.client, url, headers=headers, source=self.source)

        if status == 304:
            if cached is None:  # a 304 to an unconditional request: nothing to serve
                raise WeatherError("http", "MET Norway answered 304 without a cached forecast to serve", status=304, source=self.source)
            self._cache[key] = _MetCacheEntry(
                cached.payload, cached.points, cached.last_modified,
                _parse_http_date(resp_headers.get("Expires")) or cached.expires_at, cached.fetched_at,
            )
            return self._snapshot(self._cache[key], url, from_cache=True)

        assert body is not None
        points = parse_met_norway(body)
        entry = _MetCacheEntry(
            payload=dict(body),
            points=points,
            last_modified=resp_headers.get("Last-Modified"),
            expires_at=_parse_http_date(resp_headers.get("Expires")),
            fetched_at=fetched_at,
        )
        self._cache[key] = entry
        return self._snapshot(entry, url, from_cache=False)

    def _snapshot(self, entry: _MetCacheEntry, url: str, *, from_cache: bool) -> ForecastSnapshot:
        geometry = entry.payload.get("geometry") if isinstance(entry.payload.get("geometry"), Mapping) else {}
        coords = geometry.get("coordinates") if isinstance(geometry.get("coordinates"), list) else []
        lon = _float_or_none(coords[0]) if len(coords) > 0 else None
        lat = _float_or_none(coords[1]) if len(coords) > 1 else None
        props = entry.payload.get("properties") if isinstance(entry.payload.get("properties"), Mapping) else {}
        meta = props.get("meta") if isinstance(props.get("meta"), Mapping) else {}
        return ForecastSnapshot(
            source=self.source,
            source_url=url,
            latitude=lat if lat is not None else float("nan"),
            longitude=lon if lon is not None else float("nan"),
            fetched_at=entry.fetched_at,
            hours=entry.points,
            raw=entry.payload,
            provider_updated_at=_parse_iso_utc(meta.get("updated_at")),
            expires_at=entry.expires_at,
            from_cache=from_cache,
        )


def provider_from_env(*, client: httpx.Client | None = None, timeout_s: float = DEFAULT_TIMEOUT_S) -> OpenMeteoProvider | MetNorwayProvider:
    """Build the configured provider from ``WEATHER_PROVIDER`` / ``WEATHER_API_BASE`` /
    ``WEATHER_API_KEY`` / ``MET_NO_USER_AGENT`` (all in ``.env.example``).

    ``WEATHER_API_BASE`` applies to whichever provider is selected; when it is unset each
    provider uses its own default host. A misconfiguration (unknown provider name, MET without
    a User-Agent) raises ``WeatherError("config")`` here or on the first call — never later
    and never silently.
    """
    name = resolve_provider_name(os.getenv("WEATHER_PROVIDER"))
    base = (os.getenv("WEATHER_API_BASE") or "").strip() or None
    if name == MET_NORWAY:
        return MetNorwayProvider(base, os.getenv("MET_NO_USER_AGENT"), client=client, timeout_s=timeout_s)
    return OpenMeteoProvider(base, os.getenv("WEATHER_API_KEY"), client=client, timeout_s=timeout_s)


def snapshot_payload_json(snapshot: ForecastSnapshot) -> str:
    """The provider body as a compact JSON string for ``external_signals.payload_json``."""
    return json.dumps(snapshot.raw, separators=(",", ":"), ensure_ascii=False)
