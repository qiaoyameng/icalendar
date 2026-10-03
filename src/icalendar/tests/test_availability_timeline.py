"""Tests for :func:`icalendar.availability_timeline`.

This combines "VAVAILABILITY" (:rfc:`7953`), "VFREEBUSY" and expanded
"VEVENT" occurrences into a free/busy timeline over a finite window.
"""

from datetime import datetime, timedelta, timezone

import pytest
from dateutil.rrule import rrulestr

from icalendar import (
    FBTYPE,
    Availability,
    BusySource,
    Calendar,
    Event,
    FreeBusy,
    availability_timeline,
)

UTC = timezone.utc


def utc(day, hour=0, minute=0, month=1, year=2024):
    """A UTC datetime."""
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def make_event(uid, start, end, **props):
    """An expanded VEVENT occurrence."""
    event = Event()
    event.add("UID", uid)
    event.add("DTSTAMP", utc(1))
    event.add("DTSTART", start)
    if end is not None:
        event.add("DTEND", end)
    for name, value in props.items():
        event.add(name.upper().replace("_", "-"), value)
    return event


def segment_types(timeline):
    """The (start, end, busy_type) of each segment, for compact assertions."""
    return [(s.start, s.end, s.busy_type) for s in timeline]


class TestThreeOverlappingAvailabilities:
    """Three mutually overlapping VAVAILABILITY components.

    - ``rule-a`` has no PRIORITY, which means 0 and claims time first.
    - ``rule-b`` and ``rule-c`` share PRIORITY 1 and are merged:
      together they cover the whole window, each filling the time the
      other one does not cover.
    - ``rule-c`` ends after the window and is clipped.
    """

    ICS = """
BEGIN:VAVAILABILITY
UID:rule-a
DTSTAMP:20240101T000000Z
DTSTART:20240101T120000Z
DTEND:20240103T120000Z
BEGIN:AVAILABLE
UID:free-a
SUMMARY:Office hours
DTSTAMP:20240101T000000Z
DTSTART:20240102T090000Z
DTEND:20240102T170000Z
END:AVAILABLE
BEGIN:AVAILABLE
UID:stray-a
DTSTAMP:20240101T000000Z
DTSTART:20240103T130000Z
DTEND:20240103T140000Z
END:AVAILABLE
END:VAVAILABILITY
"""
    ICS_B = """
BEGIN:VAVAILABILITY
UID:rule-b
DTSTAMP:20240101T000000Z
PRIORITY:1
BUSYTYPE:BUSY
DTSTART:20240101T000000Z
DTEND:20240103T000000Z
END:VAVAILABILITY
"""
    ICS_C = """
BEGIN:VAVAILABILITY
UID:rule-c
DTSTAMP:20240101T000000Z
PRIORITY:1
BUSYTYPE:BUSY-TENTATIVE
DTSTART:20240103T000000Z
DTEND:20240104T120000Z
END:VAVAILABILITY
"""

    @pytest.fixture
    def timeline(self):
        return availability_timeline(
            utc(1),
            utc(4),
            availabilities=[
                Availability.from_ical(self.ICS),
                Availability.from_ical(self.ICS_B),
                Availability.from_ical(self.ICS_C),
            ],
        )

    def test_segments(self, timeline):
        """Priority 0 claims time first, same priority merges, clipping."""
        assert segment_types(timeline) == [
            (utc(1), utc(1, 12), FBTYPE.BUSY),
            (utc(1, 12), utc(2, 9), FBTYPE.BUSY_UNAVAILABLE),
            (utc(2, 9), utc(2, 17), FBTYPE.FREE),
            (utc(2, 17), utc(3, 12), FBTYPE.BUSY_UNAVAILABLE),
            (utc(3, 12), utc(4), FBTYPE.BUSY_TENTATIVE),
        ]

    def test_sources_explain_the_segments(self, timeline):
        """Each busy segment names the component it comes from."""
        rule_b, claimed_a, free_a, rest_a, rule_c = timeline
        assert rule_b.sources == (
            BusySource("VAVAILABILITY", FBTYPE.BUSY, uid="rule-b"),
        )
        assert claimed_a.sources == rest_a.sources == (
            BusySource("VAVAILABILITY", FBTYPE.BUSY_UNAVAILABLE, uid="rule-a"),
        )
        assert free_a.sources == (
            BusySource("AVAILABLE", FBTYPE.FREE, uid="free-a", summary="Office hours"),
        )
        assert rule_c.sources == (
            BusySource("VAVAILABILITY", FBTYPE.BUSY_TENTATIVE, uid="rule-c"),
        )

    def test_available_outside_its_vavailability_is_ignored(self, timeline):
        """The stray AVAILABLE of rule-a is clipped to its container."""
        for segment in timeline:
            assert all(source.uid != "stray-a" for source in segment.sources)
        assert utc(3, 13) not in [
            segment.start for segment in timeline if segment.is_free
        ]

    def test_timeline_covers_the_window_without_gaps(self, timeline):
        """The segments are adjacent and cover [start, end) exactly."""
        assert timeline[0].start == utc(1)
        assert timeline[-1].end == utc(4)
        for previous, following in zip(timeline, timeline[1:]):
            assert previous.end == following.start


