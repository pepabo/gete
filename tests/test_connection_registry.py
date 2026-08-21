"""Connections: which tokens a connection accepts and where it lets them go."""

from typing import Any

import pytest

from gete.connection import Connection, Registry
from gete.connection.checks import connection_problems
from gete.errors import DeclarationError, RetiredConnection, UnknownConnection

EXAMPLE: dict[str, Any] = {
    "id": "example",
    "display_name": "Example",
    "hosts": ["api.example.com"],
    "oauth": {
        "authorization_url": "https://auth.example.com/authorize",
        "token_url": "https://auth.example.com/token",
        "scopes": {"read": "Read data"},
    },
}


def connection(**patch: Any) -> Connection:
    return Connection.from_mapping({**EXAMPLE, **patch})


@pytest.fixture
def catalog() -> Registry:
    return Registry.from_catalog()


def test_token_is_accepted_only_with_a_declared_prefix(catalog: Registry) -> None:
    github = catalog.get("github")
    assert github.accepts_token("gho_16C7e42F292c6912E7710c838347Ae178B4a")
    assert not github.accepts_token("ya29.a0AfH6SMB")
    assert not github.accepts_token("a1b2c3d4e5f60718293a4b5c6d7e8f90")


def test_prefixless_connection_accepts_by_elimination(catalog: Registry) -> None:
    """freee tokens carry no prefix; anything resembling another service is refused."""
    freee = catalog.get("freee")
    assert freee.accepts_token("a1b2c3d4e5f60718293a4b5c6d7e8f90")
    assert not freee.accepts_token("ya29.a0AfH6SMB")
    assert not freee.accepts_token("gho_16C7e42F292c6912E7710c838347Ae178B4a")


@pytest.mark.parametrize(
    "token", ["", "eyJhbGciOiJSUzI1NiJ9.e30.sig", "a.b.c", "eyJ-only"]
)
def test_jwt_shaped_and_empty_tokens_are_never_accepted(
    catalog: Registry, token: str
) -> None:
    """JWTs are ID or service account tokens; none may go to an external service."""
    for entry in catalog.all(include_retired=True):
        assert not entry.accepts_token(token), entry.id


def test_prefixless_connection_alone_still_refuses_google_access_tokens() -> None:
    """Elimination must not depend on google being present in the registry."""
    alone = Registry([connection()]).get("example")
    assert not alone.accepts_token("ya29.a0AfH6SMB")
    assert alone.accepts_token("a1b2c3d4e5f60718293a4b5c6d7e8f90")


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://api.example.com/v1/things", True),
        ("https://API.EXAMPLE.COM:443/v1/things", True),
        ("http://api.example.com/v1/things", False),
        ("https://api.example.com.evil.example/v1", False),
        ("https://sub.api.example.com/v1", False),
        ("https://example.com/api", False),
        ("not a url", False),
    ],
)
def test_allows_only_https_to_an_exactly_declared_host(url: str, allowed: bool) -> None:
    assert connection().allows(url) is allowed


def test_base_url_host_is_allowed_too() -> None:
    """GitHub Enterprise changes the host per organization; the base URL carries it."""
    entry = connection(base_url="https://api.github.example.com/")
    assert entry.allows("https://api.github.example.com/repos")
    assert entry.allows("https://api.example.com/v1")


def test_oauth_client_defaults_to_ge_oauth_id() -> None:
    entry = connection()
    assert entry.secret_prefix == "ge-oauth-example"
    assert entry.client_id_secret == "ge-oauth-example-client-id"
    assert entry.client_secret_secret == "ge-oauth-example-client-secret"
    assert (
        connection(oauth_client="shared-client").client_id_secret
        == "shared-client-client-id"
    )


def test_catalog_entry_can_be_overridden_partially() -> None:
    registry = Registry.from_catalog(
        {"github": {"base_url": "https://api.github.example.com"}}
    )
    github = registry.get("github")
    assert "api.github.example.com" in github.hosts
    assert "api.github.com" in github.hosts
    assert github.oauth.token_url == "https://github.com/login/oauth/access_token"


def test_override_can_replace_nested_oauth_values_without_restating_the_rest() -> None:
    registry = Registry.from_catalog(
        {"google": {"oauth": {"scopes": {"openid": "Identify you"}}}}
    )
    google = registry.get("google")
    assert google.oauth.scopes == {"openid": "Identify you"}
    assert (
        google.oauth.authorization_url == "https://accounts.google.com/o/oauth2/v2/auth"
    )


def test_private_connection_must_be_complete() -> None:
    with pytest.raises(DeclarationError, match="internal"):
        Registry.from_catalog({"internal": {"display_name": "Internal API"}})


def test_private_connection_takes_its_id_from_the_key() -> None:
    registry = Registry.from_catalog(
        {"internal": {k: v for k, v in EXAMPLE.items() if k != "id"}}
    )
    assert registry.get("internal").id == "internal"


def test_override_may_not_rename_the_connection() -> None:
    with pytest.raises(DeclarationError, match="id"):
        Registry.from_catalog({"github": {"id": "gh"}})


def test_overrides_are_checked_against_the_schema() -> None:
    with pytest.raises(DeclarationError, match="hots"):
        Registry.from_catalog({"github": {"hots": ["api.github.com"]}})


def test_unknown_connection_names_the_known_ones(catalog: Registry) -> None:
    with pytest.raises(UnknownConnection, match="freee"):
        catalog.get("nope")


def test_retired_connection_explains_why(catalog: Registry) -> None:
    with pytest.raises(RetiredConnection, match="connector"):
        catalog.get("slack")
    assert catalog.get("slack", include_retired=True).retired


def test_overlapping_prefixes_between_connections_are_reported() -> None:
    """Elimination only works if no two services can claim the same token."""
    a = connection(id="a", token_prefixes=["tok_"])
    b = connection(id="b", token_prefixes=["tok_v2_"])
    registry = Registry([a, b])
    assert any(
        "tok_" in problem
        for problem in connection_problems(registry.get("a"), registry)
    )


def test_too_broad_hosts_are_reported() -> None:
    """Exact matching does not excuse listing a whole platform domain."""
    entry = connection(hosts=["googleapis.com"])
    assert any(
        "googleapis.com" in problem
        for problem in connection_problems(entry, Registry([entry]))
    )


def test_connection_without_hosts_is_reported() -> None:
    entry = Connection.from_mapping({k: v for k, v in EXAMPLE.items() if k != "hosts"})
    assert any(
        "hosts" in problem for problem in connection_problems(entry, Registry([entry]))
    )


def test_mcp_host_must_be_a_declared_host() -> None:
    entry = connection(mcp={"url": "https://mcp.example.com/mcp"})
    assert any(
        "mcp.example.com" in problem
        for problem in connection_problems(entry, Registry([entry]))
    )
    assert (
        connection_problems(
            connection(
                hosts=["api.example.com", "mcp.example.com"],
                mcp={"url": "https://mcp.example.com/mcp"},
            ),
            Registry([entry]),
        )
        == []
    )


def test_examples_are_checked_against_accepts_token() -> None:
    entry = connection(
        token_prefixes=["ex_"], examples={"accepts": ["other_1"], "rejects": ["ex_1"]}
    )
    problems = connection_problems(entry, Registry([entry]))
    assert any("other_1" in problem for problem in problems)
    assert any("ex_1" in problem for problem in problems)
