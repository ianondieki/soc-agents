"""KMD CAP early warning (spec §5.3.13, §7.3.3, §7.3.7; CONFORMANCE C-12).

**Zero network.** Every request goes through ``httpx.MockTransport`` fed from
``tests/fixtures/cap/*.xml`` — constructed from the RSS 2.0 and OASIS CAP 1.2 documents, not
captured live (each file says so; see the README there) — or from CAP documents built inline
by :func:`_cap`. An autouse fixture breaks ``socket.connect`` / ``getaddrinfo`` as the weather
tests do, so a test that tried to reach meteo.go.ke would fail for the right reason.

What is pinned, in the order the brief ranked it:

* **silence is not good news** — a feed unreachable for three days reads ``unreachable`` and
  stale, never "no warnings"; a feed that answers but is 132 days old reads ``stale_feed``
  (the spec's own observation, reproduced from §5.3.13's 2026-05-07 fixture on 2026-09-16);
  feed-health rows are never counted as warnings by the Regions dashboard and never let a
  region look CALM; a region with no counties reads ``unmapped``;
* **the Met Department's statement, not ours** — severity, urgency, certainty, areas stored
  verbatim, ``valid_until`` = ``expires``; Update/Cancel honoured and never able to resurrect;
  ``Test`` status never becomes a warning; the no-``expires`` hold is bounded by
  ``CAP_STALE_DAYS``;
* **XML from the network is hostile until proven otherwise** — entity expansion and XXE are
  refused before any parser sees them, on the defusedxml path *and* the stdlib fallback; an
  oversized body is abandoned while streaming; an HTML error page is not an empty feed;
* **fail-soft** — timeout, 500, malformed XML, oversize, TLS: nothing raises out of the job,
  the last good rows keep their payload, ``last_error`` says why;
* the county→region map (one-to-many, unknown county rejects the lane, not the app);
* both operators seeded; neither ever sees the other's rows;
* the job ships disabled and re-checks its own flag.

The adversarial review's findings each have a test here, named in the test's docstring
(F01 non-UTF-8 entity bypass, F02 staleness against now, F03 freshness from feed health, F04
canonical county keys, F06 total deadline, F07 cancel ordering, F08 incomplete, F10
re-attribution, F15 compression, F16 verbatim raw_xml, F17 areaDesc punctuation), and the
round-3 replay findings (W02 header-phase deadline, over a real LOOPBACK socket — the one place
this file lets a socket open, and only to 127.0.0.1; NEW1 pending documents survive a 304 and an
outage; NEW2 a detached row is never extended; NEW3 punctuation is not a place).
"""

from __future__ import annotations

import json
import re
import socket
import ssl
import threading
import time
import tracemalloc
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from noc_agents.adapters import kmd_cap as adapter
from noc_agents.adapters.kmd_cap import (
    MAX_FEED_BYTES,
    XML_PARSER,
    CapError,
    KmdCapProvider,
    parse_cap_alert,
    parse_feed,
    parse_xml,
    split_area_desc,
)
from noc_agents.config import get_settings
from noc_agents.db.models import ExternalSignalRow
from noc_agents.pollers import kmd_cap as poller
from noc_agents.pollers.kmd_cap import ALERT_HOLD_WITHOUT_EXPIRES, CAP_JOB, cap_stale_days, poll
from noc_agents.realtime.hub import hub
from noc_agents.scheduler.loop import job_enabled, read_state, run_job
from noc_agents.services import dashboards
from noc_agents.services.signals import (
    CAP_FEED_MAX_SILENCE,
    CAP_POLL_INTERVAL_S,
    active_signals_count,
    cap_alerts,
    cap_feed_health,
    latest_row,
    list_signals,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "cap"
#: A self-signed certificate committed for the loopback TLS tests only (see its README).
TLS_CERT = Path(__file__).resolve().parents[1] / "fixtures" / "tls" / "test_only_cert.pem"
TLS_KEY = Path(__file__).resolve().parents[1] / "fixtures" / "tls" / "test_only_key.pem"
FEED_URL = "https://meteo.go.ke/api/cap/rss.xml"
RAIN_URL = "https://meteo.go.ke/api/cap/fixture-2026-05-07-wny-heavy-rain.xml"
WIND_URL = "https://meteo.go.ke/api/cap/fixture-2026-05-06-nbi-strong-winds.xml"
RAIN_ID = "fixture-2026-05-07-wny-heavy-rain"
WIND_ID = "fixture-2026-05-06-nbi-strong-winds"

#: The heavy-rain alert: sent 2026-05-07 03:00 UTC, expires 2026-05-09 03:00 UTC.
T0 = datetime(2026, 5, 7, 12, 0)
RAIN_EXPIRES = datetime(2026, 5, 9, 3, 0)
#: §5.3.13: "the 2026-05-07 fixture is stale on 2026-09-16".
SPEC_STALE_DAY = datetime(2026, 9, 16, 12, 0)
LAST_MODIFIED = "Thu, 07 May 2026 03:05:00 GMT"
SAFARICOM_REGIONS = {"NBI_E", "NBI_W", "MTK", "CST", "RFT", "WNY"}


# ------------------------------------------------------------------------------ fixtures


#: The real socket functions, captured before any fixture replaces them, so the loopback-only
#: fixture below can re-admit 127.0.0.1 — and nothing else — for the W02 deadline tests.
_REAL_CONNECT = socket.socket.connect
_REAL_CREATE_CONNECTION = socket.create_connection
_REAL_GETADDRINFO = socket.getaddrinfo


@pytest.fixture(autouse=True)
def no_sockets(monkeypatch):
    """Any attempt to open a real connection fails loudly. MockTransport never gets here."""

    def boom(*args, **kwargs):
        raise AssertionError("a KMD CAP test tried to open a network socket")

    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket.socket, "connect_ex", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)


#: Hosts the loopback-only guard admits. A DNS test adds its own fake name, which its fake
#: resolver maps to 127.0.0.1 — otherwise the guard, not the stall, would end the test.
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost"}


