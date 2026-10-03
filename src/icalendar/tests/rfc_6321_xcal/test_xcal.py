"""Tests for the RFC 6321 xCal serialization and parsing."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from xml.parsers import expat

import pytest

from icalendar import (
    Calendar,
    Event,
    TimezoneDaylight,
    TimezoneStandard,
    vBinary,
    vPeriod,
)
from icalendar.error import XCalParsingError, XCalSerializationError
from icalendar.xcal import XCAL_NAMESPACE, xcal_to_component

NS = f'xmlns="{XCAL_NAMESPACE}"'


def test_xcal_namespace_and_structure():
    calendar = Calendar()
    event = Event()
    event.add("summary", "Meeting")
    calendar.add_component(event)

    xml = calendar.to_xcal()
    assert xml.startswith('<?xml version="1.0" encoding="utf-8"?>')
    assert "<icalendar" in xml and XCAL_NAMESPACE in xml
    assert "<vcalendar>" in xml
    assert "<properties>" in xml
    assert "<components>" in xml
    assert "<summary>" in xml
    assert "<text>Meeting</text>" in xml


def test_component_round_trip_simple():
    calendar = Calendar()
    event = Event()
    event.add("summary", "My Event")
    event.add("dtstart", date(2025, 11, 22))
    calendar.add_component(event)

    parsed = Calendar.from_xcal(calendar.to_xcal())
    assert parsed.events[0].summary == "My Event"
    assert parsed.events[0].start == date(2025, 11, 22)


def test_utc_offset_is_structured():
    """UTC-OFFSET must be a real element, not a str() dump."""
    xml = f"""<icalendar {NS}>
      <vcalendar><properties/><components>
        <vtimezone><properties><tzid><text>X</text></tzid></properties>
         <components>
           <standard><properties>
             <tzoffsetfrom><utc-offset>-05:00</utc-offset></tzoffsetfrom>
             <tzoffsetto><utc-offset>-04:00</utc-offset></tzoffsetto>
           </properties></standard>
         </components>
        </vtimezone>
      </components></vcalendar>
    </icalendar>"""
    calendar = Calendar.from_xcal(xml)
    standard = calendar.timezones[0].standard[0]
    assert standard.get("tzoffsetfrom").td == timedelta(hours=-5)
    assert standard.get("tzoffsetto").td == timedelta(hours=-4)
    # Serialization uses the colon form of RFC 6321, not +HHMMSS.
    out = calendar.to_xcal()
    assert "<utc-offset>-05:00</utc-offset>" in out
    assert "-0500" not in out


def test_period_is_structured_end_and_duration():
    end_period = vPeriod(
        (datetime(2024, 6, 1, 14, 0), datetime(2024, 6, 1, 15, 0))
    )
    duration_period = vPeriod(
        (datetime(2024, 7, 1, 14, 0), timedelta(hours=2, minutes=30))
    )
    calendar = Calendar()
    event = Event()
    event.add(
        "rdate",
        [
            (end_period.start, end_period.end),
            (duration_period.start, duration_period.duration),
        ],
    )
    calendar.add_component(event)

    xml = calendar.to_xcal()
    assert "<period>" in xml
    assert "<start>2024-06-01T14:00:00</start>" in xml
    assert "<end>2024-06-01T15:00:00</end>" in xml
    assert "<start>2024-07-01T14:00:00</start>" in xml
    assert "<duration>PT2H30M</duration>" in xml

    parsed = Calendar.from_xcal(xml)
    rdates = parsed.events[0].rdates
    assert rdates[0] == (end_period.start, end_period.end)
    assert rdates[1][0] == duration_period.start
    assert rdates[1][1] - rdates[1][0] == timedelta(hours=2, minutes=30)


def test_recur_is_structured():
    calendar = Calendar()
    event = Event()
    event.add("rrule", {"freq": "monthly", "count": 3, "byday": ["2mo", "-1su"]})
    calendar.add_component(event)
    xml = calendar.to_xcal()
    assert "<freq>monthly</freq>" in xml
    assert "<count>3</count>" in xml
    assert "<byday>" in xml
    assert "<ordwk>2</ordwk>" in xml
    assert "<weekday>mo</weekday>" in xml
    assert "<ordwk>-1</ordwk>" in xml

    parsed = Calendar.from_xcal(xml)
    rule = parsed.events[0].rrules[0]
    assert rule["COUNT"] == 3
    weekdays = [str(day) for day in rule["BYDAY"]]
    assert weekdays == ["2MO", "-1SU"]


def test_binary_is_base64_element():
    calendar = Calendar()
    event = Event()
    binary = vBinary(b"hello world")
    event.add("attach", binary, parameters={"fmttype": "application/octet-stream"})
    calendar.add_component(event)
    xml = calendar.to_xcal()
    # Binary must not be double-escaped ICS text; it sits in a <binary>.
    assert "<binary>" in xml
    assert "aGVsbG8gd29ybGQ=" in xml
    parsed = Calendar.from_xcal(xml)
    attach = parsed.events[0].get("attach")
    assert attach.bytes == b"hello world"
    assert attach.params["FMTTYPE"] == "application/octet-stream"


def test_multi_value_properties_repeat_elements():
    calendar = Calendar()
    event = Event()
    event.add("categories", ["Review", "Quarterly"])
    calendar.add_component(event)
    xml = calendar.to_xcal()
    assert xml.count("<text>") >= 2
    parsed = Calendar.from_xcal(xml)
    assert [str(item) for item in parsed.events[0].categories] == ["Review", "Quarterly"]


def test_unknown_component_round_trips():
    calendar = Calendar()
    event = Event()
    event.add("x-venue-code", "Main Hall A")
    calendar.add_component(event)

    custom = calendar.get_component_class("X-CUSTOM-COMP")()
    custom.add("x-counter", "42")
    custom.add("x-note", "vendor data")
    calendar.add_component(custom)

    xml = calendar.to_xcal()
    assert "<x-venue-code>" in xml
    assert "<x-custom-comp>" in xml
    parsed = Calendar.from_xcal(xml)
    assert parsed.events[0].get("x-venue-code") == "Main Hall A"
    names = [component.name for component in parsed.subcomponents]
    assert "X-CUSTOM-COMP" in names
    custom_parsed = [
        component
        for component in parsed.subcomponents
        if component.name == "X-CUSTOM-COMP"
    ][0]
    assert str(custom_parsed.get("x-counter")) == "42"
    assert str(custom_parsed.get("x-note")) == "vendor data"


def test_unknown_recur_part_raises_clear_error():
    calendar = Calendar()
    event = Event()
    event.add("rrule", "FREQ=MONTHLY;RSCALE=HEBREW")
    calendar.add_component(event)
    with pytest.raises(XCalSerializationError, match="rscale"):
        calendar.to_xcal()


def test_unknown_value_element_raises_clear_error():
    xml = f"""<icalendar {NS}>
      <vcalendar><properties/><components>
        <vevent><properties>
          <x-thing><bogus>data</bogus></x-thing>
        </properties></vevent>
      </components></vcalendar>
    </icalendar>"""
    with pytest.raises(XCalParsingError, match="not defined by RFC 6321"):
        Calendar.from_xcal(xml)


# ---------------------------------------------------------------------------
# Parser hardening
# ---------------------------------------------------------------------------


def test_doctype_is_rejected():
    xml = f"""<?xml version="1.0"?>
    <!DOCTYPE lolz [<!ENTITY lol "lol">]>
    <icalendar {NS}><vcalendar><properties/></vcalendar></icalendar>"""
    with pytest.raises(XCalParsingError, match="Document type"):
        Calendar.from_xcal(xml)


def test_external_entity_is_rejected():
    xml = f"""<?xml version="1.0"?>
    <!DOCTYPE icalendar SYSTEM "file:///etc/passwd">
    <icalendar {NS}><vcalendar><properties/></vcalendar></icalendar>"""
    with pytest.raises(XCalParsingError):
        Calendar.from_xcal(xml)


def test_undefined_entity_does_not_fetch():
    xml = (
        f'<icalendar {NS}><vcalendar><properties/>'
        f"<x>&ext;</x></vcalendar></icalendar>"
    )
    with pytest.raises(XCalParsingError):
        Calendar.from_xcal(xml)


def test_wrong_namespace_rejected():
    xml = (
        '<icalendar xmlns="http://example.com/not-xcal">'
        "<vcalendar><properties/></vcalendar></icalendar>"
    )
    with pytest.raises(XCalParsingError, match="namespace"):
        Calendar.from_xcal(xml)


def test_depth_limit():
    nested = "<vfoo><components>" * 60
    closing = "</components></vfoo>" * 60
    xml = (
        f"<icalendar {NS}><vcalendar><properties/>"
        f"<components>{nested}{closing}</components></vcalendar></icalendar>"
    )
    with pytest.raises(XCalParsingError, match="depth"):
        Calendar.from_xcal(xml, max_depth=50)


def test_text_size_limit():
    xml = (
        f"<icalendar {NS}><vcalendar><properties>"
        f"<summary><text>{'A' * 5000}</text></summary>"
        f"</properties></vcalendar></icalendar>"
    )
    with pytest.raises(XCalParsingError, match="text data exceeds"):
        Calendar.from_xcal(xml, max_text_size=100)


def test_attributes_rejected():
    xml = (
        f'<icalendar {NS} version="1"><vcalendar>'
        "<properties/></vcalendar></icalendar>"
    )
    with pytest.raises(XCalParsingError, match="attributes"):
        Calendar.from_xcal(xml)


def test_comment_and_processing_instruction_rejected():
    comment = (
        f"<icalendar {NS}><!-- secret -->"
        "<vcalendar><properties/></vcalendar></icalendar>"
    )
    with pytest.raises(XCalParsingError, match="Comments"):
        Calendar.from_xcal(comment)
    instruction = (
        '<?xml version="1.0"?><?php evil?>'
        f'<icalendar {NS}><vcalendar><properties/></vcalendar></icalendar>'
    )
    with pytest.raises(XCalParsingError, match="Processing instructions"):
        Calendar.from_xcal(instruction)


def test_malformed_xml_reports_error():
    with pytest.raises(XCalParsingError):
        Calendar.from_xcal("<not-xml")


def test_text_escaping_is_xml_not_ical():
    calendar = Calendar()
    event = Event()
    event.add("summary", "Tom & Jerry <season 1>")
    event.add("description", "line1\nline2\ttab")
    calendar.add_component(event)
    xml = calendar.to_xcal()
    assert "Tom &amp; Jerry &lt;season 1&gt;" in xml
    # No ICS backslash escaping must leak into xCal.
    assert "\\;" not in xml and "\\," not in xml
    parsed = Calendar.from_xcal(xml)
    assert str(parsed.events[0].summary) == "Tom & Jerry <season 1>"
    assert str(parsed.events[0].description) == "line1\nline2\ttab"


def test_time_duration_and_exdate_round_trip():
    calendar = Calendar.from_ical(
        """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:t1
