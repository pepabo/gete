"""Registering agents with Gemini Enterprise: authorizations, registrations, notices."""

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import FakeGcp, ProjectBuilder

from gete.connection import Connection, Registry
from gete.declaration import load_project
from gete.errors import DeclarationError
from gete.gcp import GcpError
from gete.register import (
    REDIRECT_URI,
    agent_body,
    authorization_body,
    authorization_uri,
    find_by_reasoning_engine,
    find_engine,
    in_use_by_another,
    needs_license,
    register_project,
    registration_matches,
)

CATALOG = Registry.from_catalog()
NUMBER = "123456789012"
GE = f"projects/{NUMBER}/locations/global"
ENGINE = f"projects/{NUMBER}/locations/us-central1/reasoningEngines/123"
AGENTS_URL = (
    f"https://discoveryengine.googleapis.com/v1alpha/{GE}/collections/default_collection"
    "/engines/app_1/assistants/default_assistant/agents"
)
AUTHS_URL = f"https://discoveryengine.googleapis.com/v1alpha/{GE}/authorizations"
ENGINES_URL = "https://us-central1-aiplatform.googleapis.com/v1/projects/example-project/locations/us-central1/reasoningEngines"
SECRETS = "https://secretmanager.googleapis.com/v1/projects/example-project/secrets"


def query(uri: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(uri).query)


# --- pure pieces


def test_authorization_uri_goes_to_the_connections_endpoint_with_fixed_redirect() -> (
    None
):
    uri = authorization_uri(CATALOG.get("github"), "client-1")
    assert uri.startswith("https://github.com/login/oauth/authorize?")
    params = query(uri)
    assert params["client_id"] == ["client-1"]
    assert params["redirect_uri"] == [REDIRECT_URI]
    assert params["response_type"] == ["code"]
    assert params["scope"] == ["repo"]


def test_authorization_uri_asks_for_a_refresh_token() -> None:
    """Without offline access the token dies after an hour and every call fails."""
    params = query(authorization_uri(CATALOG.get("github"), "c"))
    assert params["access_type"] == ["offline"]
    assert params["prompt"] == ["consent"]


def test_authorization_uri_joins_scopes_with_spaces() -> None:
    params = query(authorization_uri(CATALOG.get("google"), "c"))
    assert params["scope"] == [
        "https://www.googleapis.com/auth/gmail.readonly "
        "https://www.googleapis.com/auth/calendar.readonly"
    ]


def test_authorization_uri_uses_the_connections_scope_parameter() -> None:
    slack = CATALOG.get("slack", include_retired=True)
    params = query(authorization_uri(slack, "c"))
    assert "user_scope" in params
    assert "scope" not in params


def test_authorization_uri_copies_a_verbatim_query_when_declared() -> None:
    """freee works with response_type=code alone; Gemini Enterprise adds client_id."""
    uri = authorization_uri(CATALOG.get("freee"), "c")
    assert (
        uri
        == "https://accounts.secure.freee.co.jp/public_api/authorize?response_type=code"
    )


MENU: dict[str, Any] = {
    "id": "example",
    "display_name": "Example",
    "hosts": ["api.example.com"],
    "token_prefixes": ["ex_"],
    "oauth": {
        "authorization_url": "https://auth.example.com/authorize",
        "token_url": "https://auth.example.com/token",
        "scopes": {"read": "Read data"},
        "optional_scopes": {"write": "Change data", "admin": "Administer data"},
    },
}


def test_authorization_uri_appends_the_selection_after_the_defaults() -> None:
    entry = Connection.from_mapping(MENU)
    params = query(authorization_uri(entry, "c", ["write"]))
    assert params["scope"] == ["read write"]


def test_without_a_selection_the_defaults_stand_alone() -> None:
    entry = Connection.from_mapping(MENU)
    params = query(authorization_uri(entry, "c"))
    assert params["scope"] == ["read"]


def test_a_selection_outside_the_menu_is_refused() -> None:
    """register may run without validate; the menu is the reviewed ceiling."""
    entry = Connection.from_mapping(MENU)
    with pytest.raises(DeclarationError, match="optional_scopes"):
        authorization_uri(entry, "c", ["repo"])


def test_a_selection_next_to_a_verbatim_query_is_refused() -> None:
    """The query is used as written, so the selection would silently go missing."""
    entry = Connection.from_mapping(
        {
            **MENU,
            "oauth": {
                **MENU["oauth"],
                "authorization_query": {"response_type": "code"},
            },
        }
    )
    with pytest.raises(DeclarationError, match="authorization_query"):
        authorization_uri(entry, "c", ["write"])


