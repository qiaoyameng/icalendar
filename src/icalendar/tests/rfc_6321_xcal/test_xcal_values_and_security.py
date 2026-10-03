"""Value and security coverage for :rfc:`6321` xCal."""

from __future__ import annotations

from datetime import timedelta
from xml.etree import ElementTree as ET

import pytest

from icalendar import Calendar, Component, vAdr, vN, vOrg, vPeriod
from icalendar.xcal import XCalParsingError, component_to_xcal_element, parse_xcal_xml, property_to_xcal_element
from icalendar.xcal import _parse_property as parse_xcal_property


def _property_element(xml: str):
    root = parse_xcal_xml(
        f'<icalendar xmlns="urn:ietf:params:xml:ns:icalendar-2.0">'
        f"<vcalendar><properties>{xml}</properties><components /></vcalendar></icalendar>"
    )
    return root[0][0][0]


def test_utc_offset_seconds_round_trip():
    """UTC offsets use colon-separated xCal text and keep seconds."""
    component = Calendar.from_xcal(
        """
        <icalendar xmlns="urn:ietf:params:xml:ns:icalendar-2.0">
          <vcalendar><properties></properties><components>
            <standard><properties>
              <tzoffsetfrom><utc-offset>+05:30:30</utc-offset></tzoffsetfrom>
              <tzoffsetto><utc-offset>-00:30</utc-offset></tzoffsetto>
            </properties><components /></standard>
          </components></vcalendar>
        </icalendar>
        """
    )
    standard = component.subcomponents[0]
    assert standard["tzoffsetfrom"].td == timedelta(hours=5, minutes=30, seconds=30)
    assert standard["tzoffsetto"].td == timedelta(minutes=-30)
    assert b"<utc-offset>+05:30:30</utc-offset>" in component.to_xcal()


def test_geo_is_structured_and_round_trips():
    """GEO uses latitude and longitude elements."""
    xcal = """
    <icalendar xmlns="urn:ietf:params:xml:ns:icalendar-2.0">
      <vcalendar><properties></properties><components><vevent><properties>
        <geo><geo><latitude>37.386013</latitude><longitude>-122.082932</longitude></geo></geo>
      </properties><components /></vevent></components></vcalendar>
    </icalendar>
    """
    event = Calendar.from_xcal(xcal).events[0]
    geo = event["geo"]
    assert (geo.latitude, geo.longitude) == (37.386013, -122.082932)
    assert b"<geo><geo><latitude>37.386013</latitude>" in event.to_xcal()


def test_structured_text_properties_round_trip():
    """vCard ADR/N/ORG use multiple text elements."""
    xml_pairs = [
        ("<adr><text /><text /><text>Street</text><text>City</text><text>ST</text><text>0</text><text>US</text></adr>", vAdr),
        ("<n><text>Doe</text><text>John</text><text /><text /><text /></n>", vN),
        ("<org><text>Org</text><text>Unit</text></org>", vOrg),
    ]
    for xml, expected_class in xml_pairs:
        property_element = _property_element(xml)
        _, value = parse_xcal_property(property_element, Component)
        assert isinstance(value, expected_class)


def test_request_status_uses_three_text_values():
    """The semicolon-separated REQUEST-STATUS fields remain distinct values."""
    xcal = """
    <icalendar xmlns="urn:ietf:params:xml:ns:icalendar-2.0">
      <vcalendar><properties></properties><components><vevent><properties>
        <request-status><text>2.0</text><text>Success</text><text>extra; detail</text></request-status>
      </properties><components /></vevent></components></vcalendar>
    </icalendar>
    """
    event = Calendar.from_xcal(xcal).events[0]
    assert str(event["request-status"]) == "2.0;Success;extra; detail"


