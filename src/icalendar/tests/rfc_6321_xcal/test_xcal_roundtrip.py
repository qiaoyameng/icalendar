"""Round-trip tests for :rfc:`6321` xCal conversion."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from io import BytesIO

import pytest

from icalendar import (
    Calendar,
    Component,
    Event,
    PARTSTAT,
    ROLE,
    Timezone,
    TimezoneDaylight,
    TimezoneStandard,
    vBinary,
    vCalAddress,
    vPeriod,
    vRecur,
)
from icalendar.xcal import XCalParsingError


def _calendar_with_all_key_values() -> Calendar:
    """Create a calendar covering timezone, recurrence, period and binary data."""
    calendar = Calendar()
    calendar.add("version", "2.0")
    calendar.add("prodid", "-//icalendar//xCal test//EN")

    timezone_component = Timezone()
    timezone_component.add("tzid", "America/New_York")
    standard = TimezoneStandard()
    standard.add("dtstart", datetime(1970, 11, 1, 2, 0))
    standard.add("rrule", vRecur.from_ical("FREQ=YEARLY;BYDAY=1SU;BYMONTH=11"))
    standard.add("tzoffsetfrom", timedelta(hours=-4))
    standard.add("tzoffsetto", timedelta(hours=-5))
    daylight = TimezoneDaylight()
    daylight.add("dtstart", datetime(1970, 3, 8, 2, 0))
    daylight.add("rrule", vRecur.from_ical("FREQ=YEARLY;BYDAY=2SU;BYMONTH=3"))
    daylight.add("tzoffsetfrom", timedelta(hours=-5))
    daylight.add("tzoffsetto", timedelta(hours=-4))
    timezone_component.add_component(standard)
    timezone_component.add_component(daylight)
    calendar.add_component(timezone_component)

    event = Event()
    event.add("uid", "xcal-roundtrip@example.com")
    event.add("dtstamp", datetime(2024, 1, 1, tzinfo=timezone.utc))
    event.add(
        "dtstart",
        datetime(2024, 3, 10, 9, 0),
        parameters={"tzid": "America/New_York"},
    )
    event.add(
        "dtend",
        datetime(2024, 3, 10, 10, 0),
        parameters={"tzid": "America/New_York"},
    )
    event.add("summary", "Recurring & special <chars>")
    event.add("categories", ["Work", "Team, Project"])
    event.add(
        "rrule",
        vRecur.from_ical(
            "FREQ=MONTHLY;COUNT=3;INTERVAL=2;BYDAY=-1FR;BYHOUR=9,10;BYMONTH=5"
        ),
    )
    event.add(
        "exdate",
        [datetime(2024, 5, 31, 9, 0)],
        parameters={"tzid": "America/New_York"},
    )
    event.add(
        "rdate",
        vPeriod((datetime(2024, 6, 1, 13, tzinfo=timezone.utc), timedelta(minutes=90))),
    )
    event.add(
        "rdate",
        vPeriod(
            (
                datetime(2024, 7, 1, 13, tzinfo=timezone.utc),
                datetime(2024, 7, 1, 15, tzinfo=timezone.utc),
            )
        ),
    )
    attendee = vCalAddress.new(
        "attendee@example.com",
        cn="Jane; Doe",
        partstat=PARTSTAT.ACCEPTED,
        rsvp=True,
        role=ROLE.REQ_PARTICIPANT,
    )
    event.add("attendee", attendee)
    event.add(
        "attach",
        vBinary(b"\x00\x01binary&<>", params={"fmttype": "application/octet-stream"}),
    )
    event.add(
        "attach",
        "https://example.com/file.pdf",
        parameters={"fmttype": "application/pdf"},
    )
    event.add(
        "x-foo",
        "bar; <x>",
        parameters={"x-param": "a,b", "language": "en"},
    )
    event.add("x-weird", r"raw\,value", parameters={"value": "X-WEIRD"})
    calendar.add_component(event)
    return calendar


def test_icalendar_to_xcal_and_back_preserves_semantics():
    """ICS -> xCal -> Component preserves the requested semantic content."""
    original = _calendar_with_all_key_values()
    xcal = original.to_xcal()
    restored = Calendar.from_xcal(BytesIO(xcal).getvalue())

    original_event = original.events[0]
    restored_event = restored.events[0]
    assert restored_event.uid == original_event.uid
    assert restored_event.start.utcoffset() == timedelta(hours=-4)
    assert restored_event.start.replace(tzinfo=None) == original_event.start
    assert restored_event.end.replace(tzinfo=None) == original_event.end
    assert restored_event.summary == original_event.summary
    assert list(restored_event.categories) == list(original_event.categories)
    assert restored_event.rrules == original_event.rrules
    assert restored_event.exdates[0].replace(tzinfo=None) == original_event.exdates[0]
    def rdate_periods(event):
        values = event["rdate"]
        result = []
        for value in values:
            result.extend(item.dt for item in value.dts) if hasattr(value, "dts") else result.append(value.dt)
        return result
    original_periods = rdate_periods(original_event)
    restored_periods = rdate_periods(restored_event)
    for original_period, restored_period in zip(original_periods, restored_periods, strict=True):
        assert restored_period == original_period
    assert restored_event.attendees[0].RSVP is True
    assert restored_event.attendees[0].params["PARTSTAT"] == "ACCEPTED"
    assert restored_event.attendees[0].params["ROLE"] == "REQ-PARTICIPANT"
    assert restored_event.get("attach")[0].bytes == b"\x00\x01binary&<>"
    assert restored_event.get("x-foo").params["x-param"] == "a,b"
    assert restored_event.get("x-weird").VALUE == "X-WEIRD"
    assert str(restored_event.get("x-weird")) == r"raw\,value"

    timezone_component = restored.timezones[0]
    assert str(timezone_component["tzid"]) == "America/New_York"
    standard = next(c for c in timezone_component.subcomponents if c.name == "STANDARD")
    daylight = next(c for c in timezone_component.subcomponents if c.name == "DAYLIGHT")
    assert standard["tzoffsetto"].td == timedelta(hours=-5)
    assert daylight["tzoffsetfrom"].td == timedelta(hours=-5)
    assert daylight["rrule"]["freq"] == ["YEARLY"] or daylight["rrule"]["freq"] == "YEARLY"
    assert daylight["rrule"]["byday"] == ["2SU"]


def test_xcal_uses_namespaced_structured_nodes():
    """Period, recur and offsets use xCal structure instead of one str() node."""
    xcal = _calendar_with_all_key_values().to_xcal().decode()
    assert 'xmlns="urn:ietf:params:xml:ns:icalendar-2.0"' in xcal
    assert "<recur><freq><text>MONTHLY</text>" in xcal
    assert "<byday><text>-1FR</text>" in xcal
    assert "<byhour><integer>9</integer><integer>10</integer>" in xcal
    assert "<period><start>2024-06-01T13:00:00Z</start><duration>PT1H30M</duration>" in xcal
    assert "<period><start>2024-07-01T13:00:00Z</start><end>2024-07-01T15:00:00Z</end>" in xcal
    assert "<utc-offset>-05:00</utc-offset>" in xcal
    assert "<binary>AAFiaW5hcnkmPD4=</binary>" in xcal
    assert r"\n" not in xcal
    assert "&lt;chars&gt;" in xcal


def test_unknown_component_round_trips():
    """Unknown components remain components with their original name."""
    xcal = """
    <icalendar xmlns="urn:ietf:params:xml:ns:icalendar-2.0">
      <vcalendar><properties><prodid><text>x</text></prodid></properties>
      <components><x-xcal-test-vendor><properties><x-thing><unknown>value</unknown></x-thing>
      </properties><components/></x-xcal-test-vendor></components></vcalendar>
    </icalendar>
    """
    calendar = Calendar.from_xcal(xcal)
    component = calendar.subcomponents[0]
    assert component.name == "X-XCAL-TEST-VENDOR"
    assert str(component["x-thing"]) == "value"
    assert Component.from_xcal(component.to_xcal()) == component


def test_extension_value_type_is_preserved():
    """An unrecognized x-name value type remains a verbatim extension type."""
    xcal = """
    <icalendar xmlns="urn:ietf:params:xml:ns:icalendar-2.0">
      <vcalendar><properties><prodid><text>x</text></prodid></properties>
      <components><vevent><properties>
        <x-thing><x-weird>raw\\,value</x-weird></x-thing>
      </properties><components /></vevent></components></vcalendar>
    </icalendar>
    """
    event = Calendar.from_xcal(xcal).events[0]
    prop = event["x-thing"]
    assert prop.VALUE == "X-WEIRD"
    assert str(prop) == r"raw\,value"
    assert b"<x-weird>raw\\,value</x-weird>" in event.to_xcal()


def test_unknown_structured_extension_has_an_explicit_policy_error():
    """Only scalar extension values can be represented verbatim."""
    xcal = """
    <icalendar xmlns="urn:ietf:params:xml:ns:icalendar-2.0">
      <vcalendar><properties></properties><components><vevent><properties>
        <x-thing><x-weird><x-child>value</x-child></x-weird></x-thing>
      </properties><components /></vevent></components></vcalendar>
    </icalendar>
    """
    with pytest.raises(XCalParsingError, match="Unsupported structured extension"):
        Calendar.from_xcal(xcal)


def test_xml_input_rejects_doctype_and_external_entities():
    """Document type declarations and external entities are not parsed."""
    xcal = """<?xml version="1.0"?>
    <!DOCTYPE icalendar [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
    <icalendar xmlns="urn:ietf:params:xml:ns:icalendar-2.0">&xxe;</icalendar>
    """
    with pytest.raises(XCalParsingError, match="DTD declarations are not allowed"):
        Calendar.from_xcal(xcal)


def test_xml_input_limits_depth_and_text_size():
    """Input is rejected beyond configured depth and text limits."""
    xcal = (
        '<icalendar xmlns="urn:ietf:params:xml:ns:icalendar-2.0"><vcalendar>'
        "<properties><prodid><text>x</text></prodid></properties><components>"
        + "<vevent><properties></properties><components>" * 6
        + "</components></vevent>" * 6
        + "</components></vcalendar></icalendar>"
    )
    with pytest.raises(XCalParsingError, match="nesting depth"):
        Calendar.from_xcal(xcal, max_depth=10)
    with pytest.raises(XCalParsingError, match="size"):
        Calendar.from_xcal(xcal, max_text_size=20)
