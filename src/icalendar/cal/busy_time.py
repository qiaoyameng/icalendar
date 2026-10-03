"""Compute the actual free/busy timeline from availability and scheduled time.

This implements the combination algorithm of :rfc:`7953` and overlays it with
already scheduled time and explicitly published free/busy information.

The components :class:`~icalendar.cal.availability.Availability`
(``VAVAILABILITY``) and :class:`~icalendar.cal.available.Available`
(``AVAILABLE``) can be read and written, but by themselves they only describe
*rules*. This module turns those rules, together with explicit
:class:`~icalendar.cal.free_busy.FreeBusy` (``VFREEBUSY``) information and the
time ranges of already scheduled events, into a concrete, finite timeline.

The result uses **half-open** UTC intervals ``[start, end)``:

* Time covered by an ``AVAILABLE`` subcomponent is :attr:`BusyState.FREE`.
* Time inside a ``VAVAILABILITY`` envelope but not covered by any
  ``AVAILABLE`` subcomponent takes the enclosing component's ``BUSYTYPE``
  (the default is ``BUSY-UNAVAILABLE``).
* Time outside every ``VAVAILABILITY`` envelope is
  :attr:`BusyState.UNKNOWN`: no availability information exists for it, which
  must not be confused with the busy default *inside* an envelope.

Overlapping ``VAVAILABILITY`` components are combined according to
:rfc:`7953#section-7`. Their ``PRIORITY`` determines the order: ``PRIORITY``
value 0 (or an absent property) is the *lowest* precedence, then values
``1`` through ``9`` are applied in ascending order. Each priority level can
contain several components; at the same priority their ``AVAILABLE`` ranges
are merged (union) and their busy types are merged by precedence
(``BUSY-UNAVAILABLE`` wins over ``BUSY``, which wins over
``BUSY-TENTATIVE``).

Recurring ``AVAILABLE``/event ranges are *not* expanded here. A recurrence
expander is passed in as a callback, so this module never re-implements an
RRULE engine. See :class:`RecurrenceExpander` and
:func:`default_recurrence_expander`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum, auto
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from icalendar.enums import BUSYTYPE, FBTYPE, STATUS, TRANSP
from icalendar.error import IncompleteComponent
from icalendar.timezone import tzp
from icalendar.tools import is_date

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from icalendar.cal import Availability, Available, Event, FreeBusy

UTC = timezone.utc


class BusyState(Enum):
    """The busy state of a point on the computed timeline.

    The states correspond to the free/busy types from :rfc:`5545`, plus
    :attr:`UNKNOWN` for time for which no availability rule exists.
    """

    #: The time is free for scheduling (``AVAILABLE`` or ``FBTYPE=FREE``).
    FREE = auto()
    #: The time is busy because one or more events were scheduled (``BUSY``).
    BUSY = auto()
    #: The time is busy and cannot be scheduled (``BUSY-UNAVAILABLE``).
    BUSY_UNAVAILABLE = auto()
    #: The time is busy because one or more events were tentative
    #: (``BUSY-TENTATIVE``).
    BUSY_TENTATIVE = auto()
    #: No availability information covers the time.
    UNKNOWN = auto()

    @property
    def is_free(self) -> bool:
        """Whether the time is available for scheduling."""
        return self is BusyState.FREE

    @property
    def is_busy(self) -> bool:
        """Whether the time is unavailable for any reason."""
        return self in _BUSY_STATES

    @property
    def fbtype(self) -> str | None:
        """The :rfc:`5545` free/busy type name, or ``None`` for :attr:`UNKNOWN`."""
        return _STATE_TO_FBTYPE.get(self)


_BUSY_STATES = frozenset(
    {BusyState.BUSY, BusyState.BUSY_UNAVAILABLE, BusyState.BUSY_TENTATIVE}
)

_STATE_TO_FBTYPE: dict[BusyState, str] = {
    BusyState.FREE: FBTYPE.FREE.value,
    BusyState.BUSY: FBTYPE.BUSY.value,
    BusyState.BUSY_UNAVAILABLE: FBTYPE.BUSY_UNAVAILABLE.value,
    BusyState.BUSY_TENTATIVE: FBTYPE.BUSY_TENTATIVE.value,
}


class SourceKind(Enum):
    """Where a piece of free/busy information comes from."""

    #: The busy default inside a ``VAVAILABILITY`` envelope (its ``BUSYTYPE``).
    AVAILABILITY_BUSY = auto()
    #: A free range coming from an ``AVAILABLE`` subcomponent.
    AVAILABLE = auto()
    #: A busy time range coming from a scheduled ``VEVENT``.
    EVENT = auto()
    #: An explicitly published period from a ``VFREEBUSY`` component.
    FREEBUSY = auto()


@dataclass(frozen=True)
class BusySource:
    """Provenance explaining why a piece of the timeline has its state.

    Each source points back at the originating iCalendar component (by name,
    UID and summary where available) and, for ``AVAILABLE`` and ``VEVENT``
    ranges, at the concrete occurrence it came from.

    Attributes:
        kind: What kind of information the source contributes.
        component: The iCalendar component name, e.g. ``"VAVAILABILITY"``,
            ``"AVAILABLE"``, ``"VEVENT"`` or ``"VFREEBUSY"``.
        uid: The ``UID`` of the originating component, if present.
        summary: The ``SUMMARY`` of the originating component, if present.
        busy_type: The free/busy type of the contribution. For a busy
            ``VAVAILABILITY`` default this is its ``BUSYTYPE``; for an event it
            is the derived busy type; for a ``FREEBUSY`` period it is its
            ``FBTYPE``. It is ``None`` for free ``AVAILABLE`` ranges.
        occurrence: The UTC half-open occurrence interval the source applies
            to, as a ``(start, end)`` tuple, when a concrete range exists.
    """

    kind: SourceKind
    component: str
    uid: str | None = None
    summary: str | None = None
    busy_type: str | None = None
    occurrence: tuple[datetime, datetime] | None = None

    def describe(self) -> str:
        """Return a short human-readable explanation of the source."""
        label = self.summary or self.uid or self.component
        if self.kind is SourceKind.AVAILABILITY_BUSY:
            return f"{label} is {self.busy_type} outside AVAILABLE ranges"
        if self.kind is SourceKind.AVAILABLE:
            return f"{label} marks the time AVAILABLE"
        if self.kind is SourceKind.EVENT:
            return f"{label} ({self.busy_type}) occupies the time"
        return f"explicit FREEBUSY ({self.busy_type}) from {label}"


@dataclass(frozen=True)
class BusyPeriod:
    """A half-open UTC interval together with its state and provenance.

    Attributes:
        start: The inclusive UTC start of the period.
        end: The exclusive UTC end of the period.
        state: The :class:`BusyState` of the whole period.
        sources: The sources that determine the state. For a busy period this
            explains *why* the time is unavailable; for a free period it lists
            the ``AVAILABLE``/``FREEBUSY`` ranges that make it free; for an
            unknown period it is empty.
    """

    start: datetime
    end: datetime
    state: BusyState
    sources: tuple[BusySource, ...] = ()

    @property
    def duration(self) -> timedelta:
        """The length of the period."""
        return self.end - self.start

    @property
    def is_free(self) -> bool:
        """Whether the period is free for scheduling."""
        return self.state.is_free

    @property
    def is_busy(self) -> bool:
        """Whether the period is busy."""
        return self.state.is_busy


@dataclass(frozen=True)
class ExpandedEvent:
    """A scheduled event together with an already expanded UTC occurrence.

    This is the input shape for events. The caller is responsible for
    expanding recurrence (RRULE/RDATE/EXDATE and overrides); the timeline
    calculation only consumes the concrete half-open intervals.

    Attributes:
        event: The originating ``VEVENT`` component.
        start: The inclusive UTC start of the occurrence.
        end: The exclusive UTC end of the occurrence.
    """

    event: Event
    start: datetime
    end: datetime


@runtime_checkable
class RecurrenceExpander(Protocol):
    """Expand a recurring component into concrete occurrences.

    The timeline calculation never expands recurrence itself. Instead it calls
    a callable with the component and the finite UTC window and expects the
    concrete, already clipped occurrences back. Implementations can use any
    RRULE engine (the library ships :func:`default_recurrence_expander`
    backed by ``dateutil``).

    For a component without recurrence, the single ``[start, end)`` interval
    is returned, clipped to the window.
    """

    def __call__(
        self,
        component: Event | Available,
        window_start: datetime,
        window_end: datetime,
    ) -> Sequence[tuple[datetime, datetime]]:
        """Return the UTC half-open occurrences of ``component`` in the window."""


_BUSY_STRENGTH = {
    BusyState.BUSY_TENTATIVE: 1,
    BusyState.BUSY: 2,
    BusyState.BUSY_UNAVAILABLE: 3,
}

_BUSYTYPE_TO_STATE = {
    BUSYTYPE.BUSY.value: BusyState.BUSY,
    BUSYTYPE.BUSY_UNAVAILABLE.value: BusyState.BUSY_UNAVAILABLE,
    BUSYTYPE.BUSY_TENTATIVE.value: BusyState.BUSY_TENTATIVE,
}

_FBTYPE_TO_STATE = {
    FBTYPE.FREE.value: BusyState.FREE,
    FBTYPE.BUSY.value: BusyState.BUSY,
    FBTYPE.BUSY_UNAVAILABLE.value: BusyState.BUSY_UNAVAILABLE,
    FBTYPE.BUSY_TENTATIVE.value: BusyState.BUSY_TENTATIVE,
}


@dataclass(frozen=True)
class _Fragment:
    """A resolved half-open interval contributing one state with provenance."""

    start: datetime
    end: datetime
    state: BusyState
    sources: tuple[BusySource, ...] = ()


def _to_utc(value: date | datetime | None) -> datetime | None:
    """Normalize a date or datetime to a timezone-aware UTC datetime.

    Naive datetimes are interpreted as UTC. A pure ``date`` becomes midnight
    UTC, matching how all-day event time is handled for free/busy searches.
    """
    if value is None:
        return None
    if is_date(value) and not isinstance(value, datetime):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    return tzp.localize_utc(value)


def _local_value(component, name: str) -> date | None:
    """Return the raw ``DTSTART``/``DTEND`` value with its stored timezone.

    Recurrence must iterate in the rule's local wall-clock time so that a fixed
    local range keeps its duration across DST transitions. The RFC 7953
    ``.start``/``.end`` accessors normalize to UTC, so for recurrence we read
    the underlying value, which preserves the ``TZID`` when the component was
    parsed or built with a local timezone. A pure ``date`` (all-day event) is
    returned unchanged.
    """
    raw = component.get(name)
    value = getattr(raw, "dt", raw)
    if value is None or not isinstance(value, date):
        return None
    return value


def _clip(
    start: datetime | None,
    end: datetime | None,
    window_start: datetime,
    window_end: datetime,
) -> tuple[datetime, datetime] | None:
    """Intersect a possibly unbounded UTC interval with the finite window.

    Returns the half-open clipped interval or ``None`` when it is empty.
    """
    lo = window_start if start is None else max(start, window_start)
    hi = window_end if end is None else min(end, window_end)
    if lo < hi:
        return lo, hi
    return None


def _as_list(value: object) -> list:
    """Normalize a possibly-singleton property value into a list."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _uid(component) -> str | None:
    """Return a component's UID as a plain string, if present."""
    uid = component.get("UID")
    return None if uid is None else str(uid)


