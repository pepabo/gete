"""gete graph: Mermaid drawn from the declarations, so no second diagram goes stale."""

from typing import Any

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


def test_cli_prints_mermaid(project: ProjectBuilder) -> None:
    project.write_agent("finance", FINANCE)
    (project.agents_dir / "finance" / "src").mkdir()
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=project.root):
        result = runner.invoke(main, ["graph", "finance"])
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
