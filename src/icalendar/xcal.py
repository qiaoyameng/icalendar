"""xCal serialization and parsing according to :rfc:`6321`.

xCal is the XML representation of iCalendar data. Unlike jCal
(:rfc:`7265`), its structured value types (``period``, ``recur``,
``utc-offset`` and ``binary``) are represented with nested XML elements
instead of plain strings or arrays.

This module bridges Component objects and xCal documents by going through
the existing jCal representation:

- Serialization converts :meth:`Component.to_jcal` nodes to an XML tree.
- Parsing builds jCal-like nodes from the XML and hands them to
  :meth:`Component.from_jcal`.

Only the Python standard library is used. The parser is built directly on
:mod:`xml.parsers.expat` so that external resources are never resolved and
so that the document depth and text size can be limited. Text escaping is
handled by this XML layer; the iCalendar backslash escaping of
:rfc:`5545#section-3.3.11` is never reused.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from icalendar.error import XCalParsingError, XCalSerializationError

if TYPE_CHECKING:
    from xml.parsers.expat import XMLParserType

    from icalendar.cal.component import Component

#: The xCal namespace defined by :rfc:`6321#section-8`.
XCAL_NAMESPACE = "urn:ietf:params:xml:ns:icalendar-2.0"

#: Default maximum element nesting depth accepted by the parser.
DEFAULT_MAX_DEPTH = 100

#: Default maximum number of characters of text data accepted by the parser.
DEFAULT_MAX_TEXT_SIZE = 10 * 1024 * 1024

_PROPERTIES = "properties"
_COMPONENTS = "components"

#: Simple value types whose content is a single text node.
_TEXT_VALUE_TYPES = frozenset(
    {
        "text",
        "uri",
        "cal-address",
        "date",
        "date-time",
        "duration",
        "time",
        "utc-offset",
    }
)

#: Value types that carry a single Python scalar.
_SCALAR_VALUE_TYPES = frozenset({"boolean", "integer", "float"})

#: Recur parts rendered as direct text content.
_RECUR_TEXT_PARTS = frozenset(
    {"freq", "wkst", "count", "interval", "until"}
)

#: Recur parts rendered as repeated ``<integer>`` elements.
_RECUR_INTEGER_PARTS = frozenset(
    {
        "bysecond",
        "byminute",
        "byhour",
        "bymonthday",
        "byyearday",
        "byweekno",
        "bysetpos",
    }
)

#: Characters allowed in XML 1.0 text.
_XML_TEXT_ALLOWED = (
    {0x09, 0x0A, 0x0D}
    | set(range(0x20, 0xD7FF + 1))
    | set(range(0xE000, 0xFFFD + 1))
    | set(range(0x10000, 0x10FFFF + 1))
)


@dataclass
class _Element:
    """A minimal namespace-aware XML element used for both directions."""

    name: str
    children: list[_Element] = field(default_factory=list)
    text_parts: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(self.text_parts)


# ---------------------------------------------------------------------------
# XML output
# ---------------------------------------------------------------------------


def _escape_text(text: str) -> str:
    """Escape text using XML rules.

    The iCalendar TEXT backslash escaping is deliberately not used: xCal
    carries the logical value in an XML text node, so only XML escaping
    applies.
    """
    chunks = []
    for char in text:
        code = ord(char)
        if code not in _XML_TEXT_ALLOWED:
            raise XCalSerializationError(
                f"The character U+{code:04X} cannot be represented in XML 1.0 "
                "text.",
                value=text,
            )
        if char == "&":
            chunks.append("&amp;")
        elif char == "<":
            chunks.append("&lt;")
        elif char == ">":
            chunks.append("&gt;")
        elif char == "\r":
            # Escape CR so it survives XML line-ending normalization.
            chunks.append("&#xD;")
        else:
            chunks.append(char)
    return "".join(chunks)


def _format_scalar(value: Any) -> str:
    """Format a non-string jCal scalar for an XML text node."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    raise XCalSerializationError(
        "The scalar value must be a boolean, integer or float.", value=value
    )


#: Parameters typed as boolean in the RFC 6321 parameter schema.
_BOOLEAN_PARAMETERS = frozenset({"rsvp"})

