"""Turning tool declarations into objects ADK accepts."""

import importlib
import sys
from collections.abc import Mapping, Sequence
from typing import Any

from gete.declaration import Agent
from gete.errors import DeclarationError
from gete.policies import Policy


def build_tools(agent: Agent, policies: Sequence[Policy]) -> list[Any]:
    """Every declared tool, minus those a policy denies by name."""
    denied = {name for policy in policies for name in policy.deny_tools}
    tools: list[Any] = []
    for tool in agent.tools:
        if "builtin" in tool:
            tools.append(builtin_tool(tool["builtin"]))
        elif "python" in tool:
            tools.extend(python_tools(agent, tool["python"]))
        elif "mcp" in tool:
            raise NotImplementedError("mcp tools are not supported by the runtime yet")
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
