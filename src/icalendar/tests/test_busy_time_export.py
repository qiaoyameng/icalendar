from datetime import datetime, timezone

import pytest

from icalendar import Calendar, FloatingTimePolicy, export_busy_time

SECRET_KEY = "0123456789abcdef0123456789abcdef"

PRIVATE_CALENDAR = b"""
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Customer//Secret Calendar//EN
X-WR-CALNAME:Secret Customer
BEGIN:VTIMEZONE
TZID:Customer/Secret City
X-CUSTOMER:Acme Secret Project
BEGIN:STANDARD
DTSTART:19700101T030000
TZOFFSETFROM:+0300
TZOFFSETTO:+0200
TZNAME:SECRET-STD
RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:19700101T020000
TZOFFSETFROM:+0200
TZOFFSETTO:+0300
TZNAME:SECRET-DST
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU
END:DAYLIGHT
END:VTIMEZONE
BEGIN:VTIMEZONE
TZID:Unused/Secret
BEGIN:STANDARD
DTSTART:19700101T000000
TZOFFSETFROM:+0000
TZOFFSETTO:+0000
END:STANDARD
END:VTIMEZONE
BEGIN:VEVENT
UID:secret-series@customer.example
DTSTAMP:20260101T000000Z
DTSTART;TZID=Customer/Secret City:20260301T100000
DTEND;TZID=Customer/Secret City:20260301T110000
RRULE:FREQ=WEEKLY;COUNT=3
SUMMARY:Merger planning
DESCRIPTION:Confidential acquisition notes
LOCATION:Secret rooftop office
GEO:1.23;4.56
ATTACH;FMTTYPE=text/plain:secret-attachment.txt
URL:https://calendar.example/private/meeting
X-CUSTOMER:decoy event property
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:Secret embedded alarm
TRIGGER:-PT15M
X-CUSTOMER:decoy alarm property
END:VALARM
END:VEVENT
BEGIN:VEVENT
UID:secret-series@customer.example
DTSTAMP:20260102T000000Z
RECURRENCE-ID;TZID=Customer/Secret City:20260308T100000
DTSTART;TZID=Customer/Secret City:20260308T120000
DTEND;TZID=Customer/Secret City:20260308T130000
SEQUENCE:2
SUMMARY:Rescheduled merger planning
LOCATION:Secret relocation
END:VEVENT
BEGIN:VEVENT
UID:secret-cancelled@customer.example
DTSTAMP:20260103T000000Z
DTSTART;TZID=Customer/Secret City:20260315T100000
DURATION:PT30M
STATUS:CANCELLED
SEQUENCE:3
SUMMARY:Cancelled private meeting
END:VEVENT
BEGIN:VEVENT
UID:related-child@customer.example
DTSTAMP:20260104T000000Z
DTSTART:20260316T100000Z
DURATION:PT1H
RELATED-TO;RELTYPE=PARENT:secret-series@customer.example
RELATED-TO;VALUE=URI:https://customer.example/private/uid
SUMMARY:Related private event
X-SECRET:decoy relation
END:VEVENT
END:VCALENDAR
"""


def test_busy_time_export_is_allowlist_only():
    source = Calendar.from_ical(PRIVATE_CALENDAR)

    exported = export_busy_time(source, secret_key=SECRET_KEY)
    raw = exported.to_ical().decode()

    forbidden = [
        "Merger",
        "Confidential",
        "Secret",
        "Acme",
        "attachment",
        "rooftop",
        "calendar.example/private",
        "VALARM",
        "ATTACH",
        "DESCRIPTION",
        "LOCATION",
        "GEO",
        "URL:",
        "X-",
        "Unused/",
        "related-child@customer.example",
    ]
    for token in forbidden:
        assert token not in raw

    component_names = {component.name for component in exported.walk()}
    assert component_names == {"VCALENDAR", "VTIMEZONE", "STANDARD", "DAYLIGHT", "VEVENT"}
    assert exported.timezones[0].tz_name.startswith("busy-tz-")
    assert len(exported.timezones) == 1

    allowed = {
        "VERSION",
        "PRODID",
        "CALSCALE",
        "DTSTAMP",
        "UID",
        "DTSTART",
        "DTEND",
        "DURATION",
        "RRULE",
        "RECURRENCE-ID",
        "SEQUENCE",
        "STATUS",
        "RELATED-TO",
    }
    for component in exported.walk():
        assert set(component).issubset(allowed | {"TZID", "TZOFFSETFROM", "TZOFFSETTO"})


