"""MCP tools: the toolset the runtime builds and how the user's token reaches it."""

import logging
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import ProjectBuilder
from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.mcp_tool import McpToolset

from gete.connection import Registry
from gete.declaration import RESOLVED_FILE, load_project, resolve
from gete.errors import DeclarationError
from gete.request_context import clear_tool_call
from gete.runtime import build
from gete.runtime.mcp import GeteMcpToolset, mcp_toolset
from gete.runtime.reauthorization import ReauthorizationToolset

CATALOG = Registry.from_catalog()
FREEE_TOKEN = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
URL = "https://api.freee.co.jp/mcp"


class Context:
    """The part of ADK's ReadonlyContext / ToolContext the runtime reads."""

    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state
        self.user_id = "user"


def teardown_function() -> None:
    clear_tool_call()


def resolved_path(
    project: ProjectBuilder,
    name: str,
    agent: dict[str, Any],
    *,
    confirmation: str | None = None,
    policies: list[dict[str, Any]] | None = None,
) -> Path:
    document: dict[str, Any] = {
        "version": 1,
        "project": "example-project",
        "location": "us-central1",
    }
    if confirmation:
        document["confirmation"] = confirmation
    if policies:
        project.write_policies("p", policies)
        document["policies"] = ["./policies/p.yaml"]
    project.write_project(document)
    directory = project.write_agent(name, agent)
    loaded = load_project(project.root / "gete.yaml")
    resolved = resolve(loaded, next(a for a in loaded.agents if a.name == name))
    path = directory / RESOLVED_FILE
    path.write_text(yaml.safe_dump(resolved, sort_keys=False))
    return path


def toolset(spec: dict[str, Any], *, confirm: bool = False) -> GeteMcpToolset:
    return mcp_toolset(
        spec,
        authorizations={"freee": "mail-triage-freee"},
        registry=CATALOG,
        confirm=confirm,
    )


