"""Getting the user's token out of the state Gemini Enterprise hands over.

The key in tool_context.state is the authorization name itself, as created in
Gemini Enterprise, bare or under ADK's temp: prefix. Nothing is logged about
the values: a token is the user's credential, and the shape of the state is
enough to tell what went wrong.
"""

import logging
import re
from functools import cache
from typing import Any

from gete.connection.registry import (
    JWT_FORMAT,
    Connection,
    Registry,
    jwt_claims,
    looks_like_jwt,
)
from gete.request_context import current_tool_call

logger = logging.getLogger(__name__)

# Constraint on authorizationId in the Discovery Engine API: an RFC 1034
# label of at most 63 characters.
AUTHORIZATION_ID = re.compile(r"[a-z]([a-z0-9-]{0,61}[a-z0-9])?")
MAX_AUTHORIZATION_ID_LENGTH = 63


def authorization_id(agent_name: str, connection_id: str) -> str:
    """The name of the authorization for one agent and one connection.

    The connection id alone would not do: an authorization serves a single
    agent, so two agents using freee would fight over it. Registration creates
    this name and the runtime reads the state under it; both call this.
    """
    identifier = f"{agent_name}-{connection_id}"
    if len(identifier) > MAX_AUTHORIZATION_ID_LENGTH or not AUTHORIZATION_ID.fullmatch(
        identifier
    ):
        raise ValueError(
            f"authorization id {identifier!r} is not a label of at most "
            f"{MAX_AUTHORIZATION_ID_LENGTH} lowercase letters, digits, and hyphens"
        )
    return identifier


def state_of(tool_context: Any) -> Any:
    """Where the tokens live, or None without a tool context."""
    return getattr(tool_context, "state", None)


def _as_dict(state: Any) -> dict[str, Any]:
    # ADK's State is not a dict but offers to_dict.
    if hasattr(state, "to_dict"):
        values: dict[str, Any] = state.to_dict()
        return values
    return dict(state)


# ADK's marker for state that lives for the turn and is not persisted with
# the session. Agent Engine forwards the token from a Gemini Enterprise
# authorization as such state, so the key arrives wearing this prefix.
EPHEMERAL_PREFIX = "temp:"


def token_for(state: Any, key: str) -> str | None:
    """The non-empty string stored under this key, or its ephemeral twin, or None.

    Conversation state shares the same place. A prefix match would hand out
    google_calendar when google was asked for, so the key is matched whole;
    the one spelling accepted besides the bare key is the same key under
    EPHEMERAL_PREFIX, which is where Agent Engine puts a token it forwards
    rather than persists. The bare key wins when both are present.
    """
    values = _as_dict(state)
    for candidate in (key, f"{EPHEMERAL_PREFIX}{key}"):
        value = values.get(candidate)
        if isinstance(value, str) and value:
            return value
    return None


def describe_state(state: Any) -> list[dict[str, Any]]:
    """Keys, types, and string lengths of the state. Never the values."""
    return [
        {
            "key": str(key),
            "type": type(value).__name__,
            "length": len(value) if isinstance(value, str) else None,
        }
        for key, value in sorted(_as_dict(state).items(), key=lambda item: str(item[0]))
    ]


NO_DECLARED_SHAPE = "matches none of the declared shapes"


def describe_token(connection: Connection, token: str) -> str:
    """Which declared shape the token has, without repeating any of it.

    A connection without prefixes declares no shapes, so a refused JWT is
    described by what refused it - claims that cannot be read, or an issuer
    that does not name this service - or the shape message would leave the
    refusal looking like a mystery. A connection that declares a token format
    is refusing against a promise, and the promise is named: an operator who
    turned the provider's expiring-token setting off has every token refused
    at once, and nothing else in the log would say why. Read in the order
    accepts_token decides in, so the message names the rule that actually
    refused the token.
    """
    if connection.token_format is not None:
        return _unmet_format(connection.token_format, token)
    for prefix in connection.token_prefixes:
        if token.startswith(prefix):
            return f"starts with {prefix}"
    if connection.token_prefixes:
        return NO_DECLARED_SHAPE
    return _refused_jwt(token) or NO_DECLARED_SHAPE


def _unmet_format(token_format: str, token: str) -> str:
    """Which part of the declared format the token missed.

    A format this gete cannot judge refuses every token alike, so it is the
    format that has to be named and not the token: an operator told the
    issuer was wrong would go looking at a token that may be exactly right,
    when what refused it is a declaration newer than the gete reading it.
    """
    if token_format != JWT_FORMAT:
        return f"declared as {token_format}, which this gete cannot judge"
    if not looks_like_jwt(token):
        return f"not the {token_format} this connection declares"
    return _refused_jwt(token) or "a JWT that names no issuer"


def _refused_jwt(token: str) -> str | None:
    """Why a JWT-shaped token is not taken for this service's, or None when
    nothing about the JWT itself says so."""
    if not looks_like_jwt(token):
        return None
    claims = jwt_claims(token)
    if claims is None:
        return "a JWT whose claims cannot be read"
    if "iss" in claims:
        return "a JWT whose issuer does not name this service"
    return None


def usable_token(connection: Connection, key: str, state: Any) -> str | None:
    """The user's token for a connection under one key, or None with the reason logged.

    Values are never logged: a token is the user's credential, and its key and
    shape are enough to tell an authorization that never arrived from one that
    arrived carrying some other service's token.
    """
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


@cache
def _catalog() -> Registry:
    return Registry.from_catalog()


def resolve_connection(target: str | Connection) -> Connection:
    """A Connection for an id, from the running call's registry or the catalog."""
    if isinstance(target, Connection):
        return target
    call = current_tool_call()
    registry = (
        call.registry if call is not None and call.registry is not None else _catalog()
    )
    return registry.get(target)


def caller_token(target: str | Connection, state: Any = None) -> str | None:
    """The user's token for a connection, or None.

    The token is checked against the connection's declared shape before it is
    handed out. A key per connection does not say which kind of token the
    authorization produced; some services return an app token and a user
    token from the same flow. When the shape is wrong, nothing else is tried:
    the authorization arrived, and what is wrong is its configuration.

    Inside a tool call the state and the per-agent key come from the call, so
    tools need not carry either. Outside one, the key is the connection id.
    """
    connection = resolve_connection(target)
    call = current_tool_call()
    key = connection.id
    if call is not None:
        key = call.authorizations.get(connection.id, connection.id)
        if state is None:
            state = state_of(call.tool_context)
    if state is not None and (token := token_for(state, key)):
        if connection.accepts_token(token):
            return token
        logger.warning(
            "token for %s arrived under %r but has the wrong shape: %s",
            connection.id,
            key,
            describe_token(connection, token),
        )
        return None
    logger.warning(
        "no token for %s under %r; state holds %s",
        connection.id,
        key,
        describe_state(state) if state is not None else "(nothing)",
    )
    return None
