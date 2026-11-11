"""MCP tools: ADK's McpToolset with the user's token and gete's rules on top."""

import logging
import os
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from google.adk.tools.mcp_tool import (
    McpTool,
    McpToolset,
    StreamableHTTPConnectionParams,
)

from gete.connection.registry import Connection, Registry
from gete.connection.runtime import usable_token
from gete.errors import DeclarationError
from gete.mcp import declaration_problems

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30.0
_ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Replace ${VAR} in header values from the environment; a missing VAR is an error.

    A header that quietly became empty would show up much later as an
    authentication failure at the server, with nothing pointing back here.
    """
    expanded: dict[str, str] = {}
    for name, value in headers.items():
        missing = [
            var for var in _ENV_REFERENCE.findall(value) if var not in os.environ
        ]
        if missing:
            raise DeclarationError(
                f"mcp header {name} references {', '.join(missing)}, "
                "which is not set in the environment"
            )
        expanded[name] = _ENV_REFERENCE.sub(
            lambda match: os.environ[match.group(1)], value
        )
    return expanded


def _schema_field(listed: Any) -> str:
    """Where a listed tool keeps its input schema. MCP SDK 1.x spells the
    field inputSchema and 2.x input_schema; ADK accepts either."""
    return "inputSchema" if hasattr(listed, "inputSchema") else "input_schema"


class PinnedMcpTool(McpTool):
    """An MCP tool some of whose arguments the declaration fixes.

    The fixed arguments are taken out of the schema the model is shown, and
    every call carries the declared values over anything the model wrote.
    """

    def __init__(self, listed: McpTool, values: Mapping[str, Any]) -> None:
        # Not McpTool.__init__: its parameters move between ADK releases, and
        # the listed tool already carries everything it was built with - the
        # session, the headers, the confirmation setting.
        self.__dict__.update(listed.__dict__)
        self._pinned = dict(values)
        raw = listed.raw_mcp_tool
        field = _schema_field(raw)
        schema = dict(getattr(raw, field))
        schema["properties"] = {
            name: value
            for name, value in schema["properties"].items()
            if name not in values
        }
        required = [name for name in schema.get("required", ()) if name not in values]
        if required:
            schema["required"] = required
        else:
            schema.pop("required", None)
        # A copy: the listing may be cached and handed out again, and the
        # check below must find the fixed names there next time too.
        self._mcp_tool = raw.model_copy(update={field: schema})

    @classmethod
    def holding(
        cls,
        listed: McpTool,
        values: Mapping[str, Any],
        *,
        warn: Callable[[str], None] = logger.warning,
    ) -> "PinnedMcpTool | None":
        """The listed tool with its fixed arguments held, or None and a
        warning when the server's schema does not declare them all.

        validate cannot see a server's schemas, so a renamed argument shows
        up only here. Offering the tool anyway would send the declared value
        under a name the server ignores, and the call would run unbounded.
        """
        raw = listed.raw_mcp_tool
        schema = getattr(raw, _schema_field(raw), None)
        properties = schema.get("properties") if isinstance(schema, Mapping) else None
        declared = properties if isinstance(properties, Mapping) else {}
        missing = [name for name in values if name not in declared]
        if missing:
            warn(
                f"mcp tool {listed.name} is not offered: its input schema "
                f"declares no {', '.join(missing)}, which the declaration fixes"
            )
            return None
        return cls(listed, values)

    async def run_async(self, *, args: dict[str, Any], tool_context: Any) -> Any:
        # The declared values come last, so a value the model smuggled into
        # the arguments under a fixed name is overwritten, never sent.
        return await super().run_async(
            args={**args, **self._pinned}, tool_context=tool_context
        )


class GeteMcpToolset(McpToolset):
    """McpToolset that sends the calling user's token and honours the declaration.

    With a connection, every request carries ``Authorization: Bearer <token>``
    taken from the state under the agent's authorization name, and only when
    the token has the connection's shape. Without a usable token the server is
    not contacted at all and nothing is offered; asking the user to authorize
    again belongs to the connection, which may back more than one toolset.
    ``does_not`` is appended to every tool description, and the tools
    ``params`` names come with their fixed arguments held.
    """

    def __init__(
        self,
        *,
        url: str,
        fixed_headers: Mapping[str, str],
        timeout: float,
        allow: list[str] | None,
        connection: Connection | None,
        authorization_key: str | None,
        does_not: str | None,
        confirm: bool,
        confirm_names: Iterable[str] = (),
        denied: Iterable[str] = (),
        params: Mapping[str, Mapping[str, Mapping[str, Any]]] | None = None,
    ) -> None:
        self.url = url
        self.fixed_headers = dict(fixed_headers)
        self.timeout = timeout
        self.allow = allow
        self.does_not = does_not
        self._connection = connection
        self._authorization_key = authorization_key
        self._confirm_names = frozenset(confirm_names)
        self._denied = frozenset(denied)
        self._pinned = {
            tool: {name: fix["value"] for name, fix in arguments.items()}
            for tool, arguments in (params or {}).items()
        }
        self._warned: set[str] = set()
        provider: Callable[[Any], dict[str, str]] | None = (
            self._authorization_headers if connection is not None else None
        )
        super().__init__(
            connection_params=StreamableHTTPConnectionParams(
                url=url, headers=self.fixed_headers or None, timeout=timeout
            ),
            tool_filter=allow,
            header_provider=provider,
            require_confirmation=confirm,
        )

    @property
    def connection(self) -> Connection | None:
        return self._connection

    @property
    def connection_id(self) -> str | None:
        return self._connection.id if self._connection is not None else None

    async def get_tools(self, readonly_context: Any = None) -> list[Any]:
        # No context at all counts as no token: the Agent Card is built that
        # way, and an unauthenticated listing would still leave the server.
        if self._connection is not None and self._token(readonly_context) is None:
            return []
        tools: list[Any] = await super().get_tools(readonly_context)
        kept = []
        for tool in tools:
            if tool.name in self._denied:
                continue
            pinned = self._pinned.get(tool.name)
            if pinned:
                tool = PinnedMcpTool.holding(tool, pinned, warn=self._warn_once)
                if tool is None:
                    continue
            if self.does_not:
                tool.description = (
                    f"{tool.description}\n\nDoes not: {self.does_not}".strip()
                )
            if tool.name in self._confirm_names:
                # McpToolset applies one setting to every tool; a per-name
                # policy has to be put on the tool after the fact. Assigning a
                # name ADK no longer reads would leave the tool unconfirmed
                # and say nothing, so the attribute has to be there already.
                if not hasattr(tool, "_require_confirmation"):
                    raise RuntimeError(
                        f"{type(tool).__name__} carries no _require_confirmation; "
                        f"{tool.name} cannot be marked for confirmation"
                    )
                tool._require_confirmation = True  # noqa: SLF001
            kept.append(tool)
        return kept

    def _warn_once(self, message: str) -> None:
        # The tools are listed for every model request; drift that lasts
        # would otherwise be logged on every turn and bury the rest.
        if message not in self._warned:
            self._warned.add(message)
            logger.warning(message)

    def _authorization_headers(self, readonly_context: Any) -> dict[str, str]:
        token = self._token(readonly_context)
        return {"Authorization": f"Bearer {token}"} if token else {}

    def _token(self, readonly_context: Any) -> str | None:
        """The user's token for this connection, or None. Values are never logged."""
        connection = self._connection
        key = self._authorization_key
        if connection is None or key is None:
            return None
        return usable_token(connection, key, getattr(readonly_context, "state", None))


