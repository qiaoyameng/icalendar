"""Tests for icalendar.recurrence.expand_instances."""

import datetime as dt

import pytest
from zoneinfo import ZoneInfo

from icalendar import Calendar
from icalendar.recurrence import (
    FloatingTimeError,
    RecurrenceConflictError,
    RecurrenceError,
    expand_instances,
)

UTC = dt.timezone.utc
NY = ZoneInfo("America/New_York")
BERLIN = ZoneInfo("Europe/Berlin")


def cal(*bodies: str) -> Calendar:
    return Calendar.from_ical(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        + "".join(body.replace("\n", "\r\n") for body in bodies)
        + "END:VCALENDAR\r\n"
    )


def expand(calendar, start, end, **kwargs):
    return expand_instances(calendar, start, end, **kwargs).instances


MASTER_NY = """BEGIN:VEVENT
UID:standup
DTSTAMP:20260101T000000Z
DTSTART;TZID=America/New_York:20260302T090000
DTEND;TZID=America/New_York:20260302T093000
RRULE:FREQ=DAILY
END:VEVENT
"""


def ny_window(start_day, end_day):
    return (
        dt.datetime(2026, 3, start_day, tzinfo=NY),
        dt.datetime(2026, 3, end_day, tzinfo=NY),
    )


class TestMasterExpansion:
    def test_infinite_rule_is_bounded_by_window(self):
        start, end = ny_window(10, 13)
        instances = expand(cal(MASTER_NY), start, end)
        assert [i.start.day for i in instances] == [10, 11, 12]
        assert all(i.uid == "standup" for i in instances)
        assert not any(i.overridden for i in instances)

    def test_exdate_removes_instance(self):
        body = MASTER_NY.replace(
            "RRULE:FREQ=DAILY",
            "RRULE:FREQ=DAILY\nEXDATE;TZID=America/New_York:20260311T090000",
        )
        start, end = ny_window(10, 13)
        assert [i.start.day for i in expand(cal(body), start, end)] == [10, 12]

    def test_rdate_adds_and_coinciding_rdate_is_not_duplicated(self):
        body = MASTER_NY.replace(
            "RRULE:FREQ=DAILY",
            "RRULE:FREQ=DAILY;COUNT=3\n"
            "RDATE;TZID=America/New_York:20260303T090000\n"
            "RDATE;TZID=America/New_York:20260310T090000",
        )
        start, end = ny_window(1, 31)
        starts = [i.start for i in expand(cal(body), start, end)]
        assert [s.day for s in starts] == [2, 3, 4, 10]
        assert len(starts) == len({i.recurrence_id for i in expand(cal(body), start, end)})

    def test_duration_spilling_into_window_is_found(self):
        body = MASTER_NY.replace(
            "DTEND;TZID=America/New_York:20260302T093000",
            "DTEND;TZID=America/New_York:20260302T233000",
        )
        # instance starts before the window but ends inside it
        start = dt.datetime(2026, 3, 2, 12, 0, tzinfo=NY)
        end = dt.datetime(2026, 3, 2, 23, 0, tzinfo=NY)
        instances = expand(cal(body), start, end)
        assert [i.recurrence_id for i in instances] == [
            dt.datetime(2026, 3, 2, 9, 0, tzinfo=NY)
        ]


class TestOverrides:
    def override(self, rid, new_start, new_end, extra=""):
        return f"""BEGIN:VEVENT
UID:standup
DTSTAMP:20260102T000000Z
RECURRENCE-ID;TZID=America/New_York:{rid}
DTSTART;TZID=America/New_York:{new_start}
DTEND;TZID=America/New_York:{new_end}
SEQUENCE:1
{extra}END:VEVENT
"""

    def test_moved_out_of_window_disappears_without_duplicate(self):
        override = self.override("20260311T090000", "20260320T110000", "20260320T113000")
        start, end = ny_window(10, 13)
        instances = expand(cal(MASTER_NY, override), start, end)
        assert [i.recurrence_id.day for i in instances] == [10, 12]

    def test_moved_into_window_appears_with_original_identity(self):
        override = self.override("20260311T090000", "20260312T170000", "20260312T173000")
        start, end = ny_window(12, 13)
        instances = expand(cal(MASTER_NY, override), start, end)
        assert len(instances) == 2  # the regular Mar 12 instance + the moved one
        instance = instances[1]
        assert instance.recurrence_id == dt.datetime(2026, 3, 11, 9, 0, tzinfo=NY)
        assert instance.start == dt.datetime(2026, 3, 12, 17, 0, tzinfo=NY)
        assert instance.end == dt.datetime(2026, 3, 12, 17, 30, tzinfo=NY)
        assert instance.overridden

    def test_override_of_instance_outside_window_moves_in(self):
        # recurrence id is outside the window; only the override lands inside
        override = self.override("20260305T090000", "20260312T080000", "20260312T083000")
        start, end = ny_window(10, 13)
        instances = expand(cal(MASTER_NY, override), start, end)
        by_rid = {i.recurrence_id.day: i for i in instances}
        assert sorted(by_rid) == [5, 10, 11, 12]
        assert by_rid[5].start == dt.datetime(2026, 3, 12, 8, 0, tzinfo=NY)
        assert by_rid[5].overridden

    def test_cancelled_override_removes_instance(self):
        override = self.override(
            "20260311T090000", "20260311T090000", "20260311T093000",
            extra="STATUS:CANCELLED\n",
        )
        start, end = ny_window(10, 13)
        instances = expand(cal(MASTER_NY, override), start, end)
        assert [i.recurrence_id.day for i in instances] == [10, 12]

    def test_single_override_changes_duration_of_one_instance_only(self):
        override = self.override("20260311T090000", "20260311T090000", "20260311T120000")
        start, end = ny_window(10, 13)
        instances = expand(cal(MASTER_NY, override), start, end)
        durations = {i.recurrence_id.day: i.end - i.start for i in instances}
        assert durations[11] == dt.timedelta(hours=3)
        assert durations[10] == durations[12] == dt.timedelta(minutes=30)