#: Parameters typed as URI in the RFC 6321 parameter schema.
_URI_PARAMETERS = frozenset({"altrep", "dir"})

#: Parameters typed as calendar user addresses.
_CAL_ADDRESS_PARAMETERS = frozenset(
    {"delegated-from", "delegated-to", "member", "sent-by"}
)


def _parameter_value_type(name: str) -> str:
    """Return the xCal value element type for a parameter name."""
    if name in _BOOLEAN_PARAMETERS:
        return "boolean"
    if name in _URI_PARAMETERS:
        return "uri"
    if name in _CAL_ADDRESS_PARAMETERS:
        return "cal-address"
    # Text covers x-name and unknown IANA parameters, so their values are
    # preserved without interpretation and round-trip.
    return "text"


def _parameter_value_element(name: str, value: Any) -> _Element:
    """Build the typed inner element of one parameter value."""
    element_type = _parameter_value_type(name)
    if element_type == "boolean":
        if isinstance(value, bool):
            content = "true" if value else "false"
        else:
            content = "true" if str(value).upper() == "TRUE" else "false"
    elif isinstance(value, bool):
        content = _format_scalar(value)
    elif isinstance(value, int):
        content = str(value)
    elif isinstance(value, float):
        content = repr(value)
    else:
        content = _escape_text(str(value))
    inner = _Element(element_type)
    inner.text_parts.append(content)
    return inner


def _build_parameters(params: dict[str, Any]) -> _Element | None:
    """Build a ``<parameters>`` element from a jCal parameter mapping."""
    if not params:
        return None
    parameters = _Element("parameters")
    for name, value in params.items():
        values = value if isinstance(value, list) else [value]
        for item in values:
            parameter = _Element(name)
            parameter.children.append(_parameter_value_element(name, item))
            parameters.children.append(parameter)
    return parameters


def _leaf(tag: str, content: str) -> _Element:
    element = _Element(tag)
    element.text_parts.append(content)
    return element


def _build_period(value: Any) -> _Element:
    """Build a ``<period>`` element from a jCal period value."""
    if not isinstance(value, list) or len(value) != 2:
        raise XCalSerializationError(
            "A period value must be a list with exactly two strings.",
            value=value,
        )
    start, second = value
    if not isinstance(start, str) or not isinstance(second, str):
        raise XCalSerializationError(
            "A period value must contain strings.", value=value
        )
    period = _Element("period")
    period.children.append(_leaf("start", _escape_text(start)))
    if second.startswith(("P", "-P", "+P")):
        period.children.append(
            _leaf("duration", _escape_text(second))
        )
    else:
        period.children.append(_leaf("end", _escape_text(second)))
    return period


def _build_geo(value: Any) -> _Element:
    """Build a ``<geo>`` element from a jCal geo value."""
    if not isinstance(value, list) or len(value) != 2:
        raise XCalSerializationError(
            "A geo value must be a list with exactly two numbers.",
            value=value,
        )
    geo = _Element("geo")
    geo.children.append(_leaf("latitude", _format_scalar(value[0])))
    geo.children.append(_leaf("longitude", _format_scalar(value[1])))
    return geo


def _split_weekday(value: str) -> tuple[str | None, str]:
    """Split ``2MO``/``-1SU`` into an ordinal string and a weekday token."""
    index = 1 if value[:1] in "+-" else 0
    while index < len(value) and value[index].isdigit():
        index += 1
    if index == 0:
        return None, value
    return value[:index].lstrip("+"), value[index:]


