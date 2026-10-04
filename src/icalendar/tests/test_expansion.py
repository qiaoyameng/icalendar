from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from icalendar import Calendar
from icalendar.error import InvalidCalendar
from icalendar.expansion import EventInstance, expand_events


UTC = timezone.utc
NY = ZoneInfo("America/New_York")
BERLIN = ZoneInfo("Europe/Berlin")


def calendar_from_events(*events: str) -> Calendar:
    return Calendar.from_ical(
        "\n".join(
            [
                "BEGIN:VCALENDAR",
                "VERSION:2.0",
                "PRODID:test",
                *events,
                "END:VCALENDAR",
            ]
        )
    )


def event(
    uid: str,
    dtstart: str,
    *,
    dtend: str | None = None,
    duration: str | None = None,
    rrule: str | None = None,
    rdate: str | None = None,
    exdate: str | None = None,
    recurrence_id: str | None = None,
    range_: str | None = None,
    sequence: int | None = None,
    dtstamp: str = "20240101T000000Z",
    status: str | None = None,
    tzid: str | None = None,
    recurrence_tzid: str | None = None,
    value_date: bool = False,
) -> str:
    params = ""
    if tzid:
        params += f";TZID={tzid}"
    if value_date:
        params += ";VALUE=DATE"
    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{dtstamp}",
        f"DTSTART{params}:{dtstart}",
    ]
    if dtend:
        lines.append(f"DTEND{params}:{dtend}")
    if duration:
        lines.append(f"DURATION:{duration}")
    if rrule:
        lines.append(f"RRULE:{rrule}")
    if rdate:
        lines.append(f"RDATE{params}:{rdate}")
    if exdate:
        lines.append(f"EXDATE{params}:{exdate}")
    if recurrence_id:
        rid_params = ""
        if recurrence_tzid:
            rid_params += f";TZID={recurrence_tzid}"
        if value_date:
            rid_params += ";VALUE=DATE"
        if range_:
            rid_params += f";RANGE={range_}"
        lines.append(f"RECURRENCE-ID{rid_params}:{recurrence_id}")
    if sequence is not None:
        lines.append(f"SEQUENCE:{sequence}")
    if status:
        lines.append(f"STATUS:{status}")
    lines.append("END:VEVENT")
    return "\n".join(lines)


def instances(cal, start, end, **kwargs):
    return expand_events(cal.events, start, end, **kwargs).instances


def test_single_override_moves_instance_into_window():
    cal = calendar_from_events(
        event("u", "20240101T100000", dtend="20240101T110000", rrule="FREQ=DAILY"),
        event(
            "u",
            "20240110T150000",
            dtend="20240110T160000",
            recurrence_id="20240101T100000",
            sequence=1,
        ),
    )

    result = instances(
        cal,
        datetime(2024, 1, 10, 14, tzinfo=UTC),
        datetime(2024, 1, 10, 17, tzinfo=UTC),
        floating_timezone=UTC,
    )

    assert [(i.recurrence_id, i.start, i.end, i.status) for i in result] == [
        (
            datetime(2024, 1, 1, 10),
            datetime(2024, 1, 10, 15),
            datetime(2024, 1, 10, 16),
            None,
        )
    ]


def test_single_override_moves_instance_out_of_window():
    cal = calendar_from_events(
        event(
            "u",
            "20240110T100000",
            dtend="20240110T110000",
            rrule="FREQ=DAILY",
            tzid="UTC",
        ),
        event(
            "u",
            "20240120T150000",
            dtend="20240120T160000",
            recurrence_id="20240110T100000",
            recurrence_tzid="UTC",
            tzid="UTC",
            sequence=1,
        ),
    )

    result = instances(
        cal,
        datetime(2024, 1, 10, tzinfo=UTC),
        datetime(2024, 1, 11, tzinfo=UTC),
    )

    assert result == ()
    result_on_new_day = instances(
        cal,
        datetime(2024, 1, 20, tzinfo=UTC),
        datetime(2024, 1, 21, tzinfo=UTC),
    )
    assert {instance.recurrence_id for instance in result_on_new_day} == {
        datetime(2024, 1, 10, 10, tzinfo=UTC),
        datetime(2024, 1, 20, 10, tzinfo=UTC),
    }
    moved = next(
        instance
        for instance in result_on_new_day
        if instance.recurrence_id == datetime(2024, 1, 10, 10, tzinfo=UTC)
    )
    assert moved.start == datetime(2024, 1, 20, 15, tzinfo=UTC)


