"""External services: where user tokens may go and what they look like."""

from gete.connection.registry import (
    Connection,
    Examples,
    OAuth,
    Registry,
    looks_like_jwt,
)

__all__ = ["Connection", "Examples", "OAuth", "Registry", "looks_like_jwt"]
