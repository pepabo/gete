"""The resolved declaration: one file that carries everything the runtime needs."""

from pathlib import Path

import yaml
from conftest import ProjectBuilder

import gete
from gete.declaration import load_project, load_resolved, resolve


def write_project_with_policy(project: ProjectBuilder) -> None:
    project.write_policies(
        "finance",
        [
            {
                "name": "finance",
                "when": "always",
                "instruction_prefix": "Never approve.",
            },
            {
                "name": "writes",
                "when": "has_write_tools",
                "require_confirmation": "write_tools",
            },
        ],
    )
    project.write_project(
        {
            "version": 1,
            "project": "example-project",
            "location": "us-central1",
            "policies": ["./policies/finance.yaml"],
            "connections": {"github": {"base_url": "https://api.github.example.com"}},
        }
    )


def test_resolved_document_keeps_the_agent_and_adds_the_resolved_block(
    project: ProjectBuilder,
) -> None:
    write_project_with_policy(project)
    project.write_agent("mail-triage", {"connections": ["github"]})
    loaded = load_project(project.root / "gete.yaml")
    document = resolve(loaded, loaded.agents[0])
    assert document["name"] == "mail-triage"
    assert document["instruction"] == "./instruction.md"
    assert [p["name"] for p in document["resolved"]["policies"]] == [
        "finance",
        "writes",
    ]
    assert document["resolved"]["gete_version"] == gete.__version__


def test_resolved_connections_carry_the_overrides_and_every_known_prefix(
    project: ProjectBuilder,
) -> None:
    """The runtime builds its registry from this, with no catalog or gete.yaml."""
    write_project_with_policy(project)
    project.write_agent("mail-triage", {"connections": ["github"]})
    loaded = load_project(project.root / "gete.yaml")
    connections = resolve(loaded, loaded.agents[0])["resolved"]["connections"]
    assert connections["github"]["base_url"] == "https://api.github.example.com"
    assert "google" in connections, (
        "other services' prefixes are needed for elimination"
    )
    assert connections["slack"]["retired"]


def test_resolved_file_reads_back_without_gete_yaml(
    project: ProjectBuilder, tmp_path: Path
) -> None:
    write_project_with_policy(project)
    project.write_agent("mail-triage", {"connections": ["github"]})
    loaded = load_project(project.root / "gete.yaml")
    document = resolve(loaded, loaded.agents[0])

    elsewhere = tmp_path / "deployed"
    elsewhere.mkdir()
    (elsewhere / "agent.resolved.yaml").write_text(
        yaml.safe_dump(document, sort_keys=False)
    )
    (elsewhere / "instruction.md").write_text("You sort mail.")

    resolved = load_resolved(elsewhere / "agent.resolved.yaml")
    assert resolved.name == "mail-triage"
    assert resolved.instruction_text() == "You sort mail."
    assert [p.name for p in resolved.policies] == ["finance", "writes"]
    assert resolved.registry.get("github").allows(
        "https://api.github.example.com/repos"
    )
    assert resolved.gete_version == gete.__version__


def test_resolved_document_is_plain_yaml_types(project: ProjectBuilder) -> None:
    """It must survive safe_dump and safe_load unchanged; the archive stores that."""
    write_project_with_policy(project)
    project.write_agent("mail-triage", {"connections": ["github"]})
    loaded = load_project(project.root / "gete.yaml")
    document = resolve(loaded, loaded.agents[0])
    assert yaml.safe_load(yaml.safe_dump(document, sort_keys=False)) == document


def test_resolved_rejects_a_document_that_lost_its_resolved_block(
    tmp_path: Path,
) -> None:
    import pytest

    from gete.errors import DeclarationError

    path = tmp_path / "agent.resolved.yaml"
    path.write_text("name: x\n")
    with pytest.raises(DeclarationError, match="resolved"):
        load_resolved(path)


def test_resolved_rejects_a_connections_block_that_is_not_a_mapping(
    tmp_path: Path,
) -> None:
    """A hand-edited file must fail with a message, not an AttributeError."""
    import pytest
    from conftest import MINIMAL_AGENT

    from gete.errors import DeclarationError

    path = tmp_path / "agent.resolved.yaml"
    document = {
        **MINIMAL_AGENT,
        "instruction": "You sort mail.\nBe brief.",
        "resolved": {"policies": [], "connections": [], "gete_version": "0.0.0"},
    }
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    with pytest.raises(DeclarationError, match="connections"):
        load_resolved(path)
