"""YAML reading shared by the catalog and the declarations."""

from pathlib import Path
from typing import Any

import yaml


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
