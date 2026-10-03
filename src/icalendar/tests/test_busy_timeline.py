"""Tests for the busy timeline synthesis from :rfc:`7953` data.

These cover the combination of ``VAVAILABILITY``/``AVAILABLE`` rules,
scheduled events and explicit ``VFREEBUSY`` information into a half-open UTC
timeline.
"""

from datetime import date, datetime, timedelta, timezone

import pytest
from zoneinfo import ZoneInfo

from icalendar import (
    BUSYTYPE,
    FBTYPE,
    STATUS,
    TRANSP,
    Availability,
    Available,
    BusyState,
    Calendar,
    Event,
    ExpandedEvent,
    FreeBusy,
    busy_timeline,
    vPeriod,
)
from icalendar.cal.busy_time import SourceKind

UTC = timezone.utc
NEW_YORK = ZoneInfo("America/New_York")


def utc(hour: float = 0, day: int = 15) -> datetime:
    """Return a UTC datetime on 2024-10-``day`` at the given fractional hour."""
    return datetime(2024, 10, day, tzinfo=UTC) + timedelta(hours=hour)


def day_end(day: int = 15) -> datetime:
    """Return the exclusive end (next midnight UTC) of the given day."""
    return datetime(2024, 10, day, tzinfo=UTC) + timedelta(days=1)


def as_utc(value: datetime) -> datetime:
    """Normalize a datetime to ``datetime.timezone.utc`` for comparisons."""
    return value.astimezone(UTC)


def states(timeline) -> list[tuple[float, float, BusyState]]:
    """Reduce a timeline to ``(start_hour, end_hour, state)`` tuples."""
    origin = datetime(2024, 10, 15, tzinfo=UTC)

    def hours(value: datetime) -> float:
        return (as_utc(value) - origin).total_seconds() / 3600

    return [(hours(p.start), hours(p.end), p.state) for p in timeline.periods]


def bounds(period):
    """Return the ``(start, end)`` of a period normalized to ``timezone.utc``."""
    return as_utc(period.start), as_utc(period.end)


def make_availability(start, end, *, priority=0, busy_type=None, slots=()):
    """Build a VAVAILABILITY with the given AVAILABLE slots."""
    availability = Availability.new(
        start=start, end=end, priority=priority, busy_type=busy_type
    )
    for slot_start, slot_end in slots:
        availability.add_component(Available.new(start=slot_start, end=slot_end))
    return availability


def test_empty_window_is_unknown():
    """Without any information the whole window is UNKNOWN."""
    timeline = busy_timeline(utc(8), utc(18))
    assert states(timeline) == [(8.0, 18.0, BusyState.UNKNOWN)]
    assert timeline.busy == []
    assert timeline.free == []
    assert len(timeline.unknown) == 1


def test_available_segment_stays_inside_envelope():
    """AVAILABLE time never leaks beyond the VAVAILABILITY envelope."""
    availability = make_availability(
        utc(8), utc(18), slots=[(utc(9), utc(12))]
    )
    timeline = busy_timeline(utc(0), day_end(), availabilities=[availability])
    assert states(timeline) == [
        (0.0, 8.0, BusyState.UNKNOWN),
        (8.0, 9.0, BusyState.BUSY_UNAVAILABLE),
        (9.0, 12.0, BusyState.FREE),
        (12.0, 18.0, BusyState.BUSY_UNAVAILABLE),
        (18.0, 24.0, BusyState.UNKNOWN),
    ]


def test_busytype_default_is_busy_unavailable():
    availability = make_availability(utc(8), utc(18))
    timeline = busy_timeline(utc(8), utc(18), availabilities=[availability])
    assert [p.state for p in timeline.periods] == [BusyState.BUSY_UNAVAILABLE]


def test_explicit_busytype_is_used():
    availability = make_availability(
        utc(8), utc(18), busy_type=BUSYTYPE.BUSY_TENTATIVE
    )
    timeline = busy_timeline(utc(8), utc(18), availabilities=[availability])
    assert [p.state for p in timeline.periods] == [BusyState.BUSY_TENTATIVE]


