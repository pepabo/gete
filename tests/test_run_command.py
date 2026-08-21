"""gete run: a local conversation that hands tokens to tools like Agent Engine does."""

from typing import Any

import pytest
from conftest import ProjectBuilder

from gete.declaration import load_project
from gete.errors import DeclarationError
from gete.request_context import clear_tool_call, current_tool_call
from gete.run import build_local_agent, initial_state


def teardown_function() -> None:
    clear_tool_call()


def test_tokens_come_from_the_environment_under_the_agents_authorization_names() -> (
    None
):
    """Locally the runtime reads the state exactly as it will on Agent Engine."""
    state = initial_state(
        "finance",
        ["freee", "google"],
        {
            "GETE_TOKEN_FREEE": "a1b2c3",
            "GETE_TOKEN_GOOGLE": "ya29.x",
            "OTHER": "ignored",
        },
    )
    assert state == {"finance-freee": "a1b2c3", "finance-google": "ya29.x"}


def test_hyphens_in_connection_ids_become_underscores_in_the_variable_name() -> None:
    assert initial_state("a", ["internal-api"], {"GETE_TOKEN_INTERNAL_API": "t"}) == {
        "a-internal-api": "t"
    }


def test_missing_tokens_are_left_out_not_faked() -> None:
    assert initial_state("a", ["freee"], {}) == {}


def test_local_agent_is_built_from_the_declaration_without_writing_files(
    project: ProjectBuilder,
) -> None:
    project.write_policies(
        "p", [{"name": "p", "when": "always", "instruction_prefix": "Never approve."}]
    )
    project.write_project(
        {
            "version": 1,
            "project": "example-project",
            "location": "us-central1",
            "policies": ["./policies/p.yaml"],
        }
    )
    directory = project.write_agent("finance", {"connections": ["freee"]})
    loaded = load_project(project.root / "gete.yaml")
    agent = build_local_agent(loaded, "finance")
    assert agent.name == "finance"
    assert str(agent.instruction).startswith("Never approve.")
    assert not (directory / "agent.resolved.yaml").exists()
    context: Any = type("Ctx", (), {"state": {}, "user_id": "u"})()
    assert agent.before_tool_callback is not None
    agent.before_tool_callback(None, {}, context)  # type: ignore[call-arg, operator]
    call = current_tool_call()
    assert call is not None and call.authorizations == {"freee": "finance-freee"}


def test_unknown_agent_name_is_an_error(project: ProjectBuilder) -> None:
    project.write_agent("finance")
    with pytest.raises(DeclarationError, match="nope"):
        build_local_agent(load_project(project.root / "gete.yaml"), "nope")
