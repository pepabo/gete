"""Building the ADK agent from a resolved declaration."""

import logging
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
from gete.runtime.callbacks import MISSING_TOOL_DESCRIPTION

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


async def test_a_raising_tools_exception_text_never_reaches_the_model(
    project: ProjectBuilder,
) -> None:
    """The text may hold anything the tool touched; the model gets a stand-in."""
    agent = build(resolved_path(project, "mail-triage", {}))
    assert agent.on_tool_error_callback is not None
    result = await agent.on_tool_error_callback(  # type: ignore[call-arg, operator]
        SimpleNamespace(), {}, SimpleNamespace(), ValueError("secret-token-xyz")
    )
    assert result is not None
    assert "secret-token-xyz" not in str(result)
    assert "ValueError" in str(result)


async def test_a_raising_tools_exception_text_stays_out_of_the_logs_too(
    project: ProjectBuilder, caplog: pytest.LogCaptureFixture
) -> None:
    """The log gets the type alone: no message, and no frames either."""

    def boom() -> None:
        raise ValueError("secret-token-xyz")

    caught: Exception
    try:
        boom()
    except ValueError as error:
        caught = error
    assert caught.__traceback__ is not None  # raised for real, not constructed
    agent = build(resolved_path(project, "mail-triage", {}))
    assert agent.on_tool_error_callback is not None
    with caplog.at_level(logging.WARNING):
        await agent.on_tool_error_callback(  # type: ignore[call-arg, operator]
            SimpleNamespace(), {}, SimpleNamespace(), caught
        )
    assert "ValueError" in caplog.text
    assert "secret-token-xyz" not in caplog.text
    assert "boom" not in caplog.text


# ADK's stand-in for a function call that names no tool: the callbacks run
# with it in place of a tool, and the error is ADK's own ValueError.
def missing_tool(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, description=MISSING_TOOL_DESCRIPTION)


def within(agent: Any) -> SimpleNamespace:
    """A tool context the way ADK builds one: the invocation names the agent."""
    return SimpleNamespace(_invocation_context=SimpleNamespace(agent=agent), state={})


def offered(agent: Any, *names: str) -> SimpleNamespace:
    """A tool context whose invocation carries the step's resolved tools.

    ADK fills this in before every model call and reads it back rather than
    listing the toolsets twice.
    """
    return SimpleNamespace(
        _invocation_context=SimpleNamespace(
            agent=agent,
            canonical_tools_cache=[SimpleNamespace(name=name) for name in names],
        ),
        state={},
    )


class Listing:
    """An agent that counts how often its toolsets are asked for their tools."""

    def __init__(self, *names: str) -> None:
        self.names = names
        self.listings = 0

    async def canonical_tools(self, ctx: Any = None) -> list[Any]:
        self.listings += 1
        return [SimpleNamespace(name=name) for name in self.names]


ADK_NOT_FOUND = ValueError(
    "Tool 'google_serach' not found.\nAvailable tools: google_search\n\n"
    "Possible causes:\n  1. LLM hallucinated the function name"
)


async def test_a_made_up_tool_name_is_answered_with_the_declared_names(
    project: ProjectBuilder,
) -> None:
    """A misspelling the model can correct, once it is told the names; the
    generic answer left it with nothing to retry."""
    agent = build(
        resolved_path(project, "mail-triage", {"tools": [{"builtin": "google_search"}]})
    )
    assert agent.on_tool_error_callback is not None
    result = await agent.on_tool_error_callback(  # type: ignore[call-arg, operator]
        missing_tool("google_serach"), {}, within(agent), ADK_NOT_FOUND
    )
    assert result == {
        "error": "no tool named google_serach; the tools are: google_search"
    }


async def test_nothing_of_adks_exception_reaches_the_model_but_the_name(
    project: ProjectBuilder,
) -> None:
    """ADK's message carries advice for the developer, not for the model."""
    agent = build(
        resolved_path(project, "mail-triage", {"tools": [{"builtin": "google_search"}]})
    )
    assert agent.on_tool_error_callback is not None
    result = await agent.on_tool_error_callback(  # type: ignore[call-arg, operator]
        missing_tool("google_serach"), {}, within(agent), ADK_NOT_FOUND
    )
    assert "Possible causes" not in str(result)
    assert "hallucinated" not in str(result)
    assert "ValueError" not in str(result)


async def test_a_made_up_tool_name_is_logged_with_the_declared_names(
    project: ProjectBuilder, caplog: pytest.LogCaptureFixture
) -> None:
    """The operator sees the same line: which name was called, which exist."""
    agent = build(
        resolved_path(project, "mail-triage", {"tools": [{"builtin": "google_search"}]})
    )
    assert agent.on_tool_error_callback is not None
    with caplog.at_level(logging.WARNING):
        await agent.on_tool_error_callback(  # type: ignore[call-arg, operator]
            missing_tool("google_serach"), {}, within(agent), ADK_NOT_FOUND
        )
    [record] = [r for r in caplog.records if r.name == "gete.runtime.callbacks"]
    assert record.levelno == logging.WARNING
    assert "google_serach" in record.getMessage()
    assert "google_search" in record.getMessage()
    assert "Possible causes" not in caplog.text


