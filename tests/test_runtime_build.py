"""Building the ADK agent from a resolved declaration."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from conftest import ProjectBuilder

from gete.declaration import RESOLVED_FILE, load_project, resolve
from gete.errors import GeteError
from gete.request_context import clear_tool_call, current_tool_call
from gete.runtime import build

POLICIES: list[dict[str, Any]] = [
    {"name": "finance", "when": "always", "instruction_prefix": "Never approve."},
    {
        "name": "writes",
        "when": "has_write_tools",
        "instruction_prefix": "Show before you write.",
        "redact": {
            "keys": ["bank_name"],
            "patterns": [{"pattern": "\\bcard-\\d+\\b", "replacement": "[card]"}],
        },
    },
]


def teardown_function() -> None:
    clear_tool_call()


def resolved_path(project: ProjectBuilder, name: str, agent: dict[str, Any]) -> Path:
    project.write_policies("p", POLICIES)
    project.write_project(
        {
            "version": 1,
            "project": "example-project",
            "location": "us-central1",
            "policies": ["./policies/p.yaml"],
        }
    )
    directory = project.write_agent(name, agent)
    loaded = load_project(project.root / "gete.yaml")
    document = resolve(loaded, next(a for a in loaded.agents if a.name == name))
    path = directory / RESOLVED_FILE
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    return path


def write_tools_module(directory: Path) -> None:
    package = directory / "src" / "mail_triage"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "tools.py").write_text(
        "def lookup(query: str) -> dict:\n"
        '    """Look something up."""\n'
        '    return {"bank_name": "Example Bank", "query": query}\n'
        "\n"
        "def transfer(amount: int) -> dict:\n"
        '    """Move money."""\n'
        '    return {"moved": amount}\n'
        "\n"
        "TOOLS = [lookup, transfer]\n"
    )


def test_agent_carries_name_model_description_and_policy_prefixed_instruction(
    project: ProjectBuilder,
) -> None:
    agent = build(resolved_path(project, "mail-triage", {}))
    assert agent.name == "mail_triage"
    assert agent.model == "gemini-2.5-flash"
    assert agent.description == "Sorts pasted mail by urgency"
    assert agent.instruction == "Never approve.\n\nYou sort mail."


def test_write_policy_applies_only_to_agents_with_write_tools(
    project: ProjectBuilder,
) -> None:
    reader = resolved_path(
        project,
        "reader",
        {
            "source": "./src",
            "tools": [{"python": {"ref": "mail_triage.tools:TOOLS", "effect": "read"}}],
        },
    )
    write_tools_module(reader.parent)
    writer = resolved_path(project, "writer", {"tools": [{"builtin": "google_search"}]})
    assert "Show before you write." not in str(build(reader).instruction)
    assert str(build(writer).instruction).startswith(
        "Never approve.\n\nShow before you write."
    )


def test_builtin_tools_come_from_google_adk_tools(project: ProjectBuilder) -> None:
    from google.adk.tools import google_search

    agent = build(
        resolved_path(project, "mail-triage", {"tools": [{"builtin": "google_search"}]})
    )
    assert google_search in agent.tools


def test_python_tools_are_imported_from_source(project: ProjectBuilder) -> None:
    path = resolved_path(
        project,
        "mail-triage",
        {"source": "./src", "tools": [{"python": "mail_triage.tools:TOOLS"}]},
    )
    write_tools_module(path.parent)
    names = [getattr(tool, "__name__", None) for tool in build(path).tools]
    assert names == ["lookup", "transfer"]


def test_denied_tools_are_left_out(project: ProjectBuilder) -> None:
    path = resolved_path(
        project,
        "mail-triage",
        {"source": "./src", "tools": [{"python": "mail_triage.tools:TOOLS"}]},
    )
    write_tools_module(path.parent)
    document = yaml.safe_load(path.read_text())
    document["resolved"]["policies"].append(
        {"name": "no-transfers", "when": "always", "deny_tools": ["transfer"]}
    )
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    names = [getattr(tool, "__name__", None) for tool in build(path).tools]
    assert names == ["lookup"]


def test_before_tool_callback_binds_the_call_with_agent_specific_authorizations(
    project: ProjectBuilder,
) -> None:
    agent = build(
        resolved_path(project, "mail-triage", {"connections": ["freee", "github"]})
    )
    context = SimpleNamespace(state={}, user_id="u")
    assert agent.before_tool_callback is not None
    assert agent.before_tool_callback(SimpleNamespace(), {}, context) is None  # type: ignore[call-arg, operator]
    call = current_tool_call()
    assert call is not None
    assert call.tool_context is context
    assert call.authorizations == {
        "freee": "mail-triage-freee",
        "github": "mail-triage-github",
    }
    assert call.registry is not None and call.registry.get("freee").id == "freee"


def test_after_tool_callback_redacts_even_when_the_tool_forgot(
    project: ProjectBuilder,
) -> None:
    agent = build(
        resolved_path(project, "mail-triage", {"tools": [{"builtin": "google_search"}]})
    )
    assert agent.after_tool_callback is not None
    result = agent.after_tool_callback(  # type: ignore[call-arg, operator]
        SimpleNamespace(), {}, SimpleNamespace(), {"bank_name": "Example Bank", "x": 1}
    )
    assert result == {"bank_name": "[redacted]", "x": 1}


def test_after_tool_callback_redacts_lists_and_text_too(
    project: ProjectBuilder,
) -> None:
    """The result's shape does not decide whether the policies apply."""
    agent = build(
        resolved_path(project, "mail-triage", {"tools": [{"builtin": "google_search"}]})
    )
    assert agent.after_tool_callback is not None
    listed = agent.after_tool_callback(  # type: ignore[call-arg, operator]
        SimpleNamespace(), {}, SimpleNamespace(), [{"bank_name": "Example Bank"}]
    )
    assert listed == [{"bank_name": "[redacted]"}]
    text = agent.after_tool_callback(  # type: ignore[call-arg, operator]
        SimpleNamespace(), {}, SimpleNamespace(), "pay with card-1234 today"
    )
    assert text == "pay with [card] today"