def test_three_mutually_covering_priority_rules():
    """Three overlapping VAVAILABILITY components combine by PRIORITY.

    * priority 0 (lowest): whole day BUSY-UNAVAILABLE, free 09-11;
    * priority 1: 10-14 BUSY, free 12-13;
    * priority 2 (highest): 11-13:30 BUSY-TENTATIVE, free 13-13:30.

    Higher priority rules override lower ones inside their envelopes, while
    lower rules still govern uncovered time.
    """
    lowest = make_availability(
        utc(0), day_end(), priority=0, slots=[(utc(9), utc(11))]
    )
    middle = make_availability(
        utc(10),
        utc(14),
        priority=1,
        busy_type=BUSYTYPE.BUSY,
        slots=[(utc(12), utc(13))],
    )
    highest = make_availability(
        utc(11),
        utc(13) + timedelta(minutes=30),
        priority=2,
        busy_type=BUSYTYPE.BUSY_TENTATIVE,
        slots=[(utc(13), utc(13) + timedelta(minutes=30))],
    )

    timeline = busy_timeline(
        utc(8), utc(16), availabilities=[highest, lowest, middle]
    )
    assert states(timeline) == [
        (8.0, 9.0, BusyState.BUSY_UNAVAILABLE),
        (9.0, 10.0, BusyState.FREE),
        (10.0, 11.0, BusyState.BUSY),
        (11.0, 13.0, BusyState.BUSY_TENTATIVE),
        (13.0, 13.5, BusyState.FREE),
        (13.5, 14.0, BusyState.BUSY),
        (14.0, 16.0, BusyState.BUSY_UNAVAILABLE),
    ]


def test_missing_priority_is_lowest_precedence():
    """A VAVAILABILITY without PRIORITY behaves like PRIORITY 0."""
    default_rule = make_availability(
        utc(0), day_end(), slots=[(utc(9), utc(12))]
    )
    explicit_zero = make_availability(
        utc(0),
        day_end(),
        priority=0,
        busy_type=BUSYTYPE.BUSY_TENTATIVE,
        slots=[(utc(9), utc(12))],
    )
    higher = make_availability(
        utc(10), utc(11), priority=1, busy_type=BUSYTYPE.BUSY
    )

    timeline = busy_timeline(
        utc(8), utc(13), availabilities=[default_rule, higher]
    )
    timeline_zero = busy_timeline(
        utc(8), utc(13), availabilities=[explicit_zero, higher]
    )
    expected = [
        (8.0, 9.0, BusyState.BUSY_UNAVAILABLE),
        (9.0, 10.0, BusyState.FREE),
        (10.0, 11.0, BusyState.BUSY),
        (11.0, 12.0, BusyState.FREE),
        (12.0, 13.0, BusyState.BUSY_UNAVAILABLE),
    ]
    assert states(timeline) == expected
    # Explicit PRIORITY 0 behaves the same, except its different BUSYTYPE.
    assert timeline_zero.periods[0].state is BusyState.BUSY_TENTATIVE
    assert timeline_zero.periods[-1].state is BusyState.BUSY_TENTATIVE


def test_same_priority_available_is_unioned():
    """Two same-priority envelopes merge their AVAILABLE ranges (union)."""
    first = make_availability(
        utc(0, 1),
        utc(0, 3),
        priority=2,
        slots=[(utc(10, 2), utc(11, 2))],
    )
    second = make_availability(
        utc(0, 3),
        utc(0, 5),
        priority=2,
        slots=[(utc(10, 4), utc(11, 4))],
    )
    timeline = busy_timeline(
        utc(0, 1), utc(0, 5), availabilities=[first, second]
    )

    free = timeline.free
    assert [bounds(p) for p in free] == [
        (utc(10, 2), utc(11, 2)),
        (utc(10, 4), utc(11, 4)),
    ]
    # Same-priority components are merged (RFC 7953 section 7.1) into one
    # envelope spanning the union, so the gap keeps the busy default rather
    # than becoming UNKNOWN.
    assert all(p.state is not BusyState.UNKNOWN for p in timeline.periods)
    gap = next(
        p for p in timeline.periods
        if bounds(p) == (utc(11, 2), utc(10, 4))
    )
    assert gap.state is BusyState.BUSY_UNAVAILABLE


