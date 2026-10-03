"""Publish only busy-time information from a private calendar."""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
from collections.abc import Iterable
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from icalendar.cal import Calendar, Event, Timezone
from icalendar.parser import Parameters
from icalendar.prop import vRecur, vUid
from icalendar.prop.dt import vDDDLists, vDDDTypes, vPeriod
from icalendar.timezone import is_utc
from icalendar.timezone.tzid import tzid_from_tzinfo

_PUBLIC_PRODID = "-//collective//icalendar//busy-time//EN"
_UID_PREFIX = "busy-"
_TZID_PREFIX = "busy-tz-"
_DIGEST_LENGTH = 32


class FloatingTimePolicy(str, Enum):
    """How to handle local DATE-TIME values without a timezone or UTC marker."""

    REJECT = "reject"
    KEEP = "keep"
    ASSUME_UTC = "assume-utc"


_EVENT_PROPERTIES = frozenset(
    {
        "DTSTAMP",
        "UID",
        "DTSTART",
        "DTEND",
        "DURATION",
        "RRULE",
        "RDATE",
        "EXDATE",
        "RECURRENCE-ID",
        "SEQUENCE",
        "STATUS",
        "TRANSP",
        "RELATED-TO",
    }
)

_DATE_PARAMETERS = frozenset({"TZID", "VALUE"})
_RELATED_TO_PARAMETERS = frozenset({"VALUE", "RELTYPE", "GAP"})
_TIMEZONE_PROPERTIES = frozenset(
    {"TZID", "DTSTART", "TZOFFSETFROM", "TZOFFSETTO", "RRULE", "RDATE"}
)
_TIMEZONE_SUBCOMPONENTS = frozenset({"STANDARD", "DAYLIGHT"})


def _digest(key: bytes, namespace: str, value: str) -> str:
    """Return a stable, unpadded token for *value*."""
    message = f"{namespace}\x00{value}".encode("utf-8")
    digest = hmac.new(key, message, hashlib.sha256).digest()
    return base64.b32hexencode(digest)[:_DIGEST_LENGTH].decode("ascii")


def _map_uid(key: bytes, uid: str) -> str:
    return _UID_PREFIX + _digest(key, "uid", uid)


def _map_tzid(key: bytes, tzid: str) -> str:
    return _TZID_PREFIX + _digest(key, "tzid", tzid)


