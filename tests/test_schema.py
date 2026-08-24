"""Shape of the declarations. The JSON Schemas decide what a document may say."""

from pathlib import Path
from typing import Any

import pytest

from gete.declaration import read_yaml
from gete.errors import DeclarationError
from gete.schema import validate_document

GETE: dict[str, Any] = {
    "version": 1,
    "project": "example-project",
    "location": "us-central1",
}

AGENT: dict[str, Any] = {
    "name": "mail-triage-agent",
    "display_name": "Mail triage",
    "description": "Sorts pasted mail by urgency",
    "model": "gemini-2.5-flash",
    "instruction": "./instruction.md",
}

CONNECTION: dict[str, Any] = {
    "id": "example",
    "display_name": "Example",
    "hosts": ["api.example.com"],
    "oauth": {
        "authorization_url": "https://auth.example.com/authorize",
        "token_url": "https://auth.example.com/token",
        "scopes": {"read": "Read data"},
    },
}

POLICY: list[dict[str, Any]] = [
    {"name": "baseline", "when": "always", "instruction_prefix": "Be brief."}
]

MCP_TOOL: dict[str, Any] = {"mcp": {"url": "https://mcp.example.com/mcp"}}


@pytest.mark.parametrize(
    ("kind", "document"),
    [("gete", GETE), ("agent", AGENT), ("connection", CONNECTION), ("policy", POLICY)],
)
def test_minimal_documents_pass(kind: str, document: Any) -> None:
    validate_document(kind, document, source="doc.yaml")


@pytest.mark.parametrize(
    ("kind", "document"),
    [
        ("gete", {**GETE, "projcet": "typo"}),
        ("agent", {**AGENT, "instructions": "typo"}),
        ("connection", {**CONNECTION, "host": ["typo"]}),
        ("policy", [{**POLICY[0], "whem": "always"}]),
    ],
)
def test_unknown_keys_are_rejected_instead_of_ignored(kind: str, document: Any) -> None:
    """A misspelled key must not look like it took effect."""
    with pytest.raises(DeclarationError):
        validate_document(kind, document, source="doc.yaml")


def test_errors_name_the_source_and_the_path() -> None:
    with pytest.raises(
        DeclarationError, match=r"(?s)agent\.yaml.*runtime\.agent_engine\.min_instances"
    ):
        validate_document(
            "agent",
            {**AGENT, "runtime": {"agent_engine": {"min_instances": -1}}},
            source=Path("agents/x/agent.yaml"),
        )


def test_gete_location_global_is_rejected() -> None:
    """Agent Engine has no global region; the API would reject it much later."""
    with pytest.raises(DeclarationError, match="location"):
        validate_document("gete", {**GETE, "location": "global"}, source="gete.yaml")


@pytest.mark.parametrize(
    "location", ["us central1", "US-CENTRAL1", "-us-central1", "evil.com", "us-"]
)
def test_gete_location_must_look_like_a_region(location: str) -> None:
    """It is spliced into the Vertex AI host name, so its shape decides a URL."""
    with pytest.raises(DeclarationError, match="location"):
        validate_document("gete", {**GETE, "location": location}, source="gete.yaml")


@pytest.mark.parametrize("location", ["global", "us", "eu", "us-central1"])
def test_gemini_enterprise_location_accepts_the_regions_it_offers(
    location: str,
) -> None:
    validate_document(
        "gete",
        {**GETE, "gemini_enterprise": {"location": location}},
        source="gete.yaml",
    )


@pytest.mark.parametrize("location", ["Global", "us east1", "../global"])
def test_gemini_enterprise_location_must_look_like_a_region(location: str) -> None:
    """It becomes a path segment of every Discovery Engine URL."""
    with pytest.raises(DeclarationError, match="location"):
        validate_document(
            "gete",
            {**GETE, "gemini_enterprise": {"location": location}},
            source="gete.yaml",
        )


def test_registration_engine_accepts_a_console_id() -> None:
    validate_document(
        "agent",
        {**AGENT, "registration": {"gemini_enterprise": {"engine": "app_1234567890"}}},
        source="agent.yaml",
    )


@pytest.mark.parametrize(
    "engine", ["../../../authorizations", "app 1", "app/1", "-app"]
)
def test_registration_engine_must_be_an_identifier(engine: str) -> None:
    """It is spliced into the Discovery Engine URL path; a slash addresses elsewhere."""
    with pytest.raises(DeclarationError, match="engine"):
        validate_document(
            "agent",
            {**AGENT, "registration": {"gemini_enterprise": {"engine": engine}}},
            source="agent.yaml",
        )


@pytest.mark.parametrize(
    "name", ["Mail-Triage", "mail_triage", "-mail", "mail-", "a" * 64]
)
def test_agent_name_must_be_an_rfc1034_label(name: str) -> None:
    """The name becomes a service account and an authorization id; both need a label."""
    with pytest.raises(DeclarationError, match="name"):
        validate_document("agent", {**AGENT, "name": name}, source="agent.yaml")


def test_connections_must_be_unique() -> None:
    with pytest.raises(DeclarationError, match="connections"):
        validate_document(
            "agent", {**AGENT, "connections": ["freee", "freee"]}, source="agent.yaml"
        )


