"""Tests for :mod:`icalendar.diff` - semantic calendar diff and patch."""

import json

import pytest

from icalendar import (
    Calendar,
    PatchConflictError,
    apply_patch,
    calendar_digest,
    diff_calendars,
)

BASE_ICS = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "PRODID:-//Test//EN\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:ev-1\r\n"
    "DTSTAMP:20261001T000000Z\r\n"
    "DTSTART:20261010T100000Z\r\n"
    "SUMMARY:Planning meeting\r\n"
    "ATTENDEE;CN=Alice:mailto:alice@example.com\r\n"
    "ATTENDEE;CN=Bob;ROLE=REQ-PARTICIPANT:mailto:bob@example.com\r\n"
    "ATTENDEE;CN=Carol:mailto:carol@example.com\r\n"
    "BEGIN:VALARM\r\n"
    "ACTION:DISPLAY\r\n"
    "TRIGGER:-PT15M\r\n"
    "DESCRIPTION:First reminder\r\n"
    "END:VALARM\r\n"
    "BEGIN:VALARM\r\n"
    "ACTION:DISPLAY\r\n"
    "TRIGGER:-PT5M\r\n"
    "DESCRIPTION:Second reminder\r\n"
    "END:VALARM\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:ev-2\r\n"
    "DTSTAMP:20261001T000000Z\r\n"
    "DTSTART:20261011T100000Z\r\n"
    "SUMMARY:Other event\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


def parse(ics):
    return Calendar.from_ical(ics)


def roundtrip(base_ics, target_ics):
    """Diff two calendars and apply the patch to the base."""
    base = parse(base_ics)
    target = parse(target_ics)
    patch = diff_calendars(base, target)
    result = apply_patch(base, patch)
    return base, target, patch, result


def test_no_changes_produces_empty_patch():
    cal = parse(BASE_ICS)
    patch = diff_calendars(cal, parse(BASE_ICS))
    assert patch["ops"] == []
    assert patch["base_digest"] == patch["target_digest"]


def test_pure_line_folding_change_produces_no_changes():
    long_description = "DESCRIPTION:" + "x" * 100 + "\r\n"
    folded = "DESCRIPTION:" + "x" * 60 + "\r\n " + "x" * 40 + "\r\n"
    ics_a = BASE_ICS.replace("VERSION:2.0\r\n", "VERSION:2.0\r\n" + long_description)
    ics_b = BASE_ICS.replace("VERSION:2.0\r\n", "VERSION:2.0\r\n" + folded)
    patch = diff_calendars(parse(ics_a), parse(ics_b))
    assert patch["ops"] == []


def test_case_and_order_differences_produce_no_changes():
    variant = (
        "begin:vcalendar\r\n"
        "prodid:-//Test//EN\r\n"
        "version:2.0\r\n"
        "BEGIN:VEVENT\r\n"
        "DTSTART:20261010T100000Z\r\n"
        "DTSTAMP:20261001T000000Z\r\n"
        "UID:ev-1\r\n"
        "SUMMARY:Planning meeting\r\n"
        "ATTENDEE;CN=Alice:mailto:alice@example.com\r\n"
        "ATTENDEE;role=REQ-PARTICIPANT;cn=Bob:mailto:bob@example.com\r\n"
        "ATTENDEE;CN=Carol:mailto:carol@example.com\r\n"
        "BEGIN:VALARM\r\n"
        "ACTION:DISPLAY\r\n"
        "TRIGGER:-PT15M\r\n"
        "DESCRIPTION:First reminder\r\n"
        "END:VALARM\r\n"
        "BEGIN:VALARM\r\n"
        "ACTION:DISPLAY\r\n"
        "TRIGGER:-PT5M\r\n"
        "DESCRIPTION:Second reminder\r\n"
        "END:VALARM\r\n"
        "END:VEVENT\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:ev-2\r\n"
        "DTSTAMP:20261001T000000Z\r\n"
        "DTSTART:20261011T100000Z\r\n"
        "SUMMARY:Other event\r\n"
        "END:VEVENT\r\n"
        "end:vcalendar\r\n"
    )
    patch = diff_calendars(parse(BASE_ICS), parse(variant))
    assert patch["ops"] == []