def _build_recur(value: Any) -> _Element:
    """Build a ``<recur>`` element from a jCal recurrence mapping."""
    if not isinstance(value, dict):
        raise XCalSerializationError(
            "A recur value must be a mapping.", value=value
        )
    recur = _Element("recur")
    for part, part_value in value.items():
        if not isinstance(part, str):
            raise XCalSerializationError(
                "Recur part names must be strings.", value=part
            )
        if part == "byday":
            byday = _Element("byday")
            weekdays = part_value if isinstance(part_value, list) else [
                part_value
            ]
            for weekday in weekdays:
                ordinal, day = _split_weekday(str(weekday))
                day_element = _Element("day")
                if ordinal is not None:
                    day_element.children.append(
                        _leaf("ordwk", ordinal)
                    )
                day_element.children.append(
                    _leaf("weekday", day.lower())
                )
                byday.children.append(day_element)
            recur.children.append(byday)
        elif part in _RECUR_INTEGER_PARTS or part == "bymonth":
            numbers = part_value if isinstance(part_value, list) else [
                part_value
            ]
            for number in numbers:
                part_element = _Element(part)
                part_element.children.append(
                    _leaf("integer", _format_scalar(number))
                )
                recur.children.append(part_element)
        elif part in _RECUR_TEXT_PARTS:
            text = (
                part_value[0]
                if isinstance(part_value, list)
                else part_value
            )
            if part in ("freq", "wkst"):
                content = str(text).lower()
            else:
                content = str(text)
            recur.children.append(_leaf(part, _escape_text(content)))
        else:
            # Extension recur parts such as iana-recur-mod or x-name have no
            # element in the RFC 6321 schema. Fail explicitly instead of
            # inventing output a recipient could reject; the value is
            # attached for the caller to decide what to do.
            raise XCalSerializationError(
                f"The recurrence part {part!r} has no xCal representation "
                "according to RFC 6321.",
                value=part_value,
            )
    return recur


def _build_value_elements(
    value_type: str, values: list[Any]
) -> list[_Element]:
    """Build the xCal value element(s) for one property."""
    elements: list[_Element] = []
    if (
        value_type == "float"
        and len(values) == 1
        and isinstance(values[0], list)
    ):
        # vGeo serialises as ["geo", params, "float", [latitude, longitude]]
        # in jCal, while xCal uses a dedicated <geo> element.
        return [_build_geo(values[0])]
    if value_type in _TEXT_VALUE_TYPES:
        for value in values:
            if isinstance(value, list):
                # REQUEST-STATUS is a single jCal value holding a list of
                # text parts, rendered as repeated <text> elements.
                elements.extend(
                    _leaf(value_type, _escape_text(str(part)))
                    for part in value
                )
                continue
            elements.append(
                _leaf(value_type, _escape_text(str(value)))
            )
    elif value_type in _SCALAR_VALUE_TYPES:
        for value in values:
            elements.append(
                _leaf(value_type, _format_scalar(value))
            )
    elif value_type == "binary":
        for value in values:
            elements.append(
                _leaf("binary", _escape_text(str(value)))
            )
    elif value_type == "period":
        elements.extend(_build_period(value) for value in values)
    elif value_type == "recur":
        elements.extend(_build_recur(value) for value in values)
    else:
        # Types outside RFC 6321 (the "geo" type name never appears here
        # because vGeo serialises as "float") cannot be represented. Raising
        # gives callers a clear strategy: keep the value elsewhere or choose
        # a different serialization.
        raise XCalSerializationError(
            f"The jCal value type {value_type!r} has no xCal representation "
            "according to RFC 6321.",
            value=values,
        )
    return elements


def _build_property(prop: list[Any]) -> _Element:
    """Build a property element from a jCal property node."""
    name, params, value_type, *values = prop
    property_element = _Element(name)
    parameters = _build_parameters(params)
    if parameters is not None:
        property_element.children.append(parameters)
    if value_type == "unknown":
        # RFC 6321 represents non-standard (x-name) properties and any
        # property without a structured value as plain text. The vUnknown
        # value itself is preserved verbatim, so this round-trips.
        value_type = "text"
    property_element.children.extend(
        _build_value_elements(value_type, values)
    )
    return property_element


def _jcal_to_element_tree(root_jcal: list[Any]) -> _Element:
    """Build an ``<icalendar>`` element tree from a jCal component tree."""
    root = _Element("icalendar")
    stack: list[tuple[list[Any], _Element]] = [(root_jcal, root)]
    while stack:
        node, parent = stack.pop()
        name, properties, subcomponents = node
        component = _Element(name)
        parent.children.append(component)
        properties_element = _Element(_PROPERTIES)
        properties_element.children.extend(
            _build_property(prop_element) for prop_element in properties
        )
        component.children.append(properties_element)
        if subcomponents:
            components_element = _Element(_COMPONENTS)
            component.children.append(components_element)
            for subcomponent in reversed(subcomponents):
                stack.append((subcomponent, components_element))
    return root


