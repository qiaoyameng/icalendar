from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from dateutil.rrule import rrulestr

from icalendar import Calendar, Event, RecurrenceSplitError


NY = ZoneInfo("America/New_York")


def make_event(uid="old", start=None, **properties):
    event = Event()
    event.uid = uid
    event.DTSTAMP = datetime(2024, 1, 1, tzinfo=timezone.utc)
    event.DTSTART = start or datetime(2024, 3, 1, 9, tzinfo=NY)
    event.DTEND = event.DTSTART + timedelta(hours=1)
    for name, value in properties.items():
        event.add(name, value)
    return event


def make_calendar(*events):
    calendar = Calendar()
    for event in events:
        calendar.add_component(event)
    return calendar


def occurrences(calendar, uid, start, stop):
    """Small deterministic recurrence expansion used by these tests."""
    master = next(
        event
        for event in calendar.events
        if str(event.uid) == uid and "RECURRENCE-ID" not in event
    )
    starts = {master.DTSTART}
    rdate_ends = {}
    for entry_start, entry_end in master.rdates:
        starts.add(entry_start)
        rdate_ends[entry_start] = entry_end
    for rule in master.rrules:
        generated = rrulestr(
            rule.to_ical().decode(), dtstart=master.DTSTART
        ).between(start, stop, inc=True)
        if isinstance(master.DTSTART, date) and not isinstance(master.DTSTART, datetime):
            generated = [item.date() for item in generated]
        starts.update(generated)
    raw_exdates = master.get("EXDATE", [])
    values = raw_exdates if isinstance(raw_exdates, list) else [raw_exdates]
    for group in values:
        for item in group.dts:
            starts.discard(item.dt)
    result = {}
    for current in sorted(starts):
        if start <= current < stop:
            result[current] = (
                current,
                rdate_ends.get(current, current + master.duration),
                master,
            )
    for exception in calendar.events:
        if str(exception.uid) == uid and "RECURRENCE-ID" in exception:
            result[exception.RECURRENCE_ID] = (
                exception.start,
                exception.end,
                exception,
            )
    return {
        current: (start, end, public_view(event))
        for current, (start, end, event) in sorted(result.items())
    }


def public_view(event):
    return {
        key: value
        for key, value in event.property_items(sorted=False)
        if key
        not in {
            "UID",
            "RRULE",
            "RDATE",
            "EXDATE",
            "RECURRENCE-ID",
            "DTSTART",
            "DTEND",
            "DURATION",
        }
    }


def expanded_views(calendar, uids, start, stop):
    result = {}
    for uid in uids:
        result.update(occurrences(calendar, uid, start, stop))
    return dict(sorted(result.items()))


def test_split_count_rule_uses_rule_index_not_visible_occurrences():
    master = make_event()
    master.add("RRULE", "FREQ=DAILY;COUNT=5")
    master.add("EXDATE", [datetime(2024, 3, 2, 9, tzinfo=NY)])
    cancelled = make_event(start=datetime(2024, 3, 2, 9, tzinfo=NY), STATUS="CANCELLED")
    cancelled.uid = "old"
    cancelled.RECURRENCE_ID = cancelled.DTSTART
    changed = make_event(summary="Changed", start=datetime(2024, 3, 3, 10, tzinfo=NY))
    changed.uid = "old"
    changed.RECURRENCE_ID = datetime(2024, 3, 3, 9, tzinfo=NY)
    calendar = make_calendar(master, cancelled, changed)

    result = calendar.split_recurring_event(
        "old", datetime(2024, 3, 3, 9, tzinfo=NY), "new"
    )

    assert result.first.rrules[0].count == 2
    assert result.second.rrules[0].count == 3
    assert result.first.exdates == [datetime(2024, 3, 2, 9, tzinfo=NY)]
    assert result.second.exdates == []
    assert str(result.second.uid) == "new"
    changed_after = next(
        event
        for event in calendar.events
        if str(event.uid) == "new" and event.RECURRENCE_ID is not None
    )
    assert str(changed_after.summary) == "Changed"
    assert changed_after.RECURRENCE_ID == datetime(2024, 3, 3, 9, tzinfo=NY)


