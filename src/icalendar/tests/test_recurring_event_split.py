from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from icalendar import Calendar, Event, InvalidCalendar
from icalendar.prop import vRecur


NEW_YORK = ZoneInfo("America/New_York")
UTC = timezone.utc


def make_calendar(*events: Event) -> Calendar:
    calendar = Calendar.new(prodid="-//test//EN")
    for event in events:
        calendar.add_component(event)
    return calendar


def recurring_event(
    uid: str = "series",
    *,
    start: date | datetime | None = None,
    end: date | datetime | None = None,
    rrule: str | None = "FREQ=DAILY;COUNT=10",
) -> Event:
    start = start or datetime(2024, 3, 10, 9, tzinfo=NEW_YORK)
    if end is None:
        end = (
            start + timedelta(hours=1)
            if isinstance(start, datetime)
            else start + timedelta(days=1)
        )
    event = Event.new(
        uid=uid,
        start=start,
        end=end,
        stamp=datetime(2024, 1, 1, tzinfo=UTC),
        summary="Original",
    )
    if rrule:
        event.add("RRULE", vRecur.from_ical(rrule))
    return event


def override(
    uid: str,
    recurrence_id: date | datetime,
    *,
    start: date | datetime,
    end: date | datetime,
    summary: str = "Changed",
    this_and_future: bool = False,
    status: str | None = None,
) -> Event:
    event = Event.new(
        uid=uid,
        start=start,
        end=end,
        stamp=datetime(2024, 1, 2, tzinfo=UTC),
        summary=summary,
        status=status,
    )
    event.RECURRENCE_ID = recurrence_id
    if this_and_future:
        event["RECURRENCE-ID"].params["RANGE"] = "THISANDFUTURE"
    return event


def rrule_dates(event: Event) -> list[date | datetime]:
    from dateutil.rrule import rrulestr

    rules = event.get("RRULE", [])
    if not isinstance(rules, list):
        rules = [rules]
    dates: list[date | datetime] = []
    for rule in rules:
        dates.extend(rrulestr(rule.to_ical().decode(), dtstart=event.start))
    return dates


def expand(
    calendar: Calendar, uid: str
) -> dict[date | datetime, tuple[date | datetime, date | datetime, str]]:
    master = next(
        event
        for event in calendar.events
        if str(event.uid) == uid and "RECURRENCE-ID" not in event
    )
    components = {master.start: master}
    for event in calendar.events:
        if str(event.uid) != uid or "RECURRENCE-ID" not in event:
            continue
        if event["RECURRENCE-ID"].params.get("RANGE") == "THISANDFUTURE":
            continue
        components[event.RECURRENCE_ID] = event
    starts = [master.start, *rrule_dates(master)]
    starts.extend(rdate[0] for rdate in master.rdates)
    occurrences = {}
    for start in sorted(set(starts)):
        if start in set(master.exdates):
            continue
        component = components.get(start, master)
        duration = component.end - component.start
        occurrences[start] = (start, start + duration, str(component.summary))
    return occurrences


def test_split_count_ignores_exdates_and_cancelled_occurrences():
    split_at = datetime(2024, 3, 14, 9, tzinfo=NEW_YORK)
    master = recurring_event()
    master.add("EXDATE", datetime(2024, 3, 11, 9, tzinfo=NEW_YORK))
    cancelled = override(
        "series",
        datetime(2024, 3, 15, 9, tzinfo=NEW_YORK),
        start=datetime(2024, 3, 15, 9, tzinfo=NEW_YORK),
        end=datetime(2024, 3, 15, 10, tzinfo=NEW_YORK),
        summary="Cancelled",
        status="CANCELLED",
    )
    calendar = make_calendar(master, cancelled)
    before = expand(calendar, "series")

    old, new = calendar.split_recurring_event("series", split_at, new_uid="new-series")

    assert old.rrules[0].count == 4
    assert new.rrules[0].count == 6
    assert old.exdates == [datetime(2024, 3, 11, 9, tzinfo=NEW_YORK)]
    assert new.exdates == []
    moved_cancelled = next(
        e
        for e in calendar.events
        if "RECURRENCE-ID" in e and str(e.uid) == "new-series"
    )
    assert moved_cancelled.RECURRENCE_ID == datetime(2024, 3, 15, 9, tzinfo=NEW_YORK)
    after = {**expand(calendar, "series"), **expand(calendar, "new-series")}
    assert list(after) == list(before)


