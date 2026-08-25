"""Turning tool declarations into objects ADK accepts."""

import importlib
import sys
from collections.abc import Mapping, Sequence
from typing import Any

from gete.connection.registry import Registry
from gete.declaration import Agent
from gete.errors import DeclarationError
from gete.policies import Policy, tool_effect
from gete.runtime.mcp import mcp_toolset
from gete.shared_credentials import SHARED_CREDENTIALS


class Confirmation:
    """Which tools ADK should stop for confirmation, from the policies.

    Only in effect when the project chose confirmation: adk. Otherwise the
    policy text governs and nothing is marked.
    """

    def __init__(self, policies: Sequence[Policy], mode: str) -> None:
        active = mode == "adk"
        self.everything = active and any(
            p.require_confirmation == "all" for p in policies
        )
        self.writes = active and any(
            p.require_confirmation == "write_tools" for p in policies
        )
        self.names: frozenset[str] = frozenset(
            name
            for policy in policies
            if active and isinstance(policy.require_confirmation, tuple)
            for name in policy.require_confirmation
        )

    def for_effect(self, effect: str) -> bool:
        return self.everything or (self.writes and effect == "write")

    def for_tool(self, name: str | None, effect: str) -> bool:
        return self.for_effect(effect) or (name is not None and name in self.names)


def build_tools(
    agent: Agent,
    policies: Sequence[Policy],
    *,
    authorizations: Mapping[str, str],
    registry: Registry,
    confirmation: str,
) -> list[Any]:
    """Every declared tool, minus those a policy denies by name."""
    from google.adk.tools.function_tool import FunctionTool

    denied = {name for policy in policies for name in policy.deny_tools}
    confirm = Confirmation(policies, confirmation)
    tools: list[Any] = []
    for tool in agent.tools:
        effect = tool_effect(tool)
        if "builtin" in tool:
            # ADK's own tool objects are used as they are; they cannot be
            # wrapped for confirmation.
            tools.append(builtin_tool(tool["builtin"]))
        elif "python" in tool:
            for function in python_tools(agent, tool["python"]):
                if confirm.for_tool(tool_name(function), effect):
                    tools.append(FunctionTool(function, require_confirmation=True))
                else:
                    tools.append(function)
        elif "mcp" in tool:
            tools.append(
                mcp_toolset(
                    tool["mcp"],
                    authorizations=authorizations,
                    registry=registry,
                    confirm=confirm.for_effect(effect),
                    confirm_names=confirm.names,
                    denied=denied,
                )
            )
    # Shared credential tools carry their effects with them; the write among
    # them is confirmed and denied like any declared write tool.
    for name in agent.shared_credentials:
        for function, shared_effect in SHARED_CREDENTIALS[name].load_tools():
            if confirm.for_tool(tool_name(function), shared_effect):
                tools.append(FunctionTool(function, require_confirmation=True))
            else:
                tools.append(function)
    return [tool for tool in tools if tool_name(tool) not in denied]


def tool_name(tool: Any) -> str | None:
    """ADK tool objects carry name; plain functions carry __name__."""
    name = getattr(tool, "name", None) or getattr(tool, "__name__", None)
    return str(name) if name else None


def builtin_tool(name: str) -> Any:
    import google.adk.tools as builtin_tools

    try:
        return getattr(builtin_tools, name)
    except AttributeError:
        raise DeclarationError(f"google.adk.tools has no {name!r}") from None


def python_tools(agent: Agent, spec: str | Mapping[str, Any]) -> list[Any]:
    """Import pkg.module:ATTR below the agent's source and return its tools."""
    ref = spec if isinstance(spec, str) else str(spec["ref"])
    source = agent.source
    if source is None:
        raise DeclarationError(f"python tool {ref!r} needs source to be set")
    root = str(source.resolve())
    if root not in sys.path:
        sys.path.insert(0, root)
    module_name, _, attribute = ref.partition(":")
    value = getattr(importlib.import_module(module_name), attribute)
    if callable(value):
        return [value]
    if isinstance(value, list | tuple):
        return list(value)
    raise DeclarationError(f"{ref} is neither a function nor a list of functions")
