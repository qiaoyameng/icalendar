"""Acceptance tests for the in-memory iTIP merger (:mod:`icalendar.itip`).

Two complementary scenarios are covered:

* a shuffled stream whose *disposition* (action + reason) for every message
  is independent of delivery order and whose calendar converges to one
  final state;
* a fixed-order life cycle that verifies the order sensitive steps
  (reschedule resets attendee responses, old replies cannot overwrite the
  new invitation, whole-series versus single-instance cancellation).
"""

from __future__ import annotations

import random
from collections import Counter
from datetime import datetime, timedelta

import pytest

from icalendar import Calendar, Event
from icalendar.itip import (
    AuthenticatedSender,
    ITIPAction,
    ITIPMerger,
    ITIPMessage,
    ITIPMethod,
    merge_itip_messages,
    message_from_calendar,
)
from icalendar.prop import vCalAddress, vRecur

ORG = "mailto:organizer@example.com"
ALICE = "mailto:alice@example.com"
BOB = "mailto:bob@example.com"
CAROL = "mailto:carol@example.com"
EVIL = "mailto:evil@example.com"

ORG_SENDER = AuthenticatedSender.from_address(ORG, "Organizer")
ALICE_SENDER = AuthenticatedSender.from_address(ALICE, "Alice")
BOB_SENDER = AuthenticatedSender.from_address(BOB, "Bob")
CAROL_SENDER = AuthenticatedSender.from_address(CAROL, "Carol")


def make_event(
    uid,
    start,
    *,
    end=None,
    organizer=ORG,
    attendees=(),
    sequence=0,
    dtstamp=datetime(2026, 10, 1, 9, 0),
    recurrence=None,
    recurrence_id=None,
    status=None,
):
    event = Event()
    event["UID"] = uid
    event["DTSTART"] = start
    event["DTEND"] = end or (start + timedelta(hours=1))
    if organizer is not None:
        event["ORGANIZER"] = vCalAddress(organizer)
    event["DTSTAMP"] = dtstamp
    for address, partstat in attendees:
        attendee = vCalAddress(address)
        attendee.params["PARTSTAT"] = partstat
        event.add("ATTENDEE", attendee)
    if sequence:
        event["SEQUENCE"] = sequence
    if recurrence is not None:
        event["RRULE"] = vRecur(freq=recurrence[0], count=recurrence[1])
    if recurrence_id is not None:
        event["RECURRENCE-ID"] = recurrence_id
    if status is not None:
        event["STATUS"] = status
    return event


def msg(method, event, sender, label):
    return ITIPMessage.create(method, event, sender, label)


def request(event, sender=ORG_SENDER, label=None):
    return msg(ITIPMethod.REQUEST, event, sender, label)


def reply(event, sender, label=None):
    return msg(ITIPMethod.REPLY, event, sender, label)


def cancel(event, sender=ORG_SENDER, label=None):
    return msg(ITIPMethod.CANCEL, event, sender, label)


def exdate_strings(event):
    value = event.get("EXDATE")
    if value is None:
        return ()
    values = value if isinstance(value, list) else [value]
    return tuple(sorted(str(item.dt) for entry in values for item in entry.dts))


def snapshot(calendar):
    items = []
    for event in calendar.events:
        recurrence_id = event.get("RECURRENCE-ID")
        items.append(
            (
                str(event.uid),
                str(recurrence_id) if recurrence_id is not None else "",
                str(event.get("STATUS", "CONFIRMED")),
                int(event.get("SEQUENCE", 0)),
                str(event.start),
                str(event.end),
                tuple(
                    sorted(
                        f"{address}={address.params.get('PARTSTAT')}"
                        for address in event.attendees
                    )
                ),
                exdate_strings(event),
            )
        )
    return tuple(sorted(items))


def seeded_calendar(uid="meeting-1", sequence=2, status="CONFIRMED", attendees=None):
    calendar = Calendar()
    attendees = attendees or (
        (ALICE, "NEEDS-ACTION"),
        (BOB, "NEEDS-ACTION"),
    )
    master = make_event(
        uid,
        datetime(2026, 11, 3, 14),
        end=datetime(2026, 11, 3, 15),
        attendees=attendees,
        sequence=sequence,
        dtstamp=datetime(2026, 10, 2, 9),
        status=status,
    )
    calendar.add_component(master)
    return calendar


# ---------------------------------------------------------------------------
# Order-independent shuffled stream
# ---------------------------------------------------------------------------