@pytest.fixture()
def loopback_only(monkeypatch):
    """Re-admit connections to the loopback interface only. The W02 header-phase deadline cannot
    be tested through MockTransport — the bug lived below httpx, in socket reads — so these tests
    run a real server on 127.0.0.1. Any other host still fails exactly as before."""

    def loopback(host) -> bool:
        return str(host) in _LOOPBACK_HOSTS

    def connect(self, address):
        if not loopback(address[0]):
            raise AssertionError(f"a KMD CAP test tried to reach {address!r}")
        return _REAL_CONNECT(self, address)

    def create_connection(address, *args, **kwargs):
        if not loopback(address[0]):
            raise AssertionError(f"a KMD CAP test tried to reach {address!r}")
        return _REAL_CREATE_CONNECTION(address, *args, **kwargs)

    def getaddrinfo(host, *args, **kwargs):
        if not loopback(host):
            raise AssertionError(f"a KMD CAP test tried to resolve {host!r}")
        return _REAL_GETADDRINFO(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


def _drip_server(response: bytes, gap: float) -> int:
    """A one-shot loopback HTTP server that sends ``response`` one byte every ``gap`` seconds —
    each socket read answered promptly, so no per-read timeout ever trips."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def run():
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        try:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                data += chunk
            for byte in response:
                conn.sendall(bytes([byte]))
                time.sleep(gap)
            if not response:
                time.sleep(8.0)  # silent: longer than any per-read timeout the test gives the client
        except OSError:
            pass  # the client's watchdog shut the connection: exactly what is being tested
        finally:
            conn.close()
            srv.close()

    threading.Thread(target=run, daemon=True).start()
    return port


@pytest.fixture(autouse=True)
def cap_env(monkeypatch):
    """Every test starts with the lane off and no configuration leaking in from the shell."""
    for key in ("WEATHER_ENABLED", "CAP_STALE_DAYS", "KMD_CAP_FEED_URL", "MET_NO_USER_AGENT", "NOC_USE_TRUSTSTORE"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture()
def clean_hub():
    hub._history.clear()
    yield hub
    hub._history.clear()


def _read(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class Server:
    """A fake meteo.go.ke: URL → response, every request recorded."""

    def __init__(self) -> None:
        self.routes: dict[str, object] = {
            FEED_URL: (200, _read("kmd_rss.xml"), {"Last-Modified": LAST_MODIFIED}),
            RAIN_URL: (200, _read("cap_wny_heavy_rain.xml"), {}),
            WIND_URL: (200, _read("cap_nbi_strong_winds.xml"), {}),
        }
        self.calls: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        route = self.routes.get(str(request.url))
        if route is None:
            return httpx.Response(404, content=b"not found")
        if callable(route):
            return route(request)
        status, body, headers = route
        return httpx.Response(status, content=body, headers={"content-type": "application/xml", **headers})

    def provider(self) -> KmdCapProvider:
        return KmdCapProvider(client=httpx.Client(transport=httpx.MockTransport(self.handler)))

    def urls(self) -> list[str]:
        return [str(r.url) for r in self.calls]


def _cap(
    identifier: str,
    *,
    sent: str = "2026-05-07T09:00:00+03:00",
    msg_type: str = "Alert",
    status: str = "Actual",
    severity: str = "Severe",
    expires: str | None = "2026-05-10T09:00:00+03:00",
    areas: tuple[str, ...] = ("Kisumu",),
    references: str | None = None,
) -> bytes:
    """A CAP 1.2 document built inline for one test (obviously constructed; no file needed)."""
    refs = f"<references>{references}</references>" if references else ""
    exp = f"<expires>{expires}</expires>" if expires else ""
    area_xml = "".join(f"<area><areaDesc>{a}</areaDesc></area>" for a in areas)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">'
        f"<identifier>{identifier}</identifier><sender>forecasting@meteo.go.ke</sender><sent>{sent}</sent>"
        f"<status>{status}</status><msgType>{msg_type}</msgType><scope>Public</scope>{refs}"
        f"<info><language>en-US</language><category>Met</category><event>Heavy Rainfall</event>"
        f"<urgency>Expected</urgency><severity>{severity}</severity><certainty>Likely</certainty>{exp}"
        f"<headline>test</headline>{area_xml}</info></alert>"
    ).encode("utf-8")


def _rss(*items: tuple[str, str]) -> bytes:
    """An RSS 2.0 feed of ``(link, pubDate)`` items."""
    body = "".join(f"<item><title>t</title><link>{link}</link><guid>{link}</guid><pubDate>{pub}</pubDate></item>" for link, pub in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>{body}</channel></rss>'.encode("utf-8")


def _enable(monkeypatch) -> None:
    monkeypatch.setenv("WEATHER_ENABLED", "true")


def _rows(session, operator: str = "safaricom") -> list[ExternalSignalRow]:
    return list(
        session.scalars(
            select(ExternalSignalRow)
            .where(ExternalSignalRow.operator_id == operator, ExternalSignalRow.source == "KMD_CAP")
            .order_by(ExternalSignalRow.external_id)
        ).all()
    )


def _by_eid(session, operator: str = "safaricom") -> dict[str, ExternalSignalRow]:
    return {r.external_id: r for r in _rows(session, operator)}


def _with_regions(settings, **regions):
    """A copy of ``settings`` whose operator profile has exactly these ``{code: [counties]}``."""
    template = next(iter(settings.operator.regions.values()))
    new = {code: template.model_copy(update={"counties": list(counties)}) for code, counties in regions.items()}
    return settings.model_copy(update={"operator": settings.operator.model_copy(update={"regions": new})})


# ------------------------------------------------------------------------------ fixtures are honest


def test_fixtures_say_they_were_constructed_not_captured():
    for path in sorted(FIXTURES.glob("*.xml")):
        text = path.read_text(encoding="utf-8")
        head = text.split("-->", 1)[0]
        assert "_provenance: captured_live=false" in head, path.name
        assert "NOT a live capture" in head, path.name
    readme = (FIXTURES / "README.md").read_text(encoding="utf-8")
    assert "constructed, not captured" in readme


def test_fixture_identifiers_are_synthetic_so_nobody_mistakes_them_for_kmd():
    for name in ("cap_wny_heavy_rain.xml", "cap_nbi_strong_winds.xml"):
        assert parse_cap_alert(_read(name)).identifier.startswith("fixture-")


def test_adapter_and_poller_import_no_llm_or_mcp():
    root = Path(__file__).resolve().parents[2] / "src" / "noc_agents"
    for rel in ("adapters/kmd_cap.py", "pollers/kmd_cap.py"):
        for line in (root / rel).read_text(encoding="utf-8").splitlines():
            assert not line.strip().startswith(("import anthropic", "from anthropic", "import mcp", "from mcp", "import feedparser")), (rel, line)


# ------------------------------------------------------------------------------ parsing


def test_parse_feed_reads_items_links_and_dates_as_naive_utc():
    items = parse_feed(_read("kmd_rss.xml"))
    assert [i.link for i in items] == [RAIN_URL, WIND_URL]
    assert items[0].published == datetime(2026, 5, 7, 3, 0) and items[0].published.tzinfo is None
    assert items[1].published == datetime(2026, 5, 6, 12, 0)
    assert items[0].guid == RAIN_ID and items[0].embedded_alert is None


def test_parse_cap_alert_keeps_what_kmd_said_verbatim():
    alert = parse_cap_alert(_read("cap_wny_heavy_rain.xml"), source_url=RAIN_URL)
    assert (alert.identifier, alert.sender, alert.status, alert.msg_type, alert.scope) == (
        RAIN_ID, "forecasting@meteo.go.ke", "Actual", "Alert", "Public")
    assert (alert.category, alert.event, alert.urgency, alert.severity, alert.certainty) == (
        "Met", "Heavy Rainfall", "Expected", "Severe", "Likely")
    # +03:00 stamps become naive UTC; nothing else about them changes.
    assert alert.sent == datetime(2026, 5, 7, 3, 0) and alert.effective == datetime(2026, 5, 7, 3, 0)
    assert alert.onset == datetime(2026, 5, 7, 9, 0) and alert.expires == RAIN_EXPIRES
    assert alert.headline == "Heavy rainfall expected over parts of Western and Nyanza"
    assert alert.language == "en-US" and alert.sender_name == "Kenya Meteorological Department"
    # English info is primary; the Swahili duplicate never doubles the county list.
    assert alert.counties == ("Migori", "Nyamira", "Bungoma", "Busia")
    assert [len(a.polygons) for a in alert.areas] == [1, 1, 1, 1]
    payload = alert.as_payload()
    assert payload["severity"] == "Severe" and payload["expires"] == "2026-05-09T03:00:00Z"
    assert alert.raw_xml.startswith("<?xml")


def test_parse_cap_alert_without_expires_is_none_not_a_default():
    alert = parse_cap_alert(_read("cap_nbi_strong_winds.xml"))
    assert alert.expires is None and alert.severity == "Moderate"
    assert alert.effective == alert.sent  # CAP's own default: effective = sent
    assert alert.counties == ("Nairobi", "Kiambu", "Turkana")


@pytest.mark.parametrize(
    "body, fragment",
    [
        (_cap("x").replace(b"<identifier>x</identifier>", b""), "no <identifier>"),
        (_cap("x").replace(b"<sent>2026-05-07T09:00:00+03:00</sent>", b""), "<sent>"),
        (_cap("x", sent="2026-05-07T09:00:00"), "no UTC offset"),  # CAP 1.2 requires an offset; we never guess EAT
        (b"<?xml version='1.0'?><notcap/>", "no <alert>"),
    ],
)
def test_parse_cap_alert_rejects_a_half_understood_document(body, fragment):
    with pytest.raises(CapError) as err:
        parse_cap_alert(body)
    assert err.value.kind == "malformed" and fragment in str(err.value)


def test_references_are_the_identifiers_of_the_triples():
    alert = parse_cap_alert(_cap("u", msg_type="Update", references="kmd,orig-1,2026-05-07T09:00:00+03:00 kmd,orig-2,2026-05-07T10:00:00+03:00"))
    assert alert.references == ("orig-1", "orig-2")


def test_split_area_desc_splits_lists_but_never_a_county_name():
    assert split_area_desc("Migori, Nyamira and Busia") == ("Migori", "Nyamira", "Busia")
    assert split_area_desc("Kisumu; Siaya & Vihiga") == ("Kisumu", "Siaya", "Vihiga")
    assert split_area_desc("Elgeyo/Marakwet") == ("Elgeyo/Marakwet",)  # "/" is part of the name
    assert split_area_desc("Taita-Taveta") == ("Taita-Taveta",)
    assert split_area_desc("") == () and split_area_desc(None) == ()


def test_an_html_error_page_served_with_200_is_malformed_not_an_empty_feed():
    with pytest.raises(CapError) as err:
        parse_feed(b"<html><body>Service Unavailable</body></html>")
    assert err.value.kind == "malformed" and "not an RSS or Atom feed" in str(err.value)


def test_an_atom_feed_with_an_inline_alert_is_read_without_a_second_fetch():
    alert_xml = _cap("inline-1").split(b"?>", 1)[1].decode()
    atom = (
        '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><id>inline-1</id>'
        f'<updated>2026-05-07T06:00:00Z</updated><content type="text/xml">{alert_xml}</content></entry></feed>'
    ).encode()
    items = parse_feed(atom)
    assert len(items) == 1 and items[0].embedded_alert is not None
    assert items[0].embedded_alert.identifier == "inline-1" and items[0].published == datetime(2026, 5, 7, 6, 0)


# ------------------------------------------------------------------------------ XML safety

BILLION_LAUGHS = (
    b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
    b'<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">'
    b'<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">]>'
    b"<rss><channel><item><title>&lol3;</title></item></channel></rss>"
)
XXE = (
    b'<?xml version="1.0"?><!DOCTYPE a [<!ENTITY xxe SYSTEM "file:///c:/windows/win.ini">]>'
    b'<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2"><identifier>&xxe;</identifier></alert>'
)


@pytest.fixture(params=["defusedxml", "stdlib"])
def parser_mode(request, monkeypatch):
    """Run a safety test on BOTH paths: the hardened parser, and the stdlib fallback a clean
    install gets (defusedxml is only here because nbconvert pulled it in)."""
    if request.param == "stdlib":
        monkeypatch.setattr(adapter, "_defused_fromstring", None)
    return request.param


def test_this_interpreter_uses_defusedxml_when_it_is_importable():
    assert XML_PARSER == "defusedxml"


@pytest.mark.parametrize("body", [BILLION_LAUGHS, XXE], ids=["billion-laughs", "xxe"])
def test_entity_declarations_are_refused_before_any_parser_runs(parser_mode, body, monkeypatch):
    def parser_reached(*a, **k):
        raise AssertionError("the XML parser saw a body that declares an entity")

    monkeypatch.setattr(adapter, "_defused_fromstring", parser_reached if parser_mode == "defusedxml" else None)
    monkeypatch.setattr(adapter, "_stdlib_fromstring", parser_reached)
    with pytest.raises(CapError) as err:
        parse_xml(body, what="test body", max_bytes=MAX_FEED_BYTES)
    assert err.value.kind == "malformed" and "declares an XML entity" in str(err.value)


def test_defusedxml_is_a_second_wall_if_the_prescan_ever_misses(monkeypatch):
    """Defence in depth: with the pre-scan disabled, the hardened parser still refuses."""
    monkeypatch.setattr(adapter, "_ENTITY_DECL", re.compile(rb"(?!x)x"))  # matches nothing
    with pytest.raises(CapError) as err:
        parse_xml(BILLION_LAUGHS, what="test body", max_bytes=MAX_FEED_BYTES)
    assert err.value.kind == "malformed" and "hardening" in str(err.value)


def test_an_oversized_body_is_refused_before_parsing(parser_mode, monkeypatch):
    monkeypatch.setattr(adapter, "_stdlib_fromstring", lambda *a, **k: (_ for _ in ()).throw(AssertionError("parsed")))
    with pytest.raises(CapError) as err:
        parse_xml(b"<rss>" + b" " * 2000 + b"</rss>", what="test body", max_bytes=1000)
    assert err.value.kind == "oversize"


def test_a_streamed_body_is_abandoned_at_the_cap_even_without_content_length(monkeypatch):
    monkeypatch.setattr(adapter, "MAX_FEED_BYTES", 1000)

    def endless(request):
        return httpx.Response(200, content=iter([b"<rss>" + b" " * 600] * 50))  # chunked: no Content-Length

    provider = KmdCapProvider(client=httpx.Client(transport=httpx.MockTransport(endless)))
    with pytest.raises(CapError) as err:
        provider.fetch_feed()
    assert err.value.kind == "oversize" and "while streaming" in str(err.value)


def _encodings_of(body: bytes) -> list[tuple[str, bytes]]:
    """``body`` (UTF-8) re-encoded every way F01 used to get past the byte scan."""
    text = body.decode("utf-8")
    return [
        ("utf-16-bom", text.encode("utf-16")),
        ("utf-16le-nobom", text.encode("utf-16-le")),
        ("utf-16be-nobom", text.encode("utf-16-be")),
        ("utf-16be-bom", b"\xfe\xff" + text.encode("utf-16-be")),
        ("utf-32-bom", text.encode("utf-32")),
        ("utf-32le-nobom", text.encode("utf-32-le")),
        ("utf-32be-nobom", text.encode("utf-32-be")),
    ]


UTF16_LAUGHS = BILLION_LAUGHS.replace(b'<?xml version="1.0"?>', b'<?xml version="1.0" encoding="UTF-16"?>')


@pytest.mark.parametrize("label, body", _encodings_of(UTF16_LAUGHS) + _encodings_of(XXE), ids=lambda v: v if isinstance(v, str) else "")
def test_f01_a_non_utf8_body_is_refused_before_any_parser_on_both_paths(parser_mode, label, body, monkeypatch):
    """F01: a UTF-16/UTF-32 body spells ``<!ENTITY`` as ``<\\0!\\0E...``, so the byte scan never
    matched it and the declarations reached expat. Now nothing that is not UTF-8 is parsed."""

    def parser_reached(*a, **k):
        raise AssertionError(f"a parser saw a {label} body")

    monkeypatch.setattr(adapter, "_defused_fromstring", parser_reached if parser_mode == "defusedxml" else None)
    monkeypatch.setattr(adapter, "_stdlib_fromstring", parser_reached)
    with pytest.raises(CapError) as err:
        parse_xml(body, what="test body", max_bytes=MAX_FEED_BYTES)
    assert err.value.kind == "malformed" and "only UTF-8 is accepted" in str(err.value)


@pytest.mark.parametrize("body, fragment", [
    ('<?xml version="1.0" encoding="ISO-8859-1"?><rss>M\xfcrang\u2019a</rss>'.encode("latin-1", "replace"), "declares encoding 'ISO-8859-1'"),
    (b"<rss>\xfc</rss>", "not valid UTF-8"),
    (b"<rss>\x00</rss>", "NUL bytes"),
])
def test_f01_other_non_utf8_bodies_are_refused_too(body, fragment):
    with pytest.raises(CapError) as err:
        parse_xml(body, what="test body", max_bytes=MAX_FEED_BYTES)
    assert fragment in str(err.value)


@pytest.mark.parametrize("label, body", [("utf-8", BILLION_LAUGHS), *_encodings_of(UTF16_LAUGHS)[:1]])
def test_f01_the_stdlib_parser_refuses_entity_machinery_by_construction(label, body):
    """Defence in depth that does not depend on byte patterns: fed a billion-laughs payload
    DIRECTLY (no UTF-8 gate, no scan), the guarded expat parser refuses at the DOCTYPE, before
    the internal subset is read — in any encoding, in milliseconds, in a few hundred KB."""
    tracemalloc.start()
    started = time.perf_counter()
    try:
        with pytest.raises(ValueError, match="internal subset"):
            adapter._stdlib_fromstring(body)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert time.perf_counter() - started < 0.5 and peak < 5_000_000, (label, peak)


def test_f01_the_stdlib_path_holds_even_with_the_gate_and_the_scan_switched_off(monkeypatch):
    monkeypatch.setattr(adapter, "_defused_fromstring", None)
    monkeypatch.setattr(adapter, "_require_utf8", lambda body, what: "")
    monkeypatch.setattr(adapter, "_ENTITY_DECL", re.compile(rb"(?!x)x"))
    for body in (BILLION_LAUGHS, XXE, UTF16_LAUGHS.decode().encode("utf-16")):
        with pytest.raises(CapError) as err:
            parse_xml(body, what="test body", max_bytes=MAX_FEED_BYTES)
        assert "refused by the XML hardening" in str(err.value)


def test_the_guarded_stdlib_parser_builds_the_same_tree_as_elementtree_and_allows_a_bare_doctype():
    for name in ("kmd_rss.xml", "cap_wny_heavy_rain.xml", "cap_nbi_strong_winds.xml"):
        body = _read(name)
        mine = [(e.tag, (e.text or "").strip(), sorted(e.attrib.items())) for e in adapter._stdlib_fromstring(body).iter()]
        theirs = [(e.tag, (e.text or "").strip(), sorted(e.attrib.items())) for e in ET.fromstring(body).iter()]
        assert mine == theirs, name
    rss091 = (b'<?xml version="1.0"?><!DOCTYPE rss PUBLIC "-//Netscape Communications//DTD RSS 0.91//EN" '
              b'"http://my.netscape.com/publish/formats/rss-0.91.dtd"><rss><channel><item><title>a &amp; b</title>'
              b'<link>https://x.test/a.xml</link></item></channel></rss>')
    assert [i.link for i in parse_feed(rss091)] == ["https://x.test/a.xml"]  # legal RSS, nothing fetched


def test_f16_raw_xml_is_the_document_exactly_as_sent():
    body = _cap("u-1", areas=("Murang\u2019a", "M\u00fcranga"))
    alert = parse_cap_alert(body)
    assert alert.raw_xml == body.decode("utf-8") and "Murang\u2019a" in alert.raw_xml
    assert ET.fromstring(alert.raw_xml.encode("utf-8")).tag.endswith("alert")  # re-parseable
    with pytest.raises(CapError):  # and a UTF-16 copy is refused, never stored as mojibake
        parse_cap_alert(body.decode("utf-8").replace('encoding="UTF-8"', 'encoding="UTF-16"').encode("utf-16"))


class _Clock:
    """A monotonic clock that jumps ``step`` seconds per reading."""

    def __init__(self, step: float) -> None:
        self.t, self.step = 0.0, step

    def __call__(self) -> float:
        self.t += self.step
        return self.t


def test_f06_a_slow_drip_is_abandoned_at_the_total_deadline(monkeypatch):
    """F06: httpx's timeout is per read, so one byte every nine seconds never trips it."""
    monkeypatch.setattr(adapter, "_clock", _Clock(step=4.0))  # every chunk "takes" 4 s

    def drip(request):
        return httpx.Response(200, content=iter([b" "] * 1000))

    provider = KmdCapProvider(client=httpx.Client(transport=httpx.MockTransport(drip)))
    with pytest.raises(CapError) as err:
        provider.fetch_feed()
    assert err.value.kind == "timeout" and "10 s total deadline" in str(err.value)


def test_f06_the_poller_reports_a_slow_drip_as_a_fail_soft_timeout(tmp_db, monkeypatch):
    settings, session = tmp_db
    _enable(monkeypatch)
    monkeypatch.setattr(adapter, "_clock", _Clock(step=4.0))
    server = Server()
    server.routes[FEED_URL] = lambda req: httpx.Response(200, content=iter([b" "] * 1000))
    result = poll(session, settings, provider=server.provider(), now=T0)
    assert result.tools[0]["error_kind"] == "timeout"
    assert cap_feed_health(session, "safaricom", "WNY", T0)["state"] == "unreachable"


def test_f15_identity_is_requested_and_a_compressed_answer_is_refused_unread(monkeypatch):
    import gzip

    seen: list[httpx.Request] = []
    consumed: list[bytes] = []
    bomb = gzip.compress(b" " * 5_000_000)  # ~5 KB on the wire, 5 MB once inflated

    def body():
        consumed.append(bomb)
        yield bomb

    def gzipped(request):
        seen.append(request)
        return httpx.Response(200, content=body(), headers={"Content-Encoding": "gzip"})

    provider = KmdCapProvider(client=httpx.Client(transport=httpx.MockTransport(gzipped)))
    with pytest.raises(CapError) as err:
        provider.fetch_feed()
    assert seen[0].headers["Accept-Encoding"] == "identity"
    assert err.value.kind == "malformed" and "Content-Encoding 'gzip'" in str(err.value)
    assert consumed == []  # refused before a single byte was read, let alone inflated


HEADER_DRIP_200 = b"HTTP/1.1 200 OK\r\nX-Pad: " + b"a" * 40 + b"\r\nContent-Length: 5\r\n\r\n<rss>"
HEADER_DRIP_304 = b"HTTP/1.1 304 Not Modified\r\nX-Pad: " + b"a" * 40 + b"\r\n\r\n"


def _continue_server(count: int, gap: float) -> int:
    """A loopback server that answers with ``count`` whole ``100 Continue`` interim responses,
    ``gap`` seconds apart, then a 304 — httpcore skips each 1xx and keeps reading headers."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def run():
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        try:
            data = b""
            while b"\r\n\r\n" not in data:
                data += conn.recv(4096)
            for _ in range(count):
                conn.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")
                time.sleep(gap)
            conn.sendall(b"HTTP/1.1 304 Not Modified\r\n\r\n")
        except OSError:
            pass
        finally:
            conn.close()
            srv.close()

    threading.Thread(target=run, daemon=True).start()
    return port


W02_CASES = {
    "200-headers-dripped": (lambda: _drip_server(HEADER_DRIP_200, 0.07), 1.0),   # ~6.4 s of headers
    "304-headers-dripped": (lambda: _drip_server(HEADER_DRIP_304, 0.08), 1.0),   # ~6.2 s; a 304 skipped the check
    "100-continue-storm": (lambda: _continue_server(60, 0.07), 1.0),             # 60 interim responses, ~4.2 s
    "silent-after-request": (lambda: _drip_server(b"", 0.0), 6.0),               # nothing; client per-read 6 s
}


@pytest.mark.parametrize("label", sorted(W02_CASES))
def test_w02_the_total_deadline_covers_the_header_phase_over_a_real_socket(loopback_only, label):
    """W02: the deadline was checked only in the body loop, so a server that dripped its
    HEADERS (or sent endless 1xx responses, or a slow 304) ran 9-11 s against a 1 s budget. The
    DeadlineWatchdog shuts the socket at the deadline, and no single wait may outlast the budget."""
    make_server, per_read = W02_CASES[label]
    port = make_server()
    provider = KmdCapProvider(
        f"http://127.0.0.1:{port}/rss.xml", client=httpx.Client(timeout=httpx.Timeout(per_read)), timeout_s=0.6
    )
    started = time.monotonic()
    with pytest.raises(CapError) as err:
        provider.fetch_feed(if_modified_since=LAST_MODIFIED)
    elapsed = time.monotonic() - started
    assert err.value.kind == "timeout" and "total deadline" in str(err.value)
    assert elapsed < 2.5, f"{label}: {elapsed:.2f}s against a 0.6 s budget"


def test_w02_a_304_is_checked_against_the_deadline_before_it_is_returned(monkeypatch):
    """The 304 branch returned without consulting the deadline at all."""
    monkeypatch.setattr(adapter, "_clock", _Clock(step=20.0))  # the exchange "took" past the budget
    provider = KmdCapProvider(client=httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(304))))
    with pytest.raises(CapError) as err:
        provider.fetch_feed(if_modified_since=LAST_MODIFIED)
    assert err.value.kind == "timeout"


def test_w02_every_request_opens_its_own_connection_and_is_traced():
    """The watchdog can only shut a socket it has seen: Connection: close forces a fresh
    connection (and so a connect_tcp trace) per request, and the trace hook is attached."""
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=_read("kmd_rss.xml"))

    KmdCapProvider(client=httpx.Client(transport=httpx.MockTransport(handler))).fetch_feed()
    assert seen[0].headers["Connection"] == "close" and callable(seen[0].extensions.get("trace"))


_CRLF = bytes([13, 10])


@lru_cache(maxsize=1)
def _loopback_tls_is_intercepted() -> bool:
    """True when this machine re-signs loopback TLS, as an antivirus "web shield" does.

    The test server offers a self-signed certificate, so the certificate the client receives must
    be its own issuer. When a local interceptor sits in the path the client is handed a re-issued
    copy instead (seen here: "Avast Web/Mail Shield Self-signed Root"), which no cafile of ours
    can verify — and the response is then paced by the interceptor's buffer rather than by the
    server, so what these tests measure is not there to measure. They skip, rather than trust an
    interceptor or drop verification to stay green.
    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(TLS_CERT), str(TLS_KEY))

    def run():
        conn = None
        try:
            raw, _ = listener.accept()
            conn = context.wrap_socket(raw, server_side=True)
            conn.recv(64)
        except (OSError, ssl.SSLError):
            pass
        finally:
            if conn is not None:
                conn.close()
            listener.close()

    threading.Thread(target=run, daemon=True).start()
    probe = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    probe.check_hostname = False
    probe.verify_mode = ssl.CERT_NONE
    try:
        with probe.wrap_socket(socket.create_connection(("127.0.0.1", listener.getsockname()[1]), timeout=5)) as sock:
            served = sock.getpeercert(binary_form=True)  # parsed form is empty without verification
            sock.send(bytes([120]))
    except (OSError, ssl.SSLError):
        return False  # the probe says nothing; let the test itself report what it finds
    return bool(served) and served != ssl.PEM_cert_to_DER_cert(TLS_CERT.read_text())