def _summary(component) -> str | None:
    """Return a component's SUMMARY as a plain string, if present."""
    summary = component.get("SUMMARY")
    return None if summary is None else str(summary)


def _expand_date_occurrences(
    component: Event | Available,
    local_start: date,
    window_start: datetime,
    window_end: datetime,
) -> list[tuple[datetime, datetime]]:
    """Expand an all-day (DATE-valued) component into whole-day UTC intervals.

    An all-day event starts at midnight UTC and lasts one day, or until its
    DATE-valued ``DTEND`` / day-valued ``DURATION``. A single ``RRULE`` plus
    ``RDATE``/``EXDATE`` is honored.
    """
    import dateutil.rrule

    end_value = component.get("DTEND")
    if end_value is not None and getattr(end_value, "dt", end_value) is not None:
        end_date = end_value.dt
        day_duration = end_date - local_start
    else:
        duration = component.get("DURATION")
        if duration is not None:
            day_duration = getattr(duration, "dt", duration)
        else:
            day_duration = timedelta(days=1)

    excluded = {
        value.dt
        for exdate in _as_list(component.exdates)
        for value in getattr(exdate, "dts", [])
    }

    days: list[date] = []
    if component.rrules:
        for recur in component.rrules:
            rule = dateutil.rrule.rrulestr(
                recur.to_ical().decode("utf-8"),
                dtstart=datetime(local_start.year, local_start.month, local_start.day),
            )
            tzp.fix_rrule_until(rule, recur)
            days.extend(each.date() for each in rule)
    else:
        days.append(local_start)
    for value in _as_list(component.rdates):
        for entry in getattr(value, "dts", []):
            candidate = entry.dt
            days.append(candidate[0].date() if isinstance(candidate, tuple) else candidate)

    occurrences: list[tuple[datetime, datetime]] = []
    for day in dict.fromkeys(days):
        if day in excluded:
            continue
        start_dt = datetime(day.year, day.month, day.day, tzinfo=UTC)
        clipped = _clip(
            start_dt, start_dt + day_duration, window_start, window_end
        )
        if clipped is not None:
            occurrences.append(clipped)
    return sorted(set(occurrences))