def _render(root: _Element) -> str:
    """Render the element tree as indented xCal XML."""
    lines = ['<?xml version="1.0" encoding="utf-8"?>']

    def render_leaf(element: _Element, indent: int) -> None:
        prefix = "  " * indent
        text = "".join(element.text_parts)
        lines.append(
            f"{prefix}<{element.name}>{text}</{element.name}>"
        )

    stack: list[tuple[_Element, int, bool]] = [(root, 0, False)]
    while stack:
        element, indent, closing = stack.pop()
        prefix = "  " * indent
        if closing:
            lines.append(f"{prefix}</{element.name}>")
            continue
        if not element.children:
            render_leaf(element, indent)
            continue
        attributes = (
            f' xmlns="{XCAL_NAMESPACE}"' if element is root else ""
        )
        lines.append(f"{prefix}<{element.name}{attributes}>")
        stack.append((element, indent, True))
        child_indent = indent + 1
        for child in reversed(element.children):
            stack.append((child, child_indent, False))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# XML parsing (hardened expat)
# ---------------------------------------------------------------------------


class _XCalTreeBuilder:
    """Build :class:`_Element` trees from expat events with safety limits.

    Security properties:

    - A document type declaration is rejected, which blocks internal
      entities (entity-expansion / "billion laughs" attacks) outright.
    - External entity references, external entity declarations and
      notation declarations raise, so no external resource is ever fetched.
    - Element nesting and text size are capped.
    - Comments and processing instructions other than the XML declaration
      are rejected.
    - All elements must live in the xCal namespace and may not carry
      attributes (namespace declarations are consumed by expat).
    """

    def __init__(
        self,
        max_depth: int = DEFAULT_MAX_DEPTH,
        max_text_size: int = DEFAULT_MAX_TEXT_SIZE,
    ) -> None:
        self.max_depth = max_depth
        self.max_text_size = max_text_size
        self.text_size = 0
        self.root: _Element | None = None
        self._stack: list[_Element] = []
        self._parser: XMLParserType | None = None

    def parse(self, data: bytes | str) -> _Element:
        from xml.parsers import expat

        parser = expat.ParserCreate(namespace_separator=" ")
        self._parser = parser
        parser.StartElementHandler = self._start_element
        parser.EndElementHandler = self._end_element
        parser.CharacterDataHandler = self._character_data
        parser.ExternalEntityRefHandler = self._external_entity_ref
        # Reject any DOCTYPE, including external and parameter entities.
        parser.StartDoctypeDeclHandler = self._start_doctype
        # Defense in depth in case a DOCTYPE slips through another path.
        parser.EntityDeclHandler = self._entity_decl
        parser.NotationDeclHandler = self._notation_decl
        parser.CommentHandler = self._comment
        parser.ProcessingInstructionHandler = self._processing_instruction
        if isinstance(data, str):
            data = data.encode("utf-8")
        try:
            parser.Parse(data, True)
        except expat.ExpatError as error:
            raise XCalParsingError(
                f"Malformed XML at line {error.lineno}, column "
                f"{error.offset}: {expat.errors.messages[error.code]}"
            ) from error
        if self.root is None:
            raise XCalParsingError("The xCal document is empty.")
        return self.root

    def _reject(self, message: str) -> None:
        line = self._parser.CurrentLineNumber if self._parser else 0
        raise XCalParsingError(f"Line {line}: {message}")

    def _start_doctype(self, *args: Any) -> None:
        self._reject(
            "Document type declarations are not allowed in xCal."
        )

    def _external_entity_ref(self, *args: Any) -> None:
        self._reject("External entities are not allowed in xCal.")

    def _entity_decl(self, *args: Any) -> None:
        self._reject("Entity declarations are not allowed in xCal.")

    def _notation_decl(self, *args: Any) -> None:
        self._reject("Notation declarations are not allowed in xCal.")

    def _comment(self, data: str) -> None:
        self._reject("Comments are not allowed in xCal.")

    def _processing_instruction(self, target: str, data: str) -> None:
        self._reject(
            f"Processing instructions are not allowed in xCal ({target!r})."
        )

    def _start_element(self, full_name: str, attributes: dict) -> None:
        if " " in full_name:
            namespace, name = full_name.split(" ", 1)
        else:
            namespace, name = "", full_name
        if namespace != XCAL_NAMESPACE:
            self._reject(
                f"Element {name!r} must be in the xCal namespace "
                f"{XCAL_NAMESPACE!r}."
            )
        if attributes:
            self._reject(
                f"Element {name!r} must not carry attributes."
            )
        if len(self._stack) >= self.max_depth:
            self._reject(
                f"The element nesting depth exceeds the limit of "
                f"{self.max_depth}."
            )
        element = _Element(name)
        if self._stack:
            self._stack[-1].children.append(element)
        elif self.root is not None:
            self._reject("The document may only have one root element.")
        else:
            self.root = element
        self._stack.append(element)

    def _end_element(self, full_name: str) -> None:
        self._stack.pop()

    def _character_data(self, data: str) -> None:
        if not self._stack:
            self._reject("Text data outside of elements is not allowed.")
        self.text_size += len(data)
        if self.text_size > self.max_text_size:
            self._reject(
                f"The text data exceeds the limit of {self.max_text_size} "
                "characters."
            )
        self._stack[-1].text_parts.append(data)