def build_order_independent_stream():
    """Messages whose action/reason is the same at every stream position."""
    obsolete_reply = make_event(
        "meeting-1",
        datetime(2026, 11, 2, 9),
        end=datetime(2026, 11, 2, 10),
        attendees=((ALICE, "ACCEPTED"),),
        sequence=1,
        dtstamp=datetime(2026, 10, 1, 12),
    )
    cancel_v2 = make_event(
        "meeting-1",
        datetime(2026, 11, 3, 14),
        end=datetime(2026, 11, 3, 15),
        attendees=(),
        sequence=2,
        dtstamp=datetime(2026, 10, 3, 9),
        status="CANCELLED",
    )
    spoofed = make_event(
        "meeting-1",
        datetime(2026, 11, 3, 20),
        end=datetime(2026, 11, 3, 21),
        attendees=((ALICE, "NEEDS-ACTION"),),
        sequence=3,
        dtstamp=datetime(2026, 10, 4, 9),
    )
    carol_reply = make_event(
        "meeting-1",
        datetime(2026, 11, 3, 14),
        end=datetime(2026, 11, 3, 15),
        attendees=((CAROL, "ACCEPTED"),),
        sequence=2,
        dtstamp=datetime(2026, 10, 4, 10),
    )
    forged_organizer_reply = make_event(
        "meeting-1",
        datetime(2026, 11, 3, 14),
        end=datetime(2026, 11, 3, 15),
        organizer=EVIL,
        attendees=((ALICE, "ACCEPTED"),),
        sequence=2,
        dtstamp=datetime(2026, 10, 4, 11),
    )
    return [
        (
            reply(obsolete_reply, ALICE_SENDER, "obsolete-reply"),
            ITIPAction.IGNORED,
            "obsolete-version",
        ),
        (cancel(cancel_v2, label="cancel-series"), ITIPAction.APPLIED, "cancelled"),
        (cancel(cancel_v2, label="cancel-series-retransmit"), ITIPAction.IGNORED, "duplicate"),
        (
            request(spoofed, sender=BOB_SENDER, label="attendee-spoofs-organizer"),
            ITIPAction.REJECTED,
            "sender-not-organizer",
        ),
        (
            reply(carol_reply, CAROL_SENDER, "uninvited-reply"),
            ITIPAction.REJECTED,
            "sender-uninvited",
        ),
        (
            reply(forged_organizer_reply, ALICE_SENDER, "forged-organizer-reply"),
            ITIPAction.REJECTED,
            "organizer-mismatch",
        ),
    ]


def test_shuffled_stream_is_explained_and_converges():
    stream = build_order_independent_stream()
    expected_by_label = {
        message.label: (action, reason) for message, action, reason in stream
    }
    cancel_labels = {"cancel-series", "cancel-series-retransmit"}
    expected_final = (
        (
            "meeting-1",
            "",
            "CANCELLED",
            2,
            "2026-11-03 14:00:00",
            "2026-11-03 15:00:00",
            (
                "mailto:alice@example.com=NEEDS-ACTION",
                "mailto:bob@example.com=NEEDS-ACTION",
            ),
            (),
        ),
    )
    for seed in range(12):
        ordered = stream.copy()
        random.Random(seed).shuffle(ordered)
        calendar, reports = merge_itip_messages(
            seeded_calendar(), [message for message, _, _ in ordered]
        )
        assert snapshot(calendar) == expected_final, seed
        for report in reports:
            if report.label in cancel_labels:
                # The two byte-identical CANCELs are indistinguishable: the
                # first delivery is applied, the retransmission is ignored.
                continue
            assert (report.action, report.reason) == expected_by_label[report.label], (
                seed,
                report.label,
            )
        cancel_reports = [
            report for report in reports if report.label in cancel_labels
        ]
        assert sorted((report.action.value, report.reason) for report in cancel_reports) == [
            ("applied", "cancelled"),
            ("ignored", "duplicate"),
        ]


def test_shuffled_stream_disposition_counts():
    stream = build_order_independent_stream()
    expected_actions = Counter(action for _, action, _ in stream)
    ordered = [message for message, _, _ in stream]
    random.Random(7).shuffle(ordered)
    _, reports = merge_itip_messages(seeded_calendar(), ordered)
    assert Counter(report.action for report in reports) == expected_actions


# ---------------------------------------------------------------------------
# Fixed order life cycle: reschedule resets responses, stale replies, cancels
# ---------------------------------------------------------------------------