def test_single_component_change_is_located_by_uid():
    target_ics = BASE_ICS.replace("SUMMARY:Other event", "SUMMARY:Renamed event")
    base, target, patch, result = roundtrip(BASE_ICS, target_ics)
    assert len(patch["ops"]) == 1
    op = patch["ops"][0]
    assert op["op"] == "set_property"
    assert op["name"] == "SUMMARY"
    assert op["path"] == [{"name": "VEVENT", "uid": "ev-2", "recurrence_id": None}]
    assert calendar_digest(result) == calendar_digest(target)
    # the base calendar was not modified
    assert calendar_digest(base) == patch["base_digest"]


def test_duplicate_attendee_change_keeps_the_others():
    target_ics = BASE_ICS.replace(
        "ATTENDEE;CN=Bob;ROLE=REQ-PARTICIPANT:mailto:bob@example.com",
        "ATTENDEE;CN=Bobby;ROLE=OPT-PARTICIPANT:mailto:bob@example.com",
    )
    _base, target, patch, result = roundtrip(BASE_ICS, target_ics)
    assert len(patch["ops"]) == 1
    op = patch["ops"][0]
    assert op["op"] == "set_property"
    assert op["name"] == "ATTENDEE"
    assert op["index"] == 1
    assert op["old"]["params"] == {"CN": ["Bob"], "ROLE": ["REQ-PARTICIPANT"]}
    assert calendar_digest(result) == calendar_digest(target)
    attendees = result.walk("VEVENT")[0]["ATTENDEE"]
    assert len(attendees) == 3
    assert attendees[0].params["CN"] == "Alice"
    assert attendees[1].params["CN"] == "Bobby"
    assert attendees[2].params["CN"] == "Carol"


def test_add_and_remove_duplicate_attendee():
    target_ics = BASE_ICS.replace(
        "ATTENDEE;CN=Alice:mailto:alice@example.com\r\n", ""
    ).replace(
        "ATTENDEE;CN=Carol:mailto:carol@example.com\r\n",
        "",
    ).replace(
        "SUMMARY:Planning meeting\r\n",
        "SUMMARY:Planning meeting\r\n"
        "ATTENDEE;CN=Dave:mailto:dave@example.com\r\n",
    )
    _base, target, patch, result = roundtrip(BASE_ICS, target_ics)
    kinds = sorted(op["op"] for op in patch["ops"])
    assert kinds == ["add_property", "remove_property", "remove_property"]
    assert calendar_digest(result) == calendar_digest(target)
    attendees = result.walk("VEVENT")[0]["ATTENDEE"]
    assert [a.params["CN"] for a in attendees] == ["Bob", "Dave"]


def test_replacing_one_of_several_duplicate_attendees_is_a_change():
    target_ics = BASE_ICS.replace(
        "ATTENDEE;CN=Alice:mailto:alice@example.com\r\n",
        "ATTENDEE;CN=Dave:mailto:dave@example.com\r\n",
    )
    _base, target, patch, result = roundtrip(BASE_ICS, target_ics)
    assert [op["op"] for op in patch["ops"]] == ["set_property"]
    assert calendar_digest(result) == calendar_digest(target)


