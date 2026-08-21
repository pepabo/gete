"""gete terraform from the command line."""

from click.testing import CliRunner
from conftest import ProjectBuilder

from gete.cli import main


def test_terraform_writes_files_then_check_passes(project: ProjectBuilder) -> None:
    project.write_agent("mail-triage")
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=project.root):
        written = runner.invoke(main, ["terraform", "--out", str(project.root / "tf")])
        assert written.exit_code == 0, written.output
        assert (project.root / "tf" / "mail_triage.tf").is_file()
        checked = runner.invoke(
            main, ["terraform", "--out", str(project.root / "tf"), "--check"]
        )
    assert checked.exit_code == 0, checked.output


def test_check_exits_one_and_names_the_stale_file(project: ProjectBuilder) -> None:
    project.write_agent("mail-triage")
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=project.root):
        runner.invoke(main, ["terraform", "--out", str(project.root / "tf")])
        project.write_agent("mail-triage", {"display_name": "Renamed"})
        checked = runner.invoke(
            main, ["terraform", "--out", str(project.root / "tf"), "--check"]
        )
    assert checked.exit_code == 1
    assert "mail_triage.tf" in checked.output
