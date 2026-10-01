"""KMD CAP: the Kenya Meteorological Department's severe-weather warnings (spec §7.3, §5.3.13).

This is the *only* module that talks to meteo.go.ke. Everything else reads the
``external_signals`` rows that ``pollers/kmd_cap.py`` writes, so a dead feed degrades to a
STALE badge and never to a crash, and no incident on the hot path ever waits on it.

What the feed is
----------------
KMD publishes an RSS 2.0 feed (https://meteo.go.ke/api/cap/rss.xml) whose ``<item>`` links
each point at one OASIS **CAP 1.2** document (http://docs.oasis-open.org/emergency/cap/v1.2/
CAP-v1.2-os.html), for example https://meteo.go.ke/api/cap/269c47c8-953c-4ee2-850b-aafe83d91c24.xml
with ``areaDesc`` blocks for Migori, Nyamira, Bungoma and Busia. The spec records the feed as
**authoritative but 132 days stale on the day it was checked** (§7.3) — which is why the
poller treats "the feed answered" and "the feed is current" as two separate facts.

The parser reads what a CAP document says and nothing more: ``identifier``, ``sender``,
``sent``, ``status``, ``msgType``, ``references`` and, from the primary ``<info>`` block,
``category``, ``event``, ``urgency``, ``severity``, ``certainty``, ``effective``, ``onset``,
``expires``, ``headline``, ``description``, ``instruction`` and every ``<area>``'s
``areaDesc`` / ``polygon``. Strings are kept verbatim. Timestamps are converted to naive UTC
(the database contract) and nothing else: an alert is the Met Department's statement under
its own name, and this module does not score, soften or extend it.

Parsing untrusted XML from the network
--------------------------------------
XML from a server we do not control is a real attack surface, and CAP arrives as XML. What
closes each risk, in the order :func:`parse_xml` applies it:

1. **Size.** Every fetch is read in chunks and abandoned the moment it passes
   :data:`MAX_FEED_BYTES` / :data:`MAX_ALERT_BYTES`; a ``Content-Length`` over the cap is refused
   unread. Requests send ``Accept-Encoding: identity`` and a response that is compressed anyway
   is refused unread, so the cap measures the bytes that would reach the parser — a 300 KB gzip
   body cannot inflate to hundreds of megabytes before the cap is looked at.
2. **Time.** A total deadline (the provider's ``timeout_s``, 10 s by default — spec §9) on the
   whole exchange — connection, TLS, response headers (including any run of ``1xx`` interim
   responses) and body. httpx's timeout is per socket read, so a server dripping one byte every
   nine seconds never trips it, in the headers or in the body. The deadline is therefore
   enforced *below* httpx, by :class:`DeadlineWatchdog`: through httpx's documented ``trace``
   request extension it takes its own duplicate descriptor of the connection and, if the
   deadline passes, shuts the connection down from a timer thread, which unblocks whatever
   read is in progress — plain HTTP or TLS, handshake included (both production feeds are
   ``https``; review finding W02-TLS). Requests send ``Connection: close`` so every request opens
   (and so exposes) its own connection. A per-chunk check remains as a second, clock-based
   bound on the body (review findings F06, W02).
3. **Encoding: UTF-8 only, refused before any parser runs.** A UTF-16 or UTF-32 byte-order mark,
   any NUL byte, an XML declaration naming another encoding, or bytes that are not valid UTF-8 —
   each is ``malformed``. KMD serves UTF-8, and a CAP feed that is not UTF-8 is a malformed
   feed. This is not tidiness: an earlier version searched the raw bytes for ``<!ENTITY``, and a
   UTF-16 body spells that ``<\0!\0E\0...``, so the search never matched and the declarations
   went straight through to expat (review finding F01). Restricting the input to one encoding
   is what makes a byte-level check mean what it says — and it is also why the stored
   ``raw_xml`` is always a faithful, re-parseable copy.
4. **Entity declarations, refused by a byte scan** — exact now that the body is known UTF-8.
   A CAP document or an RSS feed has no legitimate use for ``<!ENTITY``; it is the prerequisite
   for every expansion attack ("billion laughs", quadratic blow-up) and for XXE.
5. **The parser refuses them again, by construction.** With ``defusedxml`` importable, it
   parses with entities and external references forbidden. Without it (the stdlib path),
   :func:`_stdlib_fromstring` drives an ``xml.parsers.expat`` parser whose
   ``StartDoctypeDeclHandler`` raises on any DOCTYPE with an internal subset and whose
   ``EntityDeclHandler`` raises on any declaration, with parameter-entity parsing off. Expat
   calls the DOCTYPE handler before it reads the subset, so an entity declaration is refused
   before it is ever processed — in any encoding, whatever the byte scan did or did not see.
   A bare ``<!DOCTYPE rss PUBLIC ... "...rss-0.91.dtd">`` (legal RSS, no subset) still parses,
   and nothing is ever fetched: pyexpat has no network access and no external-entity handler
   is installed.

**Why two parser paths.** ``defusedxml`` is not a declared dependency, and the rule for this
lane is "no new dependency". It is in the development interpreter only because ``nbconvert``
pulls it in, so a clean ``pip install -e .`` will not have it. Layers 1–5 hold on both paths;
:data:`XML_PARSER` says which parser is in use and the poller reports it on every run.

Failure handling
----------------
Every failure is one :class:`CapError` — a :class:`~noc_agents.adapters.weather.WeatherError`
subclass, so it carries the same ``kind`` vocabulary (``timeout``, ``tls``, ``network``,
``http``, ``malformed``, ``oversize``, ``config``) and ``except WeatherError`` in a caller
catches all three early-warning providers. Nothing here retries or sleeps.

Tests never reach the network: :class:`KmdCapProvider` takes an injectable ``httpx.Client``
and the suite builds one on ``httpx.MockTransport`` (``tests/unit/test_kmd_cap.py``).
"""

