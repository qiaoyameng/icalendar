"""Tests for isolated per-calendar timezone parsing contexts.

Two calendars may use the same TZID (e.g. ``OfficeTime``) with different
``VTIMEZONE`` definitions. Parsing each with its own
:class:`~icalendar.timezone.TZP` context keeps the definitions isolated,
so a long-running service is not influenced by the file parsed first.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from icalendar import Calendar, TimezoneDefinitionConflict
from icalendar.timezone import TZP
from icalendar.timezone import tzp as global_tzp

try:
    import pytz  # noqa: F401

    HAS_PYTZ = True
except ImportError:
    HAS_PYTZ = False

PROVIDERS = ["zoneinfo"] + (["pytz"] if HAS_PYTZ else [])

OFFSET_A = "+0545"
OFFSET_B = "-0330"
DELTA_A = timedelta(hours=5, minutes=45)
DELTA_B = -timedelta(hours=3, minutes=30)


def make_vtimezone(offset: str) -> str:
    """Create a VTIMEZONE for TZID ``OfficeTime`` with a fixed offset."""
    return f"""BEGIN:VTIMEZONE
TZID:OfficeTime
BEGIN:STANDARD
DTSTART:19700101T000000
TZOFFSETFROM:{offset}
TZOFFSETTO:{offset}
TZNAME:OFT
END:STANDARD
END:VTIMEZONE
"""


def make_calendar(offset: str, forward_reference: bool = False) -> str:
    """Create a calendar using TZID ``OfficeTime`` with a fixed offset."""
    vtimezone = make_vtimezone(offset)
    vevent = """BEGIN:VEVENT
UID:office-meeting
DTSTAMP:20240101T000000Z
DTSTART;TZID=OfficeTime:20240601T120000
END:VEVENT
"""
    body = vevent + vtimezone if forward_reference else vtimezone + vevent
    return f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//icalendar//context-test//EN
{body}END:VCALENDAR
"""


CALENDAR_A = make_calendar(OFFSET_A)
CALENDAR_B = make_calendar(OFFSET_B)


@pytest.fixture(params=PROVIDERS)
def provider(request):
    """The timezone provider implementation used for the contexts."""
    return request.param


@pytest.fixture
def make_context(provider):
    """Create timezone parsing contexts of the parametrized provider."""
    return lambda: TZP(provider)


def parse_with_new_context(ics: str, provider: str) -> Calendar:
    """Parse a calendar with a fresh isolated timezone context."""
    return Calendar.from_ical(ics, tzp=TZP(provider))


def test_alternating_parsing_stays_isolated(make_context, provider):
    """Alternating parses of two calendars keep their own definitions."""
    calendars = []
    for ics in (CALENDAR_A, CALENDAR_B, CALENDAR_A, CALENDAR_B):
        calendars.append(parse_with_new_context(ics, provider))
    offsets = [cal.events[0].start.utcoffset() for cal in calendars]
    assert offsets == [DELTA_A, DELTA_B, DELTA_A, DELTA_B]


def test_concurrent_parsing_stays_isolated(provider):
    """Concurrent parses do not influence each other through shared state."""
    ics_files = [CALENDAR_A, CALENDAR_B] * 20
    with ThreadPoolExecutor(max_workers=8) as pool:
        calendars = list(
            pool.map(lambda ics: parse_with_new_context(ics, provider), ics_files)
        )
    for index, cal in enumerate(calendars):
        expected = DELTA_A if index % 2 == 0 else DELTA_B
        assert cal.events[0].start.utcoffset() == expected


def test_serialization_after_isolated_parsing(make_context):
    """Both calendars serialize with their own offsets and the raw TZID."""
    ctx_a = make_context()
    ctx_b = make_context()
    cal_a = Calendar.from_ical(CALENDAR_A, tzp=ctx_a)
    cal_b = Calendar.from_ical(CALENDAR_B, tzp=ctx_b)

    out_a = cal_a.to_ical()
    out_b = cal_b.to_ical()
    assert b"DTSTART;TZID=OfficeTime:20240601T120000" in out_a
    assert b"DTSTART;TZID=OfficeTime:20240601T120000" in out_b
    assert b"TZOFFSETTO:+0545" in out_a
    assert b"TZOFFSETTO:-0330" in out_b
    # the raw TZID parameter is not rewritten to a system alias
    assert cal_a.events[0]["DTSTART"].params["TZID"] == "OfficeTime"
    assert cal_b.events[0]["DTSTART"].params["TZID"] == "OfficeTime"


