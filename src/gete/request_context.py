"""What a tool can learn about the call it is running in.

Agent Engine hands over the user's tokens in tool_context.state, one per
authorization. Tools that take a tool_context could read it themselves, but
many call a shared client and never see one. Passing the context by argument
would be a convention, and the one tool that forgets it would silently fail.
So the before_tool_callback puts it here, and readers need not know how.
"""

import hashlib
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gete.connection.registry import Registry
    from gete.redact import RedactRules

UNKNOWN_CALLER = "unknown"


@dataclass(frozen=True)
class ToolCall:
    """The tool call that is running right now.

    authorizations maps a connection id to the authorization name under which
    Gemini Enterprise stores that connection's token in the state. The names
    are per agent, because one authorization serves one agent only.
    """

    tool_context: Any
    authorizations: Mapping[str, str] = field(default_factory=dict)
    registry: "Registry | None" = None
    redact_rules: "RedactRules | None" = None


# ADK runs each tool call in a task of its own, the callback and the tool body
# together, so a value set here stays inside that call. Concurrent users do
# not see each other's.
_tool_call: ContextVar[ToolCall | None] = ContextVar("gete_tool_call", default=None)


def set_tool_call(call: ToolCall) -> None:
    """Record the current call. It is not reset afterwards.

    The task ends with the call and takes the value with it. Resetting in an
    after_tool_callback would miss every call that leaves through an exception.
    """
    _tool_call.set(call)


def clear_tool_call() -> None:
    """For tests, which run many calls in one context."""
    _tool_call.set(None)


def current_tool_call() -> ToolCall | None:
    """The running tool call, or None outside of one."""
    return _tool_call.get()


def caller_fingerprint(tool_context: Any = None) -> str:
    """A short, stable stand-in for the calling user.

    It says whether two calls came from the same person, not who they are.
    The identifier itself never leaves; only the start of its hash does.
    """
    if tool_context is None and (call := _tool_call.get()) is not None:
        tool_context = call.tool_context
    user_id = getattr(tool_context, "user_id", None)
    if isinstance(user_id, str) and user_id:
        return hashlib.sha256(user_id.encode()).hexdigest()[:12]
    return UNKNOWN_CALLER
