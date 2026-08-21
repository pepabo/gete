"""Rules applied after the schemas: what the declarations promise each other."""

from pathlib import Path

import pytest
from conftest import ProjectBuilder

from gete.declaration import find_project_file, load_project
from gete.errors import DeclarationError
from gete.validate import validate_project


def problems(project: ProjectBuilder) -> list[str]:
    return [
        str(problem)
        for problem in validate_project(load_project(project.root / "gete.yaml"))
    ]


def test_a_well_formed_project_has_no_problems(project: ProjectBuilder) -> None:
    project.write_agent("mail-triage")
    assert problems(project) == []


def test_project_file_is_found_by_walking_up(project: ProjectBuilder) -> None:
    """The CLI runs from anywhere below the project root, like git does."""
    nested = project.write_agent("mail-triage") / "src" / "deep"
    nested.mkdir(parents=True)
    assert find_project_file(nested) == project.root / "gete.yaml"


def test_missing_project_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(DeclarationError, match="gete.yaml"):
        find_project_file(tmp_path)


def test_unknown_connection_is_reported(project: ProjectBuilder) -> None:
    project.write_agent("mail-triage", {"connections": ["salesforce"]})
    assert any("salesforce" in p for p in problems(project))


def test_retired_connection_is_reported_with_the_reason(
    project: ProjectBuilder,
) -> None:
    project.write_agent("mail-triage", {"connections": ["slack"]})
    assert any("connector" in p for p in problems(project))


def test_duplicate_connection_is_reported(project: ProjectBuilder) -> None:
    project.write_agent("mail-triage", {"connections": ["freee", "freee"]})
    assert any("connections" in p for p in problems(project))


def test_authorization_id_longer_than_63_is_reported(project: ProjectBuilder) -> None:
    """<name>-<connection> becomes an authorization id, which is a DNS-style label."""
    name = "a" * 58
    project.write_agent(name, {"connections": ["freee"]})
    assert any("63" in p for p in problems(project))


def test_name_must_match_the_directory(project: ProjectBuilder) -> None:
    """Terraform module, service account, and archive all derive from the directory."""
    project.write_agent("mail-triage", {"name": "mail-sorter"})
    assert any("directory" in p for p in problems(project))


def test_missing_instruction_file_is_reported(project: ProjectBuilder) -> None:
    project.write_agent("mail-triage", instruction=None)
    assert any("instruction.md" in p for p in problems(project))


def test_inline_instruction_needs_no_file(project: ProjectBuilder) -> None:
    project.write_agent(
        "mail-triage", {"instruction": "You sort mail.\nBe brief."}, instruction=None
    )
    assert problems(project) == []


def test_empty_env_value_is_reported(project: ProjectBuilder) -> None:
    """Agent Engine refuses empty values; finding out at apply time is too late."""
    project.write_agent(
        "mail-triage", {"runtime": {"agent_engine": {"env": {"COMPANY_ID": ""}}}}
    )
    assert any("COMPANY_ID" in p for p in problems(project))


@pytest.mark.parametrize("name", ["GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION"])
def test_reserved_env_names_are_reported(project: ProjectBuilder, name: str) -> None:
    """Agent Engine sets these itself and rejects a spec that repeats them."""
    project.write_agent(
        "mail-triage", {"runtime": {"agent_engine": {"env": {name: "x"}}}}
    )
    assert any(name in p for p in problems(project))


def test_missing_source_directory_is_reported(project: ProjectBuilder) -> None:
    project.write_agent("mail-triage", {"source": "./src"})
    assert any("src" in p for p in problems(project))


def test_python_ref_module_must_exist_below_source(project: ProjectBuilder) -> None:
    """Located, not imported: importing would need the deployed dependencies."""
    agent_dir = project.write_agent(
        "mail-triage",
        {"source": "./src", "tools": [{"python": "mail_triage.agent:TOOLS"}]},
    )
    (agent_dir / "src").mkdir()
    assert any("mail_triage.agent" in p for p in problems(project))
    (agent_dir / "src" / "mail_triage").mkdir()
    (agent_dir / "src" / "mail_triage" / "agent.py").write_text("TOOLS = []\n")
    assert problems(project) == []


