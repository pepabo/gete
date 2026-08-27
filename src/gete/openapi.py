"""Reading an OpenAPI description: what an ``openapi:`` block selects from it.

The description is read where the agent is packed and travels inside the
archive; it is never fetched at run time. A vendor changing a published
description must not silently change the tools an agent offers - operations
appearing, or an argument's meaning shifting, without anyone declaring it.

Everything here is plain document walking, free of ADK, so validate can hold
a declaration against the description on machines that never deploy.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from gete.errors import DeclarationError

__all__ = [
    "Operation",
    "allow_tree",
    "declaration_problems",
    "exposes",
    "load_spec",
    "read_operations",
]

# The keys of a path item that name operations (RFC 9110 methods, lowercase).
HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")

# The verbs the connection client offers.
SUPPORTED_METHODS = frozenset({"get", "post", "put", "patch", "delete"})

# Methods that always change state. POST is not among them: search endpoints
# commonly take one, and the declaration's effect is where the difference is
# said.
WRITE_METHODS = frozenset({"put", "patch", "delete"})

# Methods the client sends without a body. A GET's is dropped by convention,
# a DELETE's has no meaning (RFC 9110); an operation that needs one would
# lose arguments silently.
BODYLESS_METHODS = frozenset({"get", "delete"})

# What a Gemini function may be called; the operationId becomes the tool name.
TOOL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,63}")


class _SpecLoader(yaml.SafeLoader):
    """SafeLoader that survives YAML 1.1 leftovers published specs carry.

    A ``=`` mapping key is typed as ``tag:yaml.org,2002:value``, which
    SafeLoader has no constructor for; real vendors publish specs that use
    it. It stands for the literal string, so that is what it becomes.
    """


def _construct_value(loader: _SpecLoader, node: yaml.Node) -> Any:
    return loader.construct_yaml_str(node)


_SpecLoader.add_constructor("tag:yaml.org,2002:value", _construct_value)


def load_spec(path: Path) -> Any:
    """One OpenAPI document from a YAML or JSON file.

    JSON parses as YAML, so one loader reads both serializations.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise DeclarationError(f"{path} could not be read: {error}") from error
    try:
        return yaml.load(text, Loader=_SpecLoader)  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError as error:
        raise DeclarationError(
            f"{path} could not be parsed as an OpenAPI description: {error}"
        ) from error


@dataclass(frozen=True)
class Operation:
    """One operation as the rules need it: located, with references resolved."""

    id: str
    path: str
    method: str
    document: Mapping[str, Any]
    # Path-level and operation-level parameters merged, each resolved.
    parameters: tuple[Mapping[str, Any], ...]
    # The JSON request body schema, resolved one level, or None.
    body: Mapping[str, Any] | None
    # The media type the body was found under, or None.
    body_media: str | None


def _resolve(spec: Any, node: Any) -> Any:
    """Follow local $ref pointers until a plain node is reached.

    Only ``#/...`` pointers: an external reference would reach outside the
    file the archive holds. A pointer that cannot be followed, or that loops,
    resolves to nothing rather than raising - the rules report what is
    missing in their own words.
    """
    seen: set[str] = set()
    while isinstance(node, Mapping) and isinstance(node.get("$ref"), str):
        reference: str = node["$ref"]
        if not reference.startswith("#/") or reference in seen:
            return None
        seen.add(reference)
        node = spec
        for part in reference[2:].split("/"):
            name = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(node, Mapping) or name not in node:
                return None
            node = node[name]
    return node


def _resolved_parameters(
    spec: Any, path_item: Mapping[str, Any], operation: Mapping[str, Any]
) -> tuple[Mapping[str, Any], ...]:
    """The operation's parameters: path-level first, resolved, mappings only."""
    merged: list[Mapping[str, Any]] = []
    for owner in (path_item, operation):
        for entry in owner.get("parameters", ()):
            resolved = _resolve(spec, entry)
            if isinstance(resolved, Mapping) and "name" in resolved:
                merged.append(resolved)
    return tuple(merged)


