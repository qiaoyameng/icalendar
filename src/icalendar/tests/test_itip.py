"""Tests for the pure in-memory iTIP merger (icalendar.itip)."""

from icalendar import Calendar
from icalendar.itip import (
    CalendarState,
    Identity,
    Outcome,
    process_messages,
)

ORGANIZER = "mailto:boss@example.com"
ALICE = "mailto:alice@example.com"
BOB = "mailto:bob@example.com"
MALLORY = "mailto:mallory@example.com"

BOSS = Identity.of(ORGANIZER)
ALICE_ID = Identity.of(ALICE)
BOB_ID = Identity.of(BOB)
MALLORY_ID = Identity.of(MALLORY)


def _ics(*lines):
    return Calendar.from_ical("\n".join(line for line in lines if line))


def _request(
    uid="meet-1",
    sequence=0,
    dtstamp="20260101T090000Z",
    summary="Kickoff",
    start="20260201T100000Z",
    end="20260201T110000Z",
    organizer=ORGANIZER,
    attendees=(ALICE, BOB),
):
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//test//itip//EN",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"SEQUENCE:{sequence}",
        f"DTSTAMP:{dtstamp}",
        f"DTSTART:{start}",
        f"DTEND:{end}",
        f"SUMMARY:{summary}",
        f"ORGANIZER:{organizer}",
    ]
    lines += [f"ATTENDEE;PARTSTAT=NEEDS-ACTION:{a}" for a in attendees]
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return _ics(*lines)


def _reply(
    uid="meet-1",
    sequence=0,
    dtstamp="20260102T090000Z",
    attendee=ALICE,
    partstat="ACCEPTED",
    rid=None,
):
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//test//itip//EN",
        "METHOD:REPLY",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"SEQUENCE:{sequence}",
        f"DTSTAMP:{dtstamp}",
        f"ORGANIZER:{ORGANIZER}",
    ]
    if rid is not None:
        lines.append(f"RECURRENCE-ID:{rid}")
    lines.append(f"ATTENDEE;PARTSTAT={partstat}:{attendee}")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return _ics(*lines)


def _cancel(uid="meet-2", sequence=1, dtstamp="20260106T090000Z", rid=None):
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//test//itip//EN",
        "METHOD:CANCEL",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"SEQUENCE:{sequence}",
        f"DTSTAMP:{dtstamp}",
        f"ORGANIZER:{ORGANIZER}",
    ]
    if rid is not None:
        lines.append(f"RECURRENCE-ID:{rid}")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return _ics(*lines)


def _series_request():
    """A recurring event with one overridden instance."""
    return _ics(
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//test//itip//EN",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        "UID:meet-2",
        "SEQUENCE:0",
        "DTSTAMP:20260101T100000Z",
        "DTSTART:20260301T100000Z",
        "DTEND:20260301T110000Z",
        "RRULE:FREQ=WEEKLY;COUNT=4",
        "SUMMARY:Weekly sync",
        f"ORGANIZER:{ORGANIZER}",
        f"ATTENDEE;PARTSTAT=NEEDS-ACTION:{ALICE}",
        "END:VEVENT",
        "BEGIN:VEVENT",
        "UID:meet-2",
        "SEQUENCE:0",
        "DTSTAMP:20260101T100000Z",
        "RECURRENCE-ID:20260308T100000Z",
        "DTSTART:20260308T120000Z",
        "DTEND:20260308T130000Z",
        "SUMMARY:Weekly sync (late)",
        f"ORGANIZER:{ORGANIZER}",
        f"ATTENDEE;PARTSTAT=NEEDS-ACTION:{ALICE}",
        "END:VEVENT",
        "END:VCALENDAR",
    )


def _events(state, uid):
    return [
        c for c in state.to_calendar().walk("VEVENT") if str(c.get("UID")) == uid
    ]


def _partstat(component, address):
    raw = component.get("ATTENDEE")
    attendees = raw if isinstance(raw, list) else [raw]
    for attendee in attendees:
        if str(attendee).lower() == address.lower():
            return str(attendee.params.get("PARTSTAT"))
    msg = f"{address} not found"
    raise AssertionError(msg)


