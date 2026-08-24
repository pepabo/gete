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
from importlib.metadata import requires
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

# The name gete's own files travel under inside the archive.
GETE_PACKAGE_DIR = "gete"

EXCLUDED_DIRS = frozenset({"__pycache__"})
EXCLUDED_SUFFIXES = frozenset({".pyc", ".pyo"})


@dataclass(frozen=True)
class ArchiveResult:
    agent_name: str
    archive: bytes
    sha256: str


def runtime_requirements() -> list[str]:
    """gete's own runtime dependencies, read from its package metadata.

    gete is not on PyPI; its source travels inside the archive, and nothing
    resolves the dependencies of vendored source. They are spelled out here
    from the same metadata pip would have used, so the two cannot drift. The
    cli extra stays out: the deployment never runs the commands.
    """
    lines: list[str] = []
    for requirement in requires("gete") or ():
        specifier, _, marker = requirement.partition(";")
        if "extra" in marker:
            continue
        lines.append(specifier.strip())
    return lines


def requirements_text(agent: Agent) -> str:
    """The Agent Engine base, gete's dependencies, then the agent's own lines."""
    lines = [*BASE_REQUIREMENTS, *runtime_requirements()]
    if agent.requirements is not None:
        lines.extend(
            line.strip()
            for line in agent.requirements.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    return "\n".join(lines) + "\n"


def build_archive(directory: Path, *, project: Project | None = None) -> ArchiveResult:
    """Validate, resolve, and pack the agent. Raises DeclarationError on problems."""
    directory = directory.resolve()
    project = project or load_project(find_project_file(directory))
    agent = _agent_in(project, directory)
    _check(project, agent)
    # A path that climbs out of the agent's directory would ship whatever it
    # reaches to Agent Engine, and on into the Terraform state.
    if agent.source is not None:
        _inside(agent, agent.source, "source")
    if agent.requirements is not None:
        _inside(agent, agent.requirements, "requirements")
    document = resolve(project, agent)
    entries: dict[str, bytes] = {
        ENTRY_FILE: template_text(ENTRY_FILE).encode(),
        REQUIREMENTS_FILE: requirements_text(agent).encode(),
    }
    # gete itself rides along: it is not on PyPI, and shipping the copy that
    # packed the agent means the runtime can never be a different version.
    entries.update(_gete_files())
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


def _gete_files() -> dict[str, bytes]:
    import gete

    root = Path(gete.__file__).parent
    return {
        f"{GETE_PACKAGE_DIR}/{name}": path.read_bytes()
        for name, path in _source_files(root)
    }


def _source_files(source: Path) -> Iterator[tuple[str, Path]]:
    # A symlink is read through: what would be packed under its visible,
    # in-tree name is the target - a hidden file, or anything outside the
    # tree that the packing user can read. Real files below a real root only.
    if source.is_symlink():
        raise DeclarationError(
            f"{source} is a symlink; the archive needs the real directory"
        )
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        # Hidden files are configuration and credentials (.env, .git, caches),
        # never agent code; nothing under a dot name goes to Agent Engine.
        if (
            any(part.startswith(".") for part in relative.parts)
            or EXCLUDED_DIRS.intersection(relative.parts[:-1])
            or path.suffix in EXCLUDED_SUFFIXES
        ):
            continue
        if path.is_symlink():
            raise DeclarationError(
                f"{relative.as_posix()} is a symlink; only real files below "
                f"{source} go in"
            )
        if not path.is_file():
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