def _fold_allof(spec: Any, schema: Mapping[str, Any]) -> Mapping[str, Any]:
    """One level of allOf folded into a plain schema.

    Published bodies are often a composition - a shared core plus the
    operation's own fields - and without folding, neither the rules nor the
    parser would see the composed properties. One level covers that shape;
    deeper nesting stays unread, and a body it hides ends up refused for
    declaring no properties rather than half-read.
    """
    parts = schema.get("allOf")
    if not isinstance(parts, list):
        return schema
    properties: dict[str, Any] = {}
    required: list[str] = []
    folded_type = schema.get("type")
    for part in parts:
        resolved = _resolve(spec, part)
        if not isinstance(resolved, Mapping):
            continue
        if folded_type is None:
            folded_type = resolved.get("type")
        part_properties = resolved.get("properties")
        if isinstance(part_properties, Mapping):
            properties.update(part_properties)
        part_required = resolved.get("required")
        if isinstance(part_required, list):
            required.extend(name for name in part_required if name not in required)
    own = schema.get("properties")
    if isinstance(own, Mapping):
        properties.update(own)
    own_required = schema.get("required")
    if isinstance(own_required, list):
        required.extend(name for name in own_required if name not in required)
    folded = {key: value for key, value in schema.items() if key != "allOf"}
    if properties:
        folded["properties"] = properties
    if required:
        folded["required"] = required
    if folded_type is not None:
        folded["type"] = folded_type
    return folded


def _resolved_body(
    spec: Any, operation: Mapping[str, Any]
) -> tuple[Mapping[str, Any] | None, str | None]:
    """The JSON request body schema and its media type, or (None, None).

    Only JSON is looked for; other bodies are reported by the rules, not
    silently sent as JSON. allOf is folded one level so a schema written as
    a composition still shows its properties.
    """
    request_body = _resolve(spec, operation.get("requestBody"))
    if not isinstance(request_body, Mapping):
        return None, None
    content = request_body.get("content")
    if not isinstance(content, Mapping) or not content:
        return None, None
    for media, entry in content.items():
        if media == "application/json" or media.endswith("+json"):
            schema = None
            if isinstance(entry, Mapping):
                schema = _resolve(spec, entry.get("schema"))
            if isinstance(schema, Mapping):
                schema = _fold_allof(spec, schema)
            return (schema if isinstance(schema, Mapping) else {}), media
    # No JSON among the media types; the first one names what was found.
    return None, next(iter(content))


def read_operations(spec: Any) -> tuple[dict[str, Operation], list[str]]:
    """Every operation with an operationId, and the ids that appear twice.

    A duplicated id cannot select one operation, so the caller reports it
    instead of quietly taking either.
    """
    operations: dict[str, Operation] = {}
    duplicates: list[str] = []
    paths = spec.get("paths") if isinstance(spec, Mapping) else None
    if not isinstance(paths, Mapping):
        raise DeclarationError(
            "the document has no paths; is it an OpenAPI description?"
        )
    for path, item in paths.items():
        path_item = _resolve(spec, item)
        if not isinstance(path_item, Mapping):
            continue
        for method in HTTP_METHODS:
            operation = path_item.get(method)
            if not isinstance(operation, Mapping):
                continue
            operation_id = operation.get("operationId")
            if not isinstance(operation_id, str) or not operation_id:
                continue
            if operation_id in operations:
                if operation_id not in duplicates:
                    duplicates.append(operation_id)
                continue
            body, body_media = _resolved_body(spec, operation)
            operations[operation_id] = Operation(
                id=operation_id,
                path=str(path),
                method=method,
                document=operation,
                parameters=_resolved_parameters(spec, path_item, operation),
                body=body,
                body_media=body_media,
            )
    return operations, duplicates


def declaration_problems(block: Mapping[str, Any], spec: Any) -> list[str]:
    """Hold one ``openapi:`` block against the description it selects from.

    The block has passed the schema; these are the rules the schema cannot
    see. Returned messages carry no prefix; validate adds where they were
    found.
    """
    try:
        operations, duplicates = read_operations(spec)
    except DeclarationError as error:
        return [str(error)]
    found: list[str] = []
    selected: tuple[str, ...] = tuple(block.get("operations", ()))
    effect = str(block.get("effect", "write"))
    for name in selected:
        if name in duplicates:
            found.append(
                f"operations: {name!r} appears more than once in the "
                "description; it cannot select one operation"
            )
            continue
        operation = operations.get(name)
        if operation is None:
            found.append(
                f"operations: {name!r} is not an operationId in the description"
            )
            continue
        found.extend(_operation_problems(operation, effect))
    for key in ("params", "describe", "only"):
        for name in block.get(key, {}):
            if name not in selected:
                found.append(f"{key}: {name!r} is not one of this block's operations")
    for name, fixes in block.get("params", {}).items():
        operation = operations.get(name)
        if operation is not None and name in selected:
            found.extend(
                _fix_problems(spec, operation, fixes, block.get("only", {}).get(name))
            )
    for name, exposed in block.get("only", {}).items():
        operation = operations.get(name)
        if operation is not None and name in selected:
            found.extend(
                _only_problems(
                    spec, operation, exposed, block.get("params", {}).get(name, {})
                )
            )
    return found