def test_times_identifiers_and_modification_structure_are_preserved():
    source = Calendar.from_ical(PRIVATE_CALENDAR)

    exported = export_busy_time(source, secret_key=SECRET_KEY)
    events = exported.events
    source_events = source.events
    new_tzid = exported.timezones[0].tz_name
    series_uid = events[0].uid

    assert events[0].uid == events[1].uid == series_uid
    assert series_uid.startswith("busy-")
    assert "secret-series" not in series_uid
    assert events[2].uid != series_uid
    assert events[2].status == "CANCELLED"
    assert events[1].sequence == 2

    assert events[0].start.tzinfo.utcoffset(events[0].start) == source_events[0].start.utcoffset()
    assert events[0].start.isoformat() == source_events[0].start.isoformat()
    assert events[0].end.isoformat() == source_events[0].end.isoformat()
    recurrence_id = events[1].decoded("RECURRENCE-ID")
    source_recurrence_id = source_events[1].decoded("RECURRENCE-ID")
    assert recurrence_id.isoformat() == source_recurrence_id.isoformat()
    assert recurrence_id.tzinfo is not None
    assert events[0].rrules[0].to_ical() == source_events[0].rrules[0].to_ical()

    for event in events:
        for name in ("DTSTART", "DTEND", "DURATION", "RECURRENCE-ID"):
            if name in event:
                value = event[name]
                if hasattr(value, "params"):
                    tzid = value.params.get("TZID")
                    assert tzid is None or tzid == new_tzid

    relation = events[3].related_to[0]
    assert relation == series_uid
    assert relation.params["RELTYPE"] == "PARENT"
    assert relation.params["VALUE"] == "UID"
    assert len(events[3].related_to) == 1
    assert b"RELTYPE=PARENT;VALUE=UID:" + series_uid.encode() in exported.to_ical()


def test_export_is_repeatable_and_does_not_alias_input_or_key():
    source = Calendar.from_ical(PRIVATE_CALENDAR)
    original_raw = source.to_ical()

    first = export_busy_time(source, secret_key=SECRET_KEY)
    second = export_busy_time(Calendar.from_ical(PRIVATE_CALENDAR), secret_key=SECRET_KEY)
    third = export_busy_time(source, "another-secret-key-that-is-32-bytes!")

    assert first == second
    assert first.to_ical() == second.to_ical()
    assert first.events[0].uid != third.events[0].uid
    assert source.to_ical() == original_raw

    first_event = first.events[0]
    first_event.add("SUMMARY", "mutation after export")
    first_event.uid = "changed"
    assert export_busy_time(source, secret_key=SECRET_KEY).events[0].uid != "changed"
    sanitized_after_mutation = export_busy_time(source, secret_key=SECRET_KEY).to_ical().decode()
    assert "mutation after export" not in sanitized_after_mutation
    assert SECRET_KEY not in first.to_ical().decode()


def test_floating_time_policy_is_explicit():
    source = Calendar.from_ical(
        b"BEGIN:VCALENDAR\r\n"
        b"VERSION:2.0\r\n"
        b"BEGIN:VEVENT\r\n"
        b"UID:floating@example\r\n"
        b"DTSTAMP:20260101T000000Z\r\n"
        b"DTSTART:20260301T100000\r\n"
        b"DTEND:20260301T110000\r\n"
        b"END:VEVENT\r\n"
        b"END:VCALENDAR\r\n"
    )

    with pytest.raises(ValueError, match="Floating DATE-TIME"):
        export_busy_time(source, secret_key=SECRET_KEY)

    kept = export_busy_time(
        source,
        secret_key=SECRET_KEY,
        floating_time_policy=FloatingTimePolicy.KEEP,
    )
    assert kept.events[0].start.tzinfo is None

    as_utc = export_busy_time(source, secret_key=SECRET_KEY, floating_time_policy="assume-utc")
    assert as_utc.events[0].start == datetime(2026, 3, 1, 10, tzinfo=timezone.utc)
    assert b"DTSTART:20260301T100000Z" in as_utc.to_ical()


