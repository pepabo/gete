"""Every bundled connection must be sound on its own and against the others."""

from importlib.resources import files

import pytest

from gete.catalog import catalog_connections
from gete.connection import Registry
from gete.connection.checks import connection_problems
from gete.declaration import load_yaml_text
from gete.schema import validate_document

CATALOG = catalog_connections()


def test_catalog_has_the_initial_connections() -> None:
    assert {"freee", "google", "github", "notion-mcp", "slack"} <= set(CATALOG)


@pytest.mark.parametrize("connection_id", sorted(CATALOG))
def test_entry_matches_the_schema_and_its_file_name(connection_id: str) -> None:
    entry = CATALOG[connection_id]
    validate_document("connection", entry, source=f"{connection_id}.yaml")
    assert entry["id"] == connection_id


@pytest.mark.parametrize("connection_id", sorted(CATALOG))
def test_entry_passes_the_connection_checks(connection_id: str) -> None:
    registry = Registry.from_catalog()
    entry = registry.get(connection_id, include_retired=True)
    assert connection_problems(entry, registry) == []


@pytest.mark.parametrize("connection_id", sorted(CATALOG))
def test_examples_accept_and_reject_as_declared(connection_id: str) -> None:
    entry = Registry.from_catalog().get(connection_id, include_retired=True)
    for token in entry.examples.accepts:
        assert entry.accepts_token(token), token
    for token in entry.examples.rejects:
        assert not entry.accepts_token(token), token


def test_google_access_tokens_are_rejected_everywhere_but_google() -> None:
    registry = Registry.from_catalog()
    for entry in registry.all(include_retired=True):
        expected = entry.id == "google"
        assert entry.accepts_token("ya29.a0AfH6SMB") is expected, entry.id


def test_slack_is_retired_with_a_reason() -> None:
    assert CATALOG["slack"]["retired"]


def test_google_hosts_are_specific_apis_not_the_whole_domain() -> None:
    """A Workspace authorization must not be usable against GCP APIs."""
    hosts = CATALOG["google"]["hosts"]
    assert "googleapis.com" not in hosts
    # www.googleapis.com serves storage, compute, and oauth2 as well.
    assert "www.googleapis.com" not in hosts
    assert all(host.endswith(".googleapis.com") for host in hosts)


def test_catalog_files_are_read_with_dates_as_strings() -> None:
    """The loader used for the catalog is the same one that keeps dates as strings."""
    text = (
        files("gete.catalog")
        .joinpath("connections/freee.yaml")
        .read_text(encoding="utf-8")
    )
    assert isinstance(load_yaml_text(text)["verified"]["gemini_enterprise"], str)


def test_notion_mcp_does_not_reach_the_notion_api() -> None:
    """The MCP endpoint is a different issuer; its token is not an API token."""
    hosts = CATALOG["notion-mcp"]["hosts"]
    assert hosts == ["mcp.notion.com"]
    assert "api.notion.com" not in hosts


def test_notion_mcp_says_what_a_person_has_to_do_before_authorizing() -> None:
    """Its client cannot be deleted once registered, and its grant is not ours."""
    setup = CATALOG["notion-mcp"]["setup"]
    assert "redirect URI" in setup
    assert "effect: read" in setup


def test_a_connection_fronting_another_grant_carries_no_scopes() -> None:
    """Nothing here chooses the permissions; the provider's consent screen does."""
    assert CATALOG["notion-mcp"]["oauth"]["scopes"] == {}


def test_zendesk_leaves_its_root_open_until_an_installation_names_it() -> None:
    """The tenant is a subdomain; a stand-in host would be a name a stranger
    could register, and then a user's token would be sent there."""
    zendesk = Registry.from_catalog().get("zendesk")
    assert zendesk.needs_base_url
    assert not zendesk.hosts
    assert zendesk.oauth.authorization_url.startswith("{base_url}/")
    assert zendesk.oauth.token_url.startswith("{base_url}/")


def test_zendesk_offers_the_narrow_write_scope_next_to_read() -> None:
    """write alone would cover users, organizations, and settings as well."""
    scopes = CATALOG["zendesk"]["oauth"]["scopes"]
    assert set(scopes) == {"read", "tickets:write"}


def test_zendesk_says_what_a_person_has_to_do_before_authorizing() -> None:
    setup = CATALOG["zendesk"]["setup"]
    assert "OAuth" in setup
    assert "redirect URI" in setup
