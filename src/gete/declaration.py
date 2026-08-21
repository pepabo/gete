"""Reading declarations from disk: gete.yaml, the agents below it, and their shapes."""

from collections.abc import Mapping
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Any

from gete._yaml import load_yaml_text, read_yaml
from gete.connection import Registry
from gete.errors import DeclarationError
from gete.policies import Policy, load_policy_documents
from gete.schema import problems as schema_problems
from gete.schema import validate_document

__all__ = [
    "AGENT_FILE",
    "PROJECT_FILE",
    "RESOLVED_FILE",
    "Agent",
    "Problem",
    "Project",
    "Resolved",
    "find_project_file",
    "load_project",
    "load_resolved",
    "load_yaml_text",
    "read_yaml",
    "resolve",
]

PROJECT_FILE = "gete.yaml"
AGENT_FILE = "agent.yaml"
RESOLVED_FILE = "agent.resolved.yaml"
DEFAULT_AGENTS_DIR = "agents"
RESOLVED_KEY = "resolved"
# Until Gemini Enterprise is known to surface ADK's confirmation flow, the
# policy text and the agent's own two-step tools are what governs writes.
DEFAULT_CONFIRMATION = "instruction"


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

    def instruction_text(self) -> str:
        """The instruction itself: the file's content, or the inline text."""
        path = self.instruction_path
        return path.read_text(encoding="utf-8") if path else self.instruction

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


def resolve(project: Project, agent: Agent) -> dict[str, Any]:
    """Fold gete.yaml into one agent's declaration.

    The deployment has no gete.yaml, so the policies and the connection
    definitions travel with the agent under a "resolved" key. Every known
    connection is embedded, not just the agent's: a connection without token
    prefixes is accepted by elimination against the others' prefixes.
    """
    registry = Registry.from_catalog(
        project.connection_overrides, source=project.display(project.path)
    )
    return {
        **agent.data,
        RESOLVED_KEY: {
            "policies": load_policy_documents(project.policy_files),
            "connections": registry.documents(),
            "confirmation": project.data.get("confirmation", DEFAULT_CONFIRMATION),
            "gete_version": version("gete"),
        },
    }


@dataclass(frozen=True)
class Resolved:
    """A resolved declaration as read back where it was deployed."""

    path: Path
    data: Mapping[str, Any]

    @property
    def agent(self) -> Agent:
        return Agent(directory=self.path.parent, data=self.data)

    @property
    def name(self) -> str:
        return self.agent.name

    def instruction_text(self) -> str:
        return self.agent.instruction_text()

    @property
    def policies(self) -> tuple[Policy, ...]:
        return tuple(Policy.from_mapping(entry) for entry in self._resolved["policies"])

    @property
    def registry(self) -> Registry:
        return Registry.from_documents(self._resolved["connections"])

    @property
    def confirmation(self) -> str:
        return str(self._resolved.get("confirmation", DEFAULT_CONFIRMATION))

    @property
    def gete_version(self) -> str:
        gete_version: str = self._resolved["gete_version"]
        return gete_version

    @property
    def _resolved(self) -> Mapping[str, Any]:
        resolved: Mapping[str, Any] = self.data[RESOLVED_KEY]
        return resolved


def load_resolved(path: Path) -> Resolved:
    """Read an agent.resolved.yaml and check every part of it against its schema."""
    data = read_yaml(path)
    if not isinstance(data, Mapping) or not isinstance(data.get(RESOLVED_KEY), Mapping):
        raise DeclarationError(
            f"{path} has no {RESOLVED_KEY!r} block; was it written by gete?"
        )
    resolved = data[RESOLVED_KEY]
    for key in ("policies", "connections", "gete_version"):
        if key not in resolved:
            raise DeclarationError(f"{path}: {RESOLVED_KEY}.{key} is missing")
    if not isinstance(resolved["connections"], Mapping):
        raise DeclarationError(
            f"{path}: {RESOLVED_KEY}.connections is not a mapping of id to connection"
        )
    agent_part = {key: value for key, value in data.items() if key != RESOLVED_KEY}
    validate_document("agent", agent_part, source=path)
    validate_document(
        "policy", resolved["policies"], source=f"{path}: {RESOLVED_KEY}.policies"
    )
    for connection_id, document in resolved["connections"].items():
        validate_document(
            "connection",
            document,
            source=f"{path}: {RESOLVED_KEY}.connections.{connection_id}",
        )
    return Resolved(path=path, data=data)