DTSTAMP:20240101T120000Z
DTSTART:20240312T090000Z
DURATION:PT1H30M
X-AT;VALUE=TIME:123000
EXDATE:20240313T090000Z,20240314T090000Z
END:VEVENT
END:VCALENDAR"""
    )
    parsed = Calendar.from_xcal(calendar.to_xcal())
    event = parsed.events[0]
    assert event.get("duration").dt == timedelta(hours=1, minutes=30)
    assert str(event.get("duration").dt) == "1:30:00"
    assert event.get("x-at").dt.strftime("%H:%M:%S") == "12:30:00"
    assert event.get("x-at").params["VALUE"] == "TIME"
    exdates = event.exdates
    assert exdates[0].strftime("%H:%M:%S") == "09:00:00"
    assert len(exdates) == 2


def test_typed_parameters_round_trip():
    calendar = Calendar.from_ical(
        """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:p1
DTSTAMP:20240101T120000Z
DTSTART:20240101T120000Z
ATTENDEE;RSVP=TRUE;ROLE=REQ-PARTICIPANT:mailto:a@example.com
END:VEVENT
END:VCALENDAR"""
    )
    xml = calendar.to_xcal()
    assert "<rsvp>" in xml and "<boolean>true</boolean>" in xml
    parsed = Calendar.from_xcal(xml)
    attendee = parsed.events[0].get("attendee")
    assert attendee.params["RSVP"] is True
    assert attendee.params["ROLE"] == "REQ-PARTICIPANT"


def test_alarm_and_todo_components_round_trip():
    calendar = Calendar.from_ical(
        """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VTODO
