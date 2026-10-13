"""gete init: the files a new project and a new agent start from."""

import re
from pathlib import Path
from string import Template
from typing import Any

from gete._yaml import read_yaml
from gete.declaration import (
    AGENT_FILE,
    DEFAULT_AGENTS_DIR,
    PROJECT_FILE,
    visible_directories,
)
from gete.errors import DeclarationError
from gete.templates import template_text

# Same rule as the agent schema: an RFC 1034 label.
_LABEL = re.compile(r"^[a-z]([-a-z0-9]*[a-z0-9])?$")


def init_project(root: Path) -> list[Path]:
    """Write gete.yaml and policies/example.yaml if absent; return what was written."""
    return [
        path
        for path, text in (
            (root / PROJECT_FILE, template_text("gete.yaml")),
            (root / "policies" / "example.yaml", template_text("example_policy.yaml")),
        )
        if _write_if_absent(path, text)
    ]


def init_agent(root: Path, name: str) -> list[Path]:
    """Write agents/<name>/agent.yaml and instruction.md unless they exist.

    <engine>/<name> puts the agent below the engine's directory, where it is
    listed in that engine. The engine has to be named in gete.yaml already:
    validate would refuse the directory otherwise, and a scaffold that fails
    its own validator teaches the wrong shape.
    """
    *groups, name = name.split("/")
    if len(groups) > 1:
        raise DeclarationError(
            f"{'/'.join(groups)!r}: agents group one level deep, as <engine>/<name>"
        )
    if len(name) > 63 or not _LABEL.match(name):
        raise DeclarationError(
            f"{name!r} is not a valid agent name: lowercase letters, digits, hyphens"
        )
    declared: dict[str, Any] = {}
    project_file = root / PROJECT_FILE
    if project_file.is_file():
        declared = read_yaml(project_file) or {}
    agents_dir = Path(declared.get("agents_dir", DEFAULT_AGENTS_DIR))
    directory = root / agents_dir
    engines = (declared.get("gemini_enterprise") or {}).get("engines") or {}
    # An agent and an engine's group cannot share a directory: the loader
    # reads a directory with agent.yaml as the agent and steps over nothing
    # below it, so either shape written here would hide agents.
    if groups:
        if groups[0] not in engines:
            named = f" ({', '.join(engines)})" if engines else ""
            raise DeclarationError(
                f"{groups[0]!r} is not an engine named under "
                f"gemini_enterprise.engines in {PROJECT_FILE}{named}; name it "
                "there first, and agents below that directory are listed in it"
            )
        directory = directory / groups[0]
        if (directory / AGENT_FILE).is_file():
            raise DeclarationError(
                f"{groups[0]!r} is an agent, not the engine's group: "
                f"{agents_dir / groups[0] / AGENT_FILE} exists, and an agent and "
                "an engine's group cannot share a directory"
            )
    elif name in engines and any(
        (member / AGENT_FILE).is_file()
        for member in visible_directories(directory / name)
    ):
        raise DeclarationError(
            f"{name!r} is the engine's group and holds agents; an agent and an "
            "engine's group cannot share a directory"
        )
    directory = directory / name
    return [
        path
        for path, text in (
            (
                directory / AGENT_FILE,
                Template(template_text("agent.yaml")).substitute(name=name),
            ),
            (
                directory / "instruction.md",
                Template(template_text("instruction.md")).substitute(name=name),
            ),
        )
        if _write_if_absent(path, text)
    ]


def _write_if_absent(path: Path, text: str) -> bool:
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True