def test_recurrence_id_distinguishes_instances():
    ics_a = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:recurring\r\n"
        "DTSTAMP:20261001T000000Z\r\n"
        "DTSTART:20261010T100000Z\r\n"
        "SUMMARY:Weekly sync\r\n"
        "END:VEVENT\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:recurring\r\n"
        "RECURRENCE-ID:20261017T100000Z\r\n"
        "DTSTAMP:20261001T000000Z\r\n"
        "DTSTART:20261017T110000Z\r\n"
        "SUMMARY:Weekly sync (moved)\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    ics_b = ics_a.replace("SUMMARY:Weekly sync (moved)", "SUMMARY:Weekly sync (cancelled)")
    _base, target, patch, result = roundtrip(ics_a, ics_b)
    assert len(patch["ops"]) == 1
    step = patch["ops"][0]["path"][0]
    assert step["uid"] == "recurring"
    assert step["recurrence_id"] == "20261017T100000Z"
    assert calendar_digest(result) == calendar_digest(target)


def test_valarm_uses_explicit_position_and_digest():
    target_ics = BASE_ICS.replace("TRIGGER:-PT5M", "TRIGGER:-PT10M")
    _base, target, patch, result = roundtrip(BASE_ICS, target_ics)
    assert len(patch["ops"]) == 1
    op = patch["ops"][0]
    # the VALARM has no UID: located by hierarchy position, with the old
    # semantic digest of the component as a precondition
    assert op["path"][-1] == {"name": "VALARM", "index": 1}
    assert op["component_digest"]
    assert calendar_digest(result) == calendar_digest(target)


def test_add_and_remove_component():
    ics_with_todo = BASE_ICS.replace(
        "END:VCALENDAR",
        "BEGIN:VTODO\r\n"
        "UID:todo-1\r\n"
        "DTSTAMP:20261001T000000Z\r\n"
        "SUMMARY:Buy milk\r\n"
        "END:VTODO\r\n"
        "END:VCALENDAR",
    )
    # adding a component
    _base, target, patch, result = roundtrip(BASE_ICS, ics_with_todo)
    assert [op["op"] for op in patch["ops"]] == ["add_component"]
    assert calendar_digest(result) == calendar_digest(target)
    # removing it again
    _base2, target2, patch2, result2 = roundtrip(ics_with_todo, BASE_ICS)
    assert [op["op"] for op in patch2["ops"]] == ["remove_component"]
    assert calendar_digest(result2) == calendar_digest(target2)


def test_unknown_x_properties_and_value_types_are_preserved():
    target_ics = BASE_ICS.replace(
        "SUMMARY:Other event\r\n",
        "SUMMARY:Other event\r\n"
        "X-CUSTOM-FLAG;X-INDEX=1,2:some\\,strange value\r\n"
        "X-HEX;VALUE=UNKNOWN:0xCAFE\r\n",
    )
    _base, target, patch, result = roundtrip(BASE_ICS, target_ics)
    assert calendar_digest(result) == calendar_digest(target)
    event = [e for e in result.walk("VEVENT") if e.get("X-CUSTOM-FLAG")][0]
    prop = event["X-CUSTOM-FLAG"]
    assert type(prop).__name__ == "vUnknown"
    assert prop.to_ical() == b"some\\,strange value"
    assert list(prop.params["X-INDEX"]) == ["1", "2"]
    assert event["X-HEX"].to_ical() == b"0xCAFE"


def test_parameter_lists_are_preserved():
    ics_a = BASE_ICS.replace(
        "ATTENDEE;CN=Alice:mailto:alice@example.com\r\n",
        'ATTENDEE;CN=Alice;DELEGATED-TO="mailto:a@example.com",'
        '"mailto:b@example.com":mailto:alice@example.com\r\n',
    )
    ics_b = ics_a.replace("mailto:b@example.com", "mailto:c@example.com")
    _base, target, patch, result = roundtrip(ics_a, ics_b)
    assert len(patch["ops"]) == 1
    op = patch["ops"][0]
    assert op["old"]["params"]["DELEGATED-TO"] == [
        "mailto:a@example.com",
        "mailto:b@example.com",
    ]
    assert calendar_digest(result) == calendar_digest(target)
    attendee = result.walk("VEVENT")[0]["ATTENDEE"][0]
    assert list(attendee.params["DELEGATED-TO"]) == [
        "mailto:a@example.com",
        "mailto:c@example.com",
    ]


