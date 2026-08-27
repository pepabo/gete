"""The tool a user is handed when a connection's token is missing.

One per connection, offered by the agent rather than by the toolset that
noticed. A connection can back more than one toolset - splitting a server's
reads from its writes takes a second ``mcp:`` block, because ``effect`` is
declared per block - and a tool per toolset would put two functions of the
same name in front of the model. The model API refuses a request that declares
one name twice, so the agent answers nothing at all, and it happens to every
user who has not authorized yet.
"""

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool

from gete.connection.registry import Connection
from gete.connection.runtime import usable_token


def reauthorization_tool(connection: Connection) -> Any:
    """A tool whose whole purpose is to hand the user the connection's prompt."""
    message = connection.reauthorization_message()

    def reauthorize() -> dict[str, str]:
        return {"error": message}

    reauthorize.__name__ = f"reauthorize_{connection.id.replace('-', '_')}"
    reauthorize.__doc__ = (
        f"The {connection.display_name} tools are unavailable until the user "
        "authorizes again. Call this to get the message to show them."
    )
    return FunctionTool(reauthorize)


class ReauthorizationToolset(BaseToolset):
    """Offers one tool per connection whose token is missing, and nothing else.

    Named per connection, not per toolset, so the user is told which
    authorization to approve. Which tokens are there is decided per request,
    so the decision cannot be made when the agent is built.
    """

    def __init__(
        self,
        connections: Sequence[Connection],
        authorization_keys: Mapping[str, str],
    ) -> None:
        super().__init__()
        self._connections = tuple(connections)
        self._keys = dict(authorization_keys)

    @property
    def connection_ids(self) -> tuple[str, ...]:
        return tuple(connection.id for connection in self._connections)

    async def get_tools(self, readonly_context: Any = None) -> list[Any]:
        # No context at all counts as no token: the Agent Card is built that
        # way, and a card listing the server's tools would promise what an
        # unauthorized user cannot call.
        state = getattr(readonly_context, "state", None)
        return [
            reauthorization_tool(connection)
            for connection in self._connections
            if usable_token(connection, self._keys[connection.id], state) is None
        ]

    async def close(self) -> None:
        """Nothing is held open; the tools are built per request."""


def reauthorization_toolset(
    connections: Iterable[Connection], authorizations: Mapping[str, str]
) -> ReauthorizationToolset | None:
    """The toolset for these connections, or None when there are none to ask for."""
    ordered = sorted(
        {connection.id: connection for connection in connections}.values(),
        key=lambda connection: connection.id,
    )
    if not ordered:
        return None
    return ReauthorizationToolset(
        ordered,
        {
            connection.id: authorizations.get(connection.id, connection.id)
            for connection in ordered
        },
    )
