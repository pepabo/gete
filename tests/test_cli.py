"""The gete command: exit codes and what it prints."""

from click.testing import CliRunner
from conftest import ProjectBuilder

from gete.cli import main


def test_validate_exits_zero_when_everything_is_fine(project: ProjectBuilder) -> None:
    project.write_agent("mail-triage")
    runner = CliRunner()
    # A directory below the project root; validate walks up to gete.yaml.
    with runner.isolated_filesystem(temp_dir=project.root):
        result = runner.invoke(main, ["validate"])
    assert result.exit_code == 0, result.output
    assert "1 agent" in result.output


def test_validate_lists_every_problem_and_exits_one(project: ProjectBuilder) -> None:
    project.write_agent(
        "mail-triage",
        {
            "connections": ["salesforce"],
            "runtime": {"agent_engine": {"env": {"X": ""}}},
        },
    )
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=project.root):
        result = runner.invoke(main, ["validate"])
    assert result.exit_code == 1
    assert "salesforce" in result.output
    assert "X" in result.output


def test_validate_reports_a_missing_project_file() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["validate"])
    assert result.exit_code == 1
    assert "gete.yaml" in result.output


def test_version_is_shown() -> None:
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0
    assert result.output.startswith("gete, version ")