def test_patch_is_json_serializable():
    target_ics = BASE_ICS.replace("SUMMARY:Other event", "SUMMARY:Renamed")
    base = parse(BASE_ICS)
    target = parse(target_ics)
    patch = diff_calendars(base, target)
    round_tripped = json.loads(json.dumps(patch))
    assert round_tripped == patch
    result = apply_patch(base, round_tripped)
    assert calendar_digest(result) == calendar_digest(target)
    # JSON text is accepted directly, too
    result2 = apply_patch(base, json.dumps(patch))
    assert calendar_digest(result2) == calendar_digest(target)


def test_conflict_when_applied_to_edited_copy():
    target_ics = BASE_ICS.replace("SUMMARY:Other event", "SUMMARY:Renamed event")
    base = parse(BASE_ICS)
    patch = diff_calendars(base, parse(target_ics))

    # the copy was edited independently in the meantime
    edited = parse(BASE_ICS.replace("SUMMARY:Other event", "SUMMARY:Local edit"))
    before = edited.to_ical()
    with pytest.raises(PatchConflictError) as exc_info:
        apply_patch(edited, patch)
    conflicts = exc_info.value.conflicts
    assert conflicts
    reasons = {c["reason"] for c in conflicts}
    assert reasons & {"digest_mismatch", "property_mismatch"}
    # the conflict report itself is JSON-serializable
    json.dumps(conflicts)
    # no half-applied state: the edited copy is untouched
    assert edited.to_ical() == before


def test_conflict_reports_all_failing_preconditions():
    target_ics = BASE_ICS.replace("SUMMARY:Other event", "SUMMARY:Renamed").replace(
        "TRIGGER:-PT5M", "TRIGGER:-PT10M"
    )
    base = parse(BASE_ICS)
    patch = diff_calendars(base, parse(target_ics))
    assert len(patch["ops"]) == 2
    # edit both targets so both preconditions fail
    edited = parse(
        BASE_ICS.replace("SUMMARY:Other event", "SUMMARY:Local").replace(
            "TRIGGER:-PT5M", "TRIGGER:-PT1M"
        )
    )
    with pytest.raises(PatchConflictError) as exc_info:
        apply_patch(edited, patch)
    # both failing operations are reported, not just the first one
    op_indexes = {c["op_index"] for c in exc_info.value.conflicts if c["op"]}
    assert op_indexes == {0, 1}


def test_base_digest_mismatch_is_reported():
    target_ics = BASE_ICS.replace("SUMMARY:Other event", "SUMMARY:Renamed")
    patch = diff_calendars(parse(BASE_ICS), parse(target_ics))
    other = parse(BASE_ICS.replace("PRODID:-//Test//EN", "PRODID:-//Other//EN"))
    before = other.to_ical()
    with pytest.raises(PatchConflictError) as exc_info:
        apply_patch(other, patch)
    assert exc_info.value.conflicts[0]["reason"] == "base_digest_mismatch"
    assert other.to_ical() == before


def test_removed_component_cannot_be_found_is_a_conflict():
    ics_with_todo = BASE_ICS.replace(
        "END:VCALENDAR",
        "BEGIN:VTODO\r\n"
        "UID:todo-1\r\n"
        "DTSTAMP:20261001T000000Z\r\n"
        "SUMMARY:Buy milk\r\n"
        "END:VTODO\r\n"
        "END:VCALENDAR",
    )
    patch = diff_calendars(parse(ics_with_todo), parse(BASE_ICS))
    assert patch["ops"][0]["op"] == "remove_component"
    # the TODO is already gone in this copy
    with pytest.raises(PatchConflictError) as exc_info:
        apply_patch(parse(BASE_ICS), patch)
    reasons = {c["reason"] for c in exc_info.value.conflicts}
    assert reasons & {"base_digest_mismatch", "component_not_found"}