def _skip_if_tls_is_intercepted() -> None:
    if _loopback_tls_is_intercepted():
        pytest.skip("a local TLS interceptor re-signs loopback connections on this machine: it, "
                    "not the test server, would be pacing the bytes")



def _tls_server(payload: bytes, gap: float, *, silent: bool = False) -> int:
    """A one-shot loopback HTTPS server using the committed TEST-ONLY certificate. It completes
    the handshake, reads the request, then drips ``payload`` a byte every ``gap`` seconds (or,
    ``silent``, says nothing for longer than any per-read timeout the tests use)."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(TLS_CERT), str(TLS_KEY))

    def run():
        conn = None
        try:
            raw, _ = listener.accept()
            conn = context.wrap_socket(raw, server_side=True)
            data = b""
            while _CRLF + _CRLF not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                data += chunk
            if silent:
                time.sleep(8.0)
                return
            for byte in payload:
                conn.sendall(bytes([byte]))
                time.sleep(gap)
        except (OSError, ssl.SSLError):
            pass  # the client's watchdog shut the connection: exactly what is being tested
        finally:
            if conn is not None:
                conn.close()
            listener.close()

    threading.Thread(target=run, daemon=True).start()
    return listener.getsockname()[1]


def _slow_proxy(upstream_port: int, gap: float) -> int:
    """A loopback TCP proxy that forwards client bytes at once and server bytes one every ``gap``
    seconds — so the TLS HANDSHAKE itself arrives as a drip."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def run():
        try:
            client, _ = listener.accept()
            upstream = socket.create_connection(("127.0.0.1", upstream_port))

            def forward_up():
                try:
                    while chunk := client.recv(4096):
                        upstream.sendall(chunk)
                except OSError:
                    pass

            threading.Thread(target=forward_up, daemon=True).start()
            while chunk := upstream.recv(4096):
                for byte in chunk:
                    client.sendall(bytes([byte]))
                    time.sleep(gap)
        except OSError:
            pass
        finally:
            listener.close()

    threading.Thread(target=run, daemon=True).start()
    return listener.getsockname()[1]


def _tls_client(per_read: float) -> httpx.Client:
    """A client that trusts the TEST-ONLY certificate through its own ``verify`` setting —
    certificate and hostname verification stay fully on; no production code changes."""
    return httpx.Client(timeout=httpx.Timeout(per_read), verify=ssl.create_default_context(cafile=str(TLS_CERT)))


_TLS_HEADERS = b"HTTP/1.1 200 OK" + _CRLF + b"X-Pad: " + b"z" * 80 + _CRLF + b"Content-Length: 5" + _CRLF + _CRLF + b"<rss>"
_TLS_STORM = (b"HTTP/1.1 100 Continue" + _CRLF + _CRLF) * 80 + b"HTTP/1.1 304 Not Modified" + _CRLF + _CRLF
_TLS_OK = b"HTTP/1.1 200 OK" + _CRLF + b"Content-Length: 5" + _CRLF + _CRLF + b"<rss>"