class TestThisAndFuture:
    def test_time_and_duration_adjustment_from_rid_onwards(self):
        future = """BEGIN:VEVENT
UID:standup
DTSTAMP:20260102T000000Z
RECURRENCE-ID;TZID=America/New_York;RANGE=THISANDFUTURE:20260311T090000
DTSTART;TZID=America/New_York:20260311T140000
DTEND;TZID=America/New_York:20260311T150000
SEQUENCE:1
END:VEVENT
"""
        start, end = ny_window(10, 14)
        instances = expand(cal(MASTER_NY, future), start, end)
        by_rid = {i.recurrence_id.day: i for i in instances}
        assert by_rid[10].start == dt.datetime(2026, 3, 10, 9, 0, tzinfo=NY)
        assert by_rid[10].end - by_rid[10].start == dt.timedelta(minutes=30)
        for day in (11, 12, 13):
            assert by_rid[day].start == dt.datetime(2026, 3, day, 14, 0, tzinfo=NY)
            assert by_rid[day].end - by_rid[day].start == dt.timedelta(hours=1)

    def test_future_shift_moves_pre_window_instances_into_window(self):
        # daily event in UTC, shifted +8 days from Feb 25 onwards
        master = """BEGIN:VEVENT
UID:shifted
DTSTAMP:20260101T000000Z
DTSTART:20260220T090000Z
DURATION:PT1H
RRULE:FREQ=DAILY
END:VEVENT
"""
        future = """BEGIN:VEVENT
UID:shifted
DTSTAMP:20260102T000000Z
RECURRENCE-ID;RANGE=THISANDFUTURE:20260225T090000Z
DTSTART:20260305T090000Z
DURATION:PT1H
SEQUENCE:1
END:VEVENT
"""
        start = dt.datetime(2026, 3, 1, tzinfo=UTC)
        end = dt.datetime(2026, 3, 10, tzinfo=UTC)
        instances = expand(cal(master, future), start, end)
        pairs = [(i.recurrence_id.date(), i.start.date()) for i in instances]
        assert pairs == [
            (dt.date(2026, 2, 25) + dt.timedelta(days=n), dt.date(2026, 3, 5 + n))
            for n in range(5)
        ]

    def test_cancelled_thisandfuture_truncates_series(self):
        future = """BEGIN:VEVENT
UID:standup
DTSTAMP:20260102T000000Z
RECURRENCE-ID;TZID=America/New_York;RANGE=THISANDFUTURE:20260311T090000
DTSTART;TZID=America/New_York:20260311T090000
SEQUENCE:1
STATUS:CANCELLED
END:VEVENT
"""
        start, end = ny_window(9, 15)
        instances = expand(cal(MASTER_NY, future), start, end)
        assert [i.recurrence_id.day for i in instances] == [9, 10]


class TestVersionResolution:
    def two_overrides(self, first, second):
        return cal(MASTER_NY, first, second)

    def test_higher_sequence_wins(self):
        older = """BEGIN:VEVENT
UID:standup
DTSTAMP:20260102T000000Z
RECURRENCE-ID;TZID=America/New_York:20260311T090000
DTSTART;TZID=America/New_York:20260311T100000
SEQUENCE:1
END:VEVENT
"""
        newer = older.replace("SEQUENCE:1", "SEQUENCE:2").replace(
            "20260311T100000", "20260311T120000"
        )
        start, end = ny_window(11, 12)
        instances = expand(self.two_overrides(older, newer), start, end)
        assert [i.start.hour for i in instances] == [12]

    def test_equal_sequence_uses_dtstamp(self):
        older = """BEGIN:VEVENT
UID:standup
DTSTAMP:20260102T000000Z
RECURRENCE-ID;TZID=America/New_York:20260311T090000
DTSTART;TZID=America/New_York:20260311T100000
SEQUENCE:1
END:VEVENT
"""
        newer = older.replace("20260102T000000Z", "20260103T000000Z").replace(
            "20260311T100000", "20260311T130000"
        )
        start, end = ny_window(11, 12)
        instances = expand(self.two_overrides(older, newer), start, end)
        assert [i.start.hour for i in instances] == [13]

    def test_unresolvable_tie_is_a_conflict(self):
        one = """BEGIN:VEVENT
UID:standup
DTSTAMP:20260102T000000Z
RECURRENCE-ID;TZID=America/New_York:20260311T090000
DTSTART;TZID=America/New_York:20260311T100000
SEQUENCE:1
END:VEVENT
"""
        two = one.replace("20260311T100000", "20260311T120000")
        start, end = ny_window(11, 12)
        with pytest.raises(RecurrenceConflictError):
            expand(self.two_overrides(one, two), start, end)


