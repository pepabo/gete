"""gete connections: what an installation can declare, and what is known to work."""

from click.testing import CliRunner
from conftest import ProjectBuilder

from gete.cli import main
from gete.connection import Registry
from gete.connections_listing import connections_table


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
