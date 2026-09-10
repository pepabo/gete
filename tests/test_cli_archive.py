"""gete archive from the command line.

archive takes the agent directory as an argument and finds the project by
walking up from it, so these tests pass absolute paths and never have to move
the working directory.
"""

import hashlib
from pathlib import Path

from click.testing import CliRunner
from conftest import ProjectBuilder

from gete.cli import main


def test_archive_writes_the_file_and_prints_the_hash(
    project: ProjectBuilder, tmp_path: Path
) -> None:
    project.write_agent("mail-triage")
    out = tmp_path / "out" / "mail-triage.tar.gz"
    result = CliRunner().invoke(
        main,
        ["archive", str(project.agents_dir / "mail-triage"), "--out", str(out)],
    )
    assert result.exit_code == 0, result.output
    assert hashlib.sha256(out.read_bytes()).hexdigest() in result.output


def test_archive_refuses_an_agent_that_does_not_validate(
    project: ProjectBuilder, tmp_path: Path
) -> None:
    project.write_agent("mail-triage", {"connections": ["nope"]})
    result = CliRunner().invoke(
        main,
        [
            "archive",
            str(project.agents_dir / "mail-triage"),
            "--out",
            str(tmp_path / "a.tgz"),
        ],
    )
    assert result.exit_code == 1
    assert "nope" in result.output


def test_external_mode_needs_no_directory_argument(project: ProjectBuilder) -> None:
    """Terraform's data "external" runs `gete archive --external` with JSON on stdin."""
    import json

    directory = project.write_agent("mail-triage")
    result = CliRunner().invoke(
        main,
        ["archive", "--external"],
        input=json.dumps({"directory": str(directory)}),
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert set(payload) == {"archive", "sha256"}


def test_plain_mode_still_requires_the_directory() -> None:
    result = CliRunner().invoke(main, ["archive"])
    assert result.exit_code != 0
    assert "DIRECTORY" in result.output
