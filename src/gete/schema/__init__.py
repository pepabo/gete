"""JSON Schemas for the declarations, and the check that applies them."""

import json
from collections.abc import Iterator
from functools import cache
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from gete.errors import DeclarationError

Kind = Literal["gete", "agent", "connection", "policy"]


@cache
def load_schema(kind: Kind) -> dict[str, Any]:
    """Return the bundled schema for one kind of document."""
    text = files("gete.schema").joinpath(f"{kind}.json").read_text(encoding="utf-8")
    schema: dict[str, Any] = json.loads(text)
    return schema


@cache
def _validator(kind: Kind) -> Draft202012Validator:
    schema = load_schema(kind)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(
        schema, format_checker=Draft202012Validator.FORMAT_CHECKER
    )


def problems(kind: Kind, document: Any) -> list[str]:
    """List every way the document departs from the schema, as 'path: message' lines."""
    return [
        _describe(error) for error in _sorted(_validator(kind).iter_errors(document))
    ]


def validate_document(kind: Kind, document: Any, *, source: str | Path) -> None:
    """Raise DeclarationError naming the source and every problem, or return quietly."""
    found = problems(kind, document)
    if found:
        lines = "\n".join(f"  {line}" for line in found)
        raise DeclarationError(f"{source} does not match the {kind} schema:\n{lines}")


def _sorted(errors: Iterator[ValidationError]) -> list[ValidationError]:
    return sorted(errors, key=lambda error: list(map(str, error.absolute_path)))


def _describe(error: ValidationError) -> str:
    path = ".".join(str(part) for part in error.absolute_path) or "(root)"
    # For oneOf/anyOf the top-level message only says "is not valid under any of
    # the given schemas". The nested errors say which key was wrong.
    if error.context:
        deepest = max(error.context, key=lambda nested: len(nested.absolute_path))
        return f"{path}: {error.message} ({deepest.message})"
    return f"{path}: {error.message}"