def test_authorization_body_carries_the_selection_into_the_uri() -> None:
    body = authorization_body(
        GE, "finance-agent", Connection.from_mapping(MENU), "id-1", "s", ["write"]
    )
    params = query(body["serverSideOauth2"]["authorizationUri"])
    assert params["scope"] == ["read write"]


def test_authorization_body_is_named_per_agent_and_carries_the_client() -> None:
    body = authorization_body(
        GE, "finance-agent", CATALOG.get("freee"), "id-1", "secret-1"
    )
    assert body["name"] == f"{GE}/authorizations/finance-agent-freee"
    oauth = body["serverSideOauth2"]
    assert oauth["clientId"] == "id-1"
    assert oauth["clientSecret"] == "secret-1"
    assert oauth["tokenUri"] == "https://accounts.secure.freee.co.jp/public_api/token"


def test_authorization_without_the_installation_root_is_refused() -> None:
    """An unfilled root would be handed to users as the link they consent at."""
    rooted = Connection.from_mapping(
        {
            "id": "rooted-api",
            "display_name": "Rooted API",
            "oauth": {
                "authorization_url": "{base_url}/oauth/authorizations/new",
                "token_url": "{base_url}/oauth/tokens",
                "scopes": {},
            },
        }
    )
    with pytest.raises(DeclarationError, match="base_url"):
        authorization_body(GE, "finance-agent", rooted, "id-1", "secret-1")


def test_agent_body_points_at_the_engine_and_binds_authorizations() -> None:
    declaration: dict[str, Any] = {
        "display_name": "Finance",
        "description": "Checks expenses",
        "starter_prompts": ["Show me last month"],
    }
    body = agent_body(declaration, ENGINE, [f"{GE}/authorizations/finance-freee"])
    assert body["displayName"] == "Finance"
    assert (
        body["adkAgentDefinition"]["provisionedReasoningEngine"]["reasoningEngine"]
        == ENGINE
    )
    assert body["authorizationConfig"]["toolAuthorizations"] == [
        f"{GE}/authorizations/finance-freee"
    ]
    assert body["starterPrompts"] == [{"text": "Show me last month"}]


def test_agent_body_omits_an_empty_authorization_config() -> None:
    body = agent_body({"display_name": "A", "description": "B"}, ENGINE, [])
    assert "authorizationConfig" not in body
    assert "starterPrompts" not in body


def test_find_engine_uses_the_gete_agent_label_not_the_display_name() -> None:
    engines = [
        {"name": "e/1", "displayName": "Finance", "labels": {"gete-agent": "other"}},
        {
            "name": "e/2",
            "displayName": "Something else",
            "labels": {"gete-agent": "finance"},
        },
    ]
    assert find_engine(engines, "finance")["name"] == "e/2"


def test_find_engine_fails_when_missing_or_duplicated() -> None:
    with pytest.raises(GcpError, match="apply"):
        find_engine([], "finance")
    twice = [
        {"name": "e/1", "labels": {"gete-agent": "k"}},
        {"name": "e/2", "labels": {"gete-agent": "k"}},
    ]
    with pytest.raises(GcpError, match="2"):
        find_engine(twice, "k")


def test_find_by_reasoning_engine_ignores_display_names_and_other_definitions() -> None:
    agents = [
        {
            "name": "a/1",
            "displayName": "Renamed by hand",
            "adkAgentDefinition": {
                "provisionedReasoningEngine": {"reasoningEngine": ENGINE}
            },
        },
        {
            "name": "a/2",
            "displayName": "Finance",
            "adkAgentDefinition": {
                "provisionedReasoningEngine": {"reasoningEngine": "e/other"}
            },
        },
        {"name": "a/3", "displayName": "Finance", "a2aAgentDefinition": {}},
    ]
    assert [a["name"] for a in find_by_reasoning_engine(agents, ENGINE)] == ["a/1"]


def test_registration_matches_ignores_order_but_not_content() -> None:
    agent = {"authorizationConfig": {"toolAuthorizations": ["x", "y"]}}
    assert registration_matches(agent, ["y", "x"])
    assert not registration_matches(agent, ["x"])
    assert not registration_matches({}, ["x"])
    assert registration_matches({}, [])