def test_request_from_organizer_is_applied():
    state, report = CalendarState().apply(_request(), BOSS)
    assert report.outcome is Outcome.APPLIED
    assert report.method == "REQUEST"
    assert report.uid == "meet-1"
    events = _events(state, "meet-1")
    assert len(events) == 1
    assert str(events[0]["SUMMARY"]) == "Kickoff"


def test_request_from_non_organizer_is_rejected():
    state, report = CalendarState().apply(_request(), ALICE_ID)
    assert report.outcome is Outcome.REJECTED
    assert "not the organizer" in report.reason
    assert _events(state, "meet-1") == []


def test_attendee_cannot_modify_event():
    state, _ = CalendarState().apply(_request(), BOSS)
    state, report = state.apply(
        _request(sequence=9, summary="Hijacked"), ALICE_ID
    )
    assert report.outcome is Outcome.REJECTED
    assert str(_events(state, "meet-1")[0]["SUMMARY"]) == "Kickoff"


def test_organizer_mismatch_is_rejected():
    state, _ = CalendarState().apply(_request(), BOSS)
    other = Identity.of("mailto:boss2@example.com")
    state, report = state.apply(
        _request(sequence=1, organizer="mailto:boss2@example.com"), other
    )
    assert report.outcome is Outcome.REJECTED
    assert "ORGANIZER does not match" in report.reason


def test_reply_updates_only_own_response():
    state, _ = CalendarState().apply(_request(), BOSS)
    state, report = state.apply(_reply(partstat="ACCEPTED"), ALICE_ID)
    assert report.outcome is Outcome.APPLIED
    (event,) = _events(state, "meet-1")
    assert _partstat(event, ALICE) == "ACCEPTED"
    assert _partstat(event, BOB) == "NEEDS-ACTION"


def test_reply_for_someone_else_is_rejected():
    state, _ = CalendarState().apply(_request(), BOSS)
    state, report = state.apply(_reply(attendee=BOB), ALICE_ID)
    assert report.outcome is Outcome.REJECTED
    assert "may not reply as" in report.reason
    (event,) = _events(state, "meet-1")
    assert _partstat(event, BOB) == "NEEDS-ACTION"


def test_reply_from_stranger_is_rejected():
    state, _ = CalendarState().apply(_request(), BOSS)
    state, report = state.apply(_reply(attendee=MALLORY), MALLORY_ID)
    assert report.outcome is Outcome.REJECTED
    assert "not invited" in report.reason


def test_stale_request_is_ignored():
    state, _ = CalendarState().apply(_request(sequence=2), BOSS)
    state, report = state.apply(_request(sequence=1), BOSS)
    assert report.outcome is Outcome.IGNORED
    assert "obsolete SEQUENCE" in report.reason
    assert state.series["meet-1"].sequence == 2


def test_retransmission_is_ignored():
    state, _ = CalendarState().apply(_request(sequence=1), BOSS)
    state, report = state.apply(_request(sequence=1), BOSS)
    assert report.outcome is Outcome.IGNORED
    assert "retransmission" in report.reason


def test_same_sequence_different_content_is_a_conflict():
    state, _ = CalendarState().apply(
        _request(sequence=1, summary="Version A"), BOSS
    )
    new_state, report = state.apply(
        _request(sequence=1, dtstamp="20260102T090000Z", summary="Version B"),
        BOSS,
    )
    assert report.outcome is Outcome.CONFLICT
    assert new_state is state
    assert str(_events(state, "meet-1")[0]["SUMMARY"]) == "Version A"


def test_series_cancel():
    state, _ = CalendarState().apply(_series_request(), BOSS)
    state, report = state.apply(_cancel(sequence=1), BOSS)
    assert report.outcome is Outcome.APPLIED
    assert "series cancelled" in report.reason
    assert _events(state, "meet-2") == []


def test_instance_cancel():
    state, _ = CalendarState().apply(_series_request(), BOSS)
    state, report = state.apply(
        _cancel(sequence=1, rid="20260308T100000Z"), BOSS
    )
    assert report.outcome is Outcome.APPLIED
    assert "instance" in report.reason
    events = _events(state, "meet-2")
    assert len(events) == 1
    assert events[0].get("RECURRENCE-ID") is None


