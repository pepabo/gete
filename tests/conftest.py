"""Builders for declaration trees on disk, shared by the validate and CLI tests."""

from pathlib import Path
from typing import Any

import pytest
import yaml

MINIMAL_AGENT: dict[str, Any] = {
    "name": "mail-triage",
    "display_name": "Mail triage",
    "description": "Sorts pasted mail by urgency",
    "model": "gemini-2.5-flash",
    "instruction": "./instruction.md",
}


class ProjectBuilder:
    """Writes a gete.yaml and agents under a temporary root."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.agents_dir = root / "agents"
        self.agents_dir.mkdir()
        self.write_project(
            {"version": 1, "project": "example-project", "location": "us-central1"}
        )

    def write_project(self, document: dict[str, Any]) -> Path:
        path = self.root / "gete.yaml"
        path.write_text(yaml.safe_dump(document, sort_keys=False))
        return path

    def write_agent(
        self,
        directory: str,
        document: dict[str, Any] | None = None,
        *,
        instruction: str | None = "You sort mail.",
    ) -> Path:
        agent_dir = self.agents_dir / directory
        agent_dir.mkdir(parents=True, exist_ok=True)
        body = {**MINIMAL_AGENT, "name": directory, **(document or {})}
        (agent_dir / "agent.yaml").write_text(yaml.safe_dump(body, sort_keys=False))
        if instruction is not None:
            (agent_dir / "instruction.md").write_text(instruction)
        return agent_dir

    def write_policies(self, name: str, document: list[dict[str, Any]]) -> Path:
        path = self.root / "policies" / f"{name}.yaml"
        path.parent.mkdir(exist_ok=True)
        path.write_text(yaml.safe_dump(document, sort_keys=False))
        return path


@pytest.fixture
def project(tmp_path: Path) -> ProjectBuilder:
    return ProjectBuilder(tmp_path)
