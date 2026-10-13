"""gete init: the files a new agent (and a new project) starts from."""

from pathlib import Path

import pytest
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


def test_cli_init_creates_the_project_when_there_is_none(
    outside_any_project: Path,
) -> None:
    result = CliRunner().invoke(main, ["init", "mail-triage"])
    assert result.exit_code == 0, result.output
    assert (outside_any_project / "gete.yaml").is_file()
    assert (outside_any_project / "agents" / "mail-triage" / "agent.yaml").is_file()
    assert "gete.yaml" in result.output


@pytest.mark.usefixtures("below_project")
def test_cli_init_adds_to_an_existing_project_from_anywhere_below_it(
    project: ProjectBuilder,
) -> None:
    result = CliRunner().invoke(main, ["init", "new-agent"])
    assert result.exit_code == 0, result.output
    assert (project.root / "agents" / "new-agent" / "agent.yaml").is_file()


NAMED_ENGINES = (
    "version: 1\n"
    "project: example-project\n"
    "location: us-central1\n"
    "gemini_enterprise:\n"
    "  engines:\n"
    "    sales: my-sales-app_1234567890\n"
)


def test_init_agent_below_an_engines_directory(tmp_path: Path) -> None:
    """gete init sales/mail-triage: the agent is listed in sales by where it is."""
    init_project(tmp_path)
    (tmp_path / "gete.yaml").write_text(NAMED_ENGINES)
    written = init_agent(tmp_path, "sales/mail-triage")
    assert {p.name for p in written} == {"agent.yaml", "instruction.md"}
    assert (tmp_path / "agents" / "sales" / "mail-triage" / "agent.yaml").is_file()
    project = load_project(tmp_path / "gete.yaml")
    assert [str(p) for p in validate_project(project)] == []
    assert project.agents[0].name == "mail-triage"
    assert project.agents[0].engine == "sales"


def test_init_agent_refuses_a_group_that_is_no_named_engine(tmp_path: Path) -> None:
    """validate would refuse the directory; better to refuse before writing it."""
    from gete.errors import DeclarationError

    init_project(tmp_path)
    with pytest.raises(DeclarationError, match="'sales'.*gemini_enterprise.engines"):
        init_agent(tmp_path, "sales/mail-triage")
    (tmp_path / "gete.yaml").write_text(NAMED_ENGINES)
    with pytest.raises(DeclarationError, match="'support'.*sales"):
        init_agent(tmp_path, "support/mail-triage")
    assert not (tmp_path / "agents").exists()


def test_init_agent_refuses_a_second_directory_level(tmp_path: Path) -> None:
    from gete.errors import DeclarationError

    init_project(tmp_path)
    (tmp_path / "gete.yaml").write_text(NAMED_ENGINES)
    with pytest.raises(DeclarationError, match="one level"):
        init_agent(tmp_path, "sales/eu/mail-triage")
    assert not (tmp_path / "agents").exists()


def test_cli_init_says_what_it_wrote_before_refusing_the_group(
    outside_any_project: Path,
) -> None:
    """A fresh gete.yaml names no engines, so the group is refused; the file
    it did create is still reported, since that is where the engine goes."""
    result = CliRunner().invoke(main, ["init", "sales/mail-triage"])
    assert result.exit_code == 1, result.output
    assert (outside_any_project / "gete.yaml").is_file()
    assert "gete.yaml" in result.output
    assert "'sales'" in result.output


def test_init_agent_refuses_a_group_whose_directory_is_an_agent(
    tmp_path: Path,
) -> None:
    """agents/sales/agent.yaml makes sales an agent; an agent below it would
    be stepped over by the loader, so init refuses to write one."""
    from gete.errors import DeclarationError

    init_project(tmp_path)
    (tmp_path / "gete.yaml").write_text(NAMED_ENGINES)
    init_agent(tmp_path, "sales")
    with pytest.raises(DeclarationError, match="'sales'.*agent"):
        init_agent(tmp_path, "sales/mail-triage")
    assert not (tmp_path / "agents" / "sales" / "mail-triage").exists()


def test_init_agent_refuses_an_agent_on_top_of_an_engines_group(
    tmp_path: Path,
) -> None:
    """The reverse: agents/sales/ already holds agents, so gete init sales
    would turn the group into an agent and hide them."""
    from gete.errors import DeclarationError

    init_project(tmp_path)
    (tmp_path / "gete.yaml").write_text(NAMED_ENGINES)
    init_agent(tmp_path, "sales/mail-triage")
    with pytest.raises(DeclarationError, match="'sales'.*group"):
        init_agent(tmp_path, "sales")
    assert not (tmp_path / "agents" / "sales" / "agent.yaml").exists()
