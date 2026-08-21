"""Connections that ship with gete, one YAML file each."""

from importlib.resources import files
from typing import Any

from gete.declaration import load_yaml_text


def catalog_connections() -> dict[str, dict[str, Any]]:
    """Return the bundled connection definitions keyed by id, in file name order."""
    directory = files("gete.catalog").joinpath("connections")
    entries: dict[str, dict[str, Any]] = {}
    for resource in sorted(directory.iterdir(), key=lambda item: item.name):
        if not resource.name.endswith(".yaml"):
            continue
        entry: dict[str, Any] = load_yaml_text(resource.read_text(encoding="utf-8"))
        entries[resource.name.removesuffix(".yaml")] = entry
    return entries