def test_python_tool_without_source_is_reported(project: ProjectBuilder) -> None:
    project.write_agent(
        "mail-triage", {"tools": [{"python": "mail_triage.agent:TOOLS"}]}
    )
    assert any("source" in p for p in problems(project))


def test_unknown_builtin_tool_is_reported(project: ProjectBuilder) -> None:
    project.write_agent("mail-triage", {"tools": [{"builtin": "google_serach"}]})
    assert any("google_serach" in p for p in problems(project))


def test_known_builtin_tool_passes(project: ProjectBuilder) -> None:
    project.write_agent("mail-triage", {"tools": [{"builtin": "google_search"}]})
    assert problems(project) == []


def test_mcp_host_must_be_allowed_by_its_connection(project: ProjectBuilder) -> None:
    """Otherwise the token would be sent to a host the connection never declared."""
    project.write_agent(
        "mail-triage",
        {
            "connections": ["freee"],
            "tools": [
                {"mcp": {"url": "https://mcp.example.com/mcp", "connection": "freee"}}
            ],
        },
    )
    assert any("mcp.example.com" in p for p in problems(project))


def test_mcp_connection_must_be_declared_by_the_agent(project: ProjectBuilder) -> None:
    project.write_agent(
        "mail-triage",
        {
            "tools": [
                {"mcp": {"url": "https://api.freee.co.jp/mcp", "connection": "freee"}}
            ]
        },
    )
    assert any("freee" in p for p in problems(project))


def test_mcp_without_connection_may_point_anywhere_https(
    project: ProjectBuilder,
) -> None:
    project.write_agent(
        "mail-triage", {"tools": [{"mcp": {"url": "https://mcp.example.com/mcp"}}]}
    )
    assert problems(project) == []


def test_policy_files_must_exist_and_match_the_schema(project: ProjectBuilder) -> None:
    project.write_project(
        {
            "version": 1,
            "project": "example-project",
            "location": "us-central1",
            "policies": ["./policies/missing.yaml"],
        }
    )
    project.write_agent("mail-triage")
    assert any("missing.yaml" in p for p in problems(project))
    project.write_policies("bad", [{"name": "x", "when": "sometimes"}])
    project.write_project(
        {
            "version": 1,
            "project": "example-project",
            "location": "us-central1",
            "policies": ["./policies/bad.yaml"],
        }
    )
    assert any("when" in p for p in problems(project))


def test_connection_overrides_go_through_the_connection_checks(
    project: ProjectBuilder,
) -> None:
    project.write_project(
        {
            "version": 1,
            "project": "example-project",
            "location": "us-central1",
            "connections": {"github": {"hosts": ["googleapis.com"]}},
        }
    )
    project.write_agent("mail-triage")
    assert any("googleapis.com" in p for p in problems(project))


def test_agent_schema_errors_are_reported_with_their_file(
    project: ProjectBuilder,
) -> None:
    project.write_agent("mail-triage", {"modle": "typo"})
    assert any("agent.yaml" in p and "modle" in p for p in problems(project))


def test_agents_without_declaration_are_skipped(project: ProjectBuilder) -> None:
    """A stray directory under agents/ is not an agent."""
    (project.agents_dir / "notes").mkdir()
    project.write_agent("mail-triage")
    assert problems(project) == []


def test_duplicate_policy_names_are_reported(project: ProjectBuilder) -> None:
    project.write_policies("a", [{"name": "finance", "when": "always"}])
    project.write_policies("b", [{"name": "finance", "when": "always"}])
    project.write_project(
        {
            "version": 1,
            "project": "example-project",
            "location": "us-central1",
            "policies": ["./policies/a.yaml", "./policies/b.yaml"],
        }
    )
    project.write_agent("mail-triage")
    assert any("finance" in p for p in problems(project))