TLS_CASES = {
    # ~5.4 s of dripped response headers over TLS.
    "tls-headers-dripped": (lambda: _tls_server(_TLS_HEADERS, 0.05), 1.0),
    # 80 whole 100-Continue interim responses, dripped byte by byte (~4 s).
    "tls-100-continue-storm": (lambda: _tls_server(_TLS_STORM, 0.002), 1.0),
    # The server's first TLS flight dripped through a proxy (~7 s): the handshake itself. A
    # REGRESSION GUARD: CPython already bounds a handshake by the socket timeout as one total
    # deadline, which the round-3 per-wait cap sets to the budget; the watchdog covers it too.
    "tls-handshake-dripped": (lambda: _slow_proxy(_tls_server(_TLS_OK, 0.0), 0.005), 1.0),
    # Nothing after the handshake; client per-read 6 s. A REGRESSION GUARD: already bounded
    # before this fix, by the round-3 per-wait cap, not by the watchdog (see the report).
    "tls-silent-after-request": (lambda: _tls_server(b"", 0.0, silent=True), 6.0),
}


@pytest.mark.parametrize("label", sorted(TLS_CASES))
def test_w02_tls_the_total_deadline_holds_over_https(loopback_only, label):
    """W02-TLS: for https httpcore wraps the socket in an SSLSocket, which DETACHES the plain
    socket the watchdog had recorded; at the deadline shutdown hit fileno -1, the error was
    swallowed, and both production feeds (both https) had no header-phase bound at all. The
    watchdog now owns a duplicate descriptor of the connection, which survives the wrap."""
    _skip_if_tls_is_intercepted()
    make_server, per_read = TLS_CASES[label]
    port = make_server()
    client = _tls_client(per_read)
    provider = KmdCapProvider(f"https://127.0.0.1:{port}/rss.xml", client=client, timeout_s=0.6)
    started = time.monotonic()
    with pytest.raises(CapError) as err:
        provider.fetch_feed(if_modified_since=LAST_MODIFIED)
    elapsed = time.monotonic() - started
    assert err.value.kind == "timeout" and "total deadline" in str(err.value), str(err.value)
    assert elapsed < 2.5, f"{label}: {elapsed:.2f}s against a 0.6 s budget"


def test_w02_tls_a_normal_https_fetch_still_works_and_verifies_the_certificate(loopback_only):
    """The watchdog must not disturb a healthy exchange, and verification is really on: the same
    server is refused by a client that does not trust the test certificate."""
    _skip_if_tls_is_intercepted()
    body = _read("kmd_rss.xml")
    ok = b"HTTP/1.1 200 OK" + _CRLF + b"Content-Length: " + str(len(body)).encode() + _CRLF + _CRLF + body
    port = _tls_server(ok, 0.0)
    feed = KmdCapProvider(f"https://127.0.0.1:{port}/rss.xml", client=_tls_client(5.0), timeout_s=5.0).fetch_feed()
    assert [i.link for i in feed.items] == [RAIN_URL, WIND_URL]
    port = _tls_server(ok, 0.0)
    with pytest.raises(CapError) as err:
        KmdCapProvider(f"https://127.0.0.1:{port}/rss.xml", client=httpx.Client(timeout=5.0), timeout_s=5.0).fetch_feed()
    assert err.value.kind == "tls"


def test_w02_no_watchdog_thread_outlives_its_exchange(loopback_only):
    """Every exchange — refused, timed out or successful — ends with its timer thread joined."""
    port = _tls_server(_TLS_HEADERS, 0.05)
    with pytest.raises(CapError):
        KmdCapProvider(f"https://127.0.0.1:{port}/rss.xml", client=_tls_client(1.0), timeout_s=0.5).fetch_feed()
    KmdCapProvider(client=httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, content=_read("kmd_rss.xml"))))).fetch_feed()
    alive = [t for t in threading.enumerate() if t.name == adapter.DeadlineWatchdog.THREAD_NAME and t.is_alive()]
    assert alive == []


class _Stream:
    """The one method of an httpcore network stream the watchdog uses."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock

    def get_extra_info(self, name: str):
        return self._sock if name == "socket" else None


def test_w02_the_watchdogs_handle_survives_the_socket_being_detached(loopback_only):  # socketpair() is loopback TCP on Windows
    """What wrap_socket does to the plain socket, done directly: the object the trace saw is
    detached (fileno -1) and a new object owns the descriptor. Shutdown must still reach the
    connection — the peer sees end-of-stream."""
    a, b = socket.socketpair()
    watchdog = adapter.DeadlineWatchdog(60.0)
    try:
        watchdog.trace("connection.connect_tcp.complete", {"return_value": _Stream(a)})
        owner = socket.socket(fileno=a.detach())  # SSLSocket._create does this
        assert a.fileno() == -1
        watchdog._fire()
        b.settimeout(2.0)
        assert b.recv(16) == b""  # connection shut down although the recorded object was detached
        owner.close()
    finally:
        watchdog.cancel()
        b.close()


def test_w02_a_late_fire_never_touches_a_connection_after_cancel(loopback_only):
    """Shutdown can never hit another request's socket: once cancel() has run (always, in the
    caller's finally), a timer that was already firing finds nothing to shut down."""
    a, b = socket.socketpair()
    watchdog = adapter.DeadlineWatchdog(60.0)
    watchdog.trace("connection.connect_tcp.complete", {"return_value": _Stream(a)})
    watchdog.cancel()
    watchdog._fire()  # a fire that raced the end of the exchange
    try:
        a.sendall(b"still-open")
        b.settimeout(2.0)
        assert b.recv(16) == b"still-open"  # the connection was not shut down
    finally:
        a.close()
        b.close()



def _slow_dns(monkeypatch, host: str, seconds: float) -> None:
    """Make one hostname take ``seconds`` to resolve. No real DNS is queried: every other name
    goes to the real resolver, which the loopback-only guard still limits to 127.0.0.1."""
    real = socket.getaddrinfo

    def resolver(name, *args, **kwargs):
        if name == host:
            time.sleep(seconds)
            return real("127.0.0.1", *args, **kwargs)
        return real(name, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolver)


