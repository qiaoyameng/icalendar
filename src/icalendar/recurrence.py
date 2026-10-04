"""Bounded expansion of recurring VEVENTs into concrete instances.

This module turns the recurrence description of a calendar (RRULE, RDATE,
EXDATE on a master component plus RECURRENCE-ID overrides sharing the same
UID) into the concrete meeting instances that actually occur inside a
caller-supplied time window.

Design notes:

- Instance identity is the *original* recurrence id (the DTSTART the master
  rule would have produced).  An override that moves an instance keeps the
  same identity, so rescheduled instances are neither lost nor duplicated
  when their new DTSTART crosses the query window in either direction.
- Expansion is bounded: rules are only evaluated inside the query window
  (plus a margin derived from THISANDFUTURE shifts and durations), and
  ``max_instances`` caps the number of generated candidates.  Hitting the
  cap sets :attr:`ExpansionResult.truncated` -- a truncated result is never
  silently returned as complete.
- DATE, floating and TZID-aware events are handled as three distinct kinds.
  Interpreting floating times requires the caller to pass ``floating_tz``.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import TYPE_CHECKING

from dateutil.rrule import rruleset, rrulestr

from icalendar.cal.event import Event

if TYPE_CHECKING:
    from collections.abc import Iterable

    from icalendar.cal.calendar import Calendar

__all__ = [
    "EventInstance",
    "ExpansionResult",
    "FloatingTimeError",
    "RecurrenceConflictError",
    "RecurrenceError",
    "expand_instances",
]


class RecurrenceError(ValueError):
    """Base error for problems encountered while expanding recurrences."""


class RecurrenceConflictError(RecurrenceError):
    """Two versions of one instance cannot be ordered by SEQUENCE/DTSTAMP."""


class FloatingTimeError(RecurrenceError):
    """A floating (timezone-less) value was used without ``floating_tz``."""


_KIND_DATE = "date"
_KIND_FLOATING = "floating"
_KIND_AWARE = "aware"

_THISANDFUTURE = "THISANDFUTURE"
_ZERO = dt.timedelta(0)
_MIN_STAMP = dt.datetime.min.replace(tzinfo=dt.timezone.utc)


def _kind_of(value: dt.date | dt.datetime) -> str:
    if isinstance(value, dt.datetime):
        return _KIND_AWARE if value.tzinfo is not None else _KIND_FLOATING
    return _KIND_DATE


@dataclass(frozen=True)
class EventInstance:
    """One concrete occurrence of a recurring event.

    ``recurrence_id`` is the identity of the instance: the start the master
    rule produced (or would have produced) for it, regardless of any
    override.  ``start``/``end`` are the effective times after overrides.
    For floating events they are interpreted in the ``floating_tz`` given to
    :func:`expand_instances` and returned as aware datetimes.
    """

    uid: str
    recurrence_id: dt.date | dt.datetime
    start: dt.date | dt.datetime
    end: dt.date | dt.datetime
    component: Event
    overridden: bool


@dataclass
class ExpansionResult:
    """Result of :func:`expand_instances`.

    ``truncated`` is True when ``max_instances`` stopped the rule expansion;
    in that case ``instances`` is a lower bound, not the complete answer.
    """

    instances: list[EventInstance]
    truncated: bool = False

    def __iter__(self):
        return iter(self.instances)

    def __len__(self) -> int:
        return len(self.instances)


@dataclass
class _FutureOverride:
    """A RECURRENCE-ID;RANGE=THISANDFUTURE override, reduced to its effect."""

    recurrence_id: dt.date | dt.datetime
    delta: dt.timedelta
    duration: dt.timedelta | None
    cancelled: bool


def _is_cancelled(event: Event) -> bool:
    return str(event.get("STATUS", "")).upper() == "CANCELLED"


def _version_key(event: Event) -> tuple[int, dt.datetime]:
    stamp = event.decoded("DTSTAMP", None)
    if not isinstance(stamp, dt.datetime):
        stamp = _MIN_STAMP
    elif stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return (event.sequence or 0, stamp)


def _resolve_version(events: list[Event], uid: str, what: str) -> Event:
    """Pick the authoritative version by SEQUENCE, then DTSTAMP.

    Versions that tie on both must be byte-identical, otherwise the
    situation is ambiguous and reported as a conflict.
    """
    best = max(events, key=_version_key)
    best_key = _version_key(best)
    ties = [e for e in events if _version_key(e) == best_key]
    payload = best.to_ical()
    if any(e.to_ical() != payload for e in ties):
        raise RecurrenceConflictError(
            f"UID {uid!r}: {what} has {len(ties)} versions with equal "
            f"SEQUENCE/DTSTAMP but different content"
        )
    return best


def _explicit_duration(event: Event) -> dt.timedelta | None:
    """Duration explicitly carried by the component, or None."""
    if event.get("DURATION") is not None:
        return event.decoded("DURATION")
    if event.get("DTEND") is not None:
        return event.end - event.start
    return None


def _recurrence_id_of(event: Event) -> dt.date | dt.datetime:
    rid = event.decoded("RECURRENCE-ID", None)
    if rid is None:
        raise RecurrenceError("override component without RECURRENCE-ID")
    if isinstance(rid, tuple):  # a PERIOD recurrence id is not supported
        raise RecurrenceError("RECURRENCE-ID with PERIOD value is not supported")
    return rid


def _range_of(event: Event) -> str:
    prop = event.get("RECURRENCE-ID")
    value = getattr(prop, "params", {}).get("RANGE", "")
    return str(value).upper()


def _intersects(
    start: dt.date | dt.datetime,
    end: dt.date | dt.datetime,
    window_start: dt.date | dt.datetime,
    window_end: dt.date | dt.datetime,
) -> bool:
    if end < start:
        end = start
    if start == end:  # zero-duration point instance
        return window_start <= start < window_end
    return start < window_end and end > window_start


def _chronological_key(value: dt.date | dt.datetime) -> tuple:
    """A sort key that can mix dates, floating and aware datetimes."""
    if isinstance(value, dt.datetime):
        if value.tzinfo is not None:
            return (1, value.astimezone(dt.timezone.utc).replace(tzinfo=None))
        return (1, value)
    return (0, dt.datetime.combine(value, dt.time.min))


class _Kind:
    """Per-UID-group value-kind handling: date, floating or aware."""

    def __init__(self, kind: str, floating_tz: dt.tzinfo | None):
        self.kind = kind
        self.floating_tz = floating_tz

    def normalize_window(
        self, start: dt.date | dt.datetime, end: dt.date | dt.datetime
    ) -> tuple:
        if self.kind == _KIND_AWARE:
            if self.floating_tz is not None:
                # date/naive bounds are interpreted in floating_tz
                return (self._interpret(start), self._interpret(end))
            for bound in (start, end):
                if not isinstance(bound, dt.datetime) or bound.tzinfo is None:
                    raise RecurrenceError(
                        "aware (TZID) events require aware datetime window "
                        f"bounds (or floating_tz to interpret them), got {bound!r}"
                    )
            return start, end
        if self.kind == _KIND_FLOATING:
            if self.floating_tz is None:
                raise FloatingTimeError(
                    "expanding floating (timezone-less) events requires "
                    "floating_tz to interpret them"
                )
            return (self._interpret(start), self._interpret(end))
        return (self._as_date(start), self._as_date(end))

    def _interpret(self, value: dt.date | dt.datetime) -> dt.datetime:
        """Interpret a query bound for a floating event."""
        if isinstance(value, dt.datetime):
            if value.tzinfo is not None:
                return value
            return value.replace(tzinfo=self.floating_tz)
        return dt.datetime.combine(value, dt.time.min, tzinfo=self.floating_tz)

    def _as_date(self, value: dt.date | dt.datetime) -> dt.date:
        if isinstance(value, dt.datetime):
            if value.tzinfo is not None and self.floating_tz is not None:
                value = value.astimezone(self.floating_tz)
            return value.date()
        return value

    def to_rule_datetime(self, value: dt.date | dt.datetime) -> dt.datetime:
        """Convert a value of this kind into the form dateutil works with."""
        if self.kind == _KIND_DATE and not isinstance(value, dt.datetime):
            return dt.datetime.combine(value, dt.time.min)
        if (
            self.kind == _KIND_FLOATING
            and isinstance(value, dt.datetime)
            and value.tzinfo is not None
        ):
            # rule values are naive; interpret aware bounds in floating_tz
            return value.astimezone(self.floating_tz).replace(tzinfo=None)
        return value

    def from_rule_datetime(self, value: dt.datetime) -> dt.date | dt.datetime:
        if self.kind == _KIND_DATE:
            return value.date()
        return value

    def present(self, value: dt.date | dt.datetime) -> dt.date | dt.datetime:
        """Convert an effective time into the reported, comparable form."""
        if self.kind == _KIND_FLOATING and isinstance(value, dt.datetime):
            return value.replace(tzinfo=self.floating_tz)
        return value


def _build_future_overrides(overrides: list[Event]) -> list[_FutureOverride]:
    """Reduce THISANDFUTURE overrides to (delta, duration, cancelled) effects."""
    result = []
    for event in overrides:
        rid = _recurrence_id_of(event)
        if event.get("DTSTART") is None:
            raise RecurrenceError(
                "THISANDFUTURE override without DTSTART cannot shift instances"
            )
        result.append(
            _FutureOverride(
                recurrence_id=rid,
                delta=event.start - rid,
                duration=_explicit_duration(event),
                cancelled=_is_cancelled(event),
            )
        )
    result.sort(key=lambda o: o.recurrence_id)
    return result


def _applicable_future_override(
    future_overrides: list[_FutureOverride],
    recurrence_id: dt.date | dt.datetime,
) -> _FutureOverride | None:
    """The THISANDFUTURE override with the greatest rid <= recurrence_id."""
    applicable = None
    for override in future_overrides:
        if override.recurrence_id <= recurrence_id:
            applicable = override
        else:
            break
    return applicable


def _expand_group(
    uid: str,
    master: Event | None,
    single_overrides: dict,
    future_overrides: list[_FutureOverride],
    kind: _Kind,
    window_start,
    window_end,
    max_instances: int,
) -> tuple[list[EventInstance], bool]:
    """Expand one UID group; returns (instances, truncated)."""
    # RFC 5545: a DATE DTSTART without DTEND/DURATION lasts the whole day,
    # a DATE-TIME without them is a point in time.
    default_duration = (
        dt.timedelta(days=1) if kind.kind == _KIND_DATE else _ZERO
    )
    master_duration = default_duration
    period_ends: dict = {}
    occurrences: list = []
    truncated = False

    if master is not None:
        if master.get("DTSTART") is None:
            raise RecurrenceError(f"UID {uid!r}: master component has no DTSTART")
        master_duration = _explicit_duration(master) or default_duration
        longest = master_duration
        for event in single_overrides.values():
            override_duration = _explicit_duration(event)
            if override_duration is not None:
                longest = max(longest, override_duration)
        for override in future_overrides:
            if override.duration is not None:
                longest = max(longest, override.duration)

        forward = max(
            (_ZERO, *(o.delta for o in future_overrides if o.delta > _ZERO))
        )
        backward = max(
            (_ZERO, *(-o.delta for o in future_overrides if o.delta < _ZERO))
        )
        # Generate rule candidates in the query window widened by the longest
        # duration and the largest THISANDFUTURE shift, so that instances
        # shifted into the window are still found.
        gen_start = window_start - longest - forward
        gen_end = window_end + backward

        rules = rruleset()
        dtstart = kind.to_rule_datetime(master.start)
        for recur in master.rrules:
            try:
                rules.rrule(
                    rrulestr(recur.to_ical().decode("ascii"), dtstart=dtstart)
                )
            except ValueError as exc:
                raise RecurrenceError(
                    f"UID {uid!r}: invalid RRULE {recur.to_ical()!r}: {exc}"
                ) from exc
        rules.rdate(dtstart)  # DTSTART is always part of the recurrence set
        for rdate_start, rdate_end in master.rdates:
            rules.rdate(kind.to_rule_datetime(rdate_start))
            if rdate_end is not None:
                period_ends[rdate_start] = rdate_end
        for exdate in master.exdates:
            rules.exdate(kind.to_rule_datetime(exdate))

        gen_start = kind.to_rule_datetime(gen_start)
        gen_end = kind.to_rule_datetime(gen_end)
        occurrences = rules.between(
            gen_start, gen_end, inc=True, count=max_instances + 1
        )
        if len(occurrences) > max_instances:
            truncated = True
            occurrences = occurrences[:max_instances]

    instances: list[EventInstance] = []
    seen: set = set()
    for occurrence in occurrences:
        rid = kind.from_rule_datetime(occurrence)
        if rid in seen or rid in single_overrides:
            continue  # single overrides fully define their instance
        seen.add(rid)
        start = rid
        end = period_ends.get(rid)
        if end is None:
            end = start + master_duration
        future = _applicable_future_override(future_overrides, rid)
        if future is not None:
            if future.cancelled:
                continue
            start = rid + future.delta
            end = start + (future.duration or master_duration)
        if _intersects(
            kind.present(start), kind.present(end), window_start, window_end
        ):
            instances.append(
                EventInstance(
                    uid=uid,
                    recurrence_id=rid,
                    start=kind.present(start),
                    end=kind.present(end),
                    component=master,
                    overridden=future is not None,
                )
            )

    for rid, event in single_overrides.items():
        if _is_cancelled(event):
            continue
        if event.get("DTSTART") is None:
            raise RecurrenceError(
                f"UID {uid!r}: override for {rid!r} has no DTSTART"
            )
        start = event.start
        duration = _explicit_duration(event)
        if duration is None:
            duration = master_duration
        end = start + duration
        if _intersects(
            kind.present(start), kind.present(end), window_start, window_end
        ):
            instances.append(
                EventInstance(
                    uid=uid,
                    recurrence_id=rid,
                    start=kind.present(start),
                    end=kind.present(end),
                    component=event,
                    overridden=True,
                )
            )
    return instances, truncated


def expand_instances(
    source: Calendar | Iterable[Event],
    start: dt.date | dt.datetime,
    end: dt.date | dt.datetime,
    *,
    floating_tz: dt.tzinfo | None = None,
    max_instances: int = 1000,
) -> ExpansionResult:
    """Expand the recurring VEVENTs of ``source`` into concrete instances.

    Parameters:
        source: a :class:`~icalendar.Calendar` or an iterable of
            :class:`~icalendar.Event` components.
        start: inclusive window start (date, or naive/aware datetime).
        end: exclusive window end.
        floating_tz: timezone used to interpret floating (timezone-less)
            event times and naive window bounds; required when any expanded
            event is floating.
        max_instances: cap on rule-generated candidates per UID.  Reaching
            the cap sets :attr:`ExpansionResult.truncated` instead of
            silently returning an incomplete result.

    Returns:
        An :class:`ExpansionResult` with instances sorted by start time.
        Cancelled instances (STATUS:CANCELLED overrides) are excluded.
    """
    if max_instances < 1:
        raise ValueError("max_instances must be at least 1")

    if hasattr(source, "walk"):
        events = list(source.walk("VEVENT"))
    else:
        events = list(source)

    groups: dict[str, dict] = {}
    for event in events:
        uid = str(event.get("UID", ""))
        group = groups.setdefault(
            uid, {"masters": [], "single": {}, "future": []}
        )
        if event.get("RECURRENCE-ID") is None:
            group["masters"].append(event)
        elif _range_of(event) == _THISANDFUTURE:
            group["future"].append(event)
        else:
            rid = _recurrence_id_of(event)
            group["single"].setdefault(rid, []).append(event)

    all_instances: list[EventInstance] = []
    truncated = False
    for uid, group in groups.items():
        master = (
            _resolve_version(group["masters"], uid, "master component")
            if group["masters"]
            else None
        )
        single = {
            rid: _resolve_version(versions, uid, f"override {rid!r}")
            for rid, versions in group["single"].items()
        }
        future = _build_future_overrides(group["future"])

        sample = (
            master
            or next(iter(single.values()), None)
            or (group["future"][0] if group["future"] else None)
        )
        if sample is None or sample.get("DTSTART") is None:
            continue
        kind = _Kind(_kind_of(sample.start), floating_tz)
        window_start, window_end = kind.normalize_window(start, end)
        if not window_start < window_end:
            raise RecurrenceError(
                f"empty query window: {window_start!r}..{window_end!r}"
            )
        instances, group_truncated = _expand_group(
            uid,
            master,
            single,
            future,
            kind,
            window_start,
            window_end,
            max_instances,
        )
        all_instances.extend(instances)
        truncated = truncated or group_truncated

    all_instances.sort(
        key=lambda i: (_chronological_key(i.start), i.uid, str(i.recurrence_id))
    )
    return ExpansionResult(instances=all_instances, truncated=truncated)