async def test_a_made_up_tool_name_is_still_named_when_the_tools_cannot_be_listed(
    project: ProjectBuilder,
) -> None:
    """Without a tool list the model at least learns the name was wrong."""
    agent = build(resolved_path(project, "mail-triage", {}))
    assert agent.on_tool_error_callback is not None
    result = await agent.on_tool_error_callback(  # type: ignore[call-arg, operator]
        missing_tool("google_serach"), {}, SimpleNamespace(), ADK_NOT_FOUND
    )
    assert result is not None
    assert "no tool named google_serach" in result["error"]
    assert "ValueError" not in result["error"]


async def test_a_declared_tool_that_raises_still_gets_the_generic_answer(
    project: ProjectBuilder,
) -> None:
    """A real tool's failure must not read as a wrong name: the name is
    right, and its message stays out of the answer as before."""
    agent = build(
        resolved_path(project, "mail-triage", {"tools": [{"builtin": "google_search"}]})
    )
    assert agent.on_tool_error_callback is not None
    result = await agent.on_tool_error_callback(  # type: ignore[call-arg, operator]
        SimpleNamespace(name="google_search", description="Searches"),
        {},
        within(agent),
        ValueError("secret-token-xyz"),
    )
    assert result == {
        "error": "the tool failed with ValueError; details are in the logs"
    }


async def test_a_call_to_a_tool_not_offered_this_turn_is_a_wrong_name_too(
    project: ProjectBuilder,
) -> None:
    """A name the agent does not offer now is answered with what it offers,
    whether or not ADK marked the stand-in."""
    agent = build(
        resolved_path(project, "mail-triage", {"tools": [{"builtin": "google_search"}]})
    )
    assert agent.on_tool_error_callback is not None
    result = await agent.on_tool_error_callback(  # type: ignore[call-arg, operator]
        SimpleNamespace(name="reauthorize_example", description=""),
        {},
        within(agent),
        ValueError("Tool 'reauthorize_example' not found."),
    )
    assert result == {
        "error": "no tool named reauthorize_example; the tools are: google_search"
    }


async def test_the_names_are_the_ones_the_model_was_offered_this_turn(
    project: ProjectBuilder,
) -> None:
    """Which tools a toolset offers is decided per turn and can have moved on
    by the time the error comes back; the model is told what it was given."""
    agent = build(
        resolved_path(project, "mail-triage", {"tools": [{"builtin": "google_search"}]})
    )
    assert agent.on_tool_error_callback is not None
    result = await agent.on_tool_error_callback(  # type: ignore[call-arg, operator]
        missing_tool("reauthorize_exampl"),
        {},
        offered(agent, "reauthorize_example"),
        ADK_NOT_FOUND,
    )
    assert result == {
        "error": "no tool named reauthorize_exampl; the tools are: reauthorize_example"
    }


async def test_naming_the_tools_does_not_list_the_toolsets_again(
    project: ProjectBuilder,
) -> None:
    """Listing reaches every MCP server over the network; an error is the
    worst moment to spend a round trip on what the turn already resolved."""
    agent = build(resolved_path(project, "mail-triage", {}))
    listing = Listing("google_search")
    assert agent.on_tool_error_callback is not None
    await agent.on_tool_error_callback(  # type: ignore[call-arg, operator]
        SimpleNamespace(name="google_search", description="Searches"),
        {},
        offered(listing, "google_search"),
        ValueError("boom"),
    )
    assert listing.listings == 0


async def test_the_toolsets_are_listed_when_the_turn_resolved_nothing(
    project: ProjectBuilder,
) -> None:
    """Without a resolved turn to read there is still the agent to ask."""
    agent = build(resolved_path(project, "mail-triage", {}))
    listing = Listing("google_search")
    assert agent.on_tool_error_callback is not None
    result = await agent.on_tool_error_callback(  # type: ignore[call-arg, operator]
        missing_tool("google_serach"), {}, within(listing), ADK_NOT_FOUND
    )
    assert listing.listings == 1
    assert result == {
        "error": "no tool named google_serach; the tools are: google_search"
    }


async def dispatched(agent: Any, called: str) -> Any:
    """What ADK answers a function call, run through its own flow.

    The unit tests above stand in for ADK - the tool it substitutes, the
    tools it resolved for the turn, the callback it awaits - so one test
    drives the real thing and fails if any of that moves.
    """
    from google.adk.agents.invocation_context import InvocationContext
    from google.adk.agents.run_config import RunConfig
    from google.adk.flows.llm_flows.functions import handle_function_call_list_async
    from google.adk.flows.llm_flows.single_flow import SingleFlow
    from google.adk.models.llm_request import LlmRequest
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    service = InMemorySessionService()
    session = await service.create_session(app_name="t", user_id="u", state={})
    invocation = InvocationContext(
        session_service=service,
        invocation_id="inv-1",
        agent=agent,
        session=session,
        run_config=RunConfig(),
    )
    request = LlmRequest()
    async for _ in SingleFlow()._preprocess_async(invocation, request):  # noqa: SLF001
        pass
    # The turn's resolved tools are what a wrong name is answered with, so
    # the callback is only right while ADK still leaves them here.
    assert invocation.canonical_tools_cache is not None
    event = await handle_function_call_list_async(
        invocation,
        [types.FunctionCall(id="call-1", name=called, args={})],
        request.tools_dict,
    )
    assert event is not None
    return event.content.parts[0].function_response.response