def test_same_priority_busytype_precedence():
    """Overlapping same-priority envelopes use strongest BUSYTYPE."""
    busy = make_availability(
        utc(0, 1), utc(0, 5), priority=1, busy_type=BUSYTYPE.BUSY
    )
    unavailable = make_availability(
        utc(0, 2),
        utc(0, 4),
        priority=1,
        busy_type=BUSYTYPE.BUSY_UNAVAILABLE,
    )
    timeline = busy_timeline(
        utc(0, 1), utc(0, 5), availabilities=[busy, unavailable]
    )
    def entries():
        return [
            (bounds(p)[0], bounds(p)[1], p.state) for p in timeline.periods
        ]

    assert entries() == [
        (utc(0, 1), utc(0, 2), BusyState.BUSY),
        (utc(0, 2), utc(0, 4), BusyState.BUSY_UNAVAILABLE),
        (utc(0, 4), utc(0, 5), BusyState.BUSY),
    ]
    # The strongest (most unavailable) type wins everywhere both overlap and
    # its provenance is attached.
    overlap = next(p for p in timeline.periods if bounds(p)[0] == utc(0, 2))
    assert {source.uid for source in overlap.sources} == {unavailable.uid}


@pytest.fixture
def workday_availability():
    """A whole-day envelope free from 09:00 to 17:00."""
    return make_availability(utc(0), day_end(), slots=[(utc(9), utc(17))])


def test_events_overlay_free_time(workday_availability):
    """An opaque confirmed event turns AVAILABLE time busy."""
    event = Event.new(summary="Meeting", start=utc(10), end=utc(11))
    timeline = busy_timeline(
        utc(8), utc(18), availabilities=[workday_availability], events=[event]
    )
    period = next(p for p in timeline.periods if p.start == utc(10))
    assert period.state is BusyState.BUSY
    assert period.sources[0].summary == "Meeting"
    assert period.sources[0].kind.name == "EVENT"


def test_tentative_event_is_busy_tentative(workday_availability):
    event = Event.new(
        summary="Maybe", start=utc(12), end=utc(13), status=STATUS.TENTATIVE
    )
    timeline = busy_timeline(
        utc(8), utc(18), availabilities=[workday_availability], events=[event]
    )
    period = next(p for p in timeline.periods if p.start == utc(12))
    assert period.state is BusyState.BUSY_TENTATIVE
    assert period.sources[0].busy_type == FBTYPE.BUSY_TENTATIVE.value


def test_transparent_event_does_not_block(workday_availability):
    event = Event.new(
        summary="Reminder",
        start=utc(14),
        end=utc(15),
        transparency=TRANSP.TRANSPARENT,
    )
    timeline = busy_timeline(
        utc(8), utc(18), availabilities=[workday_availability], events=[event]
    )
    period = next(
        p for p in timeline.periods if bounds(p) == (utc(9), utc(17))
    )
    assert period.state is BusyState.FREE


def test_cancelled_event_does_not_block(workday_availability):
    event = Event.new(
        summary="Cancelled", start=utc(15), end=utc(16), status=STATUS.CANCELLED
    )
    timeline = busy_timeline(
        utc(8), utc(18), availabilities=[workday_availability], events=[event]
    )
    period = next(
        p for p in timeline.periods if bounds(p) == (utc(9), utc(17))
    )
    assert period.state is BusyState.FREE


def test_preexpanded_event_occurrence():
    """Callers can supply already expanded occurrences without an expander."""
    event = Event.new(summary="Series")
    occurrence = ExpandedEvent(event=event, start=utc(10), end=utc(10.5))
    timeline = busy_timeline(
        utc(8),
        utc(18),
        events=[occurrence],
        expand=lambda *_: pytest.fail("the expander must not be used"),
    )
    period = next(p for p in timeline.periods if as_utc(p.start) == utc(10))
    assert period.state is BusyState.BUSY
    assert period.sources[0].summary == "Series"


