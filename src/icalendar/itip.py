"""Pure in-memory merging of iTIP scheduling messages (RFC 5546).

The mail layer is expected to parse incoming mail into
:class:`~icalendar.Calendar` objects.  This module decides how the
scheduling messages ``REQUEST``, ``CANCEL`` and ``REPLY`` update a
local, purely in-memory calendar.  It performs no I/O and knows
nothing about SMTP.

Trust model
-----------

Addresses found inside a message are *claims*, not facts.  The only
trusted input is the authenticated :class:`Identity` handed in by the
caller:

* ``REQUEST`` and ``CANCEL`` are honoured only when the authenticated
  sender matches the ``ORGANIZER`` of the event.
* ``REPLY`` is honoured only for ``ATTENDEE`` addresses that belong to
  the authenticated sender, and only if that attendee is actually
  invited.  A reply can therefore only ever update the sender's own
  response fields (``PARTSTAT``).

Versioning
----------

Versions are ordered by ``SEQUENCE`` and then ``DTSTAMP``:

* A higher ``SEQUENCE`` supersedes the stored version.  Replies
  recorded for older sequences are dropped, because they refer to an
  invitation that no longer applies.
* An equal ``SEQUENCE`` with identical content is a retransmission and
  is ignored; diverging content at the same sequence is reported as a
  conflict and the stored version is kept.
* A lower ``SEQUENCE`` (or, for replies, a stale ``DTSTAMP``) is
  ignored.

``CANCEL`` is supported for a whole series (no ``RECURRENCE-ID``) and
for single instances.  Replies that arrive before their invitation are
held as pending and applied once a matching invitation shows up, which
makes the merge robust against out-of-order delivery.

Everything is immutable: input calendars are never modified and every
merge returns a new :class:`CalendarState` together with a
:class:`MergeReport` explaining whether the message was applied,
ignored, conflicting or rejected.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Any, Iterable, Mapping

from icalendar.cal import Calendar, Component

__all__ = [
    "CalendarState",
    "Identity",
    "MergeReport",
    "Outcome",
    "ReplyRecord",
    "SeriesState",
    "normalize_address",
    "process_messages",
]

_METHODS = ("REQUEST", "CANCEL", "REPLY")


def normalize_address(value: Any) -> str:
    """Normalize a calendar address for comparison.

    The ``mailto:`` prefix is stripped and the rest is lowercased, so
    ``Mailto:Alice@Example.COM`` and ``mailto:alice@example.com``
    compare equal.
    """
    text = str(value).strip().lower()
    if text.startswith("mailto:"):
        text = text[len("mailto:"):]
    return text


@dataclass(frozen=True)
class Identity:
    """An authenticated sender: the addresses they may speak for."""

    addresses: frozenset

    @classmethod
    def of(cls, *addresses: str) -> Identity:
        """Create an identity from one or more addresses."""
        if not addresses:
            raise ValueError("an identity needs at least one address")
        return cls(frozenset(normalize_address(a) for a in addresses))

    def matches(self, address: Any) -> bool:
        """Check whether this identity may speak for ``address``."""
        return normalize_address(address) in self.addresses


class Outcome(Enum):
    """The result of merging one iTIP message."""

    APPLIED = "applied"
    IGNORED = "ignored"
    CONFLICT = "conflict"
    REJECTED = "rejected"


@dataclass(frozen=True)
class MergeReport:
    """Explains how a single message was handled.

    ``outcome`` is one of:

    * ``APPLIED`` -- the message changed the stored state.
    * ``IGNORED`` -- the message was valid but obsolete, a duplicate
      or not applicable.
    * ``CONFLICT`` -- the message competes with the stored version and
      was not applied; the stored state is kept.
    * ``REJECTED`` -- the message failed authentication, authorization
      or structural validation.
    """

    outcome: Outcome
    reason: str
    method: str | None = None
    uid: str | None = None
    recurrence_id: Any = None
    sequence: int | None = None

    def __str__(self) -> str:
        """Render the report as one human-readable line."""
        return f"{self.outcome.value}: {self.reason}"


@dataclass(frozen=True)
class ReplyRecord:
    """One attendee's response to a specific invitation version."""

    attendee: str
    sequence: int
    dtstamp: datetime | None
    partstat: str | None
    recurrence_id: Any = None