def test_cancelled_override_and_exdate_omit_identity():
    cal = calendar_from_events(
        event(
            "u",
            "20240101T100000",
            dtend="20240101T110000",
            rrule="FREQ=DAILY",
            exdate="20240103T100000",
        ),
        event(
            "u",
            "20240102T100000",
            dtend="20240102T110000",
            recurrence_id="20240102T100000",
            sequence=1,
            status="CANCELLED",
        ),
    )

    result = instances(
        cal,
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 5, tzinfo=UTC),
        floating_timezone=UTC,
    )

    assert [i.recurrence_id for i in result] == [
        datetime(2024, 1, 1, 10),
        datetime(2024, 1, 4, 10),
    ]


def test_thisandfuture_changes_time_and_duration_across_dst():
    cal = calendar_from_events(
        event(
            "u",
            "20240308T140000",
            dtend="20240308T150000",
            rrule="FREQ=DAILY",
            tzid="America/New_York",
        ),
        event(
            "u",
            "20240310T160000",
            duration="PT2H",
            recurrence_id="20240310T140000",
            recurrence_tzid="America/New_York",
            range_="THISANDFUTURE",
            tzid="America/New_York",
            sequence=1,
        ),
    )

    result = instances(
        cal,
        datetime(2024, 3, 8, tzinfo=UTC),
        datetime(2024, 3, 13, tzinfo=UTC),
    )

    assert [(i.start, i.end) for i in result] == [
        (datetime(2024, 3, 8, 14, tzinfo=NY), datetime(2024, 3, 8, 15, tzinfo=NY)),
        (datetime(2024, 3, 9, 14, tzinfo=NY), datetime(2024, 3, 9, 15, tzinfo=NY)),
        (datetime(2024, 3, 10, 16, tzinfo=NY), datetime(2024, 3, 10, 18, tzinfo=NY)),
        (datetime(2024, 3, 11, 16, tzinfo=NY), datetime(2024, 3, 11, 18, tzinfo=NY)),
        (datetime(2024, 3, 12, 16, tzinfo=NY), datetime(2024, 3, 12, 18, tzinfo=NY)),
    ]


def test_nominal_day_duration_preserves_local_time_across_dst():
    cal = calendar_from_events(
        event(
            "u",
            "20241102T000000",
            duration="P1D",
            rrule="FREQ=DAILY;COUNT=2",
            tzid="America/New_York",
        )
    )

    result = instances(
        cal,
        datetime(2024, 11, 1, tzinfo=UTC),
        datetime(2024, 11, 5, tzinfo=UTC),
    )

    assert [(i.start, i.end, i.duration) for i in result] == [
        (
            datetime(2024, 11, 2, tzinfo=NY),
            datetime(2024, 11, 3, tzinfo=NY),
            timedelta(days=1),
        ),
        (
            datetime(2024, 11, 3, tzinfo=NY),
            datetime(2024, 11, 4, tzinfo=NY),
            timedelta(days=1),
        ),
    ]


def test_thisandfuture_cancels_future_identities():
    cal = calendar_from_events(
        event("u", "20240131", duration="P1D", rrule="FREQ=MONTHLY;BYMONTHDAY=-1", value_date=True),
        event(
            "u",
            "20240331",
            recurrence_id="20240229",
            range_="THISANDFUTURE",
            value_date=True,
            sequence=1,
            status="CANCELLED",
        ),
    )

    result = instances(cal, date(2024, 1, 1), date(2024, 6, 1))

    assert [(i.recurrence_id, i.start, i.end) for i in result] == [
        (date(2024, 1, 31), date(2024, 1, 31), date(2024, 2, 1)),
    ]


def test_rdate_duplicates_rule_identity_once():
    cal = calendar_from_events(
        event(
            "u",
            "20240101T100000",
            dtend="20240101T110000",
            rrule="FREQ=DAILY;COUNT=2",
            rdate="20240101T100000",
        )
    )

    result = instances(
        cal,
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 3, tzinfo=UTC),
        floating_timezone=UTC,
    )

    assert [i.recurrence_id for i in result] == [
        datetime(2024, 1, 1, 10),
        datetime(2024, 1, 2, 10),
    ]