def test_event_clipped_to_window(workday_availability):
    """An event crossing a window edge is clipped with half-open semantics."""
    event = Event.new(start=utc(17), end=utc(20))
    timeline = busy_timeline(
        utc(8), utc(18), availabilities=[workday_availability], events=[event]
    )
    busy_periods = timeline.busy
    assert as_utc(busy_periods[-1].end) == utc(18)


def test_explicit_freebusy_busy_overrides_available(workday_availability):
    free_busy = FreeBusy.new(start=utc(8), end=utc(18))
    free_busy.add(
        "FREEBUSY", vPeriod((utc(9), utc(9.5)), params={"FBTYPE": "BUSY"})
    )
    timeline = busy_timeline(
        utc(8),
        utc(12),
        availabilities=[workday_availability],
        free_busy=[free_busy],
    )
    assert states(timeline)[:2] == [
        (8.0, 9.0, BusyState.BUSY_UNAVAILABLE),
        (9.0, 9.5, BusyState.BUSY),
    ]


def test_explicit_freebusy_free_opens_busy_time():
    """FBTYPE=FREE frees even busy availability time."""
    availability = make_availability(utc(0), day_end())
    free_busy = FreeBusy.new(start=utc(0), end=day_end())
    free_busy.add(
        "FREEBUSY", vPeriod((utc(10), utc(11)), params={"FBTYPE": "FREE"})
    )
    timeline = busy_timeline(
        utc(9), utc(12), availabilities=[availability], free_busy=[free_busy]
    )
    period = next(p for p in timeline.periods if p.start == utc(10))
    assert period.state is BusyState.FREE
    assert period.sources[0].kind.name == "FREEBUSY"


def test_unknown_fbtype_is_treated_as_busy(workday_availability):
    free_busy = FreeBusy.new(start=utc(8), end=utc(18))
    free_busy.add(
        "FREEBUSY", vPeriod((utc(9), utc(10)), params={"FBTYPE": "X-PRIVATE"})
    )
    timeline = busy_timeline(
        utc(9), utc(10), availabilities=[workday_availability], free_busy=[free_busy]
    )
    assert [p.state for p in timeline.periods] == [BusyState.BUSY]


def test_recurring_available_across_dst_fallback():
    """A daily local-time rule across fall-back keeps correct UTC length.

    The rule recurs every day from 01:00 to 03:00 America/New_York. On the
    fall-back day (2024-11-03) the clock repeats the 01:00 hour, so the fixed
    local range starts at the first 01:00 (EDT, 05:00 UTC) and ends at 03:00
    EST (08:00 UTC): three UTC hours. On a normal day the same local range is
    two UTC hours (06:00-08:00 EST).
    """
    window_start = datetime(2024, 11, 2, 12, tzinfo=UTC)
    window_end = datetime(2024, 11, 4, 12, tzinfo=UTC)
    calendar = Calendar.from_ical(
        "BEGIN:VCALENDAR\n"
        "BEGIN:VAVAILABILITY\n"
        "UID:env\nDTSTAMP:20240101T000000Z\n"
        "DTSTART:20241102T120000Z\nDTEND:20241104T120000Z\n"
        "BEGIN:AVAILABLE\n"
        "UID:slot\n"
        "DTSTART;TZID=America/New_York:20241102T010000\n"
        "DTEND;TZID=America/New_York:20241102T030000\n"
        "RRULE:FREQ=DAILY;COUNT=3\n"
        "END:AVAILABLE\n"
        "END:VAVAILABILITY\n"
        "END:VCALENDAR\n"
    )
    availability = calendar.availabilities[0]

    timeline = busy_timeline(
        window_start, window_end, availabilities=[availability]
    )
    free = timeline.free

    # The Nov 2 instance (05:00-07:00 UTC) is entirely before the window and is
    # clipped away. The fall-back day spans three UTC hours; the EST day spans
    # two, even though both are 01:00-03:00 local time.
    assert [bounds(p) for p in free] == [
        (datetime(2024, 11, 3, 5, tzinfo=UTC),
         datetime(2024, 11, 3, 8, tzinfo=UTC)),
        (datetime(2024, 11, 4, 6, tzinfo=UTC),
         datetime(2024, 11, 4, 8, tzinfo=UTC)),
    ]
    assert free[0].duration == timedelta(hours=3)  # fall-back (repeated hour)
    assert free[1].duration == timedelta(hours=2)  # normal EST day