def default_recurrence_expander(
    component: Event | Available,
    window_start: datetime,
    window_end: datetime,
) -> list[tuple[datetime, datetime]]:
    """Expand occurrences of a component using the installed ``dateutil``.

    This is a convenience expander for callers that do not already have
    expanded events. It handles a single ``RRULE`` plus ``RDATE``/``EXDATE``
    within the finite window. It deliberately does *not* implement the full
    iCalendar recurrence semantics (such as ``RECURRENCE-ID`` overrides):
    those remain the caller's responsibility and can be supplied through a
    custom :class:`RecurrenceExpander`.

    Recurrence is generated in the component's own local timezone so that a
    fixed local-time range keeps its wall-clock duration across DST
    transitions; each occurrence is then converted to UTC individually.

    Parameters:
        component: An ``AVAILABLE`` subcomponent or a ``VEVENT``.
        window_start: Inclusive UTC window start.
        window_end: Exclusive UTC window end.

    Returns:
        The clipped UTC half-open occurrences overlapping the window.
    """
    import dateutil.rrule

    from icalendar.tools import normalize_pytz

    local_start = _local_value(component, "DTSTART")
    if local_start is None:  # pragma: no cover - guarded by the component model
        raise IncompleteComponent("Cannot expand a component without DTSTART")

    # All-day (DATE value) components occupy whole days and have no timezone,
    # so there is no DST wall-clock handling: iterate by date and clip in UTC.
    if is_date(local_start) and not isinstance(local_start, datetime):
        return _expand_date_occurrences(
            component, local_start, window_start, window_end
        )

    local_end = _local_value(component, "DTEND")
    if local_end is not None and isinstance(local_end, datetime) and local_end.tzinfo is None:
        # A naive end belongs to the same wall clock as an aware start.
        local_end = local_end.replace(tzinfo=local_start.tzinfo)

    end_day_offset = (
        local_end.date() - local_start.date() if local_end is not None else timedelta()
    )

    def _occurrence_end(occ_start: datetime) -> datetime:
        """Build the occurrence end keeping the fixed end wall-clock time.

        Across a DST gap/fold-back, adding a duration to the start can land on
        the wrong side of the transition. Combining the occurrence date (plus
        the original DTEND/DTSTART day offset) with the end's time-of-day in
        the occurrence's timezone keeps the intended local end time, as
        required for DATE-TIME ranges that may span several days.
        """
        if local_end is None:
            return occ_start
        naive_end = datetime.combine(
            occ_start.date() + end_day_offset,
            local_end.timetz().replace(tzinfo=None),
        )
        tzinfo = occ_start.tzinfo
        if tzinfo is None:
            return naive_end
        # pytz timezones must be attached with localize(); zoneinfo can be set
        # directly. normalize() resolves fold/gap offsets for both providers.
        if hasattr(tzinfo, "localize"):
            import pytz

            try:
                localized = tzinfo.localize(naive_end, is_dst=None)
            except (pytz.NonExistentTimeError, pytz.AmbiguousTimeError):
                # The end wall time falls in a DST gap or repeated hour; use
                # the standard-time side and let normalize() resolve the offset.
                localized = tzinfo.localize(naive_end, is_dst=False)
            return tzinfo.normalize(localized)
        return naive_end.replace(tzinfo=tzinfo)

    def _excluded(value: datetime) -> bool:
        utc_value = _to_utc(value)
        for exdate in _as_list(component.exdates):
            for entry in getattr(exdate, "dts", []):
                if _to_utc(entry.dt) == utc_value:
                    return True
        return False

    local_starts: list[datetime] = []
    if component.rrules:
        for recur in component.rrules:
            rule = dateutil.rrule.rrulestr(
                recur.to_ical().decode("utf-8"), dtstart=local_start
            )
            tzp.fix_rrule_until(rule, recur)
            local_starts.extend(rule)
    else:
        local_starts.append(local_start)
    for value in _as_list(component.rdates):
        for entry in getattr(value, "dts", []):
            candidate = entry.dt
            if isinstance(candidate, tuple):  # a PERIOD RDATE
                candidate = candidate[0]
            local_starts.append(candidate)

    occurrences: list[tuple[datetime, datetime]] = []
    for occ_start in dict.fromkeys(local_starts):
        occ_start = normalize_pytz(occ_start)
        if _excluded(occ_start):
            continue
        occ_end = normalize_pytz(_occurrence_end(occ_start))
        clipped = _clip(
            _to_utc(occ_start), _to_utc(occ_end), window_start, window_end
        )
        if clipped is not None:
            occurrences.append(clipped)
    return sorted(set(occurrences))