def _quiet_server() -> int:
    """A loopback listener that accepts and says nothing: the test must never get this far."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    threading.Thread(target=lambda: (listener.accept(), time.sleep(5)), daemon=True).start()
    return listener.getsockname()[1]


def test_dns_the_total_deadline_covers_name_resolution(loopback_only, monkeypatch):
    """Review finding DNS: resolution happens before any socket exists, so the watchdog has
    nothing to shut down and httpx's connect timeout does not cover getaddrinfo. A stalled
    resolver ran 3.35 s against a 0.5 s budget; both production feeds use hostnames."""
    _slow_dns(monkeypatch, "feed.invalid", 5.0)
    monkeypatch.setitem(globals(), "_LOOPBACK_HOSTS", _LOOPBACK_HOSTS | {"feed.invalid"})
    port = _quiet_server()
    provider = KmdCapProvider(f"http://feed.invalid:{port}/rss.xml", client=httpx.Client(timeout=5.0), timeout_s=0.6)
    started = time.monotonic()
    with pytest.raises(CapError) as err:
        provider.fetch_feed()
    elapsed = time.monotonic() - started
    assert elapsed < 2.5, f"{elapsed:.2f}s against a 0.6 s budget"
    assert err.value.kind == "timeout" and "name resolution" in str(err.value)


def test_dns_an_address_is_never_looked_up(loopback_only, monkeypatch):
    """A URL that already carries an address must not spawn a lookup at all."""
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: (_ for _ in ()).throw(AssertionError("looked up an address")))
    server = Server()
    poll  # noqa: B018 — the provider below is what matters; poll is imported at module level
    feed = KmdCapProvider("http://127.0.0.1:1/rss.xml", client=httpx.Client(transport=httpx.MockTransport(server.handler)))
    with pytest.raises(CapError):
        feed.fetch_feed()  # 404 from the mock server: it got past resolution, which is the point


def test_new1_a_304_to_an_unconditional_request_is_refused_not_read_as_unchanged():
    provider = KmdCapProvider(client=httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(304))))
    with pytest.raises(CapError) as err:
        provider.fetch_feed()  # no If-Modified-Since
    assert err.value.kind == "http" and "no If-Modified-Since" in str(err.value)


def test_a_declared_content_length_over_the_cap_is_not_read(monkeypatch):
    monkeypatch.setattr(adapter, "MAX_FEED_BYTES", 1000)
    server = Server()
    server.routes[FEED_URL] = (200, b"<rss>" + b" " * 5000 + b"</rss>", {})
    with pytest.raises(CapError) as err:
        server.provider().fetch_feed()
    assert err.value.kind == "oversize" and "declares" in str(err.value)


# ------------------------------------------------------------------------------ the poller: happy path


def test_poll_is_off_by_default_and_makes_no_request(tmp_db):
    settings, session = tmp_db
    server = Server()
    result = poll(session, settings, provider=server.provider(), now=T0)
    assert "skipped" in result.summary and "WEATHER_ENABLED" in result.summary
    assert result.tools[0]["skipped"] is True and server.calls == [] and _rows(session) == []


def test_poll_stores_each_alert_once_per_region_with_kmds_own_words(tmp_db, monkeypatch, clean_hub):
    settings, session = tmp_db
    _enable(monkeypatch)
    server = Server()
    result = poll(session, settings, provider=server.provider(), now=T0)

    tool = result.tools[0]
    assert tool["state"] == "ok" and tool["reachable"] is True and tool["alerts_stored"] == 2
    assert tool["storm_regions"] == ["WNY"] and tool["unmapped_areas"] == []
    assert server.urls() == [FEED_URL, RAIN_URL, WIND_URL]

    rows = _by_eid(session)
    # Heavy rain: one row for WNY, carrying all four counties, valid until KMD's own expires.
    rain = rows[f"{RAIN_ID}#WNY"]
    assert (rain.region_code, rain.county, rain.storm_flag, rain.flood_flag) == ("WNY", "Migori, Nyamira, Bungoma, Busia", 1, 0)
    assert rain.valid_from == datetime(2026, 5, 7, 3, 0) and rain.valid_until == RAIN_EXPIRES
    assert (rain.stale, rain.confidence, rain.fetched_at, rain.last_error) == (0, 1.0, T0, None)
    said = json.loads(rain.derived_json)
    assert (said["kind"], said["severity"], said["urgency"], said["certainty"], said["event"]) == (
        "cap_alert", "Severe", "Expected", "Likely", "Heavy Rainfall")
    assert said["valid_until_basis"] == "expires" and said["identifier"] == RAIN_ID
    assert json.loads(rain.payload_json)["xml"].startswith("<?xml")  # the document as received

    # Strong winds: Nairobi and Kiambu sit in several Safaricom regions → one row in each.
    for region, counties in (("NBI_E", "Nairobi, Kiambu"), ("NBI_W", "Nairobi, Kiambu"), ("MTK", "Kiambu")):
        wind = rows[f"{WIND_ID}#{region}"]
        assert (wind.region_code, wind.county, wind.storm_flag) == (region, counties, 0)  # Moderate: no storm
    # Turkana is a real county no Safaricom region covers: stored, unattributed, never dropped.
    turkana = rows[f"{WIND_ID}#county=Turkana"]
    assert turkana.region_code is None and turkana.county == "Turkana"

    events = [r["payload"] for r in clean_hub.recent(20) if r["type"] == "external_signal.updated"]
    assert {e["region_code"] for e in events} == SAFARICOM_REGIONS
    assert next(e for e in events if e["region_code"] == "WNY")["storm_flag"] is True


def test_every_region_gets_a_feed_health_row_that_is_never_a_warning(tmp_db, monkeypatch):
    settings, session = tmp_db
    _enable(monkeypatch)
    poll(session, settings, provider=Server().provider(), now=T0)
    rows = _by_eid(session)
    for region in SAFARICOM_REGIONS:
        health = rows[f"feed:{region}"]
        block = json.loads(health.derived_json)
        assert block["kind"] == "cap_feed_health" and block["state"] == "ok" and block["reachable"] is True
        # Already expired on write: services/dashboards._count_live can never count it.
        assert health.valid_until == health.fetched_at == T0 and health.stale == 1
        assert health.storm_flag == health.flood_flag == 0
    assert active_signals_count(session, "safaricom", source="KMD_CAP", region_code="WNY", now=T0) == 1
    assert active_signals_count(session, "safaricom", source="KMD_CAP", region_code="CST", now=T0) == 0


def _regions(session, now):
    return {r["region_code"]: r for r in dashboards.regions_dashboard(session, now=now)["regions"]}


def test_the_regions_dashboard_counts_warnings_not_health_rows(tmp_db, monkeypatch):
    settings, session = tmp_db
    _enable(monkeypatch)
    poll(session, settings, provider=Server().provider(), now=T0)
    regions = _regions(session, T0)
    assert regions["WNY"]["signals"]["cap"]["count"] == 1 and regions["WNY"]["signals"]["cap"]["available"] is True
    assert regions["CST"]["signals"]["cap"] == {"available": True, "stale": False, "count": 0, "fetched_at": "2026-05-07T12:00:00Z"}


def test_f03_cap_freshness_is_the_feeds_and_a_warning_arriving_never_makes_a_region_calmer(tmp_db, monkeypatch):
    """F03: the CAP block's freshness used to be whichever KMD_CAP row sorted newest, so a new
    alert made a region fresh-and-CALM, and it stayed so after polling stopped."""
    settings, session = tmp_db
    _enable(monkeypatch)
    server = Server()
    _serve(server, {"https://x.test/old.xml": _cap("old-1", severity="Moderate", areas=("Mombasa",))})
    poll(session, settings, provider=server.provider(), now=T0)
    before = _regions(session, T0)["WNY"]

    # Run 2 finds a new Moderate warning for WNY. Freshness is the feed's either way.
    _serve(server, {"https://x.test/old.xml": _cap("old-1", severity="Moderate", areas=("Mombasa",)),
                    "https://x.test/new.xml": _cap("new-1", severity="Moderate", areas=("Kisumu",))})
    later = T0 + timedelta(minutes=30)
    poll(session, settings, provider=server.provider(), now=later)
    wny = _regions(session, later)["WNY"]
    health = cap_feed_health(session, "safaricom", "WNY", later)
    assert wny["signals"]["cap"]["stale"] is health["stale"] is before["signals"]["cap"]["stale"] is False
    assert wny["signals"]["cap"]["count"] == 1
    assert wny["signals"]["cap"]["fetched_at"] == health["fetched_at"]

    # The poller stops (flag off / scheduler dead). Three days later nothing reads fresh,
    # although the warning (no <expires> here, so held) or any alert row still exists.
    monkeypatch.setenv("WEATHER_ENABLED", "false")
    blind = later + timedelta(days=3)
    wny = _regions(session, blind)["WNY"]
    assert wny["signals"]["cap"]["stale"] is True and wny["status"] == "STALE"


def test_f03_a_severe_warning_in_force_lifts_a_region_to_watch_never_alert(tmp_db, monkeypatch):
    """The product default at services/dashboards._status: Severe/Extreme in force → WATCH."""
    settings, session = tmp_db
    _enable(monkeypatch)
    server = Server()
    poll(session, settings, provider=server.provider(), now=T0)  # fixture: Severe heavy rain over WNY
    regions = _regions(session, T0)
    assert regions["WNY"]["status"] == "WATCH"
    assert regions["CST"]["status"] == "CALM"  # a current, fully-read feed with nothing for CST: we looked
    assert regions["NBI_E"]["status"] == "CALM"  # a Moderate advisory is shown (count 1) but lifts nothing
    assert regions["NBI_E"]["signals"]["cap"]["count"] == 1

    # Still WATCH while KMD is unreachable: the warning is in force on KMD's terms, not ours.
    server.routes[FEED_URL] = (503, b"down", {})
    poll(session, settings, provider=server.provider(), now=T0 + timedelta(hours=1))
    assert _regions(session, T0 + timedelta(hours=1))["WNY"]["status"] == "WATCH"
    # And no longer once KMD's own expires has passed.
    assert _regions(session, RAIN_EXPIRES + timedelta(minutes=1))["WNY"]["status"] != "WATCH"


def test_f02_a_stored_ok_is_not_believed_once_the_poller_stops(tmp_db, monkeypatch):
    """F02: cap_feed_health used to return the stored 'ok' forever once polling stopped."""
    settings, session = tmp_db
    _enable(monkeypatch)
    poll(session, settings, provider=Server().provider(), now=T0)
    assert CAP_FEED_MAX_SILENCE == timedelta(minutes=70) and CAP_POLL_INTERVAL_S == poller.INTERVAL_S
    assert cap_feed_health(session, "safaricom", "CST", T0 + timedelta(minutes=69))["state"] == "ok"
    health = cap_feed_health(session, "safaricom", "CST", T0 + timedelta(days=40))
    assert health["state"] == "not_polled_recently" and health["stale"] is True
    assert "has not run for" in health["reason"] and "'ok'" in health["reason"]
    assert health["feed_age_days"] == pytest.approx(40.4, abs=0.1)  # recomputed from newest_sent, not frozen


def test_f02_feed_age_is_recomputed_against_now_between_polls(tmp_db, monkeypatch):
    settings, session = tmp_db
    _enable(monkeypatch)
    server = Server()
    newest = T0 - timedelta(days=6, hours=23, minutes=30)  # current at T0, over 7 days 40 min later
    server.routes = {FEED_URL: (200, _rss(("https://x.test/a.xml", newest.strftime("%a, %d %b %Y %H:%M:%S GMT"))), {}),
                     "https://x.test/a.xml": (200, _cap("a-1", sent="2026-04-30T15:30:00+03:00"), {})}
    poll(session, settings, provider=server.provider(), now=T0)
    assert cap_feed_health(session, "safaricom", "WNY", T0)["state"] == "ok"
    later = cap_feed_health(session, "safaricom", "WNY", T0 + timedelta(minutes=40))
    assert later["state"] == "stale_feed" and later["feed_age_days"] > 7


def test_re_polling_is_idempotent_polite_and_never_moves_the_first_known_time(tmp_db, monkeypatch):
    settings, session = tmp_db
    _enable(monkeypatch)
    server = Server()
    poll(session, settings, provider=server.provider(), now=T0)
    before = {eid: (r.id, r.fetched_at) for eid, r in _by_eid(session).items() if not eid.startswith("feed:")}

    poll(session, settings, provider=server.provider(), now=T0 + timedelta(minutes=30))
    after = {eid: (r.id, r.fetched_at) for eid, r in _by_eid(session).items() if not eid.startswith("feed:")}
    assert after == before  # same rows, same first-known time (M6 lead time depends on it)
    # Second run: the feed again (with If-Modified-Since), but no CAP document twice.
    assert server.urls() == [FEED_URL, RAIN_URL, WIND_URL, FEED_URL]
    assert server.calls[-1].headers.get("If-Modified-Since") == LAST_MODIFIED


def test_a_304_keeps_everything_and_still_counts_as_reaching_the_feed(tmp_db, monkeypatch):
    settings, session = tmp_db
    _enable(monkeypatch)
    server = Server()
    poll(session, settings, provider=server.provider(), now=T0)
    server.routes[FEED_URL] = (304, b"", {"Last-Modified": LAST_MODIFIED})
    result = poll(session, settings, provider=server.provider(), now=T0 + timedelta(hours=1))
    assert result.tools[0]["not_modified"] is True and result.tools[0]["state"] == "ok"
    health = cap_feed_health(session, "safaricom", "WNY", T0 + timedelta(hours=1))
    assert health["state"] == "ok" and health["newest_sent"] == "2026-05-07T03:00:00Z" and health["alerts_in_force"] == 1


def test_a_document_that_fails_is_retried_next_run_and_does_not_hide_the_others(tmp_db, monkeypatch):
    settings, session = tmp_db
    _enable(monkeypatch)
    server = Server()
    server.routes[WIND_URL] = (500, b"boom", {})
    result = poll(session, settings, provider=server.provider(), now=T0)
    assert result.tools[0]["alerts_stored"] == 1 and "HTTP 500" in result.tools[0]["document_failures"][0]
    assert f"{RAIN_ID}#WNY" in _by_eid(session) and not any(k.startswith(WIND_ID) for k in _by_eid(session))

    server.routes[WIND_URL] = (200, _read("cap_nbi_strong_winds.xml"), {})
    poll(session, settings, provider=server.provider(), now=T0 + timedelta(minutes=30))
    assert f"{WIND_ID}#NBI_E" in _by_eid(session)
    assert server.urls().count(RAIN_URL) == 1 and server.urls().count(WIND_URL) == 2


# ------------------------------------------------------------------------------ silence is not good news


def test_the_spec_fixture_is_stale_on_2026_09_16_and_fresh_the_day_after_it_was_sent(tmp_db, monkeypatch):
    """§5.3.13 acceptance: "the 2026-05-07 fixture is stale on 2026-09-16"."""
    settings, session = tmp_db
    _enable(monkeypatch)
    poll(session, settings, provider=Server().provider(), now=T0)
    assert cap_feed_health(session, "safaricom", "WNY", T0)["state"] == "ok"

    result = poll(session, settings, provider=Server().provider(), now=SPEC_STALE_DAY)
    assert result.tools[0]["state"] == "stale_feed" and result.tools[0]["feed_stale"] is True
    health = cap_feed_health(session, "safaricom", "WNY", SPEC_STALE_DAY)
    assert health["state"] == "stale_feed" and health["stale"] is True
    assert health["feed_age_days"] == pytest.approx(132.4, abs=0.1)  # the spec's "132 days stale"
    assert "not evidence of calm" in health["reason"]
    assert health["alerts_in_force"] == 0  # May's alerts expired in May — and nothing stretched them


def test_cap_stale_days_is_honoured_and_a_bad_value_falls_back_to_seven(tmp_db, monkeypatch):
    settings, session = tmp_db
    _enable(monkeypatch)
    monkeypatch.setenv("CAP_STALE_DAYS", "200")
    poll(session, settings, provider=Server().provider(), now=SPEC_STALE_DAY)
    assert cap_feed_health(session, "safaricom", "WNY", SPEC_STALE_DAY)["state"] == "ok"
    for bad in ("seven", "0", "-3"):
        monkeypatch.setenv("CAP_STALE_DAYS", bad)
        assert cap_stale_days() == 7
    monkeypatch.delenv("CAP_STALE_DAYS")
    assert cap_stale_days() == 7


def test_three_days_unreachable_reads_unreachable_and_stale_never_no_warnings(tmp_db, monkeypatch):
    settings, session = tmp_db
    _enable(monkeypatch)
    poll(session, settings, provider=Server().provider(), now=T0)

    def timeout(request):
        raise httpx.ConnectTimeout("no route", request=request)

    dead = KmdCapProvider(client=httpx.Client(transport=httpx.MockTransport(timeout)))
    for day in (1, 2, 3):
        result = poll(session, settings, provider=dead, now=T0 + timedelta(days=day))
        assert result.tools[0]["state"] == "unreachable"

    later = T0 + timedelta(days=3)
    health = cap_feed_health(session, "safaricom", "CST", later)
    assert health["state"] == "unreachable" and health["stale"] is True and health["available"] is True
    assert health["alerts_in_force"] == 0 and "timeout" in health["last_error"]
    # fetched_at is the last time the feed was REACHED, so its age is how long we have been blind.
    assert health["fetched_at"] == "2026-05-07T12:00:00Z"
    block = json.loads(latest_row(session, "safaricom", source="KMD_CAP", region_code="CST").derived_json)
    assert block["last_success_at"] == "2026-05-07T12:00:00Z" and block["attempted_at"] == "2026-05-10T12:00:00Z"
    # And the dashboard agrees: no fresh CAP signal anywhere.
    for region in dashboards.regions_dashboard(session, now=later)["regions"]:
        assert region["signals"]["cap"]["stale"] is True


def test_a_live_warning_survives_an_outage_but_is_marked_stale_until_the_feed_answers(tmp_db, monkeypatch):
    settings, session = tmp_db
    _enable(monkeypatch)
    server = Server()
    poll(session, settings, provider=server.provider(), now=T0)

    server.routes[FEED_URL] = (503, b"maintenance", {})
    poll(session, settings, provider=server.provider(), now=T0 + timedelta(hours=1))
    rain = _by_eid(session)[f"{RAIN_ID}#WNY"]
    # KMD's warning still stands (its expires is KMD's, not ours) ...
    assert rain.valid_until == RAIN_EXPIRES and json.loads(rain.derived_json)["severity"] == "Severe"
    assert active_signals_count(session, "safaricom", source="KMD_CAP", region_code="WNY", now=T0 + timedelta(hours=1)) == 1
    # ... but we can no longer vouch for it: a cancellation would be invisible.
    assert rain.stale == 1 and "HTTP 503" in rain.last_error and "cancellation" in rain.last_error
    listed = list_signals(session, "safaricom", source="KMD_CAP", region_code="WNY", active=True, now=T0 + timedelta(hours=1))
    assert [r.external_id for r in listed] == [f"{RAIN_ID}#WNY"]  # still listed as in force, flagged stale

    server.routes[FEED_URL] = (200, _read("kmd_rss.xml"), {"Last-Modified": LAST_MODIFIED})
    poll(session, settings, provider=server.provider(), now=T0 + timedelta(hours=2))
    session.refresh(rain)
    assert rain.stale == 0 and rain.last_error is None


@pytest.mark.parametrize(
    "kind, respond",
    [
        ("timeout", lambda req: (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=req))),
        ("http", lambda req: httpx.Response(500, content=b"internal error")),
        ("malformed", lambda req: httpx.Response(200, content=b"<rss><channel><item>")),
        ("malformed", lambda req: httpx.Response(200, content=b"<html><body>Bad gateway</body></html>")),
        ("malformed", lambda req: httpx.Response(200, content=BILLION_LAUGHS)),
        ("tls", lambda req: (_ for _ in ()).throw(httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] self-signed", request=req))),
        ("network", lambda req: (_ for _ in ()).throw(httpx.ConnectError("connection refused", request=req))),
    ],
    ids=["timeout", "500", "truncated-xml", "html-page", "entity-bomb", "tls", "refused"],
)
def test_feed_failures_are_fail_soft_and_keep_the_last_good_rows(tmp_db, monkeypatch, kind, respond):
    settings, session = tmp_db
    _enable(monkeypatch)
    server = Server()
    poll(session, settings, provider=server.provider(), now=T0)
    good_payload = _by_eid(session)[f"{RAIN_ID}#WNY"].payload_json
    health_payload = _by_eid(session)["feed:WNY"].payload_json

    server.routes[FEED_URL] = respond
    result = poll(session, settings, provider=server.provider(), now=T0 + timedelta(minutes=30))  # must not raise
    tool = result.tools[0]
    assert tool["state"] == "unreachable" and tool["error_kind"] == kind and tool["reachable"] is False
    rows = _by_eid(session)
    assert rows[f"{RAIN_ID}#WNY"].payload_json == good_payload  # the last good row keeps its payload
    assert rows["feed:WNY"].payload_json == health_payload
    assert rows["feed:WNY"].last_error and rows["feed:WNY"].last_error.startswith(kind)
    assert json.loads(rows["feed:WNY"].derived_json)["state"] == "unreachable"


def test_an_oversized_feed_is_fail_soft(tmp_db, monkeypatch):
    settings, session = tmp_db
    _enable(monkeypatch)
    monkeypatch.setattr(adapter, "MAX_FEED_BYTES", 1000)
    server = Server()
    server.routes[FEED_URL] = lambda req: httpx.Response(200, content=iter([b" " * 800] * 10))
    result = poll(session, settings, provider=server.provider(), now=T0)
    assert result.tools[0]["error_kind"] == "oversize"
    assert cap_feed_health(session, "safaricom", "WNY", T0)["state"] == "unreachable"


def test_an_unexpected_bug_is_still_fail_soft(tmp_db, monkeypatch):
    settings, session = tmp_db
    _enable(monkeypatch)
    monkeypatch.setattr(poller, "_process_feed", lambda *a, **k: 1 / 0)
    result = poll(session, settings, provider=Server().provider(), now=T0)
    assert result.tools[0]["error_kind"] == "unexpected" and "ZeroDivisionError" in result.tools[0]["error"]
    assert cap_feed_health(session, "safaricom", "WNY", T0)["state"] == "unreachable"


# ------------------------------------------------------------------------------ KMD's statement, not ours


def _serve(server: Server, docs: dict[str, bytes], pub: str = "Thu, 07 May 2026 06:00:00 GMT") -> None:
    server.routes = {FEED_URL: (200, _rss(*[(u, pub) for u in docs]), {})}
    for url, body in docs.items():
        server.routes[url] = (200, body, {})


def test_a_cancel_ends_the_alert_it_references_and_a_re_store_cannot_resurrect_it(tmp_db, monkeypatch):
    settings, session = tmp_db
    _enable(monkeypatch)
    server = Server()
    orig = "https://x.test/orig.xml"
    _serve(server, {orig: _cap("orig-1", areas=("Kisumu",))})
    poll(session, settings, provider=server.provider(), now=T0)
    row = _by_eid(session)["orig-1#WNY"]
    assert row.valid_until == datetime(2026, 5, 10, 6, 0)

    cancel = "https://x.test/cancel.xml"
    _serve(server, {orig: _cap("orig-1"), cancel: _cap(
        "cancel-1", msg_type="Cancel", sent="2026-05-07T18:00:00+03:00",
        references="forecasting@meteo.go.ke,orig-1,2026-05-07T09:00:00+03:00")})
    result = poll(session, settings, provider=server.provider(), now=T0 + timedelta(hours=4))
    session.refresh(row)
    assert result.tools[0]["alerts_ended"] == 1
    assert row.valid_until == datetime(2026, 5, 7, 15, 0)  # the Cancel's own sent time, in UTC
    assert json.loads(row.derived_json)["ended_by"] == {"identifier": "cancel-1", "msgType": "Cancel", "sent": "2026-05-07T15:00:00Z"}
    assert not any(k.startswith("cancel-1") for k in _by_eid(session))  # a Cancel is not itself a warning

    # Re-storing the original document (say its link was forgotten) must not undo the cancel.
    poller.store_alert(session, operator_id="safaricom", alert=parse_cap_alert(_cap("orig-1")),
                       mapping=poller.county_region_map(settings.operator), now=T0 + timedelta(hours=5))
    session.commit()
    session.refresh(row)
    assert row.valid_until == datetime(2026, 5, 7, 15, 0) and "ended_by" in json.loads(row.derived_json)


def test_an_update_ends_its_predecessor_and_stands_in_its_place(tmp_db, monkeypatch):
    settings, session = tmp_db
    _enable(monkeypatch)
    server = Server()
    _serve(server, {"https://x.test/a.xml": _cap("a-1")})
    poll(session, settings, provider=server.provider(), now=T0)
    _serve(server, {"https://x.test/a.xml": _cap("a-1"), "https://x.test/b.xml": _cap(
        "a-2", msg_type="Update", sent="2026-05-07T20:00:00+03:00", severity="Extreme",
        expires="2026-05-11T09:00:00+03:00", references="k,a-1,2026-05-07T09:00:00+03:00")})
    poll(session, settings, provider=server.provider(), now=T0 + timedelta(hours=6))
    rows = _by_eid(session)
    assert rows["a-1#WNY"].valid_until == datetime(2026, 5, 7, 17, 0)
    assert rows["a-2#WNY"].valid_until == datetime(2026, 5, 11, 6, 0) and rows["a-2#WNY"].storm_flag == 1


@pytest.mark.parametrize("status", ["Test", "Exercise", "System", "Draft"])
def test_a_non_actual_status_is_never_stored_as_a_warning(tmp_db, monkeypatch, status):
    settings, session = tmp_db
    _enable(monkeypatch)
    server = Server()
    _serve(server, {"https://x.test/t.xml": _cap("t-1", status=status)})
    result = poll(session, settings, provider=server.provider(), now=T0)
    assert result.tools[0]["skipped_not_actual"] == 1
    assert not any(k.startswith("t-1") for k in _by_eid(session))


@pytest.mark.parametrize("severity, storm", [("Extreme", 1), ("Severe", 1), ("severe", 1), ("Moderate", 0), ("Minor", 0), ("Unknown", 0)])
def test_the_storm_rule_is_the_spec_rule_and_nothing_more(tmp_db, monkeypatch, severity, storm):
    settings, session = tmp_db
    _enable(monkeypatch)
    server = Server()
    _serve(server, {"https://x.test/s.xml": _cap("s-1", severity=severity)})
    poll(session, settings, provider=server.provider(), now=T0)
    row = _by_eid(session)["s-1#WNY"]
    assert row.storm_flag == storm and row.confidence == 1.0  # certainty is carried verbatim, not turned into a number
    assert json.loads(row.derived_json)["severity"] == severity


def test_an_alert_without_expires_is_held_while_listed_and_never_past_cap_stale_days(tmp_db, monkeypatch):
    settings, session = tmp_db
    _enable(monkeypatch)
    server = Server()
    link = "https://x.test/held.xml"
    _serve(server, {link: _cap("held-1", expires=None, sent="2026-05-07T09:00:00+03:00")})
    poll(session, settings, provider=server.provider(), now=T0)
    row = _by_eid(session)["held-1#WNY"]
    assert row.valid_until == T0 + ALERT_HOLD_WITHOUT_EXPIRES
    assert json.loads(row.derived_json)["valid_until_basis"].startswith("held:")

    # Still listed an hour later: held for another window.
    poll(session, settings, provider=server.provider(), now=T0 + timedelta(hours=1))
    session.refresh(row)
    assert row.valid_until == T0 + timedelta(hours=1) + ALERT_HOLD_WITHOUT_EXPIRES

    # Still listed ten days later in a feed nobody maintains: never beyond sent + 7 days.
    poll(session, settings, provider=server.provider(), now=T0 + timedelta(days=10))
    session.refresh(row)
    assert row.valid_until == datetime(2026, 5, 14, 6, 0)  # sent 06:00 UTC + CAP_STALE_DAYS (7)
    assert active_signals_count(session, "safaricom", source="KMD_CAP", region_code="WNY", now=T0 + timedelta(days=10)) == 0


def test_f07_a_cancel_listed_before_its_original_in_the_same_feed_still_wins(tmp_db, monkeypatch):
    """F07: RSS lists newest first, so the Cancel was processed first, found nothing to end,
    and the withdrawn warning was then stored as in force."""
    settings, session = tmp_db
    _enable(monkeypatch)
    server = Server()
    cancel = _cap("cancel-1", msg_type="Cancel", sent="2026-05-07T18:00:00+03:00",
                  references="forecasting@meteo.go.ke,orig-1,2026-05-07T09:00:00+03:00")
    _serve(server, {"https://x.test/cancel.xml": cancel, "https://x.test/orig.xml": _cap("orig-1")})  # cancel first
    poll(session, settings, provider=server.provider(), now=T0 + timedelta(hours=4))
    row = _by_eid(session)["orig-1#WNY"]
    assert row.valid_until == datetime(2026, 5, 7, 15, 0)
    assert json.loads(row.derived_json)["ended_by"]["identifier"] == "cancel-1"
    assert cap_alerts(session, "safaricom", "WNY", now=T0 + timedelta(hours=5)) == []


def test_f07_a_cancel_whose_original_arrives_on_a_later_run_leaves_a_tombstone_that_wins(tmp_db, monkeypatch):
    settings, session = tmp_db
    _enable(monkeypatch)
    server = Server()
    cancel = _cap("cancel-2", msg_type="Cancel", sent="2026-05-07T18:00:00+03:00",
                  references="forecasting@meteo.go.ke,orig-2,2026-05-07T09:00:00+03:00")
    _serve(server, {"https://x.test/cancel.xml": cancel})
    result = poll(session, settings, provider=server.provider(), now=T0 + timedelta(hours=4))
    assert result.tools[0]["tombstones"] == 1
    tomb = _by_eid(session)["tombstone:orig-2"]
    assert tomb.region_code is None and tomb.valid_until == datetime(2026, 5, 7, 15, 0)
    assert json.loads(tomb.derived_json)["kind"] == "cap_tombstone"

    _serve(server, {"https://x.test/cancel.xml": cancel, "https://x.test/orig.xml": _cap("orig-2")})
    poll(session, settings, provider=server.provider(), now=T0 + timedelta(hours=5))
    row = _by_eid(session)["orig-2#WNY"]
    assert row.valid_until == datetime(2026, 5, 7, 15, 0) and json.loads(row.derived_json)["ended_by"]["msgType"] == "Cancel"
    assert active_signals_count(session, "safaricom", source="KMD_CAP", region_code="WNY", now=T0 + timedelta(hours=5)) == 0


def test_f08_a_document_that_cannot_be_read_makes_the_feed_incomplete_not_ok(tmp_db, monkeypatch):
    """F08: the feed answered, the one warning 503'd, and the lane said 'ok, 0 in force'."""
    settings, session = tmp_db
    _enable(monkeypatch)
    server = Server()
    server.routes[RAIN_URL] = (503, b"document host down", {})
    poll(session, settings, provider=server.provider(), now=T0)
    health = cap_feed_health(session, "safaricom", "WNY", T0)
    assert health["state"] == "incomplete" and health["stale"] is True and health["alerts_in_force"] == 0
    assert "HTTP 503" in health["last_error"] and "an alert may be missing" in health["reason"]
    assert _regions(session, T0)["WNY"]["signals"]["cap"]["stale"] is True

    # The failed document is retried, and the conditional header is withheld so a 304 cannot hide it.
    server.routes[RAIN_URL] = (200, _read("cap_wny_heavy_rain.xml"), {})
    poll(session, settings, provider=server.provider(), now=T0 + timedelta(minutes=30))
    feed_requests = [r for r in server.calls if str(r.url) == FEED_URL]
    assert "If-Modified-Since" not in feed_requests[-1].headers
    assert cap_feed_health(session, "safaricom", "WNY", T0 + timedelta(minutes=30))["state"] == "ok"