def run_steps(steps, calendar=None):
    merger = ITIPMerger()
    current = calendar or Calendar()
    results = []
    for message in steps:
        current, report = merger.merge(current, message)
        results.append((report.action, report.reason))
    return current, results


def test_fixed_order_lifecycle_reasons_and_state():
    invitees = ((ALICE, "NEEDS-ACTION"), (BOB, "NEEDS-ACTION"))
    first = make_event(
        "lc",
        datetime(2026, 11, 2, 9),
        attendees=invitees,
        dtstamp=datetime(2026, 10, 1, 9),
    )
    alice_accept = make_event(
        "lc",
        datetime(2026, 11, 2, 9),
        attendees=((ALICE, "ACCEPTED"),),
        dtstamp=datetime(2026, 10, 1, 10),
    )
    alice_accept_dup = make_event(
        "lc",
        datetime(2026, 11, 2, 9),
        attendees=((ALICE, "ACCEPTED"),),
        dtstamp=datetime(2026, 10, 1, 10),
    )
    rescheduled = make_event(
        "lc",
        datetime(2026, 11, 3, 14),
        end=datetime(2026, 11, 3, 15),
        attendees=invitees,
        sequence=1,
        dtstamp=datetime(2026, 10, 2, 9),
    )
    rescheduled_dup = make_event(
        "lc",
        datetime(2026, 11, 3, 14),
        end=datetime(2026, 11, 3, 15),
        attendees=invitees,
        sequence=1,
        dtstamp=datetime(2026, 10, 2, 9),
    )
    stale_accept = make_event(
        "lc",
        datetime(2026, 11, 2, 9),
        attendees=((ALICE, "ACCEPTED"),),
        dtstamp=datetime(2026, 10, 1, 12),
    )
    new_accept = make_event(
        "lc",
        datetime(2026, 11, 3, 14),
        end=datetime(2026, 11, 3, 15),
        attendees=((ALICE, "ACCEPTED"),),
        sequence=1,
        dtstamp=datetime(2026, 10, 2, 10),
    )
    cancel_all = make_event(
        "lc",
        datetime(2026, 11, 3, 14),
        end=datetime(2026, 11, 3, 15),
        attendees=(),
        sequence=1,
        dtstamp=datetime(2026, 10, 3, 9),
        status="CANCELLED",
    )
    late_decline = make_event(
        "lc",
        datetime(2026, 11, 3, 14),
        end=datetime(2026, 11, 3, 15),
        attendees=((BOB, "DECLINED"),),
        sequence=1,
        dtstamp=datetime(2026, 10, 3, 10),
    )
    steps = [
        request(first, label="create"),
        reply(alice_accept, ALICE_SENDER, "accept"),
        reply(alice_accept_dup, ALICE_SENDER, "accept-dup"),
        request(rescheduled, label="reschedule"),
        request(rescheduled_dup, label="reschedule-dup"),
        reply(stale_accept, ALICE_SENDER, "stale-accept"),
        reply(new_accept, ALICE_SENDER, "new-accept"),
        cancel(cancel_all, label="cancel"),
        reply(late_decline, BOB_SENDER, "late-decline"),
    ]
    calendar, results = run_steps(steps)
    assert results == [
        (ITIPAction.APPLIED, "created"),
        (ITIPAction.APPLIED, "response-recorded"),
        (ITIPAction.IGNORED, "duplicate"),
        (ITIPAction.APPLIED, "rescheduled"),
        (ITIPAction.IGNORED, "duplicate"),
        (ITIPAction.IGNORED, "obsolete-version"),
        (ITIPAction.APPLIED, "response-recorded"),
        (ITIPAction.APPLIED, "cancelled"),
        (ITIPAction.IGNORED, "target-cancelled"),
    ]
    assert len(calendar.events) == 1
    master = calendar.events[0]
    assert str(master["STATUS"]) == "CANCELLED"
    assert int(master["SEQUENCE"]) == 1
    assert str(master.start) == "2026-11-03 14:00:00"


