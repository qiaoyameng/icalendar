"""Compute a free/busy timeline from availability and busy information.

This combines :rfc:`7953` "VAVAILABILITY" components, explicit
:rfc:`5545` "VFREEBUSY" periods and already expanded "VEVENT"
occurrences into a single timeline over a finite window.

Recurrence expansion is deliberately not done here:
the caller provides expanded "VEVENT" occurrences and an ``expand``
callback for recurring "AVAILABLE" subcomponents.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import TYPE_CHECKING

from icalendar.enums import FBTYPE, STATUS, TRANSP

if TYPE_CHECKING:
    from icalendar.cal import Availability, Available, Event, FreeBusy

__all__ = ["BusySource", "ExpandAvailable", "TimelineSegment", "availability_timeline"]

ExpandAvailable = Callable[
    ["Available", datetime, datetime], Iterable[tuple[datetime, datetime]]
]
"""Type of the ``expand`` callback of :func:`availability_timeline`.

The callback receives an "AVAILABLE" component and the UTC window as
``(component, window_start, window_end)`` and yields ``(start, end)``
pairs for each occurrence of the component.
"""

_SEVERITY = {
    FBTYPE.FREE: 0,
    FBTYPE.BUSY_TENTATIVE: 1,
    FBTYPE.BUSY: 2,
    FBTYPE.BUSY_UNAVAILABLE: 3,
}
"""How strongly a busy type claims time; higher wins over lower."""


@dataclass(frozen=True)
class BusySource:
    """Explains why a segment of a timeline has its busy type.

    Attributes:
        component: The name of the component the time comes from,
            such as ``"VAVAILABILITY"``, ``"AVAILABLE"``,
            ``"VFREEBUSY"`` or ``"VEVENT"``.
        busy_type: The busy type this source contributes.
        uid: The "UID" of the component, if it has one.
        summary: The "SUMMARY" of the component, if it has one.
    """

    component: str
    busy_type: FBTYPE
    uid: str | None = None
    summary: str | None = None


@dataclass(frozen=True)
class TimelineSegment:
    """A half-open interval ``[start, end)`` of a timeline, in UTC.

    Attributes:
        start: The inclusive start of the segment, in UTC.
        end: The exclusive end of the segment, in UTC.
        busy_type: The :class:`~icalendar.enums.FBTYPE` of the segment.
        sources: The :class:`BusySource` entries that explain why the
            segment has its busy type. Empty for time that is free
            because nothing covers it.
    """

    start: datetime
    end: datetime
    busy_type: FBTYPE
    sources: tuple[BusySource, ...] = ()

    @property
    def duration(self) -> timedelta:
        """The length of the segment."""
        return self.end - self.start

    @property
    def is_free(self) -> bool:
        """Whether the segment is free for scheduling."""
        return self.busy_type == FBTYPE.FREE


@dataclass(frozen=True)
class _Contribution:
    """A busy or free interval contributed by one source."""

    start: datetime
    end: datetime
    busy_type: FBTYPE
    source: BusySource


def _as_utc(value: date | datetime) -> datetime:
    """Convert a date or datetime to an aware datetime in UTC.

    Dates become midnight. Naive datetimes are assumed to be in UTC.
    """
    if not isinstance(value, datetime):
        value = datetime.combine(value, time.min)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _union(
    intervals: Iterable[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """Merge overlapping and adjacent intervals into a sorted list."""
    merged: list[list[datetime]] = []
    for start, end in sorted(intervals):
        if start >= end:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _subtract(
    intervals: Iterable[tuple[datetime, datetime]],
    blockers: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """Remove the merged ``blockers`` from ``intervals``."""
    result = []
    for start, end in _union(intervals):
        for blocker_start, blocker_end in blockers:
            if blocker_end <= start:
                continue
            if blocker_start >= end:
                break
            if blocker_start > start:
                result.append((start, min(blocker_start, end)))
            start = max(start, blocker_end)
            if start >= end:
                break
        if start < end:
            result.append((start, end))
    return result


def _clip(
    start: date | datetime,
    end: date | datetime | None,
    coverage_start: datetime,
    coverage_end: datetime,
) -> tuple[datetime, datetime] | None:
    """Clip an interval to a coverage range; ``None`` end is unbounded."""
    clipped_start = max(_as_utc(start), coverage_start)
    clipped_end = coverage_end if end is None else min(_as_utc(end), coverage_end)
    if clipped_start < clipped_end:
        return clipped_start, clipped_end
    return None


def _source(component, busy_type: FBTYPE) -> BusySource:
    """Create the source attribution for a component."""
    uid = getattr(component, "uid", None)
    summary = getattr(component, "summary", None)
    return BusySource(
        component=getattr(component, "name", None) or type(component).__name__,
        busy_type=busy_type,
        uid=None if uid is None else str(uid),
        summary=None if summary is None else str(summary),
    )


def _is_recurring(component) -> bool:
    """Whether the component defines a recurrence set."""
    return any(key in component for key in ("RRULE", "RDATE", "EXDATE"))


def _available_intervals(
    available: Available,
    expand: ExpandAvailable | None,
    window_start: datetime,
    window_end: datetime,
) -> Iterable[tuple[datetime, datetime | None]]:
    """Yield the intervals of an "AVAILABLE" component.

    Recurring components are expanded with the ``expand`` callback.
    Other components contribute their single "DTSTART" to end interval,
    where a ``None`` end is unbounded.
    """
    if expand is not None and _is_recurring(available):
        yield from expand(available, window_start, window_end)
        return
    start = available.start
    if start is None:
        return
    yield start, available.end


def _availability_contributions(
    availabilities: Iterable[Availability],
    window_start: datetime,
    window_end: datetime,
    expand: ExpandAvailable | None,
) -> list[_Contribution]:
    """Collect contributions from "VAVAILABILITY" components.

    Components are combined as specified in :rfc:`7953` section 4:
    they are grouped by their "PRIORITY" value, a missing "PRIORITY"
    means 0, and groups with a lower value claim time first. Components
    in the same group are merged: the available time of the group is the
    union of all "AVAILABLE" periods, the busy time is the rest of the
    combined coverage. Time claimed by a group is not changed by groups
    with a higher "PRIORITY" value.
    """
    groups: dict[int, list[tuple[Availability, tuple[datetime, datetime]]]] = {}
    for availability in availabilities:
        start = availability.start
        end = availability.end
        coverage = _clip(
            window_start if start is None else start,
            window_end if end is None else end,
            window_start,
            window_end,
        )
        if coverage is None:
            continue
        priority = availability.priority or 0
        groups.setdefault(priority, []).append((availability, coverage))
    contributions: list[_Contribution] = []
    claimed: list[tuple[datetime, datetime]] = []
    for priority in sorted(groups):
        group = groups[priority]
        group_coverage = _union(coverage for _, coverage in group)
        free_contributions: list[_Contribution] = []
        for availability, coverage in group:
            for available in availability.available:
                source = _source(available, FBTYPE.FREE)
                for interval_start, interval_end in _available_intervals(
                    available, expand, window_start, window_end
                ):
                    clipped = _clip(interval_start, interval_end, *coverage)
                    if clipped is not None:
                        free_contributions.append(
                            _Contribution(*clipped, FBTYPE.FREE, source)
                        )
        group_free = _union((c.start, c.end) for c in free_contributions)
        for availability, coverage in group:
            busy_type = FBTYPE(str(availability.busy_type))
            source = _source(availability, busy_type)
            for seg_start, seg_end in _subtract(
                _subtract([coverage], group_free), claimed
            ):
                contributions.append(
                    _Contribution(seg_start, seg_end, busy_type, source)
                )
        for contribution in free_contributions:
            for seg_start, seg_end in _subtract(
                [(contribution.start, contribution.end)], claimed
            ):
                contributions.append(
                    _Contribution(
                        seg_start, seg_end, FBTYPE.FREE, contribution.source
                    )
                )
        claimed = _union([*claimed, *group_coverage])
    return contributions


def _fbtype(value) -> FBTYPE:
    """Map an "FBTYPE" parameter value to :class:`FBTYPE`.

    Unknown values are treated as "BUSY", see :rfc:`5545#section-3.2.9`.
    """
    if value is None:
        return FBTYPE.BUSY
    try:
        return FBTYPE(str(value))
    except ValueError:
        return FBTYPE.BUSY


def _freebusy_contributions(
    free_busies: Iterable[FreeBusy],
) -> list[_Contribution]:
    """Collect busy contributions from "VFREEBUSY" components."""
    contributions = []
    for free_busy in free_busies:
        periods = free_busy.get("FREEBUSY")
        if periods is None:
            continue
        if not isinstance(periods, list):
            periods = [periods]
        for period in periods:
            busy_type = _fbtype(period.params.get("FBTYPE"))
            if busy_type == FBTYPE.FREE:
                continue
            contributions.append(
                _Contribution(
                    _as_utc(period.start),
                    _as_utc(period.end),
                    busy_type,
                    _source(free_busy, busy_type),
                )
            )
    return contributions


def _event_interval(event: Event) -> tuple[datetime, datetime] | None:
    """The UTC interval of an expanded event occurrence, if it takes time."""
    start = event.DTSTART
    if start is None:
        return None
    end = event.DTEND
    if end is None:
        if "DURATION" in event:
            end = start + event.decoded("DURATION")
        elif isinstance(start, datetime):
            # a DATE-TIME "DTSTART" without "DTEND" takes no time,
            # see :rfc:`5545#section-3.6.1`
            return None
        else:
            end = start + timedelta(days=1)
    start_utc, end_utc = _as_utc(start), _as_utc(end)
    if end_utc <= start_utc:
        return None
    return start_utc, end_utc


def _event_contributions(events: Iterable[Event]) -> list[_Contribution]:
    """Collect busy contributions from expanded "VEVENT" occurrences.

    Transparent and cancelled events do not take time and are skipped.
    Tentative events contribute "BUSY-TENTATIVE" time.
    """
    contributions = []
    for event in events:
        if event.status == STATUS.CANCELLED:
            continue
        if event.transparency == TRANSP.TRANSPARENT:
            continue
        interval = _event_interval(event)
        if interval is None:
            continue
        busy_type = (
            FBTYPE.BUSY_TENTATIVE if event.status == STATUS.TENTATIVE else FBTYPE.BUSY
        )
        contributions.append(_Contribution(*interval, busy_type, _source(event, busy_type)))
    return contributions


def _timeline(
    contributions: list[_Contribution],
    window_start: datetime,
    window_end: datetime,
) -> list[TimelineSegment]:
    """Segment the window at all contribution boundaries and merge.

    The strongest busy type wins where contributions overlap. Adjacent
    segments with the same busy type and the same sources are merged.
    """
    points = {window_start, window_end}
    for contribution in contributions:
        points.add(contribution.start)
        points.add(contribution.end)
    ordered = sorted(points)
    segments: list[TimelineSegment] = []
    for seg_start, seg_end in zip(ordered, ordered[1:]):
        active = [
            contribution
            for contribution in contributions
            if contribution.start <= seg_start and contribution.end >= seg_end
        ]
        if not active:
            busy_type, sources = FBTYPE.FREE, ()
        else:
            busy_type = max(
                (contribution.busy_type for contribution in active),
                key=_SEVERITY.__getitem__,
            )
            sources = tuple(
                dict.fromkeys(
                    contribution.source
                    for contribution in active
                    if contribution.busy_type == busy_type
                )
            )
        if (
            segments
            and segments[-1].busy_type == busy_type
            and segments[-1].sources == sources
        ):
            segments[-1] = TimelineSegment(segments[-1].start, seg_end, busy_type, sources)
        else:
            segments.append(TimelineSegment(seg_start, seg_end, busy_type, sources))
    return segments


def availability_timeline(
    start: date | datetime,
    end: date | datetime,
    *,
    availabilities: Iterable[Availability] = (),
    free_busy: Iterable[FreeBusy] = (),
    events: Iterable[Event] = (),
    expand: ExpandAvailable | None = None,
) -> list[TimelineSegment]:
    """Combine availability and busy information into a timeline.

    The timeline covers the half-open window ``[start, end)`` with
    adjacent, non-overlapping :class:`TimelineSegment` entries in UTC.
    All input is converted to UTC first, then clipped to the window,
    segmented, and adjacent segments of the same busy type are merged.

    Parameters:
        start: The inclusive start of the window. Naive datetimes and
            dates are assumed to be in UTC.
        end: The exclusive end of the window.
        availabilities: "VAVAILABILITY" components, combined according
            to their "PRIORITY" and "BUSYTYPE" as specified in
            :rfc:`7953` section 4. "AVAILABLE" periods only apply within
            the time range of their enclosing "VAVAILABILITY" component.
        free_busy: "VFREEBUSY" components whose "FREEBUSY" periods are
            added as busy time, honoring their "FBTYPE" parameter.
        events: Already expanded "VEVENT" occurrences. Transparent and
            cancelled events are ignored, tentative events contribute
            "BUSY-TENTATIVE" time.
        expand: A callback to expand a recurring "AVAILABLE" component
            into its occurrences within the window, see
            :data:`ExpandAvailable`. Without it, only the "DTSTART" to
            "DTEND"/"DURATION" interval of a recurring "AVAILABLE"
            component is used.

    Returns:
        The list of :class:`TimelineSegment` covering the window.
        Time not covered by anything is
        :attr:`~icalendar.enums.FBTYPE.FREE`.

    Raises:
        ValueError: If ``end`` is not after ``start``.
    """
    window_start = _as_utc(start)
    window_end = _as_utc(end)
    if window_end <= window_start:
        raise ValueError("The end of the window must be after its start.")
    contributions = [
        *_availability_contributions(availabilities, window_start, window_end, expand),
        *_freebusy_contributions(free_busy),
        *_event_contributions(events),
    ]
    clipped = []
    for contribution in contributions:
        clipped_start = max(contribution.start, window_start)
        clipped_end = min(contribution.end, window_end)
        if clipped_start < clipped_end:
            clipped.append(
                _Contribution(
                    clipped_start,
                    clipped_end,
                    contribution.busy_type,
                    contribution.source,
                )
            )
    return _timeline(clipped, window_start, window_end)