def test_f08_deferred_documents_are_incomplete_too(tmp_db, monkeypatch):
    settings, session = tmp_db
    _enable(monkeypatch)
    monkeypatch.setattr(poller, "MAX_DOCS_PER_RUN", 1)
    result = poll(session, settings, provider=Server().provider(), now=T0)
    assert result.tools[0]["documents_deferred"] == 1 and result.tools[0]["state"] == "incomplete"
    assert "deferred" in cap_feed_health(session, "safaricom", "WNY", T0)["reason"]


def test_f10_a_profile_fix_re_attributes_live_alerts_without_a_refetch(tmp_db, monkeypatch):
    """F10: a warning stored while a region listed no counties stayed unattributed until KMD
    issued a new identifier."""
    settings, session = tmp_db
    _enable(monkeypatch)
    server = Server()
    _serve(server, {"https://x.test/k.xml": _cap("kw-1", areas=("Kwale",))})
    gappy = _with_regions(settings, WNY=["Kisumu"], CST=[])
    poll(session, gappy, provider=server.provider(), now=T0)
    assert _by_eid(session)["kw-1#county=Kwale"].region_code is None
    documents_before = server.urls().count("https://x.test/k.xml")

    fixed = _with_regions(settings, WNY=["Kisumu"], CST=["Mombasa", "Kwale"])
    result = poll(session, fixed, provider=server.provider(), now=T0 + timedelta(hours=1))
    assert server.urls().count("https://x.test/k.xml") == documents_before  # no re-fetch
    assert result.tools[0]["reattributed"] == 2  # a CST row created, the unattributed row ended
    rows = _by_eid(session)
    assert rows["kw-1#CST"].region_code == "CST" and rows["kw-1#CST"].fetched_at == T0 + timedelta(hours=1)
    assert rows["kw-1#county=Kwale"].valid_until == T0 + timedelta(hours=1)
    health = cap_feed_health(session, "safaricom", "CST", T0 + timedelta(hours=1))
    assert health["state"] == "ok" and health["alerts_in_force"] == 1

    # And back: a county moved out of the region has its row ended, not left attributed ...
    poll(session, gappy, provider=server.provider(), now=T0 + timedelta(hours=2))
    rows = _by_eid(session)
    assert rows["kw-1#CST"].valid_until == T0 + timedelta(hours=2) and "detached" in json.loads(rows["kw-1#CST"].derived_json)
    # ... and a later fix attributes it again — as a NEW span from now, never by reviving the
    # detached row (NEW4: reviving it credited CST with the hours it was unmapped).
    poll(session, fixed, provider=server.provider(), now=T0 + timedelta(hours=3))
    rows = _by_eid(session)
    assert rows["kw-1#CST"].valid_until == T0 + timedelta(hours=2)
    span = rows["kw-1#CST@20260507T150000Z"]
    assert span.fetched_at == T0 + timedelta(hours=3) and span.valid_until == datetime(2026, 5, 10, 6, 0)


