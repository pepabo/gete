"""Reading an OpenAPI description and holding a declaration against it."""

import json
from pathlib import Path
from typing import Any

import pytest

from gete.errors import DeclarationError
from gete.openapi import declaration_problems, load_spec, read_operations

MINIMAL = """
openapi: 3.0.0
info: {title: Example, version: "1.0"}
paths:
  /things:
    get:
      operationId: ListThings
      responses: {"200": {description: ok}}
"""


def test_load_spec_reads_yaml(tmp_path: Path) -> None:
    path = tmp_path / "spec.yaml"
    path.write_text(MINIMAL)
    spec = load_spec(path)
    assert "ListThings" in json.dumps(spec)


def test_load_spec_reads_json_too(tmp_path: Path) -> None:
    """Published descriptions come in either serialization."""
    path = tmp_path / "spec.json"
    path.write_text(
        json.dumps(
            {
                "openapi": "3.0.0",
                "info": {"title": "Example", "version": "1.0"},
                "paths": {},
            }
        )
    )
    assert load_spec(path)["openapi"] == "3.0.0"


def test_load_spec_survives_the_yaml_value_key(tmp_path: Path) -> None:
    """Real vendors publish specs with a bare = key, which SafeLoader refuses."""
    path = tmp_path / "spec.yaml"
    path.write_text("openapi: 3.0.0\npaths: {}\nx-legacy: {=: fallback}\n")
    assert load_spec(path)["x-legacy"] == {"=": "fallback"}


def test_load_spec_says_which_file_could_not_be_parsed(tmp_path: Path) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text("openapi: [unclosed")
    with pytest.raises(DeclarationError, match="broken.yaml"):
        load_spec(path)


def test_load_spec_says_when_the_file_is_missing(tmp_path: Path) -> None:
    with pytest.raises(DeclarationError, match="nowhere.yaml"):
        load_spec(tmp_path / "nowhere.yaml")


SPEC: dict[str, Any] = {
    "openapi": "3.0.0",
    "info": {"title": "Example", "version": "1.0"},
    # The published servers cannot be trusted; nothing below reads them.
    "servers": [{"url": "https://{tenant}.example.com"}],
    "paths": {
        "/search": {
            "get": {
                "operationId": "ListSearchResults",
                "description": "Vendor text.",
                "parameters": [
                    {"$ref": "#/components/parameters/Query"},
                    {"name": "per_page", "in": "query", "schema": {"type": "integer"}},
                ],
                "responses": {"200": {"description": "ok"}},
            }
        },
        "/tickets/{ticket_id}": {
            "parameters": [
                {
                    "name": "ticket_id",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "integer"},
                }
            ],
            "get": {
                "operationId": "ShowTicket",
                "responses": {"200": {"description": "ok"}},
            },
            "put": {
                "operationId": "UpdateTicket",
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/TicketUpdate"}
                        }
                    }
                },
                "responses": {"200": {"description": "ok"}},
            },
            "delete": {
                "operationId": "DeleteTicket",
                "responses": {"204": {"description": "gone"}},
            },
        },
    },
    "components": {
        "parameters": {
            "Query": {
                "name": "query",
                "in": "query",
                "required": True,
                "schema": {"type": "string"},
            }
        },
        "schemas": {
            "TicketUpdate": {
                "type": "object",
                "properties": {
                    "ticket": {
                        "type": "object",
                        "properties": {"status": {"type": "string"}},
                    }
                },
            }
        },
    },
}


def test_read_operations_indexes_by_operation_id() -> None:
    operations, duplicates = read_operations(SPEC)
    assert duplicates == []
    search = operations["ListSearchResults"]
    assert (search.path, search.method) == ("/search", "get")
    assert [p["name"] for p in search.parameters] == ["query", "per_page"]


def test_read_operations_resolves_parameter_references() -> None:
    operations, _ = read_operations(SPEC)
    query = operations["ListSearchResults"].parameters[0]
    assert query["required"] is True
    assert query["schema"]["type"] == "string"


def test_read_operations_merges_path_level_parameters() -> None:
    """A parameter declared on the path applies to every operation under it."""
    operations, _ = read_operations(SPEC)
    show = operations["ShowTicket"]
    assert [p["name"] for p in show.parameters] == ["ticket_id"]


def test_read_operations_resolves_the_request_body_schema() -> None:
    operations, _ = read_operations(SPEC)
    update = operations["UpdateTicket"]
    assert update.body is not None
    assert "ticket" in update.body["properties"]


def with_update_body(schema: dict[str, Any], **components: Any) -> dict[str, Any]:
    """SPEC with UpdateTicket's body schema replaced, plus extra components."""
    spec = json.loads(json.dumps(SPEC))
    spec["components"]["schemas"] = {
        **spec["components"]["schemas"],
        **components,
        "TicketUpdate": schema,
    }
    return spec


