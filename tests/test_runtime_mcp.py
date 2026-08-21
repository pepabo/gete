"""MCP tools: the toolset the runtime builds and how the user's token reaches it."""

import logging
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import ProjectBuilder

from gete.connection import Registry
from gete.declaration import RESOLVED_FILE, load_project, resolve
from gete.errors import DeclarationError
from gete.request_context import clear_tool_call
from gete.runtime import build
from gete.runtime.mcp import GeteMcpToolset, mcp_toolset

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
    [built] = build(path).tools
    assert isinstance(built, GeteMcpToolset)
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


async def test_no_token_means_no_server_call_only_a_reauthorization_tool() -> None:
    """Port 9 is the discard port; any attempt to connect would fail loudly."""
    built = toolset({"url": "https://127.0.0.1:9/mcp", "connection": "freee"})
    tools = await built.get_tools(Context({}))
    assert [tool.name for tool in tools] == ["reauthorize_freee"]
    result = await tools[0].run_async(args={}, tool_context=Context({}))
    assert "freee" in str(result)
    assert "Gemini Enterprise" in str(result)


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
