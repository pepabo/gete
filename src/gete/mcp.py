"""What an ``mcp:`` block may say about its tools' arguments.

A server's tool schemas are listed at run time and never packed, so the only
thing a declaration can be held against before then is itself: the tool
names it fixes or limits arguments for must be among the ones it offers,
and no argument may be both fixed and the model's. What each tool's schema
says is checked where the tools are listed.

Free of ADK, so validate runs it on machines that never deploy.
"""

from collections.abc import Mapping
from typing import Any

__all__ = ["declaration_problems"]


def declaration_problems(block: Mapping[str, Any]) -> list[str]:
    """The rules one ``mcp:`` block breaks that the schema cannot see.

    Returned messages carry no prefix; the caller adds where they were found.
    """
    allow = block.get("allow")
    found: list[str] = []
    for key in ("params", "only"):
        named: Mapping[str, Any] = block.get(key, {})
        if not named:
            continue
        if not allow:
            found.append(
                f"{key}: names tools out of allow, and this block has no allow; "
                "a misspelt name would leave the tool it meant as the server "
                "lists it"
            )
            continue
        found.extend(
            f"{key}: {name!r} is not one of this block's allow"
            for name in named
            if name not in allow
        )
    params: Mapping[str, Mapping[str, Any]] = block.get("params", {})
    for tool, names in block.get("only", {}).items():
        found.extend(
            f"only.{tool}: {name!r} is fixed by params; a fixed argument is "
            "not the model's to write"
            for name in names
            if name in params.get(tool, {})
        )
    return found