def test_split_at_single_override_preserves_exception_identity():
    split_at = datetime(2024, 3, 14, 9, tzinfo=NEW_YORK)
    changed = override(
        "series",
        split_at,
        start=datetime(2024, 3, 14, 15, tzinfo=NEW_YORK),
        end=datetime(2024, 3, 14, 16, tzinfo=NEW_YORK),
    )
    calendar = make_calendar(recurring_event(), changed)

    old, new = calendar.split_recurring_event(
        "series",
        split_at,
        new_uid="new-series",
        summary="Future title",
    )

    assert "RECURRENCE-ID" not in new
    assert new.start == split_at
    assert str(new.uid) == "new-series"
    assert str(new.summary) == "Future title"
    moved_changed = next(
        e
        for e in calendar.events
        if "RECURRENCE-ID" in e and str(e.uid) == "new-series"
    )
    assert moved_changed.RECURRENCE_ID == split_at
    assert moved_changed.start == datetime(2024, 3, 14, 15, tzinfo=NEW_YORK)
    assert old.rrules[0].count == 4
    assert new.rrules[0].count == 6


def test_split_at_thisandfuture_boundary_promotes_override():
    split_at = datetime(2024, 3, 14, 9, tzinfo=NEW_YORK)
    boundary = override(
        "series",
        split_at,
        start=datetime(2024, 3, 14, 11, tzinfo=NEW_YORK),
        end=datetime(2024, 3, 14, 12, tzinfo=NEW_YORK),
        this_and_future=True,
    )
    later_change = override(
        "series",
        datetime(2024, 3, 16, 9, tzinfo=NEW_YORK),
        start=datetime(2024, 3, 16, 12, tzinfo=NEW_YORK),
        end=datetime(2024, 3, 16, 13, tzinfo=NEW_YORK),
    )
    calendar = make_calendar(recurring_event(), boundary, later_change)

    old, new = calendar.split_recurring_event("series", split_at, new_uid="new-series")

    assert "RECURRENCE-ID" not in new
    assert new.start == datetime(2024, 3, 14, 11, tzinfo=NEW_YORK)
    assert new.rrules[0].count == 6
    assert any(
        str(e.uid) == "new-series"
        for e in calendar.events
        if "RECURRENCE-ID" in e
    )
    assert old.rrules[0].count == 4


def test_split_distributes_rdates_and_exdates():
    master = recurring_event(rrule=None)
    values = [
        datetime(2024, 3, 10, 9, tzinfo=NEW_YORK),
        datetime(2024, 3, 12, 9, tzinfo=NEW_YORK),
        datetime(2024, 3, 14, 9, tzinfo=NEW_YORK),
        datetime(2024, 3, 16, 9, tzinfo=NEW_YORK),
    ]
    master.add("RDATE", values[1:])
    master.add("EXDATE", [values[1], values[3]])
    calendar = make_calendar(master)

    old, new = calendar.split_recurring_event(
        "series", values[2], new_uid="new-series"
    )

    assert old.rdates == [(values[1], None)]
    assert new.rdates == [(values[3], None)]
    assert old.exdates == [values[1]]
    assert new.exdates == [values[3]]
    assert new.start == values[2]
    assert "RRULE" not in new


def test_split_dst_preserves_local_wall_time_and_properties():
    start = datetime(2024, 3, 7, 9, tzinfo=NEW_YORK)
    master = recurring_event(
        start=start,
        end=start + timedelta(hours=1),
        rrule="FREQ=DAILY;COUNT=6",
    )
    split_at = datetime(2024, 3, 10, 9, tzinfo=NEW_YORK)
    calendar = make_calendar(master)
    before = dict(expand(calendar, "series"))

    old, new = calendar.split_recurring_event("series", split_at, new_uid="new-series")

    after = {
        key: value
        for series in ("series", "new-series")
        for key, value in expand(calendar, series).items()
    }
    assert old.rrules[0].count == 3
    assert new.rrules[0].count == 3
    assert new.start == split_at
    assert after == before