from __future__ import annotations

import logging
import os
import re
import ssl
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterable, Mapping
from xml.etree.ElementTree import Element, ParseError, TreeBuilder
from xml.parsers import expat

import httpx

from noc_agents.adapters.weather import (
    DEFAULT_TIMEOUT_S,
    DeadlineWatchdog,
    DnsTimeout,
    WeatherError,
    _per_wait,
    default_client,
    resolve_within_deadline,
)

log = logging.getLogger("noc_agents.adapters.kmd_cap")

try:  # the hardened parser, when the interpreter happens to carry it (see module docstring)
    from defusedxml.ElementTree import fromstring as _defused_fromstring

    XML_PARSER = "defusedxml"
except ImportError:  # pragma: no cover - exercised by monkeypatching _xml_fromstring in tests
    _defused_fromstring = None
    XML_PARSER = "stdlib-expat-guarded"

__all__ = [
    "CAP_NAMESPACES",
    "DEFAULT_FEED_URL",
    "KMD_CAP",
    "MAX_ALERT_BYTES",
    "MAX_FEED_BYTES",
    "XML_PARSER",
    "CapAlert",
    "CapArea",
    "CapError",
    "FeedFetch",
    "FeedItem",
    "KmdCapProvider",
    "kmd_cap_alerts",
    "parse_cap_alert",
    "parse_feed",
    "parse_xml",
    "provider_from_env",
    "split_area_desc",
]

KMD_CAP = "KMD_CAP"
DEFAULT_FEED_URL = "https://meteo.go.ke/api/cap/rss.xml"

#: Size caps, checked while streaming and before parsing. A CAP document with four county
#: polygons is tens of kilobytes; an RSS feed of fifty items is a few hundred. The caps sit an
#: order of magnitude above that so a real feed never trips them and a runaway one stops early.
MAX_FEED_BYTES = 2 * 1024 * 1024
MAX_ALERT_BYTES = 1 * 1024 * 1024

#: CAP 1.2 is current; 1.1 still circulates. Elements are matched by LOCAL name so either
#: version, and a CAP document embedded inside an Atom entry, parse the same way.
CAP_NAMESPACES: tuple[str, ...] = (
    "urn:oasis:names:tc:emergency:cap:1.2",
    "urn:oasis:names:tc:emergency:cap:1.1",
)

#: An entity declaration — the prerequisite for every expansion attack. A plain byte search is
#: exact only because :func:`_require_utf8` has already refused every other encoding (layer 3).
_ENTITY_DECL = re.compile(rb"<!ENTITY", re.IGNORECASE)

#: Byte-order marks of the encodings this module refuses. UTF-32 LE starts with the UTF-16 LE
#: mark, so both are simply "starts with one of these".
_NON_UTF8_BOMS: tuple[tuple[bytes, str], ...] = (
    (b"\x00\x00\xfe\xff", "UTF-32 BE"),
    (b"\xff\xfe\x00\x00", "UTF-32 LE"),
    (b"\xfe\xff", "UTF-16 BE"),
    (b"\xff\xfe", "UTF-16 LE"),
)
_UTF8_BOM = b"\xef\xbb\xbf"
_XML_DECL_ENCODING = re.compile(rb"^\s*<\?xml[^>]*?\bencoding\s*=\s*[\"']([A-Za-z0-9._-]+)[\"']")

#: The clock the total fetch deadline is measured on. Indirected so a test can advance it.
_clock = time.monotonic


class CapError(WeatherError):
    """One CAP failure, classified. ``str(err)`` is safe for ``external_signals.last_error``."""

    def __init__(self, kind: str, message: str, *, status: int | None = None) -> None:
        super().__init__(kind, message, status=status, source=KMD_CAP)