def _boundaries(fragments: Iterable[_Fragment]) -> list[datetime]:
    """Collect the sorted, unique boundaries of fragments."""
    points: set[datetime] = set()
    for fragment in fragments:
        points.add(fragment.start)
        points.add(fragment.end)
    return sorted(points)


def _covering(
    fragments: Sequence[_Fragment], start: datetime, end: datetime
) -> list[_Fragment]:
    """Return all fragments covering the half-open cell ``[start, end)``."""
    return [f for f in fragments if f.start <= start and end <= f.end]


def _resolve_level(
    envelopes: Sequence[_Fragment], free_ranges: Sequence[_Fragment]
) -> list[_Fragment]:
    """Resolve one merged availability priority level into disjoint fragments.

    Within the merged level, ``AVAILABLE`` ranges win over the envelope busy
    defaults. The busy fragments carry every envelope that defaults the cell
    busy, so the provenance remains complete even when components overlap.
    """
    boundaries = sorted(
        {*_boundaries(envelopes), *_boundaries(free_ranges)}
    )
    resolved: list[_Fragment] = []
    for start, end in zip(boundaries, boundaries[1:]):
        free = _covering(list(free_ranges), start, end)
        if free:
            sources = tuple(
                source for fragment in free for source in fragment.sources
            )
            resolved.append(
                _Fragment(start, end, BusyState.FREE, tuple(dict.fromkeys(sources)))
            )
            continue
        covering_envelopes = _covering(list(envelopes), start, end)
        if covering_envelopes:
            state = max(
                (fragment.state for fragment in covering_envelopes),
                key=lambda state: _BUSY_STRENGTH[state],
            )
            sources = tuple(
                source
                for fragment in covering_envelopes
                for source in fragment.sources
                if fragment.state is state
            )
            resolved.append(
                _Fragment(start, end, state, tuple(dict.fromkeys(sources)))
            )
    return resolved


