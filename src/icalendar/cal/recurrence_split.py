"""Lossless splitting of recurring ``VEVENT`` series."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import TYPE_CHECKING, Any

from dateutil.rrule import rrulestr

from icalendar.error import InvalidCalendar, RecurrenceSplitError
from icalendar.prop.dt.list import vDDDLists
from icalendar.prop.dt.types import vDDDTypes
from icalendar.prop.recur.recur import vRecur

if TYPE_CHECKING:
    from icalendar.cal.calendar import Calendar
    from icalendar.cal.event import Event

PublicProperties = Mapping[str, Any] | Callable[..., None] | None


class SplitRecurringEventResult(tuple):
    """The old and new series produced by a recurrence split."""

    first: Event | None
    second: Event

    def __new__(cls, first: Event | None, second: Event):
        result = super().__new__(cls, (first, second))
        result.first = first
        result.second = second
        return result



@dataclass(frozen=True)
class _Built:
    first: Event | None
    second: Event
    first_exceptions: tuple[Event, ...]
    second_exceptions: tuple[Event, ...]

@dataclass(frozen=True)
class _Group:
    master: Event
    exceptions: tuple[Event, ...]


@dataclass(frozen=True)
class _Plan:
    group: _Group
    anchor: date | datetime
    new_uid: str
    ordinal: int | None
    boundary: Event | None


def split_recurring_event(
    calendar: Calendar,
    uid: str,
    recurrence_id: date | datetime,
    new_uid: str,
    properties: PublicProperties = None,
) -> SplitRecurringEventResult:
    """Split the VEVENT series with ``uid`` at an original recurrence start.

    The first result is the truncated original master, or ``None`` when no
    earlier occurrence remains. The second result is the master for the
    newly assigned UID. Existing exception components keep their original
    ``RECURRENCE-ID`` values and are moved to the series containing that
    occurrence.

    Raises :class:`RecurrenceSplitError` when RRULE/RDATE combinations cannot
    be represented without changing the expanded recurrence set.
    """
    plan = _build_plan(calendar, uid, recurrence_id, new_uid)
    built = _build_components(plan)
    _apply_properties(built.second, properties)
    _replace_components(calendar, plan, built)
    return SplitRecurringEventResult(built.first, built.second)


def _apply_properties(event: Event, properties: PublicProperties) -> None:
    if properties is None:
        return
    if callable(properties):
        properties(event)
        return
    for name, value in properties.items():
        if name in event:
            event.pop(name)
        event.add(name, value)


def _key(value: date | datetime) -> date | datetime:
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(timezone.utc)
    return value


def _same_type(left: date | datetime, right: date | datetime) -> None:
    if isinstance(left, datetime) != isinstance(right, datetime):
        raise InvalidCalendar(
            "RECURRENCE-ID must use the same DATE/DATE-TIME value type as DTSTART"
        )
    if isinstance(left, datetime) and (left.tzinfo is None) != (right.tzinfo is None):
        raise InvalidCalendar(
            "RECURRENCE-ID and DTSTART must both be floating or timezone-aware"
        )


def _rule_dt(value: date | datetime) -> datetime:
    return value if isinstance(value, datetime) else datetime.combine(value, time.min)


def _normalize_occurrence(dtstart: date | datetime, value: datetime) -> date | datetime:
    return value if isinstance(dtstart, datetime) else value.date()


def _supported_rule(rule: vRecur) -> None:
    unsupported = {"RSCALE", "SKIP", "BYWEEKDAY"}.intersection(rule)
    if unsupported:
        part = sorted(unsupported)[0]
        raise RecurrenceSplitError(f"RRULE part {part} is not supported")
    if rule.count == 0:
        raise RecurrenceSplitError("RRULE COUNT=0 does not generate an occurrence")


def _dateutil_rule(rule: vRecur, dtstart: date | datetime):
    _supported_rule(rule)
    try:
        return rrulestr(rule.to_ical().decode(), dtstart=_rule_dt(dtstart))
    except (TypeError, ValueError, OverflowError) as error:
        raise RecurrenceSplitError(f"Unsupported RRULE: {error}") from error


def _rule_occurrences(
    rule: vRecur, dtstart: date | datetime
) -> tuple[date | datetime, ...]:
    return tuple(
        _normalize_occurrence(dtstart, item) for item in _dateutil_rule(rule, dtstart)
    )


def _rule_ordinal(rule: vRecur, dtstart: date | datetime, anchor: date | datetime):
    target = _key(anchor)
    for index, occurrence in enumerate(_rule_occurrences(rule, dtstart)):
        current = _key(occurrence)
        if current == target:
            return index
        if current > target:
            return None
    return None


def _finite(rule: vRecur) -> bool:
    return rule.count is not None or "UNTIL" in rule


def _groups(event: Event, name: str) -> tuple[vDDDTypes | vDDDLists, ...]:
    value = event.get(name)
    if value is None:
        return ()
    return tuple(value) if isinstance(value, list) else (value,)


def _entries(group: vDDDTypes | vDDDLists) -> tuple[vDDDTypes, ...]:
    return tuple(group.dts)


def _entry_start(entry: vDDDTypes) -> date | datetime:
    value = entry.dt
    return value[0] if isinstance(value, tuple) else value


def _entry_end(entry: vDDDTypes) -> date | datetime | timedelta | None:
    value = entry.dt
    return value[1] if isinstance(value, tuple) else None


def _entry_key(entry: vDDDTypes) -> date | datetime:
    return _key(_entry_start(entry))


def _matching_entries(
    event: Event, name: str, anchor: date | datetime
) -> list[vDDDTypes]:
    target = _key(anchor)
    return [
        entry
        for group in _groups(event, name)
        for entry in _entries(group)
        if _entry_key(entry) == target
    ]


def _filter_groups(event: Event, name: str, keep: Callable[[vDDDTypes], bool]):
    result = []
    for group in _groups(event, name):
        original = _entries(group)
        selected = [deepcopy(entry) for entry in original if keep(entry)]
        if not selected:
            continue
        if len(selected) == len(original):
            result.append(deepcopy(group))
        else:
            result.append(vDDDLists(selected, params=dict(group.params)))
    return result


def _set_groups(
    event: Event, name: str, values: Iterable[vDDDTypes | vDDDLists]
) -> None:
    event.pop(name, None)
    for group in values:
        event.add(name, group, parameters=dict(group.params), encode=False)


def _add_value(
    event: Event,
    name: str,
    value: date | datetime,
    params: Mapping[str, Any],
) -> None:
    event.add(name, vDDDTypes(value), parameters=dict(params), encode=False)


def _copy_rule(rule: vRecur) -> vRecur:
    return deepcopy(rule)


def _set_rule(event: Event, rule: vRecur | None) -> None:
    event.pop("RRULE", None)
    if rule is not None:
        event.add("RRULE", rule, parameters=dict(rule.params), encode=False)


def _set_start(event: Event, value: date | datetime) -> None:
    duration = event.duration if "DURATION" in event or "DTEND" in event else None
    original_end = event.end if "DTEND" in event else None
    original_duration = duration
    event.DTSTART = value
    if duration is None:
        return
    if "DURATION" in event:
        event.DURATION = duration
    elif original_end is not None:
        event.pop("DURATION", None)
        if not isinstance(original_end, datetime) and not isinstance(value, datetime):
            event.DTEND = value + original_duration
        elif isinstance(value, datetime):
            event.DTEND = value.replace(
                hour=original_end.hour,
                minute=original_end.minute,
                second=original_end.second,
                microsecond=original_end.microsecond,
            )
        else:
            event.DTEND = value + duration
    else:
        event.pop("DURATION", None)
        event.DTEND = value + duration


def _utc_until(value: date | datetime) -> date | datetime:
    if isinstance(value, datetime) and value.tzinfo:
        return value.astimezone(timezone.utc)
    return value


def _calendar_group(calendar: Calendar, uid: str) -> _Group:
    events = [event for event in calendar.events if str(event.uid) == uid]
    if not events:
        raise InvalidCalendar(f"No VEVENT with UID {uid!r}")
    masters = [event for event in events if "RECURRENCE-ID" not in event]
    if len(masters) != 1:
        raise InvalidCalendar(f"UID {uid!r} must identify exactly one recurring master")
    master = masters[0]
    exceptions = tuple(event for event in events if event is not master)
    return _Group(master, exceptions)


def _boundary(exception: Event) -> bool:
    return (
        "RECURRENCE-ID" in exception
        and str(exception["RECURRENCE-ID"].params.get("RANGE", "")).upper()
        == "THISANDFUTURE"
    )


def _build_plan(
    calendar: Calendar, uid: str, anchor: date | datetime, new_uid: str
) -> _Plan:
    if not isinstance(anchor, (date, datetime)):
        raise TypeError("recurrence_id must be a date or datetime")
    if not new_uid:
        raise ValueError("new_uid must not be empty")
    if any(str(event.uid) == new_uid for event in calendar.events):
        raise InvalidCalendar(f"UID {new_uid!r} already exists")

    group = _calendar_group(calendar, uid)
    master = group.master
    if "DTSTART" not in master:
        raise InvalidCalendar("Recurring master must have DTSTART")
    dtstart = master.DTSTART
    _same_type(dtstart, anchor)

    boundaries = []
    for exception in group.exceptions:
        raw = exception["RECURRENCE-ID"]
        _same_type(dtstart, raw.dt)
        if _boundary(exception):
            boundaries.append(exception)
    if len(boundaries) > 1:
        raise InvalidCalendar("Multiple THISANDFUTURE boundaries use the same UID")

    if len(master.rrules) > 1:
        raise RecurrenceSplitError(
            "Multiple RRULE properties cannot be split losslessly"
        )

    rule = master.rrules[0] if master.rrules else None
    _validate_recurrence_members(group, rule, dtstart)
    anchor_key = _key(anchor)
    if rule is not None:
        ordinal = _rule_ordinal(rule, dtstart, anchor)
        if ordinal is None and not _matching_entries(master, "RDATE", anchor):
            raise InvalidCalendar(
                "recurrence-id is not an occurrence of the original series"
            )
    else:
        ordinal = None
        if anchor_key != _key(dtstart) and not _matching_entries(
            master, "RDATE", anchor
        ):
            raise InvalidCalendar(
                "recurrence-id is not an occurrence of the original series"
            )

    boundary = boundaries[0] if boundaries else None
    if boundary is not None:
        boundary_key = _key(boundary.RECURRENCE_ID)
        if boundary_key < anchor_key:
            raise RecurrenceSplitError(
                "An earlier THISANDFUTURE boundary cannot be represented by a split"
            )
        if boundary_key > anchor_key:
            boundary = None

    return _Plan(group, anchor, new_uid, ordinal, boundary)


def _merge_matching_date_groups(
    target: Event,
    source: Event,
    name: str,
    minimum: date | datetime,
) -> None:
    minimum_key = _key(minimum)
    existing = {
        _entry_key(entry)
        for group in _groups(target, name)
        for entry in _entries(group)
    }
    for group in _filter_groups(
        source, name, lambda entry: _entry_key(entry) >= minimum_key
    ):
        selected = [
            entry
            for entry in _entries(group)
            if _entry_key(entry) not in existing
        ]
        if not selected:
            continue
        if len(selected) == len(_entries(group)):
            target.add(name, group, parameters=dict(group.params), encode=False)
        else:
            target.add(
                name,
                vDDDLists(selected, params=dict(group.params)),
                encode=False,
            )
        existing.update(_entry_key(entry) for entry in selected)


def _anchor_entries(master: Event, anchor: date | datetime) -> list[vDDDTypes]:
    return _matching_entries(master, "RDATE", anchor)


def _anchor_special(
    master: Event, anchor: date | datetime, ordinal: int | None
) -> vDDDTypes | None:
    if ordinal is not None:
        return None
    entries = _anchor_entries(master, anchor)
    if not entries:
        return None
    if len(entries) > 1:
        raise InvalidCalendar("Duplicate RDATE values occur at the split point")
    return entries[0]


def _is_included(
    rule: vRecur | None,
    dtstart: date | datetime,
    rdates: Iterable[date | datetime],
    value: date | datetime,
) -> bool:
    target = _key(value)
    if target == _key(dtstart):
        return True
    if any(_key(item) == target for item in rdates):
        return True
    if rule is None:
        return False
    return _rule_ordinal(rule, dtstart, value) is not None


def _validate_recurrence_members(
    group: _Group, rule: vRecur | None, dtstart: date | datetime
) -> None:
    rdate_entries = [
        entry
        for rdate_group in _groups(group.master, "RDATE")
        for entry in _entries(rdate_group)
    ]
    rdate_starts = [_entry_start(entry) for entry in rdate_entries]

    seen_rdates: dict[date | datetime, vDDDTypes] = {}
    for entry in rdate_entries:
        key = _entry_key(entry)
        if key in seen_rdates and seen_rdates[key].dt != entry.dt:
            raise InvalidCalendar(
                "Conflicting RDATE values describe the same occurrence"
            )
        seen_rdates.setdefault(key, entry)

    for exception in group.exceptions:
        rid = exception.RECURRENCE_ID
        if not _is_included(rule, dtstart, rdate_starts, rid):
            raise InvalidCalendar(
                f"RECURRENCE-ID {rid!r} is not part of the original recurrence set"
            )

    seen_exceptions: set[date | datetime] = set()
    for exception in group.exceptions:
        key = _key(exception.RECURRENCE_ID)
        if key in seen_exceptions:
            raise InvalidCalendar(
                "Multiple exception components use the same RECURRENCE-ID"
            )
        seen_exceptions.add(key)

    for exdate_group in _groups(group.master, "EXDATE"):
        for entry in _entries(exdate_group):
            value = _entry_start(entry)
            if not _is_included(rule, dtstart, rdate_starts, value):
                raise InvalidCalendar(
                    f"EXDATE {value!r} is not part of the original recurrence set"
                )


def _truncate_front(
    front: Event,
    rule: vRecur | None,
    anchor: date | datetime,
    ordinal: int | None,
) -> None:
    anchor_key = _key(anchor)
    _set_groups(
        front,
        "RDATE",
        _filter_groups(front, "RDATE", lambda entry: _entry_key(entry) < anchor_key),
    )
    _set_groups(
        front,
        "EXDATE",
        _filter_groups(front, "EXDATE", lambda entry: _entry_key(entry) < anchor_key),
    )

    if rule is None:
        starts = [
            _entry_start(entry)
            for group in _groups(front, "RDATE")
            for entry in _entries(group)
        ]
        if starts:
            _set_start(front, max(starts))
        return

    if ordinal is None:
        if not _finite(rule):
            raise RecurrenceSplitError(
                "Splitting at an extra RDATE occurrence of an infinite "
                "RRULE requires flattening earlier occurrences"
            )
        existing_groups = list(_groups(front, "RDATE"))
        existing_keys = {
            _entry_key(entry)
            for group in existing_groups
            for entry in _entries(group)
        }
        generated = [
            occurrence
            for occurrence in _rule_occurrences(rule, front.DTSTART)
            if _key(occurrence) < anchor_key and _key(occurrence) not in existing_keys
        ]
        if generated:
            existing_groups.append(
                vDDDLists(
                    generated,
                    params=dict(front["DTSTART"].params),
                )
            )
        _set_rule(front, None)
        _set_groups(front, "RDATE", existing_groups)
        starts = [
            _entry_start(entry)
            for group in _groups(front, "RDATE")
            for entry in _entries(group)
        ]
        if starts:
            _set_start(front, max(starts))
        return

    if ordinal == 0:
        _set_rule(front, None)
        starts = [
            _entry_start(entry)
            for group in _groups(front, "RDATE")
            for entry in _entries(group)
        ]
        if starts:
            _set_start(front, max(starts))
        return

    front_rule = _copy_rule(rule)
    if front_rule.count is not None:
        front_rule.count = ordinal
    else:
        previous = [
            occurrence
            for occurrence in _rule_occurrences(rule, front.DTSTART)
            if _key(occurrence) < anchor_key
        ]
        if not previous:
            raise InvalidCalendar("Split point is before the first RRULE occurrence")
        front_rule["UNTIL"] = [_utc_until(previous[-1])]
    _set_rule(front, front_rule)


def _build_normal_back(
    master: Event,
    anchor: date | datetime,
    new_uid: str,
    ordinal: int | None,
) -> Event:
    special = _anchor_special(master, anchor, ordinal)
    back = deepcopy(master)
    back.uid = new_uid
    back.pop("RECURRENCE-ID", None)
    anchor_key = _key(anchor)
    rules = master.rrules
    rule = rules[0] if rules else None

    if special is None:
        for entry in _matching_entries(back, "RDATE", anchor):
            end_value = _entry_end(entry)
            if isinstance(end_value, timedelta):
                matches_master = end_value == master.duration
            elif end_value is not None:
                matches_master = _key(end_value) == _key(master.end)
            else:
                matches_master = True
            if not matches_master:
                raise RecurrenceSplitError(
                    "An RDATE period at the split point changes the master occurrence"
                )
        _set_start(back, anchor)
        if rule is not None:
            back_rule = _copy_rule(rule)
            if back_rule.count is not None:
                if ordinal is None:
                    raise RecurrenceSplitError("Cannot calculate the resulting COUNT")
                back_rule.count = rule.count - ordinal
            _set_rule(back, back_rule)
        _set_groups(
            back,
            "RDATE",
            _filter_groups(back, "RDATE", lambda entry: _entry_key(entry) > anchor_key),
        )
    elif rule is None:
        end_value = _entry_end(special)
        _set_start(back, anchor)
        if isinstance(end_value, timedelta):
            back.set_duration(end_value)
        elif end_value is not None:
            back.pop("DURATION", None)
            back.DTEND = end_value
        _set_groups(
            back,
            "RDATE",
            _filter_groups(back, "RDATE", lambda entry: _entry_key(entry) > anchor_key),
        )
    else:
        if not _finite(rule):
            raise RecurrenceSplitError(
                "Splitting at an extra RDATE occurrence of an infinite "
                "RRULE requires flattening unsupported future occurrences"
            )
        end_value = _entry_end(special)
        _set_start(back, anchor)
        if isinstance(end_value, timedelta):
            back.set_duration(end_value)
        elif end_value is not None:
            back.pop("DURATION", None)
            back.DTEND = end_value
        _set_rule(back, None)
        future_groups = list(_filter_groups(
            back, "RDATE", lambda entry: _entry_key(entry) > anchor_key
        ))
        future_keys = {
            _entry_key(entry)
            for group in future_groups
            for entry in _entries(group)
        }
        generated = [
            occurrence
            for occurrence in _rule_occurrences(rule, master.DTSTART)
            if _key(occurrence) > anchor_key and _key(occurrence) not in future_keys
        ]
        if generated:
            future_groups.append(
                vDDDLists(
                    generated,
                    params=dict(master["DTSTART"].params),
                )
            )
        _set_groups(back, "RDATE", future_groups)

    _set_groups(
        back,
        "EXDATE",
        _filter_groups(back, "EXDATE", lambda entry: _entry_key(entry) >= anchor_key),
    )
    if ordinal == 0 and not _matching_entries(back, "EXDATE", anchor):
        _add_value(back, "EXDATE", anchor, dict(master["DTSTART"].params))
    return back


def _build_boundary_back(
    master: Event,
    boundary: Event,
    anchor: date | datetime,
    new_uid: str,
    ordinal: int | None,
) -> Event:
    back = deepcopy(boundary)
    back.uid = new_uid
    back.pop("RECURRENCE-ID", None)
    rules = boundary.rrules or master.rrules
    if len(rules) > 1:
        raise RecurrenceSplitError(
            "Multiple RRULE properties cannot be split losslessly"
        )
    rule = rules[0] if rules else None
    if rule is not None:
        back_rule = _copy_rule(rule)
        if back_rule.count is not None:
            source_rule = boundary.rrules[0] if boundary.rrules else master.rrules[0]
            if ordinal is None:
                raise RecurrenceSplitError("Cannot calculate THISANDFUTURE COUNT")
            back_rule.count = max(source_rule.count - ordinal, 1)
        _set_rule(back, back_rule)
    _merge_matching_date_groups(back, master, "RDATE", anchor)
    _merge_matching_date_groups(back, master, "EXDATE", anchor)
    return back


def _has_previous_occurrence(
    front: Event, anchor: date | datetime, exceptions: Iterable[Event]
) -> bool:
    anchor_key = _key(anchor)
    if _key(front.DTSTART) < anchor_key and (front.rrules or _groups(front, "RDATE")):
        return True
    for name in ("RDATE", "EXDATE"):
        for group in _groups(front, name):
            if any(_entry_key(entry) < anchor_key for entry in _entries(group)):
                return True
    return any(
        _key(exception.RECURRENCE_ID) < anchor_key
        and not _boundary(exception)
        for exception in exceptions
    )


def _build_components(plan: _Plan) -> _Built:
    master = plan.group.master
    rule = master.rrules[0] if master.rrules else None
    front = deepcopy(master)
    _truncate_front(front, rule, plan.anchor, plan.ordinal)

    if plan.boundary is None:
        back = _build_normal_back(
            master, plan.anchor, plan.new_uid, plan.ordinal
        )
    else:
        back = _build_boundary_back(
            master, plan.boundary, plan.anchor, plan.new_uid, plan.ordinal
        )

    first_exceptions = []
    second_exceptions = []
    for exception in plan.group.exceptions:
        if exception is plan.boundary:
            continue
        copy = deepcopy(exception)
        if _key(copy.RECURRENCE_ID) < _key(plan.anchor):
            first_exceptions.append(copy)
        else:
            copy.uid = plan.new_uid
            second_exceptions.append(copy)

    first = (
        front
        if _has_previous_occurrence(front, plan.anchor, first_exceptions)
        else None
    )
    return _Built(
        first,
        back,
        tuple(first_exceptions),
        tuple(second_exceptions),
    )


def _replace_components(calendar: Calendar, plan: _Plan, built: _Built) -> None:
    originals = [plan.group.master, *plan.group.exceptions]
    original_ids = {id(component) for component in originals}
    index = next(
        (
            position
            for position, component in enumerate(calendar.subcomponents)
            if component is plan.group.master
        ),
        len(calendar.subcomponents),
    )
    calendar.subcomponents = [
        component
        for position, component in enumerate(calendar.subcomponents)
        if position < index or id(component) not in original_ids
    ]
    replacements = []
    if built.first is not None:
        replacements.append(built.first)
    replacements.extend(built.first_exceptions)
    replacements.append(built.second)
    replacements.extend(built.second_exceptions)
    calendar.subcomponents[index:index] = replacements
