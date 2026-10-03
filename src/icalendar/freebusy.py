"""Privacy-preserving calendar exports for free/busy systems."""

from __future__ import annotations

import hashlib
import hmac
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from typing import TYPE_CHECKING, Literal

from icalendar.cal import (
    Calendar,
    Component,
    Event,
    Timezone,
    TimezoneDaylight,
    TimezoneStandard,
)
from icalendar.error import InvalidCalendar
from icalendar.parser import Parameters
from icalendar.prop import vBroken, vDDDLists, vDDDTypes, vRecur

if TYPE_CHECKING:
    from collections.abc import Mapping


FloatingTimePolicy = Literal["reject", "assume-utc", "keep"]

_PRODID = "-//icalendar//Free-Busy Export//EN"
_EVENT_PROPERTIES = (
    "DTSTAMP",
    "UID",
    "DTSTART",
    "DTEND",
    "DURATION",
    "RECURRENCE-ID",
    "RRULE",
    "RDATE",
    "EXDATE",
    "SEQUENCE",
    "STATUS",
    "TRANSP",
)
_TIME_PROPERTIES = frozenset(
    {
        "DTSTAMP",
        "DTSTART",
        "DTEND",
        "RECURRENCE-ID",
        "RDATE",
        "EXDATE",
    }
)
_TIME_PARAMETERS = frozenset({"VALUE", "TZID"})
_TIMEZONE_PROPERTIES = (
    "TZID",
    "DTSTART",
    "TZOFFSETFROM",
    "TZOFFSETTO",
    "RRULE",
    "RDATE",
)
_TIMEZONE_SUBCOMPONENT_TYPES = {
    "STANDARD": TimezoneStandard,
    "DAYLIGHT": TimezoneDaylight,
}
_RECURRENCE_RULE_PARTS = frozenset(
    {
        "FREQ",
        "UNTIL",
        "COUNT",
        "INTERVAL",
        "BYSECOND",
        "BYMINUTE",
        "BYHOUR",
        "BYDAY",
        "BYMONTHDAY",
        "BYYEARDAY",
        "BYWEEKNO",
        "BYMONTH",
        "BYSETPOS",
        "WKST",
        "RSCALE",
        "SKIP",
    }
)
_SINGLE_EVENT_PROPERTIES = frozenset(_EVENT_PROPERTIES) - frozenset(
    {"RDATE", "EXDATE"}
)
_EVENT_STATUSES = frozenset({"TENTATIVE", "CONFIRMED", "CANCELLED"})
_EVENT_TRANSPARENCIES = frozenset({"OPAQUE", "TRANSPARENT"})


def export_busy_time_calendar(
    calendar: Calendar,
    secret: bytes | str,
    *,
    floating_time: FloatingTimePolicy = "reject",
) -> Calendar:
    """Return a whitelisted, UID-anonymized copy of ``calendar``.

    Only VEVENT timing, recurrence, status, sequence and UID properties are
    exported. Descriptions, summaries, locations, attachments, alarms,
    relationships and all X-/unknown properties are removed.

    ``floating_time`` selects the explicit policy for DATE-TIME and TIME
    values without a TZID:

    - ``"reject"`` raises :class:`~icalendar.error.InvalidCalendar`.
    - ``"assume-utc"`` converts the wall time to the same UTC wall time.
    - ``"keep"`` retains the floating value.

    The caller-supplied secret is used only with HMAC-SHA-256 to derive stable
    identifiers. It is never stored on or serialized into the result.
    """
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    if not isinstance(secret, bytes) or not secret:
        raise ValueError("secret must be a non-empty bytes or string value")
    if floating_time not in {"reject", "assume-utc", "keep"}:
        raise ValueError(
            "floating_time must be 'reject', 'assume-utc' or 'keep'"
        )

    source_calendar = calendar.parse()
    source_timezones = _collect_timezones(source_calendar)
    source_events = list(source_calendar.walk("VEVENT"))
    uid_map = {}
    timezone_map = {}
    timezone_sources = {}

    for event in source_events:
        original_uid = event.get("UID")
        if original_uid is None:
            raise InvalidCalendar("VEVENT without UID cannot be exported")
        if isinstance(original_uid, list):
            raise InvalidCalendar("VEVENT with multiple UID properties cannot be exported")
        _validate_event_property_multiplicity(event)
        uid_text = str(original_uid)
        uid_map[uid_text] = _digest(secret, "uid", uid_text)
        _collect_timezone_references(
            event, secret, source_timezones, timezone_map, timezone_sources
        )

    result = Calendar()
    result.add("VERSION", "2.0")
    result.add("PRODID", _PRODID)
    result.add("CALSCALE", "GREGORIAN")

    for original_tzid in sorted(timezone_map):
        exported_timezone = _export_timezone(
            source_timezones[original_tzid], timezone_map[original_tzid]
        )
        timezone_sources[original_tzid] = exported_timezone.to_tz(lookup_tzid=False)
        result.add_component(exported_timezone)

    exported_events = [
        _export_event(
            event,
            uid_map,
            source_timezones,
            timezone_map,
            timezone_sources,
            floating_time,
        )
        for event in source_events
    ]
    exported_events.sort(key=lambda event: event.to_ical(sorted=False))
    for event in exported_events:
        result.add_component(event)
    return result