def test_reschedule_resets_attendee_partstat():
    invitees = ((ALICE, "ACCEPTED"), (BOB, "TENTATIVE"))
    original = seeded_calendar("reset", sequence=0, attendees=invitees)
    original.events[0]["DTSTART"] = datetime(2026, 11, 2, 9)
    original.events[0]["DTEND"] = datetime(2026, 11, 2, 10)
    original.events[0]["DTSTAMP"] = datetime(2026, 10, 1, 9)
    updated = make_event(
        "reset",
        datetime(2026, 11, 3, 9),
        attendees=(
            (ALICE, "NEEDS-ACTION"),
            (BOB, "NEEDS-ACTION"),
        ),
        sequence=1,
        dtstamp=datetime(2026, 10, 2, 9),
    )
    calendar, results = run_steps([request(updated, label="v2")], original)
    assert results == [(ITIPAction.APPLIED, "rescheduled")]
    partstats = {
        str(a).removeprefix("mailto:"): a.params["PARTSTAT"]
        for a in calendar.events[0].attendees
    }
    assert partstats == {"alice@example.com": "NEEDS-ACTION", "bob@example.com": "NEEDS-ACTION"}


def test_same_version_refresh_preserves_partstat():
    original = seeded_calendar(
        "refresh",
        sequence=1,
        attendees=((ALICE, "ACCEPTED"), (BOB, "DECLINED")),
    )
    refreshed = make_event(
        "refresh",
        datetime(2026, 11, 3, 14),
        end=datetime(2026, 11, 3, 15),
        attendees=(
            (ALICE, "NEEDS-ACTION"),
            (BOB, "NEEDS-ACTION"),
            (CAROL, "NEEDS-ACTION"),
        ),
        sequence=1,
        dtstamp=datetime(2026, 10, 2, 9),
        status="CONFIRMED",
    )
    refreshed["LOCATION"] = "Room 42"
    calendar, results = run_steps([request(refreshed, label="refresh")], original)
    assert results == [(ITIPAction.APPLIED, "refreshed")]
    partstats = {
        str(a).removeprefix("mailto:"): a.params["PARTSTAT"]
        for a in calendar.events[0].attendees
    }
    assert partstats == {
        "alice@example.com": "ACCEPTED",
        "bob@example.com": "DECLINED",
        "carol@example.com": "NEEDS-ACTION",
    }


# ---------------------------------------------------------------------------
# Recurring series: single-instance cancel/reply, conflict and reject cases
# ---------------------------------------------------------------------------


def recurring_calendar():
    calendar = Calendar()
    master = make_event(
        "daily",
        datetime(2026, 11, 2, 9),
        end=datetime(2026, 11, 2, 10),
        attendees=((ALICE, "NEEDS-ACTION"), (BOB, "NEEDS-ACTION")),
        recurrence=("DAILY", 3),
        dtstamp=datetime(2026, 10, 1, 9),
    )
    calendar.add_component(master)
    return calendar


def instance_event(recurrence_id, *, attendees=(), status=None, dtstamp=None, sequence=0):
    return make_event(
        "daily",
        recurrence_id,
        end=recurrence_id + timedelta(hours=1),
        attendees=attendees,
        sequence=sequence,
        dtstamp=dtstamp or datetime(2026, 10, 2, 9),
        recurrence_id=recurrence_id,
        status=status,
    )


def test_single_instance_cancel_and_reply_lifecycle():
    day2 = datetime(2026, 11, 3, 9)
    day1 = datetime(2026, 11, 2, 9)
    steps = [
        cancel(
            instance_event(day2, status="CANCELLED", dtstamp=datetime(2026, 10, 2, 9)),
            label="cancel-day2",
        ),
        cancel(
            instance_event(day2, status="CANCELLED", dtstamp=datetime(2026, 10, 2, 9)),
            label="cancel-day2-dup",
        ),
        reply(
            instance_event(
                day2,
                attendees=((ALICE, "DECLINED"),),
                dtstamp=datetime(2026, 10, 2, 10),
            ),
            ALICE_SENDER,
            "reply-cancelled-instance",
        ),
        reply(
            instance_event(
                day1,
                attendees=((ALICE, "ACCEPTED"),),
                dtstamp=datetime(2026, 10, 2, 11),
            ),
            ALICE_SENDER,
            "reply-day1",
        ),
        reply(
            instance_event(
                day1,
                attendees=((ALICE, "ACCEPTED"),),
                dtstamp=datetime(2026, 10, 2, 11),
            ),
            ALICE_SENDER,
            "reply-day1-dup",
        ),
    ]
    calendar, results = run_steps(steps, recurring_calendar())
    assert results == [
        (ITIPAction.APPLIED, "instance-cancelled"),
        (ITIPAction.IGNORED, "duplicate"),
        (ITIPAction.IGNORED, "target-cancelled"),
        (ITIPAction.APPLIED, "response-recorded"),
        (ITIPAction.IGNORED, "duplicate"),
    ]
    master, overrides = split_series(calendar, "daily")
    assert exdate_strings(master) == ("2026-11-03 09:00:00",)
    cancelled_override = overrides[day2]
    assert str(cancelled_override["STATUS"]) == "CANCELLED"
    reply_override = overrides[day1]
    assert reply_override.attendees[0].params["PARTSTAT"] == "ACCEPTED"
    assert str(reply_override["DTEND"]) == "2026-11-02 10:00:00"