@dataclass(frozen=True)
class SeriesState:
    """Everything known about one UID (a whole event series).

    ``master`` and ``overrides`` are private copies of the components
    exactly as the organizer sent them; attendee responses live in
    ``replies`` and are only overlaid when rendering via
    :meth:`CalendarState.to_calendar`.
    """

    uid: str
    organizer: str
    sequence: int
    dtstamp: datetime | None
    master: Component | None = None
    overrides: Mapping[Any, Component] = field(default_factory=dict)
    cancelled: bool = False
    cancelled_instances: frozenset = field(default_factory=frozenset)
    replies: Mapping[tuple, ReplyRecord] = field(default_factory=dict)


@dataclass(frozen=True)
class _Message:
    """A validated, extracted iTIP message."""

    method: str
    uid: str
    sequence: int
    dtstamp: datetime | None
    organizer: str | None
    master: Component | None
    overrides: tuple
    attendees: tuple

    @property
    def recurrence_id(self) -> Any:
        """The single RECURRENCE-ID of this message, if any."""
        if self.master is None and len(self.overrides) == 1:
            return self.overrides[0][0]
        return None


def _report(outcome: Outcome, reason: str, msg: _Message) -> MergeReport:
    return MergeReport(
        outcome=outcome,
        reason=reason,
        method=msg.method,
        uid=msg.uid,
        recurrence_id=msg.recurrence_id,
        sequence=msg.sequence,
    )