def test_license_and_in_use_errors_are_told_apart() -> None:
    assert needs_license("Gemini Enterprise license is not available for the caller")
    assert not needs_license("PERMISSION_DENIED")
    assert in_use_by_another("authorization is used by another agent")
    assert not in_use_by_another("not found")


# --- the flow, against a fake GCP


def secret(value: str) -> dict[str, Any]:
    import base64

    return {"payload": {"data": base64.b64encode(value.encode()).decode()}}


@pytest.fixture
def gcp() -> FakeGcp:
    fake = FakeGcp()
    fake.route(
        "GET",
        ENGINES_URL,
        {"reasoningEngines": [{"name": ENGINE, "labels": {"gete-agent": "finance"}}]},
    )
    fake.route("GET", AUTHS_URL, {"authorizations": []})
    fake.route("GET", AGENTS_URL, {"agents": []})
    fake.route(
        "GET",
        f"{SECRETS}/ge-oauth-freee-client-id/versions/latest:access",
        secret("id-1"),
    )
    fake.route(
        "GET",
        f"{SECRETS}/ge-oauth-freee-client-secret/versions/latest:access",
        secret("secret-1"),
    )
    fake.route("POST", AUTHS_URL, {})
    fake.route("PATCH", f"{AUTHS_URL}/finance-freee", {})
    return fake


def project_with(project: ProjectBuilder, *agents: dict[str, Any]) -> Any:
    project.write_project(
        {
            "version": 1,
            "project": "example-project",
            "location": "us-central1",
            "gemini_enterprise": {"project_number": NUMBER},
        }
    )
    for agent in agents:
        project.write_agent(agent["name"], agent)
    return load_project(project.root / "gete.yaml")


FINANCE: dict[str, Any] = {
    "name": "finance",
    "display_name": "Finance",
    "description": "Checks expenses",
    "connections": ["freee"],
    "registration": {"gemini_enterprise": {"engine": "app_1"}},
}


def test_authorization_is_created_when_absent_and_a_notice_is_written(
    project: ProjectBuilder, gcp: FakeGcp, tmp_path: Path
) -> None:
    notice = tmp_path / "notice.md"
    summary = register_project(project_with(project, FINANCE), gcp, notice)
    posts = gcp.writes("POST")
    assert posts[0][0] == AUTHS_URL
    assert posts[0][1] == {"authorizationId": "finance-freee"}
    assert posts[0][2]["serverSideOauth2"]["clientId"] == "id-1"
    assert summary.failed == []
    assert summary.needs_human == ["finance"]
    text = notice.read_text()
    assert ENGINE in text
    assert "`finance`" in text
    assert "Skip" in text
    assert "<details>" in text


def test_the_registrar_builds_each_authorization_from_that_agents_selection(
    project: ProjectBuilder, gcp: FakeGcp, tmp_path: Path
) -> None:
    project.write_project(
        {
            "version": 1,
            "project": "example-project",
            "location": "us-central1",
            "gemini_enterprise": {"project_number": NUMBER},
            "connections": {"example": {k: v for k, v in MENU.items() if k != "id"}},
        }
    )
    project.write_agent(
        "finance",
        {
            **FINANCE,
            "connections": [{"id": "example", "scopes": ["write"]}],
        },
    )
    for suffix, value in (("client-id", "id-1"), ("client-secret", "secret-1")):
        gcp.route(
            "GET",
            f"{SECRETS}/ge-oauth-example-{suffix}/versions/latest:access",
            secret(value),
        )
    summary = register_project(
        load_project(project.root / "gete.yaml"), gcp, tmp_path / "n.md"
    )
    assert summary.failed == []
    posts = gcp.writes("POST")
    assert posts[0][1] == {"authorizationId": "finance-example"}
    params = query(posts[0][2]["serverSideOauth2"]["authorizationUri"])
    assert params["scope"] == ["read write"]


def test_the_registrar_refuses_a_connection_declared_in_both_forms(
    project: ProjectBuilder, gcp: FakeGcp, tmp_path: Path
) -> None:
    """The schema cannot see the two forms are one connection, register can run
    without validate, and one entry's selection would stand for both silently."""
    project.write_project(
        {
            "version": 1,
            "project": "example-project",
            "location": "us-central1",
            "gemini_enterprise": {"project_number": NUMBER},
            "connections": {"example": {k: v for k, v in MENU.items() if k != "id"}},
        }
    )
    project.write_agent(
        "finance",
        {
            **FINANCE,
            "connections": ["example", {"id": "example", "scopes": ["write"]}],
        },
    )
    summary = register_project(
        load_project(project.root / "gete.yaml"), gcp, tmp_path / "n.md"
    )
    assert summary.failed == ["finance"]
    assert any("twice" in message for message in summary.messages)
    assert gcp.writes("POST") == []
    assert gcp.writes("PATCH") == []


