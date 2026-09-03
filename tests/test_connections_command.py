"""gete connections: what an installation can declare, and what is known to work."""

from typing import Any

from click.testing import CliRunner
from conftest import ProjectBuilder

from gete.cli import main
from gete.connection import Connection, Registry
from gete.connections_listing import connections_table, format_connection


def test_table_lists_every_connection_with_hosts_and_verification() -> None:
    rows = connections_table(Registry.from_catalog())
    by_id = {row["id"]: row for row in rows}
    assert set(by_id) >= {"freee", "google", "github", "slack"}
    assert "api.freee.co.jp" in by_id["freee"]["hosts"]
    assert by_id["freee"]["verified"] == "2026-08-20"
    assert by_id["github"]["verified"] == "not verified in Gemini Enterprise"
    assert by_id["slack"]["status"] == "retired"
    assert by_id["freee"]["status"] == "available"


def test_table_includes_private_connections_and_marks_their_source() -> None:
    registry = Registry.from_catalog(
        {
            "internal": {
                "display_name": "Internal",
                "hosts": ["api.internal.example.com"],
                "oauth": {
                    "authorization_url": "https://auth.internal.example.com/a",
                    "token_url": "https://auth.internal.example.com/t",
                    "scopes": {},
                },
            }
        }
    )
    rows = {row["id"]: row for row in connections_table(registry)}
    assert rows["internal"]["source"] == "gete.yaml"
    assert rows["freee"]["source"] == "catalog"


def test_the_description_shows_the_menu_next_to_the_default_scopes() -> None:
    """Preparing the OAuth client takes everything an agent may select, too."""
    entry = Connection.from_mapping(
        {
            "id": "example",
            "display_name": "Example",
            "hosts": ["api.example.com"],
            "oauth": {
                "authorization_url": "https://auth.example.com/authorize",
                "token_url": "https://auth.example.com/token",
                "scopes": {"read": "Read data"},
                "optional_scopes": {"write": "Change data"},
            },
        }
    )
    output = format_connection(entry)
    assert "read: Read data" in output
    assert "write: Change data" in output
    assert "optional scopes" in output


def test_cli_prints_one_line_per_connection(project: ProjectBuilder) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=project.root):
        result = runner.invoke(main, ["connections"])
    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert any(line.startswith("freee") for line in lines)
    assert any("retired" in line and line.startswith("slack") for line in lines)


def test_cli_connections_works_without_a_project() -> None:
    """The catalog is worth reading before there is a gete.yaml."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["connections"])
    assert result.exit_code == 0, result.output
    assert "freee" in result.output


# What a person has to do before anyone can authorize: register the client,
# put it where register reads it, and get the one-shot parts right.
SETUP = (
    "Register an OAuth client with the service yourself.\n"
    "The consent screen is the service's own and grants more than reading."
)

WITH_SETUP: dict[str, Any] = {
    "display_name": "Internal API",
    "hosts": ["api.internal.example.com"],
    "token_prefixes": ["ia_"],
    "setup": SETUP,
    "oauth": {
        "authorization_url": "https://auth.internal.example.com/authorize",
        "token_url": "https://auth.internal.example.com/token",
        "scopes": {"read": "Read internal data"},
    },
}


def describe(project: ProjectBuilder, connection_id: str) -> Any:
    project.write_project(
        {
            "version": 1,
            "project": "example-project",
            "location": "us-central1",
            "connections": {"internal-api": WITH_SETUP},
        }
    )
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=project.root):
        return runner.invoke(main, ["connections", connection_id])


def test_describing_a_connection_prints_what_a_person_has_to_do_first(
    project: ProjectBuilder,
) -> None:
    result = describe(project, "internal-api")
    assert result.exit_code == 0, result.output
    for line in SETUP.splitlines():
        assert line in result.output


def test_the_description_names_the_secrets_and_the_redirect_uri(
    project: ProjectBuilder,
) -> None:
    """Registering a client takes all three at once, and some cannot be undone."""
    output = describe(project, "internal-api").output
    assert "ge-oauth-internal-api-client-id" in output
    assert "ge-oauth-internal-api-client-secret" in output
    assert "https://vertexaisearch.cloud.google.com/oauth-redirect" in output


def test_a_connection_without_setup_notes_is_still_described(
    project: ProjectBuilder,
) -> None:
    result = describe(project, "freee")
    assert result.exit_code == 0, result.output
    assert "api.freee.co.jp" in result.output
    assert "https://accounts.secure.freee.co.jp/public_api/token" in result.output


def test_describing_an_unknown_connection_names_the_known_ones(
    project: ProjectBuilder,
) -> None:
    result = describe(project, "salesforce")
    assert result.exit_code == 1
    assert "freee" in result.output


def test_a_catalog_connection_can_be_described_without_a_project() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["connections", "github"])
    assert result.exit_code == 0, result.output
    assert "api.github.com" in result.output


def test_a_retired_connection_reads_retired_with_the_reason_alongside() -> None:
    """The listing says "retired"; describing one must not say something else."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["connections", "slack"])
    assert result.exit_code == 0, result.output
    assert "status" in result.output and "retired" in result.output
    assert "native connector" in result.output


def prefixless(**patch: Any) -> Connection:
    return Connection.from_mapping(
        {
            "id": "example",
            "display_name": "Example",
            "hosts": ["api.example.com"],
            "token_prefixes": [],
            "oauth": {
                "authorization_url": "https://auth.example.com/authorize",
                "token_url": "https://auth.example.com/token",
                "scopes": {"read": "Read data"},
            },
            **patch,
        }
    )


def test_a_connection_without_prefixes_reads_as_accepted_by_elimination() -> None:
    rows = connections_table(Registry([prefixless()]))
    assert rows[0]["token_prefixes"] == "(none: by elimination)"
    assert "(none: by elimination)" in format_connection(prefixless())


def test_a_declared_token_format_reads_as_the_format_it_declares() -> None:
    """It is not accepted by elimination, and the person preparing the
    connection has to see which of the two it is."""
    entry = prefixless(tokens={"format": "jwt"})
    rows = connections_table(Registry([entry]))
    assert "jwt" in rows[0]["token_prefixes"]
    assert "elimination" not in rows[0]["token_prefixes"]
    assert "jwt" in format_connection(entry)
