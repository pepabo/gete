"""OpenAPI tools: operations selected from a description, sent through gete's client.

The description travelled inside the archive; nothing is fetched when the
agent starts. The published ``servers`` are never read - a definition's own
root may hold variables, a stale default, or another tenant - so request
URLs are built from the connection's ``base_url``, and the client's
destination check and token rules hold exactly as for every other request.

ADK's spec parser turns the pruned description into declarations the model
can call. Execution stays here: ADK's own OpenAPI tools carry requests and
credentials themselves, outside the connection's guards.
"""

import urllib.parse
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from google.adk.tools.base_tool import BaseTool
from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.openapi_tool.openapi_spec_parser.openapi_spec_parser import (
    OpenApiSpecParser,
)
from google.adk.tools.openapi_tool.openapi_spec_parser.rest_api_tool import (
    RestApiTool,
)

from gete.connection.client import shared_client
from gete.connection.registry import Connection, Registry
from gete.connection.runtime import usable_token
from gete.declaration import Agent
from gete.errors import DeclarationError, GeteError
from gete.openapi import Operation, declaration_problems, load_spec, read_operations


@dataclass(frozen=True)
class Argument:
    """How one request argument is produced: by the model, or declared.

    A fixed argument carries the declared value and is not offered to the
    model at all. prefix and suffix wrap what the model wrote; they are how
    a declaration keeps a constraint the code it replaces used to enforce.
    """

    name: str
    py_name: str
    location: str
    fixed: bool = False
    value: Any = None
    prefix: str = ""
    suffix: str = ""


class OpenApiTool(BaseTool):
    """One operation as a tool, run with the caller's token."""

    def __init__(
        self,
        *,
        name: str,
        description: str,
        method: str,
        path: str,
        connection: Connection,
        arguments: Iterable[Argument],
        declaration: Any,
        require_confirmation: bool,
    ) -> None:
        super().__init__(name=name, description=description)
        self._method = method
        self._path = path
        self._connection = connection
        self._arguments = tuple(arguments)
        self._declaration = declaration
        self._require_confirmation = require_confirmation

    def _get_declaration(self) -> Any:
        return self._declaration

    async def check_require_confirmation(
        self, args: dict[str, Any], tool_context: Any
    ) -> bool:
        return self._require_confirmation

    async def run_async(self, *, args: dict[str, Any], tool_context: Any) -> Any:
        root = self._connection.base_url
        if root is None:
            # validate refuses this; guarded again because the archive that
            # reached the runtime is whatever was packed.
            raise GeteError(
                f"connection {self._connection.id} declares no base_url; "
                "there is no root to build the request URL from"
            )
        path = self._path
        query: dict[str, Any] = {}
        body: dict[str, Any] = {}
        for argument in self._arguments:
            value = argument.value if argument.fixed else args.get(argument.py_name)
            if value is None:
                continue
            if argument.prefix or argument.suffix:
                value = f"{argument.prefix}{value}{argument.suffix}"
            if argument.location == "path":
                # Quoted with no safe characters, so a value cannot climb
                # out of its segment and address another route.
                path = path.replace(
                    "{" + argument.name + "}",
                    urllib.parse.quote(str(value), safe=""),
                )
            elif argument.location == "query":
                query[argument.name] = value
            else:
                body[argument.name] = value
        if "{" in path:
            raise GeteError(f"{self.name}: a path parameter was not given")
        url = root.rstrip("/") + path
        client = shared_client(self._connection.id)
        state = getattr(tool_context, "state", None)
        if self._method == "get":
            return await client.get_json(url, params=query or None, state=state)
        if self._method == "delete":
            # No body: DELETE gives one no meaning, and validate refused any
            # operation that declares one.
            return await client.delete_json(url, params=query or None, state=state)
        send = {
            "post": client.post_json,
            "put": client.put_json,
            "patch": client.patch_json,
        }[self._method]
        return await send(url, body or None, params=query or None, state=state)


class OpenApiToolset(BaseToolset):
    """Offers the block's tools, and only to a caller with a usable token.

    Without one the service is not spoken for at all: nothing is offered,
    and the way back to authorizing belongs to the connection's
    reauthorization tool, offered by the agent.
    """

    def __init__(
        self,
        *,
        tools: Iterable[OpenApiTool],
        connection: Connection,
        authorization_key: str,
    ) -> None:
        super().__init__()
        self._tools = tuple(tools)
        self._connection = connection
        self._key = authorization_key

    @property
    def connection(self) -> Connection:
        return self._connection

    @property
    def connection_id(self) -> str:
        return self._connection.id

    async def get_tools(self, readonly_context: Any = None) -> list[Any]:
        # No context at all counts as no token: the Agent Card is built that
        # way, and it must not promise what an unauthorized user cannot call.
        state = getattr(readonly_context, "state", None)
        if usable_token(self._connection, self._key, state) is None:
            return []
        return list(self._tools)

    async def close(self) -> None:
        """Nothing is held open; requests go through the shared client."""


