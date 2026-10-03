"""In-memory iTIP (RFC 5546) scheduling merger for VEVENT messages.

The transport layer (MIME/SMTP) is expected to have already parsed an
``METHOD`` carrying :class:`~icalendar.Calendar` into objects and to have
authenticated the sender out of band.  This module never treats an address
that merely appears in the message (``ORGANIZER``/``ATTENDEE``) as proof of
identity: every authorization decision compares the message claims against
the *authenticated* sender.

Supported methods are ``REQUEST``, ``CANCEL`` and ``REPLY``.  For every
message a :class:`ITIPReport` is produced with one of four dispositions:

* ``applied``  -- the local calendar state changed;
* ``ignored``  -- a harmless retransmission or an obsolete version;
* ``conflict`` -- the message cannot be merged automatically;
* ``rejected`` -- the sender is not authorized or the message is malformed.

Versioning uses ``SEQUENCE`` first and ``DTSTAMP`` as the tie breaker, as
required by RFC 5546.  Both whole-series (no ``RECURRENCE-ID``) and
single-instance messages are supported.  Input objects are never mutated; a
new :class:`~icalendar.Calendar` is returned.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING

from dateutil.rrule import rrulestr

from icalendar import Calendar, Event
from icalendar.prop import vCalAddress

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

__all__ = [
    "AuthenticatedSender",
    "ITIPMessage",
    "ITIPMethod",
    "ITIPAction",
    "ITIPReport",
    "ITIPMerger",
    "message_from_calendar",
    "merge_itip_message",
    "merge_itip_messages",
]


class ITIPMethod(str, Enum):
    """iTIP methods handled by the merger."""

    REQUEST = "REQUEST"
    REPLY = "REPLY"
    CANCEL = "CANCEL"


class ITIPAction(str, Enum):
    """Disposition of a processed iTIP message."""

    APPLIED = "applied"
    IGNORED = "ignored"
    CONFLICT = "conflict"
    REJECTED = "rejected"


@dataclass(frozen=True)
class AuthenticatedSender:
    """The identity the transport has authenticated for a message.

    ``address`` is the normalized calendar address (``mailto:`` URI) that the
    transport proved the message came from, e.g. through DKIM/DMARC alignment
    or S/MIME signature verification.  It is deliberately separate from any
    address inside the iCalendar payload.
    """

    address: str
    common_name: str | None = None

    @classmethod
    def from_address(cls, address: str, common_name: str | None = None) -> AuthenticatedSender:
        """Create a sender, normalizing a bare e-mail address to ``mailto:``."""
        return cls(normalize_address(address), common_name)


@dataclass(frozen=True)
class ITIPMessage:
    """One parsed iTIP message carrying exactly one VEVENT.

    Attributes:
        method: The iTIP method.
        event: The VEVENT payload (a private copy is retained).
        sender: The transport-authenticated originator.
        label: Optional caller supplied identifier, echoed on the report.
    """

    method: ITIPMethod
    event: Event
    sender: AuthenticatedSender
    label: str | None = None

    @classmethod
    def create(
        cls,
        method: ITIPMethod | str,
        event: Event,
        sender: AuthenticatedSender | str,
        label: str | None = None,
    ) -> ITIPMessage:
        """Build a message from loose inputs (event is copied)."""
        if isinstance(sender, str):
            sender = AuthenticatedSender.from_address(sender)
        return cls(
            method=ITIPMethod(method.upper()) if isinstance(method, str) else method,
            event=copy.deepcopy(event),
            sender=sender,
            label=label,
        )


@dataclass
class ITIPReport:
    """Result of merging one :class:`ITIPMessage`.

    Attributes:
        action: What happened to the message.
        reason: Short machine readable explanation.
        detail: Human readable explanation.
        method: Method of the processed message.
        uid: UID targeted by the message.
        recurrence_id: RECURRENCE-ID targeted by the message, if any.
        label: Caller label copied from the message, if given.
    """

    action: ITIPAction
    reason: str
    detail: str = ""
    method: ITIPMethod | None = None
    uid: str | None = None
    recurrence_id: datetime | date | None = None
    label: str | None = None

    @property
    def applied(self) -> bool:
        """Whether the message was applied."""
        return self.action is ITIPAction.APPLIED

    @property
    def ignored(self) -> bool:
        """Whether the message was ignored."""
        return self.action is ITIPAction.IGNORED

    @property
    def conflicted(self) -> bool:
        """Whether the message produced a conflict."""
        return self.action is ITIPAction.CONFLICT

    @property
    def rejected(self) -> bool:
        """Whether the message was rejected."""
        return self.action is ITIPAction.REJECTED


def message_from_calendar(
    calendar: Calendar | str | bytes,
    sender: AuthenticatedSender | str,
    label: str | None = None,
) -> ITIPMessage:
    """Extract a single-VEVENT iTIP message from a parsed ``METHOD`` calendar.

    Raises:
        ValueError: if the calendar has no ``METHOD``, carries more than one
            VEVENT, or carries no VEVENT at all.
    """
    if isinstance(calendar, str | bytes):
        calendar = Calendar.from_ical(calendar)
    method_value = calendar.get("METHOD")
    if method_value is None:
        msg = "iTIP message calendar must carry a METHOD property"
        raise ValueError(msg)
    try:
        method = ITIPMethod(str(method_value).upper())
    except ValueError as exc:
        msg = f"unsupported iTIP METHOD: {method_value!r}"
        raise ValueError(msg) from exc
    events = [component for component in calendar.walk("VEVENT")]
    if len(events) != 1:
        msg = f"expected exactly one VEVENT in iTIP message, got {len(events)}"
        raise ValueError(msg)
    return ITIPMessage.create(method, events[0], sender, label)


# ---------------------------------------------------------------------------
# Small property helpers
# ---------------------------------------------------------------------------


def normalize_address(address: object) -> str:
    """Return the canonical ``mailto:`` form of a calendar address."""
    text = str(address).strip()
    if "@" in text and not text.lower().startswith("mailto:"):
        text = f"mailto:{text}"
    return text.lower()


def _event_organizer(event: Event) -> str | None:
    organizer = event.get("ORGANIZER")
    if organizer is None:
        return None
    return normalize_address(organizer)


def _event_sequence(event: Event) -> int:
    value = event.get("SEQUENCE")
    return int(value) if value is not None else 0


def _event_dtstamp(event: Event) -> datetime | None:
    value = event.get("DTSTAMP")
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    return None


def _recurrence_id(event: Event) -> date | datetime | None:
    value = event.get("RECURRENCE-ID")
    if value is None:
        return None
    if isinstance(value, date | datetime):
        return value
    wrapped = getattr(value, "dt", value)
    return wrapped if isinstance(wrapped, date | datetime) else None


def _attendee_partstat(attendee: vCalAddress) -> str:
    return str(attendee.params.get("PARTSTAT", "NEEDS-ACTION")).upper()


def _attendee_map(event: Event) -> dict[str, vCalAddress]:
    attendees = event.get("ATTENDEE", [])
    if isinstance(attendees, vCalAddress):
        attendees = [attendees]
    return {normalize_address(attendee): attendee for attendee in attendees}


def _values(event: Event, name: str) -> list[object]:
    """Return all values of a (possibly multi valued) property as a list."""
    value = event.get(name)
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _compare_version(incoming: Event, stored: Event) -> int:
    """Compare two VEVENTs by (SEQUENCE, DTSTAMP).

    Returns ``-1`` if *incoming* is older, ``0`` for the same version and
    ``1`` if it is newer.  Equal SEQUENCE with no comparable DTSTAMP is a
    tie, which RFC 5546 treats as a possible retransmission.
    """
    incoming_sequence = _event_sequence(incoming)
    stored_sequence = _event_sequence(stored)
    if incoming_sequence != stored_sequence:
        return (incoming_sequence > stored_sequence) - (incoming_sequence < stored_sequence)
    incoming_stamp = _event_dtstamp(incoming)
    stored_stamp = _event_dtstamp(stored)
    if incoming_stamp is None or stored_stamp is None or incoming_stamp == stored_stamp:
        return 0
    return (incoming_stamp > stored_stamp) - (incoming_stamp < stored_stamp)


def _same_when(value_a: object, value_b: object) -> bool:
    if value_a is None or value_b is None:
        return value_a is value_b
    bytes_a = value_a.to_ical() if hasattr(value_a, "to_ical") else str(value_a)
    bytes_b = value_b.to_ical() if hasattr(value_b, "to_ical") else str(value_b)
    return bytes_a == bytes_b


# Properties that define a meeting as opposed to per-attendee bookkeeping.
_SIGNIFICANT_PROPERTIES = (
    "SUMMARY",
    "DESCRIPTION",
    "LOCATION",
    "DTSTART",
    "DTEND",
    "DURATION",
    "RRULE",
    "RDATE",
    "EXDATE",
    "STATUS",
    "CLASS",
)


def _significant_change(old: Event, new: Event) -> bool:
    for name in _SIGNIFICANT_PROPERTIES:
        if not _same_when(old.get(name), new.get(name)):
            return True
    old_attendees = _attendee_map(old)
    new_attendees = _attendee_map(new)
    if set(old_attendees) != set(new_attendees):
        return True
    for address in old_attendees.keys() & new_attendees.keys():
        if not _same_when(old_attendees[address], new_attendees[address]):
            return True
    return False


def _identical_events(left: Event, right: Event) -> bool:
    """Exact retransmission test (same property set, order independent)."""
    left_keys = {key for key in left.keys() if not key.startswith("_")}
    right_keys = {key for key in right.keys() if not key.startswith("_")}
    if left_keys != right_keys:
        return False
    for key in left_keys:
        left_values = _values(left, key) or []
        right_values = _values(right, key) or []
        if key == "ATTENDEE":
            left_values = sorted(normalize_address(item) for item in left_values)
            right_values = sorted(normalize_address(item) for item in right_values)
            if left_values != right_values:
                return False
            continue
        if len(left_values) != len(right_values):
            return False
        for left_value, right_value in zip(left_values, right_values, strict=True):
            left_bytes = (
                left_value.to_ical()
                if hasattr(left_value, "to_ical")
                else str(left_value)
            )
            right_bytes = (
                right_value.to_ical()
                if hasattr(right_value, "to_ical")
                else str(right_value)
            )
            if left_bytes != right_bytes:
                return False
    return True


def _same_cancellation(incoming: Event, cancelled: Event) -> bool:
    """Whether *incoming* is a retransmission of the stored CANCEL.

    Cancellation messages are often stripped to UID/SEQUENCE/DTSTAMP by the
    sender, so identity is based on the cancellation key rather than on every
    property.
    """
    if _event_sequence(incoming) != _event_sequence(cancelled):
        return False
    incoming_stamp = _event_dtstamp(incoming)
    cancelled_stamp = _event_dtstamp(cancelled)
    # Equal DTSTAMP proves a retransmission; when the stored timestamp only
    # came from the cancellation itself, matching SEQUENCE on an already
    # CANCELLED target is treated as the same cancellation notice.
    if incoming_stamp is None or cancelled_stamp is None:
        return True
    return incoming_stamp == cancelled_stamp


def _expanded_starts(master: Event, limit: int = 366) -> list[datetime]:
    """Generate recurrence instance start datetimes (RRULE and RDATE)."""
    start = master.start
    if not isinstance(start, datetime):
        start = datetime(start.year, start.month, start.day)
    starts: list[datetime] = [start]
    rrules = _values(master, "RRULE") or []
    for rule in rrules:
        line = rule.to_ical().decode() if isinstance(rule.to_ical(), bytes) else rule.to_ical()
        rule_set = rrulestr(f"RRULE:{line}", dtstart=start, ignoretz=True)
        for candidate in list(rule_set)[: limit - 1]:
            if candidate != start and candidate not in starts:
                starts.append(candidate)
    for value in _values(master, "RDATE") or []:
        candidates = getattr(value, "dts", None) or [value]
        for candidate in candidates:
            moment = getattr(candidate, "dt", candidate)
            if isinstance(moment, datetime) and moment not in starts:
                starts.append(moment)
    return starts[:limit]


def _instance_exists(master: Event, recurrence_id: date | datetime) -> bool:
    if isinstance(recurrence_id, datetime):
        target = recurrence_id.replace(tzinfo=None)
        return any(
            target
            == (candidate.replace(tzinfo=None) if candidate.tzinfo else candidate)
            for candidate in _expanded_starts(master)
        )
    target = recurrence_id
    start = master.start
    start_date = start.date() if isinstance(start, datetime) else start
    if target == start_date:
        return True
    return any(
        (candidate.date() if isinstance(candidate, datetime) else candidate) == target
        for candidate in _expanded_starts(master)
    )


def _event_duration(event: Event) -> timedelta:
    start = event.start
    end = event.end
    if isinstance(start, datetime) and isinstance(end, datetime):
        return end - start
    if isinstance(start, date) and isinstance(end, date):
        return end - start
    return timedelta(0)


class ITIPMerger:
    """Merge authenticated iTIP VEVENT messages into an in-memory calendar.

    The merger itself is stateless apart from configuration; each call gets
    the current calendar explicitly, so the same instance is safe to reuse.
    """

    def __init__(self, *, reset_partstat_on: Sequence[str] | None = None) -> None:
        self._reset_partstat_on = tuple(
            name.upper()
            for name in (
                reset_partstat_on
                if reset_partstat_on is not None
                else _SIGNIFICANT_PROPERTIES
            )
        )

    # -- public API --------------------------------------------------------

    def merge(
        self,
        calendar: Calendar | str | bytes,
        message: ITIPMessage,
    ) -> tuple[Calendar, ITIPReport]:
        """Return ``(new_calendar, report)`` without mutating any input."""
        parsed_calendar = (
            Calendar.from_ical(calendar)
            if isinstance(calendar, str | bytes)
            else calendar
        )
        working = copy.deepcopy(parsed_calendar)
        report = self._build_report(message)
        validation = self._validate(message)
        if validation is not None:
            action, reason, detail = validation
            report.action = action
            report.reason = reason
            report.detail = detail
            return working, report
        if message.method is ITIPMethod.REQUEST:
            self._merge_request(working, message, report)
        elif message.method is ITIPMethod.CANCEL:
            self._merge_cancel(working, message, report)
        else:
            self._merge_reply(working, message, report)
        return working, report

    # -- validation --------------------------------------------------------

    def _build_report(self, message: ITIPMessage) -> ITIPReport:
        return ITIPReport(
            action=ITIPAction.REJECTED,
            reason="unprocessed",
            detail="",
            method=message.method,
            uid=str(message.event.uid) if message.event.get("UID") is not None else None,
            recurrence_id=_recurrence_id(message.event),
            label=message.label,
        )

    def _validate(
        self, message: ITIPMessage
    ) -> tuple[ITIPAction, str, str] | None:
        event = message.event
        if event.get("UID") is None:
            return (
                ITIPAction.REJECTED,
                "missing-uid",
                "iTIP VEVENT must carry a UID",
            )
        if message.method in (ITIPMethod.REQUEST, ITIPMethod.CANCEL):
            organizer = _event_organizer(event)
            if organizer is None:
                return (
                    ITIPAction.REJECTED,
                    "missing-organizer",
                    f"{message.method.value} message must carry ORGANIZER",
                )
            if organizer != normalize_address(message.sender.address):
                return (
                    ITIPAction.REJECTED,
                    "sender-not-organizer",
                    (
                        f"authenticated sender {message.sender.address} does not match "
                        f"ORGANIZER {organizer}"
                    ),
                )
        else:
            attendee = _attendee_map(event).get(normalize_address(message.sender.address))
            if attendee is None:
                return (
                    ITIPAction.REJECTED,
                    "sender-not-attendee",
                    (
                        f"authenticated sender {message.sender.address} is not listed as "
                        "an ATTENDEE in the REPLY"
                    ),
                )
            if _attendee_partstat(attendee) not in {
                "NEEDS-ACTION",
                "ACCEPTED",
                "DECLINED",
                "TENTATIVE",
                "DELEGATED",
            }:
                return (
                    ITIPAction.REJECTED,
                    "invalid-partstat",
                    f"unsupported PARTSTAT {_attendee_partstat(attendee)!r}",
                )
        return None

    # -- calendar indexing -------------------------------------------------

    @staticmethod
    def _index(
        calendar: Calendar, uid: str
    ) -> tuple[Event | None, dict[date | datetime, Event]]:
        master = None
        overrides: dict[date | datetime, Event] = {}
        for event in calendar.events:
            if str(event.get("UID")) != uid:
                continue
            recurrence_id = _recurrence_id(event)
            if recurrence_id is None:
                if master is None:
                    master = event
            else:
                overrides[recurrence_id] = event
        return master, overrides

    @staticmethod
    def _replace_event(calendar: Calendar, old: Event, new: Event) -> None:
        calendar.subcomponents = [
            new if component is old else component
            for component in calendar.subcomponents
        ]

    @staticmethod
    def _remove_event(calendar: Calendar, target: Event) -> None:
        calendar.subcomponents = [
            component for component in calendar.subcomponents if component is not target
        ]

    # -- REQUEST -----------------------------------------------------------

    def _merge_request(
        self, calendar: Calendar, message: ITIPMessage, report: ITIPReport
    ) -> None:
        incoming = message.event
        uid = str(incoming.uid)
        recurrence_id = _recurrence_id(incoming)
        master, overrides = self._index(calendar, uid)
        if recurrence_id is None:
            self._merge_master_request(calendar, message, master, overrides, report)
            return
        if master is None:
            report.action = ITIPAction.REJECTED
            report.reason = "master-missing"
            report.detail = (
                f"instance {recurrence_id!s} REQUEST without a master event for UID {uid}"
            )
            return
        stored_override = overrides.get(recurrence_id)
        if stored_override is not None:
            self._merge_override_request(
                calendar, message, master, stored_override, report
            )
            return
        if not _instance_exists(master, recurrence_id):
            report.action = ITIPAction.CONFLICT
            report.reason = "unknown-instance"
            report.detail = (
                f"RECURRENCE-ID {recurrence_id!s} is not part of the series UID {uid}"
            )
            return
        if _compare_version(incoming, master) < 0:
            report.action = ITIPAction.IGNORED
            report.reason = "obsolete-version"
            report.detail = (
                "instance REQUEST is older (SEQUENCE/DTSTAMP) than the stored series"
            )
            return
        new_override = copy.deepcopy(incoming)
        calendar.add_component(new_override)
        report.action = ITIPAction.APPLIED
        report.reason = "instance-created"
        report.detail = f"stored new exception for instance {recurrence_id!s}"

    def _merge_master_request(
        self,
        calendar: Calendar,
        message: ITIPMessage,
        master: Event | None,
        overrides: dict[date | datetime, Event],
        report: ITIPReport,
    ) -> None:
        incoming = message.event
        uid = str(incoming.uid)
        if master is None:
            calendar.add_component(copy.deepcopy(incoming))
            report.action = ITIPAction.APPLIED
            report.reason = "created"
            report.detail = f"stored new meeting UID {uid}"
            return

        if str(master.get("STATUS", "CONFIRMED")).upper() == "CANCELLED":
            comparison = _compare_version(incoming, master)
            if comparison <= 0:
                if _identical_events(incoming, master):
                    report.action = ITIPAction.IGNORED
                    report.reason = "duplicate"
                    report.detail = "identical retransmission of the stored REQUEST"
                elif comparison == 0:
                    report.action = ITIPAction.CONFLICT
                    report.reason = "divergent-same-version"
                    report.detail = (
                        "same SEQUENCE/DTSTAMP as the CANCELLED series but different "
                        "content"
                    )
                else:
                    report.action = ITIPAction.IGNORED
                    report.reason = "obsolete-version"
                    report.detail = "series is already CANCELLED and the REQUEST is older"
                return
            new_master = copy.deepcopy(incoming)
            new_master["STATUS"] = "CONFIRMED"
            self._reset_attendee_partstats(new_master)
            self._replace_event(calendar, master, new_master)
            for override in list(overrides.values()):
                self._remove_event(calendar, override)
            report.action = ITIPAction.APPLIED
            report.reason = "rescheduled"
            report.detail = "reopened the previously cancelled series"
            return

        comparison = _compare_version(incoming, master)
        if comparison < 0:
            if _identical_events(incoming, master):
                report.action = ITIPAction.IGNORED
                report.reason = "duplicate"
                report.detail = "identical retransmission of the stored REQUEST"
            else:
                report.action = ITIPAction.IGNORED
                report.reason = "obsolete-version"
                report.detail = "older SEQUENCE/DTSTAMP than the stored REQUEST"
            return

        if comparison == 0:
            if _identical_events(incoming, master):
                report.action = ITIPAction.IGNORED
                report.reason = "duplicate"
                report.detail = "identical retransmission of the stored REQUEST"
                return
            new_master = copy.deepcopy(incoming)
            self._carry_partstats(master, new_master)
            self._replace_event(calendar, master, new_master)
            report.action = ITIPAction.APPLIED
            report.reason = "refreshed"
            report.detail = "same-version organizer update; attendee responses preserved"
            return

        # comparison > 0: newer organizer version.
        new_master = copy.deepcopy(incoming)
        significant = self._significant_with_configuration(master, new_master)
        if significant:
            self._reset_attendee_partstats(new_master)
        else:
            self._carry_partstats(master, new_master)
        self._replace_event(calendar, master, new_master)
        new_sequence = _event_sequence(new_master)
        for recurrence_id, override in list(overrides.items()):
            if _event_sequence(override) < new_sequence:
                self._remove_event(calendar, override)
                overrides.pop(recurrence_id, None)
        report.action = ITIPAction.APPLIED
        report.reason = "rescheduled" if significant else "updated"
        report.detail = (
            "newer organizer version applied; attendee responses reset"
            if significant
            else "newer organizer version applied; attendee responses preserved"
        )

    def _merge_override_request(
        self,
        calendar: Calendar,
        message: ITIPMessage,
        master: Event,
        stored: Event,
        report: ITIPReport,
    ) -> None:
        incoming = message.event
        recurrence_id = _recurrence_id(incoming)
        if str(stored.get("STATUS", "CONFIRMED")).upper() == "CANCELLED":
            comparison = _compare_version(incoming, stored)
            if comparison <= 0:
                report.action = ITIPAction.IGNORED if comparison == 0 else ITIPAction.CONFLICT
                report.reason = "duplicate" if comparison == 0 else "obsolete-version"
                report.detail = (
                    f"instance {recurrence_id!s} is already CANCELLED"
                    + ("" if comparison == 0 else " and the REQUEST is older")
                )
                return
        comparison = _compare_version(incoming, stored)
        if comparison < 0:
            report.action = ITIPAction.IGNORED
            report.reason = "obsolete-version" if not _identical_events(incoming, stored) else "duplicate"
            report.detail = (
                "older instance REQUEST" if report.reason == "obsolete-version"
                else "identical retransmission of the stored instance"
            )
            return
        if comparison == 0 and _identical_events(incoming, stored):
            report.action = ITIPAction.IGNORED
            report.reason = "duplicate"
            report.detail = "identical retransmission of the stored instance"
            return
        new_override = copy.deepcopy(incoming)
        self._carry_partstats(stored, new_override)
        self._replace_event(calendar, stored, new_override)
        report.action = ITIPAction.APPLIED
        report.reason = "instance-updated"
        report.detail = f"organizer update applied to instance {recurrence_id!s}"

    def _significant_with_configuration(self, old: Event, new: Event) -> bool:
        if _event_sequence(new) != _event_sequence(old):
            return True
        for name in self._reset_partstat_on:
            if not _same_when(old.get(name), new.get(name)):
                return True
        old_attendees = _attendee_map(old)
        new_attendees = _attendee_map(new)
        return set(old_attendees) != set(new_attendees)

    @staticmethod
    def _reset_attendee_partstats(event: Event) -> None:
        for attendee in _values(event, "ATTENDEE") or []:
            attendee.params["PARTSTAT"] = "NEEDS-ACTION"

    @staticmethod
    def _carry_partstats(old: Event, new: Event) -> None:
        """Preserve known per-attendee responses on an organizer update."""
        previous = _attendee_map(old)
        for attendee in _values(new, "ATTENDEE") or []:
            known = previous.get(normalize_address(attendee))
            if known is not None:
                attendee.params["PARTSTAT"] = _attendee_partstat(known)

    # -- CANCEL ------------------------------------------------------------

    def _merge_cancel(
        self, calendar: Calendar, message: ITIPMessage, report: ITIPReport
    ) -> None:
        incoming = message.event
        uid = str(incoming.uid)
        recurrence_id = _recurrence_id(incoming)
        master, overrides = self._index(calendar, uid)
        if recurrence_id is None:
            self._cancel_master(calendar, message, master, overrides, report)
        else:
            self._cancel_instance(
                calendar, message, master, overrides, recurrence_id, report
            )

    def _cancel_master(
        self,
        calendar: Calendar,
        message: ITIPMessage,
        master: Event | None,
        overrides: dict[date | datetime, Event],
        report: ITIPReport,
    ) -> None:
        incoming = message.event
        uid = str(incoming.uid)
        if master is None:
            report.action = ITIPAction.IGNORED
            report.reason = "unknown-uid"
            report.detail = f"CANCEL for unknown UID {uid}"
            return
        if str(master.get("STATUS", "CONFIRMED")).upper() == "CANCELLED":
            report.action = ITIPAction.IGNORED
            if _same_cancellation(incoming, master):
                report.reason = "duplicate"
                report.detail = "series already CANCELLED"
            elif _compare_version(incoming, master) < 0:
                report.reason = "obsolete-version"
                report.detail = "older CANCEL for an already cancelled series"
            else:
                report.reason = "duplicate"
                report.detail = "series already CANCELLED (re-notification)"
            return
        if _compare_version(incoming, master) < 0:
            report.action = ITIPAction.CONFLICT
            report.reason = "obsolete-version"
            report.detail = "CANCEL is older than the stored meeting version"
            return
        cancelled = copy.deepcopy(master)
        cancelled["STATUS"] = "CANCELLED"
        if incoming.get("SEQUENCE") is not None:
            cancelled["SEQUENCE"] = incoming["SEQUENCE"]
        if incoming.get("DTSTAMP") is not None:
            cancelled["DTSTAMP"] = incoming["DTSTAMP"]
        self._replace_event(calendar, master, cancelled)
        for override in list(overrides.values()):
            self._remove_event(calendar, override)
        report.action = ITIPAction.APPLIED
        report.reason = "cancelled"
        report.detail = f"whole series UID {uid} cancelled"

    def _cancel_instance(
        self,
        calendar: Calendar,
        message: ITIPMessage,
        master: Event | None,
        overrides: dict[date | datetime, Event],
        recurrence_id: date | datetime,
        report: ITIPReport,
    ) -> None:
        incoming = message.event
        uid = str(incoming.uid)
        if master is None:
            report.action = ITIPAction.IGNORED
            report.reason = "unknown-uid"
            report.detail = f"instance CANCEL for unknown UID {uid}"
            return
        stored = overrides.get(recurrence_id)
        if stored is not None:
            if str(stored.get("STATUS", "CONFIRMED")).upper() == "CANCELLED":
                report.action = ITIPAction.IGNORED
                if _same_cancellation(incoming, stored):
                    report.reason = "duplicate"
                    report.detail = f"instance {recurrence_id!s} already CANCELLED"
                elif _compare_version(incoming, stored) < 0:
                    report.reason = "obsolete-version"
                    report.detail = "older CANCEL for an already cancelled instance"
                else:
                    report.reason = "duplicate"
                    report.detail = (
                        f"instance {recurrence_id!s} already CANCELLED (re-notification)"
                    )
                return
            if _compare_version(incoming, stored) < 0:
                report.action = ITIPAction.CONFLICT
                report.reason = "obsolete-version"
                report.detail = "CANCEL is older than the stored exception version"
                return
            cancelled = copy.deepcopy(stored)
            cancelled["STATUS"] = "CANCELLED"
            if incoming.get("SEQUENCE") is not None:
                cancelled["SEQUENCE"] = incoming["SEQUENCE"]
            if incoming.get("DTSTAMP") is not None:
                cancelled["DTSTAMP"] = incoming["DTSTAMP"]
            self._replace_event(calendar, stored, cancelled)
            self._add_exdate(master, recurrence_id)
            report.action = ITIPAction.APPLIED
            report.reason = "instance-cancelled"
            report.detail = f"instance {recurrence_id!s} cancelled"
            return
        if not _instance_exists(master, recurrence_id):
            report.action = ITIPAction.CONFLICT
            report.reason = "unknown-instance"
            report.detail = (
                f"CANCEL RECURRENCE-ID {recurrence_id!s} is not part of series UID {uid}"
            )
            return
        if _compare_version(incoming, master) < 0:
            report.action = ITIPAction.CONFLICT
            report.reason = "obsolete-version"
            report.detail = "instance CANCEL is older than the stored series version"
            return
        cancelled = self._materialize_instance(master, recurrence_id)
        cancelled["STATUS"] = "CANCELLED"
        if incoming.get("SEQUENCE") is not None:
            cancelled["SEQUENCE"] = incoming["SEQUENCE"]
        if incoming.get("DTSTAMP") is not None:
            cancelled["DTSTAMP"] = incoming["DTSTAMP"]
        calendar.add_component(cancelled)
        self._add_exdate(master, recurrence_id)
        report.action = ITIPAction.APPLIED
        report.reason = "instance-cancelled"
        report.detail = f"instance {recurrence_id!s} cancelled"

    @staticmethod
    def _add_exdate(master: Event, recurrence_id: date | datetime) -> None:
        existing = []
        for value in _values(master, "EXDATE") or []:
            existing.extend(item.dt for item in value.dts)
        if recurrence_id not in existing:
            master.add("EXDATE", recurrence_id)

    # -- REPLY -------------------------------------------------------------

    def _merge_reply(
        self, calendar: Calendar, message: ITIPMessage, report: ITIPReport
    ) -> None:
        incoming = message.event
        uid = str(incoming.uid)
        recurrence_id = _recurrence_id(incoming)
        sender = normalize_address(message.sender.address)
        master, overrides = self._index(calendar, uid)

        if master is None:
            report.action = ITIPAction.IGNORED
            report.reason = "unknown-uid"
            report.detail = f"REPLY for unknown UID {uid}"
            return
        claimed_organizer = _event_organizer(incoming)
        if claimed_organizer is not None and claimed_organizer != _event_organizer(master):
            report.action = ITIPAction.REJECTED
            report.reason = "organizer-mismatch"
            report.detail = (
                f"REPLY names organizer {claimed_organizer} but the stored meeting "
                f"organizer is {_event_organizer(master)}"
            )
            return

        target = master
        materialized = False
        if recurrence_id is not None:
            target = overrides.get(recurrence_id)
            if target is None:
                if not _instance_exists(master, recurrence_id):
                    report.action = ITIPAction.IGNORED
                    report.reason = "unknown-instance"
                    report.detail = (
                        f"REPLY for RECURRENCE-ID {recurrence_id!s} that is not in the series"
                    )
                    return
                target = self._materialize_instance(master, recurrence_id)
                target["SEQUENCE"] = _event_sequence(master)
                materialized = True

        if sender not in _attendee_map(master):
            report.action = ITIPAction.REJECTED
            report.reason = "sender-uninvited"
            report.detail = (
                f"authenticated sender {sender} is not on the attendee list of the "
                "stored meeting"
            )
            return

        if sender not in _attendee_map(target):
            report.action = ITIPAction.REJECTED
            report.reason = "sender-uninvited-instance"
            report.detail = (
                f"authenticated sender {sender} is not on the attendee list of the "
                f"stored instance {recurrence_id!s}"
            )
            return

        # Version checks come before the CANCELLED check so that an obsolete
        # reply is recognized as obsolete regardless of delivery order.
        incoming_sequence = _event_sequence(incoming)
        stored_sequence = _event_sequence(master)
        if incoming_sequence < stored_sequence:
            report.action = ITIPAction.IGNORED
            report.reason = "obsolete-version"
            report.detail = (
                f"REPLY is for SEQUENCE {incoming_sequence} but the stored meeting is at "
                f"SEQUENCE {stored_sequence}"
            )
            return
        if incoming_sequence > stored_sequence:
            report.action = ITIPAction.CONFLICT
            report.reason = "reply-newer-than-request"
            report.detail = (
                f"REPLY is for SEQUENCE {incoming_sequence} but the stored invitation is "
                f"only at SEQUENCE {stored_sequence}; refusing to let a reply advance "
                "meeting state"
            )
            return

        if str(target.get("STATUS", "CONFIRMED")).upper() == "CANCELLED":
            report.action = ITIPAction.IGNORED
            report.reason = "target-cancelled"
            report.detail = "REPLY targets a CANCELLED meeting or instance"
            return

        wanted_partstat = _attendee_partstat(_attendee_map(incoming)[sender])
        current_partstat = _attendee_partstat(_attendee_map(target)[sender])
        if current_partstat == wanted_partstat and not materialized:
            report.action = ITIPAction.IGNORED
            report.reason = "duplicate"
            report.detail = f"{sender} already responded {wanted_partstat}"
            return

        for attendee in _values(target, "ATTENDEE") or []:
            if normalize_address(attendee) == sender:
                attendee.params["PARTSTAT"] = wanted_partstat
                break
        if materialized:
            calendar.add_component(target)
        report.action = ITIPAction.APPLIED
        report.reason = "response-recorded"
        report.detail = (
            f"{sender} responded {wanted_partstat}"
            + (f" to instance {recurrence_id!s}" if recurrence_id is not None else "")
        )

    # -- instance materialization -----------------------------------------

    @staticmethod
    def _materialize_instance(
        master: Event, recurrence_id: date | datetime
    ) -> Event:
        instance = Event()
        for name in ("UID", "ORGANIZER", "CLASS", "SUMMARY", "DESCRIPTION", "LOCATION"):
            if master.get(name) is not None:
                instance[name] = copy.deepcopy(master[name])
        instance["RECURRENCE-ID"] = recurrence_id
        start = recurrence_id
        if isinstance(start, date) and not isinstance(start, datetime):
            master_start = master.start
            if isinstance(master_start, datetime):
                start = datetime(start.year, start.month, start.day)
        instance["DTSTART"] = start
        master_duration = _event_duration(master)
        if isinstance(start, datetime):
            instance["DTEND"] = start + master_duration
        else:
            instance["DTEND"] = start + master_duration
        for attendee in _values(master, "ATTENDEE") or []:
            instance.add("attendee", copy.deepcopy(attendee))
        stamp = master.get("DTSTAMP")
        if stamp is not None:
            instance["DTSTAMP"] = copy.deepcopy(stamp)
        return instance


# ---------------------------------------------------------------------------
# Convenience functions
# ---------------------------------------------------------------------------


def merge_itip_message(
    calendar: Calendar | str | bytes,
    message: ITIPMessage,
    *,
    merger: ITIPMerger | None = None,
) -> tuple[Calendar, ITIPReport]:
    """Merge a single iTIP message into *calendar*.

    The input calendar and message are not mutated.
    """
    return (merger or ITIPMerger()).merge(calendar, message)


def merge_itip_messages(
    calendar: Calendar | str | bytes,
    messages: Iterable[ITIPMessage],
    *,
    merger: ITIPMerger | None = None,
) -> tuple[Calendar, list[ITIPReport]]:
    """Merge an ordered stream of iTIP messages.

    Each message is merged against the state produced by the preceding one.
    The initial calendar is copied once; messages are never mutated.
    """
    active_merger = merger or ITIPMerger()
    if isinstance(calendar, str | bytes):
        current = Calendar.from_ical(calendar)
    else:
        current = calendar
    current = copy.deepcopy(current)
    reports: list[ITIPReport] = []
    for message in messages:
        current, report = active_merger.merge(current, message)
        reports.append(report)
    return current, reports