def test_cancel_from_non_organizer_is_rejected():
    state, _ = CalendarState().apply(_series_request(), BOSS)
    state, report = state.apply(_cancel(sequence=1), ALICE_ID)
    assert report.outcome is Outcome.REJECTED
    assert len(_events(state, "meet-2")) == 2


def test_stale_cancel_is_ignored():
    state, _ = CalendarState().apply(_request(sequence=3), BOSS)
    state, report = state.apply(
        _cancel(uid="meet-1", sequence=1), BOSS
    )
    assert report.outcome is Outcome.IGNORED
    assert "stale CANCEL" in report.reason
    assert len(_events(state, "meet-1")) == 1


def test_cancel_of_unknown_event_is_ignored():
    state, report = CalendarState().apply(_cancel(), BOSS)
    assert report.outcome is Outcome.IGNORED
    assert "nothing to cancel" in report.reason


def test_stale_reply_does_not_overwrite_newer_invitation():
    state, _ = CalendarState().apply(_request(sequence=0), BOSS)
    state, _ = state.apply(
        _request(sequence=1, dtstamp="20260103T090000Z"), BOSS
    )
    state, report = state.apply(_reply(sequence=0, partstat="DECLINED"), ALICE_ID)
    assert report.outcome is Outcome.IGNORED
    assert "superseded" in report.reason
    (event,) = _events(state, "meet-1")
    assert _partstat(event, ALICE) == "NEEDS-ACTION"


def test_new_request_drops_old_replies():
    state, _ = CalendarState().apply(_request(sequence=0), BOSS)
    state, report = state.apply(_reply(sequence=0), ALICE_ID)
    assert report.outcome is Outcome.APPLIED
    (event,) = _events(state, "meet-1")
    assert _partstat(event, ALICE) == "ACCEPTED"
    state, report = state.apply(
        _request(sequence=1, dtstamp="20260103T090000Z"), BOSS
    )
    assert report.outcome is Outcome.APPLIED
    assert state.series["meet-1"].replies == {}
    (event,) = _events(state, "meet-1")
    assert _partstat(event, ALICE) == "NEEDS-ACTION"


def test_reply_before_request_is_held_and_applied():
    state, report = CalendarState().apply(_reply(sequence=1), ALICE_ID)
    assert report.outcome is Outcome.APPLIED
    assert "held" in report.reason
    state, _ = state.apply(
        _request(sequence=1, dtstamp="20260103T090000Z"), BOSS
    )
    assert state.pending_replies == {}
    (event,) = _events(state, "meet-1")
    assert _partstat(event, ALICE) == "ACCEPTED"


def test_older_reply_does_not_override_newer_reply():
    state, _ = CalendarState().apply(_request(sequence=0), BOSS)
    state, _ = state.apply(
        _reply(dtstamp="20260105T090000Z", partstat="DECLINED"), ALICE_ID
    )
    state, report = state.apply(
        _reply(dtstamp="20260102T090000Z", partstat="ACCEPTED"), ALICE_ID
    )
    assert report.outcome is Outcome.IGNORED
    (event,) = _events(state, "meet-1")
    assert _partstat(event, ALICE) == "DECLINED"


def test_reply_to_cancelled_series_is_ignored():
    state, _ = CalendarState().apply(_request(sequence=0), BOSS)
    state, _ = state.apply(_cancel(uid="meet-1", sequence=1), BOSS)
    state, report = state.apply(_reply(sequence=1), ALICE_ID)
    assert report.outcome is Outcome.IGNORED
    assert "cancelled" in report.reason


def test_message_without_method_is_rejected():
    message = _ics(
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "BEGIN:VEVENT",
        "UID:meet-1",
        "DTSTART:20260201T100000Z",
        "END:VEVENT",
        "END:VCALENDAR",
    )
    state, report = CalendarState().apply(message, BOSS)
    assert report.outcome is Outcome.REJECTED
    assert "no METHOD" in report.reason