def test_recurring_available_is_expanded_through_callback():
    availability = make_availability(
        utc(0, 15), day_end(17), slots=[(utc(9, 15), utc(10, 15))]
    )
    available = availability.available[0]
    available.add("RRULE", "FREQ=DAILY;COUNT=3")

    timeline = busy_timeline(
        utc(0, 15),
        day_end(17),
        availabilities=[availability],
    )
    free = timeline.free
    assert len(free) == 3  # day 15, 16, 17 inside the window


def test_busy_period_explains_why_unavailable(workday_availability):
    """Busy periods carry enough provenance to explain their state."""
    event = Event.new(
        uid="meeting-1",
        summary="Interview",
        start=utc(10),
        end=utc(11),
    )
    timeline = busy_timeline(
        utc(8),
        utc(18),
        availabilities=[workday_availability],
        events=[event],
    )
    busy_period = next(p for p in timeline.busy if p.start == utc(10))
    source = busy_period.sources[0]
    assert source.uid == "meeting-1"
    assert source.summary == "Interview"
    assert source.occurrence == (utc(10), utc(11))
    assert "Interview" in source.describe()

    envelope_busy = next(
        p for p in timeline.busy if p.start == utc(8)
    )
    assert envelope_busy.sources[0].kind.name == "AVAILABILITY_BUSY"
    assert "BUSY-UNAVAILABLE" in envelope_busy.sources[0].describe()


def test_half_open_window_end_is_exclusive():
    """The window end must not belong to the timeline."""
    availability = make_availability(
        utc(8), utc(18), slots=[(utc(9), utc(18))]
    )
    timeline = busy_timeline(utc(8), utc(18), availabilities=[availability])
    assert timeline.state_at(utc(17, ).replace(hour=17)) is BusyState.FREE
    with pytest.raises(KeyError):
        timeline.state_at(utc(18))
    assert as_utc(timeline.periods[-1].end) == utc(18)


def test_adjacent_same_state_segments_merge():
    availability = make_availability(
        utc(8),
        utc(18),
        slots=[(utc(9), utc(10)), (utc(10), utc(11))],
    )
    timeline = busy_timeline(utc(8), utc(18), availabilities=[availability])
    free = timeline.free
    assert [bounds(p) for p in free] == [(utc(9), utc(11))]


def test_naive_window_datetimes_are_utc():
    availability = make_availability(
        utc(8), utc(18), slots=[(utc(9), utc(12))]
    )
    timeline = busy_timeline(
        datetime(2024, 10, 15, 8),
        datetime(2024, 10, 15, 18),
        availabilities=[availability],
    )
    assert as_utc(timeline.start) == utc(8)
    assert as_utc(timeline.end) == utc(18)


def test_invalid_window_raises():
    with pytest.raises(ValueError):
        busy_timeline(utc(18), utc(8))


def test_as_free_busy_component(workday_availability):
    event = Event.new(start=utc(10), end=utc(11))
    timeline = busy_timeline(
        utc(8),
        utc(18),
        availabilities=[workday_availability],
        events=[event],
    )
    free_busy = timeline.as_free_busy()
    fb_types = sorted(
        str(period.params["FBTYPE"]) for period in free_busy["FREEBUSY"]
    )
    assert "BUSY" in fb_types
    assert "FREE" in fb_types


def test_calendar_convenience_method(workday_availability):
    calendar = Calendar()
    calendar.add_component(workday_availability)
    event = Event.new(start=utc(10), end=utc(11))
    calendar.add_component(event)

    timeline = calendar.busy_timeline(
        utc(8), utc(12), events="calendar"
    )
    period = next(p for p in timeline.periods if p.start == utc(10))
    assert period.state is BusyState.BUSY