def _digest(secret: bytes, namespace: str, value: str) -> str:
    return hmac.new(
        secret, f"{namespace}\x1f{value}".encode("utf-8"), hashlib.sha256
    ).hexdigest()[:32]


def _collect_timezones(calendar: Calendar) -> Mapping[str, Timezone]:
    result = {}
    for timezone_component in calendar.timezones:
        tzid = timezone_component.get("TZID")
        if tzid is None:
            raise InvalidCalendar("VTIMEZONE without TZID cannot be exported")
        tzid_text = str(tzid)
        if tzid_text in result:
            raise InvalidCalendar(f"Duplicate VTIMEZONE definition for {tzid_text!r}")
        result[tzid_text] = timezone_component
    return result


def _collect_timezone_references(
    component: Component,
    secret: bytes,
    source_timezones: Mapping[str, Timezone],
    timezone_map: dict[str, str],
    timezone_sources: dict[str, object],
) -> None:
    for name in _TIME_PROPERTIES:
        if name == "DTSTAMP":
            continue
        for value in _property_values(component.get(name)):
            _collect_timezone_reference(
                value, secret, source_timezones, timezone_map, timezone_sources
            )


def _collect_timezone_reference(
    value: object,
    secret: bytes,
    source_timezones: Mapping[str, Timezone],
    timezone_map: dict[str, str],
    timezone_sources: dict[str, object],
) -> None:
    if isinstance(value, vDDDLists):
        for item in value.dts:
            _collect_timezone_reference(
                item, secret, source_timezones, timezone_map, timezone_sources
            )
        return
    if isinstance(value, vRecur):
        return
    if not hasattr(value, "params"):
        return

    tzid = value.params.get("TZID")
    if tzid is not None:
        tzid = str(tzid)
        if tzid == "UTC":
            return
        if tzid not in timezone_map:
            if tzid not in source_timezones:
                raise InvalidCalendar(f"Missing VTIMEZONE definition for {tzid!r}")
            timezone_map[tzid] = "TZ-" + _digest(secret, "tzid", tzid)


def _export_timezone(source: Timezone, tzid: str) -> Timezone:
    result = Timezone()
    for name in _TIMEZONE_PROPERTIES:
        for value in _property_values(source.get(name)):
            if name == "TZID":
                continue
            copied_value = (
                _sanitize_recur(value, floating_time="keep")
                if name == "RRULE"
                else deepcopy(value)
            )
            _copy_property(result, name, copied_value, (), {})
    result.add("TZID", tzid)

    for source_child in source.subcomponents:
        child_type = _TIMEZONE_SUBCOMPONENT_TYPES.get(source_child.name)
        if child_type is None:
            continue
        child = child_type()
        for name in _TIMEZONE_PROPERTIES[1:]:
            for value in _property_values(source_child.get(name)):
                copied_value = (
                    _sanitize_recur(value, floating_time="keep")
                    if name == "RRULE"
                    else deepcopy(value)
                )
                _copy_property(child, name, copied_value, (), {})
        result.add_component(child)

    result.subcomponents.sort(key=lambda child: child.to_ical(sorted=False))
    return result