def test_cancel_of_instance_outside_series_is_conflict():
    bogus = datetime(2026, 11, 9, 9)
    calendar, results = run_steps(
        [
            cancel(
                instance_event(bogus, status="CANCELLED"),
                label="cancel-bogus",
            )
        ],
        recurring_calendar(),
    )
    assert results == [(ITIPAction.CONFLICT, "unknown-instance")]


def test_instance_request_without_master_is_rejected():
    standalone = instance_event(datetime(2026, 11, 3, 9))
    calendar, results = run_steps(
        [request(standalone, label="orphan-instance")], Calendar()
    )
    assert results == [(ITIPAction.REJECTED, "master-missing")]


def test_whole_series_cancel_removes_overrides():
    day2 = datetime(2026, 11, 3, 9)
    calendar = recurring_calendar()
    calendar, _ = run_steps(
        [
            reply(
                instance_event(
                    day2,
                    attendees=((ALICE, "ACCEPTED"),),
                    dtstamp=datetime(2026, 10, 2, 8),
                ),
                ALICE_SENDER,
            )
        ],
        calendar,
    )
    assert len(calendar.events) == 2
    cancel_master = make_event(
        "daily",
        datetime(2026, 11, 2, 9),
        end=datetime(2026, 11, 2, 10),
        attendees=(),
        dtstamp=datetime(2026, 10, 3, 9),
        status="CANCELLED",
    )
    calendar, results = run_steps([cancel(cancel_master, label="cancel-series")], calendar)
    assert results == [(ITIPAction.APPLIED, "cancelled")]
    assert len(calendar.events) == 1
    assert str(calendar.events[0]["STATUS"]) == "CANCELLED"


def split_series(calendar, uid):
    master = next(event for event in calendar.events if str(event.uid) == uid and event.get("RECURRENCE-ID") is None)
    overrides = {
        event["RECURRENCE-ID"].dt
        if hasattr(event["RECURRENCE-ID"], "dt")
        else event["RECURRENCE-ID"]: event
        for event in calendar.events
        if str(event.uid) == uid and event.get("RECURRENCE-ID") is not None
    }
    return master, overrides


# ---------------------------------------------------------------------------
# Security, immutability and wire-format parsing
# ---------------------------------------------------------------------------


def test_request_address_in_body_is_not_authenticated():
    """ORGANIZER/ATTENDEE strings inside the payload prove nothing."""
    event = make_event(
        "sec",
        datetime(2026, 11, 3, 9),
        attendees=((CAROL, "NEEDS-ACTION"),),
        sequence=1,
    )
    # The body still claims ORG is organizer, but the authenticated sender is Carol.
    calendar, results = run_steps(
        [request(event, sender=CAROL_SENDER, label="body-says-org")],
        seeded_calendar("sec", sequence=0),
    )
    assert results == [(ITIPAction.REJECTED, "sender-not-organizer")]
    assert int(calendar.events[0].get("SEQUENCE", 2)) == 2


def test_reply_cannot_modify_meeting_properties():
    """A forged REQUEST inside a REPLY body never updates meeting data."""
    calendar = seeded_calendar("sec2", sequence=1)
    forged = make_event(
        "sec2",
        datetime(2026, 12, 1, 9),
        end=datetime(2026, 12, 1, 10),
        attendees=((ALICE, "ACCEPTED"), (BOB, "NEEDS-ACTION")),
        sequence=1,
        dtstamp=datetime(2026, 10, 5, 9),
    )
    forged["SUMMARY"] = "totally different meeting"
    calendar, results = run_steps(
        [reply(forged, ALICE_SENDER, "reply-with-changes")], calendar
    )
    assert results == [(ITIPAction.APPLIED, "response-recorded")]
    master = calendar.events[0]
    assert str(master.start) == "2026-11-03 14:00:00"
    assert master.get("SUMMARY") is None
    partstat_for = {
        str(address).removeprefix("mailto:"): address.params["PARTSTAT"]
        for address in master.attendees
    }
    assert partstat_for["bob@example.com"] == "NEEDS-ACTION"
    partstats = {
        str(a).removeprefix("mailto:"): a.params["PARTSTAT"]
        for a in master.attendees
    }
    assert partstats["alice@example.com"] == "ACCEPTED"