class TestValueKinds:
    def test_all_day_date_event(self):
        body = """BEGIN:VEVENT
UID:allday
DTSTAMP:20260101T000000Z
DTSTART;VALUE=DATE:20260301
DTEND;VALUE=DATE:20260302
RRULE:FREQ=DAILY;COUNT=3
END:VEVENT
"""
        instances = expand(cal(body), dt.date(2026, 3, 2), dt.date(2026, 3, 4))
        assert [i.start for i in instances] == [dt.date(2026, 3, 2), dt.date(2026, 3, 3)]
        assert all(isinstance(i.start, dt.date) and not isinstance(i.start, dt.datetime)
                   for i in instances)
        assert instances[0].end == dt.date(2026, 3, 3)

    def test_all_day_event_without_end_lasts_one_day(self):
        body = """BEGIN:VEVENT
UID:allday
DTSTAMP:20260101T000000Z
DTSTART;VALUE=DATE:20260301
RRULE:FREQ=DAILY;COUNT=2
END:VEVENT
"""
        instances = expand(cal(body), dt.date(2026, 3, 1), dt.date(2026, 3, 3))
        assert [(i.start, i.end) for i in instances] == [
            (dt.date(2026, 3, 1), dt.date(2026, 3, 2)),
            (dt.date(2026, 3, 2), dt.date(2026, 3, 3)),
        ]

    def test_floating_event_requires_interpretation_timezone(self):
        body = """BEGIN:VEVENT
UID:float
DTSTAMP:20260101T000000Z
DTSTART:20260301T090000
DURATION:PT1H
RRULE:FREQ=DAILY;COUNT=3
END:VEVENT
"""
        with pytest.raises(FloatingTimeError):
            expand(cal(body), dt.datetime(2026, 3, 1), dt.datetime(2026, 3, 4))

    def test_floating_event_interpreted_in_given_timezone(self):
        body = """BEGIN:VEVENT
UID:float
DTSTAMP:20260101T000000Z
DTSTART:20260301T090000
DURATION:PT1H
RRULE:FREQ=DAILY;COUNT=3
END:VEVENT
"""
        instances = expand(
            cal(body),
            dt.datetime(2026, 3, 1),
            dt.datetime(2026, 3, 4),
            floating_tz=BERLIN,
        )
        assert [i.start for i in instances] == [
            dt.datetime(2026, 3, day, 9, 0, tzinfo=BERLIN) for day in (1, 2, 3)
        ]

    def test_aware_event_rejects_naive_window(self):
        with pytest.raises(RecurrenceError):
            expand(cal(MASTER_NY), dt.datetime(2026, 3, 1), dt.datetime(2026, 3, 4))


class TestBoundedness:
    def test_max_instances_flags_truncation(self):
        result = expand_instances(
            cal(MASTER_NY), *ny_window(1, 31), max_instances=5
        )
        assert result.truncated
        assert len(result.instances) <= 5

    def test_sufficient_cap_is_not_truncated(self):
        result = expand_instances(
            cal(MASTER_NY), *ny_window(1, 4), max_instances=100
        )
        assert not result.truncated
        assert len(result.instances) == 2  # series starts on Mar 2


class TestRuleEdgeCases:
    def test_daily_event_keeps_wall_clock_across_dst(self):
        # US DST starts 2026-03-08: 9:00 wall clock must survive
        start, end = ny_window(6, 11)
        instances = expand(cal(MASTER_NY), start, end)
        assert [(i.start.hour, i.start.utcoffset()) for i in instances] == [
            (9, dt.timedelta(hours=-5)),
            (9, dt.timedelta(hours=-5)),
            (9, dt.timedelta(hours=-4)),
            (9, dt.timedelta(hours=-4)),
            (9, dt.timedelta(hours=-4)),
        ]

    def test_month_end_rule_skips_short_months(self):
        body = """BEGIN:VEVENT
UID:monthend
DTSTAMP:20260101T000000Z
DTSTART:20260131T090000Z
DURATION:PT1H
RRULE:FREQ=MONTHLY
END:VEVENT
"""
        start = dt.datetime(2026, 1, 1, tzinfo=UTC)
        end = dt.datetime(2026, 7, 1, tzinfo=UTC)
        instances = expand(cal(body), start, end)
        assert [i.start.date() for i in instances] == [
            dt.date(2026, 1, 31),
            dt.date(2026, 3, 31),
            dt.date(2026, 5, 31),
        ]
