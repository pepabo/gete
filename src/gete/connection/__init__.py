"""External services: where tokens may go, what they look like, how to read them."""

from gete.connection.client import (
    DEFAULT_BACKOFF_SECONDS,
    MAX_FILE_BYTES,
    ConnectionClient,
    ExternalServiceError,
    ReauthorizationRequired,
    parse_retry_after,
    shared_client,
)
from gete.connection.registry import (
    Connection,
    Examples,
    OAuth,
    Registry,
    looks_like_jwt,
)
from gete.connection.runtime import (
    authorization_id,
    caller_token,
    describe_state,
    resolve_connection,
)

__all__ = [
    "DEFAULT_BACKOFF_SECONDS",
    "MAX_FILE_BYTES",
    "Connection",
    "ConnectionClient",
    "Examples",
    "ExternalServiceError",
    "OAuth",
    "ReauthorizationRequired",
    "Registry",
    "authorization_id",
    "caller_token",
    "describe_state",
    "looks_like_jwt",
    "parse_retry_after",
    "resolve_connection",
    "shared_client",
]
