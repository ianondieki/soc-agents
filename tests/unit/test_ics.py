"""iCalendar / iMIP for maintenance windows — spec §7.5, §7.5.7, Phase 5 exit criterion.

The exit criterion is "ICS round-trip and MIME headers exact", and the round-trip is the
deliverable: generate, parse back, assert the fields survived. Everything else in this file
protects one of the four ways a technically-valid invite still puts an engineer at a mast
at the wrong time or on the wrong night:

* a UID that moves on an update — the attendee ends up holding two events;
* a SEQUENCE that does not move — the update is silently ignored by the client;
* a cancellation sent as another REQUEST — the cancelled window stays in the calendar;
* a timezone that is assumed rather than stated — the NOC works in EAT (UTC+3), this
  database stores naive UTC, and an invite three hours out looks completely normal.

No network, no database, no ``icalendar`` dependency (it is an unconfigured optional extra;
the one test that uses it skips when it is absent — and that is the test that keeps this
module honest against a real parser rather than only against its own writer).
"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta
from email import message_from_bytes
from email.policy import default as default_policy
from types import SimpleNamespace

import pytest

from noc_agents.services import ics
from noc_agents.services.ics import (
    CALENDAR_MIME_TYPE,
    ICS_OUTBOX_KIND,
    METHOD_CANCEL,
    METHOD_REQUEST,
    PAYLOAD_VERSION,
    IcsValidationError,
    WindowInvite,
    build_calendar,
    build_imip_message,
    cancel_of,
    invite_from_window,
    invite_idempotency_key,
    invite_outbox_payload,
    invites_enabled,
    next_sequence,
    parse_calendar,
    stable_uid,
    validate,
)

# 21:00 UTC is 00:00 EAT the NEXT day — the default maintenance window of §7.5.1
# (``default_start_eat: "00:00"``, ``default_end_eat: "05:00"``). Chosen on purpose: any
# timezone mistake in the writer changes the DATE as well as the hour, which is the loudest
# possible failure and the one an engineer actually experiences.
START_UTC = datetime(2026, 9, 16, 21, 0, 0)
END_UTC = datetime(2026, 9, 17, 2, 0, 0)
DTSTAMP = datetime(2026, 9, 15, 12, 0, 0)

_HAS_ICALENDAR = importlib.util.find_spec("icalendar") is not None


def make_invite(**overrides) -> WindowInvite:
    base = dict(
        window_id="MW-2026-0007",
        uid=stable_uid("MW-2026-0007", domain="noc.example"),
        starts_at=START_UTC,
        ends_at=END_UTC,
        summary="Generator exercise",
        organizer="noc@example.com",
        attendees=("fe.rift@example.com", "egypro.ops@example.com"),
        operator_id="safaricom",
        location="NRB-0421",
        description="Monthly 30-minute run at >=30% nameplate.",
    )
    base.update(overrides)
    return WindowInvite(**base)


def build_and_parse(invite: WindowInvite, **kwargs):
    return parse_calendar(build_calendar(invite, dtstamp=DTSTAMP, **kwargs))


# --------------------------------------------------------------------------- the round trip


def test_every_field_survives_a_generate_parse_round_trip():
    invite = make_invite(rrule="FREQ=MONTHLY;BYMONTHDAY=1")
    calendar = build_and_parse(invite)
    event = calendar.event

    assert calendar.version == "2.0"
    assert calendar.method == METHOD_REQUEST
    assert calendar.prodid == ics.PRODID
    assert event.uid == invite.uid
    assert event.sequence == 0
    assert event.status == "CONFIRMED"
    assert event.summary == invite.summary
    assert event.location == invite.location
    assert event.description == invite.description
    assert event.rrule == "FREQ=MONTHLY;BYMONTHDAY=1"
    assert event.organizer == invite.organizer
    assert event.attendees == list(invite.attendees)
    assert event.dtstamp_utc == DTSTAMP


def test_the_start_and_end_come_back_as_the_same_naive_utc_instants_that_went_in():
    # The storage contract (services/clock.py) is naive UTC. If the parser returns anything
    # else, every comparison against a maintenance_windows row is silently three hours out.
    event = build_and_parse(make_invite()).event
    assert event.dtstart_utc == START_UTC
    assert event.dtend_utc == END_UTC


def test_the_window_is_written_in_eat_local_time_with_an_explicit_tzid():
    text = build_calendar(make_invite(), dtstamp=DTSTAMP)
    # 21:00 UTC on the 16th is 00:00 EAT on the 17th: both the hour AND the date move.
    assert "DTSTART;TZID=Africa/Nairobi:20260917T000000" in text
    assert "DTEND;TZID=Africa/Nairobi:20260917T050000" in text
    assert parse_calendar(text).timezone_ids == ["Africa/Nairobi"]


def test_the_vtimezone_states_the_plus_three_offset_rather_than_leaving_it_to_the_client():
    # A TZID with no VTIMEZONE is a reference to a table the client may not have; Outlook in
    # particular falls back to the viewer's own zone, which puts a Nairobi window in London.
    text = build_calendar(make_invite(), dtstamp=DTSTAMP)
    assert "BEGIN:VTIMEZONE" in text and "TZID:Africa/Nairobi" in text
    assert "TZOFFSETTO:+0300" in text and "TZOFFSETFROM:+0300" in text
    assert "TZNAME:EAT" in text


def test_eat_has_no_dst_so_a_january_window_and_a_july_window_carry_the_same_offset():
    # Kenya does not observe DST. This test exists so that if someone "improves" the
    # timezone block by hardcoding a summer/winter pair, the lie shows up immediately.
    january = build_calendar(
        make_invite(starts_at=datetime(2026, 1, 15, 21, 0), ends_at=datetime(2026, 1, 16, 2, 0)), dtstamp=DTSTAMP
    )
    july = build_calendar(
        make_invite(starts_at=datetime(2026, 7, 15, 21, 0), ends_at=datetime(2026, 7, 16, 2, 0)), dtstamp=DTSTAMP
    )
    assert "TZOFFSETTO:+0300" in january and "TZOFFSETTO:+0300" in july
    assert "DTSTART;TZID=Africa/Nairobi:20260116T000000" in january
    assert "DTSTART;TZID=Africa/Nairobi:20260716T000000" in july


def test_a_zone_that_observes_dst_falls_back_to_utc_instead_of_writing_a_wrong_offset():
    # A single STANDARD subcomponent cannot describe a DST zone, so the writer must not
    # pretend it can. Europe/London in July is +0100; an invite claiming +0000 all year
    # would be an hour out for half the year. UTC form is always unambiguous.
    text = build_calendar(make_invite(tzid="Europe/London"), dtstamp=DTSTAMP)
    assert "BEGIN:VTIMEZONE" not in text
    assert "DTSTART:20260916T210000Z" in text
    assert parse_calendar(text).event.dtstart_utc == START_UTC


def test_dtstamp_is_written_in_utc_because_rfc_5546_orders_messages_by_it():
    assert "DTSTAMP:20260915T120000Z" in build_calendar(make_invite(), dtstamp=DTSTAMP)


# --------------------------------------------------------------------------- UID stability


def test_the_uid_is_derived_from_the_window_id_and_nothing_that_can_change():
    # An update that changes the UID does not update anything: the attendee gets a second
    # event and has no way to know which one is live.
    first = stable_uid("MW-2026-0007", domain="noc.example")
    assert first == stable_uid("MW-2026-0007", domain="noc.example")
    original = make_invite()
    rescheduled = make_invite(
        starts_at=START_UTC + timedelta(days=3),
        ends_at=END_UTC + timedelta(days=3),
        summary="Generator exercise (moved)",
        attendees=("someone.else@example.com",),
        sequence=1,
    )
    assert build_and_parse(original).event.uid == build_and_parse(rescheduled).event.uid == first


def test_a_uid_is_refused_for_a_window_with_no_id_rather_than_invented():
    with pytest.raises(IcsValidationError):
        stable_uid("")


def test_an_invite_without_a_uid_is_blocked_before_it_can_be_enqueued():
    with pytest.raises(IcsValidationError, match="UID"):
        build_calendar(make_invite(uid=""))


# --------------------------------------------------------------------------- SEQUENCE


def test_sequence_increments_on_an_update_and_the_bumped_value_is_what_is_written():
    invite = make_invite(sequence=next_sequence(0))
    assert invite.sequence == 1
    assert "SEQUENCE:1" in build_calendar(invite, dtstamp=DTSTAMP)
    assert build_and_parse(invite).event.sequence == 1


def test_an_update_that_did_not_move_the_sequence_is_refused_because_clients_ignore_it():
    # RFC 5546: a client MAY ignore a REQUEST whose SEQUENCE is not greater than the one it
    # holds. "Ignore" is silent — the engineer simply never learns the window moved.
    with pytest.raises(IcsValidationError, match="SEQUENCE"):
        build_calendar(make_invite(sequence=2), previous_sequence=2)
    with pytest.raises(IcsValidationError, match="SEQUENCE"):
        build_calendar(make_invite(sequence=1), previous_sequence=2)
    build_calendar(make_invite(sequence=3), previous_sequence=2)  # a real increase is fine


def test_a_negative_sequence_is_refused():
    with pytest.raises(IcsValidationError, match="SEQUENCE"):
        build_calendar(make_invite(sequence=-1))


def test_with_sequence_moves_the_sequence_and_leaves_the_uid_alone():
    invite = make_invite()
    bumped = invite.with_sequence(4)
    assert bumped.sequence == 4 and bumped.uid == invite.uid


# --------------------------------------------------------------------------- cancellation


def test_a_cancelled_window_goes_out_as_method_cancel_with_the_same_uid_and_a_higher_sequence():
    scheduled = make_invite(sequence=2)
    cancelled = cancel_of(scheduled)
    calendar = build_and_parse(cancelled, method=METHOD_CANCEL)
    assert calendar.method == METHOD_CANCEL
    assert calendar.event.status == "CANCELLED"
    assert calendar.event.uid == scheduled.uid  # a CANCEL that does not match cancels nothing
    assert calendar.event.sequence == 3


def test_a_cancelled_window_cannot_be_shipped_as_a_request():
    # This is the failure that leaves a live booking for a window that is not happening.
    with pytest.raises(IcsValidationError, match="CANCELLED"):
        build_calendar(cancel_of(make_invite()), method=METHOD_REQUEST)


def test_a_confirmed_window_cannot_be_shipped_as_a_cancel():
    with pytest.raises(IcsValidationError, match="CANCEL"):
        build_calendar(make_invite(), method=METHOD_CANCEL)


def test_a_proposed_window_is_marked_tentative_rather_than_passed_through_verbatim():
    # PROPOSED and COMPLETED are maintenance_windows.status values, not VEVENT statuses.
    assert build_and_parse(make_invite(window_status="PROPOSED")).event.status == "TENTATIVE"
    assert build_and_parse(make_invite(window_status="COMPLETED")).event.status == "CONFIRMED"


def test_an_unsupported_itip_method_is_refused_rather_than_written_into_the_file():
    with pytest.raises(IcsValidationError, match="method"):
        build_calendar(make_invite(), method="REPLY")


# --------------------------------------------------------------------------- validation


@pytest.mark.parametrize(
    "overrides, fragment",
    [
        ({"ends_at": START_UTC}, "ends at or before"),
        ({"ends_at": START_UTC - timedelta(hours=1)}, "ends at or before"),
        ({"attendees": ()}, "attendees"),
        ({"organizer": "not-an-address"}, "ORGANIZER"),
        ({"attendees": ("fine@example.com", "broken")}, "ATTENDEE"),
        ({"rrule": "EVERY SO OFTEN"}, "RRULE"),
    ],
)
def test_the_validator_blocks_an_unshippable_window_before_anything_is_enqueued(overrides, fragment):
    # §7.5.5: "ICS invalid -> validator blocks enqueue".
    with pytest.raises(IcsValidationError, match=fragment):
        validate(make_invite(**overrides))


def test_a_newline_in_a_summary_is_refused_because_it_becomes_a_mail_header():
    # The same string reaches Subject: two functions later, where a newline is injection.
    with pytest.raises(IcsValidationError, match="SUMMARY"):
        validate(make_invite(summary="Maintenance\r\nBcc: attacker@example.com"))


def test_a_nul_byte_anywhere_in_the_text_fields_is_refused():
    for field_name in ("summary", "description", "location"):
        with pytest.raises(IcsValidationError):
            validate(make_invite(**{field_name: "ok\x00hidden"}))


# --------------------------------------------------------------------------- text encoding


def test_semicolons_commas_backslashes_and_newlines_survive_the_round_trip_unchanged():
    # RFC 5545 escaping is where a site name like "Nakuru, Rift Valley" quietly becomes two
    # properties. The assertion is on the parsed value, not on the escaped bytes.
    nasty = "Genset service; phase 2, 30% load\nContact: back\\office"
    text = build_calendar(make_invite(description=nasty), dtstamp=DTSTAMP)
    assert "\\;" in text and "\\," in text and "\\n" in text  # it really was escaped
    assert parse_calendar(text).event.description == nasty


def test_long_lines_are_folded_to_seventy_five_octets_and_unfold_to_the_original():
    long_summary = "Generator exercise at " + "Nairobi-West-Transmission-Site " * 8
    text = build_calendar(make_invite(summary=long_summary), dtstamp=DTSTAMP)
    for line in text.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75, line
    assert parse_calendar(text).event.summary == long_summary


def test_folding_counts_octets_not_characters_so_non_ascii_names_are_not_corrupted():
    # A fold that counts characters overruns the 75-octet limit, and a fold that splits a
    # multi-byte sequence corrupts it outright.
    summary = "Maintenance " + "àéîõü" * 30
    text = build_calendar(make_invite(summary=summary), dtstamp=DTSTAMP)
    for line in text.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75, line
    assert parse_calendar(text).event.summary == summary


def test_every_content_line_ends_with_crlf_as_rfc_5545_requires():
    text = build_calendar(make_invite(), dtstamp=DTSTAMP)
    assert text.endswith("\r\n")
    assert "\n" not in text.replace("\r\n", "")  # no bare LF anywhere


def test_the_parser_rejects_a_truncated_calendar_instead_of_returning_half_an_event():
    truncated = build_calendar(make_invite(), dtstamp=DTSTAMP).replace("END:VEVENT\r\nEND:VCALENDAR\r\n", "")
    with pytest.raises(IcsValidationError, match="unterminated"):
        parse_calendar(truncated)


def test_the_parser_reads_mailto_case_insensitively_as_rfc_3986_requires():
    text = build_calendar(make_invite(), dtstamp=DTSTAMP).replace("mailto:", "MAILTO:")
    assert parse_calendar(text).event.organizer == "noc@example.com"


# --------------------------------------------------------------------------- MIME / iMIP


def payload_and_message(invite: WindowInvite, method: str = METHOD_REQUEST):
    payload = invite_outbox_payload(invite, method=method, dtstamp=DTSTAMP)
    return payload, build_imip_message(payload)


def test_the_imip_message_carries_the_method_and_component_parameters_outlook_renders_from():
    _, msg = payload_and_message(make_invite())
    calendar_parts = [p for p in msg.walk() if p.get_content_type() == CALENDAR_MIME_TYPE]
    assert calendar_parts, "no text/calendar part in the message"
    for part in calendar_parts:
        # RFC 6047 §2.4. Without method= a client shows a file attachment, not an invite.
        assert part.get_param("method") == METHOD_REQUEST
        assert part.get_param("component") == "VEVENT"
        assert (part.get_param("charset") or "").lower() == "utf-8"


def test_the_message_is_multipart_with_a_plain_text_alternative_and_an_ics_attachment():
    _, msg = payload_and_message(make_invite())
    assert msg.get_content_type() == "multipart/mixed"
    types = [p.get_content_type() for p in msg.walk()]
    assert types.count("text/plain") == 1  # the fallback a non-iMIP client shows
    assert types.count(CALENDAR_MIME_TYPE) == 2  # inline alternative + .ics attachment
    attachments = [p for p in msg.walk() if p.get_content_disposition() == "attachment"]
    assert [p.get_filename() for p in attachments] == ["invite.ics"]


def test_the_from_header_matches_the_organizer_because_clients_drop_invites_that_do_not():
    payload, msg = payload_and_message(make_invite())
    assert msg["From"] == payload["organizer"] == "noc@example.com"
    assert msg["To"] == "fe.rift@example.com, egypro.ops@example.com"
    assert msg["Content-Class"] == "urn:content-classes:calendarmessage"


def test_the_calendar_part_of_the_sent_bytes_still_parses_back_to_the_same_event():
    # The real round trip: through the payload, through MIME serialisation, back out of a
    # parsed message, and into the ICS parser. Anything that mangles CRLF or encoding dies here.
    invite = make_invite()
    _, msg = payload_and_message(invite)
    parsed = message_from_bytes(msg.as_bytes(), policy=default_policy)
    part = next(p for p in parsed.walk() if p.get_content_type() == CALENDAR_MIME_TYPE)
    calendar = parse_calendar(part.get_content())
    assert calendar.method == METHOD_REQUEST
    assert calendar.event.uid == invite.uid
    assert calendar.event.dtstart_utc == START_UTC


def test_a_cancellation_email_says_cancelled_in_its_subject_and_in_its_method():
    payload, msg = payload_and_message(cancel_of(make_invite(sequence=1)), method=METHOD_CANCEL)
    assert payload["method"] == METHOD_CANCEL
    assert msg["Subject"].startswith("Cancelled:")
    assert "Do not attend" in payload["body"]
    for part in (p for p in msg.walk() if p.get_content_type() == CALENDAR_MIME_TYPE):
        assert part.get_param("method") == METHOD_CANCEL


def test_the_plain_text_body_labels_the_time_as_eat_and_prints_the_utc_instant_beside_it():
    # "02:00" with no zone at the top of an email is how an engineer arrives three hours out.
    payload, _ = payload_and_message(make_invite())
    assert "00:00 EAT" in payload["body"] and "05:00 EAT" in payload["body"]
    assert "2026-09-16T21:00:00Z" in payload["body"]
    assert "Thu 17 Sep 2026" in payload["body"]


def test_the_whole_message_stays_seven_bit_so_it_does_not_depend_on_an_8bitmime_relay():
    _, msg = payload_and_message(make_invite())
    subject = msg["Subject"]
    assert subject.isascii(), subject
    assert msg.as_bytes().isascii()


def test_building_a_message_from_an_unknown_payload_version_is_refused_rather_than_guessed():
    payload, _ = payload_and_message(make_invite())
    with pytest.raises(IcsValidationError, match="payload_version"):
        build_imip_message({**payload, "payload_version": PAYLOAD_VERSION + 1})


def test_a_payload_with_no_recipients_or_no_calendar_text_is_refused():
    payload, _ = payload_and_message(make_invite())
    with pytest.raises(IcsValidationError, match="recipients"):
        build_imip_message({**payload, "to": []})
    with pytest.raises(IcsValidationError, match="calendar text"):
        build_imip_message({**payload, "ics": "   "})


# --------------------------------------------------------------------------- the outbox seam


def test_the_outbox_payload_is_self_contained_so_the_dispatcher_never_re_reads_the_database():
    # The dispatcher runs after commit with no transaction open (orchestrator/outbox.py).
    payload = invite_outbox_payload(make_invite(), dtstamp=DTSTAMP)
    assert payload["payload_version"] == PAYLOAD_VERSION
    assert set(payload) >= {
        "operator_id", "window_id", "uid", "sequence", "method",
        "subject", "body", "organizer", "to", "ics", "filename", "content_type",
    }
    assert payload["ics"].startswith("BEGIN:VCALENDAR")
    assert payload["operator_id"] == "safaricom"
    assert ICS_OUTBOX_KIND == "ICS_INVITE"  # the kind orchestrator/outbox.py already gates


def test_the_idempotency_key_changes_with_the_sequence_so_a_reschedule_is_not_deduplicated():
    # enqueue() is INSERT OR IGNORE on this key. Leaving the sequence out would turn every
    # update after the first into a silent no-op — the same failure as forgetting SEQUENCE.
    invite = make_invite()
    assert invite_idempotency_key(invite) == "ics:safaricom:MW-2026-0007@noc.example:REQUEST:0"
    assert invite_idempotency_key(invite) == invite_idempotency_key(make_invite())  # rerun: same row
    assert invite_idempotency_key(invite.with_sequence(1)) != invite_idempotency_key(invite)
    assert invite_idempotency_key(invite, METHOD_CANCEL) != invite_idempotency_key(invite, METHOD_REQUEST)


# ------------------------------------------------------- the seam to the maintenance lane


def test_a_window_row_is_read_by_attribute_so_this_module_needs_none_of_that_lane_s_types():
    # SimpleNamespace stands in for the ORM row the maintenance lane will pass. If this test
    # ever needs an import from that lane, the seam has been broken.
    row = SimpleNamespace(
        id="MW-2026-0009",
        uid="MW-2026-0009@noc.example",
        operator_id="safaricom",
        starts_at=START_UTC,
        ends_at=END_UTC,
        scope_ref="NRB-0421",
        organizer="noc@example.com",
        sequence=2,
        status="SCHEDULED",
        rrule=None,
    )
    invite = invite_from_window(row, attendees=["fe.rift@example.com"])
    assert invite.uid == "MW-2026-0009@noc.example"
    assert invite.sequence == 2 and invite.location == "NRB-0421"
    assert invite.attendees == ("fe.rift@example.com",)
    assert build_and_parse(invite, previous_sequence=1).event.uid == invite.uid


def test_a_window_row_without_a_stored_uid_gets_the_stable_one_rather_than_a_random_one():
    row = SimpleNamespace(
        id="MW-2026-0010", uid=None, operator_id="safaricom", starts_at=START_UTC, ends_at=END_UTC,
        scope_ref="NRB-0421", organizer="noc@example.com", sequence=0, status="SCHEDULED", rrule=None,
    )
    assert invite_from_window(row, attendees=["fe@example.com"]).uid == stable_uid("MW-2026-0010")


def test_the_maintenance_model_import_is_guarded_so_a_mid_flight_sibling_cannot_break_startup():
    # Mirrors services/evidence.py's ClockEventRow guard. Whether that lane has landed its
    # module yet or not, asking must not raise.
    assert ics._window_row() is None or isinstance(ics._window_row(), type)


# --------------------------------------------------------------------------- the flag


def test_invites_are_disabled_unless_maintenance_enabled_is_explicitly_true(monkeypatch):
    monkeypatch.delenv("MAINTENANCE_ENABLED", raising=False)
    assert invites_enabled() is False
    for value in ("false", "0", "no", "off", "", "maybe"):
        monkeypatch.setenv("MAINTENANCE_ENABLED", value)
        assert invites_enabled() is False, value
    for value in ("true", "TRUE", "1", "yes", "on"):
        monkeypatch.setenv("MAINTENANCE_ENABLED", value)
        assert invites_enabled() is True, value


# --------------------------------------------------------------------------- external parser


@pytest.mark.skipif(not _HAS_ICALENDAR, reason="icalendar is an optional extra and is not installed")
def test_a_real_icalendar_parser_agrees_with_this_module_when_the_extra_is_installed():
    # The point of the exercise: a writer round-tripping through its own parser proves only
    # that the two share assumptions. When the `calendar` extra is present, check the output
    # against the library §7.5.4 names.
    from icalendar import Calendar  # noqa: PLC0415 - optional dependency, imported in the test

    invite = make_invite(rrule="FREQ=MONTHLY;BYMONTHDAY=1")
    cal = Calendar.from_ical(build_calendar(invite, dtstamp=DTSTAMP))
    assert str(cal.get("METHOD")) == METHOD_REQUEST
    event = next(component for component in cal.walk() if component.name == "VEVENT")
    assert str(event["UID"]) == invite.uid
    assert int(event["SEQUENCE"]) == 0
    assert str(event["SUMMARY"]) == invite.summary
    started = event.decoded("DTSTART")
    assert started.utcoffset() == timedelta(hours=3)  # EAT, stated in the file
    assert started.replace(tzinfo=None) == datetime(2026, 9, 17, 0, 0)  # 00:00 EAT
