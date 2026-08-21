"""A tiny Streamable HTTP MCP server for end-to-end tests.

It has no business logic. whoami returns the Authorization header it received,
which is what the tests need to see: that the user's token, and only the
user's token, reaches the server on every call.

Run it on its own with: uv run python tests/e2e/mcp_server.py 8765
"""

import sys
from typing import Any

import uvicorn
from mcp.server.fastmcp import Context, FastMCP

server = FastMCP("gete-e2e", stateless_http=True, json_response=True)


@server.tool()
def whoami(ctx: Context) -> dict[str, Any]:  # type: ignore[type-arg]
    """Return the Authorization header this request carried."""
    request = ctx.request_context.request
    header = request.headers.get("authorization") if request is not None else None
    return {"authorization": header}


@server.tool()
def lookup(query: str) -> dict[str, Any]:
    """Look something up."""
    return {"query": query}


def app() -> Any:
    return server.streamable_http_app()


if __name__ == "__main__":
    uvicorn.run(app(), host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