def _sequence_of(component: Component) -> int:
    try:
        return int(component.decoded("SEQUENCE", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _attendee_properties(component: Component) -> list:
    # Do not use the ``attendees`` property here: it mutates the
    # component when ATTENDEE is missing, and inputs must stay
    # untouched.
    raw = component.get("ATTENDEE")
    if raw is None:
        return []
    return list(raw) if isinstance(raw, list) else [raw]


def _partstat_of(attendee: Any) -> str | None:
    params = getattr(attendee, "params", None)
    if params is None:
        return None
    value = params.get("PARTSTAT")
    return None if value is None else str(value)


def _is_newer(candidate: datetime | None, current: datetime | None) -> bool:
    if current is None:
        return True
    if candidate is None:
        return False
    try:
        return candidate > current
    except TypeError:  # naive vs. aware cannot be ordered
        return False


def _extract_message(message: Any) -> _Message | MergeReport:
    def reject(
        reason: str,
        method: str | None = None,
        uid: str | None = None,
        sequence: int | None = None,
    ) -> MergeReport:
        return MergeReport(Outcome.REJECTED, reason, method, uid, None, sequence)

    if not isinstance(message, Component):
        return reject(f"expected a parsed Calendar, got {type(message).__name__}")
    method_prop = message.get("METHOD")
    if method_prop is None:
        return reject("message has no METHOD")
    method = str(method_prop).upper()
    if method not in _METHODS:
        return reject(f"unsupported METHOD {method!r}", method)
    vevents = message.walk("VEVENT")
    if not vevents:
        return reject("message contains no VEVENT", method)
    uids = set()
    for vevent in vevents:
        uid = vevent.get("UID")
        if uid is None:
            return reject("VEVENT without UID", method)
        uids.add(str(uid))
    if len(uids) != 1:
        return reject("message mixes VEVENTs with different UIDs", method)
    uid = uids.pop()
    sequences = {_sequence_of(vevent) for vevent in vevents}
    if len(sequences) != 1:
        return reject("VEVENTs disagree on SEQUENCE", method, uid)
    sequence = sequences.pop()
    master = None
    overrides = []
    for vevent in vevents:
        if vevent.get("RECURRENCE-ID") is None:
            if master is not None:
                return reject(
                    "message has more than one master VEVENT", method, uid, sequence
                )
            master = vevent
        else:
            overrides.append((vevent.decoded("RECURRENCE-ID"), vevent))
    components = ([(None, master)] if master is not None else []) + overrides
    dtstamp = next(
        (
            stamp
            for stamp in (v.decoded("DTSTAMP", None) for _, v in components)
            if stamp is not None
        ),
        None,
    )
    organizer = next(
        (
            str(v.get("ORGANIZER"))
            for _, v in components
            if v.get("ORGANIZER") is not None
        ),
        None,
    )
    attendees = tuple(
        (key, attendee)
        for key, vevent in components
        for attendee in _attendee_properties(vevent)
    )
    return _Message(
        method=method,
        uid=uid,
        sequence=sequence,
        dtstamp=dtstamp,
        organizer=organizer,
        master=master,
        overrides=tuple(overrides),
        attendees=attendees,
    )


def _copy_component(component: Component | None) -> Component | None:
    return None if component is None else copy.deepcopy(component)


def _copy_overrides(overrides: Iterable) -> dict:
    return {key: copy.deepcopy(component) for key, component in overrides}


def _invited_attendees(*components: Component | None) -> set:
    invited = set()
    for component in components:
        if component is None:
            continue
        for attendee in _attendee_properties(component):
            invited.add(normalize_address(attendee))
    return invited


def _same_version(series: SeriesState, msg: _Message) -> bool:
    """Check whether the message restates the stored components."""

    def signature(component: Component | None) -> bytes | None:
        return None if component is None else component.to_ical()

    if msg.master is not None and signature(series.master) != signature(msg.master):
        return False
    return all(
        signature(series.overrides.get(key)) == signature(component)
        for key, component in msg.overrides
    )


def _record(msg: _Message, recurrence_id: Any, attendee: Any) -> ReplyRecord:
    return ReplyRecord(
        attendee=normalize_address(attendee),
        sequence=msg.sequence,
        dtstamp=msg.dtstamp,
        partstat=_partstat_of(attendee),
        recurrence_id=recurrence_id,
    )


def _overlay_replies(
    component: Component, replies: Mapping[tuple, ReplyRecord], recurrence_id: Any
) -> None:
    records = {
        attendee: record
        for (rid, attendee), record in replies.items()
        if rid == recurrence_id
    }
    if not records:
        return
    for attendee in _attendee_properties(component):
        record = records.get(normalize_address(attendee))
        if record is not None and record.partstat is not None:
            attendee.params["PARTSTAT"] = record.partstat


def _ensure_identity(sender: Any) -> Identity:
    if isinstance(sender, Identity):
        return sender
    if isinstance(sender, str):
        return Identity.of(sender)
    return Identity.of(*sender)


@dataclass(frozen=True)
class CalendarState:
    """An immutable, purely in-memory calendar built from iTIP messages.

    Use :meth:`apply` to merge a single message and
    :meth:`to_calendar` to render the current schedule.
    ``pending_replies`` holds replies that arrived before their
    invitation; they are applied once a matching ``REQUEST`` shows up.
    """

    series: Mapping[str, SeriesState] = field(default_factory=dict)
    pending_replies: Mapping[tuple, ReplyRecord] = field(default_factory=dict)

    def apply(
        self, message: Calendar, sender: Identity | str | Iterable[str]
    ) -> tuple[CalendarState, MergeReport]:
        """Merge one iTIP message.

        ``message`` is the parsed iTIP calendar, ``sender`` the
        authenticated identity of whoever sent it.  Neither this state
        nor the message is modified; a new state and a report are
        returned.
        """
        extracted = _extract_message(message)
        if isinstance(extracted, MergeReport):
            return self, extracted
        authenticated = _ensure_identity(sender)
        handler = {
            "REQUEST": self._apply_request,
            "CANCEL": self._apply_cancel,
            "REPLY": self._apply_reply,
        }[extracted.method]
        return handler(extracted, authenticated)

    def to_calendar(self) -> Calendar:
        """Render the current schedule as a new :class:`Calendar`.

        Cancelled series and cancelled instances are omitted; accepted
        replies are overlaid onto copies of the organizer's
        components, so only attendee response fields ever change.
        """
        calendar = Calendar()
        calendar.add("VERSION", "2.0")
        calendar.add("PRODID", "-//icalendar//itip//EN")
        for series in self.series.values():
            if series.cancelled:
                continue
            if series.master is not None:
                master = copy.deepcopy(series.master)
                _overlay_replies(master, series.replies, None)
                calendar.add_component(master)
            for key, override in series.overrides.items():
                if key in series.cancelled_instances:
                    continue
                component = copy.deepcopy(override)
                _overlay_replies(component, series.replies, key)
                calendar.add_component(component)
        return calendar

    def _with_series(self, series: SeriesState) -> CalendarState:
        updated = dict(self.series)
        updated[series.uid] = series
        return replace(self, series=updated)

    def _with_pending(self, pending: Mapping[tuple, ReplyRecord]) -> CalendarState:
        return replace(self, pending_replies=pending)

    def _drain_pending(
        self, series: SeriesState
    ) -> tuple[dict, dict]:
        """Collect held replies that apply to a newly stored series."""
        invited = _invited_attendees(series.master, *series.overrides.values())
        replies = {}
        pending = dict(self.pending_replies)
        for key, record in list(pending.items()):
            uid, recurrence_id, attendee = key
            if uid != series.uid:
                continue
            del pending[key]
            if record.sequence >= series.sequence and attendee in invited:
                replies[(recurrence_id, attendee)] = record
        return replies, pending

    def _apply_request(
        self, msg: _Message, sender: Identity
    ) -> tuple[CalendarState, MergeReport]:
        if msg.organizer is None:
            return self, _report(
                Outcome.REJECTED, "REQUEST without ORGANIZER cannot be authenticated",
                msg,
            )
        if not sender.matches(msg.organizer):
            return self, _report(
                Outcome.REJECTED,
                f"sender is not the organizer ({msg.organizer})",
                msg,
            )
        organizer = normalize_address(msg.organizer)
        current = self.series.get(msg.uid)
        if current is None:
            series = SeriesState(
                uid=msg.uid,
                organizer=organizer,
                sequence=msg.sequence,
                dtstamp=msg.dtstamp,
                master=_copy_component(msg.master),
                overrides=_copy_overrides(msg.overrides),
            )
            replies, pending = self._drain_pending(series)
            series = replace(series, replies=replies)
            state = self._with_series(series)._with_pending(pending)
            return state, _report(Outcome.APPLIED, "new invitation stored", msg)
        if current.organizer != organizer:
            return self, _report(
                Outcome.REJECTED, "ORGANIZER does not match the stored event", msg
            )
        if msg.sequence < current.sequence:
            return self, _report(
                Outcome.IGNORED,
                f"obsolete SEQUENCE {msg.sequence}; "
                f"stored version is {current.sequence}",
                msg,
            )
        if msg.sequence == current.sequence:
            if _same_version(current, msg):
                return self, _report(
                    Outcome.IGNORED, "retransmission of the stored version", msg
                )
            return self, _report(
                Outcome.CONFLICT,
                f"SEQUENCE {msg.sequence} already stored with different content",
                msg,
            )
        overrides = dict(current.overrides)
        overrides.update(_copy_overrides(msg.overrides))
        # Replies to older sequences refer to an invitation that no
        # longer applies and must not overwrite the new version.
        replies = {
            key: record
            for key, record in current.replies.items()
            if record.sequence >= msg.sequence
        }
        series = replace(
            current,
            sequence=msg.sequence,
            dtstamp=msg.dtstamp,
            master=_copy_component(msg.master)
            if msg.master is not None
            else current.master,
            overrides=overrides,
            cancelled=False,
            cancelled_instances=frozenset(),
            replies=replies,
        )
        reason = f"invitation updated to SEQUENCE {msg.sequence}"
        if current.cancelled:
            reason = f"cancelled invitation restored with SEQUENCE {msg.sequence}"
        return self._with_series(series), _report(Outcome.APPLIED, reason, msg)

    def _apply_cancel(
        self, msg: _Message, sender: Identity
    ) -> tuple[CalendarState, MergeReport]:
        if msg.organizer is None:
            return self, _report(
                Outcome.REJECTED, "CANCEL without ORGANIZER cannot be authenticated",
                msg,
            )
        if not sender.matches(msg.organizer):
            return self, _report(
                Outcome.REJECTED,
                f"sender is not the organizer ({msg.organizer})",
                msg,
            )
        current = self.series.get(msg.uid)
        if current is None:
            return self, _report(
                Outcome.IGNORED, "no such event; nothing to cancel", msg
            )
        if current.organizer != normalize_address(msg.organizer):
            return self, _report(
                Outcome.REJECTED, "ORGANIZER does not match the stored event", msg
            )
        if msg.sequence < current.sequence:
            return self, _report(
                Outcome.IGNORED,
                f"stale CANCEL with SEQUENCE {msg.sequence}; "
                f"stored version is {current.sequence}",
                msg,
            )
        sequence = max(current.sequence, msg.sequence)
        dtstamp = msg.dtstamp if msg.dtstamp is not None else current.dtstamp
        if not msg.overrides:
            series = replace(
                current,
                sequence=sequence,
                dtstamp=dtstamp,
                cancelled=True,
                overrides={},
                cancelled_instances=frozenset(),
                replies={},
            )
            return self._with_series(series), _report(
                Outcome.APPLIED, "whole series cancelled", msg
            )
        cancelled_keys = {key for key, _ in msg.overrides}
        overrides = {
            key: component
            for key, component in current.overrides.items()
            if key not in cancelled_keys
        }
        replies = {
            key: record
            for key, record in current.replies.items()
            if key[0] not in cancelled_keys
        }
        series = replace(
            current,
            sequence=sequence,
            dtstamp=dtstamp,
            overrides=overrides,
            cancelled_instances=frozenset(
                set(current.cancelled_instances) | cancelled_keys
            ),
            replies=replies,
        )
        return self._with_series(series), _report(
            Outcome.APPLIED, f"{len(cancelled_keys)} instance(s) cancelled", msg
        )

    def _apply_reply(
        self, msg: _Message, sender: Identity
    ) -> tuple[CalendarState, MergeReport]:
        if not msg.attendees:
            return self, _report(Outcome.REJECTED, "REPLY without ATTENDEE", msg)
        claimed = {normalize_address(attendee) for _, attendee in msg.attendees}
        for address in sorted(claimed):
            if address not in sender.addresses:
                return self, _report(
                    Outcome.REJECTED, f"sender may not reply as {address}", msg
                )
        if any(_partstat_of(attendee) is None for _, attendee in msg.attendees):
            return self, _report(Outcome.REJECTED, "REPLY without PARTSTAT", msg)
        current = self.series.get(msg.uid)
        if current is None:
            records = {
                (msg.uid, key, normalize_address(attendee)): _record(
                    msg, key, attendee
                )
                for key, attendee in msg.attendees
            }
            pending = {**self.pending_replies, **records}
            return self._with_pending(pending), _report(
                Outcome.APPLIED, "reply held until the invitation arrives", msg
            )
        if current.cancelled:
            return self, _report(
                Outcome.IGNORED, "invitation was cancelled", msg
            )
        invited = _invited_attendees(current.master, *current.overrides.values())
        for address in sorted(claimed):
            if address not in invited:
                return self, _report(
                    Outcome.REJECTED, f"{address} is not invited to this event", msg
                )
        if msg.sequence < current.sequence:
            return self, _report(
                Outcome.IGNORED,
                f"reply to superseded SEQUENCE {msg.sequence}; "
                f"current invitation is {current.sequence}",
                msg,
            )
        replies = dict(current.replies)
        applied = 0
        for key, attendee in msg.attendees:
            address = normalize_address(attendee)
            record_key = (key, address)
            existing = replies.get(record_key)
            if existing is not None and (
                existing.sequence > msg.sequence
                or (
                    existing.sequence == msg.sequence
                    and not _is_newer(msg.dtstamp, existing.dtstamp)
                )
            ):
                continue
            replies[record_key] = _record(msg, key, attendee)
            applied += 1
        if not applied:
            return self, _report(
                Outcome.IGNORED, "superseded by a newer reply", msg
            )
        series = replace(current, replies=replies)
        return self._with_series(series), _report(
            Outcome.APPLIED, f"reply recorded for {applied} attendee(s)", msg
        )


def process_messages(
    messages: Iterable[tuple[Calendar, Identity | str | Iterable[str]]],
    initial: CalendarState | None = None,
) -> tuple[CalendarState, list[MergeReport]]:
    """Merge a sequence of ``(message, sender)`` pairs in order.

    Returns the final state and one report per message, so callers can
    audit why each message was or was not applied.
    """
    state = initial if initial is not None else CalendarState()
    reports = []
    for message, sender in messages:
        state, report = state.apply(message, sender)
        reports.append(report)
    return state, reports