def test_multiday_instance_overlaps_query_boundary_once():
    cal = calendar_from_events(
        event(
            "u",
            "20240201T120000",
            dtend="20240203T120000",
            rrule="FREQ=MONTHLY;COUNT=2",
            tzid="UTC",
        )
    )

    result = instances(
        cal,
        datetime(2024, 2, 2, tzinfo=UTC),
        datetime(2024, 2, 3, tzinfo=UTC),
    )

    assert [i.recurrence_id for i in result] == [
        datetime(2024, 2, 1, 12, tzinfo=UTC)
    ]


def test_date_floating_and_tzid_are_separate_identities():
    cal = calendar_from_events(
        event("day", "20240101", duration="P1D", value_date=True),
        event("float", "20240101T010000", duration="PT1H"),
        event(
            "zone",
            "20240101T010000",
            duration="PT1H",
            tzid="Europe/Berlin",
        ),
    )

    with pytest.raises(InvalidCalendar, match="floating_timezone"):
        instances(
            cal,
            datetime(2024, 1, 1, tzinfo=UTC),
            datetime(2024, 1, 2, tzinfo=UTC),
        )

    result = instances(
        cal,
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 2, tzinfo=UTC),
        floating_timezone=BERLIN,
    )
    assert len(result) == 3
    assert {type(i.recurrence_id) for i in result} == {date, datetime}
    datetimes = [i.start for i in result if isinstance(i.start, datetime)]
    assert sorted(datetimes, key=lambda value: (value.tzinfo is not None, value)) == [
        datetime(2024, 1, 1, 1),
        datetime(2024, 1, 1, 1, tzinfo=BERLIN),
    ]
    interpreted = [
        value.replace(tzinfo=BERLIN) if value.tzinfo is None else value
        for value in datetimes
    ]
    assert all(value.tzinfo == BERLIN for value in interpreted)


def test_infinite_rule_reports_truncation():
    cal = calendar_from_events(
        event("u", "20240101T000000", dtend="20240101T000000", rrule="FREQ=DAILY")
    )

    result = expand_events(
        cal.events,
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 31, tzinfo=UTC),
        floating_timezone=UTC,
        max_instances=5,
    )

    assert len(result.instances) == 5
    assert result.instances[0].start == datetime(2024, 1, 1)
    assert result.truncated is True
    assert result.complete is False


def test_completed_window_is_not_truncated():
    cal = calendar_from_events(
        event(
            "u",
            "20240101T000000",
            dtend="20240101T000000",
            rrule="FREQ=DAILY;COUNT=2",
        )
    )

    result = expand_events(
        cal.events,
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 31, tzinfo=UTC),
        floating_timezone=UTC,
        max_instances=5,
    )

    assert len(result.instances) == 2
    assert result.truncated is False
    assert result.complete is True


def test_equal_sequence_and_dtstamp_conflict_is_explicit():
    cal = calendar_from_events(
        event("u", "20240101T100000", duration="PT1H", rrule="FREQ=DAILY"),
        event(
            "u",
            "20240102T120000",
            duration="PT1H",
            recurrence_id="20240102T100000",
            sequence=1,
            dtstamp="20240102T000000Z",
        ),
        event(
            "u",
            "20240102T130000",
            duration="PT1H",
            recurrence_id="20240102T100000",
            sequence=1,
            dtstamp="20240102T000000Z",
        ),
    )

    with pytest.raises(InvalidCalendar, match="Conflicting"):
        expand_events(
            cal.events,
            datetime(2024, 1, 2, tzinfo=UTC),
            datetime(2024, 1, 3, tzinfo=UTC),
            floating_timezone=UTC,
        )


def test_event_instance_exposes_source_event():
    cal = calendar_from_events(event("u", "20240101", duration="P1D", value_date=True))
    source = cal.events[0]

    result = instances(cal, date(2024, 1, 1), date(2024, 1, 2))

    assert list(result) == [
        EventInstance(
            uid="u",
            recurrence_id=date(2024, 1, 1),
            start=date(2024, 1, 1),
            end=date(2024, 1, 2),
            duration=timedelta(days=1),
            status=None,
            event=source,
        )
    ]
    assert result[0].event is source