def _operation_problems(operation: Operation, effect: str) -> list[str]:
    """What keeps one selected operation from becoming a tool."""
    found: list[str] = []
    name = operation.id
    if not TOOL_NAME.fullmatch(name):
        found.append(
            f"operations: {name!r} cannot name a tool; letters, digits, "
            "underscores, and hyphens only"
        )
    if operation.method not in SUPPORTED_METHODS:
        found.append(
            f"operations: {name!r} is {operation.method.upper()}, "
            "which is not one of GET, POST, PUT, PATCH, DELETE"
        )
        return found
    if operation.method in WRITE_METHODS and effect != "write":
        found.append(
            f"operations: {name!r} is a {operation.method.upper()}, which "
            "changes state; declare it in a block with effect: write"
        )
    for parameter in operation.parameters:
        where = parameter.get("in")
        if where in ("header", "cookie") and parameter.get("required"):
            found.append(
                f"operations: {name!r} requires {where} parameter "
                f"{parameter.get('name')!r}, which a declaration does not send"
            )
    if operation.body_media is not None and operation.body is None:
        found.append(
            f"operations: {name!r} takes a {operation.body_media} body; "
            "only a JSON body can be declared"
        )
    elif operation.body is not None:
        if operation.method in BODYLESS_METHODS:
            found.append(
                f"operations: {name!r} is a {operation.method.upper()} with "
                "a request body, which is not supported"
            )
        else:
            body_type = operation.body.get("type")
            properties = operation.body.get("properties")
            if body_type is not None and body_type != "object":
                found.append(
                    f"operations: {name!r} has a {body_type} JSON body; "
                    "only an object body can be declared"
                )
            elif not (isinstance(properties, Mapping) and properties):
                # The parser would offer the model one opaque body argument
                # and the request would carry the payload wrapped under a
                # key the service never declared.
                found.append(
                    f"operations: {name!r} has a JSON body that declares no "
                    "properties; there is nothing to offer the model"
                )
    return found


@dataclass(frozen=True)
class _BodyLeaf:
    """Where a dotted name landed in the body: a schema when the walk could
    enumerate its way down, or the problem that stopped it. Neither means the
    walk left enumerable ground, so the miss is not certain."""

    schema: Mapping[str, Any] | None = None
    problem: str | None = None
    # Whether every segment was found in enumerated properties. A path into
    # a freeform object is possible, never certain.
    certain: bool = True


def _walk_body(spec: Any, operation: Operation, segments: tuple[str, ...]) -> _BodyLeaf:
    """Follow dotted segments through the body's properties, resolving and
    folding each level the way the top level was."""
    if operation.body is None:
        return _BodyLeaf(
            problem=f"reaches into the JSON body, and {operation.id} declares none"
        )
    node: Mapping[str, Any] | None = operation.body
    trail: list[str] = []
    for segment in segments:
        if node is None:
            return _BodyLeaf(certain=False)
        declared = node.get("type")
        if declared is not None and declared != "object":
            return _BodyLeaf(
                problem=f"{'.'.join(trail)!r} is a {declared}, which holds "
                "no properties"
            )
        properties = node.get("properties")
        if not isinstance(properties, Mapping):
            return _BodyLeaf(certain=False)
        if segment not in properties:
            where = ".".join(trail) or "the body"
            known = ", ".join(sorted(map(str, properties)))
            return _BodyLeaf(
                problem=f"{segment!r} is not a property of {where} "
                f"(properties: {known})"
            )
        child = _resolve(spec, properties[segment])
        if isinstance(child, Mapping):
            child = _fold_allof(spec, child)
        node = child if isinstance(child, Mapping) else None
        trail.append(segment)
    return _BodyLeaf(schema=node)