def test_existing_authorization_is_updated_not_recreated(
    project: ProjectBuilder, gcp: FakeGcp, tmp_path: Path
) -> None:
    gcp.route(
        "GET",
        AUTHS_URL,
        {"authorizations": [{"name": f"{GE}/authorizations/finance-freee"}]},
    )
    register_project(project_with(project, FINANCE), gcp, tmp_path / "n.md")
    assert gcp.writes("POST") == []
    patches = gcp.writes("PATCH")
    assert patches[0][0].endswith("/authorizations/finance-freee")
    assert patches[0][1] == {"updateMask": "serverSideOauth2"}


def test_matching_registration_is_left_alone_and_no_notice_is_written(
    project: ProjectBuilder, gcp: FakeGcp, tmp_path: Path
) -> None:
    gcp.route(
        "GET",
        AGENTS_URL,
        {
            "agents": [
                {
                    "name": "agents/7",
                    "displayName": "Whatever the human typed",
                    "adkAgentDefinition": {
                        "provisionedReasoningEngine": {"reasoningEngine": ENGINE}
                    },
                    "authorizationConfig": {
                        "toolAuthorizations": [f"{GE}/authorizations/finance-freee"]
                    },
                }
            ]
        },
    )
    summary = register_project(project_with(project, FINANCE), gcp, tmp_path / "n.md")
    assert [url for url, _, _ in gcp.writes("PATCH")] == []
    assert summary.needs_human == []
    assert not (tmp_path / "n.md").exists()


def test_registration_with_stale_authorizations_is_patched_in_place(
    project: ProjectBuilder, gcp: FakeGcp, tmp_path: Path
) -> None:
    """Recreating would change the agent id and cut every user's link."""
    gcp.route(
        "GET",
        AGENTS_URL,
        {
            "agents": [
                {
                    "name": "agents/7",
                    "adkAgentDefinition": {
                        "provisionedReasoningEngine": {"reasoningEngine": ENGINE}
                    },
                }
            ]
        },
    )
    gcp.route("PATCH", "https://discoveryengine.googleapis.com/v1alpha/agents/7", {})
    register_project(project_with(project, FINANCE), gcp, tmp_path / "n.md")
    patches = [p for p in gcp.writes("PATCH") if p[0].endswith("agents/7")]
    assert patches[0][1] == {"updateMask": "authorizationConfig"}
    assert patches[0][2]["authorizationConfig"]["toolAuthorizations"] == [
        f"{GE}/authorizations/finance-freee"
    ]


def test_license_refusal_on_update_leaves_a_notice_instead_of_failing(
    project: ProjectBuilder, gcp: FakeGcp, tmp_path: Path
) -> None:
    gcp.route(
        "GET",
        AGENTS_URL,
        {
            "agents": [
                {
                    "name": "agents/7",
                    "adkAgentDefinition": {
                        "provisionedReasoningEngine": {"reasoningEngine": ENGINE}
                    },
                }
            ]
        },
    )
    gcp.route(
        "PATCH",
        "https://discoveryengine.googleapis.com/v1alpha/agents/7",
        GcpError(403, "Gemini Enterprise license is not available"),
    )
    summary = register_project(project_with(project, FINANCE), gcp, tmp_path / "n.md")
    assert summary.failed == []
    assert "license" in (tmp_path / "n.md").read_text().lower()


def test_authorization_held_by_another_agent_is_explained_not_forced(
    project: ProjectBuilder, gcp: FakeGcp, tmp_path: Path
) -> None:
    gcp.route(
        "GET",
        AGENTS_URL,
        {
            "agents": [
                {
                    "name": "agents/7",
                    "adkAgentDefinition": {
                        "provisionedReasoningEngine": {"reasoningEngine": ENGINE}
                    },
                }
            ]
        },
    )
    gcp.route(
        "PATCH",
        "https://discoveryengine.googleapis.com/v1alpha/agents/7",
        GcpError(400, "authorization finance-freee is used by another agent"),
    )
    summary = register_project(project_with(project, FINANCE), gcp, tmp_path / "n.md")
    assert summary.failed == []
    text = (tmp_path / "n.md").read_text()
    assert "finance-freee" in text
    assert "delete" in text.lower()