def _export_event(
    event: Event,
    uid_map: Mapping[str, str],
    source_timezones: Mapping[str, Timezone],
    timezone_map: Mapping[str, str],
    timezone_sources: Mapping[str, object],
    floating_time: FloatingTimePolicy,
) -> Event:
    result = Event()
    for name in _EVENT_PROPERTIES:
        for value in _property_values(event.get(name)):
            if isinstance(value, vBroken):
                raise InvalidCalendar(f"Broken {name} property cannot be exported")
            if name == "UID":
                result.add("UID", uid_map[str(value)])
            elif name == "RRULE":
                if not isinstance(value, vRecur):
                    raise InvalidCalendar("RRULE must be a recurrence rule")
                transformed = _sanitize_recur(value, floating_time=floating_time)
                _copy_property(result, name, transformed, (), {})
            elif name in _TIME_PROPERTIES:
                transformed = _transform_time_value(
                    value,
                    source_timezones,
                    timezone_map,
                    timezone_sources,
                    floating_time,
                    name == "DTSTAMP",
                )
                _copy_property(
                    result,
                    name,
                    transformed,
                    _TIME_PARAMETERS,
                    timezone_map,
                    False,
                )
            else:
                _validate_scalar_event_property(name, value)
                _copy_property(
                    result,
                    name,
                    value,
                    (),
                    {},
                )
    return result


def _validate_event_property_multiplicity(event: Event) -> None:
    for name in _SINGLE_EVENT_PROPERTIES:
        if isinstance(event.get(name), list):
            raise InvalidCalendar(f"VEVENT has multiple {name} properties")


def _validate_scalar_event_property(name: str, value: object) -> None:
    if name == "STATUS" and str(value).upper() not in _EVENT_STATUSES:
        raise InvalidCalendar(f"Unsupported VEVENT STATUS: {value!r}")
    if name == "TRANSP" and str(value).upper() not in _EVENT_TRANSPARENCIES:
        raise InvalidCalendar(f"Unsupported VEVENT TRANSP: {value!r}")


def _transform_time_value(
    value: object,
    source_timezones: Mapping[str, Timezone],
    timezone_map: Mapping[str, str],
    timezone_sources: Mapping[str, object],
    floating_time: FloatingTimePolicy,
    normalize_to_utc: bool,
) -> object:
    if isinstance(value, vDDDLists):
        items = [
            _transform_time_value(
                item,
                source_timezones,
                timezone_map,
                timezone_sources,
                floating_time,
                normalize_to_utc,
            )
            for item in value.dts
        ]
        return vDDDLists(
            [item.dt for item in items],
            params=_allowed_params(
                value.params,
                _TIME_PARAMETERS,
                timezone_map,
                not normalize_to_utc,
            ),
        )
    if isinstance(value, vRecur):
        return _sanitize_recur(value, floating_time)
    if not isinstance(value, vDDDTypes):
        raise InvalidCalendar(f"Unsupported temporal property value {type(value)!r}")

    dt = value.dt
    dt = _transform_temporal(
        dt,
        value.params,
        source_timezones,
        timezone_map,
        timezone_sources,
        floating_time,
        normalize_to_utc,
    )
    return vDDDTypes(
        dt,
        params=_allowed_params(
            value.params,
            _TIME_PARAMETERS,
            timezone_map,
            not normalize_to_utc,
        ),
    )


def _sanitize_recur(
    value: vRecur, *, floating_time: FloatingTimePolicy
) -> vRecur:
    if not isinstance(value, vRecur):
        raise InvalidCalendar(f"Unsupported recurrence rule {type(value)!r}")
    result = vRecur()
    result.params = Parameters()
    for name, rule_value in value.items():
        if name in _RECURRENCE_RULE_PARTS:
            result[name] = deepcopy(rule_value)
    if "UNTIL" in result:
        result["UNTIL"] = [
            _transform_temporal(
                item,
                Parameters(),
                {},
                {},
                {},
                floating_time,
                False,
            )
            for item in result["UNTIL"]
        ]
    return result


