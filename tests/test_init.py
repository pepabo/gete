"""gete init: the files a new agent (and a new project) starts from."""

from pathlib import Path

from click.testing import CliRunner
from conftest import ProjectBuilder

from gete.cli import main
from gete.declaration import load_project
from gete.scaffold import init_agent, init_project
from gete.validate import validate_project


def test_init_project_writes_gete_yaml_and_an_example_policy(tmp_path: Path) -> None:
    written = init_project(tmp_path)
    assert {p.name for p in written} == {"gete.yaml", "example.yaml"}
    assert (tmp_path / "gete.yaml").is_file()
    assert (tmp_path / "policies" / "example.yaml").is_file()
    text = (tmp_path / "gete.yaml").read_text()
    assert "version: 1" in text
    assert "./policies/example.yaml" in text


def test_init_agent_writes_agent_yaml_and_instruction(tmp_path: Path) -> None:
    init_project(tmp_path)
    written = init_agent(tmp_path, "mail-triage")
    assert {p.name for p in written} == {"agent.yaml", "instruction.md"}
    assert (tmp_path / "agents" / "mail-triage" / "agent.yaml").is_file()


def test_what_init_writes_passes_validate(tmp_path: Path) -> None:
    """A scaffold that fails its own validator teaches the wrong shape."""
    init_project(tmp_path)
    init_agent(tmp_path, "mail-triage")
    project = load_project(tmp_path / "gete.yaml")
    assert [str(p) for p in validate_project(project)] == []
    assert project.agents[0].name == "mail-triage"


def test_init_never_overwrites_existing_files(tmp_path: Path) -> None:
    init_project(tmp_path)
    (tmp_path / "gete.yaml").write_text(
        "version: 1\nproject: mine\nlocation: asia-northeast1\n"
    )
    agent_dir = tmp_path / "agents" / "mail-triage"
    agent_dir.mkdir(parents=True)
    (agent_dir / "instruction.md").write_text("Keep me.")
    assert init_project(tmp_path) == []
    written = init_agent(tmp_path, "mail-triage")
    assert [p.name for p in written] == ["agent.yaml"]
    assert (agent_dir / "instruction.md").read_text() == "Keep me."
    assert "mine" in (tmp_path / "gete.yaml").read_text()


def test_init_agent_refuses_a_name_that_is_not_a_label(tmp_path: Path) -> None:
    import pytest

    from gete.errors import DeclarationError

    init_project(tmp_path)
    with pytest.raises(DeclarationError, match="Mail"):
        init_agent(tmp_path, "Mail_Triage")


def test_cli_init_creates_the_project_when_there_is_none(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as cwd:
        result = runner.invoke(main, ["init", "mail-triage"])
        assert result.exit_code == 0, result.output
        assert (Path(cwd) / "gete.yaml").is_file()
        assert (Path(cwd) / "agents" / "mail-triage" / "agent.yaml").is_file()
        assert "gete.yaml" in result.output


def test_cli_init_adds_to_an_existing_project_from_anywhere_below_it(
    project: ProjectBuilder,
) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=project.root):
        result = runner.invoke(main, ["init", "new-agent"])
    assert result.exit_code == 0, result.output
    assert (project.root / "agents" / "new-agent" / "agent.yaml").is_file()
