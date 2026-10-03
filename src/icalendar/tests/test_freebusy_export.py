from __future__ import annotations

from datetime import datetime, timezone

import pytest

from icalendar import Calendar
from icalendar.error import InvalidCalendar
from icalendar.freebusy import export_busy_time_calendar


SECRET = b"caller-secret"


def sample_calendar() -> Calendar:
    return Calendar.from_ical(
        """BEGIN:VCALENDAR
VERSION:2.0
PRODID:private-client
X-CLIENT-DECOY:decoy-calendar
BEGIN:VTIMEZONE
TZID:/Acme/Customer-Secret-Project
X-LIC-LOCATION:Acme Customer Secret Project
BEGIN:STANDARD
TZNAME:Acme Standard Secret
DTSTART:19701101T020000
TZOFFSETFROM:-0400
TZOFFSETTO:-0500
RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU
END:STANDARD
BEGIN:DAYLIGHT
TZNAME:Acme Daylight Secret
DTSTART:19700308T020000
TZOFFSETFROM:-0500
TZOFFSETTO:-0400
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU
END:DAYLIGHT
END:VTIMEZONE
BEGIN:VEVENT
UID:secret-series-uid
DTSTAMP:20240101T000000Z
DTSTART;TZID=/Acme/Customer-Secret-Project:20240102T090000
DTEND;TZID=/Acme/Customer-Secret-Project:20240102T103000
RRULE:FREQ=DAILY;UNTIL=20240104T140000Z
RDATE;VALUE=PERIOD:20240110T150000Z/20240110T160000Z
EXDATE;TZID=/Acme/Customer-Secret-Project:20240103T090000
SEQUENCE:1
STATUS:CONFIRMED
TRANSP:OPAQUE
SUMMARY:Secret acquisition meeting
DESCRIPTION:Secret negotiation details
LOCATION:Secret rooftop address
GEO:37.386013;-122.082932
URL:https://calendar.example.test/secret
ATTACH;FMTTYPE=text/plain:https://files.example.test/secret.txt
ORGANIZER;CN=Secret Organizer:mailto:boss@example.test
ATTENDEE;CN=Secret Attendee:mailto:employee@example.test
COMMENT:Secret comment
CATEGORIES:Secret-Category
X-DECOY-PROP:secret event metadata
RELATED-TO:secret-parent-uid
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:Secret embedded alarm
TRIGGER:-PT15M
X-ALARM-DECOY:secret alarm metadata
END:VALARM
END:VEVENT
BEGIN:VEVENT
UID:secret-series-uid
DTSTAMP:20240101T000000Z
RECURRENCE-ID;TZID=/Acme/Customer-Secret-Project:20240103T090000
DTSTART;TZID=/Acme/Customer-Secret-Project:20240103T120000
DTEND;TZID=/Acme/Customer-Secret-Project:20240103T130000
SEQUENCE:2
STATUS:CONFIRMED
SUMMARY:Secret override title
DESCRIPTION:Secret override description
END:VEVENT
BEGIN:VEVENT
UID:secret-cancelled-uid
DTSTAMP:20240101T000000Z
DTSTART:20240105T140000Z
DTEND:20240105T150000Z
SEQUENCE:3
STATUS:CANCELLED
SUMMARY:Secret cancellation
DESCRIPTION:Secret cancellation reason
LOCATION:Secret cancellation address
ATTACH:https://files.example.test/cancelled-secret.bin
END:VEVENT
BEGIN:VTODO
UID:secret-todo-uid
SUMMARY:Secret task
END:VTODO
BEGIN:X-SECRET
CONTENT:secret custom component
END:X-SECRET
END:VCALENDAR
        """
    )


def test_export_uses_whitelist_and_removes_sensitive_content():
    source = sample_calendar()

    exported = export_busy_time_calendar(source, SECRET)
    raw = exported.to_ical().lower()

    assert exported.walk("VEVENT")
    assert not exported.walk("VTODO")
    assert not exported.walk("VALARM")
    assert not exported.walk("X-SECRET")
    for forbidden in (
        b"secret",
        b"acme",
        b"customer",
        b"rooftop",
        b"meeting",
        b"negotiation",
        b"alarm",
        b"attendee",
        b"organizer",
        b"attachment",
        b"x-",
        b"location",
        b"description",
        b"summary",
        b"comment",
        b"category",
        b"related-to",
        b"geo",
        b"url",
    ):
        assert forbidden not in raw


def test_export_preserves_timing_and_recurrence_relations():
    source = sample_calendar()

    exported = export_busy_time_calendar(source, SECRET)
    events = exported.events
    master, exception, cancelled = sorted(
        events, key=lambda event: event["DTSTART"].dt
    )

    assert len({str(event["UID"]) for event in (master, exception)}) == 1
    assert str(cancelled["UID"]) != str(master["UID"])
    assert str(exception["RECURRENCE-ID"].params["TZID"]).startswith("TZ-")
    assert exception["SEQUENCE"] == 2
    assert cancelled["STATUS"] == "CANCELLED"

    assert master["DTSTART"].dt == source.events[0]["DTSTART"].dt
    assert master["DTEND"].dt == source.events[0]["DTEND"].dt
    assert master["RRULE"]["UNTIL"][0] == datetime(
        2024, 1, 4, 14, tzinfo=timezone.utc
    )
    assert exception["RECURRENCE-ID"].dt == source.events[1]["RECURRENCE-ID"].dt
    assert exception["DTSTART"].dt == source.events[1]["DTSTART"].dt
    assert cancelled["DTSTART"].dt == datetime(2024, 1, 5, 14, tzinfo=timezone.utc)