def openapi_toolset(
    spec: Mapping[str, Any],
    *,
    agent: Agent,
    authorizations: Mapping[str, str],
    registry: Registry,
    confirm: bool,
    confirm_names: Iterable[str] = (),
    denied: Iterable[str] = (),
) -> OpenApiToolset:
    """Build the toolset for one ``openapi:`` entry of a resolved declaration."""
    connection_id = str(spec["connection"])
    connection = registry.get(connection_id)
    document = load_spec(agent.directory / str(spec["spec"]))
    found = declaration_problems(spec, document)
    if found:
        raise DeclarationError("openapi: " + "; ".join(found))
    operations, _ = read_operations(document)
    selected = [operations[str(name)] for name in spec["operations"]]
    parsed_by_id: dict[str, Any] = {}
    for entry in OpenApiSpecParser().parse(_pruned(document, selected)):
        operation_id = entry.operation.operationId
        if operation_id:
            parsed_by_id[operation_id] = entry
    confirmed = frozenset(confirm_names)
    excluded = frozenset(denied)
    describe: Mapping[str, str] = spec.get("describe", {})
    does_not: str | None = spec.get("does_not")
    tools: list[OpenApiTool] = []
    for name in spec["operations"]:
        if name in excluded:
            continue
        parsed = parsed_by_id.get(name)
        if parsed is None:
            # read_operations saw it, the parser did not; something in the
            # description defeats the parser, and silence would offer less
            # than what was declared.
            raise DeclarationError(
                f"openapi: {name!r} was not parsed from the description"
            )
        tools.append(
            _tool(
                parsed,
                name=str(name),
                fixes=spec.get("params", {}).get(name, {}),
                description=describe.get(name),
                does_not=does_not,
                connection=connection,
                require_confirmation=confirm or name in confirmed,
            )
        )
    return OpenApiToolset(
        tools=tools,
        connection=connection,
        authorization_key=authorizations.get(connection_id, connection_id),
    )


def _pruned(spec: Mapping[str, Any], selected: Iterable[Operation]) -> dict[str, Any]:
    """The description reduced to the selected operations.

    Each operation rides with its parameters already resolved and merged -
    path-level ones included - and its JSON body schema folded, with
    ``type: object`` said out loud: the parser only expands a body's
    properties into arguments when the type is spelled, and published
    schemas often leave it implicit or compose it with allOf. Property-level
    references stay, and components ride along whole for the parser to
    resolve. Security schemes are left out of the requests: authorization is
    the client's, never the parser's.
    """
    paths: dict[str, dict[str, Any]] = {}
    for operation in selected:
        entry = dict(operation.document)
        entry["parameters"] = [dict(parameter) for parameter in operation.parameters]
        if operation.body is not None:
            entry["requestBody"] = {
                "content": {
                    operation.body_media: {
                        "schema": {**operation.body, "type": "object"}
                    }
                }
            }
        paths.setdefault(operation.path, {})[operation.method] = entry
    return {
        "openapi": str(spec.get("openapi", "3.0.0")),
        "info": dict(spec.get("info") or {"title": "", "version": ""}),
        "paths": paths,
        "components": dict(spec.get("components") or {}),
    }


def _tool(
    parsed: Any,
    *,
    name: str,
    fixes: Mapping[str, Mapping[str, Any]],
    description: str | None,
    does_not: str | None,
    connection: Connection,
    require_confirmation: bool,
) -> OpenApiTool:
    """One tool: the declaration without the fixed parameters, and the
    arguments that rebuild the request from what the model writes."""
    arguments: list[Argument] = []
    visible: list[Any] = []
    applied: set[str] = set()
    for parameter in parsed.parameters:
        if parameter.param_location in ("header", "cookie"):
            # Never model-driven; validate refused the required ones.
            continue
        fix = fixes.get(parameter.original_name, {})
        if fix:
            applied.add(parameter.original_name)
        if "value" in fix:
            arguments.append(
                Argument(
                    name=parameter.original_name,
                    py_name=parameter.py_name,
                    location=parameter.param_location,
                    fixed=True,
                    value=fix["value"],
                )
            )
            continue
        arguments.append(
            Argument(
                name=parameter.original_name,
                py_name=parameter.py_name,
                location=parameter.param_location,
                prefix=str(fix.get("prefix", "")),
                suffix=str(fix.get("suffix", "")),
            )
        )
        visible.append(parameter)
    unapplied = sorted(set(fixes) - applied)
    if unapplied:
        # validate could not see into this body; failing here is still
        # better than a constraint that silently never held.
        raise DeclarationError(
            f"openapi: params.{name}: {', '.join(map(repr, unapplied))} "
            "name no parameter the description declares"
        )
    # The declaration is built from the visible parameters only. ADK reads
    # them from the parsed operation, so the list is narrowed before the
    # tool is derived from it.
    parsed.parameters = visible
    rest = RestApiTool.from_parsed_operation(parsed)
    declaration = rest._get_declaration()  # noqa: SLF001 - ADK offers no public way
    operation = parsed.operation
    text = description or operation.description or operation.summary or ""
    if does_not:
        text = f"{text}\n\nDoes not: {does_not}".strip()
    declaration.name = name
    declaration.description = text
    return OpenApiTool(
        name=name,
        description=text,
        method=str(parsed.endpoint.method).lower(),
        path=str(parsed.endpoint.path),
        connection=connection,
        arguments=arguments,
        declaration=declaration,
        require_confirmation=require_confirmation,
    )
