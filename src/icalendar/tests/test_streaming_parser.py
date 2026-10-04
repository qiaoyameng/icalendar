"""Tests for the streaming, feed/finish-style calendar parser."""

from contextlib import suppress
from datetime import timedelta
from pathlib import Path

import pytest

from icalendar import (
    Calendar,
    IncompleteStreamError,
    InvalidCalendar,
    StreamingCalendarParser,
    StreamLimitError,
    StreamLimits,
    StreamStateError,
)
from icalendar.caselessdict import CaselessDict

CALENDARS_FOLDER = Path(__file__).parent / "calendars"

SIMPLE = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "PRODID:-//Test//Test//EN\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:1\r\n"
    "DTSTART:20240101T120000Z\r\n"
    "SUMMARY:First\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:2\r\n"
    "DTSTART:20240102T120000Z\r\n"
    "SUMMARY:Second\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
).encode()

# Folds (space and tab), quoted parameters, colons in values and
# multi-byte UTF-8 characters: many interesting chunk split points.
TRICKY = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:tricky\r\n"
    "DTSTART:20240101T120000Z\r\n"
    "DESCRIPTION:日本語のテキスト with a long line that is folded\r\n"
    " onto the next physical line\tand a tab fold\r\n"
    'ATTENDEE;CN="Doe, John; still quoted";ROLE=REQ-PARTICIPANT:mailto:j@example.com\r\n'
    'X-CUSTOM;P="quoted;param:with:colons":value:with:colons\r\n'
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
).encode()

FORWARD_TZ = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:fw\r\n"
    "DTSTART;TZID=Custom/Zone:20240101T120000\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VTIMEZONE\r\n"
    "TZID:Custom/Zone\r\n"
    "BEGIN:STANDARD\r\n"
    "DTSTART:19670101T000000\r\n"
    "TZOFFSETFROM:+0300\r\n"
    "TZOFFSETTO:+0300\r\n"
    "TZNAME:CST\r\n"
    "END:STANDARD\r\n"
    "END:VTIMEZONE\r\n"
    "END:VCALENDAR\r\n"
).encode()

UNDEFINED_TZ = (
    "BEGIN:VCALENDAR\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:undef\r\n"
    "DTSTART;TZID=Custom/NeverDefined:20240101T120000\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
).encode()


def parse_streamed(data, chunk_size):
    """Parse data with the streaming parser using equally sized chunks."""
    parser = Calendar.streaming_parser()
    delivered = []
    for start in range(0, len(data), chunk_size):
        delivered += parser.feed(data[start : start + chunk_size])
    delivered += parser.finish()
    return delivered


def assert_matches_from_ical(data, chunk_size):
    """Assert that streaming in chunks matches the one-shot from_ical()."""
    ref_roots = Calendar.from_ical(data, multiple=True)
    delivered = parse_streamed(data, chunk_size)
    remaining = list(delivered)
    for ref in ref_roots:
        # The root shell carries the calendar properties; its subcomponents
        # were delivered separately while streaming. Deferred components may
        # be delivered after their shell, so find the shell by name+content.
        for index, component in enumerate(remaining):
            if component.name == ref.name and CaselessDict.__eq__(component, ref):
                shell = remaining.pop(index)
                break
        else:
            raise AssertionError(f"no streamed shell found for {ref.name}")
        assert shell.name == ref.name
        assert shell.subcomponents == []
    ref_children = [sub for ref in ref_roots for sub in ref.subcomponents]
    assert sorted(c.to_ical() for c in remaining) == sorted(
        c.to_ical() for c in ref_children
    )


@pytest.mark.parametrize("data", [SIMPLE, TRICKY, FORWARD_TZ, UNDEFINED_TZ])
@pytest.mark.parametrize("chunk_size", [1, 2, 3, 7, 64, 100_000])
def test_matches_from_ical_for_all_chunk_sizes(tzp, data, chunk_size):
    """Chunking must not influence the result, down to single bytes."""
    assert_matches_from_ical(data, chunk_size)


def test_every_two_chunk_split_matches(tzp):
    """Every possible split into two chunks yields the same components."""
    reference = [c.to_ical() for c in parse_streamed(TRICKY, len(TRICKY))]
    for split in range(len(TRICKY) + 1):
        parser = Calendar.streaming_parser()
        delivered = parser.feed(TRICKY[:split])
        delivered += parser.feed(TRICKY[split:])
        delivered += parser.finish()
        assert [c.to_ical() for c in delivered] == reference, f"split at {split}"