# ---------------------------------------------------------------------------
# xCal to jCal conversion
# ---------------------------------------------------------------------------


def _only_child(element: _Element, name: str) -> _Element:
    """Return the unique child with ``name`` or raise."""
    matches = [child for child in element.children if child.name == name]
    if len(matches) != 1:
        raise XCalParsingError(
            f"The <{element.name}> element must contain exactly one "
            f"<{name}> element."
        )
    return matches[0]


def _require_no_text(element: _Element) -> None:
    if "".join(element.text_parts).strip():
        raise XCalParsingError(
            f"The <{element.name}> element must not contain text."
        )


def _parse_scalar(element: _Element) -> bool | int | float:
    """Parse the text of a boolean, integer or float parameter/value."""
    text = element.text
    if element.name == "boolean":
        if text == "true":
            return True
        if text == "false":
            return False
        raise XCalParsingError(
            f"A boolean value must be 'true' or 'false', got {text!r}."
        )
    if element.name == "integer":
        try:
            return int(text)
        except ValueError:
            raise XCalParsingError(
                f"An integer value must contain an integer, got {text!r}."
            ) from None
    if element.name == "float":
        try:
            return float(text)
        except ValueError:
            raise XCalParsingError(
                f"A float value must contain a number, got {text!r}."
            ) from None
    raise XCalParsingError(
        f"Unknown scalar value element <{element.name}>."
    )


def _parse_parameters(parameters: _Element) -> dict[str, Any]:
    """Convert a ``<parameters>`` element to a jCal parameter mapping."""
    _require_no_text(parameters)
    result: dict[str, Any] = {}
    for parameter in parameters.children:
        _require_no_text(parameter)
        values = []
        for value_element in parameter.children:
            if value_element.name in _SCALAR_VALUE_TYPES:
                values.append(_parse_scalar(value_element))
            elif value_element.name in (
                "text",
                "uri",
                "cal-address",
            ):
                values.append(value_element.text)
            else:
                raise XCalParsingError(
                    f"Parameter <{parameter.name}> contains an unsupported "
                    f"value element <{value_element.name}>."
                )
        result[parameter.name] = values[0] if len(values) == 1 else values
    return result


def _parse_value_element(element: _Element) -> tuple[str, Any]:
    """Convert one xCal value element to its ``(type, jcal_value)`` pair."""
    value_type = element.name
    if value_type in _TEXT_VALUE_TYPES:
        return value_type, element.text
    if value_type in _SCALAR_VALUE_TYPES:
        return value_type, _parse_scalar(element)
    if value_type == "binary":
        return "binary", element.text
    if value_type == "geo":
        # vGeo's jCal node uses the "float" type with one list value.
        return "float", _parse_geo(element)
    if value_type == "period":
        return "period", _parse_period(element)
    if value_type == "recur":
        return "recur", _parse_recur(element)
    # Value types outside of RFC 6321 cannot be mapped to a known property
    # class, so the explicit strategy is to name them in an error rather
    # than silently alter the data.
    raise XCalParsingError(
        f"The value element <{value_type}> is not defined by RFC 6321."
    )