def test_split_unbounded_rule_truncates_old_series_with_until():
    start = datetime(2024, 3, 7, 9, tzinfo=NEW_YORK)
    master = recurring_event(
        start=start, end=start + timedelta(hours=1), rrule="FREQ=DAILY"
    )
    split_at = datetime(2024, 3, 10, 9, tzinfo=NEW_YORK)
    calendar = make_calendar(master)

    old, new = calendar.split_recurring_event("series", split_at, new_uid="new-series")

    assert "UNTIL" in old.rrules[0]
    assert "COUNT" not in old.rrules[0]
    assert not new.rrules[0].keys() & {"COUNT", "UNTIL"}
    assert rrule_dates(old) == [
        datetime(2024, 3, 7, 9, tzinfo=NEW_YORK),
        datetime(2024, 3, 8, 9, tzinfo=NEW_YORK),
        datetime(2024, 3, 9, 9, tzinfo=NEW_YORK),
    ]


def test_bounded_rrule_split_at_rdate_materializes_without_losing_instances():
    master = recurring_event(rrule="FREQ=DAILY;UNTIL=20240316T130000Z")
    master.add("RDATE", datetime(2024, 3, 14, 15, tzinfo=NEW_YORK))
    split_at = datetime(2024, 3, 14, 15, tzinfo=NEW_YORK)
    calendar = make_calendar(master)

    old, new = calendar.split_recurring_event("series", split_at, new_uid="new-series")

    assert "RRULE" not in old
    assert "RRULE" not in new
    assert new.start == split_at
    assert datetime(2024, 3, 14, 9, tzinfo=NEW_YORK) in dict(old.rdates)
    assert datetime(2024, 3, 15, 9, tzinfo=NEW_YORK) in dict(new.rdates)
    assert datetime(2024, 3, 16, 9, tzinfo=NEW_YORK) in dict(new.rdates)


def test_split_date_only_rrule_preserves_value_type():
    master = recurring_event(
        start=date(2024, 3, 10),
        end=date(2024, 3, 11),
        rrule="FREQ=DAILY;UNTIL=20240320",
    )
    calendar = make_calendar(master)

    old, new = calendar.split_recurring_event(
        "series", date(2024, 3, 14), new_uid="new-series"
    )

    assert old.rrules[0]["UNTIL"] == [date(2024, 3, 13)]
    assert new.start == date(2024, 3, 14)
    assert new.end == date(2024, 3, 15)
    assert b"DTSTART;VALUE=DATE:20240314" in new.to_ical()


def test_split_rejects_point_not_in_original_series():
    calendar = make_calendar(recurring_event())

    with pytest.raises(InvalidCalendar, match="original recurrence set"):
        calendar.split_recurring_event(
            "series",
            datetime(2024, 3, 14, 10, tzinfo=NEW_YORK),
            new_uid="new-series",
        )


def test_split_reports_rdate_only_first_occurrence_as_lossless_limit():
    master = recurring_event(rrule=None)
    master.add("RDATE", datetime(2024, 3, 12, 9, tzinfo=NEW_YORK))
    calendar = make_calendar(master)

    with pytest.raises(InvalidCalendar, match="losslessly"):
        calendar.split_recurring_event(
            "series",
            datetime(2024, 3, 10, 9, tzinfo=NEW_YORK),
            new_uid="new-series",
        )
    assert calendar.events[0].uid == "series"


def test_split_reports_unbounded_non_rule_occurrence_as_lossless_limit():
    master = recurring_event(rrule="FREQ=DAILY")
    master.add("RDATE", datetime(2024, 3, 10, 15, tzinfo=NEW_YORK))
    calendar = make_calendar(master)

    with pytest.raises(InvalidCalendar, match="losslessly"):
        calendar.split_recurring_event(
            "series",
            datetime(2024, 3, 10, 15, tzinfo=NEW_YORK),
            new_uid="new-series",
        )
    assert len(calendar.events) == 1


def test_split_does_not_accept_recurrence_identity_properties():
    calendar = make_calendar(recurring_event())

    with pytest.raises(ValueError, match="recurrence identity"):
        calendar.split_recurring_event(
            "series",
            datetime(2024, 3, 14, 9, tzinfo=NEW_YORK),
            new_uid="new-series",
            properties={"RECURRENCE-ID": datetime(2024, 3, 14, 9, tzinfo=NEW_YORK)},
        )


def test_split_input_is_deep_copied():
    master = recurring_event()
    calendar = make_calendar(master)
    snapshot = deepcopy(calendar)

    with pytest.raises(Exception):  # intentionally invalid update
        calendar.split_recurring_event(
            "series",
            datetime(2024, 3, 14, 9, tzinfo=NEW_YORK),
            new_uid="new-series",
            DTEND=datetime(1999, 1, 1, tzinfo=UTC),
        )

    assert calendar == snapshot