def test_str_chunks_match_bytes(tzp):
    """Feeding str chunks yields the same components as feeding bytes."""
    text = TRICKY.decode()
    assert [c.to_ical() for c in parse_streamed(text, 3)] == [
        c.to_ical() for c in parse_streamed(TRICKY, 3)
    ]


def test_components_are_delivered_incrementally(tzp):
    """A complete subcomponent is delivered before the stream ends."""
    parser = Calendar.streaming_parser()
    cut = SIMPLE.index(b"END:VEVENT") + len(b"END:VEVENT\r\n")
    # The following line must arrive before END:VEVENT is known to be
    # unfolded, so only then can the VEVENT be delivered.
    assert parser.feed(SIMPLE[:cut]) == []
    delivered = parser.feed(SIMPLE[cut : cut + len(b"BEGIN:VEVENT\r\n")])
    assert len(delivered) == 1
    assert delivered[0].name == "VEVENT"
    assert delivered[0]["UID"] == "1"


def test_forward_referenced_timezone_is_buffered(tzp):
    """A component using a later-defined TZID is not delivered with a guess."""
    parser = Calendar.streaming_parser()
    cut = FORWARD_TZ.index(b"BEGIN:VTIMEZONE") + len(b"BEGIN:VTIMEZONE\r\n")
    assert parser.feed(FORWARD_TZ[:cut]) == []
    assert parser.pending_component_count == 1
    delivered = parser.feed(FORWARD_TZ[cut:])
    delivered += parser.finish()
    assert parser.pending_component_count == 0
    event = next(c for c in delivered if c.name == "VEVENT")
    # The event was only delivered after its VTIMEZONE had been seen, with
    # the correct private timezone attached.
    assert event["DTSTART"].dt.utcoffset() == timedelta(hours=3)
    reference = Calendar.from_ical(FORWARD_TZ)
    assert event.to_ical() == reference.walk("VEVENT")[0].to_ical()


def test_undefined_timezone_is_resolved_at_finish(tzp):
    """A TZID that is never defined falls back to one-shot semantics."""
    delivered = parse_streamed(UNDEFINED_TZ, 5)
    event = next(c for c in delivered if c.name == "VEVENT")
    reference = Calendar.from_ical(UNDEFINED_TZ)
    assert event["DTSTART"].dt == reference.walk("VEVENT")[0]["DTSTART"].dt
    assert event["DTSTART"].dt.tzinfo is None


def test_forward_reference_fixture_file(tzp):
    """The forward-reference example calendar streams correctly."""
    data = (CALENDARS_FOLDER / "america_new_york_forward_reference.ics").read_bytes()
    assert_matches_from_ical(data, 1)
    assert_matches_from_ical(data, 100_000)


def test_line_length_limit(tzp):
    parser = Calendar.streaming_parser(limits=StreamLimits(max_line_length=50))
    with pytest.raises(StreamLimitError):
        parser.feed(b"BEGIN:VCALENDAR\r\nSUMMARY:" + b"x" * 100 + b"\r\n")


def test_unterminated_line_length_limit(tzp):
    """The limit also applies while no line break has arrived yet."""
    parser = Calendar.streaming_parser(limits=StreamLimits(max_line_length=50))
    with pytest.raises(StreamLimitError):
        parser.feed(b"x" * 100)


def test_depth_limit(tzp):
    parser = Calendar.streaming_parser(limits=StreamLimits(max_depth=3))
    with pytest.raises(StreamLimitError):
        parser.feed(b"BEGIN:A\r\nBEGIN:B\r\nBEGIN:C\r\nBEGIN:D\r\nBEGIN:E\r\n")


def test_component_size_limit(tzp):
    parser = Calendar.streaming_parser(limits=StreamLimits(max_component_size=50))
    with pytest.raises(StreamLimitError):
        parser.feed(SIMPLE)


def test_pending_components_limit(tzp):
    data = (
        b"BEGIN:VCALENDAR\r\n"
        b"BEGIN:VEVENT\r\nDTSTART;TZID=Undefined/1:20240101T120000\r\nEND:VEVENT\r\n"
        b"BEGIN:VEVENT\r\nDTSTART;TZID=Undefined/2:20240101T120000\r\nEND:VEVENT\r\n"
        b"END:VCALENDAR\r\n"
    )
    parser = Calendar.streaming_parser(limits=StreamLimits(max_pending_components=1))
    with pytest.raises(StreamLimitError):
        parser.feed(data)


def test_limits_are_invalid_calendar_errors():
    assert issubclass(StreamLimitError, InvalidCalendar)
    assert issubclass(IncompleteStreamError, InvalidCalendar)