def test_unsupported_method_is_rejected():
    message = _ics(
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        "UID:meet-1",
        "DTSTART:20260201T100000Z",
        "END:VEVENT",
        "END:VCALENDAR",
    )
    state, report = CalendarState().apply(message, BOSS)
    assert report.outcome is Outcome.REJECTED
    assert "unsupported METHOD" in report.reason


def test_inputs_and_states_are_not_mutated():
    message = _request()
    before = message.to_ical()
    empty = CalendarState()
    state, _ = empty.apply(message, BOSS)
    assert message.to_ical() == before
    assert empty.to_calendar().to_ical() == CalendarState().to_calendar().to_ical()
    # Mutating the input afterwards must not leak into the state.
    message.walk("VEVENT")[0]["SUMMARY"] = "Hijacked"
    assert b"Hijacked" not in state.to_calendar().to_ical()
    # Applying a reply returns a new state and leaves the old one alone.
    state_before = state.to_calendar().to_ical()
    new_state, _ = state.apply(_reply(), ALICE_ID)
    assert new_state is not state
    assert state.to_calendar().to_ical() == state_before


def test_shuffled_message_stream_converges():
    """A shuffled stream of messages still produces the right schedule.

    The stream contains a reply that arrives before its invitation, an
    updated invitation, stale replies to the old version, a forged
    request, a retransmission, and instance/series cancellations.
    """
    moved = {
        "sequence": 1,
        "dtstamp": "20260103T090000Z",
        "summary": "Kickoff (moved)",
        "start": "20260201T110000Z",
        "end": "20260201T120000Z",
    }
    messages = [
        # Bob's reply to the update arrives before the update itself.
        (_reply(sequence=1, dtstamp="20260105T090000Z", attendee=BOB), BOB_ID),
        # The updated invitation (we never saw SEQUENCE 0 first).
        (_request(**moved), BOSS),
        # Alice replied to the old version: stale by now.
        (_reply(sequence=0, dtstamp="20260102T090000Z", attendee=ALICE), ALICE_ID),
        # A recurring event with an overridden instance.
        (_series_request(), BOSS),
        # Bob's reply to the old version: also stale.
        (_reply(
            sequence=0, dtstamp="20260104T090000Z",
            attendee=BOB, partstat="DECLINED",
        ), BOB_ID),
        # The original invitation arrives much too late.
        (_request(), BOSS),
        # Alice tries to push her own version of the meeting.
        (_request(sequence=5, summary="Hijacked"), ALICE_ID),
        # One instance of the recurring event is cancelled.
        (_cancel(sequence=1, rid="20260308T100000Z"), BOSS),
        # Then the whole recurring series is cancelled.
        (_cancel(sequence=2), BOSS),
        # The updated invitation is retransmitted.
        (_request(**moved), BOSS),
    ]
    state, reports = process_messages(messages)

    assert [report.outcome for report in reports] == [
        Outcome.APPLIED,   # Bob's early reply is held
        Outcome.APPLIED,   # updated invitation stored, held reply applied
        Outcome.IGNORED,   # Alice's reply targets a superseded version
        Outcome.APPLIED,   # recurring series stored
        Outcome.IGNORED,   # Bob's old reply is stale
        Outcome.IGNORED,   # original invitation is obsolete
        Outcome.REJECTED,  # Alice is not the organizer
        Outcome.APPLIED,   # single instance cancelled
        Outcome.APPLIED,   # whole series cancelled
        Outcome.IGNORED,   # retransmission of the stored version
    ]
    assert "held" in reports[0].reason
    assert "superseded" in reports[2].reason
    assert "obsolete" in reports[5].reason
    assert "not the organizer" in reports[6].reason
    assert "retransmission" in reports[9].reason

    # meet-1 reflects the updated invitation; only Bob's fresh reply
    # survived, Alice's stale one did not.
    (event,) = _events(state, "meet-1")
    assert str(event["SUMMARY"]) == "Kickoff (moved)"
    assert _partstat(event, ALICE) == "NEEDS-ACTION"
    assert _partstat(event, BOB) == "ACCEPTED"
    # meet-2 was cancelled as a whole series.
    assert _events(state, "meet-2") == []
    assert state.pending_replies == {}