# ---------------------------------------------------------------------------- value types


@dataclass(frozen=True)
class FeedItem:
    """One ``<item>`` (RSS 2.0) or ``<entry>`` (Atom) from the KMD feed."""

    link: str  # the CAP document URL; "" when the item embeds its alert instead
    guid: str
    title: str
    published: datetime | None  # naive UTC; None when the item carries no usable date
    embedded_alert: CapAlert | None = None  # some CAP feeds inline the whole <alert>


@dataclass(frozen=True)
class FeedFetch:
    """The result of one feed request. ``not_modified`` means a 304: nothing new to read."""

    source_url: str
    fetched_at: datetime
    not_modified: bool
    items: tuple[FeedItem, ...] = ()
    last_modified: str | None = None  # verbatim header, echoed back as If-Modified-Since
    newest_published: datetime | None = None
    size_bytes: int = 0


@dataclass(frozen=True)
class CapArea:
    """One CAP ``<area>``. ``counties`` are the ``areaDesc`` pieces, spelled as KMD spelt them."""

    area_desc: str
    counties: tuple[str, ...]
    polygons: tuple[str, ...] = ()


@dataclass(frozen=True)
class CapAlert:
    """One CAP document, verbatim apart from timestamps (naive UTC).

    ``None`` means the document did not say — never a default somebody chose. ``expires`` in
    particular is optional in CAP 1.2, and a missing one is carried as ``None`` so the poller
    can decide, visibly, how long to keep repeating an alert that names no end.
    """

    identifier: str
    sender: str
    sent: datetime | None
    status: str
    msg_type: str
    scope: str | None
    references: tuple[str, ...]  # identifiers this message updates or cancels
    category: str | None
    event: str | None
    urgency: str | None
    severity: str | None
    certainty: str | None
    effective: datetime | None
    onset: datetime | None
    expires: datetime | None
    headline: str | None
    description: str | None
    instruction: str | None
    sender_name: str | None
    web: str | None
    language: str | None
    areas: tuple[CapArea, ...]
    source_url: str = ""
    raw_xml: str = field(default="", repr=False)

    @property
    def counties(self) -> tuple[str, ...]:
        """Every county piece across every area, first occurrence order, no duplicates."""
        seen: dict[str, None] = {}
        for area in self.areas:
            for county in area.counties:
                seen.setdefault(county, None)
        return tuple(seen)

    @property
    def is_actual(self) -> bool:
        """CAP ``status=Actual``: a real warning. ``Test``/``Exercise``/``System``/``Draft`` are not."""
        return (self.status or "").strip().lower() == "actual"

    def as_payload(self) -> dict[str, Any]:
        """What was said, as a JSON-able dict — the ``derived`` half of the stored row."""

        def iso(dt: datetime | None) -> str | None:
            return dt.replace(microsecond=0).isoformat() + "Z" if dt else None

        return {
            "identifier": self.identifier,
            "sender": self.sender,
            "sent": iso(self.sent),
            "status": self.status,
            "msgType": self.msg_type,
            "scope": self.scope,
            "references": list(self.references),
            "category": self.category,
            "event": self.event,
            "urgency": self.urgency,
            "severity": self.severity,
            "certainty": self.certainty,
            "effective": iso(self.effective),
            "onset": iso(self.onset),
            "expires": iso(self.expires),
            "headline": self.headline,
            "description": self.description,
            "instruction": self.instruction,
            "senderName": self.sender_name,
            "web": self.web,
            "language": self.language,
            "areas": [
                {"areaDesc": a.area_desc, "counties": list(a.counties), "polygon_count": len(a.polygons)}
                for a in self.areas
            ],
            "source_url": self.source_url,
        }


# ---------------------------------------------------------------------------- XML helpers


class _XmlRefused(ValueError):
    """Raised from inside the guarded expat parser's handlers to stop the parse."""