class TestDaylightSavingTransition:
    """A recurring AVAILABLE crossing the DST change in America/New_York.

    On 2024-03-10 the clocks spring forward: the 9:00-17:00 local time
    block moves from 14:00-22:00 UTC to 13:00-21:00 UTC.
    """

    ICS = """
BEGIN:VAVAILABILITY
UID:dst
DTSTAMP:20240301T000000Z
BEGIN:AVAILABLE
UID:dst-free
DTSTAMP:20240301T000000Z
DTSTART;TZID=America/New_York:20240309T090000
DTEND;TZID=America/New_York:20240309T170000
RRULE:FREQ=DAILY;COUNT=3
END:AVAILABLE
END:VAVAILABILITY
"""

    @staticmethod
    def expand(available, window_start, window_end):
        """Expand the RRULE of an AVAILABLE component with dateutil."""
        rule = rrulestr(
            available["RRULE"].to_ical().decode(),
            dtstart=available["DTSTART"].dt,
        )
        duration = available.duration
        for occurrence in rule.between(window_start - duration, window_end):
            yield occurrence, occurrence + duration

    @pytest.fixture
    def timeline(self):
        return availability_timeline(
            utc(9, month=3),
            utc(12, month=3),
            availabilities=[Availability.from_ical(self.ICS)],
            expand=self.expand,
        )

    def test_free_segments_follow_the_utc_offset_change(self, timeline):
        free = [s for s in timeline if s.is_free]
        assert segment_types(free) == [
            (utc(9, 14, month=3), utc(9, 22, month=3), FBTYPE.FREE),
            (utc(10, 13, month=3), utc(10, 21, month=3), FBTYPE.FREE),
            (utc(11, 13, month=3), utc(11, 21, month=3), FBTYPE.FREE),
        ]
        assert all(
            source.uid == "dst-free" for segment in free for source in segment.sources
        )

    def test_busy_segments_fill_the_rest(self, timeline):
        assert segment_types(timeline) == [
            (utc(9, month=3), utc(9, 14, month=3), FBTYPE.BUSY_UNAVAILABLE),
            (utc(9, 14, month=3), utc(9, 22, month=3), FBTYPE.FREE),
            (utc(9, 22, month=3), utc(10, 13, month=3), FBTYPE.BUSY_UNAVAILABLE),
            (utc(10, 13, month=3), utc(10, 21, month=3), FBTYPE.FREE),
            (utc(10, 21, month=3), utc(11, 13, month=3), FBTYPE.BUSY_UNAVAILABLE),
            (utc(11, 13, month=3), utc(11, 21, month=3), FBTYPE.FREE),
            (utc(11, 21, month=3), utc(12, month=3), FBTYPE.BUSY_UNAVAILABLE),
        ]

    def test_recurring_available_without_expand_uses_first_occurrence(self):
        """Without the expand callback, recurrence is not expanded."""
        timeline = availability_timeline(
            utc(9, month=3),
            utc(12, month=3),
            availabilities=[Availability.from_ical(self.ICS)],
        )
        free = [s for s in timeline if s.is_free]
        assert segment_types(free) == [
            (utc(9, 14, month=3), utc(9, 22, month=3), FBTYPE.FREE),
        ]