@pytest.mark.parametrize(
    "tool",
    [
        {},
        {"builtin": "google_search", **MCP_TOOL},
        {"mcp": {}},
        {"mcp": {"url": "http://mcp.example.com/mcp"}},
        {"mcp": {"url": "https://mcp.example.com/mcp", "effect": "readonly"}},
        {"mcp": {"url": "https://mcp.example.com/mcp", "allow": []}},
        {"python": {"effect": "read"}},
        {"python": "no_colon_here"},
        {"openapi": {"spec": "./spec.yaml"}},
    ],
)
def test_tool_must_be_exactly_one_supported_kind(tool: dict[str, Any]) -> None:
    """One key per tool, https for MCP, effect read or write; openapi is not in."""
    with pytest.raises(DeclarationError, match="tools"):
        validate_document("agent", {**AGENT, "tools": [tool]}, source="agent.yaml")


@pytest.mark.parametrize(
    "tool",
    [
        {"builtin": "google_search"},
        {**MCP_TOOL},
        {
            "mcp": {
                "url": "https://mcp.example.com/mcp",
                "connection": "freee",
                "headers": {"X-Trace": "${TRACE}"},
                "allow": ["get_deals"],
                "effect": "read",
                "does_not": "Does not create deals",
                "timeout": 10,
            }
        },
        {"python": "my_agent.agent:TOOLS"},
        {"python": {"ref": "my_agent.agent:TOOLS", "effect": "read"}},
    ],
)
def test_supported_tool_shapes_pass(tool: dict[str, Any]) -> None:
    validate_document("agent", {**AGENT, "tools": [tool]}, source="agent.yaml")


def test_env_values_must_be_strings() -> None:
    """Agent Engine takes env values as strings; YAML would happily hand over an int."""
    with pytest.raises(DeclarationError, match="env"):
        validate_document(
            "agent",
            {**AGENT, "runtime": {"agent_engine": {"env": {"COMPANY_ID": 123456}}}},
            source="agent.yaml",
        )


def test_policy_when_is_one_of_the_known_conditions() -> None:
    with pytest.raises(DeclarationError, match="when"):
        validate_document(
            "policy", [{"name": "x", "when": "sometimes"}], source="p.yaml"
        )


def test_policy_require_confirmation_is_a_keyword_or_a_tool_list() -> None:
    for value in ("write_tools", "all", ["write_estimate_tag"]):
        validate_document(
            "policy",
            [{"name": "x", "when": "always", "require_confirmation": value}],
            source="p.yaml",
        )
    with pytest.raises(DeclarationError, match="require_confirmation"):
        validate_document(
            "policy",
            [{"name": "x", "when": "always", "require_confirmation": "some"}],
            source="p.yaml",
        )


@pytest.mark.parametrize(
    "patch",
    [
        {"hosts": ["*.example.com"]},
        {"hosts": ["https://api.example.com"]},
        {"docs": "http://example.com"},
        {
            "oauth": {
                **CONNECTION["oauth"],
                "token_url": "http://auth.example.com/token",
            }
        },
        {"oauth": {**CONNECTION["oauth"], "scopes": {"read": ""}}},
        {"mcp": {"url": "http://mcp.example.com"}},
        {"verified": {"gemini_enterprise": "yesterday"}},
    ],
)
def test_connection_rejects_wildcards_plain_http_and_empty_scope_text(
    patch: dict[str, Any],
) -> None:
    """Bare host names, https only, and scope text for the consent screen."""
    with pytest.raises(DeclarationError):
        validate_document("connection", {**CONNECTION, **patch}, source="c.yaml")


def test_connection_messages_declare_the_reauthorization_text() -> None:
    """The prompt is for end users; empty text would show them nothing."""
    validate_document(
        "connection",
        {**CONNECTION, "messages": {"reauthorization": "承認し直してください"}},
        source="c.yaml",
    )
    with pytest.raises(DeclarationError, match="reauthorization"):
        validate_document(
            "connection",
            {**CONNECTION, "messages": {"reauthorization": ""}},
            source="c.yaml",
        )


def test_yaml_dates_stay_strings(tmp_path: Path) -> None:
    """PyYAML would make 2026-08-20 a date object; the schema expects a string."""
    path = tmp_path / "c.yaml"
    path.write_text("verified:\n  gemini_enterprise: 2026-08-20\n")
    assert read_yaml(path) == {"verified": {"gemini_enterprise": "2026-08-20"}}


@pytest.mark.parametrize("url", ["https://", "https:// evil .com/x", "https://a b/c"])
def test_mcp_url_is_held_to_the_same_https_shape_as_connections(url: str) -> None:
    with pytest.raises(DeclarationError, match="tools"):
        validate_document(
            "agent", {**AGENT, "tools": [{"mcp": {"url": url}}]}, source="a.yaml"
        )


def test_redact_patterns_must_be_valid_regular_expressions() -> None:
    """A pattern that does not compile would disable a security control at runtime."""
    policy = [
        {
            "name": "x",
            "when": "always",
            "redact": {"patterns": [{"pattern": "(unclosed", "replacement": "X"}]},
        }
    ]
    with pytest.raises(DeclarationError, match="pattern"):
        validate_document("policy", policy, source="p.yaml")


def test_redact_masks_are_declared_in_the_policy() -> None:
    policy = [
        {
            "name": "x",
            "when": "always",
            "redact": {"masks": {"hidden": "[非表示]", "digits": "[{n}桁]"}},
        }
    ]
    validate_document("policy", policy, source="p.yaml")
    with pytest.raises(DeclarationError, match="digits"):
        validate_document(
            "policy",
            [
                {
                    "name": "x",
                    "when": "always",
                    "redact": {"masks": {"digits": "no-count"}},
                }
            ],
            source="p.yaml",
        )
