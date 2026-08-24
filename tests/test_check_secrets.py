"""validate --check-secrets: the secrets the deployment reads have a version."""

from typing import Any

from conftest import FakeGcp, ProjectBuilder

from gete.declaration import load_project
from gete.gcp import GcpError
from gete.secrets import check_secrets

SECRETS = "https://secretmanager.googleapis.com/v1/projects/example-project/secrets"


def versions(name: str) -> str:
    return f"{SECRETS}/{name}/versions"


def load(project: ProjectBuilder, *agents: dict[str, Any]) -> Any:
    project.write_project(
        {"version": 1, "project": "example-project", "location": "us-central1"}
    )
    for agent in agents:
        project.write_agent(agent["name"], agent)
    return load_project(project.root / "gete.yaml")


def test_enabled_version_passes(project: ProjectBuilder) -> None:
    gcp = FakeGcp()
    gcp.route("GET", versions("token"), {"versions": [{"state": "ENABLED"}]})
    loaded = load(
        project,
        {"name": "a", "runtime": {"agent_engine": {"secret_env": {"TOKEN": "token"}}}},
    )
    assert check_secrets(loaded, gcp) == []


def test_missing_secret_is_reported(project: ProjectBuilder) -> None:
    gcp = FakeGcp()
    gcp.route("GET", versions("token"), GcpError(404, "Secret [token] not found"))
    loaded = load(
        project,
        {"name": "a", "runtime": {"agent_engine": {"secret_env": {"TOKEN": "token"}}}},
    )
    problems = [str(p) for p in check_secrets(loaded, gcp)]
    assert any("token" in p and "not found" in p for p in problems)


def test_secret_without_an_enabled_version_is_reported(project: ProjectBuilder) -> None:
    """Agent Engine says 'could not access one or more secrets'; it looks like IAM."""
    gcp = FakeGcp()
    gcp.route("GET", versions("token"), {"versions": [{"state": "DESTROYED"}]})
    gcp.route("GET", versions("empty"), {})
    loaded = load(
        project,
        {
            "name": "a",
            "runtime": {"agent_engine": {"secret_env": {"T": "token", "E": "empty"}}},
        },
    )
    problems = [str(p) for p in check_secrets(loaded, gcp)]
    assert any("token" in p and "version" in p for p in problems)
    assert any("empty" in p and "version" in p for p in problems)


def test_oauth_client_secrets_are_checked_for_registered_agents(
    project: ProjectBuilder,
) -> None:
    gcp = FakeGcp()
    gcp.route(
        "GET",
        versions("ge-oauth-freee-client-id"),
        {"versions": [{"state": "ENABLED"}]},
    )
    gcp.route("GET", versions("ge-oauth-freee-client-secret"), {})
    loaded = load(
        project,
        {
            "name": "finance",
            "connections": ["freee"],
            "registration": {"gemini_enterprise": {"engine": "app_1"}},
        },
    )
    problems = [str(p) for p in check_secrets(loaded, gcp)]
    assert len(problems) == 1
    assert "ge-oauth-freee-client-secret" in problems[0]


def test_unregistered_agents_do_not_need_oauth_clients(project: ProjectBuilder) -> None:
    gcp = FakeGcp()
    loaded = load(project, {"name": "local-only", "connections": ["freee"]})
    assert check_secrets(loaded, gcp) == []
    assert gcp.calls == []


def test_each_secret_is_checked_once(project: ProjectBuilder) -> None:
    gcp = FakeGcp()
    gcp.route("GET", versions("shared"), {"versions": [{"state": "ENABLED"}]})
    loaded = load(
        project,
        {"name": "a", "runtime": {"agent_engine": {"secret_env": {"X": "shared"}}}},
        {"name": "b", "runtime": {"agent_engine": {"secret_env": {"Y": "shared"}}}},
    )
    assert check_secrets(loaded, gcp) == []
    assert len(gcp.calls) == 1


def test_an_enabled_version_on_a_later_page_is_found(project: ProjectBuilder) -> None:
    """Secret Manager pages versions; the enabled one is not always on the first."""
    gcp = FakeGcp()
    pages = iter(
        [
            {"versions": [{"state": "DESTROYED"}], "nextPageToken": "p2"},
            {"versions": [{"state": "ENABLED"}]},
        ]
    )
    gcp.route("GET", versions("token"), lambda body: next(pages))
    loaded = load(
        project,
        {"name": "a", "runtime": {"agent_engine": {"secret_env": {"TOKEN": "token"}}}},
    )
    assert check_secrets(loaded, gcp) == []