def _paint_levels(
    levels: Sequence[list[_Fragment]],
    window_start: datetime,
    window_end: datetime,
) -> list[_Fragment]:
    """Apply availability levels in ascending precedence (replace semantics).

    Levels are given from lowest to highest precedence. The highest-priority
    level that covers a cell wins entirely; cells no level covers are omitted
    (they remain unknown).
    """
    boundaries: set[datetime] = {window_start, window_end}
    for level in levels:
        boundaries.update(_boundaries(level))
    ordered = sorted(boundaries)
    painted: list[_Fragment] = []
    for start, end in zip(ordered, ordered[1:]):
        for level in reversed(levels):
            covering = _covering(level, start, end)
            if covering:
                state = covering[0].state
                sources = tuple(
                    source
                    for fragment in covering
                    if fragment.state is state
                    for source in fragment.sources
                )
                painted.append(
                    _Fragment(start, end, state, tuple(dict.fromkeys(sources)))
                )
                break
    return painted


def _overlay(
    base: Sequence[_Fragment],
    overlay_fragments: Sequence[_Fragment],
    *,
    free_replaces: bool,
    window_start: datetime,
    window_end: datetime,
) -> list[_Fragment]:
    """Overlay event/freebusy fragments on the availability result.

    Busy fragments win by strength: a busy overlay replaces a free or weaker
    busy cell, but a weaker busy overlay keeps a stronger busy base while its
    provenance is still recorded. When ``free_replaces`` is ``True`` a free
    overlay always wins (used for explicit ``FBTYPE=FREE`` periods); otherwise
    a free overlay only fills unknown time.
    """
    boundaries = sorted(
        {
            window_start,
            window_end,
            *_boundaries(base),
            *_boundaries(overlay_fragments),
        }
    )
    result: list[_Fragment] = []
    for start, end in zip(boundaries, boundaries[1:]):
        base_covering = _covering(list(base), start, end)
        over_covering = _covering(list(overlay_fragments), start, end)
        state: BusyState
        sources: tuple[BusySource, ...]

        base_state = base_covering[0].state if base_covering else BusyState.UNKNOWN
        base_sources = tuple(s for f in base_covering for s in f.sources)
        busy_overlays = [f for f in over_covering if f.state.is_busy]
        free_overlays = [f for f in over_covering if f.state is BusyState.FREE]
        free_sources = tuple(s for f in free_overlays for s in f.sources)
        busy_sources = tuple(s for f in busy_overlays for s in f.sources)
        strongest_overlay = (
            max(busy_overlays, key=lambda f: _BUSY_STRENGTH[f.state]).state
            if busy_overlays
            else None
        )

        if strongest_overlay is not None and (
            not base_state.is_busy
            or _BUSY_STRENGTH[strongest_overlay] >= _BUSY_STRENGTH[base_state]
        ):
            state = strongest_overlay
            sources = busy_sources
            if base_state.is_busy and _BUSY_STRENGTH[base_state] == _BUSY_STRENGTH[state]:
                sources = tuple(dict.fromkeys((*busy_sources, *base_sources)))
        elif strongest_overlay is not None and base_state.is_busy:
            # A weaker busy overlay does not change the state but still explains
            # competing load on the time.
            state = base_state
            sources = tuple(dict.fromkeys((*base_sources, *busy_sources)))
        elif free_overlays and (free_replaces or not base_state.is_busy):
            state = BusyState.FREE
            sources = tuple(dict.fromkeys((*free_sources, *base_sources)))
        elif base_covering:
            state = base_state
            sources = base_sources
        else:
            continue
        result.append(_Fragment(start, end, state, sources))
    return result


