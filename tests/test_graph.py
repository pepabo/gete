"""gete graph: Mermaid drawn from the declarations, so no second diagram goes stale."""

from typing import Any

import pytest
from click.testing import CliRunner
from conftest import ProjectBuilder

from gete.cli import main
from gete.declaration import load_project
from gete.graph import mermaid

FINANCE: dict[str, Any] = {
    "connections": ["freee", "google"],
    "source": "./src",
    "tools": [
        {"mcp": {"url": "https://api.freee.co.jp/mcp", "connection": "freee"}},
        {"builtin": "google_search"},
        {"python": "finance.agent:TOOLS"},
    ],
    "registration": {"gemini_enterprise": {"engine": "app_1"}},
}


def graph(project: ProjectBuilder, names: list[str] | None = None) -> str:
    return mermaid(load_project(project.root / "gete.yaml"), names)


def test_graph_shows_the_engine_the_agent_and_what_it_touches(
    project: ProjectBuilder,
) -> None:
    project.write_agent("finance", FINANCE)
    (project.agents_dir / "finance" / "src").mkdir()
    text = graph(project)
    assert text.startswith("flowchart LR")
    assert 'GE_app_1["Gemini Enterprise<br/>app_1"]' in text
    assert "GE_app_1 --> finance" in text
    assert 'finance["finance"]' in text
    assert "google_search" in text
    assert "api.freee.co.jp" in text
    assert "finance.agent:TOOLS" in text
    assert "-. freee .->" in text
    assert "-. google .->" in text


def test_unregistered_agents_hang_from_no_engine(project: ProjectBuilder) -> None:
    project.write_agent("local-only")
    text = graph(project)
    assert "local-only" in text
    assert "Gemini Enterprise" not in text


def test_graph_can_be_limited_to_named_agents(project: ProjectBuilder) -> None:
    project.write_agent("a")
    project.write_agent("b")
    text = graph(project, ["b"])
    assert 'b["b"]' in text
    assert 'a["a"]' not in text


def test_node_ids_are_safe_mermaid_identifiers(project: ProjectBuilder) -> None:
    project.write_agent(
        "mail-triage", {"tools": [{"mcp": {"url": "https://mcp.example.com/v1/mcp"}}]}
    )
    text = graph(project)
    assert 'mail_triage["mail-triage"]' in text
    assert "mcp.example.com" in text
    # every node id is an identifier: no dots, slashes, colons, or hyphens
    for line in text.splitlines()[1:]:
        head = line.strip().split("[", 1)[0].split(" ", 1)[0]
        assert head.replace("_", "").isalnum(), line


@pytest.mark.usefixtures("below_project")
def test_cli_prints_mermaid(project: ProjectBuilder) -> None:
    project.write_agent("finance", FINANCE)
    (project.agents_dir / "finance" / "src").mkdir()
    result = CliRunner().invoke(main, ["graph", "finance"])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("flowchart LR")


def test_free_text_in_a_label_cannot_break_the_diagram(project: ProjectBuilder) -> None:
    """A display name is prose; a quote in it would end the label early."""
    project.write_project(
        {
            "version": 1,
            "project": "example-project",
            "location": "us-central1",
            "connections": {
                "internal": {
                    "display_name": 'The "internal" API',
                    "hosts": ["api.internal.example.com"],
                    "oauth": {
                        "authorization_url": "https://auth.internal.example.com/a",
                        "token_url": "https://auth.internal.example.com/t",
                        "scopes": {},
                    },
                }
            },
        }
    )
    project.write_agent("finance", {"connections": ["internal"]})
    text = graph(project)
    assert '"The "internal" API"' not in text
    assert "#quot;internal#quot;" in text


def test_shared_credential_tools_appear(project: ProjectBuilder) -> None:
    project.write_agent("poster", {"shared_credentials": ["slack_post"]})
    text = graph(project)
    assert "slack_post" in text
    assert "bot" in text


def test_openapi_tools_appear_with_their_connection(project: ProjectBuilder) -> None:
    project.write_agent(
        "desk",
        {
            "connections": ["freee"],
            "tools": [
                {
                    "openapi": {
                        "spec": "./specs/service.yaml",
                        "connection": "freee",
                        "operations": ["ListThings", "ShowThing"],
                        "effect": "read",
                    }
                }
            ],
        },
    )
    text = graph(project)
    assert 'desk --> desk_tool_0[("openapi<br/>2 operations")]' in text
    assert "desk -. freee .-> desk_tool_0" in text


GITHUB_APP_CONNECTION: dict[str, Any] = {
    "github-app": {
        "app": {
            "app_id": "123",
            "private_key_secret": "ge-github-app-private-key",
            "repositories": ["example-org/requests"],
            "permissions": {"issues": "read"},
        }
    }
}


def test_an_app_connection_is_drawn_as_the_bot_it_acts_as(
    project: ProjectBuilder,
) -> None:
    project.write_project(
        {
            "version": 1,
            "project": "example-project",
            "location": "us-central1",
            "connections": GITHUB_APP_CONNECTION,
        }
    )
    project.write_agent(
        "triage",
        {
            "connections": ["github-app"],
            "tools": [
                {
                    "openapi": {
                        "spec": "./specs/github.yaml",
                        "connection": "github-app",
                        "operations": ["GetIssue"],
                        "effect": "read",
                    }
                }
            ],
        },
    )
    text = graph(project)
    assert "triage -. github-app (bot) .-> triage_tool_0" in text


def test_an_app_connection_no_tool_uses_is_still_drawn_as_a_bot(
    project: ProjectBuilder,
) -> None:
    project.write_project(
        {
            "version": 1,
            "project": "example-project",
            "location": "us-central1",
            "connections": GITHUB_APP_CONNECTION,
        }
    )
    project.write_agent("triage", {"connections": ["github-app"]})
    text = graph(project)
    assert '[("GitHub App (bot)")]' in text


def test_a_user_authorized_connection_is_not_drawn_as_a_bot(
    project: ProjectBuilder,
) -> None:
    project.write_agent("finance", {"connections": ["freee"]})
    assert "bot" not in graph(project)


def test_the_engine_is_drawn_by_its_name_when_gete_yaml_names_it(
    project: ProjectBuilder,
) -> None:
    """The id is what naming the engines takes out of sight; the diagram
    follows suit, and the node is a plain identifier again."""
    project.write_project(
        {
            "version": 1,
            "project": "example-project",
            "location": "us-central1",
            "gemini_enterprise": {"engines": {"sales": "my-sales-app_1234567890"}},
        }
    )
    project.write_agent(
        "finance", {"registration": {"gemini_enterprise": {"engine": "sales"}}}
    )
    text = graph(project)
    assert 'GE_sales["Gemini Enterprise<br/>sales"]' in text
    assert "GE_sales --> finance" in text
    assert "my-sales-app_1234567890" not in text