def test_other_update_errors_fail_that_agent(
    project: ProjectBuilder, gcp: FakeGcp, tmp_path: Path
) -> None:
    gcp.route(
        "GET",
        AGENTS_URL,
        {
            "agents": [
                {
                    "name": "agents/7",
                    "adkAgentDefinition": {
                        "provisionedReasoningEngine": {"reasoningEngine": ENGINE}
                    },
                }
            ]
        },
    )
    gcp.route(
        "PATCH",
        "https://discoveryengine.googleapis.com/v1alpha/agents/7",
        GcpError(403, "PERMISSION_DENIED"),
    )
    summary = register_project(project_with(project, FINANCE), gcp, tmp_path / "n.md")
    assert summary.failed == ["finance"]


def test_duplicate_registrations_are_not_touched(
    project: ProjectBuilder, gcp: FakeGcp, tmp_path: Path
) -> None:
    agent = {
        "name": "agents/7",
        "adkAgentDefinition": {
            "provisionedReasoningEngine": {"reasoningEngine": ENGINE}
        },
    }
    gcp.route("GET", AGENTS_URL, {"agents": [agent, {**agent, "name": "agents/8"}]})
    summary = register_project(project_with(project, FINANCE), gcp, tmp_path / "n.md")
    assert gcp.writes("PATCH") == [] or all(
        not u.endswith(("agents/7", "agents/8")) for u, _, _ in gcp.writes("PATCH")
    )
    assert summary.needs_human == ["finance"]
    assert "2" in (tmp_path / "n.md").read_text()


def test_one_failing_agent_does_not_stop_the_others(
    project: ProjectBuilder, gcp: FakeGcp, tmp_path: Path
) -> None:
    broken = {
        **FINANCE,
        "name": "broken",
        "connections": ["github"],
    }  # no github secrets routed
    summary = register_project(
        project_with(project, broken, FINANCE), gcp, tmp_path / "n.md"
    )
    assert summary.failed == ["broken"]
    assert summary.needs_human == ["finance"]


def test_agents_without_registration_are_skipped(
    project: ProjectBuilder, gcp: FakeGcp, tmp_path: Path
) -> None:
    plain = {"name": "plain", "display_name": "P", "description": "D"}
    summary = register_project(project_with(project, plain), gcp, tmp_path / "n.md")
    assert summary.skipped == ["plain"]
    assert gcp.calls == []


def test_notices_are_appended_not_overwritten(
    project: ProjectBuilder, gcp: FakeGcp, tmp_path: Path
) -> None:
    """Three new agents in one release must leave three notices."""
    notice = tmp_path / "n.md"
    notice.write_text("### earlier\n")
    register_project(project_with(project, FINANCE), gcp, notice)
    text = notice.read_text()
    assert text.startswith("### earlier\n")
    assert "Finance" in text


def test_project_number_is_looked_up_when_not_declared(
    project: ProjectBuilder, gcp: FakeGcp, tmp_path: Path
) -> None:
    project.write_project(
        {"version": 1, "project": "example-project", "location": "us-central1"}
    )
    project.write_agent("finance", FINANCE)
    gcp.route(
        "GET",
        "https://cloudresourcemanager.googleapis.com/v1/projects/example-project",
        {"projectNumber": NUMBER},
    )
    summary = register_project(
        load_project(project.root / "gete.yaml"), gcp, tmp_path / "n.md"
    )
    assert summary.failed == []
    assert any(url == AUTHS_URL for url, _, _ in gcp.writes("POST"))


def test_notice_template_can_be_replaced(
    project: ProjectBuilder, gcp: FakeGcp, tmp_path: Path
) -> None:
    template = project.root / "notice.md.tmpl"
    template.write_text("CUSTOM {display_name} -> {reasoning_engine}\n")
    project.write_project(
        {
            "version": 1,
            "project": "example-project",
            "location": "us-central1",
            "gemini_enterprise": {"project_number": NUMBER},
            "registration": {"notice_template": "./notice.md.tmpl"},
        }
    )
    project.write_agent("finance", FINANCE)
    register_project(load_project(project.root / "gete.yaml"), gcp, tmp_path / "n.md")
    assert (tmp_path / "n.md").read_text().startswith("CUSTOM Finance -> " + ENGINE)