def _merge_adjacent(fragments: Iterable[_Fragment]) -> list[BusyPeriod]:
    """Merge adjacent fragments with the same state, combining provenance."""
    periods: list[BusyPeriod] = []
    for fragment in sorted(fragments, key=lambda f: f.start):
        if periods and periods[-1].end == fragment.start and periods[-1].state is fragment.state:
            sources = tuple(
                dict.fromkeys((*periods[-1].sources, *fragment.sources))
            )
            previous = periods[-1]
            periods[-1] = BusyPeriod(
                previous.start, fragment.end, fragment.state, sources
            )
        else:
            periods.append(
                BusyPeriod(fragment.start, fragment.end, fragment.state, fragment.sources)
            )
    return periods


def _availability_levels(
    availabilities: Iterable[Availability],
    window_start: datetime,
    window_end: datetime,
    expand: RecurrenceExpander,
) -> tuple[list[int], dict[int, list[_Fragment]]]:
    """Group and resolve ``VAVAILABILITY`` components by priority.

    Returns the priority keys in ascending precedence order together with the
    resolved disjoint fragments of each level.
    """
    grouped: dict[int, list[Availability]] = {}
    for component in availabilities:
        priority = int(component.priority or 0)
        grouped.setdefault(priority, []).append(component)

    def priority_order(value: int) -> tuple[bool, int]:
        # Application order is lowest precedence first: PRIORITY 0 (or missing)
        # is the lowest precedence, then priorities 1..9 ascend. Higher levels
        # override lower ones in _paint_levels.
        return (value != 0, value)

    levels: dict[int, list[_Fragment]] = {}
    for priority in sorted(grouped, key=priority_order):
        envelopes: list[_Fragment] = []
        free_ranges: list[_Fragment] = []
        for component in grouped[priority]:
            envelope_start = _to_utc(component.start)
            envelope_end = _to_utc(component.end)
            clipped_envelope = _clip(
                envelope_start, envelope_end, window_start, window_end
            )
            busy_state = _BUSYTYPE_TO_STATE.get(
                str(component.busy_type), BusyState.BUSY_UNAVAILABLE
            )
            envelope_source = BusySource(
                kind=SourceKind.AVAILABILITY_BUSY,
                component="VAVAILABILITY",
                uid=_uid(component),
                summary=_summary(component),
                busy_type=busy_state.fbtype,
            )
            if clipped_envelope is not None:
                envelopes.append(
                    _Fragment(
                        clipped_envelope[0],
                        clipped_envelope[1],
                        busy_state,
                        (envelope_source,),
                    )
                )
            for available in component.available:
                for occ_start, occ_end in expand(
                    available, window_start, window_end
                ):
                    # AVAILABLE time never leaks outside its envelope.
                    bounded = _clip(
                        occ_start,
                        occ_end,
                        envelope_start or window_start,
                        envelope_end if envelope_end is not None else window_end,
                    )
                    if bounded is None:
                        continue
                    source = BusySource(
                        kind=SourceKind.AVAILABLE,
                        component="AVAILABLE",
                        uid=_uid(available),
                        summary=_summary(available),
                        occurrence=bounded,
                    )
                    free_ranges.append(
                        _Fragment(bounded[0], bounded[1], BusyState.FREE, (source,))
                    )
        levels[priority] = _resolve_level(envelopes, free_ranges)
    return sorted(grouped, key=priority_order), levels


def _event_state(event: Event) -> BusyState | None:
    """Map an event to a busy state, or ``None`` if it must be ignored.

    ``STATUS:CANCELLED`` events and ``TRANSP:TRANSPARENT`` events do not
    consume time. ``STATUS:TENTATIVE`` maps to ``BUSY-TENTATIVE``; everything
    else (including confirmed and unspecified status) maps to ``BUSY``.
    """
    if str(event.status) == STATUS.CANCELLED.value:
        return None
    if event.transparency is TRANSP.TRANSPARENT:
        return None
    if str(event.status) == STATUS.TENTATIVE.value:
        return BusyState.BUSY_TENTATIVE
    return BusyState.BUSY