def test_multi_valued_and_structured_parameters_round_trip():
    """Parameter lists and typed parameters are represented as xCal children."""
    xcal = """
    <icalendar xmlns="urn:ietf:params:xml:ns:icalendar-2.0">
      <vcalendar><properties></properties><components><vevent><properties>
        <attendee><parameters>
          <delegated-to><cal-address>mailto:a@example.org</cal-address><cal-address>mailto:b@example.org</cal-address></delegated-to>
          <gap><duration>P1W</duration></gap>
          <rsvp><boolean>true</boolean></rsvp>
        </parameters><cal-address>mailto:c@example.org</cal-address></attendee>
      </properties><components /></vevent></components></vcalendar>
    </icalendar>
    """
    attendee = Calendar.from_xcal(xcal).events[0].attendees[0]
    assert attendee.params["DELEGATED-TO"] == [
        "mailto:a@example.org",
        "mailto:b@example.org",
    ]
    assert attendee.params["GAP"] == timedelta(weeks=1)
    assert attendee.RSVP is True
    output = ET.tostring(property_to_xcal_element("attendee", attendee), encoding="unicode")
    assert "<delegated-to>" in output
    assert "<gap><duration>P7D</duration></gap>" in output
    assert "<boolean>true</boolean>" in output


def test_extension_parameter_value_type_is_preserved():
    """Unknown parameter extension types are represented verbatim."""
    xcal = """
    <icalendar xmlns="urn:ietf:params:xml:ns:icalendar-2.0">
      <vcalendar><properties></properties><components><vevent><properties>
        <x-thing><parameters><x-param><x-weird>a\\,b</x-weird></x-param></parameters><unknown>v</unknown></x-thing>
      </properties><components /></vevent></components></vcalendar>
    </icalendar>
    """
    prop = Calendar.from_xcal(xcal).events[0]["x-thing"]
    assert prop.params["X-PARAM"].VALUE == "X-WEIRD"
    assert str(prop.params["X-PARAM"]) == r"a\,b"
    assert b"<x-weird>a\\,b</x-weird>" in ET.tostring(property_to_xcal_element("x-thing", prop), encoding="utf-8")


def test_non_x_structured_extension_is_rejected_with_clear_error():
    """An unrecognized IANA value type gets the same explicit policy."""
    xcal = """
    <icalendar xmlns="urn:ietf:params:xml:ns:icalendar-2.0">
      <vcalendar><properties></properties><components><vevent><properties>
        <x-thing><future-type><a>1</a></future-type></x-thing>
      </properties><components /></vevent></components></vcalendar>
    </icalendar>
    """
    with pytest.raises(XCalParsingError, match="Unsupported structured extension"):
        Calendar.from_xcal(xcal)


def test_namespaced_attributes_and_text_are_rejected():
    """xCal elements use child elements, not attributes or stray text."""
    with pytest.raises(XCalParsingError, match="attributes"):
        Calendar.from_xcal(
            '<icalendar xmlns="urn:ietf:params:xml:ns:icalendar-2.0" version="1"><vcalendar><properties /><components /></vcalendar></icalendar>'
        )
    with pytest.raises(XCalParsingError, match="text outside"):
        Calendar.from_xcal(
            '<icalendar xmlns="urn:ietf:params:xml:ns:icalendar-2.0"><vcalendar><properties><summary>bad</summary><text>stray</text></properties><components /></vcalendar></icalendar>'
        )


def test_entities_are_not_resolved_even_without_doctype():
    """Built-in entities are XML-encoded by the serializer; custom entities remain disabled."""
    assert parse_xcal_xml(
        '<icalendar xmlns="urn:ietf:params:xml:ns:icalendar-2.0"><vcalendar><properties /></vcalendar></icalendar>'
    ) is not None
    with pytest.raises(XCalParsingError, match="DTD declarations|Entity declarations"):
        parse_xcal_xml(
            """<?xml version="1.0"?>
            <!DOCTYPE icalendar [<!ENTITY custom "custom">]>
            <icalendar xmlns="urn:ietf:params:xml:ns:icalendar-2.0">&custom;</icalendar>
            """
        )


def test_xcal_output_is_valid_xml_and_namespaced():
    """Serialization uses the required XML namespace and XML escaping."""
    from icalendar import Event

    event = Event()
    event.add("freebusy", vPeriod.examples()[0])
    root = ET.fromstring(ET.tostring(property_to_xcal_element("freebusy", event["freebusy"]), encoding="utf-8"))
    assert root.tag == "{urn:ietf:params:xml:ns:icalendar-2.0}freebusy"
