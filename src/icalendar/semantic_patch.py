"""Semantic diffs and transactional patches for iCalendar components."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from icalendar.error import SemanticPatchConflict
from icalendar.parser.content_line import Contentlines, _strip_ows_around_delimiters
from icalendar.parser.parameter import (
    Parameters,
    param_value,
    q_split,
    rfc_6868_unescape,
)
from icalendar.parser.property import split_on_unescaped_comma, unescape_backslash
from icalendar.parser.string import validate_token
from icalendar.parser_tools import DEFAULT_ENCODING, to_unicode
from icalendar.prop import TypesFactory, vCategory, vDDDLists, vDDDTypes, vUnknown
from icalendar.timezone import tzp

if TYPE_CHECKING:
    from icalendar.cal.component import Component

PATCH_FORMAT = "icalendar-semantic-patch/1"
DATETIME_PROPERTIES = {"DTSTART", "DTEND", "RECURRENCE-ID", "DUE", "RDATE", "EXDATE"}
LIST_DATE_PROPERTIES = {"RDATE", "EXDATE"}


class OrderedParameters(Parameters):
    """Parameters that retain repeated parameter occurrences in order."""

    def __init__(
        self,
        entries: list[tuple[str, str | list[str]]] | None = None,
    ) -> None:
        entries = [(name.upper(), value) for name, value in entries or []]
        super().__init__(entries)
        self.entries = entries

    def to_ical(self, sorted: bool = True):  # noqa: A002
        decorated = []
        for key, value in self.entries:
            if key == "TZID" and value == "UTC":
                continue
            always_quote = key in self.always_quoted
            quoted_value = param_value(value, always_quote=always_quote)
            if isinstance(quoted_value, str):
                quoted_value = quoted_value.encode(DEFAULT_ENCODING)
            decorated.append((key, quoted_value))
        if sorted:
            decorated.sort(key=lambda item: item[0])
        return b";".join(
            key.encode(DEFAULT_ENCODING) + b"=" + value for key, value in decorated
        )


@dataclass
class SemanticProperty:
    name: str
    value: str
    parameters: list[tuple[str, str | list[str]]]
    value_type: str


@dataclass
class SemanticNode:
    name: str
    properties: list[SemanticProperty] = field(default_factory=list)
    children: list[SemanticNode] = field(default_factory=list)


Json = dict[str, Any]


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _decode(value: bytes | str) -> str:
    return to_unicode(value, encoding=DEFAULT_ENCODING)


def _parameter_entries(section: str) -> list[tuple[str, str | list[str]]]:
    section = _strip_ows_around_delimiters(section)
    if not section:
        return []
    entries = []
    for parameter in q_split(section, ";"):
        parts = q_split(parameter, "=", maxsplit=1)
        if len(parts) != 2:
            raise ValueError(f"{parameter!r} is not a valid parameter")
        name = parts[0].strip()
        validate_token(name)
        values = []
        for token in q_split(parts[1], ","):
            if token.startswith('"') and token.endswith('"'):
                values.append(rfc_6868_unescape(token[1:-1]))
            else:
                values.append(rfc_6868_unescape(token))
        entries.append((name.upper(), values[0] if len(values) == 1 else values))
    return entries


def _first_parameter(
    entries: list[tuple[str, str | list[str]]], name: str
) -> str | None:
    for parameter_name, value in entries:
        if parameter_name == name.upper():
            return value if isinstance(value, str) else value[0]
    return None


def _line_parts(line: str) -> tuple[str, str, str]:
    name_end = None
    value_end = None
    in_quotes = False
    for index, char in enumerate(line):
        if char == '"':
            in_quotes = not in_quotes
        elif not in_quotes:
            if name_end is None and char in ":;":
                name_end = index
            if char == ":":
                value_end = index
                break
    if value_end is None:
        raise ValueError(f"Content line has no value: {line!r}")
    name = line[:name_end] if name_end is not None else line
    name = name.strip().replace(" ", "").replace("\t", "")
    validate_token(name)
    parameter_section = line[name_end + 1 : value_end] if name_end is not None else ""
    return name.upper(), parameter_section, line[value_end + 1 :]


def _infer_value_type(name: str, obj: object) -> str:
    explicit = getattr(obj, "params", None)
    if explicit is not None and "VALUE" in explicit:
        value = explicit["VALUE"]
        if isinstance(value, list):
            value = value[0]
        return str(value).upper()
    if isinstance(obj, vDDDLists):
        value = obj._get_value()
        if value is not None:
            return str(value).upper()
    if isinstance(obj, vDDDTypes):
        return str(obj.VALUE).upper()
    value = getattr(obj, "VALUE", None)
    if value is not None and not callable(value):
        return str(value).upper()
    return TypesFactory.instance().default_value_type(name).upper()


def _make_property_object(
    name: str,
    raw_value: str,
    entries: list[tuple[str, str | list[str]]],
    effective_type: str | None = None,
    *,
    parse: bool,
) -> list[tuple[object, list[tuple[str, str | list[str]]], str | None]]:
    parameters = OrderedParameters(entries)
    explicit_type = effective_type or _first_parameter(entries, "VALUE")
    factory = TypesFactory.instance().for_property(name, explicit_type)
    tzid = _first_parameter(entries, "TZID") if name in DATETIME_PROPERTIES else None
    fallback_type = None

    if explicit_type and explicit_type.upper() == "UNKNOWN":
        obj = vUnknown(raw_value)
    elif parse and name == "CATEGORIES":
        obj = vCategory(split_on_unescaped_comma(raw_value))
    elif parse and name in LIST_DATE_PROPERTIES:
        parsed = (
            factory.from_ical(raw_value, tzid)
            if tzid
            else factory.from_ical(raw_value)
        )
        obj = factory(parsed)
    elif parse and name == "FREEBUSY":
        result = []
        for part in raw_value.split(","):
            parsed = factory.from_ical(part)
            single = factory(parsed)
            single.params = OrderedParameters(entries)
            result.append((single, entries, None))
        return result
    else:
        factory_raw = raw_value
        if not hasattr(factory, "get_value_from_content_line"):
            factory_raw = unescape_backslash(raw_value)
        try:
            if parse and tzid:
                parsed = factory.from_ical(factory_raw, tzid)
            else:
                parsed = factory.from_ical(factory_raw)
            obj = factory(parsed)
        except (ValueError, TypeError):
            obj = vUnknown(raw_value)
            fallback_type = (
                explicit_type
                or TypesFactory.instance().default_value_type(name).upper()
            )
    obj.params = parameters
    return [(obj, entries, fallback_type)]


def _semantic_property(
    name: str,
    raw_value: str,
    entries: list[tuple[str, str | list[str]]],
) -> list[SemanticProperty]:
    objects = _make_property_object(name, raw_value, entries, parse=True)
    result = []
    for obj, object_entries, fallback_type in objects:
        ordered_entries = [entry for _, entry in sorted(
            enumerate(object_entries),
            key=lambda item: (item[1][0], item[0]),
        )]
        encoded = obj.to_ical()
        result.append(
            SemanticProperty(
                name=name,
                value=_decode(encoded),
                parameters=ordered_entries,
                value_type=fallback_type
                or _first_parameter(entries, "VALUE")
                or _infer_value_type(name, obj),
            )
        )
    return result


def parse_semantic_calendar(data: str | bytes | Component) -> SemanticNode:
    """Parse an iCalendar document into an ordered semantic tree."""
    if not isinstance(data, (str, bytes)):
        data = data.to_ical(sorted=False)
    lines = Contentlines.from_ical(data)
    stack: list[SemanticNode] = []
    roots: list[SemanticNode] = []
    for content_line in lines:
        if not content_line:
            continue
        name, parameter_section, raw_value = _line_parts(str(content_line))
        if name == "BEGIN":
            stack.append(SemanticNode(raw_value.upper()))
            continue
        if name == "END":
            if not stack or stack[-1].name != raw_value.upper():
                raise ValueError(f"Unmatched END:{raw_value}")
            node = stack.pop()
            if stack:
                stack[-1].children.append(node)
            else:
                roots.append(node)
            if node.name == "VTIMEZONE":
                tzp.cache_timezone_component(build_component(node))
            continue
        if not stack:
            raise ValueError(f"Property {name} outside a component")
        entries = _parameter_entries(parameter_section)
        stack[-1].properties.extend(_semantic_property(name, raw_value, entries))
    if stack:
        raise ValueError(f"Missing END:{stack[-1].name}")
    if len(roots) == 1:
        return roots[0]
    virtual = SemanticNode("VCALENDAR")
    virtual.children = roots
    return virtual


def _parameter_summary(entries: list[tuple[str, str | list[str]]]) -> list[Json]:
    return [{"name": name, "value": value} for name, value in entries]


def property_summary(prop: SemanticProperty) -> Json:
    return {
        "name": prop.name,
        "parameters": _parameter_summary(prop.parameters),
        "value_type": prop.value_type,
        "value": prop.value,
    }


def _summary_from_entries(entries: list[Json]) -> list[tuple[str, str | list[str]]]:
    return [(entry["name"], entry["value"]) for entry in entries]


def property_from_summary(summary: Json) -> SemanticProperty:
    return SemanticProperty(
        name=str(summary["name"]).upper(),
        value=str(summary["value"]),
        parameters=_summary_from_entries(summary.get("parameters", [])),
        value_type=str(summary.get("value_type", "UNKNOWN")).upper(),
    )


def component_summary(node: SemanticNode) -> Json:
    properties = sorted(
        (property_summary(prop) for prop in node.properties),
        key=_json,
    )
    children = sorted(
        (component_summary(child) for child in node.children), key=_json
    )
    return {"component": node.name, "properties": properties, "components": children}


def node_from_summary(summary: Json) -> SemanticNode:
    return SemanticNode(
        name=str(summary["component"]).upper(),
        properties=[
            property_from_summary(item)
            for item in summary.get("properties", [])
        ],
        children=[
            node_from_summary(item) for item in summary.get("components", [])
        ],
    )


def _property_key(prop: SemanticProperty) -> str:
    return _json(property_summary(prop))


def _component_key(node: SemanticNode) -> tuple[str, object] | None:
    uid = None
    recurrence_id = None
    for prop in node.properties:
        if prop.name == "UID":
            uid = unescape_backslash(prop.value)
        elif prop.name == "RECURRENCE-ID":
            recurrence_id = property_summary(prop)
    if uid is not None:
        return "uid", (node.name, uid, _json(recurrence_id))
    if node.name == "VTIMEZONE":
        for prop in node.properties:
            if prop.name == "TZID":
                return "tzid", (node.name, prop.value)
    return None


def _unique_stable_keys(nodes: list[SemanticNode]) -> set[tuple[str, object]]:
    counts: dict[tuple[str, object], int] = {}
    for node in nodes:
        key = _component_key(node)
        if key is not None:
            counts[key] = counts.get(key, 0) + 1
    return {key for key, count in counts.items() if count == 1}


def _same_name_ordinal(parent: SemanticNode, node: SemanticNode) -> int:
    """Return the ordinal among siblings with the same component name."""
    same_name = [child for child in parent.children if child.name == node.name]
    return same_name.index(node)


def _locator(
    node: SemanticNode,
    ordinal: int,
) -> Json:
    key = _component_key(node)
    if key is not None:
        locator: Json = {"component": node.name}
        if key[0] == "uid":
            _, (_, uid, recurrence_json) = key
            locator["uid"] = uid
            recurrence = json.loads(recurrence_json)
            if recurrence is not None:
                locator["recurrence_id"] = recurrence
        else:
            _, (_, tzid) = key
            locator["tzid"] = tzid
        return locator
    return {"component": node.name, "ordinal": ordinal}


def _property_changes(
    old: list[SemanticProperty],
    new: list[SemanticProperty],
) -> list[Json]:
    changes = []
    names = {prop.name for prop in old} | {prop.name for prop in new}
    for name in names:
        old_by_key: dict[str, list[tuple[int, SemanticProperty]]] = {}
        new_by_key: dict[str, list[SemanticProperty]] = {}
        ordinal = -1
        for prop in old:
            if prop.name == name:
                ordinal += 1
                old_by_key.setdefault(_property_key(prop), []).append(
                    (ordinal, prop)
                )
        for prop in new:
            if prop.name == name:
                new_by_key.setdefault(_property_key(prop), []).append(prop)

        for key, values in old_by_key.items():
            common = min(len(values), len(new_by_key.get(key, [])))
            del values[:common]
            if key in new_by_key:
                del new_by_key[key][:common]

        old_left = [value for values in old_by_key.values() for value in values]
        new_left = [value for values in new_by_key.values() for value in values]
        old_left.sort(key=lambda item: item[0])
        new_left.sort(key=new.index)
        pair_count = min(len(old_left), len(new_left))
        for index in range(pair_count):
            ordinal, old_prop = old_left[index]
            changes.append(
                {
                    "op": "update_property",
                    "locator": {"property": name, "ordinal": ordinal},
                    "old": property_summary(old_prop),
                    "new": property_summary(new_left[index]),
                }
            )
        for ordinal, prop in old_left[pair_count:]:
            changes.append(
                {
                    "op": "delete_property",
                    "locator": {"property": name, "ordinal": ordinal},
                    "old": property_summary(prop),
                }
            )
        for prop in new_left[pair_count:]:
            changes.append(
                {"op": "add_property", "old": None, "new": property_summary(prop)}
            )
    changes.sort(key=lambda change: _json(change))
    return changes


def _component_changes(old: SemanticNode, new: SemanticNode) -> list[Json]:
    changes = _property_changes(old.properties, new.properties)
    old_stable = _unique_stable_keys(old.children)
    new_stable = _unique_stable_keys(new.children)
    old_by_key = {
        _component_key(child): child
        for child in old.children
        if _component_key(child) in old_stable
    }
    new_by_key = {
        _component_key(child): child
        for child in new.children
        if _component_key(child) in new_stable
    }
    for key in old_stable - new_stable:
        old_node = old_by_key[key]
        changes.append(
            {
                "op": "delete_component",
                "locator": _locator(old_node, _same_name_ordinal(old, old_node)),
                "old": component_summary(old_node),
            }
        )
    for key in new_stable - old_stable:
        new_node = new_by_key[key]
        changes.append(
            {
                "op": "add_component",
                "locator": _locator(new_node, _same_name_ordinal(new, new_node)),
                "old": None,
                "new": component_summary(new_node),
            }
        )
    for key in old_stable & new_stable:
        old_node = old_by_key[key]
        new_node = new_by_key[key]
        if component_summary(old_node) != component_summary(new_node):
            nested = _component_changes(old_node, new_node)
            changes.append(
                {
                    "op": "update_component",
                    "locator": _locator(old_node, _same_name_ordinal(old, old_node)),
                    "old": component_summary(old_node),
                    "changes": nested,
                }
            )

    old_unstable = [
        child for child in old.children if _component_key(child) not in old_stable
    ]
    new_unstable = [
        child for child in new.children if _component_key(child) not in new_stable
    ]
    old_by_summary: dict[str, list[SemanticNode]] = {}
    new_by_summary: dict[str, list[SemanticNode]] = {}
    for child in old_unstable:
        old_by_summary.setdefault(_json(component_summary(child)), []).append(child)
    for child in new_unstable:
        new_by_summary.setdefault(_json(component_summary(child)), []).append(child)
    for summary in set(old_by_summary) | set(new_by_summary):
        values = old_by_summary.get(summary, [])
        others = new_by_summary.get(summary, [])
        common = min(len(values), len(others))
        del values[:common]
        del others[:common]
    old_left = [child for values in old_by_summary.values() for child in values]
    new_left = [child for values in new_by_summary.values() for child in values]
    old_left.sort(key=lambda child: old.children.index(child))
    new_left.sort(key=lambda child: new.children.index(child))
    pair_count = min(len(old_left), len(new_left))
    for index in range(pair_count):
        old_child = old_left[index]
        new_child = new_left[index]
        if component_summary(old_child) != component_summary(new_child):
            changes.append(
                {
                    "op": "update_component",
                    "locator": {
                        "component": old_child.name,
                        "ordinal": _same_name_ordinal(old, old_child),
                    },
                    "old": component_summary(old_child),
                    "changes": _component_changes(old_child, new_child),
                }
            )
    for child in old_left[pair_count:]:
        changes.append(
            {
                "op": "delete_component",
                "locator": {
                    "component": child.name,
                    "ordinal": _same_name_ordinal(old, child),
                },
                "old": component_summary(child),
            }
        )
    for child in new_left[pair_count:]:
        changes.append(
            {
                "op": "add_component",
                "locator": {
                    "component": child.name,
                    "ordinal": _same_name_ordinal(new, child),
                },
                "old": None,
                "new": component_summary(child),
            }
        )
    return sorted(changes, key=_json)


def make_semantic_patch(
    old: str | bytes | Component,
    new: str | bytes | Component,
) -> Json:
    """Create a JSON-serializable semantic patch between two calendars."""
    old_node = parse_semantic_calendar(old)
    new_node = parse_semantic_calendar(new)
    return {
        "format": PATCH_FORMAT,
        "old": component_summary(old_node),
        "changes": _component_changes(old_node, new_node),
        "new": component_summary(new_node),
    }

def build_property(prop: SemanticProperty) -> list[object]:
    """Build serializable property value objects from a semantic property."""
    entries = [(name.upper(), value) for name, value in prop.parameters]
    objects = _make_property_object(
        prop.name,
        prop.value,
        entries,
        prop.value_type,
        parse=True,
    )
    result = []
    for obj, _, _ in objects:
        obj.params = OrderedParameters(entries)
        result.append(obj)
    return result


def build_component(node: SemanticNode) -> Component:
    """Build a normal icalendar component from a semantic node."""
    from icalendar.cal.component import Component

    component = Component.get_component_class(node.name)()
    component.name = node.name
    for prop in node.properties:
        for value in build_property(prop):
            component.add(prop.name, value, encode=False)
    for child in node.children:
        component.add_component(build_component(child))
    return component


def semantic_calendar_to_ical(node: SemanticNode) -> bytes:
    """Serialize a semantic tree to folded iCalendar bytes."""
    return build_component(node).to_ical(sorted=False)


def _uid_value(node: SemanticNode) -> str | None:
    for prop in node.properties:
        if prop.name == "UID":
            return unescape_backslash(prop.value)
    return None


def _recurrence_id(node: SemanticNode) -> Json | None:
    for prop in node.properties:
        if prop.name == "RECURRENCE-ID":
            return property_summary(prop)
    return None


def _matches_locator(node: SemanticNode, locator: Json) -> bool:
    if node.name != str(locator.get("component", "")).upper():
        return False
    if "uid" in locator:
        if _uid_value(node) != locator["uid"]:
            return False
        expected_recurrence = locator.get("recurrence_id")
        if _json(_recurrence_id(node)) != _json(expected_recurrence):
            return False
    if "tzid" in locator:
        tzid = next(
            (prop.value for prop in node.properties if prop.name == "TZID"), None
        )
        if tzid != locator["tzid"]:
            return False
    return True


def _find_child(parent: SemanticNode, locator: Json) -> SemanticNode | None:
    matches = [
        child
        for child in parent.children
        if _matches_locator(child, locator)
    ]
    if "uid" in locator or "tzid" in locator:
        if len(matches) == 1:
            return matches[0]
        return None
    ordinal = int(locator.get("ordinal", 0))
    return matches[ordinal] if ordinal < len(matches) else None


def _conflict(operation: Json, message: str, actual: object = None) -> Json:
    result = {"message": message, "operation": operation}
    if actual is not None:
        result["actual"] = actual
    return result


def _find_property_index(parent: SemanticNode, summary: Json) -> int | None:
    key = _json(summary)
    for index, prop in enumerate(parent.properties):
        if prop.name == summary["name"] and _json(property_summary(prop)) == key:
            return index
    return None


def _find_located_property_index(parent: SemanticNode, change: Json) -> int | None:
    locator = change.get("locator")
    name = str(change["old"]["name"]).upper()
    if not locator:
        return _find_property_index(parent, change["old"])
    if "property" not in locator:
        return _find_property_index(parent, change["old"])
    same_name = [
        index
        for index, prop in enumerate(parent.properties)
        if prop.name == name
    ]
    ordinal = int(locator.get("ordinal", 0))
    if ordinal >= len(same_name):
        return None
    index = same_name[ordinal]
    if _json(property_summary(parent.properties[index])) == _json(change["old"]):
        return index
    return None


def _apply_property_changes(
    parent: SemanticNode,
    changes: list[Json],
    conflicts: list[Json],
) -> None:
    def priority(change: Json) -> int:
        return {"update_property": 0, "delete_property": 1, "add_property": 2}[
            change["op"]
        ]

    for change in sorted(changes, key=priority):
        if change["op"] == "update_property":
            index = _find_located_property_index(parent, change)
            if index is None:
                conflicts.append(_conflict(change, "Old property was not found."))
                continue
            parent.properties[index] = property_from_summary(change["new"])
        elif change["op"] == "delete_property":
            index = _find_located_property_index(parent, change)
            if index is None:
                conflicts.append(_conflict(change, "Deleted property was not found."))
                continue
            del parent.properties[index]
        else:
            parent.properties.append(property_from_summary(change["new"]))


def _apply_component_changes(
    parent: SemanticNode,
    changes: list[Json],
    conflicts: list[Json],
) -> None:
    def priority(change: Json) -> int:
        return {"update_component": 0, "delete_component": 1, "add_component": 2}[
            change["op"]
        ]

    ordered = sorted(changes, key=priority)
    for change in ordered:
        locator = change.get("locator", {})
        if change["op"] == "update_component":
            child = _find_child(parent, locator)
            if child is None:
                conflicts.append(_conflict(change, "Target component was not found."))
                continue
            actual = component_summary(child)
            if actual != change["old"]:
                conflicts.append(
                    _conflict(change, "Component precondition does not match.", actual)
                )
                continue
            _apply_changes(child, change.get("changes", []), conflicts)
        elif change["op"] == "delete_component":
            child = _find_child(parent, locator)
            if child is None:
                conflicts.append(_conflict(change, "Deleted component was not found."))
                continue
            actual = component_summary(child)
            if actual != change["old"]:
                conflicts.append(
                    _conflict(
                        change,
                        "Deleted component precondition does not match.",
                        actual,
                    )
                )
                continue
            parent.children.remove(child)
        else:
            child = node_from_summary(change["new"])
            if (
                "uid" in locator or "tzid" in locator
            ) and _find_child(parent, locator):
                conflicts.append(_conflict(change, "Added component already exists."))
                continue
            if "ordinal" in locator:
                same_name = [
                    item
                    for item in parent.children
                    if item.name == child.name
                ]
                position = int(locator["ordinal"])
                if position >= len(parent.children):
                    parent.children.append(child)
                elif same_name and position < len(same_name):
                    parent.children.insert(
                        parent.children.index(same_name[position]), child
                    )
                else:
                    parent.children.append(child)
            else:
                parent.children.append(child)


def _apply_changes(
    parent: SemanticNode,
    changes: list[Json],
    conflicts: list[Json],
) -> None:
    property_changes = [
        change for change in changes if change["op"].endswith("_property")
    ]
    component_changes = [
        change for change in changes if change["op"].endswith("_component")
    ]
    _apply_property_changes(parent, property_changes, conflicts)
    _apply_component_changes(parent, component_changes, conflicts)


def _as_patch(patch: Json | str) -> Json:
    if isinstance(patch, str):
        return json.loads(patch)
    return patch


def apply_semantic_patch(
    calendar: str | bytes | Component,
    patch: Json | str,
) -> Component:
    """Apply a semantic patch, returning a new unchanged-on-conflict component."""
    patch_data = _as_patch(patch)
    if patch_data.get("format") != PATCH_FORMAT:
        raise ValueError("Unsupported semantic patch format")
    node = parse_semantic_calendar(calendar)
    conflicts: list[Json] = []
    actual_summary = component_summary(node)
    if actual_summary != patch_data.get("old"):
        conflicts.append(
            {
                "message": "Calendar precondition does not match.",
                "operation": {"op": "update_calendar"},
                "actual": actual_summary,
            }
        )
    candidate = deepcopy(node)
    _apply_changes(candidate, patch_data.get("changes", []), conflicts)
    if not conflicts:
        resulting = component_summary(candidate)
        if resulting != patch_data.get("new"):
            conflicts.append(
                {
                    "message": "Result does not match the target calendar summary.",
                    "operation": {"op": "update_calendar"},
                    "actual": resulting,
                }
            )
    if conflicts:
        raise SemanticPatchConflict(conflicts)
    return build_component(candidate)


__all__ = [
    "SemanticPatchConflict",
    "apply_semantic_patch",
    "build_component",
    "component_summary",
    "make_semantic_patch",
    "parse_semantic_calendar",
    "semantic_calendar_to_ical",
]