class TestFreeBusyAndEvents:
    """Explicit FREEBUSY periods and expanded events."""

    FREE_BUSY = FreeBusy.from_ical(
        b"BEGIN:VFREEBUSY\r\n"
        b"UID:fb-1\r\n"
        b"DTSTAMP:20240601T000000Z\r\n"
        b"FREEBUSY;FBTYPE=BUSY-TENTATIVE:20240601T080000Z/20240601T100000Z\r\n"
        b"FREEBUSY:20240601T110000Z/PT1H\r\n"
        b"FREEBUSY;FBTYPE=FREE:20240601T140000Z/20240601T150000Z\r\n"
        b"END:VFREEBUSY\r\n"
    )

    @pytest.fixture
    def timeline(self):
        return availability_timeline(
            utc(1, month=6),
            utc(2, month=6),
            free_busy=[self.FREE_BUSY],
            events=[
                make_event("e1", utc(1, 9, month=6), utc(1, 10, month=6)),
                make_event(
                    "e2",
                    utc(1, 12, month=6),
                    utc(1, 13, month=6),
                    transp="TRANSPARENT",
                ),
                make_event(
                    "e3",
                    utc(1, 16, month=6),
                    utc(1, 17, month=6),
                    status="CANCELLED",
                ),
                make_event(
                    "e4",
                    utc(1, 18, month=6),
                    utc(1, 19, month=6),
                    status="TENTATIVE",
                ),
            ],
        )

    def test_segments(self, timeline):
        assert segment_types(timeline) == [
            (utc(1, month=6), utc(1, 8, month=6), FBTYPE.FREE),
            (utc(1, 8, month=6), utc(1, 9, month=6), FBTYPE.BUSY_TENTATIVE),
            (utc(1, 9, month=6), utc(1, 10, month=6), FBTYPE.BUSY),
            (utc(1, 10, month=6), utc(1, 11, month=6), FBTYPE.FREE),
            (utc(1, 11, month=6), utc(1, 12, month=6), FBTYPE.BUSY),
            (utc(1, 12, month=6), utc(1, 18, month=6), FBTYPE.FREE),
            (utc(1, 18, month=6), utc(1, 19, month=6), FBTYPE.BUSY_TENTATIVE),
            (utc(1, 19, month=6), utc(2, month=6), FBTYPE.FREE),
        ]

    def test_event_source_explains_the_busy_time(self, timeline):
        busy = next(s for s in timeline if s.busy_type == FBTYPE.BUSY and s.start == utc(1, 9, month=6))
        assert busy.sources == (BusySource("VEVENT", FBTYPE.BUSY, uid="e1"),)

    def test_transparent_and_cancelled_events_are_ignored(self, timeline):
        uids = {source.uid for segment in timeline for source in segment.sources}
        assert "e2" not in uids
        assert "e3" not in uids

    def test_tentative_event_is_busy_tentative(self, timeline):
        tentative = next(s for s in timeline if s.start == utc(1, 18, month=6))
        assert tentative.busy_type == FBTYPE.BUSY_TENTATIVE
        assert tentative.sources == (
            BusySource("VEVENT", FBTYPE.BUSY_TENTATIVE, uid="e4"),
        )

    def test_free_freebusy_period_does_not_erase_busy_time(self, timeline):
        """The FREE period at 14:00 does not split or change anything."""
        assert all(
            not (s.start <= utc(1, 14, month=6) < s.end and s.sources)
            for s in timeline
        )


def test_adjacent_same_source_periods_are_merged():
    """Adjacent half-open periods of the same type and source merge."""
    free_busy = FreeBusy.from_ical(
        b"BEGIN:VFREEBUSY\r\n"
        b"UID:fb-merge\r\n"
        b"DTSTAMP:20240601T000000Z\r\n"
        b"FREEBUSY:20240601T100000Z/20240601T110000Z\r\n"
        b"FREEBUSY:20240601T110000Z/20240601T120000Z\r\n"
        b"END:VFREEBUSY\r\n"
    )
    timeline = availability_timeline(
        utc(1, month=6), utc(2, month=6), free_busy=[free_busy]
    )
    assert segment_types(timeline) == [
        (utc(1, month=6), utc(1, 10, month=6), FBTYPE.FREE),
        (utc(1, 10, month=6), utc(1, 12, month=6), FBTYPE.BUSY),
        (utc(1, 12, month=6), utc(2, month=6), FBTYPE.FREE),
    ]


def test_unknown_fbtype_is_treated_as_busy():
    """See :rfc:`5545#section-3.2.9`: unknown FBTYPE values are BUSY."""
    free_busy = FreeBusy.from_ical(
        b"BEGIN:VFREEBUSY\r\n"
        b"UID:fb-x\r\n"
        b"DTSTAMP:20240601T000000Z\r\n"
        b"FREEBUSY;FBTYPE=X-UNKNOWN:20240601T100000Z/20240601T110000Z\r\n"
        b"END:VFREEBUSY\r\n"
    )
    timeline = availability_timeline(
        utc(1, month=6), utc(2, month=6), free_busy=[free_busy]
    )
    busy = next(s for s in timeline if not s.is_free)
    assert busy.busy_type == FBTYPE.BUSY


def test_window_must_not_be_empty():
    with pytest.raises(ValueError, match="after its start"):
        availability_timeline(utc(2), utc(2))
    with pytest.raises(ValueError, match="after its start"):
        availability_timeline(utc(3), utc(2))


def test_empty_input_is_a_free_timeline():
    timeline = availability_timeline(utc(1), utc(2))
    assert segment_types(timeline) == [(utc(1), utc(2), FBTYPE.FREE)]
    assert timeline[0].sources == ()
    assert timeline[0].duration == timedelta(days=1)


def test_event_without_end_takes_no_time():
    """A DATE-TIME event without DTEND or DURATION takes no time."""
    event = make_event("zero", utc(1, 9), None)
    timeline = availability_timeline(utc(1), utc(2), events=[event])
    assert segment_types(timeline) == [(utc(1), utc(2), FBTYPE.FREE)]


def test_naive_window_is_assumed_utc():
    event = make_event("naive", utc(1, 9), utc(1, 10))
    timeline = availability_timeline(
        datetime(2024, 1, 1), datetime(2024, 1, 2), events=[event]
    )
    assert segment_types(timeline) == [
        (utc(1), utc(1, 9), FBTYPE.FREE),
        (utc(1, 9), utc(1, 10), FBTYPE.BUSY),
        (utc(1, 10), utc(2), FBTYPE.FREE),
    ]