def test_split_until_rule_and_rdates_preserve_dst_times():
    master = make_event()
    master.add("RRULE", "FREQ=WEEKLY;UNTIL=20240407T130000Z")
    master.add(
        "RDATE",
        [
            datetime(2024, 3, 10, 9, tzinfo=NY),
            datetime(2024, 3, 20, 9, tzinfo=NY),
            datetime(2024, 3, 27, 9, tzinfo=NY),
        ],
    )
    master.add("EXDATE", [datetime(2024, 3, 8, 9, tzinfo=NY)])
    calendar = make_calendar(master)
    before = calendar.to_ical()

    result = calendar.split_recurring_event(
        "old", datetime(2024, 3, 15, 9, tzinfo=NY), "new"
    )

    assert result.first.rrules[0]["UNTIL"][0] == datetime(
        2024, 3, 8, 14, tzinfo=timezone.utc
    )
    assert result.first.rdates == [(datetime(2024, 3, 10, 9, tzinfo=NY), None)]
    assert result.second.rdates == [
        (datetime(2024, 3, 20, 9, tzinfo=NY), None),
        (datetime(2024, 3, 27, 9, tzinfo=NY), None),
    ]
    assert result.second.DTSTART == datetime(2024, 3, 15, 9, tzinfo=NY)
    assert b"20240315T090000" in calendar.to_ical()
    assert b"UNTIL=20240308T140000Z" in calendar.to_ical()
    assert b"UNTIL=20240407T130000Z" in calendar.to_ical()
    assert before != calendar.to_ical()


def test_expansion_is_unchanged_across_dst_window():
    master = make_event(summary="Meeting")
    master.add("RRULE", "FREQ=WEEKLY;UNTIL=20240405T130000Z")
    master.add(
        "RDATE",
        [
            datetime(2024, 3, 13, 9, tzinfo=NY),
            datetime(2024, 3, 20, 9, tzinfo=NY),
        ],
    )
    master.add("EXDATE", [datetime(2024, 3, 8, 9, tzinfo=NY)])
    modified = make_event(summary="Changed")
    modified.DTSTART = datetime(2024, 3, 22, 9, tzinfo=NY)
    modified.DTEND = datetime(2024, 3, 22, 10, tzinfo=NY)
    modified.uid = "old"
    modified.RECURRENCE_ID = datetime(2024, 3, 22, 9, tzinfo=NY)
    calendar = make_calendar(master, modified)
    start = datetime(2024, 3, 1, tzinfo=timezone.utc)
    stop = datetime(2024, 4, 8, tzinfo=timezone.utc)
    before = expanded_views(calendar, ["old"], start, stop)

    calendar.split_recurring_event(
        "old", datetime(2024, 3, 15, 9, tzinfo=NY), "new"
    )

    after = expanded_views(calendar, ["old", "new"], start, stop)
    assert after == before
    assert {
        current.tzname() for current, event in after.items() if current.year == 2024
    } == {"EST", "EDT"}


def test_split_at_first_occurrence_removes_empty_front():
    master = make_event()
    master.add("RRULE", "FREQ=DAILY;COUNT=2")
    calendar = make_calendar(master)

    result = calendar.split_recurring_event(
        "old", datetime(2024, 3, 1, 9, tzinfo=NY), "new"
    )

    assert result.first is None
    assert result.second.rrules[0].count == 2
    assert result.second.exdates == [datetime(2024, 3, 1, 9, tzinfo=NY)]
    assert {str(event.uid) for event in calendar.events} == {"new"}


