"""The gete command: exit codes and what it prints."""

from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from conftest import ProjectBuilder

from gete.cli import main
from gete.errors import DeclarationError


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
            "runtime": {"agent_engine": {"env": {"GOOGLE_CLOUD_PROJECT": "x"}}},
        },
    )
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=project.root):
        result = runner.invoke(main, ["validate"])
    assert result.exit_code == 1
    assert "salesforce" in result.output
    assert "GOOGLE_CLOUD_PROJECT" in result.output


def test_validate_reports_a_missing_project_file() -> None:
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(main, ["validate"])
    assert result.exit_code == 1
    assert "gete.yaml" in result.output


def test_register_passes_the_authorizations_to_reset_through(
    project: ProjectBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flag deletes; it has to arrive as given, once per authorization."""
    from gete.register import Summary

    seen: dict[str, Any] = {}

    def fake_register(
        project: Any, gcp: Any, notice: Path, names: Any, reset: Any
    ) -> Summary:
        seen["names"] = names
        seen["reset"] = list(reset)
        return Summary(registered=["finance"])

    monkeypatch.setattr("gete.cli.register_project", fake_register)
    monkeypatch.setattr("gete.gcp.GcpClient", lambda quota_project: object())
    project.write_agent("finance", {"connections": ["freee"]})
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=project.root):
        result = runner.invoke(
            main,
            [
                "register",
                "finance",
                "--reset-authorization",
                "finance-freee",
                "--reset-authorization",
                "finance-github",
            ],
        )
    assert result.exit_code == 0, result.output
    assert seen == {"names": ["finance"], "reset": ["finance-freee", "finance-github"]}


def test_version_is_shown() -> None:
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0
    assert result.output.startswith("gete, version ")


def test_a_failure_inside_the_import_check_is_a_message_not_a_traceback(
    project: ProjectBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(directory: Path, **kwargs: Any) -> None:
        raise DeclarationError("requirements.txt cannot be read")

    monkeypatch.setattr("gete.importcheck.import_check", boom)
    project.write_agent("mail-triage")
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=project.root):
        result = runner.invoke(main, ["validate", "--import-check"])
    assert result.exit_code == 1
    assert "requirements.txt cannot be read" in result.output
    assert not isinstance(result.exception, DeclarationError)