def _argument_maps(
    operation: Operation,
) -> tuple[dict[str, Any], set[str], dict[str, Any], bool]:
    """The names a declaration can speak about, by where they live: request
    parameters, header names, top-level body properties, and whether the body
    could be enumerated at all."""
    parameters: dict[str, Any] = {}
    header_names: set[str] = set()
    for parameter in operation.parameters:
        name = str(parameter.get("name"))
        if parameter.get("in") in ("query", "path"):
            parameters[name] = parameter.get("schema")
        else:
            header_names.add(name)
    properties = (operation.body or {}).get("properties")
    body_properties: dict[str, Any] = (
        {str(name): schema for name, schema in properties.items()}
        if isinstance(properties, Mapping)
        else {}
    )
    enumerable = operation.body is None or isinstance(properties, Mapping)
    return parameters, header_names, body_properties, enumerable


def _fix_problems(
    spec: Any,
    operation: Operation,
    fixes: Mapping[str, Any],
    only: Iterable[str] | None = None,
) -> list[str]:
    """What keeps the declared parameter fixes from being applied."""
    found: list[str] = []
    parameters, header_names, body_properties, enumerable = _argument_maps(operation)
    tree = (
        allow_tree(only, literal=[*parameters, *body_properties])
        if only is not None
        else None
    )
    for name, fix in fixes.items():
        if name in header_names:
            found.append(
                f"params.{operation.id}: {name!r} is a header parameter, "
                "which a declaration does not send"
            )
            continue
        if name in parameters and name in body_properties:
            # The runtime applies a fix by name; one name in two places
            # would be fixed in both, and the declaration said neither.
            found.append(
                f"params.{operation.id}: {name!r} names both a request "
                f"parameter and a body property of {operation.id}; the fix "
                "cannot choose between them"
            )
            continue
        if (
            (name in parameters or name in body_properties)
            and "." in name
            and _certainly_in_body(spec, operation, name)
        ):
            # A dot may sit in a parameter's literal name or mark a path into
            # the body; when both readings hold, neither was declared.
            found.append(
                f"params.{operation.id}: {name!r} names both a parameter and "
                "a path into the body; the fix cannot choose between them"
            )
            continue
        if name not in parameters and name not in body_properties:
            if "." in name:
                found.extend(
                    f"params.{operation.id}: {name!r} {message}"
                    for message in _dotted_problems(spec, operation, name, fix, tree)
                )
                continue
            # A body whose properties cannot be enumerated may still hold
            # the name; only a miss that is certain is reported.
            if enumerable:
                known = sorted({**body_properties, **parameters})
                found.append(
                    f"params.{operation.id}: {name!r} names no parameter of "
                    f"{operation.id} (parameters: {', '.join(known)})"
                )
            continue
        if "prefix" in fix or "suffix" in fix:
            if not exposes(tree, (name,)):
                found.append(
                    f"params.{operation.id}.{name}: prefix and suffix wrap "
                    f"what the model writes, and only does not expose {name!r}"
                )
                continue
            schema = parameters[name] if name in parameters else body_properties[name]
            declared = schema.get("type") if isinstance(schema, Mapping) else None
            if declared is not None and declared != "string":
                found.append(
                    f"params.{operation.id}.{name}: prefix and suffix need a "
                    f"string parameter, and {name!r} is {declared}"
                )
    return found