def _transform_temporal(
    value: object,
    params: Parameters,
    source_timezones: Mapping[str, Timezone],
    timezone_map: Mapping[str, str],
    timezone_sources: Mapping[str, object],
    floating_time: FloatingTimePolicy,
    normalize_to_utc: bool,
) -> object:
    if isinstance(value, tuple):
        return tuple(
            _transform_temporal(
                item,
                params,
                source_timezones,
                timezone_map,
                timezone_sources,
                floating_time,
                normalize_to_utc,
            )
            for item in value
        )
    if isinstance(value, date) and not isinstance(value, datetime):
        if params.get("TZID") is not None:
            raise InvalidCalendar("DATE value cannot have a TZID parameter")
        return deepcopy(value)
    if isinstance(value, timedelta):
        return deepcopy(value)
    if not isinstance(value, (datetime, time)):
        raise InvalidCalendar(f"Unsupported temporal value {type(value)!r}")

    tzid = params.get("TZID")
    if tzid is not None:
        tzid = str(tzid)
        if tzid == "UTC":
            aware_value = (
                value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
            )
            return (
                _utc(aware_value)
                if normalize_to_utc
                else aware_value
            )
        if normalize_to_utc and value.tzinfo is None:
            source_timezone = source_timezones.get(tzid)
            if source_timezone is None:
                raise InvalidCalendar(f"Missing VTIMEZONE definition for {tzid!r}")
            source_tzinfo = source_timezone.to_tz(lookup_tzid=False)
            if isinstance(value, datetime):
                aware_value = value.replace(tzinfo=source_tzinfo)
            else:
                aware_value = time(
                    value.hour,
                    value.minute,
                    value.second,
                    value.microsecond,
                    tzinfo=source_tzinfo,
            )
            return _utc(aware_value)
        if normalize_to_utc:
            return _utc(value)
        target_tzinfo = timezone_sources[tzid]
        wall_time = value.replace(tzinfo=None) if value.tzinfo is not None else value
        if isinstance(wall_time, datetime):
            aware_value = wall_time.replace(tzinfo=target_tzinfo)
        else:
            aware_value = time(
                wall_time.hour,
                wall_time.minute,
                wall_time.second,
                wall_time.microsecond,
                tzinfo=target_tzinfo,
        )
        if normalize_to_utc:
            return _utc(aware_value)
        return aware_value

    if isinstance(value, datetime):
        if value.tzinfo is None:
            if floating_time == "reject":
                raise InvalidCalendar("Floating DATE-TIME value is not allowed")
            if floating_time == "assume-utc":
                return value.replace(tzinfo=timezone.utc)
            return value
        if _is_utc(value):
            return _utc(value)
        return _utc(value)

    if value.tzinfo is None:
        if floating_time == "reject":
            raise InvalidCalendar("Floating TIME value is not allowed")
        if floating_time == "assume-utc":
            return value.replace(tzinfo=timezone.utc)
        return value
    return _utc(value)


def _copy_property(
    component: Component,
    name: str,
    value: object,
    allowed_parameters: tuple[str, ...],
    tzinfo_map: Mapping[str, str],
    filter_parameters: bool = True,
) -> None:
    copied = deepcopy(value)
    if filter_parameters and hasattr(copied, "params"):
        copied.params = _allowed_params(copied.params, allowed_parameters, tzinfo_map)
    component.add(name, copied, encode=False)


def _allowed_params(
    params: Parameters,
    allowed: tuple[str, ...] | frozenset[str],
    timezone_map: Mapping[str, str],
    retain_tzid: bool = True,
) -> Parameters:
    result = Parameters()
    for name, value in params.items():
        name = str(name).upper()
        if name not in allowed:
            continue
        if name == "TZID" and not retain_tzid:
            continue
        copied = deepcopy(value)
        if name == "TZID" and copied != "UTC":
            copied = timezone_map[str(copied)]
        result[name] = copied
    return result


def _property_values(value: object) -> tuple[object, ...]:
    if value is None:
        return ()
    if isinstance(value, list):
        return tuple(value)
    return (value,)


def _is_utc(value: datetime | time) -> bool:
    return value.utcoffset() == timedelta(0)


def _utc(value: datetime | time) -> datetime | time:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).replace(tzinfo=timezone.utc)
    utc_value = datetime.combine(date.min, value).astimezone(timezone.utc).timetz()
    return utc_value.replace(tzinfo=timezone.utc)


__all__ = ["FloatingTimePolicy", "export_busy_time_calendar"]
