"""ADK callbacks that bind the tool call and redact what comes back."""

from collections.abc import Callable, Mapping
from typing import Any

from gete.connection.registry import Registry
from gete.redact import RedactRules, redact
from gete.request_context import ToolCall, set_tool_call


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
    """after_tool_callback that runs every dict result through the policies' redaction.

    ADK replaces the tool's result with whatever this returns, so the tool
    cannot forget. None keeps the original, which is what happens when there
    is nothing to redact.
    """
    active = bool(rules.keys or rules.digit_only_keys or rules.patterns)

    def after_tool(
        tool: Any, args: Mapping[str, Any], tool_context: Any, tool_response: Any
    ) -> Any:
        if not active or not isinstance(tool_response, dict):
            return None
        return redact(tool_response, rules)

    return after_tool