def test_finish_with_unclosed_components_raises(tzp):
    parser = Calendar.streaming_parser()
    parser.feed(b"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:1\r\n")
    with pytest.raises(IncompleteStreamError):
        parser.finish()


def test_finish_with_unclosed_root_raises(tzp):
    parser = Calendar.streaming_parser()
    parser.feed(b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n")
    with pytest.raises(IncompleteStreamError):
        parser.finish()


def test_feed_after_finish_raises(tzp):
    parser = Calendar.streaming_parser()
    parser.finish()
    with pytest.raises(StreamStateError):
        parser.feed(b"BEGIN:VCALENDAR\r\n")


def test_finish_twice_raises(tzp):
    parser = Calendar.streaming_parser()
    parser.finish()
    with pytest.raises(StreamStateError):
        parser.finish()


def test_empty_stream(tzp):
    assert Calendar.streaming_parser().finish() == []


def test_end_without_begin_raises(tzp):
    parser = Calendar.streaming_parser()
    with pytest.raises(ValueError, match="END encountered"):
        parser.feed(b"END:VEVENT\r\n")
        parser.finish()


def test_property_without_parent_raises(tzp):
    parser = Calendar.streaming_parser()
    with pytest.raises(ValueError, match="does not have a parent"):
        parser.feed(b"SUMMARY:nope\r\n")
        parser.finish()


def test_x_comment_without_parent_is_ignored(tzp):
    parser = Calendar.streaming_parser()
    assert parser.feed(b"X-COMMENT:hello\r\n") == []
    assert parser.finish() == []


def test_final_line_without_line_break(tzp):
    """A trailing line without a line break is still parsed, as from_ical."""
    data = SIMPLE.rstrip(b"\r\n")
    assert_matches_from_ical(data, 3)


class BoundedReadOnlySource:
    """A read-only source that only offers read() of limited size."""

    def __init__(self, data: bytes, max_read: int):
        self._data = data
        self._max_read = max_read
        self.position = 0
        self.read_sizes = []

    def read(self, size: int = -1) -> bytes:
        assert 0 < size <= self._max_read, "parser must read bounded chunks"
        self.read_sizes.append(size)
        chunk = self._data[self.position : self.position + size]
        self.position += len(chunk)
        return chunk


def test_feed_stream_reads_bounded_chunks_only(tzp):
    """The parser consumes a read-only source incrementally."""
    source = BoundedReadOnlySource(SIMPLE, max_read=13)
    parser = Calendar.streaming_parser()
    delivered = []
    positions = []
    for component in parser.feed_stream(source, chunk_size=13):
        delivered.append(component)
        positions.append(source.position)
    # The first VEVENT was delivered before the source was read to the end.
    assert delivered[0].name == "VEVENT"
    assert positions[0] < len(SIMPLE)
    assert max(source.read_sizes) <= 13
    reference = Calendar.from_ical(SIMPLE)
    shell = delivered[-1]
    children = delivered[:-1]
    assert isinstance(shell, Calendar)
    assert CaselessDict.__eq__(shell, reference)
    assert sorted(c.to_ical() for c in children) == sorted(
        c.to_ical() for c in reference.subcomponents
    )


def test_feed_stream_rejects_nonpositive_chunk_size(tzp):
    parser = Calendar.streaming_parser()
    with pytest.raises(ValueError, match="chunk_size"):
        list(parser.feed_stream(BoundedReadOnlySource(SIMPLE, 1), chunk_size=0))


def test_default_parser_matches_calendar_parser(tzp):
    """StreamingCalendarParser() uses the same factories as Calendar."""
    parser = StreamingCalendarParser()
    delivered = parser.feed(SIMPLE) + parser.finish()
    assert isinstance(delivered[-1], Calendar)


def calendar_files():
    return sorted(CALENDARS_FOLDER.glob("*.ics"))


@pytest.mark.parametrize("path", calendar_files(), ids=lambda p: p.name)
@pytest.mark.parametrize("chunk_size", [1, 13, 1024, 1_000_000])
def test_calendar_fixtures_match_from_ical(tzp, path, chunk_size):
    """Every calendar fixture parses identically when streamed."""
    data = path.read_bytes()
    try:
        Calendar.from_ical(data, multiple=True)
    except (ValueError, IndexError) as reference_error:
        with pytest.raises(type(reference_error)):
            parse_streamed(data, chunk_size)
    else:
        # from_ical() silently accepts truncated input, while the
        # streaming parser reports it from finish().
        with suppress(IncompleteStreamError):
            assert_matches_from_ical(data, chunk_size)