def test_f04_a_gazetted_alias_in_the_feed_reaches_every_region_that_lists_the_county(tmp_db, monkeypatch):
    """F04: "Nairobi City County" was recognised as Nairobi, then attributed to no region."""
    settings, session = tmp_db
    _enable(monkeypatch)
    server = Server()
    _serve(server, {"https://x.test/n.xml": _cap("n-1", areas=("Nairobi City County",))})
    result = poll(session, settings, provider=server.provider(), now=T0)
    rows = _by_eid(session)
    assert rows["n-1#NBI_E"].county == rows["n-1#NBI_W"].county == "Nairobi"
    assert "n-1#county=Nairobi" not in rows and result.tools[0]["unmapped_areas"] == []


def test_f04_a_profile_spelling_alias_matches_kmds_spelling(tmp_db, monkeypatch):
    settings, session = tmp_db
    _enable(monkeypatch)
    server = Server()
    _serve(server, {"https://x.test/e.xml": _cap("e-1", areas=("Elgeyo-Marakwet",))})
    profile = _with_regions(settings, RFT=["Elgeyo/Marakwet", "Nakuru"], WNY=["Kisumu"])
    poll(session, profile, provider=server.provider(), now=T0)
    assert _by_eid(session)["e-1#RFT"].county == "Elgeyo-Marakwet"


@pytest.mark.parametrize("area_desc", ["Mombasa County.", "Mombasa\nCounty", "Coast (Mombasa, Kilifi)"])
def test_f17_punctuation_and_parentheses_do_not_lose_a_county(tmp_db, monkeypatch, area_desc):
    settings, session = tmp_db
    _enable(monkeypatch)
    server = Server()
    _serve(server, {"https://x.test/m.xml": _cap("m-1", areas=(area_desc,))})
    poll(session, settings, provider=server.provider(), now=T0)
    assert "Mombasa" in _by_eid(session)["m-1#CST"].county


def test_new1_a_nonconformant_304_to_the_retry_keeps_the_pending_document_and_is_not_ok(tmp_db, monkeypatch):
    """NEW1: the poller withheld If-Modified-Since to force a retry of an unread document; the
    server answered 304 anyway, the pending list was erased, and the feed read "ok, 0 in force"
    with the one Severe warning never read."""
    settings, session = tmp_db
    _enable(monkeypatch)
    server = Server()
    server.routes[RAIN_URL] = (503, b"document host down", {})
    poll(session, settings, provider=server.provider(), now=T0)
    assert cap_feed_health(session, "safaricom", "WNY", T0)["state"] == "incomplete"

    server.routes[RAIN_URL] = (200, _read("cap_wny_heavy_rain.xml"), {})
    server.routes[FEED_URL] = (304, b"", {"Last-Modified": LAST_MODIFIED})
    t1 = T0 + timedelta(minutes=30)
    poll(session, settings, provider=server.provider(), now=t1)
    health = cap_feed_health(session, "safaricom", "WNY", t1)
    assert health["state"] != "ok" and health["stale"] is True and "304" in health["reason"]
    assert poller._previous_health(session, "safaricom")["pending_links"] == [RAIN_URL]

    server.routes[FEED_URL] = (200, _read("kmd_rss.xml"), {"Last-Modified": LAST_MODIFIED})
    t2 = t1 + timedelta(minutes=30)
    poll(session, settings, provider=server.provider(), now=t2)
    health = cap_feed_health(session, "safaricom", "WNY", t2)
    assert health["state"] == "ok" and health["alerts_in_force"] == 1
    assert server.urls().count(RAIN_URL) == 2


def test_new1_an_outage_between_runs_does_not_erase_a_pending_document(tmp_db, monkeypatch):
    """The conformant-server variant: an unreachable run wiped the list, the next conditional
    request got a legitimate 304, and the unread warning was never read."""
    settings, session = tmp_db
    _enable(monkeypatch)
    server = Server()

    def conformant(request):
        if request.headers.get("If-Modified-Since"):
            return httpx.Response(304, headers={"Last-Modified": LAST_MODIFIED})
        return httpx.Response(200, content=_read("kmd_rss.xml"), headers={"Last-Modified": LAST_MODIFIED})

    server.routes[FEED_URL] = conformant
    server.routes[RAIN_URL] = (503, b"document host down", {})
    poll(session, settings, provider=server.provider(), now=T0)
    server.routes[FEED_URL] = (503, b"feed down", {})
    poll(session, settings, provider=server.provider(), now=T0 + timedelta(minutes=30))
    assert poller._previous_health(session, "safaricom")["pending_links"] == [RAIN_URL]

    server.routes[FEED_URL] = conformant
    server.routes[RAIN_URL] = (200, _read("cap_wny_heavy_rain.xml"), {})
    t2 = T0 + timedelta(hours=1)
    poll(session, settings, provider=server.provider(), now=t2)
    feed_requests = [r for r in server.calls if str(r.url) == FEED_URL]
    assert "If-Modified-Since" not in feed_requests[-1].headers  # withheld: something was still owed
    health = cap_feed_health(session, "safaricom", "WNY", t2)
    assert health["state"] == "ok" and health["alerts_in_force"] == 1


def test_new4_re_attaching_a_county_opens_a_new_span_and_the_gap_is_never_credited(tmp_db, monkeypatch):
    """NEW4: when a county moved back to a region, the detached row was revived — one continuous
    span from the first fetch to KMD's expiry — so the backtest credited the region with a
    warning through the hours it was not mapped. The replayers' case, exactly."""
    from noc_agents.db.models import IncidentRow
    from noc_agents.services import backtest

    settings, session = tmp_db
    _enable(monkeypatch)
    server = Server()
    _serve(server, {"https://x.test/a1.xml": _cap(
        "a1", sent="2026-05-07T14:55:00+03:00", expires="2026-05-07T21:00:00+03:00", areas=("Kwale",))})
    mapped = _with_regions(settings, CST=["Mombasa", "Kwale"], WNY=["Kisumu"])
    moved = _with_regions(settings, CST=["Mombasa"], WNY=["Kisumu", "Kwale"])
    for minutes, profile in ((0, mapped), (30, moved), (60, moved), (150, mapped), (180, mapped)):
        poll(session, profile, provider=server.provider(), now=T0 + timedelta(minutes=minutes))

    rows = _by_eid(session)
    assert rows["a1#CST"].valid_until == T0 + timedelta(minutes=30)  # the first span stays ended
    second = rows["a1#CST@20260507T143000Z"]
    assert second.fetched_at == T0 + timedelta(minutes=150) and second.valid_until == datetime(2026, 5, 7, 18, 0)
    assert "detached" not in json.loads(second.derived_json)

    session.add(IncidentRow(id="inc-gap", operator_id="safaricom", incident_number="SAF9990001", site_id="S1",
                            region_code="CST", correlation_fingerprint="fp-gap", failure_time=T0 + timedelta(minutes=90),
                            created_at=T0 + timedelta(minutes=90), updated_at=T0 + timedelta(minutes=90)))
    session.commit()
    end = T0 + timedelta(hours=12)
    episodes = backtest.build_episodes(
        backtest._family_rows(session, "safaricom", family="cap", since=T0 - timedelta(days=1), until=end, now=end),
        family="cap")
    cst = sorted((e.start, e.end) for e in episodes if e.region_code == "CST")
    assert cst == [(T0, T0 + timedelta(minutes=30)), (T0 + timedelta(minutes=150), datetime(2026, 5, 7, 18, 0))]
    score = backtest.score_region(session, "safaricom", "CST", family="cap", since=T0 - timedelta(hours=1), until=end, now=end)
    assert (score.incidents, score.incidents_warned) == (1, 0)  # 13:30 fell in the unmapped gap


def test_pir_span_a_re_attached_span_starts_when_it_is_attributed(tmp_db, monkeypatch):
    """Review finding PIR-SPAN: a span copied the alert's effective time into valid_from, so
    anything selecting on valid_from <= t <= valid_until placed it hours before it existed."""
    settings, session = tmp_db
    _enable(monkeypatch)
    server = Server()
    _serve(server, {"https://x.test/s.xml": _cap(
        "span-1", sent="2026-05-07T11:55:00+03:00", expires="2026-05-07T21:00:00+03:00", areas=("Kwale",))})
    mapped = _with_regions(settings, CST=["Mombasa", "Kwale"], WNY=["Kisumu"])
    moved = _with_regions(settings, CST=["Mombasa"], WNY=["Kisumu", "Kwale"])
    poll(session, mapped, provider=server.provider(), now=T0)
    poll(session, moved, provider=server.provider(), now=T0 + timedelta(minutes=30))
    poll(session, mapped, provider=server.provider(), now=T0 + timedelta(minutes=150))

    rows = _by_eid(session)
    first, span = rows["span-1#CST"], rows["span-1#CST@20260507T143000Z"]
    assert first.valid_from == datetime(2026, 5, 7, 8, 55)  # the alert's own effective time
    assert span.valid_from == span.fetched_at == T0 + timedelta(minutes=150)
    # The WNY span, opened for a region the map reached later, starts when it was attributed too.
    assert rows["span-1#WNY"].valid_from == T0 + timedelta(minutes=30)


