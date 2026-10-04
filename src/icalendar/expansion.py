"""Bounded expansion of recurring VEVENT components into concrete instances."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo, timezone
from typing import TYPE_CHECKING, Any, Iterable
from typing import Iterator

from dateutil import rrule as dateutil_rrule

from icalendar.error import IncompleteComponent, InvalidCalendar
from icalendar.tools import is_date

if TYPE_CHECKING:
    from icalendar.cal import Calendar, Event
    from icalendar.prop.recur.recur import vRecur

UTC = timezone.utc
DEFAULT_MAX_INSTANCES = 1000


@dataclass(frozen=True)
class EventInstance:
    """One concrete meeting occurrence.

    ``recurrence_id`` remains the original start of the occurrence. The
    effective ``start`` and ``end`` may come from the master, an RDATE period,
    a single-instance override, or an applied ``RANGE=THISANDFUTURE`` override.
    """

    uid: str
    recurrence_id: date | datetime
    start: date | datetime
    end: date | datetime
    duration: timedelta
    status: str | None
    event: Event


@dataclass(frozen=True)
class EventExpansion:
    """Instances found in a bounded query.

    ``complete`` is true only when expansion did not reach ``max_instances``.
    When ``truncated`` is true, at least one later matching instance may exist
    but was not generated.
    """

    instances: tuple[EventInstance, ...]
    complete: bool
    truncated: bool

    def __iter__(self) -> Iterator[EventInstance]:
        return iter(self.instances)

    def __len__(self) -> int:
        return len(self.instances)


@dataclass(frozen=True)
class _Interval:
    start: date | datetime
    end: date | datetime
    duration: timedelta
    nominal_duration: timedelta | None = None


@dataclass(frozen=True)
class _Version:
    sequence: int
    stamp: datetime | None
    event: Event
    thisandfuture: bool


@dataclass(frozen=True)
class _Identity:
    key: tuple[str, Any]
    value: date | datetime


@dataclass(frozen=True)
class _Query:
    start_date: date
    end_date: date
    start_datetime: datetime
    end_datetime: datetime
    floating_timezone: tzinfo | None

    def interpret(self, value: date | datetime) -> date | datetime:
        if is_date(value):
            return value
        if value.tzinfo is None:
            if self.floating_timezone is None:
                raise InvalidCalendar(
                    "floating_timezone is required to interpret floating events"
                )
            return value.replace(tzinfo=self.floating_timezone)
        return value

    def overlaps(self, interval: _Interval) -> bool:
        if is_date(interval.start):
            if interval.duration == timedelta():
                return False
            return interval.start < self.end_date and interval.end > self.start_date
        start = self.interpret(interval.start)
        end = self.interpret(interval.end)
        exact_duration = _exact_duration(start, end)
        if exact_duration == timedelta():
            return self.start_datetime <= start < self.end_datetime
        return start < self.end_datetime and end > self.start_datetime


def expand_events(
    source: Calendar | Iterable[Event],
    start: date | datetime,
    end: date | datetime,
    *,
    floating_timezone: tzinfo | None = None,
    max_instances: int = DEFAULT_MAX_INSTANCES,
) -> EventExpansion:
    """Expand VEVENTs occurring during the half-open interval ``[start, end)``.

    DATE, floating date-time, and TZID-bearing date-time values are kept
    distinct. Floating values are interpreted for comparison only with
    ``floating_timezone``; their returned values remain naive.

    Overrides are selected by SEQUENCE and then DTSTAMP. Equal versions with
    different content raise :class:`InvalidCalendar`. Cancelled overrides
    suppress their identity, while active overrides may move the effective
    occurrence without changing its original recurrence id.
    """
    if max_instances < 1:
        raise ValueError("max_instances must be at least 1")
    query = _make_query(start, end, floating_timezone)
    events = list(source.events if hasattr(source, "events") else source)
    groups = _group_events(events, query)

    results: list[EventInstance] = []
    truncated = False
    for uid in sorted(groups):
        expansion = _expand_group(uid, groups[uid], query, max_instances)
        results.extend(expansion.instances)
        truncated = truncated or expansion.truncated

    results.sort(
        key=lambda item: (
            _identity_sort_value(item.start, query),
            item.uid,
            _identity_sort_value(item.recurrence_id, query),
        )
    )
    if len(results) > max_instances:
        results = results[:max_instances]
        truncated = True
    return EventExpansion(tuple(results), not truncated, truncated)


def _make_query(
    start: date | datetime,
    end: date | datetime,
    floating_timezone: tzinfo | None,
) -> _Query:
    if not isinstance(start, (date, datetime)) or not isinstance(
        end, (date, datetime)
    ):
        raise TypeError("start and end must be date or datetime instances")
    if not start < end:
        raise ValueError("start must be before end")

    def prepare(value: date | datetime) -> datetime:
        if is_date(value):
            return datetime.combine(value, time.min, tzinfo=UTC)
        if value.tzinfo is None:
            if floating_timezone is None:
                raise InvalidCalendar(
                    "floating_timezone is required to interpret a floating query"
                )
            return value.replace(tzinfo=floating_timezone)
        return value

    start_dt = prepare(start)
    end_dt = prepare(end)
    return _Query(
        start_dt.date(),
        end_dt.date(),
        start_dt,
        end_dt,
        floating_timezone,
    )


def _group_events(events: Iterable[Event], query: _Query) -> dict[str, dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"masters": [], "overrides": defaultdict(list)}
    )
    for event in events:
        uid = str(event.uid) if event.uid else ""
        if not uid:
            raise InvalidCalendar("VEVENT requires UID for recurrence expansion")
        version = _make_version(event)
        if event.RECURRENCE_ID is None:
            groups[uid]["masters"].append(version)
        else:
            identity = _identity(event.RECURRENCE_ID, query)
            groups[uid]["overrides"][identity.key].append(version)
    return groups


def _make_version(event: Event) -> _Version:
    thisandfuture = False
    recurrence = event.get("RECURRENCE-ID")
    if recurrence is not None:
        range_value = str(recurrence.params.get("RANGE", ""))
        if range_value == "THISANDFUTURE":
            thisandfuture = True
        elif range_value:
            raise InvalidCalendar(f"Unsupported RECURRENCE-ID RANGE: {range_value}")
    return _Version(event.sequence, event.stamp, event, thisandfuture)


def _select_version(versions: list[_Version], uid: str, label: str) -> _Version:
    winner = versions[0]
    for candidate in versions[1:]:
        if _version_key(candidate) > _version_key(winner):
            winner = candidate
        elif _version_key(candidate) == _version_key(winner):
            if candidate.event.to_ical() != winner.event.to_ical():
                raise InvalidCalendar(f"Conflicting event versions for UID {uid} and {label}")
    return winner


def _version_key(version: _Version) -> tuple[int, datetime]:
    return version.sequence, version.stamp or datetime.min.replace(tzinfo=UTC)


def _identity(value: date | datetime, query: _Query) -> _Identity:
    if is_date(value):
        return _Identity(("date", value), value)
    interpreted = query.interpret(value)
    if value.tzinfo is None:
        return _Identity(("floating", interpreted.astimezone(UTC)), value)
    return _Identity(("utc", interpreted.astimezone(UTC)), value)


def _identity_sort_value(
    value: date | datetime, query: _Query
) -> tuple[int, date | datetime]:
    if is_date(value):
        return 0, datetime.combine(value, time.min, tzinfo=UTC)
    interpreted = query.interpret(value)
    return 1, interpreted if interpreted.tzinfo is None else interpreted.astimezone(UTC)


def _event_status(event: Event) -> str | None:
    status = event.status
    return str(status) if status else None


def _required_start(event: Event) -> date | datetime:
    try:
        return event.start
    except IncompleteComponent as error:
        raise InvalidCalendar("Active VEVENT requires DTSTART") from error


def _event_interval(event: Event, query: _Query) -> _Interval:
    start = _required_start(event)
    try:
        end = event.end
    except IncompleteComponent as error:
        raise InvalidCalendar("Active VEVENT requires a calculable end") from error
    interval = _interval_from_bounds(start, end, query)
    if "DURATION" in event:
        nominal_duration = event["DURATION"].dt
        nominal_end = _add_nominal_duration(start, nominal_duration)
        return _Interval(start, nominal_end, interval.duration, nominal_duration)
    return interval


def _interval_from_bounds(
    start: date | datetime,
    end: date | datetime,
    query: _Query | None = None,
) -> _Interval:
    if is_date(start) != is_date(end):
        raise InvalidCalendar("DTSTART and DTEND must use the same value type")
    if not is_date(start) and (start.tzinfo is None) != (end.tzinfo is None):
        raise InvalidCalendar("DTSTART and DTEND timezone forms must match")
    if start > end:
        raise InvalidCalendar("DTSTART must not be after DTEND")
    if is_date(start):
        duration = timedelta(days=(end - start).days)
        return _Interval(start, end, duration, duration)
    compared_start = start if query is None else query.interpret(start)
    compared_end = end if query is None else query.interpret(end)
    duration = _exact_duration(compared_start, compared_end)
    return _Interval(start, end, duration, duration)


def _exact_duration(start: date | datetime, end: date | datetime) -> timedelta:
    if is_date(start):
        return end - start
    return timedelta(seconds=end.timestamp() - start.timestamp())


def _add_nominal_duration(
    start: date | datetime, duration: timedelta
) -> date | datetime:
    if is_date(start):
        return start + duration
    if start.tzinfo is not None and hasattr(start.tzinfo, "localize"):
        return start.tzinfo.localize(start.replace(tzinfo=None) + duration)
    return (start.replace(tzinfo=None) + duration).replace(tzinfo=start.tzinfo)


def _normalize_rule_datetime(value: date | datetime) -> date | datetime:
    if is_date(value) or value.tzinfo is None or not hasattr(value.tzinfo, "localize"):
        return value
    return value.tzinfo.localize(value.replace(tzinfo=None))


def _expand_group(
    uid: str,
    group: dict[str, Any],
    query: _Query,
    max_instances: int,
) -> EventExpansion:
    masters = group["masters"]
    if not masters:
        raise InvalidCalendar(f"RECURRENCE-ID without master VEVENT for UID {uid}")
    master_version = _select_version(masters, uid, "master")
    master = master_version.event
    if _event_status(master) == "CANCELLED":
        return EventExpansion((), True, False)

    overrides = {
        key: _select_version(versions, uid, str(key))
        for key, versions in group["overrides"].items()
    }
    master_start = _required_start(master)
    master_identity = _identity(master_start, query)
    master_interval = _event_interval(master, query)
    custom_periods = {}
    for start, end in master.rdates:
        identity = _identity(start, query)
        if end is not None:
            custom_periods[identity.key] = _interval_from_bounds(start, end, query)
    _validate_recurrence_dates(master_start, master.rdates, master.exdates)

    candidates, truncated = _candidate_identities(
        master,
        master_start,
        master_identity,
        overrides,
        query,
        max_instances,
    )
    exdates = {_identity(value, query).key for value in master.exdates}
    range_versions = sorted(
        (version for version in overrides.values() if version.thisandfuture),
        key=lambda version: _identity(version.event.RECURRENCE_ID, query).key,
    )

    instances = []
    for identity in sorted(candidates, key=lambda item: item.key):
        exact = overrides.get(identity.key)
        if exact is not None and not exact.thisandfuture:
            if _event_status(exact.event) == "CANCELLED":
                continue
            interval = _event_interval(exact.event, query)
            effective = exact.event
        else:
            range_version = _latest_range(range_versions, identity)
            if (
                range_version is not None
                and _event_status(range_version.event) == "CANCELLED"
            ):
                continue
            if identity.key in exdates and range_version is None:
                continue
            interval = _base_or_range_interval(
                identity, master_interval, custom_periods, range_version, query
            )
            effective = master if range_version is None else range_version.event

        if not query.overlaps(interval):
            continue
        instances.append(
            EventInstance(
                uid,
                identity.value,
                interval.start,
                interval.end,
                interval.duration,
                _event_status(effective),
                effective,
            )
        )
    truncated = truncated or len(instances) > max_instances
    return EventExpansion(tuple(instances), not truncated, truncated)


def _candidate_identities(
    master: Event,
    master_start: date | datetime,
    master_identity: _Identity,
    overrides: dict[tuple[str, Any], _Version],
    query: _Query,
    max_instances: int,
) -> tuple[list[_Identity], bool]:
    _validate_override_types(master_start, overrides)
    lower, upper = _raw_bounds(master_identity, query)
    base_keys = {master_identity.key}
    rdate_keys = set()
    candidates = {master_identity.key: master_identity}
    if lower <= master_identity.value <= upper:
        candidates[master_identity.key] = master_identity

    for start, end in master.rdates:
        identity = _identity(start, query)
        base_keys.add(identity.key)
        rdate_keys.add(identity.key)
        interval = (
            _interval_from_bounds(start, end, query)
            if end is not None
            else _base_interval(identity, _event_interval(master, query))
        )
        if identity.value < upper and interval.end > lower:
            candidates[identity.key] = identity

    generated, truncated = _rule_identities(
        master,
        master_identity,
        lower,
        upper,
        query,
        max_instances,
    )
    base_keys.update(generated)
    candidates.update(generated)

    for version in overrides.values():
        identity = _identity(version.event.RECURRENCE_ID, query)
        if not truncated:
            _validate_recurrence_identity(
                master,
                master_identity,
                identity,
                base_keys | rdate_keys,
            )
        if version.thisandfuture:
            if _event_status(version.event) != "CANCELLED":
                interval = _event_interval(version.event, query)
                if identity.value < upper and interval.end > lower:
                    candidates[identity.key] = identity
            continue
        if _event_status(version.event) == "CANCELLED":
            continue
        if "DTSTART" not in version.event:
            raise InvalidCalendar("Active recurrence override requires DTSTART")
        interval = _event_interval(version.event, query)
        if query.overlaps(interval):
            candidates[identity.key] = identity

    return list(candidates.values()), truncated


def _validate_override_types(
    master_start: date | datetime, overrides: dict[tuple[str, Any], _Version]
) -> None:
    master_kind = _raw_kind(master_start)
    for version in overrides.values():
        kind = _raw_kind(version.event.RECURRENCE_ID)
        if kind != master_kind:
            raise InvalidCalendar(
                "RECURRENCE-ID value type must match master DTSTART"
            )


def _validate_recurrence_dates(
    master_start: date | datetime,
    rdates: list[tuple[date | datetime, date | datetime | None]],
    exdates: list[date | datetime],
) -> None:
    master_kind = _raw_kind(master_start)
    for value in exdates:
        if _raw_kind(value) != master_kind:
            raise InvalidCalendar("EXDATE value type must match DTSTART")
    for start, end in rdates:
        if _raw_kind(start) != master_kind:
            raise InvalidCalendar("RDATE value type must match DTSTART")
        if end is not None and _raw_kind(end) != master_kind:
            raise InvalidCalendar("RDATE period value type must match DTSTART")


def _validate_recurrence_identity(
    master: Event,
    master_identity: _Identity,
    identity: _Identity,
    base_keys: set[tuple[str, Any]],
) -> None:
    if identity.key in base_keys:
        return
    target = identity.value
    date_target = is_date(target)
    lookup_start = (
        target if not date_target else datetime.combine(target, time.min)
    )
    for recurrence in master.rrules:
        rule = _dateutil_rrule(recurrence, master_identity)
        candidate = rule.after(lookup_start, inc=True)
        if candidate is None:
            continue
        candidate = (
            candidate.date()
            if date_target
            else _normalize_rule_datetime(candidate)
        )
        if candidate == target:
            return
    if not master.rrules and identity.key == master_identity.key:
        return
    raise InvalidCalendar("RECURRENCE-ID is not part of the recurrence set")


def _raw_bounds(
    master_identity: _Identity, query: _Query
) -> tuple[date | datetime, date | datetime]:
    value = master_identity.value
    if is_date(value):
        return query.start_date, query.end_date
    if value.tzinfo is None:
        timezone_value = query.floating_timezone
        return (
            query.start_datetime.astimezone(timezone_value).replace(tzinfo=None),
            query.end_datetime.astimezone(timezone_value).replace(tzinfo=None),
        )
    return (
        query.start_datetime.astimezone(value.tzinfo),
        query.end_datetime.astimezone(value.tzinfo),
    )


def _raw_kind(value: date | datetime) -> str:
    if is_date(value):
        return "date"
    if value.tzinfo is None:
        return "floating"
    return "utc"


def _rule_identities(
    master: Event,
    master_identity: _Identity,
    lower: date | datetime,
    upper: date | datetime,
    query: _Query,
    max_instances: int,
) -> tuple[dict[tuple[str, Any], _Identity], bool]:
    if len(master.rrules) > 1:
        raise InvalidCalendar("Multiple RRULE properties are not supported")
    master_interval = _event_interval(master, query)
    anchor = max(master_identity.value, lower)
    date_rule = is_date(master_identity.value)
    if date_rule:
        anchor = datetime.combine(anchor, time.min)

    result = {}
    truncated = False
    for recurrence in master.rrules:
        rule = _dateutil_rrule(recurrence, master_identity)
        before_lower = datetime.combine(lower, time.min) if date_rule else lower
        preceding = rule.before(before_lower, inc=False)
        if preceding is not None:
            value = (
                preceding.date()
                if date_rule
                else _normalize_rule_datetime(preceding)
            )
            identity = _identity(value, query)
            interval = _base_interval(identity, master_interval)
            if interval.end > lower:
                result[identity.key] = identity
        cursor = anchor
        while True:
            values = list(rule.xafter(cursor, max_instances + 1, inc=True))
            if not values:
                break
            count_in_window = 0
            for raw_value in values:
                value = (
                    raw_value.date()
                    if date_rule
                    else _normalize_rule_datetime(raw_value)
                )
                identity = _identity(value, query)
                if lower <= value <= upper:
                    result[identity.key] = identity
                    count_in_window += 1
            if count_in_window > max_instances:
                truncated = True
            last = values[-1]
            if last >= (datetime.combine(upper, time.min) if date_rule else upper):
                break
            break
    return result, truncated


def _dateutil_rrule(
    recurrence: vRecur, master_identity: _Identity
) -> dateutil_rrule.rrule:
    supported = {
        "FREQ",
        "INTERVAL",
        "COUNT",
        "UNTIL",
        "BYSETPOS",
        "BYMONTH",
        "BYMONTHDAY",
        "BYYEARDAY",
        "BYWEEKNO",
        "BYHOUR",
        "BYMINUTE",
        "BYSECOND",
        "BYDAY",
        "WKST",
    }
    unsupported = set(recurrence) - supported
    if unsupported:
        raise InvalidCalendar(f"Unsupported RRULE part: {sorted(unsupported)[0]}")
    freq_value = recurrence["FREQ"][0]
    frequency = getattr(dateutil_rrule, str(freq_value))
    start = master_identity.value
    kwargs = {
        "dtstart": start if not is_date(start) else datetime.combine(start, time.min)
    }
    rule_parts = (
        "INTERVAL",
        "COUNT",
        "BYSETPOS",
        "BYMONTH",
        "BYMONTHDAY",
        "BYYEARDAY",
        "BYWEEKNO",
        "BYHOUR",
        "BYMINUTE",
        "BYSECOND",
    )
    for name in rule_parts:
        if name in recurrence:
            kwargs[name.lower()] = _as_list(recurrence[name])
    if "BYDAY" in recurrence:
        kwargs["byweekday"] = [
            dateutil_rrule.weekday(weekday.week_days[weekday.weekday])(weekday.relative)
            if weekday.relative is not None
            else dateutil_rrule.weekday(weekday.week_days[weekday.weekday])
            for weekday in recurrence["BYDAY"]
        ]
    if "WKST" in recurrence:
        weekday = recurrence["WKST"][0]
        kwargs["wkst"] = dateutil_rrule.weekday(weekday.week_days[weekday.weekday])
    if "UNTIL" in recurrence:
        kwargs["until"] = _rule_until(recurrence["UNTIL"][0], start)
    try:
        return dateutil_rrule.rrule(frequency, **kwargs)
    except (TypeError, ValueError, OverflowError) as error:
        raise InvalidCalendar(f"Invalid RRULE: {recurrence.to_ical().decode()}") from error


def _as_list(values: list[Any]) -> Any:
    return values[0] if len(values) == 1 else values


def _rule_until(value: date | datetime, start: date | datetime) -> date | datetime:
    if is_date(start):
        if not is_date(value):
            raise InvalidCalendar("RRULE UNTIL value type must match DTSTART")
        return value
    if start.tzinfo is None:
        if is_date(value) or value.tzinfo is not None:
            raise InvalidCalendar("Floating RRULE UNTIL must be floating")
        return value
    if is_date(value) or value.tzinfo is None:
        raise InvalidCalendar("Timezoned RRULE UNTIL must be a UTC date-time")
    return value.astimezone(UTC)


def _latest_range(
    versions: list[_Version], identity: _Identity
) -> _Version | None:
    result = None
    cutoff_key = None
    for version in versions:
        cutoff = _range_cutoff(version, identity)
        if cutoff is None:
            continue
        cutoff_value, key = cutoff
        if result is None or key > cutoff_key or (
            key == cutoff_key and _version_key(version) > _version_key(result)
        ):
            result = version
            cutoff_key = key
    return result


def _range_cutoff(
    version: _Version, identity: _Identity
) -> tuple[date | datetime, tuple[str, Any]] | None:
    raw = version.event.RECURRENCE_ID
    if is_date(raw) == is_date(identity.value):
        if is_date(raw):
            if raw <= identity.value:
                return raw, ("date", raw)
        elif (raw.tzinfo is None) == (identity.value.tzinfo is None):
            if raw.tzinfo is None:
                if raw <= identity.value:
                    return raw, ("floating", raw)
            else:
                key = raw.astimezone(UTC)
                if key <= identity.value.astimezone(UTC):
                    return raw, ("utc", key)
    return None


def _base_or_range_interval(
    identity: _Identity,
    master_interval: _Interval,
    custom_periods: dict[tuple[str, Any], _Interval],
    range_version: _Version | None,
    query: _Query,
) -> _Interval:
    if range_version is None:
        if identity.key in custom_periods:
            return custom_periods[identity.key]
        return _base_interval(identity, master_interval)
    cutoff = _identity(range_version.event.RECURRENCE_ID, query)
    if identity.key == cutoff.key:
        return _event_interval(range_version.event, query)
    return _future_range_interval(identity, range_version.event, query)


def _base_interval(
    identity: _Identity,
    master_interval: _Interval,
) -> _Interval:
    start = identity.value
    duration = master_interval.nominal_duration or master_interval.duration
    if is_date(start):
        return _Interval(start, start + duration, duration, duration)
    if start.tzinfo is None:
        end = start + duration
        return _Interval(start, end, duration, duration)
    end = _add_nominal_duration(start, duration)
    return _Interval(start, end, duration, duration)


def _future_range_interval(
    identity: _Identity, event: Event, query: _Query
) -> _Interval:
    override_start = _required_start(event)
    override_interval = _event_interval(event, query)
    start = identity.value
    if is_date(start):
        end = start + override_interval.duration
        return _Interval(start, end, override_interval.duration, override_interval.duration)
    duration = override_interval.nominal_duration or override_interval.duration
    local_time = override_start.timetz().replace(tzinfo=None)
    start_naive = datetime.combine(start.date(), local_time)
    if override_start.tzinfo is None:
        start = start_naive
        end = start + duration
    else:
        wall_time = (
            override_start.replace(tzinfo=None)
            if start.tzinfo is None
            else override_start.astimezone(start.tzinfo).replace(tzinfo=None)
        )
        start = datetime.combine(start.date(), wall_time.timetz()).replace(
            tzinfo=start.tzinfo
        )
        end = _add_nominal_duration(start, duration)
    return _Interval(start, end, duration, duration)
