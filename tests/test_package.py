"""Package shell. Pins down where the version comes from."""

import importlib.metadata
import tomllib
from pathlib import Path

import gete

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_version_matches_package_metadata() -> None:
    """gete.__version__ mirrors the metadata built from the git tag."""
    assert gete.__version__ == importlib.metadata.version("gete")


def test_version_is_not_written_statically_in_pyproject() -> None:
    """The tag is the only source of the version; a static one would be a second."""
    project = tomllib.loads(PYPROJECT.read_text())["project"]
    assert "version" not in project
    assert "version" in project["dynamic"]
