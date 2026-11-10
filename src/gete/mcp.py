"""What an ``mcp:`` block may say about its tools' arguments.

A server's tool schemas are listed at run time and never packed, so the only
thing a declaration can be held against before then is itself: the tool
names it fixes arguments for must be among the ones it offers. What each
tool's schema says is checked where the tools are listed.

Free of ADK, so validate runs it on machines that never deploy.
"""

from collections.abc import Mapping
from typing import Any

__all__ = ["declaration_problems"]


def declaration_problems(block: Mapping[str, Any]) -> list[str]:
    """The rules one ``mcp:`` block breaks that the schema cannot see.

    Returned messages carry no prefix; the caller adds where they were found.
    """
    params: Mapping[str, Any] = block.get("params", {})
    if not params:
        return []
    allow = block.get("allow")
    if not allow:
        return [
            "params: names tools out of allow, and this block has no allow; "
            "a misspelt name would leave the tool it meant as the server lists it"
        ]
    return [
        f"params: {name!r} is not one of this block's allow"
        for name in params
        if name not in allow
    ]