UID:task1
DTSTAMP:20240101T120000Z
SUMMARY:Task
DTSTART:20240101T120000Z
PERCENT-COMPLETE:50
BEGIN:VALARM
ACTION:DISPLAY
TRIGGER:-PT15M
DESCRIPTION:Reminder
END:VALARM
END:VTODO
END:VCALENDAR"""
    )
    parsed = Calendar.from_xcal(calendar.to_xcal())
    todo = parsed.todos[0]
    assert str(todo.summary) == "Task"
    assert int(todo.get("percent-complete")) == 50
    alarm = next(item for item in todo.subcomponents if item.name == "VALARM")
    assert str(alarm.get("action")) == "DISPLAY"
    assert alarm.get("trigger").dt == timedelta(minutes=-15)


def test_bytes_input_and_output():
    calendar = Calendar()
    event = Event()
    event.add("summary", "Bytes")
    calendar.add_component(event)
    data = calendar.to_xcal_bytes()
    assert isinstance(data, bytes)
    parsed = Calendar.from_xcal(data)
    assert parsed.events[0].summary == "Bytes"


def test_pretty_printing_and_stable_reparse():
    calendar = Calendar()
    event = Event()
    event.add("summary", "S")
    calendar.add_component(event)
    xml = calendar.to_xcal()
    # Re-parsing the serialized document must produce the same document.
    assert Calendar.from_xcal(xml).to_xcal() == xml


@pytest.mark.parametrize(
    "xml",
    [
        # two root components
        f"<icalendar {NS}><vcalendar><properties/></vcalendar>"
        "<vcalendar><properties/></vcalendar></icalendar>",
        # property without a value element
        f"<icalendar {NS}><vcalendar><properties/><components>"
        "<vevent><properties><summary/></properties></vevent>"
        "</components></vcalendar></icalendar>",
        # period without start
        f"<icalendar {NS}><vcalendar><properties/><components>"
        "<vevent><properties><freebusy><period><end>2024-01-01T00:00:00</end>"
        "</period></freebusy></properties></vevent>"
        "</components></vcalendar></icalendar>",
        # mismatched value types in one property
        f"<icalendar {NS}><vcalendar><properties/><components>"
        "<vevent><properties><x-mix><text>a</text><integer>1</integer>"
        "</x-mix></properties></vevent>"
        "</components></vcalendar></icalendar>",
        # unexpected element inside a component
        f"<icalendar {NS}><vcalendar><bogus/></vcalendar></icalendar>",
        # bad boolean
        f"<icalendar {NS}><vcalendar><properties/><components>"
        "<vevent><properties><attendee><parameters><rsvp>"
        "<boolean>maybe</boolean></rsvp></parameters>"
        "<cal-address>mailto:a@example.com</cal-address>"
        "</attendee></properties></vevent>"
        "</components></vcalendar></icalendar>",
        # recur byday without weekday
        f"<icalendar {NS}><vcalendar><properties/><components>"
        "<vevent><properties><rrule><recur><freq>daily</freq>"
        "<byday><day><ordwk>2</ordwk></day></byday></recur></rrule>"
        "</properties></vevent></components></vcalendar></icalendar>",
    ],
)
def test_invalid_structures_are_rejected(xml):
    with pytest.raises(XCalParsingError):
        Calendar.from_xcal(xml)


def test_empty_document_rejected():
    with pytest.raises(XCalParsingError):
        Calendar.from_xcal("   ")


def test_request_status_is_repeated_text():
    calendar = Calendar.from_ical(
        """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:r1