def _stdlib_fromstring(body: bytes) -> Element:
    """The stdlib path (layer 5): an expat parser that refuses entity machinery by construction.

    ``xml.etree.ElementTree.fromstring`` gives no access to expat's handlers (the C accelerator
    hides the parser object), so this drives ``xml.parsers.expat`` directly and feeds an
    ``ElementTree.TreeBuilder`` — the resulting tree is the same shape ``fromstring`` builds
    (namespaced tags as ``{uri}local``; the unit tests compare them on every fixture).

    * ``StartDoctypeDeclHandler`` raises when the DOCTYPE has an internal subset. Expat calls it
      at ``<!DOCTYPE name [``, *before* reading the subset, so nothing declared there is ever
      processed — whatever the encoding, whatever a byte scan saw.
    * ``EntityDeclHandler`` raises on any entity declaration that reaches it anyway.
    * Parameter-entity parsing is off, and no external-entity handler is installed, so an
      external DTD is never read (pyexpat cannot fetch anything regardless).
    """
    builder = TreeBuilder()
    parser = expat.ParserCreate(None, "}")
    parser.buffer_text = True
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)

    def fixname(name: str) -> str:
        return "{" + name if "}" in name else name

    def doctype(name, system_id, public_id, has_internal_subset):
        if has_internal_subset:
            raise _XmlRefused(f"DOCTYPE {name!r} carries an internal subset; refused before it was read")

    def entity_decl(name, *rest):
        raise _XmlRefused(f"entity declaration {name!r} refused")

    parser.StartDoctypeDeclHandler = doctype
    parser.EntityDeclHandler = entity_decl
    parser.StartElementHandler = lambda tag, attrs: builder.start(fixname(tag), {fixname(k): v for k, v in attrs.items()})
    parser.EndElementHandler = lambda tag: builder.end(fixname(tag))
    parser.CharacterDataHandler = builder.data
    parser.Parse(body, True)
    return builder.close()


def _xml_fromstring(body: bytes) -> Element:
    """Parse with defusedxml when present, else the guarded stdlib expat. Indirected so tests
    can force the stdlib path and prove it is safe on a machine that has defusedxml."""
    if _defused_fromstring is not None:
        # forbid_dtd stays False: a bare <!DOCTYPE rss> is legal RSS and harmless; the danger
        # is entity DECLARATIONS, which forbid_entities (the default) refuses.
        return _defused_fromstring(body, forbid_dtd=False, forbid_entities=True, forbid_external=True)
    return _stdlib_fromstring(body)


def _require_utf8(body: bytes, *, what: str) -> str:
    """Layer 3: refuse anything that is not UTF-8, before any parser sees it. Returns the text.

    The returned text is what gets stored as ``raw_xml`` — a strict decode, so the stored copy
    is exactly what KMD sent and can be parsed again (review finding F16: the old
    ``decode("utf-8", errors="replace")`` turned a UTF-16 document into unreadable NULs).
    """
    for bom, name in _NON_UTF8_BOMS:
        if body.startswith(bom):
            raise CapError("malformed", f"{what} starts with a {name} byte-order mark; only UTF-8 is accepted, refused before parsing")
    if b"\x00" in body:
        raise CapError("malformed", f"{what} contains NUL bytes (UTF-16/UTF-32 without a byte-order mark?); only UTF-8 is accepted, refused before parsing")
    declared = _XML_DECL_ENCODING.match(body[len(_UTF8_BOM):] if body.startswith(_UTF8_BOM) else body)
    if declared and declared.group(1).decode("ascii").lower().replace("_", "-") not in {"utf-8", "utf8"}:
        raise CapError(
            "malformed",
            f"{what} declares encoding {declared.group(1).decode('ascii')!r}; only UTF-8 is accepted, refused before parsing",
        )
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CapError("malformed", f"{what} is not valid UTF-8 ({exc.reason} at byte {exc.start}); refused before parsing") from exc


def parse_xml(body: bytes, *, what: str, max_bytes: int) -> Element:
    """Untrusted bytes → an element tree, or :class:`CapError`. The single XML entry point.

    Order is the defence (module docstring, layers 1, 3, 4, 5): size, then UTF-8 only, then
    the entity byte scan, then a parser that refuses entity declarations by construction.
    """
    if len(body) > max_bytes:
        raise CapError("oversize", f"{what} is {len(body)} bytes, over the {max_bytes}-byte cap; not parsed")
    _require_utf8(body, what=what)
    if _ENTITY_DECL.search(body):
        raise CapError(
            "malformed",
            f"{what} declares an XML entity (<!ENTITY ...>); refused before parsing — a CAP "
            "document or RSS feed has no use for one, and entity expansion is the attack",
        )
    try:
        return _xml_fromstring(body)
    except (ParseError, expat.ExpatError) as exc:
        raise CapError("malformed", f"{what} is not well-formed XML ({exc})") from exc
    except ValueError as exc:  # _XmlRefused, and defusedxml's DefusedXmlException family
        raise CapError("malformed", f"{what} refused by the XML hardening ({type(exc).__name__}: {exc})") from exc


