"""ADK callbacks that bind the tool call and redact what comes back."""

import logging
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


# What ADK puts in place of a tool when the function call names none.
MISSING_TOOL_DESCRIPTION = "Tool not found"


def safe_tool_error(rules: RedactRules) -> Callable[..., Any]:
    """on_tool_error_callback that keeps exception text away from the model.

    A raising tool's message may hold anything the tool touched - a response
    body, a path, a credential - and no rule set is trusted to catch it all.
    The model gets the exception's type and nothing else, and so does the
    log: not the message, and not the traceback either, whose tail would
    print the message right back. Only UserFacingError passes as written -
    raising it is the raiser's own declaration that the text was made to be
    shown - and even that passes through the policies' patterns on the way
    out.

    One error is not a tool's: the model calling a name no tool has. ADK
    runs the callbacks with a stand-in tool then, and the generic answer
    would leave the model with nothing to retry, so that one is answered
    with the names the model was offered - which are declarations, not
    data. Async because reading those names can be; ADK awaits a callback
    that returns an awaitable.
    """

    async def on_tool_error(
        tool: Any, args: Mapping[str, Any], tool_context: Any, error: Exception
    ) -> Any:
        if isinstance(error, UserFacingError):
            return {"error": redact(str(error), rules)}
        name = getattr(tool, "name", None) or type(tool).__name__
        offered = await _offered_tool_names(tool_context)
        if _names_no_tool(tool, name, offered):
            listed = ", ".join(offered) if offered else "none"
            logger.warning("no tool named %s; the tools are: %s", name, listed)
            return {"error": f"no tool named {name}; the tools are: {listed}"}
        logger.warning("tool %s failed with %s", name, type(error).__name__)
        return {
            "error": f"the tool failed with {type(error).__name__}; "
            "details are in the logs"
        }

    return on_tool_error


def _names_no_tool(tool: Any, name: str, offered: list[str] | None) -> bool:
    """Whether the call named no tool: ADK's stand-in, or a name not offered.

    The stand-in is recognised first, so a tool list that could not be read
    still tells the model the name was wrong; the list decides only when it
    was read.
    """
    if getattr(tool, "description", None) == MISSING_TOOL_DESCRIPTION:
        return True
    return offered is not None and name not in offered


async def _offered_tool_names(tool_context: Any) -> list[str] | None:
    """The names of the tools the model was offered, or None if unknown.

    Which tools a toolset offers is decided per turn - an MCP server's tools
    and the reauthorization tools are not known at build time - so ADK
    resolves them before every model call and keeps that turn's answer on
    the invocation. Read back from there: it is the list the model was
    actually given, and listing again would reach every MCP server over the
    network to re-derive what the turn already knows. Asking the agent is
    the fallback for an invocation that carries no such answer.
    """
    invocation = getattr(tool_context, "_invocation_context", None)
    offered = getattr(invocation, "canonical_tools_cache", None)
    if offered is None:
        offered = await _listed_tools(invocation, tool_context)
    if offered is None:
        return None
    names: list[str] = []
    for tool in offered:
        tool_name = getattr(tool, "name", None)
        if tool_name and tool_name not in names:
            names.append(str(tool_name))
    return names


async def _listed_tools(invocation: Any, tool_context: Any) -> list[Any] | None:
    """The agent's tools, listed from its toolsets, or None if they cannot be.

    Best effort: a failure to list must not turn an error callback into a
    second error.
    """
    listing = getattr(getattr(invocation, "agent", None), "canonical_tools", None)
    if listing is None:
        return None
    try:
        tools: list[Any] = await listing(tool_context)
    except Exception:
        # Whatever the listing raised - an MCP server down, most likely - is
        # its own problem to surface on the next turn; not here, not to the
        # model, and not as text in the log either.
        logger.warning("could not list the tools to name them")
        return None
    return tools


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
