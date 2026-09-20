"""iCalendar (RFC 5545) and iMIP (RFC 6047) for maintenance windows — spec §7.5, Phase 5 Lane 5A.

A maintenance window is only useful if the MSP engineer who has to climb the mast at
02:00 gets it *in their calendar*. §7.5 explains why this is a text format and a MIME
part rather than a Calendar API: a Google service account cannot populate an attendee
list without domain-wide delegation, Microsoft Graph app-only cannot write group
calendars, and there is no Workspace/M365 tenant here. iMIP is the IETF answer — an
RFC 5545 VEVENT carried as ``text/calendar; method=REQUEST`` — and every serious client
(Gmail, Outlook, Apple Calendar, Thunderbird) renders it as an invite. It reuses the
SMTP adapter and the transactional outbox this system already has.

WHY THIS IS STDLIB AND NOT ``icalendar``
----------------------------------------
``pyproject.toml`` declares ``icalendar>=7.3`` under the **optional** ``calendar`` extra
and it is not installed in this environment (checked before writing a line of this
module). RFC 5545 is a line-oriented text format and ``email.mime`` — which is what the
iMIP half actually needs — is standard library, so this module has **no import-time
dependency on anything outside the stdlib**. If the extra is later installed, nothing
here changes: :func:`parse_calendar` is a real parser, and ``tests/unit/test_ics.py``
cross-checks the output against ``icalendar`` when it happens to be importable and skips
that one test when it is not. A module that only round-trips through its own writer
proves nothing, which is why the parser is written to the RFC rather than to the writer.

THE FOUR THINGS THAT BREAK A REAL INVITE
----------------------------------------
1. **UID must be stable across updates.** An update that changes ``UID`` does not update
   anything — the attendee gets a *second* event and turns up twice, or at the wrong one.
   :func:`stable_uid` therefore derives the UID from the window id alone and from nothing
   mutable (not the times, not the summary, not the attendee list). The maintenance lane
   stores it once in ``maintenance_windows.uid`` and never recomputes it.
2. **SEQUENCE must increase on every change.** RFC 5546 lets a client *ignore* a REQUEST
   whose SEQUENCE is not greater than the one it already holds. A reschedule that reuses
   the sequence is silently dropped and the engineer keeps the old time. :func:`validate`
   refuses to build an update that did not move the sequence, and
   :func:`invite_idempotency_key` puts the sequence in the outbox key so a genuine change
   is a new row rather than an ignored duplicate.
3. **METHOD:CANCEL is the only thing that removes an event.** A cancelled window that
   goes out as another REQUEST leaves a live booking in the engineer's calendar for a
   window that is not happening. :func:`build_calendar` pairs ``METHOD:CANCEL`` with
   ``STATUS:CANCELLED`` and a bumped sequence, and refuses the mismatched combinations.
4. **The timezone.** The NOC works in EAT (Africa/Nairobi, UTC+3, no DST) and this
   database stores **naive UTC** (``services/clock.py``). An invite written an hour out
   is an engineer at a mast at the wrong time. Local times are emitted with an explicit
   ``TZID`` plus a ``VTIMEZONE`` whose offset is read from ``ZoneInfo`` **at the event's
   own instant** rather than hardcoded to +0300 — and if the zone ever reports a DST
   offset at either endpoint (a single ``STANDARD`` subcomponent would then be a lie),
   the writer falls back to the unambiguous UTC ``Z`` form instead of shipping a wrong
   one. See :func:`_timezone_block`.

WHAT THIS MODULE DOES NOT DO
----------------------------
It does not touch the database, it does not enqueue anything, and it does not send mail.
It takes plain data in (:class:`WindowInvite`) and returns text, a payload dict or an
``EmailMessage``. The maintenance lane owns ``maintenance_windows``; the outbox lane owns
the ``ICS_INVITE`` transmitter. The one seam back to the maintenance lane,
:func:`invite_from_window`, reads its row by attribute name and imports its model only
inside a function — the same guard ``services/evidence.py`` uses for the vendors lane, for
the same reason: a half-written sibling module must not be why the application fails to
start.

INERT BY DEFAULT (§7.5, ``MAINTENANCE_ENABLED=false``). Nothing imports this module today.
:func:`invites_enabled` is the gate the maintenance job must consult *before* enqueuing,
and it is False unless ``MAINTENANCE_ENABLED`` is explicitly true.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from email.message import EmailMessage
from typing import Any, Iterator, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from noc_agents.services.clock import DEFAULT_TIMEZONE, UTC, eat_tz, fmt_eat, to_eat, utcnow

log = logging.getLogger(__name__)

__all__ = [
    "CALENDAR_MIME_TYPE",
    "ICS_OUTBOX_KIND",
    "METHOD_CANCEL",
    "METHOD_REQUEST",
    "PAYLOAD_VERSION",
    "PRODID",
    "IcsValidationError",
    "ParsedCalendar",
    "ParsedEvent",
    "WindowInvite",
    "build_calendar",
    "build_imip_message",
    "cancel_of",
    "invite_from_window",
    "invite_idempotency_key",
    "invite_outbox_payload",
    "invites_enabled",
    "next_sequence",
    "parse_calendar",
    "stable_uid",
    "validate",
]

# --------------------------------------------------------------------------- constants

#: ``PRODID`` is mandatory (RFC 5545 §3.7.3) and is what a support engineer greps for when
#: an invite misbehaves in a client. Versioned separately from the app so a client-side bug
#: can be pinned to a generator version.
PRODID = "-//Kenya NOC Agents//Maintenance Windows 1.0//EN"

METHOD_REQUEST = "REQUEST"
METHOD_CANCEL = "CANCEL"
#: REPLY/COUNTER/REFRESH are the attendee's side of RFC 5546. §7.5.5 is explicit that
#: acceptance replies are not parsed (the task stays ``INVITED`` and is completed by hand),
#: so this module only ever *originates* a scheduling message.
SUPPORTED_METHODS: frozenset[str] = frozenset({METHOD_REQUEST, METHOD_CANCEL})

STATUS_CONFIRMED = "CONFIRMED"
STATUS_CANCELLED = "CANCELLED"
STATUS_TENTATIVE = "TENTATIVE"

#: The iMIP content type (RFC 6047 §2.4). ``method`` and ``component`` are not decoration:
#: Outlook decides whether to render an invite UI from them.
CALENDAR_MIME_TYPE = "text/calendar"
CALENDAR_FILENAME = "invite.ics"

#: The outbox kind. Already in ``OutboxRow.kind``'s comment and in
#: ``orchestrator.outbox.CHANNEL_KINDS`` — so an invite row is approval-gated exactly like
#: an EMAIL row, which is what §7.5.7 ("no invite row leaves HELD before APPROVE_SCHEDULE")
#: requires. Spelled here as a constant rather than imported: this module must not depend
#: on the outbox, which is mid-edit in another lane.
ICS_OUTBOX_KIND = "ICS_INVITE"

#: Bumped if :func:`invite_outbox_payload`'s shape changes. A dispatcher that finds a
#: version it does not know must fail the row rather than guess at the keys.
PAYLOAD_VERSION = 1

#: ``MAINTENANCE_ENABLED`` — default **false** (§7.5 heading). Mirrors the read in
#: ``agents/enrich.py`` for ``WEATHER_ENABLED``: only an explicit true value enables.
MAINTENANCE_ENABLED_ENV = "MAINTENANCE_ENABLED"
_TRUE = ("1", "true", "yes", "on")

#: The right-hand side of a UID. RFC 5545 §3.8.4.7 wants a globally unique value and the
#: convention is an ``@domain`` suffix; it is never parsed by a client, only compared.
UID_DOMAIN_ENV = "ICS_UID_DOMAIN"
DEFAULT_UID_DOMAIN = "noc.local"

_MAX_LINE_OCTETS = 75  # RFC 5545 §3.1 content-line limit, octets not characters
_CRLF = "\r\n"

#: Deliberately permissive: this is a "does it have one @ and no spaces or control
#: characters" check, not an attempt to validate an address. Its job is to keep header
#: injection and obvious junk out of ``ORGANIZER``/``ATTENDEE``, not to bounce a legal
#: address a Kenyan MSP actually uses.
_ADDRESS_RE = re.compile(r"^[^\s@,;:<>\"\\]+@[^\s@,;:<>\"\\]+\.[^\s@,;:<>\"\\]+$")
#: Control characters are forbidden in a content line (RFC 5545 §3.1) and are also the
#: classic header-injection vector once the same text reaches ``Subject:``. TAB, CR and LF
#: are excluded here because a DESCRIPTION legitimately spans lines and :func:`_escape`
#: turns those into the literal ``\n`` the format wants.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
#: The header-injection characters, checked separately on the fields that become a mail
#: header. A newline in a SUMMARY is not a display quirk: that same string is the
#: ``Subject:`` two functions later, where "\r\nBcc: attacker@..." is a *second header*.
#: Found by tests/unit/test_ics.py — the first version of this module had only
#: ``_CONTROL_RE``, which lets CR and LF through everywhere for the DESCRIPTION's sake,
#: and therefore let them through here too.
_NEWLINE_RE = re.compile(r"[\r\n]")

_DT_UTC_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z$")
_DT_LOCAL_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})$")
_DATE_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")


class IcsValidationError(ValueError):
    """The invite is not shippable. §7.5.5: "ICS invalid → validator blocks enqueue".

    Raised *before* anything is enqueued, so a malformed window never reaches an
    attendee's calendar at all rather than arriving as an unparseable attachment.
    """


def invites_enabled() -> bool:
    """``MAINTENANCE_ENABLED`` — default false, so the lane ships inert (§7.5).

    The maintenance job consults this before it enqueues. This module stays callable with
    the flag off (building text has no side effect and the unit tests need it); what the
    flag gates is the row that would actually leave.
    """
    return (os.getenv(MAINTENANCE_ENABLED_ENV) or "").strip().lower() in _TRUE


# --------------------------------------------------------------------------- the input shape


@dataclass(frozen=True)
class WindowInvite:
    """Everything an invite needs, as plain data — no ORM row, no session, no models import.

    Times are **naive UTC**, exactly as ``maintenance_windows.starts_at`` / ``.ends_at``
    store them (``services/clock.py``: "the database keeps naive UTC datetimes and that is
    an existing contract"). Conversion to EAT happens on the way out, here, once. Passing an
    aware datetime is accepted and normalised; passing an EAT-local time *labelled* as UTC
    is the bug this contract exists to prevent, and it cannot be detected from the value —
    hence the emphasis.
    """

    window_id: str
    uid: str
    starts_at: datetime
    ends_at: datetime
    summary: str
    organizer: str
    attendees: tuple[str, ...] = ()
    sequence: int = 0
    operator_id: str = ""
    location: str = ""
    description: str = ""
    #: ``maintenance_windows.status``: PROPOSED | SCHEDULED | CANCELLED | COMPLETED.
    #: Mapped to a VEVENT ``STATUS`` by :func:`_vevent_status`; not emitted verbatim.
    window_status: str = "SCHEDULED"
    #: RFC 5545 RRULE body for a recurring window, e.g. ``FREQ=MONTHLY;BYDAY=1SU``.
    #: Stored in ``maintenance_windows.rrule``; passed through unaltered.
    rrule: str | None = None
    #: Display zone for DTSTART/DTEND. Defaults to the operator profile's zone via
    #: ``clock.eat_tz()``; overridden only by a test or a future non-Kenyan operator.
    tzid: str = ""
    #: ``organizer``'s display name, if the roster has one. Role tokens are preferred over
    #: personal names in this system (§7.7.6), so this is usually something like
    #: "NOC Rift Valley" rather than a human's name.
    organizer_name: str = ""
    #: Optional per-attendee display names, keyed by address. Same role-token preference.
    attendee_names: Mapping[str, str] = field(default_factory=dict)

    def with_sequence(self, sequence: int) -> "WindowInvite":
        """A copy at a new SEQUENCE. The UID is untouched — that is the whole point."""
        return replace(self, sequence=int(sequence))


def stable_uid(window_id: str, *, domain: str | None = None) -> str:
    """``"<window_id>@<domain>"`` — the UID to store once in ``maintenance_windows.uid``.

    Derived from the window id and **nothing else**. The tempting alternatives — hashing
    the start time, or the summary, or the attendee list — all produce a new UID whenever
    the window is edited, and a new UID is a new event: the attendee ends up holding both
    the old booking and the new one with no way to tell which is live. Recompute this as
    often as you like; for a given window it is always the same string.
    """
    wid = (window_id or "").strip()
    if not wid:
        raise IcsValidationError("stable_uid: window_id is required; a UID must identify a window")
    host = (domain or os.getenv(UID_DOMAIN_ENV) or DEFAULT_UID_DOMAIN).strip() or DEFAULT_UID_DOMAIN
    return f"{wid}@{host}"


def next_sequence(current: int | None) -> int:
    """``SEQUENCE + 1``. Call on every change to a window that has already been invited.

    RFC 5546 §3.2.2: a client may ignore a REQUEST whose SEQUENCE is not higher than the
    one it holds. "Ignore" is silent — no error, no bounce, just an engineer who never
    learns the window moved.
    """
    return int(current or 0) + 1


def cancel_of(invite: WindowInvite) -> WindowInvite:
    """The invite to send as ``METHOD:CANCEL``: same UID, sequence bumped, status CANCELLED.

    Same UID because a CANCEL that does not match the UID the client holds cancels nothing;
    bumped sequence because a CANCEL at the same sequence is as ignorable as a REQUEST at
    the same sequence.
    """
    return replace(invite, sequence=next_sequence(invite.sequence), window_status="CANCELLED")


# ---------------------------------------------------- the seam to the maintenance lane


def _window_row() -> type | None:
    """``MaintenanceWindowRow`` if the maintenance lane has landed it, else None.

    Guarded, not a module-level import, for the reason ``services/evidence.py`` gives for
    ``ClockEventRow``: this module is reachable from the API and a half-written sibling
    module must not be the thing that stops the application starting. Nothing here needs
    the class except :func:`invite_from_window`'s isinstance-free duck typing, so a miss
    is a log line, not an outage.
    """
    try:  # noqa: SIM105 - the except clause is the point
        from noc_agents.db.models_maintenance import MaintenanceWindowRow  # type: ignore
    except Exception:  # noqa: BLE001 — a mid-flight sibling module must not break startup
        return None
    return MaintenanceWindowRow


def invite_from_window(
    row: Any,
    *,
    attendees: Sequence[str] | None = None,
    summary: str | None = None,
    description: str = "",
    location: str | None = None,
    organizer: str | None = None,
    tzid: str = "",
) -> WindowInvite:
    """Adapt a ``maintenance_windows`` row (or any object with those attributes) to plain data.

    Read by attribute name with ``getattr`` defaults rather than by type, so the maintenance
    lane can hand over an ORM row, a pydantic model or a ``SimpleNamespace`` and this module
    stays ignorant of all three. ``attendees_ref`` is a *reference* in the schema (§7.5.1),
    not an address list, so the caller resolves it against the roster and passes the
    addresses in; when it already holds a list on the row, that is used as a fallback.
    """
    uid = str(getattr(row, "uid", "") or "").strip()
    window_id = str(getattr(row, "id", "") or "")
    raw_attendees = attendees if attendees is not None else getattr(row, "attendees", ()) or ()
    scope_ref = str(getattr(row, "scope_ref", "") or "")
    return WindowInvite(
        window_id=window_id,
        uid=uid or stable_uid(window_id),
        starts_at=getattr(row, "starts_at"),
        ends_at=getattr(row, "ends_at"),
        summary=summary or (f"Planned maintenance — {scope_ref}" if scope_ref else "Planned maintenance"),
        organizer=organizer or str(getattr(row, "organizer", "") or ""),
        attendees=tuple(str(a).strip() for a in raw_attendees if str(a).strip()),
        sequence=int(getattr(row, "sequence", 0) or 0),
        operator_id=str(getattr(row, "operator_id", "") or ""),
        location=scope_ref if location is None else location,
        description=description,
        window_status=str(getattr(row, "status", "SCHEDULED") or "SCHEDULED"),
        rrule=(str(getattr(row, "rrule", "") or "") or None),
        tzid=tzid,
    )


# --------------------------------------------------------------------------- validation


def _clean_text(value: str | None, *, what: str, allow_newlines: bool = False) -> str:
    """Reject control characters rather than silently mangling them.

    ``allow_newlines`` is True only for DESCRIPTION, which is genuinely multi-line and whose
    newlines :func:`_escape` turns into the literal ``\\n`` RFC 5545 asks for. Everywhere
    else a line break is refused, because SUMMARY and LOCATION reach the mail ``Subject:``
    and an embedded CRLF there is header injection, not formatting.
    """
    text = str(value or "")
    if _CONTROL_RE.search(text):
        raise IcsValidationError(f"{what} contains a control character; refusing to build an invite from it")
    if not allow_newlines and _NEWLINE_RE.search(text):
        raise IcsValidationError(f"{what} contains a line break; it would become a second mail header")
    return text


def _clean_address(value: str | None, *, what: str) -> str:
    address = str(value or "").strip()
    if not _ADDRESS_RE.match(address):
        raise IcsValidationError(f"{what} is not an e-mail address: {address!r}")
    return address


def validate(invite: WindowInvite, *, method: str = METHOD_REQUEST, previous_sequence: int | None = None) -> None:
    """Raise :class:`IcsValidationError` unless this invite is safe to build and enqueue.

    §7.5.5 makes the validator the gate in front of the outbox. The checks are the ones
    that produce a *wrong* calendar entry rather than an ugly one:

    * an end at or before the start — clients variously show a zero-length event, a
      day-long event, or nothing at all;
    * a missing UID — every send becomes a new event;
    * a sequence that did not move on an update — the update is ignored (RFC 5546 §3.2.2);
    * no attendees — an invite nobody receives, which reads in the UI as "invited";
    * a CANCEL that is not marked cancelled, or a cancelled window shipped as a REQUEST —
      either one leaves a live booking for a window that is not happening.
    """
    if method not in SUPPORTED_METHODS:
        raise IcsValidationError(f"unsupported iTIP method {method!r}; this module originates {sorted(SUPPORTED_METHODS)}")
    if not (invite.uid or "").strip():
        raise IcsValidationError("invite has no UID; every send would create a duplicate event")
    if not isinstance(invite.starts_at, datetime) or not isinstance(invite.ends_at, datetime):
        raise IcsValidationError("starts_at and ends_at must be datetimes (naive UTC, as stored)")
    start, end = _as_utc(invite.starts_at), _as_utc(invite.ends_at)
    if end <= start:
        raise IcsValidationError(
            f"window ends at or before it starts ({_iso(start)} → {_iso(end)}); a client cannot render that"
        )
    if int(invite.sequence) < 0:
        raise IcsValidationError("SEQUENCE must be a non-negative integer (RFC 5545 §3.8.7.4)")
    if previous_sequence is not None and int(invite.sequence) <= int(previous_sequence):
        raise IcsValidationError(
            f"SEQUENCE {invite.sequence} does not exceed the sequence already sent ({previous_sequence}); "
            "RFC 5546 lets the client ignore this message outright"
        )
    _clean_text(invite.summary, what="SUMMARY")
    _clean_text(invite.description, what="DESCRIPTION", allow_newlines=True)
    _clean_text(invite.location, what="LOCATION")
    _clean_address(invite.organizer, what="ORGANIZER")
    if not invite.attendees:
        raise IcsValidationError("invite has no attendees; nobody would receive the window")
    for address in invite.attendees:
        _clean_address(address, what="ATTENDEE")
    if invite.rrule is not None:
        rrule = _clean_text(invite.rrule, what="RRULE").strip()
        if not rrule or "FREQ=" not in rrule.upper():
            raise IcsValidationError(f"RRULE must carry a FREQ part (RFC 5545 §3.3.10): {invite.rrule!r}")
    cancelled = _vevent_status(invite) == STATUS_CANCELLED
    if method == METHOD_CANCEL and not cancelled:
        raise IcsValidationError("METHOD:CANCEL requires window_status=CANCELLED; STATUS:CONFIRMED would contradict it")
    if method == METHOD_REQUEST and cancelled:
        raise IcsValidationError(
            "a CANCELLED window cannot go out as METHOD:REQUEST — the engineer would keep a live booking"
        )


def _vevent_status(invite: WindowInvite) -> str:
    """``maintenance_windows.status`` → a VEVENT ``STATUS`` (RFC 5545 §3.8.1.11).

    The two vocabularies are not the same and must not be passed through: ``PROPOSED`` and
    ``COMPLETED`` are not iCalendar VEVENT statuses. A proposed window that has somehow
    reached the builder is TENTATIVE (an engineer seeing "tentative" is correct); a
    completed one is CONFIRMED, because it did happen.
    """
    status = (invite.window_status or "").strip().upper()
    if status == "CANCELLED":
        return STATUS_CANCELLED
    if status == "PROPOSED":
        return STATUS_TENTATIVE
    return STATUS_CONFIRMED


# --------------------------------------------------------------------------- time


def _as_utc(dt: datetime) -> datetime:
    """Aware UTC from the storage contract: a naive value **is** UTC (``clock.to_utc``)."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _naive_utc(dt: datetime) -> datetime:
    return _as_utc(dt).replace(tzinfo=None)


def _iso(dt: datetime) -> str:
    return _as_utc(dt).replace(tzinfo=None).isoformat() + "Z"


def _zone_for(invite: WindowInvite) -> tuple[str, ZoneInfo]:
    """The display zone: the invite's override, else the operator profile's (``clock.eat_tz``)."""
    name = (invite.tzid or "").strip()
    if not name:
        zone = eat_tz()
        return str(getattr(zone, "key", DEFAULT_TIMEZONE)), zone
    try:
        return name, ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        log.warning("ics: unknown TZID %r; falling back to %s", name, DEFAULT_TIMEZONE)
        zone = eat_tz()
        return str(getattr(zone, "key", DEFAULT_TIMEZONE)), zone


def _fmt_utc(dt: datetime) -> str:
    """``20260916T090000Z`` — RFC 5545 form 2, UTC. Never ambiguous, never needs a VTIMEZONE."""
    return _as_utc(dt).strftime("%Y%m%dT%H%M%SZ")


def _fmt_local(dt: datetime, zone: ZoneInfo) -> str:
    """``20260916T120000`` — RFC 5545 form 3, local time, meaningless without its TZID."""
    return _as_utc(dt).astimezone(zone).strftime("%Y%m%dT%H%M%S")


def _offset_text(delta: timedelta | None) -> str:
    """``+0300``. RFC 5545 UTC-offset form; seconds are truncated (LMT offsets have them)."""
    total = int((delta or timedelta(0)).total_seconds())
    sign = "-" if total < 0 else "+"
    total = abs(total)
    return f"{sign}{total // 3600:02d}{(total % 3600) // 60:02d}"


def _timezone_block(tzid: str, zone: ZoneInfo, start: datetime, end: datetime) -> list[str] | None:
    """A minimal single-``STANDARD`` VTIMEZONE, or None when one would be a lie.

    Africa/Nairobi is UTC+3 all year with no DST, so one STANDARD subcomponent describes it
    completely and correctly. That is a *fact about this zone*, not a general truth, and it
    is the kind of fact that quietly stops being true when an operator profile is pointed at
    a different timezone. So the offset is read from ``ZoneInfo`` at the event's own instant
    (never hardcoded to +0300), and if ``dst()`` is non-zero at either endpoint — i.e. this
    zone *does* observe DST, so a lone STANDARD block would misdate every event outside the
    standard period — this returns None and the caller emits plain UTC instead. A UTC invite
    displays perfectly everywhere; it is only less pleasant to read in the raw file.
    """
    start_local, end_local = _as_utc(start).astimezone(zone), _as_utc(end).astimezone(zone)
    if (start_local.dst() or timedelta(0)) or (end_local.dst() or timedelta(0)):
        log.info("ics: %s reports a DST offset for this window; emitting UTC times instead of TZID", tzid)
        return None
    offset = _offset_text(start_local.utcoffset())
    if _offset_text(end_local.utcoffset()) != offset:  # an offset change inside the window
        log.info("ics: %s changes UTC offset inside this window; emitting UTC times instead of TZID", tzid)
        return None
    name = start_local.tzname() or "UTC"
    return [
        "BEGIN:VTIMEZONE",
        f"TZID:{tzid}",
        f"X-LIC-LOCATION:{tzid}",
        "BEGIN:STANDARD",
        # FROM == TO: this zone has one offset. Both properties are mandatory anyway.
        f"TZOFFSETFROM:{offset}",
        f"TZOFFSETTO:{offset}",
        f"TZNAME:{name}",
        "DTSTART:19700101T000000",  # "since forever, as far as this file is concerned"
        "END:STANDARD",
        "END:VTIMEZONE",
    ]


# --------------------------------------------------------------------------- writing


def _escape(value: str) -> str:
    """RFC 5545 §3.3.11 TEXT escaping. Order matters: backslash first, or it doubles twice."""
    return (
        value.replace("\\", "\\\\")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
        .replace(";", "\\;")
        .replace(",", "\\,")
    )


def _unescape(value: str) -> str:
    """Inverse of :func:`_escape`, left to right so ``\\\\n`` is a literal backslash-n."""
    out: list[str] = []
    i = 0
    while i < len(value):
        char = value[i]
        if char == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            out.append({"n": "\n", "N": "\n", ";": ";", ",": ",", "\\": "\\"}.get(nxt, nxt))
            i += 2
            continue
        out.append(char)
        i += 1
    return "".join(out)


def _fold(line: str) -> list[str]:
    """Fold a content line to ≤ 75 **octets** (RFC 5545 §3.1), not 75 characters.

    A Nairobi site name with a non-ASCII character is more than one octet, and a fold that
    counts characters produces lines a strict parser rejects. Worse, a naive fold can split
    a multi-byte UTF-8 sequence across the break and corrupt it, so the split point is found
    on the encoded bytes and walked back to a character boundary.
    """
    encoded = line.encode("utf-8")
    if len(encoded) <= _MAX_LINE_OCTETS:
        return [line]
    out: list[str] = []
    rest = encoded
    limit = _MAX_LINE_OCTETS
    while len(rest) > limit:
        cut = limit
        while cut > 0 and (rest[cut] & 0xC0) == 0x80:  # never split a UTF-8 continuation byte
            cut -= 1
        if cut == 0:  # pathological: a single character wider than the limit
            cut = limit
        out.append(rest[:cut].decode("utf-8", errors="ignore"))
        rest = rest[cut:]
        limit = _MAX_LINE_OCTETS - 1  # continuation lines carry a leading space
    out.append(rest.decode("utf-8", errors="ignore"))
    return [out[0]] + [" " + part for part in out[1:]]


def _param_value(value: str) -> str:
    """Quote a parameter value when it contains a character that would end it early."""
    if any(ch in value for ch in ':;,"'):
        return '"' + value.replace('"', "") + '"'
    return value


def _prop(name: str, value: str, params: Mapping[str, str] | None = None, *, escape: bool = True) -> str:
    parts = [name]
    for key, param in (params or {}).items():
        if param:
            parts.append(f";{key}={_param_value(param)}")
    return "".join(parts) + ":" + (_escape(value) if escape else value)


def build_calendar(
    invite: WindowInvite,
    *,
    method: str = METHOD_REQUEST,
    dtstamp: datetime | None = None,
    previous_sequence: int | None = None,
) -> str:
    """The full ``VCALENDAR`` text (CRLF line endings) for one window.

    ``dtstamp`` defaults to now. Unlike an evidence pack, an iCalendar object is *supposed*
    to carry a generation timestamp — RFC 5545 makes ``DTSTAMP`` mandatory and RFC 5546 uses
    it to order two messages that share a SEQUENCE — so this is not the
    ``services/evidence.py`` "no clock inside the hash" rule being broken; it is a different
    document with a different contract. It is injectable so a test can pin the bytes.
    """
    method = (method or METHOD_REQUEST).strip().upper()
    validate(invite, method=method, previous_sequence=previous_sequence)

    tzid, zone = _zone_for(invite)
    vtimezone = _timezone_block(tzid, zone, invite.starts_at, invite.ends_at)
    if vtimezone is not None:
        time_params = {"TZID": tzid}
        dtstart, dtend = _fmt_local(invite.starts_at, zone), _fmt_local(invite.ends_at, zone)
    else:  # see _timezone_block: UTC is the safe answer when one STANDARD block cannot describe the zone
        time_params = {}
        dtstart, dtend = _fmt_utc(invite.starts_at), _fmt_utc(invite.ends_at)

    status = _vevent_status(invite)
    lines: list[str] = [
        "BEGIN:VCALENDAR",
        f"PRODID:{_escape(PRODID)}",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        f"METHOD:{method}",
    ]
    lines += vtimezone or []
    lines += [
        "BEGIN:VEVENT",
        _prop("UID", invite.uid, escape=False),  # a UID is opaque; escaping would change identity
        _prop("DTSTAMP", _fmt_utc(dtstamp or utcnow()), escape=False),
        _prop("DTSTART", dtstart, time_params, escape=False),
        _prop("DTEND", dtend, time_params, escape=False),
        _prop("SUMMARY", invite.summary or "Planned maintenance"),
        f"SEQUENCE:{int(invite.sequence)}",
        f"STATUS:{status}",
        # A maintenance window genuinely blocks the engineer: OPAQUE is what makes it show
        # as busy rather than as a note nobody's free/busy check can see.
        "TRANSP:OPAQUE",
        _prop(
            "ORGANIZER",
            f"mailto:{invite.organizer}",
            {"CN": invite.organizer_name} if invite.organizer_name else None,
            escape=False,
        ),
    ]
    if invite.location:
        lines.append(_prop("LOCATION", invite.location))
    if invite.description:
        lines.append(_prop("DESCRIPTION", invite.description))
    if invite.rrule:
        lines.append(_prop("RRULE", invite.rrule.strip(), escape=False))  # RRULE is structured, not TEXT
    for address in invite.attendees:
        params = {
            "ROLE": "REQ-PARTICIPANT",
            # NEEDS-ACTION on a CANCEL too: RFC 5546 says the attendee's own participation
            # status is theirs to set, and this system never learns it (§7.5.5 — replies are
            # not parsed), so claiming ACCEPTED here would be asserting something unknown.
            "PARTSTAT": "NEEDS-ACTION",
            "RSVP": "TRUE",
            "CUTYPE": "INDIVIDUAL",
        }
        name = invite.attendee_names.get(address, "")
        if name:
            params["CN"] = name
        lines.append(_prop("ATTENDEE", f"mailto:{address}", params, escape=False))
    lines += ["END:VEVENT", "END:VCALENDAR"]

    folded: list[str] = []
    for line in lines:
        folded.extend(_fold(line))
    return _CRLF.join(folded) + _CRLF  # RFC 5545 §3.1: every content line ends with CRLF


# --------------------------------------------------------------------------- parsing


@dataclass(frozen=True)
class ParsedEvent:
    """One VEVENT, read back. Property access is by name; repeated names keep every value."""

    props: dict[str, list[tuple[dict[str, str], str]]]

    def get(self, name: str) -> str | None:
        """The first value of ``name``, unescaped, or None."""
        entries = self.props.get(name.upper())
        return _unescape(entries[0][1]) if entries else None

    def params(self, name: str) -> dict[str, str]:
        entries = self.props.get(name.upper())
        return dict(entries[0][0]) if entries else {}

    def all(self, name: str) -> list[str]:
        return [value for _, value in self.props.get(name.upper(), [])]

    # --- the fields a round-trip test actually asserts on ---
    @property
    def uid(self) -> str | None:
        return self.get("UID")

    @property
    def sequence(self) -> int:
        raw = self.get("SEQUENCE")
        return int(raw) if raw and raw.strip().lstrip("-").isdigit() else 0

    @property
    def status(self) -> str | None:
        return self.get("STATUS")

    @property
    def summary(self) -> str | None:
        return self.get("SUMMARY")

    @property
    def location(self) -> str | None:
        return self.get("LOCATION")

    @property
    def description(self) -> str | None:
        return self.get("DESCRIPTION")

    @property
    def rrule(self) -> str | None:
        return self.get("RRULE")

    @property
    def organizer(self) -> str | None:
        return _mailto(self.get("ORGANIZER"))

    @property
    def attendees(self) -> list[str]:
        return [address for address in (_mailto(_unescape(v)) for _, v in self.props.get("ATTENDEE", [])) if address]

    @property
    def dtstart_utc(self) -> datetime | None:
        """The start as a **naive UTC** datetime — directly comparable with what was stored."""
        return self._instant("DTSTART")

    @property
    def dtend_utc(self) -> datetime | None:
        return self._instant("DTEND")

    @property
    def dtstamp_utc(self) -> datetime | None:
        return self._instant("DTSTAMP")

    def _instant(self, name: str) -> datetime | None:
        entries = self.props.get(name.upper())
        if not entries:
            return None
        params, value = entries[0]
        return _parse_datetime(value, params.get("TZID"))


@dataclass(frozen=True)
class ParsedCalendar:
    """A parsed VCALENDAR. ``method`` is what tells a client to render an invite at all."""

    props: dict[str, list[tuple[dict[str, str], str]]]
    events: list[ParsedEvent]
    timezones: list[dict[str, list[tuple[dict[str, str], str]]]]

    @property
    def method(self) -> str | None:
        entries = self.props.get("METHOD")
        return entries[0][1] if entries else None

    @property
    def prodid(self) -> str | None:
        entries = self.props.get("PRODID")
        return _unescape(entries[0][1]) if entries else None

    @property
    def version(self) -> str | None:
        entries = self.props.get("VERSION")
        return entries[0][1] if entries else None

    @property
    def event(self) -> ParsedEvent:
        """The single VEVENT. Raises when there is not exactly one — an iMIP scheduling
        message carries one component type and this module never writes more."""
        if len(self.events) != 1:
            raise IcsValidationError(f"expected exactly one VEVENT, found {len(self.events)}")
        return self.events[0]

    @property
    def timezone_ids(self) -> list[str]:
        return [entries["TZID"][0][1] for entries in self.timezones if entries.get("TZID")]


def _mailto(value: str | None) -> str | None:
    """``"mailto:a@b.com"`` → ``"a@b.com"``. The scheme is case-insensitive (RFC 3986)."""
    if not value:
        return None
    text = value.strip()
    return text[7:] if text[:7].lower() == "mailto:" else text


def _parse_datetime(value: str, tzid: str | None) -> datetime | None:
    """RFC 5545 date-time → naive UTC, which is this database's storage contract.

    Three value forms exist and all three appear in the wild:
    ``…Z`` (UTC), a floating/local form that is only meaningful with its ``TZID``, and a
    date-only ``VALUE=DATE``. A local form with an unknown or missing TZID is treated as
    UTC and logged — guessing EAT for a value whose zone we do not know would invent a
    three-hour error rather than admit one.
    """
    text = (value or "").strip()
    if match := _DT_UTC_RE.match(text):
        return datetime(*(int(g) for g in match.groups()))  # type: ignore[arg-type]
    if match := _DT_LOCAL_RE.match(text):
        naive = datetime(*(int(g) for g in match.groups()))  # type: ignore[arg-type]
        if not tzid:
            log.info("ics: local date-time %r has no TZID; reading it as UTC", text)
            return naive
        try:
            zone = ZoneInfo(tzid)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            log.warning("ics: unknown TZID %r on %r; reading it as UTC", tzid, text)
            return naive
        return naive.replace(tzinfo=zone).astimezone(UTC).replace(tzinfo=None)
    if match := _DATE_RE.match(text):
        parts = [int(g) for g in match.groups()]
        return datetime(parts[0], parts[1], parts[2])
    return None


def _unfold(text: str) -> Iterator[str]:
    """Undo RFC 5545 §3.1 folding: a line beginning with SPACE or TAB continues the previous one."""
    current: str | None = None
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t"):
            current = (current or "") + raw[1:]
            continue
        if current is not None:
            yield current
        current = raw
    if current is not None:
        yield current


def _split_line(line: str) -> tuple[str, dict[str, str], str] | None:
    """``NAME;PARAM=v:value`` → (NAME, params, raw value). Colons inside a quoted parameter
    value do not end the parameter section, which is exactly why this is not ``split(":")``."""
    name_end, in_quotes = -1, False
    for index, char in enumerate(line):
        if char == '"':
            in_quotes = not in_quotes
        elif char == ":" and not in_quotes:
            name_end = index
            break
    if name_end < 0:
        return None
    head, value = line[:name_end], line[name_end + 1 :]
    params: dict[str, str] = {}
    segments, buf, in_quotes = [], "", False
    for char in head:
        if char == '"':
            in_quotes = not in_quotes
            continue
        if char == ";" and not in_quotes:
            segments.append(buf)
            buf = ""
            continue
        buf += char
    segments.append(buf)
    name = segments[0].strip().upper()
    for segment in segments[1:]:
        key, _, param_value = segment.partition("=")
        if key.strip():
            params[key.strip().upper()] = param_value.strip()
    return name, params, value


def parse_calendar(text: str | bytes) -> ParsedCalendar:
    """Parse an ICS document. Written to RFC 5545, not to :func:`build_calendar`.

    That distinction is the point of the round-trip test: a parser that merely mirrors the
    writer's assumptions will happily agree with a wrong file. This one unfolds, splits
    parameters with quoting honoured, and resolves TZID against the real tz database.
    """
    if isinstance(text, bytes):
        text = text.decode("utf-8-sig")
    calendar_props: dict[str, list[tuple[dict[str, str], str]]] = {}
    events: list[ParsedEvent] = []
    timezones: list[dict[str, list[tuple[dict[str, str], str]]]] = []
    # Nested components (VALARM inside VEVENT, STANDARD inside VTIMEZONE) mean a stack, not
    # a flag: a flat "am I in a VEVENT" boolean attributes a STANDARD's DTSTART to the event.
    stack: list[tuple[str, dict[str, list[tuple[dict[str, str], str]]]]] = []
    for line in _unfold(text):
        if not line.strip():
            continue
        parsed = _split_line(line)
        if parsed is None:
            continue
        name, params, value = parsed
        if name == "BEGIN":
            stack.append((value.strip().upper(), {}))
            continue
        if name == "END":
            if not stack:
                continue
            component, props = stack.pop()
            if component == "VEVENT" and len(stack) == 1:
                events.append(ParsedEvent(props=props))
            elif component == "VTIMEZONE":
                timezones.append(props)
            elif component == "VCALENDAR" and not stack:
                # The outermost component's own properties (METHOD, PRODID, VERSION) were
                # collected on the stack like any others; they only become the calendar's
                # when it closes. Missing this is how METHOD reads back as None — and a
                # calendar with no METHOD is a file, not an invite, in every client.
                calendar_props = props
            continue
        target = stack[-1][1] if stack else calendar_props
        target.setdefault(name, []).append((params, value))
    if stack:
        raise IcsValidationError(f"unterminated component(s): {[c for c, _ in stack]}")
    if not calendar_props and not events:
        # Everything landed inside the outermost BEGIN:VCALENDAR, which the loop pops last.
        raise IcsValidationError("no VCALENDAR content found")
    return ParsedCalendar(props=calendar_props, events=events, timezones=timezones)


# --------------------------------------------------------------------------- iMIP / MIME


def _plain_body(invite: WindowInvite, method: str) -> str:
    """The text/plain alternative — what a client that cannot render an invite shows.

    Times are printed in **EAT with the label**, because "02:00" without a zone at the top
    of an email is how an engineer ends up at a mast three hours early. The UTC instants
    follow in brackets so the mail is self-checking against the database.
    """
    start_eat, end_eat = to_eat(_naive_utc(invite.starts_at)), to_eat(_naive_utc(invite.ends_at))
    verb = "CANCELLED" if method == METHOD_CANCEL else "Scheduled"
    # Plain ASCII throughout (" -> ", not an arrow glyph). The elegant character costs the
    # whole message its 7-bit encoding: one non-ASCII byte and ``EmailMessage`` base64s the
    # entire body, which then depends on the relay negotiating 8BITMIME and is unreadable
    # in a raw mail log at 3 a.m. Nothing here is worth that.
    lines = [
        f"{verb}: {invite.summary}",
        "",
        f"When:  {start_eat:%a %d %b %Y} {fmt_eat(_naive_utc(invite.starts_at))} "
        f"-> {fmt_eat(_naive_utc(invite.ends_at))}"
        + (f" ({end_eat:%a %d %b %Y})" if end_eat.date() != start_eat.date() else ""),
        f"       ({_iso(invite.starts_at)} -> {_iso(invite.ends_at)} UTC)",
    ]
    if invite.location:
        lines.append(f"Where: {invite.location}")
    if invite.rrule:
        lines.append(f"Repeats: {invite.rrule}")
    if invite.description:
        lines += ["", invite.description]
    if method == METHOD_CANCEL:
        lines += ["", "This maintenance window has been cancelled. Do not attend."]
    lines += ["", f"Window reference: {invite.window_id or invite.uid}"]
    return "\n".join(lines) + "\n"


def _subject(invite: WindowInvite, method: str) -> str:
    prefix = "Cancelled: " if method == METHOD_CANCEL else ""
    if invite.sequence and method != METHOD_CANCEL:
        prefix = "Updated: "
    when = fmt_eat(_naive_utc(invite.starts_at), "%a %d %b %H:%M")
    # ASCII separator for the same reason as the body: an em dash turns the Subject into an
    # RFC 2047 encoded-word, which is correct but unreadable everywhere a header is logged.
    return _clean_text(f"{prefix}{invite.summary} - {when}", what="Subject")


def invite_idempotency_key(invite: WindowInvite, method: str = METHOD_REQUEST) -> str:
    """The outbox ``idempotency_key``: ``ics:<operator>:<uid>:<METHOD>:<sequence>``.

    Deliberately includes the sequence. ``enqueue`` is INSERT OR IGNORE on this column, so:
    re-running the job for an unchanged window is a no-op (same key, existing row), while a
    reschedule — which bumps the sequence — is a genuinely new row that will actually be
    sent. Leaving the sequence out would make every update after the first a silent no-op,
    which is the same failure mode as forgetting to bump SEQUENCE, one layer down.
    """
    method = (method or METHOD_REQUEST).strip().upper()
    operator = (invite.operator_id or "-").strip() or "-"
    return f"ics:{operator}:{invite.uid}:{method}:{int(invite.sequence)}"


def invite_outbox_payload(
    invite: WindowInvite,
    *,
    method: str = METHOD_REQUEST,
    dtstamp: datetime | None = None,
    previous_sequence: int | None = None,
) -> dict[str, Any]:
    """The ``payload_json`` for an ``outbox(kind="ICS_INVITE")`` row.

    Self-contained on purpose: the dispatcher runs **after commit**, with no transaction
    open, and must not have to re-read ``maintenance_windows`` to build the message. It
    therefore carries the finished ICS text, the recipients and the MIME parameters, and the
    transmitter is ``build_imip_message(payload)`` plus one ``send_message``.

    No secrets and no free-text from an external source travel in here (§7.0.2: "recipients
    as refs; never secrets"); attendee addresses do, because an e-mail cannot be addressed
    to a reference — they are personal data, which is why §7.5.6 requires a
    ``record_transfer`` row when the relay sits outside Kenya.
    """
    method = (method or METHOD_REQUEST).strip().upper()
    ics = build_calendar(invite, method=method, dtstamp=dtstamp, previous_sequence=previous_sequence)
    return {
        "payload_version": PAYLOAD_VERSION,
        "operator_id": invite.operator_id,
        "window_id": invite.window_id,
        "uid": invite.uid,
        "sequence": int(invite.sequence),
        "method": method,
        "subject": _subject(invite, method),
        "body": _plain_body(invite, method),
        "organizer": invite.organizer,
        "to": list(invite.attendees),
        "ics": ics,
        "filename": CALENDAR_FILENAME,
        "content_type": CALENDAR_MIME_TYPE,
        "starts_at": _iso(invite.starts_at),
        "ends_at": _iso(invite.ends_at),
    }


def build_imip_message(
    payload: Mapping[str, Any],
    *,
    from_addr: str | None = None,
    to_addrs: Sequence[str] | None = None,
) -> EmailMessage:
    """Turn an ``ICS_INVITE`` payload into the iMIP message (RFC 6047 §2.4).

    Shape — and every part of it earns its place::

        multipart/mixed
        ├── multipart/alternative
        │   ├── text/plain                     what a client that ignores iMIP shows
        │   └── text/calendar; method=REQUEST; component=VEVENT   the invite itself
        └── text/calendar; name="invite.ics"; Content-Disposition: attachment

    The ``method`` parameter on the ``text/calendar`` part is not cosmetic: Outlook decides
    whether to draw Accept/Decline buttons from it, and an invite delivered without it
    arrives as a file nobody opens. The duplicate **attachment** part is there for the
    clients (older Outlook, several mobile clients) that only act on an attached ``.ics``;
    it is the same bytes, so an attendee who gets both sees one event — same UID.

    ``from_addr`` defaults to the ORGANIZER. RFC 6047 §3 expects the ``From:`` to match the
    ORGANIZER for a REQUEST; a mismatch is why some clients quietly drop an invite.
    """
    method = str(payload.get("method") or METHOD_REQUEST).strip().upper()
    if method not in SUPPORTED_METHODS:
        raise IcsValidationError(f"unsupported iMIP method {method!r}")
    version = int(payload.get("payload_version") or 0)
    if version != PAYLOAD_VERSION:
        # Fail loudly rather than build a message from keys this version does not understand.
        raise IcsValidationError(f"ICS_INVITE payload_version {version} is not {PAYLOAD_VERSION}; refusing to guess")
    ics = str(payload.get("ics") or "")
    if not ics.strip():
        raise IcsValidationError("ICS_INVITE payload carries no calendar text")
    organizer = str(payload.get("organizer") or "")
    recipients = [str(a).strip() for a in (to_addrs if to_addrs is not None else payload.get("to") or []) if str(a).strip()]
    if not recipients:
        raise IcsValidationError("ICS_INVITE payload has no recipients")
    sender = (from_addr or organizer or "").strip()

    msg = EmailMessage()
    msg["Subject"] = _clean_text(payload.get("subject"), what="Subject")
    if sender:
        msg["From"] = sender
    msg["To"] = ", ".join(recipients)

    msg.set_content(str(payload.get("body") or ""))
    msg.add_alternative(
        ics,
        subtype="calendar",
        charset="utf-8",
        params={"method": method, "component": "VEVENT"},
    )
    msg.add_attachment(
        ics.encode("utf-8"),
        maintype="text",
        subtype="calendar",
        filename=str(payload.get("filename") or CALENDAR_FILENAME),
        params={"method": method, "component": "VEVENT", "charset": "utf-8"},
    )
    # Set LAST, deliberately, and not up with the other headers. ``add_alternative`` and
    # ``add_attachment`` restructure the message into a multipart, and that restructuring
    # moves every header whose name begins with "Content-" **down into the first subpart**
    # — so a Content-Class set earlier ends up on the text/plain part, where nothing looks
    # for it, and reads back as None on the message. Found by tests/unit/test_ics.py.
    # Outlook routes calendar messages on this header without descending into the body
    # (MS-OXCICAL); it is inert everywhere else.
    msg["Content-Class"] = "urn:content-classes:calendarmessage"
    return msg


def imip_bytes(payload: Mapping[str, Any], **kwargs: Any) -> bytes:
    """The wire bytes of the iMIP message — what a test asserts on and a relay sends."""
    return build_imip_message(payload, **kwargs).as_bytes()
