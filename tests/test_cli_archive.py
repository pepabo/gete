"""gete archive from the command line."""

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
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=project.root):
        result = runner.invoke(
            main,
            ["archive", str(project.agents_dir / "mail-triage"), "--out", str(out)],
        )
    assert result.exit_code == 0, result.output
    assert hashlib.sha256(out.read_bytes()).hexdigest() in result.output


def test_archive_refuses_an_agent_that_does_not_validate(
    project: ProjectBuilder, tmp_path: Path
) -> None:
    project.write_agent("mail-triage", {"connections": ["nope"]})
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=project.root):
        result = runner.invoke(
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