def test_after_tool_callback_normalizes_every_accepted_container(
    project: ProjectBuilder,
) -> None:
    """UserDict and deque walk like dict and list; redaction sees them all."""
    from collections import UserDict, deque

    agent = build(
        resolved_path(project, "mail-triage", {"tools": [{"builtin": "google_search"}]})
    )
    assert agent.after_tool_callback is not None
    mapped = agent.after_tool_callback(  # type: ignore[call-arg, operator]
        SimpleNamespace(), {}, SimpleNamespace(), UserDict({"bank_name": "B"})
    )
    assert mapped == {"bank_name": "[redacted]"}
    queued = agent.after_tool_callback(  # type: ignore[call-arg, operator]
        SimpleNamespace(), {}, SimpleNamespace(), deque(["card-1 paid"])
    )
    assert queued == ["[card] paid"]


def test_after_tool_callback_rejects_what_it_cannot_walk(
    project: ProjectBuilder,
) -> None:
    """A result that redaction cannot see through must not reach the model."""
    agent = build(
        resolved_path(project, "mail-triage", {"tools": [{"builtin": "google_search"}]})
    )
    assert agent.after_tool_callback is not None
    for opaque in (b"card-1", object()):
        with pytest.raises(GeteError, match="redact"):
            agent.after_tool_callback(  # type: ignore[call-arg, operator]
                SimpleNamespace(), {}, SimpleNamespace(), opaque
            )


def test_a_raising_tools_exception_text_never_reaches_the_model(
    project: ProjectBuilder,
) -> None:
    """The text may hold anything the tool touched; the model gets a stand-in."""
    agent = build(resolved_path(project, "mail-triage", {}))
    assert agent.on_tool_error_callback is not None
    result = agent.on_tool_error_callback(  # type: ignore[call-arg, operator]
        SimpleNamespace(), {}, SimpleNamespace(), ValueError("secret-token-xyz")
    )
    assert result is not None
    assert "secret-token-xyz" not in str(result)
    assert "ValueError" in str(result)


def test_getes_own_errors_keep_their_message_for_the_model(
    project: ProjectBuilder,
) -> None:
    """Reauthorization prompts and host refusals are written to be shown."""
    agent = build(resolved_path(project, "mail-triage", {}))
    assert agent.on_tool_error_callback is not None
    result = agent.on_tool_error_callback(  # type: ignore[call-arg, operator]
        SimpleNamespace(), {}, SimpleNamespace(), GeteError("authorize again please")
    )
    assert result == {"error": "authorize again please"}


def test_after_tool_callback_leaves_results_alone_when_no_rule_applies(
    project: ProjectBuilder,
) -> None:
    """Returning None tells ADK to keep the tool's own result."""
    agent = build(resolved_path(project, "mail-triage", {}))
    assert agent.after_tool_callback is not None
    assert (
        agent.after_tool_callback(SimpleNamespace(), {}, SimpleNamespace(), {"x": 1})
        is None
    )  # type: ignore[call-arg, operator]
