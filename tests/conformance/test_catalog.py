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
    assert {"freee", "google", "github", "slack"} <= set(CATALOG)


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
    assert "googleapis.com" not in CATALOG["google"]["hosts"]
    assert all(host.endswith(".googleapis.com") for host in CATALOG["google"]["hosts"])


def test_catalog_files_are_read_with_dates_as_strings() -> None:
    """The loader used for the catalog is the same one that keeps dates as strings."""
    text = (
        files("gete.catalog")
        .joinpath("connections/freee.yaml")
        .read_text(encoding="utf-8")
    )
    assert isinstance(load_yaml_text(text)["verified"]["gemini_enterprise"], str)
