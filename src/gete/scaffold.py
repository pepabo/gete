"""gete init: the files a new project and a new agent start from."""

import re
from pathlib import Path
from string import Template

from gete._yaml import read_yaml
from gete.declaration import AGENT_FILE, DEFAULT_AGENTS_DIR, PROJECT_FILE
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
    """Write agents/<name>/agent.yaml and instruction.md unless they exist."""
    if len(name) > 63 or not _LABEL.match(name):
        raise DeclarationError(
            f"{name!r} is not a valid agent name: lowercase letters, digits, hyphens"
        )
    agents_dir = root / DEFAULT_AGENTS_DIR
    project_file = root / PROJECT_FILE
    if project_file.is_file():
        declared = read_yaml(project_file) or {}
        agents_dir = root / declared.get("agents_dir", DEFAULT_AGENTS_DIR)
    directory = agents_dir / name
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