def test_export_includes_only_used_and_sanitized_timezones():
    source = sample_calendar()
    source.add("X-WR-TIMEZONE", "Unused/Secret/Timezone")

    exported = export_busy_time_calendar(source, SECRET)

    assert len(exported.timezones) == 1
    timezone_component = exported.timezones[0]
    tzid = str(timezone_component["TZID"])
    assert tzid.startswith("TZ-")
    assert len(tzid) == 35
    assert not any("TZNAME" in child for child in timezone_component.subcomponents)
    for event in exported.events:
        assert event["DTSTART"].params.get("TZID") in (None, tzid)
    assert b"Unused" not in exported.to_ical()


def test_export_is_deterministic_and_does_not_share_or_leak_secret():
    source = sample_calendar()

    first = export_busy_time_calendar(source, SECRET)
    second = export_busy_time_calendar(source, SECRET)

    assert first.to_ical() == second.to_ical()
    assert SECRET not in first.to_ical()
    assert first.events[0] is not source.events[0]
    first.events[0].add("X-MUTATION", "mutated copy")

    exported_again = export_busy_time_calendar(source, SECRET)
    assert b"X-MUTATION" not in exported_again.to_ical()


def test_floating_time_policy_is_explicit():
    source = Calendar.from_ical(
        """BEGIN:VCALENDAR
VERSION:2.0
PRODID:x
BEGIN:VEVENT
UID:floating-uid
DTSTAMP:20240101T000000Z
DTSTART:20240102T090000
DTEND:20240102T100000
SUMMARY:Secret floating
END:VEVENT
END:VCALENDAR
"""
    )

    with pytest.raises(InvalidCalendar, match="Floating DATE-TIME"):
        export_busy_time_calendar(source, SECRET)

    exported = export_busy_time_calendar(
        source, SECRET, floating_time="assume-utc"
    )
    assert exported.events[0]["DTSTART"].dt == datetime(
        2024, 1, 2, 9, tzinfo=timezone.utc
    )


def test_relationship_properties_are_removed_instead_of_left_verbatim():
    source = Calendar.from_ical(
        """BEGIN:VCALENDAR
VERSION:2.0
PRODID:x
BEGIN:VEVENT
UID:child-uid
DTSTAMP:20240101T000000Z
DTSTART:20240102T090000Z
DTEND:20240102T100000Z
RELATED-TO:secret-parent-uid
LINK:https://example.test/secret-link
CONCEPT:https://example.test/secret-concept
REFID:secret-refid
SUMMARY:Secret linked event
END:VEVENT
END:VCALENDAR
"""
    )

    exported = export_busy_time_calendar(source, SECRET)
    raw = exported.to_ical()

    assert b"RELATED-TO" not in raw
    assert b"LINK" not in raw
    assert b"CONCEPT" not in raw
    assert b"REFID" not in raw
    assert b"secret-parent-uid" not in raw


def test_unknown_rule_parts_and_property_parameters_are_removed():
    source = Calendar.from_ical(
        """BEGIN:VCALENDAR
VERSION:2.0
PRODID:x
BEGIN:VEVENT
UID:rule-uid
DTSTAMP:20240101T000000Z
DTSTART;X-SECRET-PARAM=secret:20240102T090000Z
DTEND;X-SECRET-PARAM=secret:20240102T100000Z
RRULE:FREQ=DAILY;COUNT=3;X-SECRET-RULE=secret
SUMMARY:Secret rule event
END:VEVENT
END:VCALENDAR
"""
    )

    exported = export_busy_time_calendar(source, SECRET, floating_time="assume-utc")
    raw = exported.to_ical()

    assert b"X-" not in raw
    assert b"X-SECRET-RULE" not in raw
    assert b"X-SECRET-PARAM" not in raw
    assert b"secret" not in raw.lower()
    assert b"RRULE:FREQ=DAILY;COUNT=3" in raw


def test_implicit_timezone_names_do_not_remain_in_result_objects():
    source = Calendar.from_ical(
        """BEGIN:VCALENDAR
VERSION:2.0
PRODID:x
BEGIN:VEVENT
UID:implicit-uid
DTSTAMP:20240101T000000Z
DTSTART:20240102T090000Z
DTEND:20240102T100000Z
RRULE:FREQ=DAILY;UNTIL=20240103T150000Z
SUMMARY:Secret implicit timezone
END:VEVENT
END:VCALENDAR
"""
    )

    exported = export_busy_time_calendar(source, SECRET)
    event = exported.events[0]

    assert event["DTSTART"].dt.tzinfo is timezone.utc
    assert event["RRULE"]["UNTIL"][0].tzname() == "UTC"


def test_dtstamp_timezone_is_used_for_conversion_but_not_exported():
    source = Calendar.from_ical(
        """BEGIN:VCALENDAR
VERSION:2.0
PRODID:x
BEGIN:VTIMEZONE
TZID:/Secret/Stamp-Zone
BEGIN:STANDARD
DTSTART:19700101T000000
TZOFFSETFROM:-0400
TZOFFSETTO:-0400
TZNAME:Secret Stamp Name
END:STANDARD
END:VTIMEZONE
BEGIN:VEVENT
UID:stamp-uid
DTSTAMP;TZID=/Secret/Stamp-Zone:20240101T050000
DTSTART:20240102T090000Z
DTEND:20240102T100000Z
SUMMARY:Secret stamp event
END:VEVENT
END:VCALENDAR
"""
    )

    exported = export_busy_time_calendar(source, SECRET)

    assert not exported.timezones
    assert exported.events[0]["DTSTAMP"].dt == datetime(
        2024, 1, 1, 9, tzinfo=timezone.utc
    )
    assert b"Stamp" not in exported.to_ical()
