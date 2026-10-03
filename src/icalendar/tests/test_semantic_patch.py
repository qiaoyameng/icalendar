import json

import pytest

from icalendar import SemanticPatchConflict
from icalendar.parser.content_line import Contentlines
from icalendar.semantic_patch import (
    apply_semantic_patch,
    make_semantic_patch,
)


def ical(
    summary="Old",
    *,
    attendees=(),
    alarms=(("DISPLAY", "-PT15M"),),
    uid="event",
    recurrence_id=None,
):
    recurrence = f"RECURRENCE-ID:{recurrence_id}\r\n" if recurrence_id else ""
    attendee_lines = "".join(
        f"ATTENDEE{parameters}:mailto:{email}\r\n"
        for email, parameters in attendees
    )
    alarm_lines = "".join(
        "BEGIN:VALARM\r\n"
        f"ACTION:{action}\r\n"
        f"TRIGGER:{trigger}\r\n"
        "END:VALARM\r\n"
        for action, trigger in alarms
    )
    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:test\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"{recurrence}"
        "DTSTART;VALUE=DATE:20260101\r\n"
        f"SUMMARY:{summary}\r\n"
        f"{attendee_lines}"
        f"{alarm_lines}"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )


def calendar_with_events(*events):
    event_text = "".join(
        event.split("PRODID:test\r\n", 1)[1].rsplit("END:VCALENDAR", 1)[0]
        for event in events
    )
    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:test\r\n"
        + event_text
        + "END:VCALENDAR\r\n"
    )


def unfolded(value):
    return "\n".join(map(str, Contentlines.from_ical(value)))


def test_property_and_parameter_name_case_and_order_are_semantically_equal():
    old = ical(attendees=[("a@example.com", ";ROLE=REQ-PARTICIPANT;cn=A")])
    new = ical(attendees=[("a@example.com", ";CN=A;role=REQ-PARTICIPANT")])

    assert make_semantic_patch(old, new)["changes"] == []


def test_pure_folding_change_has_no_semantic_delta():
    old = ical("A long enough summary without any real semantic difference")
    new = old.replace(
        "SUMMARY:A long enough summary without any real semantic difference\r\n",
        "SUMMARY:A long enough summary without\r\n  any real semantic difference\r\n",
    )

    assert make_semantic_patch(old, new)["changes"] == []


def test_duplicate_attendee_parameters_and_repeated_attendees_are_preserved():
    old = ical(
        attendees=[
            ("a@example.com", ";ROLE=REQ-PARTICIPANT;ROLE=OPT-PARTICIPANT;CN=A"),
            ("b@example.com", ";CN=B"),
        ]
    )
    new = ical(
        attendees=[
            ("a@example.com", ";ROLE=REQ-PARTICIPANT;ROLE=OPT-PARTICIPANT;CN=A"),
            ("c@example.com", ";CN=C"),
        ]
    )

    patch = make_semantic_patch(old, new)
    result = apply_semantic_patch(old, patch).to_ical(sorted=False)
    text = unfolded(result)

    assert "ROLE=REQ-PARTICIPANT;ROLE=OPT-PARTICIPANT" in text
    assert "ATTENDEE" in text and "mailto:a@example.com" in text
    assert "ATTENDEE;CN=C:mailto:c@example.com" in text
    assert "mailto:b@example.com" not in text


def test_identical_attendee_uses_property_ordinal_precondition():
    old = ical(
        attendees=[
            ("same@example.com", ";CN=Same"),
            ("same@example.com", ";CN=Same"),
        ]
    )
    new = ical(attendees=[("same@example.com", ";CN=Same")])

    patch = make_semantic_patch(old, new)
    delete = next(
        change
        for change in patch["changes"][0]["changes"]
        if change["op"] == "delete_property"
    )

    assert delete["locator"] == {"property": "ATTENDEE", "ordinal": 1}
    result = apply_semantic_patch(old, patch).to_ical(sorted=False)
    assert unfolded(result).count("mailto:same@example.com") == 1