def test_private_timezone_is_not_shared_between_contexts(make_context):
    """A timezone cached in one context cannot be looked up from another."""
    ctx_a = make_context()
    ctx_b = make_context()
    Calendar.from_ical(CALENDAR_A, tzp=ctx_a)

    assert ctx_a.timezone("OfficeTime") is not None
    assert ctx_b.timezone("OfficeTime") is None
    assert global_tzp.timezone("OfficeTime") is None


def test_context_can_be_released_independently(make_context):
    """Releasing a context frees its private timezones, others are unaffected."""
    ctx_a = make_context()
    ctx_b = make_context()
    Calendar.from_ical(CALENDAR_A, tzp=ctx_a)
    Calendar.from_ical(CALENDAR_B, tzp=ctx_b)

    ctx_a.release()
    assert ctx_a.timezone("OfficeTime") is None
    assert ctx_b.timezone("OfficeTime") is not None


def test_forward_reference_binds_to_own_calendar_definition(make_context):
    """Events written before the VTIMEZONE still bind to its definition."""
    ics = make_calendar(OFFSET_A, forward_reference=True)
    cal = Calendar.from_ical(ics, tzp=make_context())
    assert cal.events[0].start.utcoffset() == DELTA_A


def test_conflicting_duplicate_tzid_raises(make_context):
    """Duplicate, non-equivalent TZID definitions in one file are a conflict."""
    ics = CALENDAR_A.replace(
        "END:VCALENDAR", make_vtimezone(OFFSET_B) + "END:VCALENDAR"
    )
    with pytest.raises(TimezoneDefinitionConflict):
        Calendar.from_ical(ics, tzp=make_context())


def test_equivalent_duplicate_tzid_is_accepted(make_context):
    """Duplicate but equivalent TZID definitions in one file are fine."""
    ics = CALENDAR_A.replace(
        "END:VCALENDAR", make_vtimezone(OFFSET_A) + "END:VCALENDAR"
    )
    cal = Calendar.from_ical(ics, tzp=make_context())
    assert cal.events[0].start.utcoffset() == DELTA_A


def test_raw_tzid_not_rewritten_by_system_alias(make_context):
    """A TZID resolved through a system alias keeps its raw parameter."""
    ics = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//icalendar//context-test//EN
BEGIN:VEVENT
UID:alias-event
DTSTAMP:20240101T000000Z
DTSTART;TZID=W. Europe Standard Time:20240601T120000
END:VEVENT
END:VCALENDAR
"""
    cal = Calendar.from_ical(ics, tzp=make_context())
    event = cal.events[0]
    # resolved through the Windows-to-Olson alias ...
    assert event.start.utcoffset() == timedelta(hours=2)
    # ... but the raw TZID parameter is not rewritten
    assert event["DTSTART"].params["TZID"] == "W. Europe Standard Time"
    assert b"DTSTART;TZID=W. Europe Standard Time:20240601T120000" in cal.to_ical()


def test_default_entry_point_still_works():
    """Parsing without an explicit context keeps the old default behavior."""
    # a dedicated TZID keeps the shared default cache from leaking
    # into the assertions of the other tests
    cal = Calendar.from_ical(CALENDAR_A.replace("OfficeTime", "DefaultEntryTime"))
    assert cal.events[0].start.utcoffset() == DELTA_A


def test_context_manager_activates_context_for_default_entry():
    """An active context is used even without passing it explicitly."""
    with TZP() as ctx:
        cal = Calendar.from_ical(CALENDAR_A)
        assert cal.events[0].start.utcoffset() == DELTA_A
        assert ctx.timezone("OfficeTime") is not None
    assert global_tzp.timezone("OfficeTime") is None


def test_contexts_are_usable_as_context_managers(make_context):
    """Explicit contexts can be entered, used and exited independently."""
    ctx_a = make_context()
    ctx_b = make_context()
    with ctx_a:
        cal_a = Calendar.from_ical(CALENDAR_A, tzp=ctx_a)
        with ctx_b:
            cal_b = Calendar.from_ical(CALENDAR_B, tzp=ctx_b)
        # the outer context is restored after the inner one exits
        assert cal_a.events[0].start.utcoffset() == DELTA_A
    assert cal_b.events[0].start.utcoffset() == DELTA_B
