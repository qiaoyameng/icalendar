"""Split a recurring VEVENT into two independent series."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from icalendar.cal.calendar import Calendar
    from icalendar.cal.event import Event

from dateutil.rrule import rrule, rrulestr

from icalendar.error import InvalidCalendar
from icalendar.prop import vDDDLists, vRecur


@dataclass(frozen=True)
class _Occurrence:
    start: date | datetime
    end: date | datetime | None
    source: str


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _compare(value: date | datetime, other: date | datetime) -> int:
    if type(value) is not type(other):
        raise InvalidCalendar(
            "Recurrence dates must use the same value type as the split point."
        )
    if isinstance(value, datetime) and isinstance(other, datetime):
        value_naive = value.tzinfo is None
        other_naive = other.tzinfo is None
        if value_naive != other_naive:
            raise InvalidCalendar(
                "Cannot compare an aware recurrence date with a naive one."
            )
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
            other = other.astimezone(timezone.utc)
    if value < other:
        return -1
    if value > other:
        return 1
    return 0


def _same(value: date | datetime, other: date | datetime) -> bool:
    return _compare(value, other) == 0


def _before(value: date | datetime, other: date | datetime) -> bool:
    return _compare(value, other) < 0


def _after(value: date | datetime, other: date | datetime) -> bool:
    return _compare(value, other) > 0


def _event_duration(event: Event) -> timedelta:
    if "DURATION" in event:
        return event["DURATION"].dt
    return event.end - event.start


def _default_end(event: Event, start: date | datetime) -> date | datetime:
    duration = _event_duration(event)
    if isinstance(start, date) and not isinstance(start, datetime):
        return start + duration
    return start + duration


def _to_dateutil_rrule(event: Event, rule: vRecur) -> rrule:
    dtstart = event.start
    if isinstance(dtstart, date) and not isinstance(dtstart, datetime):
        dtstart = datetime(dtstart.year, dtstart.month, dtstart.day)
    try:
        return rrulestr(rule.to_ical().decode(), dtstart=dtstart)
    except (TypeError, ValueError) as error:
        raise InvalidCalendar(
            "The RRULE cannot be evaluated losslessly by icalendar."
        ) from error


def _rrule_occurrences(event: Event, rule: vRecur) -> list[date | datetime]:
    starts = list(_to_dateutil_rrule(event, rule))
    if isinstance(event.start, date) and not isinstance(event.start, datetime):
        return [
            _datetime_to_date(start) if isinstance(start, datetime) else start
            for start in starts
        ]
    return starts


def _datetime_to_date(value: datetime) -> date:
    return value.date()


def _as_datetime(value: date | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime(value.year, value.month, value.day)


def _rrule_at(rule: rrule, when: date | datetime) -> date | datetime | None:
    query = _as_datetime(when)
    try:
        occurrence = rule.before(query, inc=True)
    except TypeError as error:
        raise InvalidCalendar(
            "Recurrence values must be comparable with the split point."
        ) from error
    if occurrence is not None and _same(occurrence, query):
        return when
    return None


def _rrule_before(rule: rrule, when: date | datetime) -> date | datetime | None:
    query = _as_datetime(when)
    is_date = isinstance(when, date) and not isinstance(when, datetime)
    try:
        occurrence = rule.before(query, inc=False)
    except TypeError as error:
        raise InvalidCalendar(
            "Recurrence values must be comparable with the split point."
        ) from error
    if occurrence is None:
        return None
    return occurrence.date() if is_date else occurrence


def _recurrence_set(event: Event) -> list[_Occurrence]:
    occurrences: dict[date | datetime, _Occurrence] = {
        event.start: _Occurrence(event.start, event.end, "DTSTART")
    }
    for start, end in event.rdates:
        occurrences.setdefault(start, _Occurrence(start, end, "RDATE"))
    for rule in event.rrules:
        try:
            starts = _rrule_occurrences(event, rule)
        except TypeError as error:
            raise InvalidCalendar(
                "Recurrence values must be comparable with the split point."
            ) from error
        for start in starts:
            occurrences.setdefault(
                start, _Occurrence(start, _default_end(event, start), "RRULE")
            )
    return [occurrences[key] for key in sorted(occurrences)]


def _bounded_recurrence_set(event: Event) -> list[_Occurrence]:
    occurrences: dict[date | datetime, _Occurrence] = {
        event.start: _Occurrence(event.start, event.end, "DTSTART")
    }
    for start, end in event.rdates:
        occurrences.setdefault(start, _Occurrence(start, end, "RDATE"))
    try:
        for rule in event.rrules:
            if not _is_bounded(rule):
                raise InvalidCalendar(
                    "An unbounded RRULE cannot be fully materialized to RDATEs."
                )
            for start in _rrule_occurrences(event, rule):
                occurrences.setdefault(
                    start, _Occurrence(start, _default_end(event, start), "RRULE")
                )
    except TypeError as error:
        raise InvalidCalendar(
            "Recurrence values must be comparable with the split point."
        ) from error
    return [occurrences[key] for key in sorted(occurrences)]


def _find_split_occurrence(
    event: Event, split_at: date | datetime
) -> tuple[_Occurrence | None, bool]:
    for start, end in event.rdates:
        if _same(start, split_at):
            return _Occurrence(start, end, "RDATE"), False
    if _same(event.start, split_at):
        return _Occurrence(event.start, event.end, "DTSTART"), True
    for rule in event.rrules:
        if _rrule_at(_to_dateutil_rrule(event, rule), split_at) is not None:
            return (
                _Occurrence(
                    split_at, _default_end(event, split_at), "RRULE"
                ),
                True,
            )
    return None, False


def _is_bounded(rule: vRecur) -> bool:
    return "COUNT" in rule or "UNTIL" in rule


def _is_this_and_future(event: Event) -> bool:
    recurrence_id = event.get("RECURRENCE-ID")
    if not hasattr(recurrence_id, "params"):
        return False
    return (
        recurrence_id is not None
        and recurrence_id.params.get("RANGE") == "THISANDFUTURE"
    )


def _range_value(event: Event) -> str | None:
    recurrence_id = event.get("RECURRENCE-ID")
    if not hasattr(recurrence_id, "params"):
        return None
    return recurrence_id.params.get("RANGE")


def _date_property_start(value: Any) -> date | datetime:
    return value[0] if isinstance(value, tuple) else value


def _filter_property_dates(
    component: Event, name: str, split_at: date | datetime, *, future: bool
) -> None:
    groups = _select_property_dates(
        component, name, split_at, future=future, include_split=future
    )
    component.pop(name, None)
    for group in groups:
        component.add(name, group, encode=False)


def _replace_with_selected_dates(
    component: Event, name: str, groups: list[vDDDLists]
) -> None:
    component.pop(name, None)
    for group in groups:
        component.add(name, group, encode=False)


def _select_property_dates(
    component: Event,
    name: str,
    split_at: date | datetime,
    *,
    future: bool,
    include_split: bool,
) -> list[vDDDLists]:
    kept_properties = []
    for property_value in _as_list(component.get(name)):
        kept = []
        for item in property_value.dts:
            item_copy = deepcopy(item)
            start = _date_property_start(item_copy.dt)
            if future:
                should_keep = _after(start, split_at) or (
                    include_split and _same(start, split_at)
                )
            else:
                should_keep = _before(start, split_at)
            if should_keep:
                kept.append(item_copy)
        if kept:
            kept_properties.append(
                vDDDLists(kept, params=deepcopy(dict(property_value.params)))
            )
    return kept_properties


def _add_rdate(component: Event, occurrence: _Occurrence) -> None:
    if occurrence.source == "RDATE" and occurrence.end is not None:
        component.add("RDATE", (occurrence.start, occurrence.end))
    else:
        component.add("RDATE", occurrence.start)


def _filter_source_rdates_for_taf(
    groups: list[vDDDLists],
    component: Event,
    split_at: date | datetime,
    has_synthesized_rule: bool,
) -> list[vDDDLists]:
    existing = {start: end for start, end in component.rdates}
    result = []
    for group in groups:
        kept = []
        for item in deepcopy(group.dts):
            occurrence_end = item.dt[1] if isinstance(item.dt, tuple) else None
            start = item.dt[0] if isinstance(item.dt, tuple) else item.dt
            if not _after(start, split_at):
                continue
            if start in existing:
                if existing[start] != occurrence_end:
                    raise InvalidCalendar(
                        "Conflicting RDATE periods at the same occurrence cannot be "
                        "merged into the new THISANDFUTURE series losslessly."
                    )
                continue
            if has_synthesized_rule and occurrence_end is None:
                continue
            kept.append(item)
        if kept:
            result.append(vDDDLists(kept, params=deepcopy(dict(group.params))))
    return result


def _materialized_rdate_value(
    occurrence: _Occurrence,
) -> date | datetime | tuple[date | datetime, date | datetime]:
    if occurrence.source == "RDATE" and occurrence.end is not None:
        return occurrence.start, occurrence.end
    return occurrence.start


def _active_recurrence_set(component: Event) -> list[_Occurrence]:
    occurrences = _recurrence_set(component)
    excluded = set(component.exdates)
    return [
        occurrence
        for occurrence in occurrences
        if not any(_same(occurrence.start, exdate) for exdate in excluded)
    ]


def _rule_index(
    rule_starts: list[date | datetime], split_at: date | datetime
) -> int:
    for index, start in enumerate(rule_starts):
        if _same(start, split_at):
            return index
    return -1


def _truncate_rule_before(
    rule: vRecur,
    source_event: Event,
    dateutil_rule: rrule,
    split_at: date | datetime,
) -> None:
    if _is_bounded(rule):
        rule_starts = _rrule_occurrences(source_event, rule)
        index = _rule_index(rule_starts, split_at)
        previous_starts = (
            rule_starts[:index] if index >= 0 else [
                start for start in rule_starts if _before(start, split_at)
            ]
        )
    else:
        previous_start = _rrule_before(dateutil_rule, split_at)
        previous_starts = [] if previous_start is None else [previous_start]
    if not previous_starts:
        raise InvalidCalendar(
            "The series cannot losslessly start at this split point: the first "
            "segment would contain no RRULE occurrences."
        )
    if "COUNT" in rule:
        rule["COUNT"] = [len(previous_starts)]
        return
    until = previous_starts[-1]
    if isinstance(until, datetime) and until.tzinfo is not None:
        until = until.astimezone(timezone.utc)
    rule["UNTIL"] = [until]


def _future_rule_count(
    rule_starts: list[date | datetime], split_at: date | datetime
) -> int:
    index = _rule_index(rule_starts, split_at)
    if index < 0:
        raise InvalidCalendar("The split point is not generated by this RRULE.")
    return len(rule_starts[index:])


def _candidate_future_rule(
    source_event: Event,
    rule: vRecur,
    split_at: date | datetime,
    *,
    bounded: bool,
) -> vRecur:
    candidate = deepcopy(rule)
    if bounded:
        original_starts = _rrule_occurrences(source_event, rule)
        candidate["COUNT"] = [
            _future_rule_count(original_starts, split_at)
        ]
        candidate.pop("UNTIL", None)
    else:
        candidate.pop("COUNT", None)
        candidate.pop("UNTIL", None)
    return candidate


def _unbounded_rule_suffix_matches(
    source_event: Event, rule: vRecur, split_at: date | datetime
) -> bool:
    original = _to_dateutil_rrule(source_event, rule)
    candidate_rule = _candidate_future_rule(
        source_event, rule, split_at, bounded=False
    )
    candidate_event = deepcopy(source_event)
    candidate_event.DTSTART = split_at
    candidate_event.pop("RDATE", None)
    candidate_event.pop("EXDATE", None)
    candidate_event.pop("RRULE", None)
    candidate_event.add("RRULE", candidate_rule, encode=False)
    candidate = _to_dateutil_rrule(candidate_event, candidate_rule)
    expected = split_at
    for _count in range(200):
        actual = candidate.before(expected, inc=True)
        if actual is None or not _same(actual, expected):
            return False
        expected = original.after(expected, inc=False)
        if expected is None:
            return True
    return True


def _unbounded_event_rule_suffix_matches(
    source_event: Event, candidate_event: Event, split_at: date | datetime
) -> bool:
    if not source_event.rrules or not candidate_event.rrules:
        return False
    original = _to_dateutil_rrule(source_event, source_event.rrules[0])
    candidate = _to_dateutil_rrule(
        candidate_event, candidate_event.rrules[0]
    )
    expected = split_at
    for _count in range(200):
        actual = candidate.before(expected, inc=True)
        if actual is None or not _same(actual, expected):
            return False
        expected = original.after(expected, inc=False)
        if expected is None:
            return True
    return True


def _bounded_rule_suffix_matches(
    source_event: Event,
    rule: vRecur,
    split_at: date | datetime,
    split_end: date | datetime,
) -> bool:
    candidate_event = deepcopy(source_event)
    candidate_event.DTSTART = split_at
    if "DURATION" in candidate_event:
        candidate_event.DURATION = split_end - split_at
    else:
        candidate_event.DTEND = split_end
    candidate_event.pop("RDATE", None)
    candidate_event.pop("EXDATE", None)
    candidate_event.pop("RRULE", None)
    candidate_rule = _candidate_future_rule(
        source_event, rule, split_at, bounded=True
    )
    candidate_event.add("RRULE", candidate_rule, encode=False)
    original_starts = [
        start
        for start in _rrule_occurrences(source_event, rule)
        if not _before(start, split_at)
    ]
    return _rrule_occurrences(candidate_event, candidate_rule) == original_starts


def _materialize_range(
    source_event: Event,
    component: Event,
    occurrences: list[_Occurrence],
    split_at: date | datetime,
    *,
    future: bool,
) -> None:
    component.pop("RRULE", None)
    values = [
        _materialized_rdate_value(occurrence)
        for occurrence in occurrences
        if (
            not _before(occurrence.start, split_at)
            if future
            else _before(occurrence.start, split_at)
        )
    ]
    component.rdates = values


def _set_timing(
    component: Event, start: date | datetime, end: date | datetime | None
) -> None:
    if "DURATION" in component:
        duration = component["DURATION"].dt if end is None else end - start
        component.DTSTART = start
        component.DURATION = duration
        return
    component.DTSTART = start
    if end is not None:
        component.DTEND = end


def _apply_public_properties(
    component: Event, properties: Mapping[str, Any]
) -> None:
    protected = {
        "UID",
        "RECURRENCE-ID",
        "RRULE",
        "RDATE",
        "EXDATE",
        "DTSTART",
        "DTEND",
        "DURATION",
        "START",
        "END",
        "RECURRENCE_ID",
    }
    conflicts = {key.upper() for key in properties}.intersection(protected)
    if conflicts:
        raise ValueError(
            f"The properties {sorted(conflicts)} define recurrence identity or "
            "timing and must not be changed while splitting."
        )
    for key, value in properties.items():
        component.pop(key, None)
        component.add(key, value)


def split_recurring_event(
    calendar: Calendar,
    uid: str | UUID,
    recurrence_id: date | datetime,
    new_uid: str | UUID,
    properties: Mapping[str, Any] | None = None,
) -> tuple[Event, Event]:
    """Split one recurring event into two independent series, in-place.

    The calendar is only modified after all validation and transformation work
    succeeds.
    """
    if not isinstance(recurrence_id, (date, datetime)):
        raise TypeError("recurrence_id must be a date or datetime.")

    direct_events = [
        component
        for component in calendar.subcomponents
        if component.name == "VEVENT" and str(component.uid) == str(uid)
    ]
    masters = [event for event in direct_events if "RECURRENCE-ID" not in event]
    if len(masters) != 1:
        raise InvalidCalendar(
            "Exactly one recurring master VEVENT with this UID must exist."
        )
    if any(str(event.uid) == str(new_uid) for event in calendar.events):
        raise InvalidCalendar("The new UID is already used in this calendar.")

    master = masters[0]
    overrides = [event for event in direct_events if "RECURRENCE-ID" in event]
    exact_override = next(
        (
            event
            for event in overrides
            if _same(event.RECURRENCE_ID, recurrence_id)
        ),
        None,
    )

    for event in overrides:
        raw = event.get("RECURRENCE-ID")
        range_value = _range_value(event)
        if range_value not in (None, "THISANDFUTURE"):
            raise InvalidCalendar(f"Unsupported RECURRENCE-ID range: {range_value}.")
        if (
            range_value == "THISANDFUTURE"
            and _before(raw.dt, recurrence_id)
        ):
            raise InvalidCalendar(
                "Splitting inside an existing THISANDFUTURE range cannot be "
                "represented losslessly as two ordinary series."
            )

    if len(master.rrules) > 1:
        raise InvalidCalendar("Multiple RRULE properties cannot be split losslessly.")

    split_occurrence, from_rule = _find_split_occurrence(master, recurrence_id)
    if split_occurrence is None:
        raise InvalidCalendar(
            "The RECURRENCE-ID is not an occurrence in the original recurrence set."
        )

    rule = master.rrules[0] if master.rrules else None
    if rule is not None:
        dateutil_rule = _to_dateutil_rrule(master, rule)
        rule_bounded = _is_bounded(rule)
    else:
        dateutil_rule = None
        rule_bounded = False

    if rule is None or rule_bounded:
        occurrences = _bounded_recurrence_set(master)
    else:
        occurrences = []

    has_earlier = _before(master.start, recurrence_id) or any(
        _before(start, recurrence_id) for start, _end in master.rdates
    )
    if rule is not None and from_rule:
        previous_rule_start = (
            _rrule_before(dateutil_rule, recurrence_id)
            if dateutil_rule is not None
            else None
        )
        has_earlier = (
            has_earlier
            or previous_rule_start is not None
        )
    elif rule is not None:
        has_earlier = has_earlier or any(
            _before(occurrence.start, recurrence_id) for occurrence in occurrences
        )
    if not has_earlier:
        raise InvalidCalendar(
            "The series cannot losslessly start at this split point: the first "
            "segment would contain no occurrences."
        )

    rule_starts = (
        _rrule_occurrences(master, rule)
        if rule is not None and _is_bounded(rule)
        else []
    )
    if not from_rule and rule is not None and not _is_bounded(rule):
        raise InvalidCalendar(
            "An unbounded RRULE plus an RDATE-only split occurrence cannot be "
            "represented losslessly as two RRULE series."
        )

    exact_taf = exact_override is not None and _is_this_and_future(exact_override)
    if exact_taf and "RDATE" in exact_override:
        raise InvalidCalendar(
            "A THISANDFUTURE boundary that stores future occurrences in RDATE "
            "cannot be normalized losslessly by this operation."
        )

    copies = {id(event): deepcopy(event) for event in direct_events}
    old_master = copies[id(master)]
    new_master = deepcopy(
        copies[id(exact_override)] if exact_taf else old_master
    )

    if exact_taf:
        new_master.pop("RECURRENCE-ID", None)
        new_master.uid = new_uid
        future_exdates = _select_property_dates(
            old_master, "EXDATE", recurrence_id, future=True, include_split=False
        )
        future_rdates = _select_property_dates(
            old_master, "RDATE", recurrence_id, future=True, include_split=False
        )
        synthesized_rule = (
            "RRULE" not in new_master
            and "RDATE" not in new_master
            and rule is not None
        )
        if synthesized_rule:
            new_rule = deepcopy(rule)
            if "COUNT" in new_rule or "UNTIL" in new_rule:
                new_rule["COUNT"] = [
                    _future_rule_count(rule_starts, recurrence_id)
                ]
                new_rule.pop("UNTIL", None)
            new_master.add("RRULE", new_rule, encode=False)
        elif rule is None and "RRULE" not in new_master:
            raise InvalidCalendar(
                "A THISANDFUTURE boundary without RRULE or RDATE and without a "
                "master RRULE cannot describe the future segment losslessly."
            )
        filtered_future_rdates = _filter_source_rdates_for_taf(
            future_rdates, new_master, recurrence_id, synthesized_rule
        )
        for group in filtered_future_rdates:
            new_master.add("RDATE", group, encode=False)
        if rule is not None:
            for old_rule in old_master.rrules:
                _truncate_rule_before(
                    old_rule,
                    master,
                    _to_dateutil_rrule(master, old_rule),
                    recurrence_id,
                )
        _filter_property_dates(old_master, "RDATE", recurrence_id, future=False)
        _filter_property_dates(old_master, "EXDATE", recurrence_id, future=False)
        for group in future_exdates:
            new_master.add("EXDATE", group, encode=False)
        if rule is not None:
            if synthesized_rule and _is_bounded(rule):
                original_count = sum(
                    not _before(occurrence.start, recurrence_id)
                    for occurrence in _active_recurrence_set(master)
                )
                if original_count != len(_active_recurrence_set(new_master)):
                    raise InvalidCalendar(
                        "The THISANDFUTURE boundary cannot be represented "
                        "losslessly by the supported RRULE and RDATE combination."
                    )
            elif synthesized_rule and not _unbounded_event_rule_suffix_matches(
                master, new_master, recurrence_id
            ):
                raise InvalidCalendar(
                    "The THISANDFUTURE boundary cannot be represented losslessly "
                    "because anchoring its RRULE changes future occurrences."
                )
    else:
        end = split_occurrence.end
        if end is None:
            end = _default_end(master, split_occurrence.start)
        _set_timing(new_master, split_occurrence.start, end)
        new_master.uid = new_uid

        if rule is not None and from_rule and rule_bounded:
            for old_rule in old_master.rrules:
                _truncate_rule_before(
                    old_rule,
                    master,
                    _to_dateutil_rrule(master, old_rule),
                    recurrence_id,
                )
            if _bounded_rule_suffix_matches(
                master, rule, recurrence_id, end
            ):
                for new_rule in new_master.rrules:
                    new_rule.update(
                        _candidate_future_rule(
                            master,
                            rule,
                            recurrence_id,
                            bounded=True,
                        )
                    )
            else:
                _materialize_range(
                    master,
                    old_master,
                    occurrences,
                    recurrence_id,
                    future=False,
                )
                _materialize_range(
                    master,
                    new_master,
                    occurrences,
                    recurrence_id,
                    future=True,
                )
        elif rule is not None and from_rule:
            if not _unbounded_rule_suffix_matches(
                master, rule, recurrence_id
            ):
                raise InvalidCalendar(
                    "Moving DTSTART to this split point changes the unbounded "
                    "RRULE; the split cannot be represented losslessly without "
                    "a finite COUNT or UNTIL."
                )
            for old_rule in old_master.rrules:
                _truncate_rule_before(
                    old_rule,
                    master,
                    _to_dateutil_rrule(master, old_rule),
                    recurrence_id,
                )
            for new_rule in new_master.rrules:
                new_rule.update(
                    _candidate_future_rule(
                        master, rule, recurrence_id, bounded=False
                    )
                )
        elif rule is not None:
            old_master.pop("RRULE", None)
            old_master.rdates = [
                _materialized_rdate_value(occurrence)
                for occurrence in occurrences
                if _before(occurrence.start, recurrence_id)
            ]
            future_occurrences = [
                occurrence
                for occurrence in occurrences
                if not _before(occurrence.start, recurrence_id)
            ]
            new_master.pop("RRULE", None)
            existing = {
                rdate_start for rdate_start, _end in new_master.rdates
            }
            for occurrence in future_occurrences[1:]:
                if occurrence.start not in existing:
                    _add_rdate(new_master, occurrence)

        _filter_property_dates(old_master, "RDATE", recurrence_id, future=False)
        _replace_with_selected_dates(
            new_master,
            "RDATE",
            _select_property_dates(
                new_master,
                "RDATE",
                recurrence_id,
                future=True,
                include_split=False,
            ),
        )
        _filter_property_dates(old_master, "EXDATE", recurrence_id, future=False)
        _filter_property_dates(new_master, "EXDATE", recurrence_id, future=True)

    for original in overrides:
        copied = copies[id(original)]
        if exact_taf and original is exact_override:
            continue
        if not _before(copied.RECURRENCE_ID, recurrence_id):
            copied.uid = new_uid

    updates = dict(properties or {})
    _apply_public_properties(new_master, updates)

    new_subcomponents = []
    for component in calendar.subcomponents:
        if component is master:
            new_subcomponents.append(old_master)
            new_subcomponents.append(new_master)
        elif exact_taf and component is exact_override:
            continue
        elif id(component) in copies:
            new_subcomponents.append(copies[id(component)])
        else:
            new_subcomponents.append(component)
    calendar.subcomponents = new_subcomponents

    return old_master, new_master


__all__ = ["split_recurring_event"]