def test_curl_in_the_notice_binds_exactly_the_declared_authorizations(
    project: ProjectBuilder, gcp: FakeGcp, tmp_path: Path
) -> None:
    register_project(project_with(project, FINANCE), gcp, tmp_path / "n.md")
    text = (tmp_path / "n.md").read_text()
    assert (
        json.dumps(
            {
                "authorizationConfig": {
                    "toolAuthorizations": [f"{GE}/authorizations/finance-freee"]
                }
            }
        )
        in text
    )


def test_a_name_that_matches_no_agent_is_an_error(
    project: ProjectBuilder, gcp: FakeGcp, tmp_path: Path
) -> None:
    """Silence would let a typo in CD look like a successful registration."""
    with pytest.raises(DeclarationError, match="financee"):
        register_project(
            project_with(project, FINANCE), gcp, tmp_path / "n.md", ["financee"]
        )


def test_a_skipped_agent_says_why(
    project: ProjectBuilder, gcp: FakeGcp, tmp_path: Path
) -> None:
    plain = {"name": "plain", "display_name": "P", "description": "D"}
    summary = register_project(project_with(project, plain), gcp, tmp_path / "n.md")
    assert any("plain" in line and "engine" in line for line in summary.messages)


def test_the_console_link_points_at_the_declared_location(
    project: ProjectBuilder, gcp: FakeGcp, tmp_path: Path
) -> None:
    """The parent uses gemini_enterprise.location, so the link has to as well."""
    project.write_project(
        {
            "version": 1,
            "project": "example-project",
            "location": "us-central1",
            "gemini_enterprise": {"project_number": NUMBER, "location": "eu"},
        }
    )
    project.write_agent("finance", FINANCE)
    gcp.route(
        "GET",
        f"https://discoveryengine.googleapis.com/v1alpha/projects/{NUMBER}/locations/eu"
        "/collections/default_collection/engines/app_1/assistants/default_assistant"
        "/agents",
        {"agents": []},
    )
    gcp.route(
        "GET",
        f"https://discoveryengine.googleapis.com/v1alpha/projects/{NUMBER}"
        "/locations/eu/authorizations",
        {"authorizations": []},
    )
    gcp.route(
        "POST",
        f"https://discoveryengine.googleapis.com/v1alpha/projects/{NUMBER}"
        "/locations/eu/authorizations",
        {},
    )
    register_project(load_project(project.root / "gete.yaml"), gcp, tmp_path / "n.md")
    assert "locations/eu/engines/app_1" in (tmp_path / "n.md").read_text()


def test_a_notice_template_with_an_unknown_placeholder_is_reported(
    project: ProjectBuilder, gcp: FakeGcp, tmp_path: Path
) -> None:
    template = project.root / "notice.md.tmpl"
    template.write_text("CUSTOM {display_nmae}\n")
    project.write_project(
        {
            "version": 1,
            "project": "example-project",
            "location": "us-central1",
            "gemini_enterprise": {"project_number": NUMBER},
            "registration": {"notice_template": "./notice.md.tmpl"},
        }
    )
    project.write_agent("finance", FINANCE)
    summary = register_project(
        load_project(project.root / "gete.yaml"), gcp, tmp_path / "n.md"
    )
    assert summary.failed == ["finance"]
    assert any("display_nmae" in line for line in summary.messages)


def test_pkce_is_carried_to_the_authorization_when_the_connection_asks() -> None:
    """Some authorization servers refuse a code exchange without a verifier."""
    body = authorization_body(GE, "finance-agent", _with_pkce(True), "id-1", "s")
    assert body["serverSideOauth2"]["pkceVerificationEnabled"] is True


def test_without_pkce_the_authorization_says_nothing_about_it() -> None:
    """serverSideOauth2 is replaced whole on update, so absent is off."""
    body = authorization_body(GE, "finance-agent", _with_pkce(False), "id-1", "s")
    assert "pkceVerificationEnabled" not in body["serverSideOauth2"]


def _with_pkce(enabled: bool) -> Connection:
    oauth: dict[str, Any] = {
        "authorization_url": "https://auth.example.com/authorize",
        "token_url": "https://auth.example.com/token",
        "scopes": {},
    }
    if enabled:
        oauth["pkce"] = True
    return Connection.from_mapping(
        {"id": "example", "display_name": "Example", "oauth": oauth}
    )