def _local(tag: Any) -> str:
    """``{urn:...}identifier`` → ``identifier``. Comments/PIs have a non-str tag → ``""``."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _children(el: Element, name: str) -> list[Element]:
    return [c for c in list(el) if _local(c.tag) == name]


def _child(el: Element, name: str) -> Element | None:
    for c in list(el):
        if _local(c.tag) == name:
            return c
    return None


def _text(el: Element | None, name: str) -> str | None:
    """Text of the first child called ``name``, stripped; ``None`` if absent or empty."""
    if el is None:
        return None
    child = _child(el, name)
    if child is None or child.text is None:
        return None
    value = child.text.strip()
    return value or None


def _find_first(root: Element, name: str) -> Element | None:
    if _local(root.tag) == name:
        return root
    for el in root.iter():
        if _local(el.tag) == name:
            return el
    return None


def _to_naive_utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo is not None else dt


def _parse_cap_time(value: str | None) -> datetime | None:
    """CAP ``dateTime`` (``2026-05-07T09:00:00+03:00``; ``-00:00`` means UTC) → naive UTC.

    CAP 1.2 §3.3.2 requires an explicit offset. A stamp without one is ambiguous, and the
    only zone a Kenyan forecaster would mean is EAT (UTC+3) — but guessing a zone is exactly
    the kind of silent reinterpretation this module refuses, so an offset-less stamp is
    treated as unparseable (``None``) and the poller records that the document gave no
    usable time.
    """
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return _to_naive_utc(parsed)


def _parse_rfc822(value: str | None) -> datetime | None:
    """RSS ``pubDate`` (``Thu, 07 May 2026 06:00:00 GMT``) → naive UTC; ISO also accepted."""
    if not value:
        return None
    try:
        return _to_naive_utc(parsedate_to_datetime(value.strip()))
    except (TypeError, ValueError, IndexError):
        return _parse_cap_time(value)


#: ``areaDesc`` separators: commas, semicolons, "and", "&", and parentheses, so that
#: "Coast (Mombasa, Kilifi)" yields Coast, Mombasa and Kilifi rather than "Coast (Mombasa"
#: (review finding F17). NOT "/" — "Elgeyo/Marakwet" is one county.
_AREA_SPLIT = re.compile(r"\s*(?:,|;|\band\b|&|\(|\))\s*", re.IGNORECASE)


def split_area_desc(area_desc: str | None) -> tuple[str, ...]:
    """``"Migori, Nyamira and Busia"`` → ``("Migori", "Nyamira", "Busia")``, spelling untouched.

    KMD's own example alert carries one ``<area>`` per county, but a free-text ``areaDesc`` is
    exactly that, free text, and a forecaster listing several counties in one line is the
    ordinary case elsewhere. Splitting is on list separators only; mapping a piece to a
    county (and to a region) is ``services.signals``' job, and a piece nobody recognises is
    kept as written so it can be shown and fixed rather than dropped.
    """
    if not area_desc:
        return ()
    # A piece with no letters is punctuation left between separators ("Coast (Mombasa,Kilifi)."
    # leaves "."), never a place; it would otherwise be stored as a county row (review finding NEW3).
    return tuple(p.strip() for p in _AREA_SPLIT.split(area_desc) if p and any(ch.isalpha() for ch in p))


# ---------------------------------------------------------------------------- parsers


def parse_cap_alert(body: bytes | Element, *, source_url: str = "", raw_xml: str | None = None) -> CapAlert:
    """One CAP 1.2 (or 1.1) ``<alert>`` → :class:`CapAlert`, or ``CapError("malformed")``.

    ``identifier`` and ``sent`` are required by the CAP schema and by this system (the first
    is the dedupe key, the second is the only honest "when"); a document without them is
    rejected whole rather than stored half-understood. When several ``<info>`` blocks exist
    (CAP allows one per language) the English one is primary, else the first; the areas come
    from that same block, so a Swahili duplicate never doubles the county list.
    """
    if isinstance(body, Element):
        root = body
        raw = raw_xml or ""
    else:
        root = parse_xml(body, what=f"CAP document {source_url or '(inline)'}", max_bytes=MAX_ALERT_BYTES)
        # parse_xml has already proven the bytes are UTF-8, so this strict decode is the
        # document exactly as KMD sent it (review finding F16).
        raw = raw_xml if raw_xml is not None else body.decode("utf-8")

    alert = _find_first(root, "alert")
    if alert is None:
        raise CapError("malformed", f"CAP document {source_url or '(inline)'} has no <alert> element")
    identifier = _text(alert, "identifier")
    if not identifier:
        raise CapError("malformed", f"CAP document {source_url or '(inline)'} has no <identifier>")
    sent_raw = _text(alert, "sent")
    sent = _parse_cap_time(sent_raw)
    if sent is None:
        raise CapError(
            "malformed",
            f"CAP {identifier}: <sent>={sent_raw!r} is missing or has no UTC offset (CAP 1.2 §3.3.2 requires one)",
        )

    infos = _children(alert, "info")
    info: Element | None = None
    for candidate in infos:
        lang = (_text(candidate, "language") or "en-US").lower()
        if lang.startswith("en"):
            info = candidate
            break
    if info is None and infos:
        info = infos[0]

    areas: list[CapArea] = []
    if info is not None:
        for area_el in _children(info, "area"):
            desc = _text(area_el, "areaDesc") or ""
            polygons = tuple((p.text or "").strip() for p in _children(area_el, "polygon") if (p.text or "").strip())
            areas.append(CapArea(area_desc=desc, counties=split_area_desc(desc), polygons=polygons))

    references_raw = _text(alert, "references") or ""
    # CAP 1.2 §3.2.1: space-separated "sender,identifier,sent" triples. Only the identifier
    # is needed to find what an Update/Cancel is about.
    references = tuple(
        parts[1].strip()
        for parts in (triple.split(",") for triple in references_raw.split())
        if len(parts) >= 2 and parts[1].strip()
    )

    return CapAlert(
        identifier=identifier,
        sender=_text(alert, "sender") or "",
        sent=sent,
        status=_text(alert, "status") or "",
        msg_type=_text(alert, "msgType") or "",
        scope=_text(alert, "scope"),
        references=references,
        category=_text(info, "category"),
        event=_text(info, "event"),
        urgency=_text(info, "urgency"),
        severity=_text(info, "severity"),
        certainty=_text(info, "certainty"),
        # CAP: "effective" defaults to "sent" when absent. That default is the standard's,
        # not ours, so applying it is repeating the Met Department, not reinterpreting it.
        effective=_parse_cap_time(_text(info, "effective")) or sent,
        onset=_parse_cap_time(_text(info, "onset")),
        expires=_parse_cap_time(_text(info, "expires")),
        headline=_text(info, "headline"),
        description=_text(info, "description"),
        instruction=_text(info, "instruction"),
        sender_name=_text(info, "senderName"),
        web=_text(info, "web"),
        language=_text(info, "language"),
        areas=tuple(areas),
        source_url=source_url,
        raw_xml=raw,
    )


def _item_link(item: Element) -> str:
    """RSS ``<link>text</link>`` or Atom ``<link href=".."/>`` (a CAP-typed link preferred)."""
    links = _children(item, "link")
    best = ""
    for link in links:
        href = (link.get("href") or "").strip()
        if href:
            typ = (link.get("type") or "").lower()
            if "cap" in typ or href.lower().endswith(".xml"):
                return href
            best = best or href
        elif link.text and link.text.strip():
            return link.text.strip()
    return best


def parse_feed(body: bytes, *, source_url: str = DEFAULT_FEED_URL) -> tuple[FeedItem, ...]:
    """The KMD RSS 2.0 feed (or an Atom feed of the same alerts) → :class:`FeedItem` rows.

    Documented shape (RSS 2.0, https://www.rssboard.org/rss-specification): ``rss/channel/item``
    with ``title``, ``link``, ``guid`` and ``pubDate`` (RFC 822). Atom (RFC 4287) uses
    ``feed/entry`` with ``id``, ``link/@href`` and ``updated``/``published``. An item that
    carries a whole ``<alert>`` inline (some CAP aggregators do this) is parsed on the spot so
    the poller does not fetch what it already has. A feed whose root is neither ``rss`` nor
    ``feed`` is ``malformed`` — an HTML error page served with a 200 must not read as an empty
    feed, because an empty feed reads as "no warnings".
    """
    root = parse_xml(body, what=f"CAP feed {source_url}", max_bytes=MAX_FEED_BYTES)
    root_name = _local(root.tag)
    if root_name not in {"rss", "feed", "RDF"}:
        raise CapError("malformed", f"CAP feed {source_url}: root element is <{root_name}>, not an RSS or Atom feed")

    items: list[FeedItem] = []
    for el in root.iter():
        name = _local(el.tag)
        if name not in {"item", "entry"}:
            continue
        embedded_el = _find_first(el, "alert")
        embedded = None
        if embedded_el is not None and embedded_el is not el:
            try:
                embedded = parse_cap_alert(embedded_el, source_url=source_url)
            except CapError as exc:
                log.warning("kmd_cap: an inline alert in the feed was unreadable (%s); falling back to its link", exc)
        guid = _text(el, "guid") or _text(el, "id") or ""
        published = _parse_rfc822(_text(el, "pubDate")) or _parse_cap_time(
            _text(el, "updated") or _text(el, "published")
        )
        items.append(
            FeedItem(
                link=_item_link(el),
                guid=guid,
                title=_text(el, "title") or "",
                published=published,
                embedded_alert=embedded,
            )
        )
    return tuple(items)


# ---------------------------------------------------------------------------- HTTP


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


def _bounded_get(
    client: httpx.Client,
    url: str,
    *,
    headers: Mapping[str, str] | None,
    max_bytes: int,
    what: str,
    timeout_s: float | None = None,
) -> tuple[int, bytes, httpx.Headers]:
    """One GET bounded in bytes AND in total time. Returns ``(status, body, headers)``.

    * **Bytes:** streams and stops at ``max_bytes``; a declared ``Content-Length`` over it is
      refused unread. ``Accept-Encoding: identity`` is sent and a compressed response is refused
      unread (review finding F15), so the count is of the bytes the parser would receive.
    * **Time:** ``timeout_s`` is a total deadline on the whole exchange, headers included,
      enforced by :class:`DeadlineWatchdog` (review findings F06, W02), plus a per-chunk clock
      check on the body. A 304 is checked against the deadline before it is returned too.

    A 304 returns an empty body; a status ≥ 400 is ``http``; transport problems are classified
    the way ``adapters/weather.py`` classifies them, so ``last_error`` reads the same whichever
    early-warning provider wrote it.
    """
    if timeout_s is None:
        # Default to the budget the client was built with (its per-read timeout), so a caller
        # that configured a 1 s client gets a 1 s total deadline, not the module's 10 s.
        timeout_s = getattr(getattr(client, "timeout", None), "read", None) or DEFAULT_TIMEOUT_S
    deadline = _clock() + timeout_s
    watchdog = DeadlineWatchdog(timeout_s)
    # No single socket wait may outlast the whole budget either. The watchdog ends a DRIP at the
    # deadline; a SILENT server is ended by this (on Windows, shutdown() does not wake a recv
    # blocked on a socket that receives nothing -- measured -- so without it a silent server
    # would run to the client's own per-read timeout).
    per_wait = _per_wait(client, timeout_s)

    def check_deadline() -> None:
        if watchdog.fired or _clock() > deadline:
            raise CapError("timeout", f"{what} did not finish within the {timeout_s:g} s total deadline; abandoned")

    # Connection: close — each request opens its own connection, so the watchdog can see it.
    request_headers = {"Accept-Encoding": "identity", "Connection": "close", **dict(headers or {})}
    try:
        # Name resolution happens before any socket exists, so the watchdog cannot bound it
        # (review finding DNS): resolve inside the same budget, then connect to the address.
        try:
            resolve_within_deadline(url, timeout_s)
        except DnsTimeout as exc:
            raise CapError("timeout", f"{what}: {exc}") from exc
        with client.stream(
            "GET", url, headers=request_headers, timeout=per_wait, extensions={"trace": watchdog.trace}
        ) as response:
            status = response.status_code
            if status == 304:
                check_deadline()  # a 304 that took past the deadline is still past the deadline
                return status, b"", response.headers
            encoding = (response.headers.get("Content-Encoding") or "identity").strip().lower()
            if encoding not in {"", "identity"}:
                raise CapError(
                    "malformed",
                    f"{what} arrived with Content-Encoding {encoding!r} although identity was requested; "
                    "refused unread (a compressed body can inflate far past the size cap before it is counted)",
                    status=status if status >= 400 else None,
                )
            if status >= 400:
                snippet = b""
                for chunk in response.iter_bytes():
                    snippet += chunk
                    if len(snippet) >= 200:
                        break
                    check_deadline()
                reason = snippet[:200].decode("utf-8", errors="replace").strip()
                raise CapError("http", f"{what} returned HTTP {status}{(': ' + reason) if reason else ''}", status=status)
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise CapError("oversize", f"{what} declares {declared} bytes, over the {max_bytes}-byte cap; not read")
            buf = bytearray()
            for chunk in response.iter_bytes():
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    raise CapError("oversize", f"{what} passed the {max_bytes}-byte cap while streaming; abandoned")
                check_deadline()
            check_deadline()
            return status, bytes(buf), response.headers
    except CapError:
        raise
    except (httpx.TransportError, OSError) as exc:
        # The deadline, not a network fault, when either the watchdog shut the socket or a single
        # wait ran out the per-request cap that IS the budget (a silent server).
        if watchdog.fired or (isinstance(exc, httpx.TimeoutException) and per_wait.read == timeout_s):
            raise CapError("timeout", f"{what} did not finish within the {timeout_s:g} s total deadline; connection closed") from exc
        if isinstance(exc, httpx.TimeoutException):
            raise CapError("timeout", f"{what} did not answer within the timeout ({exc.__class__.__name__})") from exc
        if _is_tls_failure(exc):
            raise CapError(
                "tls",
                f"{what}: TLS verification failed ({exc.__class__.__name__}: {exc}); on this machine "
                "set NOC_USE_TRUSTSTORE=1 with truststore installed (docs/RUNBOOK.md §3)",
            ) from exc
        raise CapError("network", f"{what} unreachable ({exc.__class__.__name__}: {exc})") from exc
    finally:
        watchdog.cancel()


class KmdCapProvider:
    """The KMD CAP client: one feed request, then one request per CAP document not yet held.

    Stateless on purpose. What a polite client must remember between runs — the feed's
    ``Last-Modified`` for ``If-Modified-Since``, and which CAP documents were already fetched
    — is remembered by the poller *in the database*, because a provider object lives for one
    run and a process restart must not turn into a burst of re-fetches against a government
    server that publishes no usage terms at all (§7.3.4: "be polite").
    """

    source = KMD_CAP

    def __init__(
        self,
        feed_url: str | None = None,
        *,
        client: httpx.Client | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        user_agent: str | None = None,
    ) -> None:
        self.feed_url = (feed_url or DEFAULT_FEED_URL).strip()
        self._client = client
        self._timeout_s = timeout_s
        self._user_agent = (user_agent or "").strip() or None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = default_client(self._timeout_s, user_agent=self._user_agent)
        return self._client

    def fetch_feed(self, *, if_modified_since: str | None = None, now: datetime | None = None) -> FeedFetch:
        """GET the RSS feed. A 304 comes back as ``not_modified=True`` with no items."""
        fetched_at = now or datetime.now(timezone.utc).replace(tzinfo=None)
        headers = {"Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.1"}
        if if_modified_since:
            headers["If-Modified-Since"] = if_modified_since
        status, body, resp_headers = _bounded_get(
            self.client, self.feed_url, headers=headers, max_bytes=MAX_FEED_BYTES, what="KMD CAP feed",
            timeout_s=self._timeout_s,
        )
        if status == 304:
            if not if_modified_since:
                # A 304 answers a CONDITIONAL request. To an unconditional one it is a protocol
                # violation that carries no feed; treating it as "unchanged" would skip exactly the
                # retry the poller withheld the conditional header to force (review finding NEW1).
                raise CapError(
                    "http",
                    "KMD CAP feed answered 304 Not Modified to a request with no If-Modified-Since; "
                    "no feed was received, so nothing was read",
                    status=304,
                )
            return FeedFetch(
                source_url=self.feed_url, fetched_at=fetched_at, not_modified=True,
                last_modified=resp_headers.get("Last-Modified") or if_modified_since,
            )
        items = parse_feed(body, source_url=self.feed_url)
        stamps = [i.published for i in items if i.published is not None]
        stamps += [i.embedded_alert.sent for i in items if i.embedded_alert is not None and i.embedded_alert.sent]
        return FeedFetch(
            source_url=self.feed_url,
            fetched_at=fetched_at,
            not_modified=False,
            items=items,
            last_modified=resp_headers.get("Last-Modified"),
            newest_published=max(stamps) if stamps else None,
            size_bytes=len(body),
        )

    def fetch_alert(self, url: str) -> CapAlert:
        """GET and parse one CAP document."""
        if not url:
            raise CapError("malformed", "feed item has no link to a CAP document")
        _, body, _ = _bounded_get(
            self.client, url, headers={"Accept": "application/cap+xml, application/xml;q=0.9, */*;q=0.1"},
            max_bytes=MAX_ALERT_BYTES, what=f"CAP document {url}", timeout_s=self._timeout_s,
        )
        return parse_cap_alert(body, source_url=url)


def provider_from_env(*, client: httpx.Client | None = None, timeout_s: float = DEFAULT_TIMEOUT_S) -> KmdCapProvider:
    """The configured provider. ``KMD_CAP_FEED_URL`` overrides the feed (it is not in
    ``.env.example`` because nobody should need it; it exists so a mirror or a test double can
    be pointed at without a code change). ``MET_NO_USER_AGENT`` is reused as the identifying
    User-Agent when set: KMD publishes no terms, but an identifiable client is the polite
    default the MET Norway terms taught this project, and it costs nothing."""
    return KmdCapProvider(
        os.getenv("KMD_CAP_FEED_URL") or None,
        client=client,
        timeout_s=timeout_s,
        user_agent=os.getenv("MET_NO_USER_AGENT"),
    )


def kmd_cap_alerts(
    provider: KmdCapProvider | None = None,
    *,
    skip_links: Iterable[str] = (),
) -> tuple[FeedFetch, list[CapAlert], list[tuple[str, CapError]]]:
    """§5.3.13's ``kmd_cap_alerts() -> list[CapAlert]`` for ad-hoc and diagnostic use.

    Fetches the feed and every CAP document not in ``skip_links``; per-document failures are
    returned beside the alerts rather than raised, so one broken document never hides the
    others. The poller does not call this — it needs the database in the loop — but a person
    at a console asking "what is KMD saying right now?" should get one call, not a recipe.
    """
    provider = provider or provider_from_env()
    feed = provider.fetch_feed()
    skip = set(skip_links)
    alerts: list[CapAlert] = []
    failures: list[tuple[str, CapError]] = []
    for item in feed.items:
        if item.embedded_alert is not None:
            alerts.append(item.embedded_alert)
            continue
        if item.link in skip:
            continue
        try:
            alerts.append(provider.fetch_alert(item.link))
        except CapError as exc:
            failures.append((item.link, exc))
    return feed, alerts, failures