DTSTAMP:20240101T120000Z
DTSTART:20240101T120000Z
REQUEST-STATUS:2.0;Success
END:VEVENT
END:VCALENDAR"""
    )
    xml = calendar.to_xcal()
    start = xml.index("<request-status>")
    end = xml.index("</request-status>") + len("</request-status>")
    fragment = xml[start:end]
    assert fragment.count("<text>2.0</text>") == 1
    assert fragment.count("<text>Success</text>") == 1
    parsed = Calendar.from_xcal(xml)
    assert str(parsed.events[0].get("request-status")) == "2.0;Success"


def test_carriage_return_survives_for_unknown_value():
    from icalendar.prop import vUnknown

    calendar = Calendar()
    event = Event()
    event["x-note"] = vUnknown("a\rb")
    calendar.add_component(event)
    xml = calendar.to_xcal()
    # CR is escaped as an XML character reference rather than inserted raw.
    assert "&#xD;" in xml
    parsed = Calendar.from_xcal(xml.encode())
    assert str(parsed.events[0].get("x-note")) == "a\rb"


def test_xml_illegal_character_rejected_on_output():
    from icalendar.prop import vUnknown

    calendar = Calendar()
    event = Event()
    event["x-note"] = vUnknown("a\x00b")
    calendar.add_component(event)
    with pytest.raises(XCalSerializationError):
        calendar.to_xcal()
