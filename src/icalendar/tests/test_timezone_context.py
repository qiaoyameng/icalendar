"""Isolated timezone resolution contexts."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from icalendar import Calendar, LazyCalendar, TimezoneConflict
from icalendar.timezone import timezone_context, tzp


def office_time_calendar(offset: str) -> str:
    return f"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:{offset}
DTSTART;TZID=OfficeTime:20240101T100000
DTEND;TZID=OfficeTime:20240101T110000
END:VEVENT
BEGIN:VTIMEZONE
TZID:OfficeTime
BEGIN:STANDARD
DTSTART:19700101T000000
TZOFFSETFROM:{offset}
TZOFFSETTO:{offset}
TZNAME:OFF{offset[1:4]}
END:STANDARD
END:VTIMEZONE
END:VCALENDAR
"""


def private_time_calendar(timezone_id: str, offset: str) -> str:
    return f"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:{timezone_id}
DTSTART;TZID={timezone_id}:20240101T100000
END:VEVENT
BEGIN:VTIMEZONE
TZID:{timezone_id}
BEGIN:STANDARD
DTSTART:19700101T000000
TZOFFSETFROM:{offset}
TZOFFSETTO:{offset}
TZNAME:PRIVATE
END:STANDARD
END:VTIMEZONE
END:VCALENDAR
"""


@pytest.mark.parametrize("parser", [Calendar, LazyCalendar])
def test_alternating_calendars_keep_their_own_offsets(parser):
    first = parser.from_ical(office_time_calendar("+0500"))
    second = parser.from_ical(office_time_calendar("+1200"))
    first_again = parser.from_ical(office_time_calendar("+0500"))
    second_again = parser.from_ical(office_time_calendar("+1200"))

    assert first.events[0].start.utcoffset() == timedelta(hours=5)
    assert second.events[0].start.utcoffset() == timedelta(hours=12)
    assert first_again.events[0].start.utcoffset() == timedelta(hours=5)
    assert second_again.events[0].start.utcoffset() == timedelta(hours=12)

    serialized_first = parser.from_ical(first.to_ical())
    serialized_second = parser.from_ical(second.to_ical())
    assert serialized_first.events[0].start.utcoffset() == timedelta(hours=5)
    assert serialized_second.events[0].start.utcoffset() == timedelta(hours=12)


def test_concurrent_calendars_keep_their_own_offsets():
    def parse(offset: str) -> timedelta:
        result = timedelta()
        for _ in range(20):
            calendar = Calendar.from_ical(office_time_calendar(offset))
            result = calendar.events[0].start.utcoffset()
        return result

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(parse, ("+0500", "+1200")))

    assert results == [timedelta(hours=5), timedelta(hours=12)]


def test_explicit_contexts_do_not_share_private_timezones():
    first_context = timezone_context()
    second_context = timezone_context()
    first = Calendar.from_ical(
        private_time_calendar("FirstPrivate", "+0500"),
        timezone_context=first_context,
    )
    second = Calendar.from_ical(
        private_time_calendar("SecondPrivate", "+0900"),
        timezone_context=second_context,
    )

    assert first_context.timezone("FirstPrivate") is not None
    assert first_context.timezone("SecondPrivate") is None
    assert second_context.timezone("SecondPrivate") is not None
    assert second_context.timezone("FirstPrivate") is None
    assert tzp.timezone("FirstPrivate") is None
    assert tzp.timezone("SecondPrivate") is None

    first_context.close()
    second_context.close()
    assert first_context.timezone("FirstPrivate") is None
    assert second_context.timezone("SecondPrivate") is None
    assert first.events[0].start.utcoffset() == timedelta(hours=5)
    assert second.events[0].start.utcoffset() == timedelta(hours=9)


def test_context_manager_releases_private_timezone():
    with timezone_context() as context:
        Calendar.from_ical(
            private_time_calendar("ManagedPrivate", "+0500"),
            timezone_context=context,
        )
        assert context.timezone("ManagedPrivate") is not None

    assert context.timezone("ManagedPrivate") is None


def test_context_falls_back_to_public_timezone_policy():
    with timezone_context() as context:
        assert context.timezone("Europe/Berlin") is not None
        assert context.timezone("DefinitelyUnknownOfficeTime") is None


def test_original_tzid_parameter_is_preserved():
    calendar = Calendar.from_ical(office_time_calendar("+0500"))

    dtstart = calendar.events[0]["DTSTART"]
    assert dtstart.params["TZID"] == "OfficeTime"
    assert b"DTSTART;TZID=OfficeTime:20240101T100000" in calendar.to_ical()


def test_duplicate_equivalent_timezones_are_allowed():
    calendar_text = office_time_calendar("+0500")
    timezone_lines = calendar_text.splitlines()
    timezone_start = timezone_lines.index("BEGIN:VTIMEZONE")
    timezone_end = timezone_lines.index("END:VTIMEZONE", timezone_start)
    duplicate_lines = (
        timezone_lines[: timezone_end + 1]
        + timezone_lines[timezone_start:timezone_end + 1]
        + timezone_lines[timezone_end + 1 :]
    )
    duplicate = "\n".join(duplicate_lines)

    calendar = Calendar.from_ical(duplicate)

    assert calendar.events[0].start.utcoffset() == timedelta(hours=5)


def test_duplicate_non_equivalent_timezones_conflict():
    first = office_time_calendar("+0500")
    second = office_time_calendar("+0700")
    second_timezone = second[
        second.index("BEGIN:VTIMEZONE"):second.rindex("END:VTIMEZONE")
    ]
    conflicting = first.replace(
        "END:VTIMEZONE",
        second_timezone + "END:VTIMEZONE",
        1,
    )

    with pytest.raises(TimezoneConflict, match="OfficeTime"):
        Calendar.from_ical(conflicting)