def test_build_turns_the_declaration_into_a_toolset(
    project: ProjectBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRACE", "t-1")
    path = resolved_path(
        project,
        "mail-triage",
        {
            "connections": ["freee"],
            "tools": [
                {
                    "mcp": {
                        "url": URL,
                        "connection": "freee",
                        "headers": {"X-Trace": "${TRACE}"},
                        "allow": ["get_deals"],
                        "timeout": 10,
                        "does_not": "Does not create deals",
                    }
                }
            ],
        },
    )
    built, asking = build(path).tools
    assert isinstance(built, GeteMcpToolset)
    assert isinstance(asking, ReauthorizationToolset)
    assert built.url == URL
    assert built.fixed_headers == {"X-Trace": "t-1"}
    assert built.timeout == 10
    assert built.allow == ["get_deals"]
    assert built.connection_id == "freee"
    assert built.does_not == "Does not create deals"


def test_missing_environment_variable_in_headers_fails_at_build(
    project: ProjectBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A header that silently became empty would be a hard-to-see auth failure later."""
    monkeypatch.delenv("NOPE", raising=False)
    path = resolved_path(
        project,
        "mail-triage",
        {
            "tools": [
                {
                    "mcp": {
                        "url": "https://mcp.example.com/mcp",
                        "headers": {"X": "${NOPE}"},
                    }
                }
            ]
        },
    )
    with pytest.raises(DeclarationError, match="NOPE"):
        build(path)


def test_header_provider_sends_the_token_stored_under_the_agents_key() -> None:
    provider = toolset({"url": URL, "connection": "freee"}).header_provider
    assert provider is not None
    assert provider(Context({"mail-triage-freee": FREEE_TOKEN})) == {
        "Authorization": f"Bearer {FREEE_TOKEN}"
    }
    assert provider(Context({"freee": FREEE_TOKEN})) == {}


def test_header_provider_sends_nothing_for_a_wrong_shape_and_logs_no_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = toolset({"url": URL, "connection": "freee"}).header_provider
    assert provider is not None
    with caplog.at_level(logging.WARNING):
        assert provider(Context({"mail-triage-freee": "ya29.not-freee"})) == {}
    assert "ya29.not-freee" not in caplog.text


def test_a_fixed_authorization_header_is_refused_next_to_a_connection() -> None:
    """It would be sent whenever the user's own token is missing or refused.

    The name is spelled in lower case here: HTTP header names do not care, and
    neither may the check.
    """
    with pytest.raises(DeclarationError, match="stand in for the freee token"):
        toolset({"url": URL, "connection": "freee", "headers": {"authorization": "x"}})


def test_a_fixed_authorization_header_is_allowed_without_a_connection() -> None:
    """No connection means no user token to mask; the header is the only credential."""
    built = toolset({"url": URL, "headers": {"Authorization": "Bearer fixed"}})
    assert built.fixed_headers == {"Authorization": "Bearer fixed"}


def test_mcp_without_a_connection_sends_no_authorization() -> None:
    built = toolset({"url": "https://mcp.example.com/mcp"})
    assert built.header_provider is None
    assert built.connection_id is None


async def test_confirmation_adk_marks_write_tools_for_confirmation(
    project: ProjectBuilder,
) -> None:
    path = resolved_path(
        project,
        "mail-triage",
        {
            "source": "./src",
            "tools": [
                {"mcp": {"url": "https://mcp.example.com/mcp"}},
                {"mcp": {"url": "https://mcp.example.com/read", "effect": "read"}},
                {"python": {"ref": "mail_triage.tools:TOOLS", "effect": "write"}},
            ],
        },
        confirmation="adk",
        policies=[
            {
                "name": "writes",
                "when": "has_write_tools",
                "require_confirmation": "write_tools",
            }
        ],
    )
    write_tools_module(path.parent)
    writer, reader, lookup, transfer = build(path).tools
    assert writer.require_confirmation is True
    assert reader.require_confirmation is False
    assert await lookup.check_require_confirmation({}, Context({})) is True
    assert await transfer.check_require_confirmation({}, Context({})) is True


def test_confirmation_instruction_leaves_tools_unmarked(
    project: ProjectBuilder,
) -> None:
    """Until Gemini Enterprise is known to surface ADK confirmations, text governs."""
    path = resolved_path(
        project,
        "mail-triage",
        {
            "source": "./src",
            "tools": [
                {"mcp": {"url": "https://mcp.example.com/mcp"}},
                {"python": "mail_triage.tools:TOOLS"},
            ],
        },
        policies=[
            {
                "name": "writes",
                "when": "has_write_tools",
                "require_confirmation": "write_tools",
            }
        ],
    )
    write_tools_module(path.parent)
    built, lookup, transfer = build(path).tools
    assert built.require_confirmation is False
    assert callable(lookup) and not hasattr(lookup, "check_require_confirmation")


def write_tools_module(directory: Path) -> None:
    package = directory / "src" / "mail_triage"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "tools.py").write_text(
        "def lookup(query: str) -> dict:\n"
        '    """Look something up."""\n'
        '    return {"query": query}\n'
        "\n"
        "def transfer(amount: int) -> dict:\n"
        '    """Move money."""\n'
        '    return {"moved": amount}\n'
        "\n"
        "TOOLS = [lookup, transfer]\n"
    )


async def test_a_confirmation_flag_adk_no_longer_carries_fails_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting a renamed flag would leave a write tool quietly unconfirmed."""

    class Tool:
        name = "lookup"
        description = "Look something up."

    async def tools(self: Any, readonly_context: Any = None) -> list[Any]:
        return [Tool()]

    monkeypatch.setattr(McpToolset, "get_tools", tools)
    built = mcp_toolset(
        {"url": URL},
        authorizations={},
        registry=CATALOG,
        confirm=False,
        confirm_names=["lookup"],
    )
    with pytest.raises(RuntimeError, match="require_confirmation"):
        await built.get_tools(Context({}))


async def offered(path: Path, state: dict[str, Any]) -> list[str]:
    """Every tool name the model would be shown, toolsets resolved as ADK does."""
    names: list[str] = []
    for tool in build(path).tools:
        if isinstance(tool, BaseToolset):
            names.extend(offer.name for offer in await tool.get_tools(Context(state)))
        else:
            names.append(str(tool.__name__))
    return names


async def reauthorizations(path: Path, state: dict[str, Any]) -> list[str]:
    """Only the tools that ask the user to approve a connection again."""
    names: list[str] = []
    for tool in build(path).tools:
        if isinstance(tool, ReauthorizationToolset):
            names.extend(offer.name for offer in await tool.get_tools(Context(state)))
    return names


# Port 9 is the discard port; a toolset that tried to list would fail loudly.
UNREACHABLE = "https://127.0.0.1:9/mcp"


async def test_two_toolsets_on_one_connection_offer_one_reauthorization_tool(
    project: ProjectBuilder,
) -> None:
    """Two declarations of one function are refused by the model API.

    Splitting a server's reads from its writes takes two mcp: blocks, since
    effect is per block. Nothing else about such an agent looks broken:
    validate passes, and every user without a token lands in it.
    """
    path = resolved_path(
        project,
        "mail-triage",
        {
            "connections": ["freee"],
            "tools": [
                {
                    "mcp": {
                        "url": UNREACHABLE,
                        "connection": "freee",
                        "allow": ["get_deals"],
                        "effect": "read",
                    }
                },
                {
                    "mcp": {
                        "url": UNREACHABLE,
                        "connection": "freee",
                        "allow": ["create_deal"],
                    }
                },
            ],
        },
    )
    assert await offered(path, {}) == ["reauthorize_freee"]


async def test_each_connection_still_gets_its_own_reauthorization_tool(
    project: ProjectBuilder,
) -> None:
    """The user is told which authorization to approve; one tool would not say."""
    path = resolved_path(
        project,
        "mail-triage",
        {
            "connections": ["freee", "github"],
            "tools": [
                {"mcp": {"url": UNREACHABLE, "connection": "freee"}},
                {"mcp": {"url": "https://api.github.com/mcp", "connection": "github"}},
            ],
        },
    )
    assert sorted(await offered(path, {})) == [
        "reauthorize_freee",
        "reauthorize_github",
    ]


async def test_a_connection_holding_a_token_is_not_asked_to_authorize_again(
    project: ProjectBuilder,
) -> None:
    path = resolved_path(
        project,
        "mail-triage",
        {
            "connections": ["freee", "github"],
            "tools": [
                {"mcp": {"url": UNREACHABLE, "connection": "freee"}},
                {"mcp": {"url": "https://api.github.com/mcp", "connection": "github"}},
            ],
        },
    )
    state = {"mail-triage-github": "gho_16C7e42F292c6912E7710c838347Ae178B4a"}
    assert await reauthorizations(path, state) == ["reauthorize_freee"]
    assert await reauthorizations(path, {"mail-triage-freee": FREEE_TOKEN}) == [
        "reauthorize_github"
    ]


async def test_the_reauthorization_tool_names_the_connection_to_the_user(
    project: ProjectBuilder,
) -> None:
    path = resolved_path(
        project,
        "mail-triage",
        {
            "connections": ["freee"],
            "tools": [{"mcp": {"url": UNREACHABLE, "connection": "freee"}}],
        },
    )
    [asking] = [t for t in build(path).tools if isinstance(t, ReauthorizationToolset)]
    [tool] = await asking.get_tools(Context({}))
    result = await tool.run_async(args={}, tool_context=Context({}))
    assert "freee" in str(result)
    assert "Gemini Enterprise" in str(result)
    # agent.canonical_tools() passes no context; the rule must hold there too.
    assert [offer.name for offer in await asking.get_tools()] == ["reauthorize_freee"]


async def test_a_toolset_without_a_token_offers_nothing_of_its_own() -> None:
    """The tool that asks for authorization belongs to the connection, not here."""
    built = toolset({"url": UNREACHABLE, "connection": "freee"})
    assert await built.get_tools(Context({})) == []
    assert await built.get_tools() == []


async def test_an_mcp_tool_without_a_connection_asks_nobody_to_authorize(
    project: ProjectBuilder,
) -> None:
    path = resolved_path(
        project, "mail-triage", {"tools": [{"mcp": {"url": UNREACHABLE}}]}
    )
    assert not [t for t in build(path).tools if isinstance(t, ReauthorizationToolset)]