def _only_problems(
    spec: Any,
    operation: Operation,
    exposed: Iterable[str],
    fixes: Mapping[str, Any],
) -> list[str]:
    """What keeps the declared exposure list from carving the operation."""
    found: list[str] = []
    parameters, header_names, body_properties, enumerable = _argument_maps(operation)
    exposed = tuple(exposed)
    # A dotted entry still sends its top-level argument, just narrowed. The
    # tree's own keys are the names the runtime will hold parameters against.
    covered = set(allow_tree(exposed, literal=[*parameters, *body_properties]))
    fixed = {name for name, fix in fixes.items() if "value" in fix}
    for name in _required_names(operation, parameters):
        if name not in covered and name not in fixed:
            found.append(
                f"only.{operation.id}: {name!r} is required, and it is "
                "neither listed nor fixed; every request needs it"
            )
    for entry in exposed:
        if "value" in fixes.get(entry, {}):
            found.append(
                f"only.{operation.id}: {entry!r} is fixed by params; a fixed "
                "parameter is not the model's to write"
            )
            continue
        if entry in header_names:
            found.append(
                f"only.{operation.id}: {entry!r} is a header parameter, "
                "which a declaration does not send"
            )
            continue
        if entry in parameters and entry in body_properties:
            # The runtime exposes by name; one name in two places would
            # expose both, and the declaration said neither.
            found.append(
                f"only.{operation.id}: {entry!r} names both a request "
                f"parameter and a body property of {operation.id}; the "
                "entry cannot choose between them"
            )
            continue
        literal = entry in parameters or entry in body_properties
        if literal and "." in entry and _certainly_in_body(spec, operation, entry):
            found.append(
                f"only.{operation.id}: {entry!r} names both a parameter and "
                "a path into the body; the entry cannot choose between them"
            )
            continue
        if literal:
            continue
        if "." in entry:
            segments = tuple(entry.split("."))
            leaf = _walk_body(spec, operation, segments)
            if leaf.problem is not None:
                found.append(f"only.{operation.id}: {entry!r} {leaf.problem}")
            elif segments[0] in parameters:
                # The runtime keys exposure by the first segment; a request
                # parameter with that name would ride along whole.
                found.append(
                    f"only.{operation.id}: {entry!r} reaches into the body "
                    f"through {segments[0]!r}, which also names a request "
                    "parameter; the entry cannot choose between them"
                )
            continue
        if enumerable:
            known = sorted({**body_properties, **parameters})
            found.append(
                f"only.{operation.id}: {entry!r} names no parameter of "
                f"{operation.id} (parameters: {', '.join(known)})"
            )
    return found


def allow_tree(entries: Iterable[str], literal: Iterable[str] = ()) -> dict[str, Any]:
    """An ``only`` list as a tree of what the model may write.

    A node of None means the whole subtree is the model's; a mapping narrows
    it to the named children. A bare name is broader than any dotted entry
    under it, so it wins. A name in ``literal`` is one some parameter
    carries, dots and all, and stays whole - a dot marks a path only when
    nothing claims the name literally, exactly as ``params`` reads it.
    """
    names = set(map(str, literal))
    tree: dict[str, Any] = {}
    for entry in entries:
        text = str(entry)
        segments = [text] if text in names else text.split(".")
        node = tree
        for segment in segments[:-1]:
            if segment in node and node[segment] is None:
                break
            node = node.setdefault(segment, {})
        else:
            node[segments[-1]] = None
    return tree


def exposes(tree: Mapping[str, Any] | None, path: Iterable[str]) -> bool:
    """Whether the model may write the value at path under this tree.

    No tree at all means everything is the model's; landing on a mapping
    means the object itself is written, if only its named children.
    """
    if tree is None:
        return True
    node: Any = tree
    for segment in path:
        if node is None:
            return True
        if not isinstance(node, Mapping) or segment not in node:
            return False
        node = node[segment]
    return True


def _required_names(operation: Operation, parameters: Mapping[str, Any]) -> list[str]:
    """Names a request cannot go without: required query and path parameters,
    then the body's own required properties."""
    names = [
        str(parameter.get("name"))
        for parameter in operation.parameters
        if parameter.get("required") and str(parameter.get("name")) in parameters
    ]
    required = (operation.body or {}).get("required")
    if isinstance(required, list):
        names.extend(str(name) for name in required if name not in names)
    return names


def _certainly_in_body(spec: Any, operation: Operation, name: str) -> bool:
    leaf = _walk_body(spec, operation, tuple(name.split(".")))
    return leaf.problem is None and leaf.certain


def _dotted_problems(
    spec: Any,
    operation: Operation,
    name: str,
    fix: Mapping[str, Any],
    tree: Mapping[str, Any] | None,
) -> list[str]:
    """What keeps one dotted fix from reaching its place in the body."""
    segments = tuple(name.split("."))
    leaf = _walk_body(spec, operation, segments)
    if leaf.problem is not None:
        return [leaf.problem]
    if "value" in fix and not exposes(tree, segments[:-1]):
        # The fix rides on its parent object; a parent never sent would
        # leave the constraint silently unapplied.
        return ["is pinned inside a parent that only does not send"]
    if "prefix" in fix or "suffix" in fix:
        if not exposes(tree, segments):
            return [
                "takes a prefix or suffix, which wrap what the model "
                "writes, and only does not expose it"
            ]
        declared = leaf.schema.get("type") if leaf.schema is not None else None
        if declared is not None and declared != "string":
            return [
                "takes a prefix or suffix, which need a string property, "
                f"and it is {declared}"
            ]
    return []
