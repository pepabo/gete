"""Reading declarations from disk: gete.yaml, the agents below it, and their shapes."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from gete.errors import DeclarationError
from gete.schema import problems as schema_problems
from gete.schema import validate_document

PROJECT_FILE = "gete.yaml"
AGENT_FILE = "agent.yaml"
DEFAULT_AGENTS_DIR = "agents"


class _StringDatesLoader(yaml.SafeLoader):
    """SafeLoader that leaves ISO dates as strings.

    PyYAML turns an unquoted 2026-08-20 into a date object. The schemas describe
    dates as strings with format "date", and a date object would fail the type
    check before the format is ever looked at.
    """


_StringDatesLoader.yaml_implicit_resolvers = {
    first: [
        (tag, regexp)
        for tag, regexp in resolvers
        if tag != "tag:yaml.org,2002:timestamp"
    ]
    for first, resolvers in _StringDatesLoader.yaml_implicit_resolvers.items()
}


def load_yaml_text(text: str) -> Any:
    """Parse one YAML document from text. Empty text parses as None."""
    return yaml.load(text, Loader=_StringDatesLoader)  # noqa: S506 - SafeLoader subclass


def read_yaml(path: Path) -> Any:
    """Read one YAML document from a file."""
    return load_yaml_text(path.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Problem:
    """Something validate found, tied to the file it was found in."""

    source: Path | str
    message: str

    def __str__(self) -> str:
        return f"{self.source}: {self.message}"


@dataclass(frozen=True)
class Agent:
    """One agent.yaml that passed the schema, with typed access for the rules."""

    directory: Path
    data: Mapping[str, Any]

    @property
    def path(self) -> Path:
        return self.directory / AGENT_FILE

    @property
    def name(self) -> str:
        name: str = self.data["name"]
        return name

    @property
    def connections(self) -> tuple[str, ...]:
        return tuple(self.data.get("connections", ()))

    @property
    def tools(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(self.data.get("tools", ()))

    @property
    def env(self) -> Mapping[str, str]:
        env: Mapping[str, str] = self._agent_engine().get("env", {})
        return env

    @property
    def secret_env(self) -> Mapping[str, str]:
        secret_env: Mapping[str, str] = self._agent_engine().get("secret_env", {})
        return secret_env

    @property
    def instruction(self) -> str:
        instruction: str = self.data["instruction"]
        return instruction

    @property
    def instruction_path(self) -> Path | None:
        """The instruction file, or None when the instruction is written inline."""
        value = self.instruction
        if "\n" in value:
            return None
        if value.startswith(("./", "../", "/")) or value.endswith((".md", ".txt")):
            return self.directory / value
        return None

    @property
    def source(self) -> Path | None:
        value = self.data.get("source")
        return self.directory / value if value else None

    @property
    def requirements(self) -> Path | None:
        value = self.data.get("requirements")
        return self.directory / value if value else None

    def _agent_engine(self) -> Mapping[str, Any]:
        runtime: Mapping[str, Any] = self.data.get("runtime", {})
        agent_engine: Mapping[str, Any] = runtime.get("agent_engine", {})
        return agent_engine


@dataclass(frozen=True)
class Project:
    """gete.yaml and the agents found below it."""

    path: Path
    data: Mapping[str, Any]
    agents: tuple[Agent, ...]
    # Agents whose agent.yaml did not pass the schema. They are reported and
    # left out of agents, so the rules never see an unexpected shape.
    problems: tuple[Problem, ...] = ()

    @property
    def root(self) -> Path:
        return self.path.parent

    @property
    def agents_dir(self) -> Path:
        agents_dir: str = self.data.get("agents_dir", DEFAULT_AGENTS_DIR)
        return self.root / agents_dir

    @property
    def policy_files(self) -> tuple[Path, ...]:
        return tuple(self.root / entry for entry in self.data.get("policies", ()))

    @property
    def connection_overrides(self) -> Mapping[str, Mapping[str, Any]]:
        overrides: Mapping[str, Mapping[str, Any]] = self.data.get("connections", {})
        return overrides

    def display(self, path: Path) -> str:
        """Path as shown in messages: relative to the project root when below it."""
        try:
            return str(path.relative_to(self.root))
        except ValueError:
            return str(path)


def find_project_file(start: Path) -> Path:
    """Walk up from start until a gete.yaml is found, like git looks for .git."""
    for directory in (start, *start.resolve().parents):
        candidate = directory / PROJECT_FILE
        if candidate.is_file():
            return candidate
    raise DeclarationError(
        f"no {PROJECT_FILE} found in {start} or any parent directory"
    )


def load_project(path: Path) -> Project:
    """Read gete.yaml and every agents/*/agent.yaml below it.

    gete.yaml must pass its schema; nothing can be checked without it. Agents
    that fail theirs are collected as problems rather than raised, so one broken
    agent does not hide the state of the others.
    """
    data = read_yaml(path)
    validate_document("gete", data, source=path)
    project = Project(path=path, data=data, agents=())
    agents: list[Agent] = []
    problems: list[Problem] = []
    if project.agents_dir.is_dir():
        for directory in sorted(project.agents_dir.iterdir()):
            agent_file = directory / AGENT_FILE
            if not agent_file.is_file():
                continue
            agent_data = read_yaml(agent_file)
            found = schema_problems("agent", agent_data)
            if found:
                source = project.display(agent_file)
                problems.extend(Problem(source, message) for message in found)
                continue
            agents.append(Agent(directory=directory, data=agent_data))
    return Project(path=path, data=data, agents=tuple(agents), problems=tuple(problems))
