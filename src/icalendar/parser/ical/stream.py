"""Streaming, feed/finish-style parsing of iCalendar data.

This module provides :class:`StreamingCalendarParser`, an incremental
counterpart to the one-shot :meth:`icalendar.cal.component.Component.from_ical`
parsing entry point. It is meant for large calendars that arrive in chunks,
for example from an upload, so that each complete top-level calendar
subcomponent can be processed before the whole input has been received.
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass
from typing import TYPE_CHECKING

from icalendar.error import InvalidCalendar
from icalendar.parser.content_line import Contentline
from icalendar.parser.ical.component import ComponentIcalParser
from icalendar.timezone import tzp

if TYPE_CHECKING:
    from collections.abc import Iterator

    from icalendar.cal.component import Component
    from icalendar.cal.component_factory import ComponentFactory
    from icalendar.prop import TypesFactory


class StreamParseError(InvalidCalendar):
    """Base class for errors raised by the streaming parser."""


class StreamLimitError(StreamParseError):
    """A configured limit of the streaming parser was exceeded."""


class IncompleteStreamError(StreamParseError):
    """The stream ended before all components were closed."""


class StreamStateError(StreamParseError):
    """The parser was called in a way its state does not allow."""


@dataclass(frozen=True)
class StreamLimits:
    """Resource limits for the :class:`StreamingCalendarParser`.

    Parameters:
        max_line_length: Maximum length of one logical (unfolded) content
            line, in characters.
        max_depth: Maximum component nesting depth.
        max_component_size: Maximum accumulated size of one top-level
            component's raw content lines, in characters.
        max_pending_components: Maximum number of complete components that
            may be buffered while the ``VTIMEZONE`` components defining
            their ``TZID`` references are still missing.
    """

    max_line_length: int = 1_000_000
    max_depth: int = 100
    max_component_size: int = 10_000_000
    max_pending_components: int = 1_000


@dataclass
class _PendingComponent:
    """A fully received component whose timezones are not defined yet."""

    lines: list[Contentline]
    unknown_tzids: set[str]


class StreamingCalendarParser:
    """Incrementally parse iCalendar data that arrives in chunks.

    The parser follows the semantics of the one-shot
    :meth:`icalendar.cal.component.Component.from_ical` entry point, but
    accepts the input in pieces through :meth:`feed`. Each top-level
    calendar subcomponent (a direct child of the root component, such as a
    ``VEVENT`` inside the ``VCALENDAR``) is delivered as soon as it is
    complete. When the root component itself closes, it is delivered as
    well, holding its own properties; its subcomponents were already
    delivered and are not attached to it again. Call :meth:`finish` when
    the stream ends.

    Chunk boundaries do not influence the result: chunks may split the
    input in the middle of a UTF-8 character, a ``CRLF`` line break, the
    whitespace of a folded line, or a quoted parameter value.

    Components are tracked structurally through their parsed
    ``BEGIN``/``END`` content lines, never by searching the raw text for
    terminator strings, so property values that merely look like component
    boundaries cannot truncate a component.

    A component that refers to a ``TZID`` which is not known yet - for
    example a private timezone whose ``VTIMEZONE`` only appears later in
    the stream - is not delivered with a guessed timezone. Instead it is
    buffered (at most :attr:`StreamLimits.max_pending_components`
    components) and parsed once the defining ``VTIMEZONE`` has been seen.
    Components whose timezones are still undefined when the stream ends
    are parsed by :meth:`finish` with the same result the one-shot parser
    would produce. Delivered components are never modified afterwards.

    Example:

        .. code-block:: pycon

            >>> from icalendar import Calendar
            >>> parser = Calendar.streaming_parser()
            >>> parser.feed(b"BEGIN:VCALENDAR\\r\\nVERSION:2.0\\r\\nBEGIN:VEVENT\\r\\n")
            []
            >>> parser.feed(b"UID:1\\r\\nEND:VEVENT\\r\\nEND:VCALENDAR\\r\\n")
            [VEVENT({'UID': vText(b'1')})]
            >>> parser.finish()
            [VCALENDAR({'VERSION': vText(b'2.0')})]
    """

    def __init__(
        self,
        component_factory: ComponentFactory | None = None,
        types_factory: TypesFactory | None = None,
        limits: StreamLimits | None = None,
    ) -> None:
        """Create a new streaming parser.

        Parameters:
            component_factory: The factory used to create components.
                Defaults to the shared factory of
                :class:`icalendar.cal.component.Component`.
            types_factory: The factory used to create property values.
                Defaults to the shared :class:`icalendar.prop.TypesFactory`
                instance.
            limits: The resource limits to enforce, see :class:`StreamLimits`.
        """
        if component_factory is None:
            from icalendar.cal.component import Component  # noqa: PLC0415

            component_factory = Component._get_component_factory()  # noqa: SLF001
        if types_factory is None:
            from icalendar.prop import TypesFactory  # noqa: PLC0415

            types_factory = TypesFactory.instance()
        self._component_factory = component_factory
        self._types_factory = types_factory
        self._limits = limits if limits is not None else StreamLimits()
        # utf-8-sig with "replace" mirrors to_unicode(), which backs
        # Contentlines.from_ical(): a leading BOM is stripped and invalid
        # byte sequences become U+FFFD instead of raising.
        self._decoder = codecs.getincrementaldecoder("utf-8-sig")(errors="replace")
        self._buffer = ""  # one incomplete physical line
        self._logical: str | None = None  # logical line under construction
        self._stack: list[str] = []  # names of the currently open components
        self._root_lines: list[Contentline] | None = None
        self._root_size = 0
        self._child_lines: list[Contentline] | None = None
        self._child_size = 0
        self._pending: list[_PendingComponent] = []
        self._delivered: list[Component] = []
        self._finished = False

    @property
    def limits(self) -> StreamLimits:
        """The limits this parser enforces."""
        return self._limits

    @property
    def pending_component_count(self) -> int:
        """The number of complete components currently buffered.

        These components refer to ``TZID`` values that no ``VTIMEZONE``
        has defined yet. They are delivered once their timezones arrive,
        or at the latest when :meth:`finish` is called.
        """
        return len(self._pending)

    def feed(self, data: str | bytes) -> list[Component]:
        """Feed a chunk of iCalendar data to the parser.

        Parameters:
            data: The next chunk of the stream, as ``str`` or ``bytes``.
                Chunks may be of any size and may split the input anywhere.

        Returns:
            The components that were completed by this chunk, see
            :meth:`finish` for the remaining ones.

        Raises:
            StreamStateError: If :meth:`finish` was already called.
            StreamLimitError: If one of the configured limits is exceeded.
            ValueError: If the input is not valid iCalendar data.
        """
        if self._finished:
            raise StreamStateError("Cannot feed data after finish() was called.")
        if isinstance(data, str):
            text = data
        elif isinstance(data, (bytes, bytearray, memoryview)):
            text = self._decoder.decode(bytes(data))
        else:
            raise TypeError(f"Expected str or bytes, got {type(data).__name__}.")
        self._buffer += text
        self._extract_lines(final=False)
        if len(self._buffer) > self._limits.max_line_length:
            raise StreamLimitError(
                "Physical line without a line break exceeds the limit of "
                f"{self._limits.max_line_length} characters."
            )
        return self._drain()

    def finish(self) -> list[Component]:
        """Signal the end of the stream and flush the parser.

        Returns:
            The components completed by the trailing data, including any
            components that were still buffered while waiting for timezone
            definitions.

        Raises:
            StreamStateError: If :meth:`finish` was already called.
            IncompleteStreamError: If the stream ended in the middle of a
                component.
            StreamLimitError: If one of the configured limits is exceeded.
            ValueError: If the input is not valid iCalendar data.
        """
        if self._finished:
            raise StreamStateError("finish() was already called.")
        self._finished = True
        self._buffer += self._decoder.decode(b"", final=True)
        self._extract_lines(final=True)
        if self._logical is not None:
            logical, self._logical = self._logical, None
            self._process_logical_line(logical)
        if self._stack:
            raise IncompleteStreamError(
                "Truncated iCalendar stream: unclosed components "
                + ", ".join(self._stack)
            )
        # No more input can arrive, so no more VTIMEZONE definitions can
        # arrive either: parse the remaining deferred components with the
        # final timezone state, exactly as a one-shot parse would.
        pending, self._pending = self._pending, []
        for component in pending:
            self._deliver(self._parse_component(component.lines))
        return self._drain()

    def feed_stream(self, source, chunk_size: int = 65536) -> Iterator[Component]:
        """Feed the parser from a file-like object and yield components.

        The source is consumed incrementally: every ``read()`` call
        requests at most ``chunk_size`` bytes, and components are yielded
        as soon as they are complete, before the source is exhausted.

        Parameters:
            source: An object with a ``read(size)`` method returning
                ``bytes`` or ``str``, such as an open file.
            chunk_size: The maximum number of bytes or characters read
                from the source in one call.

        Returns:
            An iterator over the parsed components.

        Raises:
            ValueError: If ``chunk_size`` is not positive.
        """
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive.")
        while True:
            chunk = source.read(chunk_size)
            if not chunk:
                break
            yield from self.feed(chunk)
        yield from self.finish()

    def _extract_lines(self, final: bool) -> None:
        """Move complete physical lines out of the buffer."""
        *lines, self._buffer = self._buffer.split("\n")
        for line in lines:
            if line.endswith("\r"):
                line = line[:-1]
            self._handle_physical_line(line)
        if final and self._buffer:
            # The one-shot parser accepts a last line without a line break.
            line, self._buffer = self._buffer, ""
            self._handle_physical_line(line)

    def _handle_physical_line(self, line: str) -> None:
        """Unfold one physical line into the current logical line."""
        if line == "":
            # Empty physical lines carry no information. Dropping them here
            # matches Contentlines.from_ical(), which filters them out, and
            # UFOLD, which collapses empty lines inside a fold.
            return
        if line[0] in " \t" and self._logical is not None:
            # A folded continuation: the line break and exactly one
            # whitespace character are removed, as by UFOLD.
            self._logical += line[1:]
            self._check_line_length()
            return
        if self._logical is not None:
            self._process_logical_line(self._logical)
        self._logical = line
        self._check_line_length()

    def _check_line_length(self) -> None:
        """Enforce the logical line length limit."""
        if len(self._logical) > self._limits.max_line_length:
            raise StreamLimitError(
                "Logical line exceeds the limit of "
                f"{self._limits.max_line_length} characters."
            )

    def _process_logical_line(self, line: str) -> None:
        """Update the structural state with one complete logical line."""
        content_line = Contentline(line)
        try:
            name, _params, vals = content_line.parts()
        except ValueError:
            if not self._stack:
                # With no open component, the one-shot parser raises, too.
                raise
            # Keep the broken line verbatim: the component parser applies
            # the error tolerance of the component when it is parsed.
            self._append_line(content_line)
            return
        uname = name.upper()
        if uname == "BEGIN":
            self._begin_component(vals.upper(), content_line)
        elif uname == "END":
            self._end_component(content_line)
        elif not self._stack:
            # Only X-COMMENT is accepted outside of a component, as in
            # ComponentIcalParser.handle_property().
            if uname != "X-COMMENT":
                raise ValueError(f'Property "{uname}" does not have a parent component.')
        else:
            self._append_line(content_line)

    def _append_line(self, line: Contentline) -> None:
        """Accumulate a line of the currently open component."""
        if self._child_lines is not None:
            self._child_lines.append(line)
            self._child_size += len(line)
            if self._child_size > self._limits.max_component_size:
                raise StreamLimitError(
                    "Component exceeds the limit of "
                    f"{self._limits.max_component_size} characters."
                )
        else:
            self._root_lines.append(line)
            self._root_size += len(line)
            if self._root_size > self._limits.max_component_size:
                raise StreamLimitError(
                    "Component exceeds the limit of "
                    f"{self._limits.max_component_size} characters."
                )

    def _begin_component(self, name: str, line: Contentline) -> None:
        """Open a new component."""
        depth = len(self._stack)
        if depth + 1 > self._limits.max_depth:
            raise StreamLimitError(
                "Component nesting exceeds the depth limit of "
                f"{self._limits.max_depth}."
            )
        self._stack.append(name)
        if depth == 0:
            self._root_lines = [line]
            self._root_size = len(line)
        elif depth == 1:
            self._child_lines = [line]
            self._child_size = len(line)
        else:
            self._append_line(line)

    def _end_component(self, line: Contentline) -> None:
        """Close the current component."""
        if not self._stack:
            raise ValueError("END encountered without an accompanying BEGIN!")
        name = self._stack.pop()
        depth = len(self._stack) + 1
        if depth == 1:
            self._root_lines.append(line)
            lines, self._root_lines = self._root_lines, None
            self._deliver(self._parse_component(lines))
        elif depth == 2:
            self._child_lines.append(line)
            lines, self._child_lines = self._child_lines, None
            self._complete_child(name, lines)
        else:
            self._append_line(line)

    def _complete_child(self, name: str, lines: list[Contentline]) -> None:
        """Handle a completed direct child of the root component."""
        if name == "VTIMEZONE":
            # Parsing the component caches its timezone in tzp, see
            # ComponentIcalParser.handle_end_component().
            self._deliver(self._parse_component(lines))
            self._resolve_pending()
            return
        unknown_tzids = self._unknown_tzids(lines)
        if unknown_tzids:
            if len(self._pending) >= self._limits.max_pending_components:
                raise StreamLimitError(
                    "More than "
                    f"{self._limits.max_pending_components} components are "
                    "waiting for timezone definitions."
                )
            self._pending.append(_PendingComponent(lines, unknown_tzids))
        else:
            self._deliver(self._parse_component(lines))

    def _unknown_tzids(self, lines: list[Contentline]) -> set[str]:
        """Collect the TZIDs a component refers to that are not known yet."""
        unknown_tzids = set()
        for line in lines:
            tzid = self._tzid_of(line)
            if tzid is not None and tzp.timezone(tzid) is None:
                unknown_tzids.add(tzid)
        return unknown_tzids

    @staticmethod
    def _tzid_of(line: Contentline) -> str | None:
        """Return the TZID parameter of a datetime property line, if any."""
        try:
            name, params, _vals = line.parts()
        except ValueError:
            return None
        if name.upper() in ComponentIcalParser.datetime_names and params:
            return params.get("TZID")
        return None

    def _resolve_pending(self) -> None:
        """Deliver buffered components whose timezones are known now."""
        still_pending = []
        for component in self._pending:
            component.unknown_tzids = {
                tzid for tzid in component.unknown_tzids if tzp.timezone(tzid) is None
            }
            if component.unknown_tzids:
                still_pending.append(component)
            else:
                self._deliver(self._parse_component(component.lines))
        self._pending = still_pending

    def _parse_component(self, lines: list[Contentline]) -> Component:
        """Parse the accumulated content lines of one component."""
        parser = ComponentIcalParser(
            lines, self._component_factory, self._types_factory
        )
        return parser.parse()[0]

    def _deliver(self, component: Component) -> None:
        """Queue a completed component for the caller."""
        self._delivered.append(component)

    def _drain(self) -> list[Component]:
        """Collect the components completed since the last call."""
        delivered, self._delivered = self._delivered, []
        return delivered


__all__ = [
    "IncompleteStreamError",
    "StreamLimitError",
    "StreamLimits",
    "StreamParseError",
    "StreamStateError",
    "StreamingCalendarParser",
]
