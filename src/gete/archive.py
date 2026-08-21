"""Packing one agent into the archive Agent Engine receives.

The same input must give the same bytes. Terraform decides whether to deploy
by comparing the archive's hash, so anything that varies between two runs
over identical files (timestamps, owners, file order, gzip header) would
redeploy an unchanged agent.
"""

import base64
import gzip
import hashlib
import io
import json
import sys
import tarfile
from collections.abc import Iterator
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path, PurePosixPath
from typing import TextIO

import yaml

from gete.declaration import (
    RESOLVED_FILE,
    Agent,
    Project,
    find_project_file,
    load_project,
    resolve,
)
from gete.errors import DeclarationError
from gete.templates import template_text
from gete.validate import validate_project

ENTRY_FILE = "gete_entry.py"
REQUIREMENTS_FILE = "requirements.txt"

# Agent Engine's own runtime needs google-cloud-aiplatform; its adk extra
# brings ADK. gete pins ADK to the range it was verified with.
BASE_REQUIREMENTS = ("google-cloud-aiplatform[adk,agent_engines]>=1.140",)

EXCLUDED_DIRS = frozenset(
    {"__pycache__", ".venv", ".ruff_cache", ".pytest_cache", ".mypy_cache"}
)
EXCLUDED_SUFFIXES = frozenset({".pyc", ".pyo"})
EXCLUDED_NAMES = frozenset({".DS_Store"})


@dataclass(frozen=True)
class ArchiveResult:
    agent_name: str
    archive: bytes
    sha256: str


def requirements_text(agent: Agent, gete_version: str) -> str:
    """gete pinned to this version, the Agent Engine base, then the agent's lines."""
    lines = [f"gete=={gete_version}", *BASE_REQUIREMENTS]
    if agent.requirements is not None:
        lines.extend(
            line.strip()
            for line in agent.requirements.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    return "\n".join(lines) + "\n"


def build_archive(
    directory: Path, *, project: Project | None = None, gete_version: str | None = None
) -> ArchiveResult:
    """Validate, resolve, and pack the agent. Raises DeclarationError on problems."""
    directory = directory.resolve()
    project = project or load_project(find_project_file(directory))
    agent = _agent_in(project, directory)
    _check(project, agent)
    document = resolve(project, agent)
    entries: dict[str, bytes] = {
        ENTRY_FILE: template_text(ENTRY_FILE).encode(),
        REQUIREMENTS_FILE: requirements_text(
            agent, gete_version or version("gete")
        ).encode(),
    }
    instruction = agent.instruction_path
    if instruction is not None:
        entries[_inside(agent, instruction, "instruction")] = instruction.read_bytes()
    if agent.source is not None:
        # Agent Engine imports from the archive root, so the source directory's
        # contents go there and the resolved declaration points at ".".
        document["source"] = "."
        for relative, path in _source_files(agent.source):
            if relative in entries:
                raise DeclarationError(
                    f"source contains {relative}, which gete writes itself"
                )
            entries[relative] = path.read_bytes()
    entries[RESOLVED_FILE] = yaml.safe_dump(
        document, sort_keys=False, allow_unicode=True
    ).encode()
    archive = _pack(entries)
    return ArchiveResult(
        agent_name=agent.name,
        archive=archive,
        sha256=hashlib.sha256(archive).hexdigest(),
    )


def external_program(stdin: TextIO, stdout: TextIO, stderr: TextIO = sys.stderr) -> int:
    """Terraform's data "external" contract: JSON in, JSON strings out, 1 on failure."""
    try:
        query = json.load(stdin)
        result = build_archive(Path(query["directory"]))
    except Exception as error:  # noqa: BLE001 - everything must surface to Terraform
        print(str(error), file=stderr)
        return 1
    json.dump(
        {"archive": base64.b64encode(result.archive).decode(), "sha256": result.sha256},
        stdout,
    )
    return 0


def _agent_in(project: Project, directory: Path) -> Agent:
    for agent in project.agents:
        if agent.directory.resolve() == directory:
            return agent
    raise DeclarationError(
        f"{directory} is not an agent that passed the schema under {project.agents_dir}"
    )


def _check(project: Project, agent: Agent) -> None:
    own = PurePosixPath(project.display(agent.directory))
    agents = PurePosixPath(project.display(project.agents_dir))
    relevant = [
        problem
        for problem in validate_project(project)
        # This agent's problems, and everything outside agents/ (gete.yaml,
        # policies). Other agents' problems do not block this one.
        if _under(problem.source, own) or not _under(problem.source, agents)
    ]
    if relevant:
        lines = "\n".join(f"  {problem}" for problem in relevant)
        raise DeclarationError(f"{agent.name} cannot be archived:\n{lines}")


def _under(source: Path | str, directory: PurePosixPath) -> bool:
    """Whether the problem's file lies in the directory.

    Compared segment by segment: agents/mail is not part of agents/mail-triage,
    and a text comparison would say it is.
    """
    return PurePosixPath(source).is_relative_to(directory)


def _inside(agent: Agent, path: Path, what: str) -> str:
    try:
        relative = path.resolve().relative_to(agent.directory.resolve())
    except ValueError:
        raise DeclarationError(
            f"{what} {path} is outside {agent.directory}; only files below it go in"
        ) from None
    return relative.as_posix()


def _source_files(source: Path) -> Iterator[tuple[str, Path]]:
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        if (
            EXCLUDED_DIRS.intersection(relative.parts[:-1])
            or path.suffix in EXCLUDED_SUFFIXES
            or path.name in EXCLUDED_NAMES
        ):
            continue
        yield PurePosixPath(relative).as_posix(), path


def _pack(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w", format=tarfile.GNU_FORMAT) as tar,
    ):
        for name in sorted(entries):
            info = tarfile.TarInfo(name)
            info.size = len(entries[name])
            info.mtime = 0
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(entries[name]))
    return buffer.getvalue()
