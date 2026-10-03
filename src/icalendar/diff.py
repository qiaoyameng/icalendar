"""Semantic diff and patch for iCalendar components.

This module compares two versions of a calendar semantically instead of
textually.  The resulting *patch* describes which properties and
subcomponents were added, removed, or changed, and can be applied to the
common base version to reproduce the target calendar.

Components are located by ``UID`` and ``RECURRENCE-ID`` when they have
one, and by an explicit position in the component hierarchy otherwise.
Every operation carries a digest of the object it modifies as a
precondition, so applying a patch to a copy that was edited in the
meantime fails with a :class:`PatchConflictError` instead of silently
producing a half-applied calendar.

Patches are plain JSON-serializable dictionaries; they contain no Python
objects.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from icalendar.cal.component import Component
from icalendar.error import InvalidCalendar
from icalendar.parser import Parameters
from icalendar.prop import TypesFactory, vUnknown

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "PATCH_FORMAT",
    "PatchConflictError",
    "apply_patch",
    "calendar_digest",
    "diff_calendars",
]

#: Identifier of the patch format produced and consumed by this module.
PATCH_FORMAT = "icalendar-diff/1"


class PatchConflictError(InvalidCalendar):
    """A patch could not be applied because a precondition did not hold.

    The :attr:`conflicts` attribute is a JSON-serializable list of
    dictionaries, one per failed precondition, describing which operation
    failed and why.  When this error is raised, the calendar passed to
    :func:`apply_patch` is guaranteed to be unmodified.
    """

    def __init__(self, conflicts: list[dict[str, Any]]) -> None:
        self.conflicts = conflicts
        summary = "; ".join(
            f"op {c.get('op_index')} ({c.get('op')}): {c.get('reason')}"
            for c in conflicts[:3]
        )
        if len(conflicts) > 3:
            summary += f"; ... and {len(conflicts) - 3} more"
        super().__init__(
            f"Cannot apply patch: {len(conflicts)} precondition conflict(s). {summary}"
        )


class _Conflict(Exception):
    """Internal carrier for a single failed precondition."""

    def __init__(self, info: dict[str, Any]) -> None:
        self.info = info
        super().__init__(info.get("reason", "conflict"))


_TYPE_CLASSES = {t.__name__: t for t in TypesFactory.instance().all_types}


########################################################################
# Snapshots: JSON-serializable, lossless descriptions of components


def _component_name(component: Component) -> str:
    return (component.name or type(component).__name__).upper()


def _to_ical_str(value: Any) -> str:
    """Serialize a property value to its iCalendar string form."""
    ical = value.to_ical() if hasattr(value, "to_ical") else value
    if isinstance(ical, bytes):
        return ical.decode("utf-8", "surrogateescape")
    return str(ical)


def _param_values(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


def _property_snapshot(name: str, value: Any) -> dict[str, Any]:
    """Describe one property occurrence without losing information.

    Repeated properties, parameter lists, unknown ``X-`` properties, and
    the concrete value type are all preserved.
    """
    params = getattr(value, "params", None) or {}
    return {
        "name": str(name).upper(),
        "value_type": type(value).__name__,
        "value": _to_ical_str(value),
        "params": {
            str(key).upper(): _param_values(item) for key, item in params.items()
        },
    }


def _property_snapshots(component: Component) -> list[dict[str, Any]]:
    """All property occurrences of a component, in document order."""
    snapshots = []
    for name in component.keys():
        values = component[name]
        if not isinstance(values, list):
            values = [values]
        for value in values:
            snapshots.append(_property_snapshot(name, value))
    return snapshots


def _component_snapshot(component: Component) -> dict[str, Any]:
    """A complete JSON-serializable snapshot of a component."""
    return {
        "name": _component_name(component),
        "properties": _property_snapshots(component),
        "components": [_component_snapshot(sub) for sub in component.subcomponents],
    }


########################################################################
# Canonical forms: insensitive to non-semantic differences


def _canonical_property(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Canonical form of a property snapshot.

    Property and parameter names are already uppercased; the order of
    parameter values is not semantic, so they are sorted here.
    """
    return {
        "name": snapshot["name"],
        "value_type": snapshot["value_type"],
        "value": snapshot["value"],
        "params": {key: sorted(vals) for key, vals in snapshot["params"].items()},
    }