def _normalise_window(
    start: date | datetime, end: date | datetime
) -> tuple[datetime, datetime]:
    """Convert a finite window to aware UTC datetimes and validate it."""
    window_start = _to_utc(start)
    window_end = _to_utc(end)
    if window_start is None or window_end is None:
        raise ValueError("The busy timeline window needs both a start and an end")
    if window_end <= window_start:
        raise ValueError("The busy timeline window end must be after its start")
    return window_start, window_end


class BusyTimeline:
    """The resolved free/busy state over a finite UTC window.

    The periods form a complete, non-overlapping cover of the window using
    half-open intervals. Adjacent periods with the same state are merged.

    Attributes:
        start: The inclusive UTC start of the window.
        end: The exclusive UTC end of the window.
        periods: The merged :class:`BusyPeriod` objects in chronological order.
    """

    def __init__(
        self,
        start: datetime,
        end: datetime,
        periods: Iterable[BusyPeriod],
    ) -> None:
        self.start = start
        self.end = end
        self.periods: list[BusyPeriod] = list(periods)

    def __iter__(self):
        """Iterate over the periods."""
        return iter(self.periods)

    def __len__(self) -> int:
        """The number of distinct periods."""
        return len(self.periods)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, BusyTimeline):
            return NotImplemented
        return (
            self.start == other.start
            and self.end == other.end
            and self.periods == other.periods
        )

    @property
    def free(self) -> list[BusyPeriod]:
        """All free periods (``AVAILABLE`` or ``FBTYPE=FREE``)."""
        return [period for period in self.periods if period.is_free]

    @property
    def busy(self) -> list[BusyPeriod]:
        """All busy periods of every busy type."""
        return [period for period in self.periods if period.is_busy]

    @property
    def unknown(self) -> list[BusyPeriod]:
        """All periods without availability information."""
        return [period for period in self.periods if period.state is BusyState.UNKNOWN]

    def state_at(self, moment: datetime) -> BusyState:
        """Return the busy state at an instant.

        The instant is converted to UTC. The window end is exclusive.
        """
        moment = _to_utc(moment)
        for period in self.periods:
            if period.start <= moment < period.end:
                return period.state
        if moment == self.end:
            raise KeyError("The window end is exclusive")
        raise KeyError("The instant is outside the busy timeline window")

    def as_free_busy(self, organizer=None) -> FreeBusy:
        """Build a :class:`~icalendar.cal.free_busy.FreeBusy` from the busy periods.

        Unknown periods are not emitted (there is no free/busy type for them).
        Adjacent periods sharing a free/busy type are emitted separately unless
        already merged.
        """
        from icalendar.cal.free_busy import FreeBusy
        from icalendar.prop import vPeriod

        free_busy = FreeBusy.new(start=self.start, end=self.end, organizer=organizer)
        for period in self.periods:
            fbtype = period.state.fbtype
            if fbtype is None:
                continue
            value = vPeriod(
                (period.start, period.end), params={"FBTYPE": fbtype}
            )
            free_busy.add("FREEBUSY", value)
        return free_busy


def _event_fragments(
    events: Iterable[Event | ExpandedEvent],
    window_start: datetime,
    window_end: datetime,
    expand: RecurrenceExpander,
) -> list[_Fragment]:
    """Turn scheduled events into busy fragments.

    Both already expanded :class:`ExpandedEvent` occurrences and raw
    ``VEVENT`` components are accepted. Cancelled and transparent events are
    skipped.
    """
    fragments: list[_Fragment] = []
    for item in events:
        if isinstance(item, ExpandedEvent):
            event = item.event
            occurrences = [(_to_utc(item.start), _to_utc(item.end))]
        else:
            event = item
            occurrences = list(expand(item, window_start, window_end))
        state = _event_state(event)
        if state is None:
            continue
        for occ_start, occ_end in occurrences:
            clipped = _clip(occ_start, occ_end, window_start, window_end)
            if clipped is None:
                continue
            source = BusySource(
                kind=SourceKind.EVENT,
                component="VEVENT",
                uid=_uid(event),
                summary=_summary(event),
                busy_type=state.fbtype,
                occurrence=clipped,
            )
            fragments.append(
                _Fragment(clipped[0], clipped[1], state, (source,))
            )
    return fragments


