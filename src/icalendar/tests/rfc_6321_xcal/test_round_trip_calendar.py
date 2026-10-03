"""Integrated ICS -> xCal -> ICS round-trip test as requested for RFC 6321.

The calendar contains:

- a VTIMEZONE with DAYLIGHT/STANDARD subcomponents and their RRULEs,
- a recurring VEVENT (RRULE plus EXDATE/RDATE) in that time zone,
- PERIOD-valued RDATEs (both end-date and duration forms),
- a Base64 BINARY attachment,
- multi-valued CATEGORIES, parameters and X-properties,
- an unknown X-component.

The semantics of the components are compared rather than the exact bytes of
the two iCalendar serializations, because XML/JSON value typing makes some
representation differences (parameter order, explicit VALUE types)
irrelevant.
"""

from __future__ import annotations

from datetime import timedelta

from icalendar import Calendar

CALENDAR_WITH_TIMEZONE_RECUR_PERIOD_BINARY = """\
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//xCal Round Trip//EN
CALSCALE:GREGORIAN
BEGIN:VTIMEZONE
TZID:America/New_York
LAST-MODIFIED:20050809T050000Z
BEGIN:DAYLIGHT
TZOFFSETFROM:-0500
TZOFFSETTO:-0400
TZNAME:EDT
DTSTART:19700308T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:-0400
TZOFFSETTO:-0500
TZNAME:EST
DTSTART:19701101T020000
RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU
END:STANDARD
END:VTIMEZONE
BEGIN:VEVENT
UID:xcal-semantic-roundtrip@example.com
DTSTAMP:20240101T120000Z
SUMMARY:Quarterly review
DESCRIPTION:Semicolon; comma, and backslash \\ must survive.
DTSTART;TZID=America/New_York:20240312T090000
DTEND;TZID=America/New_York:20240312T103000
RRULE:FREQ=MONTHLY;COUNT=3;BYDAY=2TU
EXDATE;TZID=America/New_York:20240514T090000
RDATE;VALUE=PERIOD:20240601T140000Z/20240601T150000Z,20240701T140000Z/PT2H30M
ATTACH;FMTTYPE=application/octet-stream;ENCODING=BASE64;VALUE=BINARY;X-LABEL=Report:VGhlIHF1aWNrIGJyb3duIGZveCBqdW1wcyBvdmVyIHRoZSBsYXp5IGRvZy4=
CATEGORIES:Review,Quarterly
GEO:37.386013;-122.082932
ORGANIZER;CN=Alice Organizer:mailto:alice@example.com
X-VENUE-CODE:Main Hall A
END:VEVENT
BEGIN:X-CUSTOM-COMP
X-COUNTER:42
X-NOTE:vendor data
END:X-CUSTOM-COMP
END:VCALENDAR
"""


def _component_semantics(calendar: Calendar) -> dict:
    """Extract the comparable semantics of the round-tripped calendar."""
    timezone = calendar.timezones[0]
    daylight = timezone.daylight[0]
    standard = timezone.standard[0]
    event = calendar.events[0]
    rdates = event.rdates
    attach = event.get("attach")
    custom = next(
        component
        for component in calendar.subcomponents
        if component.name == "X-CUSTOM-COMP"
    )
    return {
        "timezone_id": str(timezone.get("tzid")),
        "daylight_offset_from": daylight.get("tzoffsetfrom").td,
        "daylight_offset_to": daylight.get("tzoffsetto").td,
        "daylight_name": str(daylight.get("tzname")),
        "daylight_rrule": daylight.rrules[0].to_ical(),
        "standard_offset_from": standard.get("tzoffsetfrom").td,
        "standard_offset_to": standard.get("tzoffsetto").td,
        "standard_rrule": standard.rrules[0].to_ical(),
        "uid": str(event.uid),
        "summary": str(event.summary),
        "description": str(event.description),
        "start": event.start,
        "end": event.end,
        "duration": event.end - event.start,
        "start_tzid": str(event.start.tzinfo),
        "rrule": event.rrules[0].to_ical(),
        "exdates": event.exdates,
        "rdate_1_start": rdates[0][0],
        "rdate_1_end": rdates[0][1],
        "rdate_2_start": rdates[1][0],
        "rdate_2_duration": rdates[1][1] - rdates[1][0],
        "attachment_bytes": attach.bytes,
        "attachment_fmttype": str(attach.params["FMTTYPE"]),
        "attachment_label": str(attach.params["X-LABEL"]),
        "categories": [str(item) for item in event.categories],
        "geo": event.get("geo").ical_value,
        "organizer": str(event.organizer),
        "organizer_cn": str(event.organizer.params["CN"]),
        "venue": str(event.get("x-venue-code")),
        "custom_counter": str(custom.get("x-counter")),
        "custom_note": str(custom.get("x-note")),
    }


def test_ics_xcal_ics_semantic_round_trip():
    original = Calendar.from_ical(CALENDAR_WITH_TIMEZONE_RECUR_PERIOD_BINARY)

    xcal = original.to_xcal()
    # The structured value types must not appear flattened to text.
    assert "<utc-offset>" in xcal
    assert "<recur>" in xcal
    assert "<period>" in xcal
    assert "<binary>" in xcal
    assert "urn:ietf:params:xml:ns:icalendar-2.0" in xcal

    round_tripped = Calendar.from_xcal(xcal)
    before = _component_semantics(original)
    after = _component_semantics(round_tripped)

    assert before == after

    # Spot-check important values explicitly for readable failures.
    assert after["daylight_offset_to"] == timedelta(hours=-4)
    assert after["standard_offset_to"] == timedelta(hours=-5)
    assert after["rrule"] == b"FREQ=MONTHLY;COUNT=3;BYDAY=2TU"
    assert after["rdate_2_duration"] == timedelta(hours=2, minutes=30)
    assert after["attachment_bytes"] == (
        b"The quick brown fox jumps over the lazy dog."
    )
    assert after["categories"] == ["Review", "Quarterly"]
    assert after["venue"] == "Main Hall A"
    assert after["custom_counter"] == "42"

    # A second xCal serialization must be stable.
    assert Calendar.from_xcal(round_tripped.to_xcal()).to_xcal() == xcal