def test_inputs_are_not_mutated():
    import copy

    before_calendar = seeded_calendar("imm", sequence=1)
    before_calendar_bytes = before_calendar.to_ical()
    accept = make_event(
        "imm",
        datetime(2026, 11, 3, 14),
        end=datetime(2026, 11, 3, 15),
        attendees=((ALICE, "ACCEPTED"),),
        sequence=1,
        dtstamp=datetime(2026, 10, 3, 9),
    )
    before_event_bytes = accept.to_ical()
    message = reply(accept, ALICE_SENDER, "accept")
    new_calendar, report = ITIPMerger().merge(before_calendar, message)
    assert report.applied
    assert before_calendar.to_ical() == before_calendar_bytes
    assert accept.to_ical() == before_event_bytes
    assert message.event.to_ical() == before_event_bytes
    # Original calendar still has NEEDS-ACTION.
    assert before_calendar.events[0].attendees[0].params["PARTSTAT"] == "NEEDS-ACTION"
    assert new_calendar is not before_calendar


def test_message_from_wire_calendar():
    wire = Calendar()
    wire["METHOD"] = "REQUEST"
    event = make_event(
        "wire",
        datetime(2026, 11, 3, 9),
        attendees=((ALICE, "NEEDS-ACTION"),),
    )
    wire.add_component(event)
    text = wire.to_ical().decode()
    message = message_from_calendar(text, ORG_SENDER, label="from-wire")
    assert message.method is ITIPMethod.REQUEST
    assert message.label == "from-wire"
    calendar, report = ITIPMerger().merge(Calendar(), message)
    assert report.applied
    assert len(calendar.events) == 1


@pytest.mark.parametrize(
    ("method", "expected_reason"),
    [
        ("PUBLISH", "unsupported-method"),
    ],
)
def test_unsupported_method_is_rejected(method, expected_reason):
    wire = Calendar()
    wire["METHOD"] = method
    with pytest.raises(ValueError, match="unsupported iTIP METHOD"):
        message_from_calendar(wire, ORG_SENDER)


def test_reply_ahead_of_invitation_sequence_conflicts():
    calendar = seeded_calendar("ahead", sequence=1)
    future_reply = make_event(
        "ahead",
        datetime(2026, 11, 3, 14),
        end=datetime(2026, 11, 3, 15),
        attendees=((ALICE, "ACCEPTED"),),
        sequence=5,
        dtstamp=datetime(2026, 10, 9, 9),
    )
    calendar, results = run_steps(
        [reply(future_reply, ALICE_SENDER, "future")], calendar
    )
    assert results == [(ITIPAction.CONFLICT, "reply-newer-than-request")]


def test_unknown_uid_reply_and_cancel_are_ignored():
    reply_event = make_event(
        "ghost",
        datetime(2026, 11, 3, 9),
        attendees=((ALICE, "ACCEPTED"),),
    )
    cancel_event = make_event(
        "ghost",
        datetime(2026, 11, 3, 9),
        attendees=(),
        status="CANCELLED",
    )
    calendar, results = run_steps(
        [
            reply(reply_event, ALICE_SENDER, "ghost-reply"),
            cancel(cancel_event, label="ghost-cancel"),
        ],
        Calendar(),
    )
    assert results == [
        (ITIPAction.IGNORED, "unknown-uid"),
        (ITIPAction.IGNORED, "unknown-uid"),
    ]


def test_bare_email_address_is_normalized_to_mailto():
    sender = AuthenticatedSender.from_address("organizer@example.com")
    event = make_event("norm", datetime(2026, 11, 3, 9))
    calendar, report = ITIPMerger().merge(Calendar(), request(event, sender=sender))
    assert report.applied


def test_report_fields_are_populated():
    event = make_event("fields", datetime(2026, 11, 3, 9))
    _, report = ITIPMerger().merge(Calendar(), request(event, label="lab"))
    assert report.uid == "fields"
    assert report.method is ITIPMethod.REQUEST
    assert report.label == "lab"
    assert report.recurrence_id is None
    assert report.applied
