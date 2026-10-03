"""Conversion between :class:`~icalendar.cal.component.Component` and xCal.

xCal is defined in :rfc:`6321`.  This module deliberately uses only
``xml.etree.ElementTree`` and the expat parser from the Python standard
library; no third-party XML parser is required.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
import xml.etree.ElementTree as ET
from xml.parsers import expat

from icalendar.error import InvalidCalendar
from icalendar.parser import Parameters
from icalendar.parser.string import validate_token
from icalendar.parser_tools import DEFAULT_ENCODING
from icalendar.prop.binary import vBinary
from icalendar.prop.boolean import vBoolean
from icalendar.prop.cal_address import vCalAddress
from icalendar.prop.categories import vCategory
from icalendar.prop.dt.date import vDate
from icalendar.prop.dt.datetime import vDatetime
from icalendar.prop.dt.duration import vDuration
from icalendar.prop.dt.list import vDDDLists
from icalendar.prop.dt.types import vDDDTypes
from icalendar.prop.dt.period import vPeriod
from icalendar.prop.dt.time import vTime
from icalendar.prop.dt.utc_offset import vUTCOffset
from icalendar.prop.geo import vGeo
from icalendar.prop.recur.month import vMonth
from icalendar.prop.recur.recur import vRecur
from icalendar.prop.unknown import vUnknown
from icalendar.prop.uri import vUri
from icalendar.prop.factory import TypesFactory
from icalendar.timezone import tzp

if TYPE_CHECKING:
    from xml.etree.ElementTree import Element

    from icalendar.cal.component import Component


XCAL_NAMESPACE = "urn:ietf:params:xml:ns:icalendar-2.0"
ET.register_namespace("", XCAL_NAMESPACE)
DEFAULT_MAX_DEPTH = 200
DEFAULT_MAX_TEXT_SIZE = 10 * 1024 * 1024

SCALAR_VALUE_TAGS = frozenset(
    {
        "binary",
        "boolean",
        "cal-address",
        "date",
        "date-time",
        "duration",
        "float",
        "integer",
        "text",
        "time",
        "uri",
        "utc-offset",
        "uid",
        "xml-reference",
    }
)

STRUCTURED_VALUE_TAGS = frozenset({"geo", "period", "recur"})

RECUR_TEXT_PARTS = frozenset({"freq", "rscale", "skip", "wkst"})
RECUR_LIST_PARTS = frozenset(
    {
        "bysecond",
        "byminute",
        "byhour",
        "byday",
        "byweekday",
        "bymonthday",
        "byyearday",
        "byweekno",
        "bymonth",
        "bysetpos",
    }
)
RECUR_SCALAR_PARTS = RECUR_TEXT_PARTS | frozenset({"count", "interval", "until"})

class XCalParsingError(InvalidCalendar):
    """An xCal document could not be parsed."""


def xcal_tag(name: str) -> str:
    """Return an xCal element name in the xCal namespace."""
    return f"{{{XCAL_NAMESPACE}}}{name}"


def _local_name(element: Element) -> str:
    """Return the local name of an xCal element."""
    tag = element.tag
    if not isinstance(tag, str):
        raise XCalParsingError("All xCal elements must have a string tag.")
    namespace, has_separator, local = (
        tag[1:].partition("}") if tag.startswith("{") else ("", False, tag)
    )
    if not has_separator or namespace != XCAL_NAMESPACE:
        raise XCalParsingError(
            f"Expected the xCal namespace {XCAL_NAMESPACE!r}, got "
            f"{namespace or '<no namespace>'!r}."
        )
    return local


def _require_name(name: str, kind: str) -> str:
    """Validate and normalize an xCal component/property/parameter name."""
    normalized = name.lower()
    try:
        validate_token(normalized)
    except ValueError as e:
        raise XCalParsingError(f"Invalid {kind} name: {name!r}.") from e
    return normalized


def _element_text(element: Element) -> str:
    """Return all direct text of an element with no element children."""
    if list(element):
        raise XCalParsingError(
            f"<{_local_name(element)}> must not contain child elements."
        )
    if element.text is None:
        return ""
    return element.text


def _children(element: Element) -> list[Element]:
    """Return xCal child elements and reject whitespace or direct text."""
    children = list(element)
    if (element.text or "").strip() or any(
        (child.tail or "").strip() for child in children
    ):
        raise XCalParsingError(
            f"<{_local_name(element)}> contains text outside its child elements."
        )
    return children


def _sub_element(
    parent: Element, name: str, text: Any | None = None
) -> Element:
    """Create a namespaced child element."""
    element = ET.SubElement(parent, xcal_tag(name))
    if text is not None:
        element.text = "" if text is None else str(text)
    return element


class _LimitedTreeBuilder(ET.TreeBuilder):
    """TreeBuilder that enforces nesting and text size limits."""

    def __init__(self, max_depth: int, max_text_size: int) -> None:
        super().__init__()
        self.max_depth = max_depth
        self.max_text_size = max_text_size
        self.depth = 0
        self.text_size = 0

    def start(self, tag: str, attrs: dict[str, str]) -> Any:
        self.depth += 1
        if self.depth > self.max_depth:
            raise XCalParsingError(
                f"xCal nesting depth exceeds the limit of {self.max_depth}."
            )
        return super().start(tag, attrs)

    def end(self, tag: str) -> Any:
        result = super().end(tag)
        self.depth -= 1
        return result

    def data(self, data: str) -> None:
        self.text_size += len(data)
        if self.text_size > self.max_text_size:
            raise XCalParsingError(
                f"xCal text size exceeds the limit of {self.max_text_size} bytes."
            )
        super().data(data)


class _DoctypeRejectingParser:
    """Expat parser with all document type declarations disabled."""

    def __init__(self, max_depth: int, max_text_size: int) -> None:
        self.builder = _LimitedTreeBuilder(max_depth, max_text_size)
        self.parser = expat.ParserCreate(encoding="UTF-8", namespace_separator="\x1f")
        self.parser.StartElementHandler = self.start
        self.parser.EndElementHandler = self.end
        self.parser.CharacterDataHandler = self.builder.data
        self.parser.StartDoctypeDeclHandler = self.doctype
        self.parser.EntityDeclHandler = self.entity_declaration
        self.parser.UnparsedEntityDeclHandler = self.unparsed_entity_declaration
        self.parser.ExternalEntityRefHandler = self.external_entity
        self.parser.NotationDeclHandler = self.notation

    def parse(self, data: bytes) -> Element:
        self.parser.Parse(data, True)
        return self.builder.close()

    def start(self, name: str, attributes: dict[str, str]) -> None:
        if attributes:
            raise XCalParsingError("xCal elements must not have attributes.")
        if "\x1f" in name:
            namespace, local = name.split("\x1f", 1)
            tag = f"{{{namespace}}}{local}"
        else:
            tag = name
        namespaced_attributes = {}
        for key, value in attributes.items():
            if "\x1f" in key:
                namespace, local = key.split("\x1f", 1)
                key = f"{{{namespace}}}{local}"
            namespaced_attributes[key] = value
        self.builder.start(tag, namespaced_attributes)

    def end(self, name: str) -> None:
        if "\x1f" in name:
            namespace, local = name.split("\x1f", 1)
            name = f"{{{namespace}}}{local}"
        self.builder.end(name)

    @staticmethod
    def namespace_decl(prefix: str | None, uri: str) -> None:
        # Namespaces are processed by expat; no external resource is fetched.
        return None

    @staticmethod
    def doctype(*args: Any) -> None:
        raise XCalParsingError("DTD declarations are not allowed in xCal input.")

    @staticmethod
    def entity_declaration(*args: Any) -> None:
        raise XCalParsingError("Entity declarations are not allowed in xCal input.")

    @staticmethod
    def unparsed_entity_declaration(*args: Any) -> None:
        raise XCalParsingError(
            "Unparsed entity declarations are not allowed in xCal input."
        )

    @staticmethod
    def external_entity(*args: Any) -> bool:
        raise XCalParsingError("External entities are not allowed in xCal input.")

    @staticmethod
    def notation(*args: Any) -> None:
        raise XCalParsingError("Notation declarations are not allowed in xCal input.")


def parse_xcal_xml(
    source: str | bytes | Path,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_text_size: int = DEFAULT_MAX_TEXT_SIZE,
) -> Element:
    """Parse xCal XML safely into an ElementTree document."""
    if isinstance(source, Path):
        source = source.read_bytes()
    elif isinstance(source, str):
        source = source.encode(DEFAULT_ENCODING)
    if len(source) > max_text_size:
        raise XCalParsingError(
            f"xCal input size exceeds the limit of {max_text_size} bytes."
        )
    parser = _DoctypeRejectingParser(max_depth, max_text_size)
    try:
        root = parser.parse(source)
    except XCalParsingError:
        raise
    except (expat.ExpatError, ValueError, TypeError) as e:
        raise XCalParsingError(f"Could not parse xCal XML: {e}") from e
    if _local_name(root) != "icalendar":
        raise XCalParsingError("The root element must be <icalendar>.")
    if root.attrib:
        raise XCalParsingError("<icalendar> must not have attributes.")
    return root



def _jcal_value_type(prop: Any) -> str:
    """Return the xCal/jCal type name for an encoded property."""
    if isinstance(prop, vDDDLists):
        if prop.dts:
            return prop.dts[0].to_jcal("x")[2]
        return "date-time"
    if isinstance(prop, vGeo):
        return "geo"
    if isinstance(prop, vCategory):
        return "text"
    value_type = str(prop.VALUE).lower()
    if value_type in SCALAR_VALUE_TAGS or value_type in STRUCTURED_VALUE_TAGS:
        return value_type
    if value_type in {"date-time-list"}:
        return "date-time"
    if value_type != "unknown":
        return value_type
    return "unknown"


def _jcal_value(prop: Any) -> Any:
    """Return the jCal-style value payload for an encoded property."""
    jcal = prop.to_jcal("x")
    return jcal[3:] if len(jcal) > 4 else jcal[3]


def _format_datetime(value: datetime) -> str:
    """Format a datetime as an xCal date-time value."""
    return vDatetime(value).to_jcal("x")[3]


def _format_date(value: date) -> str:
    """Format a date as an xCal date value."""
    return vDate(value).to_jcal("x")[3]


def _format_time(value: time) -> str:
    """Format a time as an xCal time value."""
    return vTime(value).to_jcal("x")[3]


def _format_duration(value: timedelta) -> str:
    """Format a timedelta as an xCal duration value."""
    return vDuration(value).to_jcal("x")[3]


def _add_scalar_value(parent: Element, tag: str, value: Any) -> None:
    """Append a scalar xCal value element."""
    if tag == "boolean":
        text = "true" if bool(value) else "false"
    elif tag == "binary":
        if isinstance(value, vBinary):
            text = value.base64data
        elif isinstance(value, bytes):
            import base64

            text = base64.b64encode(value).decode("ascii")
        else:
            text = str(value)
    else:
        text = str(value)
    _sub_element(parent, tag, text)


def _add_period_value(parent: Element, prop: vPeriod) -> None:
    """Append a structured period value."""
    period = _sub_element(parent, "period")
    _sub_element(period, "start", _format_datetime(prop.start))
    if prop.by_duration:
        _sub_element(period, "duration", _format_duration(prop.duration))
    else:
        _sub_element(period, "end", _format_datetime(prop.end))


def _add_recur_part(recur_element: Element, key: str, value: Any) -> None:
    """Append one recurrence rule part."""
    part = _sub_element(recur_element, key)
    values = value if isinstance(value, list) else [value]
    if key == "until":
        values = values[0] if values else None
        if values is not None:
            _sub_element(part, "date-time", _format_datetime(values))
        return
    for item in values:
        is_integer_part = key in {"count", "interval"} or key in RECUR_LIST_PARTS
        if isinstance(item, vMonth):
            if item.leap:
                _sub_element(part, "text", str(item))
            else:
                _sub_element(part, "integer", int(item))
        elif is_integer_part and isinstance(item, int) and not isinstance(item, bool):
            _sub_element(part, "integer", int(item))
        elif isinstance(item, timedelta):
            _sub_element(part, "duration", _format_duration(item))
        else:
            _sub_element(part, "text", str(item))


def _add_recur_value(parent: Element, prop: vRecur) -> None:
    """Append a structured recurrence value."""
    recur = _sub_element(parent, "recur")
    for key, value in prop.items():
        _add_recur_part(recur, key.lower(), value)


def _add_geo_value(parent: Element, prop: vGeo) -> None:
    """Append a structured geographic position value."""
    geo = _sub_element(parent, "geo")
    _sub_element(geo, "latitude", str(prop.latitude))
    _sub_element(geo, "longitude", str(prop.longitude))


def _add_typed_values(property_element: Element, prop: Any, name: str = "") -> None:
    """Append the value element or elements for a property."""
    if isinstance(prop, vDDDTypes):
        _add_typed_values(property_element, prop.to_property_type(), name)
        return
    tag = _jcal_value_type(prop)
    if isinstance(prop, vPeriod):
        _add_period_value(property_element, prop)
    elif isinstance(prop, vRecur):
        _add_recur_value(property_element, prop)
    elif isinstance(prop, vGeo):
        _add_geo_value(property_element, prop)
    elif isinstance(prop, vUTCOffset):
        _sub_element(property_element, "utc-offset", prop.format(":"))
    elif isinstance(prop, vDDDLists):
        for item in prop.dts:
            _add_typed_values(
                property_element, item.to_property_type() if hasattr(item, "to_property_type") else item, name
            )
    elif isinstance(prop, vCategory):
        for category in prop.cats:
            _add_scalar_value(property_element, "text", str(category))
    elif name == "request-status":
        for field in str(prop).split(";", 2):
            _add_scalar_value(property_element, "text", field)
    elif type(prop).__name__ in {"vN", "vOrg", "vAdr"}:
        for field in prop.fields:
            _add_scalar_value(property_element, "text", str(field))
    elif tag == "period":
        value = _jcal_value(prop)
        period = _sub_element(property_element, "period")
        _sub_element(period, "start", value[0])
        end_tag = "duration" if str(value[1]).startswith(("P", "-P", "+P")) else "end"
        _sub_element(period, end_tag, value[1])
    elif tag == "recur":
        _add_recur_value(property_element, prop)
    elif tag == "geo":
        _add_geo_value(property_element, prop)
    elif isinstance(prop, vBinary):
        _add_scalar_value(property_element, "binary", prop.base64data)
    else:
        text = _jcal_value(prop) if tag in {"date-time", "date", "time", "duration", "integer", "float"} else str(prop)
        _add_scalar_value(property_element, tag, text)


def _parameter_type(parameter_name: str, value: Any) -> str:
    """Select the xCal type for a property parameter value."""
    if isinstance(value, vBoolean) or isinstance(value, bool) or parameter_name.lower() in {"rsvp"}:
        return "boolean"
    if isinstance(value, vPeriod):
        return "period"
    if isinstance(value, vRecur):
        return "recur"
    if isinstance(value, vGeo):
        return "geo"
    if isinstance(value, vCalAddress):
        return "cal-address"
    if isinstance(value, vUri):
        return "uri"
    if isinstance(value, vUTCOffset):
        return "utc-offset"
    if isinstance(value, timedelta):
        return "duration"
    if isinstance(value, datetime):
        return "date-time"
    if isinstance(value, date):
        return "date"
    if isinstance(value, time):
        return "time"
    if isinstance(value, int) and not isinstance(value, bool):
        return "integer"
    if isinstance(value, float):
        return "float"
    if isinstance(value, vBinary):
        return "binary"
    if hasattr(value, "VALUE"):
        value_type = str(value.VALUE).lower()
        if value_type in SCALAR_VALUE_TAGS or value_type in STRUCTURED_VALUE_TAGS:
            return value_type
        return value_type
    type_class = TypesFactory.instance().for_property(parameter_name)
    value_type = str(getattr(type_class, "default_value", "TEXT")).lower()
    if value_type in SCALAR_VALUE_TAGS or value_type in STRUCTURED_VALUE_TAGS:
        return value_type
    return "text"


def _add_parameter_value(parameters_element: Element, name: str, value: Any) -> None:
    """Append one parameter and its typed value or values."""
    if name.lower() == "value" or (
        name.lower() == "encoding" and str(value).upper() == "BASE64"
    ):
        return
    parameter = _sub_element(parameters_element, _require_name(name, "parameter"))
    values = value if isinstance(value, list) else [value]
    if not values:
        values = [""]
    for item in values:
        tag = _parameter_type(name, item)
        if isinstance(item, timedelta) and tag == "duration":
            _sub_element(parameter, "duration", _format_duration(item))
        elif tag in STRUCTURED_VALUE_TAGS:
            if tag == "period" and isinstance(item, vPeriod):
                _add_period_value(parameter, item)
            elif tag == "recur" and isinstance(item, vRecur):
                _add_recur_value(parameter, item)
            elif tag == "geo" and isinstance(item, vGeo):
                _add_geo_value(parameter, item)
            else:
                _add_scalar_value(parameter, "unknown", str(item))
        else:
            _add_scalar_value(parameter, tag, item)


def _property_to_xcal(parent: Element, name: str, prop: Any) -> None:
    """Append one property to an xCal properties container."""
    property_element = _sub_element(parent, _require_name(name, "property"))
    params = getattr(prop, "params", None)
    if params:
        parameters_element = _sub_element(property_element, "parameters")
        for parameter_name, parameter_value in params.items():
            _add_parameter_value(parameters_element, parameter_name, parameter_value)
        if not list(parameters_element):
            property_element.remove(parameters_element)
    _add_typed_values(property_element, prop)


def _component_to_xcal(parent: Element, component: Component) -> Element:
    """Append one component and all descendants without Python recursion."""
    component_element = ET.SubElement(
        parent,
        xcal_tag(_require_name(component.name or "vcalendar", "component")),
    )
    properties_element = _sub_element(component_element, "properties")
    for name, value in component.items():
        values = value if isinstance(value, list) else [value]
        for item in values:
            _property_to_xcal(properties_element, name, item)
    components_element = _sub_element(component_element, "components")
    stack: list[tuple[Element, Component]] = [
        (components_element, child) for child in reversed(component.subcomponents)
    ]
    while stack:
        child_parent, child = stack.pop()
        child_element = ET.SubElement(
            child_parent,
            xcal_tag(_require_name(child.name or "vcalendar", "component")),
        )
        child_properties = _sub_element(child_element, "properties")
        for name, value in child.items():
            values = value if isinstance(value, list) else [value]
            for item in values:
                _property_to_xcal(child_properties, name, item)
        child_components = _sub_element(child_element, "components")
        for grandchild in reversed(child.subcomponents):
            stack.append((child_components, grandchild))
    return component_element


def property_to_xcal_element(name: str, prop: Any) -> Element:
    """Return one property as an xCal property element."""
    element = ET.Element(xcal_tag("property"))
    _property_to_xcal(element, name, prop)
    return element[0]


def component_to_xcal_element(component: Component) -> Element:
    """Return an xCal ``<icalendar>`` element for a component."""
    root = ET.Element(xcal_tag("icalendar"))
    _component_to_xcal(root, component)
    return root


def component_to_xcal(component: Component, *, xml_declaration: bool = False) -> bytes:
    """Serialize a component to an xCal XML document."""
    return ET.tostring(
        component_to_xcal_element(component),
        encoding="UTF-8" if xml_declaration else "utf-8",
        xml_declaration=xml_declaration,
        short_empty_elements=True,
    )

def _scalar_jcal_value(tag: str, text: str) -> Any:
    """Convert scalar xCal text to the value used by existing jCal parsers."""
    if tag == "boolean":
        normalized = text.strip().lower()
        if normalized not in {"true", "false"}:
            raise XCalParsingError(f"Invalid boolean value: {text!r}.")
        return normalized == "true"
    if tag == "integer":
        try:
            return int(text)
        except ValueError as e:
            raise XCalParsingError(f"Invalid integer value: {text!r}.") from e
    if tag == "float":
        try:
            return float(text)
        except ValueError as e:
            raise XCalParsingError(f"Invalid float value: {text!r}.") from e
    return text


def _require_value_tag(tag: str) -> str:
    """Validate an xCal value element name."""
    try:
        validate_token(tag)
    except ValueError as e:
        raise XCalParsingError(f"Invalid value type name: {tag!r}.") from e
    if tag not in SCALAR_VALUE_TAGS and tag not in STRUCTURED_VALUE_TAGS:
        return tag
    return tag


def _parse_period(element: Element) -> list[str]:
    """Parse a structured period into its jCal list form."""
    children = _children(element)
    if len(children) != 2 or _local_name(children[0]) != "start":
        raise XCalParsingError("<period> must contain <start> and <end> or <duration>.")
    start = _element_text(children[0])
    second_name = _local_name(children[1])
    second = _element_text(children[1])
    if second_name not in {"end", "duration"}:
        raise XCalParsingError("<period> must end with <end> or <duration>.")
    return [start, second]


def _parse_recur_part_value(tag: str, element: Element) -> Any:
    """Parse one value element inside a recurrence rule part."""
    text = _element_text(element)
    return _scalar_jcal_value(tag, text)


def _parse_recur(element: Element) -> dict[str, Any]:
    """Parse a structured recurrence rule into its jCal mapping form."""
    result: dict[str, Any] = {}
    for part in _children(element):
        name = _require_name(_local_name(part), "recurrence rule part")
        value_children = _children(part)
        if name == "until":
            if len(value_children) != 1 or _local_name(value_children[0]) != "date-time":
                raise XCalParsingError("<until> must contain one <date-time>.")
            result[name] = _element_text(value_children[0])
            continue
        values = [
            _parse_recur_part_value(_local_name(child), child)
            for child in value_children
        ]
        if not values:
            raise XCalParsingError(f"<{name}> must contain at least one value.")
        if name in RECUR_SCALAR_PARTS:
            result[name] = values[0]
        else:
            result[name] = values
    return result


def _parse_geo(element: Element) -> list[float]:
    """Parse a structured geo value into its jCal list form."""
    children = _children(element)
    if [_local_name(child) for child in children] != ["latitude", "longitude"]:
        raise XCalParsingError("<geo> must contain <latitude> and <longitude>.")
    try:
        return [float(_element_text(children[0])), float(_element_text(children[1]))]
    except ValueError as e:
        raise XCalParsingError("<geo> coordinates must be floats.") from e


def _parse_value_element(element: Element) -> tuple[str, Any]:
    """Parse one value element to a ``(jCal type, jCal value)`` pair."""
    tag = _require_value_tag(_local_name(element))
    if tag == "period":
        return "period", _parse_period(element)
    if tag == "recur":
        return "recur", _parse_recur(element)
    if tag == "geo":
        return "float", _parse_geo(element)
    if tag in SCALAR_VALUE_TAGS:
        text = _element_text(element)
        if tag == "binary":
            text = "".join(text.split())
        return tag, _scalar_jcal_value(tag, text)
    if not list(element):
        return tag, _element_text(element)
    raise XCalParsingError(
        f"Unsupported structured extension value type {tag!r}; extension types "
        "must use a scalar value element to preserve their raw VALUE type."
    )


def _parse_parameter(parameter_element: Element) -> tuple[str, list[Any]]:
    """Parse one xCal parameter element."""
    name = _require_name(_local_name(parameter_element), "parameter")
    values: list[Any] = []
    for value_element in _children(parameter_element):
        tag, value = _parse_value_element(value_element)
        if tag in STRUCTURED_VALUE_TAGS:
            raise XCalParsingError(
                f"Parameter {name!r} cannot use structured value type {tag!r}."
            )
        if tag == "boolean":
            value = value if name == "rsvp" else vBoolean(value)
        elif tag == "date-time":
            value = vDatetime.parse_jcal_value(value)
        elif tag == "date":
            value = vDate.parse_jcal_value(value)
        elif tag == "time":
            value = vTime.parse_jcal_value(value)
        elif tag == "duration":
            value = vDuration.parse_jcal_value(value)
        elif tag == "utc-offset":
            value = vUTCOffset.from_jcal(["x", {}, "utc-offset", value]).td
        elif tag not in SCALAR_VALUE_TAGS and tag not in STRUCTURED_VALUE_TAGS:
            value = vUnknown(str(value), params={"VALUE": tag.upper()})
        values.append(value)
    if not values:
        raise XCalParsingError(f"Parameter {name!r} must contain a value.")
    return name, values


def _parse_parameters(parameters_element: Element) -> dict[str, Any]:
    """Parse an xCal parameters container."""
    result: dict[str, Any] = {}
    for parameter in _children(parameters_element):
        name, values = _parse_parameter(parameter)
        result[name] = values[0] if len(values) == 1 else values
    return result


def _parse_property(
    property_element: Element, component_cls: type[Component]
) -> tuple[str, Any]:
    """Parse one xCal property into an iCalendar property object."""
    name = _require_name(_local_name(property_element), "property")
    children = _children(property_element)
    params: dict[str, Any] = {}
    if children and _local_name(children[0]) == "parameters":
        params = _parse_parameters(children[0])
        children = children[1:]
    if not children:
        raise XCalParsingError(f"Property {name!r} must contain a value.")

    parsed_values = [_parse_value_element(child) for child in children]
    value_type, first_value = parsed_values[0]
    if any(value_type != item[0] for item in parsed_values):
        raise XCalParsingError(f"Property {name!r} contains mixed value types.")

    if value_type not in SCALAR_VALUE_TAGS and value_type not in STRUCTURED_VALUE_TAGS:
        if len(parsed_values) != 1:
            raise XCalParsingError(
                f"Extension value type {value_type!r} cannot contain multiple values."
            )
        prop = vUnknown(first_value, params=params)
        prop.VALUE = value_type.upper()
        return name, prop

    factory = component_cls.types_factory.for_property(name, value_type.upper())
    xcal_params = Parameters(params)
    jcal_params = {}
    if all(
        isinstance(item, (str, int, float, bool))
        for value in params.values()
        for item in (value if isinstance(value, list) else [value])
    ):
        jcal_params = params
    if name in {"rdate", "exdate"}:
        jcal = [name, jcal_params, value_type, *(value for _, value in parsed_values)]
    elif name == "categories":
        jcal = [name, jcal_params, "text", *(value for _, value in parsed_values)]
    elif name == "request-status" and value_type == "text":
        jcal = [name, jcal_params, "text", [value for _, value in parsed_values]]
    elif factory.__name__ in {"vN", "vOrg", "vAdr"}:
        jcal = [name, jcal_params, "text", *(value for _, value in parsed_values)]
    else:
        jcal = [name, jcal_params, value_type, first_value]
    try:
        prop = factory.from_jcal(jcal)
    except Exception as error:
        raise XCalParsingError(str(error)) from error
    prop.params = xcal_params
    tzid = xcal_params.tzid
    has_typed_parameter = jcal_params != params
    if tzid and has_typed_parameter:
        if isinstance(prop, vDDDTypes) and isinstance(prop.dt, datetime):
            prop.dt = tzp.localize(prop.dt, tzid)
        elif isinstance(prop, vDDDLists):
            for item in prop.dts:
                if hasattr(item, "dt") and isinstance(item.dt, datetime):
                    item.dt = tzp.localize(item.dt, tzid)
        elif isinstance(prop, vPeriod):
            start = tzp.localize(prop.start, tzid)
            end_or_duration = (
                prop.duration if prop.by_duration else tzp.localize(prop.end, tzid)
            )
            prop = vPeriod((start, end_or_duration), params=xcal_params)
    default_type = component_cls.types_factory.default_value_type(name)
    if value_type != default_type and value_type != "unknown":
        prop.VALUE = value_type.upper()
    return name, prop


def _parse_component_element(
    element: Element, parent_cls: type[Component]
) -> tuple[Component, list[Element]]:
    """Parse one component element without attaching child components."""
    name = _require_name(_local_name(element), "component")
    component_cls = parent_cls.get_component_class(name.upper())
    component = component_cls()
    sections = _children(element)
    section_names = [_local_name(section) for section in sections]
    if section_names not in (["properties", "components"], ["properties"]):
        raise XCalParsingError(
            f"<{name}> must contain <properties> and optionally <components>."
        )
    properties_element = sections[0]
    for property_element in _children(properties_element):
        property_name, prop = _parse_property(property_element, component_cls)
        component.add(property_name, prop, encode=False)
    child_elements: list[Element] = []
    if len(sections) == 2:
        child_elements = _children(sections[1])
    return component, child_elements


def component_from_xcal(
    source: str | bytes | Path | Element,
    *,
    starting_cls: type[Component] | None = None,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_text_size: int = DEFAULT_MAX_TEXT_SIZE,
) -> Component:
    """Parse an xCal document into a :class:`Component`."""
    if starting_cls is None:
        from icalendar.cal.component import Component

        starting_cls = Component
    root = (
        source
        if isinstance(source, ET.Element)
        else parse_xcal_xml(
            source, max_depth=max_depth, max_text_size=max_text_size
        )
    )
    roots = _children(root)
    if len(roots) != 1:
        raise XCalParsingError("<icalendar> must contain exactly one root component.")
    root_component, root_children = _parse_component_element(roots[0], starting_cls)
    stack: list[tuple[Component, list[Element]]] = [(root_component, root_children)]
    while stack:
        parent, child_elements = stack.pop()
        for child_element in child_elements:
            child, grandchildren = _parse_component_element(child_element, type(parent))
            parent.add_component(child)
            stack.append((child, grandchildren))
    return root_component


__all__ = [
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_MAX_TEXT_SIZE",
    "XCAL_NAMESPACE",
    "XCalParsingError",
    "component_from_xcal",
    "component_to_xcal",
    "component_to_xcal_element",
    "parse_xcal_xml",
    "property_to_xcal_element",
]