def test_cancel_after_a_re_attach_never_leaves_an_inverted_span(tmp_db, monkeypatch):
    """Review finding CANCEL-INVERSION: a Cancel sent at 13:00 but fetched after a 13:30
    re-attach set the span's valid_until to 13:00, leaving (13:30 .. 13:00) — a window that can
    cover no incident yet counted as a resolved, never-hit episode."""
    from noc_agents.services import backtest

    settings, session = tmp_db
    _enable(monkeypatch)
    server = Server()
    original = _cap("inv-1", sent="2026-05-07T14:55:00+03:00", expires="2026-05-07T21:00:00+03:00", areas=("Kwale",))
    mapped = _with_regions(settings, CST=["Mombasa", "Kwale"], WNY=["Kisumu"])
    moved = _with_regions(settings, CST=["Mombasa"], WNY=["Kisumu", "Kwale"])
    _serve(server, {"https://x.test/o.xml": original})
    poll(session, mapped, provider=server.provider(), now=T0)
    poll(session, moved, provider=server.provider(), now=T0 + timedelta(minutes=30))
    poll(session, mapped, provider=server.provider(), now=T0 + timedelta(minutes=90))  # re-attach at 13:30

    cancel = _cap("inv-cancel", msg_type="Cancel", sent="2026-05-07T16:00:00+03:00",
                  references="kmd,inv-1,2026-05-07T14:55:00+03:00")
    _serve(server, {"https://x.test/o.xml": original, "https://x.test/c.xml": cancel})
    poll(session, mapped, provider=server.provider(), now=T0 + timedelta(minutes=120))

    span = _by_eid(session)["inv-1#CST@20260507T133000Z"]
    assert span.fetched_at == T0 + timedelta(minutes=90)
    assert span.valid_until == span.fetched_at  # ended at its own start, never before it
    end = T0 + timedelta(hours=12)
    episodes = backtest.build_episodes(
        backtest._family_rows(session, "safaricom", family="cap", since=T0 - timedelta(days=1), until=end, now=end),
        family="cap")
    assert [(e.region_code, e.start, e.end) for e in episodes if e.region_code == "CST"] == [
        ("CST", T0, T0 + timedelta(minutes=30))]  # the zero-length span is no episode


def test_new2_a_detached_held_row_is_never_extended_or_re_detached(tmp_db, monkeypatch):
    """NEW2: _refresh_hold extended a DETACHED row of an alert with no <expires>, so it crept
    forward every poll, the backtest kept crediting the old region, and every poll reported a
    re-attribution."""
    settings, session = tmp_db
    _enable(monkeypatch)
    server = Server()
    _serve(server, {"https://x.test/held.xml": _cap("held-9", expires=None, areas=("Kwale",))})
    mapped = _with_regions(settings, CST=["Mombasa", "Kwale"], WNY=["Kisumu"])
    moved = _with_regions(settings, CST=["Mombasa"], WNY=["Kisumu", "Kwale"])
    poll(session, mapped, provider=server.provider(), now=T0)

    detach_at = T0 + timedelta(minutes=30)
    result = poll(session, moved, provider=server.provider(), now=detach_at)
    assert result.tools[0]["reattributed"] == 2  # WNY row created, CST row detached
    for k in (2, 3, 4):
        result = poll(session, moved, provider=server.provider(), now=T0 + timedelta(minutes=30 * k))
        assert result.tools[0]["reattributed"] == 0, k
        cst = _by_eid(session)["held-9#CST"]
        assert cst.valid_until == detach_at and "detached" in json.loads(cst.derived_json), k
    wny = _by_eid(session)["held-9#WNY"]
    assert wny.valid_until > T0 + timedelta(minutes=120)  # the held alert lives on where it now belongs

    from noc_agents.services import backtest

    end = T0 + timedelta(hours=6)
    rows = backtest._family_rows(session, "safaricom", family="cap", since=T0 - timedelta(days=1), until=end, now=end)
    cst_episodes = [e for e in backtest.build_episodes(rows, family="cap") if e.region_code == "CST"]
    assert [(e.start, e.end) for e in cst_episodes] == [(T0, detach_at)]


@pytest.mark.parametrize("area_desc", ["Coast (Mombasa,Kilifi).", "Kwale (Coast region).", "Nakuru (and Baringo)!"])
def test_new3_punctuation_left_between_separators_is_not_a_place(area_desc):
    pieces = split_area_desc(area_desc)
    assert pieces and all(any(ch.isalpha() for ch in p) for p in pieces), pieces


def test_new3_no_junk_row_for_a_trailing_full_stop(tmp_db, monkeypatch):
    """NEW3: "Coast (Mombasa,Kilifi)." stored a row a2#county=. and reported "." unattributed."""
    settings, session = tmp_db
    _enable(monkeypatch)
    server = Server()
    _serve(server, {"https://x.test/d.xml": _cap("dot-1", areas=("Coast (Mombasa,Kilifi).",))})
    result = poll(session, settings, provider=server.provider(), now=T0)
    rows = _by_eid(session)
    assert sorted(k for k in rows if k.startswith("dot-1#")) == ["dot-1#CST", "dot-1#county=Coast"]
    assert result.tools[0]["unmapped_areas"] == ["Coast"]
    # Re-attribution from areas stored before the fix cannot recreate the junk row either.
    targets, unknown = poller._targets(["Coast", ".", "Mombasa"], poller.county_region_map(settings.operator))
    assert "county=." not in targets and "." not in unknown


# ------------------------------------------------------------------------------ county → region


def test_an_unknown_county_on_the_profile_rejects_the_lane_loudly_and_never_the_app(tmp_db, monkeypatch):
    settings, session = tmp_db
    _enable(monkeypatch)
    broken = _with_regions(settings, WNY=["Kisumu", "Kisumuu"], CST=["Mombasa"])
    server = Server()
    result = poll(session, broken, provider=server.provider(), now=T0)  # must not raise
    assert server.calls == []  # refused before any request
    tool = result.tools[0]
    assert tool["state"] == "misconfigured" and tool["error_kind"] == "config"
    assert "Kisumuu" in tool["error"] and "47 counties" in tool["error"]
    for region in ("WNY", "CST"):
        health = cap_feed_health(session, "safaricom", region, T0)
        assert health["state"] == "misconfigured" and health["stale"] is True and "Kisumuu" in health["last_error"]
    assert not [r for r in _rows(session) if not r.external_id.startswith("feed:")]  # nothing attributed


def test_a_region_with_no_counties_reads_unmapped_not_ok(tmp_db, monkeypatch):
    settings, session = tmp_db
    _enable(monkeypatch)
    gappy = _with_regions(settings, WNY=["Migori", "Nyamira", "Bungoma", "Busia"], EST=[])
    result = poll(session, gappy, provider=Server().provider(), now=T0)
    assert result.tools[0]["state"] == "ok"  # a gap is not fatal ...
    health = cap_feed_health(session, "safaricom", "EST", T0)
    assert health["state"] == "unmapped" and health["stale"] is True and "no counties" in health["reason"]  # ... but it is said


def test_a_county_spelt_differently_by_kmd_still_maps(tmp_db, monkeypatch):
    settings, session = tmp_db
    _enable(monkeypatch)
    server = Server()
    _serve(server, {"https://x.test/h.xml": _cap("h-1", areas=("HOMA BAY County", "Murang\u2019a"))})
    poll(session, settings, provider=server.provider(), now=T0)
    rows = _by_eid(session)
    assert rows["h-1#WNY"].county == "Homa Bay" and rows["h-1#MTK"].county == "Murang'a"


def test_an_area_that_is_not_a_county_is_kept_in_kmds_words_and_reported(tmp_db, monkeypatch):
    settings, session = tmp_db
    _enable(monkeypatch)
    server = Server()
    _serve(server, {"https://x.test/r.xml": _cap("r-1", areas=("Highlands West of the Rift Valley",))})
    result = poll(session, settings, provider=server.provider(), now=T0)
    assert result.tools[0]["unmapped_areas"] == ["Highlands West of the Rift Valley"]
    row = _by_eid(session)["r-1#county=Highlands West of the Rift Valley"]
    assert row.region_code is None and row.county == "Highlands West of the Rift Valley"


# ------------------------------------------------------------------------------ operator isolation


def test_both_operators_poll_the_same_feed_and_never_see_each_others_rows(tmp_db, monkeypatch):
    settings, session = tmp_db
    _enable(monkeypatch)
    airtel = get_settings("airtel").model_copy(update={"database_url": settings.database_url})
    poll(session, settings, provider=Server().provider(), now=T0)
    airtel_server = Server()
    poll(session, airtel, provider=airtel_server.provider(), now=T0)
    # Airtel fetched the documents itself: Safaricom's rows are not Airtel's memory.
    assert airtel_server.urls() == [FEED_URL, RAIN_URL, WIND_URL]

    saf, air = _rows(session, "safaricom"), _rows(session, "airtel")
    assert saf and air and not ({r.id for r in saf} & {r.id for r in air})
    # The same CAP identifier lives under both operators without breaking the unique key.
    assert f"{WIND_ID}#NBI" in {r.external_id for r in air} and f"{WIND_ID}#NBI_E" in {r.external_id for r in saf}

    for rows in (list_signals(session, "safaricom", now=T0), list_signals(session, "safaricom", source="KMD_CAP", now=T0)):
        assert rows and {r.operator_id for r in rows} == {"safaricom"}
    assert {r.operator_id for r in list_signals(session, "airtel", now=T0)} == {"airtel"}
    assert cap_alerts(session, "safaricom", "NBI", now=T0) == []  # NBI is Airtel's region code
    assert [a["region_code"] for a in cap_alerts(session, "airtel", "NBI", now=T0)] == ["NBI"]
    assert cap_feed_health(session, "safaricom", "NBI", T0)["state"] == "never_polled"
    assert cap_feed_health(session, "airtel", "NBI", T0)["state"] == "ok"
    assert cap_feed_health(session, "airtel", "CKA", T0)["state"] == "unmapped"  # Airtel lists no counties there
    assert latest_row(session, "airtel", source="KMD_CAP", region_code="WNY") is None
    assert active_signals_count(session, "airtel", source="KMD_CAP", region_code="WNY", now=T0) == 0


# ------------------------------------------------------------------------------ the scheduler card


def test_job_card_ships_disabled_under_the_weather_agents_flag():
    assert (CAP_JOB.name, CAP_JOB.interval_s, CAP_JOB.enabled_env) == ("kmd_cap", 1800, "WEATHER_ENABLED")
    assert (CAP_JOB.agent, CAP_JOB.graph_name, CAP_JOB.default_enabled) == ("WeatherRiskAgent", "kmd_cap", False)
    assert CAP_JOB.fn is poll and CAP_JOB.max_seconds >= 1 + poller.MAX_DOCS_PER_RUN * 10  # the per-call budget fits
    assert job_enabled(CAP_JOB) is False  # unset reads as OFF in /scheduler/status too


def test_the_job_re_checks_its_own_flag_even_when_run_directly(tmp_db, monkeypatch):
    settings, session = tmp_db
    monkeypatch.setattr(poller, "provider_from_env", lambda: (_ for _ in ()).throw(AssertionError("provider built while disabled")))
    outcome = run_job(CAP_JOB, settings)
    assert outcome.status == "SUCCEEDED" and "skipped" in outcome.summary and _rows(session) == []


def test_run_job_records_a_succeeded_run_when_kmd_is_down(tmp_db, monkeypatch, clean_hub):
    settings, session = tmp_db
    _enable(monkeypatch)

    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    monkeypatch.setattr(poller, "provider_from_env", lambda: KmdCapProvider(client=httpx.Client(transport=httpx.MockTransport(timeout))))
    outcome = run_job(CAP_JOB, settings)
    assert outcome.status == "SUCCEEDED" and outcome.error is None
    assert outcome.consecutive_failures == 0 and outcome.circuit_open is False  # fail-soft never feeds the circuit
    assert "unreachable" in outcome.summary
    assert read_state(session, "kmd_cap").last_status == "SUCCEEDED"
    assert not [r for r in clean_hub.recent(20) if r["type"] == "scheduler.job_failed"]
