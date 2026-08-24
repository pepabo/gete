"""Against a real MCP server: the user's token rides along on every call."""

import json
import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest
import uvicorn
from google.adk.agents import LlmAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.sessions import InMemorySessionService
from google.adk.tools.tool_context import ToolContext
from mcp_server import app

from gete.connection import Registry
from gete.runtime.mcp import mcp_toolset

CATALOG = Registry.from_catalog()
TOKEN_A = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
TOKEN_B = "0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f"


async def tool_context(state: dict[str, Any]) -> ToolContext:
    """A real ToolContext: McpTool.run_async reaches into its invocation context."""
    service = InMemorySessionService()
    session = await service.create_session(app_name="e2e", user_id="user", state=state)
    invocation = InvocationContext(
        session_service=service,
        invocation_id="inv-1",
        agent=LlmAgent(name="e2e", model="gemini-2.5-flash"),
        session=session,
    )
    return ToolContext(invocation)


@pytest.fixture(scope="module")
def server_url() -> Iterator[str]:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config = uvicorn.Config(app(), host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "the e2e MCP server did not start"
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    thread.join(timeout=5)


async def test_each_call_carries_the_current_users_token(server_url: str) -> None:
    toolset = mcp_toolset(
        {"url": server_url, "connection": "freee", "does_not": "Does not create deals"},
        authorizations={"freee": "finance-freee"},
        registry=CATALOG,
        confirm=False,
    )
    first = await tool_context({"finance-freee": TOKEN_A})
    tools = {tool.name: tool for tool in await toolset.get_tools(first)}
    assert set(tools) == {"whoami", "lookup"}
    assert tools["lookup"].description.endswith("Does not: Does not create deals")

    seen_a = await tools["whoami"].run_async(args={}, tool_context=first)
    assert f"Bearer {TOKEN_A}" in json.dumps(seen_a)

    second = await tool_context({"finance-freee": TOKEN_B})
    seen_b = await tools["whoami"].run_async(args={}, tool_context=second)
    assert f"Bearer {TOKEN_B}" in json.dumps(seen_b)
    assert TOKEN_A not in json.dumps(seen_b)


async def test_allow_list_limits_the_tools_offered(server_url: str) -> None:
    toolset = mcp_toolset(
        {"url": server_url, "connection": "freee", "allow": ["lookup"]},
        authorizations={"freee": "finance-freee"},
        registry=CATALOG,
        confirm=False,
    )
    tools = await toolset.get_tools(await tool_context({"finance-freee": TOKEN_A}))
    assert [tool.name for tool in tools] == ["lookup"]


async def test_a_denied_tool_is_not_offered(server_url: str) -> None:
    """deny_tools names a tool the server lists; it must not reach the model."""
    toolset = mcp_toolset(
        {"url": server_url, "connection": "freee"},
        authorizations={"freee": "finance-freee"},
        registry=CATALOG,
        confirm=False,
        denied=["whoami"],
    )
    tools = await toolset.get_tools(await tool_context({"finance-freee": TOKEN_A}))
    assert [tool.name for tool in tools] == ["lookup"]


async def test_a_named_tool_is_marked_for_confirmation(server_url: str) -> None:
    """McpToolset marks the whole set, so a single name is set on the tool itself."""
    toolset = mcp_toolset(
        {"url": server_url, "connection": "freee"},
        authorizations={"freee": "finance-freee"},
        registry=CATALOG,
        confirm=False,
        confirm_names=["lookup"],
    )
    context = await tool_context({"finance-freee": TOKEN_A})
    tools = {tool.name: tool for tool in await toolset.get_tools(context)}
    assert await tools["lookup"].check_require_confirmation({}, context) is True
    assert await tools["whoami"].check_require_confirmation({}, context) is False
