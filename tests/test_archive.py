"""The archive: what is sent to Agent Engine, and that it is the same every time."""

import base64
import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest
import yaml
from conftest import ProjectBuilder

from gete.archive import (
    ArchiveResult,
    build_archive,
    external_program,
    requirements_text,
)
from gete.declaration import RESOLVED_FILE, load_project
from gete.errors import DeclarationError

ENTRY = "gete_entry.py"


def prepare(
    project: ProjectBuilder, name: str = "mail-triage", **agent: object
) -> Path:
    project.write_project(
        {"version": 1, "project": "example-project", "location": "us-central1"}
    )
    return project.write_agent(name, dict(agent))


def members(result: ArchiveResult) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(result.archive), mode="r:gz") as tar:
        return {
            member.name: tar.extractfile(member).read()  # type: ignore[union-attr]
            for member in tar.getmembers()
            if member.isfile()
        }


def test_archive_holds_entry_resolved_instruction_and_requirements(
    project: ProjectBuilder,
) -> None:
    prepare(project)
    result = build_archive(project.root / "agents" / "mail-triage")
    files = members(result)
    assert set(files) == {ENTRY, RESOLVED_FILE, "instruction.md", "requirements.txt"}
    assert b'with_name("agent.resolved.yaml")' in files[ENTRY]
    assert files["instruction.md"] == b"You sort mail."
    resolved = yaml.safe_load(files[RESOLVED_FILE])
    assert resolved["name"] == "mail-triage"
    assert "resolved" in resolved


def test_same_input_gives_the_same_bytes(project: ProjectBuilder) -> None:
    """Terraform compares the archive hash; a spurious difference redeploys."""
    prepare(project)
    directory = project.root / "agents" / "mail-triage"
    first = build_archive(directory)
    second = build_archive(directory)
    assert first.archive == second.archive
    assert first.sha256 == hashlib.sha256(first.archive).hexdigest()


def test_tar_metadata_is_neutral(project: ProjectBuilder) -> None:
    """mtime, owner, and order are what usually make identical files differ."""
    prepare(project)
    with tarfile.open(
        fileobj=io.BytesIO(
            build_archive(project.root / "agents" / "mail-triage").archive
        ),
        mode="r:gz",
    ) as tar:
        for member in tar.getmembers():
            assert member.mtime == 0
            assert (member.uid, member.gid, member.uname, member.gname) == (
                0,
                0,
                "",
                "",
            )
        names = [member.name for member in tar.getmembers()]
    assert names == sorted(names)


def test_source_contents_are_placed_at_the_root_and_caches_left_out(
    project: ProjectBuilder,
) -> None:
    """Agent Engine imports from the archive root, so source/ is flattened into it."""
    directory = prepare(
        project, source="./src", tools=[{"python": "mail_triage.tools:TOOLS"}]
    )
    package = directory / "src" / "mail_triage"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "tools.py").write_text("TOOLS = []\n")
    (package / "__pycache__").mkdir()
    (package / "__pycache__" / "tools.cpython-312.pyc").write_bytes(b"x")
    (directory / "src" / ".DS_Store").write_bytes(b"x")
    files = members(build_archive(directory))
    assert "mail_triage/tools.py" in files
    assert "mail_triage/__init__.py" in files
    assert not any(
        "__pycache__" in name or name.endswith(".DS_Store") for name in files
    )
    assert yaml.safe_load(files[RESOLVED_FILE])["source"] == "."


def test_requirements_start_with_gete_and_aiplatform_then_the_agents_own(
    project: ProjectBuilder,
) -> None:
    directory = prepare(project, requirements="./requirements.txt")
    (directory / "requirements.txt").write_text("pandas>=2\n\nopenpyxl\n")
    text = requirements_text(
        load_project(project.root / "gete.yaml").agents[0], "1.2.3"
    )
    assert text.splitlines() == [
        "gete==1.2.3",
        "google-cloud-aiplatform[adk,agent_engines]>=1.140",
        "pandas>=2",
        "openpyxl",
    ]


def test_requirements_without_an_agent_file_are_just_the_base(
    project: ProjectBuilder,
) -> None:
    prepare(project)
    text = requirements_text(
        load_project(project.root / "gete.yaml").agents[0], "1.2.3"
    )
    assert text.splitlines() == [
        "gete==1.2.3",
        "google-cloud-aiplatform[adk,agent_engines]>=1.140",
    ]


def test_external_program_speaks_terraforms_contract(project: ProjectBuilder) -> None:
    """data "external" sends {"directory": ...} and wants string values back."""
    directory = prepare(project)
    out = io.StringIO()
    code = external_program(io.StringIO(json.dumps({"directory": str(directory)})), out)
    assert code == 0
    payload = json.loads(out.getvalue())
    assert set(payload) == {"archive", "sha256"}
    assert all(isinstance(value, str) for value in payload.values())
    archive = base64.b64decode(payload["archive"])
    assert payload["sha256"] == hashlib.sha256(archive).hexdigest()
    assert payload["sha256"] == build_archive(directory).sha256


def test_external_program_reports_errors_on_stderr_and_exits_one(
    tmp_path: Path,
) -> None:
    out = io.StringIO()
    err = io.StringIO()
    code = external_program(
        io.StringIO(json.dumps({"directory": str(tmp_path / "missing")})), out, err
    )
    assert code == 1
    assert out.getvalue() == ""
    assert "missing" in err.getvalue()


def test_another_agents_problem_does_not_block_this_one(
    project: ProjectBuilder,
) -> None:
    """Only this agent's problems and the project's stop it from being packed."""
    prepare(project, name="mail")
    project.write_agent("mail-triage", {"connections": ["salesforce"]})
    assert build_archive(project.root / "agents" / "mail").agent_name == "mail"


def test_this_agents_own_problem_still_blocks_it(project: ProjectBuilder) -> None:
    prepare(project, name="mail", connections=["salesforce"])
    with pytest.raises(DeclarationError, match="salesforce"):
        build_archive(project.root / "agents" / "mail")
