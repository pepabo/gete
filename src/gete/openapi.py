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
    "declaration_problems",
    "load_spec",
    "pruned_description",
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


def pruned_description(spec: Any, selected: Iterable[str]) -> dict[str, Any]:
    """The description reduced to the named operations.

    Cutting a published description down by hand breaks quietly: path-level
    parameters fall away, a flattened $ref takes the arguments it carried,
    and validate cannot tell a pruned description from one that never
    declared them. So the cutting is done here, from what read_operations
    already knows. A kept method rides with its whole path item - path-level
    parameters included - and every node a kept part references is grafted
    at its original pointer, transitively, so $ref keeps resolving. All the
    rest stays behind: unselected operations, unreferenced components, and
    the published servers, which nothing ever reads.
    """
    wanted = set(map(str, selected))
    paths = spec.get("paths") if isinstance(spec, Mapping) else None
    if not isinstance(paths, Mapping):
        raise DeclarationError(
            "the document has no paths; is it an OpenAPI description?"
        )
    kept_paths: dict[str, Any] = {}
    for path, item in paths.items():
        path_item = _resolve(spec, item)
        if not isinstance(path_item, Mapping):
            continue
        kept = {
            key: value
            for key, value in path_item.items()
            if key not in HTTP_METHODS
            or (isinstance(value, Mapping) and value.get("operationId") in wanted)
        }
        if any(method in kept for method in HTTP_METHODS):
            kept_paths[str(path)] = kept
    document: dict[str, Any] = {}
    for key in ("openapi", "info"):
        if isinstance(spec, Mapping) and key in spec:
            document[key] = spec[key]
    document["paths"] = kept_paths
    _graft_references(spec, document)
    return document


def _graft_references(spec: Any, document: dict[str, Any]) -> None:
    """Copy every locally referenced node into the document, transitively.

    Each target lands at its original JSON pointer, so the references it was
    found under keep resolving. Pointers into ``paths`` stay behind: grafting
    one would re-select what pruning just left out.
    """
    queue: list[Any] = [document["paths"]]
    grafted: set[str] = set()
    while queue:
        node = queue.pop(0)
        if isinstance(node, Mapping):
            for key, value in node.items():
                if (
                    key == "$ref"
                    and isinstance(value, str)
                    and value.startswith("#/")
                    and value not in grafted
                ):
                    grafted.add(value)
                    target = _graft(spec, document, value)
                    if target is not None:
                        queue.append(target)
                else:
                    queue.append(value)
        elif isinstance(node, list):
            queue.extend(node)


def _graft(spec: Any, document: dict[str, Any], pointer: str) -> Any:
    """Place one pointer's target into the document, returning it.

    A pointer that cannot be followed, or that leads into paths or through
    anything but mappings, grafts nothing - the reference dangles exactly as
    an unresolvable one always did, and the rules speak in their own words.
    """
    parts = [
        part.replace("~1", "/").replace("~0", "~") for part in pointer[2:].split("/")
    ]
    if not parts or parts[0] == "paths":
        return None
    source: Any = spec
    for part in parts:
        if not isinstance(source, Mapping) or part not in source:
            return None
        source = source[part]
    where = document
    for part in parts[:-1]:
        node = where.setdefault(part, {})
        if not isinstance(node, dict):
            return None
        where = node
    where[parts[-1]] = source
    return source


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
    for key in ("params", "describe"):
        for name in block.get(key, {}):
            if name not in selected:
                found.append(f"{key}: {name!r} is not one of this block's operations")
    for name, fixes in block.get("params", {}).items():
        operation = operations.get(name)
        if operation is not None and name in selected:
            found.extend(_fix_problems(operation, fixes))
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
    declared_in_path = {
        str(parameter.get("name"))
        for parameter in operation.parameters
        if parameter.get("in") == "path"
    }
    for placeholder in re.findall(r"\{([^{}]*)\}", operation.path):
        if placeholder not in declared_in_path:
            # The tool could never say which record the request addresses;
            # hand-pruned descriptions lose path-level parameters this way.
            found.append(
                f"operations: {name!r} has {{{placeholder}}} in its path, "
                "and no path parameter declares it"
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


def _fix_problems(operation: Operation, fixes: Mapping[str, Any]) -> list[str]:
    """What keeps the declared parameter fixes from being applied."""
    found: list[str] = []
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
        if name not in parameters and name not in body_properties:
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
            schema = parameters[name] if name in parameters else body_properties[name]
            declared = schema.get("type") if isinstance(schema, Mapping) else None
            if declared is not None and declared != "string":
                found.append(
                    f"params.{operation.id}.{name}: prefix and suffix need a "
                    f"string parameter, and {name!r} is {declared}"
                )
    return found