def test_recurrence_date_lists_and_relationship_scope_are_sanitized():
    source = Calendar.from_ical(
        b"BEGIN:VCALENDAR\r\n"
        b"VERSION:2.0\r\n"
        b"BEGIN:VTIMEZONE\r\n"
        b"TZID:Private/Place\r\n"
        b"BEGIN:STANDARD\r\n"
        b"DTSTART:19700101T000000\r\n"
        b"TZOFFSETFROM:+0100\r\n"
        b"TZOFFSETTO:+0100\r\n"
        b"END:STANDARD\r\n"
        b"END:VTIMEZONE\r\n"
        b"BEGIN:VEVENT\r\n"
        b"UID:inside@private\r\n"
        b"DTSTAMP:20260101T000000Z\r\n"
        b"DTSTART;TZID=Private/Place:20260401T100000\r\n"
        b"DTEND;TZID=Private/Place:20260401T110000\r\n"
        b"RDATE;TZID=Private/Place;X-SECRET=redacted:20260403T100000\r\n"
        b"EXDATE;TZID=Private/Place;X-SECRET=redacted:20260404T100000\r\n"
        b"END:VEVENT\r\n"
        b"BEGIN:VEVENT\r\n"
        b"UID:outside-reference@private\r\n"
        b"DTSTAMP:20260101T000000Z\r\n"
        b"DTSTART:20260402T100000Z\r\n"
        b"DURATION:PT1H\r\n"
        b"RELATED-TO;X-SECRET=redacted:external@other\r\n"
        b"RELATED-TO;VALUE=URI:https://private.example/external\r\n"
        b"RELATED-TO;RELTYPE=SIBLING:inside@private\r\n"
        b"END:VEVENT\r\n"
        b"END:VCALENDAR\r\n"
    )

    exported = export_busy_time(source, secret_key=SECRET_KEY)
    raw = exported.to_ical().decode()
    events = exported.events

    assert "Private/Place" not in raw
    assert "redacted" not in raw
    assert "external@other" not in raw
    assert "private.example" not in raw
    assert "X-SECRET" not in raw

    assert len(events[1].related_to) == 1
    assert events[1].related_to[0] == events[0].uid
    assert events[0]["RDATE"].dts[0].dt.isoformat() == (
        source.events[0]["RDATE"].dts[0].dt.isoformat()
    )
    assert events[0]["EXDATE"].dts[0].dt.isoformat() == (
        source.events[0]["EXDATE"].dts[0].dt.isoformat()
    )

    reparsed = Calendar.from_ical(raw.encode())
    assert reparsed.events[0]["RDATE"].dts[0].dt.isoformat() == (
        events[0]["RDATE"].dts[0].dt.isoformat()
    )
    assert reparsed.events[0]["EXDATE"].dts[0].dt.isoformat() == (
        events[0]["EXDATE"].dts[0].dt.isoformat()
    )
    assert reparsed.timezones[0].tz_name == exported.timezones[0].tz_name


def test_missing_timezone_definition_is_rejected():
    source = Calendar.from_ical(
        b"BEGIN:VCALENDAR\r\n"
        b"VERSION:2.0\r\n"
        b"BEGIN:VEVENT\r\n"
        b"UID:missing-zone@private\r\n"
        b"DTSTAMP:20260101T000000Z\r\n"
        b"DTSTART;TZID=Missing/Secret:20260401T100000\r\n"
        b"DTEND;TZID=Missing/Secret:20260401T110000\r\n"
        b"END:VEVENT\r\n"
        b"END:VCALENDAR\r\n"
    )

    with pytest.raises(ValueError, match="Missing VTIMEZONE"):
        export_busy_time(source, secret_key=SECRET_KEY)


def test_secret_key_must_have_at_least_32_bytes():
    source = Calendar.from_ical(
        b"BEGIN:VCALENDAR\r\n"
        b"VERSION:2.0\r\n"
        b"END:VCALENDAR\r\n"
    )

    with pytest.raises(ValueError, match="at least 32 bytes"):
        export_busy_time(source, secret_key=b"short")
