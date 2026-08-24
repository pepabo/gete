"""ADK callbacks that bind the tool call and redact what comes back."""

import logging
import traceback
from collections.abc import Callable, Mapping, Sequence, Set
from typing import Any

from gete.connection.registry import Registry
from gete.errors import GeteError, UserFacingError
from gete.redact import RedactRules, redact
from gete.request_context import ToolCall, set_tool_call

logger = logging.getLogger(__name__)


def bind_tool_call(
    authorizations: Mapping[str, str], registry: Registry, rules: RedactRules
) -> Callable[..., None]:
    """before_tool_callback that records the call for tools that never see a context.

    It returns None; anything else would replace the tool's response.
    """

    def before_tool(tool: Any, args: Mapping[str, Any], tool_context: Any) -> None:
        set_tool_call(
            ToolCall(
                tool_context,
                dict(authorizations),
                registry=registry,
                redact_rules=rules,
            )
        )

    return before_tool


def redact_results(rules: RedactRules) -> Callable[..., Any]:
    """after_tool_callback that runs every result through the policies' redaction.

    ADK replaces the tool's result with whatever this returns, so the tool
    cannot forget. None keeps the original, which is what happens when there
    is nothing to redact. The result's shape does not matter: redact walks
    dicts and lists and runs the patterns over strings, so a tool answering
    with a list or plain text is covered like any other.
    """
    active = bool(rules.keys or rules.digit_only_keys or rules.patterns)

    def after_tool(
        tool: Any, args: Mapping[str, Any], tool_context: Any, tool_response: Any
    ) -> Any:
        if not active:
            return None
        return redact(_jsonable(tool_response), rules)

    return after_tool


def safe_tool_error(rules: RedactRules) -> Callable[..., Any]:
    """on_tool_error_callback that keeps exception text away from the model.

    A raising tool's message may hold anything the tool touched - a response
    body, a path, a credential - and no rule set is trusted to catch it all.
    The model gets the exception's type and nothing else, and so does the
    log: the type and the frames, which are program text, never the message.
    Only UserFacingError passes as written - raising it is the raiser's own
    declaration that the text was made to be shown - and even that passes
    through the policies' patterns on the way out.
    """

    def on_tool_error(
        tool: Any, args: Mapping[str, Any], tool_context: Any, error: Exception
    ) -> Any:
        if isinstance(error, UserFacingError):
            return {"error": redact(str(error), rules)}
        logger.warning(
            "tool %s failed with %s\n%s",
            getattr(tool, "name", tool),
            type(error).__name__,
            "".join(traceback.format_tb(error.__traceback__)),
        )
        return {
            "error": f"the tool failed with {type(error).__name__}; "
            "details are in the logs"
        }

    return on_tool_error


def _jsonable(value: Any) -> Any:
    """The result rebuilt from JSON-compatible values, or GeteError.

    Redaction walks dicts, lists, and strings; anything else would carry its
    content past the rules. A result that cannot be rebuilt is refused
    outright - an unredactable answer must not reach the model.
    """
    if isinstance(value, str):
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise GeteError("a bytes tool result cannot be checked for redaction")
    if isinstance(value, (Sequence, Set)):
        return [_jsonable(item) for item in value]
    raise GeteError(
        f"a {type(value).__name__} tool result cannot be checked for redaction"
    )
