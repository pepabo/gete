"""MCP tools: ADK's McpToolset with the user's token and gete's rules on top."""

import logging
import os
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from google.adk.tools.function_tool import FunctionTool
from google.adk.tools.mcp_tool import McpToolset, StreamableHTTPConnectionParams

from gete.connection.registry import Connection, Registry
from gete.connection.runtime import describe_state, describe_token, token_for
from gete.errors import DeclarationError

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


class GeteMcpToolset(McpToolset):
    """McpToolset that sends the calling user's token and honours the declaration.

    With a connection, every request carries ``Authorization: Bearer <token>``
    taken from the state under the agent's authorization name, and only when
    the token has the connection's shape. Without a usable token the server is
    not contacted at all; the only tool offered tells the user to authorize
    again. ``does_not`` is appended to every tool description.
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
    def connection_id(self) -> str | None:
        return self._connection.id if self._connection is not None else None

    async def get_tools(self, readonly_context: Any = None) -> list[Any]:
        if (
            self._connection is not None
            and readonly_context is not None
            and self._token(readonly_context) is None
        ):
            return [self._reauthorization_tool(self._connection)]
        tools: list[Any] = await super().get_tools(readonly_context)
        kept = []
        for tool in tools:
            if tool.name in self._denied:
                continue
            if self.does_not:
                tool.description = (
                    f"{tool.description}\n\nDoes not: {self.does_not}".strip()
                )
            if tool.name in self._confirm_names:
                # McpToolset applies one setting to every tool; a per-name
                # policy has to be put on the tool after the fact.
                tool._require_confirmation = True  # noqa: SLF001
            kept.append(tool)
        return kept

    def _authorization_headers(self, readonly_context: Any) -> dict[str, str]:
        token = self._token(readonly_context)
        return {"Authorization": f"Bearer {token}"} if token else {}

    def _token(self, readonly_context: Any) -> str | None:
        """The user's token for this connection, or None. Values are never logged."""
        connection = self._connection
        key = self._authorization_key
        if connection is None or key is None:
            return None
        state = getattr(readonly_context, "state", None)
        if state is None:
            return None
        token = token_for(state, key)
        if token is None:
            logger.warning(
                "no token for %s under %r; state holds %s",
                connection.id,
                key,
                describe_state(state),
            )
            return None
        if not connection.accepts_token(token):
            logger.warning(
                "token for %s under %r has the wrong shape: %s",
                connection.id,
                key,
                describe_token(connection, token),
            )
            return None
        return token

    @staticmethod
    def _reauthorization_tool(connection: Connection) -> Any:
        message = connection.reauthorization_message()

        def reauthorize() -> dict[str, str]:
            return {"error": message}

        reauthorize.__name__ = f"reauthorize_{connection.id.replace('-', '_')}"
        reauthorize.__doc__ = (
            f"The {connection.display_name} tools are unavailable until the user "
            "authorizes again. Call this to get the message to show them."
        )
        return FunctionTool(reauthorize)


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
    )