def test_rdate_only_split_moves_groups_by_start():
    master = make_event(start=datetime(2024, 3, 1, 9, tzinfo=NY))
    master.add(
        "RDATE",
        [
            datetime(2024, 3, 1, 9, tzinfo=NY),
            datetime(2024, 3, 8, 9, tzinfo=NY),
            datetime(2024, 3, 15, 9, tzinfo=NY),
        ],
    )
    master.add("EXDATE", [datetime(2024, 3, 8, 9, tzinfo=NY)])
    calendar = make_calendar(master)

    result = calendar.split_recurring_event(
        "old", datetime(2024, 3, 8, 9, tzinfo=NY), "new"
    )

    assert result.first.DTSTART == datetime(2024, 3, 1, 9, tzinfo=NY)
    assert "RRULE" not in result.first
    assert result.first.rdates == [(datetime(2024, 3, 1, 9, tzinfo=NY), None)]
    assert result.second.DTSTART == datetime(2024, 3, 8, 9, tzinfo=NY)
    assert result.second.rdates == [(datetime(2024, 3, 15, 9, tzinfo=NY), None)]
    assert result.second.exdates == [datetime(2024, 3, 8, 9, tzinfo=NY)]


def test_rdate_and_exdate_property_groups_keep_parameters():
    master = make_event(start=date(2024, 3, 1))
    master.DTSTART = date(2024, 3, 1)
    master.DTEND = date(2024, 3, 2)
    master.add(
        "RDATE",
        [date(2024, 3, 2), date(2024, 3, 3)],
        parameters={"VALUE": "DATE", "X-GROUP": "first"},
    )
    master.add(
        "RDATE",
        [date(2024, 3, 8)],
        parameters={"VALUE": "DATE", "X-GROUP": "second"},
    )
    master.add(
        "EXDATE",
        [date(2024, 3, 3)],
        parameters={"VALUE": "DATE", "X-GROUP": "keep"},
    )
    calendar = make_calendar(master)

    result = calendar.split_recurring_event("old", date(2024, 3, 3), "new")

    back_group = result.second.get("EXDATE")
    assert back_group.params["X-GROUP"] == "keep"
    back_rdates = result.second.get("RDATE")
    assert back_rdates.params["X-GROUP"] == "second"
    front_rdates = result.first.get("RDATE")
    assert front_rdates.params["X-GROUP"] == "first"


def test_thisandfuture_boundary_becomes_new_master():
    master = make_event(summary="Old")
    master.add("RRULE", "FREQ=DAILY;COUNT=4")
    boundary = make_event(summary="New", start=datetime(2024, 3, 3, 11, tzinfo=NY))
    boundary.uid = "old"
    boundary.RECURRENCE_ID = datetime(2024, 3, 3, 9, tzinfo=NY)
    boundary["RECURRENCE-ID"].params["RANGE"] = "THISANDFUTURE"
    calendar = make_calendar(master, boundary)

    result = calendar.split_recurring_event(
        "old", datetime(2024, 3, 3, 9, tzinfo=NY), "new"
    )

    assert result.first.rrules[0].count == 2
    assert str(result.second.summary) == "New"
    assert result.second.DTSTART == datetime(2024, 3, 3, 11, tzinfo=NY)
    assert result.second.rrules[0].count == 2
    assert "RECURRENCE-ID" not in result.second


def test_split_point_must_belong_to_original_series():
    master = make_event()
    master.add("RRULE", "FREQ=DAILY;COUNT=3")
    calendar = make_calendar(master)

    with pytest.raises(Exception, match="not an occurrence"):
        calendar.split_recurring_event(
            "old", datetime(2024, 3, 4, 12, tzinfo=NY), "new"
        )
    assert str(calendar.events[0].uid) == "old"