def _as_values(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def _sanitize_parameters(allowed: Iterable[str], value: Any) -> Parameters:
    """Copy only explicitly allowed property parameters."""
    source = getattr(value, "params", None)
    result = Parameters()
    if source is not None:
        allowed_set = set(allowed)
        for name in source:
            if name in allowed_set:
                result[name] = copy.deepcopy(source[name])
    return result


def _dt_nodes(value: Any) -> Iterable[Any]:
    """Yield objects in a copied date/time property that carry datetimes."""
    if isinstance(value, vRecur):
        until = value.get("UNTIL")
        if until is not None:
            yield from until
        return
    if isinstance(value, vDDDLists):
        yield from value.dts
        return
    if isinstance(value, vDDDTypes):
        yield value
        if isinstance(value.dt, tuple):
            yield vPeriod(value.dt, value.params)
        return
    if isinstance(value, vPeriod):
        yield value


def _datetimes(value: Any) -> Iterable[datetime]:
    if isinstance(value, vRecur):
        for node in value.get("UNTIL", []):
            if isinstance(node, datetime):
                yield node
        return
    if isinstance(value, vDDDLists):
        nodes = []
        for node in value.dts:
            if isinstance(node.dt, tuple):
                nodes.append(vPeriod(node.dt, value.params))
            else:
                nodes.append(node)
    elif isinstance(value, vDDDTypes) and isinstance(value.dt, tuple):
        nodes = [vPeriod(value.dt, value.params)]
    elif isinstance(value, (vDDDTypes, vPeriod)):
        nodes = [value]
    else:
        nodes = []

    for node in nodes:
        if isinstance(node, vPeriod):
            yield node.start
            if isinstance(node.end, datetime):
                yield node.end
        elif hasattr(node, "dt") and isinstance(node.dt, datetime):
            yield node.dt


def _parameter_tzids(value: Any) -> set[str]:
    result = set()
    parent_params = getattr(value, "params", None)
    if parent_params is not None and parent_params.get("TZID") is not None:
        result.add(str(parent_params["TZID"]))
    for node in _dt_nodes(value):
        params = getattr(node, "params", None)
        if params is not None and params.get("TZID") is not None:
            result.add(str(params["TZID"]))
    return result


def _replace_datetime(dt: datetime, params: Parameters, policy: FloatingTimePolicy) -> datetime:
    tzid = params.get("TZID")
    if tzid is not None and str(tzid).upper() == "UTC":
        del params["TZID"]
        if dt.tzinfo is not None:
            return dt.astimezone(timezone.utc)
        return dt.replace(tzinfo=timezone.utc)
    if is_utc(dt):
        if tzid is not None:
            del params["TZID"]
        return dt.astimezone(timezone.utc)
    if dt.tzinfo is not None:
        if tzid is not None:
            return dt
        dt_tzid = tzid_from_tzinfo(dt.tzinfo)
        if dt_tzid == "UTC":
            return dt.astimezone(timezone.utc)
        raise ValueError("Timezone-aware DATE-TIME values require a TZID parameter")
    if policy is FloatingTimePolicy.KEEP:
        return dt
    if policy is FloatingTimePolicy.ASSUME_UTC:
        return dt.replace(tzinfo=timezone.utc)
    raise ValueError("Floating DATE-TIME values require an explicit policy")


def _transform_times(
    value: Any,
    tzid_mapping: dict[str, str],
    policy: FloatingTimePolicy,
) -> None:
    """Rewrite TZID parameters and apply the floating-time policy."""
    if isinstance(value, vRecur):
        until = value.get("UNTIL")
        if not until:
            return
        value["UNTIL"] = [
            _replace_datetime(node, Parameters(), policy)
            if isinstance(node, datetime)
            else node
            for node in until
        ]
        return

    nodes = list(_dt_nodes(value))
    for node in nodes:
        params = getattr(node, "params", None)
        if params is None:
            continue
        tzid = params.get("TZID")
        if tzid is not None and str(tzid) in tzid_mapping:
            params["TZID"] = tzid_mapping[str(tzid)]

    replacements: list[tuple[datetime, datetime]] = []
    if isinstance(value, vDDDLists):
        if "TZID" in value.params:
            value.params["TZID"] = tzid_mapping[str(value.params["TZID"])]
        for node in value.dts:
            node.params = Parameters(value.params)
            if isinstance(node.dt, tuple):
                period = vPeriod(node.dt, value.params)
                _collect_datetime_replacements(period, policy, replacements)
                node.dt = (period.start, period.duration if period.by_duration else period.end)
                continue
            _collect_datetime_replacements(node, policy, replacements)
        for old, new in replacements:
            _replace_list_datetime(value, old, new)
        return

    _collect_datetime_replacements(value, policy, replacements)
    for old, new in replacements:
        _replace_scalar_datetime(value, old, new)


def _collect_datetime_replacements(
    value: Any,
    policy: FloatingTimePolicy,
    replacements: list[tuple[datetime, datetime]],
) -> None:
    if isinstance(value, vDDDTypes):
        if isinstance(value.dt, datetime):
            new_dt = _replace_datetime(value.dt, value.params, policy)
            replacements.append((value.dt, new_dt))
        elif isinstance(value.dt, tuple):
            period = vPeriod(value.dt, value.params)
            _collect_datetime_replacements(period, policy, replacements)
        return
    if isinstance(value, vPeriod):
        new_start = _replace_datetime(value.start, value.params, policy)
        replacements.append((value.start, new_start))
        if isinstance(value.end, datetime):
            new_end = _replace_datetime(value.end, value.params, policy)
            replacements.append((value.end, new_end))


def _replace_list_datetime(value: vDDDLists, old: datetime, new: datetime) -> None:
    for node in value.dts:
        if node.dt is old:
            node.dt = new
        elif isinstance(node.dt, tuple):
            node.dt = tuple(new if item is old else item for item in node.dt)


def _replace_scalar_datetime(value: Any, old: datetime, new: datetime) -> None:
    if isinstance(value, vDDDTypes):
        value.dt = new
    elif isinstance(value, vPeriod):
        if value.start is old:
            value.start = new
        if value.end is old:
            value.end = new


def _sanitize_related_to(
    value: Any,
    retained_uids: set[str],
    uid_mapping: dict[str, str],
) -> Any | None:
    params = _sanitize_parameters(_RELATED_TO_PARAMETERS, value)
    value_type = str(params.get("VALUE", "UID")).upper()
    if value_type != "UID":
        return None
    original = str(value)
    if original not in retained_uids:
        return None
    params["VALUE"] = "UID"
    return vUid(uid_mapping[original], params=params)


def _sanitize_event(
    event: Event,
    uid_mapping: dict[str, str],
    retained_uids: set[str],
    tzid_mapping: dict[str, str],
    floating_time_policy: FloatingTimePolicy,
) -> Event:
    result = Event()
    for name in event:
        if name not in _EVENT_PROPERTIES:
            continue
        for original_value in _as_values(event[name]):
            value = copy.deepcopy(original_value)
            if name == "UID":
                uid = str(value)
                value = vUid(uid_mapping[uid])
            elif name == "RELATED-TO":
                value = _sanitize_related_to(value, retained_uids, uid_mapping)
                if value is None:
                    continue
            else:
                params = _sanitize_parameters(_DATE_PARAMETERS, value)
                if hasattr(value, "params"):
                    value.params = params
                _transform_times(value, tzid_mapping, floating_time_policy)
            result.add(name, value, encode=False)
    return result


def _sanitize_timezone(
    timezone: Timezone,
    old_tzid: str,
    new_tzid: str,
) -> Timezone:
    result = Timezone()
    result.add("TZID", new_tzid, encode=False)
    for subcomponent in timezone.subcomponents:
        if subcomponent.name not in _TIMEZONE_SUBCOMPONENTS:
            continue
        copy_subcomponent = type(subcomponent)()
        for name in subcomponent:
            if name not in _TIMEZONE_PROPERTIES:
                continue
            for original_value in _as_values(subcomponent[name]):
                value = copy.deepcopy(original_value)
                if hasattr(value, "params"):
                    value.params = Parameters()
                copy_subcomponent.add(name, value, encode=False)
        result.add_component(copy_subcomponent)
    return result


def _generated_timezone(
    source: Timezone | None,
    old_tzid: str,
    sample_datetime: datetime | None,
    new_tzid: str,
) -> Timezone:
    if source is not None:
        return _sanitize_timezone(source, old_tzid, new_tzid)
    if sample_datetime is None or sample_datetime.tzinfo is None:
        raise ValueError(f"Missing VTIMEZONE definition for TZID {old_tzid!r}")
    tzinfo = sample_datetime.tzinfo
    tzid = tzid_from_tzinfo(tzinfo)
    if tzid is None:
        return _sanitize_timezone(Timezone.from_tzinfo(tzinfo, old_tzid), old_tzid, new_tzid)
    generated = Timezone.from_tzinfo(tzinfo, tzid)
    generated["TZID"] = old_tzid
    return _sanitize_timezone(generated, old_tzid, new_tzid)


def export_busy_time(
    calendar: Calendar,
    secret_key: bytes | bytearray | memoryview | str,
    floating_time_policy: FloatingTimePolicy | str = FloatingTimePolicy.REJECT,
) -> Calendar:
    """Return a calendar containing only busy-time event properties.

    The exported calendar and all of its values are independent copies.
    UIDs and TZIDs are replaced with deterministic HMAC-SHA-256 tokens.
    All property and component names are selected from explicit allow lists.

    Parameters:
        calendar: The private source calendar.
        secret_key: Caller-provided key of at least 32 bytes.
        floating_time_policy: How to handle timezone-less DATE-TIME values.

    Raises:
        ValueError: If the key is too short or the source cannot be exported
            under the selected floating-time policy.
    """
    if isinstance(secret_key, str):
        key_bytes = secret_key.encode("utf-8")
    else:
        key_bytes = bytes(secret_key)
    if len(key_bytes) < 32:
        raise ValueError("key must contain at least 32 bytes")
    policy = FloatingTimePolicy(floating_time_policy)

    if not isinstance(calendar, Calendar):
        raise ValueError("calendar must be a Calendar")

    source_events = list(calendar.walk("VEVENT"))
    retained_events = [event for event in source_events if event.get("UID") is not None]
    if len(retained_events) != len(source_events):
        raise ValueError("All exported VEVENT components must have a UID")

    retained_uids = {str(event["UID"]) for event in retained_events}
    uid_mapping = {uid: _map_uid(key_bytes, uid) for uid in retained_uids}

    referenced_tzids: set[str] = set()
    tzid_samples: dict[str, datetime | None] = {}
    for event in retained_events:
        for name in _EVENT_PROPERTIES - {"UID", "RELATED-TO"}:
            if name not in event:
                continue
            for value in _as_values(event[name]):
                parameter_tzids = _parameter_tzids(value)
                referenced_tzids.update(parameter_tzids)
                for tzid in parameter_tzids:
                    if tzid.upper() != "UTC" and tzid not in tzid_samples:
                        tzid_samples[tzid] = next(iter(_datetimes(value)), None)
                for dt in _datetimes(value):
                    tzid = tzid_from_tzinfo(dt.tzinfo) if dt.tzinfo is not None else None
                    if (
                        tzid not in (None, "UTC")
                        and tzid not in parameter_tzids
                        and tzid not in tzid_samples
                    ):
                        tzid_samples[tzid] = dt

    source_timezones = {
        str(timezone["TZID"]): timezone
        for timezone in calendar.walk("VTIMEZONE")
        if timezone.get("TZID") is not None
    }
    tzid_mapping = {tzid: _map_tzid(key_bytes, tzid) for tzid in referenced_tzids}

    output = Calendar()
    output.add("VERSION", "2.0", encode=False)
    output.add("PRODID", _PUBLIC_PRODID, encode=False)
    output.add("CALSCALE", "GREGORIAN", encode=False)

    for tzid in sorted(referenced_tzids):
        if tzid.upper() == "UTC":
            continue
        if tzid not in tzid_mapping:
            raise ValueError(f"Missing mapping for TZID {tzid!r}")
        sanitized = _generated_timezone(
            source_timezones.get(tzid),
            tzid,
            tzid_samples.get(tzid),
            tzid_mapping[tzid],
        )
        output.add_component(sanitized)

    for event in retained_events:
        output.add_component(
            _sanitize_event(
                event,
                uid_mapping,
                retained_uids,
                tzid_mapping,
                policy,
            )
        )

    return output


__all__ = ["FloatingTimePolicy", "export_busy_time"]