def fixed_headers(
    headers: Mapping[str, str], connection_id: str | None
) -> dict[str, str]:
    """The declaration's headers, expanded; never one that stands in for the token.

    A fixed Authorization header is sent whenever the caller's own token is
    missing or refused, so the connection would answer as whoever the header
    names instead of stopping. The rule that a token is never replaced by
    another one is only kept if the header cannot be written in the first place.
    """
    if connection_id is not None:
        for name in headers:
            if name.lower() == "authorization":
                raise DeclarationError(
                    f"mcp header {name} would stand in for the {connection_id} "
                    "token whenever the caller has none; connections authorize "
                    "with the caller's own token"
                )
    return expand_headers(headers)


def mcp_toolset(
    spec: Mapping[str, Any],
    *,
    authorizations: Mapping[str, str],
    registry: Registry,
    confirm: bool,
    confirm_names: Iterable[str] = (),
    denied: Iterable[str] = (),
) -> GeteMcpToolset:
    """Build the toolset for one ``mcp:`` entry of a resolved declaration."""
    found = declaration_problems(spec)
    if found:
        # validate refuses these; checked again because the archive that
        # reached the runtime is whatever was packed.
        raise DeclarationError("mcp: " + "; ".join(found))
    connection_id: str | None = spec.get("connection")
    connection = registry.get(connection_id) if connection_id else None
    return GeteMcpToolset(
        url=str(spec["url"]),
        fixed_headers=fixed_headers(spec.get("headers", {}), connection_id),
        timeout=float(spec.get("timeout", DEFAULT_TIMEOUT_SECONDS)),
        allow=list(spec["allow"]) if spec.get("allow") else None,
        connection=connection,
        authorization_key=(
            authorizations.get(connection_id, connection_id) if connection_id else None
        ),
        does_not=spec.get("does_not"),
        confirm=confirm,
        confirm_names=confirm_names,
        denied=denied,
        params=spec.get("params"),
    )