def _free_busy_fragments(
    free_busy_components: Iterable[FreeBusy],
    window_start: datetime,
    window_end: datetime,
) -> list[_Fragment]:
    """Turn explicit ``VFREEBUSY`` periods into fragments.

    An unknown or unrecognised ``FBTYPE`` is treated as ``BUSY`` as required by
    :rfc:`5545`. ``FBTYPE=FREE`` periods are kept separate so they are handled
    with free-replace semantics in :func:`_overlay`.
    """
    fragments: list[_Fragment] = []
    for component in free_busy_components:
        periods = component.get("FREEBUSY")
        if periods is None:
            continue
        for period in _as_list(periods):
            fbtype = str(period.params.get("FBTYPE", FBTYPE.BUSY.value)).upper()
            state = _FBTYPE_TO_STATE.get(fbtype, BusyState.BUSY)
            clipped = _clip(
                _to_utc(period.start), _to_utc(period.end),
                window_start, window_end,
            )
            if clipped is None:
                continue
            source = BusySource(
                kind=SourceKind.FREEBUSY,
                component="VFREEBUSY",
                uid=_uid(component),
                summary=_summary(component),
                busy_type=state.fbtype,
                occurrence=clipped,
            )
            fragments.append(_Fragment(clipped[0], clipped[1], state, (source,)))
    return fragments


def busy_timeline(
    start: date | datetime,
    end: date | datetime,
    availabilities: Iterable[Availability] = (),
    events: Iterable[Event | ExpandedEvent] = (),
    free_busy: Iterable[FreeBusy] = (),
    expand: RecurrenceExpander = default_recurrence_expander,
) -> BusyTimeline:
    """Synthesize the actual free/busy timeline over a finite UTC window.

    The inputs are combined according to :rfc:`7953`:

    1. ``VAVAILABILITY`` components are merged by ``PRIORITY`` and ``BUSYTYPE``.
       ``AVAILABLE`` subcomponents carve free time out of the enclosing busy
       envelopes, and never extend beyond them.
    2. Scheduled events are overlaid. Cancelled and transparent events are
       ignored; tentative events mark ``BUSY-TENTATIVE``.
    3. Explicit ``VFREEBUSY`` periods are applied last. Busy periods win by
       strength, and ``FBTYPE=FREE`` explicitly frees time.
    4. Everything is converted to UTC, clipped to the window, cut at every
       boundary, and adjacent periods of the same state are merged.

    Parameters:
        start: The inclusive start of the finite window (aware, naive as UTC,
            or a pure ``date`` at midnight UTC).
        end: The exclusive end of the finite window.
        availabilities: The ``VAVAILABILITY`` components to combine.
        events: Scheduled events, either raw ``VEVENT`` components (expanded
            through ``expand``) or :class:`ExpandedEvent` occurrences.
        free_busy: Explicit ``VFREEBUSY`` components.
        expand: A :class:`RecurrenceExpander` callback used to turn recurring
            ``AVAILABLE``/``VEVENT`` components into concrete occurrences. It
            defaults to :func:`default_recurrence_expander`.

    Returns:
        A :class:`BusyTimeline` covering the whole window, including unknown
        time and the provenance of every period.
    """
    window_start, window_end = _normalise_window(start, end)

    order, levels = _availability_levels(
        availabilities, window_start, window_end, expand
    )
    painted = _paint_levels(
        [levels[key] for key in order], window_start, window_end
    )

    with_events = _overlay(
        painted,
        _event_fragments(events, window_start, window_end, expand),
        free_replaces=False,
        window_start=window_start,
        window_end=window_end,
    )
    combined = _overlay(
        with_events,
        _free_busy_fragments(free_busy, window_start, window_end),
        free_replaces=True,
        window_start=window_start,
        window_end=window_end,
    )

    # Cover the whole window so time without any information is UNKNOWN.
    boundaries = sorted({window_start, window_end, *_boundaries(combined)})
    complete: list[_Fragment] = []
    for cell_start, cell_end in zip(boundaries, boundaries[1:]):
        covering = _covering(combined, cell_start, cell_end)
        if covering:
            sources = tuple(s for f in covering for s in f.sources)
            complete.append(
                _Fragment(
                    cell_start, cell_end, covering[0].state,
                    tuple(dict.fromkeys(sources)),
                )
            )
        else:
            complete.append(_Fragment(cell_start, cell_end, BusyState.UNKNOWN, ()))

    periods = _merge_adjacent(complete)
    return BusyTimeline(window_start, window_end, periods)


__all__ = [
    "BusyPeriod",
    "BusySource",
    "BusyState",
    "BusyTimeline",
    "ExpandedEvent",
    "RecurrenceExpander",
    "SourceKind",
    "busy_timeline",
    "default_recurrence_expander",
]