async def test_adk_answers_a_made_up_name_with_the_tools_it_resolved(
    project: ProjectBuilder,
) -> None:
    """The whole path, through ADK: the model calls a name no tool has and
    is told the names it can call instead."""
    path = resolved_path(
        project,
        "mail-triage",
        {"source": "./src", "tools": [{"python": "mail_triage.tools:TOOLS"}]},
    )
    write_tools_module(path.parent)
    assert await dispatched(build(path), "lookupp") == {
        "error": "no tool named lookupp; the tools are: lookup, transfer"
    }


async def test_an_arbitrary_gete_error_is_generic_like_any_other(
    project: ProjectBuilder,
) -> None:
    """Tool code can import and raise GeteError with any text it likes."""
    agent = build(resolved_path(project, "mail-triage", {}))
    assert agent.on_tool_error_callback is not None
    result = await agent.on_tool_error_callback(  # type: ignore[call-arg, operator]
        SimpleNamespace(), {}, SimpleNamespace(), GeteError("secret-in-gete-error")
    )
    assert result is not None
    assert "secret-in-gete-error" not in str(result)


async def test_only_the_dedicated_user_safe_error_keeps_its_message(
    project: ProjectBuilder,
) -> None:
    """Raising UserFacingError is the declaration that the text may be shown."""
    from gete.connection.client import ReauthorizationRequired
    from gete.errors import UserFacingError

    agent = build(resolved_path(project, "mail-triage", {}))
    assert agent.on_tool_error_callback is not None
    told = await agent.on_tool_error_callback(  # type: ignore[call-arg, operator]
        SimpleNamespace(), {}, SimpleNamespace(), UserFacingError("shown as written")
    )
    assert told == {"error": "shown as written"}
    prompted = await agent.on_tool_error_callback(  # type: ignore[call-arg, operator]
        SimpleNamespace(),
        {},
        SimpleNamespace(),
        ReauthorizationRequired("authorize again please"),
    )
    assert prompted == {"error": "authorize again please"}


def test_a_declared_shared_credential_puts_its_tools_on_the_agent(
    project: ProjectBuilder,
) -> None:
    agent = build(
        resolved_path(project, "mail-triage", {"shared_credentials": ["slack_post"]})
    )
    names = [getattr(tool, "__name__", None) for tool in agent.tools]
    assert names == ["read_linked_message", "preview_slack_post", "post_slack_message"]


def test_without_the_declaration_nothing_shared_is_added(
    project: ProjectBuilder,
) -> None:
    agent = build(resolved_path(project, "mail-triage", {}))
    assert list(agent.tools) == []
    assert "shared Slack credential" not in str(agent.instruction)


def test_the_credentials_rules_sit_between_the_policies_and_the_agents_text(
    project: ProjectBuilder,
) -> None:
    """The agent's own text is the easiest to override; the rules out-rank it.

    Declaring the credential also counts as having write tools, so the
    write policy's prefix is here without a tools entry.
    """
    agent = build(
        resolved_path(project, "mail-triage", {"shared_credentials": ["slack_post"]})
    )
    text = str(agent.instruction)
    assert (
        text.index("Never approve.")
        < text.index("Show before you write.")
        < text.index("shared Slack credential")
        < text.index("You sort mail.")
    )


async def test_confirmation_adk_marks_the_shared_write_tool_only(
    project: ProjectBuilder,
) -> None:
    path = resolved_path(project, "mail-triage", {"shared_credentials": ["slack_post"]})
    document = yaml.safe_load(path.read_text())
    document["resolved"]["confirmation"] = "adk"
    document["resolved"]["policies"].append(
        {
            "name": "confirm-writes",
            "when": "has_write_tools",
            "require_confirmation": "write_tools",
        }
    )
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    read_linked, preview, post = build(path).tools
    assert await post.check_require_confirmation({}, SimpleNamespace(state={})) is True
    assert callable(preview) and not hasattr(preview, "check_require_confirmation")
    assert callable(read_linked)


def test_a_policy_can_deny_a_shared_tool(project: ProjectBuilder) -> None:
    path = resolved_path(project, "mail-triage", {"shared_credentials": ["slack_post"]})
    document = yaml.safe_load(path.read_text())
    document["resolved"]["policies"].append(
        {"name": "no-posting", "when": "always", "deny_tools": ["post_slack_message"]}
    )
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    names = [getattr(tool, "__name__", None) for tool in build(path).tools]
    assert names == ["read_linked_message", "preview_slack_post"]


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