def _parse_period(element: _Element) -> list[str]:
    _require_no_text(element)
    start = _only_child(element, "start")
    if any(child.name == "duration" for child in element.children):
        second = _only_child(element, "duration")
    else:
        second = _only_child(element, "end")
    return [start.text, second.text]


def _parse_number(element: _Element) -> float:
    try:
        return float(element.text)
    except ValueError:
        raise XCalParsingError(
            f"The <{element.name}> element must contain a number, got "
            f"{element.text!r}."
        ) from None


def _parse_geo(element: _Element) -> list[float]:
    _require_no_text(element)
    latitude = _only_child(element, "latitude")
    longitude = _only_child(element, "longitude")
    return [_parse_number(latitude), _parse_number(longitude)]


def _parse_recur(element: _Element) -> dict[str, Any]:
    _require_no_text(element)
    recur: dict[str, Any] = {}
    for part in element.children:
        if part.name == "byday":
            _require_no_text(part)
            weekdays: list[str] = []
            for day in part.children:
                ordinal = None
                weekday = None
                _require_no_text(day)
                for day_field in day.children:
                    if day_field.name == "ordwk":
                        ordinal = day_field.text
                    elif day_field.name == "weekday":
                        weekday = day_field.text
                    else:
                        raise XCalParsingError(
                            f"Unknown <day> child <{day_field.name}> in recur."
                        )
                if weekday is None:
                    raise XCalParsingError(
                        "Every recur <day> must contain a <weekday>."
                    )
                weekdays.append(
                    f"{ordinal if ordinal is not None else ''}"
                    f"{weekday.upper()}"
                )
            recur["byday"] = weekdays
        elif part.name in _RECUR_INTEGER_PARTS or part.name == "bymonth":
            integers = [
                _parse_scalar(child) for child in part.children
            ]
            recur[part.name] = integers
        elif part.name in _RECUR_TEXT_PARTS:
            if part.name in ("count", "interval"):
                try:
                    recur[part.name] = int(part.text)
                except ValueError:
                    raise XCalParsingError(
                        f"The recur <{part.name}> must contain an integer, "
                        f"got {part.text!r}."
                    ) from None
            else:
                recur[part.name] = part.text
        else:
            raise XCalParsingError(
                f"The recur element <{part.name}> is not defined by RFC 6321."
            )
    return recur


def _parse_property(element: _Element) -> list[Any]:
    """Convert a property element to a jCal property node."""
    _require_no_text(element)
    parameters_node = None
    value_elements = []
    for child in element.children:
        if child.name == "parameters":
            if parameters_node is not None:
                raise XCalParsingError(
                    f"Property <{element.name}> has multiple <parameters>."
                )
            parameters_node = child
        else:
            value_elements.append(child)
    if not value_elements:
        raise XCalParsingError(
            f"Property <{element.name}> must contain at least one value "
            "element."
        )
    params = (
        _parse_parameters(parameters_node)
        if parameters_node is not None
        else {}
    )
    first_type, first_value = _parse_value_element(value_elements[0])
    first_type = _adjust_property_value_type(element.name, first_type)
    node = [element.name, params, first_type, first_value]
    for value_element in value_elements[1:]:
        value_type, value = _parse_value_element(value_element)
        value_type = _adjust_property_value_type(element.name, value_type)
        if value_type != first_type:
            raise XCalParsingError(
                f"All values of property <{element.name}> must use the same "
                f"type, got {first_type!r} and {value_type!r}."
            )
        node.append(value)
    if (
        element.name == "request-status"
        and first_type == "text"
        and len(node) > 4
    ):
        # In jCal REQUEST-STATUS is one value containing a list of text
        # parts; xCal repeats <text> elements instead.
        node[3:] = [node[3:]]
    return node