def _sort_key(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True)


def _canonical_component(component: Component) -> dict[str, Any]:
    """Canonical form of a component.

    Property and subcomponent order are not semantic, so both are sorted.
    """
    properties = [
        _canonical_property(snapshot) for snapshot in _property_snapshots(component)
    ]
    properties.sort(key=_sort_key)
    subcomponents = [_canonical_component(sub) for sub in component.subcomponents]
    subcomponents.sort(key=_sort_key)
    return {
        "name": _component_name(component),
        "properties": properties,
        "components": subcomponents,
    }


def calendar_digest(component: Component) -> str:
    """A semantic digest of a component and everything it contains.

    Two components have the same digest if and only if they are
    semantically equal: property and parameter name case, property order,
    parameter order, parameter value order, subcomponent order, and line
    folding are all ignored, while values, value types, repeated
    properties, and parameter lists are significant.
    """
    canonical = json.dumps(_canonical_component(component), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8", "surrogateescape")).hexdigest()


########################################################################
# Component identity and locator paths


def _scalar(value: Any) -> Any:
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _identity(component: Component) -> dict[str, Any] | None:
    """The stable identity of a component, or ``None`` if it has none."""
    uid = _scalar(component.get("UID"))
    if uid is None:
        return None
    recurrence_id = _scalar(component.get("RECURRENCE-ID"))
    return {
        "name": _component_name(component),
        "uid": _to_ical_str(uid),
        "recurrence_id": _to_ical_str(recurrence_id)
        if recurrence_id is not None
        else None,
    }


def _sibling_positions(subcomponents: list[Component]) -> list[int]:
    """Position of each subcomponent among its same-name siblings."""
    counts: dict[str, int] = {}
    positions = []
    for sub in subcomponents:
        name = _component_name(sub)
        positions.append(counts.get(name, 0))
        counts[name] = positions[-1] + 1
    return positions


def _resolve_path(root: Component, path: list[dict[str, Any]]) -> Component:
    """Walk a locator path from the root to the addressed component."""
    current = root
    for step in path:
        name = step["name"].upper()
        candidates = [
            sub for sub in current.subcomponents if _component_name(sub) == name
        ]
        if "uid" in step:
            resolved = None
            for candidate in candidates:
                identity = _identity(candidate)
                if (
                    identity is not None
                    and identity["uid"] == step["uid"]
                    and identity["recurrence_id"] == step.get("recurrence_id")
                ):
                    resolved = candidate
                    break
            if resolved is None:
                raise _Conflict({"reason": "component_not_found", "step": step})
            current = resolved
        else:
            index = step["index"]
            if index >= len(candidates):
                raise _Conflict({"reason": "component_not_found", "step": step})
            current = candidates[index]
    return current


########################################################################
# Diff


def _named_instances(component: Component) -> dict[str, list[dict[str, Any]]]:
    """Property snapshots of a component, grouped by name, in order."""
    instances: dict[str, list[dict[str, Any]]] = {}
    for snapshot in _property_snapshots(component):
        instances.setdefault(snapshot["name"], []).append(snapshot)
    return instances


def _diff_properties(
    old: Component,
    new: Component,
    old_digest: str,
    path: list[dict[str, Any]],
    ops: list[dict[str, Any]],
) -> None:
    """Emit operations for property differences between two components."""
    old_instances = _named_instances(old)
    new_instances = _named_instances(new)
    for name in sorted(set(old_instances) | set(new_instances)):
        remaining_old = list(enumerate(old_instances.get(name, [])))
        remaining_new = []
        # Multiset matching: identical occurrences pair up and are not
        # mentioned in the patch at all.
        for new_index, new_snapshot in enumerate(new_instances.get(name, [])):
            for entry in remaining_old:
                if _canonical_property(entry[1]) == _canonical_property(new_snapshot):
                    remaining_old.remove(entry)
                    break
            else:
                remaining_new.append((new_index, new_snapshot))
        if len(remaining_old) == 1 and len(remaining_new) == 1:
            # Exactly one occurrence on each side: this is a change.
            old_index, old_snapshot = remaining_old[0]
            ops.append(
                {
                    "op": "set_property",
                    "path": path,
                    "component_digest": old_digest,
                    "name": name,
                    "index": old_index,
                    "old": old_snapshot,
                    "new": remaining_new[0][1],
                }
            )
            continue
        for old_index, old_snapshot in remaining_old:
            ops.append(
                {
                    "op": "remove_property",
                    "path": path,
                    "component_digest": old_digest,
                    "name": name,
                    "index": old_index,
                    "old": old_snapshot,
                }
            )
        for _new_index, new_snapshot in remaining_new:
            ops.append(
                {
                    "op": "add_property",
                    "path": path,
                    "component_digest": old_digest,
                    "property": new_snapshot,
                }
            )


def _diff_subcomponents(
    old: Component,
    new: Component,
    old_digest: str,
    path: list[dict[str, Any]],
    ops: list[dict[str, Any]],
) -> None:
    """Emit operations for subcomponent differences between two components."""
    old_subs = list(old.subcomponents)
    new_subs = list(new.subcomponents)
    old_positions = _sibling_positions(old_subs)
    matched_old: set[int] = set()
    matched_new: set[int] = set()

    # 1. Components with a stable identity (UID + RECURRENCE-ID) are
    #    matched by that identity, wherever they appear.
    old_by_identity: dict[str, list[int]] = {}
    for old_index, sub in enumerate(old_subs):
        identity = _identity(sub)
        if identity is not None:
            old_by_identity.setdefault(_sort_key(identity), []).append(old_index)
    for new_index, sub in enumerate(new_subs):
        identity = _identity(sub)
        if identity is None:
            continue
        candidates = old_by_identity.get(_sort_key(identity))
        if candidates:
            old_index = candidates.pop(0)
            matched_old.add(old_index)
            matched_new.add(new_index)
            _diff_component(old_subs[old_index], sub, path + [identity], ops)

    # 2. Identical components without identity pair up unchanged.
    remaining_old = [i for i in range(len(old_subs)) if i not in matched_old]
    remaining_new = [j for j in range(len(new_subs)) if j not in matched_new]
    old_canonical = {i: _canonical_component(old_subs[i]) for i in remaining_old}
    for new_index in remaining_new:
        new_canonical = _canonical_component(new_subs[new_index])
        for old_index in remaining_old:
            if old_index not in matched_old and old_canonical[old_index] == new_canonical:
                matched_old.add(old_index)
                matched_new.add(new_index)
                break

    # 3. Remaining components of the same name are paired in document
    #    order when their counts agree.  The pair is diffed recursively
    #    and located by name and sibling position; the per-operation
    #    digest preconditions make sure we never silently modify the
    #    wrong component at apply time.
    remaining_old = [i for i in remaining_old if i not in matched_old]
    remaining_new = [j for j in remaining_new if j not in matched_new]
    old_by_name: dict[str, list[int]] = {}
    for old_index in remaining_old:
        old_by_name.setdefault(_component_name(old_subs[old_index]), []).append(
            old_index
        )
    new_by_name: dict[str, list[int]] = {}
    for new_index in remaining_new:
        new_by_name.setdefault(_component_name(new_subs[new_index]), []).append(
            new_index
        )
    for name, old_indexes in old_by_name.items():
        new_indexes = new_by_name.get(name, [])
        if len(old_indexes) != len(new_indexes):
            continue
        for old_index, new_index in zip(old_indexes, new_indexes):
            step = {"name": name, "index": old_positions[old_index]}
            matched_old.add(old_index)
            matched_new.add(new_index)
            _diff_component(old_subs[old_index], new_subs[new_index], path + [step], ops)

    # 4. Whatever is left was removed or added as a whole.
    for old_index in remaining_old:
        if old_index in matched_old:
            continue
        sub = old_subs[old_index]
        identity = _identity(sub)
        step = (
            identity
            if identity is not None
            else {"name": _component_name(sub), "index": old_positions[old_index]}
        )
        ops.append(
            {
                "op": "remove_component",
                "path": path + [step],
                "component_digest": calendar_digest(sub),
            }
        )
    for new_index in remaining_new:
        if new_index in matched_new:
            continue
        ops.append(
            {
                "op": "add_component",
                "path": path,
                "parent_digest": old_digest,
                "component": _component_snapshot(new_subs[new_index]),
            }
        )


def _diff_component(
    old: Component,
    new: Component,
    path: list[dict[str, Any]],
    ops: list[dict[str, Any]],
) -> None:
    old_digest = calendar_digest(old)
    _diff_properties(old, new, old_digest, path, ops)
    _diff_subcomponents(old, new, old_digest, path, ops)


def diff_calendars(old: Component, new: Component) -> dict[str, Any]:
    """Compute a semantic patch that turns ``old`` into ``new``.

    Parameters:
        old: The common base version of the calendar.
        new: The target version of the calendar.

    Returns:
        A JSON-serializable patch dictionary.  Applying it to ``old``
        with :func:`apply_patch` yields a calendar semantically equal to
        ``new``.  Differences that carry no meaning - property and
        parameter name case, ordering, and line folding - produce no
        operations.
    """
    ops: list[dict[str, Any]] = []
    _diff_component(old, new, [], ops)
    return {
        "format": PATCH_FORMAT,
        "base_digest": calendar_digest(old),
        "target_digest": calendar_digest(new),
        "ops": ops,
    }


########################################################################
# Apply


def _live_instances(component: Component, name: str) -> list[Any]:
    """The current property value objects for ``name``, in order."""
    if name not in component:
        return []
    values = component[name]
    return list(values) if isinstance(values, list) else [values]


def _check_digest(
    component: Component, expected: str | None, info: dict[str, Any]
) -> None:
    if expected is None:
        return
    actual = calendar_digest(component)
    if actual != expected:
        raise _Conflict(
            {
                **info,
                "reason": "digest_mismatch",
                "expected_digest": expected,
                "actual_digest": actual,
            }
        )


def _validate_op(root: Component, op: dict[str, Any]) -> tuple:
    """Resolve and check one operation against the pristine base.

    Returns a context tuple used to apply the operation later, so that
    application never has to resolve anything again.
    """
    kind = op.get("op")
    if kind == "add_component":
        parent = _resolve_path(root, op["path"])
        _check_digest(parent, op.get("parent_digest"), {"op": kind, "path": op["path"]})
        return (op, parent, None, None)
    if kind == "remove_component":
        parent = _resolve_path(root, op["path"][:-1])
        target = _resolve_path(root, op["path"])
        _check_digest(
            target, op.get("component_digest"), {"op": kind, "path": op["path"]}
        )
        return (op, parent, target, None)
    if kind in ("set_property", "remove_property", "add_property"):
        target = _resolve_path(root, op["path"])
        info = {"op": kind, "path": op["path"]}
        _check_digest(target, op.get("component_digest"), info)
        if kind == "add_property":
            return (op, target, None, None)
        name = op["name"].upper()
        instances = _live_instances(target, name)
        index = op["index"]
        if index >= len(instances):
            raise _Conflict(
                {
                    **info,
                    "reason": "property_not_found",
                    "name": name,
                    "index": index,
                }
            )
        actual = _property_snapshot(name, instances[index])
        if _canonical_property(actual) != _canonical_property(op["old"]):
            raise _Conflict(
                {
                    **info,
                    "reason": "property_mismatch",
                    "name": name,
                    "index": index,
                    "expected": op["old"],
                    "actual": actual,
                }
            )
        return (op, target, instances[index], name)
    raise ValueError(f"Unknown patch operation: {kind!r}")


def _value_from_snapshot(snapshot: dict[str, Any]) -> Any:
    """Rebuild a property value, preserving its original value type."""
    klass = _TYPE_CLASSES.get(snapshot["value_type"], vUnknown)
    try:
        value = klass.from_ical(snapshot["value"])
    except Exception:
        value = vUnknown(snapshot["value"])
    if not hasattr(value, "to_ical"):
        # Some factories (for example vDDDTypes) decode to native Python
        # values; re-encode so the value serializes like the original.
        value = klass(value)
    params = Parameters()
    for key, values in snapshot["params"].items():
        params[key] = list(values) if len(values) > 1 else values[0]
    try:
        value.params = params
    except AttributeError:
        pass
    return value


def _component_from_snapshot(snapshot: dict[str, Any]) -> Component:
    """Rebuild a component from its snapshot."""
    component_class = Component.get_component_class(snapshot["name"])
    component = component_class()
    for property_snapshot in snapshot["properties"]:
        component.add(
            property_snapshot["name"],
            _value_from_snapshot(property_snapshot),
            encode=False,
        )
    for sub_snapshot in snapshot["components"]:
        component.add_component(_component_from_snapshot(sub_snapshot))
    return component


def _replace_value(component: Component, name: str, old_value: Any, new_value: Any) -> None:
    values = component[name]
    if isinstance(values, list):
        for index, item in enumerate(values):
            if item is old_value:
                values[index] = new_value
                return
        raise RuntimeError("validated property occurrence disappeared")
    component[name] = new_value


def _remove_value(component: Component, name: str, old_value: Any) -> None:
    values = component[name]
    if isinstance(values, list):
        for index, item in enumerate(values):
            if item is old_value:
                del values[index]
                break
        else:
            raise RuntimeError("validated property occurrence disappeared")
        if not values:
            del component[name]
    else:
        del component[name]


def _apply_op(context: tuple) -> None:
    op, component, target, name = context
    kind = op["op"]
    if kind == "set_property":
        _replace_value(component, name, target, _value_from_snapshot(op["new"]))
    elif kind == "remove_property":
        _remove_value(component, name, target)
    elif kind == "add_property":
        component.add(
            op["property"]["name"],
            _value_from_snapshot(op["property"]),
            encode=False,
        )
    elif kind == "add_component":
        component.add_component(_component_from_snapshot(op["component"]))
    elif kind == "remove_component":
        for index, sub in enumerate(component.subcomponents):
            if sub is target:
                del component.subcomponents[index]
                break
        else:
            raise RuntimeError("validated subcomponent disappeared")


def apply_patch(
    base: Component, patch: Mapping[str, Any] | str | bytes
) -> Component:
    """Apply a semantic patch to a calendar.

    Parameters:
        base: The calendar the patch was computed from.  It is not
            modified; the patched calendar is returned.
        patch: A patch produced by :func:`diff_calendars`, either as a
            dictionary or as a JSON string.

    Returns:
        A new component with all operations applied.

    Raises:
        PatchConflictError: If any precondition does not hold - for
            example when ``base`` was edited after the patch was
            computed.  All preconditions are checked before anything is
            modified, so a failed apply never leaves a half-applied
            calendar behind.
    """
    if isinstance(patch, (str, bytes)):
        patch = json.loads(patch)
    if patch.get("format") != PATCH_FORMAT:
        raise ValueError(f"Unsupported patch format: {patch.get('format')!r}")

    # Work on a copy: the input calendar is never modified, and a failed
    # apply cannot leave a half-applied state behind.
    result = deepcopy(base)

    # Phase 1: resolve and validate every operation.  Nothing is
    # modified yet, so all digests are checked against the pristine base.
    # A base digest mismatch does not short-circuit: the operations are
    # still validated so the report can point at every conflicting
    # precondition.
    contexts = []
    conflicts = []
    base_digest = patch.get("base_digest")
    if base_digest is not None and calendar_digest(result) != base_digest:
        conflicts.append(
            {
                "op_index": None,
                "op": None,
                "reason": "base_digest_mismatch",
                "expected_digest": base_digest,
                "actual_digest": calendar_digest(result),
            }
        )
    for op_index, op in enumerate(patch.get("ops", [])):
        try:
            contexts.append(_validate_op(result, op))
        except _Conflict as conflict:
            conflicts.append(
                {"op_index": op_index, "op": op.get("op"), **conflict.info}
            )
    if conflicts:
        raise PatchConflictError(conflicts)

    # Phase 2: apply.  Targets were resolved by identity in phase 1, so
    # earlier operations cannot disturb the addressing of later ones.
    for context in contexts:
        _apply_op(context)

    target_digest = patch.get("target_digest")
    if target_digest is not None and calendar_digest(result) != target_digest:
        raise PatchConflictError(
            [
                {
                    "op_index": None,
                    "op": None,
                    "reason": "target_digest_mismatch",
                    "expected_digest": target_digest,
                    "actual_digest": calendar_digest(result),
                }
            ]
        )
    return result