def test_read_operations_folds_an_allof_body_one_level() -> None:
    """Published bodies are often a shared core plus the operation's own
    fields; the properties have to show for the rules and the model alike."""
    spec = with_update_body(
        {
            "allOf": [
                {"$ref": "#/components/schemas/TicketCore"},
                {"properties": {"comment": {"type": "string"}}},
            ]
        },
        TicketCore={
            "type": "object",
            "properties": {"status": {"type": "string"}},
            "required": ["status"],
        },
    )
    operations, _ = read_operations(spec)
    update = operations["UpdateTicket"]
    assert update.body is not None
    assert set(update.body["properties"]) == {"status", "comment"}
    assert update.body["required"] == ["status"]
    assert update.body["type"] == "object"
    assert "allOf" not in update.body


def test_read_operations_reports_a_duplicated_operation_id() -> None:
    doubled = json.loads(json.dumps(SPEC))
    doubled["paths"]["/search"]["post"] = {
        "operationId": "ListSearchResults",
        "responses": {"200": {"description": "ok"}},
    }
    _, duplicates = read_operations(doubled)
    assert duplicates == ["ListSearchResults"]


def block(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "spec": "./spec.yaml",
        "connection": "example",
        "operations": ["ListSearchResults"],
        "effect": "read",
    }
    return {**base, **overrides}


def test_a_sound_declaration_has_no_problems() -> None:
    sound = block(
        operations=["ListSearchResults", "ShowTicket"],
        params={"ListSearchResults": {"query": {"prefix": "type:ticket "}}},
        describe={"ListSearchResults": "Search tickets."},
    )
    assert declaration_problems(sound, SPEC) == []


def test_an_operation_the_description_does_not_hold_is_reported() -> None:
    found = declaration_problems(block(operations=["NoSuchOperation"]), SPEC)
    assert found == [
        "operations: 'NoSuchOperation' is not an operationId in the description"
    ]


def test_a_duplicated_operation_id_cannot_be_selected() -> None:
    doubled = json.loads(json.dumps(SPEC))
    doubled["paths"]["/search"]["post"] = {
        "operationId": "ListSearchResults",
        "responses": {"200": {"description": "ok"}},
    }
    found = declaration_problems(block(), doubled)
    assert len(found) == 1
    assert "more than once" in found[0]


def test_a_delete_may_be_declared_as_a_write() -> None:
    sound = block(operations=["DeleteTicket"], effect="write")
    assert declaration_problems(sound, SPEC) == []


def test_a_delete_may_not_be_declared_as_a_read() -> None:
    found = declaration_problems(
        block(operations=["DeleteTicket"], effect="read"), SPEC
    )
    assert len(found) == 1
    assert "effect: write" in found[0]


def test_a_delete_with_a_request_body_is_refused() -> None:
    """The client sends no body on a DELETE; RFC 9110 gives one no meaning."""
    spec = json.loads(json.dumps(SPEC))
    spec["paths"]["/tickets/{ticket_id}"]["delete"]["requestBody"] = {
        "content": {"application/json": {"schema": {"type": "object"}}}
    }
    found = declaration_problems(
        block(operations=["DeleteTicket"], effect="write"), spec
    )
    assert len(found) == 1
    assert "request body" in found[0]


def test_a_write_method_may_not_be_declared_as_a_read() -> None:
    found = declaration_problems(
        block(operations=["UpdateTicket"], effect="read"), SPEC
    )
    assert len(found) == 1
    assert "effect: write" in found[0]


def test_a_write_method_passes_under_effect_write() -> None:
    sound = block(operations=["UpdateTicket"], effect="write")
    assert declaration_problems(sound, SPEC) == []


def test_params_must_name_a_selected_operation() -> None:
    found = declaration_problems(
        block(params={"ShowTicket": {"ticket_id": {"value": 1}}}), SPEC
    )
    assert found == ["params: 'ShowTicket' is not one of this block's operations"]


def test_describe_must_name_a_selected_operation() -> None:
    found = declaration_problems(block(describe={"ShowTicket": "A ticket."}), SPEC)
    assert found == ["describe: 'ShowTicket' is not one of this block's operations"]


def test_a_fix_naming_no_parameter_of_the_operation_is_reported() -> None:
    found = declaration_problems(
        block(params={"ListSearchResults": {"sort": {"value": "asc"}}}), SPEC
    )
    assert len(found) == 1
    assert "sort" in found[0]
    # The parameters that do exist are named, so the fix can be corrected.
    assert "query" in found[0] and "per_page" in found[0]