def test_available_clipped_to_envelope_when_it_extends_beyond():
    """An AVAILABLE range reaching past the envelope is cut at the envelope."""
    availability = make_availability(
        utc(8), utc(18), slots=[(utc(17), utc(20))]
    )
    timeline = busy_timeline(
        utc(8), day_end(), availabilities=[availability]
    )
    free = timeline.free
    assert [bounds(p) for p in free] == [(utc(17), utc(18))]
    # After the envelope the time is unknown, not free.
    tail = timeline.periods[-1]
    assert as_utc(tail.start) == utc(18)
    assert tail.state is BusyState.UNKNOWN


def test_all_day_event_covers_whole_day():
    """A DATE-valued event occupies the full UTC day."""
    event = Event()
    event.add("DTSTART", date(2024, 10, 15))
    timeline = busy_timeline(utc(0), day_end(), events=[event])
    assert [(bounds(p)[0], bounds(p)[1], p.state) for p in timeline.periods] == [
        (utc(0), day_end(), BusyState.BUSY)
    ]


def test_all_day_event_clipped_to_window():
    event = Event()
    event.add("DTSTART", date(2024, 10, 15))
    event.add("DTEND", date(2024, 10, 17))  # two full days
    timeline = busy_timeline(utc(12), day_end(15), events=[event])
    assert [(bounds(p)[0], bounds(p)[1], p.state) for p in timeline.periods] == [
        (utc(12), day_end(15), BusyState.BUSY)
    ]


def test_unbounded_availability_envelope():
    """A VAVAILABILITY without DTSTART/DTEND applies for the whole window."""
    availability = Availability()
    availability.add_component(Available.new(start=utc(9), end=utc(12)))
    timeline = busy_timeline(utc(8), utc(18), availabilities=[availability])
    assert states(timeline) == [
        (8.0, 9.0, BusyState.BUSY_UNAVAILABLE),
        (9.0, 12.0, BusyState.FREE),
        (12.0, 18.0, BusyState.BUSY_UNAVAILABLE),
    ]


def test_partially_bounded_availability_envelope():
    """Only DTSTART present: busy from the start to the window end."""
    availability = Availability.new(start=utc(14))
    timeline = busy_timeline(utc(8), utc(18), availabilities=[availability])
    assert states(timeline) == [
        (8.0, 14.0, BusyState.UNKNOWN),
        (14.0, 18.0, BusyState.BUSY_UNAVAILABLE),
    ]


def test_multiday_event_keeps_day_offset():
    """An event ending on a later day keeps the DTEND day, not the start day."""
    event = Event.new(
        start=datetime(2024, 10, 15, 10, tzinfo=UTC),
        end=datetime(2024, 10, 16, 12, tzinfo=UTC),
    )
    timeline = busy_timeline(
        datetime(2024, 10, 15, tzinfo=UTC),
        datetime(2024, 10, 17, tzinfo=UTC),
        events=[event],
    )
    busy = timeline.busy
    assert [bounds(p) for p in busy] == [
        (
            datetime(2024, 10, 15, 10, tzinfo=UTC),
            datetime(2024, 10, 16, 12, tzinfo=UTC),
        )
    ]
    assert busy[0].duration == timedelta(hours=26)


def test_provenance_describes_unavailable_segment(workday_availability):
    """Every segment exposes human-readable provenance."""
    event = Event.new(summary="Standup", start=utc(9), end=utc(9.5))
    timeline = busy_timeline(
        utc(8), utc(11), availabilities=[workday_availability], events=[event]
    )
    kinds = {
        (p.state, tuple(source.kind for source in p.sources))
        for p in timeline.periods
    }
    descriptions = [
        source.describe()
        for period in timeline.periods
        for source in period.sources
    ]
    assert any("Standup" in text for text in descriptions)
    assert any("BUSY-UNAVAILABLE" in text for text in descriptions)
    assert (BusyState.BUSY, (SourceKind.EVENT,)) in kinds