def test_repeated_parameters_and_parameter_lists_are_preserved():
    old = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:test\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:x\r\n"
        'ATTENDEE;X-P="a;b";X-P=c,d:mailto:a@example.com\r\n'
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    new = old.replace("X-P=c,d", "X-P=c,e")

    result = apply_semantic_patch(old, make_semantic_patch(old, new)).to_ical(
        sorted=False
    )

    assert unfolded(result) == unfolded(new)


def test_updates_only_selected_recurrence_component():
    old = calendar_with_events(
        ical("Old", uid="recurring"),
        ical(
        "Exception old", uid="recurring", recurrence_id="20260102T000000Z"
        ),
    )
    target = calendar_with_events(
        ical("Old", uid="recurring"),
        ical(
        "Exception new", uid="recurring", recurrence_id="20260102T000000Z"
        ),
    )

    patch = make_semantic_patch(old, target)
    event_changes = patch["changes"][0]

    assert event_changes["locator"] == {
        "component": "VEVENT",
        "uid": "recurring",
        "recurrence_id": {
            "name": "RECURRENCE-ID",
            "parameters": [],
            "value_type": "DATE-TIME",
            "value": "20260102T000000Z",
        },
    }
    assert unfolded(apply_semantic_patch(old, patch).to_ical(sorted=False)) == unfolded(
        target
    )


def test_unstable_component_uses_explicit_ordinal_and_summary():
    old = ical(alarms=[("DISPLAY", "-PT15M"), ("EMAIL", "-PT30M")])
    new = ical(alarms=[("DISPLAY", "-PT10M"), ("EMAIL", "-PT30M")])

    patch = make_semantic_patch(old, new)
    alarm_change = next(
        change
        for change in patch["changes"][0]["changes"]
        if change["op"] == "update_component"
    )

    assert alarm_change["locator"] == {"component": "VALARM", "ordinal": 0}
    assert alarm_change["old"]["component"] == "VALARM"


def test_unknown_x_property_and_explicit_value_type_are_preserved():
    old = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:test\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:x\r\n"
        "DTSTART;VALUE=DATE:20260101\r\n"
        "X-VENDOR;X-P=one;X-P=two:raw;a\\,b\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    new = old.replace("raw;a\\,b", "changed")

    result = apply_semantic_patch(old, make_semantic_patch(old, new)).to_ical(
        sorted=False
    )

    assert unfolded(result) == unfolded(new)


def test_categories_remains_a_single_multi_value_property():
    old = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:test\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:x\r\n"
        "CATEGORIES:Work,Personal\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    new = old.replace("Work,Personal", "Work,Family")

    result = apply_semantic_patch(old, make_semantic_patch(old, new)).to_ical(
        sorted=False
    )

    assert unfolded(result) == unfolded(new)


def test_patch_is_json_serializable():
    patch = make_semantic_patch(ical(), ical("New"))

    assert json.loads(json.dumps(patch)) == patch


def test_conflicting_patch_is_reported_without_partial_application():
    old = ical("Old")
    target = ical("New")
    edited = old.replace("SUMMARY:Old", "SUMMARY:Edited")
    patch = make_semantic_patch(old, target)
    snapshot = edited

    with pytest.raises(SemanticPatchConflict) as exc_info:
        apply_semantic_patch(edited, patch)

    assert exc_info.value.conflicts
    assert exc_info.value.conflicts[0]["message"] == "Calendar precondition does not match."
    assert edited == snapshot


def test_nested_precondition_conflict_leaves_calendar_unchanged():
    base = ical(
        "Old",
        alarms=(("DISPLAY", "-PT15M"), ("EMAIL", "-PT30M")),
    )
    changed_alarm = base.replace("TRIGGER:-PT15M", "TRIGGER:-PT5M")
    target = ical(
        "New",
        alarms=(("DISPLAY", "-PT10M"), ("EMAIL", "-PT30M")),
    )
    patch = make_semantic_patch(base, target)
    snapshot = changed_alarm

    with pytest.raises(SemanticPatchConflict) as exc_info:
        apply_semantic_patch(changed_alarm, patch)

    messages = {
        conflict["message"]
        for conflict in exc_info.value.conflicts
    }
    assert "Component precondition does not match." in messages
    assert changed_alarm == snapshot