def _adjust_property_value_type(property_name: str, value_type: str) -> str:
    """Map xCal text back to jCal ``unknown`` for unknown properties.

    RFC 6321 models non-standard and unregistered IANA properties as text.
    The property system's default type for such names is the RFC 7265
    reserved ``unknown`` type; using that here keeps the value verbatim and
    avoids inventing a ``VALUE=TEXT`` parameter on the way back.
    """
    if value_type != "text":
        return value_type
    from icalendar.prop import TypesFactory

    default_type = TypesFactory.instance().default_value_type(property_name)
    return "unknown" if default_type == "unknown" else "text"


def _element_to_jcal(element: _Element) -> list[Any]:
    """Convert a component element to a jCal component node."""
    _require_no_text(element)
    properties_element = _only_child(element, _PROPERTIES)
    _require_no_text(properties_element)
    properties: list[list[Any]] = [
        _parse_property(child) for child in properties_element.children
    ]
    subcomponents: list[list[Any]] = []
    component_wrappers = [
        child for child in element.children if child.name == _COMPONENTS
    ]
    if len(component_wrappers) > 1:
        raise XCalParsingError(
            f"Component <{element.name}> has multiple <components> elements."
        )
    if component_wrappers:
        _require_no_text(component_wrappers[0])
        subcomponents = [
            _element_to_jcal(child)
            for child in component_wrappers[0].children
        ]
    allowed = {_PROPERTIES, _COMPONENTS}
    for child in element.children:
        if child.name not in allowed:
            raise XCalParsingError(
                f"Component <{element.name}> contains an unexpected element "
                f"<{child.name}>"
            )
    return [element.name, properties, subcomponents]


def _xml_to_jcal(root: _Element) -> list[Any]:
    """Validate the root and convert the document to a jCal component node."""
    _require_no_text(root)
    if root.name != "icalendar":
        raise XCalParsingError(
            "The root element must be <icalendar>."
        )
    component_elements = list(root.children)
    if len(component_elements) != 1:
        raise XCalParsingError(
            "The <icalendar> element must contain exactly one component."
        )
    return _element_to_jcal(component_elements[0])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def component_to_xcal_bytes(component: Component) -> bytes:
    """Serialize a component to an xCal document as UTF-8 bytes.

    Parameters:
        component: The component to serialize, typically a
            :class:`~icalendar.Calendar`.

    Raises:
        XCalSerializationError: If a value cannot be represented in xCal.
    """
    jcal = component.to_jcal()
    tree = _jcal_to_element_tree(jcal)
    return (_render(tree) + "\n").encode("utf-8")


def component_to_xcal(component: Component) -> str:
    """Serialize a component to an xCal document as a string.

    See :func:`component_to_xcal_bytes` for the byte form.
    """
    return component_to_xcal_bytes(component).decode("utf-8")


def xcal_to_component(
    xcal: bytes | str,
    starting_cls: type[Component] | None = None,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_text_size: int = DEFAULT_MAX_TEXT_SIZE,
) -> Component:
    """Parse an xCal document into a component.

    Parameters:
        xcal: The xCal XML document as bytes or text.
        starting_cls: The component class used as the entry point for
            structural validation and component lookup. Defaults to
            :class:`~icalendar.Calendar`. The actual class is resolved
            from the document's component name, so unknown IANA- and
            X-components are created through the component factory.
        max_depth: Maximum permitted element nesting depth.
        max_text_size: Maximum permitted number of characters of text
            data across the whole document.

    Raises:
        XCalParsingError: If the XML or xCal structure is invalid, uses a
            different namespace or a value type that has no iCalendar
            representation.
    """
    from icalendar.cal import Calendar

    starting_cls = starting_cls or Calendar
    builder = _XCalTreeBuilder(
        max_depth=max_depth, max_text_size=max_text_size
    )
    tree = builder.parse(xcal)
    jcal = _xml_to_jcal(tree)
    try:
        return starting_cls.from_jcal(jcal)
    except Exception as error:
        # Surface jCal/property errors with the xCal type so callers only
        # have to handle xCal errors on this boundary.
        raise XCalParsingError(
            f"The xCal document is semantically invalid: {error}"
        ) from error


__all__ = [
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_MAX_TEXT_SIZE",
    "XCAL_NAMESPACE",
    "XCalParsingError",
    "XCalSerializationError",
    "component_to_xcal",
    "component_to_xcal_bytes",
    "xcal_to_component",
]