def test_a_fix_may_name_a_body_property() -> None:
    sound = block(
        operations=["UpdateTicket"],
        effect="write",
        params={"UpdateTicket": {"ticket": {"value": {"status": "open"}}}},
    )
    assert declaration_problems(sound, SPEC) == []


def test_prefix_needs_a_string_parameter() -> None:
    found = declaration_problems(
        block(params={"ListSearchResults": {"per_page": {"prefix": "p"}}}), SPEC
    )
    assert len(found) == 1
    assert "string" in found[0]


def test_a_fixed_value_needs_no_particular_type() -> None:
    sound = block(params={"ListSearchResults": {"per_page": {"value": 25}}})
    assert declaration_problems(sound, SPEC) == []


def test_a_required_header_parameter_cannot_be_declared() -> None:
    spec = json.loads(json.dumps(SPEC))
    spec["paths"]["/search"]["get"]["parameters"].append(
        {
            "name": "X-Team",
            "in": "header",
            "required": True,
            "schema": {"type": "string"},
        }
    )
    found = declaration_problems(block(), spec)
    assert len(found) == 1
    assert "header" in found[0]


def test_a_body_that_is_not_json_is_refused() -> None:
    spec = json.loads(json.dumps(SPEC))
    spec["paths"]["/tickets/{ticket_id}"]["put"]["requestBody"]["content"] = {
        "application/x-www-form-urlencoded": {"schema": {"type": "object"}}
    }
    found = declaration_problems(
        block(operations=["UpdateTicket"], effect="write"), spec
    )
    assert len(found) == 1
    assert "JSON" in found[0]


def test_a_body_whose_type_is_left_implicit_still_counts_as_an_object() -> None:
    """Published schemas often write properties without spelling type: object."""
    spec = with_update_body({"properties": {"ticket": {"type": "object"}}})
    sound = block(operations=["UpdateTicket"], effect="write")
    assert declaration_problems(sound, spec) == []


def test_a_fix_missing_from_an_allof_body_is_reported() -> None:
    """Folding makes the composed properties enumerable, so a miss is certain."""
    spec = with_update_body(
        {"allOf": [{"type": "object", "properties": {"status": {"type": "string"}}}]}
    )
    found = declaration_problems(
        block(
            operations=["UpdateTicket"],
            effect="write",
            params={"UpdateTicket": {"nope": {"value": 1}}},
        ),
        spec,
    )
    assert len(found) == 1
    assert "nope" in found[0]


def test_a_body_that_declares_no_properties_is_refused() -> None:
    """The parser would offer the model a single opaque body argument, and the
    request would carry the payload wrapped under a key the service never
    declared."""
    spec = with_update_body({"type": "object"})
    found = declaration_problems(
        block(operations=["UpdateTicket"], effect="write"), spec
    )
    assert len(found) == 1
    assert "properties" in found[0]


def test_a_body_declared_only_as_alternatives_is_refused() -> None:
    """oneOf without properties leaves nothing the rules or the model can hold."""
    spec = with_update_body({"oneOf": [{"type": "object"}, {"type": "string"}]})
    found = declaration_problems(
        block(operations=["UpdateTicket"], effect="write"), spec
    )
    assert len(found) == 1
    assert "properties" in found[0]


def test_a_fix_matching_a_parameter_and_a_body_property_is_refused() -> None:
    """The runtime applies fixes by name; one name in two places would fix both."""
    spec = json.loads(json.dumps(SPEC))
    spec["paths"]["/tickets/{ticket_id}"]["put"]["parameters"] = [
        {"name": "ticket", "in": "query", "schema": {"type": "string"}}
    ]
    found = declaration_problems(
        block(
            operations=["UpdateTicket"],
            effect="write",
            params={"UpdateTicket": {"ticket": {"value": "x"}}},
        ),
        spec,
    )
    assert len(found) == 1
    assert "both" in found[0]


def test_a_get_with_a_request_body_is_refused() -> None:
    """The client sends no body on a GET; the operation would lose arguments."""
    spec = json.loads(json.dumps(SPEC))
    spec["paths"]["/search"]["get"]["requestBody"] = {
        "content": {"application/json": {"schema": {"type": "object"}}}
    }
    found = declaration_problems(block(), spec)
    assert len(found) == 1
    assert "GET with a request body" in found[0]


def test_an_operation_id_that_cannot_name_a_tool_is_reported() -> None:
    spec = json.loads(json.dumps(SPEC))
    spec["paths"]["/search"]["get"]["operationId"] = "list search results"
    found = declaration_problems(block(operations=["list search results"]), spec)
    assert len(found) == 1
    assert "name" in found[0]