def test_orphan_exdate_and_recurrence_id_are_reported():
    exdate_master = make_event()
    exdate_master.add("RRULE", "FREQ=DAILY;COUNT=2")
    exdate_master.add("EXDATE", [datetime(2024, 3, 4, 9, tzinfo=NY)])
    exdate_calendar = make_calendar(exdate_master)

    with pytest.raises(Exception, match="EXDATE"):
        exdate_calendar.split_recurring_event(
            "old", datetime(2024, 3, 2, 9, tzinfo=NY), "new"
        )

    orphan = make_event(summary="Orphan")
    orphan.uid = "old"
    orphan.RECURRENCE_ID = datetime(2024, 3, 4, 9, tzinfo=NY)
    orphan_calendar = make_calendar(exdate_master, orphan)
    before = orphan_calendar.to_ical()

    with pytest.raises(Exception, match="RECURRENCE-ID"):
        orphan_calendar.split_recurring_event(
            "old", datetime(2024, 3, 2, 9, tzinfo=NY), "new"
        )
    assert orphan_calendar.to_ical() == before


def test_unsupported_rule_reports_limit_and_does_not_mutate():
    master = make_event()
    master.add("RRULE", "FREQ=DAILY;RSCALE=GREGORIAN")
    calendar = make_calendar(master)
    before = calendar.to_ical()

    with pytest.raises(RecurrenceSplitError):
        calendar.split_recurring_event(
            "old", datetime(2024, 3, 2, 9, tzinfo=NY), "new"
        )
    assert calendar.to_ical() == before


def test_extra_rdate_split_flattens_finite_rule_and_rejects_infinite_rule():
    master = make_event()
    master.add("RRULE", "FREQ=WEEKLY;COUNT=4")
    master.add("RDATE", [datetime(2024, 3, 3, 9, tzinfo=NY)])
    calendar = make_calendar(master)

    result = calendar.split_recurring_event(
        "old", datetime(2024, 3, 3, 9, tzinfo=NY), "new"
    )

    assert "RRULE" not in result.first
    assert result.first.rdates == [
        (datetime(2024, 3, 1, 9, tzinfo=NY), None),
    ]
    assert "RRULE" not in result.second
    assert result.second.rdates == [
        (datetime(2024, 3, 8, 9, tzinfo=NY), None),
        (datetime(2024, 3, 15, 9, tzinfo=NY), None),
        (datetime(2024, 3, 22, 9, tzinfo=NY), None),
    ]

    infinite = make_event()
    infinite.add("RRULE", "FREQ=WEEKLY")
    infinite.add("RDATE", [datetime(2024, 3, 3, 9, tzinfo=NY)])
    infinite_calendar = make_calendar(infinite)
    before = infinite_calendar.to_ical()

    with pytest.raises(RecurrenceSplitError, match="infinite RRULE"):
        infinite_calendar.split_recurring_event(
            "old", datetime(2024, 3, 3, 9, tzinfo=NY), "new"
        )
    assert infinite_calendar.to_ical() == before


def test_split_does_not_change_method_or_attendees_and_accepts_updates():
    master = make_event()
    master.add("RRULE", "FREQ=DAILY;COUNT=2")
    master.add("ATTENDEE", "mailto:user@example.com")
    calendar = make_calendar(master)
    calendar.method = "PUBLISH"

    result = calendar.split_recurring_event(
        "old",
        datetime(2024, 3, 2, 9, tzinfo=NY),
        "new",
        {"SUMMARY": "Renamed"},
    )

    assert str(calendar.method) == "PUBLISH"
    assert str(result.second.attendees[0]) == "mailto:user@example.com"
    assert str(result.second.summary) == "Renamed"


def test_all_day_events_split_by_dates():
    master = Event()
    master.uid = "old"
    master.DTSTAMP = datetime(2024, 1, 1, tzinfo=timezone.utc)
    master.DTSTART = date(2024, 3, 1)
    master.DTEND = date(2024, 3, 2)
    master.add("RRULE", "FREQ=DAILY;COUNT=3")
    calendar = make_calendar(master)

    result = calendar.split_recurring_event("old", date(2024, 3, 2), "new")

    assert result.first.rrules[0].count == 1
    assert result.second.rrules[0].count == 2
    assert result.second.DTSTART == date(2024, 3, 2)
