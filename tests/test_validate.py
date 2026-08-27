"""Rules applied after the schemas: what the declarations promise each other."""

from pathlib import Path
from typing import Any

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


# A connection of one's own whose tokens carry no prefix, as README shows.
INTERNAL_API: dict[str, Any] = {
    "display_name": "Internal API",
    "hosts": ["api.internal.example.com"],
    "token_prefixes": [],
    "oauth": {
        "authorization_url": "https://auth.internal.example.com/authorize",
        "token_url": "https://auth.internal.example.com/token",
        "scopes": {"read": "Read internal data"},
    },
}


def write_internal_api(project: ProjectBuilder) -> None:
    project.write_project(
        {
            "version": 1,
            "project": "example-project",
            "location": "us-central1",
            "connections": {"internal-api": INTERNAL_API},
        }
    )


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


def test_empty_env_values_are_declared_knobs_not_errors(
    project: ProjectBuilder,
) -> None:
    """An empty value documents that the knob exists; delivery drops it unsent.

    Agent Engine refuses empty values, but the Terraform module already drops
    them, so the declaration may keep the knob visible.
    """
    project.write_agent(
        "mail-triage", {"runtime": {"agent_engine": {"env": {"COMPANY_ID": ""}}}}
    )
    assert problems(project) == []


def test_empty_secret_env_value_is_reported(project: ProjectBuilder) -> None:
    """Unlike env, nothing drops an empty secret name; it fails at apply."""
    project.write_agent(
        "mail-triage",
        {"runtime": {"agent_engine": {"secret_env": {"API_KEY": ""}}}},
    )
    assert any("API_KEY" in p for p in problems(project))


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


def test_missing_requirements_file_is_reported(project: ProjectBuilder) -> None:
    """archive reads it; without the rule the failure is a traceback at pack time."""
    project.write_agent("mail-triage", {"requirements": "./requirements.txt"})
    assert any("requirements.txt" in p for p in problems(project))


def test_existing_requirements_file_passes(project: ProjectBuilder) -> None:
    agent_dir = project.write_agent("mail-triage", {"requirements": "./req.txt"})
    (agent_dir / "req.txt").write_text("httpx>=0.28\n")
    assert problems(project) == []


@pytest.mark.parametrize("name", ["ab", "a" * 28])
def test_name_that_cannot_become_a_service_account_is_reported(
    name: str, project: ProjectBuilder
) -> None:
    """<name>-ae is the service account id, and GCP wants 6 to 30 characters."""
    project.write_agent(name)
    assert any("service account" in p for p in problems(project))


@pytest.mark.parametrize("name", ["abc", "a" * 27])
def test_names_at_the_edges_of_the_service_account_length_pass(
    name: str, project: ProjectBuilder
) -> None:
    project.write_agent(name)
    assert problems(project) == []


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


def test_a_prefixless_connection_of_ones_own_is_accepted(
    project: ProjectBuilder,
) -> None:
    """Declaring one does not depend on which prefixless connections gete ships."""
    write_internal_api(project)
    project.write_agent("mail-triage", {"connections": ["internal-api"]})
    assert problems(project) == []


def test_two_prefixless_connections_on_one_agent_are_reported(
    project: ProjectBuilder,
) -> None:
    """Either authorization's token would be accepted as the other's."""
    write_internal_api(project)
    project.write_agent("mail-triage", {"connections": ["freee", "internal-api"]})
    found = problems(project)
    assert any(
        "elimination" in p and "freee" in p and "internal-api" in p for p in found
    ), found


def test_prefixless_connections_on_separate_agents_are_accepted(
    project: ProjectBuilder,
) -> None:
    """An agent is handed its own connections' tokens and no others."""
    write_internal_api(project)
    project.write_agent("mail-triage", {"connections": ["freee"]})
    project.write_agent("partner-review", {"connections": ["internal-api"]})
    assert problems(project) == []


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


SHARED_PROJECT = {
    "version": 1,
    "project": "example-project",
    "location": "us-central1",
    "shared_credentials": {"slack_post": {"token_secret": "slack-bot-token"}},
}


def test_a_shared_credential_needs_the_projects_token_secret(
    project: ProjectBuilder,
) -> None:
    """Deployed without one, the tools would refuse every call at runtime."""
    project.write_agent("poster", {"shared_credentials": ["slack_post"]})
    assert any(
        "slack_post" in message and "token_secret" in message
        for message in problems(project)
    )


def test_a_configured_shared_credential_passes(project: ProjectBuilder) -> None:
    project.write_project(SHARED_PROJECT)
    project.write_agent("poster", {"shared_credentials": ["slack_post"]})
    assert problems(project) == []


@pytest.mark.parametrize("block", ["env", "secret_env"])
def test_the_agent_cannot_claim_the_credentials_variable_itself(
    project: ProjectBuilder, block: str
) -> None:
    """The variable is delivered from gete.yaml; an agent pointing it at a
    value of its own choosing would swap the credential unseen."""
    project.write_project(SHARED_PROJECT)
    project.write_agent(
        "poster",
        {
            "shared_credentials": ["slack_post"],
            "runtime": {"agent_engine": {block: {"SLACK_BOT_TOKEN": "elsewhere"}}},
        },
    )
    assert any("SLACK_BOT_TOKEN" in message for message in problems(project))


# A service whose root moves with the installation: the tenant sits in the
# subdomain, so the API, the authorization URL, and the token URL move together.
ROOTED_API: dict[str, Any] = {
    "display_name": "Rooted API",
    "hosts": [],
    "token_prefixes": ["rt_"],
    "oauth": {
        "authorization_url": "{base_url}/oauth/authorizations/new",
        "token_url": "{base_url}/oauth/tokens",
        "scopes": {"read": "Read data"},
    },
}


def write_rooted_api(project: ProjectBuilder, base_url: str | None = None) -> None:
    entry = {**ROOTED_API, **({"base_url": base_url} if base_url else {})}
    project.write_project(
        {
            "version": 1,
            "project": "example-project",
            "location": "us-central1",
            "connections": {"rooted-api": entry},
        }
    )


def test_a_connection_without_its_root_is_refused_where_an_agent_uses_it(
    project: ProjectBuilder,
) -> None:
    write_rooted_api(project)
    project.write_agent("mail-triage", {"connections": ["rooted-api"]})
    found = problems(project)
    assert any("base_url" in p for p in found), found


def test_the_root_is_only_missing_where_it_is_needed(project: ProjectBuilder) -> None:
    """A definition nobody uses is not yet a problem; hosts is empty by design."""
    write_rooted_api(project)
    project.write_agent("mail-triage")
    assert problems(project) == []


def test_a_connection_with_its_root_set_passes(project: ProjectBuilder) -> None:
    write_rooted_api(project, "https://acme.example.com")
    project.write_agent("mail-triage", {"connections": ["rooted-api"]})
    assert problems(project) == []


def test_an_mcp_url_under_the_root_is_checked_against_the_filled_in_hosts(
    project: ProjectBuilder,
) -> None:
    write_rooted_api(project, "https://acme.example.com")
    project.write_agent(
        "mail-triage",
        {
            "connections": ["rooted-api"],
            "tools": [
                {
                    "mcp": {
                        "url": "https://acme.example.com/mcp",
                        "connection": "rooted-api",
                    }
                }
            ],
        },
    )
    assert problems(project) == []
